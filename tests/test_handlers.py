"""卡片回调的入口行为。

这一层没有任何单测的时候最容易漏掉两类事：

1. **回调响应的结构**。飞书对响应体的形状有硬要求，写错了平台回 200672/200673，
   用户看到「出错了，请稍后重试」，而服务端这边一切正常、什么都不知道。所以这里
   不是断言我们构造的那个 dict，而是断言 **SDK 序列化之后真正发出去的 JSON**。
2. **表单值的边界**。选填项没填时平台可能不给 key，也可能给 null；下拉的回传值
   可能是裸字符串也可能包一层。这些都到不了业务代码，得在入口挡住。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import lark_oapi as lark
import pytest
from lark_oapi.core.exception import UnmarshalException
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from crm_basebot.bot import cards
from crm_basebot.bot.auth import SalesDirectory
from crm_basebot.bot.handlers import BotHandlers
from crm_basebot.domain import schema
from crm_basebot.domain.audit import AuditLog
from crm_basebot.domain.dates import DEFAULT_BUSINESS_TIMEZONE, date_to_ms
from crm_basebot.domain.referral import ReferralService
from crm_basebot.domain.referred_client import ReferredClientService

from .conftest import TBL_AUDIT, TBL_CLIENT, TBL_REFERRAL, TBL_SALES

ALICE = "ou_alice000000000000000000000000"
STRANGER = "ou_stranger0000000000000000000"
UID = "577809207768677761"

# 日期选择器回传的是毫秒时间戳字符串，按真实回调的形状造值
START_DATE = date(2026, 1, 15)
START_DATE_MS = str(date_to_ms(START_DATE, tz=DEFAULT_BUSINESS_TIMEZONE))


class _StubResponse:
    code = 0
    msg = "ok"

    def success(self) -> bool:
        return True


class StubLarkClient:
    """卡片回调走同步分支时根本不碰 client；``_submit_client`` 走异步分支后
    需要 ``im.v1.message.create`` 推结果消息，所以这里给个最小可用的 stub，
    把最后一次调用记下来供断言。"""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.im = self  # type: ignore[assignment]
        self.v1 = self  # type: ignore[assignment]
        self.message = self  # type: ignore[assignment]

    def create(self, request: Any) -> _StubResponse:
        self.sent.append(request)
        return _StubResponse()


@pytest.fixture
def handlers(fake_bitable):
    fake_bitable.table(TBL_SALES).add_existing(
        {
            schema.SALES_OPEN_ID: ALICE,
            schema.SALES_NAME: "Alice",
            schema.SALES_ROLE: schema.ROLE_SALES,
            schema.SALES_STATUS: schema.SALES_STATUS_ACTIVE,
        }
    )
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    return BotHandlers(
        client=StubLarkClient(),
        directory=SalesDirectory(fake_bitable, TBL_SALES),
        referrals=ReferralService(fake_bitable, TBL_REFERRAL, audit),
        clients=ReferredClientService(fake_bitable, TBL_CLIENT, TBL_REFERRAL, audit),
        # 同步执行后台任务，避免线程竞态干扰断言
        background=lambda fn: fn(),
    )


def trigger(
    action: str,
    *,
    open_id: str = ALICE,
    form: dict[str, Any] | None = None,
    value: Any = None,
) -> P2CardActionTrigger:
    """照平台文档里的回调体造一个事件，走 SDK 自己的反序列化。

    手搓 mock 对象的话，SDK 模型改了名字我们不会知道；从 dict 构造能顺带钉住
    ``event.operator.open_id`` / ``event.action.form_value`` 这些取值路径。
    """
    return P2CardActionTrigger(
        {
            "schema": "2.0",
            "header": {
                "event_id": "evt-1",
                "event_type": "card.action.trigger",
                "app_id": "cli_test",
            },
            "event": {
                "operator": {"open_id": open_id, "tenant_key": "t1"},
                "token": "c-token",
                "action": {
                    "tag": "button",
                    "value": {"action": action} if value is None else value,
                    "form_value": form or {},
                    "name": "btn",
                },
                "host": "im_message",
            },
        }
    )


def marshalled(response) -> dict[str, Any]:
    """SDK 实际写回长连接的那串 JSON。"""
    return json.loads(lark.JSON.marshal(response))


def referral_form(**overrides) -> dict[str, Any]:
    form = {
        cards.F_REFERRAL_NAME: "北极星资本",
        cards.F_REFERRAL_EMAIL: "ops@polaris.example",
        cards.F_REFERRAL_START_DATE: START_DATE_MS,
        cards.F_REFERRAL_RATE: "20",
        cards.F_REFERRAL_PAYOUT: schema.PAYOUT_MONTHLY,
    }
    form.update(overrides)
    return form


def submit_referral(handlers, form: dict[str, Any] | None = None) -> dict[str, Any]:
    event = trigger(cards.ACTION_SUBMIT_REFERRAL, form=referral_form() if form is None else form)
    return marshalled(handlers.on_card_action(event))


# ---------- 回调响应的结构 ----------


def test_回调响应序列化成平台要求的结构(handlers):
    """``{"toast": {...}, "card": {"type": "raw", "data": <卡片 JSON>}}``

    这三个 key 的名字和嵌套关系是平台定的，改错一个就是 200672。
    """
    response = handlers.on_card_action(trigger(cards.ACTION_OPEN_REFERRAL_FORM))
    payload = marshalled(response)

    assert set(payload) <= {"toast", "card"}
    assert payload["card"]["type"] == "raw"
    assert payload["card"]["data"]["schema"] == "2.0"
    assert payload["card"]["data"]["header"]["title"]["content"] == "登记新渠道"


def test_成功时带上_toast(handlers):
    payload = submit_referral(handlers)

    # type 只能是 info / success / error / warning
    assert payload["toast"]["type"] == "success"
    assert "R001" in payload["toast"]["content"]


def _card_buttons(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "button":
                (callback,) = [b for b in node["behaviors"] if b["type"] == "callback"]
                found.append((node["text"]["content"], callback["value"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload["card"]["data"])
    return found


def test_返回目录换回主菜单(handlers):
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_OPEN_MENU)))

    assert "toast" not in payload
    assert payload["card"]["data"]["header"]["title"]["content"] == "渠道佣金助手"
    actions = {value["action"] for _, value in _card_buttons(payload)}
    assert cards.ACTION_LIST_REFERRALS in actions
    assert cards.ACTION_OPEN_MENU not in actions


def test_渠道列表翻到第二页(handlers, monkeypatch):
    """页大小生产是 60，测试里调小 —— 要测的是「页码传得下去」，不是六十条数据。"""
    monkeypatch.setattr(cards, "REFERRAL_PAGE_SIZE", 2)
    for index in range(3):
        submit_referral(handlers, referral_form(**{cards.F_REFERRAL_NAME: f"渠道{index + 1}"}))

    payload = marshalled(
        handlers.on_card_action(
            trigger(
                cards.ACTION_LIST_REFERRALS,
                value={"action": cards.ACTION_LIST_REFERRALS, "page": 1},
            )
        )
    )
    opened = [value["referral_no"] for _, value in _card_buttons(payload) if "referral_no" in value]
    assert opened == ["R003"]


def test_点进自己的渠道看到特别信息(handlers):
    submit_referral(handlers)
    payload = marshalled(
        handlers.on_card_action(
            trigger(
                "",
                value={"action": cards.ACTION_OPEN_REFERRAL, "referral_no": "R001"},
            )
        )
    )
    card = payload["card"]["data"]
    assert card["header"]["title"]["content"] == "北极星资本"
    text = json.dumps(card, ensure_ascii=False)
    assert "特别信息" in text
    assert "地址：未填写" in text
    assert "收款信息：未填写" in text
    assert "分佣比例：20%" in text
    assert ("返回列表", {"action": cards.ACTION_LIST_REFERRALS}) in _card_buttons(payload)


def test_点进别人的渠道被拒绝(fake_bitable, handlers):
    fake_bitable.table(TBL_REFERRAL).add_existing(
        {
            schema.REFERRAL_NO: "R099",
            schema.REFERRAL_NAME: "别人的渠道",
            schema.REFERRAL_OWNER_OPEN_ID: STRANGER,
            schema.REFERRAL_ADDRESS: "不该被看到的地址",
        }
    )
    payload = marshalled(
        handlers.on_card_action(
            trigger(
                "",
                value={"action": cards.ACTION_OPEN_REFERRAL, "referral_no": "R099"},
            )
        )
    )
    card = payload["card"]["data"]
    text = json.dumps(card, ensure_ascii=False)
    assert card["header"]["title"]["content"] == "找不到这个渠道"
    assert "别人的渠道" not in text
    assert "不该被看到的地址" not in text
    actions = {value["action"] for _, value in _card_buttons(payload)}
    assert actions == {cards.ACTION_LIST_REFERRALS, cards.ACTION_OPEN_MENU}


def test_没有_toast_时不发空字段(handlers):
    """``toast: null`` 会被 SDK 的 filter_null 抹掉，这里钉住这个前提。"""
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_LIST_REFERRALS)))
    assert "toast" not in payload


def test_交互后仍然是_2_0_结构的卡片(handlers):
    """平台规定 2.0 卡片交互后不能退回 1.0，否则报 200830。"""
    for action in (
        cards.ACTION_OPEN_REFERRAL_FORM,
        cards.ACTION_OPEN_CLIENT_FORM,
        cards.ACTION_LIST_REFERRALS,
        "谁也不认识的动作",
    ):
        payload = marshalled(handlers.on_card_action(trigger(action)))
        assert payload["card"]["data"]["schema"] == "2.0"


# ---------- 表单值的边界 ----------


def test_文本项回传_null_不会把回调打挂(handlers):
    """邮箱是选填的。平台对没填的项可能给 null，而不是干脆不给这个 key。

    ``form.get(key, "")`` 挡不住 null —— 默认值只在 key 缺失时生效。None 一路
    传到 ``.strip()`` 才炸，在 3 秒回调里就是一句「系统出错了」，看不出是哪个字段。
    """
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_EMAIL: None}))

    assert payload["toast"]["type"] == "success"
    assert payload["card"]["data"]["header"]["title"]["content"] == "渠道已登记"


def test_文本项整个缺失也能提交(handlers):
    form = referral_form()
    del form[cards.F_REFERRAL_EMAIL]

    assert submit_referral(handlers, form)["toast"]["type"] == "success"


def _referral_fields(fake_bitable) -> dict[str, Any]:
    """渠道表收到的第一份写入 payload。"""
    (_, written) = next(
        (table_id, fields) for table_id, fields in fake_bitable.writes if table_id == TBL_REFERRAL
    )
    return written


def test_日期时间戳落成业务时区的日历日(fake_bitable, handlers):
    """选择器给的是毫秒时间戳，写进 Base 的是业务时区那一天的零点，和导入脚本同口径。"""
    submit_referral(handlers)

    assert _referral_fields(fake_bitable)[schema.REFERRAL_START_DATE] == date_to_ms(
        START_DATE, tz=DEFAULT_BUSINESS_TIMEZONE
    )


def test_日期包成字典也认(fake_bitable, handlers):
    submit_referral(
        handlers, referral_form(**{cards.F_REFERRAL_START_DATE: {"value": START_DATE_MS}})
    )

    assert _referral_fields(fake_bitable)[schema.REFERRAL_START_DATE] == date_to_ms(
        START_DATE, tz=DEFAULT_BUSINESS_TIMEZONE
    )


def test_日期回传_YYYY_MM_DD_文本也认(fake_bitable, handlers):
    """少一种「明明填了合法日期却报错」的情况。"""
    submit_referral(handlers, referral_form(**{cards.F_REFERRAL_START_DATE: "2026-01-15"}))

    assert _referral_fields(fake_bitable)[schema.REFERRAL_START_DATE] == date_to_ms(
        START_DATE, tz=DEFAULT_BUSINESS_TIMEZONE
    )


@pytest.mark.parametrize("missing", [None, "", "不是日期"])
def test_日期取不到时给出人话报错(handlers, missing):
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_START_DATE: missing}))

    assert payload["card"]["data"]["header"]["title"]["content"] == "没能完成"
    assert "开始日期" in json.dumps(payload, ensure_ascii=False)


def test_结算频率原样写进_Base(fake_bitable, handlers):
    submit_referral(handlers, referral_form(**{cards.F_REFERRAL_PAYOUT: schema.PAYOUT_QUARTERLY}))

    assert _referral_fields(fake_bitable)[schema.REFERRAL_PAYOUT] == schema.PAYOUT_QUARTERLY


def test_结算频率包成字典也认(fake_bitable, handlers):
    """和客户登记的渠道下拉一样：回传值可能是裸字符串，也可能包一层。"""
    submit_referral(
        handlers,
        referral_form(**{cards.F_REFERRAL_PAYOUT: {"value": schema.PAYOUT_QUARTERLY}}),
    )

    assert _referral_fields(fake_bitable)[schema.REFERRAL_PAYOUT] == schema.PAYOUT_QUARTERLY


@pytest.mark.parametrize("bad", [None, "", "按月", "monthly"])
def test_结算频率不是模板原文时给出人话报错(handlers, bad):
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_PAYOUT: bad}))

    assert payload["card"]["data"]["header"]["title"]["content"] == "没能完成"
    assert "结算频率" in json.dumps(payload, ensure_ascii=False)


def test_必填项回传_null_给出人话报错(handlers):
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_NAME: None}))

    assert payload["card"]["data"]["header"]["title"]["content"] == "没能完成"
    assert "渠道名称" in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("typed", ["20", " 20 ", "20%", "20 %"])
def test_比例带百分号也认(handlers, typed):
    """输入框标签就写着「分佣比例 (%)」，照着填 20% 的人不会少。"""
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_RATE: typed}))
    assert payload["toast"]["type"] == "success"


def test_比例填了不是数字给出人话报错(handlers):
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_RATE: "两成"}))
    assert "分佣比例" in json.dumps(payload, ensure_ascii=False)


def test_下拉回传裸字符串和包字典两种形态都认(handlers):
    handlers.on_card_action(trigger(cards.ACTION_SUBMIT_REFERRAL, form=referral_form()))

    plain = handlers.on_card_action(
        trigger(
            cards.ACTION_SUBMIT_CLIENT,
            form={
                cards.F_CLIENT_UID: UID,
                cards.F_CLIENT_NAME: "普罗米修斯资本",
                cards.F_CLIENT_REFERRAL: "R001",
            },
        )
    )
    assert marshalled(plain)["toast"]["type"] == "success"

    wrapped = handlers.on_card_action(
        trigger(
            cards.ACTION_SUBMIT_CLIENT,
            form={
                cards.F_CLIENT_UID: "577809207768677762",
                cards.F_CLIENT_NAME: "普罗米修斯投资",
                cards.F_CLIENT_REFERRAL: {"value": "R001"},
            },
        )
    )
    assert marshalled(wrapped)["toast"]["type"] == "success"


def test_回传值必须是对象不能是字符串():
    """平台允许 ``action.value`` 是裸字符串，但 lark-oapi 反序列化时会直接抛异常。

    也就是说这种回调**根本到不了我们的 handler**：SDK 在 ``JSON.unmarshal`` 那步
    就炸，长连接客户端捕获后回一个 500，用户看到「出错了」，服务端只有一行
    看不出所以然的 handle message failed。

    所以 cards.py 里每个 ``behaviors[].value`` 都必须是 object。这条测试就是那个
    约束的守门人 —— 哪天有人图省事写成 ``"value": "submit"``，下一条测试会红。
    """
    with pytest.raises(UnmarshalException):
        trigger("", value="纯字符串")


def test_所有卡片的回传值都是对象():
    from .test_cards import all_cards, walk

    for name, card in all_cards():
        for node in walk(card):
            for behavior in node.get("behaviors") or []:
                if behavior.get("type") != "callback":
                    continue
                assert isinstance(behavior["value"], dict), f"{name} 里有字符串形态的回传值"


# ---------- 身份 ----------


def test_名册外的人被拒且拿到_2_0_卡片(handlers):
    payload = marshalled(
        handlers.on_card_action(trigger(cards.ACTION_OPEN_REFERRAL_FORM, open_id=STRANGER))
    )
    assert payload["card"]["data"]["schema"] == "2.0"
    assert payload["card"]["data"]["header"]["template"] == "red"


def test_归属取自回调而不是表单(fake_bitable, handlers):
    """表单里塞一个别人的 open_id，写进去的归属必须还是回调里那个人。"""
    handlers.on_card_action(
        trigger(
            cards.ACTION_SUBMIT_REFERRAL,
            form=referral_form(**{"登记人OpenID": STRANGER, "归属销售": STRANGER}),
        )
    )

    (_, written) = next(
        (table_id, fields) for table_id, fields in fake_bitable.writes if table_id == TBL_REFERRAL
    )
    assert written[schema.REFERRAL_OWNER_OPEN_ID] == ALICE
    assert written[schema.REFERRAL_OWNER] == [{"id": ALICE}]


# ---------- 做完一件事之后，下一轮入口要在原地 ----------
#
# 卡片回调的返回值是**原地替换**：点「登记新渠道」，菜单卡被表单卡盖掉；点「提交」，
# 表单卡又被成功卡盖掉。结果卡上没有按钮的话，要做下一件只能重新打字。
# 下面这几条钉的就是「每一张结果卡都带着菜单」。


def _actions_in(payload: dict[str, Any]) -> set[str]:
    found = set()
    for node in cards_walk(payload["card"]["data"]):
        if node.get("tag") != "button":
            continue
        for behavior in node.get("behaviors", []):
            if behavior.get("type") == "callback":
                found.add(behavior["value"]["action"])
    return found


def cards_walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from cards_walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from cards_walk(item)


MENU_ACTIONS = {
    cards.ACTION_OPEN_REFERRAL_FORM,
    cards.ACTION_OPEN_CLIENT_FORM,
    cards.ACTION_LIST_REFERRALS,
    cards.ACTION_OPEN_COMMISSION_QUERY,
    cards.ACTION_OPEN_ECAS_QUERY,
}


def test_登记成功的卡片上带着下一轮菜单(handlers):
    assert MENU_ACTIONS <= _actions_in(submit_referral(handlers))


def test_我的渠道走返回目录而不是叠一层菜单(handlers):
    """导览卡的下一步是往回走，不是重开一件事 —— 所以它给「返回目录」，不给五个入口。"""
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_LIST_REFERRALS)))
    actions = _actions_in(payload)
    assert cards.ACTION_OPEN_MENU in actions
    assert cards.ACTION_OPEN_REFERRAL_FORM not in actions


def test_返回目录真的回到主菜单(handlers):
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_OPEN_MENU)))
    assert _actions_in(payload) == MENU_ACTIONS


def test_认不出的动作也给菜单而不是死路(handlers):
    payload = marshalled(handlers.on_card_action(trigger("谁也不认识的动作")))
    assert MENU_ACTIONS <= _actions_in(payload)


def test_校验失败的卡片上也带菜单(handlers):
    """填错一项就要重新打字唤出菜单，是这次要修掉的体验里最烦的一种。"""
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_RATE: "不是数字"}))
    assert MENU_ACTIONS <= _actions_in(payload)


def test_名册里没有的人不给菜单(handlers):
    """给一排按钮，点了还是同一句拒绝 —— 不如不给。"""
    payload = marshalled(
        handlers.on_card_action(trigger(cards.ACTION_LIST_REFERRALS, open_id=STRANGER))
    )
    assert _actions_in(payload) == set()


def test_表单卡给退路不给整个菜单(handlers):
    """按错了进来得走得掉，但表单有自己的提交按钮，底下再堆五个入口只会让人点错。"""
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_OPEN_REFERRAL_FORM)))
    assert _actions_in(payload) == {cards.ACTION_SUBMIT_REFERRAL, cards.ACTION_OPEN_MENU}


def test_异步提交的等待卡不挂菜单而结果卡挂(handlers):
    """等待卡的下一步是「等结果」，不是「再做一件事」。菜单要跟着结果走。"""
    submit_referral(handlers)
    ack = marshalled(
        handlers.on_card_action(
            trigger(
                cards.ACTION_SUBMIT_CLIENT,
                form={
                    cards.F_CLIENT_UID: UID,
                    cards.F_CLIENT_NAME: "PLUTO STUDIO LIMITED",
                    cards.F_CLIENT_REFERRAL: "R001",
                },
            )
        )
    )
    assert _actions_in(ack) == set()

    # background 是同步执行器，结果卡这时已经推出去了
    pushed = json.loads(handlers._client.sent[-1].request_body.content)
    assert MENU_ACTIONS <= _actions_in({"card": {"data": pushed}})


# ---------- 我的渠道：权限 ----------


def test_别的销售的渠道不会出现在我的列表里(fake_bitable, handlers):
    """列表走 owned_records，别人的渠道连按钮都不该出现。"""
    submit_referral(handlers)
    fake_bitable.table(TBL_REFERRAL).add_existing(
        {schema.REFERRAL_NO: "R999", schema.REFERRAL_OWNER_OPEN_ID: STRANGER}
    )

    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_LIST_REFERRALS)))
    codes = {
        node["value"].get("referral_no")
        for node in cards_walk(payload["card"]["data"])
        if node.get("type") == "callback" and isinstance(node.get("value"), dict)
    }
    assert "R001" in codes
    assert "R999" not in codes


def test_点别人的渠道编号进不去(fake_bitable, handlers):
    """编号来自按钮回传，客户端改得了。权限判断不能只在列表那一步做。"""
    fake_bitable.table(TBL_REFERRAL).add_existing(
        {schema.REFERRAL_NO: "R999", schema.REFERRAL_OWNER_OPEN_ID: STRANGER}
    )

    payload = marshalled(
        handlers.on_card_action(
            trigger(
                cards.ACTION_OPEN_REFERRAL,
                value={"action": cards.ACTION_OPEN_REFERRAL, "referral_no": "R999"},
            )
        )
    )
    assert payload["card"]["data"]["header"]["title"]["content"] == "找不到这个渠道"


# ---------- 打开会话就自动弹菜单 ----------
#
# 「不用打字就有按钮」。飞书这个事件开得很勤（切回会话、手机上划一下都可能触发），
# 所以下面几条主要盯的是**别把会话刷满**，以及别给名册外的人推东西。


def entered(open_id: str = ALICE):
    from lark_oapi.api.im.v1 import P2ImChatAccessEventBotP2pChatEnteredV1

    return P2ImChatAccessEventBotP2pChatEnteredV1(
        {
            "schema": "2.0",
            "header": {"event_id": "evt-enter", "event_type": "im.chat.access_event"},
            "event": {
                "chat_id": "oc_1",
                "operator_id": {"open_id": open_id, "union_id": "on_1", "user_id": "u1"},
            },
        }
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _greeter(fake_bitable, clock, cooldown=300.0) -> BotHandlers:
    fake_bitable.table(TBL_SALES).add_existing(
        {
            schema.SALES_OPEN_ID: ALICE,
            schema.SALES_NAME: "Alice",
            schema.SALES_ROLE: schema.ROLE_SALES,
            schema.SALES_STATUS: schema.SALES_STATUS_ACTIVE,
        }
    )
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    return BotHandlers(
        client=StubLarkClient(),
        directory=SalesDirectory(fake_bitable, TBL_SALES),
        referrals=ReferralService(fake_bitable, TBL_REFERRAL, audit),
        clients=ReferredClientService(fake_bitable, TBL_CLIENT, TBL_REFERRAL, audit),
        background=lambda fn: fn(),
        greet_cooldown_seconds=cooldown,
        clock=clock,
    )


def _pushed(bots) -> list[dict]:
    return [json.loads(r.request_body.content) for r in bots._client.sent]


def test_打开会话就推一张主菜单(fake_bitable):
    bots = _greeter(fake_bitable, _Clock())
    bots.on_p2p_chat_entered(entered())

    (card,) = _pushed(bots)
    assert card["header"]["title"]["content"] == "渠道佣金助手"
    assert MENU_ACTIONS <= _actions_in({"card": {"data": card}})


def test_冷却期内再进来不重复推(fake_bitable):
    """这个事件开得很勤。每次都推的话，会话很快被菜单卡填满，
    刚点开的表单会被顶到上面去。"""
    clock = _Clock()
    bots = _greeter(fake_bitable, clock)

    bots.on_p2p_chat_entered(entered())
    clock.now += 299
    bots.on_p2p_chat_entered(entered())
    assert len(_pushed(bots)) == 1


def test_冷却期过了再进来会推(fake_bitable):
    clock = _Clock()
    bots = _greeter(fake_bitable, clock)

    bots.on_p2p_chat_entered(entered())
    clock.now += 301
    bots.on_p2p_chat_entered(entered())
    assert len(_pushed(bots)) == 2


def test_冷却期是按人算的(fake_bitable):
    clock = _Clock()
    bots = _greeter(fake_bitable, clock)
    fake_bitable.table(TBL_SALES).add_existing(
        {
            schema.SALES_OPEN_ID: "ou_bob000000000000000000000000000",
            schema.SALES_NAME: "Bob",
            schema.SALES_ROLE: schema.ROLE_SALES,
            schema.SALES_STATUS: schema.SALES_STATUS_ACTIVE,
        }
    )

    bots.on_p2p_chat_entered(entered())
    bots.on_p2p_chat_entered(entered("ou_bob000000000000000000000000000"))
    assert len(_pushed(bots)) == 2


def test_名册外的人打开会话不推任何东西(fake_bitable, caplog):
    """不请自来的一张拒绝卡对他没有用处。只留一行日志给管理员登记新人。"""
    import logging

    bots = _greeter(fake_bitable, _Clock())
    with caplog.at_level(logging.INFO):
        bots.on_p2p_chat_entered(entered(STRANGER))

    assert _pushed(bots) == []
    assert STRANGER in caplog.text


def test_事件里没有open_id时安静跳过(fake_bitable):
    """身份只认平台签发的那个值。取不到就什么都不做，别去猜。"""
    from lark_oapi.api.im.v1 import P2ImChatAccessEventBotP2pChatEnteredV1

    bots = _greeter(fake_bitable, _Clock())
    bots.on_p2p_chat_entered(
        P2ImChatAccessEventBotP2pChatEnteredV1(
            {"schema": "2.0", "header": {"event_id": "e"}, "event": {"chat_id": "oc_1"}}
        )
    )
    assert _pushed(bots) == []


def test_登记客户的表单卡也有退路(handlers):
    submit_referral(handlers)
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_OPEN_CLIENT_FORM)))
    assert cards.ACTION_OPEN_MENU in _actions_in(payload)


# ---------- 详情卡：附加信息取不到时不能把整张卡换成报错 ----------


def _open_r001(bots):
    return marshalled(
        bots.on_card_action(
            trigger("", value={"action": cards.ACTION_OPEN_REFERRAL, "referral_no": "R001"})
        )
    )


def _detail_text(payload) -> str:
    return "\n".join(
        node["content"]
        for node in cards_walk(payload["card"]["data"])
        if node.get("tag") == "markdown"
    )


class StubHistory:
    def __init__(self, fees=None, boom=False) -> None:
        self._fees = fees or []
        self._boom = boom
        self.asked: list[str] = []

    def recent(self, referral_no, *, today, months=3):
        self.asked.append(referral_no)
        if self._boom:
            raise RuntimeError("Base 炸了")
        return list(self._fees)


def _with_history(fake_bitable, history) -> BotHandlers:
    fake_bitable.table(TBL_SALES).add_existing(
        {
            schema.SALES_OPEN_ID: ALICE,
            schema.SALES_NAME: "Alice",
            schema.SALES_ROLE: schema.ROLE_SALES,
            schema.SALES_STATUS: schema.SALES_STATUS_ACTIVE,
        }
    )
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    return BotHandlers(
        client=StubLarkClient(),
        directory=SalesDirectory(fake_bitable, TBL_SALES),
        referrals=ReferralService(fake_bitable, TBL_REFERRAL, audit),
        clients=ReferredClientService(fake_bitable, TBL_CLIENT, TBL_REFERRAL, audit),
        referral_history=history,
        background=lambda fn: fn(),
    )


def test_详情卡带出近三个月和客户(fake_bitable):
    from decimal import Decimal

    from crm_basebot.domain.referral_history import MonthlyFee

    history = StubHistory([MonthlyFee("2026-08", None, Decimal("60000"))])
    bots = _with_history(fake_bitable, history)
    submit_referral(bots)
    bots.on_card_action(
        trigger(
            cards.ACTION_SUBMIT_CLIENT,
            form={
                cards.F_CLIENT_UID: UID,
                cards.F_CLIENT_NAME: "PLUTO STUDIO LIMITED",
                cards.F_CLIENT_REFERRAL: "R001",
            },
        )
    )

    text = _detail_text(_open_r001(bots))
    assert history.asked == ["R001"]
    assert "2026-08　交易 —　ECAS 60,000.00" in text
    assert "PLUTO STUDIO LIMITED" in text


def test_近三个月读不到时照样给资料(fake_bitable):
    """渠道资料已经在手上了。为了附加信息把整张卡换成报错，是拿有用的换没用的。"""
    bots = _with_history(fake_bitable, StubHistory(boom=True))
    submit_referral(bots)

    text = _detail_text(_open_r001(bots))
    assert "这次没查到" in text
    assert "分佣比例：20%" in text


def test_客户读不到时照样给资料(fake_bitable, monkeypatch):
    bots = _with_history(fake_bitable, StubHistory())
    submit_referral(bots)

    def boom(_record_id):
        raise RuntimeError("客户表炸了")

    monkeypatch.setattr(bots._clients, "names_for_referral", boom)
    text = _detail_text(_open_r001(bots))
    assert "分佣比例：20%" in text
    assert "客户（" not in text


def test_没注入历史服务时那一节不显示(handlers):
    """没配汇总表的租户照样能点进渠道详情。"""
    submit_referral(handlers)
    text = _detail_text(_open_r001(handlers))
    assert "近 3 个月" not in text
    assert "分佣比例：20%" in text


def test_别人的渠道下的客户不会漏进详情卡(fake_bitable):
    """names_for_referral 只按调用方鉴过权的那条 record_id 取数。"""
    bots = _with_history(fake_bitable, StubHistory())
    submit_referral(bots)
    other = fake_bitable.table(TBL_REFERRAL).add_existing(
        {schema.REFERRAL_NO: "R999", schema.REFERRAL_OWNER_OPEN_ID: STRANGER}
    )
    fake_bitable.table(TBL_CLIENT).add_existing(
        {schema.CLIENT_NAME: "别人的客户", schema.CLIENT_REFERRAL_LINK: [other]}
    )

    assert "别人的客户" not in _detail_text(_open_r001(bots))
