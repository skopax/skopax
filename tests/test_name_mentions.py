import logging

import pytest

from skopax import tagreport
from skopax.engine import parse_iso
from skopax.reminders import Reminders, person
from skopax.slack import Slack, SlackError
from skopax.statenotes import StateNotes
from skopax.store import Store
from tests.fake_slack import FakeSlackApi
from tests.test_state_notes import KEY, POST_TS, Transport, card, seed

ALICE, BOB, DORA = 'alice@example.com', 'bob@example.com', 'dora@example.com'
POSTED = parse_iso('2026-10-26T08:00:00Z') + 24.5 * 3600
H = 3600


def member(uid: str, email: str, real: str, display: str = '') -> dict:
    return {'id': uid, 'name': email.split('@')[0], 'profile': {'email': email, 'real_name': real, 'display_name': display}}


class NoScope(FakeSlackApi):
    def __call__(self, token, method, payload):
        if method == 'users.list':
            self.calls.append((method, payload))
            raise SlackError('missing_scope', code='missing_scope')
        return super().__call__(token, method, payload)


def resolver(*members: dict, api: FakeSlackApi | None = None, clock=None):
    api = api or FakeSlackApi(emails={m['id']: m['profile']['email'] for m in members})
    api.members = list(members)
    slack = Slack('xoxb-test', '#ops', Transport(), api)
    timing = {'clock': clock} if clock is not None else {}
    people = tagreport.SlackPeople(slack, spawn=lambda job: job(), **timing)
    return tagreport.name_mentions(people, tagreport.SlackIds(slack)), api


def test_a_name_with_one_matching_slack_member_is_mentioned():
    # given
    by_name, _ = resolver(member('U0ALICE', ALICE, 'Alice Moss'), member('U0BOB', BOB, 'Bob Reed'))

    # when
    out = person('Alice Moss', by_name=by_name)

    # then
    assert out == '<@U0ALICE>'


@pytest.mark.parametrize('asked', ['alice mos', 'ALICE  MOS ', 'Alıce Moş', 'a.moss'])
def test_case_turkish_letters_and_extra_spaces_still_match_the_real_or_display_name(asked):
    # given
    by_name, _ = resolver(member('U0ALICE', ALICE, 'Alıce Moş', display='a.moss'))

    # when
    out = person(asked, by_name=by_name)

    # then
    assert out == '<@U0ALICE>'


def test_two_members_with_the_same_name_are_never_pinged(caplog):
    # given
    by_name, _ = resolver(member('U0ALICE', ALICE, 'Alice Moss'), member('U0DORA', DORA, 'alice moss'))

    # when
    with caplog.at_level(logging.WARNING):
        out = person('Alice Moss', by_name=by_name)

    # then
    assert out == 'Alice Moss'
    said = [r.getMessage() for r in caplog.records if r.name == 'skopax.tagreport']
    assert len(said) == 1 and 'Alice' not in said[0] and 'Moss' not in said[0] and '10 characters' in said[0]


def test_without_the_users_read_scope_the_name_stays_plain():
    # given
    by_name, api = resolver(member('U0ALICE', ALICE, 'Alice Moss'), api=NoScope(emails={'U0ALICE': ALICE}))

    # when
    out = person('Alice <Moss>', by_name=by_name)

    # then
    assert out == 'Alice &lt;Moss&gt;' and [m for m, _ in api.calls].count('users.list') == 1


def test_the_email_path_is_unchanged_and_never_asks_by_name():
    # given
    asked: list[str] = []

    def by_name(name: str):
        asked.append(name)
        return '<@U0WRONG>'

    # when
    held = person('Alice Moss', lambda e: '<@U0ALICE>' if e == ALICE else None, lambda e: 'Alice', ALICE, by_name=by_name)
    plain = person(BOB, lambda e: None, lambda e: 'Bob Reed', by_name=by_name)

    # then
    assert (held, plain, asked) == ('<@U0ALICE>', 'Bob Reed', [])


def test_an_unresolved_name_warns_at_most_once_an_hour(caplog):
    # given
    now = [0.0]
    by_name, _ = resolver(member('U0BOB', BOB, 'Bob Reed'), clock=lambda: now[0])

    # when
    with caplog.at_level(logging.WARNING):
        person('Carl Stone', by_name=by_name)
        person('carl  stone', by_name=by_name)
        now[0] += H + 1
        person('Carl Stone', by_name=by_name)

    # then
    assert len([r for r in caplog.records if r.name == 'skopax.tagreport']) == 2


def test_the_card_reminder_and_the_group_reminder_mention_a_name_only_assignee(store: Store):
    # given
    by_name, _ = resolver(member('U0ALICE', ALICE, 'Alice Moss'))
    other = 'log|checkout-service|ERROR|other'
    seed(store, card(KEY, owner='Alice Moss'))
    single = Transport()
    Reminders(store, Slack('xoxb-test', '#ops', single, FakeSlackApi()), 24, 'https://skopax.example.com', by_name=by_name).run_once(POSTED)
    seed(store, card(other, owner='Alice Moss'), card(other + '|2', owner='Alice Moss'))
    grouped = Transport()

    # when
    Reminders(store, Slack('xoxb-test', '#ops', grouped, FakeSlackApi()), 24, 'https://skopax.example.com', by_name=by_name).run_once(POSTED)

    # then
    assert '<@U0ALICE> was assigned' in single.replies()[0][1]
    assert grouped.replies()[0][1].count('assigned to <@U0ALICE>.') == 2


def test_the_assigned_note_mentions_a_name_only_assignee(store: Store):
    # given
    by_name, _ = resolver(member('U0ALICE', ALICE, 'Alice Moss'))
    seed(store, card(KEY))
    t = Transport()
    notes = StateNotes(store, Slack('xoxb-test', '#ops', t, FakeSlackApi()), by_name=by_name)

    # when
    notes.assigned(KEY, 'Alice Moss', 'bob@example.com')

    # then
    assert t.replies() and '<@U0ALICE>' in t.replies()[0][1] and t.replies()[0][0] == POST_TS
