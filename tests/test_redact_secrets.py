import time

import pytest

from skopax.redact import pipe_columns_masked, redact, redact_secrets, secrets_masked

MARKDOWN = '| user | password |\n|---|---|\n| app | Tbl-Pa55 |\n| ops | Ops-Pa55 |\nafter the table'
HTML = '<table><tr><th>user</th><th>password</th></tr><tr><td>app</td><td>Tbl-Pa55</td></tr></table>'
KUBECTL = ('kubectl -n shop get secret db-creds -o yaml | grep host\n'
           'kubectl -n shop rollout restart deploy/checkout-service | tee restart.log\n'
           'kubectl -n shop logs deploy/checkout-service | grep -c ERROR')


@pytest.mark.parametrize('line, secret', [('https://ops:Pa$$w0rd@db.example.com/x', 'Pa$$'), ('postgres://admin:S3cr#t@db-1.internal:5432/app', 'S3cr#'),
                                          ('https://ops:p%40ss!w@db.example.com', 'p%40ss')])
def test_a_password_in_a_url_is_gone_whatever_its_characters_and_host(line, secret):
    # when
    out = redact_secrets(line)

    # then
    assert secret not in out and '://***@' in out


def test_a_markdown_table_loses_the_cells_under_a_secret_header():
    # when
    out = secrets_masked(MARKDOWN)

    # then
    assert out == '| user | password |\n|---|---|\n| app | [redacted] |\n| ops | [redacted] |\nafter the table'
    assert 'Tbl-Pa55' not in redact_secrets(MARKDOWN)


def test_a_raw_html_table_loses_the_cells_under_a_secret_header():
    # when
    out = secrets_masked(HTML)

    # then
    assert out == '<table><tr><th>user</th><th>password</th></tr><tr><td>app</td><td>[redacted]</td></tr></table>'
    assert 'Tbl-Pa55' not in redact_secrets(HTML)


def test_a_pipe_table_ends_at_the_first_line_without_a_pipe():
    # given
    text = '| user | password |\n|---|---|\n| app | P1-Pa55 |\n\n| host | port |\n| db-1 | 5432 |'

    # when
    out = secrets_masked(text)

    # then
    assert out == '| user | password |\n|---|---|\n| app | [redacted] |\n\n| host | port |\n| db-1 | 5432 |'


def test_a_shell_pipeline_is_not_a_table():
    # when
    out = [secrets_masked(KUBECTL), redact_secrets(KUBECTL)]

    # then
    assert out == [KUBECTL, KUBECTL]


def test_a_pipe_line_above_a_markdown_table_does_not_take_its_header():
    # given
    text = 'Credentials | see below\n| user | password |\n|---|---|\n| app | Pre-Pa55 |'

    # when
    out = pipe_columns_masked(text)

    # then
    assert out == 'Credentials | see below\n| user | password |\n|---|---|\n| app | [redacted] |'
    assert 'Pre-Pa55' not in secrets_masked(text) and 'Pre-Pa55' not in redact_secrets(text)


def test_pipe_text_without_a_delimiter_row_is_left_alone():
    # given
    bare = 'user | password\napp | Pa55-word'
    lone_bar = '| password |\n|\n| x |'

    # when
    out = [pipe_columns_masked(bare), pipe_columns_masked(lone_bar)]

    # then
    assert out == [bare, lone_bar]


def test_rendered_reader_rows_and_masked_tables_come_out_unchanged():
    # given
    rendered = 'Account | Password\n\napp | db-1 | [redacted]\n\nStaging\n\nops | db-2 | [redacted]'
    plain_rows = 'host | port\ndb-1 | 5432'

    # when
    again = secrets_masked(secrets_masked(MARKDOWN))

    # then
    assert secrets_masked(rendered) == rendered and secrets_masked(plain_rows) == plain_rows
    assert again == secrets_masked(MARKDOWN)


def test_redact_secrets_masks_the_secrets_before_the_personal_data():
    # given
    sample = ('mail alice@example.com, db https://ops:Pa$$w0rd@db.example.com/x, password=Hunter22\n'
              '| user | api key |\n|---|---|\n| bob@example.com | K-77 |\n\n<password>Nexus-Pa55</password>\n' + HTML)

    # when
    out = redact_secrets(sample)

    # then
    assert out == redact(secrets_masked(sample))
    for leaked in ('alice@example.com', 'bob@example.com', 'Pa$$', 'Hunter22', 'K-77', 'Nexus-Pa55', 'Tbl-Pa55'):
        assert leaked not in out


def test_the_table_passes_stay_linear_on_long_input():
    # given
    shapes = ['|' * 300_000, '| password |\n|---|\n' + '| x |\n' * 100_000, '<table><tr><th>password</th></tr>' + '<tr><td>x</td></tr>' * 50_000,
              '<table>' * 100_000, '<td>' * 100_000 + 'x', '| a |\n|---|\n' * 50_000]

    # when
    started = time.perf_counter()
    out = [secrets_masked(s) for s in shapes]
    took = time.perf_counter() - started

    # then
    assert took < 3.0
    assert out[0] == shapes[0] and out[3] == shapes[3]


@pytest.mark.parametrize('kept', ['0.4242424242424242', '{"metric": {"job": "api"}, "value": [1792224000.123, "0.4242424242424242"]}', '42424242424242.42',
                                  '17922240004242424242424242'], ids=['decimal', 'promql line', 'whole part of a decimal', 'longer number'])
def test_a_luhn_valid_run_inside_a_decimal_or_a_longer_number_is_kept(kept):
    # then
    assert redact_secrets(kept) == kept


@pytest.mark.parametrize('card', ['4242424242424242', '4242 4242 4242 4242', '4242-4242-4242-4242'], ids=['plain', 'spaces', 'dashes'])
def test_a_card_number_on_its_own_is_still_masked(card):
    # then
    assert redact_secrets(f'paid with {card} today') == 'paid with [redacted] today'
    assert redact_secrets(f'card {card}.') == 'card [redacted].', 'a sentence end is not a decimal'
