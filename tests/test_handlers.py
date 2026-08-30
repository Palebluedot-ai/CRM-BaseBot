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
from crm_basebot.domain.referral import ReferralService
from crm_basebot.domain.referred_client import ReferredClientService

from .conftest import TBL_AUDIT, TBL_CLIENT, TBL_REFERRAL, TBL_SALES

ALICE = "ou_alice000000000000000000000000"
STRANGER = "ou_stranger0000000000000000000"
UID = "577809207768677761"


class StubLarkClient:
    """handlers 只在发消息时用到 client，卡片回调这条路径根本不碰它。"""


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
        cards.F_REFERRAL_ADDRESS: "Hong Kong",
        cards.F_REFERRAL_PAYMENT: "HSBC 004-123456",
        cards.F_REFERRAL_RATE: "20",
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


def test_选填项回传_null_不会把回调打挂(handlers):
    """地址是选填的。平台对没填的项可能给 null，而不是干脆不给这个 key。

    ``form.get(key, "")`` 挡不住 null —— 默认值只在 key 缺失时生效。None 一路
    传到 ``.strip()`` 才炸，在 3 秒回调里就是一句「系统出错了」，看不出是哪个字段。
    """
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_ADDRESS: None}))

    assert payload["toast"]["type"] == "success"
    assert payload["card"]["data"]["header"]["title"]["content"] == "渠道已登记"


def test_选填项整个缺失也能提交(handlers):
    form = referral_form()
    del form[cards.F_REFERRAL_ADDRESS]

    assert submit_referral(handlers, form)["toast"]["type"] == "success"


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
