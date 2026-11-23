import logging
import time
from collections.abc import Callable

from skopax import clock, information
from skopax.cardlink import card_url
from skopax.engine import has_owner
from skopax.errkind import kind as error_kind
from skopax.messages import msg
from skopax.slack import BLOCK_LIMIT, SECTION_LIMIT, SlackDown, SlackError, cut, esc, never_sent
from skopax.store import Store
from skopax.tagreport import GONE_CHANNEL, REPEAT_GAP_S, TEXT_LIMIT, age, board_open, link_text, missed, seen_at, slot, valid_at

log = logging.getLogger('skopax.warndigest')

RUN = 'warn-digest'
FEATURE = 'warn-digest'
KEY = 'slack.warn-digest-at'
PERIOD = 300
OFFSET = 75
SHOWN_MAX = 15
TITLE_LIMIT = 60
SVC_LIMIT = 80
SEV = 'warn'
ADDRESSED = ('progress', 'handled')
PLAIN = str.maketrans({'*': '\u2217', '_': '\u02cd', '~': '\u223c'})


def waiting(cards: list[dict]) -> list[dict]:
    def left(a: dict) -> bool:
        return a.get('sev') == SEV and a.get('triage') not in ADDRESSED and not a.get('muted') and not a.get('ticket') and not has_owner(a)

    return [a for a in board_open(information.cards_only(cards)) if left(a)]


def board_link(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/#t=anoms/{SEV}/who:none"


def newest(a: dict, now: float) -> tuple:
    num = a.get('num') if isinstance(a.get('num'), int) else 0
    return (-seen_at(a, now), -num, str(a.get('key') or ''))


def svc_text(svc: str) -> str:
    return esc(cut(' '.join(str(svc).split()), SVC_LIMIT)).translate(PLAIN)


def card_line(a: dict, base_url: str, now: float) -> str:
    title = cut(' '.join(str(a.get('label') or a.get('key') or '?').split()), TITLE_LIMIT)
    name = f"#{a['num']} {title}" if a.get('num') else title
    return msg('warndigest.card', link=card_url(base_url, a['key'], a.get('num')), name=link_text(name), age=age(now - seen_at(a, now)))


def fitted(lines: list[str], limit: int) -> str:
    kept: list[str] = []
    for line in lines:
        if len('\n'.join(kept + [line])) > limit:
            break
        kept.append(line)
    return '\n'.join(kept) if kept else cut(lines[0], limit)


def build(cards: list[dict], base_url: str, now: float) -> dict | None:
    found = sorted(waiting(cards), key=lambda a: newest(a, now))
    if not found:
        return None
    link = board_link(base_url)
    total = len(found)
    head = msg(f'warndigest.head.{"one" if total == 1 else "many"}', n=total, link=link, date=clock.date(now, hm=False))
    per_svc: dict[str, int] = {}
    for a in found:
        per_svc[a.get('svc') or '-'] = per_svc.get(a.get('svc') or '-', 0) + 1
    shown = found[:SHOWN_MAX]
    groups: dict[str, list[dict]] = {}
    for a in shown:
        groups.setdefault(a.get('svc') or '-', []).append(a)
    order = sorted(groups, key=lambda s: (-len(groups[s]), newest(groups[s][0], now), s))
    blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': cut(head + '\n' + msg('warndigest.why'), SECTION_LIMIT)}}]
    plain = []
    for svc in order[:BLOCK_LIMIT - 2]:
        body = [msg('warndigest.service', svc=svc_text(svc), n=per_svc[svc])] + [card_line(a, base_url, now) for a in groups[svc]]
        text = fitted(body, SECTION_LIMIT)
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}})
        plain.append(text)
    more = msg('warndigest.more', link=link, n=total - len(shown)) if total > len(shown) else ''
    if more:
        blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': more}]})
    tail = f'\n{more}' if more else ''
    room = max(TEXT_LIMIT - len(head) - len(tail) - 1, 0)
    text = head + '\n' + fitted('\n'.join(plain).split('\n'), room) + tail
    return {'text': text, 'blocks': blocks, 'cards': total}


class WarnDigest:
    def __init__(self, store: Store, slack, at: Callable[[], str], on: Callable[[], bool], base_url: str, clock: Callable[[], float] = time.time):
        self.store = store
        self.slack = slack
        self.at = at
        self.on = on
        self.base_url = base_url
        self.clock = clock
        self.skipped: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.slack.enabled and self.on() and valid_at(self.at()))

    def run_once(self, now: float | None = None) -> dict | None:
        now = self.clock() if now is None else now
        if not self.enabled:
            return None
        when = slot(self.at(), now)
        if when is None:
            self._skip(now)
            return None
        last = self.store.warn_digest_at()
        if last is not None and 0 <= now - last < REPEAT_GAP_S:
            return None
        day = f'{RUN}:{when}'
        if not self.store.claim_run(day):
            return None
        try:
            built = build(self.store.load().get('anomalies') or [], self.base_url, now)
        except Exception:
            self.store.drop_run(day)
            raise
        if built is None:
            log.info('daily warnings digest: no Warning card is waiting, nothing posted')
            return None
        count = built.pop('cards')
        try:
            sent = self.slack.post(built)
        except SlackDown as e:
            if never_sent(e):
                self.store.drop_run(day)
                log.warning('daily warnings digest: the post did not reach Slack, it is tried again on the next pass while the window is open (%s)', error_kind(e))
            else:
                log.warning('daily warnings digest: Slack did not answer the post, so it may have gone out; it is not sent again today (%s)', error_kind(e))
            return None
        except SlackError as e:
            if e.code in GONE_CHANNEL:
                log.warning('daily warnings digest not posted today: Slack refused it (%s)', e.code)
                return None
            self.store.drop_run(day)
            raise
        self.store.note_warn_digest(now)
        self.store.add_activity('slack', 'server', f'Daily warnings digest posted · {count} card{"" if count == 1 else "s"}')
        log.info('daily warnings digest posted for %d cards', count)
        return sent | {'cards': count}

    def _skip(self, now: float) -> None:
        day = missed(self.at(), now)
        if day is None or day == self.skipped:
            return
        self.skipped = day
        last = self.store.warn_digest_at()
        if last is not None and clock.day(last) == day or self.store.has_run(f'{RUN}:{day}'):
            return
        log.info('daily warnings digest skipped today: the window %s to 2 h later has passed, it posts again on the next business day', self.at())
