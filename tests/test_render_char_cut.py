import json

from skopax.investigator.tools import READ_LIMIT_CHARS, READ_LIMIT_ROWS, render


def rows(count: int) -> list[dict]:
    return [{'m': 'x' * 289} for _ in range(count)]


def test_a_list_cut_by_characters_says_how_many_rows_came_through_whole():
    # given
    out = rows(90)
    assert len(json.dumps(out[:66])) <= READ_LIMIT_CHARS < len(json.dumps(out[:67]))

    # when
    text, count, cut = render(out)

    # then
    assert (count, cut) == (90, True)
    assert text.endswith(f'\n[truncated: cut at {READ_LIMIT_CHARS:,} characters, so only the first 66 of 90 rows are whole; narrow the query]'), text[-200:]
    assert 'showing the first 90 of 90 rows' not in text


def test_a_list_cut_by_characters_past_the_row_limit_counts_the_whole_rows_too():
    # when
    text, count, cut = render(rows(READ_LIMIT_ROWS + 100))

    # then
    assert (count, cut) == (READ_LIMIT_ROWS + 100, True)
    assert text.endswith(f'only the first 66 of {READ_LIMIT_ROWS + 100} rows are whole; narrow the query]'), text[-200:]


def test_a_list_cut_only_by_rows_and_a_long_text_keep_their_notes():
    # when
    listed, _, listed_cut = render([{'m': 'm'} for _ in range(READ_LIMIT_ROWS + 50)])
    long, long_rows, long_cut = render('y' * (READ_LIMIT_CHARS + 10))
    short, short_rows, short_cut = render([{'m': 'm'}])

    # then
    assert listed_cut and listed.endswith(f'\n[truncated: showing the first {READ_LIMIT_ROWS} of {READ_LIMIT_ROWS + 50} rows; narrow the query]')
    assert long_cut and long_rows is None and long.endswith(f'\n[truncated: cut at {READ_LIMIT_CHARS:,} characters; narrow the query]')
    assert (short, short_rows, short_cut) == ('[{"m": "m"}]', 1, False)
