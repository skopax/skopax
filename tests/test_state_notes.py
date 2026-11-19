import dataclasses
import re
import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.fake_slack import FakeSlackApi
from tests.plugin_kit import idle_jira, serve_jira
from tests.test_investigator_tools import sources
from tests.test_jiralinks import FakeJira, closed, issue
from skopax import locales, messages, statenotes
from skopax.app import create_app
from skopax.config import load_settings
from skopax.engine import iso
from skopax.investigator.tools import Effects, Toolbox
from skopax.slack import Slack, SlackDown
from skopax.statenotes import StateNotes
from skopax.store import Store

KEY = 'log|checkout-service|ERROR|boom'
KID = 'log|checkout-service|ERROR|kid'
QUIET = 'log|payments-api|ERROR|quiet'
POST_TS = '1792224000.000100'
KID_TS = '1792224000.000200'
JIRA = 'https://jira.example.com/browse/'
DAY = 86400


class Transport:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.calls: list[dict] = []

    def __call__(self, token: str, payload: dict) -> dict:
        self.calls.append(payload)
        if self.fail and payload.get('thread_ts'):
            raise self.fail
        return {'ok': True, 'channel': 'C9', 'ts': f'1792224001.{len(self.calls):06d}'}

    def replies(self) -> list[tuple[str, str]]:
        return [(p['thread_ts'], p['text']) for p in self.calls if p.get('thread_ts')]


def card(key: str, ts: str | None = POST_TS, **extra) -> dict:
    post = {'slackPost': {'channel': 'C9', 'ts': ts, 'at': '2026-10-26T08:00:00Z', 'by': 'server', 'draft': False}} if ts else {}
    return {'key': key, 'sev': 'warn', 'status': 'open', 'svc': 'checkout-service', 'label': 'ERROR pattern', 'src': 'logs', 'firstSeen': '2026-10-26T07:00:00Z', **post, **extra}


def seed(store: Store, *cards: dict) -> Store:
    store.save({'initialized': True, 'anomalies': list(cards) or [card(KEY)], 'sources': {'logs': 'ok'}, 'sweptIso': '2026-10-26T08:00:00Z', 'muted': []}, None)
    return store


def app(store: Store, transport: Transport, api: FakeSlackApi | None = None, jira=None, **over) -> TestClient:
    s = dataclasses.replace(settings(), **over) if over else settings()
    serve_jira(store, s)
    return TestClient(create_app(s, store, slack=Slack('xoxb-test', '#ops', transport, api or FakeSlackApi()), jira=jira or idle_jira()))


def act(client: TestClient, kind: str, key: str = KEY, **extra) -> dict:
    res = client.post('/api/actions', json={'type': kind, 'key': key, **extra}, headers=AUTH)
    assert res.status_code == 201, res.text
    return res.json()


def link(ticket: str) -> str:
    return f'<{JIRA}{ticket}|{ticket}>'


@pytest.mark.parametrize('kind, extra, text', [
    ('resolve', {'reason': 'disk cleaned'}, '✅ Resolved · from the page · bootstrap · disk cleaned'),
    ('resolve', {}, '✅ Resolved · from the page · bootstrap'),
    ('resolve', {'ticket': 'OPS-138'}, f"✅ Resolved · from the page · bootstrap · {link('OPS-138')}"),
    ('take', {'ticket': 'OPS-138'}, f"🛠️ In progress: bootstrap · {link('OPS-138')}"),
    ('take', {}, '🛠️ In progress: bootstrap'),
    ('known', {}, '🔕 Marked as known noise: bootstrap'),
    ('mute', {'hours': 6}, '🔕 Muted for 6 h: bootstrap'),
    ('mute', {}, '🔕 Muted for 24 h: bootstrap'),
])
def test_a_page_state_change_replies_once_in_the_cards_thread(store: Store, kind: str, extra: dict, text: str):
    # given
    seed(store)
    transport = Transport()

    # when
    with app(store, transport) as client:
        done = act(client, kind, **extra)
        post = client.get('/api/state').json()['anomalies'][0]['slackPost']

    # then
    assert done['status'] == 'done'
    assert transport.replies() == [(POST_TS, text)]
    assert transport.calls[0]['channel'] == 'C9'
    note = post['stateNote']
    assert {k: note[k] for k in ('text', 'by', 'ts', 'permalink')} == {'text': re.sub(r'<[^|>]+\|([^>]+)>', r'\1', text), 'by': 'bootstrap', 'ts': '1792224001.000001',
                                                                      'permalink': 'https://example.slack.com/archives/C9/p1792224001000001'}
    assert note['state'] == {'resolve': 'handled', 'take': 'progress', 'known': 'known', 'mute': 'muted'}[kind]
    assert note['at'].endswith('Z')


def test_a_note_in_a_thread_several_cards_share_names_its_card(store: Store):
    # given
    other = 'page-typos|shop.example.com/products/chairs'
    seed(store, card(KEY), card(other, svc='shop.example.com/products/chairs', label='Typos'), card(QUIET, ts=KID_TS))
    transport = Transport()

    # when
    with app(store, transport) as client:
        act(client, 'resolve', key=other)
        act(client, 'resolve', key=QUIET)

    # then
    assert transport.replies() == [(POST_TS, '*shop.example.com/products/chairs / Typos* · ✅ Resolved · from the page · bootstrap'), (KID_TS, '✅ Resolved · from the page · bootstrap')]


def test_the_slack_button_names_the_operator_by_login(store: Store):
    # given
    seed(store)
    transport = Transport()

    # when
    with app(store, transport) as client:
        result = client.app.state.clicks.apply({'type': 'take', 'key': KEY, 'target': KEY, 'requestedBy': 'adam.baker@example.com', 'source': 'slack'})

    # then
    assert result == 'In progress · adam.baker@example.com'
    assert transport.replies() == [(POST_TS, '🛠️ In progress: adam.baker')]
    assert store.anomaly(KEY)['owner'] == 'adam.baker@example.com'


def test_a_take_from_an_offer_records_and_names_the_speaker_not_the_clicker(store: Store):
    # given
    seed(store)
    transport = Transport()

    # when
    with app(store, transport) as client:
        result = client.app.state.clicks.apply({'type': 'take', 'key': KEY, 'target': KEY, 'requestedBy': 'adam.baker@example.com', 'owner': 'gary.cole@example.com', 'source': 'slack'})

    # then
    assert result == 'In progress · gary.cole@example.com'
    assert transport.replies() == [(POST_TS, '🛠️ In progress: gary.cole')]
    card = store.anomaly(KEY)
    assert (card['triage'], card['owner']) == ('progress', 'gary.cole@example.com')
    assert card['slackPost']['stateNote']['by'] == 'gary.cole'


def test_a_reason_can_never_mention_anyone(store: Store):
    # given
    seed(store)
    transport = Transport()

    # when
    with app(store, transport) as client:
        act(client, 'resolve', reason='<!channel> <@U0123ABCD> done & ok')

    # then
    text = transport.replies()[0][1]
    assert text == '✅ Resolved · from the page · bootstrap · &lt;!channel&gt; &lt;@U0123ABCD&gt; done &amp; ok'


def test_no_repeat_when_the_state_did_not_change(store: Store):
    # given
    seed(store)
    transport = Transport()

    # when
    with app(store, transport) as client:
        act(client, 'resolve')
        act(client, 'resolve', reason='again')
        act(client, 'take')
        act(client, 'take')
        act(client, 'take', ticket='OPS-138')
        act(client, 'resolve')
        act(client, 'mute', hours=2)
        act(client, 'mute', hours=4)
        act(client, 'known')
        act(client, 'known')

    # then
    assert [t for _, t in transport.replies()] == [
        '✅ Resolved · from the page · bootstrap',
        '🛠️ In progress: bootstrap',
        f"🛠️ In progress: bootstrap · {link('OPS-138')}",
        '✅ Resolved · from the page · bootstrap',
        '🔕 Muted for 2 h: bootstrap',
        '🔕 Marked as known noise: bootstrap',
    ]


def test_nothing_for_a_card_never_posted(store: Store):
    # given
    seed(store, card(QUIET, ts=None))
    transport = Transport()

    # when
    with app(store, transport) as client:
        done = act(client, 'resolve', key=QUIET)

    # then
    assert done['status'] == 'done'
    assert transport.calls == []


def test_a_merged_card_stays_quiet_and_its_parent_gets_the_note(store: Store):
    # given
    seed(store, card(KEY), card(KID, ts=KID_TS, parent=KEY))
    transport = Transport()

    # when
    with app(store, transport) as client:
        act(client, 'resolve', key=KID)
        act(client, 'mute', key='log|checkout-service|ERROR|*', hours=3)

    # then
    assert transport.replies() == [(POST_TS, '🔕 Muted for 3 h: bootstrap')]


def test_the_switch_turns_the_notes_off(store: Store, monkeypatch):
    # given
    seed(store)
    transport = Transport()

    # when
    with app(store, transport, slack_state_notes=False) as client:
        done = act(client, 'resolve')
    default, off = load_settings(lambda: {}), load_settings(lambda: {'slack.state-notes': ' False '})

    # then
    assert done['status'] == 'done'
    assert transport.calls == []
    assert (default.slack_state_notes, off.slack_state_notes) == (True, False)


@pytest.mark.parametrize('error', [SlackDown('Slack did not answer'), RuntimeError('socket closed')])
def test_a_failed_note_leaves_an_activity_line_and_the_action_goes_through(store: Store, error: Exception):
    # given
    seed(store)
    transport = Transport(fail=error)

    # when
    with app(store, transport) as client:
        done = act(client, 'resolve')
        saved = client.get('/api/state').json()['anomalies'][0]
        lines = [r for r in client.get('/api/activity').json() if r['kind'] == 'slack']

    # then
    assert done['status'] == 'done' and done['result'] == 'No action needed'
    assert saved['triage'] == 'handled' and 'stateNote' not in saved['slackPost']
    assert [(r['who'], r['key']) for r in lines] == [('server', KEY)]
    assert lines[0]['text'].startswith(f'status note not sent · {KEY} · ')


def test_the_note_link_is_built_when_getpermalink_fails_and_the_rev_moves(store: Store):
    # given
    seed(store)

    # when
    with app(store, Transport(), api=FakeSlackApi(fail={'chat.getPermalink'})) as client:
        before = store.rev()['rev']
        act(client, 'resolve')
        saved = client.get('/api/state').json()

    # then
    assert saved['anomalies'][0]['slackPost']['stateNote']['permalink'] == f'https://example.slack.com/archives/C9/p1792224001000001?thread_ts={POST_TS}&cid=C9'
    assert saved['rev'] == before + 2


def test_the_investigators_triage_replies_as_the_service_in_live_mode_only(store: Store):
    # given
    seed(store, card(KEY), card(QUIET, ts=None))
    calls = []
    effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, state_note=lambda key, before: calls.append((key, before.get('triage'))))
    live = Toolbox(sources(), store, effects, [KEY, QUIET], 'live', time.time(), 'https://skopax.example.com')
    shadow = Toolbox(sources(), store, effects, [QUIET], 'shadow', time.time(), 'https://skopax.example.com')

    # when
    live.call('note', {'key': KEY, 'text': 'one client, harmless'})
    live.call('triage', {'key': KEY, 'state': 'handled'})
    shadow.call('note', {'key': QUIET, 'text': 'x'})
    shadow.call('triage', {'key': QUIET, 'state': 'handled'})

    # then
    assert calls == [(KEY, 'dived')]


def test_the_app_hands_the_investigator_a_note_that_names_the_product(store: Store):
    # given
    seed(store)
    transport = Transport()

    # when
    with app(store, transport) as client:
        before = store.anomaly(KEY)
        store.triage(KEY, 'handled', by='pod')
        client.app.state.investigator.effects.state_note(KEY, before)
        client.app.state.investigator.effects.state_note(KEY, store.anomaly(KEY))
        store.triage(KEY, 'progress', ticket='OPS-1 OPS-2', by='pod')
        client.app.state.investigator.effects.state_note(KEY, before)

    # then
    assert [t for _, t in transport.replies()] == ['✅ Resolved: Skopax', f"🛠️ In progress: Skopax · {link('OPS-1')} {link('OPS-2')}"]


def test_a_resolve_note_says_how_via_and_who(store: Store):
    # given
    seed(store, card(KEY), card(QUIET, ts=KID_TS), card(KID, ts='1792224000.000300'))
    store.card_ids.insert_one({'_id': QUIET, 'n': 12})
    transport = Transport()

    # when
    with app(store, transport) as client:
        client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'kind': 'fixed', 'ref': 'OPS-140', 'reason': 'pool enlarged', 'label': 'Resolve'}, headers=AUTH)
        client.post('/api/actions', json={'type': 'resolve', 'key': KID, 'kind': 'duplicate', 'ref': '12', 'label': 'Resolve'}, headers=AUTH)
        before = store.anomaly(QUIET)
        store.triage(QUIET, 'handled', by='pod', resolution={'kind': 'handled', 'by': 'pod', 'via': 'system'})
        client.app.state.investigator.effects.state_note(QUIET, before)

    # then
    assert [t for _, t in transport.replies()] == [f"✅ Resolved · from the page · bootstrap · {link('OPS-140')} · pool enlarged", '✅ Resolved, duplicate: #12 · from the page · bootstrap',
                                                   "✅ Resolved · from Skopax · investigator"]


def test_auto_resolve_replies_once_on_a_posted_card(store: Store):
    # given
    now = time.time()
    seed(store, card(KEY, status='cleared', clearedAt=iso(now - 3 * DAY - 60)), card(QUIET, ts=None, status='cleared', clearedAt=iso(now - 3 * DAY - 60)))
    transport = Transport()

    # when
    with app(store, transport, auto_resolve_hours=72) as client:
        client.app.state.server_apply([], now, None)
        client.app.state.server_apply([], now + 60, None)

    # then
    assert store.anomaly(QUIET)['triage'] == 'handled'
    assert transport.replies() == [(POST_TS, '✅ Did not recur for 3 days, auto-resolved')]
    assert store.anomaly(KEY)['slackPost']['stateNote']['by'] == 'system'


def test_the_jira_sweep_replies_when_a_ticket_closes_a_card_or_takes_it(store: Store):
    # given
    now = 1_792_224_000.0
    seed(store, card(KEY, status='cleared', triage='progress', ticket='OPS-127'), card(QUIET, ts='1792224000.000300', triage='progress', ticket='OPS-128'),
         card('log|orders-service|ERROR|x', ts='1792224000.000400'))
    jira = FakeJira([issue('OPS-127', 'Done', 'y', closed_at=closed(3)), issue('OPS-128', 'Live', 'z', closed_at=closed(3)),
                     issue('OPS-131', 'In Development', 'card: https://skopax.example.com/#a=log%7Corders-service%7CERROR%7Cx', dev='Oscar Green')])
    transport = Transport()

    # when
    with app(store, transport, jira=jira) as client:
        client.app.state.jira_links.run_once(now)
        client.app.state.jira_links.run_once(now)

    # then
    assert sorted(transport.replies()) == [(POST_TS, f"✅ {link('OPS-127')} is now Done, card closed"),
                                           ('1792224000.000400', f"🛠️ In progress: Oscar Green · {link('OPS-131')}")]
    assert store.anomaly(QUIET).get('triage') is None, 'a ticket closed while the card still fires reopens it, without a note'


def test_state_notes_are_off_without_a_slack_token(store: Store):
    # given
    seed(store)

    # then
    assert StateNotes(store, Slack('', '#ops', Transport(), FakeSlackApi())).enabled is False
    assert StateNotes(store, Slack('xoxb-test', '#ops', Transport(), FakeSlackApi()), flag=False).enabled is False
    assert StateNotes(store, Slack('xoxb-test', '#ops', Transport(), FakeSlackApi())).enabled is True


@pytest.mark.parametrize('hours, english', [(1.5, '\U0001F515 Muted for 1.5 h: alice'), (0.25, '\U0001F515 Muted for 0.25 h: alice'), (24.0, '\U0001F515 Muted for 24 h: alice')])
def test_a_mute_note_shows_the_hours_short_in_english_and_in_a_pack_language(hours: float, english: str):
    # given
    pack = next(locale for locale in locales.packs() if locale.lang == 'tr')
    messages.set_catalogs([('tr', pack.messages)])
    shown = f'{hours:g}'

    # when
    plain = statenotes.muted_text('alice', hours)
    with messages.speaking('tr'):
        translated = statenotes.muted_text('alice', hours)

    # then
    assert plain == english
    assert translated == pack.messages['notes.state.muted'].format(hours=shown, who='alice') and f' {shown} ' in translated and '.0 ' not in translated
