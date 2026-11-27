import datetime as dt
import hashlib
import logging
import re
from collections.abc import Iterable

from skopax.errkind import kind as error_kind
from skopax.kit.text import normalised
from skopax.outward import outward
from skopax.redact import redact_text

log = logging.getLogger('skopax.semantic.templates')

SECTIONS = {'lokiTop': 'loki', 'logTop': 'elasticsearch'}
LEVELS = {'ERROR': 'error', 'ERR': 'error', 'FATAL': 'error', 'CRITICAL': 'error', 'WARN': 'warn', 'WARNING': 'warn'}
TEMPLATE_LIMIT = 300
READ_LIMIT = 4000
SVC_LIMIT = 100
KEEP_DAYS = 90
HELD_MARK = '[held'
LEVEL_WORD = re.compile(r'\b(?:ERROR|WARN(?:ING)?|FATAL|CRITICAL|Exception|Traceback)\b')


def template_of(text: object) -> str:
    return redact_text(outward(normalised(str(text or ''), READ_LIMIT)), TEMPLATE_LIMIT).strip()


def template_id(plugin: str, instance: str, svc: str, level: str, template: str) -> str:
    return hashlib.sha256('\x1f'.join((plugin, instance, svc, level, template)).encode('utf-8')).hexdigest()


def card_key_of(cards: Iterable[dict], svc: str, level: str, text: str) -> str | None:
    for card in cards:
        if isinstance(card, dict) and card.get('key') and card.get('svc') == svc and card.get('msg') == text and str(card.get('label') or '').upper().startswith(level.upper()):
            return str(card['key'])
    return None


def documents(plugin: str, instance: str, rows: Iterable[dict], cards: Iterable[dict], now: dt.datetime) -> list[dict]:
    seen: dict[str, dict] = {}
    cards = list(cards)
    stamp = now.isoformat()
    expire = now + dt.timedelta(days=KEEP_DAYS)
    for row in rows:
        if not isinstance(row, dict):
            continue
        level = LEVELS.get(str(row.get('level') or '').strip().upper())
        raw = str(row.get('msg') or '')
        if level is None or raw.startswith(HELD_MARK):
            continue
        template = template_of(raw)
        if not template or template.startswith(HELD_MARK):
            continue
        svc = str(row.get('svc') or row.get('app') or '-').strip()[:SVC_LIMIT] or '-'
        ident = template_id(plugin, instance, svc, level, template)
        peak = row.get('now')
        peak = peak if isinstance(peak, int) and not isinstance(peak, bool) and peak > 0 else 0
        card = card_key_of(cards, svc, str(row.get('level') or level), raw)
        doc = {'_id': ident, 'plugin': plugin, 'instance': instance, 'svc': svc, 'level': level, 'template': template,
               'firstSeen': stamp, 'lastSeen': stamp, 'peak': peak, 'expireAt': expire}
        if card:
            doc['card'] = card
        before = seen.get(ident)
        if before is not None:
            doc['peak'] = max(peak, before['peak'])
            doc['card'] = before.get('card') or doc.get('card')
            if not doc['card']:
                del doc['card']
        seen[ident] = doc
    return list(seen.values())


def collect(store, plugin: str, instance: str, sections: dict, cards: Iterable[dict], now: dt.datetime | None = None) -> int:
    try:
        when = now or dt.datetime.now(dt.timezone.utc)
        rows = sections.get(next((s for s, p in SECTIONS.items() if p == plugin and s in sections), ''), None) or []
        docs = documents(plugin, instance, rows, cards, when)
        return store.save_log_templates(docs) if docs else 0
    except Exception as e:
        log.warning('log templates were not saved: %s', error_kind(e))
        return 0
