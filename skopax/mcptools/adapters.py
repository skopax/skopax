import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from mcp import types

from skopax.errkind import Authored
from skopax.messages import msg
from skopax.mcptools import FOLDERS_MAX, LABEL_MAX

log = logging.getLogger('skopax.mcptools')

Call = Callable[[str, dict], types.CallToolResult]

DRIVE_TOOLS = frozenset({'search_files', 'get_file_metadata', 'read_file_content'})
DRIVE_HOSTS = ('docs.google.com', 'drive.google.com')
GITHUB_TOOLS = frozenset({'search_code', 'get_file_contents'})
GITHUB_HOSTS = ('github.com',)
FOLDER_TYPE = 'application/vnd.google-apps.folder'
UNREAD_TYPES = (FOLDER_TYPE, 'application/vnd.google-apps.shortcut')
FILE_KEYS = ('id', 'title', 'mimeType', 'modifiedTime', 'viewUrl', 'contentSnippet')
CODE_KEYS = ('path', 'html_url')
SCOPE_FIELDS = ('repository', 'path')
PAGE_MAX = 50
Q_MAX = 100
Q_MIN = 2
QUERY_MAX = 256
FRAGMENTS_MAX = 500
QUALIFIER = re.compile(r'(repo|org|user|owner):', re.IGNORECASE)
FULL_NAME = re.compile(r'[A-Za-z0-9._-]+/[A-Za-z0-9._-]+')
FILE_ID = re.compile(r'[A-Za-z0-9_-]{10,200}')
DRIVE_STRAY = '"\\'
GITHUB_STRAY = '\\'
PLACE_KEYS = ('path', 'ref', 'sha')
PLACE_REFUSED = ('\\', '%')
UP = '..'


@dataclass(frozen=True)
class Scope:
    id: str
    label: str
    url: str


class ScopeRefused(Authored, Exception):
    pass


class ReadRefused(Authored, Exception):
    def __init__(self, text: str, said: str = ''):
        super().__init__(text)
        self.said = said


def closed(chars: Iterator[str], quote: str) -> bool:
    for ch in chars:
        if ch == '\\':
            next(chars, None)
        elif ch == quote:
            return True
    return False


def bare(query: str, quote: str) -> str | None:
    kept, chars = [], iter(query)
    for ch in chars:
        if ch != quote:
            kept.append(ch)
        elif not closed(chars, quote):
            return None
    return ''.join(kept)


def balanced(query: str, quote: str) -> bool:
    outside, depth = bare(query, quote), 0
    for ch in outside or '':
        depth += {'(': 1, ')': -1}.get(ch, 0)
        if depth < 0:
            return False
    return outside is not None and depth == 0


def stray(query: str, quote: str, refused: str) -> bool:
    outside = bare(query, quote)
    return outside is None or any(ch in outside for ch in refused)


def escaped(text: str) -> str:
    return text.replace('\\', '\\\\').replace("'", "\\'")


def filled(value) -> bool:
    return isinstance(value, str) and value != ''


def rendered(result: types.CallToolResult) -> str:
    parts = []
    if result.structuredContent is not None and not any(isinstance(b, types.TextContent) for b in result.content):
        parts.append(json.dumps(result.structuredContent, ensure_ascii=False, default=str))
    for b in result.content:
        if isinstance(b, types.TextContent):
            parts.append(b.text)
        elif isinstance(b, types.ImageContent):
            parts.append(msg('mcptools.part.image'))
        elif isinstance(b, types.AudioContent):
            parts.append(msg('mcptools.part.audio'))
        elif isinstance(b, types.EmbeddedResource) and isinstance(b.resource, types.TextResourceContents):
            parts.append(f'{b.resource.uri}\n{b.resource.text}')
        elif isinstance(b, types.EmbeddedResource):
            parts.append(msg('mcptools.part.binary', uri=str(b.resource.uri)))
        elif isinstance(b, types.ResourceLink):
            parts.append(f'{b.uri} {b.name}')
    return '\n'.join(parts)


def payload(result: types.CallToolResult):
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for part in result.content:
        if isinstance(part, types.TextContent):
            try:
                return json.loads(part.text)
            except (ValueError, RecursionError):
                continue
    return None


def listed(data, key: str) -> list:
    found = data.get(key) if isinstance(data, dict) else data
    return found if isinstance(found, list) else []


def trimmed(item: dict, keys: tuple[str, ...]) -> dict:
    return {key: item[key] for key in keys if key in item}


def fragments(value) -> list[str]:
    parts = [value] if isinstance(value, str) else [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
    kept, room = [], FRAGMENTS_MAX
    for part in parts:
        if room <= 0:
            break
        kept.append(part[:room])
        room -= len(kept[-1])
    return kept


def page_size(given) -> int:
    return max(1, min(given, PAGE_MAX)) if isinstance(given, int) and not isinstance(given, bool) else PAGE_MAX


def dump(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def widen() -> ScopeRefused:
    return ScopeRefused(msg('mcptools.scope.widen'))


class Adapter:
    id = ''
    noun = ''
    tools: frozenset[str] = frozenset()
    link_hosts: tuple[str, ...] = ()

    def folders(self, call: Call, q: str) -> list[Scope]:
        raise NotImplementedError

    def before(self, tool: str, args: dict, scope: Scope, call: Call) -> dict:
        raise NotImplementedError

    def after(self, tool: str, result: types.CallToolResult, scope: Scope) -> tuple[str, int]:
        raise NotImplementedError

    def outside(self) -> ScopeRefused:
        return ScopeRefused(msg('mcptools.scope.outside', noun=self.noun))

    def refusal(self, credential: str | None) -> str:
        if credential is None:
            return msg('mcptools.access.adapter_error', noun=self.noun)
        return msg('mcptools.access.adapter', noun=self.noun, credential=credential, permission=msg(f'mcptools.permission.{self.id}'))

    def refused(self, result: types.CallToolResult | None = None) -> ReadRefused:
        return ReadRefused(self.refusal(None), rendered(result) if result is not None else '')

    def payload_of(self, result: types.CallToolResult):
        if result.isError:
            raise self.refused(result)
        return payload(result)

    def none_kept(self, scope: Scope) -> tuple[str, int]:
        return msg('mcptools.scope.none_kept', noun=self.noun, folder=scope.label[:LABEL_MAX]), 0

    def listing(self, kept: list[dict], dropped: int, scope: Scope) -> tuple[str, int]:
        if dropped:
            log.info('Knowledge search left out results outside its %s: %d', self.noun, dropped)
        if not kept:
            return self.none_kept(scope)
        return dump(kept), len(kept)


class Drive(Adapter):
    id = 'drive'
    noun = 'folder'
    tools = DRIVE_TOOLS
    link_hosts = DRIVE_HOSTS

    def folders(self, call: Call, q: str) -> list[Scope]:
        words = q.strip()[:Q_MAX]
        query = f"mimeType = '{FOLDER_TYPE}'" + (f" and title contains '{escaped(words)}'" if words else '')
        found = listed(self.payload_of(call('search_files', {'query': query, 'pageSize': PAGE_MAX, 'excludeContentSnippets': True})), 'files')
        return [Scope(f['id'], f['title'], f['viewUrl'] if filled(f.get('viewUrl')) else '')
                for f in found if isinstance(f, dict) and filled(f.get('id')) and isinstance(f.get('title'), str)][:FOLDERS_MAX]

    def before(self, tool: str, args: dict, scope: Scope, call: Call) -> dict:
        if tool == 'search_files':
            return self.search(args, scope)
        if tool == 'read_file_content':
            return self.read(args, scope, call)
        if tool == 'get_file_metadata':
            self.file_id(args)
            return dict(args)
        raise self.outside()

    @staticmethod
    def search(args: dict, scope: Scope) -> dict:
        query = args.get('query')
        if query is not None and not isinstance(query, str):
            raise widen()
        words = (query or '').strip()
        if 'parentid' in words.lower() or not balanced(words, "'") or stray(words, "'", DRIVE_STRAY):
            raise widen()
        place = f"parentId = '{escaped(scope.id)}'"
        return {**args, 'query': f'{place} and ({words})' if words else place, 'pageSize': page_size(args.get('pageSize'))}

    def file_id(self, args: dict) -> str:
        file_id = args.get('fileId')
        if not isinstance(file_id, str) or FILE_ID.fullmatch(file_id) is None:
            raise self.outside()
        return file_id

    def read(self, args: dict, scope: Scope, call: Call) -> dict:
        file_id = self.file_id(args)
        meta = self.payload_of(call('get_file_metadata', {'fileId': file_id}))
        if not self.inside(meta, scope) or meta.get('mimeType') in UNREAD_TYPES:
            raise self.outside()
        return {'fileId': file_id, 'includeComments': False}

    def after(self, tool: str, result: types.CallToolResult, scope: Scope) -> tuple[str, int]:
        data = self.payload_of(result)
        if tool == 'search_files':
            found = listed(data, 'files')
            kept = [trimmed(f, FILE_KEYS) for f in found if self.inside(f, scope)]
            return self.listing(kept, len(found) - len(kept), scope)
        if tool == 'get_file_metadata' and self.inside(data, scope):
            return dump(trimmed(data, FILE_KEYS)), 1
        if tool == 'read_file_content' and isinstance(data, dict) and isinstance(data.get('fileContent'), str):
            link = data.get('viewUrl')
            return '\n'.join(([link] if filled(link) else []) + [data['fileContent']]), 1
        return self.none_kept(scope)

    @staticmethod
    def inside(item, scope: Scope) -> bool:
        return isinstance(item, dict) and filled(item.get('parentId')) and item['parentId'] == scope.id


class GitHub(Adapter):
    id = 'github'
    noun = 'repository'
    tools = GITHUB_TOOLS
    link_hosts = GITHUB_HOSTS

    def folders(self, call: Call, q: str) -> list[Scope]:
        words = q.strip()[:Q_MAX]
        if len(words) < Q_MIN:
            return []
        found = listed(self.payload_of(call('search_repositories', {'query': f'{words} in:name', 'perPage': PAGE_MAX, 'minimal_output': False})), 'items')
        return [Scope(r['full_name'], r['full_name'], r['html_url'])
                for r in found if isinstance(r, dict) and self.named(r.get('full_name')) and filled(r.get('html_url'))][:FOLDERS_MAX]

    def before(self, tool: str, args: dict, scope: Scope, call: Call) -> dict:
        if tool not in self.tools or not self.named(scope.id):
            raise self.outside()
        if tool == 'search_code':
            return self.search(args, scope)
        return self.contents(args, scope)

    @staticmethod
    def search(args: dict, scope: Scope) -> dict:
        query = args.get('query')
        if query is not None and not isinstance(query, str):
            raise widen()
        words = (query or '').strip()
        if QUALIFIER.search(words) or not balanced(words, '"') or stray(words, '"', GITHUB_STRAY):
            raise widen()
        full = f'({words}) repo:{scope.id}' if words else f'repo:{scope.id}'
        if len(full) > QUERY_MAX:
            raise ScopeRefused(msg('mcptools.scope.query_long', limit=QUERY_MAX))
        fields = args.get('fields')
        more = {'fields': fields + [f for f in SCOPE_FIELDS if f not in fields]} if isinstance(fields, list) else {}
        return {**args, 'query': full, **more}

    @staticmethod
    def contents(args: dict, scope: Scope) -> dict:
        owner, repo = scope.id.split('/')
        for key, want in (('owner', owner), ('repo', repo)):
            given = args.get(key, want)
            if not isinstance(given, str) or given.lower() != want.lower():
                raise widen()
        for key in PLACE_KEYS:
            given = args.get(key, '')
            if not isinstance(given, str) or any(part in given for part in PLACE_REFUSED) or UP in given.split('/'):
                raise widen()
        return {**args, 'owner': owner, 'repo': repo}

    def after(self, tool: str, result: types.CallToolResult, scope: Scope) -> tuple[str, int]:
        if result.isError:
            raise self.refused(result)
        if not self.named(scope.id):
            return self.none_kept(scope)
        if tool == 'search_code':
            found = listed(self.payload_of(result), 'items')
            kept = [self.code(i) for i in found if self.inside(i, scope)]
            return self.listing(kept, len(found) - len(kept), scope)
        if tool == 'get_file_contents':
            prefix = f'repo://{scope.id}/'.lower()
            kept = [self.text(p.resource) for p in result.content if isinstance(p, types.EmbeddedResource) and str(p.resource.uri).lower().startswith(prefix)]
            if kept:
                return '\n\n'.join(kept), len(kept)
        return self.none_kept(scope)

    @staticmethod
    def named(full_name) -> bool:
        return isinstance(full_name, str) and FULL_NAME.fullmatch(full_name) is not None

    @staticmethod
    def inside(item, scope: Scope) -> bool:
        repository = item.get('repository') if isinstance(item, dict) else None
        name = repository.get('full_name') if isinstance(repository, dict) else repository
        return isinstance(name, str) and name.lower() == scope.id.lower()

    @staticmethod
    def code(item: dict) -> dict:
        matches = item.get('text_matches')
        found = item['fragments'] if 'fragments' in item else [m.get('fragment') for m in matches if isinstance(m, dict)] if isinstance(matches, list) else None
        return trimmed(item, CODE_KEYS) | ({'fragments': fragments(found)} if found is not None else {})

    @staticmethod
    def text(resource: types.TextResourceContents | types.BlobResourceContents) -> str:
        uri = str(resource.uri)
        return f'{uri}\n{resource.text}' if isinstance(resource, types.TextResourceContents) else uri


ADAPTERS: dict[str, Adapter] = {'drive': Drive(), 'github': GitHub()}
