"""修复 lark-oapi 长连接模式下卡片回调被丢弃的问题。

lark-oapi 的 WebSocket 客户端在 ``Client._handle_data_frame`` 里对 CARD 帧直接
``return``，既不分发给已注册的 handler，也不回写响应帧。用户点卡片按钮后客户端
等 3 秒超时，报 ``200340 出错了，请稍后重试``，服务端一行日志都没有。

见 https://github.com/larksuite/oapi-sdk-python/issues/126 —— issue 已关闭但截至
lark-oapi 1.7.3 仍未修复。

为什么必须解决而不是绕开：公司内网服务器没有公网入口，飞书 webhook 打不进来，
长连接（只需出网）是生产环境唯一可行的订阅方式。

补丁做法：把 CARD 帧的 ``type`` 头改写成 ``event`` 再交给原方法。SDK 的分发器
``_do_without_validation`` 本来就是靠解析 payload 里的 ``header.event_type``
来路由的（card 回调解析出 ``p2.card.action.trigger``），跟帧头的 type 无关。
所以改写帧头等价于让 CARD 走通 EVENT 那条正确的分支，而合包、响应帧回写、异常
处理全部保持 SDK 原样，不复制任何内部实现。

代价：回写给平台的响应帧里 ``type`` 变成了 ``event``。平台按 ``message_id``
关联请求与响应，这个字段是回显的，实测不影响交互（见 scripts/ws_smoke.py）。
"""

from __future__ import annotations

import functools
import inspect
import logging

from lark_oapi.ws.client import Client
from lark_oapi.ws.const import HEADER_TYPE
from lark_oapi.ws.enum import MessageType

logger = logging.getLogger(__name__)

_PATCH_FLAG = "_crm_basebot_card_frame_patched"

_BUG_SIGNATURE = "elif message_type == MessageType.CARD:"


def sdk_drops_card_frames(method=None) -> bool:
    """安装的 SDK 是否仍然丢弃 CARD 帧。

    读源码判断，因为构造真实帧来探测需要一条活的 WebSocket 连接。
    读不到源码时保守地认为有 bug —— 补丁是幂等且无害的。
    """
    if method is None:
        method = _original_handle_data_frame() or Client._handle_data_frame

    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return True

    start = source.find(_BUG_SIGNATURE)
    if start == -1:
        return False

    branch_body = source[start + len(_BUG_SIGNATURE) :].lstrip()
    return branch_body.startswith("return")


def _original_handle_data_frame():
    """拿到未打补丁的原方法，没打过补丁时返回 None。"""
    return getattr(Client, _PATCH_FLAG, None)


def apply_card_frame_patch() -> bool:
    """让长连接能收到卡片回调。返回是否真的打了补丁。

    幂等。SDK 官方修复后会自动跳过并打日志，那时就可以删掉本模块。
    """
    if _original_handle_data_frame() is not None:
        return True

    original = Client._handle_data_frame

    if not sdk_drops_card_frames(original):
        logger.info("lark-oapi 已自行修复 CARD 帧分发，跳过补丁 —— 可以删掉 ws_patch.py 了")
        return False

    @functools.wraps(original)
    async def _handle_data_frame_with_card_support(self, frame):
        for header in frame.headers:
            if header.key == HEADER_TYPE and header.value == MessageType.CARD.value:
                header.value = MessageType.EVENT.value
                logger.debug("CARD 帧改写为 EVENT 帧以绕过 SDK issue #126")
                break
        return await original(self, frame)

    Client._handle_data_frame = _handle_data_frame_with_card_support
    setattr(Client, _PATCH_FLAG, original)
    logger.info("已给 lark-oapi 长连接客户端打上 CARD 帧补丁（SDK issue #126）")
    return True


def revert_card_frame_patch() -> None:
    """撤销补丁，仅供测试用。"""
    original = _original_handle_data_frame()
    if original is not None:
        Client._handle_data_frame = original
        delattr(Client, _PATCH_FLAG)
