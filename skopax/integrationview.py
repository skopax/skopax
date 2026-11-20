import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from skopax.redact import BEARER, JWT, REDACTED, SECRET_WORDS, secrets_masked

PLAIN_KINDS = ('choice', 'bool', 'int', 'instance')
READ_KINDS = PLAIN_KINDS + ('text', 'url', 'regex')
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
ROW_KEYS = ('id', 'name', 'version', 'category', 'description', 'author', 'licence', 'origin', 'state', 'reason', 'enabled', 'icon', 'edition', 'companyKnowledge', 'knowledgeAlso',
            'setup', 'checks', 'tools', 'panels', 'fields', 'instances', 'health', 'rules')
FIELD_KEYS = ('key', 'label', 'kind', 'required', 'default', 'help', 'choices', 'choiceLabels', 'showIf', 'group')
CHECK_KEYS = ('id', 'label', 'once')
TOOL_KEYS = ('name', 'kind')
PANEL_KEYS = ('tab', 'label', 'group')
SETUP_KEYS = ('steps', 'docs', 'docsLabel')
HEALTH_KEYS = ('state', 'text', 'at')
INSTANCE_HEALTH_KEYS = ('name', 'state', 'text', 'at')
INSTANCE_KEYS = ('name', 'primary', 'enabled', 'source', 'problem', 'values', 'secrets', 'offChecks')


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


def shown(f: Mapping, value: str) -> str | None:
    kind = f.get('kind')
    if kind not in READ_KINDS or (kind not in PLAIN_KINDS and secret_named(f)):
        return None
    if kind == 'url':
        return bare_url(value)
    return value if kind in PLAIN_KINDS else masked(value)


def picked(source: Mapping | None, keys: Iterable[str]) -> dict:
    source = source or {}
    return {k: source[k] for k in keys if k in source}


def reader_field(f: Mapping) -> dict:
    out = picked(f, FIELD_KEYS)
    if out.get('kind') not in READ_KINDS or secret_named(f):
        out['default'] = ''
    elif out.get('kind') == 'url' and out.get('default'):
        out['default'] = bare_url(out['default'])
    return out


def reader_instance(inst: Mapping, fields: Iterable[Mapping]) -> dict:
    secrets = {k: {'set': bool((v or {}).get('set'))} for k, v in (inst.get('secrets') or {}).items()}
    values, given = {}, inst.get('values') or {}
    for f in fields:
        key = f.get('key')
        if key not in given or key in secrets:
            continue
        raw = str(given[key])
        out = shown(f, raw)
        if out is None or (out == '' and raw.strip()):
            secrets[key] = {'set': given[key] not in (None, '')}
        else:
            values[key] = out
    out = picked(inst, INSTANCE_KEYS)
    out.update(problem=masked(inst.get('problem')), values=values, secrets=secrets, offChecks=list(inst.get('offChecks') or []))
    return {k: out.get(k) for k in INSTANCE_KEYS}


def reader_health(health: Mapping | None) -> dict | None:
    if not health:
        return None
    out = picked(health, HEALTH_KEYS)
    out['text'] = masked(out.get('text'))
    out['instances'] = [{**picked(h, INSTANCE_HEALTH_KEYS), 'text': masked(h.get('text'))} for h in health.get('instances') or []]
    return out


def reader_row(row: Mapping, rules: list[dict]) -> dict:
    out = picked(row, ROW_KEYS)
    if 'reason' in out:
        out['reason'] = masked(out['reason'])
    fields = list(row.get('fields') or [])
    if 'fields' in out:
        out['fields'] = [reader_field(f) for f in fields]
    for key, keys in (('checks', CHECK_KEYS), ('tools', TOOL_KEYS), ('panels', PANEL_KEYS)):
        if key in out:
            out[key] = [picked(x, keys) for x in out[key] or []]
    if out.get('setup'):
        out['setup'] = picked(out['setup'], SETUP_KEYS)
    if 'instances' in out:
        out['instances'] = [reader_instance(i, fields) for i in out['instances'] or []]
    if 'health' in out:
        out['health'] = reader_health(out['health'])
    if 'rules' in out:
        out['rules'] = rules
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
