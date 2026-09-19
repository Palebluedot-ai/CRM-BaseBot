# 接手手册 · 给同事 / 给同事的 AI agent

> **给 AI agent 的一句话**：这个仓库在**原主人的飞书账号**里已经跑通了一套「渠道佣金 CRM」
> （每天从邮箱取销售明细 → 只导新加坡站的新增交易日 → 看板按 UID 自动算每笔佣金 → 机器人
> 让销售自己登记渠道/客户/查佣金）。你的任务：**把这套东西整份搬到委托人自己的飞书账号里**，
> 搬完让他这边的同事能用自己的账号继续用。你不需要重新理解业务，按下面顺序执行即可；
> 需要人在浏览器里点的三下（建应用、开权限、发版）在第 1 步，其余你都能自己做。

---

## 0. 你要交付的结果

- 委托人账号里有一个 Base，**6 张表结构完全一致**（含看板上的公式列），并且**原有数据一条不少**：
  看板约 1,650 行、渠道约 101 条、客户约 113 条、销售名册 4 人。
- 搬完能跑通自检：`scripts/verify_commission.py` 报「逐行一致」。
- （之后才需要）机器人上线：他那边的销售给机器人发消息、登记渠道/客户、查佣金。

## 1. 分工

| 谁 | 做什么 |
|---|---|
| **你（agent）** | 逐步执行本文所有命令；把报错翻译成人话给他；不要自己改代码 |
| **委托人（人类）** | 只在浏览器里做三件事（下面 1.1），然后**把 App ID / Secret 填进 `.env.target`**（不要贴进聊天） |
| **原主人** | 他手上有源端凭证。要么他把数据推过来（他把你的 `.env.target` 拿到后跑一条命令），要么他把源端 `.env` 给你、你一次跑完 —— 见第 3 步 |

## 1.1 建应用（人类在浏览器点，你给他链接和清单）

1. 打开 <https://open.feishu.cn/app> → **创建企业自建应用** → 名字建议 `CRM-BaseBot`
2. 建好后进应用 → 左侧 **「凭证与基础信息」** → 复制 `App ID`、`App Secret`
3. 左侧 **「权限管理」** → 搜「多维表格」→ 开通 **`bitable:app`**（读写）
4. 左侧 **「版本管理与发布」** → **创建版本 → 申请发布**（企业自建应用通常需管理员点一下批准）

> ⚠️ **第 3、4 步漏任何一个，后面所有接口都会 403**，而报错完全看不出是这个原因。
> 这是整条路上最容易卡死的地方。让委托人确认「版本状态 = 已发布」。

## 2. 准备仓库与配置（你来做）

```bash
git clone <原主人给你的仓库地址> CRM-BaseBot && cd CRM-BaseBot
uv sync                                  # 没装 uv：curl -LsSf https://astral.sh/uv/install.sh | sh
cp .env.target.example .env.target       # 模板里每一项都写了去哪拿
```

让委托人把 `App ID` / `App Secret` 填进 `.env.target` 的第 21、22 行（两个 `=` 后面直接粘，
不要引号/空格）。**`LARK_BASE_APP_TOKEN` 留空** —— 下面用 `--create-base` 让应用自己建 Base，
这样它天然是所有者，**不需要**「把应用加进 Base 协作者」那一步。

自检配置有没有填对（这一步不联网，只读文件）：

```bash
uv run python scripts/migrate_base.py --target-env .env.target --dry-run
```

- 打印「迁移没法继续：…还没填目标账号的应用凭证」→ 回去让委托人填第 21/22 行。
- 打印计划（`新建 Base「…」（预演，没有真的建）`）→ 配置对了，进第 3 步。

## 3. 搬迁（一条命令）

**情况 A：原主人把源端 `.env` 也给了你**（最顺）

```bash
# 把源端 .env 放到仓库根目录，命名为 .env.source
uv run python scripts/migrate_base.py --target-env .env.target \
    --create-base "CRM 佣金看板" --dry-run     # 先看要建什么、搬哪些表
uv run python scripts/migrate_base.py --target-env .env.target \
    --create-base "CRM 佣金看板" --apply       # 真搬
```

**情况 B：源端凭证没给你**（只给了你 `.env.target` 的填法）

把 `.env.target` 原样发回给原主人，让他在**他的**机器上跑同样这条命令（源端 `.env` 在他那儿）。
搬完他会把结果告诉你 —— 那时你的 Base 里应该已经有 6 张表和全部数据，继续第 4 步。

这条命令自己会做：建 6 张表 + 看板的公式列 → 搬渠道 → 搬客户（按「渠道编号」重建关联）→
搬看板（按「客户UID」重建关联）→ 搬名册 → **逐表比对两边行数**。看到每一行都是
`✅ 渠道 Referral Information：源 N 条 → 目标 N 条` 才算成功。

**情况 C：什么凭证都不交换，只交接数据文件**

原主人会导出两个 xlsx 给你（渠道+客户、看板）。你这边：

```bash
uv run python scripts/sync_base.py --apply         # 先按 schema 把 6 张表建出来
uv run python scripts/import_registrations.py --file <渠道客户.xlsx> --dry-run
uv run python scripts/import_registrations.py --file <渠道客户.xlsx> --apply
uv run python scripts/import_daily_board.py --file <看板.xlsx> --apply
```

注意：这份文件**只导一次**（没有 UID 的客户重复导入可能会堆重复行），而且原主人那边得先有
一个 Base（他导出用的就是他现在的），确保他导出的是最新数据。

## 4. 搬完立刻自检（你来做）

```bash
cp .env.target .env        # 之后所有脚本都指向委托人的 Base 了（.env 在 .gitignore 里）
uv run python scripts/inspect_base.py          # 核对 6 张表、行数、字段
uv run python scripts/verify_commission.py     # 期望：结论「逐行一致，没有差异」
```

`verify_commission.py` 不看 Base 的公式列，自己按 `用户ID → 客户表 → 所属渠道 → 分佣比例`
复算一遍再逐行对比。它报「逐行一致」就说明**关联没搬错、公式在算、钱算得对**。

## 5. 还需要人做的两件（机器代劳不了）

**为什么**：`open_id`（飞书里"这个人是谁"的 ID）是**按应用签发**的 —— 原账号的 open_id
在委托人的账号里是无效值，所以名册和归属都没有搬过去。搬完机器人里「我的渠道」是空的，
**这不是数据丢失**，是归属还没认领。判据在 `docs/BOT.md`。

```bash
# ① 让委托人这边要用的每位销售，各给机器人发一条消息；服务端日志会出现：
#    WARNING crm_basebot.bot.auth: 未登记的 open_id 尝试操作: ou_xxxx
#    把这个 open_id 填进名册（--env .env.target 或已 cp 成 .env 就省略）：
uv run python scripts/set_sales_open_id.py --name "某人的姓名" --open-id ou_xxxx --apply
uv run python scripts/set_sales_open_id.py --list          # 核对：OpenID 那列有值了

# ② 名册填好之后，按「负责销售」姓名把渠道/客户的归属接上：
uv run python scripts/backfill_owners.py --dry-run         # 先看会回填多少条
uv run python scripts/backfill_owners.py --apply
```

② 跑完，销售在机器人里就能看到「我的渠道」、能登记客户了。

## 6. 让机器人上线（可选，但这就是这套东西的价值）

```bash
uv run python -m crm_basebot.app        # 机器人（长连接，不需要公网地址）
```

应用侧要开通的能力、事件订阅、卡片回调，照 `docs/LARK_APP_SETUP.md` 清单配（和权限一样，
**改完要重新发版**）。

## 7. 每天自动取数（可选）

销售明细 xlsx 是内部系统每天发到邮箱的，让每天的导入自动跑需要 4 个 Microsoft Graph 键
（Entra 应用 + `Mail.Read` 应用权限），步骤见 `docs/PIPELINE.md`。填好 `.env` 里那 4 行后：

```bash
./scripts/run-daily-import.sh --dry-run                  # 先验：应该能取到今天那份附件
./scripts/install-daily-import-launchd.sh                # 挂成每天 10:45 / 16:00
```

## 8. 出问题对照表

| 现象 | 多半是 | 怎么办 |
|---|---|---|
| 任何接口 403 / `permission denied` | 权限没开、没发版，或（走法 B 自己建的 Base）应用没被加成协作者 | 回 1.1 的第 3、4 步；走法 B 另加：Base → 分享 → 把应用加成「可编辑」 |
| `没有权限访问该多维表格` | `LARK_BASE_APP_TOKEN` 填的是**另一个**账号的 Base | 核对 Base URL 里的 token 是不是委托人的 |
| 看板上有数据但佣金列全空 | 公式列没建出来，或客户/渠道的关联没挂上 | `uv run python scripts/sync_base.py --apply`，它会做公式自检并说明原因 |
| 行数对不上（源 N → 目标 M<N） | 某几条记录里有目标端不存在的字段/非法值，被接口整批拒了 | 看迁移报告里带 ⚠️ 的那行；把该表的 `--dry-run` 输出发给原主人 |
| 机器人对谁都回「你还没有被登记为销售」 | 名册 OpenID 还空着 | 第 5 步 ① |
| 机器人里「我的渠道」是空的 | 归属还没回填 | 第 5 步 ② |
| 断线 / 收不到消息 | 网络或 DNS（长连接不补发断线期间的消息） | 看 `docs/BOT.md` 里「断线时间线」那一节的判读方式 |

## 9. 这套东西的设计要点（免得你误改）

- **钱只在 Base 里算**：看板的 `分佣比例` / `本笔佣金` / `月份` 是公式列，Python 不参与算钱。
  `verify_commission.py` 只是**复算校验**，不是数据来源。
- **只导新加坡站**：站点是「新加坡站」的行才进看板，其它站点解析完就丢（既定口径）。
- **关联一律按业务键**：客户 ↔ 渠道用「渠道编号」，看板 → 客户用「客户UID」。
  **不要**按姓名匹配（大小写/last-first 颠倒会错，原主人踩过）。
- **每天的导入是增量**：只写看板还没有的交易日，整天替换；`--max-days`（默认 5）是防呆闸门。
- 更细的背景：`README.md`、`docs/PIPELINE.md`（每日管线）、`docs/BOT.md`（机器人现状与缺口）、
  `docs/MIGRATION.md`（迁移细节与两种走法）、`docs/SCHEMA.md`（表结构）、`docs/DASHBOARD.md`（仪表盘）。

## 10. 别做的事

- 不要 `git add .env` / `.env.target`（已在 `.gitignore`，但仍要留神）——里面有真密钥。
- 不要把客户数据（xlsx、导出的 JSON）提交进仓库。
- 不要手工改 Base 的字段类型来"修"问题：列类型改了可能毁数据，先跑 `sync_base.py` 看它怎么说。
- 不要同时跑两个机器人实例（同一个应用只能有一条长连接，会互相踢下线）。
