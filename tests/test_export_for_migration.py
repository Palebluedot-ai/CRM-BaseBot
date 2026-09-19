"""不交换凭证的交接：把源 Base 导出成「现成导入器能读」的 xlsx。

这条路的价值是「什么凭证都不用给」。代价是它把 Base 的形状和导入器的形状绑在一起了，
所以测试盯的就是**这两边的契约**：导出的表头必须正好是导入器认的那几个、日期必须是真日期
（不是毫秒数字）、客户的渠道必须从关联翻成**渠道编号**（导入端只认编号）。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from crm_basebot.domain import schema
from crm_basebot.domain.dates import date_to_ms

SGT = ZoneInfo("Asia/Singapore")


def _load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "export_for_migration.py"
    spec = importlib.util.spec_from_file_location("export_for_migration", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


exporter = _load_module()


def _record(record_id: str, **fields):
    return SimpleNamespace(record_id=record_id, fields=fields)


def test_表头就是导入器认的那几个():
    """导入器按规整后的小写认表头；改这里之前先看 import_registrations.py。"""
    assert exporter.REFERRAL_HEADERS == [
        "Referral Code",
        "Name",
        "Email",
        "Start Date",
        "Commission Rate",
        "Payout Frequency",
        "Submitted On",
        "Sales In Charge",
    ]
    assert exporter.CLIENT_HEADERS == ["Referral Code", "Client Name", "UID", "Sales In Charge"]
    # 邮箱列靠「含 email / @ / 邮箱」认出来
    assert any("email" in h.lower() for h in exporter.REFERRAL_HEADERS)


def test_看板表头等于看板列_且日期列按类型码推():
    assert exporter.BOARD_HEADERS == list(schema.DAILY_BOARD_FIELDS)
    # 至少要有「交易日期」和「KYC日期」两个日期列 —— 只按列名写死会漏掉后者
    assert schema.BOARD_ORDER_DATE in exporter.BOARD_DATE_COLUMNS


def test_渠道导出把毫秒时间戳转成真日期():
    rows = exporter.referral_rows(
        [
            _record(
                "rec1",
                **{
                    schema.REFERRAL_NO: [{"text": "R007", "type": "text"}],
                    schema.REFERRAL_NAME: [{"text": "ABC Capital", "type": "text"}],
                    schema.REFERRAL_EMAIL: [{"text": "a@b.com", "type": "text"}],
                    schema.REFERRAL_START_DATE: date_to_ms(datetime(2026, 3, 1).date(), tz=SGT),
                    schema.REFERRAL_RATE: 20,
                    schema.REFERRAL_PAYOUT: [{"text": "Monthly", "type": "text"}],
                    schema.REFERRAL_SUBMITTED_ON: date_to_ms(datetime(2026, 3, 2).date(), tz=SGT),
                    schema.REFERRAL_SALES_NAME: [{"text": "James YANG", "type": "text"}],
                },
            )
        ],
        tz=SGT,
    )

    assert rows == [
        [
            "R007",
            "ABC Capital",
            "a@b.com",
            datetime(2026, 3, 1),
            20.0,
            "Monthly",
            datetime(2026, 3, 2),
            "James YANG",
        ]
    ]


def test_客户导出的渠道列是编号不是记录id():
    """关联字段存的是 record_id，导入端只认编号 —— 必须翻过来。"""
    rows = exporter.client_rows(
        [
            _record(
                "recC",
                **{
                    schema.CLIENT_NAME: [{"text": "PLUTO STUDIO", "type": "text"}],
                    schema.CLIENT_UID: [{"text": "123456789012345678", "type": "text"}],
                    schema.CLIENT_REFERRAL_LINK: {"link_record_ids": ["recChannel"]},
                    schema.CLIENT_SALES_NAME: [{"text": "James Yang", "type": "text"}],
                },
            )
        ],
        channel_no_by_id={"recChannel": "R090"},
    )

    assert rows[0][0] == "R090"
    assert rows[0][1] == "PLUTO STUDIO"


def test_挂不上渠道的客户导出成空编号而不是崩掉():
    rows = exporter.client_rows(
        [_record("recC", **{schema.CLIENT_NAME: "独行客户"})],
        channel_no_by_id={},
    )
    assert rows[0][0] == ""


def test_看板导出摊平富文本并转日期():
    rows = exporter.board_rows(
        [
            _record(
                "recB",
                **{
                    schema.BOARD_ORDER_DATE: date_to_ms(datetime(2026, 9, 18).date(), tz=SGT),
                    schema.BOARD_STATION: [{"text": "新加坡站", "type": "text"}],
                    schema.BOARD_TOTAL_REVENUE: 488.85,
                },
            )
        ],
        tz=SGT,
    )

    row = rows[0]
    assert row[exporter.BOARD_HEADERS.index(schema.BOARD_STATION)] == "新加坡站"
    assert row[exporter.BOARD_HEADERS.index(schema.BOARD_ORDER_DATE)] == datetime(2026, 9, 18)
    assert row[exporter.BOARD_HEADERS.index(schema.BOARD_TOTAL_REVENUE)] == 488.85


def test_导出的文件能被openpyxl读回_表名也对(tmp_path):
    from openpyxl import Workbook, load_workbook

    book = Workbook()
    exporter._write_sheet(
        book, exporter.SHEET_REFERRALS, exporter.REFERRAL_HEADERS, [["R001", "ABC"]], first=True
    )
    exporter._write_sheet(
        book,
        exporter.SHEET_CLIENTS,
        exporter.CLIENT_HEADERS,
        [["R001", "客户", "123"]],
        first=False,
    )
    out = tmp_path / "handover.xlsx"
    book.save(out)

    loaded = load_workbook(out)
    assert loaded.sheetnames == [exporter.SHEET_REFERRALS, exporter.SHEET_CLIENTS]
    assert [cell.value for cell in loaded[exporter.SHEET_REFERRALS][1]] == exporter.REFERRAL_HEADERS
