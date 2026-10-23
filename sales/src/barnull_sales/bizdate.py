"""営業日（6:00〜翌6:00）と、年が省略された日付の補完。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import jpholiday

WEEKDAYS = "月火水木金土日"


def business_date(dt: datetime, tz: ZoneInfo, day_start: time) -> date:
    """例: 10/11 02:30（JST）→ 10/10。dt はタイムゾーン付きであること。"""
    local = dt.astimezone(tz)
    d = local.date()
    if local.time() < day_start:
        d -= timedelta(days=1)
    return d


def business_window(d: date, tz: ZoneInfo, day_start: time) -> tuple[datetime, datetime]:
    """営業日 d の範囲（d の day_start 〜 翌日の day_start）。"""
    start = datetime.combine(d, day_start, tzinfo=tz)
    return start, start + timedelta(days=1)


def local_date(iso: str, tz: ZoneInfo) -> date:
    """ISO の日時を、その地域の日付にする。"""
    return datetime.fromisoformat(iso).astimezone(tz).date()


def hm(dt: datetime, tz: ZoneInfo) -> str:
    """「10/11 02:14」"""
    return dt.astimezone(tz).strftime("%m/%d %H:%M")


def resolve_month_day(month: int, day: int, sent_at: datetime, tz: ZoneInfo, day_start: time) -> date | None:
    """年の無い月日を、送信日時の営業日から見て最も近い過去（当日を含む）の日付にする。存在しない日付なら None。"""
    ref = business_date(sent_at, tz, day_start)
    for year in (ref.year, ref.year - 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d <= ref:
            return d
    return None


def label(d: date) -> str:
    """10/10（土）"""
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def is_bank_holiday(d: date) -> bool:
    return d.weekday() >= 5 or jpholiday.is_holiday(d) or (d.month, d.day) in ((12, 31), (1, 1), (1, 2), (1, 3))


def add_bank_days(d: date, n: int) -> date:
    """銀行営業日で n 日後（土日祝・年末年始を飛ばす）。"""
    while n > 0:
        d += timedelta(days=1)
        if not is_bank_holiday(d):
            n -= 1
    return d
