import dataclasses

from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from skopax import settingcards
from skopax.app import create_app
from skopax.config import load_settings
from skopax.investigator.web import DOMAIN
from skopax.store import Store
from skopax.tunables import GROUPS, TunableStore, registry

STAGING = 'https://staging.example.com'
LISTS = {
    'mcp.allowed-clients': {'kind': 'client-id', 'separator': ','},
    'self-review.dm': {'kind': 'email', 'separator': ','},
    'jira.projects': {'kind': 'project-key', 'separator': ','},
    'slack.push-user': {'kind': 'slack-user-id', 'separator': ','},
    'slack.ask-channels': {'kind': 'channel', 'separator': ',', 'keywords': ['all', 'main']},
    'sso.allowed-domains': {'kind': 'domain', 'separator': ','},
    'core.cert-skip': {'kind': 'host', 'separator': ','},
    'sso.scope': {'kind': 'scope', 'separator': ' '},
    'alerts.warn': {'kind': 'alert-name', 'separator': ','},
    'investigate.web-domains': {'kind': 'web-host', 'separator': ',', 'keywords': ['any', 'off', 'none', 'false', 'no']},
    'core.own-sites': {'kind': 'own-site', 'separator': ','},
}
SAMPLES = {
    'mcp.allowed-clients': 'cli-one, cli-two',
    'self-review.dm': 'alice@example.com, bob@example.com',
    'jira.projects': 'OPS, WEB',
    'slack.push-user': 'U012ABCDE, U034FGHIJ',
    'slack.ask-channels': 'C012ABCDEFG, C034HIJKLMN',
    'sso.allowed-domains': 'example.com, example.org',
    'core.cert-skip': 'shop.example.com, *.example.com',
    'sso.scope': 'openid profile email',
    'investigate.web-domains': 'example.com, -blog.google',
    'core.own-sites': 'example.com, shop.example.org',
}


def items(c) -> dict:
    return {i['key']: i for i in c.get('/api/admin/settings', headers=AUTH).json()['items']}


def test_other_instances_is_its_own_section_and_card_not_part_of_general(store: Store):
    # given
    known = {t.key: t for t in registry()}['core.workspace-links']

    # when
    with TestClient(create_app(settings(), store)) as c:
        listed = items(c)

    # then
    assert known.group == 'Workspaces' and 'Workspaces' in GROUPS
    assert (listed['core.workspace-links']['group'], listed['core.workspace-links']['card']) == ('Workspaces', 'Other workspaces')
    assert [k for k, i in listed.items() if i['group'] == 'Workspaces'] == ['core.workspace-links', 'core.workspace-icon']
    assert settingcards.CARD_OF['core.workspace-links'][:2] == ('Workspaces', 'Other workspaces')
    assert 'core.workspace-links' not in [k for _, keys in settingcards.CARDS['General'] for k in keys]


def test_the_instances_entry_carries_the_rules_and_the_deployment_name(store: Store):
    # given
    env = dataclasses.replace(settings(), workspace_label='production')

    # when
    with TestClient(create_app(env, store)) as c:
        body = c.get('/api/admin/settings', headers=AUTH).json()
    row = {i['key']: i for i in body['items']}['core.workspace-links']

    # then
    assert row['links']['max'] == 10 and row['links']['nameMax'] == 40
    assert sorted(row['links']['rules']) == ['count', 'name', 'pair', 'repeat', 'url', 'userinfo']
    assert row['links']['rules']['userinfo'] == 'the address must not hold a user name or password'
    assert body['instance']['name'] == 'production' and body['instance']['label'] == 'production'
    assert body['instance']['from'] == 'environment'


def test_the_deployment_name_falls_back_to_the_host_and_has_no_source_without_a_label(store: Store):
    # when
    with TestClient(create_app(settings(), store)) as c:
        body = c.get('/api/admin/settings', headers=AUTH).json()

    # then
    assert {k: v for k, v in body['instance'].items() if k != 'icon'} == {'name': 'skopax.example.com', 'label': '', 'from': ''}


def test_every_comma_list_tunable_declares_its_item_kind(store: Store):
    # when
    with TestClient(create_app(settings(), store)) as c:
        listed = items(c)

    # then
    assert {k: {f: v for f, v in listed[k]['items'].items() if f in ('kind', 'separator', 'keywords')} for k in LISTS} == LISTS
    assert [k for k, i in listed.items() if 'items' in i and k not in LISTS] == []
    assert 'items' not in listed['core.workspace-links']


def test_a_list_value_is_saved_and_read_back_as_the_same_text(store: Store):
    # given
    saved = {}

    # when
    with TestClient(create_app(settings(), store)) as c:
        for key, text in SAMPLES.items():
            put = c.put('/api/admin/settings', json={'key': key, 'value': text}, headers=AUTH)
            saved[key] = (put.status_code, put.json()['item']['value'])
        after = items(c)

    # then
    assert saved == {k: (200, v) for k, v in SAMPLES.items()}
    assert {k: after[k]['value'] for k in SAMPLES} == SAMPLES


def test_instance_links_still_store_the_same_name_equals_url_text(store: Store):
    # given
    text = f'staging={STAGING}, eu=https://eu.example.com/board'

    # when
    with TestClient(create_app(settings(), store)) as c:
        put = c.put('/api/admin/settings', json={'key': 'core.workspace-links', 'value': text}, headers=AUTH)
        links = c.get('/api/me').json()['instances']['links']

    # then
    assert (put.status_code, put.json()['item']['value']) == (200, text)
    assert [(link['name'], link['url']) for link in links] == [('staging', STAGING), ('eu', 'https://eu.example.com/board')]


def test_the_workspace_links_setting_is_read_from_its_new_env_name_and_the_old_one_still_works():
    # given
    old = {'SKOPAX_INSTANCE_LINKS': f'staging={STAGING}'}
    new = {'SKOPAX_WORKSPACE_LINKS': 'eu=https://eu.example.com'}

    # when
    from_new = load_settings(env=new)
    from_old = load_settings(env=old)
    both = load_settings(env={**old, **new})

    # then
    assert from_new.workspace_links == 'eu=https://eu.example.com'
    assert from_old.workspace_links == f'staging={STAGING}', 'the old name is still picked up while the new one is unset'
    assert both.workspace_links == 'eu=https://eu.example.com', 'the new name wins when both are set'


def stored(store: Store) -> dict:
    return {d['_id']: d for d in store.db['tunables'].find({})}


def test_a_value_stored_under_the_old_key_moves_to_the_new_key_once_at_boot(store: Store):
    # given
    TunableStore(store.db).write('core.instance-links', f'staging={STAGING}', 'admin@example.com', None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        first = c.get('/api/me').json()['instances']['links']
    with TestClient(create_app(settings(), store)) as c:
        second = c.get('/api/me').json()['instances']['links']
        history = c.get('/api/admin/history', headers=AUTH).json()
    held = stored(store)

    # then
    assert [link['name'] for link in first] == ['staging'] == [link['name'] for link in second]
    assert 'core.instance-links' not in held and held['core.workspace-links']['value'] == f'staging={STAGING}'
    assert held['core.workspace-links']['by'] == 'system'
    renamed = [h for h in history if h['key'] == 'core.workspace-links' and h.get('renamed')]
    assert len(renamed) == 1, 'one history line, even after a second boot'
    assert [a for a in store.db['activity'].find({}) if 'renamed setting' in str(a)], 'and one activity line'


def test_when_both_keys_are_stored_the_old_one_is_dropped_and_the_new_value_stays(store: Store):
    # given
    TunableStore(store.db).write('core.instance-links', f'staging={STAGING}', 'admin@example.com', None)
    TunableStore(store.db).write('core.workspace-links', 'eu=https://eu.example.com', 'admin@example.com', None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        links = c.get('/api/me').json()['instances']['links']
    held = stored(store)

    # then
    assert [link['name'] for link in links] == ['eu']
    assert 'core.instance-links' not in held and held['core.workspace-links']['value'] == 'eu=https://eu.example.com'


def test_reset_returns_to_the_env_value_after_the_old_stored_key_moved(store: Store):
    # given
    TunableStore(store.db).write('core.instance-links', f'staging={STAGING}', 'admin@example.com', None)
    env = dataclasses.replace(settings(), workspace_links='asia=http://asia.example.com')

    # when
    with TestClient(create_app(env, store)) as c:
        reset = c.delete('/api/admin/settings', params={'key': 'core.workspace-links'}, headers=AUTH)
        links = c.get('/api/me').json()['instances']['links']

    # then
    assert reset.status_code == 200
    assert [link['name'] for link in links] == ['asia'], 'the old stored list does not come back'


def test_reverting_a_history_row_of_the_old_key_reaches_the_new_key(store: Store):
    # given
    TunableStore(store.db).write_change('core.instance-links', 'eu=https://eu.example.com', f'staging={STAGING}', 'admin@example.com')

    # when
    with TestClient(create_app(settings(), store)) as c:
        old = [h for h in c.get('/api/admin/history', headers=AUTH).json() if h['key'] == 'core.instance-links'][0]
        revert = c.put('/api/admin/settings', json={'key': old['key'], 'value': old['old']}, headers=AUTH)
        links = c.get('/api/me').json()['instances']['links']

    # then
    assert revert.status_code == 200
    assert [link['name'] for link in links] == ['eu']
    assert stored(store)['core.workspace-links']['value'] == 'eu=https://eu.example.com'


def test_item_rules_live_on_the_server_and_are_exposed_with_the_entry(store: Store):
    # when
    with TestClient(create_app(settings(), store)) as c:
        listed = items(c)

    # then
    for key, spec in LISTS.items():
        shown = listed[key]['items']
        assert shown['pattern'] and shown['why'], key
    assert listed['jira.projects']['items']['why'] == 'a project key starts with a letter and holds letters, digits or underscores, at most 50'
    assert listed['self-review.dm']['items']['why'] == 'not an e-mail address'


def test_a_put_refuses_an_item_that_breaks_its_kind_and_names_the_entry(store: Store):
    # given
    cases = {
        'self-review.dm': ('alice@example.com, not-an-email', 'Send to, entry 2: not an e-mail address'),
        'jira.projects': ('OPS, 1BAD', 'Projects, entry 2: a project key starts with a letter and holds letters, digits or underscores, at most 50'),
        'slack.push-user': ('U012ABCDE, nope', 'Slack DM people, entry 2: not a Slack user id such as U012ABCDE'),
        'mcp.allowed-clients': ('ok-client, bad client', 'MCP allowed clients, entry 2: a client id has no spaces or commas'),
        'slack.ask-channels': ('all, C012ABCDEFG', 'Channels that take questions: all and main stand alone, they cannot be mixed with channel ids or each other'),
    }
    seen = {}

    # when
    with TestClient(create_app(settings(), store)) as c:
        for key, (value, _) in cases.items():
            put = c.put('/api/admin/settings', json={'key': key, 'value': value}, headers=AUTH)
            seen[key] = (put.status_code, put.json()['detail'])

    # then
    assert seen == {key: (422, said) for key, (_, said) in cases.items()}


def test_a_put_accepts_what_the_consumers_accept(store: Store):
    # given
    good = {
        'jira.projects': 'OPS, PLAT_CORE, a1',
        'core.cert-skip': 'shop.example.com., *.example.com',
        'slack.ask-channels': 'main',
        'self-review.dm': 'alice@example.com',
    }

    # when
    with TestClient(create_app(settings(), store)) as c:
        codes = {key: c.put('/api/admin/settings', json={'key': key, 'value': value}, headers=AUTH).status_code for key, value in good.items()}
        all_codes = c.put('/api/admin/settings', json={'key': 'slack.ask-channels', 'value': 'all'}, headers=AUTH).status_code

    # then
    assert codes == {key: 200 for key in good} and all_codes == 200


def test_a_stored_value_that_fails_its_rule_still_loads_and_other_rows_still_save(store: Store):
    # given
    TunableStore(store.db).write('self-review.dm', 'alice@example.com, nope', 'admin@example.com', None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        listed = items(c)
        other = c.put('/api/admin/settings', json={'key': 'jira.projects', 'value': 'OPS'}, headers=AUTH)

    # then
    assert listed['self-review.dm']['value'] == 'alice@example.com, nope', 'it is shown, so the page can mark it red'
    assert other.status_code == 200


def test_a_settings_file_that_still_uses_the_old_key_is_read_and_the_new_key_wins(tmp_path):
    # given
    old = tmp_path / 'old.yaml'
    old.write_text(f'settings:\n  core.instance-links: staging={STAGING}\n', encoding='utf-8')
    both = tmp_path / 'both.yaml'
    both.write_text(f'settings:\n  core.instance-links: staging={STAGING}\n  core.workspace-links: eu=https://eu.example.com\n', encoding='utf-8')

    # when
    from skopax.registry import Registry
    from skopax.settingsources import FILE_ENV, boot
    from_old = boot({FILE_ENV: str(old)}, Registry.of())
    from_both = boot({FILE_ENV: str(both)}, Registry.of())

    # then
    assert from_old.settings.workspace_links == f'staging={STAGING}'
    assert from_both.settings.workspace_links == 'eu=https://eu.example.com'


def test_a_file_value_under_the_old_key_shows_its_source_on_the_new_row(store: Store, tmp_path):
    # given
    from skopax.kube import Kube
    from skopax.registry import Registry
    from skopax.settingsources import FILE_ENV, boot
    old = tmp_path / 'old.yaml'
    old.write_text(f'settings:\n  core.instance-links: staging={STAGING}\n', encoding='utf-8')
    b = boot({FILE_ENV: str(old)}, Registry.of())

    # when
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        row = {e['key']: e for e in c.app.state.runtime.live.entries()}['core.workspace-links']

    # then
    assert (row['value'], row['source']) == (f'staging={STAGING}', 'config')
    assert row['from'] != 'environment', 'the row names the file, not the generic environment'

def test_history_rows_of_the_old_key_name_the_key_that_labels_them(store: Store):
    # given
    TunableStore(store.db).write_change('core.instance-links', None, f'staging={STAGING}', 'admin@example.com')
    TunableStore(store.db).write_change('core.workspace-links', None, 'eu=https://eu.example.com', 'admin@example.com')

    # when
    with TestClient(create_app(settings(), store)) as c:
        history = c.get('/api/admin/history', headers=AUTH).json()

    # then
    by_key = {h['key']: h for h in history}
    assert by_key['core.instance-links']['current'] == 'core.workspace-links'
    assert 'current' not in by_key['core.workspace-links']


def test_a_copy_that_crashed_between_upsert_and_delete_still_gets_its_history_line_at_the_next_boot(store: Store):
    # given
    held = TunableStore(store.db)
    held.write('core.instance-links', f'staging={STAGING}', 'admin@example.com', None, record=False)
    store.db['tunables'].replace_one({'_id': 'core.workspace-links'}, {'_id': 'core.workspace-links', 'value': f'staging={STAGING}', 'by': 'system', 'at': '2026-11-01T00:00:00Z'}, upsert=True)

    # when
    with TestClient(create_app(settings(), store)) as c:
        history = c.get('/api/admin/history', headers=AUTH).json()
    with TestClient(create_app(settings(), store)) as c:
        again = c.get('/api/admin/history', headers=AUTH).json()
    kept = stored(store)

    # then
    assert 'core.instance-links' not in kept and kept['core.workspace-links']['value'] == f'staging={STAGING}'
    assert len([h for h in history if h.get('renamed') == 'core.instance-links']) == 1
    assert len([h for h in again if h.get('renamed') == 'core.instance-links']) == 1, 'and not a second one'
    assert len([a for a in store.db['activity'].find({}) if 'renamed setting' in str(a)]) == 1


def test_web_sites_use_the_runtime_host_rule_and_accept_a_dash_host(store: Store):
    # when
    with TestClient(create_app(settings(), store)) as c:
        shown = items(c)['investigate.web-domains']['items']
        seen = {v: c.put('/api/admin/settings', json={'key': 'investigate.web-domains', 'value': v}, headers=AUTH).status_code
                for v in ('-blog.google, example.com', 'example.com blog.example.org', '-', '-bad_host', 'not_a_host!', '--blog.google', 'a..com',
                          '*.example.com', 'localhost', '-localhost', 'Example.COM')}
        saved = items(c)['investigate.web-domains']['value']
        cert = c.put('/api/admin/settings', json={'key': 'core.cert-skip', 'value': 'localhost, *.example.com'}, headers=AUTH).status_code
        listed = items(c)

    # then
    assert shown['kind'] == 'web-host' and shown['minus'] is True and shown['pattern'] == DOMAIN.pattern
    assert 'covers only subdomains' not in shown['why'] and 'support.google.com' in shown['why']
    assert seen == {'-blog.google, example.com': 200, 'example.com blog.example.org': 200, '-': 422, '-bad_host': 422, 'not_a_host!': 422, '--blog.google': 422, 'a..com': 422,
                    '*.example.com': 422, 'localhost': 422, '-localhost': 422, 'Example.COM': 200}
    assert saved == 'Example.COM'
    assert cert == 200 and listed['core.cert-skip']['items']['kind'] == 'host' and 'minus' not in listed['core.cert-skip']['items']


def test_web_sites_keywords_stand_alone_like_the_channel_keywords(store: Store):
    # given
    ok = ('any', 'OFF', 'none', 'false', 'no')
    mixed = ('any, example.com', 'example.com, off', 'off, none', 'No, any')

    # when
    with TestClient(create_app(settings(), store)) as c:
        accepted = [c.put('/api/admin/settings', json={'key': 'investigate.web-domains', 'value': v}, headers=AUTH).status_code for v in ok]
        refused = [c.put('/api/admin/settings', json={'key': 'investigate.web-domains', 'value': v}, headers=AUTH) for v in mixed]

    # then
    assert accepted == [200] * 5
    assert [r.status_code for r in refused] == [422] * 4
    assert refused[0].json()['detail'] == 'Web sites: any, off, none, false and no stand alone, they cannot be mixed with hosts or each other'


def test_two_copies_racing_on_the_rename_write_one_history_line(store: Store):
    # given
    first, second = TunableStore(store.db), TunableStore(store.db)
    store.db['tunables'].insert_one({'_id': 'core.instance-links', 'value': f'staging={STAGING}', 'by': 'admin@example.com', 'at': '2026-11-01T00:00:00Z'})
    store.db['tunables'].insert_one({'_id': 'core.workspace-links', 'value': f'staging={STAGING}', 'by': 'system', 'at': '2026-11-01T00:00:00Z'})

    class Blind:
        def __init__(self, real):
            self.real = real

        def find_one(self, *a, **k):
            return None

        def __getattr__(self, name):
            return getattr(self.real, name)

    second.changes = Blind(second.changes)

    # when
    results = [first.migrate_renamed()]
    store.db['tunables'].insert_one({'_id': 'core.instance-links', 'value': f'staging={STAGING}', 'by': 'admin@example.com', 'at': '2026-11-01T00:00:00Z'})
    results.append(second.migrate_renamed())

    # then
    assert results == [True, False]
    assert len([c for c in store.db['tunable_changes'].find({}) if c.get('renamed') == 'core.instance-links']) == 1
    assert store.db['tunables'].find_one({'_id': 'core.instance-links'}) is None


def test_the_rename_line_is_written_before_the_old_key_goes(store: Store):
    # given
    held = TunableStore(store.db)
    store.db['tunables'].insert_one({'_id': 'core.instance-links', 'value': f'staging={STAGING}', 'by': 'admin@example.com', 'at': '2026-11-01T00:00:00Z'})
    seen = []
    real = held.write_change
    held.write_change = lambda *a, **k: seen.append(store.db['tunables'].find_one({'_id': 'core.instance-links'}) is not None) or real(*a, **k)

    # when
    held.migrate_renamed()

    # then
    assert seen == [True]
    assert store.db['tunables'].find_one({'_id': 'core.instance-links'}) is None


def test_the_workspace_name_from_a_settings_file_names_the_file_under_the_new_or_the_old_key(store: Store, tmp_path):
    # given
    from skopax.kube import Kube
    from skopax.registry import Registry
    from skopax.settingsources import FILE_ENV, boot
    new = tmp_path / 'new.yaml'
    new.write_text('settings:\n  core.workspace-label: production\n', encoding='utf-8')
    old = tmp_path / 'old.yaml'
    old.write_text('settings:\n  core.env-label: staging\n', encoding='utf-8')

    # when
    seen = []
    for path in (new, old):
        b = boot({FILE_ENV: str(path)}, Registry.of())
        named = dataclasses.replace(settings(), workspace_label=b.settings.workspace_label)
        with TestClient(create_app(named, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
            seen.append(c.get('/api/admin/settings', headers=AUTH).json()['instance'])

    # then
    assert [(i['name'], i['label']) for i in seen] == [('production', 'production'), ('staging', 'staging')]
    assert all(i['from'] and i['from'] != 'environment' for i in seen), seen
