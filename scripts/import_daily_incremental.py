#!/usr/bin/env python
"""每日增量导入：只把「新加坡站 + 新增交易日」的行写进看板。

## 每天跑这个

    uv run python scripts/import_daily_incremental.py --from-mail          # 去邮箱取最新附件
    uv run python scripts/import_daily_incremental.py --dry-run            # 只看会导什么
    uv run python scripts/import_daily_incremental.py --file 明细.xlsx      # 用指定的本地文件
    uv run python scripts/import_daily_incremental.py --refresh 2026-09-16  # 强制重导某天

和全量脚本的分工：全量（import_daily_board.py）按导出覆盖到的所有日期整天替换，一次
1,600+ 行；增量只算「导出里有、看板里没有」的交易日，日常就是新增的那一天。

增量**看不见历史修订**（导出改了旧日期的金额，看板不会跟进）—— 那正是 ``--refresh``
和大范围回填脚本存在的原因。

## 逻辑在哪

全部住在 ``crm_basebot.pipeline``：取数 = source.py，解析 = export.py，算增量 =
delta.py，写库 = board.py，编排 = daily.py（见那个包的 ``__init__``）。这个文件只做
命令行解析、把 ``DailyError`` 变成退出码、以及打印。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.pipeline import (  # noqa: E402
    BoardImportError,
    compute_new_dates,
    describe_stations,
    run_daily,
)
from crm_basebot.pipeline.daily import DEFAULT_MAX_DAYS, DailyError  # noqa: E402
from crm_basebot.pipeline.report import print_plan, print_result  # noqa: E402
from crm_basebot.pipeline.source import (  # noqa: E402
    DEFAULT_PATTERN,
    SourceError,
    pick_latest_export,
)
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

# 旧名字：测试按这两个名字调用，正名在 pipeline 里。
latest_export = pick_latest_export

__all__ = [
    "build_parser",
    "compute_new_dates",
    "latest_export",
    "main",
    "run",
]


def _parse_iso_date(text: str, what: str) -> date | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        print(f"{what} 格式要是 YYYY-MM-DD，你给的是 {text!r}", file=sys.stderr)
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把最新一份交易明细里「新加坡站 + 新增交易日」的记录增量导入看板"
    )
    parser.add_argument("--file", help="指定 xlsx，跳过「找最新一份」这一步")
    parser.add_argument(
        "--from-mail",
        action="store_true",
        help="先从邮箱抓最新附件（需要 .env 里的 MICROSOFT_GRAPH_* 四个键）",
    )
    parser.add_argument("--export-dir", help="放导出文件的目录，默认 .env 的 DAILY_EXPORT_DIR")
    parser.add_argument(
        "--pattern", default=DEFAULT_PATTERN, help=f"文件名匹配，默认 {DEFAULT_PATTERN}"
    )
    parser.add_argument(
        "--refresh",
        action="append",
        default=[],
        metavar="YYYY-MM-DD",
        help="强制重导这一天（历史被修订时用），可重复传",
    )
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="只考虑这一天之后的新增日期")
    parser.add_argument(
        "--max-days",
        type=int,
        default=DEFAULT_MAX_DAYS,
        help=f"一次最多接受几个新增交易日，默认 {DEFAULT_MAX_DAYS}",
    )
    parser.add_argument(
        "--allow-many-days",
        action="store_true",
        help="新增日期超过 --max-days 时也照导（第一跑或大范围补数据才用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析和比对，不碰 Base",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_DAILY_BOARD", "TABLE_CLIENT")
    return run(args, settings, BitableClient(settings.base_app_token))


def run(args: argparse.Namespace, settings, bitable: BitableClient) -> int:
    """入口主体。settings / bitable 从外面传，测试里换成假件就能整条路走一遍。"""
    refresh: set[date] = set()
    for raw in args.refresh:
        parsed = _parse_iso_date(raw, "--refresh")
        if parsed is None:
            return 1
        refresh.add(parsed)

    since: date | None = None
    if args.since:
        since = _parse_iso_date(args.since, "--since")
        if since is None:
            return 1

    try:
        result = run_daily(
            settings=settings,
            bitable=bitable,
            file=Path(args.file) if args.file else None,
            from_mail=args.from_mail,
            export_dir=Path(args.export_dir) if args.export_dir else None,
            pattern=args.pattern,
            refresh=refresh,
            since=since,
            max_days=args.max_days,
            allow_many_days=args.allow_many_days,
            dry_run=args.dry_run,
        )
    except BoardImportError as exc:
        print(f"\n读不了这份导出：{exc}", file=sys.stderr)
        return 1
    except (DailyError, SourceError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    print(f"站点：{describe_stations(result.plan.station_counts)}")
    print_plan(result.plan, from_mail=args.from_mail)
    print_result(result)
    if args.dry_run and result.plan.has_work:
        print("--dry-run：只解析和比对，没有写 Base。去掉这个开关才会真导入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
