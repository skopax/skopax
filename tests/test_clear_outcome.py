import pytest

from skopax import clearnotes, history
from skopax.clearnotes import ClearNotes
from skopax.contract import Check, Cleared, Field, PassResult, Sweep, anomaly
from skopax.engine import iso
from skopax.sealer import master_key
from skopax.store import Store
from tests.conftest import AUTH, settings
from tests.fake_slack import fake_slack
from tests.plugin_kit import add_instance
from tests.plugins.base import minimal
from tests.test_page_scan_seams import NOW, SITE, running

PLAN: dict = {}
THREAD = '1792220000.000100'


def card(svc: str, sev: str = 'warn') -> dict:
    x = anomaly('fix', 'deploy-bad', '-', svc, '', sev, 3, None, None, '', 'Bad deploy', f'{svc} rolled out a bad image', 1, f'{sev.upper()} {svc} bad deploy')
    x.update(ranKey=f'fix:{svc}')
    return x


def fixer(ctx) -> Sweep:
    plan = PLAN.get(ctx.instance.name) or PLAN.get('all', {})
    raised = plan.get('raised', ())
    ran = tuple(f'fix:{s}' for s in plan.get('ran', ())) or ('fix:no-data',)
    return Sweep(PassResult('fix', True, 'ok', [card(s, plan.get('sev', 'warn')) for s in raised], {}, ran), cleared=plan.get('cleared', {}))


FIXER = minimal('fixer', 'Fixer', fields=(Field('url', 'Site URL', 'url'),), checks=(Check('fix', 'Deploy fixer', 900, 0, ('fix',), fixer),))


def key_of(svc: str) -> str:
    return f'deploy-bad|-/{svc}'


@pytest.fixture(autouse=True)
def clean_plan():
    PLAN.clear()
    yield
    PLAN.clear()


def raise_then_clear(c, store: Store, cleared: dict, services=('shop', 'cart'), posted=('shop',), before=None) -> dict:
    sweeper = c.app.state.sweeper
    PLAN['all'] = {'raised': services, 'ran': services}
    sweeper.run('fixer', 'fix', NOW)
    for svc in posted:
        store.slack_posted(key_of(svc), {'channel': 'C7', 'ts': THREAD, 'at': iso(NOW + 60), 'by': 'server'})
    if before is not None:
        before()
    PLAN['all'] = {'raised': (), 'ran': services, 'cleared': cleared}
    sweeper.run('fixer', 'fix', NOW + 600)
    return {a['key']: a for a in store.load()['anomalies']}


SETTLE = 300


def notes(store: Store, slack, minutes: float = SETTLE / 60) -> ClearNotes:
    return ClearNotes(store, slack, minutes)


def test_a_fixed_outcome_resolves_the_card_it_clears_records_it_in_the_history_and_says_so_in_the_thread(store: Store, tmp_path):
    # given
    slack, post, _ = fake_slack()

    # when
    with running(store, tmp_path, plugins=(FIXER,), slack=slack) as c:
        found = raise_then_clear(c, store, {key_of('shop'): Cleared('Rolled back to 1.4.2 <!channel>', 'fixed')})
        view = c.get(f"/api/cards/{found[key_of('shop')]['num']}", headers=AUTH).json()
    at_pass = list(post.posted)
    noted = notes(store, slack).run_once(NOW + 600 + SETTLE)
    again = notes(store, slack).run_once(NOW + 600 + SETTLE + 60)
    shop_after = {a['key']: a for a in store.load()['anomalies']}[key_of('shop')]

    # then
    shop, at = found[key_of('shop')], iso(NOW + 600)
    assert (shop['status'], shop['triage'], shop['triagedBy']) == ('cleared', 'handled', 'auto')
    assert shop['resolution'] == {'kind': 'fixed', 'by': 'Fixer', 'via': 'system', 'at': at}
    assert shop['clearNote'] == {'text': 'Rolled back to 1.4.2 <!channel>', 'by': 'Fixer', 'plugin': 'fixer', 'at': at, 'outcome': 'fixed'}
    assert view['clearNote']['text'] == 'Rolled back to 1.4.2 <!channel>' and view['resolution']['kind'] == 'fixed'
    [doc] = list(store.card_history.find({'key': key_of('shop')}))
    assert doc['outcome'] == 'fixed' and doc['clearNote']['text'] == 'Rolled back to 1.4.2 <!channel>'
    assert history.outcome_text(doc['outcome'], doc['resolution']) == 'Fixed \u00b7 via Skopax \u00b7 Fixer'
    assert at_pass == [] and noted == [key_of('shop')] and again == []
    [line] = post.posted
    assert (line['channel'], line['thread_ts']) == ('C7', THREAD) and 'blocks' not in line
    assert line['text'] == '\u2705 Fixed by Fixer: *shop / Bad deploy* \u00b7 Rolled back to 1.4.2 &lt;!channel&gt;', 'the plug-in text, and no Resolve hint on a resolved card'
    assert shop_after['slackPost']['clearNote']['clearedAt'] == shop['clearedAt']
    assert found[key_of('cart')].get('triage') is None and 'clearNote' not in found[key_of('cart')]


def test_a_note_only_clear_keeps_the_resolve_hint(store: Store, tmp_path):
    # given
    slack, post, _ = fake_slack()

    # when
    with running(store, tmp_path, plugins=(FIXER,), slack=slack) as c:
        found = raise_then_clear(c, store, {key_of('shop'): Cleared('Traffic moved back to the main region'), key_of('pod-OOM'): Cleared('Memory limit raised')},
                                 services=('shop', 'pod-OOM'), posted=('shop', 'pod-OOM'))
    notes(store, slack).run_once(NOW + 600 + SETTLE)

    # then
    shop = found[key_of('shop')]
    assert shop['status'] == 'cleared' and shop.get('triage') is None and shop.get('resolution') is None
    assert shop['clearNote']['text'] == 'Traffic moved back to the main region'
    lines = {p['text'].split('*')[1]: p for p in post.posted}
    assert lines['shop / Bad deploy']['text'] == ('\U0001F440 Stopped firing, still open: *shop / Bad deploy* \u00b7 Fixer: Traffic moved back to the main region \u00b7 '
                                                  'If no action is needed, Resolve; if work is needed, Create task.')
    assert lines['shop / Bad deploy']['blocks'][-1]['type'] == 'actions', 'the Resolve button stays'
    assert 'Back to normal' not in lines['shop / Bad deploy']['text']
    recur = lines['pod-OOM / Bad deploy']['text']
    assert recur.startswith('\U0001F7E1 Alarm went quiet: *pod-OOM / Bad deploy* \u00b7 Fixer: Memory limit raised \u00b7 This kind of problem does not fix itself')


def test_a_plugin_clear_line_follows_the_clear_note_switch(store: Store, tmp_path):
    # given
    slack, post, _ = fake_slack()

    # when
    with running(store, tmp_path, plugins=(FIXER,), slack=slack, slack_clear_note_minutes=0) as c:
        found = raise_then_clear(c, store, {key_of('shop'): Cleared('Rolled back', 'fixed')})
    off = notes(store, slack, 0)

    # then
    assert found[key_of('shop')]['clearNote']['text'] == 'Rolled back'
    assert not off.enabled and post.posted == []


def test_the_plugin_clear_line_waits_for_the_settle_delay(store: Store, tmp_path):
    # given
    slack, post, _ = fake_slack()
    with running(store, tmp_path, plugins=(FIXER,), slack=slack) as c:
        raise_then_clear(c, store, {key_of('shop'): Cleared('Rolled back')})

    # when
    early = notes(store, slack).run_once(NOW + 600 + SETTLE - 1)
    seen_early = len(post.posted)
    late = notes(store, slack).run_once(NOW + 600 + SETTLE)

    # then
    assert early == [] and seen_early == 0
    assert late == [key_of('shop')] and len(post.posted) == 1 and 'Fixer: Rolled back' in post.posted[0]['text']


def test_a_person_s_triage_is_never_replaced_and_only_the_note_is_added(store: Store, tmp_path):
    # given
    slack, post, _ = fake_slack()

    def resolved_by_hand():
        store.triage(key_of('shop'), 'handled', by='alice@example.com', resolution={'kind': 'duplicate', 'ref': '#1', 'by': 'alice@example.com', 'via': 'page'})
        store.triage(key_of('cart'), 'progress', ticket='OPS-7', by='bob@example.com')

    # when
    with running(store, tmp_path, plugins=(FIXER,), slack=slack) as c:
        found = raise_then_clear(c, store, {key_of('shop'): Cleared('Rolled back', 'fixed'), key_of('cart'): Cleared('', 'handled')}, posted=(), before=resolved_by_hand)

    # then
    shop, cart = found[key_of('shop')], found[key_of('cart')]
    assert (shop['triage'], shop.get('triagedBy'), shop['resolution']['kind'], shop['resolution']['by']) == ('handled', None, 'duplicate', 'alice@example.com')
    assert shop['clearNote']['text'] == 'Rolled back'
    assert (cart['triage'], cart['ticket'], cart.get('resolution')) == ('progress', 'OPS-7', None) and 'clearNote' not in cart
    assert post.posted == []


def test_only_a_card_this_pass_clears_takes_a_note(store: Store, tmp_path):
    # given
    slack, post, _ = fake_slack()

    # when
    with running(store, tmp_path, plugins=(FIXER,), slack=slack) as c:
        found = raise_then_clear(c, store, {key_of('shop'): Cleared('first', 'fixed')}, posted=())
        PLAN['all'] = {'raised': ('cart',), 'ran': ('shop', 'cart'), 'cleared': {key_of('shop'): Cleared('late', 'handled'), key_of('cart'): Cleared('still firing', 'fixed'),
                                                                                   key_of('nowhere'): Cleared('unknown', 'fixed')}}
        c.app.state.sweeper.run('fixer', 'fix', NOW + 1200)
        later = {a['key']: a for a in store.load()['anomalies']}

    # then
    assert found[key_of('shop')]['clearNote']['text'] == 'first'
    assert later[key_of('shop')]['clearNote']['text'] == 'first' and later[key_of('shop')]['resolution']['kind'] == 'fixed'
    assert later[key_of('cart')]['status'] == 'open' and 'clearNote' not in later[key_of('cart')] and later[key_of('cart')].get('triage') is None
    assert key_of('nowhere') not in later and post.posted == []


def test_a_note_is_cut_to_its_limit_and_is_dropped_when_the_card_opens_again(store: Store, tmp_path):
    # given
    slack, _, _ = fake_slack()
    words = ' '.join(f'w{i:04d}' for i in range(300))

    # when
    with running(store, tmp_path, plugins=(FIXER,), slack=slack) as c:
        found = raise_then_clear(c, store, {key_of('shop'): Cleared(words)}, posted=())
        PLAN['all'] = {'raised': ('shop',), 'ran': ('shop',)}
        c.app.state.sweeper.run('fixer', 'fix', NOW + 1200)
        reopened = {a['key']: a for a in store.load()['anomalies']}[key_of('shop')]

    # then
    note = found[key_of('shop')]['clearNote']['text']
    first = store.card_history.find_one({'key': key_of('shop'), 'firstSeen': iso(NOW)})
    assert note.endswith('\u2026') and len(note) <= 500 and words.startswith(note[:-1])
    assert reopened['status'] == 'open' and 'clearNote' not in reopened
    assert first['clearNote']['text'] == note and first['clearNote']['by'] == 'Fixer', 'the earlier occurrence keeps its note in the history'


def test_no_line_goes_to_an_old_thread_of_a_card_that_opened_again_or_to_a_muted_card(store: Store, tmp_path):
    # given
    slack, post, _ = fake_slack()

    # when
    with running(store, tmp_path, plugins=(FIXER,), slack=slack) as c:
        raise_then_clear(c, store, {}, services=('shop', 'cart'), posted=('shop', 'cart'))
        PLAN['all'] = {'raised': ('shop',), 'ran': ('shop', 'cart')}
        c.app.state.sweeper.run('fixer', 'fix', NOW + 1200)
        store.mute(key_of('cart'), 'noise', None, 'mute')
        PLAN['all'] = {'raised': ('shop', 'cart'), 'ran': ('shop', 'cart')}
        c.app.state.sweeper.run('fixer', 'fix', NOW + 1800)
        PLAN['all'] = {'raised': (), 'ran': ('shop', 'cart'), 'cleared': {key_of('shop'): Cleared('back again, then gone'), key_of('cart'): Cleared('muted one')}}
        c.app.state.sweeper.run('fixer', 'fix', NOW + 2400)
        found = {a['key']: a for a in store.load()['anomalies']}
    notes(store, slack).run_once(NOW + 2400 + SETTLE)

    # then
    assert found[key_of('shop')]['firstSeen'] == iso(NOW + 1200) and found[key_of('shop')]['slackPost']['ts'] == THREAD
    assert found[key_of('shop')]['clearNote']['text'] == 'back again, then gone' and found[key_of('cart')]['clearNote']['text'] == 'muted one'
    assert post.posted == [], 'the thread is from an earlier occurrence, and a muted card stays quiet'


def test_an_info_card_takes_its_outcome_and_no_auto_resolve_overwrites_it(store: Store, tmp_path):
    # given
    slack, _, _ = fake_slack()

    # when
    with running(store, tmp_path, plugins=(FIXER,), slack=slack) as c:
        PLAN['all'] = {'raised': ('shop',), 'ran': ('shop',), 'sev': 'info'}
        c.app.state.sweeper.run('fixer', 'fix', NOW)
        PLAN['all'] = {'raised': (), 'ran': ('shop',), 'cleared': {key_of('shop'): Cleared('', 'handled')}}
        c.app.state.sweeper.run('fixer', 'fix', NOW + 600)
        shop = {a['key']: a for a in store.load()['anomalies']}[key_of('shop')]

    # then
    assert (shop['sev'], shop['triage'], shop['resolution']['kind'], shop['resolution']['by']) == ('info', 'handled', 'handled', 'Fixer')
    assert 'autoResolvedAt' not in shop and 'text' not in shop['clearNote'] and shop['clearNote']['outcome'] == 'handled'


def test_a_second_instance_notes_only_its_own_card(store: Store, tmp_path):
    # given
    slack, _, _ = fake_slack()
    add_instance(store, FIXER, 'east', sealer_key=master_key(store.db, settings()), url=SITE)

    # when
    with running(store, tmp_path, plugins=(FIXER,), slack=slack) as c:
        PLAN['all'] = {'raised': ('shop',), 'ran': ('shop',)}
        c.app.state.sweeper.run('fixer', 'fix', NOW)
        PLAN['east'] = {'raised': (), 'ran': ('shop',), 'cleared': {key_of('shop'): Cleared('east rolled back', 'fixed')}}
        c.app.state.sweeper.run('fixer', 'fix', NOW + 600)
        found = {a['key']: a for a in store.load()['anomalies']}

    # then
    east, main = found[key_of('shop') + '@east'], found[key_of('shop')]
    assert (east['status'], east['clearNote']['text'], east['resolution']['kind']) == ('cleared', 'east rolled back', 'fixed')
    assert main['status'] == 'open' and 'clearNote' not in main and main.get('triage') is None


@pytest.mark.parametrize('given', [['x'], {key_of('shop'): 'fixed'}, {key_of('shop'): Cleared('x', 'resolved')}, {key_of('shop'): Cleared(7)}, {'': Cleared('x')}])
def test_a_malformed_cleared_map_fails_the_pass(store: Store, tmp_path, given):
    # given
    slack, _, _ = fake_slack()

    # when
    with running(store, tmp_path, plugins=(FIXER,), slack=slack) as c:
        found = raise_then_clear(c, store, given, posted=())

    # then
    assert found[key_of('shop')]['status'] == 'open'
    assert store.load()['sources']['fix'] == 'error: the result is malformed (cleared)'
