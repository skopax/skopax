import functools
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from skopax.contract import ID
from skopax.mcptools.adapters import ADAPTERS
from skopax.mcptools.client import Refused, guard_url

FILE = Path(__file__).parent / 'presets.json'
AUTH_MODES = ('none', 'header', 'oauth')
KNOWLEDGE_STATES = ('ready', 'soon', 'none')
SOON_KEYS = frozenset({'id', 'name', 'soon'})
TEXTS = ('id', 'name', 'url', 'auth', 'headerName', 'headerHint', 'scopes', 'docs', 'knowledge', 'adapter')
LISTS = ('linkHosts', 'knowledgeTools', 'denied')
FLAGS = ('clientRequired', 'preview', 'soon')
PARAMS = 'authParams'
KEYS = frozenset(TEXTS + LISTS + FLAGS + (PARAMS,))
FLOW_PARAMS = frozenset({'response_type', 'client_id', 'redirect_uri', 'state', 'code_challenge', 'code_challenge_method', 'resource', 'scope'})
DEFAULT_PORTS = {'https': 443, 'http': 80}


class PresetError(ValueError):
    pass


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    url: str = ''
    auth: str = ''
    header_name: str = ''
    header_hint: str = ''
    scopes: str = ''
    docs: str = ''
    knowledge: str = 'none'
    adapter: str = ''
    link_hosts: tuple[str, ...] = ()
    knowledge_tools: tuple[str, ...] = ()
    client_required: bool = False
    preview: bool = False
    soon: bool = False
    denied: tuple[str, ...] = ()
    auth_params: tuple[tuple[str, str], ...] = ()

    def item(self) -> dict:
        return {'id': self.id, 'name': self.name, 'url': self.url, 'auth': self.auth, 'headerName': self.header_name, 'headerHint': self.header_hint, 'scopes': self.scopes,
                'docs': self.docs, 'knowledge': self.knowledge, 'adapter': self.adapter, 'linkHosts': list(self.link_hosts), 'knowledgeTools': list(self.knowledge_tools),
                'clientRequired': self.client_required, 'preview': self.preview, 'denied': list(self.denied)}


def https(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == 'https' and bool(parts.hostname)


def _soon(item: dict) -> Preset:
    if set(item) != SOON_KEYS or item['soon'] is not True:
        raise PresetError(f"coming soon entry {item.get('id')} holds only id, name and soon")
    return Preset(item['id'], item['name'], soon=True)


def _ready(item: dict) -> Preset:
    where = item['id']
    if set(item) != KEYS or item['soon'] is not False:
        raise PresetError(f'preset {where} must hold exactly the keys {sorted(KEYS)}')
    if not all(isinstance(item[k], str) for k in TEXTS) or not all(isinstance(item[k], bool) for k in FLAGS):
        raise PresetError(f'preset {where} has a value of the wrong type')
    if not all(isinstance(item[k], list) and all(isinstance(v, str) and v for v in item[k]) for k in LISTS):
        raise PresetError(f'preset {where} has a list that is not a list of names')
    params = item[PARAMS]
    if not isinstance(params, dict) or not all(isinstance(k, str) and k and isinstance(v, str) for k, v in params.items()) or set(params) & FLOW_PARAMS:
        raise PresetError(f'preset {where} has auth parameters that are not plain text or that replace a sign-in parameter')
    try:
        guard_url(item['url'], False)
    except Refused:
        raise PresetError(f'preset {where} has a URL that is not a callable https address') from None
    if not https(item['url']) or not https(item['docs']):
        raise PresetError(f'preset {where} needs https URL and docs addresses')
    if item['auth'] not in AUTH_MODES or item['knowledge'] not in KNOWLEDGE_STATES:
        raise PresetError(f'preset {where} has an unknown auth mode or knowledge state')
    adapter = item['adapter']
    if adapter and adapter not in ADAPTERS:
        raise PresetError(f'preset {where} names unknown adapter {adapter}')
    if item['knowledge'] == 'ready' and not adapter:
        raise PresetError(f'preset {where} is a ready knowledge source without an adapter')
    hosts, tools = tuple(item['linkHosts']), tuple(item['knowledgeTools'])
    if adapter and (hosts != ADAPTERS[adapter].link_hosts or not tools or not set(tools) <= ADAPTERS[adapter].tools):
        raise PresetError(f'preset {where} must mirror the link hosts and tools of adapter {adapter}')
    if not adapter and (hosts or tools):
        raise PresetError(f'preset {where} names link hosts or knowledge tools without an adapter')
    return Preset(item['id'], item['name'], item['url'], item['auth'], item['headerName'], item['headerHint'], item['scopes'], item['docs'], item['knowledge'], adapter,
                  hosts, tools, item['clientRequired'], item['preview'], False, tuple(item['denied']), tuple(params.items()))


def parse(data: object) -> tuple[Preset, ...]:
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise PresetError('the presets file is a list of objects')
    found = []
    for item in data:
        if not isinstance(item.get('id'), str) or not ID.fullmatch(item['id']) or not isinstance(item.get('name'), str) or not item['name'].strip():
            raise PresetError('every preset needs a valid id and a name')
        found.append(_soon(item) if item.get('soon') is True else _ready(item))
    ids = [p.id for p in found]
    if len(set(ids)) != len(ids):
        raise PresetError('preset ids repeat')
    return tuple(found)


@functools.cache
def load() -> tuple[Preset, ...]:
    return parse(json.loads(FILE.read_text(encoding='utf-8')))


def get(preset_id: str | None) -> Preset | None:
    return next((p for p in load() if p.id == preset_id and not p.soon), None)


def origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or '').lower(), port or DEFAULT_PORTS.get(scheme)


def same_origin(url: str, other: str) -> bool:
    mine = origin(url)
    return mine is not None and mine == origin(other)


load()
