"""业务日期和 Bitable 日期字段之间的换算。

Bitable 日期字段存的是 UTC 毫秒时间戳，界面按看的人所在时区显示。看板里的「交易日期」
是新加坡的日历日：写进 Base 要取业务时区那一天的零点，读回来也按业务时区取日期。
两边都按 UTC 算的话，界面里手工填的日期（新加坡零点）会被算成前一天。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from crm_basebot.domain.dates import date_to_ms, ms_to_date

SGT = ZoneInfo("Asia/Singapore")


def test_写入的是业务时区那天的零点():
    ms = date_to_ms(date(2026, 9, 10), tz=SGT)

    assert ms == int(datetime(2026, 9, 10, tzinfo=SGT).timestamp() * 1000)
    # 同一时刻按 UTC 看还是 9 月 9 日 16:00，这就是按 UTC 取日期会差一天的原因
    assert datetime.fromtimestamp(ms / 1000, tz=UTC).date() == date(2026, 9, 9)


def test_读回按业务时区取日期():
    ms = date_to_ms(date(2026, 9, 10), tz=SGT)
    assert ms_to_date(ms, tz=SGT) == date(2026, 9, 10)


def test_界面里手工填的新加坡零点也认():
    """Base 界面按看的人所在时区显示，新加坡的人填 9 月 10 日存的就是新加坡零点。"""
    hand_entered = int(datetime(2026, 9, 10, 0, 0, tzinfo=SGT).timestamp() * 1000)
    assert ms_to_date(hand_entered, tz=SGT) == date(2026, 9, 10)


def test_月初早上按业务时区已经是新的一天():
    early = int(datetime(2026, 10, 1, 7, 0, tzinfo=SGT).timestamp() * 1000)
    assert ms_to_date(early, tz=SGT) == date(2026, 10, 1)  # 按 UTC 还是 9 月 30 日


def test_换时区结果跟着变():
    ms = date_to_ms(date(2026, 9, 10), tz=SGT)
    assert ms_to_date(ms, tz=ZoneInfo("America/New_York")) == date(2026, 9, 9)
