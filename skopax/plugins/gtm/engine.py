import datetime as dt
import re
from collections.abc import Callable
from urllib.parse import quote

from skopax import clock
from skopax.contract import PassResult, ReadError, Sweep, anomaly, iso
from skopax.kit.google import GoogleAuth, GoogleError, access_card, access_key
from skopax.redact import redact

CHECK = 'gtm'
NAME = 'Google Tag Manager'
PART = 'container'
DOT = ' \u00b7 '
GTM_API = 'https://tagmanager.googleapis.com/tagmanager/v2'
GTM_UI = 'https://tagmanager.google.com'
SCOPES = ('https://www.googleapis.com/auth/tagmanager.readonly',)
NEEDS = 'Read access to the GTM container'
NO_ACCESS = 'the service account has no Read access to this GTM container; add it in Tag Manager under Admin, User Management, with Read'
BAD_CONTAINER = 'container must be a public GTM container id such as GTM-ABC1234'
CONTAINER = re.compile(r'GTM-[A-Z0-9]{4,12}')
PATH = re.compile(r'accounts/\d{1,20}/containers/\d{1,20}')
VERSION = re.compile(r'\d{1,10}')
OPS = ('live', 'versions', 'version', 'diff')
KINDS = (('tag', 'tagId', 'tags'), ('trigger', 'triggerId', 'triggers'), ('variable', 'variableId', 'variables'))
NAME_LIMIT = 120
TEXT_LIMIT = 300
CHANGES_LIMIT = 600
LIST_LIMIT = 40
VERSIONS = 10
PUBLISHES_KEPT = 30
MAX_PAGES = 10
CARD_SECONDS = 86400
NOTE = 'GTM keeps no publish time or author in its API; the gtm check records when a new live version appears'


class GtmError(ReadError):
    def __init__(self, status: int, message: str):
        super().__init__(status, message)
        self.reason = message


def hidden(container: str) -> GtmError:
    return GtmError(404, f'no GTM container {container} is visible to the service account (add it in Tag Manager under Admin, User Management, with Read)')


def pages(fetch: Callable[[str], dict], field: str) -> list[dict]:
    out, token = [], ''
    for _ in range(MAX_PAGES):
        got = fetch(f'?pageToken={quote(token, safe="")}' if token else '')
        out += [x for x in got.get(field) or [] if isinstance(x, dict)]
        token = got.get('nextPageToken')
        if not isinstance(token, str) or not token:
            break
    return out


def public_id(text: object) -> str:
    return str(text or '').strip().upper()


class Gtm:
    def __init__(self, auth: GoogleAuth, container: str = '', path: str | None = None):
        self.auth = auth
        self.container = public_id(container)
        self.path = path if isinstance(path, str) and PATH.fullmatch(path) else None
        self.listed = False

    @property
    def valid(self) -> bool:
        return bool(CONTAINER.fullmatch(self.container))

    @property
    def enabled(self) -> bool:
        return self.auth.enabled and self.valid

    def get(self, path: str, retry: bool = True) -> dict:
        return self.auth.call('GET', f'{GTM_API}/{path}', None, 'Tag Manager', NO_ACCESS, retry=retry, needs=NEEDS)

    def container_path(self, retry: bool = True) -> str:
        if self.path:
            return self.path
        for account in pages(lambda page: self.get(f'accounts{page}', retry), 'account'):
            if not isinstance(account.get('path'), str):
                continue
            for c in pages(lambda page, account=account: self.get(f"{account['path']}/containers{page}", retry), 'container'):
                if str(c.get('publicId') or '').upper() == self.container and isinstance(c.get('path'), str) and PATH.fullmatch(c['path']):
                    self.path, self.listed = c['path'], True
                    return self.path
        raise hidden(self.container)

    def read(self, tail: str, retry: bool = True) -> dict:
        try:
            return self.get(f'{self.container_path(retry)}/{tail}', retry)
        except GoogleError as e:
            if self.listed or e.status != 404:
                raise
        self.path = None
        return self.get(f'{self.container_path(retry)}/{tail}', retry)

    def live(self, retry: bool = True) -> dict:
        return self.read('versions:live', retry)

    def version(self, version_id: str) -> dict:
        return self.read(f'versions/{version_id}')

    def headers(self) -> list[dict]:
        return pages(lambda page: self.read(f'version_headers{page}'), 'containerVersionHeader')


def cut(value, limit: int = NAME_LIMIT) -> str:
    return redact(' '.join(str(value or '').split()))[:limit]


def stored_at(version: dict) -> str | None:
    try:
        ms = int(version.get('fingerprint') or 0)
    except (TypeError, ValueError):
        return None
    return iso(ms / 1000) if 10 ** 12 <= ms < 10 ** 14 else None


def summary(version: dict) -> dict:
    return {'id': str(version.get('containerVersionId') or ''), 'name': cut(version.get('name')), 'description': cut(version.get('description'), TEXT_LIMIT),
            'storedAt': stored_at(version), 'counts': {plural: len(version.get(kind) or []) for kind, _, plural in KINDS}}


def items(version: dict, kind: str, key: str) -> dict[str, dict]:
    return {str(x.get(key)): x for x in version.get(kind) or [] if isinstance(x, dict) and x.get(key)}


def diff(old: dict, new: dict) -> dict:
    out = {}
    for kind, key, plural in KINDS:
        a, b = items(old, kind, key), items(new, kind, key)
        added = [cut(b[i].get('name')) for i in b if i not in a]
        removed = [cut(a[i].get('name')) for i in a if i not in b]
        changed = [cut(b[i].get('name')) for i in b if i in a and b[i].get('fingerprint') != a[i].get('fingerprint')]
        part = {k: v[:LIST_LIMIT] for k, v in (('added', added), ('removed', removed), ('changed', changed)) if v}
        if part:
            out[plural] = part
    return out


def diff_line(changes: dict) -> str:
    parts = []
    for plural, part in changes.items():
        for verb, names in part.items():
            noun = plural[:-1] if len(names) == 1 and plural.endswith('s') else plural
            parts.append(f"{len(names)} {noun} {verb} ({', '.join(names[:5])}{'\u2026' if len(names) > 5 else ''})")
    return '; '.join(parts) or 'no tag, trigger or variable changed (container settings only)'


def tag_rows(version: dict) -> list[dict]:
    return [{'name': cut(t.get('name')), 'type': t.get('type'), 'paused': bool(t.get('paused')), 'triggers': len(t.get('firingTriggerId') or []),
             'consent': (t.get('consentSettings') or {}).get('consentStatus')} for t in (version.get('tag') or [])[:LIST_LIMIT] if isinstance(t, dict)]


def seconds(text: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(text)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def when(t: float) -> str:
    return f'{clock.date(t)} {clock.label(t)}'


def publish_items(container: str, publishes: list[dict], now: float) -> list[dict]:
    out = []
    for p in publishes:
        seen = seconds(p.get('at'))
        if seen is None or now - seen >= CARD_SECONDS:
            continue
        stored = seconds(p.get('storedAt')) if p.get('storedAt') else None
        x = anomaly(CHECK, 'gtm-publish', '-', container, f"v{p['to']}", 'info', 1, None, None, '', 'GTM published',
                    f"GTM container {container} went live with version {p['to']} \"{p.get('name') or ''}\" (was {p['from']}), seen {when(seen)}"
                    + (f', stored {when(stored)}' if stored is not None else '') + f". Changes: {p.get('changes')}."
                    + (f" Notes: {p['description']}." if p.get('description') else '')
                    + ' A GTM publish changes the tags, conversions and consent handling on the site without a deploy; check GA key events and Umami after it.',
                    1, f"INFO GTM {container} v{p['to']} published: {str(p.get('changes') or '')[:120]}")
        out.append(x)
    return out


def prior_of(rows: list | None, container: str) -> dict:
    for row in rows or []:
        if isinstance(row, dict) and row.get('container') == container:
            return row
    return {}


def kept(publishes: object) -> list[dict]:
    return [p for p in publishes if isinstance(p, dict) and p.get('to') and p.get('from') and p.get('at')] if isinstance(publishes, list) else []


def refused(gtm: Gtm, error: GoogleError) -> Sweep:
    shown = f'the {PART} {gtm.container}'
    card = access_card(CHECK, NAME, PART, shown, error)
    return Sweep(PassResult(CHECK, True, f'ok{DOT}no access to {shown}', [card], {}, (access_key(CHECK, PART),)))


def run_pass(gtm: Gtm, prior: dict, now: float) -> Sweep:
    try:
        live = gtm.live()
        current = summary(live)
        publishes = kept(prior.get('publishes'))
        before = (prior.get('live') or {}).get('id') if isinstance(prior.get('live'), dict) else None
        if before and before != current['id']:
            try:
                changes = diff_line(diff(gtm.version(str(before)), live))
            except (GoogleError, GtmError) as e:
                changes = f'the earlier version could not be read ({e.reason})'
            publishes.insert(0, {'from': str(before), 'to': current['id'], 'name': current['name'], 'description': current['description'], 'at': iso(now),
                                 'storedAt': current['storedAt'], 'changes': cut(changes, CHANGES_LIMIT)})
    except GoogleError as e:
        error = e.as_refusal(NEEDS)
        if error.denied:
            return refused(gtm, error)
        return Sweep(PassResult(CHECK, False, f'error: {error.reason}'))
    except GtmError as e:
        return Sweep(PassResult(CHECK, False, f'error: {e.reason}'))
    section = {'container': gtm.container, 'path': gtm.path, 'live': current, 'checkedAt': iso(now), 'publishes': publishes[:PUBLISHES_KEPT]}
    return Sweep(PassResult(CHECK, True, 'ok', publish_items(gtm.container, section['publishes'], now), {}, (CHECK, access_key(CHECK, PART))), sections={'gtm': [section]})


def release_rows(section: dict | None, since: float) -> list[dict]:
    if not isinstance(section, dict) or not isinstance(section.get('container'), str):
        return []
    out = []
    for p in kept(section.get('publishes')):
        at = seconds(p['at'])
        if at is not None and at >= since:
            out.append({'id': f"gtm-{section['container']}-{p['to']}", 'ns': 'gtm', 'svc': section['container'], 'at': p['at'], 'fromImage': f"v{p['from']}", 'toImage': f"v{p['to']}",
                        'gtm': {k: p.get(k) for k in ('name', 'description', 'changes', 'storedAt')}})
    return out


def links_for(a: dict) -> list[dict] | None:
    if a.get('src') != CHECK:
        return None
    return [dict(label='Google Tag Manager', url=GTM_UI)]


def wanted(a: dict) -> dict:
    op = a.get('op') or 'live'
    if op not in OPS:
        raise GtmError(400, f"op must be one of {', '.join(OPS)}")
    ids = {k: str(a.get(k) or '').strip().lstrip('v') for k in ('id', 'from', 'to')}
    for k, v in ids.items():
        if v and not VERSION.fullmatch(v):
            raise GtmError(400, f'{k} must be a GTM version number such as 134')
    if op == 'version' and not ids['id']:
        raise GtmError(400, 'op version needs id')
    if op == 'diff' and not (ids['from'] and ids['to']):
        raise GtmError(400, 'op diff needs from and to')
    return {'op': op} | ids


def read(gtm: Gtm, w: dict) -> dict:
    if w['op'] == 'versions':
        rows = [{'id': h.get('containerVersionId'), 'name': cut(h.get('name')), 'tags': int(h.get('numTags') or 0), 'triggers': int(h.get('numTriggers') or 0),
                 'variables': int(h.get('numVariables') or 0)} for h in gtm.headers() if isinstance(h, dict) and not h.get('deleted')]
        rows.sort(key=lambda r: -int(r['id'] or 0))
        return {'container': gtm.container, 'versions': rows[:VERSIONS], 'note': NOTE}
    if w['op'] == 'diff':
        return {'container': gtm.container, 'from': w['from'], 'to': w['to'], 'changes': diff(gtm.version(w['from']), gtm.version(w['to']))}
    version = gtm.version(w['id']) if w['op'] == 'version' else gtm.live()
    return {'container': gtm.container, 'live' if w['op'] == 'live' else 'version': summary(version), 'tags': tag_rows(version)}
