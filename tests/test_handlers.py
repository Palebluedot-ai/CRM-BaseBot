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
}


def test_登记成功的卡片上带着下一轮菜单(handlers):
    assert MENU_ACTIONS <= _actions_in(submit_referral(handlers))


def test_我的渠道卡片上带着下一轮菜单(handlers):
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_LIST_REFERRALS)))
    assert MENU_ACTIONS <= _actions_in(payload)


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


def test_表单卡上不挂菜单(handlers):
    """菜单按钮掉进 form 里会变成表单动作。表单本来就有自己的提交按钮，
    在它下面再堆四个入口只会让人点错。"""
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_OPEN_REFERRAL_FORM)))
    assert _actions_in(payload) == {cards.ACTION_SUBMIT_REFERRAL}


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


# ---------- 我的渠道：比例 + 名下客户 ----------


def _list_text(handlers) -> str:
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_LIST_REFERRALS)))
    blocks = [
        node["content"]
        for node in cards_walk(payload["card"]["data"])
        if node.get("tag") == "markdown"
    ]
    return "\n".join(blocks)


def test_渠道清单带出比例和名下客户(handlers):
    submit_referral(handlers)
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

    text = _list_text(handlers)
    assert "**R001** 北极星资本 · 20%" in text
    assert "1 个客户" in text
    assert "PLUTO STUDIO LIMITED" in text


def test_客户表读不出来时照样列渠道(handlers, monkeypatch):
    """渠道清单本身是有用的。为了一个附加信息把整张卡换成报错，
    是拿有用的东西去换没用的。"""
    submit_referral(handlers)

    def boom(_ids):
        raise RuntimeError("客户表炸了")

    monkeypatch.setattr(handlers._clients, "names_by_referral", boom)

    text = _list_text(handlers)
    assert "**R001** 北极星资本 · 20%" in text
    assert "客户名单这次没取到" in text


def test_别的销售的客户不会出现在我的渠道里(fake_bitable, handlers):
    """``names_by_referral`` 只认调用方鉴过权的那批 record_id。"""
    submit_referral(handlers)
    other = fake_bitable.table(TBL_REFERRAL).add_existing(
        {schema.REFERRAL_NO: "R999", schema.REFERRAL_OWNER_OPEN_ID: STRANGER}
    )
    fake_bitable.table(TBL_CLIENT).add_existing(
        {
            schema.CLIENT_NAME: "别人的客户",
            schema.CLIENT_REFERRAL_LINK: [other],
            schema.CLIENT_OWNER_OPEN_ID: STRANGER,
        }
    )

    text = _list_text(handlers)
    assert "别人的客户" not in text
    assert "R999" not in text


# ---------- ECAS 返佣按钮 ----------
#
# 它和「佣金查询」是平行的两条路，不是共用的一条：读的表不同、比例来源不同、
# 结果进不同的汇总表。长得像是因为交互一样，不是因为底下是同一件事。


class StubEcasQuery:
    def __init__(self, periods=("2026-08",), rows=()) -> None:
        self._periods = list(periods)
        self._rows = list(rows)
        self.queried: list[tuple[str, str]] = []

    def periods_for(self, sales):
        return list(self._periods)

    def query(self, sales, period):
        self.queried.append((sales.open_id, period))
        return list(self._rows)


def _with_ecas(fake_bitable, service) -> BotHandlers:
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    return BotHandlers(
        client=StubLarkClient(),
        directory=SalesDirectory(fake_bitable, TBL_SALES),
        referrals=ReferralService(fake_bitable, TBL_REFERRAL, audit),
        clients=ReferredClientService(fake_bitable, TBL_CLIENT, TBL_REFERRAL, audit),
        ecas_query=service,
        background=lambda fn: fn(),
    )


def test_主菜单上有ECAS入口(handlers):
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_LIST_REFERRALS)))
    assert cards.ACTION_OPEN_ECAS_QUERY in _actions_in(payload)


def test_没配ECAS时按钮回未启用而不是崩(handlers):
    """handlers fixture 没注入 ecas_query —— 没上 ECAS 的租户就是这个状态。"""
    payload = marshalled(handlers.on_card_action(trigger(cards.ACTION_OPEN_ECAS_QUERY)))
    text = payload["card"]["data"]["body"]["elements"][0]["content"]
    assert "未启用" in text
    # 死路上也要给菜单
    assert MENU_ACTIONS <= _actions_in(payload)


def test_打开ECAS查询时预选最新月份(fake_bitable):
    bots = _with_ecas(fake_bitable, StubEcasQuery(periods=["2026-07", "2026-08"]))
    fake_bitable.table(TBL_SALES).add_existing(
        {
            schema.SALES_OPEN_ID: ALICE,
            schema.SALES_NAME: "Alice",
            schema.SALES_ROLE: schema.ROLE_SALES,
            schema.SALES_STATUS: schema.SALES_STATUS_ACTIVE,
        }
    )
    payload = marshalled(bots.on_card_action(trigger(cards.ACTION_OPEN_ECAS_QUERY)))
    card = payload["card"]["data"]
    (select,) = [n for n in cards_walk(card) if n.get("tag") == "select_static"]
    assert select["initial_option"] == "2026-08"


def _ecas_handlers(fake_bitable, service):
    fake_bitable.table(TBL_SALES).add_existing(
        {
            schema.SALES_OPEN_ID: ALICE,
            schema.SALES_NAME: "Alice",
            schema.SALES_ROLE: schema.ROLE_SALES,
            schema.SALES_STATUS: schema.SALES_STATUS_ACTIVE,
        }
    )
    return _with_ecas(fake_bitable, service)


def test_查询走异步先ack再推结果(fake_bitable):
    service = StubEcasQuery()
    bots = _ecas_handlers(fake_bitable, service)

    ack = marshalled(
        bots.on_card_action(trigger(cards.ACTION_QUERY_ECAS, form={cards.F_ECAS_PERIOD: "2026-08"}))
    )
    assert "正在查询" in ack["card"]["data"]["header"]["title"]["content"]
    assert service.queried == [(ALICE, "2026-08")]

    pushed = json.loads(bots._client.sent[-1].request_body.content)
    assert "ECAS 返佣" in pushed["header"]["title"]["content"]
    assert MENU_ACTIONS <= _actions_in({"card": {"data": pushed}})


def test_月份格式不对时说人话(fake_bitable):
    bots = _ecas_handlers(fake_bitable, StubEcasQuery())
    payload = marshalled(
        bots.on_card_action(trigger(cards.ACTION_QUERY_ECAS, form={cards.F_ECAS_PERIOD: "八月"}))
    )
    assert "YYYY-MM" in payload["card"]["data"]["body"]["elements"][0]["content"]


def test_查询炸了推一张错误卡而不是静默(fake_bitable):
    class Boom(StubEcasQuery):
        def query(self, sales, period):
            raise RuntimeError("Base 炸了")

    bots = _ecas_handlers(fake_bitable, Boom())
    bots.on_card_action(trigger(cards.ACTION_QUERY_ECAS, form={cards.F_ECAS_PERIOD: "2026-08"}))

    pushed = json.loads(bots._client.sent[-1].request_body.content)
    assert pushed["header"]["template"] == "red"
    assert MENU_ACTIONS <= _actions_in({"card": {"data": pushed}})


def test_读月份列表失败不让整张卡挂掉(fake_bitable):
    class Boom(StubEcasQuery):
        def periods_for(self, sales):
            raise RuntimeError("Base 炸了")

    bots = _ecas_handlers(fake_bitable, Boom())
    payload = marshalled(bots.on_card_action(trigger(cards.ACTION_OPEN_ECAS_QUERY)))
    assert payload["card"]["data"]["schema"] == "2.0"
    assert "读取 ECAS 月份列表失败" in payload["card"]["data"]["body"]["elements"][0]["content"]
