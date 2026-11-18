import base64
import binascii
import datetime as dt
import logging
import time
from collections.abc import Callable
from urllib.parse import quote

import bson
from bson import Binary
from pymongo.errors import PyMongoError

from skopax import clock
from skopax.cardlink import card_url
from skopax.contract import Check, InstanceInfo, Plugin, Sweep
from skopax.engine import SHOTS, iso, mute_for
from skopax.errkind import kind
from skopax.investigator.tools import IMAGE_MAX_BYTES, IMAGE_TYPES, shot_facts
from skopax.messages import msg
from skopax.postdeploy import SERVER_BY
from skopax.slack import SlackError, cut_words, esc
from skopax.store import PLUGIN_NO_INSTANCE, Conflict, NotFound, new_id

log = logging.getLogger('skopax.sweepout')

SHOTS_MAX = 20
POSTS_MAX = 3
POST_TEXT_MAX = 3000
THREAD_PICTURES = 8
REPLY_PICTURES = 3
CARD_SHOTS = 3
MEMOS_MAX = 200
MEMO_KEY_MAX = 300
MEMO_BYTES = 4 * 1024 * 1024
TITLE_MAX = 300
LINE_MAX = 500
LABEL_MAX = 200
HANDLED = ('handled', 'progress')
BULLET = '\u2022'
DOT = ' \u00b7 '
DIGEST = 'page-digest'


class Malformed(Exception):
    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


def counted(n: int, one: str, many: str) -> str:
    return f'{n} {one if n == 1 else many}'


def filled(value: object) -> bool:
    return isinstance(value, str) and value != ''


def whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def flat(text: str) -> str:
    return ' '.join(text.split())


def shot_of(item: object) -> dict | None:
    if not isinstance(item, dict) or not filled(item.get('key')) or item.get('media_type') not in IMAGE_TYPES or not isinstance(item.get('data'), str):
        return None
    try:
        data = base64.b64decode(item['data'], validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > IMAGE_MAX_BYTES:
        return None
    return {'key': item['key'], 'mime': item['media_type'], 'data': data, 'facts': shot_facts(item)}


def shots_of(raw: object, dropped: list[str]) -> list[dict | None]:
    if not isinstance(raw, list):
        raise Malformed('shot')
    out: list[dict | None] = []
    bad = over = taken = 0
    for item in raw:
        found = shot_of(item)
        if found is None:
            bad += 1
        elif taken >= SHOTS_MAX:
            over += 1
            found = None
        else:
            taken += 1
        out.append(found)
    if bad:
        dropped.append(counted(bad, 'malformed shot', 'malformed shots'))
    if over:
        dropped.append(f"{counted(over, 'shot', 'shots')} over the {SHOTS_MAX} per pass")
    return out


def lines_of(raw: object) -> list[dict] | None:
    if not isinstance(raw, list) or not all(isinstance(x, dict) and filled(x.get('key')) and isinstance(x.get('text'), str) for x in raw):
        return None
    return [{'key': x['key'], 'text': x['text']} for x in raw]


def post_of(item: object, shots: int) -> tuple[dict | None, int]:
    if not isinstance(item, dict) or not isinstance(item.get('title', ''), str):
        return None, 0
    keys, lines, replies = item.get('keys', []), lines_of(item.get('lines', [])), item.get('replies', [])
    if not isinstance(keys, list) or not all(filled(k) for k in keys) or lines is None or not isinstance(replies, list):
        return None, 0
    kept, outside = [], 0
    for r in replies:
        picked = r.get('shots', []) if isinstance(r, dict) else None
        if not isinstance(r, dict) or not filled(r.get('key')) or not isinstance(r.get('text'), str) or not isinstance(picked, list) or not all(whole(i) for i in picked):
            return None, 0
        inside = [i for i in dict.fromkeys(picked) if 0 <= i < shots]
        outside += len(picked) - len([i for i in picked if 0 <= i < shots])
        kept.append({'key': r['key'], 'text': r['text'], 'shots': inside})
    return {'keys': list(dict.fromkeys(keys)), 'title': item.get('title', ''), 'lines': lines, 'replies': kept}, outside


def posts_of(raw: object, shots: int, dropped: list[str]) -> list[dict]:
    if not isinstance(raw, list):
        raise Malformed('post')
    out, bad, outside = [], 0, 0
    for item in raw:
        found, missed = post_of(item, shots)
        outside += missed
        if found is None:
            bad += 1
        else:
            out.append(found)
    if bad:
        dropped.append(counted(bad, 'malformed post', 'malformed posts'))
    if len(out) > POSTS_MAX:
        dropped.append(f"{counted(len(out) - POSTS_MAX, 'post', 'posts')} over the {POSTS_MAX} per pass")
    if outside:
        dropped.append(f"{counted(outside, 'picture index', 'picture indexes')} outside the shots")
    return out[:POSTS_MAX]


def memos_of(raw: object, storable: Callable[[object], bool], dropped: list[str]) -> dict[str, dict]:
    if not isinstance(raw, dict):
        raise Malformed('memo')
    out: dict[str, dict] = {}
    big = over = 0
    for key, value in raw.items():
        if not filled(key) or len(key) > MEMO_KEY_MAX or not key.isprintable() or not isinstance(value, dict) or not storable(value):
            raise Malformed('memo')
        if len(bson.encode(value)) > MEMO_BYTES:
            big += 1
        elif len(out) >= MEMOS_MAX:
            over += 1
        else:
            out[key] = value
    if big:
        dropped.append(f"{counted(big, 'memo', 'memos')} over {MEMO_BYTES // (1024 * 1024)} MB")
    if over:
        dropped.append(f"{counted(over, 'memo', 'memos')} over the {MEMOS_MAX} per pass")
    return out


def outputs_of(sweep: Sweep, storable: Callable[[object], bool]) -> tuple[list[dict | None], list[dict], dict[str, dict], list[str]]:
    dropped: list[str] = []
    shots = shots_of(sweep.shots, dropped)
    posts = posts_of(sweep.posts, len(shots), dropped)
    memos = memos_of(sweep.memos, storable, dropped)
    return shots, posts, memos, dropped


def quiet(card: dict, state: dict, now: float) -> bool:
    return bool(card.get('status') != 'open' or card.get('muted') or card.get('parent') or card.get('triage') in HANDLED or (card.get('autoSend') or {}).get('holdBy')
                or mute_for(state, card['key'], now))


def posted(card: dict) -> bool:
    at = (card.get('slackPost') or {}).get('at')
    return bool(at and at >= (card.get('firstSeen') or ''))


class Outputs:
    def __init__(self, store, slack, attacher, base_url: str, cap: Callable[[], int], clock: Callable[[], float] = time.time):
        self.store = store
        self.slack = slack
        self.attacher = attacher
        self.base = base_url.rstrip('/')
        self.cap = cap
        self.clock = clock

    def shots(self, plugin: Plugin, check: Check, info: InstanceInfo | None, sweep: Sweep, keymap: dict[str, str]) -> list[dict | None]:
        found = list(sweep.shots)
        if not any(found):
            return [None] * len(found)
        cap = int(self.cap())
        try:
            used = self.store.screenshots_since(dt.datetime.fromtimestamp(clock.midnight(self.clock()), dt.timezone.utc))
        except PyMongoError as e:
            log.warning('the shots of pass %s were not stored: %s', check.id, kind(e))
            return [None] * len(found)
        out: list[dict | None] = []
        over = 0
        for item in found:
            if item is not None and used >= cap:
                over += 1
                item = None
            entry = None if item is None else self._store(plugin, check, info, item, keymap)
            used += entry is not None
            out.append(entry)
        if over:
            log.warning('%s', msg('sweeps.outputs_dropped', check=check.id, what=f"{counted(over, 'shot', 'shots')} over the daily cap of {cap}"))
        return out

    def _store(self, plugin: Plugin, check: Check, info: InstanceInfo | None, item: dict, keymap: dict[str, str]) -> dict | None:
        facts = {k: v for k, v in item['facts'].items() if k != 'blockedHosts'}
        at = facts.pop('at', None) or iso(self.clock())
        key = keymap.get(item['key'], item['key'])
        shot_id = new_id()
        data = item['data']
        try:
            self.store.add_screenshot({'_id': shot_id, 'runId': None, 'keys': [key], 'plugin': plugin.id, 'check': check.id, 'instance': PLUGIN_NO_INSTANCE if info is None else info.name,
                                       **facts, 'at': at, 'size': len(data), 'mime': item['mime'], 'data': Binary(data), 'atDt': dt.datetime.fromisoformat(at)})
        except PyMongoError as e:
            log.warning('a shot of pass %s was not stored: %s', check.id, kind(e))
            return None
        return {'key': key, 'entry': {'id': shot_id, **facts, 'at': at, 'kb': round(len(data) / 1024, 1), 'link': f"{self.base}/api/screenshots/{quote(shot_id, safe='')}"}}

    @staticmethod
    def attach(state: dict, items: list[tuple[Sweep, list[dict | None]]]) -> None:
        by_key: dict[str, list[dict]] = {}
        for sweep, pictures in items:
            found = {c.get('key') for c in sweep.result.found}
            for p in pictures:
                if p is not None and p['key'] in found:
                    by_key.setdefault(p['key'], []).append(p['entry'])
        if not by_key:
            return
        for card in state.get('anomalies') or []:
            got = by_key.get(card.get('key'))
            if got:
                card[SHOTS] = got[:CARD_SHOTS]

    def after(self, plugin: Plugin, check: Check, info: InstanceInfo | None, sweep: Sweep, keymap: dict[str, str], pictures: list[dict | None], now: float,
              owns: Callable[[dict], bool]) -> None:
        for key, value in sweep.memos.items():
            try:
                self.store.save_plugin_memo(plugin.id, None if info is None else info.name, key, value)
            except PyMongoError as e:
                log.warning('a memo of pass %s was not stored: %s', check.id, kind(e))
        if sweep.posts and self.slack.enabled:
            self._posts(plugin, check, sweep.posts, keymap, pictures, now, owns)

    def _posts(self, plugin: Plugin, check: Check, posts: list[dict], keymap: dict[str, str], pictures: list[dict | None], now: float, owns: Callable[[dict], bool]) -> None:
        state = self.store.load()
        cards = {a['key']: a for a in state.get('anomalies') or [] if isinstance(a, dict) and isinstance(a.get('key'), str)}
        room: dict[str, int] = {}
        lost = {'reply': 0, 'thread': 0}

        def open_(key: str) -> bool:
            card = cards.get(key)
            return card is not None and owns(card) and not quiet(card, state, now) and not posted(card) and not card.get('slackDraft')

        for item in posts:
            keys = [k for k in dict.fromkeys(keymap.get(k, k) for k in item['keys']) if open_(k)]
            sent = self._top(plugin, item, keys, cards, keymap, now) if keys else None
            for r in item['replies']:
                self._reply(r, keys if sent else [], sent, cards, state, keymap, pictures, now, owns, room, lost)
        if lost['reply'] or lost['thread']:
            parts = [f"{counted(lost['reply'], 'picture', 'pictures')} over the {REPLY_PICTURES} per reply"] if lost['reply'] else []
            parts += [f"{counted(lost['thread'], 'picture', 'pictures')} over the {THREAD_PICTURES} per thread"] if lost['thread'] else []
            log.warning('%s', msg('sweeps.outputs_dropped', check=check.id, what=', '.join(parts)))

    def render(self, item: dict, keys: list[str], cards: dict[str, dict], keymap: dict[str, str]) -> str:
        out = [esc(cut_words(flat(item['title']), TITLE_MAX))] if flat(item['title']) else []
        rows: list[tuple[str, str]] = []
        for line in item['lines']:
            key = keymap.get(line['key'], line['key'])
            if key in keys and all(k != key for k, _ in rows):
                rows.append((key, f"{BULLET} {esc(cut_words(flat(line['text']), LINE_MAX))}{DOT}<{card_url(self.base, key, cards[key].get('num'))}|{esc(msg('slack.digest.card'))}>"))
        shown = 0
        for _, row in rows:
            rest = len(keys) - shown - 1
            tail = [f"{BULLET} {esc(msg('slack.digest.more', count=rest))}"] if rest > 0 else []
            if len('\n'.join(out + [row] + tail)) > POST_TEXT_MAX:
                break
            out.append(row)
            shown += 1
        if len(keys) > shown:
            out.append(f"{BULLET} {esc(msg('slack.digest.more', count=len(keys) - shown))}")
        return '\n'.join(out)

    def _top(self, plugin: Plugin, item: dict, keys: list[str], cards: dict[str, dict], keymap: dict[str, str], now: float) -> dict | None:
        label = cut_words(flat(item['title']), LABEL_MAX) or plugin.name
        try:
            sent = self.slack.linked(self.slack.post({'text': self.render(item, keys, cards, keymap)}))
        except SlackError as e:
            log.warning('the digest post of %s failed: %s', plugin.id, kind(e))
            self.store.add_action({'type': DIGEST, 'target': f'{DIGEST}:{plugin.id}', 'label': label, 'requestedBy': SERVER_BY, 'plugin': plugin.id}, 'failed', f'not sent: {kind(e)}')
            return None
        mark = {**sent, 'at': iso(now), 'by': SERVER_BY, 'digest': True}
        marked = 0
        for key in keys:
            try:
                self.store.slack_posted(key, mark)
                marked += 1
            except (NotFound, Conflict, PyMongoError) as e:
                log.warning('the digest post of %s went out but %s was not marked posted: %s', plugin.id, key, kind(e))
            cards[key]['slackPost'] = mark
        self.store.add_action({'type': DIGEST, 'target': f"{DIGEST}:{sent.get('ts')}", 'label': label, 'requestedBy': SERVER_BY, 'plugin': plugin.id, 'slack': sent}, 'done',
                              f"posted to {self.slack.channel} \u00b7 {counted(marked, 'card', 'cards')}")
        return sent

    def _reply(self, r: dict, keys: list[str], sent: dict | None, cards: dict[str, dict], state: dict, keymap: dict[str, str], pictures: list[dict | None], now: float,
               owns: Callable[[dict], bool], room: dict[str, int], lost: dict[str, int]) -> None:
        key = keymap.get(r['key'], r['key'])
        card = cards.get(key)
        if card is None or not owns(card) or quiet(card, state, now):
            return
        if sent is not None and key in keys:
            channel, ts = sent.get('channel'), sent.get('ts')
        elif posted(card):
            channel, ts = (card.get('slackPost') or {}).get('channel'), (card.get('slackPost') or {}).get('ts')
        else:
            return
        if not channel or not ts:
            return
        given = [pictures[i]['entry'] for i in r['shots'] if i < len(pictures) and pictures[i] is not None]
        shots = [s for s in (given or card.get(SHOTS) or []) if isinstance(s, dict) and s.get('id')]
        left = max(0, THREAD_PICTURES - room.get(ts, 0))
        lost['reply'] += max(0, len(shots) - REPLY_PICTURES)
        lost['thread'] += max(0, min(len(shots), REPLY_PICTURES) - left)
        shots = shots[:min(REPLY_PICTURES, left)]
        if not shots:
            return
        try:
            self.attacher.post({'channel': channel, 'thread_ts': ts, 'text': esc(cut_words(flat(r['text']), LINE_MAX))}, shots, key)
        except SlackError as e:
            log.warning('a digest reply for %s was not posted: %s', key, kind(e))
            return
        room[ts] = room.get(ts, 0) + len(shots)
        try:
            self.store.slack_thread_updated(key, ts, iso(now))
        except PyMongoError as e:
            log.warning('the digest reply for %s went out but was not recorded: %s', key, kind(e))
