#!/usr/bin/env python
"""实测 R+3 位自动编号能不能接上现有的 R001–R009。

plan 里留的待验证项。要回答两个问题：

1. 现有的渠道编号列是什么类型？如果已经是自动编号就没事了。
2. 新建的自动编号字段，在已有 N 行的表上会从 R001 开始还是从 R(N+1) 开始？

第 2 问不能靠猜，所以这个脚本在 Base 里建一张**临时表**做真实验证，写 9 行看
编号，再写第 10 行看是不是 R010。临时表和真实数据完全隔离。

    uv run python scripts/verify_numbering.py              # 只检查现状
    uv run python scripts/verify_numbering.py --probe      # 建临时表实测
    uv run python scripts/verify_numbering.py --cleanup    # 删掉临时表
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import importlib.util  # noqa: E402

from lark_oapi.api.bitable.v1 import DeleteAppTableRequest  # noqa: E402

from crm_basebot.config import get_settings  # noqa: E402
from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.referral import parse_referral_no  # noqa: E402
from crm_basebot.lark.bitable import (  # noqa: E402
    FIELD_TYPE_AUTO_NUMBER,
    FIELD_TYPE_TEXT,
    BitableClient,
)
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.lark.field_types import type_name  # noqa: E402
from crm_basebot.lark.values import extract_text  # noqa: E402

PROBE_TABLE_NAME = "ZZ 编号实测（可删）"


def _load_sync_base():
    spec = importlib.util.spec_from_file_location(
        "sync_base", Path(__file__).resolve().parent / "sync_base.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_existing(bitable: BitableClient, table_id: str) -> None:
    fields = {f.name: f for f in bitable.list_fields(table_id)}
    field = fields.get(schema.REFERRAL_NO)

    if field is None:
        print(f"渠道表里没有「{schema.REFERRAL_NO}」字段。")
        print("跑 `uv run python scripts/sync_base.py --apply` 会按自动编号建好。")
        return

    print(f"「{schema.REFERRAL_NO}」当前类型：{type_name(field.type)}")

    if field.type == FIELD_TYPE_AUTO_NUMBER:
        print(f"  规则：{field.props.get('auto_serial')}")
        print("  已经是自动编号，递增由飞书保证，不用管了。")
    elif field.type == FIELD_TYPE_TEXT:
        print("  是文本列。要么转成自动编号（先用 --probe 实测），")
        print("  要么在 ReferralService 里传 auto_number=False 走后端递增。")

    numbers = []
    for record in bitable.iter_records(table_id, field_names=[schema.REFERRAL_NO]):
        parsed = parse_referral_no(extract_text(record.fields.get(schema.REFERRAL_NO)))
        if parsed is not None:
            numbers.append(parsed)

    if not numbers:
        print("  表里还没有编号数据。")
        return

    numbers.sort()
    print(f"  现有 {len(numbers)} 条，范围 R{numbers[0]:03d} – R{numbers[-1]:03d}")

    gaps = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
    if gaps:
        print(f"  有跳号：{', '.join(f'R{n:03d}' for n in gaps)}")
        print("  跳号会让自动编号对不齐（它按行数递增，不认已有的值）。")
    else:
        print("  无跳号，连续。")


def probe(bitable: BitableClient, app_token: str) -> None:
    """建临时表实测自动编号在已有数据上的行为。"""
    sync_base = _load_sync_base()
    client = get_client()

    existing = {t.name: t for t in bitable.list_tables()}
    if PROBE_TABLE_NAME in existing:
        print(f"临时表「{PROBE_TABLE_NAME}」已存在，先跑 --cleanup 删掉再试。")
        return

    print(f"建临时表「{PROBE_TABLE_NAME}」…")
    table_id = sync_base._create_table(client, app_token, PROBE_TABLE_NAME)

    sync_base._create_field(client, app_token, table_id, "备注", FIELD_TYPE_TEXT)
    sync_base._create_field(client, app_token, table_id, schema.REFERRAL_NO, FIELD_TYPE_AUTO_NUMBER)
    print("  已加上 R+3 位自动编号字段")

    print("\n写 9 行，模拟历史数据：")
    produced = []
    for i in range(1, 10):
        created = bitable.create_record(table_id, {"备注": f"历史第 {i} 条"})
        no = extract_text(created.fields.get(schema.REFERRAL_NO))
        produced.append(no)
        print(f"  第 {i} 行 -> {no}")

    print("\n再写第 10 行：")
    created = bitable.create_record(table_id, {"备注": "新增"})
    tenth = extract_text(created.fields.get(schema.REFERRAL_NO))
    print(f"  第 10 行 -> {tenth}")

    print("\n" + "=" * 56)
    expected = [f"R{i:03d}" for i in range(1, 10)]
    if produced == expected and tenth == "R010":
        print("结论：自动编号按写入顺序产出 R001–R009，新增落在 R010。")
        print("      所以把历史数据按时间顺序导入，编号会自然对齐。")
        print("      渠道表可以用自动编号字段，不需要后端递增。")
    else:
        print("结论：行为和预期不符。")
        print(f"      前 9 行拿到 {produced}")
        print(f"      第 10 行拿到 {tenth}")
        print("      走回退方案：ReferralService(auto_number=False)，后端串行递增。")
    print("=" * 56)
    print("\n实测完了记得清理：uv run python scripts/verify_numbering.py --cleanup")


def cleanup(bitable: BitableClient, app_token: str) -> None:
    existing = {t.name: t for t in bitable.list_tables()}
    table = existing.get(PROBE_TABLE_NAME)
    if table is None:
        print(f"没有找到临时表「{PROBE_TABLE_NAME}」，无需清理。")
        return

    request = DeleteAppTableRequest.builder().app_token(app_token).table_id(table.table_id).build()
    response = get_client().bitable.v1.app_table.delete(request)
    if response.success():
        print(f"已删除临时表「{PROBE_TABLE_NAME}」。")
    else:
        print(f"删除失败：{response.code} {response.msg}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实测渠道编号方案")
    parser.add_argument("--probe", action="store_true", help="建临时表做真实验证")
    parser.add_argument("--cleanup", action="store_true", help="删掉临时表")
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.base_app_token:
        print("LARK_BASE_APP_TOKEN 没填，见 docs/LARK_APP_SETUP.md 第 7 步。")
        return 1

    bitable = BitableClient(settings.base_app_token)

    if args.cleanup:
        cleanup(bitable, settings.base_app_token)
        return 0

    if settings.table_referral:
        check_existing(bitable, settings.table_referral)
    else:
        print("TABLE_REFERRAL 没配，跳过现状检查。")

    if args.probe:
        print()
        probe(bitable, settings.base_app_token)
    else:
        print("\n要实测自动编号在已有数据上的行为，加 --probe（会建一张临时表）。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
