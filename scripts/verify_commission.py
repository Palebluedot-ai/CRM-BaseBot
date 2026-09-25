#!/usr/bin/env python
"""按 UID 核对看板上的客户关联和分佣比例（只读）。

## 为什么要有这个脚本

佣金链路上唯一可能**静默出错**的地方，是「哪一行挂到了哪个客户」：挂错了钱就记到
别人头上，而且没有任何提示 —— 平台的公式不报错，只是算出一个看着挺正常的数。

所以这里不信任 Base 里那几列公式，自己按 UID 走一遍：

    看板行的 用户ID ──按 UID 匹配──► 客户表里那条客户
                                       └─► 该客户的「所属渠道」
                                             └─► 该渠道的「分佣比例」
再和看板上反查出来的比例逐行对比。（以前还对「本笔佣金」那一列，2026-09-25 那一列删了：
AI 规则上线后它会和月结对不上，而月结从来不读它。）

**全程用 UID 和记录 id，不用姓名** —— 姓名会撞车、会大小写不一致、
会因为「先名后姓 / 先姓后名」对不上，UID 不会。

## 它报什么

| 类别 | 含义 |
|---|---|
| 客户表 UID 重复 | 按 UID 匹配时「先匹配上谁」不确定 → 佣金可能记到别的渠道 |
| 挂错 | 看板这行挂了关联，但那个客户的 UID ≠ 这行的 用户ID |
| 漏挂 | 客户表里有这个 UID，看板这行却没挂关联（导入时还没登记） |
| 比例不符 | 看板公式算出的比例 ≠ 从渠道表复算的比例 |

## 用法

    uv run python scripts/verify_commission.py              # 全表
    uv run python scripts/verify_commission.py --month 2026-03
    uv run python scripts/verify_commission.py --examples 30

退出码：``0`` 全部一致；``1`` 有差异（明细打在 stdout，可直接贴给人看）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import extract_text, link_ids, to_number, to_uid  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)

RATE_TOLERANCE = 0.001


@dataclass(frozen=True)
class Channel:
    no: str
    name: str
    rate: float | None

    @property
    def label(self) -> str:
        return f"{self.no} {self.name}".strip()


@dataclass(frozen=True)
class Client:
    record_id: str
    name: str
    channel_id: str | None


@dataclass
class Report:
    board_rows: int = 0
    linked: int = 0
    not_registered: int = 0
    duplicate_uids: dict[str, list[Client]] = field(default_factory=dict)
    wrong_link: list[str] = field(default_factory=list)
    missed_link: list[str] = field(default_factory=list)
    rate_mismatch: list[str] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return (
            len(self.duplicate_uids)
            + len(self.wrong_link)
            + len(self.missed_link)
            + len(self.rate_mismatch)
        )


def load_channels(bitable: BitableClient, table_id: str) -> dict[str, Channel]:
    channels: dict[str, Channel] = {}
    for record in bitable.iter_records(table_id):
        channels[record.record_id] = Channel(
            no=extract_text(record.fields.get(schema.REFERRAL_NO)),
            name=extract_text(record.fields.get(schema.REFERRAL_NAME)),
            rate=to_number(record.fields.get(schema.REFERRAL_RATE)),
        )
    return channels


def load_clients(bitable: BitableClient, table_id: str) -> dict[str, list[Client]]:
    """UID -> 客户记录列表。正常情况下每个 UID 只有一条。"""
    clients: dict[str, list[Client]] = defaultdict(list)
    for record in bitable.iter_records(table_id):
        uid = to_uid(record.fields.get(schema.CLIENT_UID))
        if not uid:
            continue
        links = link_ids(record.fields.get(schema.CLIENT_REFERRAL_LINK))
        clients[uid].append(
            Client(
                record_id=record.record_id,
                name=extract_text(record.fields.get(schema.CLIENT_NAME)),
                channel_id=links[0] if links else None,
            )
        )
    return clients


def audit(
    board_rows: list[dict],
    channels: dict[str, Channel],
    clients: dict[str, list[Client]],
) -> Report:
    """``board_rows`` 每项是 ``{"fields": {...}, "record_id": ...}``。"""
    report = Report(duplicate_uids={uid: rows for uid, rows in clients.items() if len(rows) > 1})

    for row in board_rows:
        fields = row["fields"]
        report.board_rows += 1
        uid = to_uid(fields.get(schema.BOARD_CLIENT_UID))
        links = link_ids(fields.get(schema.BOARD_CLIENT_LINK))
        candidates = clients.get(uid or "", [])
        if not candidates:
            report.not_registered += 1
            continue

        client = candidates[0]
        channel = channels.get(client.channel_id or "")
        rate = channel.rate if channel else None
        label = channel.label if channel else "(无渠道)"

        if not links:
            report.missed_link.append(
                f"uid={uid} 客户 {client.record_id}（{client.name[:28]}）在客户表里，"
                f"但看板这行没挂关联 —— 该走 {label} 比例={rate}"
            )
            continue

        report.linked += 1
        if links[0] != client.record_id:
            report.wrong_link.append(
                f"uid={uid} 挂到了 {links[0]}，但按 UID 应该挂 {client.record_id}"
            )

        board_rate = to_number(fields.get(schema.BOARD_CLIENT_RATE))
        if board_rate is not None and rate is not None and abs(board_rate - rate) > RATE_TOLERANCE:
            report.rate_mismatch.append(
                f"uid={uid} 看板比例={board_rate} 复算比例={rate}（{label}）"
            )

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按 UID 复算佣金并与看板公式对账")
    parser.add_argument("--month", help="只核对这个月 YYYY-MM")
    parser.add_argument("--examples", type=int, default=10, help="每类问题最多打印几条，默认 10")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(
        settings,
        "LARK_BASE_APP_TOKEN",
        "TABLE_DAILY_BOARD",
        "TABLE_CLIENT",
        "TABLE_REFERRAL",
    )
    return run(args, settings, BitableClient(settings.base_app_token))


def run(args: argparse.Namespace, settings, bitable: BitableClient) -> int:
    channels = load_channels(bitable, settings.table_referral)
    clients = load_clients(bitable, settings.table_client)

    board_rows: list[dict] = []
    for record in bitable.iter_records(settings.table_daily_board):
        if args.month and extract_text(record.fields.get(schema.BOARD_MONTH)) != args.month:
            continue
        board_rows.append({"record_id": record.record_id, "fields": record.fields})

    report = audit(board_rows, channels, clients)

    print(
        f"渠道表 {len(channels)} 条；客户表 {sum(len(v) for v in clients.values())} 条"
        f"（{len(clients)} 个 UID）"
    )
    print(f"核对看板 {report.board_rows} 行" + (f"（{args.month}）" if args.month else ""))
    print(
        f"  已挂关联 {report.linked}；客户表里没有的 UID {report.not_registered}（没登记过，正常）"
    )

    def show(title: str, items: list[str]) -> None:
        if not items:
            return
        print(f"\n  ⚠ {title}（{len(items)} 条）")
        for line in items[: args.examples]:
            print(f"      {line}")
        if len(items) > args.examples:
            print(f"      …还有 {len(items) - args.examples} 条")

    if report.duplicate_uids:
        print(f"\n  ⚠ 客户表里 UID 重复（{len(report.duplicate_uids)} 个）")
        for uid, rows in list(report.duplicate_uids.items())[: args.examples]:
            print(f"      {uid}: {', '.join(row.record_id for row in rows)}")
    show("挂错客户", report.wrong_link)
    show("漏挂（客户表里有却没挂）", report.missed_link)
    show("比例不符", report.rate_mismatch)

    if report.problems:
        print(f"\n结论：发现 {report.problems} 类差异，上面已列出。")
        return 1
    print("结论：逐行一致，没有差异。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
