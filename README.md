# CRM-BaseBot

> **先看哪一份？**
>
> | 你是谁 | 读这份 |
> |---|---|
> | **接手方（同事 / 他的 AI agent）**：要把整套系统建到自己账号 | **[HANDOFF.md](HANDOFF.md)**（机器读）· **[HANDOFF.html](HANDOFF.html)**（人读，`open HANDOFF.html` 打开；含「环境与账号要求」和「交付验收清单」） |
> | **销售同事**：日常用机器人登记渠道/客户、查佣金 | **[docs/SALES_GUIDE.md](docs/SALES_GUIDE.md)** |
> | **维护者**：日常跑批、排错 | [docs/PIPELINE.md](docs/PIPELINE.md)（每日管线）· [docs/BOT.md](docs/BOT.md)（机器人现状与缺口）· [docs/MIGRATION.md](docs/MIGRATION.md)（迁移） |
>
> 接手方不需要原主人在电脑前操作；数据搬过去有「凭证迁移」和「只交接文件」两条路，见 HANDOFF。

渠道佣金 CRM，跑在 Lark Base 上，销售通过 Lark 机器人登记和查询。

## 解决什么问题

渠道佣金的数据本来就在 Lark Base 里，但有个死结：想让销售自己登记渠道和客户，就得把他们加成 Base 协作者；一旦加了，他们就能看到整张表 —— 包括别的销售的渠道、客户和佣金。管理员只能手动一个个加人、一个个调权限，累且不可靠。

这个项目把销售挡在 Base 外面：他们只跟机器人对话，机器人代表应用身份去读写 Base。谁能看什么、谁能改什么，是后端代码里的规则，有测试、走 git、能审计。

## 怎么做到的

飞书的卡片回调里带 `operator.open_id`，这是平台签名保证的、客户端伪造不了。后端拿这个 open_id 查销售身份，然后只放行属于他自己的记录。

由此带来几个好处：

- 免费版飞书就能跑通全流程，本地开发不用等审批
- 隔离规则是代码不是配置，能写单元测试
- 从测试组织迁到公司组织只换一组凭证，代码零改动
- Base 自身的高级权限（企业版功能）降级为可选的第二层防御，不再是必需品

## 快速开始

```bash
uv sync
cp .env.example .env         # 填凭证，见 docs/LARK_APP_SETUP.md
uv run pytest
```

拿到飞书凭证之后，按这个顺序走：

```bash
uv run python scripts/ws_smoke.py           # 验证长连接和卡片回调，记下日志里的 open_id
uv run python scripts/inspect_base.py       # 看现有 Base 有什么
uv run python scripts/sync_base.py          # 预演要建哪些表和字段
uv run python scripts/sync_base.py --apply  # 执行
uv run python scripts/seed_dev_data.py --open-id ou_xxx   # 预演种子数据
uv run python scripts/seed_dev_data.py --open-id ou_xxx --yes-this-is-a-dev-base
uv run python scripts/verify_numbering.py --probe   # 实测 R+3 位编号
uv run python scripts/import_registrations.py --file "Template .xlsx"        # 渠道和客户从模板导入，先预演，加 --apply 真写
uv run python scripts/import_daily_board.py --file 交易明细.xlsx --dry-run   # 生产：导入内部系统导出的交易明细，先预演
uv run python scripts/import_daily_incremental.py --from-mail --dry-run      # 日常：邮件取数 + 只导新加坡站的新增交易日
./scripts/install-daily-import-launchd.sh                                   # 挂成每天 10:45 / 16:00 自动跑
uv run python -m crm_basebot.app            # 启动机器人
```

**为什么要造种子数据**：自建应用只能在同一个企业租户内使用，所以阶段 A 那个自建的免费
组织，读不到公司的真实数据。而手工导出 CSV 再导进来是不行的 —— Excel
只保留 15 位有效数字，18-19 位的客户UID 一过 Excel 就被抹掉低位，测试数据从第一天起就
是坏的。`scripts/seed_dev_data.py` 走 API 直接写，全程不经过浮点数，往 `sync_base.py` 建好的
日读看板表里灌一批模拟行。它默认只预演，且会先扫一遍目标 Base，发现不是它造的
数据就拒绝执行。

对账（默认只算不写）：

```bash
uv run python -m crm_basebot.jobs.reconcile                    # 最新有数据的月份
uv run python -m crm_basebot.jobs.reconcile --period 2026-03   # 指定月份
uv run python -m crm_basebot.jobs.reconcile --all-periods      # 全部月份
uv run python -m crm_basebot.jobs.reconcile --period 2026-03 --write
uv run python -m crm_basebot.jobs.reconcile --period 2026-03 --write --replace   # 重算：先删该月旧汇总再写
```

不传 `--period` 时结算的是**日读看板里最新有数据的那个月**，不是「上个月」。写死上个月，月初跑的时候会算出一片空白，而它又恰好在「这个月的数据其实已经有了」的时候什么都不说。实际选中的月份一定会打印在输出第一行，不用猜。

**重跑**：`--write` 遇到汇总表里已经有本次结算月份的行会拒绝，不会在旁边再写一套。数据改过要重算就加 `--replace`，先删那些月份的旧行再写新的；`--all-periods --replace` 清空整张汇总表。算出来是空的时候不会拿空结果顶掉旧汇总（2026-09-05 定的）。

**只看新加坡站**：看板只放「站点」是新加坡站的记录，导入时香港站、中东站的行直接丢掉（2026-09-17 定的）。筛的是站点列，不是销售分组。

**每天的数据是增量进来的**：内部系统每天把导出邮件发到指定邮箱，`scripts/import_daily_incremental.py` 去取最新那份附件，本地筛出新加坡站、算出「哪些交易日看板还没有」（通常就是新增的一天），只把那几天整天替换。所以日常跑批不会重写几个月的历史，也不会在仪表盘读数据的时候把表清空。历史被修订时要显式 `--refresh YYYY-MM-DD` 或用全量脚本回填。完整链路、Graph 应用权限怎么配、launchd 任务怎么装和回滚，见 [docs/PIPELINE.md](docs/PIPELINE.md)。

**佣金规则**：`应付佣金 = max(0, 当月 总收入(opt+现货+合约) 合计 × 分佣比例)`。基数是日读看板里的毛收入，2026-09-09 定的，此前按 Pnl(USD) 算。整月合计为负的渠道佣金按 0 保底，不倒扣、也不结转到下个月 —— 这是业务规则，2026-08-30 明确定的，不是代码漏了处理负数。收入合计仍然如实记录负值，报表上看得见这个渠道当月是负的，汇总输出也会单独标注一行。细节见 `domain/commission.py` 的 `CommissionRow.payable`。

**归月时区**：「当月」按 `.env` 里 `BUSINESS_TIMEZONE` 指定的时区算，默认 `Asia/Singapore`（2026-09-04 定的）。Bitable 日期字段存的是 UTC 时间戳，直接按 UTC 取月份的话，每个月 1 号 0 点到 8 点的交易会全部算进上个月。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/LARK_APP_SETUP.md](docs/LARK_APP_SETUP.md) | 自建免费飞书组织、创建应用、开权限、开长连接 |
| [docs/IT_APPROVAL.md](docs/IT_APPROVAL.md) | 向公司 IT 申请时的完整材料，力求一次过审 |
| [docs/SCHEMA.md](docs/SCHEMA.md) | 表结构定义，以及每个设计选择的理由 |
| [docs/BOT.md](docs/BOT.md) | 机器人现在能做什么、闭环还缺什么（梳理，不是提案） |
| [docs/PIPELINE.md](docs/PIPELINE.md) | 每日数据管线：邮件取数、增量导入、Graph 权限、launchd 任务 |
| [docs/MIGRATION.md](docs/MIGRATION.md) | 一条命令把整套 Base 搬到另一个飞书账号 |
| [docs/SALES_GUIDE.md](docs/SALES_GUIDE.md) | 销售同事怎么用机器人（给终端用户的一页说明） |
| [docs/OPENID_ONBOARDING.md](docs/OPENID_ONBOARDING.md) | **OpenID 登记规程**：每位销售 / 每换一个 Bot 都要走一遍（永久保留） |
| [HANDOFF.html](HANDOFF.html) | 接手手册的浏览器版（人+机器都读） |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | 按月佣金仪表盘怎么搭（界面步骤），佣金几列怎么算 |

## 三个必须知道的坑

**1. 客户UID 会丢精度**

客户UID 是 18–19 位数字，超过 float64 的安全整数上限（2^53，16 位）。全链路必须当字符串处理，任何一处 `int()` 或 `float()` 都会让 join key 静默错配，佣金算到别人头上。`values.py` 的读取层强制转字符串（拿到浮点数直接报错而不是凑合），`tests/test_values.py` 用真实 UID 值锁住这个行为，`tests/test_write_payloads.py` 盯住写回去的那一侧。

字符串化只能保证 UID 在我们手里不坏。日读看板是从内部系统导出的 xlsx，只要用户ID 那一列在源头被 Excel 当成数字处理（只保留 15 位有效数字），UID 的低位在进 Base 之前就已经被抹成 0 了 —— 这种损伤下游修不了。所以 `scripts/import_daily_board.py` 只认 xlsx 不认 CSV，拿到浮点形态的用户ID 直接报错；`values.py` 的 `assess_uid_health()` 再用尾零特征做事后诊断，`scripts/inspect_base.py` 和对账流程都会跑一遍并告警。它是启发式，只提示不拦截，判据和误报权衡写在 `looks_excel_truncated()` 的 docstring 里。

**2. SDK 在长连接下会丢弃卡片回调**

`lark-oapi` 的 WebSocket 客户端把 CARD 帧直接 return 掉了（[issue #126](https://github.com/larksuite/oapi-sdk-python/issues/126)，已关闭但至今未修）。后果是销售点提交按钮报 `200340`，服务端没有任何日志。`lark/ws_patch.py` 打了补丁，`tests/test_ws_patch.py` 会在 SDK 官方修复后提醒可以删掉它。

补丁把 CARD 帧的 `type` 头改写成 `event`，副作用是回写给平台的响应帧 `type` 也变成 `event`。这个副作用**已经真机验证过不影响交互**：2026-08-31 在自建的免费团队租户上（lark-oapi 1.7.3，长连接模式）跑通完整链路，点卡片表单的提交按钮后正常弹出 toast，没有 `200340`。结论只对 1.7.x 有效，升级 SDK 后用 `scripts/ws_smoke.py` 重验一次。

生产环境绕不开长连接：公司内网服务器没有公网入口，webhook 打不进来，长连接只需要出网。

**3. Bitable 写接口不支持并发**

并发写同一张表会返回 `1254291 Write conflict`。所有写操作都在 `bitable.py` 的进程级写锁里串行执行。这把锁顺带给了编号递增一个安全的临界区。

## 状态

阶段 A（本地免审批开发）。阶段 B 是一次性 IT 审批，阶段 C 是内网部署。
