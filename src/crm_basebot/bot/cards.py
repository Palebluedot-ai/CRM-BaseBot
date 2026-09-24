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
ACTION_OPEN_REFERRAL = "open_referral"
ACTION_OPEN_MENU = "open_menu"
ACTION_OPEN_COMMISSION_QUERY = "open_commission_query"
ACTION_QUERY_COMMISSION = "query_commission"
ACTION_OPEN_ECAS_QUERY = "open_ecas_query"
ACTION_QUERY_ECAS = "query_ecas"

# 管理员名下能有上百条渠道。
#
# **这个数是撞出来的，不是选出来的。** 一页八条要翻十三次（2026-09-24 反馈），改成
# 六十条之后飞书直接拒收整张卡：客户端弹 `200673`，服务端一行日志都没有 —— 回调响应
# 有上限，超了平台自己丢掉，我们这边看不见。原作者那句「一页八条，卡片还放得下按钮」
# 大概就是同一堵墙撞出来的。
#
# 二十条是往回收的一档：比八条少翻三分之二，又离六十条那个已知会炸的值足够远。
# 要再往上加，一次加十，每次都在真机上点一遍「我的渠道」——
# 这个上限没有文档，只能试。
REFERRAL_PAGE_SIZE = 20

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


def _callback_button(
    text: str,
    value: dict[str, Any],
    *,
    primary: bool = False,
    margin: str = "0px 0px 8px 0px",
) -> dict[str, Any]:
    """表单外的按钮。``value`` 必须是对象，裸字符串飞书反序列化时会直接抛掉。

    ``margin`` 给 ``"0px"`` 就是紧贴上一个按钮。渠道列表用它排成一整条，
    六十个按钮之间各留 8px 的话，光间距就多出快五百像素（2026-09-24 反馈）。
    """
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": "primary" if primary else "default",
        "width": "fill",
        "margin": margin,
        "behaviors": [{"type": "callback", "value": value}],
    }


def _menu_button(text: str, action: str, *, primary: bool = False) -> dict[str, Any]:
    return _callback_button(text, {"action": action}, primary=primary)


def _filled(value: str) -> str:
    text = value.strip() if value else ""
    return text if text else "未填写"


def _menu_buttons() -> list[dict[str, Any]]:
    """主菜单那几个按钮。

    垂直堆叠、每个撑满宽度。之前用 column_set 三等分横排，手机屏窄的时候每列只放得下
    3-4 个字，「登记新渠道」被截成「登记..」。垂直排列纵向多占一点空间，但任何设备
    都能把标签完整显示出来。
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


def back_to_menu_button() -> dict[str, Any]:
    """一个「返回目录」。表单卡用它当退路。

    **必须放在表单容器外面。** 掉进 ``{"tag": "form"}`` 里它就变成表单动作，点一下
    会连带触发表单校验 —— 必填项没填就退不出去，而退出去正是这个按钮的全部用途。
    """
    return _callback_button("返回目录", {"action": ACTION_OPEN_MENU})


def with_menu(card: dict[str, Any]) -> dict[str, Any]:
    """在一张**结果**卡的底部接上主菜单。

    卡片回调的返回值是「原地替换」：点「提交」，表单卡就被成功卡盖掉，会话里只剩
    一张没有任何按钮的卡，要再做下一件事只能重新打字。

    **只给结果卡用，不给导览卡用。** 「我的渠道」那条路上的列表卡、详情卡、找不到卡
    自己带「返回列表 / 返回目录」—— 看完一条渠道，下一步是往回走，不是重开一件事；
    而登记成功、查询出结果之后，下一步恰恰是重开一件事。两种卡片的下一步本来就不同，
    所以给的按钮也不同。在导览卡底下再堆五个入口只会让人点错。

    返回的是新 dict，不改传进来的那张 —— 调用方常常复用同一张卡的构造结果。
    """
    body = card.get("body", {})
    elements = list(body.get("elements", []))
    # 分割线就写成 SDK 自己 `CardBuilder.divider()` 发出去的那个形状：裸的
    # `{"tag": "hr"}`，不加 margin。这是整套卡片里唯一一个没在真机上发过的组件，
    # 而它会出现在每一张结果卡上 —— 渲染不出来的话是全线故障，不是一处。
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
                back_to_menu_button(),
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
                back_to_menu_button(),
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


def _referral_button_label(no: str, name: str) -> str:
    if name:
        return f"{no} {name}".strip()
    if no:
        return f"{no}（未命名）"
    return "（未命名）"


def referral_list_card(items: list[tuple[str, str]], *, page: int = 0) -> dict[str, Any]:
    """一页渠道，每条可点进详情，底部能回目录。

    空列表也留「返回目录」。不然这张卡换掉目录之后，只能再发一句话才能回去。
    """
    back = _callback_button("返回目录", {"action": ACTION_OPEN_MENU})
    if not items:
        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "我的渠道"},
                "template": "blue",
            },
            "body": {
                "elements": [
                    _text("你名下还没有登记任何渠道。"),
                    back,
                ]
            },
        }

    page_size = REFERRAL_PAGE_SIZE
    page_count = (len(items) + page_size - 1) // page_size
    current = min(max(page, 0), page_count - 1)
    start = current * page_size
    elements: list[dict[str, Any]] = [
        _text(
            f"共 {len(items)} 个，点一条查看。"
            + (f"第 {current + 1}/{page_count} 页。" if page_count > 1 else "")
        ),
    ]
    for no, name in items[start : start + page_size]:
        elements.append(
            _callback_button(
                _referral_button_label(no, name),
                {"action": ACTION_OPEN_REFERRAL, "referral_no": no},
                # 紧贴上一个：这一列是一整条名单，不是一堆各自独立的按钮。
                # 写满四个值 —— 一个值的简写平台文档说支持，但这次排查里它是另一个
                # 变量，不想两个可疑点搅在一起。
                margin="0px 0px 0px 0px",
            )
        )
    if current > 0:
        elements.append(
            _callback_button(
                "上一页",
                {"action": ACTION_LIST_REFERRALS, "page": current - 1},
            )
        )
    if current + 1 < page_count:
        elements.append(
            _callback_button(
                "下一页",
                {"action": ACTION_LIST_REFERRALS, "page": current + 1},
            )
        )
    elements.append(back)
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "我的渠道"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


# 详情卡里最多列几个客户名。超过就折叠 —— 客户多的渠道会把卡片撑到要滑很久，
# 而这张卡是「这条渠道最近怎么样」，不是客户名册。
MAX_CLIENTS_SHOWN = 15


def _money(value: Any) -> str:
    return "—" if value is None else f"{value:,.2f}"


def referral_detail_card(
    *,
    no: str,
    name: str,
    status: str,
    sales_name: str,
    start_date: str,
    rate: str,
    payout: str,
    email: str,
    submitted_on: str,
    address: str,
    payment: str,
    recent_fees: list[Any] | None = None,
    client_names: list[str] | None = None,
    history_failed: bool = False,
) -> dict[str, Any]:
    """只读。

    **顺序是「先钱，后资料」**（2026-09-24 反馈）：点进一条渠道，第一眼要看的是它最近
    挣了多少、带了哪些客户；编号邮箱地址那些是查证用的，往下挪但不删 —— 开发票时
    收款信息还是得查得到。

    ``recent_fees`` 是 ``domain.referral_history.MonthlyFee``。给 None 表示调用方没取
    （或者取失败，见 ``history_failed``）—— 那一节整个不显示，而不是显示一片空的：
    「这几个月没赚钱」和「这次没查到」不能长成一样。
    """
    title = name.strip() if name and name.strip() else (no.strip() or "渠道详情")
    elements: list[dict[str, Any]] = []

    if history_failed:
        elements.append(_text("**近几个月**\n这次没查到，Base 那边没读回来。下面的资料是准的。"))
    elif recent_fees:
        lines = ["**近 3 个月**"]
        for fee in recent_fees:
            lines.append(f"{fee.period}　交易 {_money(fee.trade)}　ECAS {_money(fee.ecas)}")
        if all(fee.is_empty for fee in recent_fees):
            # 三个月全是「—」时说一句。空表和「确实没有」在这张卡上长得一样，
            # 而前者通常意味着那几个月还没跑对账。
            lines.append("<font color='grey'>这几个月还没有结算记录。</font>")
        elements.append(_text("\n".join(lines)))

    if client_names is not None:
        lines = [f"**客户（{len(client_names)}）**"]
        if not client_names:
            lines.append("还没有登记客户。")
        for client in client_names[:MAX_CLIENTS_SHOWN]:
            lines.append(f"· {client or '（未命名客户）'}")
        hidden = len(client_names) - MAX_CLIENTS_SHOWN
        if hidden > 0:
            lines.append(f"<font color='grey'>…… 还有 {hidden} 个，完整名单在 Base 里</font>")
        elements.append(_text("\n".join(lines)))

    elements.append(
        _text(
            "**是谁**\n"
            f"编号：{_filled(no)}　状态：{_filled(status)}\n"
            f"负责销售：{_filled(sales_name)}"
        )
    )
    elements.append(
        _text(
            "**怎么分**\n"
            f"开始日期：{_filled(start_date)}　分佣比例：{_filled(rate)}\n"
            f"结算频率：{_filled(payout)}　邮箱：{_filled(email)}\n"
            f"提交日期：{_filled(submitted_on)}"
        )
    )
    # 地址和收款信息登记表单不收，但历史行里有。开发票要用，所以留着，只是排在最后。
    elements.append(_text(f"**特别信息**\n地址：{_filled(address)}\n收款信息：{_filled(payment)}"))
    elements.append(_callback_button("返回列表", {"action": ACTION_LIST_REFERRALS}))
    elements.append(_callback_button("返回目录", {"action": ACTION_OPEN_MENU}))

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def referral_missing_card() -> dict[str, Any]:
    """编号不存在，或不在当前这个人名下。不走共用的 error_card，否则回不去。"""
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "找不到这个渠道"},
            "template": "red",
        },
        "body": {
            "elements": [
                _text("这个编号不存在，或者不在你名下。"),
                _callback_button("返回列表", {"action": ACTION_LIST_REFERRALS}),
                _callback_button("返回目录", {"action": ACTION_OPEN_MENU}),
            ]
        },
    }


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
