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

## 一、准备目标环境文件

```bash
cp .env.example .env.target
```

填**目标账号**那个飞书应用的三样（表 id 可以全部留空 —— 迁移按表名自己找）：

```
LARK_APP_ID=<目标账号应用的 App ID>
LARK_APP_SECRET=<同一个应用的 Secret>
LARK_BASE_APP_TOKEN=<目标 Base 的 token>
BUSINESS_TIMEZONE=Asia/Singapore
```

`.env.target` 已在 `.gitignore` 里，不会被提交。

## 二、跑（默认只预演）

```bash
uv run python scripts/migrate_base.py --target-env .env.target --dry-run   # 看要做什么
uv run python scripts/migrate_base.py --target-env .env.target --apply     # 真搬
```

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
