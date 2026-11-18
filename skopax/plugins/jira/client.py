import base64
import logging
from urllib.parse import quote, urlencode

from skopax import remote
from skopax.contract import Denied, Missing, Refused, SourceDown
from skopax.messages import msg
from skopax.remote import HttpError, Transport, Unreachable

log = logging.getLogger('skopax.plugins.jira')

JIRA_GATEWAY = 'https://api.atlassian.com/ex/jira'
JIRA_BATCH = 100
TENANT_TIMEOUT_S = 15
CALL_TIMEOUT_S = 30
NO_CLOUD = 'Jira cloud id: tenant_info answered without a cloudId'
CREATE_MISSING = 'Jira answered HTTP 404 to the create: check the site and the cloud id of the Jira instance'
DENIED = (401, 403)
PERMISSIONS = {'issues': 'the Browse projects permission (scope read:jira-work)', 'users': 'the Browse users and groups permission (scope read:jira-user)'}


def denied_text(part: str, status: int) -> str:
    return msg('jira.denied', part=part, status=status, permission=PERMISSIONS[part])


def basic_auth(email: str, token: str) -> dict[str, str]:
    return {'Authorization': 'Basic ' + base64.b64encode(f'{email}:{token}'.encode()).decode()}


class Jira:
    def __init__(self, email: str, token: str, transport: Transport = remote.send, cloud_id: str = '', site: str = ''):
        self.email = email
        self.site = site.rstrip('/')
        self.token = token
        self.transport = transport
        self.cloud_id = cloud_id

    @property
    def base(self) -> str:
        if not self.enabled:
            raise SourceDown(msg('integrations.off', name='Jira'))
        return f"{JIRA_GATEWAY}/{quote(self._cloud(), safe='')}"

    def _cloud(self) -> str:
        if not self.enabled:
            raise SourceDown(msg('integrations.off', name='Jira'))
        if not self.cloud_id:
            try:
                body = remote.call(self.transport, 'GET', f'{self.site}/_edge/tenant_info', None, None, timeout=TENANT_TIMEOUT_S).body
            except HttpError as e:
                raise SourceDown(f'Jira cloud id: HTTP {e.code} from tenant_info') from None
            except Unreachable:
                raise SourceDown(msg('enrich.unreachable', source='Jira')) from None
            cloud = body.get('cloudId') if isinstance(body, dict) else None
            if not cloud:
                raise SourceDown(NO_CLOUD)
            self.cloud_id = str(cloud)
            log.info('jira cloud id resolved from tenant_info: %s', self.cloud_id)
        return self.cloud_id

    @property
    def enabled(self) -> bool:
        return bool(self.email and self.token and self.site)

    def _auth(self) -> dict[str, str]:
        return basic_auth(self.email, self.token)

    def _call(self, method: str, path: str, body: dict | None = None, part: str = 'issues') -> remote.Reply:
        base = self.base
        try:
            return remote.call(self.transport, method, f'{base}{path}', self._auth(), body, timeout=CALL_TIMEOUT_S)
        except HttpError as e:
            if e.code == 404:
                raise Missing(path) from None
            if e.code in DENIED:
                raise Denied(denied_text(part, e.code), part, PERMISSIONS[part]) from None
            raise SourceDown(f'Jira HTTP {e.code}') from None
        except Unreachable:
            raise SourceDown(msg('enrich.unreachable', source='Jira')) from None

    def search(self, jql: str, fields: list[str], limit: int = 20) -> list[dict]:
        return (self._call('POST', '/rest/api/3/search/jql', {'jql': jql, 'fields': fields, 'maxResults': limit}).body or {}).get('issues') or []

    def issue(self, key: str, fields: list[str]) -> dict | None:
        try:
            return self._call('GET', f'/rest/api/3/issue/{quote(key, safe="")}?{urlencode({"fields": ",".join(fields)})}').body
        except Missing:
            return None

    def comments(self, key: str, limit: int) -> dict:
        return self._call('GET', f'/rest/api/3/issue/{quote(key, safe="")}/comment?{urlencode({"orderBy": "-created", "maxResults": limit})}').body or {}

    def create_issue(self, fields: dict) -> str:
        base = self.base
        try:
            reply = remote.call(self.transport, 'POST', f'{base}/rest/api/3/issue', self._auth(), {'fields': fields}, timeout=CALL_TIMEOUT_S)
        except HttpError as e:
            if e.code == 404:
                raise Refused(CREATE_MISSING) from None
            if e.code in DENIED:
                raise Refused(msg('jira.create_denied_invalid' if e.code == 401 else 'jira.create_denied', status=e.code)) from None
            raise Refused(f'Jira answered HTTP {e.code} to the create; check the project, parent, sprint and the field values the server sends') from None
        except Unreachable:
            raise SourceDown(msg('enrich.unreachable', source='Jira')) from None
        key = (reply.body or {}).get('key')
        if not key:
            raise Refused('Jira created nothing: no key in the answer')
        return str(key)

    def find_user(self, query: str) -> str | None:
        if not query:
            return None
        try:
            if query.strip().lower() == self.email.strip().lower():
                me = self._call('GET', '/rest/api/3/myself', part='users').body
                return me.get('accountId') if isinstance(me, dict) else None
            users = self._call('GET', f'/rest/api/3/user/search?{urlencode({"query": query, "maxResults": 1})}', part='users').body or []
        except Denied:
            raise
        except (Missing, SourceDown):
            return None
        return users[0].get('accountId') if isinstance(users, list) and users and isinstance(users[0], dict) else None

    def people(self, query: str, limit: int = 8) -> list[dict]:
        users = self._call('GET', f'/rest/api/3/user/search?{urlencode({"query": query, "maxResults": limit})}', part='users').body
        return users if isinstance(users, list) else []

    def active_sprint(self, project: str, field: str) -> int | None:
        try:
            issues = self.search(f'project = {project} AND sprint in openSprints()', [field], 1)
        except Denied:
            raise
        except (Missing, SourceDown):
            return None
        sprints = ((issues[0].get('fields') or {}).get(field) or []) if issues else []
        for sprint in sprints:
            if isinstance(sprint, dict) and sprint.get('state') == 'active' and sprint.get('id'):
                return int(sprint['id'])
        return None

    def versions(self, keys: list[str]) -> dict[str, list[str]]:
        out = {}
        base = self.base
        for i in range(0, len(keys), JIRA_BATCH):
            body = {'issueIdsOrKeys': keys[i:i + JIRA_BATCH], 'fields': ['fixVersions']}
            try:
                reply = remote.call(self.transport, 'POST', f'{base}/rest/api/3/issue/bulkfetch', self._auth(), body, timeout=CALL_TIMEOUT_S)
            except HttpError as e:
                if e.code in DENIED:
                    raise Denied(denied_text('issues', e.code), 'issues', PERMISSIONS['issues']) from None
                raise SourceDown(f'Jira HTTP {e.code}') from None
            except Unreachable:
                raise SourceDown(msg('enrich.unreachable', source='Jira')) from None
            for issue in (reply.body or {}).get('issues') or []:
                out[issue['key']] = [v['name'] for v in (issue.get('fields') or {}).get('fixVersions') or [] if v.get('name')]
        return out
