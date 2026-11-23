import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from tests.conftest import anomaly, settings, state
from tests.fake_slack import fake_slack
from tests.test_slack_clicks import Applier, ephemerals, post_payload
from tests.test_slack_layout import menu_pick, resolve_submit
from tests.test_slackevents import CARD_TS, KEY, NOW
from tests.test_slack_listener import FakeSocket
from skopax import actions, locales, messages
from skopax.messages import msg
from skopax.operators import Operators
from skopax.slack import CARD_ACTION, POST_ACTIONS, RESOLVE_DUP, RESOLVE_KIND, RESOLVE_VIEW, RESOLVE_WHY, VIEW_TEXT_LIMIT, resolve_view
from skopax.slackevents import Clicks, Listener
from skopax.cardlink import card_url
from skopax.store import Store

OTHER = 'log|payment-service|ERROR|timeout'
BASE = 'https://skopax.example.com'
THREAD_TS = '1792224000.000500'
NOTE_TS = '1792224000.000600'


def cards(store: Store) -> dict:
    store.save(state(anomaly(KEY, num=store.card_number(KEY)), anomaly(OTHER, num=store.card_number(OTHER))), None)
    store.slack_posted(KEY, {'channel': 'C1', 'ts': CARD_TS, 'at': '2026-11-11T07:00:00Z', 'by': 'server'})
    return store.add_action({'type': 'post-buttons', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardActions': {k: {'key': KEY, 'action': k} for k in POST_ACTIONS}}, 'done', 'menu')


def box_of(store: Store, applier=None, base_url: str = BASE, emails=None):
    slack, _, api = fake_slack(emails={'U1': 'adam.baker@example.com', 'U9': 'x@example.com'} if emails is None else emails)
    applier = applier or Applier()
    operators = Operators(lambda email: email in {'adam.baker@example.com'}, slack, settings(), lambda: NOW)
    return Clicks(store, slack, operators, applier, base_url=base_url), applier, api


def opened(api) -> dict:
    [view] = [p['view'] for m, p in api.calls if m == 'views.open']
    return view


def real_apply(store: Store):
    return lambda action: actions.apply(store, action)


def test_resolve_from_the_menu_opens_a_window_with_the_three_choices_and_fixed_picked_like_the_page(store: Store):
    # given
    doc = cards(store)
    box, applier, api = box_of(store)

    # when
    got = box.handle(menu_pick('resolve', doc['id']))

    # then
    view = opened(api)
    kinds = next(b for b in view['blocks'] if b.get('block_id') == RESOLVE_KIND)['element']
    inputs = {b['block_id']: b for b in view['blocks'] if b['type'] == 'input'}
    assert got == 'resolve-open' and applier.calls == []
    assert (view['type'], view['callback_id'], view['title']['text'], view['submit']['text']) == ('modal', RESOLVE_VIEW, msg('slack.resolve.title'), msg('slack.resolve.ok'))
    assert [(o['value'], o['text']['text']) for o in kinds['options']] == [('fixed', 'Fixed'), ('handled', 'No action needed'), ('duplicate', 'Duplicate')]
    assert kinds['initial_option']['value'] == 'fixed'
    assert (inputs[RESOLVE_WHY]['element']['max_length'], inputs[RESOLVE_DUP]['element']['max_length']) == (300, 200)
    assert inputs[RESOLVE_WHY]['optional'] and inputs[RESOLVE_DUP]['optional']
    assert json.loads(view['private_metadata']) == {'ask': doc['id'], 'channel': 'C1', 'ts': CARD_TS}
    assert 'cardActionApplied' not in store.get_action(doc['id'])


def test_submitting_fixed_with_a_why_resolves_as_fixed_from_slack_with_the_note(store: Store):
    # given
    doc = cards(store)
    box, _, api = box_of(store, applier=real_apply(store))
    box.handle(menu_pick('resolve', doc['id']))

    # when
    got = box.handle(resolve_submit(opened(api), 'fixed', why='  rolled back 1.4.2 '))

    # then
    card = store.anomaly(KEY)
    assert got == 'applied'
    assert card['triage'] == 'handled'
    assert {k: card['resolution'].get(k) for k in ('kind', 'ref', 'by', 'via')} == {'kind': 'fixed', 'ref': None, 'by': 'adam.baker@example.com', 'via': 'slack'}
    assert card['note']['text'] == 'rolled back 1.4.2'
    [done] = [a for a in store.list_actions('done', 10) if a['type'] == 'resolve']
    assert (done['kind'], done['reason'], done['source']) == ('fixed', 'rolled back 1.4.2', 'slack') and 'presetReason' not in done


def test_no_action_needed_carries_no_kind_and_the_why_as_the_reason_like_the_page(store: Store):
    # given
    doc = cards(store)
    box, applier, api = box_of(store)
    box.handle(menu_pick('resolve', doc['id']))

    # when
    got = box.handle(resolve_submit(opened(api), 'handled', why='one-off spike'))

    # then
    assert got == 'applied'
    assert applier.calls == [{'type': 'resolve', 'key': KEY, 'target': KEY, 'label': f'Resolve from Slack: {KEY}', 'requestedBy': 'adam.baker@example.com',
                              'source': 'slack', 'askId': doc['id'], 'reason': 'one-off spike'}]


@pytest.mark.parametrize('ref, error', [(None, 'slack.resolve.dup_missing'), ('   ', 'slack.resolve.dup_missing'), ('#99', 'slack.resolve.dup_unknown'), ('1', 'slack.resolve.dup_unknown')])
def test_a_duplicate_without_a_known_other_card_answers_errors_and_applies_nothing(store: Store, ref, error):
    # given
    doc = cards(store)
    box, applier, api = box_of(store)
    box.handle(menu_pick('resolve', doc['id']))
    sent = resolve_submit(opened(api), 'duplicate', why='same as the other', ref=ref)

    # when
    errors = box.view_errors(sent)
    got = box.handle(sent)

    # then
    assert errors == {RESOLVE_DUP: msg(error, ref=' '.join(str(ref or '').split()))}
    assert got == 'invalid' and applier.calls == []
    assert ephemerals(api) == [errors[RESOLVE_DUP]]
    assert 'cardActionApplied' not in store.get_action(doc['id'])


@pytest.mark.parametrize('ref', ['2', '#2'])
def test_a_duplicate_of_a_card_number_resolves_as_duplicate_with_its_ref_and_drops_the_why(store: Store, ref):
    # given
    doc = cards(store)
    box, _, api = box_of(store, applier=real_apply(store))
    box.handle(menu_pick('resolve', doc['id']))
    sent = resolve_submit(opened(api), 'duplicate', why='ignored', ref=ref)

    # when
    errors = box.view_errors(sent)
    got = box.handle(sent)

    # then
    card = store.anomaly(KEY)
    assert errors is None and got == 'applied'
    assert {k: card['resolution'][k] for k in ('kind', 'ref', 'via')} == {'kind': 'duplicate', 'ref': '#2', 'via': 'slack'}
    [done] = [a for a in store.list_actions('done', 10) if a['type'] == 'resolve']
    assert (done['kind'], done['ref']) == ('duplicate', ref) and 'reason' not in done


def test_a_non_operator_submit_is_refused_and_applies_nothing(store: Store):
    # given
    doc = cards(store)
    box, applier, api = box_of(store)
    box.handle(menu_pick('resolve', doc['id']))

    # when
    got = box.handle(resolve_submit(opened(api), 'fixed', user='U9'))

    # then
    assert got == 'not-operator' and applier.calls == []
    assert ephemerals(api) == [msg('slackevents.click.action_non_operator')]
    assert 'cardActionApplied' not in store.get_action(doc['id'])


@pytest.mark.parametrize('base_url, line', [(BASE, msg('slackevents.click.resolve_window_failed', url=f'{BASE}/c/1')), ('', msg('slackevents.click.resolve_window_failed_bare'))])
def test_a_resolve_window_that_slack_does_not_open_logs_and_points_to_the_card_page(store: Store, caplog, base_url, line):
    # given
    doc = cards(store)
    box, applier, api = box_of(store, base_url=base_url)
    api.fail.add('views.open')

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.slackevents'):
        got = box.handle(menu_pick('resolve', doc['id']))

    # then
    assert got == 'modal-failed' and applier.calls == []
    assert ephemerals(api) == [line]
    assert 'the resolve window in C1 was not opened' in caplog.text
    assert 'cardActionApplied' not in store.get_action(doc['id'])


def test_a_double_submit_applies_once(store: Store):
    # given
    doc = cards(store)
    box, applier, api = box_of(store)
    box.handle(menu_pick('resolve', doc['id']))
    view = opened(api)

    # when
    first = box.handle(resolve_submit(view, 'fixed', why='deployed the fix'))
    second = box.handle(resolve_submit(view, 'handled'))

    # then
    assert (first, second) == ('applied', 'claimed')
    assert [(c['type'], c.get('kind'), c.get('reason')) for c in applier.calls] == [('resolve', 'fixed', 'deployed the fix')]
    assert ephemerals(api) == [msg('slackevents.click.applied', label=msg('slack.label.resolve_as', kind='Fixed'), login='adam.baker@example.com')]


def test_a_bare_resolve_button_in_a_thread_opens_the_window_and_a_kinded_one_applies_at_once(store: Store):
    # given
    cards(store)
    bare = store.add_action({'type': 'clear-note', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardAction': {'key': KEY, 'action': 'resolve'}}, 'done', 'n')
    kinded = store.add_action({'type': 'ask', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardAction': {'key': KEY, 'action': 'resolve', 'kind': 'fixed'}}, 'done', 'a')
    box, applier, api = box_of(store)
    click = lambda value: {'type': 'block_actions', 'trigger_id': 'T0TRIGGER', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'},
                           'message': {'ts': NOTE_TS, 'thread_ts': THREAD_TS, 'text': 'x', 'blocks': []}, 'actions': [{'action_id': CARD_ACTION, 'value': value}]}

    # when
    window = box.handle(click(bare['id']))
    view = opened(api)
    done = box.handle(resolve_submit(view, 'duplicate', ref=OTHER))
    direct = box.handle(click(kinded['id']))

    # then
    assert (window, done, direct) == ('resolve-open', 'applied', 'applied')
    assert json.loads(view['private_metadata']) == {'ask': bare['id'], 'channel': 'C1', 'ts': NOTE_TS, 'thread': THREAD_TS, 'button': True}
    assert [(c['askId'], c.get('kind'), c.get('ref')) for c in applier.calls] == [(bare['id'], 'duplicate', OTHER), (kinded['id'], 'fixed', None)]
    assert len([m for m, _ in api.calls if m == 'views.open']) == 1


def test_a_resolve_post_button_opens_the_window_and_its_submit_applies_through_the_post_menu_ask(store: Store):
    # given
    doc = cards(store)
    box, applier, api = box_of(store)

    # when
    window = box.handle(post_payload(doc['id'], 'resolve') | {'trigger_id': 'T0TRIGGER'})
    view = opened(api)
    done = box.handle(resolve_submit(view, 'handled'))

    # then
    assert (window, done) == ('resolve-open', 'applied')
    assert 'button' not in json.loads(view['private_metadata'])
    assert [(c['type'], c['askId'], c.get('kind')) for c in applier.calls] == [('resolve', doc['id'], None)]


class AnsweringSocket(FakeSocket):
    def __init__(self):
        super().__init__()
        self.payloads: list = []

    def send_socket_mode_response(self, resp) -> None:
        super().send_socket_mode_response(resp)
        self.payloads.append(resp.payload)


def test_the_listener_answers_a_bad_duplicate_with_slack_errors_in_the_ack_and_queues_a_good_submit(store: Store):
    # given
    doc = cards(store)
    box, applier, api = box_of(store)
    box.handle(menu_pick('resolve', doc['id']))
    view = opened(api)
    sock = AnsweringSocket()
    listener = Listener('xapp-test', box, box, lambda token: sock)
    envelope = lambda payload, envelope_id: SimpleNamespace(type='interactive', payload=payload, envelope_id=envelope_id)

    # when
    listener.on_request(sock, envelope(resolve_submit(view, 'duplicate', ref='#99'), 'e1'))
    listener.on_request(sock, envelope(resolve_submit(view, 'fixed'), 'e2'))

    # then
    assert sock.acks == ['e1', 'e2']
    assert sock.payloads[0] == {'response_action': 'errors', 'errors': {RESOLVE_DUP: msg('slack.resolve.dup_unknown', ref='#99')}}
    assert not sock.payloads[1]
    assert listener.click_queue.qsize() == 1 and applier.calls == []


@pytest.mark.parametrize('lang', ('en', 'de', 'es', 'fr', 'tr'))
def test_every_pack_says_the_resolve_window_within_the_slack_limits(lang):
    # given
    messages.set_catalogs([(pack.lang, dict(pack.messages)) for pack in locales.packs()])
    messages.use_language(lambda: lang)

    # when
    try:
        keys = [k for k in messages.MESSAGES if k.startswith('slack.resolve.') or k.startswith('slackevents.click.resolve_window')]
        texts = {k: msg(k, ref='#9', url='u') for k in keys}
        view = resolve_view('a1', 'C1', CARD_TS)
    finally:
        messages.use_language(None)

    # then
    kinds = next(b for b in view['blocks'] if b.get('block_id') == RESOLVE_KIND)['element']['options']
    assert lang == 'en' or all(locales.read(lang).messages.get(k) for k in keys), [k for k in keys if not locales.read(lang).messages.get(k)]
    assert all(len(msg_text) <= VIEW_TEXT_LIMIT for msg_text in (texts['slack.resolve.title'], texts['slack.resolve.ok']))
    assert all(len(o['text']['text']) <= 75 and not o['text']['text'].endswith('…') for o in kinds)
    assert all(len(texts[f'slack.resolve.{k}_placeholder']) <= 150 for k in ('why', 'dup'))


@pytest.mark.parametrize('kind, call, pick', [('ask', {'key': KEY, 'action': 'resolve'}, 'handled'), ('reminder', {'key': KEY, 'action': 'resolve', 'kind': 'handled'}, 'handled'),
                                              ('clear-note', {'key': KEY, 'action': 'resolve'}, 'fixed')])
def test_a_resolve_whose_kind_is_known_as_no_action_needed_preselects_it_in_the_window(store: Store, kind, call, pick):
    # given
    cards(store)
    ask = store.add_action({'type': kind, 'source': 'slack', 'key': KEY, 'target': KEY, 'cardAction': call}, 'done', 'a')
    box, applier, api = box_of(store)
    click = {'type': 'block_actions', 'trigger_id': 'T0TRIGGER', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'},
             'message': {'ts': NOTE_TS, 'thread_ts': THREAD_TS, 'text': 'x', 'blocks': []}, 'actions': [{'action_id': CARD_ACTION, 'value': ask['id']}]}

    # when
    window = box.handle(click)
    view = opened(api)
    done = box.handle(resolve_submit(view, 'handled', why='harmless'))

    # then
    kinds = next(b for b in view['blocks'] if b.get('block_id') == RESOLVE_KIND)['element']
    assert (window, done) == ('resolve-open', 'applied')
    assert kinds['initial_option']['value'] == pick
    assert [(c.get('kind'), c.get('reason')) for c in applier.calls] == [(None, 'harmless')]


def test_a_pre_check_that_raises_still_acks_empty_and_the_submit_falls_back_to_the_thread_line(store: Store, monkeypatch, caplog):
    # given
    doc = cards(store)
    box, applier, api = box_of(store)
    box.handle(menu_pick('resolve', doc['id']))
    sent = resolve_submit(opened(api), 'duplicate', ref='#2')
    monkeypatch.setattr(store, 'key_of_number', lambda n: (_ for _ in ()).throw(RuntimeError('store down')))
    sock = AnsweringSocket()
    listener = Listener('xapp-test', box, box, lambda token: sock)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.slackevents'):
        listener.on_request(sock, SimpleNamespace(type='interactive', payload=sent, envelope_id='e1'))
        got = box.handle(listener.click_queue.get_nowait()[1])

    # then
    assert sock.acks == ['e1'] and not sock.payloads[0]
    assert 'a Slack window was not checked before its submit: RuntimeError' in caplog.text
    assert got == 'check-failed' and applier.calls == []
    assert ephemerals(api)[-1] == msg('slackevents.click.resolve_check_failed', url=card_url(BASE, KEY))
    assert 'cardActionApplied' not in store.get_action(doc['id'])


def test_a_slow_pre_check_does_not_hold_the_ack(store: Store):
    # given
    held = threading.Event()

    class SlowClicks:
        def view_errors(self, payload: dict) -> dict:
            held.wait(5)
            return {RESOLVE_DUP: 'late'}
    sock = AnsweringSocket()
    listener = Listener('xapp-test', SlowClicks(), SlowClicks(), lambda token: sock, check_s=0.05)

    # when
    started = time.monotonic()
    listener.on_request(sock, SimpleNamespace(type='interactive', payload={'type': 'view_submission', 'view': {}}, envelope_id='e1'))
    listener.on_request(sock, SimpleNamespace(type='events_api', payload={'event': {'type': 'message'}}, envelope_id='e2'))
    took = time.monotonic() - started
    held.set()

    # then
    assert sock.acks == ['e1', 'e2'] and not any(sock.payloads)
    assert took < 1 and listener.click_queue.qsize() == 1


def test_a_wider_store_failure_on_submit_still_tells_the_person_and_applies_nothing(store: Store, monkeypatch, caplog):
    # given
    doc = cards(store)
    box, applier, api = box_of(store)
    box.handle(menu_pick('resolve', doc['id']))
    sent = resolve_submit(opened(api), 'fixed')
    monkeypatch.setattr(store, 'get_action', lambda action_id: (_ for _ in ()).throw(RuntimeError('store down')))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.slackevents'):
        got = box.handle(sent)

    # then
    assert got == 'check-failed' and applier.calls == []
    assert ephemerals(api) == [msg('slackevents.click.resolve_check_failed_bare')]
    assert 'a resolve window from Slack was not checked: RuntimeError' in caplog.text


def test_altered_window_metadata_applies_nothing(store: Store):
    # given
    doc = cards(store)
    kinded = store.add_action({'type': 'ask', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardAction': {'key': KEY, 'action': 'resolve', 'kind': 'fixed'}}, 'done', 'a')
    take = store.add_action({'type': 'reminder', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardAction': {'key': KEY, 'action': 'take'}}, 'done', 'r')
    box, applier, api = box_of(store)
    box.handle(menu_pick('resolve', doc['id']))
    view = opened(api)
    meta = json.loads(view['private_metadata'])
    altered = lambda **over: view | {'private_metadata': json.dumps(meta | over)}

    # when
    got = [box.handle(resolve_submit(altered(ask=kinded['id'], button=True), 'handled')), box.handle(resolve_submit(altered(button=True), 'handled')),
           box.handle(resolve_submit(altered(ask=take['id'], button=True), 'handled')), box.handle(resolve_submit(altered(ask=take['id']), 'handled'))]

    # then
    assert got == ['gone'] * 4 and applier.calls == []
    assert all('cardActionApplied' not in store.get_action(i) for i in (doc['id'], kinded['id'], take['id']))


def test_a_timed_out_pre_check_still_applies_its_envelope_once_and_a_repeated_envelope_applies_once(store: Store, monkeypatch):
    # given
    doc = cards(store)
    box, applier, api = box_of(store)
    box.handle(menu_pick('resolve', doc['id']))
    sent = resolve_submit(opened(api), 'fixed', why='deployed')
    held = threading.Event()
    checked = box.view_errors
    monkeypatch.setattr(box, 'view_errors', lambda payload: (held.wait(5), checked(payload))[1])
    sock = AnsweringSocket()
    listener = Listener('xapp-test', box, box, lambda token: sock, check_s=0.05)

    # when
    listener.on_request(sock, SimpleNamespace(type='interactive', payload=sent, envelope_id='e1'))
    listener.on_request(sock, SimpleNamespace(type='interactive', payload=sent, envelope_id='e1'))
    held.set()
    got = [listener.process(listener.click_queue.get_nowait()) for _ in range(2)]

    # then
    assert sock.acks == ['e1', 'e1'] and not any(sock.payloads)
    assert got == ['applied', 'claimed']
    assert [(c['type'], c.get('kind'), c.get('reason')) for c in applier.calls] == [('resolve', 'fixed', 'deployed')]


def test_stopping_the_listener_shuts_the_check_pool_without_waiting():
    # given
    held = threading.Event()
    listener = Listener('xapp-test', object(), object(), lambda token: FakeSocket())
    running = listener.checks.submit(held.wait, 5)
    queued = [listener.checks.submit(held.wait, 5) for _ in range(3)]

    # when
    started = time.monotonic()
    listener.stop()
    took = time.monotonic() - started
    held.set()

    # then
    assert took < 1 and all(q.cancelled() for q in queued[1:])
    assert running.result(timeout=2) is True
    with pytest.raises(RuntimeError):
        listener.checks.submit(held.wait, 0)
