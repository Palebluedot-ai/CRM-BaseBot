"""从邮箱里取最新一份销售明细 xlsx（Microsoft Graph）。

内部系统每天把「OTC组销售明细」xlsx 发到指定邮箱，这是看板唯一的原始数据来源。
这个模块只做「把附件下载到本地磁盘」，不做解析、不进 Base —— 解析和导入在
``scripts/import_daily_board.py`` / ``scripts/import_daily_incremental.py`` 里。

## 为什么自己实现而不是调 SDK

Graph 的 Python SDK 会拖进一大串依赖，而这里只用三个接口：取 token、列邮件、
下载附件。``urllib`` 加一个 dataclass 就够了，也让测试可以完全避开网络。

## 分层：选择逻辑是纯函数

``select_latest_target_message`` / ``select_xlsx_file_attachment`` 只吃 Graph
返回的 JSON 形状（dict/list），不碰网络。测试直接喂假 JSON 断言「选中的是哪封」，
这条路径是「算错钱」的第一道闸门，必须有测试钉住。

## 签名

取邮件用的是**应用权限**（client credentials），不是用户授权：机器人没有人在旁边点
「同意」，也不该拿某个人的账号当长期凭证。所以 .env 里是 client_secret，且这个应用
需要在 Entra 里被授予 ``Mail.Read``（应用权限）并限定到那个邮箱 —— 见
docs/PIPELINE.md。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# 内部系统发件人。换发件人只改这里或 .env 里的 GRAPH_SENDER。
DEFAULT_SENDER = "pro@pro-dev.hashkey.com"

TOKEN_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

# 邮件里的时间是 UTC，人看的是香港时间（和发件的时区一致）。
HONG_KONG = ZoneInfo("Asia/Hong_Kong")

# Settings 里这几个键名，.env 缺任何一个都不该往下走。
GRAPH_ENV_KEYS = (
    "MICROSOFT_GRAPH_TENANT_ID",
    "MICROSOFT_GRAPH_CLIENT_ID",
    "MICROSOFT_GRAPH_CLIENT_SECRET",
    "MICROSOFT_GRAPH_USER_ID",
)


class GraphMailError(RuntimeError):
    """环境变量、Graph 调用或选邮件失败。消息是给人看的，可以直接打印。"""


@dataclass(frozen=True)
class GraphCredentials:
    """一套 Graph 应用凭证。发件人可以覆盖，默认是内部系统的地址。"""

    tenant_id: str
    client_id: str
    client_secret: str
    user_id: str
    sender: str = DEFAULT_SENDER

    @classmethod
    def from_env_values(cls, values: Mapping[str, str]) -> GraphCredentials:
        """从「键 -> 值」的映射构造，缺键时报出**缺的是哪个键**，不回显任何值。"""
        missing = [key for key in GRAPH_ENV_KEYS if not (values.get(key) or "").strip()]
        if missing:
            raise GraphMailError(
                "读邮件需要的 .env 键缺失：" + "、".join(missing) + "（见 .env.example）"
            )
        return cls(
            tenant_id=values["MICROSOFT_GRAPH_TENANT_ID"].strip(),
            client_id=values["MICROSOFT_GRAPH_CLIENT_ID"].strip(),
            client_secret=values["MICROSOFT_GRAPH_CLIENT_SECRET"].strip(),
            user_id=values["MICROSOFT_GRAPH_USER_ID"].strip(),
            sender=(values.get("GRAPH_SENDER") or DEFAULT_SENDER).strip() or DEFAULT_SENDER,
        )


def format_received_hkt(iso_datetime: str) -> str:
    """Graph 回的是 UTC（``...Z``），显示成香港时间给人看。"""
    raw = (iso_datetime or "").strip()
    if not raw:
        return ""
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed.astimezone(HONG_KONG).strftime("%Y-%m-%d %I:%M:%S %p %Z")


def sender_address(message: Mapping[str, object]) -> str:
    from_obj = message.get("from") or {}
    if not isinstance(from_obj, Mapping):
        return ""
    email = from_obj.get("emailAddress") or {}
    if not isinstance(email, Mapping):
        return ""
    return str(email.get("address") or "").strip()


def is_target_sender(message: Mapping[str, object], sender: str = DEFAULT_SENDER) -> bool:
    return sender_address(message).lower() == (sender or DEFAULT_SENDER).lower()


def select_latest_target_message(
    messages: Sequence[Mapping[str, object]], sender: str = DEFAULT_SENDER
) -> dict[str, object]:
    """发件人匹配的邮件里最新的那封。一封都没有就报错，不退回「随便取一封」。"""
    candidates = [dict(item) for item in messages if is_target_sender(item, sender)]
    if not candidates:
        raise GraphMailError(f"邮箱里没有来自 {sender}（AI Smart Analyst）的邮件")
    return max(candidates, key=lambda item: str(item.get("receivedDateTime") or ""))


def is_xlsx_file_attachment(attachment: Mapping[str, object]) -> bool:
    """只认「非内联的 .xlsx 文件附件」。

    内联附件是签名里的小图，item 附件是转发的邮件本身 —— 两者都可能带 .xlsx 字样的
    名字，但都不是数据源。
    """
    name = str(attachment.get("name") or "").lower()
    if not name.endswith(".xlsx"):
        return False
    odata_type = str(attachment.get("@odata.type") or "").lower()
    if odata_type and "fileattachment" not in odata_type:
        return False
    if bool(attachment.get("isInline")):
        return False
    return True


def select_xlsx_file_attachment(
    attachments: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """唯一那个 xlsx 附件。

    有两个就报错而不是挑一个：挑错文件等于把别的数据导进看板，而这事在导入前
    没有任何提示。宁可停下让人看一眼。
    """
    matches = [dict(item) for item in attachments if is_xlsx_file_attachment(item)]
    if len(matches) == 0:
        raise GraphMailError("这封邮件没有 .xlsx 文件附件")
    if len(matches) != 1:
        names = "、".join(str(item.get("name") or "") for item in matches)
        raise GraphMailError(f"这封邮件有 {len(matches)} 个 .xlsx 附件，不知道该用哪个：{names}")
    return matches[0]


def select_latest_xlsx(
    messages: Sequence[Mapping[str, object]],
    attachments_by_message_id: Mapping[str, Sequence[Mapping[str, object]]],
    sender: str = DEFAULT_SENDER,
) -> tuple[dict[str, object], dict[str, object]]:
    """最新那封发件人匹配的邮件 + 它的 xlsx 附件。

    最新的那封没有 xlsx 就报错，**不往更早的邮件退**：退回去等于每天用旧文件覆盖
    看板，而且看不出来。
    """
    message = select_latest_target_message(messages, sender)
    message_id = str(message.get("id") or "")
    attachments = attachments_by_message_id.get(message_id, [])
    try:
        attachment = select_xlsx_file_attachment(attachments)
    except GraphMailError as exc:
        raise GraphMailError(
            f"最新一封来自 {sender} 的邮件（id={message_id}，"
            f"收于 {message.get('receivedDateTime')}）取不到附件：{exc}"
        ) from exc
    return message, attachment


@dataclass(slots=True)
class GraphClient:
    """三个接口的薄封装：取 token、列邮件/附件、下载附件字节。"""

    credentials: GraphCredentials
    timeout_seconds: int = 60

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        raw: bool = False,
    ) -> object:
        request = Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except HTTPError as exc:
            # 截断响应体：Graph 的错误体可能很长，而它前面就是真正的原因。
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise GraphMailError(f"HTTP {exc.code} 请求 {method} {url} 失败：{detail}") from exc
        except URLError as exc:
            raise GraphMailError(f"网络错误 请求 {method} {url} 失败：{exc.reason}") from exc
        if raw:
            return body
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def get_access_token(self) -> str:
        url = f"https://login.microsoftonline.com/{self.credentials.tenant_id}/oauth2/v2.0/token"
        payload = urlencode(
            {
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
                "scope": TOKEN_SCOPE,
                "grant_type": "client_credentials",
            }
        ).encode("utf-8")
        body = self._request(
            url,
            method="POST",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not isinstance(body, dict) or not body.get("access_token"):
            raise GraphMailError("取 token 的响应里没有 access_token")
        return str(body["access_token"])

    def _user_root(self) -> str:
        return f"{GRAPH_ROOT}/users/{quote(self.credentials.user_id)}"

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "ConsistencyLevel": "eventual",
        }

    def _get_pages(self, url: str, token: str, *, max_pages: int = 20) -> list[dict[str, object]]:
        """跟着 ``@odata.nextLink`` 翻页，最多 ``max_pages`` 页。

        带上限是有意的：真出现「nextLink 一直有」的异常响应时，没上限的循环会一直
        调接口，把一个可诊断的失败变成一次额度事故。
        """
        items: list[dict[str, object]] = []
        next_url: str | None = url
        pages = 0
        while next_url and pages < max_pages:
            payload = self._request(next_url, headers=self._auth_headers(token))
            if not isinstance(payload, dict):
                raise GraphMailError("Graph 返回了预期之外的结构")
            for raw in payload.get("value") or []:
                if isinstance(raw, dict):
                    items.append(raw)
            next_link = payload.get("@odata.nextLink")
            next_url = str(next_link) if next_link else None
            pages += 1
        return items

    def list_target_messages(self, token: str) -> list[dict[str, object]]:
        """列出该发件人的邮件。

        三条路依次退：``$search``（对 exchange 最有效）→ ``$filter`` → 扫收件箱。
        后两条是兜底：某些租户禁用了 ``$search``，某些不许 ``$count``，而
        「今天没导数据」和「查询语法不被支持」是两件事，不该长得一样。
        """
        sender = self.credentials.sender
        select = "id,subject,from,receivedDateTime,hasAttachments"

        search_query = urlencode({"$search": f'"from:{sender}"', "$select": select, "$top": "50"})
        found = [
            item
            for item in self._get_pages(f"{self._user_root()}/messages?{search_query}", token)
            if is_target_sender(item, sender)
        ]
        if found:
            return found

        filter_query = urlencode(
            {
                "$filter": f"from/emailAddress/address eq '{sender}'",
                "$select": select,
                "$top": "50",
                "$count": "true",
            }
        )
        try:
            found = [
                item
                for item in self._get_pages(f"{self._user_root()}/messages?{filter_query}", token)
                if is_target_sender(item, sender)
            ]
        except GraphMailError:
            found = []
        if found:
            return found

        inbox_query = urlencode(
            {"$select": select, "$orderby": "receivedDateTime desc", "$top": "50"}
        )
        scanned = self._get_pages(
            f"{self._user_root()}/mailFolders/inbox/messages?{inbox_query}", token, max_pages=20
        )
        return [item for item in scanned if is_target_sender(item, sender)]

    def list_attachments(self, token: str, message_id: str) -> list[dict[str, object]]:
        url = (
            f"{self._user_root()}/messages/{quote(message_id)}/attachments"
            "?$select=id,name,contentType,size,isInline"
        )
        return self._get_pages(url, token, max_pages=5)

    def download_attachment_bytes(self, token: str, message_id: str, attachment_id: str) -> bytes:
        url = (
            f"{self._user_root()}/messages/{quote(message_id)}"
            f"/attachments/{quote(attachment_id)}/$value"
        )
        body = self._request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
            raw=True,
        )
        if not isinstance(body, (bytes, bytearray)):
            raise GraphMailError("附件下载接口没有返回字节流")
        return bytes(body)


def write_xlsx(path: Path, data: bytes) -> None:
    """落盘。空内容拒绝写 —— 覆盖掉昨天的好文件之后没法恢复。"""
    if not data:
        raise GraphMailError("附件是空的，拒绝写入（避免覆盖掉已有文件）")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def fetch_latest_xlsx(
    *,
    credentials: GraphCredentials,
    out_dir: Path,
    timeout_seconds: int = 60,
) -> dict[str, object]:
    """下载最新一份 xlsx 到 ``out_dir``，返回一份可打印的结果摘要。"""
    client = GraphClient(credentials, timeout_seconds=timeout_seconds)
    token = client.get_access_token()

    # 只认最新那封：取它的附件，然后选 xlsx。附件列表按 message_id 现取，
    # 不预先扫所有邮件 —— 那会多出 N 次请求，而只有最新的那封会被用到。
    latest = select_latest_target_message(client.list_target_messages(token), credentials.sender)
    message_id = str(latest.get("id") or "")
    attachments = client.list_attachments(token, message_id)
    message, attachment = select_latest_xlsx(
        [latest], {message_id: attachments}, credentials.sender
    )

    attachment_id = str(attachment.get("id") or "")
    safe_name = Path(str(attachment.get("name") or "attachment.xlsx")).name or "attachment.xlsx"
    dest = out_dir / safe_name
    write_xlsx(dest, client.download_attachment_bytes(token, message_id, attachment_id))

    received = str(message.get("receivedDateTime") or "")
    return {
        "sender": sender_address(message).lower(),
        "receivedDateTime": received,
        "receivedDateTimeHKT": format_received_hkt(received),
        "attachmentName": safe_name,
        "savePath": str(dest.resolve()),
        "messageId": message_id,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：``--out-dir`` 指定落盘目录，``--meta`` 把摘要另存一份 JSON。"""
    args = list(sys.argv[1:] if argv is None else argv)
    out_dir = Path("attachments")
    meta_path: Path | None = None
    i = 0
    while i < len(args):
        if args[i] == "--out-dir" and i + 1 < len(args):
            out_dir = Path(args[i + 1])
            i += 2
        elif args[i] == "--meta" and i + 1 < len(args):
            meta_path = Path(args[i + 1])
            i += 2
        else:
            print(f"不认识的参数：{args[i]}", file=sys.stderr)
            return 2

    try:
        from ..config import get_settings

        settings = get_settings()
        credentials = GraphCredentials.from_env_values(_env_values(settings))
        result = fetch_latest_xlsx(credentials=credentials, out_dir=out_dir)
    except GraphMailError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if meta_path is not None:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(text + "\n", encoding="utf-8")
    return 0


def _env_values(settings) -> dict[str, str]:
    """把 Settings 上的 Graph 字段还原成「键 -> 值」，键名和 .env 一致。"""
    return {
        "MICROSOFT_GRAPH_TENANT_ID": settings.ms_tenant_id,
        "MICROSOFT_GRAPH_CLIENT_ID": settings.ms_client_id,
        "MICROSOFT_GRAPH_CLIENT_SECRET": settings.ms_client_secret,
        "MICROSOFT_GRAPH_USER_ID": settings.ms_user_id,
        "GRAPH_SENDER": settings.graph_sender,
    }


if __name__ == "__main__":
    raise SystemExit(main())
