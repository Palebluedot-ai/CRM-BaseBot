"""客户登记流程。

重点在两处：客户UID 的精度和归属隔离。
"""

import logging

import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.domain import schema
from crm_basebot.domain.audit import AuditLog
from crm_basebot.domain.referral import ReferralInput, ReferralService, ValidationError
from crm_basebot.domain.referred_client import ClientInput, ReferredClientService

from .conftest import TBL_AUDIT, TBL_CLIENT, TBL_REFERRAL

ALICE = "ou_alice000000000000000000000000"
BOB = "ou_bob00000000000000000000000000"
ADMIN = "ou_admin00000000000000000000000"

alice = Sales(open_id=ALICE, name="Alice", role=schema.ROLE_SALES, is_active=True)
bob = Sales(open_id=BOB, name="Bob", role=schema.ROLE_SALES, is_active=True)
admin = Sales(open_id=ADMIN, name="Admin", role=schema.ROLE_ADMIN, is_active=True)

UID_18 = "577809207768677761"
UID_19 = "2141293991366272768"


@pytest.fixture
def services(fake_bitable):
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    referrals = ReferralService(fake_bitable, TBL_REFERRAL, audit)
    clients = ReferredClientService(fake_bitable, TBL_CLIENT, TBL_REFERRAL, audit)
    return referrals, clients


def _referral(referrals, sales, name="ABC Capital"):
    no, _ = referrals.create(
        sales,
        ReferralInput(
            name=name,
            email="a@b.com",
            address="HK",
            payment_info="bank",
            commission_rate=20,
        ),
    )
    return no


def test_登记客户挂到自己的渠道(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, alice)

    record_id = clients.create(
        alice, ClientInput(uid=UID_18, name="PLUTO STUDIO LIMITED", referral_no=no)
    )

    stored = fake_bitable.tables[TBL_CLIENT].records[record_id]
    assert stored[schema.CLIENT_UID] == UID_18
    assert stored[schema.CLIENT_OWNER_OPEN_ID] == ALICE


def test_uid以字符串存储不丢精度(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, alice)

    record_id = clients.create(
        alice, ClientInput(uid=UID_19, name="HOMEX AND AI PTE. LTD.", referral_no=no)
    )

    stored = fake_bitable.tables[TBL_CLIENT].records[record_id]
    assert isinstance(stored[schema.CLIENT_UID], str)
    assert stored[schema.CLIENT_UID] == UID_19


def test_不能挂到别人的渠道(services):
    referrals, clients = services
    bob_no = _referral(referrals, bob, "Bob 的渠道")

    with pytest.raises(ValidationError, match="没找到你名下的渠道"):
        clients.create(alice, ClientInput(uid=UID_18, name="X", referral_no=bob_no))


def test_不存在的渠道和别人的渠道给同样的错误(services):
    """不泄露「这个编号存在但不是你的」，避免被枚举。"""
    referrals, clients = services
    bob_no = _referral(referrals, bob, "Bob 的渠道")

    with pytest.raises(ValidationError) as others:
        clients.create(alice, ClientInput(uid=UID_18, name="X", referral_no=bob_no))
    with pytest.raises(ValidationError) as missing:
        clients.create(alice, ClientInput(uid=UID_18, name="X", referral_no="R999"))

    assert str(others.value).replace(bob_no, "R999") == str(missing.value)


def test_管理员可以挂到任何渠道(services):
    referrals, clients = services
    bob_no = _referral(referrals, bob, "Bob 的渠道")
    assert clients.create(admin, ClientInput(uid=UID_18, name="X", referral_no=bob_no))


def test_同一个客户不能重复登记(services):
    referrals, clients = services
    no = _referral(referrals, alice)
    clients.create(alice, ClientInput(uid=UID_18, name="X", referral_no=no))

    with pytest.raises(ValidationError, match="已经登记过"):
        clients.create(alice, ClientInput(uid=UID_18, name="X again", referral_no=no))


def test_重复检测对大整数uid也准确(services):
    """两个只差最后几位的 UID，必须被当成不同客户。"""
    referrals, clients = services
    no = _referral(referrals, alice)

    near_a = "577809207768677761"
    near_b = "577809207768677762"
    clients.create(alice, ClientInput(uid=near_a, name="A", referral_no=no))

    # 若中途转过 float，这两个会被视为同一个值而误报重复
    assert clients.create(alice, ClientInput(uid=near_b, name="B", referral_no=no))


@pytest.mark.parametrize("bad_uid", ["", "   ", "abc123", "577-809-207"])
def test_非法uid被拒(services, bad_uid):
    referrals, clients = services
    no = _referral(referrals, alice)
    with pytest.raises(ValidationError, match="客户UID"):
        clients.create(alice, ClientInput(uid=bad_uid, name="X", referral_no=no))


def test_客户名不能为空(services):
    referrals, clients = services
    no = _referral(referrals, alice)
    with pytest.raises(ValidationError, match="客户名称"):
        clients.create(alice, ClientInput(uid=UID_18, name="", referral_no=no))


def test_登记客户留下审计(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, alice)
    clients.create(alice, ClientInput(uid=UID_18, name="X", referral_no=no))

    actions = [row[schema.AUDIT_ACTION] for row in fake_bitable.tables[TBL_AUDIT].records.values()]
    assert actions == ["登记渠道", "登记客户"]


# ---------- 日志 ----------


def test_登记成功留一行日志说清谁写了哪条(services, caplog):
    referrals, clients = services
    no = _referral(referrals, alice)

    with caplog.at_level(logging.INFO, logger="crm_basebot.domain.referred_client"):
        record_id = clients.create(
            alice, ClientInput(uid=UID_18, name="PLUTO STUDIO LIMITED", referral_no=no)
        )

    (line,) = [
        r.getMessage() for r in caplog.records if r.name == "crm_basebot.domain.referred_client"
    ]
    assert UID_18 in line
    assert record_id in line
    assert no in line
    assert ALICE in line
