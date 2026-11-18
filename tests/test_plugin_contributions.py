import dataclasses
import logging
import threading
from types import SimpleNamespace
from unittest import mock

import pytest
import regex
from fastapi.testclient import TestClient

from skopax import redact, tags, tunables
from skopax.app import create_app
from skopax.chatui.text import configure, linkify
from skopax.contract import Field, Panel, PassResult, Redaction, Tag, TagRule
from skopax.engine import anomaly
from skopax.investigator.tools import Effects, Toolbox
from skopax.kube import Kube
from skopax.models import email_allowed
from skopax.plugins import jira as jira_plugin
from skopax.registry import Registry
from skopax.slack import answer_blocks
from skopax.tickets import create_task
from tests.conftest import AUTH, anomaly as plain_card, settings, state
from tests.plugin_kit import add_instance, idle_jira, registry_with, rule_patterns, serve_jira
from tests.plugins.base import minimal
from tests.sso_kit import redeem, sign_on
from tests.test_auth import sso_settings
from tests.test_engine import NOW
from tests.test_investigator_tools import KEY, seeded, sources
from tests.test_plugin_runtime_guards import hook_runtime, runtime_of
from tests.test_plugin_hooks import hooked
from tests.test_tickets import FakeJira


TEAM_MARKETING = 'type:gtm, type:umami, type:ga, type:ad-link*, type:ab-test, type:mailer'


def newsletter(name: str):
    return anomaly('prom', name, 'shop', f'{name}-service', '', 'warn', 10, 1, 1, '', 'Mail', 'mail down')


def card_tags(store, card) -> list[str]:
    return [t['id'] for t in store.anomaly(card['key']).get('tags', [])]


def test_an_admin_tag_edit_keeps_plugin_terms_out_of_the_stored_value_and_switch_off_removes_them(store):
    # given
    plugin = dataclasses.replace(minimal('tagger', 'Tagger'), tag_rules=(TagRule('marketing', ('type:newsletter',)),))
    plugged, edited = newsletter('newsletter'), newsletter('zeta')

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(plugin))) as client:
        listed = {e['key']: e for e in client.get('/api/admin/settings', headers=AUTH).json()['items']}['tags.marketing.rules']
        client.put('/api/admin/settings', json={'key': 'tags.marketing.rules', 'value': 'type:zeta'}, headers=AUTH)
        after = {e['key']: e for e in client.get('/api/admin/settings', headers=AUTH).json()['items']}['tags.marketing.rules']
        client.app.state.server_apply([PassResult('prom', True, 'ok', [plugged, edited], {}, ('prom',))], NOW, None)
        on = (card_tags(store, plugged), card_tags(store, edited))
        client.app.state.plugin_state.set('tagger', False, 'alice')
        later = newsletter('newsletter2')
        client.app.state.server_apply([PassResult('prom', True, 'ok', [later], {}, ('prom',))], NOW + 60, None)
        off = card_tags(store, later)
        again = {e['key']: e for e in client.get('/api/admin/settings', headers=AUTH).json()['items']}['tags.marketing.rules']

    # then
    assert listed['value'] == TEAM_MARKETING and listed['pluginTerms'] == ['type:newsletter']
    assert after['value'] == 'type:zeta' and after['pluginTerms'] == ['type:newsletter']
    assert on == (['marketing'], ['marketing'])
    assert off == [] and again['value'] == 'type:zeta' and again['pluginTerms'] == []


def test_without_an_admin_edit_the_default_terms_stay_the_base_ones(store):
    # given
    plugin = dataclasses.replace(minimal('tagger', 'Tagger'), tag_rules=(TagRule('marketing', ('type:newsletter',)),))

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(plugin))) as client:
        entry = {e['key']: e for e in client.get('/api/admin/settings', headers=AUTH).json()['items']}['tags.marketing.rules']

    # then
    assert entry['default'] == entry['value'] == TEAM_MARKETING and entry['pluginTerms'] == ['type:newsletter']


@pytest.mark.parametrize('value', ['example.com', [123], ['  '], {'a': 'b'}, ['example.com', None]])
def test_a_malformed_email_domains_answer_is_no_good_result(store, value, caplog):
    # given
    runtime = runtime_of(store, hooked('shaky', org_validators=lambda: {'email_domains': value, 'link_hosts': ['example.net']}))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        runtime.refresh()
        runtime.refresh()
    org = runtime.org()

    # then
    assert org['email_closed'] is True and org['link_hosts'] == ()
    assert not email_allowed('mallory@evil.test', org['email_domains'], closed=org['email_closed'])
    assert len([r for r in caplog.records if 'shaky' in r.getMessage()]) == 1


def test_a_malformed_answer_after_a_good_one_keeps_the_last_good_values(store, caplog):
    # given
    live = {'value': ['example.com']}
    runtime = runtime_of(store, hooked('shaky', org_validators=lambda: {'email_domains': live['value']}))
    live['value'] = 'example.org'

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        runtime.refresh()
        runtime.refresh()
    org = runtime.org()

    # then
    assert org['email_domains'] == ('example.com',) and org['email_closed'] is False
    assert len([r for r in caplog.records if 'shaky' in r.getMessage()]) == 1


def test_a_switched_off_org_plugin_loses_its_last_good_values_for_good(store):
    # given
    live = {'fail': False}

    def hook():
        if live['fail']:
            raise RuntimeError('down')
        return {'email_domains': ['example.com']}

    runtime = runtime_of(store, hooked('moody', org_validators=hook))
    runtime.state.set('moody', False, 'alice')
    runtime.refresh()
    live['fail'] = True

    # when
    runtime.state.set('moody', True, 'alice')
    runtime.refresh()

    # then
    assert runtime.org()['email_closed'] is True and runtime.org()['email_domains'] == ()


def test_org_never_calls_a_hook_itself(store):
    # given
    calls = []
    plugin = hooked('counted', org_validators=lambda: calls.append(1) or {'email_domains': ['example.com']})
    runtime = runtime_of(store, plugin)
    fresh = runtime_of(store, hooked('counted', org_validators=lambda: calls.append(1) or {}))
    fresh._good.clear()
    before = len(calls)

    # when
    first = fresh.org()
    runtime.org()

    # then
    assert len(calls) == before and first['email_closed'] is True


HEADERS = {'X:Colon': 'v', ' X-Lead': 'v', 'X-Wide': '\u0161', 'X-Nul': 'a\x00b', 'X-Del': 'a\x7fb', '': 'v', '   ': 'v', 'Content-Length': '5', 'content-length': '5', 'Transfer-Encoding': 'chunked',
           'CONNECTION': 'close', 'X Space': 'v', 'X-Ok': 'fine', 'X-Tab': 'a\tb'}


def test_sso_redeem_drops_unsafe_hook_headers_and_still_succeeds(store, monkeypatch, caplog):
    # given
    seen = []
    monkeypatch.setattr('skopax.sso.post_form', lambda url, data, headers: seen.append(dict(headers)) or {'access_token': 'x'})
    runtime = hook_runtime(store, lambda request: {'headers': HEADERS})
    s = dataclasses.replace(settings(), sso_token_url='https://id.example.com/token', sso_client_id='c1')
    signon = sign_on(s, runtime.token_request, secret='shh')

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        for _ in range(2):
            redeem(signon, 'code')

    # then
    assert seen == [{'X-Ok': 'fine', 'X-Tab': 'a\tb'}] * 2
    refused = [r.getMessage() for r in caplog.records if 'refused' in r.getMessage()]
    assert len(refused) == len({m for m in refused}) and all('\x00' not in m and '\x7f' not in m and '\u0161' not in m and 'chunked' not in m for m in refused)
    assert any('X-Nul' in m for m in refused) and any('Content-Length' in m or 'content-length' in m for m in refused)


def test_each_token_hook_gets_its_own_copy_of_the_request(store):
    # given
    seen = []

    def first(request):
        request['client_id'] = 'tampered'
        request['nested'].append('tampered')
        return {}

    def second(request):
        seen.append(request)
        return {}

    runtime = runtime_of(store, hooked('aaa-first', oidc_token_request=first), dataclasses.replace(hooked('bbb-second', oidc_token_request=second), after=('aaa-first',)))
    request = {'client_id': 'c1', 'nested': ['a']}

    # when
    runtime.token_request(request)

    # then
    assert seen == [{'client_id': 'c1', 'nested': ['a']}] and request == {'client_id': 'c1', 'nested': ['a']}


def test_a_failing_token_hook_or_rule_source_logs_the_type_only(store, monkeypatch, caplog):
    # given
    def boom(*args):
        raise RuntimeError('secret-detail-9')

    sent = []
    monkeypatch.setattr('skopax.sso.post_form', lambda url, data, headers: sent.append(url) or {})
    signon = sign_on(settings(), boom)
    tags.use_rules(boom)
    tags.use_tags(boom)
    redact.use_rules(boom)

    # when
    with caplog.at_level(logging.WARNING):
        _, failure = redeem(signon)
        tags.rules()
        tags.plugin_rules()
        tags.plugin_tags()
        redact.redact('x')
        with pytest.raises(RuntimeError):
            runtime_of(store, hooked('loud', org_validators=boom, oidc_token_request=boom)).token_request({})

    # then
    assert 'RuntimeError' in caplog.text and 'secret-detail-9' not in caplog.text
    assert (failure.reason, sent) == ('hook', [])


def test_a_plugin_project_enabled_after_startup_reaches_chat_links(store):
    # given
    live = {'projects': []}
    plugin = hooked('projects', org_validators=lambda: {'ticket_projects': live['projects']})
    s = dataclasses.replace(sso_settings(), chat_ui=True)
    serve_jira(store, s)

    # when
    try:
        with TestClient(create_app(s, store, registry=registry_with(plugin, jira_plugin), jira=idle_jira()), base_url='https://testserver') as client:
            before = linkify('see PLUG-7')
            live['projects'] = ['PLUG']
            client.app.state.runtime.refresh()
            after = linkify('see PLUG-7')
    finally:
        configure('', ())

    # then
    assert before == 'see PLUG-7' and after == 'see [PLUG-7](https://jira.example.com/browse/PLUG-7)'


def test_a_plugin_project_enabled_after_startup_reaches_the_intake_decision(store):
    # given
    live = {'projects': []}
    plugin = hooked('projects', org_validators=lambda: {'ticket_projects': live['projects']})
    offered = []
    serve_jira(store, settings())

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(plugin, jira_plugin), jira=idle_jira())) as client:
        intake = client.app.state.intake
        intake.listen = 'live'
        intake.investigator = SimpleNamespace(enabled=True, why_halted=lambda now: 'stopped')
        intake._say = lambda *a, **k: 'said'
        intake._offer_ticket = lambda channel, ts, root, user, login, card, ticket, record, now: offered.append(ticket) or 'offered'
        intake._remember_said = lambda *a, **k: None
        args = ('C1', '1.1', '1.0', 'card', 'U1', 'adam.baker@example.com', 'see PLUG-7 please', {'status': 'open'}, False, {'operator': True}, NOW)
        intake._decide(*args)
        before = list(offered)
        live['projects'] = ['PLUG']
        client.app.state.runtime.refresh()
        intake._decide(*args)

    # then
    assert before == [] and offered == ['PLUG-7']


def test_create_task_ignores_a_malformed_extra_project(store):
    # given
    store.save_state(state(plain_card(KEY)), None)

    # when
    with pytest.raises(Exception) as refused:
        create_task(store, FakeJira(), settings(), KEY, None, 'oscar.green@example.com', '', 'https://skopax.example.com', project='BAD KEY', extra_projects=('BAD KEY',))

    # then
    assert 'project must be one of' in str(refused.value)


def test_closed_refusals_never_list_domains(store):
    # given
    boom = hooked('shaky', org_validators=lambda: (_ for _ in ()).throw(RuntimeError('x')))
    seeded(store)
    effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, tell=lambda key, email, text, by, name: {'slack': {'permalink': 'https://example.slack.com/x'}, 'result': 'told'})
    box = Toolbox(sources(org=lambda: {'email_domains': ('secret.example.org',), 'email_closed': True}), store, effects, [KEY], 'live', NOW, 'https://skopax.example.com', ask=True,
                  asker='oscar.green@example.com', settings=dataclasses.replace(settings(), email_domains=('example.com',)))

    # when
    tool = box.call('slack_tell', {'name': 'Gary Cole', 'email': 'gary.cole@example.com', 'text': 'Look.', 'key': KEY})
    with TestClient(create_app(dataclasses.replace(settings(), email_domains=('example.com',)), store, registry=registry_with(boom))) as client:
        page = client.put('/api/self-review', json={'text': 'Fine.', 'dm': ['alice@example.com']}, headers=AUTH)

    # then
    assert tool[1] and 'secret.example.org' not in tool[0] and 'example.com' not in tool[0] and 'closed' in tool[0]
    assert page.status_code == 422 and 'example.com' not in page.json()['detail'] and 'closed' in page.json()['detail']


@pytest.mark.parametrize('pattern', [r'(?:\d{1,3}\.){3}\d{1,3}', r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', r'[\w.+-]+@(?:[\w-]+\.)+\w+', '(a{2})+', '(a+)?b', r'([+*]x)+', r'(x)\1+y', '(a+)+', r'(\w+\s?)+', '(?i)(a+A)+$', r'(a+\x61)+$', 'a' * 500])
def test_plugin_redaction_patterns_that_compile_and_match_text_are_accepted(pattern):
    # given
    good = dataclasses.replace(minimal('rx', 'Rx'), redactions=(Redaction('r', pattern),))

    # then
    assert Registry.of(good).loaded()[0].state == 'ok'


def test_the_static_nested_repeat_check_is_gone():
    # then
    import skopax.registry as registry
    assert not hasattr(registry, 'nested_repeat') and not hasattr(registry, '_atom_end') and not hasattr(registry, '_literal')


def slow_runtime(store, *patterns, plugin_id='slow'):
    rules = tuple(Redaction(f'r{i}', p) for i, p in enumerate(patterns))
    plugin = dataclasses.replace(minimal(plugin_id, 'Slow'), redactions=rules)
    runtime = runtime_of(store, plugin)
    redact.use_rules(runtime.redaction_rules, runtime.redaction_timed_out)
    return runtime


def timeout_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == 'skopax.runtime' and 'slowest rule' in r.getMessage()]


def bounded(call, seconds: float = 2.0):
    box: dict = {}
    worker = threading.Thread(target=lambda: box.setdefault('out', call()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f'the call did not finish within {seconds} s'
    return box['out']


@pytest.mark.parametrize('pattern, hostile', [(r'(a+){1,30}$', 'a' * 40 + '!'), (r'(a|aa)+$', 'a' * 60 + '!')])
def test_a_slow_plugin_pattern_fails_closed_warns_once_and_never_breaks_the_plugin(store, pattern, hostile, caplog):
    # given
    runtime = slow_runtime(store, pattern)
    granted: list = []
    redact.use_rules(lambda: tuple(spied(owner, real, granted) for owner, real in runtime.redaction_rules()), runtime.redaction_timed_out)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        first = bounded(lambda: redact.redact(hostile), 10.0)
        rest = bounded(lambda: [redact.redact(hostile) for _ in range(3)], 10.0)
        after = runtime.registry.get('slow')

    # then
    assert first == redact.TIMEOUT_MARK == '[redacted: a redaction pattern timed out]' and rest == [redact.TIMEOUT_MARK] * 3
    assert granted == [('r0', pytest.approx(redact.BUDGET_S))] * 4
    assert after.state == 'ok' and [p.id for p in runtime.active()] == ['slow']
    warned = timeout_warnings(caplog)
    assert len(warned) == 1 and warned[0].startswith('plug-in slow: redaction r0 ') and hostile not in warned[0]


@pytest.mark.parametrize('pattern, hostile', [('(?i)(a+A)+$', 'a' * 40 + '!'), (r'(a+\x61)+$', 'a' * 40 + '!')])
def test_hostile_flag_and_escape_patterns_never_hang(store, pattern, hostile):
    # given
    runtime = slow_runtime(store, pattern)
    granted: list = []
    redact.use_rules(lambda: tuple(spied(owner, real, granted) for owner, real in runtime.redaction_rules()), runtime.redaction_timed_out)

    # when
    out = bounded(lambda: redact.redact(hostile), 10.0)

    # then
    assert out in (hostile, redact.TIMEOUT_MARK)
    assert granted == [('r0', pytest.approx(redact.BUDGET_S))]


def test_a_pattern_only_the_regex_module_knows_loads_and_masks(store):
    # given
    runtime = slow_runtime(store, r'\p{Lu}{3}-\d+')

    # when
    out = redact.redact('ticket ABC-12 is open')

    # then
    assert runtime.registry.get('slow').state == 'ok' and out == f'ticket {redact.REDACTED} is open'


def test_a_timeout_sink_that_fails_still_gives_the_marker_and_logs_only_the_type(caplog):
    # given
    def sink(owner):
        raise RuntimeError('sink down at 10.0.0.7')

    redact.use_rules(((('slow', 'r0'), regex.compile(r'(a|aa)+$')),), sink)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.redact'):
        out = bounded(lambda: redact.redact('a' * 60 + '!'))

    # then
    assert out == redact.TIMEOUT_MARK
    assert 'a redaction timeout could not be recorded (RuntimeError)' in caplog.text and '10.0.0.7' not in caplog.text


def test_ip_uuid_and_email_patterns_redact_through_regex(store):
    # given
    slow_runtime(store, r'(?:\d{1,3}\.){3}\d{1,3}', r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', r'[\w.+-]+@(?:[\w-]+\.)+\w+')

    # when
    out = redact.redact('host 10.1.2.3 id 123e4567-e89b-12d3-a456-426614174000 mail carol.x@mail.example.net end')

    # then
    assert out == f'host {redact.REDACTED} id {redact.REDACTED} mail {redact.REDACTED} end'


def test_a_backreference_pattern_loads_and_redacts(store):
    # given
    slow_runtime(store, r'(x)\1+y')

    # when
    out = redact.redact('see xxxy now')

    # then
    assert out == f'see {redact.REDACTED} now'


def spied(owner: tuple[str, str], real: regex.Pattern, granted: list):
    pattern = mock.Mock(spec=regex.Pattern)

    def sub(repl, text, timeout):
        granted.append((owner[1], timeout))
        return real.sub(repl, text, timeout=timeout)

    pattern.sub.side_effect = sub
    return owner, pattern


def test_two_slow_rules_share_one_budget_per_call(store):
    # given
    runtime = slow_runtime(store, r'(a+){1,30}$', r'(a|aa)+$')
    granted: list = []
    blamed: list = []
    redact.use_rules(tuple(spied(owner, real, granted) for owner, real in runtime.redaction_rules()), blamed.append)
    hostile = 'a' * 60 + '!'

    # when
    out = redact.redact(hostile)

    # then
    assert out == redact.TIMEOUT_MARK
    assert granted == [('r0', pytest.approx(redact.BUDGET_S))] and blamed == [('slow', 'r0')]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def fake_clock(monkeypatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(redact, 'time', SimpleNamespace(monotonic=clock.monotonic))
    return clock


def timed_rule(clock: FakeClock, seen: list, name: str, cost: float, overrun: float = 0.0, plugin_id: str = 'timed'):
    pattern = mock.Mock(spec=regex.Pattern)

    def sub(repl, text, timeout):
        seen.append((name, timeout))
        if cost > timeout:
            clock.now += timeout
            raise TimeoutError('regular expression timed out')
        clock.now += cost + overrun
        return text.replace(name, redact.REDACTED)

    pattern.sub.side_effect = sub
    return (plugin_id, name), pattern


def test_two_rules_that_fit_the_budget_alone_but_not_together_give_the_marker_within_the_budget(monkeypatch):
    # given
    clock = fake_clock(monkeypatch)
    seen: list = []
    first, second = timed_rule(clock, seen, 'first', 0.06), timed_rule(clock, seen, 'second', 0.06)
    alone = []
    for rule in (first, second):
        redact.use_rules((rule,))
        alone.append(redact.redact('see first and second'))
    seen.clear()
    clock.now = 0.0
    redact.use_rules((first, second))

    # when
    out = redact.redact('see first and second')

    # then
    assert alone == [f'see {redact.REDACTED} and second', f'see first and {redact.REDACTED}']
    assert out == redact.TIMEOUT_MARK and clock.now == pytest.approx(redact.BUDGET_S)
    assert seen == [('first', pytest.approx(redact.BUDGET_S)), ('second', pytest.approx(redact.BUDGET_S - 0.06))]


def test_each_rule_gets_the_time_left_never_a_fresh_budget(monkeypatch):
    # given
    clock = fake_clock(monkeypatch)
    seen: list = []
    redact.use_rules(tuple(timed_rule(clock, seen, name, 0.03) for name in ('one', 'two', 'three')))

    # when
    out = redact.redact('one two three')

    # then
    assert out == ' '.join([redact.REDACTED] * 3)
    assert [t for _, t in seen] == [pytest.approx(0.1), pytest.approx(0.07), pytest.approx(0.04)]


@pytest.mark.parametrize('overrun', [0.0, 0.01])
def test_with_the_budget_spent_no_rule_runs_and_regex_never_gets_a_timeout_of_zero_or_less(monkeypatch, overrun):
    # given
    clock = fake_clock(monkeypatch)
    seen: list = []
    blamed: list = []
    spender = timed_rule(clock, seen, 'spender', redact.BUDGET_S, overrun=overrun)
    after = timed_rule(clock, seen, 'after', 0.0)
    redact.use_rules((spender, after), blamed.append)

    # when
    out = redact.redact('spender after')

    # then
    assert out == redact.TIMEOUT_MARK
    assert [name for name, _ in seen] == ['spender'] and all(t > 0 for _, t in seen)
    assert after[1].sub.call_count == 0 and blamed == [('timed', 'spender')]


def test_the_timeout_warning_names_the_slowest_rule_not_the_last_one_to_run(store, monkeypatch, caplog):
    # given
    clock = fake_clock(monkeypatch)
    seen: list = []
    runtime = runtime_of(store)
    heavy = timed_rule(clock, seen, 'heavy', 0.08, plugin_id='greedy')
    light = timed_rule(clock, seen, 'light', 0.05, plugin_id='honest')
    redact.use_rules((heavy, light), runtime.redaction_timed_out)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        out = redact.redact('heavy light')

    # then
    assert out == redact.TIMEOUT_MARK and seen == [('heavy', pytest.approx(0.1)), ('light', pytest.approx(0.02))]
    warned = timeout_warnings(caplog)
    assert len(warned) == 1 and warned[0].startswith('plug-in greedy: redaction heavy ') and 'honest' not in warned[0]


def test_the_timeout_warning_names_the_slowest_rule_when_it_ran_last(store, monkeypatch, caplog):
    # given
    clock = fake_clock(monkeypatch)
    seen: list = []
    runtime = runtime_of(store)
    light = timed_rule(clock, seen, 'light', 0.02, plugin_id='honest')
    heavy = timed_rule(clock, seen, 'heavy', 0.09, plugin_id='greedy')
    redact.use_rules((light, heavy), runtime.redaction_timed_out)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        out = redact.redact('light heavy')

    # then
    assert out == redact.TIMEOUT_MARK and seen == [('light', pytest.approx(0.1)), ('heavy', pytest.approx(0.08))]
    warned = timeout_warnings(caplog)
    assert warned == ['plug-in greedy: redaction heavy was the slowest rule when the redaction budget ran out (count 1)']


def test_lines_redacted_together_share_one_budget_and_the_rest_get_the_marker(monkeypatch):
    # given
    clock = fake_clock(monkeypatch)
    seen: list = []
    redact.use_rules((timed_rule(clock, seen, 'line', 0.03),))

    # when
    out = redact.redact_lines([f'line {i}' for i in range(6)])

    # then
    assert out == [f'{redact.REDACTED} {i}' for i in range(3)] + [redact.TIMEOUT_MARK] * 3
    assert len(seen) == 4 and clock.now == pytest.approx(0.1)


def test_a_log_read_redacts_its_lines_with_one_budget(monkeypatch, tmp_path):
    # given
    clock = fake_clock(monkeypatch)
    seen: list = []
    redact.use_rules((timed_rule(clock, seen, 'line', 0.03),))
    (tmp_path / 'token').write_text('sa-token\n')
    cluster = Kube(root=tmp_path, base='https://api.test', transport=lambda url, headers, timeout: '\n'.join(f'line {i}' for i in range(50)).encode())

    # when
    out = cluster.previous_logs('shop', 'p', None, 50)

    # then
    assert out.splitlines() == [f'{redact.REDACTED} {i}' for i in range(3)] + [redact.TIMEOUT_MARK] * 47
    assert len(seen) == 4


def test_a_slack_draft_redacts_its_title_and_lines_with_one_budget(monkeypatch):
    # given
    clock = fake_clock(monkeypatch)
    seen: list = []
    redact.use_rules((timed_rule(clock, seen, 'line', 0.04),))
    proposal = {'key': 'k', 'title': 'line title', 'description': 'line one\nline two\nline three', 'points': 1}

    # when
    blocks = answer_blocks('answer', proposal, 'a1')

    # then
    assert blocks[1]['text']['text'] == f'*Task draft:* {redact.REDACTED} title\n> {redact.REDACTED} one\n> {redact.TIMEOUT_MARK}\n> {redact.TIMEOUT_MARK}'
    assert len(seen) == 3


def test_two_plugins_with_the_same_pattern_text_are_counted_apart(store, caplog):
    # given
    alpha = dataclasses.replace(minimal('alpha', 'Alpha'), redactions=(Redaction('r0', r'(a|aa)+$'),))
    beta = dataclasses.replace(minimal('beta', 'Beta'), redactions=(Redaction('r0', r'(a|aa)+$'),))
    runtime = runtime_of(store, alpha, beta)
    redact.use_rules(runtime.redaction_rules, runtime.redaction_timed_out)
    hostile = 'a' * 60 + '!'
    rules = runtime.redaction_rules()

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        both_on = redact.redact(hostile)
        first = timeout_warnings(caplog)
        runtime.state.set('alpha', False, 'alice')
        runtime.refresh()
        beta_only = redact.redact(hostile)
        second = timeout_warnings(caplog)[len(first):]

    # then
    assert rules[0][1] is rules[1][1] and [owner for owner, _ in rules] == [('alpha', 'r0'), ('beta', 'r0')]
    assert both_on == beta_only == redact.TIMEOUT_MARK
    assert len(first) == 1 and first[0].startswith('plug-in alpha: redaction r0 ')
    assert len(second) == 1 and second[0].startswith('plug-in beta: redaction r0 ')


def test_timeout_warnings_come_on_the_first_time_and_then_every_hundredth(store, caplog):
    # given
    runtime = runtime_of(store)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        for _ in range(250):
            runtime.redaction_timed_out(('alpha', 'r0'))
        runtime.redaction_timed_out(('beta', 'r0'))
        runtime.redaction_timed_out(None)

    # then
    warned = timeout_warnings(caplog)
    assert len(warned) == 4
    assert [m.startswith('plug-in alpha: redaction r0 ') for m in warned] == [True, True, True, False] and warned[3].startswith('plug-in beta: redaction r0 ')
    assert [m[m.index('(count'):] for m in warned] == ['(count 1)', '(count 100)', '(count 200)', '(count 1)']


def test_masks_keep_working_after_many_hostile_calls_and_after_a_refresh(store):
    # given
    plugin = dataclasses.replace(minimal('slow', 'Slow'), redactions=(Redaction('r0', r'(a|aa)+$'), Redaction('secret', r'SEC-\d{4}')))
    hostile = 'a' * 60 + '!'

    # when
    with plugin_tag_app(store, plugin) as client:
        outs = bounded(lambda: [redact.redact(hostile) for _ in range(5)])
        masked = redact.redact('token SEC-1234')
        client.app.state.runtime.refresh()
        refreshed = redact.redact('token SEC-1234')
        state = client.app.state.registry.get('slow').state
        active = [p.id for p in client.app.state.runtime.active()]

    # then
    assert outs == [redact.TIMEOUT_MARK] * 5
    assert masked == refreshed == f'token {redact.REDACTED}'
    assert state == 'ok' and active == ['slow']


def test_core_patterns_still_run_on_stdlib_re():
    # then
    import re
    assert isinstance(redact.EMAIL, re.Pattern) and redact.redact('mail alice@example.com') == f'mail {redact.REDACTED}'


def test_redact_skips_zero_length_matches(store):
    # given
    plugin = dataclasses.replace(minimal('edges', 'Edges'), redactions=(Redaction('edge', r'\b'), Redaction('look', r'(?=DEMO)'), Redaction('real', r'DEMO-\d{4}')))
    runtime = runtime_of(store, plugin)
    redact.use_rules(runtime.redaction_rules)

    # when
    out = redact.redact('word DEMO-1234 more words')

    # then
    assert out == f'word {redact.REDACTED} more words'


def test_validated_items_are_checked_once_per_refresh(store, monkeypatch):
    # given
    calls = []
    import skopax.runtime as rt
    real = rt.problem
    monkeypatch.setattr(rt, 'problem', lambda kind, item: calls.append(kind) or real(kind, item))
    plugin = dataclasses.replace(minimal('rx', 'Rx'), redactions=(Redaction('a', r'AAA-\d'), Redaction('b', r'BBB-\d')))
    runtime = runtime_of(store, plugin)
    calls.clear()

    # when
    for _ in range(5):
        rule_patterns(runtime)
    quiet = len(calls)
    runtime.refresh()
    rule_patterns(runtime)
    rule_patterns(runtime)

    # then
    assert quiet == 2 and len(calls) == 4


def test_tag_contract_accepts_plugin_tags_and_core_has_neutral_labels():
    # given
    ok = dataclasses.replace(minimal('tagged', 'Tagged'), tags=(Tag('ops-team', 'Operations'),))

    # then
    assert Registry.of(ok).loaded()[0].state == 'ok'
    assert Tag('a', 'B') == Tag('a', 'B') and minimal('x', 'X').tags == ()


@pytest.mark.parametrize('bad', [('ops',), (Tag('Ops', 'Ops'),), (Tag('', 'Ops'),), (Tag('x' * 31, 'Ops'),), (Tag('ops', 5),), (Tag('ops', '   '),), (Tag(5, 'Ops'),), (Tag('o_ps', 'Ops'),)])
def test_a_malformed_tag_breaks_its_plugin_at_load(bad):
    # given
    plugin = dataclasses.replace(minimal('tagged', 'Tagged'), tags=bad)

    # then
    found = Registry.of(plugin).loaded()[0]
    assert found.state == 'broken' and 'tag' in found.reason


def test_plugin_tags_join_the_core_ones_live_and_leave_with_the_plugin(store):
    # given
    plugin = dataclasses.replace(minimal('tagged', 'Tagged'), tags=(Tag('ops-team', '  Operations   Team  ' + 'x' * 60),), tag_rules=(TagRule('ops-team', ('type:ops',)),))
    card = newsletter('ops')
    card['sig'] = 'ops'

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(plugin))) as client:
        ids = [t['id'] for t in tags.info()]
        label = tags.label('ops-team')
        entry = {e['key'] for e in client.get('/api/admin/settings', headers=AUTH).json()['items']}
        client.app.state.server_apply([PassResult('prom', True, 'ok', [card], {}, ('prom',))], NOW, None)
        on = card_tags(store, card)
        added = tags.added({}, ['ops-team'], 'alice', '2026-11-14T00:00:00Z')
        client.app.state.plugin_state.set('tagged', False, 'alice')
        gone = [t['id'] for t in tags.info()]
        gone_entry = {e['key'] for e in client.get('/api/admin/settings', headers=AUTH).json()['items']}
        with pytest.raises(tags.Refused):
            tags.added({}, ['ops-team'], 'alice', '2026-11-14T00:00:00Z')

    # then
    assert 'ops-team' in ids and ids[:4] == list(tags.TAGS)[:4] and len(label) <= 40 and label.startswith('Operations Team')
    assert 'tags.ops-team.rules' in entry and on == ['ops-team'] and added['tags'][0]['id'] == 'ops-team'
    assert 'ops-team' not in gone and 'tags.ops-team.rules' not in gone_entry


def test_a_plugin_tag_that_is_core_or_already_taken_is_refused_with_one_warning(store, caplog):
    # given
    first = dataclasses.replace(minimal('aaa-one', 'One'), tags=(Tag('developer', 'Mine'), Tag('shared', 'First')))
    second = dataclasses.replace(minimal('bbb-two', 'Two'), tags=(Tag('shared', 'Second'), Tag('own', 'Own')))
    runtime = runtime_of(store, first, second)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        found = runtime.tag_defs()
        runtime.tag_defs()

    # then
    assert found == {'shared': 'First', 'own': 'Own'}
    messages = [r.getMessage() for r in caplog.records]
    assert len([m for m in messages if 'developer' in m]) == 1 and len([m for m in messages if 'shared' in m]) == 1


def test_a_plugin_tag_rule_for_a_plugin_tag_raises_no_unknown_tag_warning(store, caplog):
    # given
    plugin = dataclasses.replace(minimal('tagged', 'Tagged'), tags=(Tag('ops-team', 'Operations'),), tag_rules=(TagRule('ops-team', ('type:ops',)), TagRule('nope', ('x',))))
    runtime = runtime_of(store, plugin)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        runtime.tag_rules()

    # then
    assert [r.getMessage() for r in caplog.records if 'ops-team' in r.getMessage()] == [] and len([r for r in caplog.records if 'nope' in r.getMessage()]) == 1


def test_the_panel_script_none_is_valid_and_core_default_rules_stay_empty():
    # then
    assert Registry.of(dataclasses.replace(minimal('quiet', 'Quiet'), panels=(Panel('pt', None, 'a.html'),))).loaded()[0].state == 'ok'
    assert tunables.default_of(next(t for t in tunables.registry() if t.kind == 'tagrules' and t.part == 'analyst')) == ''


def apply_card(client, name: str, sig: str, at: float):
    found = newsletter(name)
    found['sig'] = sig
    client.app.state.server_apply([PassResult('prom', True, 'ok', [found], {}, ('prom',))], at, None)
    return found


def test_an_admin_edit_of_another_tag_does_not_freeze_a_plugin_rule(store):
    # given
    plugin = dataclasses.replace(minimal('tagger', 'Tagger'), tag_rules=(TagRule('marketing', ('type:newsletter',)),))

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(plugin))) as client:
        saved = client.put('/api/admin/settings', json={'key': 'tags.seo.rules', 'value': 'gone, type:crux, type:other'}, headers=AUTH)
        on = card_tags(store, apply_card(client, 'one', 'newsletter', NOW))
        client.app.state.plugin_state.set('tagger', False, 'alice')
        client.app.state.runtime.refresh()
        off = card_tags(store, apply_card(client, 'two', 'newsletter', NOW + 60))

    # then
    assert saved.status_code == 200 and on == ['marketing'] and off == []


def plugin_tag_app(store, plugin):
    return TestClient(create_app(settings(), store, registry=registry_with(plugin)))


def test_an_admin_rule_for_a_plugin_tag_works_while_on_stops_after_switch_off_and_survives_a_restart(store):
    # given
    plugin = dataclasses.replace(minimal('tagged', 'Tagged'), tags=(Tag('ops-team', 'Operations'),))

    # when
    with plugin_tag_app(store, plugin) as client:
        client.put('/api/admin/settings', json={'key': 'tags.ops-team.rules', 'value': 'type:zeta'}, headers=AUTH)
        on = card_tags(store, apply_card(client, 'first', 'zeta', NOW))
        client.app.state.plugin_state.set('tagged', False, 'alice')
        client.app.state.runtime.refresh()
        off = card_tags(store, apply_card(client, 'second', 'zeta', NOW + 60))
        client.app.state.plugin_state.set('tagged', True, 'alice')
    with plugin_tag_app(store, plugin) as client:
        back = card_tags(store, apply_card(client, 'third', 'zeta', NOW + 120))
        shown = {e['key']: e for e in client.get('/api/admin/settings', headers=AUTH).json()['items']}['tags.ops-team.rules']['value']

    # then
    assert on == ['ops-team'] and off == [] and back == ['ops-team'] and shown == 'type:zeta'


def fielded_tag_plugin():
    return dataclasses.replace(minimal('fielded', 'Fielded'), fields=(Field('url', 'URL', 'url', required=True),), tags=(Tag('ops-team', 'Operations'),))


def test_an_admin_rule_for_a_tag_of_a_plugin_with_instance_fields_survives_a_restart(store):
    # given
    plugin = fielded_tag_plugin()
    add_instance(store, plugin, 'main', url='https://ops.example.com')
    with plugin_tag_app(store, plugin) as client:
        saved = client.put('/api/admin/settings', json={'key': 'tags.ops-team.rules', 'value': 'type:zeta'}, headers=AUTH)
        before = card_tags(store, apply_card(client, 'first', 'zeta', NOW))

    # when
    with plugin_tag_app(store, plugin) as client:
        after = card_tags(store, apply_card(client, 'second', 'zeta', NOW + 60))
        held = dict(client.app.state.runtime.live.current().tag_rules).get('ops-team')
        shown = {e['key']: e for e in client.get('/api/admin/settings', headers=AUTH).json()['items']}['tags.ops-team.rules']

    # then
    assert saved.status_code == 200 and before == ['ops-team']
    assert after == ['ops-team'] and held == ('type:zeta',)
    assert shown['value'] == 'type:zeta' and shown['source'] == 'admin'


def test_a_stored_rule_for_a_tag_not_known_yet_is_kept_and_applies_once_the_tag_is_known():
    # given
    tags.use_tags({})
    overrides = {'tags.ops-team.rules': 'type:zeta', 'tags.data.rules': 'type:db', 'tags.Not A Tag.rules': 'type:x'}

    # when
    applied = dict(tunables.apply(settings(), overrides).tag_rules)
    unknown = dict(tags.rules(tuple(applied.items())))
    tags.use_tags({'ops-team': 'Operations'})
    known = dict(tags.rules(tuple(applied.items())))

    # then
    assert applied['ops-team'] == ('type:zeta',) and applied['data'] == ('type:db',) and 'Not A Tag' not in applied
    assert 'ops-team' not in unknown and unknown['data'] == ('type:db',)
    assert known['ops-team'] == ('type:zeta',) and known['data'] == ('type:db',)


def test_a_too_long_stored_rule_for_a_tag_not_known_yet_is_skipped_with_a_warning(caplog):
    # given
    tags.use_tags({})

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tunables'):
        applied = tunables.apply(settings(), {'tags.ops-team.rules': 'x' * (tunables.TERM_LIMIT + 1)})

    # then
    assert applied.tag_rules == () and any('tags.ops-team.rules' in r.getMessage() for r in caplog.records)


def test_stored_rules_for_an_unknown_tag_are_kept_but_never_applied():
    # given
    tags.use_rules({})
    given = (('marketing', ('type:a',)), ('gone-tag', ('type:b',)))

    # when
    found = dict(tags.rules(given))

    # then
    assert found == {'marketing': ('type:a',)}


def test_two_redactions_with_the_same_id_both_apply(store):
    # given
    plugin = dataclasses.replace(minimal('rx', 'Rx'), redactions=(Redaction('r', r'AAA-\d+'), Redaction('r', r'BBB-\d+')))
    runtime = runtime_of(store, plugin)
    redact.use_rules(runtime.redaction_rules)

    # when
    out = redact.redact('see AAA-1 and BBB-2')

    # then
    assert [p.pattern for p in rule_patterns(runtime)] == [r'AAA-\d+', r'BBB-\d+'] and out == f'see {redact.REDACTED} and {redact.REDACTED}'


def test_the_app_wires_redaction_timeouts_to_the_runtime(store, caplog):
    # given
    plugin = dataclasses.replace(minimal('slow', 'Slow'), redactions=(Redaction('r0', r'(a|aa)+$'),))
    hostile = 'a' * 60 + '!'

    # when
    with plugin_tag_app(store, plugin) as client, caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        outs = bounded(lambda: [redact.redact(hostile) for _ in range(3)])
        state = client.app.state.registry.get('slow').state

    # then
    warned = timeout_warnings(caplog)
    assert outs == [redact.TIMEOUT_MARK] * 3 and state == 'ok'
    assert len(warned) == 1 and warned[0].startswith('plug-in slow: redaction r0 ') and hostile not in warned[0]


def test_core_rule_time_never_spends_the_plugin_rule_budget(monkeypatch):
    # given
    clock = fake_clock(monkeypatch)
    seen: list = []
    redact.use_rules((timed_rule(clock, seen, 'line', 0.001),))
    real = redact._core

    def slow_core(text, cards=True):
        clock.now += 0.05
        return real(text, cards)

    monkeypatch.setattr(redact, '_core', slow_core)

    # when
    out = redact.redact_lines([f'line {i}' for i in range(10)])

    # then
    assert out == [f'{redact.REDACTED} {i}' for i in range(10)] and len(seen) == 10


def test_long_benign_log_lines_with_a_fast_plugin_rule_never_get_the_marker(tmp_path):
    # given
    redact.use_rules(((('demo', 'ticket-id'), regex.compile(r'DEMO-\d{4}')),))
    (tmp_path / 'token').write_text('sa-token\n')
    line = 'at shop.checkout.Service.handle(Service.java:42) ' * 330
    cluster = Kube(root=tmp_path, base='https://api.test', transport=lambda url, headers, timeout: '\n'.join([line] * 99 + ['ticket DEMO-1234 failed']).encode())

    # when
    out = bounded(lambda: cluster.previous_logs('shop', 'p', None, 100))

    # then
    assert redact.TIMEOUT_MARK not in out and 'DEMO-1234' not in out and f'ticket {redact.REDACTED} failed' in out
