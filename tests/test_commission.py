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


# ---------- 业务规则：整月亏损佣金保底 0 ----------
#
# 规则由使用方在 2026-08-30 明确拍板：payable = max(0, pnl_total × rate)，
# 亏损月不倒扣、不结转。这一组测试是这条规则的锁 —— 谁改动保底逻辑，这里必须炸。


def test_整月亏损时佣金保底0但pnl如实保留负数(base):
    _txn(base, UID_A, -2680.30)
    _txn(base, UID_A, -1145.20)
    _txn(base, UID_A, 890.40)

    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    row = rows[0]
    assert row.pnl_total == Decimal("-2935.10"), "Pnl 必须如实记负数，报表上要看得见亏损"
    assert row.payable == Decimal("0"), "整月亏损，应付佣金保底 0"
    # -2935.10 × 20%
    assert row.gross_payable == Decimal("-587.02"), "原始值仍然可查，保底这一步要是可见的"
    assert row.is_loss_month is True


def test_正负混合但合计为正时照常计算(base):
    """单笔亏损被同月的盈利盖过去了就不算亏损月，保底不该介入。"""
    _txn(base, UID_A, -875.40)
    _txn(base, UID_A, 430.85)
    _txn(base, UID_A, 1290.60)

    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    row = rows[0]
    assert row.pnl_total == Decimal("846.05")
    assert row.payable == Decimal("169.21")  # 846.05 * 20%
    assert row.payable == row.gross_payable, "合计为正时保底不该改动任何数字"
    assert row.is_loss_month is False


def test_合计恰好为零时不算亏损月(base):
    """0 和负数在报表上不该长成一样：一个是没赚到，一个是亏了。"""
    _txn(base, UID_A, 1000.00)
    _txn(base, UID_A, -1000.00)

    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    row = rows[0]
    assert row.pnl_total == Decimal("0.00")
    assert row.payable == Decimal("0.00")
    assert row.is_loss_month is False, "合计为 0 不是亏损月，不该被标注成亏损"


def test_保底不影响同月其他渠道(base):
    _txn(base, UID_A, -5000.00)
    _txn(base, UID_B, 3668.00)

    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    by_no = {r.referral_no: r for r in rows}
    assert by_no["R001"].payable == Decimal("0")
    assert by_no["R002"].payable == Decimal("458.50")  # 3668.00 * 12.5%，不受隔壁影响
    assert by_no["R002"].is_loss_month is False


def test_亏损月不结转到下个月(base):
    """不结转是明确决定的：上个月亏多少都不冲抵这个月的佣金。"""
    _txn(base, UID_A, -5000.00, "2026/03/02")
    _txn(base, UID_A, 1000.00, "2026/04/10")

    rows, _ = CommissionCalculator(base, settings=Settings()).compute()

    by_period = {r.period: r for r in rows}
    assert by_period["2026-03"].payable == Decimal("0")
    assert by_period["2026-04"].payable == Decimal("200.00"), "4 月按 1000 全额算，不扣 3 月的亏损"


def test_极小额亏损也保底为正零(base):
    """-0.01 × 20% 量化后是 -0.00，不能让负零漏出去。"""
    _txn(base, UID_A, -0.01)

    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    row = rows[0]
    assert row.is_loss_month is True
    assert row.payable == Decimal("0")
    assert f"{row.payable:,.2f}" == "0.00", "不能打印成 -0.00"


# ---------- 默认月份：有数据的最新月份 ----------


def test_默认结算有数据的最新月份(base):
    _txn(base, UID_A, 1000.00, "2026/01/15")
    _txn(base, UID_A, 2000.00, "2026/03/02")
    _txn(base, UID_A, 3000.00, "2026/02/10")

    period, rows, _ = CommissionCalculator(base, settings=Settings()).compute_latest()

    assert period == "2026-03"
    assert len(rows) == 1
    assert rows[0].pnl_total == Decimal("2000.00")


def test_最新月份取自交易明细而不是有佣金的月份(base):
    """最新那个月全是未登记客户时，如实返回那个月 + 空列表。

    退回到上一个算得出钱的月份，会把「新数据来了但客户还没登记」这件事藏起来。
    """
    _txn(base, UID_A, 1000.00, "2026/03/02")
    _txn(base, UID_ORPHAN, 5000.00, "2026/04/09")

    period, rows, unmapped = CommissionCalculator(base, settings=Settings()).compute_latest()

    assert period == "2026-04"
    assert rows == []
    assert unmapped == [UID_ORPHAN]


def test_未登记清单是全表范围而不是只有最新月(base):
    _txn(base, UID_ORPHAN, 5000.00, "2026/01/09")
    _txn(base, UID_A, 1000.00, "2026/03/02")

    period, rows, unmapped = CommissionCalculator(base, settings=Settings()).compute_latest()

    assert period == "2026-03"
    assert unmapped == [UID_ORPHAN], "1 月的未登记客户不该因为只结算 3 月就被藏起来"


def test_空表时最新月份为空串(base):
    period, rows, unmapped = CommissionCalculator(base, settings=Settings()).compute_latest()

    assert period == ""
    assert rows == []


def test_求最新月份不额外读表(base):
    """默认月份是扫表时顺手记的。改成再遍历一遍交易明细的话，对账 API 调用量会翻倍。"""
    _txn(base, UID_A, 1000.00, "2026/03/02")

    calculator = CommissionCalculator(base, settings=Settings())
    calculator.compute_latest()

    assert base.tables[TBL_TXN].scan_count == 1


def test_显式指定月份时也记下表里的最新月份(base):
    _txn(base, UID_A, 1000.00, "2026/01/15")
    _txn(base, UID_A, 2000.00, "2026/03/02")

    calculator = CommissionCalculator(base, settings=Settings())
    calculator.compute(period="2026-01")

    assert calculator.latest_transaction_period == "2026-03"


# ---------- 汇总输出 ----------


def test_没有数据时汇总文案友好():
    assert "没有可结算" in summarize([])


def test_汇总文案包含渠道和金额(base):
    _txn(base, UID_A, 6805.40)
    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")
    text = summarize(rows)
    assert "R001" in text
    assert "2026-03" in text
    assert "1,361.08" in text


def test_汇总文案显式标注亏损月(base):
    """只输出一个 0，读的人分不清「亏了」和「没交易」。"""
    _txn(base, UID_A, -2935.10)
    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    text = summarize(rows)

    assert "2,935.10" in text, "要看得见亏了多少"
    assert "整月亏损" in text
    assert "不结转" in text, "要写明这是有意的规则，不是算错"
    assert "1 个渠道整月亏损" in text, "月份小结也要提一句"


def test_汇总文案不给盈利渠道加亏损标注(base):
    _txn(base, UID_A, 6805.40)
    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    assert "整月亏损" not in summarize(rows)


def test_月份合计不把亏损渠道算成负数(base):
    _txn(base, UID_A, -5000.00)
    _txn(base, UID_B, 3668.00)
    rows, _ = CommissionCalculator(base, settings=Settings()).compute(period="2026-03")

    text = summarize(rows)

    assert "合计应付 458.50 USD" in text, "亏损渠道按 0 计入合计，不倒扣别人的佣金"
