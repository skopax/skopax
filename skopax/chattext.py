import html
import re
import string
from collections.abc import Mapping

ENGLISH = 'en'
TEXT_MAX = 500
STARTERS_MAX = 6
LINKISH = re.compile(r'(?i)https?://|www\.|mailto:|xmpp:|[\w.+-]@[\w-]+\.[\w-]|<[a-z][a-z0-9+.-]*:')
MARKED = re.compile(r'\]\(|\]:|!\[|[<>*_`~\\&\r\n]')
LEADING = re.compile(r'[\s#|+-]|\d+[.)]')
TEXT, ENTRIES, PAIRS = 'text', 'entries', 'pairs'
TREE_KEY = re.compile(r'[A-Za-z0-9_-]{1,60}')
TREE_DEPTH = 6
TREE_LEAVES = 1000
TREE_LEAF_MAX = 1000
ACTIONS = {'investigate': 'Investigate', 'resolve': 'Resolve', 'take': 'Take', 'known': 'Mark as known noise', 'mute': 'Mute'}
TEXTS = {
    ENGLISH: {
        'room': 'Waiting for a free room\u2026',
        'halted': 'Waiting: the investigator is {why}.',
        'halted_stopped': 'stopped',
        'halted_capped': 'capped',
        'working': 'Working\u2026',
        'call': 'Working \u00b7 {n} tool call{last}\u2026',
        'calls': 'Working \u00b7 {n} tool calls{last}\u2026',
        'last': ', last {tool}',
        'still': 'Still working; open this chat again in a moment.',
        'answered': 'Answered',
        'none': 'No answer',
        'role_tip': 'Needs the operator role',
        'queued': 'Investigation queued; its findings will be on the card.',
        'done': 'Done: {what}.',
        'starters': (('What needs attention now?', 'Which open cards need attention right now, and why?'),
                     ('Anything unusual in the last hour?', 'Is anything unusual in prod in the last hour?'),
                     ('What did the last investigation find?', 'What did the most recent investigation find, and what is the next step?')),
        'actions': ACTIONS,
        'apply': 'Apply',
        'kinds': {'fixed': 'Fixed', 'duplicate': 'Duplicate of {ref}'},
        'hours': '{n} h',
        'continue': 'This is a snapshot of a shared chat.',
        'continue_link': 'Continue it in your own private chat \u2197',
        'private_share': 'This chat started in a Slack direct message, so it stays private and cannot be shared.',
        'untitled': 'Chat',
        'investigator_off': 'AI investigator is turned off in Admin \u203a Features.',
        'chat_off': 'Chat is turned off in Admin \u203a Features.',
        'feature_off': '{feature} is turned off in Admin \u203a Features.',
        'login': 'Chatting needs an active {product} login with the operator role. Log in again, or ask an admin for the role.',
        'not_ready': 'The AI investigator is not set up yet, so this message would get no answer. {needs}',
        'refused_cap': '{cap} chat messages a day is the cap; it starts again at midnight.',
        'refused_off': 'The chat is switched off.',
        'refused_busy': 'Wait for the answer to your last message.',
        'refused_missing': 'No such chat of yours.',
        'refused_long': 'A message is at most {limit} characters.',
        'refused_empty': 'Write a message first.',
        'refused_role': 'This needs the operator role.',
        'refused_bad': 'That proposal cannot be applied.',
        'refused_failed': 'Not applied: {reason}.',
        'refused': 'That could not be done.',
    },
}


def shape(english: object) -> str:
    return TEXT if isinstance(english, str) else ENTRIES if isinstance(english, Mapping) else PAIRS


def slots(text: str) -> frozenset[str] | None:
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError:
        return None
    if any(name is not None and (conversion is not None or spec) for _, name, spec, conversion in parsed):
        return None
    return frozenset(name for _, name, _, _ in parsed if name is not None)


def filled(text: str, value: str) -> str:
    try:
        return ''.join(literal + ('' if name is None else value) for literal, name, _, _ in string.Formatter().parse(text))
    except ValueError:
        return text


def normal(text: str) -> str:
    return html.unescape(text).replace('\\', '')


def addressed(text: str) -> bool:
    return any(LINKISH.search(shown) for shown in (text, normal(text)))


def linkish(text: str) -> bool:
    return any(addressed(shown) for shown in (text, filled(text, ''), filled(text, 'x')))


def marked(message: str) -> bool:
    return any(MARKED.search(shown) or LEADING.match(shown) for shown in (message, normal(message)))


def text_ok(value: object, english: str) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > TEXT_MAX or linkish(value):
        return False
    found = slots(value)
    return found is not None and found == slots(english)


def pair_ok(pair: object) -> bool:
    return isinstance(pair, (tuple, list)) and len(pair) == 2 and all(isinstance(text, str) and text.strip() and len(text) <= TEXT_MAX and not addressed(text) for text in pair) \
        and not marked(pair[1])


def pairs_ok(value: object) -> bool:
    return isinstance(value, (tuple, list)) and 1 <= len(value) <= STARTERS_MAX and all(pair_ok(pair) for pair in value)


def entry_ok(model: Mapping, entry: object, text: object) -> bool:
    return isinstance(entry, str) and entry in model and text_ok(text, model[entry])


def entry_problem(key: str, value: object) -> tuple[str, str] | None:
    english = TEXTS[ENGLISH]
    if key not in english:
        return 'key', ''
    model = english[key]
    kind = shape(model)
    if kind == TEXT:
        return None if text_ok(value, model) else ('text', '')
    if kind == ENTRIES:
        if not isinstance(value, Mapping):
            return 'map', ''
        stray = [entry for entry, text in value.items() if not entry_ok(model, entry, text)]
        return ('entry', str(stray[0])) if stray else None
    return None if pairs_ok(value) else ('starters', '')


def _kept_value(key: str, value: object) -> tuple[object, list[tuple[str, str, object, int | None]]]:
    english = TEXTS[ENGLISH]
    if key not in english:
        return None, [('key', key, '', None)]
    model = english[key]
    kind = shape(model)
    if kind == TEXT:
        return (value, []) if text_ok(value, model) else (None, [('text', key, '', None)])
    if kind == ENTRIES:
        if not isinstance(value, Mapping):
            return None, [('map', key, '', None)]
        judged = [(entry, text, entry_ok(model, entry, text)) for entry, text in value.items()]
        return {entry: text for entry, text, ok in judged if ok}, [('entry', key, entry, None) for entry, _, ok in judged if not ok]
    if isinstance(value, (tuple, list)) and not value:
        return None, []
    if not isinstance(value, (tuple, list)) or len(value) > STARTERS_MAX:
        return None, [('starters', key, '', None)]
    judged = [(pair, pair_ok(pair)) for pair in value]
    return tuple(pair for pair, ok in judged if ok) or None, [('starters', key, '', index) for index, (_, ok) in enumerate(judged) if not ok]


def key_shown(key: object) -> str:
    return key if isinstance(key, str) else f'<{type(key).__name__}>'


def chat_kept(chat: object) -> tuple[dict[str, object], list[tuple[str, str, object, int | None]]]:
    if not isinstance(chat, Mapping):
        return {}, [('chat', '', '', None)]
    kept: dict[str, object] = {}
    dropped: list[tuple[str, str, object, int | None]] = []
    for key, value in chat.items():
        if not isinstance(key, str):
            dropped.append(('key', key_shown(key), '', None))
            continue
        held, found = _kept_value(key, value)
        if held is not None:
            kept[key] = held
        dropped.extend(found)
    return kept, dropped


def chat_problem(chat: object) -> tuple[str, str, str] | None:
    if not isinstance(chat, Mapping):
        return 'chat', '', ''
    for key, value in chat.items():
        if not isinstance(key, str):
            return 'key', key_shown(key), ''
        found = entry_problem(key, value)
        if found is not None:
            return found[0], key, found[1]
    return None


def _tree_ok(node: object, depth: int, left: list[int]) -> bool:
    if depth > TREE_DEPTH or not isinstance(node, Mapping) or not node:
        return False
    for key, value in node.items():
        if not isinstance(key, str) or not TREE_KEY.fullmatch(key):
            return False
        if isinstance(value, Mapping):
            if not _tree_ok(value, depth + 1, left):
                return False
            continue
        left[0] -= 1
        if left[0] < 0 or not isinstance(value, str) or not value.strip() or len(value) > TREE_LEAF_MAX or linkish(value) or marked_text(value):
            return False
    return True


def marked_text(text: str) -> bool:
    return any(MARKED.search(shown) for shown in (text, normal(text)))


def tree_problem(tree: object) -> bool:
    if isinstance(tree, Mapping) and not tree:
        return False
    return not _tree_ok(tree, 1, [TREE_LEAVES])
