import json

import pytest

from skopax import history
from skopax.contract import ReadError
from skopax.kit import elastic
from skopax.plugins.elasticsearch.esql import run_esql
from skopax.remote import HttpError
from skopax.store import Store
from tests.conftest import anomaly, state
from tests.fake_http import FakeHttp
from tests.github_kit import CART_API, content_json, github_http
from tests.test_elasticsearch_plugin import ES_URL, QUERY
from tests.test_esql_narrow import ROWS, THREE, ctx, sent
from skopax.plugintools import PluginTools
from tests.test_github_plugin import client as github_client, github_runtime, kit_store, refused as github_refused
from tests.test_incident_group import PARENT, SOAP, TRACKHUB, seeded
from tests.test_investigator_tools import box
from tests.test_kube import LOG, kube

RESOLVE = f'{ES_URL}/_resolve/index/*'
SHARDS = HttpError(400, QUERY, '{"error": {"type": "search_phase_execution_exception", "reason": "all shards failed"}}')
DAY = 'FROM app-logs-* | WHERE @timestamp >= NOW() - 1 day AND level == "ERROR" | STATS n = COUNT(*)'
HOURS = 'FROM app-logs-* | WHERE @timestamp >= NOW() - 12h | STATS n = COUNT(*)'
HOUR = 'FROM app-logs-* | WHERE @timestamp >= NOW() - 1 hour | STATS n = COUNT(*)'


@pytest.fixture(autouse=True)
def fresh_cache():
    elastic.PATTERNS.clear()
    yield
    elastic.PATTERNS.clear()


def scripted(*answers, resolve=None):
    left = list(answers)
    routes = {('POST', QUERY): lambda params, body: left.pop(0)}
    if resolve is not None:
        routes[('GET', RESOLVE)] = resolve
    return FakeHttp(routes)


@pytest.mark.parametrize('query', [DAY, HOURS])
def test_a_range_error_on_a_day_or_less_retries_once_on_the_last_hour_and_says_so(query: str):
    # given
    http = scripted(SHARDS, ROWS)

    # when
    out = run_esql(ctx(http), {'query': query})

    # then
    assert len(sent(http)) == 2
    assert 'NOW() - 1 hour' in sent(http)[1] and 'NOW() - 1 day' not in sent(http)[1]
    assert 'last hour' in out['note'] and out['rows'] == [{'n': 7}]


def test_a_504_on_a_one_day_range_retries_once_on_the_last_hour():
    # given
    http = scripted(HttpError(504, QUERY, 'gateway timeout'), ROWS)

    # when
    out = run_esql(ctx(http), {'query': DAY})

    # then
    assert sent(http)[1] == DAY.replace('NOW() - 1 day', 'NOW() - 1 hour')
    assert 'last hour' in out['note'] and 'timed out' in out['note']


def test_a_range_error_on_the_last_hour_is_not_retried():
    # given
    http = scripted(SHARDS)

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(ctx(http), {'query': HOUR})

    # then
    assert len(sent(http)) == 1
    assert 'narrow the time range or the index' in raised.value.message


def test_a_short_unit_spelling_over_several_days_is_narrowed_to_the_last_day():
    # given
    http = scripted(SHARDS, ROWS)

    # when
    out = run_esql(ctx(http), {'query': 'FROM app-logs-* | WHERE @timestamp >= NOW() - 3d | STATS n = COUNT(*)'})

    # then
    assert sent(http)[1] == 'FROM app-logs-* | WHERE @timestamp >= NOW() - 1 day | STATS n = COUNT(*)'
    assert 'last day' in out['note']


def test_an_unknown_short_index_name_suggests_existing_patterns_that_contain_it():
    # given
    missing = HttpError(400, QUERY, '{"error": {"type": "verification_exception", "reason": "Found 1 problem\\nline 1:1: Unknown index [checkout*]"}}')
    names = {'indices': [{'name': 'app-checkout-service-prod-logs-2026.11.01'}, {'name': 'metrics-system.cpu-default'}, {'name': 'zzz-unrelated'}], 'aliases': [], 'data_streams': []}
    http = scripted(missing, resolve=names)

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(ctx(http), {'query': 'FROM checkout* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 5'})

    # then
    message = raised.value.message
    assert 'closest existing' in message and 'app-checkout-service-prod-logs-*' in message, message
    assert 'zzz-unrelated' not in message and 'metrics-system' not in message


def test_a_bare_type_prefix_as_group_parent_child_or_absorb_is_refused_with_a_clear_message(store: Store):
    # given
    b = box(seeded(store), keys=(TRACKHUB, SOAP))

    # when
    parent, parent_failed = b.call('group', {'parent': 'log|', 'children': [TRACKHUB]})
    child, child_failed = b.call('group', {'parent': PARENT, 'children': ['log|*']})
    absorb, absorb_failed = b.call('group', {'parent': PARENT, 'children': [TRACKHUB], 'absorb': ['log|']})

    # then
    assert parent_failed and child_failed and absorb_failed
    for out in (parent, child, absorb):
        assert 'only the type prefix of a card key' in out, out
    assert store.anomaly(TRACKHUB).get('parent') is None


def test_an_absorb_pattern_that_matches_no_group_card_says_how_many_patterns_are_used_and_the_cards_outside_it_matches(store: Store):
    # given
    held = ['log|reports-service|ERROR|*held one*', 'log|reports-service|ERROR|*held two*']
    b = box(seeded(store, absorb=held), keys=(TRACKHUB,))
    outside = 'log|reports-service|ERROR|SOAP request*'

    # when
    out, failed = b.call('group', {'parent': PARENT, 'children': [TRACKHUB], 'absorb': [outside]})

    # then
    assert failed and 'matches no card of this group' in out, out
    assert '2 of 5 patterns used' in out and 'slots left: 3' in out, out
    assert 'outside this group it matches' in out and SOAP in out.split('outside this group it matches')[1], out


def test_the_limit_refusal_says_all_patterns_are_used(store: Store):
    # given
    held = [f'log|reports-service|ERROR|*TrackHub {n}*' for n in range(5)]
    b = box(seeded(store, absorb=held), keys=(TRACKHUB,))

    # when
    out, failed = b.call('group', {'parent': PARENT, 'children': [TRACKHUB], 'absorb': ['log|reports-service|ERROR|*TrackHub request*']})

    # then
    assert failed and '5 of 5 patterns used' in out, out


def test_slack_update_in_a_deploy_report_run_names_report_slack_draft(store: Store):
    # given
    card = 'log|checkout-service|ERROR|x'
    store.save_state(state(anomaly(card)), None)
    b = box(store, keys=())
    b.report_id = 'r1'

    # when
    out, failed = b.call('slack_update', {'key': card, 'text': 'Update: fixed.'})

    # then
    assert failed and 'report_slack_draft' in out, out


def test_slack_update_on_a_card_outside_the_run_names_slack_draft_and_the_run_cards(store: Store):
    # given
    mine, other = 'log|checkout-service|ERROR|x', 'log|cart-service|ERROR|y'
    store.save_state(state(anomaly(mine), anomaly(other, svc='cart-service')), None)
    b = box(store, keys=(mine,))

    # when
    out, failed = b.call('slack_update', {'key': other, 'text': 'Update: fixed.'})

    # then
    assert failed and 'slack_draft' in out and mine in out, out


def test_the_card_tool_points_a_closed_card_to_a_live_instance_suffixed_key(store: Store):
    # given
    gone, live = 'alert|checkout-service|HighLatency', 'alert|checkout-service|HighLatency|pod-a'
    store.save_state(state(anomaly(live, num=40)), None)
    store.card_history.insert_one({'_id': history.doc_id(gone, '2026-11-20T08:00:00Z'), 'key': gone, 'num': 39, 'firstSeen': '2026-11-20T08:00:00Z',
                                   'lastSeen': '2026-11-20T09:00:00Z', 'clearedAt': '2026-11-20T09:30:00Z', 'outcome': 'cleared'})
    b = box(store, keys=(live,))

    # when
    out, failed = b.call('card', {'key': gone})

    # then
    assert not failed, out
    found = json.loads(out)
    assert found['closed'] is True and found['liveKeys'] == [live], found


def test_the_card_tool_names_live_close_keys_for_a_key_it_never_saw(store: Store):
    # given
    live = 'alert|checkout-service|HighLatency|pod-a'
    store.save_state(state(anomaly(live)), None)
    b = box(store, keys=(live,))

    # when
    out, failed = b.call('card', {'key': 'alert|checkout-service|HighLatency'})
    nothing, nothing_failed = b.call('card', {'key': 'alert|cart-service|Down'})

    # then
    assert failed and 'open cards with a close key' in out and live in out, out
    assert nothing_failed and 'close key' not in nothing, nothing


def test_the_github_file_tool_lists_the_nearest_existing_directory_on_a_wrong_path():
    # given
    names = [{'name': f'f{i:02d}.py', 'path': f'src/f{i:02d}.py', 'type': 'file'} for i in range(25)] + [{'name': 'app', 'path': 'src/app', 'type': 'dir'}]
    http = github_http({('GET', f'{CART_API}/contents/src/nope/a.py'): HttpError(404, 'x'), ('GET', f'{CART_API}/contents/src/nope'): HttpError(404, 'x'),
                        ('GET', f'{CART_API}/contents/src'): names})

    # when
    error = github_refused(github_client(http), 'github_file', repo='checkout-service', path='src/nope/a.py')

    # then
    assert error.status == 404 and 'not a file' in error.message
    assert 'nearest existing directory is src' in error.message and 'app/' in error.message, error.message
    assert 'f00.py' in error.message and 'f19.py' not in error.message, error.message


def test_the_github_file_tool_on_a_folder_lists_its_entries():
    # given
    http = github_http({('GET', f'{CART_API}/contents/src'): [content_json('src/x.py', 'x'), content_json('src/y.py', 'y')]})

    # when
    error = github_refused(github_client(http), 'github_file', repo='checkout-service', path='src')

    # then
    assert 'x.py' in error.message and 'y.py' in error.message, error.message


def test_a_failing_github_listing_leaves_the_plain_refusal():
    # given
    http = github_http({('GET', f'{CART_API}/contents/nope.txt'): HttpError(404, 'x'), ('GET', f'{CART_API}/contents'): HttpError(500, 'x')})

    # when
    error = github_refused(github_client(http), 'github_file', repo='checkout-service', path='nope.txt')

    # then
    assert 'not a file' in error.message and 'nearest' not in error.message


def test_previous_logs_that_come_back_empty_fall_back_to_the_current_logs_and_say_so(tmp_path):
    # given
    k, api = kube(tmp_path, {LOG: lambda query: b'' if query.get('previous') == ['true'] else b'boot ok'})

    # when
    text = k.previous_logs('store', 'catalog-service-7d9f8b6c5-x2b4k', 'java', 50)

    # then
    assert text.splitlines()[0].startswith('no previous logs: the previous run of container java left no lines'), text
    assert text.endswith('boot ok')
    assert len(api.calls) == 2


def test_previous_logs_the_runtime_no_longer_holds_fall_back_to_the_current_logs(tmp_path):
    # given
    from skopax.kube import HttpFailed
    gone = HttpFailed(400, 'unable to retrieve container logs for containerd://0123abcd')
    k, _ = kube(tmp_path, {LOG: lambda query: gone if query.get('previous') == ['true'] else b'boot ok'})

    # when
    text = k.previous_logs('store', 'catalog-service-7d9f8b6c5-x2b4k', None, 50)

    # then
    assert text.splitlines()[0].startswith('no previous logs: the logs of the previous run of its container could not be read'), text
    assert text.endswith('boot ok')


def test_a_bare_type_prefix_as_parent_still_names_the_parents_this_run_can_use(store: Store):
    # given
    b = box(seeded(store), keys=(TRACKHUB, SOAP))

    # when
    out, failed = b.call('group', {'parent': 'log|', 'children': [TRACKHUB]})

    # then
    assert failed and 'parents this run can use' in out and TRACKHUB in out.split('parents this run can use')[1], out


def test_the_card_tool_never_names_a_cleared_card_as_a_live_key(store: Store):
    # given
    gone, cleared = 'alert|checkout-service|HighLatency', 'alert|checkout-service|HighLatency|pod-b'
    store.save_state(state(anomaly(cleared, status='cleared')), None)
    b = box(store, keys=())

    # when
    out, failed = b.call('card', {'key': gone})

    # then
    assert failed and 'close key' not in out and cleared not in out, out


def test_the_slack_update_hint_names_only_run_cards_with_no_post_yet(store: Store):
    # given
    unposted, posted, other = 'log|checkout-service|ERROR|x', 'log|checkout-service|ERROR|z', 'log|cart-service|ERROR|y'
    post = {'ts': '1.2', 'at': '2026-10-10T08:00:00Z', 'channel': 'C1', 'by': 'auto'}
    store.save_state(state(anomaly(unposted), anomaly(posted, slackPost=post), anomaly(other, svc='cart-service')), None)
    b = box(store, keys=(unposted, posted))

    # when
    out, failed = b.call('slack_update', {'key': other, 'text': 'Update: fixed.'})

    # then
    assert failed and 'slack_draft' in out and unposted in out and posted not in out.split('slack_draft')[1], out


def test_the_github_nearest_directory_hint_reaches_the_model_through_the_run_tool_path(store: Store):
    # given
    names = [{'name': f'module_{i:02d}.py', 'path': f'src/checkout/module_{i:02d}.py', 'type': 'file'} for i in range(20)]
    http = github_http({('GET', f'{CART_API}/contents/src/checkout/nope.py'): HttpError(404, 'x'), ('GET', f'{CART_API}/contents/src/checkout'): names})
    b = box(store, plugin_tools=PluginTools(github_runtime(kit_store(), http)))

    # when
    out, failed = b.call('github_file', {'repo': 'checkout-service', 'path': 'src/checkout/nope.py'})

    # then
    assert failed and 'not a file' in out and 'src/checkout/module_19.py' in out, out
