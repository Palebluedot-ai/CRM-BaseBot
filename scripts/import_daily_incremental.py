#!/usr/bin/env python
"""每日增量导入：只把「新加坡站 + 新增交易日」的行写进看板。

和 ``import_daily_board.py`` 的分工：

    · import_daily_board.py  = 全量 / 回填。它按导出覆盖到的所有日期整天替换，
      跑一次会把几个月的数据重写一遍（约 1,600 行、25+ 次批量调用）。
    · 这个脚本             = 日常增量。只算「导出里有、看板里没有」的交易日，
      只导那些天。

为什么日常走增量：

1. 免费版多维表格有月度调用额度，每天重写全量是纯浪费；
2. 全量替换有窗口期 —— 删完还没写完的时候表是空的，仪表盘正好读到中间态；
3. 日常场景里导出只会往后加一天，历史行不会变。

**但历史被修订时增量看不见**（导出改了旧日期的金额，看板不会跟进）。所以：

    · ``--refresh 2026-09-16`` 强制把某天再导一遍（可重复传）
    · 大范围回填仍然用 ``import_daily_board.py``（它按日期整天替换，语义更明确）

用法：

    uv run python scripts/import_daily_incremental.py --dry-run           # 看有没有新数据
    uv run python scripts/import_daily_incremental.py                     # 真导入
    uv run python scripts/import_daily_incremental.py --from-mail         # 先从邮箱抓最新附件
    uv run python scripts/import_daily_incremental.py --file 明细.xlsx    # 指定本地文件
    uv run python scripts/import_daily_incremental.py --refresh 2026-09-16
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import import_daily_board as board  # noqa: E402  scripts/ 不是包，按同目录导入

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.graph.mail import (  # noqa: E402
    GraphCredentials,
    GraphMailError,
    fetch_latest_xlsx,
)
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)

# 文件名里带日期（OTC组销售明细_2026-09-18.xlsx），按它挑最新一份。
_DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")

# 导出文件名的默认匹配。导出工具换名字时用 --pattern 覆盖。
DEFAULT_PATTERN = "OTC组销售明细_*.xlsx"

# 一次最多接受几个「新增交易日」。默认 5 天：日常只会加 1 天，而
# 「一次多出 20 天」几乎一定是看板被清空了（第一跑）或者指错了文件 ——
# 那时候应该停下来让人看一眼，而不是闷头把几个月的数据写进去。
DEFAULT_MAX_DAYS = 5


def latest_export(directory: Path, pattern: str = DEFAULT_PATTERN) -> Path | None:
    """目录里最新的一份导出。

    按**文件名里的日期**排，不按 mtime：附件被重新下载、目录被 rsync 之后
    mtime 会变，而文件名里的日期是导出自己的日期。文件名没日期时才退回 mtime。
    """

    def key(path: Path) -> tuple[str, float]:
        found = _DATE_IN_NAME.search(path.name)
        return (found.group(1) if found else "", path.stat().st_mtime)

    candidates = [p for p in directory.glob(pattern) if p.is_file()]
    return max(candidates, key=key) if candidates else None


def compute_new_dates(
    source_dates: set[date],
    existing: set[date],
    *,
    refresh: set[date] | None = None,
    since: date | None = None,
) -> list[date]:
    """导出里有、看板里没有的交易日，加上 ``refresh`` 里被点名的那些。

    ``refresh`` 只对导出里真有数据的日子生效 —— 点名一个导出里没有的日期，
    不该在 Base 上凭空删掉那天的历史行。
    """
    new = {day for day in source_dates if day not in existing}
    if refresh:
        new |= refresh & source_dates
    if since is not None:
        new = {day for day in new if day >= since}
    return sorted(new)


def _parse_iso_date(text: str, what: str) -> date | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        print(f"{what} 格式要是 YYYY-MM-DD，你给的是 {text!r}", file=sys.stderr)
        return None


def _resolve_xlsx(args: argparse.Namespace, settings) -> Path | str:
    """决定这次读哪个文件。返回 Path，或者一句给人看的错误说明（str）。"""
    if args.file:
        path = Path(args.file)
        return path if path.is_file() else f"找不到文件：{path}"

    export_dir = Path(args.export_dir or settings.daily_export_dir or "attachments")
    if not export_dir.is_absolute():
        export_dir = Path(__file__).resolve().parent.parent / export_dir

    if args.from_mail:
        try:
            credentials = GraphCredentials.from_env_values(
                {
                    "MICROSOFT_GRAPH_TENANT_ID": settings.ms_tenant_id,
                    "MICROSOFT_GRAPH_CLIENT_ID": settings.ms_client_id,
                    "MICROSOFT_GRAPH_CLIENT_SECRET": settings.ms_client_secret,
                    "MICROSOFT_GRAPH_USER_ID": settings.ms_user_id,
                    "GRAPH_SENDER": settings.graph_sender,
                }
            )
            result = fetch_latest_xlsx(credentials=credentials, out_dir=export_dir)
        except GraphMailError as exc:
            return f"从邮箱取附件失败：{exc}"
        print(
            f"已从邮箱取到 {result['attachmentName']}（收件时间 {result['receivedDateTimeHKT']}）"
        )
        return Path(str(result["savePath"]))

    found = latest_export(export_dir, args.pattern)
    if found is None:
        return (
            f"{export_dir} 里没有匹配 {args.pattern} 的导出文件。\n"
            "  要么加 --file 指定文件，要么加 --from-mail 去邮箱取，"
            "要么用 --export-dir / DAILY_EXPORT_DIR 指向正确的目录。"
        )
    return found


def _print_plan(
    *,
    xlsx_path: Path,
    kept: list[board.BoardRow],
    existing: set[date],
    new_dates: list[date],
) -> None:
    source_dates = {row.order_date for row in kept}
    rows_by_date: dict[date, int] = defaultdict(int)
    for row in kept:
        rows_by_date[row.order_date] += 1

    print(f"\n导出文件：{xlsx_path}")
    print(
        f"新加坡站 {len(kept)} 行，覆盖 {len(source_dates)} 天"
        f"（{min(source_dates)} 到 {max(source_dates)}）"
    )
    print(
        f"看板已有 {len(existing)} 天"
        + (f"，最新到 {max(existing)}" if existing else "（表是空的）")
    )
    if not new_dates:
        print("\n没有新增交易日 —— 导出里每一天看板都已经有了。Base 不会改动。")
        return
    print(f"\n要导入 {len(new_dates)} 天：")
    for day in new_dates:
        print(f"  {day}  {rows_by_date[day]:>5} 行")


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
    resolved = _resolve_xlsx(args, settings)
    if isinstance(resolved, str):
        print(resolved, file=sys.stderr)
        return 1
    xlsx_path = resolved

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
        rows = board.parse_workbook(xlsx_path)
    except board.BoardImportError as exc:
        print(f"\n读不了这份导出：{exc}", file=sys.stderr)
        return 1

    if not rows:
        print("这份导出一行可用的记录都没有，Base 没动。", file=sys.stderr)
        return 1

    kept, counts = board.split_by_station(rows)
    print(f"站点：{board._describe_stations(counts)}")
    if not kept:
        print(
            f"\n这份导出里一行「{schema.BOARD_STATION_IN_SCOPE}」都没有，Base 没动。\n"
            "看一眼是不是导错了文件，或者站点的写法变了。",
            file=sys.stderr,
        )
        return 1

    tz: tzinfo = ZoneInfo(settings.business_timezone)
    existing = board.existing_dates(bitable, settings.table_daily_board, tz=tz)
    new_dates = compute_new_dates(
        {row.order_date for row in kept}, existing, refresh=refresh, since=since
    )

    _print_plan(xlsx_path=xlsx_path, kept=kept, existing=existing, new_dates=new_dates)

    if not new_dates:
        return 0

    if not refresh and len(new_dates) > args.max_days and not args.allow_many_days:
        print(
            f"\n新增交易日有 {len(new_dates)} 天，超过 --max-days={args.max_days}，"
            "停在这里不动 Base。\n"
            "  这种情况通常是：看板被清空了（第一跑）、或者指错了文件。\n"
            "  确认无误后加 --allow-many-days 重跑；只是想补某几天，用 "
            "import_daily_board.py --date YYYY-MM-DD 更明确。",
            file=sys.stderr,
        )
        return 1

    to_import = [row for row in kept if row.order_date in set(new_dates)]
    to_import.sort(key=lambda row: row.order_date)
    print(f"\n将整天替换这 {len(new_dates)} 天，共写入 {len(to_import)} 行。")

    if args.dry_run:
        print("--dry-run：只解析和比对，没有写 Base。去掉这个开关才会真导入。")
        return 0

    client_links = board.client_links(bitable, settings.table_client)
    deleted, written = board._apply(
        bitable,
        settings.table_daily_board,
        to_import,
        tz=tz,
        replace_dates=new_dates,
        client_links=client_links,
    )
    print(f"删除 {deleted} 条，写入 {written} 条。")
    board._print_link_summary(to_import, client_links)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
