import time

from skopax.plugins import airbyte
from skopax.plugins.airbyte import engine
from tests.fake_http import FakeHttp
from tests.test_airbyte_plugin import BASE, NOW, ORDERS, OTHER, SHOP, URL, WS, Ctx, data, fake_api, listed, routes

WS_TWO = '0a1b2c3d-0000-4000-8000-00000000a002'


def read(http: FakeHttp, watched=(), **kw):
    return engine.airbyte_pass(engine.Airbyte(URL, 'cid', 'secret', http), list(watched), 15, **kw)


def ids(got) -> set[str]:
    return {d['conn']['id'] for d in got.data}


def test_a_name_pattern_watches_the_connections_it_matches_and_nothing_else_when_the_list_is_empty():
    # when
    got = read(fake_api(), pattern=engine.name_pattern('^Orders|shop$'))

    # then
    assert ids(got) == {ORDERS, SHOP}


def test_the_pattern_adds_to_the_named_connections():
    # when
    got = read(fake_api(), ['Some other sync'], pattern=engine.name_pattern('^Orders'))

    # then
    assert ids(got) == {ORDERS, OTHER}


def test_no_pattern_and_no_list_still_watches_every_connection_and_a_listed_name_not_found_is_named():
    # when
    every = read(fake_api())
    named = read(fake_api(), ['Missing sync'], pattern=engine.name_pattern('^Orders'))

    # then
    assert ids(every) == {ORDERS, SHOP, OTHER}
    assert any(d.get('missing') for d in named.data) and ORDERS in ids(named)


def test_a_slow_pattern_does_not_match_instead_of_holding_the_pass_up():
    # given
    conns = [listed(f'{n:08d}-1111-4111-8111-000000000001', 'a' * 60 + '!') for n in range(200)]
    http = FakeHttp(routes(BASE, conns, {}))
    started = time.monotonic()

    # when
    got = read(http, pattern=engine.name_pattern('(a|aa)+$'))

    # then
    assert ids(got) == set() and time.monotonic() - started < 1


def test_a_workspace_filter_keeps_only_connections_of_the_named_workspaces():
    # given
    conns = [listed(ORDERS, 'Orders'), dict(listed(SHOP, 'Shop'), workspaceId=WS_TWO), dict(listed(OTHER, 'Other'), workspaceId=None)]

    # when
    first = read(FakeHttp(routes(BASE, conns, {})), workspaces=[WS])
    second = read(FakeHttp(routes(BASE, conns, {})), workspaces=[WS_TWO, 'nope'])
    every = read(FakeHttp(routes(BASE, conns, {})))
    named_elsewhere = read(FakeHttp(routes(BASE, conns, {})), ['Shop'], workspaces=[WS])

    # then
    assert ids(first) == {ORDERS} and ids(second) == {SHOP} and ids(every) == {ORDERS, SHOP, OTHER}
    assert ids(named_elsewhere) == set(), 'a watched name in another workspace is left out, not reported as missing'
    assert engine.workspaces_of(' a, B ,a,') == ['a', 'b']


def test_the_workspace_filter_ignores_case_and_a_repeated_severity_is_named():
    # given
    conns = [dict(listed(ORDERS, 'Orders'), workspaceId=WS.upper())]

    # when
    got = read(FakeHttp(routes(BASE, conns, {})), workspaces=engine.workspaces_of(WS))
    found, bad = engine.severities_of('Nightly=crit, nightly=info, other=warn')

    # then
    assert ids(got) == {ORDERS}
    assert found == {'nightly': 'crit', 'other': 'warn'} and bad == ['nightly=info']


def test_each_connection_can_carry_its_own_severity_by_name_or_id():
    # given
    severities = engine.severities_of(f'Orders to Warehouse=crit, {SHOP.upper()}=Info')[0]

    # when
    found, _ = engine.evaluate(data(), NOW, URL, engine.RULES, None, severities)
    plain, _ = engine.evaluate(data(), NOW, URL, engine.RULES)

    # then
    by = {x['connId']: x for x in found}
    assert (by[ORDERS]['sev'], by[SHOP]['sev']) == ('crit', 'info')
    assert by[ORDERS]['short'].startswith('CRIT Airbyte') and by[SHOP]['short'].startswith('INFO Airbyte')
    assert {x['sev'] for x in plain} == {'warn'} and all(x['short'].startswith('WARN Airbyte') for x in plain)
    assert [x['key'] for x in found] == [x['key'] for x in plain]


def test_a_severity_entry_that_cannot_be_read_is_named():
    # when
    found, bad = engine.severities_of('a=crit, b=urgent, c, d=WARN')

    # then
    assert found == {'a': 'crit', 'd': 'warn'} and bad == ['c', 'b=urgent']


def test_the_test_and_the_pass_refuse_a_pattern_or_severity_they_cannot_use():
    # given
    http = fake_api()
    values = {'client-id': 'cid', 'client-secret': 's', 'url': URL}

    # when
    bad_severity = airbyte.test(Ctx(http, dict(values, severities='a=urgent')))
    bad_pattern = airbyte.test(Ctx(http, dict(values, **{'connection-match': '('})))
    fine = airbyte.test(Ctx(http, dict(values, severities='a=crit', **{'connection-match': '^Orders'})))
    run = airbyte.run(Ctx(http, dict(values, **{'connection-match': '('})))

    # then
    assert (bad_severity.ok, bad_pattern.ok, fine.ok) == (False, False, True)
    assert 'a=urgent' in bad_severity.summary and 'Connection name pattern' in bad_pattern.summary
    assert (run.result.ok, run.result.status) == (False, 'error: connection-match is not a valid pattern')


def test_the_run_uses_the_pattern_and_severity_from_the_instance():
    # given
    http = fake_api()
    config = {'client-id': 'cid', 'client-secret': 's', 'url': URL, 'connection-match': '^Orders', 'severities': 'Orders to Warehouse=crit'}

    # when
    sweep = airbyte.run(Ctx(http, config))

    # then
    assert {a['connId'] for a in sweep.result.found} == {ORDERS}
    assert [a['sev'] for a in sweep.result.found] == ['crit']
    assert {r['id'] for r in sweep.sections['airbyte']} == {ORDERS}
