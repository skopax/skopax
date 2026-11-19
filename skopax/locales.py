import functools
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from skopax.contract import Locale
from skopax.messages import LANG_TAG
from skopax.registry import kept_packs
from skopax.runtime import on_page, page_pack

ROOT = Path(__file__).with_name('locale')
OWNER = 'core'
LISTS = ('rules', 'tails')
CORE_PACK = '(function (all, lang, pack) { var held = all[lang] = all[lang] || {}; Object.keys(pack).forEach(function (k) { var v = pack[k]; if (k === "@rules" || k === "@tails") { v = v.slice(); v.core = v.length; } held[k] = v; }); })(window.SKOPAX_I18N = window.SKOPAX_I18N || {}, %s, %s);'


def langs() -> tuple[str, ...]:
    if not ROOT.is_dir():
        return ()
    return tuple(sorted(path.name for path in ROOT.iterdir() if path.is_dir() and LANG_TAG.fullmatch(path.name)))


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def _pairs(value: object) -> object:
    return tuple(tuple(pair) if isinstance(pair, list) else pair for pair in value) if isinstance(value, list) else value


def read(lang: str) -> Locale:
    folder = ROOT / lang
    page = _json(folder / 'page.json')
    parts = {what: page.get(what, {}) for what in ('page', 'months', 'days', 'headers')} | {what: _pairs(page.get(what, ())) for what in LISTS}
    note = folder / 'writing.txt'
    writing = note.read_text(encoding='utf-8').strip() if note.is_file() else ''
    return Locale(lang, messages=_json(folder / 'messages.json'), chat=_json(folder / 'chat.json'), chainlit=_json(folder / 'chainlit.json'), writing=writing, **parts)


@functools.cache
def packs() -> tuple[Locale, ...]:
    kept, _, _ = kept_packs(OWNER, [read(lang) for lang in langs()])
    return tuple(kept)


def page_of(locale: Locale) -> dict:
    return page_pack([locale])


@functools.cache
def page_files() -> dict[str, bytes]:
    return {f'i18n-{locale.lang}.js': (CORE_PACK % (json.dumps(locale.lang), json.dumps(page_of(locale)))).encode('utf-8') for locale in packs() if on_page(locale)}


def _over(base: Mapping, tree: Mapping) -> dict:
    out = dict(base)
    for key, value in tree.items():
        out[key] = _over(out[key], value) if isinstance(value, Mapping) and isinstance(out.get(key), Mapping) else value
    return out


def _under(base: Mapping[str, Mapping], lang: str) -> Mapping:
    return base.get(lang.lower()) or base.get(lang.partition('-')[0].lower()) or {}


def chainlit_trees(core: Sequence[Locale], added: Sequence[Locale]) -> list[tuple[str, Mapping]]:
    base = {locale.lang.lower(): locale.chainlit for locale in core if locale.chainlit}
    return [(locale.lang, locale.chainlit) for locale in core] + [(locale.lang, _over(_under(base, locale.lang), locale.chainlit) if locale.chainlit else locale.chainlit) for locale in added]
