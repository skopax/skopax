SYSTEM = 'system'
TOKEN_PREFIX = 'token:'
LEGACY_TOKEN = 'token'
BOOTSTRAP_TOKEN = 'token:bootstrap'


def is_token(by) -> bool:
    text = str(by or '')
    return text == LEGACY_TOKEN or text.startswith(TOKEN_PREFIX)


def token_label(by, legacy: str = 'Investigator') -> str:
    text = str(by or '')
    if not text.startswith(TOKEN_PREFIX):
        return legacy
    return text[len(TOKEN_PREFIX):] or legacy


POD_BY = 'Claude (pod)'


def investigator_label() -> str:
    from skopax.messages import msg
    return msg('slack.actor.investigator')


def shown(by):
    return investigator_label() if str(by or '').strip() == POD_BY else by
