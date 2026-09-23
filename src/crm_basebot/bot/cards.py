"""飞书卡片 JSON（schema 2.0）。

表单容器的关键性质：容器内的组件不会各自触发回调，只有点「提交」按钮时才把
整批数据一次性回传，回调里带 ``form_value``。这正是我们要的 —— 一次交互录入
一条完整记录。

卡片上**没有**「归属销售」这类输入项，归属一律由回调里的 open_id 决定。
把它做成输入项等于让人自报家门。
"""

from __future__ import annotations

from typing import Any

from ..domain import schema

ACTION_OPEN_REFERRAL_FORM = "open_referral_form"
ACTION_OPEN_CLIENT_FORM = "open_client_form"
ACTION_SUBMIT_REFERRAL = "submit_referral"
ACTION_SUBMIT_CLIENT = "submit_client"
ACTION_LIST_REFERRALS = "list_referrals"
ACTION_OPEN_COMMISSION_QUERY = "open_commission_query"
ACTION_QUERY_COMMISSION = "query_commission"
ACTION_OPEN_ECAS_QUERY = "open_ecas_query"
ACTION_QUERY_ECAS = "query_ecas"

# 表单项标识，回调的 form_value 里用它取值
F_REFERRAL_NAME = "referral_name"
F_REFERRAL_EMAIL = "referral_email"
F_REFERRAL_START_DATE = "referral_start_date"
F_REFERRAL_RATE = "referral_rate"
F_REFERRAL_PAYOUT = "referral_payout"
F_CLIENT_UID = "client_uid"
F_CLIENT_NAME = "client_name"
F_CLIENT_REFERRAL = "client_referral"
F_QUERY_PERIOD = "query_period"
F_ECAS_PERIOD = "ecas_period"


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


def _date_picker(name: str, placeholder: str, *, required: bool = True) -> dict[str, Any]:
    """日期选择器。

    和下拉一样没有 ``label`` 属性，标题只能用富文本组件顶上。回传的是**毫秒时间戳**
    而不是 ``YYYY-MM-DD`` 文本，解析在 ``handlers._form_date`` 里做。
    """
    return {
        "tag": "date_picker",
        "name": name,
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "required": required,
        "width": "fill",
        "margin": "0px 0px 8px 0px",
    }


def _select(
    name: str,
    placeholder: str,
    options: list[tuple[str, str]],
    *,
    required: bool = True,
) -> dict[str, Any]:
    """单选下拉。``options`` 是 [(给人看的文字, 回传给我们的值)]。

    两者分开传是有意的：标签想写「Monthly（按月）」，但写进 Base 的必须是模板原文
    ``Monthly``，否则单选列里会多出一堆同义选项。
    """
    return {
        "tag": "select_static",
        "name": name,
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "required": required,
        "width": "fill",
        "options": [
            {"text": {"tag": "plain_text", "content": label}, "value": value}
            for label, value in options
        ],
        "margin": "0px 0px 8px 0px",
    }


# 结算频率下拉的选项：标签带中文提示，值保持模板原文。
PAYOUT_CHOICES: list[tuple[str, str]] = [
    (f"{schema.PAYOUT_MONTHLY}（按月）", schema.PAYOUT_MONTHLY),
    (f"{schema.PAYOUT_QUARTERLY}（按季）", schema.PAYOUT_QUARTERLY),
]


def _menu_button(text: str, action: str, *, primary: bool = False) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": "primary" if primary else "default",
        "width": "fill",
        "margin": "0px 0px 8px 0px",
        "behaviors": [{"type": "callback", "value": {"action": action}}],
    }


def _menu_buttons() -> list[dict[str, Any]]:
    """主菜单那四个按钮。

    三个按钮垂直堆叠、每个撑满宽度。之前用 column_set 三等分横排，手机屏窄的时候
    每列只放得下 3-4 个字，「登记新渠道」被截成「登记..」。垂直排列纵向多占一点
    空间，但任何设备都能把标签完整显示出来。
    """
    return [
        _menu_button("登记新渠道", ACTION_OPEN_REFERRAL_FORM, primary=True),
        _menu_button("登记新客户", ACTION_OPEN_CLIENT_FORM),
        _menu_button("我的渠道", ACTION_LIST_REFERRALS),
        _menu_button("佣金查询", ACTION_OPEN_COMMISSION_QUERY),
        # ECAS 单独一个入口，不并进「佣金查询」。两笔钱、两套比例、两张汇总表，
        # 混在一个按钮后面只会让人分不清自己看的是哪一笔（见 docs/ECAS.md）。
        _menu_button("ECAS 返佣", ACTION_OPEN_ECAS_QUERY),
    ]


def with_menu(card: dict[str, Any]) -> dict[str, Any]:
    """在一张结果卡的底部接上主菜单。

    **为什么每张结果卡都要带菜单**：卡片回调的返回值是「原地替换」——
    点「登记新渠道」，菜单卡就被表单卡盖掉；点「提交」，表单卡又被成功卡盖掉。
    做完一件事，会话里只剩一张没有任何按钮的成功卡，要再做下一件只能重新打字。

    另一条路是提交完之后再 push 一条新的菜单消息，但那样每操作一次就多两条消息，
    会话很快被刷满。接在结果卡底部，做完的结果和下一步的入口在同一张卡上，
    消息数不变。

    返回的是新 dict，不改传进来的那张 —— 调用方常常复用同一张卡的构造结果。
    """
    body = card.get("body", {})
    elements = list(body.get("elements", []))
    # 分割线就写成 SDK 自己 `CardBuilder.divider()` 发出去的那个形状：裸的
    # `{"tag": "hr"}`，不加 margin。这是整套卡片里唯一一个没在真机上发过的组件，
    # 而它现在会出现在**每一张**结果卡上 —— 渲染不出来的话是全线故障，不是一处。
    # 按钮和上面那行字本来就各自带 margin，省掉它不影响间距。
    elements.append({"tag": "hr"})
    elements.append(_text("**接下来做什么？**"))
    elements.extend(_menu_buttons())
    return {**card, "body": {**body, "elements": elements}}


def menu_card(sales_name: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "渠道佣金助手"},
            "template": "blue",
        },
        "body": {
            "elements": [
                _text(f"**{sales_name}**，你要做什么？"),
                *_menu_buttons(),
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
                        # 日期选择器和下拉都没有 label，标题得用富文本单独顶一行，
                        # 否则用户看到一个没有任何说明的控件。
                        _text("**开始日期**"),
                        _date_picker(F_REFERRAL_START_DATE, "选择合作开始日期"),
                        _input(F_REFERRAL_RATE, "分佣比例 (%)", "例如 20 表示 20%"),
                        _text("**结算频率**"),
                        _select(F_REFERRAL_PAYOUT, "选择结算频率", list(PAYOUT_CHOICES)),
                        _submit("referral_submit", ACTION_SUBMIT_REFERRAL, "提交登记"),
                    ],
                },
                _text(
                    "<font color='grey'>编号由系统自动生成，提交日期记为今天，"
                    "归属人自动记为你本人。</font>",
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
        (f"{no} {name}".strip() if name else f"{no}（未命名）", no) for no, name in referral_options
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
                        _select(F_CLIENT_REFERRAL, "选择一个你名下的渠道", options),
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


def footnote(content: str) -> dict[str, Any]:
    """卡片底部那行灰色小字。公开出来，免得调用方去用 ``_text``。"""
    return _text(f"<font color='grey'>{content}</font>", size="notation")


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


# 一个渠道下最多列几个客户名。超过就折叠 —— 客户多的渠道会把整张卡撑到要滑很久，
# 而「我的渠道」的用途是快速确认「有哪些渠道、各自多大」，不是客户名册。
MAX_CLIENTS_SHOWN = 10


def _rate_text(rate_percent: float | None) -> str:
    """20.0 -> "20%"，None -> "比例未填"。

    去掉没意义的尾零：Base 里存的是数字，读回来是 float，直接拼会显示成「20.0%」。
    """
    if rate_percent is None:
        return "比例未填"
    text = f"{rate_percent:.10f}".rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def referral_list_card(items: list[Any]) -> dict[str, Any]:
    """``items`` 是 ``domain.referral.ReferralDetail``，每个带比例和名下客户。"""
    if not items:
        return notice_card("我的渠道", "你名下还没有登记任何渠道。")

    blocks: list[str] = []
    for item in items:
        # 名字为空时留一个占位（比如 R006 是在 Base 里直接建的、渠道名称字段没填），
        # 避免渲染成「**R006** 」这种末尾一个空格、看着像 bug 的行。
        name = item.name or "（未命名，建议到 Base 里补齐）"
        head = f"**{item.no}** {name} · {_rate_text(item.rate_percent)}"
        # 状态只在不是「生效」时才标出来。正常的渠道每行都缀一个「生效」是噪音，
        # 但停用的渠道混在列表里不作声，会让人以为它还在算钱。
        if item.status and item.status != schema.STATUS_ACTIVE:
            head += f" · **{item.status}**"
        head += f" · {len(item.client_names)} 个客户"
        lines = [head]

        for client in item.client_names[:MAX_CLIENTS_SHOWN]:
            lines.append(f"　· {client or '（未命名客户）'}")
        hidden = len(item.client_names) - MAX_CLIENTS_SHOWN
        if hidden > 0:
            lines.append(f"　· …… 还有 {hidden} 个，完整名单在 Base 里")
        if not item.client_names:
            lines.append("　· 还没有登记客户")
        blocks.append("\n".join(lines))

    total_clients = sum(len(item.client_names) for item in items)
    body = f"共 **{len(items)}** 个渠道、**{total_clients}** 个客户\n\n" + "\n\n".join(blocks)
    return notice_card("我的渠道", body, template="blue")


def _period_selector(name: str, default_period: str, period_options: list[str]) -> dict[str, Any]:
    """月份下拉。没有可选月份时退回文本框。

    ``period_options`` 为空是极少数情况（表是空的）。给个文本框兜底，让人至少能自己
    敲一个 YYYY-MM 查 —— 结果多半是「没有可展示的明细」，但这比一片空白强：
    它明确告诉了用户「查得动，只是没数据」。
    """
    if not period_options:
        return _input(name, "月份 YYYY-MM", default_period or "2026-09")

    selector: dict[str, Any] = {
        "tag": "select_static",
        "name": name,
        "placeholder": {"tag": "plain_text", "content": "选择月份"},
        "required": True,
        "width": "fill",
        "options": [
            {"text": {"tag": "plain_text", "content": p}, "value": p}
            for p in sorted(period_options, reverse=True)
        ],
        "margin": "0px 0px 8px 0px",
    }
    # initial_option 为 None 时飞书不认这个 key，所以只在有值时才加
    if default_period in period_options:
        selector["initial_option"] = default_period
    return selector


def ecas_query_card(default_period: str, period_options: list[str]) -> dict[str, Any]:
    """ECAS 返佣查询：选月份，回调 ACTION_QUERY_ECAS。"""
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "ECAS 返佣查询"},
            "template": "turquoise",
        },
        "body": {
            "elements": [
                {
                    "tag": "form",
                    "name": "ecas_query_form",
                    "elements": [
                        _text("**结算月份**"),
                        _period_selector(F_ECAS_PERIOD, default_period, period_options),
                        _submit("ecas_query_submit", ACTION_QUERY_ECAS, "查询"),
                    ],
                },
                footnote(
                    "ECAS 开户返佣，和交易佣金是两笔钱。"
                    "只显示你名下渠道介绍的开户；管理员可以看全部。"
                ),
            ]
        },
    }


def ecas_result_card(title: str, body_md: str) -> dict[str, Any]:
    """ECAS 返佣结果卡。

    标题色和交易佣金那张（blue）刻意不同：两笔钱在会话里往上翻的时候要一眼分得开。
    """
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "turquoise",
        },
        "body": {"elements": [_text(body_md)]},
    }


def commission_query_card(default_period: str, period_options: list[str]) -> dict[str, Any]:
    """佣金查询：选月份，回调 ACTION_QUERY_COMMISSION。

    ``period_options`` 是可选的月份列表（YYYY-MM）。为空时给一个手动输入的占位。
    有值时用下拉，避免用户拼错格式；``default_period`` 会预选到最新那一个。
    """
    selector = _period_selector(F_QUERY_PERIOD, default_period, period_options)

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
