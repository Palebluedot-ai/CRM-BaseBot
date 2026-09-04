"""配置读取。

重点是编号开关能真的切换实现 —— 实测结果出来后改 .env 就够，不用动代码。
"""

import pytest
from pydantic import ValidationError

from crm_basebot.bot.auth import Sales
from crm_basebot.config import Settings
from crm_basebot.domain import schema
from crm_basebot.domain.audit import AuditLog
from crm_basebot.domain.referral import ReferralInput, ReferralService

from .conftest import TBL_AUDIT, TBL_REFERRAL

alice = Sales(
    open_id="ou_alice000000000000000000000000",
    name="Alice",
    role=schema.ROLE_SALES,
    is_active=True,
)


def _settings(**overrides):
    base = {
        "LARK_APP_ID": "cli_test",
        "LARK_APP_SECRET": "secret",
        **overrides,
    }
    return Settings(_env_file=None, **base)


def test_编号开关默认打开():
    assert _settings().referral_auto_number is True


@pytest.mark.parametrize("raw,expected", [("false", False), ("true", True)])
def test_编号开关可以通过环境变量关掉(raw, expected):
    assert _settings(REFERRAL_AUTO_NUMBER=raw).referral_auto_number is expected


def test_开关关掉时走后端递增(fake_bitable):
    """同一份代码，开关决定编号从哪来。"""
    table = fake_bitable.tables[TBL_REFERRAL]
    table.auto_number_field = None
    for i in (1, 2, 3):
        table.add_existing({schema.REFERRAL_NO: f"R{i:03d}"})

    service = ReferralService(
        fake_bitable,
        TBL_REFERRAL,
        AuditLog(fake_bitable, TBL_AUDIT),
        auto_number=_settings(REFERRAL_AUTO_NUMBER="false").referral_auto_number,
    )

    no, _ = service.create(
        alice,
        ReferralInput(name="X", email="", address="", payment_info="p", commission_rate=10),
    )
    assert no == "R004"


def test_未配置的表id默认为空串():
    settings = _settings()
    assert settings.table_referral == ""
    assert settings.base_app_token == ""


# ---------- 业务时区 ----------


def test_业务时区默认新加坡():
    assert _settings().business_timezone == "Asia/Singapore"


def test_业务时区可以通过环境变量换():
    assert _settings(BUSINESS_TIMEZONE="Asia/Hong_Kong").business_timezone == "Asia/Hong_Kong"


def test_业务时区名字不合法时启动就报错():
    """拼错时区名不能等到对账跑到一半才炸，也不能悄悄退回 UTC。"""
    with pytest.raises(ValidationError) as exc_info:
        _settings(BUSINESS_TIMEZONE="Asia/Nowhere")
    assert "Asia/Nowhere" in str(exc_info.value)
