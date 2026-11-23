import logging
import time
from collections.abc import Mapping
from typing import Callable

from pymongo.errors import PyMongoError

from skopax.assign import slug
from skopax import brand
from skopax.actors import POD_BY, SYSTEM, investigator_label, is_token, token_label
from skopax.errkind import kind as error_kind
from skopax.mail import tidy
from skopax.messages import msg
from skopax.store import NotFound, Store, glob_match

log = logging.getLogger('skopax.notices')

LABELS = {
    'assigned': 'Assigned to you', 'unassigned': 'No longer assigned to you', 'progress': 'In progress', 'resolved': 'Resolved', 'reopened': 'Reopened',
    'ticket': 'Jira ticket linked', 'severity': 'Severity changed', 'merged': 'Merged into another card', 'ungrouped': 'Taken out of its group',
    'muted': 'Muted', 'known': 'Marked as known noise', 'unmuted': 'Unmuted', 'auto': 'Auto-resolved', 'fired': 'Fired again', 'escalated': 'Got worse',
    'stopped': 'Stopped firing', 'run': 'Investigation finished', 'slack': 'Posted to Slack', 'thread': 'Slack thread update', 'reminder': 'Reminder sent',
    'answer': 'Chat answer ready',
}
BURST_S = 60
FLAP_S = 3600
SAME_S = {k: FLAP_S if k in ('fired', 'escalated', 'stopped') else BURST_S for k in LABELS}
MUTES = {'mute': 'muted', 'known': 'known', 'unmute': 'unmuted'}
GLOB_TYPES = ('mute', 'known', 'unmute')
TRACKED = ('owner', 'assigned', 'triage', 'ticket', 'resolution', 'sev', 'parent', 'status', 'num', 'svc', 'label')
ENGINE_KINDS = {'reopened': 'fired', 'escalated': 'escalated', 'cleared': 'stopped'}
WHO = {'': 'Investigator', 'Claude': 'Investigator', 'investigator': 'Investigator', 'pod': 'Investigator', 'jira-links': 'Jira link sweep'}
PRODUCT_ACTORS = (SYSTEM, 'server', 'auto')
NOTE_LIMIT = 160
TITLE_LIMIT = 120


def person(who) -> str:
    who = str(who or '').strip()
    return slug(who) if '@' in who else ''


def addresses(*cards: dict | None) -> tuple[dict[str, str], set[str]]:
    emails: dict[str, str] = {}
    named: set[str] = set()
    for card in cards:
        if not card:
            continue
        held = card.get('assigned') if isinstance(card.get('assigned'), dict) else {}
        who = str(held.get('email') or card.get('owner') or '').strip()
        if '@' in who:
            emails[slug(who)] = tidy(who)
        elif who:
            named.add(slug(who))
    return emails, named - set(emails)


def came_back_text(ref: str) -> str:
    return msg('mail.notice.came_back', ref=ref) if ref else msg('mail.notice.came_back_plain')


def holder(card: dict | None) -> str:
    if not card:
        return ''
    email = (card.get('assigned') or {}).get('email') if isinstance(card.get('assigned'), dict) else None
    return slug(email or card.get('owner'))


def who_text(by) -> str:
    by = str(by or '').strip()
    if is_token(by):
        return token_label(by)
    if by == POD_BY:
        return investigator_label()
    if by in WHO:
        return WHO[by]
    if by in PRODUCT_ACTORS or by.startswith('auto'):
        return brand.product()
    return by.split('@')[0]


def clip(text, limit: int = NOTE_LIMIT) -> str:
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def title_of(card: dict) -> str:
    return clip(f"{card.get('svc') or '-'} / {card.get('label') or '?'}", TITLE_LIMIT)


def resolved_note(card: dict) -> str:
    how = card.get('resolution') if isinstance(card.get('resolution'), dict) else {}
    ref = str(how.get('ref') or '').strip()
    if how.get('kind') == 'duplicate':
        return f'Duplicate of {ref}' if ref else 'Duplicate'
    if how.get('kind') == 'fixed':
        return 'Fixed' + (f' · {ref}' if ref else '')
    return str(card.get('ticket') or '').strip()


def changes(old: dict, new: dict, by_key: dict | None = None) -> list[tuple[str, str]]:
    out = []
    was, now = old.get('triage'), new.get('triage')
    linked = bool(new.get('ticket')) and new.get('ticket') != old.get('ticket')
    if now != was:
        if now == 'progress':
            out.append(('ticket', f"{new['ticket']} · in progress") if linked else ('progress', str(new.get('ticket') or '')))
        elif now == 'handled':
            out.append(('resolved', resolved_note(new)))
        elif was in ('progress', 'handled') and not now:
            out.append(('reopened', ''))
    elif linked:
        out.append(('ticket', str(new['ticket'])))
    if new.get('sev') and old.get('sev') and new['sev'] != old['sev']:
        out.append(('severity', f"{old['sev']} → {new['sev']}"))
    if new.get('parent') != old.get('parent'):
        if new.get('parent'):
            host = (by_key or {}).get(new['parent']) or {}
            out.append(('merged', f"#{host['num']}" if host.get('num') else str(new['parent'])))
        else:
            out.append(('ungrouped', ''))
    return out


class Notices:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time, mail=None):
        self.store = store
        self.clock = clock
        self.mail = mail

    def add(self, users, kind: str, by=None, card: dict | None = None, note: str = '', key: str | None = None, title: str | None = None, link: str | None = None,
            seen: tuple[dict | None, ...] = (), emails: Mapping[str, str] | None = None, named: tuple[str, ...] = ()) -> int:
        actor = person(by)
        to = sorted({u for u in users if u and u != actor})
        if not to:
            return 0
        row = {'kind': kind, 'key': key if key is not None else (card or {}).get('key'), 'num': (card or {}).get('num'), 'title': title if title is not None else (title_of(card) if card else ''),
               'by': who_text(by), 'note': clip(note), 'text': LABELS[kind] + (f' · {clip(note)}' if note else '') + f' · by {who_text(by)}'}
        if link:
            row['link'] = link
        known, by_name = addresses(card, *seen)
        known |= {u: tidy(str(e).strip()) for u, e in (emails or {}).items() if e and '@' in str(e)}
        by_name = (by_name | set(named)) - set(known)
        fresh: list[dict] = []
        try:
            added = self.store.add_notices([{'user': u, **row, **({'to': known[u]} if u in known else {'named': True} if u in by_name else {})} for u in to], SAME_S, saved=fresh)
        except PyMongoError as e:
            log.warning('%s notification for %s not saved: %s', kind, row['key'], error_kind(e))
            return 0
        if fresh and self.mail is not None:
            self.mail.queue(fresh)
        return added

    def _cards(self) -> dict[str, dict]:
        return {c['key']: c for c in self.store.load().get('anomalies') or []}

    def watch(self, action: dict) -> dict | None:
        try:
            cards = self._cards()
        except Exception as e:
            log.warning('notifications skipped for %s, the cards could not be read: %s', action.get('key'), error_kind(e))
            return None
        pattern = str(action.get('key') or '')
        hits = [c for k, c in cards.items() if k == pattern or (action.get('type') in GLOB_TYPES and glob_match(pattern, k))]
        return {c['key']: {f: c.get(f) for f in TRACKED} | {'key': c['key']} for c in hits}

    def applied(self, action: dict, before: dict | None) -> int:
        if not before:
            return 0
        by = action.get('requestedBy')
        try:
            cards = self._cards()
        except Exception as e:
            log.warning('notifications skipped for %s, the cards could not be read: %s', action.get('key'), error_kind(e))
            return 0
        sent = 0
        for key, old in before.items():
            new = cards.get(key)
            if new is None:
                continue
            found = [(MUTES[action['type']], f"{action.get('hours'):g} h" if action['type'] == 'mute' and action.get('hours') else '')] if action.get('type') in MUTES else changes(old, new, cards)
            for kind, note in found:
                sent += self.add({holder(old), holder(new)}, kind, by, new, note, seen=(old,))
        return sent

    def triaged(self, key: str, before: dict, who: str = 'investigator') -> int:
        try:
            cards = self._cards()
        except Exception as e:
            log.warning('notifications skipped for %s, the card could not be read: %s', key, error_kind(e))
            return 0
        new = cards.get(key)
        if new is None:
            return 0
        return sum(self.add({holder(before), holder(new)}, kind, who, new, note, seen=(before,)) for kind, note in changes(before, new, cards))

    def assigned(self, before: dict, name: str | None, email: str | None, by: str) -> int:
        card = dict(before)
        old, new = holder(before), slug(email or name) if (email or name) else ''
        sent = 0
        if new and new != old:
            sent += self.add({new}, 'assigned', by, card, emails={new: email} if email else None, named=() if email else (new,))
        if old and old != new:
            sent += self.add({old}, 'unassigned', by, card, str(name or ''))
        return sent

    def fired(self, events: list[dict], cards: list[dict]) -> int:
        by_key = {c['key']: c for c in cards}
        sent = 0
        for ev in events:
            kind, card = ENGINE_KINDS.get(ev.get('ev')), by_key.get(ev.get('key'))
            if not kind or card is None or ev.get('muted') or card.get('parent'):
                continue
            resolved_at = (card.get('resolution') or {}).get('at')
            if kind == 'stopped' and resolved_at and resolved_at == ev.get('t'):
                continue
            note = f"{card.get('sev')}" if kind == 'escalated' else came_back_text(ev['cameBack']) if 'cameBack' in ev else ''
            sent += self.add({holder(card)}, kind, 'server', card, note)
        return sent

    @staticmethod
    def reason(why: str) -> str:
        why = str(why or '').strip()
        return why[:1].upper() + why[1:]

    def auto_resolved(self, keys: list[str], why: Mapping[str, str] | None = None) -> int:
        return self._each(keys, 'auto', 'server', {k: self.reason(v) for k, v in (why or {}).items()})

    def followed(self, keys: list[str]) -> int:
        sent = 0
        for key in keys or []:
            card = self._card(key)
            if card is not None:
                sent += self.add({holder(card)}, 'progress', 'server', card, str(card.get('ticket') or ''))
        return sent

    def jira_swept(self, results: list[dict]) -> int:
        sent = 0
        for r in results or []:
            card = self._card(r.get('card'))
            if card is None:
                continue
            if 'closed' not in r:
                sent += self.add({holder(card)}, 'ticket', 'jira-links', card, f"{r.get('issue')} · in progress" if r.get('issue') else '')
            else:
                kind = 'resolved' if card.get('triage') == 'handled' else 'reopened'
                sent += self.add({holder(card)}, kind, 'jira-links', card, f"{r.get('issue')} {r.get('closed')}")
        return sent

    def reminded(self, keys: list[str]) -> int:
        return self._each(keys, 'reminder', 'server')

    def posted(self, key: str, by: str, thread: bool = False, text: str = '') -> int:
        card = self._card(key)
        if card is None:
            return 0
        return self.add({holder(card)}, 'thread' if thread else 'slack', by, card, text)

    def run_done(self, keys: list[str], action: dict | None, outcome: str, recap: str) -> int:
        action = action or {}
        first = (str(recap or '').strip().splitlines() or [''])[0] if outcome == 'done' else outcome
        if action.get('type') == 'ask':
            chat = (action.get('chat') or {}).get('id')
            if action.get('source') != 'chat' or not chat:
                return 0
            return self.add({person(action.get('requestedBy'))}, 'answer', 'investigator', None, first, key='', title=clip(action.get('text'), TITLE_LIMIT), link=f'#t=chat/thread/{chat}',
                            emails={person(action.get('requestedBy')): action.get('requestedBy')})
        if action.get('type') not in (None, 'dive'):
            return 0
        sent = 0
        for key in keys:
            card = self._card(key)
            if card is not None:
                sent += self.add({holder(card), person(action.get('requestedBy'))}, 'run', 'investigator', card, first, emails={person(action.get('requestedBy')): action.get('requestedBy')})
        return sent

    def _card(self, key) -> dict | None:
        if not key:
            return None
        try:
            return self.store.anomaly(key)
        except NotFound:
            return None
        except PyMongoError as e:
            log.warning('notification for %s skipped, the card could not be read: %s', key, error_kind(e))
            return None

    def _each(self, keys: list[str], kind: str, by: str, notes: Mapping[str, str] | None = None) -> int:
        sent = 0
        for key in keys or []:
            card = self._card(key)
            if card is not None:
                sent += self.add({holder(card)}, kind, by, card, (notes or {}).get(key) or '')
        return sent
