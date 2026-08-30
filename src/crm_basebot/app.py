"""组装并运行机器人。

    uv run python -m crm_basebot.app

用长连接接收事件，只需要服务器能出网，不需要公网入口 —— 这既是本地开发的便利，
也是内网部署唯一可行的方式。
"""

from __future__ import annotations

import logging

import lark_oapi as lark

from .bot.auth import SalesDirectory
from .bot.handlers import BotHandlers
from .domain.audit import AuditLog
from .domain.referral import ReferralService
from .domain.referred_client import ReferredClientService
from .lark.bitable import BitableClient
from .lark.client import get_client
from .lark.ws_patch import apply_card_frame_patch
from .startup import load_settings, require_settings

logger = logging.getLogger(__name__)

# 机器人跑起来至少要能读写这几张表：鉴权查名册、登记写渠道和客户、每一次写都记审计。
# 交易明细和佣金汇总只有对账任务用得着，不在这里拦。
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

    return BotHandlers(
        client=get_client(),
        directory=SalesDirectory(bitable, settings.table_sales),
        referrals=ReferralService(
            bitable,
            settings.table_referral,
            audit,
            auto_number=settings.referral_auto_number,
        ),
        clients=ReferredClientService(
            bitable, settings.table_client, settings.table_referral, audit
        ),
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
