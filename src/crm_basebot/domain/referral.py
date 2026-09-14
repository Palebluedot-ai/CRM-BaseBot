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
from collections.abc import Callable
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


def display_title(referral_no: str, name: str) -> str:
    """关联字段展示用的一行文字：R001 ABC Capital。

    关联字段永远显示被关联记录的**主字段**值。所以每条渠道记录的主字段里必须
    有一行既能唯一识别、又能看出是谁的文字 —— 光有编号（R001）没辨识度，光有
    名字（ABC Capital）可能撞名。前面的编号还没生成时留个占位，别把 None
    拼进字符串。
    """
    no = referral_no.strip() if referral_no else ""
    name = name.strip() if name else ""
    if no and name:
        return f"{no} {name}"
    return no or name


def _run_sync(func: Callable[[], None]) -> None:
    """默认的「后台」执行器：其实是同步跑。

    单测和一次性脚本走这条：断言写入结果时不用管线程时序。生产在 app.py 里
    注入真正的后台线程执行器（见 ``ReferralService`` 的 ``background`` 参数）。
    """
    func()


class ReferralService:
    def __init__(
        self,
        bitable: BitableClient,
        table_id: str,
        audit: AuditLog,
        *,
        auto_number: bool = True,
        background: Callable[[Callable[[], None]], None] = _run_sync,
    ) -> None:
        self._bitable = bitable
        self._table_id = table_id
        self._audit = audit
        self._auto_number = auto_number
        # 主字段回填往返有 2 次（list_fields + update_record），加上 create 的
        # 3 次一共 5 次，压不进卡片回调 3 秒预算 —— 客户端会显示成「延时未
        # 响应」（就是那个小火箭占位）。所以生产走后台线程，主字段的展示
        # 更新对销售异步可见，不阻塞成功卡片的返回。
        self._background = background
        # 主字段名启动时解析一次，后续每次登记都往它写「R001 XXX」。
        # 建表时飞书塞的默认主字段（通常叫「文本」）一直是空的 —— 这就是
        # Referred Client 表里所属渠道展示成「untitled record」的直接原因。
        self._primary_field: str | None = None

    def primary_field(self) -> str:
        """惰性解析主字段名并缓存。启动时不强解析是为了不给单测拉出接口依赖。"""
        if self._primary_field is None:
            self._primary_field = self._bitable.resolve_primary_field(self._table_id).name
        return self._primary_field

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

        # 主字段值只有拿到编号之后才能拼全。放在 create 之后单独更新是有意的：
        # 自动编号在 create 请求的 fields 里写不进去（服务端会忽略），必须先建
        # 记录再回读，读到编号了再回填主字段。同步跑会撑爆卡片回调 3 秒预算，
        # 所以丢到后台执行器 —— 生产是线程，测试是同步。
        record_id = created.record_id
        name = clean.name
        self._background(lambda: self._backfill_primary(record_id, referral_no, name))

        return referral_no, created.record_id

    def _backfill_primary(self, record_id: str, referral_no: str, name: str) -> None:
        """把主字段更新成 display_title(no, name)。失败只记日志，不阻断登记。

        主字段回填是「让展示不再是 untitled record」的运维性补丁，不是业务正确
        的前提 —— 就算这一步失败，渠道记录本身已经写好、审计也留了，销售看到
        的编号回显不受影响。所以异常吞掉不抛，只记 error 让运维事后处理。
        """
        title = display_title(referral_no, name)
        if not title:
            return

        try:
            primary = self.primary_field()
        except Exception:  # noqa: BLE001 - 读字段列表不该阻断登记
            logger.exception("读不到渠道表的主字段名，跳过主字段回填 record_id=%s", record_id)
            return

        if primary == schema.REFERRAL_NAME:
            # 主字段就是「渠道名称」，create 时已经写过了，不用再更新
            return

        try:
            self._bitable.update_record(self._table_id, record_id, {primary: title})
        except Exception:  # noqa: BLE001 - 见方法 docstring
            logger.exception(
                "回填渠道主字段失败 record_id=%s primary=%s", record_id, primary
            )

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
