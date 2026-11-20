import json
import re
import ssl

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.engine import iso
from skopax.instances import InvalidInstance
from skopax.kit.elastic import Elastic, ElasticError
from skopax.messages import msg
from skopax.plugins import elasticsearch, varnish
from skopax.plugins.varnish import checks
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.sealer import master_key
from skopax.store import Store
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance
from tests.test_elasticsearch_plugin import ES_URL, KIBANA, NOW, QUERY, USER, card_map, es_error, esql, link_params, missing_index

EAST = 'https://east.example.com'
PERSONAL = 'checkout|login|account'
DEFAULT_RULES = {'drop': 0.75, 'floor': 20, 'min_req': 3000, 'crit_share': 0.5, 'candidate_misses': 2000, 'candidate_share': 0.5}
PATHS = [{'path': '/push-sw.js', 'req': 1550, 'misses': 1550, 'rsc': 0, 'q': 1550, 'ok': 1200},
         {'path': '/garden/chairs', 'req': 1184, 'misses': 1006, 'rsc': 700, 'q': 1184, 'ok': 1184},
         {'path': '/checkout/cart/start', 'req': 1249, 'misses': 1249, 'rsc': 0, 'q': 1249, 'ok': 1240},
         {'path': '/meta.json', 'req': 203, 'misses': 203, 'rsc': 0, 'q': 200, 'ok': 0},
         {'path': '/sale/summer', 'req': 148, 'misses': 148, 'rsc': 0, 'q': 0, 'ok': 148},
         {'path': '/search/suggest', 'req': 1690, 'misses': 816, 'rsc': 300, 'q': 1690, 'ok': 1690},
         {'path': '/search/results', 'req': 407, 'misses': 391, 'rsc': 100, 'q': 407, 'ok': 407},
         {'path': '/products/gold', 'req': 3831, 'misses': 145, 'rsc': 0, 'q': 3000, 'ok': 3831}]
TARGET = checks.Target('varnish-*', 'shop', 'varnish', re.compile(PERSONAL, re.I), {})
KEY = 'key-0042-secret'
PRIVILEGES = f'{ES_URL}/_security/user/_has_privileges'
REQUIRED = ('varnish.method.keyword', 'varnish.cache_status.keyword', 'varnish.path.keyword')
OPTIONAL = ('varnish.query_params._rsc.keyword', 'varnish.query_string', 'varnish.response_code.keyword')


class FakeEs:
    def __init__(self, ratio: list[dict] | Exception, paths: list[dict] | Exception):
        self.ratio, self.paths = ratio, paths
        self.queries: list[str] = []

    def query(self, esql: str, timeout: float | None = None) -> list[dict]:
        self.queries.append(esql)
        got = self.ratio if 'BY w' in esql else self.paths
        if isinstance(got, Exception):
            raise got
        lacking = tuple(f for f in getattr(self, 'lacking', ()) if f in esql)
        if lacking:
            raise ElasticError('ES|QL HTTP 400', 'verification_exception', 400, unknown=lacking)
        return got

    def unreadable(self, indices: tuple[str, ...], timeout: float | None = None) -> list[str]:
        return []


def lacking(es: FakeEs, *fields: str) -> FakeEs:
    es.lacking = fields
    return es


def ratio(now: float, d1: float, d7: float, req: int = 30000) -> list[dict]:
    return [{'w': w, 'req': req, 'hits': round(req * p / 100)} for w, p in (('now', now), ('d1', d1), ('d7', d7))]


def answer_of(ratio_rows, paths_rows=PATHS):
    def answer(params, body):
        got = ratio_rows if 'BY w' in body['query'] else paths_rows
        return got if isinstance(got, Exception) else esql(got)

    return answer


def unknown(*columns: str) -> HttpError:
    return HttpError(400, QUERY, es_error('verification_exception', 'Found 2 problems\n' + '\n'.join(f'line 2:9: Unknown column [{c}]' for c in columns)))


def denied() -> HttpError:
    return HttpError(403, QUERY, es_error('security_exception', f'action [indices:data/read/esql] is unauthorized for API key id [{KEY}]'))


def app_with(store: Store, http, es: dict | None = None, **instances: dict) -> TestClient:
    key = master_key(store.db, settings())
    for index, (name, values) in enumerate((es or {'main': {}}).items()):
        add_instance(store, elasticsearch.PLUGIN, name, sealer_key=key, primary=index == 0, **({'url': ES_URL} | values))
    for index, (name, values) in enumerate(instances.items()):
        add_instance(store, varnish.PLUGIN, name, sealer_key=key, primary=index == 0, **({'elasticsearch': 'main'} | values))
    return TestClient(create_app(settings(), store, transport=http))


def open_card(**extra) -> dict:
    return {'key': 'varnish-hit|shop/varnish|ratio', 'src': 'varnish', 'sig': 'varnish-hit', 'ns': 'shop', 'svc': 'varnish', 'sub': 'ratio', 'sev': 'warn', 'status': 'open', 'now': 20.0,
            'label': 'Varnish hit ratio', 'what': 'x', 'short': 'WARN Varnish hit ratio 20 %', 'firstSeen': iso(NOW - 3600), 'lastSeen': iso(NOW - 900), **extra}


def summary_of(rows: list[dict]) -> dict:
    [found] = [r for r in rows if r['part'] == 'summary']
    return found


def test_varnish_identity_and_manifest():
    # given
    registry = Registry.of(elasticsearch.PLUGIN, varnish.PLUGIN)

    # when
    loaded = registry.get('varnish')
    manifest = describe(varnish.PLUGIN, icon=True)

    # then
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.varnish' in BUILTIN
    assert Registry.load({}, entry_points=False).get('varnish').state == 'ok'
    assert json.loads(json.dumps(manifest)) == manifest
    assert (manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'Varnish', '0.1.0', 'web', 'Skopax', 'Apache-2.0', '>=0.1,<0.5', 'community')
    assert manifest['capabilities'] == {'hosts': [], 'secrets': [], 'agentReads': False}
    assert manifest['uses'] == ['elasticsearch'] and manifest['description'].endswith('.')
    assert manifest['checks'] == [{'id': 'varnish', 'label': manifest['checks'][0]['label'], 'period': 900, 'offset': 720, 'ran': ['varnish']},
                                  {'id': 'varnish-cache', 'label': manifest['checks'][1]['label'], 'period': 86400, 'offset': 25200, 'ran': ['varnish-cache']}]
    assert [(c.sections, c.tool) for c in varnish.PLUGIN.checks] == [(('varnish',), 'esql'), (('varnishCache',), 'esql')]
    assert [(r['id'], r['defaults']) for r in manifest['rules']] == [('rules', DEFAULT_RULES)]
    assert manifest['tools'] == [] and manifest['panels'] == [] and manifest['knowledge'] == ['varnish']
    assert registry.icon('varnish').startswith('<svg') and len(varnish.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert all(field in varnish.PLUGIN.knowledge[0].text for field in checks.FIELDS)


def test_varnish_defaults_are_neutral():
    # when
    defaults = {f.key: f.default for f in varnish.PLUGIN.fields}
    kinds = {f.key: f.kind for f in varnish.PLUGIN.fields}

    # then
    assert defaults == {'elasticsearch': '', 'index': '', 'namespace': '', 'service': 'varnish', 'personal-paths': ''}
    assert kinds == {'elasticsearch': 'instance', 'index': 'text', 'namespace': 'text', 'service': 'text', 'personal-paths': 'regex'}
    assert [f.key for f in varnish.PLUGIN.fields if f.required] == ['elasticsearch'] and [f.choices for f in varnish.PLUGIN.fields if f.kind == 'instance'] == [('elasticsearch',)]
    assert varnish.PLUGIN.settings == () and {r.id: dict(r.defaults) for r in varnish.PLUGIN.rules} == {'rules': DEFAULT_RULES}
    assert checks.target_of({'elasticsearch': 'main'}, {'log-index': 'logs-*'}) == checks.Target('logs-*', '-', 'varnish', None, {'log-index': 'logs-*'})


def test_varnish_connection_test_reports_ok_and_failures(store: Store):
    # given
    good = FakeHttp({('POST', QUERY): esql([{'req': 12345, 'hits': 9876}])})
    rejected = FakeHttp({('POST', QUERY): HttpError(401, QUERY, es_error('security_exception', f'unable to authenticate with provided credentials and API key [{KEY}]'))})
    down = FakeHttp({('POST', QUERY): Unreachable(f'{QUERY} unreachable (TimeoutError)')})
    fieldless = FakeHttp({('POST', QUERY): unknown(KEY, 'varnish.method.keyword', 'varnish.cache_status.keyword')})
    unread = FakeHttp({('POST', QUERY): denied()})
    hidden = FakeHttp({('POST', QUERY): missing_index('varnish-*'), ('POST', PRIVILEGES): {'has_all_requested': False, 'index': {'varnish-*': {'read': False}}}})
    absent = FakeHttp({('POST', QUERY): missing_index('varnish-*'), ('POST', PRIVILEGES): {'has_all_requested': True, 'index': {'varnish-*': {'read': True}}}})

    def no_rsc(params, body):
        return unknown('varnish.query_params._rsc.keyword') if '_rsc' in body['query'] else esql([{'req': 12345, 'hits': 9876}])

    partial = FakeHttp({('POST', QUERY): no_rsc})

    def wrong_ca(params, body):
        raise Unreachable(f'{QUERY} unreachable (SSLError)') from ssl.SSLError('certificate verify failed')

    untrusted = FakeHttp({('POST', QUERY): wrong_ca})

    def check(http, es=None, **values):
        with app_with(store, http, es=es, main=values) as c:
            runtime = c.app.state.runtime
            return varnish.test(runtime.context(varnish.PLUGIN, runtime.instances.get('varnish', 'main'), NOW))

    # when
    keyed = {'main': {'api-key': KEY}}
    ok = check(good, es=keyed, index='varnish-*')
    refused, unreachable, missing, tls = check(rejected, es=keyed), check(down, es=keyed), check(fieldless, es=keyed), check(untrusted, es=keyed)
    no_read, unseen, nothing = check(unread, es=keyed, index='varnish-*'), check(hidden, es=keyed, index='varnish-*'), check(absent, es=keyed, index='varnish-*')
    some = check(partial, es=keyed, index='varnish-*')
    off = check(good, es={'main': {'enabled': False}})

    # then
    assert (ok.ok, ok.summary) == (True, 'Varnish: 12,345 GET requests in the last hour in varnish-* through Elasticsearch main, 80 % from cache')
    assert (refused.ok, refused.summary) == (False, msg('connection.rejected', status=401))
    assert (unreachable.ok, unreachable.summary) == (False, msg('connection.unreachable'))
    assert (tls.ok, tls.summary) == (False, msg('connection.tls'))
    assert (missing.ok, missing.summary) == (False, 'logs-* lacks varnish.method.keyword, varnish.cache_status.keyword, which the Varnish checks need.')
    said = 'The API key lacks the read privilege on varnish-*, which the Varnish checks read.'
    assert (no_read.ok, no_read.summary) == (False, said) and (unseen.ok, unseen.summary) == (False, said)
    assert (nothing.ok, nothing.summary) == (False, 'No index matches varnish-*.')
    assert (some.ok, some.summary) == (True, 'Varnish: 12,345 GET requests in the last hour in varnish-* through Elasticsearch main, 80 % from cache; '
                                            'the logs have no varnish.query_params._rsc.keyword, so the hints that need it are left out')
    assert (off.ok, off.summary) == (False, msg('peers.unavailable', plugin='Elasticsearch', instance='main'))
    sent = good.calls[0][3]['query']
    assert good.calls[0][2]['Authorization'] == f'ApiKey {KEY}' and sent.startswith('FROM varnish-*') and all(f in sent for f in REQUIRED + OPTIONAL)
    assert not any(KEY in r.summary or '***' in r.summary for r in (ok, refused, unreachable, missing, tls, no_read, unseen, nothing, some, off))


def test_varnish_cards_a_hit_ratio_drop_with_the_paths_that_miss_most():
    # given
    es = FakeEs(ratio(20, 46, 44), PATHS)

    # when
    out = checks.run_ratio(es, NOW, dict(DEFAULT_RULES), TARGET)

    # then
    [card] = out.result.found
    assert (card['key'], card['sev'], card['now'], card['d1']) == ('varnish-hit|shop/varnish|ratio', 'crit', 20.0, 46.0)
    assert 'Most missed in the last hour: /push-sw.js 1,550 of 1,550 missed (100 %): static file' in card['what']
    assert '/garden/chairs 1,006 of 1,184 missed (85 %): Next.js RSC requests' in card['what']
    assert summary_of(out.sections['varnish'])['hitPct'] == 20.0 and es.queries[0].startswith('FROM varnish-*\n') and len(es.queries) == 2
    assert 'a new query value in links (query values are part of the cache key)' in card['what'] and 'landing' not in card['what']
    assert out.result.ran == ('varnish', 'varnish:fields', 'varnish:access:logs')


def test_varnish_stays_quiet_on_a_steady_ratio_and_fails_on_an_es_error():
    # when
    steady = checks.run_ratio(FakeEs(ratio(43, 46, 44), PATHS), NOW, dict(DEFAULT_RULES), TARGET)
    broken = checks.run_ratio(FakeEs(ElasticError('ES|QL HTTP 400', 'parsing_exception: x', 400), PATHS), NOW, dict(DEFAULT_RULES), TARGET)

    # then
    assert steady.result.ok and steady.result.found == [] and summary_of(steady.sections['varnish'])['d7'] == 44.0
    assert not broken.result.ok and broken.result.status == 'error: ES|QL HTTP 400'


def test_the_daily_varnish_list_tags_why_each_path_misses():
    # given
    es = FakeEs([], PATHS)

    # when
    out = checks.run_daily(es, NOW, dict(DEFAULT_RULES), TARGET)

    # then
    [card] = out.result.found
    hints = {c['path']: c['hint'].split(':')[0].split(' (')[0] for c in card['candidates']}
    assert hints == {'/push-sw.js': 'static file', '/garden/chairs': 'Next.js RSC requests', '/checkout/cart/start': 'checkout or personal flow', '/meta.json': 'mostly non-200 answers',
                     '/sale/summer': 'a page without query values that rarely hits', '/search/results': 'unique query values'}
    assert (card['sev'], card['ranKey'], out.result.ran) == ('info', 'varnish-cache', ('varnish-cache', 'varnish-cache:fields', 'varnish-cache:access:logs'))
    assert '(form inputs or tracking values)' in [c['hint'] for c in card['candidates'] if c['path'] == '/search/results'][0]
    assert '24 hours' in es.queries[0] and 'misses >= 2000' in es.queries[0] and es.queries[0].startswith('FROM varnish-*\n')
    assert [r['path'] for r in out.sections['varnishCache'] if r['part'] == 'path'] == [c['path'] for c in card['candidates']]


def test_without_personal_paths_no_path_is_read_as_a_personal_flow():
    # when
    out = checks.run_daily(FakeEs([], PATHS), NOW, dict(DEFAULT_RULES), checks.Target('varnish-*', 'shop', 'varnish', None, {}))

    # then
    [card] = out.result.found
    assert not any(c['hint'].startswith('checkout or personal flow') for c in card['candidates'])


def test_varnish_cache_is_a_daily_check_and_cache_is_not_an_instance_name(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of([], PATHS)})

    # when
    with app_with(store, http, main={}) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        instances = c.app.state.runtime.instances
        refused = {}
        for name in ('cache', 'cache-east'):
            with pytest.raises(InvalidInstance) as caught:
                instances.create('varnish', name, {'elasticsearch': 'main'}, True, USER)
            refused[name] = (caught.value.field, caught.value.text)
        c.app.state.sweeper.run('varnish', 'varnish-cache', NOW)
        state = store.load()

    # then
    assert (jobs['varnish.varnish-cache'].period, jobs['varnish.varnish-cache'].offset) == (86400, 25200)
    assert (jobs['varnish.varnish'].period, jobs['varnish.varnish'].offset) == (900, 720)
    assert refused == {name: ('name', msg('instances.invalid.reserved', name=name)) for name in ('cache', 'cache-east')}
    assert state['serverPasses']['varnish-cache']['status'] == 'ok'
    assert {a['ranKey'] for a in card_map(store).values()} == {'varnish-cache'}


def test_varnish_reads_through_its_elasticsearch_instance(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of(ratio(20, 46, 44))})

    # when
    with app_with(store, http, es={'main': {'api-key': 'lk', 'log-index': 'web-*', 'ui-url': KIBANA}}, main={'namespace': 'shop', 'personal-paths': PERSONAL}) as c:
        c.app.state.sweeper.run('varnish', 'varnish', NOW)
        state = store.load()
    cards = card_map(store)

    # then
    card = cards['varnish-hit|shop/varnish|ratio']
    assert state['serverPasses']['varnish']['status'] == 'ok' and 'cluster' not in card and 'ranKey' not in card
    assert all(c[1] == QUERY and c[2]['Authorization'] == 'ApiKey lk' and c[3]['query'].startswith('FROM web-*\n') for c in http.calls)
    assert [r['part'] for r in state['varnish']] == ['summary'] and state['varnish'][0]['plugin'] == 'varnish' and 'cluster' not in state['varnish'][0]
    labels = [x['label'] for x in card['links']]
    assert labels[0] == 'Varnish misses in Kibana'
    shown = link_params(card['links'][0]['url'])
    assert card['links'][0]['url'].startswith(f'{KIBANA}/app/discover#/') and 'varnish.cache_status:"miss" and varnish.method:"GET"' in shown and "from:'now-2h'" in shown


def test_varnish_follows_its_elasticsearch_instance(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of(ratio(20, 46, 44))})
    store.save_state({'anomalies': [open_card()], 'initialized': True}, None)

    # when
    with app_with(store, http, es={'main': {'enabled': False}}, main={'namespace': 'shop'}) as c:
        c.app.state.sweeper.run('varnish', 'varnish', NOW)
        state = store.load()

    # then
    assert state['serverPasses']['varnish']['status'] == 'error: ' + msg('peers.unavailable', plugin='Elasticsearch', instance='main')
    assert card_map(store)['varnish-hit|shop/varnish|ratio']['status'] == 'open' and http.calls == []


def test_varnish_without_varnish_logs_is_ok_and_quiet(store: Store):
    # given
    names = 'varnish.method.keyword, varnish.cache_status.keyword'
    cases = {'fields': (unknown('varnish.method.keyword', 'varnish.cache_status.keyword'), {'varnish-fields|shop/varnish|fields': f'Varnish logs lack the fields {names}',
                                                                                            'varnish-cache-fields|shop/varnish|fields': f'Varnish logs lack the fields {names}'}),
             'index': (missing_index('logs-*'), {}),
             'rows': ([], {})}

    for name, (got, extra) in cases.items():
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [open_card(), open_card(key='varnish-cache|shop/varnish|candidates', sig='varnish-cache', sub='candidates', ranKey='varnish-cache')],
                          'initialized': True}, None)
        http = FakeHttp({('POST', QUERY): answer_of(got, got)})

        # when
        with app_with(store, http, main={'namespace': 'shop'}) as c:
            c.app.state.sweeper.run('varnish', 'varnish', NOW)
            if name != 'rows':
                c.app.state.sweeper.run('varnish', 'varnish-cache', NOW)
            state = store.load()
        cards = card_map(store)

        # then
        assert state['serverPasses']['varnish']['status'] == 'ok \u00b7 no data', name
        if name != 'rows':
            assert state['serverPasses']['varnish-cache']['status'] == 'ok \u00b7 no data', name
        assert all(cards[k]['status'] == 'open' for k in ('varnish-hit|shop/varnish|ratio', 'varnish-cache|shop/varnish|candidates')), name
        assert {k: a['label'] for k, a in cards.items() if a['sig'].endswith('-fields')} == extra, name
        assert all(a['sev'] == 'warn' and a['ranKey'] in ('varnish:fields', 'varnish-cache:fields') for a in cards.values() if a['sig'].endswith('-fields')), name


def test_a_fields_card_clears_when_the_fields_arrive(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of(unknown('varnish.method.keyword'))})

    # when
    with app_with(store, http, main={'namespace': 'shop'}) as c:
        c.app.state.sweeper.run('varnish', 'varnish', NOW)
        first = card_map(store)
        http.routes[('POST', QUERY)] = answer_of(ratio(43, 46, 44))
        c.app.state.sweeper.run('varnish', 'varnish', NOW + 900)
        second = card_map(store)

    # then
    assert first['varnish-fields|shop/varnish|fields']['label'] == 'Varnish logs lack the field varnish.method.keyword'
    assert 'or the API key cannot read' in first['varnish-fields|shop/varnish|fields']['what']
    assert second['varnish-fields|shop/varnish|fields']['status'] == 'cleared'


def test_an_hour_without_varnish_logs_keeps_the_ratio_card_open(store: Store):
    # given
    rows = [r for r in ratio(20, 46, 44) if r['w'] != 'now']
    http = FakeHttp({('POST', QUERY): answer_of(rows)})
    store.save_state({'anomalies': [open_card()], 'initialized': True}, None)

    # when
    with app_with(store, http, main={'namespace': 'shop'}) as c:
        c.app.state.sweeper.run('varnish', 'varnish', NOW)
        state = store.load()

    # then
    assert state['serverPasses']['varnish']['status'] == 'ok \u00b7 no data for the last hour'
    assert card_map(store)['varnish-hit|shop/varnish|ratio']['status'] == 'open'
    assert summary_of(state['varnish'])['hitPct'] is None and summary_of(state['varnish'])['d1'] == 46.0


def test_the_daily_list_reads_without_the_optional_fields():
    # given
    es = lacking(FakeEs([], PATHS), *OPTIONAL)

    # when
    out = checks.run_daily(es, NOW, dict(DEFAULT_RULES), TARGET)

    # then
    [card] = out.result.found
    hints = {c['path']: c['hint'].split(':')[0] for c in card['candidates']}
    assert out.result.status == 'ok' and len(es.queries) == 2
    assert 'rsc = 0' in es.queries[-1] and 'q = 0' in es.queries[-1] and 'ok = 1' in es.queries[-1] and not any(f in es.queries[-1] for f in OPTIONAL)
    assert not any(h.startswith(('Next.js RSC', 'unique query values', 'mostly non-200')) for h in hints.values())
    assert hints['/push-sw.js'] == 'static file' and hints['/checkout/cart/start'] == 'checkout or personal flow'
    assert 'The logs have no varnish.query_params._rsc.keyword, varnish.query_string, varnish.response_code.keyword' in card['what']


def test_a_path_list_without_its_fields_keeps_the_ratio_card():
    # when
    optional = checks.run_ratio(lacking(FakeEs(ratio(20, 46, 44), PATHS), 'varnish.query_params._rsc.keyword'), NOW, dict(DEFAULT_RULES), TARGET)
    required = checks.run_ratio(lacking(FakeEs(ratio(20, 46, 44), PATHS), 'varnish.path.keyword'), NOW, dict(DEFAULT_RULES), TARGET)

    # then
    [card] = optional.result.found
    assert optional.result.status == 'ok' and 'Most missed in the last hour: /push-sw.js' in card['what']
    [card] = required.result.found
    assert required.result.status == 'ok \u00b7 no data for path list' and 'Most missed' not in card['what']
    assert required.result.ran == ('varnish', 'varnish:fields', 'varnish:access:logs')


def test_a_server_echo_of_the_key_never_reaches_a_status_a_card_or_a_test(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): unknown(KEY, 'varnish.path.keyword')})

    # when
    with app_with(store, http, es={'main': {'api-key': KEY}}, main={'namespace': 'shop'}) as c:
        c.app.state.sweeper.run('varnish', 'varnish-cache', NOW)
        state = store.load()
        runtime = c.app.state.runtime
        tested = varnish.test(runtime.context(varnish.PLUGIN, runtime.instances.get('varnish', 'main'), NOW))
    [card] = [a for a in card_map(store).values() if a['sig'].endswith('-fields')]

    # then
    with pytest.raises(ElasticError) as e:
        Elastic(ES_URL, KEY, http).query('FROM logs-*')
    assert KEY not in ' '.join(e.value.unknown) and '***' in e.value.unknown
    assert card['label'] == 'Varnish logs lack the field varnish.path.keyword'
    shown = ' '.join([state['serverPasses']['varnish-cache']['status'], card['label'], card['what'], card['short'], tested.summary])
    assert KEY not in shown and '***' not in shown and not tested.ok


def test_varnish_refused_for_permission_is_never_no_data(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): denied()})
    store.save_state({'anomalies': [open_card(), open_card(key='varnish-cache|shop/varnish|candidates', sig='varnish-cache', sub='candidates', ranKey='varnish-cache')],
                      'initialized': True}, None)

    # when
    with app_with(store, http, es={'main': {'api-key': KEY}}, main={'namespace': 'shop'}) as c:
        c.app.state.sweeper.run('varnish', 'varnish', NOW)
        c.app.state.sweeper.run('varnish', 'varnish-cache', NOW)
        state = store.load()
        cards = card_map(store)
        http.routes[('POST', QUERY)] = answer_of(ratio(43, 46, 44), [])
        c.app.state.sweeper.run('varnish', 'varnish', NOW + 900)
        c.app.state.sweeper.run('varnish', 'varnish-cache', NOW + 900)
    after = card_map(store)

    # then
    assert {k: state['serverPasses'][k]['status'] for k in ('varnish', 'varnish-cache')} == {'varnish': 'ok \u00b7 no access to logs', 'varnish-cache': 'ok \u00b7 no access to logs'}
    assert cards['varnish-hit|shop/varnish|ratio']['status'] == 'open' and cards['varnish-cache|shop/varnish|candidates']['status'] == 'open'
    access = {a['ranKey']: a for a in cards.values() if a['sig'] == 'no-access'}
    assert set(access) == {'varnish:access:logs', 'varnish-cache:access:logs'}
    assert all(a['label'] == 'Varnish cannot read logs: the API key lacks the read privilege on logs-*' and a['sev'] == 'warn' for a in access.values())
    assert not any(KEY in a['what'] + a['label'] for a in access.values())
    assert all(after[a['key']]['status'] == 'cleared' for a in access.values())


def test_varnish_links_are_the_card_urls_over_https_only(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of(ratio(20, 46, 44))})
    plain = FakeHttp({('POST', QUERY): answer_of(ratio(20, 46, 44))})

    # when
    with app_with(store, http, es={'main': {'ui-url': KIBANA}}, main={'namespace': 'shop'}) as c:
        c.app.state.sweeper.run('varnish', 'varnish', NOW)
        runtime = c.app.state.runtime
        card = card_map(store)['varnish-hit|shop/varnish|ratio']
        own = varnish.links(card, runtime.instances.get('varnish', 'main').info())
        forged = varnish.links(dict(card, urls=[{'label': 'x', 'url': 'javascript:alert(1)'}, {'label': 'y', 'url': 'http://kibana.example.com'}]), runtime.instances.get('varnish', 'main').info())
        other = varnish.links(dict(card, src='prom'), runtime.instances.get('varnish', 'main').info())
    store.anomalies.delete_many({})
    store.save_state({'anomalies': [], 'initialized': True}, None)
    with app_with(store, plain, es={'main': {'ui-url': 'http://kibana.example.com'}}, main={'namespace': 'shop'}) as c:
        c.app.state.sweeper.run('varnish', 'varnish', NOW)
        bare = card_map(store)['varnish-hit|shop/varnish|ratio']

    # then
    assert [x['label'] for x in own] == ['Varnish misses in Kibana'] and own[0]['url'].startswith('https://')
    assert forged is None and other is None
    assert bare.get('urls') in (None, []) and 'Varnish misses in Kibana' not in [x['label'] for x in bare.get('links') or []]


def test_varnish_second_instance_is_scoped(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of(ratio(43, 46, 44)), ('POST', f'{EAST}/_query'): answer_of(ratio(20, 46, 44))})

    # when
    with app_with(store, http, es={'main': {}, 'east': {'url': EAST}}, main={'namespace': 'shop'}, east={'namespace': 'shop', 'elasticsearch': 'east'}) as c:
        c.app.state.sweeper.run('varnish', 'varnish', NOW)
        state = store.load()
    card = card_map(store)['varnish-hit|shop/varnish|ratio@east']

    # then
    assert (card['cluster'], card['ranKey']) == ('east', 'varnish-east')
    assert set(state['serverPasses']) >= {'varnish', 'varnish-east'}
    assert sorted(r.get('cluster') or '' for r in state['varnish']) == ['', 'east']


def test_varnish_rules_come_from_the_rule_set(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): answer_of(ratio(20, 46, 44))})

    # when
    with app_with(store, http, main={'namespace': 'shop'}) as c:
        c.app.state.sweeper.run('varnish', 'varnish', NOW)
        default = set(card_map(store))
        c.app.state.runtime.live.set('varnish.rules', json.dumps({'min_req': 50000}), USER)
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [], 'initialized': True}, None)
        c.app.state.sweeper.run('varnish', 'varnish', NOW)
        stricter = set(card_map(store))

    # then
    assert default == {'varnish-hit|shop/varnish|ratio'} and stricter == set()
