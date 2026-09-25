"""写进 Bitable 的 fields 长什么样。

飞书对每种字段类型接受的写入格式很挑：人员要 ``[{"id": "ou_x"}]``、日期要毫秒
整数、单向关联要 ``["recxxx"]``、单选直接给字符串。格式不对不会静默忽略，而是
整条记录写不进去（1254015 / 1254066 / 1254067 这一串）。

再加两条更隐蔽的：

  · **只读字段不能出现在写入 payload 里**。自动编号、公式、查找引用、创建时间
    这些由系统维护，塞进去接口直接报错。
  · **值必须是 JSON 原生类型**。Decimal 就是个典型 —— 业务层算钱全程用 Decimal，
    忘了转 float 的话，SDK 序列化那一步会抛 TypeError，而且是在写请求发出之前，
    错误信息里完全看不出是哪个字段。

所有断言都盯 ``FakeBitable.writes`` 里记下的原始 fields，也就是真正会发给平台的
那个 dict。
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Any

import lark_oapi as lark
import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.domain import schema
from crm_basebot.domain.audit import AuditLog
from crm_basebot.domain.commission import CommissionRow
from crm_basebot.domain.dates import DEFAULT_BUSINESS_TIMEZONE, date_to_ms
from crm_basebot.domain.referral import ReferralInput, ReferralService
from crm_basebot.domain.referred_client import ClientInput, ReferredClientService
from crm_basebot.jobs.reconcile import _write_rows
from crm_basebot.lark.bitable import READ_ONLY_FIELD_TYPES

from .conftest import TBL_AUDIT, TBL_CLIENT, TBL_COMMISSION, TBL_REFERRAL

ALICE = "ou_alice000000000000000000000000"
UID = "577809207768677761"
START_DATE = date(2026, 1, 15)

alice = Sales(open_id=ALICE, name="Alice", role=schema.ROLE_SALES, is_active=True)

# 每张表声明的字段类型，用来反查哪些字段是只读的
DECLARED_FIELDS = {
    TBL_REFERRAL: schema.REFERRAL_FIELDS,
    TBL_CLIENT: schema.CLIENT_FIELDS,
    TBL_AUDIT: schema.AUDIT_FIELDS,
    TBL_COMMISSION: schema.COMMISSION_FIELDS,
}

JSON_SCALARS = (str, int, float, bool)


@pytest.fixture
def services(fake_bitable):
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    referrals = ReferralService(fake_bitable, TBL_REFERRAL, audit, auto_number=True)
    clients = ReferredClientService(fake_bitable, TBL_CLIENT, TBL_REFERRAL, audit)
    return referrals, clients


@pytest.fixture
def written(fake_bitable, services):
    """跑一遍全部写路径，把每张表收到的 payload 收集起来。"""
    referrals, clients = services

    no, _ = referrals.create(
        alice,
        ReferralInput(
            name="北极星资本",
            email="ops@polaris.example",
            start_date=START_DATE,
            commission_rate=12.5,
            payout_frequency=schema.PAYOUT_MONTHLY,
        ),
    )
    clients.create(
        alice, ClientInput(uid=UID, name="普罗米修斯资本", referral_no=no, ai_status="开户即AI")
    )

    _write_rows(
        fake_bitable,
        TBL_COMMISSION,
        [
            CommissionRow(
                period="2026-03",
                referral_no=no,
                referral_name="北极星资本",
                rate_percent=_decimal("12.5"),
                revenue_total=_decimal("1240.55"),
                txn_count=3,
                client_uids={UID},
            )
        ],
    )

    by_table: dict[str, list[dict[str, Any]]] = {}
    for table_id, fields in fake_bitable.writes:
        by_table.setdefault(table_id, []).append(fields)
    return by_table


def _decimal(text: str):
    from decimal import Decimal

    return Decimal(text)


def _is_json_native(value: Any) -> bool:
    if value is None or isinstance(value, JSON_SCALARS):
        return True
    if isinstance(value, list):
        return all(_is_json_native(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_native(v) for k, v in value.items())
    return False


# ---------- 通用约束 ----------


def test_只读字段从不出现在写入_payload_里(written):
    """自动编号、公式、查找引用这类字段由系统维护，写进去接口会拒绝整条记录。

    渠道编号 2026-09-17 起改成了文本列，现在六张表都没有只读字段；这条守的是以后
    有人给哪张表加了公式或查找引用列，写入代码别把它带上。
    """
    for table_id, payloads in written.items():
        read_only = {
            name
            for name, type_code in DECLARED_FIELDS[table_id].items()
            if type_code in READ_ONLY_FIELD_TYPES
        }
        for fields in payloads:
            assert not (read_only & set(fields)), f"{table_id} 的写入里混进了只读字段"


def test_写入的字段名都在_schema_里声明过(written):
    """字段名对不上平台会回 1254045 FieldNameNotFound，整条写入失败。"""
    for table_id, payloads in written.items():
        declared = set(DECLARED_FIELDS[table_id])
        for fields in payloads:
            assert set(fields) <= declared, f"{table_id} 写了 schema 里没声明的字段"


def test_字段值都是_json_原生类型(written):
    """Decimal / date / set 这类值会在 SDK 序列化那一步抛 TypeError。"""
    for table_id, payloads in written.items():
        for fields in payloads:
            for name, value in fields.items():
                assert _is_json_native(value), f"{table_id}.{name} 的值是 {type(value).__name__}"
            # 真正要过的那一关：SDK 用自己的 Encoder 序列化
            assert lark.JSON.marshal({"fields": fields})


# ---------- 渠道表 ----------


def test_人员字段写成_id_对象数组(written):
    (fields,) = written[TBL_REFERRAL]
    assert fields[schema.REFERRAL_OWNER] == [{"id": ALICE}]


def test_单选字段直接给选项字符串(written):
    (fields,) = written[TBL_REFERRAL]
    assert fields[schema.REFERRAL_STATUS] == schema.STATUS_ACTIVE
    assert isinstance(fields[schema.REFERRAL_STATUS], str)


def test_数字字段是数字不是字符串(written):
    (fields,) = written[TBL_REFERRAL]
    assert fields[schema.REFERRAL_RATE] == 12.5
    assert isinstance(fields[schema.REFERRAL_RATE], int | float)


def test_自动编号不出现在渠道写入里(written):
    """``渠道编号`` 是自动编号字段，由服务端生成。"""
    (fields,) = written[TBL_REFERRAL]
    assert schema.REFERRAL_NO not in fields


def test_关掉自动编号时才自己写编号(fake_bitable):
    """退回后端递增方案时，编号列在 Base 里是文本字段，这时候写它才是对的。"""
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    service = ReferralService(fake_bitable, TBL_REFERRAL, audit, auto_number=False)
    service.create(
        alice,
        ReferralInput(
            name="鲸落数字",
            email="",
            start_date=START_DATE,
            commission_rate=20,
            payout_frequency=schema.PAYOUT_MONTHLY,
        ),
    )

    (fields,) = [f for t, f in fake_bitable.writes if t == TBL_REFERRAL]
    assert fields[schema.REFERRAL_NO] == "R001"


# ---------- 客户表 ----------


def test_单向关联写成_record_id_数组(written):
    (fields,) = written[TBL_CLIENT]
    links = fields[schema.CLIENT_REFERRAL_LINK]
    assert isinstance(links, list)
    assert len(links) == 1
    assert all(isinstance(item, str) and item.startswith("rec") for item in links)


def test_客户uid_是字符串且没被改动(written):
    """18-19 位 UID 一旦以数字形态到达服务端就被 float64 抹平低位，静默错配。"""
    (fields,) = written[TBL_CLIENT]
    assert fields[schema.CLIENT_UID] == UID
    assert isinstance(fields[schema.CLIENT_UID], str)


# ---------- 审计表 ----------


def test_日期字段是毫秒整数(written):
    now_ms = int(time.time() * 1000)
    for fields in written[TBL_AUDIT]:
        at = fields[schema.AUDIT_AT]
        assert isinstance(at, int) and not isinstance(at, bool)
        # 秒和毫秒差三个数量级，写错量级会落到 1970 年
        assert now_ms - 60_000 <= at <= now_ms + 60_000

    (referral,) = written[TBL_REFERRAL]
    start = referral[schema.REFERRAL_START_DATE]
    assert isinstance(start, int) and not isinstance(start, bool)
    assert start == date_to_ms(START_DATE, tz=DEFAULT_BUSINESS_TIMEZONE)
    # 提交日期是登记当天，只校验量级：断言具体值会在跨零点时闪断
    assert isinstance(referral[schema.REFERRAL_SUBMITTED_ON], int)

    (commission,) = written[TBL_COMMISSION]
    computed_at = commission[schema.COMM_COMPUTED_AT]
    assert isinstance(computed_at, int)
    assert now_ms - 60_000 <= computed_at <= now_ms + 60_000


def test_审计详情是可解析的_json_文本(written):
    for fields in written[TBL_AUDIT]:
        detail = fields[schema.AUDIT_DETAIL]
        assert isinstance(detail, str)
        assert isinstance(json.loads(detail), dict)


def test_审计写入不额外回读(fake_bitable, services):
    """审计在卡片回调的 3 秒预算里，每一个多余的往返都要省。"""
    referrals, _ = services
    fake_bitable.read_back_count = 0

    referrals.create(
        alice,
        ReferralInput(
            name="恒星资本",
            email="",
            start_date=START_DATE,
            commission_rate=15,
            payout_frequency=schema.PAYOUT_MONTHLY,
        ),
    )

    # 只有渠道表那一次写入需要读回自动编号
    assert fake_bitable.read_back_count == 1


def test_渠道写入里没有地址和收款信息(written):
    """这两列模板里没有，登记表单不再收（2026-09-18 定的）。"""
    (fields,) = written[TBL_REFERRAL]
    assert schema.REFERRAL_ADDRESS not in fields
    assert schema.REFERRAL_PAYMENT not in fields


# ---------- 佣金汇总表 ----------


def test_佣金金额转成_float_而不是_decimal(written):
    (fields,) = written[TBL_COMMISSION]
    for name in (schema.COMM_REVENUE_TOTAL, schema.COMM_RATE, schema.COMM_PAYABLE):
        assert isinstance(fields[name], float), f"{name} 还是 Decimal，序列化会炸"

    assert fields[schema.COMM_REVENUE_TOTAL] == pytest.approx(1240.55)
    assert fields[schema.COMM_PAYABLE] == pytest.approx(155.07)
    assert fields[schema.COMM_CLIENT_COUNT] == 1
    assert fields[schema.COMM_TXN_COUNT] == 3
