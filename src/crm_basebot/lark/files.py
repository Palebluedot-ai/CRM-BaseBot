"""把一个文件（协议、invoice）发到某人和机器人的单聊里。

两步：先上传拿 ``file_key``（``im.v1.file.create``），再发一条 ``msg_type=file`` 的消息。

**上传要应用开「上传图片或文件资源」权限（im:resource）。** 没开的时候平台回 99991672
一类的「无权限」错误 —— 这里把它翻成一句人话，机器人照原样推给点按钮的人，管理员按它
去开发者后台开权限、发版本。见 docs/LARK_APP_SETUP.md。
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import PurePath

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
)

logger = logging.getLogger(__name__)

# 飞书单个文件上限 30MB。协议、invoice 都是几十 KB，一个月的 invoice 打包也就几 MB。
MAX_FILE_BYTES = 30 * 1024 * 1024

_FILE_TYPES = {".pdf": "pdf", ".doc": "doc", ".docx": "doc", ".xls": "xls", ".xlsx": "xls"}


class FileSendError(RuntimeError):
    """文件没发出去。消息直接给人看。"""


def file_type_of(filename: str) -> str:
    """飞书上传接口要的 file_type。不认识的扩展名一律 ``stream``（zip 就是这种）。"""
    return _FILE_TYPES.get(PurePath(filename).suffix.lower(), "stream")


class FileSender:
    def __init__(self, client: lark.Client) -> None:
        self._client = client

    def send(self, open_id: str, filename: str, data: bytes) -> None:
        if len(data) > MAX_FILE_BYTES:
            raise FileSendError(f"{filename} 太大（{len(data) // 1024 // 1024}MB），飞书上限 30MB")

        upload = self._client.im.v1.file.create(
            CreateFileRequest.builder()
            .request_body(
                CreateFileRequestBody.builder()
                .file_type(file_type_of(filename))
                .file_name(filename)
                .file(io.BytesIO(data))
                .build()
            )
            .build()
        )
        if not upload.success() or not upload.data or not upload.data.file_key:
            logger.error("上传文件失败 %s: %s %s", filename, upload.code, upload.msg)
            raise FileSendError(
                f"文件生成好了但上传不上飞书（错误码 {upload.code}）。多半是机器人还没开"
                "「上传图片或文件资源」权限，请管理员照 docs/LARK_APP_SETUP.md 开一下。"
            )

        sent = self._client.im.v1.message.create(
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("file")
                .content(json.dumps({"file_key": upload.data.file_key}))
                .build()
            )
            .build()
        )
        if not sent.success():
            logger.error("发送文件消息失败 %s: %s %s", filename, sent.code, sent.msg)
            raise FileSendError(f"{filename} 上传了但没发出来（错误码 {sent.code}），请稍后再试。")
