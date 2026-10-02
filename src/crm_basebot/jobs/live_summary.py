"""每天导入之后，把还没结算的月份写进两张结算表，标「进行中」。

2026-10-02 起（超哥：不用等 3 号，每天更新）：仪表盘的「该付」表当月也看得到，每天变。
哪些月份算「还没结算」：这个月，加上上个月 —— 如果交易佣金结算表里上个月还没有已结算的行
（每月 1 号 16:30 月结之前）。一个月结没结，以交易佣金那张为准，ECAS 跟着走。

和月结是同一套算法（``CommissionCalculator`` / ``ecas.aggregate``），只是写成「进行中」、
每次覆盖。已经结算的月份一行都不碰：``write_summary(live=True)`` 碰到已结算的行会拒绝。
不写审计表 —— 一天两次的临时数，记下来只是噪音；结算那一次照记。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from ..domain import ecas
from ..domain.commission import CommissionCalculator
from ..domain.ecas_query import load_applications, load_payees
from ..domain.settlement import referral_owners
from ..lark.bitable import BitableClient
from . import ecas_reconcile, reconcile
from .archive import settled_periods

logger = logging.getLogger(__name__)


def previous_month(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def open_months(today: date, settled: set[str]) -> list[str]:
    """要每天刷新的月份：上个月（还没结的话）和这个月。"""
    current = f"{today.year}-{today.month:02d}"
    return [m for m in (previous_month(current), current) if m not in settled]


def refresh(settings: Any, bitable: BitableClient, today: date) -> int:
    """刷新两张结算表里进行中的月份，返回写了几行。"""
    months = open_months(today, settled_periods(bitable, settings.table_commission))
    owners = referral_owners(bitable, settings.table_referral) if months else {}
    written = 0
    for month in months:
        rows, _ = CommissionCalculator(bitable, settings=settings).compute(period=month)
        try:
            written += reconcile.write_summary(
                bitable,
                settings.table_commission,
                rows,
                periods={month},
                replace=False,
                live=True,
                owners=owners,
            )[1]
        except reconcile.WriteRefused as exc:
            logger.info("交易佣金 %s 不刷新：%s", month, exc)

    if settings.table_ecas and getattr(settings, "table_ecas_commission", ""):
        tz = ZoneInfo(settings.business_timezone)
        applications = load_applications(
            bitable, settings.table_ecas, load_payees(bitable, settings.table_referral), tz=tz
        )
        for month in months:
            rows = ecas.aggregate(applications, period=month)
            try:
                written += ecas_reconcile.write_summary(
                    bitable,
                    settings.table_ecas_commission,
                    rows,
                    periods={month},
                    replace=False,
                    live=True,
                    owners=owners,
                )[1]
            except ecas_reconcile.WriteRefused as exc:
                logger.info("ECAS %s 不刷新：%s", month, exc)
    return written
