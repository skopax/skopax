import datetime as dt

from tests.conftest import anomaly, state
from skopax.store import LEASE_ID, Store, iso, utc_now


def test_pod_triage_marks_the_card_and_a_human_triage_clears_the_mark(store: Store):
    # given
    store.save_state(state(anomaly('log|a|ERROR|x')), None)

    # when
    store.triage('log|a|ERROR|x', 'progress', ticket='OPS-1', owner='Oscar Green', by='pod')
    pod = store.anomaly('log|a|ERROR|x')
    store.triage('log|a|ERROR|x', 'handled', by='token')
    human = store.anomaly('log|a|ERROR|x')

    # then
    assert (pod['triage'], pod['ticket'], pod['owner'], pod['triagedBy']) == ('progress', 'OPS-1', 'Oscar Green', 'pod')
    assert (human['triage'], human['ticket']) == ('handled', 'OPS-1')
    assert 'triagedBy' not in human


def test_every_triage_stamps_triaged_at_and_clearing_the_triage_drops_it(store: Store):
    # given
    store.save_state(state(anomaly('log|a|ERROR|x'), anomaly('log|b|ERROR|y')), None)
    before = iso(utc_now())

    # when
    store.triage('log|a|ERROR|x', 'handled', ticket='OPS-1')
    stamped = store.anomaly('log|a|ERROR|x')
    store.triage('log|b|ERROR|y', 'handled', ticket='OPS-2', at='2026-10-25T14:33:00Z')
    given = store.anomaly('log|b|ERROR|y')
    store.triage('log|a|ERROR|x', None)
    cleared = store.anomaly('log|a|ERROR|x')

    # then
    assert before <= stamped['triagedAt'] <= iso(utc_now())
    assert given['triagedAt'] == '2026-10-25T14:33:00Z'
    assert 'triagedAt' not in cleared


def test_triage_none_clears_the_state_and_an_empty_ticket_clears_the_ticket(store: Store):
    # given
    store.save_state(state(anomaly('log|a|ERROR|x', triage='progress', ticket='OPS-1')), None)

    # when
    store.triage('log|a|ERROR|x', None, ticket='')
    card = store.anomaly('log|a|ERROR|x')

    # then
    assert 'triage' not in card and 'ticket' not in card


def test_group_moves_children_under_the_parent_and_keeps_absorb_on_the_parent(store: Store):
    # given
    store.save_state(state(anomaly('p|a'), anomaly('c|b', absorb=['x*']), anomaly('c|c'), anomaly('p|other', parent='old')), None)

    # when
    store.group('p|a', ['c|b', 'c|c'], ['log|d*'])
    by = {a['key']: a for a in store.load()['anomalies']}

    # then
    assert (by['c|b']['parent'], by['c|c']['parent']) == ('p|a', 'p|a')
    assert 'absorb' not in by['c|b']
    assert by['p|a']['absorb'] == ['log|d*']
    assert 'parent' not in by['p|a']


def test_a_card_is_claimed_once_and_released_only_by_its_run(store: Store):
    # given
    store.save_state(state(anomaly('k|a'), anomaly('k|b', triage='handled')), None)

    # when
    first, again, taken = store.claim_card('k|a', 'r1'), store.claim_card('k|a', 'r2'), store.claim_card('k|b', 'r1')
    store.release_card('k|a', 'r2')
    kept = store.anomaly('k|a')
    store.release_card('k|a', 'r1')
    released = store.anomaly('k|a')

    # then
    assert (first, again, taken) == (True, False, False)
    assert (kept['triage'], kept['investigator']['runId']) == ('investigating', 'r1')
    assert 'triage' not in released and 'investigator' not in released


def test_release_leaves_a_card_the_run_already_noted(store: Store):
    # given
    store.save_state(state(anomaly('k|a')), None)
    store.claim_card('k|a', 'r1')
    store.note('k|a', 'checked')

    # when
    store.release_card('k|a', 'r1')

    # then
    assert store.anomaly('k|a')['triage'] == 'dived'


def test_one_lease_holder_at_a_time_until_it_expires(store: Store):
    # when
    a, b = store.take_lease('a', 1200), store.take_lease('b', 1200)
    renewed = store.take_lease('a', 1200)
    store.leases.update_one({'_id': LEASE_ID}, {'$set': {'until': utc_now() - dt.timedelta(seconds=1)}})
    b_after = store.take_lease('b', 1200)
    store.drop_lease('a')
    still_b = store.leases.find_one({'_id': LEASE_ID})['owner']
    store.drop_lease('b')

    # then
    assert (a, b, renewed, b_after, still_b) == (True, False, True, True, 'b')
    assert store.leases.find_one({'_id': LEASE_ID}) is None


def test_spend_adds_up_per_day(store: Store):
    # when
    store.add_spend('2026-10-21', 0.5)
    total = store.add_spend('2026-10-21', 0.25)
    store.add_spend('2026-10-22', 1.0)

    # then
    assert total == 0.75
    assert (store.spent('2026-10-21'), store.spent('2026-10-22'), store.spent('2026-10-23')) == (0.75, 1.0, 0.0)


def test_runs_are_listed_newest_first_and_filtered_by_key_and_mode(store: Store):
    # given
    store.add_investigation({'id': 'r1', 'mode': 'shadow', 'keys': ['k|a'], 'startedAt': '2026-10-21T10:00:00Z', 'outcome': 'running'})
    store.add_investigation({'id': 'r2', 'mode': 'live', 'keys': ['k|a', 'k|b'], 'startedAt': '2026-10-21T11:00:00Z', 'outcome': 'running'})

    # when
    store.update_investigation('r1', {'outcome': 'done', 'usd': 0.4})
    all_runs = store.list_investigations(None, 10)
    for_b = store.list_investigations('k|b', 10)
    shadow = store.recent_investigations('shadow', '2026-10-21T09:00:00Z')

    # then
    assert [r['id'] for r in all_runs] == ['r2', 'r1']
    assert [r['id'] for r in for_b] == ['r2']
    assert shadow == [{'id': 'r1', 'keys': ['k|a'], 'startedAt': '2026-10-21T10:00:00Z', 'outcome': 'done'}]
    assert 'startedAtDt' not in all_runs[0]


def test_the_stop_switch_is_set_by_someone_and_cleared(store: Store):
    # when
    before = store.investigator_stop()
    store.set_investigator_stop(True, 'oscar.green')
    stopped = store.investigator_stop()
    store.set_investigator_stop(False, 'oscar.green')

    # then
    assert before is None
    assert stopped['by'] == 'oscar.green' and stopped['at']
    assert store.investigator_stop() is None


def test_a_claim_older_than_the_cut_is_released_and_a_young_one_is_not(store: Store):
    # given
    old = {'runId': 'r-dead', 'at': '2026-10-21T08:00:00Z'}
    young = {'runId': 'r-live', 'at': '2026-10-21T09:59:00Z'}
    store.save_state(state(anomaly('log|a|ERROR|old', triage='investigating', investigator=old), anomaly('log|b|ERROR|young', triage='investigating', investigator=young)), None)
    rev = store.load()['rev']

    # when
    released = store.release_stale_claims('2026-10-21T09:00:00Z')
    cards = {a['key']: a for a in store.load()['anomalies']}

    # then
    assert released == 1
    assert 'triage' not in cards['log|a|ERROR|old'] and 'investigator' not in cards['log|a|ERROR|old']
    assert cards['log|b|ERROR|young']['triage'] == 'investigating' and cards['log|b|ERROR|young']['investigator'] == young
    assert store.load()['rev'] > rev
    assert store.release_stale_claims('2026-10-21T09:00:00Z') == 0


def test_a_running_run_older_than_the_cut_is_abandoned_and_a_fresh_one_is_not(store: Store):
    # given
    store.add_investigation({'id': 'r-old', 'keys': ['k'], 'startedAt': '2026-10-21T08:00:00Z', 'outcome': 'running'})
    store.add_investigation({'id': 'r-new', 'keys': ['k'], 'startedAt': '2026-10-21T09:59:00Z', 'outcome': 'running'})
    store.add_investigation({'id': 'r-done', 'keys': ['k'], 'startedAt': '2026-10-21T08:00:00Z', 'outcome': 'done'})

    # when
    count = store.abandon_stale_runs('2026-10-21T09:00:00Z')
    old = store.investigations.find_one({'_id': 'r-old'})
    new = store.investigations.find_one({'_id': 'r-new'})
    done = store.investigations.find_one({'_id': 'r-done'})

    # then
    assert count == 1
    assert (old['outcome'], old['error']) == ('abandoned', 'the pod died mid-run') and old['endedAt']
    assert new['outcome'] == 'running' and 'endedAt' not in new
    assert done['outcome'] == 'done'


def test_a_processing_dive_older_than_the_cut_is_failed_and_others_are_not(store: Store):
    # given
    def dive(status, at):
        action = store.add_action({'type': 'dive', 'key': 'k', 'target': 'k', 'label': 'Investigate', 'requestedBy': 'x'}, status=status)
        if at:
            store.actions.update_one({'_id': action['id']}, {'$set': {f'{status}At': at}})
        return action['id']

    stale = dive('processing', '2026-10-21T08:00:00Z')
    fresh = dive('processing', '2026-10-21T09:59:00Z')
    waiting = dive('requested', None)
    store.actions.update_one({'_id': waiting}, {'$set': {'requestedAt': '2026-10-21T08:00:00Z'}})

    # when
    count = store.fail_stale_actions('2026-10-21T09:00:00Z')
    docs = {d['_id']: d for d in store.actions.find({})}

    # then
    assert count == 1
    assert (docs[stale]['status'], docs[stale]['result']) == ('failed', 'the pod died mid-run')
    assert docs[fresh]['status'] == 'processing'
    assert docs[waiting]['status'] == 'requested'


def test_a_pod_note_marks_a_dived_card_a_human_note_clears_it_and_progress_is_left_alone(store: Store):
    # given
    store.save_state(state(anomaly('log|a|ERROR|x'), anomaly('log|b|ERROR|y', triage='progress', ticket='OPS-1')), None)

    # when
    store.note('log|a|ERROR|x', 'pod looked', by='pod')
    pod = store.anomaly('log|a|ERROR|x')
    store.note('log|a|ERROR|x', 'human looked')
    human = store.anomaly('log|a|ERROR|x')
    store.note('log|b|ERROR|y', 'pod looked', by='pod')
    progress = store.anomaly('log|b|ERROR|y')

    # then
    assert (pod['triage'], pod['triagedBy']) == ('dived', 'pod')
    assert human['triage'] == 'dived' and 'triagedBy' not in human
    assert progress['triage'] == 'progress' and 'triagedBy' not in progress
