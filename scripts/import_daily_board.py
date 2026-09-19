#!/usr/bin/env python
"""把内部系统导出的交易明细 xlsx 导入 Base 的看板表（按日期整天替换）。

## 日常不要用这个脚本

它按导出覆盖到的**所有**日期整天替换 —— 一次 1,600+ 行、25+ 次批量调用，而且删完还没
写完的时候表是空的。每天跑的是增量：

    uv run python scripts/import_daily_incremental.py --from-mail

这个脚本留给两种场景：

    · 回填（第一次灌数据、或换 Base）
    · 历史被修订：--date 2026-09-16 只重导那一天，语义最明确

## 表头就是合同

xlsx 的表头以 2026-09-17 的「OTC组销售明细」导出为准：18 列逐字对应
``schema.DAILY_BOARD_FIELDS``，Base 里的列名和它一模一样，不做改名。少任何一列直接
拒绝并点名缺的是哪列；多出来的列忽略并提示。

## 逻辑在哪

全部住在 ``crm_basebot.pipeline``（解析 = export.py，写库 = board.py，见那个包的
``__init__``）。这里只有「解析命令行 + 打印」，不重新实现任何规则。

    uv run python scripts/import_daily_board.py --file 明细.xlsx --dry-run   # 只解析
    uv run python scripts/import_daily_board.py --file 明细.xlsx             # 真导入
    uv run python scripts/import_daily_board.py --file 明细.xlsx --date 2026-09-16
    uv run python scripts/import_daily_board.py        # 用 .env 里的 DAILY_BOARD_XLSX
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.pipeline import (  # noqa: E402
    EXPECTED_HEADERS,
    BoardImportError,
    BoardRow,
    apply_import,
    client_links,
    describe_stations,
    parse_workbook,
    split_by_station,
)
from crm_basebot.pipeline.report import print_link_summary as _print_link_summary  # noqa: E402
from crm_basebot.pipeline.report import print_summary as _print_summary  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

# 旧名字。测试与调用方按这个调用，模块里的正名是 apply_import。
_apply = apply_import

__all__ = [
    "EXPECTED_HEADERS",
    "BoardImportError",
    "BoardRow",
    "_apply",
    "build_parser",
    "main",
    "parse_workbook",
    "run",
    "split_by_station",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把交易明细 xlsx 里新加坡站的记录导入 Base 的日读看板表"
    )
    parser.add_argument("--file", help="xlsx 文件路径。不传就用 .env 里的 DAILY_BOARD_XLSX")
    parser.add_argument("--date", help="只导这一天 YYYY-MM-DD。xlsx 里其他日期的行忽略")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析和统计，不碰 Base。先用它核对表头和数据",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_DAILY_BOARD", "TABLE_CLIENT")
    return run(args, settings, BitableClient(settings.base_app_token))


def run(args: argparse.Namespace, settings, bitable: BitableClient) -> int:
    """入口的主体。settings 和 bitable 从外面传进来，测试里换成假件就能把整条路走一遍。"""
    xlsx_path = Path(args.file or settings.daily_board_xlsx or "")
    if not xlsx_path or str(xlsx_path) == ".":
        print(
            "没指定 xlsx：加 --file /path/to.xlsx，或在 .env 里设 DAILY_BOARD_XLSX。",
            file=sys.stderr,
        )
        return 1

    only_date = None
    if args.date:
        try:
            only_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"--date 格式要是 YYYY-MM-DD，你给的是 {args.date!r}", file=sys.stderr)
            return 1

    print(f"读取：{xlsx_path}")
    if only_date:
        print(f"只导：{only_date}")

    try:
        rows = parse_workbook(xlsx_path, only_date=only_date)
    except BoardImportError as exc:
        print(f"\n导入失败：{exc}", file=sys.stderr)
        return 1

    if not rows:
        print("没有匹配的行可导入。")
        return 0

    kept, counts = split_by_station(rows)
    print(f"站点：{describe_stations(counts)}")
    if not kept:
        found = "、".join(f"{station} {n} 行" for station, n in counts.most_common())
        print(
            f"\n没有导入：这份导出里一行「{schema.BOARD_STATION_IN_SCOPE}」都没有，"
            f"只有 {found}。\n"
            "Base 没有动。看一眼是不是导错了文件，或者站点的写法变了。",
            file=sys.stderr,
        )
        return 1

    _print_summary(kept)

    if args.dry_run:
        print("\n--dry-run：只解析没写 Base。去掉这个开关才会真正导入。")
        return 0

    tz = ZoneInfo(settings.business_timezone)
    print(
        "\n先删导出覆盖到的日期上的旧记录，再写新记录。"
        f"日期按 {settings.business_timezone} 的零点写入。"
    )
    # 客户UID -> 记录 id 只读一次，写给每一行用。这一步是「匹配」，不是「算钱」：
    # 算钱在 Base 的公式里。客户表为空也照常导入，只是所有行都挂不上关联。
    links = client_links(bitable, settings.table_client)
    # 替换范围取**解析出来的所有行**（含其他站点）：导出是它覆盖的每一天的权威，
    # 某天导出里只剩香港站就说明这天新加坡站没有记录，Base 里那天的旧记录也要清掉，
    # 否则更正过的数据会留着旧账。写进去的只有 kept（新加坡站）。
    covered = {row.order_date for row in rows}
    deleted, written = apply_import(
        bitable,
        settings.table_daily_board,
        kept,
        tz=tz,
        replace_dates=covered,
        client_links_map=links,
    )
    print(f"删除 {deleted} 条，写入 {written} 条。")
    _print_link_summary(kept, links)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
