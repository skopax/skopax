import json
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from skopax import remote
from skopax.app import create_app
from skopax.contract import InstanceInfo, ReadError
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import safe_browsing
from skopax.plugins.safe_browsing import safebrowsing as engine
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance
from tests.test_investigator_tools import NOW, box

KEY = 'g-key-1'
SEAL = 'sb-seal'
API = engine.API_URL
STATUS = engine.STATUS_URL
HOSTS = 'www.example.com, login.example.com'
VALUES = {'api-key': KEY, 'hosts': HOSTS}
FREE = {'hosts': HOSTS}
ALICE = 'alice@example.com'
SOCIAL = 'social engineering (Deceptive pages)'
DOT = f' {chr(0xb7)} '
ACCESS = 'safebrowsing:access:hosts'
NO_ACCESS = 'Safe Browsing cannot read hosts: the API key lacks access to the Safe Browsing API'
NEEDS_KEY = 'the safebrowsing tool needs an API key on this Safe Browsing instance; without one only the check runs, through the public site status page'
NOT_STATUS = 'not a Safe Browsing status (rate limited?)'


class Timed(FakeHttp):
    def __init__(self, routes: dict | None = None):
        super().__init__(routes)
        self.timeouts: list[float] = []

    def __call__(self, method: str, url: str, headers: dict, data: bytes | None, timeout: float):
        self.timeouts.append(timeout)
        return super().__call__(method, url, headers, data, timeout)


@dataclass
class Ctx:
    config: dict
    transport: object
    now: float = NOW
    cards: list = field(default_factory=list)

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo('main', True, None, self.config)

    def http(self):
        return self.transport

    def open_cards(self, plugin_wide: bool = False) -> list[dict]:
        return list(self.cards)


def status(host: str, code: int = 1, social: bool = False) -> str:
    flag = 'true' if social else 'false'
    return f')]}}\'\n\n[["sb.ssr",{code},false,false,{flag},false,false,1793710899097,"{host}",false]]'


def pages(answers: dict, before=None) -> Timed:
    def answer(q, body):
        if before is not None:
            before(q['site'][0])
        return answers[q['site'][0]]
    return Timed({('GET', STATUS): answer})


def no_pause(monkeypatch, sleeps: list | None = None) -> list:
    sleeps = [] if sleeps is None else sleeps
    monkeypatch.setattr(engine, 'pause', sleeps.append)
    return sleeps


def matching(*flagged: str, kind: str = 'SOCIAL_ENGINEERING') -> Timed:
    def answer(q, body):
        sent = [e['url'] for e in body['threatInfo']['threatEntries']]
        return {'matches': [{'threatType': kind, 'platformType': 'ANY_PLATFORM', 'threat': {'url': u}} for u in flagged if u in sent]}
    return Timed({('POST', API): answer})


def tool_runtime(store, http, instances: dict | None = None) -> Runtime:
    reg = Registry.of(safe_browsing.PLUGIN)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(SEAL)), http)
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, safe_browsing.PLUGIN, name, sealer_key=SEAL, primary=i == 0, **values)
    runtime.refresh()
    return runtime


def app_with(store, tmp_path, http, instances: dict | None = None):
    sealing = master_key(store.db, settings())
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, safe_browsing.PLUGIN, name, sealer_key=sealing, primary=i == 0, **values)
    return TestClient(create_app(settings(), store, registry=Registry.of(safe_browsing.PLUGIN), transport=http, kube=Kube(root=tmp_path)))


def by_key(state: dict) -> dict:
    return {a['key']: a for a in state['anomalies']}


def test_safe_browsing_cards_a_listed_host_and_stops_when_google_pushes_back(monkeypatch):
    # given
    http = pages({'www.example.com': status('www.example.com'), 'login.example.com': status('login.example.com', 3, True), 'shop.example.com': '<html>sorry</html>',
                  'example.org': status('example.org')})
    sleeps = no_pause(monkeypatch)

    # when
    sweep = safe_browsing.run(Ctx({'hosts': 'www.example.com, login.example.com, shop.example.com, example.org'}, http))
    out = sweep.result

    # then
    assert out.ok and out.ran == ('safebrowsing:www.example.com', 'safebrowsing:login.example.com', ACCESS) and sleeps == [3.0, 3.0]
    assert out.status == f'error: hosts: shop.example.com: {NOT_STATUS}'
    [card] = out.found
    assert (card['key'], card['sev'], card['ranKey'], card['host']) == ('safebrowsing|-/login.example.com|listed', 'crit', 'safebrowsing:login.example.com', 'login.example.com')
    assert SOCIAL in card['what']
    assert [r['host'] for r in sweep.sections['safeBrowsing'] if r['part'] == 'host'] == ['www.example.com', 'login.example.com']
    assert sweep.sections['safeBrowsing'][-1] == {'part': 'summary', 'at': iso(NOW), 'via': 'status page', 'stopped': f'shop.example.com: {NOT_STATUS}'}
    method, url, headers, _ = http.calls[0]
    assert (method, url, headers['User-Agent'], http.timeouts[0]) == ('GET', f'{STATUS}?site=www.example.com', engine.USER_AGENT, 15) and len(http.calls) == 3
    assert 'json' not in headers['Accept']
    assert engine.host_list('') == () and engine.host_list(' A.com, a.com ') == ('a.com',)
    assert engine.host_list('https://Shop.example.com/x, ,www.example.com/') == ('shop.example.com', 'www.example.com')


@pytest.mark.parametrize('answer, said', [
    (HttpError(429, STATUS), 'error: hosts: www.example.com: HTTP 429 (rate limited?)'),
    (remote.Unreachable(f'{STATUS} unreachable (ConnectionRefusedError)'), 'error: hosts: www.example.com: status page unreachable'),
])
def test_safe_browsing_reports_a_failed_first_lookup_as_a_failed_check(monkeypatch, answer, said):
    # given
    no_pause(monkeypatch)

    # when
    out = safe_browsing.run(Ctx({'hosts': 'www.example.com'}, pages({'www.example.com': answer}))).result

    # then
    assert not out.ok and out.found == [] and out.status == said


def test_a_failed_pass_keeps_its_count_and_reason_inside_the_stored_status(store: Store, tmp_path, monkeypatch):
    # given
    no_pause(monkeypatch)
    hosts = ['checkout.shop.example.com'] + [f'h{i}.example.com' for i in range(11)]
    http = pages({h: HttpError(429, STATUS) for h in hosts})

    # when
    with app_with(store, tmp_path, http, instances={'main': {'hosts': ', '.join(hosts)}}) as c:
        c.app.state.sweeper.run('safe-browsing', 'safebrowsing', NOW)
    stored = store.load()['sources']['safebrowsing']

    # then
    assert stored == 'error: hosts: checkout.shop.example.com: HTTP 429 (rate limited?)'


def test_a_keyless_pass_goes_through_the_app_transport(store: Store, tmp_path, monkeypatch):
    # given
    no_pause(monkeypatch)
    http = pages({'www.example.com': status('www.example.com'), 'login.example.com': status('login.example.com', 3, True)})

    # when
    with app_with(store, tmp_path, http, instances={'main': FREE}) as c:
        c.app.state.sweeper.run('safe-browsing', 'safebrowsing', NOW)
    state = store.load()

    # then
    assert [c[1] for c in http.hits(STATUS)] == [f'{STATUS}?site=www.example.com', f'{STATUS}?site=login.example.com']
    assert by_key(state)['safebrowsing|-/login.example.com|listed']['status'] == 'open' and state['sources']['safebrowsing'] == 'ok'


def test_with_an_api_key_safe_browsing_asks_the_official_api_once_for_every_host():
    # given
    http = matching('https://login.example.com/')

    # when
    sweep = safe_browsing.run(Ctx(VALUES, http))
    out = sweep.result

    # then
    assert out.ok and out.status == 'ok' and out.ran == ('safebrowsing:www.example.com', 'safebrowsing:login.example.com', ACCESS)
    [card] = out.found
    assert (card['key'], card['sev'], card['svc']) == ('safebrowsing|-/login.example.com|listed', 'crit', 'login.example.com') and SOCIAL in card['what']
    assert '(status 3): https://login.example.com/.' in card['what']
    method, url, _, body = http.calls[0]
    assert len(http.calls) == 1 and url.endswith('?key=g-key-1') and [e['url'] for e in body['threatInfo']['threatEntries']] == ['https://www.example.com/', 'https://login.example.com/']
    assert body['client'] == {'clientId': 'site-checks', 'clientVersion': '0.1.0'} and http.hits(STATUS) == []
    assert sweep.sections['safeBrowsing'][-1] == {'part': 'summary', 'at': iso(NOW), 'via': 'api'}


def test_a_listing_card_names_the_first_three_addresses_and_counts_the_rest():
    # given
    urls = [f'https://www.example.com/p{i}' for i in range(5)]

    # when
    card = engine.card({'host': 'www.example.com', 'status': 3, 'flags': ['malware'], 'checkedAt': None, 'urls': urls})
    clean = engine.card({'host': 'www.example.com', 'status': 1, 'flags': [], 'checkedAt': None})

    # then
    assert ': https://www.example.com/p0, https://www.example.com/p1, https://www.example.com/p2 and 2 more. ' in card['what'] and clean is None


def test_an_invalid_safe_browsing_key_fails_the_check_without_showing_the_key():
    # given
    http = FakeHttp({('POST', API): HttpError(400, API, f'bad key {KEY}')})

    # when
    out = safe_browsing.run(Ctx(VALUES, http)).result

    # then
    assert (out.ok, out.status) == (False, 'error: hosts: Safe Browsing API HTTP 400 (key refused or API not enabled)')


@pytest.mark.parametrize('code', [401, 403])
def test_a_key_without_access_raises_an_access_card_and_keeps_the_host_cards(code):
    # given
    http = FakeHttp({('POST', API): HttpError(code, API, f'PERMISSION_DENIED {KEY}')})
    cards = [{'key': 'safebrowsing|-/www.example.com|listed', 'src': 'safebrowsing', 'svc': 'www.example.com', 'host': 'www.example.com'},
             {'key': 'safebrowsing|-/old.example.com|listed', 'src': 'safebrowsing', 'svc': 'old.example.com', 'host': 'old.example.com'}]

    # when
    sweep = safe_browsing.run(Ctx(VALUES, http, cards=cards))
    out = sweep.result

    # then
    assert out.ok and out.status == f'ok{DOT}no access to hosts' and out.ran == ('safebrowsing:old.example.com', ACCESS)
    [card] = out.found
    assert (card['key'], card['ranKey'], card['sev'], card['label'], card['src']) == ('access|-/safe-browsing|hosts', ACCESS, 'warn', NO_ACCESS, 'safebrowsing')
    assert f'HTTP {code}' in card['what'] and 'refused the API key' in card['what'] and KEY not in json.dumps(card) and sweep.sections == {}


def test_the_access_card_clears_once_the_key_reads_again(store: Store, tmp_path):
    # given
    answers = {'code': 403}

    def answer(q, body):
        return HttpError(answers['code'], API) if answers['code'] else {}

    http = FakeHttp({('POST', API): answer})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('safe-browsing', 'safebrowsing', NOW)
        refused = by_key(store.load())['access|-/safe-browsing|hosts']['status']
        answers['code'] = 0
        c.app.state.sweeper.run('safe-browsing', 'safebrowsing', NOW + 900)
    state = store.load()

    # then
    assert refused == 'open' and by_key(state)['access|-/safe-browsing|hosts']['status'] == 'cleared' and state['sources']['safebrowsing'] == 'ok'


def test_a_host_taken_off_the_list_closes_its_card_at_the_next_pass(monkeypatch):
    # given
    no_pause(monkeypatch)
    cards = [{'key': 'safebrowsing|-/old.example.com|listed', 'src': 'safebrowsing', 'svc': 'old.example.com', 'host': 'old.example.com', 'ranKey': 'safebrowsing:old.example.com-east'},
             {'key': 'safebrowsing|-/www.example.com|listed', 'src': 'safebrowsing', 'svc': 'www.example.com', 'ranKey': 'safebrowsing:www.example.com'},
             {'key': 'safebrowsing|-/gone.example.com|listed', 'src': 'safebrowsing', 'svc': 'gone.example.com'},
             {'key': 'other|-/x', 'src': 'prom', 'svc': 'x'}]

    # when
    api = safe_browsing.run(Ctx(VALUES, matching(), cards=cards)).result
    page = safe_browsing.run(Ctx(FREE, pages({'www.example.com': status('www.example.com'), 'login.example.com': status('login.example.com')}), cards=cards)).result

    # then
    left = ('safebrowsing:gone.example.com', 'safebrowsing:old.example.com', ACCESS)
    assert api.ran == ('safebrowsing:www.example.com', 'safebrowsing:login.example.com') + left and api.status == 'ok'
    assert page.ran == api.ran and page.status == 'ok'


def test_a_removed_host_card_closes_through_the_sweeper(store: Store, tmp_path):
    # given
    http = matching('https://old.example.com/', kind='MALWARE')

    # when
    with app_with(store, tmp_path, http, instances={'main': {'api-key': KEY, 'hosts': 'www.example.com, old.example.com'}}) as c:
        c.app.state.sweeper.run('safe-browsing', 'safebrowsing', NOW)
        first = by_key(store.load())['safebrowsing|-/old.example.com|listed']['status']
        c.app.state.runtime.instances.update('safe-browsing', 'main', {'hosts': 'www.example.com'}, ALICE)
        c.app.state.sweeper.run('safe-browsing', 'safebrowsing', NOW + 900)
    state = store.load()

    # then
    assert first == 'open' and by_key(state)['safebrowsing|-/old.example.com|listed']['status'] == 'cleared'
    assert [e['url'] for e in http.calls[-1][3]['threatInfo']['threatEntries']] == ['https://www.example.com/'] and state['sources']['safebrowsing'] == 'ok'


def test_a_pass_that_outruns_its_budget_reads_the_hosts_it_can(monkeypatch):
    # given
    clock = [500.0]
    monkeypatch.setattr(engine, 'monotonic', lambda: clock[0])
    no_pause(monkeypatch)

    def slow(host: str) -> None:
        clock[0] += 100

    http = pages({h: status(h) for h in ('www.example.com', 'login.example.com', 'shop.example.com')}, before=slow)

    # when
    sweep = safe_browsing.run(Ctx({'hosts': 'www.example.com, login.example.com, shop.example.com'}, http))
    out = sweep.result

    # then
    assert out.ok and out.status == f'ok{DOT}read 2 of 3 hosts' and out.ran == ('safebrowsing:www.example.com', 'safebrowsing:login.example.com', ACCESS)
    assert [c[1] for c in http.calls] == [f'{STATUS}?site=www.example.com', f'{STATUS}?site=login.example.com']
    assert sweep.sections['safeBrowsing'][-1]['stopped'] == 'out of time'


def test_an_empty_host_list_is_a_quiet_pass():
    # when
    out = safe_browsing.run(Ctx({'api-key': KEY, 'hosts': ' , '}, FakeHttp())).result

    # then
    assert (out.ok, out.status, out.ran, out.found) == (True, f'ok{DOT}no data', ('safebrowsing:no-data', ACCESS), [])


def test_the_safebrowsing_tool_is_offered_only_from_an_instance_with_an_api_key(store: Store):
    # given
    from skopax.investigator.toolset import ToolSet
    keyless = tool_runtime(store, FakeHttp(), {'free': FREE})

    # when
    held = ToolSet(keyless).status('safebrowsing', None)
    offered = [d['name'] for d in PluginTools(keyless).definitions()]
    with pytest.raises(ReadError) as direct:
        safe_browsing.PLUGIN.tools[0].run(keyless.context(safe_browsing.PLUGIN, keyless.instances.get('safe-browsing', 'free'), NOW), {'urls': ['https://www.example.com/']})

    # then
    assert safe_browsing.PLUGIN.tools[0].needs_field == 'api-key'
    assert held == {'state': 'off', 'reason': msg('tools.off.needs_field', field='API key')} and offered == []
    assert direct.value.message == NEEDS_KEY, 'the handler still refuses a keyless instance on its own'
    assert 'safe-browsing' in [k.id for k in keyless.knowledge()], 'a plug-in with checks keeps its knowledge while its only tool is held'


def test_the_safebrowsing_tool_needs_the_key_and_answers_per_url(store: Store):
    # given
    flagged = 'https://login.example.net/signin'
    answers = {'denied': False}

    def answer(q, body):
        return HttpError(403, API, f'PERMISSION_DENIED {KEY}') if answers['denied'] else {'matches': [{'threatType': 'SOCIAL_ENGINEERING', 'threat': {'url': flagged}}]}

    http = FakeHttp({('POST', API): answer})
    b = box(store, plugin_tools=PluginTools(tool_runtime(store, http, {'main': VALUES, 'free': FREE})))

    # when
    out = json.loads(b.call('safebrowsing', {'urls': ['https://www.example.com/', flagged, flagged]})[0])
    bad = b.call('safebrowsing', {'urls': ['javascript:alert(1)']})
    keyless_call = b.call('safebrowsing', {'urls': ['https://www.example.com/'], 'instance': 'free'})
    answers['denied'] = True
    denied = box(store, plugin_tools=PluginTools(tool_runtime(store, http, {}))).call('safebrowsing', {'urls': ['https://www.example.com/']})

    # then
    assert out['urls'] == [{'url': 'https://www.example.com/', 'flagged': False, 'threats': []}, {'url': flagged, 'flagged': True, 'threats': [SOCIAL]}]
    assert bad[1] and 'is not an http(s) address' in bad[0]
    assert keyless_call[1] and keyless_call[0].startswith('instance must be one of main, got "free"') and len(http.calls) == 2 and safe_browsing.PLUGIN.tools[0].per_run == 3
    assert denied == (f'{NO_ACCESS} (HTTP 403)', True)


@pytest.mark.parametrize('args, why', [
    ({}, 'urls needs at least one address'),
    ({'urls': 'https://www.example.com/'}, 'urls must be a list of addresses'),
    ({'urls': [f'https://www.example.com/{i}' for i in range(21)]}, 'at most 20 urls per call'),
    ({'urls': ['ftp://www.example.com/']}, 'ftp://www.example.com/ is not an http(s) address'),
])
def test_bad_tool_inputs_are_refused_before_any_call(args, why):
    # given
    http = FakeHttp()

    # when
    with pytest.raises(ReadError) as refused:
        safe_browsing.safebrowsing_tool(Ctx(VALUES, http), args)

    # then
    assert (refused.value.status, refused.value.message) == (400, why) and http.calls == []


def test_safe_browsing_cards_link_the_status_report_and_search_console():
    # given
    info = InstanceInfo('main', True, None, VALUES)

    # when
    got = safe_browsing.links({'src': 'safebrowsing', 'svc': 'login.example.com'}, info)

    # then
    assert got == [{'label': 'Safe Browsing site status', 'url': 'https://transparencyreport.google.com/safe-browsing/search?url=login.example.com'},
                   {'label': 'Search Console security issues', 'url': 'https://search.google.com/search-console/security-issues'}]
    assert safe_browsing.links({'src': 'crux', 'svc': 'www.example.com'}, info) is None


def test_safe_browsing_identity_and_manifest():
    # given
    registry = Registry.load({}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    got = registry.get('safe-browsing')
    manifest = describe(safe_browsing.PLUGIN, icon=True)
    check, tool = safe_browsing.PLUGIN.checks[0], safe_browsing.PLUGIN.tools[0]
    fields = {f.key: f for f in safe_browsing.PLUGIN.fields}

    # then
    assert 'skopax.plugins.safe_browsing' in BUILTIN and got is not None and got.state == 'ok' and got.plugin is safe_browsing.PLUGIN
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'safe-browsing', 'Safe Browsing', '0.1.0', 'web', 'Skopax', 'AGPL-3.0-only', '>=0.1,<0.8', 'community')
    assert manifest['capabilities'] == {'hosts': ['safebrowsing.googleapis.com', 'transparencyreport.google.com'], 'secrets': ['api-key'], 'agentReads': False}
    assert manifest['uses'] == []
    assert [(f.key, f.kind, f.required, f.ui) for f in safe_browsing.PLUGIN.fields] == [('api-key', 'secret', False, False), ('hosts', 'text', True, False)]
    assert 'tool stays hidden' in fields['api-key'].help and ' off' not in fields['api-key'].help
    assert (check.id, check.period, check.offset, check.ran, check.sections, check.tool, check.once) == ('safebrowsing', 900, 675, ('safebrowsing',), ('safeBrowsing',), 'safebrowsing', False)
    assert check.section_help['safeBrowsing'] and safe_browsing.PLUGIN.rules == ()
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input, tool.input_schema['additionalProperties']) == ('safebrowsing', 'read', 3, True, 'instance', False)
    assert 'API key' in tool.description
    assert manifest['panels'] == [] and registry.icon('safe-browsing').startswith('<svg')
    assert [k.id for k in safe_browsing.PLUGIN.knowledge] == ['safe-browsing'] and len(safe_browsing.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert safe_browsing.PLUGIN.client is None and safe_browsing.PLUGIN.probe_targets is None


def test_safe_browsing_defaults_are_neutral():
    # then
    assert {f.key: f.default for f in safe_browsing.PLUGIN.fields} == {'api-key': '', 'hosts': ''}
    assert {s.key: s.default for s in safe_browsing.PLUGIN.settings} == {}


def test_safe_browsing_connection_test_reports_ok_and_failures(store, monkeypatch):
    # given
    cases = {'clean': matching(), 'flagged': matching('https://www.example.com/'), 'refused': FakeHttp({('POST', API): HttpError(401, API)}),
             'denied': FakeHttp({('POST', API): HttpError(403, API)}),
             'unreachable': FakeHttp({('POST', API): remote.Unreachable('down')}), 'bad key': FakeHttp({('POST', API): HttpError(400, API, f'API key not valid {KEY}')})}
    page = pages({'www.example.com': status('www.example.com')})
    pushed_page = pages({'www.example.com': HttpError(429, STATUS)})

    # when
    got = {name: tool_runtime(store, http, instances={}).connection_test(safe_browsing.PLUGIN, None, dict(VALUES), 5, NOW) for name, http in cases.items()}
    free = tool_runtime(store, page, instances={}).connection_test(safe_browsing.PLUGIN, None, dict(FREE), 5, NOW)
    pushed = tool_runtime(store, pushed_page, instances={}).connection_test(safe_browsing.PLUGIN, None, dict(FREE), 5, NOW)

    # then
    assert (got['clean']['ok'], got['clean']['summary']) == (True, f'Safe Browsing{DOT}www.example.com clean')
    assert (got['flagged']['ok'], got['flagged']['summary']) == (True, f'Safe Browsing{DOT}www.example.com flagged')
    assert (got['refused']['ok'], got['refused']['summary']) == (False, msg('connection.rejected', status=401))
    assert (got['denied']['ok'], got['denied']['summary']) == (False, f'{NO_ACCESS} (HTTP 403)')
    assert (got['unreachable']['ok'], got['unreachable']['summary']) == (False, msg('connection.unreachable'))
    assert (got['bad key']['ok'], got['bad key']['summary']) == (False, 'Safe Browsing API HTTP 400 (key refused or API not enabled)')
    assert (free['ok'], free['summary']) == (True, f'Safe Browsing{DOT}www.example.com clean') and len(page.calls) == 1
    assert page.timeouts[0] <= safe_browsing.TEST_BUDGET_S < 15
    assert (pushed['ok'], pushed['summary']) == (False, 'www.example.com: HTTP 429 (rate limited?)')
    sent = cases['clean'].calls
    assert len(sent) == 1 and [e['url'] for e in sent[0][3]['threatInfo']['threatEntries']] == ['https://www.example.com/']
    assert KEY not in json.dumps(got)


def test_safe_browsing_second_instance_is_scoped(store: Store, tmp_path):
    # given
    http = matching('https://east.example.com/', kind='MALWARE')

    # when
    with app_with(store, tmp_path, http, instances={'main': {'api-key': KEY, 'hosts': 'www.example.com'}, 'east': {'api-key': KEY, 'hosts': 'east.example.com'}}) as c:
        c.app.state.sweeper.run('safe-browsing', 'safebrowsing', NOW)
    state = store.load()
    [east] = [a for a in state['anomalies'] if a['svc'] == 'east.example.com']

    # then
    assert (east['cluster'], east['ranKey'], east['status'], east['sev']) == ('east', 'safebrowsing:east.example.com-east', 'open', 'crit')
    assert state['sources']['safebrowsing-east'] == 'ok' and state['sources']['safebrowsing'] == 'ok'
    assert sorted((r.get('cluster') or '', r['part'], r.get('host', '')) for r in state['safeBrowsing']) == [
        ('', 'host', 'www.example.com'), ('', 'summary', ''), ('east', 'host', 'east.example.com'), ('east', 'summary', '')]


def test_the_app_runs_the_safe_browsing_check_once_an_instance_serves(store: Store, tmp_path):
    # given
    http = matching('https://login.example.com/')

    # when
    with app_with(store, tmp_path, http) as c:
        jobs = [j.name for j in c.app.state.scheduler.jobs]
        c.app.state.sweeper.run('safe-browsing', 'safebrowsing', NOW)
        tools = [d['name'] for d in PluginTools(c.app.state.runtime).definitions()]
    state = store.load()

    # then
    assert 'safe-browsing.safebrowsing' in jobs and 'safebrowsing' in tools
    card = by_key(state)['safebrowsing|-/login.example.com|listed']
    assert card['status'] == 'open' and card['links'][0]['label'] == 'Safe Browsing site status' and state['sources']['safebrowsing'] == 'ok'


def test_a_pass_cut_by_its_time_budget_keeps_the_read_form(monkeypatch):
    # given
    no_pause(monkeypatch)
    sb = engine.SafeBrowsing(('www.example.com', 'shop.example.com'), '', pages({'www.example.com': status('www.example.com'), 'shop.example.com': status('shop.example.com')}),
                             lambda s: None, iter([0.0, 0.0, 300.0, 300.0, 300.0]).__next__, 200)

    # when
    out = engine.run_pass(sb, NOW, []).result

    # then
    assert out.ok and out.status == f'ok{DOT}read 1 of 2 hosts'


def test_the_status_page_read_has_a_body_limit():
    # given
    sent = []

    def capture(method, url, headers, data, timeout):
        sent.append(headers)
        return remote.Reply(status('www.example.com'), {})

    # when
    engine.SafeBrowsing(('www.example.com',), '', capture).lookup('www.example.com')

    # then
    assert sent[0][remote.TEXT_READ] == str(engine.STATUS_LIMIT) and engine.STATUS_LIMIT == 256 * 1024
