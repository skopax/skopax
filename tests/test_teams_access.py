import json

from skopax.messages import msg
from skopax.store import Store
from tests.test_teams_plugin import checked, refusing

FORBIDDEN = ('Teams refused to post to the channel (HTTP 403): the Workflows URL needs a workflow that is switched on, that anyone may trigger, '
             'and whose owner may post in that channel')


def test_a_refused_test_post_names_the_part_and_what_the_workflows_url_needs(store: Store):
    # when
    forbidden = checked(store, refusing(403))
    rejected = checked(store, refusing(401))

    # then
    assert (forbidden['ok'], forbidden['summary']) == (False, FORBIDDEN)
    assert (rejected['ok'], rejected['summary']) == (False, msg('connection.rejected', status=401))
    assert 'sig=' not in json.dumps([forbidden, rejected])
