"""ECAS 开户返佣：表结构、费率判读、按月结算。

**这是和交易佣金完全独立的第二套账。** 两边唯一的交集是「钱最后由同一个机器人报出去」，
除此之外不共用任何一张表、任何一个费率、任何一行代码路径：

    交易佣金   Daily Revenue Board ─► Referred Client ─► Referral Information.分佣比例
    ECAS 返佣  ECAS Applications ──────────────────────► 这一行自己的分佣比例

分开的理由不是洁癖，是事实：同一个渠道两边的比例**可以不一样**（2026-09 那两笔
ECAS 是 20%，同一批人在交易那边是别的数），同一个客户两边**各付一次**
（CHANGZHENG YE 在 2026-08 同时拿了 ECAS 5,000 和交易佣金 3,474.08，两笔都付了）。
把 ECAS 的比例去 ``Referral Information`` 里取，就是把这两件事混成一件 ——
所以这个模块**永远不读** ``schema.REFERRAL_RATE``。关联到渠道表只为了拿编号和名字，
不为了拿比例。

## 费率那一栏是 0.5 还是 50

来源 xlsx 的 ``%`` 栏写的是**小数**（0.5 = 50%），而 Base 里别处的「分佣比例」一律是
**百分数**（50 = 50%）。两种写法在同一个 Base 里并存，早晚有人看着 0.5 以为是 0.5%。
所以导入时统一换算成百分数。

换算**不靠约定，靠数据自己作证**：每一行都带着 ``Amount of Referral Fee``，
拿 ``金额 × 比例`` 和 ``金额 × 比例 ÷ 100`` 各算一遍，哪个对得上那一栏，那个就是真的
（见 ``resolve_rate_percent``）。两个都对不上就拒绝这一行 —— 与其猜错一个费率，
不如让人来看一眼。哪天上游改成写 50 而不是 0.5，这里不用改代码也不会算错。

## 舍入

财务那份 recompute 的 Notes 写明：「Each individual referral fee is rounded to two
decimal places before … totals are summed」。ECAS 的比例是逐行的，所以这里天然就是
**逐行进位到分再求和**，和财务同一口径。2026-08 实测两边都是 65,000.00。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal

from ..lark.bitable import (
    FIELD_TYPE_DATETIME,
    FIELD_TYPE_FORMULA,
    FIELD_TYPE_NUMBER,
    FIELD_TYPE_SINGLE_LINK,
    FIELD_TYPE_TEXT,
)
from . import schema

CENTS = Decimal("0.01")

# 判读费率时允许的误差。来源表里的金额已经是进位到分的结果，所以差一分以内算对得上，
# 再宽就会把 0.5 和 0.51 混为一谈。
RATE_TOLERANCE = Decimal("0.01")

# ---------- 表 1：ECAS 申请（镜像 + 逐笔返佣） ----------

TABLE_ECAS_NAME = "ECAS Applications"

ECAS_CLIENT_NAME = "客户名称"  # Client Name
ECAS_AMOUNT = "ECAS金额"  # ECAS Revenue
ECAS_APPLIED_AT = "申请时间"  # Application Time
ECAS_SALES_NAME = "负责销售"  # Sales in Charge
ECAS_CLIENT_UID = "客户UID"  # UID，来源表里大多是空的
ECAS_REFERRER_NAME = "渠道名称"  # Referrer，照抄来源表的写法
ECAS_REFERRAL_LINK = "所属渠道"  # 单向关联 -> Referral Information
ECAS_REFERRAL_NO = "渠道编号"  # 公式：关联过去取编号
ECAS_RATE = "分佣比例"  # 百分数，50 表示 50%
ECAS_FEE = "ECAS佣金"  # 公式：金额 × 比例 / 100
ECAS_MONTH = "月份"  # 公式：申请时间所属月份

# 顺序就是界面上的列顺序。
ECAS_FIELDS: dict[str, int] = {
    ECAS_CLIENT_NAME: FIELD_TYPE_TEXT,
    ECAS_AMOUNT: FIELD_TYPE_NUMBER,
    ECAS_APPLIED_AT: FIELD_TYPE_DATETIME,
    ECAS_SALES_NAME: FIELD_TYPE_TEXT,
    # 文本。18-19 位 UID 存成数字会在服务端就被 float64 抹平低位（见 lark/values.py）。
    ECAS_CLIENT_UID: FIELD_TYPE_TEXT,
    ECAS_REFERRER_NAME: FIELD_TYPE_TEXT,
    ECAS_REFERRAL_LINK: FIELD_TYPE_SINGLE_LINK,
    ECAS_REFERRAL_NO: FIELD_TYPE_FORMULA,
    ECAS_RATE: FIELD_TYPE_NUMBER,
    ECAS_FEE: FIELD_TYPE_FORMULA,
    ECAS_MONTH: FIELD_TYPE_FORMULA,
}

ECAS_FORMULAS: dict[str, tuple[str, int]] = {
    ECAS_REFERRAL_NO: (
        f"[{ECAS_REFERRAL_LINK}].[{schema.REFERRAL_NO}]",
        schema.FORMULA_DATA_TYPE_TEXT,
    ),
    # 和看板的「本笔佣金」同一个写法，包括那层 ISBLANK 保护：没填比例的行（来源表里
    # 六成的申请没有介绍人）留空才是对的。空值参与乘法会算出 0，而 0 在这一列等于宣称
    # 「这笔没有返佣」，实际是「这笔没有介绍人」。
    ECAS_FEE: (
        f'IF(ISBLANK([{ECAS_RATE}]), "", [{ECAS_AMOUNT}] * [{ECAS_RATE}] / 100)',
        schema.FORMULA_DATA_TYPE_NUMBER,
    ),
    # 申请时间存的是业务时区的那一刻，平台时区实测 UTC+8，和 Asia/Singapore 同偏移，
    # 所以这里算出来的月份和 Python 侧的 period_of 一致。业务时区换成别的偏移时这一列
    # 会错月 —— 结算不依赖它（Python 自己算），但界面上会对不上，那时要连这条公式一起改。
    ECAS_MONTH: (
        f'TEXT([{ECAS_APPLIED_AT}], "yyyy-MM")',
        schema.FORMULA_DATA_TYPE_TEXT,
    ),
}

# ---------- 表 2：ECAS 佣金汇总（按月，后端写入） ----------

TABLE_ECAS_COMMISSION_NAME = "ECAS Commission Summary"

ECOMM_PERIOD = "结算月份"
ECOMM_REFERRAL_NO = "渠道编号"
ECOMM_REFERRAL_NAME = "渠道名称"
ECOMM_CLIENT_COUNT = "客户数"
ECOMM_TXN_COUNT = "记录笔数"
ECOMM_AMOUNT_TOTAL = "ECAS金额合计"
ECOMM_RATE_NOTE = "比例说明"
ECOMM_PAYABLE = "应付佣金"
ECOMM_COMPUTED_AT = "计算时间"

ECAS_COMMISSION_FIELDS: dict[str, int] = {
    ECOMM_PERIOD: FIELD_TYPE_TEXT,
    ECOMM_REFERRAL_NO: FIELD_TYPE_TEXT,
    ECOMM_REFERRAL_NAME: FIELD_TYPE_TEXT,
    ECOMM_CLIENT_COUNT: FIELD_TYPE_NUMBER,
    ECOMM_TXN_COUNT: FIELD_TYPE_NUMBER,
    ECOMM_AMOUNT_TOTAL: FIELD_TYPE_NUMBER,
    # 刻意**不是**数字。ECAS 的比例是逐行的，一个渠道一个月里可以有好几档
    # （2026-09 那两笔是 20%，同一批人别的月份是 50%）。硬塞一个数字进去，
    # 不管填哪一档都是在撒谎，填加权平均则会被人当成「合同比例」去对账。
    ECOMM_RATE_NOTE: FIELD_TYPE_TEXT,
    ECOMM_PAYABLE: FIELD_TYPE_NUMBER,
    ECOMM_COMPUTED_AT: FIELD_TYPE_DATETIME,
}


class EcasRateError(ValueError):
    """这一行的比例判读不出来。消息可以直接打印给人看。"""


def resolve_rate_percent(
    amount: Decimal,
    raw_rate: Decimal,
    stated_fee: Decimal,
    *,
    where: str = "",
) -> Decimal:
    """把来源表 ``%`` 栏的值换算成百分数（50 表示 50%）。

    判据是那一行自己带的 ``Amount of Referral Fee``：

        金额 × 比例        对得上  ->  ``%`` 栏是小数，乘 100
        金额 × 比例 ÷ 100  对得上  ->  ``%`` 栏已经是百分数，原样用

    两个都对不上就抛错。**不要**改成「小于 1 就当小数」那种形态判断 ——
    1% 写成 1、100% 写成 1，形态一模一样，靠猜必然出事，而这里手上就有能作证的数。
    """
    as_fraction = amount * raw_rate
    as_percent = amount * raw_rate / Decimal(100)
    fits_fraction = abs(as_fraction - stated_fee) <= RATE_TOLERANCE
    fits_percent = abs(as_percent - stated_fee) <= RATE_TOLERANCE

    if fits_fraction and fits_percent:
        # 两个乘积相等，只可能是比例为 0 或金额为 0。比例为 0 时两种读法都得 0，没有歧义；
        # 金额为 0 时这一行根本不作证，0.5 到底是 50% 还是 0.5% 谁也不知道。
        if raw_rate == 0:
            return Decimal("0")
        raise EcasRateError(
            f"{where}金额是 0，这一行没法作证 ``%`` 栏的 {raw_rate} 是 {raw_rate * 100}% "
            f"还是 {raw_rate}% —— 请人确认后再导"
        )
    if fits_fraction:
        return raw_rate * Decimal(100)
    if fits_percent:
        return raw_rate

    raise EcasRateError(
        f"{where}对不上账：金额 {amount}、比例栏 {raw_rate}、表里写的返佣 {stated_fee}。"
        f"按小数读是 {as_fraction}，按百分数读是 {as_percent}，两个都不等于 {stated_fee}"
    )


@dataclass(frozen=True)
class Payee:
    """收这笔 ECAS 返佣的渠道。

    ``code`` 为空表示渠道表里没有这个名字。这种行**照样结算** —— 钱是欠着的，
    把它藏起来只会让合计对不上来源表。汇总里编号留空，由人去把渠道补登记。
    """

    code: str
    name: str

    @property
    def key(self) -> str:
        return self.code or f"?{self.name}"

    @property
    def label(self) -> str:
        return f"{self.code} {self.name}".strip()


@dataclass
class EcasApplication:
    """一笔 ECAS 申请。没有介绍人的申请 ``payee`` 是 None。

    ``client_uid`` 只拿来在详情卡上把同一个客户的交易和 ECAS 排到同一行，
    不参与结算。来源表里大多是空的，空的时候按名字对（见 ``referral_history``）。
    """

    client_name: str
    amount: Decimal
    period: str
    payee: Payee | None = None
    rate_percent: Decimal | None = None
    client_uid: str = ""

    @property
    def fee(self) -> Decimal:
        """这一笔的返佣，进位到分。没有介绍人时是 0。"""
        if self.payee is None or self.rate_percent is None:
            return Decimal("0")
        return (self.amount * self.rate_percent / Decimal(100)).quantize(
            CENTS, rounding=ROUND_HALF_UP
        )


@dataclass
class EcasCommissionRow:
    period: str
    payee: Payee
    amount_total: Decimal = Decimal("0")
    txn_count: int = 0
    client_names: set[str] = field(default_factory=set)
    rate_counts: Counter[Decimal] = field(default_factory=Counter)
    # 逐行进位到分之后累加，理由见模块开头「舍入」。
    fee_total: Decimal = Decimal("0")

    @property
    def payable(self) -> Decimal:
        """应付返佣。整月合计为负时按 0 保底，规则和交易佣金一致：不倒扣，不结转。

        ECAS 金额是开户金额，实务上不会是负的，所以这条保底大多数月份是空转。
        保留是因为冲销/校准会让某个月合计变成负数，规则要能兜住那一刻。
        """
        return max(Decimal("0"), self.fee_total)

    @property
    def is_loss_month(self) -> bool:
        return self.fee_total < Decimal("0")

    @property
    def client_count(self) -> int:
        return len(self.client_names)

    @property
    def rate_note(self) -> str:
        """比例说明，例如 ``50%`` 或 ``50%×24笔 / 20%×2笔``。"""
        if not self.rate_counts:
            return ""
        if len(self.rate_counts) == 1:
            (rate,) = self.rate_counts
            return f"{_fmt_rate(rate)}%"
        ordered = sorted(self.rate_counts.items(), key=lambda kv: (-kv[1], -kv[0]))
        return " / ".join(f"{_fmt_rate(rate)}%×{count}笔" for rate, count in ordered)


def _fmt_rate(rate: Decimal) -> str:
    """50 -> "50"，12.5 -> "12.5"。去掉没意义的尾零，别让界面上出现 50.00%。"""
    text = format(rate.normalize(), "f")
    return text


def period_of(applied_at: datetime, *, tz: tzinfo) -> str:
    """申请时间 -> YYYY-MM。

    ``tz`` 刻意没有默认值：悄悄退回 UTC，每个月 1 号早上八点前的申请就会掉进上个月
    （理由和 ``commission.period_of`` 一模一样）。naive 的时间按业务时区解读 ——
    来源 xlsx 里的时间就是同事在业务时区看到的那个钟点。
    """
    moment = applied_at if applied_at.tzinfo else applied_at.replace(tzinfo=tz)
    return moment.astimezone(tz).strftime("%Y-%m")


def aggregate(
    applications: list[EcasApplication], *, period: str | None = None
) -> list[EcasCommissionRow]:
    """按 (月份, 渠道) 汇总。没有介绍人的申请不产生任何行。"""
    rows: dict[tuple[str, str], EcasCommissionRow] = {}
    for app in applications:
        if app.payee is None or app.rate_percent is None or not app.period:
            continue
        if period and app.period != period:
            continue
        key = (app.period, app.payee.key)
        row = rows.get(key)
        if row is None:
            row = EcasCommissionRow(period=app.period, payee=app.payee)
            rows[key] = row
        row.amount_total += app.amount
        row.txn_count += 1
        row.client_names.add(app.client_name)
        row.rate_counts[app.rate_percent] += 1
        row.fee_total += app.fee
    return sorted(rows.values(), key=lambda r: (r.period, r.payee.code or "ZZZ", r.payee.name))


def periods_in(applications: list[EcasApplication]) -> list[str]:
    """出现过的月份，从早到晚。只看有介绍人的申请 —— 没介绍人的月份没有账可结。"""
    return sorted(
        {
            a.period
            for a in applications
            if a.payee is not None and a.rate_percent is not None and a.period
        }
    )


def summarize(rows: list[EcasCommissionRow]) -> str:
    if not rows:
        return "没有可结算的 ECAS 返佣。"

    by_period: dict[str, list[EcasCommissionRow]] = defaultdict(list)
    for row in rows:
        by_period[row.period].append(row)

    lines: list[str] = []
    for period in sorted(by_period):
        period_rows = by_period[period]
        total = sum((r.payable for r in period_rows), Decimal("0"))
        lines.append(f"{period}  ECAS 合计应付 {total:,.2f} USD")
        for row in period_rows:
            label = row.payee.label or "(未命名)"
            lines.append(
                f"    {label[:34]:<36} 开户 {row.amount_total:>12,.2f} × {row.rate_note:<18}"
                f"= {row.payable:>10,.2f}   ({row.client_count} 客户 / {row.txn_count} 笔)"
            )
            if not row.payee.code:
                lines.append(
                    "         └─ 渠道表里没有这个名字，返佣照算但没有编号 ——"
                    " 要么去渠道表把它登记了，要么确认名字写法"
                )
            if row.is_loss_month:
                lines.append(
                    f"         └─ 整月合计为负 {abs(row.fee_total):,.2f} USD，"
                    "按业务规则保底 0：不倒扣，也不结转到下个月"
                )
    return "\n".join(lines)
