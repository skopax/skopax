from tests.conftest import anomaly, jira_links, state
from skopax.engine import iso
from skopax.jiralinks import SWEEP_BY
from skopax.jiralinks import JiraLinks, card_keys, jira_time, ticket_finder
from skopax.store import Store


class FakeJira:
    def __init__(self, issues, enabled: bool = True):
        self.issues = issues
        self.enabled = enabled
        self.calls = []

    def search(self, jql, fields, limit=20):
        self.calls.append((jql, fields, limit))
        if jql.startswith('key in ('):
            wanted = jql[len('key in ('):jql.index(')')].split(', ')
            return [i for i in self.issues if i['key'] in wanted and i['fields']['status']['name'] in ('Live', 'Done', 'Cancelled')]
        return self.issues


def paragraph(text: str) -> dict:
    return {'type': 'doc', 'version': 1, 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': text}]}]}


NOW = 1_792_224_000.0


def closed(hours: float) -> str:
    import datetime as dt
    return (dt.datetime.fromtimestamp(NOW - hours * 3600, dt.timezone(dt.timedelta(hours=-5)))).strftime('%Y-%m-%dT%H:%M:%S.000%z')


def issue(key: str, status: str, text: str, dev: str | None = None, comment: str | None = None, closed_at: str | None = None) -> dict:
    return {'key': key, 'fields': {'summary': 's', 'status': {'name': status}, 'description': paragraph(text), 'statuscategorychangedate': closed_at,
                                   'comment': {'comments': [{'body': paragraph(comment)}] if comment else []},
                                   'customfield_4': {'displayName': dev} if dev else None}}


def test_a_ticket_that_names_a_card_moves_it_to_in_progress_with_the_developer(store: Store):
    # given
    store.save_state(state(anomaly('log|a|ERROR|x'), anomaly('log|b|ERROR|y', triage='progress', ticket='OPS-1'), anomaly('log|c|ERROR|z', status='cleared')), None)
    jira = FakeJira([
        issue('OPS-126', 'In Development', 'PayCo 403, card: https://skopax.example.com/#a=log%7Ca%7CERROR%7Cx', 'Oscar Green'),
        issue('OPS-127', 'In Development', 'https://skopax.example.com/#a=log%7Cb%7CERROR%7Cy'),
        issue('OPS-128', 'Live', 'https://skopax.example.com/#a=log%7Cc%7CERROR%7Cz'),
        issue('OPS-129', 'Backlog', 'https://skopax.example.com/#a=log%7Cnope%7CERROR%7Cz'),
    ])

    # when
    linked = jira_links(store, jira).run_once()
    a = store.anomaly('log|a|ERROR|x')

    # then
    assert linked == [{'card': 'log|a|ERROR|x', 'issue': 'OPS-126'}]
    assert (a['triage'], a['ticket'], a['owner']) == ('progress', 'OPS-126', 'Oscar Green')
    assert store.anomaly('log|b|ERROR|y')['ticket'] == 'OPS-1', 'a card that already carries another ticket is left alone'
    assert jira.calls[0][0].startswith('text ~ "skopax.example.com"') and 'customfield_4' in jira.calls[0][1]
    assert [r['text'] for r in store.list_activity(None, 5)] == ['OPS-126 → log|a|ERROR|x In progress · Oscar Green']


def test_links_in_comments_count_and_a_second_sweep_changes_nothing(store: Store):
    # given
    store.save_state(state(anomaly('log|a|ERROR|x')), None)
    jira = FakeJira([issue('OPS-131', 'Backlog', 'no link here', comment='card: https://skopax.example.com/#a=log%7Ca%7CERROR%7Cx)')])
    links = jira_links(store, jira)

    # when
    first = links.run_once()
    second = links.run_once()

    # then
    assert first == [{'card': 'log|a|ERROR|x', 'issue': 'OPS-131'}] and second == []
    assert store.anomaly('log|a|ERROR|x')['triage'] == 'progress' and 'owner' not in store.anomaly('log|a|ERROR|x')
    assert len(store.list_activity(None, 5)) == 1


def test_the_sweep_is_off_without_jira_or_when_switched_off(store: Store):
    # then
    assert jira_links(store, FakeJira([], enabled=False)).enabled is False
    assert jira_links(store, FakeJira([]), flag=False).enabled is False
    assert jira_links(store, FakeJira([])).enabled is True
    assert JiraLinks(store, FakeJira([]), base_url='').enabled is False
    assert card_keys({'description': 'x https://skopax.example.com/#a=cf-status%7Ccloudflare%2FCDN%2FCache y'}, 'https://skopax.example.com') == {'cf-status|cloudflare/CDN/Cache'}


def test_a_closed_ticket_reopens_a_card_that_still_fires_and_resolves_one_that_cleared(store: Store):
    # given
    store.save_state(state(anomaly('log|a|ERROR|x', triage='progress', ticket='OPS-126', owner='Oscar Green'),
                           anomaly('log|b|ERROR|y', status='cleared', triage='progress', ticket='OPS-127'),
                           anomaly('log|c|ERROR|z', triage='progress', ticket='OPS-128'),
                           anomaly('log|d|ERROR|w', triage='progress', ticket='no ticket yet'),
                           anomaly('log|e|ERROR|v', triage='progress', ticket='OPS-129')), None)
    jira = FakeJira([issue('OPS-126', 'Live', 'x', closed_at=closed(3)), issue('OPS-127', 'Done', 'y', closed_at=closed(0.2)), issue('OPS-128', 'In Development', 'z'),
                     issue('OPS-129', 'Live', 'v', closed_at=closed(0.5))])

    # when
    out = jira_links(store, jira).run_once(NOW)
    a, b, c = (store.anomaly(k) for k in ('log|a|ERROR|x', 'log|b|ERROR|y', 'log|c|ERROR|z'))
    dives = [x for x in store.list_actions('requested', 10) if x['type'] == 'dive']

    # then
    assert out == [{'card': 'log|a|ERROR|x', 'issue': 'OPS-126', 'closed': 'Live'}, {'card': 'log|b|ERROR|y', 'issue': 'OPS-127', 'closed': 'Done'}]
    assert 'triage' not in a and a['ticket'] == 'OPS-126' and a['note']['text'].startswith('OPS-126 is Live but the card still fires'), a
    assert [(d['key'], d['requestedBy'], d['label']) for d in dives] == [('log|a|ERROR|x', SWEEP_BY, 'OPS-126 Live, still firing')]
    assert (b['triage'], b['ticket'], b['triagedAt']) == ('handled', 'OPS-127', iso(NOW)), 'the resolve is stamped with the sweep time, so the card shows 24 h under Done'
    assert 'triagedAt' not in a, 'the reopen clears the stamp with the triage'
    assert (c['triage'], c['ticket']) == ('progress', 'OPS-128'), 'an open ticket changes nothing'
    assert store.anomaly('log|e|ERROR|v')['triage'] == 'progress', 'a ticket closed 30 min ago gets its 2 h grace: the card still shows the hour before the fix'
    assert jira.calls[-1][0] == 'key in (OPS-126, OPS-127, OPS-128, OPS-129) AND status in (Live, Done, Cancelled)', 'the card without a real ticket key is not looked up'
    assert sorted(r['text'] for r in store.list_activity(None, 5)) == ['OPS-126 Live but log|a|ERROR|x still fires · reopened, Investigate queued', 'OPS-127 Done · log|b|ERROR|y cleared, resolved']
    assert jira_links(store, jira).run_once(NOW) == [], 'a second sweep finds nothing left to do'
    assert jira_links(store, jira).run_once(NOW + 2 * 3600) == [{'card': 'log|e|ERROR|v', 'issue': 'OPS-129', 'closed': 'Live'}], 'after the grace the still-firing card is reopened'


def test_a_live_or_done_ticket_resolves_its_cleared_card_as_fixed_and_a_cancelled_one_as_no_action_needed(store: Store):
    # given
    store.save_state(state(anomaly('log|b|ERROR|y', status='cleared', triage='progress', ticket='OPS-127'),
                           anomaly('log|f|ERROR|u', status='cleared', triage='progress', ticket='OPS-130')), None)
    jira = FakeJira([issue('OPS-127', 'Done', 'y', closed_at=closed(3)), issue('OPS-130', 'Cancelled', 'u', closed_at=closed(3))])

    # when
    jira_links(store, jira).run_once(NOW)
    done, cancelled = store.anomaly('log|b|ERROR|y')['resolution'], store.anomaly('log|f|ERROR|u')['resolution']

    # then
    assert done == {'kind': 'fixed', 'ref': 'OPS-127', 'by': SWEEP_BY, 'via': 'system', 'at': iso(NOW)}
    assert (cancelled['kind'], cancelled['ref'], cancelled['via']) == ('handled', 'OPS-130', 'system')


def test_jira_times_parse_with_and_without_the_offset_colon():
    # then
    assert jira_time('2026-10-22T01:15:30.123-0500') == jira_time('2026-10-22T01:15:30.123-05:00') == jira_time('2026-10-22T07:15:30.123+01:00') == 1_792_649_730.123
    assert jira_time(None) is None and jira_time('not a date') is None


def test_with_no_jira_projects_the_ticket_finder_skips_encodings_hashes_and_standards():
    # given
    text = 'fix UTF-8 decoding for OPS-12 per RFC-7231, hashed with SHA-256 over TLS-1 for CVE-2024-1234 and ISO-8859'

    # when
    found = ticket_finder(()).findall(text)
    named = ticket_finder(('UTF', 'OPS')).findall(text)

    # then
    assert found == ['OPS-12']
    assert named == ['UTF-8', 'OPS-12'], 'a configured project of that name is still found'
