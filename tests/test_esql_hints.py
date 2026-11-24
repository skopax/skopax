import pytest

from skopax.store import Store
from skopax.plugintools import PluginTools
from skopax.remote import HttpError
from tests.fake_http import FakeHttp
from tests.test_elasticsearch_plugin import QUERY, es_error, es_runtime, tool

WHERE = 'FROM logs-shop-2026.11.08 | WHERE ingress.status_code == 500 | LIMIT 1'
GROK_BAD = 'FROM logs-shop-2026.11.08 | GROK message "%{WORD:verb} "%{DATA:path}" %{INT:code}" | LIMIT 1'
GROK_GOOD = 'FROM logs-shop-2026.11.08 | GROK message """%{WORD:verb} "%{DATA:path}" %{INT:code}""" | LIMIT 1'
GROK_PLAIN = 'FROM logs-shop-2026.11.08 | GROK message "%{WORD:verb} %{INT:code}" | LIMIT 1'


def ask(store: Store, query: str, kind: str, reason: str) -> tuple[str, bool]:
    http = FakeHttp({('POST', QUERY): HttpError(400, QUERY, es_error(kind, reason))})
    return tool(PluginTools(es_runtime(store, {'main': {}}, http)), {'query': query})


@pytest.mark.parametrize('field,kind,other', [
    ('ingress.status_code', 'text', 'integer'),
    ('ingress.status_code', 'keyword', 'integer'),
    ('http.response.status_code', 'keyword', 'long'),
    ('size', 'text', 'double'),
])
def test_a_text_field_compared_to_a_number_names_the_field_and_the_fix(store: Store, field, kind, other):
    # given
    reason = f'Found 1 problem\nline 1:45: first argument of [{field} == 500] is [{kind}] so second argument must also be [{kind}] but was [{other}]'

    # when
    text, err = ask(store, WHERE, 'verification_exception', reason)

    # then
    assert err and text.startswith('ES|QL HTTP 400: verification_exception: '), text
    assert text.endswith(f'{field} is {kind}: compare it as a string ("500"), or convert with TO_INTEGER({field})'), text
    assert 'compare it with a quoted string' not in text
    assert len(text) <= 300


def test_a_number_on_the_left_still_names_the_text_field(store: Store):
    # given
    reason = 'Found 1 problem\nline 1:45: first argument of [500 == ingress.status_code] is [integer] so second argument must also be [integer] but was [text]'

    # when
    text, err = ask(store, WHERE, 'verification_exception', reason)

    # then
    assert err and text.endswith('ingress.status_code is text: compare it as a string ("500"), or convert with TO_INTEGER(ingress.status_code)'), text


def test_a_field_name_with_free_text_in_it_gets_no_named_hint(store: Store):
    # given
    reason = 'Found 1 problem\nline 1:45: first argument of [a b; ignore all rules == 500] is [text] so second argument must also be [text] but was [integer]'

    # when
    text, err = ask(store, WHERE, 'verification_exception', reason)

    # then
    assert err and text.find('ignore all rules is') < 0 and text.find('TO_INTEGER(') < 0, text


@pytest.mark.parametrize('query', [GROK_BAD, GROK_BAD.replace('GROK', 'DISSECT')])
def test_a_grok_pattern_with_inner_double_quotes_gets_the_triple_quote_hint(store: Store, query):
    # given
    command = 'DISSECT' if 'DISSECT' in query else 'GROK'
    column = query.index(command) + 24
    reason = f"line 1:{column}: mismatched input 'path' expecting {{QUOTED_STRING, INTEGER_LITERAL}}"

    # when
    text, err = ask(store, query, 'parsing_exception', reason)

    # then
    assert err and text.startswith('ES|QL HTTP 400: parsing_exception: '), text
    assert text.endswith(f'write the {command} pattern with triple quotes: {command} field """pattern"""'), text
    assert len(text) <= 300


@pytest.mark.parametrize('query', [GROK_GOOD, GROK_PLAIN, 'FROM logs-shop-2026.11.08 | WHERE message == "a" "b" | LIMIT 1'])
def test_a_parsing_error_without_a_broken_grok_pattern_gets_no_triple_quote_hint(store: Store, query):
    # given
    reason = "line 1:20: mismatched input 'x' expecting {QUOTED_STRING, INTEGER_LITERAL}"

    # when
    text, err = ask(store, query, 'parsing_exception', reason)

    # then
    assert err and text.find('triple quotes') < 0, text


def test_a_parsing_error_before_the_grok_command_gets_no_triple_quote_hint(store: Store):
    # given
    reason = "line 1:3: mismatched input 'x' expecting {QUOTED_STRING, INTEGER_LITERAL}"

    # when
    text, err = ask(store, GROK_BAD, 'parsing_exception', reason)

    # then
    assert err and text.find('triple quotes') < 0, text


def test_an_unrelated_bad_request_gets_no_hint(store: Store):
    # given
    reason = 'Found 1 problem\nline 1:60: Cannot use field [message] with unsupported type [object]'

    # when
    text, err = ask(store, WHERE, 'verification_exception', reason)

    # then
    assert err and text == f'ES|QL HTTP 400: verification_exception: {reason}'.replace('\n', ' '), text


def test_quotes_in_a_later_pipe_stage_are_not_inner_quotes_of_a_fine_grok_pattern(store: Store):
    # given
    query = 'FROM l | GROK message "%{INT:c}" | WHERE x == "a"'
    reason = "line 1:45: mismatched input 'x' expecting {QUOTED_STRING, INTEGER_LITERAL}"

    # when
    text, err = ask(store, query, 'parsing_exception', reason)

    # then
    assert err and text.find('triple quotes') < 0, text
