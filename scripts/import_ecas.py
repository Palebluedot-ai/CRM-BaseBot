#!/usr/bin/env python
"""把 ECAS 申请表导进 Base，建成独立于交易佣金的第二套账。

    uv run python scripts/import_ecas.py --file "Wallet_and_Trades_ECAS.xlsx"
    uv run python scripts/import_ecas.py --file "..." --apply
    uv run python scripts/import_ecas.py --file "新的一份.xlsx" --apply --refresh

来源是「Wallet and Trades」那份表里的 ``ECAS`` 分页 —— 一行一笔开户申请，
有介绍人的那些带着介绍人名字、比例和该付的返佣。

## 它和交易佣金的关系：**没有关系**

建出来的 ``ECAS Applications`` 不参与交易佣金的任何一步，交易那三张表也不被这里读。
唯一的交集是关联到 ``Referral Information`` —— 而且只为了拿**编号和名字**，
不为了拿比例。ECAS 的比例逐行来自这份表，理由见 ``domain/ecas.py`` 开头。

## 整张表替换，不做增量

来源表是单一事实来源，每次导入就是拿它的现状盖掉 Base 里的现状。一笔申请没有行主键
（同一个客户可以申请多次，金额时间都可能一样），拼一个出来只会在两边不一致时骗自己。
158 行重写一遍很便宜。表里已经有记录时必须显式加 ``--refresh``。

## 会让整次导入停下来的事

**比例判读不出来**（见 ``resolve_rate_percent``）。这时候不是跳过那一行继续 ——
跳过会让镜像少一笔、合计悄悄变小，而 ECAS 的价值就在于「合计对得上来源表」。
宁可整次拒绝，让人去看那一行。

**Referrer 栏填成「Referrer」**这种没填好的行例外：它不是判读失败，是资料缺失。
申请本身照样镜像进去，只是不带介绍人，并且单独列出来让人去补。
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import import_registrations as reg  # noqa: E402

from crm_basebot.domain import ecas, schema  # noqa: E402
from crm_basebot.domain.names import norm, tokens  # noqa: E402
from crm_basebot.lark.bitable import FIELD_TYPE_FORMULA, BitableClient  # noqa: E402
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.lark.values import extract_text  # noqa: E402
from crm_basebot.startup import load_settings, require_settings, set_env_value  # noqa: E402
from crm_basebot.structure import StructureError, create_field, create_table  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_SHEET = "ECAS"

# 来源表的表头，逐字照抄。少一列直接拒绝 —— 猜列位比报错危险得多。
COL_CLIENT = "Client Name"
COL_AMOUNT = "ECAS Revenue"
COL_TIME = "Application Time"
COL_SALES = "Sales in Charge"
COL_UID = "UID"
COL_REFERRER = "Referrer"
COL_RATE = "%"
COL_FEE = "Amount of Referral Fee"

REQUIRED_COLUMNS = (COL_CLIENT, COL_AMOUNT, COL_TIME, COL_REFERRER, COL_RATE, COL_FEE)

# Referrer 栏填成栏位标题的那种行。不是渠道名，是没填好的资料。
BAD_REFERRER = {"", "REFERRER"}

# 建表/建列被高级权限拒掉时的提示，和 import_client_directory.py 同一套说法。
_ROLE_PERM_ERROR_CODE = "1254302"

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
"""


class EcasImportError(RuntimeError):
    """导入停下来了。消息可以直接打印给人看。"""


def _is_role_perm_error(exc: Exception) -> bool:
    text = str(exc)
    return _ROLE_PERM_ERROR_CODE in text or "RolePermNotAllow" in text


def _decimal(value: Any, *, where: str, what: str) -> Decimal:
    """单元格 -> Decimal。来源表里这几栏是**文本形态的数字**，所以先转字符串。"""
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise EcasImportError(f"{where}的{what}「{value}」不是数字") from exc


@dataclass
class Row:
    """来源表的一行，已经判读完但还没挂上渠道。"""

    row_num: int
    client_name: str
    amount: Decimal
    applied_at: datetime
    sales: str
    uid: str
    referrer: str
    rate_percent: Decimal | None
    stated_fee: Decimal


@dataclass
class Parsed:
    rows: list[Row]
    warnings: list[str]


def parse_sheet(path: Path, sheet_name: str) -> Parsed:
    """ECAS 分页 -> 行。比例在这里就判读掉，判不出来直接抛。

    **不能用 openpyxl 的 read_only 模式。** 飞书导出的 xlsx 里 ``<dimension>`` 是错的
    （实测：1032 行的表只声明了一格），read_only 信它，只读到表头就停。
    """
    if not path.exists():
        raise EcasImportError(f"找不到文件：{path}")

    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True)
    warnings: list[str] = []
    rows: list[Row] = []
    rate_errors: list[str] = []
    try:
        sheet = reg._find_sheet(workbook, sheet_name)
        raw_rows = reg._rows(sheet)
        if len(raw_rows) < 2:
            raise EcasImportError(f"工作表「{sheet.title}」没有数据行")

        header = [reg._norm_header(cell) for cell in raw_rows[0]]
        index = {}
        for column in REQUIRED_COLUMNS + (COL_SALES, COL_UID):
            key = reg._norm_header(column)
            if key in header:
                index[column] = header.index(key)
        missing = [c for c in REQUIRED_COLUMNS if c not in index]
        if missing:
            raise EcasImportError(
                f"工作表「{sheet.title}」缺少这些列：{missing}；"
                f"实际表头：{[c for c in raw_rows[0] if c]}"
            )

        def cell(row: tuple, column: str) -> Any:
            position = index.get(column)
            if position is None or position >= len(row):
                return None
            return row[position]

        for row_num, raw in enumerate(raw_rows[1:], start=2):
            client_name = reg._clean_text(cell(raw, COL_CLIENT))
            if not client_name:
                continue
            where = f"第 {row_num} 行「{client_name}」"

            applied_raw = cell(raw, COL_TIME)
            if isinstance(applied_raw, datetime):
                applied_at = applied_raw
            elif isinstance(applied_raw, date):
                applied_at = datetime(applied_raw.year, applied_raw.month, applied_raw.day)
            else:
                raise EcasImportError(
                    f"{where}的申请时间「{applied_raw}」不是日期 —— 没有时间就归不了月"
                )

            amount = _decimal(cell(raw, COL_AMOUNT), where=where, what="ECAS 金额")
            stated_fee = _decimal(cell(raw, COL_FEE), where=where, what="返佣金额")

            uid = ""
            raw_uid = cell(raw, COL_UID)
            if raw_uid not in (None, ""):
                uid = reg._cell_to_uid(raw_uid, where=where)
                reason = reg._damaged_uid_reason(uid)
                if reason:
                    # 留空而不是写进去：这一栏只是给人查的参考，一个看不出坏掉的
                    # 坏 UID 比一个空格危险得多。
                    warnings.append(f"{where}的 UID {uid} {reason} —— 这一格留空了")
                    uid = ""

            referrer = reg._clean_text(cell(raw, COL_REFERRER))
            rate_percent: Decimal | None = None
            if norm(referrer) in BAD_REFERRER:
                if referrer:
                    warnings.append(
                        f"{where}的 Referrer 栏填的是「{referrer}」，不是渠道名 —— "
                        f"这笔申请照样导入，但不算返佣（表里写着 {stated_fee}）"
                    )
                referrer = ""
            else:
                raw_rate = cell(raw, COL_RATE)
                if raw_rate in (None, ""):
                    warnings.append(f"{where}有介绍人「{referrer}」但没填比例 —— 这笔不算返佣")
                    referrer = ""
                else:
                    try:
                        rate_percent = ecas.resolve_rate_percent(
                            amount,
                            _decimal(raw_rate, where=where, what="比例"),
                            stated_fee,
                            where=f"{where}：",
                        )
                    except ecas.EcasRateError as exc:
                        rate_errors.append(str(exc))
                        continue

            rows.append(
                Row(
                    row_num=row_num,
                    client_name=client_name,
                    amount=amount,
                    applied_at=applied_at,
                    sales=reg._clean_text(cell(raw, COL_SALES)),
                    uid=uid,
                    referrer=referrer,
                    rate_percent=rate_percent,
                    stated_fee=stated_fee,
                )
            )
    finally:
        workbook.close()

    if rate_errors:
        detail = "\n  ".join(rate_errors)
        raise EcasImportError(
            f"有 {len(rate_errors)} 行的比例判读不出来，整次导入没有进行"
            f"（见模块开头的说明）：\n  {detail}"
        )

    return Parsed(rows=rows, warnings=warnings)


def load_referrals(bitable: BitableClient, table_id: str) -> tuple[dict, dict]:
    """渠道表 -> (按规整名, 按顺序无关的名) 两张索引，值是 (record_id, 编号, 名称)。"""
    by_name: dict[str, tuple[str, str, str]] = {}
    by_tokens: dict[str, tuple[str, str, str]] = {}
    for record in bitable.iter_records(
        table_id, field_names=[schema.REFERRAL_NO, schema.REFERRAL_NAME]
    ):
        name = extract_text(record.fields.get(schema.REFERRAL_NAME))
        if not name:
            continue
        code = extract_text(record.fields.get(schema.REFERRAL_NO))
        entry = (record.record_id, code, name)
        by_name.setdefault(norm(name), entry)
        by_tokens.setdefault(tokens(name), entry)
    return by_name, by_tokens


def ensure_table(
    bitable: BitableClient,
    client,
    app_token: str,
    table_name: str,
    fields: dict[str, int],
    *,
    link_table_id: str = "",
    link_field: str = "",
    formulas: dict[str, tuple[str, int]] | None = None,
) -> str:
    """表和列都对齐到 ``fields``。已有的不碰，缺的补上。

    人手建出来的表通常只有平台自带的主字段，缺列的话写记录会被整批拒掉 ——
    所以补列这一步不管表是谁建的都要跑。
    """
    existing = {t.name: t.table_id for t in bitable.list_tables()}
    table_id = existing.get(table_name)
    if table_id is None:
        table_id = create_table(client, app_token, table_name)
        print(f"已建表「{table_name}」table_id={table_id}")

    have = {f.name for f in bitable.list_fields(table_id)}
    for name, type_code in fields.items():
        if name in have:
            continue
        create_field(
            client,
            app_token,
            table_id,
            name,
            type_code,
            link_table_id=link_table_id if name == link_field else None,
            formula=(formulas or {}).get(name),
        )
        print(f"  + 列 {name}")
    return table_id


def build_payload(
    row: Row, referral_record_id: str, primary_name: str, tz: ZoneInfo
) -> dict[str, Any]:
    """一行 -> 写进 Base 的字段。公式列不能写，平台会拒。"""
    fields: dict[str, Any] = {
        ecas.ECAS_CLIENT_NAME: row.client_name,
        ecas.ECAS_AMOUNT: float(row.amount),
        ecas.ECAS_APPLIED_AT: int(row.applied_at.replace(tzinfo=tz).timestamp() * 1000),
    }
    if row.sales:
        fields[ecas.ECAS_SALES_NAME] = row.sales
    if row.uid:
        fields[ecas.ECAS_CLIENT_UID] = row.uid
    if row.referrer:
        fields[ecas.ECAS_REFERRER_NAME] = row.referrer
    if referral_record_id:
        fields[ecas.ECAS_REFERRAL_LINK] = [referral_record_id]
    if row.rate_percent is not None:
        fields[ecas.ECAS_RATE] = float(row.rate_percent)
    # 主字段是平台建表时自带的那一列，名字没法指定。把客户名也写进去，
    # 否则界面上每行第一格都是空的，整张表看起来像「无标题记录」。
    if primary_name not in fields:
        fields[primary_name] = row.client_name
    return {k: v for k, v in fields.items() if ecas.ECAS_FIELDS.get(k) != FIELD_TYPE_FORMULA}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 ECAS 申请表导进 Base")
    parser.add_argument("--file", required=True, help="Wallet and Trades 那份 xlsx")
    parser.add_argument("--sheet", default=DEFAULT_SHEET, help=f"分页名，默认 {DEFAULT_SHEET}")
    parser.add_argument("--table-name", default=ecas.TABLE_ECAS_NAME, help="Base 里的表名")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="表里已经有记录时，先清掉再整张重写（只清记录，不动表结构）",
    )
    parser.add_argument("--apply", action="store_true", help="真写；不加则只预演")
    parser.add_argument(
        "--env",
        default=".env",
        help="环境文件，默认 .env；--apply 时会把两张 ECAS 表的 table_id 写回这个文件",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_REFERRAL")
    bitable = BitableClient(settings.base_app_token)
    tz = ZoneInfo(settings.business_timezone)

    try:
        parsed = parse_sheet(Path(args.file), args.sheet)
    except (EcasImportError, reg.RegistrationImportError) as exc:
        print(f"\n没有导入：{exc}", file=sys.stderr)
        return 1

    with_referrer = [r for r in parsed.rows if r.referrer and r.rate_percent is not None]
    fee_total = sum((r.stated_fee for r in with_referrer), Decimal("0"))
    print(f"读取 {args.file} 的「{args.sheet}」分页：{len(parsed.rows)} 笔申请")
    print(f"  其中有介绍人的 {len(with_referrer)} 笔，来源表写的返佣合计 {fee_total:,.2f}")
    for line in parsed.warnings:
        print(f"  ⚠️ {line}")

    # ---------- 挂渠道 ----------
    by_name, by_tokens = load_referrals(bitable, settings.table_referral)
    matched: dict[int, str] = {}
    loose: list[str] = []
    unmatched: dict[str, int] = {}
    for row in with_referrer:
        entry = by_name.get(norm(row.referrer))
        if entry is None:
            entry = by_tokens.get(tokens(row.referrer))
            if entry is not None:
                loose.append(f"「{row.referrer}」-> {entry[1]} {entry[2]}")
        if entry is None:
            unmatched[row.referrer] = unmatched.get(row.referrer, 0) + 1
            continue
        matched[row.row_num] = entry[0]

    if loose:
        print(f"\n这 {len(loose)} 处是靠姓名顛倒/标点才对上的渠道，请过目：")
        for line in sorted(set(loose)):
            print(f"  {line}")
    if unmatched:
        owed = sum((r.stated_fee for r in with_referrer if r.referrer in unmatched), Decimal("0"))
        print(f"\n⚠️ 渠道表里找不到这 {len(unmatched)} 个介绍人，合计 {owed:,.2f} 的返佣：")
        for name, count in sorted(unmatched.items(), key=lambda kv: -kv[1]):
            print(f"  {name}（{count} 笔）")
        print(
            "  这些申请照样会导入、返佣照样算 —— 钱是欠着的，藏起来只会让合计对不上来源表。"
            "\n  汇总里它们的渠道编号是空的。请到渠道表把这些渠道登记了，再重跑一次导入。"
        )

    # ---------- 预演 / 写入 ----------
    existing = {t.name: t.table_id for t in bitable.list_tables()}
    table_id = existing.get(args.table_name)
    stale_count = 0
    if table_id:
        stale_count = sum(
            1 for _ in bitable.iter_records(table_id, field_names=[ecas.ECAS_CLIENT_NAME])
        )
        if stale_count and not args.refresh:
            print(
                f"\n「{args.table_name}」里已经有 {stale_count} 条记录。"
                f"\n这个脚本是整张表替换，不做增量 —— 要用这份文件盖掉它，加 --refresh。",
                file=sys.stderr,
            )
            return 1

    if not args.apply:
        if table_id:
            print(
                f"\n预演：会清掉「{args.table_name}」现有的 {stale_count} 条，"
                f"再写 {len(parsed.rows)} 条。"
            )
        else:
            print(f"\n预演：会新建表「{args.table_name}」，加这几列：")
            for name in ecas.ECAS_FIELDS:
                mark = "  ← 公式" if name in ecas.ECAS_FORMULAS else ""
                if name == ecas.ECAS_REFERRAL_LINK:
                    mark = f"  ← 关联到「{schema.TABLE_REFERRAL_NAME}」"
                print(f"    {name}{mark}")
            print(f"  然后写入 {len(parsed.rows)} 条。")
        print(f"  挂上渠道的 {len(matched)} 笔，挂不上的 {len(with_referrer) - len(matched)} 笔。")
        print("\n没有写 Base。确认无误后加 --apply。")
        return 0

    client = get_client()
    try:
        table_id = ensure_table(
            bitable,
            client,
            settings.base_app_token,
            args.table_name,
            ecas.ECAS_FIELDS,
            link_table_id=settings.table_referral,
            link_field=ecas.ECAS_REFERRAL_LINK,
            formulas=ecas.ECAS_FORMULAS,
        )
        # 汇总表一起建出来。分两条命令的话，人跑完导入以为装好了，
        # 到月底结算才发现少一张表 —— 那时候才建，帐还得重跑一次。
        summary_table_id = ensure_table(
            bitable,
            client,
            settings.base_app_token,
            ecas.TABLE_ECAS_COMMISSION_NAME,
            ecas.ECAS_COMMISSION_FIELDS,
        )
    except StructureError as exc:
        if not _is_role_perm_error(exc):
            raise
        print(_ROLE_PERM_HELP.format(table=args.table_name), file=sys.stderr)
        return 1

    # 回填 table_id，别让人去界面上一个个抄 —— 抄错一位的报错是「table not found」，
    # 完全指不到真正的错处。
    for key, value in (
        ("TABLE_ECAS", table_id),
        ("TABLE_ECAS_COMMISSION", summary_table_id),
    ):
        set_env_value(args.env, key, value)
    print(f"已把 TABLE_ECAS / TABLE_ECAS_COMMISSION 写回 {args.env}")

    if stale_count:
        ids = [
            r.record_id for r in bitable.iter_records(table_id, field_names=[ecas.ECAS_CLIENT_NAME])
        ]
        deleted = bitable.batch_delete_records(table_id, ids)
        print(f"清掉「{args.table_name}」现有的 {deleted} 条")

    primary = bitable.resolve_primary_field(table_id)
    payloads = [
        build_payload(row, matched.get(row.row_num, ""), primary.name, tz) for row in parsed.rows
    ]
    written = bitable.batch_create_records(table_id, payloads)
    print(f"写入 {written} 条。")
    print(
        "\n下一步：\n"
        "  uv run python -m crm_basebot.jobs.ecas_reconcile --all-periods\n"
        f"\n⚠️ 还要去 Base 的高级权限确认「{args.table_name}」对销售那个受限角色的可见性 ——"
        "\n这张表里是整个台子的开户申请，不是某一个销售该看到的东西。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
