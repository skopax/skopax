import datetime as dt
from datetime import tzinfo

WEEK = dt.timedelta(days=7)
WORKDAYS = dt.timedelta(days=5)


def _midnight(day: dt.date, zone: tzinfo) -> float:
    return dt.datetime(day.year, day.month, day.day, tzinfo=zone).timestamp()


def _monday(t: float, zone: tzinfo) -> dt.date:
    day = dt.datetime.fromtimestamp(t, zone).date()
    return day - dt.timedelta(days=day.weekday())


def _week(monday: dt.date, start: float, end: float, zone: tzinfo) -> float:
    return max(0, min(_midnight(monday + WORKDAYS, zone), end) - max(_midnight(monday, zone), start))


def _whole(first: dt.date, last: dt.date, zone: tzinfo) -> float:
    total = 0
    monday = first
    while monday < last:
        total += _midnight(monday + WORKDAYS, zone) - _midnight(monday, zone)
        monday += WEEK
    return total


def business_elapsed(start: float, end: float, zone: tzinfo) -> float:
    if end <= start:
        return 0
    first, last = _monday(start, zone), _monday(end, zone)
    if first == last:
        return _week(first, start, end, zone)
    return _week(first, start, end, zone) + _whole(first + WEEK, last, zone) + _week(last, start, end, zone)


def elapsed(start: float, end: float, zone: tzinfo | None) -> float:
    return end - start if zone is None else business_elapsed(start, end, zone)


def business_add(start: float, seconds: float, zone: tzinfo | None) -> float:
    if zone is None:
        return start + seconds
    t, left = start, max(seconds, 0)
    while True:
        monday = _monday(t, zone)
        week_end = _midnight(monday + WORKDAYS, zone)
        if t >= week_end:
            t = _midnight(monday + WEEK, zone)
            continue
        if t + left <= week_end:
            return t + left
        left -= week_end - t
        t = _midnight(monday + WEEK, zone)
