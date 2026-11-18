import contextvars
import functools
import hashlib
import importlib.util
import json
import locale
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import Callable
from urllib.parse import parse_qsl, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Mount

from skopax import brand
from skopax.messages import msg

log = logging.getLogger('skopax.chatui')

PATH = '/chat'
CARD_COOKIE = 'skopax_chat_card'
LANG_COOKIE = 'skopax_lang'
SETTINGS_PATH = PATH + '/project/settings'
LOCALIZED = (PATH + '/project/translations', SETTINGS_PATH)
TOKEN_COOKIE = 'access_token'
TOKEN_CHUNKS = 6
OPEN_PATHS = ('/assets/', '/public/', '/favicon', '/logo', '/avatars/')
ROOT_ENV = 'CHAINLIT_APP_ROOT'
HERE = Path(__file__).parent
SESSION_S = 8 * 3600
BRAND_CACHE = 'no-cache'
BRAND_JSON = '/public/skopax-brand.json'
CHAT_ID = re.compile(r'[A-Za-z0-9-]{8,64}')
SHARE_PATH = PATH + '/project/share/'
MD_LANGS = ('en', 'en-US', 'en-GB')
README_LANG = 'en-US'
SHARED_VIEW = contextvars.ContextVar('skopax_chat_shared_view', default=False)
REQUEST_LANG = contextvars.ContextVar('skopax_chat_lang', default='en')
CONFIG = """[project]
user_session_timeout = {session}
session_timeout = 3600
allow_origins = ["{origin}"]
transports = ["websocket"]
cache = false

[features]
unsafe_allow_html = false
latex = false
edit_message = false
allow_thread_sharing = true
favorites = false

[features.spontaneous_file_upload]
enabled = false

[features.audio]
enabled = false

[features.mcp]
enabled = false

[UI]
name = {name}
description = {description}
default_theme = "dark"
default_sidebar_state = "open"
confirm_new_chat = false
cot = "tool_call"
custom_css = "/public/skopax-chat.css"
custom_js = "/public/skopax-chat.js"

[[UI.header_links]]
name = {name}
display_name = {name}
icon_url = "/chat/public/skopax-back.svg"
url = "/"
target = "_top"

[meta]
generated_by = "2.12.0"
"""


class Refused(Exception):
    def __init__(self, text: str, code: str | None = None, **fields):
        super().__init__(text)
        self.code = code
        self.fields = fields


def localized(query: bytes, language: str) -> bytes:
    return urlencode([(k, v) for k, v in parse_qsl(query.decode('latin-1')) if k != 'language'] + [('language', language)]).encode('latin-1')


def needs_session(path: str) -> bool:
    if path != PATH and not path.startswith(PATH + '/'):
        return False
    rest = path[len(PATH):]
    return rest not in ('', '/') and not rest.startswith(OPEN_PATHS)


def brand_answer(path: str, query: bytes) -> Response | None:
    if path == BRAND_JSON:
        return Response(json.dumps(brand.chat_brand()), media_type='application/json', headers={'Cache-Control': BRAND_CACHE})
    if path == '/logo':
        theme = dict(parse_qsl(query.decode('latin-1'))).get('theme')
        return Response(brand.chat_logo_svg(theme), media_type='image/svg+xml', headers={'Cache-Control': BRAND_CACHE})
    if path.startswith('/avatars/'):
        if not path[len('/avatars/'):]:
            return None
        return Response(brand.mark_svg(), media_type='image/svg+xml', headers={'Cache-Control': BRAND_CACHE})
    return None


def route_path(scope) -> str:
    path, root = scope['path'], scope.get('root_path') or ''
    if root and path.startswith(root) and path[len(root):len(root) + 1] in ('', '/'):
        return path[len(root):] or '/'
    return path


CSP = "style-src 'self' 'unsafe-inline'; font-src 'self' data:"


def with_csp(send):
    async def wrapped(message):
        if message['type'] == 'http.response.start':
            headers = list(message.get('headers') or [])
            if any(k.lower() == b'content-type' and v.lower().startswith(b'text/html') for k, v in headers):
                message = {**message, 'headers': headers + [(b'content-security-policy', CSP.encode())]}
        await send(message)
    return wrapped


@functools.cache
def chainlit_tags() -> tuple[str, ...]:
    spec = importlib.util.find_spec('chainlit')
    if spec is None or not spec.origin:
        return ()
    try:
        return tuple(sorted(path.stem for path in (Path(spec.origin).parent / 'translations').glob('*.json')))
    except OSError:
        return ()


def chainlit_language(lang: str) -> str | None:
    wanted, tags = lang.lower(), chainlit_tags()
    exact = next((tag for tag in tags if tag.lower() == wanted), None)
    return exact or next((tag for tag in tags if tag.partition('-')[0].lower() == wanted.partition('-')[0]), None)


def scope_lang(scope) -> str:
    from skopax.chatui.text import lang_of
    headers = {key.lower(): value.decode('latin-1') for key, value in scope.get('headers') or ()}
    return lang_of(headers.get(b'cookie'), headers.get(b'accept-language'))


class BrandFront:
    def __init__(self, app):
        self.app = app

    @property
    def routes(self):
        return getattr(self.app, 'routes', ())

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            spoken = REQUEST_LANG.set(scope_lang(scope))
            try:
                await self.app(scope, receive, send)
            finally:
                REQUEST_LANG.reset(spoken)
            return
        if scope['type'] == 'http' and scope['method'] in ('GET', 'HEAD'):
            path, query = route_path(scope), scope.get('query_string') or b''
            answer = await run_in_threadpool(brand_answer, path, query) if path == BRAND_JSON else brand_answer(path, query)
            if answer is not None:
                await answer(scope, receive, send)
                return
            send = with_csp(send)
        await self.app(scope, receive, send)


def forget(resp) -> None:
    for name in [TOKEN_COOKIE] + [f'{TOKEN_COOKIE}_{i}' for i in range(TOKEN_CHUNKS)]:
        resp.delete_cookie(name, path='/')


@dataclass(frozen=True)
class Bridge:
    user: Callable[[str | None], dict | None]
    post: Callable[..., dict]
    chat: Callable[[str, str], dict | None]
    chats: Callable[[str], list[dict]]
    owner: Callable[[str], str | None]
    shared: Callable[[str], bool]
    share: Callable[[str, str, bool], bool]
    hide: Callable[[str, str], None]
    apply: Callable[[str, bool, dict], tuple[str, str]]
    card: Callable[[str], dict | None]


BRIDGE: Bridge | None = None


def secret_of(seed: bytes) -> str:
    return hashlib.sha256(b'skopax-chat:' + seed).hexdigest()


def config_text(origin: str, session: int = SESSION_S, name: str | None = None) -> str:
    name = brand.product() if name is None else name
    return CONFIG.format(session=session, origin=origin, name=json.dumps(name, ensure_ascii=False), description=json.dumps(msg('chat.description', product=name), ensure_ascii=False))


TRANSLATIONS: dict[str, tuple[str, Mapping]] = {}
TRANSLATIONS_LOCK = threading.Lock()
WRITE_LOCK = threading.Lock()
WRITTEN = '.skopax-written'
DEFAULT_TRANSLATION = 'en-US'
FAILED_LOCALES: set[str] = set()


def translation_names(tag: str) -> list[str]:
    language, _, region = tag.partition('-')
    if region:
        return [tag]
    full = re.split(r'[.@]', locale.normalize(language), maxsplit=1)[0].replace('_', '-')
    head, _, place = full.partition('-')
    return [tag, f'{language}-{place}'] if head.lower() == language.lower() and place else [tag]


def translation_files(trees: Iterable[tuple[str, Mapping]]) -> dict[str, tuple[str, Mapping]]:
    chosen = {tag: tree for tag, tree in trees if isinstance(tag, str) and isinstance(tree, Mapping) and tree and tag.partition('-')[0].lower() != 'en'}
    files: dict[str, tuple[str, Mapping]] = {}
    for tag, tree in chosen.items():
        files.update({name: (tag, tree) for name in translation_names(tag)[1:]})
    for tag, tree in chosen.items():
        files[tag] = (tag, tree)
    return files


def set_translations(trees: Iterable[tuple[str, Mapping]]) -> None:
    files = translation_files(trees)
    with TRANSLATIONS_LOCK:
        TRANSLATIONS.clear()
        TRANSLATIONS.update(files)
    if os.environ.get(ROOT_ENV):
        write_translations(os.environ[ROOT_ENV])


def bundled_translation(name: str) -> dict | None:
    spec = importlib.util.find_spec('chainlit')
    if spec is None or not spec.origin:
        return None
    try:
        found = json.loads((Path(spec.origin).parent / 'translations' / f'{name}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


@functools.cache
def english() -> dict:
    return bundled_translation(DEFAULT_TRANSLATION) or {}


def _over(base: object, tree: Mapping) -> dict:
    out = dict(base) if isinstance(base, Mapping) else {}
    for key, value in tree.items():
        out[key] = _over(out.get(key), value) if isinstance(value, Mapping) else value
    return out


def translation_text(tree: Mapping) -> str:
    return json.dumps(_over(english(), tree), ensure_ascii=False, indent=4)


def _put(path: Path, text: str) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _written(target: Path) -> list[str]:
    try:
        names = json.loads((target / WRITTEN).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return [n for n in names if isinstance(n, str) and n and '/' not in n and not n.startswith('.')] if isinstance(names, list) else []


def _drop(target: Path, name: str) -> None:
    if name in chainlit_tags():
        original = bundled_translation(name)
        if original is not None:
            _put(target / f'{name}.json', json.dumps(original, ensure_ascii=False, indent=4))
            return
    (target / f'{name}.json').unlink(missing_ok=True)


def _failed_locale(tag: str, e: Exception) -> None:
    if tag not in FAILED_LOCALES:
        FAILED_LOCALES.add(tag)
        log.warning('%s', msg('chat.translation_failed', lang=tag, error=type(e).__name__))


def write_translations(root: str) -> None:
    with TRANSLATIONS_LOCK:
        held = dict(TRANSLATIONS)
    target = Path(root) / '.chainlit' / 'translations'
    with WRITE_LOCK:
        before = _written(target)
        if not held and not before:
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning('%s', msg('chat.translations_failed', error=type(e).__name__))
            return
        kept = set()
        bad = set()
        for name, (tag, tree) in held.items():
            try:
                _put(target / f'{name}.json', translation_text(tree))
            except (OSError, TypeError, ValueError) as e:
                bad.add(tag)
                _failed_locale(tag, e)
                if name in before:
                    kept.add(name)
                continue
            kept.add(name)
        FAILED_LOCALES.difference_update({tag for tag, _ in held.values()} - bad)
        for name in [n for n in before if n not in held]:
            try:
                _drop(target, name)
            except OSError as e:
                kept.add(name)
                log.warning('%s', msg('chat.translations_failed', error=type(e).__name__))
        try:
            if kept:
                _put(target / WRITTEN, json.dumps(sorted(kept)))
            else:
                (target / WRITTEN).unlink(missing_ok=True)
        except OSError as e:
            log.warning('%s', msg('chat.translations_failed', error=type(e).__name__))


def prepare(origin: str) -> str:
    os.environ.setdefault('TRACELOOP_TELEMETRY', 'false')
    if os.environ.get(ROOT_ENV):
        config = Path(os.environ[ROOT_ENV]) / '.chainlit' / 'config.toml'
        if config.exists():
            config.write_text(config_text(origin), encoding='utf-8')
        write_translations(os.environ[ROOT_ENV])
        return os.environ[ROOT_ENV]
    root = Path(tempfile.mkdtemp(prefix='skopax-chat-'))
    (root / '.chainlit').mkdir()
    (root / '.chainlit' / 'config.toml').write_text(config_text(origin), encoding='utf-8')
    shutil.copytree(HERE / 'public', root / 'public')
    for name in ('chainlit.md',) + tuple(f'chainlit_{lang}.md' for lang in MD_LANGS):
        (root / name).write_text('', encoding='utf-8')
    os.environ[ROOT_ENV] = str(root)
    write_translations(str(root))
    return str(root)


def use_secret(seed: bytes) -> None:
    os.environ['CHAINLIT_AUTH_SECRET'] = secret_of(seed)


def mount(app, bridge: Bridge, seed: bytes | None, origin: str, jira_site: str | Callable[[], str] = '', projects=()) -> None:
    global BRIDGE
    BRIDGE = bridge
    from skopax.chatui.text import configure
    configure(jira_site, projects)
    prepare(origin)
    if seed is not None:
        use_secret(seed)
    else:
        os.environ['CHAINLIT_AUTH_SECRET'] = secrets.token_hex(32)
    from chainlit.server import app as chainlit_app
    from chainlit.utils import mount_chainlit
    from fastapi.exceptions import RequestValidationError
    from skopax.app import refused_request
    chainlit_app.add_exception_handler(RequestValidationError, refused_request)
    mount_chainlit(app=app, target=str(HERE / 'handlers.py'), path=PATH)
    for route in app.routes:
        if isinstance(route, Mount) and route.path == PATH:
            route.app = BrandFront(route.app)

    @app.middleware('http')
    async def chat_language(request, call_next):
        from skopax.chatui.text import chainlit_of, lang_of
        lang = lang_of(request.headers.get('cookie'), request.headers.get('accept-language'))
        picked = README_LANG if request.url.path == SETTINGS_PATH else chainlit_of(lang) or README_LANG
        if request.url.path in LOCALIZED:
            request.scope['query_string'] = localized(request.scope.get('query_string') or b'', picked)
        spoken = REQUEST_LANG.set(lang)
        shared = SHARED_VIEW.set(lang) if request.url.path.startswith(SHARE_PATH) else None
        try:
            return await call_next(request)
        finally:
            if shared is not None:
                SHARED_VIEW.reset(shared)
            REQUEST_LANG.reset(spoken)

    log.info('chat page mounted at %s', PATH)
