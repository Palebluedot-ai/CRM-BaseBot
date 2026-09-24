"""卡片回调的入口行为。

这一层没有任何单测的时候最容易漏掉三类事：

1. **回调响应的结构**。飞书对响应体的形状有硬要求，写错了平台回 200672/200673，
   用户看到「出错了，请稍后重试」，而服务端这边一切正常、什么都不知道。所以这里
   不是断言我们构造的那个 dict，而是断言 **SDK 序列化之后真正发出去的 JSON**。
2. **被点的卡留不留得住**（2026-09-24 反馈：「紀錄會消失」）。回调响应里带 card 就是
   原地换卡。现在只有翻页和「已提交」回执可以带；其余一律空响应或一句 toast，
   内容作为新消息推出去。
3. **表单值的边界**。选填项没填时平台可能不给 key，也可能给 null；下拉的回传值
   可能是裸字符串也可能包一层；日期选择器回传 ``2026-01-15 +0800``。这些都到不了
   业务代码，得在入口挡住。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal
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
TODAY = date(2026, 9, 24)

START_DATE = date(2026, 1, 15)
# 日期选择器的真实回传：日历日 + 选的人设备的时区（飞书「卡片回传交互」文档）。
START_DATE_PICKED = "2026-01-15 +0800"
START_DATE_MS = str(date_to_ms(START_DATE, tz=DEFAULT_BUSINESS_TIMEZONE))


class _StubResponse:
    code = 0
    msg = "ok"

    def success(self) -> bool:
        return True


class _Rejected:
    code = 230099
    msg = "Failed to create card content"

    def success(self) -> bool:
        return False


class StubLarkClient:
    """推新消息走 ``im.v1.message.create``，这里把每次调用记下来供断言。

    ``reject_tables=True`` 时模拟飞书不收带表格的卡，用来钉住「改发列点版」。
    """

    def __init__(self, *, reject_tables: bool = False) -> None:
        self.sent: list[Any] = []
        self.reject_tables = reject_tables
        self.im = self  # type: ignore[assignment]
        self.v1 = self  # type: ignore[assignment]
        self.message = self  # type: ignore[assignment]

    def create(self, request: Any):
        self.sent.append(request)
        if self.reject_tables and '"tag": "table"' in request.request_body.content:
            return _Rejected()
        return _StubResponse()


def make_handlers(fake_bitable, **overrides) -> BotHandlers:
    fake_bitable.table(TBL_SALES).add_existing(
        {
            schema.SALES_OPEN_ID: ALICE,
            schema.SALES_NAME: "Alice",
            schema.SALES_ROLE: schema.ROLE_SALES,
            schema.SALES_STATUS: schema.SALES_STATUS_ACTIVE,
        }
    )
    audit = AuditLog(fake_bitable, TBL_AUDIT)
    kwargs = dict(
        client=StubLarkClient(),
        directory=SalesDirectory(fake_bitable, TBL_SALES),
        referrals=ReferralService(fake_bitable, TBL_REFERRAL, audit),
        clients=ReferredClientService(fake_bitable, TBL_CLIENT, TBL_REFERRAL, audit),
        # 同步执行后台任务，避免线程竞态干扰断言
        background=lambda fn: fn(),
        today=lambda: TODAY,
    )
    kwargs.update(overrides)
    return BotHandlers(**kwargs)


@pytest.fixture
def handlers(fake_bitable):
    return make_handlers(fake_bitable)


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


def click(bots, action: str, **kwargs) -> dict[str, Any]:
    return marshalled(bots.on_card_action(trigger(action, **kwargs)))


def pushed(bots) -> list[dict[str, Any]]:
    """推出去的新消息里的卡片，按发送顺序。"""
    return [json.loads(r.request_body.content) for r in bots._client.sent]


def last_pushed(bots) -> dict[str, Any]:
    return pushed(bots)[-1]


def referral_form(**overrides) -> dict[str, Any]:
    form = {
        cards.F_REFERRAL_NAME: "北极星资本",
        cards.F_REFERRAL_EMAIL: "ops@polaris.example",
        cards.F_REFERRAL_START_DATE: START_DATE_PICKED,
        cards.F_REFERRAL_RATE: "20",
        cards.F_REFERRAL_PAYOUT: schema.PAYOUT_MONTHLY,
    }
    form.update(overrides)
    return form


def submit_referral(handlers, form: dict[str, Any] | None = None) -> dict[str, Any]:
    event = trigger(cards.ACTION_SUBMIT_REFERRAL, form=referral_form() if form is None else form)
    return marshalled(handlers.on_card_action(event))


def client_form(**overrides) -> dict[str, Any]:
    form = {
        cards.F_CLIENT_UID: UID,
        cards.F_CLIENT_NAME: "PLUTO STUDIO LIMITED",
        cards.F_CLIENT_REFERRAL: "R001",
    }
    form.update(overrides)
    return form


def cards_walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from cards_walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from cards_walk(item)


def _buttons(card: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for node in cards_walk(card):
        if node.get("tag") != "button":
            continue
        (callback,) = [b for b in node["behaviors"] if b["type"] == "callback"]
        found.append((node["text"]["content"], callback["value"]))
    return found


def _actions(card: dict[str, Any]) -> set[str]:
    return {value["action"] for _, value in _buttons(card)}


def _text(card: dict[str, Any]) -> str:
    return "\n".join(node["content"] for node in cards_walk(card) if node.get("tag") == "markdown")


MENU_ACTIONS = {
    cards.ACTION_OPEN_REFERRAL_FORM,
    cards.ACTION_OPEN_CLIENT_FORM,
    cards.ACTION_LIST_REFERRALS,
    cards.ACTION_OPEN_COMMISSION_QUERY,
    cards.ACTION_OPEN_ECAS_QUERY,
}


# ---------- 被点的卡留得住：内容作为新消息推出去 ----------


@pytest.mark.parametrize(
    "action",
    [
        cards.ACTION_OPEN_MENU,
        cards.ACTION_OPEN_REFERRAL_FORM,
        cards.ACTION_OPEN_CLIENT_FORM,
        cards.ACTION_LIST_REFERRALS,
    ],
)
def test_点按钮不换卡_新卡作为新消息发出去(handlers, action):
    """回调响应里带 card 就是原地换卡，被点的那张就没了（「紀錄會消失」）。"""
    payload = click(handlers, action)

    assert payload == {}, "空响应：飞书收到 {} 就不动那张卡"
    assert len(pushed(handlers)) == 1
    assert last_pushed(handlers)["schema"] == "2.0"


def test_空响应序列化出来就是空对象(handlers):
    """``toast: null`` / ``card: null`` 会被 SDK 的 filter_null 抹掉，这里钉住这个前提。"""
    assert lark.JSON.marshal(handlers.on_card_action(trigger(cards.ACTION_OPEN_MENU))) == "{}"


def test_返回目录发一张新的主菜单(handlers):
    click(handlers, cards.ACTION_OPEN_MENU)

    menu = last_pushed(handlers)
    assert menu["header"]["title"]["content"] == "渠道佣金助手"
    assert _actions(menu) == MENU_ACTIONS


def test_点进渠道_详情作为新消息_列表留着(handlers):
    submit_referral(handlers)
    payload = click(
        handlers,
        cards.ACTION_OPEN_REFERRAL,
        value={"action": cards.ACTION_OPEN_REFERRAL, "referral_no": "R001"},
    )

    assert "card" not in payload
    assert payload["toast"] == {"type": "info", "content": "正在打开 R001"}
    detail = last_pushed(handlers)
    assert detail["header"]["title"]["content"] == "北极星资本"
    assert "分佣比例：20%" in _text(detail)
    assert ("返回列表", {"action": cards.ACTION_LIST_REFERRALS}) in _buttons(detail)


def test_翻页是唯一原地换列表的(handlers, monkeypatch):
    """翻的是同一张列表，不是做完了一件事。页大小测试里调小。"""
    monkeypatch.setattr(cards, "REFERRAL_PAGE_SIZE", 2)
    for index in range(3):
        submit_referral(handlers, referral_form(**{cards.F_REFERRAL_NAME: f"渠道{index + 1}"}))
    sent_before = len(pushed(handlers))

    payload = click(
        handlers,
        cards.ACTION_REFERRAL_PAGE,
        value={"action": cards.ACTION_REFERRAL_PAGE, "page": 1},
    )
    assert payload["card"]["type"] == "raw"
    assert payload["card"]["data"]["schema"] == "2.0"
    opened = [v["referral_no"] for _, v in _buttons(payload["card"]["data"]) if "referral_no" in v]
    assert opened == ["R003"]
    assert len(pushed(handlers)) == sent_before, "翻页不发新消息"


def test_从详情返回列表发一张新列表(handlers):
    submit_referral(handlers)
    payload = click(handlers, cards.ACTION_LIST_REFERRALS)

    assert payload == {}
    codes = [v["referral_no"] for _, v in _buttons(last_pushed(handlers)) if "referral_no" in v]
    assert codes == ["R001"]


def test_后台出错时推一张带菜单的报错卡(handlers, monkeypatch):
    """人点了一下，总得看到点什么。"""

    def boom(_sales):
        raise RuntimeError("Base 炸了")

    monkeypatch.setattr(handlers._referrals, "list_for", boom)
    payload = click(handlers, cards.ACTION_LIST_REFERRALS)

    assert payload == {}
    card = last_pushed(handlers)
    assert card["header"]["title"]["content"] == "没能完成"
    assert MENU_ACTIONS <= _actions(card)


def test_认不出的动作也推一张带菜单的卡而不是死路(handlers):
    payload = click(handlers, "谁也不认识的动作")
    assert payload == {}
    assert MENU_ACTIONS <= _actions(last_pushed(handlers))


# ---------- 表带表格的卡被拒时改发列点版 ----------


def test_带表格的卡被拒时改发列点版(fake_bitable, caplog):
    """表格组件线下只能照文档和 SDK 对。真机上万一不收，整条消息就发不出去 ——
    所以同样的内容换成列点再发一次，日志里留下平台回的错误码。"""
    bots = make_handlers(
        fake_bitable,
        client=StubLarkClient(reject_tables=True),
        commission_query=StubCommissionQuery(),
    )
    with caplog.at_level(logging.WARNING):
        click(bots, cards.ACTION_QUERY_COMMISSION, form={cards.F_QUERY_PERIOD: "2026-09"})

    first, second = pushed(bots)
    assert any(node.get("tag") == "table" for node in cards_walk(first))
    assert not any(node.get("tag") == "table" for node in cards_walk(second))
    assert "R076" in _text(second)
    assert "230099" in caplog.text


def test_不带表格的卡被拒时不重发(fake_bitable):
    class RejectAll(StubLarkClient):
        def create(self, request):
            self.sent.append(request)
            return _Rejected()

    bots = make_handlers(fake_bitable, client=RejectAll())
    click(bots, cards.ACTION_OPEN_MENU)
    assert len(bots._client.sent) == 1


# ---------- 登记渠道：校验回 toast，写入在后台 ----------


def test_登记渠道提交后表单换成已提交回执(handlers):
    """表单原样留着就能再点一次提交，登记出两条一样的渠道。回执把填过的列出来。"""
    payload = submit_referral(handlers)

    assert payload["toast"] == {"type": "success", "content": "已提交"}
    receipt = payload["card"]["data"]
    assert receipt["schema"] == "2.0"
    assert receipt["header"]["title"]["content"] == "登记新渠道 · 已提交"
    text = _text(receipt)
    assert "渠道名称：北极星资本" in text
    assert "开始日期：2026-01-15" in text
    assert "分佣比例：20%" in text
    assert _actions(receipt) == set(), "回执上不能再有提交按钮"


def test_登记结果作为新消息推出来且带菜单(handlers):
    submit_referral(handlers)

    result = last_pushed(handlers)
    assert result["header"]["title"]["content"] == "渠道已登记"
    assert "R001" in _text(result)
    assert "分佣比例 20%" in _text(result)
    assert MENU_ACTIONS <= _actions(result)


def test_登记写入失败时推报错卡(handlers, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("写冲突")

    monkeypatch.setattr(handlers._referrals, "create", boom)
    payload = submit_referral(handlers)

    assert payload["card"]["data"]["header"]["title"]["content"] == "登记新渠道 · 已提交"
    result = last_pushed(handlers)
    assert result["header"]["title"]["content"] == "没能完成"
    assert MENU_ACTIONS <= _actions(result)


def _assert_rejected(payload: dict[str, Any], words: str) -> None:
    """填错了：一句红字，**不带 card** —— 表单原样留着，改一个字就能再提交。"""
    assert "card" not in payload
    assert payload["toast"]["type"] == "error"
    assert words in payload["toast"]["content"]


def test_文本项回传_null_不会把回调打挂(handlers):
    """邮箱是选填的。平台对没填的项可能给 null，而不是干脆不给这个 key。

    ``form.get(key, "")`` 挡不住 null —— 默认值只在 key 缺失时生效。None 一路
    传到 ``.strip()`` 才炸，就是一句「系统出错了」，看不出是哪个字段。
    """
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_EMAIL: None}))

    assert payload["toast"]["type"] == "success"
    assert last_pushed(handlers)["header"]["title"]["content"] == "渠道已登记"


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


def test_日期选择器的真实回传能登记(fake_bitable, handlers):
    """``2026-01-15 +0800`` 是飞书日期选择器的真实回传。原先只认毫秒时间戳和纯日期，
    选了日期照样报「开始日期要选一个日期」（2026-09-24 反馈）。"""
    payload = submit_referral(handlers)

    assert payload["toast"]["type"] == "success"
    assert _referral_fields(fake_bitable)[schema.REFERRAL_START_DATE] == date_to_ms(
        START_DATE, tz=DEFAULT_BUSINESS_TIMEZONE
    )


@pytest.mark.parametrize(
    "picked",
    [
        START_DATE_PICKED,
        "2026-01-15 -0500",  # 人在别的时区也是他点的那一天
        "2026-01-15",
        "2026-01-15 10:00 +0800",
        {"value": START_DATE_PICKED},
        START_DATE_MS,
        {"value": START_DATE_MS},
    ],
)
def test_日期的几种回传形态都落成同一天(fake_bitable, handlers, picked):
    submit_referral(handlers, referral_form(**{cards.F_REFERRAL_START_DATE: picked}))

    assert _referral_fields(fake_bitable)[schema.REFERRAL_START_DATE] == date_to_ms(
        START_DATE, tz=DEFAULT_BUSINESS_TIMEZONE
    )


@pytest.mark.parametrize("missing", [None, "", "不是日期", "2026-02-30 +0800"])
def test_日期取不到时给出人话报错(handlers, missing):
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_START_DATE: missing}))
    _assert_rejected(payload, "开始日期")


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
    _assert_rejected(payload, "结算频率")


def test_必填项回传_null_给出人话报错(handlers):
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_NAME: None}))
    _assert_rejected(payload, "渠道名称")


@pytest.mark.parametrize("typed", ["20", " 20 ", "20%", "20 %"])
def test_比例带百分号也认(handlers, typed):
    """输入框标签就写着「分佣比例 (%)」，照着填 20% 的人不会少。"""
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_RATE: typed}))
    assert payload["toast"]["type"] == "success"


def test_比例填了不是数字给出人话报错(handlers):
    payload = submit_referral(handlers, referral_form(**{cards.F_REFERRAL_RATE: "两成"}))
    _assert_rejected(payload, "分佣比例")


def test_校验失败时什么都不写也不推(fake_bitable, handlers):
    submit_referral(handlers, referral_form(**{cards.F_REFERRAL_RATE: "两成"}))

    assert fake_bitable.writes == []
    assert pushed(handlers) == []


# ---------- 登记客户 ----------


def test_下拉回传裸字符串和包字典两种形态都认(handlers):
    submit_referral(handlers)

    plain = click(handlers, cards.ACTION_SUBMIT_CLIENT, form=client_form())
    assert plain["toast"]["type"] == "success"
    assert last_pushed(handlers)["header"]["title"]["content"] == "客户已登记"

    wrapped = click(
        handlers,
        cards.ACTION_SUBMIT_CLIENT,
        form=client_form(
            **{
                cards.F_CLIENT_UID: "577809207768677762",
                cards.F_CLIENT_NAME: "普罗米修斯投资",
                cards.F_CLIENT_REFERRAL: {"value": "R001"},
            }
        ),
    )
    assert wrapped["toast"]["type"] == "success"
    assert last_pushed(handlers)["header"]["title"]["content"] == "客户已登记"


def test_登记客户提交后换成已提交回执_结果另推带菜单(handlers):
    submit_referral(handlers)
    payload = click(handlers, cards.ACTION_SUBMIT_CLIENT, form=client_form())

    receipt = payload["card"]["data"]
    assert receipt["header"]["title"]["content"] == "登记新客户 · 已提交"
    assert f"客户UID：{UID}" in _text(receipt)
    assert _actions(receipt) == set()
    assert MENU_ACTIONS <= _actions(last_pushed(handlers))


def test_UID_不是数字时回红字_表单留着(handlers):
    submit_referral(handlers)
    payload = click(
        handlers, cards.ACTION_SUBMIT_CLIENT, form=client_form(**{cards.F_CLIENT_UID: "abc"})
    )
    _assert_rejected(payload, "纯数字")


def test_UID_重复时推一张报错卡(handlers):
    """查重要读客户表，放在后台做；表单已经换成回执，结果卡说清楚为什么没登记上。"""
    submit_referral(handlers)
    click(handlers, cards.ACTION_SUBMIT_CLIENT, form=client_form())
    click(handlers, cards.ACTION_SUBMIT_CLIENT, form=client_form())

    result = last_pushed(handlers)
    assert result["header"]["title"]["content"] == "没能完成"
    assert "已经登记过了" in _text(result)


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


def test_名册外的人只拿到一句红字(handlers):
    """给一排按钮，点了还是同一句拒绝 —— 不如不给。也不换掉他点的那张卡。"""
    payload = click(handlers, cards.ACTION_OPEN_REFERRAL_FORM, open_id=STRANGER)

    assert "card" not in payload
    assert payload["toast"]["type"] == "error"
    assert "销售名册" in payload["toast"]["content"]
    assert pushed(handlers) == []


def test_归属取自回调而不是表单(fake_bitable, handlers):
    """表单里塞一个别人的 open_id，写进去的归属必须还是回调里那个人。"""
    handlers.on_card_action(
        trigger(
            cards.ACTION_SUBMIT_REFERRAL,
            form=referral_form(**{"登记人OpenID": STRANGER, "归属销售": STRANGER}),
        )
    )

    written = _referral_fields(fake_bitable)
    assert written[schema.REFERRAL_OWNER_OPEN_ID] == ALICE
    assert written[schema.REFERRAL_OWNER] == [{"id": ALICE}]


def test_推给谁取自回调里的_open_id(handlers):
    click(handlers, cards.ACTION_OPEN_MENU)

    (request,) = handlers._client.sent
    assert request.receive_id_type == "open_id"
    assert request.request_body.receive_id == ALICE


# ---------- 结果卡带菜单，导览卡带退路 ----------


def test_我的渠道走返回目录而不是叠一层菜单(handlers):
    """导览卡的下一步是往回走，不是重开一件事 —— 所以它给「返回目录」，不给五个入口。"""
    click(handlers, cards.ACTION_LIST_REFERRALS)
    actions = _actions(last_pushed(handlers))
    assert cards.ACTION_OPEN_MENU in actions
    assert cards.ACTION_OPEN_REFERRAL_FORM not in actions


def test_表单卡给退路不给整个菜单(handlers):
    """按错了进来得走得掉，但表单有自己的提交按钮，底下再堆五个入口只会让人点错。"""
    click(handlers, cards.ACTION_OPEN_REFERRAL_FORM)
    assert _actions(last_pushed(handlers)) == {cards.ACTION_SUBMIT_REFERRAL, cards.ACTION_OPEN_MENU}


def test_登记客户的表单卡也有退路(handlers):
    submit_referral(handlers)
    click(handlers, cards.ACTION_OPEN_CLIENT_FORM)
    assert cards.ACTION_OPEN_MENU in _actions(last_pushed(handlers))


def test_没有渠道时登记客户给提示和菜单(handlers):
    click(handlers, cards.ACTION_OPEN_CLIENT_FORM)
    card = last_pushed(handlers)
    assert card["header"]["title"]["content"] == "还不能登记客户"
    assert MENU_ACTIONS <= _actions(card)


# ---------- 我的渠道：权限 ----------


def test_别的销售的渠道不会出现在我的列表里(fake_bitable, handlers):
    """列表走 owned_records，别人的渠道连按钮都不该出现。"""
    submit_referral(handlers)
    fake_bitable.table(TBL_REFERRAL).add_existing(
        {schema.REFERRAL_NO: "R999", schema.REFERRAL_OWNER_OPEN_ID: STRANGER}
    )

    click(handlers, cards.ACTION_LIST_REFERRALS)
    codes = {v.get("referral_no") for _, v in _buttons(last_pushed(handlers))}
    assert "R001" in codes
    assert "R999" not in codes


def test_点别人的渠道编号进不去(fake_bitable, handlers):
    """编号来自按钮回传，客户端改得了。权限判断不能只在列表那一步做。"""
    fake_bitable.table(TBL_REFERRAL).add_existing(
        {
            schema.REFERRAL_NO: "R999",
            schema.REFERRAL_NAME: "别人的渠道",
            schema.REFERRAL_OWNER_OPEN_ID: STRANGER,
        }
    )

    click(
        handlers,
        cards.ACTION_OPEN_REFERRAL,
        value={"action": cards.ACTION_OPEN_REFERRAL, "referral_no": "R999"},
    )
    card = last_pushed(handlers)
    assert card["header"]["title"]["content"] == "找不到这个渠道"
    assert "别人的渠道" not in json.dumps(card, ensure_ascii=False)
    assert _actions(card) == {cards.ACTION_LIST_REFERRALS, cards.ACTION_OPEN_MENU}


# ---------- 佣金查询 ----------


class StubCommissionQuery:
    """记下被问了哪几个月，回一个 R076 的结果。"""

    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    def query(self, sales, periods):
        from crm_basebot.domain.commission_query import (
            ClientBreakdown,
            QueryResult,
            ReferralBreakdown,
        )

        self.asked.append(list(periods))
        breakdown = ReferralBreakdown("R076", "DAI CANGWEI", Decimal("30"))
        breakdown.clients[UID] = ClientBreakdown(uid=UID, name="客户甲", revenue=Decimal("1000"))
        return QueryResult(periods=list(periods), months={periods[-1]: [breakdown]})


def test_佣金查询预选本月_不去扫看板(fake_bitable):
    """以前为了预选一个月份扫一遍上万行的看板；现在预选本月，一个请求都不发。"""
    query = StubCommissionQuery()
    bots = make_handlers(fake_bitable, commission_query=query)

    assert click(bots, cards.ACTION_OPEN_COMMISSION_QUERY) == {}
    card = last_pushed(bots)
    (select,) = [n for n in cards_walk(card) if n.get("tag") == "select_static"]
    assert select["initial_option"] == "2026-09"
    options = [o["value"] for o in select["options"]]
    assert options[0] == "2026-09"
    assert options[-1] == "2025-10"
    assert len(options) == 12
    assert query.asked == []


def test_佣金查询查选中月份和前两个月_表单留着(fake_bitable):
    query = StubCommissionQuery()
    bots = make_handlers(fake_bitable, commission_query=query)

    payload = click(bots, cards.ACTION_QUERY_COMMISSION, form={cards.F_QUERY_PERIOD: "2026-09"})

    assert "card" not in payload, "查询表单留着，可以换个月份再查"
    assert payload["toast"] == {"type": "info", "content": "正在查询 2026-07 ~ 2026-09"}
    assert query.asked == [["2026-07", "2026-08", "2026-09"]]
    result = last_pushed(bots)
    assert result["header"]["title"]["content"] == "佣金明细  2026-07 ~ 2026-09"
    assert UID not in json.dumps(result, ensure_ascii=False)
    assert MENU_ACTIONS <= _actions(result)


def test_佣金查询本月那一列标至今(fake_bitable):
    bots = make_handlers(fake_bitable, commission_query=StubCommissionQuery())
    click(bots, cards.ACTION_QUERY_COMMISSION, form={cards.F_QUERY_PERIOD: "2026-09"})

    tables = [n for n in cards_walk(last_pushed(bots)) if n.get("tag") == "table"]
    headers = [c["display_name"] for c in tables[-1]["columns"]]
    assert headers == ["渠道 / 客户", "7月", "8月", "9月至今"]


@pytest.mark.parametrize("bad", ["2026-13", "", "九月"])
def test_佣金查询月份不对时弹红字(fake_bitable, bad):
    bots = make_handlers(fake_bitable, commission_query=StubCommissionQuery())
    payload = click(bots, cards.ACTION_QUERY_COMMISSION, form={cards.F_QUERY_PERIOD: bad})
    _assert_rejected(payload, "YYYY-MM")
    assert pushed(bots) == []


def test_佣金查询失败时推一张说清楚的报错卡(fake_bitable):
    class Broken(StubCommissionQuery):
        def query(self, sales, periods):
            raise RuntimeError("看板读不到")

    bots = make_handlers(fake_bitable, commission_query=Broken())
    click(bots, cards.ACTION_QUERY_COMMISSION, form={cards.F_QUERY_PERIOD: "2026-09"})

    card = last_pushed(bots)
    assert "查询佣金明细失败" in _text(card)
    assert MENU_ACTIONS <= _actions(card)


def test_没配佣金查询时推一句未启用(handlers):
    assert click(handlers, cards.ACTION_OPEN_COMMISSION_QUERY) == {}
    assert "未启用" in _text(last_pushed(handlers))


# ---------- ECAS 返佣 ----------


class StubEcasQuery:
    def periods_for(self, sales):
        return ["2026-08", "2026-09"]

    def query(self, sales, period):
        return []


def test_ECAS查询表单作为新消息_结果也是(fake_bitable):
    bots = make_handlers(fake_bitable, ecas_query=StubEcasQuery())

    assert click(bots, cards.ACTION_OPEN_ECAS_QUERY) == {}
    (select,) = [n for n in cards_walk(last_pushed(bots)) if n.get("tag") == "select_static"]
    assert select["initial_option"] == "2026-09"

    payload = click(bots, cards.ACTION_QUERY_ECAS, form={cards.F_ECAS_PERIOD: "2026-09"})
    assert "card" not in payload
    assert payload["toast"] == {"type": "info", "content": "正在查询 2026-09"}
    result = last_pushed(bots)
    assert result["header"]["title"]["content"] == "ECAS 返佣  2026-09"
    assert MENU_ACTIONS <= _actions(result)


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
    assert MENU_ACTIONS <= _actions(card)


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


# ---------- 详情卡：附加信息取不到时不能把整张卡换成报错 ----------


def _open_r001(bots) -> dict[str, Any]:
    bots.on_card_action(
        trigger("", value={"action": cards.ACTION_OPEN_REFERRAL, "referral_no": "R001"})
    )
    return last_pushed(bots)


class StubHistory:
    def __init__(self, months=None, boom=False) -> None:
        self._months = months or []
        self._boom = boom
        self.asked: list[tuple[str, date]] = []

    def recent(self, referral_record_id, *, today, months=3):
        self.asked.append((referral_record_id, today))
        if self._boom:
            raise RuntimeError("Base 炸了")
        return list(self._months)


def test_详情卡带出近三个月每个客户和客户名单(fake_bitable):
    from crm_basebot.domain.referral_history import ChannelMonth, ClientMonth

    history = StubHistory(
        [
            ChannelMonth(
                "2026-08",
                ecas=Decimal("60000"),
                clients=(ClientMonth("PLUTO STUDIO LIMITED", None, Decimal("60000")),),
            )
        ]
    )
    bots = make_handlers(fake_bitable, referral_history=history)
    submit_referral(bots)
    click(bots, cards.ACTION_SUBMIT_CLIENT, form=client_form())

    detail = _open_r001(bots)
    text = _text(detail)
    assert "**2026-08**　交易 — · ECAS 60,000.00" in text
    assert "客户（1）" in text
    (table,) = [n for n in cards_walk(detail) if n.get("tag") == "table"]
    assert table["rows"] == [{"client": "PLUTO STUDIO LIMITED", "trade": "—", "ecas": "60,000.00"}]


def test_详情卡按鉴过权的_record_id_和业务时区的今天去取(fake_bitable):
    history = StubHistory()
    bots = make_handlers(fake_bitable, referral_history=history)
    submit_referral(bots)
    _open_r001(bots)

    ((record_id, today),) = history.asked
    assert fake_bitable.table(TBL_REFERRAL).records[record_id][schema.REFERRAL_NO] == "R001"
    assert today == TODAY


def test_近三个月读不到时照样给资料(fake_bitable):
    """渠道资料已经在手上了。为了附加信息把整张卡换成报错，是拿有用的换没用的。"""
    bots = make_handlers(fake_bitable, referral_history=StubHistory(boom=True))
    submit_referral(bots)

    text = _text(_open_r001(bots))
    assert "这次没查到" in text
    assert "分佣比例：20%" in text


def test_客户读不到时照样给资料(fake_bitable, monkeypatch):
    bots = make_handlers(fake_bitable, referral_history=StubHistory())
    submit_referral(bots)

    def boom(_record_id):
        raise RuntimeError("客户表炸了")

    monkeypatch.setattr(bots._clients, "names_for_referral", boom)
    text = _text(_open_r001(bots))
    assert "分佣比例：20%" in text
    assert "客户（" not in text


def test_没注入历史服务时那一节不显示(handlers):
    """没配看板和 ECAS 表的租户照样能点进渠道详情。"""
    submit_referral(handlers)
    text = _text(_open_r001(handlers))
    assert "近 3 个月" not in text
    assert "分佣比例：20%" in text


def test_别人的渠道下的客户不会漏进详情卡(fake_bitable):
    """names_for_referral 只按调用方鉴过权的那条 record_id 取数。"""
    bots = make_handlers(fake_bitable, referral_history=StubHistory())
    submit_referral(bots)
    other = fake_bitable.table(TBL_REFERRAL).add_existing(
        {schema.REFERRAL_NO: "R999", schema.REFERRAL_OWNER_OPEN_ID: STRANGER}
    )
    fake_bitable.table(TBL_CLIENT).add_existing(
        {schema.CLIENT_NAME: "别人的客户", schema.CLIENT_REFERRAL_LINK: [other]}
    )

    assert "别人的客户" not in _text(_open_r001(bots))
