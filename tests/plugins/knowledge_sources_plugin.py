from skopax.contract import Knowledge
from tests.plugins.base import minimal

PLUGIN = minimal('knowledge-sources-notes', 'Knowledge sources notes', knowledge=(Knowledge('knowledge-sources', 'Shadows the knowledge source section', 'x'),))
