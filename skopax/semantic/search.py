import dataclasses
import datetime as dt
import logging
import re
from urllib.parse import urlsplit
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from pymongo import DESCENDING
from pymongo.errors import PyMongoError

from skopax import embedclient, history, information
from skopax.config import EMBED_MODEL
from skopax.embedclient import EmbedError
from skopax.errkind import Authored, kind as error_kind
from skopax.investigator.toolset import Run, asked_gate
from skopax.messages import msg
from skopax.kit import text as logtext
from skopax.redact import TIMEOUT_MARK, redact_text
from skopax.semantic import chunks, docscope, templates
from skopax.semantic.index import CANDIDATES, NotReady
from skopax.settle import ignored
from skopax.store import EMBED_EXCERPT_MAX, EMBED_KINDS, EMBED_TITLE_MAX, EMBED_URL_MAX, NOT_CHAT, NOT_REFUSED, REFUSED_BY, PAST_RUN_FIELDS, RUN_PAGE_FIELDS, iso, utc_now

log = logging.getLogger('skopax.semantic.search')

STATUS = 'embed-status'
ROLES = ('admin', 'operator', 'viewer', 'anonymous')
MODES = ('card', 'ask', 'ui')
PUBLIC_KINDS = ('run', 'card', 'open', 'change')
SIGNED_IN_KINDS = ('knowledge', 'doc', 'errtpl')
DEFAULT_LIMIT = 5
MAX_LIMIT = 20
QUERY_MAX = 500
MIN_SCORE = 0.80
MIN_SCORES = {EMBED_MODEL: 0.80}
SCORE_DIGITS = 2
EXCERPT = 300
TITLE = 120
FORBIDDEN = frozenset({'guard', 'held', 'vec', 'screenshots', 'secrets'})
OPEN_FIELDS = {'key': 1, 'svc': 1, 'label': 1, 'sev': 1, 'what': 1, 'status': 1, 'muted': 1, 'firstSeen': 1, 'lastSeen': 1, 'triage': 1, 'triagedBy': 1, 'owner': 1, 'assigned': 1, 'ticket': 1}
OPEN_SHOWN = ('key', 'num', 'svc', 'label', 'sev', 'what', 'triage', 'firstSeen', 'lastSeen')
UI_RUN_KEPT = ('mode', 'trigger', 'actionId', 'keys', 'startedAt', 'outcome', 'usd', 'recap', 'toolCalls.name', 'ask.text', 'ask.by')
UI_RUN_FIELDS = {k: 1 for k in UI_RUN_KEPT if k in RUN_PAGE_FIELDS or k.startswith('ask.')}
DEPLOY_SHOWN = ('ns', 'svc', 'fromImage', 'toImage', 'imageBy', 'tickets', 'at')
TEMPLATE_SHOWN = ('svc', 'level', 'template', 'firstSeen', 'lastSeen', 'peak', 'card')
REASONS = ('down', 'refused', 'mismatch', 'auth', 'bad_answer')


class SearchUnavailable(Authored, Exception):
    status_code = 503

    def __init__(self):
        super().__init__(msg('db.unavailable'))


@dataclass(frozen=True)
class SearchContext:
    role: str | None
    mode: str
    run: Run | None = None
    searched: Callable[[object], bool] | None = None

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f'unknown search mode {self.mode!r}')
        if self.mode != 'card' and self.role not in ROLES:
            object.__setattr__(self, 'role', 'anonymous')

    @classmethod
    def card_run(cls, run: Run | None = None, searched: Callable[[object], bool] | None = None) -> 'SearchContext':
        return cls(None, 'card', run, searched)

    @classmethod
    def asked_run(cls, run: Run, searched: Callable[[object], bool] | None = None) -> 'SearchContext':
        return cls(run.role if run.role in ROLES else 'viewer', 'ask', run, searched)

    @classmethod
    def ui(cls, role: str | None) -> 'SearchContext':
        return cls(role, 'ui')

    @property
    def anonymous(self) -> bool:
        return self.mode != 'card' and self.role == 'anonymous'


@dataclass(frozen=True)
class Hit:
    kind: str
    ref: str
    score: float | None
    row: dict


@dataclass(frozen=True)
class SearchResult:
    semantic: bool
    note: str | None = None
    rebuilding: dict | None = None
    items: list = field(default_factory=list)


def min_score(model: str) -> float:
    return MIN_SCORES.get(model, MIN_SCORE)


def gate_of(ctx: SearchContext) -> Callable[[object], bool] | None:
    if ctx.mode == 'card':
        return None
    base = ctx.run or Run(read_only=True, ask=True, report=False, chat=False, keys=False, asker=False, shadow=False)
    return asked_gate(dataclasses.replace(base, ask=True, review=None, role=ctx.role))


def allowed_kinds(ctx: SearchContext) -> tuple[str, ...]:
    return PUBLIC_KINDS if ctx.anonymous else PUBLIC_KINDS + SIGNED_IN_KINDS


def instance_open(ctx: SearchContext, serving: Callable[[Callable[[object], bool] | None], list], filters: object) -> bool:
    if ctx.anonymous or not isinstance(filters, dict):
        return False
    plugin_id, name = filters.get('plugin'), filters.get('instance')
    if not isinstance(plugin_id, str) or not plugin_id or not isinstance(name, str) or not name:
        return False
    return any(plugin.id == plugin_id and any(i.name == name for i in found) for plugin, found in serving(gate_of(ctx)))


def usable_knowledge(runtime, allowed: Callable[[object], bool] | None = None) -> Iterable[tuple[str, str, object]]:
    from skopax.runtime import held_knowledge
    for plugin, found in runtime.serving(allowed):
        if held_knowledge(plugin, found):
            continue
        for item in plugin.knowledge:
            yield plugin.id, '', item


def looks_like_log_line(text: str) -> bool:
    shown = text.strip()
    if logtext.TIMESTAMP.match(shown) or logtext.LOGFMT_MSG.search(shown):
        return True
    if shown.startswith('{') and logtext.message_of(shown) != shown:
        return True
    return bool(templates.LEVEL_WORD.search(shown) and any(p.search(shown) for p in (logtext.UUID, logtext.HEX, logtext.IP, logtext.LONG_NUMBER)))


def normalise_query(text: object) -> str:
    shown = str(text or '').strip()
    if not looks_like_log_line(shown):
        return shown
    return templates.template_of(shown) or shown


def parse_id(row_id: object) -> tuple[str, str] | None:
    if not isinstance(row_id, str):
        return None
    kind, sep, rest = row_id.partition(':')
    ref, sep_part, part = rest.rpartition(':')
    if not sep or not sep_part or kind not in EMBED_KINDS or not ref or not part.isdigit():
        return None
    return kind, ref


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in FORBIDDEN}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def never(_name) -> bool:
    return False


def words_regex(text: str) -> dict:
    return {'$regex': re.escape(text[:history.TEXT_QUERY_LIMIT]), '$options': 'i'}


def interleaved(groups: list[list], limit: int) -> list:
    out: list = []
    for n in range(max(map(len, groups), default=0)):
        for g in groups:
            if n < len(g) and len(out) < limit:
                out.append(g[n])
    return out


class Searcher:
    def __init__(self, store, settings: Callable[[], object], client, index, on: Callable[[], bool], knowledge: Callable[[Callable[[object], bool] | None], Iterable[tuple[str, str, object]]] | None = None,
                 serving: Callable[[Callable[[object], bool] | None], list] | None = None):
        self.store = store
        self.settings = settings
        self.client = client
        self.index = index
        self.on = on
        self.knowledge = knowledge or (lambda allowed: ())
        self.serving = serving or (lambda allowed: [])
        self.hydrators: dict[str, Callable[[SearchContext, list[str]], dict[str, dict]]] = {
            'run': self.hydrate_runs, 'card': self.hydrate_cards, 'open': self.hydrate_open, 'change': self.hydrate_changes, 'knowledge': self.hydrate_knowledge, 'doc': self.hydrate_docs, 'errtpl': self.hydrate_errtpl}

    def kinds_for(self, ctx: SearchContext, kinds: Iterable[str] | None) -> list[str]:
        allowed = [k for k in allowed_kinds(ctx) if k in self.hydrators]
        if kinds is None:
            return allowed
        wanted = {k for k in kinds if isinstance(k, str)}
        return [k for k in allowed if k in wanted]

    def instance_open(self, ctx: SearchContext, filters: object) -> bool:
        return instance_open(ctx, self.serving, filters)

    def search(self, ctx: SearchContext, text: str, kinds: Iterable[str] | None = None, limit: int = DEFAULT_LIMIT, service: str | None = None) -> SearchResult:
        query = str(text or '').strip()[:QUERY_MAX]
        if not query:
            raise ValueError('text is empty')
        limit = max(1, min(int(limit), MAX_LIMIT))
        chosen = self.kinds_for(ctx, kinds)
        service = str(service).strip() if service else None
        s = self.settings()
        try:
            why = self.unavailable(s)
            if why is not None:
                return self.fallback(ctx, query, chosen, limit, service, why)
            return self.semantic(ctx, s, query, chosen, limit, service)
        except PyMongoError as e:
            log.error('semantic search could not read the database: %s', error_kind(e))
            raise SearchUnavailable() from None

    def unavailable(self, s) -> str | None:
        if not self.on():
            return msg('embed.reason.disabled')
        if not str(s.embed_url or '').strip():
            return msg('embed.reason.no_url')
        shown = embedclient.status(s, getattr(self.client, 'breaker', None))
        if shown.get('up') is False:
            return shown.get('message') or msg('embed.down')
        return None

    def semantic(self, ctx: SearchContext, s, query: str, chosen: list[str], limit: int, service: str | None) -> SearchResult:
        try:
            vector = self.client.embed_queries([query])[0]
        except EmbedError as e:
            return self.fallback(ctx, query, chosen, limit, service, e.text)
        except Exception as e:
            log.warning('semantic search could not embed the query: %s', error_kind(e))
            return self.fallback(ctx, query, chosen, limit, service, msg('embed.down'))
        dims = len(vector)
        if dims != self.index.dims or embedclient.dims_of(s.embed_model) not in (None, dims):
            return self.fallback(ctx, query, chosen, limit, service, msg('embed.mismatch'))
        fp = chunks.fingerprint(s.embed_model, dims, s.passage_prefix)
        if fp is None:
            return self.fallback(ctx, query, chosen, limit, service, msg('embed.reason.not_ready'))
        try:
            self.index.ensure(self.store, fp)
        except PyMongoError:
            raise
        except NotReady:
            return self.fallback(ctx, query, chosen, limit, service, msg('embed.reason.not_ready'))
        except Exception as e:
            log.warning('semantic search could not load the index: %s', error_kind(e))
            return self.fallback(ctx, query, chosen, limit, service, msg('embed.reason.not_ready'))
        if not self.index.ready or self.index.fp != fp or self.index.size == 0:
            return self.fallback(ctx, query, chosen, limit, service, msg('embed.reason.not_ready'))
        rebuilding = self.rebuilding(fp)
        note = msg('embed.rebuilding', done=rebuilding['done'], total=rebuilding['of']) if rebuilding else None
        if not chosen:
            return SearchResult(True, note, rebuilding, [])
        try:
            found = self.candidates(ctx, vector, chosen, limit, service)
        except PyMongoError:
            raise
        except NotReady:
            return self.fallback(ctx, query, chosen, limit, service, msg('embed.reason.not_ready'))
        except ValueError:
            return self.fallback(ctx, query, chosen, limit, service, msg('embed.bad_answer'))
        except Exception as e:
            log.warning('semantic search failed in the index: %s', error_kind(e))
            return self.fallback(ctx, query, chosen, limit, service, msg('embed.down'))
        floor = min_score(s.embed_model)
        best: dict[tuple[str, str], float] = {}
        for row_id, score in found:
            parsed = parse_id(row_id)
            if parsed is None or parsed[0] not in chosen or score < floor:
                continue
            best[parsed] = max(score, best.get(parsed, score))
        ranked = sorted(best.items(), key=lambda t: (-t[1], t[0]))[:limit * CANDIDATES]
        return SearchResult(True, note, rebuilding, self.hydrate(ctx, ranked, limit))

    def candidates(self, ctx: SearchContext, vector: list[float], chosen: list[str], limit: int, service: str | None) -> list[tuple[str, float]]:
        base = {'svc': service} if service else {}
        groups = [([k for k in chosen if k != 'knowledge'], base)]
        if 'knowledge' in chosen:
            groups.append((['knowledge'], base | ({'slack': True} if ctx.mode == 'ask' else {})))
        found: list[tuple[str, float]] = []
        for kinds, filters in groups:
            if kinds:
                found.extend(self.index.search(vector, limit, kinds=kinds, **filters))
        return found

    def rebuilding(self, fp: str) -> dict | None:
        shown = self.store.embed_state(STATUS)
        mark = shown.get('rebuilding')
        if shown.get('fp') != fp or not isinstance(mark, dict):
            return None
        done, total = mark.get('done'), mark.get('of')
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (done, total)):
            return None
        return {'done': done, 'of': total}

    def hydrate(self, ctx: SearchContext, ranked: list[tuple[tuple[str, str], float]], limit: int) -> list[Hit]:
        by_kind: dict[str, list[str]] = {}
        for (kind, ref), _ in ranked:
            by_kind.setdefault(kind, []).append(ref)
        shown: dict[str, dict[str, dict]] = {}
        for kind, refs in by_kind.items():
            make = self.hydrators.get(kind)
            if make is None:
                continue
            try:
                shown[kind] = make(ctx, refs)
            except PyMongoError:
                raise
            except Exception as e:
                log.warning('semantic search dropped %s hits: %s', kind, error_kind(e))
        out: list[Hit] = []
        for (kind, ref), score in ranked:
            row = shown.get(kind, {}).get(ref)
            if row is not None and len(out) < limit:
                out.append(Hit(kind, ref, round(float(score), SCORE_DIGITS), scrub(row)))
        return out

    def built(self, docs: Iterable, build: Callable[[dict], tuple[str, dict] | None]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for d in docs:
            try:
                made = build(d)
            except PyMongoError:
                raise
            except Exception as e:
                log.warning('semantic search dropped one hit: %s', error_kind(e))
                continue
            if made is not None:
                out[made[0]] = made[1]
        return out

    def hydrate_runs(self, ctx: SearchContext, refs: list[str]) -> dict[str, dict]:
        from skopax.investigator import public_run
        from skopax.investigator.tools import past_run_row
        query = {'_id': {'$in': list(refs)}, 'outcome': 'done'} | NOT_CHAT | NOT_REFUSED
        investigator = ctx.mode != 'ui'
        docs = self.store.investigations.find(query, {**(PAST_RUN_FIELDS if investigator else UI_RUN_FIELDS), 'error': 1})

        def build(d: dict) -> tuple[str, dict] | None:
            if not d.get('trigger') or d.get('trigger') == 'chat' or d.get('error') == REFUSED_BY:
                return None
            if investigator:
                return d['_id'], past_run_row(d, ctx.searched or never)
            row = {('id' if k == '_id' else k): v for k, v in d.items() if k not in ('startedAtDt', 'error')}
            row['toolCalls'] = len(row.pop('toolCalls', None) or [])
            return d['_id'], public_run(row)

        return self.built(docs, build)

    def hydrate_cards(self, ctx: SearchContext, refs: list[str]) -> dict[str, dict]:
        store = self.store
        board = list(store.anomalies.find({'firstSeen': {'$exists': True}}, {'firstSeen': 1, 'sev': 1, 'owner': 1, 'assigned': 1, 'ticket': 1, 'triage': 1, 'triagedBy': 1}))
        live = [history.doc_id(d['_id'], d['firstSeen']) for d in board if d.get('firstSeen')]
        info_keys = [d['_id'] for d in board if information.is_item(d)]
        query = {'_id': {'$in': [r for r in refs if r not in live]}, 'key': {'$nin': info_keys}}
        docs = list(store.card_history.find(query))
        numbers = store.card_numbers([d['key'] for d in docs if d.get('key') and 'num' not in d]) if docs else {}

        def build(d: dict) -> tuple[str, dict] | None:
            if not d.get('key') or not d.get('firstSeen'):
                return None
            doc = d if 'num' in d or d['key'] not in numbers else {**d, 'num': numbers[d['key']]}
            row = history.closed_row(doc) if ctx.mode == 'ui' else history.digest(doc, 'semantic')
            return d['_id'], row

        return self.built(docs, build)

    def hydrate_open(self, ctx: SearchContext, refs: list[str]) -> dict[str, dict]:
        docs = list(self.store.anomalies.find({'_id': {'$in': list(refs)}, 'status': 'open'}, OPEN_FIELDS))
        numbers = self.store.card_numbers([d['_id'] for d in docs]) if docs else {}

        def build(d: dict) -> tuple[str, dict] | None:
            if d.get('muted') or not information.is_card(d):
                return None
            doc = {**d, 'key': d.get('key') or d['_id'], 'num': numbers.get(d['_id'])}
            row = {k: doc[k] for k in OPEN_SHOWN if doc.get(k) is not None}
            if row.get('what'):
                row['what'] = history.cut(row['what'], EXCERPT)
            return d['_id'], row

        return self.built(docs, build)

    def hydrate_changes(self, ctx: SearchContext, refs: list[str]) -> dict[str, dict]:
        deploys = [r[len('deploy:'):] for r in refs if r.startswith('deploy:')]
        reports = [r[len('report:'):] for r in refs if r.startswith('report:')]
        out: dict[str, dict] = {}
        if deploys:
            skip = ignored(self.store)

            def deploy(d: dict) -> tuple[str, dict]:
                row = {'type': 'deploy', 'id': d['_id']} | {k: d[k] for k in DEPLOY_SHOWN if d.get(k) is not None}
                return f"deploy:{d['_id']}", row | ({'ignored': True} if d.get('svc') in skip else {})

            out |= self.built(self.store.deploys.find({'_id': {'$in': deploys}}), deploy)
        if reports:
            def report(d: dict) -> tuple[str, dict]:
                brief = d.get('brief') if isinstance(d.get('brief'), dict) else {}
                row = {'type': 'report', 'id': d['_id'], 'at': d.get('createdAt'), 'summary': history.cut(d.get('summary'), EXCERPT), 'headline': history.cut(brief.get('headline'), TITLE)}
                return f"report:{d['_id']}", {k: v for k, v in row.items() if v}

            out |= self.built(self.store.deploy_reports.find({'_id': {'$in': reports}}), report)
        return out

    def hydrate_knowledge(self, ctx: SearchContext, refs: list[str]) -> dict[str, dict]:
        if ctx.anonymous:
            return {}
        live: dict[str, tuple[str, object]] = {}
        for plugin, _, item in self.knowledge(gate_of(ctx) if ctx.mode == 'ask' else None):
            live[f'{chunks.clean(plugin)}:{chunks.clean(getattr(item, "id", ""))}'] = (plugin, item)
        out: dict[str, dict] = {}
        for ref in refs:
            found = live.get(ref)
            if found is None:
                continue
            plugin, item = found
            if ctx.mode == 'ask' and getattr(item, 'slack', False) is not True:
                continue
            out[ref] = {'plugin': plugin, 'id': item.id, 'title': history.cut(item.title, TITLE), 'excerpt': history.cut(item.text, EXCERPT)}
        return out

    def hydrate_docs(self, ctx: SearchContext, refs: list[str]) -> dict[str, dict]:
        if ctx.anonymous or 'docs' not in self.settings().embed_sources:
            return {}
        live = {(plugin.id, i.name): (plugin, i) for plugin, found in self.serving(None) for i in found}
        first: dict[str, dict] = {}
        for d in self.store.embeddings.find({'kind': 'doc', 'ref': {'$in': list(refs)}}, {'ref': 1, 'part': 1, 'f': 1, 'title': 1, 'url': 1, 'excerpt': 1}).sort('part', 1):
            first.setdefault(d['ref'], d)
        opened: dict[tuple[str, str], bool] = {}

        def build(d: dict) -> tuple[str, dict] | None:
            f = d.get('f') or {}
            where = (f.get('plugin'), f.get('instance'))
            found = live.get(where)
            if found is None or not docscope.index_on(found[1].values):
                return None
            if not docscope.allowed(docscope.spaces_of(found[0], found[1].values), f.get('space')):
                return None
            if where not in opened:
                opened[where] = self.instance_open(ctx, {'plugin': where[0], 'instance': where[1]})
            if not opened[where]:
                return None
            row = {'kind': 'doc', 'plugin': where[0], 'instance': where[1], 'id': d['ref']}
            title, excerpt, link = (redact_text(str(d.get(k) or '')) for k in ('title', 'excerpt', 'url'))
            if TIMEOUT_MARK in (title, excerpt, link):
                return None
            row['title'], row['excerpt'] = title[:EMBED_TITLE_MAX], excerpt[:EMBED_EXCERPT_MAX]
            row['url'] = link if link and urlsplit(link).scheme in ('http', 'https') and len(link) <= EMBED_URL_MAX and link == str(d.get('url') or '') else None
            return d['ref'], row

        return self.built(first.values(), build)

    def hydrate_errtpl(self, ctx: SearchContext, refs: list[str]) -> dict[str, dict]:
        if ctx.anonymous:
            return {}
        docs = self.store.log_templates.find({'_id': {'$in': list(refs)}})
        opened: dict[tuple[str, str], bool] = {}
        now = utc_now()

        def build(d: dict) -> tuple[str, dict] | None:
            until = d.get('expireAt')
            if isinstance(until, dt.datetime) and (until if until.tzinfo else until.replace(tzinfo=dt.timezone.utc)) <= now:
                return None
            where = (d.get('plugin'), d.get('instance'))
            if where not in opened:
                opened[where] = self.instance_open(ctx, {'plugin': where[0], 'instance': where[1]})
            if not opened[where]:
                return None
            row = {'kind': 'errtpl'} | {k: d[k] for k in TEMPLATE_SHOWN if d.get(k) is not None}
            row['template'] = redact_text(row.get('template') or '', templates.TEMPLATE_LIMIT)
            return d['_id'], row

        return self.built(docs, build)

    def fallback(self, ctx: SearchContext, query: str, chosen: list[str], limit: int, service: str | None, reason: str) -> SearchResult:
        note = msg('embed.off', reason=reason)
        if ctx.mode != 'ui':
            return SearchResult(False, note, None, [])
        groups: list[list[Hit]] = []
        if 'card' in chosen:
            groups.append(self.word_cards(ctx, query, limit, service))
        if 'run' in chosen:
            groups.append(self.word_runs(ctx, query, limit, service))
        return SearchResult(False, note, None, interleaved(groups, limit))

    def word_cards(self, ctx: SearchContext, query: str, limit: int, service: str | None) -> list[Hit]:
        now = utc_now()
        rows, _ = self.store.closed_cards(iso(now - dt.timedelta(days=history.CLOSED_DAYS_MAX)), iso(now), query, None, 0, limit if not service else history.CLOSED_PAGE_MAX)
        hits = []
        for row in rows:
            if service and row.get('svc') != service:
                continue
            if row.get('key') and row.get('firstSeen'):
                hits.append(Hit('card', history.doc_id(row['key'], row['firstSeen']), None, scrub(row)))
        return hits[:limit]

    def word_runs(self, ctx: SearchContext, query: str, limit: int, service: str | None) -> list[Hit]:
        match: dict = {'outcome': 'done', '$or': [{'ask.text': words_regex(query)}, {'recap': words_regex(query)}]}
        if service:
            match['keys'] = {'$regex': f'(^|[|/]){re.escape(service[:history.TEXT_QUERY_LIMIT])}(\\||$)'}
        found = self.store.investigations.find(match | NOT_CHAT | NOT_REFUSED, {'_id': 1}, sort=[('startedAt', DESCENDING), ('_id', DESCENDING)], limit=limit)
        ids = [d['_id'] for d in found]
        shown = self.hydrate_runs(ctx, ids) if ids else {}
        return [Hit('run', i, None, scrub(shown[i])) for i in ids if i in shown]
