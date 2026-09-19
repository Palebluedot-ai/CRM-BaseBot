"""把解析好的行写进 Base 的看板表 —— 唯一碰 Base 写接口的地方。

两件事，都是为了正确性而不是性能：

1. **按日先删后写。** 同一天可能被重导（导出每天更新），而没有稳定的行主键（同用户
   同一天可能有多行），按行 upsert 会漏改。所以按日期整批替换。
2. **按 UID 挂「客户」关联。** 多维表格的公式没有 VLOOKUP / LOOKUP，跨表取值只有
   「关联字段 + 公式引用」一条路，而关联必须由写入方建立。这里只做**匹配**，一个数
   都不算 —— 算钱在 Base 的公式里。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, tzinfo
from typing import Any

from ..domain import schema
from ..domain.dates import date_to_ms, ms_to_date
from ..lark.bitable import BitableClient
from ..lark.values import to_uid
from .export import DATE_FIELDS, BoardRow

# 看板表里那几个「派生列」由 Base 公式维护，写入时绝不能出现在 payload 里
# （只读字段混进去整条记录都写不进去）。
DERIVED_COLUMNS = frozenset(schema.DAILY_BOARD_DERIVED_FIELDS)


def _existing_by_date(
    bitable: BitableClient, table_id: str, *, tz: tzinfo
) -> dict[date, list[str]]:
    """扫一遍 Base 表，按交易日期分组 record_id，供「先删」阶段用。

    日期按业务时区取：界面里手工填的「9 月 10 日」是新加坡零点，按 UTC 取会成 9 月 9 日，
    重导 9 月 10 日时就删不掉它。
    """
    grouped: dict[date, list[str]] = defaultdict(list)
    for record in bitable.iter_records(table_id, field_names=[schema.BOARD_ORDER_DATE]):
        raw = record.fields.get(schema.BOARD_ORDER_DATE)
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            grouped[ms_to_date(raw, tz=tz)].append(record.record_id)
    return grouped


def existing_dates(bitable: BitableClient, table_id: str, *, tz: tzinfo) -> set[date]:
    """看板里已经有数据的交易日集合（增量导入用它算「哪些天是新的」）。

    只挑「交易日期」一列扫，比全字段读省得多；日期按业务时区取，和写入口径一致。
    """
    return set(_existing_by_date(bitable, table_id, tz=tz))


def to_payload(row: BoardRow, *, tz: tzinfo, client_links: dict[str, str]) -> dict[str, Any]:
    """一行写进 Base 的字段：日期列换成业务时区那天零点的毫秒时间戳，其余原样。

    ``client_links`` 是「客户UID -> 客户记录 id」。命中的行顺手把「客户」关联挂上 ——
    Base 里那几列公式（渠道编号 / 渠道名称 / 分佣比例 / 本笔佣金）全靠这个关联反查。
    挂不上的行（用户ID 不在客户表里）留空，公式自然也是空的。
    """
    payload: dict[str, Any] = {
        column: date_to_ms(value, tz=tz) if column in DATE_FIELDS else value
        for column, value in row.fields.items()
        if column not in DERIVED_COLUMNS
    }
    uid = payload.get(schema.BOARD_CLIENT_UID)
    record_id = client_links.get(str(uid)) if uid else None
    if record_id:
        payload[schema.BOARD_CLIENT_LINK] = [record_id]
    return payload


def client_links(bitable: BitableClient, table_id: str) -> dict[str, str]:
    """客户UID -> 客户记录 id。

    多维表格的公式没有 VLOOKUP / LOOKUP，跨表取值只有「关联字段 + 公式引用」一条路，
    而关联必须由写入方建立。所以「按 UID 找到是哪条客户记录」这一步只能在导入时做，
    公式负责它后面的取数和乘法（2026-09-18 定的）。

    UID 走 ``to_uid`` 而不是 extract_text：客户表那一列若被存成了数字，值已经不可信，
    这时候**报错停下**比悄悄挂错客户要好 —— 挂错了佣金就记到别人头上，且没有任何提示。
    """
    links: dict[str, str] = {}
    for record in bitable.iter_records(table_id, field_names=[schema.CLIENT_UID]):
        uid = to_uid(record.fields.get(schema.CLIENT_UID))
        if uid:
            # 同一个 UID 挂在多条客户记录上时只认第一条：客户表本身按 UID 去重，
            # 真出现重复行是登记侧的问题，不该在这里选一个「更对」的出来。
            links.setdefault(uid, record.record_id)
    return links


def apply_import(
    bitable: BitableClient,
    table_id: str,
    rows: list[BoardRow],
    *,
    tz: tzinfo,
    replace_dates: Iterable[date] | None = None,
    client_links_map: dict[str, str] | None = None,
) -> tuple[int, int]:
    """先删要替换的日期上的旧记录，再写新记录，都按批发。返回 (删除数, 写入数)。

    ``replace_dates`` 是要整天替换的日期，不传就取 rows 覆盖到的日期。
    导入时传导出覆盖到的全部日期：某天导出里只有其他站点的行，这天的旧记录也要删掉。
    ``tz`` 是业务时区：日期列写成那一天在业务时区的零点，删旧行也按同一时区取日期。
    ``client_links_map`` 是「客户UID -> 客户记录 id」，命中的行会挂上「客户」关联。
    """
    if replace_dates is None:
        replace_dates = {row.order_date for row in rows}
    days = sorted(set(replace_dates))
    existing = _existing_by_date(bitable, table_id, tz=tz)
    stale = [record_id for day in days for record_id in existing.get(day, [])]

    deleted = bitable.batch_delete_records(table_id, stale)
    links = client_links_map or {}
    written = bitable.batch_create_records(
        table_id, [to_payload(row, tz=tz, client_links=links) for row in rows]
    )
    return deleted, written


# 旧名字：scripts/import_daily_board.py 原来叫 _apply，测试与调用方还在用。
_apply = apply_import
_CLIENT_LINKS = client_links
