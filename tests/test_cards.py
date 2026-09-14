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
    assert len(inputs) == 5

    for node in inputs:
        assert node["label"]["tag"] == "plain_text"
        assert node["label"]["content"]
        assert node["placeholder"]["tag"] == "plain_text"
        assert isinstance(node["required"], bool)


def test_地址是选填其余必填():
    card = cards.referral_form_card()
    optional = {n["name"] for n in components(card, "input") if not n["required"]}
    assert optional == {cards.F_REFERRAL_ADDRESS}


# ---------- 主菜单 ----------


def test_主菜单按钮都带回调且不在表单里():
    card = cards.menu_card("张三")
    assert components(card, "form") == []

    actions = set()
    for button in components(card, "button"):
        # 表单外的按钮不需要 name，但必须能回传出一个动作
        (callback,) = [b for b in button["behaviors"] if b["type"] == "callback"]
        actions.add(callback["value"]["action"])

    assert actions == {
        cards.ACTION_OPEN_REFERRAL_FORM,
        cards.ACTION_OPEN_CLIENT_FORM,
        cards.ACTION_LIST_REFERRALS,
        cards.ACTION_OPEN_COMMISSION_QUERY,
    }


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
