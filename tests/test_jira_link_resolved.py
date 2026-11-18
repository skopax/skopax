from tests.conftest import anomaly, jira_links, state
from tests.test_jiralinks import FakeJira, issue
from skopax.actions import apply
from skopax.store import Store

LINK = 'card: https://skopax.example.com/#a=log%7Ca%7CERROR%7C'


class Notes:
    def __init__(self):
        self.linked: list[tuple[str, str, str | None]] = []

    def jira_linked(self, key: str, ticket: str, owner: str | None) -> None:
        self.linked.append((key, ticket, owner))


def test_a_card_a_person_resolved_stays_resolved_while_the_ticket_that_names_it_is_open(store: Store):
    # given
    store.save({**state(anomaly('log|a|ERROR|x')), 'initialized': True}, None)
    notes = Notes()
    links = jira_links(store, FakeJira([issue('OPS-126', 'In UAT', LINK + 'x', 'Paul Stone')]), notes=notes)
    links.run_once()
    apply(store, {'type': 'resolve', 'key': 'log|a|ERROR|x', 'requestedBy': 'nina.white@example.com'})

    # when
    again = links.run_once()
    later = links.run_once()

    # then
    card = store.anomaly('log|a|ERROR|x')
    assert (again, later) == ([], [])
    assert card['triage'] == 'handled' and 'ticket' not in card
    assert notes.linked == [('log|a|ERROR|x', 'OPS-126', 'Paul Stone')], 'the thread heard In progress once, before the resolve'
    assert [a['text'] for a in store.list_activity(None, 10) if a['kind'] == 'jira'] == ['OPS-126 → log|a|ERROR|x In progress · Paul Stone']


def test_a_card_the_investigator_or_the_auto_rules_resolved_still_follows_a_ticket_a_person_opened_for_it(store: Store):
    # given
    store.save({**state(anomaly('log|a|ERROR|pod', triage='handled', triagedBy='pod'), anomaly('log|a|ERROR|auto', triage='handled', triagedBy='auto')), 'initialized': True}, None)
    jira = FakeJira([issue('OPS-126', 'In Development', f'{LINK}pod {LINK}auto', 'Paul Stone')])

    # when
    linked = jira_links(store, jira).run_once()

    # then
    assert linked == [{'card': 'log|a|ERROR|auto', 'issue': 'OPS-126'}, {'card': 'log|a|ERROR|pod', 'issue': 'OPS-126'}]
    assert [(store.anomaly(k)['triage'], store.anomaly(k)['ticket']) for k in ('log|a|ERROR|pod', 'log|a|ERROR|auto')] == [('progress', 'OPS-126')] * 2


def test_one_ticket_naming_two_cards_gives_the_unpicked_one_the_developer_not_the_other_card_s_assignee(store: Store):
    # given
    store.save({**state(anomaly('log|a|ERROR|a', owner='nina.white@example.com', assigned={'by': 'paul.stone@example.com', 'at': '2026-11-10T09:00:00Z'}),
                        anomaly('log|a|ERROR|b')), 'initialized': True}, None)
    notes = Notes()
    jira = FakeJira([issue('OPS-126', 'In Development', f'{LINK}a {LINK}b', 'Paul Stone')])

    # when
    jira_links(store, jira, notes=notes).run_once()

    # then
    a, b = store.anomaly('log|a|ERROR|a'), store.anomaly('log|a|ERROR|b')
    assert (a['owner'], b['owner']) == ('nina.white@example.com', 'Paul Stone')
    assert notes.linked == [('log|a|ERROR|a', 'OPS-126', 'nina.white@example.com'), ('log|a|ERROR|b', 'OPS-126', 'Paul Stone')]
    assert sorted(x['text'] for x in store.list_activity(None, 10) if x['kind'] == 'jira') == ['OPS-126 → log|a|ERROR|a In progress · nina.white@example.com',
                                                                                             'OPS-126 → log|a|ERROR|b In progress · Paul Stone']
