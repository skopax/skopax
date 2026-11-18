from skopax.contract import Hooks
from tests.plugins.base import minimal

EDITION = 'enterprise'


def entitlement():
    return EDITION


PLUGIN = minimal('switchable-ent', 'Switchable', hooks=Hooks(entitlement=entitlement))
