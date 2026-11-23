import dataclasses
import json
import re
import warnings
from collections import Counter

import pytest
import regex

from skopax import chatui, locales, messages
from skopax.chattext import TEXTS, chat_kept, tree_problem
from skopax.chatui.text import built, lang_of, said
from skopax.contract import Locale
from skopax.investigator import prompt
from skopax.messages import msg
from skopax.registry import DAYS, MONTHS, WRITING_MAX, kept_packs
from tests.plugins.base import minimal
from tools import locale_source
from tests.test_plugin_hooks import served

LANGS = locales.langs()
SCRIPT = re.compile(r'<script src="([^"?]+)(?:\?v=\w+)?"></script>')
PLACEHOLDER = re.compile(r'\{(?:\d+|M|D|[A-Za-z_]\w*)\}')
GROUP_REF = re.compile(r'\$([1-9])')
PYTHON_ONLY = (('(?P<', 'named group'), ('(?P=', 'named backreference'), ('(?P>', 'named subpattern call'), ('(?#', 'comment group'), ('(?>', 'atomic group'), ('(?(', 'conditional group'), ('(?|', 'branch reset group'))
PYTHON_ESCAPES = {'A': 'anchor \\A', 'Z': 'anchor \\Z', 'z': 'anchor \\z', 'G': 'anchor \\G', 'K': 'match reset \\K'}
INLINE_FLAGS = re.compile(r'[aiLmsux-]+[:)]')
COUNTS = re.compile(r'\{\d+(?:,\d*)?\}')
OPEN_COUNT = re.compile(r'\{,\d+\}')
MAIL_HELP = 'Preferences > Email: Email me my notifications is on for every kind once an admin sets up a Mail server. Switch it off, or untick the kinds you do not want. Mail goes only to the address of your own account.'


def core(lang: str) -> Locale:
    return next(pack for pack in locales.packs() if pack.lang == lang)


def tree_texts(tree) -> int:
    return sum(tree_texts(value) if isinstance(value, dict) else 1 for value in tree.values())


def leaves(tree, path=()) -> list[tuple[str, ...]]:
    return [found for key, value in tree.items() for found in (leaves(value, path + (key,)) if isinstance(value, dict) else [path + (key,)])]


def at(tree, path):
    for key in path:
        tree = tree[key]
    return tree


def nested(path, text) -> dict:
    return {path[0]: nested(path[1:], text)} if len(path) > 1 else {path[0]: text}


def outbound() -> set[str]:
    return {key for key in messages.MESSAGES if messages.outbound(key)}


def placeholder_problems(pack: Locale) -> list[str]:
    texts = list(pack.page.items()) + list(pack.headers.items())
    return [key for key, value in texts if Counter(PLACEHOLDER.findall(key)) != Counter(PLACEHOLDER.findall(value))]


def browser_problem(pattern: str) -> str | None:
    at, quantified = 0, False
    while at < len(pattern):
        ch = pattern[at]
        if ch == '\\':
            if pattern[at + 1:at + 2] in PYTHON_ESCAPES:
                return PYTHON_ESCAPES[pattern[at + 1]]
            at, quantified = at + 2, False
            continue
        if ch == '[':
            at += 2 if pattern[at + 1:at + 2] == '^' else 1
            if pattern[at:at + 1] == ']':
                return 'class starting with ]'
            while at < len(pattern) and pattern[at] != ']':
                at += 2 if pattern[at] == '\\' else 1
            at, quantified = at + 1, False
            continue
        if ch == '(' and pattern[at + 1:at + 2] == '?':
            rest = pattern[at:]
            found = next((name for start, name in PYTHON_ONLY if rest.startswith(start)), None)
            if found:
                return found
            if INLINE_FLAGS.match(pattern, at + 2):
                return 'inline flags'
            at, quantified = at + 2, False
            continue
        if ch == '{' and OPEN_COUNT.match(pattern, at):
            return 'count with no lower bound'
        counted = COUNTS.match(pattern, at) if ch == '{' else None
        if ch in '*+?' or counted:
            if quantified and ch == '+':
                return 'possessive quantifier'
            at, quantified = (counted.end() if counted else at + 1), True
            continue
        at, quantified = at + 1, False
    return None


def rule_problems(pack: Locale) -> list[tuple[str, str]]:
    found = []
    for pattern, replacement in pack.rules:
        why = browser_problem(pattern)
        if why is None and any(int(n) > regex.compile(pattern).groups for n in GROUP_REF.findall(replacement)):
            why = 'group reference past the last group'
        if why is not None:
            found.append((pattern, why))
    return found


def test_tr_ships_as_a_core_pack():
    # then
    assert 'tr' in LANGS
    assert all(pack.lang in LANGS for pack in locales.packs())


def test_a_missing_outbound_translation_warns_and_sends_english_instead_of_failing(monkeypatch):
    # given
    real = locales.read('tr')
    key = sorted(outbound())[0]
    monkeypatch.setattr(locales, 'read', lambda lang: dataclasses.replace(real, messages={k: v for k, v in real.messages.items() if k != key}))

    # when
    with pytest.warns(UserWarning, match=f'tr pack has no translation for 1 outbound messages, English is sent: {re.escape(key)}'):
        test_every_core_pack_passes_the_plugin_pack_checks_with_no_text_dropped('tr')
    messages.set_catalogs([('tr', locales.read('tr').messages)])

    # then
    assert key not in messages.catalog('tr')


@pytest.mark.parametrize('lang', LANGS)
def test_every_core_pack_passes_the_plugin_pack_checks_with_no_text_dropped(lang):
    # given
    raw = locales.read(lang)

    # when
    kept, drops, counts = kept_packs('core', [raw])
    chat, found = chat_kept(raw.chat)
    dropped = messages.set_catalogs([(lang, raw.messages)])
    shown = built([(lang, raw.chat)]).plain[lang] if raw.chat else {}

    # then
    assert drops == [] and counts == {} and [dataclasses.replace(k, chat={}) for k in kept] == [dataclasses.replace(raw, chat={})]
    assert found == [] and set(chat) == set(raw.chat)
    assert dropped == []
    assert all(shown[key] == (value if isinstance(value, str) else shown[key]) for key, value in raw.chat.items()), 'no joined chat text falls back to English'
    assert not tree_problem(raw.chainlit)
    assert len(raw.writing) <= WRITING_MAX and raw.writing.strip()
    assert raw.page and raw.messages and raw.chat
    assert set(raw.messages) <= outbound() and set(raw.chat) <= set(TEXTS['en'])
    missing = sorted(outbound() - set(raw.messages))
    if missing:
        warnings.warn(f'{lang} pack has no translation for {len(missing)} outbound messages, English is sent: {", ".join(missing)}', stacklevel=1)
    assert placeholder_problems(raw) == []
    assert rule_problems(raw) == []


@pytest.mark.parametrize(('page', 'problem'), [
    ({'Seen {0} times': 'Vu fois'}, ['Seen {0} times']),
    ({'Since {0} {M}': 'Depuis {0} {D}'}, ['Since {0} {M}']),
    ({'Hi {name}': 'Salut {who}'}, ['Hi {name}']),
    ({'{0} of {1}': '{0} sur {0}'}, ['{0} of {1}']),
    ({'Seen {0} times on {M}': '{M} : vu {0} fois'}, []),
])
def test_the_gate_catches_a_page_text_whose_placeholders_differ_from_its_key(page, problem):
    # given
    pack = Locale('xx', page=page)

    # when
    found = placeholder_problems(pack)

    # then
    assert found == problem


@pytest.mark.parametrize(('pattern', 'replacement', 'why'), [
    (r'^Takes (.+)\.$', 'Prend $2.', 'group reference past the last group'),
    (r'^(?P<n>\d+) seen$', '$1 vus', 'named group'),
    (r'^(\d+) and (?P=n)$', '$1', 'named backreference'),
    (r'^(?#note)(\d+)$', '$1', 'comment group'),
    (r'\A(\d+) seen$', '$1', 'anchor \\A'),
    (r'^(\d+) seen\Z', '$1', 'anchor \\Z'),
    (r'^(?>\d+) seen$', 'vu', 'atomic group'),
    (r'^(\d++) seen$', '$1', 'possessive quantifier'),
    (r'^(\d{2}+) seen$', '$1', 'possessive quantifier'),
    (r'^(\d+) (?i)seen$', '$1', 'inline flags'),
    (r'^(?i:seen) (\d+)$', '$1', 'inline flags'),
    (r'^(?P>n) seen$', 'vu', 'named subpattern call'),
    (r'^(a)?(?(1)b|c) seen$', 'vu', 'conditional group'),
    (r'^(?|(a)|(b)) seen$', '$1', 'branch reset group'),
    (r'^(\d+) seen\z', '$1', 'anchor \\z'),
    (r'\G(\d+) seen$', '$1', 'anchor \\G'),
    (r'^seen \K(\d+)$', '$1', 'match reset \\K'),
    (r'^([]a]+) seen$', '$1', 'class starting with ]'),
    (r'^([^]a]+) seen$', '$1', 'class starting with ]'),
    (r'^(\d{,3}) seen$', '$1', 'count with no lower bound'),
    (r'^(\d+)\+\+ seen [+*]+$', '$1', None),
    (r'^(\d{2,}) seen [a\]]$', '$1', None),
    (r'^(\d+) seen (?:twice|\(?once\)?)$', '$1', None),
])
def test_the_gate_catches_a_rule_the_browser_cannot_run_the_same_way(pattern, replacement, why):
    # given
    pack = Locale('xx', rules=((pattern, replacement),))

    # when
    found = rule_problems(pack)

    # then
    assert found == ([] if why is None else [(pattern, why)])


@pytest.mark.parametrize('lang', LANGS)
def test_a_core_pack_carries_a_chainlit_tree_only_when_chainlit_has_no_translation_of_its_own(lang):
    # given
    raw = locales.read(lang)
    bundled = any(chatui.bundled_translation(name) for name in chatui.translation_names(lang))

    # then
    assert bool(raw.chainlit) != bundled
    assert not raw.chainlit or tree_texts(raw.chainlit) > 0


def test_core_packs_are_on_with_no_plugin_on_every_surface(store):
    # given
    pack = core('tr')
    region = chatui.translation_names('tr')[1]
    key = next(iter(pack.messages))
    chat_key = next(k for k, v in pack.chat.items() if isinstance(v, str))

    # when
    with served(store, add=False) as client:
        page = SCRIPT.findall(client.get('/').text)
        card = SCRIPT.findall(client.get('/c/1').text)
        script = client.get('/static/i18n-tr.js')
        with messages.speaking('tr'):
            sent = msg(key)
        languages = messages.languages()
        chat = said('tr', chat_key) if '{' not in pack.chat[chat_key] else pack.chat[chat_key]
        picked = lang_of({}, f'{region},tr;q=0.9,en;q=0.5')
        written = prompt.writing_languages()
        files = dict(chatui.TRANSLATIONS)

    # then
    assert page.index('/static/i18n.js') < page.index('/static/i18n-tr.js') < page.index('/static/core.js')
    assert card.index('/static/i18n.js') < card.index('/static/i18n-tr.js') < card.index('/static/card.js')
    assert script.status_code == 200 and script.headers['content-type'].startswith('text/javascript')
    assert script.text == locales.CORE_PACK % ('"tr"', json.dumps(locales.page_of(pack)))
    assert sent == pack.messages[key] and 'tr' in languages
    assert chat == pack.chat[chat_key] and picked == 'tr'
    assert 'tr' in written
    assert files['tr'][1] == pack.chainlit and files[region][1] == pack.chainlit


def test_a_plugin_pack_wins_text_by_text_over_the_core_pack(store):
    # given
    pack = core('tr')
    mine, other = list(pack.messages)[:2]
    chat_mine, chat_other = [k for k, v in pack.chat.items() if isinstance(v, str) and '{' not in v][:2]
    mine_leaf, *other_leaves = leaves(pack.chainlit)
    added = Locale('tr', page={'Open': 'Plugin open'}, messages={mine: 'Plugin message'}, chat={chat_mine: 'Plugin chat'}, writing='Plugin writing note.',
                  chainlit=nested(mine_leaf, 'Plugin button'))
    layer = dataclasses.replace(minimal('layer', 'Layer'), locales=(added,))

    # when
    with served(store, layer, add=False) as client:
        with messages.speaking('tr'):
            sent = (msg(mine), msg(other))
        chats = (said('tr', chat_mine), said('tr', chat_other))
        note = prompt.writing()['tr']
        files = dict(chatui.TRANSLATIONS)
        page = SCRIPT.findall(client.get('/').text)

    # then
    assert sent == ('Plugin message', pack.messages[other])
    assert chats == ('Plugin chat', pack.chat[chat_other])
    assert note == 'Plugin writing note.'
    tree = files['tr'][1]
    assert at(tree, mine_leaf) == 'Plugin button'
    assert other_leaves and all(at(tree, path) == at(pack.chainlit, path) for path in other_leaves)
    assert page.index('/static/i18n-tr.js') < page.index('/plugins/layer/i18n-tr.js'), 'the core pack loads first, so the plug-in pack merges over it'


def test_the_core_page_script_is_cached_by_its_digest_and_unknown_languages_are_not_served(store):
    # when
    with served(store, add=False) as client:
        first = client.get('/static/i18n-tr.js')
        again = client.get('/static/i18n-tr.js', headers={'If-None-Match': first.headers['etag']})
        missing = client.get('/static/i18n-xx.js')

    # then
    assert first.status_code == 200 and again.status_code == 304 and missing.status_code == 404


def test_the_source_tool_prints_every_english_text_a_translator_needs():
    # given
    reference = locales.read('tr')

    # when
    found = locale_source.source()
    printed = json.loads(locale_source.text())

    # then
    assert printed == found
    assert found['page'] == sorted(reference.page)
    assert found['messages'] == {key: messages.MESSAGES[key] for key in sorted(outbound())}
    assert set(found['chat']) == set(TEXTS['en']) and found['chat']['working'] == TEXTS['en']['working']
    assert found['rules'] == [pattern for pattern, _ in reference.rules] and found['tails'] == [end for end, _ in reference.tails]
    assert found['headers'] == sorted(reference.headers) and found['months'] == list(MONTHS) and found['days'] == list(DAYS)
    assert locale_source.text().isascii()


def test_the_spanish_pack_keeps_one_regional_variant_for_cost_as_it_does_for_add_and_expire():
    # given
    page = json.loads((locales.ROOT / 'es' / 'page.json').read_text(encoding='utf-8'))['page']

    # when
    values = ' '.join(page.values())

    # then
    assert re.search(r'\b[Aa]ñadir\b', values) and re.search(r'\b[Cc]aduca\b', values)
    assert not re.search(r'\b[Cc]ostos?\b', values)
    assert page['Costs'] == 'Costes'


@pytest.mark.parametrize('lang', LANGS)
def test_the_idle_switch_title_is_translated_and_no_pack_quotes_the_old_langfuse_status(lang):
    # given
    page = json.loads((locales.ROOT / lang / 'page.json').read_text(encoding='utf-8'))['page']

    # when
    idle = page.get('On, but no instance is on: open it and turn an instance on')
    quoted = [k for k, v in page.items() if "Receives Skopax's own traces" in v or 'switch on Email me my notifications' in v]
    mail = page.get(MAIL_HELP)

    # then
    assert idle and idle != 'On, but no instance is on: open it and turn an instance on'
    assert quoted == []
    assert mail and mail != MAIL_HELP and page['Email me my notifications'] in mail
    assert not [k for k in page if 'switch on Email me my notifications' in k]
