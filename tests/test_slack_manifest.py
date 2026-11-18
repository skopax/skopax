from pathlib import Path

import yaml

from skopax.plugins.slack import LISTENER_SCOPES, READ_SCOPES
from skopax.slack import REACTIONS_SCOPE

MANIFEST = Path(__file__).resolve().parent.parent / 'examples' / 'slack-app-manifest.yaml'


def test_the_example_slack_manifest_grants_every_scope_skopax_checks_for():
    # given
    manifest = yaml.safe_load(MANIFEST.read_text(encoding='utf-8'))

    # when
    granted = set(manifest['oauth_config']['scopes']['bot'])
    families = READ_SCOPES + LISTENER_SCOPES + ((REACTIONS_SCOPE,), ('im:history',), ('app_mentions:read',))

    # then
    assert [family for family in families if not set(family) <= granted] == []
    assert len(granted) == len(manifest['oauth_config']['scopes']['bot'])


def test_the_example_slack_manifest_uses_socket_mode_with_the_events_the_listener_handles():
    # given
    manifest = yaml.safe_load(MANIFEST.read_text(encoding='utf-8'))

    # when
    settings = manifest['settings']

    # then
    assert settings['socket_mode_enabled'] is True and settings['interactivity']['is_enabled'] is True
    assert sorted(settings['event_subscriptions']['bot_events']) == ['app_mention', 'message.channels', 'message.groups', 'message.im']
    assert 'request_url' not in settings['event_subscriptions'] and 'request_url' not in settings['interactivity']
