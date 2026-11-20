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
    assert saved.json()['responsibles'] == [{'email': NINA, 'name': 'Nina White'}, {'email': ADMIN_EMAIL, 'name': 'Admin'}]
    assert same.status_code == 200 and cleared.json()['responsibles'] == [{'email': ADMIN_EMAIL, 'name': 'Admin'}]
    rows = [(h['key'], h['old'], h['new'], h['owners'], h['tag'], h['by']) for h in history]
    assert rows == [('tags.developer.responsibles', f'{NINA}, {ADMIN_EMAIL}', ADMIN_EMAIL, True, 'developer', OSCAR),
                    ('tags.developer.responsibles', None, f'{NINA}, {ADMIN_EMAIL}', True, 'developer', OSCAR)]
    dev = next(t for t in listed['tags'] if t['id'] == 'developer')
    assert dev['label'] == 'Developer' and dev['responsibles'] == [{'email': ADMIN_EMAIL, 'name': 'Admin'}]
    assert listed['canEdit'] is True
    assert {u['email'] for u in listed['users']} >= {NINA, ADMIN_EMAIL, OSCAR}
    lines = [a['text'] for a in store.list_activity(None, 50) if a['text'].startswith('Responsibles of')]
    assert lines[0] == 'Responsibles of Developer: Nina White, Admin \u2192 Admin'
    assert all('@' not in line for line in lines)


def test_an_unknown_tag_or_person_is_refused(store: Store):
    # given
    store.save(state(), None)
    admin = signed_in(store)

    # when
    no_tag = admin.put('/api/admin/tags/nope/responsibles', json={'emails': [ADMIN_EMAIL]})
    no_user = admin.put('/api/admin/tags/developer/responsibles', json={'emails': ['ghost@example.com']})
    too_many = admin.put('/api/admin/tags/developer/responsibles', json={'emails': [f'u{n}@example.com' for n in range(21)]})
    people(store)
    Users(store.db).set_disabled(NINA, True)
    disabled = admin.put('/api/admin/tags/developer/responsibles', json={'emails': [NINA]})

    # then
    assert no_tag.status_code == 404 and 'nope' in no_tag.json()['detail']
    assert no_user.status_code == 422 and 'ghost@example.com' in no_user.json()['detail']
    assert disabled.status_code == 422 and NINA in disabled.json()['detail']
    assert too_many.status_code == 422
    assert store.tag_owners() == {}


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
    assert [(p['email'], p.get('stale', False)) for p in listed['responsibles']] == [(NINA, True), ('gone@example.com', True)]
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

    # then
    assert 'tag-report' in on and 'tag-report' not in autosend_off and 'tag-report' not in switch_off
    assert needs == ['Needs Slack auto-send on (Admin \u203a Features).']
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
