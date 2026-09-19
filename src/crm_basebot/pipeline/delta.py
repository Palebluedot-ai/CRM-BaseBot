"""算「哪些交易日是看板还没有的」—— 纯函数，零 IO。

日常跑的是增量：导出里只会往后加一天。但**历史被修订时增量看不见**（导出改了旧日期的
金额，看板不会跟进），所以 ``refresh`` 允许点名重导某一天，大范围回填仍然走全量脚本。
"""

from __future__ import annotations

from datetime import date


def compute_new_dates(
    source_dates: set[date],
    existing: set[date],
    *,
    refresh: set[date] | None = None,
    since: date | None = None,
) -> list[date]:
    """导出里有、看板里没有的交易日，加上 ``refresh`` 里被点名的那些。

    ``refresh`` 只对导出里真有数据的日子生效 —— 点名一个导出里没有的日期，
    不该在 Base 上凭空删掉那天的历史行。

    注意 ``source_dates`` 应当是**要的那个站点**的日期（不是所有站点）：导出覆盖几个月，
    其他站点几乎每天交易，拿全部站点当基准会把一堆别的站点的日子算成「新增」。
    「某天只剩别的站点」那种情况由 ``stale_board_dates`` 负责，两者判据不同。
    """
    new = {day for day in source_dates if day not in existing}
    if refresh:
        new |= refresh & source_dates
    if since is not None:
        new = {day for day in new if day >= since}
    return sorted(new)


def stale_board_dates(
    existing: set[date],
    covered_dates: set[date],
    kept_dates: set[date],
) -> set[date]:
    """看板里有数据、但导出说这天要的站点已经没有记录了 —— 需要清账的日期。

    场景：某天原来有新加坡站的记录，后来上游更正，这天只剩香港站的行。这时候
    这天**不是「新增」**（看板里有），所以增量不会碰它 —— 而它的旧记录会一直留在
    看板上继续参与佣金计算，属于最坏的一类错误：没人会发现。

    判据只用集合运算，三个输入都是日期集合：

        covered  导出覆盖到的日期（所有站点）
        kept     导出里**要的站点**有记录的日期
        existing 看板里有记录的日期

    交集 ``existing ∩ covered - kept`` 就是要清的天。导出没覆盖的日期不动 ——
    导出窗口之外的老数据不该被它删掉。
    """
    return {day for day in existing if day in covered_dates and day not in kept_dates}
