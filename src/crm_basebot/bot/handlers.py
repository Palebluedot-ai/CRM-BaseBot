"""机器人事件与卡片回调处理。

**每点一下都是一条新消息，被点的那张卡原样留着。**（2026-09-24 反馈：「每次处理完一个
request，譬如看完渠道，紀錄會消失」。）卡片回调的返回值会**原地替换**被点的那张卡：点
「我的渠道」，菜单就变成列表；点一条渠道，列表就变成详情；查询表单点「查询」，就变成
一张永远停在「正在查询」的卡。会话里留不下任何记录。

所以现在回调**几乎不换卡**：立即回一个空响应（或者一句 toast），真正的内容在后台算好，
作为新消息推出去（``_push``）。被点的卡原样留着，想再点还能点。只有两处原地换：

  · 渠道列表的「上一页 / 下一页」—— 翻的是同一张列表，不是做完了一件事；
  · 登记表单提交之后换成「已提交」回执 —— 表单原样留着就能再点一次提交，登记出两条
    一样的渠道。回执把填过的内容一项项列出来，记录照样在。

顺带解决了 3 秒预算：卡片回调必须 3 秒内返回，否则客户端弹「延时未响应」（客户端本地
文案，服务端拦不到）。现在回调里只剩「校验 + 回一个响应」，读表、算钱都在后台线程里。

**填错了回一句红色 toast，表单原样留着**，改一个字就能再提交 —— 不再把整张表单换成一张
报错卡，让人从头填一遍。

另一条：open_id 只从 ``data.event.operator.open_id`` 取。这个值由飞书平台签发，
客户端伪造不了。任何从 form_value 或消息文本里取身份的写法都是漏洞。

**结果卡要过 ``cards.with_menu``，导览卡不要。** 结果落在会话最底下，登记成功、查询
出结果、出错这些**结果**卡底部接上主菜单，下一件事不用往上翻。「我的渠道」那条路（列表 /
详情 / 找不到）是**导览**卡，自己带「返回列表 / 返回目录」。

鉴权失败只回一句 toast —— 名册里没有的人拿到一排按钮，点了还是同一句拒绝。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, tzinfo
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImChatAccessEventBotP2pChatEnteredV1,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from ..domain.dates import DEFAULT_BUSINESS_TIMEZONE, months_ending, ms_to_date, period_of_day
from ..domain.referral import ReferralInput, ValidationError
from ..domain.referred_client import ClientInput, validated_ai
from ..lark.values import to_number
from . import cards
from .auth import AuthError

logger = logging.getLogger(__name__)

SYSTEM_ERROR = "系统出错了，请稍后再试。管理员可以在服务端日志里看到详情。"

# 同一个人多久之内不重复自动弹菜单。进入会话的事件开得很勤（切回会话、手机上划一下
# 都可能触发），不设冷却期的话会话会被菜单卡填满。五分钟是个折中：出去办点别的再回来
# 有菜单，点开表单临时切走再切回来不会被顶掉。
GREET_COOLDOWN_SECONDS = 300.0

# 佣金查询、详情卡都看「近三个月」：含那个月在内往回三个月（domain.dates.months_ending）。
RECENT_MONTHS = 3

# 佣金查询下拉里列多少个月。数据可能追溯到很早，一次性列出几十个月对销售没意义。
QUERY_MONTH_OPTIONS = 12


def _entered_open_id(data: P2ImChatAccessEventBotP2pChatEnteredV1) -> str:
    """从「进入会话」事件里取 open_id。取不到就返回空串，让调用方安静地跳过。

    和卡片回调一样，身份只认平台签发的这个值。
    """
    event = getattr(data, "event", None)
    operator = getattr(event, "operator_id", None)
    return getattr(operator, "open_id", "") or ""


def _run_in_thread(func: Callable[[], None]) -> None:
    threading.Thread(target=func, daemon=True).start()


class BotHandlers:
    """把回调接到领域服务上。

    依赖显式注入，方便在没有飞书连接的情况下单测。

    ``ecas_query`` 同理：ECAS 是独立的第二套账（见 ``docs/ECAS.md``），没上 ECAS 的
    租户不注入它，「ECAS 返佣」按钮就回一句「未启用」。

    ``background`` 是后台任务的执行器：几乎每个回调都把真正的活丢给它，算完把结果作为
    新消息推出去（见 ``_push``）。默认起一个 daemon 线程；测试里传 ``lambda fn: fn()``
    走同步，避免线程竞态。

    ``tz`` 是业务时区：卡片上选的日期要按它落成日历日（见 ``_form_date``），「本月」
    也按它算。默认值和 ``domain/dates.py`` 里的一致，生产在 app.py 里注入
    ``BUSINESS_TIMEZONE``。
    """

    def __init__(
        self,
        *,
        client: lark.Client,
        directory,
        referrals,
        clients,
        commission_query=None,
        ecas_query=None,
        referral_history=None,
        background: Callable[[Callable[[], None]], None] = _run_in_thread,
        tz: tzinfo = DEFAULT_BUSINESS_TIMEZONE,
        greet_cooldown_seconds: float = GREET_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._client = client
        self._directory = directory
        self._referrals = referrals
        self._clients = clients
        # 可选：不注入时「佣金查询」按钮点了会回一句「暂不可用」，而不是崩。
        # 生产 app.py 一定会注入；测试有的场景不需要。
        self._commission_query = commission_query
        # 同上：没配 ECAS 那两张表时按钮点了回一句「未启用」，而不是去读一个空 table_id。
        self._ecas_query = ecas_query
        # 详情卡上的「近 3 个月」。没注入就不显示那一节，卡片其余部分照常。
        self._referral_history = referral_history
        self._background = background
        self._tz = tz
        self._greet_cooldown = greet_cooldown_seconds
        self._clock = clock
        # 业务时区的「今天」。测试里钉死一个日期，免得断言随真实日历漂。
        self._today = today or (lambda: datetime.now(self._tz).date())
        # open_id -> 上次自动弹菜单的时刻。见 on_p2p_chat_entered。
        self._greeted: dict[str, float] = {}

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

    # ---------- 进入会话：自动弹菜单 ----------

    def on_p2p_chat_entered(self, data: P2ImChatAccessEventBotP2pChatEnteredV1) -> None:
        """用户打开和机器人的单聊时，主动把主菜单推过去。

        这样「不用打字就有按钮」。飞书没有「用户打开了会话」以外更精确的信号，这个事件
        就是最接近的那一个，**但它开得很勤** —— 切回会话、手机上划一下都可能触发。
        每触发一次就推一张卡的话，会话很快被菜单卡填满，真正的结果卡反而被挤到上面去。

        所以加了冷却期：同一个人 ``greet_cooldown_seconds`` 秒内只弹一次。刚点开表单
        又切出去、过十几秒切回来，不会有一张新菜单卡把表单顶掉。

        **名册里没有的人不推。** 不请自来的一张拒绝卡对他没有任何用处；而他一旦发消息，
        现有的那条路照样会告诉他。这里只留一行日志，管理员按它登记新人。
        """
        open_id = _entered_open_id(data)
        if not open_id:
            return

        try:
            sales = self._directory.require(open_id)
        except AuthError:
            logger.info("未登记的 open_id 打开了会话：%s", open_id)
            return

        now = self._clock()
        last = self._greeted.get(open_id)
        if last is not None and now - last < self._greet_cooldown:
            return
        self._greeted[open_id] = now

        self._send_to_user(open_id, cards.menu_card(sales.name))

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
            return _toast(str(exc), kind="error")

        try:
            return self._dispatch(
                action,
                sales,
                form,
                action_value if isinstance(action_value, dict) else {},
            )
        except (ValidationError, AuthError) as exc:
            # 填错了：一句红字，卡片原样留着，改完直接再点。
            return _toast(str(exc), kind="error")
        except Exception:
            logger.exception("处理卡片动作 %s 失败", action)
            return _toast(SYSTEM_ERROR, kind="error")

    def _dispatch(
        self,
        action,
        sales,
        form,
        action_value: dict | None = None,
    ) -> P2CardActionTriggerResponse:
        action_value = action_value or {}

        if action == cards.ACTION_OPEN_MENU:
            return self._push(sales, lambda: cards.menu_card(sales.name))

        if action == cards.ACTION_OPEN_REFERRAL_FORM:
            return self._push(sales, cards.referral_form_card)

        if action == cards.ACTION_OPEN_CLIENT_FORM:
            return self._push(sales, lambda: self._client_form_card(sales))

        if action == cards.ACTION_LIST_REFERRALS:
            page = _page_index(action_value)
            return self._push(
                sales,
                lambda: cards.referral_list_card(self._referrals.list_for(sales), page=page),
            )

        if action == cards.ACTION_REFERRAL_PAGE:
            # 翻页是这张卡里唯一原地换的：翻的是同一张列表，不是做完了一件事。
            # 读的是一张一百来行的渠道表，一个请求，压得进 3 秒。
            return _card_response(
                cards.referral_list_card(
                    self._referrals.list_for(sales), page=_page_index(action_value)
                )
            )

        if action == cards.ACTION_OPEN_REFERRAL:
            referral_no = _referral_no(action_value)
            return self._push(
                sales,
                lambda: self._referral_detail_card(sales, referral_no),
                toast=f"正在打开 {referral_no}" if referral_no else None,
            )

        if action == cards.ACTION_SUBMIT_REFERRAL:
            return self._submit_referral(sales, form)

        if action == cards.ACTION_SUBMIT_CLIENT:
            return self._submit_client(sales, form)

        if action == cards.ACTION_OPEN_COMMISSION_QUERY:
            return self._open_commission_query(sales)

        if action == cards.ACTION_QUERY_COMMISSION:
            return self._query_commission(sales, form)

        if action == cards.ACTION_OPEN_ECAS_QUERY:
            return self._open_ecas_query(sales)

        if action == cards.ACTION_OPEN_AI_FORM:
            return self._push(sales, cards.ai_form_card)

        if action == cards.ACTION_SUBMIT_AI:
            return self._submit_ai(sales, form)

        if action == cards.ACTION_QUERY_ECAS:
            return self._query_ecas(sales, form)

        logger.warning("未知的卡片动作: %r", action)
        return self._push(
            sales, lambda: cards.with_menu(cards.error_card("这个操作我不认识，请重新开始。"))
        )

    def _push(
        self,
        sales,
        build: Callable[[], dict[str, Any]],
        *,
        toast: str | None = None,
        failure: str = SYSTEM_ERROR,
    ) -> P2CardActionTriggerResponse:
        """在后台把 ``build()`` 出来的卡作为**新消息**发给这个人；回调立即返回，不换卡。

        ``build`` 里读表、算钱都行 —— 它不在 3 秒预算里。它抛出来的异常变成一张带菜单
        的报错卡，同样作为新消息发出去：人点了一下，总得看到点什么。
        """
        target = sales.open_id

        def worker() -> None:
            try:
                card = build()
            except (ValidationError, AuthError) as exc:
                card = cards.with_menu(cards.error_card(str(exc)))
            except Exception:
                logger.exception("生成卡片失败 open_id=%s", target)
                card = cards.with_menu(cards.error_card(failure))
            self._send_to_user(target, card)

        self._background(worker)
        return _toast(toast, kind="info") if toast else _no_change()

    def _unavailable(self, sales, what: str) -> P2CardActionTriggerResponse:
        """没配这项功能的租户点了按钮：推一句「未启用」，而不是去读一个空 table_id。"""
        return self._push(
            sales, lambda: cards.with_menu(cards.error_card(f"{what}未启用，请联系管理员。"))
        )

    def _client_form_card(self, sales) -> dict[str, Any]:
        options = self._referrals.list_for(sales)
        card = cards.client_form_card(options)
        # 没有渠道时返回的是一张「还不能登记客户」的提示卡，不是表单 ——
        # 那种情况下人接着就想点「登记新渠道」，菜单要在。
        return card if options else cards.with_menu(card)

    def _referral_detail_card(self, sales, referral_no: str) -> dict[str, Any]:
        """详情卡。近 3 个月和客户名单要多读几张表，所以单独一个方法。

        编号来自按钮回传，客户端改得了：先过 ``get_for``（按 owned_records 鉴权），拿到
        的 record_id 才往下传。别人的渠道在这一步就变成「找不到」。

        **近 3 个月和客户名单各自包着 try**：渠道本身的资料已经在手上了，为了附加信息
        把整张卡换成一句报错，是拿有用的东西去换没用的。取不到就那一节不显示，并在卡片
        上说明。
        """
        detail = self._referrals.get_for(sales, referral_no)
        if detail is None:
            return cards.referral_missing_card()

        months = None
        history_failed = False
        if self._referral_history is not None:
            try:
                months = self._referral_history.recent(detail.record_id, today=self._today())
            except Exception:  # noqa: BLE001 - 见 docstring
                logger.exception("读取渠道 %s 的近几个月失败", detail.no)
                history_failed = True

        clients = None
        try:
            clients = self._clients.names_for_referral(detail.record_id)
        except Exception:  # noqa: BLE001 - 见 docstring
            logger.exception("读取渠道 %s 名下的客户失败", detail.no)

        return cards.referral_detail_card(
            no=detail.no,
            name=detail.name,
            start_date=detail.start_date,
            rate=detail.rate,
            payout=detail.payout,
            email=detail.email,
            submitted_on=detail.submitted_on,
            months=months,
            client_names=clients,
            history_failed=history_failed,
        )

    def _submit_referral(self, sales, form) -> P2CardActionTriggerResponse:
        """校验在回调里做（填错了回 toast，表单留着）；写入在后台做，结果推新消息。

        写入要 4-5 个串行往返（锁里扫编号、写审计、写记录、回读），和客户登记一样会撑破
        3 秒预算。表单原地换成「已提交」回执：记录留着，又不能再点一次提交。
        """
        # 输入框的标签就写着「分佣比例 (%)」，照着填「20%」是很自然的事。
        # 不去掉这个百分号，to_number 会返回 None，人看到的是「要填数字」——
        # 而他明明填的就是数字。
        rate = to_number(_form_text(form, cards.F_REFERRAL_RATE).rstrip("%").strip())
        if rate is None:
            raise ValidationError("分佣比例要填数字，例如 20")

        # 取不到日期时给人话的报错，而不是让 date_to_ms 在领域层炸成「系统出错了」。
        start_date = _form_date(form, cards.F_REFERRAL_START_DATE, tz=self._tz)
        if start_date is None:
            raise ValidationError("开始日期要选一个日期")

        # 下拉给的中文标签不回传，回传的是 value，正好是写进 Base 的原文（Monthly /
        # Quarterly）；不是这两个值的话，validated() 会拦下来。
        data = ReferralInput(
            name=_form_text(form, cards.F_REFERRAL_NAME),
            email=_form_text(form, cards.F_REFERRAL_EMAIL),
            start_date=start_date,
            commission_rate=rate,
            payout_frequency=_select_value(form.get(cards.F_REFERRAL_PAYOUT)),
        ).validated()
        target = sales.open_id

        def worker() -> None:
            try:
                referral_no, _ = self._referrals.create(sales, data)
            except (ValidationError, AuthError) as exc:
                self._send_to_user(target, cards.with_menu(cards.error_card(str(exc))))
                return
            except Exception:
                logger.exception("登记渠道失败 open_id=%s", target)
                self._send_to_user(target, cards.with_menu(cards.error_card(SYSTEM_ERROR)))
                return

            self._send_to_user(
                target,
                cards.with_menu(
                    cards.success_card(
                        "渠道已登记",
                        f"**{data.name}** 编号 **{referral_no}**，已生效。\n\n"
                        f"开始日期 {data.start_date.isoformat()}，"
                        f"结算频率 {data.payout_frequency}。\n\n"
                        f"分佣比例 {_percent(data.commission_rate)}。",
                    )
                ),
            )

        self._background(worker)

        return _card_response(
            cards.submitted_card(
                "登记新渠道",
                [
                    ("渠道名称", data.name),
                    ("邮箱", data.email),
                    ("开始日期", data.start_date.isoformat()),
                    ("分佣比例", _percent(data.commission_rate)),
                    ("结算频率", data.payout_frequency),
                ],
            ),
            toast="已提交",
        )

    def _submit_client(self, sales, form) -> P2CardActionTriggerResponse:
        # 这条路径要 4 个串行往返：扫渠道表确认归属、扫客户表查 UID 重复、写审计、
        # 写客户。真机测下来经常撑爆 3 秒预算，客户端就会弹「延时未响应」——
        # 尽管服务端其实已经写成功了。
        #
        # 所以格式校验在这里做（填错回 toast，表单留着），归属和查重这两个要读表的
        # 检查连同写入一起放到后台，结果推一条新消息。
        client_input = ClientInput(
            uid=_form_text(form, cards.F_CLIENT_UID),
            name=_form_text(form, cards.F_CLIENT_NAME),
            referral_no=_select_value(form.get(cards.F_CLIENT_REFERRAL)),
            ai_status=_select_value(form.get(cards.F_CLIENT_AI_STATUS)),
            ai_date=_form_date(form, cards.F_CLIENT_AI_DATE, tz=self._tz),
        ).validated()
        target = sales.open_id

        def worker() -> None:
            try:
                self._clients.create(sales, client_input)
            except (ValidationError, AuthError) as exc:
                self._send_to_user(target, cards.with_menu(cards.error_card(str(exc))))
                return
            except Exception:
                logger.exception("异步登记客户失败 open_id=%s", target)
                self._send_to_user(target, cards.with_menu(cards.error_card(SYSTEM_ERROR)))
                return

            self._send_to_user(
                target,
                cards.with_menu(
                    cards.success_card(
                        "客户已登记",
                        f"**{client_input.name}** 已挂到渠道 **{client_input.referral_no}**。\n\n"
                        f"AI 状态：{_ai_text(client_input.ai_status, client_input.ai_date)}",
                    )
                ),
            )

        self._background(worker)

        return _card_response(
            cards.submitted_card(
                "登记新客户",
                [
                    ("所属渠道", client_input.referral_no),
                    ("客户UID", client_input.uid),
                    ("客户名称", client_input.name),
                    ("AI状态", _ai_text(client_input.ai_status, client_input.ai_date)),
                ],
            ),
            toast="已提交",
        )

    def _submit_ai(self, sales, form) -> P2CardActionTriggerResponse:
        """补 / 改客户的 AI 状态。和登记一样：格式在回调里校验，改写在后台，结果推新消息。"""
        uid = _form_text(form, cards.F_AI_UID).strip()
        if not uid.isdigit():
            raise ValidationError(f"客户UID 应该是纯数字，你填的是「{uid}」")
        status, ai_date = validated_ai(
            _select_value(form.get(cards.F_AI_STATUS)),
            _form_date(form, cards.F_AI_DATE, tz=self._tz),
        )
        target = sales.open_id

        def worker() -> None:
            try:
                name, referral_no = self._clients.update_ai(sales, uid, status, ai_date)
            except (ValidationError, AuthError) as exc:
                self._send_to_user(target, cards.with_menu(cards.error_card(str(exc))))
                return
            except Exception:
                logger.exception("更新客户 AI 状态失败 open_id=%s", target)
                self._send_to_user(target, cards.with_menu(cards.error_card(SYSTEM_ERROR)))
                return

            self._send_to_user(
                target,
                cards.with_menu(
                    cards.success_card(
                        "AI 状态已更新",
                        f"**{name}**（{referral_no}）：{_ai_text(status, ai_date)}",
                    )
                ),
            )

        self._background(worker)

        return _card_response(
            cards.submitted_card(
                "更新客户AI状态",
                [("客户UID", uid), ("AI状态", _ai_text(status, ai_date))],
            ),
            toast="已提交",
        )

    def _open_commission_query(self, sales) -> P2CardActionTriggerResponse:
        """打开「佣金查询」表单：下拉列本月往回 12 个月，预选本月。

        以前是扫一遍看板找「最新有数据的月份」来预选 —— 上万行扫下来要好几秒，只为了
        一个几乎总是等于本月的值。月初看板还没有本月数据时，选本月照样列得出上两个月。
        """
        if self._commission_query is None:
            return self._unavailable(sales, "佣金查询功能")

        current = period_of_day(self._today())
        options = list(reversed(months_ending(current, QUERY_MONTH_OPTIONS)))
        return self._push(sales, lambda: cards.commission_query_card(current, options))

    def _query_commission(self, sales, form) -> P2CardActionTriggerResponse:
        """执行佣金查询：选中的月份和前两个月。后台跑，结果推新消息，表单原样留着。"""
        if self._commission_query is None:
            return self._unavailable(sales, "佣金查询功能")

        period = _form_text(form, cards.F_QUERY_PERIOD).strip()
        if not _is_period(period):
            raise ValidationError(f"月份格式要是 YYYY-MM，你选/填的是「{period}」")

        periods = months_ending(period, RECENT_MONTHS)
        current = period_of_day(self._today())
        query_service = self._commission_query

        def build() -> dict[str, Any]:
            result = query_service.query(sales, periods)
            return cards.with_menu(
                cards.commission_result_card(result, viewer_name=sales.name, current_period=current)
            )

        return self._push(
            sales,
            build,
            toast=f"正在查询 {periods[0]} ~ {periods[-1]}",
            failure="查询佣金明细失败，请稍后重试或联系管理员。",
        )

    # ---------- ECAS 返佣 ----------
    #
    # 和交易佣金那两个方法是**平行**的两条路，不是共用的一条：读的表不同、比例来源
    # 不同、结果进不同的汇总表（见 domain/ecas.py 开头）。长得像是因为交互一样，
    # 不是因为底下是同一件事。

    def _open_ecas_query(self, sales) -> P2CardActionTriggerResponse:
        """打开「ECAS 返佣」表单：从申请表取这名销售有数据的月份。

        月份列表按**本人能看到的**算 —— 下拉里列一个他点进去必然是空的月份，
        只会让人以为系统坏了。
        """
        if self._ecas_query is None:
            return self._unavailable(sales, "ECAS 返佣查询")

        query_service = self._ecas_query

        def build() -> dict[str, Any]:
            periods = query_service.periods_for(sales)
            latest = periods[-1] if periods else ""
            return cards.ecas_query_card(latest, periods)

        return self._push(sales, build, failure="读取 ECAS 月份列表失败，请稍后重试。")

    def _query_ecas(self, sales, form) -> P2CardActionTriggerResponse:
        """执行 ECAS 返佣查询。后台跑，结果推新消息，表单原样留着。"""
        if self._ecas_query is None:
            return self._unavailable(sales, "ECAS 返佣查询")

        period = _form_text(form, cards.F_ECAS_PERIOD).strip()
        if not _is_period(period):
            raise ValidationError(f"月份格式要是 YYYY-MM，你选/填的是「{period}」")

        query_service = self._ecas_query
        from ..domain.ecas_query import summarize as summarize_ecas

        def build() -> dict[str, Any]:
            rows = query_service.query(sales, period)
            body = summarize_ecas(rows, period=period, viewer_name=sales.name)
            return cards.with_menu(cards.ecas_result_card(f"ECAS 返佣  {period}", body))

        return self._push(
            sales, build, toast=f"正在查询 {period}", failure="查询 ECAS 返佣失败，请稍后重试。"
        )

    # ---------- 发消息 ----------

    def _send(self, chat_id: str, card: dict[str, Any]) -> None:
        self._send_card(receive_id=chat_id, receive_id_type="chat_id", card=card)

    def _send_to_user(self, open_id: str, card: dict[str, Any]) -> None:
        """按 open_id 主动发一条卡片消息给用户。

        卡片回调已经立即返回了，这一路是新起的独立请求，飞书按 open_id 路由到该用户
        和机器人的单聊会话，不需要事先记住 chat_id。
        """
        self._send_card(receive_id=open_id, receive_id_type="open_id", card=card)

    def _send_card(self, *, receive_id: str, receive_id_type: str, card: dict[str, Any]) -> None:
        """发一张卡。带表格的卡被拒时，换成列点版再发一次。

        表格组件是这套卡片里最新、字段最多的组件，线下只能照文档和 SDK 的写法对，
        真机上万一不收，**整条消息**就发不出去 —— 人点了按钮什么都看不到。所以被拒时
        同样的内容换成列点（``cards.flatten_tables``）再发一次，并在日志里留下平台回的
        错误码，照着改就是。
        """
        response = self._client.im.v1.message.create(_message(receive_id, receive_id_type, card))
        if response.success():
            return
        logger.error("发送卡片失败: %s %s", response.code, response.msg)
        if not cards.has_tables(card):
            return

        flat = cards.flatten_tables(card)
        response = self._client.im.v1.message.create(_message(receive_id, receive_id_type, flat))
        if response.success():
            logger.warning("带表格的卡被拒，已改发列点版。上面那行是平台回的错误。")
        else:
            logger.error("列点版也没发出去: %s %s", response.code, response.msg)


def _message(receive_id: str, receive_id_type: str, card: dict[str, Any]) -> CreateMessageRequest:
    return (
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


def _ai_text(status: str, ai_date: date | None) -> str:
    """「升级为AI（2026-08-24 起）」这样的一句。"""
    return f"{status}（{ai_date.isoformat()} 起）" if ai_date else status


def _percent(rate: float) -> str:
    """20.0 -> "20%"，12.5 -> "12.5%"。别让卡片上出现 20.0%。"""
    return f"{rate:g}%"


def _page_index(action_value: dict[str, Any]) -> int:
    """翻页按钮带回的页码。不是非负整数就当第一页，别让回调因为一个坏页码炸成系统错误。"""
    raw = action_value.get("page", 0)
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw if raw > 0 else 0
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0


def _referral_no(action_value: dict[str, Any]) -> str:
    raw = action_value.get("referral_no")
    return "" if raw is None else str(raw).strip()


def _form_text(form: dict[str, Any], key: str) -> str:
    """从 form_value 里取一个文本项。

    ``dict.get(key, "")`` 不够：选填项没填时平台可能不给这个 key，也可能给
    ``null``。后者会让默认值失效，一路 None 传到 ``.strip()`` 才炸，而且是在
    回调里炸成一句「系统出错了」，看不出是哪个字段。
    """
    value = form.get(key)
    return "" if value is None else str(value)


def _select_value(raw: Any) -> str:
    """下拉组件的回传值可能是裸字符串，也可能包成 ``{"value": ...}``。"""
    if isinstance(raw, dict):
        return str(raw.get("value", ""))
    return str(raw or "")


# 日期选择器的真实回传：``2026-08-01 +0800`` —— 日历日，加上选的人那台设备的时区。
# 也认不带时区的 ``2026-08-01`` 和带时刻的 ``2026-08-01 10:00 +0800``（日期时间选择器）。
_PICKER_DATE_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2})?)?(?:\s*(?:[+-]\d{2}:?\d{2}|Z))?$"
)


def _form_date(form: dict[str, Any], key: str, *, tz: tzinfo) -> date | None:
    """从 form_value 里取日期选择器的值，转成日历日；取不到返回 None。

    飞书日期选择器回传的是 ``"2026-08-01 +0800"`` 这样的文本（「卡片回传交互」文档）。
    **取的是前面那个日历日**：那就是人在选择器里点的那一天；后缀只是他设备的时区，
    不改变他选的是哪一天。

    这里原先只认毫秒时间戳和纯 ``YYYY-MM-DD``，带后缀的真实回传两样都不是 ——
    选了日期照样报「开始日期要选一个日期」，登记新渠道一次都没成功过（2026-09-24
    反馈）。毫秒时间戳仍然认（按业务时区 ``tz`` 取日历日），也认 ``{"value": ...}``
    包一层的形态。

    解析不出来一律返回 None，由调用方决定报什么错 —— 校验失败要说人话。
    """
    raw = form.get(key)
    if isinstance(raw, dict):
        raw = raw.get("value")
    if raw is None:
        return None

    text = str(raw).strip()
    if not text:
        return None

    matched = _PICKER_DATE_PATTERN.match(text)
    if matched:
        try:
            return date.fromisoformat(matched.group(1))
        except ValueError:  # 形似而非法，比如 2026-02-30
            return None

    try:
        ms = int(float(text))
    except ValueError:
        return None
    return ms_to_date(ms, tz=tz)


_PERIOD_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def _is_period(text: str) -> bool:
    """YYYY-MM 且月份在 01–12。字符串校验够用，不做 date 构造 —— 不接受 2026-13
    这类值就够了，日期本身不参与计算。"""
    if not _PERIOD_PATTERN.match(text or ""):
        return False
    month = int(text[5:])
    return 1 <= month <= 12


def _no_change() -> P2CardActionTriggerResponse:
    """空响应：飞书收到 ``{}`` 就不动那张卡。"""
    return P2CardActionTriggerResponse({})


def _toast(content: str, *, kind: str = "info") -> P2CardActionTriggerResponse:
    """只弹一句提示，不换卡。``kind`` 是平台的 info / success / error / warning。"""
    return P2CardActionTriggerResponse({"toast": {"type": kind, "content": content}})


def _card_response(
    card: dict[str, Any], *, toast: str | None = None
) -> P2CardActionTriggerResponse:
    """原地换卡。现在只有翻页和「已提交」回执用它，见模块开头。

    结构是 ``{"toast": {...}, "card": {"type": "raw", "data": <卡片 JSON>}}``。
    SDK 拿到这个对象后直接 ``JSON.marshal``，所以这里的 key 名就是最终上线的
    字段名。另外平台规定：交互前是 2.0 结构的卡片，交互后必须仍然是 2.0，
    否则报 200830 —— cards.py 里每张卡都带 ``"schema": "2.0"``。
    """
    payload: dict[str, Any] = {"card": {"type": "raw", "data": card}}
    if toast:
        payload["toast"] = {"type": "success", "content": toast}
    return P2CardActionTriggerResponse(payload)
