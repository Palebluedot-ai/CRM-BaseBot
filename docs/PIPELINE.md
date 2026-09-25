# 每日数据管线（邮件 → 本地计算 → 增量导入看板）

看板上的每一毛佣金都来自同一份原始导出：内部系统（AI Smart Analyst）每天发到指定
邮箱的「OTC组销售明细」xlsx。这个文档说明那份文件怎么进来、哪些行会进 Base、
以及怎么把它挂成每天自动跑。

```
邮箱（Microsoft Graph）
  └─ scripts/import_daily_incremental.py --from-mail   ← 只下载，不改 Base
       └─ 本地解析 + 筛站点 + 算「哪些交易日是新的」
            └─ 增量导入 Daily Revenue Board（先删这些天，再写）
                 └─ 每月 3 号月结按「收入 × 比例」算应付（domain/commission.py）
```

## 只导新加坡站

导出里有香港站、新加坡站、中东站。看板**只要新加坡站**（2026-09-17 定的口径），
其他站点的行在本地解析完就丢掉，不进 Base。

筛的是「站点」列，不是「销售分组」列 —— 新加坡站的记录里也有 HK 组、支付组的销售。
口径判定写在 `domain/schema.py` 的 `BOARD_STATION_IN_SCOPE`，不在脚本里散落。

## 只导「新增交易日」

日常跑的是增量，不是全量。增量 = 导出里有、看板里没有的交易日。

为什么不全量：全量按导出覆盖到的**所有**日期整天替换，一次 1,600+ 行、25+ 次批量
调用；而免费版多维表格有月度调用额度。更麻烦的是全量删完还没写完的时候表是空的，
仪表盘正好读到中间态。

**增量看不见历史修订**（导出改了旧日期的金额，看板不会跟进）。两条补救：

```bash
# 只重导某一天（可重复传 --refresh）
uv run python scripts/import_daily_incremental.py --refresh 2026-09-16

# 大范围回填：全量脚本按日期整天替换，语义更明确
uv run python scripts/import_daily_board.py --file 明细.xlsx
```

新增超过 `--max-days`（默认 5 天）时会**停下来不动 Base**，因为「一次多出很多天」
通常是看板被清空了（第一跑）或指错了文件。确认无误加 `--allow-many-days`。

## 数据来源：邮件

`.env` 里五个键（发件人可留空用默认值）：

```
MICROSOFT_GRAPH_TENANT_ID=
MICROSOFT_GRAPH_CLIENT_ID=
MICROSOFT_GRAPH_CLIENT_SECRET=
MICROSOFT_GRAPH_USER_ID=
GRAPH_SENDER=
```

用的是 **Graph 应用权限**（client credentials），不是用户授权：没有人在旁边点
「同意」，也不该拿某个人的账号当长期凭证。在 Entra 里需要：

1. **应用注册** → 记下 Application (client) ID 和 Directory (tenant) ID；
2. **Certificates & secrets** → 新建 client secret（记下值，只显示一次）；
3. **API permissions** → 添加 `Microsoft Graph` 的**应用程序**权限 `Mail.Read`，
   然后 **Grant admin consent**（这一步要管理员）；
4. **限制可读的邮箱**（强烈建议）：应用权限默认能读**全租户**所有邮箱。
   用 `ApplicationAccessPolicy` 把它限制到那一个邮箱：

   ```powershell
   # Exchange Online PowerShell
   New-ApplicationAccessPolicy -AppId <client_id> `
     -PolicyScopeGroupId <mailbox@your.domain> `
     -AccessRight RestrictAccess `
     -Description "CRM-BaseBot 只读这个邮箱"
   ```

5. `MICROSOFT_GRAPH_USER_ID` 填那个邮箱地址（或它的 Object ID）。

密钥只放本地 `.env`：不要提交、不要贴进聊天或日志。报错信息里只会出现**缺哪个键**，
不会回显值（`tests/test_graph_mail.py` 钉住了这点）。

## 挂成每天自动跑

```bash
./scripts/install-daily-import-launchd.sh     # 安装
./scripts/run-daily-import.sh --dry-run       # 先手工验一次（不写 Base）
tail -f logs/daily-import-stdout.log          # 看跑批输出
```

- 每天 **10:45** 和 **16:00** 各跑一次。跑两次是因为邮件不一定准点，而脚本幂等
  —— 没有新增交易日就一个写请求都不发。
- `WorkingDirectory` 必须是仓库根目录：`Settings` 读的是相对路径 `.env`。
- 回滚：`launchctl bootout "gui/$(id -u)/com.chao.crm-basebot.daily-import"`

**邮箱取件失败时会退回本地目录**（默认 `attachments/`，用 `.env` 的
`DAILY_EXPORT_DIR` 改）。退回这件事会明确写进日志 ——「今天没数据」和「今天没抓到」
必须是两句话。退回也失败（本地目录里也没有导出）时退出码 1，日志里有明确的下一步。

## 日志与产物

| 路径 | 内容 | 入库吗 |
|---|---|---|
| `attachments/` | 抓下来的 xlsx | 不（`*.xlsx` 已在 .gitignore） |
| `logs/daily-import-*.log` | 跑批输出与报错 | 不 |

日志会随天数增长，长期跑请配置轮转（参考 `~/.hermes/scripts/log-rotate.sh` 的做法：
copytruncate，不要 rename —— 日志文件句柄被 launchd 持有着）。

## 手工一次完整的验证

```bash
# 1) 只看这次会导什么，不碰 Base
uv run python scripts/import_daily_incremental.py --from-mail --dry-run

# 2) 真导入
uv run python scripts/import_daily_incremental.py --from-mail

# 3) 核对：看板里这几天的行数应该和导出里新加坡站的行数一致
uv run python scripts/inspect_base.py
```

## 出问题时先看哪里

| 现象 | 先看 |
|---|---|
| 今天的数据没进来 | `logs/daily-import-stdout.log` 里有没有「没有新增交易日」（那是正常的）；`daily-import-stderr.log` 有没有 Graph 报错 |
| 报「缺 MICROSOFT_GRAPH_* 键」 | `.env` 里那四个键没填全 |
| 报「有 N 个 .xlsx 附件」 | 那封邮件里挂了多个附件 —— 不用猜，日志里列了名字 |
| 报「最新一封取不到附件」 | 内部系统这封邮件没带附件，或发件人换了（核对 `GRAPH_SENDER`） |
| 佣金列是空的 | 先确认这个用户有没有登记渠道，见 README 的「为什么有些行是空的」 |
