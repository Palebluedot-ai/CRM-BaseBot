"""给人看的输出。全部只打印，不做任何判断 —— 判断在别处，这里只负责措辞。

单独一个文件是为了让 pipeline 的其余部分保持「纯逻辑」：编排与解析里一行 print 都没有，
测试也就不需要去断言 stdout。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from ..domain import schema
from ..lark.bitable import MAX_BATCH_SIZE
from .daily import DailyPlan, DailyResult
from .export import BoardRow


def print_summary(rows: list[BoardRow]) -> None:
    """按月列行数和天数。几个月的数据按天列会刷出几百行，看不出东西。"""
    rows_by_month: dict[str, int] = defaultdict(int)
    days_by_month: dict[str, set[date]] = defaultdict(set)
    for row in rows:
        month = row.order_date.strftime("%Y-%m")
        rows_by_month[month] += 1
        days_by_month[month].add(row.order_date)

    days = {row.order_date for row in rows}
    print(f"\n{len(rows)} 行，{min(days)} 到 {max(days)}，覆盖 {len(days)} 天：")
    for month in sorted(rows_by_month):
        print(f"  {month}  {rows_by_month[month]:>6} 行  {len(days_by_month[month]):>3} 天")

    batches = -(-len(rows) // MAX_BATCH_SIZE)
    print(f"写入每批 {MAX_BATCH_SIZE} 条，约 {batches} 次调用；删除旧记录另算。")


def print_link_summary(rows: list[BoardRow], links: dict[str, str]) -> None:
    """挂上「客户」关联的行数。

    挂不上的行，Base 里那几列佣金公式会是空的 —— 那不是 bug，是「这个用户没登记在任何
    渠道下」。把数量说出来，省得对着空列猜是公式坏了还是本来就没归属。
    """
    uids = {str(row.fields.get(schema.BOARD_CLIENT_UID) or "") for row in rows}
    uids.discard("")
    matched_uids = uids & set(links)
    matched_rows = sum(
        1 for row in rows if str(row.fields.get(schema.BOARD_CLIENT_UID) or "") in links
    )
    print(
        f"客户关联：{matched_rows}/{len(rows)} 行挂上了（涉及 {len(matched_uids)} 个用户）；"
        f"{len(uids) - len(matched_uids)} 个用户不在客户表里，佣金那几列对它们是空的。"
    )


def print_plan(plan: DailyPlan, *, from_mail: bool = False) -> None:
    """这次会导什么。日期在前、行数在后 —— 人核对的是「哪几天」。"""
    origin = "邮箱附件" if from_mail else "本地文件"
    print(f"\n导出文件（{origin}）：{plan.xlsx_path}")
    if plan.latest_source is not None:
        print(
            f"新加坡站 {plan.kept_rows} 行，最新交易日 {plan.latest_source}"
            f"（原始 {plan.source_rows} 行）"
        )
    print(
        f"看板已有 {plan.existing_days} 天"
        + (f"，最新到 {plan.latest_existing}" if plan.latest_existing else "（表是空的）")
    )

    if not plan.has_work:
        print("\n没有新增交易日 —— 导出里每一天看板都已经有了。Base 不会改动。")
        return

    print(f"\n要处理 {len(plan.new_dates)} 天（整天替换）：")
    stale = set(plan.stale_dates)
    for day in plan.new_dates:
        rows = plan.rows_by_date.get(day, 0)
        if day in stale:
            note = "   ← 导出里已无新加坡站记录，清掉看板这天的旧记录"
        elif rows == 0:
            note = "   ← 导出里这天没有新加坡站的行"
        else:
            note = ""
        print(f"  {day}  {rows:>5} 行{note}")
    print(f"\n将整天替换这 {len(plan.new_dates)} 天，共写入 {plan.rows_to_import} 行。")


def print_result(result: DailyResult) -> None:
    if not result.plan.has_work:
        return
    print(f"删除 {result.deleted} 条，写入 {result.written} 条。")
    if result.written:
        print(
            f"客户关联：{result.linked_rows} 行挂上了；"
            f"{result.unregistered_users} 个用户不在客户表里，佣金那几列对它们是空的。"
        )
