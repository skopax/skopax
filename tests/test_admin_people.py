import logging

from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings, state
from tests.fake_slack import fake_slack
from tests.test_tag_responsibles import ROLE, app_client, signed_in
from skopax import tagreport
from skopax.app import create_app
from skopax.slack import READ_METHODS, SlackError
from skopax.store import Store
from skopax.users import Users

NINA = 'nina.white@example.com'
CHLOE = 'chloe.martin@example.com'
DANIEL = 'daniel.brook@example.com'


def member(uid: str, real: str, email: str | None, display: str = '', **flags) -> dict:
    return {'id': uid, 'name': real.split()[0].lower(), 'real_name': real, 'profile': {'real_name': real, 'display_name': display, **({'email': email} if email else {})}, **flags}


def workspace() -> list[dict]:
    return [member('U1', 'Chlo\u00e9 Martin', 'Chloe.Martin@example.com', 'chloe'),
            member('U2', 'Daniel Brook', DANIEL, 'dan'),
            member('U3', 'Old Timer', 'old.timer@example.com', deleted=True),
            member('U4', 'Build Bot', 'bot@example.com', is_bot=True),
            member('U5', 'Guest Person', 'guest@example.com', is_restricted=True),
            member('U6', 'Single Guest', 'single@example.com', is_ultra_restricted=True),
            member('USLACKBOT', 'Slackbot', 'slackbot@example.com'),
            member('U7', 'No Mail', None)]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def inline(app) -> Clock:
    clock = Clock()
    app.state.slack_people.spawn = lambda job: job()
    app.state.slack_people.clock = clock
    return clock


def test_users_list_is_a_read_method():
    # then
    assert 'users.list' in READ_METHODS


def test_only_admins_search_people(store: Store):
    # given
    store.save(state(), None)
    guest, viewer, operator = app_client(store), signed_in(store, ()), signed_in(store, (ROLE,))

    # when
    codes = [c.get('/api/admin/people?q=ni').status_code for c in (guest, viewer, operator)]

    # then
    assert codes == [401, 403, 403]


def test_a_query_shorter_than_two_letters_finds_nobody_and_asks_slack_nothing(store: Store):
    # given
    Users(store.db).create(NINA, 'Nina White', 'operator')
    slack, _, api = fake_slack()
    api.members = workspace()

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        inline(c.app)
        short = c.get('/api/admin/people?q=%20n%20', headers=AUTH).json()

    # then
    assert short['people'] == []
    assert 'users.list' not in api.methods()


def test_people_match_any_part_of_the_name_or_address_ignoring_case_and_accents_from_every_source(store: Store):
    # given
    Users(store.db).create(NINA, 'Nina White', 'operator')
    Users(store.db).create('off.person@example.com', 'Nina Off', 'viewer')
    Users(store.db).set_disabled('off.person@example.com', True)
    store.set_tag_owners('developer', ['outside.nina@example.org'])
    slack, _, api = fake_slack()
    api.members = workspace() + [member('U9', 'Nina White', NINA, 'nina'), member('U10', 'Zo\u00eb Hart', 'zh@example.com')]

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        inline(c.app)
        nina = c.get('/api/admin/people?q=NIN', headers=AUTH).json()
        chloe = c.get('/api/admin/people?q=chlo', headers=AUTH).json()
        accent = c.get('/api/admin/people?q=zoe', headers=AUTH).json()
        middle = c.get('/api/admin/people?q=rook', headers=AUTH).json()
        by_mail = c.get('/api/admin/people?q=example.org', headers=AUTH).json()
        display = c.get('/api/admin/people?q=dan', headers=AUTH).json()
        skipped = [c.get(f'/api/admin/people?q={q}', headers=AUTH).json()['people'] for q in ('timer', 'bot@', 'guest', 'slackbot', 'no mail')]

    # then
    assert nina == {'people': [{'email': NINA, 'name': 'Nina White', 'source': 'skopax'}, {'email': 'outside.nina@example.org', 'name': 'outside.nina', 'source': 'responsible'}], 'slack': 'ok'}
    assert chloe['people'] == [{'email': CHLOE, 'name': 'Chlo\u00e9 Martin', 'source': 'slack'}]
    assert accent['people'] == [{'email': 'zh@example.com', 'name': 'Zo\u00eb Hart', 'source': 'slack'}], 'only the accented name matches'
    assert middle['people'] == display['people'] == [{'email': DANIEL, 'name': 'Daniel Brook', 'source': 'slack'}]
    assert [p['email'] for p in by_mail['people']] == ['outside.nina@example.org']
    assert skipped == [[], [], [], [], []]


def test_at_most_twenty_people_come_back(store: Store):
    # given
    slack, _, api = fake_slack()
    api.members = [member(f'U{n}', f'Sam Person{n:02d}', f'sam{n:02d}@example.com') for n in range(30)]

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        inline(c.app)
        found = c.get('/api/admin/people?q=sam', headers=AUTH).json()['people']

    # then
    assert len(found) == 20


def test_the_slack_list_is_paged_built_once_and_reused_for_six_hours(store: Store):
    # given
    slack, _, api = fake_slack()
    api.members = workspace()
    api.page_size = 3

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        clock = inline(c.app)
        first = c.get('/api/admin/people?q=chlo', headers=AUTH).json()
        pages = api.methods().count('users.list')
        clock.now += 6 * 3600 - 1
        c.get('/api/admin/people?q=dani', headers=AUTH)
        reused = api.methods().count('users.list')
        clock.now += 2
        api.members = api.members + [member('U8', 'Erin Vale', 'erin.vale@example.com')]
        fresh = c.get('/api/admin/people?q=erin', headers=AUTH).json()

    # then
    assert first['people'] == [{'email': CHLOE, 'name': 'Chlo\u00e9 Martin', 'source': 'slack'}]
    assert pages == 3 and reused == 3
    assert fresh['people'] == [{'email': 'erin.vale@example.com', 'name': 'Erin Vale', 'source': 'slack'}]


def test_the_list_is_built_in_the_background_and_the_first_answer_says_loading():
    # given
    slack, _, api = fake_slack()
    api.members = workspace()
    jobs = []
    people = tagreport.SlackPeople(slack, clock=Clock(), spawn=jobs.append)

    # when
    first = people.lookup()
    again = people.lookup()
    jobs[0]()
    after = people.lookup()

    # then
    assert first == ([], 'loading') and again == ([], 'loading')
    assert len(jobs) == 1
    assert after[1] == 'ok' and [p['email'] for p in after[0]] == [CHLOE, DANIEL]


def refusing(code: str):
    def api(token: str, method: str, payload: dict) -> dict:
        raise SlackError(f'Slack refused: {code}', code=code, needed='users:read' if code == 'missing_scope' else None)
    return api


def test_a_slack_failure_or_missing_scope_answers_with_local_people_and_a_flag_and_retries_later(store: Store, caplog):
    # given
    Users(store.db).create(NINA, 'Nina White', 'operator')
    slack, _, api = fake_slack()
    api.members = workspace()
    caplog.set_level(logging.INFO)

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        clock = inline(c.app)
        slack.api = refusing('missing_scope')
        scope = c.get('/api/admin/people?q=ni', headers=AUTH)
        slack.api = api
        api.fail = {'users.list'}
        paused = c.get('/api/admin/people?q=ni', headers=AUTH).json()
        asked = api.methods().count('users.list')
        clock.now += tagreport.FAILURE_TTL_S + 1
        down = c.get('/api/admin/people?q=ni', headers=AUTH).json()
        api.fail = set()
        clock.now += tagreport.FAILURE_TTL_S + 1
        back = c.get('/api/admin/people?q=chlo', headers=AUTH).json()

    # then
    assert scope.status_code == 200
    assert scope.json() == {'people': [{'email': NINA, 'name': 'Nina White', 'source': 'skopax'}], 'slack': 'unavailable'}
    assert paused['slack'] == 'unavailable' and asked == 0
    assert down['slack'] == 'unavailable' and down['people'] == scope.json()['people']
    assert back == {'people': [{'email': CHLOE, 'name': 'Chlo\u00e9 Martin', 'source': 'slack'}], 'slack': 'ok'}
    assert '@' not in caplog.text
    assert 'missing_scope' in caplog.text


def test_without_slack_the_local_people_come_back_and_slack_is_off(store: Store, client):
    # given
    Users(store.db).create(NINA, 'Nina White', 'operator')

    # when
    found = client.get('/api/admin/people?q=white', headers=AUTH).json()

    # then
    assert found == {'people': [{'email': NINA, 'name': 'Nina White', 'source': 'skopax'}], 'slack': 'off'}


def test_a_disabled_user_is_not_offered_as_a_responsible_elsewhere(store: Store, client):
    # given
    Users(store.db).create('nina.off@example.com', 'Nina Off', 'viewer')
    Users(store.db).set_disabled('nina.off@example.com', True)
    store.set_tag_owners('developer', ['nina.off@example.com'])

    # when
    found = client.get('/api/admin/people?q=nina', headers=AUTH).json()['people']

    # then
    assert found == []


def test_a_rate_limited_page_waits_and_tries_again(store: Store):
    # given
    slack, _, api = fake_slack()
    api.members = workspace()
    waits, refusals = [], [2]

    def limited(token: str, method: str, payload: dict) -> dict:
        if method == 'users.list' and refusals[0]:
            refusals[0] -= 1
            e = SlackError('Slack asked to slow down', code='ratelimited')
            e.wait = 3.0
            raise e
        return api(token, method, payload)

    slack.api = limited
    slack.sleep = waits.append

    # when
    found = slack.members()

    # then
    assert waits == [3.0, 3.0]
    assert [p['email'] for p in found] == [CHLOE, DANIEL]
