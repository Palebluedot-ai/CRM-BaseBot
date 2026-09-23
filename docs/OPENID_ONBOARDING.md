# OpenID 登记规程（每位销售都要走一遍 · 永久保留）

> 这份规程**不会过期**：每来一位新销售、每换一个飞书应用（也就是换一个 Bot），都要重新走一遍。
> 原因在第一节。相关文档：[HANDOFF.md](../HANDOFF.md)（接手流程）、[docs/BOT.md](BOT.md)（机器人现状）、
> [docs/LARK_APP_SETUP.md](LARK_APP_SETUP.md)（应用侧配置）。

## 一、为什么必须人做，而且每换一个 Bot 都要重做

`open_id` 是飞书给「**某个人在某个应用里**」签发的身份编号（`ou_` 开头的一长串）。

```
同一个人在不同应用里 → 不同的 open_id
```

所以：

- 原账号的 `ou_xxx` 在新账号里**是无效值**，数据迁移也搬不过去（搬了只会指向不存在的人）
- 新 Bot 上线前，销售发的消息**没有任何东西接收**，也就产生不了任何 open_id
- 这类身份只能**由本人的账号发一条消息**换来，没有任何接口能代替（也不该代替 —— 见第五节）

## 二、前置条件（缺一不可）

| 条件 | 怎么确认 |
|---|---|
| 应用侧配齐并**已发版** | 机器人能力 · `im:message.p2p_msg:readonly` · `im:message:send_as_bot` · 订阅 `im.message.receive_v1` + 卡片回调 `card.action.trigger` · 版本状态「已发布」。清单见 [docs/LARK_APP_SETUP.md](LARK_APP_SETUP.md) |
| **Bot 在跑** | `uv run python -m crm_basebot.app`，日志里出现 `connected to wss://msg-frontier.feishu.cn/...` |
| `.env` 指向**这个**应用的 Base | `LARK_APP_ID` / `LARK_BASE_APP_TOKEN` 属于同一套；否则名册都写错地方了 |

> ⚠️ 顺序不能反：**Bot 先跑起来，再收 open_id**。Bot 没跑时销售消息石沉大海，日志里什么都不会出现。

## 三、单人登记（标准流程）

> **先决条件：名册里得先有这个人。** `set_sales_open_id.py` 只更新现有的行，**不会帮你新建**
> —— 名册里没这个姓名它直接报错。新人要先在 Base 的 `Sales Directory` 加一行：
> `姓名` + `角色`（`销售` 或 `管理员`）+ `状态` = `在职`，OpenID 先空着。

```
① 让这位销售在飞书里找到机器人，发任意一句话
      ↓
② Bot 日志会出现一行 WARNING：
      crm_basebot.bot.auth: 未登记的 open_id 尝试操作: ou_xxxxxxxx
      ↓
③ 把 open_id 填进名册（默认预演，--apply 才写；写完会回读确认）
      uv run python scripts/set_sales_open_id.py --name "张三" --open-id ou_xxxxxxxx          # 预演
      uv run python scripts/set_sales_open_id.py --name "张三" --open-id ou_xxxxxxxx --apply  # 真写

   ⚠️ `--name` 是**规整后完全相等**，不是包含。规整只处理大小写和空白，**不处理全角/半角标点** ——
   名册里写的是 `Kevin Yu (于海峰）`（左括号半角、右括号全角）时，`--name "Kevin Yu"` 会回
   「名册里没有这个人」。直接从 `--list` 的输出把姓名整段复制过去。
      ↓
④ 核对名册（OpenID 那列应该有值了）
      uv run python scripts/set_sales_open_id.py --list
      ↓
⑤ 这一步做完，他再发消息应该能看到主菜单卡片（四个按钮）
```

## 四、一次登记多人（推荐：先都发消息，再一次性对账）

```
① 让每位销售各给机器人发一条消息（谁先谁后都行）
② 把日志里的候选捞出来、和名册对照 —— 不要用眼睛抄：
      uv run python scripts/collect_open_ids.py --log logs/bot.log
③ 脚本会列出「新面孔」。**谁是谁只有人能对上**（问本人 / 看他的飞书资料）：
   · 只剩一个候选 → 可以一步写完：
       uv run python scripts/collect_open_ids.py --log logs/bot.log --name "张三" --apply
   · 多个候选 → 脚本只给命令模板，逐个填：
       uv run python scripts/set_sales_open_id.py --name "某人" --open-id ou_xxxx --apply
④ 全部填完后回填归属（把渠道/客户挂到各自名下）：
      uv run python scripts/backfill_owners.py --dry-run      # 先看会写多少条
      uv run python scripts/backfill_owners.py --apply
⑤ 把这个人加进 Base 的高级权限角色（见下）
```

### 名册之外还有一处要加人：Base 的高级权限

名册管的是「机器人认不认得他」，**Base 里看得到什么是另一套** —— 高级权限里的角色
（记录级条件 `归属销售 = 当前访问者`）。两边都要有他，缺一样就是：

| 只加了名册 | 机器人能用，但打开 Base 看不到任何数据 |
| 只加了角色 | Base 里看得到自己的，但机器人回「你还没有被登记为销售」 |

加法：Base → 高级权限 → 那个角色 → `Members assigned to this role` → 搜姓名 → `+`。

⚠️ **管理员不要加进这个受限角色**。Base 的 Owner 本来就看得到全部，加进去反而可能被
角色的记录级条件框住。

`collect_open_ids.py` 只做三件事：捞、去重计数、和名册对照。**它不猜谁是谁** —— 日志只说明
「有人发过消息」，不说明「他是谁」。

## 五、验证清单（怎么算真的成了）

| # | 检查 | 期望 |
|---|---|---|
| 1 | `set_sales_open_id.py --list` | 该销售的 **OpenID 列有值**（`ou_` 开头） |
| 2 | 他给机器人发消息 | 弹出**主菜单卡片**（① 登记新渠道 ② 登记新客户 ③ 我的渠道 ④ 佣金查询） |
| 3 | 他点「我的渠道」 | 看到**归属他**的渠道（没回填之前是空的，见第 4 步 ④） |
| 4 | 另一位销售点「我的渠道」 | **看不到**别人的渠道 —— 归属过滤按 `owner == open_id`（`bot/auth.py: owned_records`） |
| 5 | 告警日志 | 不再出现「未登记的 open_id 尝试操作」 |

## 六、之后新增销售 / 换 Bot / 排查

| 场景 | 怎么做 |
|---|---|
| **新来一位销售** | 走第三节的 ①→⑤（他本人发消息 → 填名册 → 回填归属） |
| **换了一个 Bot（新应用、新账号）** | 整个第六节从第一节重来一遍：新应用的 open_id 与原来**完全不通用**，名册里那一列要按新应用重新收 |
| 机器人回「你还没有被登记为销售」 | 这个 open_id 不在名册（或名册里那条是**停用**状态）。跑 `collect_open_ids.py` 看他是不是新面孔 |
| 填了名册但「我的渠道」还是空的 | 名册只解决「认人」，不解决「归属」—— 还要跑 `backfill_owners.py` |
| 把某条渠道的「负责销售」改成了别人，但机器人里还是原来那个人看得到 | `backfill_owners.py` **只动「归属为空」的行**，已有归属的一律不碰（防止覆盖人工设定）。要换人就先把那一行的 `归属销售` 和 `登记人OpenID` 两格清空，再跑一次 `--apply` |
| 同一个人要同时用两个 Bot | 两边的名册**各填各的** open_id：同一个人的两个 open_id 分别属于两个应用 |

## 七、不要做的事

- **不要手工编造 `ou_`**：写一个编出来的值，机器人永远认不了这个人，而且看不出错在哪（它只是不生效）。
- **不要拿别人的账号代发消息**：open_id 会是**代发者**的 —— 于是归属记到了错的人头上，佣金也跟着错。
- **不要从卡片 value / 消息文本里取 open_id**：平台签发的身份只在回调事件本体里可信，这条是 `bot/auth.py` 开头写死的安全前提。
- **不要为了省事把 open_id 填进「负责销售」那类文本列**：机器人判归属只读 `OpenID` 列，填别处等于没填。
- 停用某人时改**状态**列（`停用`），不要删他的 OpenID —— 历史记录还要靠它对应到人。
