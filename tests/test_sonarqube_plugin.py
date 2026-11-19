import base64
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest

from skopax.contract import InstanceInfo
from skopax.plugins import sonarqube
from skopax.plugins.sonarqube import engine
from skopax.registry import BUILTIN, Registry, describe
from skopax import remote
from skopax.remote import HttpError, Reply, Unreachable

URL = 'https://sonar.example.com'
TOKEN = 'sqa_s3cr3t-token'
NOW = 1_792_224_000.0
DAY = 86400.0
SEARCH = f'{URL}/api/components/search'
STATUS = f'{URL}/api/qualitygates/project_status'
VALID = f'{URL}/api/authentication/validate'
PROJECTS = [{'key': 'shop:web', 'name': 'Shop Web'}, {'key': 'shop:api', 'name': 'Shop API'}, {'key': 'ops:tools', 'name': 'Ops Tools'}]


def status_of(key: str, state) -> dict:
    if state == 'ERROR':
        return {'projectStatus': {'status': 'ERROR', 'conditions': [
            {'status': 'OK', 'metricKey': 'coverage', 'comparator': 'LT', 'errorThreshold': '80', 'actualValue': '90'},
            {'status': 'ERROR', 'metricKey': 'new_coverage', 'comparator': 'LT', 'errorThreshold': '80', 'actualValue': '61.2'},
            {'status': 'ERROR', 'metricKey': 'new_security_rating', 'comparator': 'GT', 'errorThreshold': '1', 'actualValue': '3'}]}}
    return {'projectStatus': {'status': state or 'NONE', 'conditions': []}}


class Sonar:
    def __init__(self, states: dict, projects=None):
        self.states = dict(states)
        self.projects = list(PROJECTS if projects is None else projects)
        self.calls: list[tuple[str, dict, dict, float]] = []
        self.fail: dict = {}
        self.fail_for: dict = {}
        self.blank = False

    def __call__(self, method, url, headers, data, timeout):
        base = url.split('?')[0]
        query = parse_qs(urlsplit(url).query)
        self.calls.append((base, query, dict(headers), timeout))
        if base in self.fail:
            raise self.fail[base]
        if base == STATUS and query['projectKey'][0] in self.fail_for:
            raise self.fail_for[query['projectKey'][0]]
        if base == VALID:
            return Reply({'valid': True}, {})
        if base == SEARCH:
            page, size = int(query.get('p', ['1'])[0]), int(query.get('ps', ['100'])[0])
            rows = self.projects[(page - 1) * size:page * size]
            return Reply({'paging': {'pageIndex': page, 'pageSize': size, 'total': len(self.projects)}, 'components': rows}, {})
        if base == STATUS:
            return Reply({} if self.blank else status_of(query['projectKey'][0], self.states.get(query['projectKey'][0])), {})
        raise HttpError(404, base)

    def hits(self, base: str) -> list:
        return [c for c in self.calls if c[0] == base]


@dataclass
class Ctx:
    http_: object
    config: dict = field(default_factory=lambda: {'url': URL, 'token': TOKEN})
    memos: dict = field(default_factory=dict)
    now: float = NOW
    cards: list = field(default_factory=list)

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo('main', True, None, self.config)

    def http(self):
        return self.http_

    def memo(self, key: str):
        return self.memos.get(key)

    def open_cards(self, plugin_wide: bool = False) -> list:
        return self.cards

    def rules(self, set_id: str) -> dict:
        return {}


def run(ctx: Ctx):
    sweep = sonarqube.run(ctx)
    ctx.memos.update(sweep.memos)
    return sweep


def keys(sweep) -> list[str]:
    return sorted(c['key'] for c in sweep.result.found)


def test_the_plugin_is_built_in_loads_and_is_a_daily_check_without_a_panel():
    # when
    found = Registry.load({}).get('sonarqube')
    check = sonarqube.PLUGIN.checks[0]

    # then
    assert 'skopax.plugins.sonarqube' in BUILTIN and found is not None and found.state == 'ok' and found.plugin is sonarqube.PLUGIN
    assert (check.period, check.daily, sonarqube.PLUGIN.panels) == (86400, True, ())
    assert [(f.key, f.kind, f.required) for f in sonarqube.PLUGIN.fields][:3] == [('url', 'url', True), ('token', 'secret', True), ('projects', 'text', False)]
    assert describe(sonarqube.PLUGIN, icon=True)['id'] == 'sonarqube' and sonarqube.PLUGIN.knowledge[0].text


def test_the_first_run_records_the_current_state_silently_and_opens_no_card():
    # given
    ctx = Ctx(Sonar({'shop:web': 'ERROR', 'shop:api': 'OK', 'ops:tools': 'ERROR'}))

    # when
    sweep = run(ctx)

    # then
    assert sweep.result.ok and sweep.result.found == [] and sweep.result.ran == ('sonarqube', 'sonarqube:access:projects')
    assert ctx.memos['gates']['quiet'] == ['ops:tools', 'shop:web'] and 'baseline' in ctx.memos['gates']


def test_a_project_that_turns_red_after_the_first_run_opens_one_card_with_the_failed_conditions():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)
    run(ctx)
    sonar.states['shop:api'] = 'ERROR'

    # when
    sweep = run(ctx)

    # then
    (card,) = sweep.result.found
    assert (card['key'], card['src'], card['sig'], card['sev'], card['svc'], card['label']) == ('sonar-gate|-/shop:api', 'sonarqube', 'sonar-gate', 'warn', 'shop:api', 'quality gate failed')
    assert 'Shop API' in card['what'] and 'new_coverage' in card['what'] and '61.2' in card['what'] and 'new_security_rating' in card['what'] and 'coverage 90' not in card['what']
    assert ['shop:api'] in [c[1]['projectKey'] for c in ctx.http_.hits(STATUS)]


def test_a_project_already_red_at_the_first_run_stays_quiet_until_it_recovers_and_fails_again():
    # given
    sonar = Sonar({'shop:web': 'ERROR', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)
    run(ctx)

    # when
    still = run(ctx)
    sonar.states['shop:web'] = 'OK'
    recovered = run(ctx)
    sonar.states['shop:web'] = 'ERROR'
    again = run(ctx)

    # then
    assert still.result.found == [] and recovered.result.found == [] and ctx.memos['gates']['quiet'] == []
    assert keys(again) == ['sonar-gate|-/shop:web']


def test_a_card_is_raised_again_every_day_while_the_gate_stays_red_so_it_does_not_clear():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)
    run(ctx)
    sonar.states['ops:tools'] = 'ERROR'

    # when
    first, second = run(ctx), run(ctx)

    # then
    assert keys(first) == keys(second) == ['sonar-gate|-/ops:tools'] and 'sonarqube' in second.result.ran


def test_a_project_that_first_appears_already_red_after_the_first_run_opens_a_card():
    # given
    sonar = Sonar({'shop:web': 'OK'}, projects=PROJECTS[:1])
    ctx = Ctx(sonar)
    run(ctx)
    sonar.projects, sonar.states = list(PROJECTS), {'shop:web': 'OK', 'shop:api': 'ERROR', 'ops:tools': 'OK'}

    # when
    sweep = run(ctx)

    # then
    assert keys(sweep) == ['sonar-gate|-/shop:api']


def test_only_a_failed_gate_makes_a_card_a_warning_or_an_unanalysed_project_does_not():
    # given
    sonar = Sonar({'shop:web': 'WARN', 'shop:api': 'NONE', 'ops:tools': None})
    ctx = Ctx(sonar)
    run(ctx)

    # when
    sweep = run(ctx)

    # then
    assert sweep.result.found == [] and ctx.memos['gates']['quiet'] == []


def test_the_project_filter_matches_keys_and_names_with_wildcards_and_leaves_the_rest_unread():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar, {'url': URL, 'token': TOKEN, 'projects': 'shop:*, Ops Tools'})
    run(ctx)
    wide = sorted(c[1]['projectKey'][0] for c in sonar.hits(STATUS))
    sonar.calls.clear()
    sonar.states = {'shop:web': 'ERROR', 'shop:api': 'ERROR', 'ops:tools': 'ERROR'}
    ctx.config = {'url': URL, 'token': TOKEN, 'projects': 'shop:w*'}

    # when
    sweep = run(ctx)

    # then
    assert keys(sweep) == ['sonar-gate|-/shop:web']
    assert wide == ['ops:tools', 'shop:api', 'shop:web']
    assert sorted(c[1]['projectKey'][0] for c in sonar.hits(STATUS)) == ['shop:web']


def test_a_token_is_sent_as_the_basic_auth_user_and_projects_are_read_one_gate_at_a_time():
    # given
    many = [{'key': f'p{i:03}', 'name': f'P {i}'} for i in range(120)]
    sonar = Sonar({p['key']: 'OK' for p in many}, projects=many)
    ctx = Ctx(sonar)

    # when
    run(ctx)

    # then
    first = sonar.calls[0]
    assert first[2]['Authorization'] == 'Basic ' + base64.b64encode(f'{TOKEN}:'.encode()).decode()
    assert len(sonar.hits(STATUS)) == 120 and len(sonar.hits(SEARCH)) == 1


def test_a_401_or_403_on_the_list_opens_the_access_card_and_touches_neither_the_memo_nor_other_cards():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)
    run(ctx)
    before = dict(ctx.memos)
    sonar.fail[SEARCH] = HttpError(401, SEARCH)

    # when
    sweep = run(ctx)

    # then
    (card,) = sweep.result.found
    assert card['sig'] == 'no-access' and card['ranKey'] == 'sonarqube:access:projects' and sweep.result.ran == ('sonarqube:access:projects',)
    assert sweep.memos == {} and ctx.memos == before and TOKEN not in json.dumps(card)


def test_a_failed_gate_read_fails_the_pass_without_a_memo_and_clears_nothing():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)
    run(ctx)
    before = json.dumps(ctx.memos, sort_keys=True)
    sonar.fail[STATUS] = Unreachable(STATUS)

    # when
    sweep = run(ctx)

    # then
    assert sweep.result.ok is False and sweep.result.found == [] and sweep.memos == {} and json.dumps(ctx.memos, sort_keys=True) == before
    assert TOKEN not in sweep.result.status


def test_a_status_answer_without_a_project_status_fails_the_pass_and_keeps_the_baseline():
    # given
    sonar = Sonar({'shop:web': 'ERROR', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)
    run(ctx)
    sonar.blank = True

    # when
    broken = run(ctx)
    sonar.blank = False
    back = run(ctx)

    # then
    assert broken.result.ok is False and broken.memos == {} and ctx.memos['gates']['quiet'] == ['shop:web']
    assert back.result.found == []


def test_a_gate_read_that_hangs_in_a_simulated_timeout_stops_after_the_first_call():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)
    run(ctx)
    sonar.states = {'shop:web': 'ERROR', 'shop:api': 'ERROR', 'ops:tools': 'ERROR'}
    sonar.calls.clear()
    sonar.fail[STATUS] = remote.TimedOut(STATUS)

    # when
    sweep = run(ctx)

    # then
    assert sweep.result.ok is False and len(sonar.hits(STATUS)) == 1


def test_a_timeout_after_some_gates_were_read_keeps_the_read_ones_and_leaves_the_rest_unread():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)
    run(ctx)
    sonar.states = {'shop:web': 'OK', 'shop:api': 'ERROR', 'ops:tools': 'ERROR'}
    sonar.fail_for['shop:api'] = remote.TimedOut(STATUS)
    sonar.calls.clear()

    # when
    sweep = run(ctx)

    # then
    assert sweep.result.ok and sweep.result.found == [] and 'not read' in sweep.result.status and len(sonar.hits(STATUS)) == 2
    assert 'sonarqube' not in sweep.result.ran and ctx.memos['gates']['quiet'] == []


def test_a_403_on_one_project_leaves_only_that_project_unread():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)
    run(ctx)
    sonar.states['shop:web'] = 'ERROR'
    sonar.states['ops:tools'] = 'ERROR'
    sonar.fail_for['shop:api'] = HttpError(403, STATUS)

    # when
    sweep = run(ctx)

    # then
    assert keys(sweep) == ['sonar-gate|-/ops:tools', 'sonar-gate|-/shop:web'] and 'not read' in sweep.result.status


def test_a_401_on_a_gate_read_opens_the_access_card():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    sonar.fail[STATUS] = HttpError(401, STATUS)

    # when
    sweep = run(Ctx(sonar))

    # then
    assert [c['sig'] for c in sweep.result.found] == ['no-access'] and sweep.memos == {}


def test_the_pass_reads_under_a_time_budget_and_each_call_under_its_share():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)

    # when
    run(ctx)

    # then
    assert engine.BUDGET_S <= 300 and all(c[3] * remote.PAGE_DEADLINE <= engine.BUDGET_S for c in sonar.calls)


def test_test_connection_reads_under_a_short_budget():
    # given
    sonar = Sonar({})

    # when
    sonarqube.test(Ctx(sonar))

    # then
    assert [c[0] for c in sonar.calls] == [VALID, SEARCH] and all(c[3] * remote.PAGE_DEADLINE <= 12 for c in sonar.calls)


def test_a_cut_read_clears_only_the_read_projects_and_keeps_the_unread_baseline():
    # given
    many = [{'key': f'p{i}', 'name': f'P {i}'} for i in range(engine.PAGE * engine.PAGES + 5)]
    sonar = Sonar({'p0': 'OK', 'p1': 'OK'}, projects=many)
    ctx = Ctx(sonar, memos={'gates': {'quiet': ['p5004', 'p1'], 'baseline': 'x'}},
              cards=[{'src': 'sonarqube', 'sig': 'sonar-gate', 'svc': 'p0', 'ranKey': 'sonarqube:p0'}, {'src': 'sonarqube', 'sig': 'sonar-gate', 'svc': 'p5004', 'ranKey': 'sonarqube:p5004'}])

    # when
    sweep = run(ctx)

    # then
    assert 'sonarqube' not in sweep.result.ran and 'sonarqube:p0' in sweep.result.ran and 'sonarqube:p5004' not in sweep.result.ran
    assert ctx.memos['gates']['quiet'] == ['p5004'] and 'more not read' in sweep.result.status


def test_a_card_says_it_is_failing_without_claiming_when_it_started():
    # given
    sonar = Sonar({'shop:web': 'OK', 'shop:api': 'OK', 'ops:tools': 'OK'})
    ctx = Ctx(sonar)
    run(ctx)
    sonar.states['shop:api'] = 'ERROR'

    # when
    (card,) = run(ctx).result.found

    # then
    assert 'last daily check' not in card['what'] and 'could not be read' not in card['what']


def test_no_projects_at_all_reads_as_no_data_and_keeps_the_cards_and_the_baseline_pending():
    # given
    ctx = Ctx(Sonar({}, projects=[]))

    # when
    sweep = run(ctx)

    # then
    assert sweep.result.ok and sweep.result.ran == ('sonarqube:no-data', 'sonarqube:access:projects') and sweep.memos == {}


def test_a_filter_that_matches_nothing_clears_and_records_an_empty_baseline():
    # given
    ctx = Ctx(Sonar({'shop:web': 'ERROR'}), {'url': URL, 'token': TOKEN, 'projects': 'nothing-*'})

    # when
    sweep = run(ctx)

    # then
    assert sweep.result.ran == ('sonarqube', 'sonarqube:access:projects') and ctx.memos['gates']['quiet'] == []


def test_more_projects_than_the_cap_are_cut_and_said_in_the_status():
    # given
    many = [{'key': f'p{i}', 'name': f'P {i}'} for i in range(engine.PAGE * engine.PAGES + 5)]
    ctx = Ctx(Sonar({}, projects=many))

    # when
    sweep = run(ctx)

    # then
    assert 'not read' in sweep.result.status and len(ctx.http_.hits(SEARCH)) == engine.PAGES


def test_test_connection_validates_the_token_with_a_real_read_and_counts_projects():
    # given
    sonar = Sonar({})

    # when
    good = sonarqube.test(Ctx(sonar))

    # then
    assert good.ok and good.summary == 'SonarQube \u00b7 3 projects'
    assert [c[0] for c in sonar.calls] == [VALID, SEARCH]


@pytest.mark.parametrize('route, failure, said', [(SEARCH, HttpError(401, SEARCH), 'token'), (SEARCH, HttpError(403, SEARCH), 'permission'), (SEARCH, Unreachable('x'), 'reach'),
                                                   (VALID, HttpError(401, VALID), 'token')])
def test_test_connection_says_what_went_wrong_without_the_token(route, failure, said):
    # given
    sonar = Sonar({})
    sonar.fail[route] = failure

    # when
    out = sonarqube.test(Ctx(sonar))

    # then
    assert out.ok is False and said in out.summary.lower() and TOKEN not in out.summary


def test_test_connection_refuses_a_token_that_validate_calls_invalid_even_on_http_200():
    # given
    class Invalid(Sonar):
        def __call__(self, method, url, headers, data, timeout):
            if url.split('?')[0] == VALID:
                return Reply({'valid': False}, {})
            return super().__call__(method, url, headers, data, timeout)

    # when
    out = sonarqube.test(Ctx(Invalid({})))

    # then
    assert out.ok is False and 'token' in out.summary.lower()


def test_the_card_links_to_the_project_dashboard_on_https_only():
    # given
    card = {'src': 'sonarqube', 'sig': 'sonar-gate', 'svc': 'shop:api'}
    info = InstanceInfo('main', True, None, {'url': URL, 'token': TOKEN})
    plain = InstanceInfo('main', True, None, {'url': 'http://sonar.example.com'})

    # then
    assert sonarqube.links(card, info) == [{'label': 'SonarQube project', 'url': f'{URL}/dashboard?id=shop%3Aapi'}]
    assert sonarqube.links(card, plain) is None and sonarqube.links({'src': 'sonarqube', 'sig': 'no-access'}, info) is None


def test_only_read_paths_are_allowed():
    # then
    assert {path for path in engine.ALLOW} == {'/api/authentication/validate', '/api/components/search', '/api/qualitygates/project_status'}
