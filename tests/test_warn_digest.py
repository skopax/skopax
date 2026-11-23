import datetime as dt
import json
import logging
import string
from pathlib import Path

import pytest

from tests.conftest import AUTH
from tests.fake_slack import FakePost, FakeSlackApi, fake_slack
from skopax import clock, messages, tagreport, warndigest
from skopax.slack import Slack, SlackDown, SlackError
from skopax.store import Store

BASE = 'https://skopax.example.com'
FRI_0900 = dt.datetime(2026, 11, 20, 9, 0, tzinfo=dt.timezone.utc).timestamp()
FRI_0930 = dt.datetime(2026, 11, 20, 9, 30, tzinfo=dt.timezone.utc).timestamp()
FRI_1000 = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.timezone.utc).timestamp()
FRI_1130 = dt.datetime(2026, 11, 20, 11, 30, tzinfo=dt.timezone.utc).timestamp()
FRI_1200 = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc).timestamp()
FRI_1400 = dt.datetime(2026, 11, 20, 14, 0, tzinfo=dt.timezone.utc).timestamp()
SAT_0930 = dt.datetime(2026, 11, 21, 9, 30, tzinfo=dt.timezone.utc).timestamp()
SUN_1000 = dt.datetime(2026, 11, 22, 10, 0, tzinfo=dt.timezone.utc).timestamp()
MON_0930 = dt.datetime(2026, 11, 23, 9, 30, tzinfo=dt.timezone.utc).timestamp()
WARN_VIEW = 'https://skopax.example.com/#t=anoms/warn/who:none'


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def card(key: str, sev: str = 'warn', hours: float = 1, now: float = FRI_0930, **extra) -> dict:
    return {'key': key, 'num': extra.pop('num', None), 'sev': sev, 'status': extra.pop('status', 'open'), 'svc': extra.pop('svc', 'checkout-service'),
            'label': extra.pop('label', 'ERROR pattern'), 'firstSeen': iso(now - hours * 3600), 'tags': [], **extra}


def built(cards, now=FRI_0930):
    return warndigest.build(cards, BASE, now)


def lines(out: dict) -> list[str]:
    return [line for b in out['blocks'][1:] if b['type'] == 'section' for line in b['text']['text'].split('\n')]


def keys_shown(out: dict | None) -> list[str]:
    return [] if out is None else [line.split('|')[1].split(' ')[0] for line in lines(out) if line.startswith('\u2022')]


class Digest:
    def __init__(self, store: Store, at='09:30', on=True, autosend=True, slack=None):
        self.slack, self.post, self.api = slack or fake_slack()
        self.at, self.on, self.autosend = at, on, autosend
        self.digest = warndigest.WarnDigest(store, self.slack, lambda: self.at, lambda: self.on and self.autosend, BASE)


def open_cards(store: Store, *cards: dict) -> None:
    store.save({'anomalies': list(cards), 'sweptIso': iso(FRI_0930), 'history': []}, None)


def test_only_open_warning_cards_nobody_addressed_are_listed():
    # given
    cards = [card('warn', num=1), card('stopped', num=2, status='cleared'), card('high', 'high', num=3), card('crit', 'crit', num=4), card('info', 'info', num=5),
             card('owned', num=6, owner='alice@example.com'), card('assigned', num=7, assigned={'email': 'alice@example.com', 'at': iso(FRI_0900)}),
             card('progress', num=8, triage='progress', owner='alice@example.com'), card('handled', num=9, triage='handled'), card('muted', num=10, muted='flaky'),
             card('ticket', num=11, ticket='OPS-1'), card('child', num=12, parent='warn'), card('auto', num=13, status='cleared', triage='handled', autoResolvedAt=iso(FRI_0900)),
             card('investigating', num=14, triage='investigating')]

    # when
    out = built(cards)

    # then
    assert sorted(keys_shown(out)) == ['#1', '#14', '#2']


def test_a_card_marked_known_noise_through_the_store_is_left_out(store: Store):
    # given
    open_cards(store, card('noise', num=1), card('real', num=2))
    store.mute('noise', 'known flaky probe', None, 'known')

    # when
    out = built(store.load()['anomalies'])

    # then
    assert keys_shown(out) == ['#2']


def test_cards_are_grouped_by_service_newest_first_with_a_count_line_on_top():
    # given
    cards = [card('a1', num=1, svc='api', hours=5), card('a2', num=2, svc='api', hours=1), card('w1', num=3, svc='web', hours=2), card('a3', num=4, svc='api', hours=3)]

    # when
    out = built(cards)

    # then
    assert out['blocks'][0]['text']['text'].startswith(':warning: *4 Warning cards are waiting*')
    groups = [b['text']['text'].split('\n') for b in out['blocks'][1:] if b['type'] == 'section']
    assert [g[0] for g in groups] == ['*api* (3)', '*web* (1)']
    assert [line.split('|')[1].split(' ')[0] for line in groups[0][1:]] == ['#2', '#4', '#1']
    assert groups[0][1] == '\u2022 <https://skopax.example.com/c/2|#2 ERROR pattern> \u00b7 1 h'
    assert '<@' not in json.dumps(out) and '<!' not in json.dumps(out)


def test_one_card_says_one_card_in_the_count_line():
    # when
    out = built([card('a', num=1)])

    # then
    assert out['blocks'][0]['text']['text'].startswith(':warning: *1 Warning card is waiting*')
    assert out['text'].startswith(':warning: *1 Warning card is waiting*')


def test_nothing_is_built_when_no_warning_card_waits():
    # when
    out = built([card('h', 'high', num=1), card('w', num=2, owner='alice@example.com')])

    # then
    assert out is None


def test_the_list_stops_at_fifteen_cards_and_links_the_rest_to_the_warning_board():
    # given
    cards = [card(f'k{n}', num=n + 1, svc=f'svc-{n % 4}', hours=n + 1) for n in range(20)]

    # when
    out = built(cards)

    # then
    assert len(keys_shown(out)) == warndigest.SHOWN_MAX == 15
    assert out['blocks'][0]['text']['text'].startswith(':warning: *20 Warning cards are waiting*')
    assert out['blocks'][-1]['type'] == 'context'
    assert out['blocks'][-1]['elements'][0]['text'] == f'<{WARN_VIEW}|+5 more on the board>'
    assert out['text'].endswith(f'<{WARN_VIEW}|+5 more on the board>')
    assert set(keys_shown(out)) == {f'#{n + 1}' for n in range(15)}, 'the newest 15 cards are the ones kept'


def test_the_count_line_links_the_board_and_no_more_line_when_all_fit():
    # when
    out = built([card(f'k{n}', num=n + 1) for n in range(15)])

    # then
    assert f'<{WARN_VIEW}|' in out['blocks'][0]['text']['text']
    assert all(b['type'] == 'section' for b in out['blocks'])


def test_labels_and_services_are_escaped_and_long_titles_are_cut():
    # given
    cards = [card('a', num=1, svc='shop<web>&co', label='<@U123> <!channel> | ' + 'x' * 200), card('log|checkout|ERROR|x')]

    # when
    out = built(cards)

    # then
    text = json.dumps(out, ensure_ascii=False)
    assert '<@U123>' not in text and '<!channel>' not in text
    assert '&lt;@U123&gt; &lt;!channel&gt; \u01c0' in text
    assert '*shop&lt;web&gt;&amp;co* (1)' in text
    long_line = next(line for line in lines(out) if '#1 ' in line)
    shown = long_line.split('|', 1)[1].split('> \u00b7')[0].replace('&lt;', '<').replace('&gt;', '>')
    assert shown.endswith('\u2026') and len(shown) == len('#1 ') + warndigest.TITLE_LIMIT
    assert any(line.startswith('\u2022 <https://skopax.example.com/#a=log%7Ccheckout%7CERROR%7Cx|') for line in lines(out))


def test_many_services_keep_the_slack_block_and_text_limits():
    # given
    cards = [card(f'k{n}', num=n + 1, svc=f'service-{n}-' + 'y' * 40, label='z' * 300) for n in range(80)]

    # when
    out = built(cards)

    # then
    assert len(out['blocks']) <= 50
    assert all(len(b['text']['text']) <= 3000 for b in out['blocks'] if b['type'] == 'section')
    assert len(out['text']) <= 3000


def test_the_digest_posts_once_in_the_window_on_a_business_day(store: Store):
    # given
    open_cards(store, card('a', num=1))
    d = Digest(store)

    # when
    early = d.digest.run_once(FRI_0900)
    first = d.digest.run_once(FRI_0930)
    again = d.digest.run_once(FRI_1130)

    # then
    assert (early, again) == (None, None)
    assert first == first | {'cards': 1} and len(d.post.posted) == 1
    assert d.post.posted[0]['channel'] == '#ops'
    assert d.post.posted[0]['blocks'][0]['text']['text'].startswith(':warning: *1 Warning card is waiting*')


def test_the_digest_and_the_tag_report_both_post_on_the_same_day(store: Store):
    # given
    open_cards(store, card('a', num=1, tags=[{'id': 'developer', 'by': 'rule', 'at': iso(FRI_0930)}]))
    d = Digest(store)
    slack, post, _ = fake_slack(emails={'U0ALICE': 'alice@example.com'})
    report = tagreport.TagReport(store, slack, tagreport.SlackIds(slack), lambda: {'developer': ['alice@example.com']}, lambda: {}, lambda: '10:00', lambda: True, BASE,
                                 labels=lambda: {'developer': 'Developer'})

    # when
    digest = d.digest.run_once(FRI_0930)
    tags = report.run_once(FRI_1000)

    # then
    assert digest is not None and tags is not None
    assert len(d.post.posted) == 1 and len(post.posted) == 1


def test_only_one_pod_posts_the_day(store: Store):
    # given
    open_cards(store, card('a', num=1))
    one, two = Digest(store), Digest(store)

    # when
    one.digest.run_once(FRI_0930)
    two.digest.run_once(FRI_1000)
    restarted = Digest(store)
    restarted.digest.run_once(FRI_1130)

    # then
    assert len(one.post.posted) == 1 and two.post.posted == [] and restarted.post.posted == []


def test_a_day_another_pod_already_claimed_posts_nothing(store: Store):
    # given
    open_cards(store, card('a', num=1))
    store.runs.insert_one({'_id': 'warn-digest:2026-11-20'})
    d = Digest(store)

    # when
    out = d.digest.run_once(FRI_0930)

    # then
    assert store.warn_digest_at() is None
    assert out is None and d.post.calls == []


def test_a_service_name_cannot_break_the_bold_or_add_lines():
    # when
    out = built([card('a', num=1, svc='x*\n\u2022 <fake> _y_ ~z~')])

    # then
    head = out['blocks'][1]['text']['text'].split('\n')[0]
    assert head.startswith('*') and head.endswith('* (1)') and head.count('*') == 2
    assert '_' not in head and '~' not in head and '<fake>' not in head
    assert len(out['blocks'][1]['text']['text'].split('\n')) == 2


def test_saturday_and_sunday_post_nothing_and_monday_posts(store: Store):
    # given
    open_cards(store, card('a', num=1, now=SAT_0930))
    d = Digest(store)

    # when
    weekend = [d.digest.run_once(SAT_0930), d.digest.run_once(SUN_1000)]
    claimed = [x['_id'] for x in store.runs.find({'_id': {'$regex': '^warn-digest:'}})]
    mon = d.digest.run_once(MON_0930)

    # then
    assert weekend == [None, None] and claimed == []
    assert mon is not None and len(d.post.posted) == 1


def test_a_start_after_the_window_skips_the_day_with_one_log_line_and_never_posts_late(store: Store, caplog):
    # given
    open_cards(store, card('a', num=1))
    d = Digest(store)
    caplog.set_level(logging.INFO, 'skopax.warndigest')

    # when
    late = [d.digest.run_once(FRI_1200), d.digest.run_once(FRI_1200 + 300), d.digest.run_once(FRI_1400)]

    # then
    assert late == [None, None, None] and d.post.posted == []
    assert len([m for m in caplog.messages if 'skipped today' in m]) == 1


@pytest.mark.parametrize('change', [{'at': ''}, {'at': '9:30'}, {'on': False}, {'autosend': False}])
def test_the_digest_is_off_with_an_empty_time_the_feature_off_or_autosend_off(store: Store, change: dict):
    # given
    open_cards(store, card('a', num=1))
    d = Digest(store, **change)

    # when
    enabled = d.digest.enabled
    out = d.digest.run_once(FRI_0930)

    # then
    assert enabled is False and out is None and d.post.posted == []


def test_the_digest_is_off_without_slack(store: Store):
    # given
    open_cards(store, card('a', num=1))
    post = FakePost()
    d = Digest(store, slack=(Slack('', '', post, FakeSlackApi()), post, None))

    # when
    out = d.digest.run_once(FRI_0930)

    # then
    assert d.digest.enabled is False and out is None and post.calls == []


def test_a_day_with_no_waiting_card_posts_nothing_and_counts_as_done(store: Store, caplog):
    # given
    open_cards(store, card('a', num=1, owner='alice@example.com'))
    d = Digest(store)
    caplog.set_level(logging.INFO, 'skopax.warndigest')

    # when
    first = d.digest.run_once(FRI_0930)
    open_cards(store, card('a', num=1))
    later = d.digest.run_once(FRI_1000)

    # then
    assert (first, later) == (None, None) and d.post.posted == []
    assert any('nothing posted' in m for m in caplog.messages)


def test_a_post_that_never_reached_slack_is_tried_again_on_the_next_pass(store: Store):
    # given
    open_cards(store, card('a', num=1))
    failure = SlackDown('Slack did not answer')
    failure.__cause__ = ConnectionRefusedError(61, 'refused')
    d = Digest(store, slack=fake_slack(post_fail=failure))

    # when
    first = d.digest.run_once(FRI_0930)
    d.post.fail = False
    again = d.digest.run_once(FRI_0930 + warndigest.PERIOD)
    later = d.digest.run_once(FRI_1130)

    # then
    assert first is None and again is not None and later is None
    assert len(d.post.posted) == 1 and len(d.post.calls) == 2


def test_a_post_that_may_have_reached_slack_is_not_tried_again_that_day(store: Store):
    # given
    open_cards(store, card('a', num=1))
    failure = SlackDown('Slack did not answer')
    failure.__cause__ = TimeoutError('read timed out')
    d = Digest(store, slack=fake_slack(post_fail=failure))

    # when
    first = d.digest.run_once(FRI_0930)
    d.post.fail = False
    again = d.digest.run_once(FRI_1000)

    # then
    assert (first, again) == (None, None) and len(d.post.calls) == 1


def test_a_refused_post_leaves_the_day_open_and_raises(store: Store):
    # given
    open_cards(store, card('a', num=1))
    d = Digest(store, slack=fake_slack(post_fail=SlackError('ratelimited', code='ratelimited')))

    # when
    with pytest.raises(SlackError):
        d.digest.run_once(FRI_0930)
    d.post.fail = False
    again = d.digest.run_once(FRI_1000)

    # then
    assert again is not None and len(d.post.posted) == 1


@pytest.mark.parametrize('code', ['not_in_channel', 'channel_not_found', 'is_archived'])
def test_a_permanent_refusal_marks_the_day_done_with_one_log_line(store: Store, caplog, code: str):
    # given
    open_cards(store, card('a', num=1))
    d = Digest(store, slack=fake_slack(post_fail=SlackError(code, code=code)))
    caplog.set_level(logging.WARNING, 'skopax.warndigest')

    # when
    outs = [d.digest.run_once(FRI_0930), d.digest.run_once(FRI_1130)]

    # then
    assert outs == [None, None] and len(d.post.calls) == 1
    assert len([m for m in caplog.messages if code in m]) == 1


def test_a_time_zone_change_during_the_day_does_not_post_twice(store: Store):
    # given
    thu_0930 = FRI_0930 - 86400
    open_cards(store, card('a', num=1, now=thu_0930))
    d = Digest(store)
    d.digest.run_once(thu_0930)

    # when
    clock.set_zone('Asia/Tokyo')
    d.at = '01:00'
    tokyo_friday = d.digest.run_once(thu_0930 + 7 * 3600)

    # then
    assert tokyo_friday is None and len(d.post.posted) == 1


def test_the_turkish_pack_words_the_digest():
    # given
    pack = json.loads(Path(warndigest.__file__).parent.joinpath('locale', 'tr', 'messages.json').read_text(encoding='utf-8'))
    messages.set_catalogs([('tr', {k: v for k, v in pack.items() if k.startswith(('warndigest.', 'tagreport.'))})])

    # when
    with messages.speaking('tr'):
        out = built([card(f'k{n}', num=n + 1) for n in range(16)])

    # then
    assert out['blocks'][0]['text']['text'].startswith(pack['warndigest.head.many'].split(' \u00b7 ')[0].format(n=16))
    assert out['blocks'][-1]['elements'][0]['text'] == pack['warndigest.more'].format(link=WARN_VIEW, n=1)


def test_every_pack_words_the_digest_with_the_same_placeholders():
    # given
    keys = [k for k in messages.MESSAGES if k.startswith('warndigest.')]
    names = {k: {f[1] for f in string.Formatter().parse(messages.MESSAGES[k]) if f[1]} for k in keys}
    packs = {lang: json.loads(Path(warndigest.__file__).parent.joinpath('locale', lang, 'messages.json').read_text(encoding='utf-8')) for lang in ('de', 'es', 'fr', 'tr')}

    # then
    assert 'warndigest' in messages.OUTBOUND and keys
    for lang, pack in packs.items():
        for k in keys:
            assert {f[1] for f in string.Formatter().parse(pack[k]) if f[1]} == names[k], (lang, k)


def test_the_digest_time_setting_is_validated_and_empty_turns_it_off(client):
    # when
    bad = client.put('/api/admin/settings', json={'key': 'slack.warn-digest-at', 'value': '9:5'}, headers=AUTH)
    off = client.put('/api/admin/settings', json={'key': 'slack.warn-digest-at', 'value': ''}, headers=AUTH)
    later = client.put('/api/admin/settings', json={'key': 'slack.warn-digest-at', 'value': '08:45'}, headers=AUTH)
    item = next(i for i in client.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == 'slack.warn-digest-at')

    # then
    assert bad.status_code == 422 and 'HH:MM' in bad.json()['detail']
    assert off.status_code == 200 and off.json()['item']['value'] == ''
    assert later.status_code == 200
    assert (item['value'], item['default'], item['group'], item['card']) == ('08:45', '09:30', 'Slack', 'Daily warnings')
    assert 'Monday to Friday' in item['help']


def test_the_digest_is_a_feature_switch_that_is_on_by_default(client):
    # when
    items = client.get('/api/admin/features', headers=AUTH).json()['items']

    # then
    item = next(i for i in items if i['id'] == 'warn-digest')
    assert (item['key'], item['on'], item['label']) == ('core.warn-digest', True, 'Daily warnings digest')
