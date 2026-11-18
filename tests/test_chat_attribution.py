import asyncio
import dataclasses
import json

from fastapi.testclient import TestClient

from skopax import brand
from skopax.app import create_app
from skopax.cardposts import CardPosts
from skopax.config import Settings, load_settings
from skopax.features import KEYS
from skopax.push import Pusher
from skopax.slack import SECTION_LIMIT, Slack
from skopax.store import Store
from skopax.tunables import STATIC
from skopax.users import Users
from tests.conftest import ADMIN_EMAIL, AUTH, anomaly, settings, state
from tests.fake_slack import FakeSlackApi, fake_slack
from tests.plugin_kit import registry_with
from tests.plugins import switchable_entitlement
from tests.fake_anthropic import reply, text, verdict
from tests.test_slack import FakeSlack
from tests.test_slack_dm_chats import DM, LATER, answer, dm_event
from tests.test_slackevents import intake
from skopax.slackevents import Replier
from skopax.kube import Kube
from skopax.plugins import teams as teams_plugin
from skopax.plugins.teams import cards as teams_cards
from tests.teams_kit import TECH, Transport as TeamsTransport, add_route, open_card, texts as teams_texts

KEY = 'log|checkout-service|ERROR|boom'
NAMED = 'Acme Ops'
LINE = {'text': f'{NAMED} \u00b7 powered by Skopax {brand.display_version()}', 'href': 'https://skopax.io'}
MARK = f'<https://skopax.io|{NAMED} \u00b7 powered by Skopax {brand.display_version()}>'
PUSH_USER = 'U0AAA1111'


def app_with(store: Store, transport: FakeSlack, api: FakeSlackApi, registry=None, **over) -> TestClient:
    s = dataclasses.replace(settings(), display_name=NAMED, slack_push_user=PUSH_USER, **over)
    extra = {'registry': registry} if registry is not None else {}
    return TestClient(create_app(s, store, slack=Slack('xoxb-test', '#ops', transport, api), **extra), base_url='https://skopax.dev.example.com')


def seeded(store: Store) -> Store:
    store.save(state(anomaly(KEY, num=14, now=5, firstSeen='2026-11-11T07:00:00Z')), None)
    return store


def everything(client: TestClient) -> None:
    client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
    client.post('/api/slack-updates', json={'key': KEY, 'text': 'still on it'}, headers=AUTH)
    client.post('/api/actions', json={'type': 'take', 'key': KEY, 'label': 'Take'}, headers=AUTH)
    client.app.state.pusher.text('checkout-service is down')
    assert client.app.state.card_posts.drain(5)


def payloads(transport: FakeSlack, api: FakeSlackApi) -> list[dict]:
    return [p for _, p in transport.calls] + [p for m, p in api.calls if m == 'chat.update']


def card_post(transport: FakeSlack) -> dict:
    return next(p for _, p in transport.calls if 'thread_ts' not in p and p['channel'] == '#ops')


def dm(transport: FakeSlack) -> dict:
    return next(p for _, p in transport.calls if p['channel'] == PUSH_USER)


def bar(payload: dict) -> list[str]:
    return [e['text'] for e in payload['blocks'][-1]['elements']]


def switch_on(client: TestClient) -> None:
    saved = client.put('/api/admin/settings', json={'key': 'core.chat-attribution', 'value': 'true'}, headers=AUTH)
    assert saved.status_code == 200


def test_chat_posts_carry_no_attribution_by_default(store: Store):
    # given
    transport, api = FakeSlack(), FakeSlackApi()

    # when
    with app_with(seeded(store), transport, api) as client:
        everything(client)
        line = client.app.state.chat_attribution()

    # then
    sent = payloads(transport, api)
    assert line is None
    assert {'card post', 'thread reply', 'update', 'dm'} <= {'dm' if p['channel'] == PUSH_USER else 'thread reply' if 'thread_ts' in p else 'update' if 'ts' in p else 'card post' for p in sent}
    assert [p for p in sent if 'powered by' in json.dumps(p) or 'skopax.io' in json.dumps(p)] == []


def test_the_admin_switch_adds_the_line_to_top_level_posts(store: Store):
    # given
    transport, api = FakeSlack(), FakeSlackApi()

    # when
    with app_with(seeded(store), transport, api) as client:
        switch_on(client)
        everything(client)
        line = client.app.state.chat_attribution()

    # then
    post, message = card_post(transport), dm(transport)
    [update] = [p for m, p in api.calls if m == 'chat.update']
    assert line == LINE
    assert bar(post)[-1] == MARK and 'powered by' not in post['text'], 'the line ends the bar and stays out of the notification text'
    assert bar(update)[-1] == MARK, 'the in-place update keeps it'
    assert message['text'] == 'checkout-service is down'
    assert message['blocks'] == [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'checkout-service is down'}},
                                 {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': MARK}]}]
    assert sum(json.dumps(p).count('skopax.io') for p in (post, message)) == 2, 'one line per message'


def test_thread_replies_never_carry_it(store: Store):
    # given
    transport, api = FakeSlack(), FakeSlackApi()
    seeded(store)
    store.save(state(anomaly(KEY, num=14), anomaly('log|other|ERROR|y', num=15)), None)
    store.slack_posted('log|other|ERROR|y', {'channel': 'C1', 'ts': '1792224000.000100', 'replyTs': '1792224000.000900', 'threadOf': KEY, 'at': '2026-11-11T07:00:00Z',
                                             'by': 'server', 'head': {'kind': 'opened', 'by': 'server', 'at': '2026-11-11T07:00:00Z'}})
    slack, post, threaded = fake_slack()
    slack.attribution = lambda: LINE

    # when
    with app_with(store, transport, api, chat_attribution=True) as client:
        everything(client)
    posts = CardPosts(store, slack, 'https://skopax.example.com', autostart=False)
    posts.after({'type': 'take', 'key': 'log|other|ERROR|y', 'requestedBy': 'alice@example.com'})
    posts.run_batch()
    slack.post({'channel': 'C1', 'thread_ts': '1792224000.000100', 'text': 'a reply', 'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'a reply'}}]})

    # then
    replies = [p for _, p in transport.calls if 'thread_ts' in p] + post.calls
    [rewrite] = [p for m, p in threaded.calls if m == 'chat.update']
    assert len(replies) >= 3 and [p for p in replies if 'skopax.io' in json.dumps(p)] == []
    assert 'skopax.io' not in json.dumps(rewrite), 'a card posted in another thread is a reply too'
    assert bar(card_post(transport))[-1] == MARK


def test_hide_attribution_wins_over_the_switch(store: Store):
    # given
    transport, api = FakeSlack(), FakeSlackApi()

    # when
    with app_with(seeded(store), transport, api, registry=registry_with(switchable_entitlement), chat_attribution=True, hide_attribution=True) as client:
        hidden = client.app.state.features.on('hide-attribution')
        everything(client)
        line = client.app.state.chat_attribution()

    # then
    assert hidden is True and line is None
    assert [p for p in payloads(transport, api) if 'skopax.io' in json.dumps(p)] == []


def test_attribution_link_never_unfurls(store: Store):
    # given
    transport, api = FakeSlack(), FakeSlackApi()

    # when
    with app_with(seeded(store), transport, api, chat_attribution=True) as client:
        everything(client)

    # then
    signed = [p for _, p in transport.calls if MARK in json.dumps(p, ensure_ascii=False)]
    assert len(signed) == 2
    assert all((p['unfurl_links'], p['unfurl_media']) == (False, False) for _, p in transport.calls)


def test_a_long_direct_message_is_split_into_sections_under_the_limit():
    # given
    slack, post, _ = fake_slack()
    slack.attribution = lambda: LINE
    text = 'x' * (SECTION_LIMIT + 500) + '\nsecond line\n' + 'y' * 40

    # when
    Pusher(slack, PUSH_USER, 'https://skopax.example.com').text(text)

    # then
    [sent] = post.calls
    sections = [b['text']['text'] for b in sent['blocks'][:-1]]
    assert sent['text'] == text
    assert all(b['type'] == 'section' for b in sent['blocks'][:-1]) and max(len(s) for s in sections) <= SECTION_LIMIT
    assert ''.join(sections).replace('\n', '') == text.replace('\n', '')
    assert sent['blocks'][-1] == {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': MARK}]}


def test_a_report_post_gets_the_line_at_the_end_of_its_attachment():
    # given
    slack, post, _ = fake_slack()
    slack.attribution = lambda: LINE
    report = {'text': 'Deploy report', 'attachments': [{'color': '#1f8a4c', 'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'clean'}}]}]}

    # when
    slack.post(report)

    # then
    [sent] = post.calls
    assert 'blocks' not in sent and sent['text'] == 'Deploy report'
    assert sent['attachments'] == [{'color': '#1f8a4c', 'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'clean'}},
                                                                 {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': MARK}]}]}]


def test_a_line_that_cannot_be_read_adds_nothing_and_the_post_goes_out():
    # given
    slack, post, _ = fake_slack()

    def broken():
        raise RuntimeError('switch store down')
    slack.attribution = broken

    # when
    slack.post({'channel': PUSH_USER, 'text': 'hello'})

    # then
    assert post.calls[0]['text'] == 'hello' and 'blocks' not in post.calls[0]


def test_the_switch_is_an_other_switch_that_is_off_by_default():
    # given
    row = {t.key: t for t in STATIC}['core.chat-attribution']

    # when
    from_env = load_settings(env={'SKOPAX_CHAT_ATTRIBUTION': 'true'}).chat_attribution
    from_config = load_settings(lambda: {'core.chat-attribution': 'true'}, env={}).chat_attribution

    # then
    assert (row.group, row.kind, row.label, row.live) == ('Features', 'bool', 'Powered by on Slack and Teams posts', True)
    assert 'its own direct messages' in row.help and 'a reply to a person never carries it' in row.help
    assert 'core.chat-attribution' not in KEYS, 'an Other switch, not a feature card'
    assert Settings(mongo_uri='m', mongo_db='d', token='t', commit_id='c').chat_attribution is False
    assert from_env is True and from_config is True


def test_the_switch_is_read_in_the_sending_thread(store: Store, monkeypatch):
    # given
    transport, api = FakeSlack(), FakeSlackApi()
    seen = []
    real = brand.attribution_line

    def recorded():
        try:
            asyncio.get_running_loop()
            seen.append('loop')
        except RuntimeError:
            seen.append('thread')
        return real()
    monkeypatch.setattr(brand, 'attribution_line', recorded)

    # when
    with app_with(seeded(store), transport, api, chat_attribution=True) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)

    # then
    assert seen and set(seen) == {'thread'}


def test_replies_to_a_person_never_carry_it_while_its_own_messages_do(store: Store):
    # given
    box, f = intake(store, gate_replies=[verdict(addressed=False), verdict(on_topic=False, small_talk=True), reply(text('Hi, I am here'), stop='end_turn')])
    box.slack.attribution = lambda: LINE
    replier = Replier(store, box.slack, dataclasses.replace(settings(), slack_listen='live'), base_url='https://skopax.example.com')

    # when
    asked = box.handle(dm_event())
    action = store.list_actions('requested', 5)[0]
    answer(store, action, 'p95 1.2 s')
    replier.reply(store.get_action(action['id']), 'done', 'p95 1.2 s')
    talk = box.handle(dm_event('hi there', ts=LATER))
    Pusher(box.slack, PUSH_USER, 'https://skopax.example.com').text('checkout-service is down')

    # then
    to_alice = [p for p in f.post.calls if p['channel'] == DM]
    own = [p for p in f.post.calls if p['channel'] == PUSH_USER]
    assert (asked, talk) == ('accepted', 'small-talk') and len(to_alice) == 3, 'the placeholder, the answer and the small-talk line'
    assert [p for p in to_alice if 'skopax.io' in json.dumps(p)] == []
    assert [p['blocks'][-1]['elements'][0]['text'] for p in own] == [MARK], 'a direct message Skopax starts itself keeps the line'


def test_the_line_is_read_only_for_a_message_that_can_carry_it(store: Store, monkeypatch):
    # given
    reads = []
    real = brand.attribution_line
    monkeypatch.setattr(brand, 'attribution_line', lambda: reads.append(1) or real())
    transport, api = FakeSlack(), FakeSlackApi()

    # when
    with app_with(seeded(store), transport, api, chat_attribution=True) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        posted = len(reads)
        client.post('/api/slack-updates', json={'key': KEY, 'text': 'still on it'}, headers=AUTH)

    # then
    assert posted == 1, 'the card post reads it once, for its bar'
    assert len(reads) == 1, 'a thread reply never reads it'


TEAMS_KEY = 'log|checkout-service|ERROR|teams'


def another(store: Store, name: str) -> Store:
    made = Store(store.db.client[name])
    Users(made.db).create(ADMIN_EMAIL, 'Admin', 'admin')
    return made


def teams_body(store: Store, tmp_path, registry=None, **over) -> list[str]:
    t = TeamsTransport()
    store.save({**state(open_card(TEAMS_KEY, sev='crit')), 'initialized': True}, None)
    extra = {'registry': registry} if registry is not None else {}
    with TestClient(create_app(dataclasses.replace(settings(), display_name=NAMED, **over), store, kube=Kube(root=tmp_path), transport=t, **extra)) as c:
        add_route(c, 'tech', TECH, keys='*', min_sev='crit')
        sent = c.post('/api/routes/send', json={'key': TEAMS_KEY, 'route': 'teams/tech'}, headers=AUTH)
    assert sent.json() == {'ok': True, 'sent': ['teams/tech']}
    return teams_texts(t.calls[0][1])


def test_a_teams_route_post_carries_the_line_only_with_the_switch_and_never_while_hidden(store: Store, tmp_path):
    # given
    text, href = brand.attribution_of(NAMED)
    shown = teams_cards.attribution_row({'attribution': {'text': text, 'href': href}})[0]['text']

    # when
    off = teams_body(store, tmp_path)
    on = teams_body(another(store, 'teams-on'), tmp_path, chat_attribution=True)
    hidden = teams_body(another(store, 'teams-hidden'), tmp_path, registry=registry_with(switchable_entitlement, teams_plugin), chat_attribution=True, hide_attribution=True)

    # then
    assert off[-1] != shown and href not in ' '.join(off) and text not in ' '.join(off), 'off by default'
    assert on[-1] == shown and on[:-1] == off, 'a route post is a message Skopax starts, so the line is its last row'
    assert hidden == off, 'the Enterprise hide switch wins'
