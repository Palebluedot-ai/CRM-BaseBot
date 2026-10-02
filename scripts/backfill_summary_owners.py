#!/usr/bin/env python
"""给两张结算表（交易、ECAS）已有的行补「归属销售」。

    uv run python scripts/backfill_summary_owners.py            # 预演：数一数会补几行
    uv run python scripts/backfill_summary_owners.py --apply    # 真补

为什么（2026-10-02）：Base 的高级权限能设「销售只看归属销售是自己的行」，但结算表原来没有
人员字段，只有渠道编号。之后写的行会自动带上（jobs/reconcile.py、jobs/ecas_reconcile.py、
jobs/live_summary.py）；这个脚本把以前的行补齐。

归属销售照渠道表那一行的「登记人OpenID」填。**只补空着的**，已经有的不改；渠道表里这个渠道
没填 OpenID 的，列出来跳过。只改这一列，金额、状态一个字都不动。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.settlement import owner_value, referral_owners  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import extract_text  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402


def plan(records, owners: dict[str, str]) -> tuple[dict[str, dict], Counter[str]]:
    """(record_id -> 要写的字段, 渠道表里没有 OpenID 的渠道编号 -> 行数)。纯函数，方便测。"""
    updates: dict[str, dict] = {}
    missing: Counter[str] = Counter()
    for record in records:
        if record.fields.get(schema.COMM_OWNER):
            continue
        no = extract_text(record.fields.get(schema.COMM_REFERRAL_NO)).strip()
        open_id = owners.get(no)
        if open_id:
            updates[record.record_id] = {schema.COMM_OWNER: owner_value(open_id)}
        else:
            missing[no or "（没有渠道编号）"] += 1
    return updates, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给结算表补归属销售")
    parser.add_argument("--apply", action="store_true", help="真补；不加则只预演")
    args = parser.parse_args(argv)

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_REFERRAL", "TABLE_COMMISSION")
    bitable = BitableClient(settings.base_app_token)
    owners = referral_owners(bitable, settings.table_referral)

    tables = [("交易佣金 Commission Summary", settings.table_commission)]
    if getattr(settings, "table_ecas_commission", ""):
        tables.append(("ECAS Commission Summary", settings.table_ecas_commission))

    for label, table_id in tables:
        columns = {f.name for f in bitable.list_fields(table_id)}
        if schema.COMM_OWNER not in columns:
            print(
                f"{label} 还没有「{schema.COMM_OWNER}」这一列。"
                "先跑：uv run python scripts/sync_base.py --apply"
            )
            return 1
        updates, missing = plan(list(bitable.iter_records(table_id)), owners)
        print(f"━━ {label}：要补 {len(updates)} 行")
        for no, count in sorted(missing.items()):
            print(f"   跳过 {no}（{count} 行）：渠道表里这个渠道没填登记人OpenID")
        if args.apply and updates:
            done = bitable.batch_update_records(table_id, updates)
            print(f"   补好了 {done} 行")

    if not args.apply:
        print("\n（预演，没写。确认后加 --apply）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
