import asyncio
import dataclasses
import importlib.util
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skopax import chatui, tunables
from skopax.chattext import STARTERS_MAX as CHAT_STARTERS_MAX, TEXT_MAX as CHAT_TEXT_MAX, entry_problem
from skopax.chatui.layer import ChatDataLayer
from skopax.chatui.text import (TEXTS, action_label, answer_of, continue_line, escaped, lang_of, pending_line, refusal, said, said_md, set_texts, texts_of)
from skopax.contract import Locale, Plugin
from skopax.features import off_text
from skopax.messages import msg
from skopax.registry import Registry, problem
from tests.plugins import chat_pack_plugin
from tests.plugins.base import minimal
from tests.test_plugin_hooks import served

PACK = chat_pack_plugin.PLUGIN
GERMAN = chat_pack_plugin.GERMAN
ENGLISH = TEXTS['en']
ADMIN = 'admin@example.com'
NINA = 'nina.white@example.com'
THREAD = 'aaaabbbb-cccc-4ddd-8eee-ffff00003333'
KEY = 'log|checkout-service|ERROR|boom'
CARD = {'num': 14, 'svc': 'checkout-service', 'label': 'ERROR lines'}
NAME = '#14 checkout-service / ERROR lines'
LINK = re.compile(r'(?<!\\)\[((?:\\.|[^\]\\])*)\]\(([^)\s]*)\)')
REFUSAL_CODES = ('cap', 'off', 'busy', 'missing', 'long', 'empty', 'role', 'bad', 'not-ready')
KEPT = {'working': 'Arbeite...'}


def with_chat(chat) -> Plugin:
    return dataclasses.replace(PACK, locales=(Locale('de', chat=chat),))


def loaded(caplog, plugin: Plugin) -> tuple:
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger='skopax.registry'):
        found = Registry.of(plugin).get(plugin.id)
    return found, [r.getMessage() for r in caplog.records if r.name == 'skopax.registry']


def chat_app() -> FastAPI:
    bridge = chatui.Bridge(user=lambda cookie: {'login': NINA, 'operator': True} if cookie == 'sealed-session' else None, post=lambda *a: {}, chat=lambda by, chat_id: None,
                           chats=lambda by: [], owner=lambda chat_id: NINA, shared=lambda chat_id: False, share=lambda *a: True, hide=lambda *a: None,
                           apply=lambda *a: ('done', ''), card=lambda ref: None)
    app = FastAPI()
    chatui.mount(app, bridge, b'seed', 'http://testserver')
    return app


def starters_of(client: TestClient, lang: str) -> list[tuple[str, str]]:
    client.cookies.set(chatui.LANG_COOKIE, lang)
    listed = client.get('/chat/project/settings', params={'language': 'en-US'}).json()['starters']
    return [(s['label'], s['message']) for s in listed]


def new_chat_button(client: TestClient, **headers) -> str:
    return client.get('/chat/project/translations', params={'language': 'en-US'}, headers=headers).json()['translation']['navigation']['newChat']['button']


def bundled(tag: str) -> dict:
    folder = Path(importlib.util.find_spec('chainlit').origin).parent / 'translations'
    return json.loads((folder / f'{tag}.json').read_text(encoding='utf-8'))


@pytest.mark.usefixtures('no_core_packs')
def test_a_pack_translates_chat_texts(store):
    # when
    with served(store, PACK, add=False):
        lang = lang_of('a=1; skopax_lang=de', 'en-US')
        working = said(lang, 'working')
        one = pending_line({'status': 'processing', 'progress': {'toolCalls': 1}}, None, lang)
        many = pending_line({'status': 'processing', 'progress': {'toolCalls': 2, 'lastTool': 'esql'}}, None, lang)
        still = said(lang, 'still')

    # then
    assert lang == 'de'
    assert working == GERMAN['working']
    assert one == r'Arbeite\: 1 Werkzeugaufruf\.\.\.'
    assert many == r'Arbeite\: 2 Werkzeugaufrufe, last esql\.\.\.', 'one call and many calls are two texts, so no plural trick is needed'
    assert pending_line({'status': 'processing', 'progress': {'toolCalls': 1}}, None, 'en') == 'Working \u00b7 1 tool call\u2026'
    assert 'still' not in GERMAN and still == ENGLISH['still'], 'a key the pack leaves out stays English'


@pytest.mark.usefixtures('no_core_packs')
def test_starters_and_actions_translate(store):
    # when
    with served(store, PACK, add=False):
        with TestClient(chat_app()) as client:
            client.cookies.set('skopax_session', 'sealed-session')
            signed_in = client.post('/chat/auth/header')
            german = starters_of(client, 'de')
            english = starters_of(client, 'en')
        take = action_label({'key': KEY, 'action': 'take'}, CARD, 'de')
        mute = action_label({'key': KEY, 'action': 'mute', 'hours': 48}, CARD, 'de')
        fixed = action_label({'key': KEY, 'action': 'resolve', 'kind': 'fixed'}, CARD, 'de')
        duplicate = action_label({'key': KEY, 'action': 'resolve', 'kind': 'duplicate', 'ref': '#9'}, CARD, 'de')
        odd = action_label({'key': KEY, 'action': 'escalate'}, CARD, 'de')

    # then
    assert signed_in.status_code == 200
    assert german == [tuple(pair) for pair in GERMAN['starters']]
    assert english == [tuple(pair) for pair in ENGLISH['starters']]
    assert take == f'Uebernehmen: {NAME}'
    assert mute == f'Mute 48 Std.: {NAME}', 'an action the pack leaves out stays English'
    assert fixed == f'Erledigen \u00b7 Behoben: {NAME}'
    assert duplicate == f'Erledigen \u00b7 Duplicate of #9: {NAME}', 'a kind the pack leaves out stays English'
    assert odd == f'Anwenden: {NAME}', 'the fallback verb is a chat text too'


def test_chainlit_s_own_texts_follow_the_chat_language(store):
    # given
    german = bundled('de-DE')['navigation']['newChat']['button']
    english = bundled('en-US')['navigation']['newChat']['button']

    # when
    with TestClient(chat_app()) as client:
        client.cookies.set('skopax_session', 'sealed-session')
        client.post('/chat/auth/header')
        without_pack = new_chat_button(client, **{'accept-language': 'de-DE,de;q=0.9'})
        with served(store, PACK, add=False):
            browser = new_chat_button(client, **{'accept-language': 'de-DE,de;q=0.9'})
            client.cookies.set(chatui.LANG_COOKIE, 'de')
            picked = new_chat_button(client)
            client.cookies.set(chatui.LANG_COOKIE, 'en')
            chosen_english = new_chat_button(client, **{'accept-language': 'de-DE'})

    # then
    assert german != english
    assert without_pack == english, 'with no German chat texts the chat speaks English, and so does Chainlit'
    assert browser == german and picked == german and chosen_english == english
    assert [chatui.chainlit_language(lang) for lang in ('de', 'de-AT', 'en', 'pt-BR', 'zh', 'xx')] == ['de-DE', 'de-DE', 'en-US', 'pt-PT', 'zh-CN', None]


def test_chainlit_s_own_texts_are_english_when_the_chat_language_has_no_chainlit_bundle(store):
    # given
    unbundled = dataclasses.replace(minimal('chat-xx', 'Chat xx'), locales=(Locale('xx', chat={'working': 'Xx...'}),))
    german = bundled('de-DE')['navigation']['newChat']['button']
    english = bundled('en-US')['navigation']['newChat']['button']

    # when
    with served(store, unbundled, add=False):
        with TestClient(chat_app()) as client:
            client.cookies.set('skopax_session', 'sealed-session')
            client.post('/chat/auth/header')
            client.cookies.set(chatui.LANG_COOKIE, 'xx')
            picked = client.get('/chat/project/translations', params={'language': 'de-DE'}).json()['translation']['navigation']['newChat']['button']
            lang = lang_of('skopax_lang=xx', 'de-DE')

    # then
    assert lang == 'xx' and chatui.chainlit_language('xx') is None
    assert german != english and picked == english, 'Chainlit has no xx bundle, so its own texts are English, not the browser language'


def test_packs_of_several_plugins_merge_in_load_order(store):
    # given
    later_starters = (('Was ist neu?', 'Was hat sich in der letzten Stunde geaendert?'),)
    later = dataclasses.replace(minimal('chat-more', 'Chat more'), after=('chat-pack',),
                                locales=(Locale('de', chat={'working': 'Bin dran...', 'actions': {'take': 'Nehmen'}, 'starters': later_starters}),))

    # when
    with served(store, PACK, later, add=False):
        working = said('de', 'working')
        hours = said('de', 'hours', n=2)
        take = action_label({'key': KEY, 'action': 'take'}, CARD, 'de')
        fixed = action_label({'key': KEY, 'action': 'resolve', 'kind': 'fixed'}, CARD, 'de')
        starters = texts_of('de')['starters']

    # then
    assert working == 'Bin dran...', 'the later plug-in wins per key, though its id sorts first'
    assert hours == '2 Std.', 'a key only the earlier pack has stays'
    assert take == f'Nehmen: {NAME}' and fixed == f'Erledigen \u00b7 Behoben: {NAME}', 'actions merge per entry'
    assert starters == later_starters, 'the later starters replace the earlier ones whole'


def test_tags_that_differ_only_in_case_merge():
    # when
    set_texts([('de-AT', {'working': 'Oida...'}), ('de-at', {'still': 'Gleich.'})])

    # then
    assert lang_of('skopax_lang=de-at', None) == lang_of(None, 'DE-AT') == 'de-AT'
    assert (said('de-AT', 'working'), said('de-AT', 'still')) == ('Oida...', 'Gleich.')


@pytest.mark.usefixtures('no_core_packs')
def test_lang_of_picks_cookie_then_accept_language(store):
    # given
    garbage = ',;;q=0.9,=,\x00\u00ff;q=,*;,' + 'x' * 300

    # when
    with served(store, PACK, add=False):
        picked = {'cookie de': lang_of('skopax_lang=de', None),
                  'cookie fr, browser de-AT': lang_of('a=1; skopax_lang=fr', 'de-AT,en;q=0.5'),
                  'cookie xx': lang_of('skopax_lang=xx', None),
                  'browser xx': lang_of(None, 'xx'),
                  'empty': lang_of('skopax_lang=', ''),
                  'garbage': lang_of(garbage, garbage),
                  'cookie en, browser de': lang_of('skopax_lang=en', 'de'),
                  'browser en-US first': lang_of(None, 'en-US,de;q=0.8'),
                  'browser case': lang_of(None, 'DE-at'),
                  'de not acceptable': lang_of(None, 'de;q=0, fr'),
                  'de not acceptable, spelled out': lang_of(None, 'de-AT; Q=0.000'),
                  'de acceptable': lang_of(None, 'fr, de;q=0.1'),
                  'odd weight': lang_of(None, 'de;q=lots')}

    # then
    assert picked == {'cookie de': 'de', 'cookie fr, browser de-AT': 'de', 'cookie xx': 'en', 'browser xx': 'en', 'empty': 'en', 'garbage': 'en',
                      'cookie en, browser de': 'en', 'browser en-US first': 'en', 'browser case': 'de', 'de not acceptable': 'en', 'de not acceptable, spelled out': 'en',
                      'de acceptable': 'de', 'odd weight': 'de'}


def test_a_plain_tag_reaches_a_regional_pack():
    # when
    set_texts([('de-AT', {'working': 'Oida...'})])
    regional_only = [lang_of(None, 'de'), lang_of('skopax_lang=de', None), lang_of(None, 'de-CH')]
    set_texts([('de-AT', {'working': 'Oida...'}), ('de', {'working': 'Arbeite...'})])
    both = [lang_of(None, 'de-CH'), lang_of(None, 'de-AT'), lang_of(None, 'de')]

    # then
    assert regional_only == ['de-AT', 'de-AT', 'de-AT']
    assert both == ['de', 'de-AT', 'de'], 'an exact tag wins, then the plain language before a regional one'


def test_the_chat_picks_what_the_page_picks_for_the_same_browser():
    # when
    set_texts([('de', {'working': 'Arbeite...'})])
    german = [lang_of(None, 'fr-FR,de'), lang_of(None, 'en-US,de'), lang_of('skopax_lang=en', 'de'), lang_of('skopax_lang=fr', 'fr-FR,de-AT')]
    set_texts([('pt-BR', {'working': 'Trabalhando...'})])
    regional = [lang_of(None, 'pt-PT'), lang_of(None, 'pt')]

    # then
    assert german == ['de', 'en', 'en', 'de'] and regional == ['pt-BR', 'pt-BR'], 'the cases of theBrowserLanguagesAreWalkedInOrderAsTheChatDoes in skopax-i18n-rules.test.js'


@pytest.mark.parametrize('chat, named, part, kept', [
    ({'nope': 'x'}, 'chat key nope', "chat['nope']", {}),
    ({'calls': 'Arbeite: {last}...'}, 'chat text calls', "chat['calls']", {}),
    ({'working': 5}, 'chat text working', "chat['working']", {}),
    ({'starters': (('Frage', 'Eine Frage'),) * 7}, 'chat starters', "chat['starters']", {}),
    ({'still': 'x' * 501}, 'chat text still', "chat['still']", {}),
    ({'hours': '{n:>1000000} Std.'}, 'chat text hours', "chat['hours']", {}),
    ({'hours': '{n!z} Std.'}, 'chat text hours', "chat['hours']", {}),
    ({'working': ''}, 'chat text working', "chat['working']", {}),
    ({'working': '   '}, 'chat text working', "chat['working']", {}),
    ({'starters': (('Frage', ''),)}, 'chat starters', "chat['starters'][0]", {}),
    ({'starters': (('   ', 'Eine Frage'),)}, 'chat starters', "chat['starters'][0]", {}),
    ({'actions': 'Nimm'}, 'chat actions', "chat['actions']", {}),
    ({'actions': {'take': 'Nimm', 'fly': 'Flieg'}}, 'chat actions entry fly', "chat['actions']['fly']", {'actions': {'take': 'Nimm'}}),
    ({'kinds': {'duplicate': 'Doppelt'}}, 'chat kinds entry duplicate', "chat['kinds']['duplicate']", {'kinds': {}}),
    ({'kinds': {'duplicate': 'Doppelt: {ref!r}'}}, 'chat kinds entry duplicate', "chat['kinds']['duplicate']", {'kinds': {}}),
    ({'kinds': {'fixed': 7}}, 'chat kinds entry fixed', "chat['kinds']['fixed']", {'kinds': {}}),
    ({'kinds': {'fixed': ''}}, 'chat kinds entry fixed', "chat['kinds']['fixed']", {'kinds': {}}),
    ('Arbeite', 'chat must map', 'chat', {}),
    ({1: 'x'}, 'chat key <int>', "chat['<int>']", {}),
])
def test_chat_packs_are_validated(chat, named, part, kept, caplog):
    # given
    page, writing = {'Resolved': 'Erledigt'}, 'Schreibe auf Deutsch.'

    # when
    good = Registry.of(PACK).get('chat-pack')
    found, warned = loaded(caplog, dataclasses.replace(PACK, locales=(Locale('de', page, chat=chat, writing=writing),)))
    set_texts([('de', chat)])

    # then
    assert good.state == 'ok' and good.reason is None
    assert (found.state, found.reason) == ('ok', None), 'a chat text that breaks a rule never breaks the plug-in'
    assert found.plugin.locales == (Locale('de', page, chat=kept, writing=writing),), 'only the named text, entry or pair is left out, and the rest of the pack stays'
    assert problem('locales', found.plugin.locales[0]) is None
    assert len(warned) == 1 and warned[0].startswith(f'Locale pack de of chat-pack: {part} is left out (locale de') and named in warned[0], warned
    assert texts_of('de') == ENGLISH, 'the merge takes nothing that validation refuses'


def test_chat_pack_limits_are_inclusive():
    # given
    at_limit = with_chat({'still': 'x' * 500, 'starters': tuple((f'Frage {i}', f'Eine Frage {i}') for i in range(6))})

    # when
    found = Registry.of(at_limit).get('chat-pack')

    # then
    assert (found.state, found.reason) == ('ok', None)


def test_a_starter_message_is_sent_as_written_so_it_holds_no_markup(caplog):
    # given
    label = 'Was ist *los*?'
    marked = ['Was ist **los** [x](https://evil.example.com)?', 'Was ist **los**?', 'Was ist _los_?', 'Siehe [x](/c/1)', 'Siehe ![x](/a.png)', 'Siehe [x]\n\n[x]: /c/1',
              'Was ist <b>los</b>?', 'Ist 3 > 2?', 'Was ist `los`?', 'Was ist ~~los~~?', 'Siehe https\\://evil.example.com', 'Siehe www\\.evil.example.com',
              'Schreib ops\\@evil.example.com', 'Schreib ops&#64;evil.example.com', 'Was ist \\los?', 'Was & wie?', 'Was ist los?\nUnd jetzt?', 'Was ist los?\r\nUnd jetzt?',
              '# Was ist los', '  ## Was ist los', '| a | b |', '- Was ist los', '+ Was ist los', '1. Was ist los', '12) Was ist los', '    Was ist los', '\tWas ist los']
    plain = ['Was ist los - jetzt, Frage #1?', 'Was ist 1. und 2.?', 'Was | wie?']
    reason = msg('locales.part_dropped', lang='de', plugin=PACK.id, part="chat['starters'][0]",
                 reason=msg('plugins.why.locale_chat_starters', lang='de', key='starters', limit=CHAT_TEXT_MAX, count=CHAT_STARTERS_MAX))

    # when
    clean = [loaded(caplog, with_chat({'starters': ((label, message),)} | KEPT)) for message in ['Was ist los?', *plain]]
    found = [loaded(caplog, with_chat({'starters': ((label, message),)} | KEPT)) for message in marked]
    referenced, warned = loaded(caplog, with_chat({'starters': (('Frage an ops&#64;evil.example.com', 'Was ist los?'),)} | KEPT))

    # then
    assert [(p.state, p.reason, p.plugin.locales[0].chat['starters'][0][1], lines) for p, lines in clean] == [('ok', None, m, []) for m in ['Was ist los?', *plain]], \
        'the label is button text, so a mark in it stays plain; a mark inside a line is fine'
    assert [(m, p.state, p.reason, p.plugin.locales[0].chat, lines) for m, (p, lines) in zip(marked, found)] == [(m, 'ok', None, KEPT, [reason]) for m in marked], \
        'the marked starter is left out, and the other chat text of the pack stays'
    assert (referenced.state, referenced.plugin.locales[0].chat, warned) == ('ok', KEPT, [reason]), 'a label is checked for addresses after character references are resolved'
    assert [key for key, value in ENGLISH.items() if entry_problem(key, value)] == [] and [key for key, value in GERMAN.items() if entry_problem(key, value)] == []


LINK_SHAPES = {'https': 'https://evil.example.com/b', 'http': 'Http://evil.example.com', 'www': 'WWW.evil.example.com', 'mailto': 'mailto:ops', 'xmpp': 'XMPP:ops',
               'address': 'ops@evil.example.com', 'angle': '<x-evil:ops>', 'dash-label': 'ops@a.-b.example.com'}


@pytest.mark.parametrize('shape', LINK_SHAPES.values(), ids=LINK_SHAPES.keys())
def test_a_link_shape_in_any_chat_text_is_left_out_naming_the_key(shape, caplog):
    # given
    placed = [('chat text continue', {'continue': f'Siehe {shape}'}), ('chat text not_ready', {'not_ready': f'Nicht bereit, siehe {shape}. {{needs}}'}),
              ('chat actions entry take', {'actions': {'take': f'Nimm {shape}'}}), ('chat kinds entry fixed', {'kinds': {'fixed': f'Behoben {shape}'}}),
              ('chat starters', {'starters': ((f'Frage {shape}', 'Eine Frage'),)}), ('chat starters', {'starters': (('Frage', f'Eine Frage {shape}'),)})]

    # when
    found = [(named, *loaded(caplog, with_chat(chat | KEPT))) for named, chat in placed]

    # then
    assert [(named, p.state, len(lines), named in ''.join(lines)) for named, p, lines in found] == [(named, 'ok', 1, True) for named, _ in placed]
    assert [p.plugin.locales[0].chat for _, p, _ in found] == [KEPT, KEPT, {'actions': {}} | KEPT, {'kinds': {}} | KEPT, KEPT, KEPT], \
        'each text with a link is left out, and the other chat text of the pack stays'


def test_a_placeholder_cannot_split_an_address_past_the_link_rule(caplog):
    # given
    placed = [('chat text login', {'login': '{product}@evil.example.com'}), ('chat text hours', {'hours': 'ops@{n}.example.com'}),
              ('chat kinds entry duplicate', {'kinds': {'duplicate': 'Doppelt: www{ref}.example.com'}}), ('chat text last', {'last': ', https:{tool}//evil.example.com'})]

    # when
    found = [(named, *loaded(caplog, with_chat(chat | KEPT))) for named, chat in placed]

    # then
    assert [(named, p.state, len(lines), named in ''.join(lines), problem('locales', p.plugin.locales[0])) for named, p, lines in found] == [(named, 'ok', 1, True, None) for named, _ in placed]
    assert [p.plugin.locales[0].chat for _, p, _ in found] == [KEPT, KEPT, {'kinds': {}} | KEPT, KEPT], 'each text that splits an address is left out, and the other chat text stays'


HIDDEN_LINKS = {'escaped-colon': 'https\\://evil.example.com', 'escaped-dot': 'www\\.evil.example.com', 'escaped-at': 'ops\\@evil.example.com',
                'decimal-at': 'ops&#64;evil.example.com', 'named-at': 'ops&commat;evil.example.com', 'hex-h': '&#x68;ttps://evil.example.com'}


@pytest.mark.parametrize('spelling', HIDDEN_LINKS.values(), ids=HIDDEN_LINKS.keys())
def test_a_link_written_with_an_escape_or_a_character_reference_is_still_left_out(spelling, caplog):
    # given
    placed = [('chat text continue', {'continue': f'Siehe {spelling}'}), ('chat kinds entry fixed', {'kinds': {'fixed': f'Behoben {spelling}'}}),
              ('chat starters', {'starters': ((f'Frage {spelling}', 'Eine Frage'),)}), ('chat starters', {'starters': (('Frage', f'Eine Frage {spelling}'),)})]

    # when
    found = [(named, *loaded(caplog, with_chat(chat | KEPT))) for named, chat in placed]

    # then
    assert [(named, p.state, len(lines), named in ''.join(lines)) for named, p, lines in found] == [(named, 'ok', 1, True) for named, _ in placed]
    assert [p.plugin.locales[0].chat for _, p, _ in found] == [KEPT, {'kinds': {}} | KEPT, KEPT, KEPT], 'each text with a hidden link is left out, and the other chat text stays'
    assert escaped('ops&#64;evil') == r'ops\&\#64\;evil', 'shown pack text escapes the ampersand, so a reference stays literal in Markdown'


JOINED_LINK = {'halted': 'Warten: siehe www{why}', 'halted_capped': '.evil.example.com', 'halted_stopped': 'angehalten', 'call': 'Arbeite {n}: https:{last}',
               'last': '//evil.example.com/{tool}', 'calls': 'Arbeite: {n} Aufrufe{last}...', 'working': 'Arbeite...'}


def test_two_chat_texts_that_join_into_an_address_fall_back_to_english(caplog):
    # given
    loaded = Registry.of(with_chat(JOINED_LINK)).get('chat-pack')

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.chatui.text'):
        set_texts([('de', JOINED_LINK)])
        set_texts([('de', JOINED_LINK)])
    lines = [pending_line({'status': 'requested'}, halted, 'de') for halted in ('capped', 'stopped')] + [
        pending_line({'status': 'processing', 'progress': {'toolCalls': calls, 'lastTool': 'esql'}}, None, 'de') for calls in (1, 2)]

    # then
    assert loaded.state == 'ok', 'each text passes the link rule on its own'
    assert lines == ['Waiting: the investigator is capped.', 'Waiting: the investigator is angehalten.', 'Working \u00b7 1 tool call, last esql\u2026',
                     r'Arbeite\: 2 Aufrufe, last esql\.\.\.'], 'only the joined pairs fall back; the rest of the pack stays'
    assert said('de', 'working') == 'Arbeite...'
    assert sorted(r.getMessage() for r in caplog.records if r.name == 'skopax.chatui.text') == sorted(
        msg('locales.chat_joined', lang='de', key=key, other=other) for key, other in (('halted', 'halted_capped'), ('call', 'last'))), 'one warning per pair, not per rebuild'


def test_a_joined_address_split_across_two_plugins_falls_back_to_english(store):
    # given
    first = dataclasses.replace(minimal('chat-first', 'Chat first'), locales=(Locale('de', chat={'halted': 'Warten: ops{why}', 'working': 'Arbeite...'}),))
    second = dataclasses.replace(minimal('chat-second', 'Chat second'), after=('chat-first',), locales=(Locale('de', chat={'halted_capped': '@evil.example.com'}),))

    # when
    with served(store, first, second, add=False) as client:
        states = [client.app.state.registry.get(p).state for p in ('chat-first', 'chat-second')]
        capped = pending_line({'status': 'requested'}, 'capped', 'de')
        working = said('de', 'working')

    # then
    assert states == ['ok', 'ok'], 'each pack passes on its own'
    assert capped == 'Waiting: the investigator is capped.' and working == 'Arbeite...'


def test_a_joined_address_whose_host_label_starts_with_a_dash_falls_back_to_english():
    # given
    pack = {'halted': 'Warten: ops{why}', 'halted_capped': '@a.-b.example.com', 'halted_stopped': 'angehalten'}

    # when
    loaded = Registry.of(with_chat(pack)).get('chat-pack')
    set_texts([('de', pack)])
    lines = [pending_line({'status': 'requested'}, halted, 'de') for halted in ('capped', 'stopped')]

    # then
    assert loaded.state == 'ok', 'each text passes the link rule on its own'
    assert lines == ['Waiting: the investigator is capped.', 'Waiting: the investigator is angehalten.']


@pytest.mark.usefixtures('no_core_packs')
def test_chat_texts_stop_when_the_plugin_stops_serving(store):
    # when
    with served(store, PACK, add=False) as client:
        before = lang_of('skopax_lang=de', None)
        client.app.state.runtime.set_enabled('chat-pack', False, ADMIN).join()
        after = lang_of('skopax_lang=de', None)
        working = said('de', 'working')

    # then
    assert (before, after) == ('de', 'en')
    assert working == ENGLISH['working']


def test_chat_text_reads_never_touch_settings(store, monkeypatch):
    # given
    with served(store, PACK, add=False):
        ready = lang_of('skopax_lang=de', None)

    def refuse(self):
        raise AssertionError('a chat text read reached the live settings')

    monkeypatch.setattr(tunables.Live, 'current', refuse)

    async def read() -> tuple[str, str, str]:
        lang = lang_of('skopax_lang=de', 'de-AT')
        return lang, said(lang, 'working'), action_label({'key': KEY, 'action': 'take'}, CARD, lang)

    # when
    lang, working, take = asyncio.run(read())

    # then
    assert ready == lang == 'de'
    assert working == GERMAN['working'] and take == f'Uebernehmen: {NAME}'


def test_pack_texts_are_plain_text_in_the_chat():
    # given
    pack = {'working': '**x**', 'continue': 'Siehe ![x](/a.png)', 'continue_link': '[x](/c/1)', 'actions': {'take': '**Nimm**'}}
    loaded = Registry.of(with_chat(pack)).get('chat-pack')
    set_texts([('de', pack)])

    # when
    working = pending_line({'status': 'processing'}, None, 'de')
    line = continue_line('de', THREAD)
    take = action_label({'key': KEY, 'action': 'take'}, CARD, 'de')

    # then
    assert loaded.state == 'ok'
    assert working == r'\*\*x\*\*'
    assert line == r'Siehe \!\[x\]\(\/a\.png\) [\[x\]\(\/c\/1\)](/chat-continue/' + THREAD + ')'
    assert [target for _, target in LINK.findall(line)] == [f'/chat-continue/{THREAD}'], 'the one link is built by core around the pack label'
    assert continue_line('en', THREAD) == f'This is a snapshot of a shared chat. [Continue it in your own private chat \u2197](/chat-continue/{THREAD})'
    assert take == f'**Nimm**: {NAME}', 'a button label is plain text, so it keeps the text as written'


def test_a_refusal_speaks_the_chat_language_and_never_carries_the_error_text():
    # given
    set_texts([('de', GERMAN)])
    refused = {code: chatui.Refused('secret detail', code, cap=30, limit=1000) for code in REFUSAL_CODES}

    # when
    english = {code: refusal(error, 'en') for code, error in refused.items()}
    capped = refusal(refused['cap'], 'de')
    unknown = refusal(chatui.Refused('secret result text'), 'de')
    off = [refusal(chatui.Refused('secret', 'feature-off', feature=feature), lang) for feature, lang in (('investigator', 'de'), ('chat', 'de'), ('postdeploy', 'en'))]
    halted = pending_line({'status': 'requested'}, 'off', 'de')

    # then
    assert capped == r'30 Nachrichten am Tag sind das Limit\.'
    assert unknown == r'Das ging nicht\.', 'an unknown code gives a fixed chat text'
    assert off == [r'Der Ermittler ist aus\.', ENGLISH['chat_off'], off_text('postdeploy').replace('Post-deploy', r'Post\-deploy', 1)]
    assert halted == off[0]
    assert english['cap'] == '30 chat messages a day is the cap; it starts again at midnight.' and english['long'] == 'A message is at most 1000 characters.'
    assert len(set(english.values())) == len(REFUSAL_CODES) and ENGLISH['refused'] not in english.values(), 'each code has its own chat text'
    assert not [text for text in [*english.values(), capped, unknown, *off] if 'secret' in text]


def test_a_resumed_chat_and_its_sidebar_speak_the_chat_language():
    # given
    set_texts([('de', GERMAN)])
    chat = {'id': THREAD, 'messages': [{'id': 'm1', 'text': 'Warum?', 'at': '2026-11-10T12:00:00Z', 'status': 'failed'}]}
    layer = ChatDataLayer(chatui.Bridge(user=lambda cookie: None, post=lambda *a: {}, chat=lambda by, chat_id: chat, chats=lambda by: [chat], owner=lambda chat_id: NINA,
                                        shared=lambda chat_id: False, share=lambda *a: True, hide=lambda *a: None, apply=lambda *a: ('done', ''), card=lambda ref: None))
    seen = []

    async def inner(scope, receive, send):
        seen.append(chatui.REQUEST_LANG.get())

    socket = {'type': 'websocket', 'path': '/ws/socket.io/', 'headers': [(b'cookie', b'a=1; skopax_lang=de'), (b'accept-language', b'en')]}

    # when
    english = layer.thread(THREAD)
    asyncio.run(chatui.BrandFront(inner)(socket, None, None))
    token = chatui.REQUEST_LANG.set('de')
    try:
        resumed = layer.thread(THREAD)
        listed = layer.threads(SimpleNamespace(first=20), SimpleNamespace(userId=NINA, search=None))
    finally:
        chatui.REQUEST_LANG.reset(token)

    # then
    assert seen == ['de'] and chatui.REQUEST_LANG.get() == 'en', 'the chat socket carries the chat language like the chat requests do'
    assert (english['name'], english['steps'][-1]['output']) == ('Chat', 'No answer.')
    assert (resumed['name'], resumed['steps'][-1]['output']) == ('Unterhaltung', 'Keine Antwort.')
    assert [t['name'] for t in listed.data] == ['Unterhaltung']


def test_an_english_chat_pack_is_dropped_with_one_warning(store, caplog):
    # given
    english = dataclasses.replace(minimal('chat-english', 'Chat english'), locales=(Locale('en', chat={'working': 'Thinking...'}), Locale('en-GB', chat={'working': 'Pondering...'})))
    reason = msg('plugins.why.locale_english')

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.chatui.text'):
        with served(store, PACK, english, add=False) as client:
            client.app.state.runtime.refresh()
            client.app.state.runtime.refresh()
            state = client.app.state.registry.get('chat-english').state
            british = lang_of('skopax_lang=en-GB', None)
            working = said(british, 'working')
            german = said('de', 'working')

    # then
    assert state == 'ok' and (british, working) == ('en', ENGLISH['working']) and german == GERMAN['working']
    assert sorted(r.getMessage() for r in caplog.records if r.name == 'skopax.chatui.text') == sorted(msg('locales.chat_dropped', lang=lang, reason=reason) for lang in ('en', 'en-GB'))


def test_pack_texts_inside_core_built_markdown_keep_to_one_line():
    # given
    pack = {'continue': '    Ein Schnappschuss.\n\nMehr dazu', 'continue_link': '\n\n    Weiter\n\n  hier', 'still': '    Noch\n\n dran', 'last': ' (zuletzt\n\n{tool})'}
    loaded = Registry.of(with_chat(pack)).get('chat-pack')
    set_texts([('de', pack)])

    # when
    line = continue_line('de', THREAD)
    still = said_md('de', 'still')
    working = pending_line({'status': 'processing', 'progress': {'toolCalls': 2, 'lastTool': 'esql'}}, None, 'de')

    # then
    assert loaded.state == 'ok'
    assert line == rf' Ein Schnappschuss\. Mehr dazu [ Weiter hier](/chat-continue/{THREAD})'
    assert LINK.findall(line) == [(' Weiter hier', f'/chat-continue/{THREAD}')], 'a blank line or an indent in the label cannot break the link'
    assert '\n' not in line + still + working and '  ' not in line + still + working
    assert still == ' Noch dran'
    assert working == 'Working \u00b7 2 tool calls \\(zuletzt esql\\)\u2026', 'whitespace collapses to one space and is not trimmed, so a fragment keeps its leading space'
    assert said('de', 'continue_link') == pack['continue_link'], 'plain-text places keep the text as written'


def test_a_refusal_brings_back_its_detail_from_core_values_only():
    # given
    needs = ' '.join([msg('features.needs.anthropic_key'), msg('features.needs.mode')])
    set_texts([('de', {'not_ready': 'Der Ermittler ist nicht bereit. {needs}', 'refused_failed': 'Nicht angewandt: {reason}.', 'feature_off': '{feature} ist aus.'})])

    # when
    ready = refusal(chatui.Refused('secret', 'not-ready', needs=needs), 'en')
    bare = refusal(chatui.Refused('secret', 'not-ready'), 'en')
    german = refusal(chatui.Refused('secret', 'not-ready', needs=needs), 'de')
    failed = refusal(chatui.Refused('secret', 'failed', reason='only open or stopped cards can be grouped'), 'en')
    failed_de = refusal(chatui.Refused('secret', 'failed', reason='only open or stopped cards can be grouped'), 'de')
    other = [refusal(chatui.Refused('secret', 'feature-off', feature=feature), lang) for feature, lang in (('postdeploy', 'en'), ('push', 'de'), ('no-such-feature', 'en'))]

    # then
    assert ready == f'{msg("chat.not_ready")} {escaped(needs)}' and bare == msg('chat.not_ready'), 'the needs list comes back, and none leaves no trailing space'
    assert german == r'Der Ermittler ist nicht bereit\. ' + escaped(needs), 'the core needs are filled in escaped, inside the escaped pack text'
    assert failed == 'Not applied: only open or stopped cards can be grouped.' and failed_de == r'Nicht angewandt\: only open or stopped cards can be grouped\.'
    assert other == [off_text('postdeploy').replace('Post-deploy', r'Post\-deploy', 1), r'Phone push ist aus\.', 'no\\-such\\-feature is turned off in Admin \u203a Features.']
    assert not [text for text in [ready, bare, german, failed, failed_de, *other] if 'secret' in text]


def test_core_values_filled_into_chat_markdown_are_escaped_once():
    # given
    set_texts([('de', {'halted': 'Warten: {why}.', 'halted_capped': 'am Limit.', 'last': ', zuletzt {tool}', 'calls': '{n} Aufrufe{last}...'})])
    failed = chatui.Refused('secret', 'failed', reason='the card *is* closed')

    # when
    english = refusal(failed, 'en')
    done = said_md('en', 'done', what='[x](/c/1) `y`\n\n# z')
    capped = pending_line({'status': 'requested'}, 'capped', 'de')
    calls = pending_line({'status': 'processing', 'progress': {'toolCalls': 2, 'lastTool': 'kube_logs'}}, None, 'de')
    off = refusal(chatui.Refused('secret', 'feature-off', feature='no*such'), 'en')
    unanswered = answer_of({'status': 'failed', 'error': 'over the *tool* cap'}, 'en')

    # then
    assert english == r'Not applied: the card \*is\* closed.'
    assert done == r'Done: \[x\]\(\/c\/1\) \`y\` \# z.', 'a core value keeps to one line too'
    assert capped == r'Warten\: am Limit\.\.' and calls == r'2 Aufrufe\, zuletzt kube\_logs\.\.\.', 'a chat text filled into another chat text is escaped once'
    assert off == 'no\\*such is turned off in Admin \u203a Features.'
    assert unanswered == r'No answer: over the \*tool\* cap.'


def test_chainlit_serves_the_english_readme_to_every_chat_language_and_logs_no_warning(store, caplog):
    # given
    unbundled = dataclasses.replace(minimal('chat-xx', 'Chat xx'), locales=(Locale('xx', chat={'working': 'Xx...'}),))

    # when
    with served(store, PACK, unbundled, add=False):
        with TestClient(chat_app()) as client:
            client.cookies.set('skopax_session', 'sealed-session')
            client.post('/chat/auth/header')
            with caplog.at_level(logging.WARNING, logger='chainlit'):
                readmes = {}
                for lang, browser in (('de', 'de-DE'), ('xx', 'xx-XX'), ('en', 'en-GB'), ('fr', 'fr-FR')):
                    client.cookies.set(chatui.LANG_COOKIE, lang)
                    answer = client.get('/chat/project/settings', params={'language': browser})
                    readmes[lang] = (answer.status_code, answer.json()['markdown'])

    # then
    assert chatui.chainlit_language('xx') is None, 'xx is a chat language with no bundled Chainlit translation'
    assert readmes == {lang: (200, '') for lang in ('de', 'xx', 'en', 'fr')}
    assert [r.getMessage() for r in caplog.records if r.name.startswith('chainlit')] == []


def test_the_halted_words_are_chat_texts():
    # given
    set_texts([('de', {'halted': 'Warten: der Ermittler ist {why}.', 'halted_stopped': 'angehalten', 'halted_capped': 'am Tageslimit'})])

    # when
    german = [pending_line({'status': 'requested'}, why, 'de') for why in ('stopped', 'capped')]
    english = [pending_line({'status': 'requested'}, why, 'en') for why in ('stopped', 'capped')]

    # then
    assert german == [r'Warten\: der Ermittler ist angehalten\.', r'Warten\: der Ermittler ist am Tageslimit\.']
    assert english == ['Waiting: the investigator is stopped.', 'Waiting: the investigator is capped.']
    assert (ENGLISH['halted_stopped'], ENGLISH['halted_capped']) == ('stopped', 'capped')


def test_a_chat_key_that_is_not_text_drops_only_that_key_and_an_empty_starters_list_is_no_drop(caplog):
    # given
    chat = {1: 'x', 'working': 'Arbeite...', 'starters': ()}

    # when
    found, warned = loaded(caplog, dataclasses.replace(PACK, locales=(Locale('de', {'Resolved': 'Erledigt'}, chat=chat),)))

    # then
    assert found.plugin.locales[0].chat == {'working': 'Arbeite...'}, 'the good text stays; English starters stay since the pack gives none'
    assert len(warned) == 1 and "chat['<int>']" in warned[0]
    assert found.dropped == (('de', 1),)
