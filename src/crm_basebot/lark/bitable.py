"""Bitable 读写封装。

三件事在这里集中处理，别处不要绕过：

1. **写操作串行化。** Bitable 的写接口不支持并发，同一个 Base 上并发写会返回
   ``1254045 WriteConflict``。所有写都要拿 ``_WRITE_LOCK``。用锁而不是异步队列，
   是因为 SDK 的卡片回调处理器是同步调用的（且必须 3 秒内返回），同步锁能同时
   适配机器人回调和批处理脚本两种场景。这把锁顺带给编号递增提供了临界区。

2. **字段值规整。** 见 values.py —— 客户UID 必须全程字符串。

3. **schema 快照校验。** 交易明细表是同事每天手工导入维护的，字段随时可能被
   改名或改类型。算钱之前先比对快照，对不上就报错，而不是拿着错字段静默算。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppTableRecord,
    CreateAppTableRecordRequest,
    GetAppTableRecordRequest,
    ListAppTableFieldRequest,
    ListAppTableRequest,
    SearchAppTableRecordRequest,
    SearchAppTableRecordRequestBody,
)

from .client import get_client

logger = logging.getLogger(__name__)

# 同一进程内所有 Bitable 写操作的串行化闸门
_WRITE_LOCK = threading.RLock()

# Bitable 字段类型码，只列我们会碰到的
FIELD_TYPE_TEXT = 1
FIELD_TYPE_NUMBER = 2
FIELD_TYPE_SINGLE_SELECT = 3
FIELD_TYPE_DATETIME = 5
FIELD_TYPE_USER = 11
FIELD_TYPE_SINGLE_LINK = 18
FIELD_TYPE_LOOKUP = 19
FIELD_TYPE_FORMULA = 20
FIELD_TYPE_DUPLEX_LINK = 21
FIELD_TYPE_CREATED_TIME = 1001
FIELD_TYPE_AUTO_NUMBER = 1005

# 这些字段由系统维护，API 写入会被忽略或报错
READ_ONLY_FIELD_TYPES = frozenset(
    {
        FIELD_TYPE_LOOKUP,
        FIELD_TYPE_FORMULA,
        FIELD_TYPE_CREATED_TIME,
        1002,  # 最后更新时间
        1003,  # 创建人
        1004,  # 修改人
        FIELD_TYPE_AUTO_NUMBER,
        3001,  # 按钮
    }
)


class BitableError(RuntimeError):
    """Bitable API 调用失败。"""


class SchemaDriftError(RuntimeError):
    """表结构和快照对不上了。"""


@dataclass(frozen=True)
class FieldInfo:
    field_id: str
    name: str
    type: int
    ui_type: str
    is_primary: bool
    # 叫 props 而不是 property，否则会在类体内遮蔽掉内置的 property 装饰器
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def is_read_only(self) -> bool:
        return self.type in READ_ONLY_FIELD_TYPES


@dataclass(frozen=True)
class TableInfo:
    table_id: str
    name: str


@dataclass(frozen=True)
class Record:
    record_id: str
    fields: dict[str, Any]


def _check(response: Any, what: str) -> None:
    if not response.success():
        raise BitableError(
            f"{what} 失败: code={response.code} msg={response.msg} "
            f"log_id={getattr(response, 'get_log_id', lambda: '')()}"
        )


class BitableClient:
    """针对单个多维表格（app_token）的读写封装。"""

    def __init__(self, app_token: str, client: lark.Client | None = None) -> None:
        if not app_token:
            raise ValueError("app_token 为空 —— 检查 .env 里的 LARK_BASE_APP_TOKEN")
        self._app_token = app_token
        self._client = client or get_client()

    # ---------- 读 ----------

    def list_tables(self) -> list[TableInfo]:
        tables: list[TableInfo] = []
        page_token: str | None = None

        while True:
            builder = (
                ListAppTableRequest.builder().app_token(self._app_token).page_size(100)
            )
            if page_token:
                builder = builder.page_token(page_token)

            response = self._client.bitable.v1.app_table.list(builder.build())
            _check(response, "列出数据表")

            for item in response.data.items or []:
                tables.append(TableInfo(table_id=item.table_id, name=item.name))

            if not response.data.has_more:
                break
            page_token = response.data.page_token

        return tables

    def list_fields(self, table_id: str) -> list[FieldInfo]:
        fields: list[FieldInfo] = []
        page_token: str | None = None

        while True:
            builder = (
                ListAppTableFieldRequest.builder()
                .app_token(self._app_token)
                .table_id(table_id)
                .page_size(100)
            )
            if page_token:
                builder = builder.page_token(page_token)

            response = self._client.bitable.v1.app_table_field.list(builder.build())
            _check(response, f"列出字段 table_id={table_id}")

            for item in response.data.items or []:
                fields.append(
                    FieldInfo(
                        field_id=item.field_id,
                        name=item.field_name,
                        type=item.type,
                        ui_type=item.ui_type or "",
                        is_primary=bool(item.is_primary),
                        props=_to_plain_dict(item.property),
                    )
                )

            if not response.data.has_more:
                break
            page_token = response.data.page_token

        return fields

    def iter_records(
        self,
        table_id: str,
        *,
        page_size: int = 500,
        field_names: list[str] | None = None,
    ) -> Iterator[Record]:
        """遍历全表记录，自动翻页。"""
        page_token: str | None = None

        while True:
            body_builder = SearchAppTableRecordRequestBody.builder()
            if field_names:
                body_builder = body_builder.field_names(field_names)

            builder = (
                SearchAppTableRecordRequest.builder()
                .app_token(self._app_token)
                .table_id(table_id)
                .page_size(page_size)
                .request_body(body_builder.build())
            )
            if page_token:
                builder = builder.page_token(page_token)

            response = self._client.bitable.v1.app_table_record.search(builder.build())
            _check(response, f"查询记录 table_id={table_id}")

            for item in response.data.items or []:
                yield Record(record_id=item.record_id, fields=item.fields or {})

            if not response.data.has_more:
                break
            page_token = response.data.page_token

    def get_record(self, table_id: str, record_id: str) -> Record:
        request = (
            GetAppTableRecordRequest.builder()
            .app_token(self._app_token)
            .table_id(table_id)
            .record_id(record_id)
            .build()
        )
        response = self._client.bitable.v1.app_table_record.get(request)
        _check(response, f"读取记录 record_id={record_id}")
        item = response.data.record
        return Record(record_id=item.record_id, fields=item.fields or {})

    # ---------- 写 ----------

    def create_record(self, table_id: str, fields: dict[str, Any]) -> Record:
        """新增一条记录，返回写入后的完整记录。

        返回值包含系统生成的字段（比如自动编号），因为调用方需要把编号回显给销售。
        """
        record = AppTableRecord.builder().fields(fields).build()
        request = (
            CreateAppTableRecordRequest.builder()
            .app_token(self._app_token)
            .table_id(table_id)
            .request_body(record)
            .build()
        )

        with _WRITE_LOCK:
            response = self._client.bitable.v1.app_table_record.create(request)
            _check(response, f"新增记录 table_id={table_id}")
            created = response.data.record
            # 自动编号等系统字段在 create 响应里不一定回填，回读一次才拿得准
            return self.get_record(table_id, created.record_id)

    # ---------- schema 快照 ----------

    def snapshot_schema(self) -> dict[str, Any]:
        """把整个 Base 的表和字段结构导出成可比对的快照。"""
        snapshot: dict[str, Any] = {"app_token": self._app_token, "tables": {}}

        for table in self.list_tables():
            fields = self.list_fields(table.table_id)
            snapshot["tables"][table.name] = {
                "table_id": table.table_id,
                "fields": {
                    f.name: {
                        "field_id": f.field_id,
                        "type": f.type,
                        "ui_type": f.ui_type,
                        "is_primary": f.is_primary,
                        "read_only": f.is_read_only,
                    }
                    for f in fields
                },
            }

        return snapshot


def _to_plain_dict(obj: Any) -> dict[str, Any]:
    """SDK 的 property 对象转成普通 dict，方便序列化和比对。"""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        return {
            k: v
            for k, v in json.loads(lark.JSON.marshal(obj)).items()
            if v is not None
        }
    except Exception:  # noqa: BLE001 - property 结构五花八门，拿不到就算了
        return {}


def assert_fields_present(
    fields: list[FieldInfo],
    required: dict[str, int | None],
    *,
    table_label: str,
) -> None:
    """算钱之前确认依赖的字段还在，且类型没变。

    ``required`` 是 {字段名: 期望类型码}，类型码给 None 表示只查在不在。
    """
    by_name = {f.name: f for f in fields}
    problems: list[str] = []

    for name, expected_type in required.items():
        found = by_name.get(name)
        if found is None:
            problems.append(f"缺少字段「{name}」")
        elif expected_type is not None and found.type != expected_type:
            problems.append(
                f"字段「{name}」类型变了：期望 {expected_type}，实际 {found.type}"
                f"（{found.ui_type}）"
            )

    if problems:
        raise SchemaDriftError(
            f"{table_label} 的结构和预期不符，已停止以免算错账：\n  "
            + "\n  ".join(problems)
            + "\n如果是同事有意改的，跑 scripts/inspect_base.py 看新结构，再更新代码里的字段名。"
        )


def save_snapshot(snapshot: dict[str, Any], path: Path) -> None:
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
