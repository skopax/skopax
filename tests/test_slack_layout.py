import json
import logging
import re
import threading
import time

from pymongo.errors import PyMongoError

from tests.conftest import AUTH, anomaly, state
from tests.fake_slack import FakePost, FakeSlackApi, fake_slack
from tests.test_slack import FakeSlack, client_with
from tests.test_slack_clicks import clicks, ephemerals
from tests.test_slackevents import CARD_TS, KEY, card_with_post
from skopax.cardposts import CardPosts, drawn_sig
from skopax.engine import Applied, PassResult, anomaly as found_card
from skopax.messages import MESSAGES, msg
from skopax.postdeploy import SERVER_BY
from skopax.slack import AUTO_BY, CARD_MENU, KNOWN_VIEW, POST_ACTIONS, Slack, SlackDown, SlackError, anomaly_message, slowed
from skopax.store import Store

LINK = 'https://skopax.example.com/c/14'
CHAT = 'https://skopax.example.com/#t=chat/new/14'
CARD = {'key': 'log|checkout-service|ERROR|boom', 'num': 14, 'sev': 'warn', 'status': 'open', 'svc': 'checkout-service', 'label': 'ERROR lines', 'sub': 'east',
        'now': 12, 'd1': 3, 'd7': 40, 'unit': '', 'firstSeen': '2026-11-11T07:00:00Z', 'msg': 'boom at line 69', 'links': [{'label': 'Logs', 'url': 'https://logs.example.com/x'}]}
SINCE = 'since 11 Nov 07:00 UTC'
POSTED_TS = '1791900000.000100'
BASE = 'https://skopax.example.com'
SHOWN = {'kind': 'opened', 'by': 'server', 'at': '2026-11-11T07:00:00Z'}


def posted(store: Store, key: str, ts: str = CARD_TS, mute: str | None = None, **extra) -> None:
    drawn = {'sig': drawn_sig(store.anomaly(key), mute), 'at': '2026-11-11T07:00:00Z'}
    store.slack_posted(key, {'channel': 'C1', 'ts': ts, 'at': '2026-11-11T07:00:00Z', 'by': 'server', 'head': SHOWN, 'drawn': drawn, **extra})


class Recorder(FakeSlackApi):
    def __init__(self, before=None, **kw):
        super().__init__(**kw)
        self.before = before

    def __call__(self, token: str, method: str, payload: dict) -> dict:
        if self.before is not None:
            self.before(method, payload)
        return super().__call__(token, method, payload)


def rewrites(api: FakeSlackApi) -> list[dict]:
    return [p for m, p in api.calls if m == 'chat.update']


def posts_of(store: Store, slack, **over) -> CardPosts:
    return CardPosts(store, slack, BASE, autostart=False, **over)


def settled(client) -> None:
    assert client.app.state.card_posts.drain(5)


def head_of(message: dict) -> str:
    return message['blocks'][0]['text']['text']


def bar_of(message: dict) -> list[str]:
    return [e['text'] for e in message['blocks'][-1]['elements']]


def menu_of(message: dict) -> list[tuple]:
    accessory = message['blocks'][0].get('accessory')
    return [(o['text']['text'], o['value'], o.get('url')) for o in accessory['options']] if accessory else []


def texts(message: dict) -> list[str]:
    out = []
    for b in message['blocks']:
        out += [b['text']['text']] if b.get('text') else []
        out += [e['text'] for e in b.get('elements') or [] if e.get('type') == 'mrkdwn']
    return out


def test_an_open_card_post_reads_who_did_what_then_the_title_the_body_and_one_bar():
    # when
    message = anomaly_message(CARD, LINK, 'server', None, 'ask1', CHAT)

    # then
    blocks = message['blocks']
    assert [b['type'] for b in blocks] == ['section', 'section', 'context', 'context']
    assert head_of(message) == ':large_orange_circle: Skopax opened a card \u00b7 Warning'
    assert blocks[0]['accessory']['type'] == 'overflow' and blocks[0]['accessory']['action_id'] == CARD_MENU
    assert menu_of(message) == [('Take', 'take:ask1', None), ('Resolve', 'resolve:ask1', None), ('Known noise', 'known:ask1', None), ('Ask in chat', 'chat', CHAT)]
    assert blocks[1]['text']['text'] == f'*<{LINK}|#14 checkout-service / ERROR lines \u00b7 east>*'
    assert blocks[2]['elements'][0]['text'] == '`boom at line 69`'
    assert bar_of(message) == [f'Open \u00b7 Warning \u00b7 {SINCE}', 'Now *12* \u00b7 yesterday 3 \u00b7 last week 40', '<https://logs.example.com/x|Logs>']
    assert not any(b['type'] == 'actions' for b in blocks), 'the linked title replaces the Open button'
    assert message['text'] == 'Warning \u00b7 checkout-service / ERROR lines \u00b7 east', 'the notification text stays one plain line'


def test_a_person_who_sends_the_card_is_named_in_the_headline_and_the_ai_summary_leads_the_body():
    # given
    card = CARD | {'slackDraft': {'text': 'p95 <2 s> & rising', 'headline': 'Checkout is slow'}}

    # when
    sent = anomaly_message(card, LINK, 'alice@example.com', 'p95 <2 s> & rising')
    auto = anomaly_message(CARD, LINK, 'auto \u00b7 Claude draft')

    # then
    assert head_of(sent) == ':large_orange_circle: alice shared a card \u00b7 Warning'
    assert [b['type'] for b in sent['blocks']] == ['section', 'section', 'section', 'context', 'context']
    assert sent['blocks'][2]['text']['text'] == '*Checkout is slow*\np95 &lt;2 s&gt; &amp; rising'
    assert head_of(auto) == ':large_orange_circle: Skopax opened a card \u00b7 Warning'
    assert 'accessory' not in auto['blocks'][0], 'no menu when there is nothing to pick'


def test_taken_resolved_and_cleared_cards_change_the_headline_and_the_bar():
    # when
    taken = anomaly_message(CARD | {'triage': 'progress', 'owner': 'bob@example.com', 'tags': [{'id': 'developer', 'by': 'pod', 'at': 'x'}]}, LINK, 'server', None, 'ask1', CHAT,
                            head={'kind': 'took', 'by': 'alice@example.com'}, kinds=('resolve', 'known'))
    resolved = anomaly_message(CARD | {'triage': 'handled'}, LINK, 'server', None, 'ask1', CHAT, head={'kind': 'resolved', 'by': 'token:ci-bot'}, kinds=())
    cleared = anomaly_message(CARD | {'status': 'cleared'}, LINK, 'server', head={'kind': 'cleared'})
    reopened = anomaly_message(CARD, LINK, 'server', head={'kind': 'reopened', 'by': 'system'})
    assigned = anomaly_message(CARD | {'owner': 'Bob Smith'}, LINK, 'server', head={'kind': 'assigned', 'by': 'alice@example.com', 'name': 'Bob Smith'})
    known = anomaly_message(CARD, LINK, 'server', head={'kind': 'known', 'by': 'alice@example.com'}, mute='known')

    # then
    assert [head_of(m) for m in (taken, resolved, cleared, reopened, assigned, known)] == [
        ':large_orange_circle: alice took #14', ':white_check_mark: ci-bot resolved #14', ':large_yellow_circle: #14 stopped firing \u00b7 unassigned', ':large_orange_circle: #14 reopened',
        ':large_orange_circle: alice assigned #14 to Bob Smith', ':white_check_mark: alice marked #14 as known noise']
    assert bar_of(taken)[0] == f'In progress \u00b7 Warning \u00b7 Assignee bob \u00b7 Tags: Developer \u00b7 {SINCE}'
    assert [bar_of(m)[0].split(' \u00b7 ')[0] for m in (resolved, cleared, reopened, known)] == ['Resolved', 'Stopped firing', 'Open', 'Known noise']
    assert menu_of(taken) == [('Resolve', 'resolve:ask1', None), ('Known noise', 'known:ask1', None), ('Ask in chat', 'chat', CHAT)]
    assert menu_of(resolved) == [('Ask in chat', 'chat', CHAT)]


def test_the_menu_never_holds_more_than_five_options_and_links_stay_in_the_bar():
    # given
    many = CARD | {'links': [{'label': f'Link {i}', 'url': f'https://logs.example.com/{i}'} for i in range(9)]}

    # when
    menus = [menu_of(anomaly_message(many, LINK, 'server', None, buttons, chat, kinds=kinds))
             for buttons in (None, 'ask1') for chat in (None, CHAT) for kinds in ((), ('take',), POST_ACTIONS)]
    full = anomaly_message(many, LINK, 'server', None, 'ask1', CHAT)

    # then
    assert max(len(m) for m in menus) <= 5
    assert all(url in (None, CHAT) for m in menus for _, _, url in m)
    assert bar_of(full)[2] == ' \u00b7 '.join(f'<https://logs.example.com/{i}|Link {i}>' for i in range(5))
    assert len(full['blocks'][-1]['elements']) <= 10


def test_the_headline_the_title_and_the_bar_are_escaped():
    # given
    card = CARD | {'svc': '<!channel> & co', 'label': '*x* <@U0AAA1111>', 'sub': '<east>', 'owner': '<b>@example.com', 'msg': 'a `b` <c>',
                   'links': [{'label': '<evil> & co', 'url': 'https://logs.example.com/a?b=1&c=2'}, {'label': 'pipe', 'url': 'https://logs.example.com/a|b'},
                             {'label': 'angle', 'url': 'https://logs.example.com/<x>'}, {'label': 'script', 'url': 'javascript:alert(1)'}]}

    # when
    message = anomaly_message(card | {'num': None}, LINK, 'server', head={'kind': 'took', 'by': 'mallory<x>@example.com'})

    # then
    assert head_of(message) == ':large_orange_circle: mallory&lt;x&gt; took &lt;!channel&gt; &amp; co / *x* &lt;@U0AAA1111&gt;'
    assert message['blocks'][1]['text']['text'] == f'*<{LINK}|&lt;!channel&gt; &amp; co / *x* &lt;@U0AAA1111&gt; \u00b7 &lt;east&gt;>*'
    assert bar_of(message)[0] == f'Open \u00b7 Warning \u00b7 Assignee &lt;b&gt; \u00b7 {SINCE}'
    assert bar_of(message)[2] == '<https://logs.example.com/a?b=1&amp;c=2|&lt;evil&gt; &amp; co>'
    assert not any(re.search(r'<[!@]', t) for t in texts(message)), 'no mention or broadcast leaks through'


def known_pick(ask_id: str, user: str = 'U1') -> dict:
    return {'type': 'block_actions', 'trigger_id': 'T0TRIGGER', 'user': {'id': user}, 'channel': {'id': 'C1'}, 'message': {'ts': CARD_TS, 'text': 'x', 'blocks': []},
            'actions': [{'type': 'overflow', 'action_id': CARD_MENU, 'selected_option': {'value': f'known:{ask_id}'}}]}


def menu_pick(kind: str, ask_id: str) -> dict:
    return known_pick(ask_id) | {'actions': [{'type': 'overflow', 'action_id': CARD_MENU, 'selected_option': {'value': f'{kind}:{ask_id}'}}]}


def submit(view: dict, user: str = 'U1') -> dict:
    return {'type': 'view_submission', 'user': {'id': user}, 'view': {'callback_id': view['callback_id'], 'private_metadata': view['private_metadata']}}


def menu_ask(store: Store) -> dict:
    card_with_post(store)
    return store.add_action({'type': 'post-buttons', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardActions': {k: {'key': KEY, 'action': k} for k in POST_ACTIONS}}, 'done', 'menu')


def test_known_noise_opens_a_confirm_window_and_only_its_submit_applies(store: Store):
    # given
    doc = menu_ask(store)
    box, applier, api = clicks(store)

    # when
    picked = box.handle(known_pick(doc['id']))
    [(_, opened)] = [(m, p) for m, p in api.calls if m == 'views.open']
    claimed_before = store.get_action(doc['id']).get('cardActionApplied')
    stranger = box.handle(submit(opened['view'], user='U9'))
    done = box.handle(submit(opened['view']))

    # then
    assert picked == 'confirm-open' and opened['trigger_id'] == 'T0TRIGGER'
    view = opened['view']
    assert (view['type'], view['callback_id'], view['title']['text'], view['submit']['text']) == ('modal', KNOWN_VIEW, msg('slack.known.title'), msg('slack.known.ok'))
    assert json.loads(view['private_metadata']) == {'ask': doc['id'], 'channel': 'C1', 'ts': CARD_TS}
    assert claimed_before is None, 'picking the option alone applies and claims nothing'
    assert stranger == 'not-operator'
    assert done == 'applied'
    assert [(c['type'], c['key'], c['requestedBy'], c['source']) for c in applier.calls] == [('known', KEY, 'adam.baker@example.com', 'slack')]
    assert [m for m, _ in api.calls if m == 'chat.update'] == [], 'the in-place refresh, not the click, rewrites the post'


def test_a_known_noise_window_that_slack_does_not_open_applies_nothing(store: Store):
    # given
    doc = menu_ask(store)
    box, applier, api = clicks(store)
    api.fail.add('views.open')

    # when
    got = box.handle(known_pick(doc['id']))

    # then
    assert got == 'modal-failed' and applier.calls == []
    assert ephemerals(api) == [msg('slackevents.click.window_failed')]
    assert store.get_action(doc['id']).get('cardActionApplied') is None


def test_take_and_resolve_from_the_menu_apply_at_once_and_leave_the_post_to_the_refresh(store: Store):
    # given
    doc = menu_ask(store)
    box, applier, api = clicks(store)

    # when
    first = box.handle(menu_pick('take', doc['id']))
    second = box.handle(menu_pick('resolve', doc['id']))
    chat = box.handle(menu_pick('chat', doc['id']) | {'actions': [{'type': 'overflow', 'action_id': CARD_MENU, 'selected_option': {'value': 'chat'}}]})

    # then
    assert (first, second, chat) == ('applied', 'claimed', None)
    assert [c['type'] for c in applier.calls] == ['take']
    assert [m for m, _ in api.calls if m in ('chat.update', 'views.open')] == []


def test_a_state_change_updates_the_post_in_place_and_never_reposts_it(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14, now=5, firstSeen='2026-11-11T07:00:00Z')), None)
    transport, api = FakeSlack(), FakeSlackApi()

    # when
    with client_with(store, transport, api=api) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        client.post('/api/actions', json={'type': 'take', 'key': KEY, 'label': 'Take'}, headers=AUTH)
        settled(client)
        client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'label': 'Resolve'}, headers=AUTH)
        settled(client)

    # then
    top = [p for _, p in transport.calls if 'thread_ts' not in p]
    updates = [p for m, p in api.calls if m == 'chat.update']
    assert len(top) == 1, 'the card is posted once'
    assert [(u['channel'], u['ts']) for u in updates] == [('C123', POSTED_TS)] * 2
    assert [head_of(u) for u in updates] == [':large_orange_circle: bootstrap took #14', ':white_check_mark: bootstrap resolved #14']
    assert [bar_of(u)[0].split(' \u00b7 ')[0] for u in updates] == ['In progress', 'Resolved']
    assert store.anomaly(KEY)['slackPost']['head']['kind'] == 'resolved'


def test_assigning_or_tagging_the_card_rewrites_its_post(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14, now=5, firstSeen='2026-11-11T07:00:00Z')), None)
    transport, api = FakeSlack(), FakeSlackApi()

    # when
    with client_with(store, transport, api=api) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        assigned = client.put('/api/assignee', json={'key': KEY, 'name': 'Bob Smith'}, headers=AUTH)
        settled(client)
        tagged = client.post('/api/tags', json={'key': KEY, 'tag': 'developer', 'on': True}, headers=AUTH)
        business = client.put('/api/business', json={'key': KEY, 'text': 'Customers cannot pay.'}, headers=AUTH)
        settled(client)

    # then
    updates = [p for m, p in api.calls if m == 'chat.update']
    assert (assigned.status_code, tagged.status_code, business.status_code) == (200, 200, 200)
    assert [head_of(u) for u in updates] == [':large_orange_circle: bootstrap assigned #14 to Bob Smith'] * 2, 'a tag change keeps the last headline'
    assert [bar_of(u)[0] for u in updates] == [f'Open \u00b7 Warning \u00b7 Assignee Bob Smith \u00b7 {SINCE}', f'Open \u00b7 Warning \u00b7 Assignee Bob Smith \u00b7 Tags: Developer \u00b7 {SINCE}']
    assert len([p for _, p in transport.calls if 'thread_ts' not in p]) == 1
    assert len(updates) == 2, 'a business sentence is not on the post, so saving one rewrites nothing'


def test_a_sweep_that_clears_reopens_escalates_or_auto_resolves_a_card_rewrites_its_post(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14, status='cleared'), anomaly('log|b|ERROR|x', num=15), anomaly('log|c|ERROR|x', num=16, sev='crit'), anomaly('log|d|ERROR|x', num=17, triage='handled')), None)
    for key, ts in ((KEY, CARD_TS), ('log|b|ERROR|x', '1792224000.000201'), ('log|c|ERROR|x', '1792224000.000202'), ('log|d|ERROR|x', '1792224000.000203')):
        posted(store, key, ts)
    slack, post, api = fake_slack()

    # when
    posts = posts_of(store, slack)
    posts.swept(Applied(rev=1, events=[{'key': KEY, 'ev': 'cleared'}, {'key': 'log|other|ERROR|x', 'ev': 'cleared'}, {'key': 'log|b|ERROR|x', 'ev': 'reopened'},
                                       {'key': 'log|c|ERROR|x', 'ev': 'escalated'}, {'key': 'log|d|ERROR|x', 'ev': 'new'}], push=[], resolved=[{'key': 'log|d|ERROR|x'}]))
    posts.run_batch()

    # then
    assert [(u['ts'], head_of(u)) for u in rewrites(api)] == [(CARD_TS, ':large_yellow_circle: #14 stopped firing \u00b7 unassigned'), ('1792224000.000201', ':large_orange_circle: #15 reopened'),
                                                             ('1792224000.000202', ':red_circle: Skopax opened a card \u00b7 Critical'),
                                                             ('1792224000.000203', ':white_check_mark: Skopax resolved #17')]
    assert bar_of(rewrites(api)[2])[0].startswith('Open \u00b7 Critical'), 'an escalation redraws the bar and keeps the headline'
    assert post.calls == []


def test_investigator_triage_and_jira_sweeps_rewrite_the_headline(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14, triage='handled'), anomaly('log|b|ERROR|x', num=15, triage='progress'), anomaly('log|c|ERROR|x', num=16)), None)
    for key, ts in ((KEY, CARD_TS), ('log|b|ERROR|x', '1792224000.000201'), ('log|c|ERROR|x', '1792224000.000202')):
        posted(store, key, ts)
    slack, _, api = fake_slack()
    posts = posts_of(store, slack)

    # when
    posts.triaged(KEY, {'triage': None})
    posts.run_batch()
    posts.triaged(KEY, {'triage': 'handled'})
    posts.run_batch()
    posts.jira([{'card': 'log|b|ERROR|x', 'issue': 'OPS-123'}, {'card': 'log|c|ERROR|x', 'issue': 'OPS-124', 'closed': 'Done'}])
    posts.run_batch()

    # then
    assert [head_of(u) for u in rewrites(api)] == [':white_check_mark: Skopax resolved #14', ':white_check_mark: Skopax resolved #14', ':large_orange_circle: Skopax took #15',
                                                  ':large_orange_circle: #16 reopened']


def test_the_rewritten_menu_follows_the_card_and_drops_triage_once_the_post_was_used(store: Store):
    # given
    ask = store.add_action({'type': 'post-buttons', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardActions': {k: {'key': KEY, 'action': k} for k in POST_ACTIONS}}, 'done', 'menu')
    store.save(state(anomaly(KEY, num=14, triage='progress')), None)
    posted(store, KEY, menu=ask['id'])
    slack, _, api = fake_slack()
    posts = posts_of(store, slack)

    # when
    posts.refresh(KEY)
    posts.run_batch()
    store.claim_card_action(ask['id'], 'alice@example.com')
    posts.refresh(KEY)
    posts.run_batch()

    # then
    assert [[v for _, v, _ in menu_of(u)] for u in rewrites(api)] == [[f"resolve:{ask['id']}", f"known:{ask['id']}", 'chat'], ['chat']], 'with the listener off no fresh menu is made'


def test_a_rewrite_keeps_the_summary_that_was_sent_after_known_noise_drops_the_draft(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14, now=5, firstSeen='2026-11-11T07:00:00Z')), None)
    transport, api = FakeSlack(), FakeSlackApi()

    # when
    with client_with(store, transport, api=api) as client:
        client.put('/api/slack-drafts', json={'key': KEY, 'text': 'Checkout fails on retry.'}, headers=AUTH)
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        client.post('/api/actions', json={'type': 'known', 'key': KEY, 'label': 'Known noise'}, headers=AUTH)
        settled(client)

    # then
    [update] = rewrites(api)
    assert 'slackDraft' not in store.anomaly(KEY)
    assert head_of(update) == ':white_check_mark: bootstrap marked #14 as known noise'
    assert update['blocks'][2]['text']['text'] == 'Checkout fails on retry.' and update['text'] == 'Checkout fails on retry.'
    assert bar_of(update)[0].startswith('Known noise \u00b7 Warning') and menu_of(update) == [('Ask in chat', 'chat', f'{BASE}/#t=chat/new/14')], 'a rewrite links through SKOPAX_BASE_URL'


def test_a_known_noise_card_sent_by_hand_reads_known_noise_from_its_first_post(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14, now=5)), None)
    store.mute(KEY, 'marked as known noise on the page', None, 'known')
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)

    # then
    assert bar_of(transport.calls[0][1])[0].startswith('Known noise \u00b7 Warning')


def test_slack_down_stops_a_batch_of_rewrites_after_the_first_card(store: Store):
    # given
    calls = []

    def down(token: str, method: str, payload: dict) -> dict:
        calls.append(method)
        raise SlackDown('Slack did not answer (URLError)')
    store.save(state(anomaly(KEY, num=14, status='cleared'), anomaly('log|b|ERROR|x', num=15, status='cleared')), None)
    posted(store, KEY)
    posted(store, 'log|b|ERROR|x', '1792224000.000201')
    slack = Slack('xoxb-test', '#ops', FakePost(), down)

    # when
    posts = posts_of(store, slack)
    posts.swept(Applied(rev=1, events=[{'key': KEY, 'ev': 'cleared'}, {'key': 'log|b|ERROR|x', 'ev': 'cleared'}], push=[]))
    done = posts.run_batch()

    # then
    assert done == 0 and calls == ['chat.update']


def test_a_card_posted_in_another_cards_thread_is_rewritten_through_its_own_reply(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14, triage='progress', owner='alice@example.com')), None)
    store.slack_posted(KEY, {'channel': 'C1', 'ts': CARD_TS, 'replyTs': '1792224000.000900', 'threadOf': 'log|host|ERROR|y', 'at': '2026-11-11T07:00:00Z', 'by': 'server',
                             'head': {'kind': 'opened', 'by': 'server', 'at': '2026-11-11T07:00:00Z'}})
    slack, post, api = fake_slack()

    # when
    posts = posts_of(store, slack)
    posts.after({'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com'})
    posts.run_batch()

    # then
    [update] = [p for m, p in api.calls if m == 'chat.update']
    assert (update['ts'], head_of(update)) == ('1792224000.000900', ':large_orange_circle: alice took #14')


def test_a_post_from_before_the_new_layout_is_left_as_it_is(store: Store):
    # given
    card_with_post(store)
    slack, post, api = fake_slack()

    # when
    posts = posts_of(store, slack)
    posts.after({'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com'})
    posts.run_batch()

    # then
    assert [m for m, _ in api.calls if m == 'chat.update'] == []


def test_two_changes_of_one_card_in_a_batch_make_one_update(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14, triage='handled')), None)
    posted(store, KEY)
    slack, _, api = fake_slack()
    posts = posts_of(store, slack)

    # when
    posts.after({'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com'})
    posts.after({'type': 'resolve', 'key': KEY, 'requestedBy': 'bob@example.com'})
    posts.refresh(KEY)
    done = posts.run_batch()

    # then
    assert done == 1 and [head_of(u) for u in rewrites(api)] == [':white_check_mark: bob resolved #14'], 'the latest change wins'
    assert store.anomaly(KEY)['slackPost']['head']['kind'] == 'resolved'


def test_the_push_goes_out_before_any_rewrite_and_the_sweep_never_waits_for_one(store: Store):
    # given
    order = []
    api = Recorder(before=lambda method, payload: (threading.Event().wait(0.3), order.append('rewrite')) if method == 'chat.update' else None)
    transport = FakeSlack()
    store.save(state(anomaly(KEY, num=14, src='prom', short='ERROR boom', firstSeen='2026-11-11T07:00:00Z')), None)
    posted(store, KEY)
    new = found_card('prom', 'err', 'shop', 'payment-service', '', 'crit', 50, 0, 0, '', 'ERROR logs', 'errors')

    # when
    with client_with(store, transport, api=api, slack_push_user='U0AAA1111') as client:
        client.app.state.server_apply([], time.time(), None)
        client.app.state.server_apply([PassResult('prom', True, 'ok', [new], {}, ('prom',))], time.time(), None)
        order.append('returned')
        pushed = [p['channel'] for _, p in transport.calls]
        settled(client)

    # then
    assert 'U0AAA1111' in pushed, 'the critical push went out inside the sweep'
    assert order == ['returned', 'rewrite'], 'the sweep returned before the rewrite of the cleared card ran'
    assert head_of(rewrites(api)[0]) == ':large_yellow_circle: #14 stopped firing \u00b7 unassigned'


def test_a_page_glob_known_noise_answers_without_waiting_on_slack(store: Store):
    # given
    gate = threading.Event()
    api = Recorder(before=lambda method, payload: gate.wait(5) if method == 'chat.update' else None)
    keys = ('log|checkout-service|ERROR|a', 'log|checkout-service|ERROR|b', 'log|other|ERROR|c')
    store.save(state(*(anomaly(k, num=14 + i) for i, k in enumerate(keys))), None)
    for i, k in enumerate(keys):
        posted(store, k, f'1792224000.00030{i}')

    # when
    with client_with(store, FakeSlack(), api=api) as client:
        started = time.monotonic()
        res = client.post('/api/actions', json={'type': 'known', 'key': 'log|checkout-service|*', 'label': 'Known noise'}, headers=AUTH)
        answered = time.monotonic() - started
        held = len(rewrites(api))
        gate.set()
        settled(client)

    # then
    assert res.status_code == 201 and res.json()['status'] == 'done'
    assert answered < 2 and held == 0, 'the page answered while every rewrite still waited on Slack'
    assert sorted(head_of(u) for u in rewrites(api)) == [':white_check_mark: bootstrap marked #14 as known noise', ':white_check_mark: bootstrap marked #15 as known noise']


def test_a_rewrite_slack_did_not_answer_is_tried_once_more_on_the_next_batch(store: Store):
    # given
    tries = {'n': 0}

    def flaky(token: str, method: str, payload: dict) -> dict:
        tries['n'] += 1
        if tries['n'] == 1:
            raise SlackDown('Slack did not answer (HTTPError)')
        return {'ok': True, 'channel': payload['channel'], 'ts': payload['ts']}

    def down(token: str, method: str, payload: dict) -> dict:
        raise SlackDown('Slack did not answer (TimeoutError)')
    store.save(state(anomaly(KEY, num=14)), None)
    posted(store, KEY)
    once = posts_of(store, Slack('xoxb-test', '#ops', FakePost(), flaky), retry_after_s=0)
    never = posts_of(store, Slack('xoxb-test', '#ops', FakePost(), down), retry_after_s=0)

    # when
    once.after({'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com'})
    got_once = [once.run_batch() for _ in range(3)]
    never.after({'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com'})
    got_never = [never.run_batch() for _ in range(3)]

    # then
    assert got_once == [0, 1, 0] and tries['n'] == 2
    assert got_never == [0, 0, 0], 'a second failure is not tried again in a loop; the reconcile picks it up later'


def test_a_batch_stops_at_its_deadline_and_the_rest_waits_for_the_next_one(store: Store):
    # given
    clock = {'t': 0.0}
    keys = ('log|a|ERROR|1', 'log|a|ERROR|2', 'log|a|ERROR|3')
    store.save(state(*(anomaly(k, num=20 + i, status='cleared') for i, k in enumerate(keys))), None)
    for i, k in enumerate(keys):
        posted(store, k, f'1792224000.00040{i}')
    api = Recorder(before=lambda method, payload: clock.__setitem__('t', clock['t'] + 10) if method == 'chat.update' else None)
    slack = Slack('xoxb-test', '#ops', FakePost(), api)
    posts = posts_of(store, slack, timer=lambda: clock['t'], deadline_s=15, retry_after_s=0)

    # when
    posts.swept(Applied(rev=1, events=[{'key': k, 'ev': 'cleared'} for k in keys], push=[]))
    first = posts.run_batch()
    second = posts.run_batch()
    third = posts.run_batch()

    # then
    assert (first, second, third) == (1, 1, 1) and len(rewrites(api)) == 3, 'a 10 s call that could end past the 15 s deadline waits for the next batch'


def test_a_card_key_with_a_star_marks_only_that_card_and_a_glob_unmute_rewrites_every_match(store: Store):
    # given
    star, other = 'log|checkout-service|ERROR|SELECT * FROM orders', 'log|checkout-service|ERROR|SELECT id FROM orders'
    store.save(state(anomaly(star, num=14), anomaly(other, num=15)), None)
    posted(store, star)
    posted(store, other, '1792224000.000201')
    slack, _, api = fake_slack()
    posts = posts_of(store, slack)

    # when
    store.mute(star, 'marked as known noise on the page', None, 'known')
    posts.after({'type': 'known', 'key': star, 'requestedBy': 'alice@example.com'})
    posts.run_batch()
    known = [head_of(u) for u in rewrites(api)]
    store.unmute(star)
    posts.after({'type': 'unmute', 'key': 'log|checkout-service|*', 'requestedBy': 'alice@example.com'})
    posts.run_batch()

    # then
    assert known == [':white_check_mark: alice marked #14 as known noise']
    assert store.anomaly(other)['slackPost']['head']['kind'] == 'reopened' and len(rewrites(api)) == 3


def test_a_slack_take_leaves_a_fresh_menu_so_resolve_still_works(store: Store):
    # given
    doc = menu_ask(store)
    posted(store, KEY, menu=doc['id'])
    box, applier, _ = clicks(store)
    taken = box.handle(menu_pick('take', doc['id']))
    store.triage(KEY, 'progress', owner='adam.baker@example.com')
    slack, _, api = fake_slack()
    posts = posts_of(store, slack, menu_on=lambda: True)

    # when
    posts.after(applier.calls[0])
    posts.run_batch()
    fresh = menu_of(rewrites(api)[0])
    new_id = fresh[0][1].split(':', 1)[1]
    resolved = box.handle(menu_pick('resolve', new_id))
    stale = box.handle(menu_pick('resolve', doc['id']))

    # then
    assert taken == 'applied' and new_id != doc['id']
    assert [v for _, v, _ in fresh] == [f'resolve:{new_id}', f'known:{new_id}', 'chat']
    assert store.anomaly(KEY)['slackPost']['drawn']['menu'] == new_id
    assert (resolved, stale) == ('applied', 'claimed') and [c['type'] for c in applier.calls] == ['take', 'resolve']


def test_a_menu_that_cannot_be_read_is_logged_and_offers_no_triage(store: Store, caplog, monkeypatch):
    # given
    store.save(state(anomaly(KEY, num=14)), None)
    posted(store, KEY, menu='a0')
    slack, _, api = fake_slack()
    posts = posts_of(store, slack)

    def broken(action_id: str):
        raise PyMongoError('down')
    monkeypatch.setattr(store, 'get_action', broken)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.cardposts'):
        posts.refresh(KEY)
        posts.run_batch()

    # then
    assert [v for _, v, _ in menu_of(rewrites(api)[0])] == ['chat']
    assert 'the menu of the Slack post of log|a|ERROR|x could not be read: PyMongoError' in caplog.text


def test_a_reconcile_redraws_posts_whose_card_changed_without_a_trigger(store: Store):
    # given
    state_now = state(anomaly(KEY, num=14), anomaly('log|b|ERROR|x', num=15, muted=None), anomaly('log|c|ERROR|x', num=16))
    store.save(state_now, None)
    posted(store, KEY)
    posted(store, 'log|b|ERROR|x', '1792224000.000201', mute='mute')
    posted(store, 'log|c|ERROR|x', '1792224000.000202')
    store.set_tagging(KEY, {'tags': [{'id': 'developer', 'by': 'rule', 'at': '2026-11-11T08:00:00Z'}]})
    down = {'on': True}

    def api(token: str, method: str, payload: dict) -> dict:
        calls.append(payload['ts'])
        if down['on']:
            raise SlackDown('Slack did not answer (URLError)')
        return {'ok': True, 'channel': payload['channel'], 'ts': payload['ts']}
    calls = []
    posts = posts_of(store, Slack('xoxb-test', '#ops', FakePost(), api), retry_after_s=3600)

    # when
    posts.reconcile()
    outage = posts.run_batch()
    down['on'] = False
    posts.reconcile()
    redrawn = posts.run_batch()
    posts.reconcile()
    again = posts.run_batch()

    # then
    assert (outage, redrawn, again) == (0, 2, 0)
    assert sorted(set(calls)) == [CARD_TS, '1792224000.000201'], 'the tag set by a rule and the mute that ran out are redrawn; the unchanged post is not'


def test_an_auto_sent_post_says_who_drafted_its_body():
    # given
    card = CARD | {'slackDraft': {'text': 'Checkout fails on retry.'}}

    # when
    server = anomaly_message(card | {'slackDraft': {'text': 'x', 'by': SERVER_BY}}, LINK, AUTO_BY, 'x')
    investigator = anomaly_message(card | {'slackDraft': {'text': 'x', 'by': 'pod'}}, LINK, AUTO_BY, 'x')
    person = anomaly_message(card | {'slackDraft': {'text': 'x', 'by': 'pod'}}, LINK, 'alice@example.com', 'x')
    token = anomaly_message(card | {'slackDraft': {'text': 'x', 'by': 'token:release-bot'}}, LINK, AUTO_BY, 'x')
    unnamed = anomaly_message(card | {'slackDraft': {'text': 'x', 'by': 'token'}}, LINK, AUTO_BY, 'x')
    core = anomaly_message(card | {'slackDraft': {'text': 'x', 'by': 'token:bootstrap'}}, LINK, AUTO_BY, 'x')

    # then
    assert bar_of(server)[0] == f'Open \u00b7 Warning \u00b7 {SINCE} \u00b7 Skopax \u00b7 auto-sent'
    assert bar_of(investigator)[0] == f'Open \u00b7 Warning \u00b7 {SINCE} \u00b7 Claude investigator \u00b7 auto-sent'
    assert bar_of(token)[0] == f'Open \u00b7 Warning \u00b7 {SINCE} \u00b7 release-bot \u00b7 auto-sent'
    assert bar_of(unnamed)[0] == f'Open \u00b7 Warning \u00b7 {SINCE} \u00b7 Claude investigator \u00b7 auto-sent'
    assert bar_of(core)[0] == f'Open \u00b7 Warning \u00b7 {SINCE} \u00b7 Claude investigator \u00b7 auto-sent'
    assert bar_of(person)[0] == f'Open \u00b7 Warning \u00b7 {SINCE}', 'a person who shares it is named in the headline'


def test_the_menu_chat_option_has_its_own_message_key():
    # then
    assert 'slack.chat' not in MESSAGES and MESSAGES['slack.menu.chat'] == 'Ask in chat'


def test_the_draft_tool_says_the_title_links_the_card():
    # given
    from skopax.investigator.tools import WRITE_TOOLS
    described = next(t['description'] for t in WRITE_TOOLS if t['name'] == 'slack_draft')

    # then
    assert 'the title of the post links the card' in described and 'button' not in described


def test_the_slack_posts_job_redraws_a_post_its_card_moved_away_from(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14)), None)
    posted(store, KEY, mute='mute')
    transport, api = FakeSlack(), FakeSlackApi()

    # when
    with client_with(store, transport, api=api) as client:
        job = next(j for j in client.app.state.scheduler.jobs if j.name == 'slack-posts')
        job.run(time.time())
        settled(client)

    # then
    [update] = rewrites(api)
    assert bar_of(update)[0].startswith('Open \u00b7 Warning'), 'a mute that ran out leaves the post reading Muted until the job redraws it'


def test_a_post_slack_can_never_update_is_dropped_from_the_reconcile(store: Store):
    # given
    calls = []

    def gone(token: str, method: str, payload: dict) -> dict:
        calls.append(method)
        raise SlackError('message_not_found', code='message_not_found')
    store.save(state(anomaly(KEY, num=14)), None)
    posted(store, KEY, mute='mute')
    posts = posts_of(store, Slack('xoxb-test', '#ops', FakePost(), gone))

    # when
    posts.after({'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com'})
    first = posts.run_batch()
    posts.reconcile()
    posts.after({'type': 'resolve', 'key': KEY, 'requestedBy': 'alice@example.com'})
    later = posts.run_batch()

    # then
    assert (first, later) == (0, 0) and calls == ['chat.update'], 'one try, then the post is left alone'
    assert store.anomaly(KEY)['slackPost']['drawn']['gone'] == 'message_not_found'


def test_a_rewrite_starts_only_while_a_whole_slack_call_still_fits_the_batch_deadline(store: Store):
    # given
    clock = {'t': 0.0}
    keys = ('log|a|ERROR|1', 'log|a|ERROR|2', 'log|a|ERROR|3')
    store.save(state(*(anomaly(k, num=20 + i, status='cleared') for i, k in enumerate(keys))), None)
    for i, k in enumerate(keys):
        posted(store, k, f'1792224000.00050{i}')
    api = Recorder(before=lambda method, payload: clock.__setitem__('t', clock['t'] + 4) if method == 'chat.update' else None)
    posts = posts_of(store, Slack('xoxb-test', '#ops', FakePost(), api), timer=lambda: clock['t'], deadline_s=15, retry_after_s=0)

    # when
    posts.swept(Applied(rev=1, events=[{'key': k, 'ev': 'cleared'} for k in keys], push=[]))
    first = posts.run_batch()
    took = clock['t']

    # then
    assert first == 2 and took <= 15, 'the third call could run past 15 s, so it waits for the next batch'


def test_drain_waits_for_a_rewrite_that_is_waiting_to_be_tried_again(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14, status='cleared')), None)
    posted(store, KEY)
    answers = iter([SlackDown('Slack did not answer (TimeoutError)')])

    class Flaky(FakeSlackApi):
        def __call__(self, token, method, payload):
            if method == 'chat.update':
                err = next(answers, None)
                if err is not None:
                    self.calls.append((method, payload))
                    raise err
            return super().__call__(token, method, payload)

    api = Flaky()
    posts = CardPosts(store, Slack('xoxb-test', '#ops', FakePost(), api), BASE, retry_after_s=0.2)

    # when
    posts.swept(Applied(rev=1, events=[{'key': KEY, 'ev': 'cleared'}], push=[]))
    drained = posts.drain(5)

    # then
    assert drained and len(rewrites(api)) == 2, 'drain returns only after the retry ran'


def test_a_rate_limited_rewrite_backs_off_and_is_tried_again_like_a_slack_that_did_not_answer(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14, status='cleared')), None)
    posted(store, KEY)
    answers = iter([slowed('1')])

    class Limited(FakeSlackApi):
        def __call__(self, token, method, payload):
            if method == 'chat.update':
                err = next(answers, None)
                if err is not None:
                    self.calls.append((method, payload))
                    raise err
            return super().__call__(token, method, payload)

    api = Limited()
    posts = CardPosts(store, Slack('xoxb-test', '#ops', FakePost(), api), BASE, retry_after_s=0.2)

    # when
    posts.swept(Applied(rev=1, events=[{'key': KEY, 'ev': 'cleared'}], push=[]))
    drained = posts.drain(5)

    # then
    assert drained and len(rewrites(api)) == 2, 'a 429 waits and is tried again instead of being given up'
    assert 'gone' not in (store.anomaly(KEY).get('slackPost') or {}).get('drawn', {})


def test_a_post_made_while_the_listener_was_off_gets_a_menu_once_it_runs(store: Store):
    # given
    store.save(state(anomaly(KEY, num=14)), None)
    posted(store, KEY)
    slack, _, api = fake_slack()
    on = {'v': False}
    posts = posts_of(store, slack, menu_on=lambda: on['v'])

    # when
    posts.reconcile()
    off = posts.run_batch()
    on['v'] = True
    posts.reconcile()
    later = posts.run_batch()

    # then
    assert off == 0 and later == 1
    assert [v.split(':')[0] for _, v, _ in menu_of(rewrites(api)[-1])] == ['take', 'resolve', 'known', 'chat']


def test_a_post_marked_gone_for_a_channel_reason_is_tried_again_a_day_later_and_a_deleted_message_never(store: Store):
    # given
    keys = {'log|a|ERROR|old': 'not_in_channel', 'log|a|ERROR|new': 'not_in_channel', 'log|a|ERROR|deleted': 'message_not_found'}
    store.save(state(*(anomaly(k, num=30 + i) for i, k in enumerate(keys))), None)
    for i, (k, code) in enumerate(keys.items()):
        at = '2026-11-01T07:00:00Z' if k != 'log|a|ERROR|new' else time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        store.slack_posted(k, {'channel': 'C1', 'ts': f'1792224000.00070{i}', 'at': at, 'by': 'server', 'head': SHOWN,
                               'drawn': {'sig': drawn_sig(store.anomaly(k), None), 'at': at, 'gone': code}})
    slack, _, api = fake_slack()
    posts = posts_of(store, slack)

    # when
    posts.reconcile()
    posts.run_batch()

    # then
    assert [u['ts'] for u in rewrites(api)] == ['1792224000.000700']
    assert 'gone' not in store.anomaly('log|a|ERROR|old')['slackPost']['drawn']


def test_a_duration_card_shows_its_numbers_as_durations_and_an_access_card_shows_none():
    # given
    argo = CARD | {'src': 'argo', 'unit': 'dur', 'now': 121, 'd1': 3700, 'd7': None}
    access = CARD | {'src': 'argo', 'sig': 'no-access', 'unit': '', 'now': 1, 'd1': None, 'd7': None}

    # when
    shown = [bar_of(anomaly_message(c, LINK, 'server', None, None, CHAT)) for c in (argo, access)]

    # then
    assert 'Now *2 min* \u00b7 yesterday 61 min' in shown[0]
    assert not any(t.startswith('Now') for t in shown[1]), 'an access card has no count to show'
