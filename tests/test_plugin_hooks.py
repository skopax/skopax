import dataclasses
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient

from skopax import history, locales, messages, redact, statenotes, tags, tunables
from skopax.app import create_app
from skopax.contract import Hooks, Locale, Panel, PassResult, Plugin, TagRule
from skopax.engine import anomaly
from skopax.instances import Instances, PluginState
from skopax.investigator.tools import Effects, Sources, Toolbox, allowed_link_hosts
from skopax.jiralinks import ticket_pattern, ticket_projects
from skopax.models import email_allowed
from skopax.registry import Registry, problem
from skopax.runtime import PAGE_PACK, Runtime
from skopax.sealer import Sealer, master_key
from tests.conftest import settings
from tests.plugin_kit import add_instance, registry_with, rule_patterns
from tests.sso_kit import REDIRECT, redeem, sign_on
from tests.plugins import demo_plugin, enterprise_plugin
from tests.plugins.base import minimal
from tests.test_engine import NOW

DEMO = demo_plugin.PLUGIN
HERE = Path(__file__).parent / 'plugins'
GOOD = {'url': 'https://demo.example.com', 'token': 'pw'}
CORE_PACKS = [f'/static/i18n-{lang}.js' for lang in locales.langs()]


def boot(store, *plugins, add: bool = True) -> Runtime:
    registry = registry_with(*plugins)
    if add:
        add_instance(store, 'demo', 'main', sealer_key='k', **GOOD)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))
    runtime.refresh()
    return runtime


def served(store, *plugins, add: bool = True, **over) -> TestClient:
    if add:
        add_instance(store, 'demo', 'main', sealer_key=master_key(store.db, settings()), **GOOD)
    return TestClient(create_app(dataclasses.replace(settings(), **over), store, registry=registry_with(*plugins)))


def hooked(id: str, **hooks) -> Plugin:
    return dataclasses.replace(minimal(id, id.title()), hooks=Hooks(**hooks))


def test_redaction_rules_from_plugins_apply_wherever_redact_runs(store):
    # given
    before = redact.redact('see DEMO-1234')

    # when
    with served(store, DEMO):
        slack = statenotes.resolved_text('alice', 'fixed DEMO-1234')
        note = history.cut('closed DEMO-1234 today', 80)

    # then
    assert before == 'see DEMO-1234'
    assert 'DEMO-1234' not in slack and redact.REDACTED in slack
    assert note == f'closed {redact.REDACTED} today'


def test_redaction_rules_follow_the_serving_state_and_skip_bad_patterns(store, caplog, monkeypatch):
    # given
    monkeypatch.setattr('skopax.registry._check', lambda plugin, directory: None)
    mixed = dataclasses.replace(minimal('rx', 'Rx'), redactions=(DEMO.redactions[0], dataclasses.replace(DEMO.redactions[0], id='bad', pattern='(')))
    runtime = boot(store, mixed, add=False)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        found = rule_patterns(runtime)
        again = rule_patterns(runtime)

    # then
    assert [p.pattern for p in found] == [r'DEMO-\d{4}']
    assert found == again
    assert sum('bad' in r.getMessage() for r in caplog.records) == 1
    runtime.state.set('rx', False, 'alice')
    assert rule_patterns(runtime) == ()


def test_org_validators_extend_link_hosts_email_domains_and_ticket_projects(store):
    # given
    runtime = boot(store, DEMO)
    base = settings()

    # when
    org = runtime.org()
    hosts = allowed_link_hosts(base, org['link_hosts'])
    domains = base.email_domains + org['email_domains']
    projects = ticket_projects(base, org['ticket_projects'])

    # then
    assert org == {'link_hosts': ('example.com',), 'email_domains': ('example.com',), 'ticket_projects': ('OPS',), 'email_closed': False}
    assert 'example.com' in hosts
    assert email_allowed('alice@example.com', domains)
    assert not email_allowed('alice@example.org', domains)
    assert projects == ticket_projects(base)
    assert ticket_projects(dataclasses.replace(base, jira_project='', jira_projects=(), jira_tech_project=''), ('PLUG', 'OPS')) == ('PLUG', 'OPS')
    assert ticket_pattern(('PLUG',)).fullmatch('PLUG-12')


def test_a_raising_or_malformed_org_validator_is_skipped_with_a_warning(store, caplog):
    # given
    def boom():
        raise RuntimeError('nope')

    shaky = hooked('shaky', org_validators=boom)
    odd = hooked('odd-one', org_validators=lambda: {'link_hosts': 'example.org', 'email_domains': [7, 'example.net'], 'ticket_projects': None, 'unknown': ['x']})
    registry = Registry.of(shaky, odd, DEMO)
    add_instance(store, 'demo', 'main', sealer_key='k', **GOOD)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        runtime.refresh()
        org = runtime.org()

    # then
    assert org == {'link_hosts': ('example.com',), 'email_domains': ('example.com',), 'ticket_projects': ('OPS',), 'email_closed': True}
    assert any('shaky' in r.getMessage() for r in caplog.records) and any('odd-one' in r.getMessage() for r in caplog.records)
    assert 'nope' not in caplog.text


def test_instance_ui_hosts_are_allowed_link_hosts(store):
    # given
    add_instance(store, 'demo', 'east', sealer_key='k', url='https://east.example.org:9090/graph', token='pw')
    add_instance(store, 'demo', 'off', sealer_key='k', url='https://off.example.org', token='pw', enabled=False)
    runtime = boot(store, DEMO)

    # when
    hosts = runtime.ui_hosts()

    # then
    assert hosts == ('demo.example.com', 'east.example.org')
    assert {'demo.example.com', 'east.example.org'} <= set(allowed_link_hosts(settings(), runtime.org()['link_hosts'] + hosts))
    assert 'off.example.org' not in allowed_link_hosts(settings(), hosts)


def test_the_toolbox_asks_the_live_link_hosts_and_org_on_every_run(store):
    # given
    live = {'hosts': ('one.example.net',), 'org': {'email_domains': (), 'ticket_projects': ()}}
    sources = Sources(gitlab=None, jira=None, link_hosts=lambda: live['hosts'], org=lambda: live['org'])
    effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {})

    def toolbox() -> Toolbox:
        return Toolbox(sources, store, effects, [], 'live', NOW, 'https://skopax.example.com', settings=settings())

    # when
    first = toolbox().link_hosts
    live['hosts'] = ('two.example.net',)
    second = toolbox().link_hosts

    # then
    assert first == ('one.example.net',) and second == ('two.example.net',)


def test_the_app_wires_plugin_org_values_into_the_investigator(store):
    # given
    with served(store, DEMO) as client:
        sources = client.app.state.investigator.sources

        # when
        hosts = sources.link_hosts()
        org = sources.org()

    # then
    assert {'example.com', 'demo.example.com'} <= set(hosts)
    assert org['ticket_projects'] == ('OPS',)


def test_oidc_token_request_hook_adds_headers_and_params(store, monkeypatch):
    # given
    seen = []
    monkeypatch.setattr('skopax.sso.post_form', lambda url, data, headers: seen.append((url, dict(data), dict(headers))) or {})
    second = hooked('second-one', oidc_token_request=lambda request: {'headers': {'X-Demo': '2', 'X-Other': 'y'}, 'params': {'audience': request['params']['client_id']}})
    runtime = boot(store, DEMO, second)
    s = dataclasses.replace(settings(), sso_token_url='https://id.example.com/token', sso_token_headers='X-Client: web', sso_client_id='client-1')
    signon = sign_on(s, runtime.token_request, secret='shh')

    # when
    pending, _ = redeem(signon, 'c')

    # then
    assert runtime.token_request({'params': {'client_id': 'z'}}) == {'headers': {'X-Demo': '2', 'X-Other': 'y'}, 'params': {'audience': 'z'}}
    assert seen == [('https://id.example.com/token', {'grant_type': 'authorization_code', 'client_id': 'client-1', 'client_secret': 'shh', 'code': 'c', 'audience': 'client-1',
                                                      'redirect_uri': REDIRECT, 'code_verifier': pending['verifier']},
                     {'X-Client': 'web', 'X-Demo': '2', 'X-Other': 'y'})]


def test_the_token_request_hook_never_sees_the_secret_and_a_failure_stops_the_login(store, monkeypatch):
    # given
    seen, asked = [], []
    monkeypatch.setattr('skopax.sso.post_form', lambda url, data, headers: seen.append((dict(data), dict(headers))) or {})

    def hook(request):
        asked.append(dict(request))
        raise RuntimeError('boom')

    runtime = boot(store, hooked('bad-hook', oidc_token_request=hook))
    s = dataclasses.replace(settings(), sso_token_url='https://id.example.com/token', sso_token_headers='X-Client: web', sso_client_id='c')
    signon = sign_on(s, runtime.token_request, secret='shh')

    # when
    _, failure = redeem(signon, 'code-1')

    # then
    assert asked == [{'url': 'https://id.example.com/token', 'headers': {'X-Client': 'web'}, 'params': {'grant_type': 'authorization_code', 'redirect_uri': REDIRECT, 'client_id': 'c'}}]
    assert failure.reason == 'hook'
    assert seen == []


def test_the_app_builds_its_sso_with_the_plugin_hook(store, monkeypatch):
    # given
    seen = []
    monkeypatch.setattr('skopax.sso.post_form', lambda url, data, headers: seen.append(dict(headers)) or {})
    endpoints = {'sso_authorize_url': 'https://id.example.com/authorize', 'sso_token_url': 'https://id.example.com/token', 'sso_jwks_url': 'https://id.example.com/jwks'}

    # when
    with served(store, DEMO, sso_issuer='https://id.example.com', sso_client_secret='shh', **endpoints) as client:
        redeem(client.app.state.signon)

    # then
    assert seen == [{'X-Demo': '1'}]


def test_role_resolvers_are_listed_in_load_order(store):
    # given
    def zeta(user, channel):
        return 'zeta'

    def alpha(user, channel):
        return 'alpha'

    late = dataclasses.replace(hooked('aaa-late', role_resolver=zeta), after=('zzz-early',))
    early = hooked('zzz-early', role_resolver=alpha)
    silent = minimal('mid', 'Mid')

    # when
    runtime = boot(store, late, silent, early, add=False)

    # then
    assert runtime.role_resolvers() == [alpha, zeta]
    assert [r({}, 'page') for r in runtime.role_resolvers()] == ['alpha', 'zeta']


def test_plugin_tag_rules_join_the_defaults(store, monkeypatch):
    # given
    monkeypatch.setattr(tags, 'RULES', (('marketing', ('type:gtm',)), ('seo', ('gone',))))
    plugin = dataclasses.replace(minimal('tagger', 'Tagger'), tag_rules=(TagRule('marketing', ('type:gtm', 'type:newsletter')), TagRule('seo', ('type:crux',)), TagRule('not-a-tag', ('type:x',))))
    runtime = boot(store, plugin, add=False)
    marketing = next(t for t in tunables.registry() if t.key == 'tags.marketing.rules')

    # when
    merged = tags.merged(tags.RULES, runtime.tag_rules())
    with served(store, plugin, add=False, tag_rules=()) as client:
        shown = tunables.default_of(marketing)
        live = tags.rules()
        card = anomaly('prom', 'newsletter', 'shop', 'checkout-service', '', 'warn', 10, 1, 1, '', 'Mail', 'mail down')
        client.app.state.server_apply([PassResult('prom', True, 'ok', [card], {}, ('prom',))], NOW, None)
        stored = store.anomaly(card['key'])

    # then
    assert runtime.tag_rules() == {'marketing': ('type:gtm', 'type:newsletter'), 'seo': ('type:crux',), 'not-a-tag': ('type:x',)}
    assert merged == (('marketing', ('type:gtm', 'type:newsletter')), ('seo', ('gone', 'type:crux')), ('not-a-tag', ('type:x',)))
    assert tags.merged((), {}) == ()
    assert shown == 'type:gtm'
    assert dict(live) == {'marketing': ('type:gtm', 'type:newsletter'), 'seo': ('gone', 'type:crux')}
    assert [t['id'] for t in stored['tags']] == ['marketing']


@pytest.mark.usefixtures('no_core_packs')
def test_locale_page_pack_is_served_and_messages_registered(store):
    # given
    other = dataclasses.replace(minimal('german', 'German'), locales=(Locale('de', {'Other': 'Andere'}, {'slack.cancel': 'Abbrechen', 'german.bye': 'Tschuess'}), Locale('fr', {'Demo': 'Demo!'}, {})))

    # when
    with served(store, DEMO, other) as client:
        res = client.get('/plugins/demo/i18n-de.js')
        fr = client.get('/plugins/german/i18n-fr.js')
        catalog = messages.catalog('de')

    # then
    assert res.status_code == 200 and res.headers['cache-control'] == 'no-cache' and res.headers['content-type'].startswith('text/javascript')
    assert res.text == ('(function (all, lang, pack) { var held = all[lang] = all[lang] || {}; Object.keys(pack).forEach(function (k) { var v = pack[k], was = held[k]; '
                        'held[k] = k === "@rules" || k === "@tails" ? (function (base) { var n = base.core || 0, out = base.slice(0, base.length - n).concat(v, base.slice(base.length - n)); '
                        'out.core = n; return out; })(Array.isArray(was) ? was : []) : k === "@months" || k === "@days" || k === "@th" ? Object.assign({}, was, v) : v; }); })'
                        '(window.SKOPAX_I18N = window.SKOPAX_I18N || {}, "de", {"Demo": "Demo"});')
    assert fr.status_code == 200
    assert catalog == dict(DEMO.locales[0].messages) | {'slack.cancel': 'Abbrechen'}
    assert messages.catalog('xx') == {}
    assert 'german.bye' not in messages.MESSAGES and messages.languages() == ('en',) + tuple(sorted({'de', *(pack.lang for pack in locales.packs())}))


PAGE_RULES = (('^Seen before: (\\d+) times$', 'Schon $1 mal gesehen'),)
PAGE_TAILS = ((' \u00b7 auto-resolved', ' \u00b7 automatisch'),)
LOOKBEHIND = '(?<=a)b'


def page_locale(**over) -> Locale:
    return Locale('de', **({'page': {'Resolved': 'Erledigt'}, 'rules': PAGE_RULES, 'months': {'Mar': 'Mrz'}, 'tails': PAGE_TAILS, 'headers': {'Requests': 'Anfragen'}, 'days': {'Mon': 'Mo'}} | over))


def pages(*locales: Locale, **over) -> Plugin:
    return dataclasses.replace(minimal('pages', 'Pages'), locales=locales, **over)


@pytest.mark.parametrize('over, named, part, kept', [
    ({'rules': (('(', 'x'),)}, 'rule pattern ( is not valid', 'rules[0]', {'rules': ()}),
    ({'rules': tuple((f'^a{i}$', 'b') for i in range(201))}, 'has 201 rules; at most 200', 'rules', {'rules': ()}),
    ({'months': {'Foo': 'Bar', 'Mar': 'Mrz'}}, 'Foo is not one of Jan, Feb', "months['Foo']", {'months': {'Mar': 'Mrz'}}),
    ({'rules': ((LOOKBEHIND, 'x'),)}, f'rule pattern {LOOKBEHIND} uses a lookbehind', 'rules[0]', {'rules': ()}),
    ({'rules': (('^', 'X '),)}, 'rule pattern ^ matches the empty text', 'rules[0]', {'rules': ()}),
    ({'rules': (('(x)?', 'y'),)}, 'rule pattern (x)? matches the empty text', 'rules[0]', {'rules': ()}),
    ({'rules': (('^a$', 'costs $x'),)}, 'replacement costs $x is not valid', 'rules[0]', {'rules': ()}),
    ({'rules': (('^a$', 'all $&'),)}, 'replacement all $& is not valid', 'rules[0]', {'rules': ()}),
    ({'rules': (('a' * 301, 'x'),)}, 'at most 300 characters', 'rules[0]', {'rules': ()}),
    ({'rules': (('^a$', 'x' * 501),)}, 'at most 500 characters', 'rules[0]', {'rules': ()}),
    ({'rules': (('^a$',), ('^b$', 'c'))}, 'rules has the wrong shape', 'rules[0]', {'rules': (('^b$', 'c'),)}),
    ({'days': {'Funday': 'x', 'Mon': 'Mo'}}, 'Funday is not one of Mon, Tue', "days['Funday']", {'days': {'Mon': 'Mo'}}),
    ({'tails': tuple((f' {i}', 'x') for i in range(51))}, 'has 51 tails; at most 50', 'tails', {'tails': ()}),
    ({'tails': (('', 'x'), (' y', 'z'))}, 'tails has the wrong shape', 'tails[0]', {'tails': ((' y', 'z'),)}),
    ({'headers': {f'H{i}': 'x' for i in range(201)}}, 'has 201 headers; at most 200', 'headers', {'headers': {}}),
    ({'headers': {'Requests': 5, 'Errors': 'Fehler'}}, 'headers has the wrong shape', "headers['Requests']", {'headers': {'Errors': 'Fehler'}}),
    ({'headers': [('Requests', 'Anfragen')]}, 'headers has the wrong shape', 'headers', {'headers': {}}),
    ({'tails': {' a': 'b'}}, 'tails has the wrong shape', 'tails', {'tails': ()}),
    ({'page': {'@rules': 'x'}}, 'page key @rules starts with @', "page['@rules']", {'page': {}}),
])
def test_locale_rules_months_tails_and_headers_are_validated(over, named, part, kept, caplog):
    # given
    bad = pages(page_locale(**over))
    caplog.set_level(logging.WARNING, logger='skopax.registry')

    # when
    good = Registry.of(pages(page_locale())).get('pages')
    found = Registry.of(DEMO, bad).get('pages')
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.registry']

    # then
    assert good.state == 'ok' and good.reason is None
    assert (found.state, found.reason) == ('ok', None), 'a pack text that breaks a rule never breaks the plug-in'
    assert found.plugin.locales == (page_locale(**kept),), 'only the named text or part is left out, and the rest of the pack stays'
    assert problem('locales', found.plugin.locales[0]) is None
    assert len(warned) == 1 and warned[0].startswith(f'Locale pack de of pages: {part} is left out (locale de') and named in warned[0], warned


def test_served_pack_carries_rules_months_tails_and_headers(store):
    # given
    later = Locale('de', {'Resolved': 'Geloest', 'Open': 'Offen'}, rules=(('^Stopped (.+)$', 'Gestoppt $1'),), months={'Dec': 'Dez'}, tails=((' \u00b7 by investigator', ' \u00b7 Ermittler'),),
                   headers={'Errors': 'Fehler'})
    first = pages(page_locale(), later)
    rules_only = dataclasses.replace(minimal('more-pages', 'More pages'), after=('pages',), locales=(Locale('de', rules=(('^Gone$', 'Weg'),)),))
    pattern = re.compile(r'<script src="(/plugins/[^"]+)"></script>')

    # when
    with served(store, first, rules_only, add=False) as client:
        own = client.get('/plugins/pages/i18n-de.js')
        other = client.get('/plugins/more-pages/i18n-de.js')
        listed = pattern.findall(client.get('/').text)

    # then
    assert own.status_code == 200 and other.status_code == 200
    assert own.text == PAGE_PACK % ('"de"', json.dumps({'Resolved': 'Geloest', 'Open': 'Offen', '@rules': [list(PAGE_RULES[0]), ['^Stopped (.+)$', 'Gestoppt $1']], '@months': {'Mar': 'Mrz', 'Dec': 'Dez'},
                                                         '@days': {'Mon': 'Mo'}, '@tails': [list(PAGE_TAILS[0]), [' \u00b7 by investigator', ' \u00b7 Ermittler']], '@th': {'Requests': 'Anfragen', 'Errors': 'Fehler'}}))
    assert other.text == PAGE_PACK % ('"de"', '{"@rules": [["^Gone$", "Weg"]]}')
    assert listed == ['/plugins/pages/i18n-de.js', '/plugins/more-pages/i18n-de.js'], 'packs load, and so merge in the browser, in load order'


def test_card_page_loads_the_locale_packs_of_serving_plugins(store):
    # given
    pattern = re.compile(r'<script src="([^"?]+)(?:\?v=\w+)?"></script>')

    # when
    with served(store, DEMO) as client:
        on = pattern.findall(client.get('/c/1').text)
        client.app.state.plugin_state.set('demo', False, 'alice')
        client.app.state.runtime.refresh()
        off = pattern.findall(client.get('/c/1').text)

    # then
    assert on == ['/static/actors.js', '/static/i18n.js', *CORE_PACKS, '/plugins/demo/i18n-de.js', '/static/suggest.js', '/static/card.js'], 'packs only, never the panel scripts'
    assert off == ['/static/actors.js', '/static/i18n.js', *CORE_PACKS, '/static/suggest.js', '/static/card.js']


def test_probe_targets_come_from_runnable_instances(store):
    # given
    add_instance(store, 'demo', 'east', sealer_key='k', url='https://east.example.org', token='pw')
    add_instance(store, 'demo', 'off', sealer_key='k', url='https://off.example.org', token='pw', enabled=False)
    runtime = boot(store, DEMO)

    # when
    found = runtime.probe_targets()
    runtime.state.set('demo', False, 'alice')
    runtime.refresh()

    # then
    assert sorted(found, key=lambda t: t['instance']) == [{'name': 'demo', 'url': 'https://example.com/health', 'plugin': 'demo', 'instance': 'east'},
                                                         {'name': 'demo', 'url': 'https://example.com/health', 'plugin': 'demo', 'instance': 'main'}]
    assert runtime.probe_targets() == []


def test_a_raising_probe_target_hook_is_skipped(store, caplog):
    # given
    def boom(instance):
        raise RuntimeError('no')

    runtime = boot(store, dataclasses.replace(DEMO, probe_targets=boom), add=True)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        found = runtime.probe_targets()

    # then
    assert found == [] and any('probe' in r.getMessage() for r in caplog.records)


def test_plugin_assets_serve_declared_files_only(store, tmp_path):
    # given
    locked = dataclasses.replace(enterprise_plugin.PLUGIN, panels=(Panel('locked', 'demo-panel.js'),))
    broken = dataclasses.replace(minimal('broken-one', 'Broken one'), version='x')

    # when
    with served(store, DEMO, locked, broken) as client:
        icon = client.get('/plugins/demo/icon.svg')
        panel = client.get('/plugins/demo/demo-panel.js')
        locked_icon = client.get('/plugins/enterprise-demo/icon.svg')
        locked_panel = client.get('/plugins/enterprise-demo/demo-panel.js')
        statuses = {name: client.get(f'/plugins/demo/{name}').status_code for name in ('..%2Fapp.py', 'x.py', '..', 'base.py', 'demo-plugin.py', 'i18n-xx.js', 'icon.svg%00', '%2e%2e%2fdemo_plugin.py')}
        no_such = client.get('/plugins/nope/icon.svg')
        broken_icon = client.get('/plugins/broken-one/icon.svg')

    # then
    assert icon.status_code == 200 and icon.headers['content-type'].startswith('image/svg+xml') and icon.headers['cache-control'] == 'no-cache'
    assert icon.content == (HERE / 'demo-icon.svg').read_bytes()
    assert panel.status_code == 200 and panel.headers['content-type'].startswith('text/javascript') and panel.content == (HERE / 'demo-panel.js').read_bytes()
    assert locked_icon.status_code == 200 and locked_panel.status_code == 404
    assert set(statuses.values()) == {404}
    assert no_such.status_code == 404 and broken_icon.status_code == 404


def test_panel_files_serve_only_while_the_plugin_serves(store):
    # given
    with served(store, DEMO, add=False) as client:
        waiting = client.get('/plugins/demo/demo-panel.js').status_code
        icon = client.get('/plugins/demo/icon.svg').status_code
        add_instance(store, 'demo', 'main', sealer_key=master_key(store.db, settings()), **GOOD)
        client.app.state.runtime.refresh()

        # when
        running = client.get('/plugins/demo/demo-panel.js').status_code
        client.app.state.plugin_state.set('demo', False, 'alice')
        client.app.state.runtime.refresh()
        off = client.get('/plugins/demo/demo-panel.js').status_code

    # then
    assert (waiting, icon, running, off) == (404, 200, 200, 404)


def test_panel_paths_must_stay_inside_the_plugin_folder(tmp_path):
    # given
    home = tmp_path / 'plug'
    home.mkdir()
    (home / 'icon.svg').write_text((HERE / 'demo-icon.svg').read_text(encoding='utf-8'), encoding='utf-8')
    (home / 'ok.js').write_text('window.ok = 1;', encoding='utf-8')
    outside = tmp_path / 'secret.js'
    outside.write_text('window.secret = 1;', encoding='utf-8')
    (home / 'link.js').symlink_to(outside)
    plugin = dataclasses.replace(minimal('guarded', 'Guarded'), icon='icon.svg', panels=(Panel('pa', 'ok.js'), Panel('pb', 'link.js', 'missing.html'), Panel('pc', '../secret.js')))
    db = mongomock.MongoClient()['x']
    registry = Registry.of(plugin, directory=home)
    runtime = Runtime(registry, PluginState(db), Instances(db, registry, lambda: None))
    runtime.refresh()

    # when
    got = {name: runtime.asset('guarded', name) for name in ('ok.js', 'link.js', 'missing.html', '../secret.js', 'icon.svg')}

    # then
    assert got['ok.js'] == (b'window.ok = 1;', 'text/javascript; charset=utf-8')
    assert got['icon.svg'][1] == 'image/svg+xml'
    assert got['link.js'] is None and got['missing.html'] is None and got['../secret.js'] is None


def test_a_panel_fragment_is_served_as_html(store, tmp_path):
    # given
    home = tmp_path / 'plug'
    home.mkdir()
    (home / 'icon.svg').write_text((HERE / 'demo-icon.svg').read_text(encoding='utf-8'), encoding='utf-8')
    (home / 'tile.html').write_text('<div>tile</div>', encoding='utf-8')
    (home / 'tile.js').write_text('window.t = 1;', encoding='utf-8')
    plugin = dataclasses.replace(minimal('tiled', 'Tiled'), icon='icon.svg', panels=(Panel('pt', 'tile.js', 'tile.html'),))
    db = mongomock.MongoClient()['x']
    registry = Registry.of(plugin, directory=home)
    runtime = Runtime(registry, PluginState(db), Instances(db, registry, lambda: None))
    runtime.refresh()

    # when
    html = runtime.asset('tiled', 'tile.html')

    # then
    assert html == (b'<div>tile</div>', 'text/html; charset=utf-8')


def test_page_loads_scripts_of_serving_plugins_only(store):
    # given
    pattern = re.compile(r'<script src="(/plugins/[^"]+)"></script>')

    # when
    with served(store, DEMO, add=False) as client:
        waiting = pattern.findall(client.get('/').text)
        add_instance(store, 'demo', 'main', sealer_key=master_key(store.db, settings()), **GOOD)
        client.app.state.runtime.refresh()
        page = client.get('/').text
        running = pattern.findall(page)
        client.app.state.plugin_state.set('demo', False, 'alice')
        client.app.state.runtime.refresh()
        off = pattern.findall(client.get('/').text)

    # then
    assert waiting == [] and off == []
    assert running == ['/plugins/demo/i18n-de.js', '/plugins/demo/demo-panel.js']
    assert page.index('/static/i18n.js') < page.index('/plugins/demo/i18n-de.js') < page.index('/static/core.js')


def test_plugin_asset_route_is_open_and_listed():
    # given
    from skopax.app import is_open
    from tests.test_role_classes import READS

    # then
    assert is_open('/plugins/demo/icon.svg') and READS['/plugins/{plugin_id}/{file}'] == 'open'


def test_a_plugin_without_instance_fields_serves_its_panels_at_once(store):
    # given
    plain = dataclasses.replace(minimal('plain', 'Plain'), panels=(Panel('pp', 'demo-panel.js'),))

    # when
    runtime = boot(store, plain, add=False)

    # then
    assert [(i, p.tab) for i, p in runtime.panels()] == [('plain', 'pp')]
    assert runtime.locales() == []
