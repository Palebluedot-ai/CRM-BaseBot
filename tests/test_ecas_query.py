"""销售自查 ECAS 返佣：权限口径和结果版式。

权限那几条是这里最要紧的。ECAS 表里是整个台子的开户申请，一名销售在机器人里
按一下按钮就能读到它 —— 过滤错了，别人的客户名和金额就直接进了他的会话。

口径和交易佣金完全一致：只看**归属自己的渠道**，判据是渠道表的「登记人OpenID」，
不是 ECAS 表那一列「负责销售」。返佣是付给渠道的，谁经手那笔申请不决定谁该看到这笔钱。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from crm_basebot.bot.auth import Sales
from crm_basebot.domain import ecas, schema
from crm_basebot.domain.ecas_query import (
    EcasQueryService,
    latest_applied_date,
    load_payees,
    owned_referral_ids,
    summarize,
)

from .conftest import TBL_ECAS, TBL_REFERRAL

SG = ZoneInfo("Asia/Singapore")
ALICE = "ou_alice000000000000000000000000"
BOB = "ou_bob0000000000000000000000000000"


class Settings:
    table_referral = TBL_REFERRAL
    table_ecas = TBL_ECAS
    business_timezone = "Asia/Singapore"


def sales(open_id: str, *, admin: bool = False) -> Sales:
    return Sales(
        open_id=open_id,
        name="Alice" if open_id == ALICE else "Bob",
        role=schema.ROLE_ADMIN if admin else schema.ROLE_SALES,
        is_active=True,
    )


def referral(fake_bitable, code: str, name: str, owner: str) -> str:
    return fake_bitable.tables[TBL_REFERRAL].add_existing(
        {
            schema.REFERRAL_NO: code,
            schema.REFERRAL_NAME: name,
            schema.REFERRAL_OWNER_OPEN_ID: owner,
            # 交易那边的比例故意给一个离谱的数：ECAS 一眼都不该看它
            schema.REFERRAL_RATE: 999,
        }
    )


def application(
    fake_bitable,
    client: str,
    amount: float,
    when: datetime,
    *,
    link: str | None = None,
    rate: float | None = 50,
    referrer_name: str = "",
) -> None:
    fields = {
        ecas.ECAS_CLIENT_NAME: client,
        ecas.ECAS_AMOUNT: amount,
        ecas.ECAS_APPLIED_AT: int(when.replace(tzinfo=SG).timestamp() * 1000),
    }
    if link:
        fields[ecas.ECAS_REFERRAL_LINK] = [link]
    if rate is not None:
        fields[ecas.ECAS_RATE] = rate
    if referrer_name:
        fields[ecas.ECAS_REFERRER_NAME] = referrer_name
    fake_bitable.tables[TBL_ECAS].add_existing(fields)


@pytest.fixture
def base(fake_bitable):
    alice_ref = referral(fake_bitable, "R095", "JIANG JUN", ALICE)
    bob_ref = referral(fake_bitable, "R099", "Mo Xuelei", BOB)
    application(fake_bitable, "Alice 的客户", 5000, datetime(2026, 8, 10), link=alice_ref)
    application(fake_bitable, "Alice 的客户二", 5000, datetime(2026, 8, 12), link=alice_ref)
    application(fake_bitable, "Bob 的客户", 33000, datetime(2026, 8, 20), link=bob_ref)
    # 没挂渠道、只写了名字的孤儿行
    application(fake_bitable, "孤儿", 5000, datetime(2026, 8, 25), referrer_name="查无此人")
    return fake_bitable


# ---------- 权限 ----------


def test_销售只看得到自己名下渠道的申请(base):
    rows = EcasQueryService(base, settings=Settings()).query(sales(ALICE), "2026-08")
    (row,) = rows
    assert row.payee.code == "R095"
    assert row.client_names == {"Alice 的客户", "Alice 的客户二"}


def test_别人的客户名和金额都不出现(base):
    rows = EcasQueryService(base, settings=Settings()).query(sales(ALICE), "2026-08")
    text = summarize(rows, period="2026-08", viewer_name="Alice")
    assert "Bob 的客户" not in text
    assert "Mo Xuelei" not in text
    assert "33,000" not in text
    assert "16,500" not in text


def test_管理员看全部(base):
    rows = EcasQueryService(base, settings=Settings()).query(sales(ALICE, admin=True), "2026-08")
    assert {r.payee.code for r in rows} == {"R095", "R099"}


def test_没挂渠道的孤儿行对谁都不显示(base):
    """没有渠道就判不出归属，给谁看都是错的。

    注意结算那条路**会**把它算进去（钱是欠着的），两条路在这里刻意不一样。
    """
    for viewer in (sales(ALICE), sales(ALICE, admin=True)):
        rows = EcasQueryService(base, settings=Settings()).query(viewer, "2026-08")
        assert all(row.payee.name != "查无此人" for row in rows)


def test_归属为空的渠道普通销售看不到(fake_bitable):
    """历史数据补录归属之前只有管理员能碰。不能因为「无主」就人人可见。"""
    orphan = referral(fake_bitable, "R001", "无主渠道", "")
    application(fake_bitable, "某客户", 5000, datetime(2026, 8, 10), link=orphan)

    assert EcasQueryService(fake_bitable, settings=Settings()).query(sales(ALICE), "2026-08") == []
    assert owned_referral_ids(fake_bitable, TBL_REFERRAL, sales(ALICE, admin=True)) == {orphan}


def test_月份列表只列本人有数据的月份(base):
    """下拉里列一个他点进去必然是空的月份，只会让人以为系统坏了。"""
    service = EcasQueryService(base, settings=Settings())
    assert service.periods_for(sales(ALICE)) == ["2026-08"]
    assert service.periods_for(sales(BOB)) == ["2026-08"]


# ---------- 比例只从行来 ----------


def test_渠道表的分佣比例再离谱也不影响ECAS(base):
    """渠道表那几条的比例是 999。ECAS 只认申请行自己的 50%。"""
    rows = EcasQueryService(base, settings=Settings()).query(sales(ALICE), "2026-08")
    (row,) = rows
    assert row.payable == Decimal("5000.00")  # 10,000 × 50%
    assert row.rate_note == "50%"


def test_只取渠道的编号和名字(fake_bitable):
    payees = load_payees(fake_bitable, TBL_REFERRAL)
    referral(fake_bitable, "R095", "JIANG JUN", ALICE)
    payees = load_payees(fake_bitable, TBL_REFERRAL)
    assert list(payees.values()) == [ecas.Payee(code="R095", name="JIANG JUN")]


# ---------- 结果版式 ----------


def test_结果顶部给三个总数(base):
    rows = EcasQueryService(base, settings=Settings()).query(sales(ALICE, admin=True), "2026-08")
    text = summarize(rows, period="2026-08", viewer_name="Admin")
    assert "ECAS 返佣合计  **21,500.00** USD" in text
    assert "2 个渠道 · 3 个客户 · 开户金额合计 43,000.00" in text


def test_每个渠道给小计和客户名(base):
    rows = EcasQueryService(base, settings=Settings()).query(sales(ALICE), "2026-08")
    text = summarize(rows, period="2026-08", viewer_name="Alice")
    assert "小计：开户 10,000.00 · 2 个客户 · 2 笔 · 比例 50%" in text
    assert "· Alice 的客户二" in text


def test_没数据时说清楚可能的原因(base):
    text = summarize([], period="2026-01", viewer_name="Alice")
    assert "没有 ECAS 返佣" in text
    assert "还没导进" in text  # 「没有」和「还没导」是两回事，别让人误会


def test_结果末尾提醒这不是交易佣金(base):
    rows = EcasQueryService(base, settings=Settings()).query(sales(ALICE), "2026-08")
    assert "两笔钱" in summarize(rows, period="2026-08", viewer_name="Alice")


# ---------- 数据新鲜度 ----------


def test_最新申请日期按业务时区取(fake_bitable):
    # 新加坡 9 月 1 日 00:30，UTC 还是 8 月 31 日
    application(fake_bitable, "某客户", 5000, datetime(2026, 9, 1, 0, 30))
    assert latest_applied_date(fake_bitable, TBL_ECAS, tz=SG) == "2026-09-01"


def test_空表返回空串(fake_bitable):
    assert latest_applied_date(fake_bitable, TBL_ECAS, tz=SG) == ""
