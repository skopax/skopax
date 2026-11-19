import dataclasses
import fnmatch
import hashlib
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from skopax import tags
from skopax.bounded import HOOK_TIMEOUT_S, Bound, TimedOut
from skopax.cardlink import card_url, chat_url
from skopax.contract import ECHO_FIELD
from skopax.engine import iso as epoch_iso, parse_iso
from skopax.errkind import Authored, kind
from skopax.kit.links import sendable
from skopax.messages import msg
from skopax.slack import ENTITY_CUT, SLACK_MARKS, cut, esc
from skopax.store import NotFound

log = logging.getLogger('skopax.routes')

SEV_RANK = {'info': 0, 'warn': 1, 'crit': 2}
DEFAULTS: dict[str, object] = {'repeat-days': 7.0, 'min-sev': 'warn', 'fixed-posts': False}
TAG = 'tag:'
MAX_CARDS = 10
WHAT_LIMIT = 300
WHY_LIMIT = 300
FIXED_HOLD_S = 600
MENTION_MAX = 20
LINK_HASH = 16
FIXED = ':fixed'
ACTIVITY = 'routes'
TRIES = 6
PASS_S = 60
RETRY_CAP_S = 3600
RETRY_KEEP_S = 7 * 86400
WORKER = 'routes'
BUSY = 'busy, an earlier post to it is still running'
GONE = 'it no longer serves'
STOPPED = 'the server stopped before it was posted'
NOT_TRIED = (BUSY, GONE, STOPPED)
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
ECHO_LINE_MAX = 2900
ECHO_TTL_S = 86400
ECHO_PASS_S = 30.0
ECHO_WAIT_S = 300.0
ECHO_WAIT_MAX_S = 21600.0
ECHO_TRIES = 5
MD_LINK = re.compile(r'\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(([^)]*)\)')


@dataclass(frozen=True)
class Echo:
    post: Callable[[str], dict | None]
    say: Callable[[dict, str], None]
    ready: Callable[[dict], bool] = lambda card: True


class Refused(Authored, Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


def said(card: dict) -> str:
    return str((card.get('business') or {}).get('text') or '').strip()


def unlinked(text: str) -> str:
    return MD_LINK.sub(r'\1 (\2)', text).replace('](', '] (')


def closed(card: dict, now: float) -> bool:
    try:
        return card.get('status') == 'cleared' and not card.get('expired') and now - parse_iso(card['clearedAt']) >= FIXED_HOLD_S
    except (KeyError, TypeError, ValueError):
        return False


def ours(post: object, names, plugin: str) -> bool:
    return isinstance(post, dict) and post.get('route') in names and post.get('plugin') in (plugin, None)


def last_told(posts: list, names, plugin: str) -> dict | None:
    mine = [p for p in posts if ours(p, names, plugin)]
    told = max((i for i, p in enumerate(mine) if not p.get('fixed')), default=-1)
    return mine[told] if told > max((i for i, p in enumerate(mine) if p.get('fixed')), default=-1) else None


def wait_after(tries: int) -> float:
    return 0.0 if tries <= 1 else float(min(PASS_S * 2 ** (tries - 1), RETRY_CAP_S))


def held(records: dict, route_id: str, link: str | None, key: str, now: float) -> bool:
    row = records.get((route_id, key))
    return row is not None and row.get('link') == link and (bool(row.get('gaveUp')) or float(row.get('due') or 0) > now)


def link_of(raw: str) -> str:
    return hashlib.sha256(raw.strip().encode()).hexdigest()[:LINK_HASH]


def base_id(route_id: str) -> str:
    return route_id[:-len(FIXED)] if route_id.endswith(FIXED) else route_id


def ordered(ids: Iterable[str]) -> list[str]:
    known = list(tags.all_tags())
    return sorted(set(ids), key=lambda t: (known.index(t) if t in known else len(known), t))


def teams_of(card: dict, route_tags: Iterable[str]) -> list[str]:
    mine = tags.ids(card)
    return [tags.label(t) for t in route_tags if t in mine]


def split(raw: object) -> list[str]:
    return [p.strip() for p in str(raw or '').split(',') if p.strip()]


def tag_id(term: str) -> str:
    term = term.strip().lower()
    return term[len(TAG):].strip() if term.startswith(TAG) else term


def number(card: dict) -> int | None:
    n = card.get('num')
    return n if isinstance(n, int) and not isinstance(n, bool) else None


def page_of(card: dict) -> str:
    url = str(card.get('url') or '')
    return url if url.startswith('https://') else ''


@dataclass(frozen=True)
class Route:
    id: str
    name: str
    plugin: str
    instance: str
    tags: frozenset[str]
    mentions: tuple[str, ...]
    link: str | None
    keys: tuple[str, ...] = ()
    min_sev: str = ''
    echo: str = ''

    @property
    def tagged(self) -> bool:
        return bool(self.tags) and not self.keys

    @property
    def business(self) -> bool:
        return self.tagged and not any(t in tags.TECHNICAL for t in self.tags)

    @property
    def title(self) -> str | None:
        return ', '.join(tags.label(t) for t in ordered(self.tags)) if self.tagged else None

    @property
    def label(self) -> str:
        return tags.label(next(iter(self.tags))) if len(self.tags) == 1 else self.name

    def hit(self, card: dict) -> bool:
        mine = tags.ids(card)
        key = str(card.get('key') or '')
        return any(t in mine for t in self.tags) or any(fnmatch.fnmatchcase(key, k) for k in self.keys)

    def takes(self, card: dict) -> bool:
        if self.business:
            return self.hit(card)
        return SEV_RANK.get(card.get('sev'), 0) >= SEV_RANK.get(self.min_sev, 1) and self.hit(card)

    def due(self, card: dict) -> bool:
        if card.get('status') != 'open' or card.get('muted') or card.get('triage') == 'handled' or card.get('parent') or card.get('noRoutes') is True:
            return False
        if (self.business and not said(card)) or not self.takes(card):
            return False
        since = str(card.get('firstSeen') or '')
        return not any(ours(p, {self.instance}, self.plugin) and str(p.get('at') or '') >= since for p in card.get('teamsPosts') or [])

    def fixed_due(self, card: dict, now: float) -> bool:
        said_it = card.get('business') or {}
        return (self.business and said_it.get('kind') == tags.INFO and bool(said(card)) and not card.get('muted') and not card.get('parent') and card.get('noRoutes') is not True and self.takes(card)
                and closed(card, now))


@dataclass(frozen=True)
class Parsed:
    route: Route | None
    problem: str | None
    unknown: tuple[str, ...]
    dropped: int


def route_of(plugin, item) -> Parsed:
    hook = plugin.outbound
    values = item.info().config
    link = None
    if hook.link_field:
        raw = str(values.get(hook.link_field) or '').strip()
        if not sendable(raw):
            return Parsed(None, msg('integrations.route_link'), (), 0)
        link = link_of(raw)
    known = tags.all_tags()
    wanted = list(dict.fromkeys(tag_id(t) for t in split(values.get(hook.tags_field))))
    unknown = tuple(t for t in wanted if t not in known)
    kept = frozenset(t for t in wanted if t in known)
    keys = tuple(dict.fromkeys(split(values.get(hook.keys_field)))) if hook.keys_field else ()
    given = split(values.get(hook.mentions_field)) if hook.mentions_field else []
    mentions = tuple(dict.fromkeys(e.lower() for e in given if EMAIL.fullmatch(e)))[:MENTION_MAX]
    dropped = sum(1 for e in given if not EMAIL.fullmatch(e))
    if not kept and not keys:
        return Parsed(None, msg('integrations.route_empty'), unknown, dropped)
    sev = str(values.get(hook.sev_field) or '') if hook.sev_field else ''
    echo = str(values.get(ECHO_FIELD) or '')
    route = Route(f'{plugin.id}/{item.name}', item.name, plugin.id, item.name, kept, mentions, link, keys, sev if sev in SEV_RANK else '', echo if echo in ('on', 'off') else '')
    return Parsed(route, None, unknown, dropped)


class Routes:
    def __init__(self, store, runtime, settings, clock: Callable[[], float] = time.time, attribution: Callable[[], dict | None] = lambda: None,
                 timeout: float = HOOK_TIMEOUT_S, base: Callable[[], str] | None = None, autostart: bool = False):
        self.store = store
        self.runtime = runtime
        self.settings = settings
        self.base = base
        self.clock = clock
        self.attribution = attribution
        self.timeout = timeout
        self._bound = Bound('outbound')
        self.autostart = autostart
        self.echo: Echo | None = None
        self.echo_budget = ECHO_PASS_S
        self.holder = uuid.uuid4().hex[:8]
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._queued: dict[str, tuple[dict[str, dict], dict[str, dict]]] = {}
        self._queued_at = 0.0
        self._running = False
        self._flying: dict[str, tuple[float, list[dict]]] = {}

    def setting(self, plugin_id: str, key: str) -> object:
        default = DEFAULTS[key]
        try:
            value = self.runtime.setting(plugin_id, key)
        except KeyError:
            return default
        except Exception as e:
            log.warning('route setting %s of plug-in %s not read, using its default: %s', key, plugin_id, kind(e))
            return default
        if key == 'min-sev':
            return value if value in SEV_RANK else default
        if key == 'fixed-posts':
            return value if isinstance(value, bool) else default
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

    def routes(self) -> list[Route]:
        return [r if r.min_sev else dataclasses.replace(r, min_sev=self.setting(r.plugin, 'min-sev')) for r in self.runtime.routes()]

    @property
    def enabled(self) -> bool:
        return bool(self.runtime.routes())

    def info(self) -> list[dict]:
        return [{'id': r.id, 'name': r.name, 'tags': ordered(r.tags), 'tagged': r.tagged, 'business': r.business, 'plugin': self.runtime.plugin_name(r.plugin)}
                for r in self.runtime.routes()]

    def echoing(self, route: Route) -> bool:
        return route.echo == 'on' or (route.echo != 'off' and getattr(self.settings, 'routes_slack_echo', False) is True)

    def inert(self, text: object) -> str:
        return esc(text).translate(SLACK_MARKS)

    def echo_entry(self, card: dict, route: Route, now: float) -> dict:
        mine = tags.ids(card)
        return {'id': route.id, 'route': route.name, 'plugin': self.runtime.plugin_name(route.plugin), 'tags': [t for t in ordered(route.tags) if t in mine], 'at': epoch_iso(now)}

    def echo_text(self, entries: list[dict]) -> str:
        items = []
        for e in sorted(entries, key=lambda e: str(e.get('id'))):
            named = [self.inert(t) for t in e.get('tags') or []]
            suffix = msg('routes.echo_tag' if len(named) == 1 else 'routes.echo_tags', tags=', '.join(named)) if named else ''
            items.append(msg('routes.echo_item', route=self.inert(e.get('route')), plugin=self.inert(e.get('plugin')), tags=suffix))
        return ENTITY_CUT.sub('\u2026', cut(msg('routes.echo_line', routes='; '.join(items)), ECHO_LINE_MAX))

    def _pend(self, sent: dict[str, list[str]], live: list[Route], now: float) -> None:
        if self.echo is None or not sent:
            return
        known = {r.id: r for r in live}
        told: dict[str, list[Route]] = {}
        for route_id, keys in sent.items():
            route = known.get(route_id)
            if route is None or not self.echoing(route):
                continue
            for key in keys:
                told.setdefault(key, []).append(route)
        for key, group in told.items():
            try:
                card = self.store.anomaly(key)
                self.store.route_echo_pend(key, [self.echo_entry(card, r, now) for r in group])
            except Exception as e:
                log.warning('route post of %s not queued for the Slack echo: %s', key, kind(e))

    def nudge(self) -> None:
        if self.autostart:
            self.later([], self.clock())

    def _drain_echo(self, now: float) -> None:
        if self.echo is None:
            return
        try:
            keys = self.store.route_echo_keys()
        except Exception as e:
            log.warning('route echoes to Slack not read: %s', kind(e))
            return
        deadline = time.monotonic() + self.echo_budget
        for key in keys:
            if time.monotonic() >= deadline:
                break
            try:
                self._echo_one(key, now)
            except Exception as e:
                log.warning('route echo of %s to Slack skipped: %s', key, kind(e))

    def _expired(self, entry: dict, now: float) -> bool:
        try:
            return now - parse_iso(str(entry.get('at') or '')) > ECHO_TTL_S
        except (TypeError, ValueError):
            return True

    def _echo_failed(self, key: str, ids: list[str], e: BaseException, kind_: str = 'post') -> None:
        fails = self.store.route_echo_fail(key, self.clock(), kind_)
        log.warning('route echo of %s to Slack failed: %s', key, kind(e))
        if fails >= ECHO_TRIES and ids:
            self.store.route_echo_drop(key, ids, reset=True)
            log.info('route echo of %s for %s dropped after %d failed tries', key, ', '.join(ids), fails)
            return
        try:
            self.store.add_activity(ACTIVITY, 'server', msg('routes.echo_failed', tries=fails, limit=ECHO_TRIES), keys=[key])
        except Exception as err:
            log.warning('the failed Slack echo of %s was not recorded: %s', key, kind(err))

    def _echo_post(self, key: str, ids: list[str], now: float) -> dict | None:
        echo = self.echo
        if not self.store.route_echo_claim(key, self.holder, self.clock(), 2 * self.timeout):
            return None
        try:
            post = self.store.anomaly(key).get('slackPost') or {}
            if not (post.get('ts') and post.get('channel')):
                post = self._bound.call(('echo', key), lambda: echo.post(key), self.timeout)
        except TimedOut as e:
            self._echo_failed(key, ids, e)
            return None
        except Exception as e:
            self.store.route_echo_release(key, self.holder)
            self._echo_failed(key, ids, e)
            return None
        self.store.route_echo_release(key, self.holder)
        return post if isinstance(post, dict) and post.get('ts') and post.get('channel') else None

    def _echo_one(self, key: str, now: float) -> None:
        echo = self.echo
        card = self.store.anomaly(key)
        state = card.get('routeEcho') or {}
        entries = [p for p in state.get('pending') or [] if isinstance(p, dict) and p.get('id')]
        stale = [p['id'] for p in entries if self._expired(p, now)]
        if stale:
            log.info('route echo of %s for %s was not told to Slack within a day', key, ', '.join(stale))
            self.store.route_echo_drop(key, stale)
            entries = [p for p in entries if p['id'] not in stale]
        if not entries:
            return
        ids = [p['id'] for p in entries]
        post = card.get('slackPost') or {}
        fails = int(state.get('fails') or 0)
        if fails and (state.get('failKind') == 'line' or not (post.get('ts') and post.get('channel'))) and self.clock() < float(state.get('failedAt') or 0) + min(ECHO_WAIT_S * 2 ** (fails - 1), ECHO_WAIT_MAX_S):
            return
        if not (post.get('ts') and post.get('channel')):
            if not echo.ready(card):
                return
            post = self._echo_post(key, ids, now)
            if post is None:
                return
        if not self._bound.free(('echo', key)):
            return
        fresh = self.store.route_echo_name(key, post['ts'], ids)
        if not fresh:
            return
        text = self.echo_text(fresh)
        try:
            self._bound.call(('echo', key), lambda: echo.say(post, text), self.timeout)
        except Exception as e:
            self._echo_failed(key, [], e, 'line')

    def _base(self) -> str:
        return str((self.base() if self.base is not None else getattr(self.settings, 'base_url', '')) or '').rstrip('/')

    def _attribution(self) -> dict | None:
        try:
            line = self.attribution()
        except Exception as e:
            log.warning('the attribution line is left out of route posts: %s', kind(e))
            return None
        if not isinstance(line, dict) or not isinstance(line.get('text'), str) or not line['text'].strip():
            return None
        return {'text': line['text'], 'href': line['href'] if isinstance(line.get('href'), str) else ''}

    def _card(self, card: dict) -> dict:
        return {'key': card.get('key'), 'svc': str(card.get('svc') or ''), 'ns': str(card.get('ns') or ''), 'sev': str(card.get('sev') or 'info'), 'label': str(card.get('label') or ''),
                'short': str(card.get('short') or ''), 'number': number(card)}

    def _message(self, kind_: str, title: str, card: dict, mention: Iterable[str], **extra) -> dict:
        base = self._base()
        out = {'kind': kind_, 'title': title, 'text': '', 'why': '', 'urgent': False, 'link': card_url(base, card['key'], number(card)), 'page': page_of(card),
               'chat': chat_url(base, card['key'], number(card)), 'mentions': list(mention), 'card': self._card(card), 'teams': [], 'kids': [], 'cards': [], 'more': 0,
               'board': f'{base}/#t=anoms', 'attribution': self._attribution()}
        return out | extra

    def business_message(self, card: dict, label: str, mention: Iterable[str], kids: Iterable[dict] = (), teams: Iterable[str] = ()) -> dict:
        base = self._base()
        given = card.get('business') or {}
        why = tags.clip(unlinked(str(given.get('why') or '')), WHY_LIMIT)
        info = given.get('kind') == tags.INFO
        why_line = [{'role': 'why', 'label': msg('routes.why'), 'text': why}] if why else []
        said_line = [{'role': 'info' if info else 'recommended', 'label': msg('routes.info' if info else 'routes.recommended'), 'text': unlinked(said(card))}]
        rows = [{'text': unlinked(said(k)), 'page': page_of(k), 'number': number(k), 'link': card_url(base, k['key'], number(k)) if number(k) else ''} for k in list(kids)[:MAX_CARDS]]
        return self._message('info' if info else 'action', msg('routes.title', label=label), card, mention, text=unlinked(said(card)), why=why,
                             urgent=card.get('sev') == 'crit', teams=list(teams), kids=rows, lines=said_line + why_line if info else why_line + said_line)

    def cards_message(self, cards: list[dict], mention: Iterable[str], label: str | None) -> dict:
        base = self._base()
        shown = cards[:MAX_CARDS]
        rows = [self._card(a) | {'what': tags.clip(a.get('what'), WHAT_LIMIT), 'link': card_url(base, a['key'], number(a)), 'chat': chat_url(base, a['key'], number(a))} for a in shown]
        title = msg('routes.title', label=label) if label else msg('routes.title_one') if len(cards) == 1 else msg('routes.title_many', count=len(cards))
        return self._message('card', title, shown[0], mention, text=rows[0]['what'], cards=rows, more=max(0, len(cards) - MAX_CARDS))

    def fixed_message(self, card: dict, label: str) -> dict:
        return self._message('fixed', msg('routes.title_fixed', label=label), card, (), text=unlinked(said(card)))

    def _target(self, route: Route):
        plugin = next((p for p in self.runtime.active() if p.id == route.plugin and p.outbound is not None), None)
        found = next((i for i in self.runtime.instances_of(route.plugin) if i.name == route.instance), None) if plugin is not None else None
        return None if found is None else (plugin, found)

    def post(self, route: Route, message: dict) -> bool:
        return self.attempt(route, message) is None

    def attempt(self, route: Route, message: dict) -> str | None:
        target = self._target(route)
        if target is None:
            log.warning('route %s post skipped: %s', route.id, GONE)
            return GONE
        plugin, found = target
        ctx = self.runtime.context(plugin, found, self.clock())
        started = time.monotonic()
        try:
            answer = self._bound.call((route.plugin, route.instance), lambda: plugin.outbound.post(ctx, message), self.timeout)
        except TimedOut as e:
            if time.monotonic() - started < self.timeout:
                log.warning('route %s post failed: %s', route.id, BUSY)
                return BUSY
            return self._failed(route, kind(e))
        except Exception as e:
            return self._failed(route, kind(e))
        if isinstance(answer, dict) and answer.get('ok') is True:
            self._outcome(route, None)
            return None
        status = answer.get('status') if isinstance(answer, dict) else None
        return self._failed(route, f'HTTP {status}' if isinstance(status, int) and not isinstance(status, bool) and status > 0 else 'no usable answer')

    def _failed(self, route: Route, reason: str) -> str:
        log.warning('route %s post failed: %s', route.id, reason)
        self._outcome(route, reason)
        return reason

    def _outcome(self, route: Route, reason: str | None) -> None:
        try:
            self.store.route_outcome(route.id, reason, route.link, epoch_iso(self.clock()))
        except Exception as e:
            log.warning('the outcome of route %s was not recorded: %s', route.id, kind(e))

    def _records(self, now: float) -> dict[tuple[str, str], dict]:
        try:
            self.store.route_retries_before(epoch_iso(now - RETRY_KEEP_S))
            rows = self.store.route_retries()
        except Exception as e:
            log.warning('route retries not read: %s', kind(e))
            return {}
        return {(r['route'], r['key']): r for r in rows if isinstance(r, dict) and isinstance(r.get('route'), str) and isinstance(r.get('key'), str)}

    def _save(self, records: dict, row: dict) -> None:
        records[(row['route'], row['key'])] = row
        try:
            self.store.route_retry(row)
        except Exception as e:
            log.warning('the retry of route %s for %s was not recorded: %s', row['route'], row['key'], kind(e))

    def _missed(self, records: dict, route_id: str, link: str | None, key: str, reason: str, now: float) -> None:
        row = records.get((route_id, key))
        tries = (int(row.get('tries') or 0) if row and row.get('link') == link else 0) + 1
        gave_up = tries >= TRIES
        self._save(records, {'route': route_id, 'key': key, 'link': link, 'tries': tries, 'due': now + wait_after(tries), 'reason': reason, 'at': epoch_iso(now), 'gaveUp': gave_up})
        if gave_up:
            log.warning('route %s gave up on %s after %d tries: %s', route_id, key, tries, reason)

    def _waits(self, records: dict, route_id: str, link: str | None, key: str, now: float, reason: str = BUSY) -> None:
        row = records.get((route_id, key))
        tries = int(row.get('tries') or 0) if row and row.get('link') == link else 0
        self._save(records, {'route': route_id, 'key': key, 'link': link, 'tries': tries, 'due': now, 'reason': reason, 'at': epoch_iso(now), 'gaveUp': False})

    def _done(self, records: dict | None, route_ids: list[str], key: str) -> None:
        if records is not None and not any((r, key) in records for r in route_ids):
            return
        for r in route_ids:
            (records or {}).pop((r, key), None)
        try:
            self.store.route_retry_done(route_ids, key)
        except Exception as e:
            log.warning('the retry record of %s was not cleared: %s', key, kind(e))

    def _changed(self) -> None:
        try:
            self.store.teams_told()
        except Exception as e:
            log.warning('route posts were stored but the state rev was not moved: %s', kind(e))

    def gave_up(self) -> list[dict]:
        live = {r.id: r for r in self.runtime.routes()}
        try:
            rows = self.store.route_retries()
        except Exception as e:
            log.warning('route retries not read: %s', kind(e))
            return []
        out: dict[str, dict] = {}
        for row in rows:
            route = live.get(base_id(str(row.get('route') or '')))
            if route is None or not row.get('gaveUp') or row.get('link') != route.link:
                continue
            item = out.setdefault(route.id, {'id': route.id, 'plugin': self.runtime.plugin_name(route.plugin), 'route': route.name, 'count': 0, 'reason': ''})
            item['count'] += 1
            item['reason'] = str(row.get('reason') or '')
        return list(out.values())

    def failure(self, plugin_id: str, instance: str) -> str | None:
        route_id = f'{plugin_id}/{instance}'
        route = next((r for r in self.runtime.routes() if r.id == route_id), None)
        if route is None:
            return None
        try:
            row = self.store.route_outcomes().get(route_id)
        except Exception as e:
            log.warning('the outcome of route %s was not read: %s', route_id, kind(e))
            return None
        if not row or not row.get('reason') or row.get('link') != route.link:
            return None
        return msg('routes.post_failed', route=instance, reason=row['reason'])

    def reset(self, plugin_id: str, instance: str, given_link: str | None = None) -> None:
        route_id = f'{plugin_id}/{instance}'
        if given_link is not None and not any(r.id == route_id and r.link == link_of(given_link) for r in self.runtime.routes()):
            return
        try:
            self.store.route_retry_again([route_id, route_id + FIXED])
            self.store.route_outcome_done(route_id)
        except Exception as e:
            log.warning('the retries of route %s were not cleared: %s', route_id, kind(e))

    def _stamp(self, route: Route, by: str, now: float, fixed: bool = False) -> dict:
        return {'route': route.instance, 'plugin': route.plugin, 'at': epoch_iso(now), 'by': by} | ({'fixed': True} if fixed else {})

    def noted(self, route: Route, cards: list[dict], by: str) -> list[str]:
        keys = [a['key'] for a in cards]
        self.store.add_activity(ACTIVITY, by, msg('integrations.route_posted', plugin=self.runtime.plugin_name(route.plugin), route=route.name, count=len(cards)), keys=keys)
        return keys

    def kids(self, card: dict, cards: list[dict]) -> list[dict]:
        try:
            found = self.store.children(card['key'])
        except Exception as e:
            log.warning('merged cards of %s not read for its route post: %s', card['key'], kind(e))
            found = [c for c in cards if c.get('parent') == card['key']]
        return [k for k in found if k.get('status') == 'open' and said(k)]

    def claim(self, card: dict, route: Route, post: dict) -> bool:
        try:
            return self.store.teams_claim(card['key'], route.instance, str(card.get('firstSeen') or ''), post, plugin=route.plugin, rev=False)
        except Exception as e:
            log.warning('route post of %s to %s not claimed: %s', card['key'], route.id, kind(e))
            return False

    def unclaim(self, card: dict, post: dict) -> None:
        try:
            self.store.teams_unclaim(card['key'], post, rev=False)
        except Exception as e:
            log.warning('route claim of %s not taken back: %s', card['key'], kind(e))

    def claim_fixed(self, card: dict, route: Route, post: dict) -> bool:
        try:
            return self.store.teams_claim(card['key'], route.instance, str((self.told(card, {route.instance}, route.plugin) or {}).get('at') or ''), post, fixed=True,
                                          plugin=route.plugin, rev=False)
        except Exception as e:
            log.warning('route fixed post of %s to %s not claimed: %s', card['key'], route.id, kind(e))
            return False

    def past(self, card: dict) -> list[dict]:
        if not card.get('firstSeen'):
            return []
        try:
            return self.store.past_teams_posts(card['key'], str(card['firstSeen']))
        except Exception as e:
            log.warning('earlier route posts of %s not read: %s', card['key'], kind(e))
            return []

    def told(self, card: dict, names, plugin: str) -> dict | None:
        own = [p for p in card.get('teamsPosts') or [] if ours(p, names, plugin)]
        return last_told(own or self.past(card), names, plugin)

    def closed_after_told(self, card: dict, route: Route) -> bool:
        told = self.told(card, {route.instance}, route.plugin)
        try:
            return told is not None and parse_iso(card['clearedAt']) > parse_iso(str(told.get('at') or ''))
        except (KeyError, TypeError, ValueError):
            return False

    def recent(self, group: list[Route], route: Route, card: dict, now: float, days: float) -> bool:
        if days <= 0:
            return False
        told = self.told(card, {r.instance for r in group if r.business == route.business}, route.plugin)
        return told is not None and str(told.get('at') or '') >= epoch_iso(now - days * 86400)

    def shared(self, live: list[Route]) -> list[list[Route]]:
        groups: dict[tuple, list[Route]] = {}
        for route in live:
            if route.tagged:
                groups.setdefault((route.plugin, route.link or route.id), []).append(route)
        return list(groups.values())

    def together(self, group: list[Route], cards: list[dict], by: str, now: float, due, claim, fixed: bool) -> tuple[list[dict], dict[str, list[dict]], list[tuple[dict, dict, Route]]]:
        taken: dict[str, list[dict]] = {}
        claims = []
        for route in group:
            post = self._stamp(route, by, now, fixed)
            for a in cards:
                if due(route, a) and claim(a, route, post):
                    taken.setdefault(route.id, []).append(a)
                    claims.append((a, post, route))
        seen, out = set(), []
        for a in cards:
            if a['key'] not in seen and any(a in mine for mine in taken.values()):
                seen.add(a['key'])
                out.append(a)
        return out, taken, claims

    def one_by_one(self, group: list[Route], cards: list[dict], taken: dict[str, list[dict]], claims: list[tuple[dict, dict, Route]], build, by: str, records: dict, now: float,
                   suffix: str = '') -> dict[str, list[str]]:
        posted = set()
        for a in cards:
            mine = [(card, post, r) for card, post, r in claims if card['key'] == a['key']]
            failed = self.attempt(group[0], build(a))
            if failed is None:
                posted.add(a['key'])
                self._done(records, [r.id + suffix for _, _, r in mine], a['key'])
                continue
            for card, post, r in mine:
                self.unclaim(card, post)
                if failed not in NOT_TRIED:
                    self._missed(records, r.id + suffix, r.link, a['key'], failed, now)
        if posted:
            self._changed()
        out = {}
        for r in group:
            mine = [a for a in taken.get(r.id, []) if a['key'] in posted]
            if mine:
                out[r.id + suffix] = self.noted(r, mine, by)
        return out

    def tagged(self, cards: list[dict], by: str = 'server', now: float | None = None, live: list[Route] | None = None, records: dict | None = None) -> dict[str, list[str]]:
        now = self.clock() if now is None else now
        records = self._records(now) if records is None else records
        sent: dict[str, list[str]] = {}
        for group in self.shared(self.routes() if live is None else live):
            plugin = group[0].plugin
            floor = SEV_RANK[self.setting(plugin, 'min-sev')]
            days = self.setting(plugin, 'repeat-days')
            label = ', '.join(dict.fromkeys(r.label for r in group))
            team_tags = ordered(t for r in group for t in r.tags)
            tech_tags = ordered(t for r in group if not r.business for t in r.tags)
            mention = tuple(dict.fromkeys(m for r in group for m in r.mentions))

            def loud(a: dict) -> bool:
                return SEV_RANK.get(a.get('sev'), 0) >= floor

            def named(a: dict, among: list[str]) -> str:
                return ', '.join(teams_of(a, among)) or label

            if self.setting(plugin, 'fixed-posts'):
                done, cleared, claims = self.together(group, cards, by, now, lambda r, a: loud(a) and r.fixed_due(a, now) and self.closed_after_told(a, r)
                                                      and not held(records, r.id + FIXED, r.link, a['key'], now), self.claim_fixed, True)
                sent.update(self.one_by_one(group, done, cleared, claims, lambda a: self.fixed_message(a, named(a, team_tags)), by, records, now, FIXED))
            due, taken, claims = self.together(group, cards, by, now, lambda r, a: loud(a) and r.due(a) and not self.recent(group, r, a, now, days)
                                               and not held(records, r.id, r.link, a['key'], now), self.claim, False)
            plain = {a['key'] for r in group if r.business for a in taken.get(r.id, [])}
            sent.update(self.one_by_one(group, due, taken, claims, lambda a: self.business_message(a, named(a, team_tags), mention, self.kids(a, cards), teams_of(a, team_tags))
                                        if a['key'] in plain else self.cards_message([a], mention, named(a, tech_tags)), by, records, now))
        return sent

    def push(self, items: list[dict], routes: list[Route] | None = None, by: str = 'server', now: float | None = None, live: list[Route] | None = None,
             records: dict | None = None, failed: dict[str, str] | None = None) -> dict[str, list[str]]:
        now = self.clock() if now is None else now
        sent = {}
        chosen = [r for r in (self.routes() if live is None else live) if not r.tagged] if routes is None else list(routes)
        if routes is None and records is None:
            records = self._records(now)
        for route in chosen:
            cards = [a for a in items if not a.get('muted') and route.takes(a) and not held(records, route.id, route.link, a['key'], now)] if routes is None else list(items)
            if route.business:
                among = ordered(route.tags)
                answers = [(a, self.attempt(route, self.business_message(a, ', '.join(teams_of(a, among)) or route.label, route.mentions, self.kids(a, items), teams_of(a, among))))
                           for a in cards]
                cards = [a for a, why in answers if why is None]
                missed = next((why for _, why in answers if why is not None), None)
            elif cards:
                missed = self.attempt(route, self.cards_message(cards, route.mentions, route.title))
                if missed is not None:
                    self._retry_later(records if routes is None else None, route, cards, missed, now)
                    cards = []
            else:
                missed = None
            if missed is not None and failed is not None:
                failed[route.id] = missed
            if cards:
                sent[route.id] = self._posted(route, cards, by, now)
                for a in cards:
                    self._done(records if routes is None else None, [route.id], a['key'])
        return sent

    def _retry_later(self, records: dict | None, route: Route, cards: list[dict], failed: str, now: float) -> None:
        if records is None or failed == GONE:
            return
        for a in cards:
            if failed == BUSY:
                self._waits(records, route.id, route.link, a['key'], now)
            else:
                self._missed(records, route.id, route.link, a['key'], failed, now)

    def _posted(self, route: Route, cards: list[dict], by: str, now: float) -> list[str]:
        for a in cards:
            try:
                self.store.teams_posted(a['key'], self._stamp(route, by, now))
            except Exception as e:
                log.warning('route post of %s not recorded on the card: %s', a['key'], kind(e))
        return self.noted(route, cards, by)

    def told_since(self, card: dict, route: Route, since: str) -> bool:
        return any(ours(p, {route.instance}, route.plugin) and not p.get('fixed') and str(p.get('at') or '') >= since for p in card.get('teamsPosts') or [])

    def _again(self, live: list[Route], by: str, now: float, records: dict) -> dict[str, list[str]]:
        known = {r.id: r for r in live if not r.tagged}
        waiting: dict[str, list[dict]] = {}
        for (route_id, key), row in list(records.items()):
            route = known.get(route_id)
            if route is None or (row.get('link') == route.link and (row.get('gaveUp') or float(row.get('due') or 0) > now)):
                continue
            try:
                card = self.store.anomaly(key)
            except NotFound as e:
                log.warning('route post of %s not tried again: %s', key, kind(e))
                self._done(records, [route_id], key)
                continue
            except Exception as e:
                log.warning('route post of %s not tried again: %s', key, kind(e))
                if getattr(e, 'status', None) == 404:
                    self._done(records, [route_id], key)
                continue
            if card.get('status') != 'open' or card.get('muted') or self.told_since(card, route, str(row.get('at') or '')):
                self._done(records, [route_id], key)
                continue
            waiting.setdefault(route_id, []).append(card)
        sent = {}
        for route_id, cards in waiting.items():
            route = known[route_id]
            failed = self.attempt(route, self.cards_message(cards, route.mentions, route.title))
            if failed is None:
                sent[route_id] = self._posted(route, cards, by, now)
                for a in cards:
                    self._done(records, [route_id], a['key'])
                continue
            self._retry_later(records, route, cards, failed, now)
        return sent

    def deliver(self, changed: list[dict], now: float, pushed: list[dict] = (), by: str = 'server') -> int:
        sent = self._deliver(changed, now, pushed, by)
        self._drain_echo(now)
        return sent

    def _deliver(self, changed: list[dict], now: float, pushed: list[dict], by: str) -> int:
        try:
            live = self.routes()
            if not live:
                return 0
            records = self._records(now)
            sent = self._again(live, by, now, records) | (self.push(list(pushed), by=by, now=now, live=live, records=records) if pushed else {}) | self.tagged(list(changed), by, now, live, records)
            self._pend(sent, live, now)
        except Exception as e:
            log.warning('route delivery skipped: %s', kind(e))
            return 0
        return sum(len(keys) for keys in sent.values())

    def later(self, changed: list[dict], now: float, pushed: list[dict] = (), by: str = 'server') -> None:
        if not self.autostart:
            self.deliver(list(changed), now, list(pushed), by)
            return
        with self._lock:
            cards, pushes = self._queued.setdefault(by, ({}, {}))
            for a in changed:
                cards[a['key']] = a
            for a in pushed:
                pushes[a['key']] = a
            self._queued_at = max(self._queued_at, now)
            start = not self._running
            self._running = True
        if start:
            threading.Thread(target=self._work, name=WORKER, daemon=True).start()

    def drain(self, timeout: float = 5.0) -> bool:
        with self._idle:
            return self._idle.wait_for(lambda: not self._running, timeout)

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._queued:
                    self._running = False
                    self._idle.notify_all()
                    return
                batches, now = self._queued, self._queued_at
                self._queued, self._queued_at = {}, 0.0
                self._flying = {by: (now, list(pushes.values())) for by, (_, pushes) in batches.items()}
            for by, (cards, pushes) in batches.items():
                with self._lock:
                    if by not in self._flying:
                        continue
                try:
                    self.deliver(list(cards.values()), now, list(pushes.values()), by)
                except Exception as e:
                    log.warning('route delivery failed: %s', kind(e))
                finally:
                    with self._lock:
                        self._flying.pop(by, None)

    def stop(self, timeout: float) -> int:
        drained = self.drain(timeout)
        with self._lock:
            queued, at = self._queued, self._queued_at
            self._queued, self._queued_at = {}, 0.0
            flying, self._flying = list(self._flying.values()), {}
            left = [(at, list(pushes.values())) for _, pushes in queued.values()] + ([] if drained else flying)
        left = [(now, pushes) for now, pushes in left if pushes]
        if not left:
            return 0
        try:
            live = [r for r in self.routes() if not r.tagged]
            records = self._records(self.clock())
            kept = 0
            for now, pushes in left:
                for route in live:
                    for a in pushes:
                        if a.get('muted') or not route.takes(a) or (route.id, a['key']) in records:
                            continue
                        self._waits(records, route.id, route.link, a['key'], now, STOPPED)
                        kept += 1
        except Exception as e:
            log.warning('route posts queued at shutdown were not kept for a retry: %s', kind(e))
            return 0
        if kept:
            log.warning('route posts not sent before shutdown were kept for a retry: %d', kept)
        return kept

    def send(self, key: str, route_id: str | None, by: str) -> dict:
        card = self.store.anomaly(key)
        live = self.routes()
        if route_id:
            chosen = next((r for r in live if r.id == route_id), None)
            if chosen is None:
                raise Refused(404, msg('integrations.route_unknown', route=route_id))
            if chosen.tagged and not chosen.hit(card):
                raise Refused(422, msg('integrations.route_untagged', tags=chosen.title))
            if chosen.business and not said(card):
                raise Refused(422, msg('integrations.route_unsaid', route=chosen.label))
            picked = [chosen]
        else:
            picked = [r for r in live if r.takes(card) and (not r.business or said(card))]
            if not picked:
                raise Refused(422, msg('integrations.route_none'))
        failed: dict[str, str] = {}
        sent = self.push([card], picked, by, failed=failed)
        for route_id in sent:
            self._done(None, [route_id], key)
        self._pend(sent, picked, self.clock())
        if sent:
            self.nudge()
        if not sent:
            route = picked[0]
            raise Refused(502, msg('integrations.route_failed', plugin=self.runtime.plugin_name(route.plugin), reason=failed.get(route.id) or GONE))
        return {'ok': True, 'sent': sorted(sent)}
