"""取到那份要导入的 xlsx —— 唯一碰「外部输入」的地方。

两条路，优先邮箱：

    邮箱   graph.mail → 下载最新的 .xlsx 附件到本地目录
    本地   在目录里挑最新的一份（按**文件名里的日期**，不是 mtime）

为什么按文件名里的日期而不是 mtime：附件被重新下载、目录被 rsync 之后 mtime 会变，
而文件名里的日期是导出自己的日期。

这个模块只负责「把文件弄到本地并返回路径」，不解析、不写 Base。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..graph.mail import GraphCredentials, GraphMailError, fetch_latest_xlsx

# 文件名里带日期（OTC组销售明细_2026-09-18.xlsx），按它挑最新一份。
_DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")

# 导出文件名的默认匹配。导出工具换名字时用 --pattern 覆盖。
DEFAULT_PATTERN = "OTC组销售明细_*.xlsx"

# Settings 上的 Graph 字段 -> .env 键名，交给 GraphCredentials 校验。
_GRAPH_SETTING_KEYS = {
    "MICROSOFT_GRAPH_TENANT_ID": "ms_tenant_id",
    "MICROSOFT_GRAPH_CLIENT_ID": "ms_client_id",
    "MICROSOFT_GRAPH_CLIENT_SECRET": "ms_client_secret",
    "MICROSOFT_GRAPH_USER_ID": "ms_user_id",
    "GRAPH_SENDER": "graph_sender",
}


class SourceError(RuntimeError):
    """取不到文件。消息是给人看的，可以直接打印。"""


def pick_latest_export(directory: Path, pattern: str = DEFAULT_PATTERN) -> Path | None:
    """目录里最新的一份导出；没有返回 None。

    按文件名里的日期排，文件名没日期时才退回 mtime。
    """

    def key(path: Path) -> tuple[str, float]:
        found = _DATE_IN_NAME.search(path.name)
        return (found.group(1) if found else "", path.stat().st_mtime)

    candidates = [p for p in directory.glob(pattern) if p.is_file()]
    return max(candidates, key=key) if candidates else None


def credentials_from_settings(settings) -> GraphCredentials:
    """从 Settings 取出 Graph 凭证。缺键时抛出人话错误（不回显任何值）。"""
    return GraphCredentials.from_env_values(
        {
            key: getattr(settings, attribute, "") or ""
            for key, attribute in _GRAPH_SETTING_KEYS.items()
        }
    )


def fetch_from_mail(settings, out_dir: Path) -> Path:
    """去邮箱下载最新附件，返回落盘路径。"""
    try:
        result = fetch_latest_xlsx(
            credentials=credentials_from_settings(settings),
            out_dir=out_dir,
        )
    except GraphMailError as exc:
        raise SourceError(f"从邮箱取附件失败：{exc}") from exc
    return Path(str(result["savePath"]))
