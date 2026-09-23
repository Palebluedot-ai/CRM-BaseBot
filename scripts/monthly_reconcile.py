#!/usr/bin/env python
"""每月结算：把上个月的**交易**佣金汇总写进 Commission Summary，然后把结果发给管理员。

ECAS 开户返佣不在这里 —— 那是另一套账，见 ``crm_basebot.jobs.ecas_reconcile``
和 ``docs/ECAS.md``。两边的金额是同一个量级，所以卡片上会写明这个数只含交易佣金。

    uv run python scripts/monthly_reconcile.py                      # 预演：算上个月，不写不发
    uv run python scripts/monthly_reconcile.py --apply               # 写 + 通知
    uv run python scripts/monthly_reconcile.py --period 2026-08 --apply
    uv run python scripts/monthly_reconcile.py --apply --no-notify   # 写了但不发消息

三件事值得说清楚：

**为什么每月 3 号跑，不是 1 号。** 上个月最后一天的交易，内部系统那封邮件通常
第二天早上才发。1 号跑等于把最后一天漏掉，而汇总一旦写进去就是结算快照 ——
发现漏了要 ``--replace`` 重来，还得跟已经看过数字的人解释一遍。留两天缓冲便宜得多。

**通知里的数字是从 Base 读回来的，不是脚本自己算完报给你的。** 后者只能证明
"脚本以为自己写了什么"，前者证明"Base 里现在实际有什么"。写接口返回成功不等于
值进去了，这条在别的脚本里已经踩过（见 set_sales_open_id.py 的回读确认）。

**同一个月跑第二次不会重复写。** reconcile 本身就拒绝往已有数据的月份写，
这里把那种拒绝当成正常结果（已经结算过了），照样发通知、退出码 0 —— 否则
launchd 每个月都会报一次失败，久了就没人看了。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import re
import sys
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
from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.jobs import reconcile  # noqa: E402
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


def build_card(period: str, count: int, total: float, *, already: bool, unmapped: str) -> dict:
    lines = [
        f"**{period}** 交易佣金结算"
        + ("（本月之前已经结算过，这次没有重复写入）" if already else "已写入 Commission Summary"),
        "",
        f"渠道数：**{count}**",
        f"应付合计：**{total:,.2f} USD**",
        # 这一句不是客套。ECAS 开户返佣是另一套账、另一张汇总表（见 docs/ECAS.md），
        # 金额和这里同一个量级。不写明的话，看卡片的人会把这个数当成「这个月一共要付多少」，
        # 照着它开票就会漏掉 ECAS 那一半。
        "（只含交易佣金，**不含 ECAS 开户返佣** —— 那部分在 ECAS Commission Summary）",
    ]
    if unmapped:
        lines += [
            "",
            f"另有 {unmapped} 个客户在看板里有收入但没登记归属渠道，这部分**没有**计入上面的金额。",
        ]
    lines += ["", "开发票前请在 Base 的 Commission Summary 里核对一遍。"]
    return cards.notice_card(
        f"月结 {period}",
        "\n".join(lines),
        template="blue" if already else "green",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每月结算并通知管理员")
    parser.add_argument("--period", help="结算月份 YYYY-MM，不传就是上个月")
    parser.add_argument("--apply", action="store_true", help="真写进 Base；不加则只预演")
    parser.add_argument("--no-notify", action="store_true", help="不发消息，只写")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_COMMISSION", "TABLE_SALES")
    bitable = BitableClient(settings.base_app_token)

    today = today_in_business_tz(settings.business_timezone)
    period = args.period or previous_period(today)
    print(f"=== 月结 {period}（今天 {today}）===\n")

    before_count, _ = read_summary(bitable, settings.table_commission, period)

    argv_inner = ["--period", period] + (["--write"] if args.apply else [])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = reconcile.main(argv_inner)
    output = buffer.getvalue()
    print(output)

    if not args.apply:
        print("预演：没有写 Base，也没有发通知。确认无误后加 --apply。")
        return 0

    after_count, after_total = read_summary(bitable, settings.table_commission, period)

    # reconcile 拒绝往已有数据的月份写时会返回非 0。这个月本来就有汇总的话，那是
    # 幂等的正常结果，不是故障 —— 当成成功，否则每月一次的假告警很快就没人看了。
    already = before_count > 0
    if code != 0 and not already:
        print(f"\n结算失败（退出码 {code}），不发通知。", file=sys.stderr)
        return code

    match = UNMAPPED_RE.search(output)
    unmapped = match.group(1) if match else ""

    print(f"Base 里 {period} 现有 {after_count} 条，应付合计 {after_total:,.2f} USD")

    if args.no_notify:
        return 0

    recipients = admin_open_ids(bitable, settings.table_sales)
    if not recipients:
        print(
            "\n名册里没有任何「在职 + 管理员 + 有 OpenID」的人，通知没发出去。"
            "\n要收到月结通知，请在 Sales Directory 里把角色设成「管理员」并填上 OpenID。",
            file=sys.stderr,
        )
        return 0

    card = build_card(period, after_count, after_total, already=already, unmapped=unmapped)
    client = get_client()
    sent = 0
    for open_id, name in recipients:
        if send_card(client, open_id, card):
            sent += 1
            print(f"  已通知 {name}")
    print(f"通知 {sent}/{len(recipients)} 人。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
