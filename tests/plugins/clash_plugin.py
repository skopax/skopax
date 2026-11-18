from skopax.contract import Knowledge, Tool
from tests.plugins.base import minimal


def run(ctx, args):
    return {}


PLUGIN = minimal('clash', 'Clash', tools=(Tool('card', 'Shadows a core tool', {'type': 'object', 'properties': {}}, run, instance_input=None),),
                 knowledge=(Knowledge('how-to-work', 'Shadows a core section', 'x'),))
