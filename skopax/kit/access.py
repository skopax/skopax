from collections.abc import Iterable

from skopax.contract import anomaly
from skopax.messages import msg

REFUSED = (401, 403)
INVALID = 401
FORBIDDEN = 403
SIG = 'no-access'
CREDENTIAL = 'token'


def denied(code: object) -> bool:
    return isinstance(code, int) and not isinstance(code, bool) and code in REFUSED


def ran_key(check: str, part: str) -> str:
    return f'{check}:access:{part}'


def lacks(permission: str = '') -> str:
    return permission or msg('connection.permission')


def _form(key: str, plural: bool) -> str:
    return f'{key}_plural' if plural else key


def text(plugin: str, part: str, permission: str = '', credential: str = CREDENTIAL, status: int = 403, plural: bool = False) -> str:
    if not credential:
        return msg('connection.no_access_open', plugin=plugin, part=part)
    key = _form('connection.no_access_invalid' if status == INVALID else 'connection.no_access', plural)
    return msg(key, plugin=plugin, part=part, credential=credential, permission=lacks(permission))


def why(part: str, permission: str = '', credential: str = CREDENTIAL, status: int = 403, plural: bool = False) -> str:
    if not credential:
        return msg('connection.no_access_why_open', part=part, status=status)
    return msg(_form('connection.no_access_why', plural), part=part, status=status, credential=credential, permission=lacks(permission))


def tested(plugin: str, part: str, status: object, permission: str = '', credential: str = CREDENTIAL) -> str | None:
    if not denied(status):
        return None
    if not credential:
        return msg('connection.no_credentials', status=status) if status == INVALID else text(plugin, part, permission, credential, status)
    return text(plugin, part, permission, credential, status) if status == FORBIDDEN else None


def card(check: str, plugin: str, part: str, permission: str = '', status: int = 403, credential: str = CREDENTIAL, *, name: str = '', plural: bool = False,
         what: str = '') -> dict:
    shown = name or part
    label = text(plugin, shown, permission, credential, status, plural)
    found = anomaly(check, SIG, '-', check, part, 'warn', 1, None, None, '', label, what or why(shown, permission, credential, status, plural), short=f'WARN {label}')
    found['ranKey'] = ran_key(check, part)
    return found


def status(base: str, parts: Iterable[str]) -> str:
    named = list(dict.fromkeys(parts))
    return f"{base} \u00b7 no access to {', '.join(named)}" if named else base
