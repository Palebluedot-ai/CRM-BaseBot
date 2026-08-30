#!/usr/bin/env python
"""只读探查现有的多维表格结构。

Base 里已经有一堆表了，所以第一步不是建表而是看清现状：哪些能复用、哪些要新建、
关键字段到底是什么类型。这个脚本只读，不改任何数据。

    uv run python scripts/inspect_base.py

输出一份人看的摘要，外加 schema_snapshot.json 供后续比对（已 gitignore，
因为字段名可能带客户信息）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.config import get_settings  # noqa: E402
from crm_basebot.lark.bitable import (  # noqa: E402
    FIELD_TYPE_AUTO_NUMBER,
    FIELD_TYPE_NUMBER,
    BitableClient,
    save_snapshot,
)
from crm_basebot.lark.field_types import type_name  # noqa: E402

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "schema_snapshot.json"

# 探查时重点盯的字段：名字里带这些词的，单独拎出来提醒
UID_HINTS = ("uid", "客户号", "客户 id", "客户id")
REFERRAL_NO_HINTS = ("编号", "referral no", "referral_no", "渠道号")
PROFIT_HINTS = ("pnl", "profit", "收益", "利润")


def _looks_like(name: str, hints: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in hints)


def main() -> int:
    settings = get_settings()

    if not settings.base_app_token:
        print(
            "LARK_BASE_APP_TOKEN 没填。\n"
            "从多维表格地址栏取：https://xxx.feishu.cn/base/<这一段>?table=...\n"
            "详见 docs/LARK_APP_SETUP.md 第 7 步。",
            file=sys.stderr,
        )
        return 1

    client = BitableClient(settings.base_app_token)

    print(f"Base: {settings.base_app_token}\n")

    tables = client.list_tables()
    print(f"共 {len(tables)} 张表\n")

    findings: list[str] = []

    for table in tables:
        fields = client.list_fields(table.table_id)
        print(f"── {table.name}  ({table.table_id})  {len(fields)} 个字段")

        for f in fields:
            flags = []
            if f.is_primary:
                flags.append("主字段")
            if f.is_read_only:
                flags.append("只读")
            flag_text = f"  [{', '.join(flags)}]" if flags else ""
            print(f"     {f.name:<24} {type_name(f.type):<10}{flag_text}")

            if _looks_like(f.name, UID_HINTS):
                if f.type == FIELD_TYPE_NUMBER:
                    findings.append(
                        f"严重：{table.name}.{f.name} 是「数字」类型。"
                        "18-19 位的 UID 存成数字会在服务端就丢精度，必须改成「文本」。"
                    )
                else:
                    findings.append(
                        f"{table.name}.{f.name} 是「{type_name(f.type)}」，"
                        "不是数字类型，精度安全。"
                    )

            if _looks_like(f.name, REFERRAL_NO_HINTS):
                if f.type == FIELD_TYPE_AUTO_NUMBER:
                    serial = f.props.get("auto_serial", {})
                    findings.append(
                        f"{table.name}.{f.name} 已经是「自动编号」，规则 {serial}。"
                        "递增由飞书保证，不用自己写。"
                    )
                else:
                    findings.append(
                        f"{table.name}.{f.name} 是「{type_name(f.type)}」而非自动编号。"
                        "需要实测能否安全转换，见 scripts/verify_numbering.py。"
                    )

            if _looks_like(f.name, PROFIT_HINTS):
                findings.append(
                    f"{table.name}.{f.name} 是「{type_name(f.type)}」—— 佣金基数候选。"
                )

        print()

    if findings:
        print("=" * 60)
        print("需要注意的点\n")
        for item in findings:
            print(f"  · {item}")
        print()

    snapshot = client.snapshot_schema()
    save_snapshot(snapshot, SNAPSHOT_PATH)
    print(f"结构快照已写入 {SNAPSHOT_PATH.name}")
    print("\n把上面各表的 table_id 回填到 .env 里对应的 TABLE_* 变量。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
