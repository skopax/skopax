import collections
import datetime as dt
import json
import logging
import re
import threading
import time
from typing import Callable

import anthropic

from skopax.ai import THINK_AUTO, ModelSetting, key_for, maker, wrap
from skopax.aikey import ClientBox
from skopax.config import Setting
from skopax import toolusage
from skopax.engine import iso, mute_for, parse_iso
from skopax import clock
from skopax.errkind import kind as error_kind
from skopax.investigator import pricing
from skopax.kit.elastic import LOG_DEFAULTS, level_filter, quoted
from skopax.messages import msg
from skopax.redact import TIMEOUT_MARK, redact

log = logging.getLogger(__name__)

PASS = 'selfreview'
ACCESS_KEY = ':access:'
BY = 'Claude (self-review)'
DAY_S = 86400
MAX_TOKENS = 2200
TIMEOUT = 120.0
TEXT_LIMIT = 3000
SAMPLE = 140
ERROR_SAMPLE = 220
ERROR_SAMPLES = 4
KEY_LIMIT = 90
TOP = 8
PREVIOUS = 3
SCRUB_LIMIT_S = 5.0
MENTION = re.compile(r'<[@!#][^>]*>|(?<![\w|/.-])@[A-Za-z0-9._-]+')
SYSTEM = """You write this service's daily self-review for the engineer who runs it. Use only the facts given (the last 24 h, some 7 d); never guess numbers.
Format: the first line is a one-sentence verdict with the key numbers. Then 3 to 5 bullets, each "- <problem with numbers> \u2192 <proposed fix>. Code." or "... No code." (say which). Verdict and bullets stay under 1500 characters. Plain English, no @mentions, names, emails or other personal data, no markdown headings.
Pick what is worth acting on: failed passes, reach errors, spend near the cap, failed runs, tool errors (the tool and a short sample), one card investigated many times, Slack posts nobody acted on, recurring cards without a ticket, stopped cards nobody addressed, ungrouped alerts, warnings or errors in this service's own logs.
After the bullets, when runs.toolErrors is not empty, add a block that starts with the line "Tool errors:" and names every tool in runs.toolErrors, one line each: "- <tool> \u00d7<count> (<where: Slack asks, cards, page asks>, last <last>): <what went wrong, in a few words, from the samples> \u2192 <fix>. Code." or "... No code." A wrong input the tool could resolve by itself or answer more clearly (a short project name, a folder given where a file is needed) is a code fix; a refusal that worked as designed says so with No code. The block is extra: at most 900 characters, and a tool named there needs no bullet of its own unless it is the day's main problem.
Read the previous reviews: do not repeat a suggestion whose numbers have not changed, put it in one line as "Still open: ...", and say when an earlier suggestion looks done.
ownChanges are this service's own changes since the previous review (commits on its master, each deployed within minutes), with their titles and times: a suggestion one of them covers is done (say "done in <title>"), and errors, runs or posts from before that deploy are not evidence it is still open. cardsRunMoreThanOnce carry each card's state now (known noise, muted, handled, a ticket): a card already handled that way needs no new suggestion. nodes give each node's status and since when: errors from an agent that has been Ready since before them are past. ownLogs give each message's first and last time: one whose last time is hours ago, before a change that addresses it, is past, not a current problem. If a source could not be read (logs), say so in the verdict."""


def review_slot_offset(hour: int, at: float) -> int:
    return int(hour) * 3600 - int(clock.local(at).utcoffset().total_seconds())


def review_offset(hour: int, at: float | None = None) -> int:
    return review_slot_offset(hour, time.time() if at is None else at) % DAY_S


def short(text, limit: int = SAMPLE) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).strip()[:limit]


def local_time(at: str) -> str | None:
    try:
        t = parse_iso(at)
        return f"{clock.local(t).strftime('%d %b %H:%M')} {clock.label(t)}"
    except (TypeError, ValueError):
        return None


def passed(status: object) -> bool:
    return isinstance(status, str) and (status == 'ok' or status.startswith('ok '))


def card_state(state: dict, key: str, now: float) -> str:
    a = next((x for x in state.get('anomalies') or [] if x.get('key') == key), None)
    if a is None:
        return 'gone from the page'
    m = mute_for(state, key, now)
    parts = [a.get('status') or '?']
    if m:
        parts.append('known noise' if m.get('kind') == 'known' else f"muted until {m.get('until')}")
    parts += [x for x in (a.get('triage'), a.get('ticket')) if x]
    return ', '.join(parts)


def runs_facts(runs: list[dict], card_cap: int | None = None, slack_cap: int | None = None, state_of: Callable[[str], str] | None = None) -> dict:
    errors: dict[str, list[str]] = collections.defaultdict(list)
    last: dict[str, str] = {}
    where: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    per_key = collections.Counter()
    capped = 0
    for r in runs:
        calls = r.get('toolCalls') or []
        cap = slack_cap if r.get('trigger') == 'slack' else card_cap
        if cap and len(calls) >= cap:
            capped += 1
        for c in calls:
            if c.get('error'):
                name = c.get('name') or '?'
                errors[name].append(short(c['error'], ERROR_SAMPLE))
                last[name] = max(last.get(name, ''), str(r.get('startedAt') or ''))
                where[name][r.get('trigger') or 'other'] += 1
        per_key.update(r.get('keys') or [])
    return {'count': len(runs), 'outcomes': dict(collections.Counter(r.get('outcome') or '?' for r in runs)),
            'triggers': dict(collections.Counter(r.get('trigger') or 'other' for r in runs)), 'usd': round(sum(float(r.get('usd') or 0) for r in runs), 2),
            'atToolCap': capped, 'toolErrors': {name: {'count': len(lines), 'samples': list(dict.fromkeys(lines))[:ERROR_SAMPLES], 'last': local_time(last[name]), 'runs': dict(where[name])}
                           for name, lines in sorted(errors.items(), key=lambda kv: -len(kv[1]))[:TOP]},
            'cardsRunMoreThanOnce': [{'key': short(k, KEY_LIMIT), 'runs': n, **({'now': state_of(k)} if state_of else {})} for k, n in per_key.most_common(TOP) if n > 1]}


def state_facts(anomalies: list[dict], since: str) -> dict:
    live = [a for a in anomalies if a.get('status') == 'open' and not a.get('parent') and not a.get('muted')]
    stopped = [a for a in anomalies if a.get('status') == 'cleared' and not a.get('triage') and not a.get('muted') and a.get('sev') in ('warn', 'crit')]
    recurring = [a for a in anomalies if a.get('recurringCount') and not a.get('muted') and not (a.get('triage') == 'progress' and a.get('ticket'))]
    posted = [a for a in anomalies if ((a.get('slackPost') or {}).get('at') or '') >= since]
    return {'open': dict(collections.Counter(a.get('sev') for a in live)),
            'stoppedNotAddressed': [short(a['key'], KEY_LIMIT) for a in stopped][:TOP],
            'recurringWithoutTicket': [{'key': short(a['key'], KEY_LIMIT), 'times': a['recurringCount']} for a in sorted(recurring, key=lambda a: -a['recurringCount'])][:TOP],
            'openAlertsNotGrouped': [short(a['key'], KEY_LIMIT) for a in live if a.get('src') in ('alert', 'grafana')][:TOP],
            'noAccess': [{'label': short(a.get('label'), SAMPLE), 'instance': a.get('cluster') or None} for a in anomalies if a.get('status') == 'open' and ACCESS_KEY in str(a.get('ranKey') or '')][:TOP],
            'slackPosts': {'cards': len(posted), 'actedOn': sum(1 for a in posted if a.get('triage') or a.get('muted')),
                           'bySource': dict(collections.Counter(a.get('src') for a in posted))}}


def self_service(settings) -> str:
    return settings.self_deploy.partition('/')[2] or settings.self_project.rsplit('/', 1)[-1]


def log_query(index: str, service: str, fields: dict | None = None) -> str:
    named = {**LOG_DEFAULTS, **{k: v for k, v in (fields or {}).items() if k in LOG_DEFAULTS and isinstance(v, str) and v.strip()}}
    return (f"FROM {index} | WHERE {quoted(named['service'])} == \"{service}\" AND @timestamp >= NOW() - 24 hours AND {level_filter(named['level'])} "
            f"| EVAL level = TO_UPPER({quoted(named['level'])}), msg = SUBSTRING({quoted(named['message'])}, 1, {SAMPLE}) "
            "| STATS n = COUNT(*), oldest = MIN(@timestamp), newest = MAX(@timestamp) BY level, msg | SORT n DESC | LIMIT 10")


def own_changes(deploys: list[dict], since: str, service: str) -> list[dict]:
    seen, out = set(), []
    for d in sorted(deploys, key=lambda d: d.get('at') or ''):
        if d.get('svc') != service or (d.get('at') or '') < since or d.get('toImage') in seen:
            continue
        seen.add(d.get('toImage'))
        by = d.get('imageBy') or {}
        out.append({'at': d.get('at'), 'image': d.get('toImage'), 'title': short(by.get('title'), 160) or None, 'commits': by.get('commits')})
    return out


def commit_changes(commits: list[dict]) -> list[dict]:
    rows = [{'at': c.get('committed_date') or c.get('created_at'), 'image': (c.get('short_id') or c.get('id') or '')[:8], 'title': short(c.get('title'), 160) or None} for c in commits]
    return sorted(rows, key=lambda r: r['at'] or '')


def _rebuilt(x, clean: Callable[[str], str]):
    if isinstance(x, str):
        return clean(x)
    if isinstance(x, dict):
        return {_rebuilt(k, clean): _rebuilt(v, clean) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_rebuilt(v, clean) for v in x]
    return x


def scrubbed(facts: dict) -> dict:
    deadline = time.monotonic() + SCRUB_LIMIT_S

    def clean(text: str) -> str:
        return redact(text) if time.monotonic() < deadline else TIMEOUT_MARK

    return _rebuilt(facts, clean)


def gather(store, info: dict, logs, now: float, commits: Callable[[str], list[dict]] | None = None, service: str = '', log_index: str = '', plugin_tools=None) -> dict:
    since, week = iso(now - DAY_S), iso(now - 7 * DAY_S)
    reviews = store.self_reviews()
    state = store.load()
    changed_since = min(since, (reviews[0].get('at') or since) if reviews else since)
    runs = store.review_runs(since)
    usage_day = toolusage.summary(store.tool_usage_runs(since), 1, since, None, plugin_tools)
    usage_week = toolusage.summary(store.tool_usage_runs(week), 7, week, None, plugin_tools)
    probe = (store.last_probe() or {}) if info.get('probe') is not False else {}
    intake = [x for x in store.list_intake(500) if (x.get('at') or '') >= since]
    actions = [a for a in store.list_actions(None, 500) if (a.get('requestedAt') or '') >= since]
    investigate = info.get('investigate') or {}
    out = {'window': {'from': since, 'to': iso(now)},
           'passesNotOk': {k: (v or {}).get('status') for k, v in (info.get('passes') or {}).items() if not passed((v or {}).get('status'))},
           'reachErrors': [{'target': r.get('name'), 'plugin': r.get('plugin'), 'instance': r.get('instance'), 'error': short(r.get('error'), 80)}
                           for r in probe.get('targets') or [] if isinstance(r, dict) and not r.get('ok')],
           'spend': {'today': investigate.get('spentToday'), 'cap': investigate.get('cap'), 'mode': investigate.get('mode'), 'stopped': bool(investigate.get('stopped'))},
           'runs': runs_facts(runs, investigate.get('maxToolCalls'), investigate.get('slackMaxToolCalls'), lambda key: card_state(state, key, now)),
           'toolCalls': {'day': {'calls': usage_day['calls'], 'errors': usage_day['errors']}, 'week': {'calls': usage_week['calls'], 'errors': usage_week['errors']},
                         'weekErrorTools': [{'tool': t['name'], 'calls': t['calls'], 'errors': t['errors']} for t in sorted(usage_week['tools'], key=lambda t: -t['errors']) if t['errors']][:TOP]},
           'actions': dict(collections.Counter(a.get('type') for a in actions)),
           'slackIntake': dict(collections.Counter(x.get('decision') for x in intake)),
           'cards': state_facts(state.get('anomalies') or [], since),
           'nodes': [{'name': n.get('name'), 'status': n.get('status'), 'since': n.get('since'), 'error': 'not answering' if n.get('status') == 'NotReady' else None}
                     for n in (state.get('nodes') if isinstance(state.get('nodes'), list) else []) if isinstance(n, dict)],
           'ownChanges': own_changes(store.deploys_since(changed_since), changed_since, service),
           'previousReviews': [{'at': r.get('at'), 'text': short(r.get('text'), 2400)} for r in reviews[:PREVIOUS]]}
    if commits is not None:
        try:
            out['ownChanges'] = commit_changes(commits(changed_since))
        except Exception as e:
            log.warning('self-review: own commits not read, using the deploy rows: %s', error_kind(e))
    if logs is not None and getattr(logs, 'enabled', False):
        try:
            out['ownLogs'] = [{'level': r.get('level'), 'count': r.get('n'), 'first': r.get('oldest'), 'last': r.get('newest'), 'message': short(r.get('msg'))}
                                  for r in logs.query(log_query(log_index, service, getattr(logs, 'fields', None)))]
        except Exception as e:
            out['ownLogs'] = {'error': msg('selfreview.logs_unread', kind=error_kind(e))}
    else:
        out['ownLogs'] = {'error': 'the logs source is off'}
    return scrubbed(out)


def tidy(text: str) -> str:
    return MENTION.sub('', redact(text)).strip()[:TEXT_LIMIT]


class SelfReview:
    provider = Setting('ai_provider')
    model = ModelSetting('selfreview_model', 'review')
    cap = Setting('investigate_daily_usd')

    def __init__(self, store, settings, facts: Callable[[float], dict], save: Callable[[str], dict], client=None, clock: Callable[[], float] = time.time,
                 key: Callable[[], str] | None = None):
        self.store = store
        self.settings = settings
        self.hour = settings.selfreview_hour
        self.key = key or key_for(settings, lambda: self.provider)
        self.facts = facts
        self.save = save
        self.box = ClientBox(self.key, maker(lambda: self.provider, lambda k: anthropic.Anthropic(api_key=k, timeout=TIMEOUT, max_retries=1), TIMEOUT, 1), client, lambda: self.provider)
        self.clock = clock
        self._thread: threading.Thread | None = None

    @property
    def configured(self) -> bool:
        return (bool(self.key()) or self.box.injected) and self.hour >= 0 and bool(self.model) and bool(self.settings.self_project)

    @property
    def enabled(self) -> bool:
        return self.configured and bool(self.settings.self_review_enabled)

    def _client(self):
        return self.box.get()

    def kick(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._thread = threading.Thread(target=self._safe_run, args=(self.clock(),), name=PASS, daemon=True)
        self._thread.start()
        return True

    def reviewed(self, now: float) -> bool:
        return any(r.get('by') == BY and r.get('at') and clock.day(parse_iso(r['at'])) == clock.day(now) for r in self.store.self_reviews())

    def scheduled(self, now: float) -> bool:
        if not self.enabled or self.reviewed(now) or not self.kick():
            return False
        self._thread.join()
        return True

    def _safe_run(self, now: float) -> None:
        try:
            self.run(now)
        except Exception as e:
            log.error('self-review failed: %s', error_kind(e))
            self.store.add_activity('review', 'server', f'Self-review failed: {error_kind(e)}'[:300])

    def run(self, now: float) -> dict | None:
        day = clock.day(now)
        if self.store.spent(day) >= self.cap:
            self.store.add_activity('review', 'server', f'Self-review skipped: the daily cap of ${self.cap:g} is spent')
            return None
        ai = wrap(self._client())
        if ai is None:
            self.store.add_activity('review', 'server', msg('selfreview.no_key_openai' if self.provider == pricing.OPENAI else 'selfreview.no_key'))
            return None
        facts = self.facts(now)
        model = pricing.effective(ai.provider, self.model, 'review')
        resp = ai.once(model, SYSTEM, [{'role': 'user', 'content': 'Facts (JSON):\n' + json.dumps(facts, ensure_ascii=False, default=str)}], MAX_TOKENS, 'medium', THINK_AUTO)
        usd = pricing.usd(model, resp.usage)
        self.store.add_spend(day, usd)
        try:
            self.store.add_ledger({'source': PASS, 'day': day, 'provider': ai.provider, 'model': model, 'effort': 'medium', 'runId': None, 'actionId': None, 'keys': [], 'by': BY,
                                   **pricing.split(model, resp.usage)})
        except Exception as e:
            log.warning('spend ledger row for the self-review not written: %s', error_kind(e))
        if resp.stop == 'refusal':
            raise RuntimeError('the review model refused')
        text = tidy(''.join(resp.texts))
        if not text:
            raise RuntimeError('the review model wrote nothing')
        saved = self.save(text)
        return {**saved, 'usd': round(usd, 4)}
