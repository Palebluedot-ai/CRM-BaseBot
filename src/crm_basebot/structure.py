"""把 Base 的结构对齐到目标结构。只增，不删；不改（我们维护的公式列除外）。

Base 里已经有同事在用的表，所以安全边界很明确：

  - 缺的表 → 建
  - 缺的字段 → 加
  - 已存在的字段类型不对 → **只报告，不动手**（改类型可能毁数据，人来决定）
  - 我们维护的公式列，公式和这里的不一样 → 换成这里的（值是算出来的，换了不丢数据）
  - 多出来的表和字段 → 完全不碰

看板表除了 xlsx 里那 18 列，还会建「渠道反查列」：一个单向关联 + 几个公式
（渠道、比例、月份、AI 门槛、本笔佣金），定义在
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
from pathlib import Path
from typing import Any

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppFieldPropertyAutoSerial,
    AppFieldPropertyAutoSerialOptions,
    AppTableField,
    AppTableFieldProperty,
    AppTableFieldPropertyOption,
    AppTableFieldPropertyType,
    CreateAppTableFieldRequest,
    CreateAppTableRequest,
    CreateAppTableRequestBody,
    ReqTable,
    UpdateAppTableFieldRequest,
)

from .domain import ecas, schema
from .lark.bitable import (
    FIELD_TYPE_AUTO_NUMBER,
    FIELD_TYPE_FORMULA,
    FIELD_TYPE_NUMBER,
    FIELD_TYPE_SINGLE_LINK,
    FIELD_TYPE_SINGLE_SELECT,
    BitableClient,
)
from .lark.field_types import type_name
from .startup import set_env_value

# 我们负责维护的表。
TARGET_TABLES: dict[str, dict[str, int]] = {
    schema.TABLE_REFERRAL_NAME: schema.REFERRAL_FIELDS,
    # 客户表 = 登记要填的列 + AI 门槛公式列（放最后，公式要引用前面的 AI 两列）。
    schema.TABLE_CLIENT_NAME: {**schema.CLIENT_FIELDS, **schema.CLIENT_DERIVED_FIELDS},
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
        return sum(1 for item in self.plan if "加字段" in item)

    @property
    def changed(self) -> bool:
        return bool(self.plan)


# ---------- 数字的显示格式 ----------

# 平台「数字」「公式」列的显示格式（property.formatter）。仪表盘上的图沿用列的格式，
# 图表设置里没有地方改 —— 要 20,000 而不是 20000.0，只能把列设好（2026-09-29 反馈）。
FORMAT_MONEY = "1,000.00"
FORMAT_COUNT = "1,000"

_BOARD_MONEY = (
    schema.BOARD_SPOT_FEE_EX_MM,
    schema.BOARD_SPOT_VOLUME_EX_MM,
    schema.BOARD_CONTRACT_FEE_EX_MM,
    schema.BOARD_CONTRACT_VOLUME_EX_MM,
    schema.BOARD_OPT_FEE,
    schema.BOARD_OPT_PNL,
    schema.BOARD_OPT_REVENUE,
    schema.BOARD_OPT_VOLUME,
    schema.BOARD_TOTAL_REVENUE,
    schema.BOARD_TOTAL_VOLUME,
    schema.BOARD_ROW_COMMISSION,
)

# (表名, 字段名) -> 格式。比例列（20 表示 20%）和 AI 门槛（20260901 这种日期数）**不在这里**：
# 加千分位会变成 20,260,901，看着像钱。
NUMBER_FORMATS: dict[tuple[str, str], str] = {
    **{(schema.TABLE_DAILY_BOARD_NAME, name): FORMAT_MONEY for name in _BOARD_MONEY},
    (schema.TABLE_COMMISSION_NAME, schema.COMM_CLIENT_COUNT): FORMAT_COUNT,
    (schema.TABLE_COMMISSION_NAME, schema.COMM_TXN_COUNT): FORMAT_COUNT,
    (schema.TABLE_COMMISSION_NAME, schema.COMM_REVENUE_TOTAL): FORMAT_MONEY,
    (schema.TABLE_COMMISSION_NAME, schema.COMM_PAYABLE): FORMAT_MONEY,
    (ecas.TABLE_ECAS_NAME, ecas.ECAS_AMOUNT): FORMAT_MONEY,
    (ecas.TABLE_ECAS_NAME, ecas.ECAS_FEE): FORMAT_MONEY,
    (ecas.TABLE_ECAS_COMMISSION_NAME, ecas.ECOMM_CLIENT_COUNT): FORMAT_COUNT,
    (ecas.TABLE_ECAS_COMMISSION_NAME, ecas.ECOMM_TXN_COUNT): FORMAT_COUNT,
    (ecas.TABLE_ECAS_COMMISSION_NAME, ecas.ECOMM_AMOUNT_TOTAL): FORMAT_MONEY,
    (ecas.TABLE_ECAS_COMMISSION_NAME, ecas.ECOMM_PAYABLE): FORMAT_MONEY,
}


def build_field(
    name: str,
    type_code: int,
    *,
    link_table_id: str | None = None,
    formula: tuple[str, int] | None = None,
    formatter: str | None = None,
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

    elif type_code == FIELD_TYPE_SINGLE_SELECT and name in schema.SINGLE_SELECT_OPTIONS:
        # 选项在建列时就定好，界面上直接点选；不预先建的话只能靠第一次写入时自动长出来，
        # 在那之前手工改的人面对的是一个空下拉。
        builder = builder.property(
            AppTableFieldProperty.builder()
            .options(
                [
                    AppTableFieldPropertyOption.builder().name(option).build()
                    for option in schema.SINGLE_SELECT_OPTIONS[name]
                ]
            )
            .build()
        )

    elif type_code == FIELD_TYPE_FORMULA:
        if formula is None:
            raise ValueError(f"公式字段「{name}」缺少表达式，无法建字段")
        expression, data_type = formula
        prop = (
            AppTableFieldProperty.builder()
            .formula_expression(expression)
            # formula_type=2 的多维表格必须带返回类型，不带接口直接报错（实测）。
            .type(AppTableFieldPropertyType.builder().data_type(data_type).build())
        )
        # 改公式时整个 property 被替换：不带上格式，手动或 sync 设好的千分位会被洗掉。
        if formatter:
            prop = prop.formatter(formatter)
        builder = builder.property(prop.build())

    elif type_code == FIELD_TYPE_NUMBER and formatter:
        builder = builder.property(AppTableFieldProperty.builder().formatter(formatter).build())

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
    formatter: str | None = None,
) -> None:
    request = (
        CreateAppTableFieldRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .request_body(
            build_field(
                name,
                type_code,
                link_table_id=link_table_id,
                formula=formula,
                formatter=formatter,
            )
        )
        .build()
    )
    response = client.bitable.v1.app_table_field.create(request)
    if not response.success():
        raise StructureError(f"加字段 {name} 失败: {response.code} {response.msg}")


# (表名, 字段名) -> (表达式, 返回类型)。我们维护的公式列就这些。
FORMULAS: dict[tuple[str, str], tuple[str, int]] = {
    **{
        (schema.TABLE_DAILY_BOARD_NAME, name): formula
        for name, formula in schema.DAILY_BOARD_DERIVED_FORMULAS.items()
    },
    **{
        (schema.TABLE_CLIENT_NAME, name): formula
        for name, formula in schema.CLIENT_DERIVED_FORMULAS.items()
    },
}


def _formula_for(table_name: str, field_name: str) -> tuple[str, int] | None:
    """公式字段的 (表达式, 返回类型)。不是我们维护的公式列返回 None。"""
    return FORMULAS.get((table_name, field_name))


def _same_expression(current: str, wanted: str) -> bool:
    """空格不算差别：平台读回来的表达式可能改过空格。"""
    return "".join(current.split()) == "".join(wanted.split())


def update_formula(
    client: lark.Client,
    app_token: str,
    table_id: str,
    field_id: str,
    name: str,
    formula: tuple[str, int],
    formatter: str | None = None,
) -> None:
    """把一列已有的公式换成新的。公式列的值是算出来的，换公式不丢任何数据。"""
    request = (
        UpdateAppTableFieldRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .field_id(field_id)
        .request_body(build_field(name, FIELD_TYPE_FORMULA, formula=formula, formatter=formatter))
        .build()
    )
    response = client.bitable.v1.app_table_field.update(request)
    if not response.success():
        raise StructureError(
            f"改公式 {name} 失败: {response.code} {response.msg}。"
            f"可以在 Base 里把「{name}」这一列删掉，再跑一次 sync_base.py --apply 让它重建。"
        )


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
                        formatter=NUMBER_FORMATS.get((table_name, field_name)),
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
                        formatter=NUMBER_FORMATS.get((table_name, field_name)),
                    )
            elif found.type == type_code == FIELD_TYPE_FORMULA and (
                wanted := _formula_for(table_name, field_name)
            ):
                # 公式列的值是算出来的，换公式不会毁数据 —— 所以这一种「改」是允许的。
                # 规则变了（例如 2026-09-26 本笔佣金加上 AI 规则）要靠这里把老表跟上。
                expression_now = str((found.props or {}).get("formula_expression") or "")
                if not _same_expression(expression_now, wanted[0]):
                    result.plan.append(f"「{table_name}」改公式 {field_name}")
                    if apply:
                        update_formula(
                            client,
                            settings.base_app_token,
                            table_id,
                            found.field_id,
                            field_name,
                            wanted,
                            formatter=NUMBER_FORMATS.get((table_name, field_name)),
                        )
            elif found.type != type_code:
                result.warnings.append(
                    f"「{table_name}」的 {field_name} 现在是 {type_name(found.type)}，"
                    f"目标是 {type_name(type_code)} —— 没有自动改，"
                    "改类型可能毁数据，请你确认后手工调整。"
                )

    return result


def ensure_number_formats(
    *,
    settings: Any,
    bitable: BitableClient,
    client: lark.Client,
    apply: bool,
) -> StructureResult:
    """把 ``NUMBER_FORMATS`` 里的列设成对应的显示格式。只改格式，不碰数据、不改公式。

    ECAS 两张表不归 ``ensure_structure`` 建（``scripts/import_ecas.py`` 建的），所以这里
    按表名自己找；没有的表、没有的列、类型不是数字或公式的列都跳过。某一列改不成
    只记一条警告，不让整个 sync 停下 —— 格式是好看，不是对不对。
    """
    result = StructureResult()
    tables = {table.name: table.table_id for table in bitable.list_tables()}
    wanted_tables = sorted({table for table, _ in NUMBER_FORMATS})
    for table_name in wanted_tables:
        table_id = tables.get(table_name)
        if not table_id:
            continue
        for found in bitable.list_fields(table_id):
            wanted = NUMBER_FORMATS.get((table_name, found.name))
            if not wanted or found.type not in (FIELD_TYPE_NUMBER, FIELD_TYPE_FORMULA):
                continue
            props = found.props or {}
            if props.get("formatter") == wanted:
                continue
            formula: tuple[str, int] | None = None
            if found.type == FIELD_TYPE_FORMULA:
                expression = str(props.get("formula_expression") or "")
                data_type = (props.get("type") or {}).get("data_type")
                if not expression or data_type is None:
                    result.warnings.append(
                        f"「{table_name}」的 {found.name} 读不到公式，没改格式，可在界面上手动设。"
                    )
                    continue
                # 用 Base 里现在的公式原样写回：这里只改格式，公式由 ensure_structure 管。
                formula = (expression, int(data_type))
            result.plan.append(f"「{table_name}」{found.name} 显示成 {wanted}")
            if not apply:
                continue
            request = (
                UpdateAppTableFieldRequest.builder()
                .app_token(settings.base_app_token)
                .table_id(table_id)
                .field_id(found.field_id)
                .request_body(
                    build_field(found.name, found.type, formula=formula, formatter=wanted)
                )
                .build()
            )
            response = client.bitable.v1.app_table_field.update(request)
            if not response.success():
                result.warnings.append(
                    f"「{table_name}」{found.name} 改格式失败：{response.code} {response.msg}"
                    "（数据没受影响，可以在界面上手动设）"
                )
    return result
