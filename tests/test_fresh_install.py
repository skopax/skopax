from skopax import engine


def test_first_pass_on_empty_database_makes_a_card(store):
    # given
    found = [engine.anomaly('prom', 'err-rate', 'shop', 'checkout-service', '', 'crit', 12.0, 1.0, 1.0, '%', 'Error rate', 'Errors above 10%')]
    # when
    applied = engine.apply(store, [engine.PassResult('prom', True, 'ok', found=found)], now=1_800_000_000)
    state = store.load()
    # then
    assert applied is not None and state['initialized'] is True
    assert [a['svc'] for a in state['anomalies']] == ['checkout-service']
    assert applied.push == []


def test_fresh_install_boots_and_serves_empty_board(client):
    # given
    paths = ('/', '/api/state', '/api/server', '/c/1')
    # when
    codes = {path: client.get(path).status_code for path in paths}
    # then
    assert all(code in (200, 404) for code in codes.values()), codes
    assert codes['/api/state'] == 200
