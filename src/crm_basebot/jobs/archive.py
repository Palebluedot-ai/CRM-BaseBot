"""月结存档：结算完的那个月，拍一份不会再变的明细和汇总放进 Base。

每月 1 号 16:30 结完账，建两张新表，再往一张总表里追加：

  · ``2026-09 结算明细``  —— 每个渠道下每个客户一行（交易和 ECAS 都在），金额按
    invoice 的同一套分法，**加起来一分不差**等于结算表
  · ``2026-09 结算汇总``  —— 每个渠道一行，就是两张结算表那个月的副本
  · ``结算明细（全部月份）`` —— 所有月份的明细都追加进来，仪表盘用它做「渠道 → 客户」
    全部展开、月份在列的明细，永远和财务一致

## 为什么要存档（2026-09-30）

看板上的明细是每次打开时按当下的资料现算的。结算之后补登记客户、改渠道比例，旧月份的
数就变了，和财务已经付出去的对不上（2026-08 差了 16,594.82，见 scripts/compare_settled.py）。
存档是结算那一刻的样子，之后谁改什么都不动它。

## 客户明细怎么来的

渠道的金额**只认结算表**。客户那一层是现算的：交易按客户收入占比、ECAS 按每笔申请的
返佣，和 invoice 一样 —— 现算加起来正好等于结算数就照用，不等就按占比从结算数分下来
（最大余数法，``documents.invoice.allocate``）。一个客户都找不到的渠道写一行
「（找不到客户明细）」，金额就是结算数。

每月结算当场存档时，资料和结算是同一刻的，客户明细就是准的。事后补存的旧月份
（``scripts/archive_month.py``），渠道金额仍然准，客户分法按现在的资料，是近似。

## 左边栏的「Archive」分组

飞书没有开放接口把表放进分组，新表会出现在表格列表的最下面，要人拖一下。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppTableCreateHeader,
    CreateAppTableRequest,
    CreateAppTableRequestBody,
    ReqTable,
)

from ..bot.auth import Sales
from ..documents.invoice import Part, allocate, rate_text
from ..domain import ecas, schema
from ..domain.ecas_query import load_applications, load_payees
from ..domain.settlement import is_live
from ..lark.bitable import FIELD_TYPE_NUMBER, FIELD_TYPE_TEXT, BitableClient
from ..lark.values import extract_text, to_number
from ..structure import FORMAT_COUNT, FORMAT_MONEY, StructureError, build_field

logger = logging.getLogger(__name__)

CENTS = Decimal("0.01")
ZERO = Decimal("0")

KIND_TRADE = "交易佣金"
KIND_ECAS = "ECAS 返佣"
NO_CLIENT = "（找不到客户明细）"

CUMULATIVE_TABLE = "结算明细（全部月份）"

# 明细和汇总的列。第一列会成为表的主字段。(列名, 类型, 显示格式)
F_PERIOD = "结算月份"
F_KIND = "类型"
F_NO = "渠道编号"
F_NAME = "渠道名称"
F_CLIENT = "客户名称"
F_UID = "客户UID"
F_BASE = "收入 / ECAS金额"
F_RATE = "比例"
F_AMOUNT = "应付佣金"
F_CLIENTS = "客户数"

DETAIL_COLUMNS: tuple[tuple[str, int, str | None], ...] = (
    (F_PERIOD, FIELD_TYPE_TEXT, None),
    (F_KIND, FIELD_TYPE_TEXT, None),
    (F_NO, FIELD_TYPE_TEXT, None),
    (F_NAME, FIELD_TYPE_TEXT, None),
    (F_CLIENT, FIELD_TYPE_TEXT, None),
    (F_UID, FIELD_TYPE_TEXT, None),
    (F_BASE, FIELD_TYPE_NUMBER, FORMAT_MONEY),
    (F_RATE, FIELD_TYPE_TEXT, None),
    (F_AMOUNT, FIELD_TYPE_NUMBER, FORMAT_MONEY),
)

SUMMARY_COLUMNS: tuple[tuple[str, int, str | None], ...] = (
    (F_PERIOD, FIELD_TYPE_TEXT, None),
    (F_KIND, FIELD_TYPE_TEXT, None),
    (F_NO, FIELD_TYPE_TEXT, None),
    (F_NAME, FIELD_TYPE_TEXT, None),
    (F_CLIENTS, FIELD_TYPE_NUMBER, FORMAT_COUNT),
    (F_BASE, FIELD_TYPE_NUMBER, FORMAT_MONEY),
    (F_RATE, FIELD_TYPE_TEXT, None),
    (F_AMOUNT, FIELD_TYPE_NUMBER, FORMAT_MONEY),
)

# 读现算明细用的「看全部」身份：存档不分销售。
_EVERYONE = Sales(
    open_id="archive", name="月结存档", role=schema.ROLE_ADMIN, is_active=True, sees_all=True
)


def detail_table_name(period: str) -> str:
    return f"{period} 结算明细"


def summary_table_name(period: str) -> str:
    return f"{period} 结算汇总"


@dataclass(frozen=True)
class Settled:
    """结算表里的一行（一个渠道一个月）。"""

    kind: str
    referral_no: str
    name: str
    clients: int
    base: Decimal
    rate: str
    amount: Decimal


@dataclass(frozen=True)
class Contribution:
    """现算出来的一个客户（或一笔 ECAS 申请）：谁、按多少分、现算多少钱。"""

    client: str
    uid: str
    base: Decimal
    fresh: Decimal


@dataclass(frozen=True)
class DetailRow:
    kind: str
    referral_no: str
    name: str
    client: str
    uid: str
    base: Decimal
    rate: str
    amount: Decimal


@dataclass
class Snapshot:
    period: str
    summary: list[Settled]
    details: list[DetailRow]

    def total(self, kind: str) -> Decimal:
        return sum((s.amount for s in self.summary if s.kind == kind), ZERO)

    def mismatches(self) -> list[str]:
        """每个渠道的明细加起来不等于结算数的，逐个说出来。正常情况下永远是空的。"""
        problems: list[str] = []
        for settled in self.summary:
            if settled.amount <= 0:
                continue
            got = sum(
                (
                    d.amount
                    for d in self.details
                    if d.kind == settled.kind
                    and d.referral_no == settled.referral_no
                    and d.name == settled.name
                ),
                ZERO,
            )
            if got != settled.amount:
                problems.append(
                    f"{settled.kind} {settled.referral_no} {settled.name}："
                    f"结算 {settled.amount:,.2f}，明细合计 {got:,.2f}"
                )
        return problems


# ---------- 算（纯函数，方便测） ----------


def split(settled: Settled, parts: list[Contribution]) -> list[DetailRow]:
    """把一个渠道的结算数分给它的客户，**加起来一分不差**。规则和 invoice 的明细一样。"""

    def row(part: Contribution, amount: Decimal) -> DetailRow:
        return DetailRow(
            settled.kind,
            settled.referral_no,
            settled.name,
            part.client,
            part.uid,
            part.base,
            settled.rate,
            amount,
        )

    weighted = [p for p in parts if p.base > 0]
    if not weighted:
        placeholder = Contribution(NO_CLIENT, "", settled.base, settled.amount)
        return [row(placeholder, settled.amount)]
    if sum((p.fresh for p in weighted), ZERO) == settled.amount:
        return [row(p, p.fresh) for p in weighted]
    shares = allocate(settled.amount, [Part(p.client, p.base, p.fresh) for p in weighted])
    return [row(p, share.amount) for p, share in zip(weighted, shares, strict=True)]


def build_details(
    summary: list[Settled], contributions: dict[tuple[str, str], list[Contribution]]
) -> list[DetailRow]:
    """``contributions`` 按 (类型, 渠道编号) 分组。结算数是 0 的渠道不列明细。"""
    rows: list[DetailRow] = []
    for settled in summary:
        if settled.amount <= 0:
            continue
        key = (settled.kind, contribution_key(settled.referral_no, settled.name))
        rows += split(settled, contributions.get(key, []))
    return rows


def contribution_key(referral_no: str, name: str) -> str:
    """渠道编号；ECAS 里渠道表对不上名字的那几行没有编号，退回用名字（和 ecas.Payee.key 一样）。"""
    return referral_no or f"?{name}"


# ---------- 读 ----------


def _dec(value: Any) -> Decimal:
    number = to_number(value)
    return Decimal(str(number)) if number is not None else ZERO


def load_settled(bitable: BitableClient, settings: Any, period: str) -> list[Settled]:
    out: list[Settled] = []
    for record in bitable.iter_records(settings.table_commission):
        f = record.fields
        if extract_text(f.get(schema.COMM_PERIOD)).strip() != period or is_live(f):
            continue
        rate = to_number(f.get(schema.COMM_RATE))
        out.append(
            Settled(
                KIND_TRADE,
                extract_text(f.get(schema.COMM_REFERRAL_NO)).strip(),
                extract_text(f.get(schema.COMM_REFERRAL_NAME)).strip(),
                int(to_number(f.get(schema.COMM_CLIENT_COUNT)) or 0),
                _dec(f.get(schema.COMM_REVENUE_TOTAL)).quantize(CENTS),
                rate_text(Decimal(str(rate))) if rate is not None else "",
                _dec(f.get(schema.COMM_PAYABLE)).quantize(CENTS),
            )
        )
    table = getattr(settings, "table_ecas_commission", "")
    if table:
        for record in bitable.iter_records(table):
            f = record.fields
            if extract_text(f.get(ecas.ECOMM_PERIOD)).strip() != period or is_live(f):
                continue
            out.append(
                Settled(
                    KIND_ECAS,
                    extract_text(f.get(ecas.ECOMM_REFERRAL_NO)).strip(),
                    extract_text(f.get(ecas.ECOMM_REFERRAL_NAME)).strip(),
                    int(to_number(f.get(ecas.ECOMM_CLIENT_COUNT)) or 0),
                    _dec(f.get(ecas.ECOMM_AMOUNT_TOTAL)).quantize(CENTS),
                    extract_text(f.get(ecas.ECOMM_RATE_NOTE)).strip(),
                    _dec(f.get(ecas.ECOMM_PAYABLE)).quantize(CENTS),
                )
            )
    return out


def load_contributions(
    bitable: BitableClient, settings: Any, period: str, *, tz, commission_query
) -> dict[tuple[str, str], list[Contribution]]:
    out: dict[tuple[str, str], list[Contribution]] = {}
    if commission_query is not None:
        result = commission_query.query(_EVERYONE, [period])
        for referral in result.referrals_in(period):
            shares = referral.client_shares()
            out[(KIND_TRADE, referral.referral_no)] = [
                Contribution(client.name or uid, uid, client.revenue, shares[uid])
                for uid, client in sorted(
                    referral.clients.items(), key=lambda item: item[1].name or item[0]
                )
            ]
    table = getattr(settings, "table_ecas", "")
    if table:
        payees = load_payees(bitable, settings.table_referral)
        for app in load_applications(bitable, table, payees, tz=tz):
            if app.period != period or app.payee is None:
                continue
            key = contribution_key(app.payee.code, app.payee.name)
            out.setdefault((KIND_ECAS, key), []).append(
                Contribution(app.client_name, app.client_uid, app.amount, app.fee)
            )
    return out


def build_snapshot(
    bitable: BitableClient, settings: Any, period: str, *, tz, commission_query
) -> Snapshot:
    summary = load_settled(bitable, settings, period)
    contributions = load_contributions(
        bitable, settings, period, tz=tz, commission_query=commission_query
    )
    return Snapshot(period, summary, build_details(summary, contributions))


# ---------- 还没结算的月份（每天导入后刷新） ----------

LIVE_SUFFIX = "（未结算）"


def live_label(period: str) -> str:
    return f"{period}{LIVE_SUFFIX}"


def _percent(value: Decimal | None) -> str:
    return f"{value.normalize():f}%" if value is not None else ""


def build_live(
    bitable: BitableClient, settings: Any, period: str, *, tz, commission_query
) -> list[DetailRow]:
    """还没结算的月份按现在的资料现算：交易按渠道保底后分给客户，ECAS 一笔一行。

    和月结是同一套算法（CommissionQueryService / ECAS 申请表），结算那天会被正式存档替换。
    """
    rows: list[DetailRow] = []
    if commission_query is not None:
        result = commission_query.query(_EVERYONE, [period])
        for referral in result.referrals_in(period):
            shares = referral.client_shares()
            rate = rate_text(referral.rate_percent)
            for uid, client in sorted(
                referral.clients.items(), key=lambda item: item[1].name or item[0]
            ):
                if client.revenue == 0 and shares.get(uid, ZERO) == 0:
                    continue
                rows.append(
                    DetailRow(
                        KIND_TRADE,
                        referral.referral_no,
                        referral.referral_name,
                        client.name or uid,
                        uid,
                        client.revenue,
                        rate,
                        shares.get(uid, ZERO),
                    )
                )
    table = getattr(settings, "table_ecas", "")
    if table:
        payees = load_payees(bitable, settings.table_referral)
        for app in load_applications(bitable, table, payees, tz=tz):
            if app.period != period or app.payee is None:
                continue
            rows.append(
                DetailRow(
                    KIND_ECAS,
                    app.payee.code,
                    app.payee.name,
                    app.client_name,
                    app.client_uid,
                    app.amount,
                    _percent(app.rate_percent),
                    app.fee.quantize(CENTS),
                )
            )
    return rows


def _settled_periods(bitable: BitableClient, settings: Any) -> set[str]:
    """交易佣金结算表里有「已结算」行的月份。一个月结没结，以交易佣金那张为准。"""
    return settled_periods(bitable, settings.table_commission)


def settled_periods(bitable: BitableClient, table_id: str) -> set[str]:
    found: set[str] = set()
    for record in bitable.iter_records(table_id):
        if not is_live(record.fields):
            found.add(extract_text(record.fields.get(schema.COMM_PERIOD)).strip())
    return found


def _previous(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def _delete_live(bitable: BitableClient, table_id: str, only: str | None = None) -> int:
    """删掉总表里「未结算」的行。``only`` 给了就只删那个月的。"""
    ids = [
        record.record_id
        for record in bitable.iter_records(table_id, field_names=[F_PERIOD])
        if (label := extract_text(record.fields.get(F_PERIOD)).strip()).endswith(LIVE_SUFFIX)
        and (only is None or label == live_label(only))
    ]
    return bitable.batch_delete_records(table_id, ids) if ids else 0


# ---------- 写 ----------


def _money(value: Decimal) -> float:
    return float(value.quantize(CENTS))


def detail_fields(period: str, row: DetailRow) -> dict[str, Any]:
    return {
        F_PERIOD: period,
        F_KIND: row.kind,
        F_NO: row.referral_no,
        F_NAME: row.name,
        F_CLIENT: row.client,
        F_UID: row.uid,
        F_BASE: _money(row.base),
        F_RATE: row.rate,
        F_AMOUNT: _money(row.amount),
    }


def summary_fields(period: str, row: Settled) -> dict[str, Any]:
    return {
        F_PERIOD: period,
        F_KIND: row.kind,
        F_NO: row.referral_no,
        F_NAME: row.name,
        F_CLIENTS: row.clients,
        F_BASE: _money(row.base),
        F_RATE: row.rate,
        F_AMOUNT: _money(row.amount),
    }


def create_table(
    client: lark.Client, app_token: str, name: str, columns: tuple[tuple[str, int, str | None], ...]
) -> str:
    """建表时把列一起带上：第一列就是主字段，也不会多出平台塞的默认空列。"""
    headers = []
    for column, type_code, formatter in columns:
        spec = build_field(column, type_code, formatter=formatter)
        builder = AppTableCreateHeader.builder().field_name(column).type(type_code)
        if spec.property is not None:
            builder = builder.property(spec.property)
        headers.append(builder.build())
    request = (
        CreateAppTableRequest.builder()
        .app_token(app_token)
        .request_body(
            CreateAppTableRequestBody.builder()
            .table(ReqTable.builder().name(name).fields(headers).build())
            .build()
        )
        .build()
    )
    response = client.bitable.v1.app_table.create(request)
    if not response.success():
        raise StructureError(f"建表 {name} 失败: {response.code} {response.msg}")
    table_id = getattr(response.data, "table_id", None)
    if not table_id:
        raise StructureError(f"建表 {name} 返回成功但没给 table_id")
    return table_id


@dataclass
class ArchiveResult:
    period: str
    created: list[str]
    skipped: list[str]
    appended: int = 0

    @property
    def line(self) -> str:
        """月结卡片上的那一句。"""
        if not self.created and not self.appended:
            return f"存档：{self.period} 之前已经存过了（{'、'.join(self.skipped)}），这次没动。"
        names = "、".join(f"「{n}」" for n in self.created) or "（这个月的两张表之前就有了）"
        return (
            f"已存档：{names}，并追加进「{CUMULATIVE_TABLE}」。新表在表格列表最下面，拖进 Archive。"
        )


def write_archive(
    bitable: BitableClient, client: lark.Client, app_token: str, snapshot: Snapshot
) -> ArchiveResult:
    """建这个月的两张表、追加总表。**已经有的一律不动**：存档写一次就是那个月的样子。

    写之前自检：任何一个渠道的明细合计和结算数差一分钱，就不写，报出来。
    """
    period = snapshot.period
    problems = snapshot.mismatches()
    if problems:
        raise ValueError(f"{period} 明细和结算对不上，没有存档：" + "；".join(problems))
    result = ArchiveResult(period, [], [])
    tables = {t.name: t.table_id for t in bitable.list_tables()}

    plan = (
        (
            detail_table_name(period),
            DETAIL_COLUMNS,
            [detail_fields(period, r) for r in snapshot.details],
        ),
        (
            summary_table_name(period),
            SUMMARY_COLUMNS,
            [summary_fields(period, r) for r in snapshot.summary],
        ),
    )
    for name, columns, records in plan:
        if name in tables:
            result.skipped.append(name)
            continue
        table_id = create_table(client, app_token, name, columns)
        if records:
            bitable.batch_create_records(table_id, records)
        result.created.append(name)

    cumulative = tables.get(CUMULATIVE_TABLE)
    if cumulative is None:
        cumulative = create_table(client, app_token, CUMULATIVE_TABLE, DETAIL_COLUMNS)
    has_period = any(
        extract_text(r.fields.get(F_PERIOD)).strip() == period
        for r in bitable.iter_records(cumulative, field_names=[F_PERIOD])
    )
    if has_period:
        result.skipped.append(f"{CUMULATIVE_TABLE} 里的 {period}")
    elif snapshot.details:
        result.appended = bitable.batch_create_records(
            cumulative, [detail_fields(period, r) for r in snapshot.details]
        )
    # 这个月之前每天写进去的「未结算」行，正式存档后就不要了。
    _delete_live(bitable, cumulative, only=period)
    return result


def archive_period(
    settings: Any, bitable: BitableClient, client: lark.Client, period: str
) -> ArchiveResult:
    """月结和补存档共用的入口：读结算、现算客户明细、写表。"""
    from zoneinfo import ZoneInfo

    from ..domain.commission_query import CommissionQueryService

    commission_query = (
        CommissionQueryService(bitable, settings=settings) if settings.table_daily_board else None
    )
    snapshot = build_snapshot(
        bitable,
        settings,
        period,
        tz=ZoneInfo(settings.business_timezone),
        commission_query=commission_query,
    )
    return write_archive(bitable, client, settings.base_app_token, snapshot)


def refresh_live(
    settings: Any, bitable: BitableClient, today, client: lark.Client | None = None
) -> int:
    """每天导入后跑：把还没结算的月份（上个月如果还没结 + 这个月）现算一遍，覆盖总表里的
    「未结算」行。返回写了几行。月初 1 号下午结账之前上个月还没结，两个月都会在。"""
    from zoneinfo import ZoneInfo

    from ..domain.commission_query import CommissionQueryService
    from ..lark.client import get_client

    current = f"{today.year}-{today.month:02d}"
    settled = _settled_periods(bitable, settings)
    months = [m for m in (_previous(current), current) if m not in settled]

    tables = {t.name: t.table_id for t in bitable.list_tables()}
    cumulative = tables.get(CUMULATIVE_TABLE)
    if cumulative is None:
        cumulative = create_table(
            client or get_client(), settings.base_app_token, CUMULATIVE_TABLE, DETAIL_COLUMNS
        )
    _delete_live(bitable, cumulative)

    commission_query = (
        CommissionQueryService(bitable, settings=settings) if settings.table_daily_board else None
    )
    tz = ZoneInfo(settings.business_timezone)
    written = 0
    for month in months:
        rows = build_live(bitable, settings, month, tz=tz, commission_query=commission_query)
        if rows:
            written += bitable.batch_create_records(
                cumulative, [detail_fields(live_label(month), r) for r in rows]
            )
    return written
