import dataclasses
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skopax import messages
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.chattext import STARTERS_MAX as CHAT_STARTERS_MAX, TEXT_MAX as CHAT_TEXT_MAX, TEXTS
from skopax.chatui.text import said
from skopax.contract import Check, Locale, PassResult, Sweep
from skopax.messages import msg
from skopax.registry import DROPS_MAX, RULES_MAX, Drop, Registry, problem
from skopax.runtime import PAGE_PACK
from tests.conftest import AUTH, settings
from tests.plugins.base import minimal
from tests.test_chat_i18n import chat_app, starters_of
from tests.test_auth import sso_settings
from tests.test_plugin_hooks import PAGE_TAILS, page_locale, pages
from tests.test_role_classes import people

PLUGINS = Path(__file__).parent / 'plugins'
ENGLISH = TEXTS['en']
NOW = 1_792_224_000.0
LINKED = 'Moment, siehe https://evil.example.com'


def serve(store, *plugins) -> TestClient:
    return TestClient(create_app(settings(), store, registry=Registry.of(*plugins, directory=PLUGINS)))


def dropped(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == 'skopax.registry']


def left_out(plugin: str, part: str, reason: str) -> str:
    return msg('locales.part_dropped', lang='de', plugin=plugin, part=part, reason=reason)


def test_a_bad_chat_text_is_left_out_while_the_plugin_check_runs_and_its_other_texts_apply(store, caplog):
    # given
    ran = []

    def pulse(ctx) -> Sweep:
        ran.append(ctx.plugin.id)
        return Sweep(PassResult('chatty', True, 'ok', [], {}, ()))

    chatty = minimal('chatty', 'Chatty', checks=(Check('chatty', 'Chatty pulse', 900, 0, ('chatty',), pulse),),
                     locales=(Locale('de', chat={'working': 'Arbeite...', 'still': LINKED, 'answered': 'Beantwortet'}),))
    caplog.set_level(logging.WARNING, logger='skopax.registry')

    # when
    with serve(store, chatty) as client:
        found = client.app.state.registry.get('chatty')
        client.app.state.sweeper.run('chatty', 'chatty', NOW)
        texts = [said('de', key) for key in ('working', 'still', 'answered')]

    # then
    assert (found.state, found.reason) == ('ok', None)
    assert ran == ['chatty'], 'the check of a plug-in with a bad pack text still runs'
    assert texts == ['Arbeite...', ENGLISH['still'], 'Beantwortet']
    assert found.plugin.locales == (Locale('de', chat={'working': 'Arbeite...', 'answered': 'Beantwortet'}),)
    assert problem('locales', found.plugin.locales[0]) is None
    assert dropped(caplog) == [left_out('chatty', "chat['still']", msg('plugins.why.locale_chat_text', lang='de', key='still', limit=CHAT_TEXT_MAX))]


def test_one_bad_page_rule_among_good_ones_is_the_only_rule_left_out(store, caplog):
    # given
    first, last = ('^Gone$', 'Weg'), ('^Stopped (.+)$', 'Gestoppt $1')
    plugin = pages(page_locale(rules=(first, ('(?<=a)b', 'x'), last)))
    caplog.set_level(logging.WARNING, logger='skopax.registry')

    # when
    with serve(store, plugin) as client:
        pack = client.get('/plugins/pages/i18n-de.js')

    # then
    assert pack.status_code == 200
    assert pack.text == PAGE_PACK % ('"de"', json.dumps({'Resolved': 'Erledigt', '@rules': [list(first), list(last)], '@months': {'Mar': 'Mrz'}, '@days': {'Mon': 'Mo'},
                                                          '@tails': [list(PAGE_TAILS[0])], '@th': {'Requests': 'Anfragen'}}))
    assert dropped(caplog) == [left_out('pages', 'rules[1]', msg('plugins.why.locale_lookbehind', lang='de', pattern='(?<=a)b'))]


def test_a_bad_starter_pair_among_good_ones_is_the_only_pair_left_out(store, caplog):
    # given
    pairs = (('Was ist los?', 'Was ist gerade los?'), ('Frage', 'Was ist **los**?'), ('Noch etwas?', 'Gibt es noch etwas?'))
    plugin = minimal('starter', 'Starter', locales=(Locale('de', chat={'starters': pairs, 'working': 'Arbeite...'}),))
    caplog.set_level(logging.WARNING, logger='skopax.registry')

    # when
    with serve(store, plugin):
        with TestClient(chat_app()) as client:
            client.cookies.set('skopax_session', 'sealed-session')
            client.post('/chat/auth/header')
            german = starters_of(client, 'de')
        working = said('de', 'working')

    # then
    assert german == [pairs[0], pairs[2]]
    assert working == 'Arbeite...'
    assert dropped(caplog) == [left_out('starter', "chat['starters'][1]", msg('plugins.why.locale_chat_starters', lang='de', key='starters', limit=CHAT_TEXT_MAX,
                                                                             count=CHAT_STARTERS_MAX))]


def test_a_part_with_too_many_rules_is_left_out_whole_and_the_rest_of_the_pack_stays(store, caplog):
    # given
    many = tuple((f'^a{i}$', 'b') for i in range(RULES_MAX + 1))
    plugin = pages(page_locale(rules=many))
    caplog.set_level(logging.WARNING, logger='skopax.registry')

    # when
    with serve(store, plugin) as client:
        pack = client.get('/plugins/pages/i18n-de.js')
        found = client.app.state.registry.get('pages')

    # then
    assert (found.state, found.reason) == ('ok', None)
    assert pack.text == PAGE_PACK % ('"de"', json.dumps({'Resolved': 'Erledigt', '@months': {'Mar': 'Mrz'}, '@days': {'Mon': 'Mo'}, '@tails': [list(PAGE_TAILS[0])],
                                                          '@th': {'Requests': 'Anfragen'}}))
    assert dropped(caplog) == [left_out('pages', 'rules', msg('plugins.why.locale_many', lang='de', count=RULES_MAX + 1, what='rules', limit=RULES_MAX))]


class Exploding(dict):
    def items(self):
        raise RuntimeError('secret detail')


def test_a_pack_that_cannot_be_checked_is_left_out_whole_and_its_error_text_is_never_shown(caplog):
    # given
    plugin = minimal('shaky', 'Shaky', locales=(Locale('de', Exploding(Resolved='Erledigt')), Locale('fr', {'Resolved': 'Resolu'})))
    caplog.set_level(logging.WARNING, logger='skopax.registry')

    # when
    found = Registry.of(plugin, directory=PLUGINS).get('shaky')

    # then
    assert (found.state, found.reason, found.plugin.locales) == ('ok', None, (Locale('fr', {'Resolved': 'Resolu'}),))
    assert dropped(caplog) == [left_out('shaky', 'locales[0]', msg('plugins.why.failed', error='RuntimeError'))]
    assert 'secret detail' not in caplog.text


class Unshown(str):
    def __str__(self):
        raise RuntimeError('secret detail')


class Unwalkable(tuple):
    def __iter__(self):
        raise RuntimeError('secret detail')


def test_a_pack_whose_language_cannot_be_shown_is_left_out_and_other_plugins_load(caplog):
    # given
    steady = minimal('steady', 'Steady', locales=(Locale('de', {'Resolved': 'Erledigt'}),))
    shaky = minimal('shaky', 'Shaky', locales=(Locale(Unshown('de'), {'Resolved': 'Erledigt'}), Locale('fr', {'Resolved': 'Resolu'})))
    caplog.set_level(logging.WARNING, logger='skopax.registry')

    # when
    reg = Registry.of(steady, shaky, directory=PLUGINS)

    # then
    assert [(x.id, x.state, x.reason) for x in reg.loaded()] == [('shaky', 'ok', None), ('steady', 'ok', None)]
    assert reg.get('steady').plugin is steady
    assert reg.get('shaky').plugin.locales == (Locale('fr', {'Resolved': 'Resolu'}),)
    assert dropped(caplog) == [msg('locales.part_dropped', lang='?', plugin='shaky', part='locales[0]', reason=msg('plugins.why.failed', error='RuntimeError'))]
    assert 'secret detail' not in caplog.text


def test_a_locales_container_that_cannot_be_walked_breaks_only_its_own_plugin(caplog):
    # given
    steady = minimal('steady', 'Steady', locales=(Locale('de', {'Resolved': 'Erledigt'}),))
    shaky = minimal('shaky', 'Shaky', locales=Unwalkable((Locale('de', {'Resolved': 'Erledigt'}),)))
    caplog.set_level(logging.WARNING)

    # when
    reg = Registry.of(steady, shaky, directory=PLUGINS)

    # then
    assert [(x.id, x.state) for x in reg.loaded()] == [('shaky', 'broken'), ('steady', 'ok')]
    assert reg.get('shaky').reason == msg('plugins.broken', name='Shaky', reason=msg('plugins.why.failed', error='RuntimeError'))
    assert reg.get('steady').plugin is steady and [p.id for p in reg.usable()] == ['steady']
    assert 'secret detail' not in caplog.text


@pytest.mark.parametrize('what, given, part, kept', [
    ('headers', {**{f'H{i}': f'K{i}' for i in range(149)}, 'Errors': None}, "headers['Errors']", {f'H{i}': f'K{i}' for i in range(149)}),
    ('tails', ((' a', 'b'), ('', 'x'), (' c', 'd')), 'tails[1]', ((' a', 'b'), (' c', 'd'))),
    ('months', {'Mar': 'Mrz', 'Apr': 5}, "months['Apr']", {'Mar': 'Mrz'}),
    ('rules', (('^a$', 'b'), ('^c$',), ('^d$', 'e')), 'rules[1]', (('^a$', 'b'), ('^d$', 'e'))),
])
def test_one_malformed_entry_is_the_only_entry_left_out_and_its_part_stays(what, given, part, kept, caplog):
    # given
    plugin = pages(page_locale(**{what: given}))
    caplog.set_level(logging.WARNING, logger='skopax.registry')

    # when
    found = Registry.of(plugin, directory=PLUGINS).get('pages')

    # then
    assert (found.state, found.reason) == ('ok', None)
    assert found.plugin.locales == (page_locale(**{what: kept}),)
    assert problem('locales', found.plugin.locales[0]) is None
    assert dropped(caplog) == [left_out('pages', part, msg('plugins.why.locale_shape', lang='de', what=what))]


def test_a_control_character_in_a_pack_key_or_language_cannot_forge_a_log_line(caplog):
    # given
    plugin = minimal('shaky', 'Shaky', locales=(Locale('de', {'@x\nFORGED registry line': 'y', 'A': 'B'}, chat={'x\r\nFORGED': 'y', 'working': 'Arbeite...'}),
                                                 Locale('de\nFORGED', {'A': 'B'})))
    caplog.set_level(logging.WARNING, logger='skopax.registry')

    # when
    found = Registry.of(plugin, directory=PLUGINS).get('shaky')
    lines = dropped(caplog)

    # then
    assert found.plugin.locales == (Locale('de', {'A': 'B'}, chat={'working': 'Arbeite...'}),)
    assert len(lines) == 3
    assert [('\n' in line or '\r' in line, '\\n' in line) for line in lines] == [(False, True)] * 3, lines


def test_a_bad_text_in_one_plugin_leaves_another_plugin_untouched(store, caplog):
    # given
    alpha = minimal('alpha', 'Alpha', locales=(Locale('de', {'Resolved': 'Erledigt'}, {'slack.cancel': 'Abbrechen'}, chat={'working': 'Arbeite...'}),))
    beta = minimal('beta', 'Beta', locales=(Locale('de', {'Open': 'Offen', '@th': 'x'}, {'slack.menu.chat': 5, 'slack.open': 'In {product} oeffnen'}, chat={'still': LINKED, 'answered': 'Beantwortet'}),))
    caplog.set_level(logging.WARNING)

    # when
    with serve(store, alpha, beta) as client:
        registry = client.app.state.registry
        kept = (registry.get('alpha'), registry.get('beta'))
        packs = [client.get(f'/plugins/{name}/i18n-de.js').text for name in ('alpha', 'beta')]
        catalog = messages.catalog('de')
        texts = [said('de', key) for key in ('working', 'still', 'answered')]

    # then
    assert [(x.state, x.reason) for x in kept] == [('ok', None), ('ok', None)]
    assert kept[0].plugin is alpha, 'a plug-in with nothing to leave out is kept as it is'
    assert kept[1].plugin.locales == (Locale('de', {'Open': 'Offen'}, {'slack.open': 'In {product} oeffnen'}, chat={'answered': 'Beantwortet'}),)
    assert packs == [PAGE_PACK % ('"de"', '{"Resolved": "Erledigt"}'), PAGE_PACK % ('"de"', '{"Open": "Offen"}')]
    assert catalog == {'slack.cancel': 'Abbrechen', 'slack.open': 'In {product} oeffnen'}
    assert texts == ['Arbeite...', ENGLISH['still'], 'Beantwortet']
    assert dropped(caplog) == [left_out('beta', "page['@th']", msg('plugins.why.locale_reserved', lang='de', key='@th')),
                               left_out('beta', "messages['slack.menu.chat']", msg('plugins.why.locale', limit=1000)),
                               left_out('beta', "chat['still']", msg('plugins.why.locale_chat_text', lang='de', key='still', limit=CHAT_TEXT_MAX))]
    assert [r.getMessage() for r in caplog.records if r.name in ('skopax.messages', 'skopax.chatui.text')] == [], 'the outbound catalog and the chat table never see a dropped text'


NOTICES = '/api/admin/notices'
PLUGINS_API = '/api/admin/plugins'


def notices_of(client: TestClient) -> list[dict]:
    return client.get(NOTICES, headers=AUTH).json()['items']


def drops_of(client: TestClient, plugin_id: str) -> list:
    return next(p for p in client.get(PLUGINS_API, headers=AUTH).json()['plugins'] if p['id'] == plugin_id)['localeDrops']


def test_one_dropped_text_gives_one_notice_and_one_card_line_with_its_count(store):
    # given
    chatty = minimal('chatty', 'Chatty', locales=(Locale('de', {'Resolved': 'Erledigt'}, chat={'still': LINKED, 'working': 'Arbeite...'}),))

    # when
    with serve(store, chatty) as client:
        found = client.app.state.registry.get('chatty')
        notices = [n for n in notices_of(client) if n['id'].startswith('locale:')]
        card = drops_of(client, 'chatty')

    # then
    assert found.state == 'ok'
    assert found.drops == (Drop('chatty', 'de', 'chat', "chat['still']", 'plugins.why.locale_chat_text'),) and found.dropped == (('de', 1),)
    assert notices == [{'id': 'locale:chatty:de', 'level': 'warn', 'text': msg('notices.locale_dropped_one', lang='de', plugin='Chatty', count=1)}]
    assert card == [{'lang': 'de', 'count': 1, 'text': msg('integrations.locale_dropped_one', lang='de', count=1), 'keys': ["chat['still']"], 'more': 0}]


def test_a_plugin_with_no_dropped_text_shows_neither_a_notice_nor_a_card_line(store):
    # given
    steady = minimal('steady', 'Steady', locales=(Locale('de', {'Resolved': 'Erledigt'}, chat={'working': 'Arbeite...'}),))

    # when
    with serve(store, steady) as client:
        found = client.app.state.registry.get('steady')
        notices = [n for n in notices_of(client) if n['id'].startswith('locale:')]
        card = drops_of(client, 'steady')

    # then
    assert (found.drops, found.dropped) == ((), ())
    assert notices == [] and card == []


def test_the_kept_records_stop_at_50_while_each_language_keeps_its_full_count(store):
    # given
    many = {f'@k{i}': 'x' for i in range(DROPS_MAX + 70)}
    shaky = minimal('shaky', 'Shaky', locales=(Locale('de', many | {'Resolved': 'Erledigt'}), Locale('fr', {'@a': 'x', '@b': 'y', 'Resolved': 'Resolu'})))

    # when
    with serve(store, shaky) as client:
        found = client.app.state.registry.get('shaky')
        notices = [n for n in notices_of(client) if n['id'].startswith('locale:')]
        card = drops_of(client, 'shaky')

    # then
    assert len(found.drops) == DROPS_MAX and {d.lang for d in found.drops} == {'de'}
    assert found.dropped == (('de', DROPS_MAX + 70), ('fr', 2))
    assert [n['text'] for n in notices] == [msg('notices.locale_dropped', lang='de', plugin='Shaky', count=DROPS_MAX + 70), msg('notices.locale_dropped', lang='fr', plugin='Shaky', count=2)]
    assert card == [{'lang': 'de', 'count': DROPS_MAX + 70, 'text': msg('integrations.locale_dropped', lang='de', count=DROPS_MAX + 70), 'keys': [f"page['@k{i}']" for i in range(5)], 'more': DROPS_MAX + 65},
                    {'lang': 'fr', 'count': 2, 'text': msg('integrations.locale_dropped', lang='fr', count=2), 'keys': [], 'more': 2}]


def test_a_non_admin_sees_no_locale_notice(store):
    # given
    shaky = minimal('shaky', 'Shaky', locales=(Locale('de', {'@a': 'x'}),))
    sessions = people(store)

    # when
    with TestClient(create_app(dataclasses.replace(sso_settings(), public_read=True), store, registry=Registry.of(shaky, directory=PLUGINS)), base_url='https://testserver') as client:
        asked = {}
        for who, cookie in sessions.items():
            client.cookies.clear()
            client.cookies.set(SESSION_COOKIE, cookie)
            asked[who] = client.get(NOTICES, follow_redirects=False)
        client.cookies.clear()
        anonymous = client.get(NOTICES, follow_redirects=False)

    # then
    assert [(who, res.status_code) for who, res in asked.items()] == [('viewer', 403), ('operator', 403)]
    assert anonymous.status_code in (303, 401)
    assert not any('locale:' in res.text for res in (*asked.values(), anonymous))


BAD_MESSAGES = {'not.a.key': 'x', 'routes.title': '{product} \u00b7 {etikett}', 'routes.why': '{label:>9}', 'routes.recommended': 'Empfohlen' + chr(7),
                'routes.info': 'B' * (messages.TEXT_MAX + 1)}


def test_refused_outbound_messages_are_drops_admins_see_and_the_outbound_text_falls_back_to_english(store):
    # given
    packed = minimal('packed', 'Packed', locales=(Locale('de', {'Resolved': 'Erledigt'}, messages=BAD_MESSAGES | {'routes.fixed': 'Behoben'}),))

    # when
    with serve(store, packed) as client:
        found = client.app.state.registry.get('packed')
        notices = [n for n in notices_of(client) if n['id'].startswith('locale:')]
        card = drops_of(client, 'packed')
        with messages.speaking('de'):
            shown = (msg('routes.title', label='SEO'), msg('routes.fixed'))

    # then
    assert found.dropped == (('de', 5),) and sorted(d.key for d in found.drops) == sorted(f"messages[{k!r}]" for k in BAD_MESSAGES)
    assert notices == [{'id': 'locale:packed:de', 'level': 'warn', 'text': msg('notices.locale_dropped', lang='de', plugin='Packed', count=5)}]
    assert card[0]['count'] == 5 and card[0]['text'] == msg('integrations.locale_dropped', lang='de', count=5)
    assert shown == (msg('routes.title', label='SEO'), 'Behoben') and 'etikett' not in shown[0]


def test_a_whole_part_left_out_counts_every_text_it_held():
    # given
    many = tuple((f'^a{i}$', 'b') for i in range(RULES_MAX + 1))
    plugin = pages(page_locale(rules=many))

    # when
    found = Registry.of(plugin, directory=PLUGINS).get('pages')

    # then
    assert found.dropped == (('de', RULES_MAX + 1),), 'the notice counts the texts that show in English, not the parts'


def test_one_dropped_text_reads_in_the_singular(store):
    # given
    chatty = minimal('chatty', 'Chatty', locales=(Locale('de', {'Resolved': 'Erledigt'}, chat={'still': LINKED}),))

    # when
    with serve(store, chatty) as client:
        [notice] = [n['text'] for n in notices_of(client) if n['id'].startswith('locale:')]
        [line] = [d['text'] for d in drops_of(client, 'chatty')]

    # then
    assert '1 text that shows' in notice and '1 texts' not in notice and line == '1 de text shows in English'


def test_a_switched_off_plugin_keeps_its_drops_on_its_card_but_raises_no_notice(store):
    # given
    shaky = minimal('shaky', 'Shaky', locales=(Locale('de', {'@a': 'x', 'Resolved': 'Erledigt'}),))

    # when
    with serve(store, shaky) as client:
        client.put('/api/admin/plugins/shaky', json={'enabled': False}, headers=AUTH)
        notices = [n for n in notices_of(client) if n['id'].startswith('locale:')]
        card = drops_of(client, 'shaky')

    # then
    assert notices == [] and card[0]['count'] == 1


@pytest.mark.parametrize('over', [{'core': '>=9.0'}, {'edition': 'enterprise'}])
def test_a_locked_or_incompatible_plugin_shows_no_drops_anywhere(store, over):
    # given
    held = minimal('held', 'Held', locales=(Locale('de', {'@a': 'x', 'Resolved': 'Erledigt'}),), **over)

    # when
    with serve(store, held) as client:
        state = client.app.state.registry.get('held').state
        notices = [n for n in notices_of(client) if n['id'].startswith('locale:')]
        card = drops_of(client, 'held')

    # then
    assert state in ('locked', 'incompatible') and notices == [] and card == []


def test_the_built_in_plugins_drop_no_locale_text():
    # when
    loaded = Registry.load({}, entry_points=False).loaded()

    # then
    assert [(x.id, x.dropped) for x in loaded if x.dropped] == [] and loaded


def test_a_whole_pack_left_out_counts_the_texts_it_held_not_its_parts():
    # given
    shaky = minimal('shaky', 'Shaky', locales=(Locale('not a tag', {'Resolved': 'Erledigt'}),))

    # when
    found = Registry.of(shaky, directory=PLUGINS).get('shaky')

    # then
    assert found.dropped == (('not a tag', 1),)
