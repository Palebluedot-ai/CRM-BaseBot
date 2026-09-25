"""交易佣金只付给 AI 客户带来的交易（2026-09-25 定的规则）。

规则一句话：**客户成为 AI 的那个月起，整月都算；之前的月份不算；只管 2026-09 起的结算；
以前登记、没填 AI 那两列的客户照旧算。**

月结、佣金查询、渠道详情卡三处都走 ``AiEligibility.counts``，这里除了规则本身，
还各钉一条「这一处真的用了它」。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.domain import schema
from crm_basebot.domain.ai_status import RULE_START, AiEligibility, eligibility_of
from crm_basebot.domain.commission import CommissionCalculator
from crm_basebot.domain.commission_query import CommissionQueryService
from crm_basebot.domain.dates import DEFAULT_BUSINESS_TIMEZONE, date_to_ms
from crm_basebot.domain.referral_history import ReferralHistoryService

from .conftest import TBL_BOARD, TBL_CLIENT, TBL_ECAS, TBL_REFERRAL

SGT = DEFAULT_BUSINESS_TIMEZONE
UPGRADED = AiEligibility(status=schema.AI_STATUS_UPGRADED, since="2026-10")


# ---------- 规则本身 ----------


def test_规则从九月开始():
    assert RULE_START == "2026-09"


@pytest.mark.parametrize("period", ["2026-08", "2026-01"])
def test_八月及以前永远照旧(period):
    """已经结算付款的月份，任何时候重算都不能因为新规则变。"""
    assert AiEligibility(status=schema.AI_STATUS_NOT).counts(period)
    assert UPGRADED.counts(period)


def test_升级那个月整月都算():
    """9 月 10 号才升级，9 月 1–9 号的交易也有佣金 —— 按月，不按天。"""
    ai = AiEligibility(status=schema.AI_STATUS_UPGRADED, since="2026-09")
    assert ai.counts("2026-09")
    assert ai.counts("2026-10")


def test_升级之前的月份不算():
    assert not UPGRADED.counts("2026-09")
    assert UPGRADED.counts("2026-10")
    assert UPGRADED.counts("2027-01")


def test_非AI不算():
    assert not AiEligibility(status=schema.AI_STATUS_NOT).counts("2026-09")


def test_选了升级但日期还没补的先不算():
    assert not AiEligibility(status=schema.AI_STATUS_UPGRADED).counts("2026-09")


@pytest.mark.parametrize("status", ["", schema.AI_STATUS_ALREADY])
def test_老客户和开户即AI照算(status):
    assert AiEligibility(status=status).counts("2026-09")


def test_从客户表的一行读出资格():
    fields = {
        schema.CLIENT_AI_STATUS: schema.AI_STATUS_UPGRADED,
        schema.CLIENT_AI_DATE: date_to_ms(date(2026, 8, 24), tz=SGT),
    }
    assert eligibility_of(fields, tz=SGT) == AiEligibility(schema.AI_STATUS_UPGRADED, "2026-08")


def test_月初零点按业务时区归月():
    """9 月 1 日业务时区零点 = 8 月 31 日 16:00 UTC，要算 9 月。"""
    fields = {schema.CLIENT_AI_DATE: date_to_ms(date(2026, 9, 1), tz=SGT)}
    assert eligibility_of(fields, tz=SGT).since == "2026-09"


def test_老表没有那两列时照旧算():
    assert eligibility_of({}, tz=SGT).counts("2026-12")


# ---------- 三处都用了它 ----------

UID_OLD = "577809207768677761"  # 老客户，没填 AI
UID_LATE = "577809207768677762"  # 10 月才升级


class Settings:
    table_referral = TBL_REFERRAL
    table_client = TBL_CLIENT
    table_daily_board = TBL_BOARD
    table_ecas = TBL_ECAS
    business_timezone = "Asia/Singapore"


@pytest.fixture
def base(fake_bitable):
    referral = fake_bitable.tables[TBL_REFERRAL].add_existing(
        {
            schema.REFERRAL_NO: "R095",
            schema.REFERRAL_NAME: "JIANG JUN",
            schema.REFERRAL_RATE: 20,
            schema.REFERRAL_OWNER_OPEN_ID: "ou_prance",
        }
    )
    clients = fake_bitable.tables[TBL_CLIENT]
    clients.add_existing(
        {
            schema.CLIENT_UID: UID_OLD,
            schema.CLIENT_NAME: "老客户",
            schema.CLIENT_REFERRAL_LINK: [referral],
        }
    )
    clients.add_existing(
        {
            schema.CLIENT_UID: UID_LATE,
            schema.CLIENT_NAME: "十月才升级",
            schema.CLIENT_REFERRAL_LINK: [referral],
            schema.CLIENT_AI_STATUS: schema.AI_STATUS_UPGRADED,
            schema.CLIENT_AI_DATE: date_to_ms(date(2026, 10, 5), tz=SGT),
        }
    )
    for uid in (UID_OLD, UID_LATE):
        for day in ("2026/08/15", "2026/09/15", "2026/10/15"):
            fake_bitable.tables[TBL_BOARD].add_existing(
                {
                    schema.BOARD_CLIENT_UID: uid,
                    schema.BOARD_ORDER_DATE: day,
                    schema.BOARD_TOTAL_REVENUE: 1000.0,
                }
            )
    fake_bitable.r095 = referral
    return fake_bitable


def test_月结不算升级之前的月份(base):
    rows, _ = CommissionCalculator(base, settings=Settings()).compute()
    revenue = {row.period: row.revenue_total for row in rows}
    assert revenue == {
        "2026-08": Decimal("2000"),  # 八月照旧：两个都算
        "2026-09": Decimal("1000"),  # 九月：十月才升级的那个不算
        "2026-10": Decimal("2000"),  # 十月：升级那个月整月都算
    }


def test_月结说出哪些因为不是AI没算(base):
    calculator = CommissionCalculator(base, settings=Settings())
    calculator.compute()
    assert calculator.excluded_not_ai == {("2026-09", UID_LATE)}


def test_佣金查询不算升级之前的月份(base):
    prance = Sales(open_id="ou_prance", name="Prance", role=schema.ROLE_SALES, is_active=True)
    result = CommissionQueryService(base, settings=Settings()).query(
        prance, ["2026-08", "2026-09", "2026-10"]
    )
    assert [result.total_payable(p) for p in result.periods] == [
        Decimal("400.00"),
        Decimal("200.00"),
        Decimal("400.00"),
    ]


def test_渠道详情卡不算升级之前的月份(base):
    months = ReferralHistoryService(base, settings=Settings()).recent(
        base.r095, today=date(2026, 10, 20)
    )
    by_period = {m.period: m for m in months}
    assert [c.name for c in by_period["2026-09"].clients] == ["老客户"]
    assert by_period["2026-10"].trade == Decimal("400.00")
