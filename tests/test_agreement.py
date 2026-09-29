"""转介协议：从 onboard-bot-lark 的「填单」搬过来，范本和填法要和原来一样。"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date

import pytest

from crm_basebot.documents import agreement
from crm_basebot.documents.agreement import AgreementError

TODAY = date(2026, 9, 29)

INDIVIDUAL = {
    "client_name": "Zhang San",
    "id_number": "A1234567",
    "full_address": "Flat 1, 5/F, Example Building, Central, Hong Kong",
    "email": "zhang@example.com",
    "fee_pct": "40",
}

CORPORATE = {
    "company_name": "Acme Trading Ltd",
    "registration_number": "CR123456",
    "jurisdiction": "Hong Kong",
    "registered_address": "Suite 1, 2/F, ABC Tower, Central",
    "email": "ops@acme.example",
    "signatory_name": "Jane Doe",
    "signatory_title": "Director",
    "fee_pct": "35%",
}


def _document_xml(data: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()


def test_个人协议填满_没有剩下的空():
    plan = agreement.plan("individual", INDIVIDUAL, effective=TODAY)
    xml = _document_xml(agreement.render(plan))
    assert not re.search(r"\{\{[A-Z_]+\}\}", xml)
    for text in ("Zhang San", "A1234567", "zhang@example.com", "40%", "September 29, 2026"):
        assert text in xml
    assert "quarterly" in xml  # 不填结算周期默认按季


def test_企业协议填满_联系人默认是签署人():
    plan = agreement.plan("corporate", CORPORATE, fee_period="monthly", effective=TODAY)
    assert plan.tokens["NOTICE_ATTENTION"] == "Jane Doe"
    assert plan.tokens["NOTICE_ADDRESS"] == CORPORATE["registered_address"]
    assert plan.tokens["FEE_PERIOD"] == "monthly"
    xml = _document_xml(agreement.render(plan))
    assert not re.search(r"\{\{[A-Z_]+\}\}", xml)
    assert "Acme Trading Ltd" in xml


def test_填了联系人和通知地址就用填的():
    plan = agreement.plan(
        "corporate",
        {**CORPORATE, "notice_attention": "Bob", "notice_address": "PO Box 1"},
        effective=TODAY,
    )
    assert plan.tokens["NOTICE_ATTENTION"] == "Bob"
    assert plan.tokens["NOTICE_ADDRESS"] == "PO Box 1"


def test_缺必填项说出是哪几项():
    values = {**INDIVIDUAL, "id_number": "", "email": " "}
    with pytest.raises(AgreementError, match="证件号、邮箱"):
        agreement.plan("individual", values, effective=TODAY)


@pytest.mark.parametrize("raw", ["abc", "0", "150", "40%%x"])
def test_费率不是0到100的数就拒(raw):
    with pytest.raises(AgreementError, match="费率"):
        agreement.plan("individual", {**INDIVIDUAL, "fee_pct": raw}, effective=TODAY)


def test_邮箱格式不对就拒():
    with pytest.raises(AgreementError, match="邮箱"):
        agreement.plan("individual", {**INDIVIDUAL, "email": "not-an-email"}, effective=TODAY)


def test_飞书自动加的邮箱链接剥掉():
    values = {**INDIVIDUAL, "email": "[zhang@example.com](mailto:zhang@example.com)"}
    plan = agreement.plan("individual", values, effective=TODAY)
    assert plan.tokens["NOTICE_EMAIL"] == "zhang@example.com"


def test_会破坏XML的字符要转义():
    plan = agreement.plan(
        "corporate", {**CORPORATE, "company_name": "A & B <HK> Ltd"}, effective=TODAY
    )
    xml = _document_xml(agreement.render(plan))
    assert "A &amp; B &lt;HK&gt; Ltd" in xml


def test_文件名去掉不能用的字符():
    plan = agreement.plan(
        "corporate", {**CORPORATE, "company_name": 'A/B: "C" Ltd'}, effective=TODAY
    )
    assert plan.filename == "HTS Referral Agreement - Corporate - AB C Ltd.docx"
    individual = agreement.plan("individual", INDIVIDUAL, effective=TODAY)
    assert individual.filename == "HTS Referral Agreement - Zhang San.docx"


def test_日期写成英文():
    assert agreement.english_date(date(2026, 1, 5)) == "January 5, 2026"
