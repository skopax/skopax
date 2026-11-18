import dataclasses
import json
import urllib.parse

import pytest
from bson import Binary
from fastapi.testclient import TestClient

from skopax import slack as slack_api
from skopax.app import create_app
from skopax.attach import Attacher
from skopax.investigator.tools import Effects, Toolbox
from skopax.messages import msg
from skopax.slackevents import Replier
from skopax.store import Store
from tests.conftest import settings
from tests.fake_cdp import JPEG
from tests.fake_slack import fake_slack
from tests.test_investigator import KEY, NOW
from tests.test_investigator_slack import slack_action
from tests.test_investigator_tools import seeded, sources

THREAD = '1792224000.000100'
BASE = 'https://skopax.example.com'
CHAIRS = 'https://shop.example.com/garden/chairs'


def stored_shot(store: Store, shot_id: str = '0a1b2c3d4e5f60718293a4b5c6d7e8f9', viewport: str = 'desktop', title: str = 'Garden Chairs | Shop') -> str:
    store.add_screenshot({'_id': shot_id, 'runId': 'run-1', 'keys': [KEY], 'url': CHAIRS, 'finalUrl': CHAIRS, 'viewport': viewport, 'title': title, 'at': '2026-10-17T08:00:00Z',
                          'size': len(JPEG), 'mime': 'image/jpeg', 'data': Binary(JPEG)})
    return shot_id


class Resp:
    def __init__(self, body: dict, headers: dict):
        self.body = json.dumps(body).encode()
        self.headers = headers

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_file_methods_go_as_form_fields_and_the_bytes_as_a_raw_post(monkeypatch):
    # given
    seen = []

    def urlopen(req, timeout):
        seen.append(req)
        return Resp({'ok': True, 'upload_url': 'https://files.slack.com/upload/v1/x', 'file_id': 'F1'}, {})

    monkeypatch.setattr(slack_api.urllib.request, 'urlopen', urlopen)

    # when
    slack_api.api('xoxb-t', 'files.getUploadURLExternal', {'filename': 'a.jpg', 'length': 9})
    slack_api.api('xoxb-t', 'files.completeUploadExternal', {'files': json.dumps([{'id': 'F1', 'title': 'Chairs'}]), 'channel_id': 'C1', 'thread_ts': THREAD})
    slack_api.upload('https://files.slack.com/upload/v1/x', b'123456789')

    # then
    get, done, raw = seen
    assert get.get_method() == 'POST' and get.headers['Content-type'] == 'application/x-www-form-urlencoded'
    assert urllib.parse.parse_qs(get.data.decode()) == {'filename': ['a.jpg'], 'length': ['9']}
    assert done.get_method() == 'POST' and done.headers['Content-type'] == 'application/x-www-form-urlencoded'
    assert urllib.parse.parse_qs(done.data.decode()) == {'files': ['[{"id": "F1", "title": "Chairs"}]'], 'channel_id': ['C1'], 'thread_ts': [THREAD]}
    assert raw.get_method() == 'POST' and raw.data == b'123456789' and 'Authorization' not in raw.headers


def test_attach_uploads_each_file_and_shares_them_in_the_thread():
    # given
    slack, _, api = fake_slack()

    # when
    ids = slack.attach('C1', THREAD, [('screenshot-a.jpg', JPEG, 'Garden Chairs'), ('screenshot-b.jpg', b'xy', 'Home')])

    # then
    assert ids == ['F1', 'F2']
    assert api.methods() == ['files.getUploadURLExternal', 'upload', 'files.getUploadURLExternal', 'upload', 'files.completeUploadExternal']
    assert api.calls[0][1] == {'filename': 'screenshot-a.jpg', 'length': len(JPEG)}
    assert api.calls[1][1] == {'url': 'https://files.slack.com/upload/v1/F1', 'size': len(JPEG)}
    assert api.calls[4][1] == {'files': json.dumps([{'id': 'F1', 'title': 'Garden Chairs'}, {'id': 'F2', 'title': 'Home'}]), 'channel_id': 'C1', 'thread_ts': THREAD}


def test_the_attacher_uploads_stored_screenshots_and_says_so_in_activity(store: Store):
    # given
    shot = stored_shot(store)
    slack, _, api = fake_slack()

    # when
    line = Attacher(store, slack, True, BASE)('C1', THREAD, [shot, shot], KEY, run_id='run-1')

    # then
    assert line == msg('screenshot.attach.done', count=1, plural='', channel='#ops') == 'attached 1 screenshot to the #ops thread'
    assert api.methods().count('upload') == 1
    assert dict(api.calls)['files.getUploadURLExternal']['filename'] == f'screenshot-desktop-{shot}.jpg'
    act = store.list_activity(None, 10)[0]
    assert (act['kind'], act['who'], act['text'], act['runId']) == ('slack', 'investigator', f'{KEY} \u00b7 attached 1 screenshot to the #ops thread', 'run-1')


@pytest.mark.parametrize('scopes, enabled, fail, expected', [
    ('chat:write', True, None, 'screenshots not attached: the Slack token has no files:write scope (add it to the app and reinstall)'),
    ('chat:write,files:write', False, None, 'screenshots not attached: slack.attach-screenshots is off'),
    ('chat:write,files:write', True, {'files.completeUploadExternal'}, 'screenshots not attached: files.completeUploadExternal failed'),
    ('chat:write,files:write', True, {'upload'}, 'screenshots not attached: upload failed'),
])
def test_the_attacher_skips_with_an_activity_line_and_never_raises(store: Store, scopes, enabled, fail, expected):
    # given
    shot = stored_shot(store)
    slack, _, api = fake_slack(fail=fail, scopes=scopes)

    # when
    line = Attacher(store, slack, enabled, BASE)('C1', THREAD, [shot], KEY)

    # then
    assert line == expected
    assert store.list_activity(None, 10)[0]['text'] == f'{KEY} \u00b7 {expected}'
    if not enabled or 'files:write' not in scopes:
        assert 'upload' not in api.methods()


def test_an_expired_screenshot_is_skipped(store: Store):
    # given
    slack, _, api = fake_slack()

    # when
    line = Attacher(store, slack, True, BASE)('C1', THREAD, ['gone'], KEY)

    # then
    assert line == 'screenshots not attached: none of them is stored any more (30-day retention)'
    assert 'upload' not in api.methods()


def test_a_screenshot_is_a_link_line_named_by_its_viewport_and_the_product():
    # when
    line = slack_api.shot_links([{'id': 'a b', 'viewport': 'phone'}, {'id': 'c1'}, {'viewport': 'desktop'}], BASE)

    # then
    assert line == f"<{BASE}/api/screenshots/a%20b|Screenshot on Skopax (phone)> \u00b7 <{BASE}/api/screenshots/c1|Screenshot on Skopax (desktop)>"
    assert msg('slack.screenshot.link', viewport='phone') == 'Screenshot on Skopax (phone)'
    assert slack_api.shot_links([], BASE) == ''


def test_slack_update_refuses_a_screenshot_from_another_run(store: Store):
    # given
    seeded(store, slackPost={'channel': 'C1', 'ts': THREAD, 'at': '2026-10-21T09:00:00Z', 'by': 'auto'})
    updates = []
    b = Toolbox(sources(), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: updates.append(key)), [KEY], 'live', NOW, BASE)

    # when
    out, err = b.call('slack_update', {'key': KEY, 'text': 'x', 'screenshots': ['other-run']})

    # then
    assert err and 'not taken in this run' in out
    assert updates == []


def replier(store: Store, listen: str = 'live'):
    slack, post, api = fake_slack()
    attached = []
    r = Replier(store, slack, dataclasses.replace(settings(), slack_listen=listen), clock=lambda: NOW, attach=lambda channel, thread, ids, where, run_id: attached.append((channel, thread, ids, where, run_id)))
    return r, attached


def test_a_live_slack_answer_attaches_the_run_s_screenshots_in_its_thread(store: Store):
    # given
    r, attached = replier(store)
    act = slack_action(store) | {'screenshots': ['s1', 's2'], 'runId': 'run-7'}

    # when
    r.reply(act, 'done', 'The page looks empty.')

    # then
    assert attached == [('C1', THREAD, ['s1', 's2'], 'slack ask', 'run-7')]


def test_a_shadow_or_failed_slack_answer_attaches_nothing(store: Store):
    # given
    shadow, seen_shadow = replier(store, 'shadow')
    live, seen_live = replier(store)

    # when
    shadow.reply(slack_action(store) | {'screenshots': ['s1']}, 'done', 'x')
    live.reply(slack_action(store) | {'screenshots': ['s1']}, 'failed', '')

    # then
    assert seen_shadow == [] and seen_live == []


def test_the_app_hands_the_attacher_to_the_slack_replier(store: Store):
    # when
    with TestClient(create_app(settings(), store)) as c:
        attach = c.app.state.replier.attach

    # then
    assert isinstance(attach, Attacher) and attach.base_url == BASE


def test_the_api_serves_a_stored_screenshot_and_404s_an_unknown_one(store: Store):
    # given
    shot = stored_shot(store)

    # when
    with TestClient(create_app(settings(), store)) as c:
        ok = c.get(f'/api/screenshots/{shot}')
        missing = c.get('/api/screenshots/nope')

    # then
    assert ok.status_code == 200 and ok.content == JPEG
    assert ok.headers['content-type'] == 'image/jpeg' and 'private' in ok.headers['cache-control']
    assert missing.status_code == 404


def test_the_screenshot_switch_and_cap_are_core_settings_with_their_env_names():
    # given
    from skopax.config import load_settings
    from skopax.tunables import INV, SLACK, STATIC

    # when
    rows = {t.key: t for t in STATIC}
    loaded = load_settings(env={'SCREENSHOT_MAX_PER_DAY': '12', 'SLACK_ATTACH_SCREENSHOTS': 'false'})
    default = load_settings(env={})

    # then
    cap, switch = rows['screenshot.max-per-day'], rows['slack.attach-screenshots']
    assert (cap.field, cap.group, cap.kind, cap.low, cap.high, cap.live) == ('screenshot_max_per_day', INV, 'int', 0, 500, True)
    assert (switch.field, switch.group, switch.kind, switch.live) == ('slack_attach_screenshots', SLACK, 'bool', False)
    assert (default.screenshot_max_per_day, default.slack_attach_screenshots) == (60, True)
    assert (loaded.screenshot_max_per_day, loaded.slack_attach_screenshots) == (12, False)


def test_the_stored_attach_lines_stay_english_and_only_the_slack_link_is_outbound():
    # given
    from skopax.messages import MESSAGES, OUTBOUND

    # when
    stored = sorted(k for k in MESSAGES if k.startswith('screenshot.attach.'))

    # then
    assert stored == ['screenshot.attach.done', 'screenshot.attach.gone', 'screenshot.attach.no_scope', 'screenshot.attach.no_slack', 'screenshot.attach.none',
                      'screenshot.attach.off', 'screenshot.attach.skipped']
    assert 'screenshot' not in OUTBOUND and not any(k.startswith('slack.attach.') for k in MESSAGES)
    assert 'slack.screenshot.link' in MESSAGES and 'slack' in OUTBOUND


def test_a_failed_activity_write_never_fails_the_attach(store: Store, monkeypatch, caplog):
    # given
    from pymongo.errors import AutoReconnect
    shot = stored_shot(store)
    slack, _, api = fake_slack()

    def broken(*args, **kwargs):
        raise AutoReconnect('mongo is away at secret-host')

    monkeypatch.setattr(store, 'add_activity', broken)

    # when
    line = Attacher(store, slack, True, BASE)('C1', THREAD, [shot], KEY, run_id='run-1')

    # then
    assert line == 'attached 1 screenshot to the #ops thread' and api.methods().count('upload') == 1
    assert all('secret-host' not in r.getMessage() for r in caplog.records)
