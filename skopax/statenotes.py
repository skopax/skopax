import logging
import re
import time
from typing import Callable, Collection
from urllib.parse import quote

from skopax import brand, history, information
from skopax.actions import DEFAULT_MUTE_HOURS
from skopax.errkind import kind as error_kind
from skopax.engine import mute_for, window_text
from skopax.jiralinks import site_of
from skopax.actors import POD_BY, SYSTEM, investigator_label, is_token, token_label
from skopax.messages import msg
from skopax.redact import redact
from skopax.slack import Slack, cut_words, esc, reason_of, same_person
from skopax.store import Store, glob_match, iso, utc_now

log = logging.getLogger('skopax.statenotes')

NOTED = {'resolve': 'handled', 'take': 'progress', 'known': 'known', 'mute': 'muted', 'reopen': 'reopened'}
GLOB_KINDS = ('known', 'muted')
TRIAGE_KINDS = ('handled', 'progress')
TICKET_KEY = re.compile(r'\b[A-Z][A-Z0-9]+-\d+\b')
SLACK_LINK = re.compile(r'<[^|>]+\|([^>]+)>')
REASON_LIMIT = 200
RESOLUTION_KINDS = ('fixed', 'handled', 'duplicate')
VIA_KINDS = ('page', 'chat', 'slack', SYSTEM)


def via_label(via) -> str:
    return msg(f'notes.via.{via}') if via in VIA_KINDS else ''


def shown(who: str) -> str:
    return brand.product() if who == SYSTEM else who


def who_of(by: str | None) -> str:
    by = str(by or '').strip()
    if by == SYSTEM:
        return brand.product()
    if by == POD_BY:
        return investigator_label()
    return token_label(by) if is_token(by) else 'Investigator' if not by or by == 'Claude' else by.split('@')[0]


def tickets(text, jira_url: str = '') -> str:
    keys = list(dict.fromkeys(TICKET_KEY.findall(str(text or ''))))
    if keys and not jira_url:
        return ' '.join(keys)
    return ' '.join(f"<{jira_url}/browse/{quote(k, safe='')}|{k}>" for k in keys) or esc(str(text or '').strip())


def resolved_text(who: str, reason: str | None = None, ticket: str | None = None, resolution: dict | None = None, jira_url: str = '') -> str:
    why = (reason or '').strip()
    said = esc(cut_words(redact(why), REASON_LIMIT)) if why else ''
    how = resolution if isinstance(resolution, dict) and resolution.get('kind') in RESOLUTION_KINDS else None
    if how is None:
        parts = [msg('notes.state.resolved', who=esc(who)), tickets(ticket, jira_url) if ticket else '', said]
        return ' · '.join(p for p in parts if p)
    ref = str(how.get('ref') or '').strip()
    head = msg(f"notes.kind.{how['kind']}") + (f': {esc(ref)}' if how['kind'] == 'duplicate' and ref else '')
    by = history.who_text(how['by']) if how.get('by') else who
    parts = [msg('notes.state.resolved_as', head=head), via_label(how.get('via')), esc(by), tickets(ticket, jira_url) if ticket else '', tickets(ref, jira_url) if how['kind'] != 'duplicate' and ref and ref != ticket else '', said]
    return ' · '.join(p for p in parts if p)


def progress_text(who: str, ticket: str | None = None, jira_url: str = '') -> str:
    return msg('notes.state.progress', who=esc(who)) + (f' · {tickets(ticket, jira_url)}' if ticket else '')


def known_text(who: str) -> str:
    return msg('notes.state.known', who=esc(who))


def muted_text(who: str, hours: float) -> str:
    return msg('notes.state.muted', hours=f'{hours:g}', who=esc(who))


def note_span(hours: int, business: bool) -> str:
    return window_text(hours, words='notes.window.business_') if business else window_text(hours)


def auto_text(hours: int, since: str | None = None, business: bool = False) -> str:
    if since == 'parent':
        return msg('notes.state.auto_parent')
    return msg({'first': 'notes.state.auto_first', 'info': 'notes.state.auto_info'}.get(since, 'notes.state.auto'), span=note_span(hours, business))


def auto_event_text(hours: int, business: bool = False) -> str:
    return msg('notes.state.auto_event', span=note_span(hours, business))


def assigned_text(name: str, who: str, mention: str | None = None) -> str:
    return msg('notes.state.assigned', name=mention or esc(name)) + (msg('notes.state.assigned_by', who=esc(who)) if who and who != name else '')


def unassigned_text(who: str) -> str:
    return msg('notes.state.unassigned', who=esc(who))


def closed_text(ticket: str, status: str, jira_url: str = '') -> str:
    return msg('notes.state.closed', ticket=tickets(ticket, jira_url), status=esc(status))


def plain(text: str) -> str:
    return SLACK_LINK.sub(r'\1', text).replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')


def marked(kind: str, card: dict, mute: dict | None):
    if kind == 'handled':
        return 'handled' if card.get('triage') == 'handled' else None
    if kind == 'progress':
        return ('progress', card.get('ticket') or '') if card.get('triage') == 'progress' else None
    if kind == 'known':
        return 'known' if mute and mute.get('kind') == 'known' else None
    if kind == 'reopened':
        return 'closed' if was_closed(card, mute) else 'open'
    return 'muted' if mute and mute.get('kind') != 'known' else None


def was_closed(card: dict, mute: dict | None) -> bool:
    return card.get('triage') == 'handled' or bool(card.get('autoResolvedAt')) or bool(card.get('expired')) or bool(mute and mute.get('kind') == 'known')


def reopened_now(card: dict, mute: dict | None) -> bool:
    return card.get('triage') != 'handled' and not (mute and mute.get('kind') == 'known')


def card_name(card: dict) -> str:
    return f"{card.get('svc') or '-'} / {card.get('label') or '?'}"


def shared_thread(card: dict, cards: list[dict]) -> bool:
    post = card.get('slackPost') or {}
    return any(c['key'] != card['key'] and (c.get('slackPost') or {}).get('ts') == post.get('ts') and (c.get('slackPost') or {}).get('channel') == post.get('channel') for c in cards)


def posted(card: dict) -> bool:
    post = card.get('slackPost') or {}
    return bool(post.get('channel') and post.get('ts'))


def merged(card: dict, by_key: dict) -> bool:
    host = by_key.get(card.get('parent'))
    return bool(host and host is not card and information.is_card(host) and (host.get('status') == 'open' or card.get('status') == 'cleared'))


class StateNotes:
    def __init__(self, store: Store, slack: Slack, flag: bool = True, clock: Callable[[], float] = time.time, jira_site: str | Callable[[], str] = '',
                 mention: Callable[[str], str | None] = lambda email: None, name_of: Callable[[str], str] = lambda email: '',
                 by_name: Callable[[str], str | None] = lambda name: None):
        self.store = store
        self.mention = mention
        self.by_name = by_name
        self.name_of = name_of
        self.jira_site = jira_site
        self.slack = slack
        self.flag = flag
        self.clock = clock

    @property
    def jira_url(self) -> str:
        return site_of(self.jira_site)

    @property
    def enabled(self) -> bool:
        return self.flag and self.slack.enabled

    def watch(self, action: dict) -> dict | None:
        kind = NOTED.get(action.get('type'))
        if not kind or not self.enabled:
            return None
        try:
            return {c['key']: marked(kind, c, m) for c, m in self._cards(action['key'], kind)}
        except Exception as e:
            log.warning('status notes skipped for %s, the cards could not be read: %s', action.get('key'), error_kind(e))
            return None

    def applied(self, action: dict, before: dict | None) -> None:
        if before is None:
            return
        kind = NOTED[action['type']]
        who = who_of(action.get('owner') or action.get('requestedBy'))
        try:
            cards = self._cards(action['key'], kind)
        except Exception as e:
            log.warning('status notes skipped for %s, the cards could not be read: %s', action.get('key'), error_kind(e))
            return
        for card, mute in cards:
            now = marked(kind, card, mute)
            if kind == 'reopened':
                if before.get(card['key']) == 'closed' and reopened_now(card, mute):
                    self._send(card, kind, self._text(kind, card, who, action), who)
                continue
            if now is not None and card['key'] in before and before[card['key']] != now:
                self._send(card, kind, self._text(kind, card, who, action), who)

    def triaged(self, key: str, before: dict, who: str = SYSTEM) -> None:
        card = self._card(key)
        if card is None:
            return
        for kind in TRIAGE_KINDS:
            now = marked(kind, card, None)
            if now is not None and now != marked(kind, before, None):
                self._send(card, kind, resolved_text(shown(who), resolution=card.get('resolution'), jira_url=self.jira_url) if kind == 'handled' else progress_text(shown(who), card.get('ticket'), self.jira_url), who)

    def auto_resolved(self, keys: list[str], hours: int, since: str | None = None, events: Collection[str] = (), business: bool = False) -> None:
        for key in keys:
            self._note(key, 'handled', auto_event_text(hours, business) if key in events else auto_text(hours, since, business))

    def jira_closed(self, key: str, ticket: str, status: str) -> None:
        self._note(key, 'handled', closed_text(ticket, status, self.jira_url))

    def assigned(self, key: str, name: str | None, by: str, email: str | None = None, sure: bool = True, named=None) -> bool:
        who = who_of(by)
        card = self._card(key)
        if card is None:
            return False
        if not name:
            self._send(card, 'assigned', unassigned_text(who), who)
            return True
        shown_name = who_of(name)
        flat = assigned_text(shown_name, who)
        self._send(card, 'assigned', assigned_text(shown_name, who, self._mention(name, email, sure, named)), who, flat)
        return True

    def _mention(self, name: str, email: str | None, sure: bool, named=None) -> str | None:
        if not email:
            try:
                if '@' in name:
                    return None
                if named is None:
                    return self.by_name(name)
                resolved = named()
                return self.mention(resolved) if resolved else None
            except Exception as e:
                log.warning('the assignee mention by name was skipped: %s', error_kind(e))
                return None
        try:
            found = self.mention(email)
            if found and not sure and not same_person(name, self.name_of(email)):
                return None
            if not found and not sure and '@' not in name:
                if named is None:
                    return self.by_name(name)
                resolved = named()
                return self.mention(resolved) if resolved else None
            return found
        except Exception as e:
            log.warning('the assignee mention was skipped: %s', error_kind(e))
            return None

    def jira_linked(self, key: str, ticket: str, owner: str | None) -> None:
        self._note(key, 'progress', progress_text(shown(owner or SYSTEM), ticket, self.jira_url), owner or SYSTEM)

    def _shared(self, card: dict) -> bool:
        try:
            return shared_thread(card, self.store.load().get('anomalies') or [])
        except Exception as e:
            log.warning('status note for %s: the thread could not be checked: %s', card['key'], error_kind(e))
            return False

    def _note(self, key: str, kind: str, text: str, who: str = SYSTEM) -> None:
        card = self._card(key)
        if card is not None:
            self._send(card, kind, text, who)

    def _card(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            cards = self.store.load().get('anomalies') or []
        except Exception as e:
            log.warning('status note skipped for %s, the card could not be read: %s', key, error_kind(e))
            return None
        by_key = {c['key']: c for c in cards}
        card = by_key.get(key)
        return card if card and posted(card) and not merged(card, by_key) else None

    def _cards(self, pattern: str, kind: str) -> list[tuple[dict, dict | None]]:
        state = self.store.load()
        cards = state.get('anomalies') or []
        by_key = {c['key']: c for c in cards}
        now = self.clock()
        hit = [c for c in cards if (glob_match(pattern, c['key']) if kind in GLOB_KINDS else c['key'] == pattern)]
        return [(c, mute_for(state, c['key'], now)) for c in hit if posted(c) and not merged(c, by_key)]

    def _text(self, kind: str, card: dict, who: str, action: dict) -> str:
        if kind == 'handled':
            return resolved_text(who, action.get('reason'), action.get('ticket'), card.get('resolution'), self.jira_url)
        if kind == 'progress':
            return progress_text(who, card.get('ticket'), self.jira_url)
        if kind == 'known':
            return known_text(who)
        if kind == 'reopened':
            return msg('notes.state.reopened', who=esc(who), card=esc(f"#{card['num']}" if card.get('num') else card_name(card)))
        return muted_text(who, action.get('hours') or DEFAULT_MUTE_HOURS)

    def _send(self, card: dict, kind: str, text: str, who: str, flat: str | None = None) -> None:
        key, post = card['key'], card['slackPost']
        flat = text if flat is None else flat
        if self._shared(card):
            head = f'*{esc(card_name(card))}* \u00b7 '
            text, flat = head + text, head + flat
        try:
            sent = self.slack.post({'channel': post['channel'], 'thread_ts': post['ts'], 'text': text})
        except Exception as e:
            log.warning('status note for %s not sent: %s', key, error_kind(e))
            self.store.add_activity('slack', 'server', f'status note not sent \u00b7 {key} \u00b7 {reason_of(e)}', key=key)
            return
        sent = self.slack.linked(sent, post['ts'])
        note = {'state': kind, 'text': plain(flat), 'at': iso(utc_now()), 'by': who, 'ts': sent.get('ts'), **({'permalink': sent['permalink']} if sent.get('permalink') else {})}
        try:
            self.store.slack_state_noted(key, post['ts'], note)
        except Exception as e:
            log.warning('status note for %s went out but was not recorded on the card: %s', key, error_kind(e))
            return
        log.info('status note for %s: %s by %s', key, kind, who)
