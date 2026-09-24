"""渠道详情卡上的「近 3 个月」：每个客户每个月的交易佣金和 ECAS 返佣。

2026-09-24 第二轮反馈之后改成**现算**（第一版读汇总表，只有每月一个总数）。盯的事：

1. **和佣金查询同一套数**：同一个渠道同一个月，两张卡上的数必须一样。
2. **只读这条渠道的客户**：别的渠道的客户、别的渠道的 ECAS 申请不能混进来。
3. **不扫整张看板**：按客户UID 在服务端筛选；筛选失败时退回全表扫描，数照样对。
4. **同一个客户两套账排一行**：先按 UID 对，ECAS 那边没 UID 就按规整后的名字对。
5. **两套账各算各的**：ECAS 用那一行自己的比例，不用渠道表的比例。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.domain import ecas, schema
from crm_basebot.domain.commission_query import CommissionQueryService
from crm_basebot.domain.dates import DEFAULT_BUSINESS_TIMEZONE, date_to_ms
from crm_basebot.domain.referral_history import ReferralHistoryService

from .conftest import TBL_BOARD, TBL_CLIENT, TBL_ECAS, TBL_REFERRAL

ALICE = "ou_alice000000000000000000000000"
TODAY = date(2026, 9, 24)

UID_A1 = "577809207768677761"
UID_A2 = "577809207768677762"
UID_B1 = "2141293991366272768"


class Settings:
    table_referral = TBL_REFERRAL
    table_client = TBL_CLIENT
    table_daily_board = TBL_BOARD
    table_ecas = TBL_ECAS
    business_timezone = "Asia/Singapore"


@pytest.fixture
def base(fake_bitable):
    """R001（20%）挂 A1、A2 两个客户；R002（10%）挂 B1。"""
    referrals = fake_bitable.tables[TBL_REFERRAL]
    r001 = referrals.add_existing(
        {
            schema.REFERRAL_NO: "R001",
            schema.REFERRAL_NAME: "北极星",
            schema.REFERRAL_RATE: 20,
            schema.REFERRAL_OWNER_OPEN_ID: ALICE,
        }
    )
    r002 = referrals.add_existing(
        {
            schema.REFERRAL_NO: "R002",
            schema.REFERRAL_NAME: "鲸落",
            schema.REFERRAL_RATE: 10,
            schema.REFERRAL_OWNER_OPEN_ID: "ou_bob",
        }
    )
    clients = fake_bitable.tables[TBL_CLIENT]
    clients.add_existing(
        {
            schema.CLIENT_UID: UID_A1,
            schema.CLIENT_NAME: "PLUTO STUDIO LIMITED",
            schema.CLIENT_REFERRAL_LINK: [r001],
        }
    )
    clients.add_existing(
        {schema.CLIENT_UID: UID_A2, schema.CLIENT_NAME: "青柠", schema.CLIENT_REFERRAL_LINK: [r001]}
    )
    clients.add_existing(
        {schema.CLIENT_UID: UID_B1, schema.CLIENT_NAME: "潮汐", schema.CLIENT_REFERRAL_LINK: [r002]}
    )
    fake_bitable.r001 = r001
    fake_bitable.r002 = r002
    return fake_bitable


def trade(bitable, uid: str, revenue: float, day: str = "2026/09/02") -> None:
    bitable.tables[TBL_BOARD].add_existing(
        {
            schema.BOARD_ORDER_DATE: day,
            schema.BOARD_CLIENT_UID: uid,
            schema.BOARD_TOTAL_REVENUE: revenue,
        }
    )


def application(
    bitable,
    referral_id: str,
    name: str,
    amount: float,
    rate: float,
    *,
    day=date(2026, 9, 10),
    uid="",
) -> None:
    bitable.tables[TBL_ECAS].add_existing(
        {
            ecas.ECAS_CLIENT_NAME: name,
            ecas.ECAS_AMOUNT: amount,
            ecas.ECAS_RATE: rate,
            ecas.ECAS_APPLIED_AT: date_to_ms(day, tz=DEFAULT_BUSINESS_TIMEZONE),
            ecas.ECAS_REFERRAL_LINK: [referral_id],
            ecas.ECAS_CLIENT_UID: uid,
        }
    )


def recent(bitable, referral_id=None, settings=None):
    service = ReferralHistoryService(bitable, settings=settings or Settings())
    return service.recent(referral_id or bitable.r001, today=TODAY)


def month(months, period):
    (found,) = [m for m in months if m.period == period]
    return found


# ---------- 哪三个月 ----------


def test_含本月在内往回三个月_从早到晚(base):
    months = recent(base)
    assert [m.period for m in months] == ["2026-07", "2026-08", "2026-09"]
    assert [m.current for m in months] == [False, False, True]


def test_跨年往回数(base):
    service = ReferralHistoryService(base, settings=Settings())
    months = service.recent(base.r001, today=date(2026, 1, 5))
    assert [m.period for m in months] == ["2025-11", "2025-12", "2026-01"]


def test_没有记录的月份是空的而不是零(base):
    """「这个月没交易」和「这个月交易了、应付 0」不能长成一样。"""
    months = recent(base)
    july = month(months, "2026-07")
    assert july.is_empty
    assert july.trade is None and july.ecas is None
    assert july.clients == ()


# ---------- 每个客户的交易佣金 ----------


def test_每个客户分到的佣金加起来等于渠道应付(base):
    trade(base, UID_A1, 700.00)
    trade(base, UID_A2, 300.00)

    september = month(recent(base), "2026-09")
    assert september.trade == Decimal("200.00")  # 1000 × 20%
    shares = {c.name: c.trade for c in september.clients}
    assert shares == {"PLUTO STUDIO LIMITED": Decimal("140.00"), "青柠": Decimal("60.00")}
    assert sum(shares.values()) == september.trade


def test_客户按贡献从大到小(base):
    trade(base, UID_A1, 100.00)
    trade(base, UID_A2, 900.00)

    september = month(recent(base), "2026-09")
    assert [c.name for c in september.clients] == ["青柠", "PLUTO STUDIO LIMITED"]


def test_别的渠道的客户不会混进来(base):
    trade(base, UID_A1, 1000.00)
    trade(base, UID_B1, 5000.00)

    september = month(recent(base), "2026-09")
    assert [c.name for c in september.clients] == ["PLUTO STUDIO LIMITED"]
    assert september.trade == Decimal("200.00")


def test_月份按业务时区归(base):
    """8 月 31 日 16:30 UTC 是新加坡 9 月 1 日 00:30，要算进 9 月。"""
    base.tables[TBL_BOARD].add_existing(
        {
            schema.BOARD_ORDER_DATE: 1788193800000,  # 2026-08-31T16:30:00Z
            schema.BOARD_CLIENT_UID: UID_A1,
            schema.BOARD_TOTAL_REVENUE: 1000.00,
        }
    )
    months = recent(base)
    assert month(months, "2026-09").trade == Decimal("200.00")
    assert month(months, "2026-08").is_empty


def test_整月合计为负时应付记零并标出来(base):
    trade(base, UID_A1, -800.00)
    trade(base, UID_A2, 200.00)

    september = month(recent(base), "2026-09")
    assert september.trade == Decimal("0")
    assert september.trade_loss
    assert all(c.trade == Decimal("0") for c in september.clients)


def test_和佣金查询是同一套数(base):
    """同一个渠道同一个月，详情卡和佣金查询上的数出自同一段代码，必须一样。"""
    trade(base, UID_A1, 1234.56)
    trade(base, UID_A2, -34.56)
    trade(base, UID_A1, 99.99, "2026/08/15")

    months = recent(base)
    alice = Sales(open_id=ALICE, name="Alice", role=schema.ROLE_SALES, is_active=True)
    query = CommissionQueryService(base, settings=Settings()).query(
        alice, ["2026-07", "2026-08", "2026-09"]
    )
    (channel,) = query.channels()
    for period in ("2026-08", "2026-09"):
        assert month(months, period).trade == channel.payable[period]
        detail_shares = {c.name: c.trade for c in month(months, period).clients}
        query_shares = {c.name: c.shares[period] for c in channel.clients if period in c.shares}
        assert detail_shares == query_shares


# ---------- 读表的方式 ----------


def test_看板按客户UID筛选而不是全表扫描(base):
    """看板上万行。一条渠道十来个客户，按 UID 筛一两页就回来了。"""
    trade(base, UID_A1, 1000.00)
    recent(base)

    assert base.tables[TBL_BOARD].scan_count == 0
    assert (TBL_BOARD, schema.BOARD_CLIENT_UID, (UID_A1, UID_A2)) in base.filtered_reads


def test_筛选接口出错时退回全表扫描_数照样对(base, monkeypatch):
    trade(base, UID_A1, 700.00)
    trade(base, UID_A2, 300.00)
    trade(base, UID_B1, 5000.00)

    def broken(*args, **kwargs):
        raise RuntimeError("filter 不认")
        yield  # pragma: no cover - 让它是个生成器，和真的一样

    monkeypatch.setattr(base, "iter_records_where_in", broken)
    september = month(recent(base), "2026-09")
    assert base.tables[TBL_BOARD].scan_count == 1
    assert september.trade == Decimal("200.00")
    assert {c.name for c in september.clients} == {"PLUTO STUDIO LIMITED", "青柠"}


def test_名下没有客户时不去读看板(base):
    service = ReferralHistoryService(base, settings=Settings())
    service.recent(base.r002, today=TODAY)  # R002 有客户，先确认会读
    base.filtered_reads.clear()

    empty = base.tables[TBL_REFERRAL].add_existing(
        {schema.REFERRAL_NO: "R003", schema.REFERRAL_RATE: 20}
    )
    months = service.recent(empty, today=TODAY)
    assert all(m.is_empty for m in months)
    assert base.filtered_reads == []


# ---------- ECAS ----------


def test_ECAS用那一行自己的比例(base):
    """渠道表上 R001 是 20%，ECAS 那一行写的是 50% —— 返佣按 50% 算。"""
    application(base, base.r001, "青柠", 10000, 50)

    september = month(recent(base), "2026-09")
    assert september.ecas == Decimal("5000.00")
    (client,) = september.clients
    assert client.ecas == Decimal("5000.00")
    assert client.trade is None


def test_同一个客户两套账排在同一行_按名字对(base):
    """ECAS 表里 UID 大多是空的，名字大小写、全角半角也可能不一样。"""
    trade(base, UID_A1, 1000.00)
    application(base, base.r001, "pluto studio limited", 10000, 50)

    september = month(recent(base), "2026-09")
    (client,) = september.clients
    assert client.name == "PLUTO STUDIO LIMITED"  # 用客户表里登记的写法
    assert client.trade == Decimal("200.00")
    assert client.ecas == Decimal("5000.00")


def test_同一个客户两套账排在同一行_按UID对(base):
    trade(base, UID_A2, 1000.00)
    application(base, base.r001, "Qing Ning Ltd", 10000, 20, uid=UID_A2)

    september = month(recent(base), "2026-09")
    (client,) = september.clients
    assert client.name == "青柠"
    assert client.ecas == Decimal("2000.00")


def test_客户表里没有的ECAS客户单独一行(base):
    trade(base, UID_A1, 1000.00)
    application(base, base.r001, "新来的公司", 10000, 50)

    september = month(recent(base), "2026-09")
    assert {c.name for c in september.clients} == {"PLUTO STUDIO LIMITED", "新来的公司"}


def test_别的渠道的ECAS申请不会混进来(base):
    application(base, base.r002, "潮汐", 10000, 50)

    assert month(recent(base), "2026-09").is_empty


def test_ECAS按申请时间归月(base):
    application(base, base.r001, "青柠", 10000, 50, day=date(2026, 8, 31))

    months = recent(base)
    assert month(months, "2026-08").ecas == Decimal("5000.00")
    assert month(months, "2026-09").is_empty


def test_没配看板也照样算ECAS(base):
    class NoBoard(Settings):
        table_daily_board = ""

    trade(base, UID_A1, 1000.00)
    application(base, base.r001, "青柠", 10000, 50)

    september = month(recent(base, settings=NoBoard()), "2026-09")
    assert september.trade is None
    assert september.ecas == Decimal("5000.00")
