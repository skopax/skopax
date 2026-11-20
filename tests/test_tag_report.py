import datetime as dt
import json
import logging

import pytest

from tests.fake_slack import FakePost, FakeSlackApi, fake_slack
from skopax import clock, tagreport
from skopax.slack import Slack, SlackDown, SlackError
from skopax.store import Store

BASE = 'https://skopax.example.com'
FRI_0930 = dt.datetime(2026, 11, 20, 9, 30, tzinfo=dt.timezone.utc).timestamp()
FRI_1000 = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.timezone.utc).timestamp()
FRI_1405 = dt.datetime(2026, 11, 20, 14, 5, tzinfo=dt.timezone.utc).timestamp()
SAT_1100 = dt.datetime(2026, 11, 21, 11, 0, tzinfo=dt.timezone.utc).timestamp()
SUN_1100 = dt.datetime(2026, 11, 22, 11, 0, tzinfo=dt.timezone.utc).timestamp()
MON_1000 = dt.datetime(2026, 11, 23, 10, 0, tzinfo=dt.timezone.utc).timestamp()
FRI_2330 = dt.datetime(2026, 11, 20, 23, 30, tzinfo=dt.timezone.utc).timestamp()
ALICE, BOB, CARL = 'alice@example.com', 'bob@example.com', 'carl@example.com'
NAMES = {ALICE: 'Alice Moss', BOB: 'Bob Reed', CARL: 'Carl <Admin> & Co'}
LABELS = {'developer': 'Developer', 'data': 'Data', 'infra': 'Infra', 'seo': 'SEO'}


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def card(key: str, sev: str = 'warn', tags: tuple = ('developer',), hours: float = 1, now: float = FRI_1000, **extra) -> dict:
    return {'key': key, 'num': extra.pop('num', None), 'sev': sev, 'status': 'open', 'svc': extra.pop('svc', 'checkout-service'), 'label': 'ERROR pattern',
            'firstSeen': iso(now - hours * 3600), 'tags': [{'id': t, 'by': 'rule', 'at': iso(now)} for t in tags], **extra}


def mentions(ids: dict):
    return lambda email: f'<@{ids[email]}>' if email in ids else None


def built(cards, owners, ids=None, now=FRI_1000):
    return tagreport.build(cards, owners, NAMES, mentions(ids or {}), BASE, now, LABELS)


def test_only_tags_with_responsibles_and_an_open_card_get_a_line_sorted_by_critical_then_open_count():
    # given
    cards = [card('a', 'crit', ('data',), num=1), card('b', 'warn', ('developer',), num=2), card('c', 'warn', ('developer',), num=3), card('d', 'info', ('developer',), num=4),
             card('e', 'crit', ('infra',), num=5), card('f', 'warn', ('seo',), num=6)]
    owners = {'developer': [ALICE], 'data': [BOB], 'seo': [], 'analyst': [ALICE]}

    # when
    out = built(cards, owners, {ALICE: 'U0ALICE', BOB: 'U0BOB'})

    # then
    lines = [b['text']['text'].split('\n')[0] for b in out['blocks'][1:]]
    assert len(lines) == 2
    assert lines[0] == '<@U0BOB> \u00b7 *<https://skopax.example.com/#t=anoms/open/tag:data|Data>* \u00b7 1 open: 1 critical, 0 warning'
    assert lines[1] == '<@U0ALICE> \u00b7 *<https://skopax.example.com/#t=anoms/open/tag:developer|Developer>* \u00b7 3 open: 0 critical, 2 warning'
    assert '<@U0ALICE>' in out['text'] and '<@U0BOB>' in out['text']


def test_the_board_open_meaning_drops_muted_taken_resolved_grouped_and_quiet_info_cards():
    # given
    cards = [card('open', num=1), card('muted', muted=True, num=2), card('taken', triage='handled', num=3), card('progress', triage='progress', num=4),
             card('quiet-info', 'info', status='cleared', num=5), card('quiet-warn', 'warn', status='cleared', num=6), card('child', parent='open', num=7),
             card('host-gone', parent='nowhere', num=8)]

    # when
    shown = [c['key'] for c in tagreport.board_open(cards)]

    # then
    assert shown == ['open', 'quiet-warn', 'host-gone']


def test_each_tag_shows_its_three_worst_oldest_cards_and_links_the_rest_as_more():
    # given
    cards = [card('w-new', 'warn', hours=1, num=11), card('c-old', 'crit', hours=30, num=12), card('w-old', 'warn', hours=50, num=13), card('c-new', 'crit', hours=0.5, num=14),
             card('i', 'info', hours=90, num=15)]

    # when
    out = built(cards, {'developer': [ALICE]})

    # then
    text = out['blocks'][1]['text']['text'].split('\n')
    assert [line.split('|')[1].split('>')[0] for line in text[1:4]] == ['#12', '#14', '#13']
    assert text[1] == '\u2022 <https://skopax.example.com/c/12|#12> :red_circle: Critical \u00b7 checkout-service \u00b7 30 h'
    assert text[2].endswith('\u00b7 30 min')
    assert text[3].endswith('\u00b7 2 d')
    assert text[4] == '<https://skopax.example.com/#t=anoms/open/tag:developer|+2 more>'
    assert len(text) == 5


def test_a_card_without_a_number_links_by_key_and_no_more_line_under_four_cards():
    # given
    cards = [card('log|checkout|ERROR|x', 'warn')]

    # when
    out = built(cards, {'developer': [ALICE]}, {ALICE: 'U0ALICE'})

    # then
    lines = out['blocks'][1]['text']['text'].split('\n')
    assert len(lines) == 2
    assert lines[1].startswith('\u2022 <https://skopax.example.com/#a=log%7Ccheckout%7CERROR%7Cx|log\u01c0checkout\u01c0ERROR\u01c0x>')


def test_unmatched_users_are_named_in_plain_text_and_names_labels_and_services_are_escaped():
    # given
    cards = [card('a', 'warn', ('developer',), num=1, svc='shop<web>&co')]
    labels = dict(LABELS, developer='Dev <ops> | team')

    # when
    out = tagreport.build(cards, {'developer': [ALICE, CARL, 'dora@example.com']}, NAMES, mentions({ALICE: 'U0ALICE'}), BASE, FRI_1000, labels)

    # then
    text = out['blocks'][1]['text']['text']
    head = text.split('\n')[0]
    assert head.startswith('<@U0ALICE>, Carl &lt;Admin&gt; &amp; Co, dora \u00b7 ')
    assert '|Dev &lt;ops&gt; \u01c0 team>*' in head
    assert 'shop&lt;web&gt;&amp;co' in text
    assert '<@' not in head.replace('<@U0ALICE>', '')


def test_nothing_is_built_when_no_tag_qualifies():
    # given
    cards = [card('a', 'warn', ('developer',), muted=True), card('b', 'warn', ('seo',))]

    # when
    out = built(cards, {'developer': [ALICE], 'data': [BOB]})

    # then
    assert out is None


def test_many_tags_stay_within_the_slack_block_limit_and_say_how_many_were_left_out():
    # given
    labels = {f't{n}': f'Team {n}' for n in range(60)}
    cards = [card(f'k{n}', 'warn', (f't{n}',), num=n + 1) for n in range(60)]

    # when
    out = tagreport.build(cards, {t: [ALICE] for t in labels}, NAMES, mentions({}), BASE, FRI_1000, labels)

    # then
    assert len(out['blocks']) <= 50
    assert out['blocks'][-1]['type'] == 'context'
    assert out['blocks'][-1]['elements'][0]['text'] == '+12 more tags'
    assert all(len(b['text']['text']) <= 3000 for b in out['blocks'] if b['type'] == 'section')
    assert len(out['text']) <= 3000


def test_the_report_time_must_be_hh_mm():
    # then
    assert [tagreport.valid_at(v) for v in ('10:00', '00:00', '23:59', '9:00', '24:00', '10:60', '', 'ten')] == [True, True, True, False, False, False, False, False]


@pytest.mark.parametrize('now, zone, expected', [
    (FRI_0930, 'UTC', False), (FRI_1000, 'UTC', True), (FRI_1405, 'UTC', True), (SAT_1100, 'UTC', False), (SUN_1100, 'UTC', False), (MON_1000, 'UTC', True),
    (FRI_2330, 'UTC', True), (FRI_2330, 'Europe/Berlin', False), (FRI_2330, 'America/New_York', True)])
def test_the_report_is_due_at_or_after_the_time_on_business_days_in_the_install_zone(now, zone, expected):
    # given
    clock.set_zone(zone)

    # then
    assert tagreport.due('10:00', now) is expected


class Reporter:
    def __init__(self, store: Store, owners=None, at='10:00', on=True, autosend=True, emails=None, slack=None):
        self.slack, self.post, self.api = slack or fake_slack(emails=emails or {'U0ALICE': ALICE})
        self.flags = []
        self.ids = tagreport.SlackIds(self.slack, on_scope=self.flags.append)
        self.at, self.on, self.autosend = at, on, autosend
        self.owners = {'developer': [ALICE]} if owners is None else owners
        self.report = tagreport.TagReport(store, self.slack, self.ids, lambda: self.owners, lambda: NAMES, lambda: self.at, lambda: self.on and self.autosend, BASE, labels=lambda: LABELS)


def open_cards(store: Store, *cards: dict) -> None:
    store.save({'anomalies': list(cards), 'sweptIso': iso(FRI_1000), 'history': []}, None)


def test_the_report_posts_once_a_day_at_or_after_the_time_and_not_before(store: Store):
    # given
    open_cards(store, card('a', num=1))
    r = Reporter(store)

    # when
    early = r.report.run_once(FRI_0930)
    first = r.report.run_once(FRI_1000)
    again = r.report.run_once(FRI_1405)

    # then
    assert (early, again) == (None, None)
    assert first is not None and len(r.post.posted) == 1
    assert r.post.posted[0]['blocks'][1]['text']['text'].startswith('<@U0ALICE> \u00b7 ')
    assert r.post.posted[0]['channel'] == '#ops'


def test_a_restart_or_a_second_replica_does_not_post_the_same_day_twice(store: Store):
    # given
    open_cards(store, card('a', num=1))
    one, two = Reporter(store), Reporter(store)

    # when
    one.report.run_once(FRI_1000)
    two.report.run_once(FRI_1405)
    restarted = Reporter(store)
    restarted.report.run_once(FRI_1405)

    # then
    assert len(one.post.posted) == 1 and two.post.posted == [] and restarted.post.posted == []


def test_saturday_and_sunday_post_nothing_and_leave_the_day_unclaimed_and_monday_posts_what_is_open(store: Store):
    # given
    open_cards(store, card('a', num=1, now=SAT_1100))
    r = Reporter(store)

    # when
    sat, sun = r.report.run_once(SAT_1100), r.report.run_once(SUN_1100)
    claimed = [d['_id'] for d in store.runs.find({'_id': {'$regex': '^tag-report:'}})]
    mon = r.report.run_once(MON_1000)

    # then
    assert (sat, sun) == (None, None) and claimed == []
    assert mon is not None and len(r.post.posted) == 1


def test_late_friday_utc_is_saturday_in_berlin_and_posts_nothing_there(store: Store):
    # given
    clock.set_zone('Europe/Berlin')
    open_cards(store, card('a', num=1))
    r = Reporter(store)

    # when
    out = r.report.run_once(FRI_2330)

    # then
    assert out is None and r.post.posted == []


def test_a_day_with_no_qualifying_tag_posts_nothing_and_counts_as_reported(store: Store):
    # given
    open_cards(store, card('a', num=1, muted=True))
    r = Reporter(store)

    # when
    first = r.report.run_once(FRI_1000)
    open_cards(store, card('a', num=1))
    later = r.report.run_once(FRI_1405)

    # then
    assert (first, later) == (None, None) and r.post.posted == []


def test_a_failed_post_leaves_the_day_open_for_the_next_pass(store: Store):
    # given
    open_cards(store, card('a', num=1))
    slack = fake_slack(emails={'U0ALICE': ALICE}, post_fail=True)
    r = Reporter(store, slack=slack)

    # when
    with pytest.raises(SlackError):
        r.report.run_once(FRI_1000)
    r.post.fail = False
    again = r.report.run_once(FRI_1405)

    # then
    assert again is not None and len(r.post.posted) == 1


@pytest.mark.parametrize('change', [{'at': ''}, {'on': False}, {'autosend': False}, {'owners': {}}])
def test_the_report_is_off_with_an_empty_time_the_feature_off_autosend_off_or_no_responsibles(store: Store, change: dict):
    # given
    open_cards(store, card('a', num=1))
    r = Reporter(store, **change)

    # when
    out = r.report.enabled

    # then
    assert out is False


def test_the_report_is_off_without_slack(store: Store):
    # given
    r = Reporter(store, slack=(Slack('', '', FakePost(), FakeSlackApi()), None, None))

    # then
    assert r.report.enabled is False


def test_lookups_are_cached_for_a_day_misses_included():
    # given
    now = [0.0]
    slack, _, api = fake_slack(emails={'U0ALICE': ALICE})
    ids = tagreport.SlackIds(slack, clock=lambda: now[0])

    # when
    first = [ids.mention(ALICE), ids.mention(BOB)]
    cached = [ids.mention(ALICE), ids.mention(BOB)]
    calls_then = api.methods().count('users.lookupByEmail')
    now[0] = tagreport.LOOKUP_TTL_S + 1
    ids.mention(ALICE)

    # then
    assert first == cached == ['<@U0ALICE>', None]
    assert calls_then == 2
    assert api.methods().count('users.lookupByEmail') == 3


def test_a_slack_outage_stops_lookups_for_that_report_is_not_cached_and_a_blank_email_is_never_looked_up():
    # given
    slack, _, api = fake_slack(emails={'U0ALICE': ALICE})
    api.fail = {'users.lookupByEmail'}
    ids = tagreport.SlackIds(slack)

    # when
    down = ids.mention(ALICE)
    stopped = ids.mention(ALICE)
    api.fail = set()
    ids.start()
    up = ids.mention(ALICE)
    blank = ids.mention('')

    # then
    assert (down, stopped, up, blank) == (None, None, '<@U0ALICE>', None)
    assert api.methods().count('users.lookupByEmail') == 2


class ScopeApi(FakeSlackApi):
    def __init__(self, code='missing_scope', **kw):
        super().__init__(**kw)
        self.code = code
        self.refuse = True

    def __call__(self, token, method, payload):
        if method == 'users.lookupByEmail' and self.refuse:
            self.calls.append((method, payload))
            raise SlackError('Slack refused', code=self.code, needed='users:read.email')
        return super().__call__(token, method, payload)


@pytest.mark.parametrize('code', ['missing_scope', 'not_allowed_token_type'])
def test_a_missing_email_scope_sets_the_flag_logs_once_poisons_no_cache_and_the_report_still_posts_plain_names(store: Store, caplog, code: str):
    # given
    open_cards(store, card('a', num=1), card('b', 'warn', ('data',), num=2))
    api = ScopeApi(code, emails={'U0ALICE': ALICE, 'U0BOB': BOB})
    post = FakePost()
    slack = (Slack('xoxb-test', '#ops', post, api), post, api)
    r = Reporter(store, owners={'developer': [ALICE], 'data': [BOB]}, slack=slack)
    caplog.set_level(logging.WARNING, 'skopax.tagreport')

    # when
    out = r.report.run_once(FRI_1000)
    looked = api.methods().count('users.lookupByEmail')
    api.refuse = False
    r.ids.start()
    later = r.ids.mention(ALICE)

    # then
    assert out is not None and len(post.posted) == 1
    text = json.dumps(post.posted[0]['blocks'])
    assert 'Alice Moss' in text and 'Bob Reed' in text and '<@' not in text
    assert looked == 1, 'one refused lookup stops the others in the same report'
    assert r.flags[0] is True and r.flags[-1] is False
    assert later == '<@U0ALICE>', 'the refusal was not cached as a miss'
    assert len([m for m in caplog.messages if 'users:read.email' in m]) == 1


def test_the_scope_flag_is_logged_once_across_reports(store: Store, caplog):
    # given
    api = ScopeApi(emails={'U0ALICE': ALICE})
    ids = tagreport.SlackIds(Slack('xoxb-test', '#ops', FakePost(), api))
    caplog.set_level(logging.WARNING, 'skopax.tagreport')

    # when
    for _ in range(3):
        ids.start()
        ids.mention(ALICE)

    # then
    assert ids.missing is True
    assert len([m for m in caplog.messages if 'users:read.email' in m]) == 1


def test_a_slack_down_lookup_does_not_set_the_scope_flag():
    # given
    api = FakeSlackApi(emails={'U0ALICE': ALICE})
    flags = []
    slack = Slack('xoxb-test', '#ops', FakePost(), lambda token, method, payload: (_ for _ in ()).throw(SlackDown('down')))
    ids = tagreport.SlackIds(slack, on_scope=flags.append)

    # when
    out = ids.mention(ALICE)

    # then
    assert out is None and flags == [] and ids.missing is False and api.calls == []
