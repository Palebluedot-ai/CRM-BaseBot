"""渠道登记流程。"""

import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.domain import schema
from crm_basebot.domain.audit import AuditLog
from crm_basebot.domain.referral import (
    ReferralInput,
    ReferralService,
    ValidationError,
    format_referral_no,
    parse_referral_no,
)

from .conftest import TBL_AUDIT, TBL_REFERRAL

ALICE = "ou_alice000000000000000000000000"
BOB = "ou_bob00000000000000000000000000"

alice = Sales(open_id=ALICE, name="Alice", role=schema.ROLE_SALES, is_active=True)
bob = Sales(open_id=BOB, name="Bob", role=schema.ROLE_SALES, is_active=True)


@pytest.fixture
def service(fake_bitable):
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    return ReferralService(fake_bitable, TBL_REFERRAL, audit, auto_number=True)


def _valid(name="ABC Capital", rate=20.0):
    return ReferralInput(
        name=name,
        email="contact@abc.com",
        address="Hong Kong",
        payment_info="HSBC 001-234567-001",
        commission_rate=rate,
    )


# ---------- 编号 ----------


def test_编号解析和格式化():
    assert parse_referral_no("R009") == 9
    assert parse_referral_no("r009") == 9
    assert parse_referral_no("X009") is None
    assert parse_referral_no("") is None
    assert format_referral_no(10) == "R010"
    assert format_referral_no(9) == "R009"


def test_登记后拿到自动生成的编号(service):
    no, record_id = service.create(alice, _valid())
    assert no == "R001"
    assert record_id


def test_连续登记编号递增(service):
    assert service.create(alice, _valid("A"))[0] == "R001"
    assert service.create(bob, _valid("B"))[0] == "R002"
    assert service.create(alice, _valid("C"))[0] == "R003"


def test_编号由服务端生成调用方改不了(fake_bitable, service):
    """就算有人往 fields 里塞编号，也应该被服务端的值覆盖。"""
    no, record_id = service.create(alice, _valid())
    stored = fake_bitable.tables[TBL_REFERRAL].records[record_id]
    assert stored[schema.REFERRAL_NO] == no


def test_历史数据存在时接着往下编(fake_bitable):
    """现有 R001-R009，新增应该落在 R010。"""
    table = fake_bitable.tables[TBL_REFERRAL]
    table._serial = iter(range(10, 100))
    for i in range(1, 10):
        table.add_existing({schema.REFERRAL_NO: f"R{i:03d}"})

    audit = AuditLog(fake_bitable, TBL_AUDIT)
    service = ReferralService(fake_bitable, TBL_REFERRAL, audit, auto_number=True)

    assert service.create(alice, _valid())[0] == "R010"


def test_回退方案读最大号加一(fake_bitable):
    """自动编号不可用时，从现有记录算下一个号。"""
    table = fake_bitable.tables[TBL_REFERRAL]
    table.auto_number_field = None
    for i in (1, 2, 9):
        table.add_existing({schema.REFERRAL_NO: f"R{i:03d}"})

    audit = AuditLog(fake_bitable, TBL_AUDIT)
    service = ReferralService(fake_bitable, TBL_REFERRAL, audit, auto_number=False)

    assert service.next_manual_no() == "R010"
    assert service.create(alice, _valid())[0] == "R010"


def test_回退方案空表从R001开始(fake_bitable):
    fake_bitable.tables[TBL_REFERRAL].auto_number_field = None
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    service = ReferralService(fake_bitable, TBL_REFERRAL, audit, auto_number=False)
    assert service.next_manual_no() == "R001"


# ---------- 归属 ----------


def test_归属人自动记为提交者(fake_bitable, service):
    _, record_id = service.create(alice, _valid())
    stored = fake_bitable.tables[TBL_REFERRAL].records[record_id]
    assert stored[schema.REFERRAL_OWNER_OPEN_ID] == ALICE
    assert stored[schema.REFERRAL_OWNER] == [{"id": ALICE}]


def test_新渠道状态是待审核(fake_bitable, service):
    _, record_id = service.create(alice, _valid())
    stored = fake_bitable.tables[TBL_REFERRAL].records[record_id]
    assert stored[schema.REFERRAL_STATUS] == schema.STATUS_PENDING


def test_只列出自己名下的渠道(service):
    service.create(alice, _valid("Alice 的渠道"))
    service.create(bob, _valid("Bob 的渠道"))

    assert [name for _, name in service.list_for(alice)] == ["Alice 的渠道"]
    assert [name for _, name in service.list_for(bob)] == ["Bob 的渠道"]


# ---------- 校验 ----------


def test_渠道名不能为空(service):
    with pytest.raises(ValidationError, match="名称不能为空"):
        service.create(alice, _valid(name="   "))


@pytest.mark.parametrize("rate", [0, -5, 101, 1000])
def test_分佣比例超范围被拒(service, rate):
    with pytest.raises(ValidationError, match="分佣比例"):
        service.create(alice, _valid(rate=rate))


def test_邮箱格式检查(service):
    with pytest.raises(ValidationError, match="邮箱"):
        service.create(
            alice,
            ReferralInput(
                name="X",
                email="not-an-email",
                address="",
                payment_info="p",
                commission_rate=10,
            ),
        )


# ---------- 审计 ----------


def test_每次登记都留下审计记录(fake_bitable, service):
    service.create(alice, _valid())

    audit_rows = list(fake_bitable.tables[TBL_AUDIT].records.values())
    assert len(audit_rows) == 1
    assert audit_rows[0][schema.AUDIT_ACTOR_OPEN_ID] == ALICE
    assert audit_rows[0][schema.AUDIT_ACTION] == "登记渠道"


def test_校验失败时不写审计也不写业务表(fake_bitable, service):
    with pytest.raises(ValidationError):
        service.create(alice, _valid(rate=999))

    assert fake_bitable.tables[TBL_AUDIT].records == {}
    assert fake_bitable.tables[TBL_REFERRAL].records == {}
