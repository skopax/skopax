import copy
import json
import logging
import threading
from types import SimpleNamespace

from skopax import tags
from skopax.contract import Tag, TagRule
from skopax.engine import PassResult, apply
from skopax.plugins import teams
from skopax.registry import Registry
from skopax.store import Store
from tests.plugins.base import minimal
from tests.teams_kit import AT, SEO, Transport, route, routes_of, texts
from tests.test_admin_plugins import HERE

NOW = 1_794_240_000.0
LATER = '2026-11-09T17:00:00Z'
SENTENCE = 'We recommend sending the spring newsletter again: no one got it.'
KEY = 'x-send|-/shop|spring'


def sends(sig: str = 'x-send', **extra) -> dict:
    return {'key': f'{sig}|-/shop|spring', 'src': 'shop', 'sig': sig, 'ns': '-', 'svc': 'shop', 'sub': 'spring', 'sev': 'warn', 'now': 0, 'd1': None, 'd7': None, 'unit': '',
            'label': 'Spring newsletter', 'what': 'no sends', 'short': 'WARN no sends', 'slack': 1, 'status': 'open', 'firstSeen': AT, **extra}


def by_rule(text: str = 'An older rule sentence.') -> dict:
    return {'text': text, 'by': 'rule', 'at': AT}


def test_a_card_text_hook_writes_the_business_sentence_and_a_business_route_then_posts_it(store: Store):
    # given
    refused: list[str] = []

    def card_text(card: dict) -> str | None:
        src = card.get('src')
        try:
            card['sig'] = 'changed'
        except TypeError:
            refused.append(card['key'])
        return SENTENCE if src == 'shop' else None

    shop = minimal('shop', 'Shop', tags=(Tag('newsletter', 'Newsletter'),), tag_rules=(TagRule('newsletter', ('type:x-send',)),), card_text=card_text)
    t = Transport()
    routes = routes_of(store, t, route('news', SEO, tags='newsletter'), plugins=(teams.PLUGIN, shop))
    tags.use_rules(routes.runtime.tag_rules)
    tags.use_tags(routes.runtime.tag_defs)
    tags.use_texts(routes.runtime.card_texts)
    store.save({'anomalies': [], 'history': [], 'initialized': True}, None)

    # when
    apply(store, [PassResult('shop', True, 'ok', [sends()], {}, ('shop',))], NOW, tag_rules=tags.rules())
    written = store.anomaly(KEY)
    sent = routes.tagged(store.load()['anomalies'], now=NOW)

    # then
    assert written['business'] == {'text': SENTENCE, 'by': 'rule', 'at': written['business']['at']} and written['business']['at']
    assert tags.ids(written) == ['newsletter']
    assert written['sig'] == 'x-send' and refused == [KEY], 'the hook gets a read-only copy of the card'
    assert sent == {'teams/news': [KEY]}
    assert '**Recommended:** ' + SENTENCE in texts(t.calls[0][1])


def test_a_person_written_sentence_is_never_replaced():
    # given
    asked: list[str] = []
    tags.use_texts([('shop', lambda card: asked.append(card['key']) or SENTENCE)])
    person = sends(business={'text': 'Pause the spring send.', 'by': 'alice@example.com', 'at': AT})
    pod = sends('x-send-b', business={'text': 'The investigator wrote this.', 'by': 'pod', 'at': AT})

    # when
    tags.apply_rules([person, pod], (), LATER)

    # then
    assert person['business'] == {'text': 'Pause the spring send.', 'by': 'alice@example.com', 'at': AT}
    assert pod['business']['by'] == 'pod'
    assert asked == [], 'the hook is not even asked'


def test_a_rule_sentence_is_cleared_when_every_hook_answers_none():
    # given
    tags.use_texts([('shop', lambda card: None), ('mail', lambda card: '   ')])
    stale = sends(business=by_rule())
    long = sends('x-long', business=by_rule('x' * (tags.TEXT_LIMIT - 1) + '\u2026'))
    tags_long = [('shop', lambda card: 'x' * (tags.TEXT_LIMIT + 50) if card['sig'] == 'x-long' else None)]

    # when
    tags.apply_rules([stale], (), LATER)
    tags.use_texts(tags_long)
    tags.apply_rules([long], (), LATER)

    # then
    assert stale['business'] is None
    assert long['business'] == by_rule('x' * (tags.TEXT_LIMIT - 1) + '\u2026'), 'a long answer is cut, and an unchanged one keeps its time'


def test_the_first_answer_in_load_order_wins():
    # given
    tags.use_texts([('alpha', lambda card: None), ('beta', lambda card: 'Beta speaks.'), ('gamma', lambda card: 'Gamma speaks.')])
    fresh = sends()

    # when
    tags.apply_rules([fresh], (), LATER)

    # then
    assert fresh['business'] == {'text': 'Beta speaks.', 'by': 'rule', 'at': LATER}


def test_a_hook_that_raises_or_is_late_leaves_the_card_as_it_was(caplog):
    # given
    gate = threading.Event()

    def late(card: dict) -> str:
        gate.wait(5)
        return 'Too late.'

    raising = [('shop', lambda card: 1 / 0), ('mail', lambda card: 'Never asked.')]
    odd = [('shop', lambda card: 42)]
    slow = [('shop', late)]
    cards = {name: sends(f'x-{name}', business=by_rule()) for name in ('raising', 'odd', 'slow', 'after')}
    fresh = sends('x-fresh')

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tags'):
        tags.use_texts(raising)
        tags.apply_rules([cards['raising'], fresh], (), LATER)
        tags.use_texts(odd)
        tags.apply_rules([cards['odd']], (), LATER)
        tags.use_texts(slow, timeout=0.2)
        tags.apply_rules([cards['slow'], cards['after']], (), LATER)
    gate.set()

    # then
    assert all(c['business'] == by_rule() for c in cards.values()), 'a failed, odd or late answer neither writes nor clears'
    assert 'business' not in fresh
    assert 'ZeroDivisionError' in caplog.text and 'shop' in caplog.text


def test_a_card_text_that_is_not_callable_breaks_the_plugin():
    # when
    found = Registry.of(minimal('shop', 'Shop', card_text='write it'), directory=HERE).get('shop')

    # then
    assert found.state == 'broken' and 'card_text' in found.reason


def test_card_texts_are_asked_once_per_pass_outside_the_apply_loop_so_slow_hooks_do_not_make_it_conflict(store: Store, monkeypatch):
    # given
    cards = [sends(f'x-send-{i}') for i in range(250)]
    gone = sends('x-gone', status='cleared', clearedAt=AT, business=by_rule())
    store.save({'anomalies': [gone], 'history': [], 'initialized': True}, None)
    events: list[str] = []
    asked: list[str] = []
    real_load, real_save = store.load, store.save_state

    def load():
        events.append('load')
        return real_load()

    def save_state(state, base, loaded=None):
        events.append('save')
        return real_save(state, base, loaded)

    def card_text(card: dict) -> str:
        events.append('hook')
        asked.append(card['key'])
        if len(asked) % 251 == 1:
            real_save(real_load(), store.rev()['rev'])
        return SENTENCE

    monkeypatch.setattr(store, 'load', load)
    monkeypatch.setattr(store, 'save_state', save_state)
    tags.use_texts([('shop', card_text)])

    # when
    applied = apply(store, [PassResult('shop', True, 'ok', cards, {}, ('shop',))], NOW, tag_rules=())
    last_load = len(events) - 1 - events[::-1].index('load')

    # then
    assert applied is not None
    assert len(asked) == 250 and 'x-gone|-/shop|spring' not in asked, 'each open card is asked once per pass and a cleared card never'
    assert 'hook' not in events[last_load:], 'no hook runs between the load and the save that wins'
    assert all(store.anomaly(c['key'])['business']['text'] == SENTENCE for c in cards)
    assert store.anomaly('x-gone|-/shop|spring')['business'] == by_rule()


def test_a_pass_hands_each_hook_its_cards_in_one_bounded_call_not_one_per_card(monkeypatch):
    # given
    cards = [sends(f'x-send-{i}') for i in range(6000)]
    handed: list[str] = []
    real_call = tags.TEXT_CALLS.call

    def counted(key, fn, timeout, **kw):
        handed.append(key)
        return real_call(key, fn, timeout, **kw)

    monkeypatch.setattr(tags.TEXT_CALLS, 'call', counted)
    tags.use_texts([('alpha', lambda card: None if card['sig'].endswith(('0', '2', '4', '6', '8')) else 'Alpha speaks.'), ('beta', lambda card: 'Beta speaks.')])

    # when
    tags.apply_rules(cards, (), LATER)

    # then
    assert handed == ['alpha', 'beta']
    assert [c['business']['text'] for c in cards[:4]] == ['Beta speaks.', 'Alpha speaks.', 'Beta speaks.', 'Alpha speaks.']
    assert all(c['business']['by'] == 'rule' for c in cards)


def test_each_card_gets_its_own_copy_and_one_failing_card_keeps_only_its_own_sentence(caplog):
    # given
    seen: list[int] = []

    def card_text(card: dict) -> str:
        seen.append(id(card))
        try:
            card['label'] = 'changed'
        except TypeError:
            seen.append(-1)
        if card['sig'] in ('x-bad-1', 'x-bad-2'):
            raise LookupError('no such newsletter')
        return SENTENCE

    tags.use_texts([('shop', card_text), ('mail', lambda card: 'Never asked.')])
    cards = [sends('x-good-1'), sends('x-bad-1', business=by_rule()), sends('x-good-2'), sends('x-bad-2', business=by_rule())]

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tags'):
        tags.apply_rules(cards, (), LATER)

    # then
    assert [c['business'] for c in cards] == [{'text': SENTENCE, 'by': 'rule', 'at': LATER}, by_rule(), {'text': SENTENCE, 'by': 'rule', 'at': LATER}, by_rule()]
    assert all(c['label'] == 'Spring newsletter' for c in cards) and len(seen) == 8 and seen.count(-1) == 4 and len(set(seen) - {-1}) == 4
    assert [r.getMessage() for r in caplog.records] == ['plug-in shop: card text failed, the sentences it writes stay as they are this pass (LookupError)']


def test_a_hook_that_hangs_mid_pass_keeps_the_answers_before_it_and_stalls_nothing(caplog):
    # given
    gate = threading.Event()

    asked: list[str] = []

    def card_text(card: dict) -> str:
        asked.append(card['sig'])
        if card['sig'] == 'x-send-3':
            gate.wait(5)
        return SENTENCE

    tags.use_texts([('stuck', card_text), ('mail', lambda card: 'Never asked.')], timeout=0.2)
    cards = [sends(f'x-send-{i}', business=by_rule()) for i in range(6)]

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tags'):
        tags.apply_rules(cards, (), LATER)
    gate.set()
    for worker in [t for t in threading.enumerate() if t.name == tags.TEXT_CALLS.name]:
        worker.join(5)

    # then
    assert [c['business']['text'] for c in cards] == [SENTENCE] * 3 + ['An older rule sentence.'] * 3
    assert asked == [f'x-send-{i}' for i in range(4)], 'the late batch stops at the deadline and asks no later card'
    assert [r.getMessage() for r in caplog.records] == ['plug-in stuck: card text failed, the sentences it writes stay as they are this pass (TimedOut)']


def test_a_pass_that_runs_out_of_time_keeps_the_answers_so_far_and_says_timed_out_once(monkeypatch, caplog):
    # given
    now = [0.0]

    def card_text(card: dict) -> str:
        now[0] += 1
        return SENTENCE

    asked_later: list[str] = []
    monkeypatch.setattr(tags, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    tags.use_texts([('slow', card_text), ('later', lambda card: asked_later.append(card['key']) or 'Later speaks.')], timeout=2.5)
    cards = [sends(f'x-send-{i}', business=by_rule()) for i in range(6)]

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tags'):
        tags.apply_rules(cards, (), LATER)

    # then
    assert [c['business']['text'] for c in cards] == [SENTENCE] * 3 + ['An older rule sentence.'] * 3
    assert [r.getMessage() for r in caplog.records] == ['plug-in slow: card text failed, the sentences it writes stay as they are this pass (TimedOut)']
    assert asked_later == []


class WatchedGuard:
    def __init__(self, real, seen: threading.Event, after: int):
        self.real, self.seen, self.after, self.count = real, seen, after, 0

    def __enter__(self):
        self.real.acquire()

    def __exit__(self, *exc):
        self.real.release()
        self.count += 1
        if self.count >= self.after:
            self.seen.set()


def test_two_passes_at_once_wait_for_each_other_and_both_get_every_text(monkeypatch, caplog):
    # given
    started, gate, checked = threading.Event(), threading.Event(), threading.Event()

    def card_text(card: dict) -> str:
        if card['key'] == 'k0' and not started.is_set():
            started.set()
            gate.wait(5)
        return 'text ' + card['key']

    cards = [{'key': f'k{i}', 'tags': []} for i in range(1000)]
    monkeypatch.setattr(tags.TEXT_CALLS, '_guard', WatchedGuard(tags.TEXT_CALLS._guard, checked, 2))
    got: dict[str, list] = {}

    def run(name: str) -> None:
        got[name] = tags.rule_texts(cards, [('demo', card_text)], tags.time.monotonic() + 10)

    first, second = threading.Thread(target=run, args=('first',)), threading.Thread(target=run, args=('second',))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tags'):
        first.start()
        started.wait(5)
        second.start()
        checked.wait(5)
        gate.set()
        first.join(10)
        second.join(10)

    # then
    assert got['first'] == got['second'] == [f'text k{i}' for i in range(1000)]
    assert caplog.records == []


def test_a_pass_that_waits_past_its_own_deadline_skips_with_one_busy_warning(caplog):
    # given
    started, gate = threading.Event(), threading.Event()

    def card_text(card: dict) -> str:
        if not started.is_set():
            started.set()
            gate.wait(5)
        return 'text ' + card['key']

    cards = [{'key': f'k{i}', 'tags': []} for i in range(3)]
    got: dict[str, list] = {}
    first = threading.Thread(target=lambda: got.update(first=tags.rule_texts(cards, [('demo', card_text)], tags.time.monotonic() + 10)))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tags'):
        first.start()
        started.wait(5)
        late = tags.rule_texts(cards, [('demo', card_text)], tags.time.monotonic() + 0.2)
        gate.set()
        first.join(10)

    # then
    assert late == [tags.SKIPPED] * 3
    assert got['first'] == ['text k0', 'text k1', 'text k2']
    assert [r.getMessage() for r in caplog.records] == ["plug-in demo: card text skipped, another pass held it until this pass's deadline (Busy)"]


def test_a_busy_owner_and_a_late_hook_each_log_their_own_reason_once(caplog):
    # given
    started, gate = threading.Event(), threading.Event()

    def holding(card: dict) -> str:
        if not started.is_set():
            started.set()
            gate.wait(5)
        return 'held'

    def late(card: dict) -> str:
        gate.wait(5)
        return 'late'

    cards = [{'key': f'k{i}', 'tags': []} for i in range(3)]
    first = threading.Thread(target=lambda: tags.rule_texts(cards, [('held', holding)], tags.time.monotonic() + 10))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tags'):
        first.start()
        started.wait(5)
        busy = tags.rule_texts(cards, [('held', holding)], tags.time.monotonic() + 0.2)
        timed = tags.rule_texts(cards, [('slow', late)], tags.time.monotonic() + 0.2)
        gate.set()
        first.join(10)
    for worker in [t for t in threading.enumerate() if t.name == tags.TEXT_CALLS.name]:
        worker.join(5)

    # then
    assert busy == timed == [tags.SKIPPED] * 3
    assert [r.getMessage() for r in caplog.records] == ["plug-in held: card text skipped, another pass held it until this pass's deadline (Busy)",
                                                       'plug-in slow: card text failed, the sentences it writes stay as they are this pass (TimedOut)']


def held_owner(monkeypatch, now: list, gate: threading.Event, started: threading.Event):
    monkeypatch.setattr(tags.TEXT_CALLS, 'clock', lambda: now[0])

    def holding(card: dict) -> str:
        if not started.is_set():
            started.set()
            gate.wait(5)
        return 'text ' + card['key']

    return holding


def test_a_pass_skips_at_once_an_owner_whose_call_is_stuck_past_the_hook_timeout(monkeypatch, caplog):
    # given
    now, gate, started = [100.0], threading.Event(), threading.Event()
    holding = held_owner(monkeypatch, now, gate, started)
    tags.use_texts([('held', holding)])
    cards = [{'key': f'k{i}', 'tags': []} for i in range(3)]
    first = threading.Thread(target=lambda: tags.rule_texts(cards, [('held', holding)], tags.time.monotonic() + 100))
    first.start()
    started.wait(5)
    now[0] += tags.TEXTS[1] + 1

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tags'):
        began = tags.time.monotonic()
        skipped = tags.rule_texts(cards, [('held', holding)], tags.time.monotonic() + 5)
        took = tags.time.monotonic() - began
        still = first.is_alive()
    gate.set()
    first.join(10)

    # then
    assert skipped == [tags.SKIPPED] * 3
    assert still and took < 1, 'a stuck call is not waited for'
    assert [r.getMessage() for r in caplog.records] == ["plug-in held: card text skipped, another pass held it until this pass's deadline (Busy)"]


def test_a_pass_still_waits_for_an_owner_whose_call_started_within_the_hook_timeout(monkeypatch, caplog):
    # given
    now, gate, started, checked = [100.0], threading.Event(), threading.Event(), threading.Event()
    holding = held_owner(monkeypatch, now, gate, started)
    tags.use_texts([('held', holding)])
    cards = [{'key': f'k{i}', 'tags': []} for i in range(3)]
    monkeypatch.setattr(tags.TEXT_CALLS, '_guard', WatchedGuard(tags.TEXT_CALLS._guard, checked, 2))
    got: dict[str, list] = {}
    first = threading.Thread(target=lambda: got.update(first=tags.rule_texts(cards, [('held', holding)], tags.time.monotonic() + 100)))
    second = threading.Thread(target=lambda: got.update(second=tags.rule_texts(cards, [('held', holding)], tags.time.monotonic() + 10)))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tags'):
        first.start()
        started.wait(5)
        now[0] += tags.TEXTS[1] - 1
        second.start()
        checked.wait(5)
        gate.set()
        first.join(10)
        second.join(10)

    # then
    assert got['first'] == got['second'] == ['text k0', 'text k1', 'text k2']
    assert caplog.records == []


def test_a_hook_cannot_change_the_card_it_is_given_at_any_depth():
    # given
    tried: list[str] = []

    def card_text(card: dict) -> str:
        for name, write in (('top', lambda: card.__setitem__('sig', 'changed')), ('update', lambda: card.update(sig='changed')), ('pop', lambda: card.pop('sig')),
                            ('delete', lambda: card.__delitem__('sig')), ('default', lambda: card.setdefault('new', 1)), ('clear', card.clear),
                            ('list', lambda: card['tags'].append({'id': 'x'})), ('list item', lambda: card['tags'].__setitem__(0, None)),
                            ('nested', lambda: card['business'].__setitem__('text', 'changed')), ('merge', lambda: card.__ior__({'sig': 'changed'}))):
            try:
                write()
            except TypeError:
                tried.append(name)
        return SENTENCE

    tags.use_texts([('shop', card_text)])
    fresh = sends(tags=[{'id': 'seo', 'by': 'alice@example.com', 'at': AT}], business=by_rule())

    # when
    tags.apply_rules([fresh], (), LATER)

    # then
    assert tried == ['top', 'update', 'pop', 'delete', 'default', 'clear', 'list', 'list item', 'nested', 'merge']
    assert fresh['sig'] == 'x-send' and fresh['tags'] == [{'id': 'seo', 'by': 'alice@example.com', 'at': AT}]
    assert fresh['business'] == {'text': SENTENCE, 'by': 'rule', 'at': LATER}


def test_the_frozen_card_still_reads_like_a_dict_and_a_copy_of_it_can_be_changed():
    # given
    seen: dict = {}

    def card_text(card: dict) -> str:
        seen['dict'] = isinstance(card, dict) and isinstance(card['tags'], list)
        seen['json'] = json.loads(json.dumps(card))['tags'][0]['id']
        mine = copy.deepcopy(card)
        mine['tags'].append({'id': 'mine'})
        mine['sig'] = 'mine'
        shallow = copy.copy(card)
        shallow['sig'] = 'shallow'
        seen['copies'] = (mine['sig'], len(mine['tags']), shallow['sig'], type(mine) is dict, type(mine['tags']) is list)
        seen['plain'] = {**card, 'sig': 'spread'}['sig'] + ' ' + dict(card, sig='made')['sig']
        return SENTENCE

    tags.use_texts([('shop', card_text)])
    fresh = sends(tags=[{'id': 'seo', 'by': 'alice@example.com', 'at': AT}])

    # when
    tags.apply_rules([fresh], (), LATER)

    # then
    assert seen == {'dict': True, 'json': 'seo', 'copies': ('mine', 2, 'shallow', True, True), 'plain': 'spread made'}
    assert fresh['sig'] == 'x-send' and len(fresh['tags']) == 1


def test_every_hook_reads_the_same_one_copy_of_a_card_and_the_pass_makes_no_deep_copy(monkeypatch):
    # given
    seen: dict[str, list[int]] = {'alpha': [], 'beta': [], 'gamma': []}

    def hook(name: str, answer: str | None):
        return lambda card: seen[name].append(id(card)) or answer

    def no_deepcopy(*args, **kwargs):
        raise AssertionError('the card-text pass made a deep copy')

    tags.use_texts([('alpha', hook('alpha', None)), ('beta', hook('beta', None)), ('gamma', hook('gamma', 'Gamma speaks.'))])
    cards = [sends(f'x-send-{i}') for i in range(3)]

    # when
    with monkeypatch.context() as patched:
        patched.setattr(copy, 'deepcopy', no_deepcopy)
        tags.apply_rules(cards, (), LATER)

    # then
    assert seen['alpha'] == seen['beta'] == seen['gamma'] and len(set(seen['alpha'])) == 3
    assert [c['business']['text'] for c in cards] == ['Gamma speaks.'] * 3
