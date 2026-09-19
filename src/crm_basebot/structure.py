"""把 Base 的结构对齐到目标结构。只增，不改，不删。

Base 里已经有同事在用的表，所以安全边界很明确：

  - 缺的表 → 建
  - 缺的字段 → 加
  - 已存在的字段类型不对 → **只报告，不动手**（改类型可能毁数据，人来决定）
  - 多出来的表和字段 → 完全不碰

看板表除了 xlsx 里那 18 列，还会建「渠道反查列」：一个单向关联 + 五个公式，定义在
``schema.DAILY_BOARD_DERIVED_FIELDS`` / ``DAILY_BOARD_DERIVED_FORMULAS``。
**平台不校验公式表达式** —— 写错的公式照样建得出来，只是永远返回空值，所以调用方
（``scripts/sync_base.py``）建完要拿真实记录做一次公式自检。

这个模块被两处使用，所以必须是可复用的函数而不是脚本：

  · ``scripts/sync_base.py``  对齐当前 .env 指向的那个 Base
  · ``migration/``            搬迁时给**目标** Base 建同样的结构

它只碰结构，不搬数据 —— 数据在 ``migration/copy.py`` 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppFieldPropertyAutoSerial,
    AppFieldPropertyAutoSerialOptions,
    AppTableField,
    AppTableFieldProperty,
    AppTableFieldPropertyType,
    CreateAppTableFieldRequest,
    CreateAppTableRequest,
    CreateAppTableRequestBody,
    ReqTable,
)

from pathlib import Path

from .domain import schema
from .lark.bitable import (
    FIELD_TYPE_AUTO_NUMBER,
    FIELD_TYPE_FORMULA,
    FIELD_TYPE_SINGLE_LINK,
    BitableClient,
)
from .lark.field_types import type_name
from .startup import set_env_value

# 我们负责维护的表。
TARGET_TABLES: dict[str, dict[str, int]] = {
    schema.TABLE_REFERRAL_NAME: schema.REFERRAL_FIELDS,
    schema.TABLE_CLIENT_NAME: schema.CLIENT_FIELDS,
    # 看板 = xlsx 里那 18 列（导入的合同）+ 渠道反查列（关联和公式，导入不碰）。
    schema.TABLE_DAILY_BOARD_NAME: {
        **schema.DAILY_BOARD_FIELDS,
        **schema.DAILY_BOARD_DERIVED_FIELDS,
    },
    schema.TABLE_COMMISSION_NAME: schema.COMMISSION_FIELDS,
    schema.TABLE_AUDIT_NAME: schema.AUDIT_FIELDS,
    schema.TABLE_SALES_NAME: schema.SALES_FIELDS,
}

# 关联字段指向哪张表。(表名, 字段名) -> 被关联的表名。
# 单向关联（type 18）的 property.table_id 是**必填**的，缺了接口直接拒绝建字段。
# 建表顺序上 Referral 排在 Client 前面，所以轮到建这个字段时目标表一定已经有 id。
LINK_TARGETS: dict[tuple[str, str], str] = {
    (schema.TABLE_CLIENT_NAME, schema.CLIENT_REFERRAL_LINK): schema.TABLE_REFERRAL_NAME,
    (schema.TABLE_DAILY_BOARD_NAME, schema.BOARD_CLIENT_LINK): schema.TABLE_CLIENT_NAME,
}


# 表名 -> .env 里的键名。建完表把 id 写回去用，别让人手抄（手抄错一位的报错完全看不出因果）。
TABLE_ENV_KEYS: dict[str, str] = {
    schema.TABLE_REFERRAL_NAME: "TABLE_REFERRAL",
    schema.TABLE_CLIENT_NAME: "TABLE_CLIENT",
    schema.TABLE_DAILY_BOARD_NAME: "TABLE_DAILY_BOARD",
    schema.TABLE_COMMISSION_NAME: "TABLE_COMMISSION",
    schema.TABLE_AUDIT_NAME: "TABLE_AUDIT",
    schema.TABLE_SALES_NAME: "TABLE_SALES",
}


def write_table_ids(env_path: Path | str, table_ids: dict[str, str]) -> list[str]:
    """把表名 -> id 写进环境文件，返回写过的键名。

    调用时机是「结构刚对齐好、id 就在手上」—— 这时候让人去界面里一个个抄 id 是没必要的
    摩擦，而抄错的后果（404「table not found」）还完全指不到真正的错处。
    """
    written: list[str] = []
    for table_name, key in TABLE_ENV_KEYS.items():
        table_id = table_ids.get(table_name)
        if table_id:
            set_env_value(env_path, key, table_id)
            written.append(key)
    return written


class StructureError(RuntimeError):
    """建表或建字段失败。消息可以直接打印给人看。"""


@dataclass
class StructureResult:
    """对齐结果。``table_ids`` 是**建完之后的**表名 -> id 映射（含本来就有的表）。"""

    table_ids: dict[str, str] = field(default_factory=dict)
    plan: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def built_tables(self) -> int:
        return sum(1 for item in self.plan if item.startswith("建表"))

    @property
    def added_fields(self) -> int:
        return len(self.plan) - self.built_tables

    @property
    def changed(self) -> bool:
        return bool(self.plan)


def build_field(
    name: str,
    type_code: int,
    *,
    link_table_id: str | None = None,
    formula: tuple[str, int] | None = None,
) -> AppTableField:
    """按 schema 里的类型码造一个字段定义（关联要 table_id，公式要表达式+返回类型）。"""
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

    elif type_code == FIELD_TYPE_FORMULA:
        if formula is None:
            raise ValueError(f"公式字段「{name}」缺少表达式，无法建字段")
        expression, data_type = formula
        builder = builder.property(
            AppTableFieldProperty.builder()
            .formula_expression(expression)
            # formula_type=2 的多维表格必须带返回类型，不带接口直接报错（实测）。
            .type(AppTableFieldPropertyType.builder().data_type(data_type).build())
            .build()
        )

    return builder.build()


def create_table(client: lark.Client, app_token: str, name: str) -> str:
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
        raise StructureError(f"建表 {name} 失败: {response.code} {response.msg}")
    table_id = getattr(response.data, "table_id", None)
    if not table_id:
        # 拿不到 table_id 就没法往里加字段，也没法回填 .env。继续往下跑只会得到
        # 一串「table_id 错误」，看不出源头在这里。
        raise StructureError(f"建表 {name} 返回成功但没给 table_id，请到 Base 里确认表是否已建出来")
    return table_id


def create_field(
    client: lark.Client,
    app_token: str,
    table_id: str,
    name: str,
    type_code: int,
    *,
    link_table_id: str | None = None,
    formula: tuple[str, int] | None = None,
) -> None:
    request = (
        CreateAppTableFieldRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .request_body(build_field(name, type_code, link_table_id=link_table_id, formula=formula))
        .build()
    )
    response = client.bitable.v1.app_table_field.create(request)
    if not response.success():
        raise StructureError(f"加字段 {name} 失败: {response.code} {response.msg}")


def _formula_for(table_name: str, field_name: str) -> tuple[str, int] | None:
    """公式字段的 (表达式, 返回类型)。只有看板的反查列是公式，其余返回 None。"""
    if table_name != schema.TABLE_DAILY_BOARD_NAME:
        return None
    return schema.DAILY_BOARD_DERIVED_FORMULAS.get(field_name)


def ensure_structure(
    *,
    settings: Any,
    bitable: BitableClient,
    client: lark.Client,
    apply: bool,
) -> StructureResult:
    """把 ``settings`` 指向的 Base 对齐到 TARGET_TABLES。``apply=False`` 时只预演。

    幂等：反复跑不会有额外动作。返回的 ``table_ids`` 是**建完之后**的表名 -> id
    映射，调用方（迁移）拿它继续干活，不用再查一次。
    """
    existing = {table.name: table.table_id for table in bitable.list_tables()}
    result = StructureResult(table_ids=dict(existing))

    def link_target_id(table_name: str, field_name: str) -> str | None:
        target = LINK_TARGETS.get((table_name, field_name))
        return result.table_ids.get(target) if target else None

    for table_name, target_fields in TARGET_TABLES.items():
        table_id = existing.get(table_name)

        if table_id is None:
            result.plan.append(f"建表「{table_name}」并添加 {len(target_fields)} 个字段")
            if apply:
                table_id = create_table(client, settings.base_app_token, table_name)
                result.table_ids[table_name] = table_id
                for field_name, type_code in target_fields.items():
                    create_field(
                        client,
                        settings.base_app_token,
                        table_id,
                        field_name,
                        type_code,
                        link_table_id=link_target_id(table_name, field_name),
                        formula=_formula_for(table_name, field_name),
                    )
            continue

        current = {f.name: f for f in bitable.list_fields(table_id)}

        for field_name, type_code in target_fields.items():
            found = current.get(field_name)
            if found is None:
                result.plan.append(f"「{table_name}」加字段 {field_name} ({type_name(type_code)})")
                if apply:
                    create_field(
                        client,
                        settings.base_app_token,
                        table_id,
                        field_name,
                        type_code,
                        link_table_id=link_target_id(table_name, field_name),
                        formula=_formula_for(table_name, field_name),
                    )
            elif found.type != type_code:
                result.warnings.append(
                    f"「{table_name}」的 {field_name} 现在是 {type_name(found.type)}，"
                    f"目标是 {type_name(type_code)} —— 没有自动改，"
                    "改类型可能毁数据，请你确认后手工调整。"
                )

    return result
