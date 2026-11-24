import time
import dataclasses

import threading

from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, jira_links, settings, state
from tests.fake_slack import FakeSlackApi
from tests.test_engine import item, stored
from tests.test_jiralinks import FakeJira, issue
from tests.test_slack import FakeSlack
from skopax import engine, locales, messages, tagreport
from skopax.app import create_app
from skopax.assign import Assigner, slug
from skopax.investigator import prompt
from skopax.kube import Kube
from skopax.slack import Slack
from skopax.statenotes import StateNotes
from skopax.store import Store

KEY = 'log|checkout-service|ERROR|boom'
NINA = 'alice.smith@example.com'
PAUL = 'paul.stone@example.com'
BASE = 'https://skopax.example.com'


class People:
    enabled = True

    def __init__(self, found: list[dict] | None = None, fail: bool = False):
        self.found = found or []
        self.fail = fail
        self.asked: list[str] = []

    def people(self, query: str, limit: int = 8) -> list[dict]:
        self.asked.append(query)
        if self.fail:
            raise RuntimeError('jira is down')
        return self.found[:limit]


def assigner(store: Store, transport: FakeSlack, emails: dict | None = None, **over) -> Assigner:
    return Assigner(store, Slack('xoxb-test', '#ops', transport, FakeSlackApi(emails or {})), dataclasses.replace(settings(), **over), BASE)


def test_a_name_or_an_email_becomes_the_same_plain_handle():
    # then
    assert slug('Ren\u00e9e M\u00fcller') == 'renee.muller' and slug('  \u00c9lodie Garc\u00eda MU\u00d1OZ ') == 'elodie.garcia.munoz'
    assert slug(PAUL) == 'paul.stone' and slug('') == ''


def test_an_operator_assigns_a_card_without_changing_its_status_and_the_person_gets_one_direct_message(store: Store):
    # given
    t = FakeSlack()
    store.save({**state(anomaly(KEY, num=14, svc='checkout-service', label='ERROR lines', what='NullPointerException in OrderService')), 'initialized': True}, None)
    a = assigner(store, t, {'U1': NINA})

    # when
    first = a.assign(KEY, 'Alice Smith', NINA, PAUL)
    again = a.assign(KEY, 'Alice Smith', NINA, PAUL)

    # then
    card = store.anomaly(KEY)
    assert card['owner'] == 'Alice Smith' and card['assigned'] == {'by': PAUL, 'at': card['assigned']['at'], 'email': NINA} and card.get('triage') is None
    assert (first['dm'], again['dm']) == ('sent', 'same') and first['owner'] == 'Alice Smith'
    [(_, post)] = t.calls
    assert post['channel'] == 'U1' and post['text'].startswith('paul.stone assigned you a Skopax card: *#14 · checkout-service / ERROR lines*')
    assert f'<{BASE}/c/14|Open card>' in post['text'] and 'NullPointerException in OrderService' in post['text']
    assert [x['text'] for x in store.list_activity(None, 20) if x['kind'] == 'assign'] == [f'assigned to Alice Smith · {KEY}'] * 2


def test_without_an_address_the_message_goes_to_the_name_s_address_on_the_operator_s_domain_and_a_miss_is_said(store: Store):
    # given
    t = FakeSlack()
    store.save({**state(anomaly(KEY), anomaly('log|a|ERROR|x')), 'initialized': True}, None)
    a = assigner(store, t, {'U1': NINA})
    a.slack.api.names['U1'] = 'Alice Smith'

    # when
    guessed = a.assign(KEY, 'Alice Smith', None, PAUL)
    unknown = a.assign('log|a|ERROR|x', 'Zoe Hart', None, PAUL)
    by_token = a.assign('log|a|ERROR|x', 'Alice Smith', None, 'token')

    # then
    assert (guessed['dm'], unknown['dm'], by_token['dm']) == ('sent', 'none', 'none')
    assert [p['channel'] for _, p in t.calls] == ['U1']
    assert store.anomaly('log|a|ERROR|x')['owner'] == 'Alice Smith' and 'email' not in store.anomaly('log|a|ERROR|x')['assigned']


def test_assigning_yourself_sends_your_own_dm_and_clearing_or_a_switched_off_or_failing_slack_still_save(store: Store):
    # given
    quiet, off, failing = FakeSlack(), FakeSlack(), FakeSlack(error='channel_not_found')
    store.save({**state(anomaly(KEY), anomaly('log|a|ERROR|x'), anomaly('log|a|ERROR|y')), 'initialized': True}, None)

    # when
    me = assigner(store, quiet, {'U1': NINA}).assign(KEY, NINA, NINA, NINA)
    cleared = assigner(store, quiet, {'U1': NINA}).assign(KEY, None, None, PAUL)
    muted = assigner(store, off, {'U1': NINA}, assign_dm=False).assign('log|a|ERROR|x', 'Alice Smith', NINA, PAUL)
    failed = assigner(store, failing, {'U1': NINA}).assign('log|a|ERROR|y', 'Alice Smith', NINA, PAUL)

    # then
    assert (me['dm'], cleared['dm'], muted['dm'], failed['dm']) == ('sent', None, 'off', 'failed')
    assert [p['channel'] for _, p in quiet.calls] == ['U1'] and quiet.calls[0][1]['text'].startswith('You assigned this Skopax card to yourself: *')
    assert off.calls == []
    card = store.anomaly(KEY)
    assert 'owner' not in card and 'assigned' not in card
    assert store.anomaly('log|a|ERROR|y')['owner'] == 'Alice Smith'
    assert [x['text'] for x in store.list_activity(None, 20) if x['kind'] == 'assign' and x.get('key') == KEY][:1] == [f'assignee removed · {KEY}']


def test_the_assignee_endpoint_sets_me_a_person_and_nobody_and_refuses_bad_input(store: Store, tmp_path):
    # given
    store.save({**state(anomaly(KEY, triage='dived')), 'initialized': True}, None)
    s = settings()

    # when
    with TestClient(create_app(s, store, kube=Kube(root=tmp_path), slack=Slack('xoxb-test', '#ops', FakeSlack(), FakeSlackApi({'U1': NINA})))) as c:
        named = c.put('/api/assignee', json={'key': KEY, 'name': '  Alice   Smith ', 'email': NINA}, headers=AUTH)
        mine = c.put('/api/assignee', json={'key': KEY, 'me': True}, headers=AUTH)
        nobody = c.put('/api/assignee', json={'key': KEY, 'name': ''}, headers=AUTH)
        bad_mail = c.put('/api/assignee', json={'key': KEY, 'name': 'Alice Smith', 'email': 'not an address'}, headers=AUTH)
        missing = c.put('/api/assignee', json={'key': 'log|x|ERROR|y', 'name': 'Alice Smith'}, headers=AUTH)
        anonymous = c.put('/api/assignee', json={'key': KEY, 'name': 'Alice Smith'})
        state_now = c.get('/api/state').json()

    # then
    assert named.json()['owner'] == 'Alice Smith' and named.json()['assigned']['email'] == NINA and named.json()['dm'] == 'sent'
    assert mine.json()['owner'] == 'token:bootstrap' and mine.json()['dm'] == 'none', 'the token has no Slack account to tell'
    assert nobody.json()['owner'] is None and nobody.json()['assigned'] is None
    assert (bad_mail.status_code, missing.status_code, anonymous.status_code) == (422, 404, 401)
    assert state_now['anomalies'][0].get('triage') == 'dived' and 'owner' not in state_now['anomalies'][0]


def test_people_come_from_jira_for_the_picker_and_a_short_or_failed_search_says_so(store: Store, tmp_path):
    # given
    jira = People([{'accountId': 'a1', 'displayName': 'Alice Smith', 'emailAddress': NINA, 'active': True, 'accountType': 'atlassian'},
                   {'accountId': 'a2', 'displayName': 'Paul Stone', 'active': True, 'accountType': 'atlassian'},
                   {'accountId': 'a3', 'displayName': 'Automation for Jira', 'active': True, 'accountType': 'app'},
                   {'accountId': 'a4', 'displayName': 'Former Employee', 'active': False, 'accountType': 'atlassian'}])
    store.save({**state(anomaly(KEY)), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), jira=jira)) as c:
        found = c.get('/api/people', params={'q': 'ay'}, headers=AUTH)
        short = c.get('/api/people', params={'q': 'a'}, headers=AUTH)
        anonymous = c.get('/api/people', params={'q': 'ay'})
        jira.fail = True
        down = c.get('/api/people', params={'q': 'ay'}, headers=AUTH)

    # then
    assert found.json() == [{'name': 'Alice Smith', 'email': NINA}, {'name': 'Paul Stone', 'email': None}]
    assert short.json() == [] and jira.asked == ['ay', 'ay']
    assert (anonymous.status_code, down.status_code) == (401, 502)


def test_take_replaces_a_picked_assignee_and_a_jira_link_keeps_the_picked_one_but_still_links_the_ticket(store: Store):
    # given
    link = '[card](https://skopax.example.com/#a=log%7Ca%7CERROR%7Clinked)'
    store.save({**state(anomaly('log|a|ERROR|taken'), anomaly('log|a|ERROR|linked'), anomaly('log|a|ERROR|free')), 'initialized': True}, None)
    a = assigner(store, FakeSlack())
    a.assign('log|a|ERROR|taken', 'Alice Smith', None, 'token')
    a.assign('log|a|ERROR|linked', 'Alice Smith', None, 'token')
    free = '[card](https://skopax.example.com/#a=log%7Ca%7CERROR%7Cfree)'

    # when
    store.triage('log|a|ERROR|taken', 'progress', owner=PAUL)
    jira_links(store, FakeJira([issue('OPS-131', 'In Development', link, dev='Zoe Hart'), issue('OPS-132', 'In Development', free, dev='Zoe Hart')])).run_once()

    # then
    taken, linked, unpicked = store.anomaly('log|a|ERROR|taken'), store.anomaly('log|a|ERROR|linked'), store.anomaly('log|a|ERROR|free')
    assert taken['owner'] == PAUL and 'assigned' not in taken
    assert (linked['owner'], linked['ticket'], linked['triage']) == ('Alice Smith', 'OPS-131', 'progress') and linked['assigned']['by'] == 'token'
    assert (unpicked['owner'], unpicked['ticket']) == ('Zoe Hart', 'OPS-132')


def test_the_assignee_survives_a_pass_and_the_investigator_reads_it_on_the_card():
    # given
    picked = {'by': PAUL, 'at': '2026-11-10T10:00:00Z'}
    now = 1_794_310_000.0
    held = {'anomalies': [stored('cf-status|cdn', owner='Alice Smith', assigned=picked)], 'initialized': True, 'muted': []}

    # when
    engine.merge(held, [item('cf-status|cdn')], {'cf': True}, now)
    text = prompt.card_brief(held['anomalies'][0], BASE)

    # then
    after = held['anomalies'][0]
    assert after['owner'] == 'Alice Smith' and after['assigned'] == picked
    assert 'assignee: Alice Smith' in text


def test_a_change_of_assignee_is_noted_in_the_card_s_slack_thread_when_it_has_one(store: Store):
    # given
    t = FakeSlack()
    post = {'channel': 'C1', 'ts': '1794300000.000100', 'at': '2026-11-10T09:00:00Z', 'by': 'pod'}
    store.save({**state(anomaly(KEY, num=14, slackPost=post), anomaly('log|a|ERROR|quiet')), 'initialized': True}, None)
    slack = Slack('xoxb-test', '#ops', t, FakeSlackApi({'U1': NINA}))
    a = Assigner(store, slack, settings(), BASE, StateNotes(store, slack))

    # when
    first = a.assign(KEY, 'Alice Smith', NINA, PAUL)
    again = a.assign(KEY, 'Alice Smith', NINA, PAUL)
    cleared = a.assign(KEY, None, None, PAUL)
    no_thread = a.assign('log|a|ERROR|quiet', 'Alice Smith', NINA, PAUL)

    # then
    assert (first['thread'], again['thread'], cleared['thread'], no_thread['thread']) == (True, False, True, False)
    threaded = [p for _, p in t.calls if p.get('thread_ts')]
    assert [(p['channel'], p['thread_ts'], p['text']) for p in threaded] == [('C1', '1794300000.000100', '👤 Assigned: Alice Smith · assigned by paul.stone'),
                                                                             ('C1', '1794300000.000100', '👤 Unassigned: paul.stone')]
    assert store.anomaly(KEY)['slackPost']['stateNote']['state'] == 'assigned'


def test_slug_folds_letters_like_the_page():
    # then
    assert slug('\u0141ukasz \u00d8stergaard') == 'lukasz.ostergaard'
    assert slug('\u0141ukasz Stra\u00dfer') == 'lukasz.strasser'


POST = {'channel': 'C1', 'ts': '1794300000.000100', 'at': '2026-11-10T09:00:00Z', 'by': 'pod'}


def threaded_assign(store: Store, emails: dict, names: dict | None = None, ids: tagreport.SlackIds | None = None, fail: set | None = None):
    t = FakeSlack()
    store.save({**state(anomaly(KEY, num=14, slackPost=POST)), 'initialized': True}, None)
    api = FakeSlackApi(emails, fail=fail)
    api.names.update(names or {})
    slack = Slack('xoxb-test', '#ops', t, api)
    ids = ids or tagreport.SlackIds(slack)
    notes = StateNotes(store, slack, mention=ids.mention, name_of=ids.name)
    return Assigner(store, slack, settings(), BASE, notes), t, api, ids


def note_texts(t: FakeSlack) -> list[str]:
    return [p['text'] for _, p in t.calls if p.get('thread_ts')]


def test_the_assigned_note_mentions_the_assignee_and_keeps_the_assigner_plain(store: Store):
    # given
    a, t, _, _ = threaded_assign(store, {'U1': NINA}, {'U1': 'Alice Smith'})

    # when
    a.assign(KEY, 'Alice Smith', NINA, PAUL)

    # then
    assert note_texts(t) == ['\U0001F464 Assigned: <@U1> \u00b7 assigned by paul.stone']
    assert store.anomaly(KEY)['slackPost']['stateNote']['text'] == '\U0001F464 Assigned: Alice Smith \u00b7 assigned by paul.stone'


def test_the_assigned_note_names_the_person_when_slack_has_no_such_user(store: Store):
    # given
    a, t, _, _ = threaded_assign(store, {})

    # when
    a.assign(KEY, 'Zoe <Hart>', 'zoe.hart@example.com', PAUL)

    # then
    assert note_texts(t) == ['\U0001F464 Assigned: Zoe &lt;Hart&gt; \u00b7 assigned by paul.stone']


def test_the_assigned_note_uses_the_last_known_id_while_slack_is_down(store: Store):
    # given
    now = [0.0]
    t0 = FakeSlack()
    api = FakeSlackApi({'U1': NINA})
    ids = tagreport.SlackIds(Slack('xoxb-test', '#ops', t0, api), ttl=10, clock=lambda: now[0])
    assert ids.mention(NINA) == '<@U1>'
    a, t, _, _ = threaded_assign(store, {'U1': NINA}, ids=ids)
    api.fail.add('users.lookupByEmail')
    now[0] = 100.0

    # when
    a.assign(KEY, 'Alice Smith', NINA, PAUL)

    # then
    assert note_texts(t) == ['\U0001F464 Assigned: <@U1> \u00b7 assigned by paul.stone']


def test_a_lookup_failure_never_blocks_the_assigned_note(store: Store):
    # given
    a, t, _, _ = threaded_assign(store, {'U1': NINA}, fail={'users.lookupByEmail'})

    # when
    done = a.assign(KEY, 'Alice Smith', NINA, PAUL)

    # then
    assert done['thread'] is True and note_texts(t) == ['\U0001F464 Assigned: Alice Smith \u00b7 assigned by paul.stone']


def test_the_assigner_is_never_mentioned_even_when_slack_knows_them(store: Store):
    # given
    a, t, _, _ = threaded_assign(store, {'U1': NINA, 'U2': PAUL}, {'U1': 'Alice Smith', 'U2': 'Paul Stone'})

    # when
    a.assign(KEY, 'Alice Smith', NINA, PAUL)

    # then
    [text] = note_texts(t)
    assert '<@U2>' not in text and 'assigned by paul.stone' in text and '<@U1>' in text


def test_a_guessed_address_is_mentioned_only_when_the_slack_name_matches(store: Store):
    # given
    a, t, _, _ = threaded_assign(store, {'U1': NINA}, {'U1': 'Someone Else'})

    # when
    a.assign(KEY, 'Alice Smith', None, PAUL)

    # then
    assert note_texts(t) == ['\U0001F464 Assigned: Alice Smith \u00b7 assigned by paul.stone']


def test_the_unassigned_note_has_no_mention(store: Store):
    # given
    a, t, _, _ = threaded_assign(store, {'U1': NINA}, {'U1': 'Alice Smith'})
    a.assign(KEY, 'Alice Smith', NINA, PAUL)

    # when
    a.assign(KEY, None, None, PAUL)

    # then
    assert note_texts(t)[-1] == '\U0001F464 Unassigned: paul.stone'


def test_the_translated_assigned_note_keeps_working_with_the_mention(store: Store):
    # given
    pack = next(locale for locale in locales.packs() if locale.lang == 'tr')
    messages.set_catalogs([('tr', pack.messages)])
    a, t, _, _ = threaded_assign(store, {'U1': NINA}, {'U1': 'Alice Smith'})

    # when
    with messages.speaking('tr'):
        a.assign(KEY, 'Alice Smith', NINA, PAUL)

    # then
    assert note_texts(t) == [pack.messages['notes.state.assigned'].format(name='<@U1>') + pack.messages['notes.state.assigned_by'].format(who='paul.stone')]


def named_assign(store: Store, members: list[dict], emails: dict, by: str = PAUL, email: str | None = None, name: str = 'Alice Smith', names: dict | None = None):
    t = FakeSlack()
    store.save({**state(anomaly(KEY, num=14, slackPost=POST)), 'initialized': True}, None)
    api = FakeSlackApi(emails)
    api.names.update(names or {})
    api.members = members
    slack = Slack('xoxb-test', '#ops', t, api)
    ids = tagreport.SlackIds(slack)
    people = tagreport.SlackPeople(slack, spawn=lambda job: job())
    notes = StateNotes(store, slack, mention=ids.mention, name_of=ids.name, by_name=tagreport.name_mentions(people, ids))
    a = Assigner(store, slack, settings(), BASE, notes, by_name=people.email_of)
    return a.assign(KEY, name, email, by), t


def slack_member(uid: str, email: str, real: str) -> dict:
    return {'id': uid, 'name': email.split('@')[0], 'profile': {'email': email, 'real_name': real, 'display_name': ''}}


def test_a_guessed_address_that_misses_falls_back_to_the_one_slack_member_with_that_name_for_the_note_and_the_message(store: Store):
    # given
    members = [slack_member('U1', 'a.smith@corp.example.com', 'Alice Smith'), slack_member('U2', 'bob@corp.example.com', 'Bob Reed')]

    # when
    out, t = named_assign(store, members, {'U1': 'a.smith@corp.example.com', 'U2': 'bob@corp.example.com'})

    # then
    assert out['dm'] == 'sent'
    assert [p['channel'] for _, p in t.calls if not p.get('thread_ts')] == ['U1']
    assert note_texts(t) == ['\U0001F464 Assigned: <@U1> · assigned by paul.stone']


def test_a_guessed_address_that_misses_with_two_members_of_that_name_mentions_no_one_and_warns_once_without_the_name(store: Store, caplog):
    # given
    members = [slack_member('U1', 'a.smith@corp.example.com', 'Alice Smith'), slack_member('U2', 'alice@other.example.com', 'alice smith')]
    emails = {'U1': 'a.smith@corp.example.com', 'U2': 'alice@other.example.com'}

    # when
    with caplog.at_level('WARNING'):
        out, t = named_assign(store, members, emails)

    # then
    assert out['dm'] == 'none' and note_texts(t) == ['\U0001F464 Assigned: Alice Smith · assigned by paul.stone']
    said = [r.getMessage() for r in caplog.records if r.name == 'skopax.tagreport']
    assert len(said) == 1 and 'Alice' not in said[0] and 'Smith' not in said[0] and '11 characters' in said[0] and 'example.com' not in said[0]


def test_a_guessed_address_that_resolves_to_another_person_is_never_replaced_by_the_name_match(store: Store):
    # given
    members = [slack_member('U1', 'alice.smith@example.com', 'Alice Jones'), slack_member('U2', 'a.smith@corp.example.com', 'Alice Smith')]
    emails = {'U1': 'alice.smith@example.com', 'U2': 'a.smith@corp.example.com'}

    # when
    out, t = named_assign(store, members, emails, names={'U1': 'Alice Jones'})

    # then
    assert out['dm'] == 'none' and note_texts(t) == ['\U0001F464 Assigned: Alice Smith · assigned by paul.stone']


def test_a_given_address_that_misses_does_not_fall_back_to_the_name(store: Store):
    # given
    members = [slack_member('U2', 'a.smith@corp.example.com', 'Alice Smith')]

    # when
    out, t = named_assign(store, members, {'U2': 'a.smith@corp.example.com'}, email='alice.smith@example.com')

    # then
    assert out['dm'] == 'none' and note_texts(t) == ['\U0001F464 Assigned: Alice Smith · assigned by paul.stone']


def cold_assign(store: Store, jobs: list, caplog=None, patience: float = 0.5):
    t = FakeSlack()
    store.save({**state(anomaly(KEY, num=14, slackPost=POST)), 'initialized': True}, None)
    api = FakeSlackApi({'U1': 'a.smith@corp.example.com'})
    api.members = [slack_member('U1', 'a.smith@corp.example.com', 'Alice Smith')]
    slack = Slack('xoxb-test', '#ops', t, api)
    ids = tagreport.SlackIds(slack)
    people = tagreport.SlackPeople(slack, spawn=jobs.append)
    people.patience = patience
    notes = StateNotes(store, slack, mention=ids.mention, name_of=ids.name, by_name=tagreport.name_mentions(people, ids))
    return Assigner(store, slack, settings(), BASE, notes, by_name=people.email_of), t, people


def test_a_cold_member_list_is_waited_for_a_moment_so_the_first_assignment_still_mentions(store: Store):
    # given
    jobs: list = []
    a, t, _ = cold_assign(store, jobs)
    starter = threading.Timer(0.1, lambda: jobs[0]())

    # when
    starter.start()
    out = a.assign(KEY, 'Alice Smith', None, PAUL)
    starter.join()

    # then
    assert out['dm'] == 'sent' and note_texts(t) == ['\U0001F464 Assigned: <@U1> · assigned by paul.stone']


def test_warming_the_member_list_at_start_lets_the_first_assignment_mention_without_waiting(store: Store):
    # given
    jobs: list = []
    a, t, people = cold_assign(store, jobs, patience=0)
    people.lookup()
    jobs.pop()()

    # when
    out = a.assign(KEY, 'Alice Smith', None, PAUL)

    # then
    assert out['dm'] == 'sent' and note_texts(t) == ['\U0001F464 Assigned: <@U1> · assigned by paul.stone']


def test_a_loading_miss_does_not_use_up_the_warning_for_a_real_miss_later(caplog):
    # given
    api = FakeSlackApi({})
    api.members = [slack_member('U1', 'a.smith@corp.example.com', 'Alice Smith'), slack_member('U2', 'alice@other.example.com', 'alice smith')]
    jobs: list = []
    people = tagreport.SlackPeople(Slack('xoxb-test', '#ops', FakeSlack(), api), spawn=jobs.append)
    people.patience = 0

    # when
    with caplog.at_level('WARNING'):
        people.email_of('Alice Smith')
        jobs.pop()()
        people.email_of('Alice Smith')
        people.email_of('Alice Smith')

    # then
    said = [r.getMessage() for r in caplog.records if r.name == 'skopax.tagreport']
    assert len(said) == 2 and said[0].endswith('loading') and said[1].endswith('several Slack members have that name')


def test_the_dm_and_the_note_share_one_name_lookup(store: Store):
    # given
    asked: list[str] = []
    store.save({**state(anomaly(KEY, num=14, slackPost=POST)), 'initialized': True}, None)
    api = FakeSlackApi({'U1': 'a.smith@corp.example.com'})
    api.names['U1'] = 'Alice Smith'
    slack = Slack('xoxb-test', '#ops', FakeSlack(), api)
    ids = tagreport.SlackIds(slack)

    # when
    def find(name):
        asked.append(name)
        return 'a.smith@corp.example.com' if len(asked) == 1 else None

    notes = StateNotes(store, slack, mention=ids.mention, name_of=ids.name)
    out = Assigner(store, slack, settings(), BASE, notes, by_name=find).assign(KEY, 'Alice Smith', None, PAUL)

    # then
    assert asked == ['Alice Smith'] and out['dm'] == 'sent' and out['thread'] is True


def test_a_failing_mention_lookup_logs_no_address_and_no_name(store: Store, caplog):
    # given
    notes = StateNotes(store, Slack('xoxb-test', '#ops', FakeSlack(), FakeSlackApi()), mention=lambda email: (_ for _ in ()).throw(RuntimeError('boom alice.smith@example.com')))

    # when
    with caplog.at_level('WARNING'):
        out = notes._mention('Alice Smith', 'alice.smith@example.com', True)

    # then
    assert out is None and caplog.records and all('alice' not in r.getMessage().lower() for r in caplog.records)


def test_without_a_guessed_address_the_dm_and_the_note_still_share_one_name_lookup(store: Store):
    # given
    asked: list[str] = []
    store.save({**state(anomaly(KEY, num=14, slackPost=POST)), 'initialized': True}, None)
    api = FakeSlackApi({'U1': 'a.smith@corp.example.com'})
    slack = Slack('xoxb-test', '#ops', FakeSlack(), api)
    ids = tagreport.SlackIds(slack)

    def find(name):
        asked.append(name)
        return 'a.smith@corp.example.com' if len(asked) == 1 else None

    notes = StateNotes(store, slack, mention=ids.mention, name_of=ids.name)

    # when
    out = Assigner(store, slack, settings(), BASE, notes, by_name=find).assign(KEY, 'Alice Smith', None, 'token')

    # then
    assert asked == ['Alice Smith'] and out['thread'] is True


def test_a_member_list_that_does_not_settle_is_waited_for_only_once_per_build(caplog):
    # given
    api = FakeSlackApi({})
    jobs: list = []
    people = tagreport.SlackPeople(Slack('xoxb-test', '#ops', FakeSlack(), api), spawn=jobs.append)
    people.patience = 0.2
    started = time.monotonic()

    # when
    people.email_of('Alice Smith')
    people.email_of('Bob Reed')
    people.email_of('Cara Lund')

    # then
    assert time.monotonic() - started < 0.5


def test_a_failed_spawn_settles_the_wait_so_later_calls_do_not_wait():
    # given
    def broken(job):
        raise RuntimeError('no thread')

    people = tagreport.SlackPeople(Slack('xoxb-test', '#ops', FakeSlack(), FakeSlackApi({})), spawn=broken)
    people.patience = 5

    # when
    started = time.monotonic()
    first = people.email_of('Alice Smith')
    second = people.email_of('Bob Reed')

    # then
    assert (first, second) == (None, None) and time.monotonic() - started < 1
