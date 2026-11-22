import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from skopax.contract import Capabilities, Context, Field, HttpError, InstanceInfo, Knowledge, Plugin, Setup, ReadError, TestResult, Transport, call
from skopax.kit import connection
from skopax.kit.env import env_set
from skopax.messages import msg
from skopax.plugins.jira.client import DENIED, JIRA_GATEWAY, NO_CLOUD, Jira, basic_auth, denied_text

ID = 'jira'
GATEWAY_HOST = 'api.atlassian.com'
TEST_TIMEOUT_S = 14
TENANT_TEST_S = 5
LEAST_S = 1
PROBE_JQL = 'updated >= -1d ORDER BY updated DESC'
NO_TIME = 'the Test ran out of time'


def site_url(config: Mapping[str, str]) -> str:
    return (config.get('url') or '').strip().rstrip('/')


def env_instances(env: Mapping[str, str]) -> list[dict]:
    given = env_set(env, 'Jira', ('JIRA_URL', 'JIRA_EMAIL', 'JIRA_API_TOKEN'), ('JIRA_CLOUD_ID',))
    if given is None:
        return []
    return [{'name': 'main', 'primary': 'true', 'url': given['JIRA_URL'].rstrip('/'), 'email': given['JIRA_EMAIL'], 'api-token': given['JIRA_API_TOKEN'], 'cloud-id': given['JIRA_CLOUD_ID']}]


def test(ctx: Context) -> TestResult:
    config = ctx.instance.config
    site = site_url(config)

    def ask() -> str:
        end = time.monotonic() + TEST_TIMEOUT_S

        def left() -> float:
            remaining = end - time.monotonic()
            if remaining < LEAST_S:
                raise TimeoutError(NO_TIME)
            return remaining

        def read(part: str, method: str, path: str, body: dict | None = None) -> None:
            try:
                call(ctx.http(), method, base + path, auth, body, timeout=left())
            except HttpError as e:
                if e.code in DENIED:
                    raise ReadError(e.code, denied_text(part, e.code)) from None
                raise

        cloud = (config.get('cloud-id') or '').strip()
        if not cloud:
            body = call(ctx.http(), 'GET', f'{site}/_edge/tenant_info', timeout=TENANT_TEST_S).body
            cloud = str(body.get('cloudId') or '') if isinstance(body, dict) else ''
            if not cloud:
                raise ReadError(502, NO_CLOUD)
        base = f'{JIRA_GATEWAY}/{quote(cloud, safe="")}'
        email = config.get('email') or ''
        auth = basic_auth(email, config.get('api-token') or '')
        try:
            call(ctx.http(), 'GET', f'{base}/rest/api/3/myself', auth, timeout=left())
        except HttpError as e:
            if e.code == 403:
                raise ReadError(403, denied_text('users', 403)) from None
            raise
        read('issues', 'POST', '/rest/api/3/search/jql', {'jql': PROBE_JQL, 'fields': ['summary'], 'maxResults': 1})
        read('users', 'GET', '/rest/api/3/user/search?' + urlencode({'query': email, 'maxResults': 1}))
        return msg('jira.test_ok', site=urlsplit(site).hostname or site)

    return connection.result(ask)


def make_client(info: InstanceInfo, transport: Transport) -> Jira:
    config = info.config
    return Jira(config.get('email') or '', config.get('api-token') or '', transport, cloud_id=(config.get('cloud-id') or '').strip(), site=site_url(config))


PLUGIN = Plugin(
    id=ID, name='Jira', version='0.1.0', category='tickets', description='One Jira Cloud site for Create task, ticket links on cards, release lookups and the Jira read tools.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.6', edition='community', capabilities=Capabilities(hosts=('@url', GATEWAY_HOST), secrets=('api-token',)),
    client=make_client, max_instances=1,
    fields=(Field('url', 'Site', 'url', required=True, ui=True, help='Address of the Jira Cloud site, such as https://example.atlassian.net. Calls go through the Atlassian API gateway.'),
            Field('email', 'Email', 'text', required=True, help='Email of the account the API token belongs to.'),
            Field('api-token', 'API token', 'secret', required=True, help='An Atlassian API token of that account.'),
            Field('cloud-id', 'Cloud id', 'text', help='Optional: the id of the site; when empty it is read from the site.')),
    env_instances=env_instances, test=test,
    knowledge=(Knowledge('jira', 'Jira', (Path(__file__).parent / 'jira.md').read_text(encoding='utf-8'), slack=True, inline=True),),
    setup=Setup(steps=(
        'Sign in as the account Skopax will use and open https://id.atlassian.com/manage-profile/security/api-tokens. Choose Create API token with scopes, name it and set an expiry (1 to 365 days).',
        'Pick Jira as the app and select the scopes read:jira-work and read:jira-user. The account also needs Browse projects and Browse users and groups in Jira.',
        "Create the token, copy it and paste it into API token. Enter that account's address in Email.",
        'In Site give your Jira Cloud address, such as https://example.atlassian.net. Leave Cloud id empty unless asked, then choose Test connection.',
        "A token made with the plain Create API token button also works, but it carries all the account's access.",
    ), docs='https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/', docs_label='Atlassian: manage API tokens'),
    company_knowledge='always',
    knowledge_also=True,
)
