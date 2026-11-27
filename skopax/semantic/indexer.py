import datetime as dt
import hashlib
import json
import logging
import threading
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Callable, Iterable

from pymongo import DESCENDING
from pymongo.errors import PyMongoError

from skopax import embedclient, history
from skopax.contract import Corpus, ReadError, Truncated
from skopax.embedclient import EmbedError
from skopax.errkind import kind as error_kind
from skopax.semantic import chunks, docscope
from skopax.semantic.chunks import Passage, Tally
from skopax.semantic.index import pack_vector
from skopax.runtime import held_knowledge
from skopax.store import EMBED_EXCERPT_MAX, EMBED_KINDS, EMBED_TITLE_MAX, EMBED_URL_MAX, NOT_CHAT, NOT_REFUSED, REFUSED_BY, Store

log = logging.getLogger('skopax.semantic')

NAME = 'embed-index'
PERIOD = 60
OFFSET = 45
QUEUE_LIMIT = 200
GROUP = 8
PAGE = 25
LIVE_LIMIT = 400
RECONCILE_S = 6 * 3600
ORPHAN_PAGES = 4
EMPTY_PASSES = 5
DEADLINE_S = 45.0
MARKS = 'embed-marks'
STATUS = 'embed-status'
SOURCE_OF = {'run': 'runs', 'card': 'cards', 'open': 'cards', 'change': 'changes', 'knowledge': 'knowledge', 'doc': 'docs', 'errtpl': 'logs'}
RUN_FIELDS = {'error': 1, 'ask.text': 1, 'recap': 1, 'keys': 1, 'svc': 1, 'trigger': 1, 'startedAt': 1, 'startedAtDt': 1, 'outcome': 1}
CARD_SKIP = {'events': 0, 'asks': 0, 'said': 0, 'runs': 0}
REPORT_FIELDS = {'summary': 1, 'brief': 1, 'createdAt': 1}
OPEN_FIELDS = {'key': 1, 'svc': 1, 'label': 1, 'what': 1, 'note': 1, 'sev': 1, 'status': 1, 'muted': 1, 'at': 1, 'lastSeen': 1, 'firstSeen': 1}
FAR = '\U0010ffff'
DOCS_CRAWL_S = 6 * 3600
DOCS_FULL_S = 24 * 3600
DOCS_DOWN_S = 14 * 86400
DOCS_RETRY_S = 900
DOCS_ATTEMPTS = 3
DOCS_LOOKUP = 200
DOCS_GROUP = 8
DOC_EXCERPT_SCAN = 2000


@dataclass(frozen=True)
class Source:
    name: str
    kind: str
    setting: str
    prefix: str
    sort: str
    query: dict
    fields: dict | None


SOURCES = (
    Source('run', 'run', 'runs', '', 'startedAt', {'outcome': 'done'} | NOT_CHAT | NOT_REFUSED, RUN_FIELDS),
    Source('card', 'card', 'cards', '', 'at', {}, CARD_SKIP),
    Source('deploy', 'change', 'changes', 'deploy:', 'at', {}, None),
    Source('report', 'change', 'changes', 'report:', 'createdAt', {}, REPORT_FIELDS),
    Source('errtpl', 'errtpl', 'logs', '', 'lastSeen', {}, None),
)
BY_NAME = {s.name: s for s in SOURCES}


@dataclass(frozen=True)
class DocSource:
    plugin: str
    instance: str
    corpus: Corpus
    on: bool
    spaces: tuple[str, ...] | None
    context: Callable[[float], object]


@dataclass
class Ctx:
    s: object
    fp: str
    dims: int
    now: float
    deadline: float
    embedded: int = 0
    called: bool = False
    pages: int = 0
    tally: Tally = field(default_factory=Tally)

    def late(self) -> bool:
        return time.monotonic() >= self.deadline


Item = tuple[str, str, list[Passage], dt.datetime | None]


def current(rows: list[dict], fp: str, passages: list[Passage]) -> bool:
    if len(rows) != len(passages):
        return False
    held = {r['part']: r for r in rows}
    return all(p.part in held and held[p.part]['hash'] == p.hash and held[p.part]['fp'] == fp for p in passages)


def expiry(value) -> dt.datetime | None:
    if not isinstance(value, dt.datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)


def after(value, days: int) -> dt.datetime | None:
    when = chunks.moment(value)
    return when + dt.timedelta(days=days) if when is not None else None


class Indexer:
    def __init__(self, store: Store, settings: Callable[[], object], client, on: Callable[[], bool], knowledge: Callable[[], Iterable[tuple[str, str, object]]] | None = None,
                 docs: Callable[[], Iterable[DocSource]] | None = None):
        self.store = store
        self.settings = settings
        self.client = client
        self.on = on
        self.knowledge = knowledge
        self.docs = docs
        self.extras: dict[tuple[str, str], dict] = {}
        self.lock = threading.Lock()
        self.sigs: dict[tuple[str, str], tuple[str, str, bool]] = {}
        self.empty_passes = 0

    def enabled(self) -> bool:
        try:
            s = self.settings()
            return bool(self.on()) and bool(s.embed_url.strip()) and bool(self.sources(s))
        except Exception:
            return False

    def active(self) -> bool:
        if self.enabled():
            return True
        try:
            shown = self.store.embed_state(STATUS)
            return bool(shown) and shown.get('kind') != 'off'
        except Exception:
            return False

    def wants(self, kind: str) -> bool:
        try:
            s = self.settings()
            return bool(self.on()) and bool(s.embed_url.strip()) and self.kind_on(s, kind)
        except Exception:
            return True

    def sources(self, s) -> list[str]:
        return [name for name in ('runs', 'cards', 'changes', 'knowledge', 'docs', 'logs') if name in s.embed_sources]

    def kind_on(self, s, kind: str) -> bool:
        return SOURCE_OF.get(kind) in s.embed_sources

    def run_once(self, now: float | None = None) -> dict | None:
        at = time.time() if now is None else now
        if not self.enabled():
            self.note_off(at)
            return None
        if not self.lock.acquire(blocking=False):
            return None
        try:
            return self.pass_(self.settings(), at)
        except Exception as e:
            log.warning('embed-index pass failed: %s', error_kind(e))
            return None
        finally:
            self.lock.release()

    def note_off(self, now: float) -> None:
        try:
            if self.store.embed_state(STATUS).get('kind') != 'off':
                self.store.save_embed_state(STATUS, {'at': dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(), 'up': None, 'kind': 'off', 'counts': {}, 'backfill': {}})
        except Exception as e:
            log.warning('embed-index status not saved: %s', error_kind(e))

    def pass_(self, s, now: float) -> dict | None:
        ctx = Ctx(s, '', 0, now, time.monotonic() + DEADLINE_S, pages=max(0, int(s.embed_docs_per_pass)))
        failure: str | None = None
        marks: dict = {}
        version = 0
        try:
            self.learn(ctx)
            marks = self.marks(ctx)
            version = int(marks.get('ver') or 0)
            self.drain(ctx)
            self.live(ctx, marks)
            self.crawl(ctx, marks)
            self.backfill(ctx, marks)
            self.finish_rebuild(ctx, marks)
        except EmbedError as e:
            failure = e.kind
            log.warning('embed-index paused: %s', e.kind)
        except Exception as e:
            failure = 'error'
            log.warning('embed-index pass failed: %s', error_kind(e))
        if marks:
            try:
                if not self.store.save_embed_marks(marks, version):
                    log.info('embed-index marks were moved by another pod; this pass keeps nothing')
            except Exception as e:
                log.warning('embed-index marks not saved: %s', error_kind(e))
        return self.status(ctx, marks, failure)

    def learn(self, ctx: Ctx) -> None:
        s = ctx.s
        fp = chunks.fingerprint(s.embed_model, embedclient.dims_of(s.embed_model), s.passage_prefix)
        if fp is None:
            self.client.embed_passages(['ok'])
            ctx.called = True
            fp = chunks.fingerprint(s.embed_model, embedclient.dims_of(s.embed_model), s.passage_prefix)
        if fp is None:
            raise EmbedError('bad_answer', counts=False)
        ctx.fp, ctx.dims = fp, int(embedclient.dims_of(s.embed_model))

    def marks(self, ctx: Ctx) -> dict:
        saved = self.store.embed_state(MARKS)
        if saved.get('fp') == ctx.fp:
            saved.setdefault('cursors', {})
            return saved
        marks: dict = {'fp': ctx.fp, 'cursors': {}, 'open': False, 'knowledge': False, 'docs_ok': False, 'docs': saved.get('docs') or {}, 'ver': saved.get('ver') or 0}
        if self.store.embedding_count(other_than=ctx.fp) > 0:
            marks['rebuild'] = {'of': self.store.embedding_count()}
            log.info('embed-index rebuilds for a new fingerprint')
        self.sigs.clear()
        status = self.store.embed_state(STATUS)
        self.store.save_embed_state(STATUS, {**status, 'fp': ctx.fp})
        return marks

    def build(self, src: Source, doc: dict, tally: Tally) -> tuple[list[Passage], dt.datetime | None]:
        store = self.store
        if src.name == 'run':
            return chunks.run_passages(doc, tally), after(doc.get('startedAtDt') or doc.get('startedAt'), store.investigate_days)
        if src.name == 'card':
            merged = dict(doc)
            if merged.get('outcome'):
                merged['outcomeText'] = history.outcome_text(merged.get('outcome'), merged.get('resolution'))
            live = store.anomalies.find_one({'_id': merged.get('key')}, {'what': 1, 'firstSeen': 1}) if merged.get('key') else None
            if live and live.get('what') and live.get('firstSeen') == merged.get('firstSeen'):
                merged['what'] = live['what']
            return chunks.card_passages(merged, tally), after(doc.get('atDt') or doc.get('at'), store.history_days)
        if src.name == 'errtpl':
            ref = str(doc.get('_id') or '')
            return (chunks.errtpl_passages(ref, doc.get('svc') or '', doc.get('level') or '', doc.get('template') or '', doc.get('plugin') or '', doc.get('instance') or '', tally, doc.get('lastSeen')),
                    expiry(doc.get('expireAt')))
        if src.name == 'deploy':
            return chunks.deploy_passages(doc, tally), None
        return chunks.report_passages({**doc, 'at': doc.get('createdAt')}, tally), None

    def source_of(self, kind: str, ref: str) -> Source | None:
        if kind in ('run', 'card', 'errtpl'):
            return BY_NAME[kind]
        if kind == 'change':
            return next((x for x in SOURCES if x.kind == 'change' and ref.startswith(x.prefix)), None)
        return None

    def coll(self, src: Source):
        return {'run': self.store.investigations, 'card': self.store.card_history, 'deploy': self.store.deploys, 'report': self.store.deploy_reports, 'errtpl': self.store.log_templates}[src.name]

    def load(self, kind: str, ref: str, tally: Tally) -> tuple[list[Passage], dt.datetime | None]:
        if kind == 'open':
            doc = self.store.anomalies.find_one({'_id': ref, 'status': 'open'}, OPEN_FIELDS)
            return (chunks.open_passages(doc, tally), None) if doc else ([], None)
        src = self.source_of(kind, ref)
        if src is None:
            return [], None
        doc = self.coll(src).find_one({'_id': ref[len(src.prefix):]}, src.fields)
        if doc is None or not self.eligible(src, doc):
            return [], None
        return self.build(src, doc, tally)

    def eligible(self, src: Source, doc: dict) -> bool:
        if src.name == 'errtpl':
            until = expiry(doc.get('expireAt'))
            return until is not None and until > dt.datetime.now(dt.timezone.utc)
        if src.name == 'run':
            return doc.get('outcome') == 'done' and doc.get('trigger') not in (None, 'chat') and doc.get('error') != REFUSED_BY
        return True

    def commit(self, ctx: Ctx, items: list[Item]) -> int:
        known: dict[str, bytes] = {}
        need: dict[str, str] = {}
        for _, _, passages, _ in items:
            for p in passages:
                if p.hash in known or p.hash in need:
                    continue
                held = self.store.embedding_by_hash(ctx.fp, p.hash)
                if held is not None and len(held) == ctx.dims * 2:
                    known[p.hash] = held
                else:
                    need[p.hash] = p.text
        pairs = list(need.items())
        size = max(1, int(ctx.s.embed_batch))
        for at in range(0, len(pairs), size):
            if ctx.late():
                break
            batch = pairs[at:at + size]
            ctx.called = True
            found = self.client.embed_passages([text for _, text in batch])
            if len(found) != len(batch):
                raise EmbedError('bad_answer', counts=False)
            for (digest, _), vector in zip(batch, found):
                known[digest] = pack_vector(vector, ctx.dims)
            ctx.embedded += len(batch)
        done = 0
        for kind, ref, passages, expire in items:
            if any(p.hash not in known for p in passages):
                break
            docs = [{'kind': kind, 'ref': ref, 'part': p.part, 'fp': ctx.fp, 'hash': p.hash, 'vec': known[p.hash], 'f': p.f, **({'at': p.at} if p.at else {}),
                     **({'expireAt': expire} if expire else {}), **(self.extras.get((kind, ref), {}) if p.part == 0 else {})} for p in passages]
            if docs:
                self.store.upsert_embeddings(docs, ctx.dims)
            self.store.delete_embeddings(kind, ref, from_part=len(passages))
            done += 1
        return done

    def drain(self, ctx: Ctx) -> None:
        kinds = [k for k in EMBED_KINDS if self.kind_on(ctx.s, k)]
        ops = self.store.drain_embed_queue(QUEUE_LIMIT, kinds)
        settled: list[dict] = []
        try:
            for at in range(0, len(ops), GROUP):
                if ctx.late():
                    break
                group = ops[at:at + GROUP]
                items: list[Item] = []
                owners: list[dict] = []
                free: list[dict] = []
                for op in group:
                    kind, ref = op['kind'], op['ref']
                    if op['op'] == 'delete':
                        self.store.delete_embeddings(kind, ref)
                        free.append(op)
                        continue
                    passages, expire = self.load(kind, ref, ctx.tally)
                    items.append((kind, ref, passages, expire))
                    owners.append(op)
                done = self.commit(ctx, items) if items else 0
                finished = free + owners[:done]
                self.store.ack_embed_queue(finished)
                settled.extend(finished)
                if done < len(items):
                    break
        finally:
            left = [op for op in ops if op not in settled]
            if left:
                self.store.release_embed_queue(left)

    def live(self, ctx: Ctx, marks: dict) -> None:
        room = LIVE_LIMIT
        if self.kind_on(ctx.s, 'open'):
            room -= self.sync_open(ctx, room, marks)
        if self.kind_on(ctx.s, 'knowledge') and self.knowledge is not None:
            self.sync_knowledge(ctx, room, marks)

    def crawl(self, ctx: Ctx, marks: dict) -> None:
        if self.docs is None:
            return
        if not self.kind_on(ctx.s, 'doc'):
            if self.store.embeddings.find_one({'kind': 'doc'}, {'_id': 1}) is not None:
                self.purge_all_docs()
            marks['docs'] = {}
            marks['docs_ok'] = True
            return
        try:
            found = list(self.docs())
        except Exception as e:
            marks['docs_ok'] = False
            log.warning('embed-index docs sources unreadable: %s', error_kind(e))
            return
        state = marks.setdefault('docs', {})
        by = state.setdefault('by', {})
        live = {(d.plugin, d.instance): d for d in found if d.on}
        sig = hashlib.sha256(json.dumps(sorted([p, n, list(d.spaces) if d.spaces is not None else None] for (p, n), d in live.items())).encode('utf-8')).hexdigest()
        if state.get('sig') != sig or ctx.now - float(state.get('swept') or 0) >= DOCS_CRAWL_S:
            self.sweep(live)
            state['sig'], state['swept'] = sig, ctx.now
        for key in [k for k in by if k not in {f'{p}:{n}' for p, n in live}]:
            del by[key]
        done = True
        try:
            for (plugin, instance), d in live.items():
                entry = by.setdefault(f'{plugin}:{instance}', {'ok': ctx.now})
                try:
                    self.crawl_one(ctx, d, entry)
                except (EmbedError, PyMongoError):
                    raise
                except Exception as e:
                    log.warning('embed-index docs of %s %s failed: %s', plugin, instance, error_kind(e))
                done = done and (bool(entry.get('failed')) or (entry.get('fp') == ctx.fp and not entry.get('todo')))
        finally:
            self.extras.clear()
        marks['docs_ok'] = done or int(ctx.s.embed_docs_per_pass) <= 0

    def purge_all_docs(self) -> None:
        refs = {row['ref'] for row in self.store.embeddings.find({'kind': 'doc'}, {'ref': 1})}
        for ref in refs:
            self.store.delete_embeddings('doc', ref)

    def instance_refs(self, plugin: str, instance: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for row in self.store.embeddings.find({'kind': 'doc', 'f.plugin': plugin, 'f.instance': instance}, {'ref': 1, 'f.space': 1}):
            out[row['ref']] = (row.get('f') or {}).get('space') or ''
        return out

    def sweep(self, live: dict) -> None:
        gone: dict[str, None] = {}
        for row in self.store.embeddings.find({'kind': 'doc'}, {'ref': 1, 'f.plugin': 1, 'f.instance': 1, 'f.space': 1}):
            f = row.get('f') or {}
            d = live.get((f.get('plugin'), f.get('instance')))
            if d is None or not docscope.allowed(d.spaces, f.get('space')):
                gone[row['ref']] = None
        for ref in gone:
            self.store.delete_embeddings('doc', ref)

    def purge_instance(self, plugin: str, instance: str) -> None:
        for ref in self.instance_refs(plugin, instance):
            self.store.delete_embeddings('doc', ref)

    def crawl_one(self, ctx: Ctx, d: DocSource, entry: dict) -> None:
        c = d.context(ctx.now)
        due = not entry.get('todo') and ctx.now - float(entry.get('try') or 0) >= DOCS_RETRY_S and (entry.get('fp') != ctx.fp or ctx.now - float(entry.get('crawl') or 0) >= DOCS_CRAWL_S)
        if due and ctx.pages <= 0:
            entry['ok'] = ctx.now
        if due and ctx.pages > 0:
            if not self.listed(ctx, d, c, entry):
                return
        self.read_docs(ctx, d, c, entry)

    def listed(self, ctx: Ctx, d: DocSource, c, entry: dict) -> bool:
        full = entry.get('fp') != ctx.fp or entry.get('full') is None or ctx.now - float(entry['full']) >= DOCS_FULL_S
        since = None if full else entry.get('since')
        refs: list = []
        cut = False
        try:
            for ref in d.corpus.list(c, since):
                if len(refs) >= d.corpus.max_pages:
                    cut = True
                    break
                refs.append(ref)
        except Truncated:
            cut = True
        except Exception as e:
            log.warning('embed-index docs listing of %s %s failed: %s', d.plugin, d.instance, error_kind(e))
            entry['try'] = ctx.now
            entry['failed'] = True
            if ctx.now - float(entry.get('ok') or ctx.now) >= DOCS_DOWN_S:
                self.purge_instance(d.plugin, d.instance)
                entry.clear()
                entry.update({'ok': ctx.now, 'try': ctx.now, 'failed': True})
            return False
        kept = [r for r in refs if getattr(r, 'id', '') and docscope.allowed(d.spaces, getattr(r, 'space', ''))]
        prefix = f'{d.plugin}:{d.instance}:'
        if full and not cut:
            listed = {f'{prefix}{r.id}' for r in kept}
            for ref in self.instance_refs(d.plugin, d.instance):
                if ref not in listed:
                    self.store.delete_embeddings('doc', ref)
        entry.update({'failed': False, 'ok': ctx.now, 'crawl': ctx.now, 'since': dt.datetime.fromtimestamp(ctx.now, dt.timezone.utc).isoformat(), 'fp': ctx.fp})
        if full:
            entry['full'] = ctx.now
        entry['todo'] = [[str(r.id), r.modified if isinstance(r.modified, str) else None, str(r.space or ''), 0] for r in self.changed(ctx, prefix, kept)]
        return True

    def changed(self, ctx: Ctx, prefix: str, refs: list) -> list:
        held: dict[str, list[dict]] = {}
        for at in range(0, len(refs), DOCS_LOOKUP):
            names = [f'{prefix}{r.id}' for r in refs[at:at + DOCS_LOOKUP]]
            for row in self.store.embeddings.find({'kind': 'doc', 'ref': {'$in': names}}, {'ref': 1, 'fp': 1, 'at': 1}):
                held.setdefault(row['ref'], []).append(row)
        out = []
        for r in refs:
            rows = held.get(f'{prefix}{r.id}', [])
            when = chunks.moment(r.modified)
            if not rows or when is None or any(x.get('fp') != ctx.fp or chunks.moment(x.get('at')) != when for x in rows):
                out.append(r)
        return out

    def shown(self, text) -> dict:
        title = chunks.masked(chunks.clean(getattr(text, 'title', ''))) or ''
        body = chunks.masked(chunks.clean(getattr(text, 'text', ''))[:DOC_EXCERPT_SCAN]) or ''
        link = chunks.clean(getattr(text, 'url', ''))
        out = {'title': title[:EMBED_TITLE_MAX], 'excerpt': ' '.join(body.split())[:EMBED_EXCERPT_MAX]}
        if link and urlsplit(link).scheme in ('http', 'https') and len(link) <= EMBED_URL_MAX and (chunks.masked(link) or '').strip() == link:
            out['url'] = link
        return out

    def read_docs(self, ctx: Ctx, d: DocSource, c, entry: dict) -> None:
        todo = entry.get('todo') or []
        prefix = f'{d.plugin}:{d.instance}:'
        deferred: set[str] = set()
        while ctx.pages > 0 and not ctx.late():
            ready = [t for t in todo if t[0] not in deferred]
            if not ready:
                break
            items: list[Item] = []
            removed: set[str] = set()
            retry: list[list] = []
            for held in ready[:min(DOCS_GROUP, ctx.pages)]:
                ident, modified, space = held[0], held[1], held[2]
                tries = int(held[3]) if len(held) > 3 else 0
                ref = f'{prefix}{ident}'
                ctx.pages -= 1
                try:
                    text = d.corpus.read(c, ident)
                except Exception as e:
                    status = getattr(e, 'status', None) if isinstance(e, ReadError) else None
                    removed.add(ident)
                    if isinstance(status, int) and 400 <= status < 500 and status != 429:
                        self.store.delete_embeddings('doc', ref)
                    elif tries + 1 < DOCS_ATTEMPTS:
                        retry.append([ident, modified, space, tries + 1])
                        deferred.add(ident)
                    log.warning('embed-index docs read of %s %s failed: %s', d.plugin, d.instance, error_kind(e))
                    continue
                self.extras[('doc', ref)] = self.shown(text)
                items.append(('doc', ref, chunks.doc_passages(ref, getattr(text, 'title', ''), getattr(text, 'text', ''), d.plugin, d.instance, space, ctx.tally, modified), None))
            done = self.commit(ctx, items) if items else 0
            removed.update(item[1][len(prefix):] for item in items[:done])
            todo = [t for t in todo if t[0] not in removed] + retry
            if done < len(items):
                break
        entry['todo'] = todo

    def sync_set(self, ctx: Ctx, kind: str, entries: dict[str, tuple[str, Callable[[], list[Passage]]]], room: int, prune: bool = True) -> tuple[int, bool]:
        have = self.store.embedding_rows(kind)
        if prune:
            for ref in [r for r in have if r not in entries]:
                self.store.delete_embeddings(kind, ref)
        items: list[Item] = []
        marks: list[tuple[str, str, bool]] = []
        spent = 0
        complete = True
        for ref, (sig, build) in entries.items():
            held = self.sigs.get((kind, ref))
            if held is not None and held[0] == sig and held[1] == ctx.fp and (held[2] or ref in have):
                continue
            passages = build()
            if not passages:
                if ref in have:
                    self.store.delete_embeddings(kind, ref)
                self.sigs[(kind, ref)] = (sig, ctx.fp, True)
                continue
            if current(have.get(ref, []), ctx.fp, passages):
                self.sigs[(kind, ref)] = (sig, ctx.fp, False)
                continue
            if (items and spent + len(passages) > room) or ctx.late():
                complete = False
                break
            items.append((kind, ref, passages, None))
            marks.append((ref, sig, False))
            spent += len(passages)
        if items:
            done = self.commit(ctx, items)
            for ref, sig, empty in marks[:done]:
                self.sigs[(kind, ref)] = (sig, ctx.fp, empty)
            complete = complete and done == len(items)
        return spent, complete

    def sync_open(self, ctx: Ctx, room: int, marks: dict) -> int:
        entries: dict[str, tuple[str, Callable[[], list[Passage]]]] = {}
        for doc in self.store.anomalies.find({'status': 'open'}, OPEN_FIELDS):
            ref = doc.get('key') or doc['_id']
            sig = hashlib.sha256(json.dumps({k: doc.get(k) for k in OPEN_FIELDS}, sort_keys=True, default=str).encode('utf-8')).hexdigest()
            entries[ref] = (sig, lambda d=doc: chunks.open_passages(d, ctx.tally))
        spent, complete = self.sync_set(ctx, 'open', entries, room)
        marks['open'] = complete
        return spent

    def sync_knowledge(self, ctx: Ctx, room: int, marks: dict) -> None:
        entries: dict[str, tuple[str, Callable[[], list[Passage]]]] = {}
        for plugin, instance, item in self.knowledge():
            ref = f'{chunks.clean(plugin)}:{chunks.clean(getattr(item, "id", ""))}'
            sig = hashlib.sha256(json.dumps([plugin, instance, getattr(item, 'id', ''), getattr(item, 'title', ''), getattr(item, 'text', ''), bool(getattr(item, 'slack', False))]).encode('utf-8')).hexdigest()
            entries[ref] = (sig, lambda p=plugin, i=instance, k=item: chunks.knowledge_passages(p, k, i, ctx.tally))
        if not entries and self.store.embedding_rows('knowledge'):
            self.empty_passes += 1
            if self.empty_passes < EMPTY_PASSES:
                marks['knowledge'] = False
                return
        else:
            self.empty_passes = 0
        _, complete = self.sync_set(ctx, 'knowledge', entries, room)
        marks['knowledge'] = complete

    def cursor(self, marks: dict, src: Source, now: float) -> dict:
        cursors = marks['cursors']
        mark = cursors.get(src.name)
        if mark is None:
            mark = cursors[src.name] = {'phase': 'scan'}
        if mark['phase'] == 'done' and now - float(mark.get('doneAt') or 0) >= RECONCILE_S:
            mark = cursors[src.name] = {'phase': 'scan'}
        return mark

    def backfill(self, ctx: Ctx, marks: dict) -> None:
        room = max(0, int(ctx.s.embed_backfill_per_pass))
        active = [x for x in SOURCES if x.setting in ctx.s.embed_sources]
        for src in active:
            self.cursor(marks, src, ctx.now)
        spent = 0
        pages = ORPHAN_PAGES
        for src in active:
            mark = marks['cursors'][src.name]
            while mark['phase'] != 'done' and not ctx.late():
                if mark['phase'] in ('scan', 'missing'):
                    if room - spent <= 0:
                        break
                    used, moved = self.scan(ctx, src, mark, room - spent, spent > 0)
                    spent += used
                    if not moved:
                        break
                else:
                    if pages <= 0:
                        break
                    pages -= 1
                    self.orphans(ctx, src, mark)

    def scan(self, ctx: Ctx, src: Source, mark: dict, room: int, strict: bool) -> tuple[int, bool]:
        query = dict(src.query)
        missing = mark['phase'] == 'missing'
        if missing:
            query[src.sort] = None
            if 'id' in mark:
                query['_id'] = {'$lt': mark['id']}
            order = [('_id', DESCENDING)]
        else:
            if 'k' in mark:
                query['$or'] = [{src.sort: {'$lt': mark['k']}}, {src.sort: mark['k'], '_id': {'$lt': mark['id']}}]
            order = [(src.sort, DESCENDING), ('_id', DESCENDING)]
        docs = list(self.coll(src).find(query, src.fields).sort(order).limit(PAGE))
        if not docs:
            for gone in ('k', 'id'):
                mark.pop(gone, None)
            mark['phase'] = 'orphan' if missing else 'missing'
            mark['after'] = src.prefix
            return 0, True
        refs = [f'{src.prefix}{d["_id"]}' for d in docs]
        have = self.store.embedding_rows(src.kind, refs)
        lagging = self.lagging(src, docs) if src.name == 'errtpl' else set()
        items: list[Item] = []
        owners: list[dict] = []
        spent = 0
        for doc, ref in zip(docs, refs):
            passages, expire = self.build(src, doc, ctx.tally) if self.eligible(src, doc) else ([], None)
            if passages and (not current(have.get(ref, []), ctx.fp, passages) or ref in lagging):
                if (items or strict) and spent + len(passages) > room:
                    break
                items.append((src.kind, ref, passages, expire))
                spent += len(passages)
            elif not passages and ref in have:
                items.append((src.kind, ref, [], None))
            owners.append(doc)
        done = self.commit(ctx, items) if items else 0
        reached = owners
        if done < len(items):
            wanted = items[done][1]
            reached = owners[:next(n for n, d in enumerate(owners) if f'{src.prefix}{d["_id"]}' == wanted)]
        if not reached:
            return 0, False
        last = reached[-1]
        if not missing:
            mark['k'] = last.get(src.sort) or ''
        mark['id'] = last['_id']
        return spent, True

    def lagging(self, src: Source, docs: list[dict]) -> set[str]:
        held: dict[str, dt.datetime | None] = {}
        for d in self.store.embeddings.find({'kind': src.kind, 'ref': {'$in': [str(x['_id']) for x in docs]}}, {'ref': 1, 'expireAt': 1}):
            until = expiry(d.get('expireAt'))
            held[d['ref']] = min(until, held[d['ref']]) if held.get(d['ref']) and until else until
        out: set[str] = set()
        for d in docs:
            ref, want = str(d['_id']), expiry(d.get('expireAt'))
            if ref in held and want is not None and (held[ref] is None or want - held[ref] >= dt.timedelta(days=1)):
                out.add(ref)
        return out

    def orphans(self, ctx: Ctx, src: Source, mark: dict) -> None:
        refs = self.store.embedding_refs_after(src.kind, mark.get('after') or src.prefix, PAGE, src.prefix + FAR if src.prefix else None)
        for ref in refs:
            if self.coll(src).count_documents({'_id': ref[len(src.prefix):]} | src.query, limit=1) == 0:
                self.store.delete_embeddings(src.kind, ref)
        if len(refs) < PAGE:
            iso = dt.datetime.fromtimestamp(ctx.now, dt.timezone.utc).isoformat()
            mark.clear()
            mark.update({'phase': 'done', 'doneAt': ctx.now, 'at': iso})
            return
        mark['after'] = refs[-1]

    def finish_rebuild(self, ctx: Ctx, marks: dict) -> None:
        if 'rebuild' not in marks:
            return
        active = [x for x in SOURCES if x.setting in ctx.s.embed_sources]
        if any(marks['cursors'].get(x.name, {}).get('phase') != 'done' for x in active):
            return
        if self.kind_on(ctx.s, 'open') and not marks.get('open'):
            return
        if self.kind_on(ctx.s, 'knowledge') and self.knowledge is not None and not marks.get('knowledge'):
            return
        if self.kind_on(ctx.s, 'doc') and self.docs is not None and not marks.get('docs_ok'):
            return
        gone = self.store.purge_embeddings_except(ctx.fp)
        marks.pop('rebuild', None)
        log.info('embed-index rebuild finished, %d old vectors removed', gone)

    def status(self, ctx: Ctx, marks: dict, failure: str | None) -> dict | None:
        try:
            s = ctx.s
            shown = embedclient.status(s, getattr(self.client, 'breaker', None))
            up, kind = shown.get('up'), shown.get('kind')
            before = self.store.embed_state(STATUS)
            if failure == 'error':
                up, kind = before.get('up'), 'error'
            elif failure is not None:
                up, kind = False, failure
            elif ctx.called:
                up, kind = True, None
            elif up is None:
                up, kind = before.get('up'), before.get('kind')
            counts = {k: self.store.embedding_count(kind=k, fp=ctx.fp) for k in EMBED_KINDS} if ctx.fp else {}
            total = sum(counts.values())
            cursors = marks.get('cursors', {})
            doc: dict = {'at': dt.datetime.fromtimestamp(ctx.now, dt.timezone.utc).isoformat(), 'up': up, 'kind': kind, 'model': s.embed_model, 'dims': ctx.dims or None,
                         'fp': ctx.fp or None, 'counts': counts, 'capped': total > int(s.embed_max_vectors), 'embedded': ctx.embedded, 'skipped': ctx.tally.skipped,
                         'backfill': {name: m.get('phase') for name, m in cursors.items()}}
            if 'rebuild' in marks:
                of = int(marks['rebuild'].get('of') or 0)
                doc['rebuilding'] = {'done': min(total, of), 'of': of}
            self.store.save_embed_state(STATUS, doc)
            return doc
        except Exception as e:
            log.warning('embed-index status not saved: %s', error_kind(e))
            return None


def usable_knowledge(runtime) -> Iterable[tuple[str, str, object]]:
    for plugin, found in runtime.serving():
        if held_knowledge(plugin, found):
            continue
        for item in plugin.knowledge:
            yield plugin.id, '', item


def usable_docs(runtime) -> Iterable[DocSource]:
    for plugin, found in runtime.serving():
        corpus = getattr(plugin, 'corpus', None)
        if corpus is None:
            continue
        for item in found:
            yield DocSource(plugin.id, item.name, corpus, docscope.index_on(item.values), docscope.spaces_of(plugin, item.values),
                            lambda now, p=plugin, i=item: runtime.context(p, i, now))
