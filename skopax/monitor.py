import json
import logging

from skopax.contract import ReadError
from skopax.errkind import kind
from skopax.messages import msg
from skopax.redact import redact_text
from skopax.remote import HttpError

log = logging.getLogger(__name__)
ERROR_LIMIT = 200
REFUSALS = {401: 'connection.rejected', 403: 'connection.forbidden'}


def rules_of(raw: str, defaults: dict) -> dict:
    try:
        given = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        log.warning('monitor rules are not JSON, using the defaults: %s', raw[:ERROR_LIMIT])
        return dict(defaults)
    if not isinstance(given, dict):
        return dict(defaults)
    out = dict(defaults)
    for k, v in given.items():
        if k in defaults and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = v
    return out


def below_both(now: float, d1: float, d7: float, share: float, floor: float) -> bool:
    return min(d1, d7) >= floor and now < share * d1 and now < share * d7


def above(now: float, base: float, times: float, gap: float, floor: float) -> bool:
    return now >= floor and now > max(times * base, base + gap)


def _clean(text: object) -> str:
    return redact_text(text, ERROR_LIMIT)


def refusal(e: BaseException) -> str | None:
    code = e.code if isinstance(e, HttpError) else None
    return msg(REFUSALS[code], status=code) if code in REFUSALS else None


def failed(e: BaseException) -> str:
    if isinstance(e, ReadError):
        return f'error: {_clean(e.message)}'
    said = refusal(e)
    return f'error: {kind(e)}' if said is None else f'error: {said}'


def pct(now: float, base: float) -> str:
    return f'{round(100 * now / base)} %' if base else '\u2013'
