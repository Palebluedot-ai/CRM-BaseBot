"""隔离规则的测试。

这些用例就是「销售看不到别人数据」这个承诺的可执行版本。
"""

import pytest

from crm_basebot.bot.auth import (
    AuthError,
    Sales,
    SalesDirectory,
    owned_records,
    require_owner,
)
from crm_basebot.domain import schema
from crm_basebot.lark.bitable import Record

ALICE = "ou_alice000000000000000000000000"
BOB = "ou_bob00000000000000000000000000"
ADMIN = "ou_admin00000000000000000000000"


class FakeBitable:
    def __init__(self, records):
        self._records = records

    def iter_records(self, table_id, **kwargs):
        yield from self._records


def _sales_record(open_id, name, role=schema.ROLE_SALES, status=schema.SALES_STATUS_ACTIVE):
    return Record(
        record_id=f"rec_{name}",
        fields={
            schema.SALES_OPEN_ID: open_id,
            schema.SALES_NAME: name,
            schema.SALES_ROLE: role,
            schema.SALES_STATUS: status,
        },
    )


@pytest.fixture
def directory():
    return SalesDirectory(
        FakeBitable(
            [
                _sales_record(ALICE, "Alice"),
                _sales_record(BOB, "Bob"),
                _sales_record(ADMIN, "Admin", role=schema.ROLE_ADMIN),
                _sales_record(
                    "ou_gone00000000000000000000000",
                    "Gone",
                    status=schema.SALES_STATUS_DISABLED,
                ),
            ]
        ),
        "tbl_sales",
    )


def test_在职销售可以通过(directory):
    sales = directory.require(ALICE)
    assert sales.name == "Alice"
    assert sales.is_admin is False


def test_管理员被识别(directory):
    assert directory.require(ADMIN).is_admin is True


def test_名册里没有的人被拒绝(directory):
    with pytest.raises(AuthError, match="还没有被登记"):
        directory.require("ou_stranger0000000000000000000")


def test_停用的账号被拒绝(directory):
    with pytest.raises(AuthError, match="已停用"):
        directory.require("ou_gone00000000000000000000000")


def test_空openid被拒绝(directory):
    with pytest.raises(AuthError, match="没有 open_id"):
        directory.require("")


# ---------- 归属校验 ----------

alice = Sales(open_id=ALICE, name="Alice", role=schema.ROLE_SALES, is_active=True)
admin = Sales(open_id=ADMIN, name="Admin", role=schema.ROLE_ADMIN, is_active=True)


def test_可以访问自己名下的记录():
    require_owner(alice, ALICE, what="渠道 R007")


def test_不能访问别人名下的记录():
    with pytest.raises(AuthError, match="不在你名下"):
        require_owner(alice, BOB, what="渠道 R007")


def test_没有归属人的记录普通销售也不能碰():
    """历史数据补录归属之前，不能因为「无主」就人人可见。"""
    with pytest.raises(AuthError, match="没有登记归属人"):
        require_owner(alice, "", what="渠道 R001")


def test_管理员不受归属限制():
    require_owner(admin, BOB, what="渠道 R007")
    require_owner(admin, "", what="渠道 R001")


# ---------- 列表过滤 ----------


def _referral(record_id, owner):
    return Record(
        record_id=record_id,
        fields={schema.REFERRAL_OWNER_OPEN_ID: owner, schema.REFERRAL_NO: record_id},
    )


ALL_REFERRALS = [
    _referral("R001", ALICE),
    _referral("R002", BOB),
    _referral("R003", ALICE),
    _referral("R004", ""),
]


def test_销售只看到自己的记录():
    got = list(owned_records(alice, ALL_REFERRALS, schema.REFERRAL_OWNER_OPEN_ID))
    assert [r.record_id for r in got] == ["R001", "R003"]


def test_无主记录不会漏给普通销售():
    got = list(owned_records(alice, ALL_REFERRALS, schema.REFERRAL_OWNER_OPEN_ID))
    assert "R004" not in [r.record_id for r in got]


def test_管理员看到全部():
    got = list(owned_records(admin, ALL_REFERRALS, schema.REFERRAL_OWNER_OPEN_ID))
    assert len(got) == 4
