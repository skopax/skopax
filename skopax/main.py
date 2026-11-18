import os

from skopax.logs import setup

setup()

from skopax.agent import agent_mode

if agent_mode(os.environ):
    from skopax.agent import create_agent
    from skopax.settingsources import light_boot

    app = create_agent(light_boot())
else:
    from skopax.app import create_app
    from skopax.settingsources import boot

    b = boot()
    app = create_app(b.settings, registry=b.registry, sources=b.source_set())
