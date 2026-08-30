"""BitableClient 对真实 SDK 响应对象的处理。

其它测试用的是 conftest 里的内存假件，它证明的是业务逻辑自洽，证明不了「我们
按 SDK 的模型取值取对了」。这一份不同：假的只有网络那一层，**响应对象是 SDK 真
正的模型类**，从平台文档里的响应体 JSON 反序列化出来。所以 ``data.items`` 改名
成别的、或者响应里没有 ``record``，这里会红。

覆盖的都是接真实 API 的第一天就可能撞上的形态：
  - ``code=0`` 但 ``data`` 是 None
  - 分页：``has_more`` 为 true 时才有 ``page_token``
  - ``has_more=true`` 却没给 token（畸形响应，别转成死循环）
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from lark_oapi.api.bitable.v1 import (
    CreateAppTableRecordResponse,
    DeleteAppTableRecordResponse,
    GetAppTableRecordResponse,
    ListAppTableFieldResponse,
    ListAppTableResponse,
    SearchAppTableRecordResponse,
)

from crm_basebot.lark import bitable as bt
from crm_basebot.lark.bitable import MAX_SEARCH_PAGE_SIZE, BitableClient, BitableError

APP_TOKEN = "bascnTestAppToken"
TABLE_ID = "tblTest"


class FakeEndpoint:
    """按调用次序吐出预先准备好的响应，并记下每次请求。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[Any] = []

    def _next(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("接口被调用的次数超出预期 —— 大概率是翻页没停下来")
        return self._responses.pop(0)

    # SDK 上这些方法名不一样，但行为对我们来说是一回事
    list = _next
    search = _next
    get = _next
    create = _next
    delete = _next


class FakeSdkClient:
    def __init__(self, **endpoints: FakeEndpoint) -> None:
        self.bitable = self
        self.v1 = self
        for name, endpoint in endpoints.items():
            setattr(self, name, endpoint)


def client(**endpoints: FakeEndpoint) -> BitableClient:
    return BitableClient(APP_TOKEN, client=FakeSdkClient(**endpoints))


def search_page(items: list[dict[str, Any]], *, has_more: bool, page_token: str | None = None):
    body: dict[str, Any] = {"items": items, "has_more": has_more}
    # 平台的约定：has_more 为 false 时不返回 page_token
    if page_token is not None:
        body["page_token"] = page_token
    return SearchAppTableRecordResponse({"code": 0, "msg": "success", "data": body})


# ---------- code=0 但没有 data ----------


def test_没有_data_体时报出可诊断的错误():
    """``response.data`` 为 None 时直接点 ``.items`` 只会得到一句 AttributeError，
    既没有错误码也没有 log_id，等于把一次能查的接口失败变成一个无头异常。"""
    endpoint = FakeEndpoint([ListAppTableResponse({"code": 0, "msg": "success"})])

    with pytest.raises(BitableError, match="没有 data 体"):
        client(app_table=endpoint).list_tables()


def test_业务错误码带上_code_和_msg():
    endpoint = FakeEndpoint(
        [SearchAppTableRecordResponse({"code": 1254024, "msg": "InvalidFieldNames"})]
    )

    with pytest.raises(BitableError) as exc:
        list(client(app_table_record=endpoint).iter_records(TABLE_ID))

    assert "1254024" in str(exc.value)
    assert "InvalidFieldNames" in str(exc.value)


def test_检索记录没回_record_时报错():
    endpoint = FakeEndpoint([GetAppTableRecordResponse({"code": 0, "msg": "success", "data": {}})])

    with pytest.raises(BitableError, match="没有 record"):
        client(app_table_record=endpoint).get_record(TABLE_ID, "recX")


def test_新增记录没回_record_id_时报错():
    endpoint = FakeEndpoint(
        [CreateAppTableRecordResponse({"code": 0, "msg": "success", "data": {"record": {}}})]
    )

    with pytest.raises(BitableError, match="没拿到 record_id"):
        client(app_table_record=endpoint).create_record(TABLE_ID, {"名称": "x"})


# ---------- 分页 ----------


def test_按_page_token_翻完所有页():
    endpoint = FakeEndpoint(
        [
            search_page(
                [{"record_id": "rec1", "fields": {"名称": "a"}}],
                has_more=True,
                page_token="tok-2",
            ),
            search_page([{"record_id": "rec2", "fields": {"名称": "b"}}], has_more=False),
        ]
    )

    records = list(client(app_table_record=endpoint).iter_records(TABLE_ID))

    assert [r.record_id for r in records] == ["rec1", "rec2"]
    assert endpoint.requests[0].page_token is None
    assert endpoint.requests[1].page_token == "tok-2"


def test_最后一页不带_page_token_也能正常收尾():
    """has_more=false 时平台不返回 page_token，别把「没有 token」当成异常。"""
    endpoint = FakeEndpoint([search_page([], has_more=False)])
    assert list(client(app_table_record=endpoint).iter_records(TABLE_ID)) == []


def test_has_more_为真却没给_token_时停下而不是死循环(caplog):
    """把空 token 原样传回去等于反复查第一页 —— 一边刷接口一边无限吐重复记录。"""
    endpoint = FakeEndpoint(
        [search_page([{"record_id": "rec1", "fields": {}}], has_more=True, page_token="")]
    )

    records = list(client(app_table_record=endpoint).iter_records(TABLE_ID))

    assert [r.record_id for r in records] == ["rec1"]
    assert "has_more=true 但没拿到 page_token" in caplog.text


def test_列字段也会翻页():
    endpoint = FakeEndpoint(
        [
            ListAppTableFieldResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "field_id": "fld1",
                                "field_name": "渠道编号",
                                "type": 1005,
                                "ui_type": "AutoNumber",
                                "is_primary": True,
                            }
                        ],
                        "has_more": True,
                        "page_token": "tok-2",
                    },
                }
            ),
            ListAppTableFieldResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "field_id": "fld2",
                                "field_name": "渠道名称",
                                "type": 1,
                                "ui_type": "Text",
                                "is_primary": False,
                            }
                        ],
                        "has_more": False,
                    },
                }
            ),
        ]
    )

    fields = client(app_table_field=endpoint).list_fields(TABLE_ID)

    assert [f.name for f in fields] == ["渠道编号", "渠道名称"]
    # 自动编号是只读字段，写入 payload 里绝不能出现
    assert fields[0].is_read_only is True
    assert fields[1].is_read_only is False


# ---------- 请求参数 ----------


def test_查询记录显式传满页大小():
    """「查询记录」默认 page_size 只有 20，不显式传的话读一张几千行的表要翻几百页。"""
    endpoint = FakeEndpoint([search_page([], has_more=False)])
    list(client(app_table_record=endpoint).iter_records(TABLE_ID))

    request = endpoint.requests[0]
    assert request.page_size == MAX_SEARCH_PAGE_SIZE
    assert request.app_token == APP_TOKEN
    assert request.table_id == TABLE_ID


def test_超过上限的页大小在本地就被拦下():
    endpoint = FakeEndpoint([])
    with pytest.raises(ValueError, match="page_size"):
        list(client(app_table_record=endpoint).iter_records(TABLE_ID, page_size=1000))


def test_指定字段名会放进请求体():
    endpoint = FakeEndpoint([search_page([], has_more=False)])
    list(client(app_table_record=endpoint).iter_records(TABLE_ID, field_names=["渠道编号"]))

    assert endpoint.requests[0].request_body.field_names == ["渠道编号"]


def test_新增记录把_fields_放进_request_body():
    """写入的 payload 必须是 ``{"fields": {...}}``，字段值原样传给平台。"""
    endpoint = FakeEndpoint(
        [
            CreateAppTableRecordResponse(
                {
                    "code": 0,
                    "data": {"record": {"record_id": "recNew", "fields": {"客户UID": "5778"}}},
                }
            )
        ]
    )

    written = {"客户UID": "577809207768677761", "归属销售": [{"id": "ou_x"}], "所属渠道": ["recA"]}
    record = client(app_table_record=endpoint).create_record(TABLE_ID, written, reread=False)

    assert record.record_id == "recNew"
    assert endpoint.requests[0].request_body.fields == written


def test_需要读回自动编号时才多发一次检索():
    create = CreateAppTableRecordResponse(
        {"code": 0, "data": {"record": {"record_id": "recNew", "fields": {"渠道名称": "北极星"}}}}
    )
    get = GetAppTableRecordResponse(
        {
            "code": 0,
            "data": {"record": {"record_id": "recNew", "fields": {"渠道编号": "R001"}}},
        }
    )
    endpoint = FakeEndpoint([create, get])

    record = client(app_table_record=endpoint).create_record(TABLE_ID, {"渠道名称": "北极星"})

    assert record.fields["渠道编号"] == "R001"
    assert len(endpoint.requests) == 2


def test_请求在途时别的线程拿不到写锁():
    """同一张表并发写会撞 1254291 Write conflict，所以写路径必须整段串行。

    在「请求已发出、响应还没回来」的那一刻去另一个线程抢锁 —— 抢到了就说明锁的
    范围没盖住真正发请求的那一段，等于没锁。
    """
    lock_free_elsewhere: list[bool] = []

    class ProbingEndpoint(FakeEndpoint):
        def _next(self, request):
            result: list[bool] = []

            def probe():
                acquired = bt._WRITE_LOCK.acquire(blocking=False)
                result.append(acquired)
                if acquired:
                    bt._WRITE_LOCK.release()

            other = threading.Thread(target=probe)
            other.start()
            other.join()
            lock_free_elsewhere.extend(result)
            return super()._next(request)

        create = _next

    endpoint = ProbingEndpoint(
        [CreateAppTableRecordResponse({"code": 0, "data": {"record": {"record_id": "rec1"}}})]
    )
    client(app_table_record=endpoint).create_record(TABLE_ID, {"x": 1}, reread=False)

    assert lock_free_elsewhere == [False], "写请求在途时写锁没被持有"


def test_删除不因为没有_data_体而判失败():
    """删除不看返回内容，data 是不是 None 无所谓，别把成功的操作判成失败。"""
    endpoint = FakeEndpoint([DeleteAppTableRecordResponse({"code": 0, "msg": "success"})])
    client(app_table_record=endpoint).delete_record(TABLE_ID, "rec1")

    assert endpoint.requests[0].record_id == "rec1"


def test_空_app_token_立刻报错():
    with pytest.raises(ValueError, match="app_token"):
        BitableClient("", client=FakeSdkClient())
