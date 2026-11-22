import datetime as dt
from urllib.parse import urlsplit

from skopax import clock
from skopax.contract import Capabilities, Check, Context, Field, HttpError, PassResult, Plugin, Setup, Sweep, TestResult, anomaly, iso
from skopax.kit import connection
from skopax.kit.browser import CONNECT_S, BrowserDown, endpoint

ID = 'browser'
NAME = 'Headless browser'
CHECK = 'browser'
SECTION = 'browser'
ACCESS = 'browser:access:browser'
ACCESS_KEY = 'access|-/website|browser'
DOWN_S = 300
DOT = ' \u00b7 '
ACCESS_LABEL = 'Website cannot read browser: the browser service refused the request'
DOWN_LABEL = 'Screenshot browser not answering'
SECTION_HELP = {SECTION: 'The headless browser: whether it answers, its version and when it last answered'}


def test(ctx: Context) -> TestResult:
    url = (ctx.instance.config.get('url') or '').strip()

    def ask() -> str:
        try:
            version = endpoint(url, CONNECT_S, ctx.http())[1]
        except BrowserDown as e:
            if e.code == 401:
                raise HttpError(401, url) from None
            raise
        return f"{NAME}{DOT}{version or '?'}"

    return connection.result(ask)


def previous_row(ctx: Context) -> dict:
    rows = ctx.previous(SECTION) or []
    return next((r for r in rows if isinstance(r, dict) and r.get('name') == 'browser'), {})


def seen_at(value: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value)).timestamp() if value else None
    except ValueError:
        return None


def down_card(host: str, since: object) -> dict:
    at = seen_at(since)
    said = f'; it last answered at {clock.date(at)} {clock.label(at)}' if at is not None else ' and has not answered since it was set up'
    x = anomaly(CHECK, 'browser-down', '-', host, '', 'warn', 1, None, None, '', DOWN_LABEL,
                f'The screenshot browser at {host} failed two checks in a row{said}. Screenshots and the page scan wait for it; check the browser service.',
                1, f'WARN {host} {DOWN_LABEL}')
    x['key'] = f'browser-down|{host}'
    return x


def access_card(host: str, code: int | None) -> dict:
    x = anomaly(CHECK, 'access', '-', 'website', 'browser', 'warn', 1, None, None, '', ACCESS_LABEL,
                f'The browser service at {host} refused the version read (HTTP {code}), so whether it answers is unknown and the open browser cards stay as they are. '
                'Let this server reach its DevTools endpoint without a login.', 1, f'WARN {ACCESS_LABEL}')
    x['ranKey'] = ACCESS
    return x


def browser_check(ctx: Context) -> Sweep:
    url = (ctx.instance.config.get('url') or '').strip()
    if not url:
        return Sweep(PassResult(CHECK, True, f'ok{DOT}no data', [], {}, (CHECK, ACCESS)), {SECTION: []})
    host = urlsplit(url).hostname or url
    before = previous_row(ctx)
    try:
        version = endpoint(url, CONNECT_S, ctx.http())[1]
    except BrowserDown as e:
        row = {'name': 'browser', 'ok': False, 'version': before.get('version'), 'lastSeen': before.get('lastSeen'), 'error': e.reason or 'BrowserDown'}
        if e.code in (401, 403):
            return Sweep(PassResult(CHECK, True, f'ok{DOT}no access to browser', [access_card(host, e.code)], {}, (ACCESS,)), {SECTION: [row]})
        last = seen_at(before.get('lastSeen'))
        late = before.get('ok') is False and (last is None or ctx.now - last >= DOWN_S)
        return Sweep(PassResult(CHECK, True, f'ok{DOT}browser down', [down_card(host, before.get('lastSeen'))] if late else [], {}, (CHECK,)), {SECTION: [row]})
    row = {'name': 'browser', 'ok': True, 'version': version or None, 'lastSeen': iso(ctx.now), 'error': None}
    return Sweep(PassResult(CHECK, True, 'ok', [], {}, (CHECK, ACCESS)), {SECTION: [row]})


PLUGIN = Plugin(
    id=ID, name=NAME, version='0.1.0', category='other',
    description='A headless Chrome service that Website screenshots and the page scan load pages in; watched for whether it answers and which version it runs.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.6', edition='community',
    capabilities=Capabilities(hosts=('@url',)),
    fields=(Field('url', 'Browser URL', 'url', required=True,
                  help='Chrome DevTools endpoint of a headless browser service, for example http://browser:9222. Website instances pick it for screenshots and the page scan. '
                       'It holds no secrets: run it on a private network, see the compose example in docs/plugins.md.'),),
    setup=Setup(
        steps=(
            'Run a headless Chrome service that exposes the DevTools endpoint on a private network; docs/plugins.md and docs/operations.md show a compose example.',
            'Set Browser URL to its address, for example http://browser:9222. Test connection reads the browser version.',
            'In each Website instance, pick this browser under Headless browser. Screenshots and the page scan need it.',
        ),
        docs='https://github.com/chromedp/docker-headless-shell', docs_label='chromedp headless-shell image'),
    test=test,
    checks=(Check(CHECK, 'Browser answers', 300, 45, (CHECK,), browser_check, sections=(SECTION,), section_help=SECTION_HELP),),
)
