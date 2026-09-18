"""渠道登记流程。"""

import json
import logging
from datetime import date

import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.domain import schema
from crm_basebot.domain.audit import AuditLog
from crm_basebot.domain.dates import DEFAULT_BUSINESS_TIMEZONE, date_to_ms, today_in
from crm_basebot.domain.referral import (
    ReferralInput,
    ReferralService,
    ValidationError,
    display_title,
    format_referral_no,
    parse_referral_no,
)

from .conftest import TBL_AUDIT, TBL_REFERRAL

ALICE = "ou_alice000000000000000000000000"
BOB = "ou_bob00000000000000000000000000"

START_DATE = date(2026, 1, 15)

alice = Sales(open_id=ALICE, name="Alice", role=schema.ROLE_SALES, is_active=True)
bob = Sales(open_id=BOB, name="Bob", role=schema.ROLE_SALES, is_active=True)


@pytest.fixture
def service(fake_bitable):
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    return ReferralService(fake_bitable, TBL_REFERRAL, audit, auto_number=True)


def _valid(name="ABC Capital", rate=20.0, *, start_date=START_DATE, payout=schema.PAYOUT_MONTHLY):
    return ReferralInput(
        name=name,
        email="contact@abc.com",
        start_date=start_date,
        commission_rate=rate,
        payout_frequency=payout,
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


# ---------- 主字段回填 ----------


def test_display_title拼编号和名称():
    assert display_title("R001", "ABC Capital") == "R001 ABC Capital"
    assert display_title("R001", "") == "R001"
    assert display_title("", "ABC") == "ABC"
    assert display_title("", "") == ""


def test_登记后主字段被回填成编号加名称(fake_bitable, service):
    """关联字段展示主字段值，主字段没写就是「无标题记录」。"""
    _, record_id = service.create(alice, _valid("鲸落数字"))
    stored = fake_bitable.tables[TBL_REFERRAL].records[record_id]
    primary_name = fake_bitable.tables[TBL_REFERRAL].primary_field
    assert stored[primary_name] == "R001 鲸落数字"


def test_主字段就是渠道名称时不重复更新(fake_bitable):
    """避免多一次无谓的 update 往返 —— create 已经写过渠道名称了。"""
    fake_bitable.tables[TBL_REFERRAL].primary_field = schema.REFERRAL_NAME
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    service = ReferralService(fake_bitable, TBL_REFERRAL, audit, auto_number=True)

    service.create(alice, _valid())

    assert fake_bitable.updates == []


def test_回填主字段失败不阻断登记(fake_bitable, service, monkeypatch):
    """主字段是给展示看的，写不进去也不该把整个登记推翻。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟接口超时")

    monkeypatch.setattr(fake_bitable, "update_record", boom)

    no, record_id = service.create(alice, _valid())

    assert no == "R001"
    assert record_id in fake_bitable.tables[TBL_REFERRAL].records


# ---------- 归属 ----------


def test_归属人自动记为提交者(fake_bitable, service):
    _, record_id = service.create(alice, _valid())
    stored = fake_bitable.tables[TBL_REFERRAL].records[record_id]
    assert stored[schema.REFERRAL_OWNER_OPEN_ID] == ALICE
    assert stored[schema.REFERRAL_OWNER] == [{"id": ALICE}]


def test_新渠道登记即生效(fake_bitable, service):
    """没有「待审核」这个状态（2026-09-04 定的）：销售登记完，渠道直接生效。"""
    _, record_id = service.create(alice, _valid())
    stored = fake_bitable.tables[TBL_REFERRAL].records[record_id]
    assert stored[schema.REFERRAL_STATUS] == schema.STATUS_ACTIVE


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
                start_date=START_DATE,
                commission_rate=10,
                payout_frequency=schema.PAYOUT_MONTHLY,
            ),
        )


# ---------- 表单字段落库 ----------


def test_开始日期按业务时区零点落库(fake_bitable, service):
    """和导入脚本、看板同一口径：写成业务时区那一天的零点，不是 UTC 零点。"""
    _, record_id = service.create(alice, _valid())

    stored = fake_bitable.tables[TBL_REFERRAL].records[record_id]
    assert stored[schema.REFERRAL_START_DATE] == date_to_ms(
        START_DATE, tz=DEFAULT_BUSINESS_TIMEZONE
    )


def test_结算频率原样落库(fake_bitable, service):
    """写的是模板原文 Monthly / Quarterly，不是下拉标签里的中文。"""
    _, record_id = service.create(alice, _valid(payout=schema.PAYOUT_QUARTERLY))

    stored = fake_bitable.tables[TBL_REFERRAL].records[record_id]
    assert stored[schema.REFERRAL_PAYOUT] == "Quarterly"


def test_提交日期记为登记当天(fake_bitable, service):
    """模板里的 Submitted On 在导入的历史行上都有值，机器人登记的行也不能空着。"""
    _, record_id = service.create(alice, _valid())

    stored = fake_bitable.tables[TBL_REFERRAL].records[record_id]
    expected = date_to_ms(today_in(DEFAULT_BUSINESS_TIMEZONE), tz=DEFAULT_BUSINESS_TIMEZONE)
    assert stored[schema.REFERRAL_SUBMITTED_ON] == expected


def test_登记表单不再写地址和收款信息(fake_bitable, service):
    """这两列模板里没有，机器人不再收，只留给人手工补（2026-09-18 定的）。"""
    _, record_id = service.create(alice, _valid())

    stored = fake_bitable.tables[TBL_REFERRAL].records[record_id]
    assert schema.REFERRAL_ADDRESS not in stored
    assert schema.REFERRAL_PAYMENT not in stored


@pytest.mark.parametrize("payout", ["monthly", "按月", "", "Yearly"])
def test_结算频率只能是模板里的两种(service, payout):
    """单选列里多出一个没人选的选项就删不掉了，所以在这里拦住。"""
    with pytest.raises(ValidationError, match="结算频率"):
        service.create(alice, _valid(payout=payout))


def test_开始日期为空被拒(service):
    """类型上不该发生，但值是从卡片回调里取的，真能是 None。"""
    with pytest.raises(ValidationError, match="开始日期"):
        service.create(alice, _valid(start_date=None))


def test_审计里记下开始日期和结算频率(fake_bitable, service):
    service.create(alice, _valid(payout=schema.PAYOUT_QUARTERLY))

    (row,) = fake_bitable.tables[TBL_AUDIT].records.values()
    detail = json.loads(row[schema.AUDIT_DETAIL])
    assert detail["开始日期"] == START_DATE.isoformat()
    assert detail["结算频率"] == schema.PAYOUT_QUARTERLY


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


# ---------- 日志 ----------


def test_登记成功留一行日志说清谁写了哪条(service, caplog):
    """销售说「我登记了」而 Base 里没有时，服务端要能翻到痕迹，不用去查审计表。"""
    with caplog.at_level(logging.INFO, logger="crm_basebot.domain.referral"):
        no, record_id = service.create(alice, _valid())

    (line,) = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert no in line
    assert record_id in line
    assert ALICE in line
