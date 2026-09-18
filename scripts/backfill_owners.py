#!/usr/bin/env python
"""把存量渠道 / 客户的归属人按「负责销售」姓名回填成 OpenID。

背景：渠道和客户最初是从模板 xlsx 导进来的，模板里只有销售**姓名**，没有 open_id
（见 import_registrations.py 的「归属人先空着」）。而机器人判归属读的是
``登记人OpenID`` 这一列，所以那批记录对任何销售都不可见 —— 列「我的渠道」是空的，
「登记新客户」的渠道下拉也是空的。

这个脚本把「姓名 → 名册里的 open_id」这一步补上：

    · 只动 ``登记人OpenID`` 为空的行；已经有归属的一律不碰
    · 姓名比对忽略大小写、多余空格、全角空格（模板里有 ``James Yang`` / ``James YANG``
      两种写法，指的是同一个人）
    · 同时写两列：``归属销售``（人员字段，拿它做展示和 At）和 ``登记人OpenID``
      （文本，机器人的归属过滤读它）
    · 姓名对不上名册、或「负责销售」为空的行，**列出来不动** —— 猜错归属等于把钱记到
      别人头上，而且没有任何提示

默认只预演，加 ``--apply`` 才真写。

    uv run python scripts/backfill_owners.py                  # 预演全部
    uv run python scripts/backfill_owners.py --only "James YANG"
    uv run python scripts/backfill_owners.py --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 复用名册的读法与姓名归一：两个脚本对「谁是同一个人」必须用同一套规则。
import set_sales_open_id as roster_lib  # noqa: E402

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.lark.bitable import _WRITE_LOCK, BitableClient  # noqa: E402
from crm_basebot.lark.values import extract_text  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    """一张表要补哪几列。"""

    label: str
    table_setting: str  # Settings 上的属性名
    name_field: str  # 展示用：这条记录叫什么
    sales_name_field: str  # 「负责销售」姓名
    owner_field: str  # 归属销售（人员）
    owner_open_id_field: str  # 登记人OpenID（文本）


TARGETS: tuple[Target, ...] = (
    Target(
        label="渠道",
        table_setting="table_referral",
        name_field=schema.REFERRAL_NO,
        sales_name_field=schema.REFERRAL_SALES_NAME,
        owner_field=schema.REFERRAL_OWNER,
        owner_open_id_field=schema.REFERRAL_OWNER_OPEN_ID,
    ),
    Target(
        label="客户",
        table_setting="table_client",
        name_field=schema.CLIENT_UID,
        sales_name_field=schema.CLIENT_SALES_NAME,
        owner_field=schema.CLIENT_OWNER,
        owner_open_id_field=schema.CLIENT_OWNER_OPEN_ID,
    ),
)


@dataclass(frozen=True)
class Change:
    record_id: str
    title: str
    sales_name: str
    open_id: str


@dataclass
class Plan:
    changes: list[Change]
    already_owned: int  # 已经有 OpenID，跳过
    no_sales_name: int  # 「负责销售」为空
    unmatched: Counter[str]  # 姓名对不上名册的次数


def build_plan(rows: list[tuple[str, str, str, str]], open_ids: dict[str, str]) -> Plan:
    """``rows`` 是 [(record_id, 名称, 负责销售, 现有 OpenID)]。

    ``open_ids`` 是归一姓名 -> open_id。
    """
    changes: list[Change] = []
    already_owned = 0
    no_sales_name = 0
    unmatched: Counter[str] = Counter()

    for record_id, title, sales_name, current_open_id in rows:
        if current_open_id:
            already_owned += 1
            continue
        if not sales_name.strip():
            no_sales_name += 1
            continue
        open_id = open_ids.get(roster_lib.normalize(sales_name))
        if not open_id:
            unmatched[sales_name] += 1
            continue
        changes.append(
            Change(
                record_id=record_id,
                title=title,
                sales_name=sales_name,
                open_id=open_id,
            )
        )
    return Plan(
        changes=changes,
        already_owned=already_owned,
        no_sales_name=no_sales_name,
        unmatched=unmatched,
    )


def read_rows(
    bitable: BitableClient, target: Target, table_id: str
) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for record in bitable.iter_records(
        table_id,
        field_names=[target.name_field, target.sales_name_field, target.owner_open_id_field],
    ):
        rows.append(
            (
                record.record_id,
                extract_text(record.fields.get(target.name_field)),
                extract_text(record.fields.get(target.sales_name_field)),
                extract_text(record.fields.get(target.owner_open_id_field)),
            )
        )
    return rows


def open_id_map(roster: list[tuple[str, str, str]]) -> dict[str, str]:
    """归一姓名 -> open_id。名册里 OpenID 还空着的人不进这个映射（补不了）。"""
    mapping: dict[str, str] = {}
    for _record_id, name, open_id in roster:
        if name.strip() and open_id:
            mapping[roster_lib.normalize(name)] = open_id
    return mapping


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按「负责销售」姓名回填存量渠道/客户的归属 OpenID")
    parser.add_argument("--only", help="只处理「负责销售」等于这个姓名的人（忽略大小写/空格）")
    parser.add_argument("--apply", action="store_true", help="真写；不加则只预演")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(
        settings, "LARK_BASE_APP_TOKEN", "TABLE_REFERRAL", "TABLE_CLIENT", "TABLE_SALES"
    )
    return run(args, settings, BitableClient(settings.base_app_token))


def run(args: argparse.Namespace, settings, bitable: BitableClient) -> int:
    roster = roster_lib.load_roster(bitable, settings.table_sales)
    open_ids = open_id_map(roster)

    print("名册里能对上 OpenID 的人：")
    if not open_ids:
        print("  （一个都没有 —— 先用 scripts/set_sales_open_id.py 把 OpenID 填上）")
    for _record_id, name, open_id in roster:
        if open_id:
            print(f"  {name:<20}{open_id}")
    print()

    only = roster_lib.normalize(args.only) if args.only else None
    total_changes = 0

    for target in TARGETS:
        table_id = getattr(settings, target.table_setting)
        if not table_id:
            print(f"{target.label}：.env 里没配 {target.table_setting.upper()}，跳过")
            continue

        rows = read_rows(bitable, target, table_id)
        if only is not None:
            rows = [row for row in rows if roster_lib.normalize(row[2]) == only]

        plan = build_plan(rows, open_ids)
        print(f"=== {target.label}（共 {len(rows)} 条待看）===")
        print(
            f"  可回填 {len(plan.changes)} 条；已有归属跳过 {plan.already_owned} 条；"
            f"负责销售为空 {plan.no_sales_name} 条"
        )
        if plan.unmatched:
            print("  姓名对不上名册（不动，请人工确认）：")
            for name, n in plan.unmatched.most_common():
                print(f"    {name:<24}{n:>4} 条")
        by_person: Counter[str] = Counter(change.sales_name for change in plan.changes)
        if by_person:
            print("  将回填给：")
            for name, n in by_person.most_common():
                print(f"    {name:<24}{n:>4} 条")

        total_changes += len(plan.changes)

        if not args.apply:
            continue

        written = 0
        for change in plan.changes:
            with _WRITE_LOCK:
                bitable.update_record(
                    table_id,
                    change.record_id,
                    {
                        target.owner_field: [{"id": change.open_id}],
                        target.owner_open_id_field: change.open_id,
                    },
                )
            written += 1
        print(f"  已写入 {written} 条")

    if not args.apply:
        print(f"\n预演：一共会写 {total_changes} 条，Base 没有被改动。确认无误后加 --apply。")
        return 0

    print(f"\n完成：一共写了 {total_changes} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
