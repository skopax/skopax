import hashlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Executor
from urllib.parse import urlsplit

from skopax import brand
from skopax.cardlink import card_url
from skopax.contract import Missing
from skopax.errkind import kind, text_of
from skopax.jiralinks import ticket_finder
from skopax.messages import msg
from skopax.store import NotFound, Store
from skopax.tickets import link_paragraph

log = logging.getLogger('skopax.backlink')

LINK, COMMENT, OFF = 'link', 'comment', 'off'
MODES = (LINK, COMMENT, OFF)
DONE = 'jiraBacklinks'
TICKET_TYPES = frozenset({'ticket', 'link', 'take', 'resolve'})
TITLE_LIMIT = 200
GLOBAL_ID_LIMIT = 255
HASH_CHARS = 24
STOP_S = 5
TOLD_MAX = 500


def keys_in(ticket, projects: tuple[str, ...] = ()) -> list[str]:
    return list(dict.fromkeys(ticket_finder(projects).findall(str(ticket or ''))))


def card_title(card: dict) -> str:
    text = ' '.join(f"{card.get('svc') or '-'} / {card.get('label') or '?'}".split())
    return text if len(text) <= TITLE_LIMIT else text[:TITLE_LIMIT - 1].rstrip() + '…'


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:HASH_CHARS]


def global_id(base_url: str, ident: str) -> str:
    parts = urlsplit(base_url)
    site = parts.netloc + parts.path.rstrip('/')
    found = f'skopax:{site}:card:{ident}'
    if len(found) <= GLOBAL_ID_LIMIT:
        return found
    found = f'skopax:{site}:card:{digest(ident)}'
    return found if len(found) <= GLOBAL_ID_LIMIT else f'skopax:{digest(site)}:card:{digest(ident)}'


class Backlinks:
    def __init__(self, store: Store, jira: object, base_url: str, mode: Callable[[], str] = lambda: LINK, projects: Callable[[], tuple[str, ...]] = lambda: (),
                 pool: Callable[[], Executor] | None = None):
        self.store = store
        self.jira = jira
        self.base_url = base_url.rstrip('/')
        self.mode = mode
        self.projects = projects
        self.make_pool = pool
        self.pool: Executor | None = None
        self.guard = threading.Lock()
        self.busy = threading.Lock()
        self.stopped = False
        self.told_skip = False
        self.told: dict[tuple[str, str], str] = {}

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc

    def current(self) -> str:
        try:
            mode = str(self.mode() or LINK).strip().lower()
        except Exception as e:
            log.warning('the Jira back-link setting could not be read, links stay off: %s', kind(e))
            return OFF
        return mode if mode in MODES else LINK

    def ready(self) -> bool:
        if self.current() == OFF:
            return False
        try:
            on = bool(self.jira is not None and self.jira.enabled)
        except Exception:
            on = False
        if on and self.host:
            return True
        if not self.told_skip:
            self.told_skip = True
            log.info('jira back-links skipped: %s', 'no public address (core.base-url)' if on else 'Jira is not set up')
        return False

    def keys(self, ticket) -> list[str]:
        try:
            projects = tuple(self.projects() or ())
        except Exception as e:
            log.warning('the Jira projects could not be read for back-links: %s', kind(e))
            projects = ()
        return keys_in(ticket, projects)

    def watch(self, action: dict) -> dict | None:
        if action.get('type') not in TICKET_TYPES or not action.get('key'):
            return None
        return {action['key']: True}

    def applied(self, action: dict, before: dict | None) -> None:
        for key in (before or {}):
            self.changed(key, action.get('requestedBy') or '', created=action.get('type') == 'ticket')

    def triaged(self, key: str, before: dict | None) -> None:
        self.changed(key, 'investigator')

    def swept(self, results: list[dict]) -> None:
        for r in results or []:
            if 'closed' not in r and r.get('card') and r.get('issue'):
                self.ticket_set(r['card'], r['issue'], 'jira-links', created=False)

    def changed(self, key: str, by: str, created: bool = False) -> None:
        try:
            card = self.store.anomaly(key)
        except Exception as e:
            log.warning('jira back-link skipped for %s, the card could not be read: %s', key, kind(e))
            return
        mode = self.current()
        done = {str(d) for d in card.get(DONE) or []}
        for issue in self.keys(card.get('ticket')):
            if f'{issue} {mode}' not in done:
                self.ticket_set(key, issue, by, created)

    def ticket_set(self, key: str, issue: str, by: str, created: bool = False) -> None:
        try:
            if not self.ready():
                return
            if self.make_pool is None:
                self.send(key, issue, by, created)
                return
            with self.guard:
                if self.stopped:
                    return
                if self.pool is None:
                    self.pool = self.make_pool()
                self.pool.submit(self.send, key, issue, by, created)
        except Exception as e:
            log.warning('jira back-link for %s on %s not queued: %s', key, issue, kind(e))

    def stop(self, timeout: float = STOP_S) -> None:
        with self.guard:
            self.stopped = True
            pool = self.pool
        if pool is None:
            return
        pool.shutdown(wait=False, cancel_futures=True)
        if self.busy.acquire(timeout=timeout):
            self.busy.release()
        else:
            log.info('a Jira back-link was still going out at shutdown; it is left to finish')

    def note(self, key: str, by: str, line: str) -> None:
        try:
            self.store.add_activity('jira', by or 'server', line, key=key)
        except Exception as e:
            log.warning('jira back-link activity for %s not saved: %s', key, kind(e))

    def send(self, key: str, issue: str, by: str, created: bool) -> None:
        with self.busy:
            self._send(key, issue, by, created)

    def _send(self, key: str, issue: str, by: str, created: bool) -> None:
        try:
            mode = self.current()
            if mode == OFF or (mode == COMMENT and created):
                return
            card = self.store.anomaly(key)
            mark = f'{issue} {mode}'
            done = [str(d) for d in card.get(DONE) or []]
            if mark in done:
                return
            num = card.get('num')
            url = card_url(self.base_url, key, num)
            product = brand.product()
            title = msg('tickets.backlink_title', product=product, num=num, title=card_title(card)) if num else msg('tickets.backlink_title_key', product=product, title=card_title(card))
            if mode == COMMENT:
                body = {'type': 'doc', 'version': 1, 'content': [link_paragraph(title, url, msg('tickets.backlink_comment', product=product))]}
                self.jira.add_comment(issue, body)
                line = f'Commented the card link on {issue}'
            else:
                self.jira.remote_link(issue, url, title, global_id(self.base_url, str(num or key)), f'{self.base_url}/favicon.ico', product)
                line = f'Linked back from {issue}'
        except NotFound:
            return
        except Exception as e:
            log.warning('jira back-link for %s on %s failed: %s', key, issue, kind(e))
            reason = msg('jira.issue_unseen', key=issue) if isinstance(e, Missing) else text_of(e)
            if self.told.get((key, issue)) != reason:
                self.told.pop((key, issue), None)
                while len(self.told) >= TOLD_MAX:
                    self.told.pop(next(iter(self.told)))
                self.told[(key, issue)] = reason
                self.note(key, by, f'Jira back-link not added to {issue}: {reason}')
            return
        self.told.pop((key, issue), None)
        try:
            self.store.set_tagging(key, {DONE: done + [mark]})
        except Exception as e:
            log.warning('jira back-link on %s for %s went out but was not recorded: %s', issue, key, kind(e))
        self.note(key, by, line)
        log.info('jira back-link: %s on %s (%s)', key, issue, mode)
