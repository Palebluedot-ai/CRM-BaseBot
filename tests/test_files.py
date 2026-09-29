"""发文件：先上传拿 file_key，再发 file 消息；没权限时说人话。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from crm_basebot.lark.files import FileSender, FileSendError, file_type_of


class _Resp:
    def __init__(self, ok=True, code=0, file_key="file_v3_abc"):
        self._ok, self.code, self.msg = ok, code, "x"
        self.data = SimpleNamespace(file_key=file_key) if ok else None

    def success(self):
        return self._ok


class _Client:
    def __init__(self, upload_ok=True, send_ok=True):
        self.uploads, self.messages = [], []
        self.upload_ok, self.send_ok = upload_ok, send_ok
        self.im = SimpleNamespace(
            v1=SimpleNamespace(
                file=SimpleNamespace(create=self._upload),
                message=SimpleNamespace(create=self._send),
            )
        )

    def _upload(self, request):
        self.uploads.append(request)
        return _Resp(ok=self.upload_ok, code=0 if self.upload_ok else 99991672)

    def _send(self, request):
        self.messages.append(request)
        return _Resp(ok=self.send_ok, code=0 if self.send_ok else 230001)


@pytest.mark.parametrize(
    ("name", "kind"),
    [("a.pdf", "pdf"), ("A.DOCX", "doc"), ("x.zip", "stream"), ("noext", "stream")],
)
def test_文件类型(name, kind):
    assert file_type_of(name) == kind


def test_先上传再发file消息():
    client = _Client()
    FileSender(client).send("ou_x", "HTS Referral Agreement - A.docx", b"PK..")
    body = client.uploads[0].request_body
    assert (body.file_type, body.file_name) == ("doc", "HTS Referral Agreement - A.docx")
    message = client.messages[0]
    assert message.receive_id_type == "open_id"
    assert message.request_body.msg_type == "file"
    assert json.loads(message.request_body.content) == {"file_key": "file_v3_abc"}


def test_上传不了就说是权限():
    with pytest.raises(FileSendError, match="上传图片或文件资源"):
        FileSender(_Client(upload_ok=False)).send("ou_x", "a.pdf", b"%PDF")


def test_发消息失败也报出来():
    with pytest.raises(FileSendError, match="没发出来"):
        FileSender(_Client(send_ok=False)).send("ou_x", "a.pdf", b"%PDF")
