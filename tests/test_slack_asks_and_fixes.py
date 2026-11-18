from tests.conftest import anomaly, state
from tests.fake_anthropic import verdict
from tests.test_assign import NINA, PAUL, assigner
from tests.test_slack import FakeSlack
from tests.test_slack_clicks import clicks, payload
from tests.test_slack_images import SHOT
from tests.test_slackevents import JIRA_LINK, KEY, TOP_TS, card_with_post, intake, reply_event, top_event
from skopax.slack import FIX_DIFF_LIMIT, fix_message, task_confirm
from skopax.slackevents import IMAGE_ONLY
from skopax.store import Store

POM = '\n'.join(f'<dependency><groupId>com.example&amp;co</groupId><artifactId>lib-{i}</artifactId><version>1.{i}</version></dependency>' for i in range(30))


def fenced(message: dict) -> list[str]:
    return [b['text']['text'] for b in message['blocks'] if b['type'] == 'section' and b['text']['text'].startswith('```')]


def test_a_markup_heavy_suggested_fix_stays_inside_slack_s_section_limit_and_never_splits_an_entity():
    # given
    fix = {'repo': 'checkout-service', 'path': 'pom.xml', 'lines': '10-40', 'url': 'https://gitlab.com/x', 'current': POM, 'suggestion': POM.replace('1.', '2.')}

    # when
    [diff] = fenced(fix_message([fix], 'https://skopax.example.com/c/4'))

    # then
    assert len(diff) <= FIX_DIFF_LIMIT + 6 < 3000
    assert diff.endswith('…```') and not diff[:-4].rsplit('&', 1)[-1].isalpha()
    assert diff.startswith('```- &lt;dependency&gt;&lt;groupId&gt;com.example&amp;amp;co&lt;/groupId&gt;')


def test_a_short_suggested_fix_is_shown_whole():
    # given
    fix = {'repo': 'r', 'path': 'a.kt', 'lines': '3', 'url': 'https://gitlab.com/x', 'current': 'if (a < b && c > d)', 'suggestion': 'if (a <= b && c > d)'}

    # when
    [diff] = fenced(fix_message([fix], 'https://skopax.example.com/c/4'))

    # then
    assert diff == '```- if (a &lt; b &amp;&amp; c &gt; d)\n+ if (a &lt;= b &amp;&amp; c &gt; d)```'


def test_the_create_task_confirm_names_no_project_since_the_default_project_is_a_setting():
    # when
    plain, tech = task_confirm('Checkout null guard', {'project': None}), task_confirm('Cache: /checkout', {'project': 'INFRA', 'tech': True})
    other = task_confirm('Checkout null guard', {'project': 'WEB'})

    # then
    assert plain['title']['text'] == 'Create task?' and 'OPS' not in str(plain)
    assert tech['title']['text'] == 'Create INFRA task?' and 'assigned to Infra' in tech['text']['text']
    assert other['title']['text'] == 'Create WEB task?' and 'assigned to Infra' not in other['text']['text']


def test_an_ask_that_names_a_jira_key_still_opens_its_task_draft_on_the_create_task_click(store: Store):
    # given
    card_with_post(store)
    box, _ = intake(store, gate_replies=[verdict(addressed=True, on_topic=True)])
    box.handle(reply_event(f'<@UBOT> open a task for this, under {JIRA_LINK}'))
    ask = store.list_actions('requested', 1)[0]
    store.set_action_fields(ask['id'], {'ticketProposal': {'key': KEY, 'title': 'checkout-service null guard', 'description': 'NPE at line 69', 'points': 3}})
    handler, applier, api = clicks(store)

    # when
    got = handler.handle(payload(ask['id']))

    # then
    done = store.get_action(ask['id'])
    assert got == 'created' and [c['type'] for c in applier.calls] == ['ticket']
    assert done['ticket']['key'] == 'OPS-140' and done['mentionedTicket'] == 'INFRA-402'


def test_a_top_level_tag_whose_event_carries_no_files_reads_the_message_for_its_image(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])
    f.api.thread = [{'ts': TOP_TS, 'user': 'U1', 'text': '<@UBOT>', 'files': [SHOT]}]

    # when
    got = box.handle(top_event('<@UBOT>'))

    # then
    ask = store.list_actions('requested', 1)[0]
    assert got == 'accepted' and ask['text'] == IMAGE_ONLY and [i['name'] for i in ask['images']] == ['error.png']
    assert [p['ts'] for m, p in f.api.calls if m == 'conversations.replies'] == [TOP_TS]


def test_a_typed_name_gets_a_message_only_when_the_guessed_slack_account_is_that_person(store: Store):
    # given
    t = FakeSlack()
    store.save({**state(anomaly('log|b|ERROR|one'), anomaly('log|b|ERROR|two'), anomaly('log|b|ERROR|three')), 'initialized': True}, None)
    a = assigner(store, t, {'U1': 'adam@example.com', 'U2': NINA})
    a.slack.api.names.update({'U1': 'Adam Cross', 'U2': 'Alice Smith'})

    # when
    first_name = a.assign('log|b|ERROR|one', 'Adam', None, PAUL)
    other = a.assign('log|b|ERROR|two', 'Adam Baker', 'adam@example.com', PAUL)
    full = a.assign('log|b|ERROR|three', 'Alice Smith', None, PAUL)

    # then
    assert (first_name['dm'], other['dm'], full['dm']) == ('none', 'sent', 'sent'), 'a picked address is trusted; a guess must match the Slack name'
    assert [p['channel'] for _, p in t.calls] == ['U1', 'U2']
    assert store.anomaly('log|b|ERROR|one')['owner'] == 'Adam', 'the card is still assigned; only the message is held back'
