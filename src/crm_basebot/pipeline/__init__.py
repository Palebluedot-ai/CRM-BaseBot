"""每日数据管线：把内部系统的导出搬进 Base。

## 这个模块负责什么

一条单向的搬运链路，四步各有一个文件：

    source.py   取到那份 xlsx（本地目录里挑最新，或去邮箱下载）
    export.py   解析成 BoardRow（表头契约、UID 防精度损伤、站点筛选）
    delta.py    算出「哪些交易日是看板还没有的」—— 纯函数，零 IO
    board.py    写进 Base（按日先删后写、按 UID 挂客户关联）
    daily.py    把上面四步串起来（依赖可注入，测试不用网络也不用真 Base）
    report.py   给人看的输出

## 这个模块**不**负责什么

- **不算钱。** 每笔佣金由 Base 的公式算（`总收入 × 分佣比例 / 100`），月度汇总在
  `jobs/reconcile`。这里只搬数据。
- **不碰渠道/客户的登记。** 那是机器人的事（`bot/` + `domain/referral.py`）。
- **不决定口径。** 「只导新加坡站」这种业务规则写在 `domain/schema.py`
  （`BOARD_STATION_IN_SCOPE`），这里只执行。

## 为什么要单独一个模块

原来这些逻辑住在一个 500 行的 `scripts/import_daily_board.py` 里，而且增量脚本还要
`import` 那个脚本 —— 依赖方向是反的（脚本互相依赖，逻辑不在包里，测试只能按路径
加载脚本）。现在数据方向是单行道：

    scripts/*.py   ──►   pipeline/*   ──►   domain/ + lark/ + graph/

脚本只剩「解析命令行 + 打印」，逻辑都在这个包里，测试直接 import，不加载脚本。
"""

from __future__ import annotations

from .board import apply_import, client_links, existing_dates
from .daily import DailyPlan, DailyResult, run_daily
from .delta import compute_new_dates, stale_board_dates
from .export import (
    EXPECTED_HEADERS,
    BoardImportError,
    BoardRow,
    describe_stations,
    parse_workbook,
    split_by_station,
)
from .source import pick_latest_export

__all__ = [
    "EXPECTED_HEADERS",
    "BoardImportError",
    "BoardRow",
    "DailyPlan",
    "DailyResult",
    "apply_import",
    "client_links",
    "compute_new_dates",
    "describe_stations",
    "existing_dates",
    "parse_workbook",
    "pick_latest_export",
    "run_daily",
    "split_by_station",
    "stale_board_dates",
]
