import asyncio
import dataclasses
import logging
import re

import mongomock
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from tests.conftest import RELEASE_IGNORE, settings
from skopax.app import FORM_BODY_MAX, create_app, read_form
from skopax.auth import SESSION_COOKIE
from skopax.messages import msg
from skopax.setup import CODE_ALPHABET, BadCode, SetupDone, SetupGate
from skopax.store import Store
from skopax.users import InvalidUser, Users, derive

CODE_LINE = re.compile(r'Skopax setup: open (\S*)setup and enter the code ([A-Z0-9]{4}-[A-Z0-9]{4})')
PASSWORD = 'correct horse battery'
HTML = {'Accept': 'text/html'}


def fresh_store() -> Store:
    return Store(mongomock.MongoClient(tz_aware=True)['skopax'], release_ignore=RELEASE_IGNORE, self_deploy='platform/skopax')


def form(code: str, **over) -> dict:
    return {'code': code, 'email': 'Alice@Example.com', 'name': 'Alice', 'password': PASSWORD, 'password2': PASSWORD} | over


def issued_code(caplog) -> str:
    found = [m.group(2) for r in caplog.records if (m := CODE_LINE.search(r.getMessage()))]
    assert len(found) == 1
    return found[0]


def app_client(store: Store, **over) -> TestClient:
    return TestClient(create_app(dataclasses.replace(settings(), **over), store), base_url='https://testserver', follow_redirects=False)


def test_fresh_db_redirects_pages_to_setup_and_api_503():
    # given
    store = fresh_store()

    # when
    with app_client(store) as c:
        page = c.get('/', headers=HTML)
        card = c.get('/c/7', headers=HTML)
        api = c.get('/api/state')
        post = c.post('/api/actions', json={})

    # then
    assert (page.status_code, page.headers['location']) == (303, '/setup')
    assert (card.status_code, card.headers['location']) == (303, '/setup')
    assert api.status_code == 503
    assert api.json() == {'detail': 'Skopax is not set up yet: open /setup'}
    assert post.status_code == 503


def test_code_is_issued_once_and_logged(caplog):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')

    # when
    with app_client(store) as c:
        code = issued_code(caplog)
        again = SetupGate(store.db, Users(store.db)).issue_code()
        c.get('/setup')

    # then
    line = next(r for r in caplog.records if CODE_LINE.search(r.getMessage()))
    assert line.levelno == logging.WARNING
    assert line.getMessage() == f'Skopax setup: open https://skopax.example.com/setup and enter the code {code}'
    assert re.fullmatch(f'[{CODE_ALPHABET}]{{4}}-[{CODE_ALPHABET}]{{4}}', code)
    assert again is None
    assert len([r for r in caplog.records if CODE_LINE.search(r.getMessage())]) == 1
    assert store.db['meta'].find_one({'_id': 'setup-code'})['hash'] != code


def test_setup_creates_admin_and_signs_in(caplog):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    with app_client(store) as c:
        code = issued_code(caplog)
        shown = c.get('/setup')

        # when
        res = c.post('/setup', data=form(code.lower()))
        me = c.get('/api/me')
        page = c.get('/', headers=HTML)

    # then
    assert shown.status_code == 200 and 'name="code"' in shown.text and 'http' not in shown.text.replace('https://testserver', '').replace('https://skopax.io', '')
    assert (res.status_code, res.headers['location']) == (303, '/')
    cookie = res.headers['set-cookie']
    assert cookie.startswith(f'{SESSION_COOKIE}=') and 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie
    assert me.json()['user'] == 'alice@example.com' and me.json()['admin'] is True
    assert page.status_code == 200
    user = Users(store.db).get('alice@example.com')
    assert (user['role'], user['name'], user['source']) == ('admin', 'Alice', 'local')
    assert store.db['meta'].find_one({'_id': 'setup-code'}) is None


@pytest.mark.parametrize('over', [{'code': 'AAAA-AAAA'}, {'password2': 'something else entirely'}, {'password': 'short', 'password2': 'short'}, {'email': 'nope'}, {'name': ' '}])
def test_wrong_code_or_mismatch_creates_nothing(caplog, over):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    with app_client(store) as c:
        code = issued_code(caplog)

        # when
        res = c.post('/setup', data=form(code) | over)
        after = Users(store.db).count()
        retry = c.post('/setup', data=form(code))

    # then
    assert res.status_code == 400
    assert msg('setup.error') in res.text
    assert PASSWORD not in res.text and 'Set-Cookie' not in res.headers
    assert after == 0
    assert retry.status_code == 303


def test_form_error_escapes_what_was_typed(caplog):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    with app_client(store) as c:
        code = issued_code(caplog)

        # when
        res = c.post('/setup', data=form(code, name='<script>x</script>', password2='no'))

    # then
    assert '<script>x' not in res.text and '&lt;script&gt;x' in res.text


def test_two_setups_create_one_admin():
    # given
    store = fresh_store()
    users = Users(store.db)
    gate = SetupGate(store.db, users)
    code = gate.issue_code()

    # when
    first = gate.complete(code, 'alice@example.com', 'Alice', PASSWORD)
    with pytest.raises(SetupDone):
        gate.complete(code, 'bob@example.com', 'Bob', PASSWORD)

    # then
    assert first['role'] == 'admin'
    assert users.count() == 1
    assert gate.needed() is False


def test_a_second_complete_during_the_first_ones_create_loses():
    # given
    store = fresh_store()
    inner = {}

    class Racing(Users):
        def create(self, *a, **kw):
            try:
                inner['gate'].complete(inner['code'], 'bob@example.com', 'Bob', PASSWORD)
                inner['result'] = 'won'
            except (SetupDone, BadCode) as e:
                inner['result'] = type(e).__name__
            return super().create(*a, **kw)

    users = Racing(store.db)
    gate = SetupGate(store.db, users)
    inner |= {'gate': gate, 'code': gate.issue_code()}

    # when
    first = gate.complete(inner['code'], 'alice@example.com', 'Alice', PASSWORD)

    # then
    assert first['email'] == 'alice@example.com'
    assert inner['result'] in ('BadCode', 'SetupDone')
    assert users.count() == 1


def test_leftover_setup_done_marker_cannot_block_setup():
    # given
    store = fresh_store()
    users = Users(store.db)
    gate = SetupGate(store.db, users)
    code = gate.issue_code()
    store.db['meta'].insert_one({'_id': 'setup-done', 'at': 0})

    # when
    admin = SetupGate(store.db, users).complete(code, 'alice@example.com', 'Alice', PASSWORD)

    # then
    assert admin['role'] == 'admin'
    assert users.count() == 1


def test_a_failed_create_puts_the_code_back():
    # given
    store = fresh_store()

    class Broken(Users):
        def create(self, *a, **kw):
            raise AutoReconnect('down')

    code = SetupGate(store.db, Users(store.db)).issue_code()

    # when
    with pytest.raises(AutoReconnect):
        SetupGate(store.db, Broken(store.db)).complete(code, 'alice@example.com', 'Alice', PASSWORD)

    # then
    assert SetupGate(store.db, Users(store.db)).complete(code, 'alice@example.com', 'Alice', PASSWORD)['role'] == 'admin'


def test_a_lost_code_is_issued_again_on_the_next_setup_visit(caplog):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')

    class Broken(Users):
        def create(self, *a, **kw):
            raise AutoReconnect('down')

    with app_client(store) as c:
        old = issued_code(caplog)
        caplog.clear()
        gate = SetupGate(store.db, Broken(store.db))

        def failing(*a, **kw):
            raise AutoReconnect('down')

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(gate.meta, 'insert_one', failing)
            with pytest.raises(AutoReconnect):
                gate.complete(old, 'alice@example.com', 'Alice', PASSWORD)

        # when
        shown = c.get('/setup')
        fresh_code = issued_code(caplog)
        done = c.post('/setup', data=form(fresh_code))

    # then
    assert shown.status_code == 200
    assert fresh_code != old
    assert done.status_code == 303
    assert Users(store.db).count() == 1


def test_invalid_input_with_a_valid_code_creates_nothing_and_keeps_the_code():
    # given
    store = fresh_store()
    users = Users(store.db)
    gate = SetupGate(store.db, users)
    code = gate.issue_code()

    # when
    with pytest.raises(InvalidUser):
        gate.complete(code, 'alice@example.com', 'Alice', 'short')

    # then
    assert users.count() == 0 and gate.needed() is True
    assert gate.complete(code, 'alice@example.com', 'Alice', PASSWORD)['email'] == 'alice@example.com'


def test_a_wrong_code_is_refused_and_the_real_code_still_works():
    # given
    store = fresh_store()
    gate = SetupGate(store.db, Users(store.db))
    code = gate.issue_code()

    # when
    with pytest.raises(BadCode):
        gate.complete('AAAA-AAAA', 'alice@example.com', 'Alice', PASSWORD)

    # then
    assert Users(store.db).count() == 0
    assert gate.complete(code, 'alice@example.com', 'Alice', PASSWORD)['role'] == 'admin'


def test_env_admin_skips_setup(caplog):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')

    # when
    with app_client(store, admin_email='Root@Example.com', admin_password=PASSWORD) as c:
        page = c.get('/', headers=HTML)
        setup = c.get('/setup')

    # then
    assert page.status_code == 200
    assert setup.status_code == 303
    user = Users(store.db).get('root@example.com')
    assert user['role'] == 'admin' and user['source'] == 'local'
    assert Users(store.db).authenticate('root@example.com', PASSWORD) is not None
    assert 'admin created from env' in caplog.text
    assert store.db['meta'].find_one({'_id': 'setup-code'}) is None


@pytest.mark.parametrize('taken', [{'role': 'viewer'}, {'role': 'admin', 'disabled': True}])
def test_an_env_admin_email_held_by_another_account_warns_once_and_stops_hashing(caplog, monkeypatch, taken):
    # given
    store = fresh_store()
    Users(store.db).create('root@example.com', 'Root', 'viewer')
    Users(store.db).update('root@example.com', taken)
    caplog.set_level(logging.INFO, logger='skopax')
    hashed = []
    monkeypatch.setattr('skopax.users.derive', lambda *a, **k: hashed.append(1) or derive(*a, **k))

    # when
    with app_client(store, admin_email='Root@Example.com', admin_password=PASSWORD) as c:
        hashed.clear()
        pages = [c.get('/', headers=HTML).status_code for _ in range(3)]
        apis = [c.get('/api/state').status_code for _ in range(3)]

    # then
    assert pages == [303] * 3 and apis == [503] * 3, 'setup is still needed'
    assert hashed == [], 'a known conflict runs no scrypt per request'
    warned = [r.getMessage() for r in caplog.records if 'SKOPAX_ADMIN_EMAIL' in r.getMessage() and r.levelno == logging.WARNING]
    assert warned == ['SKOPAX_ADMIN_EMAIL root@example.com already belongs to an account that is not an active admin, so no admin was created from the environment. '
                      'Finish setup at /setup, or make that account an active admin.']


def test_a_lost_race_to_create_the_env_admin_is_not_a_conflict(caplog, monkeypatch):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    real = Users.create

    def other_request_won(self, email, name, role, password=None, source='local'):
        real(self, email, name, role, password, source)
        return real(self, email, name, role, password, source)

    monkeypatch.setattr(Users, 'create', other_request_won)

    # when
    with app_client(store, admin_email='Root@Example.com', admin_password=PASSWORD) as c:
        page = c.get('/', headers=HTML)

    # then
    assert page.status_code == 200
    assert 'already belongs to an account' not in caplog.text


def test_a_setup_post_answers_503_while_the_session_key_is_unavailable(caplog, monkeypatch):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    monkeypatch.setattr('skopax.app.session_secret', lambda *a: (_ for _ in ()).throw(AutoReconnect('down')))
    with app_client(store) as c:
        code = issued_code(caplog)

        # when
        page = c.post('/setup', data=form(code), headers=HTML)
        api = c.post('/setup', data=form(code))

    # then
    assert page.status_code == 503 and msg('setup.page_unavailable') in page.text and 'href="/login"' not in page.text
    assert api.status_code == 503 and api.json() == {'detail': msg('setup.unavailable')}
    assert Users(store.db).count() == 0


def test_an_email_an_sso_viewer_holds_is_a_form_error_and_the_code_still_works(caplog):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    Users(store.db).upsert_sso('alice@example.com', 'Alice', 'viewer')
    with app_client(store) as c:
        code = issued_code(caplog)

        # when
        taken = c.post('/setup', data=form(code))
        other = c.post('/setup', data=form(code, email='bob@example.com', name='Bob'))

    # then
    assert taken.status_code == 400 and msg('setup.error') in taken.text
    assert other.status_code == 303 and Users(store.db).get('bob@example.com')['role'] == 'admin'
    assert Users(store.db).get('alice@example.com')['role'] == 'viewer'


def test_setup_page_gone_after_setup():
    # given
    store = fresh_store()
    Users(store.db).create('alice@example.com', 'Alice', 'admin', PASSWORD)

    # when
    with app_client(store) as c:
        res = c.get('/setup')
        post = c.post('/setup', data=form('AAAA-AAAA'))

    # then
    assert (res.status_code, res.headers['location']) == (303, '/')
    assert post.status_code == 303
    assert Users(store.db).count() == 1


def test_health_stays_open_before_setup():
    # given
    store = fresh_store()

    # when
    with app_client(store) as c:
        health = c.get('/actuator/health')
        live = c.get('/actuator/health/liveness')
        ready = c.get('/actuator/health/readiness')
        manifest = c.get('/manifest.webmanifest')
        icon = c.get('/icons/missing.png')
        asset = c.get('/static/missing.js')

    # then
    assert health.status_code == 200 and live.status_code == 200 and manifest.status_code == 200
    assert (ready.status_code, ready.json()['status']) == (200, 'UP')
    assert icon.status_code == 404 and asset.status_code == 404


def test_cross_site_setup_post_is_refused(caplog):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    with app_client(store) as c:
        code = issued_code(caplog)

        # when
        res = c.post('/setup', data=form(code), headers={'Origin': 'https://evil.example'})

    # then
    assert res.status_code == 403
    assert Users(store.db).count() == 0


def test_code_is_issued_on_first_setup_visit_when_startup_could_not_reach_mongo(caplog, monkeypatch):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    broken = {'on': True}
    real = store.ensure_indexes

    def indexes():
        if broken['on']:
            raise AutoReconnect('down')
        return real()

    monkeypatch.setattr(store, 'ensure_indexes', indexes)

    # when
    with app_client(store) as c:
        assert not [r for r in caplog.records if CODE_LINE.search(r.getMessage())]
        broken['on'] = False
        first = c.get('/setup')
        second = c.get('/setup')
        code = issued_code(caplog)
        done = c.post('/setup', data=form(code))

    # then
    assert first.status_code == 200 and second.status_code == 200
    assert done.status_code == 303


def test_mongo_error_while_checking_setup_answers_503_and_is_not_cached(monkeypatch):
    # given
    store = fresh_store()
    real = Users.active_admins
    state = {'down': False}

    def count(self):
        if state['down']:
            raise AutoReconnect('down')
        return real(self)

    with app_client(store) as c:
        monkeypatch.setattr(Users, 'active_admins', count)
        state['down'] = True

        # when
        page = c.get('/', headers=HTML)
        api = c.get('/api/state')
        state['down'] = False
        back = c.get('/', headers=HTML)

    # then
    assert page.status_code == 503 and msg('setup.page_unavailable') in page.text
    assert 'href="/login"' not in page.text and 'href="/"' in page.text, 'nobody can log in before setup'
    assert api.status_code == 503 and api.json() == {'detail': msg('setup.unavailable')}
    assert (back.status_code, back.headers['location']) == (303, '/setup')


def test_settled_setup_is_not_checked_again(monkeypatch):
    # given
    store = fresh_store()
    Users(store.db).create('alice@example.com', 'Alice', 'admin', PASSWORD)
    calls = []
    real = Users.active_admins

    def count(self):
        calls.append(1)
        return real(self)

    # when
    with app_client(store) as c:
        monkeypatch.setattr(Users, 'active_admins', count)
        c.get('/api/state')
        seen = len(calls)
        c.get('/api/state')
        c.get('/api/state')

    # then
    assert len(calls) == seen


def test_an_sso_viewer_signing_in_before_setup_does_not_close_it():
    # given
    store = fresh_store()
    users = Users(store.db)
    gate = SetupGate(store.db, users)
    code = gate.issue_code()

    # when
    users.upsert_sso('viewer@example.com', 'Viewer', 'viewer')

    # then
    assert gate.needed() is True
    assert gate.complete(code, 'alice@example.com', 'Alice', PASSWORD)['role'] == 'admin'
    assert gate.needed() is False


def test_an_sso_claim_admin_closes_setup():
    # given
    store = fresh_store()
    users = Users(store.db)
    users.upsert_sso('boss@example.com', 'Boss', 'viewer')
    users.apply_sign_in_role('boss@example.com', 'admin', 'claim')

    # then
    assert SetupGate(store.db, users).needed() is False


def test_an_sso_viewer_before_setup_keeps_the_pages_gated():
    # given
    store = fresh_store()
    Users(store.db).upsert_sso('viewer@example.com', 'Viewer', 'viewer')

    # when
    with app_client(store) as c:
        page = c.get('/', headers=HTML)

    # then
    assert (page.status_code, page.headers['location']) == (303, '/setup')


def test_env_admin_is_created_when_mongo_was_down_at_boot(caplog, monkeypatch):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    broken = {'on': True}
    real = store.ensure_indexes

    def indexes():
        if broken['on']:
            raise AutoReconnect('down')
        return real()

    monkeypatch.setattr(store, 'ensure_indexes', indexes)

    # when
    with app_client(store, admin_email='root@example.com', admin_password=PASSWORD) as c:
        assert Users(store.db).count() == 0
        broken['on'] = False
        page = c.get('/', headers=HTML)

    # then
    assert page.status_code == 200
    assert Users(store.db).get('root@example.com')['role'] == 'admin'
    assert 'admin created from env' in caplog.text


def test_env_admin_name_and_strings_come_from_messages():
    # given
    store = fresh_store()

    # when
    with app_client(store, admin_email='root@example.com', admin_password=PASSWORD):
        pass

    # then
    assert Users(store.db).get('root@example.com')['name'] == msg('setup.admin_name')


def test_cross_site_and_unavailable_texts_come_from_messages(caplog, monkeypatch):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    with app_client(store) as c:
        code = issued_code(caplog)
        cross = c.post('/setup', data=form(code), headers={'Origin': 'https://evil.example'})
        monkeypatch.setattr(Users, 'active_admins', lambda self: (_ for _ in ()).throw(AutoReconnect('down')))
        html = c.post('/setup', data=form(code), headers=HTML)
        api = c.post('/setup', data=form(code))

    # then
    assert cross.json() == {'detail': msg('setup.cross_site')}
    assert html.status_code == 503 and msg('setup.page_unavailable') in html.text and 'login could not' not in html.text
    assert 'href="/login"' not in html.text
    assert api.status_code == 503 and api.json() == {'detail': msg('setup.unavailable')}


def test_pending_code_line_explains_the_env_admin(caplog):
    # given
    store = fresh_store()
    SetupGate(store.db, Users(store.db)).issue_code()
    caplog.set_level(logging.INFO, logger='skopax')

    # when
    with app_client(store):
        pass

    # then
    line = next(r.getMessage() for r in caplog.records if 'pending' in r.getMessage())
    assert 'SKOPAX_ADMIN_EMAIL' in line and 'SKOPAX_ADMIN_PASSWORD' in line and 'another instance' not in line


def test_a_body_over_the_cap_is_refused_whatever_the_content_length(caplog):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    with app_client(store) as c:
        code = issued_code(caplog)

        # when
        big = c.post('/setup', data=form(code, name='a' * 20000))
        chunked = c.post('/setup', content=iter([b'a' * 10000] * 3), headers={'Content-Type': 'application/x-www-form-urlencoded'})

    # then
    assert big.status_code == 413 and chunked.status_code == 413
    assert Users(store.db).count() == 0


class Streamed:
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.headers = {}

    async def stream(self):
        for chunk in self.chunks:
            yield chunk


def test_the_form_cap_is_exact_whatever_the_chunks():
    # when
    exact = asyncio.run(read_form(Streamed([b'name=' + b'a' * (FORM_BODY_MAX - 8), b'abc']), ('name',)))
    with pytest.raises(HTTPException) as over:
        asyncio.run(read_form(Streamed([b'a' * FORM_BODY_MAX, b'b']), ('name',)))

    # then
    assert len(exact['name']) == FORM_BODY_MAX - 5
    assert (over.value.status_code, over.value.detail) == (413, msg('form.too_large'))


def test_a_post_after_setup_never_reads_the_body(monkeypatch):
    # given
    store = fresh_store()
    Users(store.db).create('alice@example.com', 'Alice', 'admin', PASSWORD)

    async def refuse(self):
        raise AssertionError('body was read')

    def refuse_stream(self):
        raise AssertionError('body was read')

    with app_client(store) as c:
        monkeypatch.setattr('starlette.requests.Request.body', refuse)
        monkeypatch.setattr('starlette.requests.Request.stream', refuse_stream)

        # when
        res = c.post('/setup', data=form('AAAA-AAAA'))

    # then
    assert (res.status_code, res.headers['location']) == (303, '/')


def test_the_login_and_setup_pages_name_their_icon_and_it_loads_signed_out():
    # given
    icon = '<link rel="icon" type="image/svg+xml" href="/icons/mark.svg">'
    fresh, used = fresh_store(), fresh_store()
    Users(used.db).create('alice@example.com', 'Alice', 'admin', PASSWORD)

    # when
    with app_client(fresh, public_read=False) as c:
        setup = c.get('/setup', headers=HTML)
        setup_icon = c.get('/icons/mark.svg')
    with app_client(used, public_read=False) as c:
        login = c.get('/login', headers=HTML)
        login_icon = c.get('/icons/mark.svg')

    # then
    assert (setup.status_code, login.status_code) == (200, 200)
    assert icon in setup.text and icon in login.text
    assert (setup_icon.status_code, login_icon.status_code) == (200, 200) and login_icon.headers['content-type'].startswith('image/svg+xml')
