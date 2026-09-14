#!/usr/bin/env python
"""给历史渠道记录回填主字段，修好 Referred Client.所属渠道 显示成「无标题记录」。

背景：Referred Client.所属渠道 是指向 Referral Information 的单向关联字段。飞书
里关联字段永远展示被关联记录的**主字段**值。建表时 sync_base 只传了表名，飞书
给渠道表塞了一个默认主字段（通常叫「文本」），而 ReferralService 从未往这个字段
写过东西 —— 所以所有关联展示都是「无标题记录」。

代码层的修复已经进 ReferralService.create()，新登记的渠道自动带主字段值。这个
脚本负责把已有记录补齐。

    uv run python scripts/backfill_referral_primary.py           # 预演，只打印
    uv run python scripts/backfill_referral_primary.py --apply   # 真的写

只写主字段是空、或和目标不一致的记录。已经和目标一致的跳过，重跑幂等。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.referral import display_title  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import extract_text  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填渠道表主字段")
    parser.add_argument("--apply", action="store_true", help="真的写，默认只预演")
    args = parser.parse_args(argv)

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_REFERRAL")

    bitable = BitableClient(settings.base_app_token)
    primary = bitable.resolve_primary_field(settings.table_referral)

    print(f"渠道表主字段：{primary.name}\n")

    if primary.name == schema.REFERRAL_NAME:
        # 主字段本身就是渠道名称，create 时已经写了，没什么可回填的。
        # 但历史上如果曾经空过一段时间才切过来，还是可能有空值 —— 继续往下扫。
        print(f"主字段就是「{schema.REFERRAL_NAME}」，只补空值。")

    updated = 0
    skipped = 0
    empty_name_or_no = 0

    # 用 iter_records 遍历所有渠道记录。字段列表带上主字段，才能判断当前值。
    field_names = [schema.REFERRAL_NO, schema.REFERRAL_NAME, primary.name]
    for record in bitable.iter_records(settings.table_referral, field_names=field_names):
        no = extract_text(record.fields.get(schema.REFERRAL_NO))
        name = extract_text(record.fields.get(schema.REFERRAL_NAME))
        target = display_title(no, name)

        if not target:
            # 编号和名称都是空的，拼不出有意义的主字段值。别硬写空字符串盖掉现有值。
            empty_name_or_no += 1
            continue

        current = extract_text(record.fields.get(primary.name))
        if current == target:
            skipped += 1
            continue

        print(f"  · {record.record_id}  {current or '(空)'} -> {target}")
        if args.apply:
            bitable.update_record(
                settings.table_referral,
                record.record_id,
                {primary.name: target},
            )
        updated += 1

    print()
    verb = "已写入" if args.apply else "将写入（预演）"
    print(f"{verb} {updated} 条；跳过 {skipped} 条（已一致）；"
          f"{empty_name_or_no} 条编号和名称均为空，未处理。")

    if updated and not args.apply:
        print("\n确认无误后加 --apply 真正执行。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
