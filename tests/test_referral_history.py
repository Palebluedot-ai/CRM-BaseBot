"""渠道详情卡上的「近 3 个月」。

数字取自两张**汇总表**，不是现算 —— 汇总表是对账写进去的结算快照，就是实际要付的
那个数；现算要扫整张日读看板，压不进卡片回调的 3 秒预算。

所以这里盯两件事：
1. **没结算过的月份要留空**，不能编一个 0 出来 —— 那会让人以为钱已经算好了。
2. **两套账分开**，永远不合并成一个数。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from crm_basebot.domain import ecas, schema
from crm_basebot.domain.referral_history import (
    MonthlyFee,
    ReferralHistoryService,
    recent_months,
)

from .conftest import TBL_COMMISSION, TBL_ECAS_COMMISSION


class Settings:
    table_commission = TBL_COMMISSION
    table_ecas_commission = TBL_ECAS_COMMISSION


TODAY = date(2026, 9, 24)


def trade_row(fake_bitable, period: str, no: str, payable: float) -> None:
    fake_bitable.tables[TBL_COMMISSION].add_existing(
        {
            schema.COMM_PERIOD: period,
            schema.COMM_REFERRAL_NO: no,
            schema.COMM_PAYABLE: payable,
        }
    )


def ecas_row(fake_bitable, period: str, no: str, payable: float) -> None:
    fake_bitable.tables[TBL_ECAS_COMMISSION].add_existing(
        {
            ecas.ECOMM_PERIOD: period,
            ecas.ECOMM_REFERRAL_NO: no,
            ecas.ECOMM_PAYABLE: payable,
        }
    )


def service(fake_bitable, settings=None) -> ReferralHistoryService:
    return ReferralHistoryService(fake_bitable, settings=settings or Settings())


# ---------- 哪三个月 ----------


def test_从上个月往回数三个月():
    """不含本月：本月还没过完也还没结算，列出来只会是一行空的。"""
    assert recent_months(TODAY) == ["2026-08", "2026-07", "2026-06"]


def test_跨年往回数():
    assert recent_months(date(2026, 1, 15)) == ["2025-12", "2025-11", "2025-10"]


# ---------- 取数 ----------


def test_两套账各取各的(fake_bitable):
    trade_row(fake_bitable, "2026-08", "R095", 1234.5)
    ecas_row(fake_bitable, "2026-08", "R095", 60000.0)

    (august, *_rest) = service(fake_bitable).recent("R095", today=TODAY)
    assert august == MonthlyFee("2026-08", Decimal("1234.5"), Decimal("60000"))


def test_没结算过的月份留空不是零(fake_bitable):
    """0 在这里等于宣称「这个月一分没有」，实际是「这个月还没结算」。"""
    ecas_row(fake_bitable, "2026-08", "R095", 60000.0)

    august, july, june = service(fake_bitable).recent("R095", today=TODAY)
    assert august.trade is None
    assert august.ecas == Decimal("60000")
    assert july.is_empty and june.is_empty


def test_只看这条渠道的行(fake_bitable):
    trade_row(fake_bitable, "2026-08", "R095", 100.0)
    trade_row(fake_bitable, "2026-08", "R099", 999.0)

    (august, *_rest) = service(fake_bitable).recent("R095", today=TODAY)
    assert august.trade == Decimal("100")


def test_三个月以外的不取(fake_bitable):
    trade_row(fake_bitable, "2026-05", "R095", 999.0)

    fees = service(fake_bitable).recent("R095", today=TODAY)
    assert [f.period for f in fees] == ["2026-08", "2026-07", "2026-06"]
    assert all(f.is_empty for f in fees)


def test_同月两行就加起来不默默丢掉(fake_bitable):
    """正常一个月一个渠道只有一行。真出现两行是数据问题，但丢掉一行更糟。"""
    trade_row(fake_bitable, "2026-08", "R095", 100.0)
    trade_row(fake_bitable, "2026-08", "R095", 50.0)

    (august, *_rest) = service(fake_bitable).recent("R095", today=TODAY)
    assert august.trade == Decimal("150")


def test_没配ECAS汇总表时那一列全空(fake_bitable):
    """没上 ECAS 的租户照样能看这张卡。"""

    class NoEcas(Settings):
        table_ecas_commission = ""

    trade_row(fake_bitable, "2026-08", "R095", 100.0)
    (august, *_rest) = service(fake_bitable, NoEcas()).recent("R095", today=TODAY)
    assert august.trade == Decimal("100")
    assert august.ecas is None


def test_编号为空时直接返回空(fake_bitable):
    assert service(fake_bitable).recent("", today=TODAY) == []
