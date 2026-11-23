import dataclasses

import anthropic
import httpx2
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings
from tests.fake_anthropic import reply, text
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance
from tests.test_investigator import KEY, NOW, investigator, note_and_recap, seed
from tests.test_tracing import PROJECT, URL, attrs, make, spans
from skopax import tracing
from skopax.app import create_app
from skopax.config import load_settings
from skopax.engine import iso
from skopax.messages import msg
from skopax.plugins import langfuse
from skopax.registry import Registry
from skopax.sessions import master_key
from skopax.store import Store
from skopax.tracing import OTLP, PROJECTS
from skopax.tunables import STATIC

PK, SK = 'pk-lf-trace', 'sk-lf-trace-SECRET'
VALUES = {'url': URL, 'public-key': PK, 'secret-key': SK}
NEEDS = msg('features.needs.trace_instance')
OTHER = 'log|b|ERROR|y'


def boom() -> Exception:
    return anthropic.APIConnectionError(request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))


def test_a_traced_run_notes_its_trace_only_once_sent_and_the_board_links_it(installed, store: Store):
    # given
    found, http = make()
    installed(found)
    seed(store, anomaly(KEY, firstSeen=iso(NOW - 600)))
    inv = investigator(store, note_and_recap())

    # when
    record = inv.run_once(NOW)
    queued = store.list_investigations(KEY, 1)[0].get('trace')
    found.drain()
    noted = store.list_investigations(KEY, 1)[0]['trace']
    board = inv.board(NOW)

    # then
    assert record['outcome'] == 'done' and 'trace' not in record and queued is None
    assert (noted['url'], noted['project'], len(noted['id'])) == (URL, PROJECT, 32)
    root = next(s for s in spans(http) if 'parentSpanId' not in s)
    a = attrs(root)
    assert (root['traceId'], a['langfuse.trace.name'], a['langfuse.trace.metadata.runId'], a['langfuse.trace.metadata.cards'], a['langfuse.trace.metadata.provider'],
            a['langfuse.trace.metadata.trigger']) == (noted['id'], 'card run', record['id'], KEY, 'anthropic', 'cards')
    kinds = [attrs(s)['langfuse.observation.type'] for s in spans(http) if 'parentSpanId' in s]
    assert kinds == ['generation', 'tool', 'generation']
    tracing.uninstall(found)
    assert inv.board(NOW)['recent'][0]['traceUrl'] == board['recent'][0]['traceUrl'] == f'{URL}/project/{PROJECT}/traces/{noted["id"]}', 'the link comes from where it was sent'


def test_a_sampled_out_run_has_no_trace_but_a_failed_one_is_always_sent(installed, store: Store):
    # given
    found, http = make(sample=0)
    installed(found)
    seed(store, anomaly(KEY, firstSeen=iso(NOW - 600)))

    # when
    kept_out = investigator(store, note_and_recap()).run_once(NOW)
    seed(store, anomaly(OTHER, firstSeen=iso(NOW - 600)))
    failed = investigator(store, [boom()]).run_once(NOW + 1)
    found.drain()

    # then
    runs = {r['id']: r for r in store.list_investigations(None, 5)}
    assert kept_out['outcome'] == 'done' and 'trace' not in runs[kept_out['id']]
    assert failed['outcome'] == 'failed' and runs[failed['id']]['trace']['id'] in {s['traceId'] for s in spans(http)}
    root = next(s for s in spans(http) if 'parentSpanId' not in s)
    assert root['status'] == {'code': 2, 'message': 'failed: APIConnectionError'}
    board = investigator(store, []).board(NOW + 2)
    assert [bool(r.get('traceUrl')) for r in board['recent']] == [True, False]


def test_without_a_tracer_or_switched_off_a_run_carries_no_trace(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen=iso(NOW - 600)))

    # when
    record = investigator(store, note_and_recap()).run_once(NOW)

    # then
    assert record['outcome'] == 'done' and 'trace' not in store.list_investigations(KEY, 1)[0]


def test_the_feature_is_off_by_default_and_the_settings_default_to_metadata_and_every_trace():
    # given
    base = load_settings(env={})
    tunables = {t.key: t for t in STATIC}

    # then
    assert (base.langfuse_tracing_on, base.langfuse_trace_instance, base.langfuse_trace_content, base.langfuse_trace_sample) == (False, '', 'metadata', 100)
    assert load_settings(env={'LANGFUSE_TRACE_CONTENT': 'everything', 'LANGFUSE_TRACE_SAMPLE': '250'}).langfuse_trace_content == 'metadata'
    assert load_settings(env={'LANGFUSE_TRACE_SAMPLE': '250'}).langfuse_trace_sample == 100
    assert load_settings(env={'SKOPAX_FEATURE_LANGFUSE_TRACING': 'true', 'LANGFUSE_TRACE_CONTENT': 'full', 'LANGFUSE_TRACE_SAMPLE': '25'}).langfuse_trace_sample == 25
    assert tunables['langfuse.trace-content'].choices == ('metadata', 'full') and 'Warning' in tunables['langfuse.trace-content'].help
    assert (tunables['langfuse.trace-sample'].low, tunables['langfuse.trace-sample'].high) == (0, 100)


def served(store: Store, http: FakeHttp, **over) -> TestClient:
    add_instance(store, langfuse.PLUGIN, 'main', sealer_key=master_key(store.db, settings()), primary=True, **VALUES)
    return TestClient(create_app(dataclasses.replace(settings(), **over), store, registry=Registry.of(langfuse.PLUGIN), transport=http))


def feature(c: TestClient) -> dict:
    return next(i for i in c.get('/api/admin/features', headers=AUTH).json()['items'] if i['id'] == 'langfuse-tracing')


def setting(c: TestClient, key: str) -> dict:
    return next(s for s in c.get('/api/admin/settings', headers=AUTH).json()['items'] if s['key'] == key)


def test_admin_switches_tracing_on_picks_a_configured_instance_and_the_note_goes_away(store: Store):
    # given
    http = FakeHttp({('POST', URL + OTLP): {}, ('GET', URL + PROJECTS): {'data': [{'id': PROJECT}]}})
    with served(store, http) as c:
        tracer = c.app.state.tracer
        before = feature(c)
        switched = c.put('/api/admin/features/langfuse-tracing', json={'on': True}, headers=AUTH).json()
        unpicked = (tracer.active(), switched['needs'], c.app.state.runtime.setting('langfuse', 'traced-instance'))
        offered = setting(c, 'langfuse.trace-instance')
        refused = c.put('/api/admin/settings', json={'key': 'langfuse.trace-instance', 'value': 'nope'}, headers=AUTH)

        # when
        picked = c.put('/api/admin/settings', json={'key': 'langfuse.trace-instance', 'value': 'main'}, headers=AUTH)
        after = feature(c)
        target = tracer.target()
        cached = tracer.target() is target
        traced = c.app.state.runtime.setting('langfuse', 'traced-instance')
        with tracing.opened('check') as trace:
            trace.generation('anthropic', 'once', 'claude-haiku-5-5', 1.0, 2.0)
        tracer.drain()

    # then
    assert (before['on'], before['needs']) == (False, [NEEDS])
    assert unpicked == (False, [NEEDS], ''), 'no instance picked, so the check still watches every one'
    assert offered['choices'] == ['', 'main'] and offered['card'] == 'Langfuse tracing'
    assert refused.status_code == 422 and picked.status_code == 200
    assert after['on'] is True and after['needs'] == []
    assert (target.name, target.url, target.public) == ('main', URL, PK) and SK not in repr(target)
    assert cached and traced == 'main', 'the target is built once per settings revision, not per call'
    assert len(http.hits(URL + OTLP)) == 1
    assert tracing.installed() is None, 'the app takes its tracer out when it stops'
