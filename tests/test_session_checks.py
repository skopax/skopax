from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_auth import sso_settings
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.sessions import Sessions, session_secret
from skopax.sso import SsoHttp
from skopax.store import Store
from skopax.users import Users

READER, OPERATOR, ADMIN = 'reader@example.com', 'operator@example.com', 'admin.user@example.com'
ROUTES = {'login': ('POST', '/api/notifications/read', {'all': True}), 'operator': ('POST', '/api/actions', {'type': 'ask', 'key': 'ask', 'target': 'ask', 'text': 'Question'}),
          'admin': ('GET', '/api/admin/settings', None)}


def test_login_operator_and_admin_routes_check_the_token_then_the_session_then_the_origin_then_the_role(store: Store):
    # given
    s = sso_settings()
    users = Users(store.db)
    for email, role in ((READER, 'viewer'), (OPERATOR, 'operator'), (ADMIN, 'admin')):
        users.create(email, email, role)
    store.add_notices([{'user': who, 'kind': 'assigned', 'key': f'card-{who}'} for who in ('reader', 'operator', 'admin.user')], {})
    sessions = Sessions(session_secret(store.db, s), 3600, 7200)
    cookie = lambda email, role: {'Cookie': f'{SESSION_COOKIE}={sessions.issue(email, role, "local")}'}
    callers = {'token': AUTH, 'wrong token': {'Authorization': 'Bearer nope'}, 'nobody': {}, 'reader': cookie(READER, 'viewer'), 'operator': cookie(OPERATOR, 'operator'),
               'admin': cookie(ADMIN, 'admin'), 'other site': cookie(ADMIN, 'admin') | {'Origin': 'https://evil.example'}}

    # when
    with TestClient(create_app(s, store, sso_http=SsoHttp(fetch=lambda _: {'keys': []})), base_url='https://testserver') as c:
        seen = {}
        for who, headers in callers.items():
            for need, (method, path, body) in ROUTES.items():
                res = c.request(method, path, json=body, headers=headers)
                said = res.json()
                seen[who, need] = (res.status_code, said.get('detail') or said.get('requestedBy') or said.get('by') or (f"read {said['read']}" if 'read' in said else 'ok'))

    # then
    assert seen == {
        ('token', 'login'): (200, 'read 0'), ('token', 'operator'): (201, 'token:bootstrap'), ('token', 'admin'): (200, 'ok'),
        ('wrong token', 'login'): (401, 'missing or wrong token'), ('wrong token', 'operator'): (401, 'missing or wrong token'), ('wrong token', 'admin'): (401, 'missing or wrong token'),
        ('nobody', 'login'): (401, 'log in first'), ('nobody', 'operator'): (401, 'log in first'), ('nobody', 'admin'): (401, 'log in first'),
        ('reader', 'login'): (200, 'read 1'), ('reader', 'operator'): (403, 'This needs the operator role.'), ('reader', 'admin'): (403, 'This needs the admin role.'),
        ('operator', 'login'): (200, 'read 1'), ('operator', 'operator'): (201, OPERATOR), ('operator', 'admin'): (403, 'This needs the admin role.'),
        ('admin', 'login'): (200, 'read 1'), ('admin', 'operator'): (201, ADMIN), ('admin', 'admin'): (200, 'ok'),
        ('other site', 'login'): (403, 'cross-site request'), ('other site', 'operator'): (403, 'cross-site request'), ('other site', 'admin'): (403, 'cross-site request'),
    }
