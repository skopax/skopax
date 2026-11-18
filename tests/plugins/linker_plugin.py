from skopax.contract import Field
from tests.plugins.base import minimal

PLAN: dict = {}
LOGS = {'label': 'Logs', 'url': 'https://logs.example.com/x'}


def reset() -> None:
    PLAN.clear()


def links(card, instance):
    if PLAN.get('raise'):
        raise RuntimeError('linker down')
    if 'only' in PLAN:
        return PLAN['only']
    return [dict(LOGS)] + [dict(x) for x in PLAN.get('extra', ())]


PLUGIN = minimal('linker', 'Linker', fields=(Field('url', 'URL', 'url'),), links=links)
