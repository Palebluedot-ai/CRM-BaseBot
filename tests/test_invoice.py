"""佣金 invoice：金额只认结算表；明细只有和结算表一分不差才列；收款资料不全就不出。"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.documents import invoice as inv
from crm_basebot.domain import ecas, schema
from crm_basebot.domain.audit import AuditLog
from crm_basebot.domain.commission_query import CommissionQueryService
from crm_basebot.domain.dates import DEFAULT_BUSINESS_TIMEZONE, date_to_ms
from crm_basebot.domain.payment import PaymentInfo, PaymentService

from .conftest import (
    TBL_AUDIT,
    TBL_BOARD,
    TBL_CLIENT,
    TBL_COMMISSION,
    TBL_ECAS,
    TBL_ECAS_COMMISSION,
    TBL_REFERRAL,
)

SGT = DEFAULT_BUSINESS_TIMEZONE
ALICE = Sales(open_id="ou_alice", name="Alice", role=schema.ROLE_SALES, is_active=True)
BOB = Sales(open_id="ou_bob", name="Bob", role=schema.ROLE_SALES, is_active=True)
PAID = date(2026, 10, 10)

CRYPTO = {
    schema.REFERRAL_PAY_METHOD: schema.PAY_METHOD_CRYPTO,
    schema.REFERRAL_ADDRESS: "Flat 1\nCentral\nHong Kong",
    schema.REFERRAL_CRYPTO_TYPE: "USDT-ERC20",
    schema.REFERRAL_WALLET: "0xWALLET",
}
BANK = {
    schema.REFERRAL_PAY_METHOD: schema.PAY_METHOD_BANK,
    schema.REFERRAL_ADDRESS: "1 Raffles Place",
    schema.REFERRAL_BANK_NAME: "DBS Bank",
    schema.REFERRAL_BANK_ACCOUNT_NO: "0123456789",
}


class Settings:
    table_referral = TBL_REFERRAL
    table_client = TBL_CLIENT
    table_daily_board = TBL_BOARD
    table_commission = TBL_COMMISSION
    table_ecas = TBL_ECAS
    table_ecas_commission = TBL_ECAS_COMMISSION
    business_timezone = "Asia/Singapore"


@pytest.fixture
def base(fake_bitable):
    referrals = fake_bitable.tables[TBL_REFERRAL]
    r1 = referrals.add_existing(
        {
            schema.REFERRAL_NO: "R001",
            schema.REFERRAL_NAME: "北极星",
            schema.REFERRAL_RATE: 20,
            schema.REFERRAL_OWNER_OPEN_ID: "ou_alice",
            **CRYPTO,
        }
    )
    referrals.add_existing(
        {
            schema.REFERRAL_NO: "R002",
            schema.REFERRAL_NAME: "南十字",
            schema.REFERRAL_RATE: 50,
            schema.REFERRAL_OWNER_OPEN_ID: "ou_alice",
            # 没登记收款资料
        }
    )
    clients = fake_bitable.tables[TBL_CLIENT]
    for uid, name in (("577809207768677761", "甲公司"), ("577809207768677762", "乙公司")):
        clients.add_existing(
            {
                schema.CLIENT_UID: uid,
                schema.CLIENT_NAME: name,
                schema.CLIENT_REFERRAL_LINK: [r1],
            }
        )
    for uid, revenue in (("577809207768677761", 1000.0), ("577809207768677762", 500.0)):
        fake_bitable.tables[TBL_BOARD].add_existing(
            {
                schema.BOARD_CLIENT_UID: uid,
                schema.BOARD_ORDER_DATE: "2026/08/15",
                schema.BOARD_TOTAL_REVENUE: revenue,
            }
        )
    commission = fake_bitable.tables[TBL_COMMISSION]
    for no, rate, payable in (("R001", 20, 300.0), ("R002", 50, 80.0)):
        commission.add_existing(
            {
                schema.COMM_PERIOD: "2026-08",
                schema.COMM_REFERRAL_NO: no,
                schema.COMM_RATE: rate,
                schema.COMM_PAYABLE: payable,
            }
        )
    fake_bitable.tables[TBL_ECAS].add_existing(
        {
            ecas.ECAS_CLIENT_NAME: "丙公司",
            ecas.ECAS_AMOUNT: 5000,
            ecas.ECAS_APPLIED_AT: date_to_ms(date(2026, 8, 3), tz=SGT),
            ecas.ECAS_REFERRAL_LINK: [r1],
            ecas.ECAS_RATE: 50,
        }
    )
    fake_bitable.tables[TBL_ECAS_COMMISSION].add_existing(
        {
            ecas.ECOMM_PERIOD: "2026-08",
            ecas.ECOMM_REFERRAL_NO: "R001",
            ecas.ECOMM_RATE_NOTE: "50%",
            ecas.ECOMM_PAYABLE: 2500.0,
        }
    )
    return fake_bitable


def service(bitable, tmp_path: Path) -> inv.InvoiceService:
    return inv.InvoiceService(
        bitable,
        settings=Settings(),
        payments=PaymentService(bitable, TBL_REFERRAL, AuditLog(bitable, TBL_AUDIT)),
        commission_query=CommissionQueryService(bitable, settings=Settings()),
        tz=SGT,
        work_dir=tmp_path,
    )


def test_月份只列结算表里有的_自己看得到的(base, tmp_path):
    assert service(base, tmp_path).periods_for(ALICE) == ["2026-08"]
    assert service(base, tmp_path).periods_for(BOB) == []


def test_明细和结算表对得上就逐客户列(base, tmp_path):
    batch = service(base, tmp_path).build(ALICE, "2026-08", kinds=["trade", "ecas"])
    trade, ecas_invoice = batch.invoices
    assert (trade.kind, trade.total, trade.itemized) == ("trade", Decimal("300.00"), True)
    assert [(r.description, r.amount) for r in trade.rows] == [
        ("乙公司", Decimal("100.00")),
        ("甲公司", Decimal("200.00")),
    ]
    assert trade.fee_rate == "20.00%"
    assert (ecas_invoice.kind, ecas_invoice.total, ecas_invoice.fee_rate) == (
        "ecas",
        Decimal("2500.00"),
        "50%",
    )
    assert [r.description for r in ecas_invoice.rows] == ["丙公司"]


def test_收款资料不全的渠道不出_说缺什么(base, tmp_path):
    batch = service(base, tmp_path).build(ALICE, "2026-08", kinds=["trade"])
    assert [i.referral_no for i in batch.invoices] == ["R001"]
    (skipped,) = batch.skipped
    assert skipped.referral_no == "R002"
    assert "收款方式" in skipped.missing


def test_结算后数据变了_只印总额(base, tmp_path):
    """结算表写的是 300，但之后比例改了 / 客户变了，现算不再是 300：只印一行总额。"""
    record = next(
        rid
        for rid, f in base.tables[TBL_COMMISSION].records.items()
        if f[schema.COMM_REFERRAL_NO] == "R001"
    )
    base.tables[TBL_COMMISSION].records[record][schema.COMM_PAYABLE] = 280.0
    batch = service(base, tmp_path).build(ALICE, "2026-08", kinds=["trade"])
    (only,) = batch.invoices
    assert only.itemized is False
    assert [(r.description, r.amount) for r in only.rows] == [
        ("Referral fee for August 2026", Decimal("280.00"))
    ]
    assert batch.summary_only == [only]


def test_别人的渠道出不了(base, tmp_path):
    batch = service(base, tmp_path).build(BOB, "2026-08", kinds=["trade", "ecas"])
    assert batch.invoices == [] and batch.skipped == []


def _xml(data: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()


def test_加密货币版填满_日期写法照原工具(base, tmp_path):
    batch = service(base, tmp_path).build(ALICE, "2026-08", kinds=["trade"])
    xml = _xml(inv.render(batch.invoices[0], PAID))
    text = re.sub(r"<[^>]+>", "", xml)
    assert "{{" not in text and "{%" not in text
    for expected in ("北极星", "0xWALLET", "USDT-ERC20", "01/08/2026", "31/08/2026"):
        assert expected in text
    assert "31-August-2026" in text
    assert "10 October 2026" in text
    assert "300.00" in text


def test_银行版填满(base, tmp_path):
    invoice = inv.Invoice(
        kind="trade",
        period="2026-08",
        referral_no="R009",
        referral_name="Bank Co",
        fee_rate="30.00%",
        rows=(inv.InvoiceRow("A", Decimal("1234.5")),),
        total=Decimal("1234.5"),
        payment=PaymentInfo.from_fields(BANK),
    )
    text = re.sub(r"<[^>]+>", "", _xml(inv.render(invoice, PAID)))
    assert "{{" not in text
    assert "ACCOUNT NAME: Bank Co" in text  # 户名没填就用渠道名称
    assert "DBS Bank" in text and "0123456789" in text
    assert "1,234.50" in text


def test_docPr编号还原成范本原值(base, tmp_path):
    batch = service(base, tmp_path).build(ALICE, "2026-08", kinds=["trade"])
    rendered = _xml(inv.render(batch.invoices[0], PAID))
    template = zipfile.ZipFile(inv.template_path("invoice-crypto.docx")).read("word/document.xml")
    ids = re.findall(r'wp:docPr\b[^>]*?\bid="(\d+)"', template.decode())
    assert re.findall(r'wp:docPr\b[^>]*?\bid="(\d+)"', rendered) == ids


def test_一份发Word_多份打成zip(base, tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "to_pdf", lambda docx, work_dir: ({}, "不是 Mac"))
    one = service(base, tmp_path).generate(ALICE, "2026-08", kinds=["trade"], paid_on=PAID)
    assert [name for name, _ in one.files()] == [
        "北极星 - Referral Fee Statement (August 2026).docx"
    ]
    both = service(base, tmp_path).generate(ALICE, "2026-08", kinds=["trade", "ecas"], paid_on=PAID)
    ((name, data),) = both.files()
    assert name == "Invoices August 2026.zip"
    assert sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) == [
        "Word/北极星 - ECAS Referral Fee Statement (August 2026).docx",
        "Word/北极星 - Referral Fee Statement (August 2026).docx",
    ]
    assert both.pdf_note == "不是 Mac"


def test_不是Mac就不转PDF(tmp_path, monkeypatch):
    monkeypatch.setattr(inv.sys, "platform", "linux")
    pdfs, note = inv.to_pdf({"a.docx": b"PK"}, tmp_path)
    assert pdfs == {} and "Mac" in note
