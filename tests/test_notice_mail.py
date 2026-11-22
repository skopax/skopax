import email
import json
import logging
import pathlib
import smtplib

import pytest

from tests.conftest import ADMIN_EMAIL, AUTH, anomaly
from tests.mail_kit import CONFIG, FakeSMTP, faked
from tests.test_login import make_client, post_login, with_password
from tests.test_notifications import board
from skopax import mail, messages, noticemail
from skopax.mail import Mailer
from skopax.noticemail import GIVE_UP_S, HOUR_S, LEASE_S, PER_HOUR, NoticeMail
from skopax.notices import LABELS, Notices
from skopax.store import Store
from skopax.users import Users

ALICE = 'alice@example.com'
BOB = 'bob@example.com'
BASE = 'https://skopax.example.com'
CARD = 'log|checkout-service|ERROR|boom'
T0 = 1_800_000_000.0 - (1_800_000_000 % 3600) + 60
PASSWORD = 'correct horse battery'


class Clock:
    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def people(store: Store, **choices) -> Users:
    users = Users(store.db)
    for name, choice in choices.items():
        address = f'{name}@example.com'
        users.create(address, name.title(), 'operator', PASSWORD)
        if choice is not None:
            users.update(address, {'mailNotices': choice})
    return users


def wired(store: Store, clock: Clock, ready: bool = True, base: str = BASE, owner: str = 'pod-a') -> tuple[Notices, NoticeMail]:
    sender = NoticeMail(store, lambda: Mailer(CONFIG) if ready else None, base_url=base, clock=clock, owner=owner)
    return Notices(store, mail=sender), sender


def sent(smtp) -> list[email.message.EmailMessage]:
    return [m for box in smtp.made for m, _, _ in box.sent]


def body(message, subtype: str) -> str:
    return next(p for p in message.walk() if p.get_content_type() == f'text/{subtype}').get_content()


def assign(notices: Notices, key: str = CARD, to: str = ALICE) -> int:
    return notices.assigned({'key': key, 'num': 14, 'svc': 'checkout-service', 'label': 'ERROR lines'}, 'Alice', to, BOB)


def test_off_by_default_and_off_sends_nothing(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice=None, carol={'on': False, 'kinds': list(LABELS)})
    clock = Clock()
    notices, sender = wired(store, clock)
    board(store, anomaly(CARD, num=14))

    # when
    assign(notices)
    assign(notices, to='carol@example.com')
    clock.now += 120
    done = sender.run_once()

    # then
    assert done == 0 and sent(smtp) == []
    assert store.db['notice_mail'].count_documents({}) == 0


def test_an_opted_in_kind_mails_once_after_the_worker_pass_never_in_the_request(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['assigned']})
    clock = Clock()
    notices, sender = wired(store, clock)
    board(store, anomaly(CARD, num=14, svc='checkout-service', label='ERROR lines'))

    # when
    added = assign(notices)
    in_request = len(smtp.made)
    first = sender.run_once()
    again = sender.run_once()

    # then
    assert added == 1 and in_request == 0, 'creating the notice never opens a mail session'
    assert (first, again) == (1, 0)
    [message] = sent(smtp)
    assert message['To'] == ALICE and message['X-Skopax-Kind'] == 'notice'
    assert message['Subject'] == '[Skopax] Assigned to you: #14 checkout-service / ERROR lines'
    text = body(message, 'plain')
    assert 'By bob' in text
    assert f'{BASE}/c/14' in text


def test_a_kind_not_chosen_is_not_mailed(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['reminder']})
    clock = Clock()
    notices, sender = wired(store, clock)
    board(store, anomaly(CARD, num=14))

    # when
    assign(notices)
    done = sender.run_once()

    # then
    assert done == 0 and sent(smtp) == []


def test_the_footer_says_why_and_links_the_preferences_page(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['assigned']})
    notices, sender = wired(store, Clock())
    board(store, anomaly(CARD, num=14))

    # when
    assign(notices)
    sender.run_once()

    # then
    [message] = sent(smtp)
    why = f'You get this because you turned on email notifications. Change it in Preferences: {BASE}/#t=prefs'
    text, html = body(message, 'plain'), body(message, 'html')
    assert text.index('-- \nSent automatically by Skopax.\n' + why + '\n') > text.index(f'{BASE}/c/14'), 'the line sits in the footer, under the card link'
    assert html.index('Sent automatically by Skopax.<br>You get this because you turned on email notifications. Change it in Preferences: '
                      f'<a href="{BASE}/#t=prefs"') > html.index(f'href="{BASE}/c/14"'), 'in HTML too, with a real link'


def test_card_text_is_html_escaped(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['assigned', 'answer']})
    notices, sender = wired(store, Clock())
    evil = '<img src=x onerror=alert(1)>'
    board(store, anomaly(CARD, num=14, label=evil))
    notices.add({'alice'}, 'answer', 'investigator', None, evil, key='', title=evil, link='#t=chat/thread/c1')

    # when
    notices.assigned({'key': CARD, 'num': 14, 'svc': 'checkout-service', 'label': evil}, 'Alice', ALICE, BOB)
    sender.run_once()

    # then
    messages = sent(smtp)
    assert len(messages) == 2
    for message in messages:
        html = body(message, 'html')
        assert '<img' not in html and '&lt;img src=x onerror=alert(1)&gt;' in html
    chat = next(m for m in messages if 'Chat answer ready' in m['Subject'])
    assert f'{BASE}/#t=chat/thread/c1' in body(chat, 'plain')


def test_the_21st_mail_in_an_hour_is_held_and_summarised_at_the_end_of_the_hour(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['assigned']})
    clock = Clock()
    notices, sender = wired(store, clock)
    keys = [f'log|checkout-service|ERROR|n{i}' for i in range(PER_HOUR + 2)]
    board(store, *[anomaly(k, num=100 + i) for i, k in enumerate(keys)])

    # when
    for key in keys:
        assign(notices, key)
    first = sender.run_once()
    within = len(sent(smtp))
    clock.now = T0 - (T0 % HOUR_S) + HOUR_S + 5
    later = sender.run_once()
    again = sender.run_once()

    # then
    assert (first, within) == (PER_HOUR, PER_HOUR)
    assert (later, again) == (1, 0)
    summary = sent(smtp)[-1]
    assert summary['Subject'] == '[Skopax] 2 more notifications'
    assert f'{BASE}/#t=slack' in body(summary, 'plain')
    assert 'Change it in Preferences' in body(summary, 'plain')
    assert store.db['notice_mail'].count_documents({'state': 'held'}) == 0


def test_mail_down_is_retried_with_backoff_and_dropped_after_a_day(store: Store, monkeypatch, caplog):
    # given
    smtp = faked(monkeypatch, send=smtplib.SMTPServerDisconnected('gone'))
    people(store, alice={'on': True, 'kinds': ['assigned', 'reminder']})
    clock = Clock()
    notices, sender = wired(store, clock)
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15, owner=ALICE))

    # when
    assign(notices)
    failed = sender.run_once()
    soon = sender.run_once()
    smtp.fail.clear()
    clock.now += 61
    retried = sender.run_once()

    # then
    assert (failed, soon, retried) == (0, 0, 1)
    assert len(sent(smtp)) == 2, 'one try failed, the retry went out'

    # when
    smtp.fail['send'] = smtplib.SMTPServerDisconnected('gone')
    notices.reminded(['log|payments|ERROR|x'])
    with caplog.at_level(logging.WARNING, logger='skopax.noticemail'):
        for _ in range(40):
            clock.now += HOUR_S
            sender.run_once()

    # then
    assert store.db['notice_mail'].count_documents({'state': {'$in': ['queued', 'sending']}}) == 0
    assert store.db['notice_mail'].count_documents({'state': 'expired'}) == 1
    assert any('dropped' in r.getMessage() for r in caplog.records)
    assert clock.now - T0 > GIVE_UP_S


def test_a_down_mail_server_stops_the_pass_at_the_first_failure(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch, connect=OSError('refused'))
    people(store, alice={'on': True, 'kinds': ['assigned']})
    clock = Clock()
    notices, sender = wired(store, clock)
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15))
    assign(notices)
    assign(notices, 'log|payments|ERROR|x')

    # when
    done = sender.run_once()

    # then
    assert done == 0 and len(smtp.made) == 1, 'one failed connect ends the pass, the next mail waits for the next pass'
    assert store.db['notice_mail'].count_documents({'state': 'queued'}) == 2


def test_the_app_queues_the_mail_of_a_real_action_and_sends_nothing_in_the_request(store: Store, monkeypatch):
    # given
    from tests.test_mcp import call, mcp_client, token
    smtp = faked(monkeypatch)
    monkeypatch.setattr(mail, 'mailer', lambda runtime: Mailer(CONFIG))
    people(store, nina={'on': True, 'kinds': ['progress', 'muted']})
    board(store, anomaly(CARD, num=14, sev='warn', owner='Nina', assigned={'by': BOB, 'at': '2026-11-12T08:00:00Z', 'email': 'nina@example.com'}))

    # when
    with mcp_client(store) as c:
        call(c, token(sub='paul.stone@example.com'), 'take_card', {'card': 14, 'reason': 'looking into it'})

    # then
    assert smtp.made == [], 'nothing is sent inside the request'
    assert [d['kind'] for d in store.db['notice_mail'].find({'email': 'nina@example.com'})] == ['progress']


def test_mail_refused_is_dropped_and_recorded_on_the_persons_own_notice(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch, send=smtplib.SMTPRecipientsRefused({ALICE: (550, b'no such user')}))
    people(store, alice={'on': True, 'kinds': ['assigned']}, dora=None)
    clock = Clock()
    notices, sender = wired(store, clock)
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15))

    # when
    assign(notices)
    assign(notices, 'log|payments|ERROR|x', to='dora@example.com')
    done = sender.run_once()
    clock.now += HOUR_S
    again = sender.run_once()

    # then
    assert (done, again) == (0, 0)
    assert [box.steps[-1] for box in smtp.made] == ['close'], 'the refused send, and no probe'
    [row] = store.notices_of('alice', 10)
    assert row['mail']['state'] == 'refused' and row['mail']['why']
    [hers] = store.notices_of('dora', 10)
    assert 'mail' not in hers, "another person's notice is not marked"
    assert store.db['notice_mail'].count_documents({'state': 'refused'}) == 1


def test_a_refused_sign_in_is_the_servers_problem_so_every_mail_stays_queued(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch, login=smtplib.SMTPAuthenticationError(535, b'bad credentials'))
    people(store, alice={'on': True, 'kinds': ['assigned']})
    clock = Clock()
    notices, sender = wired(store, clock)
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15))
    assign(notices)
    assign(notices, 'log|payments|ERROR|x')

    # when
    done = sender.run_once()

    # then
    assert done == 0 and len(smtp.made) == 1, 'one refused sign-in, then the pass stops'
    assert store.db['notice_mail'].count_documents({'state': 'queued'}) == 2
    assert all('mail' not in row for row in store.notices_of('alice', 10)), 'no notice is marked refused'


def test_two_workers_claim_a_mail_once(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['assigned']})
    clock = Clock()
    notices, a = wired(store, clock, owner='pod-a')
    _, b = wired(store, clock, owner='pod-b')
    board(store, anomaly(CARD, num=14))
    assign(notices)

    # when
    held = a.claim(clock())
    other = b.run_once()
    clock.now += LEASE_S + 1
    taken = b.run_once()
    a.finish(held, 'queued', due=clock())
    late = a.run_once()

    # then
    assert held is not None and other == 0, 'a claimed mail is not sent by another pod'
    assert taken == 1 and len(sent(smtp)) == 1, 'a stale claim of a stopped pod is taken over once'
    assert late == 0 and store.db['notice_mail'].find_one({'_id': held['_id']})['state'] == 'sent', 'the stale pod cannot overwrite the row it lost'


class SlowMailer:
    def __init__(self, clock: Clock, step: float, log: list, then=None):
        self.clock, self.step, self.log, self.then = clock, step, log, then

    def ready(self) -> bool:
        return True

    def probe(self) -> str:
        return 'ok'

    def send(self, to, subject, text, html=None, *, kind=''):
        self.log.append(subject)
        self.clock.now += self.step
        if self.then is not None:
            self.then()
        return '<id@example.com>'


def test_a_long_pass_stamps_each_claim_when_it_is_made_so_no_other_pod_takes_a_mail_in_flight(store: Store, monkeypatch):
    # given
    people(store, alice={'on': True, 'kinds': ['assigned']})
    clock = Clock()
    log: list = []
    stolen: list = []
    b = NoticeMail(store, lambda: None, base_url=BASE, clock=clock, owner='pod-b')

    def other_pod() -> None:
        flying = {d['_id'] for d in store.db['notice_mail'].find({'state': 'sending', 'by': 'pod-a'})}
        taken = []
        while (job := b.claim()) is not None:
            taken.append(job)
        stolen.extend(j['_id'] for j in taken if j['_id'] in flying)
        for job in taken:
            b.finish(job, 'queued')

    a = NoticeMail(store, lambda: SlowMailer(clock, 200, log, then=other_pod), base_url=BASE, clock=clock, owner='pod-a')
    notices = Notices(store, mail=a)
    keys = [f'log|checkout-service|ERROR|n{i}' for i in range(5)]
    board(store, *[anomaly(k, num=100 + i) for i, k in enumerate(keys)])
    for key in keys:
        assign(notices, key)

    # when
    a.run_once()

    # then
    assert clock.now - T0 > LEASE_S, 'the pass ran past one lease'
    assert stolen == [], 'another pod never takes a mail that is being sent'
    assert len(log) == 5 and len(set(log)) == 5, f'each mail went out once: {log}'


def test_a_slug_shared_by_two_accounts_mails_neither_and_an_exact_address_reaches_only_its_account(store: Store, monkeypatch, caplog):
    # given
    smtp = faked(monkeypatch)
    users = people(store, alice={'on': True, 'kinds': ['assigned', 'reminder']})
    users.create('alice@partner.example.com', 'Other Alice', 'operator', PASSWORD)
    users.update('alice@partner.example.com', {'mailNotices': {'on': True, 'kinds': ['assigned', 'reminder']}})
    clock = Clock()
    notices, sender = wired(store, clock)
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15, owner='Alice'))

    # when
    with caplog.at_level(logging.INFO, logger='skopax.noticemail'):
        notices.reminded(['log|payments|ERROR|x'])
        notices.add({'alice'}, 'reminder', 'server', None, '', key='k2', title='t')
        assign(notices)
        sender.run_once()

    # then
    assert [m['To'] for m in sent(smtp)] == [ALICE], 'only the exact address of the assigned person'
    assert any('alice' in r.getMessage() and 'reminder' in r.getMessage() for r in caplog.records), 'the skipped slug-only mail is logged'
    assert not any('@' in r.getMessage() for r in caplog.records)


def test_a_holder_known_only_by_display_name_is_never_mailed(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['reminder']})
    notices, sender = wired(store, Clock())
    board(store, anomaly('log|payments|ERROR|x', num=15, owner='Alice'))

    # when
    notices.reminded(['log|payments|ERROR|x'])
    done = sender.run_once()

    # then
    assert store.notices_of('alice', 10) and done == 0 and sent(smtp) == []


def test_logs_mask_the_recipient(store: Store, monkeypatch, caplog):
    # given
    faked(monkeypatch, send=smtplib.SMTPRecipientsRefused({ALICE: (550, b'no such user')}))
    people(store, alice={'on': True, 'kinds': ['assigned']})
    notices, sender = wired(store, Clock())
    board(store, anomaly(CARD, num=14))
    assign(notices)

    # when
    with caplog.at_level(logging.INFO, logger='skopax.noticemail'):
        sender.run_once()

    # then
    lines = [r.getMessage() for r in caplog.records]
    assert any('a***@example.com' in line for line in lines) and not any(ALICE in line for line in lines)


def test_an_unexpected_error_skips_that_row_and_the_pass_goes_on(store: Store, monkeypatch, caplog):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['assigned']})
    notices, sender = wired(store, Clock())
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15))
    assign(notices)
    assign(notices, 'log|payments|ERROR|x')
    real = sender.compose
    calls = []

    def flaky(job):
        calls.append(job['_id'])
        if len(calls) == 1:
            raise ValueError('broken')
        return real(job)
    monkeypatch.setattr(sender, 'compose', flaky)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.noticemail'):
        done = sender.run_once()

    # then
    assert done == 1 and len(sent(smtp)) == 1
    assert store.db['notice_mail'].count_documents({'state': 'skipped'}) == 1
    window = int(T0 // HOUR_S)
    assert store.db['notice_mail_quota'].find_one({'_id': f'{ALICE}|{window}'})['n'] == 1, 'the skipped row gave its slot back'
    assert any('skipped' in r.getMessage() for r in caplog.records)


def test_a_sender_refused_for_two_people_is_the_servers_problem_and_marks_no_notice(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch, send=smtplib.SMTPSenderRefused(553, b'sender not owned', 'from@example.com'))
    people(store, alice={'on': True, 'kinds': ['assigned']}, dora={'on': True, 'kinds': ['assigned']})
    notices, sender = wired(store, Clock())
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15))
    assign(notices)
    assign(notices, 'log|payments|ERROR|x', to='dora@example.com')

    # when
    done = sender.run_once()

    # then
    assert done == 0 and len(smtp.made) == 1, 'the first server-wide refusal stops the pass'
    assert store.db['notice_mail'].count_documents({'state': 'queued'}) == 2, 'both rows wait for a retry'
    assert all('mail' not in row for who in ('alice', 'dora') for row in store.notices_of(who, 10)), 'neither notice is marked refused'


class NoSuchUser(FakeSMTP):
    refuse: set[str] = set()

    def send_message(self, message, from_addr=None, to_addrs=None):
        [to] = to_addrs
        if to in NoSuchUser.refuse:
            raise smtplib.SMTPRecipientsRefused({to: (550, b'5.1.1 no such user')})
        return super().send_message(message, from_addr, to_addrs)


def test_two_refused_recipients_are_both_dropped_and_marked_and_the_pass_goes_on(store: Store, monkeypatch):
    # given
    faked(monkeypatch)
    monkeypatch.setattr(smtplib, 'SMTP', NoSuchUser)
    NoSuchUser.refuse = {ALICE, 'dora@example.com'}
    people(store, alice={'on': True, 'kinds': ['assigned']}, dora={'on': True, 'kinds': ['assigned']}, nina={'on': True, 'kinds': ['assigned']})
    notices, sender = wired(store, Clock())
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15), anomaly('log|shop|ERROR|y', num=16))
    assign(notices)
    assign(notices, 'log|payments|ERROR|x', to='dora@example.com')
    assign(notices, 'log|shop|ERROR|y', to='nina@example.com')

    # when
    done = sender.run_once()

    # then
    assert done == 1 and [m['To'] for m in sent(FakeSMTP)] == ['nina@example.com']
    assert store.db['notice_mail'].count_documents({'state': 'refused'}) == 2 and store.db['notice_mail'].count_documents({'state': 'queued'}) == 0
    for who in ('alice', 'dora'):
        [row] = store.notices_of(who, 10)
        assert row['mail']['state'] == 'refused' and '@example.com' not in row['mail']['why'].replace('***@example.com', '')
    [hers] = store.notices_of('nina', 10)
    assert 'mail' not in hers


def test_a_refused_sender_after_a_refused_recipient_keeps_both_queued_and_marks_nothing(store: Store, monkeypatch):
    # given
    faked(monkeypatch)
    monkeypatch.setattr(smtplib, 'SMTP', NoSuchUser)
    NoSuchUser.refuse = {ALICE}
    people(store, alice={'on': True, 'kinds': ['assigned']}, dora={'on': True, 'kinds': ['assigned']})
    clock = Clock()
    notices, sender = wired(store, clock)
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15))
    assign(notices)
    clock.now += 1
    assign(notices, 'log|payments|ERROR|x', to='dora@example.com')
    real = NoSuchUser.send_message

    def then_sender_refused(box, message, from_addr=None, to_addrs=None):
        if to_addrs == ['dora@example.com']:
            raise smtplib.SMTPSenderRefused(553, b'5.7.1 sender not owned', 'alerts@example.com')
        return real(box, message, from_addr, to_addrs)
    monkeypatch.setattr(NoSuchUser, 'send_message', then_sender_refused)

    # when
    done = sender.run_once()

    # then
    assert done == 0
    rows = list(store.db['notice_mail'].find({}))
    assert [r['state'] for r in rows] == ['queued', 'queued'] and all(r['tries'] == 1 and r['due'] > clock.now for r in rows), 'both rows wait with a backoff'
    assert all('mail' not in row for who in ('alice', 'dora') for row in store.notices_of(who, 10)), 'neither notice is marked refused'


def test_a_reply_that_echoes_the_address_is_masked_in_logs_and_on_the_notice(store: Store, monkeypatch, caplog):
    # given
    faked(monkeypatch, send=smtplib.SMTPRecipientsRefused({ALICE: (550, b'5.1.1 <ALICE@EXAMPLE.COM>: Recipient address rejected')}))
    people(store, alice={'on': True, 'kinds': ['assigned']})
    notices, sender = wired(store, Clock())
    board(store, anomaly(CARD, num=14))
    assign(notices)

    # when
    with caplog.at_level(logging.INFO, logger='skopax.noticemail'):
        sender.run_once()

    # then
    [row] = store.notices_of('alice', 10)
    lines = [r.getMessage() for r in caplog.records]
    assert row['mail']['state'] == 'refused'
    assert ALICE not in row['mail']['why'].lower() and 'a***@example.com' in row['mail']['why']
    assert lines and not any(ALICE in line.lower() for line in lines)
    assert store.db['notice_mail'].find_one({'state': 'refused'})['why'].lower().count(ALICE) == 0


def test_the_shared_mail_log_masks_an_echoed_address_in_any_case(store: Store, monkeypatch, caplog):
    # given
    faked(monkeypatch, send=smtplib.SMTPRecipientsRefused({ALICE: (550, b'5.1.1 <Alice@Example.COM>: Recipient address rejected')}))
    people(store, alice={'on': True, 'kinds': ['assigned']})
    notices, sender = wired(store, Clock())
    board(store, anomaly(CARD, num=14))
    assign(notices)

    # when
    with caplog.at_level(logging.INFO, logger='skopax.mail'):
        sender.run_once()

    # then
    lines = [r.getMessage() for r in caplog.records if r.name == 'skopax.mail']
    assert lines and any('a***@example.com' in line for line in lines)
    assert not any(ALICE in line.lower() for line in lines)


def test_the_bell_api_never_shows_the_mail_address_fields(store: Store, monkeypatch):
    # given
    faked(monkeypatch)
    users = people(store, nina=None)
    users.set_password('nina@example.com', PASSWORD)
    notices, _ = wired(store, Clock())
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15, owner='Nina'))
    assign(notices, to='nina@example.com')
    notices.reminded(['log|payments|ERROR|x'])
    raw = list(store.db['notifications'].find({'user': 'nina'}))
    client = make_client(store)
    post_login(client, email='nina@example.com')

    # when
    items = client.get('/api/notifications').json()['items']

    # then
    assert {'to', 'named'} <= {k for row in raw for k in row}, 'the rows do carry them'
    assert len(items) == 2 and all('to' not in row and 'named' not in row for row in items)
    assert all('to' not in row and 'named' not in row for row in store.notices_of('nina', 10))


def test_an_owner_address_in_mixed_case_reaches_the_lower_case_account(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['reminder']})
    notices, sender = wired(store, Clock())
    board(store, anomaly('log|payments|ERROR|x', num=15, owner='Alice@Example.com'))

    # when
    notices.reminded(['log|payments|ERROR|x'])
    sender.run_once()

    # then
    assert [m['To'] for m in sent(smtp)] == [ALICE]


def test_a_slug_shared_with_an_account_that_is_not_opted_in_is_still_ambiguous(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    users = people(store, alice={'on': True, 'kinds': ['reminder']})
    users.create('alice@partner.example.com', 'Other Alice', 'operator', PASSWORD)
    notices, sender = wired(store, Clock())

    # when
    notices.add({'alice'}, 'reminder', 'server', None, '', key='k2', title='t')
    sender.run_once()

    # then
    assert sent(smtp) == [], 'two enabled accounts share the name, so neither is mailed'


def test_a_failure_after_the_slot_was_given_back_gives_it_back_once(store: Store, monkeypatch):
    # given
    faked(monkeypatch, send=smtplib.SMTPServerDisconnected('gone'))
    people(store, alice={'on': True, 'kinds': ['assigned']})
    notices, sender = wired(store, Clock())
    board(store, anomaly(CARD, num=14))
    assign(notices)

    def broken(*a, **k):
        raise ValueError('broken')
    monkeypatch.setattr(sender, 'retry', broken)

    # when
    sender.run_once()

    # then
    window = int(T0 // HOUR_S)
    assert store.db['notice_mail_quota'].find_one({'_id': f'{ALICE}|{window}'})['n'] == 0


def test_a_disabled_person_gets_no_mail(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    users = people(store, alice={'on': True, 'kinds': ['assigned']}, dora={'on': True, 'kinds': ['assigned']})
    users.set_disabled('dora@example.com', True)
    clock = Clock()
    notices, sender = wired(store, clock)
    board(store, anomaly(CARD, num=14), anomaly('log|payments|ERROR|x', num=15))

    # when
    assign(notices, to='dora@example.com')
    assign(notices)
    users.set_disabled(ALICE, True)
    done = sender.run_once()

    # then
    assert done == 0 and sent(smtp) == []


def test_no_mail_server_queues_nothing(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['assigned']})
    notices, sender = wired(store, Clock(), ready=False)
    board(store, anomaly(CARD, num=14))

    # when
    added = assign(notices)
    done = sender.run_once()

    # then
    assert (added, done) == (1, 0) and sent(smtp) == []
    assert store.db['notice_mail'].count_documents({}) == 0


def test_the_mail_follows_the_outbound_language(store: Store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    people(store, alice={'on': True, 'kinds': ['assigned']})
    notices, sender = wired(store, Clock())
    board(store, anomaly(CARD, num=14))
    assign(notices)

    pack = json.loads((pathlib.Path(noticemail.__file__).parent / 'locale' / 'de' / 'messages.json').read_text())
    messages.set_catalogs([('de', pack)])

    # when
    try:
        with messages.speaking('de'):
            sender.run_once()
    finally:
        messages.reset()

    # then
    [message] = sent(smtp)
    assert message['Subject'] == '[Skopax] Ihnen zugewiesen: #14 checkout-service / ERROR pattern'
    text = body(message, 'plain')
    assert 'Sie erhalten diese Mail, weil Sie E-Mail-Benachrichtigungen eingeschaltet haben.' in text and 'Von bob' in text


def own(store: Store):
    with_password(store)
    client = make_client(store)
    post_login(client)
    return client


def test_the_api_reads_and_saves_only_your_own_choice(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(mail, 'mailer', lambda runtime: Mailer(CONFIG))
    people(store, bob={'on': True, 'kinds': ['reminder']})
    client = own(store)

    # when
    first = client.get('/api/me/notice-mail').json()
    saved = client.put('/api/me/notice-mail', json={'on': True, 'kinds': ['assigned', 'reminder']})
    after = client.get('/api/me/notice-mail').json()
    other = client.put('/api/me/notice-mail', json={'on': False, 'kinds': [], 'email': BOB})
    bad = client.put('/api/me/notice-mail', json={'on': True, 'kinds': ['nope']})

    # then
    assert first == {'on': False, 'kinds': list(LABELS), 'all': list(LABELS), 'email': ADMIN_EMAIL, 'ready': True}
    assert saved.status_code == 200 and saved.json()['on'] is True
    assert (after['on'], after['kinds']) == (True, ['assigned', 'reminder'])
    assert other.status_code == 422 and bad.status_code == 422
    assert Users(store.db).get(BOB)['mailNotices'] == {'on': True, 'kinds': ['reminder']}, "another person's choice is out of reach"
    assert Users(store.db).get(ADMIN_EMAIL)['mailNotices'] == {'on': True, 'kinds': ['assigned', 'reminder']}


def test_the_api_needs_a_signed_in_person_and_a_mail_server_to_turn_on(store: Store, monkeypatch):
    # given
    people(store, bob={'on': True, 'kinds': ['reminder']})
    signed_out = make_client(store)
    client = own(store)

    # when
    read = signed_out.get('/api/me/notice-mail')
    write = signed_out.put('/api/me/notice-mail', json={'on': True, 'kinds': ['assigned']})
    token = signed_out.get('/api/me/notice-mail', headers=AUTH)
    shown = client.get('/api/me/notice-mail').json()
    refused = client.put('/api/me/notice-mail', json={'on': True, 'kinds': ['assigned']})
    off = client.put('/api/me/notice-mail', json={'on': False, 'kinds': ['assigned']})
    admin = client.patch(f'/api/admin/users/{BOB}', json={'mailNotices': {'on': False, 'kinds': []}})
    listed = client.get('/api/admin/users').json()

    # then
    assert (read.status_code, write.status_code, token.status_code) == (401, 401, 401)
    assert shown['ready'] is False and shown['on'] is False
    assert refused.status_code == 409 and off.status_code == 200
    assert admin.status_code == 422, 'the admin user form has no field for it'
    assert Users(store.db).get(BOB)['mailNotices'] == {'on': True, 'kinds': ['reminder']}
    assert 'mailNotices' not in str(listed)


@pytest.mark.parametrize('kind', list(LABELS))
def test_every_kind_has_an_outbound_head(kind):
    assert noticemail.head_of(kind) == LABELS[kind]
