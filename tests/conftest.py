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
from crm_basebot.lark.bitable import Record


class FakeTable:
    def __init__(self, auto_number_field: str | None = None, start_at: int = 0):
        self.auto_number_field = auto_number_field
        self.records: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._serial = itertools.count(start_at + 1)

    def add_existing(self, fields: dict[str, Any]) -> str:
        record_id = f"rec{next(self._ids):04d}"
        self.records[record_id] = dict(fields)
        return record_id


class FakeBitable:
    """只实现 BitableClient 里被领域代码用到的那部分。"""

    def __init__(self) -> None:
        self.tables: dict[str, FakeTable] = {}
        self.write_count = 0

    def table(self, table_id: str) -> FakeTable:
        return self.tables.setdefault(table_id, FakeTable())

    def iter_records(self, table_id: str, **kwargs):
        for record_id, fields in list(self.table(table_id).records.items()):
            yield Record(record_id=record_id, fields=dict(fields))

    def get_record(self, table_id: str, record_id: str) -> Record:
        return Record(record_id=record_id, fields=dict(self.table(table_id).records[record_id]))

    def create_record(self, table_id: str, fields: dict[str, Any]) -> Record:
        self.write_count += 1
        table = self.table(table_id)

        stored = dict(fields)
        if table.auto_number_field:
            # 服务端生成，忽略调用方传进来的任何值
            stored[table.auto_number_field] = f"R{next(table._serial):03d}"

        record_id = table.add_existing(stored)
        return self.get_record(table_id, record_id)


TBL_REFERRAL = "tblReferral"
TBL_CLIENT = "tblClient"
TBL_AUDIT = "tblAudit"
TBL_SALES = "tblSales"
TBL_TXN = "tblTxn"
TBL_COMMISSION = "tblCommission"


@pytest.fixture
def fake_bitable():
    bitable = FakeBitable()
    bitable.tables[TBL_REFERRAL] = FakeTable(auto_number_field=schema.REFERRAL_NO, start_at=0)
    bitable.tables[TBL_CLIENT] = FakeTable()
    bitable.tables[TBL_AUDIT] = FakeTable()
    bitable.tables[TBL_SALES] = FakeTable()
    bitable.tables[TBL_TXN] = FakeTable()
    bitable.tables[TBL_COMMISSION] = FakeTable()
    return bitable
