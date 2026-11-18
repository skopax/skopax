import copy
import json

import pytest

from skopax.plugins.vwo.wingify import DECIDED, LEANING, REPORT_FROM, Wingify, read, results, wanted
from skopax.remote import Reply
from skopax.store import Store
from tests.test_investigator_tools import box
from tests.test_wingify import API, ARROW, SHOP, TOKEN, vwo_tools

CONTROL = {'goal': 2, 'variation': 1,
           'aggregated': {'visitorCount': 6175, 'conversionCount': 1350, 'isGoalDurationComplete': True, 'standardDeviation': 0.00608,
                          'conversionRate': {'median': 0.2247, 'credibleInterval1pct': [0.2091, 0.2404]}, 'improvementRate': None, 'relativeImprovementRate': None,
                          'conversionExpectedImprovementRate': None,
                          'decisionGraphData': {'betterPercentage': 0, 'notBetterPercentage': 0, 'practicalEquivalancePercentage': 0, 'notWorsePercentage': 0, 'worsePercentage': 0},
                          'decision': None, 'isDecisionMade': False},
           'isWinner': False, 'isSmartDecision': False, 'possibleWinner': False,
           'intervalWise': [{'interval': 1794268800, 'visitorCount': 2094, 'conversionCount': 469, 'productCount': None},
                            {'interval': 1794355200, 'visitorCount': 4081, 'conversionCount': 881, 'productCount': None}],
           'isInObservatoryMode': False}
VARIATION = {'goal': 2, 'variation': 2,
             'aggregated': {'visitorCount': 6185, 'conversionCount': 1455, 'standardDeviation': 0.00621,
                            'conversionRate': {'median': 0.2392, 'credibleInterval1pct': [0.2232, 0.2552]},
                            'improvementRate': {'median': 0.01446, 'credibleInterval1pct': [0.00017, 0.02875]},
                            'relativeImprovementRate': {'median': 0.0643, 'credibleInterval1pct': [0.00074, 0.1279]},
                            'conversionExpectedImprovementRate': {'median': 0.0643, 'credibleInterval1pct': [0.00074, 0.1279]},
                            'decisionGraphData': {'betterPercentage': 0.92, 'notBetterPercentage': 0.08, 'practicalEquivalancePercentage': 0.0527, 'notWorsePercentage': 0.9727,
                                                  'worsePercentage': 0.0273},
                            'decision': None, 'isDecisionMade': False},
             'isWinner': False, 'isSmartDecision': False, 'possibleWinner': False,
             'intervalWise': [{'interval': 1794268800, 'visitorCount': 2101, 'conversionCount': 497, 'productCount': None},
                              {'interval': 1794355200, 'visitorCount': 4084, 'conversionCount': 958, 'productCount': None}],
             'isInObservatoryMode': False}
GOALS = [{'id': 2, 'name': 'Add to cart click', 'type': 'CLICK_ELEMENT'}, {'id': 3, 'name': 'Form submit', 'type': 'FORM_SUBMIT'}]
VARIATIONS = [{'id': 1, 'name': 'Control', 'isControl': True, 'percentSplit': 50}, {'id': 2, 'name': 'Similar chairs on top', 'isControl': False, 'percentSplit': 50}]


def entry(base: dict, goal: int, variation: int, top: dict | None = None, **aggregated) -> dict:
    out = copy.deepcopy(base)
    out.update({'goal': goal, 'variation': variation} | (top or {}))
    out['aggregated'].update(aggregated)
    return out


def campaign(cid: int = 101, entries: list | None = None, goals: list | None = None, variations: list | None = None, **over) -> dict:
    return {'id': cid, 'name': 'Product page similar chairs', 'type': 'ab', 'status': 'RUNNING', 'platform': 'website', 'createdOn': 1_794_000_000, 'percentTraffic': 100,
            'primaryUrl': f'{SHOP}/products/item', 'urls': [], 'variations': VARIATIONS if variations is None else variations,
            'goals': GOALS if goals is None else goals, 'thresholds': {'winningPercent': 95, 'losingPercent': 5, 'visitors': 100}, 'isStatsComplete': False, 'isMAB': False,
            'variationGoalData': [CONTROL, VARIATION, entry(CONTROL, 3, 1), entry(VARIATION, 3, 2)] if entries is None else entries} | over


class Api:
    def __init__(self, raw: dict | None = None):
        self.raw = campaign() if raw is None else raw
        self.calls: list[str] = []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append(url)
        return Reply({'_data': self.raw}, {})


def test_the_real_api_shape_becomes_control_against_variation_per_goal_with_names_and_the_control_nulls_left_out():
    # given
    raw = campaign()

    # when
    res = results(raw)

    # then
    first = res['goals'][0]
    assert [g['goal'] for g in res['goals']] == ['Add to cart click', 'Form submit'] and 'primary' not in first and first['type'] == 'CLICK_ELEMENT'
    assert first['control'] == {'name': 'Control', 'visitors': 6175, 'conversions': 1350, 'rate': 0.2247}
    assert first['variations'] == [{'name': 'Similar chairs on top', 'visitors': 6185, 'conversions': 1455, 'rate': 0.2392, 'lift': 0.0643, 'liftRange': [0.00074, 0.1279],
                                    'beatChance': 0.92, 'worseChance': 0.0273, 'sameChance': 0.0527, 'status': LEANING}]
    assert res['vwoThresholds'] == {'winChance': 0.95, 'loseChance': 0.05, 'visitors': 100} and res['statsComplete'] is False and res['mab'] is False
    assert res['campaign']['id'] == 101 and 'variationGoalData' not in json.dumps(res)


def test_two_variations_the_other_equivalence_spelling_string_ids_and_missing_numbers_are_read_without_failing():
    # given
    variations = VARIATIONS + [{'id': 3, 'name': 'Short form', 'isControl': False, 'percentSplit': 0}]
    third = entry(VARIATION, 2, 3, None, visitorCount=6201, conversionCount=None, relativeImprovementRate={'median': -0.012}, conversionRate=None,
                  decisionGraphData={'betterPercentage': 41, 'practicalEquivalencePercentage': 0.31, 'worsePercentage': True})
    third['goal'], third['variation'] = '2', '3'
    raw = campaign(entries=[CONTROL, VARIATION, third], goals=GOALS[:1], variations=variations, thresholds=None, isMAB='yes')

    # when
    res = results(raw)

    # then
    [goal] = res['goals']
    assert [v['name'] for v in goal['variations']] == ['Similar chairs on top', 'Short form']
    assert goal['variations'][1] == {'name': 'Short form', 'visitors': 6201, 'lift': -0.012, 'beatChance': 0.41, 'sameChance': 0.31, 'status': LEANING}
    assert 'vwoThresholds' not in res and 'mab' not in res


def test_a_goal_without_results_an_entry_for_an_unlisted_goal_or_variation_and_broken_entries_keep_names_and_never_raise():
    # given
    stray = entry(VARIATION, 9, 4)
    broken = [None, 'x', {'goal': None, 'variation': 1}, {'goal': 2, 'variation': True}, {'goal': 2, 'variation': 5, 'aggregated': None}]
    raw = campaign(entries=[CONTROL, VARIATION, stray] + broken, goals=GOALS + [{'id': 'bad'}, 'goal'], variations=VARIATIONS + [None])

    # when
    res = results(raw)

    # then
    by_name = {g['goal']: g for g in res['goals']}
    assert list(by_name) == ['Add to cart click', 'Form submit', 'goal 9']
    assert by_name['Form submit'] == {'goal': 'Form submit', 'type': 'FORM_SUBMIT', 'noData': True}
    assert [v['name'] for v in by_name['goal 9']['variations']] == ['Similar chairs on top', 'variation 4']
    assert by_name['goal 9']['control'] == {'name': 'Control', 'noData': True} and by_name['goal 9']['variations'][0] == {'name': 'Similar chairs on top', 'noData': True, 'status': LEANING}
    assert by_name['Add to cart click']['variations'][-1] == {'name': 'variation 5', 'status': LEANING}


def test_the_report_op_reads_the_api_once_without_the_browser_and_labels_undecided_numbers_as_leaning():
    # given
    api = Api()
    w = Wingify(TOKEN, transport=api)

    # when
    out = read(w, wanted({'op': 'report', 'id': '101'}))

    # then
    assert api.calls == [f'{API}/accounts/current/campaigns/101']
    assert out['from'] == REPORT_FROM and out['campaign']['status'] == 'RUNNING'
    assert out['verdict'] == ['VWO has not decided on any goal yet; every number below is leaning, not decided by VWO']
    assert out['lines'][0] == (f'Add to cart click: "Similar chairs on top" against "Control": conversion 22.5 % {ARROW} 23.9 %, lift +6.4 % (+0.1 % to +12.8 %), '
                               '92.0 % chance to beat control, visitors 6,175 / 6,185; leaning, not decided by VWO')
    assert 'early highs often fade' in out['note'] and TOKEN not in json.dumps(out)


def test_the_report_verdict_holds_only_what_vwo_decided_on_the_primary_goal():
    # given
    goals = [GOALS[0], GOALS[1] | {'isPrimary': True}]
    entries = [CONTROL, entry(VARIATION, 2, 2, {'isWinner': True}), entry(CONTROL, 3, 1), entry(VARIATION, 3, 2, None, decision='DISABLE_VARIATION', isDecisionMade=True)]
    w = Wingify(TOKEN, transport=Api(campaign(entries=entries, goals=goals)))

    # when
    out = read(w, wanted({'op': 'report', 'id': 101}))

    # then
    assert [g['goal'] for g in out['goals'] if g.get('primary')] == ['Form submit']
    assert out['verdict'] == [(f'Form submit (primary goal): "Similar chairs on top" against "Control": conversion 22.5 % {ARROW} 23.9 %, lift +6.4 % (+0.1 % to +12.8 %), '
                               '92.0 % chance to beat control, visitors 6,175 / 6,185; decided by VWO: decision "DISABLE_VARIATION"')]
    assert out['goals'][0]['variations'][0]['status'] == DECIDED and out['goals'][0]['variations'][0]['vwo'] == {'winner': True}


def test_the_tool_reads_reports_with_the_verdict_check_unticked_and_counts_them_with_the_other_wingify_calls(store: Store):
    # given
    api = Api()
    b = box(store, plugin_tools=vwo_tools(api, off_checks=('wingify-verdict',)))

    # when
    answers = [b.call('wingify', {'op': 'report', 'id': 101}) for _ in range(7)]

    # then
    assert [err for _, err in answers] == [False] * 6 + [True] and len(api.calls) == 6
    assert LEANING in answers[0][0] and '"from": "Wingify API"' in answers[0][0] and TOKEN not in answers[0][0]


def test_an_empty_campaign_says_vwo_sent_no_results_yet():
    # given
    w = Wingify(TOKEN, transport=Api(campaign(entries=[], goals=[])))

    # when
    out = read(w, wanted({'op': 'report', 'id': 101}))

    # then
    assert out['goals'] == [] and out['lines'] == [] and out['verdict'] == ['VWO sent no results for this campaign yet']


def test_op_campaign_lists_the_names_of_every_field_the_api_sent_without_their_values():
    # given
    raw = campaign(sharedWith=['a team'])
    w = Wingify(TOKEN, transport=Api(raw))

    # when
    out = read(w, wanted({'op': 'campaign', 'id': 101}))

    # then
    assert out['campaign']['rawKeys'] == sorted(raw) and 'variationGoalData' in out['campaign']['rawKeys']
    assert out['campaign']['thresholds'] == {'winningPercent': 95, 'losingPercent': 5, 'visitors': 100} and 'sharedWith' not in out['campaign']
    assert 'variationGoalData' not in out['campaign'] and 'betterPercentage' not in json.dumps(out), 'results come only labelled, from op report'


def test_a_decision_vwo_stamps_on_the_control_row_is_kept_and_shown_in_the_verdict():
    # given
    entries = [entry(CONTROL, 2, 1, {'isWinner': True}, decision='KEEP_CONTROL', isDecisionMade=True), VARIATION]
    w = Wingify(TOKEN, transport=Api(campaign(entries=entries, goals=GOALS[:1])))

    # when
    out = read(w, wanted({'op': 'report', 'id': 101}))

    # then
    control = out['goals'][0]['control']
    assert control['status'] == DECIDED and control['vwo'] == {'winner': True, 'decisionMade': True, 'decision': 'KEEP_CONTROL'}
    assert out['verdict'] == ['Add to cart click: control "Control": conversion 22.5 %, visitors 6,175; decided by VWO: winner, decision "KEEP_CONTROL"']
    assert out['lines'][0] == out['verdict'][0] and out['lines'][1].endswith(LEANING)


@pytest.mark.parametrize('value, share', [(0.92, 0.92), (92, 0.92), (0, 0.0), (101, None), (-1, None), (True, None), ('0.9', None), (float('nan'), None)])
def test_chances_are_read_as_shares_whether_vwo_sends_a_share_or_a_percent(value, share):
    # given
    raw = campaign(entries=[CONTROL, entry(VARIATION, 2, 2, None, decisionGraphData={'betterPercentage': value})], goals=GOALS[:1])

    # when
    [goal] = results(raw)['goals']

    # then
    assert goal['variations'][0].get('beatChance') == share
