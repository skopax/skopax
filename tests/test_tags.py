import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings, state
from skopax import tags
from skopax.app import create_app
from skopax.config import tags_rules_of
from tests.test_investigator_tools import box
from skopax.kube import Kube
from skopax.routes import Route
from skopax.plugins.website import pagescan
from skopax.store import Store, keep_slack_state
from tests.teams_kit import BIZDEV, SEO, TECH, Transport, add_route, route, routes_of, texts

NOW = 1_794_240_000.0
AT = '2026-11-09T16:00:00Z'
TYPO = {'wrong': 'Gift Crad', 'fix': 'Gift Card', 'where': 'FAQ'}


def typo_card(url: str, typos: list[dict]) -> dict:
    return pagescan.typo_card(url, typos)


def layout_card(url: str, issues: list[dict]) -> dict:
    return pagescan.layout_card(url, issues)


def open_card(key: str, sev: str = 'warn', **extra) -> dict:
    return {'key': key, 'sev': sev, 'svc': 'checkout-service', 'label': 'ERROR lines', 'what': 'NullPointerException in OrderService', 'status': 'open',
            'firstSeen': '2026-11-09T10:00:00Z', **extra}


def test_rules_tag_typo_removed_page_and_gtm_cards_and_write_no_sentence():
    # given
    typo = typo_card('https://shop.example.com/products', [TYPO, {'wrong': 'pirce', 'fix': 'price', 'where': ''}])
    gone = layout_card('https://shop.example.com/old-campaign', [{'wrong': 'overflow', 'where': ''}]) | {'gone': {'status': 404, 'since': AT}}
    layout = layout_card('https://shop.example.com/shipping', [{'wrong': 'overflow', 'where': ''}])
    gtm = {'key': 'gtm-publish|-/GTM-ABC|v12', 'src': 'gtm', 'sig': 'gtm-publish', 'sev': 'info'}
    log = open_card('log|checkout-service|ERROR|boom', src='logs', sig='log')

    # when
    tags.apply_rules([typo, gone, layout, gtm, log], tags.RULES, AT)

    # then
    assert typo['tags'] == [{'id': 'bizdev', 'by': 'rule', 'at': AT}], 'typos are business development work'
    assert 'business' not in typo and 'business' not in gone
    assert tags.ids(gone) == ['seo']
    assert 'tags' not in layout and 'business' not in layout
    assert tags.ids(gtm) == ['marketing'] and 'business' not in gtm
    assert 'tags' not in log


def test_a_tag_a_person_removed_stays_off_their_sentence_stays_and_a_rule_tag_goes_when_its_rule_no_longer_matches():
    # given
    removed = typo_card('https://shop.example.com/products', [TYPO]) | {'tagsOff': ['bizdev']}
    written = typo_card('https://shop.example.com/cart', [TYPO]) | {'business': {'text': 'Fix the typo on the cart page.', 'by': 'nina.white@example.com', 'at': AT}}
    stale = typo_card('https://shop.example.com/shipping', [TYPO]) | {'tags': [{'id': 'seo', 'by': 'rule', 'at': AT}, {'id': 'marketing', 'by': 'pod', 'at': AT}]}
    rules = tags_rules_of({'tags.bizdev.rules': 'gone'})

    # when
    tags.apply_rules([removed, written, stale], tags.RULES, '2026-11-09T17:00:00Z')
    tags.apply_rules([stale], rules, '2026-11-09T17:00:00Z')

    # then
    assert 'tags' not in removed
    assert written['business']['by'] == 'nina.white@example.com' and tags.ids(written) == ['bizdev']
    assert stale['tags'] == [{'id': 'marketing', 'by': 'pod', 'at': AT}], 'the old SEO rule tag went, and the bizdev one went when its rule changed'
    assert dict(rules)['bizdev'] == ('gone',) and dict(rules)['marketing'] == ('type:gtm', 'type:umami', 'type:ga', 'type:ad-link*', 'type:ab-test', 'type:mailer')


def test_a_tag_change_a_person_made_while_a_pass_ran_survives_the_pass_save():
    # given
    stored = {'tags': [{'id': 'bizdev', 'by': 'nina.white@example.com', 'at': AT}], 'tagsOff': ['seo'], 'business': {'text': 'x', 'by': 'nina.white@example.com', 'at': AT}, 'tagsAt': AT}
    incoming = {'key': 'k', 'tags': [{'id': 'seo', 'by': 'rule', 'at': AT}], 'tagsAt': '2026-11-09T15:00:00Z'}

    # when
    kept = keep_slack_state(incoming, stored)
    newer = keep_slack_state({**incoming, 'tagsAt': '2026-11-09T18:00:00Z'}, stored)

    # then
    assert {f: kept[f] for f in tags.FIELDS} == stored
    assert newer['tags'] == incoming['tags']


def test_operators_tag_untag_and_write_the_sentence_and_the_card_goes_to_its_channel(store: Store, tmp_path):
    # given
    t = Transport()
    store.save({**state(open_card('log|checkout-service|ERROR|boom')), 'initialized': True}, None)
    key = 'log|checkout-service|ERROR|boom'

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'bizdev', BIZDEV, tags='bizdev')
        tagged = c.post('/api/tags', json={'key': key, 'tag': 'bizdev', 'on': True}, headers=AUTH)
        early = list(t.calls)
        said = c.put('/api/business', json={'key': key, 'text': '  The garden chair   price in the admin tool is out of date.  '}, headers=AUTH)
        seo_off = c.post('/api/tags', json={'key': key, 'tag': 'seo', 'on': False}, headers=AUTH)
        unknown = c.post('/api/tags', json={'key': key, 'tag': 'finance', 'on': True}, headers=AUTH)
        missing = c.post('/api/tags', json={'key': 'log|x|ERROR|y', 'tag': 'seo', 'on': True}, headers=AUTH)
        anonymous = c.put('/api/business', json={'key': key, 'text': 'x'})
        removed = c.put('/api/business', json={'key': key, 'text': ''}, headers=AUTH)
        assert c.app.state.routes.drain(5)
        me = c.get('/api/me').json()

    # then
    assert tagged.status_code == 200 and tagged.json()['tags'] == [{'id': 'bizdev', 'by': 'token:bootstrap', 'at': tagged.json()['tags'][0]['at']}]
    assert early == []
    assert said.json()['business']['text'] == "The garden chair price in the admin tool is out of date."
    [(url, body)] = t.calls
    assert url == BIZDEV and texts(body)[0] == 'Skopax \u00b7 Business Development'
    assert seo_off.json()['tagsOff'] == ['seo']
    assert (unknown.status_code, missing.status_code, anonymous.status_code) == (422, 404, 401)
    card = store.anomaly(key)
    assert 'business' not in card and card['tagsAt'] and card.get('triage') is None
    assert removed.json()['business'] is None and me['tags'] == tags.info()
    assert [a['text'] for a in store.list_activity(None, 20) if a['kind'] == 'tags'][:1] == [f'business sentence removed · {key}']


@pytest.fixture
def tool_store(store: Store) -> Store:
    store.save({**state(open_card('log|checkout-service|ERROR|boom'), open_card('log|a|ERROR|said', business={'text': 'A person wrote it.', 'by': 'nina.white@example.com', 'at': AT}),
                        open_card('log|a|ERROR|off', tagsOff=['seo'])), 'initialized': True}, None)
    return store


def test_the_investigator_tags_a_card_of_its_run_with_a_sentence_and_keeps_a_persons(tool_store: Store):
    # given
    t = box(tool_store, keys=('log|checkout-service|ERROR|boom', 'log|a|ERROR|said', 'log|a|ERROR|off'))

    # when
    saved, _ = t.call('tag', {'key': 'log|checkout-service|ERROR|boom', 'tags': ['bizdev'], 'text': 'Newsletter sign-ups fail; talk to the mailing provider.', 'why': 'New shoppers cannot sign up.'})
    kept, _ = t.call('tag', {'key': 'log|a|ERROR|said', 'tags': ['seo'], 'text': 'Another sentence.'})
    refused, error = t.call('tag', {'key': 'log|a|ERROR|off', 'tags': ['seo'], 'text': 'x'})
    empty, empty_error = t.call('tag', {'key': 'log|checkout-service|ERROR|boom', 'tags': ['seo']})
    blank, blank_error = t.call('tag', {'key': 'log|a|ERROR|off', 'tags': ['marketing']})

    # then
    card = tool_store.anomaly('log|checkout-service|ERROR|boom')
    assert saved == 'tag saved on log|checkout-service|ERROR|boom'
    assert card['tags'][0]['id'] == 'bizdev' and card['tags'][0]['by'] == 'pod' and tags.ids(card) == ['bizdev', 'seo']
    assert card['business']['text'] == 'Newsletter sign-ups fail; talk to the mailing provider.' and card['business']['by'] == 'pod'
    assert kept.endswith('the business sentence a person wrote stays as it is') and tool_store.anomaly('log|a|ERROR|said')['business']['text'] == 'A person wrote it.'
    assert error and 'a person removed seo' in refused
    assert not empty_error
    assert blank_error and 'text is required' in blank


def test_the_tag_tool_puts_the_why_first_and_refuses_an_action_with_no_why(tool_store: Store):
    # given
    t = box(tool_store, keys=('log|checkout-service|ERROR|boom', 'log|a|ERROR|said', 'log|a|ERROR|off'))
    tool = next(d for d in t.definitions() if d['name'] == 'tag')

    # when
    bare, bare_error = t.call('tag', {'key': 'log|checkout-service|ERROR|boom', 'tags': ['bizdev'], 'text': 'We recommend talking to the mailing provider.'})
    blank, blank_error = t.call('tag', {'key': 'log|checkout-service|ERROR|boom', 'tags': ['bizdev'], 'text': 'We recommend talking to the mailing provider.', 'why': '  '})
    info, info_error = t.call('tag', {'key': 'log|checkout-service|ERROR|boom', 'tags': ['bizdev'], 'kind': 'info', 'text': 'Newsletter sign-ups fail right now; engineering is fixing it.'})
    tech, tech_error = t.call('tag', {'key': 'log|a|ERROR|off', 'tags': ['developer']})
    kept, kept_error = t.call('tag', {'key': 'log|a|ERROR|said', 'tags': ['seo'], 'text': 'Another sentence.'})

    # then
    assert bare_error and blank_error and bare == blank and 'why is required for kind action' in bare
    assert not info_error and not tech_error and not kept_error, (info, tech, kept)
    assert tool_store.anomaly('log|checkout-service|ERROR|boom')['business']['kind'] == 'info'
    text = tool['description']
    assert text.index('why (required for kind action') < text.index('text: one plain sentence that recommends the action') and 'We recommend' in text and 'one plain instruction' not in text
    assert 'kind action' in tool['input_schema']['properties']['why']['description']


def test_operators_save_the_reason_with_the_action_and_the_investigator_can_too(store: Store, tmp_path):
    # given
    store.save({**state(open_card('log|checkout-service|ERROR|boom')), 'initialized': True}, None)
    key = 'log|checkout-service|ERROR|boom'

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        saved = c.put('/api/business', json={'key': key, 'text': "Update the price in the admin tool.", 'why': '  The campaign page shows the old price.  '}, headers=AUTH).json()
        plain = c.put('/api/business', json={'key': key, 'text': "Update the price in the admin tool."}, headers=AUTH).json()
    b = box(store, keys=(key,))
    b.call('tag', {'key': key, 'tags': ['marketing'], 'text': 'x', 'why': 'y'})

    # then
    assert saved['business']['why'] == 'The campaign page shows the old price.' and 'why' not in plain['business']
    assert plain['business']['by'] == 'token:bootstrap', 'a person wrote it, so the investigator keeps it'
    assert store.anomaly(key)['business']['text'] == "Update the price in the admin tool."


def test_a_card_whose_posts_field_was_stored_as_null_still_takes_a_teams_claim_and_a_fix_claim(store: Store):
    # given
    store.save(state(open_card('log|a|ERROR|null', teamsPosts=None, fixPosts=None)), None)
    assert store.anomalies.find_one({'_id': 'log|a|ERROR|null'})['teamsPosts'] is None
    post = {'route': 'seo', 'at': '2026-11-10T09:00:00Z', 'by': 'server'}

    # when
    first = store.teams_claim('log|a|ERROR|null', 'seo', '2026-11-09T10:00:00Z', post)
    again = store.teams_claim('log|a|ERROR|null', 'seo', '2026-11-09T10:00:00Z', post)
    store.teams_posted('log|a|ERROR|null', {'route': 'tech', 'at': '2026-11-10T09:01:00Z', 'by': 'server'})
    store.anomalies.update_one({'_id': 'log|a|ERROR|null'}, {'$set': {'slackPost': {'ts': '1.1'}}})
    fix = store.claim_fix('log|a|ERROR|null', '1.1', 'h1')

    # then
    assert (first, again) == (True, False) and fix is True
    assert [p['route'] for p in store.anomaly('log|a|ERROR|null')['teamsPosts']] == ['seo', 'tech']


def test_a_card_that_clears_and_fires_again_carries_no_null_post_lists():
    # given
    from skopax.engine import merge
    found = open_card('log|a|ERROR|again') | {'src': 'log', 'sig': 'err', 'ns': 'a', 'svc': 'a', 'sub': '', 'now': 5, 'd1': None, 'd7': None, 'unit': '', 'short': 'x', 'slack': 1}
    st = state(found | {'status': 'cleared', 'clearedAt': '2026-11-10T08:00:00Z', 'lastSeen': '2026-11-10T08:00:00Z', 'ranKey': 'log'})

    # when
    merge(st, [dict(found)], {'log': True}, 1_794_300_000.0)
    merge(st, [dict(found)], {'log': True}, 1_794_300_300.0)

    # then
    card = st['anomalies'][0]
    assert card['status'] == 'open' and 'teamsPosts' not in card and 'fixPosts' not in card


def test_the_tech_tag_is_a_card_tag_whose_only_built_in_rules_are_the_quality_cards_and_its_route_is_not_a_business_route():
    # given
    card = open_card('log|a|ERROR|x')

    # when
    fields = tags.toggled(card, 'developer', True, 'nina.white@example.com', AT)
    tags.apply_rules([card], tags.RULES, AT)

    # then
    eng, mixed, seo = (Route(f'teams/{name}', name, 'teams', name, frozenset(ids), (), None) for name, ids in (('eng', {'developer'}), ('all', {'developer', 'seo'}), ('seo', {'seo'})))
    assert tags.TAGS['developer'] == 'Developer' and dict(tags.RULES)['developer'] == ('type:sonar-gate', 'type:cve-deps') and not card.get('tags')
    assert fields['tags'] == [{'id': 'developer', 'by': 'nina.white@example.com', 'at': AT}]
    assert [(r.tagged, r.business, r.label) for r in (eng, mixed, seo)] == [(True, False, 'Developer'), (True, False, 'all'), (True, True, 'SEO')]
    assert dict(tags.rules_of({'tags.developer.rules': 'type:kafka-lag'}))['developer'] == ('type:kafka-lag',)


def test_the_investigator_adds_the_tech_tag_without_a_sentence_and_a_business_tag_still_needs_one(tool_store: Store):
    # given
    t = box(tool_store, keys=('log|checkout-service|ERROR|boom', 'log|a|ERROR|off'))

    # when
    saved, error = t.call('tag', {'key': 'log|checkout-service|ERROR|boom', 'tags': ['developer']})
    mixed, mixed_error = t.call('tag', {'key': 'log|a|ERROR|off', 'tags': ['developer', 'marketing']})

    # then
    card = tool_store.anomaly('log|checkout-service|ERROR|boom')
    assert not error and saved == 'tag saved on log|checkout-service|ERROR|boom'
    assert tags.ids(card) == ['developer'] and not card.get('business')
    assert mixed_error and 'text is required' in mixed and not tool_store.anomaly('log|a|ERROR|off').get('tags')


def test_an_operator_tags_a_card_tech_and_it_goes_to_the_tech_tag_route_and_by_hand_only_when_tagged(store: Store, tmp_path):
    # given
    t = Transport()
    store.save({**state(open_card('log|checkout-service|ERROR|boom'), open_card('log|a|ERROR|bare')), 'initialized': True}, None)
    key = 'log|checkout-service|ERROR|boom'

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'eng', TECH, tags='developer')
        tagged = c.post('/api/tags', json={'key': key, 'tag': 'developer', 'on': True}, headers=AUTH)
        assert c.app.state.routes.drain(5)
        untagged = c.post('/api/routes/send', json={'key': 'log|a|ERROR|bare', 'route': 'teams/eng'}, headers=AUTH)
        by_hand = c.post('/api/routes/send', json={'key': key, 'route': 'teams/eng'}, headers=AUTH)

    # then
    assert tagged.status_code == 200 and [x['id'] for x in tagged.json()['tags']] == ['developer']
    assert untagged.status_code == 422 and 'no Developer tag' in untagged.json()['detail']
    assert by_hand.json() == {'ok': True, 'sent': ['teams/eng']}
    assert [texts(b)[0] for _, b in t.calls] == ['Skopax \u00b7 Developer', 'Skopax \u00b7 Developer']
    assert [a['text'] for a in store.list_activity(None, 20) if a['kind'] == 'tags'][:1] == [f'tagged Developer · {key}']


def test_the_infra_tag_is_a_second_technical_tag_with_its_own_route_and_no_sentence_and_only_the_os_cve_rule(tool_store: Store):
    # given
    t = Transport()
    routes = routes_of(tool_store, t, route('infra', TECH, tags='infra'), route('seo', SEO, tags='seo'))
    box_ = box(tool_store, keys=('log|checkout-service|ERROR|boom',))

    # when
    saved, error = box_.call('tag', {'key': 'log|checkout-service|ERROR|boom', 'tags': ['infra']})
    sent = routes.tagged(tool_store.load()['anomalies'])

    # then
    assert tags.TAGS['infra'] == 'Infra' and dict(tags.RULES)['infra'] == ('type:cve-os',) and tags.TECHNICAL == ('developer', 'data', 'infra', 'analyst')
    assert not error and tags.ids(tool_store.anomaly('log|checkout-service|ERROR|boom')) == ['infra']
    assert [(r.tagged, r.business, r.label) for r in routes.routes()] == [(True, False, 'Infra'), (True, True, 'SEO')]
    assert sent == {'teams/infra': ['log|checkout-service|ERROR|boom']} and texts(t.calls[0][1])[0] == 'Skopax \u00b7 Infra'


def test_the_shipped_tag_rules_and_labels_are_empty_and_a_config_of_rules_changes_nothing():
    # given
    import importlib.util

    spec = importlib.util.spec_from_file_location('skopax_tags_fresh', tags.__file__)
    fresh = importlib.util.module_from_spec(spec)

    # when
    spec.loader.exec_module(fresh)

    # then
    assert fresh.TAGS == {'developer': 'Developer', 'data': 'Data', 'infra': 'Infrastructure', 'analyst': 'Analyst'} and fresh.RULES == () and [t['id'] for t in fresh.info()] == ['developer', 'data', 'infra', 'analyst']
    assert fresh.rules_of({'tags.seo.rules': 'gone'}) == ()
