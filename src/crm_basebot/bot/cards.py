"""飞书卡片 JSON（schema 2.0）。

表单容器的关键性质：容器内的组件不会各自触发回调，只有点「提交」按钮时才把
整批数据一次性回传，回调里带 ``form_value``。这正是我们要的 —— 一次交互录入
一条完整记录。

卡片上**没有**「归属销售」这类输入项，归属一律由回调里的 open_id 决定。
把它做成输入项等于让人自报家门。
"""

from __future__ import annotations

from typing import Any

ACTION_OPEN_REFERRAL_FORM = "open_referral_form"
ACTION_OPEN_CLIENT_FORM = "open_client_form"
ACTION_SUBMIT_REFERRAL = "submit_referral"
ACTION_SUBMIT_CLIENT = "submit_client"
ACTION_LIST_REFERRALS = "list_referrals"
ACTION_OPEN_COMMISSION_QUERY = "open_commission_query"
ACTION_QUERY_COMMISSION = "query_commission"

# 表单项标识，回调的 form_value 里用它取值
F_REFERRAL_NAME = "referral_name"
F_REFERRAL_EMAIL = "referral_email"
F_REFERRAL_ADDRESS = "referral_address"
F_REFERRAL_PAYMENT = "referral_payment"
F_REFERRAL_RATE = "referral_rate"
F_CLIENT_UID = "client_uid"
F_CLIENT_NAME = "client_name"
F_CLIENT_REFERRAL = "client_referral"
F_QUERY_PERIOD = "query_period"


def _text(content: str, size: str = "normal") -> dict[str, Any]:
    return {"tag": "markdown", "content": content, "text_size": size}


def _input(name: str, label: str, placeholder: str, *, required: bool = True) -> dict[str, Any]:
    return {
        "tag": "input",
        "name": name,
        "label": {"tag": "plain_text", "content": label},
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "required": required,
        "margin": "0px 0px 8px 0px",
    }


def _submit(name: str, action: str, text: str = "提交") -> dict[str, Any]:
    """表单容器里的提交按钮。

    ``form_action_type`` 不能写成 1.0 时代的 ``action_type: "form_submit"`` ——
    在 JSON 2.0 里 ``form_action_type`` 是表单内按钮的必填属性，``action_type``
    已经标记为废弃。``name`` 同样必填且要在整张卡片内唯一，否则平台回 200530。
    """
    return {
        "tag": "button",
        "name": name,
        "text": {"tag": "plain_text", "content": text},
        "type": "primary",
        "form_action_type": "submit",
        "behaviors": [{"type": "callback", "value": {"action": action}}],
    }


def _menu_button(text: str, action: str, *, primary: bool = False) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": "primary" if primary else "default",
        "width": "fill",
        "margin": "0px 0px 8px 0px",
        "behaviors": [{"type": "callback", "value": {"action": action}}],
    }


def menu_card(sales_name: str) -> dict[str, Any]:
    # 三个按钮垂直堆叠、每个撑满宽度。之前用 column_set 三等分横排，手机屏
    # 窄的时候每列只放得下 3-4 个字，「登记新渠道」被截成「登记..」。垂直
    # 排列纵向多占一点空间，但任何设备都能把标签完整显示出来。
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "渠道佣金助手"},
            "template": "blue",
        },
        "body": {
            "elements": [
                _text(f"**{sales_name}**，你要做什么？"),
                _menu_button("登记新渠道", ACTION_OPEN_REFERRAL_FORM, primary=True),
                _menu_button("登记新客户", ACTION_OPEN_CLIENT_FORM),
                _menu_button("我的渠道", ACTION_LIST_REFERRALS),
                _menu_button("佣金查询", ACTION_OPEN_COMMISSION_QUERY),
            ]
        },
    }


def referral_form_card() -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "登记新渠道"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "form",
                    "name": "referral_form",
                    "elements": [
                        _input(F_REFERRAL_NAME, "渠道名称", "例如 ABC Capital"),
                        _input(F_REFERRAL_EMAIL, "邮箱", "contact@example.com"),
                        _input(F_REFERRAL_ADDRESS, "地址", "用于合同和付款", required=False),
                        _input(F_REFERRAL_PAYMENT, "收款信息", "银行账户或钱包地址"),
                        _input(F_REFERRAL_RATE, "分佣比例 (%)", "例如 20 表示 20%"),
                        _submit("referral_submit", ACTION_SUBMIT_REFERRAL, "提交登记"),
                    ],
                },
                _text(
                    "<font color='grey'>编号由系统自动生成，归属人自动记为你本人。</font>",
                    size="notation",
                ),
            ]
        },
    }


def client_form_card(referral_options: list[tuple[str, str]]) -> dict[str, Any]:
    """``referral_options`` 是 [(编号, 名称)]，只包含该销售名下的渠道。"""
    if not referral_options:
        return notice_card(
            "还不能登记客户",
            "你名下还没有渠道。请先登记渠道，再把客户挂到渠道下面。",
        )

    options = [
        {
            "text": {
                "tag": "plain_text",
                "content": f"{no} {name}".strip() if name else f"{no}（未命名）",
            },
            "value": no,
        }
        for no, name in referral_options
    ]

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "登记新客户"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "form",
                    "name": "client_form",
                    "elements": [
                        # 下拉选择组件没有 label 属性（只有输入框有），标题只能单独用
                        # 一个富文本组件顶上，官方示例也是这么做的。
                        _text("**所属渠道**"),
                        {
                            "tag": "select_static",
                            "name": F_CLIENT_REFERRAL,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "选择一个你名下的渠道",
                            },
                            "required": True,
                            "width": "fill",
                            "options": options,
                            "margin": "0px 0px 8px 0px",
                        },
                        _input(F_CLIENT_UID, "客户UID", "例如 577809207768677761"),
                        _input(F_CLIENT_NAME, "客户名称", "例如 PLUTO STUDIO LIMITED"),
                        _submit("client_submit", ACTION_SUBMIT_CLIENT, "提交登记"),
                    ],
                },
                _text(
                    "<font color='grey'>客户UID 要和交易明细表里的完全一致，"
                    "否则佣金对不上。</font>",
                    size="notation",
                ),
            ]
        },
    }


def notice_card(title: str, body: str, *, template: str = "grey") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "body": {"elements": [_text(body)]},
    }


def success_card(title: str, body: str) -> dict[str, Any]:
    return notice_card(title, body, template="green")


def error_card(body: str) -> dict[str, Any]:
    return notice_card("没能完成", body, template="red")


def referral_list_card(items: list[tuple[str, str]]) -> dict[str, Any]:
    if not items:
        return notice_card("我的渠道", "你名下还没有登记任何渠道。")

    # 名字为空时留一个占位（比如 R006 是在 Base 里直接建的、渠道名称字段没填），
    # 避免渲染成「- **R006** 」这种末尾一个空格、看着像 bug 的行。
    lines = "\n".join(
        f"- **{no}** {name if name else '（未命名，建议到 Base 里补齐）'}" for no, name in items
    )
    return notice_card("我的渠道", f"共 {len(items)} 个：\n\n{lines}", template="blue")


def commission_query_card(default_period: str, period_options: list[str]) -> dict[str, Any]:
    """佣金查询：选月份，回调 ACTION_QUERY_COMMISSION。

    ``period_options`` 是可选的月份列表（YYYY-MM）。为空时给一个手动输入的占位。
    有值时用下拉，避免用户拼错格式；``default_period`` 会预选到最新那一个。
    """
    if period_options:
        options = [
            {
                "text": {"tag": "plain_text", "content": p},
                "value": p,
            }
            for p in sorted(period_options, reverse=True)
        ]
        selector: dict[str, Any] = {
            "tag": "select_static",
            "name": F_QUERY_PERIOD,
            "placeholder": {"tag": "plain_text", "content": "选择月份"},
            "required": True,
            "width": "fill",
            "options": options,
            "initial_option": default_period if default_period in period_options else None,
            "margin": "0px 0px 8px 0px",
        }
        # initial_option 为 None 时飞书不认这个 key，去掉
        if selector["initial_option"] is None:
            del selector["initial_option"]
    else:
        # 极少数情况：连一个月份都取不到（看板空）。给一个文本框兜底，让用户
        # 至少能自己敲一个 YYYY-MM 查（结果多半是「没有可展示的明细」，但这
        # 比一片空白强 —— 它明确告诉了用户「查得动，只是没数据」）。
        selector = _input(
            F_QUERY_PERIOD,
            "月份 YYYY-MM",
            default_period or "2026-09",
        )

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "佣金查询"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "form",
                    "name": "commission_query_form",
                    "elements": [
                        _text("**结算月份**"),
                        selector,
                        _submit("commission_query_submit", ACTION_QUERY_COMMISSION, "查询"),
                    ],
                },
                _text(
                    "<font color='grey'>展示你名下每个渠道的应付佣金，"
                    "以及每个客户的贡献占比。管理员可以看全部渠道。</font>",
                    size="notation",
                ),
            ]
        },
    }


def commission_result_card(title: str, body_md: str) -> dict[str, Any]:
    """佣金明细结果卡。

    结果内容由 domain.commission_query.summarize() 生成，卡片这一层只负责套壳。
    单独一张卡是因为它可能相当长，独立标题也更容易在会话里定位。
    """
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "body": {"elements": [_text(body_md)]},
    }
