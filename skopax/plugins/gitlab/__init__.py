from collections.abc import Mapping
from pathlib import Path
from time import monotonic
from urllib.parse import quote, urlencode

from skopax.contract import Capabilities, Context, Field, HttpError, InstanceInfo, Knowledge, Plugin, Setup, ReadError, TestResult, Transport, call
from skopax.kit import connection
from skopax.kit.env import env_set
from skopax.messages import msg
from skopax.plugins.gitlab.client import GITLAB_URL, GitLab, denied_text, hidden_text
from skopax.plugins.gitlab.tools import TOOLS

ID = 'gitlab'
TEST_BUDGET_S = 12
PROBE = {'include_subgroups': 'true', 'archived': 'false', 'simple': 'true', 'per_page': 1}


def base_of(config: Mapping[str, str]) -> str:
    return (config.get('url') or GITLAB_URL).rstrip('/')


def env_instances(env: Mapping[str, str]) -> list[dict]:
    given = env_set(env, 'GitLab', ('GITLAB_TOKEN', 'GITLAB_GROUP'), ('GITLAB_URL',))
    if given is None:
        return []
    return [{'name': 'main', 'primary': 'true', 'url': given['GITLAB_URL'] or GITLAB_URL, 'token': given['GITLAB_TOKEN'], 'group': given['GITLAB_GROUP']}]


def left(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


def test(ctx: Context) -> TestResult:
    config = ctx.instance.config

    def ask() -> str:
        deadline = monotonic() + TEST_BUDGET_S
        api, headers, group = base_of(config) + '/api/v4', {'PRIVATE-TOKEN': config.get('token') or ''}, config.get('group') or ''
        body = call(ctx.http(), 'GET', api + '/version', headers, timeout=left(deadline)).body
        version = body.get('version') if isinstance(body, dict) else None
        try:
            call(ctx.http(), 'GET', f"{api}/groups/{quote(group.strip('/'), safe='')}/projects?{urlencode(PROBE)}", headers, timeout=left(deadline))
        except HttpError as e:
            if e.code == 403:
                raise ReadError(403, denied_text('projects', 403)) from None
            if e.code == 404:
                raise ReadError(404, hidden_text(group)) from None
            raise
        return msg('gitlab.test.version', version=version) if isinstance(version, str) and version else msg('gitlab.test.unknown')

    return connection.result(ask)


def make_client(info: InstanceInfo, transport: Transport) -> GitLab:
    return GitLab(info.config.get('token') or '', transport, group=info.config.get('group') or '', base=base_of(info.config))


PLUGIN = Plugin(
    id=ID, name='GitLab', version='0.1.0', category='code', description='Commits, code, merge requests and pipelines of one GitLab group, for deploy authors, tickets and code links.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.4', edition='community', capabilities=Capabilities(hosts=('@url',), secrets=('token',)),
    client=make_client, max_instances=1,
    fields=(Field('url', 'URL', 'url', required=True, default=GITLAB_URL, ui=True, help='Address of GitLab; its API is read under /api/v4.'),
            Field('token', 'Token', 'secret', required=True, help='An access token with the read_api scope.'),
            Field('group', 'Group', 'text', required=True, help='Path of the group whose projects are read, such as acme. Code links must point under it.'),
            Field('ca', 'CA certificate', 'pem', group='advanced', help='PEM certificate to trust instead of the system roots.'),
            Field('ca-pinned', 'Trust this certificate as it is', 'bool', default='false', show_if=('ca', '*'), group='advanced', help="For a server whose own certificate fails the usual checks: paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host. A CA certificate does not work with the switch on.")),
    env_instances=env_instances, test=test, tools=TOOLS,
    knowledge=(Knowledge('gitlab', 'GitLab', (Path(__file__).parent / 'gitlab.md').read_text(encoding='utf-8'), slack=True, inline=True),),
    setup=Setup(steps=(
        'In GitLab choose your avatar > Edit profile > Access > Personal access tokens, then Generate token > Legacy token. Or, as a group Owner, open the group > Settings > Access tokens > Add new token.',
        'Name it, set an expiration date, and select only the read_api scope. For a group token choose a role that can read the projects, such as Reporter.',
        'Create the token, copy it at once (GitLab shows it once) and paste it into Token.',
        'In URL keep https://gitlab.com or give your GitLab address. Enter the path of the group whose projects Skopax reads in Group, such as acme, then choose Test connection.',
    ), docs='https://docs.gitlab.com/user/profile/personal_access_tokens/', docs_label='GitLab: personal access tokens'),
)
