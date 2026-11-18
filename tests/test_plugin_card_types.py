import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skopax import registry as registry_module
from skopax.app import create_app
from skopax.autosend import CritNow, Fallback
from skopax.contract import CardType, Check
from skopax.engine import iso
from skopax.investigator import select
from skopax.registry import Registry
from tests.conftest import AUTH, settings
from tests.plugins.base import minimal, run
from tests.test_admin_plugins import HERE

NOW = 1_794_240_000.0
CORE_JS = Path(registry_module.__file__).parent / 'web' / 'static' / 'core.js'


def card(key: str, src: str, sev: str) -> dict:
    return {'key': key, 'src': src, 'sig': 'x-send', 'svc': 'checkout-service', 'sev': sev, 'status': 'open', 'firstSeen': iso(NOW - 1200), 'label': 'Sends', 'what': 'no sends'}


def fired(*keys: str) -> list[dict]:
    return [{'t': iso(NOW - 1200), 'key': k, 'ev': 'new', 'sev': 'warn', 'text': k, 'muted': False} for k in keys]


def shop(**over):
    return minimal('shop', 'Shop', **over)


def test_a_check_marked_own_findings_is_never_picked_by_the_investigator_or_the_fallback(store):
    # given
    own = Check('shop-sends', 'Shop sends', 900, 0, ('shop-sends',), run, own_findings=True)
    plain = Check('shop-orders', 'Shop orders', 900, 0, ('shop-orders',), run)
    registry = Registry.of(shop(checks=(own, plain)), directory=HERE)
    cards = [card('own-warn', 'shop-sends', 'warn'), card('own-crit', 'shop-sends', 'crit'), card('other-crit', 'shop-orders', 'crit')]
    state = {'anomalies': cards, 'history': fired('own-warn', 'own-crit', 'other-crit')}

    # when
    with TestClient(create_app(settings(), store, registry=registry)):
        picked = [a['key'] for a in select.eligible(state, 'live', [], NOW, 5)]
        fallback = [a['key'] for a, _ in Fallback(store, 15).due(state, [], NOW)]
        crit_now = [a['key'] for a in CritNow(store, lambda key: None, lambda: True).due(cards, cards, NOW)]

    # then
    assert registry.own_findings() == frozenset({'shop-sends'})
    assert picked == ['other-crit'], 'the investigator skips the cards of an own-findings check'
    assert fallback == ['other-crit'] and crit_now == ['other-crit'], 'and so does the Slack fallback'
    assert set(select.OWN_FINDINGS) == {'pagescan', 'nodes', 'slack-posts', 'browser'}, 'the core tuple stays as it is'


def test_card_types_label_and_alias_reach_the_page_and_a_clash_with_a_core_sig_breaks_the_plugin(store):
    # given
    types = (CardType('x-send', 'X sends'), CardType('x-access', 'No access', alias='no-access'))
    good = Registry.of(shop(card_types=types), directory=HERE)
    bad = {name: Registry.of(shop(card_types=given), directory=HERE).get('shop') for name, given in (
        ('core', (CardType('no-access', 'Blocked'),)),
        ('charset', (CardType('X_send', 'X sends'),)),
        ('alias', (CardType('x-send', 'X sends', alias='No Access'),)),
        ('label', (CardType('x-send', 'x' * 41),)),
        ('blank', (CardType('x-send', '  '),)),
        ('repeat', (CardType('x-send', 'X sends'), CardType('x-send', 'X sends again'))),
        ('kind', ('x-send',)),
    )}

    # when
    with TestClient(create_app(settings(), store, registry=good)) as c:
        tile = {t['id']: t for t in c.get('/api/server', headers=AUTH).json()['plugins']}['shop']
        page = c.get('/').text

    # then
    assert tile['types'] == [{'sig': 'x-send', 'label': 'X sends', 'alias': ''}, {'sig': 'x-access', 'label': 'No access', 'alias': 'no-access'}]
    assert '"x-access"' in page and '"X sends"' in page, 'the first render already carries the types'
    assert {name: found.state for name, found in bad.items()} == dict.fromkeys(bad, 'broken')
    assert 'no-access' in bad['core'].reason and 'built-in card type' in bad['core'].reason


def test_the_core_sigs_are_the_card_types_the_page_names():
    # given
    script = CORE_JS.read_text(encoding='utf-8')

    # when
    names = re.search(r'var TYPE_NAMES = \{(.*?)\};', script, re.S)[1]
    alias = re.search(r'var TYPE_ALIAS = \{(.*?)\};', script, re.S)[1]
    found = {m for block in (names, alias) for m in re.findall(r"(?:^|[{,]\s*)'?([a-z0-9-]+)'?:\s", block)}

    # then
    assert len(found) > 100
    assert registry_module.CORE_SIGS == frozenset(found | {'other'})


@pytest.mark.parametrize('flag', ['yes', 1])
def test_an_own_findings_flag_that_is_not_a_bool_breaks_the_plugin(flag):
    # given
    check = Check('shop-sends', 'Shop sends', 900, 0, ('shop-sends',), run, own_findings=flag)

    # when
    found = Registry.of(shop(checks=(check,)), directory=HERE).get('shop')

    # then
    assert found.state == 'broken' and 'own_findings' in found.reason


def test_a_plugin_broken_at_load_keeps_its_own_findings_checks_away_from_the_investigator(store):
    # given
    own = Check('shop-sends', 'Shop sends', 900, 0, ('shop-sends',), run, own_findings=True)
    registry = Registry.of(shop(checks=(own,), card_types=(CardType('log', 'Shop log'),)), directory=HERE)
    cards = [card('own-crit', 'shop-sends', 'crit')]
    state = {'anomalies': cards, 'history': fired('own-crit')}

    # when
    with TestClient(create_app(settings(), store, registry=registry)):
        picked = [a['key'] for a in select.eligible(state, 'live', [], NOW, 5)]
        fallback = [a['key'] for a, _ in Fallback(store, 15).due(state, [], NOW)]

    # then
    assert registry.get('shop').state == 'broken'
    assert registry.own_findings() == frozenset({'shop-sends'})
    assert picked == [] and fallback == []
