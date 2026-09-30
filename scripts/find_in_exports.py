#!/usr/bin/env python
"""在存下来的每日收入导出（attachments/ 下的 xlsx）里找客户：哪天、哪个站点、收入多少。**只读。**

    uv run python scripts/find_in_exports.py --uid 577809185790524801 --uid 2234927971478601216

看板只收「新加坡站」的行（schema.BOARD_STATION_IN_SCOPE）。财务表里有、看板上没有的交易，
用它看是不是在别的站点 —— 是的话看板本来就不会收，要不要算进渠道佣金是业务上的决定。
导出里也找不到，就是那几天的导出本身没有这个客户（或者那封邮件的附件没存下来）。
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.pipeline.export import BoardImportError, parse_workbook  # noqa: E402
from crm_basebot.startup import load_settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在每日导出里找客户")
    parser.add_argument("--uid", action="append", required=True, help="客户UID，可以给几个")
    parser.add_argument(
        "--dir", help="导出所在的目录，默认 .env 的 DAILY_EXPORT_DIR（attachments）"
    )
    args = parser.parse_args(argv)

    directory = Path(args.dir or load_settings().daily_export_dir or "attachments")
    files = sorted(directory.glob("*.xlsx"))
    if not files:
        print(f"{directory} 下没有 xlsx。导出存在哪里？用 --dir 指定。")
        return 1
    wanted = {u.strip() for u in args.uid}
    # uid -> (日期, 站点) -> 收入
    hits: dict[str, dict[tuple[str, str], float]] = defaultdict(dict)
    days: set = set()
    for path in files:
        try:
            rows = parse_workbook(path)
        except BoardImportError as exc:
            print(f"  跳过 {path.name}：{exc}")
            continue
        for row in rows:
            day = row.order_date
            days.add(day)
            uid = str(row.fields.get(schema.BOARD_CLIENT_UID) or "")
            if uid in wanted:
                key = (str(day), str(row.fields.get(schema.BOARD_STATION) or ""))
                revenue = row.fields.get(schema.BOARD_TOTAL_REVENUE) or 0
                hits[uid][key] = hits[uid].get(key, 0) + float(revenue)

    known = sorted(d for d in days if d)
    span = f"{known[0]} 到 {known[-1]}" if known else "（没读到日期）"
    print(f"看了 {len(files)} 份导出，覆盖 {span}。看板只收「{schema.BOARD_STATION_IN_SCOPE}」。\n")
    for uid in sorted(wanted):
        print(f"━━ UID {uid}")
        if not hits.get(uid):
            print("   这些导出里一行都没有。")
            continue
        for (day, station), revenue in sorted(hits[uid].items()):
            mark = "" if station == schema.BOARD_STATION_IN_SCOPE else "　← 不是新加坡站，看板不收"
            print(f"   {day}　{station}　收入 {revenue:,.2f}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
