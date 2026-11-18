from tests.conftest import anomaly
from tests.fake_anthropic import FakeAnthropic
from tests.test_investigator import KEY, NOW, investigator, note_and_recap, seed
from skopax import actions
from skopax.store import Store


class ResolvedMidRun(FakeAnthropic):
    def __init__(self, store: Store, replies: list):
        super().__init__(replies)
        self.store = store

    def _create(self, **kw):
        if not self.requests:
            actions.apply(self.store, {'type': 'resolve', 'key': KEY, 'requestedBy': 'nina.white@example.com', 'reason': 'deploy rolled back'})
        return super()._create(**kw)


def test_a_card_resolved_while_the_pod_investigates_stays_resolved_when_the_run_writes_its_note(store: Store):
    # given
    seed(store, anomaly(KEY))
    inv = investigator(store, [])
    inv.client = ResolvedMidRun(store, note_and_recap())

    # when
    record = inv.run_once(NOW)

    # then
    card = store.anomaly(KEY)
    assert record['outcome'] == 'done' and record['keys'] == [KEY]
    assert (card['triage'], card.get('triagedBy')) == ('handled', None)
    assert 'one client, 40 lines' in card['note']['text']


def test_a_pod_note_marks_an_open_card_investigated_and_leaves_taken_and_resolved_ones(store: Store):
    # given
    seed(store, anomaly('a|1'), anomaly('b|2', triage='investigating'), anomaly('c|3', triage='progress'), anomaly('d|4', triage='handled'), anomaly('e|5', triage='handled'))

    # when
    for key in ('a|1', 'b|2', 'c|3', 'd|4'):
        store.note(key, f'pod note {key}', by='pod')
    store.note('e|5', 'a person writes a note')

    # then
    got = {k: (store.anomaly(k).get('triage'), store.anomaly(k).get('triagedBy'), store.anomaly(k)['note']['text']) for k in ('a|1', 'b|2', 'c|3', 'd|4', 'e|5')}
    assert got == {'a|1': ('dived', 'pod', 'pod note a|1'), 'b|2': ('dived', 'pod', 'pod note b|2'), 'c|3': ('progress', None, 'pod note c|3'),
                   'd|4': ('handled', None, 'pod note d|4'), 'e|5': ('dived', None, 'a person writes a note')}
