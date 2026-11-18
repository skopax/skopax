import os

import mongomock
from fastapi.testclient import TestClient

from skopax import app as app_module, settingsources
from skopax.config import NEW_ENV_PREFIX, settings_env_names
from skopax.kube import Kube
from skopax.settingsources import SourceSet
from skopax.store import Store
from tests.agent_kit import Feed, feed_plugin, run_main
from tests.conftest import RELEASE_IGNORE
from tests.plugin_kit import registry_with
from tests.plugins import demo_plugin


def clear_settings_env(monkeypatch) -> None:
    known = settings_env_names()
    for name in list(os.environ):
        if name in known or name.startswith(NEW_ENV_PREFIX) or name == 'DEMO_URL':
            monkeypatch.delenv(name)


def test_main_mode_hands_its_own_boot_to_create_app(monkeypatch, tmp_path):
    # given
    clear_settings_env(monkeypatch)
    booted, made = [], []
    real_boot, real_create = settingsources.boot, app_module.create_app

    def spy_boot(*args, **kwargs):
        booted.append(real_boot(*args, **kwargs))
        return booted[-1]

    def spy_create(*args, **kwargs):
        made.append((args, dict(kwargs)))
        store = Store(mongomock.MongoClient(tz_aware=True)['skopax'], release_ignore=RELEASE_IGNORE, self_deploy='platform/shop')
        return real_create(*args, store=store, kube=Kube(root=tmp_path), **kwargs)

    monkeypatch.setattr(settingsources, 'boot', spy_boot)
    monkeypatch.setattr(app_module, 'create_app', spy_create)

    # when
    app = run_main(monkeypatch, registry_with(demo_plugin, feed_plugin(Feed())))['app']
    with TestClient(app) as client:
        health = client.get('/actuator/health').status_code

    # then
    assert not set(os.environ) & settings_env_names()
    assert len(booted) == 1 and len(made) == 1
    (args, kwargs), b = made[0], booted[0]
    assert args == (b.settings,) and kwargs['registry'] is b.registry
    assert isinstance(kwargs['sources'], SourceSet) and [label for label, _ in kwargs['sources'].loaders] == ['environment', 'config file', 'Agent feed', 'Demo']
    assert health == 200

