import dataclasses
import datetime as dt
import json

import pytest
from bson import Binary
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_anthropic import FakeAnthropic, reply, text, tool
from tests.fake_cdp import JPEG, FakeChrome, Site
from tests.fake_http import FakePages
from tests.fake_slack import fake_slack, images
from tests.test_investigator import KEY, NOW, seed
from tests.test_investigator_tools import box, seeded, sources
from tests.website_kit import CHAIRS, quiet, site_box, through
from skopax.app import create_app
from skopax.cardposts import CardPosts
from skopax.investigator import Investigator, clip
from skopax.investigator.tools import POD_BY, Effects
from skopax.plugintools import PluginTools
from skopax.slack import AUTO_BY, SlackDown
from skopax.store import Store, iso, utc_now

DRAFT = 'checkout-service ERROR lines 40, 2 a day ago.\nThe page looks empty, the screenshot is below.'
THREAD = '1792224000.000100'
SHOT = {'id': '0a1b2c3d4e5f60718293a4b5c6d7e8f9', 'url': 'https://www.example.com/', 'viewport': 'desktop', 'full': False, 'at': '2026-10-21T08:55:00Z'}
BASE = 'https://skopax.example.com'
ATTACHED = 'attached 1 screenshot to the #ops thread'
LINE = {'text': 'Shop Ops \u00b7 powered by Skopax', 'href': 'https://skopax.io'}
MARK = '<https://skopax.io|Shop Ops \u00b7 powered by Skopax>'


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    quiet(monkeypatch)


@pytest.fixture
def chrome():
    c = FakeChrome({CHAIRS: Site()})
    yield c
    c.close()


def shot(store: Store, viewport: str = 'desktop') -> dict:
    entry = {'id': '1b2c3d4e5f60718293a4b5c6d7e8f90a', 'url': CHAIRS, 'finalUrl': CHAIRS, 'viewport': viewport, 'full': False, 'at': '2026-10-21T08:55:00Z',
             'title': 'Garden Chairs | Shop', 'status': 200, 'kb': 0.5, 'link': f'{BASE}/api/screenshots/1b2c3d4e5f60718293a4b5c6d7e8f90a'}
    store.add_screenshot({'_id': entry['id'], 'runId': 'run-1', 'keys': [KEY], **{k: v for k, v in entry.items() if k not in ('id', 'kb', 'link')}, 'size': len(JPEG),
                          'mime': 'image/jpeg', 'data': Binary(JPEG), 'atDt': dt.datetime(2026, 10, 21, 8, 55, tzinfo=dt.UTC)})
    return entry


def drafted(store: Store, shots: list[dict], sev: str = 'crit', **extra) -> Store:
    store.save(state(anomaly(KEY, sev=sev, **extra)), None)
    store.pod_slack_draft(KEY, DRAFT, POD_BY, shots)
    return store


def app(store: Store, slack, **over) -> TestClient:
    return TestClient(create_app(dataclasses.replace(settings(), **over), store, slack=slack))


def due_now(store: Store) -> None:
    store.anomalies.update_one({'_id': KEY}, {'$set': {'autoSend.at': iso(utc_now() - dt.timedelta(minutes=1))}})


def auto_send(client: TestClient, store: Store) -> None:
    due_now(store)
    assert client.app.state.auto_send.run_once() == 1


def send_now(client: TestClient, store: Store) -> None:
    assert client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH).status_code == 201


def pod_send(client: TestClient, store: Store) -> None:
    client.app.state.investigator.effects.send_slack(KEY, POD_BY)


def blocks_of(payload: dict) -> list[dict]:
    return payload.get('blocks') or payload['attachments'][0]['blocks']


def kinds(payload: dict) -> list[str]:
    return [b['type'] for b in blocks_of(payload)]


def bar_of(blocks: list[dict]) -> list[str]:
    return [e['text'] for e in blocks[-1]['elements']]


def slack_lines(store: Store) -> list[tuple]:
    return [(a['who'], a['text'], a.get('runId')) for a in store.list_activity(None, 20) if a['kind'] == 'slack' and a['who'] == 'investigator']


def link_of(entry: dict) -> str:
    return f"<{BASE}/api/screenshots/{entry['id']}|Screenshot on Skopax ({entry['viewport']})>"


@pytest.mark.parametrize('send', [auto_send, send_now, pod_send], ids=['auto-send', 'send-now', 'slack_send'])
def test_every_way_out_posts_the_screenshot_link_then_puts_the_image_in_the_post_s_thread(store: Store, send):
    # given
    s = shot(store)
    drafted(store, [s])
    slack, post, api = fake_slack()

    # when
    with app(store, slack) as client:
        slack.attribution = lambda: LINE
        send(client, store)
        listed = client.get('/api/actions').json()

    # then
    uploads = [m for m in api.methods() if m in ('files.getUploadURLExternal', 'upload', 'files.completeUploadExternal')]
    assert uploads == ['files.getUploadURLExternal', 'upload', 'files.completeUploadExternal']
    assert len(post.calls) == 1 and images(post.posted[0]) == [], 'Slack shows an unshared file in a block as a broken image, so the post carries no image block'
    bar = bar_of(post.posted[0]['blocks'])
    assert bar[-2:] == [link_of(s), MARK], 'the screenshot line sits in the bottom bar, right before the attribution'
    assert post.posted[0]['text'].startswith('checkout-service ERROR lines 40')
    done = next(p for m, p in api.calls if m == 'files.completeUploadExternal')
    assert (done['channel_id'], done['thread_ts']) == ('#ops', store.anomaly(KEY)['slackPost']['ts'])
    assert store.anomaly(KEY)['slackPost']['screenshots'] == [s], 'the sent post keeps the screenshots it carried'
    assert json.loads(done['files'])[0]['id'] == 'F1'
    assert slack_lines(store) == [('investigator', f'{KEY} \u00b7 {ATTACHED}', None)]
    assert listed[0]['result'] == f'sent to #ops with the draft; {ATTACHED}'


def test_a_post_with_shots_keeps_its_screenshot_line_after_a_take_or_resolve_rewrites_it(store: Store):
    # given
    s = shot(store, 'phone')
    drafted(store, [s])
    slack, post, api = fake_slack()
    with app(store, slack) as client:
        send_now(client, store)
    slack.attribution = lambda: LINE
    posts = CardPosts(store, slack, BASE, autostart=False)

    # when
    posts.after({'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com'})
    posts.run_batch()
    store.triage(KEY, 'handled', by='alice@example.com')
    posts.after({'type': 'resolve', 'key': KEY, 'requestedBy': 'alice@example.com'})
    posts.run_batch()

    # then
    rewrites = [p for m, p in api.calls if m == 'chat.update']
    assert len(rewrites) == 2
    assert [bar_of(r['blocks'])[-2:] for r in rewrites] == [[link_of(s), MARK]] * 2


def test_slack_not_answering_the_post_fails_the_send_and_uploads_nothing(store: Store):
    # given
    drafted(store, [shot(store)])
    slack, post, api = fake_slack(post_fail=True)

    # when
    with app(store, slack) as client:
        res = client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)

    # then
    assert res.status_code == 502
    assert len(post.calls) == 1 and 'upload' not in api.methods()
    assert store.anomaly(KEY).get('slackPost') is None


@pytest.mark.parametrize('over, scopes, fail, line', [
    ({'slack_attach_screenshots': False}, 'chat:write,files:write', None, 'screenshots not attached: slack.attach-screenshots is off'),
    ({}, 'chat:write', None, 'screenshots not attached: the Slack token has no files:write scope (add it to the app and reinstall)'),
    ({}, 'chat:write,files:write', {'upload'}, 'screenshots not attached: upload failed'),
    ({}, 'chat:write,files:write', {'files.completeUploadExternal'}, 'screenshots not attached: files.completeUploadExternal failed'),
], ids=['attach-off', 'no-files-write', 'upload-error', 'complete-error'])
def test_a_screenshot_problem_never_fails_the_post(store: Store, over, scopes, fail, line):
    # given
    s = shot(store)
    drafted(store, [s])
    slack, post, api = fake_slack(fail=fail, scopes=scopes)

    # when
    with app(store, slack, **over) as client:
        auto_send(client, store)

    # then
    assert len(post.posted) == 1 and images(post.posted[0]) == []
    assert link_of(s) in bar_of(post.posted[0]['blocks'])
    assert store.anomaly(KEY)['slackPost']['by'] == AUTO_BY
    assert slack_lines(store) == [('investigator', f'{KEY} \u00b7 {line}', None)]
    if over or 'files:write' not in scopes:
        assert 'upload' not in api.methods()


def test_an_expired_screenshot_posts_the_text_and_says_so(store: Store):
    # given
    drafted(store, [{'id': 'gone', 'url': CHAIRS, 'finalUrl': CHAIRS, 'viewport': 'phone'}])
    slack, post, api = fake_slack()

    # when
    with app(store, slack) as client:
        send_now(client, store)

    # then
    assert len(post.posted) == 1 and images(post.posted[0]) == []
    assert 'upload' not in api.methods()
    assert slack_lines(store) == [('investigator', f'{KEY} \u00b7 screenshots not attached: none of them is stored any more (30-day retention)', None)]


def test_a_report_post_links_its_screenshots_before_its_button_and_puts_them_in_its_thread(store: Store):
    # given
    s = shot(store)
    report = store.add_deploy_report({'summary': '1 deploy since 05:26 UTC: 1 clean', 'windowFrom': '2026-10-22T05:26:00Z', 'windowTo': '2026-10-22T07:13:00Z', 'releases': [],
                                      'rows': [{'ns': 'shop', 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': '2026-10-22T05:26:00Z', 'flags': []}],
                                      'issues': [], 'usd': 0.0, 'costNote': 'numbers'})
    store.report_slack_draft(report['id'], 'One clean deploy.', POD_BY, [s])
    slack, post, api = fake_slack()

    # when
    with app(store, slack) as client:
        res = client.post(f"/api/deploy-reports/{report['id']}/slack-posts", headers=AUTH)

    # then
    assert res.status_code == 201
    blocks = blocks_of(post.posted[0])
    shown = [i for i, b in enumerate(blocks) if b['type'] == 'context' and any(link_of(s) in e.get('text', '') for e in b['elements'])]
    actions = [i for i, b in enumerate(blocks) if b['type'] == 'actions']
    assert shown and (not actions or shown[0] < actions[0])
    assert 'files.completeUploadExternal' in api.methods()
    assert res.json()['result'].endswith(f'; {ATTACHED}')


def test_slack_draft_keeps_the_screenshots_taken_in_this_run(store: Store, chrome: FakeChrome):
    # given
    seeded(store, sev='crit')
    b = site_box(store, through(FakePages()), {'main': {'browser-url': f'http://127.0.0.1:{chrome.port}'}}, run_id='run-1', keys=[KEY])
    content, _ = b.call('screenshot', {'url': CHAIRS})
    shot_id = json.loads(content[0]['text'])['id']

    # when
    refused, refused_err = b.call('slack_draft', {'key': KEY, 'text': DRAFT, 'screenshots': ['other-run']})
    out, err = b.call('slack_draft', {'key': KEY, 'text': DRAFT, 'screenshots': [shot_id, shot_id]})

    # then
    assert refused_err and 'screenshot other-run was not taken in this run' in refused
    assert not err, out
    draft = store.anomaly(KEY)['slackDraft']
    assert [x['id'] for x in draft['screenshots']] == [shot_id]
    assert draft['screenshots'][0]['link'] == f'{BASE}/api/screenshots/{shot_id}' and 'data' not in draft['screenshots'][0]
    assert store.anomaly(KEY)['autoSend']['draftHash']


def test_a_shadow_slack_draft_stores_no_screenshots(store: Store, chrome: FakeChrome):
    # given
    seeded(store, sev='crit')
    b = site_box(store, through(FakePages()), {'main': {'browser-url': f'http://127.0.0.1:{chrome.port}'}}, mode='shadow', keys=[KEY])
    content, _ = b.call('screenshot', {'url': CHAIRS})

    # when
    out, err = b.call('slack_draft', {'key': KEY, 'text': DRAFT, 'screenshots': [json.loads(content[0]['text'])['id']]})

    # then
    assert not err and 'shadow mode' in out
    assert store.anomaly(KEY).get('slackDraft') is None


def test_slack_update_posts_its_reply_and_puts_the_screenshots_in_the_same_thread(store: Store):
    # given
    s = shot(store)
    store.save(state(anomaly(KEY, sev='crit', slackPost={'channel': 'C1', 'ts': THREAD, 'at': '2026-10-21T09:00:00Z', 'by': 'auto'})), None)
    slack, post, api = fake_slack()

    # when
    with app(store, slack) as client:
        done = client.app.state.investigator.effects.thread_update(KEY, 'The page is fine again.', POD_BY, [s], 'run-9')

    # then
    payload = post.posted[0]
    assert (payload['thread_ts'], payload['text']) == (THREAD, 'The page is fine again.')
    assert images(payload) == [] and [b['type'] for b in payload['attachments'][0]['blocks']] == ['context']
    assert link_of(s) in payload['attachments'][0]['blocks'][0]['elements'][0]['text']
    shared = [p for m, p in api.calls if m == 'files.completeUploadExternal']
    assert (shared[0]['channel_id'], shared[0]['thread_ts']) == ('C1', THREAD), 'the images join the card thread, not a thread under the reply'
    assert done['result'] == f'replied in the Slack thread; {ATTACHED}'
    assert slack_lines(store) == [('investigator', f'{KEY} \u00b7 {ATTACHED}', 'run-9')]


def test_the_slack_update_tool_hands_this_run_s_screenshots_to_the_reply(store: Store, chrome: FakeChrome):
    # given
    seeded(store, slackPost={'channel': 'C1', 'ts': THREAD, 'at': '2026-10-21T09:00:00Z', 'by': 'auto'})
    updates = []
    fx = Effects(lambda key, by: {}, lambda key, text, by, shots=(), run_id=None, notice=None: updates.append((key, [x['id'] for x in shots], run_id)) or {'result': f'replied in the Slack thread; {ATTACHED}'})
    b = site_box(store, through(FakePages()), {'main': {'browser-url': f'http://127.0.0.1:{chrome.port}'}}, run_id='run-1', keys=[KEY], fx=fx)
    content, _ = b.call('screenshot', {'url': CHAIRS})
    shot_id = json.loads(content[0]['text'])['id']

    # when
    out, err = b.call('slack_update', {'key': KEY, 'text': 'The page is fine again, the screenshot is attached.', 'screenshots': [shot_id]})

    # then
    assert not err, out
    assert updates == [(KEY, [shot_id], 'run-1')]
    assert out == f'slack_update saved on {KEY}; replied in the Slack thread; {ATTACHED}'


def test_the_run_line_in_activity_carries_the_run_s_screenshots(store: Store, chrome: FakeChrome):
    # given
    seed(store, anomaly(KEY), history=[])
    question = 'How does the chairs page look?'
    store.add_action({'type': 'ask', 'key': 'ask', 'target': 'ask', 'label': question, 'text': question, 'cards': [], 'requestedBy': 'alice'})
    runtime = site_box(store, through(FakePages()), {'main': {'browser-url': f'http://127.0.0.1:{chrome.port}'}}).sources.plugin_tools.runtime
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live')
    inv = Investigator(store, sources(plugin_tools=PluginTools(runtime)), Effects(lambda key, by: {}, lambda *a, **k: {}), s,
                       client=FakeAnthropic([reply(tool('screenshot', {'url': CHAIRS})), reply(text('The page opens.'), stop='end_turn')]), clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)
    lines = store.list_activity(None, 20)

    # then
    shots = store.investigation(record['id'])['screenshots']
    ended = [a for a in lines if a['kind'] == 'run' and a['text'].startswith('run done')]
    assert shots and ended[0]['screenshots'] == shots and ended[0]['runId'] == record['id']
    assert all('screenshots' not in a for a in lines if a is not ended[0])


def test_a_slack_ask_run_hands_its_screenshots_to_the_reply_and_keeps_them_on_the_record(store: Store, chrome: FakeChrome):
    # given
    from tests.test_investigator_slack import slack_action
    seed(store, anomaly(KEY), history=[])
    slack_action(store)
    seen = []

    def slack_reply(action, outcome, recap):
        seen.append(action)
        return {'ts': action['slack']['placeholderTs'], 'text': recap}

    runtime = site_box(store, through(FakePages()), {'main': {'browser-url': f'http://127.0.0.1:{chrome.port}'}}).sources.plugin_tools.runtime
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live')
    inv = Investigator(store, sources(plugin_tools=PluginTools(runtime)), Effects(lambda key, by: {}, lambda *a, **k: {}, slack_reply), s,
                       client=FakeAnthropic([reply(tool('screenshot', {'url': CHAIRS})), reply(text('The page opens, no banner.'), stop='end_turn')]), clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)

    # then
    shots = store.investigation(record['id'])['screenshots']
    assert [x['url'] for x in shots] == [CHAIRS]
    assert seen[0]['screenshots'] == [shots[0]['id']] and seen[0]['runId'] == record['id']
    listed = store.list_investigations(KEY, 5, page=True)[0]
    assert listed['screenshots'][0]['id'] == shots[0]['id']
    image = inv.client.requests[1]['messages'][-1]['content'][0]['content'][1]
    assert image['type'] == 'image' and image['source']['media_type'] == 'image/jpeg'


def test_a_draft_without_screenshots_posts_as_before(store: Store):
    # given
    drafted(store, [])
    slack, post, api = fake_slack()

    # when
    with app(store, slack) as client:
        send_now(client, store)

    # then
    assert api.methods() == ['chat.getPermalink'] and kinds(post.posted[0])[-1] == 'context' and 'image' not in kinds(post.posted[0]) and 'api/screenshots' not in str(post.posted[0])
    assert 'screenshots' not in store.anomaly(KEY)['slackDraft'] and 'screenshots' not in store.anomaly(KEY)['slackPost']
    assert slack_lines(store) == []


def test_an_operator_edit_keeps_the_screenshots_and_a_discard_drops_them(store: Store):
    # given
    s = SHOT
    drafted(store, [s])
    slack, _, _ = fake_slack()

    # when
    with app(store, slack) as client:
        client.put('/api/slack-drafts', json={'key': KEY, 'text': 'Text edited by hand.'}, headers=AUTH)
        edited = store.anomaly(KEY)['slackDraft']
        client.put('/api/slack-drafts', json={'key': KEY, 'text': ''}, headers=AUTH)
        discarded = store.anomaly(KEY).get('slackDraft')
        client.put('/api/slack-drafts', json={'key': KEY, 'text': 'New draft.'}, headers=AUTH)
        fresh = store.anomaly(KEY)['slackDraft']

    # then
    assert edited['text'] == 'Text edited by hand.' and edited['by'] == 'token:bootstrap' and edited['screenshots'] == [s]
    assert discarded is None
    assert 'screenshots' not in fresh


def test_the_card_tool_shows_the_screenshots_of_the_draft_and_of_the_sent_post_after_the_draft_is_gone(store: Store):
    # given
    s = SHOT
    drafted(store, [s])
    slack, _, _ = fake_slack()
    shown = [{'id': s['id'], 'url': s['url'], 'viewport': 'desktop', 'full': False, 'at': s['at']}]

    # when
    before, _ = box(store, keys=()).call('card', {'key': KEY})
    with app(store, slack) as client:
        send_now(client, store)
        client.put('/api/slack-drafts', json={'key': KEY, 'text': ''}, headers=AUTH)
    after, _ = box(store, keys=()).call('card', {'key': KEY})

    # then
    draft_card, sent_card = json.loads(before), json.loads(after)
    assert draft_card['slackDraft']['screenshots'] == shown and 'slackPost' not in draft_card
    assert 'slackDraft' not in sent_card and sent_card['slackPost']['screenshots'] == shown
    assert sent_card['slackPost']['by'] == 'token:bootstrap'
    assert 'its Slack draft and post with their screenshots' in next(d['description'] for d in box(store).definitions() if d['name'] == 'card')


def test_the_card_tool_lists_no_screenshots_when_the_post_carried_none(store: Store):
    # given
    store.save(state(anomaly(KEY, sev='crit', slackPost={'channel': 'C1', 'ts': THREAD, 'at': '2026-10-21T09:00:00Z', 'by': 'auto'})), None)

    # when
    out, err = box(store, keys=()).call('card', {'key': KEY})

    # then
    assert not err and json.loads(out)['slackPost'] == {'at': '2026-10-21T09:00:00Z', 'by': 'auto'}


@pytest.mark.parametrize('text, expected', [
    ('short line', 'short line'),
    ('see https://skopax.example.com/api/screenshots/0a1b2c3d4e5f60718293a4b5c6d7e8f9 now', 'see'),
    ('see https://example.com/ and so on, words after it', 'see https://example.com/ and so'),
    ('ends on https://example.com/shop exactly then', 'ends on https://example.com/shop'),
    ('no link in this line, just words that run long', 'no link in this line, just words'),
])
def test_clip_never_leaves_half_a_link(text, expected):
    # when
    out = clip(text, 32)

    # then
    assert out == expected


def test_a_shadow_slack_update_attaches_nothing(store: Store, chrome: FakeChrome):
    # given
    seeded(store, slackPost={'channel': 'C1', 'ts': THREAD, 'at': '2026-10-21T09:00:00Z', 'by': 'auto'})
    attached = []
    fx = Effects(lambda key, by: {}, lambda *a, **k: attached.append(a))
    b = site_box(store, through(FakePages()), {'main': {'browser-url': f'http://127.0.0.1:{chrome.port}'}}, mode='shadow', keys=[KEY], fx=fx)
    content, _ = b.call('screenshot', {'url': CHAIRS})

    # when
    out, err = b.call('slack_update', {'key': KEY, 'text': 'x', 'screenshots': [json.loads(content[0]['text'])['id']]})

    # then
    assert not err and 'shadow mode' in out
    assert attached == []
