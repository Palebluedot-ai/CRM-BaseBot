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

> 人和浏览器友好版：**`HANDOFF.html`**（仓库根目录，`open HANDOFF.html` 打开）。内容与本文
> 一致，含「环境与账号要求」「交付验收清单」两节表格，适合直接给委托人看。

## 1. 分工

| 谁 | 做什么 |
|---|---|
| **你（agent）** | 逐步执行本文所有命令；把报错翻译成人话给他；不要自己改代码 |
| **委托人（人类）** | 只在浏览器里做三件事（下面 1.1），然后**把 App ID / Secret 填进 `.env.target`**（不要贴进聊天） |
| **原主人** | 他手上有源端凭证。要么他把数据推过来（他把你的 `.env.target` 拿到后跑一条命令），要么他把源端 `.env` 给你、你一次跑完 —— 见第 3 步 |

### 1.1 两套实例的边界（各建各的 Bot，互不影响）

**要建的是两个完全独立的 Bot**：各自的飞书账号、各自的应用、各自的 Base。功能一模一样，
背后的 Base 逻辑和初始数据也一模一样 —— 但运行起来之后是两套东西。

| 项目 | 两套之间 |
|---|---|
| 代码 | **同一份**（就是这个仓库，各自 clone） |
| Base 结构与公式逻辑 | **完全一样**（同一个 schema 建出来的，含看板那几列的公式） |
| Base 数据 | 迁移时**复制**过去；之后**各自独立演化** —— 你这边登记的客户不会出现在他那边 |
| 飞书应用（App ID / Secret） | **各自的**（一个应用只能属于一个账号，不能共用） |
| 机器人进程 | **各自跑各自的**。同一个应用只能有一条长连接，所以**千万别两个进程用同一套凭证**（会互相踢下线） |
| `open_id` | **各自的**（按应用签发，互不通用）→ 名册必须各填各的，见 [docs/OPENID_ONBOARDING.md](docs/OPENID_ONBOARDING.md) |
| 取数邮箱 | 可能是**同一个**（内部系统那封日报邮件）。按对方的权限决定：能拿到 Graph 凭证就自己取，否则原主人导出发文件 |

> 一句话：**共享的是代码和一开始的数据，运行起来之后没有任何东西是共享的。**

## 1.0 要求（Requirements）

**机器**（跑命令的那台）

| 要求 | 说明 |
|---|---|
| macOS / Linux（Windows 也能跑） | 全是纯 Python。只有「定时任务」脚本是 macOS 的 launchd（脚本自带），Linux 用 cron、Windows 用任务计划程序替代 |
| `uv` | **唯一需要你装的工具**：`curl -LsSf https://astral.sh/uv/install.sh | sh`。它会把 Python 也装好 —— 项目要 ≥3.12（实测用 3.13），**不需要你先装 Python** |
| `git` | clone 仓库 |
| 网络 | 必须能到 `open.feishu.cn`（国际版是 `open.larksuite.com`）和 `github.com`；第 7 步的邮件取数另需 `graph.microsoft.com` |

**飞书账号**（这一步最容易踩坑）

- **必须是「企业/团队」账号，个人版不行**：个人版没有管理后台，而「开权限」「发版」都要管理员在后台点通过 —— 个人版里应用永远发不出去（拿到 App ID 也没用，权限和机器人能力都不生效）。自建免费企业即可，不需要营业执照。+86 手机号建飞书企业；只有港号走国际版 Lark。
- 权限清单（**一次开齐**，免得反复发版）：`bitable:app`（必须）· `im:message.p2p_msg:readonly` + `im:message:send_as_bot`（机器人用）· `contact:user.base:readonly`（可选，当前代码没调用）。
  **注意**：接收消息用的是 `im:message.p2p_msg:readonly`，**不是** `im:message`（官方事件文档里才写得对，见 `docs/LARK_APP_SETUP.md` 第 4 步）。
- 事件订阅：`im.message.receive_v1` + 卡片回调 `card.action.trigger`，用**长连接**（不需要公网地址）。
- Base 不用手工建：第 3 步的方案 A 会让应用自己建一个（顺带省掉「把应用加成协作者」这个 403 坑）。

**可选**（只有「每天自动取数」需要）：Microsoft Entra 应用 + `Mail.Read` 应用权限并限定到一个邮箱。没有也能用：手工把 xlsx 放进 `attachments/` 再跑导入。

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

**情况 C：什么凭证都不交换，只交接文件**

原主人会给你一个 zip（里面是 `渠道客户.xlsx` + `看板.xlsx` + `导入说明.txt` + `HANDOFF.html`）
—— **是 xlsx，不是 CSV**：18–19 位的客户 UID 用 CSV 转一手会被 Excel 抹掉末尾几位，那种 UID
之后永远算不出佣金，而且不报错。

你这边（Base 用你自己界面建的，把 URL 里的 token 填进 `LARK_BASE_APP_TOKEN`）：

```bash
cp .env.target.example .env        # 填你的 App ID / Secret，再填 Base token
# 把包里的两个 xlsx 放到仓库根目录，然后一条命令导完：
uv run python scripts/import_handover.py --dir . --dry-run   # 先预演（不写库）
uv run python scripts/import_handover.py --dir . --apply     # 真导
```

这一条命令会按顺序做：建 6 张表（含看板公式列）+ 把 6 个 table_id **自动写进 `.env`** →
导渠道/客户（按「渠道编号」重建关联）→ 导看板（按「客户UID」重建关联）→ 名册按姓名补齐。
`table_id` 不用手抄（早期版本要人手抄，抄错一位会得到 404 而看不出因果）。

注意：这份数据**只导一次**（没有 UID 的客户重复导会堆重复行），而且原主人那边得先有
一个 Base（他就是从那儿导出的），确保导出的是最新数据。

## 4. 搬完立刻自检（你来做）

```bash
cp .env.target .env        # 之后所有脚本都指向委托人的 Base 了（.env 在 .gitignore 里）
uv run python scripts/inspect_base.py          # 核对 6 张表、行数、字段
uv run python scripts/verify_commission.py     # 期望：结论「逐行一致，没有差异」
```

`verify_commission.py` 不看 Base 的公式列，自己按 `用户ID → 客户表 → 所属渠道 → 分佣比例`
复算一遍再逐行对比。它报「逐行一致」就说明**关联没搬错、公式在算、钱算得对**。

## 5. 还需要人做的两件（机器代劳不了）

> 完整规程（每位销售都要走一遍，换 Bot 也要重走）：**[docs/OPENID_ONBOARDING.md](docs/OPENID_ONBOARDING.md)**
> —— 含多人批量登记、验证清单、以及「不要手工编 `ou_`／不要拿别人账号代发」这类坑。
>
> **前置：新的机器人得先跑起来**（见第 6 步）。
> `open_id` 是**应用**签发的 —— 委托人这边是一个**全新的应用、全新的 bot**，在它上线之前，
> 销售发的消息没有任何东西接收，也就拿不到任何 open_id。原主人那台机器上的机器人服务的是
> 原账号，与这里无关。
> 所以顺序是：**建应用 / 开权限 / 发版**（第 1.1 步）→ **把机器人跑起来**（第 6 步）→
> 收 open_id（本节）→ 回填归属（本节）。

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

另外两个常驻任务，装法一样（都会先预演、都能 `launchctl bootout` 回滚）：

```bash
./scripts/install-bot-launchd.sh                 # 机器人：开机自启 + 崩溃自动拉起
./scripts/run-monthly-reconcile.sh --dry-run     # 先验月结：应该算出上个月的金额
./scripts/install-monthly-reconcile-launchd.sh   # 每月 3 号 10:00 结算上月并私信管理员
```

⚠️ 机器人同时只能有一个进程 —— 两个进程拿同一对 App ID/Secret 连上去，飞书按集群处理，
每条事件只投给其中一个，表现是「时灵时不灵」且日志里没有任何错误。装之前先确认没有
手工起的进程（安装脚本会自己查一遍，有就拒绝装）。

⚠️ 这三个都是**用户级**（`gui/$(id -u)`）任务：那个账号登出、或机器重启后停在登录界面，
它们都不会自己起来。要真 7×24 得开自动登录或改成系统级 LaunchDaemon。

## 8. 出问题对照表

| 现象 | 多半是 | 怎么办 |
|---|---|---|
| 任何接口 403 / `permission denied` | 权限没开、没发版，或（走法 B 自己建的 Base）应用没被加成协作者 | 回 1.1 的第 3、4 步；走法 B 另加：Base → 分享 → 把应用加成「可编辑」 |
| `没有权限访问该多维表格` | `LARK_BASE_APP_TOKEN` 填的是**另一个**账号的 Base | 核对 Base URL 里的 token 是不是委托人的 |
| 看板上有数据但佣金列全空 | 公式列没建出来，或客户/渠道的关联没挂上 | `uv run python scripts/sync_base.py --apply`，它会做公式自检并说明原因 |
| 行数对不上（源 N → 目标 M<N） | 某几条记录里有目标端不存在的字段/非法值，被接口整批拒了 | 看迁移报告里带 ⚠️ 的那行；把该表的 `--dry-run` 输出发给原主人 |
| 机器人对谁都回「你还没有被登记为销售」 | 名册 OpenID 还空着 | 第 5 步 ① |
| 机器人里「我的渠道」是空的 | 归属还没回填 | 第 5 步 ② |
| 改了某条渠道的「负责销售」，机器人里还是原来那个人看得到 | `backfill_owners.py` 只动「归属为空」的行 | 先把那行的 `归属销售` 和 `登记人OpenID` 清空，再跑 `--apply` |
| 重启机器后机器人没起来 | 用户级 launchd 任务要等那个账号登录 | `launchctl print gui/$(id -u)/com.chao.crm-basebot.bot \| grep state` |
| 断线 / 收不到消息 | 网络或 DNS（长连接不补发断线期间的消息） | 看 `docs/BOT.md` 里「断线时间线」那一节的判读方式 |

## 9. 这套东西的设计要点（免得你误改）

- **钱只在 Base 里算**：看板的 `分佣比例` / `本笔佣金` / `月份` 是公式列，Python 不参与算钱。
  `verify_commission.py` 只是**复算校验**，不是数据来源。
- **只导新加坡站**：站点是「新加坡站」的行才进看板，其它站点解析完就丢（既定口径）。
- **关联一律按业务键**：客户 ↔ 渠道用「渠道编号」，看板 → 客户用「客户UID」。
  **不要**按姓名匹配（大小写/last-first 颠倒会错，原主人踩过）。
- **每天的导入是增量**：只写看板还没有的交易日，整天替换；`--max-days`（默认 5）是防呆闸门。
- **ECAS 是另一套账**：`ECAS Applications` / `ECAS Commission Summary` 和上面这条链路
  没有任何数据往来。同一个渠道两边的比例可以不一样，同一个客户两边各付一次，所以
  ECAS 的比例逐行来自 ECAS 数据，**永远不从渠道表取**。月结卡片上写明了「不含 ECAS」，
  改文案前先看 `docs/ECAS.md`。
- 更细的背景：`README.md`、`docs/PIPELINE.md`（每日管线）、`docs/BOT.md`（机器人现状与缺口）、
  `docs/MIGRATION.md`（迁移细节与两种走法）、`docs/SCHEMA.md`（表结构）、`docs/DASHBOARD.md`（仪表盘）、
  `docs/ECAS.md`（ECAS 开户返佣）。

## 10. 别做的事

- 不要 `git add .env` / `.env.target`（已在 `.gitignore`，但仍要留神）——里面有真密钥。
- 不要把客户数据（xlsx、导出的 JSON）提交进仓库。
- 不要手工改 Base 的字段类型来"修"问题：列类型改了可能毁数据，先跑 `sync_base.py` 看它怎么说。
- 不要同时跑两个机器人实例（同一个应用只能有一条长连接，会互相踢下线）。
