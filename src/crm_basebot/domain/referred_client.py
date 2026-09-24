"""渠道介绍的客户登记。

关键约束：``客户UID`` 全程字符串。这是和交易明细表 join 的键，18-19 位，
一旦变成数字就会静默错配。见 lark/values.py。

销售只能把客户挂到自己名下的渠道上 —— 归属校验在写入前做，不是写完再查。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..bot.auth import Sales, owned_records
from ..lark.bitable import BitableClient
from ..lark.values import extract_text, to_uid
from . import schema
from .audit import ACTION_CREATE_CLIENT, AuditLog
from .commission import _link_ids
from .referral import ValidationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClientInput:
    uid: str
    name: str
    referral_no: str

    def validated(self) -> ClientInput:
        uid = to_uid(self.uid)
        if not uid:
            raise ValidationError("客户UID 不能为空")
        if not uid.isdigit():
            raise ValidationError(f"客户UID 应该是纯数字，你填的是「{uid}」")
        if not self.name.strip():
            raise ValidationError("客户名称不能为空")
        if not self.referral_no.strip():
            raise ValidationError("必须选择所属渠道")

        return ClientInput(
            uid=uid,
            name=self.name.strip(),
            referral_no=self.referral_no.strip().upper(),
        )


class ReferredClientService:
    def __init__(
        self,
        bitable: BitableClient,
        table_id: str,
        referral_table_id: str,
        audit: AuditLog,
    ) -> None:
        self._bitable = bitable
        self._table_id = table_id
        self._referral_table_id = referral_table_id
        self._audit = audit

    def _resolve_owned_referral(self, sales: Sales, referral_no: str) -> str:
        """把渠道编号换成 record_id，顺带确认它归这名销售所有。

        找不到和不属于你，都回同一句话 —— 不告诉对方「这个编号存在但不是你的」，
        避免通过试探枚举出别人的渠道。
        """
        records = self._bitable.iter_records(self._referral_table_id)
        for record in owned_records(sales, records, schema.REFERRAL_OWNER_OPEN_ID):
            if extract_text(record.fields.get(schema.REFERRAL_NO)) == referral_no:
                return record.record_id

        raise ValidationError(f"没找到你名下的渠道 {referral_no}")

    def find_by_uid(self, uid: str) -> str | None:
        target = to_uid(uid)
        for record in self._bitable.iter_records(self._table_id):
            if to_uid(record.fields.get(schema.CLIENT_UID)) == target:
                return record.record_id
        return None

    def names_for_referral(self, referral_record_id: str) -> list[str]:
        """这条渠道名下的客户名称，按名称排序。

        只读要用的两列。客户表是全台子的客户，全字段读回来光 UID 那一串就白白占掉
        大半流量，而这里只要名字和挂在谁下面。

        ``referral_record_id`` 必须是调用方**已经鉴过权**的那条 —— 这个方法自己不判
        归属，它只按关联取数。
        """
        if not referral_record_id:
            return []
        names: list[str] = []
        for record in self._bitable.iter_records(
            self._table_id,
            field_names=[schema.CLIENT_NAME, schema.CLIENT_REFERRAL_LINK],
        ):
            linked = _link_ids(record.fields.get(schema.CLIENT_REFERRAL_LINK) or [])
            if referral_record_id in linked:
                names.append(extract_text(record.fields.get(schema.CLIENT_NAME)))
        names.sort()
        return names

    def create(self, sales: Sales, data: ClientInput) -> str:
        clean = data.validated()

        referral_record_id = self._resolve_owned_referral(sales, clean.referral_no)

        existing = self.find_by_uid(clean.uid)
        if existing is not None:
            raise ValidationError(
                f"客户 {clean.uid} 已经登记过了。如果归属有误，请联系管理员调整。"
            )

        fields = {
            schema.CLIENT_UID: clean.uid,
            schema.CLIENT_NAME: clean.name,
            schema.CLIENT_REFERRAL_LINK: [referral_record_id],
            schema.CLIENT_OWNER: [{"id": sales.open_id}],
            schema.CLIENT_OWNER_OPEN_ID: sales.open_id,
        }

        self._audit.record(
            actor_open_id=sales.open_id,
            actor_name=sales.name,
            action=ACTION_CREATE_CLIENT,
            target_table=schema.TABLE_CLIENT_NAME,
            detail={
                "客户UID": clean.uid,
                "客户名称": clean.name,
                "所属渠道": clean.referral_no,
            },
        )

        created = self._bitable.create_record(self._table_id, fields)
        logger.info(
            "登记客户 %s「%s」挂到渠道 %s record_id=%s 操作人=%s(%s)",
            clean.uid,
            clean.name,
            clean.referral_no,
            created.record_id,
            sales.name,
            sales.open_id,
        )
        return created.record_id
