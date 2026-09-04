"""身份与归属校验。

整个方案的安全性压在一件事上：飞书回调里的 ``operator.open_id`` 是平台签发的，
客户端伪造不了。我们据此判断「这个人是谁」，再判断「这条记录是不是他的」。

所以有两条铁律：

1. open_id **只能**取自回调事件本体，绝不能从卡片的 value、表单字段或消息文本里
   取 —— 那些是用户可控的，等于让人自报家门。
2. 凡是按归属取数据的地方都走 ``owned_records`` 过滤，先筛掉不是他的记录再动手：
   列「我的渠道」、把客户挂到渠道，走的都是这一个函数。管理员放行也只在它里面
   定义，别在别处另写一份判断。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..domain import schema
from ..lark.bitable import BitableClient
from ..lark.values import extract_text

logger = logging.getLogger(__name__)


class AuthError(PermissionError):
    """身份不明或越权。"""


@dataclass(frozen=True)
class Sales:
    open_id: str
    name: str
    role: str
    is_active: bool

    @property
    def is_admin(self) -> bool:
        return self.role == schema.ROLE_ADMIN


# 名册缓存的有效期。停用一个人、加一个新人，最多这么久之后生效，不用重启机器人。
# 名册很小，重新读一次就是一个请求，但卡片回调只有 3 秒预算，每个往返都是实的，
# 所以不做成每次回调都读。
CACHE_TTL_SECONDS = 60.0


class SalesDirectory:
    """销售名册。

    缓存 ``ttl_seconds`` 秒，默认一分钟：名册在 Base 里改了，最多一分钟后生效；
    要立刻生效调 ``refresh()``。``clock`` 只是给测试拨时间用的。
    """

    def __init__(
        self,
        bitable: BitableClient,
        table_id: str,
        *,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bitable = bitable
        self._table_id = table_id
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, Sales] | None = None
        self._loaded_at = 0.0

    def refresh(self) -> None:
        self._cache = None

    def _load(self) -> dict[str, Sales]:
        if self._cache is not None and self._clock() - self._loaded_at < self._ttl:
            return self._cache

        directory: dict[str, Sales] = {}
        for record in self._bitable.iter_records(self._table_id):
            open_id = extract_text(record.fields.get(schema.SALES_OPEN_ID))
            if not open_id:
                continue
            directory[open_id] = Sales(
                open_id=open_id,
                name=extract_text(record.fields.get(schema.SALES_NAME)),
                role=extract_text(record.fields.get(schema.SALES_ROLE)) or schema.ROLE_SALES,
                is_active=extract_text(record.fields.get(schema.SALES_STATUS))
                != schema.SALES_STATUS_DISABLED,
            )

        self._cache = directory
        self._loaded_at = self._clock()
        return directory

    def lookup(self, open_id: str) -> Sales | None:
        if not open_id:
            return None
        return self._load().get(open_id)

    def require(self, open_id: str) -> Sales:
        """确认这个 open_id 属于一名在职销售，否则拒绝。

        名册里没有的人一律拒绝，而不是默默放行 —— 新人入职要先登记进名册，
        这是有意的，让「谁能用这个机器人」始终是一个可查的清单。
        """
        if not open_id:
            raise AuthError("回调里没有 open_id，拒绝处理")

        sales = self.lookup(open_id)
        if sales is None:
            logger.warning("未登记的 open_id 尝试操作: %s", open_id)
            raise AuthError(
                "你还没有被登记为销售，无法使用这个机器人。请联系管理员把你加入销售名册。"
            )

        if not sales.is_active:
            raise AuthError("你的账号已停用，如有疑问请联系管理员。")

        return sales


def owned_records(sales: Sales, records, owner_field: str):
    """过滤出该销售名下的记录。管理员看全部。

    归属为空的记录普通销售看不到：历史数据补录归属之前只有管理员能碰，
    不能因为「无主」就人人可见。用生成器而不是列表，避免把整表读进内存。
    """
    for record in records:
        if sales.is_admin:
            yield record
            continue
        owner = extract_text(record.fields.get(owner_field))
        if owner and owner == sales.open_id:
            yield record
