"""一条渠道近几个月拿了多少钱 —— 交易佣金和 ECAS 返佣各一列。

给「我的渠道」的详情卡用。销售点进一条渠道，第一眼想看的是「这个渠道最近给我挣了
多少」，不是它的邮箱和提交日期（2026-09-24 反馈）。

**读的是两张汇总表，不是现算。** 汇总表是对账任务写进去的结算快照，就是实际要付的
那个数；现算要扫整张日读看板，几万行压不进卡片回调的 3 秒预算。代价是**没结算过的
月份这里看不到** —— 那是对的：没结算就还没有「应付」这回事，编一个出来只会让人以为
钱已经算好了。

两套账分开列，不合并成一个数。同一个渠道两边的比例可以不一样，合起来就看不出哪笔是
哪笔了（见 ``domain/ecas.py`` 开头）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..lark.bitable import BitableClient
from ..lark.values import extract_text, to_number
from . import ecas, schema

logger = logging.getLogger(__name__)

DEFAULT_MONTHS = 3


@dataclass(frozen=True)
class MonthlyFee:
    """某个月这条渠道的两笔钱。``None`` 表示那套账这个月没有结算记录。"""

    period: str
    trade: Decimal | None = None
    ecas: Decimal | None = None

    @property
    def is_empty(self) -> bool:
        return self.trade is None and self.ecas is None


def recent_months(today: date, count: int = DEFAULT_MONTHS) -> list[str]:
    """从**上个月**往回数 ``count`` 个月，新的在前。

    不含本月：本月还没过完，也还没结算，列出来只会是一行空的
    （对账任务每月 3 号结上个月，见 scripts/monthly_reconcile.py）。
    """
    year, month = today.year, today.month
    out: list[str] = []
    for _ in range(count):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        out.append(f"{year:04d}-{month:02d}")
    return out


def _payable_by_period(
    bitable: BitableClient,
    table_id: str,
    *,
    referral_no: str,
    period_field: str,
    no_field: str,
    payable_field: str,
) -> dict[str, Decimal]:
    """一张汇总表里这条渠道各月的应付。表 id 为空就当没有这套账。"""
    if not table_id:
        return {}
    found: dict[str, Decimal] = {}
    for record in bitable.iter_records(
        table_id, field_names=[period_field, no_field, payable_field]
    ):
        if extract_text(record.fields.get(no_field)) != referral_no:
            continue
        period = extract_text(record.fields.get(period_field))
        if not period:
            continue
        amount = to_number(record.fields.get(payable_field))
        if amount is None:
            continue
        # 同一个月同一个渠道正常只有一行。真有两行就加起来，别默默丢掉一行。
        found[period] = found.get(period, Decimal("0")) + Decimal(str(amount))
    return found


class ReferralHistoryService:
    """按渠道编号查近几个月的两笔钱。只读。

    两张汇总表都很小（每月每渠道一行），各扫一遍就够；不加缓存的理由和
    ``CommissionQueryService`` 一样 —— 刚结算完查不到才是真麻烦。
    """

    def __init__(self, bitable: BitableClient, *, settings) -> None:
        self._bitable = bitable
        self._settings = settings

    def recent(
        self, referral_no: str, *, today: date, months: int = DEFAULT_MONTHS
    ) -> list[MonthlyFee]:
        if not referral_no:
            return []
        trade = _payable_by_period(
            self._bitable,
            self._settings.table_commission,
            referral_no=referral_no,
            period_field=schema.COMM_PERIOD,
            no_field=schema.COMM_REFERRAL_NO,
            payable_field=schema.COMM_PAYABLE,
        )
        book = _payable_by_period(
            self._bitable,
            getattr(self._settings, "table_ecas_commission", ""),
            referral_no=referral_no,
            period_field=ecas.ECOMM_PERIOD,
            no_field=ecas.ECOMM_REFERRAL_NO,
            payable_field=ecas.ECOMM_PAYABLE,
        )
        return [
            MonthlyFee(period=period, trade=trade.get(period), ecas=book.get(period))
            for period in recent_months(today, months)
        ]
