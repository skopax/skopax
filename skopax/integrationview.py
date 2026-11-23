import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from skopax.redact import BEARER, JWT, REDACTED, SECRET_WORDS, secrets_masked

WEB_SCHEMES = ('http', 'https')
SECRET_NAME = re.compile(rf'(?i)(?:{SECRET_WORDS}|header[ _-]?value|web[ _-]?hook|hook|cookie|dsn|signature|bearer|session)')
URL_IN_TEXT = re.compile(r'[a-z][a-z0-9+.-]*://[^\s"\'<>]+', re.I)
BASIC = re.compile(r'(?i)\bBasic\s+[A-Za-z0-9+/=._-]{4,}')
KEYED = re.compile(r'(?i)\b(authorization|cookie|session|sig|signature|token|api_key|apikey)(\s*[:=]\s*|\s+)("[^"]*"|\'[^\']*\'|[^\s,;]+)')
TOKEN_SHAPED = re.compile(r'\b(?:xox[abprse]-[A-Za-z0-9-]+|xapp-[A-Za-z0-9-]+|gh[pousr]_[A-Za-z0-9_]{4,}|github_pat_[A-Za-z0-9_]{4,}|glpat-[A-Za-z0-9_-]{4,}|gl(?:dt|ptt|rt|soat|cbt)-[A-Za-z0-9_-]{4,}'
                          r'|sk-[A-Za-z0-9_-]{8,}|sk_live_[A-Za-z0-9]{4,}|rk_live_[A-Za-z0-9]{4,}|AKIA[0-9A-Z]{12,}|ASIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_-]{12,}|npm_[A-Za-z0-9]{8,}|glsa_[A-Za-z0-9_]{8,}'
                          r'|hf_[A-Za-z0-9]{8,}|dop_v1_[a-f0-9]{8,}|SG\.[A-Za-z0-9_-]{8,})')
HOST_PATH = re.compile(r'(?<![\w/:@.-])((?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d{1,5})?)/[^\s"\'<>]*', re.I)
RUN_KEYS = ('state', 'reason', 'at', 'ranAt', 'series', 'read', 'skipped', 'noBaseline', 'firing', 'cards')
ROW_KEYS = ('id', 'name', 'category', 'description', 'origin', 'state', 'enabled', 'icon', 'edition', 'companyKnowledge', 'knowledgeAlso')
HEALTH_WORDS = {'ok': 'Connected', 'warn': 'Needs attention: an admin can see the details', 'error': 'Needs attention: an admin can see the details', 'pending': 'Checking', 'none': 'Off', 'off': 'Off'}
MCP_ID = 'mcp-tools'


def bare_url(url: str) -> str:
    try:
        parts = urlsplit(str(url).strip())
        port = parts.port
    except ValueError:
        return ''
    if parts.scheme.lower() not in WEB_SCHEMES or not parts.hostname:
        return ''
    host = f'[{parts.hostname}]' if ':' in parts.hostname else parts.hostname
    return f'{parts.scheme.lower()}://{host}' + (f':{port}' if port else '')


def masked(text) -> str | None:
    if text is None:
        return None
    plain = JWT.sub(REDACTED, BEARER.sub(REDACTED, BASIC.sub(REDACTED, secrets_masked(str(text)))))
    plain = TOKEN_SHAPED.sub(REDACTED, KEYED.sub(lambda m: m.group(1) + m.group(2) + REDACTED, plain))
    return HOST_PATH.sub(lambda m: m.group(1), URL_IN_TEXT.sub(lambda m: bare_url(m.group(0)), plain))


def secret_named(f: Mapping) -> bool:
    return bool(SECRET_NAME.search(str(f.get('key', ''))) or SECRET_NAME.search(str(f.get('label', ''))))


def picked(source: Mapping | None, keys: Iterable[str]) -> dict:
    source = source or {}
    return {k: source[k] for k in keys if k in source}


def held(inst: Mapping, key: str) -> str:
    value = (inst.get('values') or {}).get(key)
    return '' if value is None else str(value)


def knowing(row: Mapping, insts: list[Mapping]) -> bool:
    key = row.get('companyKnowledge')
    if row.get('id') == MCP_ID:
        return any(held(i, 'knowledge') == 'true' for i in insts)
    if key == 'always':
        return row.get('state') == 'ok'
    return bool(key) and any(held(i, key).strip() != '' for i in insts)


def knowledge_error(row: Mapping, insts: list[Mapping]) -> bool:
    health = row.get('health') or {}
    rows = [h for h in health.get('instances') or [] if isinstance(h, Mapping)]
    if row.get('id') == MCP_ID and rows:
        known = {i.get('name') for i in insts if held(i, 'knowledge') == 'true'}
        return any(h.get('state') == 'error' and h.get('name') in known for h in rows)
    if rows:
        return any(h.get('state') == 'error' for h in rows)
    return health.get('state') == 'error'


def reader_health(health: Mapping | None) -> dict | None:
    if not health:
        return None
    state = health.get('state')
    text = HEALTH_WORDS.get(state) or HEALTH_WORDS['none']
    return {'state': state, 'text': text, 'at': health.get('at')}


def reader_row(row: Mapping) -> dict:
    out = picked(row, ROW_KEYS)
    insts = [i for i in row.get('instances') or [] if isinstance(i, Mapping)]
    out.update(instanceCount=len(insts), instancesOn=sum(1 for i in insts if i.get('enabled') is True and not i.get('problem')),
               hasFields=bool(row.get('fields')), outside=not (row.get('fields') or row.get('checks') or row.get('panels')), knowing=knowing(row, insts),
               knowledgeError=knowledge_error(row, insts))
    if 'health' in row:
        out['health'] = reader_health(row['health'])
    return out


def reader_run(run: Mapping | None) -> dict | None:
    return None if not run else masked_tree(picked(run, RUN_KEYS))


def masked_tree(value):
    if isinstance(value, str):
        return masked(value)
    if isinstance(value, dict):
        return {k: masked_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [masked_tree(v) for v in value]
    return value
