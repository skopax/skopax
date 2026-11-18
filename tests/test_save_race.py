import copy

from tests.test_engine import NOW, ago, item, stored
from skopax import actions
from skopax.engine import PassResult, apply, iso
from skopax.store import Store

CDN, WAF, DNS, SSL, IST = 'cf-status|cdn', 'cf-status|waf', 'cf-status|dns', 'cf-status|ssl', 'cf-status|ist'
OLD_MUTE = {'pattern': 'cf-health|*', 'until': ago(60), 'reason': 'expired', 'kind': 'mute', 'at': ago(86400)}


class ClickDuringSave:
    def __init__(self, coll, click):
        self.coll, self.click, self.fired = coll, click, False

    def __getattr__(self, name):
        found = getattr(self.coll, name)
        if name not in ('replace_one', 'update_one'):
            return found

        def write(*args, **kwargs):
            if not self.fired:
                self.fired = True
                self.click()
            return found(*args, **kwargs)
        return write


def seeded(store: Store) -> None:
    cards = [stored(k, pushedAt=ago(7200)) for k in (CDN, WAF, DNS, SSL, IST)]
    store.save({'initialized': True, 'muted': [OLD_MUTE], 'history': [], 'anomalies': cards}, None)


def operator_clicks(other: Store) -> None:
    actions.apply(other, {'type': 'known', 'key': WAF, 'requestedBy': 'nina.white@example.com'})
    actions.apply(other, {'type': 'resolve', 'key': DNS, 'requestedBy': 'nina.white@example.com'})
    other.slack_draft(SSL, 'Draft: ssl warning', 'token')


def test_clicks_landing_while_a_pass_writes_its_cards_survive_and_rev_never_goes_back(store: Store):
    # given
    seeded(store)
    before = store.rev()['rev']
    other = Store(store.db)
    store.anomalies = ClickDuringSave(store.anomalies, lambda: operator_clicks(other))
    found = [item(k, now=3) for k in (CDN, WAF, DNS, SSL)]

    # when
    applied = apply(store, [PassResult('cf', True, 'ok', copy.deepcopy(found))], NOW)
    state = other.load()
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert store.anomalies.fired and applied.rev == before + 1
    assert other.rev()['rev'] == before + 4, 'the pass claimed once and the three clicks after it, so rev only goes up'
    assert by[WAF]['muted'] == 'marked as known noise on the page'
    assert [m['pattern'] for m in state['muted']] == [WAF], 'the expired mute is pruned by the pass, the new one stays'
    assert by[DNS]['triage'] == 'handled'
    assert by[SSL]['slackDraft']['text'] == 'Draft: ssl warning' and by[SSL]['autoSend']['draftAt'] == by[SSL]['slackDraft']['at']
    assert (by[IST]['status'], by[IST]['clearedAt']) == ('cleared', iso(NOW)), 'what the pass changed is still written'
    assert by[CDN]['lastSeen'] == iso(NOW) and by[CDN]['now'] == 3
    assert state['serverPasses']['cf'] == {'at': iso(NOW), 'status': 'ok'} and (IST, 'cleared') in {(e['key'], e['ev']) for e in state['history']}
