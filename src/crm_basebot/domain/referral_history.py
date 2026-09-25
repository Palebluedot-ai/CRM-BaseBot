"""一条渠道近三个月、**每个客户**贡献了多少 —— 交易佣金和 ECAS 返佣各一列。

给「我的渠道」的详情卡用。第一版只有每月一个总数（读两张汇总表）；2026-09-24 的反馈
是不够：要看到每个客户这个月贡献了多少。

**现算，不读汇总表。** 汇总表是按「月 × 渠道」结的，没有客户这一层；本月也还没结算，
汇总表里没有它。现算用的是和「佣金查询」**同一套算法**（``commission_query.accumulate``
加 ``ReferralBreakdown``：渠道级算应付、按收入占比分到客户），所以两张卡上同一个渠道
同一个月的数一定对得上。

**只读这条渠道的客户的交易行。** 日读看板上万行，全表扫一遍是十几个分页往返；按客户UID
在服务端筛选，一两页就回来了（``BitableClient.iter_records_where_in``）。筛选接口出错时
退回全表扫描 —— 慢，但数是对的，并在日志里留一行。

两套账分两列，不合并成一个数（理由见 ``domain/ecas.py`` 开头）。同一个客户两边都有钱时
排在同一行：先按 UID 对，ECAS 那边没填 UID 就按规整后的名字对（``names.norm``）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..lark.bitable import BitableClient, Record
from ..lark.values import extract_text, to_uid
from . import ecas, schema
from .ai_status import AiEligibility, eligibility_of
from .commission import _link_ids
from .commission_query import BOARD_FIELDS, ReferralBreakdown, accumulate, referral_from_record
from .dates import months_ending, period_of_day
from .ecas_query import load_applications
from .names import norm

logger = logging.getLogger(__name__)

DEFAULT_MONTHS = 3


@dataclass(frozen=True)
class ChannelClient:
    """渠道名下的一个客户。UID 被 Excel 弄坏、客户表里是空的时候 ``uid`` 为空。"""

    uid: str
    name: str
    ai: AiEligibility = AiEligibility()


@dataclass(frozen=True)
class ClientMonth:
    """一个客户一个月的两笔钱。``None`` 表示那套账这个月没有他的记录。"""

    name: str
    trade: Decimal | None = None
    ecas: Decimal | None = None

    @property
    def total(self) -> Decimal:
        return (self.trade or Decimal("0")) + (self.ecas or Decimal("0"))


@dataclass(frozen=True)
class ChannelMonth:
    """一条渠道一个月：两套账各自的应付，和每个有记录的客户。

    ``trade`` / ``ecas`` 是渠道当月应付（已按规则保底 0）；``None`` 表示那套账这个月
    没有记录 —— 和「有记录、应付 0」分得开。``trade_loss`` 是交易整月合计为负、按规则
    记 0 的月份，卡片上要说一句，不然 0 看起来像漏算。``current`` 是本月（还没过完）。
    """

    period: str
    trade: Decimal | None = None
    ecas: Decimal | None = None
    clients: tuple[ClientMonth, ...] = ()
    trade_loss: bool = False
    current: bool = False

    @property
    def is_empty(self) -> bool:
        return self.trade is None and self.ecas is None


class ReferralHistoryService:
    """按渠道 record_id 算近几个月每个客户的两笔钱。只读。

    ``referral_record_id`` 必须是调用方**已经鉴过权**的那条（handlers 里先过
    ``ReferralService.get_for``）。这里只按关联取数，不判归属。

    不缓存，理由和 ``CommissionQueryService`` 一样：刚登记的客户查不到才是真麻烦。
    """

    def __init__(self, bitable: BitableClient, *, settings) -> None:
        self._bitable = bitable
        self._settings = settings
        self._tz = ZoneInfo(settings.business_timezone)

    def recent(
        self, referral_record_id: str, *, today: date, months: int = DEFAULT_MONTHS
    ) -> list[ChannelMonth]:
        """含本月在内往回 ``months`` 个月，从早到晚。本月那一格标 ``current``。"""
        if not referral_record_id:
            return []
        periods = months_ending(period_of_day(today), months)
        referral = referral_from_record(
            self._bitable.get_record(self._settings.table_referral, referral_record_id)
        )
        if referral is None:
            return []

        clients = self._clients(referral_record_id)
        trade = self._trade(referral, clients, periods)
        book = self._ecas(referral_record_id, referral, periods)

        current = periods[-1] if periods else ""
        return [
            _month(period, trade.get(period), book.get(period, []), clients, current=current)
            for period in periods
        ]

    # ---------- 读数 ----------

    def _clients(self, referral_record_id: str) -> list[ChannelClient]:
        found: dict[str, ChannelClient] = {}
        # 不限定列：AI 那两列是 2026-09-25 才加的，还没跑 sync 的 Base 里没有它们，
        # 点名要一个不存在的列整个请求会被拒。客户表只有几百行，全列读回来也不贵。
        for record in self._bitable.iter_records(self._settings.table_client):
            linked = _link_ids(record.fields.get(schema.CLIENT_REFERRAL_LINK) or [])
            if referral_record_id not in linked:
                continue
            uid = to_uid(record.fields.get(schema.CLIENT_UID))
            name = extract_text(record.fields.get(schema.CLIENT_NAME))
            # 没有 UID 的客户按 record_id 占位：对不上交易，但 ECAS 那边还能按名字对。
            found[uid or f"?{record.record_id}"] = ChannelClient(
                uid=uid, name=name, ai=eligibility_of(record.fields, tz=self._tz)
            )
        return list(found.values())

    def _trade(
        self, referral, clients: list[ChannelClient], periods: list[str]
    ) -> dict[str, ReferralBreakdown]:
        board = self._settings.table_daily_board
        uids = sorted({c.uid for c in clients if c.uid})
        if not board or not uids:
            return {}
        client_map = {uid: referral for uid in uids}
        names = {c.uid: c.name for c in clients if c.uid}
        months = accumulate(
            self._board_rows(board, uids),
            client_map,
            names,
            periods,
            tz=self._tz,
            eligibility={c.uid: c.ai for c in clients if c.uid},
        )
        return {period: by_no[referral.no] for period, by_no in months.items()}

    def _board_rows(self, board: str, uids: list[str]) -> list[Record]:
        try:
            return list(
                self._bitable.iter_records_where_in(
                    board, schema.BOARD_CLIENT_UID, uids, field_names=BOARD_FIELDS
                )
            )
        except Exception:  # noqa: BLE001 - 退回全表扫描，见模块开头
            logger.warning("按客户UID 筛选日读看板失败，退回全表扫描", exc_info=True)
            return list(self._bitable.iter_records(board, field_names=BOARD_FIELDS))

    def _ecas(
        self, referral_record_id: str, referral, periods: list[str]
    ) -> dict[str, list[ecas.EcasApplication]]:
        table = getattr(self._settings, "table_ecas", "")
        if not table:
            return {}
        payee = ecas.Payee(code=referral.no, name=referral.name)
        wanted = set(periods)
        out: dict[str, list[ecas.EcasApplication]] = {}
        for app in load_applications(
            self._bitable,
            table,
            {referral_record_id: payee},
            tz=self._tz,
            allowed_referral_ids={referral_record_id},
        ):
            if app.period in wanted and app.rate_percent is not None:
                out.setdefault(app.period, []).append(app)
        return out


def _month(
    period: str,
    trade: ReferralBreakdown | None,
    applications: list[ecas.EcasApplication],
    clients: list[ChannelClient],
    *,
    current: str,
) -> ChannelMonth:
    """把一个月的交易明细和 ECAS 申请并成按客户的一行行。"""
    # 客户的键：有 UID 用 UID，没有就用规整后的名字。显示名优先用客户表里登记的那个。
    known: dict[str, str] = {}
    key_by_name: dict[str, str] = {}
    for client in clients:
        key = client.uid or f"?{norm(client.name)}"
        known[key] = client.name
        if client.name:
            key_by_name.setdefault(norm(client.name), key)

    trades: dict[str, Decimal] = {}
    books: dict[str, Decimal] = {}
    fallback_names: dict[str, str] = {}

    if trade is not None:
        trades.update(trade.client_shares())
        for uid, client in trade.clients.items():
            fallback_names[uid] = client.name

    for app in applications:
        if app.client_uid and app.client_uid in known:
            key = app.client_uid
        else:
            key = key_by_name.get(norm(app.client_name)) or f"?{norm(app.client_name)}"
        books[key] = books.get(key, Decimal("0")) + app.fee
        fallback_names.setdefault(key, app.client_name)

    rows = [
        ClientMonth(
            name=known.get(key) or fallback_names.get(key, ""),
            trade=trades.get(key),
            ecas=books.get(key),
        )
        for key in {**trades, **books}
    ]
    rows.sort(key=lambda row: (-row.total, row.name))

    ecas_total = sum((app.fee for app in applications), Decimal("0")) if applications else None
    return ChannelMonth(
        period=period,
        trade=trade.payable if trade is not None else None,
        # 规则和交易佣金一致：整月合计为负按 0，不倒扣（见 EcasCommissionRow.payable）。
        ecas=max(Decimal("0"), ecas_total) if ecas_total is not None else None,
        clients=tuple(rows),
        trade_loss=trade.is_loss_month if trade is not None else False,
        current=period == current,
    )
