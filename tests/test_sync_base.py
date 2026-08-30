"""建字段请求体的形状。

``sync_base.py --apply`` 是接真实 API 的第一步，它建错一个字段，后面所有事都做
不下去。不同字段类型对 ``property`` 的要求差别很大，而这些要求在本地一行代码都
测不到 —— 除非把请求体拆开看。这里就是拆开看。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import lark_oapi as lark
import pytest

from crm_basebot.domain import schema
from crm_basebot.lark.bitable import (
    FIELD_TYPE_AUTO_NUMBER,
    FIELD_TYPE_SINGLE_LINK,
    FIELD_TYPE_TEXT,
    FIELD_TYPE_USER,
)


def _load(name: str):
    path = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync_base = _load("sync_base")


def body(field) -> dict:
    """SDK 真正会发出去的那个 JSON。"""
    return json.loads(lark.JSON.marshal(field))


# ---------- 单向关联 ----------


def test_单向关联必须带上被关联表的_table_id():
    """``property.table_id`` 是必填的。

    漏了它，建字段接口只回一句「字段属性错误」，看不出是哪一环缺东西 ——
    而 ``Referred Client.所属渠道`` 恰好就是整条 交易→客户→渠道 链路的那一环，
    建不出来后面全塌。
    """
    field = sync_base._build_field(
        schema.CLIENT_REFERRAL_LINK, FIELD_TYPE_SINGLE_LINK, link_table_id="tblReferral123"
    )
    payload = body(field)

    assert payload["type"] == FIELD_TYPE_SINGLE_LINK
    assert payload["property"]["table_id"] == "tblReferral123"


def test_一个客户只能挂一个渠道():
    """``multiple`` 默认是 true。留着 true，有人在界面上多挂一个渠道，
    佣金该算给谁就说不清了。"""
    field = sync_base._build_field(
        schema.CLIENT_REFERRAL_LINK, FIELD_TYPE_SINGLE_LINK, link_table_id="tblX"
    )
    assert body(field)["property"]["multiple"] is False


def test_没给被关联表就在本地停下():
    with pytest.raises(ValueError, match="table_id"):
        sync_base._build_field(schema.CLIENT_REFERRAL_LINK, FIELD_TYPE_SINGLE_LINK)


def test_关联目标声明指向渠道表():
    assert (
        sync_base.LINK_TARGETS[(schema.TABLE_CLIENT_NAME, schema.CLIENT_REFERRAL_LINK)]
        == schema.TABLE_REFERRAL_NAME
    )


def test_声明为单向关联的字段都在_link_targets_里有目标():
    """schema 里新增一个关联字段却忘了配目标表，建表时才炸就太晚了。"""
    for table_name, fields in sync_base.TARGET_TABLES.items():
        for field_name, type_code in fields.items():
            if type_code == FIELD_TYPE_SINGLE_LINK:
                assert (table_name, field_name) in sync_base.LINK_TARGETS


def test_关联目标表都是我们自己建的表():
    """指向同事那张只读的交易明细表是不行的 —— sync_base 根本不建它。"""
    for target in sync_base.LINK_TARGETS.values():
        assert target in sync_base.TARGET_TABLES


# ---------- 自动编号 ----------


def test_自动编号规则是_r_加三位数字():
    field = sync_base._build_field(schema.REFERRAL_NO, FIELD_TYPE_AUTO_NUMBER)
    auto_serial = body(field)["property"]["auto_serial"]

    assert auto_serial["type"] == "custom"
    assert auto_serial["options"] == [
        {"type": "fixed_text", "value": "R"},
        {"type": "system_number", "value": "3"},
    ]


def test_自动编号位数是_1_到_9_的字符串():
    """``system_number`` 的 value 必须是 1-9 的整数，且**以字符串形式**传。"""
    (number_option,) = [
        o for o in schema.REFERRAL_NO_AUTO_SERIAL["options"] if o["type"] == "system_number"
    ]
    assert isinstance(number_option["value"], str)
    assert 1 <= int(number_option["value"]) <= 9


# ---------- 无 property 的类型 ----------


def test_文本和人员字段不带多余的_property():
    """文本字段的 property 就该是空的；人员字段的 multiple 有默认值，不用显式给。"""
    for type_code in (FIELD_TYPE_TEXT, FIELD_TYPE_USER):
        payload = body(sync_base._build_field("某字段", type_code))
        assert payload["field_name"] == "某字段"
        assert payload["type"] == type_code
        assert "property" not in payload


# ---------- 不该碰的表 ----------


def test_不建同事的交易明细表():
    """在生产 Base 上建一张同名表会盖住同事那张真表，是灾难。"""
    assert schema.TABLE_TRANSACTION_NAME not in sync_base.TARGET_TABLES
