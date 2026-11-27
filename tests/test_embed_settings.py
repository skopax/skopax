import dataclasses
import json
import logging
import re
from pathlib import Path

import pytest

from skopax import locales, messages, settingcards
from skopax.config import EMBED_SOURCES, load_settings
from skopax.features import BY_ID, BY_KEY, Features
from skopax.messages import msg
from skopax.tunables import Invalid, Live, TunableStore, registry
from tests.conftest import settings

KEYS = {
    'embed.model': ('embed_model', 'text'),
    'embed.query-prefix': ('embed_query_prefix', 'text'),
    'embed.passage-prefix': ('embed_passage_prefix', 'text'),
    'embed.timeout-seconds': ('embed_timeout_seconds', 'int'),
    'embed.batch-timeout-seconds': ('embed_batch_timeout_seconds', 'int'),
    'embed.batch': ('embed_batch', 'int'),
    'embed.backfill-per-pass': ('embed_backfill_per_pass', 'int'),
    'embed.docs-per-pass': ('embed_docs_per_pass', 'int'),
    'embed.max-vectors': ('embed_max_vectors', 'int'),
    'embed.sources': ('embed_sources', 'list'),
    'investigate.similar-in-brief': ('investigate_similar_in_brief', 'bool'),
}
DEPLOYMENT_ONLY = ('embed.url', 'embed.api-key', 'embed.allow-public')
BOUNDS = {
    'SKOPAX_EMBED_TIMEOUT_SECONDS': ('embed_timeout_seconds', 3, 1, 60),
    'SKOPAX_EMBED_BATCH_TIMEOUT_SECONDS': ('embed_batch_timeout_seconds', 60, 5, 300),
    'SKOPAX_EMBED_BATCH': ('embed_batch', 8, 1, 32),
    'SKOPAX_EMBED_BACKFILL_PER_PASS': ('embed_backfill_per_pass', 200, 0, 5000),
    'SKOPAX_EMBED_DOCS_PER_PASS': ('embed_docs_per_pass', 200, 0, 2000),
    'SKOPAX_EMBED_MAX_VECTORS': ('embed_max_vectors', 300000, 1000, 2000000),
}


def test_the_defaults_are_off_and_follow_the_design():
    # given
    s = load_settings(env={})

    # then
    assert s.semantic_search_on is False
    assert (s.embed_url, s.embed_api_key, s.embed_allow_public) == ('', '', False)
    assert s.embed_model == 'intfloat/multilingual-e5-small'
    assert (s.embed_timeout_seconds, s.embed_batch_timeout_seconds, s.embed_batch) == (3, 60, 8)
    assert (s.embed_backfill_per_pass, s.embed_docs_per_pass, s.embed_max_vectors) == (200, 200, 300000)
    assert s.embed_sources == ('runs', 'cards', 'changes', 'knowledge', 'docs', 'logs') == EMBED_SOURCES
    assert s.investigate_similar_in_brief is True


def test_the_environment_names_follow_the_other_settings():
    # given
    env = {'SKOPAX_FEATURE_SEMANTIC_SEARCH': 'true', 'SKOPAX_EMBED_URL': ' http://skopax-embed/ ', 'SKOPAX_EMBED_API_KEY': 'k-secret', 'SKOPAX_EMBED_MODEL': 'org/other-model',
           'SKOPAX_EMBED_ALLOW_PUBLIC': 'true', 'SKOPAX_EMBED_SOURCES': 'runs, cards', 'INVESTIGATE_SIMILAR_IN_BRIEF': 'false'}

    # when
    s = load_settings(env=env)

    # then
    assert s.semantic_search_on is True and s.embed_url == 'http://skopax-embed' and s.embed_api_key == 'k-secret'
    assert s.embed_model == 'org/other-model' and s.embed_allow_public is True
    assert s.embed_sources == ('runs', 'cards') and s.investigate_similar_in_brief is False


def test_the_config_service_wins_over_the_environment():
    # given
    config = {'embed.url': 'http://from-config', 'embed.batch': '4'}

    # when
    s = load_settings(fetch=lambda: config, env={'SKOPAX_EMBED_URL': 'http://from-env', 'SKOPAX_EMBED_BATCH': '16'})

    # then
    assert (s.embed_url, s.embed_batch) == ('http://from-config', 4)


def test_the_key_never_shows_in_the_repr():
    # given
    s = load_settings(env={'SKOPAX_EMBED_API_KEY': 'k-very-secret'})

    # then
    assert 'k-very-secret' not in repr(s) and 'k-very-secret' not in str(dataclasses.asdict(dataclasses.replace(s, embed_api_key='')))


@pytest.mark.parametrize('var', sorted(BOUNDS))
def test_each_number_is_bounded_and_garbage_falls_back_to_the_default(var, caplog):
    # given
    name, default, low, high = BOUNDS[var]

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.config'):
        inside = getattr(load_settings(env={var: str(low)}), name), getattr(load_settings(env={var: str(high)}), name)
        below = getattr(load_settings(env={var: str(low - 1)}), name)
        above = getattr(load_settings(env={var: str(high + 1)}), name)
        junk = getattr(load_settings(env={var: 'many'}), name)

    # then
    assert inside == (low, high)
    assert (below, above) == (low, high)
    assert junk == default


def test_the_prefixes_follow_an_e5_model_unless_the_deployment_says_otherwise():
    # given
    e5 = load_settings(env={})
    other = load_settings(env={'SKOPAX_EMBED_MODEL': 'org/plain-model'})
    named = load_settings(env={'SKOPAX_EMBED_MODEL': 'org/plain-model', 'SKOPAX_EMBED_QUERY_PREFIX': 'ask:', 'SKOPAX_EMBED_PASSAGE_PREFIX': 'doc: '})
    big = load_settings(env={'SKOPAX_EMBED_MODEL': 'intfloat/multilingual-E5-large'})

    # then
    assert (e5.query_prefix, e5.passage_prefix) == ('query: ', 'passage: ')
    assert (big.query_prefix, big.passage_prefix) == ('query: ', 'passage: ')
    assert (other.query_prefix, other.passage_prefix) == ('', '')
    assert (named.query_prefix, named.passage_prefix) == ('ask: ', 'doc: ')


def test_a_model_change_from_admin_moves_the_prefix_defaults_with_it():
    # given
    base = settings()
    changed = dataclasses.replace(base, embed_model='org/plain-model')

    # then
    assert (base.query_prefix, base.passage_prefix) == ('query: ', 'passage: ')
    assert (changed.query_prefix, changed.passage_prefix) == ('', '')


def test_an_unknown_source_is_dropped_with_a_warning_and_duplicates_collapse(caplog):
    # when
    with caplog.at_level(logging.WARNING, logger='skopax.config'):
        s = load_settings(env={'SKOPAX_EMBED_SOURCES': 'runs, nonsense, RUNS, logs'})

    # then
    assert s.embed_sources == ('runs', 'logs') and 'nonsense' in caplog.text


def test_the_settings_are_tunables_with_changes_recorded_and_the_deployment_ones_are_not():
    # given
    known = {t.key: t for t in registry(())}

    # then
    assert {k: (known[k].field, known[k].kind) for k in KEYS} == KEYS
    assert all(key not in known for key in DEPLOYMENT_ONLY)
    assert known['core.semantic-search'].kind == 'bool' and known['core.semantic-search'].group == 'Features'
    assert (known['embed.timeout-seconds'].low, known['embed.timeout-seconds'].high) == (1, 60)
    assert (known['embed.batch-timeout-seconds'].low, known['embed.batch-timeout-seconds'].high) == (5, 300)
    assert (known['embed.batch'].low, known['embed.batch'].high) == (1, 32)
    assert (known['embed.backfill-per-pass'].low, known['embed.backfill-per-pass'].high) == (0, 5000)
    assert (known['embed.docs-per-pass'].low, known['embed.docs-per-pass'].high) == (0, 2000)
    assert (known['embed.max-vectors'].low, known['embed.max-vectors'].high) == (1000, 2000000)
    assert all(settingcards.CARD_OF[key][:2] == ('Investigator', 'Search by meaning') for key in KEYS)


def test_the_feature_is_listed_with_the_label_search_by_meaning():
    # then
    feature = BY_KEY['core.semantic-search']
    assert (feature.id, feature.label, feature.edition) == ('semantic-search', 'Search by meaning', 'community') and BY_ID['semantic-search'] is feature


@pytest.fixture
def live(store):
    return Live(settings(), TunableStore(store.db))


def test_the_feature_refuses_to_turn_on_while_the_url_is_empty(live):
    # given
    features = Features(live, lambda: 'community')

    # when
    with pytest.raises(Invalid) as refused:
        features.set('semantic-search', True, 'alice@example.com')
    with pytest.raises(Invalid):
        live.set('core.semantic-search', 'true', 'alice@example.com')
    off = features.set('semantic-search', False, 'alice@example.com')

    # then
    assert refused.value.text == msg('embed.needs_url') and off['on'] is False and features.on('semantic-search') is False


def test_the_feature_turns_on_when_the_url_is_set_and_the_change_is_recorded(store):
    # given
    live = Live(dataclasses.replace(settings(), embed_url='http://skopax-embed'), TunableStore(store.db))
    features = Features(live, lambda: 'community')

    # when
    item = features.set('semantic-search', True, 'alice@example.com')

    # then
    assert item['on'] is True and live.current().semantic_search_on is True


def test_the_settings_entry_says_why_the_switch_cannot_go_on(live):
    # when
    entry = live.entry(live.known()['core.semantic-search'])

    # then
    assert entry['value'] == 'false' and entry['off'] == msg('embed.needs_url')


@pytest.mark.parametrize('key, value', [('embed.timeout-seconds', '0'), ('embed.timeout-seconds', '61'), ('embed.batch', '33'), ('embed.batch-timeout-seconds', '4'), ('embed.max-vectors', '999'),
                                         ('embed.sources', 'runs, nonsense'), ('embed.docs-per-pass', '2001')])
def test_admin_refuses_a_value_outside_the_bounds_or_an_unknown_source(live, key, value):
    # then
    with pytest.raises(Invalid):
        live.set(key, value, 'alice@example.com')


def test_admin_saves_in_range_values_and_normalises_the_prefix(live):
    # when
    live.set('embed.batch', '16', 'alice@example.com')
    live.set('embed.sources', 'runs, cards', 'alice@example.com')
    live.set('embed.query-prefix', 'find:', 'alice@example.com')

    # then
    now = live.current()
    assert now.embed_batch == 16 and now.embed_sources == ('runs', 'cards') and now.query_prefix == 'find: '


LANGS = ('de', 'es', 'fr', 'tr')
NOTES = ('embed.off', 'embed.reason.disabled', 'embed.reason.no_url', 'embed.down', 'embed.refused', 'embed.mismatch', 'embed.auth', 'embed.bad_answer', 'embed.rebuilding', 'embed.needs_url', 'embed.sources_invalid')


def test_the_notes_are_english_messages_and_outbound():
    # then
    assert all(key in messages.MESSAGES and messages.outbound(key) for key in NOTES)
    assert '{reason}' in messages.MESSAGES['embed.off'] and '{done}' in messages.MESSAGES['embed.rebuilding'] and '{total}' in messages.MESSAGES['embed.rebuilding']


@pytest.mark.parametrize('lang', LANGS)
def test_every_note_has_a_translation_with_the_same_placeholders(lang):
    # given
    pack = json.loads((Path(locales.__file__).parent / 'locale' / lang / 'messages.json').read_text(encoding='utf-8'))

    # then
    for key in NOTES:
        assert pack[key].strip() and pack[key] != messages.MESSAGES[key]
        assert sorted(re.findall(r'\{\w+\}', pack[key])) == sorted(re.findall(r'\{\w+\}', messages.MESSAGES[key]))
