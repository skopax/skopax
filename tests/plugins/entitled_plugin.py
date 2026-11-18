from skopax.contract import Hooks
from tests.plugins.base import minimal

PLUGIN = minimal('entitled-demo', 'Entitled demo', hooks=Hooks(entitlement=lambda: 'enterprise'))
