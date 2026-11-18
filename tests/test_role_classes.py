import dataclasses

from tests.conftest import AUTH
from tests.test_auth import app_client, sso_settings
from tests.test_read_gate import concrete, walk
from skopax.app import is_open
from skopax.auth import SESSION_COOKIE
from skopax.messages import msg
from skopax.sessions import Sessions, session_secret
from skopax.store import Store
from skopax.users import Users

VIEWER = 'carol@example.com'
OPERATOR = 'bob@example.com'
READ_METHODS = ('GET', 'HEAD')
PATTERNS = {
    'open': ('ok', 'ok', 'ok', 'ok'),
    'read': ('ok', 'ok', 'ok', 'ok'),
    'login': ('401', 'ok', 'ok', 'ok'),
    'session': ('401', 'ok', 'ok', '401'),
    'operator': ('401', '403', 'ok', 'ok'),
    'admin': ('401', '403', '403', 'ok'),
    'machine': ('401', '401', '401', 'ok'),
    'idp-token': ('401', '401', '401', '401'),
}
WRITES = {
    ('DELETE', '/api/admin/anthropic-key'): 'admin',
    ('DELETE', '/api/admin/mcp-tools/instances/{name}/oauth'): 'admin',
    ('DELETE', '/api/admin/plugins/{plugin_id}/instances/{name}'): 'admin',
    ('DELETE', '/api/admin/settings'): 'admin',
    ('DELETE', '/api/admin/small-talk'): 'admin',
    ('DELETE', '/api/admin/tokens/{token_id}'): 'admin',
    ('DELETE', '/api/deploy-reports/{report_id}'): 'machine',
    ('DELETE', '/api/deploys/{deploy_id}'): 'machine',
    ('DELETE', '/mcp'): 'idp-token',
    ('PATCH', '/api/actions/{action_id}'): 'operator',
    ('PATCH', '/api/admin/plugins/{plugin_id}/instances/{name}'): 'admin',
    ('PATCH', '/api/admin/users/{email}'): 'admin',
    ('POST', '/api/actions'): 'operator',
    ('POST', '/api/admin/anthropic-key/test'): 'admin',
    ('POST', '/api/admin/mcp-tools/instances/{name}/confirm'): 'admin',
    ('POST', '/api/admin/mcp-tools/instances/{name}/oauth/start'): 'admin',
    ('POST', '/api/admin/mcp-tools/instances/{name}/tools/refresh'): 'admin',
    ('POST', '/api/admin/plugins/{plugin_id}/instances'): 'admin',
    ('POST', '/api/admin/plugins/{plugin_id}/test'): 'admin',
    ('POST', '/api/admin/sso/test'): 'admin',
    ('POST', '/api/admin/tokens'): 'admin',
    ('POST', '/api/admin/users'): 'admin',
    ('POST', '/api/assessments'): 'machine',
    ('POST', '/api/chats'): 'operator',
    ('POST', '/api/chats/{chat_id}/continue'): 'login',
    ('POST', '/api/checks/{plugin_id}/{check_id}/run'): 'operator',
    ('POST', '/api/deploy-reports'): 'machine',
    ('POST', '/api/deploy-reports/{report_id}/slack-hold'): 'operator',
    ('POST', '/api/deploy-reports/{report_id}/slack-posts'): 'operator',
    ('POST', '/api/deploys'): 'machine',
    ('POST', '/api/investigations/{run_id}/proposal'): 'operator',
    ('POST', '/api/me/password'): 'session',
    ('POST', '/api/notes'): 'machine',
    ('POST', '/api/notifications/read'): 'login',
    ('POST', '/api/probe'): 'machine',
    ('POST', '/api/routes/send'): 'operator',
    ('POST', '/api/self-review/run'): 'machine',
    ('POST', '/api/slack-holds'): 'operator',
    ('POST', '/api/slack-posts'): 'operator',
    ('POST', '/api/slack-updates'): 'operator',
    ('POST', '/api/tags'): 'operator',
    ('POST', '/login'): 'open',
    ('POST', '/logout'): 'open',
    ('POST', '/mcp'): 'idp-token',
    ('POST', '/setup'): 'open',
    ('PUT', '/api/admin/anthropic-key'): 'admin',
    ('PUT', '/api/admin/features/{feature_id}'): 'admin',
    ('PUT', '/api/admin/mcp-tools/instances/{name}/scope'): 'admin',
    ('PUT', '/api/admin/mcp-tools/instances/{name}/tools'): 'admin',
    ('PUT', '/api/admin/plugins/{plugin_id}'): 'admin',
    ('PUT', '/api/admin/settings'): 'admin',
    ('PUT', '/api/admin/sso'): 'admin',
    ('PUT', '/api/admin/small-talk'): 'admin',
    ('PUT', '/api/assignee'): 'operator',
    ('PUT', '/api/business'): 'operator',
    ('PUT', '/api/chats/{chat_id}/share'): 'login',
    ('PUT', '/api/deploy-reports/{report_id}/brief'): 'machine',
    ('PUT', '/api/deploy-reports/{report_id}/slack-draft'): 'operator',
    ('PUT', '/api/gate-examples'): 'machine',
    ('PUT', '/api/investigate'): 'admin',
    ('PUT', '/api/postdeploy-ignore'): 'machine',
    ('PUT', '/api/self-review'): 'machine',
    ('PUT', '/api/slack-drafts'): 'operator',
    ('PUT', '/api/small-talk'): 'machine',
    ('PUT', '/api/state'): 'machine',
}
READS = {
    '/': 'read',
    '/.well-known/oauth-protected-resource': 'open',
    '/.well-known/oauth-protected-resource/mcp': 'open',
    '/actuator/health': 'open',
    '/actuator/health/liveness': 'open',
    '/actuator/health/readiness': 'open',
    '/api/actions': 'read',
    '/api/activity': 'read',
    '/api/admin/anthropic-key': 'admin',
    '/api/admin/features': 'admin',
    '/api/admin/history': 'admin',
    '/api/admin/mcp-tools/instances/{name}': 'admin',
    '/api/admin/mcp-tools/instances/{name}/folders': 'admin',
    '/api/admin/mcp-tools/oauth/callback': 'admin',
    '/api/admin/mcp-tools/presets': 'admin',
    '/api/admin/notices': 'admin',
    '/api/admin/plugins': 'admin',
    '/api/admin/settings': 'admin',
    '/api/admin/sso': 'admin',
    '/api/admin/small-talk': 'admin',
    '/api/admin/tokens': 'admin',
    '/api/admin/users': 'admin',
    '/api/card-runs': 'read',
    '/api/cards/{num}': 'read',
    '/api/chats': 'login',
    '/api/chats/{chat_id}': 'login',
    '/api/costs': 'login',
    '/api/deploy-reports': 'read',
    '/api/deploys': 'read',
    '/api/gate-examples': 'read',
    '/api/history/cards': 'read',
    '/api/investigations': 'read',
    '/api/investigations/{run_id}': 'read',
    '/api/investigator/status': 'read',
    '/api/investigator/usage': 'read',
    '/api/jira/search': 'operator',
    '/api/mcp/info': 'read',
    '/api/me': 'read',
    '/api/notifications': 'login',
    '/api/notifications/count': 'login',
    '/api/operators/check': 'operator',
    '/api/people': 'login',
    '/api/postdeploy-ignore': 'read',
    '/api/requests': 'read',
    '/api/rev': 'read',
    '/api/screenshots/{shot_id}': 'read',
    '/api/self-review': 'read',
    '/api/server': 'read',
    '/api/slack-intake': 'operator',
    '/api/small-talk': 'read',
    '/api/state': 'read',
    '/api/tool-usage': 'read',
    '/authorized': 'open',
    '/c/{num}': 'read',
    '/chat-continue/{chat_id}': 'session',
    '/docs': 'read',
    '/docs/oauth2-redirect': 'read',
    '/icons/{name}': 'open',
    '/login': 'open',
    '/login/sso': 'open',
    '/logout': 'open',
    '/manifest.webmanifest': 'open',
    '/mcp': 'idp-token',
    '/openapi.json': 'read',
    '/plugins/{plugin_id}/{file}': 'open',
    '/redoc': 'read',
    '/setup': 'open',
    '/share/{chat_id}': 'read',
    '/static/{name}': 'open',
}
BODIES = {('POST', '/api/me/password'): {'current': 'correct horse battery', 'new': 'another horse battery'},
          ('PUT', '/api/admin/mcp-tools/instances/{name}/tools'): {'on': [], 'understood': False},
          ('POST', '/api/admin/mcp-tools/instances/{name}/confirm'): {'understood': True, 'version': 1},
          ('PUT', '/api/admin/mcp-tools/instances/{name}/scope'): {'id': 'f1'},
          ('PUT', '/api/admin/sso'): {},
          ('POST', '/api/admin/sso/test'): {}}
REFUSALS = {msg('access.login_first'), msg('access.token_invalid'), msg('access.need_operator'), msg('access.need_admin'), msg('access.token_role'), msg('password.needs_session'),
            msg('mcp.login_first'), msg('mcp.token_invalid')}


def outcome(res) -> str:
    if res.status_code == 303 and res.headers['location'].startswith('/login'):
        return '401'
    if res.status_code in (401, 403) and res.headers.get('content-type', '').startswith('application/json') and (res.request.method == 'HEAD' and not res.content or res.json().get('detail') in REFUSALS):
        return str(res.status_code)
    return 'ok'


def probe(client, cookies: dict[str, str], method: str, path: str) -> tuple[str, ...]:
    seen = []
    for who in ('anonymous', 'viewer', 'operator', 'token'):
        client.cookies.clear()
        if who in cookies:
            client.cookies.set(SESSION_COOKIE, cookies[who])
        body = BODIES.get((method, path), {})
        res = client.request(method, concrete(path), json=None if method in READ_METHODS else body, headers=AUTH if who == 'token' else {}, follow_redirects=False)
        seen.append(outcome(res))
    client.cookies.clear()
    return tuple(seen)


def people(store: Store) -> dict[str, str]:
    users = Users(store.db)
    users.create(VIEWER, 'Carol', 'viewer')
    users.create(OPERATOR, 'Bob', 'operator')
    sealer = Sessions(session_secret(store.db, sso_settings()), 3600, 7200)
    return {'viewer': sealer.issue(VIEWER, 'viewer', 'local'), 'operator': sealer.issue(OPERATOR, 'operator', 'local')}


def test_every_write_route_has_a_listed_role_class_and_keeps_it(store: Store):
    # given
    cookies = people(store)
    with app_client(store, s=dataclasses.replace(sso_settings(), public_read=True)) as client:
        routes = sorted({(m, p) for m, p in walk(client.app.routes) if m not in ('WS', 'MOUNT') and m not in READ_METHODS})

        # when
        seen = {(m, p): probe(client, cookies, m, p) for m, p in routes}

    # then
    assert routes == sorted(WRITES), 'a write route was added or removed: give it a role class in WRITES on purpose'
    wrong = {route: (WRITES[route], got) for route, got in seen.items() if got != PATTERNS[WRITES[route]]}
    assert wrong == {}


def test_every_read_route_is_read_for_all_or_guarded_as_listed(store: Store):
    # given
    cookies = people(store)
    with app_client(store, s=dataclasses.replace(sso_settings(), public_read=True)) as client:
        routes = sorted({(m, p) for m, p in walk(client.app.routes) if m in READ_METHODS})

        # when
        seen = {(m, p): probe(client, cookies, m, p) for m, p in routes}

    # then
    assert sorted({p for _, p in routes}) == sorted(READS), 'a read route was added or removed: list it in READS on purpose'
    wrong = {route: (READS[route[1]], got) for route, got in seen.items() if got != PATTERNS[READS[route[1]]]}
    assert wrong == {}
    assert sorted(p for p, kind in READS.items() if kind in ('open', 'idp-token')) == sorted(p for p in READS if is_open(concrete(p)))
