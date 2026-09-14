#!/usr/bin/env python
"""把每天的销售收入日读看板 xlsx 导入到 Base 的 Daily Revenue Board 表。

## 为什么必须走 xlsx 不走 CSV

CSV 是纯文本，看起来对 18-19 位客户UID 更安全。但**上游导出**这一步几乎总是过
Excel —— Excel 只保留 15 位有效数字，UID 的低位被抹成 0 之后再存成 CSV，字符
本身就已经是错的，下游修不了。

xlsx 底层是每个单元格保留原生类型和显示格式。只要「user_id」这一列在源头就被
明确标成文本格式，openpyxl 能拿到字符串形态的原值，全链路不经过 float。这个
脚本对 user_id 列做强制字符串校验：拿到浮点数直接报错而不是凑合。

## 幂等策略

看板每天更新，脚本会被反复跑。策略是**按交易日期区间幂等**：

    · 默认：读取 xlsx 里出现的所有日期，先删 Base 上这些日期的记录，再写新的
    · --date YYYY-MM-DD：只处理这一天（xlsx 里其他日期的行忽略）
    · --dry-run：只统计不写，验证映射对不对

「先删后写」而不是「upsert」，是因为一天的数据没有稳定的行主键（同一 user_id
同一天可能只有一行，也可能有多行的历史修正），按行 upsert 会漏改重复。按日期
批量替换是最简单也最安全的语义。

## 用法

    uv run python scripts/import_daily_board.py                # 用 .env 的默认路径全量导
    uv run python scripts/import_daily_board.py --file X.xlsx  # 指定文件
    uv run python scripts/import_daily_board.py --date DATE    # 只导这一天 (YYYY-MM-DD)
    uv run python scripts/import_daily_board.py --dry-run      # 只算不写

放到 crontab 里每天跑：

    0 8 * * * cd /path/to/CRM-BaseBot-main && uv run python scripts/import_daily_board.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import PrecisionLossError, extract_text, to_uid  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)

# xlsx 表头 -> Base 字段名。左侧是**看板 xlsx** 里的表头（用户给的），右侧是 Base
# 字段名（schema.py 定义）。跨列改名的两个：user_id -> 客户UID，client_name -> 客户名称
# —— 为的是和客户表用同一个 join 键，机器人查询也少一层字段映射。
COLUMN_MAP: dict[str, str] = {
    "站点": schema.BOARD_STATION,
    "user_id": schema.BOARD_CLIENT_UID,
    "客户UID": schema.BOARD_CLIENT_UID,  # 有的导出里已经中文化了，两种都认
    "client_name": schema.BOARD_CLIENT_NAME,
    "客户名称": schema.BOARD_CLIENT_NAME,
    "销售分组": schema.BOARD_SALES_GROUP,
    "销售": schema.BOARD_SALES_NAME,
    "交易日期": schema.BOARD_ORDER_DATE,
    "总收入（opt+现货）": schema.BOARD_TOTAL_REVENUE,
    "总收入(opt+现货)": schema.BOARD_TOTAL_REVENUE,
    "总收入": schema.BOARD_TOTAL_REVENUE,
    "opt手续费": schema.BOARD_OPT_FEE,
    "现货手续费剔除做市商": schema.BOARD_SPOT_FEE_EX_MM,
    "opt_pnl": schema.BOARD_OPT_PNL,
}

# 必须在 xlsx 里出现的表头（映射到这四个 Base 字段就算齐了）。少一个不行 ——
# 佣金计算依赖 客户UID + 交易日期 + 总收入。
REQUIRED_BASE_FIELDS = {
    schema.BOARD_CLIENT_UID,
    schema.BOARD_ORDER_DATE,
    schema.BOARD_TOTAL_REVENUE,
    schema.BOARD_CLIENT_NAME,
}

NUMBER_FIELDS = {
    schema.BOARD_TOTAL_REVENUE,
    schema.BOARD_OPT_FEE,
    schema.BOARD_SPOT_FEE_EX_MM,
    schema.BOARD_OPT_PNL,
}


@dataclass(frozen=True)
class BoardRow:
    order_date: date
    fields: dict[str, Any]


class BoardImportError(RuntimeError):
    """xlsx 结构或值不符合要求。"""


def _load_workbook(path: Path):
    """惰性 import openpyxl，避免让主进程启动就吃这个依赖。"""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise BoardImportError(
            "缺 openpyxl，先跑 `uv sync` 装齐依赖再试。"
        ) from exc

    if not path.exists():
        raise BoardImportError(f"找不到文件：{path}")
    # data_only=True 让公式单元格返回上次计算的值，而不是公式本身
    return load_workbook(path, data_only=True, read_only=True)


def _read_header(sheet) -> tuple[list[str], list[str]]:
    """读第一行表头，返回 (原始表头, 映射后的 Base 字段名)。

    未识别的列头**不报错**，只忽略并 warn —— 上游可能加了不影响佣金的辅助列。
    但必填的四个字段有一个缺，就算错，因为下游没法算了。
    """
    first_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
    if first_row is None:
        raise BoardImportError("xlsx 是空的，连表头都没有")

    raw_headers = [str(cell).strip() if cell is not None else "" for cell in first_row]
    mapped = [COLUMN_MAP.get(h, "") for h in raw_headers]

    seen = {m for m in mapped if m}
    missing = REQUIRED_BASE_FIELDS - seen
    if missing:
        raise BoardImportError(
            f"xlsx 表头缺少必填列：{sorted(missing)}\n"
            f"实际表头：{raw_headers}\n"
            f"支持的表头别名见 scripts/import_daily_board.py 的 COLUMN_MAP。"
        )

    unknown = [raw_headers[i] for i, m in enumerate(mapped) if not m and raw_headers[i]]
    if unknown:
        logger.warning("忽略未识别的列：%s", unknown)

    return raw_headers, mapped


def _cell_to_uid(value: Any, *, row_num: int) -> str:
    """user_id 列的值。任何浮点形态都当作精度已损伤，直接拒绝。

    看板 xlsx 里 user_id 应该是文本格式；如果拿到 float，就是源头把它当数字了 ——
    18-19 位数字过一遍 float 就丢低位，下游对不上。让这里就炸而不是继续。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        raise BoardImportError(
            f"第 {row_num} 行的 user_id 是浮点数 {value!r}，"
            "说明源头把它当数字存了。请让上游把 user_id 那一列在 xlsx 里设成"
            "「文本」格式重新导出，否则 18-19 位 UID 的低位已经被抹成 0。"
        )
    if isinstance(value, int):
        return str(value)
    # 字符串：交给 to_uid 做常规校验（会剥空白）
    try:
        return to_uid(value)
    except PrecisionLossError as exc:
        raise BoardImportError(f"第 {row_num} 行的 user_id 无法安全转字符串：{exc}") from exc


def _cell_to_date(value: Any, *, row_num: int) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        # 只取日期部分（用空格切一下，扔掉可能跟着的时分秒），再规范化分隔符
        text = value.strip().split(" ", 1)[0].replace("/", "-")
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            pass
    raise BoardImportError(f"第 {row_num} 行的交易日期无法解析：{value!r}")


def _cell_to_number(value: Any, field_name: str, *, row_num: int) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，得先挡
        raise BoardImportError(f"第 {row_num} 行的「{field_name}」是布尔值 {value!r}")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError as exc:
            raise BoardImportError(
                f"第 {row_num} 行的「{field_name}」不是数字：{value!r}"
            ) from exc
    raise BoardImportError(f"第 {row_num} 行的「{field_name}」无法转数字：{value!r}")


def parse_workbook(path: Path, *, only_date: date | None = None) -> list[BoardRow]:
    """把 xlsx 读成 BoardRow 列表。所有校验都在这里做。

    ``only_date`` 传了就只保留那一天的行，其他丢弃。
    """
    workbook = _load_workbook(path)
    sheet = workbook.active
    _, mapped_headers = _read_header(sheet)

    rows: list[BoardRow] = []
    for index, raw_row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        if all(cell is None or cell == "" for cell in raw_row):
            continue  # 空行

        fields: dict[str, Any] = {}
        for cell, base_field in zip(raw_row, mapped_headers, strict=False):
            if not base_field:
                continue

            if base_field == schema.BOARD_CLIENT_UID:
                fields[base_field] = _cell_to_uid(cell, row_num=index)
            elif base_field == schema.BOARD_ORDER_DATE:
                # 稍后再转，这里先留原值给外层用
                fields[base_field] = _cell_to_date(cell, row_num=index)
            elif base_field in NUMBER_FIELDS:
                number = _cell_to_number(cell, base_field, row_num=index)
                if number is not None:
                    fields[base_field] = number
            else:
                text = extract_text(cell)
                if text:
                    fields[base_field] = text

        # 必填字段缺一个就丢这一行（比 raise 温和：可能有个别脏数据不该拖垮整份导入）
        if not all(fields.get(f) for f in REQUIRED_BASE_FIELDS - {schema.BOARD_CLIENT_NAME}):
            logger.warning("第 %d 行必填字段缺失，跳过：%s", index, fields)
            continue

        order_date: date = fields[schema.BOARD_ORDER_DATE]
        if only_date is not None and order_date != only_date:
            continue

        rows.append(BoardRow(order_date=order_date, fields=fields))

    workbook.close()
    return rows


def _to_timestamp_ms(d: date) -> int:
    """交易日期按 UTC 零点存成毫秒时间戳（Bitable 日期字段要求）。"""
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def _existing_by_date(bitable: BitableClient, table_id: str) -> dict[date, list[str]]:
    """扫一遍 Base 表，按交易日期分组 record_id，供「先删」阶段用。"""
    grouped: dict[date, list[str]] = defaultdict(list)
    for record in bitable.iter_records(table_id, field_names=[schema.BOARD_ORDER_DATE]):
        raw = record.fields.get(schema.BOARD_ORDER_DATE)
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            d = datetime.fromtimestamp(float(raw) / 1000, tz=UTC).date()
            grouped[d].append(record.record_id)
    return grouped


def _apply(bitable: BitableClient, table_id: str, rows: list[BoardRow]) -> tuple[int, int]:
    """先删涉及日期的旧记录，再写新记录。返回 (删除数, 写入数)。"""
    affected_dates = {r.order_date for r in rows}
    existing = _existing_by_date(bitable, table_id)

    deleted = 0
    for d in affected_dates:
        for record_id in existing.get(d, []):
            bitable.delete_record(table_id, record_id)
            deleted += 1

    written = 0
    for row in rows:
        payload = dict(row.fields)
        payload[schema.BOARD_ORDER_DATE] = _to_timestamp_ms(row.order_date)
        # 汇总类表没有需要读回来的系统字段，不用 reread 省一个往返
        bitable.create_record(table_id, payload, reread=False)
        written += 1

    return deleted, written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把销售收入日读看板 xlsx 导入 Base")
    parser.add_argument(
        "--file",
        help="xlsx 文件路径。不传就用 .env 里的 DAILY_BOARD_XLSX",
    )
    parser.add_argument(
        "--date",
        help="只导这一天 YYYY-MM-DD。xlsx 里其他日期的行忽略",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读只算，不写 Base。用来验证列映射对不对",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_DAILY_BOARD")

    xlsx_path = Path(args.file or settings.daily_board_xlsx or "")
    if not xlsx_path or str(xlsx_path) == ".":
        print(
            "没指定 xlsx：加 --file /path/to.xlsx，或在 .env 里设 DAILY_BOARD_XLSX。",
            file=sys.stderr,
        )
        return 1

    only_date: date | None = None
    if args.date:
        try:
            only_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"--date 格式要是 YYYY-MM-DD，你给的是 {args.date!r}", file=sys.stderr)
            return 1

    print(f"读取：{xlsx_path}")
    if only_date:
        print(f"只导：{only_date}")

    try:
        rows = parse_workbook(xlsx_path, only_date=only_date)
    except BoardImportError as exc:
        print(f"\n导入失败：{exc}", file=sys.stderr)
        return 1

    if not rows:
        print("没有匹配的行可导入。")
        return 0

    by_date: dict[date, int] = defaultdict(int)
    for r in rows:
        by_date[r.order_date] += 1
    print(f"\n{len(rows)} 行，覆盖 {len(by_date)} 天：")
    for d in sorted(by_date):
        print(f"  {d}  {by_date[d]:>4} 行")

    if args.dry_run:
        print("\n--dry-run：不写 Base。去掉这个开关才会真正导入。")
        return 0

    bitable = BitableClient(settings.base_app_token)
    print("\n先删涉及日期的旧记录，再写新记录…")
    deleted, written = _apply(bitable, settings.table_daily_board, rows)
    print(f"删除 {deleted} 条，写入 {written} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
