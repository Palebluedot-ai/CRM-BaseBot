#!/usr/bin/env python
"""给一个已经结算过的月份补建存档（月结每月 3 号会自动做，这个是补旧月份用的）。

    uv run python scripts/archive_month.py --period 2026-08            # 预演：列出会写什么
    uv run python scripts/archive_month.py --period 2026-08 --apply    # 真建表
    uv run python scripts/archive_month.py --period 2026-01 2026-02 2026-03 --apply   # 一次几个月

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
    parser.add_argument("--period", required=True, nargs="+", help="YYYY-MM，可以给几个")
    parser.add_argument("--apply", action="store_true", help="真建表；不加则只预演")
    args = parser.parse_args(argv)
    for period in args.period:
        if not re.fullmatch(r"\d{4}-\d{2}", period):
            parser.error(f"--period 要是 YYYY-MM，这个不对：{period}")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_COMMISSION", "TABLE_REFERRAL")
    bitable = BitableClient(settings.base_app_token)

    commission_query = (
        CommissionQueryService(bitable, settings=settings) if settings.table_daily_board else None
    )
    client = get_client() if args.apply else None
    failed = 0
    for period in sorted(set(args.period)):
        print(f"━━ {period}")
        snapshot = archive.build_snapshot(
            bitable,
            settings,
            period,
            tz=ZoneInfo(settings.business_timezone),
            commission_query=commission_query,
        )
        if not snapshot.summary:
            print(f"   结算表里没有 {period}，先结算再存档。")
            failed += 1
            continue
        for kind in (archive.KIND_TRADE, archive.KIND_ECAS):
            details = [d for d in snapshot.details if d.kind == kind]
            print(
                f"   {kind}：结算 {snapshot.total(kind):,.2f}，"
                f"明细 {len(details)} 行合计 {sum((d.amount for d in details), archive.ZERO):,.2f}"
            )
        problems = snapshot.mismatches()
        if problems:
            print("   ✗ 自检没过，这个月不存：")
            for line in problems:
                print(f"     {line}")
            failed += 1
            continue
        print("   ✓ 自检：每个渠道的明细合计都等于结算数")
        if args.apply:
            print(
                "   "
                + archive.write_archive(bitable, client, settings.base_app_token, snapshot).line
            )
    if not args.apply:
        print("\n（预演，没写。确认后加 --apply）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
