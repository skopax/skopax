import base64
import dataclasses
import json
import logging

import pytest

from skopax import bounded, remote
from skopax.config import ConfigurationError
from skopax.contract import Hooks
from skopax.plugins import spring_config
from skopax.plugins.spring_config import (BadAnswer, BadMap, BadSetup, EmptyAnswer, SpringConfig, SpringConfigError, TimedOut, Unauthorized, Unreachable, Refused, flatten,
                                          map_of, resolve_value)
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply
from skopax.settingsources import SourceSet, boot, light_boot

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


def source(server=None, clock=None) -> SpringConfig:
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
    ({'SPRING_CONFIG_MAP': 'a=core.timezone,a=general.x'}, BadMap),
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
    registry = Registry.of(plugin_with(source(Server(HttpError(401, 'x')))))

    # when
    with pytest.raises(ConfigurationError) as raised:
        boot(dict(BASE), registry)

    # then
    assert 'Unauthorized' in str(raised.value) and PASSWORD not in str(raised.value)


def test_a_main_starts_from_the_server_and_the_values_reach_the_settings():
    # given
    registry = Registry.of(plugin_with(source()))

    # when
    b = boot(dict(BASE), registry)

    # then
    assert b.settings.timezone == 'Europe/Berlin' and b.settings.investigate_max_cards == 9
    assert [d.label for d in b.sources if d.instances] == ['Spring Cloud Config']


def test_an_agent_starts_on_the_environment_alone_when_the_server_times_out_simulated():
    # given
    registry = Registry.of(plugin_with(source(Server(remote.TimedOut('x')))))

    # when
    b = light_boot(dict(BASE), registry)

    # then
    assert b.failed == {'Spring Cloud Config': 'ConfigurationError'} and b.sources == []


def test_a_server_that_fails_later_leaves_the_applied_values_in_place():
    # given
    server, clock = Server(), Clock()
    registry = Registry.of(plugin_with(source(server, clock)))
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


def test_a_key_mapped_twice_logs_which_key(caplog):
    # when
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BadMap):
            source()({**BASE, 'SPRING_CONFIG_MAP': 'dup.key=core.timezone,dup.key=general.x'})

    # then
    assert 'dup.key' in caplog.text


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
