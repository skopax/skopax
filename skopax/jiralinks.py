import datetime as dt
import json
import logging
import re
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from skopax.actors import SYSTEM
from skopax.cardlink import linked
from skopax.errkind import NoAccess
from skopax.store import NotFound, Store, iso

GENERIC_KEY = r'[A-Z][A-Z0-9]{1,9}-\d+'


def ticket_pattern(projects: tuple[str, ...]) -> re.Pattern:
    if not projects:
        return re.compile(GENERIC_KEY)
    return re.compile(r'(?:' + '|'.join(re.escape(p) for p in projects) + r')-\d+')


def listed(projects) -> tuple[str, ...]:
    return tuple(projects() if callable(projects) else projects)


def site_of(site: str | Callable[[], str]) -> str:
    return str((site() if callable(site) else site) or '').rstrip('/')


def ticket_projects(settings, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    return tuple(dict.fromkeys(p for p in (settings.jira_project, *settings.jira_projects, settings.jira_tech_project, *extra) if p))


def ticket_finder(projects: tuple[str, ...]) -> re.Pattern:
    return re.compile(r'\b(' + ticket_pattern(projects).pattern + r')\b')


SWEEP_BY = 'jira-links'
MACHINE_TRIAGE = ('pod', 'auto')

log = logging.getLogger('skopax.jiralinks')

FIELDS = ['summary', 'status', 'description', 'comment']
CANCELLED = 'Cancelled'
CLOSED = ('Live', 'Done', CANCELLED)
LIMIT = 50
CLOSED_FIELDS = ['status', 'statuscategorychangedate']
REOPEN_GRACE_S = 2 * 3600
OFFSET_NO_COLON = re.compile(r'([+-]\d{2})(\d{2})$')


def jira_time(value) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(OFFSET_NO_COLON.sub(r'\1:\2', str(value))).timestamp()
    except ValueError:
        return None


def card_keys(fields: dict, store: Store | None = None, base_url: str = '') -> set[str]:
    keys, nums = linked(json.dumps(fields, ensure_ascii=False), base_url)
    if store is not None:
        for n in nums:
            key = store.key_of_number(n)
            if key:
                keys.add(key)
    return keys


class JiraLinks:
    def __init__(self, store: Store, jira: object, flag: bool = True, notes=None, base_url: str = '', developer_field: str = ''):
        self.store = store
        self.jira = jira
        self.flag = flag
        self.notes = notes
        self.base_url = base_url.rstrip('/')
        self.developer_field = developer_field
        self.refused: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self.flag and self.jira.enabled and bool(urlsplit(self.base_url).netloc)

    @property
    def jql(self) -> str:
        return f'text ~ "{urlsplit(self.base_url).netloc}" AND updated >= -2d ORDER BY updated DESC'

    @property
    def fields(self) -> list[str]:
        return FIELDS + ([self.developer_field] if self.developer_field else [])

    def run_once(self, now: float | None = None) -> list[dict]:
        linked = []
        self.refused = {}
        for issue in self._read(lambda: self.jira.search(self.jql, self.fields, LIMIT)) or []:
            key = issue.get('key')
            fields = issue.get('fields') or {}
            if not key or ((fields.get('status') or {}).get('name') in CLOSED):
                continue
            owner = (fields.get(self.developer_field) or {}).get('displayName') if self.developer_field else None
            for card_key in sorted(card_keys(fields, self.store, self.base_url)):
                try:
                    card = self.store.anomaly(card_key)
                except NotFound:
                    continue
                if card.get('status') != 'open' or (card.get('triage') == 'handled' and card.get('triagedBy') not in MACHINE_TRIAGE) or (card.get('ticket') and card['ticket'] != key):
                    continue
                if card.get('triage') == 'progress' and card.get('ticket') == key:
                    continue
                picked = card.get('owner') if card.get('assigned') else None
                self.store.triage(card_key, 'progress', ticket=key, owner=None if picked else owner)
                shown = picked or owner
                self.store.add_activity('jira', 'server', f'{key} → {card_key} In progress' + (f' · {shown}' if shown else ''), key=card_key)
                if self.notes:
                    self.notes.jira_linked(card_key, key, shown)
                log.info('jira link: %s now tracks %s', key, card_key)
                linked.append({'card': card_key, 'issue': key})
        return linked + self.closed_tickets(time.time() if now is None else now)

    def _read(self, search: Callable[[], list[dict]]) -> list[dict] | None:
        try:
            return search()
        except NoAccess as e:
            part = e.part or 'issues'
            self.refused[part] = e.permission
            log.warning('jira link sweep: no access to %s', part)
            return None

    def closed_tickets(self, now: float) -> list[dict]:
        tracked = [a for a in self.store.load().get('anomalies') or [] if a.get('triage') == 'progress' and ticket_pattern(()).fullmatch(str(a.get('ticket') or ''))]
        if not tracked:
            return []
        keys = sorted({a['ticket'] for a in tracked})
        found = self._read(lambda: self.jira.search(f"key in ({', '.join(keys)}) AND status in ({', '.join(CLOSED)})", CLOSED_FIELDS, len(keys)))
        if found is None:
            return []
        closed = {i['key']: (i.get('fields') or {}) for i in found}
        out = []
        for a in tracked:
            ticket, fields = a['ticket'], closed.get(a['ticket'])
            status = (fields or {}).get('status', {}).get('name')
            if not status:
                continue
            if a.get('status') == 'open':
                closed_at = jira_time(fields.get('statuscategorychangedate'))
                if closed_at is None or now - closed_at < REOPEN_GRACE_S:
                    log.info('jira link: %s is %s but closed under %d min ago, %s gets its grace', ticket, status, REOPEN_GRACE_S // 60, a['key'])
                    continue
                self.store.note(a['key'], f'{ticket} is {status} but the card still fires; back to the queue.', SWEEP_BY)
                self.store.triage(a['key'], None, by=SWEEP_BY)
                self.store.add_action({'type': 'dive', 'key': a['key'], 'target': a['key'], 'label': f'{ticket} {status}, still firing', 'requestedBy': SWEEP_BY})
                self.store.add_activity('jira', 'server', f'{ticket} {status} but {a["key"]} still fires · reopened, Investigate queued', key=a['key'])
                log.warning('jira link: %s is %s but %s still fires, reopened', ticket, status, a['key'])
            else:
                how = {'kind': 'handled' if status == CANCELLED else 'fixed', 'ref': ticket, 'by': SWEEP_BY, 'via': SYSTEM}
                self.store.triage(a['key'], 'handled', ticket=ticket, by=SWEEP_BY, at=iso(dt.datetime.fromtimestamp(now, dt.timezone.utc)), resolution=how)
                self.store.add_activity('jira', 'server', f'{ticket} {status} · {a["key"]} cleared, resolved', key=a['key'])
                if self.notes:
                    self.notes.jira_closed(a['key'], ticket, status)
                log.info('jira link: %s is %s and %s cleared, resolved', ticket, status, a['key'])
            out.append({'card': a['key'], 'issue': ticket, 'closed': status})
        return out
