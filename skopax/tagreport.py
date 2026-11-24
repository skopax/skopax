import datetime as dt
import logging
import threading
import time
from collections.abc import Callable

from skopax import clock, information, partmail, severity, tags
from skopax.cardlink import card_url
from skopax.engine import parse_iso
from skopax.errkind import kind as error_kind
from skopax.mail import flat
from skopax.messages import fold, msg
from skopax.slack import BLOCK_LIMIT, EMOJI, SCOPE_ERRORS, SECTION_LIMIT, SlackDown, SlackError, cut, esc, never_sent
from skopax.store import Store

log = logging.getLogger('skopax.tagreport')

RUN = 'tag-report'
MAIL_RUN = 'tag-report-mail'
MAIL_SHOWN = 25
FEATURE = 'tag-report'
KEY = 'tags.report-at'
PERIOD = 300
OFFSET = 45
SHOWN = 3
TAGS_MAX = BLOCK_LIMIT - 2
NOBODY = 'nobody'
TEXT_LIMIT = 3000
NAME_LIMIT = 60
FAILURE_TTL_S = 600
LOOKUP_TTL_S = 86400
UNKNOWN_TTL_S = 3600
WEEKEND = (5, 6)
SCOPE = 'users:read.email'
OWNERS_MAX = 20
PEOPLE_TTL_S = 6 * 3600
PEOPLE_MAX = 20
NAME_WARN_S = 3600
PEOPLE_WAIT_S = 3.0
QUERY_MIN = 2
QUERY_MAX = 100
SOURCES = ('skopax', 'responsible', 'slack')
STOPPED = ('progress', 'handled')
SEV_ORDER = {sev: i for i, sev in enumerate(severity.LEVELS)}
QUIET_KEPT = severity.CARD_LEVELS
DAY_H = 24
WINDOW_S = 2 * 3600
REPEAT_GAP_S = 20 * 3600
GONE_CHANNEL = ('not_in_channel', 'channel_not_found', 'is_archived', 'invalid_auth', 'account_inactive', 'token_revoked', 'missing_scope', 'not_allowed_token_type')


def valid_at(text) -> bool:
    return tags.report_at_ok(text)


def business_day(now: float) -> bool:
    return clock.local(now).weekday() not in WEEKEND


def slot(at: str, now: float) -> str | None:
    if not valid_at(at):
        return None
    hour, minute = (int(x) for x in at.split(':'))
    today = clock.local(now).date()
    for d in (today, today - dt.timedelta(days=1)):
        start = dt.datetime(d.year, d.month, d.day, hour, minute, tzinfo=clock.zone()).timestamp()
        if start <= now < start + WINDOW_S and d.weekday() not in WEEKEND:
            return d.isoformat()
    return None


def due(at: str, now: float) -> bool:
    return slot(at, now) is not None


def missed(at: str, now: float) -> str | None:
    if not valid_at(at) or not business_day(now) or clock.hm(now) < at or slot(at, now):
        return None
    return clock.day(now)


def board_cards(cards: list[dict], stopped: bool) -> list[dict]:
    by_key = {c.get('key'): c for c in cards}

    def grouped(a: dict) -> bool:
        host = by_key.get(a.get('parent')) if a.get('parent') and a.get('parent') != a.get('key') else None
        return bool(host) and (host.get('status') == 'open' or a.get('status') == 'cleared')

    def shown(a: dict) -> bool:
        if a.get('muted') or (a.get('triage') in STOPPED) != stopped or grouped(a):
            return False
        return a.get('status') == 'open' or (a.get('status') == 'cleared' and a.get('sev') in QUIET_KEPT)

    return [a for a in cards if shown(a)]


def board_open(cards: list[dict]) -> list[dict]:
    return board_cards(cards, False)


def board_taken(cards: list[dict]) -> list[dict]:
    return [a for a in board_cards(cards, True) if a.get('triage') == 'progress']


def board_link(base_url: str, tag: str) -> str:
    return f"{base_url.rstrip('/')}/#t=anoms/open/tag:{tag}"


def open_link(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/#t=anoms/open"


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


def counts(total: int, crit: int, warn: int, high: int = 0) -> str:
    split = [counted(kind, n) for kind, n in (('crit', crit), ('high', high), ('warn', warn)) if n]
    head = counted('open', total)
    return msg('tagreport.split', open=head, parts=', '.join(split)) if split else head


def shown_name(name, slack_name, email: str) -> str:
    return next((n for n in (str(name or '').strip(), str(slack_name or '').strip()) if n and '@' not in n), email.split('@')[0])


def person(email: str, names: dict[str, str], mention: Callable[[str], str | None]) -> str:
    found = mention(email)
    if found:
        return found
    return esc(shown_name(names.get(email), None, email))


def card_line(a: dict, base_url: str, now: float) -> str:
    name = f"#{a['num']}" if a.get('num') else cut(str(a.get('key') or ''), NAME_LIMIT)
    sev = a.get('sev') if a.get('sev') in SEV_ORDER else 'info'
    return msg('tagreport.card', link=card_url(base_url, a['key'], a.get('num')), name=link_text(name), emoji=EMOJI[sev], sev=msg(f'slack.sev.{sev}'),
               svc=esc(a.get('svc') or '-'), age=age(now - seen_at(a, now)))


def split_counts(crit: int, warn: int, high: int = 0) -> str:
    return ', '.join(counted(kind, n) for kind, n in (('crit', crit), ('high', high), ('warn', warn)) if n)


def of_sev(cards: list[dict], sev: str) -> int:
    return sum(1 for a in cards if a.get('sev') == sev)


def build(cards: list[dict], owners: dict[str, list[str]], names: dict[str, str], mention: Callable[[str], str | None], base_url: str, now: float,
          labels: dict[str, str]) -> dict | None:
    pool = information.cards_only(cards)
    shown = [a for a in board_open(pool) if a.get('sev') in information.CARD_SEVS]
    taken = [a for a in board_taken(pool) if a.get('sev') in information.CARD_SEVS]
    held = {tag: emails for tag, emails in owners.items() if tag in labels and emails}
    rows = []
    for tag, emails in held.items():
        mine = sorted((a for a in shown if tag in tags.ids(a)), key=lambda a: (SEV_ORDER.get(a.get('sev'), 3), seen_at(a, now)))
        busy = sum(1 for a in taken if tag in tags.ids(a))
        if mine or busy:
            rows.append((tag, emails, mine, (of_sev(mine, 'crit'), of_sev(mine, 'high')), busy))
    orphans = sorted((a for a in shown if not any(t in held for t in tags.ids(a))), key=lambda a: (SEV_ORDER.get(a.get('sev'), 3), seen_at(a, now)))
    if not orphans and not any(r[2] for r in rows):
        return None
    rows.sort(key=lambda r: (-r[3][0], -r[3][1], -len(r[2]), labels[r[0]].lower(), r[0]))
    head = msg('tagreport.head', date=clock.date(now, hm=False))
    blocks, lines, closing = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': head}}], [], None
    room = TAGS_MAX - (1 if orphans else 0)
    for tag, emails, mine, crit, busy in rows[:room]:
        link = board_link(base_url, tag)
        label = link_text(labels[tag])
        progress = msg('tagreport.progress', n=busy) if busy else ''
        if mine:
            found = counts(len(mine), crit[0], of_sev(mine, 'warn'), crit[1])
            line = msg('tagreport.tag', who=', '.join(person(e, names, mention) for e in emails), link=link, tag=label,
                       counts=msg('tagreport.with_progress', counts=found, progress=progress) if busy else found)
        else:
            line = msg('tagreport.tag_quiet', link=link, tag=label, counts=progress)
        body = [line] + [card_line(a, base_url, now) for a in mine[:SHOWN]]
        if len(mine) > SHOWN:
            body.append(msg('tagreport.more', link=link, n=len(mine) - SHOWN))
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': cut('\n'.join(body), SECTION_LIMIT)}})
        lines.append(line)
    if len(rows) > room:
        blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': msg('tagreport.more_tags', n=len(rows) - room)}]})
    if orphans:
        link = open_link(base_url)
        line = msg(f'tagreport.nobody.{"one" if len(orphans) == 1 else "many"}', link=link, n=len(orphans), parts=split_counts(of_sev(orphans, 'crit'), of_sev(orphans, 'warn'), of_sev(orphans, 'high')))
        body = [line] + [card_line(a, base_url, now) for a in orphans[:SHOWN]]
        if len(orphans) > SHOWN:
            body.append(msg('tagreport.more', link=link, n=len(orphans) - SHOWN))
        blocks.append({'type': 'section', 'block_id': NOBODY, 'text': {'type': 'mrkdwn', 'text': cut('\n'.join(body), SECTION_LIMIT)}})
        closing = line
    tail = f'\n{closing}' if closing else ''
    room_text = max(TEXT_LIMIT - len(head) - len(tail) - 1, 0)
    text = head + '\n' + cut('\n'.join(lines), room_text) + tail if lines else head + tail
    return {'text': text, 'blocks': blocks}


def mail_name(a: dict) -> str:
    title = cut(flat(a.get('label') or a.get('key') or '?'), NAME_LIMIT)
    return f"#{a['num']} {title}" if a.get('num') else title


def mail_row(a: dict, base_url: str, now: float, also: list[str] = ()) -> partmail.Row:
    sev = a.get('sev') if a.get('sev') in SEV_ORDER else 'info'
    name, sev_text = mail_name(a), msg(f'mail.sev.{sev}')
    rest = f" \u00b7 {cut(flat(a.get('svc') or '-'), NAME_LIMIT)} \u00b7 {age(now - seen_at(a, now))}" + (f" \u00b7 {msg('mail.tagreport.also', tags=', '.join(also))}" if also else '')
    return partmail.Row(f'{name} \u00b7 {sev_text}{rest}', card_url(base_url, a['key'], a.get('num')), lead=name, sev=sev, sev_text=sev_text, rest=rest)


def build_mails(cards: list[dict], people: dict[str, list[str]], base_url: str, now: float, labels: dict[str, str]) -> list[tuple[str, tuple[str, str, str]]]:
    pool = information.cards_only(cards)
    shown = [a for a in board_open(pool) if a.get('sev') in information.CARD_SEVS]
    taken = [a for a in board_taken(pool) if a.get('sev') in information.CARD_SEVS]
    by_tag: dict[str, list[dict]] = {}
    for tag in {t for owned in people.values() for t in owned}:
        by_tag[tag] = sorted((a for a in shown if tag in tags.ids(a)), key=lambda a: (SEV_ORDER.get(a.get('sev'), 3), seen_at(a, now)))
    out = []
    head = msg('mail.tagreport.head', date=clock.date(now, hm=False))
    for email, owned in people.items():
        rows = [(tag, by_tag[tag]) for tag in owned if by_tag.get(tag)]
        if not rows:
            continue
        rows.sort(key=lambda r: (-of_sev(r[1], 'crit'), -of_sev(r[1], 'high'), -len(r[1]), labels.get(r[0], r[0]).lower(), r[0]))
        order = [tag for tag, _ in rows]
        listed: set[str] = set()
        sections = []
        for tag, all_mine in rows:
            mine = [a for a in all_mine if a['key'] not in listed]
            listed.update(a['key'] for a in mine)
            if not mine:
                continue
            busy = sum(1 for a in taken if tag in tags.ids(a))
            found = counts(len(mine), of_sev(mine, 'crit'), of_sev(mine, 'warn'), of_sev(mine, 'high'))
            said = msg('tagreport.with_progress', counts=found, progress=msg('tagreport.progress', n=busy)) if busy else found
            link = board_link(base_url, tag)
            more = (partmail.Row(msg('mail.tagreport.more', n=len(mine) - MAIL_SHOWN), link),) if len(mine) > MAIL_SHOWN else ()
            shown_rows = tuple(mail_row(a, base_url, now, [flat(labels.get(t, t)) for t in order if t != tag and t in tags.ids(a)]) for a in mine[:MAIL_SHOWN])
            sections.append((tag, partmail.Section(f'{flat(labels.get(tag, tag))} \u00b7 {said}', link, shown_rows + more)))
        board = board_link(base_url, sections[0][0]) if len(sections) == 1 else open_link(base_url)
        title = msg(f'mail.tagreport.title.{"one" if len(listed) == 1 else "many"}', n=len(listed))
        out.append((email, partmail.compose(partmail.product_subject(title), head, title, [sec for _, sec in sections], board=board)))
    return out


class SlackIds:
    def __init__(self, slack, ttl: float = LOOKUP_TTL_S, clock: Callable[[], float] = time.monotonic, on_scope: Callable[[bool], None] = lambda missing: None,
                 flagged: Callable[[], bool] = lambda: False):
        self.slack = slack
        self.ttl = ttl
        self.clock = clock
        self.on_scope = on_scope
        self.flagged = flagged
        self._held: dict[str, tuple[float, str | None, str]] = {}
        self._failed: dict[str, float] = {}
        self._failures_told: set[str] = set()
        self._missing = False
        self._down_until = -FAILURE_TTL_S
        self._recheck = False
        self._told = False

    @property
    def missing(self) -> bool:
        return self._missing

    def start(self) -> Callable[[str], str | None]:
        self._recheck = self._stored()
        stopped = [False]
        return lambda email: self._mention(email, stopped)

    def _stored(self) -> bool:
        try:
            return bool(self.flagged())
        except Exception as e:
            log.warning('the Slack scope flag of the tag report was not read: %s', error_kind(e))
            return False

    def mention(self, email: str) -> str | None:
        return self._mention(email, None)

    def _waiting(self, email: str) -> bool:
        now = self.clock()
        failed = self._failed.get(email)
        return now < self._down_until or (failed is not None and now - failed < FAILURE_TTL_S)

    def _mention(self, email: str, stopped: list[bool] | None) -> str | None:
        email = str(email or '').strip().lower()
        if not email:
            return None
        held = self._held.get(email)
        last = held[1] if held else None
        if self.fresh(held) and not self._recheck:
            return last
        if stopped is not None and stopped[0]:
            return last
        if stopped is None and self._waiting(email):
            return last
        try:
            user = self.slack.user_by_email(email)
        except SlackError as e:
            self._failed_mention(e, email, stopped)
            return last
        self._failed.pop(email, None)
        found = f"<@{user['id']}>" if user else None
        self._held[email] = (self.clock(), found, str((user or {}).get('name') or '').strip())
        self._recheck = False
        self._scope(False)
        return found

    def _failed_mention(self, e: SlackError, email: str, stopped: list[bool] | None) -> None:
        code = e.code or error_kind(e)
        if stopped is not None:
            stopped[0] = True
        elif isinstance(e, SlackDown):
            self._down_until = self.clock() + FAILURE_TTL_S
        else:
            self._failed[email] = self.clock()
        if e.code in SCOPE_ERRORS:
            self._scope(True)
        elif stopped is not None:
            log.warning('Slack user lookups stopped for this report, responsibles are named without a ping: %s', code)
        elif code not in self._failures_told:
            self._failures_told.add(code)
            log.warning('a Slack mention lookup failed, the last known id or the name is used for %d s before the next try: %s', FAILURE_TTL_S, code)

    def name_now(self, email: str) -> str:
        email = str(email or '').strip().lower()
        if not email:
            return ''
        held = self._held.get(email)
        if self.fresh(held):
            return held[2]
        failed = self._failed.get(email)
        if failed is not None and self.clock() - failed < FAILURE_TTL_S:
            return held[2] if held else ''
        try:
            user = self.slack.user_by_email(email)
        except SlackDown:
            return held[2] if held else ''
        except SlackError as e:
            self._failed[email] = self.clock()
            code = e.code or error_kind(e)
            if code not in self._failures_told:
                self._failures_told.add(code)
                log.warning('Slack refused a name lookup for a card post, names wait %d s before the next try: %s', FAILURE_TTL_S, code)
            return held[2] if held else ''
        self._failed.pop(email, None)
        self._held[email] = (self.clock(), f"<@{user['id']}>" if user else None, str((user or {}).get('name') or '').strip())
        return self._held[email][2]

    def peek(self, email: str) -> str:
        held = self._held.get(str(email or '').strip().lower())
        if held is None:
            return 'unchecked'
        return 'found' if held[1] else 'unknown'

    def name(self, email: str) -> str:
        held = self._held.get(str(email or '').strip().lower())
        return held[2] if held else ''

    def fresh(self, held: tuple[float, str | None, str] | None) -> bool:
        return held is not None and self.clock() - held[0] < (self.ttl if held[1] else min(self.ttl, UNKNOWN_TTL_S))

    def check_all(self, emails: list[str]) -> None:
        for email in emails:
            email = str(email or '').strip().lower()
            if self.fresh(self._held.get(email)):
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
        if not missing:
            self._told = False
        elif not self._told:
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
        self._told: dict[tuple[str, str], float] = {}
        self.patience = PEOPLE_WAIT_S
        self._settled = threading.Event()
        self._settled.set()
        self._gave_up = False

    def email_of(self, name: str) -> str | None:
        wanted = folded_name(name)
        if not wanted or '@' in wanted:
            return None
        people, state = self.lookup()
        if state == 'loading' and not self._gave_up:
            if self._settled.wait(self.patience):
                people, state = self.lookup()
            else:
                self._gave_up = True
        found = {p['email'] for p in people if wanted in (folded_name(p.get('name')), folded_name(p.get('display')))}
        if len(found) == 1:
            return found.pop()
        now = self.clock()
        why = 'several Slack members have that name' if found else 'no Slack member has that name' if state == 'ok' else f'the Slack member list is {state}'
        slot = (wanted, why)
        last = self._told.get(slot)
        if last is None or now - last >= NAME_WARN_S:
            self._told[slot] = now
            log.warning('an assignee name of %d characters stays plain text in Slack, without a mention: %s', len(wanted), why)
        return None

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
                self._settled.clear()
                self._gave_up = False
        if start:
            try:
                self.spawn(lambda: self._build(token))
            except Exception as e:
                log.warning('the Slack member list could not be started: %s', error_kind(e))
                with self._guard:
                    self._busy = False
                    self._down_until = self.clock() + FAILURE_TTL_S
                self._settled.set()
        with self._guard:
            if self._people is not None:
                return self._people, 'ok'
            return [], 'loading' if self._busy else 'unavailable'

    def _build(self, token: str) -> None:
        try:
            self._load(token)
        finally:
            self._settled.set()

    def _load(self, token: str) -> None:
        try:
            found = self.slack.members()
        except Exception as e:
            log.warning('Slack members were not listed, the responsible picker offers Skopax people only for a while: %s', getattr(e, 'code', None) or error_kind(e))
            with self._guard:
                self._busy = False
                self._down_until = self.clock() + FAILURE_TTL_S
            return
        with self._guard:
            self._busy = False
            if token == self._for:
                self._people, self._at = found, self.clock()
        log.info('%d Slack members kept for the responsible picker', len(found))


def folded_name(text) -> str:
    return ' '.join(fold(str(text or '')).split())


def name_mentions(people: SlackPeople, ids: SlackIds) -> Callable[[str], str | None]:
    def found(name: str) -> str | None:
        email = people.email_of(name)
        return ids.mention(email) if email else None

    return found


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
                 on: Callable[[], bool], base_url: str, labels: Callable[[], dict[str, str]] = tags.all_tags, clock: Callable[[], float] = time.time,
                 left_out: Callable[[], set[str]] = set, mail: partmail.PartMail | None = None, mail_on: Callable[[], bool] = lambda: True):
        self.store = store
        self.mail = mail
        self.mail_on = mail_on
        self.slack = slack
        self.ids = ids
        self.owners = owners
        self.names = names
        self.at = at
        self.on = on
        self.base_url = base_url
        self.labels = labels
        self.clock = clock
        self.left_out = left_out
        self.skipped: str | None = None

    def held(self) -> dict[str, list[str]]:
        known, gone = self.labels(), self.left_out()
        out = {tag: [e for e in emails if isinstance(e, str) and '@' in e and e not in gone] for tag, emails in self.owners().items() if tag in known}
        return {tag: emails for tag, emails in out.items() if emails}

    def named(self, held: dict[str, list[str]]) -> dict[str, str]:
        names = self.names()
        return {e: shown_name(names.get(e), self.ids.name(e), e) for emails in held.values() for e in emails}

    @property
    def wanted(self) -> bool:
        return bool(self.on() and valid_at(self.at()) and self.held())

    @property
    def enabled(self) -> bool:
        return bool(self.slack.enabled and self.wanted)

    @property
    def mailing(self) -> bool:
        return self.mail is not None and bool(self.mail_on() and valid_at(self.at()) and self.mail.held() and self.mail.ready())

    @property
    def scheduled(self) -> bool:
        return self.enabled or self.mailing

    def run_once(self, now: float | None = None) -> dict | None:
        now = self.clock() if now is None else now
        try:
            return self._post(now) if self.enabled else None
        finally:
            try:
                self._mail(now)
            except Exception as e:
                log.warning('daily tag report mails stopped: %s', error_kind(e))

    def _mail(self, now: float) -> int:
        sender = self.mail.sender() if self.mail is not None and self.mail_on() and valid_at(self.at()) else None
        when = slot(self.at(), now) if sender is not None else None
        if when is None:
            return 0
        last = self.store.daily_mail_at(MAIL_RUN)
        if last is not None and 0 <= now - last < REPEAT_GAP_S:
            return 0
        day = f'{MAIL_RUN}:{when}'
        if not self.store.claim_run(day):
            return 0
        try:
            mails = build_mails(self.store.load().get('anomalies') or [], self.mail.people(), self.base_url, now, self.labels())
        except Exception as e:
            self.store.drop_run(day)
            log.warning('daily tag report mails not built, tried again on the next pass: %s', error_kind(e))
            return 0
        self.store.note_daily_mail(MAIL_RUN, now)
        if not mails:
            log.info('daily tag report: no responsible has an open card in their tags, nothing mailed')
            return 0
        sent = self.mail.send(sender, mails, 'daily tag report')
        self.store.add_activity('mail', 'server', f'Daily tag report mailed \u00b7 {sent} of {len(mails)} responsible{"" if len(mails) == 1 else "s"}')
        log.info('daily tag report mailed to %d of %d responsibles', sent, len(mails))
        return sent

    def _post(self, now: float) -> dict | None:
        when = slot(self.at(), now)
        if when is None:
            self._skip(now)
            return None
        last = self.store.tag_report_at()
        if last is not None and 0 <= now - last < REPEAT_GAP_S:
            return None
        day = f'{RUN}:{when}'
        if not self.store.claim_run(day):
            return None
        try:
            mention = self.ids.start()
            held = self.held()
            message = build(self.store.load().get('anomalies') or [], held, self.named(held), mention, self.base_url, now, self.labels())
        except Exception:
            self.store.drop_run(day)
            raise
        if message is None:
            log.info('daily tag report: no tag with responsibles has an open card, nothing posted')
            return None
        try:
            sent = self.slack.post(message)
        except SlackDown as e:
            if never_sent(e):
                self.store.drop_run(day)
                log.warning('daily tag report: the post did not reach Slack, it is tried again on the next pass while the window is open (%s)', error_kind(e))
            else:
                log.warning('daily tag report: Slack did not answer the post, so it may have gone out; it is not sent again today (%s)', error_kind(e))
            return None
        except SlackError as e:
            if e.code in GONE_CHANNEL:
                log.warning('daily tag report not posted today: Slack refused it (%s)', e.code)
                return None
            self.store.drop_run(day)
            raise
        self.store.note_tag_report(now)
        shown = sum(1 for b in message['blocks'] if b['type'] == 'section' and b.get('block_id') != NOBODY) - 1
        self.store.add_activity('slack', 'server', f'Daily tag report posted \u00b7 {shown} tag{"" if shown == 1 else "s"}')
        log.info('daily tag report posted for %d tags', shown)
        return sent | {'tags': shown}

    def _skip(self, now: float) -> None:
        day = missed(self.at(), now)
        if day is None or day == self.skipped:
            return
        self.skipped = day
        last = self.store.tag_report_at()
        if last is not None and clock.day(last) == day or self.store.has_run(f'{RUN}:{day}'):
            return
        log.info('daily tag report skipped today: the window %s to 2 h later has passed, it posts again on the next business day', self.at())
