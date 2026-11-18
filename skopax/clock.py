import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from skopax.errkind import Refusal

STAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

_zone = ZoneInfo('UTC')


def _load(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError, KeyError) as e:
        raise Refusal(f'unknown timezone {name!r}') from e


def set_zone(name: str) -> None:
    global _zone
    _zone = _load(name)


def check(name: str) -> None:
    _load(name)


def zone() -> ZoneInfo:
    return _zone


def local(t: float | dt.datetime) -> dt.datetime:
    if isinstance(t, dt.datetime):
        return t.astimezone(_zone)
    return dt.datetime.fromtimestamp(t, _zone)


def label(t: float | dt.datetime | None = None) -> str:
    d = dt.datetime.now(_zone) if t is None else local(t)
    return d.tzname() or str(_zone)


def hm(t: float | dt.datetime) -> str:
    return local(t).strftime('%H:%M')


def date(t: float | dt.datetime, hm: bool = True) -> str:
    d = local(t)
    return f'{d.day} {MONTHS[d.month - 1]}' + (d.strftime(' %H:%M') if hm else '')


def stamp(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_stamp(value) -> float | None:
    if not isinstance(value, str) or not STAMP.match(value):
        return None
    try:
        return dt.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def day(t: float) -> str:
    return local(t).date().isoformat()


def midnight(now: float) -> float:
    d = local(now)
    return dt.datetime(d.year, d.month, d.day, tzinfo=_zone).timestamp()
