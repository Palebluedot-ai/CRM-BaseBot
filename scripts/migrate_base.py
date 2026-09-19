#!/usr/bin/env python
"""一条命令把整套 Base 从你的账号搬到另一个账号。

    uv run python scripts/migrate_base.py --target-env .env.target --dry-run   # 先预演
    uv run python scripts/migrate_base.py --target-env .env.target --apply     # 真搬

它会做完这些（中间不用手工点 Base）：目标端建结构（含公式列）→ 搬渠道 → 搬客户 →
搬看板 → 搬名册 → 逐表比对行数 → 打印「还需人工」的那两件。

两边只有「应用凭证」和「open_id」不一样：**表头和内容一模一样**（同一个 schema 建出来的），
所以字段按名字对应、关联按业务键（渠道编号 / 客户UID）重建，不需要任何映射表。

准备目标环境文件（复制 .env.example 改）：

    LARK_APP_ID=<目标账号那个飞书应用的 App ID>
    LARK_APP_SECRET=<同一个应用的 Secret>
    LARK_BASE_APP_TOKEN=<目标 Base 的 token>
    BUSINESS_TIMEZONE=Asia/Singapore

表 id（TABLE_*）可以留空 —— 迁移按表名找，找到什么用什么。

逻辑住在 ``crm_basebot.migration``，这个文件只有命令行和打印。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.migration import run_migration  # noqa: E402
from crm_basebot.migration.runner import MigrationError, print_report  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把整套 Base（结构 + 数据）从 .env 搬到 --target-env 指向的账号"
    )
    parser.add_argument(
        "--target-env",
        default=".env.target",
        help="目标账号的环境文件，默认 .env.target",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真的建表并搬数据；不加则只预演（默认）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只预演（这就是默认行为，写着读起来更明确）",
    )
    parser.add_argument(
        "--include-commission",
        action="store_true",
        help="也搬「月度汇总」（默认不搬，目标端跑一次 reconcile 就有）",
    )
    parser.add_argument(
        "--include-audit",
        action="store_true",
        help="也搬「审计日志」（默认不搬，它是历史流水）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if args.apply and args.dry_run:
        print(
            "--apply 和 --dry-run 一起给没有意义：要么预演，要么真搬。",
            file=sys.stderr,
        )
        return 2

    source = load_settings()
    require_settings(source, "LARK_BASE_APP_TOKEN", "TABLE_REFERRAL", "TABLE_CLIENT")

    try:
        result = run_migration(
            target_env_path=Path(args.target_env),
            apply=args.apply,
            include_audit=args.include_audit,
            include_commission=args.include_commission,
            source_settings=source,
        )
    except MigrationError as exc:
        print(f"\n迁移没法继续：{exc}", file=sys.stderr)
        return 1

    print_report(result, target_env_path=Path(args.target_env))
    return 0 if (not result.applied or result.ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
