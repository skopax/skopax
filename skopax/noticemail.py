import datetime as dt
import logging
import re
import time
from collections.abc import Iterable
from typing import Callable

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from skopax import brand
from skopax.assign import slug
from skopax.cardlink import card_url
from skopax.errkind import kind as error_kind
from skopax.mail import BUTTON, EDGE, FONT, INK, MUTED, PAGE, PAPER, SUBJECT_MAX, WEB_LINK, MailDown, MailRefused, Mailer, attribution_html, card_mail, esc, flat, footer_lines, site_html
from skopax.messages import msg
from skopax.notices import LABELS
from skopax.store import NotFound, Store, new_id

log = logging.getLogger('skopax.noticemail')

KINDS = tuple(LABELS)
MAIL_KIND = 'notice'
SUMMARY = 'summary'
PER_HOUR = 20
HOUR_S = 3600
GIVE_UP_S = 24 * 3600
LEASE_S = 300
BACKOFF_S = (60, 300, 900, 1800, 3600)
BATCH = 200
KEEP_DAYS = 3
PERIOD = 60
OFFSET = 7
PREFS = '/#t=prefs'
INBOX = '/#t=slack'
WHY_LIMIT = 200
CHOSE_ON = {'$or': [{'mailNotices.on': True}, {'mailNotices.on': {'$ne': False}, 'lastLogin': {'$ne': None}, 'opened': {'$ne': False}}]}


def choice(doc: dict | None) -> dict:
    held = (doc or {}).get('mailNotices')
    held = held if isinstance(held, dict) else {}
    kinds = held.get('kinds')
    signed = (doc or {}).get('lastLogin') is not None and (doc or {}).get('opened') is not False
    return {'on': held.get('on') is True or (held.get('on') is not False and signed), 'kinds': [k for k in KINDS if k in kinds] if isinstance(kinds, list) else list(KINDS)}


def wants(doc: dict | None, kind: str) -> bool:
    if not doc or doc.get('disabled'):
        return False
    held = choice(doc)
    return held['on'] and (kind == SUMMARY or kind in held['kinds'])


def head_of(kind: str) -> str:
    return msg(f'mail.notice.kind.{kind}')


def masked(address: str) -> str:
    local, _, domain = str(address or '').partition('@')
    return f'{local[:1]}***@{domain}' if domain else '***'


def scrub(text: object, address: str) -> str:
    text = str(text or '')
    return re.sub(re.escape(address), masked(address), text, flags=re.IGNORECASE) if address else text


def stamp(at: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(at, dt.timezone.utc)


def why_html(prefs: str) -> str:
    if not prefs:
        return esc(msg('mail.notice.why_nolink'))
    before, _, after = msg('mail.notice.why', link='\0').partition('\0')
    return f'{esc(before)}<a href="{esc(prefs)}" style="color:{MUTED};">{esc(prefs)}</a>{esc(after)}'


def why_text(prefs: str) -> str:
    return msg('mail.notice.why', link=prefs) if prefs else msg('mail.notice.why_nolink')


def with_why(mailed: tuple[str, str, str], prefs: str) -> tuple[str, str, str]:
    subject, text, html = mailed
    footer = msg('mail.footer')
    mark_text, mark_html = f'\n{footer}', esc(footer)
    if '\n-- \n' not in text or mark_text not in text or mark_html not in html:
        return subject, text.rstrip('\n') + '\n' + why_text(prefs) + '\n', html.replace('</body>', f'<p style="color:{MUTED};">{why_html(prefs)}</p></body>', 1)
    head, _, tail = text.rpartition(mark_text)
    start, _, end = html.rpartition(mark_html)
    return subject, f'{head}{mark_text}\n{why_text(prefs)}{tail}', f'{start}{mark_html}<br>{why_html(prefs)}{end}'


def plain_mail(title: str, head: str, lines: list[str], link: str | None, button: str, prefs: str) -> tuple[str, str, str]:
    web = link if link and WEB_LINK.fullmatch(link) else None
    attribution = brand.attribution_line()
    subject = flat(f'[{brand.product()}] {head}' + (f': {title}' if title else ''))[:SUBJECT_MAX]
    parts = [head, ''] + ([title] if title else []) + lines + ([f'{button}: {web}'] if web else []) + [''] + footer_lines([msg('mail.footer'), why_text(prefs)], attribution)
    side = 'padding-left:24px;padding-right:24px;'
    rows = [f'<tr><td style="padding-top:20px;padding-bottom:8px;{side}font-size:14px;color:{MUTED};background-color:{PAPER};">{esc(head)}</td></tr>']
    if title:
        rows.append(f'<tr><td style="padding-bottom:12px;{side}font-size:20px;font-weight:bold;color:{INK};background-color:{PAPER};">{esc(title)}</td></tr>')
    rows += [f'<tr><td style="padding-bottom:8px;{side}font-size:15px;line-height:1.5;color:{INK};background-color:{PAPER};">{esc(line)}</td></tr>' for line in lines]
    if web:
        rows.append(f'<tr><td style="padding-top:16px;padding-bottom:24px;{side}background-color:{PAPER};"><a href="{esc(web)}" style="display:inline-block;background-color:{BUTTON};'
                    f'color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:6px;font-weight:bold;">{esc(button)}</a></td></tr>')
    rows.append(f'<tr><td style="padding-top:12px;padding-bottom:12px;{side}border-top:1px solid {EDGE};font-size:12px;color:{MUTED};background-color:{PAPER};">'
                f'{site_html()}{esc(msg("mail.footer"))}<br>{why_html(prefs)}{attribution_html(attribution)}</td></tr>')
    html = ('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
            f'<title>{esc(subject)}</title></head><body style="margin:0;padding:0;background-color:{PAGE};color:{INK};">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{PAGE};"><tr><td align="center" style="padding:24px 12px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;background-color:{PAPER};color:{INK};border:1px solid {EDGE};'
            f'border-radius:8px;font-family:{FONT};">{"".join(rows)}</table></td></tr></table></body></html>')
    return subject, '\n'.join(parts) + '\n', html


class NoticeMail:
    def __init__(self, store: Store, mailer: Callable[[], Mailer | None], base_url: str | Callable[[], str] = '', clock: Callable[[], float] = time.time, owner: str = ''):
        self.store = store
        self.mailer = mailer
        self.base_url = base_url
        self.clock = clock
        self.owner = owner or new_id()
        self.users = store.db['users']
        self.outbox = store.db['notice_mail']
        self.quota = store.db['notice_mail_quota']
        self.notices = store.db['notifications']

    def ensure_indexes(self) -> None:
        self.outbox.create_index([('state', ASCENDING), ('due', ASCENDING)])
        self.outbox.create_index([('state', ASCENDING), ('window', ASCENDING)])
        self.outbox.create_index('at', expireAfterSeconds=KEEP_DAYS * 86400)
        self.quota.create_index('at', expireAfterSeconds=2 * 86400)

    def base(self) -> str:
        return str((self.base_url() if callable(self.base_url) else self.base_url) or '').rstrip('/')

    def sender(self) -> Mailer | None:
        try:
            found = self.mailer()
        except Exception as e:
            log.warning('the mail server could not be read: %s', error_kind(e))
            return None
        return found if found is not None and found.ready() else None

    def ready(self) -> bool:
        return self.sender() is not None

    def queue(self, rows: Iterable[dict]) -> int:
        try:
            rows = [r for r in rows if r.get('kind') in LABELS and r.get('user')]
            if not rows or not self.ready():
                return 0
            kinds = sorted({r['kind'] for r in rows})
            found = list(self.users.find({'disabled': {'$ne': True}, '$and': [CHOSE_ON, {'$or': [{'mailNotices.kinds': {'$in': kinds}}, {'mailNotices.kinds': {'$not': {'$type': 'array'}}}]}]},
                                         {'email': 1, 'mailNotices': 1, 'disabled': 1, 'lastLogin': 1, 'opened': 1}))
            now = self.clock()
            loose = {r['user'] for r in rows if not r.get('to') and not r.get('named')}
            enabled = [doc['_id'] for doc in self.users.find({'disabled': {'$ne': True}}, {'_id': 1})] if loose else []
            shared = {u for u in loose if sum(slug(e) == u for e in enabled) > 1}
            jobs = [{'_id': new_id(), 'notice': r['_id'], 'user': r['user'], 'email': address, 'kind': r['kind'], 'state': 'queued', 'due': now, 'created': now, 'tries': 0, 'at': stamp(now)}
                    for r in rows for address in self.recipient(r, found, shared)]
            if jobs:
                self.outbox.insert_many(jobs)
            return len(jobs)
        except Exception as e:
            log.warning('notification mails not queued: %s', error_kind(e))
            return 0

    def recipient(self, row: dict, found: list[dict], shared: set[str]) -> list[str]:
        if row.get('named'):
            return []
        if row.get('to'):
            return [doc['_id'] for doc in found if doc['_id'].lower() == row['to'].lower() and wants(doc, row['kind'])]
        same = [doc['_id'] for doc in found if slug(doc['_id']) == row['user'] and wants(doc, row['kind'])]
        if row['user'] in shared or len(same) > 1:
            log.info('a %s notification mail for %s was skipped: more than one account has that name', row['kind'], row['user'])
            return []
        return same

    def claim(self, now: float | None = None) -> dict | None:
        now = self.clock() if now is None else now
        open_ = {'$or': [{'state': 'queued', 'due': {'$lte': now}}, {'state': 'sending', 'claimedAt': {'$lt': now - LEASE_S}}]}
        return self.outbox.find_one_and_update(open_, {'$set': {'state': 'sending', 'claimedAt': now, 'by': self.owner}}, sort=[('due', ASCENDING)], return_document=ReturnDocument.AFTER)

    def run_once(self, now: float | None = None) -> int:
        mailer = self.sender()
        if mailer is None:
            return 0
        done = 0
        refused: list[tuple[dict, MailRefused]] = []
        try:
            self.summarise(self.clock())
            for _ in range(BATCH):
                job = self.claim()
                if job is None:
                    break
                self.renew(refused, job['claimedAt'])
                sent = self.attempt(mailer, job, job['claimedAt'], refused)
                if sent is None:
                    break
                done += sent
        except PyMongoError as e:
            log.warning('notification mails paused, the outbox could not be read: %s', error_kind(e))
        try:
            for job, e in refused:
                self.drop_refused(job, e)
        except PyMongoError as e:
            log.warning('refused notification mails not dropped yet, the outbox could not be written: %s', error_kind(e))
        return done

    def renew(self, refused: list[tuple[dict, MailRefused]], now: float) -> None:
        for job, _ in refused:
            if self.outbox.update_one({'_id': job['_id'], 'claimedAt': job['claimedAt']}, {'$set': {'claimedAt': now}}).modified_count:
                job['claimedAt'] = now

    def drop_refused(self, job: dict, e: MailRefused) -> None:
        why = scrub(e.text, job['email'])[:WHY_LIMIT]
        self.finish(job, 'refused', why=why)
        if job['kind'] != SUMMARY:
            self.notices.update_one({'_id': job['notice'], 'user': job['user']}, {'$set': {'mail': {'state': 'refused', 'why': why}}})
        log.warning('a %s notification mail to %s was refused and dropped: %s', job['kind'], masked(job['email']), why)

    def finish(self, job: dict, state: str, **fields) -> None:
        self.outbox.update_one({'_id': job['_id'], 'claimedAt': job['claimedAt']}, {'$set': {'state': state, **fields}})

    def summarise(self, now: float) -> None:
        window = int(now // HOUR_S)
        groups: dict[tuple, int] = {}
        users: dict[tuple, str] = {}
        for doc in self.outbox.find({'state': 'held', 'window': {'$lt': window}}, {'email': 1, 'window': 1, 'user': 1}):
            at = (doc['email'], doc['window'])
            groups[at] = groups.get(at, 0) + 1
            users[at] = doc.get('user') or ''
        for (address, held), count in groups.items():
            try:
                self.outbox.insert_one({'_id': f'summary|{address}|{held}', 'user': users[(address, held)], 'email': address, 'kind': SUMMARY, 'count': count, 'window': held,
                                        'state': 'queued', 'due': now, 'created': now, 'tries': 0, 'at': stamp(now)})
            except DuplicateKeyError:
                pass
            self.outbox.update_many({'state': 'held', 'email': address, 'window': held}, {'$set': {'state': 'summarised'}})

    def counted(self, address: str, now: float) -> bool:
        window = int(now // HOUR_S)
        doc = self.quota.find_one_and_update({'_id': f'{address}|{window}'}, {'$inc': {'n': 1}, '$setOnInsert': {'at': stamp(now)}}, upsert=True, return_document=ReturnDocument.AFTER)
        if doc['n'] <= PER_HOUR:
            return True
        self.quota.update_one({'_id': f'{address}|{window}'}, {'$inc': {'n': -1}})
        return False

    def attempt(self, mailer: Mailer, job: dict, now: float, refused: list) -> int | None:
        try:
            return self.deliver(mailer, job, now, refused)
        except PyMongoError:
            raise
        except Exception as e:
            self.finish(job, 'skipped')
            self.give_back(job, now)
            log.warning('a %s notification mail to %s was skipped after an error: %s', job['kind'], masked(job['email']), error_kind(e))
            return 0

    def deliver(self, mailer: Mailer, job: dict, now: float, refused: list) -> int | None:
        if now - job['created'] >= GIVE_UP_S:
            self.finish(job, 'expired')
            log.warning('a %s notification mail to %s was dropped after 24 h of tries', job['kind'], masked(job['email']))
            return 0
        person = self.users.find_one({'_id': job['email']})
        if not wants(person, job['kind']):
            self.finish(job, 'skipped')
            return 0
        summary = job['kind'] == SUMMARY
        if not summary:
            if not self.counted(job['email'], now):
                self.finish(job, 'held', window=int(now // HOUR_S))
                return 0
            job['counted'] = True
        subject, text, html = self.compose(job)
        if subject is None:
            self.finish(job, 'skipped')
            self.give_back(job, now)
            return 0
        try:
            mailer.send([job['email']], subject, text, html, kind=MAIL_KIND)
        except MailDown as e:
            self.give_back(job, now)
            self.retry(job, now, e)
            return None
        except MailRefused as e:
            self.give_back(job, now)
            if e.recipient:
                refused.append((job, e))
                return 0
            for other, _ in refused:
                self.retry(other, now, e)
            refused.clear()
            self.retry(job, now, e)
            log.warning('notification mails paused, the mail server refuses them all: %s', scrub(e.text, job['email']))
            return None
        self.finish(job, 'sent', sentAt=now)
        return 1

    def give_back(self, job: dict, now: float) -> None:
        if job.pop('counted', False):
            self.quota.update_one({'_id': f"{job['email']}|{int(now // HOUR_S)}"}, {'$inc': {'n': -1}})

    def retry(self, job: dict, now: float, e: MailDown | MailRefused) -> None:
        tries = int(job.get('tries') or 0) + 1
        due = now + BACKOFF_S[min(tries, len(BACKOFF_S)) - 1]
        if due - job['created'] >= GIVE_UP_S:
            self.finish(job, 'expired', tries=tries)
            log.warning('a %s notification mail to %s was dropped after 24 h of tries: %s', job['kind'], masked(job['email']), scrub(e.text, job['email']))
            return
        self.finish(job, 'queued', tries=tries, due=due)

    def compose(self, job: dict) -> tuple[str | None, str, str]:
        base = self.base()
        prefs = base + PREFS if base else ''
        if job['kind'] == SUMMARY:
            count = int(job.get('count') or 0)
            head = msg('mail.notice.more_one' if count == 1 else 'mail.notice.more', count=count)
            return plain_mail('', head, [msg('mail.notice.more_body')], base + INBOX if base else None, msg('mail.notice.open_inbox'), prefs)
        row = self.notices.find_one({'_id': job['notice']})
        if row is None:
            return None, '', ''
        head = head_of(row['kind'])
        lines = [str(row.get('note') or '')] + ([msg('mail.notice.by', who=row['by'])] if row.get('by') else [])
        lines = [line for line in lines if line.strip()]
        card = self.card(row.get('key'))
        if card is None:
            link = row.get('link') or ''
            web = base + '/' + link.lstrip('/') if base and link.startswith('#') else link
            title = ' '.join(f"{'#' + str(row['num']) + ' ' if row.get('num') else ''}{row.get('title') or ''}".split())
            return plain_mail(title, head, lines, web or None, msg('mail.notice.open'), prefs)
        link = card_url(base, card['key'], card.get('num')) if base else ''
        return with_why(card_mail(card, link, head=head, body_lines=lines), prefs)

    def card(self, key) -> dict | None:
        if not key:
            return None
        try:
            return self.store.anomaly(key)
        except NotFound:
            return None
