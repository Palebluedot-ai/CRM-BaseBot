#!/usr/bin/env python
"""把一个渠道的交易佣金逐笔导成 Excel，给渠道核对用。**只读 Base。**

    uv run python scripts/export_channel_trades.py --channel R095
    uv run python scripts/export_channel_trades.py --channel "jiang jun" --period 2026-09

``--channel`` 给渠道编号，或渠道名称（忽略大小写、空格和标点）。``--period`` 可以给几次，
不给就是全部月份。文件写到 ``output/``（不进仓库：里面是客户资料）。

为什么（2026-10-02）：渠道要「交易时间、交易金额、交易费用、客户名称、UID」的明细来核对佣金。

两张工作表：

  · 交易明细：看板上这个渠道客户的每一笔，和月结同一套规则 —— 升级 AI 之前的交易标「不计」，
    不算佣金。「交易费用」是佣金基数（总收入 opt+现货+合约），「交易金额」是总交易额。
  · 按月汇总：每月合计和应付佣金（月结算法：月合计 × 比例，四舍五入到分，负数按 0），
    旁边放结算表上记的数，两边一致才发出去。逐笔佣金各自四舍五入，加起来可能和月合计差几分，
    以月合计为准。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.ai_status import eligibility_of  # noqa: E402
from crm_basebot.domain.commission import (  # noqa: E402
    CommissionRow,
    Referral,
    day_of,
    period_of,
    trade_counts,
)
from crm_basebot.domain.settlement import is_live  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import (  # noqa: E402
    PrecisionLossError,
    extract_text,
    link_ids,
    to_number,
    to_uid,
)
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

CENTS = Decimal("0.01")
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


@dataclass
class Trade:
    day: date
    period: str
    client_name: str
    uid: str
    volume: Decimal
    fee: Decimal
    counts: bool


def norm(text: str) -> str:
    return re.sub(r"[^0-9A-Z一-鿿]", "", (text or "").upper())


def _uid(value) -> str:
    try:
        return to_uid(value)
    except PrecisionLossError:
        return ""


def _decimal(value) -> Decimal:
    number = to_number(value)
    return Decimal(str(number)) if number is not None else Decimal("0")


def find_channel(referrals: list[Referral], query: str) -> list[Referral]:
    """编号完全一样的优先；其次名称一样；再其次名称包含。"""
    key = norm(query)
    for test in (
        lambda r: norm(r.no) == key,
        lambda r: norm(r.name) == key,
        lambda r: key and key in norm(r.name),
    ):
        hits = [r for r in referrals if test(r)]
        if hits:
            return hits
    return []


def load_referrals(bitable, table_id) -> list[Referral]:
    referrals = []
    for record in bitable.iter_records(table_id):
        no = extract_text(record.fields.get(schema.REFERRAL_NO)).strip()
        if not no:
            continue
        referrals.append(
            Referral(
                record_id=record.record_id,
                no=no,
                name=extract_text(record.fields.get(schema.REFERRAL_NAME)).strip(),
                rate_percent=Decimal(str(to_number(record.fields.get(schema.REFERRAL_RATE)) or 0)),
                status=extract_text(record.fields.get(schema.REFERRAL_STATUS)),
            )
        )
    return referrals


def collect_trades(bitable, settings, referral: Referral, periods: set[str]) -> list[Trade]:
    tz = ZoneInfo(settings.business_timezone)
    clients: dict[str, tuple[str, object]] = {}  # UID -> (客户名称, AI 资格)
    for record in bitable.iter_records(settings.table_client):
        if referral.record_id not in link_ids(record.fields.get(schema.CLIENT_REFERRAL_LINK)):
            continue
        uid = _uid(record.fields.get(schema.CLIENT_UID))
        if uid:
            name = extract_text(record.fields.get(schema.CLIENT_NAME)).strip()
            clients[uid] = (name, eligibility_of(record.fields, tz=tz))

    trades = []
    for record in bitable.iter_records(settings.table_daily_board):
        fields = record.fields
        uid = _uid(fields.get(schema.BOARD_CLIENT_UID))
        if uid not in clients:
            continue
        order_time = fields.get(schema.BOARD_ORDER_DATE)
        period = period_of(order_time, tz=tz)
        day = day_of(order_time, tz=tz)
        if not period or day is None or (periods and period not in periods):
            continue
        if to_number(fields.get(schema.BOARD_TOTAL_REVENUE)) is None:
            continue  # 月结也跳过没有收入数的行
        name, eligibility = clients[uid]
        trades.append(
            Trade(
                day=day,
                period=period,
                client_name=name or extract_text(fields.get(schema.BOARD_CLIENT_NAME)).strip(),
                uid=uid,
                volume=_decimal(fields.get(schema.BOARD_TOTAL_VOLUME)),
                fee=_decimal(fields.get(schema.BOARD_TOTAL_REVENUE)),
                counts=trade_counts(eligibility, order_time, tz=tz),
            )
        )
    trades.sort(key=lambda t: (t.day, t.client_name, t.uid))
    return trades


def monthly(trades: list[Trade], referral: Referral) -> dict[str, CommissionRow]:
    """和 CommissionCalculator.compute 同一套：只有计入的交易进合计。"""
    rows: dict[str, CommissionRow] = {}
    for trade in trades:
        if not trade.counts:
            continue
        row = rows.setdefault(
            trade.period,
            CommissionRow(
                period=trade.period,
                referral_no=referral.no,
                referral_name=referral.name,
                rate_percent=referral.rate_percent,
            ),
        )
        row.revenue_total += trade.fee
        row.txn_count += 1
        row.client_uids.add(trade.uid)
    return rows


def settled_amounts(bitable, table_id, referral_no: str) -> dict[str, tuple[Decimal, str]]:
    """结算表上这个渠道每月的应付佣金和状态。"""
    found: dict[str, tuple[Decimal, str]] = {}
    for record in bitable.iter_records(table_id):
        fields = record.fields
        if extract_text(fields.get(schema.COMM_REFERRAL_NO)).strip() != referral_no:
            continue
        period = extract_text(fields.get(schema.COMM_PERIOD)).strip()
        status = schema.SETTLE_LIVE if is_live(fields) else schema.SETTLE_DONE
        found[period] = (_decimal(fields.get(schema.COMM_PAYABLE)), status)
    return found


def write_workbook(path: Path, referral: Referral, trades, rows, settled) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    money = "#,##0.00"
    bold = Font(bold=True)
    rate = referral.rate_percent

    book = Workbook()
    sheet = book.active
    sheet.title = "交易明细"
    sheet.append([f"{referral.no} {referral.name}　分佣比例 {rate.normalize():f}%"])
    sheet["A1"].font = bold
    header = ["交易日期", "月份", "客户名称", "UID", "交易金额", "交易费用", "计入佣金", "佣金"]
    sheet.append(header)
    for cell in sheet[2]:
        cell.font = bold
    for trade in trades:
        commission = (
            (trade.fee * rate / 100).quantize(CENTS, rounding=ROUND_HALF_UP)
            if trade.counts
            else Decimal("0")
        )
        sheet.append(
            [
                trade.day,
                trade.period,
                trade.client_name,
                trade.uid,
                float(trade.volume),
                float(trade.fee),
                "是" if trade.counts else "否（升级 AI 之前）",
                float(commission),
            ]
        )
    for row in sheet.iter_rows(min_row=3):
        row[0].number_format = "yyyy-mm-dd"
        row[3].number_format = "@"  # UID 当文本，19 位存成数字会丢精度
        for cell in (row[4], row[5], row[7]):
            cell.number_format = money
    for column, width in zip("ABCDEFGH", (12, 9, 28, 22, 16, 14, 18, 12), strict=True):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A3"

    summary = book.create_sheet("按月汇总")
    summary.append(
        [
            "月份",
            "笔数（计入）",
            "客户数",
            "交易金额",
            "交易费用（计入）",
            "分佣比例",
            "应付佣金",
            "结算表上的数",
            "状态",
        ]
    )
    for cell in summary[1]:
        cell.font = bold
    volume_by_period: dict[str, Decimal] = defaultdict(Decimal)
    for trade in trades:
        if trade.counts:
            volume_by_period[trade.period] += trade.volume
    for period in sorted(set(rows) | {t.period for t in trades}):
        row = rows.get(period)
        on_table, status = settled.get(period, (None, "还没写"))
        summary.append(
            [
                period,
                row.txn_count if row else 0,
                row.client_count if row else 0,
                float(volume_by_period[period]),
                float(row.revenue_total) if row else 0.0,
                f"{rate.normalize():f}%",
                float(row.payable) if row else 0.0,
                float(on_table) if on_table is not None else None,
                status,
            ]
        )
    for row in summary.iter_rows(min_row=2):
        for cell in (row[3], row[4], row[6], row[7]):
            cell.number_format = money
    for column, width in zip("ABCDEFGHI", (9, 12, 8, 16, 16, 9, 12, 14, 8), strict=True):
        summary.column_dimensions[column].width = width

    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出一个渠道的逐笔交易佣金")
    parser.add_argument("--channel", required=True, help="渠道编号或渠道名称")
    parser.add_argument("--period", action="append", default=[], help="YYYY-MM，可给几次")
    parser.add_argument("--out", help="输出路径（默认 output/渠道编号_交易明细.xlsx）")
    args = parser.parse_args(argv)

    settings = load_settings()
    require_settings(
        settings,
        "LARK_BASE_APP_TOKEN",
        "TABLE_REFERRAL",
        "TABLE_CLIENT",
        "TABLE_DAILY_BOARD",
        "TABLE_COMMISSION",
    )
    bitable = BitableClient(settings.base_app_token)

    hits = find_channel(load_referrals(bitable, settings.table_referral), args.channel)
    if len(hits) != 1:
        print(f"「{args.channel}」对上 {len(hits)} 个渠道，换个写法（用渠道编号最准）：")
        for referral in hits:
            print(f"   {referral.no} {referral.name}")
        return 1
    referral = hits[0]

    trades = collect_trades(bitable, settings, referral, set(args.period))
    rows = monthly(trades, referral)
    settled = settled_amounts(bitable, settings.table_commission, referral.no)

    suffix = "_".join(sorted(args.period)) or "全部月份"
    path = Path(args.out) if args.out else OUTPUT_DIR / f"{referral.no}_交易明细_{suffix}.xlsx"
    write_workbook(path, referral, trades, rows, settled)

    print(f"{referral.no} {referral.name}　分佣比例 {referral.rate_percent.normalize():f}%")
    print(f"共 {len(trades)} 笔，其中 {sum(t.counts for t in trades)} 笔计入佣金")
    for period in sorted(set(rows) | {t.period for t in trades}):
        row = rows.get(period)
        mine = row.payable if row else Decimal("0")
        on_table, status = settled.get(period, (None, "还没写"))
        if on_table is None:
            mark = "结算表上没有"
        elif on_table == mine:
            mark = f"和结算表一致（{status}）"
        else:
            mark = f"⚠️ 结算表上是 {on_table:,.2f}（{status}）"
        print(f"   {period}：应付 {mine:,.2f}　{mark}")
    print(f"\n已存：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
