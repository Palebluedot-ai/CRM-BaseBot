"""docx 的两件共用小事：找范本、把文件名里不能用的字符去掉。"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def template_path(name: str) -> Path:
    path = TEMPLATE_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"找不到范本 {path}")
    return path


def safe_filename(text: str, *, fallback: str = "Referrer") -> str:
    """Windows / macOS 文件名里不能出现的字符去掉；去完是空的就用 ``fallback``。"""
    cleaned = _UNSAFE.sub("", str(text or "")).strip()
    return cleaned or fallback
