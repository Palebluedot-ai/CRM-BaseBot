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

from .conftest import TBL_BOARD, TBL_CLIENT

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
    expression, data_type = schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_CLIENT_RATE]
    field = sync_base._build_field(
        schema.BOARD_CLIENT_RATE, FIELD_TYPE_FORMULA, formula=(expression, data_type)
    )
    payload = body(field)

    assert payload["type"] == FIELD_TYPE_FORMULA
    assert payload["property"]["formula_expression"] == expression
    assert payload["property"]["type"]["data_type"] == data_type


def test_没给表达式就在本地停下():
    """少了表达式，接口只回一句「字段属性错误」，看不出是哪一环缺东西。"""
    with pytest.raises(ValueError, match="表达式"):
        sync_base._build_field(schema.BOARD_CLIENT_RATE, FIELD_TYPE_FORMULA)


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


def test_本笔佣金按AI规则算():
    """2026-09-26：超哥每天看这一列，所以留着，但要和月结同一条 AI 规则。"""
    expression, data_type = schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_ROW_COMMISSION]
    assert data_type == schema.FORMULA_DATA_TYPE_NUMBER
    assert expression.startswith(f'IF(ISBLANK([{schema.BOARD_CLIENT_RATE}]), ""')  # 没渠道是空
    assert f"< {schema.AI_RULE_START_NUMBER}" in expression  # 9 月以前照旧
    assert f">= [{schema.BOARD_AI_GATE}]" in expression  # 不早于门槛就算（升级当天起）
    assert f"[{schema.BOARD_TOTAL_REVENUE}] * [{schema.BOARD_CLIENT_RATE}] / 100, 0))" in expression


def test_门槛列从客户表拿():
    expression, _ = schema.DAILY_BOARD_DERIVED_FORMULAS[schema.BOARD_AI_GATE]
    assert expression == f"[{schema.BOARD_CLIENT_LINK}].[{schema.CLIENT_AI_GATE}]"
    assert list(schema.DAILY_BOARD_DERIVED_FIELDS)[-2:] == [
        schema.BOARD_AI_GATE,
        schema.BOARD_ROW_COMMISSION,
    ]  # 本笔佣金引用门槛列，门槛列要先建


def test_客户表的门槛公式用的是那三个选项和两个特殊数():
    expression, data_type = schema.CLIENT_DERIVED_FORMULAS[schema.CLIENT_AI_GATE]
    assert data_type == schema.FORMULA_DATA_TYPE_NUMBER
    for option in schema.AI_STATUS_OPTIONS:
        assert f'"{option}"' in expression
    assert f"[{schema.CLIENT_AI_STATUS}]" in expression
    assert f"ISBLANK([{schema.CLIENT_AI_DATE}])" in expression
    assert str(schema.AI_GATE_NEVER) in expression


def test_客户表的公式列建在AI两列后面():
    fields = list(sync_base.TARGET_TABLES[schema.TABLE_CLIENT_NAME])
    assert fields.index(schema.CLIENT_AI_GATE) > fields.index(schema.CLIENT_AI_DATE)


# ---------- 公式变了要跟上 ----------


class _Response:
    def __init__(self) -> None:
        self.code, self.msg, self.data = 0, "ok", None

    def success(self) -> bool:
        return True


class _FieldEndpoint:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.updated: list[tuple[str, str]] = []

    def create(self, request):
        self.created.append(request.request_body.field_name)
        return _Response()

    def update(self, request):
        body = request.request_body
        self.updated.append((request.field_id, body.property.formula_expression))
        return _Response()


class _Sdk:
    def __init__(self) -> None:
        self.bitable = self
        self.v1 = self
        self.app_table_field = _FieldEndpoint()


class _Base:
    """只有 list_tables / list_fields：ensure_structure 只读这两个。"""

    def __init__(self, fields_by_table: dict[str, list]) -> None:
        self._fields = fields_by_table

    def list_tables(self):
        from crm_basebot.lark.bitable import TableInfo

        return [TableInfo(table_id=f"tbl_{name}", name=name) for name in sync_base.TARGET_TABLES]

    def list_fields(self, table_id: str):
        return self._fields.get(table_id.removeprefix("tbl_"), [])


def _complete_fields(overrides: dict[tuple[str, str], str]) -> dict[str, list]:
    """每张表的字段都齐了；公式列默认是现行公式，``overrides`` 里的换成旧公式。"""
    from crm_basebot.lark.bitable import FieldInfo
    from crm_basebot.structure import FORMULAS

    out: dict[str, list] = {}
    for table, fields in sync_base.TARGET_TABLES.items():
        out[table] = []
        for index, (name, type_code) in enumerate(fields.items()):
            props = {}
            if (table, name) in FORMULAS:
                wanted = FORMULAS[(table, name)][0]
                props = {"formula_expression": overrides.get((table, name), wanted)}
            out[table].append(
                FieldInfo(
                    field_id=f"fld{index}",
                    name=name,
                    type=type_code,
                    ui_type="",
                    is_primary=index == 0,
                    props=props,
                )
            )
    return out


def _ensure(fields, *, apply: bool):
    from types import SimpleNamespace

    from crm_basebot.structure import ensure_structure

    sdk = _Sdk()
    result = ensure_structure(
        settings=SimpleNamespace(base_app_token="bascn"),
        bitable=_Base(fields),
        client=sdk,
        apply=apply,
    )
    return result, sdk.app_table_field


def test_老的本笔佣金公式会换成带AI规则的():
    old = 'IF(ISBLANK([分佣比例]), "", [总收入(opt+现货+合约)] * [分佣比例] / 100)'
    key = (schema.TABLE_DAILY_BOARD_NAME, schema.BOARD_ROW_COMMISSION)
    result, endpoint = _ensure(_complete_fields({key: old}), apply=True)

    assert result.plan == [f"「{schema.TABLE_DAILY_BOARD_NAME}」改公式 本笔佣金"]
    assert len(endpoint.updated) == 1
    assert endpoint.updated[0][1] == schema.DAILY_BOARD_DERIVED_FORMULAS["本笔佣金"][0]
    assert endpoint.created == []


def test_预演时公式只说不改():
    key = (schema.TABLE_DAILY_BOARD_NAME, schema.BOARD_ROW_COMMISSION)
    result, endpoint = _ensure(_complete_fields({key: "1"}), apply=False)
    assert result.plan and endpoint.updated == []


def test_公式一样时什么都不做_空格不算差别():
    key = (schema.TABLE_CLIENT_NAME, schema.CLIENT_AI_GATE)
    spaced = "  ".join(schema.CLIENT_DERIVED_FORMULAS[schema.CLIENT_AI_GATE][0].split(" "))
    result, endpoint = _ensure(_complete_fields({key: spaced}), apply=True)
    assert result.plan == [] and endpoint.updated == []


def test_缺了门槛列和本笔佣金就加上():
    fields = _complete_fields({})
    fields[schema.TABLE_DAILY_BOARD_NAME] = [
        f for f in fields[schema.TABLE_DAILY_BOARD_NAME] if f.name != schema.BOARD_ROW_COMMISSION
    ]
    fields[schema.TABLE_CLIENT_NAME] = [
        f for f in fields[schema.TABLE_CLIENT_NAME] if f.name != schema.CLIENT_AI_GATE
    ]
    result, endpoint = _ensure(fields, apply=True)
    assert endpoint.created == [schema.CLIENT_AI_GATE, schema.BOARD_ROW_COMMISSION]
    assert result.added_fields == 2


def test_AI状态单选列建的时候带着三个选项():
    payload = body(sync_base._build_field(schema.CLIENT_AI_STATUS, 3))
    assert [o["name"] for o in payload["property"]["options"]] == list(schema.AI_STATUS_OPTIONS)


def test_客户表要有AI那两列():
    assert schema.CLIENT_FIELDS[schema.CLIENT_AI_STATUS] == 3
    assert schema.CLIENT_FIELDS[schema.CLIENT_AI_DATE] == 5


def test_公式自检看分佣比例列(fake_bitable, capsys):
    """以前看「本笔佣金」算没算出来；那一列删了之后改看「分佣比例」反查得出没有。"""
    fake_bitable.tables[TBL_BOARD].add_existing(
        {schema.BOARD_CLIENT_LINK: {"link_record_ids": ["rec1"]}, schema.BOARD_CLIENT_RATE: 20}
    )
    sync_base._verify_formulas(fake_bitable, TBL_BOARD, tz=SGT, sample=10)
    out = capsys.readouterr().out
    assert "反查得出「分佣比例」的 1 行" in out
    assert "公式在算" in out


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


# ---------- AI 规则自检 ----------


def _ai_board(fake_bitable, *, commission_on_sep_9: float):
    """一个 9/10 升级的客户，9/9 和 9/10 各一笔 1000 的交易，比例 20%。"""
    client = fake_bitable.tables[TBL_CLIENT].add_existing(
        {
            schema.CLIENT_AI_STATUS: schema.AI_STATUS_UPGRADED,
            schema.CLIENT_AI_DATE: date_to_ms(date(2026, 9, 10), tz=SGT),
        }
    )
    for day, commission in ((9, commission_on_sep_9), (10, 200.0)):
        fake_bitable.tables[TBL_BOARD].add_existing(
            {
                schema.BOARD_ORDER_DATE: date_to_ms(date(2026, 9, day), tz=SGT),
                schema.BOARD_CLIENT_LINK: {"link_record_ids": [client]},
                schema.BOARD_TOTAL_REVENUE: 1000.0,
                schema.BOARD_CLIENT_RATE: 20,
                schema.BOARD_AI_GATE: 20260910,
                schema.BOARD_ROW_COMMISSION: commission,
            }
        )


def test_AI自检_Base和月结算的一样就说全对(fake_bitable, capsys):
    _ai_board(fake_bitable, commission_on_sep_9=0.0)
    assert sync_base._verify_ai_commission(fake_bitable, TBL_BOARD, TBL_CLIENT, tz=SGT)
    assert "核对 2 行，全对" in capsys.readouterr().out


def test_AI自检_升级前一天Base还算了钱就报出来(fake_bitable, capsys):
    _ai_board(fake_bitable, commission_on_sep_9=200.0)
    assert not sync_base._verify_ai_commission(fake_bitable, TBL_BOARD, TBL_CLIENT, tz=SGT)
    out = capsys.readouterr().out
    assert "1 行对不上" in out
    assert "2026-09-09 本笔佣金 200.0（应为 0.00）" in out


# ---------- 数字显示格式 ----------


class _FormatEndpoint:
    def __init__(self, fail: bool = False) -> None:
        self.updated: list = []
        self.fail = fail

    def update(self, request):
        self.updated.append(request.request_body)
        response = _Response()
        if self.fail:
            response.success = lambda: False
            response.code, response.msg = 1254000, "bad formatter"
        return response


class _FormatSdk:
    def __init__(self, endpoint) -> None:
        self.bitable = self
        self.v1 = self
        self.app_table_field = endpoint


class _FormatBase:
    def __init__(self, fields: dict[str, list]) -> None:
        self._fields = fields

    def list_tables(self):
        from crm_basebot.lark.bitable import TableInfo

        return [TableInfo(table_id=f"tbl_{name}", name=name) for name in self._fields]

    def list_fields(self, table_id: str):
        return self._fields[table_id.removeprefix("tbl_")]


def _field(name, type_code, props=None, index=1):
    from crm_basebot.lark.bitable import FieldInfo

    return FieldInfo(
        field_id=f"fld_{name}",
        name=name,
        type=type_code,
        ui_type="",
        is_primary=False,
        props=props or {},
    )


def _formats(fields, *, apply=True, fail=False):
    from types import SimpleNamespace

    from crm_basebot.structure import ensure_number_formats

    endpoint = _FormatEndpoint(fail=fail)
    result = ensure_number_formats(
        settings=SimpleNamespace(base_app_token="bascn"),
        bitable=_FormatBase(fields),
        client=_FormatSdk(endpoint),
        apply=apply,
    )
    return result, endpoint.updated


def test_金额列加千分位_笔数不带小数_比例和门槛不动():
    from crm_basebot.lark.bitable import FIELD_TYPE_FORMULA, FIELD_TYPE_NUMBER

    fields = {
        schema.TABLE_COMMISSION_NAME: [
            _field(schema.COMM_PAYABLE, FIELD_TYPE_NUMBER, {"formatter": "0.0"}),
            _field(schema.COMM_TXN_COUNT, FIELD_TYPE_NUMBER),
            _field(schema.COMM_RATE, FIELD_TYPE_NUMBER),
        ],
        schema.TABLE_DAILY_BOARD_NAME: [
            _field(
                schema.BOARD_AI_GATE,
                FIELD_TYPE_FORMULA,
                {"formula_expression": "1", "type": {"data_type": 2}},
            ),
        ],
    }
    result, updated = _formats(fields)
    got = {body.field_name: body.property.formatter for body in updated}
    assert got == {schema.COMM_PAYABLE: "1,000.00", schema.COMM_TXN_COUNT: "1,000"}
    assert result.warnings == []


def test_公式列改格式时公式原样写回():
    from crm_basebot.lark.bitable import FIELD_TYPE_FORMULA

    expression = "IF(1, 2, 3)"
    fields = {
        schema.TABLE_DAILY_BOARD_NAME: [
            _field(
                schema.BOARD_ROW_COMMISSION,
                FIELD_TYPE_FORMULA,
                {"formula_expression": expression, "type": {"data_type": 2}},
            )
        ]
    }
    _, (body,) = _formats(fields)
    assert body.property.formula_expression == expression
    assert body.property.type.data_type == 2
    assert body.property.formatter == "1,000.00"


def test_格式已经对了就不动_预演只说不改():
    from crm_basebot.lark.bitable import FIELD_TYPE_NUMBER

    done = {
        schema.TABLE_COMMISSION_NAME: [
            _field(schema.COMM_PAYABLE, FIELD_TYPE_NUMBER, {"formatter": "1,000.00"})
        ]
    }
    result, updated = _formats(done)
    assert result.plan == [] and updated == []

    todo = {schema.TABLE_COMMISSION_NAME: [_field(schema.COMM_PAYABLE, FIELD_TYPE_NUMBER)]}
    result, updated = _formats(todo, apply=False)
    assert result.plan and updated == []


def test_ECAS两张表也设_没有的表跳过():
    from crm_basebot.domain import ecas
    from crm_basebot.lark.bitable import FIELD_TYPE_NUMBER

    fields = {
        ecas.TABLE_ECAS_COMMISSION_NAME: [_field(ecas.ECOMM_PAYABLE, FIELD_TYPE_NUMBER)],
    }
    _, updated = _formats(fields)
    assert [body.field_name for body in updated] == [ecas.ECOMM_PAYABLE]


def test_改格式失败只警告_不停下():
    from crm_basebot.lark.bitable import FIELD_TYPE_NUMBER

    fields = {
        schema.TABLE_COMMISSION_NAME: [
            _field(schema.COMM_PAYABLE, FIELD_TYPE_NUMBER),
            _field(schema.COMM_REVENUE_TOTAL, FIELD_TYPE_NUMBER),
        ]
    }
    result, updated = _formats(fields, fail=True)
    assert len(updated) == 2 and len(result.warnings) == 2
    assert "手动设" in result.warnings[0]


def test_换公式时带上格式_不会把千分位洗掉():
    old = 'IF(ISBLANK([分佣比例]), "", [总收入(opt+现货+合约)] * [分佣比例] / 100)'
    key = (schema.TABLE_DAILY_BOARD_NAME, schema.BOARD_ROW_COMMISSION)
    fields = _complete_fields({key: old})
    from types import SimpleNamespace

    from crm_basebot.structure import ensure_structure

    endpoint = _FormatEndpoint()
    ensure_structure(
        settings=SimpleNamespace(base_app_token="bascn"),
        bitable=_Base(fields),
        client=_FormatSdk(endpoint),
        apply=True,
    )
    (body,) = endpoint.updated
    assert body.property.formatter == "1,000.00"


def test_ECAS结算表在的话补状态列_不在就不建():
    from types import SimpleNamespace

    from crm_basebot.domain import ecas
    from crm_basebot.lark.bitable import TableInfo
    from crm_basebot.structure import ensure_structure

    fields = _complete_fields({})
    old_ecas = [
        _field(name, type_code)
        for name, type_code in ecas.ECAS_COMMISSION_FIELDS.items()
        if name != ecas.ECOMM_STATUS
    ]

    class WithEcas(_Base):
        def list_tables(self):
            return super().list_tables() + [
                TableInfo(
                    table_id=f"tbl_{ecas.TABLE_ECAS_COMMISSION_NAME}",
                    name=ecas.TABLE_ECAS_COMMISSION_NAME,
                )
            ]

    sdk = _Sdk()
    result = ensure_structure(
        settings=SimpleNamespace(base_app_token="bascn"),
        bitable=WithEcas({**fields, ecas.TABLE_ECAS_COMMISSION_NAME: old_ecas}),
        client=sdk,
        apply=True,
    )
    assert sdk.app_table_field.created == [ecas.ECOMM_STATUS]
    assert not any("建表" in item for item in result.plan)

    result, endpoint = _ensure(_complete_fields({}), apply=True)  # 没有 ECAS 表
    assert endpoint.created == [] and not any("ECAS" in item for item in result.plan)
