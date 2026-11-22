import json
from pathlib import Path
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from skopax import remote
from skopax.app import create_app
from skopax.contract import InstanceInfo, ReadError
from skopax.engine import iso
from skopax.instances import Instances, InvalidInstance, PluginState, validate
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import crux
from skopax.plugins.crux import crux as engine
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

KEY = 'crux-key-9'
SEAL = 'crux-seal'
ORIGIN = 'https://www.example.com'
SHOP = 'https://shop.example.com'
EAST = 'https://east.example.com'
VALUES = {'api-key': KEY, 'origins': ORIGIN}
ALICE = 'alice@example.com'
HISTORY = engine.HISTORY_URL
ACCESS = 'crux:access:origins'
NO_ACCESS = 'Chrome UX Report cannot read origins: the API key lacks access to the Chrome UX Report API'
DOT = f' {chr(0xb7)} '
HINT = '(key refused, API not enabled or origin malformed)'


@dataclass
class Ctx:
    config: dict
    transport: object
    now: float = NOW
    changed: dict = field(default_factory=dict)

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo('main', True, None, self.config)

    def http(self):
        return self.transport

    def rules(self, set_id: str) -> dict:
        return dict(crux.PLUGIN.rules[0].defaults) | self.changed

    def open_cards(self, plugin_wide: bool = False) -> list[dict]:
        return []


def crux_record(inp: list, lcp: list | None = None) -> dict:
    metrics = {'interaction_to_next_paint': {'percentilesTimeseries': {'p75s': inp}}, 'cumulative_layout_shift': {'percentilesTimeseries': {'p75s': ['0.05', '0.04']}}}
    if lcp:
        metrics['largest_contentful_paint'] = {'percentilesTimeseries': {'p75s': lcp}}
    periods = [{'firstDate': {'year': 2026, 'month': 9, 'day': d}, 'lastDate': {'year': 2026, 'month': 10, 'day': d}} for d in range(25 - 7 * (len(inp) - 1), 26, 7)]
    return {'record': {'metrics': metrics, 'collectionPeriods': periods}}


def phone_only(inp: list):
    return lambda body: crux_record(inp) if body.get('formFactor') == 'PHONE' else crux_record([150, 150])


def by_origin(answers: dict):
    def answer(q, body):
        got = answers.get(body.get('origin'))
        return got(body) if callable(got) else got if got is not None else HttpError(404, HISTORY)
    return FakeHttp({('POST', HISTORY): answer})


def tool_runtime(store, http, instances: dict | None = None) -> Runtime:
    reg = Registry.of(crux.PLUGIN)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(SEAL)), http)
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, crux.PLUGIN, name, sealer_key=SEAL, primary=i == 0, **values)
    runtime.refresh()
    return runtime


def app_with(store, tmp_path, http, instances: dict | None = None):
    sealing = master_key(store.db, settings())
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, crux.PLUGIN, name, sealer_key=sealing, primary=i == 0, **values)
    return TestClient(create_app(settings(), store, registry=Registry.of(crux.PLUGIN), transport=http, kube=Kube(root=tmp_path)))


def by_key(state: dict) -> dict:
    return {a['key']: a for a in state['anomalies']}


def test_crux_cards_a_metric_that_moved_a_band_or_got_much_worse_and_keeps_the_trend():
    # given
    http = FakeHttp({('POST', HISTORY): lambda q, body: crux_record([230, 247, 310], [2400, 2600]) if body['formFactor'] == 'PHONE' else crux_record([150, 'NaN', 160])})

    # when
    sweep = crux.run(Ctx(VALUES, http))
    out = sweep.result

    # then
    assert out.ok and out.status == 'ok' and out.ran == ('crux', ACCESS)
    assert [c['key'] for c in out.found] == ['crux|-/www.example.com|mobile:inp', 'crux|-/www.example.com|mobile:lcp']
    inp = out.found[0]
    assert (inp['now'], inp['previous'], inp['sev']) == (310, 247, 'warn') and 'needs improvement' in inp['what'] and '28 days to 2026-10-25' in inp['what']
    desktop = next(r for r in sweep.sections['crux'] if r.get('formFactor') == 'DESKTOP')
    assert (desktop['part'], desktop['origin'], desktop['inp']) == ('trend', ORIGIN, [150, None, 160])
    assert sweep.sections['crux'][-1] == {'part': 'summary', 'at': iso(NOW)} and out.sections == {}
    method, url, _, body = http.calls[0]
    assert url.endswith('?key=crux-key-9') and body['collectionPeriodCount'] == 6 and body['origin'] == ORIGIN and body['formFactor'] == 'PHONE'


def test_crux_reports_an_invalid_key_without_the_key_and_needs_one():
    # given
    http = FakeHttp({('POST', HISTORY): HttpError(400, HISTORY, f'API key not valid {KEY}')})

    # when
    out = crux.run(Ctx(VALUES, http)).result
    with pytest.raises(InvalidInstance) as missing:
        validate(crux.PLUGIN, {'origins': ORIGIN})

    # then
    assert (out.ok, out.status) == (False, f'error: CrUX HTTP 400 {HINT}: https://www.example.com PHONE')
    assert missing.value.field == 'api-key'


@pytest.mark.parametrize('code', [401, 403])
def test_a_key_without_access_raises_an_access_card_and_closes_no_page_speed_card(code):
    # given
    http = FakeHttp({('POST', HISTORY): HttpError(code, HISTORY, f'PERMISSION_DENIED for {KEY}')})

    # when
    sweep = crux.run(Ctx({'api-key': KEY, 'origins': f'{ORIGIN}, {SHOP}'}, http))
    out = sweep.result

    # then
    assert out.ok and out.status == 'ok \u00b7 no access to origins' and out.ran == (ACCESS,) and len(http.calls) == 1
    [card] = out.found
    assert (card['key'], card['ranKey'], card['sev'], card['label'], card['src']) == ('access|-/crux|origins', ACCESS, 'warn', NO_ACCESS, 'crux')
    assert f'HTTP {code}' in card['what'] and KEY not in json.dumps(card) and sweep.sections == {}


def test_a_failed_pass_keeps_its_hint_inside_the_stored_status(store: Store, tmp_path):
    # given
    http = FakeHttp({('POST', HISTORY): HttpError(400, HISTORY, f'API key not valid {KEY}')})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('crux', 'crux', NOW)
    stored = store.load()['sources']['crux']

    # then
    assert stored == f'error: CrUX HTTP 400 {HINT}: https://www.example.com PHONE'


def test_origins_are_normalised_and_a_malformed_one_fails_the_pass_and_the_test(store):
    # given
    http = FakeHttp({('POST', HISTORY): lambda q, body: crux_record([230, 230])})
    bad = {'api-key': KEY, 'origins': 'ftp://files.example.com, https://www.example.com'}
    said = 'origin ftp://files.example.com is not an http(s) origin like https://www.example.com'

    # when
    loose = crux.run(Ctx({'api-key': KEY, 'origins': 'www.Example.com/pricing'}, http)).result
    refused = crux.run(Ctx(bad, FakeHttp())).result
    tested = tool_runtime(store, FakeHttp(), instances={}).connection_test(crux.PLUGIN, None, dict(bad), 5, NOW)

    # then
    assert engine.origins_of('www.Example.com, https://Shop.example.com/path?q=1, http://old.example.com:8080/') == (
        ('https://www.example.com', 'https://shop.example.com', 'http://old.example.com:8080'), ())
    assert engine.origins_of('ftp://files.example.com, https://, https://[broken, https://www.example.com, https://www.example.com/') == (
        ('https://www.example.com',), ('ftp://files.example.com', 'https://', 'https://[broken'))
    assert loose.ok and {c[3]['origin'] for c in http.calls} == {'https://www.example.com'}
    assert (refused.ok, refused.status) == (False, f'error: {said}')
    assert (tested['ok'], tested['summary']) == (False, said)


def test_an_unreachable_api_fails_the_pass_with_a_fixed_text():
    # given
    http = FakeHttp({('POST', HISTORY): remote.Unreachable(f'{HISTORY} unreachable (ConnectionRefusedError) key={KEY}')})

    # when
    out = crux.run(Ctx(VALUES, http)).result

    # then
    assert (out.ok, out.status) == (False, 'error: CrUX: the Chrome UX Report API could not be reached')


def test_an_origin_without_data_is_a_quiet_part():
    # given
    some = by_origin({ORIGIN: phone_only([230, 247, 310])})
    none = by_origin({})
    origins = {'api-key': KEY, 'origins': f'{ORIGIN}, {SHOP}/'}

    # when
    partly = crux.run(Ctx(origins, some))
    quiet = crux.run(Ctx(origins, none))

    # then
    assert partly.result.ok and partly.result.status == 'ok \u00b7 no data for https://shop.example.com' and partly.result.ran == ('crux', ACCESS)
    assert [c['key'] for c in partly.result.found] == ['crux|-/www.example.com|mobile:inp']
    assert quiet.result.ok and quiet.result.status == 'ok \u00b7 no data' and quiet.result.ran == ('crux:no-data', ACCESS) and quiet.result.found == []
    assert [(r['origin'], r['formFactor'], r['inp']) for r in quiet.sections['crux'] if r['part'] == 'trend'] == [(ORIGIN, 'PHONE', []), (ORIGIN, 'DESKTOP', []), (SHOP, 'PHONE', []), (SHOP, 'DESKTOP', [])]


def test_a_pass_that_outruns_its_budget_keeps_whole_origins_and_closes_nothing(monkeypatch):
    # given
    clock = [1000.0]
    monkeypatch.setattr(engine, 'monotonic', lambda: clock[0])

    def slow(q, body):
        clock[0] += 70
        return phone_only([230, 247, 310])(body)

    http = FakeHttp({('POST', HISTORY): slow})
    origins = {'api-key': KEY, 'origins': f'{ORIGIN}, {SHOP}, {EAST}'}

    # when
    sweep = crux.run(Ctx(origins, http))

    # then
    assert sweep.result.ok and sweep.result.status == 'ok \u00b7 read 1 of 3 origins' and sweep.result.ran == ('crux:no-data', ACCESS)
    assert [c['key'] for c in sweep.result.found] == ['crux|-/www.example.com|mobile:inp']
    assert len(http.calls) == 3 and {r.get('origin') for r in sweep.sections['crux']} == {ORIGIN, None}


def test_the_rules_set_how_much_worse_counts():
    # given
    http = FakeHttp({('POST', HISTORY): lambda q, body: crux_record([230, 247, 290]) if body['formFactor'] == 'PHONE' else crux_record([150, 150])})

    # when
    default = crux.run(Ctx(VALUES, http)).result
    stricter = crux.run(Ctx(VALUES, http, changed={'worse': 0.1})).result

    # then
    assert default.found == [] and [c['key'] for c in stricter.found] == ['crux|-/www.example.com|mobile:inp']


def test_the_crux_tool_reads_an_origin_or_one_url_on_the_configured_origins_only(store: Store):
    # given
    http = FakeHttp({('POST', HISTORY): lambda q, body: crux_record([230, 247, 310], [2400, 2600]) if 'url' not in body else HttpError(404, HISTORY)})
    b = box(store, plugin_tools=PluginTools(tool_runtime(store, http)))

    # when
    origin = json.loads(b.call('crux', {'form': 'ALL', 'periods': 3})[0])
    page = json.loads(b.call('crux', {'url': 'https://www.example.com/pricing'})[0])
    foreign = b.call('crux', {'url': 'https://elsewhere.example.org/'})
    plain = b.call('crux', {'url': 'http://www.example.com/pricing'})

    # then
    assert origin['latest']['inp'] == {'p75': '310 ms', 'band': 'needs improvement', 'good': '200 ms', 'poor': 'above 500 ms'} and origin['p75']['lcp'] == [2400, 2600]
    assert 'formFactor' not in http.calls[0][3] and http.calls[0][3]['collectionPeriodCount'] == 3 and http.calls[0][3]['origin'] == ORIGIN
    assert page['note'].startswith('CrUX has no data for it') and http.calls[1][3]['url'] == 'https://www.example.com/pricing'
    assert foreign == (engine.OFF_ORIGIN, True) and plain == foreign
    assert len(http.calls) == 2 and crux.PLUGIN.tools[0].per_run == 4


@pytest.mark.parametrize('args, why', [
    ({'url': f'{ORIGIN}/a', 'origin': ORIGIN}, 'give url or origin, not both'),
    ({'form': 'TABLET'}, 'form must be one of PHONE, DESKTOP, ALL'),
    ({'periods': 40}, 'periods must be 1-25'),
    ({'periods': 'many'}, 'periods must be 1-25'),
    ({'origin': 'https://[broken'}, engine.OFF_ORIGIN),
])
def test_bad_tool_inputs_are_refused_before_any_call(args, why):
    # given
    http = FakeHttp()

    # when
    with pytest.raises(ReadError) as refused:
        crux.crux_tool(Ctx(VALUES, http), args)

    # then
    assert (refused.value.status, refused.value.message) == (400, why) and http.calls == []


def test_a_failed_tool_read_names_the_code_or_the_missing_access_without_the_key():
    # given
    bad = FakeHttp({('POST', HISTORY): HttpError(400, HISTORY, f'API key not valid: {KEY}')})
    denied = FakeHttp({('POST', HISTORY): HttpError(403, HISTORY, f'PERMISSION_DENIED: {KEY}')})

    # when
    with pytest.raises(ReadError) as invalid:
        crux.crux_tool(Ctx(VALUES, bad), {})
    with pytest.raises(ReadError) as refused:
        crux.crux_tool(Ctx(VALUES, denied), {})

    # then
    assert invalid.value.message == f'CrUX HTTP 400 {HINT}: https://www.example.com PHONE'
    assert (refused.value.status, refused.value.message) == (403, f'{NO_ACCESS} (HTTP 403)')


def test_crux_cards_link_the_trend_and_pagespeed():
    # given
    card = {'src': 'crux', 'svc': 'www.example.com', 'origin': ORIGIN, 'formFactor': 'DESKTOP'}
    info = InstanceInfo('main', True, None, VALUES)

    # when
    got = crux.links(card, info)

    # then
    assert got == [{'label': 'CrUX trend', 'url': 'https://cruxvis.withgoogle.com/#/?view=cwvsummary&url=https%3A%2F%2Fwww.example.com&identifier=origin&device=DESKTOP'},
                   {'label': 'PageSpeed Insights', 'url': 'https://pagespeed.web.dev/analysis?url=https%3A%2F%2Fwww.example.com%2F'}]
    assert crux.links({'src': 'prom', 'svc': 'checkout-service'}, info) is None


def test_crux_identity_and_manifest():
    # given
    registry = Registry.load({}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    got = registry.get('crux')
    manifest = describe(crux.PLUGIN, icon=True)
    check, tool, rules = crux.PLUGIN.checks[0], crux.PLUGIN.tools[0], crux.PLUGIN.rules[0]

    # then
    assert 'skopax.plugins.crux' in BUILTIN and got is not None and got.state == 'ok' and got.plugin is crux.PLUGIN
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'crux', 'Chrome UX Report', '0.1.0', 'web', 'Skopax', 'Apache-2.0', '>=0.1,<0.6', 'community')
    assert manifest['capabilities'] == {'hosts': ['chromeuxreport.googleapis.com'], 'secrets': ['api-key'], 'agentReads': False} and manifest['uses'] == []
    assert [(f.key, f.kind, f.required, f.ui) for f in crux.PLUGIN.fields] == [('api-key', 'secret', True, False), ('origins', 'text', True, False)]
    assert (check.id, check.period, check.offset, check.ran, check.sections, check.tool, check.once) == ('crux', 86400, 28800, ('crux',), ('crux',), 'crux', False)
    assert check.section_help['crux']
    assert (rules.id, dict(rules.defaults), set(rules.help) == set(rules.defaults)) == ('rules', {'worse': 0.2}, True)
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input, tool.input_schema['additionalProperties']) == ('crux', 'read', 4, True, 'instance', False)
    assert '{field:origins}' in tool.description
    assert manifest['panels'] == [] and registry.icon('crux').startswith('<svg')
    assert [k.id for k in crux.PLUGIN.knowledge] == ['crux'] and len(crux.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert crux.PLUGIN.client is None and crux.PLUGIN.probe_targets is None


def test_crux_defaults_are_neutral():
    # then
    assert {f.key: f.default for f in crux.PLUGIN.fields} == {'api-key': '', 'origins': ''}
    assert {s.key: s.default for s in crux.PLUGIN.settings} == {}
    assert dict(crux.PLUGIN.rules[0].defaults) == {'worse': 0.2}


def test_crux_connection_test_reports_ok_and_failures(store):
    # given
    cases = {'ok': FakeHttp({('POST', HISTORY): crux_record([230, 247])}), 'empty': FakeHttp({('POST', HISTORY): HttpError(404, HISTORY)}),
             'refused': FakeHttp({('POST', HISTORY): HttpError(401, HISTORY)}), 'denied': FakeHttp({('POST', HISTORY): HttpError(403, HISTORY)}),
             'unreachable': FakeHttp({('POST', HISTORY): remote.Unreachable('down')}),
             'bad key': FakeHttp({('POST', HISTORY): HttpError(400, HISTORY, f'API key not valid {KEY}')})}
    given = {'api-key': KEY, 'origins': f'{ORIGIN}, {SHOP}'}

    # when
    got = {name: tool_runtime(store, http, instances={}).connection_test(crux.PLUGIN, None, dict(given), 5, NOW) for name, http in cases.items()}

    # then
    assert (got['ok']['ok'], got['ok']['summary']) == (True, 'CrUX \u00b7 https://www.example.com')
    assert (got['empty']['ok'], got['empty']['summary']) == (True, 'CrUX \u00b7 https://www.example.com \u00b7 no data for this origin')
    assert (got['refused']['ok'], got['refused']['summary']) == (False, msg('connection.rejected', status=401))
    assert (got['denied']['ok'], got['denied']['summary']) == (False, f'{NO_ACCESS} (HTTP 403)')
    assert (got['unreachable']['ok'], got['unreachable']['summary']) == (False, msg('connection.unreachable'))
    assert (got['bad key']['ok'], got['bad key']['summary']) == (False, f'CrUX HTTP 400 {HINT}: https://www.example.com')
    asked = cases['ok'].calls
    assert len(asked) == 1 and asked[0][3]['origin'] == ORIGIN and asked[0][1].endswith(f'?key={KEY}')
    assert KEY not in json.dumps(got)


def test_crux_second_instance_is_scoped(store: Store, tmp_path):
    # given
    http = by_origin({ORIGIN: crux_record([230, 230]), EAST: phone_only([230, 247, 310])})

    # when
    with app_with(store, tmp_path, http, instances={'main': VALUES, 'east': {'api-key': KEY, 'origins': EAST}}) as c:
        c.app.state.sweeper.run('crux', 'crux', NOW)
    state = store.load()
    [east] = [a for a in state['anomalies'] if a['svc'] == 'east.example.com']

    # then
    assert (east['cluster'], east['ranKey'], east['status']) == ('east', 'crux-east', 'open')
    assert state['sources']['crux-east'] == 'ok' and state['sources']['crux'] == 'ok'
    assert east['links'][0]['url'].endswith('url=https%3A%2F%2Feast.example.com&identifier=origin&device=PHONE')
    assert sorted((r.get('cluster') or '', r['part'], r.get('origin', '')) for r in state['crux']) == [
        ('', 'summary', ''), ('', 'trend', ORIGIN), ('', 'trend', ORIGIN), ('east', 'summary', ''), ('east', 'trend', EAST), ('east', 'trend', EAST)]


def test_crux_rules_come_from_the_rule_set(store: Store, tmp_path):
    # given
    http = FakeHttp({('POST', HISTORY): lambda q, body: crux_record([230, 247, 290]) if body['formFactor'] == 'PHONE' else crux_record([150, 150])})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('crux', 'crux', NOW)
        before = set(by_key(store.load()))
        c.app.state.runtime.live.set('crux.rules', '{"worse": 0.1}', ALICE)
        c.app.state.sweeper.run('crux', 'crux', NOW)
        after = by_key(store.load())

    # then
    assert 'crux|-/www.example.com|mobile:inp' not in before
    assert after['crux|-/www.example.com|mobile:inp']['status'] == 'open' and after['crux|-/www.example.com|mobile:inp']['sev'] == 'warn'


def test_the_access_card_stays_until_the_key_reads_again_and_page_speed_cards_survive_it(store: Store, tmp_path):
    # given
    answers = {'code': 403}

    def answer(q, body):
        if answers['code']:
            return HttpError(answers['code'], HISTORY)
        return crux_record([230, 247, 310]) if body['formFactor'] == 'PHONE' else crux_record([150, 150])

    http = FakeHttp({('POST', HISTORY): answer})

    # when
    with app_with(store, tmp_path, http) as c:
        answers['code'] = 0
        c.app.state.sweeper.run('crux', 'crux', NOW)
        answers['code'] = 403
        c.app.state.sweeper.run('crux', 'crux', NOW + 60)
        refused = by_key(store.load())
        answers['code'] = 0
        c.app.state.sweeper.run('crux', 'crux', NOW + 120)
        back = by_key(store.load())

    # then
    assert refused['access|-/crux|origins']['status'] == 'open' and refused['crux|-/www.example.com|mobile:inp']['status'] == 'open'
    assert back['access|-/crux|origins']['status'] == 'cleared' and back['crux|-/www.example.com|mobile:inp']['status'] == 'open'
    assert store.load()['sources']['crux'] == 'ok'


def test_the_app_runs_the_crux_check_once_an_instance_serves(store: Store, tmp_path):
    # given
    http = FakeHttp({('POST', HISTORY): lambda q, body: crux_record([230, 247, 310]) if body['formFactor'] == 'PHONE' else crux_record([150, 150])})

    # when
    with app_with(store, tmp_path, http) as c:
        jobs = [j.name for j in c.app.state.scheduler.jobs]
        c.app.state.sweeper.run('crux', 'crux', NOW)
        tools = [d['name'] for d in PluginTools(c.app.state.runtime).definitions()]
    state = store.load()

    # then
    assert 'crux.crux' in jobs and 'crux' in tools
    assert by_key(state)['crux|-/www.example.com|mobile:inp']['status'] == 'open' and state['sources']['crux'] == 'ok'


def test_an_origin_drops_its_default_port_keeps_ipv6_brackets_and_an_http_only_origin_works_in_the_tool():
    # given
    http = FakeHttp({('POST', HISTORY): lambda q, body: crux_record([230, 247, 310], [2400, 2600])})
    values = VALUES | {'origins': 'http://old.example.com:80, https://www.example.com:443, https://[2001:db8::1]:8443'}

    # when
    found = engine.origin_list(values['origins'])
    plain = crux.crux_tool(Ctx(values, http), {'url': 'http://old.example.com/pricing'})
    other_port = engine.ours('https://www.example.com:8443/x', found)

    # then
    assert found == ('http://old.example.com', 'https://www.example.com', 'https://[2001:db8::1]:8443')
    assert plain['target'] == 'http://old.example.com/pricing' and http.calls[0][3]['url'] == 'http://old.example.com/pricing'
    assert other_port is False and engine.ours('https://WWW.example.com/', found) is True


def test_a_card_uses_an_before_a_metric_read_with_a_vowel_sound():
    # when
    lcp = engine.evaluate(ORIGIN, 'PHONE', {'periods': ['2026-10-18', '2026-10-25'], 'lcp': [2400, 4100]}, crux.PLUGIN.rules[0].defaults)
    cls = engine.evaluate(ORIGIN, 'PHONE', {'periods': ['2026-10-18', '2026-10-25'], 'cls': [0.05, 0.3]}, crux.PLUGIN.rules[0].defaults)

    # then
    assert 'had an LCP p75' in lcp[0]['what'] and 'had a CLS p75' in cls[0]['what']


def test_the_crux_icon_is_told_apart_from_the_crashlytics_one():
    # given
    from skopax.plugins import crashlytics

    # when
    glyphs = [(Path(p.__file__).parent / 'icon.svg').read_text(encoding='utf-8').split('<g ', 1)[1] for p in (crux, crashlytics)]

    # then
    assert glyphs[0] != glyphs[1]
