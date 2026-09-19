#!/usr/bin/env python
"""把要交给同事的东西打成一个包（编码正确，Windows 解压不乱码）。

    uv run python scripts/pack_handover.py --out out/

包里四样：

    渠道客户.xlsx    渠道 + 客户 + 用户UID（由 export_for_migration.py 生成）
    看板.xlsx        交易明细 18 列
    导入说明.txt     给对方（和他的 agent）的操作步骤
    HANDOFF.html     完整接手手册

## 为什么不用命令行 zip

macOS 自带的 `zip` 不给中文文件名打 UTF-8 标记，Windows 那边解出来是
`µ╕áΘüôσ«óµê╖.xlsx` 这种乱码（实测）。Python 的 zipfile 会正确设置标记，所以打包这一步
也必须由脚本做 —— 一个只有「在别人机器上才暴露」的 bug，正是最该写进脚本的那种。

导出的 xlsx 缺了会提示先跑 export_for_migration.py（不在这里偷偷导出：导出要连源端 Base，
而打包是纯本地动作，不该有网络副作用）。
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# 包内的中文名（同事在飞书里收到时一眼知道是什么）
REGISTRATIONS_IN_ZIP = "渠道客户.xlsx"
BOARD_IN_ZIP = "看板.xlsx"
README_IN_ZIP = "导入说明.txt"
HANDOFF_IN_ZIP = "HANDOFF.html"

# 打包时接受的本名（export_for_migration.py 的输出）
REGISTRATIONS_SOURCES = ("handover.xlsx", "渠道客户.xlsx")
BOARD_SOURCES = ("board.xlsx", "看板.xlsx")

README = """CRM-BaseBot 首次导入说明
================================================================

这个包里是原系统（另一个飞书账号）里的全部数据：

  渠道客户.xlsx   渠道 101 条 · 客户 113 条 · 用户UID 99 条（三个工作表）
  看板.xlsx       交易明细 1,650 行 × 18 列（2026-01-02 ~ 2026-09-18，只含新加坡站）

⚠️ 两个文件都是 xlsx，**不要转成 CSV**：客户 UID 是 18~19 位数字，经 CSV/Excel 转一手
   会被抹掉末尾几位（只保留 15 位有效数字），那种 UID 之后永远算不出佣金，而且不报错。


你要做的事（约十分钟）
----------------------------------------------------------------

第 0 步 准备你的应用
  · https://open.feishu.cn/app → 创建企业自建应用（个人版发不出去，必须是企业号）
  · 「凭证与基础信息」→ 复制 App ID / App Secret
  · 「权限管理」→ 搜「多维表格」→ 开通 bitable:app（读写）
  · 「版本管理与发布」→ 创建版本 → 申请发布（要管理员批准）
    ⚠️ 权限和发版漏任何一个，后面所有接口都 403，且报错看不出是这个原因

第 1 步 准备仓库与配置
  git clone <仓库地址> CRM-BaseBot && cd CRM-BaseBot
  uv sync                       # 没装 uv：curl -LsSf https://astral.sh/uv/install.sh | sh
  cp .env.target.example .env              # 每一项都写了「去哪拿」
  # 填 App ID / App Secret（.env 第 21、22 行）
  # 在飞书里新建一个空 Base，把地址栏 /base/<这一段> 填进 LARK_BASE_APP_TOKEN

第 2 步 导入（先预演，再真导）
  把本包里的两个 xlsx 放到仓库根目录，然后：
  uv run python scripts/import_handover.py --dir . --dry-run
  uv run python scripts/import_handover.py --dir . --apply

  它会自动：建 6 张表（含看板公式列）→ 把 6 个 table_id 写进 .env
           → 导渠道/客户（按「渠道编号」重建关联）→ 导看板（按「客户UID」重建关联）
           → 名册按「负责销售」姓名自动补齐

  ⚠️ 这份数据**只导一次**。重复导，没有 UID 的客户会堆出重复行。

第 3 步 复核
  uv run python scripts/verify_commission.py
  # 期望：合计「复算 = 看板」，结论「逐行一致，没有差异」

  对照数字（导出时刻的真实值）：
    看板 1,650 行 · 渠道 101 条 · 客户 113 条 · 名册 4 人
    佣金合计 复算 = 看板 = 163,175.36 USD


还要人做的两件，以及它们的**前置条件**
----------------------------------------------------------------
先看清顺序：你这边是**全新的应用、全新的 bot**，而 open_id 是**按应用签发**的 ——
在新 bot 跑起来之前，销售发的消息没有任何东西接收，也就拿不到任何 open_id。所以：

  (a) 先把应用侧配齐并**发版**：机器人能力、im:message.p2p_msg:readonly、
      im:message:send_as_bot、订阅 im.message.receive_v1 + 卡片回调 card.action.trigger
      （长连接方式，不需要公网地址）。照 docs/LARK_APP_SETUP.md 的清单配。
  (b) 在本机把机器人跑起来：uv run python -m crm_basebot.app
  (c) 每位销售各给机器人发一条消息，服务端日志会出现他的 open_id：
        WARNING crm_basebot.bot.auth: 未登记的 open_id 尝试操作: ou_xxxx
      填进名册：uv run python scripts/set_sales_open_id.py --name "某人" --open-id ou_xxxx --apply
  (d) 名册填好后回填归属：
        uv run python scripts/backfill_owners.py --dry-run
        uv run python scripts/backfill_owners.py --apply

在 (c)(d) 之前，机器人里「我的渠道」是空的 —— 这不是数据丢失，是归属还没认领。
（原主人那台机器上的机器人服务的是原账号，和你这边没有关系。）


之后的日常（不用再碰这个包）
----------------------------------------------------------------
  · 看板：内部系统每天发到邮箱的 xlsx → scripts/run-daily-import.sh（只导新加坡站新增交易日）
  · 渠道/客户：销售在 Lark 机器人里自己登记、查佣金（docs/SALES_GUIDE.md）
  · 每天自动跑：scripts/install-daily-import-launchd.sh（macOS；Linux 用 cron）


细节文档（都在仓库里）
----------------------------------------------------------------
  HANDOFF.html / HANDOFF.md   接手手册（人看 / 机器看）
  docs/MIGRATION.md           迁移两条路与细节
  docs/PIPELINE.md            每日数据管线
  docs/BOT.md                 机器人现状与已知缺口
  docs/SALES_GUIDE.md         销售怎么用机器人
"""


def _find(directory: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把交接材料打成一个编码正确的 zip")
    parser.add_argument("--out", default="out", help="xlsx 所在目录 / zip 输出目录，默认 out")
    parser.add_argument("--zip", dest="zip_path", help="zip 的完整路径（默认自动命名）")
    parser.add_argument("--no-handoff-html", action="store_true", help="不把 HANDOFF.html 放进包里")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    directory = Path(args.out)

    registrations = _find(directory, REGISTRATIONS_SOURCES)
    board = _find(directory, BOARD_SOURCES)
    if registrations is None or board is None:
        missing = "、".join(
            label
            for label, found in (
                ("渠道客户（handover.xlsx）", registrations),
                ("看板（board.xlsx）", board),
            )
            if found is None
        )
        print(
            f"在 {directory} 里找不到：{missing}\n"
            "  先导出：uv run python scripts/export_for_migration.py "
            "--out out/handover.xlsx --board-out out/board.xlsx",
            file=sys.stderr,
        )
        return 1

    zip_path = (
        Path(args.zip_path)
        if args.zip_path
        else directory / (f"CRM-BaseBot-首次导入-{date.today():%Y%m%d}.zip")
    )
    handoff = ROOT / HANDOFF_IN_ZIP

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(registrations, REGISTRATIONS_IN_ZIP)
        archive.write(board, BOARD_IN_ZIP)
        # 说明文字由脚本生成：它随代码一起改，不会变成一份过期的手抄副本
        archive.writestr(README_IN_ZIP, README)
        if not args.no_handoff_html and handoff.is_file():
            archive.write(handoff, HANDOFF_IN_ZIP)

    print(f"已打包：{zip_path}（{zip_path.stat().st_size // 1024} KB）")
    for info in zipfile.ZipFile(zip_path).infolist():
        print(f"  {info.filename:<20}{info.file_size:>8} 字节")
    print("\n发给同事（走内部渠道，别提交进 git —— 里面有真实客户数据）。")
    print("对方拿到后照包里的「导入说明.txt」做即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
