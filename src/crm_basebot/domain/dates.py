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


def date_to_ms(day: date, *, tz: tzinfo) -> int:
    """日历日 -> 该日在 ``tz`` 的零点，Bitable 日期字段要的毫秒时间戳。"""
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp() * 1000)


def ms_to_date(ms: int | float, *, tz: tzinfo) -> date:
    """毫秒时间戳 -> 在 ``tz`` 里的日历日。"""
    return datetime.fromtimestamp(float(ms) / 1000, tz=tz).date()
