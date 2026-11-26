import pytest

from skopax import mcpserver
from skopax.store import Store
from tests.test_mcp import NINA, call, mcp_client, token, rpc

VIEWER = ('SKOPAX_MCP',)


def reviews(store: Store, *rows: dict) -> None:
    for row in reversed(rows):
        store.add_self_review(row)


def test_get_self_review_is_a_read_only_idempotent_viewer_tool_listed_in_the_instructions(store: Store):
    # given
    tool = next(t for t in mcpserver.TOOLS if t.name == 'get_self_review')

    # when
    with mcp_client(store) as c:
        listed = {t['name'] for t in rpc(c, 'tools/list', bearer=token(roles=VIEWER)).json()['result']['tools']}

    # then
    assert tool.annotations.readOnlyHint and tool.annotations.idempotentHint and not tool.annotations.destructiveHint
    assert mcpserver.TOOL_ROLES['get_self_review'] == 'viewer'
    assert 'get_self_review' in listed
    assert 'get_self_review' in mcpserver.instructions('')


def test_the_newest_review_is_returned_by_default_with_its_date_and_a_link(store: Store):
    # given
    reviews(store, {'at': '2026-11-26T05:00:00Z', 'by': 'server', 'text': 'Noise went down. 3 cards re-fired.', 'dm': []},
            {'at': '2026-11-25T05:00:00Z', 'by': 'server', 'text': 'Yesterday was calm.', 'dm': []})

    # when
    with mcp_client(store) as c:
        text, failed = call(c, token(roles=VIEWER), 'get_self_review')

    # then
    assert not failed
    assert '2026-11-26' in text and 'Noise went down. 3 cards re-fired.' in text
    assert 'Yesterday was calm.' not in text
    assert '#t=review' in text


def test_a_date_picks_that_day_and_a_day_without_a_review_says_so(store: Store):
    # given
    reviews(store, {'at': '2026-11-26T05:00:00Z', 'by': 'server', 'text': 'Latest.', 'dm': []}, {'at': '2026-11-25T05:00:00Z', 'by': 'server', 'text': 'Older.', 'dm': []})

    # when
    with mcp_client(store) as c:
        older, _ = call(c, token(roles=VIEWER), 'get_self_review', {'date': '2026-11-25'})
        none, _ = call(c, token(roles=VIEWER), 'get_self_review', {'date': '2026-11-20'})

    # then
    assert 'Older.' in older and 'Latest.' not in older
    assert 'No self-review for 2026-11-20' in none


def test_limit_returns_the_latest_n_and_is_capped_at_seven(store: Store):
    # given
    reviews(store, *({'at': f'2026-11-{d:02d}T05:00:00Z', 'by': 'server', 'text': f'Review of day {d}.', 'dm': []} for d in range(26, 14, -1)))

    # when
    with mcp_client(store) as c:
        three, _ = call(c, token(roles=VIEWER), 'get_self_review', {'limit': 3})
        too_many = rpc(c, 'tools/call', {'name': 'get_self_review', 'arguments': {'limit': 8}}, token(roles=VIEWER)).json()['result']

    # then
    assert [f'Review of day {d}.' in three for d in (26, 25, 24, 23)] == [True, True, True, False]
    assert too_many['isError'] is True


def test_a_bad_date_is_refused_and_no_review_at_all_says_so(store: Store):
    # given
    with mcp_client(store) as c:
        bad = rpc(c, 'tools/call', {'name': 'get_self_review', 'arguments': {'date': 'yesterday'}}, token(roles=VIEWER)).json()['result']
        empty, failed = call(c, token(roles=VIEWER), 'get_self_review')

    # then
    assert bad['isError'] is True
    assert not failed and 'No self-review yet' in empty


def test_findings_are_shown_with_their_kind_and_state_when_the_review_holds_them(store: Store):
    # given
    reviews(store, {'at': '2026-11-26T05:00:00Z', 'by': 'server', 'text': 'Two items.', 'dm': [],
                    'findings': [{'text': 'esql lists no columns', 'kind': 'code', 'state': 'open'}, {'text': 'Mute noisy cards', 'kind': 'no code', 'state': 'done'}]})

    # when
    with mcp_client(store) as c:
        text, _ = call(c, token(roles=VIEWER), 'get_self_review')

    # then
    assert '- code \u00b7 open \u00b7 esql lists no columns' in text
    assert '- no code \u00b7 done \u00b7 Mute noisy cards' in text


def test_the_review_text_goes_out_masked(store: Store):
    # given
    reviews(store, {'at': '2026-11-26T05:00:00Z', 'by': 'server', 'text': 'A client wrote to alice.smith@example.com and used Bearer abcdefghijklmnopqrstuvwxyz0123456789', 'dm': []})

    # when
    with mcp_client(store) as c:
        text, _ = call(c, token(roles=VIEWER), 'get_self_review')

    # then
    assert 'alice.smith@example.com' not in text and 'abcdefghijklmnopqrstuvwxyz0123456789' not in text


def test_the_date_argument_is_described_as_the_utc_date():
    # given
    tool = next(t for t in mcpserver.TOOLS if t.name == 'get_self_review')

    # then
    assert 'UTC date' in tool.description


@pytest.mark.parametrize('limit', ['abc', [], {}, 0, -3, 99])
def test_a_bad_limit_gives_a_plain_argument_error_not_a_crash_name(store: Store, limit):
    # given
    reviews(store, {'at': '2026-11-26T05:00:00Z', 'by': 'server', 'text': 'Latest.', 'dm': []})

    # when
    with mcp_client(store) as c:
        reply = rpc(c, 'tools/call', {'name': 'get_self_review', 'arguments': {'limit': limit}}, token(roles=VIEWER)).json()['result']

    # then
    assert reply['isError'] is True and 'ValueError' not in str(reply) and 'TypeError' not in str(reply)


def test_the_finding_kind_and_state_are_masked_and_clipped_like_the_text(store: Store):
    # given
    reviews(store, {'at': '2026-11-26T05:00:00Z', 'by': 'server', 'text': 'Latest.', 'dm': [],
                    'findings': [{'kind': 'alice@example.com ' + 'k' * 500, 'state': 'bob@example.com ' + 's' * 500, 'text': 'fine'}]})

    # when
    with mcp_client(store) as c:
        text, failed = call(c, token(roles=VIEWER), 'get_self_review')

    # then
    line = next(x for x in text.splitlines() if x.startswith('- '))
    assert not failed and 'alice@example.com' not in text and 'bob@example.com' not in text and len(line) < 400


@pytest.mark.parametrize('limit', ['abc', [], {}, 0, -3, 99, True, float('nan')])
def test_the_handler_itself_refuses_a_bad_limit_with_a_plain_tool_error(store: Store, limit):
    # given
    from skopax.mcpserver import Tools, ToolError, McpUser
    reviews(store, {'at': '2026-11-26T05:00:00Z', 'by': 'server', 'text': 'Latest.', 'dm': []})
    tools = Tools.__new__(Tools)
    tools.store = store
    tools.site = lambda: 'https://example.com'

    # when
    with pytest.raises(ToolError) as refused:
        tools.get_self_review(None, {'limit': limit})

    # then
    assert 'whole number from 1 to 7' in str(refused.value)
