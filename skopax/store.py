import contextlib
import datetime as dt
import functools
import logging
import re
import hashlib
import threading
import time
import uuid
from typing import Callable, Iterator

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.database import Database
from pymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure, PyMongoError
from pymongo.results import UpdateResult

from skopax import causes, history
from skopax.actors import is_token
from skopax.errkind import Authored, kind as error_kind
from skopax.tags import FIELDS as TAG_FIELDS

log = logging.getLogger('skopax.store')

STATE_ID = 'current'
PAGE_SCAN_PREFIX = 'page-scan:'
PLUGIN_MEMO_PREFIX = 'plugin-memo:'
PLUGIN_NO_INSTANCE = '-'
PLUGIN_MEMO_KEEP_DAYS = 90
TEAMS_KEEP = 10
FIX_KEEP = 20
CARD_COUNTER = 'card'
CARD_ACTION_TYPES = ('dive', 'take', 'resolve', 'mute', 'unmute', 'reopen', 'link', 'severity', 'known', 'group', 'ticket')
POST_STAMPS = ('reminder', 'clearNote', 'update', 'stateNote', 'head', 'drawn')
ECHO_KEEP = 20
MUTED_PRESET = history.MUTED_PRESET
PROBE_ID = 'probe'
PROXY_ID = 'proxy-warning'
SLACK_SCOPES_ID = 'slack-scopes'
VERSION_CHECK_ID = 'version-check'
SEVERITIES = ('crit', 'warn', 'info')
CLAIM_ATTEMPTS = 3
WRITE_LEASE_S = 30
SETTLE_FIRST_S = 0.002
SETTLE_MAX_S = 0.2
HOLD_WAIT_S = 2 * WRITE_LEASE_S
MERGED = ('sources', 'serverPasses')
PREPENDED = ('history', 'serverPushes')
AUTO_SEND_MINUTES = 0
AUTO_SEND_SEVERITIES = ('crit', 'warn')
AUTO_PENDING = {'autoSend.claimedAt': {'$exists': False}, 'autoSend.holdBy': {'$exists': False}}
AUTO_RETRIES = 3
AUTO_RETRY_S = 600
UNLINKED_POST = {'slackPost.ts': {'$exists': True}, 'slackPost.permalink': {'$exists': False}}
ERROR_LIMIT = 300
RUN_KEEP_DAYS = 7
CHECK_RUN_KEEP_DAYS = 31
CHECK_RUN_LIMIT = 20000
CHECK_ERROR_LIMIT = 300
ACTIVITY_KEEP_DAYS = 7
ACTIVITY_TEXT_LIMIT = 300
DEPLOY_IMAGE = [('ns', ASCENDING), ('svc', ASCENDING), ('toImage', ASCENDING)]
REDEPLOY_GAP_S = 2 * 3600
TRIAGE_STATES = ('investigating', 'dived', 'progress', 'handled')
LEASE_ID = 'investigator'
STOP_ID = 'investigator-stop'
SMALL_TALK_ID = 'small-talk'
GATE_EXAMPLES_ID = 'gate-examples'
POSTDEPLOY_IGNORE_ID = 'postdeploy-ignore'
SELF_REVIEW_ID = 'self-review'
SELF_REVIEW_KEEP = 30
INVESTIGATION_KEEP_DAYS = 365
SLACK_SEEN_KEEP_DAYS = 7
SPEND_KEEP_DAYS = 90
SCREENSHOT_KEEP_DAYS = 30
NOTICE_KEEP_DAYS = 30
NOTICE_PAGE_MAX = 100
PAST_RUN_FIELDS = {'startedAt': 1, 'trigger': 1, 'keys': 1, 'ask': 1, 'recap': 1, 'toolCalls': 1}
LEDGER_ROW_LIMIT = 50_000
RUN_COST_FIELDS = {'trigger': 1, 'outcome': 1, 'startedAt': 1, 'endedAt': 1, 'keys': 1, 'ask.text': 1, 'report.summary': 1, 'toolCalls.name': 1}
RUN_FIELDS = {'keys': 1, 'startedAt': 1, 'outcome': 1, 'trigger': 1}
USAGE_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
USAGE_RUN_LIMIT = 2000
TOOL_USAGE_FIELDS = {'trigger': 1, 'startedAt': 1, 'toolCalls.name': 1, 'toolCalls.ms': 1, 'toolCalls.error': 1, 'toolCalls.truncated': 1, 'toolCalls.cluster': 1, 'toolCalls.op': 1}
USAGE_TOP_KEYS = 10
USAGE_TOP_SLOW = 5
REQUEST_FIELDS = {'source': 1, 'text': 1, 'status': 1, 'requestedAt': 1, 'requestedBy': 1, 'result': 1, 'cards': 1, 'doneAt': 1, 'failedAt': 1, 'cancelledAt': 1,
                  'slack.channel': 1, 'slack.channelName': 1, 'slack.outside': 1, 'slack.permalink': 1, 'slack.where': 1, 'slack.threadTs': 1}
REQUEST_RUN_FIELDS = {'actionId': 1, 'outcome': 1, 'mode': 1, 'startedAt': 1, 'usd': 1, 'recap': 1, 'toolCalls.name': 1}
RUN_PAGE_FIELDS = {'mode': 1, 'trigger': 1, 'actionId': 1, 'ask': 1, 'keys': 1, 'startedAt': 1, 'outcome': 1, 'usd': 1, 'recap': 1, 'writes': 1, 'flags': 1, 'toolCalls.name': 1, 'screenshots': 1,
                   'proposal': 1}
BACKFILL_RUN_FIELDS = {'keys': 1, 'startedAt': 1, 'endedAt': 1, 'trigger': 1, 'outcome': 1, 'recap': 1, 'writes': 1, 'ask': 1, 'usd': 1}
BACKFILL_ACTION_FIELDS = {'type': 1, 'key': 1, 'status': 1, 'requestedAt': 1, 'requestedBy': 1, 'reason': 1, 'ticket': 1, 'result': 1}
INDEX_OPTIONS_CONFLICT = 85
INDEX_NOT_FOUND = 27
OPEN_ACTIONS = ('requested', 'processing')
CHAT_FIELDS = {'text': 1, 'status': 1, 'requestedAt': 1, 'doneAt': 1, 'failedAt': 1, 'result': 1, 'cards': 1, 'chat': 1}
CHAT_RUN_FIELDS = {'actionId': 1, 'outcome': 1, 'recap': 1, 'usd': 1, 'cardAction': 1, 'startedAt': 1}
CHAT_SCAN = 1000
CHAT_RUN_PUBLIC = ('id', 'mode', 'trigger', 'pod', 'room', 'startedAt', 'endedAt', 'outcome', 'usd', 'turns', 'model', 'effort', 'models', 'usage')
NOT_CHAT = {'trigger': {'$ne': 'chat'}}
CARD_RUNS_DAYS = 30
META_OWN = ('anomalies', 'rev', 'updatedAt', '_id', 'writing')
CHAT_ERROR = '(private chat)'
CHAT_TITLE = 80
ACTION_MOVES_FROM = {
    'requested': (),
    'processing': ('requested',),
    'done': ('requested', 'processing'),
    'failed': ('requested', 'processing'),
    'cancelled': ('requested',),
}


class Conflict(Authored, Exception):
    def __init__(self, current: int):
        super().__init__(f'state is at rev {current}')
        self.current = current


class NotFound(Authored, Exception):
    pass


class ActionMoved(Authored, Exception):
    def __init__(self, status: str):
        super().__init__(f'action is already {status}')
        self.status = status


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


class ChatPrivate(Authored, Exception):
    pass


def chat_title(ask: dict) -> str:
    text = ' '.join(str(ask.get('text') or '').split())
    return (f'Slack · {text}' if (ask.get('chat') or {}).get('dm') else text)[:CHAT_TITLE]


def new_id() -> str:
    return uuid.uuid7().hex


def _without_ids(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in ('_id', 'pos')}


def _with_id(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != '_id'}
    out['id'] = doc['_id']
    return out


def draft_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


AUTO_SEND_BY = ('server', 'Claude (pod)')


def auto_sender(by) -> bool:
    return is_token(by) or by in AUTO_SEND_BY


def auto_send(draft: dict, now: dt.datetime) -> dict:
    return {'at': iso(now + dt.timedelta(minutes=AUTO_SEND_MINUTES)), 'draftAt': draft['at'], 'draftHash': draft_hash(draft['text'])}


def _auto_stamp(auto: dict) -> str:
    return max(str(auto.get(f) or '') for f in ('claimedAt', 'holdAt', 'failedAt'))


def keep_slack_state(incoming: dict, stored: dict) -> dict:
    out = dict(incoming)
    post, old_post = incoming.get('slackPost') or {}, stored.get('slackPost') or {}
    if old_post.get('at', '') > post.get('at', ''):
        out['slackPost'] = old_post
    elif old_post.get('ts') and old_post.get('ts') == post.get('ts'):
        newer = {f: old_post[f] for f in POST_STAMPS if (old_post.get(f) or {}).get('at', '') > (post.get(f) or {}).get('at', '')}
        if newer:
            out['slackPost'] = {**post, **newer}
    if 'teamsPosts' in stored:
        out['teamsPosts'] = stored['teamsPosts']
    elif stored:
        out.pop('teamsPosts', None)
    if 'routeEcho' in stored:
        out['routeEcho'] = stored['routeEcho']
    if len(stored.get('fixPosts') or []) > len(incoming.get('fixPosts') or []):
        out['fixPosts'] = stored['fixPosts']
    if (stored.get('tagsAt') or '') > (incoming.get('tagsAt') or ''):
        for f in TAG_FIELDS:
            out[f] = stored.get(f)
    auto, old_auto = incoming.get('autoSend') or {}, stored.get('autoSend') or {}
    if old_auto.get('draftAt') and old_auto.get('draftAt') == auto.get('draftAt') and _auto_stamp(old_auto) > _auto_stamp(auto):
        out['autoSend'] = old_auto
    return out


def changes(old: dict, new: dict) -> dict:
    sets = {k: v for k, v in new.items() if old.get(k) != v}
    unsets = {k: '' for k in old if k not in new}
    return ({'$set': sets} if sets else {}) | ({'$unset': unsets} if unsets else {})


def _path_safe(key) -> bool:
    return isinstance(key, str) and bool(key) and '.' not in key and not key.startswith('$')


def _per_key(name: str, old, new, sets: dict, unsets: dict) -> bool:
    if not isinstance(old, dict) or not isinstance(new, dict) or not all(_path_safe(k) for k in (*old, *new)):
        return False
    sets.update({f'{name}.{k}': v for k, v in new.items() if k not in old or old[k] != v})
    unsets.update({f'{name}.{k}': '' for k in old if k not in new})
    return True


def _prepended(old, new) -> dict | None:
    if not isinstance(old, list) or not isinstance(new, list):
        return None
    for i in range(len(new) + 1):
        if new[i:] == old[:len(new) - i]:
            return {'$each': new[:i], '$position': 0} | ({'$slice': len(new)} if i + len(old) > len(new) else {})
    return None


def meta_changes(old: dict, new: dict) -> dict:
    sets, unsets, pushes = {}, {}, {}
    for k, v in new.items():
        if k in old and old[k] == v:
            continue
        if k in old and k in MERGED and _per_key(k, old[k], v, sets, unsets):
            continue
        push = _prepended(old[k], v) if k in old and k in PREPENDED else None
        if push is None:
            sets[k] = v
        else:
            pushes[k] = push
    unsets.update({k: '' for k in old if k not in new})
    return ({'$set': sets} if sets else {}) | ({'$unset': unsets} if unsets else {}) | ({'$push': pushes} if pushes else {})


def draft_update(draft: dict | None, auto: dict | None) -> dict:
    if not draft:
        return {'$unset': {'slackDraft': '', 'autoSend': ''}}
    if auto:
        return {'$set': {'slackDraft': draft, 'autoSend': auto}}
    return {'$set': {'slackDraft': draft}, '$unset': {'autoSend': ''}}


def ttl_index(coll, field: str, seconds: int) -> None:
    try:
        coll.create_index(field, expireAfterSeconds=seconds)
    except OperationFailure as e:
        if e.code != INDEX_OPTIONS_CONFLICT:
            raise
        try:
            coll.database.command({'collMod': coll.name, 'index': {'keyPattern': {field: 1}, 'expireAfterSeconds': seconds}})
        except OperationFailure as denied:
            recreate_ttl_index(coll, field, seconds, denied)


def recreate_ttl_index(coll, field: str, seconds: int, why: OperationFailure) -> None:
    try:
        coll.drop_index([(field, ASCENDING)])
    except OperationFailure as e:
        if e.code != INDEX_NOT_FOUND:
            log.warning('%s keeps its old TTL on %s, the change to %d s was refused: %s; the drop too: %s', coll.name, field, seconds, error_kind(why), error_kind(e))
            return
    coll.create_index(field, expireAfterSeconds=seconds)
    log.info('%s TTL on %s is now %d s: collMod was refused, so the index was dropped and created again', coll.name, field, seconds)


def drop_index_if_there(coll, keys: list[tuple[str, int]]) -> None:
    for name, spec in coll.index_information().items():
        if list(spec.get('key') or []) != keys:
            continue
        try:
            coll.drop_index(name)
        except OperationFailure as e:
            if e.code != INDEX_NOT_FOUND:
                log.warning('%s keeps its old index %s, the drop was refused: %s', coll.name, name, error_kind(e))


def deploy_gap(row: dict, d: dict) -> float:
    try:
        return abs(dt.datetime.fromisoformat(str(row['at']).replace('Z', '+00:00')) - dt.datetime.fromisoformat(str(d['at']).replace('Z', '+00:00'))).total_seconds()
    except (KeyError, TypeError, ValueError):
        return 0.0


def same_deploy(row: dict, d: dict) -> bool:
    return not d.get('fromImage') or d['fromImage'] == row.get('fromImage') or deploy_gap(row, d) <= REDEPLOY_GAP_S


def glob_match(pattern: str, key: str) -> bool:
    if pattern == key:
        return True
    return re.fullmatch('.*'.join(re.escape(p) for p in pattern.split('*')), key, re.S) is not None


@functools.lru_cache(maxsize=1024)
def glob_rx(pattern: str) -> re.Pattern:
    return re.compile('.*'.join(re.escape(p) for p in pattern.split('*')), re.S)


def mute_matcher(mute: dict) -> Callable[[str], bool]:
    pattern = mute['pattern']
    if mute.get('exact') or '*' not in pattern:
        return lambda key: key == pattern
    rx = glob_rx(pattern)
    return lambda key: key == pattern or rx.fullmatch(key) is not None


def mute_matches(mute: dict, key: str) -> bool:
    return mute_matcher(mute)(key)


def _counts(anomalies: list[dict]) -> dict:
    opened = [a for a in anomalies if a.get('status') == 'open']
    live = [a for a in opened if not a.get('muted')]
    counts = {s: sum(1 for a in live if a.get('sev') == s) for s in SEVERITIES}
    counts['muted'] = len(opened) - len(live)
    return counts


_HELD = threading.local()


def _held() -> set[int]:
    if not hasattr(_HELD, 'dbs'):
        _HELD.dbs = set()
    return _HELD.dbs


def _writing(meta: dict) -> bool:
    mark = meta.get('writing')
    return isinstance(mark, dict) and str(mark.get('at') or '') >= iso(utc_now() - dt.timedelta(seconds=WRITE_LEASE_S))


def _idle() -> dict:
    return {'$or': [{'writing.at': {'$exists': False}}, {'writing.at': {'$lt': iso(utc_now() - dt.timedelta(seconds=WRITE_LEASE_S))}}]}


class Store:
    def __init__(self, db: Database, history_days: int = history.KEEP_DAYS, investigate_days: int = INVESTIGATION_KEEP_DAYS, screenshot_days: int = SCREENSHOT_KEEP_DAYS, self_deploy: str = '', release_ignore: tuple[str, ...] = (), row_links: Callable[[dict], dict] | None = None):
        self.db = db
        self.self_deploy = self_deploy
        self.row_links = row_links
        self.release_ignore = release_ignore
        self.history_days = history_days
        self.investigate_days = investigate_days
        self.screenshot_days = screenshot_days
        self.meta = db['state']
        self.anomalies = db['anomalies']
        self.actions = db['actions']
        self.deploy_reports = db['deploy_reports']
        self.deploys = db['deploys']
        self.runs = db['runs']
        self.investigations = db['investigations']
        self.spend = db['investigator_spend']
        self.leases = db['leases']
        self.activity = db['activity']
        self.chat_shares = db['chat_shares']
        self.slack_seen = db['slack_seen']
        self.ledger = db['spend']
        self.card_history = db['card_history']
        self.screenshots = db['screenshots']
        self.card_ids = db['card_ids']
        self.counters = db['counters']
        self.check_runs = db['check_runs']
        self.notices = db['notifications']
        self.quality = db['quality']
        self.route_tries = db['route_retries']
        self.route_health = db['route_health']
        self.plugin_memos = db['plugin_memos']

    def ensure_indexes(self) -> None:
        self.runs.create_index('at', expireAfterSeconds=RUN_KEEP_DAYS * 86400)
        self.check_runs.create_index('at', expireAfterSeconds=CHECK_RUN_KEEP_DAYS * 86400)
        self.activity.create_index('at', expireAfterSeconds=ACTIVITY_KEEP_DAYS * 86400)
        self.activity.create_index([('t', DESCENDING), ('_id', DESCENDING)])
        self.anomalies.create_index([('pos', ASCENDING)])
        self.card_ids.create_index('n', unique=True)
        self.actions.create_index([('status', ASCENDING), ('requestedAt', ASCENDING)])
        self.deploy_reports.create_index([('createdAt', DESCENDING), ('_id', DESCENDING)])
        self.deploys.create_index(DEPLOY_IMAGE + [('seq', ASCENDING)], unique=True)
        drop_index_if_there(self.deploys, DEPLOY_IMAGE)
        self.deploys.create_index([('at', DESCENDING), ('_id', DESCENDING)])
        ttl_index(self.investigations, 'startedAtDt', self.investigate_days * 86400)
        self.investigations.create_index([('keys', ASCENDING), ('startedAt', DESCENDING)])
        self.investigations.create_index([('mode', ASCENDING), ('startedAt', DESCENDING)])
        self.anomalies.create_index([('slackPost.channel', ASCENDING), ('slackPost.ts', ASCENDING)])
        self.actions.create_index([('slack.channel', ASCENDING), ('slack.threadTs', ASCENDING)])
        self.slack_seen.create_index('atDt', expireAfterSeconds=SLACK_SEEN_KEEP_DAYS * 86400)
        self.slack_seen.create_index([('at', DESCENDING)])
        ttl_index(self.ledger, 'atDt', max(SPEND_KEEP_DAYS, self.investigate_days) * 86400)
        self.ledger.create_index([('day', ASCENDING), ('at', DESCENDING)])
        ttl_index(self.card_history, 'atDt', self.history_days * 86400)
        self.card_history.create_index([('key', ASCENDING), ('firstSeen', DESCENDING)])
        self.card_history.create_index([('svc', ASCENDING), ('label', ASCENDING), ('lastSeen', DESCENDING)])
        self.card_history.create_index([('lastSeen', DESCENDING), ('_id', DESCENDING)])
        ttl_index(self.screenshots, 'atDt', self.screenshot_days * 86400)
        ttl_index(self.plugin_memos, 'atDt', PLUGIN_MEMO_KEEP_DAYS * 86400)
        self.screenshots.create_index([('runId', ASCENDING)])
        self.notices.create_index('at', expireAfterSeconds=NOTICE_KEEP_DAYS * 86400)
        self.notices.create_index([('user', ASCENDING), ('at', DESCENDING), ('_id', DESCENDING)])
        self.notices.create_index([('user', ASCENDING), ('read', ASCENDING)])

    def ping(self) -> bool:
        try:
            self.db.command('ping')
            return True
        except PyMongoError:
            return False

    def _meta(self) -> dict:
        return self.meta.find_one({'_id': STATE_ID}) or {'_id': STATE_ID}

    def rev(self) -> dict:
        meta = self._meta()
        return {'rev': meta.get('rev', 0), 'updatedAt': meta.get('updatedAt')}

    def server_runs(self) -> dict:
        meta = self.meta.find_one({'_id': STATE_ID}, {'serverPasses': 1, 'serverPushes': 1}) or {}
        return {'passes': meta.get('serverPasses') or {}, 'pushes': meta.get('serverPushes') or []}

    def state_section(self, name: str) -> object:
        return (self.meta.find_one({'_id': STATE_ID}, {name: 1}) or {}).get(name)

    def save_probe(self, report: dict) -> None:
        self.meta.replace_one({'_id': PROBE_ID}, {**report, '_id': PROBE_ID}, upsert=True)

    def last_probe(self) -> dict | None:
        doc = self.meta.find_one({'_id': PROBE_ID})
        return _without_ids(doc) if doc else None

    def open_cards_of(self, src: str) -> list[dict]:
        return [_without_ids(a) for a in self.anomalies.find({'src': src, 'status': 'open'}, {'key': 1, 'sev': 1, 'status': 1, 'src': 1})]

    def note_proxy(self, peer: str, at: str) -> None:
        self.db['meta'].replace_one({'_id': PROXY_ID}, {'_id': PROXY_ID, 'peer': peer, 'at': at}, upsert=True)

    def clamp_proxy(self, seen: str, at: str) -> None:
        self.db['meta'].update_one({'_id': PROXY_ID, 'at': seen}, {'$set': {'at': at}})

    def note_slack_scopes(self, missing: list[str], at: str) -> None:
        self.db['meta'].replace_one({'_id': SLACK_SCOPES_ID}, {'_id': SLACK_SCOPES_ID, 'missing': list(missing), 'at': at}, upsert=True)

    def slack_scopes(self) -> dict:
        doc = self.db['meta'].find_one({'_id': SLACK_SCOPES_ID}) or {}
        missing = doc.get('missing')
        return {'missing': [str(m) for m in missing] if isinstance(missing, list) else [], 'at': doc.get('at') if isinstance(doc.get('at'), str) else None}

    def note_version_check(self, found: dict) -> None:
        self.db['meta'].replace_one({'_id': VERSION_CHECK_ID}, {'_id': VERSION_CHECK_ID, 'latest': found['latest'], 'url': found['url'], 'securityIn': [str(v) for v in found['securityIn']], 'at': found['at']}, upsert=True)

    def version_check(self) -> dict | None:
        doc = self.db['meta'].find_one({'_id': VERSION_CHECK_ID})
        return _without_ids(doc) if doc else None

    def proxy_sighting(self) -> dict | None:
        doc = self.db['meta'].find_one({'_id': PROXY_ID}) or {}
        peer, at = doc.get('peer'), doc.get('at')
        return {'peer': peer, 'at': at} if isinstance(peer, str) and isinstance(at, str) else None

    def _settled(self) -> dict:
        if id(self.db) in _held():
            return self._meta()
        deadline = time.monotonic() + WRITE_LEASE_S
        step = SETTLE_FIRST_S
        while True:
            meta = self._meta()
            if not _writing(meta) or time.monotonic() >= deadline:
                return meta
            time.sleep(step)
            step = min(step * 2, SETTLE_MAX_S)

    def load(self) -> dict:
        state = _without_ids(self._settled())
        state.pop('writing', None)
        state.setdefault('rev', 0)
        state['anomalies'] = [_without_ids(a) for a in self.anomalies.find({}, sort=[('pos', ASCENDING)])]
        return state

    def _claim_once(self, base_rev: int | None) -> tuple[int, dict]:
        meta = self._meta()
        current = meta.get('rev', 0)
        if base_rev is not None and base_rev != current:
            raise Conflict(current)
        match = current if current else {'$in': [0, None]}
        try:
            result = self.meta.update_one({'_id': STATE_ID, 'rev': match}, {'$set': {'rev': current + 1, 'updatedAt': iso(utc_now())}}, upsert=not current)
        except DuplicateKeyError:
            raise Conflict(self._meta().get('rev', 0))
        if result.matched_count == 0 and result.upserted_id is None:
            raise Conflict(self._meta().get('rev', 0))
        return current + 1, meta

    def _claim(self, base_rev: int | None) -> tuple[int, dict]:
        if base_rev is not None:
            return self._claim_once(base_rev)
        for attempt in range(CLAIM_ATTEMPTS):
            try:
                return self._claim_once(None)
            except Conflict:
                if attempt == CLAIM_ATTEMPTS - 1:
                    raise
        raise Conflict(self._meta().get('rev', 0))

    def _hold_once(self, base_rev: int | None) -> str:
        meta = self._settled()
        current = meta.get('rev', 0)
        if base_rev is not None and base_rev != current:
            raise Conflict(current)
        mark = {'id': new_id(), 'at': iso(utc_now())}
        query = {'_id': STATE_ID} | _idle() | ({} if base_rev is None else {'rev': current if current else {'$in': [0, None]}})
        try:
            result = self.meta.update_one(query, {'$set': {'writing': mark}}, upsert=not current)
        except DuplicateKeyError:
            raise Conflict(self._meta().get('rev', 0))
        if result.matched_count == 0 and result.upserted_id is None:
            raise Conflict(self._meta().get('rev', 0))
        return mark['id']

    def _hold(self, base_rev: int | None) -> str:
        if base_rev is not None:
            return self._hold_once(base_rev)
        for attempt in range(CLAIM_ATTEMPTS):
            try:
                return self._hold_once(None)
            except Conflict:
                if attempt == CLAIM_ATTEMPTS - 1:
                    raise
        raise Conflict(self._meta().get('rev', 0))

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if id(self.db) in _held():
            yield
            return
        mark = self._hold_patiently()
        if mark is None:
            yield
            return
        _held().add(id(self.db))
        try:
            yield
        finally:
            _held().discard(id(self.db))
            self._release(mark)

    def _hold_patiently(self) -> str | None:
        deadline = time.monotonic() + HOLD_WAIT_S
        step = SETTLE_FIRST_S
        while True:
            try:
                return self._hold_once(None)
            except Conflict:
                if time.monotonic() >= deadline:
                    log.error('a card write went ahead without the state lease: the lease was not free for %d s', HOLD_WAIT_S)
                    return None
            time.sleep(step)
            step = min(step * 2, SETTLE_MAX_S)

    def save(self, state: dict, base_rev: int | None) -> int:
        return self.save_state(state, base_rev)[0]

    def save_state(self, state: dict, base_rev: int | None, loaded: dict | None = None) -> tuple[int, dict[str, str | None] | None]:
        mark = self._hold(base_rev)
        _held().add(id(self.db))
        try:
            rev, previous = self._claim(base_rev)
            items, stored, gone = self._write_state(state, rev, mark, loaded)
        except BaseException:
            self._release(mark)
            raise
        finally:
            _held().discard(id(self.db))
        for card, ev in history.exits(items, stored, gone):
            self.record_card(card, ev)
        return rev, {k: d.get('pushedAt') for k, d in stored.items()} if previous.get('initialized') else None

    def _write_state(self, state: dict, rev: int, mark: str, loaded: dict | None) -> tuple[list[dict], dict[str, dict], list[dict]]:
        items = state.get('anomalies') or []
        keys = [a['key'] for a in items]
        stored = {d['_id']: d for d in self.anomalies.find({'_id': {'$in': keys}}, history.STORED_FIELDS)}
        before = {a['key']: a for a in (loaded or {}).get('anomalies') or []}
        if loaded is None:
            gone = list(self.anomalies.find({'_id': {'$nin': keys}}, history.STORED_FIELDS))
        else:
            dropped = [k for k in before if k not in set(keys)]
            gone = list(self.anomalies.find({'_id': {'$in': dropped}}, history.STORED_FIELDS)) if dropped else []
        for i, a in enumerate(items):
            fresh = {**keep_slack_state(a, stored.get(a['key'], {})), 'pos': i}
            if loaded is not None and a['key'] in before and a['key'] in stored:
                self.anomalies.update_one({'_id': a['key']}, changes(before[a['key']], fresh))
            else:
                self.anomalies.replace_one({'_id': a['key']}, {**fresh, '_id': a['key']}, upsert=True)
        if gone:
            self.anomalies.delete_many({'_id': {'$in': [d['_id'] for d in gone]}})
        rest = {k: v for k, v in state.items() if k not in META_OWN}
        now = utc_now()
        if loaded is None:
            done = self.meta.replace_one({'_id': STATE_ID, 'writing.id': mark}, {**rest, '_id': STATE_ID, 'rev': rev, 'updatedAt': iso(now)})
        else:
            done = self._save_meta(rest, {k: v for k, v in loaded.items() if k not in META_OWN}, now, mark)
        if done.matched_count == 0:
            raise Conflict(self._meta().get('rev', 0))
        return items, stored, gone

    def _release(self, mark: str) -> None:
        try:
            self.meta.update_one({'_id': STATE_ID, 'writing.id': mark}, {'$unset': {'writing': ''}})
        except PyMongoError as e:
            log.warning('the state write mark stays until its lease ends: %s', error_kind(e))

    def _save_meta(self, rest: dict, old: dict, now: dt.datetime, mark: str) -> UpdateResult:
        kept, wanted = old.get('muted') or [], rest.get('muted') or []
        for m in kept:
            if m not in wanted:
                self.meta.update_one({'_id': STATE_ID}, {'$pull': {'muted': m}})
        added = [m for m in wanted if m not in kept]
        if added:
            self.meta.update_one({'_id': STATE_ID}, {'$push': {'muted': {'$each': added}}})
        update = meta_changes({k: v for k, v in old.items() if k != 'muted'}, {k: v for k, v in rest.items() if k != 'muted'})
        update.setdefault('$set', {})['updatedAt'] = iso(now)
        update.setdefault('$unset', {})['writing'] = ''
        return self.meta.update_one({'_id': STATE_ID, 'writing.id': mark}, update)

    def _anomaly(self, key: str) -> dict:
        doc = self.anomalies.find_one({'_id': key})
        if not doc:
            raise NotFound(f'no anomaly with key {key}')
        return doc

    def anomaly(self, key: str) -> dict:
        return _without_ids(self._anomaly(key))

    def keys_near(self, key: str, shortest: int) -> list[str]:
        longer = [d['_id'] for d in self.anomalies.find({'_id': {'$regex': '^' + re.escape(key)}}, {'_id': 1}, limit=3)]
        shorter = [d['_id'] for d in self.anomalies.find({'_id': {'$in': [key[:n] for n in range(shortest, len(key))]}}, {'_id': 1}, limit=3)]
        return list(dict.fromkeys(longer + shorter))

    def anomaly_by_post(self, channel: str, ts: str) -> dict | None:
        where = {'slackPost.channel': channel, 'slackPost.ts': ts}
        roots = list(self.anomalies.find(where | {'slackPost.threadOf': {'$exists': False}}, limit=2))
        if len(roots) > 1:
            return None
        doc = roots[0] if roots else self.anomalies.find_one(where)
        return _without_ids(doc) if doc else None

    def post_channels(self) -> set[str]:
        return {c for coll in (self.anomalies, self.deploy_reports) for c in coll.distinct('slackPost.channel') if c}

    def assess(self, key: str, suspects: list[str] | None, recommend: str | None, code: list[dict] | None) -> int:
        doc = self._anomaly(key)
        update = {'$set': {'assessedAt': iso(utc_now())}}
        for field, value in (('suspects', suspects), ('recommend', recommend), ('code', code)):
            if value:
                update['$set'][field] = value
            elif value is not None:
                update.setdefault('$unset', {})[field] = ''
        with self._locked():
            rev, _ = self._claim(None)
            self.anomalies.update_one({'_id': doc['_id']}, update)
            return rev

    def note(self, key: str, text: str, by: str | None = None, cause: dict | None = None, drop_cause: bool = False) -> int:
        doc = self._anomaly(key)
        kept = ['progress', 'handled'] if by == 'pod' else ['progress']
        dived = {'$set': {'triage': 'dived', 'triagedBy': 'pod'}, '$unset': {'resolution': ''}} if by == 'pod' else {'$set': {'triage': 'dived'}, '$unset': {'triagedBy': '', 'resolution': ''}}
        with self._locked():
            rev, _ = self._claim(None)
            self.anomalies.update_one({'_id': doc['_id']}, {'$set': {'note': {'text': text, 'at': iso(utc_now())}} | ({'cause': causes.recorded(cause, doc.get('firstSeen'))} if cause else {})}
                                      | ({'$unset': {'cause': ''}} if drop_cause else {}))
            self.anomalies.update_one({'_id': doc['_id'], 'triage': {'$nin': kept}}, dived)
            return rev

    def triage(self, key: str, state: str | None, ticket: str | None = None, owner: str | None = None, by: str | None = None, at: str | None = None,
               resolution: dict | None = None) -> int:
        if state is not None and state not in TRIAGE_STATES:
            raise ValueError(f'unknown triage state {state}')
        doc = self._anomaly(key)
        sets, unsets = {}, {}
        if state:
            sets['triage'] = state
            sets['triagedAt'] = at or iso(utc_now())
        else:
            unsets['triage'] = ''
            unsets['triagedAt'] = ''
        if state == 'handled' and resolution:
            sets['resolution'] = {'at': sets['triagedAt']} | {k: v for k, v in resolution.items() if v not in (None, '')}
        elif state != 'handled':
            unsets['resolution'] = ''
        for field, value in (('ticket', ticket), ('owner', owner)):
            if value:
                sets[field] = value
            elif value is not None:
                unsets[field] = ''
        if owner is not None:
            unsets['assigned'] = ''
        if by == 'pod' and state:
            sets['triagedBy'] = 'pod'
        else:
            unsets['triagedBy'] = ''
        update = {}
        if sets:
            update['$set'] = sets
        if unsets:
            update['$unset'] = unsets
        with self._locked():
            rev, _ = self._claim(None)
            self.anomalies.update_one({'_id': doc['_id']}, update)
            if state in history.ADDRESSED:
                self.record_card(self.anomalies.find_one({'_id': doc['_id']}) or doc, state)
            return rev

    def set_severity(self, key: str, sev: str, by: str | None) -> tuple[int, str]:
        doc = self._anomaly(key)
        auto = doc.get('sevAuto') or doc.get('sev')
        if sev == 'auto':
            update = {'$set': {'sev': auto}, '$unset': {'sevSet': '', 'sevAuto': ''}}
        elif sev in ('crit', 'warn', 'info'):
            update = {'$set': {'sev': sev, 'sevAuto': auto, 'sevSet': {'sev': sev, 'by': by or 'Claude', 'at': iso(utc_now()), 'auto': auto}}}
        else:
            raise ValueError(f'unknown severity {sev}')
        with self._locked():
            rev, _ = self._claim(None)
            self.anomalies.update_one({'_id': doc['_id']}, update)
            return rev, auto

    def group(self, parent: str, children: list[str], absorb: list[str] | None = None) -> int:
        host = self._anomaly(parent)
        kids = [self._anomaly(c) for c in children if c != parent]
        with self._locked():
            rev, _ = self._claim(None)
            for k in kids:
                self.anomalies.update_one({'_id': k['_id']}, {'$set': {'parent': parent}, '$unset': {'absorb': ''}})
            if kids:
                self.anomalies.update_many({'parent': {'$in': [k['_id'] for k in kids]}, '_id': {'$ne': parent}}, {'$set': {'parent': parent}})
            update = {'$unset': {'parent': ''}}
            if absorb:
                update['$set'] = {'absorb': sorted(set((host.get('absorb') or []) + absorb))}
            self.anomalies.update_one({'_id': host['_id']}, update)
            return rev

    def _mute_hits(self, key: str, exact: bool) -> list[str]:
        if exact:
            return [key]
        return [a['_id'] for a in self.anomalies.find({}, {'_id': 1}) if glob_match(key, a['_id'])]

    def mute(self, key: str, reason: str, until: str | None, kind: str, preset: bool = False) -> int:
        exact = '*' in key and self.anomalies.find_one({'_id': key}, {'_id': 1}) is not None
        hit = self._mute_hits(key, exact)
        if not hit:
            raise NotFound(f'no anomaly with key {key}')
        entry = {'pattern': key, 'until': until, 'reason': reason, 'kind': kind, 'at': iso(utc_now())} | ({'exact': True} if exact else {}) | ({'preset': True} if preset else {})
        with self._locked():
            kept = [m for m in self._meta().get('muted') or [] if m.get('pattern') != key]
            update: dict = {'$set': {'muted': reason} | ({MUTED_PRESET: True} if preset else {})}
            if not preset:
                update['$unset'] = {MUTED_PRESET: ''}
            if kind == 'known':
                update['$unset'] = update.get('$unset', {}) | {'slackDraft': '', 'autoSend': ''}
            rev, _ = self._claim(None)
            self.meta.update_one({'_id': STATE_ID}, {'$set': {'muted': kept + [entry]}})
            self.anomalies.update_many({'_id': {'$in': hit}}, update)
            for card in self.anomalies.find({'_id': {'$in': hit}}):
                self.record_card(card, 'known' if kind == 'known' else 'muted')
            return rev

    def unmute(self, key: str) -> int:
        with self._locked():
            muted = self._meta().get('muted') or []
            exact = any(m.get('pattern') == key and m.get('exact') for m in muted)
            hit = self._mute_hits(key, exact)
            kept = [m for m in muted if m.get('pattern') != key]
            rev, _ = self._claim(None)
            self.meta.update_one({'_id': STATE_ID}, {'$set': {'muted': kept}})
            if hit:
                self.anomalies.update_many({'_id': {'$in': hit}}, {'$set': {'muted': None}, '$unset': {MUTED_PRESET: ''}})
            return rev

    def claim_card(self, key: str, run_id: str) -> bool:
        with self._locked():
            doc = self.anomalies.find_one_and_update({'_id': key, 'triage': None}, {'$set': {'triage': 'investigating', 'investigator': {'runId': run_id, 'at': iso(utc_now())}}})
            if doc is None:
                return False
            self._bump()
            return True

    def release_card(self, key: str, run_id: str) -> None:
        with self._locked():
            if self.anomalies.update_one({'_id': key, 'triage': 'investigating', 'investigator.runId': run_id}, {'$unset': {'triage': '', 'investigator': ''}}).modified_count:
                self._bump()

    def release_stale_claims(self, before: str) -> int:
        query = {'triage': 'investigating', 'investigator.at': {'$lt': before}}
        with self._locked():
            n = self.anomalies.update_many(query, {'$unset': {'triage': '', 'investigator': ''}}).modified_count
            if n:
                self._bump()
            return n

    def dead_pods(self, stale_before: float) -> set[str]:
        return {d['_id'][len('boot:'):] for d in self.runs.find({'_id': {'$regex': '^boot:'}, 'beatAt': {'$lt': stale_before}}, {'_id': 1})}

    def abandon_runs_of(self, pods: set[str], error: str) -> list[dict]:
        query = {'outcome': 'running', 'pod': {'$in': sorted(pods)}}
        runs = [_with_id(d) for d in self.investigations.find(query, {'actionId': 1, 'pod': 1})]
        if not runs:
            return []
        self.investigations.update_many({'_id': {'$in': [r['id'] for r in runs]}, 'outcome': 'running'}, {'$set': {'outcome': 'abandoned', 'endedAt': iso(utc_now()), 'error': error}})
        with self._locked():
            if self.anomalies.update_many({'triage': 'investigating', 'investigator.runId': {'$in': [r['id'] for r in runs]}}, {'$unset': {'triage': '', 'investigator': ''}}).modified_count:
                self._bump()
        for r in runs:
            if r.get('actionId'):
                self.requeue_action(r['actionId'])
        return runs

    def drop_leases_of(self, pods: set[str]) -> int:
        dead = [d['_id'] for d in self.leases.find({}, {'owner': 1}) if ':'.join(str(d.get('owner') or '').split(':')[:2]) in pods]
        return self.leases.delete_many({'_id': {'$in': dead}}).deleted_count if dead else 0

    def abandon_stale_runs(self, before: str) -> int:
        update = {'$set': {'outcome': 'abandoned', 'endedAt': iso(utc_now()), 'error': 'the pod died mid-run'}}
        return self.investigations.update_many({'outcome': 'running', 'startedAt': {'$lt': before}}, update).modified_count

    def fail_stale_actions(self, before: str, types: tuple[str, ...] = ('dive', 'ask')) -> int:
        update = {'$set': {'status': 'failed', 'result': 'the pod died mid-run'}}
        query = {'status': 'processing', 'type': {'$in': list(types)}, 'source': {'$ne': 'slack'}, 'slack.channel': {'$exists': False}, 'processingAt': {'$lt': before}}
        return self.actions.update_many(query, update).modified_count

    def fail_stale_slack_asks(self, before: str) -> list[dict]:
        query = {'type': 'ask', 'source': {'$in': ['slack', 'chat']}, 'slack.channel': {'$exists': True},
                 '$or': [{'status': 'requested', 'requestedAt': {'$lt': before}}, {'status': 'processing', 'processingAt': {'$lt': before}}]}
        failed = []
        for doc in list(self.actions.find(query)):
            why = 'the pod died mid-run' if doc['status'] == 'processing' else 'not picked up in time'
            try:
                failed.append(self.update_action(doc['_id'], 'failed', why, doc['status']))
            except (ActionMoved, NotFound):
                continue
        return failed

    def take_lease(self, owner: str, seconds: int, lease_id: str = LEASE_ID) -> bool:
        now = utc_now()
        until = now + dt.timedelta(seconds=seconds)
        try:
            self.leases.insert_one({'_id': lease_id, 'owner': owner, 'until': until})
            return True
        except DuplicateKeyError:
            pass
        query = {'_id': lease_id, '$or': [{'until': {'$lt': now}}, {'owner': owner}]}
        return self.leases.update_one(query, {'$set': {'owner': owner, 'until': until}}).matched_count == 1

    def drop_lease(self, owner: str, lease_id: str = LEASE_ID) -> None:
        self.leases.delete_one({'_id': lease_id, 'owner': owner})

    def requeue_action(self, action_id: str) -> bool:
        return self.actions.update_one({'_id': action_id, 'status': 'processing'}, {'$set': {'status': 'requested'}, '$unset': {'processingAt': ''}}).modified_count == 1

    def add_spend(self, day: str, usd: float) -> float:
        doc = self.spend.find_one_and_update({'_id': day}, {'$inc': {'usd': usd}}, upsert=True, return_document=ReturnDocument.AFTER)
        return doc['usd']

    def spent(self, day: str) -> float:
        return float((self.spend.find_one({'_id': day}) or {}).get('usd', 0.0))

    def add_ledger(self, row: dict, now: dt.datetime | None = None) -> None:
        at = now or utc_now()
        self.ledger.insert_one({**row, '_id': new_id(), 'at': iso(at), 'atDt': at})

    def backfill_candidates(self) -> list[dict]:
        done = set(self.ledger.distinct('runId'))
        return [d for d in self.investigations.find({'usd': {'$gt': 0}}) if d['_id'] not in done]

    def add_ledger_backfill(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        try:
            return len(self.ledger.insert_many(rows, ordered=False).inserted_ids)
        except BulkWriteError as e:
            return int(e.details.get('nInserted', 0))

    def ledger_since(self, day: str) -> list[dict]:
        return list(self.ledger.find({'day': {'$gte': day}}, {'_id': 0, 'atDt': 0}, limit=LEDGER_ROW_LIMIT))

    def run_costs_meta(self, run_ids: list[str]) -> dict[str, dict]:
        return {d['_id']: self._run(d) for d in self.investigations.find({'_id': {'$in': list(run_ids)}}, RUN_COST_FIELDS)}

    def record_card(self, card: dict, ev: str, ask: dict | None = None, said: dict | None = None) -> str | None:
        if not card.get('key') or not card.get('firstSeen'):
            return None
        try:
            return self._record_card(card, ev, ask, said)
        except PyMongoError as e:
            log.warning('card history for %s (%s) not written: %s', card.get('key'), ev, error_kind(e))
            return None

    def _record_card(self, card: dict, ev: str, ask: dict | None, said: dict | None = None) -> str:
        key, first = card['key'], card['firstSeen']
        did = history.doc_id(key, first)
        now = utc_now()
        old = self.card_history.find_one({'_id': did}, {'outcome': 1})
        runs = list(self.investigations.find({'keys': key, 'startedAt': {'$gte': first}} | NOT_CHAT, history.RUN_FIELDS, sort=[('startedAt', DESCENDING), ('_id', DESCENDING)], limit=history.RUNS_KEEP))
        doc = history.snapshot(card) | {'outcome': history.outcome(card, ev, (old or {}).get('outcome')), 'at': iso(now), 'atDt': now, 'runs': [r['_id'] for r in runs]} | history.latest_run(runs)
        push = {'events': {'$each': [{'ev': ev, 'at': iso(now)}], '$slice': -history.EVENTS_KEEP}}
        if ask:
            push['asks'] = {'$each': [ask], '$slice': -history.ASKS_KEEP}
            doc['lastAsk'] = history.last_ask(ask)
        if said:
            push['said'] = {'$each': [said], '$slice': -history.SAID_KEEP}
        update = {'$set': doc, '$push': push}
        if 'resolution' not in doc:
            update['$unset'] = {'resolution': ''}
        if old is None:
            update['$setOnInsert'] = {'createdAt': iso(now), 'reopens': self.card_history.count_documents({'key': key, 'firstSeen': {'$lt': first}})}
        self.card_history.update_one({'_id': did}, update, upsert=True)
        linked = list(dict.fromkeys(doc['runs'] + ([ask['runId']] if ask and ask.get('runId') else [])))
        if linked:
            self.investigations.update_many({'_id': {'$in': linked}}, {'$addToSet': {'cardHistory': did}})
        return did

    def history_rows(self, keys: list[str]) -> dict[str, list[dict]] | None:
        try:
            rows: dict[str, list[dict]] = {}
            for d in self.card_history.find({'key': {'$in': list(keys)}}, history.ROW_FIELDS):
                rows.setdefault(d['key'], []).append(d)
            return rows
        except PyMongoError as e:
            log.warning('card history not read: %s', error_kind(e))
            return None

    def mute_entries(self) -> list:
        return list(self._meta().get('muted') or [])

    def mark_preset_mutes(self, marks: list[tuple]) -> int | None:
        wanted = set(marks)
        with self._locked():
            muted = self._meta().get('muted') or []
            out = [m | {'preset': True} if isinstance(m, dict) and not m.get('preset') and (m.get('kind'), m.get('pattern'), m.get('at')) in wanted else m for m in muted]
            hit = sum(1 for a, b in zip(muted, out) if a is not b)
            if not hit:
                return 0
            self._claim(None)
            if self.meta.update_one({'_id': STATE_ID, 'muted': muted}, {'$set': {'muted': out}}).matched_count == 0:
                log.info('mute marks skipped: the mute list changed while they were written; the next start tries again')
                return None
            return hit

    def mark_preset_rows(self, ids: list[str]) -> int:
        return self.card_history.update_many({'_id': {'$in': list(ids)}, MUTED_PRESET: {'$ne': True}}, {'$set': {MUTED_PRESET: True}}).modified_count

    def preset_muted(self, ids: list[str]) -> set[str]:
        return {d['_id'] for d in self.card_history.find({'_id': {'$in': list(ids)}, MUTED_PRESET: True}, {'_id': 1})} if ids else set()

    def past_cards(self, key: str | None = None, service: str | None = None, text: str | None = None, limit: int = history.PAST_DEFAULT) -> list[dict]:
        limit = max(1, min(int(limit or history.PAST_DEFAULT), history.PAST_LIMIT))
        out: list[dict] = []
        seen: set[str] = set()

        def take(query: dict, match: str) -> None:
            for d in self.card_history.find(query, sort=[('lastSeen', DESCENDING), ('firstSeen', DESCENDING)], limit=limit):
                if len(out) >= limit:
                    return
                if d['_id'] not in seen:
                    seen.add(d['_id'])
                    out.append(history.past_row(d, match))

        if key:
            take({'key': key}, 'key')
            live = self.anomalies.find_one({'_id': key}, {'svc': 1, 'label': 1}) or self.card_history.find_one({'key': key}, {'svc': 1, 'label': 1}, sort=[('lastSeen', DESCENDING)]) or {}
            if live.get('svc') and live.get('label'):
                take({'svc': live['svc'], 'label': live['label'], 'key': {'$ne': key}}, 'service+label')
        if service:
            take({'svc': service}, 'service')
        if text:
            rx = {'$regex': re.escape(text[:history.TEXT_QUERY_LIMIT]), '$options': 'i'}
            take({'$or': [{'label': rx}, {'note': rx}]}, 'text')
        return out

    def closed_cards(self, start: str, end: str, text: str | None = None, outcome: str | None = None, page: int = 0, size: int = 50) -> tuple[list[dict], int]:
        live = [history.doc_id(d['_id'], d['firstSeen']) for d in self.anomalies.find({'firstSeen': {'$exists': True}}, {'firstSeen': 1}) if d.get('firstSeen')]
        query: dict = {'lastSeen': {'$gte': start, '$lte': end}, '_id': {'$nin': live}}
        if outcome:
            query['outcome'] = outcome
        text = (text or '').strip()[:history.TEXT_QUERY_LIMIT]
        number = history.CARD_NUMBER.match(text)
        if number:
            n = int(number.group(1))
            query['$or'] = [{'num': n}] + ([{'key': self.key_of_number(n)}] if self.key_of_number(n) else [])
        elif text:
            rx = {'$regex': re.escape(text), '$options': 'i'}
            query['$or'] = [{'key': rx}, {'svc': rx}, {'label': rx}, {'note': rx}, {'ticket': rx}, {'owner': rx}]
        size = max(1, min(int(size), history.CLOSED_PAGE_MAX))
        total = self.card_history.count_documents(query)
        fields = {f: 1 for f in history.CLOSED_FIELDS + ('note', 'muted')}
        docs = list(self.card_history.find(query, fields, sort=[('lastSeen', DESCENDING), ('_id', DESCENDING)], skip=max(0, int(page)) * size, limit=size))
        rows = [history.closed_row(d) for d in docs]
        missing = [r['key'] for r in rows if 'num' not in r and r.get('key')]
        if missing:
            nums = self.card_numbers(missing)
            for r in rows:
                if 'num' not in r and r.get('key') in nums:
                    r['num'] = nums[r['key']]
        return rows, total

    def runs_since(self, since: str) -> list[dict]:
        return list(self.investigations.find({'startedAt': {'$gte': since}} | NOT_CHAT, BACKFILL_RUN_FIELDS))

    def actions_since(self, since: str) -> list[dict]:
        return list(self.actions.find({'requestedAt': {'$gte': since}, 'key': {'$exists': True}}, BACKFILL_ACTION_FIELDS))

    def add_card_history_backfill(self, docs: list[dict]) -> int:
        if not docs:
            return 0
        try:
            written = len(self.card_history.insert_many(docs, ordered=False).inserted_ids)
        except BulkWriteError as e:
            written = int(e.details.get('nInserted', 0))
        for d in docs:
            if d.get('runs'):
                self.investigations.update_many({'_id': {'$in': d['runs']}}, {'$addToSet': {'cardHistory': d['_id']}})
        return written

    def backfill_marked(self, name: str) -> bool:
        return self.meta.find_one({'_id': name}, {'_id': 1}) is not None

    def mark_backfill(self, name: str, written: int) -> None:
        self.meta.replace_one({'_id': name}, {'_id': name, 'at': iso(utc_now()), 'docs': written}, upsert=True)

    def past_runs(self, key: str | None = None, service: str | None = None, text: str | None = None, limit: int = history.PAST_DEFAULT, skip: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit or history.PAST_DEFAULT), history.PAST_LIMIT))
        query: dict = {'outcome': 'done'}
        both: list[dict] = []
        if skip:
            query['_id'] = {'$ne': skip}
        if key:
            query['keys'] = key
        if service:
            both.append({'keys': {'$regex': f'(^|[|/]){re.escape(service[:history.TEXT_QUERY_LIMIT])}(\\||$)'}})
        if text:
            rx = {'$regex': re.escape(text[:history.TEXT_QUERY_LIMIT]), '$options': 'i'}
            both.append({'$or': [{'ask.text': rx}, {'recap': rx}, {'toolCalls.input': rx}]})
        if both:
            query['$and'] = both
        return list(self.investigations.find(query | NOT_CHAT, PAST_RUN_FIELDS, sort=[('startedAt', DESCENDING), ('_id', DESCENDING)], limit=limit))

    def add_investigation(self, doc: dict) -> dict:
        body = {k: v for k, v in doc.items() if k != 'id'}
        full = {**body, '_id': doc.get('id') or new_id(), 'startedAtDt': utc_now()}
        self.investigations.insert_one(full)
        return self._run(full)

    def update_investigation(self, run_id: str, fields: dict) -> None:
        self.investigations.update_one({'_id': run_id}, {'$set': fields})

    def _run(self, doc: dict) -> dict:
        run = {k: v for k, v in _with_id(doc).items() if k != 'startedAtDt'}
        if run.get('trigger') != 'chat':
            return run
        shown = {k: v for k, v in run.items() if k in CHAT_RUN_PUBLIC}
        return shown | {'keys': [], 'ask': {'private': True}} | ({'toolCalls': [{'name': c.get('name')} for c in run['toolCalls']]} if 'toolCalls' in run else {})

    def investigation(self, run_id: str) -> dict:
        doc = self.investigations.find_one({'_id': run_id})
        if not doc:
            raise NotFound(f'no investigation with id {run_id}')
        return self._run(doc)

    def run_proposal(self, run_id: str) -> dict | None:
        found = (self.investigations.find_one({'_id': run_id}, {'proposal': 1}) or {}).get('proposal')
        return found if isinstance(found, dict) else None

    def claim_proposal(self, run_id: str, by: str) -> dict | None:
        doc = self.investigations.find_one_and_update({'_id': run_id, 'proposal.tool': {'$exists': True}, 'proposal.confirmedAt': {'$exists': False}},
                                                      {'$set': {'proposal.confirmedBy': by, 'proposal.confirmedAt': iso(utc_now())}}, return_document=ReturnDocument.AFTER)
        return _with_id(doc) if doc else None

    def proposal_result(self, run_id: str, result: dict) -> None:
        self.investigations.update_one({'_id': run_id}, {'$set': {'proposal.result': result}})

    def run_of_action(self, action_id: str) -> dict | None:
        doc = next(iter(self.investigations.find({'actionId': action_id}, sort=[('startedAt', DESCENDING), ('_id', DESCENDING)], limit=1)), None)
        return self._run(doc) if doc else None

    def add_check_runs(self, rows: list[dict], at: dt.datetime | None = None) -> None:
        when = at or utc_now()
        docs = [{'name': str(r['name']), 'ok': bool(r['ok']), 'at': when} | ({'ms': int(r['ms'])} if r.get('ms') is not None else {}) | ({'check': str(r['check'])} if r.get('check') else {}) | ({'error': str(r['error'])[:CHECK_ERROR_LIMIT]} if r.get('error') else {}) for r in rows]
        if docs:
            self.check_runs.insert_many(docs)

    def quality_doc(self, name: str) -> dict | None:
        doc = self.quality.find_one({'_id': name}, {'_id': 0})
        return doc if isinstance(doc, dict) else None

    def save_quality_doc(self, name: str, doc: dict) -> None:
        self.quality.replace_one({'_id': name}, {**doc, '_id': name}, upsert=True)

    def check_runs_since(self, since: str) -> list[dict]:
        start = dt.datetime.fromisoformat(since.replace('Z', '+00:00'))
        rows = self.check_runs.find({'at': {'$gte': start}}, {'_id': 0}, sort=[('at', DESCENDING)], limit=CHECK_RUN_LIMIT)
        return [r | {'at': iso(r['at'] if r['at'].tzinfo else r['at'].replace(tzinfo=dt.timezone.utc))} for r in rows]

    def tool_usage_runs(self, since: str) -> list[dict]:
        runs = list(self.investigations.find({'startedAt': {'$gte': since}}, TOOL_USAGE_FIELDS, sort=[('startedAt', DESCENDING)], limit=USAGE_RUN_LIMIT))
        for r in runs:
            if r.get('trigger') == 'chat':
                r['toolCalls'] = [c | {'error': CHAT_ERROR} if c.get('error') else c for c in r.get('toolCalls') or []]
        return runs

    def investigation_usage(self, since: str) -> dict:
        runs = list(self.investigations.find({'startedAt': {'$gte': since}}, {'toolCalls.input': 0}, sort=[('startedAt', DESCENDING), ('_id', DESCENDING)], limit=USAGE_RUN_LIMIT))
        usage = dict.fromkeys(USAGE_FIELDS, 0)
        tools: dict[str, dict] = {}
        keys: dict[str, dict] = {}
        models: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        slowest: list[dict] = []
        turns = 0
        usd = 0.0
        for r in runs:
            usd += float(r.get('usd') or 0)
            turns += int(r.get('turns') or 0)
            for f in USAGE_FIELDS:
                usage[f] += int((r.get('usage') or {}).get(f) or 0)
            for m in r.get('models') or []:
                models[m] = models.get(m, 0) + 1
            outcomes[r.get('outcome') or '?'] = outcomes.get(r.get('outcome') or '?', 0) + 1
            for k in [] if r.get('trigger') == 'chat' else r.get('keys') or []:
                row = keys.setdefault(k, {'key': k, 'runs': 0, 'usd': 0.0})
                row['runs'] += 1
                row['usd'] += float(r.get('usd') or 0) / max(len(r.get('keys') or []), 1)
            for c in r.get('toolCalls') or []:
                t = tools.setdefault(c.get('name') or '?', {'name': c.get('name') or '?', 'calls': 0, 'ms': 0, 'maxMs': 0, 'truncated': 0, 'errors': 0})
                ms = int(c.get('ms') or 0)
                t['calls'] += 1
                t['ms'] += ms
                t['maxMs'] = max(t['maxMs'], ms)
                t['truncated'] += 1 if c.get('truncated') else 0
                t['errors'] += 1 if c.get('error') else 0
                slowest.append({'run': r['_id'], 'tool': t['name'], 'ms': ms, 'startedAt': r.get('startedAt')})
        prompt = usage['input_tokens'] + usage['cache_read_input_tokens'] + usage['cache_creation_input_tokens']
        return {'since': since, 'runs': len(runs), 'usd': round(usd, 4), 'turns': turns, 'usage': usage,
                'cacheShare': round(usage['cache_read_input_tokens'] / prompt, 3) if prompt else None,
                'models': models, 'outcomes': outcomes,
                'tools': sorted(({**t, 'avgMs': round(t['ms'] / t['calls'])} for t in tools.values()), key=lambda t: (-t['calls'], -t['ms'], t['name'])),
                'keys': sorted(({**k, 'usd': round(k['usd'], 4)} for k in keys.values()), key=lambda k: (-k['usd'], -k['runs'], k['key']))[:USAGE_TOP_KEYS],
                'slowest': sorted(slowest, key=lambda c: (-c['ms'], c['tool'], str(c['run'])))[:USAGE_TOP_SLOW]}

    def list_investigations(self, key: str | None, limit: int, page: bool = False, chats: bool = True) -> list[dict]:
        query = {'keys': key} | NOT_CHAT if key else ({} if chats else NOT_CHAT)
        rows = [self._run(d) for d in self.investigations.find(query, RUN_PAGE_FIELDS if page else None, sort=[('startedAt', DESCENDING), ('_id', DESCENDING)], limit=limit)]
        if page:
            for r in rows:
                r['toolCalls'] = len(r.pop('toolCalls', None) or [])
        return rows

    def card_runs(self, keys) -> dict[str, dict]:
        keys = sorted({k for k in keys if k})
        if not keys:
            return {}
        since = iso(utc_now() - dt.timedelta(days=CARD_RUNS_DAYS))
        pipeline = [{'$match': {'keys': {'$in': keys}, 'startedAt': {'$gte': since}} | NOT_CHAT}, {'$project': RUN_PAGE_FIELDS}, {'$sort': {'startedAt': DESCENDING, '_id': DESCENDING}},
                    {'$unwind': '$keys'}, {'$match': {'keys': {'$in': keys}}},
                    {'$group': {'_id': '$keys', 'latest': {'$first': '$$ROOT'}, 'n': {'$sum': 1}, 'usd': {'$sum': {'$ifNull': ['$usd', 0]}}}}]
        out = {}
        for g in self.investigations.aggregate(pipeline):
            run = self._run(g['latest'] | {'keys': [g['_id']]})
            run['toolCalls'] = len(run.pop('toolCalls', None) or [])
            out[g['_id']] = {'latest': run, 'n': g['n'], 'usd': round(float(g['usd']), 4)}
        return out

    def review_runs(self, since: str) -> list[dict]:
        fields = {'trigger': 1, 'startedAt': 1, 'outcome': 1, 'usd': 1, 'keys': 1, 'toolCalls.name': 1, 'toolCalls.error': 1}
        return [self._run(d) for d in self.investigations.find({'startedAt': {'$gte': since}}, fields, sort=[('startedAt', DESCENDING)], limit=USAGE_RUN_LIMIT)]

    def leases_now(self) -> dict[str, dict]:
        now = utc_now()
        return {d['_id']: {'owner': str(d.get('owner') or ''), 'until': iso(d['until'])} for d in self.leases.find({'until': {'$gt': now}})}

    def running_investigations(self) -> list[dict]:
        fields = {'trigger': 1, 'actionId': 1, 'keys': 1, 'pod': 1, 'room': 1, 'startedAt': 1, 'model': 1, 'effort': 1, 'ask': 1, 'report': 1}
        return [self._run(d) for d in self.investigations.find({'outcome': 'running'}, fields, sort=[('startedAt', ASCENDING), ('_id', ASCENDING)], limit=20)]

    def tool_progress(self, run_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not run_ids:
            return out
        for d in self.activity.find({'kind': 'tool', 'runId': {'$in': run_ids}}, {'runId': 1, 'tool': 1, 't': 1}, sort=[('at', ASCENDING), ('_id', ASCENDING)]):
            row = out.setdefault(d['runId'], {'toolCalls': 0})
            row.update(toolCalls=row['toolCalls'] + 1, lastTool=d.get('tool'), lastToolAt=d.get('t'))
        return out

    def askers(self, since: str, limit: int = 5) -> list[dict]:
        rows = self.actions.aggregate([{'$match': {'type': 'ask', 'source': 'slack', 'smallTalk': {'$ne': True}, 'requestedAt': {'$gte': since}}},
                                       {'$group': {'_id': '$requestedBy', 'asks': {'$sum': 1}}}, {'$sort': {'asks': -1, '_id': 1}}, {'$limit': limit}])
        return [{'by': r['_id'], 'asks': r['asks']} for r in rows]

    def recent_investigations(self, mode: str, since: str) -> list[dict]:
        return [self._run(d) for d in self.investigations.find({'mode': mode, 'startedAt': {'$gte': since}}, RUN_FIELDS, sort=[('startedAt', ASCENDING)])]

    def add_screenshot(self, doc: dict) -> None:
        self.screenshots.insert_one(doc)

    def screenshot(self, shot_id: str) -> dict:
        doc = self.screenshots.find_one({'_id': shot_id})
        if doc is None:
            raise NotFound(f'no screenshot {shot_id}')
        return doc

    def screenshots_since(self, since: dt.datetime) -> int:
        return self.screenshots.count_documents({'atDt': {'$gte': since}})

    def add_activity(self, kind: str, who: str, text: str, **extra) -> None:
        now = utc_now()
        doc = {'_id': new_id(), 'at': now, 't': iso(now), 'kind': kind, 'who': who, 'text': text[:ACTIVITY_TEXT_LIMIT], **{k: v for k, v in extra.items() if v is not None}}
        try:
            self.activity.insert_one(doc)
        except PyMongoError:
            pass

    def add_notices(self, rows: list[dict], same_s: dict[str, int], now: dt.datetime | None = None) -> int:
        now = now or utc_now()
        fresh = []
        for row in rows:
            quiet = now - dt.timedelta(seconds=same_s.get(row['kind'], 0))
            if any(r['user'] == row['user'] and r.get('key') == row.get('key') and r['kind'] == row['kind'] for r in fresh):
                continue
            if self.notices.find_one({'user': row['user'], 'key': row.get('key'), 'kind': row['kind'], 'at': {'$gte': quiet}}, {'_id': 1}):
                continue
            fresh.append({'_id': new_id(), 'at': now, 't': iso(now), 'read': False, **row})
        if fresh:
            self.notices.insert_many(fresh)
        return len(fresh)

    def notices_of(self, user: str, limit: int, before: str | None = None) -> list[dict]:
        query: dict = {'user': user}
        if before:
            edge = self.notices.find_one({'_id': before, 'user': user}, {'at': 1})
            if edge is None:
                return []
            query['$or'] = [{'at': {'$lt': edge['at']}}, {'at': edge['at'], '_id': {'$lt': before}}]
        return [{k: v for k, v in _with_id(d).items() if k not in ('at', 'user')} for d in self.notices.find(query, sort=[('at', DESCENDING), ('_id', DESCENDING)], limit=min(limit, NOTICE_PAGE_MAX))]

    def unread_notices(self, user: str) -> int:
        return self.notices.count_documents({'user': user, 'read': False})

    def read_notices(self, user: str, ids: list[str] | None) -> int:
        query = {'user': user, 'read': False} | ({'_id': {'$in': list(ids)}} if ids is not None else {})
        return self.notices.update_many(query, {'$set': {'read': True, 'readAt': iso(utc_now())}}).modified_count

    def list_activity(self, since: str | None, limit: int, hide: tuple[str, ...] = ()) -> list[dict]:
        query = ({'t': {'$gte': since}} if since else {}) | ({'kind': {'$nin': list(hide)}} if hide else {})
        return [{k: v for k, v in _with_id(d).items() if k != 'at'} for d in self.activity.find(query, sort=[('t', DESCENDING), ('_id', DESCENDING)], limit=limit)]

    def set_investigator_stop(self, stopped: bool, by: str) -> dict | None:
        if not stopped:
            self.meta.delete_one({'_id': STOP_ID})
            return None
        doc = {'_id': STOP_ID, 'by': by, 'at': iso(utc_now())}
        self.meta.replace_one({'_id': STOP_ID}, doc, upsert=True)
        return _without_ids(doc)

    def investigator_stop(self) -> dict | None:
        doc = self.meta.find_one({'_id': STOP_ID})
        return _without_ids(doc) if doc else None

    def small_talk_rules(self) -> dict | None:
        doc = self.meta.find_one({'_id': SMALL_TALK_ID})
        return _without_ids(doc) if doc else None

    def set_small_talk_rules(self, rules: dict) -> dict:
        doc = {**rules, '_id': SMALL_TALK_ID}
        self.meta.replace_one({'_id': SMALL_TALK_ID}, doc, upsert=True)
        return _without_ids(doc)

    def rename_tags(self, renamed: dict[str, str]) -> int:
        if not renamed:
            return 0
        moved = 0
        query = {'$or': [{'tags.id': {'$in': list(renamed)}}, {'tagsOff': {'$in': list(renamed)}}]}
        for card in self.anomalies.find(query, {'_id': 1, 'tags': 1, 'tagsOff': 1}):
            seen, tags = set(), []
            for t in card.get('tags') or []:
                if isinstance(t, dict):
                    t = {**t, 'id': renamed.get(t.get('id'), t.get('id'))}
                    if t['id'] in seen:
                        continue
                    seen.add(t['id'])
                tags.append(t)
            off = list(dict.fromkeys(renamed.get(o, o) for o in card.get('tagsOff') or []))
            self.anomalies.update_one({'_id': card['_id']}, {'$set': {'tags': tags, 'tagsOff': off}})
            moved += 1
        return moved

    def clear_small_talk_rules(self) -> None:
        self.meta.delete_one({'_id': SMALL_TALK_ID})

    def gate_examples(self) -> dict | None:
        doc = self.meta.find_one({'_id': GATE_EXAMPLES_ID})
        return _without_ids(doc) if doc else None

    def set_gate_examples(self, examples: dict) -> dict:
        doc = {**examples, '_id': GATE_EXAMPLES_ID}
        self.meta.replace_one({'_id': GATE_EXAMPLES_ID}, doc, upsert=True)
        return _without_ids(doc)

    def self_reviews(self) -> list[dict]:
        return (self.meta.find_one({'_id': SELF_REVIEW_ID}) or {}).get('reviews') or []

    def page_scan(self, url: str) -> dict | None:
        doc = self.meta.find_one({'_id': PAGE_SCAN_PREFIX + url})
        return _without_ids(doc) if doc else None

    def save_page_scan(self, url: str, doc: dict) -> None:
        self.meta.replace_one({'_id': PAGE_SCAN_PREFIX + url}, {**doc, '_id': PAGE_SCAN_PREFIX + url}, upsert=True)

    def plugin_memo(self, plugin: str, instance: str | None, key: str) -> dict | None:
        doc = self.plugin_memos.find_one({'_id': f'{PLUGIN_MEMO_PREFIX}{plugin}:{instance or PLUGIN_NO_INSTANCE}:{key}'}, {'value': 1})
        value = (doc or {}).get('value')
        return value if isinstance(value, dict) else None

    def save_plugin_memo(self, plugin: str, instance: str | None, key: str, value: dict) -> None:
        at = utc_now()
        name = instance or PLUGIN_NO_INSTANCE
        doc_id = f'{PLUGIN_MEMO_PREFIX}{plugin}:{name}:{key}'
        self.plugin_memos.replace_one({'_id': doc_id}, {'_id': doc_id, 'plugin': plugin, 'instance': name, 'key': key, 'value': value, 'at': iso(at), 'atDt': at}, upsert=True)

    def add_self_review(self, review: dict) -> None:
        reviews = [review] + self.self_reviews()
        self.meta.replace_one({'_id': SELF_REVIEW_ID}, {'_id': SELF_REVIEW_ID, 'reviews': reviews[:SELF_REVIEW_KEEP]}, upsert=True)

    def set_self_review_dm(self, at: str, dm: list[str]) -> None:
        self.meta.update_one({'_id': SELF_REVIEW_ID, 'reviews.at': at}, {'$set': {'reviews.$.dm': dm}})

    def postdeploy_ignore(self) -> dict | None:
        doc = self.meta.find_one({'_id': POSTDEPLOY_IGNORE_ID})
        return _without_ids(doc) if doc else None

    def set_postdeploy_ignore(self, ignore: dict) -> dict:
        doc = {**ignore, '_id': POSTDEPLOY_IGNORE_ID}
        self.meta.replace_one({'_id': POSTDEPLOY_IGNORE_ID}, doc, upsert=True)
        return _without_ids(doc)

    def count_small_talk(self, since: str) -> int:
        return (self.slack_seen.count_documents({'decision': 'small-talk', 'at': {'$gte': since}})
                + self.actions.count_documents({'type': 'ask', 'smallTalk': True, 'requestedAt': {'$gte': since}}))

    def slack_draft(self, key: str, text: str, by: str) -> tuple[int, dict | None, dict | None]:
        doc = self._anomaly(key)
        now = utc_now()
        shots = (doc.get('slackDraft') or {}).get('screenshots')
        draft = {'text': text, 'by': by, 'at': iso(now), **({'screenshots': shots} if shots else {})} if text else None
        auto = auto_send(draft, now) if draft and is_token(by) and doc.get('sev') in AUTO_SEND_SEVERITIES else None
        with self._locked():
            rev, _ = self._claim(None)
            self.anomalies.update_one({'_id': doc['_id']}, draft_update(draft, auto))
            return rev, draft, auto

    def pod_slack_draft(self, key: str, text: str, by: str, screenshots: list[dict] | None = None, hold: str | None = None, headline: str | None = None) -> dict:
        doc = self._anomaly(key)
        now = utc_now()
        draft = {'text': text, 'by': by, 'at': iso(now), **({'screenshots': screenshots} if screenshots else {}), **({'notAutoSent': hold} if hold else {}), **({'headline': headline} if headline else {})}
        auto = None if hold else auto_send(draft, now)
        with self._locked():
            self._claim(None)
            self.anomalies.update_one({'_id': doc['_id']}, draft_update(draft, auto))
            return {'slackDraft': draft, 'autoSend': auto}

    def fallback_slack_draft(self, key: str, text: str, by: str, fired_at: str, now: dt.datetime, skip_triage: tuple[str, ...]) -> dict | None:
        draft = {'text': text, 'by': by, 'at': iso(now)}
        auto = auto_send(draft, now)
        fallback = {'at': draft['at'], 'firedAt': fired_at}
        query = {'_id': key, 'slackDraft': None, 'slackPost.at': {'$exists': False}, 'autoSend.holdBy': {'$exists': False}, 'slackFallback.firedAt': {'$ne': fired_at},
                 'triage': {'$nin': list(skip_triage)}}
        with self._locked():
            self._claim(None)
            if not self.anomalies.update_one(query, {'$set': {'slackDraft': draft, 'autoSend': auto, 'slackFallback': fallback}}).modified_count:
                return None
            return {'slackDraft': draft, 'autoSend': auto, 'slackFallback': fallback}

    def set_tagging(self, key: str, fields: dict) -> tuple[int, dict]:
        doc = self._anomaly(key)
        update = {}
        for field, value in fields.items():
            if value is None:
                update.setdefault('$unset', {})[field] = ''
            else:
                update.setdefault('$set', {})[field] = value
        with self._locked():
            rev, _ = self._claim(None)
            self.anomalies.update_one({'_id': doc['_id']}, update)
            return rev, self.anomaly(key)

    def children(self, key: str) -> list[dict]:
        return [_without_ids(d) for d in self.anomalies.find({'parent': key, '_id': {'$ne': key}}).sort('pos', ASCENDING)]

    def card_numbers(self, keys) -> dict[str, int]:
        return {d['_id']: d['n'] for d in self.card_ids.find({'_id': {'$in': list(keys)}})}

    def card_number(self, key: str) -> int:
        doc = self.card_ids.find_one({'_id': key})
        if doc:
            return doc['n']
        n = self.counters.find_one_and_update({'_id': CARD_COUNTER}, {'$inc': {'n': 1}}, upsert=True, return_document=ReturnDocument.AFTER)['n']
        try:
            self.card_ids.insert_one({'_id': key, 'n': n, 'at': iso(utc_now())})
        except DuplicateKeyError:
            return self.card_ids.find_one({'_id': key})['n']
        return n

    def key_of_number(self, n: int) -> str | None:
        doc = self.card_ids.find_one({'n': n})
        return doc['_id'] if doc else None

    def past_teams_posts(self, key: str, before: str) -> list[dict]:
        doc = self.card_history.find_one({'key': key, 'firstSeen': {'$lt': before}}, {'teamsPosts': 1}, sort=[('firstSeen', DESCENDING)])
        return [p for p in (doc or {}).get('teamsPosts') or [] if isinstance(p, dict)]

    def last_past_card(self, key: str) -> dict | None:
        doc = next(iter(self.card_history.find({'key': key}).sort('firstSeen', DESCENDING).limit(1)), None)
        return _without_ids(doc) if doc else None

    def _list_field(self, key: str, field: str) -> None:
        self.anomalies.update_one({'_id': key, '$and': [{field: None}, {field: {'$exists': True}}]}, {'$unset': {field: ''}})

    def claim_fix(self, key: str, ts: str, h: str) -> bool:
        self._list_field(key, 'fixPosts')
        return self.anomalies.update_one({'_id': key, 'slackPost.ts': ts, 'fixPosts': {'$ne': h}}, {'$push': {'fixPosts': {'$each': [h], '$slice': -FIX_KEEP}}}).modified_count == 1

    def unclaim_fix(self, key: str, h: str) -> None:
        self.anomalies.update_one({'_id': key}, {'$pull': {'fixPosts': h}})

    def teams_claim(self, key: str, route: str, since: str, post: dict, fixed: bool = False, plugin: str | None = None, rev: bool = True) -> bool:
        self._list_field(key, 'teamsPosts')
        same = {'route': route, 'at': {'$gte': since}} | ({'fixed': True} if fixed else {}) | ({'plugin': {'$in': [plugin, None]}} if plugin else {})
        query = {'_id': key, 'teamsPosts': {'$not': {'$elemMatch': same}}}
        with self._locked():
            if not self.anomalies.update_one(query, {'$push': {'teamsPosts': {'$each': [post], '$slice': -TEAMS_KEEP}}}).modified_count:
                return False
            if rev:
                self._claim(None)
            return True

    def teams_unclaim(self, key: str, post: dict, rev: bool = True) -> None:
        with self._locked():
            self.anomalies.update_one({'_id': key}, {'$pull': {'teamsPosts': post}})
            if rev:
                self._claim(None)

    def teams_told(self) -> None:
        with self._locked():
            self._bump()

    def route_retries(self) -> list[dict]:
        return list(self.route_tries.find({}, {'_id': 0}))

    def route_retry(self, row: dict) -> None:
        self.route_tries.replace_one({'route': row['route'], 'key': row['key']}, row, upsert=True)

    def route_retry_done(self, routes: list[str], key: str | None = None) -> None:
        self.route_tries.delete_many({'route': {'$in': routes}} | ({'key': key} if key is not None else {}))

    def route_retry_again(self, routes: list[str]) -> None:
        self.route_tries.update_many({'route': {'$in': routes}}, {'$set': {'tries': 0, 'due': 0, 'gaveUp': False}})

    def route_retries_before(self, at: str) -> None:
        self.route_tries.delete_many({'at': {'$lt': at}})

    def route_outcome(self, route: str, reason: str | None, link: str | None, at: str) -> None:
        self.route_health.replace_one({'_id': route}, {'_id': route, 'reason': reason, 'link': link, 'at': at}, upsert=True)

    def route_outcomes(self) -> dict[str, dict]:
        return {d['_id']: d for d in self.route_health.find({})}

    def route_outcome_done(self, route: str) -> None:
        self.route_health.delete_one({'_id': route})

    def teams_posted(self, key: str, post: dict) -> None:
        doc = self.anomalies.find_one({'_id': key}, {'_id': 1})
        if doc is None:
            return
        with self._locked():
            self._claim(None)
            self._list_field(key, 'teamsPosts')
            self.anomalies.update_one({'_id': key}, {'$push': {'teamsPosts': {'$each': [post], '$slice': -TEAMS_KEEP}}})

    def slack_posted(self, key: str, post: dict) -> int:
        doc = self._anomaly(key)
        with self._locked():
            rev, _ = self._claim(None)
            self.anomalies.update_one({'_id': doc['_id']}, {'$set': {'slackPost': post}})
            return rev

    def offer_card(self, key: str, kind: str, offer: dict) -> bool:
        with self._locked():
            self._claim(None)
            return bool(self.anomalies.update_one({'_id': key, f'{kind}Offered': None}, {'$set': {f'{kind}Offered': offer}}).modified_count)

    def release_card_offer(self, key: str, kind: str, ts: str) -> None:
        with self._locked():
            self._claim(None)
            self.anomalies.update_one({'_id': key, f'{kind}Offered.ts': ts}, {'$unset': {f'{kind}Offered': ''}})

    def offer_ticket(self, key: str, offer: dict) -> bool:
        with self._locked():
            self._claim(None)
            return bool(self.anomalies.update_one({'_id': key, 'status': 'open', 'ticket': {'$ne': offer['ticket']}, 'ticketOffered.ticket': {'$ne': offer['ticket']}},
                                                  {'$set': {'ticketOffered': offer}}).modified_count)

    def release_ticket_offer(self, key: str, ts: str) -> None:
        with self._locked():
            self._claim(None)
            self.anomalies.update_one({'_id': key, 'ticketOffered.ts': ts}, {'$unset': {'ticketOffered': ''}})

    def slack_reminded(self, key: str, post_ts: str, reminder: dict) -> bool:
        with self._locked():
            self._claim(None)
            return bool(self.anomalies.update_one({'_id': key, 'slackPost.ts': post_ts}, {'$set': {'slackPost.reminder': reminder}}).matched_count)

    def slack_clear_noted(self, key: str, post_ts: str, note: dict) -> bool:
        with self._locked():
            self._claim(None)
            return bool(self.anomalies.update_one({'_id': key, 'slackPost.ts': post_ts}, {'$set': {'slackPost.clearNote': note}}).matched_count)

    def slack_thread_updated(self, key: str, post_ts: str, at: str) -> bool:
        with self._locked():
            self._claim(None)
            return bool(self.anomalies.update_one({'_id': key, 'slackPost.ts': post_ts}, {'$set': {'slackPost.update': {'at': at}}}).matched_count)

    def slack_state_noted(self, key: str, post_ts: str, note: dict) -> bool:
        with self._locked():
            self._claim(None)
            return bool(self.anomalies.update_one({'_id': key, 'slackPost.ts': post_ts}, {'$set': {'slackPost.stateNote': note}}).matched_count)

    def slack_post_head(self, key: str, post_ts: str, head: dict) -> bool:
        with self._locked():
            self._claim(None)
            return bool(self.anomalies.update_one({'_id': key, 'slackPost.ts': post_ts}, {'$set': {'slackPost.head': head}}).matched_count)

    def route_echo_pend(self, key: str, entries: list[dict]) -> bool:
        if self.anomalies.find_one({'_id': key}, {'_id': 1}) is None:
            return False
        ids = [e['id'] for e in entries]
        self.anomalies.update_one({'_id': key}, {'$pull': {'routeEcho.pending': {'id': {'$in': ids}}}})
        self.anomalies.update_one({'_id': key}, {'$push': {'routeEcho.pending': {'$each': list(entries), '$slice': -ECHO_KEEP}}})
        return True

    def route_echo_keys(self) -> list[str]:
        docs = list(self.anomalies.find({'routeEcho.pending': {'$exists': True, '$ne': []}}, {'routeEcho.pending': 1}))
        return [d['_id'] for d in sorted(docs, key=lambda d: min((str(p.get('at') or '') for p in d['routeEcho']['pending'] if isinstance(p, dict)), default=''))]

    def route_echo_name(self, key: str, ts: str, ids: list[str]) -> list[dict]:
        before = self.anomalies.find_one_and_update({'_id': key}, {'$pull': {'routeEcho.pending': {'id': {'$in': list(ids)}}}, '$set': {'routeEcho.fails': 0}}, return_document=ReturnDocument.BEFORE)
        if before is None:
            return []
        echo = before.get('routeEcho') or {}
        named = set(echo.get('routes') or []) if echo.get('ts') == ts else set()
        chosen = [p for p in echo.get('pending') or [] if isinstance(p, dict) and p.get('id') in ids and p['id'] not in named]
        if chosen:
            if echo.get('ts') == ts:
                self.anomalies.update_one({'_id': key}, {'$addToSet': {'routeEcho.routes': {'$each': [p['id'] for p in chosen]}}})
            else:
                self.anomalies.update_one({'_id': key}, {'$set': {'routeEcho.routes': sorted(p['id'] for p in chosen), 'routeEcho.ts': ts}})
        return chosen

    def route_echo_drop(self, key: str, ids: list[str], reset: bool = False) -> bool:
        update = {'$pull': {'routeEcho.pending': {'id': {'$in': list(ids)}}}} | ({'$set': {'routeEcho.fails': 0}} if reset else {})
        return self.anomalies.update_one({'_id': key}, update).matched_count == 1

    def route_echo_fail(self, key: str, now: float, kind: str = 'post') -> int:
        doc = self.anomalies.find_one_and_update({'_id': key}, {'$inc': {'routeEcho.fails': 1}, '$set': {'routeEcho.failedAt': now, 'routeEcho.failKind': kind}}, return_document=ReturnDocument.AFTER)
        return int(((doc or {}).get('routeEcho') or {}).get('fails') or 0)

    def route_echo_claim(self, key: str, holder: str, now: float, lease: float) -> bool:
        free = {'$or': [{'routeEcho.posting.until': {'$exists': False}}, {'routeEcho.posting.until': {'$lte': now}}]}
        return self.anomalies.update_one({'_id': key, **free}, {'$set': {'routeEcho.posting': {'by': holder, 'until': now + lease}}}).matched_count == 1

    def route_echo_release(self, key: str, holder: str) -> None:
        self.anomalies.update_one({'_id': key, 'routeEcho.posting.by': holder}, {'$unset': {'routeEcho.posting': ''}})

    def slack_post_drawn(self, key: str, post_ts: str, drawn: dict) -> bool:
        return bool(self.anomalies.update_one({'_id': key, 'slackPost.ts': post_ts}, {'$set': {'slackPost.drawn': drawn}}).matched_count)

    def _bump(self) -> None:
        try:
            self._claim(None)
        except Conflict:
            pass

    def _auto_due(self, coll, now: str) -> list[dict]:
        return list(coll.find({'autoSend.at': {'$lte': now}, **AUTO_PENDING}))

    def _auto_claim(self, coll, doc_id: str, draft: dict, now: str) -> dict | None:
        query = {'_id': doc_id, 'slackDraft.at': draft['at'], 'slackDraft.text': draft['text'], 'autoSend.draftAt': draft['at'], 'autoSend.at': {'$lte': now}, **AUTO_PENDING}
        return coll.find_one_and_update(query, {'$set': {'autoSend.claimedAt': now}}, return_document=ReturnDocument.AFTER)

    def _auto_hold(self, coll, doc_id: str, by: str) -> dict | None:
        query = {'_id': doc_id, 'autoSend.at': {'$exists': True}, **AUTO_PENDING}
        doc = coll.find_one_and_update(query, {'$set': {'autoSend.holdBy': by, 'autoSend.holdAt': iso(utc_now())}}, return_document=ReturnDocument.AFTER)
        return doc['autoSend'] if doc else None

    def _auto_failed(self, coll, doc_id: str, error: str) -> None:
        now = utc_now()
        doc = coll.find_one({'_id': doc_id}, {'autoSend.retries': 1}) or {}
        retries = int((doc.get('autoSend') or {}).get('retries') or 0)
        fields = {'autoSend.failedAt': iso(now), 'autoSend.error': error[:ERROR_LIMIT]}
        if retries < AUTO_RETRIES:
            coll.update_one({'_id': doc_id}, {'$set': fields | {'autoSend.at': iso(now + dt.timedelta(seconds=AUTO_RETRY_S)), 'autoSend.retries': retries + 1},
                                              '$unset': {'autoSend.claimedAt': ''}})
            return
        coll.update_one({'_id': doc_id}, {'$set': fields})

    def due_anomaly_auto_sends(self, now: str) -> list[dict]:
        return [_without_ids(a) for a in self._auto_due(self.anomalies, now)]

    def claim_anomaly_auto_send(self, key: str, draft: dict, now: str) -> dict | None:
        with self._locked():
            doc = self._auto_claim(self.anomalies, key, draft, now)
            if doc is None:
                return None
            self._bump()
            return _without_ids(doc)

    def hold_anomaly_auto_send(self, key: str, by: str) -> dict | None:
        self._anomaly(key)
        with self._locked():
            held = self._auto_hold(self.anomalies, key, by)
            if held:
                self._bump()
            return held

    def stop_anomaly_auto_send(self, key: str) -> None:
        with self._locked():
            if self.anomalies.update_one({'_id': key}, {'$unset': {'autoSend': ''}}).modified_count:
                self._bump()

    def anomaly_auto_send_failed(self, key: str, error: str) -> None:
        self._auto_failed(self.anomalies, key, error)

    def last_action(self, key: str) -> dict | None:
        doc = next(iter(self.actions.find({'target': key, 'type': {'$in': list(CARD_ACTION_TYPES)}}, sort=[('requestedAt', DESCENDING), ('_id', DESCENDING)], limit=1)), None)
        return {k: doc.get(k) for k in ('type', 'status', 'label', 'requestedBy', 'requestedAt', 'result', 'doneAt') if doc.get(k) is not None} if doc else None

    def list_actions(self, status: str | None, limit: int) -> list[dict]:
        query = {'status': status} if status else {}
        return [_with_id(a) for a in self.actions.find(query, sort=[('requestedAt', DESCENDING), ('_id', DESCENDING)], limit=limit)]

    def count_actions(self, action_type: str, since: str, source: str | None = None, exclude_source: str | None = None, by: str | None = None) -> int:
        query = {'type': action_type, 'requestedAt': {'$gte': since}, 'smallTalk': {'$ne': True}, 'copied': {'$ne': True}} | ({'requestedBy': by} if by else {})
        if source is not None:
            query['source'] = source
        elif exclude_source is not None:
            query['source'] = {'$ne': exclude_source}
        return self.actions.count_documents(query)

    def action_done_at(self, kind: str, target: str) -> str | None:
        doc = next(iter(self.actions.find({'type': kind, 'target': target, 'status': 'done'}, {'doneAt': 1}, sort=[('doneAt', DESCENDING), ('_id', DESCENDING)], limit=1)), None)
        return doc.get('doneAt') if doc else None

    def add_action(self, action: dict, status: str = 'requested', result: str | None = None) -> dict:
        now = iso(utc_now())
        doc = {**action, '_id': new_id(), 'status': status, 'requestedAt': now}
        if status != 'requested':
            doc[f'{status}At'] = now
        if result is not None:
            doc['result'] = result
        self.actions.insert_one(doc)
        return _with_id(doc)

    def update_action(self, action_id: str, status: str, result: str | None, expect: str | None, report: str | None = None) -> dict:
        allowed = [s for s in ACTION_MOVES_FROM[status] if not expect or s == expect]
        query = {'_id': action_id, 'status': {'$in': allowed}}
        update = {'status': status, f'{status}At': iso(utc_now())}
        if result is not None:
            update['result'] = result
        if report is not None:
            update['report'] = report
        doc = self.actions.find_one_and_update(query, {'$set': update}, return_document=ReturnDocument.AFTER)
        if doc:
            return _with_id(doc)
        current = self.actions.find_one({'_id': action_id})
        if not current:
            raise NotFound(f'no action with id {action_id}')
        raise ActionMoved(current.get('status', 'unknown'))

    def hand_back_action(self, action_id: str) -> None:
        if self.actions.update_one({'_id': action_id, 'status': 'processing'}, {'$set': {'status': 'requested'}, '$unset': {'processingAt': ''}}).matched_count == 0:
            current = self.actions.find_one({'_id': action_id})
            if not current:
                raise NotFound(f'no action with id {action_id}')
            raise ActionMoved(current.get('status', 'unknown'))

    def get_action(self, action_id: str) -> dict:
        doc = self.actions.find_one({'_id': action_id})
        if not doc:
            raise NotFound(f'no action with id {action_id}')
        return _with_id(doc)

    def set_action_fields(self, action_id: str, fields: dict) -> None:
        if self.actions.update_one({'_id': action_id}, {'$set': fields}).matched_count == 0:
            raise NotFound(f'no action with id {action_id}')

    def open_thread_ask(self, channel: str, thread_ts: str) -> dict | None:
        doc = self.actions.find_one({'type': 'ask', 'source': 'slack', 'slack.channel': channel, 'slack.threadTs': thread_ts, 'status': {'$in': list(OPEN_ACTIONS)}},
                                    sort=[('requestedAt', ASCENDING), ('_id', ASCENDING)])
        return _with_id(doc) if doc else None

    def thread_answers(self, channel: str, thread_ts: str, since: str) -> list[str]:
        query = {'type': 'ask', 'source': 'slack', 'slack.channel': channel, 'slack.threadTs': thread_ts, 'slackReply.at': {'$gte': since}, 'slackReply.text': {'$type': 'string'}}
        return [d['slackReply']['text'] for d in self.actions.find(query, {'slackReply': 1}, sort=[('slackReply.at', ASCENDING), ('_id', ASCENDING)])]

    def requests(self, limit: int) -> list[dict]:
        asks = [_with_id(d) for d in self.actions.find({'type': 'ask', 'smallTalk': {'$ne': True}, 'source': {'$ne': 'chat'}}, REQUEST_FIELDS, sort=[('requestedAt', DESCENDING), ('_id', DESCENDING)], limit=limit)]
        runs = {}
        for d in self.investigations.find({'actionId': {'$in': [a['id'] for a in asks]}}, REQUEST_RUN_FIELDS, sort=[('startedAt', ASCENDING)]):
            runs[d['actionId']] = {'id': d['_id'], 'outcome': d.get('outcome'), 'mode': d.get('mode'), 'startedAt': d.get('startedAt'), 'usd': d.get('usd'),
                                   'recap': d.get('recap'), 'toolCalls': len(d.get('toolCalls') or [])}
        return [a | {'run': runs.get(a['id'])} for a in asks]

    def chat_asks(self, by: str, chat_id: str | None = None) -> list[dict]:
        query = {'type': 'ask', 'source': 'chat', 'requestedBy': by, 'chat.hidden': {'$ne': True}} | ({'chat.id': chat_id} if chat_id else {})
        found = self.actions.find(query, CHAT_FIELDS, sort=[('requestedAt', DESCENDING), ('_id', DESCENDING)], limit=CHAT_SCAN)
        return [_with_id(d) for d in found][::-1]

    def chats(self, by: str, limit: int) -> list[dict]:
        rows: dict[str, dict] = {}
        for a in self.chat_asks(by):
            chat_id = (a.get('chat') or {}).get('id')
            if not chat_id:
                continue
            row = rows.setdefault(chat_id, {'id': chat_id, 'title': chat_title(a), 'startedAt': a.get('requestedAt'), 'messages': 0, 'cards': a.get('cards') or []}
                                  | ({'slack': True} if (a.get('chat') or {}).get('dm') else {}))
            row.update(messages=row['messages'] + 1, updatedAt=a.get('doneAt') or a.get('failedAt') or a.get('requestedAt'), pending=a.get('status') in OPEN_ACTIONS, last=a['id'])
        ordered = sorted(rows.values(), key=lambda c: (c['updatedAt'] or '', c['last']), reverse=True)[:limit]
        return [{k: v for k, v in c.items() if k != 'last'} for c in ordered]

    def chat(self, by: str, chat_id: str) -> dict | None:
        asks = self.chat_asks(by, chat_id)
        if not asks:
            return None
        runs = {d['actionId']: d for d in self.investigations.find({'actionId': {'$in': [a['id'] for a in asks]}}, CHAT_RUN_FIELDS, sort=[('startedAt', ASCENDING)])}
        progress = self.tool_progress([r['_id'] for r in runs.values() if r.get('outcome') == 'running'])
        messages = []
        for a in asks:
            run, open_now = runs.get(a['id']) or {}, a.get('status') in OPEN_ACTIONS
            answer = None if open_now or a.get('status') != 'done' else run.get('recap') or a.get('result')
            message = {'id': a['id'], 'text': a.get('text'), 'at': a.get('requestedAt'), 'status': a.get('status'), 'cards': a.get('cards') or [], 'answer': answer,
                       'answeredAt': a.get('doneAt') or a.get('failedAt'), 'runId': run.get('_id'), 'usd': run.get('usd'), 'cardAction': run.get('cardAction') if not open_now else None,
                       'error': a.get('result') if a.get('status') in ('failed', 'cancelled') else None}
            if open_now and run.get('_id') in progress:
                message['progress'] = progress[run['_id']]
            messages.append({k: v for k, v in message.items() if v is not None})
        dm = bool((asks[0].get('chat') or {}).get('dm'))
        return {'id': chat_id, 'title': chat_title(asks[0]), 'cards': asks[0].get('cards') or [], 'messages': messages,
                'pending': any(a.get('status') in OPEN_ACTIONS for a in asks)} | ({'slack': True} if dm else {})

    def dm_chat(self, chat_id: str) -> bool:
        return self.actions.find_one({'type': 'ask', 'source': 'chat', 'chat.id': chat_id, 'chat.dm': {'$exists': True}}, {'_id': 1}) is not None

    def dm_chat_id(self, by: str, channel: str, since: str) -> str | None:
        doc = self.actions.find_one({'type': 'ask', 'source': 'chat', 'requestedBy': by, 'chat.dm': channel, 'chat.hidden': {'$ne': True}, 'requestedAt': {'$gte': since}},
                                    {'chat': 1}, sort=[('requestedAt', DESCENDING), ('_id', DESCENDING)])
        return ((doc or {}).get('chat') or {}).get('id')

    def continue_chat(self, reader: str, chat_id: str) -> str:
        chat = self.chat_for(reader, chat_id)
        if chat is None:
            raise NotFound(f'no chat with id {chat_id}, or it is not shared with you')
        done = [m for m in chat['messages'] if m.get('status') == 'done' and m.get('answer')]
        if not done:
            raise NotFound('that chat has no answered message to continue from')
        fresh, now = new_id(), iso(utc_now())
        cards = list(chat.get('cards') or [])
        for m in done:
            self.actions.insert_one({'_id': new_id(), 'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': f'chat:{fresh}', 'text': m.get('text') or '', 'label': (m.get('text') or '')[:300],
                                     'cards': cards, 'requestedBy': reader, 'chat': {'id': fresh, 'from': chat_id}, 'copied': True, 'status': 'done',
                                     'requestedAt': m.get('at') or now, 'doneAt': now, 'result': m['answer']})
        return fresh

    def chat_owner(self, chat_id: str) -> str | None:
        doc = self.actions.find_one({'type': 'ask', 'source': 'chat', 'chat.id': chat_id}, {'requestedBy': 1})
        return (doc or {}).get('requestedBy')

    def hide_chat(self, by: str, chat_id: str) -> None:
        self.actions.update_many({'type': 'ask', 'source': 'chat', 'requestedBy': by, 'chat.id': chat_id}, {'$set': {'chat.hidden': True}})
        self.chat_shares.delete_one({'_id': chat_id})

    def chat_shared(self, chat_id: str) -> bool:
        return self.chat_shares.find_one({'_id': chat_id}) is not None

    def share_chat(self, by: str, chat_id: str, on: bool) -> bool:
        if self.chat_owner(chat_id) != by:
            raise NotFound(f'no chat with id {chat_id}')
        if on and self.dm_chat(chat_id):
            raise ChatPrivate('a chat that started in a Slack direct message stays private and cannot be shared')
        if on:
            self.chat_shares.replace_one({'_id': chat_id}, {'_id': chat_id, 'by': by, 'at': iso(utc_now()), 'n': len(self.chat_asks(by, chat_id))}, upsert=True)
        else:
            self.chat_shares.delete_one({'_id': chat_id})
        return on

    def chat_for(self, reader: str, chat_id: str) -> dict | None:
        owner = self.chat_owner(chat_id)
        if owner is None:
            return None
        share = self.chat_shares.find_one({'_id': chat_id})
        if owner != reader and share is None:
            return None
        chat = self.chat(owner, chat_id)
        if not chat:
            return None
        if owner != reader:
            upto = share.get('at') or ''
            kept = chat['messages'][:share['n']] if isinstance(share.get('n'), int) else [m for m in chat['messages'] if (m.get('at') or '') <= upto]
            chat = chat | {'messages': kept, 'pending': any(m.get('status') in OPEN_ACTIONS for m in kept), 'snapshotAt': upto}
        return chat | {'mine': owner == reader, 'by': owner, 'shared': share is not None}

    def channel_ask_root(self, channel: str, thread_ts: str) -> dict | None:
        query = {'type': 'ask', 'source': 'slack', 'slack.where': 'channel', 'slack.channel': channel, 'slack.threadTs': thread_ts}
        doc = self.actions.find_one(query, sort=[('requestedAt', ASCENDING), ('_id', ASCENDING)])
        return _with_id(doc) if doc else None

    def claim_ticket(self, action_id: str, by: str) -> dict | None:
        doc = self.actions.find_one_and_update({'_id': action_id, 'ticket': {'$exists': False}}, {'$set': {'ticket': {'claimedBy': by, 'claimedAt': iso(utc_now())}}},
                                               return_document=ReturnDocument.AFTER)
        return _with_id(doc) if doc else None

    def release_ticket(self, action_id: str) -> None:
        self.actions.update_one({'_id': action_id, 'ticket.key': {'$exists': False}}, {'$unset': {'ticket': ''}})

    def ticket_created(self, action_id: str, key: str) -> None:
        self.actions.update_one({'_id': action_id}, {'$set': {'ticket.key': key, 'ticket.at': iso(utc_now())}})

    def claim_card_action(self, action_id: str, by: str, label: str | None = None, call: dict | None = None) -> dict | None:
        claim = {'claimedBy': by, 'claimedAt': iso(utc_now())} | ({'label': label} if label else {}) | ({'call': call} if call else {})
        doc = self.actions.find_one_and_update({'_id': action_id, 'cardActionApplied': {'$exists': False}}, {'$set': {'cardActionApplied': claim}},
                                               return_document=ReturnDocument.AFTER)
        return _with_id(doc) if doc else None

    def release_card_action(self, action_id: str) -> None:
        self.actions.update_one({'_id': action_id, 'cardActionApplied.at': {'$exists': False}}, {'$unset': {'cardActionApplied': ''}})

    def card_action_applied(self, action_id: str) -> None:
        self.actions.update_one({'_id': action_id}, {'$set': {'cardActionApplied.at': iso(utc_now())}})

    def _linked(self, report: dict) -> dict:
        return self.row_links(report) if self.row_links is not None else report

    def list_deploy_reports(self, limit: int) -> list[dict]:
        return [self._linked(_with_id(r)) for r in self.deploy_reports.find({}, sort=[('createdAt', DESCENDING), ('_id', DESCENDING)], limit=limit)]

    def add_deploy_report(self, report: dict) -> dict:
        replaces = report.pop('replaces', None) or []
        old = {r['_id']: r for r in self.deploy_reports.find({'_id': {'$in': replaces}}, {'actionId': 1})}
        missing = [i for i in replaces if i not in old]
        if missing:
            raise NotFound(f'no deploy report with id {missing[0]}')
        action_id = report.get('actionId') or next((old[i]['actionId'] for i in replaces if old[i].get('actionId')), None)
        created = utc_now().isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        doc = {**report, '_id': new_id(), 'createdAt': created}
        if doc.get('brief'):
            doc['brief'] = {**doc['brief'], 'at': created}
        if action_id:
            action = self.actions.find_one({'_id': action_id})
            if not action:
                raise NotFound(f'no action with id {action_id}')
            doc['actionId'] = action_id
            if action.get('requestedBy'):
                doc['requestedBy'] = action['requestedBy']
        self._linked(doc)
        self.deploy_reports.insert_one(doc)
        if replaces:
            self.actions.update_many({'reportId': {'$in': replaces}}, {'$set': {'reportId': doc['_id']}})
            self.deploy_reports.delete_many({'_id': {'$in': replaces}})
        if action_id:
            self.actions.update_one({'_id': action_id}, {'$set': {'reportId': doc['_id']}})
        return _with_id(doc)

    def add_deploys(self, deploys: list[dict]) -> dict:
        now = iso(utc_now())
        inserted = ignored = 0
        for d in deploys:
            if self.self_deploy and f"{d['ns']}/{d['svc']}" == self.self_deploy:
                ignored += 1
                continue
            image = {'ns': d['ns'], 'svc': d['svc'], 'toImage': d['toImage']}
            seq = self._deploy_seq(image, d)
            key = image | ({'seq': seq} if seq else {'seq': {'$exists': False}})
            update = {'$setOnInsert': {'_id': new_id(), **image, **({'seq': seq} if seq else {}), 'fromImage': d.get('fromImage'), 'createdAt': now} | ({'cluster': d['cluster']} if d.get('cluster') else {}), '$min': {'at': d['at']}}
            fresh = {k: d[k] for k in ('imageBy', 'tickets') if d.get(k)}
            if d.get('releases') is not None:
                fresh['releases'] = d['releases']
            if d.get('tickets'):
                update['$unset'] = {'ticketsNote': ''}
            elif d.get('ticketsNote'):
                fresh['ticketsNote'] = d['ticketsNote']
            if fresh:
                update['$set'] = fresh
            try:
                inserted += self.deploys.update_one(key, update, upsert=True).upserted_id is not None
            except DuplicateKeyError:
                self.deploys.update_one(key, {k: v for k, v in update.items() if k != '$setOnInsert'})
        out = {'received': len(deploys), 'inserted': inserted, 'existing': len(deploys) - inserted - ignored}
        return out | {'ignored': ignored} if ignored else out

    def _deploy_seq(self, image: dict, d: dict) -> int | None:
        rows = list(self.deploys.find(image, {'at': 1, 'fromImage': 1, 'seq': 1}))
        same = [r for r in rows if same_deploy(r, d)]
        if same:
            return min(same, key=lambda r: deploy_gap(r, d)).get('seq')
        return max(r.get('seq') or 0 for r in rows) + 1 if rows else None

    def list_deploys(self, days: int) -> list[dict]:
        since = iso(utc_now() - dt.timedelta(days=days))
        return [_with_id(d) for d in self.deploys.find({'at': {'$gte': since}}, sort=[('at', DESCENDING), ('_id', DESCENDING)])]

    def delete_deploy(self, deploy_id: str) -> None:
        if self.deploys.delete_one({'_id': deploy_id}).deleted_count == 0:
            raise NotFound(f'no deploy with id {deploy_id}')

    def deploys_since(self, since: str) -> list[dict]:
        return [_with_id(d) for d in self.deploys.find({'at': {'$gte': since}}, sort=[('at', ASCENDING), ('_id', ASCENDING)])]

    def deploys_by_keys(self, keys: list[tuple[str, str, str]]) -> list[dict]:
        if not keys:
            return []
        query = {'$or': [{'ns': ns, 'svc': svc, 'toImage': image} for ns, svc, image in keys]}
        return [_with_id(d) for d in self.deploys.find(query, sort=[('at', ASCENDING), ('_id', ASCENDING)])]

    def update_deploy(self, deploy_id: str, guard: dict, update: dict) -> bool:
        return self.deploys.update_one({'_id': deploy_id, **guard}, update).modified_count > 0

    def claim_run(self, name: str, boot: str | None = None) -> bool:
        try:
            self.runs.insert_one({'_id': name, 'at': utc_now()} | ({'boot': boot, 'done': False} if boot else {}))
        except DuplicateKeyError:
            return False
        return True

    def finish_run(self, name: str, boot: str) -> None:
        self.runs.update_one({'_id': name, 'boot': boot}, {'$set': {'done': True}})

    def beat_run(self, boot: str, now: float) -> None:
        self.runs.update_one({'_id': f'boot:{boot}'}, {'$set': {'at': utc_now(), 'beatAt': now}}, upsert=True)

    def retake_run(self, name: str, boot: str, stale_before: float, limit: int) -> bool:
        doc = self.runs.find_one({'_id': name, 'done': False})
        if not doc or not doc.get('boot') or doc['boot'] == boot or (doc.get('retakes') or 0) >= limit:
            return False
        if ((self.runs.find_one({'_id': f"boot:{doc['boot']}"}) or {}).get('beatAt') or 0) >= stale_before:
            return False
        query = {'_id': name, 'done': False, 'boot': doc['boot'], 'retakes': doc.get('retakes')}
        return self.runs.update_one(query, {'$set': {'boot': boot, 'retakenAt': utc_now()}, '$inc': {'retakes': 1}}).modified_count == 1

    def reports_pages_due(self, now: str, stuck_before: str) -> list[dict]:
        query = {'$or': [{'pages.state': 'waiting', 'pages.readyAt': {'$lte': now}}, {'pages.state': 'checking', 'pages.claimedAt': {'$lte': stuck_before}}]}
        return [_with_id(r) for r in self.deploy_reports.find(query, sort=[('createdAt', ASCENDING), ('_id', ASCENDING)])]

    def claim_report_pages(self, report_id: str, seen: dict, now: str) -> bool:
        query = {'_id': report_id, 'pages.state': seen.get('state'), 'pages.claimedAt': seen.get('claimedAt')}
        return self.deploy_reports.update_one(query, {'$set': {'pages.state': 'checking', 'pages.claimedAt': now}}).modified_count == 1

    def reports_since(self, since: str) -> list[dict]:
        return [_with_id(r) for r in self.deploy_reports.find({'createdAt': {'$gte': since}}, {'rows': 1, 'createdAt': 1, 'windowFrom': 1, 'windowTo': 1})]

    def postdeploy_actions(self, since: str) -> list[dict]:
        query = {'type': 'postdeploy', '$or': [{'status': {'$in': list(OPEN_ACTIONS)}}, {'requestedAt': {'$gte': since}}]}
        return [_with_id(a) for a in self.actions.find(query, sort=[('requestedAt', DESCENDING)])]

    def deploy_report(self, report_id: str) -> dict:
        doc = self.deploy_reports.find_one({'_id': report_id})
        if not doc:
            raise NotFound(f'no deploy report with id {report_id}')
        return self._linked(_with_id(doc))

    def report_slack_draft(self, report_id: str, text: str, by: str, screenshots: list[dict] | None = None, hold: str | None = None) -> dict:
        now = utc_now()
        if screenshots is None:
            old = self.deploy_reports.find_one({'_id': report_id}, {'slackDraft.screenshots': 1}) or {}
            screenshots = (old.get('slackDraft') or {}).get('screenshots')
        draft = {'text': text, 'by': by, 'at': iso(now), **({'screenshots': screenshots} if screenshots else {}), **({'notAutoSent': hold} if hold else {})} if text else None
        auto = auto_send(draft, now) if draft and auto_sender(by) and not hold else None
        if self.deploy_reports.update_one({'_id': report_id}, draft_update(draft, auto)).matched_count == 0:
            raise NotFound(f'no deploy report with id {report_id}')
        return {'slackDraft': draft, 'autoSend': auto}

    def due_report_auto_sends(self, now: str) -> list[dict]:
        return [_with_id(r) for r in self._auto_due(self.deploy_reports, now)]

    def claim_report_auto_send(self, report_id: str, draft: dict, now: str) -> dict | None:
        doc = self._auto_claim(self.deploy_reports, report_id, draft, now)
        return _with_id(doc) if doc else None

    def hold_report_auto_send(self, report_id: str, by: str) -> dict | None:
        if self.deploy_reports.find_one({'_id': report_id}, {'_id': 1}) is None:
            raise NotFound(f'no deploy report with id {report_id}')
        return self._auto_hold(self.deploy_reports, report_id, by)

    def stop_report_auto_send(self, report_id: str) -> None:
        self.deploy_reports.update_one({'_id': report_id}, {'$unset': {'autoSend': ''}})

    def report_auto_send_failed(self, report_id: str, error: str) -> None:
        self._auto_failed(self.deploy_reports, report_id, error)

    def report_brief(self, report_id: str, brief: dict) -> dict:
        report = self.deploy_reports.find_one({'_id': report_id}, {'rows': 1})
        if not report:
            raise NotFound(f'no deploy report with id {report_id}')
        flagged = any(r.get('flags') for r in report.get('rows') or [])
        brief = {**brief, 'level': brief.get('level') or ('warn' if flagged else 'ok'), 'at': iso(utc_now())}
        self.deploy_reports.update_one({'_id': report_id}, {'$set': {'brief': brief}})
        return brief

    def set_report_fields(self, report_id: str, fields: dict) -> None:
        if self.deploy_reports.update_one({'_id': report_id}, {'$set': fields}).matched_count == 0:
            raise NotFound(f'no deploy report with id {report_id}')

    def related_post(self, card: dict, since: str) -> dict | None:
        roots = {'slackPost.ts': {'$exists': True}, 'slackPost.threadOf': {'$exists': False}, 'slackPost.digest': {'$ne': True}, 'slackPost.at': {'$gte': since},
                 '_id': {'$ne': card.get('key')}, 'triage': {'$ne': 'handled'}, 'muted': None}
        tries = [{'_id': card['parent']}] if card.get('parent') and card['parent'] != card.get('key') else []
        tries += [{field: card[field]} for field in ('svc', 'ns') if card.get(field) not in (None, '', '-', '?')]
        for t in tries:
            doc = self.anomalies.find_one(roots | t, {'slackPost': 1}, sort=[('slackPost.at', DESCENDING)])
            if doc:
                return {'key': doc['_id'], 'channel': doc['slackPost'].get('channel'), 'ts': doc['slackPost']['ts']}
        return None

    def report_day_thread(self, start: str, end: str, exclude: str) -> dict | None:
        query = {'_id': {'$ne': exclude}, 'slackPost.at': {'$gte': start, '$lt': end}, 'slackPost.ts': {'$exists': True}, 'slackPost.threadTs': {'$exists': False}}
        doc = self.deploy_reports.find_one(query, {'slackPost': 1}, sort=[('slackPost.at', 1)])
        return doc['slackPost'] if doc else None

    def report_slack_posted(self, report_id: str, post: dict) -> None:
        if self.deploy_reports.update_one({'_id': report_id}, {'$set': {'slackPost': post}}).matched_count == 0:
            raise NotFound(f'no deploy report with id {report_id}')

    def unlinked_slack_posts(self) -> bool:
        return any(coll.find_one(UNLINKED_POST, {'_id': 1}) for coll in (self.anomalies, self.deploy_reports))

    def fill_slack_links(self, link: Callable[[str, str], str | None]) -> int:
        filled = 0
        for coll in (self.anomalies, self.deploy_reports):
            urls = [(doc['_id'], doc['slackPost']['ts'], link(doc['slackPost'].get('channel'), doc['slackPost']['ts'])) for doc in coll.find(UNLINKED_POST, {'slackPost': 1})]
            urls = [u for u in urls if u[2]]
            with self._locked() if urls and coll is self.anomalies else contextlib.nullcontext():
                if urls and coll is self.anomalies:
                    self._claim(None)
                for doc_id, ts, url in urls:
                    if coll.update_one({'_id': doc_id, 'slackPost.ts': ts}, {'$set': {'slackPost.permalink': url}}).modified_count:
                        filled += 1
        return filled

    def delete_deploy_report(self, report_id: str) -> None:
        if self.deploy_reports.delete_one({'_id': report_id}).deleted_count == 0:
            raise NotFound(f'no deploy report with id {report_id}')
        self.actions.update_many({'reportId': report_id}, {'$unset': {'reportId': ''}})

    def seen(self, channel: str, ts: str, at: dt.datetime) -> bool:
        try:
            self.slack_seen.insert_one({'_id': f'{channel}:{ts}', 'channel': channel, 'ts': ts, 'at': iso(at), 'atDt': at})
            return False
        except DuplicateKeyError:
            return True

    def record_intake(self, channel: str, ts: str, fields: dict) -> None:
        self.slack_seen.update_one({'_id': f'{channel}:{ts}'}, {'$set': fields})

    def list_intake(self, limit: int) -> list[dict]:
        return [_with_id(d) for d in self.slack_seen.find({}, {'atDt': 0}, sort=[('at', DESCENDING), ('_id', DESCENDING)], limit=limit)]

    def count_intake(self, decision: str, since: str, user: str) -> int:
        return self.slack_seen.count_documents({'decision': decision, 'user': user, 'at': {'$gte': since}})

    def count_gate_calls(self, since: str) -> int:
        return self.slack_seen.count_documents({'gate': {'$exists': True}, 'at': {'$gte': since}})
