import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field

from skopax import history
from skopax.outward import outward
from skopax.store import REFUSED_BY
from skopax.redact import REDACTED, TIMEOUT_MARK, redact_text

CHUNKER_VERSION = 1
WINDOW = 1200
OVERLAP = 150
CAPS = {'run': 3, 'card': 1, 'open': 1, 'change': 1, 'knowledge': 20, 'doc': 20, 'errtpl': 1}
KEYS_KEPT = 4
LINE_LIMIT = 300
HEADER_LIMIT = 200
HEADER_SCAN = 1000
F_LIMIT = 120
F_LIST = 4
CARD_KEY_MIN = 8
HEADING = re.compile(r'(?m)^(?=#{1,6}[ \t])')
PARAGRAPH = re.compile(r'\n{2,}')
SENTENCE = re.compile(r'[^.!?\n]*[.!?]+\s*|[^.!?]+$')


@dataclass(frozen=True)
class Passage:
    kind: str
    ref: str
    part: int
    text: str
    hash: str
    f: dict
    at: dt.datetime | None = None


@dataclass
class Tally:
    skipped: int = 0
    reasons: dict = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped += 1
        self.reasons[why] = self.reasons.get(why, 0) + 1


def fingerprint(model: str, dims: int | None, passage_prefix: str) -> str | None:
    if dims is None or isinstance(dims, bool) or int(dims) <= 0:
        return None
    raw = '\x1f'.join([str(model), str(int(dims)), str(passage_prefix), f'chunker-{CHUNKER_VERSION}'])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def clean(text: object) -> str:
    return '' if text is None or isinstance(text, (dict, list, tuple, set, bool)) else str(text).strip()


def line_of(text: object) -> str:
    first = next((ln.strip() for ln in clean(text).splitlines() if ln.strip()), '')
    return first[:LINE_LIMIT]


def masked(text: str) -> str | None:
    if not text.strip():
        return None
    try:
        shown = redact_text(outward(text))
    except Exception:
        return None
    if TIMEOUT_MARK in shown or not shown.strip() or shown.strip() == REDACTED:
        return None
    return shown


def card_key(key: str) -> bool:
    return '|' in key and len(key) >= CARD_KEY_MIN and not any(c.isspace() for c in key)


def masked_header(header: str, keep: tuple = ()) -> str | None:
    text = header[:HEADER_SCAN]
    keys = sorted({k for k in keep if isinstance(k, str) and card_key(k)}, key=len, reverse=True)
    parts = re.split('(' + '|'.join(re.escape(k) for k in keys) + ')', text) if keys else [text]
    shown: list[str] = []
    for n, part in enumerate(parts):
        if n % 2:
            shown.append(part)
        elif part.strip():
            done = masked(part)
            if done is None:
                return None
            lead = part[:len(part) - len(part.lstrip())]
            trail = part[len(part.rstrip()):]
            shown.append(lead + done.strip() + trail)
        else:
            shown.append(part)
    out = ''.join(shown).strip()[:HEADER_LIMIT].strip()
    return out or None


def moment(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            found = dt.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
        return found if found.tzinfo else found.replace(tzinfo=dt.timezone.utc)
    return None


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def safe_text(value: str) -> str:
    try:
        shown = redact_text(value)
    except Exception:
        return ''
    return '' if TIMEOUT_MARK in shown else shown[:F_LIMIT]


def safe_filters(f: dict) -> dict:
    out: dict = {}
    for key, value in f.items():
        if isinstance(value, str):
            out[key] = safe_text(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [safe_text(v) for v in value if isinstance(v, str)][:F_LIST]
        elif isinstance(value, (bool, int, float)):
            out[key] = value
    return out


def pieces(text: str, window: int) -> list[str]:
    out: list[str] = []
    for section in (s for s in HEADING.split(text) if s):
        if len(section) <= window:
            out.append(section)
            continue
        out.extend(paragraphs(section, window))
    return out


def paragraphs(section: str, window: int) -> list[str]:
    out: list[str] = []
    at = 0
    for m in PARAGRAPH.finditer(section):
        out.append(section[at:m.end()])
        at = m.end()
    if at < len(section):
        out.append(section[at:])
    final: list[str] = []
    for para in out:
        final.extend([para] if len(para) <= window else sentences(para, window))
    return final


def sentences(para: str, window: int) -> list[str]:
    final: list[str] = []
    for s in SENTENCE.findall(para) or [para]:
        if len(s) <= window:
            final.append(s)
        else:
            final.extend(words(s, window))
    return final


def words(sentence: str, window: int) -> list[str]:
    out: list[str] = []
    for w in re.findall(r'\S+\s*', sentence) or [sentence]:
        out.extend([w] if len(w) <= window else [w[i:i + window] for i in range(0, len(w), window)])
    return out


def overlap_tail(chunk: str, size: int) -> str:
    if size <= 0:
        return ''
    if len(chunk) <= size:
        return chunk
    tail = chunk[-size:]
    if chunk[-size - 1].isspace():
        return tail
    cut = re.search(r'\s', tail)
    return tail[cut.end():] if cut else tail


def split(text: str, window: int = WINDOW, overlap: int = OVERLAP) -> list[str]:
    body = text.strip()
    if not body:
        return []
    if len(body) <= window:
        return [body]
    chunks: list[str] = []
    current = ''
    fresh = False
    for atom in pieces(body, window):
        if current and len(current) + len(atom) > window:
            if fresh:
                chunks.append(current.strip())
            tail = overlap_tail(current, overlap)
            room = window - len(atom)
            current = tail[-room:] if room > 0 and len(tail) > room else tail if room > 0 else ''
            fresh = False
        current += atom
        fresh = True
    if fresh and current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


def assemble(kind: str, ref: str, header: str, body: str, f: dict, tally: Tally | None, at: dt.datetime | None = None, keep: tuple = ()) -> list[Passage]:
    tally = tally if tally is not None else Tally()
    head = masked_header(header, keep)
    try:
        laid = outward(body) if body.strip() else ''
        whole = masked(laid)
    except Exception:
        laid, whole = '', None
    if head is None or whole is None:
        tally.skip('masked')
        return []
    filters = safe_filters(f)
    out: list[Passage] = []
    for shown in (masked(c) for c in split(laid)):
        if len(out) >= CAPS[kind]:
            break
        if shown is None:
            tally.skip('masked')
            continue
        text = f'{head}\n{shown}'.strip()
        out.append(Passage(kind, str(ref), len(out), text, text_hash(text), dict(filters), at))
    return out


def joined(*parts: object) -> str:
    return '\n'.join(p for p in (clean(x) for x in parts) if p)


def svc_of(key: object) -> str:
    segment = clean(key).split('|')
    return segment[1].split('/')[-1] if len(segment) > 1 else ''


def ticket_key(ticket: object) -> str:
    return clean(ticket.get('key')) if isinstance(ticket, dict) else clean(ticket)


def run_passages(doc: dict, tally: Tally | None = None) -> list[Passage]:
    if not isinstance(doc, dict) or doc.get('outcome') != 'done' or doc.get('trigger') in (None, 'chat') or doc.get('error') == REFUSED_BY or not doc.get('_id'):
        return []
    keys = [clean(k) for k in (doc.get('keys') or []) if clean(k)][:KEYS_KEPT]
    svc = clean(doc.get('svc')) or (svc_of(keys[0]) if keys else '')
    ask = doc.get('ask')
    ask_text = clean(ask.get('text')) if isinstance(ask, dict) else ''
    body = joined(ask_text, doc.get('recap'))
    header = f"cards: {', '.join(keys)} svc: {svc}"
    f = {'svc': svc, 'keys': keys, 'trigger': clean(doc.get('trigger')), 'started': clean(doc.get('startedAt'))}
    return assemble('run', doc['_id'], header, body, f, tally, moment(doc.get('startedAt')), tuple(keys))


def card_passages(doc: dict, tally: Tally | None = None) -> list[Passage]:
    if not isinstance(doc, dict) or not doc.get('_id'):
        return []
    note = history.note_text(doc.get('note')) or ''
    outcome_text = clean(doc.get('outcomeText')) or (history.outcome_text(doc.get('outcome'), doc.get('resolution')) if doc.get('outcome') else '')
    cause = line_of(note)
    verdict = clean(doc.get('verdictNote')) or clean(doc.get('muted'))
    lines = [f"what: {clean(doc.get('what'))}" if clean(doc.get('what')) else '', f'outcome: {outcome_text}' if outcome_text else '',
             f'cause: {cause}' if cause else '', f'note: {note}' if note and note != cause else '', f'verdict: {verdict}' if verdict else '',
             f"ticket: {ticket_key(doc.get('ticket'))}" if ticket_key(doc.get('ticket')) else '']
    header = joined(f"card {clean(doc.get('key'))}", f"svc: {clean(doc.get('svc'))} label: {clean(doc.get('label'))} sub: {clean(doc.get('sub'))}").replace('\n', ' ')
    f = {'svc': clean(doc.get('svc')), 'label': clean(doc.get('label')), 'sev': clean(doc.get('sev'))}
    return assemble('card', doc['_id'], header, '\n'.join(x for x in lines if x), f, tally, moment(doc.get('at') or doc.get('lastSeen') or doc.get('firstSeen')), (clean(doc.get('key')),))


def open_passages(doc: dict, tally: Tally | None = None) -> list[Passage]:
    if not isinstance(doc, dict) or not (doc.get('_id') or doc.get('key')) or doc.get('status') not in (None, 'open') or doc.get('muted'):
        return []
    note = history.note_text(doc.get('note')) or ''
    body = joined(f"what: {clean(doc.get('what'))}" if clean(doc.get('what')) else '', f'note: {note}' if note else '')
    header = f"open svc: {clean(doc.get('svc'))} label: {clean(doc.get('label'))}"
    f = {'svc': clean(doc.get('svc')), 'label': clean(doc.get('label')), 'sev': clean(doc.get('sev'))}
    return assemble('open', doc.get('key') or doc['_id'], header, body, f, tally, moment(doc.get('at') or doc.get('lastSeen') or doc.get('firstSeen')), (clean(doc.get('key')),))


def deploy_passages(doc: dict, tally: Tally | None = None) -> list[Passage]:
    if not isinstance(doc, dict) or not doc.get('_id'):
        return []
    tickets = doc.get('tickets')
    tickets = [ticket_key(t) for t in tickets] if isinstance(tickets, list) else []
    where = f"{clean(doc.get('ns'))}/{clean(doc.get('svc'))}"
    body = joined(f"image: {clean(doc.get('fromImage'))} -> {clean(doc.get('toImage'))}", f"by: {clean(doc.get('imageBy'))}" if clean(doc.get('imageBy')) else '',
                  f"tickets: {', '.join(t for t in tickets if t)}" if any(tickets) else '')
    f = {'svc': clean(doc.get('svc')), 'ns': clean(doc.get('ns'))}
    return assemble('change', f"deploy:{doc['_id']}", f'deploy {where}', body, f, tally, moment(doc.get('at')))


def report_passages(doc: dict, tally: Tally | None = None) -> list[Passage]:
    if not isinstance(doc, dict) or not doc.get('_id'):
        return []
    brief = doc.get('brief') if isinstance(doc.get('brief'), dict) else {}
    points = brief.get('points') if isinstance(brief.get('points'), list) else []
    body = joined(doc.get('summary'), brief.get('headline'), *[p for p in points if isinstance(p, str)])
    return assemble('change', f"report:{doc['_id']}", 'deploy report', body, {'svc': '', 'ns': ''}, tally, moment(doc.get('at')))


def knowledge_passages(plugin: str, item, instance: str = '', tally: Tally | None = None, seen: object = None) -> list[Passage]:
    ident, title, text = clean(getattr(item, 'id', '')), clean(getattr(item, 'title', '')), clean(getattr(item, 'text', ''))
    if not ident:
        return []
    f = {'plugin': clean(plugin), 'instance': clean(instance), 'slack': bool(getattr(item, 'slack', False))}
    return assemble('knowledge', f'{clean(plugin)}:{ident}', f'{clean(plugin)}: {title}', text, f, tally, moment(seen) or now())


def doc_passages(ref: str, title: str, text: str, plugin: str = '', instance: str = '', space: str = '', tally: Tally | None = None, modified: object = None) -> list[Passage]:
    if not clean(ref):
        return []
    f = {'plugin': clean(plugin), 'instance': clean(instance), 'space': clean(space)}
    return assemble('doc', clean(ref), clean(title), clean(text), f, tally, moment(modified))


def errtpl_passages(ref: str, svc: str, level: str, template: str, plugin: str = '', instance: str = '', tally: Tally | None = None, seen: object = None) -> list[Passage]:
    if not clean(ref):
        return []
    f = {'plugin': clean(plugin), 'instance': clean(instance), 'level': clean(level), 'svc': clean(svc)}
    return assemble('errtpl', clean(ref), f'svc: {clean(svc)} level: {clean(level)}', clean(template), f, tally, moment(seen) or now())
