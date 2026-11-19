from pathlib import Path
from time import monotonic

from skopax.contract import Capabilities, Context, Field, InstanceInfo, Knowledge, Missing, Plugin, ReadError, Setup, TestResult, Transport
from skopax.kit import connection
from skopax.messages import msg
from skopax.plugins.sharepoint.client import LOGIN_HOST, GRAPH_HOST, Graph, folder_problem, guid_problem, site_problem
from skopax.plugins.sharepoint.tools import NAME, TOOLS

ID = 'sharepoint'
HOME = Path(__file__).parent
TEST_BUDGET_S = 12
MAX_INSTANCES = 10
TENANT, CLIENT = 'Tenant ID', 'Client ID'
LEARN = 'https://learn.microsoft.com/en-us/graph/permissions-selected-overview'


def graph_of(config, transport: Transport) -> Graph:
    return Graph(config.get('tenant-id') or '', config.get('client-id') or '', config.get('client-secret') or '', config.get('site') or '', config.get('library') or '',
                 config.get('folder') or '', transport)


def make_client(info: InstanceInfo, transport: Transport) -> Graph:
    return graph_of(info.config, transport)


def problem(config) -> str | None:
    return (guid_problem(TENANT)(config.get('tenant-id') or '') or guid_problem(CLIENT)(config.get('client-id') or '') or site_problem(config.get('site') or '')
            or folder_problem(config.get('folder') or ''))


def counted(n: int, more: bool) -> str:
    return f'{n} or more items' if more else f"{n} {'item' if n == 1 else 'items'}"


def test(ctx: Context) -> TestResult:
    config = ctx.instance.config

    def ask() -> str:
        said = problem(config)
        if said:
            raise ReadError(400, said)
        deadline = monotonic() + TEST_BUDGET_S

        def bounded(method: str, url: str, headers: dict, data: bytes | None, timeout: float):
            left = deadline - monotonic()
            if left <= 0:
                raise TimeoutError()
            return ctx.http()(method, url, headers, data, min(timeout, left))

        g = graph_of(config, bounded)
        try:
            place = g.place()
            count, more = g.first_page()
        except Missing as e:
            raise ReadError(404, str(e.args[0]) if e.args and e.args[0] else msg('sharepoint.missing', folder=g.folder)) from None
        library = config.get('library') or 'the default library'
        return msg('sharepoint.test.ok', site=g.site, library=library, folder=place.folder, count=counted(count, more))

    return connection.result(ask)


PLUGIN = Plugin(
    id=ID, name=NAME, version='0.1.0', category='other',
    description='Preview, not yet tried on a live tenant. Searches and reads one SharePoint or OneDrive folder of company knowledge, such as runbooks, postmortems and design notes, read only and cited with links.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.2,<0.3', edition='community',
    capabilities=Capabilities(hosts=('@site', LOGIN_HOST, GRAPH_HOST), secrets=('client-secret',)),
    client=make_client, max_instances=MAX_INSTANCES,
    fields=(Field('tenant-id', TENANT, 'text', required=True, check=guid_problem(TENANT),
                  help='The Directory (tenant) ID of your Microsoft Entra tenant, a GUID.'),
            Field('client-id', CLIENT, 'text', required=True, check=guid_problem(CLIENT),
                  help='The Application (client) ID of the app registration Skopax signs in as, a GUID.'),
            Field('client-secret', 'Client secret', 'secret', required=True, help='The Value of a client secret of that app registration.'),
            Field('site', 'Site URL', 'url', required=True, ui=True, check=site_problem,
                  help='The address of one SharePoint site, such as https://contoso.sharepoint.com/sites/ops, or of a OneDrive, such as https://contoso-my.sharepoint.com/personal/jane_contoso_com.'),
            Field('library', 'Library', 'text', help="The name of the document library, such as Documents. Empty: the site's default library."),
            Field('folder', 'Folder', 'text', required=True, check=folder_problem,
                  help='The folder inside the library whose documents, subfolders included, the investigator may search and read, such as Runbooks/Prod.')),
    test=test, tools=TOOLS,
    knowledge=(Knowledge('sharepoint', NAME, (HOME / 'sharepoint.md').read_text(encoding='utf-8'), slack=True, inline=True),),
    setup=Setup(steps=(
        'Sign in to the Microsoft Entra admin center as at least an Application Developer. Browse to Entra ID > App registrations > New registration, name the app, keep Single tenant only, and select Register.',
        "On the app's Overview page copy the Application (client) ID into Client ID and the Directory (tenant) ID into Tenant ID.",
        'Select Certificates & secrets > Client secrets > New client secret, pick an expiration, select Add, and copy the Value into Client secret at once; it is never shown again.',
        'Select API permissions > Add a permission > Microsoft Graph > Application permissions, add Sites.Selected only, then a Privileged Role Administrator selects Grant admin consent. Do not add Files.Read.All or Sites.Read.All: they read every site in the tenant.',
        'Sites.Selected alone reads nothing until the app is granted one site. That grant needs a SharePoint Administrator who is a member of the site, signed in to Graph Explorer and consented to Sites.FullControl.All under Modify permissions.',
        'In Graph Explorer read the site id with GET https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/ops, then send POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions with roles ["read"] and grantedToIdentities holding the application id (the Client ID).',
        'Fill in Site URL and Folder (and Library when it is not the default one), then choose Test connection.',
    ), docs=LEARN, docs_label='Microsoft Learn: selected permissions'),
)
