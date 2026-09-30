#!/usr/bin/env python
"""给一个已经结算过的月份补建存档（月结每月 3 号会自动做，这个是补旧月份用的）。

    uv run python scripts/archive_month.py --period 2026-08            # 预演：列出会写什么
    uv run python scripts/archive_month.py --period 2026-08 --apply    # 真建表

建「2026-08 结算明细」「2026-08 结算汇总」两张表，并追加进「结算明细（全部月份）」。
已经存过的不动。渠道金额只认结算表；补存旧月份时客户明细按**现在**的资料分摊（近似），
渠道合计仍和结算一分不差。逻辑在 ``crm_basebot.jobs.archive``。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain.commission_query import CommissionQueryService  # noqa: E402
from crm_basebot.jobs import archive  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补建一个月的结算存档")
    parser.add_argument("--period", required=True, help="YYYY-MM，要已经结算过")
    parser.add_argument("--apply", action="store_true", help="真建表；不加则只预演")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}", args.period):
        parser.error("--period 要是 YYYY-MM")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_COMMISSION", "TABLE_REFERRAL")
    bitable = BitableClient(settings.base_app_token)

    if args.apply:
        result = archive.archive_period(settings, bitable, get_client(), args.period)
        print(result.line)
        return 0

    snapshot = archive.build_snapshot(
        bitable,
        settings,
        args.period,
        tz=ZoneInfo(settings.business_timezone),
        commission_query=(
            CommissionQueryService(bitable, settings=settings)
            if settings.table_daily_board
            else None
        ),
    )
    if not snapshot.summary:
        print(f"结算表里没有 {args.period}，先结算再存档。")
        return 1
    for kind in (archive.KIND_TRADE, archive.KIND_ECAS):
        details = [d for d in snapshot.details if d.kind == kind]
        print(
            f"{kind}：结算 {snapshot.total(kind):,.2f}，"
            f"明细 {len(details)} 行合计 {sum(d.amount for d in details):,.2f}"
        )
    print(
        f"会建「{archive.detail_table_name(args.period)}」「{archive.summary_table_name(args.period)}」，"
        f"并追加进「{archive.CUMULATIVE_TABLE}」。（预演，没写。确认后加 --apply）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
