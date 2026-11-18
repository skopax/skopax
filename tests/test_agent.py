import hashlib
import json
import logging
import threading
import time
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skopax import agent, kube as kube_module, redact, remote
from skopax.agent import AgentCore, AgentError, RemoteAgent, agent_mode, create_agent, http_link, rev_of
from skopax.config import ConfigurationError
from skopax.contract import AgentRead
from skopax.messages import msg
from skopax.settingsources import FILE_ENV, Boot, boot as main_boot, light_boot
from skopax.version import RELEASE
from tests.agent_kit import AUTH, NOW, RELAY_HOSTS, ROUTES, TOKEN, FakeKube, Feed, Transport, agent_boot, agent_client, feed_plugin, relay_headers, run_asgi, run_main
from tests.plugin_kit import registry_with
from tests.plugins import demo_plugin


def test_agent_mode_reads_only_the_new_env_name():
    # then
    assert agent_mode({'SKOPAX_MODE': 'agent'}) is True
    assert agent_mode({'SKOPAX_MODE': ' Agent '}) is True
    assert agent_mode({'SKOPAX_MODE': 'main'}) is False
    assert agent_mode({}) is False


def test_agent_routes_are_exactly_the_v1_set_and_all_need_the_token():
    # given
    app = create_agent(agent_boot(), kube=FakeKube(), clock=lambda: NOW, refresh_every=None)
    keyless = TestClient(create_agent(agent_boot(env={}), kube=FakeKube(), clock=lambda: NOW, refresh_every=None))
    client = TestClient(app)

    # when
    found = {(m, r.path) for r in app.routes for m in getattr(r, 'methods', ()) if m != 'HEAD'}
    guarded = [('GET', '/api/agent'), ('POST', '/api/kube'), ('POST', '/api/read'), ('PUT', '/api/agent/sources')]
    wrong = [client.request(m, p, json={}, headers={'Authorization': 'Bearer nope'}) for m, p in guarded]
    bare = [client.request(m, p, json={}) for m, p in guarded]
    none = [keyless.request(m, p, json={}, headers=AUTH) for m, p in guarded]
    relayed = [c.request(m, '/api/relay/api/agent', json={}, headers=h) for c, h in ((client, {'Authorization': 'Bearer nope'}), (client, {}), (keyless, AUTH)) for m in ('GET', 'POST', 'PUT')]

    # then
    assert found == ROUTES
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
    assert [r.status_code for r in wrong] == [401] * 4 and all(r.json() == {'error': 'unauthorized'} for r in wrong)
    assert [r.status_code for r in bare] == [401] * 4
    assert [r.status_code for r in none] == [503] * 4 and all('SKOPAX_AGENT_TOKEN' in r.json()['error'] for r in none)
    assert [r.status_code for r in relayed] == [401] * 9 and all(r.json() == {'error': 'unauthorized', 'relay': True} for r in relayed)
    assert client.get('/actuator/health/liveness').json() == {'status': 'UP'}
    ready = client.get('/actuator/health/readiness')
    assert (ready.status_code, ready.json()) == (200, {'status': 'UP'})


def test_health_is_open_and_reports_kube_and_token():
    # given
    on = agent_client()
    off = TestClient(create_agent(agent_boot(env={}), kube=FakeKube(enabled=False), clock=lambda: NOW, refresh_every=None))

    # then
    assert on.get('/actuator/health').json() == {'status': 'UP', 'mode': 'agent', 'kube': True, 'token': True}
    assert off.get('/actuator/health').json() == {'status': 'UP', 'mode': 'agent', 'kube': False, 'token': False}


def test_agent_info_shape():
    # given
    boot = agent_boot(env={'SKOPAX_AGENT_TOKEN': TOKEN, 'COMMIT_ID': 'abc123'})
    client = agent_client(boot)
    down = agent_client(boot, kube=FakeKube(enabled=False))

    # when
    body = client.get('/api/agent', headers=AUTH).json()
    failed = down.get('/api/agent', headers=AUTH).json()

    # then
    assert set(body) == {'version', 'release', 'startedAt', 'kube', 'reads', 'sourcesRev', 'cluster'}
    assert (body['version'], body['release'], body['kube'], body['sourcesRev']) == ('abc123', RELEASE, True, '')
    assert body['startedAt'] == '2023-11-14T22:13:20Z'
    assert body['reads'] == {'demo-echo': []}
    assert body['cluster']['total'] == 2 and body['cluster']['ready'] == 1 and body['cluster']['notReady'] == ['n2']
    assert set(failed) == {'version', 'release', 'startedAt', 'kube', 'reads', 'sourcesRev', 'clusterError'}
    assert failed['kube'] is False and failed['clusterError'] == 'kube is off'


def test_kube_ops_and_arg_checks():
    # given
    kube = FakeKube()
    client = agent_client(kube=kube)

    def post(body, headers=AUTH):
        return client.post('/api/kube', json=body, headers=headers)

    # when
    ok = [post({'op': 'pods', 'args': {'namespace': 'shop', 'selector': 'app=checkout-service'}}), post({'op': 'pod', 'args': {'namespace': 'shop', 'name': 'checkout-service-1'}}),
          post({'op': 'events', 'args': {'namespace': 'shop', 'since_minutes': 15}}), post({'op': 'rollout', 'args': {'namespace': 'shop', 'deployment': 'checkout-service'}}),
          post({'op': 'previous_logs', 'args': {'namespace': 'shop', 'pod': 'p', 'tail': 5}}), post({'op': 'nodes'}), post({'op': 'node', 'args': {'name': 'n1'}}),
          post({'op': 'daemonset', 'args': {'namespace': 'shop', 'name': 'agent'}})]
    refused = post({'op': 'pod', 'args': {'namespace': 'shop', 'name': 'forbidden'}})
    not_json = client.post('/api/kube', content=b'nope', headers=AUTH)
    unknown = post({'op': 'delete'})
    not_object = post([1])
    bad = [post({'op': 'pods', 'args': {}}), post({'op': 'pods', 'args': {'namespace': 'shop', 'extra': 'x'}}), post({'op': 'pods', 'args': 'x'}),
           post({'op': 'events', 'args': {'namespace': 'shop', 'since_minutes': '5'}}), post({'op': 'events', 'args': {'namespace': 'shop', 'since_minutes': True}}),
           post({'op': 'pods', 'args': {'namespace': 'x' * 201}}), post({'op': 'nodes', 'args': {'namespace': 'shop'}}), post({'op': 'pod', 'args': {'namespace': 'shop'}})]

    # then
    assert [r.status_code for r in ok] == [200] * 8 and all(set(r.json()) == {'data'} for r in ok)
    assert kube.calls[0] == ('pods', 'shop', 'app=checkout-service')
    assert kube.calls[2] == ('events', 'shop', None, 15, NOW)
    assert kube.calls[4] == ('previous_logs', 'shop', 'p', None, 5)
    assert (refused.status_code, refused.json()) == (400, {'error': 'pods is forbidden', 'code': 403})
    assert not_json.status_code == 400 and set(not_json.json()) == {'error'}
    assert unknown.status_code == 404 and 'op must be one of' in unknown.json()['error']
    assert not_object.status_code == 404
    assert [r.status_code for r in bad] == [422] * 8 and all(set(r.json()) == {'error'} for r in bad)
    assert len(kube.calls) == 9


def test_agent_reads_come_from_plugins_and_take_pushed_config():
    # given
    client = agent_client()

    def read(op, args):
        return client.post('/api/read', json={'op': op, 'args': args}, headers=AUTH)

    # when
    off = read('demo-echo', {'text': 'hi'})
    pushed = client.put('/api/agent/sources', json={'rev': 'r1', 'config': {'url': 'https://example.com', 'token': 'secret-value'}}, headers=AUTH)
    on = read('demo-echo', {'text': 'hi'})
    info = client.get('/api/agent', headers=AUTH).json()
    unknown = read('esql', {})
    odd = read('demo-echo', {'other': 'x'})
    not_object = read('demo-echo', 'x')
    not_json = client.post('/api/read', content=b'nope', headers=AUTH)
    cleared = client.put('/api/agent/sources', json={'rev': '', 'config': {}}, headers=AUTH)
    again = read('demo-echo', {'text': 'hi'})

    # then
    assert (off.status_code, set(off.json())) == (503, {'error'}) and 'demo-echo' in off.json()['error']
    assert (pushed.status_code, pushed.json()) == (200, {'rev': 'r1', 'reads': {'demo-echo': ['demo']}})
    assert (on.status_code, on.json()) == (200, {'data': {'echo': 'hi'}})
    assert (info['sourcesRev'], info['reads']) == ('r1', {'demo-echo': ['demo']})
    assert (unknown.status_code, set(unknown.json())) == (404, {'error'})
    assert (odd.status_code, set(odd.json())) == (422, {'error'})
    assert not_object.status_code == 422
    assert (not_json.status_code, set(not_json.json())) == (400, {'error'})
    assert (cleared.json(), again.status_code) == ({'rev': '', 'reads': {'demo-echo': []}}, 503)


def test_a_push_takes_only_declared_keys_and_checks_values():
    # given
    client = agent_client()

    def push(config, rev='r'):
        return client.put('/api/agent/sources', json={'rev': rev, 'config': config}, headers=AUTH)

    # when
    bad = [push(None), push([]), push('x'), push({'unknown': 'x'}), push({'url': 1}), push({'url': 'x' * 2001}), push({f'k{i}': 'v' for i in range(61)}), push({'url': 'https://example.com', 'extra': 'x'})]
    edge = push({'url': 'x' * 2000})
    not_json = client.put('/api/agent/sources', content=b'nope', headers=AUTH)

    # then
    assert [r.status_code for r in bad] == [422] * 8 and all(set(r.json()) == {'error'} for r in bad)
    assert edge.status_code == 200
    assert (not_json.status_code, set(not_json.json())) == (400, {'error'})


def test_a_push_serves_a_read_only_when_it_holds_one_of_its_keys_and_never_merges_with_the_agents_own():
    # given
    seen: list[dict] = []

    def run(config, args):
        seen.append(dict(config))
        return {'ok': True}

    plugin = replace(demo_plugin.PLUGIN, agent_reads=(replace(demo_plugin.PLUGIN.agent_reads[0], run=run),))
    boot = agent_boot(values={'url': 'https://own.example.com', 'token': 'own-token', 'unrelated': 'x'}, plugins=(plugin,))
    client = agent_client(boot)

    def call():
        return client.post('/api/read', json={'op': 'demo-echo', 'args': {}}, headers=AUTH)

    # when
    own = call()
    client.put('/api/agent/sources', json={'rev': 'a', 'config': {'url': 'https://pushed.example.com'}}, headers=AUTH)
    only_url = call()
    client.put('/api/agent/sources', json={'rev': 'b', 'config': {'token': 'pushed-token'}}, headers=AUTH)
    only_token = call()
    client.put('/api/agent/sources', json={'rev': 'c', 'config': {'url': 'https://pushed.example.com', 'token': 'pushed-token'}}, headers=AUTH)
    both = call()
    client.put('/api/agent/sources', json={'rev': '', 'config': {}}, headers=AUTH)
    back = call()

    # then
    assert [r.status_code for r in (own, only_url, only_token, both, back)] == [200] * 5
    assert seen == [{'url': 'https://own.example.com', 'token': 'own-token'}, {'url': 'https://pushed.example.com'}, {'token': 'pushed-token'},
                    {'url': 'https://pushed.example.com', 'token': 'pushed-token'}, {'url': 'https://own.example.com', 'token': 'own-token'}]
    assert not any(c.get('url') == 'https://pushed.example.com' and 'own-token' in c.values() for c in seen)


def test_a_read_failure_gives_its_status_and_never_its_secrets():
    # given
    from skopax.contract import ReadError

    def run(config, args):
        if args.get('text') == 'status':
            raise ReadError(400, 'source said no')
        raise RuntimeError('boom with secret-value')

    plugin = replace(demo_plugin.PLUGIN, agent_reads=(replace(demo_plugin.PLUGIN.agent_reads[0], run=run),))
    client = agent_client(agent_boot(values={'url': 'https://example.com', 'token': 'secret-value'}, plugins=(plugin,)))

    # when
    known = client.post('/api/read', json={'op': 'demo-echo', 'args': {'text': 'status'}}, headers=AUTH)
    other = client.post('/api/read', json={'op': 'demo-echo', 'args': {'text': 'x'}}, headers=AUTH)

    # then
    assert (known.status_code, known.json()) == (400, {'error': 'source said no'})
    assert other.status_code == 500 and 'secret-value' not in other.text


def test_relay_forwards_only_the_agent_api_to_allowed_hosts():
    # given
    transport = Transport({'data': 1})
    client = agent_client(transport=transport)
    body = {'op': 'pods', 'args': {'namespace': 'shop'}}

    # when
    sent = client.post('/api/relay/api/kube', json=body, headers=relay_headers())
    info = client.get('/api/relay/api/agent', headers=relay_headers('https://agent.example.com/'))
    read = client.post('/api/relay/api/read', json=body, headers=relay_headers('https://a.example.org:443'))
    push = client.put('/api/relay/api/agent/sources', json={'rev': 'r', 'config': {}}, headers=relay_headers())
    other_path = client.post('/api/relay/api/other', json=body, headers=relay_headers())
    other_method = client.get('/api/relay/api/kube', headers=relay_headers())
    refused = [client.post('/api/relay/api/kube', json=body, headers=relay_headers(to)) for to in
               ('http://agent.example.com', 'https://agent.example.com:8443', 'https://user@agent.example.com', 'https://agent.example.com/x', 'https://agent.example.com?x=1',
                'https://agent.example.com#f', 'https://other.example.net', '', 'https://example.org')]
    no_inner = client.post('/api/relay/api/kube', json=body, headers={**AUTH, 'X-Relay-To': 'https://agent.example.com'})
    short_inner = client.post('/api/relay/api/kube', json=body, headers=relay_headers(inner='Bearer '))
    not_json = client.post('/api/relay/api/kube', content=b'nope', headers=relay_headers())
    empty = TestClient(create_agent(agent_boot(env={'SKOPAX_AGENT_TOKEN': TOKEN}), kube=FakeKube(), transport=transport, clock=lambda: NOW, refresh_every=None))
    nobody = empty.post('/api/relay/api/kube', json=body, headers=relay_headers())

    # then
    assert [r.json() for r in (sent, info, read, push)] == [{'data': 1}] * 4
    assert [(c[0], c[1], c[2]['Authorization'], c[4]) for c in transport.calls] == [
        ('POST', 'https://agent.example.com/api/kube', 'Bearer inner-token-0002', 30), ('GET', 'https://agent.example.com/api/agent', 'Bearer inner-token-0002', 15),
        ('POST', 'https://a.example.org:443/api/read', 'Bearer inner-token-0002', 75), ('PUT', 'https://agent.example.com/api/agent/sources', 'Bearer inner-token-0002', 15)]
    assert json.loads(transport.calls[0][3]) == body and transport.calls[1][3] is None
    assert [r.status_code for r in (other_path, other_method)] == [404, 404]
    assert [r.status_code for r in refused] == [400] * 9 and all(r.json()['relay'] is True and 'SKOPAX_AGENT_RELAY_HOSTS' in r.json()['error'] for r in refused)
    assert (no_inner.status_code, no_inner.json()['relay']) == (400, True)
    assert (short_inner.status_code, short_inner.json()['relay']) == (400, True)
    assert (not_json.status_code, set(not_json.json())) == (400, {'error'})
    assert (nobody.status_code, nobody.json()['relay']) == (400, True)
    assert len(transport.calls) == 4


def test_a_relayed_target_error_passes_through_and_an_unreachable_target_is_502():
    # given
    body = {'op': 'nodes'}
    refused = agent_client(transport=Transport(remote.HttpError(401, 'https://agent.example.com/api/kube', json.dumps({'error': 'unauthorized'}))))
    gone = agent_client(transport=Transport(remote.Unreachable('https://agent.example.com/api/kube unreachable (timed out)')))

    # when
    passed = refused.post('/api/relay/api/kube', json=body, headers=relay_headers())
    unreachable = gone.post('/api/relay/api/kube', json=body, headers=relay_headers())

    # then
    assert (passed.status_code, passed.json()) == (401, {'error': 'unauthorized'})
    assert unreachable.status_code == 502 and unreachable.json()['relay'] is True and 'agent.example.com' in unreachable.json()['error']


def test_agent_token_from_env_or_source():
    # given
    from_env = agent_client(agent_boot(env={'SKOPAX_AGENT_TOKEN': ' env-token-0003 '}, values={'agent.token': 'source-token-4'}))
    blank = agent_client(agent_boot(env={'SKOPAX_AGENT_TOKEN': '   '}, values={'agent.token': TOKEN}))
    feed = Feed()
    late = agent_client(agent_boot(env={}, feed=feed))

    # when
    env_ok = from_env.get('/api/agent', headers={'Authorization': 'Bearer env-token-0003'}).status_code
    source_ignored = from_env.get('/api/agent', headers={'Authorization': 'Bearer source-token-4'}).status_code
    blank_ok = blank.get('/api/agent', headers=AUTH).status_code
    missing = late.get('/api/agent', headers=AUTH).status_code
    feed.values['agent.token'] = ' ' + TOKEN + ' '
    before = late.get('/api/agent', headers=AUTH).status_code
    late.app.state.refresher.run_once()
    found = late.get('/api/agent', headers=AUTH).status_code
    health = late.get('/actuator/health').json()['token']

    # then
    assert (env_ok, source_ignored, blank_ok, missing, before, found, health) == (200, 401, 200, 503, 503, 200, True)
    assert feed.calls == 2


def test_relay_hosts_come_from_env_or_the_source_and_follow_a_refresh_only_after_it_runs():
    # given
    transport = Transport({'ok': 1})
    feed = Feed({'agent.relay-hosts': 'one.example.com, two.example.com'})
    client = agent_client(agent_boot(env={'SKOPAX_AGENT_TOKEN': TOKEN}, feed=feed), transport=transport)
    body = {'op': 'nodes'}

    def to(host):
        return client.post('/api/relay/api/kube', json=body, headers=relay_headers(f'https://{host}')).status_code

    # when
    allowed = to('two.example.com')
    refused = to('agent.example.com')
    feed.values['agent.relay-hosts'] = 'agent.example.com'
    stale = (to('agent.example.com'), to('two.example.com'))
    client.app.state.refresher.run_once()
    fresh = (to('agent.example.com'), to('two.example.com'))

    # then
    assert (allowed, refused) == (200, 400)
    assert stale == (400, 200)
    assert fresh == (200, 400)


def test_the_refresher_runs_in_a_background_thread_so_a_slow_source_never_stalls_a_request():
    # given
    gate = threading.Event()
    feed = Feed()
    boot = agent_boot(env={}, feed=feed)
    feed.values['agent.token'] = TOKEN
    feed.hold = gate
    client = TestClient(create_agent(boot, kube=FakeKube(), clock=lambda: NOW, refresh_every=0.01))
    client.app.state.refresher.start()

    # when
    assert feed.held.wait(5)
    began = time.monotonic()
    health = client.get('/actuator/health').json()
    waited = time.monotonic() - began
    gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get('/api/agent', headers=AUTH).status_code != 200:
        time.sleep(0.01)
    final = client.get('/api/agent', headers=AUTH).status_code
    client.app.state.refresher.stop()

    # then
    assert health['token'] is False and waited < 1
    assert final == 200


def test_agent_mode_needs_no_mongo(monkeypatch):
    # given
    import pymongo

    def refuse(*args, **kwargs):
        raise AssertionError('the agent must not touch Mongo')

    monkeypatch.setattr(pymongo, 'MongoClient', refuse)
    monkeypatch.setattr('skopax.app.MongoClient', refuse, raising=False)
    monkeypatch.setenv('MONGO_URI', 'mongodb://unused')

    # when
    client = agent_client()
    answers = [client.get('/actuator/health').status_code, client.get('/api/agent', headers=AUTH).status_code, client.post('/api/kube', json={'op': 'nodes'}, headers=AUTH).status_code]

    # then
    assert answers == [200, 200, 200]


def direct_link(core: AgentCore, token: str = TOKEN):
    def link(method, path, body, timeout):
        code, out = core.dispatch(method, path, f'Bearer {token}', body)
        if code >= 400:
            raise remote.HttpError(code, path, json.dumps(out))
        return out

    return link


def core_for(boot: Boot, kube: FakeKube | None = None) -> AgentCore:
    return AgentCore(boot.registry, kube or FakeKube(), lambda: TOKEN, lambda: ('agent.example.com',), lambda: {}, clock=lambda: NOW)


def test_dispatch_runs_without_http():
    # given
    kube = FakeKube()
    core = core_for(agent_boot(), kube)
    client = RemoteAgent(direct_link(core), 'east')
    over_http = RemoteAgent(http_link('http://agent.test', TOKEN, lambda method, url, headers, data, timeout: remote.Reply(
        TestClient(create_agent(agent_boot(), kube=FakeKube(), clock=lambda: NOW, refresh_every=None)).request(method, url.removeprefix('http://agent.test'), headers=headers, content=data).json(), {})), 'east')

    # when
    info = client.info()
    nodes = client.kube('nodes')
    pods = client.kube('pods', namespace='shop', selector=None)
    pushed = client.push_sources('r1', {'url': 'https://example.com', 'token': 'secret-value'})
    echoed = client.read('demo-echo', {'text': 'hi', 'skip': None})
    wrong = RemoteAgent(direct_link(core, 'wrong-token-0009'), 'east')

    # then
    assert (info['release'], info['reads']) == (RELEASE, {'demo-echo': []})
    assert [n['name'] for n in nodes] == ['n1', 'n2'] and pods == [{'name': 'checkout-service-1'}]
    assert kube.calls[-1] == ('pods', 'shop', None)
    assert pushed == {'rev': 'r1', 'reads': {'demo-echo': ['demo']}} and echoed == {'echo': 'hi'}
    assert over_http.info()['reads'] == info['reads']
    for call, args in ((wrong.info, ()), (lambda: wrong.kube('nodes'), ())):
        with pytest.raises(AgentError) as e:
            call(*args)
        assert e.value.code == 401 and 'the east agent refused Skopax' in str(e.value)


def test_an_unreachable_agent_is_logged_with_its_error_type_and_never_the_address(caplog):
    # given
    def link(method, path, payload, timeout):
        raise remote.Unreachable('https://user:SENTINEL-77@agent.example.com/api/agent unreachable (timed out)')

    # when
    with caplog.at_level(logging.WARNING), pytest.raises(AgentError) as gone:
        RemoteAgent(link, 'east', 'hub').info()

    # then
    assert str(gone.value) == 'the east agent is unreachable through the hub agent'
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == 'skopax.agent'] == [(logging.WARNING, 'the east agent is unreachable through the hub agent: Unreachable')]
    assert 'SENTINEL' not in caplog.text


def test_agent_error_texts_name_the_instance_and_the_hop():
    # given
    def failing(code, body='', unreachable=False, missing=''):
        def link(method, path, payload, timeout):
            if missing:
                raise agent.NotConfigured(missing)
            if unreachable:
                raise remote.Unreachable('https://agent.example.com/api/agent unreachable (timed out)')
            raise remote.HttpError(code, path, body)

        return link

    def error(link, relay=''):
        with pytest.raises(AgentError) as e:
            RemoteAgent(link, 'east', relay).info()
        return e.value

    # when
    refused = error(failing(401, json.dumps({'error': 'unauthorized'})))
    via_refused = error(failing(401, json.dumps({'error': 'unauthorized', 'relay': True})), 'hub')
    via_failed = error(failing(502, json.dumps({'error': 'it could not reach x', 'relay': True})), 'hub')
    cluster = error(failing(400, json.dumps({'error': 'pods is forbidden', 'code': 403})))
    answered = error(failing(422, json.dumps({'error': 'bad args'})))
    plain = error(failing(500, 'oops'))
    gone = error(failing(0, unreachable=True))
    gone_via = error(failing(0, unreachable=True), 'hub')
    unset = error(failing(0, missing='agent'))
    unset_relay = error(failing(0, missing='relay'), 'hub')

    # then
    assert (str(refused), refused.code) == ('the east agent refused Skopax (its token does not match the agent token)', 401)
    assert (str(via_refused), via_refused.code) == ('the hub agent, which relays to the east agent, refused Skopax (its own token does not match)', 401)
    assert (str(via_failed), via_failed.code) == ('the hub agent could not relay to the east agent: it could not reach x', 502)
    assert (str(cluster), cluster.code) == ('pods is forbidden', 403)
    assert (str(answered), answered.code) == ('the east agent answered HTTP 422: bad args', 422)
    assert (str(plain), plain.code) == ('the east agent answered HTTP 500', 500)
    assert str(gone) == 'the east agent is unreachable'
    assert str(gone_via) == 'the east agent is unreachable through the hub agent'
    assert 'the east agent is not configured' in str(unset)
    assert 'reached through the hub agent, which has no URL or token' in str(unset_relay)


def test_http_link_goes_direct_or_through_a_relay():
    # given
    transport = Transport({'ok': 1})
    direct = http_link('https://east.example.com/', 'east-token', transport)
    relayed = http_link('https://east.example.com/', 'east-token', transport, via=('https://hub.example.com/', 'hub-token'))

    # when
    direct('GET', '/api/agent', None, 15)
    relayed('POST', '/api/kube', {'op': 'nodes'}, 30)

    # then
    assert transport.calls[0][:2] == ('GET', 'https://east.example.com/api/agent')
    assert transport.calls[0][2]['Authorization'] == 'Bearer east-token' and transport.calls[0][4] == 15
    method, url, headers, data, timeout = transport.calls[1]
    assert (method, url, timeout) == ('POST', 'https://hub.example.com/api/relay/api/kube', 40)
    assert headers['Authorization'] == 'Bearer hub-token' and headers['X-Relay-To'] == 'https://east.example.com' and headers['X-Relay-Authorization'] == 'Bearer east-token'
    assert json.loads(data) == {'op': 'nodes'}
    for bad in (http_link('', 'token', transport), http_link('https://east.example.com', '', transport)):
        with pytest.raises(agent.NotConfigured):
            bad('GET', '/api/agent', None, 15)
    with pytest.raises(agent.NotConfigured):
        http_link('https://east.example.com', 'token', transport, via=('', ''))('GET', '/api/agent', None, 15)


def test_rev_of_matches_the_v1_hash():
    # then
    assert rev_of({}) == ''
    assert rev_of({'b': '2', 'a': '1'}) == rev_of({'a': '1', 'b': '2'})
    assert rev_of({'a': '1', 'b': '2'}) == hashlib.sha256(json.dumps({'a': '1', 'b': '2'}, sort_keys=True).encode()).hexdigest()[:16]
    assert len(rev_of({'a': '1'})) == 16


def test_main_starts_the_agent_app_in_agent_mode(monkeypatch):
    # given
    import pymongo

    monkeypatch.setenv('SKOPAX_MODE', 'agent')
    monkeypatch.setattr(pymongo, 'MongoClient', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no mongo in agent mode')))

    # when
    app = run_main(monkeypatch, registry_with(demo_plugin, feed_plugin(Feed())))['app']

    # then
    assert isinstance(app, FastAPI)
    assert ('GET', '/api/agent') in {(m, r.path) for r in app.routes for m in getattr(r, 'methods', ())}
    assert TestClient(app).get('/actuator/health').json()['mode'] == 'agent'
    app.state.refresher.stop()


def relay_answer(code, raw):
    def transport(method, url, headers, data, timeout):
        raise remote.HttpError(code, url, raw)

    core = AgentCore(registry_with(demo_plugin), FakeKube(), lambda: TOKEN, lambda: ('agent.example.com',), lambda: {}, transport=transport, clock=lambda: NOW)
    return core.dispatch('POST', '/api/relay/api/kube', f'Bearer {TOKEN}', {'op': 'nodes'}, 'https://agent.example.com', 'Bearer inner')


def main_view(code, raw):
    def link(method, path, body, timeout):
        raise remote.HttpError(code, path, raw)

    with pytest.raises(AgentError) as e:
        RemoteAgent(link, 'east', 'hub').kube('nodes')
    return str(e.value), e.value.code


@pytest.mark.parametrize('code,raw', [(400, '<html><body><h1>400 Bad Request</h1></body></html>'), (500, ''), (503, '<html>503 Service Temporarily Unavailable</html>'), (422, json.dumps([1, 2]))])
def test_a_relayed_non_object_error_body_passes_through_as_the_target_sent_it(code, raw):
    # when
    status, out = relay_answer(code, raw)

    # then
    assert (status, out) == (code, raw) and isinstance(out, agent.RawBody)
    assert main_view(status, out) == main_view(code, raw)
    assert main_view(status, out)[1] == code


def test_the_relay_route_answers_a_non_object_error_body_byte_for_byte():
    # given
    raw = '<html>503 Service Temporarily Unavailable</html>'
    client = agent_client(transport=Transport(remote.HttpError(503, 'https://agent.example.com/api/kube', raw)))

    # when
    res = client.post('/api/relay/api/kube', json={'op': 'nodes'}, headers=relay_headers())

    # then
    assert (res.status_code, res.text, res.headers['content-type']) == (503, raw, 'application/json')


def test_a_relayed_object_error_body_passes_through_unchanged():
    # when
    status, out = relay_answer(400, json.dumps({'error': 'pods is forbidden', 'code': 403}))

    # then
    assert (status, out) == (400, {'error': 'pods is forbidden', 'code': 403})


NESTED = b'[' * 200_000


@pytest.mark.parametrize('method,path', [('POST', '/api/kube'), ('POST', '/api/read'), ('PUT', '/api/agent/sources'), ('POST', '/api/relay/api/kube'), ('PUT', '/api/relay/api/agent/sources')])
def test_the_token_is_checked_before_the_body_is_read(method, path):
    # given
    client = agent_client()
    keyless = agent_client(agent_boot(env={}))
    relay = path.startswith('/api/relay')
    bad = {'Authorization': 'Bearer nope'}

    # when
    answers = [client.request(method, path, content=body, headers=h) for body in (b'not json', NESTED) for h in (bad, {})]
    nothing = [keyless.request(method, path, content=body, headers=AUTH) for body in (b'not json', NESTED)]
    valid = [client.request(method, path, content=body, headers={**AUTH, **(relay_headers() if relay else {})}) for body in (b'not json', NESTED)]

    # then
    assert [r.status_code for r in answers] == [401] * 4
    assert all(r.json().get('relay') is relay or ('relay' not in r.json() and not relay) for r in answers)
    assert [r.status_code for r in nothing] == ([401] * 2 if relay else [503] * 2)
    assert [r.status_code for r in valid] == [400, 400]


def test_every_target_the_relay_refuses_never_reaches_the_transport():
    # given
    transport = Transport({'ok': 1})
    client = agent_client(transport=transport)
    refused = ['https://@agent.example.com', 'https://:@agent.example.com', 'https://user:pw@agent.example.com', 'https://a.example.org:443@evil.test', 'https://agent.example.com?', 'https://agent.example.com#',
               'https://agent.example.com?a=1', 'https://agent.example.com#f', 'https://agent.example.com:', 'https://agent.example.com:0443', 'https://agent.example.com:8443', 'https://agent.example.com:99999',
               'https://agent.example.com:-1', 'http://agent.example.com', 'https://agent.example.com/x', 'https://evil.test\\.example.org', 'https://evil.test%2f.example.org', 'https://agent.exa\tmple.com',
               'https://evil.test;.example.org', 'https://evil.test#@a.example.org', 'https://evil.test?@a.example.org', 'https://[::1]', 'https://other.example.net', 'https://example.org', 'https://a.example.org.evil.test',
               'https://agent.example.com\\@evil.test', 'https://agent_x.example.com', 'ftp://agent.example.com', '//agent.example.com']

    # when
    answers = [client.get('/api/relay/api/agent', headers=relay_headers(to)) for to in refused]

    # then
    assert [(to, r.status_code) for to, r in zip(refused, answers)] == [(to, 400) for to in refused]
    assert transport.calls == []


def test_an_allowed_target_is_rebuilt_as_https_host_with_a_port_only_when_given():
    # given
    transport = Transport({'ok': 1})
    client = agent_client(transport=transport)

    # when
    for to in ('HTTPS://AGENT.EXAMPLE.COM', 'https://agent.example.com/', 'https://Agent.Example.com:443', 'https://a.example.org:443/'):
        client.get('/api/relay/api/agent', headers=relay_headers(to))

    # then
    assert [c[1] for c in transport.calls] == ['https://agent.example.com/api/agent', 'https://agent.example.com/api/agent', 'https://agent.example.com:443/api/agent', 'https://a.example.org:443/api/agent']


def test_relay_host_globs_match_without_regard_to_case():
    # given
    transport = Transport({'ok': 1})
    client = agent_client(agent_boot(env={'SKOPAX_AGENT_TOKEN': TOKEN, 'SKOPAX_AGENT_RELAY_HOSTS': 'AGENT.Example.com'}), transport=transport)

    # then
    assert client.get('/api/relay/api/agent', headers=relay_headers('https://agent.example.com')).status_code == 200


def test_the_relay_error_names_the_setting_the_hosts_came_from():
    # given
    both = agent_client(agent_boot(env={'SKOPAX_AGENT_TOKEN': TOKEN, 'SKOPAX_AGENT_RELAY_HOSTS': 'a.example.com'}, values={'agent.relay-hosts': 'b.example.com'}))
    source = agent_client(agent_boot(env={'SKOPAX_AGENT_TOKEN': TOKEN}, values={'agent.relay-hosts': 'b.example.com'}))
    neither = agent_client(agent_boot(env={'SKOPAX_AGENT_TOKEN': TOKEN}))

    # when
    texts = [c.get('/api/relay/api/agent', headers=relay_headers('https://zzz.example.net')).json()['error'] for c in (both, source, neither)]

    # then
    assert 'SKOPAX_AGENT_RELAY_HOSTS' in texts[0] and 'agent.relay-hosts' not in texts[0]
    assert 'agent.relay-hosts' in texts[1] and 'SKOPAX_AGENT_RELAY_HOSTS' not in texts[1]
    assert 'SKOPAX_AGENT_RELAY_HOSTS' in texts[2] and 'agent.relay-hosts' in texts[2]


def test_an_agent_with_no_plugin_reads_says_so():
    # given
    client = agent_client(agent_boot(plugins=()))

    # when
    out = client.post('/api/read', json={'op': 'esql', 'args': {}}, headers=AUTH)

    # then
    assert out.status_code == 404
    assert out.json()['error'] == 'this agent has no source reads'


def reads_plugin(*keys, fields=()):
    return replace(demo_plugin.PLUGIN, agent_reads=(AgentRead('demo-echo', ('text',), keys, demo_plugin.agent_sources, demo_plugin.agent_echo, fields=fields),))


def test_agent_read_keys_are_patterns_matched_in_full():
    # given
    plugin = reads_plugin('url', r'bo\.[a-z]+\.url', 'k[0-9]+')
    client = agent_client(agent_boot(plugins=(plugin,)))

    def push(key):
        return client.put('/api/agent/sources', json={'rev': 'r', 'config': {key: 'v'}}, headers=AUTH).status_code

    # when
    accepted = [push(k) for k in ('url', 'bo.dev.url', 'k7', 'k42')]
    refused = [push(k) for k in ('urlx', 'xurl', 'bo.dev.url.x', 'boXdevXurl', 'bo.dev.', 'k', 'k7x', 'u.l', 'bo.dev.u')]

    # then
    assert accepted == [200] * 4
    assert refused == [422] * 9


def test_a_push_of_exactly_sixty_keys_is_taken_and_sixty_one_is_refused():
    # given
    client = agent_client(agent_boot(plugins=(reads_plugin('k[0-9]+'),)))

    def push(count):
        return client.put('/api/agent/sources', json={'rev': 'r', 'config': {f'k{i}': 'v' for i in range(count)}}, headers=AUTH).status_code

    # then
    assert (push(60), push(61)) == (200, 422)


def test_an_invalid_key_pattern_marks_the_plugin_broken_at_load():
    # given
    from skopax.registry import Registry

    # when
    found = [Registry.of(reads_plugin(key)).get('demo') for key in ('k[0-9', '(', 'ok', 5)]

    # then
    assert [x.state for x in found] == ['broken', 'broken', 'ok', 'broken']
    assert 'k[0-9' in found[0].reason and 'agent read key' in found[0].reason


@pytest.mark.parametrize('header', ['bearer ' + TOKEN, 'Bearer  ' + TOKEN, 'Bearer' + TOKEN, TOKEN, 'Basic ' + TOKEN, 'Bearer ' + TOKEN + ' ', ' Bearer ' + TOKEN, 'Bearer ' + TOKEN[:-1], 'Bearer ' + TOKEN + 'x'])
def test_only_the_exact_bearer_prefix_and_token_pass(header):
    # given
    client = agent_client()

    # then
    assert client.get('/api/agent', headers={'Authorization': header}).status_code == 401
    assert client.get('/api/relay/api/agent', headers={'Authorization': header, 'X-Relay-To': 'https://agent.example.com', 'X-Relay-Authorization': 'Bearer i'}).status_code == 401


def test_logs_never_carry_exception_text(caplog):
    # given
    def boom(config, args):
        raise RuntimeError('leak secret-value in run')

    def sources(config):
        if config.get('token') == 'second':
            raise RuntimeError('leak secret-value in sources')
        return ['demo']

    plugin = replace(demo_plugin.PLUGIN, agent_reads=(replace(demo_plugin.PLUGIN.agent_reads[0], run=boom, sources=sources),))
    client = agent_client(agent_boot(values={'url': 'https://example.com', 'token': 'first'}, plugins=(plugin,)))

    # when
    with caplog.at_level(logging.DEBUG):
        client.post('/api/read', json={'op': 'demo-echo', 'args': {}}, headers=AUTH)
        client.put('/api/agent/sources', json={'rev': 'r', 'config': {'token': 'second'}}, headers=AUTH)
        client.get('/api/agent', headers=AUTH)

    # then
    assert 'RuntimeError' in caplog.text and 'secret-value' not in caplog.text


def test_rev_of_is_a_literal_hash_with_escaped_non_ascii_text():
    # then
    assert rev_of({'k': chr(233) + 'x', 'a': '1'}) == '3c853167d61559ab'
    assert rev_of({}) == ''


def test_a_pushed_rev_is_cut_at_64_characters():
    # given
    client = agent_client()

    # when
    client.put('/api/agent/sources', json={'rev': 'x' * 70, 'config': {'url': 'https://example.com'}}, headers=AUTH)

    # then
    assert client.get('/api/agent', headers=AUTH).json()['sourcesRev'] == 'x' * 64


def test_dispatch_answers_the_same_over_http_and_directly():
    # given
    steps = [('GET', '/api/agent', None), ('POST', '/api/kube', {'op': 'nodes'}), ('POST', '/api/kube', {'op': 'pod', 'args': {'namespace': 'shop', 'name': 'forbidden'}}),
             ('POST', '/api/kube', {'op': 'pods', 'args': {}}), ('POST', '/api/kube', {'op': 'nope'}), ('POST', '/api/read', {'op': 'demo-echo', 'args': {'text': 'a'}}),
             ('PUT', '/api/agent/sources', {'rev': 'r', 'config': {'url': 'https://example.com', 'token': 't'}}), ('PUT', '/api/agent/sources', {'rev': 'r', 'config': {'zzz': 'x'}}),
             ('POST', '/api/read', {'op': 'demo-echo', 'args': {'text': 'a'}}), ('POST', '/api/read', {'op': 'other', 'args': {}}), ('GET', '/api/agent', None)]
    over_http, direct_core = agent_client(), core_for(agent_boot())

    # when
    http_answers = [(r.status_code, r.json()) for r in (over_http.request(m, p, json=b, headers=AUTH) for m, p, b in steps)]
    direct_answers = [direct_core.dispatch(m, p, f'Bearer {TOKEN}', b) for m, p, b in steps]

    # then
    assert http_answers == direct_answers


def test_a_remote_agent_names_its_relay_from_via():
    # given
    transport = Transport(remote.HttpError(401, 'x', json.dumps({'error': 'unauthorized', 'relay': True})))
    link = http_link('https://east.example.com', 'east-token', transport, via=('https://hub.example.com', 'hub-token'), via_label='hub')
    plain = http_link('https://east.example.com', 'east-token', transport, via=('https://hub.example.com', 'hub-token'))

    # then
    for made, relay in ((link, 'hub'), (plain, 'hub.example.com')):
        with pytest.raises(AgentError) as e:
            RemoteAgent(made, 'east').info()
        assert f'the {relay} agent, which relays to the east agent, refused Skopax' in str(e.value)
    assert http_link('https://east.example.com', 'east-token', transport).relay == ''


def test_the_relay_transport_does_not_follow_redirects():
    # given
    import http.server

    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == '/start':
                self.send_response(302)
                self.send_header('Location', '/landed')
                self.end_headers()
            else:
                body = b'{"ok": true}'
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'

    try:
        # when
        followed = remote.send('GET', url + '/start', {}, None, 5)
        seen.clear()
        with pytest.raises(remote.HttpError) as e:
            remote.relay_send('GET', url + '/start', {}, None, 5)
    finally:
        server.shutdown()

    # then
    assert followed.body == {'ok': True}
    assert e.value.code == 302 and seen == ['/start']
    assert AgentCore(registry_with(), FakeKube(), lambda: TOKEN, lambda: (), lambda: {}).transport is remote.relay_send
    assert TestClient(create_agent(agent_boot(), kube=FakeKube(), refresh_every=None)).app.state.core.transport is remote.relay_send


def keyed_app(**kw):
    return create_agent(agent_boot(**kw), kube=FakeKube(), transport=Transport(), clock=lambda: NOW, refresh_every=None)


BODY_ROUTES = [('POST', '/api/kube'), ('POST', '/api/read'), ('PUT', '/api/agent/sources'), ('POST', '/api/relay/api/kube'), ('PUT', '/api/relay/api/agent/sources')]


@pytest.mark.parametrize('method,path', BODY_ROUTES)
def test_a_wrong_or_missing_token_never_receives_the_body(method, path):
    # given
    keyed, keyless = keyed_app(), keyed_app(env={})
    extra = relay_headers()
    extra = {k: v for k, v in extra.items() if k != 'Authorization'} if path.startswith('/api/relay') else {}
    big = [b'x' * 65536] * 4

    # when
    wrong = run_asgi(keyed, method, path, {'authorization': 'Bearer nope', **extra}, big)
    missing = run_asgi(keyed, method, path, extra, big)
    no_token = run_asgi(keyless, method, path, {'authorization': 'Bearer ' + TOKEN, **extra}, big)
    good = run_asgi(keyed, method, path, {'authorization': 'Bearer ' + TOKEN, **extra}, [b'{}'])

    # then
    assert (wrong[0], wrong[1]) == (401, 0) and (missing[0], missing[1]) == (401, 0)
    assert (no_token[0], no_token[1]) == ((401 if path.startswith('/api/relay') else 503), 0)
    assert good[1] >= 1


@pytest.mark.parametrize('method,path', BODY_ROUTES)
def test_a_body_over_one_mebibyte_is_refused_with_413_before_parsing_and_read_in_a_bounded_way(method, path):
    # given
    app = keyed_app()
    extra = {k: v for k, v in relay_headers().items() if k != 'Authorization'} if path.startswith('/api/relay') else {}
    head = {'authorization': 'Bearer ' + TOKEN, **extra}
    piece = b'[' * 262144

    # when
    declared = run_asgi(app, method, path, {**head, 'content-length': str(2 * 1024 * 1024)}, [piece] * 8)
    streamed = run_asgi(app, method, path, head, [piece] * 40)

    # then
    assert declared[0] == 413 and declared[1] == 0
    assert streamed[0] == 413 and streamed[1] <= 6
    assert json.loads(streamed[2]) == {'error': 'the body is larger than 1 MiB'} | ({'relay': True} if path.startswith('/api/relay') else {})


def test_a_body_of_exactly_one_mebibyte_is_taken():
    # given
    app = keyed_app()
    shell = b'{"op": "nodes", "pad": ""}'
    body = b'{"op": "nodes", "pad": "' + b'x' * (1024 * 1024 - len(shell)) + b'"}'

    # when
    exact = run_asgi(app, 'POST', '/api/kube', {'authorization': 'Bearer ' + TOKEN}, [body[:700000], body[700000:]])
    over = run_asgi(app, 'POST', '/api/kube', {'authorization': 'Bearer ' + TOKEN}, [body + b' '])

    # then
    assert len(body) == 1024 * 1024
    assert exact[0] == 200 and over[0] == 413


def live_threads():
    return [t for t in threading.enumerate() if t.name == 'agent-sources' and t.is_alive()]


def test_the_default_wiring_refreshes_every_sixty_seconds_in_a_live_thread_that_stops():
    # given
    boot = agent_boot()

    # when
    app = create_agent(boot, kube=FakeKube(), clock=lambda: NOW)
    refresher = app.state.refresher
    refresher.start()
    alive = live_threads()
    refresher.stop()

    # then
    assert refresher.every == 60 and agent.REFRESH_S == 60
    assert len(alive) == 1
    assert live_threads() == []


def test_the_refresher_stops_at_shutdown():
    # given
    app = create_agent(agent_boot(), kube=FakeKube(), clock=lambda: NOW, refresh_every=0.01)

    # when
    with TestClient(app) as client:
        client.get('/actuator/health')
        running = live_threads()

    # then
    assert len(running) == 1
    assert live_threads() == []


def test_a_raising_refresh_keeps_the_last_good_settings_logs_once_per_streak_and_never_stops_the_loop(caplog):
    # given
    boot = agent_boot(env={}, values={'agent.token': TOKEN, 'agent.relay-hosts': 'one.example.com'})
    sources = boot.source_set()
    calls = [0]
    healthy = [False]

    def refresh():
        calls[0] += 1
        if not healthy[0]:
            raise RuntimeError('leak secret-value in refresh')
        return True

    sources.refresh = refresh
    boot.source_set = lambda: sources
    transport = Transport({'ok': 1})

    # when
    with caplog.at_level(logging.DEBUG):
        app = create_agent(boot, kube=FakeKube(), transport=transport, clock=lambda: NOW, refresh_every=0.005)
        app.state.refresher.start()
        client = TestClient(app)
        deadline = time.monotonic() + 5
        while calls[0] < 4 and time.monotonic() < deadline:
            time.sleep(0.005)
        failing = calls[0]
        info = client.get('/api/agent', headers=AUTH).status_code
        relayed = client.post('/api/relay/api/kube', json={'op': 'nodes'}, headers=relay_headers('https://one.example.com')).status_code
        healthy[0] = True
        before = calls[0]
        deadline = time.monotonic() + 5
        while calls[0] < before + 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        app.state.refresher.stop()
    warned = [r.getMessage() for r in caplog.records if 'not refreshed' in r.getMessage()]

    # then
    assert failing >= 4 and calls[0] >= before + 2
    assert (info, relayed) == (200, 200)
    assert len(warned) == 1 and 'RuntimeError' in warned[0]
    assert 'secret-value' not in caplog.text


def test_a_second_failure_streak_warns_again(caplog):
    # given
    sources = agent_boot().source_set()
    state = ['fail']

    def refresh():
        if state[0] == 'fail':
            raise RuntimeError('x')
        return True

    sources.refresh = refresh
    refresher = agent.Refresher(sources, None)

    # when
    with caplog.at_level(logging.WARNING):
        for step in ('fail', 'fail', 'ok', 'fail', 'fail'):
            state[0] = step
            refresher.run_once()

    # then
    assert len([r for r in caplog.records if 'not refreshed' in r.getMessage()]) == 2


def test_a_raising_own_config_logs_the_type_only_and_the_agent_goes_on(caplog):
    # given
    def own():
        raise RuntimeError('leak secret-value in own config')

    core = AgentCore(registry_with(demo_plugin), FakeKube(), lambda: TOKEN, lambda: (), own, clock=lambda: NOW)

    # when
    with caplog.at_level(logging.DEBUG):
        status, out = core.dispatch('GET', '/api/agent', f'Bearer {TOKEN}', None)

    # then
    assert status == 200 and out['reads'] == {'demo-echo': []}
    assert 'RuntimeError' in caplog.text and 'secret-value' not in caplog.text


def test_a_relay_failure_log_carries_the_type_only(caplog):
    # given
    client = agent_client(transport=Transport(remote.Unreachable('https://agent.example.com/api/kube unreachable (secret-value)')))

    # when
    with caplog.at_level(logging.DEBUG):
        out = client.post('/api/relay/api/kube', json={'op': 'nodes'}, headers=relay_headers())

    # then
    assert out.status_code == 502
    assert 'Unreachable' in caplog.text and 'secret-value' not in caplog.text


def two_reads_plugin(runs):
    first = AgentRead('alpha-read', ('text',), ('alpha-url', 'alpha-token'), lambda config: ['a'], lambda config, args: runs.append(('alpha', dict(config))) or {'ok': 1})
    second = AgentRead('beta-read', ('text',), ('beta-url',), lambda config: ['b'], lambda config, args: runs.append(('beta', dict(config))) or {'ok': 1})
    return replace(demo_plugin.PLUGIN, agent_reads=(first, second))


def test_a_push_for_one_read_leaves_the_other_read_on_the_agents_own_config():
    # given
    runs: list = []
    own = {'alpha-url': 'https://own-a.example.com', 'alpha-token': 'own-a-token', 'beta-url': 'https://own-b.example.com'}
    client = agent_client(agent_boot(values=own, plugins=(two_reads_plugin(runs),)))

    def call(op):
        return client.post('/api/read', json={'op': op, 'args': {}}, headers=AUTH).status_code

    # when
    client.put('/api/agent/sources', json={'rev': 'r', 'config': {'alpha-url': 'https://pushed-a.example.com'}}, headers=AUTH)
    codes = [call('alpha-read'), call('beta-read')]
    client.put('/api/agent/sources', json={'rev': 's', 'config': {'beta-url': 'https://pushed-b.example.com'}}, headers=AUTH)
    codes += [call('alpha-read'), call('beta-read')]

    # then
    assert codes == [200] * 4
    assert runs == [('alpha', {'alpha-url': 'https://pushed-a.example.com'}), ('beta', {'beta-url': 'https://own-b.example.com'}),
                    ('alpha', {'alpha-url': 'https://own-a.example.com', 'alpha-token': 'own-a-token'}), ('beta', {'beta-url': 'https://pushed-b.example.com'})]


def test_relay_targets_with_at_signs_underscores_or_non_ascii_look_alikes_are_refused():
    # given
    transport = Transport({'ok': 1})
    core = AgentCore(registry_with(demo_plugin), FakeKube(), lambda: TOKEN, lambda: ('agent.example.com', '*.example.org'), lambda: {}, transport=transport, clock=lambda: NOW)
    refused = ['https://a@b.example.org', 'https://b.example.org@', 'https://a_b.example.org', 'https://_.example.org', 'https://a%b.example.org',
               'https://' + chr(0x212a) + '.example.org', 'https://' + chr(0x130) + '.example.org', 'https://' + chr(0x131) + '.example.org', 'https://' + chr(0x17f) + '.example.org',
               'https://agent.example.com'.replace('a', chr(0x212a), 1)]

    # when
    answers = [core.dispatch('GET', '/api/relay/api/agent', f'Bearer {TOKEN}', None, to, 'Bearer inner')[0] for to in refused]
    allowed = core.dispatch('GET', '/api/relay/api/agent', f'Bearer {TOKEN}', None, 'https://b.example.org', 'Bearer inner')[0]

    # then
    assert answers == [400] * len(refused) and allowed == 200
    assert len(transport.calls) == 1


def test_a_blank_relay_hosts_env_falls_back_to_the_source_and_the_error_names_the_source():
    # given
    transport = Transport({'ok': 1})
    boot = agent_boot(env={'SKOPAX_AGENT_TOKEN': TOKEN, 'SKOPAX_AGENT_RELAY_HOSTS': '  '}, values={'agent.relay-hosts': 'one.example.com'})
    client = agent_client(boot, transport=transport)

    # when
    allowed = client.get('/api/relay/api/agent', headers=relay_headers('https://one.example.com')).status_code
    refused = client.get('/api/relay/api/agent', headers=relay_headers('https://zzz.example.net')).json()['error']

    # then
    assert allowed == 200
    assert 'agent.relay-hosts' in refused and 'SKOPAX_AGENT_RELAY_HOSTS' not in refused


def test_every_handler_is_guarded_unless_it_is_on_the_open_list():
    # given
    core = core_for(agent_boot())

    # when
    wrong = {key: core.dispatch(key[0], key[1], 'Bearer nope', None) for key in core.routes()}
    good = core.dispatch('GET', '/api/agent', f'Bearer {TOKEN}', None)
    open_ones = {key for key in core.routes() if core.dispatch(key[0], key[1], 'Bearer nope', None)[0] != 401 and key[0] == 'GET' and 'api' not in key[1]}

    # then
    assert all(wrong[key][0] == 401 for key in core.routes() if key not in agent.OPEN_ROUTES)
    assert good[0] == 200 and not hasattr(core, 'add_route')
    assert open_ones == {('GET', '/actuator/health'), ('GET', '/actuator/health/liveness'), ('GET', '/actuator/health/readiness')}
    assert agent.OPEN_ROUTES == frozenset({('GET', '/actuator/health'), ('GET', '/actuator/health/liveness'), ('GET', '/actuator/health/readiness')})


def test_the_wrapper_serves_exactly_the_core_route_table_and_reads_the_token_once_per_request(monkeypatch):
    # given
    reads = [0]

    class Counting(agent.TokenSource):
        def __call__(self):
            reads[0] += 1
            return super().__call__()

    monkeypatch.setattr(agent, 'TokenSource', Counting)
    app = keyed_app()
    core = app.state.core
    served = {(m, r.path) for r in app.routes for m in getattr(r, 'methods', ()) if m != 'HEAD'}
    reads[0] = 0

    # when
    with TestClient(app) as client:
        client.post('/api/kube', json={'op': 'nodes'}, headers=AUTH)
        used = reads[0]

    # then
    assert {k for k in core.routes() if not k[1].startswith('/api/relay')} <= served
    assert used == 1


def test_a_token_found_log_appears_only_when_it_was_missing_first(caplog):
    # given
    always = agent_boot()
    feed = Feed()
    late = agent_boot(env={}, feed=feed)

    # when
    with caplog.at_level(logging.INFO, logger='skopax.agent'):
        always_app = create_agent(always, kube=FakeKube(), clock=lambda: NOW, refresh_every=None)
        TestClient(always_app).get('/api/agent', headers=AUTH)
        early = [r for r in caplog.records if 'token found' in r.getMessage()]
        late_app = create_agent(late, kube=FakeKube(), clock=lambda: NOW, refresh_every=None)
        TestClient(late_app).get('/actuator/health')
        feed.values['agent.token'] = TOKEN
        late_app.state.refresher.run_once()
        TestClient(late_app).get('/api/agent', headers=AUTH)
        TestClient(late_app).get('/api/agent', headers=AUTH)
    found = [r for r in caplog.records if 'token found' in r.getMessage()]

    # then
    assert early == [] and len(found) == 1


def test_a_very_deep_relayed_body_is_a_400_not_a_crash():
    # given
    body = agent.parse_body(b'{"a":' * 100_000 + b'1' + b'}' * 100_000)
    core = AgentCore(registry_with(demo_plugin), FakeKube(), lambda: TOKEN, lambda: ('agent.example.com',), lambda: {}, transport=Transport(), clock=lambda: NOW)

    # when
    status, out = core.dispatch('POST', '/api/relay/api/kube', f'Bearer {TOKEN}', body, 'https://agent.example.com', 'Bearer inner')

    # then
    assert status == 400 and set(out) == {'error'}


def test_there_is_one_no_redirect_handler():
    # then
    assert kube_module.NoRedirect is remote.NoRedirect


def main_only_failures(tmp_path) -> dict[str, dict[str, str]]:
    path = tmp_path / 'skopax.yaml'
    path.write_text('plugins:\n  demo:\n    instances:\n      - name: east\n        url: ftp://x\n', encoding='utf-8')
    return {'settings': {'SSO_DEFAULT_ROLE': 'wizard'}, 'env instance': {'DEMO_URL': 'ftp://x'}, 'file instance': {FILE_ENV: str(path)}, 'hook': {}}


def failing_feed() -> Feed:
    feed = Feed({'agent.token': TOKEN, 'agent.relay-hosts': 'one.example.com'})
    feed.failure = RuntimeError('leak secret-value in the settings source')
    return feed


@pytest.mark.parametrize('case', ['settings', 'env instance', 'file instance', 'hook', 'broken file'])
def test_each_main_only_failure_stops_the_main_boot_but_not_the_light_agent_boot(case, tmp_path):
    # given
    broken = tmp_path / 'broken.yaml'
    broken.write_text('settings: [unclosed\n', encoding='utf-8')
    env = {**main_only_failures(tmp_path).get(case, {FILE_ENV: str(broken)}), 'SKOPAX_AGENT_TOKEN': TOKEN}
    registry = registry_with(demo_plugin, feed_plugin(failing_feed() if case == 'hook' else Feed()))
    expected = {'hook': {'Agent feed': 'ConfigurationError'}, 'broken file': {'config file': 'ConfigurationError'}}.get(case, {})

    # when
    with pytest.raises(ConfigurationError):
        main_boot(env, registry)
    light = light_boot(env, registry)
    answer = TestClient(create_agent(light, kube=FakeKube(), clock=lambda: NOW, refresh_every=None)).get('/api/agent', headers=AUTH)

    # then
    assert light.settings is None
    assert light.failed == expected
    assert answer.status_code == 200


def test_agent_mode_boots_past_a_raising_settings_hook_refuses_reads_and_serves_once_the_source_answers(monkeypatch, tmp_path, caplog):
    # given
    feed = failing_feed()
    for case in main_only_failures(tmp_path).values():
        for name, value in case.items():
            monkeypatch.setenv(name, value)
    monkeypatch.setenv('SKOPAX_MODE', 'agent')
    monkeypatch.delenv('SKOPAX_AGENT_TOKEN', raising=False)
    monkeypatch.delenv('SKOPAX_AGENT_RELAY_HOSTS', raising=False)
    monkeypatch.setattr(agent, 'Kube', FakeKube)

    # when
    with caplog.at_level(logging.DEBUG):
        app = run_main(monkeypatch, registry_with(demo_plugin, feed_plugin(feed)))['app']
        refresher = app.state.refresher
        try:
            client = TestClient(app)
            health = client.get('/actuator/health')
            refused = [client.get('/api/agent', headers=AUTH), client.post('/api/read', json={'op': 'demo-echo', 'args': {}}, headers=AUTH)]
            refresher.run_once()
            still = client.get('/api/agent', headers=AUTH).status_code
            feed.failure = None
            refresher.run_once()
            served = client.get('/api/agent', headers=AUTH).status_code
            token = client.get('/actuator/health').json()['token']
        finally:
            refresher.stop()
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and 'Agent feed' in r.getMessage()]

    # then
    assert health.status_code == 200 and health.json()['token'] is False
    assert [r.status_code for r in refused] == [503, 503] and all(r.json() == {'error': msg('agent.no_token')} for r in refused)
    assert still == 503
    assert (served, token) == (200, True)
    assert isinstance(app.state.core.kube, FakeKube)
    assert refresher.every == 60 and feed.calls == 3
    assert warned == [msg('settings.source.retry', source='Agent feed', error='ConfigurationError', every=60)]
    assert 'secret-value' not in caplog.text


def test_main_mode_stops_on_the_same_raising_settings_hook(monkeypatch):
    # given
    monkeypatch.delenv('SKOPAX_MODE', raising=False)
    registry = registry_with(demo_plugin, feed_plugin(failing_feed()))

    # when
    with pytest.raises(ConfigurationError) as e:
        run_main(monkeypatch, registry)

    # then
    assert 'Agent feed' in str(e.value) and 'RuntimeError' in str(e.value) and 'secret-value' not in str(e.value)


def test_a_guarded_request_never_waits_for_a_hanging_source_refresh():
    # given
    gate = threading.Event()
    feed = Feed({'agent.token': TOKEN})
    boot = agent_boot(env={}, feed=feed)
    feed.hold = gate
    app = create_agent(boot, kube=FakeKube(), clock=lambda: NOW, refresh_every=0.01)
    app.state.refresher.start()
    client = TestClient(app)

    try:
        # when
        assert feed.held.wait(5)
        began = time.monotonic()
        answers = [client.get('/api/agent', headers=AUTH).status_code, client.post('/api/kube', json={'op': 'nodes'}, headers=AUTH).status_code]
        labels = [s.label for s in app.state.refresher.sources.current()]
        waited = time.monotonic() - began
        hanging = not feed.released.is_set()
    finally:
        gate.set()
        app.state.refresher.stop()

    # then
    assert answers == [200, 200] and 'Agent feed' in labels
    assert waited < 1 and hanging


def test_a_failing_source_refresh_keeps_the_last_good_token_and_relay_hosts(caplog):
    # given
    feed = Feed({'agent.token': TOKEN, 'agent.relay-hosts': 'one.example.com'})
    client = agent_client(agent_boot(env={}, feed=feed), transport=Transport({'ok': 1}))
    sources = client.app.state.refresher.sources
    feed.failure = RuntimeError('leak secret-value in the settings source')

    # when
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            client.app.state.refresher.run_once()
        answers = [client.get('/api/agent', headers=AUTH).status_code, client.get('/api/relay/api/agent', headers=relay_headers('https://one.example.com')).status_code]
    status = sources.status()['Agent feed']

    # then
    assert answers == [200, 200] and feed.calls == 4
    assert status['ok'] is False and status['error'] == 'ConfigurationError'
    assert len([r for r in caplog.records if 'Agent feed' in r.getMessage()]) == 1
    assert 'secret-value' not in caplog.text


def test_the_agent_token_and_relay_hosts_come_through_the_real_sources(tmp_path):
    # given
    path = tmp_path / 'skopax.yaml'
    path.write_text('settings:\n  agent.token: file-token-0005\n  agent.relay-hosts: one.example.com\n', encoding='utf-8')
    transport = Transport({'ok': 1})
    from_file = agent_client(agent_boot(env={FILE_ENV: str(path)}), transport=transport)
    from_hook = agent_client(agent_boot(env={}, values={'agent.token': 'hook-token-0006', 'agent.relay-hosts': 'two.example.com'}), transport=transport)
    both = agent_client(agent_boot(env={FILE_ENV: str(path)}, values={'agent.token': 'hook-token-0006'}), transport=transport)

    def ask(client, token, host):
        auth = {'Authorization': f'Bearer {token}'}
        relayed = client.get('/api/relay/api/agent', headers={**auth, 'X-Relay-To': f'https://{host}', 'X-Relay-Authorization': 'Bearer inner'})
        return client.get('/api/agent', headers=auth).status_code, relayed.status_code

    # then
    assert ask(from_file, 'file-token-0005', 'one.example.com') == (200, 200)
    assert ask(from_hook, 'hook-token-0006', 'two.example.com') == (200, 200)
    assert ask(from_hook, 'hook-token-0006', 'one.example.com') == (200, 400)
    assert ask(both, 'hook-token-0006', 'one.example.com') == (200, 200)
    assert ask(both, 'file-token-0005', 'one.example.com')[0] == 401
    assert [s.label for s in both.app.state.refresher.sources.current()] == ['config file', 'Agent feed', 'demo source']
    assert not hasattr(agent, 'merged_values')


def test_reads_wait_for_a_source_that_failed_at_boot_and_then_run_on_the_full_config(tmp_path):
    # given
    seen: list[dict] = []

    def run(config, args):
        seen.append(dict(config))
        return {'ok': True}

    plugin = replace(demo_plugin.PLUGIN, agent_reads=(replace(demo_plugin.PLUGIN.agent_reads[0], run=run),))
    path = tmp_path / 'skopax.yaml'
    path.write_text('settings:\n  url: https://own.example.com\n', encoding='utf-8')
    feed = Feed({'token': 'own-token'})
    feed.failure = RuntimeError('leak secret-value in the settings source')
    boot = agent_boot(env={'SKOPAX_AGENT_TOKEN': TOKEN, FILE_ENV: str(path)}, plugins=(plugin,), feed=feed)
    client = agent_client(boot)
    refresher = client.app.state.refresher

    def read():
        return client.post('/api/read', json={'op': 'demo-echo', 'args': {}}, headers=AUTH)

    # when
    waiting = read()
    listed = client.get('/api/agent', headers=AUTH).json()['reads']
    refresher.run_once()
    still = read().status_code
    feed.failure = None
    refresher.run_once()
    served = read()
    listed_after = client.get('/api/agent', headers=AUTH).json()['reads']
    feed.failure = RuntimeError('down again')
    refresher.run_once()
    kept = read().status_code

    # then
    assert boot.failed == {'Agent feed': 'ConfigurationError'}
    assert (waiting.status_code, waiting.json()) == (503, {'error': msg('agent.source_pending', source='Agent feed')})
    assert listed == {'demo-echo': []} and still == 503
    assert served.status_code == 200 and listed_after == {'demo-echo': ['demo']}
    assert kept == 200
    assert seen == [{'url': 'https://own.example.com', 'token': 'own-token'}] * 2


def test_a_push_that_holds_a_reads_keys_serves_only_that_read_while_a_source_is_still_down():
    # given
    runs: list = []
    feed = Feed({'alpha-token': 'own-a-token'})
    feed.failure = RuntimeError('settings source down')
    client = agent_client(agent_boot(values={}, plugins=(two_reads_plugin(runs),), feed=feed))

    def call(op):
        return client.post('/api/read', json={'op': op, 'args': {}}, headers=AUTH)

    # when
    client.put('/api/agent/sources', json={'rev': 'r', 'config': {'alpha-url': 'https://pushed-a.example.com', 'alpha-token': 'pushed-a-token'}}, headers=AUTH)
    alpha, beta = call('alpha-read'), call('beta-read')
    listed = client.get('/api/agent', headers=AUTH).json()['reads']

    # then
    assert alpha.status_code == 200 and runs == [('alpha', {'alpha-url': 'https://pushed-a.example.com', 'alpha-token': 'pushed-a-token'})]
    assert (beta.status_code, beta.json()) == (503, {'error': msg('agent.source_pending', source='Agent feed')})
    assert listed == {'alpha-read': ['a'], 'beta-read': []}


def test_the_agent_boot_has_no_environment_loader_so_a_raising_env_instances_never_blocks_reads(tmp_path):
    # given
    def boom(env):
        raise RuntimeError('leak secret-value in env instances')

    path = tmp_path / 'skopax.yaml'
    path.write_text('settings:\n  url: https://own.example.com\n  token: own-token\n', encoding='utf-8')
    env = {'SKOPAX_AGENT_TOKEN': TOKEN, FILE_ENV: str(path)}
    registry = registry_with(replace(demo_plugin.PLUGIN, env_instances=boom), feed_plugin(Feed()))

    # when
    with pytest.raises(ConfigurationError):
        main_boot(env, registry)
    light = light_boot(env, registry)
    labels = [label for label, _ in light.source_set().loaders]
    client = TestClient(create_agent(light, kube=FakeKube(), clock=lambda: NOW, refresh_every=None))
    answer = client.post('/api/read', json={'op': 'demo-echo', 'args': {'text': 'hi'}}, headers=AUTH)

    # then
    assert light.failed == {} and labels == ['config file', 'Agent feed', 'Demo']
    assert (answer.status_code, answer.json()) == (200, {'data': {'echo': 'hi'}})


def test_an_agent_masks_with_the_plugin_redaction_rules(tmp_path):
    # given
    (tmp_path / 'token').write_text('sa-token\n')
    cluster = kube_module.Kube(root=tmp_path, base='https://api.test', transport=lambda url, headers, timeout: b'user DEMO-1234 failed for alice@example.com\n')
    echo = replace(demo_plugin.PLUGIN.agent_reads[0], run=lambda config, args: {'echo': redact.redact(args.get('text', ''))})
    plugin = replace(demo_plugin.PLUGIN, agent_reads=(echo,))
    client = agent_client(agent_boot(values={'url': 'https://example.com', 'token': 'secret-value'}, plugins=(plugin,)), kube=cluster)

    # when
    logs = client.post('/api/kube', json={'op': 'previous_logs', 'args': {'namespace': 'shop', 'pod': 'p', 'tail': 5}}, headers=AUTH)
    read = client.post('/api/read', json={'op': 'demo-echo', 'args': {'text': 'ticket DEMO-4321'}}, headers=AUTH)

    # then
    assert logs.status_code == 200 and 'DEMO-1234' not in logs.text and 'alice@example.com' not in logs.text
    assert logs.text.count('[redacted]') == 2
    assert read.status_code == 200 and read.json() == {'data': {'echo': 'ticket [redacted]'}}


def test_an_agent_redaction_timeout_gives_the_marker_and_warns_with_the_slowest_rule(tmp_path, caplog):
    # given
    (tmp_path / 'token').write_text('sa-token\n')
    hostile = 'a' * 60 + '!'
    cluster = kube_module.Kube(root=tmp_path, base='https://api.test', transport=lambda url, headers, timeout: hostile.encode())
    slow = replace(demo_plugin.PLUGIN, redactions=(replace(demo_plugin.PLUGIN.redactions[0], id='slow', pattern=r'(a|aa)+$'),))
    client = agent_client(agent_boot(plugins=(slow,)), kube=cluster)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.agent'):
        logs = client.post('/api/kube', json={'op': 'previous_logs', 'args': {'namespace': 'shop', 'pod': 'p', 'tail': 5}}, headers=AUTH)

    # then
    assert logs.json() == {'data': redact.TIMEOUT_MARK}
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.agent' and 'slowest rule' in r.getMessage()]
    assert warned == ['plug-in demo: redaction slow was the slowest rule when the redaction budget ran out (count 1)']


def test_reads_stay_open_when_a_source_that_answered_nothing_later_fails(tmp_path):
    # given
    path = tmp_path / 'skopax.yaml'
    feed = Feed({'url': 'https://own.example.com', 'token': 'own-token'})
    client = agent_client(agent_boot(env={'SKOPAX_AGENT_TOKEN': TOKEN, FILE_ENV: str(path)}, feed=feed))

    def read():
        return client.post('/api/read', json={'op': 'demo-echo', 'args': {'text': 'hi'}}, headers=AUTH).status_code

    # when
    before = read()
    path.write_text('settings: [unclosed\n', encoding='utf-8')
    client.app.state.refresher.run_once()
    after = read()

    # then
    assert (before, after) == (200, 200)
    assert client.app.state.refresher.sources.status()['config file']['ok'] is False


def test_the_retry_warning_names_the_refresh_interval_of_the_agent(caplog):
    # given
    feed = Feed({'agent.token': TOKEN})
    feed.failure = RuntimeError('down')
    boot = agent_boot(env={}, feed=feed)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.agent'):
        create_agent(boot, kube=FakeKube(), clock=lambda: NOW, refresh_every=7)
        create_agent(boot, kube=FakeKube(), clock=lambda: NOW, refresh_every=None)

    # then
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.agent']
    assert warned == [msg('agent.no_token') + '; trying the settings again every 7 s', msg('settings.source.retry', source='Agent feed', error='ConfigurationError', every=7), msg('agent.no_token')]


@pytest.mark.parametrize('given, answered', [(200, agent.DEFAULT_STATUS), (302, agent.DEFAULT_STATUS), (99, agent.DEFAULT_STATUS), (1000, agent.DEFAULT_STATUS), (404, 404)])
def test_a_plugin_read_refusal_answers_only_an_error_status(given, answered):
    # given
    def run(config, args):
        raise agent.ReadError(given, 'source said no')

    plugin = replace(demo_plugin.PLUGIN, agent_reads=(replace(demo_plugin.PLUGIN.agent_reads[0], run=run),))
    client = agent_client(agent_boot(values={'url': 'https://example.com', 'token': 'secret-value'}, plugins=(plugin,)))

    # when
    res = client.post('/api/read', json={'op': 'demo-echo', 'args': {}}, headers=AUTH)

    # then
    assert (res.status_code, res.json()) == (answered, {'error': 'source said no'})
