import pytest

from tests.conftest import anomaly, state
from tests.fake_slack import fake_slack
from tests.plugin_kit import prom_tools
from tests.teams_kit import MARKETING, SEO, TECH, Transport, open_card, route, routes_of, tag
from tests.test_routes import NOW, SAID
from tests.test_flagged_report_and_fix import PLANTED
from tests.test_investigator_tools import FakeProm, box
from tests.test_slack_layout import BASE, posted, rewrites
from skopax import locales, messages
from skopax.actors import POD_BY
from skopax.autosend import fallback_text
from skopax.cardposts import CardPosts
from skopax.engine import iso as epoch
from skopax.investigator.tools import draft_problem
from skopax.store import Store

KEY = 'log|checkout-service|ERROR|boom'
TS = '1792224000.000200'
NOTE = 'The checkout link in the campaign points to a removed page. Visitors land on a 404 and leave. The old path was dropped in the last release.'
PLUGINS = {'teams': 'Microsoft Teams', 'email': 'Email route'}


def seed(store: Store, post: dict | None = None, **extra) -> CardPosts:
    store.save(state(anomaly(KEY, num=16, **extra)), None)
    posted(store, KEY, TS, **(post or {}))
    return copy(store)


def copy(store: Store) -> CardPosts:
    slack, _, api = fake_slack()
    posts = CardPosts(store, slack, BASE, autostart=False, plugin_name=lambda p: PLUGINS.get(p, p),
                      check=lambda: lambda text: draft_problem(text, ('example.com',)))
    posts.api = api
    return posts


def run(posts: CardPosts) -> list[dict]:
    posts.reconcile()
    posts.run_batch()
    return rewrites(posts.api)


def body_of(update: dict) -> str:
    return update['blocks'][2]['text']['text']


def stored_body(store: Store) -> dict | None:
    return store.anomaly(KEY)['slackPost'].get('body')


def delivered(store: Store, route: str, plugin: str = 'teams', **extra) -> None:
    store.teams_posted(KEY, {'route': route, 'plugin': plugin, 'at': '2026-11-11T07:05:00Z', 'by': 'server', 'ok': True, **extra})


@pytest.fixture
def speak():
    def use(lang: str) -> None:
        messages.set_catalogs([(pack.lang, dict(pack.messages)) for pack in locales.packs()])
        messages.use_language(lambda: lang)
    yield use
    messages.reset()


def test_an_echo_post_with_no_body_gets_the_headline_and_summary_of_the_draft_in_one_update(store: Store):
    # given
    posts = seed(store)
    store.pod_slack_draft(KEY, 'The campaign link points to a removed page.', POD_BY, headline='Broken campaign link')
    posts.api.calls.clear()

    # when
    sent = run(posts)

    # then
    assert len(sent) == 1 and sent[0]['ts'] == TS
    assert body_of(sent[0]) == '*Broken campaign link*\nThe campaign link points to a removed page.'
    assert not [m for m, _ in posts.api.calls if m == 'chat.postMessage']
    assert stored_body(store) == {'text': 'The campaign link points to a removed page.', 'headline': 'Broken campaign link', 'by': POD_BY, 'filled': True}


def test_with_no_draft_the_first_sentence_of_the_investigator_note_is_the_headline_and_the_next_ones_the_summary(store: Store):
    # given
    posts = seed(store)
    store.note(KEY, NOTE, by='pod')

    # when
    sent = run(posts)

    # then
    assert body_of(sent[0]) == '*The checkout link in the campaign points to a removed page.*\nVisitors land on a 404 and leave. The old path was dropped in the last release.'


def test_a_one_sentence_note_is_the_summary_with_no_headline(store: Store):
    # given
    posts = seed(store)
    store.note(KEY, 'The checkout link points to a removed page.', by='pod')

    # when
    sent = run(posts)

    # then
    assert body_of(sent[0]) == 'The checkout link points to a removed page.'


def test_sentences_are_not_split_after_e_g_or_a_list_number(store: Store):
    # given
    posts = seed(store)
    store.note(KEY, 'Old links break, e.g. The spring sale page. Steps: 3. Redirect it. Then tell the team.', by='pod')

    # when
    sent = run(posts)

    # then
    assert body_of(sent[0]) == '*Old links break, e.g. The spring sale page.*\nSteps: 3. Redirect it. Then tell the team.'


def test_the_recommendation_and_the_routes_that_delivered_follow_the_summary(store: Store):
    # given
    posts = seed(store)
    store.note(KEY, NOTE, by='pod')
    store.assess(KEY, [], 'Marketing should update the ad link.\nAdd a 301 redirect too.', None, by='pod')
    for route, plugin in (('Marketing', 'teams'), ('SEO', 'teams'), ('Marketing', 'teams'), ('Ops', 'email')):
        delivered(store, route, plugin)

    # when
    sent = run(posts)

    # then
    lines = body_of(sent[0]).split('\n')
    assert lines[2:] == ['Recommended: Marketing should update the ad link.', 'Notified: Marketing, SEO (Microsoft Teams); Ops (Email route)']
    assert 'Notified' not in stored_body(store)['text']


def test_the_turkish_lines_are_exact(store: Store, speak):
    # given
    speak('tr')
    posts = seed(store)
    store.note(KEY, NOTE, by='pod')
    store.assess(KEY, [], 'Pazarlama kampanya linkini g\u00fcncellemeli.', None, by='pod')
    delivered(store, 'Pazarlama')

    # when
    sent = run(posts)

    # then
    assert body_of(sent[0]).split('\n')[2:] == ['\u00d6neri: Pazarlama kampanya linkini g\u00fcncellemeli.', 'Bildirildi: Pazarlama (Microsoft Teams)']


def test_no_recommendation_and_no_delivery_leave_both_lines_out(store: Store):
    # given
    posts = seed(store)
    store.note(KEY, NOTE, by='pod')
    store.assess(KEY, [], 'A person wrote this.', None, by='alice@example.com')
    delivered(store, 'Marketing', fixed=True)

    # when
    sent = run(posts)

    # then
    assert body_of(sent[0]).count('\n') == 1


def test_a_route_claimed_and_not_delivered_is_not_listed_and_a_later_delivery_appears_on_the_next_redraw(store: Store):
    # given
    posts = seed(store)
    posts.clock = lambda: NOW
    store.note(KEY, NOTE, by='pod')
    store.teams_posted(KEY, {'route': 'SEO', 'plugin': 'teams', 'at': epoch(NOW - 60), 'by': 'server'})
    first = run(posts)

    # when
    delivered(store, 'Marketing')
    sent = run(posts)
    again = run(posts)

    # then
    assert 'Notified' not in body_of(first[0])
    assert len(sent) == 2 and body_of(sent[1]).split('\n')[-1] == 'Notified: Marketing (Microsoft Teams)'
    assert len(again) == 2


def test_a_post_with_body_text_is_not_changed(store: Store):
    # given
    posts = seed(store, {'body': {'text': 'What the investigator posted.', 'by': POD_BY}})
    store.note(KEY, NOTE, by='pod')

    # when
    sent = run(posts)

    # then
    assert sent == [] and stored_body(store) == {'text': 'What the investigator posted.', 'by': POD_BY}


def test_a_whitespace_only_body_is_filled(store: Store):
    # given
    posts = seed(store, {'body': {'text': '  \n ', 'by': POD_BY}})
    store.note(KEY, NOTE, by='pod')

    # when
    sent = run(posts)

    # then
    assert len(sent) == 1 and stored_body(store)['filled'] is True


def test_the_real_fallback_numbers_body_is_replaced_and_its_thread_line_goes(store: Store):
    # given
    card = anomaly(KEY, num=16, now=12, d1=3, d7=4, firstSeen='2026-11-11T07:00:00Z')
    numbers = fallback_text(card, '2026-11-11T07:00:00Z')
    posts = seed(store, {'body': {'text': numbers, 'by': 'server'}}, now=12, d1=3, d7=4, firstSeen='2026-11-11T07:00:00Z')
    store.note(KEY, NOTE, by='pod')

    # when
    sent = run(posts)

    # then
    last = messages.msg('autosend.fallback.last').strip()
    assert last and last in numbers
    assert len(sent) == 1 and last not in body_of(sent[0]) and body_of(sent[0]).startswith('*The checkout link')
    assert stored_body(store)['by'] == POD_BY


def test_a_reconcile_after_the_fill_sends_nothing(store: Store):
    # given
    posts = seed(store)
    store.note(KEY, NOTE, by='pod')
    run(posts)

    # when
    sent = run(posts)

    # then
    assert len(sent) == 1


def test_a_failed_store_of_the_body_leaves_the_post_unfilled_so_the_next_pass_tries_once_more(store: Store, monkeypatch):
    # given
    posts = seed(store)
    store.note(KEY, NOTE, by='pod')
    real = store.slack_post_body
    tries = []

    def flaky(*args, **kw):
        tries.append(1)
        if len(tries) == 1:
            raise RuntimeError('down')
        return real(*args, **kw)
    monkeypatch.setattr(store, 'slack_post_body', flaky)

    # when
    first = len(run(posts))
    unfilled = store.anomaly(KEY)['slackPost']['drawn'].get('filled')
    second = len(run(posts))
    third = len(run(posts))

    # then
    assert (first, second, third) == (1, 2, 2) and unfilled is None
    assert store.anomaly(KEY)['slackPost']['drawn']['filled'] is True and stored_body(store)['filled'] is True


def test_a_route_stamp_from_before_the_delivered_mark_counts_once_it_is_ten_minutes_old(store: Store):
    # given
    posts = seed(store)
    posts.clock = lambda: NOW
    store.note(KEY, NOTE, by='pod')
    store.teams_posted(KEY, {'route': 'Marketing', 'plugin': 'teams', 'at': epoch(NOW - 3600), 'by': 'server'})
    store.teams_posted(KEY, {'route': 'SEO', 'plugin': 'teams', 'at': epoch(NOW - 60), 'by': 'server'})

    # when
    sent = run(posts)

    # then
    assert body_of(sent[0]).split('\n')[-1] == 'Notified: Marketing (Microsoft Teams)'


def test_the_link_rules_are_read_once_per_pass_not_per_card(store: Store):
    # given
    other = 'log|checkout-service|ERROR|other'
    store.save(state(anomaly(KEY, num=16), anomaly(other, num=17)), None)
    posted(store, KEY, TS)
    posted(store, other, '1792224000.000400')
    store.note(KEY, NOTE, by='pod')
    store.note(other, NOTE, by='pod')
    reads = []
    slack, _, api = fake_slack()
    posts = CardPosts(store, slack, BASE, autostart=False, check=lambda: reads.append(1) or (lambda text: None))

    # when
    posts.reconcile()
    posts.run_batch()

    # then
    assert len(rewrites(api)) == 2 and len(reads) == 1


def test_two_copies_filling_the_same_post_leave_the_stored_body_as_slack_shows_it(store: Store):
    # given
    one = seed(store)
    other = copy(store)
    store.note(KEY, NOTE, by='pod')
    real = one.slack.update
    raced = []

    def racing(*args, **kw):
        if not raced:
            raced.append(1)
            other.reconcile()
            other.run_batch()
        return real(*args, **kw)
    one.slack.update = racing

    # when
    run(one)

    # then
    shown = rewrites(other.api) + rewrites(one.api)
    stored = stored_body(store)
    assert all(body_of(u) == f"*{stored['headline']}*\n{stored['text']}" for u in shown)
    run(one)
    run(other)
    assert len(rewrites(one.api)) + len(rewrites(other.api)) == len(shown)


def test_a_later_redraw_keeps_the_filled_body(store: Store):
    # given
    posts = seed(store)
    store.note(KEY, NOTE, by='pod')
    run(posts)

    # when
    posts.refresh(KEY)
    posts.run_batch()

    # then
    sent = rewrites(posts.api)
    assert len(sent) == 2 and body_of(sent[1]) == body_of(sent[0])


def test_the_summary_is_escaped_and_cut_to_the_draft_limit(store: Store):
    # given
    posts = seed(store)
    store.pod_slack_draft(KEY, 'a <b> & ' + 'x' * 3000, POD_BY, headline='<!channel> *loud* & clear')

    # when
    sent = run(posts)

    # then
    text = body_of(sent[0])
    assert text.startswith('*\u2039!channel\u203a loud &amp; clear*\na &lt;b&gt; &amp; x')
    assert '<!channel>' not in text and len(stored_body(store)['text']) <= 1500


def test_a_human_note_or_a_held_draft_does_not_fill_the_body(store: Store):
    # given
    posts = seed(store)
    store.note(KEY, NOTE, by='alice@example.com')
    store.pod_slack_draft(KEY, 'Planted words.', POD_BY, hold='planted text in the web answer (ignore)')

    # when
    sent = run(posts)

    # then
    assert sent == [] and stored_body(store) is None


def test_a_server_draft_does_not_fill_the_body(store: Store):
    # given
    posts = seed(store)
    store.pod_slack_draft(KEY, 'checkout-service errors at 12, usually 3.', 'server')

    # when
    sent = run(posts)

    # then
    assert sent == [] and stored_body(store) is None


def test_a_note_or_recommendation_that_fails_the_draft_checks_is_skipped(store: Store):
    # given
    posts = seed(store)
    store.note(KEY, 'Visitors are sent to https://evil.example/login now. Fix it.', by='pod')
    first = run(posts)
    store.note(KEY, NOTE, by='pod')
    store.assess(KEY, [], 'Ping <@U123> about it.', None, by='pod')

    # when
    sent = run(posts)

    # then
    assert first == []
    assert len(sent) == 1 and 'Recommended' not in body_of(sent[0])


def test_a_run_that_read_planted_text_fills_nothing_and_a_later_clean_note_fills_the_post(store: Store):
    # given
    store.save(state(anomaly(KEY, num=16)), None)
    posted(store, KEY, TS)
    posts = copy(store)
    flagged = box(store, keys=(KEY,), plugin_tools=prom_tools(FakeProm(PLANTED)))
    flagged.call('promql', {'expr': 'up'})
    flagged.call('note', {'key': KEY, 'text': NOTE})
    flagged.call('assess', {'key': KEY, 'suspects': [], 'recommend': 'Marketing should update the ad link.'})
    card = store.anomaly(KEY)
    held = run(posts)

    # when
    box(store, keys=(KEY,)).call('note', {'key': KEY, 'text': NOTE})
    sent = run(posts)

    # then
    assert card['noteBy'] == 'pod' and card['noteFlagged'] is True and card['recommendFlagged'] is True
    assert held == [] and 'noteFlagged' not in store.anomaly(KEY)
    assert len(sent) == 1 and 'Recommended' not in body_of(sent[0])


def test_a_thread_copy_in_another_cards_thread_is_left_alone(store: Store):
    # given
    posts = seed(store, {'threadOf': 'log|checkout-service|ERROR|other', 'replyTs': '1792224000.000300'})
    store.note(KEY, NOTE, by='pod')

    # when
    sent = run(posts)

    # then
    assert sent == [] and stored_body(store) is None


def test_a_tagged_route_marks_its_stamp_delivered_and_a_failed_one_leaves_no_stamp(store: Store):
    # given
    store.save(state(open_card(KEY, tags=tag('seo'), business=SAID), open_card('log|a|ERROR|other', tags=tag('marketing'), business=SAID)), None)
    routes = routes_of(store, Transport(fail='sig=SECRET'), route('seo', SEO, tags='seo'), route('marketing', MARKETING, tags='marketing'))

    # when
    routes.tagged(store.load()['anomalies'], now=NOW)

    # then
    assert [(p['route'], p.get('ok')) for p in store.anomaly(KEY)['teamsPosts']] == [('seo', True)]
    assert not store.anomaly('log|a|ERROR|other').get('teamsPosts')


def test_a_key_route_stamps_its_post_delivered(store: Store):
    # given
    store.save(state(open_card(KEY, 'crit')), None)
    routes = routes_of(store, Transport(), route('tech', TECH, keys='*'))

    # when
    routes.push(store.load()['anomalies'], now=NOW)

    # then
    assert [(p['route'], p.get('ok')) for p in store.anomaly(KEY)['teamsPosts']] == [('tech', True)]
