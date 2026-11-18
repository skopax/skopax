import json
from urllib.parse import parse_qs, urlsplit

import pytest

from skopax.contract import ReadError
from skopax.messages import msg
from skopax.remote import HttpError
from skopax.store import Store
from tests.test_confluence_plugin import SPACE_LIST, connection_of
from tests.test_confluence_tool import PAGE, PAGE_CONTENT, SEARCH, SPACES, TENANT_INFO, TOKEN, call, fake, fresh_cloud_ids, wiki, wiki_box

SCOPED = 'Confluence refused to read search results (HTTP 403): the token lacks the scope search:confluence'
UNSEEN_OPS = 'Confluence shows the account no space OPS: the keys are wrong, or the account lacks the View permission on them'


def listing(*keys: str) -> dict:
    return {'results': [{'key': k, 'name': k} for k in keys], 'size': len(keys)}


def no_hits(query, body):
    return {'results': [], 'totalSize': 0}


def test_a_refused_read_names_the_part_and_the_scope_and_never_echoes_the_server(store: Store):
    # given
    echoed = json.dumps({'message': f'token {TOKEN} is not valid; scope does not match'})
    forbidden = wiki_box(store, fake({('GET', SEARCH): HttpError(403, SEARCH, echoed)}))
    unauthorized = wiki_box(store, fake({('GET', SEARCH): HttpError(401, SEARCH, echoed)}))
    pages = fake({('GET', PAGE): HttpError(403, PAGE)}, page={k: v for k, v in PAGE_CONTENT.items() if k != 'body'})

    # when
    refused = call(forbidden, op='search', text='runbook')
    invalid = call(unauthorized, op='search', text='runbook')
    with pytest.raises(ReadError) as page:
        wiki(pages).page('123456')

    # then
    assert refused == (SCOPED, True)
    assert invalid == ('Confluence refused to read search results (HTTP 401): the token is invalid or lacks the scope search:confluence', True)
    assert (page.value.status, page.value.message) == (403, 'Confluence refused to read pages (HTTP 403): the token lacks the scope read:page:confluence')
    assert TOKEN not in json.dumps([refused, invalid, page.value.message]) and 'not valid' not in refused[0]


def test_a_404_or_a_missing_page_names_the_view_space_permission(store: Store):
    # given
    missing = wiki_box(store, fake(page=None))
    gone = fake({('GET', PAGE): HttpError(404, PAGE)}, page={k: v for k, v in PAGE_CONTENT.items() if k != 'body'})

    # when
    page = call(missing, op='page', id='123456')
    with pytest.raises(ReadError) as v2:
        wiki(gone).page('123456')

    # then
    assert page == ('Confluence found no page 123456: it does not exist, or the account lacks the View permission on its space', True)
    assert (v2.value.status, v2.value.message) == (404, 'Confluence found nothing at /api/v2/pages/123456: it does not exist, or the account lacks the View permission on its space')


def test_a_refused_or_keyless_space_read_names_read_space_and_not_a_wrong_space():
    # given
    bare = {k: v for k, v in PAGE_CONTENT.items() if k != 'body'}
    refused = fake({('GET', f'{SPACES}/98765'): HttpError(403, SPACES)}, page=bare)
    keyless = fake({('GET', f'{SPACES}/98765'): {'id': '98765', 'name': 'Docs'}}, page=bare)

    # when
    errors = []
    for http in (refused, keyless):
        with pytest.raises(ReadError) as e:
            wiki(http).page('123456')
        errors.append((e.value.status, e.value.message))

    # then
    assert errors == [(403, 'Confluence refused to read spaces (HTTP 403): the token lacks the scope read:space:confluence'),
                      (403, 'Confluence did not say which space page 123456 is in, so it is not shown: the token may lack the scope read:space:confluence')]


def test_an_empty_search_reads_the_spaces_and_names_the_ones_the_account_cannot_see(store: Store):
    # given
    some = fake({('GET', SEARCH): no_hits, ('GET', SPACE_LIST): listing('DOCS')})
    every = fake({('GET', SEARCH): no_hits, ('GET', SPACE_LIST): listing('DOCS', 'OPS')})
    refused = fake({('GET', SEARCH): no_hits, ('GET', SPACE_LIST): HttpError(403, SPACE_LIST)})

    # when
    partly, partly_err = call(wiki_box(store, some, spaces=('DOCS', 'OPS')), op='search', text='runbook')
    asked, asked_err = call(wiki_box(store, some, spaces=('DOCS', 'OPS')), op='search', text='runbook', space='OPS')
    clean, clean_err = call(wiki_box(store, every, spaces=('DOCS', 'OPS')), op='search', text='runbook')
    unknown, unknown_err = call(wiki_box(store, refused, spaces=('DOCS', 'OPS')), op='search', text='runbook')

    # then
    assert not partly_err and partly['hits'] == [] and partly['note'] == UNSEEN_OPS
    assert parse_qs(urlsplit(some.hits(SPACE_LIST)[0][1]).query) == {'keys': ['DOCS,OPS'], 'limit': ['2']}
    assert not asked_err and asked['hits'] == [] and asked['note'] == UNSEEN_OPS, 'a space the account cannot see is named, never an empty result'
    assert not clean_err and clean['hits'] == [] and 'note' not in clean
    assert not unknown_err and unknown['hits'] == [] and unknown['note'] == 'Confluence refused to read spaces (HTTP 403): the token lacks the scope read:space:confluence'


@pytest.mark.parametrize('over, summary', [
    ({('GET', SPACE_LIST): HttpError(403, SPACE_LIST)}, 'Confluence refused to read spaces (HTTP 403): the token lacks the scope read:space:confluence'),
    ({('GET', SPACE_LIST): HttpError(401, SPACE_LIST)}, msg('connection.rejected', status=401)),
    ({('GET', SPACE_LIST): HttpError(401, SPACE_LIST, '{"code": 401, "message": "Unauthorized; scope does not match"}')},
     'Confluence refused to read spaces (HTTP 401): the token lacks the scope read:space:confluence'),
    ({('GET', SPACE_LIST): listing()}, 'Confluence shows the account no space DOCS: the keys are wrong, or the account lacks the View permission on them'),
    ({('GET', SEARCH): HttpError(403, SEARCH)}, SCOPED),
    ({('GET', SEARCH): HttpError(401, SEARCH)}, 'Confluence refused to read search results (HTTP 401): the token lacks the scope search:confluence'),
])
def test_the_connection_test_reads_the_allowed_spaces_and_search_and_names_the_scope(over, summary):
    # given
    http = fake({('GET', SPACE_LIST): listing('DOCS'), ('GET', SEARCH): no_hits} | over)

    # when
    result = connection_of(http)

    # then
    assert (result.ok, result.summary) == (False, summary)


def test_the_connection_test_passes_when_the_allowed_spaces_and_search_answer():
    # given
    http = fake({('GET', SPACE_LIST): listing('DOCS'), ('GET', SEARCH): no_hits})

    # when
    result = connection_of(http)

    # then
    assert (result.ok, result.summary) == (True, 'Confluence at example.atlassian.net')
    assert [c[1].split('?')[0] for c in http.calls] == [TENANT_INFO, SPACE_LIST, SEARCH]
    assert parse_qs(urlsplit(http.calls[1][1]).query) == {'keys': ['DOCS'], 'limit': ['1']}
    assert SPACE_LIST.endswith('/wiki/api/v2/spaces')
    assert parse_qs(urlsplit(http.calls[2][1]).query) == {'cql': ['type = page AND space in ("DOCS")'], 'limit': ['1']}


def test_a_page_found_by_search_without_its_space_names_read_space_and_not_a_wrong_space():
    # given
    bodied = {k: v for k, v in PAGE_CONTENT.items() if k != 'space'}

    # when
    with pytest.raises(ReadError) as e:
        wiki(fake(page=bodied)).page('123456')

    # then
    assert (e.value.status, e.value.message) == (403, 'Confluence did not say which space page 123456 is in, so it is not shown: the token may lack the scope read:space:confluence')
