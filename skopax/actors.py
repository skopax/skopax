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
CLAUDE_MARKS = ('claude', 'anthropic')
CLAUDE_LABEL = 'Claude investigator'
_source = [lambda: 'claude']


def use_source(read) -> None:
    _source[0] = read


def use_model(model) -> None:
    _source[0] = lambda: model


def investigator_label() -> str:
    try:
        model = _source[0]()
    except Exception:
        model = ''
    if any(m in str(model or '').lower() for m in CLAUDE_MARKS):
        return CLAUDE_LABEL
    from skopax.messages import msg
    return msg('slack.actor.investigator')


def shown(by):
    return investigator_label() if str(by or '').strip() == POD_BY else by
