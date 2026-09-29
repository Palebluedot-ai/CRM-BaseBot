"""渠道的收款资料：地址 + 银行账户或加密货币钱包。出 invoice 要用。

2026-09-29 定的：收款资料放进 Base 的「Referral Information」（以前在 invoice 小工具
自己的 SQLite 里），以后只维护这一份。登记渠道时对方往往还没给钱包地址（通常另外发
邮件问），所以**不和登记渠道绑在一起**，机器人单独一个「登记收款资料」按钮。

  · 银行转账：银行户名（不填就用渠道名称）、银行名称、银行账号
  · 加密货币：币种（不填就是 USDT）、钱包地址
  · 地址：最多三行，invoice 抬头上一行一行印。**选填**：空着照样出 invoice，不提醒

权限和别处一样：只能改**自己名下**的渠道（「看全部」的人全部）。审计表只记改了哪几项，
**不记值** —— 账号、钱包地址不该在第二个地方再存一份。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..bot.auth import Sales, owned_records
from ..lark.bitable import BitableClient
from ..lark.values import extract_text
from . import schema
from .audit import AuditLog
from .referral import ValidationError

ACTION_UPDATE_PAYMENT = "更新收款资料"

ADDRESS_LINES = 3
DEFAULT_CRYPTO = "USDT"


@dataclass(frozen=True)
class PaymentInfo:
    method: str = ""
    address_lines: tuple[str, ...] = ()
    bank_account_name: str = ""
    bank_name: str = ""
    bank_account_no: str = ""
    crypto_type: str = ""
    wallet_address: str = ""

    @classmethod
    def from_fields(cls, fields: dict[str, Any]) -> PaymentInfo:
        address = extract_text(fields.get(schema.REFERRAL_ADDRESS))
        return cls(
            method=extract_text(fields.get(schema.REFERRAL_PAY_METHOD)).strip(),
            address_lines=tuple(line.strip() for line in address.splitlines() if line.strip()),
            bank_account_name=extract_text(fields.get(schema.REFERRAL_BANK_ACCOUNT_NAME)).strip(),
            bank_name=extract_text(fields.get(schema.REFERRAL_BANK_NAME)).strip(),
            bank_account_no=extract_text(fields.get(schema.REFERRAL_BANK_ACCOUNT_NO)).strip(),
            crypto_type=extract_text(fields.get(schema.REFERRAL_CRYPTO_TYPE)).strip(),
            wallet_address=extract_text(fields.get(schema.REFERRAL_WALLET)).strip(),
        )

    def missing(self) -> list[str]:
        """哪几项还空着（含地址）。给人看的「齐不齐」。"""
        return (["地址"] if not self.address_lines else []) + self.missing_for_payment()

    def missing_for_payment(self) -> list[str]:
        """出 invoice **必须**有的还缺哪几项：收款方式和那种方式的账户。

        地址不在里面（2026-09-29 定的）：地址空着照样出 invoice，也不提醒。
        钱包地址 / 银行账号少了不行 —— 那是钱往哪里打。
        """
        lacking: list[str] = []
        if self.method == schema.PAY_METHOD_BANK:
            if not self.bank_name:
                lacking.append("银行名称")
            if not self.bank_account_no:
                lacking.append("银行账号")
        elif self.method == schema.PAY_METHOD_CRYPTO:
            if not self.wallet_address:
                lacking.append("钱包地址")
        else:
            lacking.append("收款方式")
        return lacking

    @property
    def is_bank(self) -> bool:
        return self.method == schema.PAY_METHOD_BANK

    def validated(self) -> PaymentInfo:
        """表单提交时用：清掉多余空白，缺项报成一句人话。"""
        lines = tuple(line.strip() for line in self.address_lines if line and line.strip())
        if len(lines) > ADDRESS_LINES:
            raise ValidationError(f"地址最多 {ADDRESS_LINES} 行")
        info = replace(
            self,
            method=self.method.strip(),
            address_lines=lines,
            crypto_type=self.crypto_type.strip()
            or (DEFAULT_CRYPTO if self.method == schema.PAY_METHOD_CRYPTO else ""),
        )
        if info.method not in schema.PAY_METHOD_OPTIONS:
            raise ValidationError("收款方式要选一个：" + " / ".join(schema.PAY_METHOD_OPTIONS))
        lacking = info.missing_for_payment()
        if lacking:
            raise ValidationError(f"还没填：{'、'.join(lacking)}")
        return info

    def to_fields(self) -> dict[str, Any]:
        """写回 Base 的字段。另一种收款方式的那几格不动：换回去时不用再填一遍。"""
        fields: dict[str, Any] = {
            schema.REFERRAL_PAY_METHOD: self.method,
            schema.REFERRAL_ADDRESS: "\n".join(self.address_lines),
        }
        if self.is_bank:
            fields[schema.REFERRAL_BANK_ACCOUNT_NAME] = self.bank_account_name
            fields[schema.REFERRAL_BANK_NAME] = self.bank_name
            fields[schema.REFERRAL_BANK_ACCOUNT_NO] = self.bank_account_no
        else:
            fields[schema.REFERRAL_CRYPTO_TYPE] = self.crypto_type
            fields[schema.REFERRAL_WALLET] = self.wallet_address
        return fields


def masked(value: str) -> str:
    """回执上只露最后 4 位：``****5678``。"""
    text = value.strip()
    if not text:
        return ""
    return f"****{text[-4:]}" if len(text) > 4 else "****"


@dataclass(frozen=True)
class ReferralPayment:
    record_id: str
    no: str
    name: str
    info: PaymentInfo


class PaymentService:
    def __init__(self, bitable: BitableClient, referral_table: str, audit: AuditLog) -> None:
        self._bitable = bitable
        self._table = referral_table
        self._audit = audit

    def visible(self, sales: Sales) -> list[ReferralPayment]:
        """这个人能看到的渠道和它们的收款资料（销售：自己名下；管理员：全部）。"""
        out: list[ReferralPayment] = []
        records = self._bitable.iter_records(self._table)
        for record in owned_records(sales, records, schema.REFERRAL_OWNER_OPEN_ID):
            no = extract_text(record.fields.get(schema.REFERRAL_NO)).strip()
            if not no:
                continue
            out.append(
                ReferralPayment(
                    record_id=record.record_id,
                    no=no,
                    name=extract_text(record.fields.get(schema.REFERRAL_NAME)).strip(),
                    info=PaymentInfo.from_fields(record.fields),
                )
            )
        out.sort(key=lambda item: item.no)
        return out

    def get(self, sales: Sales, referral_no: str) -> ReferralPayment:
        wanted = referral_no.strip()
        for item in self.visible(sales):
            if item.no == wanted:
                return item
        # 不是你的和不存在回同一句话，不让人靠试探知道别人有哪些渠道。
        raise ValidationError(f"没找到你名下的渠道 {wanted}")

    def update(self, sales: Sales, referral_no: str, info: PaymentInfo) -> ReferralPayment:
        current = self.get(sales, referral_no)
        info = info.validated()
        fields = info.to_fields()
        changed = [
            name
            for name, value in fields.items()
            if value != current.info.to_fields().get(name, _NOT_SET)
        ]
        self._audit.record(
            actor_open_id=sales.open_id,
            actor_name=sales.name,
            action=ACTION_UPDATE_PAYMENT,
            target_table=schema.TABLE_REFERRAL_NAME,
            target_record=current.record_id,
            detail={"渠道编号": current.no, "改了": "、".join(changed) or "（没有变化）"},
        )
        self._bitable.update_record(self._table, current.record_id, fields)
        return replace(current, info=info)


_NOT_SET = object()
