"""把整套 Base 从「一个飞书账号」搬到「另一个飞书账号」。

## 一条命令

    uv run python scripts/migrate_base.py --target-env .env.target --dry-run   # 预演
    uv run python scripts/migrate_base.py --target-env .env.target --apply     # 真搬

它会按顺序做完这些事，中间不需要你手工点 Base：

    1. 目标端建结构   —— 复用 crm_basebot.structure（和 sync_base.py 同一份逻辑），
                         含看板那几列的公式
    2. 搬渠道        —— Referral Information
    3. 搬客户        —— Referred Client，所属渠道按「渠道编号」重建关联
    4. 搬看板        —— Daily Revenue Board，客户按「客户UID」重建关联
    5. 搬销售名册    —— Sales Directory，只搬姓名/角色/状态
    6. 自检 + 报告   —— 逐表比对两边行数，列出「还需人工」的那几件事

## 两边不一样的地方（这是要点）

| | 源（你的账号） | 目标（他的账号） |
|---|---|---|
| 应用凭证 | .env 里的 | .env.target 里的 |
| Base | 你的 | 他的 |
| 表结构 | **一模一样** | 同一个 schema 建出来的，所以字段名就是映射 |
| open_id | 你的应用签发 | **完全不同**：人员列不搬，迁移完在目标端重新认领 |

**表头和内容一样**，所以搬运不需要任何映射表：字段按名字对，关联按业务键
（渠道编号 / 客户UID）重建。

## 搬完还需要人做的两件

1. **目标端的 OpenID**：open_id 按应用签发，源端的值到了目标端是无效的。让目标账号的
   几位销售各给机器人发一条消息，用 ``set_sales_open_id.py --env .env.target`` 填名册。
2. **归属回填**：名册填好之后跑 ``backfill_owners.py --env .env.target``，渠道/客户的
   归属就按姓名接上了。在那之前，机器人里「我的渠道」是空的。

报告的最后会打印这两步的具体命令。
"""

from __future__ import annotations

from .runner import MigrationResult, TableCopyResult, load_target_settings, run_migration

__all__ = [
    "MigrationResult",
    "TableCopyResult",
    "load_target_settings",
    "run_migration",
]
