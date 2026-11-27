import dataclasses
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skopax import locales, messages
from skopax.app import create_app
from skopax.contract import Knowledge
from skopax.semantic.search import Hit, SearchResult, SearchUnavailable
from tests.conftest import settings
from skopax.users import UserExists, Users
from tests.test_api_tokens import PASSWORD, add_user, sign_in
from tests.test_semantic_search import KEY, QUERY, SVC, Box, add_closed, add_run, conf

HIDDEN = ('held', 'releasing', 'dropped', 'discarded')
GUARD_FIELDS = {'guard', 'held', 'vec', 'screenshots', 'secrets'}
ITEM_KEYS = {'kind', 'title', 'sub', 'when', 'link', 'match', 'score'}
LANGS = ('de', 'es', 'fr', 'tr')


@pytest.fixture
def box(store) -> Box:
    return Box(store)


def seed(box: Box, store) -> dict:
    ids = {}
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')
    for outcome in HIDDEN:
        add_run(store, f'hidden-{outcome}', outcome=outcome, guard={'reason': 'secret', 'category': 'x'}, held={'text': 'draft', 'until': 'later'}, screenshots=['s1'])
        box.put('run', f'hidden-{outcome}', svc=SVC, trigger='review')
    add_run(store, 'chat-run', trigger='chat', outcome='done')
    box.put('run', 'chat-run', svc=SVC, trigger='chat')
    ids['card'] = add_closed(store)
    box.put('card', ids['card'], svc=SVC, label='High CPU', sev='warn')
    store.add_deploys([{'ns': 'shop', 'svc': SVC, 'toImage': 'reg/app:2', 'fromImage': 'reg/app:1', 'at': '2026-11-20T08:00:00Z'}])
    ids['deploy'] = str(store.deploys.find_one({})['_id'])
    box.put('change', f"deploy:{ids['deploy']}", svc=SVC, ns='shop')
    store.add_deploy_report({'summary': '1 deploy: 1 flagged', 'rows': [], 'brief': {'headline': 'Errors up', 'points': ['p']}})
    ids['report'] = str(store.deploy_reports.find_one({})['_id'])
    box.put('change', f"report:{ids['report']}", svc='', ns='')
    box.items = [('shop', '', Knowledge('pool', 'Pool notes', 'How the pool is sized', slack=True))]
    box.put('knowledge', 'shop:pool', plugin='shop', slack=True)
    return ids


def make(store, box: Box, public: bool = True, role: str | None = None, **over):
    app = create_app(dataclasses.replace(settings(public_read=public), **over), store)
    app.state.semantic_searcher = box.searcher
    client = TestClient(app, base_url='https://testserver', follow_redirects=False)
    if role:
        email = f'{role}@example.com'
        try:
            add_user(store, email, role)
        except UserExists:
            Users(store.db).set_password(email, PASSWORD)
        sign_in(client, email)
    return client


def keys_of(value) -> set:
    if isinstance(value, dict):
        return set(value) | set().union(*(keys_of(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_of(v) for v in value))
    return set()


@pytest.mark.parametrize('role', ['admin', 'operator', 'viewer'])
def test_signed_in_roles_get_every_group_without_hidden_runs_or_guard_fields(store, box, role):
    # given
    ids = seed(box, store)
    client = make(store, box, role=role)

    # when
    res = client.get('/api/search', params={'q': QUERY, 'limit': 20})

    # then
    body = res.json()
    assert res.status_code == 200 and body['semantic'] is True and body['note'] is None
    items = {(i['kind'], i['link']): i for i in body['items']}
    assert ('runs', '/api/investigations/ok') in items
    assert any(k == 'cards' for k, _ in items) and ('pages', '#t=integrations') in items
    assert ('changes', f"#rel={ids['deploy']}") in items and ('changes', f"#r={ids['report']}") in items
    assert all(i['match'] == 'meaning' and set(i) <= ITEM_KEYS and isinstance(i['score'], float) for i in body['items'])
    text = json.dumps(body)
    assert not any(f'hidden-{o}' in text for o in HIDDEN) and 'chat-run' not in text
    assert not GUARD_FIELDS & keys_of(body) and 'secret' not in text and 'draft' not in text


def test_a_card_links_to_its_number_or_key_and_runs_to_their_record(store, box):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')
    did = add_closed(store)
    box.put('card', did, svc=SVC, label='High CPU', sev='warn')
    client = make(store, box, role='viewer')

    # when
    body = client.get('/api/search', params={'q': QUERY, 'kinds': 'cards,runs'}).json()

    # then
    by = {i['kind']: i for i in body['items']}
    assert by['cards']['title'] == 'High CPU' and re.fullmatch(r'#c=\d+|#a=.+', by['cards']['link'])
    assert by['cards']['sub'].endswith(SVC)
    assert by['runs']['link'] == '/api/investigations/ok' and by['runs']['title'] == 'why is checkout slow'


def test_anonymous_with_public_read_sees_no_pages_or_knowledge(store, box):
    # given
    seed(box, store)
    client = make(store, box, public=True)

    # when
    res = client.get('/api/search', params={'q': QUERY, 'limit': 20})

    # then
    kinds = {i['kind'] for i in res.json()['items']}
    assert res.status_code == 200 and kinds == {'cards', 'runs', 'changes'}
    assert 'pages' not in kinds and 'errors' not in kinds
    assert not GUARD_FIELDS & keys_of(res.json())


def test_anonymous_without_public_read_is_refused(store, box):
    # given
    seed(box, store)
    client = make(store, box, public=False)

    # when
    res = client.get('/api/search', params={'q': QUERY})

    # then
    assert res.status_code in (401, 303) and 'items' not in res.text


def test_an_anonymous_ask_for_pages_returns_nothing(store, box):
    # given
    seed(box, store)
    client = make(store, box, public=True)

    # when
    body = client.get('/api/search', params={'q': QUERY, 'kinds': 'pages,errors'}).json()

    # then
    assert body['items'] == []


def test_kinds_narrow_the_result(store, box):
    # given
    seed(box, store)
    client = make(store, box, role='admin')

    # when
    body = client.get('/api/search', params={'q': QUERY, 'kinds': 'changes', 'limit': 20}).json()

    # then
    assert {i['kind'] for i in body['items']} == {'changes'}


@pytest.mark.parametrize('params, code', [
    ({'q': 'ab'}, 422), ({'q': '  a  '}, 422), ({'q': 'x' * 201}, 422), ({'q': 'abc', 'limit': 21}, 422), ({'q': 'abc', 'limit': 0}, 422),
    ({'q': 'abc', 'kinds': 'cards,nope'}, 422), ({}, 422), ({'q': 'abc', 'limit': 20}, 200), ({'q': 'x' * 200}, 200), ({'q': 'abc', 'kinds': 'cards,runs,changes,pages,errors'}, 200),
])
def test_bounds(store, box, params, code):
    # given
    client = make(store, box, role='viewer')

    # when
    res = client.get('/api/search', params=params)

    # then
    assert res.status_code == code


def test_limit_caps_the_rows(store, box):
    # given
    for n in range(4):
        add_run(store, f'r{n}')
        box.put('run', f'r{n}', svc=SVC, trigger='review')
    client = make(store, box, role='viewer')

    # when
    body = client.get('/api/search', params={'q': QUERY, 'limit': 2}).json()

    # then
    assert len(body['items']) == 2


def test_the_thirty_first_search_in_a_minute_is_throttled_per_user(store, box):
    # given
    client = make(store, box, role='viewer')
    other = make(store, box, role='operator')

    # when
    codes = [client.get('/api/search', params={'q': QUERY}).status_code for _ in range(31)]
    again = other.get('/api/search', params={'q': QUERY}).status_code

    # then
    assert codes[:30] == [200] * 30 and codes[30] == 429 and again == 200


def test_anonymous_searches_are_throttled_per_address(store, box):
    # given
    client = make(store, box, public=True)

    # when
    codes = [client.get('/api/search', params={'q': QUERY}).status_code for _ in range(31)]

    # then
    assert codes[:30] == [200] * 30 and codes[30] == 429


def test_the_throttle_answer_carries_the_server_message(store, box):
    # given
    client = make(store, box, role='viewer')
    for _ in range(30):
        client.get('/api/search', params={'q': QUERY})

    # when
    res = client.get('/api/search', params={'q': QUERY})

    # then
    assert res.status_code == 429 and res.json()['detail'] == messages.MESSAGES['search.throttled']


@pytest.mark.parametrize('role', ['viewer', None])
def test_semantic_off_gives_the_note_and_word_matches(store, box, role):
    # given
    add_run(store, 'words', recap='Checkout latency came from the retry storm.')
    add_closed(store, label='checkout CPU', note={'text': 'checkout pool'})
    box.on = False
    client = make(store, box, role=role)

    # when
    res = client.get('/api/search', params={'q': 'checkout'})

    # then
    body = res.json()
    assert res.status_code == 200 and body['semantic'] is False and body['note'].startswith('Search by meaning is off: ')
    assert {i['kind'] for i in body['items']} == {'cards', 'runs'}
    assert all(i['match'] == 'words' and 'score' not in i for i in body['items'])


def test_an_unavailable_database_gives_503_and_no_rows(store, box):
    # given
    class Down:
        def search(self, *a, **k):
            raise SearchUnavailable()

    client = make(store, box, role='viewer')
    client.app.state.semantic_searcher = Down()

    # when
    res = client.get('/api/search', params={'q': QUERY})

    # then
    assert res.status_code == 503 and 'items' not in res.json()


def test_the_caller_role_reaches_the_searcher(store, box):
    # given
    seen = []

    class Spy:
        def search(self, ctx, text, kinds, limit, service=None):
            seen.append((ctx.mode, ctx.role, text, kinds, limit))
            return SearchResult(True, None, None, [Hit('run', 'x', 0.9, {'id': 'x', 'recap': 'r', 'startedAt': '2026-11-20T10:00:00Z'})])

    client = make(store, box, role='operator')
    client.app.state.semantic_searcher = Spy()
    anon = make(store, box, public=True)
    anon.app.state.semantic_searcher = Spy()

    # when
    client.get('/api/search', params={'q': ' pool leak ', 'kinds': 'cards,pages', 'limit': 3})
    anon.get('/api/search', params={'q': 'pool leak'})

    # then
    assert seen[0] == ('ui', 'operator', 'pool leak', ['card', 'open', 'knowledge', 'doc'], 3)
    assert seen[1][:2] == ('ui', 'anonymous') and seen[1][3] is None


def test_rows_without_a_title_or_link_are_dropped(store, box):
    # given
    class Odd:
        def search(self, ctx, text, kinds, limit, service=None):
            return SearchResult(True, None, None, [Hit('run', 'a', 0.9, {}), Hit('card', 'b', 0.9, {'label': 'x'}), Hit('nope', 'c', 0.9, {'title': 't'}),
                                                   Hit('run', 'd', 0.9, {'id': 'd', 'recap': 'ok'})])

    client = make(store, box, role='viewer')
    client.app.state.semantic_searcher = Odd()

    # when
    body = client.get('/api/search', params={'q': 'anything'}).json()

    # then
    assert [i['link'] for i in body['items']] == ['/api/investigations/d']


def test_the_default_searcher_is_the_apps_own(store):
    # given
    app = create_app(settings(), store)

    # then
    assert app.state.semantic_searcher is not None


def test_the_new_strings_are_translated():
    # given
    root = Path(locales.__file__).parent / 'locale'

    # then
    for lang in LANGS:
        page = json.loads((root / lang / 'page.json').read_text(encoding='utf-8'))['page']
        for key in ('Search by meaning', 'Search by meaning: cards, runs, changes, pages', 'Showing word matches.', 'meaning', 'words', 'No results', '{0} result', '{0} results',
                    'No word matches. {0} similar by meaning', 'Too many searches, wait a minute and try again.', 'Search is not available right now.', 'Nothing matches.'):
            assert page[key].strip() and page[key] != key, (lang, key)
        assert '{0}' in page['No word matches. {0} similar by meaning']


def test_errors_and_docs_are_shaped_and_a_card_less_error_has_no_link(store, box):
    # given
    class Rows:
        def search(self, ctx, text, kinds, limit, service=None):
            return SearchResult(True, None, None, [
                Hit('errtpl', 'e1', 0.9, {'kind': 'errtpl', 'svc': 'checkout-service', 'level': 'ERROR', 'template': 'pool exhausted after <n> tries', 'lastSeen': '2026-11-20T10:00:00Z', 'card': 'k|1'}),
                Hit('errtpl', 'e2', 0.9, {'kind': 'errtpl', 'svc': 'cart', 'level': 'WARN', 'template': 'slow'}),
                Hit('doc', 'd1', 0.9, {'title': 'Runbook', 'excerpt': 'steps', 'url': 'https://wiki.example.com/pool'}),
                Hit('doc', 'd2', 0.9, {'title': 'Unsafe', 'excerpt': 'x', 'url': 'javascript:alert(1)'}),
                Hit('card', 'c', 0.9, {'label': 'x'})])

    client = make(store, box, role='viewer')
    client.app.state.semantic_searcher = Rows()

    # when
    items = client.get('/api/search', params={'q': 'pool'}).json()['items']

    # then
    by = {i['title']: i for i in items}
    assert by['checkout-service · ERROR']['link'] == '#a=k%7C1' and by['checkout-service · ERROR']['sub'] == 'pool exhausted after <n> tries'
    assert by['checkout-service · ERROR']['kind'] == 'errors' and by['cart · WARN']['link'] == ''
    assert by['Runbook']['link'] == 'https://wiki.example.com/pool' and by['Unsafe']['link'] == '#t=integrations'
    assert len(items) == 4


def test_the_throttled_answer_says_when_to_retry(store, box):
    # given
    client = make(store, box, role='viewer')
    for _ in range(30):
        client.get('/api/search', params={'q': QUERY})

    # when
    res = client.get('/api/search', params={'q': QUERY})

    # then
    assert res.status_code == 429 and 1 <= int(res.headers['Retry-After']) <= 60


@pytest.mark.parametrize('lang', LANGS)
def test_the_throttle_message_is_translated_and_outbound(lang):
    # given
    pack = json.loads((Path(locales.__file__).parent / 'locale' / lang / 'messages.json').read_text(encoding='utf-8'))

    # then
    assert messages.outbound('search.throttled') and pack['search.throttled'].strip() and pack['search.throttled'] != messages.MESSAGES['search.throttled']


def test_a_log_line_in_the_ui_query_is_normalised_like_the_investigator_does(store, box):
    # given
    seen = []

    class Spy:
        def search(self, ctx, text, kinds, limit, service=None):
            seen.append((text, kinds))
            return SearchResult(True, None, None, [])

    client = make(store, box, role='viewer')
    client.app.state.semantic_searcher = Spy()
    line = '2026-11-20T10:00:00Z ERROR connection refused to 10.1.2.3:5432 request 8f14e45f-ceea-467f-a0e6-b3a2f0f4c9d1'

    # when
    client.get('/api/search', params={'q': line})
    client.get('/api/search', params={'q': line, 'kinds': 'errors'})
    client.get('/api/search', params={'q': line, 'kinds': 'runs'})

    # then
    assert seen[0][0] != line and seen[0][0] == seen[1][0] and seen[2][0] == line
