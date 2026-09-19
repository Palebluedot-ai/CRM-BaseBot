"""按表搬运记录：字段按**名字**映射，不按位置。

两个 Base 的表头和内容一模一样（同一个 schema 建出来的），所以搬运不需要映射表 ——
字段名就是映射。要处理的只有三件事：

1. **不搬只读列和公式列。** 公式列（渠道编号 / 渠道名称 / 分佣比例 / 本笔佣金 / 月份）
   在目标端由公式自己算；搬过去也写不进（只读字段混进 payload 整条记录都写不进去）。
   自动编号、创建时间/人这些系统列同理。

2. **关联要在目标端重建。** 关联字段存的是 record_id，两边的 id 完全不同，直接抄过去
   会指向空。所以必须借**业务键**重建：客户 → 渠道 用「渠道编号」，看板 → 客户 用
   「客户UID」。这也是为什么迁移要按 渠道 → 客户 → 看板 的顺序跑。

3. **人员字段的 open_id 不能跨应用搬。** open_id 是飞书按**应用**签发的，源端的
   open_id 到了目标端是无效值。所以人员列（归属销售）不搬，迁移完在目标端重新认领
   （登记人OpenID 留空 → 用 backfill_owners.py 按姓名回填，见迁移报告里的「还需人工」）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..lark.bitable import BitableClient
from ..lark.values import extract_text, link_ids, to_uid

logger = logging.getLogger(__name__)

# 人员字段里的 open_id 是**源应用**签发的，搬到目标端是无效值 —— 一律不搬。
# 目标端拿到人之后用 backfill_owners.py 按姓名重新认领。
TENANT_SCOPED_TYPES = frozenset({11})  # FIELD_TYPE_USER


@dataclass(frozen=True)
class CopySpec:
    """一张表怎么搬。"""

    label: str
    source_table_id: str
    target_table_id: str
    # 写入前把源记录加工成目标 payload（重建关联、丢开放 open_id 等）
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # 要跳过的字段名（源端或目标端是只读/公式/人员时自动加入，这里只放额外的）
    extra_skip: frozenset[str] = frozenset()


@dataclass
class CopyReport:
    label: str
    read: int = 0
    written: int = 0
    dropped_fields: set[str] = field(default_factory=set)

    def summary(self) -> str:
        dropped = (
            f"，未搬的列：{'、'.join(sorted(self.dropped_fields))}" if self.dropped_fields else ""
        )
        return f"{self.label}：读到 {self.read} 条，写入 {self.written} 条{dropped}"


def unwritable_fields(bitable: BitableClient, table_id: str) -> set[str]:
    """这张表里「不该出现在写入 payload 里」的字段名：只读列、公式列、人员列。"""
    skip: set[str] = set()
    for info in bitable.list_fields(table_id):
        if info.is_read_only or info.type in TENANT_SCOPED_TYPES:
            skip.add(info.name)
    return skip


def copy_records(
    *,
    source: BitableClient,
    target: BitableClient,
    spec: CopySpec,
    dry_run: bool = False,
) -> CopyReport:
    """把一张表的记录搬过去。``dry_run=True`` 时只读不写。"""
    report = CopyReport(label=spec.label)
    skip = unwritable_fields(source, spec.source_table_id) | unwritable_fields(
        target, spec.target_table_id
    )
    skip |= set(spec.extra_skip)
    report.dropped_fields = set(skip)

    payloads: list[dict[str, Any]] = []
    for record in source.iter_records(spec.source_table_id):
        report.read += 1
        fields = {name: value for name, value in record.fields.items() if name not in skip}
        if spec.transform is not None:
            fields = spec.transform(fields)
        payloads.append(fields)

    if dry_run or not payloads:
        return report

    # 按批发：目标端也是同一个 Bitable 接口，一次一条会把额度吃光。
    report.written = target.batch_create_records(spec.target_table_id, payloads)
    return report


def channel_index(bitable: BitableClient, table_id: str, *, id_field: str, key_field: str):
    """建「业务键 -> record_id」索引（例如 渠道编号 -> 记录 id），供关联重建用。

    ``id_field`` 那条记录在源端可能是空的（历史行），空键不进货 —— 否则会把一堆
    无键记录互相覆盖掉。
    """
    index: dict[str, str] = {}
    for record in bitable.iter_records(table_id, field_names=[key_field]):
        key = extract_text(record.fields.get(key_field))
        if key:
            index.setdefault(key.strip(), record.record_id)
    return index


def rebuild_link(
    *,
    fields: dict[str, Any],
    link_field: str,
    source_id_index: dict[str, str],
    source_key_by_id: dict[str, str],
    target_id_index: dict[str, str],
) -> None:
    """把源端的关联值（record_id）换成目标端的 record_id。

    走两步：源 record_id → 业务键 → 目标 record_id。中间那一步是业务键（渠道编号 /
    客户UID），所以两边表里的**业务键必须一致** —— 这也是迁移前要先核对的事。
    """
    ids = link_ids(fields.get(link_field))
    if not ids:
        fields.pop(link_field, None)
        return

    key = source_key_by_id.get(ids[0], "")
    target_id = target_id_index.get(key, "") if key else ""
    if target_id:
        fields[link_field] = [target_id]
    else:
        # 指向的那条记录在目标端还不存在（或业务键不一致）：宁可留空，挂一个错的更糟。
        logger.warning("关联重建失败：%s 指向的业务键「%s」在目标端找不到，留空", link_field, key)
        fields.pop(link_field, None)


def uid_by_record_id(bitable: BitableClient, table_id: str, uid_field: str) -> dict[str, str]:
    """客户表：记录 id -> UID。看板搬过去要按 UID 找目标端的客户记录。"""
    mapping: dict[str, str] = {}
    for record in bitable.iter_records(table_id, field_names=[uid_field]):
        uid = to_uid(record.fields.get(uid_field))
        if uid:
            mapping[record.record_id] = uid
    return mapping
