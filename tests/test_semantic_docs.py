import dataclasses
import re
import types
from types import SimpleNamespace

import pytest

from skopax import embedclient
from skopax.config import EMBED_MODEL
from skopax.contract import Corpus, DocRef, DocText, InstanceInfo
from skopax.investigator.toolset import Run
from skopax.plugins import confluence, sharepoint
from skopax.plugins.confluence import client as wiki_client
from skopax.plugins.confluence.client import GATEWAY
from skopax.plugins.sharepoint.client import Graph
from skopax.registry import Registry
from skopax.remote import HttpError, Reply
from skopax.semantic import chunks, docscope
from skopax.semantic import indexer as ix
from skopax.semantic.index import VectorIndex, pack_vector
from skopax.semantic.indexer import DocSource, Indexer
from skopax.semantic.search import Searcher, SearchContext
from tests.conftest import settings
from tests.fake_embedder import DIMS, FakeEmbedder, embed_text
from tests.fake_http import FakeHttp
from tests.sharepoint_kit import CLIENT, DRIVE, FOLDER_ID, GRAPH, HOST, SECRET, SITE, TENANT, FakeGraph, base_routes, docx, file_json

NOW = 1_792_224_000.0
HOUR = 3600.0
DAY = 86400.0
SITE_URL = 'https://example.atlassian.net'
TENANT_INFO = f'{SITE_URL}/_edge/tenant_info'
SEARCH = f'{GATEWAY}/cid-1/wiki/rest/api/search'
DOWNLOAD = f'https://{HOST}/sites/ops/_layouts/15/download.aspx'
QUERY = 'database failover runbook for the checkout pool'
BODY = '<h1>Database failover runbook</h1><p>When a node drops, check db-2 first and restart the checkout pool.</p>'


@pytest.fixture(autouse=True)
def clean():
    embedclient.LEARNED.clear()
    wiki_client.CLOUD_IDS.clear()
    wiki_client.SPACE_KEYS.clear()
    yield
    embedclient.LEARNED.clear()
    wiki_client.CLOUD_IDS.clear()
    wiki_client.SPACE_KEYS.clear()


def conf(**over):
    return dataclasses.replace(settings(), **({'embed_url': 'http://skopax-embed', 'semantic_search_on': True} | over))


class Wiki:
    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.leak = False
        self.down = False
        self.queries: list[str] = []
        self.http = FakeHttp({('GET', TENANT_INFO): {'cloudId': 'cid-1'}, ('GET', SEARCH): self.answer})

    def put(self, pid: str, space: str = 'DOCS', title: str = 'Failover runbook', html: str = BODY, when: str = '2026-11-01T08:00:00.000Z') -> None:
        self.pages[pid] = {'space': space, 'title': title, 'html': html, 'when': when}

    def content(self, pid: str, page: dict) -> dict:
        return {'id': pid, 'type': 'page', 'title': page['title'], 'space': {'key': page['space']}, 'version': {'number': 1, 'when': page['when']},
                'body': {'storage': {'value': page['html']}}, '_links': {'webui': f'/spaces/{page["space"]}/pages/{pid}/x'}}

    def answer(self, query, body):
        cql = query['cql'][0]
        self.queries.append(cql)
        if self.down:
            return HttpError(500, SEARCH)
        if cql.startswith('id = '):
            pid = cql[5:]
            return {'results': [{'content': self.content(pid, self.pages[pid])}] if pid in self.pages else []}
        found = re.search(r'space in \((.*?)\)', cql)
        spaces = set(re.findall(r'"([^"]+)"', found.group(1))) if found else set()
        chosen = [(pid, p) for pid, p in sorted(self.pages.items()) if self.leak or p['space'] in spaces]
        start, limit = int(query.get('start', ['0'])[0]), int(query['limit'][0])
        return {'results': [{'content': self.content(pid, p), 'lastModified': p['when']} for pid, p in chosen[start:start + limit]]}

    def source(self, spaces: str = 'DOCS', on: bool = True, name: str = 'main') -> DocSource:
        config = {'url': SITE_URL, 'email': 'bot@example.com', 'api-token': 'tok-123', 'spaces': spaces, 'index-pages': 'true' if on else 'false'}
        ctx = SimpleNamespace(instance=InstanceInfo(name, True, None, config), http=lambda: self.http, now=NOW)
        return DocSource('confluence', name, confluence.PLUGIN.corpus, on, docscope.spaces_of(confluence.PLUGIN, config), lambda now: ctx)


class Box:
    def __init__(self, store, **over):
        self.store = store
        self.s = conf(**over)
        self.fake = FakeEmbedder()
        self.sources: list[DocSource] = []
        self.wiki = Wiki()
        self.indexer = Indexer(store, lambda: self.s, self.fake, lambda: True, None, lambda: list(self.sources))

    def set(self, **over) -> None:
        self.s = dataclasses.replace(self.s, **over)

    def run(self, at: float = NOW) -> dict:
        return self.indexer.run_once(at)

    def rows(self) -> list[dict]:
        return list(self.store.embeddings.find({'kind': 'doc'}))

    def refs(self) -> set[str]:
        return {r['ref'] for r in self.rows()}


@pytest.fixture
def box(store) -> Box:
    return Box(store)


def test_pages_outside_allowed_spaces_are_never_indexed(box):
    # given
    box.wiki.put('1', 'DOCS')
    box.wiki.put('2', 'OPS', 'Ops notes')
    box.wiki.leak = True
    box.sources = [box.wiki.source('DOCS')]

    # when
    box.run()

    # then
    assert box.refs() == {'confluence:main:1'}
    assert 'space in ("DOCS")' in box.wiki.queries[0] and 'type = page' in box.wiki.queries[0]
    assert all(r['f'] == {'plugin': 'confluence', 'instance': 'main', 'space': 'DOCS'} for r in box.rows())
    assert not any('Ops notes' in t for t in box.fake.texts)


def test_a_page_row_keeps_title_url_and_a_capped_masked_excerpt(box):
    # given
    long = 'Contact alice@example.com about the pool. ' + 'The pool saturates under load. ' * 60
    box.wiki.put('1', title='Pool runbook', html=f'<p>{long}</p>')
    box.sources = [box.wiki.source()]

    # when
    box.run()

    # then
    row = sorted(box.rows(), key=lambda r: r['part'])[0]
    assert row['title'] == 'Pool runbook' and row['url'] == f'{SITE_URL}/wiki/spaces/DOCS/pages/1/x'
    assert 0 < len(row['excerpt']) <= 300 and 'alice@example.com' not in row['excerpt'] and 'The pool saturates' in row['excerpt']
    assert not any('alice@example.com' in t for t in box.fake.texts)
    assert len(box.rows()) > 1 and [r for r in box.rows() if r['part'] > 0 and ('title' in r or 'url' in r or 'excerpt' in r)] == []


def test_a_changed_page_is_reembedded_and_an_unchanged_one_is_not(box):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source()]
    box.run()
    before = len(box.fake.calls)

    # when
    box.run(NOW + 7 * HOUR)
    unchanged = len(box.fake.calls)
    box.wiki.put('1', html='<p>Totally new failover steps for the pool.</p>', when='2026-11-02T08:00:00.000Z')
    box.run(NOW + 14 * HOUR)

    # then
    assert unchanged == before
    assert any('Totally new failover steps' in t for t in box.fake.texts)
    assert len(box.refs()) == 1 and box.rows()[0]['at'] is not None


def test_removing_a_space_from_the_allowed_spaces_purges_its_pages(box):
    # given
    box.wiki.put('1', 'DOCS')
    box.wiki.put('2', 'OPS', 'Ops notes')
    box.sources = [box.wiki.source('DOCS,OPS')]
    box.run()
    assert box.refs() == {'confluence:main:1', 'confluence:main:2'}

    # when
    box.sources = [box.wiki.source('DOCS')]
    box.run(NOW + 60)

    # then
    assert box.refs() == {'confluence:main:1'}


def test_a_page_deleted_in_the_source_leaves_at_the_daily_listing(box):
    # given
    box.wiki.put('1')
    box.wiki.put('2', title='Second page')
    box.sources = [box.wiki.source()]
    box.run()
    del box.wiki.pages['2']

    # when
    box.run(NOW + 7 * HOUR)
    still = box.refs()
    box.run(NOW + 25 * HOUR)

    # then
    assert still == {'confluence:main:1', 'confluence:main:2'}
    assert box.refs() == {'confluence:main:1'}


def test_the_incremental_listing_asks_only_for_pages_modified_since(box):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source()]
    box.run()

    # when
    box.run(NOW + 7 * HOUR)

    # then
    assert 'lastmodified >' not in box.wiki.queries[0]
    listing = [q for q in box.wiki.queries if not q.startswith('id = ')]
    assert 'lastmodified > "' in listing[-1] and 'space in ("DOCS")' in listing[-1]


def test_field_off_means_no_crawl_and_purges_what_was_kept(box):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source(on=False)]
    box.run()
    assert box.wiki.http.calls == [] and box.rows() == []
    box.sources = [box.wiki.source(on=True)]
    box.run(NOW + 60)
    assert box.refs() == {'confluence:main:1'}
    calls = len(box.wiki.http.calls)

    # when
    box.sources = [box.wiki.source(on=False)]
    box.run(NOW + 8 * HOUR)

    # then
    assert box.rows() == [] and len(box.wiki.http.calls) == calls


def test_a_deleted_instance_purges_its_pages_and_leaves_others(box):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source(name='main'), box.wiki.source(name='second')]
    box.run()
    assert {r['f']['instance'] for r in box.rows()} == {'main', 'second'}

    # when
    box.sources = [box.wiki.source(name='second')]
    box.run(NOW + 60)

    # then
    assert {r['f']['instance'] for r in box.rows()} == {'second'}


def test_an_unreachable_source_purges_after_fourteen_days_without_a_listing(box):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source()]
    box.run()
    box.wiki.down = True

    # when
    box.run(NOW + 7 * HOUR)
    box.run(NOW + 13 * DAY)
    kept = box.refs()
    box.run(NOW + 14 * DAY + HOUR)

    # then
    assert kept == {'confluence:main:1'}
    assert box.rows() == []


def test_a_successful_listing_resets_the_fourteen_day_clock(box):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source()]
    box.run()
    box.wiki.down = True
    box.run(NOW + 10 * DAY)
    box.wiki.down = False
    box.run(NOW + 11 * DAY)
    box.wiki.down = True

    # when
    box.run(NOW + 20 * DAY)

    # then
    assert box.refs() == {'confluence:main:1'}


def test_the_pass_budget_limits_pages_and_the_crawl_resumes(box):
    # given
    for n in range(5):
        box.wiki.put(str(n + 1), title=f'Page {n + 1}', html=f'<p>Unique text number {n + 1} about the pool.</p>')
    box.set(embed_docs_per_pass=2)
    box.sources = [box.wiki.source()]

    # when
    box.run()
    first = len({r['ref'] for r in box.rows()})
    box.run(NOW + 60)
    box.run(NOW + 120)

    # then
    assert first == 2
    assert len(box.refs()) == 5
    listing = {q for q in box.wiki.queries if not q.startswith('id = ')}
    assert len(listing) == 1 and 'lastmodified >' not in next(iter(listing))


def test_zero_pages_per_pass_pauses_the_crawl(box):
    # given
    box.wiki.put('1')
    box.set(embed_docs_per_pass=0)
    box.sources = [box.wiki.source()]

    # when
    box.run()

    # then
    assert box.rows() == [] and box.wiki.http.calls == []


def test_the_docs_source_switch_turns_the_crawl_off(box):
    # given
    box.wiki.put('1')
    box.set(embed_sources=('runs', 'cards'))
    box.sources = [box.wiki.source()]

    # when
    box.run()

    # then
    assert box.rows() == [] and box.wiki.http.calls == []


def test_a_page_that_turns_unreadable_is_removed(box):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source()]
    box.run()
    box.wiki.put('1', space='SECRET', when='2026-11-05T08:00:00.000Z')
    box.wiki.leak = True

    # when
    box.run(NOW + 25 * HOUR)

    # then
    assert box.rows() == []


def test_the_crawl_state_is_kept_in_the_marks(box, store):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source()]

    # when
    box.run()

    # then
    entry = store.embed_state('embed-marks')['docs']['by']['confluence:main']
    assert entry['ok'] == NOW and entry['todo'] == [] and entry['full'] == NOW and entry['fp']


def test_a_pass_that_loses_the_marks_race_keeps_the_rows_and_crawls_again(box, store, monkeypatch):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source()]
    monkeypatch.setattr(store, 'save_embed_marks', lambda marks, version: False)

    # when
    box.run()
    box.run(NOW + 60)

    # then
    assert box.refs() == {'confluence:main:1'}
    assert store.embed_state('embed-marks').get('docs') is None


def test_a_source_that_fails_to_list_does_not_stop_other_instances(box):
    # given
    box.wiki.put('1')
    broken = Wiki()
    broken.down = True
    box.sources = [broken.source(name='broken'), box.wiki.source(name='main')]

    # when
    box.run()

    # then
    assert box.refs() == {'confluence:main:1'}


def failing_source(error: Exception, name: str = 'broken') -> DocSource:
    def raising(now):
        raise error

    return DocSource('confluence', name, confluence.PLUGIN.corpus, True, None, raising)


def test_a_plain_error_in_one_docs_instance_is_logged_and_the_next_instance_and_the_backfill_still_run(box, monkeypatch):
    # given
    box.wiki.put('1')
    box.sources = [failing_source(RuntimeError('boom')), box.wiki.source(name='main')]
    calls = []
    original = box.indexer.backfill
    monkeypatch.setattr(box.indexer, 'backfill', lambda ctx, marks: (calls.append(1), original(ctx, marks))[1])

    # when
    status = box.run()

    # then
    assert box.refs() == {'confluence:main:1'}
    assert calls == [1]
    assert status['kind'] != 'error'


def test_an_embed_error_in_a_docs_instance_ends_the_pass_with_its_kind(box, monkeypatch):
    # given
    box.wiki.put('1')
    box.sources = [failing_source(embedclient.EmbedError('down')), box.wiki.source(name='main')]
    calls = []
    original = box.indexer.backfill
    monkeypatch.setattr(box.indexer, 'backfill', lambda ctx, marks: (calls.append(1), original(ctx, marks))[1])

    # when
    status = box.run()

    # then
    assert status['kind'] == 'down' and status['up'] is False
    assert calls == []
    assert box.refs() == set()


def test_a_space_with_more_pages_than_max_pages_never_deletes(box, monkeypatch):
    # given
    for n in range(3):
        box.wiki.put(str(n + 1), title=f'Page {n + 1}', html=f'<p>Text {n + 1} about checkout.</p>')
    source = box.wiki.source()
    box.sources = [source]
    box.run()
    monkeypatch.setattr(confluence, 'CORPUS_PAGES', 2)
    small = dataclasses.replace(source, corpus=Corpus(confluence.corpus_list, confluence.corpus_read, 2, source.corpus.spaces))
    box.sources = [small]

    # when
    box.run(NOW + 25 * HOUR)
    del box.wiki.pages['3']
    box.run(NOW + 26 * HOUR)
    capped = box.refs()
    monkeypatch.setattr(confluence, 'CORPUS_PAGES', 3)
    box.sources = [dataclasses.replace(small, corpus=Corpus(confluence.corpus_list, confluence.corpus_read, 3, source.corpus.spaces))]
    box.run(NOW + 50 * HOUR)

    # then
    assert capped == {'confluence:main:1', 'confluence:main:2', 'confluence:main:3'}
    assert box.refs() == {'confluence:main:1', 'confluence:main:2'}


def test_a_listing_ignores_hits_with_a_bad_space_key(box):
    # given
    box.wiki.put('1', 'DOCS')
    box.wiki.put('2', 'DO CS')
    box.wiki.leak = True
    box.sources = [box.wiki.source('DOCS')]

    # when
    box.run()

    # then
    assert box.refs() == {'confluence:main:1'}


def sp_source(files: dict[str, bytes], on: bool = True) -> tuple[DocSource, FakeGraph]:
    base = f'{GRAPH}/drives/{DRIVE}/items'
    routes = base_routes() | {
        ('GET', f'{base}/F1'): file_json('cart-restart.md', 'F1', '/Runbooks/Prod'),
        ('GET', f'{base}/F2'): file_json('Payments outage.docx', 'F2', '/Runbooks/Prod', size=5000),
        ('GET', f'{base}/F4'): file_json('checkout-service.md', 'F4', '/Runbooks/Prod/Checkout'),
        ('GET', f'{base}/FOLDER2'): {'id': 'FOLDER2', 'name': 'Checkout', 'folder': {}, 'parentReference': {'driveId': DRIVE, 'id': FOLDER_ID, 'path': f'/drives/{DRIVE}/root:/Runbooks/Prod'}},
    }
    for item in ('F1', 'F2', 'F4'):
        routes[('GET', f'{base}/{item}/content')] = Reply(b'', {'location': f'{DOWNLOAD}?item={item}'}, 302)
    routes[('GET', DOWNLOAD)] = lambda q, h, d: Reply(files[q['item'][0]], {}, 200)
    http = FakeGraph(routes)
    g = Graph(TENANT, CLIENT, SECRET, SITE, '', 'Runbooks/Prod', http)
    ctx = SimpleNamespace(client=lambda: g, instance=None, now=NOW)
    return DocSource('sharepoint', 'ops', sharepoint.PLUGIN.corpus, on, None, lambda now: ctx), http


def test_sharepoint_indexes_only_readable_files_of_the_folder(box):
    # given
    source, http = sp_source({'F1': b'# Cart restart\nRestart the cart pods one at a time. Contact alice@example.com.', 'F2': docx(['Payments outage', 'Fail over the payment queue.']),
                              'F4': b'# Checkout\nRaise the pool size.'})
    box.sources = [source]

    # when
    box.run()

    # then
    assert box.refs() == {'sharepoint:ops:F1', 'sharepoint:ops:F2', 'sharepoint:ops:F4'}
    first = next(r for r in box.rows() if r['ref'] == 'sharepoint:ops:F1')
    assert first['title'] == 'cart-restart.md' and first['url'].endswith('/Runbooks/Prod/cart-restart.md') and first['f'] == {'plugin': 'sharepoint', 'instance': 'ops', 'space': ''}
    assert 'alice@example.com' not in first['excerpt'] and not any('alice@example.com' in t for t in box.fake.texts)
    assert not [c for c in http.calls if c[1].endswith('/F3/content') or c[1].endswith('/F5/content') or c[1].endswith('/F6/content')]


def test_sharepoint_turned_off_purges_the_folder_copies(box):
    # given
    source, _ = sp_source({'F1': b'# Cart', 'F2': docx(['Payments']), 'F4': b'# Checkout'})
    box.sources = [source]
    box.run()
    assert len(box.refs()) == 3

    # when
    box.sources = [dataclasses.replace(source, on=False)]
    box.run(NOW + 60)

    # then
    assert box.rows() == []


def test_a_rebuild_waits_for_the_docs_before_dropping_old_vectors(box, store):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source()]
    box.run()
    marks = store.embed_state('embed-marks')

    # then
    assert marks['docs_ok'] is True and 'rebuild' not in marks


def instance(name: str, questions: str = 'everyone', on: bool = True):
    return types.SimpleNamespace(name=name, values={'questions': questions, 'spaces': 'DOCS', 'index-pages': 'true' if on else 'false'})


class SBox:
    def __init__(self, store):
        self.store = store
        self.s = conf()
        self.fake = FakeEmbedder()
        self.insts: list = []
        self.index = VectorIndex(DIMS)
        self.searcher = Searcher(store, lambda: self.s, self.fake, self.index, lambda: True, lambda allowed: [], self.serving)
        self.plugin = types.SimpleNamespace(id='confluence', fields=[object()], corpus=confluence.PLUGIN.corpus)

    def serving(self, allowed):
        return [(self.plugin, [i for i in found if allowed is None or allowed(i)]) for plugin, found in [(self.plugin, self.insts)]]

    def put(self, ref: str = 'confluence:main:1', space: str = 'DOCS', title: str = 'Failover runbook', url: str | None = 'https://example.atlassian.net/wiki/x',
            excerpt: str = 'check db-2 first', part: int = 0, instance_name: str = 'main', plugin: str = 'confluence') -> None:
        fp = chunks.fingerprint(EMBED_MODEL, DIMS, self.s.passage_prefix)
        doc = {'kind': 'doc', 'ref': ref, 'part': part, 'fp': fp, 'hash': f'{ref}{part}', 'vec': pack_vector(embed_text(QUERY), DIMS),
               'f': {'plugin': plugin, 'instance': instance_name, 'space': space}, 'title': title, 'excerpt': excerpt} | ({'url': url} if url else {})
        self.store.upsert_embeddings([doc], dims=DIMS)

    def run(self, ctx, kinds=('doc',)):
        return self.searcher.search(ctx, QUERY, list(kinds), 5)


@pytest.fixture
def sbox(store) -> SBox:
    return SBox(store)


def asked(role: str) -> SearchContext:
    return SearchContext.asked_run(Run(read_only=True, ask=True, report=False, chat=False, keys=False, asker=True, shadow=False, role=role))


def test_a_doc_hit_shows_the_row_shape_of_the_embedding_doc(sbox):
    # given
    sbox.insts = [instance('main')]
    sbox.put()

    # when
    result = sbox.run(SearchContext.ui('viewer'))

    # then
    assert result.semantic is True
    assert [h.row for h in result.items] == [{'kind': 'doc', 'plugin': 'confluence', 'instance': 'main', 'title': 'Failover runbook', 'url': 'https://example.atlassian.net/wiki/x',
                                              'excerpt': 'check db-2 first', 'id': 'confluence:main:1'}]
    assert result.items[0].kind == 'doc' and result.items[0].ref == 'confluence:main:1'


def test_a_doc_hit_uses_the_first_part_of_the_page_once(sbox):
    # given
    sbox.insts = [instance('main')]
    sbox.put(part=1, excerpt='second part text')
    sbox.put(part=0, excerpt='first part text')

    # when
    result = sbox.run(SearchContext.ui('viewer'))

    # then
    assert [h.row['excerpt'] for h in result.items] == ['first part text']


@pytest.mark.parametrize('questions, role, shown', [('everyone', 'viewer', True), ('operators', 'viewer', False), ('operators', 'operator', True), ('operators', 'admin', True),
                                                    ('none', 'admin', False), ('none', 'operator', False), ('none', 'viewer', False)])
def test_doc_hits_follow_the_questions_setting_for_asks_and_the_ui(sbox, questions, role, shown):
    # given
    sbox.insts = [instance('main', questions)]
    sbox.put()

    # when
    ui = sbox.run(SearchContext.ui(role)).items
    ask = sbox.run(asked(role)).items

    # then
    assert bool(ui) is shown and bool(ask) is shown


def test_card_runs_see_docs_of_every_serving_instance_and_anonymous_never(sbox):
    # given
    sbox.insts = [instance('main', 'none')]
    sbox.put()

    # when
    card = sbox.run(SearchContext.card_run()).items
    anonymous = sbox.run(SearchContext.ui('anonymous'), kinds=('doc', 'run')).items

    # then
    assert [h.kind for h in card] == ['doc'] and anonymous == []


def test_a_doc_hit_needs_the_instance_serving_the_switch_on_and_the_space_allowed(sbox):
    # given
    sbox.put('confluence:main:1', space='DOCS')
    sbox.put('confluence:main:2', space='OPS')
    ctx = SearchContext.ui('admin')

    # when
    sbox.insts = []
    gone = sbox.run(ctx).items
    sbox.insts = [instance('main', on=False)]
    off = sbox.run(ctx).items
    sbox.insts = [instance('main')]
    live = sbox.run(ctx).items
    sbox.insts = [instance('other')]
    other = sbox.run(ctx).items

    # then
    assert gone == [] and off == [] and other == []
    assert [h.ref for h in live] == ['confluence:main:1']


def test_a_doc_excerpt_title_and_url_are_capped_and_masked_again(sbox):
    # given
    sbox.insts = [instance('main')]
    sbox.put(title='Runbook for alice@example.com', excerpt='Mail alice@example.com now. ' + 'x' * 250, url='https://example.atlassian.net/wiki/x?mail=alice@example.com')

    # when
    row = sbox.run(SearchContext.ui('viewer')).items[0].row

    # then
    assert 'alice@example.com' not in row['title'] and 'alice@example.com' not in row['excerpt'] and len(row['excerpt']) <= 300
    assert row['url'] is None


def test_doc_hits_are_dropped_when_their_filters_are_missing(sbox):
    # given
    sbox.insts = [instance('main')]
    sbox.put(plugin='', instance_name='')

    # when
    result = sbox.run(SearchContext.ui('admin'))

    # then
    assert result.items == []


def test_a_crawled_page_is_found_by_meaning_through_the_real_indexer(store):
    # given
    box = Box(store)
    box.wiki.put('1', title='Failover', html=f'<p>{QUERY}</p>')
    box.sources = [box.wiki.source()]
    box.run()
    sbox = SBox(store)
    sbox.insts = [instance('main')]

    # when
    result = sbox.run(SearchContext.ui('viewer'))

    # then
    assert [h.row['title'] for h in result.items] == ['Failover']
    assert result.items[0].row['url'] == f'{SITE_URL}/wiki/spaces/DOCS/pages/1/x'


def good(**over):
    base = confluence.PLUGIN.corpus
    return Corpus(over.get('list', base.list), over.get('read', base.read), over.get('max_pages', base.max_pages), over.get('spaces', base.spaces))


@pytest.mark.parametrize('corpus', [good(list=None), good(read='no'), good(max_pages=0), good(max_pages=-1), good(max_pages=True), good(max_pages='9'), good(max_pages=10 ** 9),
                                    good(spaces='x'), 'corpus'])
def test_the_registry_rejects_a_bad_corpus(corpus):
    # given
    plugin = dataclasses.replace(confluence.PLUGIN, corpus=corpus)

    # when
    loaded = Registry.of(plugin).loaded()[0]

    # then
    assert loaded.state == 'broken' and 'corpus' in loaded.reason


def test_the_registry_wants_the_index_pages_switch_next_to_a_corpus():
    # given
    fields = tuple(f for f in confluence.PLUGIN.fields if f.key != 'index-pages')
    plugin = dataclasses.replace(confluence.PLUGIN, fields=fields)

    # when
    loaded = Registry.of(plugin).loaded()[0]

    # then
    assert loaded.state == 'broken' and 'index-pages' in loaded.reason


@pytest.mark.parametrize('plugin', [confluence.PLUGIN, sharepoint.PLUGIN])
def test_the_docs_plugins_load_with_the_off_by_default_switch(plugin):
    # when
    loaded = Registry.of(plugin).loaded()[0]
    field = next(f for f in plugin.fields if f.key == 'index-pages')

    # then
    assert loaded.state == 'ok' and plugin.corpus is not None
    assert (field.kind, field.default, field.group, field.label) == ('bool', 'false', 'advanced', 'Index pages for search by meaning')
    assert 'vectors' in field.help and 'excerpts' in field.help


def test_the_contract_types_carry_what_the_indexer_needs():
    # given
    ref = DocRef('1', 'Title', None, None)
    text = DocText('Title', None, 'body')

    # then
    assert (ref.space, ref.modified, text.url) == ('', None, None)
    assert Registry.of(confluence.PLUGIN).loaded()[0].state == 'ok'
    assert ix.DOCS_DOWN_S == 14 * DAY and ix.DOCS_CRAWL_S == 6 * HOUR and ix.DOCS_FULL_S == 24 * HOUR


def test_one_unreadable_file_does_not_stall_the_rest(box):
    # given
    source, http = sp_source({'F1': b'not a word file', 'F2': b'not a zip at all', 'F4': b'# Checkout\nRaise the pool size.'})
    http.routes[('GET', f'{GRAPH}/drives/{DRIVE}/items/F1')] = file_json('cart-restart.md', 'F1', '/Runbooks/Prod', size=5 * 1024 * 1024)
    box.sources = [source]

    # when
    box.run()

    # then
    assert box.refs() == {'sharepoint:ops:F4'}
    assert box.store.embed_state('embed-marks')['docs']['by']['sharepoint:ops']['todo'] == []


def test_a_failing_read_is_retried_three_passes_then_dropped_keeping_old_rows(box, monkeypatch):
    # given
    box.wiki.put('1')
    box.wiki.put('2', title='Second')
    box.sources = [box.wiki.source()]
    box.run()
    box.wiki.put('1', html='<p>Changed.</p>', when='2026-11-09T08:00:00.000Z')
    original = box.wiki.answer

    def flaky(query, body):
        if query['cql'][0] == 'id = 1':
            return HttpError(500, SEARCH)
        return original(query, body)

    box.wiki.http.routes[('GET', SEARCH)] = flaky

    # when
    box.run(NOW + 25 * HOUR)
    after_one = box.store.embed_state('embed-marks')['docs']['by']['confluence:main']['todo']
    box.run(NOW + 25 * HOUR + 60)
    box.run(NOW + 25 * HOUR + 120)
    box.run(NOW + 25 * HOUR + 180)

    # then
    assert [t[0] for t in after_one] == ['1'] and after_one[0][3] == 1
    assert box.store.embed_state('embed-marks')['docs']['by']['confluence:main']['todo'] == []
    assert box.refs() == {'confluence:main:1', 'confluence:main:2'}
    assert not any('Changed.' in t for t in box.fake.texts)


def test_an_unreachable_instance_does_not_block_a_rebuild(box, store):
    # given
    broken = Wiki()
    broken.down = True
    box.sources = [broken.source()]

    # when
    box.run()

    # then
    assert store.embed_state('embed-marks')['docs_ok'] is True


def test_docs_paused_by_a_zero_budget_do_not_block_a_rebuild_and_keep_the_clock_fresh(box, store):
    # given
    box.wiki.put('1')
    box.sources = [box.wiki.source()]
    box.run()
    box.set(embed_docs_per_pass=0)

    # when
    box.run(NOW + 13 * DAY)
    entry = store.embed_state('embed-marks')['docs']['by']['confluence:main']
    box.set(embed_docs_per_pass=200)
    box.wiki.down = True
    box.run(NOW + 20 * DAY)

    # then
    assert store.embed_state('embed-marks')['docs_ok'] is True and entry['ok'] == NOW + 13 * DAY
    assert box.refs() == {'confluence:main:1'}


def test_turning_docs_off_in_the_sources_purges_the_doc_vectors_and_hides_them(box, store):
    # given
    box.wiki.put('1', html=f'<p>{QUERY}</p>')
    box.sources = [box.wiki.source()]
    box.run()
    sbox = SBox(store)
    sbox.insts = [instance('main')]
    assert sbox.run(SearchContext.ui('viewer')).items

    # when
    sbox.s = dataclasses.replace(sbox.s, embed_sources=('runs',))
    hidden = sbox.run(SearchContext.ui('viewer')).items
    box.set(embed_sources=('runs',))
    box.run(NOW + 60)

    # then
    assert hidden == [] and box.rows() == []


def test_a_doc_url_with_another_scheme_is_dropped(sbox):
    # given
    sbox.insts = [instance('main')]
    sbox.put(url='javascript:alert(1)')

    # when
    row = sbox.run(SearchContext.ui('viewer')).items[0].row

    # then
    assert row['url'] is None
