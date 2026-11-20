import dataclasses
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.conftest import settings
from skopax.app import create_app
from skopax.settingsources import boot
from skopax.sso import SsoConfig
from skopax.config import ConfigurationError, load_settings, settings_env_names
from skopax.messages import msg
from skopax.tunables import TEXT_LIMITS

ENV = ('MONGO_URI', 'MONGO_DB', 'SKOPAX_TOKEN', 'TICK_RETENTION_DAYS', 'VAULT_ONLY',
       'CLOUDFLARE_API_TOKEN', 'GITLAB_TOKEN', 'JIRA_EMAIL', 'JIRA_API_TOKEN', 'SLACK_PUSH_USER', 'SKOPAX_RELEASE_SETTLE', 'SKOPAX_PROBE',
       'ELASTIC_LOGGING_URL', 'ELASTIC_LOGGING_API_KEY', 'ELASTIC_APM_URL', 'ELASTIC_APM_API_KEY',
       'PROMETHEUS_URL',
       'AIRBYTE_URL', 'AIRBYTE_CLIENT_ID', 'AIRBYTE_CLIENT_SECRET', 'PMM_URL', 'PMM_TOKEN',
       'ANTHROPIC_API_KEY', 'INVESTIGATE_MODE', 'INVESTIGATE_MODEL', 'INVESTIGATE_EFFORT', 'INVESTIGATE_DAILY_USD', 'INVESTIGATE_MAX_CARDS',
       'INVESTIGATE_MAX_TOOL_CALLS', 'SLACK_APP_TOKEN', 'SLACK_LISTEN', 'SLACK_ASK_CAP', 'SLACK_OPERATOR_CACHE_SECONDS',
       'ROLES_URL', 'ROLES_CLIENT_ID', 'ROLES_CLIENT_SECRET', 'ROLES_CHANNEL', 'ROLES_SCOPE', 'INVESTIGATE_GATE_MODEL',
       'INVESTIGATE_SLACK_MODEL', 'INVESTIGATE_SLACK_EFFORT', 'INVESTIGATE_SLACK_MAX_TOOL_CALLS', 'SKOPAX_AUTO_RESOLVE_DAYS', 'SKOPAX_AUTO_RESOLVE_HOURS', 'SKOPAX_AUTO_RESOLVE_HOURS_CRIT', 'SKOPAX_AUTO_RESOLVE_HOURS_WARN', 'SKOPAX_INFO_AUTO_CLOSE_DAYS',
       'INVESTIGATE_STRICT_TOOLS', 'SSO_SESSION_IDLE_HOURS', 'SSO_SESSION_MAX_DAYS', 'JIRA_TITLE_PREFIX', 'SKOPAX_ENV_LABEL', 'SLACK_POST_SEV', 'SLACK_CRIT_NOW')


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_without_a_fetch_settings_come_from_env(monkeypatch):
    # given
    monkeypatch.setenv('MONGO_URI', 'mongodb://localhost:27017/local_db')
    monkeypatch.setenv('SKOPAX_TOKEN', 'local')

    # when
    s = load_settings()

    # then
    assert s.mongo_uri == 'mongodb://localhost:27017/local_db'
    assert s.mongo_db == 'local_db'
    assert s.token == 'local'
    assert s.investigate_max_cards == 5


def test_config_server_values_win_and_placeholders_resolve(monkeypatch):
    # given
    monkeypatch.setenv('SKOPAX_TOKEN', 'from-env')
    remote = {
        'mongo.uri': 'mongodb://usr:${mongo.password}@mongo:27017/app_db?authSource=app_db',
        'mongo.password': 's3cret',
        'core.token': '${core.write-token}',
        'core.write-token': 'from-vault',
        'investigate.max-cards': '${retention:14}',
    }

    # when
    s = load_settings(lambda: remote)

    # then
    assert s.mongo_uri == 'mongodb://usr:s3cret@mongo:27017/app_db?authSource=app_db'
    assert s.mongo_db == 'app_db'
    assert s.token == 'from-vault'
    assert s.investigate_max_cards == 14


def test_server_job_keys_are_read_from_the_config_and_default_to_off(monkeypatch):
    # given
    remote = {'slack.push-user': ' U0123ABCD ', 'core.release-settle': 'True'}

    # when
    on = load_settings(lambda: remote)
    off = load_settings(lambda: {})

    # then
    assert (on.slack_push_user, on.release_settle) == ('U0123ABCD', True)
    assert (off.slack_push_user, off.release_settle) == ('', False)


def test_investigate_keys_are_read_from_the_config_and_an_unknown_mode_means_off(monkeypatch):
    # given

    # when
    shadow = load_settings(lambda: {'investigate.mode': 'Shadow', 'investigate.daily-usd': '12.5'})
    unknown = load_settings(lambda: {'investigate.mode': 'on'})

    # then
    assert (shadow.investigate_mode, shadow.investigate_daily_usd) == ('shadow', 12.5)
    assert unknown.investigate_mode == 'off'


def test_slack_run_keys_are_read_from_the_config_with_defaults(monkeypatch):
    # given

    # when
    s = load_settings(lambda: {'investigate.slack-model': ' claude-haiku-5-5 ', 'investigate.slack-effort': ' high ', 'investigate.slack-max-tool-calls': '6'})
    bare = load_settings(lambda: {})

    # then
    assert (s.investigate_slack_model, s.investigate_slack_effort, s.investigate_slack_max_tool_calls) == ('claude-haiku-5-5', 'high', 6)
    assert (bare.investigate_slack_model, bare.investigate_slack_effort, bare.investigate_slack_max_tool_calls) == ('claude-sonnet-5-5', 'medium', 10)


def test_the_assessment_tool_budget_is_read_from_the_config_with_a_default(monkeypatch):
    # given

    # when
    s = load_settings(lambda: {'investigate.assess-max-tool-calls': '8'})
    bare = load_settings(lambda: {})

    # then
    assert (s.investigate_assess_max_tool_calls, bare.investigate_assess_max_tool_calls) == (8, 16)
    assert bare.investigate_max_tool_calls == 30


def test_strict_tools_are_off_unless_the_config_turns_them_on(monkeypatch):
    # given

    # when
    on = load_settings(lambda: {'investigate.strict-tools': ' TRUE '})
    other = load_settings(lambda: {'investigate.strict-tools': 'yes'})
    bare = load_settings(lambda: {})

    # then
    assert (on.investigate_strict_tools, other.investigate_strict_tools, bare.investigate_strict_tools) == (True, False, False)


def test_slack_listen_keys_are_read_from_the_config_and_an_unknown_listen_mode_means_off(monkeypatch):
    # given

    # when
    s = load_settings(lambda: {'slack.app-token': ' xapp-1-abc ', 'slack.listen': 'Shadow', 'slack.ask-cap': '4', 'slack.operator-cache-seconds': '120',
                               'investigate.gate-model': 'claude-haiku-5-5'})
    odd = load_settings(lambda: {'slack.listen': 'loud'})
    bare = load_settings(lambda: {})

    # then
    assert (s.slack_listen, s.slack_ask_cap, s.slack_operator_cache_s, s.investigate_gate_model) == ('shadow', 4, 120, 'claude-haiku-5-5')
    assert 'xapp-1-abc' not in repr(s) and not hasattr(s, 'slack_app_token')
    assert odd.slack_listen == 'off'
    assert (bare.slack_listen, bare.slack_ask_cap, bare.slack_operator_cache_s, bare.investigate_gate_model) == ('off', 10, 600, 'claude-haiku-5-5')


def test_session_lengths_default_to_three_idle_days_and_seven_days_after_login(monkeypatch, caplog):
    # given
    caplog.set_level(logging.WARNING, logger='skopax.config')

    # when
    bare = load_settings(lambda: {})
    s = load_settings(lambda: {'sso.session-idle-hours': ' 24 ', 'sso.session-max-days': '14'})
    widest = load_settings(lambda: {'sso.session-idle-hours': '168', 'sso.session-max-days': '30'})
    narrowest = load_settings(lambda: {'sso.session-idle-hours': '1', 'sso.session-max-days': '1'})
    day = load_settings(lambda: {'sso.session-idle-hours': '24', 'sso.session-max-days': '1'})
    monkeypatch.setenv('SSO_SESSION_IDLE_HOURS', '12')
    monkeypatch.setenv('SSO_SESSION_MAX_DAYS', '2')
    env = load_settings(lambda: {})

    # then
    assert (bare.sso_session_idle_hours, bare.sso_session_max_days) == (72, 7)
    assert (s.sso_session_idle_hours, s.sso_session_max_days) == (24, 14)
    assert (widest.sso_session_idle_hours, widest.sso_session_max_days) == (168, 30)
    assert (narrowest.sso_session_idle_hours, narrowest.sso_session_max_days) == (1, 1)
    assert (day.sso_session_idle_hours, day.sso_session_max_days) == (24, 1)
    assert (env.sso_session_idle_hours, env.sso_session_max_days) == (12, 2)
    assert caplog.records == []


@pytest.mark.parametrize('values, expected', [
    ({'sso.session-idle-hours': 'three days'}, (72, 7)),
    ({'sso.session-idle-hours': ' '}, (72, 7)),
    ({'sso.session-idle-hours': '1.5'}, (72, 7)),
    ({'sso.session-idle-hours': '0'}, (72, 7)),
    ({'sso.session-idle-hours': '169'}, (72, 7)),
    ({'sso.session-max-days': 'week'}, (72, 7)),
    ({'sso.session-max-days': '0'}, (72, 7)),
    ({'sso.session-max-days': '31'}, (72, 7)),
    ({'sso.session-idle-hours': '120', 'sso.session-max-days': '4'}, (72, 4)),
    ({'sso.session-idle-hours': '48', 'sso.session-max-days': '1'}, (24, 1)),
    ({'sso.session-max-days': '2'}, (48, 2)),
])
def test_an_invalid_session_length_falls_back_with_one_warning(monkeypatch, caplog, values, expected):
    # given
    caplog.set_level(logging.WARNING, logger='skopax.config')

    # when
    s = load_settings(lambda: values)

    # then
    assert (s.sso_session_idle_hours, s.sso_session_max_days) == expected
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert 'sso.session-' in record.getMessage()


def hours_of(config: dict, monkeypatch, env: dict | None = None) -> int:
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    return load_settings(lambda: config).auto_resolve_hours


def test_auto_resolve_hours_default_to_six_when_neither_hours_nor_days_is_set(monkeypatch):
    # given

    # when
    default = hours_of({}, monkeypatch)
    blank = hours_of({'core.auto-resolve-hours': ' ', 'core.auto-resolve-days': ' '}, monkeypatch)

    # then
    assert (default, blank) == (6, 6)


def test_only_the_deprecated_days_set_gives_days_times_twenty_four_hours(monkeypatch):
    # given

    # when
    from_config = hours_of({'core.auto-resolve-days': ' 5 '}, monkeypatch)
    from_env = hours_of({}, monkeypatch, {'SKOPAX_AUTO_RESOLVE_DAYS': '2'})

    # then
    assert (from_config, from_env) == (120, 48)


def test_hours_win_when_both_hours_and_days_are_set(monkeypatch):
    # given

    # when
    config = hours_of({'core.auto-resolve-hours': '12', 'core.auto-resolve-days': '5'}, monkeypatch)
    env_hours = hours_of({'core.auto-resolve-days': '5'}, monkeypatch, {'SKOPAX_AUTO_RESOLVE_HOURS': '8'})

    # then
    assert (config, env_hours) == (12, 8)


def test_zero_days_or_zero_hours_turns_auto_resolve_off_and_an_out_of_range_hours_falls_back(monkeypatch):
    # given

    # when
    days_off = hours_of({'core.auto-resolve-days': '0'}, monkeypatch)
    hours_off = hours_of({'core.auto-resolve-hours': '0', 'core.auto-resolve-days': '3'}, monkeypatch)
    too_big = hours_of({'core.auto-resolve-hours': '721'}, monkeypatch)
    text = hours_of({'core.auto-resolve-hours': 'soon'}, monkeypatch)

    # then
    assert (days_off, hours_off, too_big, text) == (0, 0, 6, 6)


def test_a_non_numeric_days_value_warns_and_counts_as_unset(monkeypatch, caplog):
    # given

    # when
    with caplog.at_level(logging.WARNING):
        loaded = load_settings(lambda: {'core.auto-resolve-days': 'three'})

    # then
    assert (loaded.auto_resolve_days, loaded.auto_resolve_hours) == (None, 6)
    assert any('core.auto-resolve-days' in r.getMessage() for r in caplog.records)


def test_an_admin_days_value_becomes_hours_unless_hours_are_set_by_config_or_by_admin():
    # given
    from skopax.tunables import apply

    # when
    days_only = apply(load_settings(lambda: {}), {'core.auto-resolve-days': '2'})
    hours_by_admin = apply(load_settings(lambda: {}), {'core.auto-resolve-days': '2', 'core.auto-resolve-hours': '9'})
    hours_by_config = apply(load_settings(lambda: {'core.auto-resolve-hours': '4'}), {'core.auto-resolve-days': '2'})
    days_off = apply(load_settings(lambda: {}), {'core.auto-resolve-days': '0'})

    # then
    assert (days_only.auto_resolve_hours, hours_by_admin.auto_resolve_hours, hours_by_config.auto_resolve_hours, days_off.auto_resolve_hours) == (48, 9, 4, 0)


def test_admin_lists_the_hours_setting_in_the_cards_group_and_hides_the_deprecated_keys(store):
    # given
    from skopax.tunables import Live, TunableStore

    # when
    live = Live(load_settings(lambda: {}), TunableStore(store.db))
    listed = {e['key']: e for e in live.entries()}
    live.set('core.auto-resolve-hours-warn', '10', 'alice@example.com')

    # then
    hours = listed['core.auto-resolve-hours-warn']
    assert (hours['group'], hours['label'], hours['kind'], hours['low'], hours['high'], hours['default']) == ('Cards', 'Auto-resolve cleared warning cards after (hours)', 'int', 0, 720, '6')
    assert 'Closes a warning card that stopped firing and that nobody acted on after N hours' in hours['help']
    assert 'core.auto-resolve-days' not in listed and 'core.auto-resolve-hours' not in listed
    assert live.current().auto_resolve_hours_warn == 10
    with pytest.raises(KeyError):
        live.set('core.auto-resolve-days', '2', 'alice@example.com')
    with pytest.raises(KeyError):
        live.set('core.auto-resolve-hours', '2', 'alice@example.com')


def test_post_at_once_from_defaults_to_warning_and_takes_each_severity_or_off(monkeypatch, caplog):
    # given
    caplog.set_level(logging.WARNING, logger='skopax.config')

    # when
    default = load_settings(lambda: {})
    chosen = [load_settings(lambda: {'slack.post-sev': f' {v.upper()} '}).slack_post_sev for v in ('crit', 'warn', 'info', 'off')]
    odd = load_settings(lambda: {'slack.post-sev': 'loud'})
    monkeypatch.setenv('SLACK_POST_SEV', 'crit')
    env = load_settings(lambda: {})

    # then
    assert (default.slack_post_sev, default.slack_post_sev_set) == ('warn', False)
    assert chosen == ['crit', 'warn', 'info', 'off']
    assert (odd.slack_post_sev, env.slack_post_sev, env.slack_post_sev_set) == ('warn', 'crit', True)
    assert [r.getMessage() for r in caplog.records] == ["slack.post-sev 'loud' is not one of crit, warn, info, off; using warn"]


def test_the_old_critical_at_once_switch_still_counts_until_post_at_once_from_is_set(monkeypatch, caplog):
    # given
    from skopax import config
    monkeypatch.setattr(config, 'TOLD', set())
    caplog.set_level(logging.INFO, logger='skopax.config')

    # when
    old_off = [load_settings(lambda: {'slack.crit-now': v}).slack_post_sev for v in (' False ', '0', 'no', 'off', 'OFF')]
    old_on = load_settings(lambda: {'slack.crit-now': ' TRUE '})
    both = load_settings(lambda: {'slack.crit-now': 'false', 'slack.post-sev': 'info'})
    monkeypatch.setenv('SLACK_CRIT_NOW', '0')
    env_off = load_settings(lambda: {})

    # then
    assert old_off == ['off'] * 5, 'anything but true turned the old switch off, as before'
    assert (old_on.slack_post_sev, old_on.slack_crit_now, both.slack_post_sev, env_off.slack_post_sev) == ('warn', True, 'info', 'off')
    told = [r.getMessage() for r in caplog.records if 'slack.crit-now' in r.getMessage()]
    assert len(told) == 1 and 'replaced by slack.post-sev' in told[0], 'one line per process names the new key'
    assert 'SLACK_CRIT_NOW' in settings_env_names() and 'SLACK_POST_SEV' in settings_env_names()


def test_a_stored_admin_critical_at_once_switch_maps_to_off_unless_post_at_once_from_is_set_anywhere():
    # given
    from skopax.tunables import apply

    # when
    stored_off = apply(load_settings(lambda: {}), {'slack.crit-now': 'false'})
    stored_on = apply(load_settings(lambda: {'slack.crit-now': 'false'}), {'slack.crit-now': 'true'})
    stored_both = apply(load_settings(lambda: {}), {'slack.crit-now': 'false', 'slack.post-sev': 'crit'})
    config_new = apply(load_settings(lambda: {'slack.post-sev': 'info'}), {'slack.crit-now': 'false'})
    env_old_admin_new = apply(load_settings(lambda: {'slack.crit-now': 'false'}), {'slack.post-sev': 'warn'})

    # then
    assert (stored_off.slack_post_sev, stored_on.slack_post_sev, stored_both.slack_post_sev) == ('off', 'warn', 'crit')
    assert (config_new.slack_post_sev, env_old_admin_new.slack_post_sev) == ('info', 'warn')


def test_admin_offers_post_at_once_from_with_named_choices_and_hides_the_old_switch(store, caplog):
    # given
    from skopax.tunables import Live, TunableStore
    store.db['tunables'].insert_one({'_id': 'slack.crit-now', 'value': 'false', 'by': 'alice@example.com', 'at': '2026-11-01T10:00:00Z'})
    caplog.set_level(logging.INFO, logger='skopax.tunables')

    # when
    live = Live(load_settings(lambda: {}), TunableStore(store.db))
    listed = {e['key']: e for e in live.entries()}
    live.current()
    live.refresh(force=True)
    old = live.current().slack_post_sev
    saved, _ = live.set('slack.post-sev', 'info', 'bob@example.com')

    # then
    entry = listed['slack.post-sev']
    assert (entry['group'], entry['label'], entry['kind'], entry['default']) == ('Slack', 'Post at once from', 'choice', 'warn')
    assert entry['choices'] == ['crit', 'warn', 'info', 'off']
    assert entry['choiceLabels'] == {'crit': 'Critical', 'warn': 'Warning', 'info': 'Info', 'off': 'Off'}
    assert (entry['value'], entry['source'], entry['by'], entry['oldSwitch']) == ('off', 'admin', 'alice@example.com', 'slack.crit-now'), 'the row says the value comes from the old switch'
    assert 'slack.crit-now' not in listed
    assert old == 'off' and live.current().slack_post_sev == 'info'
    assert 'oldSwitch' not in saved and store.db['tunables'].find_one({'_id': 'slack.crit-now'}) is None, 'saving the new setting clears the old switch'
    told = [r.getMessage() for r in caplog.records if 'slack.crit-now' in r.getMessage()]
    assert len(told) == 1 and 'slack.post-sev' in told[0], 'the stored old key is named once, not on every refresh'
    with pytest.raises(KeyError):
        live.set('slack.crit-now', 'true', 'alice@example.com')


def test_reset_on_post_at_once_from_clears_the_old_switch_and_a_config_old_switch_is_named(store):
    # given
    from skopax.tunables import Live, TunableStore
    store.db['tunables'].insert_one({'_id': 'slack.crit-now', 'value': 'false', 'by': 'alice@example.com', 'at': '2026-11-01T10:00:00Z'})
    live = Live(load_settings(lambda: {}), TunableStore(store.db))

    # when
    reset, change = live.set('slack.post-sev', None, 'bob@example.com')
    from_config = {e['key']: e for e in Live(load_settings(lambda: {'slack.crit-now': 'no'}), TunableStore(store.db)).entries()}['slack.post-sev']

    # then
    assert (reset['value'], reset['source'], 'oldSwitch' in reset) == ('warn', 'default', False)
    assert store.db['tunables'].find_one({'_id': 'slack.crit-now'}) is None and live.current().slack_post_sev == 'warn'
    assert (from_config['value'], from_config['source'], from_config['oldSwitch']) == ('off', 'config', 'slack.crit-now')


def test_the_changes_history_of_post_at_once_from_reverts_to_what_was_in_effect(store):
    # given
    from skopax.tunables import Live, TunableStore
    store.db['tunables'].insert_one({'_id': 'slack.crit-now', 'value': 'false', 'by': 'alice@example.com', 'at': '2026-11-01T10:00:00Z'})
    live = Live(load_settings(lambda: {}), TunableStore(store.db))

    # when
    _, saved = live.set('slack.post-sev', 'info', 'bob@example.com')
    rows = live.tunables.history()
    live.set('slack.post-sev', saved['old'], 'bob@example.com')
    reverted = live.current().slack_post_sev

    # then
    old_row = next(r for r in rows if r['key'] == 'slack.crit-now')
    assert (old_row['old'], old_row['new'], old_row.get('legacy')) == ('false', None, True), 'the old switch row is marked so the page offers no Revert for it'
    assert (saved['old'], saved['new']) == ('off', 'info'), 'the post-sev row records Off, the value in effect, so Revert puts Off back'
    assert reverted == 'off'


def test_reset_with_only_the_old_switch_stored_writes_no_empty_post_at_once_row(store):
    # given
    from skopax.tunables import Live, TunableStore
    store.db['tunables'].insert_one({'_id': 'slack.crit-now', 'value': 'false', 'by': 'alice@example.com', 'at': '2026-11-01T10:00:00Z'})
    live = Live(load_settings(lambda: {}), TunableStore(store.db))

    # when
    _, change = live.set('slack.post-sev', None, 'bob@example.com')
    rows = live.tunables.history()

    # then
    assert [(r['key'], r['old'], r['new']) for r in rows] == [('slack.crit-now', 'false', None)]
    assert change['key'] == 'slack.crit-now' and live.current().slack_post_sev == 'warn'


def test_slack_fallback_minutes_default_to_fifteen_and_zero_turns_it_off(monkeypatch):
    # given

    # when
    default = load_settings(lambda: {})
    off = load_settings(lambda: {'slack.fallback-minutes': ' 0 '})
    blank = load_settings(lambda: {'slack.fallback-minutes': ' '})
    monkeypatch.setenv('SLACK_FALLBACK_MINUTES', '30')
    env = load_settings(lambda: {})

    # then
    assert (default.slack_fallback_minutes, off.slack_fallback_minutes, blank.slack_fallback_minutes, env.slack_fallback_minutes) == (15, 0, 15, 30)


def test_explicit_database_overrides_the_uri_path(monkeypatch):
    # given
    remote = {'mongo.uri': 'mongodb://mongo:27017/admin', 'mongo.database': 'app_db'}

    # when
    s = load_settings(lambda: remote)

    # then
    assert s.mongo_db == 'app_db'


def test_unresolved_or_circular_placeholders_fail(monkeypatch):
    # given

    # when
    with pytest.raises(ConfigurationError, match='unresolved placeholder'):
        load_settings(lambda: {'core.token': '${missing.key}'})
    with pytest.raises(ConfigurationError, match='circular placeholder'):
        load_settings(lambda: {'core.token': '${a}', 'a': '${b}', 'b': '${a}'})


ALLOWED_HOSTS = ('localhost',)


def test_env_only_boot_with_nothing_set():
    # when
    s = load_settings(env={})

    # then
    assert s.mongo_uri == 'mongodb://localhost:27017' and s.timezone == 'UTC' and s.base_url == '' and s.link_hosts == ()
    assert s.investigate_language == 'en'


def test_no_default_points_at_a_real_host():
    # given
    s = load_settings(env={})

    # then
    for f in dataclasses.fields(s):
        v = getattr(s, f.name)
        if isinstance(v, str) and '://' in v:
            assert urlsplit(v).hostname in ALLOWED_HOSTS, f.name


def test_no_default_names_a_company_id_or_project():
    # given
    s = load_settings(env={})

    # then
    assert (s.jira_project, s.jira_projects, s.jira_tech_project, s.jira_tech_assignee) == ('', (), '', '')
    assert (s.self_deploy, s.alert_twins, s.release_ignore) == ('', (), ())


def test_comma_lists_and_headers_parse():
    # given
    env = {'SKOPAX_LINK_HOSTS': ' a.example.com, ,b.example.org ', 'JIRA_PROJECTS': 'OPS,WEB', 'SSO_TOKEN_HEADERS': 'X-Client: web; X-Env: prod'}

    # when
    s = load_settings(env=env)

    # then
    assert s.link_hosts == ('a.example.com', 'b.example.org') and s.jira_projects == ('OPS', 'WEB')
    assert s.token_headers == {'X-Client': 'web', 'X-Env': 'prod'}


def test_new_fields_read_their_env_names():
    # given
    env = {'SKOPAX_BASE_URL': 'https://w.example.com', 'SKOPAX_TIMEZONE': 'Europe/Berlin', 'SKOPAX_ORG_ABOUT': 'A shop.',
           'SKOPAX_EMAIL_DOMAINS': 'example.com,example.org',
           'SKOPAX_SELF_PROJECT': 'platform/skopax', 'SKOPAX_SELF_LOG_INDEX': 'logs-platform-*',
           'SKOPAX_SELF_DEPLOY': 'platform/skopax', 'JIRA_DEVELOPER_FIELD': 'customfield_10001',
           'SKOPAX_ALERT_TWINS': 'HighErrorRate, , HighHttp5xxRatio', 'SKOPAX_RELEASE_IGNORE': 'shop/cron, platform/tools'}

    # when
    s = load_settings(env=env)

    # then
    assert (s.base_url, s.timezone, s.org_about, s.email_domains) == ('https://w.example.com', 'Europe/Berlin', 'A shop.', ('example.com', 'example.org'))
    assert (s.self_project, s.self_log_index, s.self_deploy, s.jira_developer_field) == ('platform/skopax', 'logs-platform-*', 'platform/skopax', 'customfield_10001')
    assert s.alert_twins == ('HighErrorRate', 'HighHttp5xxRatio') and s.release_ignore == ('shop/cron', 'platform/tools')


def test_the_title_prefix_and_the_environment_label_default_to_empty_and_read_their_env_names():
    # given
    blank = load_settings(env={})

    # when
    s = load_settings(env={'JIRA_TITLE_PREFIX': 'OPS: ', 'SKOPAX_ENV_LABEL': ' PROD '})

    # then
    assert (blank.jira_title_prefix, blank.env_label) == ('', '')
    assert (s.jira_title_prefix, s.env_label) == ('OPS: ', 'PROD'), 'the prefix keeps its trailing space'


def test_fetch_seam_still_layers_under_env():
    # when
    s = load_settings(fetch=lambda: {'mongo.uri': 'mongodb://localhost:27018/w'}, env={})

    # then
    assert s.mongo_uri == 'mongodb://localhost:27018/w'


def test_unknown_timezone_stops_startup(store):
    # given
    bad = dataclasses.replace(settings(), timezone='Mars/Olympus')

    # when
    with pytest.raises(ConfigurationError, match='Mars/Olympus'):
        create_app(bad, store)


def test_invalid_prometheus_namespaces_stop_startup():
    # given
    env = {'PROMETHEUS_URL': 'https://prom.example.com', 'PROMETHEUS_NAMESPACES': 'shop|(payments'}

    # when
    with pytest.raises(ConfigurationError) as stopped:
        boot(env)

    # then
    assert all(word in str(stopped.value) for word in ('prometheus', 'main', 'namespaces', 'environment')) and 'shop|(payments' not in str(stopped.value)


def test_a_pasted_secret_in_a_regex_or_choice_field_never_reaches_the_error_or_the_log(tmp_path, caplog):
    # given
    secret = 'tok_PASTEDSECRET0123456789('
    path = tmp_path / 'skopax.yaml'
    path.write_text(f"plugins:\n  prometheus:\n    instances:\n      - name: east\n        url: https://prom.example.com\n        auth: '{secret}'\n", encoding='utf-8')

    # when
    with caplog.at_level(logging.DEBUG), pytest.raises(ConfigurationError) as in_regex:
        boot({'PROMETHEUS_URL': 'https://prom.example.com', 'PROMETHEUS_NAMESPACES': secret})
    with caplog.at_level(logging.DEBUG), pytest.raises(ConfigurationError) as in_choice:
        boot({'SKOPAX_CONFIG_FILE': str(path)})

    # then
    assert 'namespaces' in str(in_regex.value) and 'auth' in str(in_choice.value)
    assert 'PASTEDSECRET' not in str(in_regex.value) + str(in_choice.value) + caplog.text


def test_jira_field_ids_and_issue_types_read_their_env_names():
    # when
    s = load_settings(env={'JIRA_SCOPE_FIELD': 'customfield_1', 'JIRA_DEV_SCOPE': ' Web ', 'JIRA_POINTS_FIELD': 'customfield_2', 'JIRA_SPRINT_FIELD': 'customfield_3', 'JIRA_ISSUE_TYPES': 'OPS=Story, bad, WEB=Bug'})
    empty = load_settings(env={})

    # then
    assert (s.jira_scope_field, s.jira_points_field, s.jira_sprint_field) == ('customfield_1', 'customfield_2', 'customfield_3')
    assert s.jira_issue_types == (('OPS', 'Story'), ('WEB', 'Bug'))
    assert (empty.jira_scope_field, empty.jira_points_field, empty.jira_sprint_field, empty.jira_issue_types) == ('', '', '', ())
    assert (s.jira_dev_scope, empty.jira_dev_scope) == ('Web', ''), 'no scope value unless one is set'


def test_access_settings_default_to_a_closed_viewer_setup():
    # when
    s = load_settings(env={})

    # then
    assert (s.secret_key, s.sso_default_role, s.public_read) == ('', 'viewer', False)


def test_access_settings_come_from_the_environment():
    # given
    env = {'SKOPAX_SECRET_KEY': ' a-long-secret ', 'SSO_DEFAULT_ROLE': 'operator', 'SKOPAX_PUBLIC_READ': 'TRUE'}

    # when
    s = load_settings(env=env)

    # then
    assert (s.secret_key, s.sso_default_role, s.public_read) == ('a-long-secret', 'operator', True)
    assert 'a-long-secret' not in repr(s)


def test_an_unknown_default_role_is_a_configuration_error():
    # then
    with pytest.raises(ConfigurationError, match='SSO_DEFAULT_ROLE'):
        load_settings(env={'SSO_DEFAULT_ROLE': 'superuser'})


def test_an_over_long_org_about_in_the_environment_stops_boot_and_one_at_the_admin_limit_is_kept():
    # when
    kept = load_settings(env={'SKOPAX_ORG_ABOUT': 'x' * 300})
    with pytest.raises(ConfigurationError) as stopped:
        load_settings(env={'SKOPAX_ORG_ABOUT': 'x' * 301})

    # then
    assert kept.org_about == 'x' * 300 and TEXT_LIMITS['core.org-about'] == 300
    assert str(stopped.value) == msg('settings.env_too_long', name='SKOPAX_ORG_ABOUT', limit=300)


def test_empty_access_variables_behave_as_unset():
    # given
    env = {'SKOPAX_SECRET_KEY': '', 'SKOPAX_ADMIN_EMAIL': '', 'SKOPAX_ADMIN_PASSWORD': '', 'SKOPAX_PUBLIC_READ': ''}

    # when
    s = load_settings(env=env)

    # then
    assert (s.secret_key, s.admin_email, s.admin_password, s.public_read) == ('', '', '', False)
    assert not s.secret_key
    assert not (s.admin_email and s.admin_password)


CONFIG_SOURCE = (Path(__file__).resolve().parent.parent / 'skopax' / 'config.py').read_text()
ENV_NAMES = sorted(set(re.findall(r"get\('[a-z.-]+', '(SKOPAX_[A-Z0-9_]+)'", CONFIG_SOURCE)))


def test_the_env_names_are_found_in_the_source():
    # then
    assert len(ENV_NAMES) >= 20
    assert 'SKOPAX_TOKEN' in ENV_NAMES and 'SKOPAX_SECRET_KEY' in ENV_NAMES


def differs(base: dict, name: str, value: str) -> bool:
    try:
        return dataclasses.asdict(load_settings(env={name: value})) != base
    except ValueError:
        return True


@pytest.mark.parametrize('name', ENV_NAMES)
def test_skopax_env_names_are_read(name):
    # given
    base = dataclasses.asdict(load_settings(env={}))

    # when
    seen = [value for value in ('7', 'true', 'false', 'x', 'openai') if differs(base, name, value)]

    # then
    assert seen, f'{name} changes no setting'


def test_default_role_names_use_the_new_prefix():
    # when
    s = load_settings(env={})

    # then
    assert (s.operator_role, s.admin_role, s.mcp_required_role) == ('SKOPAX_OPERATOR', 'SKOPAX_ADMIN', 'SKOPAX_MCP')


def test_default_database_is_skopax():
    # when
    s = load_settings(env={})
    from_uri = load_settings(env={'MONGO_URI': 'mongodb://mongo:27017/other'})

    # then
    assert s.mongo_db == 'skopax'
    assert from_uri.mongo_db == 'other'


def test_core_key_namespace():
    # given
    from skopax.tunables import STATIC
    keys = set(re.findall(r"\bget\('([a-z.-]+)'", CONFIG_SOURCE))
    ids = {t.key for t in STATIC}

    # then
    assert not [k for k in keys | ids if k.startswith(('skopax.', 'service.'))]
    assert {'core.token', 'core.secret-key', 'core.timezone'} <= keys
    assert {'core.assign-dm', 'core.postdeploy', 'core.jira-links', 'core.release-settle', 'core.auto-resolve-days', 'core.auto-resolve-hours'} <= ids
    assert 'access.public-read' in ids and 'chat.daily-cap' in ids


def test_the_known_settings_are_derived_from_what_load_settings_reads():
    # when
    known = settings_env_names()

    # then
    assert {'SKOPAX_TOKEN', 'SKOPAX_SECRET_KEY', 'SKOPAX_TIMEZONE', 'SKOPAX_DISPLAY_NAME'} <= known
    assert set(ENV_NAMES) <= known
    assert 'SKOPAX_SERVICE_HOST' not in known and 'SKOPAX_PORT' not in known


def test_boot_env_lists_the_names_read_before_settings_exist():
    # given
    from skopax import agent, registry, settingsources
    from skopax.config import BOOT_ENV
    from skopax.plugins import kubernetes

    # then
    assert BOOT_ENV == ('SKOPAX_PLUGINS', 'SKOPAX_CONFIG_FILE', 'SKOPAX_MODE', 'SKOPAX_AGENT_TOKEN', 'SKOPAX_AGENT_RELAY_HOSTS', 'SKOPAX_KUBE_LOCAL_NAME')
    assert kubernetes.LOCAL_NAME_ENV in BOOT_ENV
    assert registry.ENV in BOOT_ENV
    assert settingsources.FILE_ENV in BOOT_ENV
    assert (agent.MODE_ENV, agent.TOKEN_ENV, agent.RELAY_ENV) == ('SKOPAX_MODE', 'SKOPAX_AGENT_TOKEN', 'SKOPAX_AGENT_RELAY_HOSTS')
    assert {agent.MODE_ENV, agent.TOKEN_ENV, agent.RELAY_ENV} <= set(BOOT_ENV)
    assert set(BOOT_ENV) <= settings_env_names()


def test_sso_switches_have_their_defaults_and_unknown_modes_fall_back_with_a_warning(caplog):
    # given
    given = {'SSO_ENABLED': 'false', 'SSO_CLAIMS': 'Access-Token', 'SSO_CLIENT_AUTH': 'BASIC', 'SSO_REQUIRE_VERIFIED_EMAIL': 'TRUE'}
    unknown = {'SSO_CLAIMS': 'userinfo', 'SSO_CLIENT_AUTH': 'private-key-jwt'}

    # when
    plain = load_settings(env={})
    chosen = load_settings(env=given)
    with caplog.at_level(logging.WARNING, logger='skopax.config'):
        odd = load_settings(env=unknown)

    # then
    assert (plain.sso_on, plain.sso_claims, plain.sso_client_auth, plain.sso_require_verified_email, plain.sso_scope) == (True, 'id-token', 'auto', False, 'openid email profile')
    assert (chosen.sso_on, chosen.sso_claims, chosen.sso_client_auth, chosen.sso_require_verified_email) == (False, 'access-token', 'basic', True)
    assert (odd.sso_claims, odd.sso_client_auth) == ('id-token', 'auto')
    assert "sso.claims 'userinfo' is not one of id-token, access-token; using id-token" in caplog.text
    assert "sso.client-auth 'private-key-jwt' is not one of auto, post, basic; using auto" in caplog.text


def test_sso_is_active_only_when_switched_on_and_configured():
    # given
    on = dataclasses.replace(settings(), sso_issuer='https://idp.example.com', sso_client_secret='client-secret', sso_redirect_uri='https://skopax.example.com/authorized')

    # then
    assert SsoConfig.of(on, 'client-secret').active is True
    assert SsoConfig.of(dataclasses.replace(on, sso_on=False), 'client-secret').active is False
    assert SsoConfig.of(on, '').active is False
