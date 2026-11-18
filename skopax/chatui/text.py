import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from skopax.chattext import ENGLISH, ENTRIES, TEXT, TEXTS, entry_problem, filled, linkish, shape, slots
from skopax.chatui import LANG_COOKIE, Refused, chainlit_language
from skopax.features import BY_ID as FEATURE_BY_ID
from skopax.jiralinks import listed, site_of, ticket_pattern
from skopax.messages import msg

log = logging.getLogger('skopax.chatui.text')

LINK = re.compile(r'<(https?://[^|>\s]+)\|([^>]+)>')
BARE = re.compile(r'<(https?://[^|>\s]+)>')
BOLD = re.compile(r'(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])')
CARD = re.compile(r'(?<![\w/\[#])#(\d{1,6})\b')
BULLET = re.compile(r'(?m)^(\s*)•\s+')
KEEP = re.compile(r'(`[^`]*`|\[[^\]]*\]\([^)]*\)|https?://[^\s)>\]]+)')
JIRA = {'url': '', 'projects': (), 'cache': {}}
OPEN = ('requested', 'processing')
MARKUP = re.compile(r'([!-/:-@\[-`|~])')
SPACES = re.compile(r'\s+')
REFUSALS = {'cap': 'refused_cap', 'off': 'refused_off', 'busy': 'refused_busy', 'missing': 'refused_missing', 'long': 'refused_long', 'empty': 'refused_empty', 'role': 'refused_role',
            'bad': 'refused_bad', 'failed': 'refused_failed', 'not-ready': 'not_ready'}
FEATURES_OFF = {'investigator': 'investigator_off', 'chat': 'chat_off'}
HALTS = {'stopped': 'halted_stopped', 'capped': 'halted_capped'}
JOINED = (('halted', 'why', 'halted_stopped'), ('halted', 'why', 'halted_capped'), ('call', 'last', 'last'), ('calls', 'last', 'last'))
SAMPLES = ('', 'x')
WARNED: set[str] = set()
WARNED_GUARD = threading.Lock()


def configure(jira_site: str | Callable[[], str], projects) -> None:
    JIRA['url'] = jira_site
    JIRA['projects'] = projects
    JIRA['cache'] = {}


def tickets_pattern() -> re.Pattern | None:
    found = listed(JIRA['projects'])
    if not found:
        return None
    if found not in JIRA['cache']:
        JIRA['cache'] = {found: re.compile(r'(?<![\w/-])(' + ticket_pattern(found).pattern + r')\b')}
    return JIRA['cache'][found]


def linkify(text) -> str:
    pattern = tickets_pattern()
    site = site_of(JIRA['url']) if pattern is not None else ''
    if not site:
        return str(text or '')
    parts = KEEP.split(str(text or ''))
    for i in range(0, len(parts), 2):
        parts[i] = pattern.sub(lambda m: f"[{m.group(1)}]({site}/browse/{m.group(1)})", parts[i])
    return ''.join(parts)


def markdown_of(text) -> str:
    out = LINK.sub(lambda m: f'[{m.group(2)}]({m.group(1)})', str(text or ''))
    out = BARE.sub(lambda m: m.group(1), out)
    out = BOLD.sub(lambda m: f'**{m.group(1)}**', out)
    out = BULLET.sub(lambda m: f'{m.group(1)}- ', out)
    return linkify(CARD.sub(lambda m: f'[#{m.group(1)}](/c/{m.group(1)})', out))


@dataclass(frozen=True)
class Held:
    plain: Mapping[str, Mapping[str, object]]
    markdown: Mapping[str, Mapping[str, str]]
    langs: Mapping[str, str]
    primaries: Mapping[str, str]
    chainlit: Mapping[str, str]


class Markdown(str):
    pass


def escaped(text: str) -> str:
    return MARKUP.sub(r'\\\1', text)


def shown_value(value: object) -> str:
    return value if isinstance(value, Markdown) else escaped(SPACES.sub(' ', str(value)))


def primary(tag: str) -> str:
    return tag.partition('-')[0].lower()


def warn_english(lang: str) -> None:
    with WARNED_GUARD:
        if lang.lower() in WARNED:
            return
        WARNED.add(lang.lower())
    log.warning('%s', msg('locales.chat_dropped', lang=lang, reason=msg('plugins.why.locale_english')))


def warn_joined(lang: str, key: str, other: str) -> None:
    seen = f'{lang.lower()}|{key}|{other}'
    with WARNED_GUARD:
        if seen in WARNED:
            return
        WARNED.add(seen)
    log.warning('%s', msg('locales.chat_joined', lang=lang, key=key, other=other))


def joined(outer: str, field: str, inner: str) -> list[str]:
    return [outer.format(**{name: filled(inner, deep) if name == field else sample for name in slots(outer) or ()}) for sample in SAMPLES for deep in SAMPLES]


def keep_apart(lang: str, held: dict[str, object], shown: dict[str, str], english_shown: Mapping[str, str]) -> None:
    english = TEXTS[ENGLISH]
    while True:
        bad = next(((outer, inner) for outer, field, inner in JOINED if (held[outer], held[inner]) != (english[outer], english[inner])
                    and any(linkish(text) for text in joined(str(held[outer]), field, str(held[inner])))), None)
        if bad is None:
            return
        for key in bad:
            held[key], shown[key] = english[key], english_shown[key]
        warn_joined(lang, *bad)


def built(packs: Iterable[tuple[str, Mapping[str, object]]], trees: Iterable[tuple[str, Mapping[str, object]]] = ()) -> Held:
    english = TEXTS[ENGLISH]
    plain: dict[str, dict[str, object]] = {ENGLISH: dict(english)}
    markdown: dict[str, dict[str, str]] = {ENGLISH: {key: value for key, value in english.items() if shape(value) == TEXT}}
    spelled = {ENGLISH: ENGLISH}
    for lang, chat in packs:
        if not isinstance(chat, Mapping) or not chat:
            continue
        if primary(lang) == ENGLISH:
            warn_english(lang)
            continue
        name = spelled.setdefault(lang.lower(), lang)
        held = plain.setdefault(name, dict(english))
        shown = markdown.setdefault(name, dict(markdown[ENGLISH]))
        for key, value in chat.items():
            if entry_problem(key, value) is not None:
                continue
            kind = shape(english[key])
            if kind == TEXT:
                held[key], shown[key] = value, escaped(SPACES.sub(' ', value))
            elif kind == ENTRIES:
                held[key] = {**held[key], **value}
            else:
                held[key] = tuple(tuple(pair) for pair in value)
    written: dict[str, str] = {}
    for lang, tree in trees:
        if isinstance(tree, Mapping) and tree and isinstance(lang, str) and primary(lang) != ENGLISH:
            name = spelled.setdefault(lang.lower(), lang)
            plain.setdefault(name, dict(english))
            markdown.setdefault(name, dict(markdown[ENGLISH]))
            written[name] = name
    for name in plain:
        if name != ENGLISH:
            keep_apart(name, plain[name], markdown[name], markdown[ENGLISH])
    langs = {name.lower(): name for name in plain}
    primaries: dict[str, str] = {}
    for low in sorted(langs):
        primaries.setdefault(primary(low), langs[low])
    return Held(plain, markdown, langs, primaries, {name: tag for name in plain if (tag := written.get(name) or chainlit_language(name))})


HELD: list[Held] = [built(())]


def set_texts(packs: Iterable[tuple[str, Mapping[str, object]]], trees: Iterable[tuple[str, Mapping[str, object]]] = ()) -> None:
    HELD[0] = built(packs, list(trees))


def reset() -> None:
    HELD[0] = built(())
    with WARNED_GUARD:
        WARNED.clear()


def texts_of(lang: str) -> Mapping[str, object]:
    plain = HELD[0].plain
    return plain.get(lang) or plain[ENGLISH]


def said(lang: str, key: str, **fields) -> str:
    return str(texts_of(lang)[key]).format(**fields)


def said_md(lang: str, key: str, **fields) -> str:
    shown = HELD[0].markdown
    return Markdown((shown.get(lang) or shown[ENGLISH])[key].format(**{name: shown_value(value) for name, value in fields.items()}))


def quality(param: str) -> float | None:
    name, _, value = param.partition('=')
    if name.strip().lower() != 'q':
        return None
    try:
        return float(value)
    except ValueError:
        return None


def wanted(cookies, accepted) -> list[str]:
    tags = [cookie_of(cookies, LANG_COOKIE) or '']
    for part in str(accepted or '').split(','):
        tag, *params = part.split(';')
        if all(quality(param) != 0 for param in params):
            tags.append(tag)
    return [tag.strip().lower() for tag in tags]


def lang_of(cookies, accepted=None) -> str:
    held = HELD[0]
    for tag in wanted(cookies, accepted):
        found = held.langs.get(tag) or held.primaries.get(primary(tag))
        if found:
            return found
    return ENGLISH


def chainlit_of(lang: str) -> str | None:
    return HELD[0].chainlit.get(lang)


def refusal(error: Refused, lang: str) -> str:
    code, fields = (error.code, error.fields) if isinstance(error, Refused) else (None, {})
    feature = fields.get('feature')
    key = FEATURES_OFF.get(feature, 'feature_off') if code == 'feature-off' else REFUSALS.get(code, 'refused')
    named = FEATURE_BY_ID[feature].label if feature in FEATURE_BY_ID else str(feature or '')
    return said_md(lang, key, cap=fields.get('cap'), limit=fields.get('limit'), feature=named, needs=fields.get('needs') or '', reason=fields.get('reason') or '').strip()


def continue_line(lang: str, chat_id: str) -> str:
    return f"{said_md(lang, 'continue')} [{said_md(lang, 'continue_link')}](/chat-continue/{chat_id})"


def pending_line(message: dict, halted: str | None = None, lang: str = 'en') -> str:
    if halted == 'off':
        return said_md(lang, 'investigator_off')
    if halted:
        return said_md(lang, 'halted', why=said_md(lang, HALTS[halted]) if halted in HALTS else halted)
    if message.get('status') == 'requested':
        return said_md(lang, 'room')
    progress = message.get('progress') or {}
    calls = progress.get('toolCalls') or 0
    if not calls:
        return said_md(lang, 'working')
    last = said_md(lang, 'last', tool=progress['lastTool']) if progress.get('lastTool') else ''
    return said_md(lang, 'call' if calls == 1 else 'calls', n=calls, last=last)


def done_line(message: dict, lang: str = 'en') -> str:
    bits = [said_md(lang, 'answered' if message.get('answer') else 'none')]
    if message.get('usd') is not None:
        bits.append(f"${float(message['usd']):.2f}")
    return ' · '.join(bits)


def answer_of(message: dict, lang: str = 'en') -> str:
    if message.get('answer'):
        return markdown_of(message['answer'])
    return said_md(lang, 'none') + (f": {shown_value(message['error'])}" if message.get('error') else '') + '.'


def action_label(card_action: dict, card: dict | None, lang: str = 'en') -> str:
    name = (f"#{card['num']} " if card and card.get('num') else '') + (f"{card.get('svc') or '-'} / {card.get('label') or '?'}" if card else str(card_action.get('key') or ''))
    texts = texts_of(lang)
    verb = texts['actions'].get(card_action.get('action')) or texts['apply']
    kind = texts['kinds'].get(card_action.get('kind')) if card_action.get('action') == 'resolve' else None
    verb += ' \u00b7 ' + kind.format(ref=card_action.get('ref') or '?') if kind else ''
    return f"{verb}{' ' + said(lang, 'hours', n=card_action.get('hours') or 24) if card_action.get('action') == 'mute' else ''}: {name}"


SEV_MARK = {'crit': '🔴', 'warn': '🟠', 'info': '🔵'}
CONTEXT_LIMIT = 400


def clipped(text, limit: int = CONTEXT_LIMIT) -> str:
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def about_card(c: dict) -> str:
    mark = '✅' if c.get('status') == 'cleared' else SEV_MARK.get(c.get('sev'), '')
    name = f"[#{c['num']}](/c/{c['num']}) · {c.get('title') or ''}" if c.get('num') else str(c.get('title') or c.get('key') or '')
    lines = ['📌 ' + (mark + ' ' if mark else '') + f'**{name}**']
    if c.get('what'):
        lines.append('> ' + linkify(clipped(c['what'])))
    if c.get('next') or c.get('note'):
        lines.append('→ ' + linkify(clipped(c.get('next') or c.get('note'))))
    who = ' · '.join(str(x) for x in ((f"👤 {str(c['owner']).split('@')[0]}" if c.get('owner') else ''), linkify(c.get('ticket') or '')) if x)
    if who:
        lines.append(who)
    return '\n\n'.join(lines)


def about_line(cards: list[dict]) -> str:
    return '\n\n'.join(about_card(c) for c in cards)


def card_ref(referer) -> str | None:
    refs = parse_qs(urlsplit(str(referer or '')).query).get('card') or []
    return refs[0][:500] if refs and refs[0] else None


def cookie_of(header, name: str) -> str | None:
    for part in str(header or '').split(';'):
        key, _, value = part.strip().partition('=')
        if key == name and value:
            return unquote(value)
    return None
