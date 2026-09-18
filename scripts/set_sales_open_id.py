#!/usr/bin/env python
"""把销售名册（Sales Directory）里某个人的 OpenID 填上。

机器人靠 open_id 认人，而 open_id 只能从飞书事件里拿（详见 docs/BOT.md）。拿到之后
用它填名册，那个人才能使用机器人。

默认只预演，加 ``--apply`` 才真写。

    uv run python scripts/set_sales_open_id.py --name "James YANG" --open-id ou_xxxxxxxx
    uv run python scripts/set_sales_open_id.py --name "James YANG" --open-id ou_xxxxxxxx --apply
    uv run python scripts/set_sales_open_id.py --list          # 看名册现状

为什么用姓名匹配而不是让你抄 record_id：名册是人维护的，姓名在上面；而 record_id 是
接口产物，抄错一个字符就写到了别人身上，且同样静默。姓名匹配唯一的风险是同名/改名，
所以撞名时**拒绝写入并列出来**，由人来消歧。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import extract_text  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)


def normalize(name: str) -> str:
    """姓名归一：去空白、全角空格、大小写不敏感。

    Base 里的名字是人手敲的（``Kevin Yu (于海峰）`` 里还有全角括号），拿原文逐字节比
    会「明明有这个人却找不到」。
    """
    return " ".join(name.replace("\u3000", " ").split()).casefold()


def load_roster(bitable: BitableClient, table_id: str) -> list[tuple[str, str, str]]:
    """返回 [(record_id, 姓名, 现有 OpenID)]。"""
    rows: list[tuple[str, str, str]] = []
    for record in bitable.iter_records(table_id):
        rows.append(
            (
                record.record_id,
                extract_text(record.fields.get(schema.SALES_NAME)),
                extract_text(record.fields.get(schema.SALES_OPEN_ID)),
            )
        )
    return rows


def find_by_name(roster: list[tuple[str, str, str]], name: str) -> list[tuple[str, str, str]]:
    target = normalize(name)
    return [row for row in roster if normalize(row[1]) == target]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把销售名册里的 OpenID 填上")
    parser.add_argument("--name", help='名册里的姓名，例如 "James YANG"')
    parser.add_argument("--open-id", dest="open_id", help="要写入的 open_id，形如 ou_xxxxxxxx")
    parser.add_argument("--list", action="store_true", help="只列出名册现状")
    parser.add_argument("--apply", action="store_true", help="真写；不加则只预演")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_SALES")
    return run(args, settings, BitableClient(settings.base_app_token))


def run(args: argparse.Namespace, settings, bitable: BitableClient) -> int:
    """入口主体。settings / bitable 从外面传，测试里换成假件就能整条路走一遍。"""
    roster = load_roster(bitable, settings.table_sales)

    print(f"名册共 {len(roster)} 人：")
    for record_id, name, open_id in roster:
        print(f"  {name or '(无名)':<20}{open_id or '(OpenID 为空)':<26}{record_id}")

    if args.list:
        return 0

    if not args.name or not args.open_id:
        print("\n要写就同时给 --name 和 --open-id；只看看现状用 --list。", file=sys.stderr)
        return 1

    open_id = args.open_id.strip()
    if not open_id.startswith("ou_") or " " in open_id:
        print(
            f"open_id 形状不对：{open_id!r}。飞书的 open_id 形如 ou_xxxxxxxxxxxx，"
            "不含空格。别把 user_id（u_ 开头）或 union_id（on_ 开头）填进来。",
            file=sys.stderr,
        )
        return 1

    matches = find_by_name(roster, args.name)
    if not matches:
        print(f"\n名册里没有叫「{args.name}」的人。上面列出了现有的名字。", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"\n名册里有 {len(matches)} 个叫「{args.name}」的人，不敢猜是哪个：", file=sys.stderr)
        for record_id, name, open_id_now in matches:
            print(f"  {name}  {record_id}  {open_id_now or '(空)'}", file=sys.stderr)
        print("  请先在 Base 里把重名改开，或直接改这个脚本的匹配方式。", file=sys.stderr)
        return 1

    record_id, name, current = matches[0]
    if current == open_id:
        print(f"\n{name} 的 OpenID 已经是这个值，不用改。")
        return 0

    print(f"\n将把 {name} 的 OpenID：")
    print(f"  现在：{current or '(空)'}")
    print(f"  改成：{open_id}")

    if not args.apply:
        print("\n预演：没有写 Base。确认无误后加 --apply。")
        return 0

    bitable.update_record(settings.table_sales, record_id, {schema.SALES_OPEN_ID: open_id})
    # 回读确认：写接口返回成功不等于值进去了（只读字段、类型不符等都会静默丢弃）。
    after = extract_text(
        bitable.get_record(settings.table_sales, record_id).fields.get(schema.SALES_OPEN_ID)
    )
    if after != open_id:
        print(f"\n写回读到的值不对（{after!r}），请手工核对名册。", file=sys.stderr)
        return 1
    print(f"\n已写入并回读确认：{name} -> {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
