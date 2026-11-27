import dataclasses
import datetime as dt
import types

import pytest

from skopax import embedclient
from skopax.config import EMBED_MODEL
from skopax.investigator.toolset import Run
from skopax.kit.text import normalised
from skopax.plugins.loki.loki import normalised as loki_normalised
from skopax.semantic import chunks, templates
from skopax.semantic import search as sr
from skopax.semantic.index import VectorIndex
from skopax.semantic.indexer import Indexer
from skopax.semantic.search import Searcher, SearchContext
from skopax.store import Store
from tests.conftest import settings
from tests.fake_embedder import DIMS, FakeEmbedder
from tests.test_loki_plugin import DAY, FakeLoki, burst, open_cards, runtime_of, spike, sweeper_of
from tests.test_loki_plugin import NOW as LOKI_NOW

WHEN = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def clean_learned():
    embedclient.LEARNED.clear()
    yield
    embedclient.LEARNED.clear()


def row(msg: str, svc: str = 'checkout-service', level: str = 'ERROR', now: int = 12, **extra) -> dict:
    return {'svc': svc, 'level': level, 'msg': msg, 'now': now, **extra}


def collect(store, rows, plugin='loki', instance='main', when=WHEN, cards=()):
    section = 'lokiTop' if plugin == 'loki' else 'logTop'
    return templates.collect(store, plugin, instance, {section: rows}, cards, when)


def stored(store) -> list[dict]:
    return list(store.log_templates.find({}))


class Indexing:
    def __init__(self, store: Store):
        self.store = store
        self.s = dataclasses.replace(settings(), embed_url='http://skopax-embed', semantic_search_on=True, embed_sources=('logs',))
        self.fake = FakeEmbedder()
        self.indexer = Indexer(store, lambda: self.s, self.fake, lambda: True)

    def run(self, at: float = 1_792_224_000.0) -> None:
        self.indexer.run_once(at)

    def texts(self) -> list[str]:
        return [t for t in self.fake.texts if t != 'ok']


def test_the_same_error_with_different_ids_is_one_template(store):
    # given
    rows = [row('payment error for order 10001'), row('payment error for order 20002'), row('payment error for order 30003', now=40)]

    # when
    collect(store, rows)

    # then
    found = stored(store)
    assert len(found) == 1 and found[0]['template'] == 'payment error for order <n>' and found[0]['peak'] == 40
    assert found[0]['plugin'] == 'loki' and found[0]['instance'] == 'main' and found[0]['svc'] == 'checkout-service' and found[0]['level'] == 'error'


def test_the_id_depends_on_plugin_instance_service_level_and_template(store):
    # given
    base = row('disk full')

    # when
    collect(store, [base])
    collect(store, [base], instance='east')
    collect(store, [base], plugin='elasticsearch')
    collect(store, [row('disk full', svc='search')])
    collect(store, [row('disk full', level='WARN')])
    collect(store, [row('disk almost full')])

    # then
    assert len(stored(store)) == 6
    assert templates.template_id('loki', 'main', 'checkout-service', 'error', 'disk full') in {d['_id'] for d in stored(store)}


def test_only_error_and_warn_levels_are_kept(store):
    # given
    rows = [row('a failure', level='ERROR'), row('a warning', level='WARN'), row('some note', level='INFO'), row('noise', level='DEBUG'), row('odd', level='')]

    # when
    collect(store, rows)

    # then
    assert {(d['level'], d['template']) for d in stored(store)} == {('error', 'a failure'), ('warn', 'a warning')}


def test_elasticsearch_rows_read_app_and_json_messages(store):
    # given
    rows = [{'app': 'cart', 'level': 'ERROR', 'msg': '{"msg":"stock sync failed for 1234567","level":"error"}', 'now': 9, 'd1': 0, 'd7': 0}]

    # when
    collect(store, rows, plugin='elasticsearch')

    # then
    found = stored(store)
    assert len(found) == 1 and found[0]['svc'] == 'cart' and found[0]['template'] == 'stock sync failed for <n>'


def test_a_held_pattern_is_never_stored(store):
    # given
    held = '[held: the text reads like instructions to an AI]'

    # when
    collect(store, [row(held), row('')])

    # then
    assert stored(store) == []


def test_expire_at_mirrors_last_seen_plus_90_days(store):
    # given
    store.ensure_indexes()
    later = WHEN + dt.timedelta(days=3)

    # when
    collect(store, [row('disk full', now=5)])
    collect(store, [row('disk full', now=30)], when=later)
    collect(store, [row('disk full', now=7)], when=later + dt.timedelta(hours=1))

    # then
    (doc,) = stored(store)
    assert doc['firstSeen'] == WHEN.isoformat() and doc['lastSeen'] == (later + dt.timedelta(hours=1)).isoformat()
    assert doc['expireAt'] == later + dt.timedelta(hours=1) + dt.timedelta(days=90) and doc['peak'] == 30
    assert any(v.get('expireAfterSeconds') == 0 and list(v['key']) == [('expireAt', 1)] for v in store.log_templates.index_information().values())


def test_the_card_key_is_kept_when_the_row_raised_one(store):
    # given
    card = {'key': 'log|checkout-service|ERROR|disk full', 'svc': 'checkout-service', 'msg': 'disk full', 'label': 'ERROR pattern'}

    # when
    collect(store, [row('disk full'), row('other failure')], cards=[card])

    # then
    by = {d['template']: d for d in stored(store)}
    assert by['disk full']['card'] == card['key'] and 'card' not in by['other failure']


def test_secrets_and_emails_are_masked_before_storage(store):
    # given
    rows = [row('login failed for alice@example.com'), row('upstream call failed password=hunter2hunter2 for user'), row('token Bearer abcdefghijklmnopqrstuvwxyz0123456789 rejected')]

    # when
    collect(store, rows)

    # then
    body = ' '.join(d['template'] for d in stored(store))
    assert 'alice@example.com' not in body and 'hunter2hunter2' not in body and 'abcdefghijklmnopqrstuvwxyz0123456789' not in body
    assert len(stored(store)) == 3


def test_a_template_is_capped_at_300_characters(store):
    # when
    collect(store, [row('failure ' + 'x' * 900)])

    # then
    assert len(stored(store)[0]['template']) <= 300


def test_a_collector_failure_never_raises(store, monkeypatch, caplog):
    # given
    def boom(docs):
        raise RuntimeError('mongo down')

    monkeypatch.setattr(store, 'save_log_templates', boom)

    # when
    done = collect(store, [row('disk full')])

    # then
    assert done == 0 and 'log templates were not saved' in caplog.text


def test_a_collector_failure_does_not_fail_the_loki_pass(store, monkeypatch):
    # given
    def boom(docs):
        raise RuntimeError('mongo down')

    monkeypatch.setattr(store, 'save_log_templates', boom)
    runtime = runtime_of(store, FakeLoki(spike()))

    # when
    sweeper_of(store, runtime).run('loki', 'loki', LOKI_NOW)

    # then
    assert store.load()['serverPasses']['loki']['status'] == 'ok'
    assert set(open_cards(store)) == {'log|checkout|ERROR|payment error for order <n>', 'log|search|ERROR|index shard <n> unavailable'}


def test_a_loki_pass_saves_its_templates_with_the_card_key(store):
    # given
    runtime = runtime_of(store, FakeLoki(spike()))

    # when
    sweeper_of(store, runtime).run('loki', 'loki', LOKI_NOW)

    # then
    by = {(d['svc'], d['template']): d for d in stored(store)}
    first = by[('checkout', 'payment error for order <n>')]
    assert first['plugin'] == 'loki' and first['instance'] == 'main' and first['level'] == 'error' and first['card'] == 'log|checkout|ERROR|payment error for order <n>'
    assert ('cart', 'error: cache miss storm') in by and 'card' not in by[('cart', 'error: cache miss storm')]
    assert store.embed_queue.count_documents({'kind': 'errtpl'}) == len(by)


def test_a_second_pass_updates_last_seen_without_queueing_again(store):
    # given
    fake = FakeLoki(spike())
    runtime = runtime_of(store, fake)
    sweeper_of(store, runtime).run('loki', 'loki', LOKI_NOW)
    queued = store.embed_queue.count_documents({'kind': 'errtpl'})
    count = len(stored(store))

    # when
    sweeper_of(store, runtime).run('loki', 'loki', LOKI_NOW + 900)

    # then
    assert len(stored(store)) == count and store.embed_queue.count_documents({'kind': 'errtpl'}) == queued


def test_loki_normalising_is_unchanged_after_the_move():
    # when
    same = loki_normalised is normalised

    # then
    assert same
    assert normalised('2026-11-20T10:00:00Z ERROR payment 12345 failed for 10.0.0.1:8080 req 3f2a9c1d8b7e') == 'ERROR payment <n> failed for <ip> req <id>'
    assert normalised('{"level":"error","msg":"shard 123456 unavailable"}') == 'shard <n> unavailable'
    assert normalised('ts=1 level=error msg="cache miss 99999"') == 'cache miss <n>'
    assert normalised('x' * 200) == 'x' * 90 and normalised('x' * 200, 300) == 'x' * 200


def test_only_new_templates_are_embedded(store):
    # given
    box = Indexing(store)
    collect(store, [row('payment error for order 10001'), row('index shard 123456 unavailable', svc='search')])

    # when
    box.run()
    first = box.texts()
    box.run(1_792_224_100.0)
    collect(store, [row('payment error for order 90009', now=99), row('index shard 654321 unavailable', svc='search')], when=WHEN + dt.timedelta(hours=1))
    box.run(1_792_224_200.0)
    unchanged = box.texts()
    collect(store, [row('queue stalled')])
    box.run(1_792_224_300.0)

    # then
    assert len(first) == 2 and unchanged == first
    assert len(box.texts()) == 3 and box.texts()[-1].endswith('queue stalled')
    assert store.embedding_count(kind='errtpl') == 3
    held = {r['ref']: r for r in store.embeddings.find({'kind': 'errtpl'})}
    assert set(held) == {d['_id'] for d in stored(store)}
    assert all(r['f']['plugin'] == 'loki' and r['f']['instance'] == 'main' for r in held.values())


def test_the_logs_source_is_off_when_not_selected(store):
    # given
    box = Indexing(store)
    box.s = dataclasses.replace(box.s, embed_sources=('runs',))
    collect(store, [row('disk full')])

    # when
    box.run()

    # then
    assert box.texts() == [] and store.embedding_count(kind='errtpl') == 0


def test_an_expired_or_gone_template_loses_its_vector(store):
    # given
    box = Indexing(store)
    collect(store, [row('disk full'), row('queue stalled')])
    box.run()
    expired, gone = [d['_id'] for d in sorted(stored(store), key=lambda d: d['template'])]
    assert store.embedding_count(kind='errtpl') == 2

    # when
    store.log_templates.update_one({'_id': expired}, {'$set': {'expireAt': dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)}})
    store.enqueue_embed('errtpl', expired, 'upsert')
    store.log_templates.delete_one({'_id': gone})
    at = 1_792_224_100.0
    for n in range(12):
        box.run(at + n * 7 * 3600)

    # then
    assert store.embedding_count(kind='errtpl') == 0


class Finding:
    def __init__(self, store):
        self.store = store
        self.s = dataclasses.replace(settings(), embed_url='http://skopax-embed', semantic_search_on=True)
        self.fake = FakeEmbedder()
        self.insts: list = []
        self.index = VectorIndex(DIMS)
        self.searcher = Searcher(store, lambda: self.s, self.fake, self.index, lambda: True, lambda allowed: [], self.serving)

    def serving(self, allowed):
        return [(plugin, [i for i in found if allowed is None or allowed(i)]) for plugin, found in self.insts]

    def seed(self, rows) -> str:
        collect(self.store, rows)
        box = Indexing(self.store)
        box.run()
        self.text = box.texts()[0]
        return stored(self.store)[0]['_id']

    def ask(self, ctx):
        return self.searcher.search(ctx, self.text, ['errtpl'], 5)


def instance(questions: str):
    return types.SimpleNamespace(name='main', values={'questions': questions})


def ask_ctx(role: str) -> SearchContext:
    return SearchContext.asked_run(Run(read_only=True, ask=True, report=False, chat=False, keys=False, asker=True, shadow=False, role=role))


@pytest.fixture
def finding(store) -> Finding:
    return Finding(store)


def test_a_found_template_has_the_documented_row(finding):
    # given
    card = {'key': 'log|checkout-service|ERROR|payment error for order <n>', 'svc': 'checkout-service', 'msg': 'payment error for order 10001', 'label': 'ERROR pattern'}
    collect(finding.store, [row('payment error for order 10001', now=21)], cards=[card])
    box = Indexing(finding.store)
    box.run()
    finding.text = box.texts()[0]
    finding.insts = [(types.SimpleNamespace(id='loki', fields=[object()]), [instance('everyone')])]
    finding.s = dataclasses.replace(finding.s, embed_model=EMBED_MODEL)

    # when
    result = finding.ask(SearchContext.ui('viewer'))

    # then
    assert [h.kind for h in result.items] == ['errtpl']
    assert result.items[0].row == {'kind': 'errtpl', 'svc': 'checkout-service', 'level': 'error', 'template': 'payment error for order <n>', 'firstSeen': WHEN.isoformat(),
                                   'lastSeen': WHEN.isoformat(), 'peak': 21, 'card': card['key']}


@pytest.mark.parametrize('questions, shown', [('everyone', True), ('operators', False), ('none', False)])
def test_a_template_from_a_closed_instance_is_hidden_in_asks_and_ui(finding, questions, shown):
    # given
    finding.seed([row('payment error for order 10001')])
    finding.insts = [(types.SimpleNamespace(id='loki', fields=[object()]), [instance(questions)])]

    # when
    ui = finding.ask(SearchContext.ui('viewer'))
    asked = finding.ask(ask_ctx('viewer'))
    admin_ui = finding.ask(SearchContext.ui('admin'))
    admin_ask = finding.ask(ask_ctx('admin'))
    card = finding.ask(SearchContext.card_run())

    # then
    assert bool(ui.items) is shown and bool(asked.items) is shown
    assert bool(admin_ui.items) is (shown or questions == 'operators') and bool(admin_ask.items) is (shown or questions == 'operators')
    assert bool(card.items) is True


def test_an_anonymous_reader_and_a_missing_instance_never_see_templates(finding):
    # given
    finding.seed([row('payment error for order 10001')])

    # when
    finding.insts = [(types.SimpleNamespace(id='loki', fields=[object()]), [instance('everyone')])]
    anonymous = finding.ask(SearchContext.ui('anonymous'))
    finding.insts = []
    gone = finding.ask(SearchContext.ui('admin'))
    finding.insts = [(types.SimpleNamespace(id='loki', fields=[object()]), [types.SimpleNamespace(name='west', values={'questions': 'everyone'})])]
    other = finding.ask(SearchContext.ui('admin'))

    # then
    assert anonymous.items == [] and gone.items == [] and other.items == []


def test_an_expired_template_is_not_hydrated(finding):
    # given
    ident = finding.seed([row('payment error for order 10001')])
    finding.insts = [(types.SimpleNamespace(id='loki', fields=[object()]), [instance('everyone')])]
    finding.store.log_templates.update_one({'_id': ident}, {'$set': {'expireAt': dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)}})

    # when
    result = finding.ask(SearchContext.ui('viewer'))

    # then
    assert result.items == []


def test_the_stored_template_is_masked_again_when_shown(finding):
    # given
    ident = finding.seed([row('payment error for order 10001')])
    finding.insts = [(types.SimpleNamespace(id='loki', fields=[object()]), [instance('everyone')])]
    finding.store.log_templates.update_one({'_id': ident}, {'$set': {'template': 'payment error for alice@example.com'}})

    # when
    shown = finding.searcher.hydrate_errtpl(SearchContext.ui('viewer'), [ident])

    # then
    assert 'alice@example.com' not in shown[ident]['template']


def test_a_raw_log_line_is_normalised_before_it_is_embedded():
    # when
    jsonl = sr.normalise_query('{"level":"error","msg":"shard 123456 unavailable on 10.0.0.1"}')
    stamped = sr.normalise_query('2026-11-20T10:00:00Z ERROR payment 12345 failed for alice@example.com')
    logfmt = sr.normalise_query('level=warn msg="cache miss 99999"')
    plain = sr.normalise_query('why is checkout slow after the 14:00 deploy')
    sentence = sr.normalise_query('  checkout pool exhausted by retry storm  ')

    # then
    assert jsonl == 'shard <n> unavailable on <ip>'
    assert stamped.startswith('ERROR payment <n> failed for') and 'alice@example.com' not in stamped
    assert logfmt == 'cache miss <n>'
    assert plain == 'why is checkout slow after the 14:00 deploy' and sentence == 'checkout pool exhausted by retry storm'
    assert sr.normalise_query(None) == '' and sr.normalise_query('') == ''


def test_a_secret_cut_by_the_limit_is_masked_before_it_is_cut(store):
    # given
    secret = 'password=hunter2hunter2hunter2'
    message = 'x' * 285 + ' ' + secret

    # when
    collect(store, [row(message)])

    # then
    body = stored(store)[0]['template']
    assert 'hunter2' not in body and 'password=hunt' not in body and len(body) <= 300


def test_the_vector_expiry_follows_last_seen(store):
    # given
    box = Indexing(store)
    collect(store, [row('disk full')])
    box.run()
    before = store.embeddings.find_one({'kind': 'errtpl'})['expireAt']
    calls = len(box.fake.texts)

    # when
    later = WHEN + dt.timedelta(days=40)
    collect(store, [row('disk full')], when=later)
    for n in range(1, 8):
        box.run(1_792_224_000.0 + n * 7 * 3600)

    # then
    (held,) = list(store.embeddings.find({'kind': 'errtpl'}))
    assert held['expireAt'] - before >= dt.timedelta(days=39) and held['expireAt'].replace(microsecond=0) == (later + dt.timedelta(days=90)).replace(tzinfo=held['expireAt'].tzinfo)
    assert len(box.fake.texts) == calls
