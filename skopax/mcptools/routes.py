import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from skopax.contract import Context, Plugin, TestResult
from skopax.instances import Instance, Instances
from skopax.messages import msg
from skopax.redact import redact_text, secrets_masked
from skopax.mcptools import (ERROR_TEXT_MAX, FOLDERS_MAX, LABEL_MAX, LIST_DEADLINE_S, OUTPUT_MAX, PLUGIN_ID, TEST_DEADLINE_S, TOOLS_MAX, Reconnect, TokenSource, left, scrub,
                             secrets_of, within)
from skopax.mcptools import client as mcp_client
from skopax.mcptools import presets, warnings
from skopax.mcptools.adapters import ADAPTERS, Q_MAX, Adapter, ReadRefused, Scope
from skopax.mcptools.catalog import NAME_MAX, Catalog, InvalidSwitch, NeedsUnderstood, StaleListing, scope_id
from skopax.mcptools.client import FORBIDDEN, PERMISSION_WORDS, UNAUTHORIZED, Listing, McpClient, McpFailure, Unauthorized, refused_scope
from skopax.mcptools.oauth import CALLBACK_PATH, FlowError, McpOAuth, NoScope, NotOAuth

GATED = ('url', 'auth', 'header-name', 'header-value', 'oauth-scopes', 'oauth-client-id', 'oauth-client-secret', 'preset', 'knowledge', 'ca')
ACCOUNT_FIELDS = ('auth', 'header-name', 'header-value', 'oauth-scopes', 'oauth-client-id', 'oauth-client-secret', 'ca')
URL_SECRETS = {'header': 'header-value', 'oauth': 'oauth-client-secret'}
DEFAULT_HEADER = 'Authorization'
LOCAL_HTTP = 'allow-local-http'
AUTH_DETAIL = ('connectedBy', 'connectedAt', 'expiresAt', 'last')
SCOPE_ID_MAX = 200
URL_MAX = 2000
REFUSED = (401, 403)


def http_for(ca: str) -> Callable[[], httpx.AsyncClient]:
    return partial(mcp_client.client_for, ca)


@dataclass
class Deps:
    catalog: Catalog
    tokens: TokenSource
    instances: Instances
    plugin: Callable[[], Plugin | None]
    setting: Callable[[str], object]
    refused: Callable[[str, str], Exception]
    http: Callable[[str], Callable[[], httpx.AsyncClient]] = http_for
    oauth: McpOAuth | None = None


class SwitchIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    on: list[Annotated[str, Field(max_length=NAME_MAX)]] = Field(max_length=TOOLS_MAX)
    understood: bool = Field(default=False, strict=True)


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    understood: bool = Field(default=False, strict=True)
    version: int = Field(default=0, strict=True)


class ScopeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(max_length=SCOPE_ID_MAX)


def defaults(deps: Deps) -> dict[str, str]:
    plugin = deps.plugin()
    return {f.key: f.default for f in plugin.fields if f.default} if plugin is not None else {}


def effective(deps: Deps, values: Mapping[str, str | None]) -> dict[str, str]:
    return {**defaults(deps), **{k: v for k, v in values.items() if v}}


def values_of(deps: Deps, inst: Instance) -> dict[str, str]:
    return effective(deps, {**inst.values, **inst.secrets})


def knowledge_on(values: Mapping[str, str]) -> bool:
    return values.get('knowledge') == 'true'


def labels(deps: Deps) -> dict[str, str]:
    plugin = deps.plugin()
    return {f.key: f.label for f in plugin.fields} if plugin is not None else {}


def adapter_tools(values: Mapping[str, str]) -> frozenset[str] | None:
    preset = presets.get(values.get('preset'))
    if knowledge_on(values) and preset is not None and preset.knowledge == 'ready':
        return frozenset(preset.knowledge_tools)
    return None


def watched(make: Callable[[], httpx.AsyncClient], heard: dict, secrets: tuple[str, ...], named: tuple[int, ...] = REFUSED) -> Callable[[], httpx.AsyncClient]:
    async def answer(response: httpx.Response) -> None:
        if response.status_code in REFUSED and response.request.method == 'POST':
            sent = secrets_of({'header-value': response.request.headers.get('authorization') or ''})
            found = refused_scope(response, REFUSED, any_error=response.status_code in named, secrets=tuple(dict.fromkeys((*secrets, *sent))))
            heard.update(status=response.status_code, scope=found or '')

    def build() -> httpx.AsyncClient:
        client = make()
        hooks = client.event_hooks
        client.event_hooks = {**hooks, 'response': [*hooks.get('response', []), answer]}
        return client
    return build


def credential(values: Mapping[str, str]) -> str | None:
    mode = values.get('auth') or 'none'
    if mode == 'header':
        return msg('mcptools.credential.header', header=values.get('header-name') or DEFAULT_HEADER)
    return msg('mcptools.credential.oauth') if mode == 'oauth' else None


def permission_of(heard: dict, hidden: tuple[str, ...]) -> str:
    scope = scrub(heard.get('scope') or '', hidden)
    return msg('mcptools.access.scope', scope=scope) if scope else msg('mcptools.access.unknown')


def denied(values: Mapping[str, str]) -> tuple[str, ...]:
    host = (presets.origin(values.get('url') or '') or ('', '', None))[1]
    found = [p for p in presets.load() if not p.soon and (p.id == values.get('preset') or (host and presets.origin(p.url)[1] == host))]
    return tuple(dict.fromkeys(n for p in found for n in p.denied))


def ready_adapter(values: Mapping[str, str]) -> Adapter | None:
    preset = presets.get(values.get('preset'))
    return ADAPTERS.get(preset.adapter) if preset is not None and preset.knowledge == 'ready' else None


def on_origin(values: Mapping[str, str]) -> bool:
    preset = presets.get(values.get('preset'))
    return preset is not None and presets.same_origin(values.get('url') or '', preset.url)


def knowledge_adapter(values: Mapping[str, str]) -> Adapter:
    adapter = ready_adapter(values)
    if adapter is None:
        raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.knowledge.no_adapter'))
    if not on_origin(values):
        raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.preset.origin'))
    return adapter


def allowed(read: Callable[[], object]) -> bool:
    try:
        return read() is True
    except KeyError:
        return False


def wanted(values: Mapping[str, str]) -> str:
    preset = presets.get(values.get('preset'))
    scopes = (values.get('oauth-scopes') or (preset.scopes if preset is not None else '')) if values.get('auth') == 'oauth' else ''
    return msg('mcptools.access.scope', scope=scopes) if scopes else msg('mcptools.access.unknown')


def local_http(deps: Deps) -> bool:
    return allowed(lambda: deps.setting(LOCAL_HTTP))


def client_of(tokens: TokenSource, http: Callable[[str], Callable[[], httpx.AsyncClient]], name: str, values: Mapping[str, str], allow_local_http: bool,
              renew: bool = False, signing: Mapping[str, str] | None = None) -> McpClient:
    mode = values.get('auth') or 'none'
    headers: dict[str, str] = {}
    signer = values if signing is None else signing
    if mode == 'header':
        headers = {values.get('header-name') or DEFAULT_HEADER: values.get('header-value') or ''}
    elif mode == 'oauth':
        headers = {DEFAULT_HEADER: 'Bearer ' + (tokens.after_unauthorized(name, signer) if renew else tokens.token(name, signer))}
    held = tuple(dict.fromkeys(secrets_of(values, tokens.secrets(name)) + secrets_of(signer)))
    return McpClient(values.get('url') or '', headers, allow_local_http, http=http(values.get('ca') or ''), secrets=held, held=credential(values), named=True)


def signing_of(deps: Deps, name: str, values: Mapping[str, str]) -> Mapping[str, str]:
    saved = deps.instances.get(PLUGIN_ID, name)
    return values if saved is None else {**values_of(deps, saved), 'url': values.get('url') or ''}


def moved_away(deps: Deps, name: str, values: Mapping[str, str]) -> bool:
    if (values.get('auth') or 'none') != 'oauth' or not deps.tokens.moved(name, values.get('url') or ''):
        return False
    deps.tokens.park(name, 'moved')
    return True


def listing_of(deps: Deps, name: str, values: Mapping[str, str], allow_local_http: bool, deadline: float, signing: Mapping[str, str] | None = None) -> Listing:
    with within(deadline):
        try:
            client = client_of(deps.tokens, deps.http, name, values, allow_local_http, signing=signing)
            return client.listing(left(deadline), secrets_of(values, deps.tokens.secrets(name)))
        except Unauthorized:
            if (values.get('auth') or 'none') != 'oauth':
                raise
        client = client_of(deps.tokens, deps.http, name, values, allow_local_http, renew=True, signing=signing)
        return client.listing(left(deadline), secrets_of(values, deps.tokens.secrets(name)))


def folders_of(deps: Deps, name: str, values: Mapping[str, str], adapter: Adapter, q: str, heard: dict) -> list[Scope]:
    hidden = secrets_of(values, deps.tokens.secrets(name))
    http = lambda ca: watched(deps.http(ca), heard, hidden, (FORBIDDEN,))

    def listed(client: McpClient) -> list[Scope]:
        return adapter.folders(lambda tool, args: client.call(tool, args, left(LIST_DEADLINE_S)), q)

    with within(LIST_DEADLINE_S):
        try:
            return listed(client_of(deps.tokens, http, name, values, local_http(deps)))
        except Unauthorized:
            if (values.get('auth') or 'none') != 'oauth':
                raise
        heard.clear()
        heard['renewed'] = True
        return listed(client_of(deps.tokens, http, name, values, local_http(deps), renew=True))


def list_failure(deps: Deps, name: str, values: Mapping[str, str], adapter: Adapter, heard: dict, error: McpFailure) -> str:
    hidden, held = secrets_of(values, deps.tokens.secrets(name)), credential(values)
    if isinstance(error, Unauthorized):
        if held is None:
            return msg('connection.rejected', status=UNAUTHORIZED)
        if heard.get('renewed') and heard.get('status') == UNAUTHORIZED:
            return msg('mcptools.access.list_invalid', noun=adapter.noun, credential=held, permission=permission_of(heard, hidden))
    elif heard.get('status') == FORBIDDEN:
        if held is None:
            return msg('mcptools.access.list_no_credential', instance=name, noun=adapter.noun)
        return msg('mcptools.access.list_refused', noun=adapter.noun, credential=held, permission=permission_of(heard, hidden))
    return scrub(error.text, hidden)


def server_words(said: str, hidden: tuple[str, ...]) -> str:
    return redact_text(secrets_masked(scrub(said, hidden)[:2 * OUTPUT_MAX]), ERROR_TEXT_MAX)


def read_refusal(adapter: Adapter, values: Mapping[str, str], error: ReadRefused, hidden: tuple[str, ...]) -> str:
    words = server_words(error.said, hidden)
    base = adapter.refusal(credential(values) if PERMISSION_WORDS.search(words) else None)
    return msg('mcptools.access.adapter_words', refusal=base, text=words) if words else base


def folder_url(url: str, hidden: tuple[str, ...]) -> str:
    try:
        kept = len(url) <= URL_MAX and presets.https(url)
    except ValueError:
        return ''
    return redact_text(scrub(url, hidden)) if kept else ''


def cut_label(label: str) -> str:
    return label if len(label) <= LABEL_MAX else label[:LABEL_MAX - 1].rstrip() + '\u2026'


def folder_items(found: list[Scope], hidden: tuple[str, ...]) -> list[dict]:
    items = []
    for folder in found:
        if not 0 < len(folder.id) <= SCOPE_ID_MAX or not folder.id.isprintable() or scrub(folder.id, hidden) != folder.id:
            continue
        items.append({'id': folder.id, 'label': cut_label(redact_text(scrub(folder.label, hidden)).strip()), 'url': folder_url(folder.url, hidden)})
    return items[:FOLDERS_MAX]


def tester(deps: Deps) -> Callable[[Context], TestResult]:
    def test(ctx: Context) -> TestResult:
        info = ctx.instance
        values = dict(info.config)
        saved = deps.instances.get(PLUGIN_ID, info.name)
        if saved is not None and moved_away(deps, info.name, values_of(deps, saved)):
            return TestResult(False, msg('mcptools.oauth.moved', instance=info.name), 0)
        began = time.monotonic()
        try:
            found = listing_of(deps, info.name, values, allowed(lambda: ctx.setting(LOCAL_HTTP)), TEST_DEADLINE_S, signing_of(deps, info.name, values))
        except NoScope as e:
            return TestResult(False, scrub(e.text, secrets_of(values, deps.tokens.secrets(info.name))), 0)
        except Reconnect:
            return TestResult(False, msg('mcptools.oauth.connect_first'), 0)
        except McpFailure as e:
            return TestResult(False, scrub(e.text, secrets_of(values, deps.tokens.secrets(info.name))), int((time.monotonic() - began) * 1000))
        server = found.server or (urlsplit(values.get('url') or '').hostname or '')
        if not found.tools and credential(values) is None:
            return TestResult(False, msg('mcptools.test_empty_open', server=server), found.ms)
        if not found.tools:
            return TestResult(False, msg('mcptools.test_empty', server=server, permission=wanted(values)), found.ms)
        return TestResult(True, msg('mcptools.test_ok', server=server, count=len(found.tools)), found.ms)
    return test


def instance_of(deps: Deps, name: str) -> tuple[Instance, dict[str, str]]:
    inst = deps.instances.get(PLUGIN_ID, name)
    if inst is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, msg('instances.unknown', name=name))
    return inst, values_of(deps, inst)


def row_view(row: dict) -> dict:
    schema = row.get('schema') or {}
    return {'name': row['name'], 'exposed': row.get('exposed'), 'title': row.get('title') or '', 'description': row.get('description') or '', 'kind': row.get('kind'),
            'on': bool(row.get('on')), 'blocked': row.get('blocked'), 'flagged': msg('mcptools.tools.flagged', phrase=row['flagged']) if row.get('flagged') else None,
            'changed': bool(row.get('changed')), 'understood': row.get('understood'), 'params': list(schema.get('properties') or {})}


def signed_in(deps: Deps, name: str) -> dict:
    if deps.oauth is not None and deps.oauth is deps.tokens:
        return deps.oauth.view(name)
    detail = deps.oauth.view(name) if deps.oauth is not None else {}
    return {**detail, 'state': deps.tokens.state(name)}


def flow(deps: Deps) -> McpOAuth:
    if deps.oauth is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, msg('mcptools.oauth.not_oauth'))
    return deps.oauth


def auth_view(deps: Deps, name: str, mode: str) -> dict:
    found: dict = {'mode': mode, 'state': 'none', 'connectedBy': None, 'connectedAt': None, 'expiresAt': None, 'last': None}
    if mode != 'oauth':
        return found
    detail = signed_in(deps, name)
    return found | {k: detail[k] for k in AUTH_DETAIL if k in detail} | {'state': detail['state']}


def view(deps: Deps, name: str) -> dict:
    _, values = instance_of(deps, name)
    doc = deps.catalog.get(name) or {}
    preset = presets.get(values.get('preset'))
    state = preset.knowledge if preset is not None else 'none'
    adapter = ADAPTERS.get(preset.adapter) if preset is not None and preset.adapter else None
    scope = doc.get('scope') if isinstance(doc.get('scope'), dict) else None
    record = doc.get('confirm') if isinstance(doc.get('confirm'), dict) else {}
    knowledge, mode = knowledge_on(values), values.get('auth') or 'none'
    return {'name': name, 'server': doc.get('server'), 'listedAt': doc.get('listedAt'), 'truncated': bool(doc.get('truncated')), 'preset': preset.id if preset is not None else '',
            'tools': [row_view(r) for r in doc.get('tools') or []],
            'auth': auth_view(deps, name, mode),
            'knowledge': {'on': knowledge, 'state': state, 'possible': state == 'ready', 'noun': adapter.noun if adapter is not None else None, 'scope': scope},
            'confirm': {'current': deps.catalog.current(name, knowledge, None, values.get('url') or ''), 'by': record.get('by'), 'at': record.get('at')},
            'refused': doc.get('refused') if isinstance(doc.get('refused'), dict) else None,
            'warnings': warnings.view(knowledge and state == 'ready', scope.get('label') if scope else None, bool(deps.catalog.public())),
            'identity': msg('mcptools.identity_note'),
            'notes': {'direct': msg('mcptools.knowledge.direct'), 'truncated': msg('mcptools.tools.truncated'), 'knowledgeSoon': msg('mcptools.knowledge.soon'),
                      'empty': msg('mcptools.tools.empty')}}


def check_origin(deps: Deps, values: Mapping[str, str]) -> None:
    preset = presets.get(values.get('preset'))
    url = values.get('url') or ''
    if preset is not None and preset.adapter and url and not presets.same_origin(url, preset.url):
        raise deps.refused('preset', msg('mcptools.preset.origin'))


def check_knowledge(deps: Deps, values: Mapping[str, str]) -> None:
    preset = presets.get(values.get('preset'))
    if knowledge_on(values) and (preset is None or preset.knowledge != 'ready'):
        raise deps.refused('knowledge', msg('mcptools.knowledge.no_adapter'))


def check_create(deps: Deps, values: Mapping[str, str | None], enabled: bool) -> None:
    if enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.confirm_first'))
    check_origin(deps, effective(deps, values))
    check_knowledge(deps, effective(deps, values))


def patched(deps: Deps, before: Instance, given: Mapping[str, str | None]) -> dict[str, str]:
    raw = {**before.values, **before.secrets}
    for key, value in given.items():
        if value is not None:
            raw[key] = value
    return effective(deps, raw)


def moved(deps: Deps, before: Instance, given: Mapping[str, str | None]) -> bool:
    return patched(deps, before, given).get('url', '') != values_of(deps, before).get('url', '')


def pem_text(value: object) -> str:
    return ''.join(value.split()) if isinstance(value, str) else ''


def retrusted(deps: Deps, before: Instance, given: Mapping[str, str | None]) -> bool:
    return pem_text(patched(deps, before, given).get('ca')) != pem_text(values_of(deps, before).get('ca'))


def patch_values(deps: Deps, before: Instance, given: Mapping[str, str | None]) -> dict[str, str | None]:
    out = dict(given)
    if moved(deps, before, given) or retrusted(deps, before, given):
        out.update({key: '' for key in URL_SECRETS.values() if before.secrets.get(key) and not given.get(key)})
    return out


def check_patch(deps: Deps, before: Instance, given: Mapping[str, str | None], enabled: bool | None) -> None:
    stored = values_of(deps, before)
    after = patched(deps, before, given)
    changed = [k for k in GATED if after.get(k, '') != stored.get(k, '')]
    if enabled is True and (changed or not deps.catalog.current(before.name, knowledge_on(stored), None, stored.get('url') or '')):
        raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.confirm_first'))
    if 'url' in changed or retrusted(deps, before, given):
        check_reenter(deps, before, given, after, 'integrations.reenter_secret' if 'url' in changed else 'integrations.reenter_trust')
    if 'url' in changed or 'preset' in changed:
        check_origin(deps, after)
    if 'knowledge' in changed or 'preset' in changed:
        check_knowledge(deps, after)


def check_reenter(deps: Deps, before: Instance, given: Mapping[str, str | None], after: Mapping[str, str], said: str = 'integrations.reenter_secret') -> None:
    active = URL_SECRETS.get(after.get('auth') or '')
    for key in sorted(URL_SECRETS.values(), key=lambda k: k != active):
        if not before.secrets.get(key) or given.get(key):
            continue
        if key in before.owned:
            raise deps.refused(key, msg('integrations.reenter_owned', field=labels(deps).get(key, key), source=before.source))
        if key == active:
            raise deps.refused(key, msg(said))


def after_create(deps: Deps, name: str) -> None:
    deps.catalog.reset(name)
    deps.tokens.forget(name)


def after_patch(deps: Deps, name: str, before: Instance, after: Instance) -> None:
    old, new = values_of(deps, before), values_of(deps, after)
    if old.get('url') != new.get('url'):
        deps.catalog.reset(name)
        deps.tokens.forget(name)
        return
    if (old.get('auth') == 'oauth' and new.get('auth') != 'oauth') or pem_text(old.get('ca')) != pem_text(new.get('ca')):
        deps.tokens.forget(name)
    if any(old.get(k) != new.get(k) for k in ACCOUNT_FIELDS):
        deps.catalog.bump_account(name)
    if old.get('preset') != new.get('preset'):
        deps.catalog.unconfirm(name, scope=True)
    elif old.get('knowledge') != new.get('knowledge'):
        deps.catalog.unconfirm(name)
    if old.get('preset') != new.get('preset') or old.get('knowledge') != new.get('knowledge'):
        deps.catalog.restrict(name, adapter_tools(new), denied(new))


def after_delete(deps: Deps, name: str) -> None:
    deps.catalog.forget(name)
    deps.tokens.forget(name)


def reconnect_text(inst: Instance, values: Mapping[str, str], state: str) -> str | None:
    return msg('mcptools.oauth.reconnect', instance=inst.name) if values.get('auth') == 'oauth' and state == 'reconnect' else None


def refused_text(name: str, doc: dict) -> str | None:
    found = doc.get('refused')
    if not isinstance(found, dict) or not found.get('text'):
        return None
    return msg('mcptools.access.call_notice' if found.get('call') else 'mcptools.access.notice', instance=name, text=found['text'])


def health(deps: Deps, inst: Instance) -> dict:
    values = values_of(deps, inst)
    held = reconnect_text(inst, values, deps.tokens.state(inst.name)) if values.get('auth') == 'oauth' else None
    text = held or refused_text(inst.name, deps.catalog.get(inst.name) or {})
    if text is not None:
        return {'state': 'error', 'text': text}
    if not deps.catalog.current(inst.name, knowledge_on(values), None, values.get('url') or ''):
        return {'state': 'warn', 'text': msg('mcptools.health.confirm')}
    return {'state': 'ok', 'text': msg('integrations.health.connected')}


def notices(deps: Deps) -> list[dict]:
    found, changed, refused = [], [], []
    for i in deps.instances.all(PLUGIN_ID):
        if not i.enabled:
            continue
        values, doc = values_of(deps, i), deps.catalog.get(i.name) or {}
        detail = signed_in(deps, i.name) if values.get('auth') == 'oauth' else {'state': 'none'}
        text, last = reconnect_text(i, values, detail['state']), detail.get('last')
        if text is not None:
            found.append({'id': f'mcp-reconnect:{i.name}', 'level': 'warn', 'text': text} | ({'at': last['at']} if isinstance(last, dict) and last.get('at') else {}))
        if any(r.get('changed') for r in doc.get('tools') or []):
            changed.append({'id': f'mcp-changed:{i.name}', 'level': 'info', 'text': msg('mcptools.tools.changed_notice', instance=i.name)})
        text = refused_text(i.name, doc)
        if text is not None:
            refused.append({'id': f'mcp-refused:{i.name}', 'level': 'warn', 'text': text, 'at': doc['refused'].get('at')})
    return found + changed + refused


def mount(app: FastAPI, deps: Deps, admin: Callable[..., str]) -> None:
    app.state.mcptools = deps
    By = Annotated[str, Depends(admin)]

    @app.get('/api/admin/mcp-tools/presets')
    def get_mcptools_presets(by: By) -> dict:
        found = presets.load()
        return {'items': [p.item() for p in found if not p.soon], 'soon': [{'id': p.id, 'name': p.name} for p in found if p.soon],
                'texts': {'preview': msg('mcptools.preset.preview'), 'client': msg('mcptools.preset.client'), 'knowledgeSoon': msg('mcptools.knowledge.soon'), 'soon': msg('mcptools.soon')}}

    @app.get('/api/admin/mcp-tools/instances/{name}')
    def get_mcptools_instance(name: str, by: By) -> dict:
        return view(deps, name)

    @app.post('/api/admin/mcp-tools/instances/{name}/tools/refresh')
    def post_mcptools_refresh(name: str, by: By) -> dict:
        _, values = instance_of(deps, name)
        if moved_away(deps, name, values):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.oauth.moved', instance=name))
        expected = deps.catalog.account_of(name)
        try:
            found = listing_of(deps, name, values, local_http(deps), LIST_DEADLINE_S)
        except NoScope as e:
            raise HTTPException(status.HTTP_409_CONFLICT, scrub(e.text, secrets_of(values, deps.tokens.secrets(name)))) from None
        except Reconnect:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.oauth.connect_first')) from None
        except McpFailure as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, scrub(e.text, secrets_of(values, deps.tokens.secrets(name)))) from None
        hidden = secrets_of(values, deps.tokens.secrets(name))
        try:
            deps.catalog.merge(name, found, by, adapter_tools(values), secrets=hidden, denied=denied(values), account=expected, url=values.get('url') or '')
        except StaleListing as e:
            raise HTTPException(status.HTTP_409_CONFLICT, e.text) from None
        return view(deps, name)

    @app.put('/api/admin/mcp-tools/instances/{name}/tools')
    def put_mcptools_tools(name: str, body: SwitchIn, by: By) -> dict:
        instance_of(deps, name)
        try:
            deps.catalog.switch(name, body.on, body.understood, by)
        except InvalidSwitch as e:
            raise deps.refused(e.field, e.text) from None
        except NeedsUnderstood as e:
            raise HTTPException(status.HTTP_409_CONFLICT, e.text) from None
        return view(deps, name)

    @app.get('/api/admin/mcp-tools/instances/{name}/folders')
    def get_mcptools_folders(name: str, by: By, q: Annotated[str, Query(max_length=Q_MAX)] = '') -> dict:
        _, values = instance_of(deps, name)
        if moved_away(deps, name, values):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.oauth.moved', instance=name))
        adapter = knowledge_adapter(values)
        if values.get('auth') == 'oauth' and deps.tokens.state(name) != 'connected':
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.oauth.connect_first'))
        heard: dict = {}
        try:
            found = folders_of(deps, name, values, adapter, q, heard)
        except NoScope as e:
            raise HTTPException(status.HTTP_409_CONFLICT, scrub(e.text, secrets_of(values, deps.tokens.secrets(name)))) from None
        except Reconnect:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.oauth.connect_first')) from None
        except McpFailure as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, list_failure(deps, name, values, adapter, heard, e)) from None
        except ReadRefused as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, read_refusal(adapter, values, e, secrets_of(values, deps.tokens.secrets(name)))) from None
        items = folder_items(found, secrets_of(values, deps.tokens.secrets(name)))
        deps.catalog.list_folders(name, adapter.id, items)
        return {'noun': adapter.noun, 'items': items}

    @app.put('/api/admin/mcp-tools/instances/{name}/scope')
    def put_mcptools_scope(name: str, body: ScopeIn, by: By) -> dict:
        _, values = instance_of(deps, name)
        adapter = knowledge_adapter(values)
        picked = deps.catalog.listed_folder(name, adapter.id, body.id)
        if picked is None:
            raise deps.refused('id', msg('mcptools.scope.pick_from_list'))
        deps.catalog.set_scope(name, picked, adapter.noun, by)
        deps.catalog.restrict(name, adapter_tools(values), denied(values))
        return view(deps, name)

    @app.post('/api/admin/mcp-tools/instances/{name}/confirm')
    def post_mcptools_confirm(name: str, body: ConfirmIn, by: By) -> dict:
        _, values = instance_of(deps, name)
        if body.understood is not True:
            raise deps.refused('understood', msg('mcptools.confirm.understood'))
        if body.version != warnings.WARNINGS_VERSION:
            raise deps.refused('version', msg('mcptools.confirm.version'))
        if not deps.catalog.listed(name, values.get('url') or ''):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.confirm.list_first'))
        if values.get('auth') == 'oauth' and deps.tokens.state(name) != 'connected':
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.oauth.connect_first'))
        preset = presets.get(values.get('preset'))
        if knowledge_on(values) and (preset is None or preset.knowledge != 'ready'):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.knowledge.no_adapter'))
        if knowledge_on(values) and scope_id(deps.catalog.get(name) or {}) is None:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mcptools.confirm.scope_first'))
        deps.catalog.confirm(name, body.version, by, values.get('url') or '')
        return view(deps, name)

    @app.post('/api/admin/mcp-tools/instances/{name}/oauth/start')
    def post_mcptools_oauth_start(name: str, by: By) -> dict:
        _, values = instance_of(deps, name)
        try:
            return {'url': flow(deps).start(name, values, local_http(deps), by)}
        except NotOAuth as e:
            raise deps.refused('auth', e.text) from None
        except FlowError as e:
            raise HTTPException(e.status, e.text) from None

    @app.get(CALLBACK_PATH)
    def get_mcptools_oauth_callback(by: By, code: str | None = None, state: str | None = None, error: str | None = None) -> RedirectResponse:
        signing = flow(deps)
        name, _ = signing.finish(code, state, error, by)
        return RedirectResponse(signing.landing(name), status.HTTP_303_SEE_OTHER)

    @app.delete('/api/admin/mcp-tools/instances/{name}/oauth', status_code=status.HTTP_204_NO_CONTENT)
    def delete_mcptools_oauth(name: str, by: By) -> Response:
        instance_of(deps, name)
        flow(deps).disconnect(name, by)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
