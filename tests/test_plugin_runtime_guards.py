import dataclasses
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient

from skopax import redact, tags, tunables
from skopax.actions import apply as apply_action
from skopax.app import create_app, plugin_script_tags
from skopax.auth import AuthError
from skopax.bounded import HOOK_TIMEOUT_S
from skopax.chatui.text import configure, linkify
from skopax.contract import Hooks, Locale, Panel, PassResult, Redaction, SourceData, TagRule
from skopax.engine import anomaly
from skopax.enrich import Enricher
from skopax.instances import REFRESH_S, Instances, PluginState
from skopax.investigator.tools import Effects, Sources, Toolbox
from skopax.jiralinks import listed
from skopax.messages import msg
from skopax.migrations import MigrationFailed
from skopax.models import email_allowed
from skopax.plugins import prometheus
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.slackevents import new_ticket
from skopax.settingsources import SourceSet
from skopax.sweeps import WINDOW_S, Sweeper
from skopax.tickets import create_task
from skopax.enrich import JiraRefused
from tests.conftest import AUTH, anomaly as plain_card, settings, state
from tests.plugin_kit import KIT_KEY, PROM_URL, add_instance, fake_transport, no_loop, registry_with, rule_patterns
from tests.plugins import demo_plugin
from tests.plugins.base import minimal
from tests.sso_kit import REDIRECT, sign_on
from tests.test_engine import NOW
from tests.test_investigator_tools import KEY, FakeProm, box, seeded, sources
from tests.test_plugin_hooks import DEMO, GOOD, boot, hooked, served
from tests.test_tickets import FakeJira

HERE = Path(__file__).parent / 'plugins'
ICON = (HERE / 'demo-icon.svg').read_text(encoding='utf-8')


def org_of(plugin_id: str, **values):
    def hook():
        return {k: list(v) for k, v in values.items()}
    return hooked(plugin_id, org_validators=hook)


def runtime_of(store, *plugins, directory=None) -> Runtime:
    registry = Registry.of(*plugins, directory=directory)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))
    runtime.refresh()
    return runtime


def folder(tmp_path: Path, **files) -> Path:
    home = tmp_path / 'plug'
    home.mkdir()
    (home / 'icon.svg').write_text(ICON, encoding='utf-8')
    for name, text in files.items():
        (home / name.replace('__', '.')).write_text(text, encoding='utf-8')
    return home


def tell_box(store, org, domains=()) -> Toolbox:
    told = []
    effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, tell=lambda key, email, text, by, name: told.append(email) or {'slack': {'permalink': 'https://example.slack.com/x'}, 'result': 'told'})
    box = Toolbox(sources(org=lambda: org), store, effects, [KEY], 'live', NOW, 'https://skopax.example.com', ask=True, asker='oscar.green@example.com', settings=dataclasses.replace(settings(), email_domains=domains))
    box.told = told
    return box


def test_ticket_projects_of_plugins_are_live_in_every_consumer(store):
    # given
    live = {'now': ()}
    card = {'status': 'open'}
    configure('https://jira.example.com', lambda: live['now'])

    # when
    before = (listed(lambda: live['now']), new_ticket(card, 'see PLUG-7', lambda: live['now']), linkify('PLUG-7'))
    live['now'] = ('PLUG',)
    after = (listed(lambda: live['now']), new_ticket(card, 'see PLUG-7', lambda: live['now']), linkify('PLUG-7'))
    configure('', ())

    # then
    assert before == ((), None, 'PLUG-7')
    assert after[:2] == (('PLUG',), 'PLUG-7') and after[2] == '[PLUG-7](https://jira.example.com/browse/PLUG-7)'


def test_create_task_accepts_a_plugin_project_only_when_given_it(store):
    # given
    store.save_state(state(plain_card(KEY)), None)
    jira = FakeJira()

    # when
    with pytest.raises(JiraRefused) as refused:
        create_task(store, jira, settings(), KEY, None, 'oscar.green@example.com', '', 'https://skopax.example.com', project='PLUG')
    made = create_task(store, jira, settings(), KEY, None, 'oscar.green@example.com', '', 'https://skopax.example.com', project='PLUG', extra_projects=('PLUG',))

    # then
    assert 'project must be one of' in str(refused.value)
    assert made[0].startswith('OPS-') and jira.created[0]['project'] == {'key': 'PLUG'}


def test_the_app_hands_plugin_projects_to_the_enricher_the_intake_and_the_ticket_action(store):
    # given
    plugin = org_of('projects', ticket_projects=('PLUG',))
    jira = FakeJira()
    store.save_state(state(plain_card(KEY)), None)

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(plugin), jira=jira)) as client:
        enricher = listed(client.app.state.enricher.projects)
        intake = client.app.state.intake.ticket_extra()
        done = client.post('/api/actions', json={'type': 'ticket', 'key': KEY, 'project': 'PLUG', 'label': 'Create task'}, headers=AUTH).json()

    # then
    assert 'PLUG' in enricher and intake == ('PLUG',)
    assert done['status'] == 'done' and jira.created[0]['project'] == {'key': 'PLUG'}


def test_enricher_accepts_a_callable_for_its_projects(store):
    # given
    enricher = Enricher(store, SimpleNamespace(enabled=False), SimpleNamespace(enabled=False), lambda: ('PLUG',))

    # then
    assert listed(enricher.projects) == ('PLUG',)


def test_the_stored_tag_rules_never_hold_plugin_rules_and_the_merge_happens_at_read(store, monkeypatch):
    # given
    monkeypatch.setattr(tags, 'RULES', (('marketing', ('type:gtm',)), ('seo', ('gone',))))
    plugin = dataclasses.replace(minimal('tagger', 'Tagger'), tag_rules=(TagRule('marketing', ('type:newsletter',)),))
    edited = tunables.apply(settings(), {'tags.seo.rules': 'type:crux'})
    first = anomaly('prom', 'newsletter', 'shop', 'checkout-service', '', 'warn', 10, 1, 1, '', 'Mail', 'mail down')
    second = anomaly('prom', 'newsletter', 'shop', 'cart-service', '', 'warn', 10, 1, 1, '', 'Mail', 'mail down')

    # when
    with TestClient(create_app(edited, store, registry=registry_with(plugin))) as client:
        client.app.state.server_apply([PassResult('prom', True, 'ok', [first], {}, ('prom',))], NOW, None)
        on = [t['id'] for t in store.anomaly(first['key']).get('tags', [])]
        client.app.state.plugin_state.set('tagger', False, 'alice')
        client.app.state.server_apply([PassResult('prom', True, 'ok', [second], {}, ('prom',))], NOW + 60, None)
        off = [t['id'] for t in store.anomaly(second['key']).get('tags', [])]

    # then
    assert dict(edited.tag_rules) == {'marketing': ('type:gtm',), 'seo': ('type:crux',)}
    assert on == ['marketing'] and off == []


def test_org_values_follow_a_changed_hook_after_refresh_and_leave_with_a_switched_off_plugin(store):
    # given
    live = {'hosts': ['one.example.net']}
    plugin = hooked('moving', org_validators=lambda: {'link_hosts': live['hosts']})
    runtime = runtime_of(store, plugin)

    # when
    first = runtime.org()['link_hosts']
    live['hosts'] = ['two.example.net']
    stale = runtime.org()['link_hosts']
    runtime.refresh()
    second = runtime.org()['link_hosts']
    runtime.state.set('moving', False, 'alice')
    gone = runtime.org()['link_hosts']

    # then
    assert (first, stale, second, gone) == (('one.example.net',), ('one.example.net',), ('two.example.net',), ())


def test_a_hook_that_succeeded_keeps_its_last_good_values_and_logs_once_per_streak(store, caplog):
    # given
    live = {'fail': False}

    def hook():
        if live['fail']:
            raise RuntimeError('down')
        return {'email_domains': ['example.com']}

    runtime = runtime_of(store, hooked('flaky', org_validators=hook))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        live['fail'] = True
        runtime.refresh()
        runtime.refresh()
        during = runtime.org()
        live['fail'] = False
        runtime.refresh()
        live['fail'] = True
        runtime.refresh()

    # then
    assert during['email_domains'] == ('example.com',) and during['email_closed'] is False
    assert sum('flaky' in r.getMessage() and 'org_validators' in r.getMessage() for r in caplog.records) == 2


def test_a_serving_hook_that_never_worked_closes_the_email_check(store):
    # given
    def boom():
        raise RuntimeError('down')

    runtime = runtime_of(store, hooked('broken-hook', org_validators=boom))

    # when
    org = runtime.org()

    # then
    assert org['email_closed'] is True and org['email_domains'] == ()
    assert not email_allowed('mallory@evil.test', org['email_domains'], closed=org['email_closed'])
    assert email_allowed('alice@example.com', (), closed=False)


def test_a_malformed_hook_result_also_counts_as_no_good_result(store):
    # given
    runtime = runtime_of(store, hooked('odd-hook', org_validators=lambda: ['not', 'a', 'dict']))

    # then
    assert runtime.org()['email_closed'] is True


def test_org_is_a_pure_snapshot_hooks_run_in_refresh_only_and_a_switch_refreshes_in_a_thread(store):
    # given
    calls = []

    def hook():
        calls.append(1)
        return {'link_hosts': ['one.example.net']}

    runtime = runtime_of(store, hooked('counted', org_validators=hook), hooked('later', org_validators=hook))
    after_boot = len(calls)
    runtime.state.set('later', False, 'alice')

    # when
    for _ in range(5):
        runtime.org()
        runtime.ui_hosts()
    quiet = len(calls)
    thread = runtime.set_enabled('later', True, 'alice')
    thread.join(5)

    # then
    assert after_boot == 2 and quiet == 2 and len(calls) == 4
    assert runtime.org()['link_hosts'] == ('one.example.net',)


def test_the_app_fills_org_before_the_first_request(store, monkeypatch):
    # given
    calls = []
    plugin = hooked('early', org_validators=lambda: calls.append(1) or {'email_domains': ['example.com']})
    monkeypatch.setattr(Runtime, 'loop', no_loop)

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(plugin))) as client:
        before = len(calls)
        client.get('/')
        client.get('/api/state', headers=AUTH)
        domains = client.app.state.runtime.org()['email_domains']

    # then
    assert before >= 1 and len(calls) == before and domains == ('example.com',)


def test_slack_tell_uses_org_domains_and_fails_closed(store):
    # given
    seeded(store)
    allowed = {'email_domains': ('plug.example.org',), 'email_closed': False}
    closed = {'email_domains': (), 'email_closed': True}
    args = {'name': 'Gary Cole', 'text': 'Look at this.', 'key': KEY}

    # when
    good = tell_box(store, allowed).call('slack_tell', {**args, 'email': 'gary.cole@plug.example.org'})
    bad = tell_box(store, allowed).call('slack_tell', {**args, 'email': 'gary.cole@evil.test'})
    shut = tell_box(store, closed).call('slack_tell', {**args, 'email': 'gary.cole@example.com'})
    open_ = tell_box(store, {'email_domains': (), 'email_closed': False}).call('slack_tell', {**args, 'email': 'gary.cole@evil.test'})

    # then
    assert not good[1] and bad[1] and 'work address' in bad[0] and shut[1] and 'work address' not in shut[0] and 'closed' in shut[0] and 'example.com' not in shut[0] and not open_[1]


def test_the_self_review_dm_check_uses_org_domains_and_fails_closed(store):
    # given
    ok = hooked('domains', org_validators=lambda: {'email_domains': ['plug.example.org']})

    def boom():
        raise RuntimeError('down')

    # when
    with served(store, ok, add=False) as client:
        good = client.put('/api/self-review', json={'text': 'Fine.', 'dm': ['alice@plug.example.org']}, headers=AUTH)
        bad = client.put('/api/self-review', json={'text': 'Fine.', 'dm': ['mallory@evil.test']}, headers=AUTH)
    with served(store, hooked('broken-hook', org_validators=boom), add=False) as client:
        shut = client.put('/api/self-review', json={'text': 'Fine.', 'dm': ['alice@example.com']}, headers=AUTH)

    # then
    assert good.status_code == 200 and bad.status_code == 422 and shut.status_code == 422


def test_propose_ticket_triage_and_the_ticket_schema_use_org_projects(store):
    # given
    seeded(store)
    org = {'ticket_projects': ('PLUG',)}
    issue = SimpleNamespace(issue=lambda key, fields: {'key': key, 'fields': {}}, comments=lambda key, limit: {'comments': [], 'total': 0})
    mine = Toolbox(sources(org=lambda: org), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), [KEY], 'live', NOW, 'https://skopax.example.com', ask=True, read_only=True, settings=settings())
    plain = Toolbox(sources(), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), [KEY], 'live', NOW, 'https://skopax.example.com', ask=True, read_only=True, settings=settings())
    args = {'title': 'Pool leak', 'description': 'It leaks.', 'project': 'PLUG'}

    # when
    refused = plain.call('propose_ticket', args)
    saved = mine.call('propose_ticket', args)
    schema = next(d for d in mine.definitions() if d['name'] == 'propose_ticket')['input_schema']
    tracked = box(store, jira=issue, org=lambda: org)
    tracked.call('jira_issue', {'key': 'PLUG-1'})
    done = tracked.call('triage', {'key': KEY, 'state': 'progress', 'ticket': 'PLUG-1'})

    # then
    assert refused[1] and 'project must be one of' in refused[0]
    assert not saved[1] and mine.proposal['project'] == 'PLUG'
    assert 'PLUG' in schema['properties']['project']['enum']
    assert not done[1] and store.anomaly(KEY)['ticket'] == 'PLUG-1'


def hook_runtime(store, hook, **kw):
    return runtime_of(store, hooked('tokens', oidc_token_request=hook), **kw)


def test_sso_redeem_sends_core_fields_over_hook_params_and_safe_headers_only(store, monkeypatch):
    # given
    seen = []
    monkeypatch.setattr('skopax.sso.post_form', lambda url, data, headers: seen.append((url, dict(data), dict(headers))) or {'access_token': 'x'})
    runtime = hook_runtime(store, lambda request: {'params': {'audience': 'api', 'client_id': 'evil', 'grant_type': 'password', 'code': 'stolen'},
                                                   'headers': {'Accept': 'x', 'content-type': 'y', 'HOST': 'z', 'X-Ok': '1', 'X-Bad': 'a\r\nInjected: 1', 'Bad\nName': 'v', 'X-Num': 7, 'X-Flag': True}})
    s = dataclasses.replace(settings(), sso_token_url='https://id.example.com/token', sso_token_headers='X-Client: web', sso_client_id='client-1', sso_scope='openid')
    signon = sign_on(s, runtime.token_request, secret='shh')
    _, pending = signon.start('/')

    # when
    with pytest.raises(AuthError):
        signon.finish(pending, 'code-1', pending['state'], None)

    # then
    assert seen == [('https://id.example.com/token', {'audience': 'api', 'grant_type': 'authorization_code', 'code': 'code-1', 'redirect_uri': REDIRECT,
                                                      'client_id': 'client-1', 'client_secret': 'shh', 'code_verifier': pending['verifier']}, {'X-Client': 'web', 'X-Ok': '1', 'X-Num': '7'})]


def test_a_refused_header_logs_a_warning(store, caplog):
    # given
    runtime = hook_runtime(store, lambda request: {'headers': {'X-Bad': 'a\nb'}})

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        out = runtime.token_request({})

    # then
    assert out == {'headers': {}, 'params': {}} and any('X-Bad' in r.getMessage() for r in caplog.records)


def test_the_token_value_filter_keeps_strings_and_ints_only(store):
    # given
    runtime = hook_runtime(store, lambda request: {'params': {'a': 'x', 'b': 2, 'c': True, 'd': None, 'e': [1], 7: 'num', 'f': 1.5}, 'headers': {'X-A': 'x', 'X-B': None, 'X-C': ['1']}})

    # then
    assert runtime.token_request({}) == {'headers': {'X-A': 'x'}, 'params': {'a': 'x', 'b': '2'}}


def test_undeclared_files_in_the_plugin_folder_are_never_served(store, tmp_path):
    # given
    home = folder(tmp_path, declared__js='window.a = 1;', extra__js='window.extra = 1;', extra__html='<p>x</p>', secret__html='<p>s</p>')
    plugin = dataclasses.replace(minimal('guard', 'Guard'), icon='icon.svg', panels=(Panel('pt', 'declared.js'),))
    runtime = runtime_of(store, plugin, directory=home)

    # when
    got = {name: runtime.asset('guard', name) for name in ('declared.js', 'extra.js', 'extra.html', 'secret.html', 'icon.svg')}
    served_ = TestClient(create_app(settings(), store, registry=Registry.of(plugin, directory=home)))
    with served_ as client:
        status = {name: client.get(f'/plugins/guard/{name}').status_code for name in ('declared.js', 'extra.js', 'extra.html', 'secret.html')}

    # then
    assert got['declared.js'] is not None and got['icon.svg'] is not None
    assert got['extra.js'] is None and got['extra.html'] is None and got['secret.html'] is None
    assert status == {'declared.js': 200, 'extra.js': 404, 'extra.html': 404, 'secret.html': 404}


def test_the_asset_guards_each_refuse_on_their_own(store, tmp_path, monkeypatch):
    # given
    (tmp_path / 'sub').mkdir()
    home = folder(tmp_path, ok__js='window.ok = 1;')
    (home / 'sub').mkdir()
    (home / 'sub' / 'in.js').write_text('window.in = 1;', encoding='utf-8')
    (home / 'big.js').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
    (home / 'edge.js').write_bytes(b'x' * (2 * 1024 * 1024))
    panels = (Panel('pa', 'ok.js'), Panel('pb', 'sub/in.js'), Panel('pc', 'big.js'), Panel('pd', 'edge.js'))
    plugin = dataclasses.replace(minimal('edgy', 'Edgy'), icon='icon.svg', panels=panels)
    broken = dataclasses.replace(minimal('broke', 'Broke'), icon='icon.svg', version='x', panels=(Panel('pa', 'ok.js'),))
    runtime = runtime_of(store, plugin, broken, directory=home)

    # when
    separators = [runtime.asset('edgy', name) for name in ('sub/in.js', 'sub\\in.js', 'ok.js\x00', '.', '..', '', 'ok.js/')]
    over = runtime.asset('edgy', 'big.js')
    edge = runtime.asset('edgy', 'edge.js')
    gone = [runtime.asset('broke', 'icon.svg'), runtime.asset('broke', 'ok.js'), runtime.asset('nope', 'icon.svg')]
    unreadable_rt, invalid_rt = runtime_of(store, plugin, directory=home), runtime_of(store, plugin, directory=home)
    with monkeypatch.context() as m:
        m.setattr(Path, 'read_bytes', lambda self: (_ for _ in ()).throw(OSError('disk')))
        unreadable = unreadable_rt.asset('edgy', 'ok.js')
    with monkeypatch.context() as m:
        m.setattr(Path, 'resolve', lambda self, strict=False: (_ for _ in ()).throw(ValueError('bad path')))
        invalid = invalid_rt.asset('edgy', 'ok.js')

    # then
    assert separators == [None] * 7 and over is None and edge is not None and len(edge[0]) == 2 * 1024 * 1024
    assert gone == [None, None, None] and unreadable is None and invalid is None


def test_panel_files_are_read_once_per_refresh(store, tmp_path):
    # given
    home = folder(tmp_path, ok__js='window.v = 1;')
    plugin = dataclasses.replace(minimal('cached', 'Cached'), icon='icon.svg', panels=(Panel('pa', 'ok.js'),))
    runtime = runtime_of(store, plugin, directory=home)
    first = runtime.asset('cached', 'ok.js')
    (home / 'ok.js').write_text('window.v = 2;', encoding='utf-8')

    # when
    same = runtime.asset('cached', 'ok.js')
    runtime.refresh()
    fresh = runtime.asset('cached', 'ok.js')

    # then
    assert first[0] == same[0] == b'window.v = 1;' and fresh[0] == b'window.v = 2;'


def test_served_assets_carry_nosniff_and_sandbox_for_svg_and_html(store, tmp_path):
    # given
    home = folder(tmp_path, ok__js='window.v = 1;', tile__html='<p>tile</p>')
    plugin = dataclasses.replace(minimal('safe', 'Safe'), icon='icon.svg', panels=(Panel('pa', 'ok.js', 'tile.html'),), locales=(Locale('de', {'A': 'B'}, {}),))

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(plugin, directory=home))) as client:
        got = {name: client.get(f'/plugins/safe/{name}') for name in ('icon.svg', 'ok.js', 'tile.html', 'i18n-de.js')}

    # then
    assert all(r.status_code == 200 and r.headers['x-content-type-options'] == 'nosniff' for r in got.values())
    assert [r.headers.get('content-security-policy') for r in got.values()] == ['sandbox', None, 'sandbox', None]


@pytest.mark.parametrize('over', [{'panels': ('oops.js',)}, {'panels': (Panel('pt', ['a.js']),)}, {'panels': (Panel(5, 'a.js'),)}, {'panels': (Panel('pt', 'a.js', 7),)},
                                  {'redactions': (Redaction('x', ['p']),)}, {'redactions': ('x',)}, {'redactions': (Redaction(3, 'p'),)},
                                  {'tag_rules': (TagRule('t', 5),)}, {'tag_rules': ('t',)}, {'tag_rules': (TagRule('t', (1,)),)}])
def test_a_malformed_contribution_breaks_only_its_plugin(over):
    # given
    bad = dataclasses.replace(minimal('shaky', 'Shaky'), **over)

    # when
    reg = Registry.of(DEMO, bad)
    states = {x.id: x.state for x in reg.loaded()}

    # then
    assert states == {'demo': 'ok', 'shaky': 'broken'}
    assert [p.id for p in reg.usable()] == ['demo']


FRENCH = Locale('fr', {'A': 'C'})


@pytest.mark.parametrize('pack, lang, part, kept', [(Locale('de', {'k': object(), 'A': 'B'}, {}), 'de', "page['k']", (Locale('de', {'A': 'B'}, {}), FRENCH)),
                                                     (Locale('../x', {'A': 'B'}, {}), '../x', 'locales[0]', (FRENCH,)),
                                                     (Locale('de', {1: 'x', 'A': 'B'}, {}), 'de', 'page[<int>]', (Locale('de', {'A': 'B'}, {}), FRENCH)),
                                                     (Locale('de', {'A': 'B'}, {'k': object(), 'slack.cancel': 'Abbrechen'}), 'de', "messages['k']",
                                                      (Locale('de', {'A': 'B'}, {'slack.cancel': 'Abbrechen'}), FRENCH)),
                                                     ('de', '?', 'locales[0]', (FRENCH,))])
def test_a_malformed_locale_pack_is_left_out_and_its_plugin_still_loads(pack, lang, part, kept, caplog):
    # given
    bad = dataclasses.replace(minimal('shaky', 'Shaky'), locales=(pack, FRENCH))
    caplog.set_level(logging.WARNING, logger='skopax.registry')

    # when
    reg = Registry.of(DEMO, bad)
    states = {x.id: x.state for x in reg.loaded()}

    # then
    assert states == {'demo': 'ok', 'shaky': 'ok'}
    assert [p.id for p in reg.usable()] == ['demo', 'shaky']
    assert reg.get('shaky').plugin.locales == kept
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.registry'] == [msg('locales.part_dropped', lang=lang, plugin='shaky', part=part, reason=msg('plugins.why.locale', limit=1000))]


def test_a_panel_without_a_script_is_valid_and_gets_no_script_tag(store):
    # given
    ok = dataclasses.replace(minimal('quiet', 'Quiet'), panels=(Panel('pt', None, 'tile.html'),))

    # when
    reg = Registry.of(ok)
    runtime = runtime_of(store, ok)

    # then
    assert reg.loaded()[0].state == 'ok' and runtime.page_scripts() == []


def test_runtime_skips_a_bad_item_with_one_warning_and_keeps_the_rest(store, caplog, monkeypatch):
    # given
    monkeypatch.setattr('skopax.registry._check', lambda plugin, directory: None)
    monkeypatch.setattr('skopax.registry._kept_locales', lambda plugin: (plugin, (), ()))
    bad = dataclasses.replace(minimal('shaky', 'Shaky'), panels=('oops.js', Panel('pt', ['a.js']), Panel('ok', 'demo-panel.js')), redactions=(Redaction('x', ['p']), Redaction('good', r'GOOD-\d{4}')),
                              tag_rules=(TagRule('marketing', 5), TagRule('seo', ('type:crux',))), locales=(Locale('de', {'k': object()}, {}), Locale('fr', {'A': 'B'}, {'m': 'n'})))
    other = dataclasses.replace(minimal('other', 'Other'), redactions=(Redaction('ticket', r'DEMO-\d{4}'),))
    registry = Registry.of(bad, other)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        found = [runtime.panels(), runtime.panels(), runtime.locales(), runtime.tag_rules(), runtime.page_scripts()]
        patterns = [p.pattern for p in rule_patterns(runtime)]
        once = len([r for r in caplog.records if 'shaky' in r.getMessage()])
        runtime.panels(), runtime.locales(), runtime.tag_rules(), runtime.page_scripts(), rule_patterns(runtime)
        twice = len([r for r in caplog.records if 'shaky' in r.getMessage()])

    # then
    assert [p.tab for _, p in found[0]] == ['ok'] and found[1] == found[0]
    assert [loc.lang for _, loc in found[2]] == ['fr'] and found[3] == {'seo': ('type:crux',)}
    assert found[4] == ['/plugins/shaky/i18n-fr.js', '/plugins/shaky/demo-panel.js']
    assert patterns == [r'DEMO-\d{4}', r'GOOD-\d{4}']
    assert once >= 1 and once == twice


def test_a_bad_contribution_never_breaks_the_page_or_other_redactions(store):
    # given
    bad = dataclasses.replace(minimal('shaky', 'Shaky'), panels=('oops.js',), redactions=(Redaction('x', ['p']),))
    add_instance(store, 'demo', 'main', sealer_key=master_key(store.db, settings()), **GOOD)

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(DEMO, bad))) as client:
        page = client.get('/')
        out = redact.redact('see DEMO-1234')

    # then
    assert page.status_code == 200 and out == f'see {redact.REDACTED}'


@pytest.mark.parametrize('pattern', ['', 'a*', '(', 'x?', '(a|b)*', 'a' * 501])
def test_a_redaction_pattern_that_matches_nothing_or_is_invalid_or_too_long_breaks_the_plugin(pattern):
    # given
    bad = dataclasses.replace(minimal('rx', 'Rx'), redactions=(Redaction('r', pattern),))

    # when
    found = Registry.of(bad).loaded()[0]

    # then
    assert found.state == 'broken' and found.reason


@pytest.mark.parametrize('pattern', [r'DEMO-\d{4}', r'(?:ab)+c', r'[a+*]{3}', r'\(a+\)+', r'(a){3}', r'(?P<id>[A-Z]{2})-\d+', r'x{2,3}'])
def test_ordinary_redaction_patterns_are_accepted(pattern):
    # given
    good = dataclasses.replace(minimal('rx', 'Rx'), redactions=(Redaction('r', pattern),))

    # then
    assert Registry.of(good).loaded()[0].state == 'ok'


def test_unknown_tag_rules_warn_once_per_plugin_and_tag(store, caplog):
    # given
    plugin = dataclasses.replace(minimal('tagger', 'Tagger'), tag_rules=(TagRule('not-a-tag', ('type:x',)),))
    runtime = runtime_of(store, plugin)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        runtime.tag_rules()
        runtime.tag_rules()

    # then
    assert len([r for r in caplog.records if 'not-a-tag' in r.getMessage()]) == 1


def test_a_failing_redaction_or_tag_source_falls_back_to_the_built_ins(caplog):
    # given
    def boom():
        raise RuntimeError('down')

    redact.use_rules(boom)
    tags.use_rules(boom)

    # when
    with caplog.at_level(logging.WARNING):
        out = redact.redact('mail alice@example.com')
        rules = tags.rules()

    # then
    assert out == f'mail {redact.REDACTED}' and rules == tags.RULES
    assert {'skopax.redact', 'skopax.tags'} <= {r.name for r in caplog.records}


def test_plugin_script_tags_escape_the_path():
    # then
    assert plugin_script_tags(['/plugins/x/a"b.js']) == '<script src="/plugins/x/a&quot;b.js"></script>'


def test_page_rereads_do_not_hide_a_background_refresh_that_stopped(store, caplog):
    # given
    now, ran = [1000.0], []
    registry = Registry.of(prometheus.PLUGIN)
    instances = Instances(store.db, registry, lambda: Sealer(KIT_KEY), clock=lambda: now[0])
    runtime = Runtime(registry, PluginState(store.db), instances, fake_transport(FakeProm([])))
    add_instance(store, prometheus.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, url=PROM_URL)
    sweeper = Sweeper(runtime, store, lambda results, *args, **kwargs: ran.extend(r.name for r in results))
    runtime.refresh()
    sweeper.run('prometheus', 'prom', NOW)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.sweeps'):
        for _ in range(int(WINDOW_S // 5) + 2):
            now[0] += 5
            instances.reread()
            instances.fresh('prometheus', 'main')
        skipped = sweeper.run('prometheus', 'prom', NOW)

    # then
    assert ran == ['prom'] and skipped is None
    assert instances.age() < WINDOW_S and instances.live_age() > WINDOW_S
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.sweeps'] == [msg('sweeps.stale_instances', plugin='prometheus', seconds=int(instances.live_age()))]


def test_a_hook_that_hangs_does_not_stop_the_prometheus_pass(store, caplog):
    # given
    release, calls, now, ran = threading.Event(), [], [1000.0], []

    def stuck():
        calls.append(1)
        release.wait(10)
        return {}

    registry = Registry.of(prometheus.PLUGIN, hooked('stuck', org_validators=stuck))
    instances = Instances(store.db, registry, lambda: Sealer(KIT_KEY), clock=lambda: now[0])
    runtime = Runtime(registry, PluginState(store.db), instances, fake_transport(FakeProm([])), hook_timeout=0.05)
    add_instance(store, prometheus.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, url=PROM_URL)
    sweeper = Sweeper(runtime, store, lambda results, *args, **kwargs: ran.extend(r.name for r in results))

    # when
    hung = False
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        for _ in range(4):
            now[0] += REFRESH_S
            worker = threading.Thread(target=runtime.refresh, daemon=True)
            worker.start()
            worker.join(2)
            hung = worker.is_alive()
            if hung:
                break
        sweeper.run('prometheus', 'prom', NOW)
    closed = runtime.org()['email_closed']
    release.set()
    warned = [r.getMessage() for r in caplog.records if 'stuck' in r.getMessage()]

    # then
    assert not hung and ran == ['prom']
    assert calls == [1] and len(warned) == 1 and 'TimedOut' in warned[0]
    assert closed is True


def test_a_settings_source_that_hangs_keeps_the_instance_list_fresh(store, caplog):
    # given
    release, calls, now = threading.Event(), [], [1000.0]

    def hanging():
        calls.append(1)
        release.wait(3)
        return []

    registry = registry_with(demo_plugin)
    instances = Instances(store.db, registry, lambda: Sealer('k'), sources=hanging, clock=lambda: now[0], source_timeout=0.05)
    add_instance(store, 'demo', 'main', sealer_key='k', primary=True, url='https://demo.example.com')

    # when
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger='skopax.instances'):
        for _ in range(3):
            now[0] += REFRESH_S
            instances.refresh()
    took = time.monotonic() - began
    release.set()
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.instances']

    # then
    assert took < 2 and calls == [1]
    assert instances.age() == 0 and [i.name for i in instances.all('demo')] == ['main']
    assert warned == [msg('instances.sources_failed', error='TimedOut')]


def test_a_migration_catch_up_that_hangs_does_not_hold_the_refresh(store, caplog):
    # given
    release, calls = threading.Event(), []

    def migrate():
        calls.append(1)
        release.wait(3)
        return None

    registry = Registry.of()
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')), hook_timeout=0.05)
    runtime.migrate = migrate

    # when
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        runtime.refresh()
        runtime.refresh()
    took = time.monotonic() - began
    release.set()
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.runtime']

    # then
    assert took < 2 and calls == [1] and runtime.instances.age() is not None
    assert warned == [msg('runtime.step_failed', step='migrate', error='TimedOut')]


def test_a_migration_that_fails_after_its_deadline_is_still_named_in_the_log(store, caplog):
    # given
    release = threading.Event()

    def migrate():
        release.wait(3)
        raise MigrationFailed('the remap failed', 'remap', 'RuntimeError')

    registry = Registry.of()
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')), hook_timeout=0.05)
    runtime.migrate = migrate

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        runtime.refresh()
        release.set()
        for worker in [t for t in threading.enumerate() if t.name == 'plugin-hook']:
            worker.join(2)
    said = [r.getMessage() for r in caplog.records if r.name == 'skopax.runtime']

    # then
    assert said == [msg('runtime.step_failed', step='migrate', error='TimedOut'), msg('runtime.migration_failed', step='migrate', migration='remap', error='RuntimeError')]


def within(fn, seconds: float = 2) -> list:
    box: list = []
    worker = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    worker.start()
    worker.join(seconds)
    return box


def test_an_entitlement_hook_that_hangs_does_not_stop_the_prometheus_pass(store, caplog):
    # given
    release, calls, now, ran, hang = threading.Event(), [], [1000.0], [], [False]

    def licence():
        calls.append(1)
        if hang[0]:
            release.wait(10)
        return 'enterprise'

    registry = Registry.of(prometheus.PLUGIN, hooked('licence', entitlement=licence))
    registry.hook_timeout = 0.05
    instances = Instances(store.db, registry, lambda: Sealer(KIT_KEY), clock=lambda: now[0])
    runtime = Runtime(registry, PluginState(store.db), instances, fake_transport(FakeProm([])), hook_timeout=0.05)
    add_instance(store, prometheus.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, url=PROM_URL)
    sweeper = Sweeper(runtime, store, lambda results, *args, **kwargs: ran.extend(r.name for r in results))
    hang[0] = True

    def tick() -> bool:
        now[0] += REFRESH_S
        runtime.refresh()
        return True

    def sweep() -> bool:
        sweeper.run('prometheus', 'prom', NOW)
        return True

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.registry'):
        refreshed = [within(tick) for _ in range(4)]
        serving = within(lambda: sorted(p.id for p, _ in runtime.serving()))
        swept = within(sweep)
        edition = registry.edition()
    release.set()
    warned = [r.getMessage() for r in caplog.records if 'licence' in r.getMessage()]

    # then
    assert refreshed == [[True]] * 4 and serving == [['licence', 'prometheus']] and swept == [True] and ran == ['prom']
    assert calls == [1, 1] and edition == 'enterprise'
    assert len(warned) == 1 and 'TimedOut' in warned[0] and 'enterprise' in warned[0]


def test_a_settings_source_hook_that_hangs_is_bounded_and_its_late_answer_dropped(caplog):
    # given
    release, calls = threading.Event(), []

    def load():
        calls.append(1)
        release.wait(3)
        return SourceData('hook', {'general.timezone': 'UTC'})

    sources = SourceSet([('hook', load)], {'hook': None}, timeout=0.05)

    # when
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger='skopax.settingsources'):
        changed = [sources.refresh(), sources.refresh()]
    took = time.monotonic() - began
    status = sources.status()['hook']
    release.set()
    for worker in [t for t in threading.enumerate() if t.name == 'settings-source']:
        worker.join(2)
    after = sources.current()
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.settingsources']

    # then
    assert took < 2 and calls == [1] and changed == [False, False]
    assert (status['ok'], status['error']) == (False, 'TimedOut')
    assert after == []
    assert warned == [msg('settings.source.kept', source='hook', error='TimedOut')]


def test_a_step_that_fails_after_its_deadline_is_still_logged_with_its_error(store, caplog):
    # given
    release = threading.Event()

    def heal():
        release.wait(3)
        raise RuntimeError('token=s3cret-value')

    registry = Registry.of()
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')), hook_timeout=0.05)
    runtime.after_refresh = heal

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        runtime.refresh()
        release.set()
        for worker in [t for t in threading.enumerate() if t.name == 'plugin-hook']:
            worker.join(2)
    said = [r.getMessage() for r in caplog.records if r.name == 'skopax.runtime']

    # then
    assert said == [msg('runtime.step_failed', step='heal', error='TimedOut'), msg('runtime.step_failed', step='heal', error='RuntimeError')]
    assert all('s3cret' not in line for line in said)


def test_a_hook_deadline_and_the_refresh_pause_stay_inside_the_stale_window():
    # then
    assert HOOK_TIMEOUT_S + REFRESH_S < WINDOW_S
