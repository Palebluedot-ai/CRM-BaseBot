#!/usr/bin/env python
"""长连接连通性 + 卡片回调补丁的实测。

这是整个项目最关键的一次人工验证。它不依赖 Base，也不依赖任何表，
只验证两件事：

1. App ID / Secret 对不对，长连接能不能建起来
2. 点卡片按钮时，回调能不能真的到达我们的代码

第 2 点就是 SDK issue #126 的现场检验。补丁没生效的话，你会在飞书客户端看到
`200340 出错了，请稍后重试`，而这里一行日志都不会打。

    uv run python scripts/ws_smoke.py

然后在飞书里给机器人发任意一条消息，点它回复的卡片上的按钮。

2026-08-31 在 lark-oapi 1.7.3 上跑通过一次（自建免费团队租户，长连接模式）：
点按钮正常弹 toast。所以现在再跑它是回归检查 —— 升级 SDK 或换租户之后跑一遍，
确认补丁在新版本上依然成立。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import lark_oapi as lark  # noqa: E402
from lark_oapi.api.im.v1 import (  # noqa: E402
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: E402
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.lark.ws_patch import (  # noqa: E402
    apply_card_frame_patch,
    sdk_drops_card_frames,
)
from crm_basebot.startup import load_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
logger = logging.getLogger("ws_smoke")

SMOKE_CARD = {
    "schema": "2.0",
    "header": {
        "title": {"tag": "plain_text", "content": "连通性自检"},
        "template": "blue",
    },
    "body": {
        "elements": [
            {
                "tag": "markdown",
                "content": "点下面的按钮。**收到 toast 提示**就说明卡片回调打通了。",
            },
            {
                "tag": "form",
                "name": "smoke_form",
                "elements": [
                    {
                        "tag": "input",
                        "name": "note",
                        "label": {"tag": "plain_text", "content": "随便写点什么"},
                        "placeholder": {"tag": "plain_text", "content": "hello"},
                        "required": False,
                    },
                    {
                        "tag": "button",
                        "name": "smoke_submit",
                        "text": {"tag": "plain_text", "content": "提交测试"},
                        "type": "primary",
                        # JSON 2.0 里表单内按钮用 form_action_type，不是 1.0 的 action_type
                        "form_action_type": "submit",
                        "behaviors": [{"type": "callback", "value": {"action": "smoke_test"}}],
                    },
                ],
            },
        ]
    },
}


def on_message(data: P2ImMessageReceiveV1) -> None:
    chat_id = data.event.message.chat_id
    sender = data.event.sender.sender_id.open_id
    logger.info("收到消息 chat_id=%s open_id=%s", chat_id, sender)

    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(SMOKE_CARD, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = get_client().im.v1.message.create(request)
    if response.success():
        logger.info("已回复自检卡片，请点击卡片上的按钮")
    else:
        logger.error("发送卡片失败: code=%s msg=%s", response.code, response.msg)


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    operator = data.event.operator
    form = data.event.action.form_value or {}

    logger.info("=" * 56)
    logger.info("卡片回调到达了 —— CARD 帧补丁生效")
    logger.info("  open_id  = %s", operator.open_id)
    logger.info("  form     = %s", form)
    logger.info("=" * 56)

    return P2CardActionTriggerResponse(
        {
            "toast": {
                "type": "success",
                "content": "回调收到了，长连接和补丁都正常",
            }
        }
    )


def main() -> int:
    settings = load_settings()

    if sdk_drops_card_frames():
        logger.info("检测到 SDK 仍会丢弃 CARD 帧，正在打补丁")
    else:
        logger.info("SDK 已自行修复 CARD 帧分发，无需补丁")
    apply_card_frame_patch()

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )

    logger.info("正在连接飞书开放平台…")
    logger.info("连上之后：在飞书里给机器人发条消息，然后点卡片按钮")
    logger.info("（开发者后台保存「使用长连接接收事件」时，这个进程必须在跑）")

    lark.ws.Client(
        settings.app_id,
        settings.app_secret,
        event_handler=event_handler,
        # 和 REST 客户端保持同一个 domain，见 app.py 里的说明
        domain=settings.domain,
        log_level=lark.LogLevel.INFO,
    ).start()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
