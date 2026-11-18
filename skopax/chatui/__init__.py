import contextvars
import functools
import hashlib
import importlib.util
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
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
generated_by = "2.11.0"
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


def prepare(origin: str) -> str:
    os.environ.setdefault('TRACELOOP_TELEMETRY', 'false')
    if os.environ.get(ROOT_ENV):
        config = Path(os.environ[ROOT_ENV]) / '.chainlit' / 'config.toml'
        if config.exists():
            config.write_text(config_text(origin), encoding='utf-8')
        return os.environ[ROOT_ENV]
    root = Path(tempfile.mkdtemp(prefix='skopax-chat-'))
    (root / '.chainlit').mkdir()
    (root / '.chainlit' / 'config.toml').write_text(config_text(origin), encoding='utf-8')
    shutil.copytree(HERE / 'public', root / 'public')
    for name in ('chainlit.md',) + tuple(f'chainlit_{lang}.md' for lang in MD_LANGS):
        (root / name).write_text('', encoding='utf-8')
    os.environ[ROOT_ENV] = str(root)
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
