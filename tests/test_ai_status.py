"""交易佣金只付给 AI 客户带来的交易（2026-09-26 JY 定的规则）。

规则一句话：**升级 AI 当天起的交易就算；开户即AI 全算；非AI 不算；只管 2026-09-01 起的
交易；以前登记、没填 AI 那两列的客户照旧算。**

月结、佣金查询、渠道详情卡三处都走 ``commission.trade_counts``，这里除了规则本身，
还各钉一条「这一处真的用了它」。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.domain import schema
from crm_basebot.domain.ai_status import (
    GATE_ALWAYS,
    GATE_NEVER,
    RULE_START_DAY,
    AiEligibility,
    day_number,
    eligibility_of,
)
from crm_basebot.domain.commission import CommissionCalculator, trade_counts
from crm_basebot.domain.commission_query import CommissionQueryService
from crm_basebot.domain.dates import DEFAULT_BUSINESS_TIMEZONE, date_to_ms
from crm_basebot.domain.referral_history import ReferralHistoryService

from .conftest import TBL_BOARD, TBL_CLIENT, TBL_ECAS, TBL_REFERRAL

SGT = DEFAULT_BUSINESS_TIMEZONE
SEP_10 = AiEligibility(status=schema.AI_STATUS_UPGRADED, upgraded_on=date(2026, 9, 10))


# ---------- 规则本身 ----------


def test_规则从九月一号的交易开始():
    assert RULE_START_DAY == date(2026, 9, 1)
    assert day_number(RULE_START_DAY) == schema.AI_RULE_START_NUMBER  # Base 公式里那个数


@pytest.mark.parametrize("day", [date(2026, 8, 31), date(2026, 1, 5)])
def test_八月及以前永远照旧(day):
    """已经结算付款的月份，任何时候重算都不能因为新规则变。"""
    assert AiEligibility(status=schema.AI_STATUS_NOT).counts(day)
    assert SEP_10.counts(day)


def test_升级当天起就算():
    """9 月 10 号升级，9 月 1–9 号不能有佣金，10 号当天开始有（2026-10-02 改，原先是 11 号起）。"""
    assert not SEP_10.counts(date(2026, 9, 1))
    assert not SEP_10.counts(date(2026, 9, 9))
    assert SEP_10.counts(date(2026, 9, 10))  # 升级当天就算
    assert SEP_10.counts(date(2026, 9, 11))
    assert SEP_10.counts(date(2026, 10, 1))


def test_八月升级的九月全算():
    ai = AiEligibility(status=schema.AI_STATUS_UPGRADED, upgraded_on=date(2026, 8, 24))
    assert ai.counts(date(2026, 9, 1))


def test_非AI不算():
    assert not AiEligibility(status=schema.AI_STATUS_NOT).counts(date(2026, 9, 15))


def test_选了升级但日期还没补的先不算():
    assert not AiEligibility(status=schema.AI_STATUS_UPGRADED).counts(date(2026, 9, 15))


@pytest.mark.parametrize("status", ["", schema.AI_STATUS_ALREADY])
def test_老客户和开户即AI照算(status):
    assert AiEligibility(status=status).counts(date(2026, 9, 15))


def test_开户即AI填了日期也全算():
    ai = AiEligibility(status=schema.AI_STATUS_ALREADY, upgraded_on=date(2026, 9, 20))
    assert ai.counts(date(2026, 9, 2))


@pytest.mark.parametrize(
    ("ai", "gate"),
    [
        (AiEligibility(), GATE_ALWAYS),
        (AiEligibility(status=schema.AI_STATUS_ALREADY), GATE_ALWAYS),
        (SEP_10, 20260910),
        (AiEligibility(status=schema.AI_STATUS_UPGRADED), GATE_NEVER),
        (AiEligibility(status=schema.AI_STATUS_NOT), GATE_NEVER),
    ],
)
def test_门槛数和Base公式同一个口径(ai, gate):
    """Base 的「AI佣金起算」公式算出同一个数；自检拿它逐行对。"""
    assert ai.gate_number() == gate


def test_从客户表的一行读出资格():
    fields = {
        schema.CLIENT_AI_STATUS: schema.AI_STATUS_UPGRADED,
        schema.CLIENT_AI_DATE: date_to_ms(date(2026, 8, 24), tz=SGT),
    }
    assert eligibility_of(fields, tz=SGT) == AiEligibility(
        schema.AI_STATUS_UPGRADED, date(2026, 8, 24)
    )


def test_日期按业务时区取():
    """9 月 1 日业务时区零点 = 8 月 31 日 16:00 UTC，要算 9 月 1 日。"""
    fields = {schema.CLIENT_AI_DATE: date_to_ms(date(2026, 9, 1), tz=SGT)}
    assert eligibility_of(fields, tz=SGT).upgraded_on == date(2026, 9, 1)


def test_老表没有那两列时照旧算():
    assert eligibility_of({}, tz=SGT).counts(date(2026, 12, 1))


def test_一笔交易按它自己那天判():
    assert not trade_counts(SEP_10, date_to_ms(date(2026, 9, 9), tz=SGT), tz=SGT)
    assert trade_counts(SEP_10, date_to_ms(date(2026, 9, 10), tz=SGT), tz=SGT)
    assert trade_counts(None, date_to_ms(date(2026, 9, 1), tz=SGT), tz=SGT)


# ---------- 三处都用了它 ----------

UID_OLD = "577809207768677761"  # 老客户，没填 AI
UID_LATE = "577809207768677762"  # 10 月 5 日才升级
UID_MID = "577809207768677763"  # 9 月 10 日升级


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
    clients.add_existing(
        {
            schema.CLIENT_UID: UID_MID,
            schema.CLIENT_NAME: "九月十号升级",
            schema.CLIENT_REFERRAL_LINK: [referral],
            schema.CLIENT_AI_STATUS: schema.AI_STATUS_UPGRADED,
            schema.CLIENT_AI_DATE: date_to_ms(date(2026, 9, 10), tz=SGT),
        }
    )
    for day in ("2026/09/05", "2026/09/09", "2026/09/10"):  # 只有升级当天那笔算
        fake_bitable.tables[TBL_BOARD].add_existing(
            {
                schema.BOARD_CLIENT_UID: UID_MID,
                schema.BOARD_ORDER_DATE: day,
                schema.BOARD_TOTAL_REVENUE: 100.0,
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


def test_月结不算升级之前的交易(base):
    rows, _ = CommissionCalculator(base, settings=Settings()).compute()
    revenue = {row.period: row.revenue_total for row in rows}
    assert revenue == {
        "2026-08": Decimal("2000"),  # 八月照旧：两个都算
        "2026-09": Decimal("1100"),  # 老客户 1000 + 九月十号升级的只算 11 号那笔 100
        "2026-10": Decimal("2000"),  # 十月五号升级的，15 号的交易算
    }


def test_月结说出哪些因为不是AI没算(base):
    calculator = CommissionCalculator(base, settings=Settings())
    calculator.compute()
    assert calculator.excluded_not_ai == {("2026-09", UID_LATE), ("2026-09", UID_MID)}


def test_佣金查询不算升级之前的交易(base):
    prance = Sales(open_id="ou_prance", name="Prance", role=schema.ROLE_SALES, is_active=True)
    result = CommissionQueryService(base, settings=Settings()).query(
        prance, ["2026-08", "2026-09", "2026-10"]
    )
    assert [result.total_payable(p) for p in result.periods] == [
        Decimal("400.00"),
        Decimal("220.00"),
        Decimal("400.00"),
    ]


def test_渠道详情卡不算升级之前的交易(base):
    months = ReferralHistoryService(base, settings=Settings()).recent(
        base.r095, today=date(2026, 10, 20)
    )
    by_period = {m.period: m for m in months}
    assert sorted(c.name for c in by_period["2026-09"].clients) == ["九月十号升级", "老客户"]
    assert by_period["2026-09"].trade == Decimal("220.00")
    assert by_period["2026-10"].trade == Decimal("400.00")
