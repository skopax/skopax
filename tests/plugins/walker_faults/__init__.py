from skopax.contract import RuleSet
from tests.plugins.base import minimal

PLUGIN = minimal('walker-faults', 'Walker faults', rules=(RuleSet('rates', 'Rates', {'err': float('nan')}, {'err': 'error lines above the floor', 'gone': 'a threshold with no default'}),))
