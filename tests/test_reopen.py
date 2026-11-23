from tests.fake_slack import FakeSlackApi
from tests.test_state_notes import KEY, POST_TS, Transport, act, app, card, seed
from skopax.messages import msg
from skopax.store import Store

SYSTEM_NOTE = 'Website instance prod was switched off, so Skopax closed this card'


def closed_card(**extra) -> dict:
    return card(KEY, status='cleared', clearedAt='2026-10-26T08:30:00Z', expired=True, triage='handled', note={'text': SYSTEM_NOTE, 'at': '2026-10-26T08:30:00Z'}, **extra)


def test_reopen_drops_a_note_the_system_wrote_when_it_closed_the_card(store: Store):
    # given
    seed(store, closed_card(noteBy='system'))

    # when
    with app(store, Transport()) as client:
        act(client, 'reopen')

    # then
    doc = store.anomaly(KEY)
    assert doc.get('triage') is None and doc.get('note') is None and doc.get('noteBy') is None


def test_reopen_drops_an_old_close_note_that_has_no_author_but_the_known_text(store: Store):
    # given
    seed(store, closed_card())

    # when
    with app(store, Transport()) as client:
        act(client, 'reopen')

    # then
    assert store.anomaly(KEY).get('note') is None


def test_reopen_keeps_a_note_a_person_wrote_and_the_investigators_analysis(store: Store):
    # given
    person = card(KEY, triage='handled', note={'text': 'Checked the logs, the cache was cold.', 'at': '2026-10-26T08:30:00Z'}, expired=True)
    seed(store, person)

    # when
    with app(store, Transport()) as client:
        act(client, 'reopen')
        store.anomalies.update_one({'_id': KEY}, {'$set': {'triage': 'handled', 'note': {'text': SYSTEM_NOTE, 'at': '2026-10-26T08:30:00Z'}, 'noteBy': 'pod'}})
        act(client, 'reopen')

    # then
    assert store.anomaly(KEY)['note']['text'] == SYSTEM_NOTE and store.anomaly(KEY)['noteBy'] == 'pod'


def test_reopen_tells_the_slack_thread_once_and_redraws_the_head(store: Store):
    # given
    seed(store, card(KEY, triage='handled', num=349))
    store.slack_post_head(KEY, POST_TS, {'kind': 'resolved', 'by': 'alice@example.com', 'at': '2026-10-26T08:10:00Z'})
    transport, api = Transport(), FakeSlackApi()

    # when
    with app(store, transport, api) as client:
        act(client, 'reopen')
        act(client, 'reopen')
        client.app.state.card_posts.drain(5)

    # then
    replies = transport.replies()
    assert replies == [(POST_TS, msg('notes.state.reopened', who='bootstrap', card='#349'))] and 'reopened #349' in replies[0][1]
    updates = [p for m, p in api.calls if m == 'chat.update']
    assert updates and updates[-1]['blocks'][0]['text']['text'].startswith(':large_yellow_circle:') and ':white_check_mark:' not in updates[-1]['blocks'][0]['text']['text']


def test_reopen_of_a_card_without_a_post_or_that_was_not_closed_sends_nothing(store: Store):
    # given
    seed(store, card(KEY, ts=None, triage='handled'), card('log|b|ERROR|x', status='open'))
    transport = Transport()

    # when
    with app(store, transport) as client:
        act(client, 'reopen')
        act(client, 'reopen', 'log|b|ERROR|x')

    # then
    assert transport.replies() == []


def test_the_reopen_note_reads_the_same_in_every_language():
    # given
    import json
    from pathlib import Path
    root = Path(__file__).parent.parent / 'skopax' / 'locale'

    # then
    assert msg('notes.state.reopened', who='alice', card='#349') == '\U0001F513 alice reopened #349'
    for lang in ('tr', 'de', 'es', 'fr'):
        pack = json.loads((root / lang / 'messages.json').read_text(encoding='utf-8'))
        assert '{who}' in pack['notes.state.reopened'] and '{card}' in pack['notes.state.reopened']
    assert json.loads((root / 'tr' / 'messages.json').read_text(encoding='utf-8'))['notes.state.reopened'] == '\U0001F513 {who}, {card} kart\u0131n\u0131 yeniden a\u00e7t\u0131'


def test_reopen_tells_the_thread_for_known_noise_and_an_expired_card_but_not_for_an_in_progress_or_muted_one(store: Store):
    # given
    known, expired, taken, muted = 'log|k|ERROR|x', 'log|e|ERROR|x', 'log|t|ERROR|x', 'log|m|ERROR|x'
    seed(store, card(known, '1792224000.000111', num=1), card(expired, '1792224000.000222', num=2, status='cleared', expired=True), card(taken, '1792224000.000333', num=3, triage='progress'), card(muted, '1792224000.000444', num=4))
    transport = Transport()

    # when
    with app(store, transport) as client:
        act(client, 'known', known)
        act(client, 'mute', muted, hours=6)
        for key in (known, expired, taken, muted):
            act(client, 'reopen', key)

    # then
    assert sorted(text for _, text in transport.replies() if 'reopened' in text) == sorted([msg('notes.state.reopened', who='bootstrap', card='#1'), msg('notes.state.reopened', who='bootstrap', card='#2')])
