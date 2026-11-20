import logging
import re
import time
from collections.abc import Callable

from skopax import clock, tags
from skopax.cardlink import card_url
from skopax.engine import parse_iso
from skopax.errkind import kind as error_kind
from skopax.messages import msg
from skopax.slack import BLOCK_LIMIT, EMOJI, SCOPE_ERRORS, SECTION_LIMIT, SlackError, cut, esc
from skopax.store import Store

log = logging.getLogger('skopax.tagreport')

RUN = 'tag-report'
FEATURE = 'tag-report'
KEY = 'tags.report-at'
DEFAULT_AT = '10:00'
HM = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d')
PERIOD = 300
OFFSET = 45
SHOWN = 3
TAGS_MAX = BLOCK_LIMIT - 2
TEXT_LIMIT = 3000
NAME_LIMIT = 60
LOOKUP_TTL_S = 86400
WEEKEND = (5, 6)
SCOPE = 'users:read.email'
OWNERS_MAX = 20
STOPPED = ('progress', 'handled')
SEV_ORDER = {'crit': 0, 'warn': 1, 'info': 2}
QUIET_KEPT = ('crit', 'warn')
DAY_H = 24


def valid_at(text) -> bool:
    return isinstance(text, str) and bool(HM.fullmatch(text))


def business_day(now: float) -> bool:
    return clock.local(now).weekday() not in WEEKEND


def due(at: str, now: float) -> bool:
    return valid_at(at) and business_day(now) and clock.hm(now) >= at


def board_open(cards: list[dict]) -> list[dict]:
    by_key = {c.get('key'): c for c in cards}

    def grouped(a: dict) -> bool:
        host = by_key.get(a.get('parent')) if a.get('parent') and a.get('parent') != a.get('key') else None
        return bool(host) and (host.get('status') == 'open' or a.get('status') == 'cleared')

    def shown(a: dict) -> bool:
        if a.get('muted') or a.get('triage') in STOPPED or grouped(a):
            return False
        return a.get('status') == 'open' or (a.get('status') == 'cleared' and a.get('sev') in QUIET_KEPT)

    return [a for a in cards if shown(a)]


def board_link(base_url: str, tag: str) -> str:
    return f"{base_url.rstrip('/')}/#t=anoms/open/tag:{tag}"


def link_text(text) -> str:
    return esc(text).replace('|', '\u01c0')


def age(seconds: float) -> str:
    hours = max(seconds, 0) / 3600
    if hours < 1:
        return msg('tagreport.age.min', n=max(int(hours * 60), 1))
    if hours < 2 * DAY_H:
        return msg('tagreport.age.hours', n=int(hours))
    return msg('tagreport.age.days', n=int(hours // DAY_H))


def seen_at(a: dict, now: float) -> float:
    try:
        return parse_iso(a['firstSeen']) if a.get('firstSeen') else now
    except (TypeError, ValueError):
        return now


def person(email: str, names: dict[str, str], mention: Callable[[str], str | None]) -> str:
    found = mention(email)
    if found:
        return found
    return esc(str(names.get(email) or '').strip() or email.split('@')[0])


def card_line(a: dict, base_url: str, now: float) -> str:
    name = f"#{a['num']}" if a.get('num') else cut(str(a.get('key') or ''), NAME_LIMIT)
    sev = a.get('sev') if a.get('sev') in SEV_ORDER else 'info'
    return msg('tagreport.card', link=card_url(base_url, a['key'], a.get('num')), name=link_text(name), emoji=EMOJI[sev], sev=msg(f'slack.sev.{sev}'),
               svc=esc(a.get('svc') or '-'), age=age(now - seen_at(a, now)))


def build(cards: list[dict], owners: dict[str, list[str]], names: dict[str, str], mention: Callable[[str], str | None], base_url: str, now: float,
          labels: dict[str, str]) -> dict | None:
    shown = board_open(cards)
    rows = []
    for tag, emails in owners.items():
        if tag not in labels or not emails:
            continue
        mine = sorted((a for a in shown if tag in tags.ids(a)), key=lambda a: (SEV_ORDER.get(a.get('sev'), 3), seen_at(a, now)))
        if mine:
            crit = sum(1 for a in mine if a.get('sev') == 'crit')
            rows.append((tag, emails, mine, crit))
    if not rows:
        return None
    rows.sort(key=lambda r: (-r[3], -len(r[2]), labels[r[0]].lower(), r[0]))
    head = msg('tagreport.head', date=clock.date(now, hm=False))
    blocks, lines = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': head}}], [head]
    for tag, emails, mine, crit in rows[:TAGS_MAX]:
        link = board_link(base_url, tag)
        line = msg('tagreport.tag', who=', '.join(person(e, names, mention) for e in emails), link=link, tag=link_text(labels[tag]), open=len(mine), crit=crit,
                   warn=sum(1 for a in mine if a.get('sev') == 'warn'))
        body = [line] + [card_line(a, base_url, now) for a in mine[:SHOWN]]
        if len(mine) > SHOWN:
            body.append(msg('tagreport.more', link=link, n=len(mine) - SHOWN))
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': cut('\n'.join(body), SECTION_LIMIT)}})
        lines.append(line)
    if len(rows) > TAGS_MAX:
        blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': msg('tagreport.more_tags', n=len(rows) - TAGS_MAX)}]})
    return {'text': cut('\n'.join(lines), TEXT_LIMIT), 'blocks': blocks}


class SlackIds:
    def __init__(self, slack, ttl: float = LOOKUP_TTL_S, clock: Callable[[], float] = time.monotonic, on_scope: Callable[[bool], None] = lambda missing: None):
        self.slack = slack
        self.ttl = ttl
        self.clock = clock
        self.on_scope = on_scope
        self._held: dict[str, tuple[float, str | None]] = {}
        self._missing: bool | None = None
        self._stopped = False
        self._told = False

    @property
    def missing(self) -> bool:
        return bool(self._missing)

    def start(self) -> None:
        self._stopped = False

    def mention(self, email: str) -> str | None:
        email = str(email or '').strip().lower()
        if not email or self._stopped:
            return None
        held = self._held.get(email)
        if held is not None and self.clock() - held[0] < self.ttl:
            return held[1]
        try:
            user = self.slack.user_by_email(email)
        except SlackError as e:
            self._stopped = True
            if e.code in SCOPE_ERRORS:
                self._scope(True)
            else:
                log.warning('Slack user lookups stopped for this report, responsibles are named without a ping: %s', e.code or error_kind(e))
            return None
        found = f"<@{user['id']}>" if user else None
        self._held[email] = (self.clock(), found)
        self._scope(False)
        return found

    def _scope(self, missing: bool) -> None:
        if missing == self._missing:
            return
        self._missing = missing
        if missing and not self._told:
            self._told = True
            log.warning('the Slack app lacks the scope %s: responsibles in the tag report are named without a ping', SCOPE)
        try:
            self.on_scope(missing)
        except Exception as e:
            log.warning('the Slack scope flag of the tag report was not stored: %s', error_kind(e))


class TagReport:
    def __init__(self, store: Store, slack, ids: SlackIds, owners: Callable[[], dict[str, list[str]]], names: Callable[[], dict[str, str]], at: Callable[[], str],
                 on: Callable[[], bool], base_url: str, labels: Callable[[], dict[str, str]] = tags.all_tags, clock: Callable[[], float] = time.time):
        self.store = store
        self.slack = slack
        self.ids = ids
        self.owners = owners
        self.names = names
        self.at = at
        self.on = on
        self.base_url = base_url
        self.labels = labels
        self.clock = clock

    def held(self) -> dict[str, list[str]]:
        known = self.labels()
        return {tag: emails for tag, emails in self.owners().items() if tag in known and emails}

    @property
    def enabled(self) -> bool:
        return bool(self.on() and self.slack.enabled and valid_at(self.at()) and self.held())

    def run_once(self, now: float | None = None) -> dict | None:
        now = self.clock() if now is None else now
        if not self.enabled or not due(self.at(), now):
            return None
        day = f'{RUN}:{clock.day(now)}'
        if not self.store.claim_run(day):
            return None
        try:
            self.ids.start()
            message = build(self.store.load().get('anomalies') or [], self.held(), self.names(), self.ids.mention, self.base_url, now, self.labels())
            if message is None:
                log.info('daily tag report: no tag with responsibles has an open card, nothing posted')
                return None
            sent = self.slack.post(message)
        except Exception:
            self.store.drop_run(day)
            raise
        shown = sum(1 for b in message['blocks'] if b['type'] == 'section') - 1
        self.store.add_activity('slack', 'server', f'Daily tag report posted \u00b7 {shown} tag{"" if shown == 1 else "s"}')
        log.info('daily tag report posted for %d tags', shown)
        return sent | {'tags': shown}
