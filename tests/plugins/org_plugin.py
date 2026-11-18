from skopax.contract import OrgPack
from tests.plugins.base import minimal

PROCESS = ('# Shop investigator\n\nYou look into the shop for {product}.\n\n'
           '## Working rules {#how-to-work}\n\nRead the checkout-service logs first.\n\n'
           '## Team notes {#notes}\n\nThe checkout-service team owns payments.\n')
GATE = 'Screen for {product}. Org: {org}.'
SMALL_TALK = 'You are {product}, the shop bot of {org}. Keep it short. '
PACK = OrgPack(process=PROCESS, gate=GATE, small_talk=SMALL_TALK, slack_sections=('notes',))

PLUGIN = minimal('org', 'Org pack', org_pack=PACK)
