import threading

from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from tests.test_tag_responsibles import ROLE, app_client, signed_in
from skopax import actions, tags
from skopax.app import create_app
from skopax.store import Store
from skopax.users import Users

NINA = 'nina.white@example.com'
OSCAR = 'oscar.green@example.com'
PIA = 'pia.stone@example.com'
OUTSIDE = 'outside@example.org'
SINCE = '2026-01-01T00:00:00Z'
NEW = '2026-01-02T09:00:00Z'
OLD = '2025-12-31T09:00:00Z'


def card(key: str, tag_list=('developer',), first: str = NEW, tagged: str | None = None, by: str = 'rule', **extra) -> dict:
    return anomaly(key, firstSeen=first, lastSeen=first, tags=[{'id': t, 'by': by, 'at': tagged or first} for t in tag_list], **extra)


def inline(app) -> list:
    jobs = []
    app.state.auto_assign.later = jobs.append
    return jobs


def run(jobs: list) -> None:
    while jobs:
        jobs.pop(0)()


def team(store: Store) -> None:
    users = Users(store.db)
    users.create(NINA, 'Nina White', 'operator')
    users.create(OSCAR, 'Oscar Green', 'operator')
    users.create(PIA, 'Pia Stone', 'viewer')
    users.set_disabled(PIA, True)
    store.auto_assign_since(SINCE)


def owners(store: Store) -> dict:
    return {a['key']: a.get('owner') for a in store.load()['anomalies']}


def test_auto_assign_is_on_by_default_and_an_admin_switches_it_off_per_tag_with_a_names_only_activity_line(store: Store, client):
    # given
    store.set_tag_owners('developer', [NINA])

    # when
    first = {t['id']: t['autoAssign'] for t in client.get('/api/admin/tags', headers=AUTH).json()['tags']}
    off = client.put('/api/admin/tags/developer/auto-assign', json={'on': False}, headers=AUTH)
    same = client.put('/api/admin/tags/developer/auto-assign', json={'on': False}, headers=AUTH)
    after = {t['id']: t['autoAssign'] for t in client.get('/api/admin/tags', headers=AUTH).json()['tags']}
    back = client.put('/api/admin/tags/developer/auto-assign', json={'on': True}, headers=AUTH)
    unknown = client.put('/api/admin/tags/nope/auto-assign', json={'on': False}, headers=AUTH)

    # then
    assert all(first.values()) and first['developer'] is True
    assert off.status_code == 200 and off.json()['autoAssign'] is False and same.status_code == 200
    assert after['developer'] is False and all(v for k, v in after.items() if k != 'developer')
    assert back.json()['autoAssign'] is True and store.tag_auto_off() == set()
    assert unknown.status_code == 404
    lines = [a['text'] for a in store.list_activity(None, 50) if a['text'].startswith('Auto-assign of')]
    assert lines == ['Auto-assign of Developer: off \u2192 on', 'Auto-assign of Developer: on \u2192 off']


def test_only_admins_switch_auto_assign(store: Store):
    # given
    store.save(state(), None)
    guest, viewer, operator = app_client(store), signed_in(store, ()), signed_in(store, (ROLE,))

    # when
    codes = [c.put('/api/admin/tags/developer/auto-assign', json={'on': False}).status_code for c in (guest, viewer, operator)]

    # then
    assert codes == [401, 403, 403]
    assert store.tag_auto_off() == set()


def test_new_cards_are_assigned_round_robin_through_the_normal_assign_path(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA, OUTSIDE, PIA, OSCAR])
    store.save(state(card('log|a'), card('log|b'), card('log|c')), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        done = c.app.state.auto_assign.sweep(store.load()['anomalies'])
        again = c.app.state.auto_assign.sweep(store.load()['anomalies'])

    # then
    assert done == ['log|a', 'log|b', 'log|c'] and again == []
    assert owners(store) == {'log|a': 'Nina White', 'log|b': 'Oscar Green', 'log|c': 'Nina White'}
    a = store.anomaly('log|a')
    assert a['assigned']['email'] == NINA and a['assigned']['by'] == 'system' and a['autoAssigned']['tag'] == 'developer'
    lines = [x['text'] for x in store.list_activity(None, 50) if x['kind'] == 'assign']
    assert sorted(lines) == ['Auto-assigned to Nina White (tag Developer) \u00b7 log|a', 'Auto-assigned to Nina White (tag Developer) \u00b7 log|c', 'Auto-assigned to Oscar Green (tag Developer) \u00b7 log|b']
    assert all('@' not in line for line in lines)


def test_two_server_copies_share_the_turn_and_never_assign_one_card_twice(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA, OSCAR])
    store.save(state(card('log|a'), card('log|b'), card('log|c'), card('log|d')), None)

    # when
    with TestClient(create_app(settings(), store)) as one, TestClient(create_app(settings(), store)) as two:
        cards = store.load()['anomalies']
        first = one.app.state.auto_assign.sweep(cards[:2])
        second = two.app.state.auto_assign.sweep(cards[1:])
        late = one.app.state.auto_assign.sweep(cards)

    # then
    assert first == ['log|a', 'log|b'] and second == ['log|c', 'log|d'] and late == []
    assert owners(store) == {'log|a': 'Nina White', 'log|b': 'Oscar Green', 'log|c': 'Nina White', 'log|d': 'Oscar Green'}


def test_taken_assigned_ticketed_information_cleared_switched_off_and_old_cards_are_left_alone(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.set_tag_owners('data', [NINA])
    store.set_tag_auto('data', False)
    store.save(state(card('log|taken', triage='progress', triagedBy=OSCAR), card('log|owned', owner='Oscar Green'), card('log|ticket', ticket='OPS-123'),
                     card('log|info', sev='info'), card('log|cleared', status='cleared'), card('log|off', tag_list=('data',)),
                     card('log|old', first=OLD), card('log|tagged-now', first=OLD, tagged=NEW, by=NINA),
                     card('log|rule-edit', first=OLD, tagged=NEW)), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        done = c.app.state.auto_assign.sweep(store.load()['anomalies'])

    # then
    assert done == ['log|tagged-now']
    assert owners(store)['log|owned'] == 'Oscar Green' and owners(store)['log|taken'] is None


def test_only_active_skopax_users_are_picked_and_without_one_nothing_happens_and_the_tag_says_so(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [OUTSIDE, PIA])
    store.set_tag_owners('data', [OUTSIDE, OSCAR])
    store.save(state(card('log|a'), card('log|b', tag_list=('data',))), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        done = c.app.state.auto_assign.sweep(store.load()['anomalies'])
        listed = {t['id']: t['autoReady'] for t in c.get('/api/admin/tags', headers=AUTH).json()['tags']}

    # then
    assert done == ['log|b'] and owners(store) == {'log|a': None, 'log|b': 'Oscar Green'}
    assert listed['developer'] is False and listed['data'] is True
    assert 'autoAssigned' not in store.anomaly('log|a')


def test_a_card_a_person_unassigned_is_never_auto_assigned_again(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.save(state(card('log|a')), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.app.state.auto_assign.sweep(store.load()['anomalies'])
        removed = c.put('/api/assignee', json={'key': 'log|a', 'name': ''}, headers=AUTH)
        again = c.app.state.auto_assign.sweep(store.load()['anomalies'])

    # then
    assert removed.status_code == 200
    assert again == [] and store.anomaly('log|a').get('owner') is None


def test_with_several_auto_assign_tags_the_first_by_label_decides(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.set_tag_owners('data', [OSCAR])
    store.save(state(card('log|a', tag_list=('developer', 'data'))), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.app.state.auto_assign.sweep(store.load()['anomalies'])

    # then
    assert owners(store) == {'log|a': 'Oscar Green'}
    assert store.anomaly('log|a')['autoAssigned']['tag'] == 'data'


def test_tagging_a_card_by_hand_assigns_it_at_once(store: Store):
    # given
    team(store)
    store.set_tag_owners('data', [OSCAR])
    store.save(state(card('log|a', tag_list=(), first=OLD)), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        tagged = c.post('/api/tags', json={'key': 'log|a', 'tag': 'data', 'on': True}, headers=AUTH)

    # then
    assert tagged.status_code == 200
    assert owners(store) == {'log|a': 'Oscar Green'}


def test_muted_cards_grouped_children_and_system_handled_cards_are_left_alone(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.save(state(card('log|muted', muted='known noise'), card('log|parent', sev='info'), card('log|child', parent='log|parent'),
                     card('log|resolved', triage='handled', triagedBy='auto')), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        done = c.app.state.auto_assign.sweep(store.load()['anomalies'])

    # then
    assert done == []
    assert set(owners(store).values()) == {None}


def test_adding_a_responsible_or_switching_back_on_never_assigns_the_cards_already_open(store: Store):
    # given
    team(store)
    store.save(state(*[card(f'log|{n}') for n in range(3)]), None)
    with TestClient(create_app(settings(), store)) as c:
        before = c.app.state.auto_assign.sweep(store.load()['anomalies'])
        store.set_tag_owners('developer', [NINA])

        # when
        added = c.app.state.auto_assign.sweep(store.load()['anomalies'])
        c.put('/api/admin/tags/developer/auto-assign', json={'on': False}, headers=AUTH)
        c.put('/api/admin/tags/developer/auto-assign', json={'on': True}, headers=AUTH)
        back_on = c.app.state.auto_assign.sweep(store.load()['anomalies'])
        tagged = c.post('/api/tags', json={'key': 'log|0', 'tag': 'data', 'on': True}, headers=AUTH)

    # then
    assert before == [] and added == [] and back_on == []
    assert tagged.status_code == 200 and set(owners(store).values()) == {None}


def test_a_tag_with_responsibles_at_the_upgrade_assigns_cards_of_the_first_pass(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.save(state(card('log|a')), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        done = c.app.state.auto_assign.sweep(store.load()['anomalies'])

    # then
    assert done == ['log|a']


def test_a_card_a_person_assigned_unassigned_or_took_is_never_auto_assigned(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.set_tag_auto('developer', False)
    store.save(state(card('log|a'), card('log|b', tagged=NEW, by=NINA)), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.put('/api/assignee', json={'key': 'log|a', 'name': 'Oscar Green'}, headers=AUTH)
        c.put('/api/assignee', json={'key': 'log|a', 'name': ''}, headers=AUTH)
        store.triage('log|b', 'progress', owner='Oscar Green')
        store.mark_hand_assigned('log|b')
        store.triage('log|b', None, owner='')
        store.set_tag_auto('developer', True)
        store.set_tag_ready('developer', SINCE)
        again = c.app.state.auto_assign.sweep(store.load()['anomalies'])

    # then
    assert again == []
    assert store.anomaly('log|a').get('handAssigned') and store.anomaly('log|b').get('handAssigned')


def test_a_person_who_assigns_first_is_never_overwritten_by_a_stale_card(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.save(state(card('log|a')), None)
    stale = store.load()['anomalies']

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.put('/api/assignee', json={'key': 'log|a', 'name': 'Oscar Green', 'email': OSCAR}, headers=AUTH)
        done = c.app.state.auto_assign.sweep(stale)

    # then
    assert done == [] and owners(store) == {'log|a': 'Oscar Green'}
    assert 'autoAssigned' not in store.anomaly('log|a')


def test_the_pass_only_writes_and_slack_messages_go_out_in_the_background(store: Store):
    # given
    from tests.fake_slack import fake_slack
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.save(state(card('log|a')), None)
    slack, posts, api = fake_slack(emails={'U0NINA': NINA})

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        jobs = inline(c.app)
        asked = len(api.calls)
        done = c.app.state.auto_assign.sweep(store.load()['anomalies'])
        during = api.calls[asked:]
        written = owners(store)
        run(jobs)
        after = api.calls[asked:]
        noticed = list(store.db['notifications'].find({}, {'_id': 0, 'user': 1, 'kind': 1}))

    # then
    assert done == ['log|a'] and written == {'log|a': 'Nina White'}
    assert during == []
    assert [m for m, _ in after].count('users.lookupByEmail') == 1, 'the assignee is looked up for the direct message once the job runs'
    assert any(p.get('channel') == 'U0NINA' for p in posts.calls), 'the direct message goes out in the background'
    assert [(n['user'], n['kind']) for n in noticed] == [('nina.white', 'assigned')]
    assert store.anomaly('log|a')['assigned'] == {'by': 'system', 'at': store.anomaly('log|a')['assigned']['at'], 'email': NINA}


def test_a_renamed_tag_keeps_its_switch_its_start_and_its_turn(store: Store):
    # given
    team(store)
    store.set_tag_owners('tech', [NINA, OSCAR])
    store.set_tag_auto('tech', False)
    store.set_tag_ready('tech', NEW)
    store.next_auto_turn('tech')

    # when
    store.rename_tags({'tech': 'developer'})

    # then
    assert store.tag_auto_off() == {'developer'}
    assert store.tag_ready() == {'developer': NEW}
    assert store.next_auto_turn('developer') == 2


def test_an_old_card_the_investigator_tags_is_not_auto_assigned(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.save(state(card('log|old', first=OLD, tagged=NEW, by=tags.POD)), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        inline(c.app)
        done = c.app.state.auto_assign.sweep(store.load()['anomalies'])

    # then
    assert done == []


def test_a_card_a_person_took_through_the_take_action_is_never_auto_assigned(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.save(state(card('log|a')), None)

    # when
    actions.apply(store, {'type': 'take', 'key': 'log|a', 'requestedBy': OSCAR})
    store.triage('log|a', None, owner='')
    with TestClient(create_app(settings(), store)) as c:
        done = c.app.state.auto_assign.sweep(store.load()['anomalies'])

    # then
    assert done == [] and store.anomaly('log|a').get('handAssigned') is True


def test_a_lost_claim_does_not_use_up_a_turn(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA, OSCAR])
    store.save(state(card('log|a'), card('log|b')), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        cards = store.load()['anomalies']
        real = store.claim_auto_assign
        store.claim_auto_assign = lambda key, *rest: None if key == 'log|a' else real(key, *rest)
        c.app.state.auto_assign.sweep(cards)

    # then
    assert owners(store) == {'log|a': None, 'log|b': 'Nina White'}


def test_a_failed_activity_line_still_announces_the_assignment(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.save(state(card('log|a')), None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        jobs = inline(c.app)
        told = []
        c.app.state.auto_assign.assigner.announce = lambda before, name, email, by: told.append((before['key'], name, email, by))
        store.add_activity = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('down'))
        done = c.app.state.auto_assign.sweep(store.load()['anomalies'])
        run(jobs)

    # then
    assert done == ['log|a'] and told == [('log|a', 'Nina White', NINA, 'system')]


def test_the_background_messages_finish_when_the_app_stops(store: Store):
    # given
    team(store)
    store.set_tag_owners('developer', [NINA])
    store.save(state(card('log|a')), None)
    told = []

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.app.state.auto_assign.assigner.announce = lambda before, name, email, by: told.append(before['key'])
        c.app.state.auto_assign.sweep(store.load()['anomalies'])
    threads = [t.name for t in threading.enumerate() if t.name.startswith('skopax-auto-assign') and t.is_alive()]

    # then
    assert told == ['log|a']
    assert threads == []
