"""按月对账：算佣金并把汇总写回 Base。

    uv run python -m crm_basebot.jobs.reconcile                     # 最新有数据的月份
    uv run python -m crm_basebot.jobs.reconcile --period 2026-03
    uv run python -m crm_basebot.jobs.reconcile --period 2026-03 --write
    uv run python -m crm_basebot.jobs.reconcile --all-periods

默认**只算不写**。要真的写进 Base 得显式加 ``--write`` —— 这是一次会改动结算
数据的操作，不应该手滑就发生。

不传 ``--period`` 时结算**交易明细里最新有数据的那个月**，不是「上个月」。理由见
``CommissionCalculator.compute_latest``。实际选中的月份一定会打印出来，不用猜。

算之前先校验交易明细表的结构。那张表是同事每天手工导入维护的，列名被改过而
我们浑然不觉地继续算，是这个系统最容易出的事故。
"""

from __future__ import annotations

import argparse
import logging
import time

from ..domain import schema
from ..domain.audit import ACTION_COMPUTE_COMMISSION, AuditLog
from ..domain.commission import CommissionCalculator, CommissionRow, summarize
from ..lark.bitable import BitableClient, assert_fields_present
from ..lark.values import uid_health_advice
from ..startup import load_settings, require_settings

logger = logging.getLogger(__name__)

# 算一次佣金要读的三张表：交易明细出 Pnl，客户表把 UID 归到渠道，渠道表给分佣比例。
REQUIRED_KEYS = (
    "LARK_BASE_APP_TOKEN",
    "TABLE_TRANSACTION",
    "TABLE_CLIENT",
    "TABLE_REFERRAL",
)

# --write 才需要的两张。刻意在开算之前就查：全量拉一遍表要花掉不少 API 额度，
# 算完了才发现写不进去，那次调用就白费了。
WRITE_KEYS = ("TABLE_COMMISSION", "TABLE_AUDIT")


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
                schema.COMM_PNL_TOTAL: float(row.pnl_total),
                schema.COMM_RATE: float(row.rate_percent),
                schema.COMM_PAYABLE: float(row.payable),
                schema.COMM_COMPUTED_AT: now_ms,
            },
            # 汇总表没有要读回来的系统字段，一行一个往返就够了
            reread=False,
        )
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
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
        help="把汇总写进 Base。不加这个就只打印结果",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="交易明细里有未登记归属的客户时直接失败",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    require_settings(settings, *REQUIRED_KEYS)
    if args.write:
        require_settings(settings, *WRITE_KEYS)

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    bitable = BitableClient(settings.base_app_token)

    assert_fields_present(
        bitable.list_fields(settings.table_transaction),
        schema.TXN_REQUIRED_FIELDS,
        table_label="交易明细表",
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
            # 定不出默认月份就没法往下走。返回非 0 是有意的：交易明细空了，
            # 或者整列订单时间都解析不出来，都说明上游导入出了问题，该被告警看见。
            print(
                "\n定不出要结算哪个月：交易明细是空的，"
                f"或者没有一行的「{schema.TXN_ORDER_TIME}」能解析出 YYYY-MM。\n"
                "先确认同事的导入跑过了，或者用 --period YYYY-MM 显式指定。"
            )
            return 1
        scope = f"{period}（自动选定：交易明细里最新有数据的月份）"

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
            f"\n注意：{len(unmapped)} 个客户在交易明细里有记录但没登记归属渠道{scope_note}，"
            "这部分 Pnl 没有计入任何佣金："
        )
        for uid in unmapped[:20]:
            print(f"    {uid}")
        if len(unmapped) > 20:
            print(f"    …… 还有 {len(unmapped) - 20} 个")

    if not args.write:
        print("\n（只算没写。确认无误后加 --write 写进 Base）")
        return 0

    written = _write_rows(bitable, settings.table_commission, rows)

    AuditLog(bitable, settings.table_audit).record(
        actor_open_id="system",
        actor_name="对账任务",
        action=ACTION_COMPUTE_COMMISSION,
        target_table=schema.TABLE_COMMISSION_NAME,
        detail={
            # 记的是实际结算的月份，不是命令行传进来的原始值 —— 默认值是算出来的，
            # 审计里必须能看出那次跑的到底是哪个月。
            "结算范围": period or "全部月份",
            "写入行数": written,
            "未登记客户数": len(unmapped),
            "UID体检": health.verdict,
        },
    )

    print(f"\n已写入 {written} 行汇总。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
