#!/usr/bin/env python
"""按名字从看板找出客户的 UID，登记到一个渠道名下，顺手把他以前的交易挂上。

    uv run python scripts/register_from_board.py --referral R029 --name "HAN BAO" \
        --ai-status 开户即AI            # 预演
    uv run python scripts/register_from_board.py --referral R029 --name "HAN BAO" \
        --ai-status 开户即AI --apply    # 真写

不加 ``--apply`` 只预演。给财务表里有、我们没登记的客户用（2026-09-30：HAN BAO、
PRIMAL TECH SUPPLY）—— 不用人去看板里抄 19 位的 UID。

**名字要在看板上只对应一个 UID 才登记。** 对到两个以上就列出来停下，用 ``--uid`` 指定；
一个都对不到就列出名字相近的，让人看是不是写法不一样。名字比较时忽略大小写和标点
（「PTE. LTD.」和「PTE LTD」算一样）。

登记人 = 渠道负责人（渠道那一行的「登记人OpenID」），和 register_ai_clients.py 一样。
已经登记过的 UID 一律不改。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.bot.auth import Sales, SalesDirectory  # noqa: E402
from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.audit import AuditLog  # noqa: E402
from crm_basebot.domain.referral import ValidationError  # noqa: E402
from crm_basebot.domain.referred_client import ClientInput, ReferredClientService  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import PrecisionLossError, extract_text, to_uid  # noqa: E402
from crm_basebot.pipeline import board  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402


def norm(name: str) -> str:
    return re.sub(r"[^0-9A-Z一-鿿]", "", (name or "").upper())


def board_uids(records, name: str) -> tuple[Counter[str], dict[str, str]]:
    """看板上名字对得上的 UID（次数），和名字相近但对不上的写法（给人看）。"""
    wanted = norm(name)
    first_word = norm(name.split()[0]) if name.split() else wanted
    exact: Counter[str] = Counter()
    similar: dict[str, str] = {}
    for fields in records:
        label = extract_text(fields.get(schema.BOARD_CLIENT_NAME))
        key = norm(label)
        if not key:
            continue
        try:
            uid = to_uid(fields.get(schema.BOARD_CLIENT_UID))
        except PrecisionLossError:
            continue
        if not uid:
            continue
        if key == wanted:
            exact[uid] += 1
        elif first_word and first_word in key:
            similar.setdefault(label, uid)
    return exact, similar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按看板上的名字登记客户")
    parser.add_argument("--referral", required=True, help="渠道编号，例如 R029")
    parser.add_argument("--name", required=True, help="客户名称，和看板上的写法一样")
    parser.add_argument(
        "--ai-status", required=True, choices=list(schema.AI_STATUS_OPTIONS), help="AI 状态"
    )
    parser.add_argument("--ai-date", help="升级为AI 的日期 YYYY-MM-DD")
    parser.add_argument("--uid", help="名字对到几个 UID 时，指定用哪个")
    parser.add_argument("--apply", action="store_true", help="真写；不加则只预演")
    args = parser.parse_args(argv)

    settings = load_settings()
    require_settings(
        settings,
        "LARK_BASE_APP_TOKEN",
        "TABLE_CLIENT",
        "TABLE_REFERRAL",
        "TABLE_SALES",
        "TABLE_AUDIT",
        "TABLE_DAILY_BOARD",
    )
    bitable = BitableClient(settings.base_app_token)
    referral_no = args.referral.strip().upper()

    records = [
        r.fields
        for r in bitable.iter_records(
            settings.table_daily_board,
            field_names=[schema.BOARD_CLIENT_NAME, schema.BOARD_CLIENT_UID],
        )
    ]
    exact, similar = board_uids(records, args.name)
    if args.uid:
        uid = args.uid.strip()
        if uid not in exact:
            print(
                f"「{args.name}」在看板上没有 UID {uid}。"
                f"看板上对得上的是：{dict(exact) or '（没有）'}"
            )
            return 1
    elif len(exact) == 1:
        uid = next(iter(exact))
    elif not exact:
        print(f"看板上找不到「{args.name}」。")
        if similar:
            print("名字相近的有（写法不一样的话，用看板上的写法再跑一次）：")
            for label, candidate in sorted(similar.items()):
                print(f"  {label}　UID {candidate}")
        return 1
    else:
        print(f"「{args.name}」在看板上对到 {len(exact)} 个 UID，用 --uid 指定是哪个：")
        for candidate, count in exact.most_common():
            print(f"  {candidate}　{count} 笔交易")
        return 1

    # 登记人 = 渠道负责人
    owner_open_id = owner_name = ""
    for record in bitable.iter_records(settings.table_referral):
        if extract_text(record.fields.get(schema.REFERRAL_NO)).upper() == referral_no:
            owner_open_id = extract_text(record.fields.get(schema.REFERRAL_OWNER_OPEN_ID))
            owner_name = extract_text(record.fields.get(schema.REFERRAL_SALES_NAME))
            break
    else:
        print(f"渠道表里没有 {referral_no}")
        return 1
    if not owner_open_id:
        print(
            f"{referral_no} 那一行的「{schema.REFERRAL_OWNER_OPEN_ID}」是空的，先在 Base 里填上。"
        )
        return 1
    rostered = SalesDirectory(bitable, settings.table_sales).lookup(owner_open_id)
    owner = Sales(
        open_id=owner_open_id,
        name=rostered.name if rostered else owner_name,
        role=schema.ROLE_SALES,
        is_active=True,
    )

    service = ReferredClientService(
        bitable,
        settings.table_client,
        settings.table_referral,
        AuditLog(bitable, settings.table_audit),
        tz=ZoneInfo(settings.business_timezone),
    )
    existing = service.find_by_uid(uid)
    if existing is not None:
        print(f"UID {uid} 已经登记过了，不改。要改归属请在 Base 里手动调整。")
        return 1

    try:
        data = ClientInput(
            uid=uid,
            name=args.name.strip(),
            referral_no=referral_no,
            ai_status=args.ai_status,
            ai_date=date.fromisoformat(args.ai_date) if args.ai_date else None,
        ).validated()
    except (ValidationError, ValueError) as exc:
        print(f"资料不对：{exc}")
        return 1

    print(
        f"会登记：{data.name}　UID {uid}（看板上 {exact[uid]} 笔交易）"
        f"　→ {referral_no}（登记人 {owner.name}）　{data.ai_status}"
    )
    if not args.apply:
        print("（预演，没写。确认无误后加 --apply）")
        return 0

    record_id = service.create(owner, data)
    linked = board.relink_uid(bitable, settings.table_daily_board, uid, record_id)
    print(f"登记好了，看板上 {linked} 笔以前的交易已经挂到 {referral_no}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
