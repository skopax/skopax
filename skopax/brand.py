import functools
import html
import logging
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

from skopax import __version__
from skopax.actors import POD_BY, investigator_label

log = logging.getLogger('skopax.brand')

DEFAULT_NAME = 'Skopax'
MAX_NAME = 40
SITE = 'https://skopax.io'
MARK = '{product}'
SLUG_RUN = re.compile(r'[^a-z0-9]+')
BRAND_DIR = Path(__file__).parent / 'web' / 'brand'
SVG_NAME_ATTRS = re.compile(r'\s(?:role|aria-label|xmlns)="[^"]*"')
THEMES = ('light', 'dark')
TEXT_INK = {'light': '#0f1720', 'dark': '#e8eef5'}
FOOTER = '<footer class="attribution"><a href="{href}" id="attribution" rel="noopener noreferrer">{text}</a></footer>'
ELEMENT_ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]*')
VERSION_NUMBER = '(0|[1-9][0-9]*)'
PACKAGE_VERSION = re.compile(rf'{VERSION_NUMBER}\.{VERSION_NUMBER}\.{VERSION_NUMBER}(?:rc{VERSION_NUMBER})?')

_current = DEFAULT_NAME
_hidden: Callable[[], bool] | None = None
_failing = False


DROPPED = frozenset('<>&"')
DROPPED_CATEGORIES = frozenset(('Cc', 'Cs'))


def clean_name(raw) -> str:
    kept = ''.join(c for c in str(raw or '') if c not in DROPPED and unicodedata.category(c) not in DROPPED_CATEGORIES)
    text = ' '.join(kept.split())
    return text[:MAX_NAME].strip() or DEFAULT_NAME


def display_name(settings) -> str:
    return clean_name(getattr(settings, 'display_name', ''))


def mcp_slug(name: str) -> str:
    return SLUG_RUN.sub('-', str(name or '').lower()).strip('-') or DEFAULT_NAME.lower()


def display_version(version: str = __version__) -> str:
    match = PACKAGE_VERSION.fullmatch(version)
    if match is None:
        return version
    major, minor, patch, rc = match.groups()
    return f'{major}.{minor}.{patch}' + (f'-rc.{rc}' if rc is not None else '')


def attribution_of(name: str) -> tuple[str, str]:
    if name == DEFAULT_NAME:
        return f'{DEFAULT_NAME} {display_version()} \u00b7 skopax.io', SITE
    return f'{name} \u00b7 powered by {DEFAULT_NAME} {display_version()}', SITE


def attribution(settings) -> tuple[str, str]:
    return attribution_of(display_name(settings))


def use(name: str) -> None:
    global _current
    _current = clean_name(name)


def product() -> str:
    return _current


def fill(text: str) -> str:
    return text.replace(MARK, _current)


def use_hidden(fn: Callable[[], bool] | None) -> None:
    global _hidden, _failing
    _hidden = fn
    _failing = False


def shown() -> bool:
    global _failing
    fn = _hidden
    if fn is None:
        return True
    try:
        hidden = fn() is True
    except Exception as e:
        if not _failing:
            _failing = True
            log.warning('the attribution switch could not be read, so the line is shown: %s', type(e).__name__)
        return True
    _failing = False
    return not hidden


def attribution_line() -> dict | None:
    if not shown():
        return None
    text, href = attribution_of(_current)
    return {'text': text, 'href': href}


def footer_html(element_id: str | None = 'attribution') -> str:
    if element_id is not None and not (isinstance(element_id, str) and ELEMENT_ID.fullmatch(element_id)):
        raise ValueError('the attribution id must be a letter followed by letters, digits, hyphens or underscores')
    line = attribution_line()
    if line is None:
        return ''
    markup = FOOTER.format(href=html.escape(line['href'], quote=True), text=html.escape(line['text'], quote=True))
    return markup.replace(' id="attribution"', f' id="{element_id}"' if element_id is not None else '', 1)


def page_info() -> dict:
    return {'name': _current, 'slug': mcp_slug(_current), 'version': display_version(), 'attribution': attribution_line(), 'investigator': investigator_label(), 'podBy': POD_BY}


def chat_brand() -> dict:
    return {'attribution': attribution_line()}


@functools.cache
def asset(name: str) -> str:
    return (BRAND_DIR / name).read_text(encoding='utf-8').strip()


def wordmark_inline(theme: str) -> str:
    return SVG_NAME_ATTRS.sub('', asset(f'wordmark-on-{theme}.svg')).replace('<svg ', '<svg aria-hidden="true" focusable="false" ', 1)


def wordmark_html(theme: str = 'auto') -> str:
    label = html.escape(_current, quote=True)
    if _current != DEFAULT_NAME:
        return f'<span class="wordmark wordmark-text" role="img" aria-label="{label}">{label}</span>'
    themes = THEMES if theme == 'auto' else (theme,)
    inner = ''.join(f'<span class="wm-{t}">{wordmark_inline(t)}</span>' for t in themes)
    return f'<span class="wordmark" role="img" aria-label="{label}">{inner}</span>'


def chat_logo_svg(theme: str) -> str:
    theme = theme if theme in THEMES else 'light'
    if _current == DEFAULT_NAME:
        return asset(f'wordmark-on-{theme}.svg')
    label = html.escape(_current)
    width = max(40, round(len(_current) * 12.5) + 8)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} 32" width="{width}" height="32" role="img" aria-label="{label}">'
            f'<text x="0" y="23" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" font-size="20" font-weight="700" fill="{TEXT_INK[theme]}">{label}</text></svg>')


def mark_svg() -> str:
    return asset('mark.svg')


def mark_tile_html() -> str:
    if _current == DEFAULT_NAME:
        return ''
    inner = SVG_NAME_ATTRS.sub('', mark_svg()).replace('<svg ', '<svg aria-hidden="true" focusable="false" ', 1)
    return f'<a class="mark home-mark" href="/" tabindex="-1" aria-hidden="true">{inner}</a>'
