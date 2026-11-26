import dataclasses
import time

from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings, state
from skopax import aiuses, clock, costs, settingcards
from skopax.app import create_app
from skopax.messages import msg
from skopax.store import Store
from skopax.tunables import Live, TunableStore, registry

KEYS = ('investigate.guard-model', 'guard.check-questions', 'guard.check-answers')
SWITCHES = KEYS[1:]
CARD = 'log|checkout-service|ERROR|boom'
TODAY = '2026-10-24'


def items(c: TestClient) -> dict:
    return {i['key']: i for i in c.get('/api/admin/settings', headers=AUTH).json()['items']}


def ledger(source: str, usd: float, run: str | None = None, guard_run: str | None = None, model: str = 'claude-haiku-5-5') -> dict:
    parts = dict.fromkeys(('cacheRead', 'input', 'cacheWrite5m', 'cacheWrite1h', 'output', 'webSearch'), 0.0) | {'input': usd}
    row = {'source': source, 'day': TODAY, 'at': f'{TODAY}T06:00:00Z', 'model': model, 'effort': 'low', 'runId': run, 'actionId': None, 'keys': [], 'by': None,
           'tokens': dict.fromkeys(parts, 0), 'usd': parts | {'total': usd}}
    return row | ({'use': 'guard', 'guardFor': 'answer', 'guardRun': guard_run} if source == 'guard' else {})


def held_run() -> dict:
    return {'id': 'r1', 'trigger': 'cards', 'mode': 'live', 'keys': [CARD], 'startedAt': '2026-11-26T10:00:00Z', 'outcome': 'held', 'usd': 0.4, 'recap': 'This answer is waiting for a review.',
            'writes': [], 'toolCalls': [], 'held': {'text': 'The token is sk-live-123.', 'writes': [{'tool': 'note', 'key': CARD, 'summary': 'x', 'args': {}}], 'until': '2026-11-27T10:00:00Z'},
            'guard': {'questions': None, 'answers': 'hold', 'masked': 0, 'ms': 40, 'model': 'claude-haiku-5-5', 'reason': 'names a live token'}}


def test_the_three_guard_keys_are_admin_settings_on_one_investigator_card():
    # given
    known = {t.key: t for t in registry()}

    # when
    found = [known[k] for k in KEYS]

    # then
    assert [(t.field, t.group, t.kind) for t in found] == [('investigate_guard_model', 'Investigator', 'choice'), ('guard_check_questions', 'Investigator', 'bool'),
                                                           ('guard_check_answers', 'Investigator', 'bool')]
    assert found[0].choices[0] == '' and dict(found[0].choice_labels or {})[''] == 'None'
    assert len({settingcards.CARD_OF[k][:2] for k in KEYS}) == 1 and settingcards.CARD_OF[KEYS[0]][0] == 'Investigator'


def test_the_switches_are_off_while_the_guard_model_is_empty_and_cannot_be_turned_on(client: TestClient):
    # given
    listed = items(client)

    # when
    refused = client.put('/api/admin/settings', json={'key': 'guard.check-answers', 'value': 'true'}, headers=AUTH)
    kept_off = client.put('/api/admin/settings', json={'key': 'guard.check-questions', 'value': 'false'}, headers=AUTH)

    # then
    assert [listed[k]['value'] for k in KEYS] == ['', 'false', 'false']
    assert all(listed[k]['off'] == msg('guard.needs_model') for k in SWITCHES) and 'off' not in listed[KEYS[0]]
    assert (refused.status_code, refused.json()['detail']) == (422, msg('guard.needs_model'))
    assert kept_off.status_code == 200


def test_with_a_guard_model_the_switches_turn_on_and_every_change_is_in_changes(store: Store):
    # given
    store.save(state(), None)
    c = TestClient(create_app(settings(), store))

    # when
    model = c.put('/api/admin/settings', json={'key': 'investigate.guard-model', 'value': 'claude-haiku-5-5'}, headers=AUTH)
    on = [c.put('/api/admin/settings', json={'key': k, 'value': 'true'}, headers=AUTH) for k in SWITCHES]
    off = c.put('/api/admin/settings', json={'key': 'guard.check-answers', 'value': 'false'}, headers=AUTH)
    listed = items(c)
    history = c.get('/api/admin/history', headers=AUTH).json()
    cleared = c.put('/api/admin/settings', json={'key': 'investigate.guard-model', 'value': ''}, headers=AUTH)
    after = items(c)

    # then
    assert model.status_code == 200 and [r.status_code for r in on] == [200, 200] and off.status_code == 200
    assert [listed[k]['value'] for k in KEYS] == ['claude-haiku-5-5', 'true', 'false'] and not any(listed[k].get('off') for k in KEYS)
    assert [(h['key'], h['old'], h['new']) for h in history][:4] == [('guard.check-answers', 'true', 'false'), ('guard.check-answers', None, 'true'), ('guard.check-questions', None, 'true'),
                                                                     ('investigate.guard-model', None, 'claude-haiku-5-5')]
    assert (cleared.status_code, cleared.json()['detail']) == (422, msg('guard.checks_need_model'))
    assert after['investigate.guard-model']['value'] == 'claude-haiku-5-5' and after['guard.check-questions']['value'] == 'true'


def test_a_switch_left_on_without_a_model_says_so_and_can_be_turned_off(store: Store):
    # given
    base = dataclasses.replace(settings(), guard_check_questions=True)
    live = Live(base, TunableStore(store.db))
    known = {t.key: t for t in registry()}

    # when
    on = live.entry(known['guard.check-questions'])
    other = live.entry(known['guard.check-answers'])
    item, _ = live.set('guard.check-questions', 'false', 'admin@example.com')

    # then
    assert on['problem'] == msg('guard.no_model') and 'off' not in on
    assert other['off'] == msg('guard.needs_model') and 'problem' not in other
    assert item['value'] == 'false'


def test_the_guard_average_is_per_guarded_run_read_from_guard_run():
    # given
    rows = [ledger('guard', 0.01, guard_run='r1'), ledger('guard', 0.03, guard_run='r1'), ledger('guard', 0.02, guard_run='r2'), ledger('pod', 1.0, run='r1', model='claude-opus-5-5')]

    # when
    found = aiuses.averages(rows)

    # then
    assert found['guard'] == 0.03 and found['card'] == 1.0


def test_costs_show_a_guard_line_whose_runs_are_the_guarded_runs():
    # given
    rows = [ledger('guard', 0.01, guard_run='r1'), ledger('guard', 0.03, guard_run='r1'), ledger('guard', 0.02, guard_run='r2'), ledger('pod', 1.0, run='r1', model='claude-opus-5-5')]

    # when
    s = costs.summary(rows, {}, TODAY, 7, 1.06, 20.0)
    by = {x['source']: x for x in s['sources']}

    # then
    assert (by['guard']['calls'], by['guard']['runs'], by['guard']['usd']['total'], by['guard']['avgRun']) == (3, 2, 0.06, 0.03)
    assert [r['id'] for r in s['runs']] == ['r1'] and s['total']['runs'] == 1
    assert s['perBucket'][-1]['guard'] == 0.06 and s['guardOn'] is False


def test_the_costs_endpoint_says_whether_the_guard_is_on(store: Store):
    # given
    off = TestClient(create_app(dataclasses.replace(settings(), guard_check_answers=True), store))
    on = TestClient(create_app(dataclasses.replace(settings(), investigate_guard_model='claude-haiku-5-5', guard_check_questions=True), store))

    # when
    without_model = off.get('/api/costs', headers=AUTH).json()
    with_model = on.get('/api/costs', headers=AUTH).json()

    # then
    assert (without_model['guardOn'], with_model['guardOn']) == (False, True)
    assert with_model['today']['day'] == clock.day(time.time())


def test_run_lists_carry_the_guard_verdict_and_hold_time_but_never_the_held_text_or_reason(store: Store, client: TestClient):
    # given
    store.add_investigation(held_run())
    store.save(state() | {'anomalies': [{'key': CARD, 'sev': 'warn', 'status': 'open', 'svc': 'checkout-service', 'label': 'ERROR pattern'}]}, None)

    # when
    listed = store.list_investigations(None, 10, page=True)[0]
    latest = store.card_runs([CARD])[CARD]['latest']
    served = client.get('/api/investigations', params={'chats': 'false'}).json()[0]

    # then
    for run in (listed, latest, served):
        assert run['outcome'] == 'held' and run['guard'] == {'questions': None, 'answers': 'hold', 'masked': 0}
        assert run['held'] == {'until': '2026-11-27T10:00:00Z'}
        assert 'sk-live-123' not in str(run) and 'live token' not in str(run)
