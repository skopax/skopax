import time

import pytest

from skopax.contract import Field, InstanceInfo
from skopax.errkind import InvalidInstance
from skopax.instances import _check
from skopax.plugins import pmm
from skopax.plugins.pmm import checks
from skopax.store import Store
from tests.fake_http import FakeHttp
from tests.test_pmm_plugin import NOW, PRIMARY, R1, URL, app_with, cards_of, connection_of, pmm_data, pmm_routes, pmm_runtime, qan_data, sweep

NAMES = ['service_name', 'database', 'queryid', 'q', 'c0', 't0', 'c1', 't1', 'c2', 't2', 'pk0', 'pkat0', 'pkc0']


def split_data() -> dict:
    data = pmm_data()
    for k in ('connmain', 'connsplit'):
        for i in range(3):
            data[(k, i)] = []
    for i, (main, apart) in enumerate(((100, 700), (100, 100), (100, 100))):
        data[('connmain', i)].append(({'service_name': PRIMARY}, main))
        data[('connsplit', i)].append(({'service_name': PRIMARY}, apart))
    for i, v in enumerate((600, 200, 210)):
        data[('connmain', i)].append(({'service_name': R1}, v))
    return data


def test_the_split_adds_two_connection_queries_and_nothing_changes_without_users():
    # when
    plain = checks.pg_queries('.*')
    split = checks.pg_queries('.*', ['bi_user', 'a.b'])

    # then
    assert 'connmain' not in plain and 'connsplit' not in plain
    assert list(split)[2:] == list(plain) and list(split)[:2] == ['connmain', 'connsplit']
    assert 'usename!~"bi_user|a\\\\.b"' in split['connmain'] and 'usename=~"bi_user|a\\\\.b"' in split['connsplit']


def test_split_user_connections_never_raise_the_application_jump_and_show_apart_at_info():
    # when
    found, rows = checks.evaluate_pmm(split_data(), checks.PMM_RULE)

    # then
    by = {x['key']: x for x in found}
    assert f'pmm|{PRIMARY}|conn-jump' not in by
    assert by[f'pmm|{PRIMARY}|saturation']['sev'] == 'warn', 'every user still counts against max_connections'
    assert by[f'pmm|{PRIMARY}|conn-split']['sev'] == 'info' and by[f'pmm|{PRIMARY}|conn-split']['short'].startswith('INFO ')
    assert f'pmm|{R1}|conn-jump' in by and f'pmm|{R1}|conn-split' not in by
    row = next(r for r in rows if r['svc'] == PRIMARY)
    assert (row['connSplit'], row['connSplitD1']) == (700, 100)


def test_the_plain_pass_keeps_its_jump_cards_and_rows_without_a_split():
    # when
    found, rows = checks.evaluate_pmm(pmm_data(), checks.PMM_RULE)

    # then
    assert f'pmm|{PRIMARY}|conn-jump' in {x['key'] for x in found} and not any(x['sig'] == 'pg-conn-split' for x in found)
    assert all('connSplit' not in r for r in rows)


def test_qan_reads_the_split_users_apart_and_leaves_them_out_of_the_main_read():
    # given
    rules = checks.QAN_RULE

    # when
    main = checks.qan_sql(NOW, True, '.*', ['pmm', 'bi'], rules)
    apart = checks.qan_sql(NOW, True, '.*', ['pmm'], rules, ['bi'])
    plain = checks.qan_sql(NOW, True, '.*', ['pmm'], rules)

    # then
    assert "username NOT IN ('pmm', 'bi')" in main and "username IN ('bi') AND username NOT IN ('pmm')" in apart
    assert 'username IN' not in plain.replace('NOT IN', '')


def test_split_qan_cards_are_info_only_and_keyed_apart_from_the_application_ones():
    # when
    found, top = checks.evaluate_qan(qan_data(), checks.QAN_RULE, '.*', None, True)
    plain, _ = checks.evaluate_qan(qan_data(), checks.QAN_RULE)

    # then
    assert {x['sev'] for x in found} == {'info'} and all(x['key'].endswith('|split') and x['short'].startswith('INFO ') and x['split'] for x in found)
    assert [x['key'] for x in found] == [x['key'] + '|split' for x in plain]
    assert all(r['split'] for r in top)


def qan_frame(rows: list[dict]) -> dict:
    return {'results': {'A': {'frames': [{'schema': {'fields': [{'name': n} for n in NAMES]}, 'data': {'values': [[r[n] for r in rows] for n in NAMES]}}]}}}


def row(db: str, qid: str, t0: float) -> dict:
    return dict(service_name=PRIMARY, database=db, queryid=qid, q='SELECT 1', c0=1000, t0=t0, c1=1000, t1=1, c2=1000, t2=1, pk0=0, pkat0=0, pkc0=0)


def test_a_split_user_load_opens_an_info_card_and_the_main_read_excludes_that_user(store: Store):
    # given
    sent = []

    def answer(params, body):
        sql = body['queries'][0]['rawSql']
        sent.append(sql)
        if 'queryid' not in sql:
            return {'results': {'A': {'frames': []}}}
        return qan_frame([row('reports_db', 'qs', 2500)] if "username IN ('bi')" in sql else [row('shop_db', 'qm', 5)])

    http = FakeHttp(pmm_routes(qan=answer))

    # when
    with app_with(store, http, main={'split-users': 'bi', 'exclude-users': 'pmm'}) as c:
        sweep(c, 'qan')
    cards = cards_of(store)

    # then
    key = f'qan|{PRIMARY}|load|qs|split'
    assert cards[key]['sev'] == 'info' and cards[key]['status'] == 'open'
    assert not [k for k in cards if k.endswith('|qm') or '|qm|' in k]
    assert any("username NOT IN ('pmm', 'bi')" in s for s in sent) and any("username IN ('bi')" in s for s in sent)


def test_without_split_users_the_qan_pass_sends_the_same_two_statements(store: Store):
    # given
    sent = []

    def answer(params, body):
        sent.append(body['queries'][0]['rawSql'])
        return {'results': {'A': {'frames': []}}}

    # when
    with app_with(store, FakeHttp(pmm_routes(qan=answer)), main={}) as c:
        sweep(c, 'qan')

    # then
    assert len(sent) == 2 and all("username NOT IN ('pmm')" in s and 'username IN' not in s.replace('NOT IN', '') for s in sent)


def test_the_app_name_of_a_qan_card_comes_from_the_database_name_by_the_pattern():
    # given
    pattern = checks.app_pattern(r'^(?P<app>[a-z_]+?)_db$')

    # then
    assert checks.qan_app('checkout_service_db', pattern) == 'checkout-service'
    assert checks.qan_app('legacy', pattern) == 'legacy' and checks.qan_app('weird_name', None) == 'weird-name' and checks.qan_app(None, pattern) == '?'
    found, _ = checks.evaluate_qan(qan_data(), checks.QAN_RULE, '.*', checks.app_pattern(r'^(?P<app>[a-z]+)_service$'))
    assert {x['svc'] for x in found if x.get('queryid')} == {'checkout', 'orders', 'customer'}


def test_a_qan_pattern_needs_an_app_group_and_a_slow_one_never_holds_the_pass_up():
    # given
    slow = checks.app_pattern('(?P<app>(a|aa)+)$')
    started = time.monotonic()

    # when
    names = [checks.qan_app('a' * 60 + '!', slow) for _ in range(300)]

    # then
    assert checks.app_pattern('^(.+)_db$') is None and checks.app_pattern('(') is None and checks.app_pattern('') is None
    assert names == ['a' * 60 + '!'] * 300 and time.monotonic() - started < 1


def test_the_test_refuses_a_qan_pattern_without_an_app_group_a_bad_host_header_and_a_plain_http_link_base(store: Store):
    # given
    http = FakeHttp(pmm_routes())

    # when
    group = connection_of(pmm_runtime(store, http, main={'qan-app': '^(.+)_db$'}))
    host = connection_of(pmm_runtime(store, http, main={'host-header': 'bad host/x'}))
    base = connection_of(pmm_runtime(store, http, main={'link-base': 'http://pmm.example.org'}))
    fine = connection_of(pmm_runtime(store, http, main={'qan-app': '^(?P<app>.+)_db$', 'host-header': 'pmm.internal:8443', 'link-base': 'https://pmm.example.org'}))

    # then
    assert (group.ok, host.ok, base.ok, fine.ok) == (False, False, False, True)
    assert 'group named app' in group.summary and 'Host header' in host.summary and 'Link base' in base.summary


def test_the_host_header_goes_with_every_request_and_never_when_empty(store: Store):
    # given
    with_host, without = FakeHttp(pmm_routes()), FakeHttp(pmm_routes())

    # when
    connection_of(pmm_runtime(store, with_host, main={'host-header': 'pmm.internal:8443'}))
    connection_of(pmm_runtime(store, without, main={}))

    # then
    assert with_host.calls and all(c[2].get('Host') == 'pmm.internal:8443' and c[2]['Authorization'].startswith('Bearer ') for c in with_host.calls)
    assert without.calls and all('Host' not in c[2] for c in without.calls)


def test_dashboard_links_open_the_link_base_and_fall_back_to_the_url():
    # given
    card = {'src': 'pmm', 'sig': 'pg-down', 'svc': PRIMARY}

    def links(**config):
        return pmm.links(card, InstanceInfo('main', True, None, {'url': URL, **config}))[0]['url']

    # then
    assert links().startswith(URL + '/graph/d/')
    assert links(**{'link-base': 'https://pmm.public.example.com/'}).startswith('https://pmm.public.example.com/graph/d/')
    for bad in ('http://pmm.public.example.com', 'ftp://x', 'https://user:pw@pmm.public.example.com', 'not a url', 'https://pmm.example.com:bad'):
        assert links(**{'link-base': bad}).startswith(URL + '/graph/d/')


def test_a_link_base_is_checked_when_it_is_saved_so_a_bad_one_never_widens_the_link_hosts():
    # given
    field = next(f for f in pmm.PLUGIN.fields if f.key == 'link-base')

    # then
    assert field.kind == 'url' and field.ui is True and isinstance(field, Field)
    assert _check(field, 'https://pmm.public.example.com') == 'https://pmm.public.example.com'
    for bad in ('ftp://pmm.example.com', 'not a url', 'https://user:pw@pmm.example.com', 'javascript:alert(1)'):
        with pytest.raises(InvalidInstance):
            _check(field, bad)
