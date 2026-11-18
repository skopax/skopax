import re
import unicodedata
from pathlib import Path

import pytest

import skopax
from skopax.messages import FOLD_LETTERS, MESSAGES, fold, msg

MODULES = ('slack', 'statenotes', 'clearnotes', 'reminders', 'assign', 'autosend', 'tickets', 'postdeploy', 'slackevents', 'tags', 'app', 'messages', 'chatui/__init__', 'chatui/text', 'chattext')


def test_msg_formats_and_refuses_unknown_keys():
    # when
    text = msg('postdeploy.head', date='25 Oct', count=3, span='14:05', zone='UTC', clean=2)

    # then
    assert text == '25 Oct deploy check: 3 services (14:05 UTC), 2 clean'
    with pytest.raises(KeyError):
        msg('no.such.key')


def test_fold_strips_accents_and_case():
    # then
    assert fold('Ren' + chr(0xe9) + 'e M' + chr(0xfc) + 'ller') == 'renee muller'


def test_fold_turns_letters_without_a_decomposition_into_their_base_letters():
    # given
    names = '\u00d8stergaard \u0141ukasz \u0110or\u0111e \u00c6gir \u00de\u00f3r Stra\u00dfe \u0152uvre \u0126al N\u0131ko \u01fe'

    # then
    assert fold(names) == 'ostergaard lukasz dorde aegir thor strasse oeuvre hal niko o'


def test_the_page_folds_the_same_letters_as_the_server():
    # given
    core = (Path(skopax.__file__).parent / 'web' / 'static' / 'core.js').read_text(encoding='utf-8')
    table = re.search(r'var FOLD_LETTERS = \{([^}]*)\};', core)

    # when
    page = {key.encode('ascii').decode('unicode_escape'): value for key, value in re.findall(r"'([^']+)': '([a-z]+)'", table.group(1))} if table else {}

    # then
    assert page == FOLD_LETTERS
    assert len(FOLD_LETTERS) >= 8 and all(len(k) == 1 and k == k.lower() and v.isascii() for k, v in FOLD_LETTERS.items())


def test_no_non_ascii_letters_in_outbound_modules():
    for name in MODULES:
        # given
        text = (Path(skopax.__file__).parent / f'{name}.py').read_text()

        # then
        assert not [ch for ch in text if ord(ch) > 127 and unicodedata.category(ch).startswith('L')], name


def test_every_message_is_ascii_letters_only():
    for key, template in MESSAGES.items():
        assert not [ch for ch in template if ord(ch) > 127 and unicodedata.category(ch).startswith('L')], key


def test_the_web_and_marketing_blocks_sit_at_their_anchor_in_id_order():
    # given
    keys = list(MESSAGES)
    start = keys.index('kubernetes.test.agent_cluster') + 1
    end = keys.index('agent.relay_not_configured')

    # when
    blocks = list(dict.fromkeys(k.split('.', 1)[0] for k in keys[start:end]))

    # then
    assert blocks == ['app-stores', 'google', 'kubernetes', 'langfuse', 'pagescan', 'screenshot', 'website']
