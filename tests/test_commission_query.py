"""销售自查用的佣金明细：CommissionQueryService。

对账写的是「按月 × 渠道」的粗粒度汇总，这个 service 是「按月 × 渠道 × 客户」的
读取路径。要点：

1. **权限**沿用现有 owned_records 的口径 —— 销售只看自己名下渠道，管理员看全部。
2. **孤儿 UID**（在看板里但没登记归属的客户）只对管理员可见 —— 销售看到别人的孤儿
   没有意义，也不该让他知道存在过。
3. **客户份额**用「按客户收入占比切分渠道应付」的算法，保证 sum(客户份额) == 渠道
   应付；直接按客户 × 比例算再累加会在渠道被 max(0, ...) 兜底时对不上。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.domain import schema
from crm_basebot.domain.commission_query import (
    ClientBreakdown,
    CommissionQueryService,
    QueryResult,
    ReferralBreakdown,
    summarize,
)

from .conftest import TBL_BOARD, TBL_CLIENT, TBL_REFERRAL

ALICE = "ou_alice000000000000000000000000"
BOB = "ou_bob0000000000000000000000000000"

UID_A1 = "577809207768677761"  # Alice 名下渠道的客户
UID_A2 = "577809207768677762"  # 同上，另一个客户
UID_B1 = "2141293991366272768"  # Bob 名下渠道的客户
UID_ORPHAN = "999999999999999999"  # 没登记归属的孤儿

alice = Sales(open_id=ALICE, name="Alice", role=schema.ROLE_SALES, is_active=True)
bob = Sales(open_id=BOB, name="Bob", role=schema.ROLE_SALES, is_active=True)
admin = Sales(open_id="ou_admin0", name="Admin", role=schema.ROLE_ADMIN, is_active=True)


class Settings:
    table_referral = TBL_REFERRAL
    table_client = TBL_CLIENT
    table_daily_board = TBL_BOARD
    business_timezone = "Asia/Singapore"


@pytest.fixture
def base(fake_bitable):
    """两个渠道：R001 归 Alice、R002 归 Bob。各挂两个客户。"""
    r_alice = fake_bitable.tables[TBL_REFERRAL].add_existing(
        {
            schema.REFERRAL_NO: "R001",
            schema.REFERRAL_NAME: "北极星",
            schema.REFERRAL_RATE: 20,
            schema.REFERRAL_STATUS: schema.STATUS_ACTIVE,
            schema.REFERRAL_OWNER_OPEN_ID: ALICE,
        }
    )
    r_bob = fake_bitable.tables[TBL_REFERRAL].add_existing(
        {
            schema.REFERRAL_NO: "R002",
            schema.REFERRAL_NAME: "鲸落",
            schema.REFERRAL_RATE: 10,
            schema.REFERRAL_STATUS: schema.STATUS_ACTIVE,
            schema.REFERRAL_OWNER_OPEN_ID: BOB,
        }
    )
    fake_bitable.tables[TBL_CLIENT].add_existing(
        {
            schema.CLIENT_UID: UID_A1,
            schema.CLIENT_NAME: "普罗米修斯",
            schema.CLIENT_REFERRAL_LINK: [r_alice],
        }
    )
    fake_bitable.tables[TBL_CLIENT].add_existing(
        {
            schema.CLIENT_UID: UID_A2,
            schema.CLIENT_NAME: "青柠",
            schema.CLIENT_REFERRAL_LINK: [r_alice],
        }
    )
    fake_bitable.tables[TBL_CLIENT].add_existing(
        {
            schema.CLIENT_UID: UID_B1,
            schema.CLIENT_NAME: "潮汐",
            schema.CLIENT_REFERRAL_LINK: [r_bob],
        }
    )
    return fake_bitable


def _row(bitable, uid, revenue, order="2026/03/02"):
    bitable.tables[TBL_BOARD].add_existing(
        {
            schema.BOARD_ORDER_DATE: order,
            schema.BOARD_CLIENT_UID: uid,
            schema.BOARD_TOTAL_REVENUE: revenue,
        }
    )


# ---------- 权限 ----------


def test_销售只看到自己名下的渠道(base):
    _row(base, UID_A1, 1000.00)  # Alice
    _row(base, UID_B1, 5000.00)  # Bob

    result = CommissionQueryService(base, settings=Settings()).query(alice, "2026-03")

    assert [r.referral_no for r in result.referrals] == ["R001"]
    assert result.referrals[0].revenue_total == Decimal("1000.00")


def test_管理员看全部渠道(base):
    _row(base, UID_A1, 1000.00)
    _row(base, UID_B1, 5000.00)

    result = CommissionQueryService(base, settings=Settings()).query(admin, "2026-03")

    assert {r.referral_no for r in result.referrals} == {"R001", "R002"}


def test_销售看不到孤儿uid(base):
    _row(base, UID_A1, 1000.00)
    _row(base, UID_ORPHAN, 500.00)

    result = CommissionQueryService(base, settings=Settings()).query(alice, "2026-03")

    assert result.unmapped_uids == [], "销售看到别的销售的孤儿没有意义，也不该知道存在过"


def test_管理员看得到孤儿uid(base):
    _row(base, UID_A1, 1000.00)
    _row(base, UID_ORPHAN, 500.00)

    result = CommissionQueryService(base, settings=Settings()).query(admin, "2026-03")

    assert result.unmapped_uids == [UID_ORPHAN]


# ---------- 客户份额 ----------


def test_客户份额加起来等于渠道应付(base):
    """按占比切分是关键 —— 保证 sum(客户份额) == 渠道应付。"""
    _row(base, UID_A1, 700.00)
    _row(base, UID_A2, 300.00)

    result = CommissionQueryService(base, settings=Settings()).query(alice, "2026-03")

    referral = result.referrals[0]
    total_share = sum((referral.client_share(c) for c in referral.clients.values()), Decimal("0"))
    assert referral.payable == Decimal("200.00")  # 1000 × 20%
    assert total_share == referral.payable


def test_负值客户不会让份额被单独归零(base):
    """一个客户负、一个客户正，如果按各自计算 × 比例后相加，负客户会被 max(0, ...)
    归零，正客户不受影响，加起来就大于渠道应付。占比法避免这个 bug。
    """
    _row(base, UID_A1, 800.00)
    _row(base, UID_A2, -300.00)

    result = CommissionQueryService(base, settings=Settings()).query(alice, "2026-03")

    referral = result.referrals[0]
    assert referral.revenue_total == Decimal("500.00")
    assert referral.payable == Decimal("100.00")  # 500 × 20%

    a1_share = referral.client_share(referral.clients[UID_A1])
    a2_share = referral.client_share(referral.clients[UID_A2])
    # 负客户拿到的份额也是负的（按占比 -300/500）—— 这样加起来等于渠道应付
    assert a2_share < 0
    assert a1_share + a2_share == referral.payable


def test_整月负值时所有份额归零(base):
    """渠道级 payable 已经保底成 0；客户份额没有正数可分，全归 0，避免看板上
    出现「客户 A 拿正、客户 B 拿负、总和为 0」这种令人困惑的画面。"""
    _row(base, UID_A1, -800.00)
    _row(base, UID_A2, -200.00)

    result = CommissionQueryService(base, settings=Settings()).query(alice, "2026-03")

    referral = result.referrals[0]
    assert referral.is_loss_month
    assert referral.payable == Decimal("0")
    for client in referral.clients.values():
        assert referral.client_share(client) == Decimal("0")


# ---------- 月份过滤 ----------


def test_只算指定月份(base):
    _row(base, UID_A1, 1000.00, "2026/02/15")
    _row(base, UID_A1, 3000.00, "2026/03/02")

    result = CommissionQueryService(base, settings=Settings()).query(alice, "2026-03")

    assert result.referrals[0].revenue_total == Decimal("3000.00")


def test_latest_period_返回看板最大月份(base):
    _row(base, UID_A1, 1000.00, "2026/01/15")
    _row(base, UID_A1, 2000.00, "2026/03/02")
    _row(base, UID_A1, 1500.00, "2026/02/10")

    assert CommissionQueryService(base, settings=Settings()).latest_period() == "2026-03"


def test_latest_period_空表返回空串(base):
    assert CommissionQueryService(base, settings=Settings()).latest_period() == ""


# ---------- 展示 ----------


def test_summarize_包含渠道客户和佣金(base):
    _row(base, UID_A1, 1000.00)
    result = CommissionQueryService(base, settings=Settings()).query(alice, "2026-03")

    text = summarize(result, viewer_name="Alice")

    assert "R001" in text
    assert "普罗米修斯" in text
    assert UID_A1 in text
    assert "200.00" in text  # 应付
    assert "2026-03" in text


def test_summarize_没数据时给出友好提示(base):
    result = CommissionQueryService(base, settings=Settings()).query(alice, "2026-03")

    text = summarize(result, viewer_name="Alice")
    assert "没有可展示" in text


# ---------- 卡片上的总数 ----------
#
# 光有一个「合计应付」，看的人没法判断它合不合理。少了一个渠道、少了一个客户，
# 金额照样是一个像样的数字 —— 把渠道数、客户数、收入合计摆出来，缺了就立刻看得出。


def _breakdown(no: str, rate: str, clients: list[tuple[str, str, int]]) -> ReferralBreakdown:
    item = ReferralBreakdown(referral_no=no, referral_name=f"{no} 名称", rate_percent=Decimal(rate))
    for uid, revenue, rows in clients:
        entry = ClientBreakdown(uid=uid, name=f"客户{uid[-1]}")
        entry.revenue = Decimal(revenue)
        entry.row_count = rows
        item.clients[uid] = entry
    return item


def test_顶部三个总数都算对():
    result = QueryResult(
        period="2026-08",
        referrals=[
            _breakdown("R001", "20", [("uid1", "25600", 47), ("uid2", "12000", 9)]),
            _breakdown("R002", "15", [("uid3", "8000", 3)]),
        ],
        unmapped_uids=[],
    )
    assert result.referral_count == 2
    assert result.client_count == 3
    assert result.revenue_total == Decimal("45600")
    assert result.total_payable == Decimal("8720.00")

    text = summarize(result, viewer_name="Alice")
    assert "2 个渠道 · 3 个客户 · 收入合计 45,600.00" in text


def test_同一个客户挂在两个渠道下只数一次():
    """客户表被人手改过之后 UID 不保证只挂一个渠道。不去重的话
    「12 个客户」其实是同一个人数了两遍。"""
    result = QueryResult(
        period="2026-08",
        referrals=[
            _breakdown("R001", "20", [("uid1", "100", 1)]),
            _breakdown("R002", "20", [("uid1", "100", 1)]),
        ],
        unmapped_uids=[],
    )
    assert result.client_count == 1


def test_每个渠道下都有小计():
    result = QueryResult(
        period="2026-08",
        referrals=[_breakdown("R001", "20", [("uid1", "25600", 47), ("uid2", "12000", 9)])],
        unmapped_uids=[],
    )
    (ref,) = result.referrals
    assert ref.client_count == 2
    assert ref.row_count == 56  # 笔数和客户数是两回事：一个客户一个月能有几十笔
    assert "小计：收入 37,600.00 · 2 个客户 · 56 笔" in summarize(result, viewer_name="Alice")


def test_收入合计不做保底而应付做():
    """保底是应付金额的规则。收入也截成 0 的话，看报表的人看不出这个月是负的。"""
    result = QueryResult(
        period="2026-08",
        referrals=[_breakdown("R001", "20", [("uid1", "-5000", 2)])],
        unmapped_uids=[],
    )
    assert result.revenue_total == Decimal("-5000")
    assert result.total_payable == Decimal("0")
