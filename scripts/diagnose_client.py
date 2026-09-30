#!/usr/bin/env python
"""查一个客户：登记在哪个渠道、看板上每个月有多少交易、挂上没有。**只读。**

    uv run python scripts/diagnose_client.py --name "HAN BAO"
    uv run python scripts/diagnose_client.py --uid 577809185790524801

财务表里有、我们算不出来的客户，用它看卡在哪一步：

  · 客户表里没有                → 没登记
  · 登记了，但挂在别的渠道        → 归属不对
  · 登记了，看板上这个月没有交易  → 看板的来源（每日收入导出，只收新加坡站）里没有这笔；
                                   财务表用的是逐笔成交记录，两边来源不同
  · 看板上有交易，但没挂上关联    → 跑 scripts/relink_board.py --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.commission import period_of  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import (  # noqa: E402
    PrecisionLossError,
    extract_text,
    link_ids,
    to_number,
    to_uid,
)
from crm_basebot.startup import load_settings, require_settings  # noqa: E402


def norm(name: str) -> str:
    return re.sub(r"[^0-9A-Z一-鿿]", "", (name or "").upper())


def _uid(value) -> str:
    try:
        return to_uid(value)
    except PrecisionLossError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查一个客户卡在哪")
    parser.add_argument("--name", help="客户名称（忽略大小写和标点）")
    parser.add_argument("--uid", help="客户UID")
    args = parser.parse_args(argv)
    if not args.name and not args.uid:
        parser.error("--name 和 --uid 至少给一个")

    settings = load_settings()
    require_settings(
        settings, "LARK_BASE_APP_TOKEN", "TABLE_CLIENT", "TABLE_REFERRAL", "TABLE_DAILY_BOARD"
    )
    bitable = BitableClient(settings.base_app_token)
    tz = ZoneInfo(settings.business_timezone)
    wanted = norm(args.name or "")

    referrals = {
        r.record_id: (
            extract_text(r.fields.get(schema.REFERRAL_NO)),
            extract_text(r.fields.get(schema.REFERRAL_NAME)),
        )
        for r in bitable.iter_records(
            settings.table_referral, field_names=[schema.REFERRAL_NO, schema.REFERRAL_NAME]
        )
    }

    print("━━ 客户表")
    uids: set[str] = {args.uid.strip()} if args.uid else set()
    found = 0
    for record in bitable.iter_records(settings.table_client):
        f = record.fields
        uid = _uid(f.get(schema.CLIENT_UID))
        name = extract_text(f.get(schema.CLIENT_NAME))
        if not ((args.uid and uid == args.uid.strip()) or (wanted and norm(name) == wanted)):
            continue
        found += 1
        uids.add(uid)
        linked = [
            referrals[rid]
            for rid in link_ids(f.get(schema.CLIENT_REFERRAL_LINK))
            if rid in referrals
        ]
        channel = " ".join(linked[0]) if linked else "（没挂渠道）"
        print(
            f"   {name}　UID {uid}　渠道 {channel}"
            f"　AI {extract_text(f.get(schema.CLIENT_AI_STATUS)) or '（空，按 AI 算）'}"
        )
    if not found:
        print("   没有登记。")

    print("\n━━ 看板（Daily Revenue Board）")
    by_month: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
    names_seen: set[str] = set()
    for record in bitable.iter_records(settings.table_daily_board):
        f = record.fields
        uid = _uid(f.get(schema.BOARD_CLIENT_UID))
        name = extract_text(f.get(schema.BOARD_CLIENT_NAME))
        if uid not in uids and not (wanted and norm(name) == wanted):
            continue
        names_seen.add(f"{name}（UID {uid}）")
        month = period_of(f.get(schema.BOARD_ORDER_DATE), tz=tz)
        row = by_month[month]
        row[0] += 1
        row[1] += Decimal(str(to_number(f.get(schema.BOARD_TOTAL_REVENUE)) or 0))
        if not link_ids(f.get(schema.BOARD_CLIENT_LINK)):
            row[2] += 1
    if not by_month:
        print(
            "   看板上一行都没有。看板来自每日收入导出，只收新加坡站；"
            "这个客户的交易不在那份导出里（财务表是逐笔成交记录，来源不同）。"
        )
        return 0
    print(f"   看板上的写法：{'、'.join(sorted(names_seen))}")
    for month in sorted(by_month):
        count, revenue, unlinked = by_month[month]
        note = f"　其中 {unlinked} 行没挂上关联" if unlinked else ""
        print(f"   {month}　{count} 行　收入 {revenue:,.2f}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
