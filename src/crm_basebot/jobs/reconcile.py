"""按月对账：算佣金并把汇总写回 Base。

    uv run python -m crm_basebot.jobs.reconcile --period 2026-03
    uv run python -m crm_basebot.jobs.reconcile --period 2026-03 --write

默认**只算不写**。要真的写进 Base 得显式加 ``--write`` —— 这是一次会改动结算
数据的操作，不应该手滑就发生。

算之前先校验交易明细表的结构。那张表是同事每天手工导入维护的，列名被改过而
我们浑然不觉地继续算，是这个系统最容易出的事故。
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime

from ..config import get_settings
from ..domain import schema
from ..domain.audit import ACTION_COMPUTE_COMMISSION, AuditLog
from ..domain.commission import CommissionCalculator, CommissionRow, summarize
from ..lark.bitable import BitableClient, assert_fields_present

logger = logging.getLogger(__name__)


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
        )
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按月计算渠道佣金")
    parser.add_argument(
        "--period",
        help="结算月份 YYYY-MM，默认上个月",
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

    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    period = None if args.all_periods else (args.period or _last_month())

    bitable = BitableClient(settings.base_app_token)

    assert_fields_present(
        bitable.list_fields(settings.table_transaction),
        schema.TXN_REQUIRED_FIELDS,
        table_label="交易明细表",
    )

    calculator = CommissionCalculator(bitable, settings=settings)
    rows, unmapped = calculator.compute(period=period, strict=args.strict)

    print(f"\n结算范围：{period or '全部月份'}\n")
    print(summarize(rows))

    if unmapped:
        print(
            f"\n注意：{len(unmapped)} 个客户在交易明细里有记录但没登记归属渠道，"
            "这部分 Pnl 没有计入任何佣金："
        )
        for uid in unmapped[:20]:
            print(f"    {uid}")
        if len(unmapped) > 20:
            print(f"    …… 还有 {len(unmapped) - 20} 个")

    if not args.write:
        print("\n（只算没写。确认无误后加 --write 写进 Base）")
        return 0

    if not settings.table_commission:
        print("\nTABLE_COMMISSION 没配，写不了。", flush=True)
        return 1

    written = _write_rows(bitable, settings.table_commission, rows)

    AuditLog(bitable, settings.table_audit).record(
        actor_open_id="system",
        actor_name="对账任务",
        action=ACTION_COMPUTE_COMMISSION,
        target_table=schema.TABLE_COMMISSION_NAME,
        detail={
            "结算范围": period or "全部月份",
            "写入行数": written,
            "未登记客户数": len(unmapped),
        },
    )

    print(f"\n已写入 {written} 行汇总。")
    return 0


def _last_month() -> str:
    now = datetime.now(UTC)
    year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{year}-{month:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
