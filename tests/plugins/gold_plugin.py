from skopax.contract import Hooks
from tests.plugins.base import minimal

PLUGIN = minimal('gold-demo', 'Gold demo', hooks=Hooks(entitlement=lambda: 'gold'))
