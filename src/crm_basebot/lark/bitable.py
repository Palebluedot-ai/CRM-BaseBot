"""Bitable 读写封装。

三件事在这里集中处理，别处不要绕过：

1. **写操作串行化。** Bitable 的写接口不支持并发，同一张表上并发写会返回
   ``1254291 Write conflict``。所有写都要拿 ``_WRITE_LOCK``。用锁而不是异步队列，
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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppTableRecord,
    BatchCreateAppTableRecordRequest,
    BatchCreateAppTableRecordRequestBody,
    BatchDeleteAppTableRecordRequest,
    BatchDeleteAppTableRecordRequestBody,
    Condition,
    CreateAppTableRecordRequest,
    DeleteAppTableRecordRequest,
    FilterInfo,
    GetAppTableRecordRequest,
    ListAppTableFieldRequest,
    ListAppTableRequest,
    SearchAppTableRecordRequest,
    SearchAppTableRecordRequestBody,
    UpdateAppTableRecordRequest,
)

from .client import get_client

logger = logging.getLogger(__name__)

# 同一进程内所有 Bitable 写操作的串行化闸门
_WRITE_LOCK = threading.RLock()

# 「查询记录」接口单次最多 500 行（默认只有 20，所以必须显式传）
MAX_SEARCH_PAGE_SIZE = 500

# 「查询记录」按某一列等于若干个值筛选时，一个请求里放多少个条件。平台上限是 50，
# 取 20 留足余量 —— 一条渠道名下的客户通常十几个，一个请求就够。
MAX_FILTER_VALUES = 20

# 「列出数据表」「列出字段」两个接口的分页上限
MAX_LIST_PAGE_SIZE = 100

# 批量写一次发多少条。平台的批量新增一次最多 1000 条、批量删除一次最多 500 条，
# 统一按 500 切，调用方不用记哪个接口是多少。
MAX_BATCH_SIZE = 500

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


def _check(response: Any, what: str, *, require_data: bool = True) -> Any:
    """确认调用成功并把 ``response.data`` 返回出来。

    两道检查缺一不可。``success()`` 只看业务码，而 ``data`` 在少数情况下会是
    None —— 网关层返回非 JSON 体、或者响应体没有 data 字段时，SDK 反序列化出来
    就是 None。直接 ``response.data.items`` 会抛 AttributeError，报错里既没有
    错误码也没有 log_id，等于把一次可诊断的接口失败变成一句无头的异常。

    ``require_data=False`` 留给不看返回内容的调用（比如删除）—— 那种情况下
    data 是不是 None 无所谓，不该因此把一次成功的操作判成失败。
    """
    if not response.success():
        raise BitableError(
            f"{what} 失败: code={response.code} msg={response.msg} "
            f"log_id={getattr(response, 'get_log_id', lambda: '')()}"
        )

    if require_data and response.data is None:
        raise BitableError(
            f"{what} 返回 code=0 但没有 data 体，无法继续。"
            f"log_id={getattr(response, 'get_log_id', lambda: '')()}"
        )

    return response.data


def _next_page_token(data: Any, what: str) -> str | None:
    """算出下一页的 page_token，没有下一页返回 None。

    平台的约定是「``has_more`` 为 true 时才返回 ``page_token``」。真出现
    has_more=true 但 token 为空的情况，照原样把空 token 传回去等于从头再查一遍
    第一页 —— 死循环，而且是那种一边刷接口一边不停 yield 重复记录的死循环。
    这里宁可提前结束并留一条 error 日志。
    """
    if not data.has_more:
        return None

    token = data.page_token
    if not token:
        logger.error("%s：has_more=true 但没拿到 page_token，只能按已读到的部分继续", what)
        return None

    return token


def _check_batch_size(batch_size: int) -> None:
    if not 0 < batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size 要在 1 到 {MAX_BATCH_SIZE} 之间，给的是 {batch_size}")


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
                ListAppTableRequest.builder()
                .app_token(self._app_token)
                .page_size(MAX_LIST_PAGE_SIZE)
            )
            if page_token:
                builder = builder.page_token(page_token)

            response = self._client.bitable.v1.app_table.list(builder.build())
            data = _check(response, "列出数据表")

            for item in data.items or []:
                tables.append(TableInfo(table_id=item.table_id, name=item.name))

            page_token = _next_page_token(data, "列出数据表")
            if page_token is None:
                break

        return tables

    def list_fields(self, table_id: str) -> list[FieldInfo]:
        fields: list[FieldInfo] = []
        page_token: str | None = None

        while True:
            builder = (
                ListAppTableFieldRequest.builder()
                .app_token(self._app_token)
                .table_id(table_id)
                .page_size(MAX_LIST_PAGE_SIZE)
            )
            if page_token:
                builder = builder.page_token(page_token)

            response = self._client.bitable.v1.app_table_field.list(builder.build())
            data = _check(response, f"列出字段 table_id={table_id}")

            for item in data.items or []:
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

            page_token = _next_page_token(data, f"列出字段 table_id={table_id}")
            if page_token is None:
                break

        return fields

    def resolve_primary_field(self, table_id: str) -> FieldInfo:
        """找到某张表的主字段。找不到就抛，别拿一个不确定的字段回去写。

        主字段决定所有指向本表的关联字段展示什么 —— 主字段为空时飞书界面上会
        显示成「无标题记录」，佣金对账里再复杂的关联链也就跟着「untitled」。
        """
        for f in self.list_fields(table_id):
            if f.is_primary:
                return f
        raise BitableError(
            f"表 {table_id} 没有主字段？飞书至少给每张表一个主字段，读不到就是接口异常"
        )

    def iter_records(
        self,
        table_id: str,
        *,
        page_size: int = MAX_SEARCH_PAGE_SIZE,
        field_names: list[str] | None = None,
    ) -> Iterator[Record]:
        """遍历全表记录，自动翻页。

        ``field_names`` 里的名字必须和 Base 里的字段名**逐字符**一致，差一个空格
        就是 1254024 InvalidFieldNames，不会退化成「返回全部字段」。
        """
        if not 0 < page_size <= MAX_SEARCH_PAGE_SIZE:
            raise ValueError(f"page_size 要在 1 到 {MAX_SEARCH_PAGE_SIZE} 之间，给的是 {page_size}")

        yield from self._search(table_id, page_size=page_size, field_names=field_names)

    def iter_records_where_in(
        self,
        table_id: str,
        field_name: str,
        values: Iterable[str],
        *,
        field_names: list[str] | None = None,
    ) -> Iterator[Record]:
        """只取 ``field_name`` 等于 ``values`` 里任意一个值的记录，筛选在服务端做。

        给「大表里按一小撮键取数」用：一条渠道名下十来个客户的交易明细，全表扫一遍
        日读看板是十几个分页往返，按客户UID 筛选一两页就回来了。

        值按 ``MAX_FILTER_VALUES`` 切批，每批各自翻页。去重后一个值都没有时一个请求
        都不发。条件是「等于」（``is``），对文本列是逐字符比较 —— UID 两边都是文本，
        正合适。
        """
        wanted = sorted({value for value in values if value})
        for start in range(0, len(wanted), MAX_FILTER_VALUES):
            chunk = wanted[start : start + MAX_FILTER_VALUES]
            filter_info = (
                FilterInfo.builder()
                .conjunction("or")
                .conditions(
                    [
                        Condition.builder()
                        .field_name(field_name)
                        .operator("is")
                        .value([value])
                        .build()
                        for value in chunk
                    ]
                )
                .build()
            )
            yield from self._search(
                table_id,
                page_size=MAX_SEARCH_PAGE_SIZE,
                field_names=field_names,
                filter_info=filter_info,
            )

    def _search(
        self,
        table_id: str,
        *,
        page_size: int,
        field_names: list[str] | None,
        filter_info: FilterInfo | None = None,
    ) -> Iterator[Record]:
        page_token: str | None = None

        while True:
            body_builder = SearchAppTableRecordRequestBody.builder()
            if field_names:
                body_builder = body_builder.field_names(field_names)
            if filter_info is not None:
                body_builder = body_builder.filter(filter_info)

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
            data = _check(response, f"查询记录 table_id={table_id}")

            for item in data.items or []:
                yield Record(record_id=item.record_id, fields=item.fields or {})

            page_token = _next_page_token(data, f"查询记录 table_id={table_id}")
            if page_token is None:
                break

    def get_record(self, table_id: str, record_id: str) -> Record:
        request = (
            GetAppTableRecordRequest.builder()
            .app_token(self._app_token)
            .table_id(table_id)
            .record_id(record_id)
            .build()
        )
        response = self._client.bitable.v1.app_table_record.get(request)
        data = _check(response, f"读取记录 record_id={record_id}")
        item = data.record
        if item is None:
            raise BitableError(f"读取记录 record_id={record_id} 返回的 data 里没有 record")
        return Record(record_id=item.record_id, fields=item.fields or {})

    # ---------- 写 ----------

    def create_record(
        self, table_id: str, fields: dict[str, Any], *, reread: bool = True
    ) -> Record:
        """新增一条记录，返回写入后的完整记录。

        ``reread=True`` 时会额外读一次刚写的记录，为的是拿到系统生成的字段（自动
        编号），因为调用方要把编号回显给销售。代价是每次写变成两个串行请求。

        不需要读回编号的调用方应该传 ``reread=False``：卡片回调只有 3 秒预算，
        而审计写入是每一次业务操作的前置步骤，白白多一个往返直接吃掉预算。
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
            data = _check(response, f"新增记录 table_id={table_id}")
            created = data.record
            if created is None or not created.record_id:
                raise BitableError(f"新增记录 table_id={table_id} 成功但没拿到 record_id")
            if not reread:
                return Record(record_id=created.record_id, fields=created.fields or {})
            # 自动编号等系统字段在 create 响应里不一定回填，回读一次才拿得准
            return self.get_record(table_id, created.record_id)

    def update_record(self, table_id: str, record_id: str, fields: dict[str, Any]) -> Record:
        """更新已有记录的部分字段。fields 里没提到的字段不动。

        用于回填历史数据（比如给老渠道记录补上主字段值），机器人日常业务是不改
        已有记录的 —— 改动都走「新增」，审计表更是明确只增不改。
        """
        record = AppTableRecord.builder().fields(fields).build()
        request = (
            UpdateAppTableRecordRequest.builder()
            .app_token(self._app_token)
            .table_id(table_id)
            .record_id(record_id)
            .request_body(record)
            .build()
        )

        with _WRITE_LOCK:
            response = self._client.bitable.v1.app_table_record.update(request)
            data = _check(response, f"更新记录 record_id={record_id}")
            updated = data.record
            if updated is None or not updated.record_id:
                raise BitableError(f"更新记录 record_id={record_id} 成功但没拿到 record")
            return Record(record_id=updated.record_id, fields=updated.fields or {})

    def delete_record(self, table_id: str, record_id: str) -> None:
        """删除一条记录。

        目前只有 ``scripts/seed_dev_data.py --reset`` 用它清理开发租户里的种子
        数据。机器人和对账任务都不删记录 —— 审计表更是明确只增不改。放在这里而
        不是让脚本自己调 SDK，是为了让删除也走同一把写锁：删和写并发同样会撞
        ``1254291 Write conflict``。
        """
        request = (
            DeleteAppTableRecordRequest.builder()
            .app_token(self._app_token)
            .table_id(table_id)
            .record_id(record_id)
            .build()
        )

        with _WRITE_LOCK:
            response = self._client.bitable.v1.app_table_record.delete(request)
            _check(response, f"删除记录 record_id={record_id}", require_data=False)

    def batch_create_records(
        self,
        table_id: str,
        records: list[dict[str, Any]],
        *,
        batch_size: int = MAX_BATCH_SIZE,
    ) -> int:
        """批量新增，返回写入条数。

        导入看板一次就是成千上万行。一行一个请求的话，2026-09-17 那份 1.2 万行的导出
        就要 1.2 万次调用，而免费版一个月的基线额度才 1 万次；按 500 条一批只要 25 次。

        每一批单独拿写锁，和单条写是同一把锁，不会和机器人的写入撞 ``1254291``。
        不回读，批量写的调用方都用不着系统字段。中途某一批失败时，前面的批已经写进去了，
        报错里带着已写条数；看板导入按日期先删后写，重跑一次就能盖掉写了一半的数据。
        """
        _check_batch_size(batch_size)
        written = 0
        for start in range(0, len(records), batch_size):
            chunk = records[start : start + batch_size]
            body = (
                BatchCreateAppTableRecordRequestBody.builder()
                .records([AppTableRecord.builder().fields(fields).build() for fields in chunk])
                .build()
            )
            request = (
                BatchCreateAppTableRecordRequest.builder()
                .app_token(self._app_token)
                .table_id(table_id)
                .request_body(body)
                .build()
            )
            with _WRITE_LOCK:
                response = self._client.bitable.v1.app_table_record.batch_create(request)
                data = _check(response, f"批量新增记录 table_id={table_id}")
            created = data.records or []
            if len(created) != len(chunk):
                raise BitableError(
                    f"批量新增 table_id={table_id} 这一批发了 {len(chunk)} 条，"
                    f"平台只回了 {len(created)} 条；在这一批之前已写入 {written} 条"
                )
            written += len(chunk)
        return written

    def batch_delete_records(
        self,
        table_id: str,
        record_ids: list[str],
        *,
        batch_size: int = MAX_BATCH_SIZE,
    ) -> int:
        """批量删除，返回删除条数。为什么要批量、怎么切，同 ``batch_create_records``。

        平台对每一条回报删没删掉。有一条没删掉就报错并点名，不当成功处理。
        """
        _check_batch_size(batch_size)
        deleted = 0
        for start in range(0, len(record_ids), batch_size):
            chunk = record_ids[start : start + batch_size]
            body = BatchDeleteAppTableRecordRequestBody.builder().records(chunk).build()
            request = (
                BatchDeleteAppTableRecordRequest.builder()
                .app_token(self._app_token)
                .table_id(table_id)
                .request_body(body)
                .build()
            )
            with _WRITE_LOCK:
                response = self._client.bitable.v1.app_table_record.batch_delete(request)
                data = _check(response, f"批量删除记录 table_id={table_id}", require_data=False)
            results = (data.records if data is not None else None) or []
            failed = [item.record_id for item in results if item.deleted is False]
            if failed:
                raise BitableError(
                    f"批量删除 table_id={table_id} 有 {len(failed)} 条没删掉，例如 {failed[:5]}；"
                    f"在这一批之前已删除 {deleted} 条"
                )
            deleted += len(chunk)
        return deleted

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
        return {k: v for k, v in json.loads(lark.JSON.marshal(obj)).items() if v is not None}
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
