import json

from tests.conftest import anomaly
from tests.test_investigator import NOW
from tests.test_investigator_tools import box, sources
from skopax.engine import iso
from skopax.investigator import prompt
from skopax.investigator.tools import READ_TOOLS, Effects, Toolbox
from skopax.store import Store

KEY = 'log|reports-service|WARN|ShipCorp 456 invalid client ip'
TWIN = 'log|reports-service|WARN|ShipCorp 456 invalid client ip from 10.1'
DAY = 86400


def ago(seconds: float) -> str:
    return iso(NOW - seconds)


def past(key: str, days: float, svc: str = 'reports-service', label: str = 'WARN pattern', **extra) -> dict:
    first = ago(days * DAY)
    return {'_id': f'{key}|{first}', 'key': key, 'svc': svc, 'label': label, 'ns': '-', 'src': 'logs', 'sev': 'warn', 'firstSeen': first, 'lastSeen': first, 'clearedAt': first,
            'peak': 12, 'unit': '', 'outcome': 'handled', 'triage': 'handled', 'note': 'ShipCorp answers 456, they allow-list our IP', 'recommend': 'ask ShipCorp', 'runId': 'r1',
            'recap': 'ShipCorp 456 again', 'reopens': 0, 'runs': ['r1'], 'events': [{'ev': 'handled', 'at': first}], 'atDt': None, 'muted': None, **extra}


def call(b: Toolbox, args: dict) -> tuple[list[dict], bool]:
    out, err = b.call('past_cards', args)
    return (json.loads(out) if not err else out), err


def test_past_cards_is_a_read_tool_so_slack_asks_get_it_too(store: Store):
    # given
    slack = Toolbox(sources(), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), [KEY], 'live', NOW, 'https://skopax.example.com', read_only=True)

    # then
    assert 'past_cards' in {t['name'] for t in READ_TOOLS}
    assert 'past_cards' in {t['name'] for t in slack.definitions()}


def test_exact_key_first_then_same_service_and_label_then_text(store: Store):
    # given
    store.card_history.insert_many([
        past(KEY, 9), past(KEY, 2),
        past(TWIN, 5),
        past('log|checkout-service|ERROR|x', 1, svc='checkout-service', label='ERROR pattern', note='nothing about ShipCorp 456 here'),
        past('log|checkout-service|ERROR|y', 1.5, svc='checkout-service', label='ERROR pattern', note='unrelated'),
    ])
    store.save_state({'initialized': True, 'anomalies': [anomaly(KEY, svc='reports-service', label='WARN pattern', firstSeen=ago(600))]}, None)

    # when
    rows, err = call(box(store, keys=(KEY,)), {'key': KEY, 'text': 'shipcorp 456'})

    # then
    assert not err
    assert [(r['key'], r['match']) for r in rows] == [(KEY, 'key'), (KEY, 'key'), (TWIN, 'service+label'), ('log|checkout-service|ERROR|x', 'text')]
    assert rows[0]['firstSeen'] == ago(2 * DAY)


def test_only_allowlisted_fields_come_back(store: Store):
    # given
    store.card_history.insert_one(past(KEY, 2, asks=[{'at': ago(DAY), 'by': 'adam.baker', 'question': 'Why?', 'answer': 'ShipCorp', 'thread': 'https://example.slack.com/archives/C1/p1', 'usd': 0.1, 'runId': 'r2', 'secret': 'x'}],
                                       slackDraft={'text': 'draft'}, internal='x'))

    # when
    [row], _ = call(box(store, keys=(KEY,)), {'key': KEY})

    # then
    assert set(row) <= {'key', 'svc', 'label', 'sub', 'ns', 'src', 'sev', 'unit', 'firstSeen', 'lastSeen', 'clearedAt', 'peak', 'outcome', 'triage', 'ticket', 'owner',
                        'triagedBy', 'muted', 'note', 'recommend', 'runId', 'recap', 'reopens', 'asks', 'backfill', 'match', 'resolution', 'outcomeText'}
    assert row['asks'] == [{'at': ago(DAY), 'by': 'adam.baker', 'question': 'Why?', 'answer': 'ShipCorp', 'thread': 'https://example.slack.com/archives/C1/p1', 'usd': 0.1, 'runId': 'r2'}]


def test_limit_is_capped_at_ten_and_one_filter_is_needed(store: Store):
    # given
    store.card_history.insert_many([past(KEY, d) for d in range(1, 16)])

    # when
    rows, _ = call(box(store, keys=(KEY,)), {'key': KEY, 'limit': 10})
    over, over_err = call(box(store, keys=(KEY,)), {'key': KEY, 'limit': 50})
    few, _ = call(box(store, keys=(KEY,)), {'key': KEY, 'limit': 3})
    none, err = call(box(store, keys=(KEY,)), {})

    # then
    assert len(rows) == 10 and len(few) == 3
    assert (over, over_err) == ('limit must be at most 10, got 50', True)
    assert err and 'key, service or text' in none


def test_service_alone_lists_that_service_newest_first(store: Store):
    # given
    store.card_history.insert_many([past(KEY, 9), past(TWIN, 2), past('log|checkout-service|ERROR|x', 1, svc='checkout-service')])

    # when
    rows, _ = call(box(store, keys=(KEY,)), {'service': 'reports-service'})

    # then
    assert [(r['key'], r['match']) for r in rows] == [(TWIN, 'service'), (KEY, 'service')]


def test_the_brief_carries_a_seen_before_line_with_the_ticket_and_the_last_slack_answer():
    # given
    card = anomaly(KEY, firstSeen=ago(600), seenBefore={'count': 3, 'lastAt': '2026-10-24T11:05:00Z', 'outcome': 'progress', 'ticket': 'OPS-120',
                                                         'slack': {'at': '2026-10-25T08:00:00Z', 'line': 'one client, 40 lines'}})

    # when
    text = prompt.card_brief(card, 'https://skopax.example.com')

    # then
    assert '  seen before: 3 times, last 24 Oct 11:05 UTC, ended In progress, ticket OPS-120; check past_cards before a deep dive' in text
    assert '  asked in Slack 25 Oct: one client, 40 lines' in text


def test_the_brief_and_past_cards_say_how_an_earlier_occurrence_was_resolved(store: Store):
    # given
    how = {'kind': 'fixed', 'ref': 'OPS-120', 'by': 'nina.white@example.com', 'via': 'page', 'at': ago(DAY)}
    fixed = anomaly(KEY, firstSeen=ago(600), seenBefore={'count': 1, 'lastAt': '2026-10-24T11:05:00Z', 'outcome': 'fixed', 'resolution': how})
    old = anomaly(KEY, firstSeen=ago(600), seenBefore={'count': 1, 'lastAt': '2026-10-24T11:05:00Z', 'outcome': 'handled'})
    store.card_history.insert_one(past(KEY, 2, outcome='fixed', resolution=how))

    # when
    brief, legacy = prompt.card_brief(fixed, 'https://skopax.example.com'), prompt.card_brief(old, 'https://skopax.example.com')
    [row], _ = call(box(store, keys=(KEY,)), {'key': KEY})

    # then
    assert 'seen before: once, last 24 Oct 11:05 UTC, ended Fixed (OPS-120) · via page · nina.white;' in brief
    assert 'ended Resolved;' in legacy, 'an occurrence resolved before the kinds existed reads Resolved'
    assert (row['outcomeText'], row['resolution']) == ('Fixed (OPS-120) · via page · nina.white', how)


def test_no_seen_before_line_for_a_first_timer():
    # when
    text = prompt.card_brief(anomaly(KEY, firstSeen=ago(600)), 'https://skopax.example.com')

    # then
    assert 'seen before' not in text and 'asked in Slack' not in text


def run_doc(run_id: str, hours: float, keys=(), question: str | None = None, recap: str = 'answer', calls=(), outcome: str = 'done', trigger: str = 'slack') -> dict:
    return {'_id': run_id, 'startedAt': ago(hours * 3600), 'trigger': trigger, 'keys': list(keys), 'outcome': outcome, 'recap': recap,
            **({'ask': {'text': question, 'by': 'adam.baker'}} if question else {}), 'toolCalls': list(calls)}


def tool_call(name: str, query: str, rows=3, error=None) -> dict:
    return {'name': name, 'input': json.dumps({'query': query}), 'rows': rows, 'truncated': False, 'error': error, 'ms': 40}


def runs(b: Toolbox, args: dict) -> tuple[list[dict], bool]:
    out, err = b.call('past_runs', args)
    return (json.loads(out) if not err else out), err


def test_past_runs_finds_earlier_questions_and_the_searches_that_found_rows(store: Store):
    # given
    found = tool_call('promql', 'FROM logs-reports-2026.11.0* | WHERE message LIKE "*W005*"')
    store.investigations.insert_many([
        run_doc('r1', 30, question='only the ip not defined error W005', recap='W005 last seen 3 Nov 23:01 UTC, 314 lines',
                calls=[tool_call('promql', 'FROM x | WHERE a', rows=0), found, tool_call('promql', 'FROM y', error='400'), tool_call('card', 'k')]),
        run_doc('r2', 2, keys=[KEY], question='when did this start', recap='ShipCorp 456 since 1 Nov'),
        run_doc('r3', 1, keys=['5xx|reports/reports-service'], recap='TrackHub 503s', trigger='cards'),
        run_doc('r4', 0.5, question='W005 again?', outcome='failed'),
        run_doc('now', 0.1, question='W005 now?'),
    ])

    # when
    by_text, _ = runs(box(store, run_id='now'), {'text': 'w005'})
    by_service, _ = runs(box(store), {'service': 'reports-service', 'limit': 1})
    by_key, _ = runs(box(store), {'key': KEY})
    nothing = box(store).call('past_runs', {})

    # then
    assert by_text == [{'id': 'r1', 'at': ago(30 * 3600), 'trigger': 'slack', 'question': 'only the ip not defined error W005', 'by': 'adam.baker',
                        'answer': 'W005 last seen 3 Nov 23:01 UTC, 314 lines', 'queries': [{'tool': 'promql', 'input': found['input'], 'rows': 3}]}]
    assert [r['id'] for r in by_service] == ['r3'] and [r['id'] for r in by_key] == ['r2']
    assert nothing == ('give at least one of key, service or text', True)


def test_past_runs_is_offered_to_slack_asks_and_the_knowledge_points_to_it(store: Store):
    # given
    slack = Toolbox(sources(), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), [], 'live', NOW, 'https://skopax.example.com', read_only=True)

    # when
    how = prompt.system_prompt().split('## How to work', 1)[1].split('\n## ', 1)[0]

    # then
    assert 'past_runs' in {t['name'] for t in slack.definitions()}
    assert 'past_runs' in how and 'reuse' in how


def test_what_people_said_in_the_thread_comes_back_with_the_card(store: Store):
    # given
    store.card_history.insert_one(past(KEY, 1, said=[{'at': ago(3600), 'by': 'gary.cole@example.com', 'text': 'ShipCorp added the IP, fixed now', 'kind': 'resolve', 'agreed': True}]))

    # when
    rows, _ = call(box(store, keys=(KEY,)), {'key': KEY})

    # then
    assert rows[0]['said'] == [{'at': ago(3600), 'by': 'gary.cole@example.com', 'text': 'ShipCorp added the IP, fixed now', 'kind': 'resolve', 'agreed': True}]
