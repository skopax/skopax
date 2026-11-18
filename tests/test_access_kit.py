from skopax.kit import access
from skopax.kit.promql import Prometheus, PrometheusError, credential
from skopax.messages import msg
from skopax.remote import HttpError


def test_the_access_card_names_the_plugin_the_part_and_the_permission():
    # when
    named = access.card('grafana', 'Grafana', 'Prometheus', 'datasources:query', status=403)
    plain = access.card('kafka', 'Kafka', 'groups', status=401)

    # then
    assert named['label'] == 'Grafana cannot read Prometheus: the token lacks datasources:query'
    assert (named['key'], named['ranKey'], named['src'], named['sig'], named['sev'], named['ns'], named['svc'], named['sub']) == (
        'no-access|-/grafana|Prometheus', 'grafana:access:Prometheus', 'grafana', 'no-access', 'warn', '-', 'grafana', 'Prometheus')
    assert named['short'] == 'WARN Grafana cannot read Prometheus: the token lacks datasources:query'
    assert named['what'] == msg('connection.no_access_why', part='Prometheus', status=403, permission='datasources:query', credential='token')
    assert plain['label'] == 'Kafka cannot read groups: the token is invalid or lacks the needed permission' and '(HTTP 401)' in plain['what']
    assert access.text('Kafka', 'groups', status=401) == plain['label'] and access.text('Kafka', 'groups') == 'Kafka cannot read groups: the token lacks the needed permission'
    assert access.text('Prometheus', 'metrics', credential='password') == 'Prometheus cannot read metrics: the password lacks the needed permission'
    assert access.card('prom', 'Prometheus', 'metrics', status=401, credential='password')['label'] == 'Prometheus cannot read metrics: the password is invalid or lacks the needed permission'
    assert 'the password' in access.card('prom', 'Prometheus', 'metrics', credential='password')['what']
    assert [credential(c) for c in ({'auth': 'basic', 'username': 'u'}, {'auth': 'bearer', 'token': 't'}, {'auth': 'none'}, {}, {'auth': 'bearer'})] == ['password', 'token', '', '', '']


def test_the_status_names_the_refused_parts_after_any_other_form():
    # then
    assert access.status('ok', ['groups', 'app', 'groups']) == 'ok \u00b7 no access to groups, app'
    assert access.status('ok \u00b7 no data for clusters', ['groups']) == 'ok \u00b7 no data for clusters \u00b7 no access to groups'
    assert access.status('ok', []) == 'ok'
    assert access.ran_key('nodes', 'nodes of local') == 'nodes:access:nodes of local'
    assert [access.denied(c) for c in (401, 403, 404, 500, None, True, '403')] == [True, True, False, False, False, False, False]


def test_a_prometheus_error_keeps_the_status_it_was_answered_with():
    # given
    def refusing(method, url, headers, data, timeout):
        raise HttpError(403, url)

    def failing(method, url, headers, data, timeout):
        raise HttpError(502, url)

    # when
    caught = []
    for transport in (refusing, failing):
        try:
            Prometheus('https://prometheus.example.com', transport, sleep=lambda s: None).call('query', query='up')
        except PrometheusError as e:
            caught.append((e.code, e.reason))

    # then
    assert caught == [(403, 'Prometheus query HTTP 403'), (502, 'Prometheus query HTTP 502')]
    assert PrometheusError('Prometheus query unreachable').code is None


def test_an_access_card_shows_the_part_by_its_name_and_keeps_the_part_id_in_its_keys():
    # when
    found = access.card('capacity', 'Capacity', 'hpa', status=403, name='autoscalers')

    # then
    assert found['label'] == 'Capacity cannot read autoscalers: the token lacks the needed permission'
    assert (found['key'], found['ranKey'], found['sub'], found['sig']) == ('no-access|-/capacity|hpa', 'capacity:access:hpa', 'hpa', 'no-access')
    assert 'hpa' not in found['what'] and 'autoscalers' in found['what']
    assert access.status('ok', ['autoscalers', 'JVM heap']) == 'ok \u00b7 no access to autoscalers, JVM heap'


def test_a_plural_credential_is_named_with_a_plural_verb():
    # then
    assert access.text('Langfuse', 'metrics', 'read access to the project', 'keys', plural=True) == 'Langfuse cannot read metrics: the keys lack read access to the project'
    assert access.text('Langfuse', 'metrics', 'read access to the project', 'keys', 401, plural=True) == 'Langfuse cannot read metrics: the keys are invalid or lack read access to the project'
    assert 'give them read access to the project' in access.card('langfuse', 'Langfuse', 'metrics', 'read access to the project', 403, 'keys', plural=True)['what']


def test_a_refusal_with_no_credential_set_says_the_server_requires_credentials():
    # when
    found = access.card('prom', 'Prometheus', 'metrics', status=403, credential='')

    # then
    assert found['label'] == 'Prometheus cannot read metrics: the server requires credentials and none are set'
    assert access.text('Prometheus', 'metrics', status=401, credential='') == found['label']
    assert 'token' not in found['what'] and 'none are set' in found['what']


def test_a_plugin_may_explain_its_access_card_in_its_own_words():
    # when
    found = access.card('cf', 'Cloudflare', 'health-checks', 'Health Checks Read', 401, name='health checks', what='Cloudflare refused it.')

    # then
    assert found['what'] == 'Cloudflare refused it.'
    assert found['label'] == 'Cloudflare cannot read health checks: the token is invalid or lacks Health Checks Read'
    assert (found['key'], found['ranKey']) == ('no-access|-/cf|health-checks', 'cf:access:health-checks')


def test_a_refused_connection_test_names_what_was_sent():
    # then
    assert access.tested('Prometheus', 'metrics', 401, credential='token') is None
    assert access.tested('Prometheus', 'metrics', 403, credential='token') == 'Prometheus cannot read metrics: the token lacks the needed permission'
    assert access.tested('Prometheus', 'metrics', 401, credential='') == 'The service requires credentials and none are set (HTTP 401)'
    assert access.tested('Prometheus', 'metrics', 403, credential='') == 'Prometheus cannot read metrics: the server requires credentials and none are set'
    assert access.tested('Prometheus', 'metrics', 500, credential='') is None
