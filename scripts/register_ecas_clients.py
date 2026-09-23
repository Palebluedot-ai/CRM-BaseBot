#!/usr/bin/env python
"""把**个别**已确认符合 PI 资格的 ECAS 客户登记进客户表。

    uv run python scripts/register_ecas_clients.py --file "Wallet_and_Trades_ECAS.xlsx"
    uv run python scripts/register_ecas_clients.py --file "..." --client "某某某" --apply

## 先读这一段：整批补登记是错的

ECAS 表里有 79 个被介绍的客户，其中只有 9 个登记在客户表里，于是只有那 9 个的交易
会算出佣金。这个差额曾经被当成「漏登记」—— **不是。**

**交易佣金要求客户符合 PI（专业投资者）资格**（2026-09-23 业务确认）。ECAS 的介绍
关系不会自动延伸到交易那边：介绍人照样拿 ECAS 返佣，但客户不够 PI 资格就没有交易
佣金可分。所以「有的有、有的没有」是**正确状态**，不是待修复的缺口。

财务 2026-08 那份权威输出没有付另外那 68 位，和这条规则一致；他们的 PnL 出现在
Unmatched 分页里，也和这条规则一致。

于是这个脚本**不再是整批补登记工具**。它只用在一件事上：某个 ECAS 客户经业务确认
符合 PI 资格之后，把那一个客户登记进去。所以 ``--apply`` 必须配 ``--client``，
不带 ``--client`` 的 ``--apply`` 会被直接拒绝 —— 整批写进去等于给 68 个不该拿交易
佣金的客户开了口子，而且写进去之后没有任何地方会告诉你错了。

ECAS 自己的返佣和这件事**完全无关**：那套账按 ECAS 表逐行算，79 个一个不少，
见 ``docs/ECAS.md``。

## 绝不做的事

**已经登记过的客户一律不碰。** 如果客户表里那条挂的渠道和 ECAS 表说的不一样，
列出来让人判断，不覆盖 —— 覆盖等于把一笔已经在付的佣金悄悄改付给另一个人。

**UID 拿不到就不登记。** 客户表那一行的全部作用就是让看板按 UID join 到它；
没有 UID 的行对交易佣金毫无用处，只会让人以为已经登记好了。

**渠道名字对不上就不猜。** 姓名顺序颠倒（KE JIAHUI / JIAHUI KE）会尝试，但每一个
靠颠倒才对上的都单独列出来，等人确认。
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import import_registrations as reg  # noqa: E402

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.names import norm, tokens  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.values import extract_text, to_uid  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

logger = logging.getLogger(__name__)

SHEET = "ECAS"
COL_CLIENT, COL_TIME, COL_SALES, COL_UID, COL_REFERRER = 0, 2, 4, 5, 6

# Referrer 栏填成栏位标题的那种行。不是渠道名，是没填好的资料。
BAD_REFERRER = {"", "REFERRER"}

# 查 Client Directory 用的表名。没有这张表也能跑，只是只剩 ECAS 表自带的 UID 可用。
DIRECTORY_TABLE = "Client Directory"
DIRECTORY_UID, DIRECTORY_NAME = "user_id", "client_name"


def _cell(row, index):
    """短行也安全地取一格 —— xlsx 的尾部空列不一定会补齐。"""
    return row[index] if index < len(row) else None


@dataclass
class Plan:
    uid: str
    client_name: str
    sales: str
    referral_record_id: str
    referral_label: str
    uid_source: str
    referral_how: str


def read_ecas(path: Path) -> tuple[dict[str, dict], list[str]]:
    """ECAS xlsx -> {规整客户名: {...}}，外加坏行说明。同一客户多次申请只留一条。"""
    if not path.exists():
        raise reg.RegistrationImportError(f"找不到文件：{path}")
    from openpyxl import load_workbook

    # 不能用 read_only：飞书导出的 xlsx 里 <dimension> 是错的，read_only 信它，
    # 结果只读到表头就停（见 backfill_client_uids.py 里的同一条）。
    workbook = load_workbook(path, data_only=True)
    out: dict[str, dict] = {}
    bad: list[str] = []
    try:
        sheet = reg._find_sheet(workbook, SHEET)
        rows = reg._rows(sheet)
        for row_num, row in enumerate(rows[1:], start=2):
            client = reg._clean_text(_cell(row, COL_CLIENT))
            referrer = reg._clean_text(_cell(row, COL_REFERRER))
            if not client:
                continue
            if norm(referrer) in BAD_REFERRER:
                if referrer:
                    bad.append(f"第 {row_num} 行「{client}」的 Referrer 填的是「{referrer}」")
                continue
            uid = ""
            raw_uid = _cell(row, COL_UID)
            if raw_uid not in (None, ""):
                uid = reg._cell_to_uid(raw_uid, where=f"「{SHEET}」第 {row_num} 行")
                if reg._damaged_uid_reason(uid):
                    bad.append(f"第 {row_num} 行「{client}」的 UID {uid} 像被 Excel 改坏过")
                    uid = ""
            out.setdefault(
                norm(client),
                {
                    "client": client,
                    "referrer": referrer,
                    "uid": uid,
                    "sales": reg._clean_text(_cell(row, COL_SALES)),
                },
            )
    finally:
        workbook.close()
    return out, bad


def load_directory(bitable: BitableClient) -> tuple[dict[str, set], dict[str, set]]:
    """Client Directory -> (按名字, 按顺序无关的名字) 两张 UID 索引。"""
    by_name: dict[str, set] = {}
    by_tokens: dict[str, set] = {}
    table = {t.name: t.table_id for t in bitable.list_tables()}.get(DIRECTORY_TABLE)
    if not table:
        return by_name, by_tokens
    for record in bitable.iter_records(table, field_names=[DIRECTORY_UID, DIRECTORY_NAME]):
        uid = to_uid(record.fields.get(DIRECTORY_UID))
        name = extract_text(record.fields.get(DIRECTORY_NAME))
        if uid and name:
            by_name.setdefault(norm(name), set()).add(uid)
            by_tokens.setdefault(tokens(name), set()).add(uid)
    return by_name, by_tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把个别已确认符合 PI 资格的 ECAS 客户登记进客户表")
    parser.add_argument("--file", required=True, help="ECAS 的 xlsx")
    parser.add_argument(
        "--client",
        action="append",
        default=[],
        metavar="客户名",
        help="只处理这个客户（可以给多次）。--apply 必须配它，理由见模块开头",
    )
    parser.add_argument("--apply", action="store_true", help="真写；不加则只预演")
    args = parser.parse_args(argv)

    if args.apply and not args.client:
        parser.error(
            "不带 --client 的 --apply 已经被禁掉了。\n"
            "交易佣金要求客户符合 PI 资格（2026-09-23 业务确认），"
            "ECAS 表里那 79 个不是都够格 ——\n"
            "整批写进去等于给不够格的客户开了口子，而且写完没有任何地方会告诉你错了。\n"
            '确认某个客户够格之后，用 --client "客户名" --apply 一个一个来。'
        )

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_CLIENT", "TABLE_REFERRAL")
    bitable = BitableClient(settings.base_app_token)

    ecas, bad_rows = read_ecas(Path(args.file))
    print(f"ECAS 表里有介绍人的客户：{len(ecas)} 个")
    for line in bad_rows:
        print(f"  ⚠️ {line}")

    # ---------- 渠道索引 ----------
    ref_by_name: dict[str, tuple[str, str]] = {}
    ref_by_tokens: dict[str, tuple[str, str]] = {}
    for record in bitable.iter_records(
        settings.table_referral, field_names=[schema.REFERRAL_NO, schema.REFERRAL_NAME]
    ):
        name = extract_text(record.fields.get(schema.REFERRAL_NAME))
        code = extract_text(record.fields.get(schema.REFERRAL_NO))
        if not name:
            continue
        ref_by_name.setdefault(norm(name), (record.record_id, f"{code} {name}"))
        ref_by_tokens.setdefault(tokens(name), (record.record_id, f"{code} {name}"))

    # ---------- 客户表现状 ----------
    existing_by_uid: dict[str, tuple[str, str, list[str]]] = {}
    for record in bitable.iter_records(
        settings.table_client,
        field_names=[schema.CLIENT_UID, schema.CLIENT_NAME, schema.CLIENT_REFERRAL_LINK],
    ):
        uid = to_uid(record.fields.get(schema.CLIENT_UID))
        if not uid:
            continue
        links = record.fields.get(schema.CLIENT_REFERRAL_LINK)
        ids = links.get("link_record_ids", []) if isinstance(links, dict) else []
        existing_by_uid[uid] = (
            record.record_id,
            extract_text(record.fields.get(schema.CLIENT_NAME)),
            list(ids),
        )

    dir_by_name, dir_by_tokens = load_directory(bitable)
    if not dir_by_name:
        print(f"  ⚠️ Base 里没有「{DIRECTORY_TABLE}」表，只能用 ECAS 表自带的 UID")

    # ---------- 逐个判定 ----------
    plans: list[Plan] = []
    skipped: list[tuple[str, str]] = []
    conflicts: list[str] = []
    claimed: dict[str, str] = {}

    wanted = {norm(name) for name in args.client}
    if wanted:
        missing = wanted - set(ecas)
        if missing:
            print(f"\n⚠️ ECAS 表里没有这些客户：{sorted(missing)}")
        ecas = {k: v for k, v in ecas.items() if k in wanted}
        print(f"按 --client 筛剩 {len(ecas)} 个")

    for key, row in sorted(ecas.items()):
        client, referrer = row["client"], row["referrer"]

        ref = ref_by_name.get(norm(referrer))
        how = ""
        if ref is None:
            ref = ref_by_tokens.get(tokens(referrer))
            how = "（靠姓名顛倒/标点才对上）"
        if ref is None:
            skipped.append((client, f"渠道表里找不到介绍人「{referrer}」"))
            continue

        uid, source = row["uid"], "ECAS 表自带"
        if not uid:
            hits = dir_by_name.get(key) or set()
            source = "Client Directory 名字命中"
            if not hits:
                hits = dir_by_tokens.get(tokens(client)) or set()
                source = "Client Directory 靠姓名顛倒/标点命中"
            if len(hits) == 1:
                uid = next(iter(hits))
            elif len(hits) > 1:
                skipped.append((client, f"Client Directory 里这个名字对着 {len(hits)} 个 UID"))
                continue
            else:
                skipped.append((client, "查不到 UID —— 没有 UID 登记了也算不到交易佣金"))
                continue

        # 损坏判定放在解析之后，**不分来源**。早先只查了 ECAS 表自带的那批，漏了从
        # Client Directory 捞回来的 —— 而那张表是忠实镜像，**故意**留着已经被 Excel
        # 抹过低位的值（见 import_client_directory.py 的说明）。于是一个坏 UID 会经由
        # 名录绕过检查写进客户表。坏 UID 比没有 UID 更糟：没有的看得出来，坏的看不出来，
        # 只会静默匹配到别的客户或永远匹配不上。
        reason = reg._damaged_uid_reason(uid)
        if reason:
            skipped.append((client, f"UID {uid} {reason}（来源：{source}）—— 要先拿到正确的值"))
            continue

        if uid in existing_by_uid:
            _, existing_name, links = existing_by_uid[uid]
            if ref[0] in links:
                skipped.append((client, f"客户表里已经登记过，且已挂在 {ref[1]}"))
            elif links:
                conflicts.append(
                    f"{client}（UID {uid}）客户表里挂的是别的渠道，ECAS 说是 {ref[1]} —— 没动"
                )
            else:
                conflicts.append(f"{client}（UID {uid}）客户表里有这条但没挂渠道 —— 请人工挂上")
            continue

        if uid in claimed:
            conflicts.append(f"{client} 和 {claimed[uid]} 会配到同一个 UID {uid} —— 都没登记")
            continue
        claimed[uid] = client

        plans.append(Plan(uid, client, row["sales"], ref[0], ref[1], source, how))

    # ---------- 输出 ----------
    if skipped:
        print(f"\n跳过的 {len(skipped)} 个：")
        for name, why in skipped:
            print(f"  {name[:36]:<38} {why}")
    if conflicts:
        print(f"\n⚠️ 要人工判断的 {len(conflicts)} 个：")
        for line in conflicts:
            print(f"  {line}")

    if not plans:
        print("\n没有可以安全登记的客户。")
        return 0

    print(f"\n准备登记的 {len(plans)} 个：")
    for p in plans:
        print(f"  {p.client_name[:34]:<36} -> {p.referral_label[:34]:<36}{p.referral_how}")
        print(f"      UID {p.uid}（{p.uid_source}）  负责销售 {p.sales}")

    if not args.apply:
        print(
            "\n预演：没有写 Base。\n"
            "要真的登记某一个客户，先确认他符合 PI 资格，"
            '然后：--client "客户名" --apply'
        )
        return 0

    written = 0
    for p in plans:
        fields = {
            schema.CLIENT_UID: p.uid,
            schema.CLIENT_NAME: p.client_name,
            schema.CLIENT_REFERRAL_LINK: [p.referral_record_id],
            schema.CLIENT_SALES_NAME: p.sales,
        }
        created = bitable.create_record(settings.table_client, fields)
        back = to_uid(
            bitable.get_record(settings.table_client, created.record_id).fields.get(
                schema.CLIENT_UID
            )
        )
        if back != p.uid:
            print(
                f"\n「{p.client_name}」写回读到的 UID 不对（{back!r}，期望 {p.uid}）—— 停下来。",
                file=sys.stderr,
            )
            return 1
        written += 1
        print(f"  ✓ {p.client_name} -> {p.referral_label}")

    print(f"\n登记 {written} 个客户。")
    print(
        "\n下一步（必须做，否则看板的历史行还是挂不上这些客户）："
        "\n  uv run python scripts/import_daily_board.py"
        ' --file "$(ls -t attachments/OTC组销售明细_*.xlsx | head -1)"'
        "\n  uv run python scripts/verify_commission.py"
        "\n  uv run python -m crm_basebot.jobs.reconcile --all-periods"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
