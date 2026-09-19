"""把四步串成「每天跑一次」的那件事。

    source  取到今天的 xlsx（邮箱优先，可退回本地目录）
      ↓
    export  解析 + 只留新加坡站
      ↓
    delta   算出看板还没有的交易日
      ↓
    board   整天替换那几天（按 UID 挂客户关联）

## 依赖是可注入的

``run_daily`` 的两个外部依赖 —— 取数（网络）和写库（Base）—— 都走参数进来，默认是真实
实现。测试传假件就能把整条路走一遍，不用出网、不碰真 Base。这也是「低耦合」的实际含义：
编排层只认识「给我一个文件路径」和「把这批行写进去」两个动作，不认识 HTTP、更不认识表格。

## 拒绝的三种情况（都不动 Base）

- 导出里一行新加坡站都没有：多半导错了文件，或站点的写法变了。
- 新增交易日超过 ``max_days``：一次多出很多天，通常是看板被清空（第一跑）或指错文件。
- 没有新增交易日：这不是错误，是日常 —— 一个写请求都不发。
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from ..domain import schema
from . import board as board_module
from . import export as export_module
from . import source as source_module
from .delta import compute_new_dates, stale_board_dates

logger = logging.getLogger(__name__)

# 一次最多接受几个「新增交易日」。默认 5 天：日常只会加 1 天，而
# 「一次多出 20 天」几乎一定是看板被清空了（第一跑）或者指错了文件 ——
# 那时候应该停下来让人看一眼，而不是闷头把几个月的数据写进去。
DEFAULT_MAX_DAYS = 5


class DailyError(RuntimeError):
    """这次不该导。消息是给人看的，调用方打印它并退非零。"""


@dataclass(frozen=True)
class DailyPlan:
    """这次要做什么（还没做）。"""

    xlsx_path: Path
    source_rows: int
    kept_rows: int
    station_counts: Counter[str]
    existing_days: int
    latest_existing: date | None
    latest_source: date | None
    new_dates: tuple[date, ...]
    rows_by_date: dict[date, int] = field(default_factory=dict)
    # 其中属于「清旧账」的日期：看板里有、导出也覆盖，但导出里要的站点已经没有记录了。
    # 这些天只会删、不会写，见 delta.stale_board_dates。
    stale_dates: tuple[date, ...] = ()

    @property
    def rows_to_import(self) -> int:
        return sum(self.rows_by_date.get(day, 0) for day in self.new_dates)

    @property
    def has_work(self) -> bool:
        return bool(self.new_dates)


@dataclass(frozen=True)
class DailyResult:
    """这次实际做了什么。``plan.has_work`` 为假时 ``deleted`` / ``written`` 都是 0。"""

    plan: DailyPlan
    deleted: int = 0
    written: int = 0
    linked_rows: int = 0
    unregistered_users: int = 0


def build_plan(
    *,
    xlsx_path: Path,
    bitable,
    settings,
    refresh: set[date] | None = None,
    since: date | None = None,
    max_days: int = DEFAULT_MAX_DAYS,
    allow_many_days: bool = False,
) -> DailyPlan:
    """解析 + 筛站点 + 算增量。只读，不写 Base。"""
    rows = export_module.parse_workbook(xlsx_path)
    if not rows:
        raise DailyError("这份导出一行可用的记录都没有，Base 没动。")

    kept, counts = export_module.split_by_station(rows)
    if not kept:
        found = "、".join(f"{station} {n} 行" for station, n in counts.most_common())
        raise DailyError(
            f"这份导出里一行「{schema.BOARD_STATION_IN_SCOPE}」都没有，Base 没动。\n"
            f"  导出里只有 {found}。\n"
            "  看一眼是不是导错了文件，或者站点的写法变了。"
        )

    tz: tzinfo = ZoneInfo(settings.business_timezone)
    existing = board_module.existing_dates(bitable, settings.table_daily_board, tz=tz)
    all_source_dates = {row.order_date for row in rows}
    kept_dates = {row.order_date for row in kept}

    # 「新增」只看**要的站点**：导出覆盖 9 个月，而香港站几乎每天都交易 —— 拿所有站点
    # 当基准，会把一堆「只有香港站交易」的日子算成新增，天天触发 --max-days 守卫。
    new_dates = compute_new_dates(kept_dates, existing, refresh=refresh, since=since)

    # 「清账」反过来必须知道所有站点的覆盖范围：这天看板里有、导出也覆盖，但导出里要的
    # 站点已经没有记录了 —— 它不是「新增」，普通增量永远碰不到它（见 stale_board_dates）。
    stale = stale_board_dates(existing, all_source_dates, kept_dates)
    if since is not None:
        stale = {day for day in stale if day >= since}
    replace_dates = sorted(set(new_dates) | stale)

    rows_by_date: dict[date, int] = defaultdict(int)
    for row in kept:
        rows_by_date[row.order_date] += 1

    plan = DailyPlan(
        xlsx_path=xlsx_path,
        source_rows=len(rows),
        kept_rows=len(kept),
        station_counts=counts,
        existing_days=len(existing),
        latest_existing=max(existing) if existing else None,
        latest_source=max(all_source_dates) if all_source_dates else None,
        new_dates=tuple(replace_dates),
        rows_by_date=dict(rows_by_date),
        stale_dates=tuple(sorted(stale)),
    )

    if not plan.has_work:
        return plan

    # 守卫只看「新增」的天数：它拦的是「看板被清空了」或「指错了文件」（一次冒出几十天）。
    # 清账是自动发现的、规模天然受看板本身限制，不参与这个闸门。
    if not refresh and len(new_dates) > max_days and not allow_many_days:
        raise DailyError(
            f"新增交易日有 {len(new_dates)} 天，超过 --max-days={max_days}，"
            "停在这里不动 Base。\n"
            "  这种情况通常是：看板被清空了（第一跑）、或者指错了文件。\n"
            "  确认无误后加 --allow-many-days 重跑；只是想补某几天，用 "
            "import_daily_board.py --date YYYY-MM-DD 更明确。"
        )

    return plan


def apply_plan(*, plan: DailyPlan, bitable, settings) -> DailyResult:
    """把计划写进 Base。没有新增交易日时一个写请求都不发。"""
    if not plan.has_work:
        return DailyResult(plan=plan)

    tz: tzinfo = ZoneInfo(settings.business_timezone)
    links = board_module.client_links(bitable, settings.table_client)

    new = set(plan.new_dates)
    to_import = [
        row
        for row in export_module.parse_workbook(plan.xlsx_path)
        if row.order_date in new
        and row.fields.get(schema.BOARD_STATION) == schema.BOARD_STATION_IN_SCOPE
    ]
    to_import.sort(key=lambda row: row.order_date)

    deleted, written = board_module.apply_import(
        bitable,
        settings.table_daily_board,
        to_import,
        tz=tz,
        replace_dates=plan.new_dates,
        client_links_map=links,
    )

    linked_rows = sum(
        1 for row in to_import if str(row.fields.get(schema.BOARD_CLIENT_UID) or "") in links
    )
    users = {str(row.fields.get(schema.BOARD_CLIENT_UID) or "") for row in to_import}
    users.discard("")
    return DailyResult(
        plan=plan,
        deleted=deleted,
        written=written,
        linked_rows=linked_rows,
        unregistered_users=len(users) - len(users & set(links)),
    )


def resolve_export(
    *,
    settings,
    file: Path | None = None,
    from_mail: bool = False,
    export_dir: Path | None = None,
    pattern: str = source_module.DEFAULT_PATTERN,
    fetch: Callable[..., Path] = source_module.fetch_from_mail,
) -> Path:
    """决定这次读哪个文件。取不到就抛 DailyError（消息里带着下一步怎么做）。"""
    if file is not None:
        if not file.is_file():
            raise DailyError(f"找不到文件：{file}")
        return file

    directory = export_dir or Path(settings.daily_export_dir or "attachments")
    if not directory.is_absolute():
        directory = Path(__file__).resolve().parent.parent.parent.parent / directory

    if from_mail:
        return fetch(settings, directory)

    found = source_module.pick_latest_export(directory, pattern)
    if found is None:
        raise DailyError(
            f"{directory} 里没有匹配 {pattern} 的导出文件。\n"
            "  要么加 --file 指定文件，要么加 --from-mail 去邮箱取，"
            "要么用 --export-dir / DAILY_EXPORT_DIR 指向正确的目录。"
        )
    return found


def run_daily(
    *,
    settings,
    bitable,
    file: Path | None = None,
    from_mail: bool = False,
    export_dir: Path | None = None,
    pattern: str = source_module.DEFAULT_PATTERN,
    refresh: set[date] | None = None,
    since: date | None = None,
    max_days: int = DEFAULT_MAX_DAYS,
    allow_many_days: bool = False,
    dry_run: bool = False,
    fetch: Callable[..., Path] = source_module.fetch_from_mail,
) -> DailyResult:
    """每天那件事的完整入口：取数 → 解析 → 算增量 →（除非 dry_run）写 Base。"""
    xlsx_path = resolve_export(
        settings=settings,
        file=file,
        from_mail=from_mail,
        export_dir=export_dir,
        pattern=pattern,
        fetch=fetch,
    )
    plan = build_plan(
        xlsx_path=xlsx_path,
        bitable=bitable,
        settings=settings,
        refresh=refresh,
        since=since,
        max_days=max_days,
        allow_many_days=allow_many_days,
    )
    if dry_run or not plan.has_work:
        return DailyResult(plan=plan)
    return apply_plan(plan=plan, bitable=bitable, settings=settings)
