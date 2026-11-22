import ast
import dataclasses
import logging
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import skopax
from skopax import messages, registry, runtime, tunables
from skopax.app import create_app
from skopax.config import load_settings
from skopax.contract import Locale
from skopax.investigator import prompt
from skopax.investigator.tools import Effects, Toolbox
from skopax.investigator.toolset import Proposals
from skopax.messages import MESSAGES, msg
from skopax.operators import Operators
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.slack import AUTO_BY, POST_ACTIONS, PROPOSAL_CONFIRM, Slack, answer_blocks, card_action_label, known_confirm, level_label, report_message, sender, task_button, task_confirm
from skopax.slackevents import Clicks
from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_anthropic import verdict
from tests.fake_slack import FakeSlackApi, fake_slack
from tests.test_investigator_tools import sources
from tests.plugin_kit import no_loop, registry_with
from tests.plugins.base import minimal
from tests.test_plugin_hooks import DEMO, served
from tests.test_slack import FakeSlack
from tests.test_slack_clicks import action_ask, action_payload, clicks, ephemerals, post_payload
from tests.test_slackevents import KEY, NOW, card_with_post, intake, reply_event, slack_ask, small_talk_capped
from tests.test_tool_registry import RUN_ID, counted, stored_run

SKOPAX = Path(skopax.__file__).parent
ADMIN = 'admin@example.com'
GERMAN_OPEN = 'In Skopax oeffnen'
GERMAN_CAPS = {'slackevents.ask_capped': 'Das Fragenlimit dieser Stunde ist erreicht; bitte gleich noch einmal versuchen.',
               'slackevents.user_capped': 'Du kannst hoechstens {cap} Fragen pro Stunde stellen; bitte gleich noch einmal versuchen.',
               'slackevents.small_talk_capped': 'Mein Plauderlimit fuer diese Stunde ist voll. Fragen zu Prod gehen weiter.'}
GERMAN_SLACK = {'slack.known.title': 'Bekanntes Rauschen?', 'slack.known.ok': 'Bekanntes Rauschen', 'slack.label.take': 'Uebernehmen', 'slack.label.resolve': 'Erledigt',
                'slack.label.known': 'Bekanntes Rauschen', 'slack.label.mute': 'Stumm {hours} h', 'slack.level.ok': 'ALLES GUT', 'slack.sender.auto': '{who} \u00b7 automatisch gesendet',
                'reports.no_prometheus': 'keine Daten, die Prometheus-Instanz {instance} ist nicht da'}
SLACK_TWINS = {'slackevents.feature_off': 'features.off', 'slackevents.not_ready': 'chat.not_ready', 'slackevents.needs.anthropic_key': 'features.needs.anthropic_key',
               'slackevents.needs.mode': 'features.needs.mode', 'tickets.jira_off': 'actions.jira_off'}
GERMAN_TASKS = {'slack.task.create': 'Aufgabe anlegen', 'slack.task.create_named': '{name}-Aufgabe anlegen', 'slack.task.create_ok': 'Anlegen',
                'slack.report.name': 'Deploy-Bericht', 'slack.report.check': 'Pruefung nach dem Deploy', 'slack.report.flagged': '{n} markiert', 'slack.report.clean': 'sauber',
                'slack.report.service': '{n} Dienst', 'slack.report.services': '{n} Dienste', 'slack.report.more': '+{n} weitere im Bericht'}
GERMAN_PROPOSALS = {f'{prefix}.{key}': text for prefix in ('proposals', 'proposal') for key, text in
                    (('none', 'Kein Vorschlag'), ('done', 'Schon bestaetigt'), ('gone', 'Die Integration ist aus'), ('failed', 'Die Aktion ist fehlgeschlagen'),
                     ('timed_out', 'Die Aktion kam nicht rechtzeitig zurueck'), ('unavailable', 'Gerade nicht pruefbar'), ('confirmed', '{by} hat bestaetigt: {title}'))}
GERMAN_REPORT = {'slack.report.flagged_head': 'Markiert', 'slack.report.shipped': 'Ausgeliefert', 'slack.report.ticket': '{n} Ticket', 'slack.report.tickets': '{n} Tickets',
                 'slack.report.more_tickets': '+{n} weitere', 'slack.report.open': 'Bericht oeffnen', 'slack.report.window': 'Fenster {start} bis {end} {zone}', 'slack.report.now': 'jetzt'}
GERMAN_UPDATE = {'slack.update.asked': '{text}\n_gefragt von {who}_', 'tools.update.asked': '{text}\n_gefragt von {who}_'}


def import_time(node: ast.AST):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        decorators = getattr(node, 'decorator_list', [])
        for part in [*decorators, *node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]:
            yield from import_time(part)
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from import_time(child)


def is_lookup(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    called = node.func
    return (isinstance(called, ast.Name) and called.id == 'msg') or (isinstance(called, ast.Attribute) and called.attr == 'msg' and isinstance(called.value, ast.Name) and called.value.id == 'messages')


def raw_template(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    held = node.value
    return (isinstance(held, ast.Name) and held.id == 'MESSAGES') or (isinstance(held, ast.Attribute) and held.attr == 'MESSAGES')


def entry(client, key: str) -> dict:
    return {e['key']: e for e in client.get('/api/admin/settings', headers=AUTH).json()['items']}[key]


def switch_off(client, plugin_id: str) -> None:
    client.app.state.runtime.set_enabled(plugin_id, False, ADMIN).join()


def test_no_message_is_looked_up_at_import():
    # given
    found = []

    # when
    for path in sorted(SKOPAX.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        where = path.relative_to(SKOPAX.parent)
        found += [f'{where}:{n.lineno} msg' for n in import_time(tree) if is_lookup(n)]
        if path != SKOPAX / 'messages.py':
            found += [f'{where}:{n.lineno} MESSAGES[' for n in ast.walk(tree) if raw_template(n)]

    # then
    assert found == []


@pytest.mark.usefixtures('no_core_packs')
def test_outbound_text_follows_the_outbound_language(store):
    # given
    with served(store, DEMO, language='de') as client:
        # when
        german = msg('slack.open')
        page = msg('access.login_first')
        switch_off(client, 'demo')
        english = msg('slack.open')

    # then
    assert DEMO.locales[0].messages['slack.open'] == 'In {product} oeffnen'
    assert german == GERMAN_OPEN
    assert page == MESSAGES['access.login_first']
    assert english == 'Open in Skopax'
    assert {key: MESSAGES[key] for key in SLACK_TWINS} == {key: MESSAGES[page_key] for key, page_key in SLACK_TWINS.items()}, 'a Slack twin reads like its page text in English'


@pytest.mark.parametrize('reply, key', [('off-topic', 'slackevents.off_topic'), ('ask-cap', 'slackevents.ask_capped'), ('user-cap', 'slackevents.user_capped'),
                                        ('small-talk-cap', 'slackevents.small_talk_capped')])
def test_a_canned_reply_follows_the_askers_language(store, reply, key):
    # given
    messages.set_catalogs([(locale.lang, dict(locale.messages) | GERMAN_CAPS) for locale in DEMO.locales])
    card_with_post(store)
    if reply == 'ask-cap':
        for i in range(10):
            slack_ask(store, thread_ts=f'1792224000.0009{i:02d}')
    if reply == 'user-cap':
        store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'slack:C1:1792224000.000900', 'text': 'q', 'cards': [], 'requestedBy': 'adam.baker@example.com'})
    if reply == 'small-talk-cap':
        small_talk_capped(store)
    capped = reply in ('ask-cap', 'user-cap')
    box, f = intake(store, gate_replies=[verdict(addressed=True, on_topic=capped, small_talk=reply == 'small-talk-cap', language='de')], user_cap=1)
    german = (DEMO.locales[0].messages | GERMAN_CAPS)[key].format(product='Skopax', cap=1)
    english = MESSAGES[key].format(product='Skopax', cap=1)
    together = threading.Barrier(2)
    said: dict[str, str] = {}

    def speak(lang: str) -> None:
        with messages.speaking(lang):
            together.wait(5)
            said[lang] = msg(key, cap=1)

    # when
    decision = box.handle(reply_event('<@UBOT> write me a poem'))
    threads = [threading.Thread(target=speak, args=(lang,)) for lang in ('de', 'en')]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    # then
    assert decision == reply
    assert [p['text'] for p in f.post.calls] == [german] and german != english
    assert said == {'de': german, 'en': english}
    assert msg(key, cap=1) == english


@pytest.mark.usefixtures('no_core_packs')
def test_bad_locale_entries_are_dropped_with_one_warning(store, caplog):
    # given
    bad = {'no.such.key': 'Hallo', 'slackevents.click.refused': 'Konnte die Aufgabe nicht anlegen', 'slackevents.user_capped': 'Hoechstens {cap} Fragen {x}',
           'slack.open': '{product} ' + 'y' * 3991, 'slack.cancel': 'Abbrechen\x07', 'slack.menu.chat': 'Plaudern',
           'slackevents.click.non_operator': 'Nur {product:>60} darf das', 'slackevents.click.action_non_operator': 'Nur {product!r} darf das'}
    plugin = dataclasses.replace(DEMO, locales=(Locale('de', {}, bad),))
    caplog.set_level(logging.WARNING, logger='skopax.messages')

    # when
    with served(store, plugin, language='de') as client:
        client.app.state.runtime.refresh()
        sent = [msg('slackevents.click.refused', reason='busy'), msg('slackevents.user_capped', cap=2), msg('slack.open'), msg('slack.cancel'), msg('slack.menu.chat'),
                msg('slackevents.click.non_operator'), msg('slackevents.click.action_non_operator')]
    dropped = messages.set_catalogs([('de', bad), ('de-', {'slack.menu.chat': 'x'}), ('DE', {'slack.menu.chat': 'x'})])
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.messages']

    # then
    assert len('{product} ' + 'y' * 3991) == 4001
    assert sorted(dropped) == [('DE', 'slack.menu.chat'), ('de', 'no.such.key'), ('de', 'slack.cancel'), ('de', 'slack.open'), ('de', 'slackevents.click.action_non_operator'),
                               ('de', 'slackevents.click.non_operator'), ('de', 'slackevents.click.refused'), ('de', 'slackevents.user_capped'), ('de-', 'slack.menu.chat')]
    assert sent == ['Could not open the task: busy', 'You can ask at most 2 questions an hour; try again in a bit.', 'Open in Skopax', 'Cancel', 'Plaudern',
                    'Only Skopax operators can open tasks.', 'Only Skopax operators can act on cards.']
    for key in ('no.such.key', 'slackevents.click.refused', 'slackevents.user_capped', 'slack.open', 'slack.cancel', 'slackevents.click.non_operator', 'slackevents.click.action_non_operator'):
        assert len([w for w in warned if w.startswith(f'Locale pack de: message {key} is left out (')]) == 1, key
    for key in ('slackevents.click.non_operator', 'slackevents.click.action_non_operator'):
        assert msg('locales.dropped', lang='de', key=key, reason=msg('locales.why.format')) in warned, key
    assert len(warned) == 9


@pytest.mark.usefixtures('no_core_packs')
def test_missing_translations_fall_back_to_english(store):
    # given
    with served(store, DEMO, language='de'):
        # when
        translated = msg('slack.open')
        missing = msg('slack.cancel')
        reminded = msg('reminder.still')
    messages.HELD[0] = ({'de': {'notes.state.muted': 'Stumm fuer {hours:d} h: {who}'}}, ('en', 'de'))
    with messages.speaking('de'):
        muted = msg('notes.state.muted', hours=1.5, who='bob')

    # then
    assert translated == GERMAN_OPEN
    assert (missing, reminded) == ('Cancel', 'Still ongoing.')
    assert 'slack.cancel' not in messages.catalog('de')
    assert muted == MESSAGES['notes.state.muted'].format(hours=1.5, who='bob'), 'a pack whose text fails to format sends the English text'


def test_a_slow_heal_does_not_hold_back_a_language_change(store, monkeypatch):
    # given
    release = threading.Event()
    with served(store, DEMO) as client:
        runtime = client.app.state.runtime
        runtime.hook_timeout = 0.1
        monkeypatch.setattr(client.app.state.sweeper, 'heal', lambda: release.wait(3))
        runtime.refresh()
        english = msg('slack.open')
        client.put('/api/admin/settings', json={'key': 'core.language', 'value': 'de'}, headers=AUTH)

        # when
        runtime.refresh()
        german = msg('slack.open')
    release.set()

    # then
    assert (english, german) == ('Open in Skopax', GERMAN_OPEN)


@pytest.mark.usefixtures('no_core_packs')
def test_outbound_language_lists_registered_languages(store):
    # given
    with served(store, DEMO) as client:
        # when
        offered = entry(client, 'core.language')
        stored = client.put('/api/admin/settings', json={'key': 'core.language', 'value': 'de'}, headers=AUTH)
        client.app.state.runtime.refresh()
        german = msg('slack.open')
        switch_off(client, 'demo')
        after = entry(client, 'core.language')
        english = msg('slack.open')
        kept = client.put('/api/admin/settings', json={'key': 'core.language', 'value': 'de'}, headers=AUTH)
        refused = client.put('/api/admin/settings', json={'key': 'core.language', 'value': 'fr'}, headers=AUTH)

    # then
    assert (offered['group'], offered['label'], offered['kind'], offered['live']) == ('General', 'Outbound language', 'choice', True)
    assert offered['choices'] == ['en', 'de'] and offered['default'] == 'en'
    assert offered['help'] == 'Language of what Skopax sends to Slack, Teams, Jira and direct messages: en, or a language a locale pack adds'
    assert stored.status_code == 200 and german == GERMAN_OPEN
    assert 'problem' not in offered
    assert after['value'] == 'de' and after['choices'] == ['en', 'de'] and english == 'Open in Skopax'
    assert after['problem'] == 'No loaded locale pack adds de, so English is used until one does', 'Admin shows the stored language that waits for its pack'
    assert kept.status_code == 200 and kept.json()['item']['value'] == 'de' and kept.json()['item']['problem'] == after['problem'], 'the waiting language saves again unchanged'
    assert refused.status_code == 422, 'another language with no pack is still refused'


def test_a_stored_outbound_language_applies_after_a_restart(store):
    # given
    with served(store, DEMO) as client:
        stored = client.put('/api/admin/settings', json={'key': 'core.language', 'value': 'de'}, headers=AUTH)

    # when
    with served(store, DEMO, add=False):
        said = msg('slack.open')

    # then
    assert stored.status_code == 200
    assert said == GERMAN_OPEN, 'the stored language is read before any pack loads, and it still applies once the pack is there'


@pytest.mark.usefixtures('no_core_packs')
def test_writing_language_comes_from_locale_packs(store, caplog):
    # given
    long_note = dataclasses.replace(DEMO, locales=(Locale('de', {'Demo': 'Demo'}, writing='x' * 1001),))

    # when
    with served(store, DEMO) as client:
        note = prompt.language_note('de')
        languages = prompt.writing_languages()
        offered = entry(client, 'investigate.language')['choices']
        switch_off(client, 'demo')
        after_note = prompt.language_note('de')
        after = entry(client, 'investigate.language')
        client.app.state.runtime.set_enabled('demo', True, ADMIN).join()
        client.put('/api/admin/settings', json={'key': 'investigate.language', 'value': 'de'}, headers=AUTH)
        switch_off(client, 'demo')
        waiting = entry(client, 'investigate.language')
        kept = client.put('/api/admin/settings', json={'key': 'investigate.language', 'value': 'de'}, headers=AUTH)
        refused = client.put('/api/admin/settings', json={'key': 'investigate.language', 'value': 'fr'}, headers=AUTH)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger='skopax.registry'):
        dropped = Registry.of(long_note).get('demo')

    # then
    assert DEMO.locales[0].writing == 'Write in German.'
    assert note == '\n\nWrite in German.' and languages == ('de',)
    assert offered == ['en', 'de'] and after['choices'] == ['en'] and 'problem' not in after and after_note == ''
    assert waiting['value'] == 'de' and waiting['choices'] == ['en', 'de'] and waiting['problem'] == 'No loaded locale pack adds de, so English is used until one does'
    assert (kept.status_code, kept.json()['item']['value'], refused.status_code) == (200, 'de', 422), 'the waiting language saves again; another one with no pack does not'
    assert (dropped.state, dropped.reason, dropped.plugin.locales) == ('ok', None, (Locale('de', {'Demo': 'Demo'}),)), 'a writing note that is too long is left out, and the plug-in loads'
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.registry'] == [msg('locales.part_dropped', lang='de', plugin='demo', part='writing',
                                                                                             reason=msg('plugins.why.locale', limit=registry.WRITING_MAX))]


@pytest.mark.usefixtures('no_core_packs')
def test_a_regional_language_matches_its_pack_whatever_its_case(store):
    # given
    regional = dataclasses.replace(DEMO, locales=(Locale('pt-BR', {}, {'slack.open': 'Abrir no {product}'}, writing='Escreva em portugues do Brasil.'),))
    loaded = load_settings(env={'INVESTIGATE_LANGUAGE': 'pt-BR'})

    # when
    with served(store, regional, investigate_language=loaded.investigate_language, language='pt-br') as client:
        client.app.state.runtime.refresh()
        card = entry(client, 'investigate.language')
        outbound = entry(client, 'core.language')
        sent = msg('slack.open')
        saved = client.put('/api/admin/settings', json={'key': 'investigate.language', 'value': 'PT-br'}, headers=AUTH)

    # then
    assert loaded.investigate_language == 'pt-BR'
    assert (card['value'], card['choices'], 'problem' in card) == ('pt-BR', ['en', 'pt-BR'], False)
    assert (outbound['value'], outbound['choices'], 'problem' in outbound) == ('pt-BR', ['en', 'pt-BR'], False)
    assert sent == 'Abrir no Skopax', 'the outbound language a case-insensitive match names is the one that is sent'
    assert (saved.status_code, saved.json()['item']['value']) == (200, 'pt-BR')


def test_every_language_check_uses_the_one_tag_pattern():
    # given
    page = (SKOPAX / 'web' / 'static' / 'i18n.js').read_text(encoding='utf-8')
    tag = messages.LANG_TAG
    scripts = {p.name: p.read_text(encoding='utf-8') for p in (SKOPAX / 'web' / 'static').glob('*.js')}

    # then
    assert tag.pattern == '[a-z]{2,3}(-[A-Za-z0-9]{2,8})?'
    assert registry.LANG is tag
    assert runtime.I18N_FILE.pattern == r'i18n-(' + tag.pattern + r')\.js'
    assert f"var SKOPAX_LANG_TAG = '{tag.pattern}';" in page and "skopax_lang=(' + SKOPAX_LANG_TAG + ')" in page
    assert [name for name, text in scripts.items() if '[a-z]{2,3}' in text] == ['i18n.js'] and 'SKOPAX_LANG_TAG' in scripts['overview.js']
    assert [bool(tag.fullmatch(t)) for t in ('de', 'pt-BR', 'DE', 'de_DE', 'german', 'de-')] == [True, True, False, False, False, False]
    assert [load_settings(env={'SKOPAX_LANGUAGE': v}).language for v in ('de', ' pt-BR ', '')] == ['de', 'pt-BR', 'en']
    assert tunables.choices_of({t.key: t for t in tunables.STATIC}['core.language']) == messages.languages() == ('en',)


def test_a_pack_cannot_change_the_built_in_english(caplog):
    # given
    caplog.set_level(logging.WARNING, logger='skopax.messages')
    english = msg('slack.open')

    # when
    dropped = messages.set_catalogs([('en', {'slack.open': 'Open it'}), ('en-GB', {'slack.open': 'Open it'}), ('de', {'slack.open': 'In {product} oeffnen'})])
    with messages.speaking('en-GB'):
        british = msg('slack.open')
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.messages']

    # then
    assert dropped == [('en', 'slack.open'), ('en-GB', 'slack.open')]
    assert msg('slack.open') == british == english and messages.languages() == ('en', 'de')
    assert warned == [f'Locale pack {lang}: message slack.open is left out ({MESSAGES["plugins.why.locale_english"]}), so its English text is sent' for lang in ('en', 'en-GB')]


def test_a_regional_pack_serves_its_primary_language():
    # given
    messages.set_catalogs([('pt-BR', {'slack.open': 'Abrir no {product}'}), ('pt-PT', {'slack.cancel': 'Cancelar'})])

    # when
    said = {}
    for lang in ('pt', 'pt-br', 'pt-AO', 'es'):
        with messages.speaking(lang):
            said[lang] = (messages.language(), msg('slack.open'))
    configured = {}
    for lang in ('pt', 'pt-BR'):
        messages.use_language(lambda lang=lang: lang)
        configured[lang] = (messages.language(), msg('slack.open'))

    # then
    assert said == {'pt': ('pt-BR', 'Abrir no Skopax'), 'pt-br': ('pt-BR', 'Abrir no Skopax'), 'pt-AO': ('pt-BR', 'Abrir no Skopax'), 'es': ('en', 'Open in Skopax')}
    assert configured == {'pt': ('en', 'Open in Skopax'), 'pt-BR': ('pt-BR', 'Abrir no Skopax')}, 'the configured language is matched exactly, as Admin offers it'


def test_a_dropped_key_is_cut_and_kept_on_one_line_in_the_log(caplog):
    # given
    caplog.set_level(logging.WARNING, logger='skopax.messages')
    odd = 'no.such\nkey' + 'k' * 300

    # when
    dropped = messages.set_catalogs([('de', {odd: 'x'})])
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.messages']

    # then
    assert dropped == [('de', odd)] and len(warned) == 1
    assert '\n' not in warned[0] and f"message {'no.such?key' + 'k' * (messages.SHOWN_MAX - 11)} is left out" in warned[0]


def test_every_investigator_need_has_a_slack_twin_and_a_missing_one_falls_back(store):
    # given
    app = create_app(settings(), store)
    needs = [need for need, _ in app.state.feature_needs['investigator']]

    # when
    app.state.feature_needs['investigator'] = (('mode', lambda: True), ('chat_ui', lambda: True))
    said = app.state.intake.not_ready()

    # then
    assert needs == ['anthropic_key', 'openai_key', 'mode'] and [n for n in needs if f'slackevents.needs.{n}' not in MESSAGES] == []
    assert said == ' '.join([msg('slackevents.not_ready'), msg('slackevents.needs.mode'), msg('features.needs.chat_ui')]), 'a need without a Slack twin uses its page text'


def test_the_language_step_warns_once_about_a_value_that_is_no_tag_and_names_its_own_failure(store, caplog, monkeypatch):
    # given
    caplog.set_level(logging.WARNING, logger='skopax')
    monkeypatch.setattr(Runtime, 'loop', no_loop)

    # when
    with served(store, DEMO, language='german') as client:
        runtime_ = client.app.state.runtime
        runtime_.refresh()
        healed = []
        monkeypatch.setattr(client.app.state.sweeper, 'heal', lambda: healed.append(True))
        monkeypatch.setattr(runtime_, 'locales', lambda: 1 / 0)
        runtime_.refresh()
        runtime_.refresh()
    logged = [r.getMessage() for r in caplog.records]

    # then
    assert [m for m in logged if 'german' in m] == ['The outbound language german is not a language tag such as de or pt-BR, so English is sent']
    assert [m for m in logged if 'step languages' in m] == ['runtime refresh step languages failed (ZeroDivisionError), keeping the last data']
    assert healed == [True, True] and not [m for m in logged if 'step heal' in m], 'the heal step still runs and is not blamed'


def test_slack_labels_follow_the_outbound_language_while_stored_labels_and_the_deploy_flag_stay_english(store):
    # given
    messages.set_catalogs([('de', GERMAN_SLACK)])
    messages.use_language(lambda: 'de')
    card_with_post(store)
    call = {'key': KEY, 'action': 'mute', 'hours': 6}
    act = action_ask(store, call)
    box, applier, api = clicks(store)

    # when
    clicked = box.handle(action_payload(act['id'], call=call))
    confirm = known_confirm()
    labels = [card_action_label({'action': a}) for a in ('take', 'resolve', 'known')]
    flag = msg('reports.no_prometheus', instance='east')

    # then
    assert clicked == 'applied'
    assert (applier.calls[0]['label'], applier.calls[0]['reason']) == (f'Mute 6 h from Slack: {KEY}', 'Mute 6 h from Slack by adam.baker@example.com'), 'stored labels stay English'
    assert next(p for m, p in api.calls if m == 'chat.update')['blocks'][-1]['elements'][0]['text'] == 'Stumm 6 h \u00b7 adam.baker@example.com'
    assert (confirm['title']['text'], confirm['confirm']['text']) == ('Bekanntes Rauschen?', 'Bekanntes Rauschen')
    assert labels == ['Uebernehmen', 'Erledigt', 'Bekanntes Rauschen']
    assert (level_label('ok'), level_label('crit'), sender(AUTO_BY)) == ('ALLES GUT', 'CRITICAL', 'Investigator \u00b7 automatisch gesendet')
    assert flag == 'no data, the Prometheus instance east is not available' and 'reports' not in messages.OUTBOUND, 'the deploy flag is stored and shown on the page, so it stays English'


def test_task_buttons_and_deploy_report_words_follow_the_outbound_language():
    # given
    messages.set_catalogs([('de', GERMAN_TASKS)])
    messages.use_language(lambda: 'de')
    flagged = [{'svc': f'svc-{i}', 'ns': 'shop', 'flags': ['restarts=3']} for i in range(10)]

    # when
    plain, named = task_button('a1')['text']['text'], task_button('a1', {'project': 'WEB'})['text']['text']
    confirm = task_confirm('Checkout null guard', {'project': None})
    one = report_message({'rows': [{'svc': 'orders-service', 'ns': 'shop'}]}, 'https://w/#r=1', 'token')['text']
    many = report_message({'rows': flagged, 'brief': {'level': 'warn', 'headline': 'h', 'points': [f'p{i}' for i in range(8)]}}, 'https://w/#r=1', 'token')
    blocks = [b['text']['text'] for b in many['attachments'][0]['blocks'] if b['type'] == 'section']

    # then
    assert (plain, named) == ('Aufgabe anlegen', 'WEB-Aufgabe anlegen')
    assert (confirm['title']['text'], confirm['confirm']['text']) == ('Aufgabe anlegen?', 'Anlegen')
    assert one == ':large_green_circle: *Deploy-Bericht* \u00b7 Pruefung nach dem Deploy \u00b7 ALL CLEAR \u00b7 1 Dienst \u00b7 sauber'
    assert many['text'] == ':large_orange_circle: *Deploy-Bericht* \u00b7 Pruefung nach dem Deploy \u00b7 WARNING \u00b7 10 Dienste \u00b7 10 markiert'
    assert blocks[0].endswith('_+2 weitere im Bericht_') and blocks[1].endswith('_+2 weitere im Bericht_')


def test_an_already_applied_reply_names_the_claimed_action_in_the_outbound_language_while_the_claim_stays_english(store):
    # given
    messages.set_catalogs([('de', GERMAN_SLACK | {'slackevents.click.applied': 'Schon angewandt: {label} \u00b7 {login}'})])
    messages.use_language(lambda: 'de')
    card_with_post(store)
    doc = store.add_action({'type': 'post-buttons', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardActions': {k: {'key': KEY, 'action': k} for k in POST_ACTIONS}}, 'done', 'buttons')
    box, applier, api = clicks(store)

    # when
    took = box.handle(post_payload(doc['id'], 'take'))
    late = box.handle(post_payload(doc['id'], 'resolve'))
    claim = store.get_action(doc['id'])['cardActionApplied']

    # then
    assert (took, late) == ('applied', 'claimed')
    assert claim['label'] == 'Take', 'the stored claim keeps the English label'
    assert ephemerals(api) == ['Schon angewandt: Uebernehmen \u00b7 adam.baker@example.com'], 'the reply names the claimed Take, not the clicked Resolve, in the outbound language'


def test_proposal_texts_on_the_page_and_in_the_activity_stay_english_while_the_slack_reply_follows_the_outbound_language(store):
    # given
    calls = []
    plugin = dataclasses.replace(counted(DEMO, 'demo_propose', calls, raises=RuntimeError('boom'), field='confirm'), locales=(Locale('de', {}, GERMAN_PROPOSALS),))
    stored_run(store)
    stored_run(store, run_id='run-slack')
    slack, post, api = fake_slack(emails={'U1': 'adam.baker@example.com'})
    blocks = answer_blocks('A restart is proposed.', None, None, confirm={'run': 'run-slack', 'title': 'Restart the demo worker'})

    def click(run_id: str) -> dict:
        return {'type': 'block_actions', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'}, 'message': {'ts': '1792224000.000300', 'thread_ts': '1792224000.000100', 'text': 'x', 'blocks': blocks},
                'actions': [{'action_id': PROPOSAL_CONFIRM, 'value': run_id}]}

    # when
    with served(store, plugin, language='de') as client:
        box = Clicks(store, slack, Operators(lambda email: True, slack, settings(), lambda: NOW), lambda action: 'done', confirm=Proposals(store, client.app.state.runtime).confirm,
                     background=lambda job: job())
        page = client.post(f'/api/investigations/{RUN_ID}/proposal', headers=AUTH)
        again = client.post(f'/api/investigations/{RUN_ID}/proposal', headers=AUTH)
        missing = client.post('/api/investigations/no-such-run/proposal', headers=AUTH)
        box.handle(click('run-slack'))
        box.handle(click(RUN_ID))
        box.handle(click('no-such-run'))
        english = {key: msg(key) for key in ('proposal.done', 'proposal.none', 'proposal.failed')}
    activity = sorted(a['text'] for a in store.list_activity(None, 20) if a.get('kind') == 'proposal')
    page_by = store.investigation(RUN_ID)['proposal']['confirmedBy']

    # then
    assert english == {'proposal.done': 'This proposal was already confirmed', 'proposal.none': 'This run has no proposal',
                       'proposal.failed': 'The confirmed action failed and may or may not have happened: someone should check it, as this proposal cannot be confirmed again'}
    assert (page.status_code, page.json()) == (200, {'ok': False, 'summary': english['proposal.failed'], 'outcome': 'failed'})
    assert (again.status_code, again.json()) == (409, {'detail': english['proposal.done']})
    assert (missing.status_code, missing.json()) == (404, {'detail': english['proposal.none']})
    assert activity == sorted(f'{by} confirmed: Restart the demo worker \u00b7 {english["proposal.failed"]}' for by in (page_by, 'adam.baker@example.com'))
    assert [p['text'] for p in post.posted] == ['Die Aktion ist fehlgeschlagen']
    assert next(p for m, p in api.calls if m == 'chat.update')['blocks'][-1]['elements'][0]['text'] == 'adam.baker@example.com hat bestaetigt: Restart the demo worker'
    assert ephemerals(api) == ['Schon bestaetigt', 'Kein Vorschlag']
    assert 'proposals' in messages.OUTBOUND and 'proposal' not in messages.OUTBOUND
    assert {k: v for k, v in MESSAGES.items() if k.startswith('proposal.')} == {'proposal.' + k.partition('.')[2]: v for k, v in MESSAGES.items() if k.startswith('proposals.') and k not in ('proposals.button', 'proposals.non_operator')}, \
        'every page text has a Slack twin with the same English, so a Slack reply by code always finds its key'


def test_a_slack_update_from_an_ask_goes_out_in_the_outbound_language_and_is_stored_in_english(store):
    # given
    messages.set_catalogs([('de', GERMAN_UPDATE)])
    messages.use_language(lambda: 'de')
    card_with_post(store)
    sent = []
    b = Toolbox(sources(), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: sent.append(text) or {}), [KEY], 'live', NOW, 'https://skopax.example.com', run_id='run-1',
                ask=True, asker='alice@example.com')

    # when
    out, failed = b.call('slack_update', {'key': KEY, 'text': 'Back to normal.'})
    stored = [a['text'] for a in store.list_activity(None, 20) if a.get('kind') == 'write']

    # then
    assert not failed, out
    assert sent == ['Back to normal.\n_gefragt von alice_']
    assert [w['summary'] for w in b.writes] == ['Back to normal.\n_asked by alice_']
    assert stored == [f'slack_update \u00b7 {KEY} \u00b7 Back to normal.\n_asked by alice_']
    assert 'tools' not in messages.OUTBOUND


def test_the_rest_of_the_deploy_report_follows_the_outbound_language():
    # given
    messages.set_catalogs([('de', GERMAN_REPORT)])
    messages.use_language(lambda: 'de')
    dot = ' ' + chr(0xb7) + ' '
    many = {'windowFrom': '2026-10-15T06:00:00Z', 'rows': [{'svc': 'orders-service', 'ns': 'shop', 'flags': ['restarts=3'], 'issues': [f'OPS-{i}' for i in range(1, 14)]}]}
    one = {'rows': [{'svc': 'orders-service', 'ns': 'shop', 'issues': ['OPS-1']}]}

    # when
    blocks = report_message(many, 'https://w/#r=1', 'token')['attachments'][0]['blocks']
    single = report_message(one, 'https://w/#r=1', 'token')['attachments'][0]['blocks']
    sections = [b['text']['text'] for b in blocks if b['type'] == 'section']

    # then
    assert [t for t in sections if t.startswith('*Markiert*')] == [f'*Markiert*\n\u2022 *orders-service*{dot}restarts 3']
    assert f'*Ausgeliefert*{dot}13 Tickets\n' + ', '.join(f'OPS-{i}' for i in range(1, 13)) + ' +1 weitere' in sections
    assert f'*Ausgeliefert*{dot}1 Ticket\nOPS-1' in [b['text']['text'] for b in single if b['type'] == 'section']
    assert next(b for b in blocks if b['type'] == 'actions')['elements'][0]['text']['text'] == 'Bericht oeffnen'
    assert blocks[-1]['elements'][0]['text'].startswith(f'Fenster 06:00 bis jetzt UTC{dot}')


def test_the_bell_notice_of_an_asked_slack_update_stays_english_while_the_thread_follows_the_outbound_language(store):
    # given
    store.save(state(anomaly(KEY, num=14, owner='nina.white@example.com')), None)
    store.slack_posted(KEY, {'channel': 'C1', 'ts': '1792224000.000100', 'at': '2026-10-15T06:00:00Z', 'by': 'server'})
    transport = FakeSlack()
    pack = minimal('outbound-de', 'Outbound de', locales=(Locale('de', {}, GERMAN_UPDATE),))
    app = create_app(dataclasses.replace(settings(), language='de'), store, registry=registry_with(pack), slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi()))

    # when
    with TestClient(app):
        b = Toolbox(sources(), store, app.state.investigator.effects, [KEY], 'live', NOW, 'https://skopax.example.com', run_id='run-1', ask=True, asker='alice@example.com')
        out, failed = b.call('slack_update', {'key': KEY, 'text': 'Back to normal.'})
    notes = [n['note'] for n in store.notices_of('nina.white', 50) if n['kind'] == 'thread']

    # then
    assert not failed, out
    assert transport.calls[-1][1]['text'].startswith('Back to normal.\n_gefragt von alice_')
    assert notes == ['Back to normal. _asked by alice_'], 'the bell notice is stored, so it stays English'


def test_a_saved_outbound_language_applies_at_once_without_waiting_for_a_refresh(store):
    # given
    with served(store, DEMO) as client:
        client.app.state.runtime.refresh()
        english = msg('slack.open')

        # when
        client.put('/api/admin/settings', json={'key': 'core.language', 'value': 'de'}, headers=AUTH)
        german = msg('slack.open')

    # then
    assert (english, german) == ('Open in Skopax', GERMAN_OPEN)
