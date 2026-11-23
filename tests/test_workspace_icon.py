import dataclasses

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from tests.conftest import AUTH, settings
from skopax import ssoicon, workspaceicon
from skopax.app import create_app
from skopax.config import WORKSPACE_ICONS, load_settings
from skopax.messages import msg
from skopax.store import Store
from skopax.tunables import instance_menu, registry

UPLOAD = '/api/admin/workspace/icon'
SSO_UPLOAD = '/api/admin/sso/icon'
SERVED = '/api/workspace-icon'
KEY = 'core.workspace-icon'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40
GIF = b'GIF89a' + b'\x00' * 40
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><rect width="20" height="20" rx="4" fill="#0f766e"/></svg>'
ICON_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def upload(c, data: bytes, kind: str = 'image/png', path: str = UPLOAD, headers: dict | None = None):
    return c.put(path, content=data, headers={**(AUTH if headers is None else headers), 'Content-Type': kind})


def choose(c, value: str):
    return c.put('/api/admin/settings', json={'key': KEY, 'value': value}, headers=AUTH)


def current(c) -> dict:
    return c.get('/api/me').json()['instances']['current']


def history(c) -> list[str]:
    return [h.get('text') for h in c.get('/api/admin/history', headers=AUTH).json() if h.get('access')]


def test_the_icon_choice_is_a_workspaces_choice_that_defaults_to_the_letter():
    # given
    known = {t.key: t for t in registry()}[KEY]

    # when
    plain = load_settings(env={})
    from_env = load_settings(env={'SKOPAX_WORKSPACE_ICON': 'upload'})
    wrong = load_settings(env={'SKOPAX_WORKSPACE_ICON': 'logo'})

    # then
    assert (known.group, known.kind, known.field, tuple(known.choices)) == ('Workspaces', 'choice', 'workspace_icon', ('letter', 'company', 'upload'))
    assert WORKSPACE_ICONS == ('letter', 'company', 'upload')
    assert (plain.workspace_icon, from_env.workspace_icon, wrong.workspace_icon) == ('letter', 'upload', 'letter')


def test_the_menu_carries_an_icon_only_when_one_is_given():
    # given
    s = dataclasses.replace(settings(), workspace_label='production')

    # when
    bare = instance_menu(s, [])
    shown = instance_menu(s, [], '/api/workspace-icon?v=abc')

    # then
    assert bare['current'] == {'name': 'production', 'letter': 'P'}
    assert shown['current'] == {'name': 'production', 'letter': 'P', 'icon': '/api/workspace-icon?v=abc'}


def test_an_uploaded_icon_is_served_to_readers_shown_in_me_audited_and_removed(store: Store):
    with TestClient(create_app(settings(), store)) as c:
        # given
        before = c.get(SERVED)
        letter = current(c)

        # when
        saved = upload(c, SVG, 'image/svg+xml')
        version = saved.json()['icon']['upload']['version']
        still_letter = current(c)
        picked = choose(c, 'upload')
        shown = current(c)
        served = c.get(SERVED, params={'v': version})
        latest = c.get(SERVED)
        stale = c.get(SERVED, params={'v': 'old'})
        admin = c.get('/api/admin/settings', headers=AUTH).json()['instance']['icon']
        lines = history(c)
        removed = c.delete(UPLOAD, headers=AUTH)
        gone = current(c)
        after = c.get(SERVED)
        removed_again = c.delete(UPLOAD, headers=AUTH)
        changes = c.get('/api/admin/history', headers=AUTH).json()

    # then
    assert before.status_code == 404
    assert letter == still_letter == {'name': 'skopax.example.com', 'letter': 'S'}
    assert saved.status_code == 200 and saved.json()['icon']['upload'] == {'set': True, 'type': 'image/svg+xml', 'size': len(SVG), 'version': ssoicon.version_of(SVG)}
    assert picked.status_code == 200
    assert shown == {'name': 'skopax.example.com', 'letter': 'S', 'icon': f'{SERVED}?v={version}'}
    assert served.status_code == 200 and served.content == SVG
    assert served.headers['content-type'] == 'image/svg+xml'
    assert served.headers['content-security-policy'] == ICON_CSP
    assert served.headers['x-content-type-options'] == 'nosniff'
    assert served.headers['cache-control'].startswith('private') and 'immutable' in served.headers['cache-control'] and 'public' not in served.headers['cache-control']
    assert latest.status_code == 200 and latest.headers['cache-control'] == 'no-cache'
    assert stale.status_code == 404
    assert admin == {'mode': 'upload', 'company': {'set': False, 'type': None, 'size': 0, 'version': None},
                     'upload': {'set': True, 'type': 'image/svg+xml', 'size': len(SVG), 'version': version}, 'max': ssoicon.ICON_MAX}
    assert lines[0] == msg('workspace.icon_saved', type='image/svg+xml', size=len(SVG))
    assert removed.status_code == 200 and removed.json()['icon']['upload']['set'] is False
    assert gone == {'name': 'skopax.example.com', 'letter': 'S'}, 'with nothing uploaded the tile falls back to the letter'
    assert after.status_code == 404
    assert removed_again.status_code == 200
    assert [h['text'] for h in changes if h.get('access')].count(msg('workspace.icon_removed')) == 1
    assert any(h.get('key') == KEY and h.get('new') == 'upload' for h in changes), 'the choice is in Changes'
    assert store.db[workspaceicon.COLLECTION].count_documents({}) == 0


def test_the_company_icon_reuses_the_single_sign_on_upload_and_falls_back_to_the_letter(store: Store):
    with TestClient(create_app(settings(), store)) as c:
        # given
        choose(c, 'company')
        without = current(c)

        # when
        upload(c, PNG, path=SSO_UPLOAD)
        version = ssoicon.version_of(PNG)
        shown = current(c)
        served = c.get(SERVED, params={'v': version})
        admin = c.get('/api/admin/settings', headers=AUTH).json()['instance']['icon']
        choose(c, 'letter')
        letter = current(c)
        none_served = c.get(SERVED)

    # then
    assert without == {'name': 'skopax.example.com', 'letter': 'S'}
    assert shown['icon'] == f'{SERVED}?v={version}'
    assert served.status_code == 200 and served.content == PNG and served.headers['content-type'] == 'image/png'
    assert admin['mode'] == 'company' and admin['company']['set'] is True and admin['upload']['set'] is False
    assert letter == {'name': 'skopax.example.com', 'letter': 'S'}
    assert none_served.status_code == 404
    assert store.db[workspaceicon.COLLECTION].count_documents({}) == 0, 'the company icon is not copied'


def test_uploads_are_refused_when_too_big_wrong_or_unsafe_and_only_admins_may_upload(store: Store):
    with TestClient(create_app(settings(), store)) as c:
        # when
        big = upload(c, PNG + b'\x00' * ssoicon.ICON_MAX)
        gif = upload(c, GIF)
        script = upload(c, b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', 'image/svg+xml')
        anonymous = upload(c, PNG, headers={})
        anonymous_delete = c.delete(UPLOAD)

    # then
    assert (big.status_code, big.json()) == (413, {'detail': msg('sso.icon_too_big', max=ssoicon.ICON_MAX // 1024)})
    assert (gif.status_code, gif.json()) == (422, {'detail': msg('sso.icon_type')})
    assert (script.status_code, script.json()) == (422, {'detail': msg('sso.icon_svg')})
    assert anonymous.status_code in (401, 403) and anonymous_delete.status_code in (401, 403)
    assert store.db[workspaceicon.COLLECTION].count_documents({}) == 0


def test_a_database_fault_leaves_the_letter_and_the_page_still_loads(store: Store, monkeypatch):
    with TestClient(create_app(settings(), store)) as c:
        # given
        upload(c, PNG)
        choose(c, 'upload')

        def broken(self):
            raise PyMongoError('down')

        monkeypatch.setattr(ssoicon.SsoIcon, 'status', broken)

        # when
        me = c.get('/api/me')
        served = c.get(SERVED)

    # then
    assert me.status_code == 200 and 'icon' not in me.json()['instances']['current']
    assert served.status_code == 503


def test_the_icon_is_hidden_from_anonymous_readers_when_reading_needs_a_login(store: Store):
    with TestClient(create_app(settings(public_read=False), store)) as c:
        # given
        upload(c, PNG)
        choose(c, 'upload')

        # when
        anonymous = c.get(SERVED)
        signed_in = c.get(SERVED, headers=AUTH)

    # then
    assert anonymous.status_code == 401
    assert signed_in.status_code == 200 and signed_in.content == PNG


@pytest.mark.parametrize('mode', ['letter', 'company', 'upload'])
def test_every_choice_saves_through_the_settings_api(store: Store, mode: str):
    with TestClient(create_app(settings(), store)) as c:
        # when
        saved = choose(c, mode)

    # then
    assert saved.status_code == 200 and saved.json()['item']['value'] == mode
