import json

from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.test_auth import sso_settings
from tests.test_read_gate import login_cookie
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.store import Store
from skopax.users import Users

OPERATOR = 'olga.page@example.com'
CANARIES = ('canary-question', 'canary-answer', 'canary-reason', 'canary-context', 'canary-fix-reason', 'canary-first-reason')


def chat_run(run_id: str, guard: dict, started: str = '2026-11-26T10:00:00Z') -> dict:
    return {'_id': run_id, 'trigger': 'chat', 'mode': 'live', 'keys': [], 'startedAt': started, 'outcome': 'done', 'usd': 0.02, 'recap': 'canary-answer',
            'ask': {'text': 'canary-question', 'by': OPERATOR}, 'toolCalls': [], 'writes': [], 'guard': guard}


def full_guard(**over) -> dict:
    return {'questions': 'pass', 'answers': 'mask', 'masked': 2, 'ms': 40, 'model': 'claude-haiku-5-5', 'reason': 'canary-reason', 'category': 'leak', 'noted': True,
            'question': 'canary-question', 'context': 'canary-context', 'fix': {'answers': 'pass', 'reason': 'canary-fix-reason'},
            'first': {'questions': 'pass', 'answers': 'hold', 'reason': 'canary-first-reason'}} | over


def leaks(found) -> list[str]:
    seen = json.dumps(found, default=str)
    return [c for c in CANARIES if c in seen]


def test_an_admin_sees_the_guard_result_of_a_private_chat_but_no_text_or_reason(store: Store):
    # given
    store.investigations.insert_one(chat_run('chat-1', full_guard()))
    store.investigations.insert_one(chat_run('chat-2', {'questions': None, 'answers': 'hold', 'masked': 0, 'watch': True, 'reason': 'canary-reason', 'category': 'injection'},
                                             '2026-11-26T11:00:00Z'))

    # when
    with TestClient(create_app(settings(), store)) as c:
        one = c.get('/api/investigations/chat-1', headers=AUTH).json()
        listed = {r['id']: r for r in c.get('/api/investigations', params={'limit': 50}, headers=AUTH).json()}

    # then
    assert one['guard'] == {'questions': 'pass', 'answers': 'mask', 'masked': 2, 'category': 'leak', 'noted': True, 'fix': {'answers': 'pass'}, 'first': {'questions': 'pass', 'answers': 'hold'}}
    assert one['ask'] == {'private': True}
    assert listed['chat-1']['guard'] == {'questions': 'pass', 'answers': 'mask', 'masked': 2, 'noted': True}
    assert listed['chat-2']['guard'] == {'questions': None, 'answers': 'hold', 'masked': 0, 'watch': True}
    assert leaks(one) == [] and leaks(list(listed.values())) == []


def test_people_who_are_not_admins_see_no_guard_result_on_a_private_chat(store: Store):
    # given
    store.investigations.insert_one(chat_run('chat-1', full_guard()))
    Users(store.db).create(OPERATOR, 'Olga Page', 'operator')
    operator = login_cookie(store, OPERATOR, 'operator')

    # when
    with TestClient(create_app(sso_settings(), store)) as c:
        c.cookies.set(SESSION_COOKIE, operator)
        one = c.get('/api/investigations/chat-1').json()
        listed = [r for r in c.get('/api/investigations', params={'limit': 50}).json() if r['id'] == 'chat-1']

    # then
    assert 'guard' not in one and one['ask'] == {'private': True}
    assert listed and 'guard' not in listed[0]
    assert leaks(one) == [] and leaks(listed) == []
