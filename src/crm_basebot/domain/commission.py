"""佣金计算。

    佣金 = max(0, 该渠道名下所有客户在该月的 总收入(opt+现货) 合计 × 该渠道的分佣比例)

那个 ``max(0, ...)`` 是业务规则不是技术细节：整月为负的渠道佣金按 0 保底，不倒扣也
不结转到下个月。完整说明和拍板时间见 ``CommissionRow.payable``。

**基数从 Pnl 切到毛收入是 2026-09-09 的业务决定。** 原方案按 Pnl(USD) 算，
现在按看板里已经聚合好的「总收入(opt+现货)」算。毛收入几乎不会为负，所以那个
`max(0, ...)` 保底大多数月份是空转 —— 但保留，因为「有可能是负」和「几乎不会是负」
是两码事：退款/冲销/校准会让某个月的收入合计变成负数，规则要能兜住那一刻。

三张表的连接链路：

    日读看板.客户UID  ──►  客户表.客户UID  ──►  渠道表.渠道编号  ──►  分佣比例

为什么在后端算而不是在 Base 里写公式：Base 的 ``FILTER`` 上限是 2 万条，而日读
看板是每天追加的表且只会越来越长；再者跨表 rollup 的中间结果也有大小限制。后端
按月聚合后只往 Base 写少量汇总行，既避开上限，也让「这个数是怎么来的」可被测试。

金额用 Decimal 而不是 float：收入是钱，累加上千行的浮点误差会让对账对不上。
客户UID 全程字符串，理由见 lark/values.py。

归月先把订单时间换算到业务时区（``BUSINESS_TIMEZONE``，默认 Asia/Singapore）再取年月，
理由见 ``period_of``。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

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
    """日读看板里出现了没有登记归属渠道的客户。"""


@dataclass
class CommissionRow:
    period: str
    referral_no: str
    referral_name: str
    rate_percent: Decimal
    revenue_total: Decimal = Decimal("0")
    txn_count: int = 0
    client_uids: set[str] = field(default_factory=set)

    @property
    def gross_payable(self) -> Decimal:
        """按比例直接算出来的金额，**可能是负数**（虽然按毛收入算时几乎不会）。

        存在的意义是让「保底 0」这一步是可见的。真出现负收入合计（退款/冲销/校准）
        时报表上看到收入是负的、应付是 0，能对上这里的原始值，而不用去猜中间发生
        了什么。
        """
        return (self.revenue_total * self.rate_percent / Decimal(100)).quantize(
            CENTS, rounding=ROUND_HALF_UP
        )

    @property
    def payable(self) -> Decimal:
        """应付佣金。整月收入为负时按 0 保底。

        **这是一条业务规则，不是技术上的取舍。** 不要因为「负数看起来不对」就来改它，
        也不要因为「max(0, x) 看起来像在掩盖问题」就把它删掉。规则的完整内容是：

            payable = max(0, revenue_total × rate)

          · 负值月**不倒扣** —— 渠道不会因为当月合计为负而倒欠我们佣金
          · 负值**不结转** —— 这个月的负值不会去冲抵下个月的佣金，每个月独立结算

        「不结转」是使用方在 2026-08-30 明确决定的，不是默认行为，也不是漏了没做。
        如果哪天要改成结转，那是一次业务规则变更，得先有人拍板 —— 因为结转会让
        「这个月该付多少」依赖于之前所有月份，跨月的账要重算。

        规则原本是为 Pnl 基数写的（每月都可能亏损）；2026-09-09 基数切到毛收入
        之后大多数月份不会触发保底，但规则条款保持不变 —— 退款/冲销那种边界情况
        仍然要兜住。

        注意 ``revenue_total`` 仍然如实保留负值，只有应付佣金被保底。把收入也截成
        0 会让报表看不出这个渠道当月是负的，对账时说不清账。
        """
        return max(Decimal("0"), self.gross_payable)

    @property
    def is_loss_month(self) -> bool:
        """这个渠道这个月合计为负。

        判据是严格小于 0：合计恰好为 0 不算负值月，只是这个月没进账，
        两者在报表上不该长成一样。
        """
        return self.revenue_total < Decimal("0")

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


def period_of(order_time: Any, *, tz: tzinfo) -> str:
    """把订单时间归到 YYYY-MM。

    Bitable 日期字段是 UTC 毫秒时间戳，归月必须先换算到业务时区 ``tz``：Base 里
    显示「3 月 1 日 00:30」的交易，UTC 还是 2 月 28 日，按 UTC 取月份会把每个月
    1 号凌晨的交易全算进上个月。``tz`` 刻意没有默认值 —— 悄悄退回 UTC 就是这个
    bug 本身。

    导入的数据偶尔是 '2026/03/02' 这类字符串，没有时区可言，照字面归月。
    """
    if order_time is None or order_time == "":
        return ""

    if isinstance(order_time, int | float) and not isinstance(order_time, bool):
        moment = datetime.fromtimestamp(float(order_time) / 1000, tz=tz)
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
        # 归月用的业务时区。名字在 Settings 里已经校验过，这里不会炸。
        self._tz = ZoneInfo(settings.business_timezone)
        # compute() 途中顺手攒下来的 UID，用于事后体检。
        # 用 set 而不是 list：唯一 UID 的数量受客户数约束，不会随交易笔数膨胀。
        self._seen_uids: set[str] = set()
        # 交易明细里出现过的最大月份，同样是 compute() 途中顺手记的。
        # 「有数据的最新月份」是 reconcile 不传 --period 时的默认结算范围，
        # 单独再扫一遍表去求它，等于让每次对账的 API 调用量翻倍。
        self._latest_period = ""

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

        for record in self._bitable.iter_records(self._settings.table_daily_board):
            uid = to_uid(record.fields.get(schema.BOARD_CLIENT_UID))
            if not uid:
                continue
            self._seen_uids.add(uid)

            row_period = period_of(record.fields.get(schema.BOARD_ORDER_DATE), tz=self._tz)
            if not row_period:
                continue

            # 刻意放在 period 过滤**之前**：这个值的语义是「表里最新的月份」，
            # 跟这次算的是哪个月无关。YYYY-MM 按字典序比就是按时间序比。
            self._latest_period = max(self._latest_period, row_period)

            if period and row_period != period:
                continue

            referral = client_map.get(uid)
            if referral is None:
                unmapped.add(uid)
                continue

            revenue = to_number(record.fields.get(schema.BOARD_TOTAL_REVENUE))
            if revenue is None:
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

            row.revenue_total += Decimal(str(revenue))
            row.txn_count += 1
            row.client_uids.add(uid)

        if unmapped and strict:
            raise UnmappedClientError(
                f"有 {len(unmapped)} 个客户在日读看板里出现但没登记归属渠道，"
                f"佣金会算少。示例：{sorted(unmapped)[:5]}"
            )

        ordered = sorted(rows.values(), key=lambda r: (r.period, r.referral_no))
        return ordered, sorted(unmapped)

    def compute_latest(self, *, strict: bool = False) -> tuple[str, list[CommissionRow], list[str]]:
        """只算日读看板里**最新有数据的那个月**，并把月份一起返回。

        为什么默认是这个而不是「上个月」：写死上个月，在月初跑的时候会算出一片空白
        （上个月的看板还没导完），而它又恰好在「这个月的数据其实已经有了」的时候
        什么都不说。跟着数据走，默认行为总是落在真正有东西可看的那批数据上。

        月份取的是**看板里的最大月份**，而不是「有佣金可算的最大月份」。差别在于：
        如果最新那个月的记录全部来自未登记归属的客户，这里会如实返回那个月 + 一个空
        列表，配合 unmapped 告警就能看出「新数据来了，但客户还没登记」—— 这正是需要
        被看见的状态。要是退回到上一个算得出钱的月份，这件事就被藏起来了。

        返回的 unmapped 是**全表范围**的，不限于这个月。未登记归属是数据问题，
        不该因为这次只结算一个月就被藏起来。
        """
        rows, unmapped = self.compute(strict=strict)
        latest = self._latest_period
        return latest, [row for row in rows if row.period == latest], unmapped

    @property
    def latest_board_period(self) -> str:
        """日读看板里出现过的最大月份。compute() 跑完才有值，空表返回空串。"""
        return self._latest_period

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
        loss_rows = [r for r in period_rows if r.is_loss_month]

        header = f"{period}  合计应付 {total:,.2f} USD"
        if loss_rows:
            header += f"（其中 {len(loss_rows)} 个渠道整月合计为负，本月不付佣金）"
        lines.append(header)

        for row in period_rows:
            lines.append(
                f"    {row.referral_no} {row.referral_name or '(未命名)':<20} "
                f"收入 {row.revenue_total:>12,.2f} × {row.rate_percent}% "
                f"= {row.payable:>10,.2f}   "
                f"({row.client_count} 客户 / {row.txn_count} 笔)"
            )
            # 负值月单独起一行说明，不是挤在上面那行末尾。
            # 只输出一个 0 的话，读的人分不清「这个月亏了」和「这个月没交易」——
            # 两种情况在报表上长得一样，但意思完全不同。
            if row.is_loss_month:
                lines.append(
                    f"         └─ 整月合计为负 {abs(row.revenue_total):,.2f} USD"
                    f"（按比例应为 {row.gross_payable:,.2f}），"
                    "按业务规则佣金保底 0：不倒扣，也不结转到下个月"
                )
    return "\n".join(lines)
