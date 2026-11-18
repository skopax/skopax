from tests.conftest import AUTH, state
from tests.test_auth import ROLE, TokenEndpoint, access_token, app_client, log_in
from skopax.investigator import prompt
from skopax.store import Store

ADMIN = 'SKOPAX_ADMIN'
RULES = 'Answer in one short sentence, friendly, no emoji; point prod questions to a new message.'


def admin_of(store: Store, roles=(ADMIN,)):
    client = app_client(store, TokenEndpoint(access_token(list(roles))))
    log_in(client)
    return client


def test_only_admins_and_the_token_see_and_change_the_small_talk_rules(store: Store):
    # given
    store.save(state(), None)
    guest, operator = app_client(store), admin_of(store, (ROLE,))

    # when
    codes = [guest.get('/api/admin/small-talk').status_code, operator.get('/api/admin/small-talk').status_code,
             operator.put('/api/admin/small-talk', json={'enabled': True, 'rules': RULES, 'hourlyCap': 5}).status_code,
             operator.delete('/api/admin/small-talk').status_code]
    by_token = guest.get('/api/admin/small-talk', headers=AUTH)

    # then
    assert codes == [401, 403, 403, 403]
    assert by_token.status_code == 200
    body = by_token.json()
    assert (body['custom'], body['enabled'], body['hourlyCap'], body['rules']) == (False, True, 10, prompt.SMALL_TALK_RULES)
    assert body['defaults'] == {'enabled': True, 'rules': prompt.SMALL_TALK_RULES, 'hourlyCap': 10} and body['usedThisHour'] == 0
    assert store.small_talk_rules() is None


def test_an_admin_saves_the_rules_each_change_lands_in_history_and_back_to_default_clears_them(store: Store):
    # given
    store.save(state(), None)
    admin = admin_of(store)

    # when
    saved = admin.put('/api/admin/small-talk', json={'enabled': False, 'rules': RULES, 'hourlyCap': 3})
    same = admin.put('/api/admin/small-talk', json={'enabled': False, 'rules': RULES, 'hourlyCap': 3})
    live = admin.get('/api/small-talk').json()
    history_saved = admin.get('/api/admin/history').json()
    reset = admin.delete('/api/admin/small-talk')
    history = admin.get('/api/admin/history').json()

    # then
    assert saved.status_code == 200 and saved.json()['custom'] is True
    assert (live['enabled'], live['hourlyCap'], live['rules'], live['updatedBy']) == (False, 3, RULES, 'oscar.green@example.com')
    assert [(h['key'], h['old'], h['new'], h['talk']) for h in history_saved] == [('small-talk.rules', prompt.SMALL_TALK_RULES, RULES, True),
                                                                                 ('small-talk.hourly-cap', '10', '3', True), ('small-talk.enabled', 'on', 'off', True)]
    assert same.status_code == 200, 'saving the same values adds no history rows'
    assert reset.json()['custom'] is False and store.small_talk_rules() is None
    assert (reset.json()['enabled'], reset.json()['hourlyCap'], reset.json()['rules']) == (True, 10, prompt.SMALL_TALK_RULES)
    assert [h['key'] for h in history][:3] == ['small-talk.rules', 'small-talk.hourly-cap', 'small-talk.enabled'] and len(history) == 6
    lines = [a['text'] for a in store.list_activity(None, 50) if a['kind'] == 'admin']
    assert lines == [f'Small talk on · 10 an hour · rules {len(prompt.SMALL_TALK_RULES)} characters · back to the default', f'Small talk off · 3 an hour · rules {len(RULES)} characters']


def test_bad_values_are_refused_and_the_token_edit_also_lands_in_history(store: Store):
    # given
    store.save(state(), None)
    admin, guest = admin_of(store), app_client(store)

    # when
    empty = admin.put('/api/admin/small-talk', json={'enabled': True, 'rules': '   ', 'hourlyCap': 5}).status_code
    over = admin.put('/api/admin/small-talk', json={'enabled': True, 'rules': RULES, 'hourlyCap': 61}).status_code
    extra = admin.put('/api/admin/small-talk', json={'enabled': True, 'rules': RULES, 'hourlyCap': 5, 'updatedBy': 'x'}).status_code
    token = guest.put('/api/small-talk', headers=AUTH, json={'enabled': True, 'rules': RULES, 'hourlyCap': 10})
    history = admin.get('/api/admin/history').json()

    # then
    assert (empty, over, extra) == (422, 422, 422)
    assert token.status_code == 200 and token.json()['updatedBy'] == 'token:bootstrap'
    assert [(h['key'], h['by'], h['talk']) for h in history] == [('small-talk.rules', 'token:bootstrap', True)]
