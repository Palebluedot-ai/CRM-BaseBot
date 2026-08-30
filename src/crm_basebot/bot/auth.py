"""身份与归属校验。

整个方案的安全性压在一件事上：飞书回调里的 ``operator.open_id`` 是平台签发的，
客户端伪造不了。我们据此判断「这个人是谁」，再判断「这条记录是不是他的」。

所以有两条铁律：

1. open_id **只能**取自回调事件本体，绝不能从卡片的 value、表单字段或消息文本里
   取 —— 那些是用户可控的，等于让人自报家门。
2. 每一个读写入口都要过 ``require_owner``，不要因为「这个接口只有自己人用」
   就跳过。
"""

from __future__ import annotations

import logging
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


class SalesDirectory:
    """销售名册。缓存是进程级的，人员变动后重启或调用 refresh()。"""

    def __init__(self, bitable: BitableClient, table_id: str) -> None:
        self._bitable = bitable
        self._table_id = table_id
        self._cache: dict[str, Sales] | None = None

    def refresh(self) -> None:
        self._cache = None

    def _load(self) -> dict[str, Sales]:
        if self._cache is not None:
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


def require_owner(sales: Sales, record_owner_open_id: str, *, what: str) -> None:
    """确认这条记录归这名销售所有。管理员放行。

    ``what`` 用于日志和报错文案，比如「渠道 R007」。
    """
    if sales.is_admin:
        return

    if not record_owner_open_id:
        # 归属为空的记录一律不给普通销售碰。历史数据补录归属之前只有管理员能看。
        logger.warning("记录 %s 没有归属人，拒绝 %s(%s) 访问", what, sales.name, sales.open_id)
        raise AuthError(f"{what} 没有登记归属人，请联系管理员处理。")

    if record_owner_open_id != sales.open_id:
        logger.warning(
            "越权访问被拦截: %s(%s) 试图访问归属于 %s 的 %s",
            sales.name,
            sales.open_id,
            record_owner_open_id,
            what,
        )
        raise AuthError(f"{what} 不在你名下，无法查看或修改。")


def owned_records(sales: Sales, records, owner_field: str):
    """过滤出该销售名下的记录。管理员看全部。

    用生成器而不是列表，避免把整表读进内存。
    """
    for record in records:
        if sales.is_admin:
            yield record
            continue
        owner = extract_text(record.fields.get(owner_field))
        if owner and owner == sales.open_id:
            yield record
