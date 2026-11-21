from skopax.investigator.tools import Effects, Toolbox
from skopax.store import Store
from tests.conftest import anomaly, state
from tests.test_investigator_tools import KEY, LINK, NOW, seeded, sources


def toolbox(store: Store, ask: bool = False) -> Toolbox:
    effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {})
    return Toolbox(sources(), store, effects, [KEY], 'live', NOW, 'https://skopax.example.com', **({'ask': True, 'asker': 'alice@example.com'} if ask else {}))


def test_an_update_on_a_card_that_was_never_posted_says_to_use_slack_draft(store: Store):
    # given
    b = toolbox(seeded(store))

    # when
    text, failed = b.call('slack_update', {'key': KEY, 'text': 'Update: fixed.'})

    # then
    assert failed and 'slack_draft' in text and 'never posted' in text, text


def test_an_update_right_after_this_runs_draft_says_the_post_is_on_its_way(store: Store):
    # given
    b = toolbox(seeded(store))
    b.call('slack_draft', {'key': KEY, 'text': 'x ' + LINK})

    # when
    text, failed = b.call('slack_update', {'key': KEY, 'text': 'Update: fixed.'})

    # then
    assert failed and 'on its way' in text and 'never posted' not in text, text


def test_an_update_on_a_card_merged_into_an_open_parent_points_at_the_parent(store: Store):
    # given
    parent = 'log|b|ERROR|y'
    store.save_state(state(anomaly(KEY, parent=parent), anomaly(parent)), None)
    b = toolbox(store)

    # when
    text, failed = b.call('slack_update', {'key': KEY, 'text': 'Update: fixed.'})

    # then
    assert failed and parent in text and 'merged' in text, text


def test_a_page_ask_update_on_an_unposted_card_says_there_is_nothing_to_update(store: Store):
    # given
    b = toolbox(seeded(store), ask=True)

    # when
    text, failed = b.call('slack_update', {'key': KEY, 'text': 'Update: fixed.'})

    # then
    assert failed and 'nothing to update' in text and 'slack_draft' not in text, text


def test_the_tool_descriptions_say_a_card_without_a_post_has_no_thread(store: Store):
    # given
    b = toolbox(seeded(store, slackPost={'ts': '1.2', 'at': '2026-10-10T08:00:00Z', 'channel': 'C1', 'by': 'auto'}))

    # when
    described = {d['name']: d['description'] for d in b.definitions()}

    # then
    assert 'no thread' in described['slack_update'] and 'slack_draft' in described['slack_update']
    assert 'never slack_update' in described['slack_draft'] or 'not slack_update' in described['slack_draft']


def test_the_post_on_its_way_follows_the_auto_send_state():
    # given
    way = Toolbox._post_on_its_way

    # then
    assert way({'slackDraft': 'x', 'autoSend': {'at': '2026-10-21T09:00:00Z'}})
    assert not way({'slackDraft': 'x', 'autoSend': {'at': '2026-10-21T09:00:00Z', 'failedAt': 'a', 'claimedAt': 'b'}})
    assert way({'autoSend': {'holdBy': 'alice@example.com'}})
    assert not way({'autoSend': {'at': '2026-10-21T09:00:00Z'}})
    assert not way({})
