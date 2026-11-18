import pytest

from skopax.errkind import NoAccess
from skopax.investigator.tools import ToolError
from skopax.kube import HttpFailed
from skopax.messages import msg
from skopax.remote import HttpError
from skopax.store import Store
from tests.conftest import AUTH
from tests.fake_http import FakeHttp
from tests.test_argo import APPS
from tests.test_argo_pass import CHECKOUT, NOW, app_in, argo_runtime, in_cluster, run_check
from tests.test_argo_tool import argo_tools
from tests.test_argocd_plugin import API_TOKEN, DOT, EAST, LIST, VERSION, cards, connection_of, serving

ACCESS = 'argo:access:applications'
CARD = 'no-access|-/argo|applications'
API_LABEL = 'Argo CD cannot read applications: the token lacks the RBAC permission applications, get'
KUBE_LABEL = 'Argo CD cannot read applications: the service account lacks get and list on applications.argoproj.io in the namespace argocd'
API_DENIED = 'Argo CD refused to read applications (HTTP 403): the token lacks the RBAC permission applications, get'
KUBE_DENIED = 'Argo CD refused to read applications (HTTP 403): the service account lacks get and list on applications.argoproj.io in the namespace argocd'
BLIND = 'a token without the RBAC permission applications, get sees none either'
CAN_I = 'https://argocd.example.com/api/v1/account/can-i/applications/get/*/*'
NO_APP = f'Argo CD has no application {CHECKOUT}, or the token lacks the RBAC permission applications, get'
NO_APP_401 = f'Argo CD has no application {CHECKOUT}, or the token is invalid or lacks the RBAC permission applications, get'


def test_a_refused_list_is_an_ok_pass_with_a_warn_card_that_keeps_the_app_cards_open(store: Store):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    answers = [{'items': [app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed')]}]
    http = FakeHttp({('GET', LIST): lambda query, body: answers[0]})

    # when
    with serving(http, store, main={'token': API_TOKEN}) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        rows = store.load()['argo']
        answers[0] = HttpError(403, LIST, '{"message": "permission denied"}')
        c.app.state.sweeper.run('argocd', 'argo', NOW + 300)
        refused, state = cards(store), store.load()
        answers[0] = {'items': [app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed')]}
        c.app.state.sweeper.run('argocd', 'argo', NOW + 600)
    read_again = cards(store)

    # then
    assert state['serverPasses']['argo']['status'] == f'ok {DOT} no access to applications' and state['argo'] == rows
    card = refused[CARD]
    assert (card['status'], card['sev'], card['src'], card['ranKey'], card['label']) == ('open', 'warn', 'argo', ACCESS, API_LABEL)
    assert refused['argo|shop/checkout-service']['status'] == 'open', 'the refused part keeps its cards open'
    assert read_again[CARD]['status'] == 'cleared' and read_again['argo|shop/checkout-service']['status'] == 'open'


def test_a_401_says_the_token_may_be_invalid_and_in_cluster_names_the_service_account(monkeypatch, tmp_path):
    # given
    revoked = FakeHttp({('GET', LIST): HttpError(401, LIST)})

    # when
    api = run_check(argo_runtime(revoked, token=API_TOKEN))
    in_cluster(monkeypatch, tmp_path, {APPS: HttpFailed(403, 'applications.argoproj.io is forbidden')})
    kube = run_check(argo_runtime(FakeHttp(), mode='in-cluster'))

    # then
    assert (api.result.ok, api.result.status, api.result.ran) == (True, f'ok {DOT} no access to applications', (ACCESS,))
    assert api.result.found[0]['label'] == 'Argo CD cannot read applications: the token is invalid or lacks the RBAC permission applications, get'
    assert (kube.result.ok, kube.result.ran, [c['label'] for c in kube.result.found]) == (True, (ACCESS,), [KUBE_LABEL])
    assert api.sections == {} and kube.sections == {}


def test_a_failure_that_is_no_refusal_fails_in_the_part_form_and_keeps_the_access_card(store: Store):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    answers = [HttpError(403, LIST)]
    http = FakeHttp({('GET', LIST): lambda query, body: answers[0]})

    # when
    with serving(http, store, main={'token': API_TOKEN}) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        answers[0] = HttpError(502, LIST)
        c.app.state.sweeper.run('argocd', 'argo', NOW + 300)
        state = store.load()

    # then
    assert state['serverPasses']['argo']['status'] == 'error: applications: HTTP 502 on Argo CD applications'
    assert cards(store)[CARD]['status'] == 'open', 'a 5xx never clears a real refusal'


def test_every_read_lists_the_access_key_and_an_empty_api_list_leaves_it_out(monkeypatch, tmp_path):
    # given
    good = FakeHttp({('GET', LIST): {'items': [app_in(CHECKOUT, 'shop')]}})
    empty = FakeHttp({('GET', LIST): {'items': []}})

    # when
    read = run_check(argo_runtime(good, token=API_TOKEN))
    blind = run_check(argo_runtime(empty, token=API_TOKEN))
    in_cluster(monkeypatch, tmp_path, {APPS: {'items': []}})
    none = run_check(argo_runtime(FakeHttp(), mode='in-cluster'))
    in_cluster(monkeypatch, tmp_path, {APPS: lambda query: {'metadata': {'continue': 'p2', 'remainingItemCount': 3}, 'items': [app_in(CHECKOUT, 'shop')]}})
    monkeypatch.setattr('skopax.plugins.argocd.argo.PASS_PAGES', 1)
    capped = run_check(argo_runtime(FakeHttp(), mode='in-cluster'))

    # then
    assert read.result.status == 'ok' and read.result.ran[-1] == ACCESS
    assert (blind.result.status, blind.result.ran) == (f'ok {DOT} no data', ('argo:no-data',)), 'Argo CD filters the list by RBAC: an empty answer cannot clear a refusal'
    assert (none.result.status, none.result.ran) == (f'ok {DOT} no data', ('argo:no-data', ACCESS))
    assert capped.result.status == f'ok {DOT} read 1 of 4 applications' and capped.result.ran == (f'argo:{CHECKOUT}', ACCESS)


@pytest.mark.parametrize('answer, summary', [
    (HttpError(403, LIST, '{"message": "permission denied"}'), API_DENIED),
    (HttpError(401, LIST), msg('connection.rejected', status=401)),
])
def test_the_api_connection_test_names_the_rbac_permission(answer, summary):
    # given
    http = FakeHttp({('GET', VERSION): {'Version': 'v2.13.1'}, ('GET', LIST): answer})

    # when
    result = connection_of(http, token=API_TOKEN)

    # then
    assert (result.ok, result.summary) == (False, summary)


def test_the_in_cluster_test_names_the_role_and_an_empty_api_list_says_what_a_blind_token_sees(monkeypatch, tmp_path):
    # given
    in_cluster(monkeypatch, tmp_path, {APPS: HttpFailed(403, 'forbidden')})
    empty = FakeHttp({('GET', VERSION): {'Version': 'v2.13.1'}, ('GET', LIST): {'items': []}})

    # when
    forbidden = connection_of(mode='in-cluster')
    in_cluster(monkeypatch, tmp_path, {APPS: HttpFailed(401, 'unauthorized')})
    revoked = connection_of(mode='in-cluster')
    blind = connection_of(empty, token=API_TOKEN)

    # then
    assert (forbidden.ok, forbidden.summary) == (False, KUBE_DENIED)
    assert (revoked.ok, revoked.summary) == (False, msg('connection.rejected', status=401))
    assert (blind.ok, blind.summary) == (True, f'Argo CD v2.13.1: 0 applications; {BLIND}')


def test_a_refused_tool_call_is_a_tool_error_naming_the_permission_and_an_empty_list_says_it_may_be_one(monkeypatch, tmp_path):
    # given
    refused = FakeHttp({('GET', LIST): HttpError(403, LIST), ('GET', f'{LIST}/{CHECKOUT}'): HttpError(403, LIST)})
    empty = FakeHttp({('GET', LIST): {'items': []}})

    # when
    errors = []
    for args in ({'op': 'apps'}, {'op': 'app', 'name': CHECKOUT}):
        with pytest.raises(ToolError) as e:
            argo_tools(refused, token=API_TOKEN).call('argo', args, NOW)
        errors.append(e.value.text)
    listed = argo_tools(empty, token=API_TOKEN).call('argo', {'op': 'apps'}, NOW)
    in_cluster(monkeypatch, tmp_path, {APPS: HttpFailed(403, 'forbidden')})
    with pytest.raises(ToolError) as kube:
        argo_tools(mode='in-cluster').call('argo', {'op': 'apps'}, NOW)

    # then
    assert errors == [API_DENIED, NO_APP]
    assert listed == [{'note': f'Argo CD listed no applications; {BLIND}'}]
    assert kube.value.text == KUBE_DENIED


def test_the_refusal_is_one_access_error_carrying_the_part_and_the_permission(monkeypatch, tmp_path):
    # given
    from skopax.plugins.argocd.argo import ApiSource, ArgoError, KubeSource
    from skopax.kube import Kube
    from tests.test_kube import BASE, FakeApi, TOKEN
    (tmp_path / 'token').write_text(TOKEN)
    kube = KubeSource(Kube(root=tmp_path, base=BASE, transport=FakeApi({APPS: HttpFailed(403, 'forbidden')})), 'argocd')
    api = ApiSource('https://argocd.example.com', API_TOKEN, FakeHttp({('GET', LIST): HttpError(403, LIST)}))

    # when
    raised = []
    for source in (api, kube):
        with pytest.raises(ArgoError) as e:
            source.list()
        raised.append(e.value)

    # then
    assert all(isinstance(e, NoAccess) and e.part == 'applications' for e in raised)
    assert [(e.status, e.permission) for e in raised] == [(403, 'the RBAC permission applications, get'), (403, 'get and list on applications.argoproj.io in the namespace argocd')]


def test_a_second_refused_instance_drops_nothing_and_raises_its_own_access_card(store: Store):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    http = FakeHttp({('GET', LIST): HttpError(403, LIST), ('GET', f'{EAST}/api/v1/applications'): HttpError(403, LIST)})

    # when
    with serving(http, store, main={'token': API_TOKEN}, east={'url': EAST, 'token': API_TOKEN}) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        state = store.load()

    # then
    assert state['serverPasses']['argo']['status'] == f'ok {DOT} no access to applications'
    assert state['serverPasses']['argo-east']['status'] == f'ok {DOT} no access to applications'
    assert sorted((k, a['status'], a['ranKey'], a.get('cluster')) for k, a in cards(store).items()) == [(CARD, 'open', ACCESS, None), (f'{CARD}@east', 'open', f'{ACCESS}-east', 'east')]


def test_a_refused_instance_shows_no_access_in_admin_and_the_one_that_reads_stays_healthy(store: Store):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    http = FakeHttp({('GET', LIST): {'items': [app_in(CHECKOUT, 'shop')]}, ('GET', f'{EAST}/api/v1/applications'): HttpError(403, LIST)})

    # when
    with serving(http, store, main={'token': API_TOKEN}, east={'url': EAST, 'token': API_TOKEN}) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        health = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'argocd')['health']

    # then
    shown = msg('integrations.health.no_access')
    assert (health['state'], health['text']) == ('warn', shown)
    assert {i['name']: (i['state'], i['text']) for i in health['instances']} == {'main': ('ok', msg('integrations.health.ok')), 'east': ('warn', shown)}


def empty_with(answer) -> FakeHttp:
    return FakeHttp({('GET', LIST): {'items': []}, ('GET', CAN_I): answer})


def test_an_empty_api_list_asks_can_i_a_yes_lists_the_access_key_a_no_is_a_refusal_and_a_failure_changes_nothing():
    # given
    asks = {'yes': empty_with({'value': 'yes'}), 'no': empty_with({'value': 'no'}), 'failed': empty_with(HttpError(502, CAN_I))}

    # when
    got = {k: run_check(argo_runtime(http, token=API_TOKEN)).result for k, http in asks.items()}

    # then
    assert (got['yes'].status, got['yes'].ran, got['yes'].found) == (f'ok {DOT} no data', ('argo:no-data', ACCESS), [])
    assert (got['no'].status, got['no'].ran, [c['label'] for c in got['no'].found]) == (f'ok {DOT} no access to applications', (ACCESS,), [API_LABEL])
    assert (got['failed'].status, got['failed'].ran, got['failed'].found) == (f'ok {DOT} no data', ('argo:no-data',), [])
    assert all(http.hits(CAN_I)[0][2]['Authorization'] == f'Bearer {API_TOKEN}' for http in asks.values())


def test_a_fixed_token_on_an_argo_cd_without_applications_closes_the_access_card(store: Store):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    answers = [HttpError(403, LIST)]
    http = FakeHttp({('GET', LIST): lambda query, body: answers[0], ('GET', CAN_I): {'value': 'yes'}})

    # when
    with serving(http, store, main={'token': API_TOKEN}) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        opened = cards(store)[CARD]['status']
        answers[0] = {'items': []}
        c.app.state.sweeper.run('argocd', 'argo', NOW + 300)

    # then
    assert (opened, cards(store)[CARD]['status']) == ('open', 'cleared')


def test_the_api_test_and_the_apps_tool_ask_can_i_on_an_empty_list():
    # given
    def http(answer) -> FakeHttp:
        return FakeHttp({('GET', VERSION): {'Version': 'v2.13.1'}, ('GET', LIST): {'items': []}, ('GET', CAN_I): answer})

    # when
    tested = {k: connection_of(http(a), token=API_TOKEN) for k, a in (('yes', {'value': 'yes'}), ('no', {'value': 'no'}))}
    listed = argo_tools(empty_with({'value': 'yes'}), token=API_TOKEN).call('argo', {'op': 'apps'}, NOW)
    with pytest.raises(ToolError) as refused:
        argo_tools(empty_with({'value': 'no'}), token=API_TOKEN).call('argo', {'op': 'apps'}, NOW)

    # then
    assert (tested['yes'].ok, tested['yes'].summary) == (True, 'Argo CD v2.13.1: 0 applications')
    assert (tested['no'].ok, tested['no'].summary) == (False, API_LABEL)
    assert listed == [{'note': 'Argo CD listed no applications'}]
    assert refused.value.text == API_LABEL


def test_an_unknown_application_and_a_refused_one_read_alike_keep_the_name_and_a_401_says_the_token_may_be_invalid():
    # given
    from skopax.plugins.argocd.argo import ApiSource, Argo, ArgoError
    errors = []

    # when
    for code in (401, 403):
        http = FakeHttp({('GET', f'{LIST}/{CHECKOUT}'): HttpError(code, LIST)})
        with pytest.raises(ArgoError) as e:
            Argo(ApiSource('https://argocd.example.com', API_TOKEN, http), 'https://argocd.example.com', 'argocd').app(CHECKOUT)
        errors.append((e.value.status, e.value.message))

    # then
    assert errors == [(401, NO_APP_401), (403, NO_APP)]


def test_a_server_message_that_echoes_the_token_is_masked_on_other_api_errors():
    # given
    from skopax.plugins.argocd.argo import ApiSource, Argo, ArgoError
    echo = f'{{"message": "application x not found for token {API_TOKEN}"}}'
    errors = []

    # when
    for code in (404, 500):
        http = FakeHttp({('GET', f'{LIST}/{CHECKOUT}'): HttpError(code, LIST, echo)})
        with pytest.raises(ArgoError) as e:
            Argo(ApiSource('https://argocd.example.com', API_TOKEN, http), 'https://argocd.example.com', 'argocd').app(CHECKOUT)
        errors.append(e.value.message)

    # then
    assert errors == [f'HTTP {code} on Argo CD application {CHECKOUT}: application x not found for token ***' for code in (404, 500)]


def policy_server(policy: str):
    from fnmatch import fnmatchcase
    from skopax.remote import Reply
    asked = []
    head = 'https://argocd.example.com/api/v1/account/can-i/applications/get/'

    def send(method, url, headers, data, timeout):
        if url == LIST:
            return Reply({'items': []}, {})
        if url.startswith(head):
            obj = url[len(head):]
            asked.append(obj)
            return Reply({'value': 'yes' if fnmatchcase(obj, policy) else 'no'}, {})
        raise HttpError(404, url)

    return send, asked


def test_can_i_asks_for_the_object_star_slash_star_so_the_built_in_roles_answer_yes():
    # given
    readonly, asked = policy_server('*/*')

    # when
    result = run_check(argo_runtime(readonly, token=API_TOKEN)).result

    # then
    assert asked == ['*/*'], 'the object is sent with its slash, not percent-encoded; a bare * would not match the built-in */* roles'
    assert (result.status, result.ran) == (f'ok {DOT} no data', ('argo:no-data', ACCESS))


@pytest.mark.parametrize('answer, kept', [({'value': 'no'}, 'open'), (HttpError(502, CAN_I), 'open'), ({'value': 'yes'}, 'cleared')])
def test_an_app_that_drops_out_of_the_list_closes_its_card_only_when_can_i_says_the_token_sees_every_project(store: Store, answer, kept):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    lists = [{'items': [app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed'), app_in('web-storefront', 'web')]}]
    http = FakeHttp({('GET', LIST): lambda query, body: lists[0], ('GET', CAN_I): answer})

    # when
    with serving(http, store, main={'token': API_TOKEN}) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        opened = cards(store)['argo|shop/checkout-service']['status']
        lists[0] = {'items': [app_in('web-storefront', 'web')]}
        c.app.state.sweeper.run('argocd', 'argo', NOW + 300)

    # then
    assert opened == 'open' and cards(store)['argo|shop/checkout-service']['status'] == kept
    assert len(http.hits(CAN_I)) == 1, 'can-i is asked once, only when an open card names an application the list left out'
