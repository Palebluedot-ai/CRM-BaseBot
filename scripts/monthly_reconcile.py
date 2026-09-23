#!/usr/bin/env python
"""每月结算：把上个月的两套账都结掉，然后把结果发给管理员。

    uv run python scripts/monthly_reconcile.py                      # 预演：算上个月，不写不发
    uv run python scripts/monthly_reconcile.py --apply               # 写 + 通知
    uv run python scripts/monthly_reconcile.py --period 2026-08 --apply
    uv run python scripts/monthly_reconcile.py --apply --no-notify   # 写了但不发消息
    uv run python scripts/monthly_reconcile.py --apply --skip-ecas   # 只结交易佣金

**一个任务结两套账，发一张卡。** 交易佣金和 ECAS 开户返佣的算法完全独立
（``jobs/reconcile.py`` 和 ``jobs/ecas_reconcile.py``，两边不共用任何数据），
但「每个月 3 号把上个月结掉、告诉管理员多少钱」是一件运维的事，不是两件。

分成两个任务发两张卡的话，收卡片的人得自己把两个数加起来 —— 而 2026-08 那个月
交易佣金 19,294.51、ECAS 65,000.00，只看到前一张就去开票会漏掉四分之三的钱。
所以卡片上两套分别列出，底下给一个**两项合计**。

一边失败不影响另一边：ECAS 没结成，交易佣金那半照发，卡片上写明 ECAS 这次没算出来。
``TABLE_ECAS_COMMISSION`` 空着时整节跳过 —— 这个脚本在没上 ECAS 的租户里照样能用。

四件事值得说清楚：

**为什么每月 3 号跑，不是 1 号。** 上个月最后一天的交易，内部系统那封邮件通常
第二天早上才发。1 号跑等于把最后一天漏掉，而汇总一旦写进去就是结算快照 ——
发现漏了要 ``--replace`` 重来，还得跟已经看过数字的人解释一遍。留两天缓冲便宜得多。

**通知里的数字是从 Base 读回来的，不是脚本自己算完报给你的。** 后者只能证明
"脚本以为自己写了什么"，前者证明"Base 里现在实际有什么"。写接口返回成功不等于
值进去了，这条在别的脚本里已经踩过（见 set_sales_open_id.py 的回读确认）。

**同一个月跑第二次不会重复写。** reconcile 本身就拒绝往已有数据的月份写，
这里把那种拒绝当成正常结果（已经结算过了），照样发通知、退出码 0 —— 否则
launchd 每个月都会报一次失败，久了就没人看了。两套账各自独立判断。

**ECAS 的数据不是每天自动来的。** 交易看板每天从邮件导一次，ECAS 申请表要人手工跑
``scripts/import_ecas.py``。所以结算前会读一下申请表里最新那笔申请是什么时候，
截止日期早于结算月末就在卡片上写出来 —— 「这个月只结出 5,000」和「这个月的申请
还没导进来」在金额上长得一模一样，不说破没人会发现。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lark_oapi.api.im.v1 import (  # noqa: E402
    CreateMessageRequest,
    CreateMessageRequestBody,
)

from crm_basebot.bot import cards  # noqa: E402
from crm_basebot.domain import ecas, schema  # noqa: E402
from crm_basebot.domain.ecas_query import latest_applied_date  # noqa: E402
from crm_basebot.jobs import ecas_reconcile, reconcile  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.lark.values import extract_text, to_number  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)

# reconcile 打印未登记客户时的那一行，用来在通知里提一句"这个月有多少收入没算进任何渠道"。
# 抓不到就不提 —— 通知少一行无所谓，为了它让整个月结失败是本末倒置。
UNMAPPED_RE = re.compile(r"注意：(\d+) 个客户在日读看板里有记录但没登记归属渠道")


def today_in_business_tz(timezone_name: str) -> date:
    """业务时区的今天。用机器本地时区的话，月初那几个小时会算成上上个月。"""
    return datetime.now(ZoneInfo(timezone_name)).date()


def previous_period(today: date) -> str:
    """上个月，形如 2026-08。"""
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{year:04d}-{month:02d}"


def read_summary(bitable: BitableClient, table_id: str, period: str) -> tuple[int, float]:
    """从 Base 读回这个月已经写进去的 (行数, 应付合计)。"""
    count, total = 0, 0.0
    for record in bitable.iter_records(
        table_id, field_names=[schema.COMM_PERIOD, schema.COMM_PAYABLE]
    ):
        if extract_text(record.fields.get(schema.COMM_PERIOD)) != period:
            continue
        count += 1
        total += to_number(record.fields.get(schema.COMM_PAYABLE)) or 0.0
    return count, total


def read_ecas_summary(bitable: BitableClient, table_id: str, period: str) -> tuple[int, float]:
    """从 Base 读回 ECAS 汇总里这个月的 (行数, 应付合计)。

    和 ``read_summary`` 分开写而不是传字段名进去：两张表的列名恰好一样是巧合，
    不是约定。哪天 ECAS 那张表的列改了名，该炸的是这个函数，不是两张表一起错。
    """
    count, total = 0, 0.0
    for record in bitable.iter_records(
        table_id, field_names=[ecas.ECOMM_PERIOD, ecas.ECOMM_PAYABLE]
    ):
        if extract_text(record.fields.get(ecas.ECOMM_PERIOD)) != period:
            continue
        count += 1
        total += to_number(record.fields.get(ecas.ECOMM_PAYABLE)) or 0.0
    return count, total


@dataclass
class Book:
    """一套账这次结算的结果。两套账都走这个形状，卡片那边就不用分两套写法。"""

    label: str
    table_name: str
    count: int = 0
    total: float = 0.0
    already: bool = False
    failed: bool = False
    note: str = ""

    @property
    def status_text(self) -> str:
        if self.failed:
            return "**这次没有算出来**"
        if self.already:
            return f"（之前已经结算过，这次没有重复写入）已在 {self.table_name}"
        return f"已写入 {self.table_name}"


def admin_open_ids(bitable: BitableClient, table_id: str) -> list[tuple[str, str]]:
    """名册里在职的管理员 -> [(open_id, 姓名)]。

    收件人从名册来，不写死在脚本或 .env 里：换了管理员、多了一个管理员，
    改 Base 那一格就生效，不用动代码也不用重装 launchd 任务。
    """
    out: list[tuple[str, str]] = []
    for record in bitable.iter_records(table_id):
        if extract_text(record.fields.get(schema.SALES_ROLE)) != schema.ROLE_ADMIN:
            continue
        if extract_text(record.fields.get(schema.SALES_STATUS)) == schema.SALES_STATUS_DISABLED:
            continue
        open_id = extract_text(record.fields.get(schema.SALES_OPEN_ID))
        if open_id:
            out.append((open_id, extract_text(record.fields.get(schema.SALES_NAME))))
    return out


def send_card(client, open_id: str, card: dict) -> bool:
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = client.im.v1.message.create(request)
    if not response.success():
        logger.error("发给 %s 失败: %s %s", open_id, response.code, response.msg)
        return False
    return True


def build_card(period: str, books: list[Book]) -> dict:
    """两套账一张卡，底下给两项合计。

    合计那一行是这张卡存在的主要理由：2026-08 交易佣金 19,294.51、ECAS 65,000.00，
    只看其中一个数去开票会漏掉四分之三的钱。失败的那套不计入合计，而且会明说
    ——「少算了」和「没算」不能长成一样。
    """
    lines: list[str] = []
    for book in books:
        lines.append(f"**{book.label}**  {book.status_text}")
        if not book.failed:
            lines.append(f"  {book.count} 个渠道 · 应付 **{book.total:,.2f}** USD")
        if book.note:
            lines.append(f"  {book.note}")
        lines.append("")

    settled = [b for b in books if not b.failed]
    if len(settled) > 1:
        lines.append(f"**两项合计  {sum(b.total for b in settled):,.2f} USD**")
    if any(b.failed for b in books):
        lines.append(
            "<font color='grey'>上面的合计**不含**没算出来的那套。服务端日志里有原因。</font>"
        )
    lines += ["", "开发票前请在 Base 里核对一遍。"]

    if any(b.failed for b in books):
        template = "orange"
    elif all(b.already for b in books):
        template = "blue"
    else:
        template = "green"
    return cards.notice_card(f"月结 {period}", "\n".join(lines), template=template)


def settle(
    label: str,
    table_name: str,
    *,
    module,
    bitable: BitableClient,
    table_id: str,
    period: str,
    apply: bool,
    read_back,
) -> tuple[Book, str]:
    """跑一套账的结算，返回 (结果, 那次结算打印出来的全文)。

    ``module`` 是 ``jobs.reconcile`` 或 ``jobs.ecas_reconcile`` —— 两个模块的
    ``main()`` 签名和开关刻意一致，所以这里能用同一段代码驱动。**它们内部一行都不共用**。

    数字是从 Base **读回来**的，不是拿 module 算完的结果报给你。后者只能证明
    「脚本以为自己写了什么」，前者证明「Base 里现在实际有什么」。
    """
    before_count, _ = read_back(bitable, table_id, period)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = module.main(["--period", period] + (["--write"] if apply else []))
    output = buffer.getvalue()

    if not apply:
        return Book(label=label, table_name=table_name), output

    count, total = read_back(bitable, table_id, period)
    # 拒绝往已有数据的月份写时返回非 0。这个月本来就有汇总的话，那是幂等的正常结果，
    # 不是故障 —— 当成成功，否则每月一次的假告警很快就没人看了。
    already = before_count > 0
    return (
        Book(
            label=label,
            table_name=table_name,
            count=count,
            total=total,
            already=already,
            failed=code != 0 and not already,
        ),
        output,
    )


def ecas_freshness(
    bitable: BitableClient, table_id: str, period: str, *, timezone_name: str
) -> str:
    """ECAS 申请表的数据截止到哪天。晚于结算月末就返回空串（没什么好提醒的）。

    交易看板每天从邮件自动导，ECAS 申请表要人手工跑 import_ecas.py。所以「这个月
    只结出 5,000」和「这个月的申请还没导进来」在金额上长得一模一样。
    """
    try:
        through = latest_applied_date(bitable, table_id, tz=ZoneInfo(timezone_name))
    except Exception:  # noqa: BLE001 - 提醒取不到无所谓，不该让整个月结失败
        logger.exception("读取 ECAS 申请表的最新申请时间失败")
        return ""
    if not through:
        return "⚠️ ECAS Applications 表是空的 —— 先跑 scripts/import_ecas.py"
    # 截止日期落在结算月之内（或更早）就说明那个月的申请可能还没导全
    if through[:7] > period:
        return ""
    return (
        f"⚠️ 申请表的数据只到 {through}，之后的申请还没导进来 —— 先跑 scripts/import_ecas.py 再重算"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每月结算两套账并通知管理员")
    parser.add_argument("--period", help="结算月份 YYYY-MM，不传就是上个月")
    parser.add_argument("--apply", action="store_true", help="真写进 Base；不加则只预演")
    parser.add_argument("--no-notify", action="store_true", help="不发消息，只写")
    parser.add_argument(
        "--skip-ecas",
        action="store_true",
        help="只结交易佣金，不碰 ECAS（ECAS 表没配时本来就会自动跳过）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_COMMISSION", "TABLE_SALES")
    bitable = BitableClient(settings.base_app_token)

    today = today_in_business_tz(settings.business_timezone)
    period = args.period or previous_period(today)
    print(f"=== 月结 {period}（今天 {today}）===\n")

    # ---------- 交易佣金 ----------
    print("--- 交易佣金 ---")
    trades, output = settle(
        "交易佣金",
        schema.TABLE_COMMISSION_NAME,
        module=reconcile,
        bitable=bitable,
        table_id=settings.table_commission,
        period=period,
        apply=args.apply,
        read_back=read_summary,
    )
    print(output)

    match = UNMAPPED_RE.search(output)
    if match:
        trades.note = (
            f"另有 {match.group(1)} 个客户在看板里有收入但没登记归属渠道，"
            "这部分**没有**计入上面的金额"
        )

    # ---------- ECAS 返佣 ----------
    # 没配表就整节跳过 —— 没上 ECAS 的租户跑这个脚本不该报错。
    run_ecas = bool(settings.table_ecas and settings.table_ecas_commission) and not args.skip_ecas
    ecas_book: Book | None = None
    if run_ecas:
        print("--- ECAS 开户返佣 ---")
        ecas_book, ecas_output = settle(
            "ECAS 开户返佣",
            ecas.TABLE_ECAS_COMMISSION_NAME,
            module=ecas_reconcile,
            bitable=bitable,
            table_id=settings.table_ecas_commission,
            period=period,
            apply=args.apply,
            read_back=read_ecas_summary,
        )
        print(ecas_output)
        ecas_book.note = ecas_freshness(
            bitable,
            settings.table_ecas,
            period,
            timezone_name=settings.business_timezone,
        )
    elif args.skip_ecas:
        print("--- ECAS 开户返佣：--skip-ecas，跳过 ---\n")
    else:
        print("--- ECAS 开户返佣：TABLE_ECAS / TABLE_ECAS_COMMISSION 没配，跳过 ---\n")

    books = [trades] + ([ecas_book] if ecas_book else [])

    if not args.apply:
        print("预演：没有写 Base，也没有发通知。确认无误后加 --apply。")
        return 0

    for book in books:
        print(f"Base 里 {period} 的{book.label}：{book.count} 条，应付合计 {book.total:,.2f} USD")

    # 交易佣金结不出来就不发通知 —— 这是这个任务的主要产出，它没了这张卡没什么好报的。
    # ECAS 失败不拦：交易佣金那个数是真的，照发，卡片上写明 ECAS 这次没算出来。
    if trades.failed:
        print("\n交易佣金结算失败，不发通知。", file=sys.stderr)
        return 1

    if args.no_notify:
        return 1 if any(b.failed for b in books) else 0

    recipients = admin_open_ids(bitable, settings.table_sales)
    if not recipients:
        print(
            "\n名册里没有任何「在职 + 管理员 + 有 OpenID」的人，通知没发出去。"
            "\n要收到月结通知，请在 Sales Directory 里把角色设成「管理员」并填上 OpenID。",
            file=sys.stderr,
        )
        return 0

    card = build_card(period, books)
    client = get_client()
    sent = 0
    for open_id, name in recipients:
        if send_card(client, open_id, card):
            sent += 1
            print(f"  已通知 {name}")
    print(f"通知 {sent}/{len(recipients)} 人。")

    # 通知发出去了，但确实有一套没结成 —— 人知道了，退出码也得如实说，
    # 否则 launchd 的日志里这次跑看起来一切正常。
    return 1 if any(b.failed for b in books) else 0


if __name__ == "__main__":
    raise SystemExit(main())
