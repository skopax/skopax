from collections.abc import Mapping
from pathlib import Path
from time import monotonic

from skopax.contract import Capabilities, Check, Context, Denied, Field, InstanceInfo, Knowledge, Missing, Plugin, Setup, ReadError, TestResult, Transport
from skopax.kit import connection
from skopax.messages import msg
from skopax.plugins.github.client import GITHUB_URL, GitHub, denied_text
from skopax.plugins.github.tools import TOOLS
from skopax.plugins.github.track import CHECK, SECTION, SECTION_HELP, links, releases, run

ID = 'github'
API_HOST = 'api.github.com'
TEST_BUDGET_S = 12
OFFSET = 223


def base_of(config: Mapping[str, str]) -> str:
    return (config.get('url') or GITHUB_URL).rstrip('/')


def make_client(info: InstanceInfo, transport: Transport) -> GitHub:
    return GitHub(info.config.get('token') or '', transport, owner=info.config.get('owner') or '', base=base_of(info.config), docs=info.config.get('docs') or '')


def counted(n: int, more: bool = False) -> str:
    return f"{n} or more repositories" if more else f"{n} {'repository' if n == 1 else 'repositories'}"


def test(ctx: Context) -> TestResult:
    config = ctx.instance.config

    def ask() -> str:
        deadline = monotonic() + TEST_BUDGET_S

        def bounded(method: str, url: str, headers: dict, data: bytes | None, timeout: float):
            left = deadline - monotonic()
            if left <= 0:
                raise TimeoutError()
            return ctx.http()(method, url, headers, data, min(timeout, left))

        gh = GitHub(config.get('token') or '', bounded, owner=config.get('owner') or '', base=base_of(config))
        try:
            gh.user()
        except Denied as e:
            if e.text != denied_text('owner', 401):
                raise
            raise ReadError(401, msg('github.token_invalid', status=401)) from None
        try:
            kind = gh.owner_kind()
        except Missing:
            raise ReadError(404, msg('github.owner_hidden', owner=gh.owner)) from None
        count = counted(*gh.repo_count())
        version = gh.version()
        if version:
            return msg('github.test.ghes', version=version, owner=gh.owner, kind=kind, count=count)
        return msg('github.test.ok', owner=gh.owner, kind=kind, count=count)

    return connection.result(ask)


PLUGIN = Plugin(
    id=ID, name='GitHub', version='0.1.0', category='code',
    description='Commits, code, pull requests, checks, deployments and releases of one GitHub owner, for deploy authors, tickets and code links.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.2,<0.4', edition='community', capabilities=Capabilities(hosts=('@url', API_HOST), secrets=('token',)),
    client=make_client, max_instances=1,
    fields=(Field('url', 'URL', 'url', required=True, default=GITHUB_URL, ui=True,
                  help='Address of GitHub: https://github.com, or a GitHub Enterprise Server, whose API is read under /api/v3.'),
            Field('token', 'Token', 'secret', required=True,
                  help='A fine-grained personal access token, read only, with Metadata, Contents, Pull requests, Checks, Commit statuses and Deployments set to Read.'),
            Field('owner', 'Owner', 'text', required=True, help='The organization or user whose repositories are read, such as acme. Code links must point under it.'),
            Field('repos', 'Tracked repositories', 'text',
                  help='Repositories whose deployments and releases the github check tracks, by name and comma separated, such as checkout-service, billing; at most 20. Empty: none.'),
            Field('docs', 'Docs repository', 'text',
                  help='A repository of the owner, with an optional folder, whose Markdown and text documents the investigator may search and read as company knowledge, such as acme/handbook/runbooks.'),
            Field('ca', 'CA certificate', 'pem', group='advanced', help='PEM certificate to trust instead of the system roots.'),
            Field('ca-pinned', 'Trust this certificate as it is', 'bool', default='false', show_if=('ca', '*'), group='advanced', help="For a server whose own certificate fails the usual checks: paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host. A CA certificate does not work with the switch on.")),
    test=test, tools=TOOLS,
    checks=(Check(CHECK, 'GitHub deployments and releases', 300, OFFSET, (CHECK,), run, sections=(SECTION,), section_help=SECTION_HELP),),
    links=links, releases=releases,
    knowledge=(Knowledge('github', 'GitHub', (Path(__file__).parent / 'github.md').read_text(encoding='utf-8'), slack=True, inline=True),),
    setup=Setup(steps=(
        'In GitHub open your profile menu > Settings > Developer settings > Personal access tokens > Fine-grained tokens, then Generate new token. Name it and set an expiration.',
        'Under Resource owner pick the organization or user you will enter as Owner. Under Repository access choose Only select repositories (or All repositories) for the repositories Skopax should read.',
        'Under Repository permissions set Metadata, Contents, Pull requests, Checks, Commit statuses and Deployments to Read-only. Leave every other permission at No access.',
        'Choose Generate token, copy it, and paste it into Token. If the organization requires approval, an owner must approve it first.',
        'In URL keep https://github.com, or give the address of your GitHub Enterprise Server. Enter the organization or user in Owner, then choose Test connection.',
    ), docs='https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens', docs_label='GitHub: managing personal access tokens'),
    company_knowledge='docs',
)
