from pathlib import Path

from skopax.contract import Capabilities, Check, Context, Field, InstanceInfo, Knowledge, PassResult, Plugin, Setup, ReadError, Sweep, TestResult, iso
from skopax.kit import access, connection
from skopax.plugins.sonarqube.engine import BUDGET_S, FAILING, SonarError, SonarQube, TEST_BUDGET_S, VALIDATE, SEARCH, card, filters_of, gates_of, link_for, projects_of, watched

ID = 'sonarqube'
CHECK = 'sonarqube'
NAME = 'SonarQube'
RAN = (CHECK,)
PART = 'projects'
ACCESS = access.ran_key(CHECK, PART)
NO_DATA = f'{CHECK}:no-data'
MEMO = 'gates'
DOT = ' \u00b7 '
SIG = 'sonar-gate'
PERMISSION = 'Browse on the projects'
TOKEN_REFUSED = 'the token was refused'
TOKEN_DENIED = 'the token lacks the Browse permission on the projects'


def client_of(ctx: Context, budget: float = BUDGET_S) -> SonarQube:
    return SonarQube.of(ctx.instance.config, ctx.http(), budget)


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        client = client_of(ctx, TEST_BUDGET_S)
        try:
            if client.call(VALIDATE).get('valid') is not True:
                raise ReadError(401, f'SonarQube says {TOKEN_REFUSED}: it is invalid or expired')
            got = client.call(SEARCH, qualifiers='TRK', ps=1)
        except SonarError as e:
            if e.code == 401:
                raise ReadError(401, f'SonarQube answered HTTP 401: {TOKEN_REFUSED}') from None
            if e.code == 403:
                raise ReadError(403, f'SonarQube answered HTTP 403: {TOKEN_DENIED}') from None
            if e.cause is not None:
                raise e.cause from None
            raise
        paging = got.get('paging') if isinstance(got.get('paging'), dict) else {}
        total = paging.get('total')
        return f"{NAME}{DOT}{total if isinstance(total, int) and not isinstance(total, bool) else 0} projects"

    return connection.result(ask)


def access_card(code: int) -> dict:
    return access.card(CHECK, NAME, PART, PERMISSION, code, 'token')


def run(ctx: Context) -> Sweep:
    client = client_of(ctx)
    try:
        listed, total, cut = projects_of(client)
        if not listed:
            return Sweep(PassResult(CHECK, True, f'ok{DOT}no data', [], {}, (NO_DATA, ACCESS)))
        chosen = watched(listed, filters_of(ctx.instance.config.get('projects') or ''))
        gates, unread = gates_of(client, [p['key'] for p in chosen])
    except SonarError as e:
        if e.denied:
            return Sweep(PassResult(CHECK, True, access.status('ok', [PART]), [access_card(e.code)], {}, (ACCESS,)))
        return Sweep(PassResult(CHECK, False, f'error: {e.reason}'))
    failing = {k for k, (state, _) in gates.items() if state == FAILING}
    before = ctx.memo(MEMO)
    first = not isinstance(before, dict) or not isinstance(before.get('quiet'), list)
    prior = set() if first else {k for k in before['quiet'] if isinstance(k, str)}
    partial = cut or bool(unread)
    quiet = failing if first else (failing & prior) | ((prior - set(gates)) if partial else set())
    names = {p['key']: p for p in chosen}
    found = [] if first else [card(names[k], gates[k][1]) for k in sorted(failing - prior)]
    for x in found:
        x['ranKey'] = f"{CHECK}:{x['svc']}"
    stamp = before.get('baseline') if not first and isinstance(before.get('baseline'), str) else iso(ctx.now)
    memos = {MEMO: {'quiet': sorted(quiet), 'baseline': stamp}}
    held = [a['ranKey'] for a in ctx.open_cards() if a.get('src') == CHECK and a.get('sig') == SIG and isinstance(a.get('ranKey'), str) and (not partial or a.get('svc') in gates)]
    notes = [f'{len(chosen)} projects', f'{len(failing)} failing'] + ([f'{len(unread)} not read'] if unread else []) + ([f'read {len(listed)} of {total} projects, more not read'] if cut else [])
    ran = ((ACCESS,) if partial else RAN + (ACCESS,)) + tuple(dict.fromkeys(held))
    return Sweep(PassResult(CHECK, True, DOT.join(['ok'] + notes), found, {}, ran), memos=memos)


def links(card_: dict, info: InstanceInfo) -> list[dict] | None:
    return link_for(card_, info.config.get('url') or '')


PLUGIN = Plugin(
    id=ID, name=NAME, version='0.1.0', category='code', description='A card for each SonarQube project whose quality gate starts to fail, checked once a day.', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.2,<0.4', edition='community', capabilities=Capabilities(hosts=('@url',), secrets=('token',)),
    fields=(Field('url', 'URL', 'url', required=True, ui=True, help='Address of SonarQube, for example https://sonar.example.com.'),
            Field('token', 'Token', 'secret', required=True, help='A user or project analysis token that can browse the projects. It is sent as the user name of a basic sign-in.'),
            Field('projects', 'Projects', 'text', help='Comma-separated project keys or names; * matches any characters. Empty watches every project the token can browse.'),
            Field('ca', 'CA certificate', 'pem', group='advanced', help='PEM certificate to trust instead of the system roots.'),
            Field('ca-pinned', 'Trust this certificate as it is', 'bool', default='false', show_if=('ca', '*'), group='advanced', help="For a server whose own certificate fails the usual checks: paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host. A CA certificate does not work with the switch on.")),
    test=test,
    checks=(Check(CHECK, 'SonarQube quality gates', 86400, 30600, RAN, run, daily=True),),
    links=links,
    knowledge=(Knowledge('sonarqube', NAME, (Path(__file__).parent / 'sonarqube.md').read_text(encoding='utf-8'), slack=True),),
    setup=Setup(steps=(
        'In SonarQube open the account menu > My account > Security. Under Generate Tokens choose the type User, set an expiration and choose Generate.',
        'Use an account that has the Browse permission on the projects to watch (Project settings > Permissions of each project). The token reads only what that account may browse.',
        'Copy the token at once (SonarQube shows it once) and paste it into Token.',
        'In URL give your SonarQube address. In Projects list project keys or names to watch (* matches any characters), or leave it empty for all browsable projects. Choose Test connection.',
    ), docs='https://docs.sonarsource.com/sonarqube-server/latest/user-guide/managing-tokens/', docs_label='SonarQube: managing tokens'),
)
