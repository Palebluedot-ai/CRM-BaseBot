"""机器人事件与卡片回调处理。

一条硬约束：卡片回调必须在 **3 秒内**返回，否则客户端弹「延时未响应」，而
「延时未响应」是客户端本地文案，改不掉服务端也拦不到 —— 唯一的办法是让回调
在 3 秒内返回。所以这里只做「校验 + 一次写入」，不做批量扫描、不做佣金计算，
那些放 jobs/ 里跑。

对超预算的路径（比如客户登记，要 4 个串行往返），走「立即 ack + 后台写入
+ 结果推送为新消息」的异步模式，见 ``_submit_client``。

另一条：open_id 只从 ``data.event.operator.open_id`` 取。这个值由飞书平台签发，
客户端伪造不了。任何从 form_value 或消息文本里取身份的写法都是漏洞。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
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


def _run_in_thread(func: Callable[[], None]) -> None:
    threading.Thread(target=func, daemon=True).start()


class BotHandlers:
    """把回调接到领域服务上。

    依赖显式注入，方便在没有飞书连接的情况下单测。

    ``background`` 是后台任务的执行器，用于卡片回调超预算时把实际写入丢到后台
    去跑（见 ``_submit_client``）。默认起一个 daemon 线程；测试里传
    ``lambda fn: fn()`` 走同步，避免线程竞态。
    """

    def __init__(
        self,
        *,
        client: lark.Client,
        directory,
        referrals,
        clients,
        commission_query=None,
        background: Callable[[Callable[[], None]], None] = _run_in_thread,
    ) -> None:
        self._client = client
        self._directory = directory
        self._referrals = referrals
        self._clients = clients
        # 可选：不注入时「佣金查询」按钮点了会回一句「暂不可用」，而不是崩。
        # 生产 app.py 一定会注入；测试有的场景不需要。
        self._commission_query = commission_query
        self._background = background

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
        # value 在平台侧允许是 object 也允许是裸字符串，SDK 把它声明成 Dict[str, Any]
        # 但不做校验。非 dict 的情况按「认不出的动作」处理，别让 .get 抛 AttributeError。
        action_value = data.event.action.value
        action = action_value.get("action", "") if isinstance(action_value, dict) else ""
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

        if action == cards.ACTION_OPEN_COMMISSION_QUERY:
            return self._open_commission_query(sales)

        if action == cards.ACTION_QUERY_COMMISSION:
            return self._query_commission(sales, form)

        logger.warning("未知的卡片动作: %r", action)
        return _card_response(cards.error_card("这个操作我不认识，请重新开始。"))

    def _submit_referral(self, sales, form) -> P2CardActionTriggerResponse:
        # 输入框的标签就写着「分佣比例 (%)」，照着填「20%」是很自然的事。
        # 不去掉这个百分号，to_number 会返回 None，人看到的是「要填数字」——
        # 而他明明填的就是数字。
        rate = to_number(_form_text(form, cards.F_REFERRAL_RATE).rstrip("%").strip())
        if rate is None:
            raise ValidationError("分佣比例要填数字，例如 20")

        referral_no, _ = self._referrals.create(
            sales,
            ReferralInput(
                name=_form_text(form, cards.F_REFERRAL_NAME),
                email=_form_text(form, cards.F_REFERRAL_EMAIL),
                address=_form_text(form, cards.F_REFERRAL_ADDRESS),
                payment_info=_form_text(form, cards.F_REFERRAL_PAYMENT),
                commission_rate=rate,
            ),
        )

        return _card_response(
            cards.success_card(
                "渠道已登记",
                f"编号 **{referral_no}**，已生效。\n\n接下来可以把这个渠道介绍的客户登记进来。",
            ),
            toast=f"已登记 {referral_no}",
        )

    def _submit_client(self, sales, form) -> P2CardActionTriggerResponse:
        # 这条路径要 4 个串行往返：扫渠道表确认归属、扫客户表查 UID 重复、写审计、
        # 写客户。真机测下来经常撑爆 3 秒预算，客户端就会弹「延时未响应」——
        # 尽管服务端其实已经写成功了。
        #
        # 所以走异步模式：立即 ack 一张「已提交，处理中」的卡片让客户端满意，
        # 真正的写入丢到后台线程；结果（成功或失败）通过 im.v1.message.create
        # 推一条新的消息给这名销售。
        client_input = ClientInput(
            uid=_form_text(form, cards.F_CLIENT_UID),
            name=_form_text(form, cards.F_CLIENT_NAME),
            referral_no=_select_value(form.get(cards.F_CLIENT_REFERRAL)),
        )
        target_open_id = sales.open_id

        def worker() -> None:
            try:
                self._clients.create(sales, client_input)
            except (ValidationError, AuthError) as exc:
                self._send_to_user(target_open_id, cards.error_card(str(exc)))
                return
            except Exception:
                logger.exception("异步登记客户失败 open_id=%s", target_open_id)
                self._send_to_user(
                    target_open_id,
                    cards.error_card(
                        "系统出错了，请稍后再试。管理员可以在服务端日志里看到详情。"
                    ),
                )
                return

            self._send_to_user(
                target_open_id,
                cards.success_card(
                    "客户已登记",
                    "这个客户的交易会自动计入对应渠道的佣金。",
                ),
            )

        self._background(worker)

        return _card_response(
            cards.notice_card(
                "已收到，正在提交",
                "客户信息正在写入 Base，处理完会作为新消息推送给你（通常 3-10 秒）。",
                template="blue",
            ),
            toast="已提交，处理中",
        )

    def _open_commission_query(self, sales) -> P2CardActionTriggerResponse:
        """打开「佣金查询」表单：从看板取月份列表，销售在里面选一个。

        取月份列表这一步要扫整张看板；只挑「交易日期」一列，比全字段读省得多。
        真实数据下几万行的扫描仍然可能超 3 秒 —— 这里刻意保持同步，是因为下拉
        月份对回填初值有依赖，异步弹卡的话得先返回一张空卡再改，交互反而更绕。
        真发现慢，再改成异步：立即弹一张「加载中」的卡，后台读完月份后 push 新卡。
        """
        if self._commission_query is None:
            return _card_response(cards.error_card("佣金查询功能未启用，请联系管理员。"))

        try:
            latest = self._commission_query.latest_period()
        except Exception:  # noqa: BLE001 - 查询失败不该让整个卡片挂掉
            logger.exception("读取看板最新月份失败 open_id=%s", sales.open_id)
            return _card_response(cards.error_card("读取月份列表失败，请稍后重试。"))

        # 月份列表：优先给最近 12 个月，避免下拉太长
        options = _recent_months(latest, count=12) if latest else []
        return _card_response(cards.commission_query_card(latest or "", options))

    def _query_commission(self, sales, form) -> P2CardActionTriggerResponse:
        """执行佣金查询。走异步：立即 ack，后台跑，结果 push。

        全表扫描 + 聚合几乎肯定超 3 秒。同步返回会看到「延时未响应」的红字，
        而服务端其实还在跑；异步能让人清楚看到「已提交」→ 独立结果卡。
        """
        if self._commission_query is None:
            return _card_response(cards.error_card("佣金查询功能未启用，请联系管理员。"))

        period = _form_text(form, cards.F_QUERY_PERIOD)
        if not _is_period(period):
            raise ValidationError(f"月份格式要是 YYYY-MM，你选/填的是「{period}」")

        target_open_id = sales.open_id
        query_service = self._commission_query
        # 领域层里定义好的展示函数，handlers 不应该自己拼字符串
        from ..domain.commission_query import summarize as summarize_commission

        def worker() -> None:
            try:
                result = query_service.query(sales, period)
            except Exception:
                logger.exception("佣金查询失败 open_id=%s period=%s", target_open_id, period)
                self._send_to_user(
                    target_open_id,
                    cards.error_card("查询佣金明细失败，请稍后重试或联系管理员。"),
                )
                return

            body = summarize_commission(result, viewer_name=sales.name)
            title = f"佣金明细  {period}"
            self._send_to_user(target_open_id, cards.commission_result_card(title, body))

        self._background(worker)

        return _card_response(
            cards.notice_card(
                "正在查询",
                f"{period} 的佣金明细正在算，结果会作为新消息推给你（通常 3-10 秒）。",
                template="blue",
            ),
            toast="已提交",
        )

    # ---------- 发消息 ----------

    def _send(self, chat_id: str, card: dict[str, Any]) -> None:
        self._send_card(receive_id=chat_id, receive_id_type="chat_id", card=card)

    def _send_to_user(self, open_id: str, card: dict[str, Any]) -> None:
        """按 open_id 主动发一条卡片消息给用户。

        供异步回调（``_submit_client``）在后台写入完成后推送结果用 —— 卡片
        callback 已经在 3 秒内 ack 掉了，这一路是新起的独立请求，飞书按 open_id
        路由到该用户和机器人的单聊会话，不需要事先记住 chat_id。
        """
        self._send_card(receive_id=open_id, receive_id_type="open_id", card=card)

    def _send_card(self, *, receive_id: str, receive_id_type: str, card: dict[str, Any]) -> None:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.error("发送卡片失败: %s %s", response.code, response.msg)


def _form_text(form: dict[str, Any], key: str) -> str:
    """从 form_value 里取一个文本项。

    ``dict.get(key, "")`` 不够：选填项没填时平台可能不给这个 key，也可能给
    ``null``。后者会让默认值失效，一路 None 传到 ``.strip()`` 才炸，而且是在
    3 秒回调里炸成一句「系统出错了」，看不出是哪个字段。
    """
    value = form.get(key)
    return "" if value is None else str(value)


def _select_value(raw: Any) -> str:
    """下拉组件的回传值可能是裸字符串，也可能包成 {"value": ...}。"""
    if isinstance(raw, dict):
        return str(raw.get("value", ""))
    return str(raw or "")


_PERIOD_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def _is_period(text: str) -> bool:
    """YYYY-MM 且月份在 01–12。字符串校验够用，不做 date 构造 —— 不接受 2026-13
    这类值就够了，日期本身不参与计算。"""
    if not _PERIOD_PATTERN.match(text or ""):
        return False
    month = int(text[5:])
    return 1 <= month <= 12


def _recent_months(anchor: str, *, count: int) -> list[str]:
    """以 ``anchor``（YYYY-MM）为最新月份，倒推 ``count`` 个月。

    包含 anchor 本身。用来限制佣金查询下拉的长度：数据可能追溯到很早，一次性
    列出几十个月对销售没意义，绝大多数查询都是「本月 / 上个月」。
    """
    if not _is_period(anchor):
        return []
    year, month = int(anchor[:4]), int(anchor[5:])
    months: list[str] = []
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return months


def _card_response(
    card: dict[str, Any], *, toast: str | None = None
) -> P2CardActionTriggerResponse:
    """按平台要求的回调响应体构造返回值。

    结构是 ``{"toast": {...}, "card": {"type": "raw", "data": <卡片 JSON>}}``。
    SDK 拿到这个对象后直接 ``JSON.marshal``，所以这里的 key 名就是最终上线的
    字段名。另外平台规定：交互前是 2.0 结构的卡片，交互后必须仍然是 2.0，
    否则报 200830 —— cards.py 里每张卡都带 ``"schema": "2.0"``。
    """
    payload: dict[str, Any] = {"card": {"type": "raw", "data": card}}
    if toast:
        payload["toast"] = {"type": "success", "content": toast}
    return P2CardActionTriggerResponse(payload)
