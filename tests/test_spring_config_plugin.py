import base64
import dataclasses
import json
import logging

import pytest

from skopax import bounded, remote
from skopax.config import ConfigurationError
from skopax.contract import Capabilities, Field, Hooks, SourceData
from skopax.plugins import spring_config
from skopax.plugins.spring_config import (BadAnswer, BadMap, BadSetup, EmptyAnswer, SpringConfig, SpringConfigError, TimedOut, Unauthorized, Unreachable, Refused, flatten,
                                          map_of, resolve_value)
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply
from skopax.settingsources import SourceSet, _hook_data, boot, field_kinds, light_boot

URL = 'https://config.example.com'
USER = 'cfg-user'
PASSWORD = 'pw-s3cr3t-value'
SECRET = 'tok-s3cr3t-value'
BASE = {'SPRING_CONFIG_URL': URL, 'SPRING_CONFIG_APP': 'shop', 'SPRING_CONFIG_PROFILE': 'prod', 'SPRING_CONFIG_LABEL': 'main', 'SPRING_CONFIG_USER': USER,
        'SPRING_CONFIG_PASSWORD': PASSWORD,
        'SPRING_CONFIG_MAP': 'investigate.max-cards=investigate.max-cards,jira.url=jira/main/url,jira.token=jira/main/api-token,tz=core.timezone'}
ANSWER = {
    'name': 'shop', 'profiles': ['prod'], 'label': 'main', 'version': 'a1b2c3', 'state': None,
    'propertySources': [
        {'name': 'https://git.example.com/cfg/shop-prod.yml', 'source': {'investigate.max-cards': '${cards:7}', 'jira.url': 'https://${jira.host}', 'jira.host': 'jira.example.com',
                                                                         'jira.token': SECRET, 'tz': 'Europe/Berlin'}},
        {'name': 'https://git.example.com/cfg/application.yml', 'source': {'jira.host': 'old.example.com', 'cards': 9, 'tz': 'UTC', 'flag': True}},
    ],
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Server:
    def __init__(self, answer=None):
        self.answer = ANSWER if answer is None else answer
        self.calls: list[tuple] = []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return Reply(self.answer, {})


SECRET_FIELDS = ('scan-headers', 'headers', 'token')


def fake_kind(plugin_id, key):
    return 'secret' if key in SECRET_FIELDS else 'text'


class Told(SpringConfig):
    def __call__(self, env, field_kind=fake_kind):
        return super().__call__(env, field_kind)


def source(server=None, clock=None) -> SpringConfig:
    return Told(server if server is not None else Server(), clock if clock is not None else Clock())


def plain(server=None, clock=None) -> SpringConfig:
    return SpringConfig(server if server is not None else Server(), clock if clock is not None else Clock())


def get(env=None, answer=None) -> dict:
    return source(Server(answer))({**BASE, **(env or {})})


def test_the_plugin_is_built_in_loads_and_offers_only_the_settings_source_hook():
    # when
    found = Registry.load({}).get('spring-config')

    # then
    assert 'skopax.plugins.spring_config' in BUILTIN
    assert found is not None and found.state == 'ok' and found.plugin is spring_config.PLUGIN
    assert describe(spring_config.PLUGIN, icon=True)['hooks'] == ['settings_source']
    assert spring_config.PLUGIN.fields == () and spring_config.PLUGIN.checks == ()


def test_without_a_url_in_the_environment_the_hook_is_inert_and_asks_nothing():
    # given
    server = Server()

    # when
    out = source(server)({'SPRING_CONFIG_APP': 'shop'})

    # then
    assert out is None and server.calls == []


def test_it_reads_url_app_profile_label_with_basic_auth_under_one_short_deadline():
    # given
    server = Server()

    # when
    source(server)(BASE)

    # then
    method, url, headers, timeout = server.calls[0]
    assert (method, url) == ('GET', f'{URL}/shop/prod/main')
    assert headers['Authorization'] == 'Basic ' + base64.b64encode(f'{USER}:{PASSWORD}'.encode()).decode()
    assert len(server.calls) == 1 and timeout * remote.PAGE_DEADLINE < bounded.HOOK_TIMEOUT_S


def test_a_slash_in_the_label_is_sent_the_spring_way_and_a_missing_label_is_left_out():
    # given
    server = Server()

    # when
    source(server)({**BASE, 'SPRING_CONFIG_LABEL': 'feature/x y'})
    source(server)({k: v for k, v in BASE.items() if k != 'SPRING_CONFIG_LABEL'})

    # then
    assert [c[1] for c in server.calls] == [f'{URL}/shop/prod/feature(_)x%20y', f'{URL}/shop/prod']


def test_the_profile_defaults_and_a_trailing_slash_on_the_url_is_dropped():
    # given
    server = Server()

    # when
    source(server)({**BASE, 'SPRING_CONFIG_URL': URL + '/', 'SPRING_CONFIG_PROFILE': ''})

    # then
    assert server.calls[0][1] == f'{URL}/shop/default/main'


def test_no_credentials_means_no_authorization_header():
    # given
    server = Server()

    # when
    source(server)({k: v for k, v in BASE.items() if k not in ('SPRING_CONFIG_USER', 'SPRING_CONFIG_PASSWORD')})

    # then
    assert 'Authorization' not in server.calls[0][2]


def test_the_first_property_source_wins_and_the_map_gives_settings_and_instance_fields():
    # when
    out = get()

    # then
    assert out['values'] == {'investigate.max-cards': '9', 'core.timezone': 'Europe/Berlin'}
    assert out['instances'] == [{'plugin': 'jira', 'name': 'main', 'url': 'https://jira.example.com', 'api-token': SECRET}]


def test_flatten_keeps_the_first_source_and_scalars_only():
    # given
    sources = [{'source': {'a': '1', 'n': None, 'deep': {'x': 1}}}, {'source': {'a': '2', 'b': 3, 'c': False, 'l': [1]}}, 'junk', {'source': 'junk'}, {}]

    # when
    found = flatten(sources)

    # then
    assert found == {'a': '1', 'b': '3', 'c': 'false'}


def test_placeholders_resolve_from_the_config_then_the_environment_then_the_default():
    # given
    config = {'host': 'cfg-host', 'port': '${PORT_ENV}', 'indirect': '${host}:${port}', 'nested': '${missing:${host}}'}
    env = {'PORT_ENV': '8080', 'FROM_ENV': 'e', 'MY_APP_NAME': 'relaxed', 'host': 'env-must-lose'}

    # when
    out = [resolve_value(text, config, env) for text in ('${host}', '${FROM_ENV}', '${absent:dflt}', '${absent:}', '${indirect}', '${nested}', '${my.app-name}', 'a-${host}-b-${FROM_ENV}', 'plain')]

    # then
    assert out == ['cfg-host', 'e', 'dflt', '', 'cfg-host:8080', 'cfg-host', 'relaxed', 'a-cfg-host-b-e', 'plain']


@pytest.mark.parametrize('config', [{'a': '${b}', 'b': '${a}'}, {'a': '${a}'}, {'a': '${b}', 'b': '${c}', 'c': '${a:fallback}'}, {'a': '${nope}'}])
def test_a_loop_or_an_unresolved_key_is_not_resolved(config):
    # then
    with pytest.raises(spring_config.Unresolved):
        resolve_value('${a}', config, {})


def test_an_exponential_expansion_stops_at_a_bound():
    # given
    config = {f'k{i}': f'${{k{i + 1}}}${{k{i + 1}}}' for i in range(40)} | {'k40': 'x'}

    # then
    with pytest.raises(spring_config.Unresolved):
        resolve_value('${k0}', config, {})


def test_an_unresolved_or_looping_value_is_left_out_with_a_warning_that_names_keys_and_no_values(caplog):
    # given
    answer = {'propertySources': [{'name': 'x', 'source': {'investigate.max-cards': '${loop-a}', 'loop-a': '${loop-b}', 'loop-b': '${loop-a}', 'jira.url': '${gone}',
                                                          'jira.token': SECRET, 'tz': 'Europe/Berlin'}}]}

    # when
    with caplog.at_level(logging.WARNING):
        out = get(answer=answer)

    # then
    assert out['values'] == {'core.timezone': 'Europe/Berlin'} and out['instances'] == [{'plugin': 'jira', 'name': 'main', 'api-token': SECRET}]
    text = caplog.text
    assert 'investigate.max-cards' in text and 'jira.url' in text
    assert SECRET not in text and 'Europe/Berlin' not in text and PASSWORD not in text


def test_a_mapped_key_the_server_does_not_have_is_left_out_with_a_warning(caplog):
    # given
    answer = {'propertySources': [{'name': 'x', 'source': {'tz': 'UTC'}}]}

    # when
    with caplog.at_level(logging.WARNING):
        out = get(answer=answer)

    # then
    assert out['values'] == {'core.timezone': 'UTC'} and out['instances'] == []
    assert 'jira.url' in caplog.text


def test_a_secret_value_and_the_password_never_reach_the_log_or_an_error_text(caplog):
    # given
    server = Server(HttpError(401, f'{URL}/shop/prod/main', f'bad credentials {PASSWORD} {SECRET}'))

    # when
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Unauthorized) as raised:
            source(server)(BASE)
        get()

    # then
    assert PASSWORD not in caplog.text and SECRET not in caplog.text
    assert PASSWORD not in str(raised.value) and SECRET not in str(raised.value) and PASSWORD not in repr(raised.value)


@pytest.mark.parametrize('failure, expected', [
    (HttpError(401, 'x'), Unauthorized), (HttpError(403, 'x'), Unauthorized), (HttpError(404, 'x'), Refused), (HttpError(500, 'x'), Refused),
    (remote.TimedOut('x'), TimedOut), (remote.Unreachable('x'), Unreachable), (remote.NotJson('x'), BadAnswer),
])
def test_each_failure_raises_its_own_error_class_so_the_log_names_the_cause(failure, expected):
    # then
    with pytest.raises(expected):
        source(Server(failure))(BASE)
    assert issubclass(expected, SpringConfigError)


@pytest.mark.parametrize('answer, expected', [
    ({'propertySources': []}, EmptyAnswer), ({'name': 'shop'}, BadAnswer), ([], BadAnswer), ('text', BadAnswer), ({'propertySources': 'x'}, BadAnswer),
    ({'propertySources': ['junk', {'source': 'junk'}]}, EmptyAnswer),
])
def test_an_empty_or_malformed_answer_is_a_failure_not_an_empty_config(answer, expected):
    # then
    with pytest.raises(expected):
        get(answer=answer)


@pytest.mark.parametrize('env, expected', [
    ({'SPRING_CONFIG_MAP': 'a=core.timezone,a=core.timezone'}, BadMap), ({'SPRING_CONFIG_MAP': 'a=core.timezone|core.timezone'}, BadMap),
    ({'SPRING_CONFIG_MAP': 'a=core.timezone|'}, BadMap), ({'SPRING_CONFIG_MAP': 'a=|core.timezone'}, BadMap), ({'SPRING_CONFIG_MAP': 'a=jira/main/url|b=jira/main/api-token'}, BadMap),
    ({'SPRING_CONFIG_MAP': 'a=core.timezone|jira/main/name'}, BadMap), ({'SPRING_CONFIG_MAP': 'a=core.timezone|jira.url'}, BadMap),
    ({'SPRING_CONFIG_MAP': 'a=core.timezone,b=core.timezone'}, BadMap),
    ({'SPRING_CONFIG_MAP': 'a=jira/main/url,b=jira/main/url'}, BadMap),
    ({'SPRING_CONFIG_MAP': 'a'}, BadMap), ({'SPRING_CONFIG_MAP': '=core.timezone'}, BadMap), ({'SPRING_CONFIG_MAP': 'a='}, BadMap),
    ({'SPRING_CONFIG_MAP': 'a=Not A Setting'}, BadMap), ({'SPRING_CONFIG_MAP': 'a=jira/Main/url'}, BadMap), ({'SPRING_CONFIG_MAP': 'a=jira/main'}, BadMap),
    ({'SPRING_CONFIG_MAP': ''}, BadSetup), ({'SPRING_CONFIG_APP': ''}, BadSetup), ({'SPRING_CONFIG_URL': 'ftp://x.example.com'}, BadSetup),
    ({'SPRING_CONFIG_URL': 'https://u:p@config.example.com'}, BadSetup), ({'SPRING_CONFIG_URL': 'https://config.example.com?token=x'}, BadSetup),
    ({'SPRING_CONFIG_USER': USER, 'SPRING_CONFIG_PASSWORD': ''}, BadSetup),
])
def test_a_wrong_map_or_setup_is_refused_before_any_request(env, expected):
    # given
    server = Server()

    # then
    with pytest.raises(expected):
        source(server)({**BASE, **env})
    assert server.calls == []


def test_map_of_reads_pairs_trims_blanks_and_keeps_targets_in_order():
    # when
    found = map_of(' a.b = core.timezone , c = jira/main/url ,, ')

    # then
    assert found == [('a.b', ('setting', 'core.timezone')), ('c', ('instance', 'jira', 'main', 'url'))]


def test_the_answer_is_kept_for_fifteen_minutes_and_read_again_after_that():
    # given
    server, clock = Server(), Clock()
    feed = source(server, clock)

    # when
    first = feed(BASE)
    clock.now += spring_config.REFETCH_S - 1
    feed(BASE)
    clock.now += 2
    again = feed(BASE)

    # then
    assert len(server.calls) == 2 and first == again and spring_config.REFETCH_S == 15 * 60


def test_a_failed_refetch_raises_keeps_the_last_good_copy_and_waits_before_trying_again():
    # given
    server, clock = Server(), Clock()
    feed = source(server, clock)
    good = feed(BASE)
    clock.now += spring_config.REFETCH_S
    server.answer = remote.TimedOut('x')

    # when
    with pytest.raises(TimedOut):
        feed(BASE)
    clock.now += spring_config.RETRY_S - 1
    with pytest.raises(TimedOut):
        feed(BASE)
    asked = len(server.calls)
    server.answer = ANSWER
    clock.now += 2
    back = feed(BASE)

    # then
    assert asked == 2 and len(server.calls) == 3 and back == good and feed.good == good


def test_a_first_failure_is_retried_only_after_the_retry_wait():
    # given
    server, clock = Server(remote.Unreachable('x')), Clock()
    feed = source(server, clock)

    # when
    for _ in range(3):
        with pytest.raises(Unreachable):
            feed(BASE)

    # then
    assert len(server.calls) == 1


def test_a_changed_setup_does_not_reuse_the_old_answer():
    # given
    server = Server()
    feed = source(server)

    # when
    feed(BASE)
    feed({**BASE, 'SPRING_CONFIG_PROFILE': 'qa'})

    # then
    assert [c[1] for c in server.calls] == [f'{URL}/shop/prod/main', f'{URL}/shop/qa/main']


def plugin_with(feed) -> object:
    return dataclasses.replace(spring_config.PLUGIN, hooks=Hooks(settings_source=feed))


def test_a_main_does_not_start_when_the_server_cannot_be_read():
    # given
    registry = Registry.of(plugin_with(plain(Server(HttpError(401, 'x')))))

    # when
    with pytest.raises(ConfigurationError) as raised:
        boot(dict(BASE), registry)

    # then
    assert 'Unauthorized' in str(raised.value) and PASSWORD not in str(raised.value)


def test_a_main_starts_from_the_server_and_the_values_reach_the_settings():
    # given
    registry = Registry.of(plugin_with(plain()))

    # when
    b = boot(dict(BASE), registry)

    # then
    assert b.settings.timezone == 'Europe/Berlin' and b.settings.investigate_max_cards == 9
    assert [d.label for d in b.sources if d.instances] == ['Spring Cloud Config']


def test_an_agent_starts_on_the_environment_alone_when_the_server_times_out_simulated():
    # given
    registry = Registry.of(plugin_with(plain(Server(remote.TimedOut('x')))))

    # when
    b = light_boot(dict(BASE), registry)

    # then
    assert b.failed == {'Spring Cloud Config': 'ConfigurationError'} and b.sources == []


def test_a_server_that_fails_later_leaves_the_applied_values_in_place():
    # given
    server, clock = Server(), Clock()
    registry = Registry.of(plugin_with(plain(server, clock)))
    b = boot(dict(BASE), registry)
    live = SourceSet(b.loaders, b.loaded, 60)
    before = [d.values for d in live.current()]
    clock.now += spring_config.REFETCH_S
    server.answer = HttpError(500, 'x')

    # when
    changed = live.refresh()

    # then
    assert changed is False and [d.values for d in live.current()] == before
    assert live.status()['Spring Cloud Config']['ok'] is False and live.status()['Spring Cloud Config']['error'] == 'ConfigurationError'


def test_the_map_loads_from_the_hook_output_as_a_valid_source():
    # when
    out = get()

    # then
    assert json.loads(json.dumps(out)) == out and set(out) == {'values', 'instances'}


def test_a_bad_setup_logs_the_variable_it_names_and_no_password(caplog):
    # given
    env = {**BASE, 'SPRING_CONFIG_PASSWORD': ''}

    # when
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BadSetup):
            source()(env)

    # then
    assert 'SPRING_CONFIG_PASSWORD' in caplog.text and USER not in caplog.text


def test_a_target_mapped_twice_logs_which_key_and_target(caplog):
    # when
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BadMap):
            source()({**BASE, 'SPRING_CONFIG_MAP': 'one.key=core.timezone,dup.key=general.x|core.timezone'})

    # then
    assert 'dup.key' in caplog.text and 'core.timezone' in caplog.text and 'already mapped' in caplog.text


def test_an_unresolved_placeholder_inside_a_secret_value_logs_the_key_and_no_part_of_the_value(caplog):
    # given
    answer = {'propertySources': [{'name': 'x', 'source': {'jira.token': 'ab${Hx7qPart}cd', 'tz': 'UTC'}}]}

    # when
    with caplog.at_level(logging.WARNING):
        get(answer=answer)

    # then
    assert 'jira.token' in caplog.text and 'Hx7qPart' not in caplog.text


@pytest.mark.parametrize('target', ['jira/main/plugin', 'jira/main/name', 'jira.url', 'jira.api-token', 'gitlab.token', 'gitlab.group', 'jira.email', 'jira.cloud-id', 'slack.bot-token', 'slack.app-token'])
def test_identity_fields_and_old_connection_keys_are_refused_as_targets(target):
    # then
    with pytest.raises(BadMap):
        map_of(f'a={target}')


def test_a_null_config_value_is_left_out_not_turned_into_an_empty_setting(caplog):
    # given
    answer = {'propertySources': [{'name': 'x', 'source': {'tz': None, 'investigate.max-cards': '3'}}]}

    # when
    with caplog.at_level(logging.WARNING):
        out = get(answer=answer)

    # then
    assert out['values'] == {'investigate.max-cards': '3'} and 'tz' in caplog.text


def test_an_unconfigured_install_has_no_settings_source_at_all():
    # when
    b = boot({}, Registry.of(spring_config.PLUGIN))

    # then
    assert 'Spring Cloud Config' not in [d.label for d in b.sources] and b.loaded['Spring Cloud Config'] is None


SHARED = '{"type": "service_account", "private_key": "pk-s3cr3t-value"}'
SHARED_ANSWER = {'propertySources': [{'name': 'x', 'source': {'google.sa-json': SHARED, 'google.api-key': 'ak-s3cr3t-value', 'pagescan.automation-secret': 'as-s3cr3t-value',
                                                             'pagescan.automation-id': 'bot-7', 'tz': 'UTC'}}]}
SHARED_MAP = 'google.sa-json=analytics/main/key|search/main/key, google.sa-json=sheets/main/key, google.api-key=maps/main/key|places/main/key'
TEMPLATES = 'website/shop/scan-headers = X-Automation-Secret: ${pagescan.automation-secret}; X-Automation-Id: ${pagescan.automation-id}, end\n\n  demo/one/token=${tz}=${tz}  \n'


def test_map_of_gives_one_entry_per_target_for_both_the_bar_and_the_repeated_key():
    # when
    found = map_of('k = a.b | jira/main/url , k = c.d, j=e.f')

    # then
    assert found == [('k', ('setting', 'a.b')), ('k', ('instance', 'jira', 'main', 'url')), ('k', ('setting', 'c.d')), ('j', ('setting', 'e.f'))]


def test_one_key_fills_every_target_it_is_mapped_to_with_the_same_value():
    # when
    out = get({'SPRING_CONFIG_MAP': SHARED_MAP}, SHARED_ANSWER)

    # then
    assert out['values'] == {}
    assert out['instances'] == [{'plugin': p, 'name': 'main', 'key': v} for p, v in
                                (('analytics', SHARED), ('search', SHARED), ('sheets', SHARED), ('maps', 'ak-s3cr3t-value'), ('places', 'ak-s3cr3t-value'))]


def test_a_shared_key_the_server_lacks_is_warned_once_and_no_target_gets_it(caplog):
    # given
    answer = {'propertySources': [{'name': 'x', 'source': {'tz': 'UTC'}}]}

    # when
    with caplog.at_level(logging.WARNING):
        out = get({'SPRING_CONFIG_MAP': SHARED_MAP + ',tz=core.timezone'}, answer)

    # then
    assert out['values'] == {'core.timezone': 'UTC'} and out['instances'] == []
    assert caplog.text.count('google.sa-json') == 1 and caplog.text.count('google.api-key') == 1


def test_a_rotated_shared_key_reaches_every_target_and_the_template_at_the_next_read():
    # given
    server, clock = Server(SHARED_ANSWER), Clock()
    feed = source(server, clock)
    env = {**BASE, 'SPRING_CONFIG_MAP': SHARED_MAP, 'SPRING_CONFIG_TEMPLATES': 'website/shop/scan-headers=S: ${pagescan.automation-secret}; K: ${google.api-key}'}
    before = feed(env)
    body = dict(SHARED_ANSWER['propertySources'][0]['source'], **{'google.sa-json': 'rotated-json', 'google.api-key': 'rotated-key'})
    server.answer = {'propertySources': [{'name': 'x', 'source': body}]}

    # when
    clock.now += spring_config.REFETCH_S
    after = feed(env)

    # then
    fields = {i['plugin']: i.get('key') or i.get('scan-headers') for i in after['instances']}
    assert before != after and len(server.calls) == 2
    assert fields == {'analytics': 'rotated-json', 'search': 'rotated-json', 'sheets': 'rotated-json', 'maps': 'rotated-key', 'places': 'rotated-key',
                      'website': 'S: as-s3cr3t-value; K: rotated-key'}


def test_a_rotated_shared_key_is_a_changed_source_for_the_refresh():
    # given
    server, clock = Server(SHARED_ANSWER), Clock()
    feed = source(server, clock)
    env = {**BASE, 'SPRING_CONFIG_MAP': SHARED_MAP}
    sources = SourceSet([('Spring Cloud Config', lambda: SourceData.of(feed(env), 'Spring Cloud Config'))], {'Spring Cloud Config': SourceData.of(feed(env), 'Spring Cloud Config')})
    body = dict(SHARED_ANSWER['propertySources'][0]['source'], **{'google.api-key': 'rotated-key'})

    # when
    unchanged = sources.refresh()
    server.answer = {'propertySources': [{'name': 'x', 'source': body}]}
    clock.now += spring_config.REFETCH_S
    changed = sources.refresh()

    # then
    keys = {i['plugin']: i['key'] for i in sources.current()[0].instances}
    assert unchanged is False and changed is True
    assert keys['maps'] == keys['places'] == 'rotated-key' and keys['sheets'] == SHARED


def test_a_template_fills_one_field_from_several_keys_with_commas_and_equals_kept():
    # when
    out = get({'SPRING_CONFIG_MAP': 'tz=core.timezone', 'SPRING_CONFIG_TEMPLATES': TEMPLATES}, SHARED_ANSWER)

    # then
    assert out['values'] == {'core.timezone': 'UTC'}
    assert out['instances'] == [{'plugin': 'website', 'name': 'shop', 'scan-headers': 'X-Automation-Secret: as-s3cr3t-value; X-Automation-Id: bot-7, end'},
                                {'plugin': 'demo', 'name': 'one', 'token': 'UTC=UTC'}]


def test_templates_alone_are_a_valid_setup():
    # when
    out = get({'SPRING_CONFIG_MAP': '', 'SPRING_CONFIG_TEMPLATES': 'demo/one/token=v${tz}'}, SHARED_ANSWER)

    # then
    assert out == {'values': {}, 'instances': [{'plugin': 'demo', 'name': 'one', 'token': 'vUTC'}]}


def test_a_template_reads_config_keys_only_never_the_environment_nor_a_default():
    # given
    env = {'SPRING_CONFIG_MAP': '', 'SPRING_CONFIG_TEMPLATES': 'demo/one/token=${ONLY_IN_ENV}\ndemo/two/token=$tz ${tz} $ {tz} }'}

    # when
    out = get({**env, 'ONLY_IN_ENV': 'env-value'}, SHARED_ANSWER)

    # then
    assert out['instances'] == [{'plugin': 'demo', 'name': 'two', 'token': '$tz UTC $ {tz} }'}]


def test_a_template_takes_the_raw_config_value_with_no_placeholder_filled_inside_it():
    # given
    answer = {'propertySources': [{'name': 'x', 'source': {'host': 'cfg-host', 'dir': '${HOME}/x', 'url': 'https://${host}/${PORT_ENV:80}'}}]}

    # when
    out = get({'SPRING_CONFIG_MAP': 'url=general.y', 'SPRING_CONFIG_TEMPLATES': 'demo/one/token=<${url}> ${dir}', 'PORT_ENV': '8080', 'HOME': '/home/someone'}, answer)

    # then
    assert out['values'] == {'general.y': 'https://cfg-host/8080'}
    assert out['instances'] == [{'plugin': 'demo', 'name': 'one', 'token': '<https://${host}/${PORT_ENV:80}> ${HOME}/x'}]


def test_a_template_with_a_missing_key_is_left_unset_and_the_warning_names_the_key_only(caplog):
    # given
    env = {'SPRING_CONFIG_MAP': 'tz=core.timezone', 'SPRING_CONFIG_TEMPLATES': 'website/shop/scan-headers=X-Automation-Secret: ${pagescan.automation-secret}; X-Id: ${pagescan.gone}'}

    # when
    with caplog.at_level(logging.WARNING):
        out = get(env, SHARED_ANSWER)

    # then
    assert out['values'] == {'core.timezone': 'UTC'} and out['instances'] == []
    assert 'pagescan.gone' in caplog.text and 'website/shop/scan-headers' in caplog.text
    assert 'as-s3cr3t-value' not in caplog.text and 'X-Automation-Secret' not in caplog.text and 'X-Id' not in caplog.text


def test_a_template_that_grows_past_the_value_bound_is_left_unset(caplog):
    # given
    answer = {'propertySources': [{'name': 'x', 'source': {'big': 'x' * spring_config.VALUE_MAX}}]}

    # when
    with caplog.at_level(logging.WARNING):
        out = get({'SPRING_CONFIG_MAP': '', 'SPRING_CONFIG_TEMPLATES': 'demo/one/token=${big}${big}'}, answer)

    # then
    assert out['instances'] == [] and 'demo/one/token' in caplog.text


@pytest.mark.parametrize('templates', [
    'demo/one/token', 'demo/one/token=', '=${a}', 'demo/one/token=plain text', 'demo/one/token=${a:dflt}', 'demo/one/token=${a${b}}', 'demo/one/token=${}', 'demo/one/token=${a b}', 'demo/one/token=${open',
    'jira/main/name=${a}', 'jira.url=${a}', 'Not A Target=${a}', 'general.x=${a}', 'demo/one/note=${a}', 'demo/one/token=${a}\ndemo/one/token=${b}', 'core.timezone=${a}', 'demo/one/token=' + 'x' * (spring_config.VALUE_MAX + 1),
])
def test_a_wrong_template_is_refused_before_any_request(templates):
    # given
    server = Server()

    # then
    with pytest.raises(BadMap):
        source(server)({**BASE, 'SPRING_CONFIG_MAP': 'tz=core.timezone', 'SPRING_CONFIG_TEMPLATES': templates})
    assert server.calls == []


def test_a_refused_template_names_its_target_and_never_its_text(caplog):
    # when
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BadMap) as raised:
            source()({**BASE, 'SPRING_CONFIG_TEMPLATES': 'website/shop/scan-headers=X-Inline: lit-s3cr3t ${a:dflt-s3cr3t}'})

    # then
    assert 'website/shop/scan-headers' in caplog.text and 'website/shop/scan-headers' in str(raised.value)
    assert 's3cr3t' not in caplog.text and 's3cr3t' not in str(raised.value) and 'X-Inline' not in caplog.text


@pytest.mark.parametrize('templates', ['demo/one/token=${a}\n  rest-of-lit-s3cr3t ${b}', 'X-Inline: lit-s3cr3t=${a}', 'demo/one/token=${a}\nlit-s3cr3t'])
def test_a_line_that_does_not_start_with_a_target_is_refused_by_its_number_and_never_echoed(templates, caplog):
    # when
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BadMap) as raised:
            source()({**BASE, 'SPRING_CONFIG_TEMPLATES': templates})

    # then
    assert 'SPRING_CONFIG_TEMPLATES line ' in str(raised.value)
    assert 's3cr3t' not in caplog.text and 's3cr3t' not in str(raised.value) and 'X-Inline' not in caplog.text


def test_a_changed_template_does_not_reuse_the_old_answer():
    # given
    server = Server(SHARED_ANSWER)
    feed = source(server)

    # when
    first = feed({**BASE, 'SPRING_CONFIG_TEMPLATES': 'demo/one/token=a${tz}'})
    second = feed({**BASE, 'SPRING_CONFIG_TEMPLATES': 'demo/one/token=b${tz}'})

    # then
    assert len(server.calls) == 2 and first['instances'][0]['token'] == 'aUTC' and second['instances'][0]['token'] == 'bUTC'


def test_a_shared_key_and_a_template_show_as_from_the_config_server_and_their_secrets_stay_sealed():
    # given
    import mongomock
    from skopax.instances import Instances
    from skopax.sealer import Sealer
    from tests.plugin_kit import registry_with
    from tests.plugins.base import minimal
    holder = minimal('holder', 'Holder', capabilities=Capabilities(secrets=('key', 'headers')), fields=(
        Field('url', 'URL', 'url'), Field('key', 'Key', 'secret'), Field('headers', 'Headers', 'secret'), Field('note', 'Note', 'text')))
    answer = {'propertySources': [{'name': 'x', 'source': {'google.sa-json': SHARED, 'pagescan.automation-secret': 'as-s3cr3t-value', 'pagescan.automation-id': 'bot-7',
                                                          'host': 'https://h.example.com'}}]}
    env = {'SPRING_CONFIG_MAP': 'google.sa-json=holder/one/key|holder/two/key, host=holder/one/url|holder/two/url',
           'SPRING_CONFIG_TEMPLATES': 'holder/one/headers=X-Automation-Secret: ${pagescan.automation-secret}; X-Automation-Id: ${pagescan.automation-id}\nholder/two/headers=id ${pagescan.automation-id}'}
    data = SourceData.of(get(env, answer), 'Spring Cloud Config')
    instances = Instances(mongomock.MongoClient(tz_aware=True)['skopax'], registry_with(holder), lambda: Sealer('k'), sources=lambda: [data])

    # when
    instances.refresh()
    one, two = instances.get('holder', 'one'), instances.get('holder', 'two')
    views = json.dumps([one.view(), two.view()])

    # then
    assert one.source == two.source == 'Spring Cloud Config'
    assert one.view()['secrets'] == {'key': {'set': True, 'from': 'Spring Cloud Config'}, 'headers': {'set': True, 'from': 'Spring Cloud Config'}}
    assert two.view()['owned'] == ['headers', 'key', 'url'] and two.info().config['headers'] == 'id bot-7'
    assert one.info().config['headers'] == 'X-Automation-Secret: as-s3cr3t-value; X-Automation-Id: bot-7' and two.info().config['key'] == SHARED
    assert 'as-s3cr3t-value' not in views and 'pk-s3cr3t-value' not in views


def test_a_template_into_a_field_that_is_not_secret_is_refused_by_target_before_any_request(caplog):
    # given
    server = Server(SHARED_ANSWER)

    # when
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BadMap) as raised:
            source(server)({**BASE, 'SPRING_CONFIG_TEMPLATES': 'demo/one/note=X-Automation-Secret: ${pagescan.automation-secret}'})

    # then
    assert server.calls == [] and 'demo/one/note' in str(raised.value) and 'not a secret field' in str(raised.value)
    assert 'X-Automation-Secret' not in caplog.text and 'pagescan.automation-secret' not in str(raised.value)


def test_core_tells_the_source_the_field_kinds_and_a_secret_field_is_filled():
    # given
    env = {**BASE, 'SPRING_CONFIG_MAP': '', 'SPRING_CONFIG_TEMPLATES': 'website/shop/scan-headers=v ${tz}'}
    plugin = plugin_with(SpringConfig(Server(SHARED_ANSWER), Clock()))

    # when
    out = _hook_data(plugin, env, Registry.load({}))

    # then
    assert [dict(i) for i in out.instances] == [{'plugin': 'website', 'name': 'shop', 'scan-headers': 'v UTC'}]


@pytest.mark.parametrize('target', ['website/shop/kept-headers', 'website/shop/url', 'core.timezone', 'nosuch/main/token'])
def test_with_the_real_field_kinds_a_template_into_a_field_that_is_not_secret_is_refused(target):
    # given
    server = Server(SHARED_ANSWER)
    env = {**BASE, 'SPRING_CONFIG_MAP': '', 'SPRING_CONFIG_TEMPLATES': f'{target}=v ${{tz}}'}

    # when
    with pytest.raises(BadMap) as raised:
        SpringConfig(server, Clock())(env, field_kinds(Registry.load({})))

    # then
    assert str(raised.value).startswith(f"SPRING_CONFIG_TEMPLATES line 1 target {target!r} is not a secret field;") and server.calls == []


def test_without_the_field_kind_callable_templates_are_refused_and_the_map_still_works():
    # given
    feed = SpringConfig(Server(SHARED_ANSWER), Clock())

    # when
    plain = feed({**BASE, 'SPRING_CONFIG_MAP': 'tz=core.timezone'})
    with pytest.raises(BadMap) as raised:
        feed({**BASE, 'SPRING_CONFIG_MAP': 'tz=core.timezone', 'SPRING_CONFIG_TEMPLATES': 'website/shop/scan-headers=v ${tz}'})

    # then
    assert plain['values'] == {'core.timezone': 'UTC'} and 'website/shop/scan-headers' in str(raised.value)


def test_a_field_kind_callable_that_raises_counts_as_not_secret():
    # given
    def broken(plugin_id, key):
        raise RuntimeError('down')

    # when
    with pytest.raises(BadMap) as raised:
        SpringConfig(Server(SHARED_ANSWER), Clock())({**BASE, 'SPRING_CONFIG_TEMPLATES': 'website/shop/scan-headers=v ${tz}'}, broken)

    # then
    assert 'is not a secret field' in str(raised.value)


@pytest.mark.parametrize('mapping, names', [
    ('google.api-key=maps/main/key|maps/main/token', 'maps/main/key, maps/main/token'),
    ('google.api-key=demo/one/token, google.api-key=core.timezone', 'demo/one/token, core.timezone'),
])
def test_a_key_that_fills_both_secret_and_plain_fields_is_refused_by_key_and_targets(mapping, names, caplog):
    # given
    server = Server(SHARED_ANSWER)

    # when
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BadMap) as raised:
            source(server)({**BASE, 'SPRING_CONFIG_MAP': mapping})

    # then
    assert str(raised.value) == (f"SPRING_CONFIG_MAP key 'google.api-key' fills both secret and non-secret fields ({names}); "
                                 'a key can fill several targets only when all are secret or none is')
    assert server.calls == [] and 'ak-s3cr3t-value' not in caplog.text


def test_a_key_that_fills_only_secret_or_only_plain_fields_or_an_unknown_one_is_allowed():
    # given
    def kinds(plugin_id, key):
        return None if plugin_id == 'gone' else fake_kind(plugin_id, key)

    # when
    secret = source(Server(SHARED_ANSWER))({**BASE, 'SPRING_CONFIG_MAP': 'google.api-key=demo/one/token|demo/two/token'}, kinds)
    plain = source(Server(SHARED_ANSWER))({**BASE, 'SPRING_CONFIG_MAP': 'google.api-key=maps/main/key|core.timezone'}, kinds)
    unknown = source(Server(SHARED_ANSWER))({**BASE, 'SPRING_CONFIG_MAP': 'google.api-key=demo/one/token|gone/main/key'}, kinds)

    # then
    assert secret['instances'] == [{'plugin': 'demo', 'name': 'one', 'token': 'ak-s3cr3t-value'}, {'plugin': 'demo', 'name': 'two', 'token': 'ak-s3cr3t-value'}]
    assert plain['values'] == {'core.timezone': 'ak-s3cr3t-value'} and plain['instances'] == [{'plugin': 'maps', 'name': 'main', 'key': 'ak-s3cr3t-value'}]
    assert unknown['instances'] == [{'plugin': 'demo', 'name': 'one', 'token': 'ak-s3cr3t-value'}, {'plugin': 'gone', 'name': 'main', 'key': 'ak-s3cr3t-value'}]


def test_without_the_field_kinds_a_spread_key_still_fills_its_targets_and_one_warning_says_it_was_not_checked(caplog):
    # given
    feed = SpringConfig(Server(SHARED_ANSWER), Clock())
    env = {**BASE, 'SPRING_CONFIG_MAP': 'google.api-key=maps/main/key|maps/main/token, tz=core.timezone'}

    # when
    with caplog.at_level(logging.WARNING):
        out = feed(env)
        feed(env)

    # then
    assert out['instances'] == [{'plugin': 'maps', 'name': 'main', 'key': 'ak-s3cr3t-value', 'token': 'ak-s3cr3t-value'}]
    assert caplog.text.count('cannot be checked') == 1 and 'google.api-key' in caplog.text and 'tz' not in caplog.text.split('cannot be checked')[1].split('\n')[0]
    assert 'ak-s3cr3t-value' not in caplog.text


def test_a_continuation_line_shaped_like_a_target_is_named_by_its_number_only(caplog):
    # when
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BadMap) as raised:
            source()({**BASE, 'SPRING_CONFIG_TEMPLATES': 'demo/one/token=${a}\npk-abc123=${b}'})

    # then
    assert str(raised.value).startswith('SPRING_CONFIG_TEMPLATES line 2 ')
    assert 'pk-abc123' not in str(raised.value) and 'pk-abc123' not in caplog.text


def missing_answer(*keys):
    present = {'tz': 'UTC'}
    present.update({k: 'x' for k in keys})
    return {'propertySources': [{'name': 'x', 'source': present}]}


def warnings_naming(caplog, key):
    return [r for r in caplog.records if r.levelno == logging.WARNING and key in r.getMessage()]


def test_a_missing_key_warns_once_per_process(caplog):
    # given
    server, clock = Server(missing_answer()), Clock()
    feed = source(server, clock)
    env = {'SPRING_CONFIG_MAP': 'a.b=core.timezone,tz=investigate.max-cards'}

    # when
    with caplog.at_level(logging.WARNING):
        feed({**BASE, **env})
        feed({**BASE, **env})
        clock.now += spring_config.REFETCH_S + 1
        feed({**BASE, **env})

    # then
    assert len(server.calls) == 2
    assert len(warnings_naming(caplog, 'a.b')) == 1


def test_a_missing_key_warns_again_after_the_source_changes(caplog):
    # given
    server, clock = Server(missing_answer()), Clock()
    feed = source(server, clock)
    env = {'SPRING_CONFIG_MAP': 'a.b=core.timezone'}

    # when
    with caplog.at_level(logging.WARNING):
        feed({**BASE, **env})
        feed({**BASE, **env, 'SPRING_CONFIG_PROFILE': 'qa'})

    # then
    assert len(warnings_naming(caplog, 'a.b')) == 2


def test_a_key_that_comes_back_and_goes_again_warns_again(caplog):
    # given
    server, clock = Server(missing_answer()), Clock()
    feed = source(server, clock)
    env = {**BASE, 'SPRING_CONFIG_MAP': 'a.b=core.timezone'}

    # when
    with caplog.at_level(logging.WARNING):
        feed(env)
        server.answer = missing_answer('a.b')
        clock.now += spring_config.REFETCH_S + 1
        feed(env)
        server.answer = missing_answer()
        clock.now += spring_config.REFETCH_S + 1
        feed(env)

    # then
    assert len(warnings_naming(caplog, 'a.b')) == 2


def test_a_template_key_warns_once_too(caplog):
    # given
    server, clock = Server(missing_answer()), Clock()
    feed = source(server, clock)
    env = {**BASE, 'SPRING_CONFIG_MAP': 'tz=core.timezone', 'SPRING_CONFIG_TEMPLATES': 'website/shop/scan-headers=X-Id: ${pagescan.gone}'}

    # when
    with caplog.at_level(logging.WARNING):
        feed(env)
        clock.now += spring_config.REFETCH_S + 1
        feed(env)

    # then
    assert len(warnings_naming(caplog, 'pagescan.gone')) == 1


def test_the_source_status_lists_the_left_out_keys():
    # given
    keys = [f'k{n:02d}' for n in range(60)]
    many = {**BASE, 'SPRING_CONFIG_MAP': ','.join(f'{k}=jira/n{i}/url' for i, k in enumerate(keys))}
    few = {**BASE, 'SPRING_CONFIG_MAP': 'c.d=core.timezone,a.b=jira/main/url'}

    # when
    small = SourceData.of(source(Server(missing_answer()))(few), 'x')
    big = SourceData.of(source(Server(missing_answer()))(many), 'x')
    sources = SourceSet([('x', lambda: small)], {'x': small})
    status = sources.status()['x']

    # then
    assert small.left_out == ('a.b', 'c.d') and small.left_out_more == 0
    assert status['leftOut'] == ['a.b', 'c.d'] and status['leftOutMore'] == 0
    assert len(big.left_out) == 50 and big.left_out_more == 10
    assert sources.status()['x']['ok'] is True
