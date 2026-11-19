import json

from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.engine import iso
from skopax.kit.elastic import ElasticError
from skopax.messages import msg
from skopax.plugins import csp, elasticsearch
from skopax.plugins.csp import checks
from skopax.plugins.csp.checks import PASS, RULES, evaluate, host_of, query, run_pass
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.sealer import master_key
from skopax.store import Store
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance
from tests.test_elasticsearch_plugin import ES_URL, KIBANA, QUERY, USER, card_map, es_error, esql, link_params, missing_index

NOW = 1_794_133_200.0
EAST = 'https://east.example.com'
BASE = 'FROM web-logs-* | WHERE message LIKE "*csp-report*"'
TARGET = checks.Target(BASE, 'shop', 'storefront', {})
KEY = 'key-0042-secret'
PRIVILEGES = f'{ES_URL}/_security/user/_has_privileges'
DEFAULT_RULES = {'new_min': 10, 'jump': 5.0, 'jump_min': 100, 'new_days': 7, 'quiet_hours': 24, 'forget_days': 30, 'history_hours': 168, 'min_history': 24}


class FakeEs:
    def __init__(self, week: list[dict], hour: list[dict], fail: bool = False):
        self.week = week
        self.hour = hour
        self.fail = fail
        self.asked: list[str] = []

    def query(self, esql: str, timeout: float | None = None) -> list[dict]:
        self.asked.append(esql)
        if self.fail:
            raise ElasticError('ES|QL HTTP 504', 'gateway timeout', 504)
        return self.hour if 'NOW() - 60 minutes' in esql else self.week

    def unreadable(self, indices: tuple[str, ...], timeout: float | None = None) -> list[str]:
        return []


def row(directive: str, blocked: str, c: int) -> dict:
    return {'directive': directive, 'blocked': blocked, 'c': c, 'newest': iso(NOW - 600)}


def previous(hosts: dict | None = None, history: list | None = None) -> list[dict]:
    summary = {'part': 'summary'}
    if hosts is not None:
        summary['hosts'] = [{'name': k, **v} for k, v in hosts.items()]
    if history is not None:
        summary['history'] = history
    return [summary]


def summary_of(rows: list[dict]) -> dict:
    [found] = [r for r in rows if r['part'] == 'summary']
    return found


def hosts_of(rows: list[dict]) -> dict:
    return {h['name']: {k: v for k, v in h.items() if k != 'name'} for h in summary_of(rows)['hosts']}


def answer_of(week: list[dict], hour: list[dict] | dict):
    def answer(params, body):
        if 'NOW() - 60 minutes' in body['query']:
            return esql(hour['rows'] if isinstance(hour, dict) else hour)
        return esql(week)

    return answer


def app_with(store: Store, http, es: dict | None = None, **instances: dict) -> TestClient:
    key = master_key(store.db, settings())
    for index, (name, values) in enumerate((es or {'main': {}}).items()):
        add_instance(store, elasticsearch.PLUGIN, name, sealer_key=key, primary=index == 0, **({'url': ES_URL} | values))
    for index, (name, values) in enumerate(instances.items()):
        add_instance(store, csp.PLUGIN, name, sealer_key=key, primary=index == 0, **({'elasticsearch': 'main', 'query': BASE} | values))
    return TestClient(create_app(settings(), store, transport=http))


def open_card(**extra) -> dict:
    return {'key': 'csp|-/csp|script-src-elem cdn.example.org', 'src': 'csp', 'sig': 'csp', 'ns': '-', 'svc': 'csp', 'sub': 'script-src-elem cdn.example.org', 'sev': 'warn', 'status': 'open',
            'now': 12, 'label': 'CSP blocks a new host', 'what': 'x', 'short': 'WARN CSP blocks cdn.example.org', 'ranKey': 'csp', 'host': 'cdn.example.org',
            'firstSeen': iso(NOW - 3600), 'lastSeen': iso(NOW - 900), **extra}


def test_a_blocked_url_is_reduced_to_its_host():
    # then
    assert [host_of(b) for b in ('https://frames.example.net', 'https://x.example.com:443/a/b.js?v=1', 'data', '', 'wss://example.com/socket')] == [
        'frames.example.net', 'x.example.com', 'data', 'inline', 'example.com']


def test_the_query_covers_both_report_shapes_and_skips_browser_extensions():
    # when
    q = query(BASE, 168)

    # then
    for field in ('blockedURL', 'blocked-uri', 'effectiveDirective', 'effective-directive', 'sourceFile', 'source-file'):
        assert f'%{{}}{field}\\":\\"' in q
    assert 'NOT (blocked LIKE "*-extension*" OR source LIKE "*-extension*")' in q
    assert q.startswith(BASE + '\n| WHERE @timestamp >= NOW() - 10080 minutes\n') and q.endswith('| SORT c DESC\n| LIMIT 1000')


def test_the_first_run_learns_the_week_and_only_a_host_new_to_it_becomes_a_card():
    # given
    es = FakeEs([row('frame-src', 'https://frames.example.net', 427)],
                [row('frame-src', 'https://frames.example.net', 30), row('script-src-elem', 'https://cdn.example.org/sdk.js', 12),
                 row('script-src-elem', 'chrome-extension://abc/x.js', 50), row('connect-src', 'https://rare.example/x', 3)])

    # when
    out = run_pass(es, NOW, None, dict(RULES), TARGET)

    # then
    result = out.result
    assert result.ok and result.ran == (PASS, 'csp:fields', 'csp:access:reports')
    assert [x['key'] for x in result.found] == ['csp|shop/storefront|script-src-elem cdn.example.org']
    card = result.found[0]
    assert (card['sev'], card['now'], card['host']) == ('warn', 12, 'cdn.example.org') and 'had not shown before' in card['what']
    hosts = hosts_of(out.sections['csp'])
    assert set(hosts) == {'frame-src frames.example.net', 'script-src-elem cdn.example.org', 'connect-src rare.example'}
    assert len(es.asked) == 2 and 'NOW() - 10080 minutes' in es.asked[0]


def test_a_host_stays_new_for_a_week_and_the_week_is_not_queried_again():
    # given
    prev = previous({'script-src-elem cdn.example.org': {'first': iso(NOW - 3 * 86400), 'last': iso(NOW - 1800)},
                     'frame-src frames.example.net': {'first': iso(NOW - 8 * 86400), 'last': iso(NOW - 1800)}}, [])
    es = FakeEs([], [row('script-src-elem', 'https://cdn.example.org/sdk.js', 40), row('frame-src', 'https://frames.example.net', 40)])

    # when
    out = run_pass(es, NOW, prev, dict(RULES), TARGET)

    # then
    assert [x['host'] for x in out.result.found] == ['cdn.example.org'] and len(es.asked) == 1


def test_a_jump_in_all_reports_needs_a_day_of_history_and_five_times_the_week():
    # given
    hosts = {'frame-src frames.example.net': {'first': iso(NOW - 9 * 86400), 'last': iso(NOW - 1800)}}
    history = [{'at': iso(NOW - i * 1800), 'c': 2} for i in range(1, 49)]
    es = FakeEs([], [row('frame-src', 'https://frames.example.net', 160)])

    # when
    jump = run_pass(es, NOW, previous(hosts, history), dict(RULES), TARGET)
    young = run_pass(es, NOW, previous(hosts, history[:10]), dict(RULES), TARGET)
    unkept = run_pass(es, NOW, previous(hosts, history), {**RULES, 'history_hours': 0}, TARGET)

    # then
    assert [x['key'] for x in jump.result.found] == ['csp|shop/storefront|reports']
    assert 'jumped to 160' in jump.result.found[0]['what'] and 'frames.example.net (frame-src) 160' in jump.result.found[0]['what']
    assert young.result.found == [] and summary_of(jump.sections['csp'])['history'][-1] == {'at': iso(NOW), 'c': 160}
    assert summary_of(unkept.sections['csp'])['history'] == [{'at': iso(NOW), 'c': 160}]


def test_a_failed_query_reports_the_pass_down_without_clearing_cards():
    # when
    out = run_pass(FakeEs([], [], fail=True), NOW, previous({}), dict(RULES), TARGET)

    # then
    assert (out.result.ok, out.result.status) == (False, 'error: ES|QL HTTP 504') and out.sections == {}


def test_a_flagged_host_keeps_its_card_past_the_week_until_a_quiet_day():
    # given
    name = 'script-src-elem cdn.example.org'
    old = {name: {'first': iso(NOW - 9 * 86400), 'last': iso(NOW - 3600), 'flagged': iso(NOW - 9 * 86400)}}
    quiet = {name: {'first': iso(NOW - 9 * 86400), 'last': iso(NOW - 25 * 3600), 'flagged': iso(NOW - 9 * 86400)}}

    # when
    still = run_pass(FakeEs([], [row('script-src-elem', 'https://cdn.example.org/sdk.js', 4)]), NOW, previous(old), dict(RULES), TARGET)
    night = run_pass(FakeEs([], []), NOW, previous(old), dict(RULES), TARGET)
    over = run_pass(FakeEs([], []), NOW, previous(quiet), dict(RULES), TARGET)

    # then
    assert [x['host'] for x in still.result.found] == ['cdn.example.org'] and still.result.found[0]['now'] == 4
    assert [x['now'] for x in night.result.found] == [0]
    assert over.result.found == [] and 'flagged' not in hosts_of(over.sections['csp'])[name]
    assert hosts_of(still.sections['csp'])[name]['flagged'] == iso(NOW - 9 * 86400)


def test_a_zero_history_rule_with_no_history_yet_does_not_divide_by_zero():
    # when
    found, flagged = evaluate({}, {}, [], NOW, {**RULES, 'min_history': 0})

    # then
    assert (found, flagged) == ([], set())


def test_csp_identity_and_manifest():
    # given
    registry = Registry.of(elasticsearch.PLUGIN, csp.PLUGIN)

    # when
    loaded = registry.get('csp')
    manifest = describe(csp.PLUGIN, icon=True)

    # then
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.csp' in BUILTIN
    assert Registry.load({}, entry_points=False).get('csp').state == 'ok'
    assert json.loads(json.dumps(manifest)) == manifest
    assert (manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'CSP reports', '0.1.0', 'web', 'Skopax', 'Apache-2.0', '>=0.1,<0.3', 'community')
    assert manifest['capabilities'] == {'hosts': [], 'secrets': [], 'agentReads': False}
    assert manifest['uses'] == ['elasticsearch'] and manifest['description'].endswith('.')
    assert manifest['checks'] == [{'id': 'csp', 'label': manifest['checks'][0]['label'], 'period': 900, 'offset': 0, 'ran': ['csp']}]
    assert [(c.sections, c.tool) for c in csp.PLUGIN.checks] == [(('csp',), 'esql')]
    assert [(r['id'], r['defaults']) for r in manifest['rules']] == [('rules', DEFAULT_RULES)]
    assert manifest['tools'] == [] and manifest['panels'] == [] and registry.icon('csp').startswith('<svg')


def test_csp_defaults_are_neutral():
    # when
    defaults = {f.key: f.default for f in csp.PLUGIN.fields}
    kinds = {f.key: f.kind for f in csp.PLUGIN.fields}

    # then
    assert defaults == {'elasticsearch': '', 'query': '', 'namespace': '', 'service': 'csp'}
    assert kinds == {'elasticsearch': 'instance', 'query': 'text', 'namespace': 'text', 'service': 'text'}
    assert [f.key for f in csp.PLUGIN.fields if f.required] == ['elasticsearch', 'query']
    assert csp.PLUGIN.settings == () and {r.id: dict(r.defaults) for r in csp.PLUGIN.rules} == {'rules': DEFAULT_RULES}
    assert checks.target_of({'query': f'  {BASE}  '}, {}) == checks.Target(BASE, '-', 'csp', {})


def test_csp_connection_test_reports_ok_and_failures(store: Store):
    # given
    good = FakeHttp({('POST', QUERY): esql([{'directive': 'frame-src', 'blocked': 'https://frames.example.net', 'c': 30, 'newest': iso(NOW)},
                                            {'directive': 'script-src', 'blocked': 'https://cdn.example.org/a.js', 'c': 4, 'newest': iso(NOW)}])})
    denied = FakeHttp({('POST', QUERY): HttpError(401, QUERY, es_error('security_exception', f'unable to authenticate with API key [{KEY}]'))})
    down = FakeHttp({('POST', QUERY): Unreachable(f'{QUERY} unreachable (TimeoutError)')})
    no_message = FakeHttp({('POST', QUERY): HttpError(400, QUERY, es_error('verification_exception', f'Found 2 problems\nline 3:10: Unknown column [message]\nline 3:20: Unknown column [{KEY}]'))})
    bad_query = FakeHttp({('POST', QUERY): HttpError(400, QUERY, es_error('parsing_exception', f"line 1:6: mismatched input 'WHERE' near {KEY}"))})
    unread = FakeHttp({('POST', QUERY): HttpError(403, QUERY, es_error('security_exception', f'action [indices:data/read/esql] is unauthorized for API key id [{KEY}]'))})
    hidden = FakeHttp({('POST', QUERY): missing_index('web-logs-*'), ('POST', PRIVILEGES): {'has_all_requested': False, 'index': {'web-logs-*': {'read': False}}}})
    absent = FakeHttp({('POST', QUERY): missing_index('web-logs-*'), ('POST', PRIVILEGES): {'has_all_requested': True, 'index': {'web-logs-*': {'read': True}}}})

    def check(http, es=None, **values):
        with app_with(store, http, es=es, main=values) as c:
            runtime = c.app.state.runtime
            return csp.test(runtime.context(csp.PLUGIN, runtime.instances.get('csp', 'main'), NOW))

    # when
    keyed = {'main': {'api-key': KEY}}
    ok = check(good, es=keyed)
    refused, unreachable, missing, broken = check(denied, es=keyed), check(down, es=keyed), check(no_message, es=keyed), check(bad_query, es=keyed)
    no_read, unseen, nothing = check(unread, es=keyed), check(hidden, es=keyed), check(absent, es=keyed)
    off = check(good, es={'main': {'enabled': False}})

    # then
    assert (ok.ok, ok.summary) == (True, 'CSP reports: 34 in the last hour from 2 blocked hosts, through Elasticsearch main')
    assert (refused.ok, refused.summary) == (False, msg('connection.rejected', status=401))
    assert (unreachable.ok, unreachable.summary) == (False, msg('connection.unreachable'))
    assert (missing.ok, missing.summary) == (False, checks.NO_MESSAGE)
    assert not broken.ok and 'parsing_exception' in broken.summary and 'mismatched input' in broken.summary
    said = 'The API key lacks the read privilege on web-logs-*, which the CSP check reads.'
    assert (no_read.ok, no_read.summary) == (False, said) and (unseen.ok, unseen.summary) == (False, said)
    assert (nothing.ok, nothing.summary) == (False, 'No index matches web-logs-*.')
    assert (off.ok, off.summary) == (False, msg('peers.unavailable', plugin='Elasticsearch', instance='main'))
    sent = good.calls[0][3]['query']
    assert good.calls[0][2]['Authorization'] == f'ApiKey {KEY}' and sent.startswith(BASE + '\n| WHERE @timestamp >= NOW() - 60 minutes\n')
    assert not any(KEY in r.summary for r in (ok, refused, unreachable, missing, broken, no_read, unseen, nothing, off))


def test_csp_history_survives_between_runs(store: Store):
    # given
    hour = {'rows': [row('frame-src', 'https://frames.example.net', 5)]}
    http = FakeHttp({('POST', QUERY): answer_of([row('frame-src', 'https://frames.example.net', 400)], hour)})

    # when
    with app_with(store, http, main={}) as c:
        c.app.state.runtime.live.set('csp.rules', json.dumps({'min_history': 1, 'jump': 2, 'jump_min': 10}), USER)
        c.app.state.sweeper.run('csp', 'csp', NOW - 1800)
        first = card_map(store)
        hour['rows'] = [row('frame-src', 'https://frames.example.net', 100)]
        c.app.state.sweeper.run('csp', 'csp', NOW)
        state = store.load()
    second = card_map(store)
    summary = summary_of(state['csp'])

    # then
    assert first == {} and set(second) == {'csp|-/csp|reports'}
    assert 'average of 5 an hour' in second['csp|-/csp|reports']['what']
    assert len(http.calls) == 3 and 'NOW() - 10080 minutes' in http.calls[0][3]['query'] and all('NOW() - 60 minutes' in c[3]['query'] for c in http.calls[1:])
    assert summary['history'] == [{'at': iso(NOW - 1800), 'c': 5}, {'at': iso(NOW), 'c': 100}]
    assert [h['name'] for h in summary['hosts']] == ['frame-src frames.example.net'] and summary['plugin'] == 'csp' and 'cluster' not in summary
    assert [(r['host'], r['c']) for r in state['csp'] if r['part'] == 'top'] == [('frames.example.net', 100)]


def test_csp_without_its_index_is_ok_and_quiet_and_keeps_its_history(store: Store):
    # given
    stored = [{'part': 'summary', 'plugin': 'csp', 'at': iso(NOW - 900), 'total': 3, 'hosts': [{'name': 'frame-src frames.example.net', 'first': iso(NOW - 86400), 'last': iso(NOW - 900)}],
               'history': [{'at': iso(NOW - 900), 'c': 3}]}]
    store.save_state({'anomalies': [open_card()], 'initialized': True, 'csp': stored}, None)
    http = FakeHttp({('POST', QUERY): missing_index('web-logs-*')})

    # when
    with app_with(store, http, main={}) as c:
        c.app.state.sweeper.run('csp', 'csp', NOW)
        state = store.load()

    # then
    assert state['serverPasses']['csp']['status'] == 'ok \u00b7 no data'
    assert card_map(store)['csp|-/csp|script-src-elem cdn.example.org']['status'] == 'open'
    assert state['csp'] == stored


def test_a_query_without_a_message_column_raises_a_fields_card(store: Store):
    # given
    stored = [{'part': 'summary', 'plugin': 'csp', 'at': iso(NOW - 900), 'total': 3, 'hosts': [], 'history': [{'at': iso(NOW - 900), 'c': 3}]}]
    http = FakeHttp({('POST', QUERY): HttpError(400, QUERY, es_error('verification_exception', 'Found 1 problem\nline 3:10: Unknown column [message]'))})
    store.save_state({'anomalies': [open_card()], 'initialized': True, 'csp': stored}, None)

    # when
    with app_with(store, http, main={}) as c:
        c.app.state.sweeper.run('csp', 'csp', NOW)
        state = store.load()
    cards = card_map(store)

    # then
    assert state['serverPasses']['csp']['status'] == 'ok \u00b7 no data'
    assert cards['csp|-/csp|script-src-elem cdn.example.org']['status'] == 'open'
    card = cards['csp-fields|-/csp|fields']
    assert (card['label'], card['ranKey'], card['sev']) == ('CSP report logs lack the field message', 'csp:fields', 'warn')
    assert state['csp'] == stored


def test_csp_refused_for_permission_is_never_no_data(store: Store):
    # given
    cases = {'refused': FakeHttp({('POST', QUERY): HttpError(403, QUERY, es_error('security_exception', f'action [indices:data/read/esql] is unauthorized for API key id [{KEY}]'))}),
             'hidden': FakeHttp({('POST', QUERY): missing_index('web-logs-*'), ('POST', PRIVILEGES): {'has_all_requested': False, 'index': {'web-logs-*': {'read': False}}}})}
    stored = [{'part': 'summary', 'plugin': 'csp', 'at': iso(NOW - 900), 'total': 3, 'hosts': [], 'history': [{'at': iso(NOW - 900), 'c': 3}]}]

    for name, http in cases.items():
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [open_card()], 'initialized': True, 'csp': stored}, None)

        # when
        with app_with(store, http, es={'main': {'api-key': KEY}}, main={}) as c:
            c.app.state.sweeper.run('csp', 'csp', NOW)
            state = store.load()
            cards = card_map(store)
            http.routes[('POST', QUERY)] = answer_of([], [])
            c.app.state.sweeper.run('csp', 'csp', NOW + 900)
        after = card_map(store)

        # then
        assert state['serverPasses']['csp']['status'] == 'ok \u00b7 no access to reports', name
        assert cards['csp|-/csp|script-src-elem cdn.example.org']['status'] == 'open' and state['csp'] == stored, name
        [access] = [a for a in cards.values() if a['sig'] == 'no-access']
        assert (access['ranKey'], access['sev']) == ('csp:access:reports', 'warn'), name
        assert access['label'] == 'CSP reports cannot read reports: the API key lacks the read privilege on web-logs-*', name
        assert KEY not in access['what'] + access['label'], name
        assert after[access['key']]['status'] == 'cleared', name


def test_the_csp_history_keeps_the_hours_it_claims():
    # given
    hosts = {'frame-src frames.example.net': {'first': iso(NOW - 9 * 86400), 'last': iso(NOW - 1800)}}
    history = [{'at': iso(NOW - i * 900), 'c': 2} for i in range(800, 0, -1)]
    es = FakeEs([], [row('frame-src', 'https://frames.example.net', 3)])

    # when
    out = run_pass(es, NOW, previous(hosts, history), dict(RULES), TARGET)

    # then
    kept = summary_of(out.sections['csp'])['history']
    assert len(kept) == 673 and kept[0] == {'at': iso(NOW - 672 * 900), 'c': 2} and kept[-1] == {'at': iso(NOW), 'c': 3}


def test_csp_follows_its_elasticsearch_instance(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of([], [])})
    store.save_state({'anomalies': [open_card()], 'initialized': True}, None)

    # when
    with app_with(store, http, es={'main': {'enabled': False}}, main={}) as c:
        c.app.state.sweeper.run('csp', 'csp', NOW)
        state = store.load()

    # then
    assert state['serverPasses']['csp']['status'] == 'error: ' + msg('peers.unavailable', plugin='Elasticsearch', instance='main')
    assert card_map(store)['csp|-/csp|script-src-elem cdn.example.org']['status'] == 'open' and http.calls == []


def test_csp_links_are_the_card_urls_over_https_only(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of([], [row('script-src-elem', 'https://cdn.example.org/sdk.js', 12)])})

    # when
    with app_with(store, http, es={'main': {'api-key': 'lk', 'ui-url': KIBANA}}, main={}) as c:
        c.app.state.sweeper.run('csp', 'csp', NOW)
        info = c.app.state.runtime.instances.get('csp', 'main').info()
        card = card_map(store)['csp|-/csp|script-src-elem cdn.example.org']
        own = csp.links(card, info)
        forged = csp.links(dict(card, urls=[{'label': 'x', 'url': 'javascript:alert(1)'}]), info)

    # then
    assert [x['label'] for x in card['links']] == ['CSP reports in Kibana'] and own == card['links']
    shown = link_params(own[0]['url'])
    assert own[0]['url'].startswith(f'{KIBANA}/app/discover#/') and '(message:"blocked-uri" or message:"blockedURL") and message:"cdn.example.org"' in shown
    assert "from:'now-2h'" in shown and forged is None and 'cluster' not in card


def test_long_hosts_and_directives_are_cut_before_they_reach_a_card_key():
    # given
    blocked = 'https://' + 'a' * 300 + '.example.com/x.js'
    es = FakeEs([], [row('script-src-elem' + 'x' * 100, blocked, 40)])

    # when
    out = run_pass(es, NOW, previous({}), dict(RULES), TARGET)

    # then
    [card] = out.result.found
    assert len(card['host']) <= checks.HOST_MAX and len(card['directive']) <= checks.DIRECTIVE_MAX
    assert len(card['key']) <= len('csp|shop/storefront|') + checks.DIRECTIVE_MAX + 1 + checks.HOST_MAX


def test_csp_second_instance_is_scoped(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of([], []), ('POST', f'{EAST}/_query'): answer_of([], [row('script-src-elem', 'https://cdn.example.org/sdk.js', 12)])})

    # when
    with app_with(store, http, es={'main': {}, 'east': {'url': EAST}}, main={}, east={'elasticsearch': 'east'}) as c:
        c.app.state.sweeper.run('csp', 'csp', NOW)
        state = store.load()
    card = card_map(store)['csp|-/csp|script-src-elem cdn.example.org@east']

    # then
    assert (card['cluster'], card['ranKey']) == ('east', 'csp-east')
    assert set(state['serverPasses']) >= {'csp', 'csp-east'}
    assert sorted(r.get('cluster') or '' for r in state['csp'] if r['part'] == 'summary') == ['', 'east']


def test_csp_rules_come_from_the_rule_set(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of([], [row('script-src-elem', 'https://cdn.example.org/sdk.js', 12)])})

    # when
    with app_with(store, http, main={}) as c:
        c.app.state.sweeper.run('csp', 'csp', NOW)
        default = set(card_map(store))
        c.app.state.runtime.live.set('csp.rules', json.dumps({'new_min': 50}), USER)
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [], 'initialized': True}, None)
        c.app.state.sweeper.run('csp', 'csp', NOW)
        stricter = set(card_map(store))

    # then
    assert default == {'csp|-/csp|script-src-elem cdn.example.org'} and stricter == set()


def test_csp_ships_a_knowledge_file_like_its_varnish_sibling():
    # given
    from skopax.registry import describe

    # when
    manifest = describe(csp.PLUGIN, icon=True)

    # then
    assert manifest['knowledge'] == ['csp'] and len(csp.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert 'blocked-uri' in csp.PLUGIN.knowledge[0].text and 'esql' in csp.PLUGIN.knowledge[0].text
