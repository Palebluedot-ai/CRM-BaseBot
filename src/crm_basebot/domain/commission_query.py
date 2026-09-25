"""销售自查用的佣金明细：按渠道 × 客户展开**连续几个月**的应付佣金。

对账任务（jobs/reconcile.py）只写「按月 × 渠道」的粗粒度汇总到 Commission Summary
表，因为 Base 里不需要按客户分行留存。但销售自查时想看到「我的 R001 里，客户 A
贡献了多少佣金、客户 B 贡献了多少」，这个粒度必须在读取路径上现算。

一次查几个月（2026-09-24 反馈：要看到近三个月），看板只扫一遍，只读算钱要的三列。

设计约束：

1. **权限**：普通销售只看归属自己的渠道；管理员看全部。沿用现有 owned_records
   的口径，不新写一份鉴权。
2. **只读**：这个模块不写任何东西，也不触碰 Commission Summary 表 —— 那是对账
   任务的写入面，跟自查是两个用途。
3. **性能**：扫全表 + 聚合要好几秒，压不进卡片回调的 3 秒。调用方（handlers.py）
   在后台线程里跑，算完把结果作为新消息推出去。
4. **同一段算法**：「我的渠道」详情卡上的每客户数字也走 ``accumulate`` 和
   ``ReferralBreakdown``（见 ``referral_history.py``）。两张卡上同一个渠道同一个月的
   数出自同一段代码，才一定对得上。
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from ..bot.auth import Sales
from ..lark.bitable import BitableClient, Record
from ..lark.values import extract_text, to_number, to_uid
from . import schema
from .ai_status import AiEligibility, eligibility_of
from .commission import CENTS, Referral, _link_ids, period_of

logger = logging.getLogger(__name__)

# 算钱只要这三列。看板有十几列，全字段读回来大半是白搬。
BOARD_FIELDS = [schema.BOARD_CLIENT_UID, schema.BOARD_ORDER_DATE, schema.BOARD_TOTAL_REVENUE]


@dataclass
class ClientBreakdown:
    """某个渠道下某个客户在一个月里的贡献。

    单客户的收入有可能是负（退款/冲销集中在这一个客户），但那不影响客户应不应该被
    展示 —— 它只影响渠道合计后要不要被保底，保底在 ``ReferralBreakdown`` 那层。
    """

    uid: str
    name: str
    revenue: Decimal = Decimal("0")
    row_count: int = 0


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
    def client_count(self) -> int:
        return len(self.clients)

    @property
    def row_count(self) -> int:
        """这个渠道这个月一共多少笔交易记录。

        和客户数是两回事：一个客户一个月可能有几十笔。对账时「笔数对不上」
        往往比「金额对不上」先被发现，所以两个都摆出来。
        """
        return sum(c.row_count for c in self.clients.values())

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

    def client_shares(self) -> dict[str, Decimal]:
        """把渠道级的应付按各客户的收入占比分下去，**加起来一分不差**等于渠道应付。

        为什么按占比分而不是「客户收入 × 分佣比例」：因为渠道级要过一次 max(0,...)
        保底，如果直接按客户算再相加，负客户会被单独归零、正客户不受影响 —— 加起来
        就会大于渠道应付。

        为什么不是逐个四舍五入：客户一多，各自进位之后的合计会和渠道应付差一两分，
        而卡片上写着「加起来就是渠道应付」。所以用最大余数法：先都往下取到分，差的
        那几分给被截掉最多的几个客户。

        整月合计为负、或者 revenue_total 恰好为 0 时，客户份额都归 0；对应的
        payable 本来也是 0，占比无从算起。
        """
        if self.revenue_total <= 0 or self.payable == 0:
            return {uid: Decimal("0") for uid in self.clients}

        exact = {
            uid: self.payable * client.revenue / self.revenue_total
            for uid, client in self.clients.items()
        }
        shares = {uid: value.quantize(CENTS, rounding=ROUND_FLOOR) for uid, value in exact.items()}
        missing = int(
            ((self.payable - sum(shares.values(), Decimal("0"))) / CENTS).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
        by_remainder = sorted(exact, key=lambda uid: (shares[uid] - exact[uid], uid))
        for uid in by_remainder[:missing]:
            shares[uid] += CENTS
        return shares

    def client_share(self, client: ClientBreakdown) -> Decimal:
        """一个客户分到的那份，见 ``client_shares``。"""
        return self.client_shares()[client.uid]


def referral_from_record(record: Record) -> Referral | None:
    """渠道表的一行 -> 算钱用的 ``Referral``。没有编号的行返回 None（算不了，也认不出）。"""
    no = extract_text(record.fields.get(schema.REFERRAL_NO))
    if not no:
        return None
    rate = to_number(record.fields.get(schema.REFERRAL_RATE)) or 0.0
    return Referral(
        record_id=record.record_id,
        no=no,
        name=extract_text(record.fields.get(schema.REFERRAL_NAME)),
        rate_percent=Decimal(str(rate)),
        status=extract_text(record.fields.get(schema.REFERRAL_STATUS)),
    )


def accumulate(
    records: Iterable[Record],
    client_map: dict[str, Referral],
    client_names: dict[str, str],
    periods: Collection[str],
    *,
    tz,
    eligibility: dict[str, AiEligibility] | None = None,
) -> dict[str, dict[str, ReferralBreakdown]]:
    """看板行 -> ``{月份: {渠道编号: ReferralBreakdown}}``。

    只收 ``periods`` 里的月份、``client_map`` 里的客户（UID -> 所属渠道）；其余的行
    跳过。佣金查询和渠道详情卡共用这一个函数，见模块开头第 4 条。

    ``eligibility`` 是客户UID -> AI 资格：那个月还不是 AI 的客户，交易不算（见
    domain/ai_status.py，和月结同一条规则）。不在里面的客户照旧算。
    """
    eligibility = eligibility or {}
    wanted = set(periods)
    out: dict[str, dict[str, ReferralBreakdown]] = {}
    for record in records:
        uid = to_uid(record.fields.get(schema.BOARD_CLIENT_UID))
        if not uid:
            continue
        referral = client_map.get(uid)
        if referral is None:
            continue
        period = period_of(record.fields.get(schema.BOARD_ORDER_DATE), tz=tz)
        if period not in wanted:
            continue
        if not eligibility.get(uid, AiEligibility()).counts(period):
            continue
        revenue = to_number(record.fields.get(schema.BOARD_TOTAL_REVENUE))
        if revenue is None:
            continue

        month = out.setdefault(period, {})
        breakdown = month.get(referral.no)
        if breakdown is None:
            breakdown = ReferralBreakdown(
                referral_no=referral.no,
                referral_name=referral.name,
                rate_percent=referral.rate_percent,
            )
            month[referral.no] = breakdown
        entry = breakdown.clients.get(uid)
        if entry is None:
            entry = ClientBreakdown(uid=uid, name=client_names.get(uid, ""))
            breakdown.clients[uid] = entry
        entry.revenue += Decimal(str(revenue))
        entry.row_count += 1
    return out


@dataclass(frozen=True)
class ClientLine:
    """结果卡上的一个客户：各月分到的佣金。那个月没有交易就不在 ``shares`` 里。"""

    uid: str
    name: str
    shares: dict[str, Decimal]

    @property
    def total(self) -> Decimal:
        return sum(self.shares.values(), Decimal("0"))


@dataclass(frozen=True)
class ChannelLine:
    """结果卡上的一个渠道：各月应付（保底后），和它名下每个客户各月分到的。

    ``payable`` 里没有的月份就是那个月没有交易，和「有交易、应付 0」分得开。
    ``loss_periods`` 是整月合计为负、按规则保底成 0 的月份，卡片上要单独说一句。
    """

    referral_no: str
    referral_name: str
    payable: dict[str, Decimal]
    loss_periods: tuple[str, ...]
    clients: tuple[ClientLine, ...]


@dataclass
class QueryResult:
    periods: list[str]
    """从早到晚。"""

    months: dict[str, list[ReferralBreakdown]] = field(default_factory=dict)
    """月份 -> 那个月有交易的渠道，按编号排序。没有交易的月份可以不在里面。"""

    def referrals_in(self, period: str) -> list[ReferralBreakdown]:
        return self.months.get(period, [])

    @property
    def is_empty(self) -> bool:
        return not any(self.referrals_in(period) for period in self.periods)

    def total_payable(self, period: str) -> Decimal:
        return sum((r.payable for r in self.referrals_in(period)), Decimal("0"))

    def referral_count(self, period: str) -> int:
        return len(self.referrals_in(period))

    def client_count(self, period: str) -> int:
        """去重后的客户数。

        同一个 UID 理论上只挂一个渠道，但客户表被人手改过之后不保证 ——
        按 UID 去重，免得「12 个客户」其实是同一个人数了两遍。
        """
        return len({uid for ref in self.referrals_in(period) for uid in ref.clients})

    def revenue_total(self, period: str) -> Decimal:
        """收入合计。**不做 max(0, ...) 保底** —— 保底是应付金额的规则，
        收入该是多少就是多少，截成 0 会让人看不出这个月是负的。"""
        return sum((r.revenue_total for r in self.referrals_in(period)), Decimal("0"))

    def channels(self) -> list[ChannelLine]:
        """按渠道展开成卡片要的形状：渠道按编号，渠道下的客户按几个月合计从大到小。

        客户份额用 ``ReferralBreakdown.client_shares``（按收入占比分渠道应付），所以
        同一个月里一个渠道下各客户的数加起来一分不差就是这个渠道的应付。
        """
        by_no: dict[str, dict[str, ReferralBreakdown]] = {}
        for period in self.periods:
            for ref in self.referrals_in(period):
                by_no.setdefault(ref.referral_no, {})[period] = ref

        lines: list[ChannelLine] = []
        for no in sorted(by_no):
            months = by_no[no]
            latest = months[max(months)]
            shares: dict[str, dict[str, Decimal]] = {}
            names: dict[str, str] = {}
            for period, ref in months.items():
                month_shares = ref.client_shares()
                for uid, client in ref.clients.items():
                    shares.setdefault(uid, {})[period] = month_shares[uid]
                    names[uid] = client.name or names.get(uid, "")
            clients = sorted(
                (ClientLine(uid=uid, name=names[uid], shares=shares[uid]) for uid in shares),
                key=lambda c: (-c.total, c.name, c.uid),
            )
            lines.append(
                ChannelLine(
                    referral_no=no,
                    referral_name=latest.referral_name,
                    payable={period: ref.payable for period, ref in months.items()},
                    loss_periods=tuple(
                        period
                        for period in self.periods
                        if period in months and months[period].is_loss_month
                    ),
                    clients=tuple(clients),
                )
            )
        return lines


class CommissionQueryService:
    """按 (销售, 连续几个月) 查佣金明细。

    不缓存维表 —— 每次查询都重新读渠道表和客户表。渠道数量小（几十到几百），成本
    可控；缓存反而会导致「销售刚新登记的渠道查不到」这类隔层问题，得不偿失。
    """

    def __init__(self, bitable: BitableClient, *, settings) -> None:
        self._bitable = bitable
        self._settings = settings
        # 归月用的业务时区，和 CommissionCalculator 读同一份配置
        self._tz = ZoneInfo(settings.business_timezone)

    def query(self, sales: Sales, periods: Sequence[str]) -> QueryResult:
        """这名销售在 ``periods``（YYYY-MM，从早到晚）里能看到的佣金明细。看板只扫一遍。"""
        referrals_by_record = self._load_referrals()

        # 管理员看全部；销售只看归属自己的渠道 record_id。
        owned_record_ids = self._owned_referral_ids(sales, referrals_by_record)
        allowed_referrals: dict[str, Referral] = {
            rid: ref for rid, ref in referrals_by_record.items() if rid in owned_record_ids
        }

        # UID -> Referral，只包含 allowed 里的渠道所对应的客户
        client_map, client_names, eligibility = self._load_allowed_clients(allowed_referrals)

        months: dict[str, dict[str, ReferralBreakdown]] = {}
        # 名下一个客户都没有就不去扫那张上万行的看板了：扫完也是空的。
        if client_map:
            months = accumulate(
                self._bitable.iter_records(
                    self._settings.table_daily_board, field_names=BOARD_FIELDS
                ),
                client_map,
                client_names,
                periods,
                tz=self._tz,
                eligibility=eligibility,
            )
        return QueryResult(
            periods=list(periods),
            months={
                period: sorted(months.get(period, {}).values(), key=lambda b: b.referral_no)
                for period in periods
            },
        )

    # ---------- 内部辅助 ----------

    def _load_referrals(self) -> dict[str, Referral]:
        result: dict[str, Referral] = {}
        for record in self._bitable.iter_records(self._settings.table_referral):
            referral = referral_from_record(record)
            if referral is not None:
                result[record.record_id] = referral
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
    ) -> tuple[dict[str, Referral], dict[str, str], dict[str, AiEligibility]]:
        """UID -> Referral（只保留归属在 allowed 里的）、UID -> 客户名称、UID -> AI 资格。"""
        by_uid: dict[str, Referral] = {}
        names: dict[str, str] = {}
        eligibility: dict[str, AiEligibility] = {}
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
                    eligibility[uid] = eligibility_of(record.fields, tz=self._tz)
                    break
        return by_uid, names, eligibility
