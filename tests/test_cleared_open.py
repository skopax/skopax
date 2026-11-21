import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, state
from tests.fake_slack import fake_slack
from tests.test_slack_layout import CARD, CHAT, LINK, KEY, CARD_TS, bar_of, head_of, posted, posts_of, rewrites, texts
from skopax import actors, brand
from skopax.app import create_app
from skopax.engine import Applied
from skopax.messages import MESSAGES, msg
from skopax.slack import anomaly_message
from skopax.store import Store
from tests.test_ai_provider import on_openai, settings as base_settings

LOCALES = Path(__file__).parent.parent / 'skopax' / 'locale'
NEW_KEYS = ('slack.head.cleared', 'slack.status.cleared', 'slack.status.event', 'slack.cleared', 'slack.head.auto', 'slack.head.auto_first', 'slack.actor.investigator', 'clear.cleared', 'clear.plugin_note')


def test_a_cleared_card_is_still_open_and_never_wears_the_done_mark():
    # when
    cleared = anomaly_message(CARD | {'status': 'cleared'}, LINK, 'server', head={'kind': 'cleared'})
    cleared_progress = anomaly_message(CARD | {'status': 'cleared', 'triage': 'progress'}, LINK, 'server', head={'kind': 'took', 'by': 'alice@example.com'})

    # then
    assert head_of(cleared) == ':eyes: #14 stopped firing \u00b7 still open'
    assert head_of(cleared_progress).startswith(':eyes: ')
    assert bar_of(cleared)[0].startswith('Stopped firing \u00b7 still open')
    assert ':white_check_mark:' not in ''.join(texts(cleared)) and ':large_green_circle:' not in ''.join(texts(cleared))
    assert cleared['text'].startswith('Stopped firing \u00b7 still open')


def test_only_a_closed_card_wears_the_done_mark():
    # when
    marks = {
        'open': head_of(anomaly_message(CARD, LINK, 'server', head={'kind': 'reopened', 'by': 'system'})),
        'progress': head_of(anomaly_message(CARD | {'triage': 'progress'}, LINK, 'server', head={'kind': 'took', 'by': 'alice@example.com'})),
        'cleared': head_of(anomaly_message(CARD | {'status': 'cleared'}, LINK, 'server', head={'kind': 'cleared'})),
        'handled': head_of(anomaly_message(CARD | {'triage': 'handled'}, LINK, 'server', head={'kind': 'resolved', 'by': 'alice@example.com'})),
        'cleared_handled': head_of(anomaly_message(CARD | {'status': 'cleared', 'triage': 'handled'}, LINK, 'server', head={'kind': 'resolved', 'by': 'alice@example.com'})),
        'known': head_of(anomaly_message(CARD, LINK, 'server', head={'kind': 'known', 'by': 'alice@example.com'}, mute='known')),
        'cleared_known': head_of(anomaly_message(CARD | {'status': 'cleared'}, LINK, 'server', head={'kind': 'known', 'by': 'alice@example.com'}, mute='known')),
        'muted': head_of(anomaly_message(CARD | {'status': 'cleared'}, LINK, 'server', head={'kind': 'muted', 'by': 'alice@example.com'}, mute='mute')),
    }

    # then
    assert [k for k, v in marks.items() if v.startswith(':white_check_mark:')] == ['handled', 'cleared_handled', 'known', 'cleared_known']
    assert marks['muted'].startswith(':eyes:')
    assert marks['open'].startswith(':large_orange_circle:') and marks['progress'].startswith(':large_orange_circle:')


def test_an_event_stop_says_it_is_still_open_in_the_bar_and_the_notification_text():
    # given
    event = CARD | {'status': 'cleared', 'cause': {'kind': 'event', 'what': 'a rollout'}}

    # when
    message = anomaly_message(event, LINK, 'server', head={'kind': 'cleared'})

    # then
    assert 'still open' in msg('slack.status.event') and 'still open' in msg('slack.cleared')
    assert ':white_check_mark:' not in head_of(message)


def test_the_auto_resolve_sweep_closes_the_head_and_says_what_closed_it(store: Store):
    # given
    store.save(state(anomaly_card('log|d|ERROR|x', 17), anomaly_card('log|e|ERROR|x', 18)), None)
    for key, ts in (('log|d|ERROR|x', '1792224000.000203'), ('log|e|ERROR|x', '1792224000.000204')):
        posted(store, key, ts)
    slack, _, api = fake_slack()
    posts = posts_of(store, slack)

    # when
    posts.swept(Applied(rev=1, events=[], push=[], resolved=[{'key': 'log|d|ERROR|x', 'hours': 4, 'businessTime': True}, {'key': 'log|e|ERROR|x', 'hours': 48, 'since': 'first'}]))
    posts.run_batch()

    # then
    assert [head_of(u) for u in rewrites(api)] == [':white_check_mark: #17 closed automatically: stopped firing for 4 business hours',
                                                  ':white_check_mark: #18 closed automatically: nobody acted in 2 days']
    assert store.anomaly('log|d|ERROR|x')['slackPost']['head']['kind'] == 'auto'


def anomaly_card(key: str, num: int) -> dict:
    from tests.conftest import anomaly
    return anomaly(key, num=num, triage='handled', triagedBy='auto', autoResolvedAt='2026-11-12T07:00:00Z')


@pytest.mark.parametrize('lang', ['de', 'es', 'fr', 'tr'])
def test_every_pack_translates_the_new_texts_and_no_pack_calls_a_cleared_card_recovered(lang):
    # given
    pack = json.loads((LOCALES / lang / 'messages.json').read_text(encoding='utf-8'))

    # then
    assert all(k in pack for k in NEW_KEYS)
    assert pack['slack.status.cleared'] != MESSAGES['slack.status.cleared'] and pack['slack.head.cleared'] != MESSAGES['slack.head.cleared']
    assert '\U0001F7E2' not in pack['clear.cleared'] and '\U0001F7E2' not in pack['clear.plugin_note']


def test_the_turkish_texts_are_the_ones_the_owner_asked_for():
    # given
    pack = json.loads((LOCALES / 'tr' / 'messages.json').read_text(encoding='utf-8'))

    # then
    assert pack['slack.head.cleared'] == '{card} sinyali durdu \u00b7 kart a\u00e7\u0131k'
    assert pack['slack.status.cleared'] == 'Sinyal durdu \u00b7 kart a\u00e7\u0131k'


def test_the_cleared_thread_notes_carry_no_green_mark():
    # then
    assert '\U0001F7E2' not in MESSAGES['clear.cleared'] and '\U0001F7E2' not in MESSAGES['clear.plugin_note']
    assert 'Back to normal' not in MESSAGES['clear.cleared']


@pytest.fixture(autouse=True)
def investigator_model():
    yield
    actors.use_model('claude-opus-5-5')


def test_the_investigator_actor_never_shows_a_pod_label():
    # given
    stored = actors.POD_BY

    # when
    actors.use_model('claude-opus-5-5')
    claude = actors.shown(stored)
    actors.use_model('gpt-6.2-astra')
    other = actors.shown(stored)

    # then
    assert stored == 'Claude (pod)'
    assert (claude, other) == ('Claude investigator', 'Investigator')
    assert actors.shown('alice@example.com') == 'alice@example.com'


def test_the_label_follows_the_live_provider_and_model_without_a_restart(store: Store):
    # given
    c = TestClient(create_app(dataclasses.replace(base_settings(), anthropic_api_key='test-anthropic-key', openai_api_key='test-key', investigate_mode='shadow'), store))
    before = (actors.investigator_label(), brand.page_info()['investigator'], brand.page_info()['podBy'])

    # when
    put = c.put('/api/admin/settings', json={'key': 'investigate.provider', 'value': 'openai'}, headers=AUTH)
    c.app.state.live.refresh()
    after = (actors.investigator_label(), brand.page_info()['investigator'])
    back = c.put('/api/admin/settings', json={'key': 'investigate.provider', 'value': 'anthropic'}, headers=AUTH)
    c.app.state.live.refresh()

    # then
    assert put.status_code == 200 and back.status_code == 200
    assert before == ('Claude investigator', 'Claude investigator', 'Claude (pod)')
    assert after == ('Investigator', 'Investigator')
    assert actors.investigator_label() == 'Claude investigator'


def test_openai_with_a_claude_model_set_still_reads_investigator(store: Store):
    # when
    TestClient(create_app(on_openai(investigate_model='claude-opus-5'), store))

    # then
    assert actors.investigator_label() == 'Investigator'


def test_a_card_shared_by_the_investigator_names_it_and_not_the_pod():
    # when
    message = anomaly_message(CARD, LINK, actors.POD_BY, head={'kind': 'shared', 'by': actors.POD_BY})

    # then
    assert '(pod)' not in ' '.join(texts(message)) and head_of(message).startswith(':large_orange_circle: Claude investigator shared a card')


def test_history_notices_and_mcp_texts_show_the_investigator_label():
    # given
    from skopax import history, notices

    # then
    assert history.who_text(actors.POD_BY) == actors.investigator_label()
    assert '(pod)' not in notices.who_text(actors.POD_BY) and notices.who_text(actors.POD_BY) == actors.investigator_label()


def test_a_cleared_open_card_never_says_fixed_in_a_route_post():
    # given
    from skopax.plugins.teams.cards import fixed_rows

    # when
    rows = fixed_rows({'text': 'Add a redirect.', 'link': 'https://skopax.example.com/c/1'})
    text = json.dumps(rows, ensure_ascii=False)

    # then
    assert '\u2705' not in text and 'Fixed' not in text and text.count('\U0001F440') == 1 and 'Stopped firing, still open' in text
    assert 'fixed' not in msg('routes.title_fixed', product='Skopax', label='SEO') and 'still open' in msg('routes.title_fixed', product='Skopax', label='SEO')
    assert msg('reminder.calmed') == 'Stopped firing but still open.'


@pytest.mark.parametrize('lang', ['de', 'es', 'fr', 'tr'])
def test_the_route_and_reminder_texts_of_every_pack_no_longer_say_fixed_or_back_to_normal(lang):
    # given
    pack = json.loads((LOCALES / lang / 'messages.json').read_text(encoding='utf-8'))

    # then
    for key in ('routes.fixed', 'routes.title_fixed', 'reminder.calmed'):
        assert pack[key] != MESSAGES[key]
    assert pack['routes.fixed'] != {'de': 'Behoben', 'es': 'Corregida', 'fr': 'Corrig\u00e9e', 'tr': 'D\u00fczeltildi'}[lang]


def test_nothing_a_person_reads_shows_the_pod_label_for_the_investigator_actor():
    # given
    from skopax import history, notices
    from skopax.mcpserver import Tools
    from skopax.slack import facts_line, sender, actor
    from skopax.statenotes import who_of

    bridge = SimpleNamespace(link=lambda v: 'https://skopax.example.com/c/1', site=lambda: 'https://skopax.example.com')
    shown = [
        ' '.join(texts(anomaly_message(CARD, LINK, actors.POD_BY, head={'kind': 'shared', 'by': actors.POD_BY}))),
        facts_line(CARD, None, sender(actors.POD_BY)),
        actor(actors.POD_BY), sender(actors.POD_BY), history.who_text(actors.POD_BY), notices.who_text(actors.POD_BY), who_of(actors.POD_BY),
        Tools.chat_text(bridge, {'id': 'c1', 'title': 't', 'mine': False, 'by': actors.POD_BY, 'messages': []}),
    ]

    # then
    assert all('(pod)' not in text for text in shown), shown
