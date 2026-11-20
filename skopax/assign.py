import logging
import re

from skopax.cardlink import card_url, chat_url
from skopax.config import Setting
from skopax.errkind import Authored, kind as error_kind
from skopax.messages import fold, msg
from skopax.slack import Slack, SlackError, esc, same_person
from skopax.statenotes import who_of
from skopax.store import Store, iso, utc_now

log = logging.getLogger('skopax.assign')

NAME_LIMIT = 80
WHAT_LIMIT = 200
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
SENT, SAME, NONE, OFF, FAILED = 'sent', 'same', 'none', 'off', 'failed'


class Invalid(Authored, ValueError):
    pass


def slug(person) -> str:
    text = fold(str(person or '').strip()).split('@')[0]
    return '.'.join(p for p in re.split(r'[^a-z0-9]+', text) if p)


def clean(name) -> str:
    return ' '.join(str(name or '').split())


def people_of(users) -> list[dict]:
    out = []
    for u in users if isinstance(users, list) else []:
        if not isinstance(u, dict) or not u.get('active') or u.get('accountType') != 'atlassian' or not clean(u.get('displayName')):
            continue
        out.append({'name': clean(u['displayName']), 'email': str(u.get('emailAddress') or '').strip() or None})
    return out


class Assigner:
    dm = Setting('assign_dm')

    def __init__(self, store: Store, slack: Slack, settings, base: str, notes=None, notices=None, posts=None):
        self.store = store
        self.notes = notes
        self.posts = posts
        self.notices = notices
        self.slack = slack
        self.settings = settings
        self.base = base

    def address(self, name: str, email: str | None, by: str) -> str | None:
        if email:
            return email
        handle, domain = slug(name), by.split('@')[1] if '@' in by else ''
        return f'{handle}@{domain}' if handle and domain else None

    def tell(self, card: dict, name: str, email: str | None, by: str) -> str:
        if not self.dm or not self.slack.enabled:
            return OFF
        to = self.address(name, email, by)
        mine = slug(name) == slug(by) or bool(to and to.lower() == by.lower())
        try:
            user = self.slack.user_by_email(to) if to else None
            if user is None or not (email or mine or same_person(name, user['name'])):
                return NONE
            title = (f"#{card['num']} · " if card.get('num') else '') + f"{card.get('svc') or '-'} / {card.get('label') or '?'}"
            what = ' '.join(str(card.get('what') or '').split())
            what = what if len(what) <= WHAT_LIMIT else what[:WHAT_LIMIT - 1] + '…'
            link = card_url(self.base, card['key'], card.get('num'))
            head = msg('assign.dm.mine', title=esc(title)) if mine else msg('assign.dm.other', who=esc(who_of(by)), title=esc(title))
            self.slack.post({'channel': user['id'], 'text': head + (f'\n{esc(what)}' if what else '') + msg('assign.dm.links', link=link, chat=chat_url(self.base, card['key'], card.get('num')))})
        except SlackError as e:
            log.warning('assignee message for %s not sent: %s', card['key'], error_kind(e))
            return FAILED
        return SENT

    def assign(self, key: str, name: str | None, email: str | None, by: str) -> dict:
        name, email = clean(name), str(email or '').strip() or None
        if len(name) > NAME_LIMIT:
            raise Invalid(f'the name is at most {NAME_LIMIT} characters')
        if email and not EMAIL.fullmatch(email):
            raise Invalid('that is not an email address')
        before = self.store.anomaly(key)
        if not name:
            rev, card = self.store.set_tagging(key, {'owner': None, 'assigned': None, 'handAssigned': True})
            self.store.add_activity('assign', by, f'assignee removed · {key}', key=key)
            self.notify(before, None, None, by)
            return {'rev': rev, 'owner': None, 'assigned': None, 'dm': None, 'thread': self.noted(key, None, by) if before.get('owner') else False}
        same = slug(before.get('owner')) == slug(name)
        assigned = {'by': by, 'at': iso(utc_now())} | ({'email': email} if email else {})
        rev, card = self.store.set_tagging(key, {'owner': name, 'assigned': assigned, 'handAssigned': True})
        self.store.add_activity('assign', by, f'assigned to {name} · {key}', key=key)
        if not same:
            self.notify(before, name, email, by)
        log.info('%s assigned by %s', key, by)
        return {'rev': rev, 'owner': name, 'assigned': assigned, 'dm': SAME if same else self.tell(card, name, email, by), 'thread': False if same else self.noted(key, name, by)}

    def announce(self, before: dict, name: str, email: str | None, by: str) -> None:
        self.notify(before, name, email, by)
        try:
            self.tell(self.store.anomaly(before['key']), name, email, by)
        except Exception as e:
            log.warning('assignee message for %s not sent: %s', before.get('key'), error_kind(e))
        try:
            self.noted(before['key'], name, by)
        except Exception as e:
            log.warning('assignee note for %s not sent: %s', before.get('key'), error_kind(e))

    def notify(self, before: dict, name: str | None, email: str | None, by: str) -> None:
        if self.notices is None:
            return
        try:
            self.notices.assigned(before, name, email, by)
        except Exception as e:
            log.warning('assignee notification for %s not saved: %s', before.get('key'), error_kind(e))

    def noted(self, key: str, name: str | None, by: str) -> bool:
        if self.posts is not None:
            self.posts.assigned(key, name, by)
        if self.notes is None:
            return False
        try:
            return self.notes.assigned(key, name, by)
        except Exception as e:
            log.warning('assignee note for %s not sent to its thread: %s', key, error_kind(e))
            return False
