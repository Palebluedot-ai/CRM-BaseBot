"""佣金计算。

    佣金 = 该渠道名下所有客户在该月的 Pnl(USD) 合计 × 该渠道的分佣比例

三张表的连接链路：

    交易明细.客户UID  ──►  客户表.客户UID  ──►  渠道表.渠道编号  ──►  分佣比例

为什么在后端算而不是在 Base 里写公式：Base 的 ``FILTER`` 上限是 2 万条，而交易
明细是全量表且只会越来越长；再者跨表 rollup 的中间结果也有大小限制。后端按月
聚合后只往 Base 写少量汇总行，既避开上限，也让「这个数是怎么来的」可被测试。

金额用 Decimal 而不是 float：Pnl 是钱，累加上千行的浮点误差会让对账对不上。
客户UID 全程字符串，理由见 lark/values.py。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..lark.bitable import BitableClient
from ..lark.values import (
    UidHealthReport,
    assess_uid_health,
    extract_text,
    to_number,
    to_uid,
)
from . import schema

logger = logging.getLogger(__name__)

CENTS = Decimal("0.01")


class UnmappedClientError(RuntimeError):
    """交易明细里出现了没有登记归属渠道的客户。"""


@dataclass
class CommissionRow:
    period: str
    referral_no: str
    referral_name: str
    rate_percent: Decimal
    pnl_total: Decimal = Decimal("0")
    txn_count: int = 0
    client_uids: set[str] = field(default_factory=set)

    @property
    def payable(self) -> Decimal:
        return (self.pnl_total * self.rate_percent / Decimal(100)).quantize(
            CENTS, rounding=ROUND_HALF_UP
        )

    @property
    def client_count(self) -> int:
        return len(self.client_uids)


@dataclass(frozen=True)
class Referral:
    record_id: str
    no: str
    name: str
    rate_percent: Decimal
    status: str


def period_of(order_time: Any) -> str:
    """把订单时间归到 YYYY-MM。

    Bitable 日期字段是毫秒时间戳；导入的数据偶尔是 '2026/03/02' 这类字符串，
    两种都认。
    """
    if order_time is None or order_time == "":
        return ""

    if isinstance(order_time, int | float) and not isinstance(order_time, bool):
        moment = datetime.fromtimestamp(float(order_time) / 1000, tz=UTC)
        return moment.strftime("%Y-%m")

    text = extract_text(order_time)
    if not text:
        return ""

    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 2].strip(), fmt).strftime("%Y-%m")
        except ValueError:
            continue

    # 退一步：形如 2026/03 或 2026-03 开头的也认
    normalized = text.replace("/", "-")
    if len(normalized) >= 7 and normalized[4] == "-":
        return normalized[:7]

    logger.warning("无法解析订单时间 %r，该行不计入任何月份", order_time)
    return ""


class CommissionCalculator:
    def __init__(self, bitable: BitableClient, *, settings) -> None:
        self._bitable = bitable
        self._settings = settings
        # compute() 途中顺手攒下来的 UID，用于事后体检。
        # 用 set 而不是 list：唯一 UID 的数量受客户数约束，不会随交易笔数膨胀。
        self._seen_uids: set[str] = set()

    # ---------- 载入维表 ----------

    def load_referrals(self) -> dict[str, Referral]:
        referrals: dict[str, Referral] = {}
        for record in self._bitable.iter_records(self._settings.table_referral):
            no = extract_text(record.fields.get(schema.REFERRAL_NO))
            if not no:
                continue
            rate = to_number(record.fields.get(schema.REFERRAL_RATE)) or 0.0
            referrals[record.record_id] = Referral(
                record_id=record.record_id,
                no=no,
                name=extract_text(record.fields.get(schema.REFERRAL_NAME)),
                rate_percent=Decimal(str(rate)),
                status=extract_text(record.fields.get(schema.REFERRAL_STATUS)),
            )
        return referrals

    def load_client_map(self, referrals: dict[str, Referral]) -> dict[str, Referral]:
        """客户UID -> 所属渠道。"""
        mapping: dict[str, Referral] = {}
        for record in self._bitable.iter_records(self._settings.table_client):
            uid = to_uid(record.fields.get(schema.CLIENT_UID))
            if not uid:
                continue
            self._seen_uids.add(uid)

            linked = record.fields.get(schema.CLIENT_REFERRAL_LINK) or []
            referral = None
            for referral_record_id in _link_ids(linked):
                referral = referrals.get(referral_record_id)
                if referral is not None:
                    break

            if referral is None:
                logger.warning("客户 %s 的所属渠道解析不出来，跳过", uid)
                continue

            mapping[uid] = referral
        return mapping

    # ---------- 计算 ----------

    def compute(
        self,
        *,
        period: str | None = None,
        strict: bool = False,
    ) -> tuple[list[CommissionRow], list[str]]:
        """算出佣金汇总。

        ``period`` 给 ``YYYY-MM`` 就只算那个月，给 None 算全部月份。
        ``strict=True`` 时，遇到未登记归属的客户直接报错而不是跳过。

        返回 (汇总行, 未登记归属的客户UID)。
        """
        referrals = self.load_referrals()
        client_map = self.load_client_map(referrals)

        rows: dict[tuple[str, str], CommissionRow] = {}
        unmapped: set[str] = set()

        for record in self._bitable.iter_records(self._settings.table_transaction):
            uid = to_uid(record.fields.get(schema.TXN_CLIENT_UID))
            if not uid:
                continue
            self._seen_uids.add(uid)

            row_period = period_of(record.fields.get(schema.TXN_ORDER_TIME))
            if not row_period:
                continue
            if period and row_period != period:
                continue

            referral = client_map.get(uid)
            if referral is None:
                unmapped.add(uid)
                continue

            pnl = to_number(record.fields.get(schema.TXN_PNL))
            if pnl is None:
                continue

            key = (row_period, referral.no)
            row = rows.get(key)
            if row is None:
                row = CommissionRow(
                    period=row_period,
                    referral_no=referral.no,
                    referral_name=referral.name,
                    rate_percent=referral.rate_percent,
                )
                rows[key] = row

            row.pnl_total += Decimal(str(pnl))
            row.txn_count += 1
            row.client_uids.add(uid)

        if unmapped and strict:
            raise UnmappedClientError(
                f"有 {len(unmapped)} 个客户在交易明细里出现但没登记归属渠道，"
                f"佣金会算少。示例：{sorted(unmapped)[:5]}"
            )

        ordered = sorted(rows.values(), key=lambda r: (r.period, r.referral_no))
        return ordered, sorted(unmapped)

    def uid_health(self) -> UidHealthReport:
        """体检 compute() 过程中见到的所有 UID，看有没有 Excel 截断痕迹。

        不额外读一遍表 —— UID 是 compute() 途中顺手攒的，所以这个检查基本不花钱。
        """
        return assess_uid_health(sorted(self._seen_uids))


def _link_ids(value: Any) -> Iterable[str]:
    """关联字段可能是 ['recXXX']，也可能是 {'link_record_ids': [...]}。"""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                candidate = item.get("record_id") or item.get("id")
                if candidate:
                    yield candidate
    elif isinstance(value, dict):
        for item in value.get("link_record_ids", []) or []:
            yield item


def summarize(rows: list[CommissionRow]) -> str:
    if not rows:
        return "没有可结算的数据。"

    by_period: dict[str, list[CommissionRow]] = defaultdict(list)
    for row in rows:
        by_period[row.period].append(row)

    lines: list[str] = []
    for period in sorted(by_period):
        period_rows = by_period[period]
        total = sum((r.payable for r in period_rows), Decimal("0"))
        lines.append(f"{period}  合计应付 {total:,.2f} USD")
        for row in period_rows:
            lines.append(
                f"    {row.referral_no} {row.referral_name or '(未命名)':<20} "
                f"Pnl {row.pnl_total:>12,.2f} × {row.rate_percent}% "
                f"= {row.payable:>10,.2f}   "
                f"({row.client_count} 客户 / {row.txn_count} 笔)"
            )
    return "\n".join(lines)
