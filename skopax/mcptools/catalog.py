import hashlib
import json
import math
import re
import secrets as random
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from mcp import types
from pymongo.errors import DuplicateKeyError

from skopax.engine import iso
from skopax.errkind import Authored
from skopax.investigator.flags import scan
from skopax.messages import msg
from skopax.redact import redact_text
from skopax.registry import NUMBER_TYPES, SCHEMA_DEPTH, SCHEMA_TYPES, _schema_ok
from skopax.mcptools import DESCRIPTION_MAX, FOLDERS_TTL_S, LABEL_MAX, ON_MAX, PARAM_DESCRIPTION_MAX, PREFIX, SCHEMA_MAX, TOOLS_MAX, scrub
from skopax.mcptools.client import Listing
from skopax.mcptools.warnings import WARNINGS_VERSION, quoted

COLLECTION = 'mcp_tools'
NAME_MAX = 128
EXPOSED_MAX = 64
HASH_HEX = 16
SHORT_HASH = 6
ACCOUNT_BYTES = 6
UNSAFE = re.compile(r'[^A-Za-z0-9_-]')
API_NAME = re.compile(r'[A-Za-z0-9_.-]{1,64}')
COMPOSED = ('anyOf', 'oneOf', 'allOf', '$ref')
NUMBER_KEYS = ('minimum', 'maximum', 'minLength', 'maxLength', 'minItems', 'maxItems')
STALE = 'changed'
EXECUTE_WRITE = re.compile(r'execute.*(write|destructive)', re.IGNORECASE)


class InvalidSwitch(Authored, Exception):
    def __init__(self, field: str, text: str):
        super().__init__(text)
        self.field = field


class NeedsUnderstood(Authored, Exception):
    pass


class StaleListing(Authored, Exception):
    pass


@dataclass(frozen=True)
class Changes:
    new: tuple[str, ...]
    changed: tuple[str, ...]
    gone: tuple[str, ...]


def shown(name: str) -> str:
    return redact_text(name, NAME_MAX)


def flat(text: object) -> str:
    if not isinstance(text, str):
        return ''
    return ''.join(' ' if ch.isspace() else ch for ch in text if ch.isprintable() or ch.isspace()).strip()


def clean_description(text: object) -> str:
    return flat(text)[:DESCRIPTION_MAX]


def usable(name: object) -> bool:
    return isinstance(name, str) and 0 < len(name) <= NAME_MAX and name.isprintable()


def exposed_names(instance: str, names: list[str]) -> dict[str, str | None]:
    prefix = f'{PREFIX}{instance}_'
    plain = {n: prefix + UNSAFE.sub('_', n) for n in names if usable(n)}
    counts = Counter(plain.values())
    out: dict[str, str | None] = {}
    for n in names:
        if n not in plain:
            out[n] = None
        elif len(plain[n]) <= EXPOSED_MAX and counts[plain[n]] == 1:
            out[n] = plain[n]
        else:
            room = EXPOSED_MAX - len(prefix) - 1 - SHORT_HASH
            out[n] = f'{prefix}{UNSAFE.sub("_", n)[:room]}_{hashlib.sha256(n.encode()).hexdigest()[:SHORT_HASH]}'
    return out


def tool_hash(tool: types.Tool) -> str:
    notes = tool.annotations.model_dump(mode='json', exclude_none=True) if tool.annotations is not None else None
    raw = {'name': tool.name, 'description': tool.description, 'inputSchema': tool.inputSchema, 'annotations': notes}
    text = json.dumps(raw, sort_keys=True, separators=(',', ':'), ensure_ascii=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:HASH_HEX]


def write_named(name: object) -> bool:
    return isinstance(name, str) and EXECUTE_WRITE.match(name) is not None


def url_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:HASH_HEX]


def kind_of(tool: types.Tool) -> str:
    if write_named(tool.name):
        return 'write'
    notes = tool.annotations
    if notes is not None and (notes.destructiveHint is True or notes.readOnlyHint is False):
        return 'write'
    if notes is not None and notes.readOnlyHint is True:
        return 'read'
    return 'unknown'


def finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _types(value: object) -> list[str] | None:
    found = [value] if isinstance(value, str) else value
    if isinstance(found, list) and found and all(isinstance(x, str) and x in SCHEMA_TYPES for x in found):
        return found
    return None


def _clean(s: object, depth: int, secrets: tuple[str, ...]) -> dict:
    if not isinstance(s, dict) or (depth and any(k in s for k in COMPOSED)):
        return {}
    out: dict = {}
    kinds = _types(s.get('type'))
    if kinds is not None:
        out['type'] = s['type']
    plain = set(kinds or ()) - {'null'}
    if isinstance(s.get('description'), str):
        out['description'] = scrub(flat(s['description']), secrets)[:PARAM_DESCRIPTION_MAX]
    if isinstance(s.get('enum'), list):
        out['enum'] = [scrub(v, secrets) if isinstance(v, str) else v for v in s['enum'] if not isinstance(v, float) or math.isfinite(v)]
    for key in NUMBER_KEYS:
        if finite(s.get(key)):
            out[key] = s[key]
    if not (plain and plain <= NUMBER_TYPES):
        out.pop('minimum', None)
        out.pop('maximum', None)
    if plain != {'string'}:
        out.pop('minLength', None)
        out.pop('maxLength', None)
    if isinstance(s.get('additionalProperties'), bool):
        out['additionalProperties'] = s['additionalProperties']
    if isinstance(s.get('required'), list) and all(isinstance(k, str) for k in s['required']):
        out['required'] = [scrub(k, secrets) for k in s['required']]
    if depth < SCHEMA_DEPTH and isinstance(s.get('properties'), dict):
        out['properties'] = {scrub(k, secrets): _clean(v, depth + 1, secrets) for k, v in s['properties'].items() if isinstance(k, str)}
    if depth < SCHEMA_DEPTH and isinstance(s.get('items'), dict):
        out['items'] = _clean(s['items'], depth + 1, secrets)
    return out


def object_tree(schema: object, secrets: Iterable[str] = ()) -> dict | None:
    if not isinstance(schema, dict):
        return None
    top = dict(schema)
    if 'type' not in top and isinstance(top.get('properties'), dict):
        top['type'] = 'object'
    if top.get('type') != 'object':
        return None
    return _clean(top, 0, tuple(secrets))


def clean_schema(schema: object, secrets: Iterable[str] = ()) -> dict | None:
    cleaned = object_tree(schema, secrets)
    if cleaned is None:
        return None
    try:
        size = len(json.dumps(cleaned, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return None
    if size > SCHEMA_MAX or not _schema_ok(cleaned):
        return None
    return cleaned


def tidy(text: str) -> bool:
    return text == text.strip() and text.isprintable()


def malformed(schema: object, depth: int = 0) -> bool:
    if not isinstance(schema, dict) or depth > SCHEMA_DEPTH:
        return False
    names = [*(schema.get('properties') or {}), *(schema.get('required') or ())]
    choices = [v for v in schema.get('enum') or () if isinstance(v, str)]
    if not all(tidy(t) for t in names + choices):
        return True
    return any(malformed(sub, depth + 1) for sub in (schema.get('properties') or {}).values()) or malformed(schema.get('items'), depth + 1)


def echoes(schema: object, secrets: tuple[str, ...], depth: int = 0) -> bool:
    if not secrets or not isinstance(schema, dict) or depth > SCHEMA_DEPTH:
        return False
    names = [*(schema.get('properties') or {}), *(schema.get('required') or ())]
    choices = [v for v in schema.get('enum') or () if isinstance(v, str)]
    if any(scrub(t, secrets) != t for t in names + choices):
        return True
    return any(echoes(sub, secrets, depth + 1) for sub in (schema.get('properties') or {}).values()) or echoes(schema.get('items'), secrets, depth + 1)


def sendable(schema: object, depth: int = 0) -> bool:
    if not isinstance(schema, dict) or depth > SCHEMA_DEPTH:
        return True
    names = [*(schema.get('properties') or {}), *(schema.get('required') or ())]
    if not all(isinstance(n, str) and API_NAME.fullmatch(n) for n in names):
        return False
    return all(sendable(sub, depth + 1) for sub in (schema.get('properties') or {}).values()) and sendable(schema.get('items'), depth + 1)


def schema_texts(schema: object, depth: int = 0) -> list[str]:
    if not isinstance(schema, dict) or depth > SCHEMA_DEPTH:
        return []
    found = [schema['description']] if isinstance(schema.get('description'), str) else []
    found += [v for v in schema.get('enum') or () if isinstance(v, str)]
    for key, sub in (schema.get('properties') or {}).items():
        found += [key, *schema_texts(sub, depth + 1)]
    return found + schema_texts(schema.get('items'), depth + 1)


def scan_row(row: dict) -> str | None:
    text = ' '.join([row.get('title') or '', row.get('description') or '', *schema_texts(row.get('schema'))])
    found = scan('mcp', text)
    return found[0]['phrase'] if found else None


def unique(tools: Iterable[types.Tool], secrets: tuple[str, ...] = ()) -> dict[str, types.Tool]:
    seen: dict[str, types.Tool] = {}
    for t in tools:
        seen.setdefault(scrub(t.name, secrets), t)
    return seen


def scope_id(doc: dict) -> str | None:
    scope = doc.get('scope')
    return scope.get('id') if isinstance(scope, dict) else None


def new_account() -> str:
    return random.token_hex(ACCOUNT_BYTES)


class Catalog:
    def __init__(self, db, clock: Callable[[], float] = time.time, note: Callable[[str, str], None] | None = None, public: Callable[[], bool] | None = None):
        self.col = db[COLLECTION]
        self.clock = clock
        self.note = note or (lambda by, text: None)
        self.public = public or (lambda: False)
        self._guard = threading.Lock()

    def get(self, name: str) -> dict | None:
        return self.col.find_one({'_id': name})

    def listed(self, name: str, url: str = '') -> bool:
        doc = self.get(name)
        return bool(doc and doc.get('listedAt') and doc.get('url') == url_key(url))

    def account_of(self, name: str) -> str:
        found = (self.get(name) or {}).get('account')
        if found:
            return found
        try:
            self.col.update_one({'_id': name, 'account': {'$exists': False}}, {'$set': {'account': new_account()}}, upsert=True)
        except DuplicateKeyError:
            pass
        return (self.get(name) or {})['account']

    def merge(self, name: str, listing: Listing, by: str, adapter_tools: frozenset[str] | None = None, secrets: Iterable[str] = (), denied: Iterable[str] = (),
              account: str | None = None, url: str = '') -> Changes:
        hidden, refused, place = tuple(secrets), frozenset(denied), url_key(url)
        with self._guard:
            doc = self.get(name) or {}
            if account is not None and doc.get('account') != account:
                raise StaleListing(msg('mcptools.tools.list_again'))
            moved = 'url' in doc and doc['url'] != place
            first = moved or not doc.get('listedAt')
            old = {} if moved else {r['name']: r for r in doc.get('tools') or []}
            tools = unique(listing.tools[:TOOLS_MAX], hidden)
            masked = {n for n, t in tools.items() if n != t.name}
            exposed = exposed_names(name, [n for n in tools if n not in masked])
            clashes = {n for n, count in Counter(v for v in exposed.values() if v is not None).items() if count > 1}
            rows, new, changed, on = [], [], [], 0
            for n, t in tools.items():
                row = self._row(t, n, exposed.get(n), hidden)
                row['blocked'] = self._blocked(row, row['exposed'] in clashes, adapter_tools, refused)
                row['flagged'] = scan_row(row)
                prev = old.get(n)
                if first:
                    row['on'] = row['kind'] == 'read' and not row['blocked'] and not row['flagged'] and on < ON_MAX
                    on += row['on']
                elif prev is not None and prev.get('hash') == row['hash']:
                    row.update(on=bool(prev.get('on')) and not row['blocked'], changed=bool(prev.get('changed')), understood=prev.get('understood'))
                elif prev is not None:
                    row['changed'] = True
                    changed.append(n)
                else:
                    new.append(n)
                rows.append(row)
            gone = [n for n in old if n not in tools]
            put = {'server': {'name': scrub(listing.server, hidden), 'version': scrub(listing.version, hidden)}, 'listedAt': iso(self.clock()), 'listedBy': by,
                   'truncated': bool(listing.truncated) or len(listing.tools) > TOOLS_MAX, 'tools': rows, 'url': place,
                   'account': new_account() if moved else doc.get('account') or new_account()}
            dropped = {'refused': '', **({'scope': '', 'folders': '', 'confirm': ''} if moved else {})}
            self.col.update_one({'_id': name}, {'$set': put, '$unset': dropped}, upsert=True)
        self.note(by, msg('mcptools.tools.listed_one' if len(rows) == 1 else 'mcptools.tools.listed', by=by, instance=name, count=len(rows), on=sum(1 for r in rows if r['on'])))
        return Changes(tuple(new), tuple(changed), tuple(gone))

    @staticmethod
    def _row(t: types.Tool, name: str, exposed: str | None, hidden: tuple[str, ...]) -> dict:
        title = t.title or (t.annotations.title if t.annotations is not None else None) or ''
        return {'name': name, 'masked': name != t.name, 'exposed': exposed, 'title': scrub(flat(title), hidden)[:LABEL_MAX], 'description': clean_description(scrub(flat(t.description), hidden)),
                'schema': clean_schema(t.inputSchema, hidden), 'hash': tool_hash(t), 'kind': kind_of(t), 'on': False, 'blocked': None, 'flagged': None, 'changed': False,
                'understood': None, 'echoed': echoes(object_tree(t.inputSchema), hidden)}

    @staticmethod
    def _blocked(row: dict, clash: bool, adapter_tools: frozenset[str] | None, refused: frozenset[str]) -> str | None:
        if row.get('masked'):
            return msg('mcptools.tools.malformed')
        if row['exposed'] is None:
            return msg('mcptools.tools.bad_name')
        if clash:
            return msg('mcptools.tools.name_clash')
        if row['schema'] is None:
            return msg('mcptools.tools.schema')
        if malformed(row['schema']) or row.get('echoed'):
            return msg('mcptools.tools.malformed')
        if not API_NAME.fullmatch(row['exposed']) or not sendable(row['schema']):
            return msg('mcptools.tools.field_name')
        if adapter_tools is not None and row['name'] not in adapter_tools:
            return msg('mcptools.knowledge.only_adapted')
        if row['name'] in refused:
            return msg('mcptools.tools.denied', tool=shown(row['name']))
        if row['kind'] == 'write':
            return msg('mcptools.tools.write_later', tool=shown(row['name']))
        return None

    def restrict(self, name: str, adapter_tools: frozenset[str] | None, denied: Iterable[str] = ()) -> None:
        refused = frozenset(denied)
        with self._guard:
            rows = (self.get(name) or {}).get('tools') or []
            counts = Counter(r.get('exposed') for r in rows if r.get('exposed') is not None)
            for row in rows:
                row['blocked'] = self._blocked(row, counts[row.get('exposed')] > 1, adapter_tools, refused)
                row['on'] = bool(row['on']) and not row['blocked']
            if rows:
                self.col.update_one({'_id': name}, {'$set': {'tools': rows}})

    def switch(self, name: str, on: list[str], understood: bool, by: str) -> dict:
        with self._guard:
            doc = self.get(name) or {}
            rows = doc.get('tools') or []
            found = {r['name']: r for r in rows}
            wanted = list(dict.fromkeys(on))
            for n in wanted:
                row = found.get(n)
                if row is None:
                    raise InvalidSwitch('on', msg('mcptools.tools.unknown', tool=shown(n)))
                if row['blocked']:
                    raise InvalidSwitch('on', row['blocked'])
                if row['kind'] == 'write':
                    raise InvalidSwitch('on', msg('mcptools.tools.write_later', tool=shown(n)))
                if row.get('hash') is None:
                    raise InvalidSwitch('on', msg('mcptools.tools.refresh_first', tool=shown(n)))
            if len(wanted) > ON_MAX:
                raise InvalidSwitch('on', msg('mcptools.tools.too_many_on', limit=ON_MAX))
            fresh = [n for n in wanted if found[n]['kind'] == 'unknown' and not found[n]['on']]
            if fresh and not understood:
                raise NeedsUnderstood(msg('mcptools.tools.understand_first', tool=shown(fresh[0])))
            stamp = iso(self.clock())
            turned_on = [r['name'] for r in rows if r['name'] in wanted and not r['on']]
            turned_off = [r['name'] for r in rows if r['name'] not in wanted and r['on']]
            for row in rows:
                if row['name'] in fresh:
                    row['understood'] = {'by': by, 'at': stamp}
                if row['name'] in wanted:
                    row['changed'] = False
                row['on'] = row['name'] in wanted
            self.col.update_one({'_id': name}, {'$set': {'tools': rows}})
        if turned_on or turned_off:
            self.note(by, msg('mcptools.tools.switched', by=by, instance=name, on=self._names(turned_on), off=self._names(turned_off)))
        return self.get(name) or {}

    @staticmethod
    def _names(names: list[str]) -> str:
        return ', '.join(shown(n) for n in names) if names else msg('mcptools.tools.none')

    def turn_off(self, name: str, tools: list[str], why: str) -> None:
        with self._guard:
            doc = self.get(name) or {}
            rows = doc.get('tools') or []
            for row in rows:
                if row['name'] in tools:
                    row['on'] = False
                    if why == STALE:
                        row.update(changed=True, hash=None)
            self.col.update_one({'_id': name}, {'$set': {'tools': rows}})

    def confirm(self, name: str, version: int, by: str, url: str = '') -> dict:
        with self._guard:
            doc = self.get(name) or {}
            record = {'by': by, 'at': iso(self.clock()), 'account': doc.get('account') or new_account(), 'scope': scope_id(doc), 'version': version, 'public': bool(self.public()),
                      'url': url_key(url)}
            self.col.update_one({'_id': name}, {'$set': {'confirm': record, 'account': record['account']}}, upsert=True)
        self.note(by, msg('mcptools.confirmed', by=by, instance=name))
        return record

    def current(self, name: str, knowledge: bool, doc: dict | None = None, url: str = '') -> bool:
        doc = (self.get(name) or {}) if doc is None else doc
        record = doc.get('confirm')
        if not isinstance(record, dict) or not doc.get('account') or record.get('url') != url_key(url):
            return False
        if record.get('version') != WARNINGS_VERSION or record.get('account') != doc['account'] or record.get('public') != bool(self.public()):
            return False
        return not knowledge or (scope_id(doc) is not None and record.get('scope') == scope_id(doc))

    def list_folders(self, name: str, adapter: str, items: list[dict]) -> None:
        account = self.account_of(name)
        self.col.update_one({'_id': name}, {'$set': {'folders': {'at': self.clock(), 'adapter': adapter, 'account': account, 'items': items}}}, upsert=True)

    def listed_folder(self, name: str, adapter: str, folder_id: str) -> dict | None:
        doc = self.get(name) or {}
        found = doc.get('folders')
        if not isinstance(found, dict) or found.get('adapter') != adapter or not doc.get('account') or found.get('account') != doc['account']:
            return None
        if not finite(found.get('at')) or self.clock() - found['at'] > FOLDERS_TTL_S:
            return None
        return next((i for i in found.get('items') or [] if isinstance(i, dict) and i.get('id') == folder_id), None)

    def set_scope(self, name: str, folder: dict, noun: str, by: str) -> dict:
        record = {'id': folder['id'], 'label': folder.get('label') or '', 'url': folder.get('url') or '', 'by': by, 'at': iso(self.clock())}
        self.col.update_one({'_id': name}, {'$set': {'scope': record}, '$unset': {'confirm': ''}}, upsert=True)
        self.note(by, msg('mcptools.scope.set', by=by, noun=noun, instance=name, folder=quoted(record['label'])))
        return record

    def refusal(self, name: str, text: str | None, call: bool = False) -> None:
        if text is None:
            self.col.update_one({'_id': name}, {'$unset': {'refused': ''}})
        else:
            self.col.update_one({'_id': name}, {'$set': {'refused': {'text': text, 'at': iso(self.clock()), 'call': call}}})

    def unconfirm(self, name: str, scope: bool = False) -> None:
        self.col.update_one({'_id': name}, {'$unset': {'confirm': '', **({'scope': ''} if scope else {})}})

    def bump_account(self, name: str) -> None:
        self.col.update_one({'_id': name}, {'$set': {'account': new_account()}, '$unset': {'confirm': ''}}, upsert=True)

    def reset(self, name: str) -> None:
        self.col.replace_one({'_id': name}, {'_id': name, 'account': new_account()}, upsert=True)

    def forget(self, name: str) -> None:
        self.col.delete_one({'_id': name})
