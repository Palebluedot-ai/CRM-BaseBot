"""组装并运行机器人。

    uv run python -m crm_basebot.app

用长连接接收事件，只需要服务器能出网，不需要公网入口 —— 这既是本地开发的便利，
也是内网部署唯一可行的方式。
"""

from __future__ import annotations

import logging
import threading
from zoneinfo import ZoneInfo

import lark_oapi as lark

from .bot.auth import SalesDirectory
from .bot.handlers import BotHandlers
from .domain.audit import AuditLog
from .domain.commission_query import CommissionQueryService
from .domain.ecas_query import EcasQueryService
from .domain.referral import ReferralService
from .domain.referral_history import ReferralHistoryService
from .domain.referred_client import ReferredClientService
from .lark.bitable import BitableClient
from .lark.client import get_client
from .lark.ws_patch import apply_card_frame_patch
from .startup import load_settings, require_settings

logger = logging.getLogger(__name__)

# 机器人跑起来至少要能读写这几张表：鉴权查名册、登记写渠道和客户、每一次写都记审计。
# 佣金查询按钮还要读日读看板 —— 缺表会在点按钮时报错，不在启动时硬拦，是刻意的：
# 先让机器人能跑，佣金查询是增量功能，不该拦住登记流。佣金汇总表由对账任务写。
REQUIRED_KEYS = (
    "LARK_BASE_APP_TOKEN",
    "TABLE_REFERRAL",
    "TABLE_CLIENT",
    "TABLE_AUDIT",
    "TABLE_SALES",
)


def build_handlers() -> BotHandlers:
    settings = load_settings()
    require_settings(settings, *REQUIRED_KEYS)

    bitable = BitableClient(settings.base_app_token)
    audit = AuditLog(bitable, settings.table_audit)
    # 卡片上选的日期、写进库的日期字段都按业务时区落成日历日，见 domain/dates.py。
    tz = ZoneInfo(settings.business_timezone)

    # 主字段回填走后台线程，不进卡片回调 3 秒预算。见 ReferralService.__init__
    # 里对 background 的说明。
    def _in_background(fn):
        threading.Thread(target=fn, daemon=True).start()

    # 佣金查询只在配了 TABLE_DAILY_BOARD 时启用。没配的话按钮点了会回「未启用」，
    # 而不是随便去读一个空 table_id 报一堆看不懂的错。
    commission_query = (
        CommissionQueryService(bitable, settings=settings) if settings.table_daily_board else None
    )

    # ECAS 是独立的第二套账（docs/ECAS.md）。没跑过 scripts/import_ecas.py 的租户
    # TABLE_ECAS 是空的，这时不注入 —— 「ECAS 返佣」按钮回一句「未启用」，
    # 而不是拿一个空 table_id 去读、报一堆看不懂的错。
    ecas_query = EcasQueryService(bitable, settings=settings) if settings.table_ecas else None

    # 渠道详情卡上的「近 3 个月」。汇总表没配就不注入 —— 那一节不显示，卡片其余照常。
    referral_history = (
        ReferralHistoryService(bitable, settings=settings) if settings.table_commission else None
    )

    return BotHandlers(
        client=get_client(),
        directory=SalesDirectory(bitable, settings.table_sales),
        referrals=ReferralService(
            bitable,
            settings.table_referral,
            audit,
            auto_number=settings.referral_auto_number,
            background=_in_background,
            tz=tz,
        ),
        clients=ReferredClientService(
            bitable, settings.table_client, settings.table_referral, audit
        ),
        commission_query=commission_query,
        ecas_query=ecas_query,
        referral_history=referral_history,
        background=_in_background,
        tz=tz,
    )


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # 必须在建立长连接之前打，否则卡片提交会被 SDK 丢弃
    apply_card_frame_patch()

    handlers = build_handlers()

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handlers.on_message)
        .register_p2_card_action_trigger(handlers.on_card_action)
        # 打开会话就自动弹主菜单。这个事件要在开发者后台单独订阅
        # （事件订阅 -> 添加事件 -> 「用户进入与机器人的会话」），
        # 没订阅的话这里注册了也永远收不到，机器人其余功能不受影响。
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(handlers.on_p2p_chat_entered)
        .build()
    )

    logger.info("正在建立长连接…")
    ws_client = lark.ws.Client(
        settings.app_id,
        settings.app_secret,
        event_handler=event_handler,
        # 必须和 REST 客户端用同一个 domain。不传的话 SDK 默认走 open.feishu.cn，
        # 而 LARK_DOMAIN 指向 Lark（larksuite.com）时 REST 走 Lark、长连接走飞书，
        # 表现是「连上了但一条事件都收不到」，极难看出原因。
        domain=settings.domain,
        log_level=lark.LogLevel.DEBUG
        if settings.log_level.upper() == "DEBUG"
        else lark.LogLevel.INFO,
    )
    ws_client.start()


if __name__ == "__main__":
    main()
