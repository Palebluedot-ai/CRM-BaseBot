"""机器人事件与卡片回调处理。

一条硬约束：卡片回调必须在 **3 秒内**返回，否则用户看到 `200340`。所以这里
只做「校验 + 一次写入」，不做批量扫描、不做佣金计算，那些放 jobs/ 里跑。

另一条：open_id 只从 ``data.event.operator.open_id`` 取。这个值由飞书平台签发，
客户端伪造不了。任何从 form_value 或消息文本里取身份的写法都是漏洞。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from ..domain.referral import ReferralInput, ValidationError
from ..domain.referred_client import ClientInput
from ..lark.values import to_number
from . import cards
from .auth import AuthError

logger = logging.getLogger(__name__)


class BotHandlers:
    """把回调接到领域服务上。

    依赖显式注入，方便在没有飞书连接的情况下单测。
    """

    def __init__(
        self,
        *,
        client: lark.Client,
        directory,
        referrals,
        clients,
    ) -> None:
        self._client = client
        self._directory = directory
        self._referrals = referrals
        self._clients = clients

    # ---------- 收到消息：弹主菜单 ----------

    def on_message(self, data: P2ImMessageReceiveV1) -> None:
        chat_id = data.event.message.chat_id
        open_id = data.event.sender.sender_id.open_id

        try:
            sales = self._directory.require(open_id)
        except AuthError as exc:
            self._send(chat_id, cards.error_card(str(exc)))
            return

        self._send(chat_id, cards.menu_card(sales.name))

    # ---------- 卡片回调 ----------

    def on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        action_value = data.event.action.value or {}
        action = action_value.get("action", "")
        form = data.event.action.form_value or {}
        open_id = data.event.operator.open_id

        try:
            sales = self._directory.require(open_id)
        except AuthError as exc:
            return _card_response(cards.error_card(str(exc)))

        try:
            return self._dispatch(action, sales, form)
        except (ValidationError, AuthError) as exc:
            return _card_response(cards.error_card(str(exc)))
        except Exception:
            logger.exception("处理卡片动作 %s 失败", action)
            return _card_response(
                cards.error_card("系统出错了，请稍后再试。管理员可以在服务端日志里看到详情。")
            )

    def _dispatch(self, action, sales, form) -> P2CardActionTriggerResponse:
        if action == cards.ACTION_OPEN_REFERRAL_FORM:
            return _card_response(cards.referral_form_card())

        if action == cards.ACTION_OPEN_CLIENT_FORM:
            options = self._referrals.list_for(sales)
            return _card_response(cards.client_form_card(options))

        if action == cards.ACTION_LIST_REFERRALS:
            return _card_response(cards.referral_list_card(self._referrals.list_for(sales)))

        if action == cards.ACTION_SUBMIT_REFERRAL:
            return self._submit_referral(sales, form)

        if action == cards.ACTION_SUBMIT_CLIENT:
            return self._submit_client(sales, form)

        logger.warning("未知的卡片动作: %r", action)
        return _card_response(cards.error_card("这个操作我不认识，请重新开始。"))

    def _submit_referral(self, sales, form) -> P2CardActionTriggerResponse:
        rate = to_number(form.get(cards.F_REFERRAL_RATE))
        if rate is None:
            raise ValidationError("分佣比例要填数字，例如 20")

        referral_no, _ = self._referrals.create(
            sales,
            ReferralInput(
                name=form.get(cards.F_REFERRAL_NAME, ""),
                email=form.get(cards.F_REFERRAL_EMAIL, ""),
                address=form.get(cards.F_REFERRAL_ADDRESS, ""),
                payment_info=form.get(cards.F_REFERRAL_PAYMENT, ""),
                commission_rate=rate,
            ),
        )

        return _card_response(
            cards.success_card(
                "渠道已登记",
                f"编号 **{referral_no}**，状态为待审核。\n\n"
                f"接下来可以把这个渠道介绍的客户登记进来。",
            ),
            toast=f"已登记 {referral_no}",
        )

    def _submit_client(self, sales, form) -> P2CardActionTriggerResponse:
        self._clients.create(
            sales,
            ClientInput(
                uid=form.get(cards.F_CLIENT_UID, ""),
                name=form.get(cards.F_CLIENT_NAME, ""),
                referral_no=_select_value(form.get(cards.F_CLIENT_REFERRAL)),
            ),
        )

        return _card_response(
            cards.success_card(
                "客户已登记",
                "这个客户的交易会自动计入对应渠道的佣金。",
            ),
            toast="登记成功",
        )

    # ---------- 发消息 ----------

    def _send(self, chat_id: str, card: dict[str, Any]) -> None:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.error("发送卡片失败: %s %s", response.code, response.msg)


def _select_value(raw: Any) -> str:
    """下拉组件的回传值可能是裸字符串，也可能包成 {"value": ...}。"""
    if isinstance(raw, dict):
        return str(raw.get("value", ""))
    return str(raw or "")


def _card_response(
    card: dict[str, Any], *, toast: str | None = None
) -> P2CardActionTriggerResponse:
    payload: dict[str, Any] = {"card": {"type": "raw", "data": card}}
    if toast:
        payload["toast"] = {"type": "success", "content": toast}
    return P2CardActionTriggerResponse(payload)
