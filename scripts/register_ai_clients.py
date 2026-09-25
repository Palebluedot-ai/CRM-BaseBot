#!/usr/bin/env python
"""把一张名单上的 AI 客户，一次登记到某个渠道名下（带 AI 状态和升级日期）。

    uv run python scripts/register_ai_clients.py --file JIANGJUN_AI.xlsx --referral R095
    uv run python scripts/register_ai_clients.py --file JIANGJUN_AI.xlsx --referral R095 --apply

不加 ``--apply`` 只预演：列出每一行会怎么处理，一个字都不写。

## 名单长什么样

一个 xlsx，第一个分页，第一行是表头，至少这四列（顺序不限）：

    客户名称 | 客户UID | AI状态 | 升级AI日期

- ``客户UID`` 必须是**文本**格式的格子。数字格子里的 19 位 UID 已经被 Excel 抹掉了末几位，
  读到就停下，不写。
- ``AI状态`` 是 开户即AI / 升级为AI / 非AI 三选一；``升级AI日期`` 在「升级为AI」时必填。
  规则见 ``src/crm_basebot/domain/ai_status.py``。

## 登记人是谁

**就是这个渠道的负责人**：渠道表里那一行的「登记人OpenID」。2026-09-25 起 JIANG JUN
（R095）归 Prance，这批客户登记人也就是 Prance —— 机器人里谁看得到这些客户，由它决定。
渠道那一行的「登记人OpenID」是空的就停下，先去 Base 里填上。

## 绝不做的事

**已经登记过的 UID 一律跳过，不改。** 挂在别的渠道下的也只是列出来 —— 改归属等于把
一笔正在付的佣金悄悄改付给别人，要人来决定。
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import openpyxl  # noqa: E402

from crm_basebot.bot.auth import Sales, SalesDirectory  # noqa: E402
from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.audit import AuditLog  # noqa: E402
from crm_basebot.domain.referral import ValidationError  # noqa: E402
from crm_basebot.domain.referred_client import (  # noqa: E402
    ClientInput,
    ReferredClientService,
)
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import (  # noqa: E402
    PrecisionLossError,
    extract_text,
    looks_excel_truncated,
    to_uid,
)
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)

COL_NAME = "客户名称"
COL_UID = "客户UID"
COL_STATUS = "AI状态"
COL_DATE = "升级AI日期"
REQUIRED = (COL_NAME, COL_UID, COL_STATUS, COL_DATE)


@dataclass
class Row:
    line: int
    name: str
    uid: str
    status: str
    ai_date: date | None
    problem: str = ""


def _date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-")
    try:
        year, month, day = (int(part) for part in text.split("-"))
        return date(year, month, day)
    except ValueError:
        raise ValueError(f"日期看不懂：{value!r}，要写成 2026-08-24") from None


def read_rows(path: Path) -> list[Row]:
    sheet = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    lines = list(sheet.iter_rows(values_only=True))
    if not lines:
        raise SystemExit(f"{path} 是空的")
    header = [str(cell or "").strip() for cell in lines[0]]
    missing = [column for column in REQUIRED if column not in header]
    if missing:
        raise SystemExit(
            f"{path} 第一行少了这几列：{'、'.join(missing)}（要有 {'、'.join(REQUIRED)}）"
        )
    index = {column: header.index(column) for column in REQUIRED}

    rows: list[Row] = []
    for number, cells in enumerate(lines[1:], start=2):

        def cell(column, cells=cells):
            position = index[column]
            return cells[position] if position < len(cells) else None

        if all(value in (None, "") for value in cells):
            continue
        row = Row(
            line=number,
            name=str(cell(COL_NAME) or "").strip(),
            uid="",
            status=str(cell(COL_STATUS) or "").strip(),
            ai_date=None,
        )
        try:
            row.uid = to_uid(cell(COL_UID))
        except PrecisionLossError:
            row.problem = "UID 是数字格子，末几位已经被 Excel 抹掉了 —— 把那一列改成文本重新填"
        try:
            row.ai_date = _date(cell(COL_DATE))
        except ValueError as exc:
            row.problem = row.problem or str(exc)
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把名单上的 AI 客户登记到一个渠道名下")
    parser.add_argument("--file", required=True, help="名单 xlsx")
    parser.add_argument("--referral", required=True, help="渠道编号，例如 R095")
    parser.add_argument("--apply", action="store_true", help="真写；不加则只预演")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(
        settings,
        "LARK_BASE_APP_TOKEN",
        "TABLE_CLIENT",
        "TABLE_REFERRAL",
        "TABLE_SALES",
        "TABLE_AUDIT",
    )
    bitable = BitableClient(settings.base_app_token)
    tz = ZoneInfo(settings.business_timezone)
    referral_no = args.referral.strip().upper()

    # 客户表得先有 AI 那两列，不然写进去整行被拒。
    columns = {f.name for f in bitable.list_fields(settings.table_client)}
    lacking = [c for c in (schema.CLIENT_AI_STATUS, schema.CLIENT_AI_DATE) if c not in columns]
    if lacking:
        print(
            f"客户表还没有这几列：{'、'.join(lacking)}。"
            "先跑：uv run python scripts/sync_base.py --apply"
        )
        return 1

    # 登记人 = 渠道负责人（渠道那一行的登记人OpenID）。
    owner_open_id = owner_name = ""
    for record in bitable.iter_records(settings.table_referral):
        if extract_text(record.fields.get(schema.REFERRAL_NO)).upper() == referral_no:
            owner_open_id = extract_text(record.fields.get(schema.REFERRAL_OWNER_OPEN_ID))
            owner_name = extract_text(record.fields.get(schema.REFERRAL_SALES_NAME))
            break
    else:
        print(f"渠道表里没有 {referral_no}")
        return 1
    if not owner_open_id:
        print(
            f"{referral_no} 那一行的「{schema.REFERRAL_OWNER_OPEN_ID}」是空的。"
            "先在 Base 里填上负责人的 OpenID。"
        )
        return 1
    rostered = SalesDirectory(bitable, settings.table_sales).lookup(owner_open_id)
    owner = Sales(
        open_id=owner_open_id,
        name=rostered.name if rostered else owner_name,
        role=schema.ROLE_SALES,
        is_active=True,
    )
    print(f"渠道 {referral_no}，登记人：{owner.name or '(名册里没有这个人)'}  {owner_open_id}")

    service = ReferredClientService(
        bitable,
        settings.table_client,
        settings.table_referral,
        AuditLog(bitable, settings.table_audit),
        tz=tz,
    )
    existing: dict[str, str] = {}
    for record in bitable.iter_records(settings.table_client):
        uid = to_uid(record.fields.get(schema.CLIENT_UID))
        if uid:
            existing[uid] = extract_text(record.fields.get(schema.CLIENT_NAME))

    rows = read_rows(Path(args.file))
    todo: list[tuple[Row, ClientInput]] = []
    print(f"\n名单 {len(rows)} 行：")
    for row in rows:
        label = f"第{row.line}行 {row.name[:30]:30} {row.uid or '(无UID)':20}"
        if row.problem:
            print(f"  ✗ {label} {row.problem}")
            continue
        if row.uid in existing:
            print(f"  - {label} 已经登记过（{existing[row.uid]}），跳过不改")
            continue
        try:
            data = ClientInput(
                uid=row.uid,
                name=row.name,
                referral_no=referral_no,
                ai_status=row.status,
                ai_date=row.ai_date,
            ).validated()
        except ValidationError as exc:
            print(f"  ✗ {label} {exc}")
            continue
        warn = (
            "  ⚠ UID 末尾一串 0，像被 Excel 截过，核对一下"
            if looks_excel_truncated(row.uid)
            else ""
        )
        when = row.ai_date.isoformat() if row.ai_date else ""
        print(f"  + {label} {data.ai_status} {when}{warn}")
        todo.append((row, data))

    print(f"\n要登记 {len(todo)} 个。")
    if not args.apply:
        print("（预演，没写。确认无误后加 --apply）")
        return 0

    failed = 0
    for row, data in todo:
        try:
            service.create(owner, data)
        except ValidationError as exc:
            failed += 1
            print(f"  ✗ 第{row.line}行 {row.name}：{exc}")
    print(f"写完：成功 {len(todo) - failed} 个，失败 {failed} 个。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
