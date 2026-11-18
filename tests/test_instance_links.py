import dataclasses
import hashlib
import logging

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from skopax.app import create_app
from skopax.config import load_settings
from skopax.store import Store
from skopax.tunables import Invalid, TunableStore, instance_links, parse, registry

STAGING = 'https://staging.example.com'
EU = 'https://eu.example.com/board'
COLOURS = ('#e3a744', '#9a7ce3', '#5cbf80', '#f0857a', '#7eaaf2', '#c98500')
SECRET_URL = 'https://u:p@private.example.com'
HIDDEN = 'not shown: it breaks the Other instances rules'


def known(key: str):
    return {t.key: t for t in registry()}[key]


def colour(name: str) -> str:
    return COLOURS[hashlib.sha256(name.encode()).digest()[0] % 6]


def test_instance_links_are_validated():
    # given
    good = f' staging={STAGING} , eu cluster = {EU} ,'
    refused = {
        'eleven': ', '.join(f'n{i}=https://n{i}.example.com' for i in range(11)),
        'name': 'bad<name>=https://x.example.com',
        'scheme': f'staging={STAGING}, x=javascript:alert(1)',
        'userinfo': f'x={SECRET_URL}',
        'repeat': f'staging={STAGING}, Staging={EU}',
        'pair': 'staging',
        'host': 'x=https://',
        'long': 'n' * 41 + '=https://x.example.com',
    }

    # when
    parsed = instance_links(good)
    errors = {}
    for case, raw in refused.items():
        with pytest.raises(Invalid) as e:
            instance_links(raw)
        errors[case] = str(e.value)
    with pytest.raises(Invalid) as put:
        parse(known('core.instance-links'), refused['userinfo'], settings())

    # then
    assert parsed == [{'name': 'staging', 'url': STAGING}, {'name': 'eu cluster', 'url': EU}]
    assert instance_links('') == [] and instance_links(' , ') == []
    assert parse(known('core.instance-links'), good, settings()) == good.strip(), 'a valid value is stored as typed'
    assert errors == {
        'eleven': 'Other instances, entry 11: at most 10 other instances can be listed',
        'name': 'Other instances, entry 1: a name is 1 to 40 letters, digits, spaces, dots, dashes or underscores',
        'scheme': 'Other instances, entry 2: the address must start with http:// or https:// and name a host',
        'userinfo': 'Other instances, entry 1: the address must not hold a user name or password',
        'repeat': 'Other instances, entry 2: the name is already used by an earlier entry',
        'pair': 'Other instances, entry 1: write each entry as name=url',
        'host': 'Other instances, entry 1: the address must start with http:// or https:// and name a host',
        'long': 'Other instances, entry 1: a name is 1 to 40 letters, digits, spaces, dots, dashes or underscores',
    }
    assert str(put.value) == errors['userinfo'], 'an admin PUT runs the same check'
    leaks = ('u:p', 'private', 'javascript', '<name>', 'example.com', 'Staging', 'n10')
    assert [(case, word) for case, text in errors.items() for word in leaks if word in text] == [], 'a refusal names the position and the rule, never the value'


def test_me_lists_the_current_instance_and_its_links(store: Store):
    # given
    env = load_settings(env={'SKOPAX_ENV_LABEL': 'production', 'SKOPAX_INSTANCE_LINKS': f'staging={STAGING}, eu={EU}'})
    labelled = dataclasses.replace(settings(), env_label=env.env_label, instance_links=env.instance_links)
    hosted = dataclasses.replace(settings(), base_url='https://ops.example.com:8443/board')
    bare = dataclasses.replace(settings(), base_url='')

    # when
    seen = []
    for s in (labelled, hosted, bare):
        with TestClient(create_app(s, store)) as c:
            seen.append(c.get('/api/me').json()['instances'])

    # then
    assert (env.env_label, env.instance_links) == ('production', f'staging={STAGING}, eu={EU}')
    assert seen[0] == {'current': {'name': 'production', 'letter': 'P'},
                       'links': [{'name': 'staging', 'url': STAGING, 'letter': 'S', 'colour': colour('staging')}, {'name': 'eu', 'url': EU, 'letter': 'E', 'colour': colour('eu')}]}
    assert all(link['colour'] in COLOURS for link in seen[0]['links'])
    assert seen[1] == {'current': {'name': 'ops.example.com', 'letter': 'O'}, 'links': []}, 'with no label, the host of the base URL'
    assert seen[2] == {'current': {'name': 'local', 'letter': 'L'}, 'links': []}, 'with neither, local'


def test_an_admin_sets_the_links_live_and_a_refused_value_is_never_echoed(store: Store):
    # given
    with TestClient(create_app(settings(), store)) as c:
        # when
        listed = [i for i in c.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == 'core.instance-links']
        refused = c.put('/api/admin/settings', json={'key': 'core.instance-links', 'value': f'x={SECRET_URL}'}, headers=AUTH)
        after_refused = c.get('/api/me').json()['instances']['links']
        saved = c.put('/api/admin/settings', json={'key': 'core.instance-links', 'value': f'staging={STAGING}'}, headers=AUTH)
        after_saved = c.get('/api/me').json()['instances']['links']

    # then
    assert [(i['group'], i['label'], i['kind'], i['live'], i['value']) for i in listed] == [('General', 'Other instances', 'text', True, '')]
    assert refused.status_code == 422
    assert refused.json()['detail'] == 'Other instances, entry 1: the address must not hold a user name or password'
    assert 'u:p' not in refused.text and 'private' not in refused.text
    assert after_refused == []
    assert store.db['tunable_changes'].count_documents({'key': 'core.instance-links'}) == 1, 'only the saved value is in the history'
    assert saved.status_code == 200
    assert after_saved == [{'name': 'staging', 'url': STAGING, 'letter': 'S', 'colour': colour('staging')}], 'the switcher follows the setting without a restart'


def test_a_bad_env_or_stored_value_lists_no_links_and_warns_once_with_the_type_only(store: Store, caplog):
    # given
    from_env = dataclasses.replace(settings(), instance_links=f'x={SECRET_URL}')
    good_env = dataclasses.replace(settings(), instance_links=f'staging={STAGING}')

    # when
    with caplog.at_level(logging.WARNING):
        with TestClient(create_app(from_env, store)) as c:
            first = c.get('/api/me')
            second = c.get('/api/me')
        env_records = [r.getMessage() for r in caplog.records if 'instance' in r.getMessage()]
        caplog.clear()
        with TestClient(create_app(good_env, store)) as c:
            TunableStore(store.db).write('core.instance-links', f'x={SECRET_URL}', 'admin@example.com', None)
            c.get('/api/admin/settings', headers=AUTH)
            stored = c.get('/api/me')
        stored_records = [r.getMessage() for r in caplog.records if 'instance' in r.getMessage()]

    # then
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()['instances']['links'] == [] == second.json()['instances']['links']
    assert env_records == ['core.instance-links ignored, the instance switcher lists no other instances: Invalid'], 'one warning, with the type only'
    assert stored.status_code == 200
    assert stored.json()['instances']['links'] == [{'name': 'staging', 'url': STAGING, 'letter': 'S', 'colour': colour('staging')}], 'a bad stored value is skipped and the configured one stays'
    assert stored_records == ['admin setting core.instance-links skipped: Invalid']
    assert not [m for m in env_records + stored_records if 'u:p' in m or 'private' in m]


def admin_row(c) -> dict:
    return [i for i in c.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == 'core.instance-links'][0]


def store_raw(store: Store, value: str) -> None:
    store.db['tunables'].replace_one({'_id': 'core.instance-links'}, {'_id': 'core.instance-links', 'value': value, 'by': 'admin@example.com', 'at': '2026-11-15T01:00:00Z'}, upsert=True)
    store.db['tunables_rev'].update_one({'_id': 'rev'}, {'$inc': {'n': 1}}, upsert=True)


def leaks(text: str) -> list[str]:
    return [w for w in ('u:p', 'private.example.com', f'x={SECRET_URL}') if w in text]


def test_admin_never_returns_a_refused_env_value_and_names_its_layer(store: Store):
    # given
    rule = 'Entry 1: the address must not hold a user name or password.'
    from_env = dataclasses.replace(settings(), instance_links=f'x={SECRET_URL}')

    # when
    with TestClient(create_app(from_env, store)) as c:
        body = c.get('/api/admin/settings', headers=AUTH).text
        row = admin_row(c)
        saved = c.put('/api/admin/settings', json={'key': 'core.instance-links', 'value': f'staging={STAGING}'}, headers=AUTH)
        reset = c.delete('/api/admin/settings', params={'key': 'core.instance-links'}, headers=AUTH)
        history = c.get('/api/admin/history', headers=AUTH).text
    written = ' '.join(str(d) for name in ('tunable_changes', 'activity') for d in store.db[name].find({}))

    # then
    assert {k: row[k] for k in ('value', 'base', 'source', 'from', 'problem')} == {'value': None, 'base': None, 'source': 'config', 'from': 'environment',
                                                                                 'problem': 'The environment value is not used. ' + rule}
    assert leaks(body) == [], 'the admin list never returns the refused value'
    assert (saved.status_code, saved.json()['change']['before'], saved.json()['item']['problem']) == (200, HIDDEN, 'The environment value is not used. ' + rule)
    assert (reset.status_code, reset.json()['change']['after'], reset.json()['item']['value']) == (200, HIDDEN, None)
    assert leaks(saved.text + reset.text + history + written) == [], 'nor does a save, a reset, the history or the activity log'


def test_a_refused_stored_value_is_ignored_shown_as_a_note_and_never_returned(store: Store):
    # given
    good_env = dataclasses.replace(settings(), instance_links=f'staging={STAGING}')
    bad_env = dataclasses.replace(settings(), instance_links=f'x={SECRET_URL}')
    rule = 'Entry 1: the address must not hold a user name or password.'

    # when
    with TestClient(create_app(good_env, store)) as c:
        store_raw(store, f'x={SECRET_URL}')
        TunableStore(store.db).write_change('core.instance-links', f'y={SECRET_URL}', f'x={SECRET_URL}', 'admin@example.com')
        body = c.get('/api/admin/settings', headers=AUTH).text
        row = admin_row(c)
        links = c.get('/api/me').json()['instances']['links']
        history = c.get('/api/admin/history', headers=AUTH).json()
    with TestClient(create_app(bad_env, store)) as c:
        both = admin_row(c)

    # then
    assert links == [{'name': 'staging', 'url': STAGING, 'letter': 'S', 'colour': colour('staging')}], 'the configured links serve'
    assert {k: row[k] for k in ('value', 'base', 'source', 'from', 'by', 'at', 'problem')} == {'value': f'staging={STAGING}', 'base': f'staging={STAGING}', 'source': 'config', 'from': 'environment',
                                                                                            'by': None, 'at': None, 'problem': 'The Admin value is not used. ' + rule}, 'no "set here" for a value that is not used'
    assert leaks(body) == []
    change = [h for h in history if h['key'] == 'core.instance-links'][0]
    assert (change['old'], change['new']) == (HIDDEN, HIDDEN), 'the history hides a refused value too'
    assert leaks(str(history)) == []
    assert {k: both[k] for k in ('value', 'base', 'source', 'problem')} == {'value': None, 'base': None, 'source': 'config',
                                                                           'problem': 'The Admin value is not used. ' + rule + ' The environment value is not used. ' + rule}
