from urllib.parse import urlsplit

from skopax.contract import Capabilities, Check, Context, Field, HttpError, Panel, PassResult, Plugin, SourceDown, Sweep, TestResult, Unreachable, anomaly, iso
from skopax.kit import connection
from skopax.remote import call_page

ID = 'hello'
TIMEOUT_S = 10
SECTION_HELP = {ID: 'The address the last pass asked, and what it answered'}


def shown(url: str) -> str:
    return urlsplit(url)._replace(query='', fragment='').geturl()


def problem(ctx: Context) -> str | None:
    url = ctx.instance.config['url']
    try:
        status = call_page(ctx.http(), url, timeout=TIMEOUT_S).status
    except HttpError as e:
        status = e.code
    except Unreachable:
        return f'{shown(url)} did not answer'
    return None if status == 200 else f'{shown(url)} answered HTTP {status}'


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        found = problem(ctx)
        if found:
            raise SourceDown(found)
        return f"{shown(ctx.instance.config['url'])} answered 200"

    return connection.result(ask)


def card(host: str, found: str) -> dict:
    x = anomaly(ID, f'{ID}-down', '-', host, '', 'warn', 1, None, None, '', 'not answering 200', found)
    x['key'] = f'{ID}|{host}'
    return x


def run(ctx: Context) -> Sweep:
    url = shown(ctx.instance.config['url'])
    found = problem(ctx)
    cards = [card(urlsplit(url).hostname or url, found)] if found else []
    row = {'url': url, 'ok': found is None, 'said': found or f'{url} answered 200', 'at': iso(ctx.now)}
    return Sweep(PassResult(ID, True, 'ok', cards, {}, (ID,)), sections={ID: [row]})


PLUGIN = Plugin(
    id=ID, name='Hello', version='0.1.0', category='other', description='Example plug-in: opens a card when an address does not answer HTTP 200.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.4',
    capabilities=Capabilities(hosts=('@url',)),
    fields=(Field('url', 'URL', 'url', required=True, help='The address to ask, for example https://shop.example.com/health.'),),
    test=test,
    checks=(Check(ID, 'Hello address', 300, 0, (ID,), run, sections=(ID,), section_help=SECTION_HELP),),
    panels=(Panel(ID, 'panel.js', label='Hello', group='more'),),
)
