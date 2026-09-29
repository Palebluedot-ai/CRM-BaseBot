"""用 mac mini 上的 Word 把 docx 转成 PDF。invoice 和转介协议共用。

只在装了 Microsoft Word 的 Mac 上能转（docx2pdf 通过 AppleScript 让 Word 另存）；
别的机器上返回一句说明，调用方照发 Word 版。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Word 第一次开一个大文件、或者弹窗等人点时会很慢。超时就先发 Word 版。
PDF_TIMEOUT_SECONDS = 300

# 同一时间只转一批：几个人同时点「生成 Invoice」「生成转介协议」时共用同一对固定目录，
# 不能互相踩。
_PDF_LOCK = threading.Lock()


def _empty(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for item in folder.iterdir():
        if item.is_file():
            item.unlink()


def to_pdf(docx_files: dict[str, bytes], work_dir: Path) -> tuple[dict[str, bytes], str]:
    """用 Word 把一批 docx 转成 PDF。返回 ({pdf 文件名: 字节}, 出错说明)。

    只在装了 Microsoft Word 的 Mac 上能转（docx2pdf 通过 AppleScript 让 Word 另存）。
    在子进程里跑、带超时：Word 卡住不能把机器人一起卡死。

    **永远用同一对目录** ``work_dir/docx`` 和 ``work_dir/pdf``，不用随机的临时目录。
    Word 有沙盒：第一次读写某个文件夹要人在 mac mini 上点「授权访问」，它只记住点过的
    那个文件夹。换成每次一个随机目录的话，每次都要有人去 mac mini 前点一次（2026-09-29
    真机上撞到的）。转完两个目录都清空：里面有收款账号、协议方的证件号，不留在磁盘上。
    """
    if not docx_files:
        return {}, ""
    if sys.platform != "darwin":
        return {}, "这台机器不是 Mac，转不了 PDF（只有装了 Word 的 Mac 能转），先发 Word 版。"

    # 转 PDF 是锦上添花：出了任何没料到的错（目录建不了、磁盘满……）都只换来一句说明，
    # 调用方照发 Word。不能让它把整件事变成一张「系统错误」卡、连 Word 都拿不到。
    try:
        return _convert(docx_files, work_dir)
    except Exception:
        logger.exception("转 PDF 出错")
        return {}, "转 PDF 出错了，先发 Word 版。管理员可以在 mac mini 的日志里看原因。"


def _convert(docx_files: dict[str, bytes], work_dir: Path) -> tuple[dict[str, bytes], str]:
    source, target = work_dir / "docx", work_dir / "pdf"
    with _PDF_LOCK:
        _empty(source)
        _empty(target)
        try:
            for name, data in docx_files.items():
                (source / name).write_bytes(data)
            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; from docx2pdf import convert; "
                        "convert(sys.argv[1], sys.argv[2])",
                        str(source),
                        str(target),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=PDF_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return {}, (
                    "Word 转 PDF 超时了（mac mini 上 Word 可能弹了窗口在等人点），先发 Word 版。"
                )
            except subprocess.CalledProcessError as exc:
                logger.error("docx2pdf 失败: %s", exc.stderr.decode(errors="replace")[-2000:])
                return {}, (
                    "Word 转 PDF 失败了，先发 Word 版。管理员可以在 mac mini 的日志里看原因。"
                )

            pdfs: dict[str, bytes] = {}
            for name in docx_files:
                pdf = target / (Path(name).stem + ".pdf")
                if pdf.is_file():
                    pdfs[pdf.name] = pdf.read_bytes()
            missing = len(docx_files) - len(pdfs)
            return pdfs, (f"有 {missing} 份没转出 PDF，那几份只有 Word 版。" if missing else "")
        finally:
            _empty(source)
            _empty(target)
