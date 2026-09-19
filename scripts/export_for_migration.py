#!/usr/bin/env python
"""把源 Base 的渠道/客户/看板导出成**可以直接导入的 xlsx** —— 不交换凭证的交接办法。

迁移有两条路：

    A 凭证迁移   scripts/migrate_base.py      需要目标端凭证，一条命令搬完（推荐）
    C 文件交接   本脚本 + 现成的两个导入脚本   不交换任何凭证，只传一个文件

方案 C 的用法（两边各一条命令）：

    # 源端（你这边）：导出
    uv run python scripts/export_for_migration.py --out out/handover.xlsx --board-out out/board.xlsx

    # 目标端（同事那边）：导入
    uv run python scripts/import_registrations.py --file out/handover.xlsx --apply
    uv run python scripts/import_daily_board.py --file out/board.xlsx --apply

导出的表头**照抄现成导入脚本认的那套**（渠道/客户那两份是模板 xlsx 的形状，看板那 18 列
就是 Base 的列名），所以不需要任何改造，也不需要目标端有源端凭证。

## 两个要注意的

1. **导出文件含真实客户数据。** ``out/`` 和 ``*.xlsx`` 都在 .gitignore 里，别手工提交。
   发给同事走内部渠道（飞书/邮件），和平时发销售明细一样的规矩。
2. **看板导出会很大**（1,600+ 行 × 18 列）而且每天在变。只补历史才用它；正常走增量。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openpyxl import Workbook  # noqa: E402

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.dates import ms_to_date  # noqa: E402
from crm_basebot.lark.bitable import FIELD_TYPE_DATETIME, BitableClient  # noqa: E402
from crm_basebot.lark.values import extract_text, link_ids, to_number  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)

SHEET_REFERRALS = "Referral Registration"
SHEET_CLIENTS = "Referred Clients"
SHEET_UIDS = "用户UID"

# 表头照抄 import_registrations.py 认的那套（它按规整后的小写匹配）。
REFERRAL_HEADERS = [
    "Referral Code",
    "Name",
    "Email",
    "Start Date",
    "Commission Rate",
    "Payout Frequency",
    "Submitted On",
    "Sales In Charge",
]
CLIENT_HEADERS = ["Referral Code", "Client Name", "UID", "Sales In Charge"]
UID_HEADERS = ["UID", "客户名称"]

# 看板导出用 Base 的列名 —— import_daily_board.py 的表头契约就是它们。
BOARD_HEADERS = list(schema.DAILY_BOARD_FIELDS)

# 看板里的日期列（不止「交易日期」，还有 KYC日期）。按 schema 的类型码推，别写死列名 ——
# 漏一个就会导出成毫秒数字，导入端报「无法解析成日期」。
BOARD_DATE_COLUMNS = {
    name
    for name, type_code in schema.DAILY_BOARD_FIELDS.items()
    if type_code == FIELD_TYPE_DATETIME
}


def _as_date(value: Any, tz: tzinfo) -> datetime | None:
    """Bitable 日期是毫秒时间戳；导出成 datetime 让导入端按日期认。"""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.combine(ms_to_date(int(value), tz=tz), datetime.min.time())
    return None


def referral_rows(records: list[Any], *, tz: tzinfo) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for record in records:
        fields = record.fields
        rows.append(
            [
                extract_text(fields.get(schema.REFERRAL_NO)),
                extract_text(fields.get(schema.REFERRAL_NAME)),
                extract_text(fields.get(schema.REFERRAL_EMAIL)),
                _as_date(fields.get(schema.REFERRAL_START_DATE), tz),
                to_number(fields.get(schema.REFERRAL_RATE)),
                extract_text(fields.get(schema.REFERRAL_PAYOUT)),
                _as_date(fields.get(schema.REFERRAL_SUBMITTED_ON), tz),
                extract_text(fields.get(schema.REFERRAL_SALES_NAME)),
            ]
        )
    return rows


def client_rows(
    records: list[Any],
    *,
    channel_no_by_id: dict[str, str],
) -> list[list[Any]]:
    """客户行的「Referral Code」要从关联（record_id）翻回渠道编号 —— 导入端只认编号。"""
    rows: list[list[Any]] = []
    for record in records:
        fields = record.fields
        ids = link_ids(fields.get(schema.CLIENT_REFERRAL_LINK))
        code = channel_no_by_id.get(ids[0], "") if ids else ""
        rows.append(
            [
                code,
                extract_text(fields.get(schema.CLIENT_NAME)),
                extract_text(fields.get(schema.CLIENT_UID)),
                extract_text(fields.get(schema.CLIENT_SALES_NAME)),
            ]
        )
    return rows


def _flat(value: Any) -> Any:
    """文本列从接口读回来是富文本片段（``[{'text': ..., 'type': 'text'}]``），
    openpyxl 写不了这种结构 —— 摊平成字符串。数字/日期原样留着。"""
    if isinstance(value, list | dict):
        return extract_text(value)
    return value


def board_rows(records: list[Any], *, tz: tzinfo) -> list[list[Any]]:
    """18 列原样导出（派生列不在 DAILY_BOARD_FIELDS 里，天然不会被导出）。

    日期列要转成**真日期**：接口给的是毫秒时间戳，直接写进 xlsx 会变成一串数字，
    导入端按日期读就会读成 1970 年。
    """
    rows: list[list[Any]] = []
    for record in records:
        fields = record.fields
        row: list[Any] = []
        for name in BOARD_HEADERS:
            value = fields.get(name)
            if name in BOARD_DATE_COLUMNS:
                row.append(_as_date(value, tz) or _flat(value))
            else:
                row.append(_flat(value))
        rows.append(row)
    return rows


def _write_sheet(
    book: Workbook, title: str, headers: list[str], rows: list[list[Any]], first: bool
):
    sheet = book.active if first else book.create_sheet()
    sheet.title = title
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    return sheet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出渠道/客户（可选看板）成可直接导入的 xlsx")
    parser.add_argument("--out", required=True, help="渠道+客户的导出路径，例如 out/handover.xlsx")
    parser.add_argument("--board-out", help="额外导出看板（18 列），例如 out/board.xlsx")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_REFERRAL", "TABLE_CLIENT")
    bitable = BitableClient(settings.base_app_token)
    tz = ZoneInfo(settings.business_timezone)

    referral_records = list(bitable.iter_records(settings.table_referral))
    channel_no_by_id = {
        record.record_id: extract_text(record.fields.get(schema.REFERRAL_NO))
        for record in referral_records
    }
    client_records = list(bitable.iter_records(settings.table_client))

    referrals = referral_rows(referral_records, tz=tz)
    clients = client_rows(client_records, channel_no_by_id=channel_no_by_id)

    book = Workbook()
    _write_sheet(book, SHEET_REFERRALS, REFERRAL_HEADERS, referrals, first=True)
    _write_sheet(book, SHEET_CLIENTS, CLIENT_HEADERS, clients, first=False)
    _write_sheet(
        book,
        SHEET_UIDS,
        UID_HEADERS,
        [[row[2], row[1]] for row in clients if row[2]],
        first=False,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    book.save(out)
    print(f"渠道 {len(referrals)} 条、客户 {len(clients)} 条 → {out}")
    print(f'  （目标端：uv run python scripts/import_registrations.py --file "{out}" --apply）')

    if args.board_out:
        board = board_rows(list(bitable.iter_records(settings.table_daily_board)), tz=tz)
        board_book = Workbook()
        _write_sheet(board_book, "交易明细", BOARD_HEADERS, board, first=True)
        board_path = Path(args.board_out)
        board_path.parent.mkdir(parents=True, exist_ok=True)
        board_book.save(board_path)
        print(f"看板 {len(board)} 行 → {board_path}")
        print(
            "  （目标端：uv run python scripts/import_daily_board.py --file "
            f'"{board_path}" --apply）'
        )

    print("\n提醒：这两个文件含真实客户数据，别提交进 git；发同事走内部渠道。")

    # 既没 UID 又没渠道编号的客户：导入端只能按「编号+客户名」匹配，两条都没有就没法匹配，
    # 重复导入会堆出重复行。而且它们本来也算不出佣金（没有所属渠道）。点出来给人看。
    homeless = [row for row in clients if not row[0] and not row[2]]
    if homeless:
        print(
            f"\n有 {len(homeless)} 条客户既没 UID 也没渠道编号，"
            "导入端无法识别它们（重复跑会重复）："
        )
        for row in homeless[:5]:
            print(f"  · {row[1] or '(无名)'}")
        if len(homeless) > 5:
            print(f"  · …另外 {len(homeless) - 5} 条")
        print("  这些客户没有所属渠道，本来也算不出佣金；建议先在源端补上渠道再导出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
