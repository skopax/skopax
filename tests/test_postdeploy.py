import datetime as dt
from urllib.parse import parse_qs, urlsplit

from skopax import clock
from skopax.enrich import SourceDown
from skopax.postdeploy import LOOKBACK_HOURS, PostDeploy, queries, slack_text
from skopax.kit.promql import Prometheus
from tests.plugin_kit import prom_runtime
from skopax.remote import Reply, Unreachable
from skopax.store import Store, iso

NOW = 1_792_224_000.0
NS = 'shop'
OLD_RELEASE = '{project} {dd}.{mm}.{yyyy} Release'


def at(seconds_ago: float) -> str:
    return iso(dt.datetime.fromtimestamp(NOW - seconds_ago, dt.timezone.utc))


def tr(seconds_ago: float, fmt: str = '%H:%M') -> str:
    return clock.local(NOW - seconds_ago).strftime(fmt)


def prom(values: dict[tuple[str, str, int], float], fail: bool = False) -> Prometheus:
    def transport(method, url, headers, data, timeout) -> Reply:
        if fail:
            raise Unreachable('prometheus down')
        q = parse_qs(urlsplit(url).query)
        expr, t = q['query'][0], float(q['time'][0])
        offset = round((NOW - t) / 86400)
        value = 0.0
        for (svc, name, days), v in values.items():
            if days == offset and queries(NS, svc, expr.split('[')[-1].split(']')[0] if '[' in expr else '0s').get(name) == expr:
                value = v
        body = {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': {}, 'value': [t, str(value)]}]}}
        return Reply(body=body, headers={})
    return Prometheus('https://prometheus.prod.example.com', transport, sleep=lambda s: None)


def only(client):
    return lambda cluster: client


class FakeJira:
    def __init__(self, issues: list[dict], enabled: bool = True, down: bool = False):
        self.issues, self.enabled, self.down, self.jql = issues, enabled, down, []

    def search(self, jql: str, fields: list[str], limit: int = 20) -> list[dict]:
        self.jql.append(jql)
        if self.down:
            raise SourceDown('Jira HTTP 503')
        return self.issues


def issue(key: str, summary: str, status: str, versions: list[str]) -> dict:
    return {'key': key, 'fields': {'summary': summary, 'status': {'name': status}, 'fixVersions': [{'name': v} for v in versions]}}


def settled_action(store: Store, first_ago: float, last_ago: float) -> dict:
    return store.add_action({'type': 'postdeploy', 'key': 'postdeploy', 'target': 'postdeploy', 'label': 'Post-deploy check', 'requestedBy': 'auto · release settled',
                             'release': {'firstAt': at(first_ago), 'lastAt': at(last_ago), 'deploys': 2, 'services': ['checkout-service', 'orders-service']}})


def test_a_settled_release_becomes_a_report_with_flags_released_issues_and_an_armed_slack_draft(store: Store):
    # given
    store.add_deploys([{'ns': NS, 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': at(3600), 'tickets': ['OPS-121']},
                       {'ns': NS, 'svc': 'orders-service', 'fromImage': 'cccccccc', 'toImage': 'dddddddd', 'at': at(1800)},
                       {'ns': NS, 'svc': 'old-service', 'fromImage': 'eeeeeeee', 'toImage': 'ffffffff', 'at': at(5 * 3600)}])
    act = settled_action(store, 3600, 1800)
    release = f"OPS {tr(1800, '%d.%m.%Y')} Release"
    jira = FakeJira([issue('OPS-121', 'Cart total fix', 'Live', [release]), issue('OPS-122', 'Other work', 'Live', [release])])
    values = {('checkout-service', 'err', 0): 40, ('checkout-service', 'err', 1): 5, ('checkout-service', 'err', 7): 4, ('checkout-service', 'req', 0): 900, ('checkout-service', 'req', 1): 850, ('checkout-service', 'req', 7): 800,
              ('orders-service', 'req', 0): 100, ('orders-service', 'req', 1): 120, ('orders-service', 'req', 7): 90, ('orders-service', 'restarts', 0): 1}
    store.row_links = prom_runtime(store, {'main': {'ui-url': 'https://prometheus.example.com'}}).row_links
    pd = PostDeploy(store, only(prom(values)), jira, base_url='https://skopax.example.com', clock=lambda: NOW, projects=('OPS', 'WEB'), release_template=lambda: OLD_RELEASE)

    # when
    saved = pd.run_once(NOW)
    report = store.deploy_report(saved['id'])
    done = store.list_actions(None, 5)[0]
    lines = [r['text'] for r in store.list_activity(None, 5)]

    # then
    assert report['summary'] == f'2 deploys since {tr(1800)} UTC: 0 clean, 2 flagged', 'the old deploy outside the release is left out; a restart counts as a flag like on the laptop'
    assert [(r['svc'], r['flags'], r['metrics'], r['issues']) for r in report['rows']] == [('checkout-service', ['err up'], 'err 40 (5/4), req 900 (850/800)', ['OPS-121']), ('orders-service', ['restarts=1'], 'req 100 (120/90)', [])]
    assert report['rows'][0]['fromImage'] == 'aaaaaaaa' and report['rows'][0]['toImage'] == 'bbbbbbbb' and 'links' in report['rows'][0]
    assert (report['windowFrom'], report['windowTo']) == (at(1800), at(0))
    assert report['brief']['level'] == 'warn' and report['brief']['points'] == ['checkout-service: err up', 'orders-service: restarts=1']
    assert report['releases'] == [release, release.replace('OPS', 'WEB')]
    assert report['issues'] == [{'key': 'OPS-121', 'summary': 'Cart total fix', 'status': 'Live', 'inRelease': True}, {'key': 'OPS-122', 'summary': 'Other work', 'status': 'Live', 'inRelease': True}]
    assert jira.jql == [f'fixVersion in ("{release}", "{release.replace("OPS", "WEB")}") OR key in (OPS-121) ORDER BY key']
    assert report['text'] == '[WARN] shop/checkout-service  err 40 (5/4), req 900 (850/800)  << err up\n[WARN] shop/orders-service  req 100 (120/90)  << restarts=1'
    assert report['slackDraft']['text'].startswith(f"17 Oct deploy check: 2 services ({tr(3600)}–{tr(1800)} UTC), 0 clean, 2 flagged: checkout-service (err up); orders-service (restarts=1)") and report['autoSend']
    assert (done['id'], done['status'], done['result'], done['reportId']) == (act['id'], 'done', report['summary'], report['id'])
    assert report['actionId'] == act['id'] and report['requestedBy'] == 'auto · release settled'
    assert (report['usd'], report['costNote']) == (0.0, 'Prometheus and Jira queries only, no model'), 'the report says what it cost: nothing'
    assert lines == [f"post-deploy check · {report['summary']} · Slack post queued"]


def test_a_click_without_a_release_checks_the_last_six_hours_and_an_empty_window_just_closes_the_action(store: Store):
    # given
    store.add_deploys([{'ns': NS, 'svc': 'old-service', 'fromImage': 'eeeeeeee', 'toImage': 'ffffffff', 'at': at((LOOKBACK_HOURS + 1) * 3600)}])
    store.add_action({'type': 'postdeploy', 'key': 'postdeploy', 'target': 'postdeploy', 'label': 'Post-deploy check', 'requestedBy': 'oscar.green'})
    pd = PostDeploy(store, only(prom({})), FakeJira([], enabled=False), clock=lambda: NOW)

    # when
    nothing = pd.run_once(NOW)
    store.add_deploys([{'ns': NS, 'svc': 'new-service', 'fromImage': '11111111', 'toImage': '22222222', 'at': at(600)}])
    store.add_action({'type': 'postdeploy', 'key': 'postdeploy', 'target': 'postdeploy', 'label': 'Post-deploy check', 'requestedBy': 'oscar.green'})
    saved = pd.run_once(NOW)
    actions = store.list_actions(None, 5)

    # then
    assert nothing is None and actions[1]['status'] == 'done' and actions[1]['result'] == f'no image change in the last {LOOKBACK_HOURS} h'
    assert saved['summary'] == f'1 deploys since {tr(600)} UTC: 1 clean' and saved['rows'][0]['svc'] == 'new-service' and saved['issues'] == []
    assert actions[0]['status'] == 'done' and actions[0]['reportId'] == saved['id']


def test_a_prometheus_failure_fails_the_action_and_the_job_stays_off_without_prometheus(store: Store):
    # given
    store.add_deploys([{'ns': NS, 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': at(1800)}])
    act = settled_action(store, 1800, 1800)
    jira = FakeJira([], down=True)

    # when
    out = PostDeploy(store, only(prom({}, fail=True)), jira, clock=lambda: NOW).run_once(NOW)
    failed = store.list_actions(None, 5)[0]

    # then
    assert out is None and failed['id'] == act['id'] and failed['status'] == 'failed' and failed['result'] == 'PrometheusError'
    noted = store.list_activity(None, 5)[0]
    assert (noted['text'], noted['error']) == ('post-deploy check failed \u00b7 PrometheusError', 'PrometheusError')
    assert PostDeploy(store, only(None), jira).enabled is False
    assert PostDeploy(store, only(prom({})), jira, flag=False).enabled is False
    assert PostDeploy(store, only(prom({})), jira).released([{'issues': ['OPS-5']}], ['OPS 22.10.2026 Release']) == ([{'key': 'OPS-5'}], None), 'a Jira outage leaves the keys without details'


def test_a_release_action_follows_the_whole_wave_past_its_recorded_last_deploy(store: Store):
    # given
    store.add_deploys([{'ns': NS, 'svc': 'a-service', 'fromImage': '1', 'toImage': '2', 'at': at(5400)},
                       {'ns': NS, 'svc': 'b-service', 'fromImage': '3', 'toImage': '4', 'at': at(4200)},
                       {'ns': NS, 'svc': 'c-service', 'fromImage': '5', 'toImage': '6', 'at': at(2400)},
                       {'ns': NS, 'svc': 'much-later', 'fromImage': '7', 'toImage': '8', 'at': at(-3 * 3600)}])
    act = settled_action(store, 5400, 5400)

    # when
    rows = PostDeploy(store, only(prom({})), FakeJira([], enabled=False), clock=lambda: NOW).targets(act, NOW)

    # then
    assert [r['svc'] for r in rows] == ['a-service', 'b-service', 'c-service'], 'deploys detected after the action was queued belong to the same wave; a deploy hours later does not'


def test_ignored_services_are_left_out_of_the_checked_rows_of_a_release_and_of_a_click(store: Store):
    # given
    store.add_deploys([{'ns': 'platform', 'svc': 'help-center', 'fromImage': '1', 'toImage': '2', 'at': at(5400)},
                       {'ns': NS, 'svc': 'b-service', 'fromImage': '3', 'toImage': '4', 'at': at(4200)},
                       {'ns': 'platform', 'svc': 'status-board', 'fromImage': '5', 'toImage': '6', 'at': at(2400)}])
    store.set_postdeploy_ignore({'services': ['help-center', 'status-board']})
    check = PostDeploy(store, only(prom({})), FakeJira([], enabled=False), clock=lambda: NOW)

    # when
    release = check.targets(settled_action(store, 5400, 5400), NOW)
    click = check.targets({'type': 'postdeploy'}, NOW)

    # then
    assert [r['svc'] for r in release] == ['b-service']
    assert [r['svc'] for r in click] == ['b-service']


def test_with_the_investigator_available_the_numbers_draft_waits_and_an_assessment_is_queued(store: Store):
    # given
    store.add_deploys([{'ns': NS, 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': at(1800)}])
    settled_action(store, 1800, 1800)
    pd = PostDeploy(store, only(prom({('checkout-service', 'err', 0): 40, ('checkout-service', 'err', 1): 5, ('checkout-service', 'err', 7): 4})), FakeJira([], enabled=False), clock=lambda: NOW, assess=lambda: True)

    # when
    saved = pd.run_once(NOW)
    report = store.deploy_report(saved['id'])
    actions = store.list_actions(None, 5)

    # then
    assert report['slackDraft']['by'] == 'server · numbers' and 'autoSend' not in report, 'the numbers draft is saved but not armed'
    assert [(a['type'], a['status']) for a in actions] == [('assess', 'requested'), ('postdeploy', 'done')]
    assert actions[0]['reportId'] == saved['id'] and actions[0]['label'].startswith('Assess deploy report: 1 deploys since')
    assert store.list_activity(None, 5)[0]['text'].endswith('assessment queued for the investigator, its post waits')


def test_with_slack_auto_send_off_the_report_queues_no_slack_text(store: Store):
    # given
    store.add_deploys([{'ns': NS, 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': at(3600)}])
    settled_action(store, 3600, 1800)
    values = {('checkout-service', 'req', 0): 900, ('checkout-service', 'req', 1): 850, ('checkout-service', 'req', 7): 800}
    pd = PostDeploy(store, only(prom(values)), FakeJira([]), base_url='https://skopax.example.com', clock=lambda: NOW, autopost=lambda: False)

    # when
    saved = pd.run_once(NOW)
    report = store.deploy_report(saved['id'])

    # then
    assert 'slackDraft' not in report and 'autoSend' not in report and 'slackSkipped' not in report
    assert store.list_activity(None, 5)[0]['text'].endswith('Slack auto-send is off, not posted')


def test_a_deploy_check_of_one_service_says_one_service():
    # given
    one = [{'svc': 'checkout-service', 'flags': []}]
    two = one + [{'svc': 'cart-service', 'flags': []}]

    # when
    said = slack_text(one, NOW, NOW), slack_text(two, NOW, NOW)

    # then
    assert ': 1 service (' in said[0] and '1 services' not in said[0]
    assert ': 2 services (' in said[1]
