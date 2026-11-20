import datetime as dt
from datetime import tzinfo

WEEK = dt.timedelta(days=7)
WORKDAYS = dt.timedelta(days=5)


def _midnight(day: dt.date, zone: tzinfo) -> float:
    return dt.datetime(day.year, day.month, day.day, tzinfo=zone).timestamp()


def business_elapsed(start: float, end: float, zone: tzinfo) -> float:
    if end <= start:
        return 0
    first = dt.datetime.fromtimestamp(start, zone).date()
    monday = first - dt.timedelta(days=first.weekday())
    total = 0
    opens = _midnight(monday, zone)
    while opens < end:
        closes = _midnight(monday + WORKDAYS, zone)
        total += max(0, min(closes, end) - max(opens, start))
        monday += WEEK
        opens = _midnight(monday, zone)
    return total


def elapsed(start: float, end: float, zone: tzinfo | None) -> float:
    return end - start if zone is None else business_elapsed(start, end, zone)
