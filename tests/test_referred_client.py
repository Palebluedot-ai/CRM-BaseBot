"""客户登记流程。

重点在两处：客户UID 的精度和归属隔离。
"""

import logging
from datetime import date

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

START_DATE = date(2026, 1, 15)


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
            start_date=START_DATE,
            commission_rate=20,
            payout_frequency=schema.PAYOUT_MONTHLY,
        ),
    )
    return no


def test_登记客户挂到自己的渠道(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, alice)

    record_id = clients.create(
        alice,
        ClientInput(uid=UID_18, name="PLUTO STUDIO LIMITED", referral_no=no, ai_status="开户即AI"),
    )

    stored = fake_bitable.tables[TBL_CLIENT].records[record_id]
    assert stored[schema.CLIENT_UID] == UID_18
    assert stored[schema.CLIENT_OWNER_OPEN_ID] == ALICE


def test_uid以字符串存储不丢精度(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, alice)

    record_id = clients.create(
        alice,
        ClientInput(
            uid=UID_19, name="HOMEX AND AI PTE. LTD.", referral_no=no, ai_status="开户即AI"
        ),
    )

    stored = fake_bitable.tables[TBL_CLIENT].records[record_id]
    assert isinstance(stored[schema.CLIENT_UID], str)
    assert stored[schema.CLIENT_UID] == UID_19


def test_不能挂到别人的渠道(services):
    referrals, clients = services
    bob_no = _referral(referrals, bob, "Bob 的渠道")

    with pytest.raises(ValidationError, match="没找到你名下的渠道"):
        clients.create(
            alice, ClientInput(uid=UID_18, name="X", referral_no=bob_no, ai_status="开户即AI")
        )


def test_不存在的渠道和别人的渠道给同样的错误(services):
    """不泄露「这个编号存在但不是你的」，避免被枚举。"""
    referrals, clients = services
    bob_no = _referral(referrals, bob, "Bob 的渠道")

    with pytest.raises(ValidationError) as others:
        clients.create(
            alice, ClientInput(uid=UID_18, name="X", referral_no=bob_no, ai_status="开户即AI")
        )
    with pytest.raises(ValidationError) as missing:
        clients.create(
            alice, ClientInput(uid=UID_18, name="X", referral_no="R999", ai_status="开户即AI")
        )

    assert str(others.value).replace(bob_no, "R999") == str(missing.value)


def test_管理员可以挂到任何渠道(services):
    referrals, clients = services
    bob_no = _referral(referrals, bob, "Bob 的渠道")
    assert clients.create(
        admin, ClientInput(uid=UID_18, name="X", referral_no=bob_no, ai_status="开户即AI")
    )


def test_同一个客户不能重复登记(services):
    referrals, clients = services
    no = _referral(referrals, alice)
    clients.create(alice, ClientInput(uid=UID_18, name="X", referral_no=no, ai_status="开户即AI"))

    with pytest.raises(ValidationError, match="已经登记过"):
        clients.create(
            alice, ClientInput(uid=UID_18, name="X again", referral_no=no, ai_status="开户即AI")
        )


def test_重复检测对大整数uid也准确(services):
    """两个只差最后几位的 UID，必须被当成不同客户。"""
    referrals, clients = services
    no = _referral(referrals, alice)

    near_a = "577809207768677761"
    near_b = "577809207768677762"
    clients.create(alice, ClientInput(uid=near_a, name="A", referral_no=no, ai_status="开户即AI"))

    # 若中途转过 float，这两个会被视为同一个值而误报重复
    assert clients.create(
        alice, ClientInput(uid=near_b, name="B", referral_no=no, ai_status="开户即AI")
    )


@pytest.mark.parametrize("bad_uid", ["", "   ", "abc123", "577-809-207"])
def test_非法uid被拒(services, bad_uid):
    referrals, clients = services
    no = _referral(referrals, alice)
    with pytest.raises(ValidationError, match="客户UID"):
        clients.create(
            alice, ClientInput(uid=bad_uid, name="X", referral_no=no, ai_status="开户即AI")
        )


def test_客户名不能为空(services):
    referrals, clients = services
    no = _referral(referrals, alice)
    with pytest.raises(ValidationError, match="客户名称"):
        clients.create(
            alice, ClientInput(uid=UID_18, name="", referral_no=no, ai_status="开户即AI")
        )


def test_登记客户留下审计(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, alice)
    clients.create(alice, ClientInput(uid=UID_18, name="X", referral_no=no, ai_status="开户即AI"))

    actions = [row[schema.AUDIT_ACTION] for row in fake_bitable.tables[TBL_AUDIT].records.values()]
    assert actions == ["登记渠道", "登记客户"]


# ---------- 日志 ----------


def test_登记成功留一行日志说清谁写了哪条(services, caplog):
    referrals, clients = services
    no = _referral(referrals, alice)

    with caplog.at_level(logging.INFO, logger="crm_basebot.domain.referred_client"):
        record_id = clients.create(
            alice,
            ClientInput(
                uid=UID_18, name="PLUTO STUDIO LIMITED", referral_no=no, ai_status="开户即AI"
            ),
        )

    (line,) = [
        r.getMessage() for r in caplog.records if r.name == "crm_basebot.domain.referred_client"
    ]
    assert UID_18 in line
    assert record_id in line
    assert no in line
    assert ALICE in line


# ---------- AI 状态（2026-09-25） ----------

from crm_basebot.domain.dates import DEFAULT_BUSINESS_TIMEZONE, date_to_ms  # noqa: E402

AI_DAY = date(2026, 8, 24)


def _client_row(fake_bitable, record_id):
    return fake_bitable.table(TBL_CLIENT).records[record_id]


def test_登记时写入AI状态和升级日期(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, alice)
    record_id = clients.create(
        alice,
        ClientInput(
            uid=UID_18,
            name="X",
            referral_no=no,
            ai_status=schema.AI_STATUS_UPGRADED,
            ai_date=AI_DAY,
        ),
    )
    row = _client_row(fake_bitable, record_id)
    assert row[schema.CLIENT_AI_STATUS] == schema.AI_STATUS_UPGRADED
    assert row[schema.CLIENT_AI_DATE] == date_to_ms(AI_DAY, tz=DEFAULT_BUSINESS_TIMEZONE)


def test_开户即AI可以不填日期(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, alice)
    record_id = clients.create(
        alice, ClientInput(uid=UID_18, name="X", referral_no=no, ai_status="开户即AI")
    )
    row = _client_row(fake_bitable, record_id)
    assert row[schema.CLIENT_AI_STATUS] == schema.AI_STATUS_ALREADY
    assert schema.CLIENT_AI_DATE not in row


@pytest.mark.parametrize(
    ("status", "day", "words"),
    [
        ("", None, "AI 状态要选一个"),
        ("随便写", None, "AI 状态要选一个"),
        (schema.AI_STATUS_UPGRADED, None, "要填升级日期"),
        (schema.AI_STATUS_NOT, AI_DAY, "不用填升级日期"),
    ],
)
def test_AI状态和日期的校验(status, day, words):
    data = ClientInput(uid=UID_18, name="X", referral_no="R001", ai_status=status, ai_date=day)
    with pytest.raises(ValidationError, match=words):
        data.validated()


def test_补上升级日期(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, alice)
    record_id = clients.create(
        alice, ClientInput(uid=UID_18, name="X", referral_no=no, ai_status=schema.AI_STATUS_NOT)
    )

    name, referral_no = clients.update_ai(alice, UID_18, schema.AI_STATUS_UPGRADED, AI_DAY)

    assert (name, referral_no) == ("X", no)
    row = _client_row(fake_bitable, record_id)
    assert row[schema.CLIENT_AI_STATUS] == schema.AI_STATUS_UPGRADED
    assert row[schema.CLIENT_AI_DATE] == date_to_ms(AI_DAY, tz=DEFAULT_BUSINESS_TIMEZONE)


def test_改回开户即AI时清掉旧日期(fake_bitable, services):
    """判定时日期优先。旧日期留着的话，状态说「开户即AI」，算的却还是升级那个月起。"""
    referrals, clients = services
    no = _referral(referrals, alice)
    record_id = clients.create(
        alice,
        ClientInput(
            uid=UID_18,
            name="X",
            referral_no=no,
            ai_status=schema.AI_STATUS_UPGRADED,
            ai_date=AI_DAY,
        ),
    )
    clients.update_ai(alice, UID_18, schema.AI_STATUS_ALREADY, None)
    assert _client_row(fake_bitable, record_id)[schema.CLIENT_AI_DATE] is None


def test_别人名下的客户改不了_也不告诉你它存在(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, bob, "Bob 的渠道")
    clients.create(
        bob, ClientInput(uid=UID_18, name="Bob 的客户", referral_no=no, ai_status="非AI")
    )

    with pytest.raises(ValidationError) as others:
        clients.update_ai(alice, UID_18, schema.AI_STATUS_ALREADY, None)
    with pytest.raises(ValidationError) as missing:
        clients.update_ai(alice, "999999999999999999", schema.AI_STATUS_ALREADY, None)

    assert "没找到你名下" in str(others.value)
    assert str(others.value).replace(UID_18, "X") == str(missing.value).replace(
        "999999999999999999", "X"
    )


def test_管理员能改任何人的客户(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, bob, "Bob 的渠道")
    clients.create(
        bob, ClientInput(uid=UID_18, name="Bob 的客户", referral_no=no, ai_status="非AI")
    )
    assert clients.update_ai(admin, UID_18, schema.AI_STATUS_UPGRADED, AI_DAY)[1] == no


def test_更新AI状态记审计(fake_bitable, services):
    referrals, clients = services
    no = _referral(referrals, alice)
    clients.create(alice, ClientInput(uid=UID_18, name="X", referral_no=no, ai_status="非AI"))
    clients.update_ai(alice, UID_18, schema.AI_STATUS_UPGRADED, AI_DAY)

    actions = [f[schema.AUDIT_ACTION] for t, f in fake_bitable.writes if t == TBL_AUDIT]
    assert actions[-1] == "更新客户AI状态"
