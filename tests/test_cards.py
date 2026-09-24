"""卡片 JSON 2.0 的结构约束。

这些断言是从飞书「卡片 JSON 2.0 组件」文档一条条抄下来的，存在的理由很直白：
卡片写错了不会在本地报任何错，要等真机上发出去才知道 —— 轻则渲染成一团乱，
重则整条消息发不出去，或者按钮点了没反应（平台回 200530 / 200673，而我们这边
一行日志都没有）。能在离线断言的规则，就不要留到真机上撞。

改卡片时如果这里红了，先去看文档确认新写法，再改断言，不要反过来。
"""

from __future__ import annotations

from typing import Any

import pytest

from crm_basebot.bot import cards
from crm_basebot.domain import schema

# header.template 的合法取值，来自「标题组件」文档。写错了标题会退化成 default。
VALID_TEMPLATES = {
    "blue",
    "wathet",
    "turquoise",
    "green",
    "yellow",
    "orange",
    "red",
    "carmine",
    "violet",
    "purple",
    "indigo",
    "grey",
    "default",
}

# 富文本组件 text_size 的合法取值，来自「富文本（markdown）组件」文档
VALID_TEXT_SIZES = {
    "heading-0",
    "heading-1",
    "heading-2",
    "heading-3",
    "heading-4",
    "heading",
    "normal",
    "notation",
    "xxxx-large",
    "xxx-large",
    "xx-large",
    "x-large",
    "large",
    "medium",
    "small",
    "x-small",
}

# 表单容器里必须带 name 的组件（交互组件）。展示类组件不需要。
INTERACTIVE_TAGS = {
    "input",
    "button",
    "select_static",
    "multi_select_static",
    "select_person",
    "date_picker",
    "checker",
}

REFERRAL_OPTIONS = [("R001", "北极星资本"), ("R002", "鲸落数字")]


MANY_OPTIONS = [(f"R{i:03d}", f"渠道{i}") for i in range(1, 21)]


def all_cards() -> list[tuple[str, dict[str, Any]]]:
    """机器人会发出去的每一张卡片。新增卡片时记得挂到这里。"""
    return [
        ("menu_card", cards.menu_card("张三")),
        ("referral_form_card", cards.referral_form_card()),
        ("client_form_card", cards.client_form_card(REFERRAL_OPTIONS)),
        ("client_form_card_空渠道", cards.client_form_card([])),
        ("notice_card", cards.notice_card("标题", "正文")),
        ("success_card", cards.success_card("成了", "正文")),
        ("error_card", cards.error_card("出错了")),
        ("referral_list_card", cards.referral_list_card(REFERRAL_OPTIONS)),
        ("referral_list_card_空", cards.referral_list_card([])),
        (
            "referral_detail_card",
            cards.referral_detail_card(
                no="R001",
                name="北极星资本",
                status="生效",
                sales_name="Alice",
                start_date="2026-01-15",
                rate="20%",
                payout="Monthly",
                email="a@b.com",
                submitted_on="2026-01-16",
                address="",
                payment="",
            ),
        ),
        ("referral_missing_card", cards.referral_missing_card()),
        # 结果卡接上菜单之后仍然要满足上面每一条骨架约束 —— 这是实际会发出去的形态
        ("with_menu_成功卡", cards.with_menu(cards.success_card("成了", "正文"))),
        ("referral_list_card_第二页", cards.referral_list_card(MANY_OPTIONS, page=1)),
        ("with_menu_佣金结果", cards.with_menu(cards.commission_result_card("佣金明细", "正文"))),
        ("ecas_query_card", cards.ecas_query_card("2026-09", ["2026-08", "2026-09"])),
        ("ecas_query_card_无月份", cards.ecas_query_card("", [])),
        ("commission_query_card", cards.commission_query_card("2026-09", ["2026-08", "2026-09"])),
        ("commission_query_card_无月份", cards.commission_query_card("", [])),
        ("with_menu_ECAS结果", cards.with_menu(cards.ecas_result_card("ECAS 返佣", "正文"))),
    ]


CARD_CASES = all_cards()
CARD_IDS = [name for name, _ in CARD_CASES]
CARD_VALUES = [card for _, card in CARD_CASES]


def walk(node: Any):
    """深度遍历卡片里的每一个组件 dict。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def components(card: dict[str, Any], tag: str) -> list[dict[str, Any]]:
    return [n for n in walk(card) if n.get("tag") == tag]


# ---------- 卡片骨架 ----------


@pytest.mark.parametrize("card", CARD_VALUES, ids=CARD_IDS)
def test_每张卡都显式声明_schema_2_0(card):
    """不写 schema 会被当成 1.0 结构解析，body/form 这些 2.0 才有的字段直接失效。"""
    assert card["schema"] == "2.0"


@pytest.mark.parametrize("card", CARD_VALUES, ids=CARD_IDS)
def test_元素挂在_body_下而不是顶层(card):
    """1.0 把 elements 放在顶层，2.0 必须放在 body 里。"""
    assert "elements" not in card
    assert isinstance(card["body"]["elements"], list)


@pytest.mark.parametrize("card", CARD_VALUES, ids=CARD_IDS)
def test_标题主题色是合法枚举值(card):
    header = card.get("header")
    if header is None:
        return
    assert header["template"] in VALID_TEMPLATES
    assert header["title"]["tag"] == "plain_text"
    assert header["title"]["content"]


@pytest.mark.parametrize("card", CARD_VALUES, ids=CARD_IDS)
def test_富文本字号是合法枚举值(card):
    for markdown in components(card, "markdown"):
        assert markdown["content"], "markdown 组件的 content 不能为空"
        if "text_size" in markdown:
            assert markdown["text_size"] in VALID_TEXT_SIZES


# ---------- 表单容器 ----------


@pytest.mark.parametrize("card", CARD_VALUES, ids=CARD_IDS)
def test_表单容器只能挂在卡片根节点(card):
    """文档明确：表单容器不能被其它组件内嵌，只能放在 body.elements 下面。"""
    top_level = [e for e in card["body"]["elements"] if e.get("tag") == "form"]
    assert components(card, "form") == top_level


@pytest.mark.parametrize("card", CARD_VALUES, ids=CARD_IDS)
def test_表单容器有全局唯一的_name(card):
    forms = components(card, "form")
    names = [f.get("name") for f in forms]
    assert all(names), "表单容器的 name 必填"
    assert len(set(names)) == len(names)


@pytest.mark.parametrize("card", CARD_VALUES, ids=CARD_IDS)
def test_表单内的交互组件都有唯一的_name(card):
    """name 为空或重复时平台回 200530，用户点了提交没有任何反应。"""
    for form in components(card, "form"):
        names = [
            node.get("name")
            for node in walk(form)
            if node is not form and node.get("tag") in INTERACTIVE_TAGS
        ]
        assert names, "表单里一个交互组件都没有"
        assert all(names), f"表单 {form['name']} 里有交互组件没写 name"
        assert len(set(names)) == len(names), f"表单 {form['name']} 里的 name 有重复"


@pytest.mark.parametrize("card", CARD_VALUES, ids=CARD_IDS)
def test_每个表单恰好有一个提交按钮(card):
    for form in components(card, "form"):
        submits = [
            node
            for node in walk(form)
            if node.get("tag") == "button" and node.get("form_action_type") == "submit"
        ]
        assert len(submits) == 1, f"表单 {form['name']} 需要且只需要一个提交按钮"


@pytest.mark.parametrize("card", CARD_VALUES, ids=CARD_IDS)
def test_不再使用_1_0_的_action_type(card):
    """``action_type: form_submit`` 是卡片 1.0 的写法，2.0 已标记废弃。

    2.0 里表单内按钮的必填属性是 ``form_action_type``，取值 submit / reset。
    """
    for node in walk(card):
        assert "action_type" not in node, f"{node.get('tag')} 还在用废弃的 action_type"


@pytest.mark.parametrize("card", CARD_VALUES, ids=CARD_IDS)
def test_提交按钮带回调_behavior(card):
    """按钮不配 callback 的话，表单提交上来我们认不出这是哪个动作。"""
    for form in components(card, "form"):
        for button in components(form, "button"):
            if button.get("form_action_type") != "submit":
                continue
            behaviors = button["behaviors"]
            callbacks = [b for b in behaviors if b["type"] == "callback"]
            assert len(callbacks) == 1
            assert callbacks[0]["value"]["action"]


# ---------- 下拉选择 ----------


def test_渠道下拉的选项结构符合文档():
    card = cards.client_form_card(REFERRAL_OPTIONS)
    (select,) = components(card, "select_static")

    assert select["name"] == cards.F_CLIENT_REFERRAL
    assert select["required"] is True

    values = []
    for option in select["options"]:
        assert option["text"]["tag"] == "plain_text"
        assert option["text"]["content"]
        assert isinstance(option["value"], str) and option["value"]
        values.append(option["value"])

    # 同一个下拉里 value 重复的话，服务端分不清用户点的是哪一个
    assert len(set(values)) == len(values)
    # 回传上来的就是 value，必须是渠道编号，_submit_client 靠它反查渠道
    assert values == ["R001", "R002"]


def test_下拉选择不带文档里没有的_label_字段():
    """``select_static`` 没有 label 属性（只有 input 有）。

    写了不会报错，但标题根本不渲染 —— 用户看到一个光秃秃的下拉框，
    不知道要选什么。标题得单独用一个富文本组件顶上。
    """
    card = cards.client_form_card(REFERRAL_OPTIONS)
    (select,) = components(card, "select_static")
    assert "label" not in select

    form = components(card, "form")[0]
    labels = [n["content"] for n in components(form, "markdown")]
    assert any("所属渠道" in text for text in labels), "下拉框上面要有一行说明它是什么"


def test_没有渠道时不给表单而是给提示():
    """选项为空的下拉框点开是空的，用户只会以为卡片坏了。"""
    card = cards.client_form_card([])
    assert components(card, "form") == []
    assert components(card, "select_static") == []


# ---------- 输入框 ----------


def test_输入框带_label_和_placeholder():
    """input 是文档里明确支持 label 的组件，表单里的输入项都该有标题。"""
    card = cards.referral_form_card()
    inputs = components(card, "input")
    assert len(inputs) == 3

    for node in inputs:
        assert node["label"]["tag"] == "plain_text"
        assert node["label"]["content"]
        assert node["placeholder"]["tag"] == "plain_text"
        assert isinstance(node["required"], bool)


def test_表单不再收地址和收款信息():
    """模板（2026-09-17）里没有这两项；表单按模板收开始日期和结算频率（2026-09-18 定的）。"""
    card = cards.referral_form_card()
    labels = [n["label"]["content"] for n in components(card, "input")]
    assert labels == ["渠道名称", "邮箱", "分佣比例 (%)"]


# ---------- 日期选择器 ----------


def test_开始日期是必填的日期选择器():
    """回传的是毫秒时间戳而不是 YYYY-MM-DD 文本，这一项必须有值。"""
    card = cards.referral_form_card()
    (picker,) = components(card, "date_picker")

    assert picker["name"] == cards.F_REFERRAL_START_DATE
    assert picker["required"] is True
    assert picker["placeholder"]["tag"] == "plain_text"
    assert picker["placeholder"]["content"]
    # 日期选择器和下拉一样没有 label 属性，写了也不渲染
    assert "label" not in picker

    form = components(card, "form")[0]
    titles = [n["content"] for n in components(form, "markdown")]
    assert any("开始日期" in text for text in titles), "日期选择器上面要有一行说明它是什么"


def test_结算频率下拉的取值是模板原文():
    """值必须写 Monthly / Quarterly：导入的历史行就是这两个字符串，翻译会让单选列出现两套值。"""
    card = cards.referral_form_card()
    (select,) = components(card, "select_static")

    assert select["name"] == cards.F_REFERRAL_PAYOUT
    assert select["required"] is True
    assert [option["value"] for option in select["options"]] == [
        schema.PAYOUT_MONTHLY,
        schema.PAYOUT_QUARTERLY,
    ]

    form = components(card, "form")[0]
    titles = [n["content"] for n in components(form, "markdown")]
    assert any("结算频率" in text for text in titles), "下拉框上面要有一行说明它是什么"


# ---------- 主菜单 ----------


def _button_callbacks(card: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for button in components(card, "button"):
        (callback,) = [b for b in button["behaviors"] if b["type"] == "callback"]
        found.append((button["text"]["content"], callback["value"]))
    return found


def test_渠道列表每条可点且底部能回目录():
    """列表不再是一段点不了的文本。每条渠道一个按钮，底部能回目录，不靠再发一条消息。"""
    card = cards.referral_list_card(REFERRAL_OPTIONS)
    assert components(card, "form") == []

    callbacks = _button_callbacks(card)
    assert (
        "R001 北极星资本",
        {"action": cards.ACTION_OPEN_REFERRAL, "referral_no": "R001"},
    ) in callbacks
    assert (
        "R002 鲸落数字",
        {"action": cards.ACTION_OPEN_REFERRAL, "referral_no": "R002"},
    ) in callbacks
    assert ("返回目录", {"action": cards.ACTION_OPEN_MENU}) in callbacks


def test_渠道列表按八条一页切开():
    items = [(f"R{i:03d}", f"渠道{i}") for i in range(1, 11)]

    first = _button_callbacks(cards.referral_list_card(items, page=0))
    first_nos = [value["referral_no"] for _, value in first if "referral_no" in value]
    assert first_nos == [f"R{i:03d}" for i in range(1, 9)]
    assert ("下一页", {"action": cards.ACTION_LIST_REFERRALS, "page": 1}) in first
    assert all(value.get("page") != 0 for _, value in first)

    second = _button_callbacks(cards.referral_list_card(items, page=1))
    second_nos = [value["referral_no"] for _, value in second if "referral_no" in value]
    assert second_nos == ["R009", "R010"]
    assert ("上一页", {"action": cards.ACTION_LIST_REFERRALS, "page": 0}) in second
    assert all(text != "下一页" for text, _ in second)

    # 页码超出最后一页时停在最后一页，而不是给一张空卡
    clamped = _button_callbacks(cards.referral_list_card(items, page=99))
    assert [value["referral_no"] for _, value in clamped if "referral_no" in value] == second_nos


def test_空渠道列表也能回目录():
    card = cards.referral_list_card([])
    assert components(card, "form") == []
    assert _button_callbacks(card) == [("返回目录", {"action": cards.ACTION_OPEN_MENU})]
    text = "\n".join(node["content"] for node in components(card, "markdown"))
    assert "还没有" in text


def test_未命名渠道的按钮不留空尾巴():
    callbacks = _button_callbacks(cards.referral_list_card([("R006", "")]))
    assert (
        "R006（未命名）",
        {"action": cards.ACTION_OPEN_REFERRAL, "referral_no": "R006"},
    ) in callbacks


def test_渠道详情把特别信息和空值分开():
    card = cards.referral_detail_card(
        no="R001",
        name="北极星资本",
        status="生效",
        sales_name="Alice",
        start_date="2026-01-15",
        rate="20%",
        payout="Monthly",
        email="a@b.com",
        submitted_on="2026-01-16",
        address="",
        payment="USDT TRC20 abc",
    )
    text = "\n".join(node["content"] for node in components(card, "markdown"))
    assert "**是谁**" in text
    assert "**怎么分**" in text
    assert "**特别信息**" in text
    assert "编号：R001" in text
    assert "分佣比例：20%" in text
    assert "地址：未填写" in text
    assert "收款信息：USDT TRC20 abc" in text

    assert _button_callbacks(card) == [
        ("返回列表", {"action": cards.ACTION_LIST_REFERRALS}),
        ("返回目录", {"action": cards.ACTION_OPEN_MENU}),
    ]
    assert components(card, "form") == []


def test_提示卡和结果卡不加返回目录():
    """返回目录只加在渠道列表和详情上。共用的提示卡一加，查询中和登记结果也会多一个按钮。"""
    untouched = [
        cards.notice_card("标题", "正文"),
        cards.success_card("成了", "正文"),
        cards.error_card("出错了"),
        cards.commission_result_card("佣金明细", "正文"),
        cards.menu_card("张三"),
    ]
    for card in untouched:
        actions = {value["action"] for _, value in _button_callbacks(card)}
        assert cards.ACTION_OPEN_MENU not in actions


def test_主菜单按钮都带回调且不在表单里():
    card = cards.menu_card("张三")
    assert components(card, "form") == []

    actions = set()
    for button in components(card, "button"):
        # 表单外的按钮不需要 name，但必须能回传出一个动作
        (callback,) = [b for b in button["behaviors"] if b["type"] == "callback"]
        actions.add(callback["value"]["action"])

    assert actions == MENU_ACTIONS


def test_卡片上没有归属销售输入项():
    """归属只能由回调里的 open_id 决定。做成输入项等于让人自报家门。"""
    for _, card in all_cards():
        for node in walk(card):
            name = node.get("name") or ""
            assert "owner" not in name.lower()
            assert "open_id" not in name.lower()


# ---------- 连通性自检卡片 ----------
#
# ws_smoke.py 那张卡是用户第一次见到的卡片，而它是硬编码在脚本里的，
# 不会被上面任何一条断言覆盖。它渲染不出来的话，人会以为是凭证或长连接的问题，
# 排查方向从第一步就歪了。


def smoke_card() -> dict[str, Any]:
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "ws_smoke.py"
    spec = importlib.util.spec_from_file_location("ws_smoke", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SMOKE_CARD


def test_自检卡片同样符合_2_0_规范():
    card = smoke_card()

    assert card["schema"] == "2.0"
    assert card["header"]["template"] in VALID_TEMPLATES
    assert "elements" not in card

    (form,) = components(card, "form")
    assert form["name"]

    (button,) = components(form, "button")
    assert button["form_action_type"] == "submit"
    assert button["name"]
    assert "action_type" not in button

    (text_input,) = components(form, "input")
    assert text_input["name"]
    assert text_input["required"] is False


# ---------- 结果卡底部的菜单 ----------


def _menu_actions(card: dict[str, Any]) -> set[str]:
    actions = set()
    for button in components(card, "button"):
        for behavior in button.get("behaviors", []):
            if behavior["type"] == "callback":
                actions.add(behavior["value"]["action"])
    return actions


MENU_ACTIONS = {
    cards.ACTION_OPEN_REFERRAL_FORM,
    cards.ACTION_OPEN_CLIENT_FORM,
    cards.ACTION_LIST_REFERRALS,
    cards.ACTION_OPEN_COMMISSION_QUERY,
    # ECAS 单独一个入口，不并进「佣金查询」：两笔钱、两套比例、两张汇总表。
    cards.ACTION_OPEN_ECAS_QUERY,
}


def test_结果卡接上菜单后带齐全部入口():
    """卡片回调是原地替换：做完一件事，会话里只剩这张卡。

    它上面没有按钮的话，要做下一件只能重新打字 —— 这正是要修掉的那个体验。
    """
    assert _menu_actions(cards.with_menu(cards.success_card("成了", "正文"))) == MENU_ACTIONS


def test_原卡不会被就地改掉():
    """调用方常常复用同一张卡的构造结果，改坏了会累积出四个八个菜单。"""
    card = cards.success_card("成了", "正文")
    before = len(card["body"]["elements"])
    cards.with_menu(card)
    assert len(card["body"]["elements"]) == before


def test_菜单按钮不在表单里():
    """表单外的按钮才是普通回调；掉进 form 里会变成表单动作，点了不是我们要的行为。"""
    card = cards.with_menu(cards.success_card("成了", "正文"))
    assert components(card, "form") == []


def test_菜单里的按钮和主菜单完全一致():
    """两处分头维护的话，早晚有一边少一个入口。"""
    assert _menu_actions(cards.menu_card("张三")) == _menu_actions(
        cards.with_menu(cards.notice_card("标题", "正文"))
    )


def test_分割线就是裸的_hr():
    """`hr` 是这套卡片里唯一没在真机上发过的组件，而它出现在每一张结果卡上。

    形状对齐 SDK 自己的 `lark_oapi.channel.card.CardBuilder.divider()`：
    它发的就是裸的 `{"tag": "hr"}`。加属性属于没必要的赌。
    """
    card = cards.with_menu(cards.success_card("成了", "正文"))
    dividers = components(card, "hr")
    assert dividers == [{"tag": "hr"}]


# ---------- ECAS 那两张卡 ----------


def test_ECAS查询卡的月份下拉预选最新月份():
    card = cards.ecas_query_card("2026-09", ["2026-07", "2026-08", "2026-09"])
    (select,) = components(card, "select_static")
    assert select["name"] == cards.F_ECAS_PERIOD
    assert select["initial_option"] == "2026-09"
    # 最新的排在最前面：绝大多数查询都是「上个月」
    assert [o["value"] for o in select["options"]] == ["2026-09", "2026-08", "2026-07"]


def test_预选月份不在候选里时不写这个key():
    """``initial_option`` 给一个不在 options 里的值，飞书会拒掉整张卡。"""
    card = cards.ecas_query_card("2099-01", ["2026-08"])
    (select,) = components(card, "select_static")
    assert "initial_option" not in select


def test_一个月份都没有时退回文本框():
    """下拉是空的话点开什么都没有，用户只会以为卡片坏了。"""
    card = cards.ecas_query_card("", [])
    assert components(card, "select_static") == []
    (text_input,) = components(card, "input")
    assert text_input["name"] == cards.F_ECAS_PERIOD


def test_ECAS的卡和交易佣金的卡颜色不一样():
    """两笔钱在会话里往上翻的时候要一眼分得开。"""
    ecas_template = cards.ecas_result_card("ECAS 返佣", "正文")["header"]["template"]
    trade_template = cards.commission_result_card("佣金明细", "正文")["header"]["template"]
    assert ecas_template != trade_template


def test_两张查询卡的表单名和字段名都不冲突():
    """同一个会话里两张卡都可能在，name 撞了平台会认错是哪个动作。"""
    ecas_card = cards.ecas_query_card("2026-09", ["2026-09"])
    trade_card = cards.commission_query_card("2026-09", ["2026-09"])
    assert cards.F_ECAS_PERIOD != cards.F_QUERY_PERIOD
    assert components(ecas_card, "form")[0]["name"] != components(trade_card, "form")[0]["name"]


# ---------- 表单卡的退路 ----------


def test_两张表单卡都有返回目录():
    """按错了进来、看一眼不想填了，得走得掉。改之前只能重新发一条消息。"""
    for card in (cards.referral_form_card(), cards.client_form_card(REFERRAL_OPTIONS)):
        assert cards.ACTION_OPEN_MENU in _menu_actions(card)


def test_返回目录按钮在表单容器外面():
    """掉进 form 里它就变成表单动作：必填项没填就退不出去，
    而退出去正是这个按钮的全部用途。"""
    for card in (cards.referral_form_card(), cards.client_form_card(REFERRAL_OPTIONS)):
        (form,) = components(card, "form")
        inside = [b["text"]["content"] for b in components(form, "button")]
        assert inside == ["提交登记"]
        top_level = [
            e["text"]["content"] for e in card["body"]["elements"] if e.get("tag") == "button"
        ]
        assert "返回目录" in top_level


def test_表单卡只给退路不给整个菜单():
    """表单有自己的提交按钮，底下再堆五个入口只会让人点错。"""
    actions = _menu_actions(cards.referral_form_card())
    assert actions == {cards.ACTION_SUBMIT_REFERRAL, cards.ACTION_OPEN_MENU}
