"""佣金计算。

用的是截图里的真实数字：Pnl 729.99 / 710.89 / 6805.40 / 3668.00 / 4231.31，
客户UID 是 18-19 位。
"""

from decimal import Decimal

import pytest

from crm_basebot.domain import schema
from crm_basebot.domain.commission import (
    CommissionCalculator,
    UnmappedClientError,
    period_of,
    summarize,
)

from .conftest import TBL_CLIENT, TBL_REFERRAL, TBL_TXN

UID_A = "577809207768677761"
UID_B = "2141293991366272768"
UID_ORPHAN = "999999999999999999"


class Settings:
    table_referral = TBL_REFERRAL
    table_client = TBL_CLIENT
    table_transaction = TBL_TXN


@pytest.fixture
def base(fake_bitable):
    referral_a = fake_bitable.tables[TBL_REFERRAL].add_existing(
        {
            schema.REFERRAL_NO: "R001",
            schema.REFERRAL_NAME: "ABC Capital",
            schema.REFERRAL_RATE: 20,
            schema.REFERRAL_STATUS: schema.STATUS_ACTIVE,
        }
    )
    referral_b = fake_bitable.tables[TBL_REFERRAL].add_existing(
        {
            schema.REFERRAL_NO: "R002",
            schema.REFERRAL_NAME: "XYZ Partners",
            schema.REFERRAL_RATE: 12.5,
            schema.REFERRAL_STATUS: schema.STATUS_ACTIVE,
        }
    )

    fake_bitable.tables[TBL_CLIENT].add_existing(
        {
            schema.CLIENT_UID: UID_A,
            schema.CLIENT_NAME: "PLUTO STUDIO LIMITED",
            schema.CLIENT_REFERRAL_LINK: [referral_a],
        }
    )
    fake_bitable.tables[TBL_CLIENT].add_existing(
        {
            schema.CLIENT_UID: UID_B,
            schema.CLIENT_NAME: "HOMEX AND AI PTE. LTD.",
            schema.CLIENT_REFERRAL_LINK: [referral_b],
        }
    )
    return fake_bitable


def _txn(bitable, uid, pnl, order_time="2026/03/02"):
    bitable.tables[TBL_TXN].add_existing(
        {
            schema.TXN_ORDER_TIME: order_time,
            schema.TXN_CLIENT_UID: uid,
            schema.TXN_PNL: pnl,
        }
    )


# ---------- 期间解析 ----------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026/03/02", "2026-03"),
        ("2026-03-11", "2026-03"),
        (1772409600000, "2026-03"),
        ("", ""),
        (None, ""),
        ("garbage", ""),
    ],
)
def test_订单时间归月(value, expected):
    assert period_of(value) == expected


# ---------- 计算 ----------


def test_单渠道单月佣金(base):
    _txn(base, UID_A, 729.99)
    _txn(base, UID_A, 710.89)

    rows, unmapped = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    assert unmapped == []
    assert len(rows) == 1
    row = rows[0]
    assert row.referral_no == "R001"
    assert row.pnl_total == Decimal("1440.88")
    assert row.payable == Decimal("288.18")  # 1440.88 * 20%
    assert row.txn_count == 2
    assert row.client_count == 1


def test_不同渠道分别汇总(base):
    _txn(base, UID_A, 6805.40)
    _txn(base, UID_B, 3668.00)

    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    by_no = {r.referral_no: r for r in rows}
    assert by_no["R001"].payable == Decimal("1361.08")  # 6805.40 * 20%
    assert by_no["R002"].payable == Decimal("458.50")  # 3668.00 * 12.5%


def test_按月分开算(base):
    _txn(base, UID_A, 1000, "2026/03/02")
    _txn(base, UID_A, 2000, "2026/04/15")

    rows, _ = CommissionCalculator(base, settings=Settings()).compute()

    periods = {r.period: r.pnl_total for r in rows}
    assert periods == {"2026-03": Decimal("1000"), "2026-04": Decimal("2000")}


def test_指定月份只算那个月(base):
    _txn(base, UID_A, 1000, "2026/03/02")
    _txn(base, UID_A, 2000, "2026/04/15")

    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-04")

    assert len(rows) == 1
    assert rows[0].pnl_total == Decimal("2000")


def test_累加不引入浮点误差(base):
    """1000 笔 0.1 用 float 加会得到 99.99999999999859。"""
    for _ in range(1000):
        _txn(base, UID_A, 0.1)

    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    assert rows[0].pnl_total == Decimal("100.0")
    assert rows[0].payable == Decimal("20.00")


def test_同一渠道多个客户去重计数(base, fake_bitable):
    referral_a = next(iter(fake_bitable.tables[TBL_REFERRAL].records))
    second_uid = "577809207768677762"
    fake_bitable.tables[TBL_CLIENT].add_existing(
        {
            schema.CLIENT_UID: second_uid,
            schema.CLIENT_NAME: "另一个客户",
            schema.CLIENT_REFERRAL_LINK: [referral_a],
        }
    )
    _txn(base, UID_A, 100)
    _txn(base, UID_A, 100)
    _txn(base, second_uid, 100)

    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    assert rows[0].client_count == 2
    assert rows[0].txn_count == 3


# ---------- 未登记客户 ----------


def test_未登记归属的客户被单独列出而不是算进别人头上(base):
    _txn(base, UID_A, 1000)
    _txn(base, UID_ORPHAN, 5000)

    rows, unmapped = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    assert unmapped == [UID_ORPHAN]
    assert sum(r.pnl_total for r in rows) == Decimal("1000")


def test_strict模式下未登记客户直接报错(base):
    _txn(base, UID_ORPHAN, 5000)

    with pytest.raises(UnmappedClientError, match="没登记归属渠道"):
        CommissionCalculator(base, settings=Settings()).compute(period="2026-03", strict=True)


def test_相近uid不会串台(base, fake_bitable):
    """UID 只差最后一位，必须分别归属。float 化的话这两个会合并。"""
    referral_b = list(fake_bitable.tables[TBL_REFERRAL].records)[1]
    near = "577809207768677762"
    fake_bitable.tables[TBL_CLIENT].add_existing(
        {schema.CLIENT_UID: near, schema.CLIENT_REFERRAL_LINK: [referral_b]}
    )

    _txn(base, UID_A, 1000)
    _txn(base, near, 2000)

    rows, unmapped = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    assert unmapped == []
    by_no = {r.referral_no: r.pnl_total for r in rows}
    assert by_no == {"R001": Decimal("1000"), "R002": Decimal("2000")}


def test_uid以查找引用数组形式出现也能join(base):
    base.tables[TBL_TXN].add_existing(
        {
            schema.TXN_ORDER_TIME: "2026/03/02",
            schema.TXN_CLIENT_UID: [{"type": "text", "text": UID_A}],
            schema.TXN_PNL: 4231.31,
        }
    )

    rows, unmapped = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    assert unmapped == []
    assert rows[0].pnl_total == Decimal("4231.31")


def test_空pnl的行被跳过(base):
    _txn(base, UID_A, None)
    _txn(base, UID_A, 100)

    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    assert rows[0].txn_count == 1


# ---------- UID 体检 ----------


def test_干净数据的uid体检不告警(base):
    _txn(base, UID_A, 729.99)
    _txn(base, UID_B, 710.89)

    calculator = CommissionCalculator(base, settings=Settings())
    calculator.compute(period="2026-03")

    assert calculator.uid_health().verdict == "clean"


def test_交易明细里的excel截断uid被体检抓到(base):
    # 这批 UID 是同事导入的，尾部低位已经被 Excel 抹成 0。
    # 它们 join 不上客户表（进了 unmapped），但真正危险的是「刚好撞上别的客户」，
    # 所以要在算钱之前就把损伤本身报出来。
    for damaged in ("577809207768678000", "2141293991366270000", "577809207768600000"):
        _txn(base, damaged, 1000)

    calculator = CommissionCalculator(base, settings=Settings())
    calculator.compute(period="2026-03")

    report = calculator.uid_health()
    assert report.verdict == "likely_damaged"
    assert len(report.truncated) == 3


def test_体检不额外读表(base):
    # UID 是 compute() 途中顺手攒的。如果哪天有人改成再遍历一遍交易明细，
    # 这里会炸 —— 对账不该因为体检多花一倍的 API 调用。
    _txn(base, UID_A, 729.99)

    calculator = CommissionCalculator(base, settings=Settings())
    calculator.compute(period="2026-03")
    scans_after_compute = base.tables[TBL_TXN].scan_count

    calculator.uid_health()

    assert base.tables[TBL_TXN].scan_count == scans_after_compute


def test_没有数据时汇总文案友好():
    assert "没有可结算" in summarize([])


def test_汇总文案包含渠道和金额(base):
    _txn(base, UID_A, 6805.40)
    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")
    text = summarize(rows)
    assert "R001" in text
    assert "2026-03" in text
    assert "1,361.08" in text
