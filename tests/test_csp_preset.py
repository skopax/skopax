import contextlib
import json
import os
import re
import time
import uuid

import pytest

from skopax import engine, promqlchecks, remote
from skopax.checkpresets import load, presets
from skopax.contract import InstanceInfo
from skopax.instances import Instances, PluginState
from skopax.kit.elastic import epoch
from skopax.plugins import elasticsearch
from skopax.plugins.csp import checks as csp
from skopax.promqlchecks import PASS, CustomChecks, ElasticAsker, Invalid
from skopax.registry import Registry
from skopax.remote import Reply
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.plugin_kit import KIT_KEY, add_instance

NOW = 1_792_224_000.0
HOUR, DAY = 3600, 86400
ES_URL = 'https://es.example.com'
VALUES = {'instance': 'central', 'name': 'shop', 'index': 'logs-csp-*'}
COLUMNS = {'new-host': ['host', 'value'], 'reports-jump': ['value', 'day_ago', 'week_ago']}
LIVE = os.environ.get('SKOPAX_TEST_ES_URL', '')
DISSECT = re.compile(r'\| DISSECT (message|m) """(.*?)"""')
SHIPPED_FIELDS = '\n'.join(['| EVAL m = REPLACE(REPLACE(message, """\\\\["]""", "\\""), """["]\\s*:\\s*["]""", "\\":\\"")']
                           + [f'| DISSECT m """%{{}}"{key}":"%{{{col}}}"%{{}}"""' for key, col in (('blockedURL', 'b1'), ('blocked-uri', 'b2'), ('effectiveDirective', 'd1'),
                                                                                       ('effective-directive', 'd2'), ('sourceFile', 's1'), ('source-file', 's2'))]
                           + ['| EVAL blocked = COALESCE(b1, b2, ""), directive = COALESCE(d1, d2, "?"), source = COALESCE(s1, s2, "")',
                              '| WHERE NOT (blocked LIKE "*-extension*" OR source LIKE "*-extension*")'])


def report(blocked: str, directive: str = 'script-src-elem', source: str = 'https://shop.example.com/app.js', legacy: bool = False, raw: bool = False, spaced: bool = False) -> str:
    if legacy:
        body = {'csp-report': {'document-uri': 'https://shop.example.com/', 'blocked-uri': blocked, 'effective-directive': directive, 'source-file': source}}
    else:
        body = {'type': 'csp-violation', 'body': {'documentURL': 'https://shop.example.com/', 'blockedURL': blocked, 'effectiveDirective': directive, 'sourceFile': source}}
    plain = json.dumps(body, separators=(', ', ' : ') if spaced else (',', ':'))
    return plain if raw else 'csp-report ' + plain.replace('"', '\\"')


def dissect(pattern: str, text: str) -> dict | None:
    parts = [p for p in re.split(r'(%\{[^}]*\})', pattern) if p]
    regex, names = '', []
    for n, p in enumerate(parts):
        if p.startswith('%{'):
            regex += '(.*)' if n == len(parts) - 1 else '(.*?)'
            names.append(p[2:-1])
        else:
            regex += re.escape(p)
    m = re.fullmatch(regex, text, re.S)
    return {k: v for k, v in zip(names, m.groups()) if k} if m else None


def fields(query: str, message: str) -> dict | None:
    got: dict = {}
    found = DISSECT.findall(query)
    if any(on == 'm' for on, _ in found):
        message = re.sub(r'["]\s*:\s*["]', '":"', re.sub(r'\\["]', '"', message))
    for _, pattern in found:
        got |= dissect(pattern, message) or {}
    first = lambda k: next((got[f'{k}{n}'] for n in (1, 2, 3, 4) if got.get(f'{k}{n}') is not None), None)
    blocked, source = first('b') or '', first('s') or ''
    if '-extension' in blocked or '-extension' in source:
        return None
    return {'blocked': blocked, 'directive': first('d') or '?'}


def host(blocked: str) -> str:
    if blocked == '':
        return 'inline'
    rest = dissect('%{}://%{rest}', blocked)
    if rest is None:
        return blocked
    h = re.sub(':443$', '', rest['rest'].split('/')[0].split('?')[0])
    return h or blocked


def reports(docs: list[tuple[float, str]], query: str, since: float, until: float) -> list[tuple[float, dict]]:
    out = []
    for at, message in docs:
        if since <= at < until and ('blockedURL' in message or 'blocked-uri' in message):
            found = fields(query, message)
            if found is not None:
                out.append((at, found))
    return out


def preset_rows(key: str, query: str, docs: list[tuple[float, str]], start: float, end: float) -> list[dict]:
    if key == 'new-host':
        seen: dict = {}
        for at, f in reports(docs, query, end - DAY - 7 * DAY, end):
            h = host(f['blocked'])
            first, count = seen.get(h, (at, 0))
            seen[h] = (min(first, at), count + (at >= start))
        return [{'host': h, 'value': count} for h, (first, count) in sorted(seen.items()) if first >= end - DAY]
    now = len(reports(docs, query, start, end))
    day = len(reports(docs, query, start - DAY, end - DAY))
    week = len(reports(docs, query, start - 7 * DAY, end - 7 * DAY))
    usual = max(day, week, 1)
    return [{'value': now, 'day_ago': day, 'week_ago': week}] if now >= 100 and now >= 5 * usual else []


def plugin_rows(docs: list[tuple[float, str]], since: float, until: float) -> list[dict]:
    rows: dict = {}
    for at, f in reports(docs, csp.query('FROM logs-*', 1), since, until):
        r = rows.setdefault((f['directive'], f['blocked']), {'directive': f['directive'], 'blocked': f['blocked'], 'c': 0, 'newest': ''})
        r['c'] += 1
        r['newest'] = max(r['newest'], engine.iso(at))
    return list(rows.values())


class Elastic:
    def __init__(self, docs: list[tuple[float, str]], checks: list[dict]):
        self.docs = docs
        self.keys = {d['query']: d['id'].removeprefix('csp-shop-') for d in checks}
        self.asked: list[dict] = []
        self.shipped = {d['query'] for d in presets()['csp'].fill(VALUES)[1]}

    def __call__(self, method, url, headers, data, timeout):
        assert (method, url) == ('POST', f'{ES_URL}/_query')
        body = json.loads(data)
        params = {k: v for p in body.get('params') or [] for k, v in p.items()}
        self.asked.append(params)
        assert body['query'] in self.shipped, 'the query sent is the rendered text of the shipped preset'
        key = self.keys[body['query']]
        rows = preset_rows(key, body['query'], self.docs, epoch(params['start']), epoch(params['end']))
        names = COLUMNS[key]
        return Reply({'columns': [{'name': n, 'type': 'keyword'} for n in names], 'values': [[r.get(n) for n in names] for r in rows]}, {})


def running(store, docs: list[tuple[float, str]]) -> tuple[CustomChecks, list[dict]]:
    _, made = presets()['csp'].fill(VALUES)
    fake = Elastic(docs, made)
    registry = Registry.of(elasticsearch.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), fake)
    add_instance(store, elasticsearch.PLUGIN, 'central', sealer_key=KIT_KEY, primary=True, url=ES_URL, **{'ui-url': 'https://kibana.example.com'})
    runtime.refresh()
    checks = CustomChecks(store, runtime, lambda results, now, links: engine.apply(store, results, now, links), monotonic=lambda: 0.0)
    for d in made:
        checks.defs.create(d, 'admin@example.com')
    return checks, made


def opened(store) -> list[dict]:
    return [a for a in store.load().get('anomalies') or [] if a.get('src') == PASS and a['status'] == 'open']


def burst(at: float, blocked: str, n: int, **kw) -> list[tuple[float, str]]:
    return [(at + i, report(blocked, **kw)) for i in range(n)]


def test_the_csp_preset_loads_at_start_as_an_elasticsearch_preset_with_an_index_placeholder():
    # when
    preset = load()['csp']
    view = preset.view()

    # then
    assert (view['source'], [p['kind'] for p in view['placeholders']]) == ('elasticsearch', ['instance', 'name', 'index', 'condition'])
    assert [(c['key'], c['severity']) for c in view['checks']] == [('new-host', 'warn'), ('reports-jump', 'warn')]
    index = next(p for p in view['placeholders'] if p['kind'] == 'index')
    assert 'default' not in index and 'only the CSP report logs' in index['help']


def test_filling_the_preset_gives_bounded_elasticsearch_checks_that_read_the_report_fields_of_the_plugin():
    # when
    values, made = presets()['csp'].fill(VALUES)

    # then
    assert values == VALUES | {'filter': ''}
    assert [d['id'] for d in made] == ['csp-shop-new-host', 'csp-shop-reports-jump']
    for d in made:
        assert (d['source'], d['instance'], d['window']) == ('elasticsearch', 'central', 60)
        assert d['query'].startswith('FROM logs-csp-*\n') and '?start' in d['query'] and '?end' in d['query']
        assert SHIPPED_FIELDS in d['query'], 'the message is normalised once, then read with the plain patterns'
        assert csp.FIELDS.split('\n')[-1] in d['query'], 'the extension filter of the plug-in'
        assert promqlchecks.clean(d) == d
    new, jump = made
    assert new['by'] == ['host'] and (new['op'], new['threshold']) == ('>=', csp.RULES['new_min'])
    assert 'MIN(@timestamp)' in new['query'] and '- 7 days' in new['query'] and '| WHERE seen >= TO_DATETIME(?end) - 24 hours\n' in new['query']
    assert '| WHERE @timestamp >= TO_DATETIME(?end) - 24 hours - 7 days AND @timestamp < TO_DATETIME(?end)\n' in new['query']
    assert 'value = SUM(CASE(@timestamp >= TO_DATETIME(?start), 1, 0)) BY host' in new['query']
    assert f"value >= {csp.RULES['jump_min']} AND value >= {csp.RULES['jump']:g} * usual" in jump['query'] and 'GREATEST(day_ago, week_ago, TO_LONG(1))' in jump['query']
    assert jump['by'] == [] and '- 1 day' in jump['query']


def test_the_index_is_required():
    # when
    with pytest.raises(Invalid) as e:
        presets()['csp'].fill(VALUES | {'index': ''})

    # then
    assert 'Index' in e.value.text


@pytest.mark.parametrize('index', ['logs | DROP message', 'logs-* METADATA _index', 'logs"', 'logs`', 'logs\n| LIMIT 1', 'logs-*;x', ','])
def test_an_index_pattern_that_could_break_out_of_from_is_refused(index):
    # when
    with pytest.raises(Invalid) as e:
        presets()['csp'].fill(VALUES | {'index': index})

    # then
    assert 'Index' in e.value.text and 'index pattern' in e.value.text


@pytest.mark.parametrize('index', ['logs-*,remote:app-logs-*', '.ds-logs-web-*', 'logs-*,-logs-debug-*', 'app_logs.2026'])
def test_ordinary_index_patterns_are_taken(index):
    # when
    _, made = presets()['csp'].fill(VALUES | {'index': index})

    # then
    assert all(d['query'].startswith(f'FROM {index}\n') for d in made)


def test_a_new_host_fires_once_a_known_host_and_extensions_stay_quiet_like_the_plugin(store):
    # given
    hour = NOW - HOUR
    docs = (burst(NOW - 3 * DAY, 'https://cdn.example.com/lib.js', 4) + burst(hour + 60, 'https://cdn.example.com/lib.js', 15)
            + burst(hour + 120, 'https://tracker.example.net/t.js?id=1', 12)
            + burst(hour + 180, 'https://ads.example.org:443/pixel.gif', 11, directive='img-src', legacy=True)
            + burst(hour + 240, 'chrome-extension://abcdef/inject.js', 30)
            + burst(hour + 300, 'https://evil.example.com/x.js', 30, source='moz-extension://abc/content.js')
            + burst(hour + 360, 'https://few.example.com/a.js', 3))
    checks, _ = running(store, docs)
    known = csp.seeded(plugin_rows(docs, hour - 7 * DAY, hour), NOW, csp.RULES)
    plugin = {g['host'] for _, g, _ in csp.blocked_hosts(csp.grouped(plugin_rows(docs, hour, NOW)), known, NOW, csp.RULES)}

    # when
    checks.run_once(NOW)
    found = opened(store)

    # then
    assert plugin == {'tracker.example.net', 'ads.example.org'}
    assert sorted(a['checkLabels']['host'] for a in found) == sorted(plugin)
    assert all(a['key'].startswith(f'{PASS}|csp-shop-new-host|') and a['sev'] == 'warn' for a in found)
    card = next(a for a in found if a['checkLabels']['host'] == 'ads.example.org')
    assert (card['label'], card['now']) == ('CSP blocks ads.example.org', 11)
    assert 'csp-shop-new-host' in {a['checkId'] for a in found} and checks.defs.state('csp-shop-reports-jump')['state'] == 'ok'


def test_an_hourly_jump_against_the_larger_of_a_day_and_a_week_ago_fires_and_a_small_one_stays_quiet(store):
    # given
    hour = NOW - HOUR
    docs = burst(hour, 'https://cdn.example.com/lib.js', 600) + burst(hour - DAY, 'https://cdn.example.com/lib.js', 50) + burst(hour - 7 * DAY, 'https://cdn.example.com/lib.js', 80)
    checks, _ = running(store, docs)

    # when
    checks.run_once(NOW)
    found = [a for a in opened(store) if a['checkId'] == 'csp-shop-reports-jump']

    # then
    assert [a['now'] for a in found] == [600]
    assert 'against 50 in the same hour a day ago and 80 a week ago' in found[0]['what']


@pytest.mark.parametrize('now, day, week, fires', [(300, 100, 0, False), (150, 0, 0, True), (90, 0, 0, False), (500, 0, 100, True), (499, 0, 100, False)])
def test_the_jump_needs_five_times_the_usual_hour_and_100_reports_and_a_quiet_baseline_still_fires(store, now, day, week, fires):
    # given
    hour = NOW - HOUR
    blocked = 'https://cdn.example.com/lib.js'
    docs = burst(hour, blocked, now) + burst(hour - DAY, blocked, day) + burst(hour - 7 * DAY, blocked, week)
    checks, _ = running(store, docs)

    # when
    checks.run_once(NOW)

    # then
    assert bool([a for a in opened(store) if a['checkId'] == 'csp-shop-reports-jump']) is fires


def test_the_preset_thresholds_are_the_plugin_rules():
    # when
    _, (new, jump) = presets()['csp'].fill(VALUES)

    # then
    assert new['threshold'] == csp.RULES['new_min'] and f"{csp.RULES['new_days']} days" in new['query']
    assert f"{csp.RULES['jump']:g} * usual" in jump['query'] and f"value >= {csp.RULES['jump_min']}" in jump['query']


def test_plain_and_spaced_report_json_is_read_too_and_an_extension_in_it_is_left_out(store):
    # given
    hour = NOW - HOUR
    docs = (burst(hour + 60, 'https://tracker.example.net/t.js', 12, raw=True) + burst(hour + 120, 'https://ads.example.org/p.gif', 11, legacy=True, raw=True)
            + burst(hour + 150, 'https://spaced.example.net/s.js', 10, raw=True, spaced=True) + burst(hour + 160, 'https://wide.example.net/w.js', 10, spaced=True)
            + burst(hour + 180, 'https://evil.example.com/x.js', 30, source='chrome-extension://abc/c.js', raw=True))
    checks, _ = running(store, docs)

    # when
    checks.run_once(NOW)

    # then
    assert sorted(a['checkLabels']['host'] for a in opened(store)) == ['ads.example.org', 'spaced.example.net', 'tracker.example.net', 'wide.example.net']


def test_a_host_that_starts_slowly_still_fires_in_its_first_day_and_its_card_stays_while_it_does(store):
    # given
    blocked = 'https://tracker.example.net/t.js'
    docs = burst(NOW - HOUR, blocked, 5) + burst(NOW, blocked, 15) + burst(NOW + HOUR, blocked, 15) + burst(NOW + 30 * HOUR, blocked, 15)
    checks, _ = running(store, docs)

    # when
    seen = []
    for at in (NOW, NOW + HOUR, NOW + 2 * HOUR, NOW + 31 * HOUR):
        checks.run_once(at)
        seen.append(sorted(a['checkLabels']['host'] for a in opened(store)))

    # then
    assert seen == [[], ['tracker.example.net'], ['tracker.example.net'], []], 'quiet at 5, open at 15 in its first day, closed once it is a day old'


def live_docs(now: float) -> tuple[list[tuple[float, str]], list[tuple[float, str]]]:
    hour = now - HOUR
    hosts = (burst(now - 3 * DAY, 'https://cdn.example.com/lib.js', 4) + burst(hour + 60, 'https://cdn.example.com/lib.js', 15)
             + burst(hour + 120, 'https://tracker.example.net/t.js?id=1', 12) + burst(hour + 180, 'https://ads.example.org:443/pixel.gif', 11, directive='img-src', legacy=True)
             + burst(hour + 200, 'https://raw.example.net/r.js', 10, raw=True)
             + burst(hour + 210, 'https://spaced.example.net/s.js', 10, legacy=True, raw=True, spaced=True) + burst(hour + 240, 'chrome-extension://abcdef/inject.js', 30)
             + burst(hour + 300, 'https://evil.example.com/x.js', 30, source='moz-extension://abc/content.js') + burst(hour + 360, 'https://few.example.com/a.js', 3))
    blocked = 'https://cdn.example.com/lib.js'
    jump = burst(hour, blocked, 600) + burst(hour - DAY, blocked, 50) + burst(hour - 7 * DAY, blocked, 80)
    return hosts, jump


def loaded(stream: str, docs: list[tuple[float, str]]) -> None:
    lines = [x for at, message in docs for x in (json.dumps({'create': {}}), json.dumps({'@timestamp': engine.iso(at), 'message': message}))]
    got = remote.send('POST', f"{LIVE.rstrip('/')}/{stream}/_bulk?refresh=true", {'Content-Type': 'application/x-ndjson'}, ('\n'.join(lines) + '\n').encode(), 60).body
    assert got.get('errors') is False


@pytest.mark.skipif(not LIVE, reason='set SKOPAX_TEST_ES_URL to an Elasticsearch 8.17 or later without security to run the preset queries for real')
def test_the_shipped_queries_give_the_rows_the_tests_assume_on_a_real_elasticsearch():
    # given
    now = float(int(time.time()))
    hosts, jump = live_docs(now)
    tag = uuid.uuid4().hex[:8]
    streams = {'new-host': (f'logs-skopaxcsp-hosts{tag}', hosts), 'reports-jump': (f'logs-skopaxcsp-jump{tag}', jump)}
    asker = ElasticAsker(InstanceInfo('central', True, None, {'url': LIVE}), remote.send, 60)

    try:
        for stream, docs in streams.values():
            loaded(stream, docs)

        # when
        got = {}
        for key, (stream, docs) in streams.items():
            _, made = presets()['csp'].fill(VALUES | {'index': stream})
            d = next(c for c in made if c['id'].endswith(key))
            got[key] = (asker.rows(d['query'], now), preset_rows(key, d['query'], docs, now - HOUR, now))
    finally:
        for stream, _ in streams.values():
            with contextlib.suppress(Exception):
                remote.send('DELETE', f"{LIVE.rstrip('/')}/_data_stream/{stream}", {}, None, 30)

    # then
    (columns, rows), want = got['new-host']
    assert columns == COLUMNS['new-host'] and sorted(rows, key=lambda r: r['host']) == want
    assert {r['host'] for r in want} == {'tracker.example.net', 'ads.example.org', 'raw.example.net', 'spaced.example.net', 'few.example.com'}
    (columns, rows), want = got['reports-jump']
    assert columns == COLUMNS['reports-jump'] and rows == want == [{'value': 600, 'day_ago': 50, 'week_ago': 80}]


def test_the_filter_is_optional_and_empty_adds_nothing_to_the_queries():
    # given
    view = presets()['csp'].view()
    filter_ = next(p for p in view['placeholders'] if p['key'] == 'filter')

    # when
    _, plain = presets()['csp'].fill(VALUES)
    _, blank = presets()['csp'].fill(VALUES | {'filter': '   '})

    # then
    assert filter_['optional'] is True and filter_['kind'] == 'condition'
    assert plain == blank and all('()' not in d['query'] and '${' not in d['query'] and 'kubernetes' not in d['query'] for d in plain)


def test_a_filter_goes_in_parentheses_right_after_the_time_where_of_every_check():
    # given
    condition = 'kubernetes.namespace == "shop" AND NOT (log.level == "debug")'

    # when
    values, made = presets()['csp'].fill(VALUES | {'filter': condition})

    # then
    assert values['filter'] == condition
    for d in made:
        before, after = d['query'].split(f'\n| WHERE ({condition})')
        assert before.count('| WHERE') == 1 and 'TO_DATETIME(?end)' in before.split('\n')[-1]
        assert after.startswith('\n| WHERE message LIKE "*blockedURL*"')
        assert promqlchecks.clean(d) == d


@pytest.mark.parametrize('condition', ['kubernetes.namespace == "shop"', 'host.name LIKE "web-*" AND NOT (log.level == "debug")', 
                                       'message LIKE "*a(b*"', 'a == "say \\"hi\\""'])
def test_plain_boolean_conditions_are_taken(condition):
    # when
    values, _ = presets()['csp'].fill(VALUES | {'filter': condition})

    # then
    assert values['filter'] == condition


@pytest.mark.parametrize('condition', ['a == 1 | DROP message', 'a == 1; b', 'a == 1\n| LIMIT 1', '(a == 1', 'a == 1) OR (b == 2', 'a == "x', 'a == 1 /* c', 'a == 1 // c', 'a == 1 ) | ( b', 'a == """x"""" // ) OR (true', 'a == """x"""', '`k8s.pod` == "x"', 'a == `b', 'a == "x|y"', 'a == "x;y"',
                                       'x' * 301])
def test_a_filter_that_is_not_a_plain_condition_is_refused(condition):
    # when
    with pytest.raises(Invalid) as e:
        presets()['csp'].fill(VALUES | {'filter': condition})

    # then
    assert 'Filter' in e.value.text and 'condition' in e.value.text
