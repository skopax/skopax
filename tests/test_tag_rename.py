from skopax import tags
from skopax.store import Store

AT = '2026-11-11T07:00:00Z'


def test_the_tech_tag_is_gone_developer_and_data_are_technical_tags_and_an_old_tag_tech_route_means_developer():
    # then
    assert 'tech' not in tags.TAGS and (tags.TAGS['developer'], tags.TAGS['data']) == ('Developer', 'Data')
    assert tags.TECHNICAL == ('developer', 'data', 'infra', 'analyst')


def test_the_data_tag_rule_catches_data_cluster_alerts_and_airbyte_cards_only():
    # given
    cards = [{'key': 'alert|KubePodNotReady|cluster=east-data-prometheus,namespace=warehouse', 'src': 'grafana', 'sig': 'alert'},
             {'key': 'airbyte|Mailer Users → S3', 'src': 'airbyte', 'sig': 'sync-failed'},
             {'key': 'alert|KubePodNotReady|cluster=east-generic-prometheus,namespace=reports', 'src': 'grafana', 'sig': 'alert'},
             {'key': 'log|checkout-service|ERROR|boom', 'src': 'logs', 'sig': 'log'}]

    # when
    tags.apply_rules(cards, tags.RULES, AT)

    # then
    assert [tags.ids(c) for c in cards] == [['data'], ['data'], [], []]


def test_old_tech_tags_on_cards_move_to_developer_once(store: Store):
    # given
    store.anomalies.insert_many([
        {'key': 'a', 'pos': 0, 'tags': [{'id': 'tech', 'by': 'pod', 'at': AT}, {'id': 'seo', 'by': 'rule', 'at': AT}], 'tagsOff': []},
        {'key': 'b', 'pos': 1, 'tags': [{'id': 'tech', 'by': 'pod', 'at': AT}, {'id': 'developer', 'by': 'nina.white@example.com', 'at': AT}], 'tagsOff': ['tech']},
        {'key': 'c', 'pos': 2, 'tags': [{'id': 'infra', 'by': 'pod', 'at': AT}]}])

    # when
    moved = store.rename_tags({'tech': 'developer'})
    again = store.rename_tags({'tech': 'developer'})
    cards = {c['key']: c for c in store.anomalies.find({})}

    # then
    assert (moved, again) == (2, 0)
    assert [t['id'] for t in cards['a']['tags']] == ['developer', 'seo'] and cards['a']['tags'][0]['by'] == 'pod'
    assert [t['id'] for t in cards['b']['tags']] == ['developer'] and cards['b']['tagsOff'] == ['developer']
    assert [t['id'] for t in cards['c']['tags']] == ['infra']
