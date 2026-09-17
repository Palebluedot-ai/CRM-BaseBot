"""内存版 Bitable 假件。

模拟三件真实行为，因为业务逻辑正是围着它们写的：
  - 自动编号字段由「服务端」生成，API 写不进去
  - 写入后要回读才拿得到自动编号
  - 记录以 dict 形式存取
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from crm_basebot.domain import schema
from crm_basebot.lark.bitable import FieldInfo, Record


class FakeTable:
    def __init__(
        self,
        auto_number_field: str | None = None,
        start_at: int = 0,
        primary_field: str = "文本",
    ):
        self.auto_number_field = auto_number_field
        # 主字段名字。真机上建表若不指定 fields，飞书会塞一个默认叫「文本」的
        # 主字段 —— 这里跟着这个默认，测试里也能显式改。
        self.primary_field = primary_field
        self.records: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._serial = itertools.count(start_at + 1)
        # 被完整遍历过几次。真实 API 下每次遍历都是一串分页请求，
        # 所以有测试拿它来钉住「不许多读一遍表」。
        self.scan_count = 0
        # list_fields 会返回的字段清单。默认空，要过 assert_fields_present 的测试自己填。
        self.fields: list[FieldInfo] = []

    def add_existing(self, fields: dict[str, Any]) -> str:
        record_id = f"rec{next(self._ids):04d}"
        self.records[record_id] = dict(fields)
        return record_id


class FakeBitable:
    """只实现 BitableClient 里被领域代码用到的那部分。"""

    def __init__(self) -> None:
        self.tables: dict[str, FakeTable] = {}
        self.write_count = 0
        # 每次 create_record 拿到的**原始** fields，写入格式的断言靠它。
        # 存 (table_id, fields)，因为「哪张表收到什么」本身就是要钉住的事。
        self.writes: list[tuple[str, dict[str, Any]]] = []
        # 真实 API 下 reread=True 会多一个 get_record 往返，这里如实计数，
        # 让「别在 3 秒回调里白白多读一次」这条约束能被测到。
        self.read_back_count = 0
        # 被删掉的 (table_id, record_id)，按删除顺序。删除是最该被盯住的写操作。
        self.deleted: list[tuple[str, str]] = []
        # 批量写每发一次记一笔 (table_id, 这一批的条数)，钉住「按批发、每批多大」。
        self.batch_create_calls: list[tuple[str, int]] = []
        self.batch_delete_calls: list[tuple[str, int]] = []
        # 记录 update_record 的调用，便于测试断言主字段回填的实际写入
        self.updates: list[tuple[str, str, dict[str, Any]]] = []

    def table(self, table_id: str) -> FakeTable:
        return self.tables.setdefault(table_id, FakeTable())

    def resolve_primary_field(self, table_id: str) -> FieldInfo:
        primary_name = self.table(table_id).primary_field
        return FieldInfo(
            field_id=f"fld_primary_{table_id}",
            name=primary_name,
            type=1,
            ui_type="Text",
            is_primary=True,
        )

    def update_record(self, table_id: str, record_id: str, fields: dict[str, Any]) -> Record:
        self.updates.append((table_id, record_id, dict(fields)))
        stored = self.table(table_id).records[record_id]
        stored.update(fields)
        return Record(record_id=record_id, fields=dict(stored))

    def iter_records(self, table_id: str, **kwargs):
        table = self.table(table_id)
        table.scan_count += 1
        for record_id, fields in list(table.records.items()):
            yield Record(record_id=record_id, fields=dict(fields))

    def list_fields(self, table_id: str) -> list[FieldInfo]:
        return list(self.table(table_id).fields)

    def delete_record(self, table_id: str, record_id: str) -> None:
        del self.table(table_id).records[record_id]
        self.deleted.append((table_id, record_id))

    def batch_create_records(
        self, table_id: str, records: list[dict[str, Any]], *, batch_size: int = 500
    ) -> int:
        for start in range(0, len(records), batch_size):
            chunk = records[start : start + batch_size]
            self.batch_create_calls.append((table_id, len(chunk)))
            for fields in chunk:
                self.write_count += 1
                self.writes.append((table_id, dict(fields)))
                self.table(table_id).add_existing(dict(fields))
        return len(records)

    def batch_delete_records(
        self, table_id: str, record_ids: list[str], *, batch_size: int = 500
    ) -> int:
        for start in range(0, len(record_ids), batch_size):
            chunk = record_ids[start : start + batch_size]
            self.batch_delete_calls.append((table_id, len(chunk)))
            for record_id in chunk:
                self.delete_record(table_id, record_id)
        return len(record_ids)

    def get_record(self, table_id: str, record_id: str) -> Record:
        return Record(record_id=record_id, fields=dict(self.table(table_id).records[record_id]))

    def create_record(
        self, table_id: str, fields: dict[str, Any], *, reread: bool = True
    ) -> Record:
        self.write_count += 1
        self.writes.append((table_id, dict(fields)))
        table = self.table(table_id)

        stored = dict(fields)
        if table.auto_number_field:
            # 服务端生成，忽略调用方传进来的任何值
            stored[table.auto_number_field] = f"R{next(table._serial):03d}"

        record_id = table.add_existing(stored)

        if not reread:
            # 真实接口的 create 响应只回显你写进去的字段，自动编号不保证在里面
            return Record(record_id=record_id, fields=dict(fields))

        self.read_back_count += 1
        return self.get_record(table_id, record_id)


TBL_REFERRAL = "tblReferral"
TBL_CLIENT = "tblClient"
TBL_AUDIT = "tblAudit"
TBL_SALES = "tblSales"
TBL_BOARD = "tblBoard"
TBL_COMMISSION = "tblCommission"


@pytest.fixture
def fake_bitable():
    bitable = FakeBitable()
    bitable.tables[TBL_REFERRAL] = FakeTable(auto_number_field=schema.REFERRAL_NO, start_at=0)
    bitable.tables[TBL_CLIENT] = FakeTable()
    bitable.tables[TBL_AUDIT] = FakeTable()
    bitable.tables[TBL_SALES] = FakeTable()
    bitable.tables[TBL_BOARD] = FakeTable()
    bitable.tables[TBL_COMMISSION] = FakeTable()
    return bitable
