import copy
import fnmatch
import json
import logging
import re
import time
from collections.abc import Callable

from skopax.bounded import HOOK_TIMEOUT_S, Bound, Busy, TimedOut
from skopax.errkind import Authored, kind as error_kind

log = logging.getLogger('skopax.tags')

DEVELOPER = 'developer'
DATA = 'data'
INFRA = 'infra'
ANALYST = 'analyst'
TECHNICAL = (DEVELOPER, DATA, INFRA, ANALYST)
TAGS: dict[str, str] = {DEVELOPER: 'Developer', DATA: 'Data', INFRA: 'Infrastructure', ANALYST: 'Analyst'}
TAG_ID = re.compile(r'[a-z0-9-]{1,30}')
LABEL_MAX = 40
RULES: tuple = ()
RULE = 'rule'
POD = 'pod'
FIELDS = ('tags', 'tagsOff', 'business', 'tagsAt')
TEXT_LIMIT = 500
INFO = 'info'
KINDS = ('action', INFO)
WHY_LIMIT = 300
RULES_KEY = re.compile(r'tags\.([a-z0-9-]{1,30})\.rules')
EXTRA: list = [{}]
DEFS: list = [{}]
TEXTS: list = [(), HOOK_TIMEOUT_S]
TEXT_CALLS = Bound('card-text')
SKIPPED = object()


class Refused(Authored, Exception):
    pass


def use_rules(extra: dict[str, tuple[str, ...]] | Callable[[], dict[str, tuple[str, ...]]]) -> None:
    EXTRA[0] = extra


def merged(base: tuple, extra: dict[str, tuple[str, ...]]) -> tuple:
    out = {tag: tuple(terms) for tag, terms in base}
    for tag, terms in extra.items():
        out[tag] = tuple(dict.fromkeys((*out.get(tag, ()), *terms)))
    return tuple(out.items())


def rules(given: tuple | None = None) -> tuple:
    plugged = EXTRA[0]
    try:
        extra = plugged() if callable(plugged) else plugged
    except Exception as e:
        log.warning('plug-in tag rules not read, using the defaults (%s)', type(e).__name__)
        extra = {}
    known = all_tags()
    return merged(tuple((tag, terms) for tag, terms in (given or RULES) if tag in known), {tag: terms for tag, terms in extra.items() if tag in known})


def plugin_rules() -> dict[str, tuple[str, ...]]:
    plugged = EXTRA[0]
    try:
        extra = plugged() if callable(plugged) else plugged
    except Exception as e:
        log.warning('plug-in tag rules not read (%s)', type(e).__name__)
        return {}
    known = all_tags()
    return {tag: tuple(terms) for tag, terms in extra.items() if tag in known}


def clean_label(raw) -> str:
    return ' '.join(str(raw or '').split())[:LABEL_MAX].strip()


def use_tags(defs: dict[str, str] | Callable[[], dict[str, str]]) -> None:
    DEFS[0] = defs


def plugin_tags() -> dict[str, str]:
    given = DEFS[0]
    try:
        found = given() if callable(given) else given
    except Exception as e:
        log.warning('plug-in tags not read (%s)', type(e).__name__)
        return {}
    return {k: v for k, v in found.items() if k not in TAGS}


def all_tags() -> dict[str, str]:
    return {**TAGS, **plugin_tags()}


def info() -> list[dict]:
    return [{'id': k, 'label': v} for k, v in all_tags().items()]


def label(tag: str) -> str:
    return all_tags().get(tag, tag)


def ids(card: dict) -> list[str]:
    return [t.get('id') for t in card.get('tags') or [] if isinstance(t, dict)]


def clip(text, limit: int) -> str:
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def by_person(by) -> bool:
    return bool(by) and by not in (RULE, POD)


def hits(term: str, card: dict) -> bool:
    if term == 'gone':
        return bool(card.get('gone'))
    if term.startswith('type:'):
        return any(fnmatch.fnmatchcase(str(card.get(f) or ''), term[5:]) for f in ('sig', 'src'))
    return fnmatch.fnmatchcase(str(card.get('key') or ''), term)


def rules_of(config: dict[str, str]) -> tuple:
    given = {}
    for key, value in config.items():
        m = RULES_KEY.fullmatch(key)
        if m and m[1] in all_tags():
            given[m[1]] = tuple(p.strip() for p in str(value or '').split(',') if p.strip())
    return tuple((tag, given.get(tag, terms)) for tag, terms in RULES)


def use_texts(hooks: list[tuple[str, Callable[[dict], str | None]]] | Callable[[], list[tuple[str, Callable[[dict], str | None]]]], timeout: float = HOOK_TIMEOUT_S) -> None:
    TEXTS[0], TEXTS[1] = hooks, timeout


def text_hooks() -> list[tuple[str, Callable[[dict], str | None]]] | None:
    plugged = TEXTS[0]
    try:
        return list(plugged() if callable(plugged) else plugged)
    except Exception as e:
        log.warning('plug-in card texts not read, rule sentences stay as they are (%s)', error_kind(e))
        return None


def asked_each(hook: Callable[[dict], str | None], cards: list[dict], said: list, end: float) -> None:
    for card in cards:
        if time.monotonic() >= end:
            return
        try:
            said.append((True, hook(copy.deepcopy(card))))
        except Exception as e:
            said.append((False, e))


def warn_text(owner: str, failed: set[str], why: str) -> None:
    if owner not in failed:
        failed.add(owner)
        log.warning('plug-in %s: %s', owner, why)


def rule_texts(cards: list[dict], hooks: list[tuple[str, Callable[[dict], str | None]]] | None, end: float) -> list[object]:
    if hooks is None:
        return [SKIPPED] * len(cards)
    out: list[object] = [None] * len(cards)
    waiting = list(range(len(cards)))
    failed: set[str] = set()
    for owner, hook in hooks:
        if not waiting:
            break
        left = end - time.monotonic()
        if left <= 0:
            for at in waiting:
                out[at] = SKIPPED
            return out
        said: list = []
        try:
            TEXT_CALLS.call(owner, lambda hook=hook, given=[cards[at] for at in waiting], said=said: asked_each(hook, given, said, end), left, wait=True, stuck=TEXTS[1])
            stopped = TimedOut()
        except Exception as e:
            stopped = e
        answers = list(said)
        still = []
        for n, at in enumerate(waiting):
            if n >= len(answers):
                warn_text(owner, failed, f"card text skipped, another pass held it until this pass's deadline ({error_kind(stopped)})" if isinstance(stopped, Busy)
                          else f'card text failed, the sentences it writes stay as they are this pass ({error_kind(stopped)})')
                out[at] = SKIPPED
                continue
            ok, value = answers[n]
            if not ok:
                warn_text(owner, failed, f'card text failed, the sentences it writes stay as they are this pass ({error_kind(value)})')
                out[at] = SKIPPED
            elif value is None:
                still.append(at)
            elif not isinstance(value, str):
                warn_text(owner, failed, f'card text answered {type(value).__name__} and not text, the sentences it writes stay as they are this pass')
                out[at] = SKIPPED
            elif text := clip(value, TEXT_LIMIT):
                out[at] = text
            else:
                still.append(at)
        waiting = still
    return out


def _ruled(a: dict, rules: tuple, at: str) -> None:
    off = set(a.get('tagsOff') or [])
    tags = [t for t in a.get('tags') or [] if isinstance(t, dict)]
    kept = [t for t in tags if t.get('by') != RULE]
    have = {t.get('id') for t in kept}
    before = {t.get('id'): t for t in tags if t.get('by') == RULE}
    ruled = [before.get(tag) or {'id': tag, 'by': RULE, 'at': at} for tag, terms in rules
             if tag not in off and tag not in have and any(hits(term, a) for term in terms)]
    if kept + ruled != tags:
        a['tags'] = kept + ruled


def _wants_text(a: dict) -> bool:
    said = a.get('business') or {}
    return a.get('status') != 'cleared' and isinstance(said, dict) and (not said or said.get('by') == RULE)


def _seen_as(a: dict) -> str:
    return json.dumps(a, sort_keys=True, default=str)


def card_texts(cards: list[dict], rules: tuple, at: str) -> dict[str, tuple[str, object]]:
    return texts_with(cards, rules, at, text_hooks() if cards else [])


def texts_with(cards: list[dict], rules: tuple, at: str, hooks: list[tuple[str, Callable[[dict], str | None]]] | None) -> dict[str, tuple[str, object]]:
    wanted = []
    for a in copy.deepcopy(cards):
        _ruled(a, rules, at)
        if _wants_text(a):
            wanted.append(a)
    said = rule_texts(wanted, hooks, time.monotonic() + TEXTS[1])
    return {a['key']: (_seen_as(a), text) for a, text in zip(wanted, said)}


def apply_rules(cards: list[dict], rules: tuple, at: str, texts: dict[str, tuple[str, object]] | None = None) -> None:
    if texts is None:
        texts = card_texts(cards, rules, at)
    for a in cards:
        _ruled(a, rules, at)
        if not _wants_text(a):
            continue
        seen, text = texts.get(a.get('key'), (None, SKIPPED))
        if text is SKIPPED or seen != _seen_as(a):
            continue
        said = a.get('business') or {}
        if text and said.get('text') != text:
            a['business'] = {'text': text, 'by': RULE, 'at': at}
        elif not text and said:
            a['business'] = None


def toggled(card: dict, tag: str, on: bool, by: str, at: str) -> dict:
    if tag not in all_tags():
        raise Refused(f'no tag named {tag}; tags are ' + ', '.join(all_tags()))
    tags = [t for t in card.get('tags') or [] if isinstance(t, dict) and t.get('id') != tag]
    off = [x for x in card.get('tagsOff') or [] if x != tag]
    if on:
        tags.append({'id': tag, 'by': by, 'at': at})
    else:
        off.append(tag)
    return {'tags': tags, 'tagsOff': off, 'tagsAt': at}


def added(card: dict, wanted: list[str], by: str, at: str) -> dict:
    known = all_tags()
    unknown = [t for t in wanted if t not in known]
    if unknown:
        raise Refused(f"no tag named {', '.join(unknown)}; tags are " + ', '.join(known))
    removed = [t for t in wanted if t in (card.get('tagsOff') or [])]
    if removed:
        raise Refused(f"a person removed {', '.join(removed)} from this card; leave it off")
    tags = [t for t in card.get('tags') or [] if isinstance(t, dict)]
    have = {t.get('id') for t in tags}
    return {'tags': tags + [{'id': t, 'by': by, 'at': at} for t in wanted if t not in have], 'tagsAt': at}


def business(text, by: str, at: str, why=None, kind=None) -> dict:
    text, why = clip(text, TEXT_LIMIT), clip(why, WHY_LIMIT)
    return {'business': ({'text': text, 'by': by, 'at': at} | ({'why': why} if why else {}) | ({'kind': INFO} if kind == INFO else {})) if text else None, 'tagsAt': at}
