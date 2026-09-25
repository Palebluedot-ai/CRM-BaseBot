#!/usr/bin/env python
"""把 ECAS 系统导出的申请**追加**进 ECAS Applications —— 已有的一笔不动，只加新的。

    uv run python scripts/append_ecas.py --file export.xlsx --assign "Prance Wang=R095:50"
    uv run python scripts/append_ecas.py --file export.xlsx --assign "Prance Wang=R095:50" --apply

不加 ``--apply`` 只预演。

## 为什么是追加，不是整表替换（2026-09-25 定的）

以前 ``import_ecas.py`` 拿一份财务表整张盖掉 Base 里的表。现在来源变成每月一份 ECAS
系统导出，而且介绍人、比例要在 Base 里补 —— 整表替换会把补上去的内容一起冲掉。所以：

  · 以前的记录一律保留，一个字不改；
  · 导出里**表里还没有**的申请，一笔加一行；
  · 介绍人和比例导出里没有：没被 ``--assign`` 认领的行先空着（返佣为 0），
    之后在 Base 里直接填，不会再被冲掉。

## 怎么认出「已经在表里了」

每笔申请有唯一的「引用ID」，追加进来的行都带着它，再导一次同一份（或者日期重叠的
下一份）不会重复。**不用「审批单号」**：一张审批单能批好几笔 —— HONG KONG XIAOJIA
9/11 的 2000、2000、1000 三笔就共用一个审批单号，按它认会丢掉两笔。

2026-09-25 之前按财务表导入的老记录没有引用ID，就按「客户名 + 同一天 + 同一金额」认：
认出来算已有，不加。老记录里真有同一天同金额的两笔，会被当成同一笔 —— 预演里会列出来，
看一眼再 --apply。

## --assign：哪个销售的客户算哪个渠道、多少比例

``--assign "销售名=渠道编号:比例"``，可以给多次。例如 JIANG JUN（R095）的客户都是
Prance 负责，比例 50%：``--assign "Prance Wang=R095:50"``。只作用于这次加进去的新行，
老记录不碰。

只收状态是 APPROVED 的申请，其它状态列出来不导。
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import import_ecas  # noqa: E402
import openpyxl  # noqa: E402

from crm_basebot.domain import ecas, schema  # noqa: E402
from crm_basebot.domain.names import norm  # noqa: E402
from crm_basebot.lark.bitable import FIELD_TYPE_FORMULA, BitableClient  # noqa: E402
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.lark.values import (  # noqa: E402
    PrecisionLossError,
    extract_text,
    to_number,
    to_uid,
)
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)

COL_UID = "UID"
COL_STATUS = "状态"
COL_CREATED = "创建时间"
COL_NAME = "账户名称"
COL_AMOUNT = "收费金额"
COL_SALES = "销售名称"
COL_REF = "引用ID"
REQUIRED = (COL_UID, COL_STATUS, COL_CREATED, COL_NAME, COL_AMOUNT, COL_SALES, COL_REF)
APPROVED = "APPROVED"


@dataclass
class Application:
    line: int
    ref_id: str
    name: str
    uid: str
    amount: Decimal
    created: datetime
    sales: str


@dataclass(frozen=True)
class Assignment:
    referral_no: str
    rate: Decimal


def parse_assign(values: list[str]) -> dict[str, Assignment]:
    """``["Prance Wang=R095:50"]`` -> ``{norm("Prance Wang"): Assignment("R095", 50)}``。"""
    out: dict[str, Assignment] = {}
    for value in values:
        try:
            sales, target = value.split("=", 1)
            referral_no, rate = target.split(":", 1)
            assignment = Assignment(referral_no.strip().upper(), Decimal(rate.strip()))
        except (ValueError, ArithmeticError):
            raise SystemExit(f'--assign 写法不对：「{value}」，要写成 "销售名=R095:50"') from None
        if not 0 < assignment.rate <= 100:
            raise SystemExit(f"--assign 比例要在 0 到 100 之间：「{value}」")
        out[norm(sales)] = assignment
    return out


def read_export(path: Path) -> tuple[list[Application], list[str]]:
    """导出 -> (APPROVED 的申请, 跳过的行的说明)。"""
    sheet = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    lines = list(sheet.iter_rows(values_only=True))
    header = [str(cell or "").strip() for cell in lines[0]] if lines else []
    missing = [column for column in REQUIRED if column not in header]
    if missing:
        raise SystemExit(f"{path} 第一行少了这几列：{'、'.join(missing)}。这是 ECAS 系统导出吗？")
    index = {column: header.index(column) for column in REQUIRED}

    apps: list[Application] = []
    skipped: list[str] = []
    for number, cells in enumerate(lines[1:], start=2):

        def cell(column, cells=cells):
            position = index[column]
            return cells[position] if position < len(cells) else None

        if all(value in (None, "") for value in cells):
            continue
        name = str(cell(COL_NAME) or "").strip()
        status = str(cell(COL_STATUS) or "").strip().upper()
        if status != APPROVED:
            skipped.append(f"第{number}行 {name}：状态是 {status or '(空)'}，不是 {APPROVED}")
            continue
        created = cell(COL_CREATED)
        amount = to_number(cell(COL_AMOUNT))
        if not isinstance(created, datetime) or amount is None:
            skipped.append(f"第{number}行 {name}：创建时间或收费金额读不出来")
            continue
        try:
            uid = to_uid(cell(COL_UID))
        except PrecisionLossError:
            uid = ""
            skipped.append(f"第{number}行 {name}：UID 是数字格子、精度已丢，这一行照导但 UID 留空")
        apps.append(
            Application(
                line=number,
                ref_id=str(cell(COL_REF) or "").strip(),
                name=name,
                uid=uid if uid.isdigit() else "",
                amount=Decimal(str(amount)),
                created=created,
                sales=str(cell(COL_SALES) or "").strip(),
            )
        )
    return apps, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 ECAS 系统导出追加进 ECAS Applications")
    parser.add_argument("--file", required=True, help="ECAS 系统导出的 xlsx")
    parser.add_argument(
        "--assign",
        action="append",
        default=[],
        metavar="销售名=渠道编号:比例",
        help='这个销售的新申请记到哪个渠道、多少比例，例如 "Prance Wang=R095:50"。可以给多次',
    )
    parser.add_argument("--apply", action="store_true", help="真写；不加则只预演")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_REFERRAL", "TABLE_ECAS")
    bitable = BitableClient(settings.base_app_token)
    tz = ZoneInfo(settings.business_timezone)
    assign = parse_assign(args.assign)

    # 认领到的渠道要真的在渠道表里。
    referrals: dict[str, tuple[str, str]] = {}
    for record in bitable.iter_records(
        settings.table_referral, field_names=[schema.REFERRAL_NO, schema.REFERRAL_NAME]
    ):
        no = extract_text(record.fields.get(schema.REFERRAL_NO)).upper()
        referrals[no] = (record.record_id, extract_text(record.fields.get(schema.REFERRAL_NAME)))
    for sales, assignment in assign.items():
        if assignment.referral_no not in referrals:
            print(f"渠道表里没有 {assignment.referral_no}（--assign 里 {sales} 那一条）")
            return 1

    apps, skipped = read_export(Path(args.file))
    print(f"读取 {args.file}：{len(apps)} 笔已批准的申请")
    for line in skipped:
        print(f"  ⚠ {line}")

    # ---------- 表里已经有的 ----------
    known_refs: set[str] = set()
    # 老记录（没有引用ID）只能按「客户名 + 同一天 + 同金额」认。
    known_keys: set[tuple[str, str, Decimal]] = set()
    for record in bitable.iter_records(settings.table_ecas):
        fields = record.fields
        ref = extract_text(fields.get(ecas.ECAS_REF_ID)).strip()
        if ref:
            known_refs.add(ref)
            # 带引用ID的行只按引用ID认。同名同日同金额的真新申请（XIAOJIA 9/11 就有
            # 两笔 2000）不能因为长得像就被挡掉。
            continue
        applied = fields.get(ecas.ECAS_APPLIED_AT)
        amount = to_number(fields.get(ecas.ECAS_AMOUNT))
        if (
            isinstance(applied, int | float)
            and not isinstance(applied, bool)
            and amount is not None
        ):
            day = datetime.fromtimestamp(float(applied) / 1000, tz=tz).date().isoformat()
            known_keys.add(
                (norm(extract_text(fields.get(ecas.ECAS_CLIENT_NAME))), day, Decimal(str(amount)))
            )

    new: list[Application] = []
    seen_in_file: set[str] = set()
    for app in apps:
        key = (norm(app.name), app.created.date().isoformat(), app.amount)
        if app.ref_id and (app.ref_id in known_refs or app.ref_id in seen_in_file):
            print(f"  - 第{app.line}行 {app.name} {app.amount}：引用ID {app.ref_id} 已经在表里")
            continue
        if key in known_keys:
            print(
                f"  - 第{app.line}行 {app.name} {app.amount}：同名、同一天、同金额的记录已经在表里"
            )
            continue
        seen_in_file.add(app.ref_id)
        new.append(app)

    # ---------- 预演 ----------
    fee_by_referral: dict[str, Decimal] = {}
    print(f"\n要加进去的 {len(new)} 笔：")
    for app in new:
        assignment = assign.get(norm(app.sales))
        tag = "（没有介绍人，返佣 0，之后可以在 Base 里补）"
        if assignment:
            fee = (app.amount * assignment.rate / 100).quantize(Decimal("0.01"))
            fee_by_referral[assignment.referral_no] = (
                fee_by_referral.get(assignment.referral_no, Decimal("0")) + fee
            )
            tag = f"→ {assignment.referral_no} {assignment.rate}%  返佣 {fee:,.2f}"
        print(
            f"  + {app.created:%Y-%m-%d} {app.name[:34]:34} {app.amount:>10,.2f} "
            f"{app.sales[:14]:14} {tag}"
        )
    for referral_no, fee in sorted(fee_by_referral.items()):
        print(f"\n  {referral_no} {referrals[referral_no][1]}：返佣合计 {fee:,.2f}")

    if not args.apply:
        print("\n（预演，没写。确认无误后加 --apply）")
        return 0
    if not new:
        print("\n没有要加的。")
        return 0

    # 「引用ID」这一列是 2026-09-25 加的，旧表里没有就先补上。
    table_id = import_ecas.ensure_table(
        bitable,
        get_client(),
        settings.base_app_token,
        ecas.TABLE_ECAS_NAME,
        ecas.ECAS_FIELDS,
        link_table_id=settings.table_referral,
        link_field=ecas.ECAS_REFERRAL_LINK,
        formulas=ecas.ECAS_FORMULAS,
    )
    primary = bitable.resolve_primary_field(table_id)
    payloads = [build_payload(app, assign, referrals, primary.name, tz) for app in new]
    written = bitable.batch_create_records(table_id, payloads)
    print(f"\n加了 {written} 条。以前的记录一条没动。")
    print("下一步（算返佣）：uv run python -m crm_basebot.jobs.ecas_reconcile --period 2026-09")
    return 0


def build_payload(
    app: Application,
    assign: dict[str, Assignment],
    referrals: dict[str, tuple[str, str]],
    primary_name: str,
    tz: ZoneInfo,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        ecas.ECAS_CLIENT_NAME: app.name,
        ecas.ECAS_AMOUNT: float(app.amount),
        # 导出里的时间是业务时区的钟点（新加坡），存成那一刻的 UTC 毫秒。
        ecas.ECAS_APPLIED_AT: int(app.created.replace(tzinfo=tz).timestamp() * 1000),
        ecas.ECAS_SALES_NAME: app.sales,
        ecas.ECAS_REF_ID: app.ref_id,
    }
    if app.uid:
        fields[ecas.ECAS_CLIENT_UID] = app.uid
    assignment = assign.get(norm(app.sales))
    if assignment:
        record_id, name = referrals[assignment.referral_no]
        fields[ecas.ECAS_REFERRAL_LINK] = [record_id]
        fields[ecas.ECAS_REFERRER_NAME] = name
        fields[ecas.ECAS_RATE] = float(assignment.rate)
    if primary_name not in fields:
        fields[primary_name] = app.name
    return {k: v for k, v in fields.items() if ecas.ECAS_FIELDS.get(k) != FIELD_TYPE_FORMULA}


if __name__ == "__main__":
    raise SystemExit(main())
