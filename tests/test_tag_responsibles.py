from tests.conftest import ADMIN_EMAIL, AUTH, state
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


def test_only_admins_change_responsibles_while_every_signed_in_role_reads_them(store: Store):
    # given
    store.save(state(), None)
    people(store)
    guest, viewer, operator = app_client(store), signed_in(store, ()), signed_in(store, (ROLE,))

    # when
    reads = [guest.get('/api/admin/tags').status_code, viewer.get('/api/admin/tags').status_code, operator.get('/api/admin/tags').status_code]
    writes = [c.put('/api/admin/tags/developer/responsibles', json={'emails': [NINA]}).status_code for c in (guest, viewer, operator)]
    seen = operator.get('/api/admin/tags').json()

    # then
    assert reads == [401, 200, 200]
    assert writes == [401, 403, 403]
    assert seen['canEdit'] is False and seen['users'] == []
    assert store.tag_owners() == {}


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
    lines = [a['text'] for a in store.list_activity(None, 50) if a['kind'] == 'admin']
    assert lines[0] == f'Responsibles of Developer: {NINA}, {ADMIN_EMAIL} \u2192 {ADMIN_EMAIL}'


def test_an_unknown_tag_or_person_is_refused(store: Store):
    # given
    store.save(state(), None)
    admin = signed_in(store)

    # when
    no_tag = admin.put('/api/admin/tags/nope/responsibles', json={'emails': [ADMIN_EMAIL]})
    no_user = admin.put('/api/admin/tags/developer/responsibles', json={'emails': ['ghost@example.com']})
    too_many = admin.put('/api/admin/tags/developer/responsibles', json={'emails': [f'u{n}@example.com' for n in range(21)]})

    # then
    assert no_tag.status_code == 404 and 'nope' in no_tag.json()['detail']
    assert no_user.status_code == 422 and 'ghost@example.com' in no_user.json()['detail']
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


def test_a_missing_email_scope_shows_as_a_notice_and_next_to_the_report_setting(store: Store, client):
    # given
    store.note_email_scope(True, '2026-11-20T10:00:00Z')

    # when
    notices = client.get('/api/admin/notices', headers=AUTH).json()['items']
    item = next(i for i in client.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == 'tags.report-at')
    listed = client.get('/api/admin/tags', headers=AUTH).json()
    store.note_email_scope(False, '2026-11-20T11:00:00Z')
    after = client.get('/api/admin/notices', headers=AUTH).json()['items']

    # then
    warning = 'Slack app is missing users:read.email; responsibles are named without a ping'
    assert [n['text'] for n in notices if n['id'] == 'tag-report-scope'] == [warning]
    assert item['problem'] == warning
    assert listed['emailScopeMissing'] is True
    assert [n for n in after if n['id'] == 'tag-report-scope'] == []
