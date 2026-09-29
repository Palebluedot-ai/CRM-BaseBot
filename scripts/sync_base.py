#!/usr/bin/env python
"""把 Base 的结构对齐到目标结构。只增，不删；不改（我们维护的公式列除外）。

    uv run python scripts/sync_base.py            # 预演，打印将要做什么
    uv run python scripts/sync_base.py --apply    # 真的执行

安全边界：缺的表建、缺的字段加、**类型不对的只报告不动手**（改类型可能毁数据）、
多出来的表和字段完全不碰。唯一会「改」的是我们自己维护的公式列：公式变了就换成新的
（值是算出来的，换了不丢数据），例如 2026-09-26「本笔佣金」加上了 AI 规则。

逻辑住在 ``crm_basebot.structure``（那边也负责说清「为什么」）。这个文件只剩
「解析命令行 + 打印 + 建完做一次公式自检」——同样的结构逻辑迁移时也要用，所以它必须
是模块里的函数，而不是脚本里的一段代码。

**平台不校验公式表达式**：写错的公式照样建得出来，只是永远返回空值，所以 --apply
之后会拿真实记录做一次公式自检（见 ``_verify_formulas``）。

副产品是迁移能力：换掉 .env 里的凭证跑一次 --apply，结构就复刻到另一个 Base 了。
要连数据一起搬，用 ``scripts/migrate_base.py``。
"""

from __future__ import annotations

import argparse
import sys
from datetime import tzinfo
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.ai_status import eligibility_of  # noqa: E402
from crm_basebot.domain.commission import trade_counts  # noqa: E402
from crm_basebot.domain.dates import ms_to_date  # noqa: E402
from crm_basebot.lark.bitable import BitableClient  # noqa: E402
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.lark.values import extract_text, link_ids, to_number  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402
from crm_basebot.structure import (  # noqa: E402
    LINK_TARGETS,
    TARGET_TABLES,
    ensure_number_formats,
    ensure_structure,
    write_table_ids,
)
from crm_basebot.structure import build_field as _build_field  # noqa: E402

__all__ = [
    "LINK_TARGETS",
    "TARGET_TABLES",
    "_build_field",
    "_verify_ai_commission",
    "_verify_formulas",
    "main",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="幂等对齐 Base 结构")
    parser.add_argument("--apply", action="store_true", help="真的执行，默认只预演")
    parser.add_argument(
        "--env",
        default=".env",
        help="环境文件，默认 .env；--apply 时会把 6 个 table_id 写回这个文件",
    )
    args = parser.parse_args(argv)

    settings = load_settings(env_file=args.env)
    require_settings(settings, "LARK_BASE_APP_TOKEN")

    result = ensure_structure(
        settings=settings,
        bitable=BitableClient(settings.base_app_token),
        client=get_client(),
        apply=args.apply,
    )
    # 金额列显示成 20,000.00、笔数显示成 1,000（仪表盘的图沿用列的格式）。放在结构之后：
    # 刚建出来的列、刚换过公式的列这一步也照顾到。
    formats = ensure_number_formats(
        settings=settings,
        bitable=BitableClient(settings.base_app_token),
        client=get_client(),
        apply=args.apply,
    )
    result.plan.extend(formats.plan)
    result.warnings.extend(formats.warnings)

    if not result.changed and not result.warnings:
        print("结构已经对齐，没什么要做的。")
    else:
        if result.plan:
            print("\n计划执行：" if args.apply else "\n将要执行（预演）：")
            for item in result.plan:
                print(f"  · {item}")
        if result.warnings:
            print("\n需要你决定：")
            for item in result.warnings:
                print(f"  ! {item}")
        if not args.apply and result.plan:
            print("\n确认无误后加 --apply 真正执行。")

    if args.apply:
        written = write_table_ids(args.env, result.table_ids)
        print(f"\n已把 {len(written)} 个 table_id 写进 {args.env}：{'、'.join(written)}")
        print("  （不用手抄了；想看全部表名和 id 也可以跑 scripts/inspect_base.py）")
        _verify_formulas(
            BitableClient(settings.base_app_token),
            result.table_ids.get(schema.TABLE_DAILY_BOARD_NAME),
            tz=ZoneInfo(settings.business_timezone),
        )
        _verify_ai_commission(
            BitableClient(settings.base_app_token),
            result.table_ids.get(schema.TABLE_DAILY_BOARD_NAME),
            result.table_ids.get(schema.TABLE_CLIENT_NAME),
            tz=ZoneInfo(settings.business_timezone),
        )

    return 0


def _verify_formulas(
    bitable: BitableClient, table_id: str | None, *, tz: tzinfo, sample: int = 200
) -> None:
    """读回真实记录，确认看板上的公式真的在算、月份列也没错月。

    平台**不校验**公式表达式：写错的公式照样建得出来，接口照样回 code=0，只是那一列永远
    是空的（2026-09-18 实测）。所以「建好了」这一步光看返回值不算数，得拿记录核对：
    挂了「客户」关联却一行都反查不出「分佣比例」的，就是公式的问题。（以前看的是「本笔佣金」，
    那一列 2026-09-25 删了。）

    「月份」还要单独对一遍时区：公式里的 TEXT() 按**平台**时区算（实测 UTC+8），业务时区
    不是 UTC+8 时它会错月 —— 而错月不报任何错，只会在报表上把 8 月的钱算进 7 月。
    """
    if not table_id:
        return

    columns = [
        schema.BOARD_CLIENT_LINK,
        schema.BOARD_CLIENT_RATE,
        schema.BOARD_MONTH,
        schema.BOARD_ORDER_DATE,
    ]
    scanned = linked = computed = month_checked = month_bad = 0
    month_samples: list[str] = []

    for record in islice(bitable.iter_records(table_id, field_names=columns), sample):
        scanned += 1
        fields = record.fields

        raw_date = fields.get(schema.BOARD_ORDER_DATE)
        month = extract_text(fields.get(schema.BOARD_MONTH))
        if isinstance(raw_date, int | float) and not isinstance(raw_date, bool) and month:
            month_checked += 1
            expected = ms_to_date(raw_date, tz=tz).strftime("%Y-%m")
            if month != expected:
                month_bad += 1
                if len(month_samples) < 3:
                    month_samples.append(f"{month}≠{expected}")

        # 空关联读回来是 {"link_record_ids": None}，是真的，不是 None —— 判真假会把它当成
        # 「挂上了」，于是每一行都报「已挂、算得出佣金」，看着一切正常（这个坑踩过）。
        if not link_ids(fields.get(schema.BOARD_CLIENT_LINK)):
            continue
        linked += 1
        if to_number(fields.get(schema.BOARD_CLIENT_RATE)) is not None:
            computed += 1

    print(f"\n公式自检（抽查前 {scanned} 行）：")
    if scanned == 0:
        print("  看板还没有数据，公式无从验证 —— 先跑一次 import_daily_board.py 再看。")
        return

    print(
        f"  挂了「{schema.BOARD_CLIENT_LINK}」关联的有 {linked} 行，"
        f"其中反查得出「{schema.BOARD_CLIENT_RATE}」的 {computed} 行。"
    )
    if linked and not computed:
        print(
            "  ! 挂了关联却一行都没算出来，公式多半没生效。"
            f"去 Base 里点开「{schema.BOARD_CLIENT_RATE}」那一列，看公式是不是变成了错误值。"
        )
    elif linked:
        print("  公式在算。")
    else:
        print(
            "  还没有行挂上关联，公式没法验证 —— 关联是 import_daily_board.py 写进去的"
            "（客户表为空、或者这些用户都没登记时，它也没得挂）。"
        )

    if not month_checked:
        return
    if month_bad:
        print(
            f"  ! 「{schema.BOARD_MONTH}」和业务时区对不上：抽查 {month_checked} 行里 "
            f"{month_bad} 行不符，例如 {month_samples}。"
            "公式里的 TEXT() 按平台时区算（实测 UTC+8），业务时区换成别的就会错月 —— "
            "改 .env 之后要跟着调整这一列的公式。"
        )
    else:
        print(f"  「{schema.BOARD_MONTH}」抽查 {month_checked} 行，和业务时区一致。")


def _verify_ai_commission(
    bitable: BitableClient,
    board_id: str | None,
    client_id: str | None,
    *,
    tz: tzinfo,
    show: int = 3,
) -> bool:
    """拿 Python 的算法（月结用的那套）逐行核对 Base 里的「AI佣金起算」「本笔佣金」。

    那两条公式是 domain/ai_status.py 的翻版，平台又不校验公式 —— 写错了只会静默出错数。
    所以每一行挂上渠道的交易都用 Python 再算一遍比对。全表扫，不抽样：AI 规则只影响
    9 月以后的行，抽前几百行可能一行都碰不到。返回是否全对。
    """
    if not board_id or not client_id:
        return True

    eligibility = {
        record.record_id: eligibility_of(record.fields, tz=tz)
        for record in bitable.iter_records(client_id)
    }
    columns = [
        schema.BOARD_ORDER_DATE,
        schema.BOARD_CLIENT_LINK,
        schema.BOARD_TOTAL_REVENUE,
        schema.BOARD_CLIENT_RATE,
        schema.BOARD_AI_GATE,
        schema.BOARD_ROW_COMMISSION,
    ]
    checked = bad = 0
    samples: list[str] = []
    for record in bitable.iter_records(board_id, field_names=columns):
        fields = record.fields
        linked = link_ids(fields.get(schema.BOARD_CLIENT_LINK))
        rate = to_number(fields.get(schema.BOARD_CLIENT_RATE))
        if not linked or rate is None or linked[0] not in eligibility:
            continue
        checked += 1
        ai = eligibility[linked[0]]
        revenue = to_number(fields.get(schema.BOARD_TOTAL_REVENUE)) or 0.0
        order_time = fields.get(schema.BOARD_ORDER_DATE)
        expected = revenue * rate / 100 if trade_counts(ai, order_time, tz=tz) else 0.0
        got = to_number(fields.get(schema.BOARD_ROW_COMMISSION))
        gate = to_number(fields.get(schema.BOARD_AI_GATE))
        if got is None or abs(got - expected) > 0.01 or gate != ai.gate_number():
            bad += 1
            if len(samples) < show:
                day = ms_to_date(order_time, tz=tz) if isinstance(order_time, int | float) else ""
                samples.append(
                    f"{day} 本笔佣金 {got}（应为 {expected:.2f}），"
                    f"AI佣金起算 {gate}（应为 {ai.gate_number()}）"
                )

    print(f"\nAI 规则自检（「{schema.BOARD_ROW_COMMISSION}」逐行和月结的算法对）：")
    if not checked:
        print("  还没有挂上渠道的交易，没得核对。")
        return True
    if bad:
        print(f"  ! 核对 {checked} 行，{bad} 行对不上。例如：")
        for line in samples:
            print(f"    {line}")
        print(
            "  刚改完公式时 Base 可能还在算：等一两分钟再跑一次 sync_base.py --apply"
            "（结构已经对齐，不会再改东西，只会再核对一次）。还不对就把这段截图发给开发。"
        )
        print("  钱不受影响：月结和机器人不读这一列。")
        return False
    print(f"  核对 {checked} 行，全对。")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
