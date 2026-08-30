"""渠道登记。

编号策略：优先用 Bitable 原生的「自动编号」字段（``R`` + 3 位自增），递增由飞书
系统保证，多个销售同时提交也不会撞号。该字段不能通过 API 写入，所以写完记录要
回读一次才能拿到编号回显给销售。

如果现有表的编号列是手工文本、又无法安全转成自动编号，就退回 ``next_manual_no``：
在写锁的临界区内读当前最大号 +1。写锁本来就为了规避 Bitable 的 WriteConflict
而存在，顺带给了递增一个安全的临界区。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..bot.auth import Sales
from ..lark.bitable import _WRITE_LOCK, BitableClient
from ..lark.values import extract_text
from . import schema
from .audit import ACTION_CREATE_REFERRAL, AuditLog

logger = logging.getLogger(__name__)

REFERRAL_NO_PATTERN = re.compile(r"^R(\d+)$")


class ValidationError(ValueError):
    """销售填的内容不合法。"""


@dataclass(frozen=True)
class ReferralInput:
    name: str
    email: str
    address: str
    payment_info: str
    commission_rate: float

    def validated(self) -> ReferralInput:
        if not self.name.strip():
            raise ValidationError("渠道名称不能为空")

        if self.email and "@" not in self.email:
            raise ValidationError(f"邮箱格式不对：{self.email}")

        if not 0 < self.commission_rate <= 100:
            raise ValidationError(f"分佣比例要在 0 到 100 之间，你填的是 {self.commission_rate}")

        return ReferralInput(
            name=self.name.strip(),
            email=self.email.strip(),
            address=self.address.strip(),
            payment_info=self.payment_info.strip(),
            commission_rate=self.commission_rate,
        )


def parse_referral_no(value: str) -> int | None:
    """R007 -> 7。不符合格式返回 None。"""
    match = REFERRAL_NO_PATTERN.match(value.strip().upper())
    return int(match.group(1)) if match else None


def format_referral_no(number: int, width: int = 3) -> str:
    return f"R{number:0{width}d}"


class ReferralService:
    def __init__(
        self,
        bitable: BitableClient,
        table_id: str,
        audit: AuditLog,
        *,
        auto_number: bool = True,
    ) -> None:
        self._bitable = bitable
        self._table_id = table_id
        self._audit = audit
        self._auto_number = auto_number

    def next_manual_no(self) -> str:
        """回退方案：读当前最大编号 +1。必须在写锁内调用。"""
        highest = 0
        for record in self._bitable.iter_records(self._table_id, field_names=[schema.REFERRAL_NO]):
            number = parse_referral_no(extract_text(record.fields.get(schema.REFERRAL_NO)))
            if number is not None:
                highest = max(highest, number)
        return format_referral_no(highest + 1)

    def create(self, sales: Sales, data: ReferralInput) -> tuple[str, str]:
        """登记一个新渠道，返回 (渠道编号, record_id)。

        归属人由 open_id 决定，销售不能自己指定 —— 卡片上没有这个输入项，
        这里也不接受传入。
        """
        clean = data.validated()

        fields: dict[str, object] = {
            schema.REFERRAL_NAME: clean.name,
            schema.REFERRAL_EMAIL: clean.email,
            schema.REFERRAL_ADDRESS: clean.address,
            schema.REFERRAL_PAYMENT: clean.payment_info,
            schema.REFERRAL_RATE: clean.commission_rate,
            schema.REFERRAL_OWNER: [{"id": sales.open_id}],
            schema.REFERRAL_OWNER_OPEN_ID: sales.open_id,
            schema.REFERRAL_STATUS: schema.STATUS_PENDING,
        }

        self._audit.record(
            actor_open_id=sales.open_id,
            actor_name=sales.name,
            action=ACTION_CREATE_REFERRAL,
            target_table=schema.TABLE_REFERRAL_NAME,
            detail={
                "渠道名称": clean.name,
                "邮箱": clean.email,
                "分佣比例": clean.commission_rate,
            },
        )

        with _WRITE_LOCK:
            if not self._auto_number:
                # 手工编号必须和写入在同一个临界区内，否则两个销售会拿到同一个号
                fields[schema.REFERRAL_NO] = self.next_manual_no()

            created = self._bitable.create_record(self._table_id, fields)

        referral_no = extract_text(created.fields.get(schema.REFERRAL_NO))

        if not referral_no:
            logger.error(
                "渠道 %s 写入成功但没读到编号，record_id=%s",
                clean.name,
                created.record_id,
            )
            referral_no = "(编号待生成)"

        return referral_no, created.record_id

    def list_for(self, sales: Sales) -> list[tuple[str, str]]:
        """该销售名下的渠道，返回 [(编号, 名称)]。管理员看全部。"""
        from ..bot.auth import owned_records

        result: list[tuple[str, str]] = []
        records = self._bitable.iter_records(self._table_id)
        for record in owned_records(sales, records, schema.REFERRAL_OWNER_OPEN_ID):
            result.append(
                (
                    extract_text(record.fields.get(schema.REFERRAL_NO)),
                    extract_text(record.fields.get(schema.REFERRAL_NAME)),
                )
            )
        result.sort()
        return result
