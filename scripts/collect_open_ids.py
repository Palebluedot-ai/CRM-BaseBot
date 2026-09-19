"""从机器人日志里把还没登记的 open_id 捞出来 —— 让「收 open_id」这一步变确定。

机器人收到**未登记**的人发来的消息时，会打这样一行：

    WARNING crm_basebot.bot.auth: 未登记的 open_id 尝试操作: ou_d0303d9d76b24774db58c6eef13b6fbc

于是流程是「让每位销售各发一条消息，然后从日志里抄」。抄日志这件事容易出错（抄漏一位、
抄到别人的、抄了两遍），所以这里做三件事：

  1. 把日志里出现过的 open_id 全部捞出来、去重、计数
  2. 和名册对照：已经在名册里的忽略，剩下的才是**新面孔**
  3. 只剩下一个、并且你用 ``--name`` 指名了他是谁时，可以直接写进名册（否则只给命令模板）

**为什么不能猜**：日志只知道「有这么个 open_id 发过消息」，不知道他是谁。谁是谁只有人能对上
（问他本人、看他的飞书资料），所以两个以上候选时脚本只列出候选、不替你选。

用法：

    uv run python scripts/collect_open_ids.py --log logs/bot.log
    uv run python scripts/collect_open_ids.py --log logs/bot.log --name "张三" --apply
    tail -200 logs/bot.log | uv run python scripts/collect_open_ids.py --log -
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)

# 只认这一行里的 open_id：这是 auth.require() 打出来的判据，不是从聊天内容里猜。
UNREGISTERED = re.compile(r"未登记的 open_id 尝试操作[:：]\s*(ou_[A-Za-z0-9_-]+)")


def extract_open_ids(text: str) -> Counter[str]:
    """从日志文本里捞 open_id 并计数（重复出现只算一个候选，但保留次数）。"""
    return Counter(UNREGISTERED.findall(text))


def partition(candidates: Counter[str], known: set[str]) -> tuple[list[str], list[str]]:
    """分成 (已经在名册里的, 新面孔)，都按出现次数从多到少排。"""
    ordered = [open_id for open_id, _ in candidates.most_common()]
    return [o for o in ordered if o in known], [o for o in ordered if o not in known]


def _load_script(name: str):
    path = HERE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_log(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(path)
    return file.read_text(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从机器人日志里捞未登记的 open_id")
    parser.add_argument("--log", default="logs/bot.log", help="机器人日志路径，- 表示从 stdin 读")
    parser.add_argument("--env", default=".env", help="环境文件，默认 .env")
    parser.add_argument("--name", help="新面孔是谁（名册里的姓名），配合 --apply 直接写")
    parser.add_argument("--apply", action="store_true", help="真的写名册；不加则只列出来")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    try:
        text = _read_log(args.log)
    except FileNotFoundError:
        print(
            f"找不到日志：{args.log}\n"
            "  机器人如果是前台跑的，日志在终端里；被 launchd 拉起时通常重定向到 logs/bot.log。\n"
            "  也可以直接把日志喂进来：tail -200 <日志> | "
            "uv run python scripts/collect_open_ids.py --log -",
            file=sys.stderr,
        )
        return 1

    candidates = extract_open_ids(text)
    if not candidates:
        print(
            "日志里没有「未登记的 open_id」记录。\n"
            "  要让这一步凑效：机器人得在跑，且**那位销售确实给机器人发过消息**。\n"
            f"  判据是这一行：未登记的 open_id 尝试操作: ou_xxxx（{args.log}）"
        )
        return 0

    settings = load_settings(env_file=args.env)
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_SALES")
    bitable = BitableClient(settings.base_app_token)
    roster = _load_script("set_sales_open_id")

    # load_roster 返回 [(record_id, 姓名, 现有 OpenID)]
    entries = roster.load_roster(bitable, settings.table_sales)
    known = {row[2] for row in entries if row[2]}
    already, fresh = partition(candidates, known)

    print(f"日志：{args.log}")
    print(f"出现过的 open_id：{len(candidates)} 个")
    if already:
        print(f"\n已经在名册里（跳过）：{len(already)} 个")
        for open_id in already:
            name = next((row[1] for row in entries if row[2] == open_id), "")
            print(f"  {open_id}   {name}   （出现 {candidates[open_id]} 次）")
    if not fresh:
        print("\n没有新面孔 —— 名册是最新的。")
        return 0

    print(f"\n新面孔：{len(fresh)} 个（日志只知道「有人发过消息」，**谁是谁只有人能对上**）")
    for open_id in fresh:
        print(f"  {open_id}   （出现 {candidates[open_id]} 次）")

    if len(fresh) == 1 and args.name:
        open_id = fresh[0]
        print(f"\n只有一个新面孔，且你指名是「{args.name}」——")
        code = roster.run(
            roster.build_parser().parse_args(
                ["--env", args.env, "--name", args.name, "--open-id", open_id]
                + (["--apply"] if args.apply else [])
            ),
            settings,
            bitable,
        )
        if code == 0 and args.apply:
            print("\n名册已更新。接着回填归属：uv run python scripts/backfill_owners.py --apply")
        return code

    print("\n逐个填进名册（把姓名换成实际的人）：")
    for open_id in fresh:
        print("  uv run python scripts/set_sales_open_id.py \\")
        print(f'      --name "某人" --open-id {open_id} --apply')
    if len(fresh) == 1:
        print('\n  只有一个候选时也可以直接：--name "某人" --apply')
    else:
        print("\n  候选不止一个 —— 脚本不替你猜谁是谁；问本人或看飞书资料对上之后再填。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
