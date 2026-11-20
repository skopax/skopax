import json

from skopax.contract import Outbound
from skopax.messages import msg
from skopax.plugins import teams
from skopax.plugins.teams import cards
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply, Unreachable
from skopax.store import Store
from tests.conftest import state
from tests.teams_kit import ALICE, AT, BIZDEV, SEO, Transport, open_card, route, routes_of, runtime_of, tag, texts

NOW = 1_794_240_000.0
KEY = 'log|checkout-service|ERROR|slug'


def refusing(code: int):
    def send(method, url, headers, data, timeout):
        raise HttpError(code, url, 'no')

    return send


def down(method, url, headers, data, timeout):
    raise Unreachable(f'{url} unreachable (ConnectionRefusedError)')


def checked(store: Store, transport, **values) -> dict:
    runtime = runtime_of(store, transport)
    return runtime.connection_test(teams.PLUGIN, None, {'webhook': SEO, 'tags': 'seo'} | values, 5, NOW)


def lines_of(kind: str, text: str, why: str) -> list[dict]:
    why_line = [{'role': 'why', 'label': 'Why', 'text': why}] if why else []
    if kind == 'info':
        return [{'role': 'info', 'label': 'Info', 'text': text}] + why_line
    return why_line + [{'role': 'recommended', 'label': 'Recommended', 'text': text}]


def message(**over) -> dict:
    base = {'kind': 'action', 'title': 'Skopax \u00b7 SEO', 'text': 'We recommend adding a redirect for the old spring sale page.', 'why': 'Visitors from the old campaign land on an empty page.',
            'urgent': False, 'link': 'https://skopax.example.com/c/7', 'page': 'https://shop.example.com/spring', 'chat': 'https://skopax.example.com/#t=chat/new/7', 'mentions': [],
            'card': {'key': KEY, 'svc': 'checkout-service', 'ns': 'shop', 'sev': 'warn', 'label': 'ERROR lines', 'short': 'x', 'number': 7}, 'teams': ['SEO'], 'kids': [], 'cards': [],
            'more': 0, 'board': 'https://skopax.example.com/#t=anoms', 'attribution': None}
    out = base | over
    return out if 'lines' in over else out | {'lines': lines_of(out['kind'], out['text'], out['why'])}


def test_teams_identity_and_manifest():
    # when
    manifest = describe(teams.PLUGIN, icon=True)

    # then
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['category'], manifest['version'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'teams', 'Microsoft Teams', 'chat', '0.1.0', 'Skopax', 'Apache-2.0', '>=0.1,<0.5', 'community')
    assert manifest['capabilities'] == {'hosts': [], 'secrets': ['webhook'], 'agentReads': False} and manifest['uses'] == [] and manifest['checks'] == []
    assert 'Workflows' in manifest['description'] and manifest['description'].endswith('.')
    assert teams.PLUGIN.outbound == Outbound(teams.post, 'tags', 'mentions', link_field='webhook', keys_field='keys', sev_field='min-sev')
    assert 'skopax.plugins.teams' in BUILTIN and Registry.of(teams.PLUGIN).get('teams').state == 'ok'
    assert Registry.of(teams.PLUGIN).icon('teams').startswith('<svg')


def test_teams_connection_test_reports_ok_and_failures(store: Store):
    # given
    t = Transport()

    # when
    ok = checked(store, t)
    rejected = checked(store, refusing(401))
    answered = checked(store, refusing(400))
    unreachable = checked(store, down)
    plain = checked(store, t, webhook='http://hooks.example.com/workflows/seo/invoke?sig=SEO')
    empty = checked(store, t, tags='')

    # then
    assert (ok['ok'], ok['summary']) == (True, 'Teams took the test post')
    assert [texts(b) for _, b in t.calls] == [['Skopax \u00b7 connection test', msg('routes.test')]], 'the refused link and the empty route post nothing'
    assert (rejected['ok'], rejected['summary']) == (False, msg('connection.rejected', status=401))
    assert (answered['ok'], answered['summary']) == (False, msg('connection.status', status=400))
    assert (unreachable['ok'], unreachable['summary']) == (False, msg('connection.unreachable'))
    assert (plain['ok'], plain['summary']) == (False, msg('integrations.route_link'))
    assert (empty['ok'], empty['summary']) == (False, msg('integrations.route_empty'))
    assert 'sig=' not in json.dumps([ok, rejected, answered, unreachable, plain, empty])


def test_teams_defaults_are_neutral():
    # when
    fields = {f.key: f.default for f in teams.PLUGIN.fields}
    kinds = {f.key: (f.kind, f.required) for f in teams.PLUGIN.fields}
    settings = {s.key: (s.kind, s.default, s.low, s.high, s.choices) for s in teams.PLUGIN.settings}

    # then
    assert fields == {'webhook': '', 'tags': '', 'mentions': '', 'keys': '', 'min-sev': ''}
    assert kinds == {'webhook': ('secret', True), 'tags': ('text', False), 'mentions': ('text', False), 'keys': ('text', False), 'min-sev': ('choice', False)}
    assert settings == {'repeat-days': ('float', '7', 0, 365, ()), 'min-sev': ('choice', 'warn', None, None, ('info', 'warn', 'crit')), 'fixed-posts': ('bool', 'false', None, None, ())}


def test_a_post_answers_ok_or_the_status_and_never_the_webhook(store: Store):
    # given
    runtime = runtime_of(store, refusing(429), route('seo', SEO, tags='seo'))
    ctx = runtime.context(teams.PLUGIN, runtime.instances.get('teams', 'seo'), NOW)
    fine = runtime_of(store, Transport(), route('seo', SEO, tags='seo'))

    # when
    refused = teams.post(ctx, message())
    sent = teams.post(fine.context(teams.PLUGIN, fine.instances.get('teams', 'seo'), NOW), message())

    # then
    assert (refused, sent) == ({'ok': False, 'status': 429}, {'ok': True, 'status': 200})


def test_an_action_post_leads_with_the_why_then_the_recommendation():
    # when
    body = cards.render(message())
    plain = cards.render(message(why=''))
    urgent = cards.render(message(urgent=True))
    info = cards.render(message(kind='info', text='Newsletter sign-ups fail right now; engineering is fixing it.'))

    # then
    blocks = body['attachments'][0]['content']['body']
    assert [b['text'] for b in blocks] == ['Skopax \u00b7 SEO', 'Why: Visitors from the old campaign land on an empty page.',
                                           '**Recommended:** We recommend adding a redirect for the old spring sale page.',
                                           '#7 \u00b7 [Open page](https://shop.example.com/spring) \u00b7 [Open card](https://skopax.example.com/c/7) \u00b7 [Chat](https://skopax.example.com/#t=chat/new/7)']
    assert blocks[1]['weight'] == 'Bolder' and blocks[1]['separator'] is True and 'weight' not in blocks[2], 'the reason is the prominent line'
    assert texts(plain)[1:3] == ['**Recommended:** We recommend adding a redirect for the old spring sale page.', texts(body)[3]], 'an action with no why shows the recommendation alone'
    assert texts(urgent)[1] == '\U0001f534 Urgent \u00b7 Why: Visitors from the old campaign land on an empty page.'
    assert texts(info)[1:3] == ['**Info:** Newsletter sign-ups fail right now; engineering is fixing it.', 'Why: Visitors from the old campaign land on an empty page.']
    assert 'To do' not in json.dumps([body, plain, urgent, info])
    assert texts(cards.render(message(lines=[{'role': 'recommended', 'label': 'Recommended', 'text': 'A'}, {'role': 'why', 'label': 'Why', 'text': 'B'}])))[1:3] == ['**Recommended:** A', 'Why: B'], 'the renderer keeps the order core gives'


def test_no_attribution_shows_nothing_and_a_given_line_shows_last_escaped(store: Store):
    # given
    store.save(state(open_card(KEY, tags=tag('bizdev'), business={'text': 'We recommend checking the sale page.', 'by': 'pod', 'at': AT})), None)
    bare, given = Transport(), Transport()
    line = {'text': 'Sent by *Skopax* [beta]', 'href': 'https://skopax.example.com/about'}

    # when
    routes_of(store, bare, route('bizdev', BIZDEV, tags='bizdev')).tagged(store.load()['anomalies'], now=NOW)
    store.anomalies.update_one({'_id': KEY}, {'$set': {'teamsPosts': []}})
    routes_of(store, given, route('bizdev', BIZDEV, tags='bizdev'), attribution=lambda: line).tagged(store.load()['anomalies'], now=NOW)

    # then
    shown, plain = texts(given.calls[0][1]), texts(bare.calls[0][1])
    assert shown[:-1] == plain, 'no attribution adds no line'
    assert shown[-1] == '[Sent by \\*Skopax\\* \\[beta\\]](https://skopax.example.com/about)'
    assert given.calls[0][1]['attachments'][0]['content']['body'][-1]['size'] == 'Small'
    assert cards.render(message(attribution={'text': '<at>x</at>', 'href': 'javascript:alert(1)'}))['attachments'][0]['content']['body'][-1]['text'] == '\\<at\\>x\\</at\\>'


def test_mentions_ping_the_route_people_by_name(store: Store):
    # when
    body = cards.render(message(mentions=[ALICE, 'bob.stone@example.com']))

    # then
    content = body['attachments'][0]['content']
    assert texts(body)[1] == 'cc <at>Alice Archer</at>, <at>Bob Stone</at>'
    assert content['msteams'] == {'width': 'Full', 'entities': [{'type': 'mention', 'text': '<at>Alice Archer</at>', 'mentioned': {'id': ALICE, 'name': 'Alice Archer'}},
                                                               {'type': 'mention', 'text': '<at>Bob Stone</at>', 'mentioned': {'id': 'bob.stone@example.com', 'name': 'Bob Stone'}}]}


def test_card_text_from_a_check_reaches_teams_as_plain_text_never_as_a_live_link():
    # given
    planted = '[Sign in here](https://evil.example/p) **now**'
    body = {'kind': 'card', 'title': 'Skopax', 'cards': [{'sev': 'warn', 'svc': 'web_shop', 'label': f'ERROR {planted}', 'what': f'404 on /login {planted}', 'number': 3,
                                                        'link': 'https://skopax.example.com/#a=x', 'chat': 'https://skopax.example.com/#t=chat/new/x'}]}

    # when
    shown = texts(cards.render(body))

    # then
    joined = '\n'.join(shown)
    assert '](https://evil.example' not in joined and '**now**' not in joined
    assert '\\[Sign in here\\]\\(https://evil.example/p\\)' in joined and 'web\\_shop' in joined
    assert '[Open in Skopax](https://skopax.example.com/#a=x)' in joined, 'the links Skopax makes stay live'


def test_a_teams_test_on_a_deleted_workflow_says_the_address_holds_no_workflow(store: Store):
    # when
    gone = checked(store, refusing(404))

    # then
    assert (gone['ok'], gone['summary']) == (False, msg('teams.gone')) and 'deleted' in gone['summary']
