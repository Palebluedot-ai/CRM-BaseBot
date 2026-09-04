"""隔离规则的测试。

这些用例就是「销售看不到别人数据」这个承诺的可执行版本。
"""

import pytest

from crm_basebot.bot.auth import (
    AuthError,
    Sales,
    SalesDirectory,
    owned_records,
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


# ---------- 缓存有效期 ----------


class MutableBitable(FakeBitable):
    """记录可以改、遍历次数可以数的假件，用来看缓存什么时候真的重新读表。"""

    def __init__(self, records):
        super().__init__(records)
        self.scan_count = 0

    def iter_records(self, table_id, **kwargs):
        self.scan_count += 1
        yield from list(self._records)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def ticking():
    """(名册, 假件, 时钟)。时钟手动拨，测试不用真睡。"""
    bitable = MutableBitable([_sales_record(ALICE, "Alice")])
    clock = Clock()
    return SalesDirectory(bitable, "tbl_sales", ttl_seconds=60, clock=clock), bitable, clock


def test_有效期内不重复读表(ticking):
    directory, bitable, _ = ticking
    directory.require(ALICE)
    directory.require(ALICE)
    assert bitable.scan_count == 1


def test_过了有效期重新读表(ticking):
    directory, bitable, clock = ticking
    directory.require(ALICE)
    clock.now += 61
    directory.require(ALICE)
    assert bitable.scan_count == 2


def test_停用的人最多一个有效期后被拒(ticking):
    """人员变动不用重启机器人：名册改了，一分钟内生效。"""
    directory, bitable, clock = ticking
    directory.require(ALICE)

    bitable._records[:] = [_sales_record(ALICE, "Alice", status=schema.SALES_STATUS_DISABLED)]
    # 还在有效期内，旧缓存放行。这是有意的：用最多一分钟的延迟换掉每次回调多读一遍表。
    directory.require(ALICE)

    clock.now += 61
    with pytest.raises(AuthError, match="已停用"):
        directory.require(ALICE)


def test_新人最多一个有效期后能用(ticking):
    directory, bitable, clock = ticking
    with pytest.raises(AuthError, match="还没有被登记"):
        directory.require(BOB)

    bitable._records.append(_sales_record(BOB, "Bob"))
    clock.now += 61
    assert directory.require(BOB).name == "Bob"


def test_refresh立刻生效(ticking):
    directory, bitable, _ = ticking
    directory.require(ALICE)
    bitable._records.append(_sales_record(BOB, "Bob"))
    directory.refresh()
    assert directory.require(BOB).name == "Bob"


def test_默认有效期是一分钟():
    """「人员变动一分钟内生效」是对使用方的承诺，改这个数要同时改文档。"""
    bitable = MutableBitable([_sales_record(ALICE, "Alice")])
    clock = Clock()
    directory = SalesDirectory(bitable, "tbl_sales", clock=clock)

    directory.require(ALICE)
    clock.now += 59
    directory.require(ALICE)
    assert bitable.scan_count == 1

    clock.now += 2
    directory.require(ALICE)
    assert bitable.scan_count == 2


# ---------- 列表过滤 ----------

alice = Sales(open_id=ALICE, name="Alice", role=schema.ROLE_SALES, is_active=True)
admin = Sales(open_id=ADMIN, name="Admin", role=schema.ROLE_ADMIN, is_active=True)


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
