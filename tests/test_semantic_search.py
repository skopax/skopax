import dataclasses
import types

import numpy as np
import pytest
from pymongo.errors import AutoReconnect

from skopax import embedclient
from skopax.config import EMBED_MODEL
from skopax.contract import Knowledge
from skopax.investigator.toolset import Run
from skopax.semantic import chunks
from skopax.semantic import search as sr
from skopax.semantic.index import VectorIndex, pack_vector
from skopax.semantic.search import Searcher, SearchContext, SearchUnavailable
from tests.conftest import settings
from tests.fake_embedder import DIMS, FakeEmbedder, embed_text

QUERY = 'checkout pool exhausted by retry storm'
KEY = 'cpu|shop/checkout-service|pod-a'
SVC = 'checkout-service'
ROLES = ('admin', 'operator', 'viewer', 'anonymous')
HIDDEN_OUTCOMES = ('held', 'releasing', 'dropped', 'discarded')
FORBIDDEN = {'guard', 'held', 'vec', 'screenshots'}


@pytest.fixture(autouse=True)
def clean_learned():
    embedclient.LEARNED.clear()
    yield
    embedclient.LEARNED.clear()


def conf(**over):
    return dataclasses.replace(settings(), **({'embed_url': 'http://skopax-embed', 'semantic_search_on': True} | over))


class Box:
    def __init__(self, store):
        self.store = store
        self.s = conf()
        self.fake = FakeEmbedder()
        self.on = True
        self.items: list = []
        self.insts: list = []
        self.index = VectorIndex(DIMS)
        self.searcher = Searcher(store, lambda: self.s, self.fake, self.index, lambda: self.on, lambda allowed: list(self.items), self.serving)

    def serving(self, allowed):
        out = []
        for plugin, found in self.insts:
            kept = [i for i in found if allowed is None or allowed(i)]
            if kept or not plugin.fields:
                out.append((plugin, kept))
        return out

    @property
    def fp(self) -> str:
        return chunks.fingerprint(EMBED_MODEL, DIMS, self.s.passage_prefix)

    def put(self, kind: str, ref: str, text: str = QUERY, part: int = 0, vec=None, **f) -> None:
        vector = embed_text(text) if vec is None else vec
        doc = {'kind': kind, 'ref': ref, 'part': part, 'fp': self.fp, 'hash': f'{kind}{ref}{part}', 'vec': pack_vector(vector, DIMS), 'f': f}
        self.store.upsert_embeddings([doc], dims=DIMS)

    def run(self, ctx, text: str = QUERY, kinds=None, limit: int = 5, service=None):
        return self.searcher.search(ctx, text, kinds, limit, service)


@pytest.fixture
def box(store) -> Box:
    return Box(store)


def add_run(store, run_id='ok', outcome='done', trigger='review', keys=(KEY,), recap='The pool was exhausted by a retry storm.', started='2026-11-20T10:00:00Z', **extra) -> str:
    store.add_investigation({'id': run_id, 'outcome': outcome, 'trigger': trigger, 'keys': list(keys), 'ask': {'text': 'why is checkout slow', 'by': 'alice@example.com'}, 'recap': recap,
                             'startedAt': started, 'toolCalls': [{'name': 'logs', 'input': 'query one', 'rows': 3}], **extra})
    return run_id


def add_card(store, key=KEY, first='2026-11-20T09:00:00Z', what='CPU above 90 percent for 10 minutes', **extra) -> str:
    card = {'key': key, 'svc': SVC, 'label': 'High CPU', 'sub': 'pod-a', 'sev': 'warn', 'firstSeen': first, 'lastSeen': first, 'what': what, **extra}
    store.anomalies.replace_one({'_id': key}, {'_id': key, 'status': 'open', **card}, upsert=True)
    return store.record_card(card, 'cleared')


def add_closed(store, key=KEY, first='2026-11-20T09:00:00Z', **extra) -> str:
    card = {'key': key, 'svc': SVC, 'label': 'High CPU', 'sub': 'pod-a', 'sev': 'warn', 'firstSeen': first, 'lastSeen': first, **extra}
    return store.record_card(card, 'cleared')


def seen(value) -> set:
    if isinstance(value, dict):
        return set(value) | set().union(*(seen(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(seen(v) for v in value))
    return set()


def refs(result, kind: str | None = None) -> list[str]:
    return [h.ref for h in result.items if kind is None or h.kind == kind]


def contexts(role: str) -> list[tuple[str, SearchContext]]:
    run = Run(read_only=True, ask=True, report=False, chat=False, keys=False, asker=True, shadow=False, role=role)
    return [('ask', SearchContext.asked_run(run)), ('ui', SearchContext.ui(role))]


ALL = [('card', SearchContext.card_run())] + [(f'{m}-{r}', c) for r in ROLES for m, c in contexts(r)]


@pytest.mark.parametrize('ctx', [c for _, c in ALL], ids=[n for n, _ in ALL])
def test_hidden_runs_never_appear(box, store, ctx):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')
    for n, outcome in enumerate(HIDDEN_OUTCOMES):
        add_run(store, f'hidden-{outcome}', outcome=outcome, guard={'reason': 'secret', 'category': 'x'}, held={'text': 'draft', 'until': 'later'}, screenshots=['s1'])
        box.put('run', f'hidden-{outcome}', svc=SVC, trigger='review')
    add_run(store, 'chat-run', trigger='chat')
    box.put('run', 'chat-run', svc=SVC, trigger='chat')
    add_run(store, 'no-trigger', trigger=None)
    box.put('run', 'no-trigger', svc=SVC)
    box.put('run', 'gone', svc=SVC, trigger='review')

    # when
    result = box.run(ctx, limit=20)

    # then
    assert result.semantic is True
    assert refs(result, 'run') == ['ok']


@pytest.mark.parametrize('ctx', [c for _, c in ALL], ids=[n for n, _ in ALL])
def test_rows_carry_no_guard_held_or_screenshots_even_for_released_runs(box, store, ctx):
    # given
    add_run(store, 'released', guard={'reason': 'secret', 'category': 'x', 'questions': 1, 'watch': False, 'outcome': 'held'}, held={'text': 'draft', 'until': 'later', 'released': 'now'},
            screenshots=['s1'], proposal={'tool': 'x'}, trace=['t'])
    box.put('run', 'released', svc=SVC, trigger='review')
    add_run(store, 'held-one', outcome='held', guard={'reason': 'secret'}, held={'text': 'draft'}, screenshots=['s2'])
    box.put('run', 'held-one', svc=SVC, trigger='review')

    # when
    result = box.run(ctx, limit=20)

    # then
    assert refs(result, 'run') == ['released']
    assert not seen([h.row for h in result.items]) & FORBIDDEN
    assert 'vec' not in repr(result.items)


def test_ui_run_row_for_admin_is_the_run_page_shape_without_guard(box, store):
    # given
    add_run(store, 'ok', guard={'reason': 'secret'}, held={'text': 'draft'}, screenshots=['s1'])
    box.put('run', 'ok', svc=SVC, trigger='review')

    # when
    row = box.run(SearchContext.ui('admin')).items[0].row

    # then
    assert row['id'] == 'ok' and row['toolCalls'] == 1 and row['outcome'] == 'done' and row['ask'] == {'text': 'why is checkout slow', 'by': 'alice@example.com'}
    assert not seen(row) & FORBIDDEN


def test_rows_are_scrubbed_again_even_if_a_reader_fetched_guard_or_held(box, store, monkeypatch):
    # given
    add_run(store, 'ok', guard={'reason': 'secret'}, held={'text': 'draft', 'until': 'later'}, screenshots=['s1'])
    box.put('run', 'ok', svc=SVC, trigger='review')
    monkeypatch.setattr(sr, 'UI_RUN_FIELDS', {**sr.UI_RUN_FIELDS, 'guard': 1, 'held': 1, 'screenshots': 1})

    # when
    row = box.run(SearchContext.ui('admin')).items[0].row

    # then
    assert row['id'] == 'ok'
    assert not seen(row) & FORBIDDEN
    assert sr.scrub({'a': [{'guard': 1, 'b': {'vec': 2, 'ok': 3}}], 'held': 1}) == {'a': [{'b': {'ok': 3}}]}


def test_investigator_run_row_is_the_past_run_shape(box, store):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')
    run = Run(read_only=False, ask=False, report=False, chat=False, keys=True, asker=False, shadow=False)

    # when
    plain = box.run(SearchContext.card_run(run)).items[0].row
    queried = box.run(SearchContext.card_run(run)).items[0]
    with_names = box.run(SearchContext.card_run(run, lambda name: name == 'logs')).items[0].row

    # then
    assert plain['id'] == 'ok' and plain['cards'] == [KEY] and plain['question'] == 'why is checkout slow' and 'queries' not in plain
    assert queried.score == 1.0
    assert with_names['queries'] == [{'tool': 'logs', 'input': 'query one', 'rows': 3}]


def test_cards_open_and_changes_are_visible_to_everyone_including_public_read(box, store):
    # given
    first = '2026-11-20T09:00:00Z'
    did = add_closed(store, first=first)
    box.put('card', did, svc=SVC, label='High CPU', sev='warn')
    open_key = 'mem|shop/api|pod-b'
    store.anomalies.replace_one({'_id': open_key}, {'_id': open_key, 'key': open_key, 'status': 'open', 'svc': 'api', 'label': 'Memory', 'sev': 'high', 'firstSeen': first, 'what': 'memory near limit'}, upsert=True)
    box.put('open', open_key, svc='api', label='Memory', sev='high')
    store.add_deploys([{'ns': 'shop', 'svc': SVC, 'toImage': 'reg/app:2', 'fromImage': 'reg/app:1', 'at': '2026-11-20T08:00:00Z', 'imageBy': 'Fix pool leak', 'tickets': ['OPS-123']}])
    deploy = store.deploys.find_one({})['_id']
    box.put('change', f'deploy:{deploy}', svc=SVC, ns='shop')
    store.add_deploy_report({'summary': '1 deploy: 1 flagged', 'rows': [], 'brief': {'headline': 'Errors up', 'points': ['p']}})
    report = store.deploy_reports.find_one({})['_id']
    box.put('change', f'report:{report}', svc='', ns='')

    # when
    results = [box.run(ctx, limit=20) for role in ROLES for _, ctx in contexts(role)]

    # then
    for found in results:
        assert refs(found, 'card') == [did]
        assert refs(found, 'open') == [open_key]
        assert sorted(refs(found, 'change')) == sorted([f'deploy:{deploy}', f'report:{report}'])
    rows = {h.ref: h.row for h in box.run(SearchContext.ui('anonymous'), limit=20).items}
    assert rows[did]['key'] == KEY and rows[open_key]['what'] == 'memory near limit'
    assert rows[f'deploy:{deploy}']['toImage'] == 'reg/app:2' and rows[f'report:{report}']['headline'] == 'Errors up'


def test_cards_that_no_longer_pass_their_reader_are_dropped(box, store):
    # given
    first = '2026-11-20T09:00:00Z'
    live = add_closed(store, first=first)
    store.anomalies.replace_one({'_id': KEY}, {'_id': KEY, 'key': KEY, 'status': 'open', 'svc': SVC, 'label': 'High CPU', 'sev': 'warn', 'firstSeen': first}, upsert=True)
    info_key = 'noise|shop/info|x'
    info = add_closed(store, key=info_key, first=first)
    store.anomalies.replace_one({'_id': info_key}, {'_id': info_key, 'key': info_key, 'status': 'open', 'svc': 'info', 'label': 'Noise', 'sev': 'info', 'firstSeen': '2026-11-22T09:00:00Z'}, upsert=True)
    old = add_closed(store, key='old|shop/x|y', first='2026-10-01T09:00:00Z')
    muted_key = 'mut|shop/muted|z'
    store.anomalies.replace_one({'_id': muted_key}, {'_id': muted_key, 'key': muted_key, 'status': 'open', 'svc': 'm', 'label': 'M', 'sev': 'high', 'muted': 'noisy', 'firstSeen': first}, upsert=True)
    closed_open = 'cl|shop/closed|z'
    store.anomalies.replace_one({'_id': closed_open}, {'_id': closed_open, 'key': closed_open, 'status': 'cleared', 'svc': 'c', 'label': 'C', 'sev': 'high', 'firstSeen': first}, upsert=True)
    for ref in (live, info, old):
        box.put('card', ref, svc=SVC)
    for ref in (muted_key, closed_open, 'missing'):
        box.put('open', ref, svc=SVC)

    # when
    result = box.run(SearchContext.ui('viewer'), limit=20)

    # then
    assert refs(result, 'card') == [old]
    assert refs(result, 'open') == []


def test_knowledge_is_rechecked_against_the_usable_set_at_query_time(box):
    # given
    note = Knowledge('pool', 'Pool notes', 'How the checkout pool saturates.', slack=True)
    box.items = [('shop', '', note)]
    box.put('knowledge', 'shop:pool', plugin='shop', slack=True)
    ui = SearchContext.ui('viewer')

    # when
    first = box.run(ui)
    box.items = []
    second = box.run(ui)

    # then
    assert first.items[0].row == {'plugin': 'shop', 'id': 'pool', 'title': 'Pool notes', 'excerpt': 'How the checkout pool saturates.'}
    assert second.items == []


def test_usable_knowledge_drops_held_plugins():
    # given
    note = Knowledge('pool', 'Pool notes', 'text')
    gated = types.SimpleNamespace(id='gated', checks=[], tools=[types.SimpleNamespace(needs_field='token')], knowledge=[note])
    plain = types.SimpleNamespace(id='plain', checks=[], tools=[], knowledge=[note])
    empty = types.SimpleNamespace(name='a', values={}, secrets={})
    runtime = types.SimpleNamespace(serving=lambda allowed=None: [(gated, [empty]), (plain, [])])

    # when
    found = list(sr.usable_knowledge(runtime))

    # then
    assert [p for p, _, _ in found] == ['plain']


def test_knowledge_in_asked_runs_needs_slack_true(box):
    # given
    box.items = [('shop', '', Knowledge('open', 'Open note', 'visible text', slack=True)), ('shop', '', Knowledge('closed', 'Closed note', 'private text', slack=False))]
    box.put('knowledge', 'shop:open', plugin='shop', slack=True)
    box.put('knowledge', 'shop:closed', plugin='shop', slack=False)
    box.put('knowledge', 'shop:flipped', plugin='shop', slack=True)
    run = Run(read_only=True, ask=True, report=False, chat=False, keys=False, asker=True, shadow=False, role='viewer')

    # when
    asked = box.run(SearchContext.asked_run(run), limit=20)
    card = box.run(SearchContext.card_run(), limit=20)
    ui = box.run(SearchContext.ui('viewer'), limit=20)

    # then
    assert refs(asked, 'knowledge') == ['shop:open']
    assert sorted(refs(card, 'knowledge')) == ['shop:closed', 'shop:open']
    assert sorted(refs(ui, 'knowledge')) == ['shop:closed', 'shop:open']


def test_stale_slack_flag_in_the_index_does_not_override_the_live_note(box):
    # given
    box.items = [('shop', '', Knowledge('closed', 'Closed note', 'private text', slack=False))]
    box.put('knowledge', 'shop:closed', plugin='shop', slack=True)
    run = Run(read_only=True, ask=True, report=False, chat=False, keys=False, asker=True, shadow=False, role='admin')

    # when
    asked = box.run(SearchContext.asked_run(run))

    # then
    assert asked.items == []


def test_knowledge_in_asked_runs_respects_the_questions_gate(box):
    # given
    seen_gates = []
    box.searcher.knowledge = lambda allowed: seen_gates.append(allowed) or []
    box.put('knowledge', 'shop:pool', plugin='shop', slack=True)
    run = Run(read_only=True, ask=True, report=False, chat=False, keys=False, asker=True, shadow=False, role='viewer')

    # when
    box.run(SearchContext.asked_run(run))
    box.run(SearchContext.ui('viewer'))

    # then
    assert callable(seen_gates[0]) and seen_gates[1] is None
    hidden = types.SimpleNamespace(values={'questions': 'none'})
    assert seen_gates[0](hidden) is False


def test_anonymous_gets_no_knowledge_doc_or_errtpl(box, store):
    # given
    box.items = [('shop', '', Knowledge('pool', 'Pool notes', 'text', slack=True))]
    box.put('knowledge', 'shop:pool', plugin='shop', slack=True)
    box.put('doc', 'd1', plugin='confluence', instance='main')
    box.put('errtpl', 'e1', plugin='loki', instance='main')
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')
    anon = SearchContext.ui('anonymous')

    # when
    result = box.run(anon, kinds=['knowledge', 'doc', 'errtpl', 'run'], limit=20)

    # then
    assert refs(result) == ['ok']
    assert sr.allowed_kinds(anon) == sr.PUBLIC_KINDS
    assert set(sr.allowed_kinds(SearchContext.ui('viewer'))) == set(sr.PUBLIC_KINDS + sr.SIGNED_IN_KINDS)
    assert box.searcher.hydrate_knowledge(anon, ['shop:pool']) == {}


def instance(name: str, questions: str = 'everyone'):
    return types.SimpleNamespace(name=name, values={'questions': questions})


def fixture_row(plugin='confluence', name='main') -> dict:
    return {'kind': 'doc', 'ref': 'd1', 'part': 0, 'f': {'plugin': plugin, 'instance': name, 'space': 'OPS'}}


@pytest.mark.parametrize('questions, role, ask_ok', [('everyone', 'viewer', True), ('operators', 'viewer', False), ('operators', 'operator', True), ('operators', 'admin', True), ('none', 'admin', False),
                                                      ('none', 'operator', False), ('none', 'viewer', False)])
def test_doc_and_errtpl_gate_for_asks_and_ui(box, questions, role, ask_ok):
    # given
    plugin = types.SimpleNamespace(id='confluence', fields=[object()])
    box.insts = [(plugin, [instance('main', questions)])]
    run = Run(read_only=True, ask=True, report=False, chat=False, keys=False, asker=True, shadow=False, role=role)
    row = fixture_row()

    # when
    asked = box.searcher.instance_open(SearchContext.asked_run(run), row['f'])
    shown = box.searcher.instance_open(SearchContext.ui(role), row['f'])
    card = box.searcher.instance_open(SearchContext.card_run(), row['f'])

    # then
    assert asked is ask_ok
    assert shown is ask_ok
    assert card is True


def test_doc_gate_for_card_runs_serves_questions_none_and_refuses_the_rest(box):
    # given
    plugin = types.SimpleNamespace(id='confluence', fields=[object()])
    box.insts = [(plugin, [instance('main', 'none')])]

    # when
    card = SearchContext.card_run()

    # then
    assert box.searcher.instance_open(card, fixture_row()['f']) is True
    assert box.searcher.instance_open(card, fixture_row(name='other')['f']) is False
    assert box.searcher.instance_open(card, fixture_row(plugin='sharepoint')['f']) is False
    assert box.searcher.instance_open(card, {'plugin': 'confluence'}) is False
    assert box.searcher.instance_open(card, {'instance': 'main'}) is False
    assert box.searcher.instance_open(card, None) is False


def test_doc_gate_refuses_anonymous_and_instances_not_serving(box):
    # given
    plugin = types.SimpleNamespace(id='confluence', fields=[object()])
    box.insts = [(plugin, [instance('main')])]

    # when
    anonymous = box.searcher.instance_open(SearchContext.ui('anonymous'), fixture_row()['f'])
    bogus = box.searcher.instance_open(SearchContext.ui('bogus'), fixture_row()['f'])
    box.insts = []
    none = box.searcher.instance_open(SearchContext.ui('admin'), fixture_row()['f'])

    # then
    assert anonymous is False
    assert bogus is False
    assert none is False


def test_a_hydration_exception_drops_the_hits_of_that_kind_only(box, store):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')
    did = add_closed(store)
    box.put('card', did, svc=SVC)

    def broken(ctx, ids):
        raise RuntimeError('boom')

    box.searcher.hydrators['run'] = broken

    # when
    result = box.run(SearchContext.ui('viewer'), limit=20)

    # then
    assert refs(result, 'run') == [] and refs(result, 'card') == [did]


def test_a_row_that_fails_to_build_is_dropped_alone(box, store, monkeypatch):
    # given
    first = add_closed(store, key='a|shop/a|1', first='2026-11-20T09:00:00Z')
    second = add_closed(store, key='b|shop/b|2', first='2026-11-20T09:05:00Z')
    box.put('card', first, svc=SVC)
    box.put('card', second, svc=SVC)
    real = sr.history.closed_row

    def flaky(doc):
        if doc['key'].startswith('a|'):
            raise ValueError('bad row')
        return real(doc)

    monkeypatch.setattr(sr.history, 'closed_row', flaky)

    # when
    result = box.run(SearchContext.ui('viewer'), limit=20)

    # then
    assert refs(result) == [second]


def test_a_mongo_error_raises_a_503_and_returns_no_rows(box, store, monkeypatch):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')

    def down(*a, **k):
        raise AutoReconnect('gone')

    monkeypatch.setattr(store.investigations, 'find', down)

    # when
    with pytest.raises(SearchUnavailable) as caught:
        box.run(SearchContext.ui('viewer'))

    # then
    assert caught.value.status_code == 503


def test_a_mongo_error_in_the_status_read_raises_too(box, store, monkeypatch):
    # given
    box.put('run', 'ok', svc=SVC, trigger='review')

    def down(*a, **k):
        raise AutoReconnect('gone')

    monkeypatch.setattr(store, 'embed_state', down)

    # when
    with pytest.raises(SearchUnavailable) as caught:
        box.run(SearchContext.ui('viewer'))

    # then
    assert caught.value.status_code == 503


def test_a_mongo_error_in_the_index_load_raises_too(box, store, monkeypatch):
    # given
    box.put('run', 'ok', svc=SVC, trigger='review')

    def down(*a, **k):
        raise AutoReconnect('gone')

    monkeypatch.setattr(store, 'embeddings_since', down)

    # when
    with pytest.raises(SearchUnavailable) as caught:
        box.run(SearchContext.ui('viewer'))

    # then
    assert caught.value.status_code == 503


def test_a_mongo_error_in_the_word_fallback_raises(box, store, monkeypatch):
    # given
    box.on = False

    def down(*a, **k):
        raise AutoReconnect('gone')

    monkeypatch.setattr(store, 'closed_cards', down)

    # when
    with pytest.raises(SearchUnavailable) as caught:
        box.run(SearchContext.ui('viewer'))

    # then
    assert caught.value.status_code == 503


def seed_word_matches(store) -> str:
    add_run(store, 'words', recap='Checkout latency came from the retry storm.')
    return add_closed(store, label='checkout CPU', note={'text': 'checkout pool'})


@pytest.mark.parametrize('setup, reason', [
    (lambda b: setattr(b, 'on', False), 'turned off in Admin'),
    (lambda b: setattr(b, 's', conf(embed_url='')), 'no embedding service address'),
    (lambda b: setattr(b.fake, 'down', 'down'), 'not answering'),
    (lambda b: setattr(b.fake, 'down', 'auth'), 'did not accept the key'),
    (lambda b: setattr(b.fake, 'down', 'mismatch'), 'another model'),
    (lambda b: setattr(b.fake, 'down', 'refused'), 'public and not allowed'),
], ids=['feature-off', 'url-empty', 'pod-down', 'auth', 'mismatch', 'refused'])
def test_unavailable_service_falls_back_to_words_in_the_ui_only(box, store, setup, reason):
    # given
    seed_word_matches(store)
    box.put('run', 'words', svc=SVC, trigger='review')
    setup(box)
    run = Run(read_only=True, ask=True, report=False, chat=False, keys=False, asker=True, shadow=False, role='admin')

    # when
    ui = box.run(SearchContext.ui('viewer'), text='checkout')
    asked = box.run(SearchContext.asked_run(run), text='checkout')
    card = box.run(SearchContext.card_run(), text='checkout')

    # then
    assert ui.semantic is False and reason in ui.note and ui.note.startswith('Search by meaning is off: ')
    assert {h.kind for h in ui.items} == {'run', 'card'} and all(h.score is None for h in ui.items)
    assert asked.semantic is False and reason in asked.note and asked.items == []
    assert card.semantic is False and card.items == []


def test_a_tripped_breaker_reports_down_before_any_call(box, store):
    # given
    seed_word_matches(store)
    box.put('run', 'words', svc=SVC, trigger='review')
    for _ in range(3):
        box.fake.breaker.failure('down')

    # when
    result = box.run(SearchContext.ui('viewer'), text='checkout')

    # then
    assert result.semantic is False and 'not answering' in result.note and box.fake.calls == []
    assert {h.kind for h in result.items} == {'run', 'card'}


def test_an_index_that_is_not_ready_falls_back_with_its_note(box, store):
    # given
    seed_word_matches(store)

    # when
    result = box.run(SearchContext.ui('viewer'), text='checkout')
    asked = box.run(SearchContext.card_run(), text='checkout')

    # then
    assert result.semantic is False and 'the index is not ready yet' in result.note and refs(result, 'run') == ['words']
    assert asked.semantic is False and asked.items == []


def test_an_index_for_another_fingerprint_is_not_ready(box, store):
    # given
    seed_word_matches(store)
    box.put('run', 'words', svc=SVC, trigger='review')
    box.s = dataclasses.replace(box.s, embed_passage_prefix='doc: ')

    # when
    result = box.run(SearchContext.ui('viewer'), text='checkout')

    # then
    assert result.semantic is False and 'not ready' in result.note


def test_a_query_vector_of_the_wrong_length_falls_back(box, store):
    # given
    seed_word_matches(store)
    box.put('run', 'words', svc=SVC, trigger='review')
    box.fake.embed_queries = lambda texts: [[1.0] * 8]

    # when
    result = box.run(SearchContext.ui('viewer'), text='checkout')

    # then
    assert result.semantic is False and 'another model' in result.note


def test_word_fallback_matches_ask_and_recap_and_never_tool_inputs(box, store):
    # given
    add_run(store, 'by-ask', recap='nothing')
    add_run(store, 'by-input', recap='nothing', ask={'text': 'other'}, toolCalls=[{'name': 'logs', 'input': 'checkout-needle'}])
    add_run(store, 'held', outcome='held', recap='checkout held')
    add_run(store, 'chat', trigger='chat', recap='checkout chat')
    box.on = False

    # when
    result = box.run(SearchContext.ui('viewer'), text='checkout', kinds=['run'])
    needle = box.run(SearchContext.ui('viewer'), text='checkout-needle', kinds=['run'])

    # then
    assert refs(result) == ['by-ask']
    assert needle.items == []
    assert not seen([h.row for h in result.items]) & FORBIDDEN


def test_word_fallback_follows_kinds_limit_and_service(box, store):
    # given
    for n in range(4):
        add_run(store, f'r{n}', started=f'2026-11-20T10:0{n}:00Z', keys=['cpu|shop/other-service|x'] if n == 0 else [KEY], ask={'text': 'checkout slow'})
    add_closed(store, label='checkout')
    box.on = False
    ui = SearchContext.ui('viewer')

    # when
    only_cards = box.run(ui, text='checkout', kinds=['card'])
    limited = box.run(ui, text='checkout', limit=2)
    scoped = box.run(ui, text='checkout', service=SVC, kinds=['run'], limit=20)

    # then
    assert {h.kind for h in only_cards.items} == {'card'}
    assert len(limited.items) == 2
    assert sorted(refs(scoped)) == ['r1', 'r2', 'r3']


def blended(base: np.ndarray, cosine: float) -> list[float]:
    other = np.array(embed_text('completely unrelated words about holidays'))
    other = other - (other @ base) * base
    other = other / np.linalg.norm(other)
    return list(cosine * base + np.sqrt(1 - cosine ** 2) * other)


def test_hits_below_the_threshold_are_dropped_and_scores_are_rounded(box, store):
    # given
    base = np.array(embed_text(QUERY))
    for ref, cosine in (('strong', 0.86), ('edge-in', 0.82), ('edge-out', 0.78), ('weak', 0.4)):
        add_run(store, ref)
        box.put('run', ref, svc=SVC, trigger='review', vec=blended(base, cosine))

    # when
    result = box.run(SearchContext.ui('viewer'), limit=20)

    # then
    assert refs(result) == ['strong', 'edge-in']
    assert [h.score for h in result.items] == [0.86, 0.82]
    assert all(round(h.score, 2) == h.score for h in result.items)


def test_threshold_is_keyed_by_model():
    # given
    sr.MIN_SCORES['other/model'] = 0.5

    # when
    try:
        scores = [sr.min_score('other/model'), sr.min_score(EMBED_MODEL), sr.min_score('unknown/model')]
    finally:
        del sr.MIN_SCORES['other/model']

    # then
    assert scores == [0.5, 0.80, 0.80]


def test_limit_is_respected_and_parts_count_once(box, store):
    # given
    for n in range(8):
        add_run(store, f'r{n}', started=f'2026-11-20T10:0{n}:00Z')
        for part in range(3):
            box.put('run', f'r{n}', part=part, svc=SVC, trigger='review')

    # when
    three = box.run(SearchContext.ui('viewer'), limit=3)
    many = box.run(SearchContext.ui('viewer'), limit=500)

    # then
    assert len(three.items) == 3 and len(set(refs(three))) == 3
    assert len(many.items) == 8 and len(set(refs(many))) == 8
    assert sr.MAX_LIMIT == 20


def test_service_filter_keeps_only_that_service(box, store):
    # given
    add_run(store, 'a')
    add_run(store, 'b', keys=['cpu|shop/other-service|x'])
    add_run(store, 'c')
    box.put('run', 'a', svc=SVC, trigger='review')
    box.put('run', 'b', svc='other-service', trigger='review')
    box.put('run', 'c', trigger='review')

    # when
    scoped = box.run(SearchContext.ui('viewer'), service=SVC, limit=20)
    everything = box.run(SearchContext.ui('viewer'), limit=20)

    # then
    assert refs(scoped) == ['a']
    assert sorted(refs(everything)) == ['a', 'b', 'c']


def test_unknown_kinds_and_unhydrated_kinds_never_return_rows(box, store):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')
    box.put('doc', 'd1', plugin='confluence', instance='main')
    box.put('errtpl', 'e1', plugin='loki', instance='main')

    # when
    bogus = box.run(SearchContext.ui('admin'), kinds=['bogus', 'vectors', 3])
    everything = box.run(SearchContext.ui('admin'), limit=20)

    # then
    assert bogus.semantic is True and bogus.items == []
    assert refs(everything) == ['ok']


def test_parse_id_splits_kind_ref_and_part_even_when_the_ref_has_colons():
    # when
    parsed = sr.parse_id('change:deploy:abc:0')

    # then
    assert parsed == ('change', 'deploy:abc')
    assert sr.parse_id('change:deploy:abc:0') == ('change', 'deploy:abc')
    assert sr.parse_id('open:cpu|shop/x:y|z:2') == ('open', 'cpu|shop/x:y|z')
    assert sr.parse_id('bogus:x:0') is None
    assert sr.parse_id('run:x') is None
    assert sr.parse_id('run:x:no') is None
    assert sr.parse_id(None) is None


def test_rebuilding_is_reported_for_the_active_fingerprint(box, store):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')
    store.save_embed_state('embed-status', {'fp': box.fp, 'rebuilding': {'done': 3, 'of': 10}})

    # when
    result = box.run(SearchContext.ui('viewer'))
    store.save_embed_state('embed-status', {'fp': 'other', 'rebuilding': {'done': 3, 'of': 10}})
    stale = box.run(SearchContext.ui('viewer'))

    # then
    assert result.semantic is True and result.rebuilding == {'done': 3, 'of': 10} and '3 of 10' in result.note and refs(result) == ['ok']
    assert stale.rebuilding is None and stale.note is None


def test_the_role_is_fixed_at_construction(box):
    # given
    ctx = SearchContext.ui('admin')

    # when
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.role = 'viewer'

    # then
    assert SearchContext.ui('root').role == 'anonymous'
    assert SearchContext.ui(None).role == 'anonymous'
    with pytest.raises(ValueError):
        SearchContext(None, 'bogus')


def test_empty_text_is_refused(box):
    # when
    with pytest.raises(ValueError) as caught:
        box.run(SearchContext.ui('viewer'), text='   ')

    # then
    assert caught.type is ValueError


def test_a_query_sent_to_the_embedder_is_trimmed(box, store):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')

    # when
    box.run(SearchContext.ui('viewer'), text='  ' + QUERY + '  ')

    # then
    assert box.fake.calls == [[QUERY]]


def test_an_index_failure_during_the_search_falls_back(box, store, monkeypatch):
    # given
    seed_word_matches(store)
    box.put('run', 'words', svc=SVC, trigger='review')
    box.run(SearchContext.ui('viewer'), text='checkout')

    def broken(*a, **k):
        raise RuntimeError('boom')

    monkeypatch.setattr(box.index, 'search', broken)

    # when
    ui = box.run(SearchContext.ui('viewer'), text='checkout')
    asked = box.run(SearchContext.card_run(), text='checkout')
    monkeypatch.setattr(box.index, 'search', lambda *a, **k: (_ for _ in ()).throw(sr.NotReady('x')))
    late = box.run(SearchContext.ui('viewer'), text='checkout')

    # then
    assert ui.semantic is False and 'not answering' in ui.note and refs(ui, 'run') == ['words']
    assert asked.semantic is False and asked.items == []
    assert late.semantic is False and 'not ready' in late.note


def test_investigator_card_rows_match_the_past_cards_shape(box, store):
    # given
    first = '2026-11-20T09:00:00Z'
    did = add_closed(store, first=first)
    store.card_history.update_one({'_id': did}, {'$set': {'note': {'text': 'pool leak found'}, 'noteBy': 'pod', 'clearedAt': '2026-11-20T10:00:00Z', 'outcome': 'resolved',
                                                           'asks': [{'at': first, 'line': 'why'}], 'events': [{'at': first}], 'said': [{'x': 1}]}})
    box.put('card', did, svc=SVC)
    want = [r for r in store.past_cards(key=KEY) if r.get('firstSeen') == first][0]

    # when
    row = box.run(SearchContext.card_run()).items[0].row

    # then
    assert row == {k: v for k, v in want.items() if k != 'match'} | {'match': 'semantic'}
    assert row.get('asks') == 1 and row.get('durationS') == 3600 and row['cause']['text'] == 'pool leak found'
    assert not seen(row) & FORBIDDEN


def test_deploy_rows_carry_the_ignored_flag(box, store):
    # given
    store.add_deploys([{'ns': 'shop', 'svc': SVC, 'toImage': 'reg/app:2', 'fromImage': 'reg/app:1', 'at': '2026-11-20T08:00:00Z'},
                       {'ns': 'shop', 'svc': 'other', 'toImage': 'reg/o:2', 'fromImage': 'reg/o:1', 'at': '2026-11-20T08:00:00Z'}])
    ids = {d['svc']: d['_id'] for d in store.deploys.find({})}
    for svc, ref in ids.items():
        box.put('change', f'deploy:{ref}', svc=svc, ns='shop')
    store.release_ignore = (SVC,)

    # when
    rows = {h.row['svc']: h.row for h in box.run(SearchContext.ui('viewer'), limit=20).items}

    # then
    assert rows[SVC]['ignored'] is True
    assert 'ignored' not in rows['other']


@pytest.mark.parametrize('ctx', [c for _, c in ALL], ids=[n for n, _ in ALL])
def test_guard_refused_asks_never_appear(box, store, ctx):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')
    add_run(store, 'refused', error='Refused by the guard', recap='refused text checkout')
    box.put('run', 'refused', svc=SVC, trigger='review')

    # when
    result = box.run(ctx, limit=20)

    # then
    assert refs(result, 'run') == ['ok']
    assert 'error' not in seen([h.row for h in result.items])


def test_guard_refused_asks_are_not_in_the_word_fallback(box, store):
    # given
    add_run(store, 'ok', recap='checkout fine')
    add_run(store, 'refused', error='Refused by the guard', recap='checkout refused')
    box.on = False

    # when
    result = box.run(SearchContext.ui('viewer'), text='checkout', kinds=['run'])

    # then
    assert refs(result) == ['ok']


SECRET = 'AKIAIOSFODNN7EXAMPLE'


def test_a_secret_and_an_email_in_the_query_never_reach_the_embedder(box, store):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')

    # when
    result = box.run(SearchContext.ui('viewer'), text=f'pool exhausted for alice@example.com with key {SECRET}')

    # then
    sent = box.fake.texts
    assert len(sent) == 1 and SECRET not in sent[0] and 'alice@example.com' not in sent[0] and 'pool exhausted' in sent[0]
    assert result.semantic is True


def test_a_masking_timeout_falls_back_without_calling_the_service(box, store, monkeypatch):
    # given
    add_run(store, 'ok')
    box.put('run', 'ok', svc=SVC, trigger='review')
    monkeypatch.setattr(chunks, 'redact_text', lambda text, limit=None: sr.TIMEOUT_MARK)

    # when
    result = box.run(SearchContext.ui('viewer'))

    # then
    assert result.semantic is False and result.note and box.fake.calls == []


def test_a_query_that_masks_to_nothing_falls_back_without_calling_the_service(box, store):
    # given
    add_run(store, 'ok')

    # when
    result = box.run(SearchContext.card_run(), text=SECRET)

    # then
    assert result.semantic is False and result.note and result.items == [] and box.fake.calls == []


def test_the_word_fallback_still_reads_the_raw_text_locally(box, store, monkeypatch):
    # given
    add_run(store, 'ok', recap='The retry storm hit alice@example.com')
    monkeypatch.setattr(chunks, 'redact_text', lambda text, limit=None: sr.TIMEOUT_MARK)

    # when
    result = box.run(SearchContext.ui('viewer'), text='alice@example.com')

    # then
    assert result.semantic is False and refs(result) == ['ok'] and box.fake.calls == []


def test_hits_far_below_the_best_hit_are_dropped(box, store):
    # given
    base = np.array(embed_text(QUERY))
    for ref, cosine in (('best', 0.95), ('near', 0.91), ('far', 0.83)):
        add_run(store, ref)
        box.put('run', ref, svc=SVC, trigger='review', vec=blended(base, cosine))

    # when
    result = box.run(SearchContext.ui('viewer'), limit=20)

    # then
    assert refs(result) == ['best', 'near']


def test_the_relative_cut_is_keyed_by_model():
    # given
    sr.RELATIVE_CUTS['other/model'] = 0.2

    # when
    try:
        cuts = [sr.relative_cut('other/model'), sr.relative_cut(EMBED_MODEL), sr.relative_cut('unknown/model')]
    finally:
        del sr.RELATIVE_CUTS['other/model']

    # then
    assert cuts == [0.2, 0.05, 0.05]


def test_the_search_module_imports_first_in_a_fresh_interpreter():
    # given
    import subprocess
    import sys

    # when
    done = subprocess.run([sys.executable, '-c', 'import skopax.semantic.search'], capture_output=True, text=True, timeout=120)

    # then
    assert done.returncode == 0, done.stderr
