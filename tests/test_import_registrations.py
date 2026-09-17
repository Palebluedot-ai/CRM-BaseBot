"""渠道登记表和客户表从模板 xlsx 导入。

模板是 2026-09-17 给的「Template .xlsx」：三个工作表，Referral Registration（101 个渠道）、
Referred Clients（114 个客户）、用户UID（客户全量 UID 和名称，以后补）。这里钉住的事：

1. **表结构对齐模板**：渠道编号是文本（现成的 R001-R101 写得进去），多了开始日期、
   结算频率、提交日期、负责销售四列；客户表多了负责销售。
2. **重复导入不产生重复行**：渠道按编号、客户按 UID 或「编号 + 客户名」找到旧行就更新。
3. **UID 只认可靠的**：浮点数、被 Excel 抹成尾零的、科学计数法的一律不用，留空并提示。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook

from crm_basebot.domain import schema
from crm_basebot.domain.dates import date_to_ms
from crm_basebot.lark.bitable import (
    FIELD_TYPE_AUTO_NUMBER,
    FIELD_TYPE_DATETIME,
    FIELD_TYPE_SINGLE_SELECT,
    FIELD_TYPE_TEXT,
)

from .conftest import TBL_CLIENT, TBL_REFERRAL, TBL_SALES

SGT = ZoneInfo("Asia/Singapore")


def _load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "import_registrations.py"
    spec = importlib.util.spec_from_file_location("import_registrations", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


importer = _load_module()

REFERRAL_HEADERS = [
    "Numbering",
    "Referral Code",
    "Name",
    "Email",
    "Start Date",
    "Commission Rate",
    "Payout Frequency",
    "Submitted On",
    "Sales In Charge",
]
CLIENT_HEADERS = [
    None,
    "Referral Code ",
    "Name of Referral",
    "Client Name",
    "UID",
    None,
    "Sales In Charge",
]

R001 = (
    "R001",
    "XU CONG",
    "xu@example.com",
    datetime(2024, 3, 1),
    0.2,
    "Monthly",
    datetime(2026, 3, 4),
    "@James YANG",
)
R003 = (
    "R003",
    "LAPSON LIMITED",
    "lapson@example.com",
    datetime(2025, 7, 15),
    0.5,
    "Quarterly",
    datetime(2026, 3, 4),
    "@Jackie Cao",
)
UID_A = "577809159836171649"
UID_B = "2141293991366272768"


def make_template(tmp_path, referrals=(R001, R003), clients=(), uids=(), *, email_header="Email"):
    wb = Workbook()
    ws = wb.active
    ws.title = "Referral Registration"
    headers = list(REFERRAL_HEADERS)
    headers[3] = email_header
    ws.append(headers)
    for index, row in enumerate(referrals, start=1):
        ws.append([index, *row])
    ws2 = wb.create_sheet("Referred Clients")
    ws2.append(CLIENT_HEADERS)
    for code, client, uid, sales in clients:
        ws2.append([None, code, "whoever", client, uid, None, sales])
    ws3 = wb.create_sheet("用户UID")
    ws3.append(["UID", "客户名称"])
    for uid, name in uids:
        ws3.append([uid, name])
    path = tmp_path / "template.xlsx"
    wb.save(path)
    return path


class Settings:
    table_referral = TBL_REFERRAL
    table_client = TBL_CLIENT
    table_sales = TBL_SALES
    business_timezone = "Asia/Singapore"


def _sync(path, bitable, *, apply=True):
    parsed = importer.parse_template(path)
    return importer.sync(bitable, Settings(), parsed, tz=SGT, apply=apply)


def _referrals(bitable):
    return {r[schema.REFERRAL_NO]: r for r in bitable.tables[TBL_REFERRAL].records.values()}


def _clients(bitable):
    return list(bitable.tables[TBL_CLIENT].records.values())


# ---------- 表结构对齐模板 ----------


def test_渠道编号是文本列不再是自动编号():
    assert schema.REFERRAL_FIELDS[schema.REFERRAL_NO] == FIELD_TYPE_TEXT
    assert FIELD_TYPE_AUTO_NUMBER not in schema.REFERRAL_FIELDS.values()


def test_渠道表多了模板里的四列():
    assert schema.REFERRAL_FIELDS[schema.REFERRAL_START_DATE] == FIELD_TYPE_DATETIME
    assert schema.REFERRAL_FIELDS[schema.REFERRAL_PAYOUT] == FIELD_TYPE_SINGLE_SELECT
    assert schema.REFERRAL_FIELDS[schema.REFERRAL_SUBMITTED_ON] == FIELD_TYPE_DATETIME
    assert schema.REFERRAL_FIELDS[schema.REFERRAL_SALES_NAME] == FIELD_TYPE_TEXT


def test_客户表多了负责销售():
    assert schema.CLIENT_FIELDS[schema.CLIENT_SALES_NAME] == FIELD_TYPE_TEXT


# ---------- 读模板 ----------


def test_读渠道登记表_比例从小数换成百分数(tmp_path):
    parsed = importer.parse_template(make_template(tmp_path))

    by_code = {r.code: r for r in parsed.referrals}
    assert by_code["R001"].rate_percent == 20
    assert by_code["R003"].rate_percent == 50
    assert by_code["R001"].name == "XU CONG"
    assert by_code["R001"].email == "xu@example.com"
    assert by_code["R001"].start_date == date(2024, 3, 1)
    assert by_code["R001"].submitted_on == date(2026, 3, 4)
    assert by_code["R001"].payout == "Monthly"


@pytest.mark.parametrize(
    "raw,expected", [(0, 0), (0.33, 33), (1, 100), (20, 20), ("20%", 20), (None, None)]
)
def test_比例各种写法(tmp_path, raw, expected):
    row = (
        "R001",
        "XU CONG",
        "xu@example.com",
        datetime(2024, 3, 1),
        raw,
        "Monthly",
        datetime(2026, 3, 4),
        "@James YANG",
    )
    (referral,) = importer.parse_template(make_template(tmp_path, referrals=(row,))).referrals
    assert referral.rate_percent == expected


def test_邮箱列的表头被写坏了也认(tmp_path):
    """模板里这一列的表头被人覆盖成了一个邮箱地址。按「含 email 或 @」认，不按全名认。"""
    parsed = importer.parse_template(make_template(tmp_path, email_header="emailliwen@gmail.com"))
    assert parsed.referrals[0].email == "xu@example.com"


def test_销售名去掉开头的艾特(tmp_path):
    parsed = importer.parse_template(make_template(tmp_path))
    assert parsed.referrals[0].sales == "James YANG"


def test_名字里带TERMINATED的渠道状态是停用(tmp_path):
    row = (
        "R033",
        "SOMEONE (TERMINATED)",
        "s@example.com",
        datetime(2024, 1, 1),
        0.2,
        "Monthly",
        datetime(2026, 3, 4),
        "@James YANG",
    )
    (referral,) = importer.parse_template(make_template(tmp_path, referrals=(row,))).referrals
    assert referral.status == schema.STATUS_DISABLED
    assert (
        importer.parse_template(make_template(tmp_path)).referrals[0].status == schema.STATUS_ACTIVE
    )


def test_客户行的UID为空时从用户UID表按名字补(tmp_path):
    path = make_template(
        tmp_path,
        clients=[("R001", "Zhenchao  Zhan", None, "James Yang")],
        uids=[(UID_A, "ZHENCHAO ZHAN")],
    )
    (client,) = importer.parse_template(path).clients
    assert client.uid == UID_A
    assert client.name == "Zhenchao  Zhan"


def test_用户UID表里被Excel改坏的值不用并提示(tmp_path):
    path = make_template(
        tmp_path,
        clients=[("R001", "YIMING ZHU", None, None), ("R001", "SOMEONE", None, None)],
        uids=[("‘582600147922353000", "YIMING ZHU"), ("5.82600147922353E+17", "SOMEONE")],
    )
    parsed = importer.parse_template(path)

    assert [c.uid for c in parsed.clients] == ["", ""]
    assert len(parsed.warnings) == 2
    assert "582600147922353000" in parsed.warnings[0]


def test_客户行的UID是浮点数直接拒绝(tmp_path):
    path = make_template(tmp_path, clients=[("R001", "ZHENCHAO ZHAN", 5.77809159836172e17, None)])

    with pytest.raises(importer.RegistrationImportError, match="UID"):
        importer.parse_template(path)


def test_缺工作表或缺列直接报错(tmp_path):
    wb = Workbook()
    wb.active.title = "Referral Registration"
    wb.active.append(["Referral Code", "Name"])
    path = tmp_path / "bad.xlsx"
    wb.save(path)

    with pytest.raises(importer.RegistrationImportError):
        importer.parse_template(path)


# ---------- 写进 Base ----------


def test_首次导入建渠道和客户并回填主字段(tmp_path, fake_bitable):
    path = make_template(tmp_path, clients=[("R001", "ZHENCHAO ZHAN", UID_A, "James Yang")])

    summary = _sync(path, fake_bitable)

    assert (summary.referrals_created, summary.clients_created) == (2, 1)
    referral = _referrals(fake_bitable)["R001"]
    assert referral[schema.REFERRAL_NAME] == "XU CONG"
    assert referral[schema.REFERRAL_RATE] == 20
    assert referral[schema.REFERRAL_PAYOUT] == "Monthly"
    assert referral[schema.REFERRAL_SALES_NAME] == "James YANG"
    assert referral[schema.REFERRAL_STATUS] == schema.STATUS_ACTIVE
    assert referral[schema.REFERRAL_START_DATE] == date_to_ms(date(2024, 3, 1), tz=SGT)
    assert referral[schema.REFERRAL_SUBMITTED_ON] == date_to_ms(date(2026, 3, 4), tz=SGT)
    primary = fake_bitable.tables[TBL_REFERRAL].primary_field
    assert referral[primary] == "R001 XU CONG"

    (client,) = _clients(fake_bitable)
    assert client[schema.CLIENT_UID] == UID_A
    assert client[schema.CLIENT_NAME] == "ZHENCHAO ZHAN"
    assert client[schema.CLIENT_SALES_NAME] == "James Yang"
    referral_id = next(
        rid
        for rid, r in fake_bitable.tables[TBL_REFERRAL].records.items()
        if r[schema.REFERRAL_NO] == "R001"
    )
    assert client[schema.CLIENT_REFERRAL_LINK] == [referral_id]


def test_没有归属人的列留空等OpenID补上(tmp_path, fake_bitable):
    _sync(make_template(tmp_path, clients=[("R001", "ZHENCHAO ZHAN", UID_A, None)]), fake_bitable)

    for record in list(_referrals(fake_bitable).values()) + _clients(fake_bitable):
        assert (
            schema.REFERRAL_OWNER_OPEN_ID not in record or not record[schema.REFERRAL_OWNER_OPEN_ID]
        )
        assert schema.REFERRAL_OWNER not in record


def test_重复导入不产生重复行(tmp_path, fake_bitable):
    path = make_template(tmp_path, clients=[("R001", "ZHENCHAO ZHAN", UID_A, None)])
    _sync(path, fake_bitable)

    summary = _sync(path, fake_bitable)

    assert (summary.referrals_created, summary.clients_created, summary.sales_created) == (0, 0, 0)
    assert summary.referrals_updated == 0
    assert len(_referrals(fake_bitable)) == 2
    assert len(_clients(fake_bitable)) == 1


def test_模板改了值再导入会更新同一行(tmp_path, fake_bitable):
    _sync(make_template(tmp_path), fake_bitable)
    changed = (
        "R001",
        "XU CONG",
        "xu@example.com",
        datetime(2024, 3, 1),
        0.3,
        "Quarterly",
        datetime(2026, 3, 4),
        "@James YANG",
    )

    summary = _sync(make_template(tmp_path, referrals=(changed, R003)), fake_bitable)

    assert summary.referrals_updated == 1
    assert len(_referrals(fake_bitable)) == 2
    assert _referrals(fake_bitable)["R001"][schema.REFERRAL_RATE] == 30


def test_客户后来补上UID会更新同一行(tmp_path, fake_bitable):
    _sync(make_template(tmp_path, clients=[("R001", "ZHENCHAO ZHAN", None, None)]), fake_bitable)
    (before,) = fake_bitable.tables[TBL_CLIENT].records
    assert not _clients(fake_bitable)[0].get(schema.CLIENT_UID)

    summary = _sync(
        make_template(tmp_path, clients=[("R001", "ZHENCHAO ZHAN", UID_A, None)]), fake_bitable
    )

    assert summary.clients_updated == 1
    (after,) = fake_bitable.tables[TBL_CLIENT].records
    assert after == before
    assert _clients(fake_bitable)[0][schema.CLIENT_UID] == UID_A


def test_同一渠道下重复的客户合并成一行并提示(tmp_path, fake_bitable):
    path = make_template(
        tmp_path,
        clients=[("R001", "GUANGWEI HUANG", UID_A, None), ("R001", "GUANGWEI HUANG", UID_A, None)],
    )

    summary = _sync(path, fake_bitable)

    assert len(_clients(fake_bitable)) == 1
    assert any("GUANGWEI HUANG" in note for note in summary.notes)


def test_同一个UID挂在两个渠道下只认第一个并提示(tmp_path, fake_bitable):
    path = make_template(
        tmp_path,
        clients=[("R001", "ZHENCHAO ZHAN", UID_A, None), ("R003", "SOMEONE ELSE", UID_A, None)],
    )

    summary = _sync(path, fake_bitable)

    (client,) = _clients(fake_bitable)
    assert client[schema.CLIENT_NAME] == "ZHENCHAO ZHAN"
    assert any(UID_A in note for note in summary.notes)


def test_客户引用了不存在的渠道编号时跳过并提示(tmp_path, fake_bitable):
    summary = _sync(
        make_template(tmp_path, clients=[("R999", "NOBODY", UID_B, None)]), fake_bitable
    )

    assert _clients(fake_bitable) == []
    assert any("R999" in note for note in summary.notes)


def test_销售名册按姓名补齐且OpenID留空(tmp_path, fake_bitable):
    """两张表里同一个人写法不同（@James YANG / James Yang），只建一行。"""
    path = make_template(tmp_path, clients=[("R001", "ZHENCHAO ZHAN", UID_A, "James Yang")])

    summary = _sync(path, fake_bitable)

    rows = list(fake_bitable.tables[TBL_SALES].records.values())
    assert summary.sales_created == 2
    assert sorted(r[schema.SALES_NAME] for r in rows) == ["Jackie Cao", "James YANG"]
    assert all(r[schema.SALES_ROLE] == schema.ROLE_SALES for r in rows)
    assert all(r[schema.SALES_STATUS] == schema.SALES_STATUS_ACTIVE for r in rows)
    assert all(not r.get(schema.SALES_OPEN_ID) for r in rows)


def test_预演什么都不写(tmp_path, fake_bitable):
    summary = _sync(
        make_template(tmp_path, clients=[("R001", "ZHENCHAO ZHAN", UID_A, None)]),
        fake_bitable,
        apply=False,
    )

    assert (summary.referrals_created, summary.clients_created, summary.sales_created) == (2, 1, 2)
    assert fake_bitable.write_count == 0
    assert fake_bitable.updates == []


def test_日志里不带客户名和UID(tmp_path, fake_bitable, caplog):
    with caplog.at_level(logging.INFO):
        _sync(
            make_template(tmp_path, clients=[("R001", "ZHENCHAO ZHAN", UID_A, None)]), fake_bitable
        )
    assert UID_A not in caplog.text
    assert "ZHENCHAO" not in caplog.text
