# ECAS 开户返佣

这是**第二套账**。它和交易佣金住在同一个 Base 里，但两套的数据、费率、结算、汇总表
全都是分开的，唯一的共用物是最后由同一个机器人把结果报出去。

```
交易佣金   Daily Revenue Board ─► Referred Client ─► Referral Information.分佣比例
                                                              └─► Commission Summary

ECAS 返佣  ECAS Applications ───────────────────────────────► 这一行自己的分佣比例
                 └─► 关联 Referral Information（只取编号和名字）
                                                              └─► ECAS Commission Summary
```

## 为什么一定要分开

**因为它们本来就是两笔钱。**

同一个渠道，两边的比例可以不一样。2026-09 那两笔 ECAS 是 20%，同一批人在交易那边是
别的数。同一个客户，两边各付一次 —— CHANGZHENG YE 在 2026-08 同时拿到了 ECAS 的
5,000 和交易佣金的 3,474.08，财务两笔都付了。

所以 ECAS 的比例**只从 ECAS 的数据来**，永远不从 `Referral Information` 取。
`tests/test_ecas.py` 里有一条测试专门钉这件事：把渠道表的分佣比例改成 999%，
ECAS 算出来的钱一分不变。

## 数据从哪来

内部那份「Wallet and Trades」表格里的 `ECAS` 分页，一行一笔开户申请：

| 列 | 说明 |
|---|---|
| `Client Name` | 客户名 |
| `ECAS Revenue` | 开户金额，返佣的基数 |
| `Application Time` | 申请时间，按它归月 |
| `Sales in Charge` | 负责销售 |
| `UID` | 大多是空的 |
| `Referrer` | 介绍人。**六成的行是空的** —— 那些申请没有介绍人，不产生返佣 |
| `%` | 比例。写的是**小数**（`0.5` = 50%） |
| `Amount of Referral Fee` | 该付的返佣 |

### `%` 那一栏是 0.5 还是 50

Base 里别处的「分佣比例」一律是百分数（`50` = 50%），来源表写的却是小数。
两种写法并存，早晚有人看着 `0.5` 以为是 0.5%。所以导入时统一换算成百分数。

换算**不靠约定，靠数据自己作证**：每一行都带着 `Amount of Referral Fee`，
拿「金额 × 比例」和「金额 × 比例 ÷ 100」各算一遍，哪个对得上，那个就是真的。

```
5000 × 0.5   = 2500  ✓ 对上了  ->  这一栏是小数，换算成 50
5000 × 0.5/100 = 25  ✗
```

两个都对不上就**整次拒绝导入**，不是跳过那一行。跳过会让合计悄悄变小，
而变小之后没有任何地方会报警 —— 这张表的全部价值就在于合计对得上来源表。

哪天上游改成写 `50` 而不是 `0.5`，这里不用改代码也不会算错。

## 建出来的两张表

### `ECAS Applications`

来源表的镜像 + 挂上渠道 + 逐笔返佣。**全部 158 笔申请都进去**，
包括没有介绍人的那些 —— 它是「开户申请全集」，不只是「要付钱的那些」。

| 列 | 类型 | 说明 |
|---|---|---|
| 客户名称 | 文本 | |
| ECAS金额 | 数字 | |
| 申请时间 | 日期 | 按业务时区存 |
| 负责销售 | 文本 | |
| 客户UID | 文本 | 来源表里大多是空的；被 Excel 改坏的一律留空并报出来 |
| 渠道名称 | 文本 | 照抄来源表的写法 |
| 所属渠道 | 关联 | 指向 `Referral Information`，**只为了拿编号和名字** |
| 渠道编号 | 公式 | `[所属渠道].[渠道编号]` |
| 分佣比例 | 数字 | 百分数。**这一行自己的**，不是渠道的 |
| ECAS佣金 | 公式 | `金额 × 比例 / 100`，没填比例的行留空（不是 0） |
| 月份 | 公式 | `TEXT([申请时间], "yyyy-MM")` |

### `ECAS Commission Summary`

按月 × 渠道。列和交易佣金那张汇总表几乎一样，只有一处不同：

**「比例说明」是文本，不是数字。** ECAS 的比例是逐行的，一个渠道一个月可以有好几档
（`50%×24笔 / 20%×2笔`）。硬塞一个数字进去，不管填哪一档都是在撒谎。

## 怎么跑

### 第一次：建表 + 导数据

需要 `.env` 里有 `LARK_APP_ID` / `LARK_APP_SECRET` / `LARK_BASE_APP_TOKEN` /
`TABLE_REFERRAL`。**不需要 mac mini** —— 这是一次性的 API 调用，哪台机器跑都一样。

```bash
# 先预演，什么都不写
uv run python scripts/import_ecas.py --file "$HOME/Downloads/Wallet and Trades_ECAS.xlsx"

# 确认无误再真跑
uv run python scripts/import_ecas.py --file "$HOME/Downloads/Wallet and Trades_ECAS.xlsx" --apply
```

`--apply` 会建两张表、补齐所有列、写入记录，并把 `TABLE_ECAS` /
`TABLE_ECAS_COMMISSION` 回填到 `.env`（不用去界面上抄 table_id）。

建表被 `1254302 RolePermNotAllow` 拒掉的话，脚本会把两条解法打出来
（把应用权限临时改成「可管理」，或者人手建一张空表再重跑）。

### 换了新的一份文件

整张表替换，不做增量 —— 来源表是单一事实来源：

```bash
uv run python scripts/import_ecas.py --file "新的一份.xlsx" --apply --refresh
```

### 结算

```bash
uv run python -m crm_basebot.jobs.ecas_reconcile                          # 最新月份，只算不写
uv run python -m crm_basebot.jobs.ecas_reconcile --period 2026-08
uv run python -m crm_basebot.jobs.ecas_reconcile --all-periods --write    # 写进汇总表
uv run python -m crm_basebot.jobs.ecas_reconcile --period 2026-08 --write --replace
```

开关和交易佣金那个 `reconcile` 逐条一致，拒绝规则也一样：同一个月份不许写出第二套
汇总，要顶掉得显式 `--replace`，算出来是空的时候不会拿空结果去顶掉已有的。

## 对过的数

2026-09 那份来源表，逐行复算：

| 月份 | 应付 |
|---|---:|
| 2026-03 | 750.00 |
| 2026-04 | 7,500.00 |
| 2026-05 | 15,000.00 |
| 2026-06 | 66,000.00 |
| 2026-07 | 44,000.00 |
| **2026-08** | **65,000.00** |
| 2026-09 | 2,000.00 |
| 合计 | **200,250.00** |

- 合计和来源表 `Amount of Referral Fee` 那一列**逐分一致**。
- 2026-08 的 65,000.00 和财务那份 `08_August2026_ReferralFee_Recomputed` 的
  ECAS 分页**完全一致**。

舍入口径也和财务一样：财务的 Notes 写明「每一笔先进位到两位小数，再求和」，
而 ECAS 的比例本来就是逐行的，所以这里天然就是逐行进位再求和。

## 已知要人处理的事

1. **`Gong Ming` 那一行的 Referrer 栏填的是「Referrer」**（2026-06，6,000 × 10% = 600）。
   不是渠道名，是没填好的资料。申请照样导入，但不算返佣。要 Jackie 去改或删。
2. **渠道表里没登记的收款方**。ECAS 表里的介绍人名字在 `Referral Information` 里
   找不到时，返佣**照算**（钱是欠着的），只是汇总行的渠道编号是空的。
   结算时会单独列出来。去渠道表把他们登记上，名字写成和 ECAS 表一致，再重跑一次导入。
3. **ECAS 的比例不是固定 50%**。2026-09 那两笔是 20%，另外还有 30% 和 15% 各一笔。
   按固定 50% 算会多付。这件事要让财务知道。
4. **两个 UID 被 Excel 改坏**（`YEMU XU` / `TEERAWAS CHAUSIRI`，尾巴一串 0）。
   导入时留空了。要让对方把那一列设成文本后重新导出。

## 还没做的

- **机器人上的 ECAS 按钮**。要改 `src/crm_basebot/bot/cards.py` 和 `handlers.py`。
- **每月自动结算 ECAS**。现有的 `scripts/monthly_reconcile.py` 只结算交易佣金，
  卡片上已经写明「不含 ECAS」。ECAS 的月结任务还没建。
- **ECAS 客户要不要同时算交易佣金**。这是个业务问题，不是技术问题：
  ECAS 表里 79 个被介绍的客户，只有 9 个登记在 `Referred Client` 里，
  所以只有那 9 个的交易会算出佣金。要不要把另外 68 个补登记，等业务拍板。
  补登记的脚本是 `scripts/register_ecas_clients.py`，**在业务确认之前不要跑 `--apply`**。
  这件事和上面的 ECAS 结算**完全无关** —— ECAS 自己的返佣不受它影响。
