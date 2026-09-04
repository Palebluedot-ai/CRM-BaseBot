"""对账写回汇总表的规则。

--write 会改动结算数据，所以这里盯三件事：同一个月份不许悄悄写出第二套汇总、
--replace 只删本次结算的那些月份、算出来是空的时候不许拿空结果去顶掉旧汇总。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from crm_basebot.domain import schema
from crm_basebot.domain.commission import CommissionRow
from crm_basebot.jobs.reconcile import WriteRefused, build_parser, main, run, write_summary
from crm_basebot.lark.bitable import (
    FIELD_TYPE_DATETIME,
    FIELD_TYPE_NUMBER,
    FIELD_TYPE_TEXT,
    FieldInfo,
)

from .conftest import TBL_AUDIT, TBL_CLIENT, TBL_COMMISSION, TBL_REFERRAL, TBL_TXN

UID = "577809207768677761"


class Settings:
    table_referral = TBL_REFERRAL
    table_client = TBL_CLIENT
    table_transaction = TBL_TXN
    table_commission = TBL_COMMISSION
    table_audit = TBL_AUDIT
    business_timezone = "Asia/Singapore"


def _row(period: str, no: str = "R001") -> CommissionRow:
    return CommissionRow(
        period=period,
        referral_no=no,
        referral_name="ABC Capital",
        rate_percent=Decimal("20"),
        pnl_total=Decimal("100"),
        txn_count=1,
        client_uids={UID},
    )


def _old_summary(fake_bitable, period: str, count: int) -> list[str]:
    """往汇总表里塞几行上一次对账留下的结果，返回 record_id。"""
    table = fake_bitable.tables[TBL_COMMISSION]
    return [
        table.add_existing({schema.COMM_PERIOD: period, schema.COMM_REFERRAL_NO: f"R{i:03d}"})
        for i in range(1, count + 1)
    ]


def _summary_periods(fake_bitable) -> list[str]:
    records = fake_bitable.tables[TBL_COMMISSION].records.values()
    return sorted(r[schema.COMM_PERIOD] for r in records)


# ---------- write_summary：先删后写的规则 ----------


def test_空表时直接写入(fake_bitable):
    deleted, written = write_summary(
        fake_bitable, TBL_COMMISSION, [_row("2026-03")], periods={"2026-03"}, replace=False
    )
    assert (deleted, written) == (0, 1)
    assert _summary_periods(fake_bitable) == ["2026-03"]


def test_同一月份已有汇总时不带replace拒绝写入(fake_bitable):
    _old_summary(fake_bitable, "2026-03", 2)

    with pytest.raises(WriteRefused) as exc_info:
        write_summary(
            fake_bitable, TBL_COMMISSION, [_row("2026-03")], periods={"2026-03"}, replace=False
        )

    assert "2026-03" in str(exc_info.value)
    assert "--replace" in str(exc_info.value)
    assert fake_bitable.write_count == 0
    assert fake_bitable.deleted == []


def test_别的月份有汇总不影响写入(fake_bitable):
    kept = _old_summary(fake_bitable, "2026-02", 1)

    deleted, written = write_summary(
        fake_bitable, TBL_COMMISSION, [_row("2026-03")], periods={"2026-03"}, replace=False
    )

    assert (deleted, written) == (0, 1)
    assert kept[0] in fake_bitable.tables[TBL_COMMISSION].records
    assert _summary_periods(fake_bitable) == ["2026-02", "2026-03"]


def test_replace只删本次结算月份的旧行(fake_bitable):
    kept = _old_summary(fake_bitable, "2026-02", 1)
    stale = _old_summary(fake_bitable, "2026-03", 2)

    deleted, written = write_summary(
        fake_bitable, TBL_COMMISSION, [_row("2026-03")], periods={"2026-03"}, replace=True
    )

    assert (deleted, written) == (2, 1)
    remaining = fake_bitable.tables[TBL_COMMISSION].records
    assert kept[0] in remaining
    assert not any(record_id in remaining for record_id in stale)
    assert _summary_periods(fake_bitable) == ["2026-02", "2026-03"]


def test_先删干净再写(fake_bitable):
    """删和写的顺序不能反：先写再删会把刚写的也删掉。"""
    _old_summary(fake_bitable, "2026-03", 2)

    write_summary(
        fake_bitable, TBL_COMMISSION, [_row("2026-03")], periods={"2026-03"}, replace=True
    )

    assert len(fake_bitable.deleted) == 2
    assert len(fake_bitable.tables[TBL_COMMISSION].records) == 1


def test_全部月份replace清空整张汇总表(fake_bitable):
    _old_summary(fake_bitable, "2026-01", 1)
    _old_summary(fake_bitable, "2026-02", 2)

    deleted, written = write_summary(
        fake_bitable,
        TBL_COMMISSION,
        [_row("2026-01"), _row("2026-02")],
        periods=None,
        replace=True,
    )

    assert (deleted, written) == (3, 2)
    assert _summary_periods(fake_bitable) == ["2026-01", "2026-02"]


def test_算出来是空的时候不拿空结果顶掉旧汇总(fake_bitable):
    """交易明细没导完就跑 --replace，不能把上个月好好的汇总删成空的。"""
    _old_summary(fake_bitable, "2026-03", 2)

    with pytest.raises(WriteRefused) as exc_info:
        write_summary(fake_bitable, TBL_COMMISSION, [], periods={"2026-03"}, replace=True)

    assert "2026-03" in str(exc_info.value)
    assert fake_bitable.deleted == []
    assert len(fake_bitable.tables[TBL_COMMISSION].records) == 2


def test_空结果且没有旧汇总时什么都不做(fake_bitable):
    assert write_summary(fake_bitable, TBL_COMMISSION, [], periods={"2026-03"}, replace=True) == (
        0,
        0,
    )


# ---------- run：命令行入口把规则串起来 ----------


@pytest.fixture
def base(fake_bitable):
    fake_bitable.tables[TBL_TXN].fields = [
        FieldInfo("fld1", schema.TXN_ORDER_TIME, FIELD_TYPE_DATETIME, "DateTime", True),
        FieldInfo("fld2", schema.TXN_CLIENT_UID, FIELD_TYPE_TEXT, "Text", False),
        FieldInfo("fld3", schema.TXN_PNL, FIELD_TYPE_NUMBER, "Number", False),
    ]
    referral = fake_bitable.tables[TBL_REFERRAL].add_existing(
        {
            schema.REFERRAL_NO: "R001",
            schema.REFERRAL_NAME: "ABC Capital",
            schema.REFERRAL_RATE: 20,
            schema.REFERRAL_STATUS: schema.STATUS_ACTIVE,
        }
    )
    fake_bitable.tables[TBL_CLIENT].add_existing(
        {
            schema.CLIENT_UID: UID,
            schema.CLIENT_NAME: "PLUTO STUDIO LIMITED",
            schema.CLIENT_REFERRAL_LINK: [referral],
        }
    )
    fake_bitable.tables[TBL_TXN].add_existing(
        {
            schema.TXN_ORDER_TIME: "2026/03/02",
            schema.TXN_CLIENT_UID: UID,
            schema.TXN_PNL: 729.99,
        }
    )
    return fake_bitable


def _run(base, *argv: str) -> int:
    return run(build_parser().parse_args(list(argv)), Settings(), base)


def test_只算不写时有旧汇总也不碰(base):
    _old_summary(base, "2026-03", 2)

    assert _run(base, "--period", "2026-03") == 0

    assert base.deleted == []
    assert len(base.tables[TBL_COMMISSION].records) == 2


def test_write遇到已有月份以非零退出且不写不删(base, capsys):
    _old_summary(base, "2026-03", 2)

    assert _run(base, "--period", "2026-03", "--write") == 1

    assert "--replace" in capsys.readouterr().out
    assert base.deleted == []
    assert len(base.tables[TBL_COMMISSION].records) == 2
    assert not base.tables[TBL_AUDIT].records, "没写就不该留审计"


def test_write加replace先删后写并记审计(base, capsys):
    stale = _old_summary(base, "2026-03", 2)

    assert _run(base, "--period", "2026-03", "--write", "--replace") == 0

    remaining = base.tables[TBL_COMMISSION].records
    assert not any(record_id in remaining for record_id in stale)
    assert _summary_periods(base) == ["2026-03"]

    (audit,) = base.tables[TBL_AUDIT].records.values()
    detail = json.loads(audit[schema.AUDIT_DETAIL])
    assert detail["删除行数"] == 2
    assert detail["写入行数"] == 1
    assert "2" in capsys.readouterr().out


def test_默认月份也按同样规则拒绝重复写入(base):
    """不传 --period 时结算的是最新月份，旧汇总的检查要落在那个算出来的月份上。"""
    _old_summary(base, "2026-03", 1)

    assert _run(base, "--write") == 1
    assert base.deleted == []


def test_replace不带write是用法错误():
    with pytest.raises(SystemExit) as exc_info:
        main(["--replace"])
    assert exc_info.value.code == 2
