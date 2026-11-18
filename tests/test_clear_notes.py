from tests.fake_slack import FakeSlackApi
from tests.test_state_notes import KEY, KID, POST_TS, Transport, card, seed
from tests.test_slack_clicks import clicks
from skopax.clearnotes import ClearNotes, task_for, took_text
from skopax.engine import iso, parse_iso
from skopax.messages import msg
from skopax.slack import CARD_ACTION, CREATE_TASK, Slack
from skopax.store import Store, keep_slack_state

CLEARED = '2026-10-26T09:30:00Z'
T = parse_iso(CLEARED)
M = 60


def notes(store: Store, transport: Transport, minutes: float = 15, prefix: str = '') -> ClearNotes:
    return ClearNotes(store, Slack('xoxb-test', '#ops', transport, FakeSlackApi()), minutes, title_prefix=prefix)


def line(tail: str = msg('clear.free_task')) -> str:
    return f"🟢 Back to normal: *checkout-service / ERROR pattern* · clean since 26 Oct 09:30 UTC, lasted 2 h 30 min. {tail}"


def test_a_posted_card_that_stays_clear_gets_one_note_in_its_thread_with_a_resolve_button(store: Store):
    # given
    seed(store, card(KEY, status='cleared', clearedAt=CLEARED))
    t = Transport()
    box = notes(store, t, prefix='OPS: ')

    # when
    early = box.run_once(T + 10 * M)
    settled = box.run_once(T + 16 * M)
    again = box.run_once(T + 90 * M)

    # then
    assert (early, settled, again) == ([], [KEY], [])
    assert t.replies() == [(POST_TS, line())]
    assert t.calls[0]['blocks'][1]['elements'][0] | {'value': None} == {'type': 'button', 'action_id': CARD_ACTION, 'value': None, 'style': 'primary',
                                                                      'text': {'type': 'plain_text', 'text': 'Resolve'}}
    offer = [a for a in store.list_actions(None, 20) if a['type'] == 'clear-note'][-1]
    assert (offer['status'], offer['cardAction'], t.calls[0]['blocks'][1]['elements'][0]['value']) == ('done', {'key': KEY, 'action': 'resolve'}, offer['id'])
    task = t.calls[0]['blocks'][1]['elements'][1]
    assert (task['action_id'], task['value'], task['text']['text'], task['confirm']['title']['text']) == (CREATE_TASK, offer['id'], 'Create task', 'Create task?')
    assert task['confirm']['text']['text'].startswith('*OPS: checkout-service ERROR pattern*') and 'is assigned to you' in task['confirm']['text']['text']
    assert store.anomaly(KEY)['slackPost']['clearNote'] == {'at': iso(T + 16 * M), 'ts': '1792224001.000001', 'clearedAt': CLEARED}


def test_create_task_on_a_clear_note_opens_a_task_from_the_card_and_keeps_the_resolve_button(store: Store):
    # given
    seed(store, card(KEY, status='cleared', clearedAt=CLEARED))
    t = Transport()
    notes(store, t).run_once(T + 16 * M)
    offer = [a for a in store.list_actions(None, 20) if a['type'] == 'clear-note'][-1]
    box, applier, api = clicks(store)
    message = {'ts': '1792224001.000001', 'thread_ts': POST_TS, 'text': t.calls[0]['text'], 'blocks': t.calls[0]['blocks']}

    # when
    out = box.handle({'type': 'block_actions', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'}, 'message': message, 'actions': [{'action_id': CREATE_TASK, 'value': offer['id']}]})

    # then
    assert out == 'created'
    assert applier.calls[0] | {'askId': None} == {'type': 'ticket', 'key': KEY, 'target': KEY, 'title': 'checkout-service ERROR pattern', 'description': '', 'points': None, 'project': None,
                                                 'label': 'Create task from Slack: checkout-service ERROR pattern', 'requestedBy': 'adam.baker@example.com', 'source': 'slack', 'askId': None}
    update = next(p for m, p in api.calls if m == 'chat.update')
    assert [e['action_id'] for b in update['blocks'] if b['type'] == 'actions' for e in b['elements']] == [CARD_ACTION]


def test_an_infrastructure_card_offers_a_tech_task_without_the_title_prefix(store: Store):
    # given
    pvc = 'pvc-disk|cluster-logging/data-es-0|disk'
    seed(store, card(pvc, status='cleared', clearedAt=CLEARED, src='capacity', svc='data-es-0', label='Volume disk'))
    t = Transport()

    # when
    notes(store, t, prefix='OPS: ').run_once(T + 16 * M)

    # then
    task = t.calls[0]['blocks'][1]['elements'][1]
    assert (task['text']['text'], task['confirm']['title']['text']) == ('Create infrastructure task', 'Create infrastructure task?')
    assert 'assigned to Infra' in task['confirm']['text']['text']
    proposal = [a for a in store.list_actions(None, 20) if a['type'] == 'clear-note'][-1]['ticketProposal']
    assert (proposal['project'], proposal['tech'], proposal['title']) == (None, True, 'data-es-0 Volume disk')


def test_a_card_that_already_has_a_ticket_gets_only_the_resolve_button(store: Store):
    # given
    seed(store, card(KEY, status='cleared', clearedAt=CLEARED, ticket='OPS-1'))
    t = Transport()

    # when
    notes(store, t).run_once(T + 16 * M)

    # then
    assert t.replies() == [(POST_TS, line(msg('clear.free')))]
    assert [e['action_id'] for e in t.calls[0]['blocks'][1]['elements']] == [CARD_ACTION]
    assert 'ticketProposal' not in [a for a in store.list_actions(None, 20) if a['type'] == 'clear-note'][-1]


def test_a_clear_older_than_twelve_hours_stays_quiet_so_a_deploy_sends_no_burst(store: Store):
    # given
    seed(store, card(KEY, status='cleared', clearedAt=CLEARED))
    t = Transport()

    # when
    sent = notes(store, t).run_once(T + 12 * 60 * M + 1)

    # then
    assert sent == []
    assert t.calls == []


def test_cards_that_are_open_handled_merged_muted_stale_or_refired_without_a_word_in_slack_get_no_note(store: Store):
    # given
    seed(store,
         card(KEY, status='open'),
         card('log|a|ERROR|handled', ts='1792224000.000300', status='cleared', clearedAt=CLEARED, triage='handled'),
         card(KID, ts='1792224000.000200', status='cleared', clearedAt=CLEARED, parent=KEY),
         card('log|a|ERROR|muted', ts='1792224000.000400', status='cleared', clearedAt=CLEARED, muted=True),
         card('log|a|ERROR|silent', ts='1792224000.000500', status='cleared', clearedAt=CLEARED, firstSeen='2026-10-26T09:00:00Z'),
         card('log|a|ERROR|unposted', ts=None, status='cleared', clearedAt=CLEARED),
         card('log|a|ERROR|stale', ts='1792224000.000700', status='cleared', clearedAt=CLEARED, expired=True))
    t = Transport()

    # when
    sent = notes(store, t).run_once(T + 20 * M)

    # then
    assert sent == []
    assert t.calls == []


def test_a_refire_with_a_thread_update_gets_its_own_note_and_a_taken_card_says_so(store: Store):
    # given
    first = '2026-10-26T09:00:00Z'
    old = {'at': '2026-10-26T06:00:00Z', 'ts': '1', 'clearedAt': '2026-10-26T05:50:00Z'}
    seed(store, card(KEY, status='cleared', clearedAt='2026-10-26T11:30:00Z', firstSeen=first, triage='progress',
                     slackPost={'channel': 'C9', 'ts': POST_TS, 'at': '2026-10-26T04:00:00Z', 'update': {'at': '2026-10-26T09:10:00Z'}, 'clearNote': old}))
    t = Transport()

    # when
    sent = notes(store, t).run_once(parse_iso('2026-10-26T11:50:00Z'))

    # then
    assert sent == [KEY]
    assert t.replies() == [(POST_TS, f"🟢 Back to normal: *checkout-service / ERROR pattern* · clean since 26 Oct 11:30 UTC, lasted 2 h 30 min. {msg('clear.taken')}")]


def test_a_newer_clear_note_survives_a_state_write_from_an_older_copy_and_minutes_zero_turns_it_off(store: Store):
    # given
    stored = card(KEY)
    stored['slackPost'] = stored['slackPost'] | {'clearNote': {'at': '2026-10-26T09:50:00Z', 'ts': '1', 'clearedAt': CLEARED}, 'update': {'at': '2026-10-26T09:00:00Z'}}

    # when
    out = keep_slack_state(card(KEY), stored)

    # then
    assert not notes(None, Transport(), minutes=0).enabled
    assert out['slackPost']['clearNote']['at'] == '2026-10-26T09:50:00Z'
    assert out['slackPost']['update'] == {'at': '2026-10-26T09:00:00Z'}


def test_the_duration_reads_in_minutes_hours_or_days():
    # then
    assert [took_text(s) for s in (20, 45 * M, 60 * M, 150 * M, 47 * 3600, 50 * 3600)] == ['1 min', '45 min', '1 h', '2 h 30 min', '47 h', '2 days']


def test_an_oom_crashloop_or_eviction_card_that_went_quiet_is_not_called_back_to_normal(store: Store):
    # given
    oom = 'alert|ContainerOOMKilled|cluster=east-data-prometheus,namespace=analytics,container=trino-worker'
    restarts = 'restarts|legacy/checkout-service|'
    seed(store, card(oom, status='cleared', clearedAt=CLEARED, src='grafana', svc='kube-state-metrics', label='ContainerOOMKilled'),
         card(restarts, ts='1792224000.000300', status='cleared', clearedAt=CLEARED, src='kube', label='container restarts', triage='progress',
              what='3 container restarts in the last 15 min (last exit: OOMKilled)'),
         card(KEY, ts='1792224000.000400', status='cleared', clearedAt=CLEARED))
    t = Transport()

    # when
    sent = notes(store, t).run_once(T + 16 * M)
    said = dict(t.replies())

    # then
    assert sorted(sent) == sorted([oom, restarts, KEY])
    assert said[POST_TS] == ("🟡 Alarm went quiet: *kube-state-metrics / ContainerOOMKilled* · no new signal since 26 Oct 09:30 UTC, lasted 2 h 30 min. "
                             'This kind of problem does not fix itself; if the cause is not removed it can come back at any time. If a lasting fix is in, Resolve; if not, Create task.')
    assert said['1792224000.000300'].startswith('🟡 Alarm went quiet: *checkout-service / container restarts*') and said['1792224000.000300'].endswith('Being worked on; Resolve once the lasting fix lands.')
    assert said['1792224000.000400'] == line()
    assert all('Back to normal' not in said[ts] for ts in (POST_TS, '1792224000.000300'))


def test_a_cloudflare_card_of_the_plugin_and_of_before_is_an_infrastructure_card():
    # given
    found = [{'key': f'cf-status|cloudflare/{n}', 'src': src, 'svc': n, 'label': 'Cloudflare status'} for n, src in (('WAF', 'cf'), ('CDN', 'cloudflare'), ('Edge', 'edge'))]

    # when
    tasks = [task_for(card, 'OPS: ') for card in found]

    # then
    assert [(t['tech'], t['title']) for t in tasks] == [(True, 'WAF Cloudflare status'), (True, 'CDN Cloudflare status'), (True, 'Edge Cloudflare status')]
