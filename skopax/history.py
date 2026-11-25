import datetime as dt
import re
from collections.abc import Iterable
from typing import Callable

from skopax import brand
from skopax.actors import POD_BY, SYSTEM, investigator_label, is_token, token_label
from skopax.errkind import Refusal
from skopax.ownwords import REASONS as OWN_REASONS
from skopax.redact import redact

KEEP_DAYS = 365
RECURRING_COUNT = 3
RECURRING_DAYS = 7
RECURRING_FIELDS = ('recurring', 'recurringCount', 'recurringDays')
TEXT_LIMIT = 1000
ANSWER_LIMIT = 800
QUESTION_LIMIT = 500
MUTED_LIMIT = 200
LINE_LIMIT = 200
EVENTS_KEEP = 20
ASKS_KEEP = 20
SAID_KEEP = 20
SAID_LIMIT = 300
INSTANCE_TAIL = re.compile(r'@[a-z0-9](?:[-a-z0-9]{0,18}[a-z0-9])?$')
SAID_EVENT = 'slack-said'
RUNS_KEEP = 20
PAST_LIMIT = 10
PAST_DEFAULT = 5
TEXT_QUERY_LIMIT = 100
BACKFILL_DAYS = 30
BACKFILL_GAP_S = 24 * 3600
BACKFILL_ID = 'card-history-backfill'
ASK_EVENT = 'slack-ask'
ADDRESSED = ('progress', 'handled')
CARD_FIELDS = ('key', 'num', 'svc', 'label', 'sub', 'ns', 'src', 'sev', 'unit', 'firstSeen', 'lastSeen', 'clearedAt', 'peak', 'triage', 'ticket', 'owner', 'triagedBy', 'autoResolvedAt', 'parent', 'teamsPosts', 'resolution')
MUTED_PRESET = 'mutedPreset'
STORED_FIELDS = {f: 1 for f in CARD_FIELDS + ('note', 'noteBy', 'recommend', 'recommendBy', 'muted', MUTED_PRESET, 'status', 'slackPost', 'autoSend', 'pushedAt', 'teamsPosts', 'tags', 'tagsOff', 'business', 'tagsAt', 'fixPosts', 'clearNote', 'routeEcho', 'threadSummary', 'threadSummaryDay', 'threadSeenTs', 'threadSeenAt')}
PAST_FIELDS = ('key', 'num', 'svc', 'label', 'sub', 'ns', 'src', 'sev', 'unit', 'firstSeen', 'lastSeen', 'clearedAt', 'peak', 'outcome', 'triage', 'ticket', 'owner', 'triagedBy',
               'muted', 'note', 'noteBy', 'recommend', 'recommendBy', 'runId', 'recap', 'reopens', 'asks', 'said', 'backfill', 'resolution', 'clearNote')
ASK_FIELDS = ('at', 'by', 'question', 'answer', 'thread', 'usd', 'runId')
ROW_FIELDS = {'key': 1, 'num': 1, 'firstSeen': 1, 'lastSeen': 1, 'clearedAt': 1, 'outcome': 1, 'ticket': 1, 'lastAsk': 1, 'resolution': 1, 'triagedBy': 1, 'owner': 1}
CLEARED_FIELDS = ('resolution', 'triagedBy', 'owner', 'ticket', 'autoResolvedAt')
VERDICT_FIELDS = {'note': 1, 'noteBy': 1, 'muted': 1, 'mutedPreset': 1, 'events': 1, 'backfill': 1}
CLOSED_FIELDS = ('key', 'num', 'svc', 'ns', 'label', 'sub', 'sev', 'src', 'firstSeen', 'lastSeen', 'clearedAt', 'outcome', 'ticket', 'owner', 'reopens', 'resolution')
CLOSED_NOTE_LIMIT = 160
CLOSED_PAGE_MAX = 100
CLOSED_DAYS_MAX = 365
CARD_NUMBER = re.compile(r'^#?(\d{1,7})$')
RUN_FIELDS = {'trigger': 1, 'outcome': 1, 'recap': 1, 'startedAt': 1}
OUTCOME_TEXT = {'handled': 'No action needed', 'fixed': 'Fixed', 'duplicate': 'Duplicate', 'auto-resolved': 'auto-resolved', 'progress': 'In progress', 'muted': 'muted',
                'known': 'known noise', 'cleared': 'cleared, nobody acted', 'open': 'still open'}
RESOLVE_KINDS = ('fixed', 'handled', 'duplicate')
LEGACY_RESOLVED = 'Resolved'
RESOLVE_VIAS = ('page', 'chat', 'slack', SYSTEM)
VIA_TEXT = {'page': 'page', 'chat': 'chat', 'slack': 'Slack'}
MACHINE_BY = {'pod': 'investigator', 'auto': 'auto-resolve', 'jira-links': 'Jira link sweep'}
REF_LIMIT = 200
BACKFILL_ACTIONS = ('resolve', 'take', 'known', 'mute', 'ticket', 'dive', 'slack', 'slack-update')
TRIAGE_WRITE = re.compile(r'^(progress|handled)(?: ((?:[A-Z][A-Z0-9]+-\d+ ?)+))?(?: \((.+?)\))?(?: · (fixed|duplicate)(?: (.+))?)?$')
CREATED = re.compile(r'^([A-Z][A-Z0-9]+-\d+) created')
PERSON = 'person'
AUTO = 'auto'
INVESTIGATOR = 'investigator'
API = 'api'
MACHINE = (AUTO, INVESTIGATOR, API)
SWEEP_BY = 'jira-links'
INVESTIGATOR_BY = ('pod', POD_BY, 'Claude')
MACHINE_DECIDERS = INVESTIGATOR_BY + ('auto', SWEEP_BY, SYSTEM, 'server')
MACHINE_TRIAGE = ('pod', 'auto', SWEEP_BY)
MUTE_KINDS = ('known', 'muted')
PERSON_KINDS = RESOLVE_KINDS + ('progress',)
VERDICTS_KEEP = 3
VERDICT_NOTE_LIMIT = 160
PERSON_LINE = re.compile(r'^(?:Resolved|Taken) by [^:\n]+: *(\S.*)$', re.M)
TOKEN_BY = re.compile(r'^token(?::|$)')
OWN_MUTE = re.compile(r'^(?:muted on the page(?: for [\d.]+ h)?|follows \S+: .*)$')
PERSON_QUERY = {'$or': [{'outcome': {'$in': list(MUTE_KINDS)}},
                        {'outcome': {'$in': list(PERSON_KINDS)}, '$or': [
                            {'$and': [{'resolution.by': {'$nin': [None, ''] + list(MACHINE_DECIDERS)}}, {'resolution.by': {'$not': TOKEN_BY}}, {'resolution.via': {'$ne': SYSTEM}}]},
                            {'resolution.by': {'$in': [None, '']}, 'triagedBy': {'$nin': list(MACHINE_TRIAGE)}, 'owner': {'$not': TOKEN_BY}}]}]}
MACHINE_QUERY = {'$nor': [PERSON_QUERY]}
DIGEST_FIELDS = ('key', 'num', 'svc', 'label', 'sev', 'peak', 'unit', 'firstSeen', 'clearedAt', 'outcome', 'outcomeText', 'ticket', 'muted', 'reopens', 'runId')
CAUSE_LIMIT = 160
HISTORY_TAIL = 10
HISTORY_LINE = 160
HISTORY_SCAN = 60
HISTORY_RUNS = 10
HISTORY_HIDDEN = ('access',)
HISTORY_WORDS = {'take': 'Started progress', 'resolve': 'Resolved', 'ticket': 'Ticket created', 'link': 'Ticket linked', 'mute': 'Muted', 'unmute': 'Unmuted', 'known': 'Marked known noise',
                 'reopen': 'Reopened', 'dive': 'Investigation queued', 'group': 'Grouped', 'severity': 'Severity changed', 'ask': 'Asked the investigator', 'keep': 'Kept open'}
HISTORY_TRIED_WORDS = {'take': 'Start progress', 'resolve': 'Resolve', 'ticket': 'Create task', 'link': 'Link ticket', 'mute': 'Mute', 'unmute': 'Unmute', 'known': 'Mark as known noise',
                       'reopen': 'Reopen', 'dive': 'Investigate', 'group': 'Group', 'severity': 'Change severity', 'ask': 'Ask the investigator', 'keep': 'Keep open'}
HISTORY_TRIED = {'failed': ' failed', 'cancelled': ' cancelled'}
HISTORY_SEV = {'crit': 'Critical', 'high': 'High', 'warn': 'Warning', 'info': 'Info'}
HISTORY_ROUTED = {'teams': 'Posted to Teams', 'email': 'Sent by email'}
HISTORY_SYSTEM = ('server', 'auto', 'system')
HISTORY_ROUTE_S = 120
HISTORY_RULED_S = 120
HISTORY_RULED_KIND = {'mute': 'muted', 'known': 'known'}
HISTORY_AT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
HISTORY_SEV_RESULT = re.compile(r'\(([A-Za-z]+)\)$')
HISTORY_SEV_SIGNAL = re.compile(r'the signal says ([A-Za-z]+)')


def doc_id(key: str, first_seen: str) -> str:
    return f'{key}|{first_seen}'


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def stamp(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def cut(text, limit: int) -> str:
    t = redact(str(text or '').strip())
    return t if len(t) <= limit else t[:limit].rstrip() + '…'


def first_line(text) -> str:
    return cut(next((line for line in str(text or '').splitlines() if line.strip()), ''), LINE_LIMIT)


def note_text(note) -> str | None:
    text = note.get('text') if isinstance(note, dict) else note
    return cut(text, TEXT_LIMIT) if text else None


def who_text(by) -> str:
    by = str(by or '').strip()
    if by == POD_BY:
        return investigator_label()
    if by in MACHINE_BY:
        return MACHINE_BY[by]
    return token_label(by) if is_token(by) else 'Investigator' if not by or by == 'Claude' else by.split('@')[0]


def via_text(via) -> str | None:
    return brand.product() if via == SYSTEM else VIA_TEXT.get(via)


def resolution_of(card: dict) -> dict | None:
    r = card.get('resolution')
    return r if isinstance(r, dict) and r.get('kind') in RESOLVE_KINDS else None


def ref_text(resolution: dict) -> str:
    return str(resolution.get('ref') or '').strip()


def kind_text(resolution: dict | None) -> str:
    if not resolution:
        return LEGACY_RESOLVED
    ref = ref_text(resolution)
    if resolution['kind'] == 'duplicate':
        return f'Duplicate of {ref}' if ref else OUTCOME_TEXT['duplicate']
    return OUTCOME_TEXT[resolution['kind']] + (f' ({ref})' if ref else '')


def resolution_text(resolution: dict | None) -> str:
    if not resolution:
        return LEGACY_RESOLVED
    via = via_text(resolution.get('via'))
    return ' · '.join(x for x in (kind_text(resolution), f'via {via}' if via else '', who_text(resolution.get('by')) if resolution.get('by') else '') if x)


def resolve_problem(kind, ref) -> str | None:
    if kind is not None and kind not in RESOLVE_KINDS:
        return 'kind is one of ' + ', '.join(RESOLVE_KINDS)
    if ref is not None and len(str(ref)) > REF_LIMIT:
        return f'ref is at most {REF_LIMIT} characters'
    if kind == 'duplicate' and not str(ref or '').strip():
        return "a duplicate needs ref: the other card's number or key"
    return None


def duplicate_ref(store, key: str, ref: str) -> str:
    number = CARD_NUMBER.match(ref)
    other = store.key_of_number(int(number.group(1))) if number else ref
    if other is None:
        raise Refusal(f'no card #{number.group(1)} to be a duplicate of')
    if other == key:
        raise Refusal('a card cannot be a duplicate of itself; give the other card')
    n = int(number.group(1)) if number else store.card_numbers([other]).get(other)
    return f'#{n}' if n else other


def outcome_text(outcome_id: str | None, resolution=None) -> str:
    if outcome_id in RESOLVE_KINDS:
        r = resolution if isinstance(resolution, dict) and resolution.get('kind') in RESOLVE_KINDS else None
        return resolution_text(r)
    return OUTCOME_TEXT.get(outcome_id, str(outcome_id or '?'))


def outcome(card: dict, ev: str, previous: str | None = None) -> str:
    if ev in ('known', 'muted'):
        return ev
    triage = card.get('triage')
    if triage == 'handled':
        r = resolution_of(card)
        if r:
            return 'auto-resolved' if r.get('by') == 'auto' else r['kind']
        return 'auto-resolved' if card.get('autoResolvedAt') or card.get('triagedBy') == 'auto' else 'handled'
    if triage == 'progress':
        return 'progress'
    if card.get('muted'):
        return previous if previous in ('known', 'muted') else 'muted'
    return 'cleared' if card.get('status') == 'cleared' else 'open'


def snapshot(card: dict) -> dict:
    out = {f: card.get(f) for f in CARD_FIELDS if card.get(f) is not None}
    note = note_text(card.get('note'))
    if note:
        out['note'] = note
    out['noteBy'] = card['noteBy'] if note and card.get('noteBy') in ('pod', PERSON) else None
    if card.get('recommend'):
        out['recommend'] = cut(card['recommend'], TEXT_LIMIT)
    out['recommendBy'] = 'pod' if card.get('recommend') and card.get('recommendBy') == 'pod' else None
    out['muted'] = cut(card['muted'], MUTED_LIMIT) if card.get('muted') else None
    out[MUTED_PRESET] = True if card.get('muted') and card.get(MUTED_PRESET) else None
    said = card.get('clearNote')
    if isinstance(said, dict) and said.get('text'):
        out['clearNote'] = {k: cut(said[k], TEXT_LIMIT) if k == 'text' else said[k] for k in ('text', 'by', 'plugin', 'at') if said.get(k)}
    return out


def latest_run(runs: list[dict]) -> dict:
    own = [r for r in runs if r.get('trigger') != 'slack']
    picked = next((r for r in own if r.get('outcome') == 'done' and r.get('recap')), None) or next(iter(own), None)
    if not picked:
        return {}
    return {'runId': picked['_id']} | ({'recap': first_line(picked['recap'])} if picked.get('recap') else {})


def ask_entry(question, by, answer, thread, at: str, usd, run_id: str) -> dict:
    out = {'at': at, 'by': by or None, 'question': cut(question, QUESTION_LIMIT), 'answer': cut(answer, ANSWER_LIMIT), 'thread': thread or None,
           'usd': round(float(usd or 0), 4), 'runId': run_id}
    return {k: v for k, v in out.items() if v is not None}


def said_entry(text, by, kind: str, agreed: bool, at: str, link: str | None) -> dict:
    out = {'at': at, 'by': by or None, 'text': cut(text, SAID_LIMIT), 'kind': kind, 'agreed': agreed or None, 'link': link or None}
    return {k: v for k, v in out.items() if v is not None}


def last_ask(ask: dict) -> dict:
    return {'at': ask.get('at'), 'by': ask.get('by'), 'line': first_line(ask.get('answer'))}


def exits(items: list[dict], stored: dict[str, dict], gone: list[dict]) -> list[tuple[dict, str]]:
    out = []
    for a in items:
        old = stored.get(a['key'])
        if not old:
            continue
        if old.get('firstSeen') and a.get('firstSeen') and old['firstSeen'] != a['firstSeen']:
            out.append((old, 'reopened'))
        elif a.get('autoResolvedAt') and not old.get('autoResolvedAt'):
            out.append((a, 'auto-resolved'))
        elif a.get('triage') in ADDRESSED and (a.get('triage'), a.get('ticket')) != (old.get('triage'), old.get('ticket')):
            out.append((a, a['triage']))
        elif a.get('muted') and not old.get('muted'):
            out.append((a, 'muted'))
    return out + [(old, 'left') for old in gone]


def closed_row(doc: dict) -> dict:
    row = {f: doc[f] for f in CLOSED_FIELDS if doc.get(f) is not None}
    note = first_line(doc.get('note'))
    if note:
        row['note'] = cut(note, CLOSED_NOTE_LIMIT)
    if doc.get('muted'):
        row['muted'] = cut(doc['muted'], CLOSED_NOTE_LIMIT)
    return row


def past_row(doc: dict, match: str) -> dict:
    row = {f: doc[f] for f in PAST_FIELDS if doc.get(f) is not None}
    if row.get('asks'):
        row['asks'] = [{f: x[f] for f in ASK_FIELDS if x.get(f) is not None} for x in row['asks']]
    if row.get('outcome'):
        row['outcomeText'] = outcome_text(row['outcome'], row.get('resolution'))
    row['decidedBy'] = decided_by(doc)
    return row | {'match': match}


def decided_by(doc: dict) -> str:
    outcome = doc.get('outcome')
    if outcome in MUTE_KINDS:
        return PERSON
    r = doc.get('resolution')
    by = r.get('by') if isinstance(r, dict) else None
    if by:
        if by in INVESTIGATOR_BY:
            return INVESTIGATOR
        if by in MACHINE_DECIDERS or r.get('via') == SYSTEM:
            return AUTO
        if TOKEN_BY.match(str(by)):
            return API
        return PERSON if outcome in PERSON_KINDS else AUTO
    if doc.get('triagedBy') == 'pod':
        return INVESTIGATOR
    if doc.get('triagedBy') in MACHINE_TRIAGE or outcome == 'auto-resolved':
        return AUTO
    if TOKEN_BY.match(str(doc.get('owner') or '')):
        return API
    return PERSON if outcome in PERSON_KINDS else AUTO


def own_mute(text: str) -> bool:
    return text in OWN_REASONS or bool(OWN_MUTE.match(text))


def person_note(doc: dict) -> str | None:
    if doc.get('outcome') in MUTE_KINDS:
        text = first_line(doc.get('muted'))
        if not text or doc.get(MUTED_PRESET) or own_mute(text):
            return None
        return cut(text, VERDICT_NOTE_LIMIT)
    if doc.get('noteBy') == 'pod':
        return None
    text = str(note_text(doc.get('note')) or '')
    said = PERSON_LINE.findall(text)
    if said:
        return cut(said[-1], VERDICT_NOTE_LIMIT)
    if doc.get('noteBy') == PERSON or doc.get('backfill'):
        return cut(first_line(text), VERDICT_NOTE_LIMIT) or None
    return None


def event_at(doc: dict, kind: str) -> str | None:
    names = {kind, 'handled'} if kind in RESOLVE_KINDS else {kind}
    return next((e.get('at') for e in reversed(doc.get('events') or []) if isinstance(e, dict) and e.get('ev') in names), None)


def verdict(doc: dict) -> dict:
    r = doc.get('resolution') if isinstance(doc.get('resolution'), dict) else {}
    kind = doc.get('outcome')
    out = {'kind': kind, 'by': r.get('by') or (doc.get('owner') if kind == 'progress' else None),
           'at': r.get('at') or event_at(doc, kind) or doc.get('clearedAt') or doc.get('lastSeen') or doc.get('firstSeen'),
           'ref': r.get('ref'), 'ticket': doc.get('ticket'), 'note': person_note(doc), 'num': doc.get('num'), 'firstSeen': doc.get('firstSeen')}
    return {k: v for k, v in out.items() if v not in (None, '')}


def ruled_at(doc: dict) -> str:
    r = doc.get('resolution') if isinstance(doc.get('resolution'), dict) else {}
    return r.get('at') or doc.get('clearedAt') or doc.get('lastSeen') or doc.get('firstSeen') or ''


def picked(occurrences: Iterable[dict], keep: int = VERDICTS_KEEP) -> list[dict]:
    out: list[dict] = []
    for o in sorted((o for o in occurrences if decided_by(o) == PERSON), key=ruled_at, reverse=True):
        if out and o.get('outcome') in MUTE_KINDS and o.get('outcome') == out[-1].get('outcome'):
            continue
        out.append(o)
        if len(out) == keep:
            break
    return out


def verdicts(occurrences: Iterable[dict], keep: int = VERDICTS_KEEP) -> list[dict]:
    return [verdict(o) for o in picked(occurrences, keep)]


def past_of(card: dict, occurrences: Iterable[dict]) -> list[dict]:
    return [o for o in occurrences if o.get('firstSeen') != card.get('firstSeen')]


def machine_cause(doc: dict) -> str | None:
    if not doc.get('note') or doc.get('noteBy') != 'pod':
        return None
    return cut(first_line(note_text(doc['note'])), CAUSE_LIMIT) or None


def duration_s(doc: dict) -> int | None:
    end = doc.get('clearedAt') or doc.get('lastSeen')
    if not doc.get('firstSeen') or not end:
        return None
    try:
        return max(0, int(stamp(end) - stamp(doc['firstSeen'])))
    except ValueError:
        return None


def digest(doc: dict, match: str) -> dict:
    row = {f: doc[f] for f in DIGEST_FIELDS if doc.get(f) is not None}
    if row.get('outcome'):
        row['outcomeText'] = outcome_text(row['outcome'], doc.get('resolution'))
    row['decidedBy'] = decided_by(doc)
    took = duration_s(doc)
    if took is not None:
        row['durationS'] = took
    cause = machine_cause(doc)
    if cause:
        row['cause'] = {'text': cause, 'by': INVESTIGATOR}
    if row['decidedBy'] == PERSON:
        row['verdict'] = {k: v for k, v in verdict(doc).items() if k not in ('num', 'firstSeen')}
    asks = [a for a in doc.get('asks') or [] if isinstance(a, dict)]
    if asks:
        row['asks'] = len(asks)
    return row | {'match': match}


def seen_before(card: dict, occurrences: list[dict]) -> dict | None:
    past = sorted((o for o in occurrences if o.get('firstSeen') != card.get('firstSeen')), key=lambda o: o.get('firstSeen') or '', reverse=True)
    asks = sorted((o['lastAsk'] for o in occurrences if (o.get('lastAsk') or {}).get('line')), key=lambda x: x.get('at') or '', reverse=True)
    if not past and not asks:
        return None
    out = {'count': len(past)}
    if past:
        out |= {'lastAt': past[0].get('clearedAt') or past[0].get('lastSeen') or past[0].get('firstSeen'), 'outcome': past[0].get('outcome'), 'decidedBy': decided_by(past[0])}
        ruled = verdicts(past, 1)
        if ruled:
            out['ruled'] = {k: ruled[0][k] for k in ('kind', 'by', 'at') if ruled[0].get(k)}
        if past[0].get('outcome') in RESOLVE_KINDS and isinstance(past[0].get('resolution'), dict):
            out['resolution'] = past[0]['resolution']
        ticket = next((o['ticket'] for o in past if o.get('ticket')), None)
        if ticket:
            out['ticket'] = ticket
    if asks:
        out['slack'] = {'at': asks[0].get('at'), 'line': asks[0].get('line')}
    return out


def recurring(card: dict, occurrences: list[dict], now: float, days: int) -> int:
    since = now - days * 86400
    starts = {o['firstSeen'] for o in occurrences if o.get('firstSeen')} | ({card['firstSeen']} if card.get('firstSeen') else set())
    return sum(1 for f in starts if stamp(f) >= since)


def annotate(cards: list[dict], rows: dict[str, list[dict]], now: float, count: int = RECURRING_COUNT, days: int = RECURRING_DAYS, owned: Callable[[dict], bool] | None = None) -> None:
    for c in cards:
        occurrences = rows.get(c['key']) or []
        seen = seen_before(c, occurrences)
        if seen:
            c['seenBefore'] = seen
        else:
            c.pop('seenBefore', None)
        n = recurring(c, occurrences, now, days)
        if n >= count and not (owned and owned(c)):
            c.update(recurring=True, recurringCount=n, recurringDays=days)
        else:
            for f in RECURRING_FIELDS:
                c.pop(f, None)


def key_parts(key: str, names: Iterable[str] = ()) -> dict:
    tail = INSTANCE_TAIL.search(key)
    parts = (key[:tail.start()] if tail and tail.group(0)[1:] in set(names) else key).split('|')
    where = parts[1] if len(parts) > 1 else ''
    ns, svc = where.split('/', 1) if '/' in where else ('-', where)
    return {'svc': svc or '?', 'ns': ns or '-', 'label': (' '.join(parts[2:]) or parts[0])[:LINE_LIMIT]}


def _events(runs: list[dict], actions: list[dict], since: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in runs:
        if r.get('trigger') == 'assess' or (r.get('startedAt') or '') < since:
            continue
        for k in r.get('keys') or []:
            out.setdefault(k, []).append({'t': r['startedAt'], 'run': r})
    for a in actions:
        k = a.get('key') or ''
        if a.get('type') not in BACKFILL_ACTIONS or not k or k.startswith('report:') or k == 'ask' or (a.get('requestedAt') or '') < since:
            continue
        out.setdefault(k, []).append({'t': a['requestedAt'], 'action': a})
    return out


def _clusters(events: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    for e in sorted(events, key=lambda e: e['t']):
        if out and stamp(e['t']) - stamp(out[-1][-1]['t']) <= BACKFILL_GAP_S:
            out[-1].append(e)
        else:
            out.append([e])
    return out


def _apply_run(doc: dict, run: dict, key: str) -> None:
    if run.get('trigger') == 'slack' and (run.get('ask') or {}).get('text'):
        ask = run['ask']
        doc.setdefault('asks', []).append(ask_entry(ask.get('text'), ask.get('by'), run.get('recap'), ask.get('thread'), run.get('endedAt') or run['startedAt'], run.get('usd'), run['_id']))
        return
    for w in run.get('writes') or []:
        if w.get('key') != key or w.get('proposed'):
            continue
        summary = str(w.get('summary') or '')
        if w.get('tool') == 'note' and summary:
            doc['note'] = cut(summary, TEXT_LIMIT)
            doc['noteBy'] = 'pod'
        elif w.get('tool') == 'assess' and summary:
            doc['recommend'] = cut(summary.split(' → ', 1)[-1], TEXT_LIMIT)
            doc['recommendBy'] = 'pod'
        elif w.get('tool') == 'triage':
            m = TRIAGE_WRITE.match(summary)
            if m:
                doc.update(outcome=m.group(4) or m.group(1), triage=m.group(1), triagedBy='pod')
                if m.group(2):
                    doc['ticket'] = m.group(2).strip()
                if m.group(3):
                    doc['owner'] = m.group(3)
                if m.group(4):
                    doc['resolution'] = {'kind': m.group(4), 'by': 'pod', 'via': SYSTEM, 'at': run['startedAt']} | ({'ref': m.group(5)} if m.group(5) else {})


def _apply_action(doc: dict, a: dict) -> None:
    if a.get('status') != 'done':
        return
    kind, reason, ticket = a['type'], (a.get('reason') or '').strip(), (a.get('ticket') or '').strip()
    if kind in ('resolve', 'take'):
        doc.update(outcome='handled' if kind == 'resolve' else 'progress', triage='handled' if kind == 'resolve' else 'progress', triagedBy=None)
        if ticket:
            doc['ticket'] = ticket
        if reason:
            doc['note'] = cut(reason, TEXT_LIMIT)
            doc['noteBy'] = None
        if kind == 'take' and a.get('requestedBy'):
            doc['owner'] = a['requestedBy']
    elif kind in ('known', 'mute'):
        doc.update(outcome='known' if kind == 'known' else 'muted', muted=cut(reason or 'muted on the page', MUTED_LIMIT))
    elif kind == 'ticket':
        m = CREATED.match(a.get('result') or '')
        if m:
            doc.update(outcome='progress', triage='progress', ticket=m.group(1), owner=a.get('requestedBy'))


def _occurrence(key: str, cluster: list[dict], index: int, base: dict, now: float) -> dict:
    first, last = cluster[0]['t'], cluster[-1]['t']
    doc = {'_id': doc_id(key, first), 'key': key, **base, 'firstSeen': first, 'lastSeen': last, 'outcome': 'cleared', 'reopens': index, 'backfill': True,
           'events': [], 'at': iso(now), 'atDt': dt.datetime.fromtimestamp(stamp(last), dt.timezone.utc)}
    for e in cluster:
        if 'run' in e:
            _apply_run(doc, e['run'], key)
            doc['events'].append({'ev': 'run', 'at': e['t']})
        else:
            _apply_action(doc, e['action'])
            doc['events'].append({'ev': e['action']['type'], 'at': e['t']})
    runs = sorted((e['run'] for e in cluster if 'run' in e), key=lambda r: r['startedAt'], reverse=True)
    doc['runs'] = [r['_id'] for r in runs][:RUNS_KEEP]
    doc |= latest_run(runs)
    doc['events'] = doc['events'][-EVENTS_KEEP:]
    if doc.get('asks'):
        doc['asks'] = doc['asks'][-ASKS_KEEP:]
        doc['lastAsk'] = last_ask(max(doc['asks'], key=lambda x: x.get('at') or ''))
    return {k: v for k, v in doc.items() if v is not None}


def build_backfill(runs: list[dict], actions: list[dict], state: dict, now: float, names: Iterable[str] = ()) -> list[dict]:
    live = {a['key']: a for a in state.get('anomalies') or [] if a.get('key')}
    names = set(names)
    sev = {}
    for e in state.get('history') or []:
        sev.setdefault(e.get('key'), e.get('sev'))
    out = []
    for key, events in _events(runs, actions, iso(now - BACKFILL_DAYS * 86400)).items():
        card = live.get(key) or {}
        if card.get('firstSeen'):
            events = [e for e in events if e['t'] < card['firstSeen']]
        base = key_parts(key, names) | {f: card[f] for f in ('svc', 'label', 'ns', 'src', 'sev', 'unit', 'sub') if card.get(f) is not None}
        if 'sev' not in base and sev.get(key):
            base['sev'] = sev[key]
        out += [_occurrence(key, c, i, base, now) for i, c in enumerate(_clusters(events))]
    return out


def backfill(store, now: float, names: Iterable[str] = ()) -> int:
    if store.backfill_marked(BACKFILL_ID):
        return 0
    since = iso(now - BACKFILL_DAYS * 86400)
    docs = build_backfill(store.runs_since(since), store.actions_since(since), store.load(), now, names)
    written = store.add_card_history_backfill(docs)
    store.mark_backfill(BACKFILL_ID, written)
    return written


def hist_who(by) -> str:
    s = str(by or '')
    if not s:
        return 'system'
    if s in HISTORY_SYSTEM:
        return 'system'
    return 'Investigator' if s in ('Claude', 'pod') else who_text(s)


def hist_cut(text) -> str:
    t = cut(' '.join(str(text or '').split()), HISTORY_LINE)
    return t


def hist_first(text) -> str:
    return next((x.strip() for x in str(text or '').split('\n') if x.strip()), '')


def hist_label(ident, names) -> str:
    return (names or {}).get(ident) or ident


def hist_plain(r: dict, key: str = '') -> str:
    t = str(r.get('text') or '')
    tail = f' \u00b7 {key}'
    t = t[:-len(tail)] if key and t.endswith(tail) else t
    return t[:1].upper() + t[1:]


def hist_at(x: dict) -> str | None:
    return x.get('doneAt') or x.get('failedAt') or x.get('cancelledAt') or x.get('requestedAt')


def hist_time(s) -> float | None:
    try:
        return stamp(s)
    except (TypeError, ValueError, AttributeError):
        return None


def card_rows(card: dict | None, actions, activity, runs, names: dict | None = None) -> list[dict]:
    c = card or {}
    key = c.get('key') or ''
    rows: list[dict] = []
    acts = sorted((dict(x) for x in actions or []), key=lambda x: x.get('doneAt') or x.get('requestedAt') or '')
    log = list(activity or [])

    def add(at, who, label, data='', **extra) -> None:
        if not at or not (label or data):
            return
        bare = extra.get('bare')
        what = (label + (' ' if bare else ': ') + data) if label and data else label or data
        rows.append({'at': at, 'who': who, 'label': label, 'data': data, 'what': what, 'kind': extra.pop('kind', None), **extra})

    said = [f"{r.get('kind')}:{hist_plain(r, key).lower()}" for r in log]
    assign_logged = any(r.get('kind') == 'assign' for r in log)
    sev_now = None
    for x in acts:
        if x.get('type') not in HISTORY_WORDS or x.get('status') not in ('done', 'failed', 'cancelled'):
            continue
        at, who = hist_at(x), hist_who(x.get('requestedBy'))
        if x['status'] != 'done':
            add(at, who, HISTORY_TRIED_WORDS[x['type']] + HISTORY_TRIED[x['status']], hist_cut(x['result']) if x.get('result') else '', kind='action', src=x)
            continue
        if x['type'] == 'severity':
            result = x.get('result') or ''
            to = HISTORY_SEV.get(x.get('sev')) or (HISTORY_SEV_RESULT.search(result) or [None, None])[1] or x.get('sev') or ''
            was = sev_now or (HISTORY_SEV_SIGNAL.search(result) or [None, None])[1] or ''
            add(at, who, 'Severity changed', '', kind='severity', **{'from': was if was and was != to else '', 'to': to}, src=x)
            sev_now = to
            continue
        detail = hist_cut(x['result']) if x.get('result') else ''
        add(at, who, HISTORY_WORDS[x['type']], detail, kind='action', src=x)

    def route_recorded(r: dict) -> bool:
        at = hist_time(r.get('t'))
        return at is not None and any(isinstance(p, dict) and p.get('route') and f" \u00b7 {p['route']} \u00b7 " in str(r.get('text') or '')
                                      and hist_time(p.get('at')) is not None and abs(hist_time(p['at']) - at) <= HISTORY_ROUTE_S for p in c.get('teamsPosts') or [])

    for r in log:
        if r.get('kind') == 'routes' and route_recorded(r):
            continue
        add(r.get('t'), hist_who(r.get('who')), '', hist_cut(hist_plain(r, key)), kind=r.get('kind'))
    for r in runs or []:
        if not isinstance(r, dict) or not r.get('id'):
            continue
        line = hist_first(r.get('recap'))
        add(r.get('startedAt'), 'Investigator', 'Investigation run', hist_cut(line) if line else r.get('outcome') or '', kind='run')
    for t in c.get('tags') or []:
        label = hist_label(t.get('id'), names)
        if f'tags:tagged {label.lower()}' in said:
            continue
        add(t.get('at'), 'rule' if t.get('by') == 'rule' else hist_who(t.get('by')), 'Tag added', label, kind='tags')
    b = c.get('business')
    if isinstance(b, dict) and b.get('text') and b.get('at') and 'tags:business sentence saved' not in said:
        add(b['at'], 'rule' if b.get('by') == 'rule' else hist_who(b.get('by')), 'Business note written', '', kind='tags')
    a = c.get('assigned')
    if isinstance(a, dict) and a.get('at') and not assign_logged:
        add(a['at'], hist_who(a.get('by')), 'Assigned to', str(c.get('owner') or '').split('@')[0], kind='assign', bare=True)
    sv = c.get('sevSet')
    if isinstance(sv, dict) and sv.get('at') and not any(x.get('type') == 'severity' for x in acts):
        add(sv['at'], hist_who(sv.get('by')), 'Severity changed', '', kind='severity', **{'from': HISTORY_SEV.get(sv.get('auto'), ''), 'to': HISTORY_SEV.get(sv.get('sev')) or sv.get('sev')})
    p = c.get('slackPost')
    if isinstance(p, dict) and p.get('at'):
        add(p['at'], hist_who(p.get('by')), 'Posted to Slack', '', kind='slack')
    for t in c.get('teamsPosts') or []:
        plugin = t.get('plugin') or 'teams'
        add(t.get('at'), hist_who(t.get('by')), HISTORY_ROUTED.get(plugin) or f'Posted by the {plugin} route', hist_label(t.get('route'), names), kind='teams')
    if c.get('clearedAt'):
        add(c['clearedAt'], 'signal', 'Stopped firing', '', kind='signal')
    again = any((x.get('doneAt') or x.get('requestedAt') or '') < (c.get('firstSeen') or '') for x in acts) or bool((c.get('seenBefore') or {}).get('count'))
    if c.get('firstSeen'):
        add(c['firstSeen'], 'signal', 'Fired again' if again else 'Fired', '', kind='signal')
    return sorted(rows, key=lambda r: r['at'], reverse=True)


def ruled_already(x: dict, verdicts: list[dict]) -> bool:
    if x.get('status') != 'done':
        return False
    kind = (x.get('kind') or 'handled') if x.get('type') == 'resolve' else HISTORY_RULED_KIND.get(x.get('type'))
    at = hist_time(hist_at(x))
    return kind is not None and at is not None and any(v.get('kind') == kind and v.get('by') == x.get('requestedBy') and v.get('at') and hist_time(v['at']) is not None
                                                       and abs(hist_time(v['at']) - at) <= HISTORY_RULED_S for v in verdicts)


def row_text(r: dict) -> str:
    if r.get('kind') == 'severity' and r.get('to'):
        return f"{r['what']} from {r['from']} to {r['to']}" if r.get('from') else f"{r['what']} to {r['to']}"
    return r['what']


def tail(card: dict | None, actions, activity, runs, verdicts=(), names: dict | None = None, limit: int = HISTORY_TAIL) -> list[dict]:
    ruled = [v for v in verdicts or [] if isinstance(v, dict)]
    kept = [x for x in actions or [] if x.get('source') != 'chat' and not ruled_already(x, ruled)]
    rows = [r for r in card_rows(card, kept, activity, runs, names) if HISTORY_AT.match(str(r['at']))]
    out = [{'at': r['at'], 'who': r['who'], 'text': row_text(r)} for r in reversed(rows[:limit])]
    return [] if [r['text'] for r in out] == ['Fired'] else out
