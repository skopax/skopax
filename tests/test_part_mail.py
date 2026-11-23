import datetime as dt
import html
import logging

import pytest

from skopax import brand, mail, partmail, sweepout, tagreport, warndigest
from skopax.cardlink import card_url
from skopax.contract import Check, Digest, Field, PassResult, Sweep
from skopax.slack import Slack, SlackDown
from skopax.store import Store
from tests.conftest import AUTH
from tests.fake_slack import FakePost, FakeSlackApi, fake_slack
from tests.plugins.base import minimal
from tests.test_page_scan_seams import NOW, running

BASE = 'https://skopax.example.com'
FRI_0930 = dt.datetime(2026, 11, 20, 9, 30, tzinfo=dt.timezone.utc).timestamp()
FRI_1000 = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.timezone.utc).timestamp()
FRI_1130 = dt.datetime(2026, 11, 20, 11, 30, tzinfo=dt.timezone.utc).timestamp()
ALICE, BOB, CARL = 'alice@example.com', 'bob@example.com', 'carl@example.com'
LABELS = {'developer': 'Developer', 'data': 'Data', 'infra': 'Infra', 'seo': 'SEO'}
PLAN: dict = {}


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def card(key: str, sev: str = 'warn', tags: tuple = ('developer',), num=None, now: float = FRI_0930, **extra) -> dict:
    return {'key': key, 'num': num, 'sev': sev, 'status': 'open', 'svc': extra.pop('svc', 'checkout-service'), 'label': extra.pop('label', 'ERROR pattern'),
            'firstSeen': iso(now - 3600), 'tags': [{'id': t, 'by': 'rule', 'at': iso(now)} for t in tags], **extra}


def open_cards(store: Store, *cards: dict) -> None:
    store.save({'anomalies': list(cards), 'sweptIso': iso(FRI_0930), 'history': []}, None)


class Box:
    def __init__(self, ready: bool = True, refuse: tuple = (), down: bool = False):
        self.is_ready, self.refuse, self.down = ready, refuse, down
        self.sent: list[dict] = []
        self.tried: list[str] = []

    def ready(self) -> bool:
        return self.is_ready

    def send(self, to, subject, text, html=None, *, kind=''):
        self.tried.append(to[0])
        if self.down:
            raise mail.MailDown('connection refused')
        if to[0] in self.refuse:
            raise mail.MailRefused(f'550 5.1.1 <{to[0]}> no such user', recipient=True)
        self.sent.append({'to': list(to), 'subject': subject, 'text': text, 'html': html, 'kind': kind})
        return '<id@example.com>'

    def to(self, address: str) -> list[dict]:
        return [m for m in self.sent if m['to'] == [address]]


def parts(box: Box, owners: dict, on: bool = True, gone: set | None = None) -> partmail.PartMail:
    return partmail.PartMail(lambda: box, lambda: on, lambda: owners, labels=lambda: LABELS, left_out=lambda: set(gone or ()))


def no_slack():
    post = FakePost()
    return Slack('', '', post, FakeSlackApi()), post


def reporter(store: Store, box: Box, owners: dict, slack=None, on: bool = True, autosend: bool = True) -> tagreport.TagReport:
    slack = slack or fake_slack(emails={'U0ALICE': ALICE})[0]
    return tagreport.TagReport(store, slack, tagreport.SlackIds(slack), lambda: owners, lambda: {}, lambda: '10:00', lambda: autosend, BASE, labels=lambda: LABELS,
                               mail=parts(box, owners, on))


def hrefs(text: str) -> list[str]:
    return [html.unescape(part.split('"', 1)[0]) for part in text.split('href="')[1:]]


def test_the_tag_report_mails_each_responsible_only_their_tags_and_those_tags_open_cards(store: Store):
    # given
    open_cards(store, card('a', 'crit', ('developer',), num=1, svc='checkout <b>'), card('b', 'warn', ('developer',), num=2), card('c', 'warn', ('data',), num=3),
               card('d', 'warn', ('infra',), num=4))
    box = Box()
    r = reporter(store, box, {'developer': [ALICE], 'data': [BOB, ALICE.upper()]})

    # when
    r.run_once(FRI_1000)

    # then
    [alice] = box.to(ALICE)
    [bob] = box.to(BOB)
    assert len(box.sent) == 2
    assert card_url(BASE, 'a', 1) in hrefs(alice['html']) and card_url(BASE, 'b', 2) in hrefs(alice['html']) and card_url(BASE, 'c', 3) in hrefs(alice['html'])
    assert card_url(BASE, 'd', 4) not in hrefs(alice['html']) and tagreport.board_link(BASE, 'developer') in hrefs(alice['html'])
    assert hrefs(bob['html']).count(card_url(BASE, 'c', 3)) == 1 and card_url(BASE, 'a', 1) not in hrefs(bob['html'])
    assert 'checkout &lt;b&gt;' in alice['html'] and 'checkout <b>' in alice['text']
    assert [line.rsplit(' ', 1)[1] for line in bob['text'].split('\n') if line.startswith('- ')] == [card_url(BASE, 'c', 3)] and '#1' not in bob['text']
    assert bob['subject'] == '[Skopax] 1 open card in your tags' and alice['subject'] == '[Skopax] 3 open cards in your tags'
    attribution = brand.attribution_line()
    assert alice['text'].rstrip('\n').split('\n')[-1] == f"{attribution['text']} {attribution['href']}" and alice['kind'] == partmail.MAIL_KIND


def test_no_tag_report_mail_without_a_ready_mail_server_or_with_the_switch_off(store: Store):
    # given
    open_cards(store, card('a', num=1))
    owners = {'developer': [ALICE]}
    unready, off = Box(ready=False), Box()
    none = tagreport.TagReport(store, no_slack()[0], tagreport.SlackIds(no_slack()[0]), lambda: owners, lambda: {}, lambda: '10:00', lambda: True, BASE, labels=lambda: LABELS,
                               mail=partmail.PartMail(lambda: None, lambda: True, lambda: owners, labels=lambda: LABELS))

    # when
    reporter(store, unready, owners, slack=no_slack()[0]).run_once(FRI_1000)
    reporter(store, off, owners, slack=no_slack()[0], on=False).run_once(FRI_1000)
    none.run_once(FRI_1000)

    # then
    assert unready.tried == [] and off.tried == []
    assert none.scheduled is False


def test_the_tag_report_mail_goes_when_slack_is_not_set_up_or_auto_send_is_off(store: Store):
    # given
    open_cards(store, card('a', num=1))
    owners = {'developer': [ALICE]}
    box, other = Box(), Box()
    unset = reporter(store, box, owners, slack=no_slack()[0])

    # when
    scheduled = unset.scheduled
    unset.run_once(FRI_1000)
    store.runs.delete_many({})
    store.db['meta'].delete_many({})
    reporter(store, other, owners, autosend=False).run_once(FRI_1000)

    # then
    assert scheduled is True and unset.enabled is False
    assert len(box.to(ALICE)) == 1 and len(other.to(ALICE)) == 1


def test_a_responsible_with_nothing_open_in_their_tags_gets_no_mail(store: Store):
    # given
    open_cards(store, card('a', num=1), card('b', num=2, tags=('seo',), triage='progress'), card('c', num=3, tags=('infra',), muted=True))
    box = Box()

    # when
    reporter(store, box, {'developer': [ALICE], 'seo': [BOB], 'infra': [CARL]}, slack=no_slack()[0]).run_once(FRI_1000)

    # then
    assert [m['to'] for m in box.sent] == [[ALICE]]


def test_the_tag_report_mails_once_a_day_across_two_pods_with_slack_off(store: Store):
    # given
    open_cards(store, card('a', num=1))
    owners = {'developer': [ALICE]}
    one, two = Box(), Box()

    # when
    reporter(store, one, owners, slack=no_slack()[0]).run_once(FRI_1000)
    reporter(store, two, owners, slack=no_slack()[0]).run_once(FRI_1000)
    reporter(store, two, owners, slack=no_slack()[0]).run_once(FRI_1130)

    # then
    assert len(one.sent) == 1 and two.tried == []


def test_a_slack_post_tried_again_does_not_mail_twice(store: Store):
    # given
    open_cards(store, card('a', num=1))
    owners = {'developer': [ALICE]}
    down = SlackDown('Slack did not answer')
    down.__cause__ = ConnectionRefusedError(61, 'refused')
    slack, post, _ = fake_slack(emails={'U0ALICE': ALICE}, post_fail=down)
    box = Box()
    r = reporter(store, box, owners, slack=slack)

    # when
    first = r.run_once(FRI_1000)
    post.fail = False
    again = r.run_once(FRI_1000 + tagreport.PERIOD)

    # then
    assert first is None and again is not None and len(post.posted) == 1
    assert len(box.sent) == 1


def test_a_refused_responsible_is_logged_masked_once_and_the_others_still_get_theirs(store: Store, caplog):
    # given
    open_cards(store, card('a', num=1), card('c', num=3, tags=('data',)))
    box = Box(refuse=(ALICE,))
    r = reporter(store, box, {'developer': [ALICE], 'data': [BOB]}, slack=no_slack()[0])

    # when
    with caplog.at_level(logging.WARNING):
        r.run_once(FRI_1000)
        r.run_once(FRI_1130)

    # then
    assert box.tried.count(ALICE) == 1 and len(box.to(BOB)) == 1
    said = [rec.getMessage() for rec in caplog.records if 'a***@example.com' in rec.getMessage()]
    assert len(said) == 1 and 'no such user' in said[0]
    assert all(ALICE not in rec.getMessage() for rec in caplog.records)


def test_a_mail_server_that_is_down_stops_the_rest_with_one_line_and_no_retry(store: Store, caplog):
    # given
    open_cards(store, card('a', num=1), card('c', num=3, tags=('data',)))
    box = Box(down=True)
    r = reporter(store, box, {'developer': [ALICE], 'data': [BOB]}, slack=no_slack()[0])

    # when
    with caplog.at_level(logging.WARNING):
        r.run_once(FRI_1000)
        r.run_once(FRI_1130)

    # then
    assert len(box.tried) == 1
    assert len([rec for rec in caplog.records if rec.name == 'skopax.partmail']) == 1


def digest_of(store: Store, box: Box, owners: dict, slack=None, autosend: bool = True) -> warndigest.WarnDigest:
    slack = slack or fake_slack()[0]
    return warndigest.WarnDigest(store, slack, lambda: '09:30', lambda: autosend, BASE, mail=parts(box, owners))


def test_the_warnings_digest_mails_each_responsible_only_the_waiting_warnings_of_their_tags(store: Store):
    # given
    open_cards(store, card('a', num=1, label='Slow <checkout>'), card('b', num=2, tags=('data',)), card('c', num=3, tags=()), card('d', num=4, owner=ALICE),
               card('e', 'crit', num=5), card('f', num=6, tags=('data', 'developer')))
    box = Box()

    # when
    digest_of(store, box, {'developer': [ALICE], 'data': [BOB], 'seo': [CARL]}).run_once(FRI_0930)

    # then
    [alice] = box.to(ALICE)
    [bob] = box.to(BOB)
    assert len(box.sent) == 2
    assert [u for u in hrefs(alice['html']) if '/c/' in u] == [card_url(BASE, 'f', 6), card_url(BASE, 'a', 1)]
    assert [u for u in hrefs(bob['html']) if '/c/' in u] == [card_url(BASE, 'f', 6), card_url(BASE, 'b', 2)]
    assert 'Slow &lt;checkout&gt;' in alice['html'] and 'Slow <checkout>' in alice['text']
    assert alice['subject'] == '[Skopax] 2 Warning cards in your tags are waiting'


def test_the_warnings_digest_mail_goes_with_slack_off_once_across_pods(store: Store):
    # given
    open_cards(store, card('a', num=1))
    one, two = Box(), Box()
    first = digest_of(store, one, {'developer': [ALICE]}, slack=no_slack()[0])

    # when
    scheduled = first.scheduled
    first.run_once(FRI_0930)
    digest_of(store, two, {'developer': [ALICE]}, slack=no_slack()[0]).run_once(FRI_1000)
    digest_of(store, two, {'developer': [ALICE]}, autosend=False).run_once(FRI_1130)

    # then
    assert scheduled is True and first.enabled is False
    assert len(one.sent) == 1 and two.tried == []


def test_the_warnings_digest_sends_no_mail_to_a_responsible_whose_tags_wait_on_nothing(store: Store):
    # given
    open_cards(store, card('a', num=1, tags=()))
    box = Box()

    # when
    digest_of(store, box, {'developer': [ALICE]}).run_once(FRI_0930)

    # then
    assert box.tried == []


def weekly(ctx) -> Sweep:
    return Sweep(PassResult('weekly', True, 'ok', [], {}, ('weekly',)), digest=PLAN.get('digest'))


WEEKLY = minimal('weekly', 'Weekly', fields=(Field('url', 'Site URL', 'url'),), checks=(Check('weekly', 'Weekly summary', 604800, 0, ('weekly',), weekly),))


@pytest.fixture
def outbox(monkeypatch):
    PLAN.clear()
    box = Box()
    monkeypatch.setattr(mail, 'mailer', lambda runtime: box)
    yield box
    PLAN.clear()


def mailed(c) -> list[dict]:
    return [a for a in c.get('/api/actions', headers=AUTH).json() if a['type'] == sweepout.CHECK_DIGEST_MAIL]


def test_a_digest_that_names_tags_is_mailed_once_to_each_responsible_with_slack_not_set_up(store: Store, tmp_path, outbox, caplog):
    # given
    store.set_tag_owners('developer', [ALICE])
    store.set_tag_owners('data', [ALICE, BOB])
    PLAN['digest'] = Digest('Weekly <b>summary</b> & co', ('12 deploys <@U123>', '  ', 'Errors \n down 30%'), tags=('developer', 'data', 'nope', 'seo'))

    # when
    with caplog.at_level(logging.WARNING), running(store, tmp_path, plugins=(WEEKLY,)) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)
        done = mailed(c)

    # then
    [alice] = outbox.to(ALICE)
    [bob] = outbox.to(BOB)
    assert len(outbox.sent) == 2
    assert alice['subject'] == '[Skopax] Weekly <b>summary</b> & co'
    assert 'Weekly &lt;b&gt;summary&lt;/b&gt; &amp; co' in alice['html'] and '12 deploys &lt;@U123&gt;' in alice['html'] and 'Errors down 30%' in alice['html']
    assert '12 deploys <@U123>' in bob['text'] and 'Errors down 30%' in bob['text']
    [action] = done
    assert (action['status'], action['plugin'], action['label']) == ('done', 'weekly', 'Weekly <b>summary</b> & co')
    assert action['target'] == f'{sweepout.CHECK_DIGEST_MAIL}:weekly/weekly/main' and '2 people' in action['result']
    assert len([r for r in caplog.records if 'nope' in r.getMessage()]) == 1


def test_a_digest_without_tags_is_not_mailed(store: Store, tmp_path, outbox):
    # given
    store.set_tag_owners('developer', [ALICE])
    PLAN['digest'] = Digest('Weekly summary', ('12 deploys',))

    # when
    with running(store, tmp_path, plugins=(WEEKLY,)) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)
        done = mailed(c)

    # then
    assert outbox.tried == [] and done == []


def test_a_huge_digest_mail_keeps_the_slack_limits(store: Store, tmp_path, outbox):
    # given
    store.set_tag_owners('developer', [ALICE])
    words = ' '.join(f'w{i:04d}' for i in range(200))
    PLAN['digest'] = Digest(words, tuple(f'line {i} ' + words for i in range(sweepout.DIGEST_LINES_MAX + 7)), tags=('developer',))

    # when
    with running(store, tmp_path, plugins=(WEEKLY,)) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)

    # then
    [alice] = outbox.sent
    body = alice['text'].split('\n-- \n')[0]
    rows = [line for line in body.split('\n') if line.startswith('- ')]
    assert len(alice['subject']) <= mail.SUBJECT_MAX and len(body) <= sweepout.POST_TEXT_MAX + 200
    assert all(len(row) <= sweepout.LINE_MAX + 2 for row in rows) and rows[-1].startswith('- and ') and rows[-1].endswith(' more')


@pytest.mark.parametrize('bad', ['developer', ('developer', 3), {'developer'}])
def test_digest_tags_that_are_not_a_tuple_of_texts_fail_the_pass(store: Store, tmp_path, outbox, bad):
    # given
    slack, post, _ = fake_slack()
    store.set_tag_owners('developer', [ALICE])
    PLAN['digest'] = Digest('Weekly summary', ('12 deploys',), tags=bad)

    # when
    with running(store, tmp_path, plugins=(WEEKLY,), slack=slack) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)

    # then
    assert outbox.tried == [] and post.posted == []
    assert store.load()['sources']['weekly'].startswith('error:') and 'digest' in store.load()['sources']['weekly']


def test_the_mail_switch_is_a_card_tags_setting_on_by_default(client):
    # when
    item = next(i for i in client.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == 'tags.mail')
    bad = client.put('/api/admin/settings', json={'key': 'tags.mail', 'value': 'maybe'}, headers=AUTH)
    off = client.put('/api/admin/settings', json={'key': 'tags.mail', 'value': 'false'}, headers=AUTH)

    # then
    assert (item['group'], item['card'], item['kind'], item['default'], item['value']) == ('Card tags', 'Mail', 'bool', 'true', 'true')
    assert bad.status_code == 422 and off.status_code == 200


def test_the_app_schedules_and_mails_both_daily_runs_with_slack_not_set_up_and_the_switch_stops_all_three(store: Store, tmp_path, outbox):
    # given
    store.set_tag_owners('developer', [ALICE])
    open_cards(store, card('a', num=1))
    PLAN['digest'] = Digest('Weekly summary', ('12 deploys',), tags=('developer',))

    # when
    with running(store, tmp_path, plugins=(WEEKLY,)) as c:
        state = c.app.state
        on = {j.name for j in state.scheduler.jobs}
        tagged, warned = state.tag_report._mail(FRI_1000), state.warn_digest._mail(FRI_0930)
        switched = c.put('/api/admin/settings', json={'key': 'tags.mail', 'value': 'false'}, headers=AUTH)
        off = {j.name for j in state.scheduler.jobs}
        before = len(outbox.tried)
        state.sweeper.run('weekly', 'weekly', NOW)
        after = len(outbox.tried)

    # then
    assert {tagreport.RUN, warndigest.RUN} <= on and state.tag_report.enabled is False and state.warn_digest.enabled is False
    assert (tagged, warned) == (1, 1) and len(outbox.to(ALICE)) == 2
    assert switched.status_code == 200 and not {tagreport.RUN, warndigest.RUN} & off
    assert after == before and mailed_none(store)


def mailed_none(store: Store) -> bool:
    return list(store.actions.find({'type': sweepout.CHECK_DIGEST_MAIL})) == []


def rows_of(text: str) -> list[str]:
    return [line for line in text.split('\n') if line.startswith('- ')]


def test_a_card_with_two_of_a_persons_tags_is_listed_once_under_the_first_with_also_and_the_counts_match(store: Store):
    # given
    open_cards(store, card('x', 'warn', ('developer', 'data'), num=1), card('y', 'crit', ('developer',), num=2), card('z', 'warn', ('data',), num=3))
    box = Box()

    # when
    reporter(store, box, {'developer': [ALICE], 'data': [ALICE]}, slack=no_slack()[0]).run_once(FRI_1000)

    # then
    [alice] = box.sent
    rows = rows_of(alice['text'])
    assert hrefs(alice['html']).count(card_url(BASE, 'x', 1)) == 1 and len(rows) == 3
    assert [r.split(' ', 2)[1] for r in rows] == ['#2', '#1', '#3']
    assert rows[1].endswith(f'also Data {card_url(BASE, "x", 1)}')
    assert 'Developer · 2 open cards: 1 critical, 1 warning' in alice['text'] and 'Data · 1 open card: 1 warning' in alice['text']
    assert alice['subject'] == '[Skopax] 3 open cards in your tags'


def test_a_tag_without_a_label_is_named_by_its_id():
    # when
    [(_, (subject, text, _))] = tagreport.build_mails([card('a', num=1, tags=('ghost',))], {ALICE: ['ghost']}, BASE, FRI_1000, {})

    # then
    assert 'ghost · 1 open card: 1 warning' in text


def test_the_tag_report_mail_shows_a_coloured_severity_links_only_the_card_name_and_has_a_board_button(store: Store):
    # given
    open_cards(store, card('a', 'crit', ('developer',), num=1, label='Checkout down'), card('b', 'high', ('developer',), num=2), card('c', 'warn', ('data',), num=3))
    box = Box()

    # when
    reporter(store, box, {'developer': [ALICE, BOB], 'data': [ALICE]}, slack=no_slack()[0]).run_once(FRI_1000)

    # then
    [alice] = box.to(ALICE)
    [bob] = box.to(BOB)
    page = bob['html']
    assert f'<a href="{card_url(BASE, "a", 1)}" style="color:{mail.INK};">#1 Checkout down</a>' in page
    for sev, word in (('crit', 'Critical'), ('high', 'High')):
        assert f'background-color:{mail.SEV_COLOURS[sev]};color:{mail.SEV_INKS.get(sev, "#ffffff")};' in page and f'>{word}</span>' in page
    assert f'background-color:{mail.SEV_COLOURS["warn"]};color:{mail.SEV_INKS["warn"]};' in alice['html']
    assert hrefs(bob['html'])[-2] == tagreport.board_link(BASE, 'developer') and f'background-color:{mail.BUTTON};' in bob['html']
    assert f'Open the board: {tagreport.board_link(BASE, "developer")}' in bob['text'].split('\n')
    assert f'Open the board: {tagreport.open_link(BASE)}' in alice['text'].split('\n')


def test_the_warnings_mail_and_the_digest_mail_point_their_button_at_their_view(store: Store, tmp_path, outbox):
    # given
    open_cards(store, card('a', num=1))
    store.set_tag_owners('developer', [ALICE])
    PLAN['digest'] = Digest('Weekly summary', ('12 deploys',), tags=('developer',))
    box = Box()

    # when
    digest_of(store, box, {'developer': [ALICE]}).run_once(FRI_0930)
    with running(store, tmp_path, plugins=(WEEKLY,)) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)

    # then
    [warned] = box.sent
    [weekly] = outbox.sent
    assert hrefs(warned['html'])[-2] == warndigest.board_link(BASE) and f'Open the board: {warndigest.board_link(BASE)}' in warned['text'].split('\n')
    assert f'background-color:{mail.BUTTON};' in weekly['html'] and any(line.startswith('Open the board: ') and line.endswith('/#t=anoms/open') for line in weekly['text'].split('\n'))
    assert weekly['subject'] == '[Skopax] Weekly summary'


def test_a_failed_digest_mail_also_waits_out_the_hourly_gap(store: Store, tmp_path, outbox):
    # given
    store.set_tag_owners('developer', [ALICE])
    outbox.down = True
    PLAN['digest'] = Digest('Weekly summary', ('12 deploys',), tags=('developer',))

    # when
    with running(store, tmp_path, plugins=(WEEKLY,)) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)
        c.app.state.sweeper.run('weekly', 'weekly', NOW + 60)
        done = mailed(c)

    # then
    assert len(outbox.tried) == 1 and [a['status'] for a in done] == ['failed']


def test_the_daily_features_say_what_the_slack_post_and_the_mail_each_need(store: Store, tmp_path):
    # given
    post = FakePost()
    unset = Slack('', '', post, FakeSlackApi())

    # when
    with running(store, tmp_path, plugins=(), slack=unset) as c:
        items = {i['id']: i for i in c.get('/api/admin/features', headers=AUTH).json()['items']}
        settings = {i['key']: i for i in c.get('/api/admin/settings', headers=AUTH).json()['items']}

    # then
    for fid in ('tag-report', 'warn-digest'):
        assert 'the mail to responsibles needs a Mail server and Mail responsibles their part' in items[fid]['help'], fid
        assert items[fid]['needs'] and all(n.startswith('The Slack post needs ') and 'the mail to responsibles needs a Mail server' in n for n in items[fid]['needs']), fid
    for key in ('tags.report-at', 'slack.warn-digest-at'):
        assert 'Slack post and the mail to responsibles' in settings[key]['help'], key
