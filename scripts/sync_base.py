#!/usr/bin/env python
"""把 Base 的结构对齐到目标结构。只增，不改，不删。

Base 里已经有同事在用的表，所以这个脚本的安全边界很明确：

  - 缺的表 → 建
  - 缺的字段 → 加
  - 已存在的字段类型不对 → **只报告，不动手**（改类型可能毁数据，人来决定）
  - 多出来的表和字段 → 完全不碰

默认只看不做：

    uv run python scripts/sync_base.py            # 预演，打印将要做什么
    uv run python scripts/sync_base.py --apply    # 真的执行

副产品是迁移能力：以后从测试组织搬到公司组织，换掉 .env 里的凭证跑一次
--apply，结构就复刻过去了，不用手工点。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import lark_oapi as lark  # noqa: E402
from lark_oapi.api.bitable.v1 import (  # noqa: E402
    AppFieldPropertyAutoSerial,
    AppFieldPropertyAutoSerialOptions,
    AppTableField,
    AppTableFieldProperty,
    CreateAppTableFieldRequest,
    CreateAppTableRequest,
    CreateAppTableRequestBody,
    ReqTable,
)

from crm_basebot.config import get_settings  # noqa: E402
from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.lark.bitable import FIELD_TYPE_AUTO_NUMBER, BitableClient  # noqa: E402
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.lark.field_types import type_name  # noqa: E402

# 我们负责维护的表。交易明细不在其中 —— 那是同事的表，我们只读。
TARGET_TABLES: dict[str, dict[str, int]] = {
    schema.TABLE_REFERRAL_NAME: schema.REFERRAL_FIELDS,
    schema.TABLE_CLIENT_NAME: schema.CLIENT_FIELDS,
    schema.TABLE_COMMISSION_NAME: schema.COMMISSION_FIELDS,
    schema.TABLE_AUDIT_NAME: schema.AUDIT_FIELDS,
    schema.TABLE_SALES_NAME: schema.SALES_FIELDS,
}


def _build_field(name: str, type_code: int) -> AppTableField:
    builder = AppTableField.builder().field_name(name).type(type_code)

    if type_code == FIELD_TYPE_AUTO_NUMBER:
        options = [
            AppFieldPropertyAutoSerialOptions.builder()
            .type(option["type"])
            .value(option["value"])
            .build()
            for option in schema.REFERRAL_NO_AUTO_SERIAL["options"]
        ]
        builder = builder.property(
            AppTableFieldProperty.builder()
            .auto_serial(
                AppFieldPropertyAutoSerial.builder()
                .type(schema.REFERRAL_NO_AUTO_SERIAL["type"])
                .options(options)
                .build()
            )
            .build()
        )

    return builder.build()


def _create_table(client: lark.Client, app_token: str, name: str) -> str:
    request = (
        CreateAppTableRequest.builder()
        .app_token(app_token)
        .request_body(
            CreateAppTableRequestBody.builder().table(ReqTable.builder().name(name).build()).build()
        )
        .build()
    )
    response = client.bitable.v1.app_table.create(request)
    if not response.success():
        raise SystemExit(f"建表 {name} 失败: {response.code} {response.msg}")
    return response.data.table_id


def _create_field(
    client: lark.Client, app_token: str, table_id: str, name: str, type_code: int
) -> None:
    request = (
        CreateAppTableFieldRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .request_body(_build_field(name, type_code))
        .build()
    )
    response = client.bitable.v1.app_table_field.create(request)
    if not response.success():
        raise SystemExit(f"加字段 {name} 失败: {response.code} {response.msg}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="幂等对齐 Base 结构")
    parser.add_argument("--apply", action="store_true", help="真的执行，默认只预演")
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.base_app_token:
        print("LARK_BASE_APP_TOKEN 没填，见 docs/LARK_APP_SETUP.md 第 7 步。")
        return 1

    bitable = BitableClient(settings.base_app_token)
    client = get_client()

    existing_tables = {t.name: t for t in bitable.list_tables()}
    plan: list[str] = []
    warnings: list[str] = []

    for table_name, target_fields in TARGET_TABLES.items():
        table = existing_tables.get(table_name)

        if table is None:
            plan.append(f"建表「{table_name}」并添加 {len(target_fields)} 个字段")
            if args.apply:
                table_id = _create_table(client, settings.base_app_token, table_name)
                print(f"  已建表 {table_name} -> {table_id}")
                for field_name, type_code in target_fields.items():
                    _create_field(client, settings.base_app_token, table_id, field_name, type_code)
                    print(f"    + {field_name} ({type_name(type_code)})")
            continue

        current = {f.name: f for f in bitable.list_fields(table.table_id)}

        for field_name, type_code in target_fields.items():
            found = current.get(field_name)
            if found is None:
                plan.append(f"「{table_name}」加字段 {field_name} ({type_name(type_code)})")
                if args.apply:
                    _create_field(
                        client,
                        settings.base_app_token,
                        table.table_id,
                        field_name,
                        type_code,
                    )
                    print(f"  + {table_name}.{field_name}")
            elif found.type != type_code:
                warnings.append(
                    f"「{table_name}」的 {field_name} 现在是 {type_name(found.type)}，"
                    f"目标是 {type_name(type_code)} —— 没有自动改，"
                    "改类型可能毁数据，请你确认后手工调整。"
                )

    if not plan and not warnings:
        print("结构已经对齐，没什么要做的。")
        return 0

    if plan:
        print("\n计划执行：" if args.apply else "\n将要执行（预演）：")
        for item in plan:
            print(f"  · {item}")

    if warnings:
        print("\n需要你决定：")
        for item in warnings:
            print(f"  ! {item}")

    if not args.apply and plan:
        print("\n确认无误后加 --apply 真正执行。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
