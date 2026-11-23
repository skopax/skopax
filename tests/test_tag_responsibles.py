from fastapi.testclient import TestClient

from tests.conftest import ADMIN_EMAIL, AUTH, settings, state
from tests.fake_slack import fake_slack
from skopax.app import create_app
from tests.test_auth import ROLE, TokenEndpoint, access_token, app_client, log_in
from skopax.store import Store
from skopax.users import Users

ADMIN = 'SKOPAX_ADMIN'
NINA = 'nina.white@example.com'
OSCAR = 'oscar.green@example.com'


def signed_in(store: Store, roles=(ADMIN,)):
    client = app_client(store, TokenEndpoint(access_token(list(roles))))
    log_in(client)
    return client


def people(store: Store) -> None:
    Users(store.db).create(NINA, 'Nina White', 'operator')


def test_the_store_keeps_a_list_per_tag_and_an_empty_list_drops_the_tag(store: Store):
    # when
    store.set_tag_owners('developer', [NINA, ADMIN_EMAIL])
    store.set_tag_owners('seo', [NINA])
    store.set_tag_owners('seo', [])

    # then
    assert store.tag_owners() == {'developer': [NINA, ADMIN_EMAIL]}


def test_a_renamed_tag_keeps_its_responsibles_and_merges_into_an_existing_list(store: Store):
    # given
    store.set_tag_owners('tech', [NINA, ADMIN_EMAIL])
    store.set_tag_owners('developer', [ADMIN_EMAIL])
    store.set_tag_owners('old-seo', [NINA])

    # when
    store.rename_tags({'tech': 'developer', 'old-seo': 'seo'})

    # then
    assert store.tag_owners() == {'developer': [ADMIN_EMAIL, NINA], 'seo': [NINA]}


def test_only_admins_read_and_change_responsibles(store: Store):
    # given
    store.save(state(), None)
    people(store)
    guest, viewer, operator = app_client(store), signed_in(store, ()), signed_in(store, (ROLE,))

    # when
    reads = [c.get('/api/admin/tags').status_code for c in (guest, viewer, operator)]
    writes = [c.put('/api/admin/tags/developer/responsibles', json={'emails': [NINA]}).status_code for c in (guest, viewer, operator)]

    # then
    assert reads == [401, 403, 403]
    assert writes == [401, 403, 403]
    assert store.tag_owners() == {}


def test_readers_who_are_not_admins_see_no_responsible_address_in_the_activity(store: Store):
    # given
    store.save(state(), None)
    people(store)
    admin = signed_in(store)
    admin.put('/api/admin/tags/developer/responsibles', json={'emails': [NINA]})
    guest, viewer = app_client(store), signed_in(store, ())

    # when
    shown = [guest.get('/api/activity').text, viewer.get('/api/activity').text]
    own = admin.get('/api/activity').json()

    # then
    assert all(NINA not in text and 'Responsibles of' not in text for text in shown)
    assert [a['text'] for a in own if a['kind'] == 'access' and a['text'].startswith('Responsibles of')] == ['Responsibles of Developer: \u2013 \u2192 Nina White']


def test_an_admin_sets_and_clears_responsibles_and_each_change_is_a_history_row_without_revert(store: Store):
    # given
    store.save(state(), None)
    people(store)
    admin = signed_in(store)

    # when
    saved = admin.put('/api/admin/tags/developer/responsibles', json={'emails': [' Nina.White@example.com ', NINA, ADMIN_EMAIL]})
    same = admin.put('/api/admin/tags/developer/responsibles', json={'emails': [NINA, ADMIN_EMAIL]})
    cleared = admin.put('/api/admin/tags/developer/responsibles', json={'emails': [ADMIN_EMAIL]})
    listed = admin.get('/api/admin/tags').json()
    history = admin.get('/api/admin/history').json()

    # then
    assert saved.status_code == 200
    assert saved.json()['responsibles'] == [{'email': NINA, 'name': 'Nina White', 'slack': 'unchecked'}, {'email': ADMIN_EMAIL, 'name': 'Admin', 'slack': 'unchecked'}]
    assert same.status_code == 200 and cleared.json()['responsibles'] == [{'email': ADMIN_EMAIL, 'name': 'Admin', 'slack': 'unchecked'}]
    rows = [(h['key'], h['old'], h['new'], h['owners'], h['tag'], h['by']) for h in history]
    assert rows == [('tags.developer.responsibles', 'Nina White, Admin', 'Admin', True, 'developer', OSCAR),
                    ('tags.developer.responsibles', None, 'Nina White, Admin', True, 'developer', OSCAR)]
    assert store.tag_owners() == {'developer': [ADMIN_EMAIL]}
    dev = next(t for t in listed['tags'] if t['id'] == 'developer')
    assert dev['label'] == 'Developer' and dev['responsibles'] == [{'email': ADMIN_EMAIL, 'name': 'Admin', 'slack': 'unchecked'}]
    assert listed['canEdit'] is True
    assert {u['email'] for u in listed['users']} >= {NINA, ADMIN_EMAIL, OSCAR}
    lines = [a['text'] for a in store.list_activity(None, 50) if a['text'].startswith('Responsibles of')]
    assert lines[0] == 'Responsibles of Developer: Nina White, Admin \u2192 Admin'
    assert all('@' not in line for line in lines)


def test_an_unknown_tag_or_a_disabled_person_or_too_many_are_refused(store: Store):
    # given
    store.save(state(), None)
    admin = signed_in(store)

    # when
    no_tag = admin.put('/api/admin/tags/nope/responsibles', json={'emails': [ADMIN_EMAIL]})
    too_many = admin.put('/api/admin/tags/developer/responsibles', json={'emails': [f'u{n}@example.com' for n in range(21)]})
    people(store)
    Users(store.db).set_disabled(NINA, True)
    disabled = admin.put('/api/admin/tags/developer/responsibles', json={'emails': [NINA]})

    # then
    assert no_tag.status_code == 404 and 'nope' in no_tag.json()['detail']
    assert disabled.status_code == 200
    assert too_many.status_code == 422
    assert store.tag_owners() == {'developer': [NINA]}


def test_a_removed_tag_drops_out_of_the_list_and_the_token_can_set_responsibles(store: Store, client):
    # given
    store.set_tag_owners('gone-team', [ADMIN_EMAIL])

    # when
    put = client.put('/api/admin/tags/infra/responsibles', json={'emails': [ADMIN_EMAIL]}, headers=AUTH)
    listed = client.get('/api/admin/tags', headers=AUTH).json()

    # then
    assert put.status_code == 200
    assert 'gone-team' not in [t['id'] for t in listed['tags']]
    assert [t['id'] for t in listed['tags'] if t['responsibles']] == ['infra']


def test_the_report_time_setting_is_validated_and_empty_turns_it_off(client):
    # when
    bad = client.put('/api/admin/settings', json={'key': 'tags.report-at', 'value': '9:5'}, headers=AUTH)
    off = client.put('/api/admin/settings', json={'key': 'tags.report-at', 'value': ''}, headers=AUTH)
    later = client.put('/api/admin/settings', json={'key': 'tags.report-at', 'value': '16:30'}, headers=AUTH)
    item = next(i for i in client.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == 'tags.report-at')

    # then
    assert bad.status_code == 422 and 'HH:MM' in bad.json()['detail']
    assert off.status_code == 200 and off.json()['item']['value'] == ''
    assert later.status_code == 200
    assert (item['value'], item['default'], item['group'], item['card']) == ('16:30', '10:00', 'Card tags', 'Daily report')
    assert 'Monday to Friday' in item['help']


def test_the_daily_report_is_a_feature_switch_that_is_on_by_default(client):
    # when
    items = client.get('/api/admin/features', headers=AUTH).json()['items']

    # then
    item = next(i for i in items if i['id'] == 'tag-report')
    assert (item['key'], item['on']) == ('core.tag-report', True)


def test_a_stale_chip_is_marked_does_not_block_adding_someone_and_can_be_removed(store: Store, client):
    # given
    people(store)
    Users(store.db).create(OSCAR, 'Oscar Green', 'viewer')
    store.set_tag_owners('developer', [NINA, 'gone@example.com'])
    Users(store.db).set_disabled(NINA, True)

    # when
    listed = next(t for t in client.get('/api/admin/tags', headers=AUTH).json()['tags'] if t['id'] == 'developer')
    added = client.put('/api/admin/tags/developer/responsibles', json={'emails': [NINA, 'gone@example.com', OSCAR]}, headers=AUTH)
    removed = client.put('/api/admin/tags/developer/responsibles', json={'emails': [OSCAR]}, headers=AUTH)

    # then
    assert [(p['email'], p.get('stale', False)) for p in listed['responsibles']] == [(NINA, True), ('gone@example.com', False)]
    assert added.status_code == 200 and [p['email'] for p in added.json()['responsibles']] == [NINA, 'gone@example.com', OSCAR]
    assert removed.status_code == 200 and store.tag_owners() == {'developer': [OSCAR]}


def test_the_app_registers_the_report_job_gated_by_its_switch_and_slack_auto_send_and_names_only_active_people(store: Store):
    # given
    people(store)
    Users(store.db).create(OSCAR, 'Oscar Green', 'viewer')
    Users(store.db).set_disabled(OSCAR, True)
    store.set_tag_owners('developer', [NINA])
    slack, _, _ = fake_slack()

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        on = [j.name for j in c.app.state.scheduler.jobs]
        c.put('/api/admin/features/slack-autosend', json={'on': False}, headers=AUTH)
        needs = next(i for i in c.get('/api/admin/features', headers=AUTH).json()['items'] if i['id'] == 'tag-report')['needs']
        autosend_off = [j.name for j in c.app.state.scheduler.jobs]
        c.put('/api/admin/features/slack-autosend', json={'on': True}, headers=AUTH)
        c.put('/api/admin/features/tag-report', json={'on': False}, headers=AUTH)
        switch_off = [j.name for j in c.app.state.scheduler.jobs]
        names = c.app.state.tag_report.names()
        store.set_tag_owners('developer', [NINA, OSCAR, 'dana.gray@example.com'])
        held = c.app.state.tag_report.held()

    # then
    assert held == {'developer': [NINA, 'dana.gray@example.com']}
    assert 'tag-report' in on and 'tag-report' not in autosend_off and 'tag-report' not in switch_off
    assert needs == ['The Slack post needs Slack auto-send on (Admin \u203a Features); the mail to responsibles needs a Mail server and Mail responsibles their part (Admin \u203a Settings \u203a Card tags).']
    assert NINA in names and OSCAR not in names


def test_a_missing_email_scope_shows_as_a_notice_and_next_to_the_report_setting(store: Store, client):
    # given
    store.note_email_scope(True, '2026-11-20T10:00:00Z')
    hidden = [n['id'] for n in client.get('/api/admin/notices', headers=AUTH).json()['items']]
    store.set_tag_owners('developer', [ADMIN_EMAIL])

    # when
    notices = client.get('/api/admin/notices', headers=AUTH).json()['items']
    item = next(i for i in client.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == 'tags.report-at')
    listed = client.get('/api/admin/tags', headers=AUTH).json()
    store.note_email_scope(False, '2026-11-20T11:00:00Z')
    after = client.get('/api/admin/notices', headers=AUTH).json()['items']

    # then
    assert 'tag-report-scope' not in hidden, 'the warning is hidden while the report is off'
    warning = 'Slack app is missing users:read.email; responsibles are named without a ping'
    assert [n['text'] for n in notices if n['id'] == 'tag-report-scope'] == [warning]
    assert item['problem'] == warning
    assert listed['emailScopeMissing'] is True
    assert [n for n in after if n['id'] == 'tag-report-scope'] == []


def test_an_address_without_a_skopax_account_is_saved_and_named_by_its_local_part(store: Store, client):
    # given
    store.save(state(), None)

    # when
    put = client.put('/api/admin/tags/developer/responsibles', json={'emails': [' Ghost.Person@Example.com ']}, headers=AUTH)

    # then
    assert put.status_code == 200
    assert put.json()['responsibles'] == [{'email': 'ghost.person@example.com', 'name': 'ghost.person', 'slack': 'unchecked'}]
    assert store.tag_owners() == {'developer': ['ghost.person@example.com']}
    history = client.get('/api/admin/history', headers=AUTH).json()
    row = next(h for h in history if h['key'] == 'tags.developer.responsibles')
    assert (row['old'], row['new']) == (None, 'ghost.person')
    assert '@' not in str(row['new'])


def test_an_invalid_address_is_refused_with_its_own_message_and_nothing_is_saved(store: Store, client):
    # given
    store.save(state(), None)
    bad = ['not-an-email', 'a b@example.com', 'a@localhost', 'a@@example.com', 'x' * 250 + '@example.com']

    # when
    refused = [client.put('/api/admin/tags/developer/responsibles', json={'emails': [NINA, e]}, headers=AUTH) for e in bad]

    # then
    assert [r.status_code for r in refused] == [422] * len(bad)
    assert all('e-mail' in r.json()['detail'] and 'No active' not in r.json()['detail'] for r in refused)
    assert store.tag_owners() == {}


def test_slack_marks_each_responsible_found_or_unknown_and_a_found_one_is_named_by_its_slack_name(store: Store):
    # given
    store.save(state(), None)
    slack, _, api = fake_slack(emails={'U0ALICE': 'alice@example.com'})
    api.names['U0ALICE'] = 'Alice Moss'

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        put = c.put('/api/admin/tags/developer/responsibles', json={'emails': ['alice@example.com', 'bob@example.com', NINA]}, headers=AUTH)
        listed = next(t for t in c.get('/api/admin/tags', headers=AUTH).json()['tags'] if t['id'] == 'developer')

    # then
    assert put.status_code == 200
    expected = [{'email': 'alice@example.com', 'name': 'Alice Moss', 'slack': 'found'}, {'email': 'bob@example.com', 'name': 'bob', 'slack': 'unknown'},
                {'email': NINA, 'name': 'nina.white', 'slack': 'unknown'}]
    assert put.json()['responsibles'] == expected
    assert listed['responsibles'] == expected


def test_a_slack_error_never_blocks_the_save_and_marks_unchecked(store: Store):
    # given
    store.save(state(), None)
    slack, _, api = fake_slack(emails={'U0ALICE': 'alice@example.com'})
    api.fail = {'users.lookupByEmail'}

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        put = c.put('/api/admin/tags/developer/responsibles', json={'emails': ['alice@example.com', 'bob@example.com']}, headers=AUTH)

    # then
    assert put.status_code == 200
    assert [p['slack'] for p in put.json()['responsibles']] == ['unchecked', 'unchecked']
    assert store.tag_owners() == {'developer': ['alice@example.com', 'bob@example.com']}


def test_listing_makes_no_slack_call_and_a_save_looks_up_only_the_newly_added_address(store: Store):
    # given
    store.save(state(), None)
    store.set_tag_owners('developer', ['bob@example.com'])
    slack, _, api = fake_slack(emails={'U0ALICE': 'alice@example.com'})

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        before = c.get('/api/admin/tags', headers=AUTH).json()
        calls_before = api.methods().count('users.lookupByEmail')
        c.put('/api/admin/tags/developer/responsibles', json={'emails': ['bob@example.com', 'alice@example.com']}, headers=AUTH)
        calls_saved = api.methods().count('users.lookupByEmail')
        after = c.get('/api/admin/tags', headers=AUTH).json()

    # then
    marks = lambda body: [p['slack'] for p in next(t for t in body['tags'] if t['id'] == 'developer')['responsibles']]
    assert (marks(before), calls_before) == (['unchecked'], 0)
    assert calls_saved == 1
    assert marks(after) == ['unchecked', 'found'] and api.methods().count('users.lookupByEmail') == calls_saved


def test_a_failing_slack_is_called_once_per_save_and_not_again_for_a_while_and_the_owners_are_saved(store: Store):
    # given
    store.save(state(), None)
    slack, _, api = fake_slack()
    api.fail = {'users.lookupByEmail'}
    people = [f'u{n}@example.com' for n in range(5)]

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        first = c.put('/api/admin/tags/developer/responsibles', json={'emails': people}, headers=AUTH)
        calls_first = api.methods().count('users.lookupByEmail')
        second = c.put('/api/admin/tags/seo/responsibles', json={'emails': people}, headers=AUTH)
        calls_second = api.methods().count('users.lookupByEmail')

    # then
    assert first.status_code == second.status_code == 200
    assert store.tag_owners() == {'developer': people, 'seo': people}
    assert calls_first == 1 and calls_second == 1
    assert {p['slack'] for p in first.json()['responsibles']} == {'unchecked'}


def test_the_activity_line_for_an_outside_address_names_it_by_the_local_part_only(store: Store, client):
    # given
    store.save(state(), None)

    # when
    client.put('/api/admin/tags/developer/responsibles', json={'emails': ['ghost.person@example.com']}, headers=AUTH)

    # then
    lines = [a['text'] for a in store.list_activity(None, 50) if a['text'].startswith('Responsibles of')]
    assert lines == ['Responsibles of Developer: \u2013 \u2192 ghost.person']


def test_adding_a_person_shows_in_changes_by_name_with_no_address_and_no_revert(store: Store):
    # given
    store.save(state(), None)
    admin = signed_in(store)

    # when
    made = admin.post('/api/admin/users', json={'email': 'dana.gray@example.com', 'name': 'Dana Gray', 'role': 'viewer', 'password': 'a-long-enough-password-1'})
    admin.put('/api/admin/tags/developer/responsibles', json={'emails': ['dana.gray@example.com']})
    history = admin.get('/api/admin/history').json()

    # then
    assert made.status_code == 201, made.text
    access = [h for h in history if h.get('access')]
    assert len(access) == 1 and 'Dana Gray' in access[0]['text'] and access[0]['by']
    assert all('@' not in str(v) for h in history for v in (h.get('text'), h.get('old'), h.get('new')))
    assert '@' not in access[0]['by']
    assert [h['key'] for h in history][0] == 'tags.developer.responsibles'


def test_a_responsibles_row_saved_with_addresses_reads_back_by_name(store: Store):
    # given
    from skopax.tunables import TunableStore
    store.save(state(), None)
    people(store)
    TunableStore(store.db).write_change('tags.developer.responsibles', f'{NINA}, dana.gray@example.com', None, ADMIN_EMAIL, owners=True, tag='developer')
    admin = signed_in(store)

    # when
    row = next(h for h in admin.get('/api/admin/history').json() if h['key'] == 'tags.developer.responsibles')

    # then
    assert (row['old'], row['new']) == ('Nina White, dana.gray', None)


def test_the_tag_list_reads_the_people_once_however_many_tags_have_responsibles(store: Store, monkeypatch):
    # given
    store.save(state(), None)
    people(store)
    store.set_tag_owners('developer', [NINA])
    store.set_tag_owners('seo', [NINA, ADMIN_EMAIL])
    admin = signed_in(store)
    reads = []
    listing = Users.list
    monkeypatch.setattr(Users, 'list', lambda self: reads.append(1) or listing(self))

    # when
    listed = admin.get('/api/admin/tags').json()

    # then
    seo = next(t for t in listed['tags'] if t['id'] == 'seo')
    assert [r['name'] for r in seo['responsibles']] == ['Nina White', 'Admin']
    assert len(reads) == 1, f'the people were listed {len(reads)} times'
