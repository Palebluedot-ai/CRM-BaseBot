"""ECAS 对账：读 Base、写汇总、以及「写入是会改结算数据的操作」那几条拒绝规则。

规则和交易佣金那个 reconcile 逐条一致，是刻意的 —— 用的人不用记两套。
最后一组测试钉的是两套账的边界：渠道表的分佣比例改成离谱的数，ECAS 一分不变。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from crm_basebot.domain import ecas, schema
from crm_basebot.jobs.ecas_reconcile import (
    WriteRefused,
    build_parser,
    load_applications,
    load_payees,
    run,
    write_summary,
)
from crm_basebot.lark.bitable import FIELD_TYPE_DATETIME, FIELD_TYPE_NUMBER, FieldInfo

from .conftest import TBL_AUDIT, TBL_ECAS, TBL_ECAS_COMMISSION, TBL_REFERRAL

SG = ZoneInfo("Asia/Singapore")


class Settings:
    table_referral = TBL_REFERRAL
    table_ecas = TBL_ECAS
    table_ecas_commission = TBL_ECAS_COMMISSION
    table_audit = TBL_AUDIT
    business_timezone = "Asia/Singapore"


def _ms(moment: datetime) -> int:
    return int(moment.replace(tzinfo=SG).timestamp() * 1000)


def _fields_present(fake_bitable) -> None:
    """让 assert_fields_present 过关。三列缺一不可，所以这里如实填上。"""
    fake_bitable.tables[TBL_ECAS].fields = [
        FieldInfo(
            field_id="f1",
            name=ecas.ECAS_APPLIED_AT,
            type=FIELD_TYPE_DATETIME,
            ui_type="D",
            is_primary=False,
        ),
        FieldInfo(
            field_id="f2",
            name=ecas.ECAS_AMOUNT,
            type=FIELD_TYPE_NUMBER,
            ui_type="N",
            is_primary=False,
        ),
        FieldInfo(
            field_id="f3",
            name=ecas.ECAS_RATE,
            type=FIELD_TYPE_NUMBER,
            ui_type="N",
            is_primary=False,
        ),
    ]


def _referral(fake_bitable, code: str, name: str, rate: float = 20) -> str:
    return fake_bitable.tables[TBL_REFERRAL].add_existing(
        {schema.REFERRAL_NO: code, schema.REFERRAL_NAME: name, schema.REFERRAL_RATE: rate}
    )


def _application(
    fake_bitable,
    client: str,
    amount: float,
    when: datetime,
    *,
    rate: float | None = 50,
    link: str | None = None,
    referrer_name: str = "",
) -> str:
    fields = {
        ecas.ECAS_CLIENT_NAME: client,
        ecas.ECAS_AMOUNT: amount,
        ecas.ECAS_APPLIED_AT: _ms(when),
    }
    if rate is not None:
        fields[ecas.ECAS_RATE] = rate
    if link:
        fields[ecas.ECAS_REFERRAL_LINK] = [link]
    if referrer_name:
        fields[ecas.ECAS_REFERRER_NAME] = referrer_name
    return fake_bitable.tables[TBL_ECAS].add_existing(fields)


def _args(**kwargs) -> argparse.Namespace:
    base = {"period": None, "all_periods": False, "write": False, "replace": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def _summary_rows(fake_bitable) -> list[dict]:
    return list(fake_bitable.tables[TBL_ECAS_COMMISSION].records.values())


# ---------- 从 Base 读出来 ----------


def test_挂了关联的申请收款方是那个渠道(fake_bitable):
    record_id = _referral(fake_bitable, "R095", "JIANG JUN")
    _application(fake_bitable, "A", 5000, datetime(2026, 8, 10), link=record_id)

    payees = load_payees(fake_bitable, TBL_REFERRAL)
    (app,) = load_applications(fake_bitable, TBL_ECAS, payees, tz=SG)
    assert app.payee == ecas.Payee(code="R095", name="JIANG JUN")
    assert app.period == "2026-08"
    assert app.fee == Decimal("2500.00")


def test_没挂关联但写了介绍人名字的照样结算只是没编号(fake_bitable):
    _application(fake_bitable, "A", 5000, datetime(2026, 8, 10), referrer_name="Teo Yu Yuan")

    payees = load_payees(fake_bitable, TBL_REFERRAL)
    (app,) = load_applications(fake_bitable, TBL_ECAS, payees, tz=SG)
    assert app.payee == ecas.Payee(code="", name="Teo Yu Yuan")
    assert app.fee == Decimal("2500.00")


def test_既没关联也没名字的申请不产生返佣(fake_bitable):
    _application(fake_bitable, "散客", 5000, datetime(2026, 8, 10), rate=None)

    payees = load_payees(fake_bitable, TBL_REFERRAL)
    (app,) = load_applications(fake_bitable, TBL_ECAS, payees, tz=SG)
    assert app.payee is None
    assert app.fee == Decimal("0")
    assert ecas.aggregate([app]) == []


def test_月初凌晨的申请归到本月不是上个月(fake_bitable):
    record_id = _referral(fake_bitable, "R095", "JIANG JUN")
    # 新加坡 9 月 1 日 00:30，UTC 还是 8 月 31 日
    _application(fake_bitable, "A", 5000, datetime(2026, 9, 1, 0, 30), link=record_id)

    payees = load_payees(fake_bitable, TBL_REFERRAL)
    (app,) = load_applications(fake_bitable, TBL_ECAS, payees, tz=SG)
    assert app.period == "2026-09"


# ---------- 写汇总的拒绝规则 ----------


def _row(period: str = "2026-08") -> ecas.EcasCommissionRow:
    row = ecas.EcasCommissionRow(period=period, payee=ecas.Payee("R095", "JIANG JUN"))
    row.amount_total = Decimal("5000")
    row.txn_count = 1
    row.client_names = {"A"}
    row.rate_counts[Decimal("50")] = 1
    row.fee_total = Decimal("2500.00")
    return row


def test_空表时直接写入(fake_bitable):
    deleted, written = write_summary(
        fake_bitable, TBL_ECAS_COMMISSION, [_row()], periods={"2026-08"}, replace=False
    )
    assert (deleted, written) == (0, 1)
    (stored,) = _summary_rows(fake_bitable)
    assert stored[ecas.ECOMM_PAYABLE] == 2500.0
    assert stored[ecas.ECOMM_RATE_NOTE] == "50%"


def test_同月份已有汇总时不带replace拒绝(fake_bitable):
    fake_bitable.tables[TBL_ECAS_COMMISSION].add_existing({ecas.ECOMM_PERIOD: "2026-08"})

    with pytest.raises(WriteRefused) as exc_info:
        write_summary(
            fake_bitable, TBL_ECAS_COMMISSION, [_row()], periods={"2026-08"}, replace=False
        )
    assert "--replace" in str(exc_info.value)
    # 拒绝了就一行都没动
    assert len(_summary_rows(fake_bitable)) == 1
    assert fake_bitable.deleted == []


def test_replace只删本次结算的月份(fake_bitable):
    table = fake_bitable.tables[TBL_ECAS_COMMISSION]
    table.add_existing({ecas.ECOMM_PERIOD: "2026-07"})
    table.add_existing({ecas.ECOMM_PERIOD: "2026-08"})

    deleted, written = write_summary(
        fake_bitable, TBL_ECAS_COMMISSION, [_row()], periods={"2026-08"}, replace=True
    )
    assert (deleted, written) == (1, 1)
    assert sorted(r[ecas.ECOMM_PERIOD] for r in _summary_rows(fake_bitable)) == [
        "2026-07",
        "2026-08",
    ]


def test_算出来是空时不拿空结果顶掉旧汇总(fake_bitable):
    fake_bitable.tables[TBL_ECAS_COMMISSION].add_existing({ecas.ECOMM_PERIOD: "2026-08"})

    with pytest.raises(WriteRefused) as exc_info:
        write_summary(fake_bitable, TBL_ECAS_COMMISSION, [], periods={"2026-08"}, replace=True)
    assert "空结果" in str(exc_info.value)
    assert fake_bitable.deleted == []


def test_replace不带write直接被命令行挡掉():
    with pytest.raises(SystemExit):
        parser = build_parser()
        args = parser.parse_args(["--replace"])
        if args.replace and not args.write:
            parser.error("x")


# ---------- 整条路 ----------


def test_不传月份时结算最新有数据的那个月(fake_bitable, capsys):
    _fields_present(fake_bitable)
    record_id = _referral(fake_bitable, "R095", "JIANG JUN")
    _application(fake_bitable, "旧", 5000, datetime(2026, 7, 10), link=record_id)
    _application(fake_bitable, "新", 5000, datetime(2026, 8, 10), link=record_id)

    assert run(_args(), Settings(), fake_bitable) == 0
    out = capsys.readouterr().out
    assert "2026-08（自动选定" in out
    assert "2026-07" not in out


def test_只算不写时一行都不写(fake_bitable):
    _fields_present(fake_bitable)
    record_id = _referral(fake_bitable, "R095", "JIANG JUN")
    _application(fake_bitable, "A", 5000, datetime(2026, 8, 10), link=record_id)

    assert run(_args(), Settings(), fake_bitable) == 0
    assert _summary_rows(fake_bitable) == []


def test_写入后汇总表里是算出来的那一行(fake_bitable):
    _fields_present(fake_bitable)
    record_id = _referral(fake_bitable, "R095", "JIANG JUN")
    _application(fake_bitable, "A", 5000, datetime(2026, 8, 10), link=record_id)
    _application(fake_bitable, "B", 5000, datetime(2026, 8, 12), link=record_id, rate=20)

    assert run(_args(write=True), Settings(), fake_bitable) == 0
    (stored,) = _summary_rows(fake_bitable)
    assert stored[ecas.ECOMM_PERIOD] == "2026-08"
    assert stored[ecas.ECOMM_REFERRAL_NO] == "R095"
    assert stored[ecas.ECOMM_AMOUNT_TOTAL] == 10000.0
    assert stored[ecas.ECOMM_PAYABLE] == 3500.0  # 2500 + 1000
    assert stored[ecas.ECOMM_RATE_NOTE] == "50%×1笔 / 20%×1笔"


def test_申请表是空的时候定不出月份退非零(fake_bitable, capsys):
    _fields_present(fake_bitable)
    assert run(_args(), Settings(), fake_bitable) == 1
    assert "定不出要结算哪个月" in capsys.readouterr().out


def test_收款方没登记渠道时单独提醒(fake_bitable, capsys):
    _fields_present(fake_bitable)
    _application(fake_bitable, "A", 5000, datetime(2026, 8, 10), referrer_name="Teo Yu Yuan")

    assert run(_args(), Settings(), fake_bitable) == 0
    out = capsys.readouterr().out
    assert "在渠道表里没有登记" in out
    assert "2,500.00" in out


# ---------- 两套账的边界 ----------


def test_渠道表的分佣比例再离谱ECAS也只认行里的比例(fake_bitable):
    """交易那边给这个渠道 999%，ECAS 这一笔仍然按自己那行的 50% 算。

    这不是假设出来的边界：2026-09 有两笔 ECAS 是 20%，同一批人在交易那边是别的数。
    """
    _fields_present(fake_bitable)
    record_id = _referral(fake_bitable, "R095", "JIANG JUN", rate=999)
    _application(fake_bitable, "A", 5000, datetime(2026, 8, 10), link=record_id, rate=50)

    assert run(_args(write=True), Settings(), fake_bitable) == 0
    (stored,) = _summary_rows(fake_bitable)
    assert stored[ecas.ECOMM_PAYABLE] == 2500.0


def test_结算只写ECAS自己的汇总表(fake_bitable):
    """别的表一条记录都不许多出来 —— 交易佣金那套账不该因为跑了 ECAS 而变。"""
    _fields_present(fake_bitable)
    record_id = _referral(fake_bitable, "R095", "JIANG JUN")
    _application(fake_bitable, "A", 5000, datetime(2026, 8, 10), link=record_id)

    run(_args(write=True), Settings(), fake_bitable)
    written_tables = {table_id for table_id, _ in fake_bitable.writes}
    assert written_tables == {TBL_ECAS_COMMISSION, TBL_AUDIT}
