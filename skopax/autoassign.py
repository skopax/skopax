import logging
import threading
from collections.abc import Callable

from skopax import tags
from skopax.actors import SYSTEM
from skopax.errkind import kind as error_kind
from skopax.information import CARD_SEVS, IN_FLIGHT
from skopax.messages import fold, msg
from skopax.store import NotFound, Store, iso, utc_now

log = logging.getLogger('skopax.autoassign')

OPEN = 'open'


def in_background(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name='skopax-auto-assign', daemon=True).start()


def open_and_free(card: dict) -> bool:
    parent = card.get('parent')
    return (card.get('sev') in CARD_SEVS and (card.get('status') or OPEN) == OPEN and not card.get('owner') and not card.get('assigned') and not card.get('ticket')
            and card.get('triage') not in IN_FLIGHT and not card.get('muted') and (not parent or parent == card.get('key'))
            and not card.get('autoAssigned') and not card.get('handAssigned') and bool(card.get('key')))


class AutoAssign:
    def __init__(self, store: Store, assigner, people: Callable[[], dict[str, str]], labels: Callable[[], dict[str, str]] = tags.all_tags,
                 now: Callable[[], str] = lambda: iso(utc_now()), later: Callable[[Callable[[], None]], None] = in_background):
        self.store = store
        self.assigner = assigner
        self.people = people
        self.labels = labels
        self.now = now
        self.later = later
        self._since: str | None = None

    def since(self) -> str:
        if self._since is None:
            self._since = self.store.auto_assign_since(self.now())
        return self._since

    def ready(self, tag: str, owners: dict[str, list[str]] | None = None, people: dict[str, str] | None = None) -> bool:
        people = self.people() if people is None else people
        return any(e in people for e in (self.store.tag_owners() if owners is None else owners).get(tag, []))

    def card(self, key: str) -> str | None:
        try:
            found = self.store.anomaly(key)
        except NotFound:
            return None
        done = self.sweep([found])
        return done[0] if done else None

    def floors(self, off: set[str], owners: dict[str, list[str]], people: dict[str, str], labels: dict[str, str]) -> dict[str, str]:
        since, held, out = self.since(), self.store.tag_ready(), {}
        for tag in labels:
            if tag in off:
                continue
            at = held.get(tag)
            if not self.ready(tag, owners, people):
                if at is not False:
                    self.store.set_tag_ready(tag, False)
                continue
            if at is False:
                at = self.now()
                self.store.set_tag_ready(tag, at)
            out[tag] = max(since, at) if isinstance(at, str) else since
        return out

    def sweep(self, cards: list[dict]) -> list[str]:
        try:
            off, owners, people, labels = self.store.tag_auto_off(), self.store.tag_owners(), self.people(), self.labels()
            floors = self.floors(off, owners, people, labels)
        except Exception as e:
            log.warning('auto-assign skipped this time: %s', error_kind(e))
            return []
        done = []
        for card in cards:
            try:
                key = self._one(card, floors, owners, people, labels)
            except Exception as e:
                log.warning('card %s was not auto-assigned: %s', card.get('key'), error_kind(e))
                continue
            if key:
                done.append(key)
        return done

    def _tag(self, card: dict, floors: dict[str, str], owners: dict[str, list[str]], people: dict[str, str], labels: dict[str, str]) -> tuple[str, list[str]] | None:
        first = str(card.get('firstSeen') or '')
        found = []
        for t in card.get('tags') or []:
            tag = t.get('id') if isinstance(t, dict) else None
            floor = floors.get(tag)
            if floor is None or not (first >= floor or (tags.by_person(t.get('by')) and str(t.get('at') or '') >= floor)):
                continue
            found.append((fold(labels[tag]), tag, [e for e in owners.get(tag, []) if e in people]))
        return min(found)[1:] if found else None

    def _one(self, card: dict, floors: dict[str, str], owners: dict[str, list[str]], people: dict[str, str], labels: dict[str, str]) -> str | None:
        if not open_and_free(card):
            return None
        chosen = self._tag(card, floors, owners, people, labels)
        if chosen is None:
            return None
        tag, ready = chosen
        key, at = card['key'], self.now()
        if not open_and_free(self.store.anomaly(key)):
            return None
        turn = self.store.peek_auto_turn(tag)
        email = ready[turn % len(ready)]
        name = people[email] or email.split('@')[0]
        if self.store.claim_auto_assign(key, name, {'by': SYSTEM, 'at': at, 'email': email}, {'tag': tag, 'at': at}) is None:
            return None
        self.store.next_auto_turn(tag)
        before = dict(card)
        self.later(lambda: self._announce(before, name, email))
        try:
            self.store.add_activity('assign', SYSTEM, msg('assign.auto', name=name, tag=labels[tag], key=key), key=key)
        except Exception as e:
            log.warning('the auto-assign line of %s was not written: %s', key, error_kind(e))
        log.info('%s auto-assigned by tag %s', key, tag)
        return key

    def _announce(self, before: dict, name: str, email: str) -> None:
        try:
            self.assigner.announce(before, name, email, SYSTEM)
        except Exception as e:
            log.warning('the auto-assign of %s was not announced: %s', before.get('key'), error_kind(e))
