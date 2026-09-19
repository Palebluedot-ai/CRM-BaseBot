"""建字段请求体的形状。

``sync_base.py --apply`` 是接真实 API 的第一步，它建错一个字段，后面所有事都做
不下去。不同字段类型对 ``property`` 的要求差别很大，而这些要求在本地一行代码都
测不到 —— 除非把请求体拆开看。这里就是拆开看。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import lark_oapi as lark
import pytest

from crm_basebot.domain import schema
from crm_basebot.domain.dates import date_to_ms
from crm_basebot.lark.bitable import (
    FIELD_TYPE_AUTO_NUMBER,
    FIELD_TYPE_FORMULA,
    FIELD_TYPE_SINGLE_LINK,
    FIELD_TYPE_TEXT,
    FIELD_TYPE_USER,
)

from .conftest import TBL_BOARD

SGT = ZoneInfo("Asia/Singapore")


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


# ---------- 日读看板表纳入 sync ----------


def test_建日读看板表():
    """日读看板是我们自己维护的表（从 xlsx 导入），必须由 sync_base 负责建结构。

    早期版本这里是「不建交易明细」—— 那时候交易明细是同事在公司租户维护的只读表，
    我们建同名表会盖住她们的。切到日读看板后，这张表变成了我们自己的写入面，
    sync_base 必须把它建出来。
    """
    assert schema.TABLE_DAILY_BOARD_NAME in sync_base.TARGET_TABLES


# ---------- 看板的渠道反查列（关联 + 公式，2026-09-18 定的） ----------


def test_看板反查列由_sync_base_负责建():
    """生产迁移靠的就是这一步：换一份凭证跑 --apply，5 个列自动建出来，不用手工点。"""
    board = sync_base.TARGET_TABLES[schema.TABLE_DAILY_BOARD_NAME]
    for field_name in schema.DAILY_BOARD_DERIVED_FIELDS:
        assert field_name in board


def test_看板客户关联声明指向客户表():
    assert (
        sync_base.LINK_TARGETS[(schema.TABLE_DAILY_BOARD_NAME, schema.BOARD_CLIENT_LINK)]
        == schema.TABLE_CLIENT_NAME
    )


def test_公式字段带上表达式和返回类型():
    """``formula_type=2`` 的多维表格必须带 ``property.type.data_type``，不带接口报错（实测）。"""
    expression, data_type = schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_ROW_COMMISSION]
    field = sync_base._build_field(
        schema.BOARD_ROW_COMMISSION, FIELD_TYPE_FORMULA, formula=(expression, data_type)
    )
    payload = body(field)

    assert payload["type"] == FIELD_TYPE_FORMULA
    assert payload["property"]["formula_expression"] == expression
    assert payload["property"]["type"]["data_type"] == data_type


def test_没给表达式就在本地停下():
    """少了表达式，接口只回一句「字段属性错误」，看不出是哪一环缺东西。"""
    with pytest.raises(ValueError, match="表达式"):
        sync_base._build_field(schema.BOARD_ROW_COMMISSION, FIELD_TYPE_FORMULA)


def test_建完表把table_id写回环境文件(tmp_path):
    """id 就在程序手上，没道理让人去界面里一个个抄 —— 抄错的报错还完全指不到错处。"""
    from crm_basebot.structure import write_table_ids

    env = tmp_path / ".env"
    env.write_text("# 我的配置\nTABLE_REFERRAL=\nTABLE_CLIENT=\n", encoding="utf-8")

    written = write_table_ids(
        env, {schema.TABLE_REFERRAL_NAME: "tblA", schema.TABLE_CLIENT_NAME: "tblB"}
    )

    text = env.read_text(encoding="utf-8")
    assert written == ["TABLE_REFERRAL", "TABLE_CLIENT"]
    assert "TABLE_REFERRAL=tblA" in text
    assert "TABLE_CLIENT=tblB" in text
    assert "# 我的配置" in text  # 注释不能被重排掉


def test_写table_id时只写真拿到的那些(tmp_path):
    from crm_basebot.structure import write_table_ids

    env = tmp_path / ".env"
    env.write_text("TABLE_REFERRAL=\nTABLE_SALES=\n", encoding="utf-8")

    written = write_table_ids(env, {schema.TABLE_SALES_NAME: "tblS"})

    assert written == ["TABLE_SALES"]
    assert env.read_text(encoding="utf-8").splitlines()[0] == "TABLE_REFERRAL="  # 没被动


def test_环境文件的键名映射覆盖六张表():
    from crm_basebot.structure import TABLE_ENV_KEYS

    assert set(TABLE_ENV_KEYS) == set(sync_base.TARGET_TABLES)


def test_每个公式列都配了表达式():
    for field_name, type_code in schema.DAILY_BOARD_DERIVED_FIELDS.items():
        if type_code == FIELD_TYPE_FORMULA:
            assert field_name in schema.DAILY_BOARD_DERIVED_FORMULAS


def test_反查公式里的字段名和_schema_一致():
    """公式里的 ``[字段名]`` 写错，平台**不报错**，只会静默出空值 —— 名字在这里钉死。

    链路是 看板.客户 → 客户.所属渠道 → 渠道.<字段>，三段名字都必须和 schema 里的一致。
    """
    two_hop = f"[{schema.BOARD_CLIENT_LINK}].[{schema.CLIENT_REFERRAL_LINK}].[{schema.REFERRAL_NO}]"
    assert schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_REFERRAL_NO][0] == two_hop

    rate, _ = schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_CLIENT_RATE]
    assert rate.endswith(f".[{schema.REFERRAL_RATE}]")

    commission, _ = schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_ROW_COMMISSION]
    assert f"[{schema.BOARD_TOTAL_REVENUE}]" in commission
    assert f"[{schema.BOARD_CLIENT_RATE}]" in commission


def test_本笔佣金逐行如实不做保底():
    """逐行 MAX(0, ...) 会让逐行相加大于月度应付，和 Python 对账对不上。
    月度保底是 reconcile 的业务规则，不在公式里复制一份。"""
    expression, _ = schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_ROW_COMMISSION]
    assert "MAX" not in expression.upper()


def test_没挂渠道的行佣金留空而不是零():
    """``0.00`` 在这一列是个错误陈述：它等于说「这笔没有佣金」。

    没挂上关联（用户没登记渠道）实际是「不知道有没有」，得留空。
    实测：不加保护时空值参与乘法会算出 0，当天 1,287 行全显示 0.00。
    """
    expression, _ = schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_ROW_COMMISSION]
    assert expression.startswith(f"IF(ISBLANK([{schema.BOARD_CLIENT_RATE}]),")


def test_本笔佣金公式逐字符就是线上那一版():
    """线上那一列（2026-09-18 建的）就是这个字符串。

    故意写成字面量而不是拼 schema 常量：差一个字符就是另一个公式，而平台不校验表达式，
    只会静默算成空列或错值。生产迁移建出来的必须和已核对过的那一版一模一样。
    """
    expression, data_type = schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_ROW_COMMISSION]
    assert expression == ('IF(ISBLANK([分佣比例]), "", [总收入(opt+现货+合约)] * [分佣比例] / 100)')
    assert data_type == schema.FORMULA_DATA_TYPE_NUMBER


def test_月份列是_yyyy_MM_文本():
    """视图和仪表盘都没法按「派生维度」分组，得先有月份列才能做按月报表。"""
    expression, data_type = schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_MONTH]
    assert expression == 'TEXT([交易日期], "yyyy-MM")'
    assert data_type == schema.FORMULA_DATA_TYPE_TEXT


def test_月份自检在错月时报警(fake_bitable, capsys):
    """公式里的 TEXT() 按**平台**时区算（实测 UTC+8），业务时区不是 UTC+8 就会错月。

    错月不报任何别的错：只是把 8 月的钱算进 7 月，报表上看不出来。所以自检必须自己发现。
    """
    fake_bitable.tables[TBL_BOARD].add_existing(
        {
            schema.BOARD_ORDER_DATE: date_to_ms(date(2026, 8, 1), tz=SGT),
            schema.BOARD_MONTH: "2026-07",  # 错月
        }
    )

    sync_base._verify_formulas(fake_bitable, TBL_BOARD, tz=SGT, sample=10)

    assert "对不上" in capsys.readouterr().out


def test_月份自检通过时不报警(fake_bitable, capsys):
    fake_bitable.tables[TBL_BOARD].add_existing(
        {
            schema.BOARD_ORDER_DATE: date_to_ms(date(2026, 8, 1), tz=SGT),
            schema.BOARD_MONTH: "2026-08",
        }
    )

    sync_base._verify_formulas(fake_bitable, TBL_BOARD, tz=SGT, sample=10)

    assert "和业务时区一致" in capsys.readouterr().out
