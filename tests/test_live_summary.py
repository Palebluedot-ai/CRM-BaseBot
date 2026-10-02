"""每天导入后刷新结算表里进行中的月份（jobs/live_summary.py）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from crm_basebot.domain import ecas, schema
from crm_basebot.domain.commission import CommissionRow
from crm_basebot.jobs import live_summary

from .conftest import TBL_COMMISSION, TBL_ECAS, TBL_ECAS_COMMISSION, TBL_REFERRAL


def test_月初上个月还没结_两个月都刷新_结了就只刷这个月():
    assert live_summary.open_months(date(2026, 10, 1), {"2026-08"}) == ["2026-09", "2026-10"]
    assert live_summary.open_months(date(2026, 10, 2), {"2026-09"}) == ["2026-10"]
    assert live_summary.open_months(date(2027, 1, 5), set()) == ["2026-12", "2027-01"]


def _row(period):
    return CommissionRow(
        period=period,
        referral_no="R001",
        referral_name="A",
        rate_percent=Decimal("20"),
        revenue_total=Decimal("100"),
        txn_count=1,
        client_uids={"1"},
    )


def test_刷新只写还没结算的月份_结过的一行不动(fake_bitable, monkeypatch):
    fake_bitable.tables[TBL_COMMISSION].add_existing(
        {schema.COMM_PERIOD: "2026-09", schema.COMM_PAYABLE: 1.0}  # 已结算
    )

    class Calc:
        def __init__(self, *_a, **_k):
            pass

        def compute(self, *, period):
            return [_row(period)], []

    ecas_row = ecas.EcasCommissionRow(period="2026-10", payee=ecas.Payee("R095", "JIANG JUN"))
    ecas_row.fee_total = Decimal("2500")
    monkeypatch.setattr(live_summary, "CommissionCalculator", Calc)
    monkeypatch.setattr(live_summary, "load_payees", lambda *_a: {})
    monkeypatch.setattr(live_summary, "load_applications", lambda *_a, **_k: [])
    monkeypatch.setattr(
        live_summary.ecas,
        "aggregate",
        lambda _apps, period: [ecas_row] if period == "2026-10" else [],
    )
    settings = SimpleNamespace(
        table_commission=TBL_COMMISSION,
        table_ecas=TBL_ECAS,
        table_ecas_commission=TBL_ECAS_COMMISSION,
        table_referral=TBL_REFERRAL,
        business_timezone="Asia/Singapore",
    )
    assert live_summary.refresh(settings, fake_bitable, date(2026, 10, 2)) == 2

    trade = sorted(
        (r[schema.COMM_PERIOD], r.get(schema.COMM_STATUS, ""))
        for r in fake_bitable.tables[TBL_COMMISSION].records.values()
    )
    assert trade == [("2026-09", ""), ("2026-10", "进行中")]
    (stored,) = fake_bitable.tables[TBL_ECAS_COMMISSION].records.values()
    assert stored[ecas.ECOMM_STATUS] == "进行中"
