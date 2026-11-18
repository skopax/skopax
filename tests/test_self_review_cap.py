from tests.test_investigator_tools import box
from skopax.investigator.tools import RUN_CALLS
from skopax.store import Store


def test_self_review_reads_its_live_facts_at_most_its_run_cap_times(store: Store):
    # given
    asked = []
    b = box(store, selfreview=lambda now: asked.append(now) or {'runs': {'count': 2}})
    cap = RUN_CALLS['self_review']

    # when
    answers = [b.call('self_review', {'facts': True}) for _ in range(cap + 1)]

    # then
    assert [failed for _, failed in answers] == [False] * cap + [True]
    assert f'self_review made its {cap} calls in this run' in answers[-1][0]
    assert len(asked) == cap
