import time

import pytest

from skopax.contract import Field
from skopax.errkind import InvalidInstance
from skopax.instances import _check
from skopax.kit import options


def test_pairs_read_key_value_entries_and_name_the_ones_they_cannot_read():
    # when
    found, bad = options.pairs('a=1, b = two ,c, =d, e=, a=9')

    # then
    assert found == {'a': '1', 'b': 'two'}
    assert bad == ['c', '=d', 'e=', 'a=9']


def test_an_empty_or_missing_value_reads_as_nothing():
    # then
    assert options.pairs('') == ({}, []) and options.pairs(None) == ({}, []) and options.entries(' , ,') == []


@pytest.mark.parametrize('raw', ['', '   ', '(', '[a-', 'a' * 201, '*a', None])
def test_a_pattern_that_is_empty_broken_or_too_long_does_not_compile(raw):
    # then
    assert options.compiled(raw) is None


def test_a_slow_pattern_is_cut_off_and_never_matches():
    # given
    pattern = options.compiled('(a|aa)+$')
    started = time.monotonic()

    # when
    found = options.search(pattern, 'a' * 60 + '!')

    # then
    assert pattern is not None and found is None
    assert time.monotonic() - started < 1
    with pytest.raises(TimeoutError):
        pattern.search('a' * 60 + '!', timeout=options.MATCH_S)


def test_a_pattern_that_backtracks_badly_is_found_by_the_probe_and_a_plain_one_is_not():
    # then
    assert options.slow(options.compiled('(a|aa)+$')) is True
    assert options.slow(options.compiled('body=.*')) is False and options.slow(options.compiled(r'\d{3,}-\w+')) is False


def test_a_guard_stops_matching_after_its_first_timeout_so_many_texts_cost_one_timeout():
    # given
    guard = options.Guard(options.compiled('(a|aa)+$'))
    started = time.monotonic()

    # when
    found = [options.search(guard, 'a' * 60 + '!') for _ in range(500)]

    # then
    assert found == [None] * 500 and guard.timed_out is True
    assert time.monotonic() - started < 1
    assert options.search(options.Guard(options.compiled('b')), 'abc') is not None


def test_a_match_is_looked_for_in_the_first_characters_only():
    # given
    pattern = options.compiled('needle')

    # then
    assert options.search(pattern, 'x' * 100 + 'needle') is not None
    assert options.search(pattern, 'x' * (options.MATCH_CHARS + 10) + 'needle') is None


@pytest.mark.parametrize('text, ok', [('0', False), ('-1', False), ('nan', False), ('inf', False), ('abc', False), ('2.5', True), (' 3 ', True)])
def test_a_number_option_is_a_finite_positive_number(text, ok):
    # then
    assert (options.number(text) is not None) is ok


def test_a_regex_field_is_compiled_when_it_is_saved():
    # given
    field = Field('mask', 'Mask', 'regex')

    # then
    assert _check(field, r'body=\{.*\}') == r'body=\{.*\}'
    for bad in ('(', 'a' * 201):
        with pytest.raises(InvalidInstance):
            _check(field, bad)
