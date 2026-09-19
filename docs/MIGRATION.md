# Base 迁移：一条命令搬到另一个飞书账号

场景：这套 CRM 先在**你的**飞书账号里跑通了，现在要搬到**另一个人**的账号下（他的应用、
他的团队、他的 Base）。两边不一样的地方只有两处：

| | 源（你的账号） | 目标（他的账号） |
|---|---|---|
| 应用凭证 | `.env` | `.env.target` |
| Base | 你的 | 他的 |
| **表头与内容** | **一模一样**（同一个 schema 建出来的） | 同上 |
| open_id | 你的应用签发 | **完全不同**，人员列不搬 |

因为表头一模一样，搬运不需要任何映射表：**字段按名字对应，关联按业务键重建**
（客户→渠道 用「渠道编号」，看板→客户 用「客户UID」）。

## 一、准备目标环境文件（**在哪儿填、填什么**）

```bash
cp .env.target.example .env.target      # 模板就在仓库里，同事 clone 下来就有
```

`.env.target` 在仓库根目录，**只需要填三个值**（模板里每一项都写了去哪抄）：

| 填什么 | 去哪拿 |
|---|---|
| `LARK_APP_ID` | https://open.feishu.cn/app → 在**同事的**账号里「创建企业自建应用」→ 左侧「凭证与基础信息」 |
| `LARK_APP_SECRET` | 同上那一页 |
| `LARK_BASE_APP_TOKEN` | **走法 A 可以留空**（见下）；走法 B 填同事自己建的那个空 Base 的 URL 里 `/base/<这一段>` |
| `BUSINESS_TIMEZONE` | 保持 `Asia/Singapore`，除非业务不在新加坡 |

两个必做的开通动作（**漏了会得到 403，而报错完全看不出原因** —— 这是迁移最容易卡住的地方）：

1. **权限**：开发者后台 → 该应用 → 「权限管理」→ 搜「多维表格」→ 开通 `bitable:app`（读写）
2. **发版**：同一后台 → 「版本管理与发布」→ 创建版本 → 申请发布（自建企业应用通常要管理员点一下）

`.env.target` 已在 `.gitignore` 里（`.env.target`、`.env.target.*`），不会被提交。

### 目标 Base 用哪种走法？

| | 走法 A（推荐） | 走法 B |
|---|---|---|
| 做法 | 命令里加 `--create-base "CRM 佣金看板"`，**什么都不用建** | 同事自己先建一个空 Base，把 URL 里的 token 填进 `.env.target` |
| 优点 | 应用自己建 = 它就是所有者，**不用**「把应用加为协作者」这一步（少一个 403 坑） | Base 从一出生就在同事自己的空间里，他界面上直接看得见 |
| 代价 | 建完要在 Base 界面把同事加成协作者（或把所有权转给他），他才能看见 | 必须记得把应用加成协作者（可编辑），否则 403 |
| token | 迁移自动写回 `.env.target` | 手工填 |

## 二、跑（默认只预演）

```bash
# 走法 A：目标账号里连 Base 都还没建
uv run python scripts/migrate_base.py --target-env .env.target --create-base "CRM 佣金看板" --dry-run
uv run python scripts/migrate_base.py --target-env .env.target --create-base "CRM 佣金看板" --apply

# 走法 B：已经有一个空 Base（token 填在 .env.target 里）
uv run python scripts/migrate_base.py --target-env .env.target --dry-run
uv run python scripts/migrate_base.py --target-env .env.target --apply
```

**在哪台机器上跑？** 任何一台能同时看到两份环境文件的机器都行 —— 所以最省事的做法是
**在你的机器上跑**（源端 `.env` 已经在这儿了），同事那边只需要出一个应用凭证。搬完 Base
和结构都在他的账号里，之后他 clone 仓库、填自己的 `.env`（把 `.env.target` 的内容抄成
`.env`）就能接手日常。

`--apply` 一次做完这些，中间不用手工点 Base：

```
1. 目标端建结构   缺的表建、缺的列加，含看板那几列的公式（和 sync_base.py 同一份逻辑）
2. 搬渠道         Referral Information
3. 搬客户         Referred Client，所属渠道按「渠道编号」重建关联
4. 搬看板         Daily Revenue Board，客户按「客户UID」重建关联
5. 搬销售名册     Sales Directory（只搬姓名/角色/状态）
6. 自检 + 报告    逐表比对两边行数，打印「还需人工」的清单
```

可选项：`--include-audit`（连审计日志一起搬）、`--include-commission`（连月度汇总一起搬；
不搬的话目标端跑一次 `reconcile` 就有了）。

## 二·五、方案 C：不交换任何凭证，只交接两个文件

如果委托人一行凭证都不想给，也可以只传数据文件：

```bash
# 源端（原主人的机器）：导出两个 xlsx
uv run python scripts/export_for_migration.py --out out/handover.xlsx --board-out out/board.xlsx

# 目标端（委托人的机器）：用现成的导入脚本灌进去
uv run python scripts/import_registrations.py --file out/handover.xlsx --dry-run   # 先预演
uv run python scripts/import_registrations.py --file out/handover.xlsx --apply
uv run python scripts/import_daily_board.py --file out/board.xlsx --apply
```

导出的表头照抄现成导入脚本认的那套（渠道/客户是模板 xlsx 的形状，看板就是那 18 列），
所以目标端不需要任何改造。实测：导出 1,650 行看板后导入端读出「客户关联 342/1650 行
（43 个用户）」，与源 Base 完全一致。

**三条注意**：

1. 这两个文件**含真实客户数据**：`out/` 和 `*.xlsx` 都在 `.gitignore` 里，别提交；发文件走内部渠道。
2. **只跑一次**：没有 UID 的客户靠「编号+客户名」匹配，重复跑可能堆出重复行
   （导出命令会把你名下这类客户点出来）。方案 A 没有这个问题。
3. 结构：目标端要先把表建出来（`uv run python scripts/sync_base.py --apply`），导入脚本才会
   认得那些列 —— 这也是方案 C 比方案 A 多一步的地方。

## 三、搬完之后还需要人做的两件

这两件机器代劳不了，因为 **open_id 是飞书按应用签发的** —— 你在源端的 `ou_xxx` 在目标端
是无效值，搬过去只会指向不存在的人。

```bash
# ① 目标账号的每位销售，各给机器人发一条消息；服务端日志里会记下他的 open_id：
#    WARNING crm_basebot.bot.auth: 未登记的 open_id 尝试操作: ou_xxxx
#    然后填进目标端名册：
uv run python scripts/set_sales_open_id.py --env .env.target \
    --name "James YANG" --open-id ou_xxxx --apply

# ② 名册填好之后，按「负责销售」姓名把存量渠道/客户的归属接上：
uv run python scripts/backfill_owners.py --env .env.target --dry-run   # 先看
uv run python scripts/backfill_owners.py --env .env.target --apply
```

在 ① 之前，机器人里「我的渠道」是空的、登记客户时渠道下拉也是空的 —— 这不是迁移丢了
数据，是归属还没认领。

## 四、搬完怎么验

```bash
# 目标端看板的对账（同样是按 UID 复算，见 verify_commission.py）
cp .env .env.source.bak      # 建议先留一份源端配置
# 把 .env.target 的内容合并进 .env（或直接用它跑一遍）后：
uv run python scripts/verify_commission.py
```

迁移报告本身只比对**行数**（源 N 条 → 目标 N 条）。公式是否在算，跑一次
`sync_base.py --apply` 或看 `verify_commission.py` 的输出就知道 —— 公式列是目标端自己
算出来的，不需要搬。

## 五、不搬的东西（以及为什么）

| 不搬 | 原因 |
|---|---|
| 看板上的渠道编号/渠道名称/分佣比例/本笔佣金/月份 | 公式列，目标端自己算（写了也写不进去） |
| 归属销售（人员列） | open_id 跨应用无效 |
| 名册的 OpenID 列 | 同上，目标端重新认领 |
| 自动编号、创建时间/人 | 系统列，只读 |
| 月度汇总 / 审计日志 | 默认不搬：前者目标端可重算，后者是历史流水（要搬加对应开关） |

## 六、反复跑是安全的

结构那一步幂等（缺什么建什么，类型不对只报告不动手）；数据那一步是**追加**性质 ——
重复跑会把记录再写一遍，所以**迁移只跑一次**。真要重来，先在目标 Base 里清空那几张表，
或者换一个新的 Base。
