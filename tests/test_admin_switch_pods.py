from contextlib import ExitStack

from skopax.messages import msg
from tests.conftest import AUTH
from tests.test_admin_plugins import add, listed, served


def test_a_plugin_switch_made_on_one_pod_shows_at_once_on_another(store):
    # given
    with ExitStack() as pods:
        first = pods.enter_context(served(store))
        second = pods.enter_context(served(store))
        assert listed(second)['demo']['enabled'] is True

        # when
        off = first.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH)
        seen_off = listed(second)['demo']
        on = first.put('/api/admin/plugins/demo', json={'enabled': True}, headers=AUTH)
        seen_on = listed(second)['demo']

        # then
        assert (off.status_code, on.status_code) == (200, 200)
        assert seen_off['enabled'] is False and seen_on['enabled'] is True


def test_an_instance_switch_made_on_one_pod_shows_at_once_on_another(store):
    # given
    with ExitStack() as pods:
        first = pods.enter_context(served(store))
        second = pods.enter_context(served(store))
        assert add(first, 'main', enabled=True).status_code == 201
        assert listed(second)['demo']['instances'][0]['enabled'] is True

        # when
        first.patch('/api/admin/plugins/demo/instances/main', json={'enabled': False}, headers=AUTH)
        seen = listed(second)['demo']['instances'][0]['enabled']

        # then
        assert seen is False


def test_the_reader_list_follows_a_switch_made_on_another_pod(store):
    # given
    with ExitStack() as pods:
        first = pods.enter_context(served(store))
        second = pods.enter_context(served(store))
        before = {p['id']: p for p in second.get('/api/integrations', headers=AUTH).json()['plugins']}
        assert before['demo']['enabled'] is True

        # when
        first.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH)
        after = {p['id']: p for p in second.get('/api/integrations', headers=AUTH).json()['plugins']}

        # then
        assert after['demo']['enabled'] is False


def test_a_setting_saved_on_one_pod_shows_at_once_on_another(store):
    # given
    with ExitStack() as pods:
        first = pods.enter_context(served(store))
        second = pods.enter_context(served(store))
        items = second.get('/api/admin/settings', headers=AUTH).json()['items']
        key = next(i['key'] for i in items if i['key'] == 'core.language')
        before = next(i['value'] for i in items if i['key'] == key)

        # when
        saved = first.put('/api/admin/settings', json={'key': key, 'value': 'de' if before != 'de' else 'en'}, headers=AUTH)
        seen = next(i['value'] for i in second.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == key)

        # then
        assert saved.status_code == 200 and seen == saved.json()['item']['value'] and seen != before


def test_a_feature_switched_on_one_pod_shows_at_once_on_another(store):
    # given
    with ExitStack() as pods:
        first = pods.enter_context(served(store))
        second = pods.enter_context(served(store))
        before = {i['id']: i for i in second.get('/api/admin/features', headers=AUTH).json()['items']}['chat']['stored']

        # when
        saved = first.put('/api/admin/features/chat', json={'on': not before}, headers=AUTH)
        seen = {i['id']: i for i in second.get('/api/admin/features', headers=AUTH).json()['items']}['chat']['stored']

        # then
        assert saved.status_code == 200 and seen is (not before)


def test_a_switch_the_other_pod_did_not_do_still_logs_the_change_it_makes(store):
    # given
    with ExitStack() as pods:
        first = pods.enter_context(served(store))
        second = pods.enter_context(served(store))
        listed(second)
        first.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH)

        # when
        again = second.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH)
        back = second.put('/api/admin/plugins/demo', json={'enabled': True}, headers=AUTH)

        # then
        lines = [d['text'] for d in store.activity.find()]
        assert (again.status_code, back.status_code) == (200, 200)
        name = listed(second)['demo']['name']
        assert lines.count(msg('integrations.plugin_off', plugin=name)) == 1
        assert lines.count(msg('integrations.plugin_on', plugin=name)) == 1


def test_a_refused_switch_leaves_the_state_as_it_was(store):
    # given
    with served(store) as c:
        before = listed(c)['demo']['enabled']

        # when
        refused = c.put('/api/admin/plugins/not-a-plugin', json={'enabled': True}, headers=AUTH)

        # then
        assert refused.status_code >= 400 and listed(c)['demo']['enabled'] is before


def test_the_plugin_row_and_instance_carry_the_time_of_the_last_switch(store):
    # given
    with served(store) as c:
        assert add(c, 'main', enabled=True).status_code == 201

        # when
        row = c.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH).json()
        done = c.patch('/api/admin/plugins/demo/instances/main', json={'enabled': False}, headers=AUTH).json()

        # then
        assert row['switchedAt'] and done['instance']['updatedAt']
        assert listed(c)['demo']['switchedAt'] == row['switchedAt']
