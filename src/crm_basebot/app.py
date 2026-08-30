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
from .config import get_settings
from .domain.audit import AuditLog
from .domain.referral import ReferralService
from .domain.referred_client import ReferredClientService
from .lark.bitable import BitableClient
from .lark.client import get_client
from .lark.ws_patch import apply_card_frame_patch

logger = logging.getLogger(__name__)


def _require_tables(settings) -> None:
    missing = [
        name
        for name, value in {
            "LARK_BASE_APP_TOKEN": settings.base_app_token,
            "TABLE_REFERRAL": settings.table_referral,
            "TABLE_CLIENT": settings.table_client,
            "TABLE_AUDIT": settings.table_audit,
            "TABLE_SALES": settings.table_sales,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(
            "以下环境变量还没填：\n  "
            + "\n  ".join(missing)
            + "\n\n先跑 `uv run python scripts/inspect_base.py` 拿到各表的 table_id。"
        )


def build_handlers() -> BotHandlers:
    settings = get_settings()
    _require_tables(settings)

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
    settings = get_settings()
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
