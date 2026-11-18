import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import mongomock
import pytest

from skopax.contract import ReadError, iso
from skopax.instances import Instances, PluginState
from skopax.plugins import vwo
from skopax.plugins.vwo.wingify import NO_TOKEN, Refused, Wingify, WingifyError, read, run_pass, verdict_pass, wanted
from skopax.plugintools import PluginTools
from skopax.registry import Registry
from skopax.remote import HttpError, Reply, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.plugin_kit import KIT_KEY, add_instance

TOKEN = 'vwo-token-VERYSECRET'
NOW = 1_794_306_000.0
SHOP = 'https://shop.example.com'
API = 'https://app.wingify.com/api/v2'
ARROW = chr(0x2192)
DOT = chr(0xb7)
NAME = 'Promo test alice@example.com\n+1 555 123 4567'
CAMPAIGNS = [{'id': 7, 'name': 'Product list  CTA colour', 'type': 'ab', 'status': 'RUNNING', 'platform': 'website', 'createdOn': 1_794_000_000, 'percentTraffic': 50,
              'primaryUrl': f'{SHOP}/products', 'urls': [{'type': 'url', 'value': f'{SHOP}/products'}, {'type': 'pattern', 'value': f'{SHOP}/products/*'}]},
             {'id': 9, 'name': 'Checkout form', 'type': 'split', 'status': 'PAUSED', 'createdOn': 1_793_000_000, 'urls': [{'type': 'url', 'value': f'{SHOP}/checkout'}]},
             {'id': 4, 'name': 'Old experiment', 'type': 'ab', 'status': 'STOPPED', 'createdOn': 1_780_000_000, 'urls': []}]


class Api:
    def __init__(self, campaigns=None, fail: int | None = None, beyond: int | None = None):
        self.campaigns = CAMPAIGNS if campaigns is None else campaigns
        self.fail = fail
        self.beyond = beyond
        self.calls = []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers))
        if self.fail:
            raise HttpError(self.fail, url.split('?')[0], '{"message": "no"}')
        if '/campaigns/' in url:
            cid = int(url.rsplit('/', 1)[1])
            one = next(c for c in self.campaigns if c['id'] == cid)
            return Reply({'_data': one | {'variations': [{'id': 1, 'name': 'Control', 'isControl': True, 'percentSplit': 50}, {'id': 2, 'name': 'Green button', 'isControl': False, 'percentSplit': 50}],
                                          'goals': [{'id': 1, 'name': 'Sign-up', 'type': 'visitPage'}], 'stats': {'visitors': 1200}}}, {})
        query = parse_qs(urlsplit(url).query)
        start = int(query.get('offset', ['0'])[0])
        if self.beyond and start >= len(self.campaigns):
            raise HttpError(self.beyond, url.split('?')[0])
        return Reply({'_data': self.campaigns[start:start + int(query.get('limit', [len(self.campaigns) or 1])[0])]}, {})

    def pages(self) -> list[str]:
        return [urlsplit(url).query for _, url, _ in self.calls]


def parts(sweep, part: str) -> list[dict]:
    return [r for r in sweep.sections['wingify'] if r['part'] == part]


def paused(n: int, created: float = 1_700_000_000) -> dict:
    return {'id': n, 'name': f'Test {n}', 'type': 'ab', 'status': 'PAUSED', 'createdOn': created + n}


def ids_of(sweep) -> set:
    return {r['id'] for r in parts(sweep, 'campaign')}


def summary_of(sweep) -> dict:
    [row] = parts(sweep, 'summary')
    return row


def vwo_runtime(transport=None, off_checks: tuple[str, ...] = (), **values) -> Runtime:
    db = mongomock.MongoClient(tz_aware=True)['vwo']
    registry = Registry.of(vwo.PLUGIN)
    runtime = Runtime(registry, PluginState(db), Instances(db, registry, lambda: Sealer(KIT_KEY)), transport)
    add_instance(SimpleNamespace(db=db), vwo.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, **({'api-token': TOKEN} | values))
    if off_checks:
        db['instances'].update_one({'_id': 'vwo/main'}, {'$set': {'offChecks': list(off_checks)}})
    runtime.refresh()
    return runtime


def vwo_tools(transport=None, **values) -> PluginTools:
    return PluginTools(vwo_runtime(transport, **values))


def test_a_client_without_a_token_reads_nothing_and_one_with_a_token_sends_it_in_a_header():
    # given
    api = Api()

    # when
    with pytest.raises(WingifyError) as e:
        Wingify('', transport=api).campaigns()
    asked_off = list(api.calls)
    rows = Wingify(f' {TOKEN} ', transport=api).campaigns()

    # then
    assert str(e.value) == NO_TOKEN and e.value.reason == NO_TOKEN and isinstance(e.value, ReadError) and asked_off == []
    assert Wingify('', transport=api).enabled is False and Wingify(TOKEN, transport=api).enabled and len(rows) == 3
    assert api.calls == [('GET', f'{API}/accounts/current/campaigns?limit=100&offset=0', {'Accept': 'application/json', 'token': TOKEN})]


def test_campaigns_come_back_running_first_with_their_pages_and_can_be_filtered():
    # given
    w = Wingify(TOKEN, transport=Api())

    # when
    everything = read(w, wanted({}))
    running = read(w, wanted({'op': 'campaigns', 'status': 'running'}))
    found = read(w, wanted({'op': 'campaigns', 'contains': 'checkout'}))

    # then
    assert everything['byStatus'] == {'RUNNING': 1, 'PAUSED': 1, 'STOPPED': 1} and [c['id'] for c in everything['campaigns']] == [7, 9, 4]
    assert everything['campaigns'][0] == {'id': 7, 'name': 'Product list CTA colour', 'type': 'ab', 'status': 'RUNNING', 'platform': 'website', 'urls': [f'{SHOP}/products', f'{SHOP}/products/*'],
                                          'createdOn': iso(1_794_000_000), 'percentTraffic': 50}
    assert [c['id'] for c in running['campaigns']] == [7] and [c['id'] for c in found['campaigns']] == [9]
    assert everything['truncated'] is False


def test_one_campaign_carries_its_variations_goals_and_stats_and_bad_inputs_are_refused():
    # given
    w = Wingify(TOKEN, account='12345', transport=Api())

    # when
    one = read(w, wanted({'op': 'campaign', 'id': '7'}))['campaign']
    errors = []
    for bad in ({'op': 'delete'}, {'op': 'campaign'}, {'op': 'campaign', 'id': '7; drop'}, {'op': 'campaigns', 'status': 'run ning'}):
        with pytest.raises(WingifyError) as e:
            wanted(bad)
        errors.append(e.value)

    # then
    assert one['variations'][1] == {'id': 2, 'name': 'Green button', 'isControl': False, 'percentSplit': 50} and one['goals'] == [{'id': 1, 'name': 'Sign-up', 'type': 'visitPage'}]
    assert one['stats'] == {'visitors': 1200} and w.transport.calls[0][1] == f'{API}/accounts/12345/campaigns/7'
    assert errors[0].reason == 'op is one of campaigns, campaign, report' and 'needs id' in errors[1].reason and 'needs id' in errors[2].reason
    assert 'status is a Wingify status' in errors[3].reason and {e.status for e in errors} == {400}


def test_an_account_that_is_not_current_or_a_number_falls_back_to_current():
    # when
    accounts = [Wingify(TOKEN, account=given, transport=Api()).account for given in ('12345', ' current ', '', '../users', '1234567890123')]

    # then
    assert accounts == ['12345', 'current', 'current', 'current', 'current']


@pytest.mark.parametrize('code, said', [(401, 'the token was refused'), (403, 'Browse permission is enough'), (429, 'rate limited'), (500, 'Wingify HTTP 500')])
def test_an_api_error_says_what_it_means_and_never_carries_the_token(code, said):
    # when
    with pytest.raises(WingifyError) as e:
        Wingify(TOKEN, transport=Api(fail=code)).campaigns()

    # then
    assert said in e.value.reason and TOKEN not in e.value.reason and e.value.reason == str(e.value) == e.value.message and e.value.status == 502
    assert ('Browse permission' in e.value.reason) == (code in (401, 403)) and (isinstance(e.value, Refused)) == (code in (401, 403))


def test_an_unreachable_api_gives_a_fixed_reason_without_the_token():
    # given
    def down(method, url, headers, data, timeout):
        raise Unreachable(f'{url} unreachable ({TOKEN})')

    # when
    with pytest.raises(WingifyError) as e:
        Wingify(TOKEN, transport=down).campaigns()

    # then
    assert e.value.reason == 'Wingify unreachable on /campaigns' and TOKEN not in e.value.reason


def test_the_check_learns_the_campaigns_first_and_then_raises_a_card_when_one_starts_stops_or_appears():
    # given
    first = run_pass(Wingify(TOKEN, transport=Api()), None, NOW)
    later = [dict(CAMPAIGNS[0], status='PAUSED'), dict(CAMPAIGNS[1], status='RUNNING'), CAMPAIGNS[2],
             {'id': 12, 'name': 'New landing', 'type': 'ab', 'status': 'NOT_STARTED', 'urls': [{'type': 'url', 'value': f'{SHOP}/campaign'}]}]

    # when
    second = run_pass(Wingify(TOKEN, transport=Api(later)), first.sections['wingify'], NOW + 300)
    third = run_pass(Wingify(TOKEN, transport=Api(later)), second.sections['wingify'], NOW + 600)
    old = run_pass(Wingify(TOKEN, transport=Api(later)), third.sections['wingify'], NOW + 25 * 3600)
    down = run_pass(Wingify(TOKEN, transport=Api(fail=500)), None, NOW)

    # then
    assert first.result.ok and first.result.found == [] and summary_of(first)['running'] == 1 and summary_of(first)['total'] == 3
    assert (first.result.name, first.result.status, first.result.ran) == ('wingify', 'ok', ('wingify', 'wingify:access:campaigns'))
    cards = {c['key']: c for c in second.result.found}
    assert sorted(cards) == ['ab-test|-/wingify|c12', 'ab-test|-/wingify|c7', 'ab-test|-/wingify|c9']
    assert cards['ab-test|-/wingify|c7']['label'] == 'A/B test stopped' and cards['ab-test|-/wingify|c9']['label'] == 'A/B test started' and cards['ab-test|-/wingify|c12']['label'] == 'A/B test created'
    started = cards['ab-test|-/wingify|c9']
    assert started['sev'] == 'info' and started['src'] == 'wingify' and started['url'] == f'{SHOP}/checkout' and started['campaignId'] == 9
    assert 'went from PAUSED to RUNNING' in started['what'] and f'on {SHOP}/checkout' in started['what'] and 'without a deploy' in started['what']
    assert len(third.result.found) == 3 and len(parts(third, 'change')) == 3, 'the cards stay for a day and nothing is counted twice'
    assert old.result.found == [] and down.result.ok is False and 'VERYSECRET' not in down.result.status
    assert down.result.status == 'error: Wingify HTTP 500 on /campaigns' and down.sections == {}


def test_a_running_experiment_whose_pages_or_traffic_share_change_raises_a_changed_card_and_a_paused_one_does_not():
    # given
    first = run_pass(Wingify(TOKEN, transport=Api()), None, NOW)
    edited = [dict(CAMPAIGNS[0], percentTraffic=100, primaryUrl=f'{SHOP}/products/new-arrivals', urls=[{'type': 'url', 'value': f'{SHOP}/products/new-arrivals'}]),
              dict(CAMPAIGNS[1], urls=[{'type': 'url', 'value': f'{SHOP}/elsewhere'}]), CAMPAIGNS[2]]

    # when
    second = run_pass(Wingify(TOKEN, transport=Api(edited)), first.sections['wingify'], NOW + 300)
    third = run_pass(Wingify(TOKEN, transport=Api(edited)), second.sections['wingify'], NOW + 600)

    # then
    [card] = second.result.found
    assert card['key'] == 'ab-test|-/wingify|c7|edit' and card['label'] == 'A/B test changed'
    assert (f'was changed while RUNNING: pages {SHOP}/products, {SHOP}/products/* {ARROW} {SHOP}/products/new-arrivals; traffic share 50 % {ARROW} 100 %') in card['what']
    assert len(third.result.found) == 1 and len(parts(third, 'change')) == 1, 'the edit is reported once'


def test_campaign_names_are_redacted_while_page_urls_stay_whole_so_their_links_still_open():
    # given
    page = f'{SHOP}/campaign/10000000078'
    campaign = {'id': 31, 'name': NAME, 'type': 'ab', 'status': 'RUNNING', 'urls': [{'type': 'url', 'value': page}]}
    w = Wingify('token', transport=lambda method, url, headers, data, timeout: Reply({'_data': [campaign]}, {}))
    learned = [{'part': 'summary', 'checkedAt': iso(NOW - 300), 'running': 0, 'total': 0}]

    # when
    out = run_pass(w, learned, NOW)

    # then
    [card] = out.result.found
    rows = parts(out, 'campaign')
    shown = json.dumps([card['what'], card['short'], rows, parts(out, 'change')])
    assert 'alice' not in shown and '555' not in shown and 'Promo test [redacted] [redacted]' in card['what']
    assert card['url'] == page and page in card['what'] and rows[0]['urls'] == [page]


def test_the_section_is_a_list_of_rows_one_per_campaign_with_one_summary_row_and_no_reserved_keys():
    # given
    first = run_pass(Wingify(TOKEN, transport=Api()), None, NOW)

    # when
    rows = first.sections['wingify']

    # then
    assert set(first.sections) == {'wingify'} and all(isinstance(r, dict) for r in rows)
    assert [r['part'] for r in rows] == ['summary', 'campaign', 'campaign', 'campaign']
    assert summary_of(first) == {'part': 'summary', 'checkedAt': iso(NOW), 'running': 1, 'total': 3}
    assert parts(first, 'campaign')[1] == {'part': 'campaign', 'id': 9, 'name': 'Checkout form', 'type': 'split', 'status': 'PAUSED', 'urls': [f'{SHOP}/checkout'], 'seenAt': iso(NOW)}
    assert not any(k in r for r in rows for k in ('cluster', 'plugin'))


def test_rows_read_back_from_the_state_with_core_keys_are_compared_the_same_way():
    # given
    first = run_pass(Wingify(TOKEN, transport=Api()), None, NOW)
    stored = [dict(r, plugin='vwo') for r in first.sections['wingify']]
    later = [dict(CAMPAIGNS[0], status='PAUSED'), CAMPAIGNS[1], CAMPAIGNS[2]]

    # when
    second = run_pass(Wingify(TOKEN, transport=Api(later)), stored, NOW + 300)

    # then
    assert [c['key'] for c in second.result.found] == ['ab-test|-/wingify|c7']
    assert not any(k in r for r in second.sections['wingify'] for k in ('cluster', 'plugin'))


def test_an_empty_campaign_list_is_a_quiet_pass_that_keeps_the_rows_and_closes_nothing():
    # given
    w = Wingify(TOKEN, transport=Api([]))
    learned = run_pass(Wingify(TOKEN, transport=Api()), None, NOW).sections['wingify']

    # when
    quiet = run_pass(w, learned, NOW + 300)
    nameless = run_pass(Wingify(TOKEN, transport=Api([{'name': 'no id', 'status': 'RUNNING'}])), learned, NOW + 300)

    # then
    for out in (quiet, nameless):
        assert (out.result.ok, out.result.status, out.result.ran, out.result.found, out.sections) == (True, f'ok {DOT} no data', ('wingify:no-data', 'wingify:access:campaigns'), [], {})


@pytest.mark.parametrize('total, offsets, status', [
    (299, ['limit=100&offset=0', 'limit=100&offset=100', 'limit=100&offset=200'], 'ok'),
    (300, ['limit=100&offset=0', 'limit=100&offset=100', 'limit=100&offset=200', 'limit=100&offset=300'], 'ok'),
    (301, ['limit=100&offset=0', 'limit=100&offset=100', 'limit=100&offset=200', 'limit=100&offset=300'], f'ok {DOT} read 300 campaigns, more not read')])
def test_the_listing_reads_pages_of_100_and_one_more_row_tells_a_full_list_from_a_cut_one(total, offsets, status):
    # given
    api = Api([paused(n) for n in range(1, total + 1)])

    # when
    out = run_pass(Wingify(TOKEN, transport=api), None, NOW)

    # then
    assert api.pages() == offsets and out.result.status == status and len(ids_of(out)) == min(total, 300)
    assert out.result.ran == ('wingify', 'wingify:access:campaigns') and summary_of(out).get('cut', False) is (total > 300)


def test_a_cut_listing_carries_the_campaigns_it_left_out_and_learns_older_ones_that_slide_in_without_a_created_card():
    # given
    rows = [paused(n) for n in range(1, 311)]
    learned = run_pass(Wingify(TOKEN, transport=Api(rows)), None, NOW).sections['wingify']
    fresh = {'id': 400, 'name': 'Brand new test', 'type': 'ab', 'status': 'PAUSED', 'createdOn': NOW + 400}

    # when
    slid = run_pass(Wingify(TOKEN, transport=Api([r for r in rows if r['id'] != 5])), learned, NOW + 300)
    added = run_pass(Wingify(TOKEN, transport=Api([fresh] + [r for r in rows if r['id'] != 5])), slid.sections['wingify'], NOW + 600)
    back = run_pass(Wingify(TOKEN, transport=Api(rows)), added.sections['wingify'], NOW + 900)

    # then
    assert slid.result.found == [] and {5, 301} <= ids_of(slid), 'campaign 301 slid into the window but is older than the last check'
    assert slid.result.status == f'ok {DOT} read 300 campaigns, more not read'
    assert [c['key'] for c in added.result.found] == ['ab-test|-/wingify|c400'] and added.result.found[0]['label'] == 'A/B test created'
    assert [c['key'] for c in back.result.found] == ['ab-test|-/wingify|c400'], 'campaign 5 was carried, so it is not created again'


def test_a_carried_campaign_row_is_dropped_after_a_week_unlisted():
    # given
    rows = [paused(n) for n in range(1, 311)]
    learned = run_pass(Wingify(TOKEN, transport=Api(rows)), None, NOW).sections['wingify']
    without = [r for r in rows if r['id'] != 5]

    # when
    day = run_pass(Wingify(TOKEN, transport=Api(without)), learned, NOW + 86_400)
    week = run_pass(Wingify(TOKEN, transport=Api(without)), day.sections['wingify'], NOW + 8 * 86_400)

    # then
    assert 5 in ids_of(day) and 5 not in ids_of(week)
    assert [r['seenAt'] for r in parts(day, 'campaign') if r['id'] == 5] == [iso(NOW)]


def test_a_campaign_without_a_creation_time_that_slides_into_a_cut_listing_is_carded_first_seen_not_created():
    # given
    rows = [paused(n) for n in range(1, 311)]
    rows[300] = {k: v for k, v in rows[300].items() if k != 'createdOn'}
    learned = run_pass(Wingify(TOKEN, transport=Api(rows)), None, NOW).sections['wingify']

    # when
    slid = run_pass(Wingify(TOKEN, transport=Api([r for r in rows if r['id'] != 5])), learned, NOW + 300)

    # then
    [card] = slid.result.found
    assert (card['key'], card['label'], card['sev']) == ('ab-test|-/wingify|c301', 'A/B test first seen', 'info')
    assert 'is listed for the first time with status PAUSED' in card['what'] and 'may be older' in card['what'] and 'created' not in card['label']
    assert [r.get('firstSeen') for r in parts(slid, 'change')] == [True]


@pytest.mark.parametrize('code', [400, 404, 416, 422])
def test_an_error_on_the_one_row_read_past_300_campaigns_means_the_list_is_whole(code):
    # given
    api = Api([paused(n) for n in range(1, 301)], beyond=code)

    # when
    changes = run_pass(Wingify(TOKEN, transport=api), None, NOW)
    verdicts = verdict_pass(Wingify(TOKEN, transport=Api([paused(n) for n in range(1, 301)], beyond=code)), None, NOW)

    # then
    assert (changes.result.ok, changes.result.status, len(ids_of(changes))) == (True, 'ok', 300) and api.pages()[-1] == 'limit=100&offset=300'
    assert (verdicts.result.ok, verdicts.result.status) == (True, 'ok')


def test_unreadable_stored_times_never_wedge_the_change_check():
    # given
    rows = [paused(n) for n in range(1, 311)]
    learned = run_pass(Wingify(TOKEN, transport=Api(rows)), None, NOW).sections['wingify']
    garbled = [dict(r, checkedAt='garbled') if r['part'] == 'summary' else {k: v for k, v in r.items() if k != 'seenAt'} for r in learned]

    # when
    out = run_pass(Wingify(TOKEN, transport=Api([r for r in rows if r['id'] != 5])), garbled, NOW + 300)

    # then
    assert out.result.ok and 5 in ids_of(out) and [r['seenAt'] for r in parts(out, 'campaign') if r['id'] == 5] == [iso(NOW + 300)]


def test_the_tool_says_when_the_campaign_list_goes_past_what_it_read():
    # when
    cut = read(Wingify(TOKEN, transport=Api([paused(n) for n in range(1, 302)])), wanted({}))
    whole = read(Wingify(TOKEN, transport=Api()), wanted({}))

    # then
    assert cut['more'] is True and len(cut['campaigns']) == 100 and cut['truncated'] is True
    assert 'more' not in whole


def test_campaigns_without_a_whole_number_id_are_left_out_of_the_check():
    # given
    odd = [{'id': 'abc', 'name': 'Odd', 'status': 'RUNNING'}, {'id': True, 'name': 'Flag', 'status': 'RUNNING'}, {'id': '8', 'name': 'Text id', 'status': 'PAUSED'}]

    # when
    out = run_pass(Wingify(TOKEN, transport=Api(odd + CAMPAIGNS)), None, NOW)

    # then
    assert out.result.ok and ids_of(out) == {'8', 7, 9, 4} and summary_of(out)['total'] == 4


@pytest.mark.parametrize('code', [401, 403])
def test_a_refused_campaign_list_is_no_access_never_no_campaigns_and_keeps_the_change_cards_open(code):
    # given
    learned = run_pass(Wingify(TOKEN, transport=Api()), None, NOW).sections['wingify']

    # when
    out = run_pass(Wingify(TOKEN, transport=Api(fail=code)), learned, NOW + 300)

    # then
    [card] = out.result.found
    assert (out.result.ok, out.result.status, out.result.ran, out.sections) == (True, f'ok {DOT} no access to campaigns', ('wingify:access:campaigns',), {})
    lacks = 'is invalid or lacks' if code == 401 else 'lacks'
    assert (card['ranKey'], card['sev'], card['label'], card['src']) == ('wingify:access:campaigns', 'warn', f'VWO cannot read campaigns: the token {lacks} Browse permission', 'wingify')
    assert f'HTTP {code}' in card['what'] and TOKEN not in json.dumps(card) and 'cluster' not in card


def test_a_stored_change_with_an_unreadable_time_is_left_out_instead_of_failing_the_pass():
    # given
    learned = run_pass(Wingify(TOKEN, transport=Api()), None, NOW).sections['wingify']
    broken = learned + [{'part': 'change', 'id': 4, 'name': 'Old experiment', 'urls': [], 'from': 'RUNNING', 'to': 'STOPPED', 'at': 'yesterday'}]

    # when
    out = run_pass(Wingify(TOKEN, transport=Api()), broken, NOW + 300)

    # then
    assert out.result.ok and out.result.found == [] and parts(out, 'change') == []


@pytest.mark.parametrize('code', [408, 425, 500, 503])
def test_any_other_answer_past_300_campaigns_fails_the_pass_instead_of_reading_the_list_as_whole(code):
    # given
    rows = [paused(n) for n in range(1, 301)]

    # when
    changes = run_pass(Wingify(TOKEN, transport=Api(rows, beyond=code)), None, NOW)
    verdicts = verdict_pass(Wingify(TOKEN, transport=Api(rows, beyond=code)), None, NOW)

    # then
    assert changes.result.ok is False and changes.result.status.startswith('error: ') and f'HTTP {code}' in changes.result.status
    assert verdicts.result.ok is False and verdicts.result.status.startswith('error: ')


def test_a_rate_limit_on_the_read_past_300_campaigns_keeps_the_tool_answer_and_says_the_rest_is_unknown():
    # when
    out = read(Wingify(TOKEN, transport=Api([paused(n) for n in range(1, 301)], beyond=429)), wanted({}))

    # then
    assert len(out['campaigns']) == 100 and out['byStatus'] == {'PAUSED': 300}
    assert out['more'] == 'unknown: the read past 300 campaigns was refused for now (HTTP 429)'
