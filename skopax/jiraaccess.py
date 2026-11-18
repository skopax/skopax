from skopax.engine import PassResult, anomaly
from skopax.messages import msg

PASS = 'jira-links'
PARTS = ('issues',)
SIG = 'no-access'


def ran_key(part: str) -> str:
    return f'{PASS}:access:{part}'


def access_pass(refused: dict[str, str], plugin: str) -> list[PassResult]:
    found = []
    for part, permission in sorted(refused.items()):
        label = msg('cards.no_access', plugin=plugin, part=part, permission=permission)
        card = anomaly(PASS, SIG, '-', 'jira', part, 'warn', 1, None, None, '', label, label, 1, f'WARN {label}')
        card['ranKey'] = ran_key(part)
        found.append(card)
    status = 'ok' + (' \u00b7 no access to ' + ', '.join(sorted(refused)) if refused else '')
    return [PassResult(PASS, True, status, found, {}, tuple(ran_key(p) for p in sorted(set(PARTS) | set(refused))))]
