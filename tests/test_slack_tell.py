import dataclasses

import pytest
from fastapi.testclient import TestClient

from tests.conftest import anomaly, settings, state
from tests.fake_slack import fake_slack
from tests.test_investigator_tools import KEY, NOW, box, seeded, sources
from skopax.app import create_app
from skopax.engine import iso
from skopax.errkind import Refusal
from skopax.investigator.tools import Effects, Toolbox
from skopax.slack import answer_blocks
from skopax.store import Store

ASKER = 'oscar.green@example.com'
LINK = 'https://example.slack.com/archives/C1/p1792224000000900'


def tell_box(store: Store, told: list, mode: str = 'live', asker: str | None = ASKER, read_only: bool = False) -> Toolbox:
    def tell(key, email, text, by, name):
        if email == 'adam.baker@example.com':
            raise Refusal('adam.baker@example.com belongs to Adam White in Slack, not Adam Baker; ask the asker who they meant')
        told.append((key, email, text, by, name))
        return {'slack': {'permalink': LINK}, 'result': 'told Gary Cole in the card thread'}
    effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, tell=tell)
    return Toolbox(sources(), store, effects, [KEY], mode, NOW, 'https://skopax.example.com', ask=True, asker=asker, read_only=read_only,
                   settings=dataclasses.replace(settings(), email_domains=('example.com',)))


def test_a_page_ask_can_tell_a_person_on_its_card_or_in_a_new_thread(store: Store):
    # given
    seeded(store)
    told = []
    b = tell_box(store, told)

    # when
    wrong = b.call('slack_tell', {'name': 'Adam Baker', 'email': 'adam.baker@example.com', 'text': 'Look.'})
    on_card = b.call('slack_tell', {'name': 'Gary Cole', 'email': 'gary.cole@example.com', 'text': 'Can you look at the cert alert on this card?', 'key': KEY})
    in_channel = b.call('slack_tell', {'name': 'Gary Cole', 'email': 'gary.cole@example.com', 'text': 'Let us talk tomorrow at 10:00.'})
    third = b.call('slack_tell', {'name': 'Gary Cole', 'email': 'gary.cole@example.com', 'text': 'Once more.'})

    # then
    assert 'slack_tell' in [d['name'] for d in b.definitions()]
    assert wrong == ('adam.baker@example.com belongs to Adam White in Slack, not Adam Baker; ask the asker who they meant', True)
    assert not on_card[1] and on_card[0] == f'slack_tell saved on {KEY}; told Gary Cole in the card thread; {LINK}'
    assert not in_channel[1] and in_channel[0].startswith('slack_tell saved on channel')
    assert told == [(KEY, 'gary.cole@example.com', 'Can you look at the cert alert on this card?', ASKER, 'Gary Cole'),
                    (None, 'gary.cole@example.com', 'Let us talk tomorrow at 10:00.', ASKER, 'Gary Cole')]
    assert third == ('slack_tell already posted 2 messages in this ask', True)


@pytest.mark.parametrize('args, error', [
    ({'name': 'Gary', 'email': 'gary.cole@example.com', 'text': 'x'}, 'name needs a first and a last name'),
    ({'name': 'Gary Cole', 'email': 'Gary', 'text': 'x'}, 'email must be a work address'),
    ({'name': 'Gary Cole', 'email': 'gary@example.org', 'text': 'x'}, 'email must be a work address'),
    ({'name': 'Gary Cole', 'email': 'gary.cole@example.com', 'text': '<@U1> look'}, 'no Slack mentions'),
    ({'name': 'Gary Cole', 'email': 'gary.cole@example.com', 'text': 'look', 'key': 'log|other|ERROR|y'}, 'is not one of the cards this run investigates'),
])
def test_slack_tell_refuses_a_bad_address_a_mention_or_another_card(store: Store, args, error):
    # given
    seeded(store)
    told = []

    # when
    out, failed = tell_box(store, told).call('slack_tell', args)

    # then
    assert failed and error in out and told == []


def test_slack_tell_is_offered_to_page_asks_only_and_posts_nothing_in_shadow_mode(store: Store):
    # given
    seeded(store)
    told = []

    # when
    shadow = tell_box(store, told, mode='shadow').call('slack_tell', {'name': 'Gary Cole', 'email': 'gary.cole@example.com', 'text': 'Look.'})
    slack_ask = tell_box(store, told, asker=None, read_only=True)
    pod = box(store)

    # then
    assert not shadow[1] and 'shadow mode' in shadow[0] and told == []
    assert 'slack_tell' not in [d['name'] for d in slack_ask.definitions()] and 'slack_tell' not in [d['name'] for d in pod.definitions()]
    assert pod.call('slack_tell', {'name': 'Gary Cole', 'email': 'gary.cole@example.com', 'text': 'Look.'})[1]


def test_the_app_mentions_the_person_in_the_card_thread_posts_the_card_first_or_starts_a_thread(store: Store):
    # given
    posted, fresh = 'log|posted|ERROR|x', 'log|fresh|ERROR|y'
    slack, post, api = fake_slack(emails={'U7': 'renee.francois@example.com', 'U8': 'adam.baker@example.com'})
    api.names = {'U7': 'Ren\u00e9e FRAN\u00c7OIS', 'U8': 'Adam White'}
    store.save(state(anomaly(posted), anomaly(fresh)), None)
    store.slack_posted(posted, {'channel': 'C1', 'ts': '1792224000.000100', 'at': iso(NOW), 'by': 'server'})

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        tell = c.app.state.slack_tell
        tell(posted, 'renee.francois@example.com', 'Can you take a look? <b>', ASKER, 'Ren\u00e9e Fran\u00e7ois')
        tell(fresh, 'renee.francois@example.com', 'This one is new too.', ASKER, 'renee francois')
        done = tell(None, 'renee.francois@example.com', 'A general note.', ASKER, 'Ren\u00e9e Fran\u00e7ois')
        with pytest.raises(ValueError, match='no Slack user has the email nobody@example.com'):
            tell(None, 'nobody@example.com', 'x', ASKER, 'No Body')
        with pytest.raises(ValueError, match='adam.baker@example.com belongs to Adam White in Slack, not Adam Baker'):
            tell(None, 'adam.baker@example.com', 'x', ASKER, 'Adam Baker')

    # then
    replies = [p for p in post.posted if p.get('text', '').startswith('<@U7>')]
    assert [(p.get('thread_ts'), p['text']) for p in replies] == [
        ('1792224000.000100', '<@U7> Can you take a look? &lt;b&gt; (asked by oscar.green)'),
        (store.anomaly(fresh)['slackPost']['ts'], '<@U7> This one is new too. (asked by oscar.green)'),
        (None, '<@U7> A general note. (asked by oscar.green)')]
    assert replies[0]['channel'] == 'C1' and replies[2]['channel'] == '#ops'
    assert (done['type'], done['status'], done['requestedBy'], done['result']) == ('slack-tell', 'done', ASKER, 'told Ren\u00e9e FRAN\u00c7OIS in a new #ops thread')
    assert len(replies) == 3


def test_a_tech_ticket_draft_drops_points_and_its_button_says_tech(store: Store):
    # given
    seeded(store)
    infra = dataclasses.replace(settings(), jira_project='OPS', jira_projects=('OPS',), jira_tech_project='INFRA')
    b = Toolbox(sources(), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), [KEY], 'live', NOW, 'https://skopax.example.com', ask=True, read_only=True, settings=infra)

    # when
    saved = b.call('propose_ticket', {'title': 'Cache: /checkout', 'description': 'Add it to the list.', 'points': 5, 'project': 'INFRA'})
    blocks = answer_blocks('Answer.', b.proposal, 'a1')

    # then
    assert not saved[1] and b.proposal['project'] == 'INFRA' and b.proposal['points'] is None and b.proposal['tech'] is True
    assert blocks[1]['text']['text'].startswith('*INFRA task draft:* Cache: /checkout')
    assert blocks[-1]['elements'][0]['text']['text'] == 'Create INFRA task'
    other = answer_blocks('Answer.', {'title': 'Fix it', 'description': '', 'project': 'WEB'}, 'a2')
    assert other[1]['text']['text'].startswith('*WEB task draft:* Fix it') and other[-1]['elements'][0]['text']['text'] == 'Create WEB task'
    plain = answer_blocks('Answer.', {'title': 'Fix it', 'description': '', 'project': None}, 'a3')
    assert plain[1]['text']['text'].startswith('*Task draft:* Fix it') and plain[-1]['elements'][0]['text']['text'] == 'Create task'


def test_the_tell_line_posted_to_slack_is_english_and_names_who_asked(store: Store):
    # given
    slack, post, api = fake_slack(emails={'U7': 'gary.cole@example.com'})
    api.names = {'U7': 'Alice Smith'}

    # when
    with TestClient(create_app(settings(), store, slack=slack)) as c:
        c.app.state.slack_tell(None, 'gary.cole@example.com', 'Could you check this?', ASKER, 'Alice Smith')

    # then
    assert [p['text'] for p in post.posted if p.get('text', '').startswith('<@U7>')] == ['<@U7> Could you check this? (asked by oscar.green)']
