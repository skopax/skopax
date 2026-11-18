import logging
import threading

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
    def card_text(card: dict) -> str | None:
        src = card.get('src')
        card['sig'] = 'changed'
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
    assert written['sig'] == 'x-send', 'the hook gets a copy of the card'
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
