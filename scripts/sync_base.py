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

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.lark.bitable import (  # noqa: E402
    FIELD_TYPE_AUTO_NUMBER,
    FIELD_TYPE_SINGLE_LINK,
    BitableClient,
)
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.lark.field_types import type_name  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

# 我们负责维护的表。
TARGET_TABLES: dict[str, dict[str, int]] = {
    schema.TABLE_REFERRAL_NAME: schema.REFERRAL_FIELDS,
    schema.TABLE_CLIENT_NAME: schema.CLIENT_FIELDS,
    schema.TABLE_DAILY_BOARD_NAME: schema.DAILY_BOARD_FIELDS,
    schema.TABLE_COMMISSION_NAME: schema.COMMISSION_FIELDS,
    schema.TABLE_AUDIT_NAME: schema.AUDIT_FIELDS,
    schema.TABLE_SALES_NAME: schema.SALES_FIELDS,
}

# 关联字段指向哪张表。(表名, 字段名) -> 被关联的表名。
# 单向关联（type 18）的 property.table_id 是**必填**的，缺了接口直接拒绝建字段。
# 建表顺序上 Referral 排在 Client 前面，所以轮到建这个字段时目标表一定已经有 id。
LINK_TARGETS: dict[tuple[str, str], str] = {
    (schema.TABLE_CLIENT_NAME, schema.CLIENT_REFERRAL_LINK): schema.TABLE_REFERRAL_NAME,
}


def _build_field(name: str, type_code: int, *, link_table_id: str | None = None) -> AppTableField:
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

    elif type_code == FIELD_TYPE_SINGLE_LINK:
        if not link_table_id:
            # 宁可在本地就停下：少了 table_id 的请求发出去只会得到一句
            # 「字段属性错误」，看不出是哪一环漏了。
            raise ValueError(f"单向关联字段「{name}」缺少被关联表的 table_id，无法建字段")
        builder = builder.property(
            # multiple=False：一个客户只属于一个渠道。默认是 true，
            # 留着 true 的话有人在界面上多挂一个渠道，佣金归属就说不清了。
            AppTableFieldProperty.builder().table_id(link_table_id).multiple(False).build()
        )

    return builder.build()


def _create_table(client: lark.Client, app_token: str, name: str) -> str:
    """只给表名建表，字段随后一个个加。

    待真机确认：只传 name 时平台会给新表塞几个默认字段（主字段「文本」，通常还有
    「单选」「日期」）。它们不影响任何计算 —— 我们所有读写都按字段名来 —— 但会在
    表里留下几列没人填的空列。真跑完看一眼，碍眼的话在 Base 界面上手工删掉即可，
    不要在这里加自动删除逻辑：删字段是不可逆的，脚本不该有这个权力。
    """
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
    table_id = getattr(response.data, "table_id", None)
    if not table_id:
        # 拿不到 table_id 就没法往里加字段，也没法回填 .env。继续往下跑只会得到
        # 一串「table_id 错误」，看不出源头在这里。
        raise SystemExit(f"建表 {name} 返回成功但没给 table_id，请到 Base 里确认表是否已建出来")
    return table_id


def _create_field(
    client: lark.Client,
    app_token: str,
    table_id: str,
    name: str,
    type_code: int,
    *,
    link_table_id: str | None = None,
) -> None:
    request = (
        CreateAppTableFieldRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .request_body(_build_field(name, type_code, link_table_id=link_table_id))
        .build()
    )
    response = client.bitable.v1.app_table_field.create(request)
    if not response.success():
        raise SystemExit(f"加字段 {name} 失败: {response.code} {response.msg}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="幂等对齐 Base 结构")
    parser.add_argument("--apply", action="store_true", help="真的执行，默认只预演")
    args = parser.parse_args(argv)

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN")

    bitable = BitableClient(settings.base_app_token)
    client = get_client()

    existing_tables = {t.name: t for t in bitable.list_tables()}
    plan: list[str] = []
    warnings: list[str] = []

    # 表名 -> table_id。建关联字段要用被关联表的 id，所以边建边记。
    table_ids: dict[str, str] = {name: t.table_id for name, t in existing_tables.items()}

    def link_target_id(table_name: str, field_name: str) -> str | None:
        target = LINK_TARGETS.get((table_name, field_name))
        return table_ids.get(target) if target else None

    for table_name, target_fields in TARGET_TABLES.items():
        table = existing_tables.get(table_name)

        if table is None:
            plan.append(f"建表「{table_name}」并添加 {len(target_fields)} 个字段")
            if args.apply:
                table_id = _create_table(client, settings.base_app_token, table_name)
                table_ids[table_name] = table_id
                print(f"  已建表 {table_name} -> {table_id}")
                for field_name, type_code in target_fields.items():
                    _create_field(
                        client,
                        settings.base_app_token,
                        table_id,
                        field_name,
                        type_code,
                        link_table_id=link_target_id(table_name, field_name),
                    )
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
                        link_table_id=link_target_id(table_name, field_name),
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
