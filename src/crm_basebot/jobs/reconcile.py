"""按月对账：算佣金并把汇总写回 Base。

    uv run python -m crm_basebot.jobs.reconcile                     # 最新有数据的月份
    uv run python -m crm_basebot.jobs.reconcile --period 2026-03
    uv run python -m crm_basebot.jobs.reconcile --period 2026-03 --write
    uv run python -m crm_basebot.jobs.reconcile --period 2026-03 --write --replace
    uv run python -m crm_basebot.jobs.reconcile --all-periods

默认**只算不写**。要真的写进 Base 得显式加 ``--write`` —— 这是一次会改动结算
数据的操作，不应该手滑就发生。

``--write`` 遇到汇总表里已经有本次结算月份的行时会拒绝，而不是在旁边再写一套：
两套同月汇总摆在一起，看报表的人分不清哪套是对的，求和还会翻倍。数据改过要重算，
加 ``--replace``：先删掉那些月份的旧行，再写新的（2026-09-05 定的）。删的范围就是
本次结算的月份，``--all-periods --replace`` 则清空整张汇总表。算出来是空的时候
不会拿空结果去顶掉旧汇总 —— 看板没导完就跑一次，不该把上个月好好的账删没。

先删后写没有事务：删完写到一半失败，表里就是半套数据。这种情况下再跑一次
``--write --replace`` 就好，不需要人工清理。

不传 ``--period`` 时结算**日读看板里最新有数据的那个月**，不是「上个月」。理由见
``CommissionCalculator.compute_latest``。实际选中的月份一定会打印出来，不用猜。

算之前先校验日读看板的结构。那张表由 scripts/import_daily_board.py 每天从 xlsx
导入，列名被改过而我们浑然不觉地继续算，是这个系统最容易出的事故。
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import defaultdict

from ..domain import schema
from ..domain.audit import ACTION_COMPUTE_COMMISSION, AuditLog
from ..domain.commission import CommissionCalculator, CommissionRow, summarize
from ..lark.bitable import BitableClient, assert_fields_present
from ..lark.values import extract_text, uid_health_advice
from ..startup import load_settings, require_settings

logger = logging.getLogger(__name__)

# 算一次佣金要读的三张表：日读看板出总收入，客户表把 UID 归到渠道，渠道表给分佣比例。
REQUIRED_KEYS = (
    "LARK_BASE_APP_TOKEN",
    "TABLE_DAILY_BOARD",
    "TABLE_CLIENT",
    "TABLE_REFERRAL",
)

# --write 才需要的两张。刻意在开算之前就查：全量拉一遍表要花掉不少 API 额度，
# 算完了才发现写不进去，那次调用就白费了。
WRITE_KEYS = ("TABLE_COMMISSION", "TABLE_AUDIT")


class WriteRefused(RuntimeError):
    """汇总表的现状不允许这次写入。入口把它打出来、以非 0 退出，一行都不改。"""


def existing_summary(
    bitable: BitableClient, table_id: str, periods: set[str] | None
) -> dict[str, list[str]]:
    """汇总表里已有的行，按结算月份分组，值是 record_id。``periods`` 给 None 表示不限月份。

    只拉结算月份这一列。汇总表很小（每月每渠道一行），扫一遍比按月份 filter 少一种
    请求形态，也不用担心字段名对不上时 filter 静默返回空、让检查形同虚设。
    """
    found: dict[str, list[str]] = defaultdict(list)
    for record in bitable.iter_records(table_id, field_names=[schema.COMM_PERIOD]):
        period = extract_text(record.fields.get(schema.COMM_PERIOD))
        if periods is None or period in periods:
            found[period].append(record.record_id)
    return dict(found)


def _describe(existing: dict[str, list[str]]) -> str:
    return "、".join(
        f"{period or '(月份为空)'}（{len(ids)} 行）" for period, ids in sorted(existing.items())
    )


def write_summary(
    bitable: BitableClient,
    table_id: str,
    rows: list[CommissionRow],
    *,
    periods: set[str] | None,
    replace: bool,
) -> tuple[int, int]:
    """把汇总行写进 Base，返回 (删除行数, 写入行数)。

    ``periods`` 是本次结算覆盖的月份，None 表示全部。汇总表里已经有这些月份的行时：
    不带 ``replace`` 直接拒绝；带了就先删旧行再写新行。两种拒绝都发生在动手之前，
    拒绝了就一行都没动。
    """
    existing = existing_summary(bitable, table_id, periods)

    if existing and not replace:
        raise WriteRefused(
            f"汇总表里已经有 {_describe(existing)} 的汇总，不会在旁边再写一套。"
            "要用这次的结果顶掉它们，加 --replace（先删旧行再写新行）。"
        )

    if existing and not rows:
        raise WriteRefused(
            f"这次算出来是空的，不会拿空结果去顶掉汇总表里已有的 {_describe(existing)}。"
            "先确认看板导全了、客户都登记了，再跑。"
        )

    deleted = 0
    for record_ids in existing.values():
        for record_id in record_ids:
            bitable.delete_record(table_id, record_id)
            deleted += 1

    return deleted, _write_rows(bitable, table_id, rows)


def _write_rows(bitable: BitableClient, table_id: str, rows: list[CommissionRow]) -> int:
    now_ms = int(time.time() * 1000)
    written = 0
    for row in rows:
        bitable.create_record(
            table_id,
            {
                schema.COMM_PERIOD: row.period,
                schema.COMM_REFERRAL_NO: row.referral_no,
                schema.COMM_REFERRAL_NAME: row.referral_name,
                schema.COMM_CLIENT_COUNT: row.client_count,
                schema.COMM_TXN_COUNT: row.txn_count,
                schema.COMM_REVENUE_TOTAL: float(row.revenue_total),
                schema.COMM_RATE: float(row.rate_percent),
                schema.COMM_PAYABLE: float(row.payable),
                schema.COMM_COMPUTED_AT: now_ms,
            },
            # 汇总表没有要读回来的系统字段，一行一个往返就够了
            reread=False,
        )
        written += 1
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按月计算渠道佣金")
    parser.add_argument(
        "--period",
        help="结算月份 YYYY-MM。不传的话结算交易明细里最新有数据的那个月，实际选中的月份会打印出来",
    )
    parser.add_argument(
        "--all-periods",
        action="store_true",
        help="算所有月份，忽略 --period",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="把汇总写进 Base。不加这个就只打印结果。汇总表里已有本次月份的行时会拒绝",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="汇总表里已有本次结算月份的行时，先删掉它们再写。只和 --write 一起用",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="交易明细里有未登记归属的客户时直接失败",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.replace and not args.write:
        parser.error("--replace 只在 --write 时有意义：不写就没有什么可替换的")

    settings = load_settings()
    require_settings(settings, *REQUIRED_KEYS)
    if args.write:
        require_settings(settings, *WRITE_KEYS)

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    return run(args, settings, BitableClient(settings.base_app_token))


def run(args: argparse.Namespace, settings, bitable: BitableClient) -> int:
    """入口的主体。settings 和 bitable 从外面传进来，单测里换成假件就能把整条路走一遍。"""
    assert_fields_present(
        bitable.list_fields(settings.table_daily_board),
        schema.DAILY_BOARD_REQUIRED_FIELDS,
        table_label="日读看板",
    )

    calculator = CommissionCalculator(bitable, settings=settings)

    if args.all_periods:
        period = None
        rows, unmapped = calculator.compute(strict=args.strict)
        scope = "全部月份"
    elif args.period:
        period = args.period
        rows, unmapped = calculator.compute(period=period, strict=args.strict)
        scope = f"{period}（你显式指定的）"
    else:
        period, rows, unmapped = calculator.compute_latest(strict=args.strict)
        if not period:
            # 定不出默认月份就没法往下走。返回非 0 是有意的：看板空了，
            # 或者整列交易日期都解析不出来，都说明上游导入出了问题，该被告警看见。
            print(
                "\n定不出要结算哪个月：日读看板是空的，"
                f"或者没有一行的「{schema.BOARD_ORDER_DATE}」能解析出 YYYY-MM。\n"
                "先确认 scripts/import_daily_board.py 跑过了，或者用 --period YYYY-MM 显式指定。"
            )
            return 1
        scope = f"{period}（自动选定：日读看板里最新有数据的月份）"

    # 把实际结算的月份原原本本打出来。默认值是算出来的而不是写死的，
    # 不打印的话，看报表的人没法确认这个数对应的是哪个月。
    print(f"\n结算范围：{scope}\n")
    print(summarize(rows))

    if period and not rows:
        print(
            f"\n{period} 有交易数据，但没有任何一笔能归属到已登记的渠道。看下面的未登记客户清单。"
        )

    # UID 体检。算钱之前发现比事后对账发现便宜得多 —— 一旦 UID 被 Excel 改坏，
    # 佣金会静默算到别的渠道头上。这是启发式，会误报，所以只告警不中止。
    health = calculator.uid_health()
    if health.verdict in ("likely_damaged", "inconclusive"):
        print(f"\n{'!' * 60}")
        print(uid_health_advice(health))
        print("!" * 60)

    if unmapped:
        # 只有显式 --period 时，未登记清单才是限定在那个月的；另外两种模式下是全表范围。
        # 未登记归属是数据问题，不该因为这次只结算一个月就被藏起来。
        scope_note = "" if args.period else "（全表范围，不限本月）"
        print(
            f"\n注意：{len(unmapped)} 个客户在日读看板里有记录但没登记归属渠道{scope_note}，"
            "这部分收入没有计入任何佣金："
        )
        for uid in unmapped[:20]:
            print(f"    {uid}")
        if len(unmapped) > 20:
            print(f"    …… 还有 {len(unmapped) - 20} 个")

    if not args.write:
        print("\n（只算没写。确认无误后加 --write 写进 Base）")
        return 0

    # 本次结算覆盖的月份：--all-periods 是全部，另外两种模式都是单个月份。
    # 默认模式的月份是算出来的，所以旧汇总的检查只能放在这里，没法提前到开算之前。
    target_periods = None if args.all_periods else {period}
    try:
        deleted, written = write_summary(
            bitable,
            settings.table_commission,
            rows,
            periods=target_periods,
            replace=args.replace,
        )
    except WriteRefused as exc:
        print(f"\n没有写入：{exc}")
        return 1

    AuditLog(bitable, settings.table_audit).record(
        actor_open_id="system",
        actor_name="对账任务",
        action=ACTION_COMPUTE_COMMISSION,
        target_table=schema.TABLE_COMMISSION_NAME,
        detail={
            # 记的是实际结算的月份，不是命令行传进来的原始值 —— 默认值是算出来的，
            # 审计里必须能看出那次跑的到底是哪个月。
            "结算范围": period or "全部月份",
            "替换": args.replace,
            "删除行数": deleted,
            "写入行数": written,
            "未登记客户数": len(unmapped),
            "UID体检": health.verdict,
        },
    )

    if deleted:
        print(f"\n已删除 {deleted} 行旧汇总，写入 {written} 行。")
    else:
        print(f"\n已写入 {written} 行汇总。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
