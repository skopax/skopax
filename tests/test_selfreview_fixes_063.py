import base64
import re

import pytest

from skopax.contract import ReadError
from skopax.investigator import tools as investigator_tools
from skopax.kit import elastic
from skopax.plugins.elasticsearch.esql import run_esql
from skopax.remote import HttpError, Unreachable
from skopax.store import Store
from tests.conftest import anomaly, state
from tests.fake_http import FakeHttp
from tests.github_kit import CART_API, FULL, commit_json, content_json, github_http
from tests.test_elasticsearch_plugin import QUERY
from tests.test_esql_narrow import ROWS, THREE
from tests.test_github_plugin import client, refused
from tests.test_gitlab_file_tool_errors import FILE, PROJECT, TREE, box as gitlab_box, entries, routes
from tests.test_investigator_tools import box
from tests.test_tool_errors_048 import es_ctx, es_scripted, es_sent


@pytest.fixture(autouse=True)
def fresh_cache():
    elastic.PATTERNS.clear()
    yield
    elastic.PATTERNS.clear()


def unknown(name: str) -> HttpError:
    return HttpError(400, QUERY, '{"error": {"type": "verification_exception", "reason": "Found 1 problem\\nline 1:40: Unknown column [%s]"}}' % name)


def test_an_esql_unknown_column_lists_the_valid_columns_with_the_asked_prefix_first_and_at_most_40():
    # given
    names = ['@timestamp', 'message'] + [f'service.field{i:02d}' for i in range(30)] + [f'ingress.attr{i:02d}' for i in range(30)]
    http = es_scripted(unknown('ingress.zzqx'), {'columns': [{'name': n} for n in names], 'values': []})

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(es_ctx(http), {'query': 'FROM app-logs-* | WHERE @timestamp >= NOW() - 1 hour | KEEP ingress.zzqx'})

    # then
    message = raised.value.message
    listed = message.split('valid columns of app-logs-*: ')[1].split(' - ')[0].split(', ')
    assert len(listed) == 40, listed
    assert listed[:30] == [f'ingress.attr{i:02d}' for i in range(30)], listed
    assert '@timestamp' in listed


def test_an_esql_unknown_column_still_names_the_closest_columns_before_the_valid_list():
    # given
    http = es_scripted(unknown('servce.name'), {'columns': [{'name': n} for n in ('@timestamp', 'service.name', 'message')], 'values': []})

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(es_ctx(http), {'query': 'FROM app-logs-* | WHERE @timestamp >= NOW() - 1 hour | KEEP servce.name'})

    # then
    message = raised.value.message
    assert message.index('closest columns of app-logs-*: service.name') < message.index('valid columns of app-logs-*')


def test_an_esql_that_cannot_be_reached_over_several_days_retries_once_on_the_last_day_and_says_so():
    # given
    http = es_scripted(Unreachable(f'{QUERY} unreachable (ConnectError)'), ROWS)

    # when
    out = run_esql(es_ctx(http), {'query': THREE})

    # then
    assert len(es_sent(http)) == 2 and 'NOW() - 1 day' in es_sent(http)[1]
    assert 'could not be reached' in out['note'] and 'last day' in out['note'] and out['rows'] == [{'n': 7}]


def test_an_esql_that_stays_unreachable_fails_and_says_the_retry_failed_too():
    # given
    down = Unreachable(f'{QUERY} unreachable (ConnectError)')
    http = es_scripted(down, down)

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(es_ctx(http), {'query': THREE})

    # then
    assert 'the retry on the last day failed too' in raised.value.message, raised.value.message
    assert len(es_sent(http)) == 2


def test_an_esql_that_cannot_be_reached_on_a_one_hour_range_is_not_retried():
    # given
    http = es_scripted(Unreachable(f'{QUERY} unreachable (ConnectError)'))

    # when
    with pytest.raises(ReadError):
        run_esql(es_ctx(http), {'query': 'FROM app-logs-* | WHERE @timestamp >= NOW() - 1 hour | STATS n = COUNT(*)'})

    # then
    assert len(es_sent(http)) == 1


def test_the_triage_description_says_high_and_critical_take_progress_and_handled_is_for_a_warning(store: Store):
    # given
    store.save_state(state(anomaly('log|a|ERROR|x')), None)

    # when
    [described] = [d for d in box(store).definitions() if d['name'] == 'triage']

    # then
    assert 'High and Critical cards take progress with a Jira ticket' in described['description']
    assert 'handled is only for a Warning card' in described['description']


@pytest.mark.parametrize('sev', ['crit', 'high'])
def test_a_handled_refusal_on_a_high_or_critical_card_names_progress_with_a_ticket_and_the_warning_rule(store: Store, sev: str):
    # given
    store.save_state(state(anomaly('log|a|ERROR|x', sev=sev)), None)

    # when
    text, failed = box(store).call('triage', {'key': 'log|a|ERROR|x', 'state': 'handled'})

    # then
    assert failed
    assert 'High and Critical cards take triage progress with a Jira ticket' in text and 'handled is only for a Warning card' in text, text


def test_the_tag_description_states_the_wording_rules_that_make_it_refuse(store: Store):
    # given
    description = investigator_tools.tag_tool()['description']

    # then
    assert 'say or imply that someone is following, looking into or fixing it' in description
    assert 'today, tonight or yesterday' in description and description.count('looking into or fixing it') == 1
    assert 'being worked on' not in description


def test_a_tag_text_that_says_someone_is_working_on_it_is_refused_with_that_rule(store: Store, monkeypatch):
    # given
    store.save_state(state(anomaly('log|a|ERROR|x')), None)
    monkeypatch.setattr('skopax.tags.all_tags', lambda: {'marketing': 'Marketing'})
    b = box(store)

    # when
    text, failed = b.call('tag', {'key': 'log|a|ERROR|x', 'tags': ['marketing'], 'text': 'We recommend a fix, the team is working on it', 'why': 'visitors see nothing'})

    # then
    assert failed and 'never say or imply that someone is following' in text


def test_a_gitlab_ref_that_does_not_exist_lists_up_to_ten_nearest_tags(store):
    # given
    tags = [{'name': f'v1.{i}.0'} for i in range(14)] + [{'name': 'nightly'}]
    http = FakeHttp(routes({('GET', f'{FILE}/pom.xml'): HttpError(404, 'x'), ('GET', f'{PROJECT}/repository/commits/v9.9.9'): HttpError(404, 'x'), ('GET', f'{PROJECT}/repository/tags'): tags}))

    # when
    text, err = gitlab_box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'pom.xml', 'ref': 'v9.9.9'})

    # then
    assert err and 'v9.9.9 is not a branch, tag or commit' in text, text
    listed = text.split('nearest existing tags')[1].split(': ', 1)[1].split(', ')
    assert len(listed) == 10 and all(re.fullmatch(r'v1\.\d+\.0', t.strip()) for t in listed), listed


def test_a_gitlab_compare_with_a_missing_tag_lists_the_nearest_tags_and_names_the_missing_one(store):
    # given
    compare = f'{PROJECT}/repository/compare'
    http = FakeHttp(routes({('GET', compare): HttpError(404, 'x'), ('GET', f'{PROJECT}/repository/commits/v2.0'): {'id': 'a' * 40},
                            ('GET', f'{PROJECT}/repository/commits/v3.0'): HttpError(404, 'x'), ('GET', f'{PROJECT}/repository/tags'): [{'name': 'v3.0.1'}, {'name': 'v2.0'}, {'name': 'zzz'}]}))

    # when
    text, err = gitlab_box(store, http).call('gitlab_compare', {'repo': 'checkout-service', 'from': 'v2.0', 'to': 'v3.0'})

    # then
    assert err and 'nearest existing tags' in text and 'v3.0.1' in text, text


def test_a_gitlab_missing_file_on_an_existing_ref_keeps_the_path_hint_and_does_not_list_tags(store):
    # given
    http = FakeHttp(routes({('GET', f'{FILE}/nope.txt'): HttpError(404, 'x'), ('GET', f'{PROJECT}/repository/commits/v1.0'): {'id': 'b' * 40},
                            ('GET', TREE): lambda q, _b: entries(['README.md'])}))

    # when
    text, err = gitlab_box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'nope.txt', 'ref': 'v1.0'})

    # then
    assert err and 'README.md' in text and 'nearest existing tags' not in text


def test_a_missing_gitlab_path_lists_at_most_ten_nearest_real_paths_closest_first(store):
    # given
    names = [f'values-{c}.yaml' for c in 'abcdefghijklmno'] + ['deployment.yaml']
    http = FakeHttp(routes({('GET', f'{FILE}/deploy%2Fdeployments.yaml'): HttpError(404, 'x'),
                            ('GET', TREE): lambda q, _b: entries([f'deploy/{n}' for n in names]) if q.get('path') == ['deploy'] else HttpError(404, 'x')}))

    # when
    text, err = gitlab_box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'deploy/deployments.yaml'})

    # then
    listed = text.split('with: ')[1].split(', ')
    assert err and len(listed) == 10 and listed[0] == 'deploy/deployment.yaml', listed


def test_a_gitlab_line_range_past_the_end_of_the_file_names_the_file_length_and_the_accepted_format(store):
    # given
    body = {'content': base64.b64encode(b'one\ntwo\nthree\n').decode()}
    http = FakeHttp(routes({('GET', f'{FILE}/a.txt'): body}))

    # when
    text, err = gitlab_box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'a.txt', 'lines': '120-160'})
    zero, zero_err = gitlab_box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'a.txt', 'lines': '0-5'})

    # then
    assert err and 'the file has 3 lines' in text and '"120-160"' in text, text
    assert zero_err and '"120-160"' in zero, zero


def test_a_github_ref_that_does_not_exist_lists_up_to_ten_nearest_tags():
    # given
    tags = [{'name': f'v1.{i}.0'} for i in range(14)] + [{'name': 'nightly'}]
    http = github_http({('GET', f'{CART_API}/contents/pom.xml'): HttpError(404, 'x'), ('GET', f'{CART_API}/commits/v9.9.9'): HttpError(404, 'x'), ('GET', f'{CART_API}/tags'): tags})
    gh = client(http)

    # when
    error = refused(gh, 'github_file', repo='checkout-service', path='pom.xml', ref='v9.9.9')

    # then
    assert error.status == 404 and 'v9.9.9 is not a branch, tag or commit' in error.message, error.message
    listed = error.message.split('nearest existing tags')[1].split(': ', 1)[1].split(', ')
    assert len(listed) == 10 and all(re.fullmatch(r'v1\.\d+\.0', t.strip()) for t in listed), listed


def test_a_github_compare_with_a_missing_tag_lists_the_nearest_tags():
    # given
    http = github_http({('GET', f'{CART_API}/compare/v2.0...v3.0'): HttpError(404, 'x'), ('GET', f'{CART_API}/commits/v2.0'): commit_json(FULL, 'x', 'Oscar'),
                        ('GET', f'{CART_API}/commits/v3.0'): HttpError(404, 'x'), ('GET', f'{CART_API}/tags'): [{'name': 'v3.0.1'}, {'name': 'v2.0'}]})

    # when
    error = refused(client(http), 'github_compare', repo='checkout-service', **{'from': 'v2.0', 'to': 'v3.0'})

    # then
    assert error.status == 404 and 'nearest existing tags' in error.message and 'v3.0.1' in error.message, error.message


def test_a_github_line_range_past_the_end_of_the_file_names_the_file_length_and_the_accepted_format():
    # given
    http = github_http({('GET', f'{CART_API}/contents/a.txt'): content_json('a.txt', 'one\ntwo\nthree\n')})

    # when
    past = refused(client(http), 'github_file', repo='checkout-service', path='a.txt', lines='120-160')

    # then
    assert 'the file has 3 lines' in past.message and '"120-160"' in past.message, past.message


def test_a_missing_github_path_lists_at_most_ten_nearest_real_paths_closest_first():
    # given
    names = [f'deploy/values-{c}.yaml' for c in 'abcdefghijklmno'] + ['deploy/deployment.yaml']
    http = github_http({('GET', f'{CART_API}/contents/deploy/deployments.yaml'): HttpError(404, 'x'),
                        ('GET', f'{CART_API}/contents/deploy'): [content_json(n, 'x') for n in names]})

    # when
    error = refused(client(http), 'github_file', repo='checkout-service', path='deploy/deployments.yaml')

    # then
    listed = error.message.split('with: ')[1].split(', ')
    assert len(listed) == 10 and listed[0] == 'deploy/deployment.yaml', listed


def promql_rig(store: Store):
    from skopax.plugins import prometheus
    from tests.test_plugin_tools import Rig
    from tests.test_tool_errors_048 import NODES
    rig = Rig(store, prometheus.PLUGIN, NODES)
    rig.add(prometheus.PLUGIN, 'prod', primary=True, url='https://prom.example.com')
    rig.add(prometheus.PLUGIN, 'data-eu', url='https://prom-eu.example.com')
    rig.add(prometheus.PLUGIN, 'data-us', url='https://prom-us.example.com')
    rig.add(NODES, 'data', primary=True)
    return rig


def test_a_promql_cluster_that_is_another_tool_s_instance_with_several_close_names_lists_them_all(store: Store):
    # given
    b = promql_rig(store).box()

    # when
    text, failed = b.call('promql', {'expr': 'up', 'cluster': 'data'})

    # then
    assert failed and text.startswith('cluster must be one of prod, data-eu, data-us'), text
    assert '"data" is an instance of nodes_read, not of promql' in text and 'did you mean data-eu or data-us?' in text, text


def test_a_promql_cluster_that_is_a_typo_names_the_closest_configured_cluster(store: Store):
    # given
    b = promql_rig(store).box()

    # when
    text, failed = b.call('promql', {'expr': 'up', 'cluster': 'prd'})

    # then
    assert failed and 'cluster must be one of prod, data-eu, data-us' in text and 'closest configured: prod' in text, text
    assert 'is not configured' in text


def test_an_esql_answer_without_rows_is_not_retried_or_called_unreachable():
    # given
    http = es_scripted(['not an object'])

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(es_ctx(http), {'query': THREE})

    # then
    assert 'answered without rows' in raised.value.message and 'unreachable' not in raised.value.message
    assert len(es_sent(http)) == 1
