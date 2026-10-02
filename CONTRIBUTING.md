# 接手与改代码

给之后维护这个项目的人。先读这一页，再按需要翻 `docs/`。给不写代码的人看的版本是
[docs/MAINTAINER_GUIDE.md](docs/MAINTAINER_GUIDE.md)（也导进了 Lark 文档）。

## 这个项目是什么

HashKey OTC 的渠道佣金系统。三样东西：

1. **Lark Base（多维表格）**：所有资料都在这里 —— 渠道、客户、每日交易、结算、名册、审计。
2. **Lark 机器人「CRM-BaseBot」**：销售在里面登记渠道和客户、查佣金、生成协议和 Invoice。
3. **定时任务**：每天把交易导进 Base、更新结算表里当月（进行中）的数；每月 1 号 16:30 把上个月结掉（已结算）、存档、发月结卡片给管理员。

机器人和定时任务都跑在**公司的 mac mini** 上（launchd 管着，崩了自动重启）。

## 代码在哪

```
src/crm_basebot/
  app.py            机器人入口：组装所有服务，连 Lark 长连接
  config.py         .env 里的设定（Settings）
  structure.py      Base 的表和列长什么样、怎么对齐（sync_base 用）
  bot/              机器人：auth（名册、谁看得到什么）、cards（每张卡片）、handlers（每个按钮做什么）
  domain/           业务规则：schema（所有表名列名）、commission（佣金怎么算）、ai_status（AI 规则）、
                    referral / referred_client / payment（登记和收款资料）、ecas*（ECAS 那一套）
  documents/        转介协议、Invoice（Word 模板 + 转 PDF）
  pipeline/         每天导入：取邮件附件 → 解析 → 算增量 → 写看板 → 补挂客户
  jobs/             每月：reconcile（交易结算）、ecas_reconcile（ECAS 结算）、archive（存档）
  lark/             Lark 接口的薄封装：bitable（读写表）、files（发文件）、values（值怎么取）
  graph/            从 Outlook 邮箱取每日导出（Microsoft Graph）
scripts/            一次性或运维用的命令，每个文件开头都写了用法和为什么
tests/              pytest，全部离线（假 Base、假 Lark），不连网
docs/               每一块的设计理由；改之前先读对应的那篇
```

**先读的文件**：`domain/schema.py`（所有名字）→ `domain/commission.py`（钱怎么算）→
`bot/handlers.py`（机器人每个按钮）。

## 本地跑起来

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```
uv sync
uv run pytest -q          # 全部测试，约 25 秒，1400+ 个
uv run ruff check .       # lint
uv run ruff format .      # 格式
```

连真的 Base 要 `.env`（照 `.env.example` 填）。**`.env` 绝不进仓库**，里面有 App Secret。

## 改代码的规矩

- **改完跑一遍全部测试和 ruff，全过才提交。** 新行为要有测试；测试名直接写中文描述行为。
- **注释写「为什么」，不写「做了什么」。** 很多看起来多余的写法都是真机上踩过坑，注释里写了
  日期和原因。删之前先看注释。
- **所有表名、列名只在 `domain/schema.py`（ECAS 在 `domain/ecas.py`）定义**，别处引用常量，
  不要写字面量。Base 里改列名 = 改这里 + 跑 `sync_base.py`。
- **钱永远用 `Decimal`**，写进 Base 才转 float。分摊用最大余数法（`documents/invoice.allocate`），
  保证加起来一分不差。
- **客户 UID 永远当文本**（19 位，存成数字会丢精度）。取值用 `lark/values.to_uid`。
- **结算过的月份不重写。** 结算表每一行有「状态」：进行中（每天覆盖）/ 已结算（空着也算已结算）。已结算的行、存档写一次就是那个月的样子；要重算得显式 `--replace`。读结算表算钱的地方只认已结算（`domain/settlement.is_live`）。
- **脚本默认预演，`--apply` 才写。** 新脚本照这个做。
- **客户资料、xlsx、`output/` 不进仓库**（`.gitignore` 已挡）。收款账号、证件号不写日志、不进审计表。

## 改 Base 的结构

在 `schema.py` 加列 → 在 MacBook 跑：

```
uv run python scripts/sync_base.py            # 预演
uv run python scripts/sync_base.py --apply
```

它只加不删；会改的只有我们维护的公式列和数字显示格式。跑完会自检公式和 AI 规则。

## 发布（怎么让改动上线）

代码有两个 GitHub 仓库：

| 仓库 | 用途 |
| --- | --- |
| `okyterrance/CRM-BaseBot` 的 `claude/pensive-wright-k6h2km` 分支 | 开发暂存 |
| **`Palebluedot-ai/CRM-BaseBot` 的 `main`** | **正式版**，mac mini 从这里拉 |

上线三步：

1. **MacBook**（crm-basebot 资料夹）把开发分支合进正式 main：
   ```
   git checkout main && git pull https://github.com/okyterrance/CRM-BaseBot.git claude/pensive-wright-k6h2km && git push origin main
   ```
2. 改了 Base 结构的话，再跑 `uv run python scripts/sync_base.py --apply`。
3. **mac mini**（crm-basebot 资料夹）拉代码、重启机器人：
   ```
   git pull origin main && launchctl kickstart -k gui/$(id -u)/com.chao.crm-basebot.bot
   ```
   改了依赖（`pyproject.toml`）的话中间加一步 `uv sync`。定时任务每次都是新起的进程，不用重启。

## 定时任务（mac mini 上的 launchd）

| 任务 | 时间 | 做什么 | 日志 |
| --- | --- | --- | --- |
| `com.chao.crm-basebot.bot` | 常驻 | 机器人 | `logs/bot.log` |
| `com.chao.crm-basebot.daily-import` | 每天 10:45、16:00 | 取邮件导出 → 写看板 → 补挂客户 → 刷新结算表和存档里还没结算的月份 | `logs/daily-import-*.log` |
| `com.chao.crm-basebot.monthly-reconcile` | 每月 1 号 16:30 | 结算上个月（交易 + ECAS，进行中 → 已结算）→ 存档 → 发卡片给管理员 | `logs/monthly-reconcile-*.log` |

安装脚本在 `scripts/install-*-launchd.sh`。

## 出问题先看哪

| 现象 | 看这里 |
| --- | --- |
| 机器人没反应 | mac mini `logs/bot.log`；`scripts/bot_connectivity_report.py` 看断线空窗 |
| 某个客户的佣金对不上 | `scripts/diagnose_client.py --name "..."` |
| 结算和现在重算差多少、为什么 | `scripts/compare_settled.py` |
| 和财务的月度表对账 | `scripts/compare_finance.py --period YYYY-MM --file 财务表.xlsx` |
| 交易在不在每日导出里、哪个站点 | `scripts/find_in_exports.py --uid ...`（在 mac mini 跑） |
| 客户登记了但看板没挂上 | `scripts/relink_board.py --apply` |

## 各篇文档

- `docs/BOT.md` 机器人每个按钮、权限、卡片设计
- `docs/SCHEMA.md` 每张表每一列
- `docs/PIPELINE.md` 每日导入
- `docs/ECAS.md` ECAS 返佣
- `docs/DASHBOARD.md` 仪表盘、存档
- `docs/LARK_APP_SETUP.md` Lark 应用的权限和事件
- `docs/SALES_GUIDE.md` 给销售看的使用说明
- `HANDOFF.md` 整套搬到新租户的步骤
