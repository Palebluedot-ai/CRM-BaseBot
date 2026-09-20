#!/usr/bin/env python
"""算机器人「断了多久」—— 只在有空窗时说话。

## 为什么需要它

长连接会自己重连（SDK 无限重试），所以「断过」这件事**只有日志知道**。而日志是给人看的流水，
没人会天天翻。结果是：真正该知道的「小时级空窗」和「秒级抖动」混在一起，看不出区别。

这个脚本把日志读成一条时间线，算出每段空窗的时长，并**只报超过阈值的**（默认 1 分钟）：

    秒级抖动（2–10 秒）    TCP 被短暂切断，下一次重试就通 —— 不用管
    分钟级（1–5 分钟）     网络消失后心跳 120 秒才察觉 —— 会漏消息，值得知道
    小时级（数小时）       网络/DNS 长时间不可用（休眠、VPN、断网）—— 必须知道

## 用法

    uv run python scripts/bot_connectivity_report.py --log logs/bot.log
    uv run python scripts/bot_connectivity_report.py --log logs/bot.log --threshold 300

退出码：**有空窗超阈值就是 1**，否则 0 —— 这样挂进 cron / launchd 就是「有事才通知」
（超时或非零退出会告警，一切正常时无声无息）。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 日志里两种形态各出现一次（带 [Lark] 前缀的和 ours logger 的），所以按行去重后只取一种。
CONNECTED = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\] \[INFO\] connected to wss")
DISCONNECTED = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\] \[(?:ERROR|INFO)\] "
    r"(?:disconnected|receive message loop exit)"
)

DEFAULT_THRESHOLD_SECONDS = 60


@dataclass(frozen=True)
class Gap:
    """一段空窗：从断开到重新连上。"""

    started: datetime
    ended: datetime | None  # None = 到日志结尾还没连上

    @property
    def seconds(self) -> float | None:
        if self.ended is None:
            return None
        return (self.ended - self.started).total_seconds()


def parse_events(text: str) -> list[tuple[datetime, str]]:
    """把日志扫成 [(时间, 'up' | 'down')]，按时间排序。

    只认那一行的时间戳 —— 不去猜 chat 内容、不看后面的自述。
    """
    events: list[tuple[datetime, str]] = []
    for line in text.splitlines():
        match = DISCONNECTED.search(line)
        if match:
            events.append((datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"), "down"))
            continue
        match = CONNECTED.search(line)
        if match:
            events.append((datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"), "up"))
    events.sort(key=lambda item: item[0])
    # 同一秒里重复的行（两种 logger 各打一遍）只留一条
    deduped: list[tuple[datetime, str]] = []
    for event in events:
        if deduped and deduped[-1] == event:
            continue
        deduped.append(event)
    return deduped


def compute_gaps(events: list[tuple[datetime, str]]) -> list[Gap]:
    """把 down -> up 配成空窗。日志开头就是 up（没有 down）不算空窗。"""
    gaps: list[Gap] = []
    down_at: datetime | None = None
    for when, kind in events:
        if kind == "down" and down_at is None:
            down_at = when
        elif kind == "up" and down_at is not None:
            gaps.append(Gap(started=down_at, ended=when))
            down_at = None
    if down_at is not None:
        gaps.append(Gap(started=down_at, ended=None))  # 到日志结尾还没回来
    return gaps


def notable(gaps: list[Gap], threshold: float = DEFAULT_THRESHOLD_SECONDS) -> list[Gap]:
    """超过阈值的空窗（以及「还没回来」的那段）。"""
    return [gap for gap in gaps if gap.seconds is None or gap.seconds > threshold]


def _human(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="算机器人长连接断了多久（只在有空窗时告警）")
    parser.add_argument("--log", default="logs/bot.log", help="机器人日志，- 表示 stdin")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD_SECONDS,
        help=f"超过多少秒的空窗才算「值得知道」（默认 {DEFAULT_THRESHOLD_SECONDS}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.log == "-":
        text = sys.stdin.read()
    else:
        path = Path(args.log)
        if not path.is_file():
            print(f"找不到日志：{path}", file=sys.stderr)
            return 2
        text = path.read_text(encoding="utf-8", errors="replace")

    events = parse_events(text)
    if not events:
        print(f"{args.log} 里没有连接记录（机器人可能还没跑过？）")
        return 0

    gaps = compute_gaps(events)
    interesting = notable(gaps, args.threshold)
    closed = [gap for gap in gaps if gap.seconds is not None]
    total_down = sum(gap.seconds or 0 for gap in closed)

    longest = max((gap.seconds or 0 for gap in gaps), default=0.0)
    print(
        f"连接记录 {len(events)} 条 · 断开 {len(gaps)} 次 · "
        f"累计空窗 {_human(total_down)} · 最长 {_human(longest)}"
    )
    if not interesting:
        print(f"没有超过 {_human(args.threshold)} 的空窗 —— 都是秒级抖动，不用管。")
        return 0

    print(
        f"\n⚠️ 有 {len(interesting)} 段空窗超过 {_human(args.threshold)}（这些窗口里的消息不补发）："
    )
    for gap in interesting:
        if gap.seconds is None:
            print(f"  {gap.started} 起断开，到日志结尾还没连上")
        else:
            print(f"  {gap.started} → {gap.ended}   空窗 {_human(gap.seconds)}")
    print("\n判读：分钟级多半是网络瞬断；小时级要查机器是不是休眠/断网（见 docs/BOT.md 第四节）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
