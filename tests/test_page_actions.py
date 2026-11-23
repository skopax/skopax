import re

from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, state

KEY = 'log|checkout-service|ERROR|boom'
OTHER = 'log|orders-service|ERROR|x'


def card(client: TestClient, key: str = KEY) -> dict:
    return next(a for a in client.get('/api/state').json()['anomalies'] if a['key'] == key)


def muted_list(client: TestClient) -> list[dict]:
    return client.get('/api/state').json().get('muted') or []


def seed(client: TestClient) -> int:
    draft = {'text': 'draft', 'by': 'Claude', 'at': '2026-10-21T09:00:00Z'}
    return client.put('/api/state', json=state(anomaly(KEY, slackDraft=draft), anomaly(OTHER)), headers=AUTH).json()['rev']


def test_mute_applies_at_once_for_the_given_hours(client: TestClient):
    # given
    rev = seed(client)

    # when
    res = client.post('/api/actions', json={'type': 'mute', 'key': KEY, 'hours': 24, 'label': 'Mute'}, headers=AUTH)

    # then
    assert res.status_code == 201
    assert res.json()['status'] == 'done' and re.fullmatch(r'muted until \d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{2}:\d{2} UTC', res.json()['result']), res.json()['result']
    assert card(client)['muted'] == 'muted on the page for 24 h' and 'slackDraft' in card(client)
    assert 'muted' not in card(client, OTHER) or card(client, OTHER)['muted'] is None
    assert [(m['pattern'], m['kind'], m['reason']) for m in muted_list(client)] == [(KEY, 'mute', 'muted on the page for 24 h')]
    assert muted_list(client)[0]['until'] > '2026'
    assert client.get('/api/rev').json()['rev'] == rev + 1
    assert client.get('/api/actions', params={'status': 'requested'}).json() == []


def test_known_noise_mutes_for_good_with_the_reason_and_drops_the_slack_draft(client: TestClient):
    # given
    seed(client)

    # when
    res = client.post('/api/actions', json={'type': 'known', 'key': KEY, 'reason': 'expected on month end', 'label': 'Known noise'}, headers=AUTH)

    # then
    assert res.json()['status'] == 'done' and res.json()['result'] == 'known noise'
    assert card(client)['muted'] == 'expected on month end' and 'slackDraft' not in card(client)
    assert [(m['pattern'], m['kind'], m['until']) for m in muted_list(client)] == [(KEY, 'known', None)]


def test_unmute_clears_the_card_and_the_list_entry(client: TestClient):
    # given
    seed(client)
    client.post('/api/actions', json={'type': 'known', 'key': KEY, 'reason': 'r', 'label': 'Known noise'}, headers=AUTH)

    # when
    res = client.post('/api/actions', json={'type': 'unmute', 'key': KEY, 'label': 'Unmute'}, headers=AUTH)

    # then
    assert res.json()['status'] == 'done' and res.json()['result'] == 'unmuted'
    assert card(client)['muted'] is None
    assert muted_list(client) == []


def test_reopen_takes_a_card_back_from_in_progress_done_or_muted_and_keeps_its_owner_and_ticket(client: TestClient):
    # given
    seed(client)
    client.post('/api/actions', json={'type': 'take', 'key': KEY, 'ticket': 'OPS-1234', 'label': 'Take'}, headers=AUTH)
    client.post('/api/actions', json={'type': 'known', 'key': OTHER, 'reason': 'r', 'label': 'Known noise'}, headers=AUTH)

    # when
    back = client.post('/api/actions', json={'type': 'reopen', 'key': KEY, 'label': 'Reopen'}, headers=AUTH).json()
    unmuted = client.post('/api/actions', json={'type': 'reopen', 'key': OTHER, 'label': 'Reopen'}, headers=AUTH).json()

    # then
    assert (back['status'], back['result'], unmuted['result']) == ('done', 'reopened', 'reopened')
    assert 'triage' not in card(client) and card(client)['ticket'] == 'OPS-1234' and card(client).get('owner')
    assert card(client, OTHER)['muted'] is None and muted_list(client) == []


def test_link_puts_a_jira_key_on_the_card_in_progress_and_keeps_the_assignee_and_a_done_card_stays_done(client: TestClient):
    # given
    seed(client)
    client.put('/api/assignee', json={'key': KEY, 'name': 'Nina White'}, headers=AUTH)
    client.post('/api/actions', json={'type': 'resolve', 'key': OTHER, 'label': 'Resolve'}, headers=AUTH)

    # when
    linked = client.post('/api/actions', json={'type': 'link', 'key': KEY, 'ticket': 'ops-145', 'label': 'Link'}, headers=AUTH).json()
    done = client.post('/api/actions', json={'type': 'link', 'key': OTHER, 'ticket': 'INFRA-403', 'label': 'Link'}, headers=AUTH).json()
    words = client.post('/api/actions', json={'type': 'link', 'key': KEY, 'ticket': 'disk is full', 'label': 'Link'}, headers=AUTH).json()

    # then
    assert (linked['status'], linked['result']) == ('done', 'linked to OPS-145 · In progress')
    assert (card(client)['triage'], card(client)['ticket'], card(client)['owner']) == ('progress', 'OPS-145', 'Nina White')
    assert done['result'] == 'linked to INFRA-403' and (card(client, OTHER)['triage'], card(client, OTHER)['ticket']) == ('handled', 'INFRA-403')
    assert (words['status'], words['result']) == ('failed', 'link needs a Jira key like OPS-123') and card(client)['ticket'] == 'OPS-145'


def test_severity_is_set_by_hand_and_auto_gives_the_signal_s_back(client: TestClient, store):
    # given
    seed(client)

    # when
    down = client.post('/api/actions', json={'type': 'severity', 'key': KEY, 'sev': 'info', 'label': 'Severity'}, headers=AUTH).json()
    set_card = store.anomaly(KEY)
    listed = client.get('/api/state').json()
    back = client.post('/api/actions', json={'type': 'severity', 'key': KEY, 'sev': 'auto', 'label': 'Severity'}, headers=AUTH).json()
    wrong = client.post('/api/actions', json={'type': 'severity', 'key': KEY, 'sev': 'high', 'label': 'Severity'}, headers=AUTH)

    # then
    assert (down['status'], down['result']) == ('done', 'severity set to Info by hand (the signal says Warning)')
    assert (set_card['sev'], set_card['sevAuto'], set_card['sevSet']['sev'], set_card['sevSet']['auto']) == ('info', 'warn', 'info', 'warn')
    assert KEY not in [a['key'] for a in listed['anomalies']] and [r['key'] for r in listed['information']] == [KEY], 'Info moves it to the Information page'
    assert back['result'] == 'severity back to the signal (Warning)' and card(client)['sev'] == 'warn' and 'sevSet' not in card(client) and 'sevAuto' not in card(client)
    assert wrong.status_code == 422


def test_take_is_in_progress_with_the_operator_as_owner_and_resolve_is_no_action_needed(client: TestClient):
    # given
    seed(client)

    # when
    taken = client.post('/api/actions', json={'type': 'take', 'key': KEY, 'ticket': 'OPS-1234', 'label': 'Take'}, headers=AUTH)
    owner = card(client).get('owner')
    again = client.post('/api/actions', json={'type': 'take', 'key': KEY, 'reason': 'looking at it', 'label': 'Take'}, headers=AUTH)
    resolved = client.post('/api/actions', json={'type': 'resolve', 'key': OTHER, 'reason': 'one phone retried, nothing for us', 'label': 'Resolve'}, headers=AUTH)

    # then
    assert owner and (taken.json()['status'], taken.json()['result']) == ('done', f'In progress · {owner} · OPS-1234')
    assert (again.json()['status'], again.json()['result']) == ('done', f'In progress · {owner} · looking at it')
    assert (card(client)['triage'], card(client)['ticket'], card(client)['note']['text']) == ('progress', 'OPS-1234', 'looking at it'), 'a Take without a ticket keeps the one on the card'
    assert (resolved.json()['status'], resolved.json()['result']) == ('done', 'No action needed · one phone retried, nothing for us')
    assert card(client, OTHER)['triage'] == 'handled' and 'ticket' not in card(client, OTHER)
    assert card(client, OTHER)['note']['text'] == 'one phone retried, nothing for us' and 'triagedBy' not in card(client, OTHER)


def test_take_keeps_the_person_the_card_is_assigned_to(client: TestClient):
    # given
    seed(client)
    client.put('/api/assignee', json={'key': KEY, 'name': 'Dora Lane', 'email': 'dora.lane@example.com'}, headers=AUTH)

    # when
    taken = client.post('/api/actions', json={'type': 'take', 'key': KEY, 'label': 'Take'}, headers=AUTH).json()
    other = client.post('/api/actions', json={'type': 'take', 'key': OTHER, 'label': 'Take'}, headers=AUTH).json()

    # then
    assert (taken['status'], taken['result']) == ('done', 'In progress · Dora Lane'), 'starting progress on an assigned card does not take it over'
    assert (card(client)['triage'], card(client)['owner'], card(client)['assigned']['email']) == ('progress', 'Dora Lane', 'dora.lane@example.com')
    assert card(client, OTHER)['owner'] and card(client, OTHER)['owner'] != 'Dora Lane', 'a card nobody holds goes to whoever started it'
    assert other['status'] == 'done'


def test_take_and_resolve_reasons_go_under_the_services_finding_instead_of_replacing_it(client: TestClient):
    # given
    client.put('/api/state', json=state(anomaly(KEY, note={'text': 'A crawl, not users: Screaming Frog from 203.0.113.7.', 'at': '2026-11-07T10:35:17Z'})), headers=AUTH)

    # when
    taken = client.post('/api/actions', json={'type': 'take', 'key': KEY, 'reason': 'looking into it', 'label': 'Take'}, headers=AUTH)
    resolved = client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'reason': 'I ran the crawl, no problem', 'label': 'Resolve'}, headers=AUTH)

    # then
    assert taken.json()['status'] == 'done' and resolved.json()['result'] == 'No action needed · I ran the crawl, no problem'
    assert card(client)['note']['text'] == ('A crawl, not users: Screaming Frog from 203.0.113.7.\n\nTaken by bootstrap: looking into it'
                                           '\n\nResolved by bootstrap: I ran the crawl, no problem')
    assert card(client)['triage'] == 'handled'


def test_group_merges_under_an_open_card_and_refuses_flips_merged_parents_and_a_critical_child_under_a_handled_parent(client: TestClient):
    # given
    crit, handled, cleared = 'log|c|ERROR|c', 'log|h|ERROR|h', 'log|z|ERROR|z'
    client.put('/api/state', json=state(anomaly(KEY), anomaly(OTHER), anomaly(crit, sev='crit'), anomaly(handled, triage='handled'), anomaly(cleared, status='cleared')), headers=AUTH)
    group = lambda key, parent: client.post('/api/actions', json={'type': 'group', 'key': key, 'parent': parent, 'label': 'Group'}, headers=AUTH).json()

    # when
    ok = group(OTHER, KEY)
    flip = group(KEY, OTHER)
    under_child = group(crit, OTHER)
    crit_under_handled = group(crit, handled)
    gone = group(KEY, cleared)
    itself = group(KEY, KEY)
    missing = group(KEY, 'log|nope|ERROR|x')

    # then
    host = card(client, KEY)
    named = (f"#{host['num']} " if host.get('num') else '') + f"{host['svc']} / {host['label']}"
    assert (ok['status'], ok['result']) == ('done', f'grouped under {named}') and card(client, OTHER)['parent'] == KEY and KEY not in ok['result']
    assert (flip['status'], flip['result']) == ('failed', f'{OTHER} is already merged under {KEY}; that grouping stands')
    assert (under_child['status'], under_child['result']) == ('failed', f'{OTHER} is itself merged under {KEY}; group under {KEY} instead')
    assert (crit_under_handled['status'], crit_under_handled['result']) == ('failed', f'{handled} is handled and {crit} is critical; a critical card stays open for a human, also under a parent')
    assert (gone['status'], gone['result']) == ('failed', f'{cleared} already stopped and {KEY} is still open; pick an open card as the parent')
    stopped = group(cleared, KEY)
    assert (stopped['status'], stopped['result']) == ('done', f'grouped under {named}') and card(client, cleared)['parent'] == KEY, 'a card that stopped firing can still be merged under an open one'
    assert (itself['status'], itself['result']) == ('failed', 'pick another card as the parent')
    assert (missing['status'], missing['result']) == ('failed', 'no anomaly with key log|nope|ERROR|x')
    assert 'parent' not in card(client, KEY) and 'parent' not in card(client, crit)


def test_an_instant_action_on_a_missing_card_fails_at_once_and_a_dive_still_queues(client: TestClient):
    # given
    seed(client)

    # when
    gone = client.post('/api/actions', json={'type': 'mute', 'key': 'log|nope|ERROR|x', 'hours': 1, 'label': 'Mute'}, headers=AUTH)
    dive = client.post('/api/actions', json={'type': 'dive', 'key': KEY, 'label': 'Investigate'}, headers=AUTH)

    # then
    assert gone.status_code == 201 and gone.json()['status'] == 'failed' and gone.json()['result'] == 'no anomaly with key log|nope|ERROR|x'
    assert dive.json()['status'] == 'requested'
    assert [a['type'] for a in client.get('/api/actions', params={'status': 'requested'}).json()] == ['dive']


def test_grouping_a_card_that_holds_others_moves_them_along_and_a_resolved_card_is_never_a_parent(client: TestClient):
    # given
    a, b, c, d, resolved = 'page-typos|a', 'page-typos|b', 'page-typos|c', 'page-typos|d', 'page-layout|r'
    client.put('/api/state', json=state(anomaly(a), anomaly(b), anomaly(c), anomaly(d), anomaly(resolved, triage='handled')), headers=AUTH)
    group = lambda key, parent: client.post('/api/actions', json={'type': 'group', 'key': key, 'parent': parent, 'label': 'Group'}, headers=AUTH).json()

    # when
    group(a, b)
    group(b, c)
    under_resolved = group(d, resolved)

    # then
    assert (card(client, a)['parent'], card(client, b)['parent']) == (c, c), 'no chain: a moved along with b'
    assert 'parent' not in card(client, c)
    assert (under_resolved['status'], under_resolved['result']) == ('failed', f'{resolved} is already resolved (Done), so {d} would disappear from Open under it; pick an open card as the parent')
    assert 'parent' not in card(client, d)


def test_resolve_stores_how_the_card_was_resolved_and_a_resolve_without_a_kind_is_no_action_needed(client: TestClient, store):
    # given
    seed(client)

    # when
    fixed = client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'kind': 'fixed', 'ref': 'OPS-140', 'label': 'Resolve'}, headers=AUTH).json()
    plain = client.post('/api/actions', json={'type': 'resolve', 'key': OTHER, 'label': 'Resolve'}, headers=AUTH).json()
    stored, other = store.anomaly(KEY), store.anomaly(OTHER)

    # then
    assert (fixed['status'], fixed['result']) == ('done', 'Fixed (OPS-140)')
    assert (plain['status'], plain['result']) == ('done', 'No action needed')
    assert stored['triage'] == 'handled' and stored['resolution'] == {'kind': 'fixed', 'ref': 'OPS-140', 'by': 'token:bootstrap', 'via': 'page', 'at': stored['triagedAt']}
    assert {k: other['resolution'][k] for k in ('kind', 'via')} == {'kind': 'handled', 'via': 'page'} and 'ref' not in other['resolution']


def test_a_bad_kind_or_a_duplicate_without_a_ref_is_a_422_and_changes_nothing(client: TestClient, store):
    # given
    seed(client)

    # when
    bad = client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'kind': 'gone', 'label': 'Resolve'}, headers=AUTH)
    bare = client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'kind': 'duplicate', 'ref': '  ', 'label': 'Resolve'}, headers=AUTH)
    long = client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'kind': 'fixed', 'ref': 'x' * 201, 'label': 'Resolve'}, headers=AUTH)

    # then
    assert (bad.status_code, bare.status_code, long.status_code) == (422, 422, 422)
    assert 'kind is one of fixed, handled, duplicate' in bad.text and 'a duplicate needs ref' in bare.text
    assert store.anomaly(KEY).get('triage') is None and client.get('/api/actions').json() == []


def test_a_duplicate_names_the_other_card_by_number_and_never_itself(client: TestClient, store):
    # given
    seed(client)
    store.card_ids.insert_many([{'_id': KEY, 'n': 12}, {'_id': OTHER, 'n': 13}])

    # when
    by_number = client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'kind': 'duplicate', 'ref': '13', 'label': 'Resolve'}, headers=AUTH).json()
    client.post('/api/actions', json={'type': 'reopen', 'key': KEY, 'label': 'Reopen'}, headers=AUTH)
    by_key = client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'kind': 'duplicate', 'ref': OTHER, 'label': 'Resolve'}, headers=AUTH).json()
    itself = client.post('/api/actions', json={'type': 'resolve', 'key': OTHER, 'kind': 'duplicate', 'ref': '#13', 'label': 'Resolve'}, headers=AUTH).json()
    unknown = client.post('/api/actions', json={'type': 'resolve', 'key': OTHER, 'kind': 'duplicate', 'ref': '#999', 'label': 'Resolve'}, headers=AUTH).json()

    # then
    assert by_number['result'] == by_key['result'] == 'Duplicate of #13'
    assert store.anomaly(KEY)['resolution']['ref'] == '#13'
    assert (itself['status'], itself['result']) == ('failed', 'a card cannot be a duplicate of itself; give the other card')
    assert (unknown['status'], unknown['result']) == ('failed', 'no card #999 to be a duplicate of')
    assert store.anomaly(OTHER).get('triage') is None


def test_any_other_triage_drops_the_resolution_and_a_relink_keeps_it(client: TestClient, store):
    # given
    seed(client)
    client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'kind': 'fixed', 'label': 'Resolve'}, headers=AUTH)

    # when
    client.post('/api/actions', json={'type': 'link', 'key': KEY, 'ticket': 'OPS-141', 'label': 'Link'}, headers=AUTH)
    relinked = store.anomaly(KEY)
    client.post('/api/actions', json={'type': 'take', 'key': KEY, 'label': 'Take'}, headers=AUTH)
    taken = store.anomaly(KEY)

    # then
    assert (relinked['triage'], relinked['ticket'], relinked['resolution']['kind']) == ('handled', 'OPS-141', 'fixed')
    assert taken['triage'] == 'progress' and 'resolution' not in taken
