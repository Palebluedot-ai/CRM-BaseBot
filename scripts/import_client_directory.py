#!/usr/bin/env python
"""把「全量 UID」那份导出做成 Base 里的一张表（客户名录，只读参考用）。

用途：给人查「这个客户的 UID 是多少」。它**不参与佣金计算** —— 算钱的链路只认
客户表的「客户UID」和看板的「用户ID」，这张表谁都不读。所以这里的原则是
**忠实镜像**：列名、值、行数都照导出的样子，不做加工。

    uv run python scripts/import_client_directory.py --file "全量UID.xlsx"
    uv run python scripts/import_client_directory.py --file "全量UID.xlsx" --apply
    uv run python scripts/import_client_directory.py --file "新的一份.xlsx" --apply --refresh

三件和别处不一样的取舍，都是「这是镜像不是算钱输入」推出来的：

1. ``user_id`` 建成**文本**字段。18-19 位存成数字会在服务端就被 float64 抹平低位，
   而这张表的全部价值就是那串数字准不准。
2. **坏掉的 UID 照样写进去**，只在输出里大声报出来。别处（backfill_client_uids.py）
   是宁可不写 —— 那里 UID 是 join key，写错就把钱记到别人头上。这里不一样：镜像少
   几行会让人以为名录是完整的，那更糟。谁要用这张表，得知道哪几行不可信。
3. 不删列、不删表。``--refresh`` 只清记录不动结构。

⚠️ 建完表要去 Base 的高级权限确认一次：这是**整个台子**一千多个客户的名单，
销售那个受限角色不该看得到。新建的表在高级权限里的默认可见性要自己确认。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 复用登记导入那套 UID 读法：浮点数直接报错，损坏形态判得出来。
import import_registrations as reg  # noqa: E402

from crm_basebot.domain.dates import date_to_ms  # noqa: E402
from crm_basebot.lark.bitable import (  # noqa: E402
    FIELD_TYPE_DATETIME,
    FIELD_TYPE_TEXT,
    BitableClient,
)
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402
from crm_basebot.structure import StructureError, create_field, create_table  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_TABLE_NAME = "Client Directory"

# 开了高级权限之后，应用能做什么由角色决定。读写既有表的角色**不含建表建列**，
# 于是建表会以 1254302 被拒。这个码从平台回来只有一句 RolePermNotAllow，看不出
# 该去点哪里，所以在这里翻成可执行的话。
_ROLE_PERM_ERROR_CODE = "1254302"


def _is_role_perm_error(exc: Exception) -> bool:
    text = str(exc)
    return _ROLE_PERM_ERROR_CODE in text or "RolePermNotAllow" in text


_ROLE_PERM_HELP = """
建表/建列被 Base 的高级权限拒了（1254302 RolePermNotAllow）。
应用能读写现有的表，但它那个角色没有建表的权力。两条路，二选一：

  路 A（推荐，改一次权限）
    Base 右上角 ⋯ -> 更多 -> 添加应用 -> CRM-BaseBot -> 权限改成【可管理】
    然后重跑这条命令。跑完把它改回【可编辑】。

  路 B（不动应用权限，人手建表）
    在 Base 里手工新增一张表，命名「{table}」，列不用建。
    然后重跑这条命令 —— 脚本会把缺的列补齐再写数据。
    （如果建列也被拒，就得走路 A。）

两条路跑完都要做同一件事：去高级权限把「{table}」对销售那个受限角色设成【无权限】。
这是整个台子的客户名单。
"""

# 列名照抄导出的表头 —— 导出长什么样，Base 就长什么样，拿着 xlsx 能在 Base 里
# 找到同一列。类型全部保守：ID 类一律文本，只有日期是日期。
COLUMNS: dict[str, int] = {
    "org_id": FIELD_TYPE_TEXT,
    "user_id": FIELD_TYPE_TEXT,
    "client_name": FIELD_TYPE_TEXT,
    "type": FIELD_TYPE_TEXT,
    "kyc_date": FIELD_TYPE_DATETIME,
}
UID_COLUMN = "user_id"
NAME_COLUMN = "client_name"


def _cell_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def parse_sheet(path: Path, sheet_name: str | None) -> tuple[list[dict[str, Any]], list[str]]:
    """xlsx -> [{列名: 值}]，外加警告。

    **不能用 openpyxl 的 read_only 模式。** 飞书表格导出的 xlsx 里
    ``<dimension ref="A1">`` 是错的（实测：1032 行的表只声明了一格），read_only 信
    这个声明，只读到表头就停，看起来像「文件是空的」。普通模式自己扫完整张表。
    """
    if not path.exists():
        raise reg.RegistrationImportError(f"找不到文件：{path}")

    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True)
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    try:
        sheet = (
            reg._find_sheet(workbook, sheet_name)
            if sheet_name
            else workbook[workbook.sheetnames[0]]
        )
        rows = reg._rows(sheet)
        if len(rows) < 2:
            raise reg.RegistrationImportError(f"工作表「{sheet.title}」没有数据行")

        header = [reg._norm_header(cell) for cell in rows[0]]
        missing = [name for name in (UID_COLUMN, NAME_COLUMN) if name not in header]
        if missing:
            raise reg.RegistrationImportError(
                f"工作表「{sheet.title}」缺少这些列：{missing}；"
                f"实际表头：{[c for c in rows[0] if c]}"
            )
        index = {name: header.index(name) for name in COLUMNS if name in header}
        ignored = [c for c in rows[0] if c and reg._norm_header(c) not in COLUMNS]
        if ignored:
            warnings.append(f"这几列不认识，没有导入：{ignored}")

        for row_num, row in enumerate(rows[1:], start=2):
            record: dict[str, Any] = {}
            for name, col in index.items():
                raw = row[col] if col < len(row) else None
                if COLUMNS[name] is FIELD_TYPE_DATETIME:
                    if isinstance(raw, datetime):
                        record[name] = date_to_ms(raw.date(), tz=ZoneInfo("UTC"))
                    elif isinstance(raw, date):
                        record[name] = date_to_ms(raw, tz=ZoneInfo("UTC"))
                    continue
                text = _cell_text(raw)
                if text:
                    record[name] = text

            uid = record.get(UID_COLUMN, "")
            if uid:
                reason = reg._damaged_uid_reason(uid)
                if reason:
                    # 照样写，只报出来 —— 见模块开头第 2 条。
                    warnings.append(
                        f"第 {row_num} 行「{record.get(NAME_COLUMN, '')}」的 UID {uid} {reason}"
                    )
            if any(record.values()):
                out.append(record)
    finally:
        workbook.close()

    return out, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把全量 UID 导出做成 Base 里的一张表")
    parser.add_argument("--file", required=True, help="全量 UID 的 xlsx")
    parser.add_argument("--sheet", help="工作表名，默认第一张")
    parser.add_argument("--table-name", default=DEFAULT_TABLE_NAME, help="Base 里的表名")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="表已存在时，先清掉它现有的全部记录再写（只清记录，不动表结构）",
    )
    parser.add_argument("--apply", action="store_true", help="真写；不加则只预演")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN")
    bitable = BitableClient(settings.base_app_token)

    records, warnings = parse_sheet(Path(args.file), args.sheet)
    print(f"读取 {args.file}：{len(records)} 行")
    for line in warnings:
        print(f"  ⚠️ {line}")
    if warnings:
        print(
            "  以上这些行照样会写进名录（这张表是镜像，不参与算钱），"
            "但用它查 UID 的人需要知道哪几行不可信 —— 请把这几个客户名转给对方重新导出。"
        )

    existing = {t.name: t.table_id for t in bitable.list_tables()}
    table_id = existing.get(args.table_name)

    if table_id and not args.refresh:
        print(
            f"\nBase 里已经有一张叫「{args.table_name}」的表（{table_id}）。"
            f"\n要用新的一份盖掉它现有的记录，加 --refresh；"
            f"\n要另开一张，用 --table-name 换个名字。",
            file=sys.stderr,
        )
        return 1

    if not args.apply:
        if table_id:
            count = sum(1 for _ in bitable.iter_records(table_id, field_names=[UID_COLUMN]))
            print(f"\n预演：会清掉「{args.table_name}」现有的 {count} 条，再写 {len(records)} 条。")
        else:
            print(f"\n预演：会新建表「{args.table_name}」，加这几列：")
            for name, type_code in COLUMNS.items():
                kind = "日期" if type_code is FIELD_TYPE_DATETIME else "文本"
                note = "  ← 必须是文本，18-19 位存成数字会丢精度" if name == UID_COLUMN else ""
                print(f"    {name:<14}{kind}{note}")
            print(f"  然后写入 {len(records)} 条。")
        print("\n没有写 Base。确认无误后加 --apply。")
        return 0

    client = get_client()

    if table_id is None:
        try:
            table_id = create_table(client, settings.base_app_token, args.table_name)
        except StructureError as exc:
            if not _is_role_perm_error(exc):
                raise
            print(_ROLE_PERM_HELP.format(table=args.table_name), file=sys.stderr)
            return 1
        print(f"\n已建表「{args.table_name}」table_id={table_id}")
    else:
        stale = [r.record_id for r in bitable.iter_records(table_id, field_names=[UID_COLUMN])]
        deleted = bitable.batch_delete_records(table_id, stale)
        print(f"\n清掉「{args.table_name}」现有的 {deleted} 条")

    # 无论表是脚本建的还是人在界面上建的，缺的列都在这里补齐 —— 人手建的表通常
    # 只有平台自带的主字段，缺列的话下面写记录会被平台以「未知字段」整批拒掉。
    have = {f.name for f in bitable.list_fields(table_id)}
    for name, type_code in COLUMNS.items():
        if name in have:
            continue
        try:
            create_field(client, settings.base_app_token, table_id, name, type_code)
        except StructureError as exc:
            if not _is_role_perm_error(exc):
                raise
            print(_ROLE_PERM_HELP.format(table=args.table_name), file=sys.stderr)
            return 1
        print(f"  + 列 {name}")

    # 主字段是平台建表时自带的那一列，我们没法指定它的名字。把客户名也写进去，
    # 否则界面上每一行的第一格都是空的，整张表看起来像「无标题记录」。
    primary = bitable.resolve_primary_field(table_id)
    payloads = []
    for record in records:
        fields = dict(record)
        if primary.name not in fields and record.get(NAME_COLUMN):
            fields[primary.name] = record[NAME_COLUMN]
        payloads.append(fields)

    written = bitable.batch_create_records(table_id, payloads)
    print(f"写入 {written} 条。")
    print(
        f"\n⚠️ 最后一步：去 Base 的高级权限确认「{args.table_name}」对销售那个受限角色是"
        "\n【无权限】。这是整个台子的客户名单，不是某一个销售该看到的东西。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
