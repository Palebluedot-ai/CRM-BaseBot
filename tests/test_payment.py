"""收款资料：只能改自己名下的渠道；审计只记改了哪几项，不记账号。"""

from __future__ import annotations

import json

import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.domain import schema
from crm_basebot.domain.audit import AuditLog
from crm_basebot.domain.payment import PaymentInfo, PaymentService, masked
from crm_basebot.domain.referral import ValidationError

from .conftest import TBL_AUDIT, TBL_REFERRAL

ALICE = Sales(open_id="ou_alice", name="Alice", role=schema.ROLE_SALES, is_active=True)
BOB = Sales(open_id="ou_bob", name="Bob", role=schema.ROLE_SALES, is_active=True)
ADMIN = Sales(open_id="ou_admin", name="Admin", role=schema.ROLE_ADMIN, is_active=True)

CRYPTO = PaymentInfo(
    method=schema.PAY_METHOD_CRYPTO,
    address_lines=("Flat 1", "Central", ""),
    wallet_address="0xabc1234567",
)


@pytest.fixture
def service(fake_bitable):
    fake_bitable.tables[TBL_REFERRAL].add_existing(
        {
            schema.REFERRAL_NO: "R001",
            schema.REFERRAL_NAME: "北极星",
            schema.REFERRAL_OWNER_OPEN_ID: "ou_alice",
            schema.REFERRAL_ADDRESS: "Old line",
        }
    )
    return PaymentService(fake_bitable, TBL_REFERRAL, AuditLog(fake_bitable, TBL_AUDIT))


def test_保存加密货币_默认币种USDT_地址按行存(service, fake_bitable):
    saved = service.update(ALICE, "R001", CRYPTO)
    row = next(iter(fake_bitable.tables[TBL_REFERRAL].records.values()))
    assert row[schema.REFERRAL_PAY_METHOD] == schema.PAY_METHOD_CRYPTO
    assert row[schema.REFERRAL_CRYPTO_TYPE] == "USDT"
    assert row[schema.REFERRAL_WALLET] == "0xabc1234567"
    assert row[schema.REFERRAL_ADDRESS] == "Flat 1\nCentral"
    assert saved.info.missing() == []


def test_审计只记改了哪几项_不记账号(service, fake_bitable):
    service.update(ALICE, "R001", CRYPTO)
    audit = next(iter(fake_bitable.tables[TBL_AUDIT].records.values()))
    detail = json.loads(audit[schema.AUDIT_DETAIL])
    assert "钱包地址" in detail["改了"]
    assert "0xabc" not in audit[schema.AUDIT_DETAIL]


def test_别人的渠道改不了(service):
    with pytest.raises(ValidationError, match="没找到你名下的渠道 R001"):
        service.update(BOB, "R001", CRYPTO)


def test_管理员能改(service):
    assert service.update(ADMIN, "R001", CRYPTO).no == "R001"


@pytest.mark.parametrize(
    ("info", "message"),
    [
        (PaymentInfo(method=schema.PAY_METHOD_BANK, address_lines=("x",)), "银行名称、银行账号"),
        (PaymentInfo(method=schema.PAY_METHOD_CRYPTO, address_lines=("x",)), "钱包地址"),
        (PaymentInfo(method="", address_lines=("x",)), "收款方式"),
    ],
)
def test_缺项说人话(info, message):
    with pytest.raises(ValidationError, match=message):
        info.validated()


def test_读回来_地址拆成行():
    info = PaymentInfo.from_fields(
        {schema.REFERRAL_ADDRESS: "A\n\n B ", schema.REFERRAL_PAY_METHOD: "银行转账"}
    )
    assert info.address_lines == ("A", "B")
    assert info.missing() == ["银行名称", "银行账号"]


def test_只露最后四位():
    assert masked("0123456789") == "****6789"
    assert masked("12") == "****"
    assert masked("") == ""


def test_地址选填_钱包必填():
    info = PaymentInfo(method=schema.PAY_METHOD_CRYPTO, wallet_address="0x1").validated()
    assert info.address_lines == ()
    assert info.missing() == ["地址"]  # 给人看的「还空着」照样列出来
    assert info.missing_for_payment() == []
