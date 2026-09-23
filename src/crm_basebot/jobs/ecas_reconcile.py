"""ECAS 返佣按月对账：从 ``ECAS Applications`` 算，写回 ``ECAS Commission Summary``。

    uv run python -m crm_basebot.jobs.ecas_reconcile                    # 最新有数据的月份
    uv run python -m crm_basebot.jobs.ecas_reconcile --period 2026-08
    uv run python -m crm_basebot.jobs.ecas_reconcile --period 2026-08 --write
    uv run python -m crm_basebot.jobs.ecas_reconcile --all-periods --write --replace

行为和交易佣金那个 ``reconcile`` 一模一样，是刻意的 —— 同一套开关、同一套拒绝规则，
用的人不用记两套。默认只算不写；``--write`` 碰到汇总表里已有本次月份的行会拒绝，
要顶掉得显式加 ``--replace``；算出来是空的时候不会拿空结果去顶掉已有的汇总。

**但它和那个 reconcile 没有共用任何数据。** 读的是 ECAS 申请表，比例来自每一行自己，
写的是 ECAS 自己的汇总表。交易佣金的三张表在这里一张都不读。理由见 ``domain/ecas.py``。

比例来自行、不来自渠道，所以汇总行里的「比例说明」是文本而不是数字：一个渠道一个月
可以有好几档比例，硬塞一个数字进去不管填哪档都是在撒谎。
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from ..domain import ecas, schema
from ..domain.audit import ACTION_COMPUTE_ECAS, AuditLog
from ..lark.bitable import BitableClient, assert_fields_present
from ..lark.values import extract_text, to_number
from ..startup import load_settings, require_settings

logger = logging.getLogger(__name__)


REQUIRED_KEYS = ("LARK_BASE_APP_TOKEN", "TABLE_ECAS", "TABLE_REFERRAL")
WRITE_KEYS = ("TABLE_ECAS_COMMISSION", "TABLE_AUDIT")

# 算钱真正依赖的三列。类型给 None 表示只要求存在。
ECAS_REQUIRED_FIELDS: dict[str, int | None] = {
    ecas.ECAS_APPLIED_AT: None,
    ecas.ECAS_AMOUNT: None,
    ecas.ECAS_RATE: None,
}


class WriteRefused(RuntimeError):
    """汇总表的现状不允许这次写入。入口把它打出来、以非 0 退出，一行都不改。"""


def _link_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [
            item if isinstance(item, str) else str(item.get("record_id") or item.get("id") or "")
            for item in value
        ]
    if isinstance(value, dict):
        return list(value.get("link_record_ids") or [])
    return []


def load_payees(bitable: BitableClient, referral_table: str) -> dict[str, ecas.Payee]:
    """渠道表 record_id -> Payee。只取编号和名字，**不取比例**。"""
    out: dict[str, ecas.Payee] = {}
    for record in bitable.iter_records(
        referral_table, field_names=[schema.REFERRAL_NO, schema.REFERRAL_NAME]
    ):
        out[record.record_id] = ecas.Payee(
            code=extract_text(record.fields.get(schema.REFERRAL_NO)),
            name=extract_text(record.fields.get(schema.REFERRAL_NAME)),
        )
    return out


def load_applications(
    bitable: BitableClient, table_id: str, payees: dict[str, ecas.Payee], *, tz
) -> list[ecas.EcasApplication]:
    """ECAS 申请表 -> 计算用的行。

    没挂上渠道关联但填了介绍人名字的行**照样结算**，收款人就是那个名字、编号留空 ——
    钱是欠着的，藏起来只会让合计对不上来源表。
    """
    applications: list[ecas.EcasApplication] = []
    for record in bitable.iter_records(table_id):
        rate = to_number(record.fields.get(ecas.ECAS_RATE))
        amount = to_number(record.fields.get(ecas.ECAS_AMOUNT))
        applied = record.fields.get(ecas.ECAS_APPLIED_AT)
        name = extract_text(record.fields.get(ecas.ECAS_CLIENT_NAME))

        payee: ecas.Payee | None = None
        for record_id in _link_ids(record.fields.get(ecas.ECAS_REFERRAL_LINK)):
            if record_id in payees:
                payee = payees[record_id]
                break
        if payee is None:
            written_name = extract_text(record.fields.get(ecas.ECAS_REFERRER_NAME))
            if written_name:
                payee = ecas.Payee(code="", name=written_name)

        period = ""
        if isinstance(applied, int | float) and not isinstance(applied, bool):
            period = ecas.period_of(datetime.fromtimestamp(float(applied) / 1000, tz=tz), tz=tz)

        applications.append(
            ecas.EcasApplication(
                client_name=name,
                amount=Decimal(str(amount)) if amount is not None else Decimal("0"),
                period=period,
                payee=payee,
                rate_percent=Decimal(str(rate)) if rate is not None else None,
            )
        )
    return applications


def existing_summary(
    bitable: BitableClient, table_id: str, periods: set[str] | None
) -> dict[str, list[str]]:
    found: dict[str, list[str]] = defaultdict(list)
    for record in bitable.iter_records(table_id, field_names=[ecas.ECOMM_PERIOD]):
        period = extract_text(record.fields.get(ecas.ECOMM_PERIOD))
        if periods is None or period in periods:
            found[period].append(record.record_id)
    return dict(found)


def _describe(existing: dict[str, list[str]]) -> str:
    return "、".join(
        f"{period or '(月份为空)'}（{len(ids)} 行）" for period, ids in sorted(existing.items())
    )


def write_summary(
    bitable: BitableClient,
    table_id: str,
    rows: list[ecas.EcasCommissionRow],
    *,
    periods: set[str] | None,
    replace: bool,
) -> tuple[int, int]:
    """写汇总，返回 (删除行数, 写入行数)。两种拒绝都发生在动手之前。"""
    existing = existing_summary(bitable, table_id, periods)

    if existing and not replace:
        raise WriteRefused(
            f"ECAS 汇总表里已经有 {_describe(existing)} 的汇总，不会在旁边再写一套。"
            "要用这次的结果顶掉它们，加 --replace（先删旧行再写新行）。"
        )
    if existing and not rows:
        raise WriteRefused(
            f"这次算出来是空的，不会拿空结果去顶掉 ECAS 汇总表里已有的 {_describe(existing)}。"
            "先确认 ECAS 申请表导进来了，再跑。"
        )

    deleted = 0
    for record_ids in existing.values():
        for record_id in record_ids:
            bitable.delete_record(table_id, record_id)
            deleted += 1

    now_ms = int(time.time() * 1000)
    written = 0
    for row in rows:
        bitable.create_record(
            table_id,
            {
                ecas.ECOMM_PERIOD: row.period,
                ecas.ECOMM_REFERRAL_NO: row.payee.code,
                ecas.ECOMM_REFERRAL_NAME: row.payee.name,
                ecas.ECOMM_CLIENT_COUNT: row.client_count,
                ecas.ECOMM_TXN_COUNT: row.txn_count,
                ecas.ECOMM_AMOUNT_TOTAL: float(row.amount_total),
                ecas.ECOMM_RATE_NOTE: row.rate_note,
                ecas.ECOMM_PAYABLE: float(row.payable),
                ecas.ECOMM_COMPUTED_AT: now_ms,
            },
            reread=False,
        )
        written += 1
    return deleted, written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按月计算 ECAS 开户返佣")
    parser.add_argument(
        "--period",
        help="结算月份 YYYY-MM。不传的话结算 ECAS 申请表里最新有数据的那个月，实际选中的会打印出来",
    )
    parser.add_argument("--all-periods", action="store_true", help="算所有月份，忽略 --period")
    parser.add_argument(
        "--write",
        action="store_true",
        help="把汇总写进 Base。不加就只打印。汇总表里已有本次月份的行时会拒绝",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="汇总表里已有本次结算月份的行时，先删掉它们再写。只和 --write 一起用",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.replace and not args.write:
        parser.error("--replace 只在 --write 时有意义：不写就没有什么可替换的")

    settings = load_settings()
    require_settings(settings, *REQUIRED_KEYS)
    if args.write:
        require_settings(settings, *WRITE_KEYS)

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    return run(args, settings, BitableClient(settings.base_app_token))


def run(args: argparse.Namespace, settings, bitable: BitableClient) -> int:
    assert_fields_present(
        bitable.list_fields(settings.table_ecas),
        ECAS_REQUIRED_FIELDS,
        table_label="ECAS 申请表",
    )

    tz = ZoneInfo(settings.business_timezone)
    payees = load_payees(bitable, settings.table_referral)
    applications = load_applications(bitable, settings.table_ecas, payees, tz=tz)
    periods = ecas.periods_in(applications)

    if args.all_periods:
        period = None
        scope = "全部月份"
    elif args.period:
        period = args.period
        scope = f"{period}（你显式指定的）"
    else:
        if not periods:
            print(
                "\n定不出要结算哪个月：ECAS 申请表里没有一笔带介绍人和比例的申请。\n"
                "先确认 scripts/import_ecas.py 跑过了，或者用 --period YYYY-MM 显式指定。"
            )
            return 1
        period = periods[-1]
        scope = f"{period}（自动选定：ECAS 申请表里最新有数据的月份）"

    rows = ecas.aggregate(applications, period=period)

    print(f"\nECAS 结算范围：{scope}\n")
    print(ecas.summarize(rows))

    no_code = [r for r in rows if not r.payee.code]
    if no_code:
        owed = sum((r.payable for r in no_code), Decimal("0"))
        print(
            f"\n注意：{len(no_code)} 个收款方在渠道表里没有登记，"
            f"合计 {owed:,.2f} USD 的返佣算出来了但没有渠道编号。"
            "\n去渠道表把他们登记了（名字要和 ECAS 表一致），再重跑一次 scripts/import_ecas.py。"
        )

    if period and not rows:
        print(f"\n{period} 没有带介绍人的 ECAS 申请。")

    if not args.write:
        print("\n（只算没写。确认无误后加 --write 写进 Base）")
        return 0

    target_periods = None if args.all_periods else {period}
    try:
        deleted, written = write_summary(
            bitable,
            settings.table_ecas_commission,
            rows,
            periods=target_periods,
            replace=args.replace,
        )
    except WriteRefused as exc:
        print(f"\n没有写入：{exc}")
        return 1

    AuditLog(bitable, settings.table_audit).record(
        actor_open_id="system",
        actor_name="ECAS对账任务",
        action=ACTION_COMPUTE_ECAS,
        target_table=ecas.TABLE_ECAS_COMMISSION_NAME,
        detail={
            "结算范围": period or "全部月份",
            "替换": args.replace,
            "删除行数": deleted,
            "写入行数": written,
            "未登记渠道数": len(no_code),
        },
    )

    if deleted:
        print(f"\n已删除 {deleted} 行旧汇总，写入 {written} 行。")
    else:
        print(f"\n已写入 {written} 行汇总。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
