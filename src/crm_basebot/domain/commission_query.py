"""销售自查用的佣金明细：按渠道 × 客户展开某个月的应付佣金。

对账任务（jobs/reconcile.py）只写「按月 × 渠道」的粗粒度汇总到 Commission Summary
表，因为 Base 里不需要按客户分行留存。但销售自查时想看到「我的 R001 里，客户 A
贡献了多少佣金、客户 B 贡献了多少」，这个粒度必须在读取路径上现算。

设计约束：

1. **权限**：普通销售只看归属自己的渠道；管理员看全部。沿用现有 owned_records
   的口径，不新写一份鉴权。
2. **只读**：这个模块不写任何东西，也不触碰 Commission Summary 表 —— 那是对账
   任务的写入面，跟自查是两个用途。
3. **性能**：卡片回调只有 3 秒，扫全表 + 聚合可能超时。调用方（handlers.py）负责
   走异步模式：立即 ack「处理中」，实际计算和结果消息在后台线程里做。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from ..bot.auth import Sales
from ..lark.bitable import BitableClient
from ..lark.values import extract_text, to_number, to_uid
from . import schema
from .commission import CENTS, Referral, _link_ids, period_of

logger = logging.getLogger(__name__)


@dataclass
class ClientBreakdown:
    """某个渠道下某个客户在一个月里的贡献。"""

    uid: str
    name: str
    revenue: Decimal = Decimal("0")
    row_count: int = 0

    @property
    def gross_payable(self) -> Decimal:
        """先按客户级算个原始金额。渠道级保底 max(0, ...) 在 ReferralBreakdown 那层。

        单客户的原始金额有可能是负（退款/冲销集中在这一个客户），但那不影响客户
        应不应该被展示 —— 它只影响渠道合计后要不要被保底。
        """
        # 客户级 gross 只是拿来展示的，具体的 rate 从渠道那边带下来
        return self.revenue


@dataclass
class ReferralBreakdown:
    """某个渠道在一个月里的佣金明细（含各客户的贡献）。"""

    referral_no: str
    referral_name: str
    rate_percent: Decimal
    clients: dict[str, ClientBreakdown] = field(default_factory=dict)

    @property
    def revenue_total(self) -> Decimal:
        return sum((c.revenue for c in self.clients.values()), Decimal("0"))

    @property
    def gross_payable(self) -> Decimal:
        """按比例算出来的原始金额，可能是负（见 CommissionRow.gross_payable）。"""
        return (self.revenue_total * self.rate_percent / Decimal(100)).quantize(
            CENTS, rounding=ROUND_HALF_UP
        )

    @property
    def payable(self) -> Decimal:
        """整月保底 0 —— 和 CommissionRow.payable 保持同一条业务规则。"""
        return max(Decimal("0"), self.gross_payable)

    @property
    def is_loss_month(self) -> bool:
        return self.revenue_total < Decimal("0")

    def client_share(self, client: ClientBreakdown) -> Decimal:
        """把渠道级的应付按各客户的收入占比分下去，方便展示。

        为什么按占比分而不是「客户收入 × 分佣比例」：因为渠道级要过一次 max(0,...)
        保底，如果直接按客户算再相加，负客户会被单独归零、正客户不受影响 —— 加起来
        就会大于渠道应付。占比法保证 sum(客户份额) == 渠道应付。

        整月合计为负、或者 revenue_total 恰好为 0 时，客户份额都归 0；对应的
        payable 本来也是 0，占比无从算起。
        """
        if self.revenue_total <= 0 or self.payable == 0:
            return Decimal("0")
        share = client.revenue / self.revenue_total
        return (self.payable * share).quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass
class QueryResult:
    period: str
    referrals: list[ReferralBreakdown]
    unmapped_uids: list[str]
    """在这次查询范围里出现、但没登记归属的 UID。管理员看全表，销售只看空 —— 因为
    未登记归属的客户根本不知道该算谁的，普通销售不该看到别的销售的孤儿。"""

    @property
    def total_payable(self) -> Decimal:
        return sum((r.payable for r in self.referrals), Decimal("0"))


class CommissionQueryService:
    """按 (销售, 月份) 查佣金明细。

    不缓存维表 —— 每次查询都重新读渠道表和客户表。渠道数量小（几十到几百），成本
    可控；缓存反而会导致「销售刚新登记的渠道查不到」这类隔层问题，得不偿失。
    """

    def __init__(self, bitable: BitableClient, *, settings) -> None:
        self._bitable = bitable
        self._settings = settings
        # 归月用的业务时区，和 CommissionCalculator 读同一份配置
        self._tz = ZoneInfo(settings.business_timezone)

    def query(self, sales: Sales, period: str) -> QueryResult:
        """算出这名销售在 ``period``（YYYY-MM）能看到的佣金明细。"""
        referrals_by_record = self._load_referrals()

        # 管理员看全部；销售只看归属自己的渠道 record_id。
        owned_record_ids = self._owned_referral_ids(sales, referrals_by_record)
        allowed_referrals: dict[str, Referral] = {
            rid: ref for rid, ref in referrals_by_record.items() if rid in owned_record_ids
        }

        # UID -> Referral，只包含 allowed 里的渠道所对应的客户
        client_map, client_names = self._load_allowed_clients(allowed_referrals)

        breakdowns: dict[str, ReferralBreakdown] = {}
        unmapped: set[str] = set()

        for record in self._bitable.iter_records(self._settings.table_daily_board):
            uid = to_uid(record.fields.get(schema.BOARD_CLIENT_UID))
            if not uid:
                continue

            row_period = period_of(record.fields.get(schema.BOARD_ORDER_DATE), tz=self._tz)
            if row_period != period:
                continue

            referral = client_map.get(uid)
            if referral is None:
                # 只有管理员看得到未登记归属的孤儿 —— 普通销售看到别人渠道的孤儿也没用
                if sales.is_admin:
                    unmapped.add(uid)
                continue

            revenue = to_number(record.fields.get(schema.BOARD_TOTAL_REVENUE))
            if revenue is None:
                continue

            breakdown = breakdowns.get(referral.no)
            if breakdown is None:
                breakdown = ReferralBreakdown(
                    referral_no=referral.no,
                    referral_name=referral.name,
                    rate_percent=referral.rate_percent,
                )
                breakdowns[referral.no] = breakdown

            client_entry = breakdown.clients.get(uid)
            if client_entry is None:
                client_entry = ClientBreakdown(
                    uid=uid,
                    name=client_names.get(uid, ""),
                )
                breakdown.clients[uid] = client_entry

            client_entry.revenue += Decimal(str(revenue))
            client_entry.row_count += 1

        ordered = sorted(breakdowns.values(), key=lambda b: b.referral_no)
        return QueryResult(
            period=period,
            referrals=ordered,
            unmapped_uids=sorted(unmapped),
        )

    def latest_period(self) -> str:
        """看板里最新有数据的月份。空表返回空串。

        用来在没显式传月份时给一个合理的默认值，同 reconcile 的策略。
        """
        latest = ""
        for record in self._bitable.iter_records(
            self._settings.table_daily_board,
            field_names=[schema.BOARD_ORDER_DATE],
        ):
            row_period = period_of(record.fields.get(schema.BOARD_ORDER_DATE), tz=self._tz)
            if row_period and row_period > latest:
                latest = row_period
        return latest

    # ---------- 内部辅助 ----------

    def _load_referrals(self) -> dict[str, Referral]:
        result: dict[str, Referral] = {}
        for record in self._bitable.iter_records(self._settings.table_referral):
            no = extract_text(record.fields.get(schema.REFERRAL_NO))
            if not no:
                continue
            rate = to_number(record.fields.get(schema.REFERRAL_RATE)) or 0.0
            result[record.record_id] = Referral(
                record_id=record.record_id,
                no=no,
                name=extract_text(record.fields.get(schema.REFERRAL_NAME)),
                rate_percent=Decimal(str(rate)),
                status=extract_text(record.fields.get(schema.REFERRAL_STATUS)),
            )
        return result

    def _owned_referral_ids(
        self, sales: Sales, referrals_by_record: dict[str, Referral]
    ) -> set[str]:
        """哪些渠道 record_id 是这名销售能看的。"""
        if sales.is_admin:
            return set(referrals_by_record)

        owned: set[str] = set()
        for record in self._bitable.iter_records(self._settings.table_referral):
            owner = extract_text(record.fields.get(schema.REFERRAL_OWNER_OPEN_ID))
            if owner == sales.open_id and record.record_id in referrals_by_record:
                owned.add(record.record_id)
        return owned

    def _load_allowed_clients(
        self, allowed_referrals: dict[str, Referral]
    ) -> tuple[dict[str, Referral], dict[str, str]]:
        """UID -> Referral（只保留归属在 allowed 里的），以及 UID -> 客户名称。"""
        by_uid: dict[str, Referral] = {}
        names: dict[str, str] = {}
        for record in self._bitable.iter_records(self._settings.table_client):
            uid = to_uid(record.fields.get(schema.CLIENT_UID))
            if not uid:
                continue
            names[uid] = extract_text(record.fields.get(schema.CLIENT_NAME))

            linked = record.fields.get(schema.CLIENT_REFERRAL_LINK) or []
            for referral_record_id in _link_ids(linked):
                referral = allowed_referrals.get(referral_record_id)
                if referral is not None:
                    by_uid[uid] = referral
                    break
        return by_uid, names


def summarize(result: QueryResult, *, viewer_name: str) -> str:
    """把查询结果拼成一段人看的 markdown（供卡片展示）。"""
    if not result.referrals:
        return (
            f"**{result.period}**  {viewer_name} 名下没有可展示的佣金明细。\n\n"
            "可能原因：这个月看板里没有归属你名下客户的记录；或者你的客户还没登记归属。"
        )

    lines: list[str] = [
        f"**{result.period}**  合计应付 **{result.total_payable:,.2f}** USD",
        "",
    ]

    for ref in result.referrals:
        lines.append(
            f"**{ref.referral_no}** {ref.referral_name or '(未命名)'}  "
            f"—— 应付 {ref.payable:,.2f} USD"
        )
        if ref.is_loss_month:
            lines.append(
                f"  ⚠︎ 整月合计为负 {abs(ref.revenue_total):,.2f} USD，本月按业务规则保底 0"
            )

        # 客户按贡献从大到小；负贡献放最后，一眼看得出是谁把这个渠道拉负了
        clients_sorted = sorted(ref.clients.values(), key=lambda c: c.revenue, reverse=True)
        for client in clients_sorted:
            share = ref.client_share(client)
            name = client.name or "(未命名客户)"
            lines.append(
                f"  · {name} `{client.uid}`  "
                f"收入 {client.revenue:,.2f} × {ref.rate_percent}% ≈ 佣金 {share:,.2f}"
            )
        lines.append("")

    if result.unmapped_uids:
        lines.append(
            f"另有 **{len(result.unmapped_uids)}** 个 UID 在看板里但未登记归属，"
            "未计入任何渠道（示例）："
        )
        for uid in result.unmapped_uids[:5]:
            lines.append(f"  · `{uid}`")

    return "\n".join(lines)
