import pytest

from tests.test_engine import NOW, waiting
from skopax import locales, messages
from skopax.cardposts import auto_head
from skopax.engine import auto_resolve, window_text
from skopax.slack import AUTO_BY, sender
from skopax.statenotes import auto_text
from tools import locale_source

H = 3600
LANGS = ('de', 'es', 'fr', 'tr')
WINDOW = ('engine.window.hour', 'engine.window.hours', 'engine.window.day', 'engine.window.days')
SHORT = {'de': 'Nicht mehr aktiv', 'es': 'Dej\u00f3 de dispararse', 'fr': 'N\u2019\u00e9met plus', 'tr': 'Sinyal durdu'}
LOWER = {'de': 'nicht mehr aktiv', 'es': 'dej\u00f3 de dispararse', 'fr': 'n\u2019\u00e9met plus', 'tr': 'sinyal durdu'}
LONG = 'Stopped firing \u00b7 unassigned'
ASSIGNED = 'Stopped firing \u00b7 awaiting action \u00b7 assigned to'
INVESTIGATOR = {'de': 'Investigator', 'es': 'Investigador', 'fr': 'Enqu\u00eateur', 'tr': 'Investigator'}
AUTO_SENT = {'de': 'automatisch gesendet', 'es': 'env\u00edo autom\u00e1tico', 'fr': 'envoi automatique', 'tr': 'otomatik g\u00f6nderildi'}


def speak(lang: str) -> None:
    messages.set_catalogs([(pack.lang, dict(pack.messages)) for pack in locales.packs()])
    messages.use_language(lambda: lang)


@pytest.mark.parametrize('lang', LANGS)
def test_every_pack_says_the_window_hours_and_days(lang):
    # given
    pack = locales.read(lang)

    # then
    assert all(pack.messages.get(key, '').startswith('{n} ') for key in WINDOW), [key for key in WINDOW if key not in pack.messages]


def test_tr_window_texts_read_saat_and_gun_in_the_slack_head_and_thread():
    # given
    speak('tr')

    # when
    spans = [window_text(h) for h in (1, 6, 24, 72)]
    head = auto_head({'hours': 48})
    business = auto_head({'hours': 6, 'businessTime': True})
    note = auto_text(72)

    # then
    assert spans == ['1 saat', '6 saat', '1 g\u00fcn', '3 g\u00fcn']
    assert head == {'kind': 'auto', 'span': '2 g\u00fcn'}
    assert business == {'kind': 'auto', 'span': '6 i\u015f saati'}
    assert '3 g\u00fcn' in note and 'days' not in note


def test_the_stored_auto_resolve_note_stays_english_under_another_language():
    # given
    speak('tr')
    state = {'anomalies': [waiting('log|a|ERROR|plain', 7 * H)]}

    # when
    done = auto_resolve(state, NOW, 6)

    # then
    card = state['anomalies'][0]
    assert done[0]['text'].endswith('nobody acted in 6 hours')
    assert card['note']['text'].endswith('nobody acted in 6 hours')


def test_the_translator_source_lists_the_window_texts_as_outbound():
    # when
    found = locale_source.source()['messages']

    # then
    assert {key: found[key] for key in WINDOW} == {key: messages.MESSAGES[key] for key in WINDOW}
    assert 'engine.grouped.twin' not in found and 'engine.auto.event' not in found


@pytest.mark.parametrize('lang', LANGS)
def test_every_pack_translates_takes_on_the_page(lang):
    # given
    pack = locales.read(lang)

    # then
    assert pack.page.get('Takes', 'Takes') != 'Takes'
    if lang == 'tr':
        assert pack.page['Takes'] == 'Ald\u0131klar\u0131'


def test_the_page_uses_the_english_slack_words_for_a_stopped_card():
    # then
    assert messages.MESSAGES['slack.status.cleared'] == LONG


@pytest.mark.parametrize('lang', LANGS)
def test_every_pack_reads_a_stopped_card_with_its_own_slack_words_on_the_page(lang):
    # given
    pack = locales.read(lang)
    long, short = pack.messages['slack.status.cleared'], SHORT[lang]

    # then
    assert pack.page[LONG] == long and long.startswith(short + ' \u00b7 ')
    assert pack.page['Stopped firing'] == short and pack.page['stopped firing'] == LOWER[lang]
    assert pack.page[LONG + ' {0} {M} {1}'].startswith(long + ' ')
    assert pack.page['Stopped firing {0} {M} {1}'].startswith(short + ' ') and ('^Stopped firing (\\d[^,]*)$', short + ' $1') in pack.rules
    assert ('^' + LONG + ' (\\d.*)$', long + ' $1') in pack.rules
    assigned = pack.messages['slack.status.cleared_assigned'].replace(' {who}', '')
    assert pack.page[ASSIGNED] == assigned and assigned.startswith(short + ' \u00b7 ')
    assert [rule[1] for rule in pack.rules if rule[0].startswith('^' + ASSIGNED + ' ')] == [assigned + (' $1 $2. $3 $4' if lang == 'de' else ' $1 $2 $3 $4')]
    assert pack.page['Stopped firing \u00b7 no action needed'].startswith(short + ' \u00b7 ')
    gone = ('Alert went quiet', 'Went quiet', 'Alert went quiet {0} {M} {1}', 'Went quiet \u00b7 no action needed', 'Cleared {0} {M} {1}', 'Cleared \u00b7 no action needed')
    assert [key for key in gone if key in pack.page] == []
    assert [rule for rule in pack.rules if rule[0] in ('^Cleared (\\d.*)$', '^Alert went quiet (\\d.*)$')] == []


def test_the_auto_sent_line_names_the_investigator_for_every_model():
    # when
    named = [sender(AUTO_BY, by) for by in (None, 'Claude (pod)', 'token', 'token:bootstrap')]

    # then
    assert named == ['Investigator \u00b7 auto-sent'] * 4
    assert sender(AUTO_BY, 'token:release-bot') == 'release-bot \u00b7 auto-sent'


@pytest.mark.parametrize('lang', LANGS)
def test_every_pack_names_the_investigator_on_the_auto_sent_line(lang):
    # given
    speak(lang)

    # when
    got = sender(AUTO_BY, None)

    # then
    assert got == f'{INVESTIGATOR[lang]} \u00b7 {AUTO_SENT[lang]}'
