from skopax.plugins.vwo import wingify
from skopax.plugins.vwo.wingify import BUDGET_S, READ_RESERVE_S, VERDICT_EVERY_S, VERDICT_PASS, VERDICT_RETRY_S, VERDICT_SECTION, Wingify, WingifyError, verdict_pass
from skopax.remote import HttpError, Reply
from skopax.sweeps import CHECK_TIMEOUT_S
from tests.fake_http import FakeHttp
from tests.test_wingify import API, DOT, SHOP, TOKEN
from tests.test_wingify_report import CONTROL, GOALS, VARIATION, campaign as detail, entry

NOW = 1_794_380_000.0
LIST = f'{API}/accounts/current/campaigns'
RAN = (VERDICT_PASS, f'{VERDICT_PASS}:access:campaigns', f'{VERDICT_PASS}:access:results')
PRIMARY = [GOALS[0] | {'isPrimary': True}, GOALS[1]]


def listed(cid: int, status: str = 'RUNNING') -> dict:
    return {'id': cid, 'name': f'Product page test {cid}', 'type': 'ab', 'status': status, 'urls': [{'value': f'{SHOP}/products/{cid}'}]}


def leaning(cid: int, better: float = 0.92, visitors: int = 6185) -> dict:
    return detail(cid, entries=[CONTROL, entry(VARIATION, 2, 2, {'possibleWinner': True}, visitorCount=visitors, decisionGraphData={'betterPercentage': better})], goals=PRIMARY)


def winner(cid: int) -> dict:
    return detail(cid, entries=[CONTROL, entry(VARIATION, 2, 2, {'isWinner': True}, isDecisionMade=True)], goals=PRIMARY)


def decided(cid: int, goals: list = PRIMARY, goal: int = 2) -> dict:
    return detail(cid, entries=[entry(CONTROL, goal, 1), entry(VARIATION, goal, 2, None, decision='DISABLE_VARIATION')], goals=goals)


def kept(cid: int, at: float = NOW) -> dict:
    return {'name': f'Product page test {cid}', 'url': f'{SHOP}/products/{cid}', 'seenAt': at}


def entries(sweep) -> dict[str, dict]:
    return {r['id']: {k: v for k, v in r.items() if k != 'id'} for r in sweep.sections[VERDICT_SECTION]}


class Vwo:
    def __init__(self, campaigns: list[dict], answers: dict, more: bool = False):
        self.listed = campaigns
        self.answers = answers
        self.more = more
        self.asked: list[int] = []

    def campaigns(self) -> list[dict]:
        return self.listed

    def listing(self) -> tuple[list[dict], bool]:
        return self.listed, self.more

    def campaign(self, cid: int) -> dict:
        self.asked.append(cid)
        got = self.answers[cid]
        if isinstance(got, Exception):
            raise got
        return got


class Times:
    def __init__(self, *values: float):
        self.values = list(values)

    def __call__(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def test_vwo_picking_a_winner_on_the_primary_goal_raises_a_card_with_the_decision_fields():
    # given
    vwo = Vwo([listed(5), listed(12, 'PAUSED')], {5: winner(5)})

    # when
    out = verdict_pass(vwo, None, NOW)

    # then
    result = out.result
    [card] = result.found
    assert (result.name, result.ok, result.status, result.ran, vwo.asked) == (VERDICT_PASS, True, 'ok', RAN, [5])
    assert card['key'] == 'ab-verdict|-/wingify|c5' and card['label'] == 'A/B test: VWO picked a winner'
    assert (card['sev'], card['verdictKind'], card['ranKey'], card['campaignId'], card['url']) == ('warn', 'winner', VERDICT_PASS, 5, f'{SHOP}/products/5')
    assert (card['goalName'], card['variationName'], card['campaignName']) == ('Add to cart click', 'Similar chairs on top', 'Product page test 5')
    assert 'decided by VWO: winner' in card['verdict'][0]
    assert "carry VWO's own decision" in card['what'] and 'from the Wingify API' in card['what'] and 'The campaign is still running' in card['what']
    assert entries(out)['5']['kind'] == 'winner' and 'cluster' not in card


def test_a_vwo_decision_raises_a_card_that_names_the_decision():
    # given
    vwo = Vwo([listed(7)], {7: decided(7)})

    # when
    out = verdict_pass(vwo, None, NOW)

    # then
    [card] = out.result.found
    assert card['verdictKind'] == 'decision' and card['label'] == 'A/B test: VWO made a decision' and card['decision'] == 'DISABLE_VARIATION'
    assert (card['goalName'], card['variationName']) == ('Add to cart click', 'Similar chairs on top')


def test_a_vwo_decision_on_the_control_row_raises_a_decision_card():
    # given
    raw = detail(11, entries=[entry(CONTROL, 2, 1, {'isWinner': True}, isDecisionMade=True), VARIATION], goals=PRIMARY)
    vwo = Vwo([listed(11)], {11: raw})

    # when
    out = verdict_pass(vwo, None, NOW)

    # then
    [card] = out.result.found
    assert (card['verdictKind'], card['variationName'], card['decision'], card['goalName']) == ('decision', 'Control', None, 'Add to cart click')


def test_a_high_chance_to_beat_control_or_a_possible_winner_without_a_vwo_decision_raises_no_card():
    # given
    vwo = Vwo([listed(101), listed(102)], {101: leaning(101), 102: leaning(102, better=0.999, visitors=60_000)})

    # when
    out = verdict_pass(vwo, None, NOW)

    # then
    assert out.result.ok and out.result.status == 'ok' and out.result.found == [] and sorted(vwo.asked) == [101, 102]
    assert out.sections[VERDICT_SECTION] == [{'id': '101', 'readAt': NOW} | kept(101), {'id': '102', 'readAt': NOW} | kept(102)]


def test_a_decision_on_a_secondary_goal_counts_only_when_no_goal_is_primary():
    # given
    secondary = decided(8, goals=PRIMARY, goal=3)
    no_primary = decided(9, goals=GOALS, goal=3)
    vwo = Vwo([listed(8), listed(9)], {8: secondary, 9: no_primary})

    # when
    out = verdict_pass(vwo, None, NOW)

    # then
    assert [c['key'] for c in out.result.found] == ['ab-verdict|-/wingify|c9'] and out.result.found[0]['goalName'] == 'Form submit'


def test_results_are_read_again_only_after_six_hours_and_a_stopped_test_drops_its_card():
    # given
    vwo = Vwo([listed(101)], {101: winner(101)})
    first = verdict_pass(vwo, None, NOW)
    stored = first.sections[VERDICT_SECTION]

    # when
    soon = verdict_pass(vwo, stored, NOW + 3600)
    later = verdict_pass(vwo, stored, NOW + VERDICT_EVERY_S + 1)
    stopped = verdict_pass(Vwo([listed(101, 'PAUSED')], {}), stored, NOW + 7200)

    # then
    assert vwo.asked == [101, 101], 'read once, then again only after six hours'
    assert [c['key'] for c in soon.result.found] == ['ab-verdict|-/wingify|c101'], 'the stored decision keeps the card open between reads'
    assert later.result.found and stopped.result.found == [] and stopped.sections[VERDICT_SECTION] == []
    assert stopped.result.ran == RAN, 'a listing without the test closes its card'


def test_a_failed_read_keeps_the_earlier_decision_says_which_test_failed_and_is_due_again_after_fifty_minutes():
    # given
    stored = verdict_pass(Vwo([listed(101)], {101: winner(101)}), None, NOW - VERDICT_EVERY_S - 10).sections[VERDICT_SECTION]
    vwo = Vwo([listed(101)], {101: WingifyError('Wingify HTTP 500 on /campaigns/101')})

    # when
    result = verdict_pass(vwo, stored, NOW)
    early = verdict_pass(vwo, result.sections[VERDICT_SECTION], NOW + VERDICT_RETRY_S - 60)
    again = verdict_pass(vwo, result.sections[VERDICT_SECTION], NOW + VERDICT_RETRY_S)

    # then
    assert result.result.ok and result.result.status == f'ok {DOT} 1 result read failed: #101: Wingify HTTP 500 on /campaigns/101'
    assert [c['key'] for c in result.result.found] == ['ab-verdict|-/wingify|c101'] and entries(result)['101']['kind'] == 'winner'
    assert entries(result)['101']['error'] == 'Wingify HTTP 500 on /campaigns/101'
    assert vwo.asked == [101, 101] and early.result.status == 'ok' and again.result.status.startswith(f'ok {DOT} 1 result read failed')


def test_at_most_three_tests_are_read_per_run_never_read_ones_first():
    # given
    stored = [{'id': '1', 'readAt': NOW - 60}, {'id': '2', 'readAt': NOW - 60}]
    vwo = Vwo([listed(1), listed(2), listed(3), listed(4), listed(5), listed(6)], {n: leaning(n) for n in range(1, 7)})

    # when
    verdict_pass(vwo, stored, NOW)

    # then
    assert vwo.asked == [3, 4, 5], 'never-read tests first, at most three a run, and the ones read in the last six hours wait'


def test_an_entry_stored_by_the_old_share_link_reader_raises_no_card_and_does_not_fail():
    # given
    stored = [{'id': '101', 'readAt': NOW - 60, 'lines': ['We recommend you to pause the campaign.']},
              {'id': '102', 'readAt': NOW - 60, 'lines': [], 'error': 'the share link opened the login page'}]
    vwo = Vwo([listed(101), listed(102)], {102: leaning(102)})

    # when
    out = verdict_pass(vwo, stored, NOW + VERDICT_RETRY_S)

    # then
    assert out.result.ok and out.result.found == [] and vwo.asked == [102]


def test_a_rate_limit_waits_and_tries_once_more_then_moves_on_and_that_test_is_read_first_next_run():
    # given
    calls: list[str] = []
    limited = {'101': 4}

    def api(method, url, headers, data, timeout):
        calls.append(url.split('/accounts/current')[1])
        if '/campaigns?' in url:
            return Reply({'_data': [listed(193), listed(101), listed(195)]}, {})
        cid = url.rsplit('/', 1)[1]
        if limited.get(cid, 0) > 0:
            limited[cid] -= 1
            raise HttpError(429, url, '{"message": "Too many requests"}')
        return Reply({'_data': winner(int(cid))}, {})

    w = Wingify(TOKEN, transport=api)
    waits: list[float] = []

    # when
    first = verdict_pass(w, None, NOW, sleep=waits.append)
    again = verdict_pass(w, first.sections[VERDICT_SECTION], NOW + 3600, sleep=waits.append)
    third = verdict_pass(w, again.sections[VERDICT_SECTION], NOW + 7200, sleep=waits.append)

    # then
    assert calls[:5] == ['/campaigns?limit=100&offset=0', '/campaigns/193', '/campaigns/101', '/campaigns/101', '/campaigns/195']
    assert waits[0] == wingify.VERDICT_WAIT_S
    assert first.result.status == (f'ok {DOT} 1 result read failed: #101: Wingify HTTP 429 on /campaigns/101: rate limited, try again in a minute, twice, '
                                   'so it is read first next run')
    assert set(entries(first)) == {'193', '195'}, '101 is not stamped as read'
    assert sorted(c['campaignId'] for c in first.result.found) == [193, 195]
    assert calls[5:] == ['/campaigns?limit=100&offset=0', '/campaigns/101', '/campaigns/101', '/campaigns?limit=100&offset=0', '/campaigns/101']
    assert again.result.status.startswith(f'ok {DOT} 1 result read failed: #101') and third.result.status == 'ok' and len(third.result.found) == 3
    assert TOKEN not in first.result.status


def test_an_empty_campaign_list_is_a_quiet_pass_that_reads_nothing_and_closes_nothing():
    # given
    vwo = Vwo([], {})
    stored = [{'id': '101', 'readAt': NOW - 60, 'kind': 'winner', 'lines': ['decided'], 'hit': {'goal': 'Add to cart click'}}]

    # when
    out = verdict_pass(vwo, stored, NOW)

    # then
    assert (out.result.ok, out.result.status, out.result.ran, out.result.found, out.sections, vwo.asked) == (True, f'ok {DOT} no data', (f'{VERDICT_PASS}:no-data', f'{VERDICT_PASS}:access:campaigns'), [], {}, [])


def test_a_cut_listing_keeps_the_cards_of_unlisted_tests_closes_those_that_stopped_and_forgets_after_a_week():
    # given
    paused = [listed(n, 'PAUSED') for n in range(1, 301)]
    stored = [{'id': '999', 'readAt': NOW - 60, 'kind': 'winner', 'lines': ['decided'], 'hit': {'goal': 'Add to cart click'}} | kept(999, NOW - 60),
              {'id': '7', 'readAt': NOW - 60, 'kind': 'winner', 'lines': ['decided']} | kept(7, NOW - 60),
              {'id': '998', 'readAt': NOW - 60, 'kind': 'winner', 'lines': ['decided']} | kept(998, NOW - 8 * 86_400)]

    # when
    cut = verdict_pass(Vwo(paused, {}, more=True), stored, NOW)
    whole = verdict_pass(Vwo(paused, {}), stored, NOW)

    # then
    assert (cut.result.ok, cut.result.status, cut.result.ran) == (True, f'ok {DOT} read 300 campaigns, more not read', RAN)
    assert set(entries(cut)) == {'999'}, 'an unlisted test keeps its entry for a week; a listed one that stopped drops it'
    [card] = cut.result.found
    assert (card['key'], card['campaignId'], card['campaignName'], card['url']) == ('ab-verdict|-/wingify|c999', 999, 'Product page test 999', f'{SHOP}/products/999')
    assert 'was running when it was last listed' in card['what'] and 'still running' not in card['what']
    assert whole.result.status == 'ok' and whole.result.ran == RAN and entries(whole) == {} and whole.result.found == []


def test_campaigns_without_a_whole_number_id_are_never_read():
    # given
    vwo = Vwo([{'id': 'abc', 'name': 'Odd', 'status': 'RUNNING'}, {'id': True, 'name': 'Flag', 'status': 'RUNNING'}, listed(5)], {5: winner(5)})

    # when
    out = verdict_pass(vwo, None, NOW)

    # then
    assert out.result.ok and vwo.asked == [5] and set(entries(out)) == {'5'}


def test_the_first_pass_that_reads_results_again_closes_the_results_access_card_however_many_tests_wait():
    # given
    state = {'refuse': True}

    def api(method, url, headers, data, timeout):
        if '/campaigns?' in url:
            return Reply({'_data': [listed(n) for n in range(1, 6)]}, {})
        cid = int(url.rsplit('/', 1)[1])
        if state['refuse']:
            raise HttpError(403, url)
        return Reply({'_data': leaning(cid)}, {})

    w = Wingify(TOKEN, transport=api)

    # when
    first = verdict_pass(w, None, NOW)
    second = verdict_pass(w, first.sections[VERDICT_SECTION], NOW + 3600)
    state['refuse'] = False
    fixed = verdict_pass(w, second.sections[VERDICT_SECTION], NOW + 7200)

    # then
    assert [c['ranKey'] for c in second.result.found] == [f'{VERDICT_PASS}:access:results'] and second.result.status == f'ok {DOT} no access to campaign results'
    assert (fixed.result.status, fixed.result.found, fixed.result.ran) == ('ok', [], RAN), 'three tests read, so the access card closes now'
    assert not any('refused' in e for e in entries(fixed).values())
    assert {k for k, e in entries(fixed).items() if 'error' in e} == {'4', '5'}, 'the two tests not read yet keep their error, so they stay due'


def test_a_failed_listing_fails_the_pass_with_its_fixed_reason():
    # given
    w = Wingify(TOKEN, transport=FakeHttp({('GET', LIST): HttpError(500, LIST, f'bad token {TOKEN}')}))

    # when
    out = verdict_pass(w, None, NOW)

    # then
    assert (out.result.ok, out.result.status, out.sections) == (False, 'error: Wingify HTTP 500 on /campaigns', {})


def test_a_refused_listing_keeps_the_verdict_cards_open_and_raises_an_access_card():
    # given
    w = Wingify(TOKEN, transport=FakeHttp({('GET', LIST): HttpError(403, LIST, f'no rights for {TOKEN}')}))
    stored = [{'id': '101', 'readAt': NOW - 60, 'kind': 'winner', 'lines': ['decided'], 'hit': {'goal': 'Add to cart click'}}]

    # when
    out = verdict_pass(w, stored, NOW)

    # then
    [card] = out.result.found
    assert (out.result.ok, out.result.status, out.sections) == (True, f'ok {DOT} no access to campaigns', {})
    assert out.result.ran == (f'{VERDICT_PASS}:access:campaigns', f'{VERDICT_PASS}:access:results'), 'the verdict cards keep their key out of ran'
    assert (card['ranKey'], card['sev'], card['label']) == (f'{VERDICT_PASS}:access:campaigns', 'warn', 'VWO cannot read campaigns: the token lacks Browse permission')
    assert TOKEN not in card['what'] and 'HTTP 403' in card['what']


def test_a_refused_results_read_is_no_access_never_no_decision_and_clears_when_the_results_read_again():
    # given
    answers = {'code': 403}

    def campaign_of(query, body):
        return HttpError(answers['code'], f'{LIST}/101') if answers['code'] else {'_data': winner(101)}

    http = FakeHttp({('GET', LIST): {'_data': [listed(101), listed(102)]}, ('GET', f'{LIST}/101'): campaign_of, ('GET', f'{LIST}/102'): {'_data': leaning(102)}})
    w = Wingify(TOKEN, transport=http)
    stored = [{'id': '101', 'readAt': NOW - VERDICT_EVERY_S - 10, 'kind': 'winner', 'lines': ['decided'], 'hit': {'goal': 'Add to cart click'}}]

    # when
    refused = verdict_pass(w, stored, NOW)
    waiting = verdict_pass(w, refused.sections[VERDICT_SECTION], NOW + 600)
    answers['code'] = 0
    back = verdict_pass(w, waiting.sections[VERDICT_SECTION], NOW + VERDICT_RETRY_S)

    # then
    cards = {c['ranKey']: c for c in refused.result.found}
    assert refused.result.ok and refused.result.status == f'ok {DOT} no access to campaign results'
    assert refused.result.ran == RAN
    assert set(cards) == {VERDICT_PASS, f'{VERDICT_PASS}:access:results'} and cards[VERDICT_PASS]['campaignId'] == 101, 'the earlier decision stays open'
    assert cards[f'{VERDICT_PASS}:access:results']['label'] == 'VWO cannot read campaign results: the token lacks Browse permission'
    assert '#101' in cards[f'{VERDICT_PASS}:access:results']['what'] and entries(refused)['101']['refused'] == 403 and entries(refused)['102'] == {'readAt': NOW} | kept(102)
    assert {c['ranKey'] for c in waiting.result.found} == {VERDICT_PASS, f'{VERDICT_PASS}:access:results'}, 'not read again yet, so the access card stays'
    assert back.result.status == 'ok' and [c['ranKey'] for c in back.result.found] == [VERDICT_PASS] and 'refused' not in entries(back)['101']


def test_reads_that_would_pass_the_time_budget_wait_for_the_next_run_and_are_read_first_then():
    # given
    vwo = Vwo([listed(1), listed(2), listed(3)], {n: winner(n) for n in range(1, 4)})
    clock = Times(0.0, 0.0, BUDGET_S)

    # when
    first = verdict_pass(vwo, None, NOW, clock=clock)
    asked = list(vwo.asked)
    second = verdict_pass(vwo, first.sections[VERDICT_SECTION], NOW + 3600)

    # then
    assert asked == [1] and set(entries(first)) == {'1'}
    assert first.result.status == f'ok {DOT} read 1 of 3 campaign results'
    assert vwo.asked == [1, 2, 3] and second.result.status == 'ok' and len(second.result.found) == 3
    assert BUDGET_S < CHECK_TIMEOUT_S and READ_RESERVE_S == 2 * wingify.TIMEOUT + wingify.VERDICT_WAIT_S
