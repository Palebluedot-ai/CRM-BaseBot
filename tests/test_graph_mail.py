"""邮件抓取里的「选哪封、选哪个附件」。

这是最靠前的一道闸门：选错邮件或选错附件 = 把别的数据往看板里导，而在导入之前
没有任何提示。所以这段逻辑全是纯函数，测试直接喂 Graph 形状的假 JSON，断言选中
的是哪一个。
"""

from __future__ import annotations

import pytest

from crm_basebot.graph.mail import (
    DEFAULT_SENDER,
    GraphCredentials,
    GraphMailError,
    format_received_hkt,
    is_target_sender,
    is_xlsx_file_attachment,
    select_latest_target_message,
    select_latest_xlsx,
    select_xlsx_file_attachment,
)


def _message(
    message_id: str,
    *,
    sender: str = DEFAULT_SENDER,
    received: str = "2026-09-18T02:02:00Z",
    subject: str = "OTC组销售明细",
) -> dict:
    return {
        "id": message_id,
        "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": received,
        "hasAttachments": True,
    }


def _xlsx(name: str = "OTC组销售明细_2026-09-18.xlsx", attachment_id: str = "att1") -> dict:
    return {
        "id": attachment_id,
        "name": name,
        "@odata.type": "#microsoft.graph.fileAttachment",
        "isInline": False,
    }


# ---------- 选邮件 ----------


def test_发件人匹配不看大小写():
    assert is_target_sender(_message("m1", sender=DEFAULT_SENDER.upper())) is True


def test_别的发件人不算():
    assert is_target_sender(_message("m1", sender="someone@example.com")) is False


def test_缺发件人字段不算():
    assert is_target_sender({"id": "m1"}) is False


def test_最新的一封目标邮件获胜():
    messages = [
        _message("old", received="2026-09-16T02:00:00Z"),
        _message("noise", sender="other@example.com", received="2026-09-19T02:00:00Z"),
        _message("new", received="2026-09-18T02:00:00Z"),
    ]
    assert select_latest_target_message(messages)["id"] == "new"


def test_一封目标邮件都没有就报错():
    with pytest.raises(GraphMailError) as err:
        select_latest_target_message([_message("m1", sender="other@example.com")])
    assert DEFAULT_SENDER in str(err.value)


def test_发件人可以覆盖():
    messages = [_message("m1", sender="analyst@example.com")]
    assert select_latest_target_message(messages, "analyst@example.com")["id"] == "m1"


# ---------- 选附件 ----------


def test_只认非内联的xlsx文件附件():
    assert is_xlsx_file_attachment(_xlsx()) is True
    assert is_xlsx_file_attachment(_xlsx(name="签名.png")) is False
    assert is_xlsx_file_attachment(_xlsx(name="报表.xls")) is False
    assert is_xlsx_file_attachment({**_xlsx(), "isInline": True}) is False
    # 转发的邮件本身（item 附件）名字里也可能带 .xlsx，但它不是数据源
    assert (
        is_xlsx_file_attachment({**_xlsx(), "@odata.type": "#microsoft.graph.itemAttachment"})
        is False
    )


def test_唯一那个xlsx就是它():
    assert select_xlsx_file_attachment([_xlsx("a.xlsx")])["name"] == "a.xlsx"


def test_没有xlsx附件时报错():
    with pytest.raises(GraphMailError):
        select_xlsx_file_attachment([_xlsx(name="图.png")])


def test_两个xlsx就报错并把名字列出来():
    """挑一个「更对的」等于赌运气：挑错就把别的数据导进看板了。"""
    with pytest.raises(GraphMailError) as err:
        select_xlsx_file_attachment([_xlsx("a.xlsx", "a1"), _xlsx("b.xlsx", "b1")])
    assert "a.xlsx" in str(err.value)
    assert "b.xlsx" in str(err.value)


def test_最新邮件没有附件时不退回更早的邮件():
    """退回旧邮件 = 每天拿昨天的文件覆盖看板，而且看不出来。"""
    messages = [
        _message("new", received="2026-09-18T02:00:00Z"),
        _message("old", received="2026-09-17T02:00:00Z"),
    ]
    with pytest.raises(GraphMailError) as err:
        select_latest_xlsx(messages, {"new": [], "old": [_xlsx()]})
    assert "new" in str(err.value)


def test_最新邮件带附件时取它的():
    messages = [
        _message("new", received="2026-09-18T02:00:00Z"),
        _message("old", received="2026-09-17T02:00:00Z"),
    ]
    message, attachment = select_latest_xlsx(
        messages,
        {"new": [_xlsx("latest.xlsx", "a2")], "old": [_xlsx("yesterday.xlsx", "a1")]},
    )
    assert message["id"] == "new"
    assert attachment["name"] == "latest.xlsx"


# ---------- 时区与凭证 ----------


def test_UTC时间显示成香港时间():
    # 02:02 UTC = 10:02 香港
    assert format_received_hkt("2026-09-18T02:02:00Z").startswith("2026-09-18 10:02:00 AM")


def test_空时间不炸():
    assert format_received_hkt("") == ""


def test_缺键时报出缺哪个键且不回显任何值():
    with pytest.raises(GraphMailError) as err:
        GraphCredentials.from_env_values({"MICROSOFT_GRAPH_CLIENT_ID": "cid"})
    message = str(err.value)
    assert "MICROSOFT_GRAPH_TENANT_ID" in message
    assert "cid" not in message  # 已经给的值不该出现在报错里


def test_四个键齐了就构造得出来且发件人回默认():
    credentials = GraphCredentials.from_env_values(
        {
            "MICROSOFT_GRAPH_TENANT_ID": "t",
            "MICROSOFT_GRAPH_CLIENT_ID": "c",
            "MICROSOFT_GRAPH_CLIENT_SECRET": "s",
            "MICROSOFT_GRAPH_USER_ID": "u",
            "GRAPH_SENDER": "  ",
        }
    )
    assert credentials.tenant_id == "t"
    assert credentials.user_id == "u"
    # 发件人留空/纯空格都回默认值，而不是变成空字符串去匹配所有邮件
    assert credentials.sender == DEFAULT_SENDER


def test_发件人写了就用写的那个():
    credentials = GraphCredentials.from_env_values(
        {
            "MICROSOFT_GRAPH_TENANT_ID": "t",
            "MICROSOFT_GRAPH_CLIENT_ID": "c",
            "MICROSOFT_GRAPH_CLIENT_SECRET": "s",
            "MICROSOFT_GRAPH_USER_ID": "u",
            "GRAPH_SENDER": "analyst@example.com",
        }
    )
    assert credentials.sender == "analyst@example.com"
