import logging
import threading
import time
from collections.abc import Callable

from skopax import clock, information, tags
from skopax.cardlink import card_url
from skopax.engine import parse_iso
from skopax.errkind import kind as error_kind
from skopax.messages import fold, msg
from skopax.slack import BLOCK_LIMIT, EMOJI, SCOPE_ERRORS, SECTION_LIMIT, SlackDown, SlackError, cut, esc, reason_of
from skopax.store import Store

log = logging.getLogger('skopax.tagreport')

RUN = 'tag-report'
FEATURE = 'tag-report'
KEY = 'tags.report-at'
PERIOD = 300
OFFSET = 45
SHOWN = 3
TAGS_MAX = BLOCK_LIMIT - 2
TEXT_LIMIT = 3000
NAME_LIMIT = 60
FAILURE_TTL_S = 600
LOOKUP_TTL_S = 86400
WEEKEND = (5, 6)
SCOPE = 'users:read.email'
OWNERS_MAX = 20
PEOPLE_TTL_S = 6 * 3600
PEOPLE_MAX = 20
QUERY_MIN = 2
QUERY_MAX = 100
SOURCES = ('skopax', 'responsible', 'slack')
STOPPED = ('progress', 'handled')
SEV_ORDER = {'crit': 0, 'warn': 1, 'info': 2}
QUIET_KEPT = ('crit', 'warn')
DAY_H = 24
REPEAT_GAP_S = 20 * 3600
GONE_CHANNEL = ('not_in_channel', 'channel_not_found', 'is_archived', 'invalid_auth', 'account_inactive', 'token_revoked', 'missing_scope', 'not_allowed_token_type')


def valid_at(text) -> bool:
    return tags.report_at_ok(text)


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


def counted(kind: str, n: int) -> str:
    return msg(f'tagreport.{kind}.{"one" if n == 1 else "many"}', n=n)


def counts(total: int, crit: int, warn: int) -> str:
    split = [counted(kind, n) for kind, n in (('crit', crit), ('warn', warn)) if n]
    head = counted('open', total)
    return msg('tagreport.split', open=head, parts=', '.join(split)) if split else head


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
    shown = [a for a in board_open(information.cards_only(cards)) if a.get('sev') in information.CARD_SEVS]
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
        line = msg('tagreport.tag', who=', '.join(person(e, names, mention) for e in emails), link=link, tag=link_text(labels[tag]),
                   counts=counts(len(mine), crit, sum(1 for a in mine if a.get('sev') == 'warn')))
        body = [line] + [card_line(a, base_url, now) for a in mine[:SHOWN]]
        if len(mine) > SHOWN:
            body.append(msg('tagreport.more', link=link, n=len(mine) - SHOWN))
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': cut('\n'.join(body), SECTION_LIMIT)}})
        lines.append(line)
    if len(rows) > TAGS_MAX:
        blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': msg('tagreport.more_tags', n=len(rows) - TAGS_MAX)}]})
    return {'text': cut('\n'.join(lines), TEXT_LIMIT), 'blocks': blocks}


class SlackIds:
    def __init__(self, slack, ttl: float = LOOKUP_TTL_S, clock: Callable[[], float] = time.monotonic, on_scope: Callable[[bool], None] = lambda missing: None,
                 flagged: Callable[[], bool] = lambda: False):
        self.slack = slack
        self.ttl = ttl
        self.clock = clock
        self.on_scope = on_scope
        self.flagged = flagged
        self._held: dict[str, tuple[float, str | None, str]] = {}
        self._missing = False
        self._down_until = -FAILURE_TTL_S
        self._stopped = False
        self._recheck = False
        self._told = False

    @property
    def missing(self) -> bool:
        return self._missing

    def start(self) -> None:
        self._stopped = False
        self._recheck = self._stored()

    def _stored(self) -> bool:
        try:
            return bool(self.flagged())
        except Exception as e:
            log.warning('the Slack scope flag of the tag report was not read: %s', error_kind(e))
            return False

    def mention(self, email: str) -> str | None:
        email = str(email or '').strip().lower()
        if not email:
            return None
        held = self._held.get(email)
        fresh = held is not None and self.clock() - held[0] < self.ttl
        cached = held[1] if fresh else None
        if fresh and not self._recheck:
            return cached
        if self._stopped:
            return cached
        try:
            user = self.slack.user_by_email(email)
        except SlackError as e:
            self._stopped = True
            if e.code in SCOPE_ERRORS:
                self._scope(True)
            else:
                log.warning('Slack user lookups stopped for this report, responsibles are named without a ping: %s', e.code or error_kind(e))
            return cached
        found = f"<@{user['id']}>" if user else None
        self._held[email] = (self.clock(), found, str((user or {}).get('name') or '').strip())
        self._recheck = False
        self._scope(False)
        return found

    def peek(self, email: str) -> str:
        held = self._held.get(str(email or '').strip().lower())
        if held is None:
            return 'unchecked'
        return 'found' if held[1] else 'unknown'

    def name(self, email: str) -> str:
        held = self._held.get(str(email or '').strip().lower())
        return held[2] if held else ''

    def check_all(self, emails: list[str]) -> None:
        for email in emails:
            email = str(email or '').strip().lower()
            held = self._held.get(email)
            if held is not None and self.clock() - held[0] < self.ttl:
                continue
            if not self.slack.enabled or self.clock() < self._down_until:
                continue
            try:
                user = self.slack.user_by_email(email)
            except Exception as e:
                self._down_until = self.clock() + FAILURE_TTL_S
                log.warning('a responsible was not looked up in Slack, lookups pause for a while: %s', getattr(e, 'code', None) or error_kind(e))
                return
            self._held[email] = (self.clock(), f"<@{user['id']}>" if user else None, str((user or {}).get('name') or '').strip())

    def _scope(self, missing: bool) -> None:
        self._missing = missing
        if missing and not self._told:
            self._told = True
            log.warning('the Slack app lacks the scope %s: responsibles in the tag report are named without a ping', SCOPE)
        if missing == self._stored():
            return
        try:
            self.on_scope(missing)
        except Exception as e:
            log.warning('the Slack scope flag of the tag report was not stored: %s', error_kind(e))


def in_background(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name='slack-people', daemon=True).start()


class SlackPeople:
    def __init__(self, slack, ttl: float = PEOPLE_TTL_S, clock: Callable[[], float] = time.monotonic, spawn: Callable[[Callable[[], None]], None] = in_background):
        self.slack = slack
        self.ttl = ttl
        self.clock = clock
        self.spawn = spawn
        self._guard = threading.Lock()
        self._people: list[dict] | None = None
        self._at = 0.0
        self._for: str | None = None
        self._busy = False
        self._down_until = -FAILURE_TTL_S

    def lookup(self) -> tuple[list[dict], str]:
        token = self.slack.token
        if not token:
            return [], 'off'
        with self._guard:
            if token != self._for:
                self._for, self._people, self._down_until = token, None, -FAILURE_TTL_S
            now = self.clock()
            start = (self._people is None or now - self._at >= self.ttl) and not self._busy and now >= self._down_until
            if start:
                self._busy = True
        if start:
            self.spawn(lambda: self._build(token))
        with self._guard:
            if self._people is not None:
                return self._people, 'ok'
            return [], 'loading' if self._busy else 'unavailable'

    def _build(self, token: str) -> None:
        try:
            found = self.slack.members()
        except Exception as e:
            log.warning('Slack members were not listed, the responsible picker offers Skopax people only for a while: %s', reason_of(e))
            with self._guard:
                self._busy = False
                self._down_until = self.clock() + FAILURE_TTL_S
            return
        with self._guard:
            self._busy = False
            if token == self._for:
                self._people, self._at = found, self.clock()
        log.info('%d Slack members kept for the responsible picker', len(found))


def find_people(query: str, rows: list[dict], limit: int = PEOPLE_MAX) -> list[dict]:
    wanted = fold(str(query or '').strip())
    merged: dict[str, dict] = {}
    for row in rows:
        email = str(row.get('email') or '').strip().lower()
        if not email:
            continue
        held = merged.setdefault(email, {'email': email, 'name': row.get('name') or email.split('@')[0], 'source': row['source'], 'text': [email]})
        held['text'] += [fold(str(row.get(k) or '')) for k in ('name', 'display')]
    found = [p for p in merged.values() if any(wanted in t for t in p['text'])]
    def rank(p: dict) -> tuple:
        words = [w for t in p['text'] for w in t.replace('@', ' ').replace('.', ' ').split()]
        return (not any(w.startswith(wanted) for w in words), SOURCES.index(p['source']), fold(p['name']))
    return [{'email': p['email'], 'name': p['name'], 'source': p['source']} for p in sorted(found, key=rank)[:limit]]


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

    def held(self, names: dict[str, str] | None = None) -> dict[str, list[str]]:
        known = self.labels()
        active = self.names() if names is None else names
        out = {tag: [e for e in emails if e in active] for tag, emails in self.owners().items() if tag in known}
        return {tag: emails for tag, emails in out.items() if emails}

    @property
    def wanted(self) -> bool:
        return bool(self.on() and valid_at(self.at()) and self.held())

    @property
    def enabled(self) -> bool:
        return bool(self.slack.enabled and self.wanted)

    def run_once(self, now: float | None = None) -> dict | None:
        now = self.clock() if now is None else now
        if not self.enabled or not due(self.at(), now):
            return None
        last = self.store.tag_report_at()
        if last is not None and 0 <= now - last < REPEAT_GAP_S:
            return None
        day = f'{RUN}:{clock.day(now)}'
        if not self.store.claim_run(day):
            return None
        try:
            self.ids.start()
            names = self.names()
            message = build(self.store.load().get('anomalies') or [], self.held(names), names, self.ids.mention, self.base_url, now, self.labels())
        except Exception:
            self.store.drop_run(day)
            raise
        if message is None:
            log.info('daily tag report: no tag with responsibles has an open card, nothing posted')
            return None
        try:
            sent = self.slack.post(message)
        except SlackDown as e:
            log.warning('daily tag report: Slack did not answer the post, so it may have gone out; it is not sent again today (%s)', error_kind(e))
            return None
        except SlackError as e:
            if e.code in GONE_CHANNEL:
                log.warning('daily tag report not posted today: Slack refused it (%s)', e.code)
                return None
            self.store.drop_run(day)
            raise
        self.store.note_tag_report(now)
        shown = sum(1 for b in message['blocks'] if b['type'] == 'section') - 1
        self.store.add_activity('slack', 'server', f'Daily tag report posted \u00b7 {shown} tag{"" if shown == 1 else "s"}')
        log.info('daily tag report posted for %d tags', shown)
        return sent | {'tags': shown}
