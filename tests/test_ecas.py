"""ECAS 返佣：费率判读、归月、汇总、以及「它不碰交易佣金」这条边界。

这套账和交易佣金唯一的共用物是 ``Referral Information`` 里的编号和名字。
比例、金额、月份全都来自 ECAS 自己的数据 —— 最后一组测试就是钉这件事的：
把渠道表的分佣比例改成一个离谱的数，ECAS 算出来的钱一分不变。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from crm_basebot.domain import ecas, schema
from crm_basebot.domain.names import norm, tokens

SG = ZoneInfo("Asia/Singapore")


def _app(
    client: str,
    amount: str,
    period: str,
    code: str = "R001",
    name: str = "JIANG JUN",
    rate: str = "50",
) -> ecas.EcasApplication:
    return ecas.EcasApplication(
        client_name=client,
        amount=Decimal(amount),
        period=period,
        payee=ecas.Payee(code=code, name=name),
        rate_percent=Decimal(rate),
    )


# ---------- 费率那一栏是 0.5 还是 50 ----------


def test_小数写法按行内的返佣金额认出来():
    # 来源表就是这么写的：5000 × 0.5 = 2500
    assert ecas.resolve_rate_percent(Decimal("5000"), Decimal("0.5"), Decimal("2500")) == Decimal(
        "50.0"
    )


def test_百分数写法也认得出来():
    # 同样是 50%，只是那一栏写成了 50
    assert ecas.resolve_rate_percent(Decimal("5000"), Decimal("50"), Decimal("2500")) == Decimal(
        "50"
    )


def test_百分之一和百分之百形态一样但金额分得开():
    # 1 既可能是 100%（小数写法）也可能是 1%（百分数写法）。形态上无解，
    # 但行内的返佣金额直接把答案写出来了 —— 这就是不靠形态判断的理由。
    assert ecas.resolve_rate_percent(Decimal("5000"), Decimal("1"), Decimal("5000")) == Decimal(
        "100"
    )
    assert ecas.resolve_rate_percent(Decimal("5000"), Decimal("1"), Decimal("50")) == Decimal("1")


def test_对不上账的行直接抛而不是猜一个():
    with pytest.raises(ecas.EcasRateError) as exc_info:
        ecas.resolve_rate_percent(Decimal("5000"), Decimal("0.5"), Decimal("1234"))
    assert "对不上账" in str(exc_info.value)


def test_金额为零时说不清是百分之五十还是百分之零点五():
    with pytest.raises(ecas.EcasRateError) as exc_info:
        ecas.resolve_rate_percent(Decimal("0"), Decimal("0.5"), Decimal("0"))
    assert "没法作证" in str(exc_info.value)


def test_比例为零时两种读法都是零没有歧义():
    assert ecas.resolve_rate_percent(Decimal("5000"), Decimal("0"), Decimal("0")) == Decimal("0")


def test_允许一分钱的进位误差():
    # 来源表里的金额是进位到分之后的结果，差一分算对得上
    assert ecas.resolve_rate_percent(
        Decimal("3333"), Decimal("0.15"), Decimal("499.95")
    ) == Decimal("15.00")


# ---------- 归月 ----------


def test_归月按业务时区不按UTC():
    # 新加坡 9 月 1 日 07:00 = UTC 8 月 31 日 23:00。按 UTC 取月份会掉进上个月。
    moment = datetime(2026, 9, 1, 7, 0, tzinfo=SG)
    assert ecas.period_of(moment, tz=SG) == "2026-09"
    assert ecas.period_of(moment, tz=ZoneInfo("UTC")) == "2026-08"


def test_来源表里的naive时间按业务时区解读():
    # xlsx 里的时间就是同事在业务时区看到的那个钟点，不是 UTC
    assert ecas.period_of(datetime(2026, 8, 27, 11, 42, 56), tz=SG) == "2026-08"


# ---------- 汇总 ----------


def test_逐行进位到分再求和和财务同一口径():
    # 财务那份 recompute 的 Notes：每一笔先进位到两位小数，再求和。
    # 三笔 33.333…，逐行进位是 11.11×3 = 33.33；先加后进位会得到 33.34。
    apps = [_app(f"C{i}", "33.333", "2026-08", rate="33.333") for i in range(3)]
    (row,) = ecas.aggregate(apps)
    assert row.payable == Decimal("33.33")


def test_一个渠道一个月有好几档比例时比例说明列出每一档():
    apps = [
        _app("A", "5000", "2026-09", rate="50"),
        _app("B", "5000", "2026-09", rate="20"),
        _app("C", "5000", "2026-09", rate="20"),
    ]
    (row,) = ecas.aggregate(apps)
    assert row.rate_note == "20%×2笔 / 50%×1笔"
    assert row.payable == Decimal("4500.00")


def test_只有一档比例时比例说明不带笔数也不带尾零():
    (row,) = ecas.aggregate([_app("A", "5000", "2026-08", rate="50.0")])
    assert row.rate_note == "50%"


def test_没有介绍人的申请不产生任何汇总行():
    apps = [
        ecas.EcasApplication("散客", Decimal("5000"), "2026-08"),
        _app("A", "5000", "2026-08"),
    ]
    rows = ecas.aggregate(apps)
    assert len(rows) == 1
    assert rows[0].client_names == {"A"}


def test_渠道表里没登记的收款方照样结算只是没有编号():
    # 钱是欠着的。把它藏起来，合计就和来源表对不上了。
    apps = [_app("A", "5000", "2026-08", code="", name="Teo Yu Yuan", rate="30")]
    (row,) = ecas.aggregate(apps)
    assert row.payee.code == ""
    assert row.payable == Decimal("1500.00")
    assert "渠道表里没有这个名字" in ecas.summarize([row])


def test_同一个客户在同一个月申请两次算两笔但只算一个客户():
    apps = [_app("A", "5000", "2026-08"), _app("A", "5000", "2026-08")]
    (row,) = ecas.aggregate(apps)
    assert (row.txn_count, row.client_count) == (2, 1)
    assert row.payable == Decimal("5000.00")


def test_整月合计为负时保底零不倒扣():
    (row,) = ecas.aggregate([_app("A", "-5000", "2026-08")])
    assert row.fee_total == Decimal("-2500.00")
    assert row.payable == Decimal("0")
    assert row.is_loss_month


def test_指定月份时只算那个月():
    apps = [_app("A", "5000", "2026-07"), _app("B", "5000", "2026-08")]
    rows = ecas.aggregate(apps, period="2026-08")
    assert [r.period for r in rows] == ["2026-08"]


def test_按月份和渠道编号排序没编号的排在最后():
    apps = [
        _app("A", "5000", "2026-08", code="R099", name="Zed"),
        _app("B", "5000", "2026-08", code="", name="Aaa"),
        _app("C", "5000", "2026-08", code="R001", name="Bbb"),
    ]
    assert [r.payee.code for r in ecas.aggregate(apps)] == ["R001", "R099", ""]


def test_月份清单只看有介绍人的申请():
    apps = [
        ecas.EcasApplication("散客", Decimal("5000"), "2026-12"),
        _app("A", "5000", "2026-08"),
        _app("B", "5000", "2026-07"),
    ]
    assert ecas.periods_in(apps) == ["2026-07", "2026-08"]


def test_没有可结算的返佣时汇总是一句人话():
    assert ecas.summarize([]) == "没有可结算的 ECAS 返佣。"


# ---------- 名字比对 ----------


def test_全角右括号和半角右括号算同一个人():
    # 2026-09 在这里对不上人：名册里是半角左括号配全角右括号
    assert norm("Kevin Yu (于海峰）") == norm("Kevin Yu (于海峰)")


def test_姓名顺序颠倒和多余句点靠tokens才兜得住():
    assert norm("KE JIAHUI") != norm("JIAHUI KE")
    assert tokens("KE JIAHUI") == tokens("JIAHUI KE")
    assert tokens("BG TECHNOLOGY VENTURE PTE. LTD.") == tokens("BG TECHNOLOGY VENTURE PTE LTD")


# ---------- 边界：ECAS 不读交易佣金的比例 ----------


def test_公式不许顺着关联去渠道表取比例():
    """关联过去只准取编号和名字。

    同一个渠道在交易那边和 ECAS 这边可以是两个完全不同的比例
    （2026-09 那两笔 ECAS 是 20%），同一个客户两边各付一次。哪天有人图省事，
    把 ECAS 的比例改成顺着关联去 ``Referral Information`` 里取，这个测试会红。

    注意 ``ECAS_RATE`` 和 ``REFERRAL_RATE`` 字面上是同一个中文列名「分佣比例」，
    只是住在两张不同的表里 —— 所以这里比的是**两跳引用**，不是列名本身。
    """
    through_link = f"[{ecas.ECAS_REFERRAL_LINK}].[{schema.REFERRAL_RATE}]"
    for expression, _ in ecas.ECAS_FORMULAS.values():
        assert through_link not in expression


def test_ECAS的代码里一次都没有引用渠道表的分佣比例():
    """比注释硬的一道栏杆：整个 ECAS 链路的源码里不许出现 ``REFERRAL_RATE``。

    列名相同（都叫「分佣比例」）让这件事很容易在 review 里滑过去，所以用源码来钉。
    """
    import ast
    import inspect

    from crm_basebot.jobs import ecas_reconcile

    for module in (ecas, ecas_reconcile):
        # 走 AST 而不是 grep 源码：模块开头的说明里就写着「永远不读 REFERRAL_RATE」，
        # 按文本查会被自己的注释绊倒。要钉的是**取值**，不是提到这个名字。
        tree = ast.parse(inspect.getsource(module))
        offenders = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "REFERRAL_RATE"
        ]
        assert offenders == [], f"{module.__name__} 里读了渠道表的分佣比例"


def test_佣金公式对没填比例的行留空而不是算成零():
    expression, _ = ecas.ECAS_FORMULAS[ecas.ECAS_FEE]
    # 0 在这一列等于宣称「这笔没有返佣」，实际是「这笔没有介绍人」，两码事
    assert expression.startswith(f'IF(ISBLANK([{ecas.ECAS_RATE}]), ""')


def test_汇总表的比例说明是文本不是数字():
    # 一个渠道一个月可以有好几档比例，硬塞一个数字进去不管填哪档都是在撒谎
    from crm_basebot.lark.bitable import FIELD_TYPE_TEXT

    assert ecas.ECAS_COMMISSION_FIELDS[ecas.ECOMM_RATE_NOTE] == FIELD_TYPE_TEXT


# ---------- 钉住和财务对上的那个数 ----------


def test_二零二六年八月合计等于财务给的六万五():
    """财务那份 ``08_August2026_ReferralFee_Recomputed`` 的 ECAS 分页合计是 65,000.00。

    来源表里 2026-08 的构成：JIANG JUN 24 笔 × 5,000 × 50%，Mo Xuelei 1 笔 × 10,000 × 50%。
    这个数是这套账唯一的外部校验点 —— 算法改动一旦让它变了，就是算错了。
    """
    apps = [_app(f"C{i}", "5000", "2026-08", code="R095", name="JIANG JUN") for i in range(24)]
    apps.append(_app("CHANGZHENG YE", "10000", "2026-08", code="", name="Mo Xuelei"))

    rows = ecas.aggregate(apps, period="2026-08")
    assert sum((r.payable for r in rows), Decimal("0")) == Decimal("65000.00")
