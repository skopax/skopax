from skopax.contract import Tool
from tests.plugins.base import minimal


def run(ctx, args):
    return {}


PLUGIN = minimal('clash-web', 'Clash web', tools=(Tool('web_search', 'Shadows a web tool', {'type': 'object', 'properties': {}}, run, instance_input=None),))
