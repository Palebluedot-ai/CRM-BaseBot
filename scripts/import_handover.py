#!/usr/bin/env python
"""第一次把数据搬进一个新 Base —— 一条命令，按顺序做完四步。

    uv run python scripts/import_handover.py --dir . --dry-run    # 先看会写什么
    uv run python scripts/import_handover.py --dir . --apply      # 真导

`--dir` 里要有原主人给你的两个 xlsx（默认按这些名字找）：

    渠道客户.xlsx   （或 handover.xlsx）  渠道 + 客户 + 用户UID 三个工作表
    看板.xlsx       （或 board.xlsx）     交易明细，18 列

它做四件事：

    1. 结构     缺的表建出来、缺的列加上（含看板那几列的公式），并把 6 个 table_id 写回 .env
    2. 渠道+客户 按「渠道编号」重建「所属渠道」关联；名册按「负责销售」姓名自动补齐
    3. 看板     按「客户UID」重建客户关联，整天替换
    4. 汇总     打印各步结果和「接下来还要人做什么」

## 为什么是「编排」而不是重写一遍

导入是一件已经有答案的事：``import_registrations.py`` 管渠道/客户（含 UID 防精度处理），
``import_daily_board.py`` 管看板（含整天替换和关联重建），两者都有 ``run(args, settings,
bitable)`` 这个注入点。这里只负责**顺序和参数**，导入逻辑一行都不重复 —— 否则两套实现
迟早会不一致，而且是「偷偷不一致」那种。

## 用完之后

第一次导完，之后的日常不用再碰这个脚本：看板靠每天邮箱里那份 xlsx 增量跟进
（``scripts/run-daily-import.sh``），渠道和客户靠 Lark 机器人登记
（``docs/SALES_GUIDE.md``）。
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402
from crm_basebot.structure import ensure_structure, write_table_ids  # noqa: E402

logger = logging.getLogger(__name__)

# 原主人导出的默认文件名的候选。两边文件名不一样也能跑（--registrations / --board 覆盖）。
REGISTRATIONS_NAMES = ("渠道客户.xlsx", "handover.xlsx")
BOARD_NAMES = ("看板.xlsx", "board.xlsx")


def _load_script(name: str):
    """按路径加载 scripts/ 里的同级脚本（它们不是包）。

    这样复用比「复制一份逻辑」安全：UID 的精度处理、整天替换的边界，都只有一处实现。
    """
    path = HERE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def find_file(directory: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def board_argv(board: Path, *, apply: bool) -> list[str]:
    """给 import_daily_board 的命令行参数。

    **它的开关方向和前两步相反**：看板脚本默认写、用 ``--dry-run`` 预演，而
    ``sync_base`` 和 ``import_registrations`` 默认预演、用 ``--apply`` 才写。
    照搬前两步的写法会两头都错 —— ``--apply`` 会让看板的 argparse 以
    「unrecognized arguments」当场退出，而预演时什么都不传，等于让「预演」
    把看板真写进 Base。所以这一步单独翻译一次，并且由测试钉住。
    """
    return ["--file", str(board)] + ([] if apply else ["--dry-run"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="第一次把导出的 xlsx 全部导进目标 Base")
    parser.add_argument("--dir", default=".", help="放那两个 xlsx 的目录，默认当前目录")
    parser.add_argument("--registrations", help="渠道客户 xlsx 的路径（默认按名字找）")
    parser.add_argument("--board", help="看板 xlsx 的路径（默认按名字找）")
    parser.add_argument("--env", default=".env", help="环境文件，默认 .env（table_id 会写回它）")
    parser.add_argument("--apply", action="store_true", help="真的写；不加则只预演（默认）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只预演（这就是默认行为，写着读起来更明确）",
    )
    parser.add_argument(
        "--skip-structure",
        action="store_true",
        help="跳过建表（结构已经对齐好、或没权限建表时用）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if args.apply and args.dry_run:
        print("--apply 和 --dry-run 一起给没有意义：要么预演，要么真导。", file=sys.stderr)
        return 2

    settings = load_settings(env_file=args.env)
    require_settings(settings, "LARK_BASE_APP_TOKEN")

    directory = Path(args.dir)
    registrations = (
        Path(args.registrations)
        if args.registrations
        else find_file(directory, REGISTRATIONS_NAMES)
    )
    board = Path(args.board) if args.board else find_file(directory, BOARD_NAMES)

    missing = [
        label for label, found in (("渠道客户", registrations), ("看板", board)) if found is None
    ]
    if missing:
        seen = "、".join(sorted(p.name for p in directory.glob("*.xlsx"))) or "（一个 xlsx 都没有）"
        print(
            f"\n在 {directory} 里找不到：{'、'.join(missing)} 的 xlsx。\n"
            f"  期望文件名：{' 或 '.join(REGISTRATIONS_NAMES)} / {' 或 '.join(BOARD_NAMES)}\n"
            f"  也可以显式指定：--registrations <路径> --board <路径>\n"
            f"  这个目录里现有的 xlsx：{seen}",
            file=sys.stderr,
        )
        return 1

    print(f"渠道客户：{registrations}")
    print(f"看板：    {board}")
    print(f"环境：    {args.env}（{'真导' if args.apply else '预演'}）\n")

    bitable = BitableClient(settings.base_app_token)

    # ① 结构（含公式列）—— 导入脚本要认得那些列，必须先建出来
    if args.skip_structure:
        print("跳过建表（--skip-structure）。\n")
    else:
        result = ensure_structure(
            settings=settings, bitable=bitable, client=get_client(), apply=args.apply
        )
        if args.apply:
            written = write_table_ids(args.env, result.table_ids)
            built = (
                f"建了 {result.built_tables} 张表、{result.added_fields} 个字段"
                if result.changed
                else "本来就对齐"
            )
            print(f"结构：{built}；table_id 已写回 {args.env}：{len(written)} 个")
        else:
            print(
                f"结构：{'要建 ' + str(len(result.plan)) + ' 项' if result.changed else '已对齐'}"
            )
        for warning in result.warnings:
            print(f"  ! {warning}")

    # ② 渠道 + 客户（含名册按姓名补齐）
    registrations_module = _load_script("import_registrations")
    reg_args = registrations_module.build_parser().parse_args(
        ["--file", str(registrations)] + (["--apply"] if args.apply else [])
    )
    if registrations_module.run(reg_args, settings, bitable) != 0:
        print("\n渠道/客户这一步没成功，先停下（看板依赖客户关联）。", file=sys.stderr)
        return 1

    # ③ 看板（按 UID 重建客户关联）
    board_module = _load_script("import_daily_board")
    board_args = board_module.build_parser().parse_args(board_argv(board, apply=args.apply))
    if board_module.run(board_args, settings, bitable) != 0:
        print("\n看板这一步没成功。", file=sys.stderr)
        return 1

    # ④ 汇总
    if args.apply:
        print("\n=== 接下来还要人做的 ===")
        print("  ① 先把**新 bot** 跑起来（你这边是全新应用；open_id 由它签发）：")
        print("     应用侧配齐机器人能力 + 订阅 im.message.receive_v1 + 卡片回调并**发版**，")
        print("     然后 uv run python -m crm_basebot.app")
        print("  ② 名册里的 OpenID 还是空的（open_id 按应用签发，跨账号无效）：")
        print("     让每位销售各给机器人发一条消息，日志里会出现 ou_xxxx，然后：")
        print(
            '     uv run python scripts/set_sales_open_id.py --name "某人" '
            "--open-id ou_xxxx --apply"
        )
        print("  ③ 名册填好之后回填渠道/客户的归属：")
        print("     uv run python scripts/backfill_owners.py --apply")
        print("\n=== 复核 ===")
        print("  uv run python scripts/verify_commission.py    # 期望：逐行一致，没有差异")
    else:
        print("\n预演结束 —— 什么都没写。确认无误后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
