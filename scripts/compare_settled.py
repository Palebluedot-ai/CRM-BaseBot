#!/usr/bin/env python
"""已经结算的月份：当时结的 vs 按现在的资料重算，差在哪个渠道、为什么。**只读，不写。**

    uv run python scripts/compare_settled.py                  # 所有结算过的月份
    uv run python scripts/compare_settled.py --period 2026-08 # 只看一个月

## 为什么要有它（2026-09-30）

仪表盘上的「交易佣金明细」是按**现在**的资料算的（看板的「本笔佣金」），和当时写进
Commission Summary 的结算数不一样：结算之后改过比例、补登记了客户，旧月份的数就跟着变。
付钱永远以结算表为准；这个脚本把两边的差一个渠道一个渠道列出来，并说明原因，
决定要不要补发时拿它看。

原因只分三种，都能从结算表里存的那几列直接判断：

  · 比例改过：结算时存的「分佣比例」和现在渠道表里的不一样
  · 收入变了：结算后才登记（或改挂）的客户，他那个月的交易现在算进来了
  · 结算时没有这个渠道：这个渠道的客户全是结算之后才登记的
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.commission import CommissionCalculator, CommissionRow  # noqa: E402
from crm_basebot.domain.settlement import is_live  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import extract_text, to_number  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

ZERO = Decimal("0")
CENTS = Decimal("0.01")


@dataclass(frozen=True)
class Settled:
    period: str
    referral_no: str
    name: str
    rate: Decimal
    revenue: Decimal
    clients: int
    payable: Decimal


@dataclass(frozen=True)
class Diff:
    period: str
    referral_no: str
    name: str
    settled: Decimal
    now: Decimal
    reasons: tuple[str, ...]

    @property
    def delta(self) -> Decimal:
        return self.now - self.settled


def _dec(value) -> Decimal:
    number = to_number(value)
    return Decimal(str(number)) if number is not None else ZERO


def _rate(value: Decimal) -> str:
    return f"{value.normalize():f}%"


def compare(settled: list[Settled], now: list[CommissionRow]) -> list[Diff]:
    """两边按 (月份, 渠道) 对齐，只返回金额不一样的。纯函数，方便测。"""
    periods = {row.period for row in settled}
    current = {(row.period, row.referral_no): row for row in now if row.period in periods}
    before = {(row.period, row.referral_no): row for row in settled}

    diffs: list[Diff] = []
    for key in sorted(set(before) | set(current)):
        old, new = before.get(key), current.get(key)
        old_pay = old.payable if old else ZERO
        new_pay = new.payable if new else ZERO
        if old_pay == new_pay:
            continue
        reasons: list[str] = []
        if old is None:
            reasons.append("结算时没有这个渠道（客户都是结算之后才登记的）")
        elif new is None:
            reasons.append("现在这个月算不出这个渠道（客户被删、改挂或 AI 状态改过）")
        else:
            if old.rate != new.rate_percent:
                reasons.append(f"比例改过：{_rate(old.rate)} → {_rate(new.rate_percent)}")
            # 结算表里的收入是存进 Base 的数字（读回来是浮点），比到分就够了
            if old.revenue.quantize(CENTS) != new.revenue_total.quantize(CENTS):
                reasons.append(
                    f"收入 {old.revenue:,.2f} → {new.revenue_total:,.2f}"
                    f"（客户 {old.clients} → {new.client_count} 个，结算后登记或改挂的客户）"
                )
        name = (new.referral_name if new else "") or (old.name if old else "")
        diffs.append(Diff(key[0], key[1], name, old_pay, new_pay, tuple(reasons)))
    return diffs


def _load_settled(bitable: BitableClient, table_id: str, period: str | None) -> list[Settled]:
    out: list[Settled] = []
    for record in bitable.iter_records(table_id):
        f = record.fields
        month = extract_text(f.get(schema.COMM_PERIOD))
        if not month or (period and month != period) or is_live(f):
            continue
        out.append(
            Settled(
                period=month,
                referral_no=extract_text(f.get(schema.COMM_REFERRAL_NO)),
                name=extract_text(f.get(schema.COMM_REFERRAL_NAME)),
                rate=_dec(f.get(schema.COMM_RATE)),
                revenue=_dec(f.get(schema.COMM_REVENUE_TOTAL)),
                clients=int(to_number(f.get(schema.COMM_CLIENT_COUNT)) or 0),
                payable=_dec(f.get(schema.COMM_PAYABLE)),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="结算数 vs 现在重算，差在哪")
    parser.add_argument("--period", help="只看一个月，YYYY-MM")
    args = parser.parse_args(argv)

    settings = load_settings()
    require_settings(
        settings,
        "LARK_BASE_APP_TOKEN",
        "TABLE_REFERRAL",
        "TABLE_CLIENT",
        "TABLE_DAILY_BOARD",
        "TABLE_COMMISSION",
    )
    bitable = BitableClient(settings.base_app_token)

    settled = _load_settled(bitable, settings.table_commission, args.period)
    if not settled:
        print("结算表里没有要对的月份。")
        return 0
    now, _ = CommissionCalculator(bitable, settings=settings).compute(period=args.period)
    diffs = compare(settled, now)

    periods = sorted({row.period for row in settled})
    print("只读，没有改任何东西。付钱以结算表为准。\n")
    for period in periods:
        was = sum((r.payable for r in settled if r.period == period), ZERO)
        is_now = sum((r.payable for r in now if r.period == period), ZERO)
        month = [d for d in diffs if d.period == period]
        print(f"━━ {period}　结算 {was:,.2f}　现在重算 {is_now:,.2f}　差 {is_now - was:+,.2f}")
        if not month:
            print("   一样，没有差别。\n")
            continue
        for d in sorted(month, key=lambda d: abs(d.delta), reverse=True):
            print(
                f"   {d.referral_no} {d.name}：{d.settled:,.2f} → {d.now:,.2f}（{d.delta:+,.2f}）"
            )
            for reason in d.reasons:
                print(f"       · {reason}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
