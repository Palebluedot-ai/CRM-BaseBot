"""从 Base 读 ECAS 申请，按权限过滤，按月汇总。

两个调用方，用途不一样但读的是同一批数据：

  · ``jobs/ecas_reconcile.py``   结算，读全量，写汇总表
  · ``bot/handlers.py``          销售自查，只读归属自己的渠道

所以读取和权限过滤放在这里，别处不再各写一份。**算法在 ``domain/ecas.py``**，
这个模块只负责把 Base 里的行变成 ``ecas.EcasApplication``。

权限口径和交易佣金完全一致：普通销售只看**归属自己的渠道**，管理员看全部。
判据是渠道表的 ``登记人OpenID``，不是 ECAS 表自己那一列「负责销售」——
返佣是付给渠道的，谁经手那笔申请不决定谁该看到这笔钱。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from ..bot.auth import Sales, owned_records
from ..lark.bitable import BitableClient
from ..lark.values import extract_text, to_number
from . import ecas, schema

logger = logging.getLogger(__name__)


def link_ids(value: Any) -> list[str]:
    """关联字段可能是 ``['recXXX']``，也可能是 ``{'link_record_ids': [...]}``。"""
    if isinstance(value, list):
        return [
            item if isinstance(item, str) else str(item.get("record_id") or item.get("id") or "")
            for item in value
        ]
    if isinstance(value, dict):
        return list(value.get("link_record_ids") or [])
    return []


def load_payees(bitable: BitableClient, referral_table: str) -> dict[str, ecas.Payee]:
    """渠道表 record_id -> Payee。只取编号和名字，**不取比例**。

    ECAS 的比例逐行来自 ECAS 数据，见 ``domain/ecas.py`` 开头。
    """
    out: dict[str, ecas.Payee] = {}
    for record in bitable.iter_records(
        referral_table, field_names=[schema.REFERRAL_NO, schema.REFERRAL_NAME]
    ):
        out[record.record_id] = ecas.Payee(
            code=extract_text(record.fields.get(schema.REFERRAL_NO)),
            name=extract_text(record.fields.get(schema.REFERRAL_NAME)),
        )
    return out


def owned_referral_ids(bitable: BitableClient, referral_table: str, sales: Sales) -> set[str]:
    """这名销售能看的渠道 record_id。管理员是全部。"""
    records = bitable.iter_records(
        referral_table, field_names=[schema.REFERRAL_NO, schema.REFERRAL_OWNER_OPEN_ID]
    )
    return {r.record_id for r in owned_records(sales, records, schema.REFERRAL_OWNER_OPEN_ID)}


def load_applications(
    bitable: BitableClient,
    table_id: str,
    payees: dict[str, ecas.Payee],
    *,
    tz,
    allowed_referral_ids: set[str] | None = None,
) -> list[ecas.EcasApplication]:
    """ECAS 申请表 -> 计算用的行。

    ``allowed_referral_ids`` 给 None 表示不限（结算走这条）。给了集合就只留挂在
    那批渠道下的申请 —— 销售自查走这条。

    没挂上渠道关联但填了介绍人名字的行，在**不限权限**时照样结算：钱是欠着的，
    藏起来只会让合计对不上来源表。但在**限权限**时它们一律不出现 —— 没有渠道就
    判不出归属，给谁看都是错的。
    """
    applications: list[ecas.EcasApplication] = []
    for record in bitable.iter_records(table_id):
        rate = to_number(record.fields.get(ecas.ECAS_RATE))
        amount = to_number(record.fields.get(ecas.ECAS_AMOUNT))
        applied = record.fields.get(ecas.ECAS_APPLIED_AT)
        name = extract_text(record.fields.get(ecas.ECAS_CLIENT_NAME))

        payee: ecas.Payee | None = None
        linked = link_ids(record.fields.get(ecas.ECAS_REFERRAL_LINK))
        for record_id in linked:
            if record_id in payees:
                payee = payees[record_id]
                break

        if allowed_referral_ids is not None:
            if not any(record_id in allowed_referral_ids for record_id in linked):
                continue
        elif payee is None:
            written_name = extract_text(record.fields.get(ecas.ECAS_REFERRER_NAME))
            if written_name:
                payee = ecas.Payee(code="", name=written_name)

        period = ""
        if isinstance(applied, int | float) and not isinstance(applied, bool):
            period = ecas.period_of(datetime.fromtimestamp(float(applied) / 1000, tz=tz), tz=tz)

        # 客户UID 列是文本（18-19 位存成数字会被抹平低位）。不是纯数字的一律当没填：
        # 它只用来把同一个客户排到同一行，对不上时退回按名字对，不影响任何金额。
        uid = extract_text(record.fields.get(ecas.ECAS_CLIENT_UID)).strip()

        applications.append(
            ecas.EcasApplication(
                client_name=name,
                amount=Decimal(str(amount)) if amount is not None else Decimal("0"),
                period=period,
                payee=payee,
                rate_percent=Decimal(str(rate)) if rate is not None else None,
                client_uid=uid if uid.isdigit() else "",
            )
        )
    return applications


def latest_applied_date(bitable: BitableClient, table_id: str, *, tz) -> str:
    """申请表里最新那笔申请的日期，形如 ``2026-09-22``。空表返回空串。

    这是**数据新鲜度**的指标，不是业务数据。交易看板每天自动导，ECAS 申请表要人手工
    跑 ``scripts/import_ecas.py`` —— 所以「这个月只有 5,000」和「这个月的申请还没导
    进来」在金额上长得一模一样。把截止日期摆出来，两者就分得开了。
    """
    latest = 0.0
    for record in bitable.iter_records(table_id, field_names=[ecas.ECAS_APPLIED_AT]):
        value = record.fields.get(ecas.ECAS_APPLIED_AT)
        if isinstance(value, int | float) and not isinstance(value, bool):
            latest = max(latest, float(value))
    if not latest:
        return ""
    return datetime.fromtimestamp(latest / 1000, tz=tz).date().isoformat()


class EcasQueryService:
    """销售自查 ECAS 返佣。只读，不写任何东西。

    不缓存维表 —— 渠道数量小（几十到几百），缓存反而会导致「刚登记的渠道查不到」
    这类隔层问题，和 ``CommissionQueryService`` 同一个取舍。
    """

    def __init__(self, bitable: BitableClient, *, settings) -> None:
        self._bitable = bitable
        self._settings = settings
        self._tz = ZoneInfo(settings.business_timezone)

    def _applications(self, sales: Sales) -> list[ecas.EcasApplication]:
        payees = load_payees(self._bitable, self._settings.table_referral)
        allowed = owned_referral_ids(self._bitable, self._settings.table_referral, sales)
        return load_applications(
            self._bitable,
            self._settings.table_ecas,
            payees,
            tz=self._tz,
            allowed_referral_ids=allowed,
        )

    def query(self, sales: Sales, period: str) -> list[ecas.EcasCommissionRow]:
        return ecas.aggregate(self._applications(sales), period=period)

    def periods_for(self, sales: Sales) -> list[str]:
        """这名销售有返佣的月份，从早到晚。

        月份列表按**本人能看到的数据**算，不是全表：下拉里列出一个他点进去必然是空的
        月份，只会让人以为系统坏了。
        """
        return ecas.periods_in(self._applications(sales))


def summarize(rows: list[ecas.EcasCommissionRow], *, period: str, viewer_name: str) -> str:
    """把查询结果拼成一段人看的 markdown（供卡片展示）。

    版式对齐交易佣金那张结果卡：顶部三个总数，然后每个渠道一段。
    光有一个金额，看的人没法判断它合不合理 —— 少了一个渠道，金额照样是个像样的数字。
    """
    if not rows:
        return (
            f"**{period}**  {viewer_name} 名下没有 ECAS 返佣。\n\n"
            "可能原因：这个月你名下的渠道没有介绍 ECAS 开户；"
            "或者那些申请还没导进 ECAS Applications 表。"
        )

    total = sum((row.payable for row in rows), Decimal("0"))
    clients = sum(row.client_count for row in rows)
    amount = sum((row.amount_total for row in rows), Decimal("0"))

    lines = [
        f"**{period}**",
        f"ECAS 返佣合计  **{total:,.2f}** USD",
        f"{len(rows)} 个渠道 · {clients} 个客户 · 开户金额合计 {amount:,.2f}",
        "",
    ]
    for row in rows:
        label = row.payee.label or "(未命名)"
        lines.append(f"**{label}**  —— 应付 {row.payable:,.2f} USD")
        lines.append(
            f"  小计：开户 {row.amount_total:,.2f} · "
            f"{row.client_count} 个客户 · {row.txn_count} 笔 · 比例 {row.rate_note}"
        )
        for name in sorted(row.client_names):
            lines.append(f"  · {name or '(未命名客户)'}")
        lines.append("")

    lines.append("<font color='grey'>这是 ECAS 开户返佣，和交易佣金是两笔钱，分开结算。</font>")
    return "\n".join(lines)
