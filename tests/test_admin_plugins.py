import dataclasses
import logging
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import CATEGORIES, Check, Field, PassResult, SourceData, Sweep, TestResult
from skopax.engine import anomaly, iso
from skopax.messages import msg
from skopax.registry import Registry
from skopax.settingsources import SourceSet
from skopax.store import Conflict
from tests.conftest import AUTH, settings
from tests.plugin_kit import add_instance, registry_with
from tests.plugins import demo_plugin, two_checks_plugin
from tests.plugins.base import minimal
from tests.test_read_gate import walk

HERE = Path(demo_plugin.__file__).parent
NOW = 1_792_224_000.0
SECRET = 'tok-7f3a9c1d-plain'
GOOD = {'url': 'https://demo.example.com', 'token': SECRET}
SEEN: list[dict] = []
HOLD = threading.Event()
PLUGINS = 'tests.plugins.demo_plugin,tests.plugins.enterprise_plugin,tests.plugins.broken_plugin'


def probe(ctx):
    config = dict(ctx.instance.config)
    SEEN.append(config)
    if config.get('mode') == 'slow':
        raise RuntimeError(f"boom {config.get('token')}")
    if 'wait.' in config['url']:
        HOLD.wait(5)
    if 'raw.' in config['url']:
        return None
    if 'echo.' in config['url']:
        return TestResult(True, f"token {config.get('token')} accepted", 2)
    return TestResult(config.get('token') == SECRET, f"answered {config['url']}", 4)


def pulse(ctx):
    cards = [anomaly('demo', 'err', 'shop', f'{ctx.instance.name}-svc', '', 'warn', 5, None, None, '', 'ERROR logs', 'five errors')]
    return Sweep(PassResult('pulse', True, 'ok', cards, {}, ('demo',)), {})


CHECK = Check('pulse', 'Demo pulse', 900, 0, ('demo',), pulse, legacy_alias=True)
DEMO = dataclasses.replace(demo_plugin.PLUGIN, test=probe, checks=(CHECK,))


@pytest.fixture(autouse=True)
def clean():
    SEEN.clear()
    HOLD.clear()
    two_checks_plugin.reset()
    yield
    HOLD.set()
    two_checks_plugin.RELEASE.set()


@contextmanager
def served(store, *plugins, registry=None, **kw):
    registry = registry if registry is not None else Registry.of(*(plugins or (DEMO,)), directory=HERE)
    with TestClient(create_app(settings(), store, registry=registry, **kw)) as c:
        yield c


def listed(c) -> dict:
    return {p['id']: p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins']}


def add(c, name='main', enabled=False, values=None, plugin='demo'):
    return c.post(f'/api/admin/plugins/{plugin}/instances', json={'name': name, 'values': GOOD if values is None else values, 'enabled': enabled}, headers=AUTH)


def texts(store) -> list[str]:
    return [d['text'] for d in store.activity.find()]


def test_catalog_lists_every_plugin_with_state_and_reason(store):
    # given
    registry = Registry.load({'SKOPAX_PLUGINS': PLUGINS}, builtin=(), entry_points=False)

    # when
    with served(store, registry=registry) as c:
        body = c.get('/api/admin/plugins', headers=AUTH).json()
    rows = {p['id']: p for p in body['plugins']}

    # then
    assert body['edition'] == 'community' and body['secretsKey'] == 'generated' and 'secretsNote' not in body and 'sources' not in body
    assert (rows['demo']['state'], rows['demo']['reason'], rows['demo']['enabled'], rows['demo']['icon']) == ('ok', None, True, True)
    assert [f['key'] for f in rows['demo']['fields']] == ['url', 'token', 'ca', 'mode'] and rows['demo']['instances'] == []
    assert rows['demo']['category'] == 'other' and rows['demo']['edition'] == 'community'
    assert rows['enterprise-demo']['state'] == 'locked' and rows['enterprise-demo']['reason'] == msg('plugins.needs_edition', name='Enterprise demo', edition='Enterprise')
    broken = rows['tests.plugins.broken_plugin']
    assert 'ImportError' in broken['reason'] and set(broken) == {'id', 'module', 'reason', 'origin'} and broken['origin'] == 'env'


def test_each_plugin_row_says_where_it_came_from_and_only_custom_ones_name_the_module(store):
    # given
    base = Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.demo_plugin,tests.plugins.broken_plugin'}, builtin=('tests.plugins.linker_plugin',), entry_points=False)
    package = dataclasses.replace(Registry.load({}, builtin=('tests.plugins.enterprise_plugin',), entry_points=False).loaded()[0], origin='entry-point')
    registry = Registry(base.loaded() + [package])

    # when
    with served(store, registry=registry) as c:
        rows = listed(c)
        put = c.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH).json()
    builtin = rows['linker']

    # then
    assert builtin['origin'] == 'builtin' and 'module' not in builtin
    assert rows['demo']['origin'] == 'env' and rows['demo']['module'] == 'tests.plugins.demo_plugin'
    assert rows['enterprise-demo']['origin'] == 'package' and rows['enterprise-demo']['module'] == 'tests.plugins.enterprise_plugin'
    assert rows['tests.plugins.broken_plugin']['origin'] == 'env' and rows['tests.plugins.broken_plugin']['module'] == 'tests.plugins.broken_plugin'
    assert put['origin'] == 'env' and put['module'] == 'tests.plugins.demo_plugin'


def test_a_broken_builtin_and_a_broken_package_carry_their_origin(store):
    # given
    base = Registry.load({}, builtin=('tests.plugins.broken_plugin',), entry_points=False)
    package = dataclasses.replace(Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.broken_plugin'}, builtin=(), entry_points=False).loaded()[0], id='broken-pkg', origin='entry-point')

    # when
    with served(store, registry=Registry(base.loaded() + [package])) as c:
        rows = listed(c)

    # then
    assert rows['tests.plugins.broken_plugin']['origin'] == 'builtin'
    assert rows['broken-pkg']['origin'] == 'package'


def test_switching_on_a_locked_plugin_is_409(store):
    # given
    registry = Registry.load({'SKOPAX_PLUGINS': PLUGINS}, builtin=(), entry_points=False)

    # when
    with served(store, registry=registry) as c:
        locked = c.put('/api/admin/plugins/enterprise-demo', json={'enabled': True}, headers=AUTH)
        broken = c.put('/api/admin/plugins/tests.plugins.broken_plugin', json={'enabled': True}, headers=AUTH)
        made = add(c, plugin='enterprise-demo')
        missing = c.put('/api/admin/plugins/nope', json={'enabled': True}, headers=AUTH)
        rows = listed(c)

    # then
    assert locked.status_code == 409 and locked.json()['detail'] == rows['enterprise-demo']['reason']
    assert broken.status_code == 409 and 'ImportError' in broken.json()['detail']
    assert made.status_code == 409 and missing.status_code == 404
    assert store.db['plugin_state'].count_documents({}) == 0 and texts(store) == []


def test_add_test_save_and_turn_on_an_instance(store):
    # given
    with served(store) as c:
        nudged = []
        c.app.state.scheduler.nudge = nudged.append

        # when
        made = add(c)
        after_add = list(nudged)
        tested = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'token': None}}, headers=AUTH)
        wiped = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'token': ''}}, headers=AUTH)
        seen_before = len(SEEN)
        edited = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'https://other.example.com', 'token': None}}, headers=AUTH)
        seen_after = len(SEEN)
        same = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': GOOD['url'], 'token': None}}, headers=AUTH)
        fresh = c.post('/api/admin/plugins/demo/test', json={'values': {'url': 'https://new.example.com', 'token': SECRET}}, headers=AUTH)
        turned = c.patch('/api/admin/plugins/demo/instances/main', json={'enabled': True}, headers=AUTH)
        row = listed(c)['demo']

    # then
    assert made.status_code == 201 and made.json()['name'] == 'main' and made.json()['primary'] is True and made.json()['enabled'] is False
    assert after_add == []
    assert tested.status_code == 200 and tested.json()['ok'] is True and tested.json()['summary'] == 'answered https://demo.example.com' and tested.json()['ms'] == 4
    assert wiped.json()['ok'] is False
    assert edited.status_code == 422 and edited.json() == {'detail': msg('integrations.reenter', field='Token'), 'field': 'token'} and seen_after == seen_before
    assert same.json()['ok'] is True and same.json()['summary'] == 'answered https://demo.example.com'
    assert texts(store).count(msg('integrations.test_used_secret', plugin='Demo', instance='main')) == 2
    assert fresh.json()['ok'] is True and fresh.json()['summary'] == 'answered https://new.example.com'
    assert turned.status_code == 200 and turned.json()['instance']['enabled'] is True and 'notice' not in turned.json()
    assert nudged == ['demo.pulse']
    assert [i['enabled'] for i in row['instances']] == [True]
    assert sum(1 for s in SEEN if s.get('token') == SECRET) == 3


def test_creating_an_enabled_instance_nudges_every_job_and_a_bad_one_is_refused(store):
    # given
    with served(store) as c:
        nudged = []
        c.app.state.scheduler.nudge = nudged.append

        # when
        made = add(c, enabled=True)
        again = add(c)
        bad = add(c, name='east', values={'url': 'ftp://example.com'})
        short = add(c, name='Bad Name')
        long = c.post('/api/admin/plugins/demo/instances', json={'name': 'x' * 21, 'values': GOOD}, headers=AUTH)

    # then
    assert made.status_code == 201 and nudged == ['demo.pulse']
    assert again.status_code == 409 and again.json()['detail'] == msg('instances.exists', name='main')
    assert bad.status_code == 422 and bad.json()['field'] == 'url' and bad.json()['detail']
    assert short.status_code == 422 and short.json()['field'] == 'name'
    assert long.status_code == 422
    assert [t for t in texts(store) if 'main' in t] == [msg('integrations.instance_created', plugin='Demo', instance='main'), msg('integrations.instance_on', plugin='Demo', instance='main')]


def test_secrets_never_leave_the_server(store, caplog):
    # given
    with served(store) as c:
        c.app.state.scheduler.nudge = lambda name: None
        with caplog.at_level(logging.DEBUG):
            made = add(c, enabled=True)
            listing = c.get('/api/admin/plugins', headers=AUTH)
            slow = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'mode': 'slow'}}, headers=AUTH)
            stored_test = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'token': None}}, headers=AUTH)
            changed = c.patch('/api/admin/plugins/demo/instances/main', json={'values': {'token': SECRET + '-2'}}, headers=AUTH)
            relisted = c.get('/api/admin/plugins', headers=AUTH)
            feeds = [c.get('/api/activity', headers=AUTH), c.get('/api/state', headers=AUTH)]
        doc = store.db['instances'].find_one({'_id': 'demo/main'})
        stored = repr(list(store.db['instances'].find()))

    # then
    assert [r.status_code for r in (made, listing, slow, stored_test, changed, relisted, *feeds)] == [201, 200, 200, 200, 200, 200, 200, 200]
    assert stored_test.json()['ok'] is True and any(s.get('token') == SECRET for s in SEEN), 'the secret was really in use'
    assert doc['secrets']['token'].startswith('enc:v1:') and 'token' not in doc['values']
    bodies = [r.text for r in (made, listing, slow, stored_test, changed, relisted, *feeds)]
    assert all(SECRET not in b and SECRET + '-2' not in b for b in bodies)
    assert SECRET not in caplog.text and SECRET not in repr(texts(store)) and SECRET not in stored
    assert SECRET + '-2' not in caplog.text + repr(texts(store)) + stored
    assert relisted.json()['plugins'][0]['instances'][0]['secrets'] == {'token': {'set': True, 'from': None}}
    assert texts(store) and any('token' in t for t in texts(store))


def test_the_secret_view_says_only_whether_it_is_set_and_where_from(store):
    # given
    feed = SourceData('config file', {}, ({'plugin': 'demo', 'name': 'east', 'url': 'https://east.example.com', 'token': 'from-source'},))
    sources = SourceSet([('config file', lambda: feed)], {'config file': feed})

    # when
    with served(store, sources=sources) as c:
        add(c)
        rows = {i['name']: i for i in listed(c)['demo']['instances']}

    # then
    assert rows['main']['secrets'] == {'token': {'set': True, 'from': None}} and rows['east']['secrets'] == {'token': {'set': True, 'from': 'config file'}}
    assert 'from-source' not in repr(rows) and SECRET not in repr(rows)


def test_the_address_of_a_source_owned_instance_cannot_be_changed_here(store):
    # given
    feed = SourceData('config file', {}, ({'plugin': 'demo', 'name': 'east', 'url': 'https://east.example.com', 'token': 'from-source'},))
    sources = SourceSet([('config file', lambda: feed)], {'config file': feed})

    with served(store, sources=sources) as c:
        # when
        moved = c.patch('/api/admin/plugins/demo/instances/east', json={'values': {'url': 'https://elsewhere.example.net'}}, headers=AUTH)
        row = {i['name']: i for i in listed(c)['demo']['instances']}['east']
        tested = c.post('/api/admin/plugins/demo/test', json={'name': 'east', 'values': {}}, headers=AUTH)

    # then
    assert moved.status_code == 409 and moved.json()['detail'] == msg('instances.source_owned', field='URL', source='config file')
    assert row['owned'] == ['token', 'url'] and row['values']['url'] == 'https://east.example.com'
    assert tested.status_code == 200 and [(s['url'], s['token']) for s in SEEN] == [('https://east.example.com', 'from-source')]


def test_source_owned_instance_cannot_be_deleted_and_its_secret_is_read_only(store):
    # given
    feed = SourceData('config file', {}, ({'plugin': 'demo', 'name': 'east', 'url': 'https://east.example.com', 'token': 'from-source', 'enabled': 'true'},))
    sources = SourceSet([('config file', lambda: feed)], {'config file': feed})

    # when
    with served(store, sources=sources) as c:
        row = {i['name']: i for i in listed(c)['demo']['instances']}['east']
        gone = c.delete('/api/admin/plugins/demo/instances/east', headers=AUTH)
        secret = c.patch('/api/admin/plugins/demo/instances/east', json={'values': {'token': 'mine'}}, headers=AUTH)
        plain = c.patch('/api/admin/plugins/demo/instances/east', json={'values': {'mode': 'slow'}}, headers=AUTH)
        after = {i['name']: i for i in listed(c)['demo']['instances']}['east']

    # then
    assert row['source'] == 'config file' and 'token' in row['owned']
    assert gone.status_code == 409 and gone.json()['detail'] == msg('instances.source_delete', source='config file')
    assert secret.status_code == 409 and 'config file' in secret.json()['detail']
    assert plain.status_code == 200 and after['values']['mode'] == 'slow'


def test_a_moved_address_of_a_source_owned_instance_says_where_its_secret_comes_from(store):
    # given
    feed = SourceData('config file', {}, ({'plugin': 'demo', 'name': 'east', 'url': 'https://east.example.com', 'token': 'from-source'},))
    sources = SourceSet([('config file', lambda: feed)], {'config file': feed})

    with served(store, sources=sources) as c:
        # when
        moved = c.post('/api/admin/plugins/demo/test', json={'name': 'east', 'values': {'url': 'https://echo.example.com'}}, headers=AUTH)

    # then
    assert moved.status_code == 422 and SEEN == []
    assert moved.json() == {'detail': 'Token comes from config file; change the address there', 'field': 'token'}


def test_disable_and_delete_retire_cards(store):
    # given
    with served(store) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True)
        c.app.state.runtime.refresh()
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        open_now = {a['key']: a['status'] for a in store.load()['anomalies']}

        # when
        off = c.patch('/api/admin/plugins/demo/instances/east', json={'enabled': False}, headers=AUTH)
        after_off = {a['key']: a['status'] for a in store.load()['anomalies']}
        gone = c.delete('/api/admin/plugins/demo/instances/east', headers=AUTH)
        plugin_off = c.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH)
        after_all = {a['key']: a['status'] for a in store.load()['anomalies']}
        notes = {a['key']: (a.get('note') or {}).get('text') for a in store.load()['anomalies']}

    # then
    assert set(open_now.values()) == {'open'} and len(open_now) == 2
    east, main = 'err|shop/east-svc@east', 'err|shop/main-svc'
    assert off.status_code == 200 and after_off[east] == 'cleared' and after_off[main] == 'open'
    assert gone.status_code == 204 and plugin_off.status_code == 200 and plugin_off.json()['enabled'] is False
    assert after_all[main] == 'cleared'
    assert notes[east] == msg('cards.retired', plugin='Demo', instance='east') and notes[main] == msg('cards.retired_plugin', plugin='Demo')
    assert msg('integrations.instance_off', plugin='Demo', instance='east') in texts(store) and msg('integrations.instance_deleted', plugin='Demo', instance='east') in texts(store)
    assert msg('integrations.plugin_off', plugin='Demo') in texts(store)


def test_a_primary_deleted_with_a_promotion_retires_both_segments(store):
    # given
    with served(store) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True)
        c.app.state.runtime.refresh()
        c.app.state.sweeper.run('demo', 'pulse', NOW)

        # when
        gone = c.delete('/api/admin/plugins/demo/instances/main', headers=AUTH)
        after = {a['key']: a['status'] for a in store.load()['anomalies']}
        rows = {i['name']: i for i in listed(c)['demo']['instances']}

    # then
    assert gone.status_code == 204 and set(after.values()) == {'cleared'} and rows['east']['primary'] is True


def test_reenabling_an_instance_runs_it_again(store):
    # given
    with served(store) as c:
        nudged = []
        c.app.state.scheduler.nudge = nudged.append
        add(c, 'main', True)
        add(c, 'east', True)
        c.app.state.runtime.refresh()
        c.patch('/api/admin/plugins/demo/instances/east', json={'enabled': False}, headers=AUTH)

        # when
        c.patch('/api/admin/plugins/demo/instances/east', json={'enabled': True}, headers=AUTH)
        c.app.state.runtime.refresh()
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        back = {a['key']: a['status'] for a in store.load()['anomalies']}

    # then
    assert back['err|shop/east-svc@east'] == 'open' and nudged.count('demo.pulse') == 3


def test_primary_change_returns_the_notice(store):
    # given
    with served(store) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True)
        c.app.state.runtime.refresh()
        c.app.state.sweeper.run('demo', 'pulse', NOW)

        # when
        moved = c.patch('/api/admin/plugins/demo/instances/east', json={'primary': True}, headers=AUTH)
        again = c.patch('/api/admin/plugins/demo/instances/east', json={'primary': True}, headers=AUTH)
        rows = {i['name']: i for i in listed(c)['demo']['instances']}
        after = {a['key']: a['status'] for a in store.load()['anomalies']}
        missing = c.patch('/api/admin/plugins/demo/instances/nope', json={'primary': True}, headers=AUTH)
        empty = c.patch('/api/admin/plugins/demo/instances/east', json={'primary': False}, headers=AUTH)

    # then
    assert moved.status_code == 200 and moved.json()['notice'] == msg('instances.primary_moved', plugin='Demo', instance='east', old='main')
    assert again.status_code == 200 and 'notice' not in again.json()
    assert (rows['east']['primary'], rows['main']['primary']) == (True, False)
    assert set(after.values()) == {'cleared'}
    assert missing.status_code == 404 and empty.status_code == 422


def test_connection_test_never_500s(store, monkeypatch):
    # given
    monkeypatch.setattr('skopax.app.TEST_TIMEOUT_S', 0.3)
    with served(store) as c:
        add(c)

        # when
        raised = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'mode': 'slow'}}, headers=AUTH)
        odd = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'https://raw.example.com', 'token': SECRET}}, headers=AUTH)
        hung = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'https://wait.example.com', 'token': SECRET}}, headers=AUTH)
        invalid = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'ftp://example.com'}}, headers=AUTH)
        unknown = c.post('/api/admin/plugins/demo/test', json={'name': 'missing', 'values': {'url': 'https://example.com'}}, headers=AUTH)
        untested = c.post('/api/admin/plugins/nope/test', json={'values': {}}, headers=AUTH)
    bare = dataclasses.replace(DEMO, test=None)
    with served(store, bare) as c:
        none = c.post('/api/admin/plugins/demo/test', json={'values': GOOD}, headers=AUTH)

    # then
    assert raised.status_code == 200 and raised.json()['ok'] is False and 'RuntimeError' in raised.json()['summary'] and 'boom' not in raised.text and SECRET not in raised.text
    assert hung.status_code == 200 and hung.json()['ok'] is False and '0.3' in hung.json()['summary']
    assert odd.status_code == 200 and odd.json()['ok'] is False and odd.json()['summary'] == msg('integrations.test_bad')
    assert invalid.status_code == 422 and invalid.json()['field'] == 'url'
    assert unknown.status_code == 404 and unknown.json()['detail'] == msg('instances.unknown', name='missing')
    assert untested.status_code == 404
    assert none.status_code == 200 and none.json()['ok'] is False


def test_a_plugin_adds_no_routes(store):
    # given
    with served(store, registry=registry_with(demo_plugin)) as with_demo, served(store, registry=Registry.of()) as bare:
        full = sorted(walk(with_demo.app.routes))
        empty = sorted(walk(bare.app.routes))

    # then
    assert full == empty
    assert ('POST', '/api/admin/plugins/{plugin_id}/test') in full


def test_every_new_route_needs_the_admin_role(store):
    # given
    with served(store) as c:
        calls = [('GET', '/api/admin/plugins', None), ('PUT', '/api/admin/plugins/demo', {'enabled': True}), ('POST', '/api/admin/plugins/demo/instances', {'name': 'main', 'values': {}}),
                 ('PATCH', '/api/admin/plugins/demo/instances/main', {'enabled': True}), ('DELETE', '/api/admin/plugins/demo/instances/main', None), ('POST', '/api/admin/plugins/demo/test', {'values': {}})]

        # when
        codes = [c.request(method, path, json=body).status_code for method, path, body in calls]

    # then
    assert codes == [401] * len(calls)


def test_an_untouched_secret_is_kept_and_an_explicit_empty_one_is_cleared(store):
    # given
    with served(store) as c:
        add(c)
        instances = c.app.state.instances

        # when
        c.patch('/api/admin/plugins/demo/instances/main', json={'values': {'token': None, 'mode': 'slow'}}, headers=AUTH)
        kept = instances.get('demo', 'main').secrets.get('token')
        quiet = len(texts(store))
        noop = c.patch('/api/admin/plugins/demo/instances/main', json={'values': {'token': None}}, headers=AUTH)
        unchanged = len(texts(store))
        c.patch('/api/admin/plugins/demo/instances/main', json={'values': {'token': ''}}, headers=AUTH)
        cleared = instances.get('demo', 'main').secrets.get('token')

    # then
    assert kept == SECRET and cleared is None
    assert noop.status_code == 200 and unchanged == quiet, 'a patch that carries only untouched secrets writes no activity line'


def test_the_create_task_dialog_lists_the_projects_plugins_add(store):
    # given
    org = dataclasses.replace(demo_plugin.PLUGIN, hooks=dataclasses.replace(demo_plugin.PLUGIN.hooks, org_validators=lambda: {'ticket_projects': ['PLUG', 'OPS', 'INFRA', 'bad key']}))

    # when
    with served(store, org) as c:
        add(c, 'main', True, plugin='demo')
        c.app.state.runtime.refresh()
        task = c.get('/api/me').json()['task']

    # then
    assert task['projects'] == ['OPS', 'WEB', 'APP', 'MOB', 'PLUG']


def test_the_form_fields_of_a_plugin_leave_out_the_fields_its_agent_reads_ask_for(store):
    # given
    read = dataclasses.replace(demo_plugin.PLUGIN.agent_reads[0], keys=('url', 'token', 'ns', 'ca'), fields=(Field('ns', 'Namespace', 'text'), DEMO.fields[2]))
    wide = dataclasses.replace(DEMO, agent_reads=(read,))

    # when
    with served(store, wide) as c:
        row = listed(c)['demo']
        fields = c.app.state.runtime.fields_of('demo')
        none = c.app.state.runtime.fields_of('nope')

    # then
    assert [f['key'] for f in row['fields']] == ['url', 'token', 'ca', 'mode'] == [f['key'] for f in fields]
    assert none == []


def test_a_changed_address_never_gets_the_saved_secret_and_a_summary_never_echoes_one(store):
    # given
    odd = 'ab' + chr(7) + 'cd'
    with served(store) as c:
        add(c)
        before = len(SEEN)

        # when
        moved = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'https://echo.example.com'}}, headers=AUTH)
        moved_blank = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'https://echo.example.com', 'token': ''}}, headers=AUTH)
        blocked = len(SEEN) - before
        typed = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'https://echo.example.com', 'token': SECRET}}, headers=AUTH)
        control = c.post('/api/admin/plugins/demo/test', json={'values': {'url': 'https://echo.example.com', 'token': odd}}, headers=AUTH)

    # then
    assert moved.status_code == 422 and moved.json()['field'] == 'token' and moved.json()['detail'] == msg('integrations.reenter', field='Token')
    assert moved_blank.status_code == 200 and blocked == 1 and SEEN[before].get('token') is None
    assert typed.json()['summary'] == 'token *** accepted' and SECRET not in typed.text
    assert control.json()['summary'] == 'token *** accepted' and 'ab' not in control.text


def test_a_hidden_secret_is_not_asked_for_again_when_the_address_moves(store):
    # given
    fields = (Field('url', 'URL', 'url', required=True, ui=True), Field('auth', 'Authentication', 'choice', default='basic', choices=('basic', 'bearer')),
              Field('token', 'Token', 'secret', show_if=('auth', 'bearer')))
    gated = dataclasses.replace(DEMO, fields=fields)
    with served(store, gated) as c:
        add(c, values={'url': GOOD['url'], 'auth': 'bearer', 'token': SECRET})
        before = len(SEEN)

        # when
        hidden = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'https://echo.example.com', 'auth': 'basic'}}, headers=AUTH)
        ran = len(SEEN) - before
        shown = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'https://echo.example.com', 'auth': 'bearer'}}, headers=AUTH)

    # then
    assert hidden.status_code == 200 and ran == 1 and SEEN[before].get('token') is None and hidden.json()['summary'] == 'token None accepted'
    assert shown.status_code == 422 and shown.json()['field'] == 'token' and len(SEEN) - before == 1


def test_a_cleared_switch_falls_back_to_its_default_when_a_moved_address_asks_for_a_secret(store):
    # given
    fields = (Field('url', 'URL', 'url', required=True, ui=True), Field('auth', 'Authentication', 'choice', default='bearer', choices=('basic', 'bearer')),
              Field('token', 'Token', 'secret', show_if=('auth', 'bearer')))
    gated = dataclasses.replace(DEMO, fields=fields)
    with served(store, gated) as c:
        add(c, values={'url': GOOD['url'], 'auth': 'basic', 'token': SECRET})

        # when
        cleared = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'https://echo.example.com', 'auth': ''}}, headers=AUTH)

    # then
    assert cleared.status_code == 422 and cleared.json()['field'] == 'token' and SEEN == []


def test_switching_a_plugin_back_on_revives_it_and_nudges_its_jobs(store):
    # given
    with served(store) as c:
        nudged = []
        c.app.state.scheduler.nudge = nudged.append
        c.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH)
        tombs = [t for t in store.load().get('retired') or [] if t['plugin'] == 'demo']

        # when
        on = c.put('/api/admin/plugins/demo', json={'enabled': True}, headers=AUTH)

    # then
    assert tombs and on.status_code == 200
    assert [t for t in store.load().get('retired') or [] if t['plugin'] == 'demo'] == [] and nudged == ['demo.pulse']


def test_a_locked_plugin_can_be_switched_off_and_its_instances_edited_but_not_switched_on(store):
    # given
    registry = Registry.load({'SKOPAX_PLUGINS': PLUGINS}, builtin=(), entry_points=False)
    with served(store, registry=registry) as c:
        add_instance(store, 'enterprise-demo', 'main', sealer_key='', primary=True, url='https://demo.example.com')

        # when
        off = c.put('/api/admin/plugins/enterprise-demo', json={'enabled': False}, headers=AUTH)
        on = c.put('/api/admin/plugins/enterprise-demo', json={'enabled': True}, headers=AUTH)
        quiet = c.patch('/api/admin/plugins/enterprise-demo/instances/main', json={'enabled': False}, headers=AUTH)
        loud = c.patch('/api/admin/plugins/enterprise-demo/instances/main', json={'enabled': True}, headers=AUTH)
        gone = c.delete('/api/admin/plugins/enterprise-demo/instances/main', headers=AUTH)
        broken = c.put('/api/admin/plugins/tests.plugins.broken_plugin', json={'enabled': False}, headers=AUTH)

    # then
    assert (off.status_code, on.status_code) == (200, 409) and off.json()['enabled'] is False
    assert (quiet.status_code, loud.status_code, gone.status_code) == (200, 409, 204)
    assert broken.status_code == 409 and 'ImportError' in broken.json()['detail']


def test_only_one_connection_test_per_plugin_runs_at_a_time(store, monkeypatch):
    # given
    monkeypatch.setattr('skopax.app.TEST_TIMEOUT_S', 0.2)
    waiting = {'url': 'https://wait.example.com', 'token': SECRET}
    with served(store) as c:
        # when
        first = c.post('/api/admin/plugins/demo/test', json={'values': waiting}, headers=AUTH)
        second = c.post('/api/admin/plugins/demo/test', json={'values': GOOD}, headers=AUTH)
        HOLD.set()
        deadline = time.monotonic() + 5
        while any(t.name == 'test-demo' and t.is_alive() for t in threading.enumerate()) and time.monotonic() < deadline:
            time.sleep(0.01)
        third = c.post('/api/admin/plugins/demo/test', json={'values': GOOD}, headers=AUTH)

    # then
    assert first.json()['ok'] is False and '0.2' in first.json()['summary']
    assert second.json() == {'ok': False, 'summary': msg('integrations.test_busy'), 'ms': 0}
    assert third.json()['ok'] is True and len(SEEN) == 2


def test_a_connection_test_started_right_after_another_ends_is_not_refused(store, monkeypatch):
    # given
    class Lingering(threading.Thread):
        def run(self):
            super().run()
            if self.name.startswith('test-'):
                time.sleep(0.3)

    monkeypatch.setattr('skopax.runtime.threading.Thread', Lingering)
    with served(store) as c:
        # when
        first = c.post('/api/admin/plugins/demo/test', json={'values': GOOD}, headers=AUTH)
        second = c.post('/api/admin/plugins/demo/test', json={'values': GOOD}, headers=AUTH)

    # then
    assert first.json()['ok'] is True
    assert second.json()['ok'] is True, second.json()


def test_a_slow_test_of_one_instance_does_not_hold_the_test_of_another(store, monkeypatch):
    # given
    monkeypatch.setattr('skopax.app.TEST_TIMEOUT_S', 0.2)
    with served(store) as c:
        add(c, 'main')
        add(c, 'east')

        # when
        slow = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {'url': 'https://wait.example.com', 'token': SECRET}}, headers=AUTH)
        monkeypatch.setattr('skopax.app.TEST_TIMEOUT_S', 15)
        other = c.post('/api/admin/plugins/demo/test', json={'name': 'east', 'values': GOOD}, headers=AUTH)
        again = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': GOOD}, headers=AUTH)
        HOLD.set()

    # then
    assert slow.json()['ok'] is False and '0.2' in slow.json()['summary']
    assert other.json()['ok'] is True
    assert again.json() == {'ok': False, 'summary': msg('integrations.test_busy'), 'ms': 0}


def test_a_plugin_without_a_test_says_so_before_it_checks_the_values(store):
    # given
    bare = dataclasses.replace(DEMO, test=None)
    with served(store, bare) as c:
        # when
        answer = c.post('/api/admin/plugins/demo/test', json={'values': {'url': 'ftp://example.com'}}, headers=AUTH)

    # then
    assert answer.status_code == 200 and answer.json() == {'ok': False, 'summary': msg('integrations.test_none'), 'ms': 0}


def test_an_instance_another_pod_just_added_can_be_edited_and_deleted_at_once(store):
    # given
    with served(store) as c:
        add_instance(store, DEMO, 'main', sealer_key='', primary=True, url='https://demo.example.com')

        # when
        edited = c.patch('/api/admin/plugins/demo/instances/main', json={'values': {'mode': 'slow'}}, headers=AUTH)
        add_instance(store, DEMO, 'east', sealer_key='', url='https://east.example.com')
        deleted = c.delete('/api/admin/plugins/demo/instances/east', headers=AUTH)

    # then
    assert edited.status_code == 200 and edited.json()['instance']['values']['mode'] == 'slow'
    assert deleted.status_code == 204


def test_a_patch_that_changes_nothing_writes_no_changed_line(store):
    # given
    with served(store) as c:
        add(c)

        # when
        same = c.patch('/api/admin/plugins/demo/instances/main', json={'values': {'url': GOOD['url'], 'token': SECRET}}, headers=AUTH)
        other = c.patch('/api/admin/plugins/demo/instances/main', json={'values': {'url': 'https://two.example.com', 'token': SECRET}}, headers=AUTH)

    # then
    assert same.status_code == other.status_code == 200
    changed = [t for t in texts(store) if 'was changed' in t]
    assert changed == [msg('integrations.instance_changed', plugin='Demo', instance='main', fields='url')]


def test_a_secret_with_no_printable_character_leaves_the_summary_readable(store):
    # given
    with served(store) as c:
        # when
        answer = c.post('/api/admin/plugins/demo/test', json={'values': {'url': 'https://demo.example.com', 'token': chr(7) * 3}}, headers=AUTH)

    # then
    assert answer.json()['summary'] == 'answered https://demo.example.com'


def test_a_switched_off_instance_cannot_be_made_primary_unless_it_is_switched_on_in_the_same_step(store):
    # given
    with served(store) as c:
        add(c, 'main', True)
        add(c, 'east', False, values={'url': 'https://east.example.com', 'token': SECRET})

        # when
        refused = c.patch('/api/admin/plugins/demo/instances/east', json={'primary': True}, headers=AUTH)
        still = c.app.state.instances.primary('demo').name
        both = c.patch('/api/admin/plugins/demo/instances/east', json={'enabled': True, 'primary': True}, headers=AUTH)

    # then
    assert refused.status_code == 409 and refused.json()['detail'] == msg('instances.primary_disabled', name='east') and still == 'main'
    assert both.status_code == 200 and both.json()['instance']['primary'] is True


def test_a_plugin_broken_at_runtime_can_be_switched_off_and_its_cards_and_passes_retire(store):
    # given
    with served(store) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True, values={'url': 'https://east.example.com', 'token': SECRET})
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        opened = [a['key'] for a in store.load()['anomalies'] if a['status'] == 'open']
        c.app.state.registry.mark_broken('demo', 'a migration failed')
        c.app.state.runtime.refresh()

        # when
        off = c.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH)
        state = store.load()

    # then
    assert len(opened) == 2 and off.status_code == 200
    assert [a['key'] for a in state['anomalies'] if a['status'] == 'open'] == []
    assert not {'pulse', 'pulse-east'} & set(state['sources']) and not {'pulse', 'pulse-east'} & set(state.get('serverPasses', {}))


def test_a_plugin_broken_at_load_can_be_switched_off_without_a_retire(store, tmp_path, monkeypatch):
    # given
    (tmp_path / 'odd_checks.py').write_text("import dataclasses\nfrom tests.plugins import demo_plugin\nPLUGIN = dataclasses.replace(demo_plugin.PLUGIN, checks=({'id': 'odd'},))\n", encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = Registry.load({'SKOPAX_PLUGINS': 'odd_checks'}, builtin=(), entry_points=False)
    with served(store, registry=registry) as c:
        # when
        off = c.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH)

    # then
    assert off.status_code == 200 and 'could not be checked' in off.json()['reason']
    assert store.db['plugin_state'].find_one({'_id': 'demo'})['enabled'] is False


PAIR = two_checks_plugin.PLUGIN
def pair_key(check: str, name: str) -> str:
    return two_checks_plugin.key_of(check, name) + ('' if name == 'main' else f'@{name}')
EAST = {'url': 'https://east.example.com', 'token': SECRET}
BAD_PASS = 'error: timed out after 30 s'


def seed_passes(store, passes):
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}, 'serverPasses': passes}, None)


def both_ran(c, now):
    c.app.state.sweeper.run('demo', 'pulse', now)
    c.app.state.sweeper.run('demo', 'beat', now)


def instance_rows(c, plugin='demo') -> dict:
    return {i['name']: i for i in listed(c)[plugin]['instances']}


def test_admin_plugins_carry_health_and_checks(store):
    # given
    other = minimal('other', 'Other', fields=(Field('url', 'URL', 'url'),))
    with served(store, DEMO, other) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True, values=EAST)
        add(c, 'secondary', True, values={'url': 'https://secondary.example.com', 'token': SECRET})
        add(c, 'main', True, values={'url': 'https://other.example.com'}, plugin='other')
        c.put('/api/admin/plugins/other', json={'enabled': False}, headers=AUTH)
        seed_passes(store, {'pulse': {'at': iso(NOW), 'status': 'ok'}, 'pulse-east': {'at': iso(NOW + 5), 'status': BAD_PASS}})

        # when
        rows = listed(c)

    # then
    ok, pending, off = msg('integrations.health.ok'), msg('integrations.health.pending'), msg('integrations.health.off')
    assert [(x['id'], x['label']) for x in rows['demo']['checks']] == [('pulse', 'Demo pulse')]
    assert rows['demo']['health'] == {'state': 'error', 'text': BAD_PASS, 'at': iso(NOW + 5), 'instances': [
        {'name': 'main', 'state': 'ok', 'text': ok, 'at': iso(NOW)}, {'name': 'east', 'state': 'error', 'text': BAD_PASS, 'at': iso(NOW + 5)},
        {'name': 'secondary', 'state': 'pending', 'text': pending, 'at': None}]}
    assert rows['other']['health'] == {'state': 'off', 'text': off, 'at': None, 'instances': [{'name': 'main', 'state': 'off', 'text': off, 'at': None}]}


def test_health_of_a_parked_instance_of_one_that_runs_no_check_and_of_plugins_with_nothing_to_set_up_or_check(store):
    # given
    bare = minimal('bare', 'Bare', fields=(Field('url', 'URL', 'url'),))
    solo = minimal('solo', 'Solo')
    quiet = minimal('quiet', 'Quiet', fields=(Field('url', 'URL', 'url'),), checks=())
    seed_passes(store, {'pulse': {'at': iso(NOW), 'status': 'ok'}, 'solo': {'at': iso(NOW + 9), 'status': 'ok \u00b7 1 card dropped'}})
    add_instance(store, DEMO, 'main', primary=True, url='https://demo.example.com')
    add_instance(store, DEMO, 'east', url='not a url')
    add_instance(store, DEMO, 'west', url='https://west.example.com')
    store.db['instances'].update_one({'_id': 'demo/west'}, {'$set': {'offChecks': ['pulse']}})
    add_instance(store, quiet, 'main', primary=True, url='https://quiet.example.com')

    # when
    with served(store, DEMO, bare, solo, quiet) as c:
        rows = listed(c)

    # then
    parked = msg('instances.invalid.url', field='URL')
    assert rows['demo']['health'] == {'state': 'error', 'text': parked, 'at': None, 'instances': [
        {'name': 'main', 'state': 'ok', 'text': msg('integrations.health.ok'), 'at': iso(NOW)}, {'name': 'east', 'state': 'error', 'text': parked, 'at': None},
        {'name': 'west', 'state': 'off', 'text': msg('integrations.health.unchecked'), 'at': None}]}
    assert rows['bare']['health'] == {'state': 'off', 'text': msg('integrations.health.unset'), 'at': None, 'instances': []}
    assert rows['solo']['health'] == {'state': 'ok', 'text': msg('integrations.health.ok'), 'at': iso(NOW + 9), 'instances': []}
    neutral = msg('integrations.health.no_checks')
    assert rows['quiet']['health'] == {'state': 'none', 'text': neutral, 'at': None, 'instances': [{'name': 'main', 'state': 'none', 'text': neutral, 'at': None}]}


def test_what_it_watches_skips_an_unchecked_check_for_one_instance(store):
    # given
    with served(store, PAIR) as c:
        nudged = []
        c.app.state.scheduler.nudge = nudged.append
        add(c, 'main', True)
        add(c, 'east', True, values=EAST)
        nudged.clear()
        writes = []
        real = c.app.state.instances.set_checks
        c.app.state.instances.set_checks = lambda *a: writes.append(a[2]) or real(*a)

        # when
        off = c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['pulse']}, headers=AUTH)
        doc = store.db['instances'].find_one({'_id': 'demo/east'})
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        ran = store.load()
        same = c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['pulse']}, headers=AUTH)
        quiet = list(nudged)
        back = c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': []}, headers=AUTH)
        c.app.state.sweeper.run('demo', 'pulse', NOW + 900)
        again = store.load()

    # then
    assert off.status_code == 200 and off.json()['instance']['offChecks'] == ['pulse'] and doc['offChecks'] == ['pulse'] and doc['updatedBy'] == doc['createdBy']
    assert set(ran['sources']) == {'pulse'} and [a['key'] for a in ran['anomalies']] == [pair_key('pulse', 'main')]
    assert writes == [frozenset({'pulse'}), frozenset()]
    assert same.status_code == 200 and quiet == [] and texts(store).count(msg('integrations.check_off', plugin='Demo', instance='east', check='Demo pulse')) == 1
    assert back.status_code == 200 and back.json()['instance']['offChecks'] == [] and nudged == ['demo.pulse']
    assert again['sources']['pulse-east'] == 'ok' and {a['key']: a['status'] for a in again['anomalies']}[pair_key('pulse', 'east')] == 'open'
    assert not [t for t in again['retired'] if t.get('check')]
    assert msg('integrations.check_on', plugin='Demo', instance='east', check='Demo pulse') in texts(store)


def test_unchecking_retires_only_that_checks_cards_and_rows(store):
    # given
    with served(store, PAIR) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True, values=EAST)
        both_ran(c, NOW)
        before = store.load()

        # when
        off = c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['pulse']}, headers=AUTH)
        after = store.load()

    # then
    found, was = {a['key']: a for a in after['anomalies']}, {a['key']: a for a in before['anomalies']}
    gone = found.pop(pair_key('pulse', 'east'))
    assert off.status_code == 200 and gone['status'] == 'cleared' and gone['expired'] is True
    assert gone['note']['text'] == msg('cards.retired_check', plugin='Demo', instance='east', check='Demo pulse')
    assert found == {k: a for k, a in was.items() if k != pair_key('pulse', 'east')} and {a['status'] for a in found.values()} == {'open'}
    assert set(after['sources']) == set(after['serverPasses']) == {'pulse', 'beat', 'beat-east'}
    east = lambda state, section: [r['svc'] for r in state.get(section) or [] if r.get('cluster') == 'east']
    assert east(after, 'routes') == [] and east(after, 'beats') == east(after, 'services') == ['east-beat']
    main = lambda state: [(s, r['svc']) for s in ('services', 'routes', 'beats') for r in state.get(s) or [] if not r.get('cluster')]
    assert main(after) == main(before) != []
    assert [(t['name'], t.get('check')) for t in after['retired']] == [('east', 'pulse')]


def test_unknown_check_ids_are_422(store):
    # given
    with served(store, PAIR) as c:
        add(c, 'main', False)

        # when
        patched = c.patch('/api/admin/plugins/demo/instances/main', json={'values': {'mode': 'slow'}, 'offChecks': ['pulse', 'nope']}, headers=AUTH)
        made = c.post('/api/admin/plugins/demo/instances', json={'name': 'east', 'values': EAST, 'offChecks': ['nope']}, headers=AUTH)
        rows = instance_rows(c)

    # then
    assert patched.status_code == 422 and patched.json() == {'detail': msg('instances.invalid.checks', check='nope'), 'field': 'offChecks'}
    assert made.status_code == 422 and made.json() == {'detail': msg('instances.invalid.checks', check='nope'), 'field': 'offChecks'}
    assert list(rows) == ['main'] and rows['main']['values']['mode'] == 'fast' and rows['main']['offChecks'] == []
    assert store.db['instances'].find_one({'_id': 'demo/main'}).get('offChecks') is None


def test_a_new_instance_can_start_without_some_of_its_checks(store):
    # given
    with served(store, PAIR) as c:
        nudged = []
        c.app.state.scheduler.nudge = nudged.append
        add(c, 'main', True)

        # when
        made = c.post('/api/admin/plugins/demo/instances', json={'name': 'east', 'values': EAST, 'enabled': True, 'offChecks': ['beat', 'beat']}, headers=AUTH)
        both_ran(c, NOW)
        state = store.load()

    # then
    assert made.status_code == 201 and made.json()['offChecks'] == ['beat'] and store.db['instances'].find_one({'_id': 'demo/east'})['offChecks'] == ['beat']
    assert set(state['sources']) == {'pulse', 'pulse-east', 'beat'}


def test_source_instance_checks_are_an_admin_override(store):
    # given
    feed = SourceData('config file', {}, ({'plugin': 'demo', 'name': 'east', 'url': 'https://east.example.com', 'token': 'from-source'},
                                          {'plugin': 'demo', 'name': 'west', 'url': 'https://west.example.com', 'offChecks': 'pulse'}))
    sources = SourceSet([('config file', lambda: feed)], {'config file': feed})
    with served(store, PAIR, sources=sources) as c:
        c.app.state.scheduler.nudge = lambda name: None

        # when
        off = c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['beat']}, headers=AUTH)
        doc = store.db['instances'].find_one({'_id': 'demo/east'})
        rows = instance_rows(c)

    # then
    assert off.status_code == 200 and doc['offChecks'] == ['beat'] and not doc.get('values') and not doc.get('secrets')
    assert (rows['east']['source'], rows['east']['offChecks'], rows['east']['values']['url'], rows['east']['problem']) == ('config file', ['beat'], 'https://east.example.com', None)
    assert rows['west']['problem'] == msg('instances.invalid.unknown', field='offChecks') and rows['west']['offChecks'] == []


def test_a_plugin_broken_after_it_loaded_still_says_whether_it_is_switched_on(store):
    # given
    with served(store) as c:
        c.app.state.registry.mark_broken('demo', 'a migration failed')
        c.app.state.runtime.refresh()

        # when
        row = listed(c)['demo']
        off = c.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH)

    # then
    assert set(row) == {'id', 'module', 'reason', 'enabled', 'origin'} and row['enabled'] is True
    assert off.status_code == 200 and set(off.json()) == {'id', 'module', 'reason', 'enabled', 'origin'} and off.json()['enabled'] is False


def test_an_instance_made_again_under_the_same_name_runs_every_check_it_watches_at_once(store):
    # given
    with served(store, PAIR) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True, values=EAST)
        c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['pulse']}, headers=AUTH)
        c.delete('/api/admin/plugins/demo/instances/east', headers=AUTH)

        saves = []
        real = store.save_state
        store.save_state = lambda *a, **k: saves.append(1) or real(*a, **k)

        # when
        made = add(c, 'east', True, values=EAST)
        store.save_state = real
        both_ran(c, NOW)
        state = store.load()

    # then
    assert made.status_code == 201 and made.json()['offChecks'] == [] and len(saves) == 1
    assert {'pulse-east', 'beat-east'} <= set(state['sources']) and not [t for t in state['retired'] if t['name'] == 'east']


def test_switching_on_an_instance_made_again_lifts_the_tombstones_of_the_checks_it_runs(store):
    # given
    with served(store, PAIR) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True, values=EAST)
        c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['pulse']}, headers=AUTH)
        c.delete('/api/admin/plugins/demo/instances/east', headers=AUTH)
        add(c, 'east', False, values=EAST)

        # when
        on = c.patch('/api/admin/plugins/demo/instances/east', json={'enabled': True}, headers=AUTH)
        both_ran(c, NOW)
        state = store.load()

    # then
    assert on.status_code == 200 and on.json()['instance']['offChecks'] == []
    assert {'pulse-east', 'beat-east'} <= set(state['sources']) and not [t for t in state['retired'] if t['name'] == 'east']


def test_unchecking_every_check_in_one_patch_leaves_no_rows_of_the_instance(store):
    # given
    with served(store, PAIR) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True, values=EAST)
        both_ran(c, NOW)

        # when
        off = c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['pulse', 'beat']}, headers=AUTH)
        state = store.load()

    # then
    assert off.status_code == 200 and sorted(off.json()['instance']['offChecks']) == ['beat', 'pulse']
    assert [(s, r['svc']) for s in ('services', 'routes', 'beats') for r in state.get(s) or [] if r.get('cluster') == 'east'] == []
    assert {a['key']: a['status'] for a in state['anomalies'] if a.get('cluster') == 'east'} == {pair_key('pulse', 'east'): 'cleared', pair_key('beat', 'east'): 'cleared'}
    assert set(state['sources']) == {'pulse', 'beat'}


def test_a_check_retire_that_fails_is_finished_by_the_next_heal(store):
    # given
    with served(store, PAIR) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True, values=EAST)
        both_ran(c, NOW)
        sweeper = c.app.state.sweeper
        real, calls = sweeper.retire, []

        def flaky(*args):
            calls.append(args)
            if len(calls) == 1:
                raise Conflict(0)
            return real(*args)

        sweeper.retire = flaky

        # when
        off = c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['pulse']}, headers=AUTH)
        half = store.load()
        sweeper.heal()
        healed = store.load()

    # then
    cards = lambda state: {a['key']: a for a in state['anomalies']}
    east = lambda state, section: [r['svc'] for r in state.get(section) or [] if r.get('cluster') == 'east']
    assert off.status_code == 409 and instance_rows(c)['east']['offChecks'] == ['pulse']
    assert cards(half)[pair_key('pulse', 'east')]['status'] == 'open' and 'pulse-east' in half['sources'] and east(half, 'routes') == ['east-pulse']
    gone = cards(healed)[pair_key('pulse', 'east')]
    assert gone['status'] == 'cleared' and gone['note']['text'] == msg('cards.retired_check', plugin='Demo', instance='east', check='Demo pulse')
    assert 'pulse-east' not in healed['sources'] and 'pulse-east' not in healed['serverPasses'] and east(healed, 'routes') == []
    assert cards(healed)[pair_key('beat', 'east')]['status'] == 'open' and east(healed, 'beats') == ['east-beat'] and 'beat-east' in healed['sources']


@pytest.mark.parametrize('turn_on', [False, True])
def test_a_checks_change_whose_retire_conflicts_says_what_was_saved(store, turn_on):
    # given
    with served(store, PAIR) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', not turn_on, values=EAST)
        both_ran(c, NOW)
        sweeper = c.app.state.sweeper
        real, calls = sweeper.retire, []

        def flaky(*args):
            calls.append(args)
            if len(calls) == 1:
                raise Conflict(0)
            return real(*args)

        sweeper.retire = flaky

        # when
        sent = {'offChecks': ['pulse'], **({'enabled': True} if turn_on else {})}
        off = c.patch('/api/admin/plugins/demo/instances/east', json=sent, headers=AUTH)
        row = instance_rows(c)['east']

    # then
    key = 'integrations.checks_pending_off' if turn_on else 'integrations.checks_pending'
    assert (off.status_code, off.json()) == (409, {'detail': msg(key, instance='east')})
    assert row['offChecks'] == ['pulse'] and row['enabled'] is (not turn_on)


def test_health_reads_only_the_checks_an_instance_runs(store):
    # given
    two_checks_plugin.PLAN['beat:east'] = {'fail': 'boom'}
    with served(store, PAIR) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        add(c, 'east', True, values=EAST)
        both_ran(c, NOW)

        # when
        failing = listed(c)['demo']['health']
        c.app.state.instances.set_checks('demo', 'east', ['beat'], 'alice')
        unchecked = listed(c)['demo']['health']
        state = store.load()

    # then
    assert state['sources']['beat-east'] == 'error: boom'
    assert (failing['state'], failing['text']) == ('error', 'error: boom') and [i['state'] for i in failing['instances']] == ['ok', 'error']
    assert (unchecked['state'], [i['state'] for i in unchecked['instances']]) == ('ok', ['ok', 'ok'])


def test_the_integrations_page_names_every_plugin_category():
    # given
    page = (Path(__file__).resolve().parent.parent / 'skopax' / 'web' / 'static' / 'integrations.js').read_text(encoding='utf-8')

    # when
    names = re.search(r'^var INT_CATS = \{(.*?)\};$', page, re.M)[1]
    keys = re.findall(r"(?:^|, )([a-z]+): '[A-Z][A-Za-z &]*'", names)

    # then
    assert keys == list(CATEGORIES)
