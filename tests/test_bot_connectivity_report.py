"""机器人长连接的空窗统计。

这个脚本的唯一用途是把「断了多久」变成可判断的数字，所以测试盯三件事：
**只认那一行、两种 logger 不重复算、还没连回来的那段也算空窗**。第三条最容易漏：
日志读到一半程序还在断着，此时如果只统计「已配对的 down→up」，你会看到一份过于乐观的报告。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


report = _load("bot_connectivity_report")

# 真实日志里同一次事件会打两遍（[Lark] 前缀一行 + ours logger 一行）
SAMPLE = """\
[Lark] [2026-09-18 23:36:36,185] [INFO] connected to wss://x
2026-09-18 23:36:36,185 INFO    Lark: connected to wss://x
2026-09-19 07:30:38,736 ERROR   Lark: receive message loop exit, err: no close frame
[Lark] [2026-09-19 07:30:38,736] [ERROR] receive message loop exit, err: no close frame
[Lark] [2026-09-19 15:44:53,222] [INFO] connected to wss://x
"""


def test_两种logger各打一遍只算一条():
    events = report.parse_events(SAMPLE)
    assert events == [
        (datetime(2026, 9, 18, 23, 36, 36), "up"),
        (datetime(2026, 9, 19, 7, 30, 38), "down"),
        (datetime(2026, 9, 19, 15, 44, 53), "up"),
    ]


def test_配出空窗并算出时长():
    gaps = report.compute_gaps(report.parse_events(SAMPLE))
    assert len(gaps) == 1
    assert gaps[0].seconds == 8 * 3600 + 14 * 60 + 15  # 8 小时 14 分 15 秒


def test_开头就连上不算空窗():
    text = "[Lark] [2026-09-20 00:00:00,000] [INFO] connected to wss://x\n"
    assert report.compute_gaps(report.parse_events(text)) == []


def test_还没连回来的那段也算空窗():
    """日志读到一半程序还在断着 —— 不能因为「没配上 up」就当成没断过。"""
    text = (
        "[Lark] [2026-09-20 00:00:00,000] [INFO] connected to wss://x\n"
        "[Lark] [2026-09-20 00:01:00,000] [ERROR] receive message loop exit, err: boom\n"
    )
    gaps = report.compute_gaps(report.parse_events(text))
    assert len(gaps) == 1
    assert gaps[0].ended is None
    assert gaps[0].seconds is None
    # 「还没回来」必须算进要注意的，否则报告会过于乐观
    assert report.notable(gaps, threshold=1) == gaps


def test_秒级抖动不算进要注意的():
    text = (
        "[Lark] [2026-09-20 17:27:33,000] [ERROR] receive message loop exit, err: x\n"
        "[Lark] [2026-09-20 17:27:39,000] [INFO] connected to wss://x\n"
    )
    gaps = report.compute_gaps(report.parse_events(text))
    assert gaps[0].seconds == 6
    assert report.notable(gaps, threshold=60) == []


def test_阈值生效():
    text = (
        "[Lark] [2026-09-20 10:40:08,000] [ERROR] receive message loop exit, err: x\n"
        "[Lark] [2026-09-20 10:42:03,000] [INFO] connected to wss://x\n"
    )
    gaps = report.compute_gaps(report.parse_events(text))
    assert len(report.notable(gaps, threshold=60)) == 1
    assert report.notable(gaps, threshold=600) == []


def test_聊天内容里的字样不会误判():
    text = "销售说：机器人好像 disconnected 了\n这不是日志行\n"
    assert report.parse_events(text) == []


def test_人话格式化():
    assert report._human(6) == "6 秒"
    assert report._human(115) == "1.9 分钟"
    assert report._human(29500) == "8.2 小时"


def test_默认阈值是一分钟():
    args = report.build_parser().parse_args([])
    assert args.threshold == 60
    assert args.log == "logs/bot.log"
