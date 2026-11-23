import logging

import pytest
from fastapi.testclient import TestClient

from skopax import tags
from skopax.app import create_app
from skopax.contract import SuggestContext
from skopax.plugins import email_route, teams
from skopax.registry import Registry
from skopax.routes import route_of
from tests.conftest import AUTH, settings, state
from tests.teams_kit import SEO, Transport, route as teams_route, runtime_of


def field_of(plugin, key):
    return next(f for f in plugin.fields if f.key == key)


@pytest.mark.parametrize('given, kept', [('Infra', {'infra'}), (' INFRA ', {'infra'}), ('infra', {'infra'}), ('tag:Infra', {'infra'}), ('Business  Development, SEO', {'bizdev', 'seo'}),
                                         ('seo, SEO, tag:seo', {'seo'}), ('Product Team', {'product'})])
def test_a_route_takes_a_tag_by_id_or_label_in_any_case(store, given, kept):
    # given
    runtime = runtime_of(store, Transport(), teams_route('seo', SEO, tags=given))

    # when
    [found] = runtime.routes()

    # then
    assert found.tags == frozenset(kept)


def test_an_id_wins_over_a_label_that_spells_the_same(store, monkeypatch):
    # given
    monkeypatch.setitem(tags.TAGS, 'seo', 'Marketing')
    runtime = runtime_of(store, Transport(), teams_route('seo', SEO, tags='marketing'))

    # when
    [found] = runtime.routes()

    # then
    assert found.tags == frozenset({'marketing'})


def test_a_stored_route_with_an_unknown_tag_still_loads_without_it(store, caplog):
    # given
    runtime = runtime_of(store, Transport(), teams_route('seo', SEO, tags='seo, Nonsense'))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        [found] = runtime.routes()
    parsed = route_of(runtime.registry.get('teams').plugin, runtime.instances.get('teams', 'seo'))

    # then
    assert found.tags == frozenset({'seo'}) and parsed.unknown == ('nonsense',)
    assert 'route seo takes nonsense, which no card can carry' in caplog.text


def test_a_stored_route_with_only_unknown_tags_is_skipped_not_fatal(store):
    # given
    runtime = runtime_of(store, Transport(), teams_route('seo', SEO, tags='Nonsense'))

    # when
    found = runtime.routes()

    # then
    assert found == []


@pytest.mark.parametrize('plugin', [teams.PLUGIN, email_route.PLUGIN])
def test_the_tags_field_of_a_route_plugin_checks_and_suggests_tags(plugin):
    # given
    f = field_of(plugin, 'tags')

    # when
    problem = f.check('Infra, seo')
    refused = f.check('infra, Nonsense, other')

    # then
    assert problem is None and 'nonsense, other' not in (refused or '') and 'Nonsense, other' in refused and f.separator == ','


def test_a_route_with_an_unknown_tag_is_refused_on_save_with_a_422_naming_it(store):
    # given
    store.save({**state(), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(teams.PLUGIN), transport=Transport())) as c:
        values = {'webhook': SEO, 'tags': 'Infra, Nonsense'}
        bad = c.post('/api/admin/plugins/teams/instances', json={'name': 'ops', 'values': values, 'enabled': True}, headers=AUTH)
        good = c.post('/api/admin/plugins/teams/instances', json={'name': 'ops', 'values': {**values, 'tags': 'Infra, tag:Developer'}, 'enabled': True}, headers=AUTH)
        edit = c.patch('/api/admin/plugins/teams/instances/ops', json={'values': {'tags': 'Unknown-one'}}, headers=AUTH)

    # then
    assert bad.status_code == 422 and bad.json()['field'] == 'tags' and 'Nonsense' in bad.json()['detail'] and 'Infra' not in bad.json()['detail'].split(':')[0]
    assert good.status_code == 201
    assert edit.status_code == 422 and 'Unknown-one' in edit.text


def test_the_suggestions_are_the_tag_ids_including_a_plug_in_tag():
    # given
    tags.use_tags({'ops-team': 'Operations'})
    ctx = SuggestContext(values={}, transport=None)

    # when
    found = field_of(teams.PLUGIN, 'tags').suggest(ctx)

    # then
    assert 'infra' in found and 'developer' in found and 'ops-team' in found and 'Operations' not in found


def test_a_failing_plug_in_tag_hook_does_not_refuse_a_tag_on_save():
    # given
    def boom():
        raise RuntimeError('hook down')

    tags.use_tags(boom)

    # when
    problem = tags.terms_problem('ops-team, Infra')

    # then
    assert problem is None


def test_a_term_naming_two_tags_is_refused_with_their_ids_and_an_id_still_wins(monkeypatch):
    # given
    tags.use_tags({'ops-a': 'Shared Team', 'ops-b': ' shared  team '})
    monkeypatch.setitem(tags.TAGS, 'seo', 'Marketing')

    # when
    vague = tags.terms_problem('Shared Team')
    by_id = tags.terms_problem('ops-a, marketing')
    resolved = [tags.resolve('shared team'), tags.resolve('ops-b'), tags.resolve('marketing')]

    # then
    assert vague is not None and 'ops-a, ops-b' in vague and 'Shared Team' in vague
    assert by_id is None
    assert resolved == [None, 'ops-b', 'marketing']


def test_an_ambiguous_term_in_a_stored_route_is_dropped_not_fatal(store):
    # given
    tags.use_tags({'ops-a': 'Shared', 'ops-b': 'Shared'})
    runtime = runtime_of(store, Transport(), teams_route('seo', SEO, tags='shared, seo'))

    # when
    [found] = runtime.routes()

    # then
    assert found.tags == frozenset({'seo'})


def test_the_list_of_known_ids_in_the_refusal_is_capped():
    # given
    tags.use_tags({f'team-{n:03d}': f'Team {n}' for n in range(200)})

    # when
    problem = tags.terms_problem('x' * 500)

    # then
    assert problem is not None and len(problem) < 2 * tags.SHOWN_MAX + 200


def test_a_stored_stale_tag_blocks_only_an_edit_that_sends_the_tags_and_the_refusal_names_it(store):
    # given
    store.save({**state(), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(teams.PLUGIN), transport=Transport())) as c:
        made = c.post('/api/admin/plugins/teams/instances', json={'name': 'ops', 'values': {'webhook': SEO, 'tags': 'infra'}, 'enabled': True}, headers=AUTH)
        store.db['instances'].update_one({'_id': 'teams/ops'}, {'$set': {'values.tags': 'infra, Retired Team'}})
        unrelated = c.patch('/api/admin/plugins/teams/instances/ops', json={'values': {'mentions': 'alice@example.com'}}, headers=AUTH)
        resent = c.patch('/api/admin/plugins/teams/instances/ops', json={'values': {'tags': 'infra, Retired Team', 'mentions': 'bob@example.com'}}, headers=AUTH)
        fixed = c.patch('/api/admin/plugins/teams/instances/ops', json={'values': {'tags': 'infra'}}, headers=AUTH)

    # then
    assert made.status_code == 201 and unrelated.status_code == 200
    assert resent.status_code == 422 and resent.json()['field'] == 'tags' and 'Retired Team' in resent.json()['detail']
    assert fixed.status_code == 200
