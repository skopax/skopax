import logging
import re

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from skopax import ssobutton
from skopax.app import create_app
from skopax.logs import HideQuery
from skopax.messages import msg
from skopax.store import Store
from skopax.users import Users
from tests.conftest import AUTH, settings
from tests.test_add_user import DANA, PASSWORD, USERS, admin_of, raw, sso_client
from tests.test_signin_trust import BOSS, ISSUER, claims, signin_of

WEB = 'investigate.web-domains'


def record(path: str) -> logging.LogRecord:
    return logging.LogRecord('uvicorn.access', logging.INFO, 'x.py', 1, '%s - "%s %s HTTP/%s" %d', ('10.0.0.1:1', 'GET', path, '1.1', 200), None)


def test_the_people_search_text_never_reaches_the_access_log():
    # given
    searched = record('/api/admin/people?q=alice.smith')

    # when
    kept = HideQuery().filter(searched)

    # then
    assert kept is True
    assert searched.getMessage() == '10.0.0.1:1 - "GET /api/admin/people HTTP/1.1" 200'


def test_a_failing_idp_name_write_does_not_refuse_a_linked_sign_in(store: Store, monkeypatch, caplog):
    # given
    users = Users(store.db)
    users.create(BOSS, 'Boss', 'admin', PASSWORD, link_by='admin@example.com')
    signin = signin_of(store)
    monkeypatch.setattr(Users, 'idp_name', lambda self, email, name: (_ for _ in ()).throw(AutoReconnect('down')))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.signin'):
        stored = signin.page(claims(name='Boss Person'), ISSUER)

    # then
    assert stored['email'] == BOSS and stored['ssoLink']['state'] == 'bound'
    assert [r for r in caplog.records if r.name == 'skopax.signin' and 'AutoReconnect' in r.getMessage()]


def idp_role_account(store: Store, opened: bool) -> TestClient:
    admin = admin_of(sso_client(store))
    admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': True})
    store.db['users'].update_one({'_id': DANA}, {'$set': {'roleFrom': 'claim'}} | ({'$unset': {'opened': ''}} if opened else {}))
    return admin


@pytest.mark.parametrize('opened', [True, False], ids=['opened', 'unopened'])
def test_a_role_in_a_patch_on_an_idp_role_person_is_refused_alike_with_or_without_a_password(store: Store, opened: bool):
    # given
    admin = idp_role_account(store, opened)
    before = raw(store, DANA)

    # when
    role = admin.patch(f'{USERS}/{DANA}', json={'role': 'operator'})
    both = admin.patch(f'{USERS}/{DANA}', json={'role': 'operator', 'password': PASSWORD})
    after = raw(store, DANA)

    # then
    assert (role.status_code, role.json()) == (both.status_code, both.json()) == (409, {'detail': msg('access.role_from_idp')})
    assert after == before, 'a refused role also keeps the password part out'


def test_an_sso_link_on_an_sso_account_is_refused_at_the_field(store: Store):
    # given
    admin = admin_of(sso_client(store))
    admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': True})

    # when
    res = admin.patch(f'{USERS}/{DANA}', json={'ssoLink': True})

    # then
    assert (res.status_code, res.json()) == (422, {'detail': msg('access.sso_link_local_only'), 'field': 'ssoLink'})


def admin_lines(store: Store) -> list[str]:
    return [a['text'] for a in store.list_activity(None, 50) if a['kind'] == 'admin']


def test_a_save_at_the_value_already_in_use_adds_no_activity_line(store: Store):
    # given
    client = TestClient(create_app(settings(), store), headers=AUTH)
    shown = {i['key']: i for i in client.get('/api/admin/settings').json()['items']}['slack.remind-hours']['value']

    # when
    same = client.put('/api/admin/settings', json={'key': 'slack.remind-hours', 'value': shown})
    other = client.put('/api/admin/settings', json={'key': 'slack.remind-hours', 'value': str(int(shown) + 1)})

    # then
    assert same.status_code == other.status_code == 200
    assert len(admin_lines(store)) == 1 and admin_lines(store)[0].startswith(f'Reminder after (h) (slack.remind-hours): {shown} ')


def test_leaving_out_a_site_that_is_not_built_in_saves_with_a_warning(store: Store):
    # given
    client = TestClient(create_app(settings(), store), headers=AUTH)

    # when
    odd = client.put('/api/admin/settings', json={'key': WEB, 'value': 'example.com, -shop.example.org, -blog.google'})
    fine = client.put('/api/admin/settings', json={'key': WEB, 'value': 'example.com, -blog.google, -example.com'})

    # then
    assert odd.status_code == fine.status_code == 200
    assert odd.json()['item']['problem'] == msg('items.minus_unknown.web-host', field='Web sites', hosts='-shop.example.org')
    assert 'problem' not in fine.json()['item']


def test_every_sso_letter_mark_reads_differently():
    # given
    marks = sorted(ssobutton.ICON_DIR.glob('*.svg'))

    # when
    letters = {m.stem: re.findall(r'<text[^>]*>([^<]+)</text>', m.read_text()) for m in marks}
    shown = [t[0] for t in letters.values() if t]

    # then
    assert len(shown) >= 8
    assert len(shown) == len(set(shown)), letters


@pytest.mark.parametrize('known', [False, True], ids=['first sign-in', 'known person'])
def test_a_failing_idp_name_write_does_not_refuse_an_sso_sign_in(store: Store, monkeypatch, caplog, known: bool):
    # given
    if known:
        Users(store.db).upsert_sso(BOSS, 'Boss', 'viewer')
    signin = signin_of(store)
    monkeypatch.setattr(Users, 'idp_name', lambda self, email, name: (_ for _ in ()).throw(AutoReconnect('down')))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.users'):
        stored = signin.page(claims(name='Boss Person'), ISSUER)

    # then
    assert stored['email'] == BOSS and stored['source'] == 'sso'
    assert [r for r in caplog.records if r.name == 'skopax.users' and 'AutoReconnect' in r.getMessage()]
