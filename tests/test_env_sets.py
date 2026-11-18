import logging

import pytest

from skopax.kit import env as env_kit
from skopax.settingsources import boot

SITE = 'https://example.atlassian.net'


@pytest.mark.parametrize('env, plugin, missing', [
    ({'GITLAB_TOKEN': 'glpat'}, 'gitlab', 'GITLAB_GROUP'),
    ({'JIRA_URL': SITE, 'JIRA_API_TOKEN': 'jt'}, 'jira', 'JIRA_EMAIL'),
    ({'CONFLUENCE_URL': SITE, 'CONFLUENCE_API_TOKEN': 'ct'}, 'confluence', 'CONFLUENCE_EMAIL'),
    ({'CONFLUENCE_EMAIL': 'bot@example.com'}, 'confluence', 'CONFLUENCE_URL, CONFLUENCE_API_TOKEN'),
])
def test_a_partial_env_set_starts_without_its_instance_and_warns_once_naming_what_is_missing(env, plugin, missing, caplog, monkeypatch):
    # given
    monkeypatch.setattr(env_kit, 'WARNED', set())

    # when
    with caplog.at_level(logging.WARNING):
        first = boot(env)
        boot(env)

    # then
    rows = [r for r in first.sources[0].instances if r['plugin'] == plugin]
    warned = [r.getMessage() for r in caplog.records if 'environment' in r.getMessage() and missing in r.getMessage()]
    assert rows == [] and len(warned) == 1, [r.getMessage() for r in caplog.records]
    assert not any(v in ' '.join(warned) for v in env.values() if v != SITE and '@' not in v), 'the warning names variables, never their values'


@pytest.mark.parametrize('env', [{'GITLAB_URL': 'https://gitlab.example.com'}, {'JIRA_CLOUD_ID': 'cloud-1'}, {'CONFLUENCE_SPACES': 'OPS'}])
def test_an_optional_variable_alone_gives_no_instance_and_no_warning(env, caplog, monkeypatch):
    # given
    monkeypatch.setattr(env_kit, 'WARNED', set())

    # when
    with caplog.at_level(logging.WARNING):
        booted = boot(env)

    # then
    assert [r['plugin'] for r in booted.sources[0].instances] == []
    assert [r.getMessage() for r in caplog.records if 'environment' in r.getMessage()] == []
