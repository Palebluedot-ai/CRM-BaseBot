# CRM-BaseBot 维护手册

> 给之后接手这个项目、这个机器人的人。不写代码也看得懂。
> 最后更新：2026-09-30。代码仓库里有同一份：`docs/MAINTAINER_GUIDE.md`；要改代码看仓库根目录的 `CONTRIBUTING.md`。

## 一、这是什么

**HashKey OTC 渠道（Referral）佣金系统。** 渠道介绍客户来交易、开户，我们按比例付佣金给渠道。这套东西把原来靠人手在 Excel 算的事自动化了：

| 部分 | 在哪 | 做什么 |
| --- | --- | --- |
| **Lark Base「CRM-BaseBot」** | Lark 云文档 | 所有资料：渠道、客户、每日交易、结算、名册、存档、仪表盘 |
| **机器人「CRM-BaseBot」** | Lark 里搜名字 | 销售登记渠道和客户、查佣金、生成协议和 Invoice |
| **定时任务** | 公司的 mac mini | 每天导交易、更新当月佣金；每月 1 号结算 |
| **代码** | GitHub `Palebluedot-ai/CRM-BaseBot` | 上面所有东西的程序 |

两种佣金，**完全分开算**：

- **交易佣金**：渠道介绍来的客户交易带来的收入 × 渠道的分佣比例。**2026-09-01 起只算 AI 客户**：升级 AI 的第二天起的交易才算；开户即 AI 的全算；AI 状态空着的老客户照算；非 AI 不算。
- **ECAS 返佣**：客户开 ECAS 账户，按申请逐笔返给渠道（常见每笔 2,500）。不看 AI。

## 二、Base 里每张表

| 分组 | 表 | 放什么 | 谁写 |
| --- | --- | --- | --- |
| Trades管理 | **Referral Information** | 渠道：编号（R001…）、名称、比例、结算频率、负责销售、收款资料 | 机器人「登记新渠道」「登记收款资料」 |
|  | **Referred Client** | 客户：UID、名称、所属渠道、AI 状态和升级日期 | 机器人「登记新客户」「更新客户AI状态」 |
| Database | **Daily Revenue Board**（看板） | 每天每个客户的交易收入（只收新加坡站），右边几列自动算出渠道、比例、本笔佣金；最右边「渠道（自动查找）」由 Base 自己按 UID 查 | 每天自动导入 |
|  | **Commission Summary** | 交易佣金：每月每个渠道一行。「状态」= 进行中（当月，每天更新）或 已结算（**付钱以它为准**） | 每天自动更新，每月 1 号结算 |
|  | Client Directory | 公司客户名录（参考用） | 导入 |
|  | Audit Log | 谁在什么时候改了什么（不记账号） | 自动 |
| ECAS管理 | **ECAS Applications** | 每笔 ECAS 申请；「所属渠道」和「分佣比例」要人补 | 导入 + 人工 |
|  | **ECAS Commission Summary** | ECAS：每月每个渠道一行，同样有「状态」 | 每天自动更新，每月 1 号结算 |
| 销售管理 | **Sales Directory**（名册） | 能用机器人的人：OpenID、姓名、角色、状态、机器人可见范围、邮箱 | 人工 |
| Archive | **YYYY-MM 结算明细 / 结算汇总** | 每月结算那一刻的存底（明细到客户），之后不会变 | 每月 1 号自动建，**要人拖进 Archive** |
|  | **结算明细（全部月份）** | 所有月份的明细，还没结算的月份标「未结算」、每天更新 | 自动 |

**名册的几列要知道：**
- **角色**：「管理员」= 每月 1 号收月结卡片。不影响能看什么。
- **机器人可见范围**：空着或「只看自己」= 机器人里只看自己名下的渠道；「看全部」= 全部。
- **邮箱**：印在这个人生成的 Invoice 上（Sales Representative）。

## 三、机器人（9 个功能）

打开和机器人的对话，菜单会自己弹出来（同一个人 5 分钟内只弹一次）。

| 功能 | 做什么 |
| --- | --- |
| 🏢 登记新渠道 | 自动给编号，归到自己名下 |
| 👤 登记新客户 | 填 UID、名称、AI 状态；**登记当下就把他以前的交易挂上** |
| 📋 我的渠道 | 列出名下渠道，点进去看近 3 个月 |
| 💰 佣金查询 | 选月份，看每个渠道每个客户的交易佣金 |
| 💎 ECAS 返佣 | 选月份，看 ECAS 返佣 |
| 🤖 更新客户AI状态 | 客户升级 AI 后补日期 |
| 📝 生成转介协议 | 个人 / 企业，发回 Word + PDF，资料不存 Base |
| 🏦 登记收款资料 | 渠道的地址、银行账户或钱包 |
| 🧾 生成 Invoice | 选月份，每个渠道一份 Word + PDF，金额取自结算表 |

销售用的说明书：仓库 `docs/SALES_GUIDE.md`。

## 四、每天、每月自动发生什么

**每天 10:45、16:00（mac mini）**
1. 从 Outlook 邮箱取每日收入导出（xlsx 附件），存在 mac mini 的 `attachments/`。
2. 只取新加坡站的行，写进看板（同一天重导会整天替换）。
3. 把「客户后来才登记」的旧交易补挂上。
4. 两张结算表里当月（月初还没结的话也包括上个月）重算一遍，标「进行中」。
5. 刷新存档总表里「未结算」的月份。

**每月 1 号 16:30（mac mini）**
1. 结算上个月：两张结算表里上个月的「进行中」换成「已结算」，之后不再变。
2. 建存档「YYYY-MM 结算明细」「YYYY-MM 结算汇总」。
3. 发月结卡片给名册里的管理员（两项分别列 + 合计）。

**人每月要做的**：把两张新存档表拖进左边栏的 Archive；ECAS 申请要有人导入并补上渠道和比例（在结算前）。

## 五、仪表盘

Base 左边栏「Dashboard」。上面看钱，下面看客户：

- **交易佣金该付 / ECAS 该付**：每个渠道每个月要付多少，来自结算表。已结算的月份**和 Invoice 一样**；当月是进行中的数，每天更新，下个月 1 号下午锁定。
- **交易佣金明细 / ECAS 佣金明细**：每个渠道下面每个客户每个月的贡献，按**现在**的资料算，当月每天更新。

⚠️ 旧月份（1–8 月）明细表的数不一定等于实际付款：结算后改过比例（DAI CANGWEI 30%→50%）、补登记了客户、少数交易不在每日导出里。**付钱永远看「该付」表或财务。** 9 月起只要结算后不回头改那个月的资料，两边会一致。

## 六、机器和账号

| 东西 | 在哪 | 备注 |
| --- | --- | --- |
| **mac mini** | 公司 | 机器人和定时任务都在这台，**要一直开着、不休眠**。装了 Microsoft Word（协议、Invoice 转 PDF 用） |
| **MacBook**（Terrance） | — | 合并代码、改 Base 结构、跑查询脚本 |
| **Lark 应用** | open.larksuite.com/app → CRM-BaseBot | 权限、事件、版本发布都在这里；改了要发新版本 |
| **GitHub 正式仓库** | `Palebluedot-ai/CRM-BaseBot` 的 `main` | mac mini 从这里拉代码 |
| **GitHub 开发仓库** | `okyterrance/CRM-BaseBot` | 开发暂存，合并进正式仓库才上线 |
| **.env** | mac mini 和 MacBook 的 crm-basebot 资料夹 | App ID / Secret、Base 地址、各表 id、邮箱凭证。**绝不发到聊天、不进 GitHub** |

## 七、常见的事怎么做

所有命令都在 crm-basebot 资料夹里跑。带 `--apply` 才真的写，不带只预演。

**上线新代码**
1. MacBook：`git checkout main && git pull https://github.com/okyterrance/CRM-BaseBot.git claude/pensive-wright-k6h2km && git push origin main`
2. 改了表结构的话，MacBook：`uv run python scripts/sync_base.py --apply`
3. mac mini：`git pull origin main && launchctl kickstart -k gui/$(id -u)/com.chao.crm-basebot.bot`

**加一个新销售**
1. 名册加一行：英文姓名、角色=销售、状态=在职、邮箱。
2. 请他在 Lark 搜「CRM-BaseBot」发一句话（机器人会拒绝，正常）。搜不到的话，去开发者后台的 Availability 把他加进去、发版本。
3. mac mini：`uv run python scripts/collect_open_ids.py --log logs/bot.log --name "他的姓名" --apply`
4. 一分钟后他就能用了。

**某个客户的佣金不对**
- `uv run python scripts/diagnose_client.py --name "客户名称"`：看登记在哪个渠道、看板上每个月有没有交易、挂上没有。
- 客户登记了但看板没挂上：`uv run python scripts/relink_board.py --apply`。

**财务表有、我们没登记的客户**
- `uv run python scripts/register_from_board.py --referral R029 --name "客户名称" --ai-status 开户即AI --apply`（按看板上的名字找 UID，不用抄）。

**和财务对账**
- `uv run python scripts/compare_finance.py --period 2026-07 --file ~/Downloads/财务那份.xlsx`：逐渠道、逐客户列出差在哪。
- `uv run python scripts/compare_settled.py`：结算时 vs 现在重算，差在哪、为什么。
- 交易在不在每日导出里、哪个站点（mac mini 跑）：`uv run python scripts/find_in_exports.py --uid 客户UID`。

**补一个月的存档**：`uv run python scripts/archive_month.py --period 2026-08 --apply`（写之前自检，差一分钱就不写）。

## 八、出问题对照

| 现象 | 可能原因 / 怎么办 |
| --- | --- |
| 机器人完全没反应 | mac mini 关机、断网或休眠。看 `logs/bot.log`；`scripts/bot_connectivity_report.py` 列出断线时段 |
| 机器人说「你还没有被登记」 | 名册没这个人或 OpenID 空着 → 见「加一个新销售」 |
| 打开对话不弹菜单 | 5 分钟冷却；或开发者后台没订阅「User enter chat with bot」事件 |
| 看板今天没新数据 | 当天邮件没来或附件格式变了；看 `logs/daily-import-stderr.log` |
| 月结卡片没收到 | 名册里没有「在职 + 管理员 + 有 OpenID」的人；看 `logs/monthly-reconcile-stderr.log` |
| 生成 Invoice 只有 Word 没有 PDF | mac mini 上 Word 弹了窗口在等人点，去点掉；结果卡上会写原因 |
| Invoice 有渠道没出 | 收款资料缺钱包地址或银行账号 → 「登记收款资料」补 |
| 佣金数字和财务对不上 | 先跑 `compare_finance.py`，再用 `diagnose_client.py` 查具体客户 |

## 九、做过的决定（别改回去）

- **付钱以结算表里「已结算」的行为准。** 每月 1 号 16:30 锁住上个月，之后改资料不会改到它；要重算得显式 `--replace`。Invoice 只出已结算的月份。
- **AI 规则从 2026-09-01 起**，升级当天不算、第二天起算。8 月及以前全部照算（已经付过）。
- **每个月合计为负的渠道记 0**，不倒扣、不结转。
- **比例在月初结算后再改**，从下个月生效；否则旧月份的明细会被新比例重算。
- **只收新加坡站的交易。** 别的站点的交易（例如 PRIMAL TECH SUPPLY 7 月那笔）系统算不到，要不要算是业务决定。
- **机器人里只看自己名下的渠道**，管理员也一样；看全部人的数据用仪表盘。
- **Invoice 缺地址照出，缺收款账户不出。** Sales Representative 印生成的人。
- **UID 永远是文本。** 19 位的 UID 存成数字会丢末几位，客户就挂错。

## 十、还没完成 / 待决定

- **1–8 月补发**：DAI CANGWEI 2–8 月按 30% 付（现在 50%）；JIANG JUN、1 ORIGIN、HongKong Dimi 有结算后才登记的客户。金额见 `compare_settled.py`。目前决定不处理。
- **1–7 月没有存档**（8 月有），财务的月度表是准的。
- **权限**：渠道表的收款几列要在 Base「高级权限」对销售角色隐藏；仪表盘对销售隐藏；超哥要是 Base 管理员。
- **ECAS 申请**目前靠人导入、补渠道；没有渠道的申请不付返佣。

## 十一、更多文档（在代码仓库里）

| 文档 | 内容 |
| --- | --- |
| `CONTRIBUTING.md` | 改代码、测试、发布 |
| `docs/SALES_GUIDE.md` | 给销售看的使用说明 |
| `docs/BOT.md` | 机器人每个功能的设计 |
| `docs/SCHEMA.md` | 每张表每一列 |
| `docs/PIPELINE.md` | 每日导入 |
| `docs/ECAS.md` | ECAS 返佣 |
| `docs/DASHBOARD.md` | 仪表盘和存档 |
| `docs/LARK_APP_SETUP.md` | Lark 应用的权限和事件 |
| `HANDOFF.md` | 整套搬到新的 Lark 租户 |
