import json
import logging
import re
import time
from collections.abc import Callable

import anthropic

from skopax import brand, clock, tracing
from skopax.ai import THINK_OFF, ModelSetting, key_for, maker, wrap
from skopax.aikey import ClientBox
from skopax.config import Setting
from skopax.engine import iso as epoch_iso, parse_iso
from skopax.errkind import kind as error_kind
from skopax.investigator import pricing, prompt
from skopax.investigator.gate import GATE_TIMEOUT
from skopax.kit.flags import SOFT, scan
from skopax.redact import REDACTED, redact_secrets
from skopax.slack import SlackError
from skopax.slackevents import FILE_SUBTYPE, MENTION, decode
from skopax.statenotes import who_of

log = logging.getLogger('skopax.threadsummary')
SOURCE = 'threadsummary'
THREAD_QUIET_S = 1800
SUMMARY_GAP_S = 1800
SUMMARY_DAILY_CAP = 6
PEOPLE_MIN = 3
THREAD_READ = 60
LINE_LIMIT = 400
ACTIVE_S = 86400
SWEEP_PERIOD = 300
SWEEP_OFFSET = 150
SWEEP_LIMIT = 20
MAX_TOKENS = 500
EFFORT = 'low'
BULLETS_MAX = 6
BULLET_LIMIT = 240
TEXT_LIMIT = 300
SOMEONE = 'someone'
NAME_TTL_S = 3600
OWN_ANSWER = ' (its own earlier answer, not a person)'
TOKEN = re.compile(r'(?<![\w-])(?:xox[abposr]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}'
                   r'|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})')
ORDER = re.compile(r'\bignore\s+(?:all\s+)?(?:your|my|the|these|those|any|its)\s+(?:(?:previous|prior|earlier|own)\s+)?instructions\b', re.I)
PEOPLE_SUBTYPES = (None, 'thread_broadcast', FILE_SUBTYPE)
SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['text', 'bullets'],
          'properties': {'text': {'type': 'string'}, 'bullets': {'type': 'array', 'items': {'type': 'string'}}}}


def email_names(operators) -> Callable[[str], str | None]:
    def name(user: str) -> str | None:
        email = operators.email_of(user)
        return who_of(email) if email else None
    return name


def masked(text: str) -> str:
    return redact_secrets(TOKEN.sub(REDACTED, text))


def tidy(text: object, limit: int) -> str:
    return masked(' '.join(str(text or '').split()))[:limit]


class ThreadSummaries:
    provider = Setting('ai_provider')
    model = ModelSetting('investigate_gate_model', 'gate')
    language = Setting('investigate_language')
    switch = Setting('slack_thread_summary')

    def __init__(self, store, slack, settings, client=None, clock=time.time, key: Callable[[], str] | None = None, names: Callable[[str], str | None] = lambda user: None,
                 halted: Callable[[float], str | None] = lambda now: None):
        self.store = store
        self.slack = slack
        self.settings = settings
        self.key = key or key_for(settings, lambda: self.provider)
        self.box = ClientBox(self.key, maker(lambda: self.provider, lambda k: anthropic.Anthropic(api_key=k, timeout=GATE_TIMEOUT, max_retries=0), GATE_TIMEOUT, 0), client,
                             lambda: self.provider)
        self.clock = clock
        self.names = names
        self.halted = halted
        self._names: dict[str, tuple[float, str]] = {}

    @property
    def on(self) -> bool:
        return bool(self.switch)

    def ready(self, now: float) -> bool:
        return self.on and bool(self.key()) and pricing.known(self.model) and self.halted(now) is None

    def seen(self, card: dict, ts: str, now: float) -> None:
        if self.on and card.get('key') and ts:
            self.store.mark_thread_seen(card['key'], ts, epoch_iso(now))

    def answered(self, action: dict) -> dict | None:
        s = action.get('slack') or {}
        if s.get('where') != 'card' or not s.get('channel') or not s.get('threadTs'):
            return None
        card = self.store.anomaly_by_post(s['channel'], s['threadTs'])
        return self.summarise(card, self.clock()) if card else None

    def sweep(self, now: float) -> int:
        done = 0
        held = epoch_iso(now - SUMMARY_GAP_S)
        for card in self.store.thread_marks(epoch_iso(now - THREAD_QUIET_S), held, clock.day(now), SUMMARY_DAILY_CAP, SWEEP_LIMIT):
            try:
                if parse_iso(card['threadSeenAt']) < now - ACTIVE_S:
                    self.store.clear_thread_mark(card['key'], card.get('threadSeenTs') or '')
                elif self.summarise(card, now) is not None:
                    done += 1
            except Exception as e:
                log.warning('thread summary of %s failed: %s', card.get('key'), error_kind(e))
        return done

    def _room(self, card: dict, now: float) -> bool:
        day = card.get('threadSummaryDay') or {}
        for last in ((card.get('threadSummary') or {}).get('at'), day.get('attemptAt')):
            if last and now - parse_iso(last) < SUMMARY_GAP_S:
                return False
        return not (day.get('day') == clock.day(now) and (day.get('n') or 0) >= SUMMARY_DAILY_CAP)

    def summarise(self, card: dict, now: float) -> dict | None:
        post = card.get('slackPost') or {}
        channel, root, key = post.get('channel'), post.get('ts'), card['key']
        if not channel or not root or not self.ready(now) or not self._room(card, now):
            return None
        earlier = card.get('threadSummary') or {}
        seen = card.get('threadSeenTs') or ''
        try:
            posts = self.slack.replies(channel, root, THREAD_READ + 1, oldest=earlier.get('upto'))
        except SlackError as e:
            log.warning('thread read for the summary of %s failed: %s', key, error_kind(e))
            return None
        lines, people, upto = self._lines(posts, root, now)
        if people < PEOPLE_MIN or not upto or earlier.get('upto') == upto:
            self.store.clear_thread_mark(key, max(upto, seen))
            return None
        attempt = epoch_iso(now)
        if not self.store.claim_thread_attempt(key, clock.day(now), attempt, epoch_iso(now - SUMMARY_GAP_S), SUMMARY_DAILY_CAP):
            return None
        made = self._ask(card, lines, [b for b in earlier.get('bullets') or [] if isinstance(b, str)], now)
        if made is None:
            return None
        said = '\n'.join([made[0], *made[1]])
        hits = {h['phrase'] for h in scan('thread summary', said, skip=SOFT)} | ({'ignore instructions'} if ORDER.search(said) else set())
        if hits:
            log.warning('thread summary of %s dropped: it repeats planted text (%s)', key, ', '.join(sorted(hits)))
            self.store.clear_thread_mark(key, max(upto, seen))
            return None
        link = post.get('permalink') or earlier.get('permalink') or self.slack.permalink(channel, root)
        summary = {'text': made[0], 'bullets': made[1], 'at': attempt, 'upto': upto, 'count': len(lines), 'permalink': link, 'model': made[2]}
        if not self.store.set_thread_summary(key, summary, attempt):
            return None
        self.store.clear_thread_mark(key, max(upto, seen))
        return summary

    def _lines(self, posts: list[dict], root: str, now: float) -> tuple[list[dict], int, str]:
        bot = self.slack.bot_user()
        lines, people, upto = [], 0, ''
        for m in posts:
            if not m.get('ts') or m.get('ts') == root:
                continue
            if bot and m.get('user') == bot:
                who = brand.product() + OWN_ANSWER
            elif m.get('bot_id') or not m.get('user') or m.get('subtype') not in PEOPLE_SUBTYPES:
                continue
            else:
                who = self._name(m['user'], now)
                people += 1
            said = MENTION.sub(lambda x: '@' + (brand.product() if x.group(1) == bot else self._name(x.group(1), now)), m.get('text') or '')
            lines.append({'who': who, 'text': masked(decode(said, bot))[:LINE_LIMIT]})
            upto = m['ts']
        return lines[-THREAD_READ:], people, upto

    def _name(self, user: str, now: float) -> str:
        hit = self._names.get(user)
        if hit and now - hit[0] < NAME_TTL_S:
            return hit[1]
        try:
            name = self.names(user) or SOMEONE
        except Exception as e:
            log.info('no name for a Slack user in a thread summary: %s', error_kind(e))
            return SOMEONE
        self._names[user] = (now, name)
        return name

    def _ask(self, card: dict, lines: list[dict], earlier: list[str], now: float) -> tuple[str, list[str], str] | None:
        try:
            ai = wrap(self.box.get())
            if ai is None:
                return None
            model = pricing.effective(ai.provider, self.model, 'gate')
            with tracing.opened('thread summary', provider=ai.provider, cards=card['key']):
                resp = ai.once(model, prompt.thread_summary_system(self.language), [{'role': 'user', 'content': prompt.thread_summary_input(card, lines, earlier)}],
                               MAX_TOKENS, EFFORT, THINK_OFF, SCHEMA)
        except Exception as e:
            log.warning('thread summary call for %s failed: %s', card['key'], error_kind(e))
            return None
        day = clock.day(now)
        served = resp.model or model
        try:
            try:
                priced = served
                usd = pricing.usd(served, resp.usage)
            except pricing.UnknownModel:
                priced = max(pricing.TABLES[ai.provider], key=lambda m: pricing.usd(m, resp.usage))
                usd = pricing.usd(priced, resp.usage)
                log.warning('thread summary of %s came from the unpriced model %s, charged at the rate of %s', card['key'], served, priced)
        except Exception as e:
            used = resp.usage
            log.warning('thread summary usage of %s on %s could not be priced (%s): %s input, %s output, %s cache read, %s cache write tokens', card['key'], served, error_kind(e),
                        getattr(used, 'input', '?'), getattr(used, 'output', '?'), getattr(used, 'cache_read', '?'), getattr(used, 'cache_write', '?'))
        else:
            self.store.add_spend(day, usd)
            self._ledger(day, ai.provider, served, priced, resp.usage, card['key'])
        if resp.stop == 'refusal':
            return None
        try:
            body = json.loads(next(iter(resp.texts)))
            bullets = [tidy(b, BULLET_LIMIT) for b in body['bullets'] if isinstance(b, str) and b.strip()][:BULLETS_MAX]
            gist = tidy(body.get('text'), TEXT_LIMIT)
        except (StopIteration, ValueError, KeyError, TypeError, AttributeError):
            log.warning('thread summary for %s was not the JSON asked for', card['key'])
            return None
        return (gist, bullets, model) if bullets else None

    def _ledger(self, day: str, provider: str, model: str, priced: str, usage, key: str) -> None:
        try:
            self.store.add_ledger({'source': SOURCE, 'day': day, 'provider': provider, 'model': model, 'effort': None if provider == pricing.OPENAI else EFFORT, 'runId': None, 'actionId': None,
                                   'keys': [key], 'by': None, **pricing.split(priced, usage)} | ({'pricedAs': priced} if priced != model else {}))
        except Exception as e:
            log.warning('spend ledger row for a thread summary not written: %s', error_kind(e))
