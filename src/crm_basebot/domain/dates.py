"""业务日期和 Bitable 日期字段之间的换算。

Bitable 日期字段存的是 UTC 毫秒时间戳，界面上按看的人所在时区显示。看板 xlsx 里的
「交易日期」是新加坡的日历日，没有时分秒。写进 Base 时取**业务时区**（BUSINESS_TIMEZONE）
那一天的零点，读回来也按业务时区取日期，界面里看到的就是那一天 0:00。

两边都按 UTC 算的话：写进去的是 UTC 零点，界面显示成早上 8 点；而界面里手工填的
「9 月 10 日」是新加坡零点，按 UTC 取日期成了 9 月 9 日，导入脚本按日期先删后写
就删不掉它，表里留下两份。归月的理由一样，见 ``commission.period_of``。
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo
from zoneinfo import ZoneInfo

from ..config import Settings

# 没显式传时区时的默认值，直接取 Settings 里那个字段的默认（Asia/Singapore），
# 免得在这里再抄一遍字面量 —— 两份默认值迟早会不一致。生产路径（app.py）总是把
# .env 里的 BUSINESS_TIMEZONE 显式传进来；这个常量只服务于单测和一次性脚本，
# 它们不该为了一个默认值去凑齐 LARK_APP_ID 之类的环境变量才能实例化 Settings。
DEFAULT_BUSINESS_TIMEZONE = ZoneInfo(Settings.model_fields["business_timezone"].default)


def date_to_ms(day: date, *, tz: tzinfo) -> int:
    """日历日 -> 该日在 ``tz`` 的零点，Bitable 日期字段要的毫秒时间戳。"""
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp() * 1000)


def ms_to_date(ms: int | float, *, tz: tzinfo) -> date:
    """毫秒时间戳 -> 在 ``tz`` 里的日历日。"""
    return datetime.fromtimestamp(float(ms) / 1000, tz=tz).date()


def today_in(tz: tzinfo) -> date:
    """``tz`` 里的今天。

    登记日期这类「此刻」的日历日一律从这里取：``date.today()`` 拿的是机器所在时区的
    今天，服务器时区一变，记下来的日期就会差一天。
    """
    return datetime.now(tz).date()


def period_of_day(day: date) -> str:
    """日历日 -> 它所在的月份 ``YYYY-MM``。"""
    return f"{day.year:04d}-{day.month:02d}"


def months_ending(period: str, count: int) -> list[str]:
    """以 ``period``（``YYYY-MM``）为最后一个月，往回数 ``count`` 个月，**从早到晚**。

    「近三个月」在两张卡上是同一个意思：详情卡以本月为止，佣金查询以选中的月份为止，
    都是含那个月在内往回三个月。两处共用这一个函数，免得一边按新到旧、一边按旧到新，
    或者一边含本月、一边不含。

    ``period`` 不是合法的 ``YYYY-MM``（月份不在 01–12）时返回空列表：调用方都先校验过，
    这里不再抛。
    """
    if len(period) != 7 or period[4] != "-" or not (period[:4] + period[5:]).isdigit():
        return []
    year, month = int(period[:4]), int(period[5:])
    if not 1 <= month <= 12 or count <= 0:
        return []
    months: list[str] = []
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    months.reverse()
    return months
