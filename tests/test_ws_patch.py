"""卡片回调补丁的回归测试。

这里同时承担一个提醒职责：如果哪天 lark-oapi 官方修好了 CARD 帧分发，
``test_补丁在sdk修复后自动跳过`` 会变成 skip 并打印提示，那时就可以删掉
ws_patch.py 和本文件。
"""

import asyncio

import pytest
from lark_oapi.ws.client import Client
from lark_oapi.ws.const import HEADER_TYPE
from lark_oapi.ws.enum import MessageType

from crm_basebot.lark import ws_patch


@pytest.fixture(autouse=True)
def _clean_patch_state():
    ws_patch.revert_card_frame_patch()
    yield
    ws_patch.revert_card_frame_patch()


class FakeHeader:
    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value


class FakeFrame:
    """只提供补丁会碰到的部分：headers 列表。"""

    def __init__(self, message_type: str) -> None:
        self.headers = [
            FakeHeader("message_id", "msg-1"),
            FakeHeader(HEADER_TYPE, message_type),
            FakeHeader("trace_id", "trace-1"),
        ]

    def header_value(self, key: str) -> str | None:
        for header in self.headers:
            if header.key == key:
                return header.value
        return None


def test_确认sdk仍然丢弃card帧():
    """前提检查：补丁存在的理由还成立吗。

    失败说明 SDK 变了 —— 去看 https://github.com/larksuite/oapi-sdk-python/issues/126
    """
    if not ws_patch.sdk_drops_card_frames():
        pytest.skip("lark-oapi 已修复 CARD 帧分发，可以删掉 ws_patch.py 了")
    assert ws_patch.sdk_drops_card_frames() is True


def test_打补丁后card帧被改写成event帧():
    if not ws_patch.sdk_drops_card_frames():
        pytest.skip("SDK 已修复，补丁不再适用")

    seen: list[str] = []

    async def fake_original(self, frame):
        seen.append(frame.header_value(HEADER_TYPE))

    original = Client._handle_data_frame
    Client._handle_data_frame = fake_original
    try:
        # 直接安装包装层，绕过 sdk_drops_card_frames 对假方法的源码检查
        ws_patch.sdk_drops_card_frames = lambda method=None: True  # type: ignore[assignment]
        assert ws_patch.apply_card_frame_patch() is True

        frame = FakeFrame(MessageType.CARD.value)
        asyncio.run(Client._handle_data_frame(None, frame))
    finally:
        ws_patch.revert_card_frame_patch()
        Client._handle_data_frame = original
        ws_patch.sdk_drops_card_frames = _real_detector  # type: ignore[assignment]

    assert seen == [MessageType.EVENT.value], (
        "CARD 帧没被改写成 EVENT，卡片提交仍会被 SDK 丢弃"
    )


def test_event帧不受影响():
    seen: list[str] = []

    async def fake_original(self, frame):
        seen.append(frame.header_value(HEADER_TYPE))

    original = Client._handle_data_frame
    Client._handle_data_frame = fake_original
    try:
        ws_patch.sdk_drops_card_frames = lambda method=None: True  # type: ignore[assignment]
        ws_patch.apply_card_frame_patch()

        frame = FakeFrame(MessageType.EVENT.value)
        asyncio.run(Client._handle_data_frame(None, frame))
    finally:
        ws_patch.revert_card_frame_patch()
        Client._handle_data_frame = original
        ws_patch.sdk_drops_card_frames = _real_detector  # type: ignore[assignment]

    assert seen == [MessageType.EVENT.value]


def test_重复打补丁是幂等的():
    if not ws_patch.sdk_drops_card_frames():
        pytest.skip("SDK 已修复，补丁不再适用")

    assert ws_patch.apply_card_frame_patch() is True
    first = Client._handle_data_frame
    assert ws_patch.apply_card_frame_patch() is True
    assert Client._handle_data_frame is first


def test_撤销补丁能还原原方法():
    if not ws_patch.sdk_drops_card_frames():
        pytest.skip("SDK 已修复，补丁不再适用")

    before = Client._handle_data_frame
    ws_patch.apply_card_frame_patch()
    assert Client._handle_data_frame is not before
    ws_patch.revert_card_frame_patch()
    assert Client._handle_data_frame is before


_real_detector = ws_patch.sdk_drops_card_frames
