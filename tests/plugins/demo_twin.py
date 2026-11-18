import dataclasses

from tests.plugins import demo_plugin

PLUGIN = dataclasses.replace(demo_plugin.PLUGIN, name='Demo twin')
