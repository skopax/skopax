import dataclasses
import json
import logging
import os
import pathlib
from types import MappingProxyType

import pytest

from skopax import chatui
from skopax.investigator import prompt
from skopax.chatui.text import chainlit_of, lang_of, reset, set_texts
from skopax.contract import Locale
from skopax.registry import Registry
from tests.conftest import store
from tests.plugins.base import minimal
from tests.test_plugin_hooks import served

TREE = {'common': {'actions': {'cancel': 'Annuler', 'confirm': 'Confirmer'}}, 'chat': {'input': {'placeholder': 'Ecrivez votre message'}}}


def pack(*locales: Locale):
    return minimal('fr-pack', 'Fr pack', locales=locales)


def translations(root: str) -> pathlib.Path:
    return pathlib.Path(root) / '.chainlit' / 'translations'


def bundled(name: str) -> dict:
    import chainlit
    return json.loads((pathlib.Path(chainlit.__file__).parent / 'translations' / f'{name}.json').read_text(encoding='utf-8'))


def over_english(tree: dict) -> dict:
    def merged(base: dict, top: dict) -> dict:
        out = dict(base)
        for key, value in top.items():
            out[key] = merged(out.get(key) or {}, value) if isinstance(value, dict) else value
        return out

    return merged(bundled('en-US'), tree)


def read(root, name: str) -> dict:
    return json.loads((translations(str(root)) / f'{name}.json').read_text(encoding='utf-8'))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(chatui.ROOT_ENV, raising=False)
    chatui.set_translations(())
    reset()
    yield
    chatui.set_translations(())
    reset()


def test_a_locale_chainlit_tree_is_written_as_the_chainlit_translation_for_its_tag_at_boot(store):
    # given
    plugin = pack(Locale('fr', chainlit=TREE))

    # when
    with served(store, plugin, add=False):
        root = chatui.prepare('https://skopax.example.com')
    written = json.loads((translations(root) / 'fr.json').read_text(encoding='utf-8'))

    # then
    assert Registry.of(plugin).get('fr-pack').plugin.locales[0].chainlit == TREE
    assert written == over_english(TREE)
    assert chainlit_of('fr') == 'fr' and lang_of(None, 'fr-FR,fr;q=0.9') == 'fr'


def test_a_locale_chainlit_tree_also_answers_the_region_form_the_browser_asks_for(tmp_path, monkeypatch, caplog):
    # given
    monkeypatch.setenv(chatui.ROOT_ENV, str(tmp_path))
    (tmp_path / '.chainlit').mkdir()
    set_texts((), [('fr', TREE), ('pt-BR', TREE)])

    # when
    chatui.set_translations([('fr', TREE), ('pt-BR', TREE)])
    names = sorted(p.name for p in translations(str(tmp_path)).glob('*.json'))
    with caplog.at_level(logging.WARNING):
        from chainlit.config import config
        monkeypatch.setattr('chainlit.config.config_translation_dir', str(translations(str(tmp_path))))
        loaded = config.load_translation('fr-FR')

    # then
    assert names == ['fr-FR.json', 'fr.json', 'pt-BR.json']
    assert loaded == over_english(TREE)
    assert not [r for r in caplog.records if 'not found' in r.getMessage()], 'chainlit finds the exact file and logs no missing translation'


@pytest.mark.parametrize('tree', [
    {'common': {'actions': {'cancel': 'See https://example.com/x'}}},
    {'common': {'actions': {'cancel': 'Write to alice@example.com'}}},
    {'common': {'actions': {'cancel': 'Annuler *bold*'}}},
    {'common': {'actions': {'cancel': 7}}},
    {'common': ['Annuler']},
    {1: 'Annuler'},
    'Annuler',
])
def test_a_chainlit_tree_with_a_link_or_a_non_string_leaf_breaks_only_that_locale_plugin(tree, caplog):
    # given
    good = Locale('de', chainlit={'common': {'actions': {'cancel': 'Abbrechen'}}})
    plugin = pack(Locale('fr', chainlit=tree), good)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.registry'):
        found = Registry.of(plugin).get('fr-pack')
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.registry']

    # then
    assert found.state == 'ok'
    assert found.plugin.locales == (Locale('fr'), good), 'only the bad locale loses its tree'
    assert len(warned) == 1 and warned[0].startswith('Locale pack fr of fr-pack: chainlit is left out') and 'chainlit' in warned[0], warned


def test_a_mapping_proxy_tree_is_written_as_plain_json_and_the_language_step_still_runs(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setenv(chatui.ROOT_ENV, str(tmp_path))
    (tmp_path / '.chainlit').mkdir()
    frozen = MappingProxyType({'common': MappingProxyType({'actions': MappingProxyType({'cancel': 'Annuler'})})})
    plugin = pack(Locale('fr', chainlit=frozen, writing='Write short French sentences.'))

    # when
    with served(store, plugin, add=False):
        notes = dict(prompt.writing())
    kept = Registry.of(plugin).get('fr-pack').plugin.locales[0].chainlit

    # then
    assert type(kept) is dict and type(kept['common']) is dict
    assert read(tmp_path, 'fr') == over_english({'common': {'actions': {'cancel': 'Annuler'}}})
    assert notes == {'fr': 'Write short French sentences.'}


def test_an_unserialisable_tree_drops_only_its_own_locale_files(tmp_path, monkeypatch, caplog):
    # given
    monkeypatch.setenv(chatui.ROOT_ENV, str(tmp_path))
    (tmp_path / '.chainlit').mkdir()

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.chatui'):
        chatui.set_translations([('fr', {'common': {'actions': {'cancel': object()}}}), ('de', TREE)])
        chatui.set_translations([('fr', {'common': {'actions': {'cancel': object()}}}), ('de', TREE)])
    names = sorted(p.name for p in translations(str(tmp_path)).glob('*.json'))
    warned = [r.getMessage() for r in caplog.records if 'fr' in r.getMessage()]

    # then
    assert names == ['de-DE.json', 'de.json']
    assert len(warned) == 1 and 'TypeError' in warned[0], warned


def test_a_partial_tree_falls_back_to_english_per_key():
    # given
    tree = {'common': {'actions': {'cancel': 'Annuler'}}}

    # when
    written = chatui.translation_text(tree)

    # then
    assert json.loads(written)['common']['actions'] == {**bundled('en-US')['common']['actions'], 'cancel': 'Annuler'}
    assert json.loads(written)['chat'] == bundled('en-US')['chat']


def test_a_translation_file_is_replaced_whole_or_not_at_all(tmp_path, monkeypatch):
    # given
    monkeypatch.setenv(chatui.ROOT_ENV, str(tmp_path))
    (tmp_path / '.chainlit').mkdir()
    chatui.set_translations([('fr', TREE)])
    before = read(tmp_path, 'fr')

    def refused(src, dst):
        raise OSError('disk full')

    monkeypatch.setattr(os, 'replace', refused)

    # when
    chatui.set_translations([('fr', {'common': {'actions': {'cancel': 'Fermer'}}})])
    left = sorted(p.name for p in translations(str(tmp_path)).iterdir() if not p.name.startswith('.skopax'))

    # then
    assert read(tmp_path, 'fr') == before
    assert left == ['fr-FR.json', 'fr.json'], 'no half-written or temporary file is left behind'


def test_files_of_a_removed_locale_are_cleaned_up_and_bundled_ones_are_restored(tmp_path, monkeypatch):
    # given
    monkeypatch.setenv(chatui.ROOT_ENV, str(tmp_path))
    target = translations(str(tmp_path))
    target.mkdir(parents=True)
    (target / 'fr-FR.json').write_text(json.dumps(bundled('fr-FR')), encoding='utf-8')
    (target / 'it.json').write_text('{"own": "file"}', encoding='utf-8')
    chatui.set_translations([('fr', TREE), ('pt-BR', TREE)])
    written = read(tmp_path, 'fr-FR')

    # when
    chatui.set_translations([('pt-BR', TREE)])
    names = sorted(p.name for p in target.glob('*.json'))
    chatui.set_translations(())
    after = sorted(p.name for p in target.glob('*.json'))

    # then
    assert written == over_english(TREE)
    assert names == ['fr-FR.json', 'it.json', 'pt-BR.json']
    assert read(tmp_path, 'fr-FR') == bundled('fr-FR'), 'a bundled Chainlit file is restored, never deleted'
    assert after == ['fr-FR.json', 'it.json']
    assert read(tmp_path, 'it') == {'own': 'file'}


def test_the_region_form_comes_from_the_locale_alias_table():
    # then
    assert chatui.translation_names('ja') == ['ja', 'ja-JP']
    assert chatui.translation_names('tr') == ['tr', 'tr-' + 'tr'.upper()]
    assert chatui.translation_names('fr') == ['fr', 'fr-FR']
    assert chatui.translation_names('pt-BR') == ['pt-BR']
    assert chatui.translation_names('qq') == ['qq']
