# CRM-BaseBot

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
uv run python scripts/ws_smoke.py           # 验证长连接和卡片回调
uv run python scripts/inspect_base.py       # 看现有 Base 有什么
uv run python scripts/sync_base.py          # 预演要建哪些表和字段
uv run python scripts/sync_base.py --apply  # 执行
uv run python scripts/verify_numbering.py --probe   # 实测 R+3 位编号
uv run python -m crm_basebot.app            # 启动机器人
```

对账（默认只算不写）：

```bash
uv run python -m crm_basebot.jobs.reconcile --period 2026-03
uv run python -m crm_basebot.jobs.reconcile --period 2026-03 --write
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/LARK_APP_SETUP.md](docs/LARK_APP_SETUP.md) | 自建免费飞书组织、创建应用、开权限、开长连接 |
| [docs/IT_APPROVAL.md](docs/IT_APPROVAL.md) | 向公司 IT 申请时的完整材料，力求一次过审 |
| [docs/SCHEMA.md](docs/SCHEMA.md) | 表结构定义，以及每个设计选择的理由 |

## 三个必须知道的坑

**1. 客户UID 会丢精度**

客户UID 是 18–19 位数字，超过 float64 的安全整数上限（2^53，16 位）。全链路必须当字符串处理，任何一处 `int()` 或 `float()` 都会让 join key 静默错配，佣金算到别人头上。`bitable.py` 的读取层强制转字符串，`tests/test_bitable.py` 用真实 UID 值锁住这个行为。

**2. SDK 在长连接下会丢弃卡片回调**

`lark-oapi` 的 WebSocket 客户端把 CARD 帧直接 return 掉了（[issue #126](https://github.com/larksuite/oapi-sdk-python/issues/126)，已关闭但至今未修）。后果是销售点提交按钮报 `200340`，服务端没有任何日志。`lark/ws_patch.py` 打了补丁，`tests/test_ws_patch.py` 会在 SDK 官方修复后提醒可以删掉它。

生产环境绕不开长连接：公司内网服务器没有公网入口，webhook 打不进来，长连接只需要出网。

**3. Bitable 写接口不支持并发**

并发写同一个 Base 会返回 `1254045 WriteConflict`。所有写操作都过 `bitable.py` 里单 worker 的队列串行化。这个队列顺带给了编号递增一个安全的临界区。

## 状态

阶段 A（本地免审批开发）。阶段 B 是一次性 IT 审批，阶段 C 是内网部署。
