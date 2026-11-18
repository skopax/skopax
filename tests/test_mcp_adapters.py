import json
import logging

import pytest
from mcp import types

from skopax.errkind import text_of
from skopax.messages import msg
from skopax.mcptools import FOLDERS_MAX
from skopax.mcptools.adapters import ADAPTERS, Scope, ScopeRefused, balanced

DRIVE = ADAPTERS['drive']
GITHUB = ADAPTERS['github']
FOLDER = Scope('f1', 'Runbooks', 'https://drive.google.com/drive/folders/f1')
REPO = Scope('shop/runbooks', 'shop/runbooks', 'https://github.com/shop/runbooks')
INSIDE = 'docInside01'
OUTSIDE = 'docOutside1'


def as_text(data) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(data))])


def errored(data) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(data))], isError=True)


def resource(uri: str, text: str) -> types.EmbeddedResource:
    return types.EmbeddedResource(type='resource', resource=types.TextResourceContents(uri=uri, mimeType='text/markdown', text=text))


def drive_file(file_id: str, parent: str | None, **extra) -> dict:
    found = {'id': file_id, 'title': f'{file_id} runbook', 'mimeType': 'application/vnd.google-apps.document', 'modifiedTime': '2026-11-01T10:00:00Z',
             'viewUrl': f'https://docs.google.com/document/d/{file_id}', 'contentSnippet': f'restart {file_id}', 'owners': ['alice@example.com']}
    return found | ({'parentId': parent} if parent is not None else {}) | extra


def code_item(full_name: str | None, path: str = 'docs/timeout.md', **extra) -> dict:
    found = {'name': path.rsplit('/', 1)[-1], 'path': path, 'sha': 'abc123', 'html_url': f'https://github.com/{full_name}/blob/main/{path}',
             'fragments': ['timeout after 30 s']}
    return found | ({'repository': {'full_name': full_name, 'private': True}} if full_name is not None else {}) | extra


class FakeCall:
    def __init__(self, answers: dict | None = None):
        self.answers = answers or {}
        self.seen: list[tuple[str, dict]] = []

    def __call__(self, tool: str, args: dict) -> types.CallToolResult:
        self.seen.append((tool, args))
        answer = self.answers.get(tool, as_text({}))
        return answer(args) if callable(answer) else answer

    def tools(self) -> list[str]:
        return [tool for tool, _ in self.seen]


def run(adapter, tool: str, args: dict, scope: Scope, call: FakeCall) -> tuple[str, int]:
    sent = adapter.before(tool, args, scope, call)
    return adapter.after(tool, call(tool, sent), scope)


def refused(adapter, tool: str, args: dict, scope: Scope, call: FakeCall) -> str:
    with pytest.raises(ScopeRefused) as caught:
        run(adapter, tool, args, scope, call)
    return caught.value.text


def none_kept(adapter, scope: Scope) -> str:
    return msg('mcptools.scope.none_kept', noun=adapter.noun, folder=scope.label)


def logged(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == 'skopax.mcptools' and r.levelno == logging.INFO]


@pytest.mark.parametrize('query, quote, expected', [
    ('', "'", True),
    ("title contains 'runbook'", "'", True),
    ("(title contains 'a') or (title contains 'b')", "'", True),
    ("title contains 'a) b'", "'", True),
    ("title contains 'it\\'s (x'", "'", True),
    ('"a) b"', '"', True),
    ('timeout "x \\" ) y"', '"', True),
    ("title contains 'a') or (title contains 'b'", "'", False),
    ('timeout) OR (secret', '"', False),
    ('(timeout', '"', False),
    ("x 'a\\\\') or (y 'b\\\\'", "'", False),
    ("title contains 'open", "'", False),
    ('timeout "open', '"', False),
    ('title contains "a) b"', "'", False),
    ("timeout 'a) b'", '"', False),
])
def test_balanced_counts_parentheses_outside_quoted_strings(query, quote, expected):
    # when
    found = balanced(query, quote)

    # then
    assert found is expected


@pytest.mark.parametrize('adapter, tool, args', [
    (DRIVE, 'search_files', {'query': "parentId = 'other'"}),
    (DRIVE, 'search_files', {'query': "title contains 'a' and PARENTID = 'other'"}),
    (DRIVE, 'search_files', {'query': "title contains 'a') or (title contains 'b'"}),
    (DRIVE, 'search_files', {'query': ['title contains a']}),
    (DRIVE, 'search_files', {'query': 'title contains "(" ) or (title contains ")"'}),
    (DRIVE, 'search_files', {'query': "title contains 'a' \\"}),
    (GITHUB, 'search_code', {'query': 'timeout repo:acme/secrets'}),
    (GITHUB, 'search_code', {'query': 'timeout org:acme'}),
    (GITHUB, 'search_code', {'query': 'timeout USER:bob'}),
    (GITHUB, 'search_code', {'query': 'timeout Owner:acme'}),
    (GITHUB, 'search_code', {'query': 'timeout) OR (secret'}),
    (GITHUB, 'search_code', {'query': 'timeout "open'}),
    (GITHUB, 'search_code', {'query': 7}),
    (GITHUB, 'search_code', {'query': '\\"a) b"'}),
    (GITHUB, 'get_file_contents', {'owner': 'other', 'path': 'README.md'}),
    (GITHUB, 'get_file_contents', {'repo': 'secrets', 'path': 'README.md'}),
    (GITHUB, 'get_file_contents', {'owner': '', 'path': 'README.md'}),
    (GITHUB, 'get_file_contents', {'path': '../../../other/secret/main/x.md', 'ref': 'main'}),
    (GITHUB, 'get_file_contents', {'path': '../x'}),
    (GITHUB, 'get_file_contents', {'path': 'a/../b'}),
    (GITHUB, 'get_file_contents', {'path': 'a/..'}),
    (GITHUB, 'get_file_contents', {'path': 'a.md', 'ref': 'main/..'}),
    (GITHUB, 'get_file_contents', {'path': 'docs\\..\\a.md'}),
    (GITHUB, 'get_file_contents', {'path': 'docs/%2e%2e/a.md'}),
    (GITHUB, 'get_file_contents', {'path': 'a.md', 'ref': '../../other/secret'}),
    (GITHUB, 'get_file_contents', {'path': 'a.md', 'sha': 'abc%2f..'}),
    (GITHUB, 'get_file_contents', {'path': ['a.md']}),
    (GITHUB, 'get_file_contents', {'path': 'a.md', 'ref': 7}),
], ids=['drive-parent', 'drive-parent-case', 'drive-breakout', 'drive-not-text', 'drive-double-quote', 'drive-backslash', 'github-repo', 'github-org',
        'github-user', 'github-owner', 'github-breakout', 'github-open-quote', 'github-not-text', 'github-backslash', 'contents-owner', 'contents-repo',
        'contents-empty-owner', 'contents-path-up', 'contents-path-parent', 'contents-path-middle', 'contents-path-end', 'contents-ref-end', 'contents-path-backslash', 'contents-path-percent', 'contents-ref-up',
        'contents-sha-percent', 'contents-path-not-text', 'contents-ref-not-text'])
def test_a_scope_widening_argument_is_refused(adapter, tool, args):
    # given
    call = FakeCall()
    scope = FOLDER if adapter is DRIVE else REPO

    # when
    text = refused(adapter, tool, args, scope, call)

    # then
    assert text == msg('mcptools.scope.widen')
    assert call.seen == []


def test_parentheses_inside_a_quoted_string_are_allowed():
    # given
    drive, github = FakeCall(), FakeCall()

    # when
    run(DRIVE, 'search_files', {'query': "title contains 'a) b'"}, FOLDER, drive)
    run(GITHUB, 'search_code', {'query': '"a) b"'}, REPO, github)

    # then
    assert drive.seen[0][1]['query'] == "parentId = 'f1' and (title contains 'a) b')"
    assert github.seen[0][1]['query'] == '("a) b") repo:shop/runbooks'


def test_quotes_and_backslashes_inside_a_string_are_allowed():
    # given
    drive, github = FakeCall(), FakeCall()

    # when
    run(DRIVE, 'search_files', {'query': 'title contains \'it\\\'s "on" C:\\\\ops\''}, FOLDER, drive)
    run(GITHUB, 'search_code', {'query': '"say \\"hi\\" C:\\\\ops" it\'s'}, REPO, github)

    # then
    assert drive.seen[0][1]['query'] == "parentId = 'f1' and (title contains 'it\\'s \"on\" C:\\\\ops')"
    assert github.seen[0][1]['query'] == '("say \\"hi\\" C:\\\\ops" it\'s) repo:shop/runbooks'


def test_the_scope_is_added_to_every_call():
    # given
    call = FakeCall()

    # when
    run(DRIVE, 'search_files', {'query': "title contains 'runbook'", 'pageSize': 500}, FOLDER, call)
    run(DRIVE, 'search_files', {}, FOLDER, call)
    run(DRIVE, 'search_files', {'query': '  ', 'pageSize': 20}, FOLDER, call)
    run(GITHUB, 'search_code', {'query': 'timeout', 'fields': ['path', 'fragments']}, REPO, call)
    run(GITHUB, 'search_code', {'query': 'timeout'}, REPO, call)
    run(GITHUB, 'get_file_contents', {'path': 'docs/timeout.md'}, REPO, call)
    run(GITHUB, 'get_file_contents', {'owner': 'SHOP', 'repo': 'Runbooks', 'path': 'docs/timeout.md', 'ref': 'main'}, REPO, call)

    # then
    assert call.seen == [
        ('search_files', {'query': "parentId = 'f1' and (title contains 'runbook')", 'pageSize': 50}),
        ('search_files', {'query': "parentId = 'f1'", 'pageSize': 50}),
        ('search_files', {'query': "parentId = 'f1'", 'pageSize': 20}),
        ('search_code', {'query': '(timeout) repo:shop/runbooks', 'fields': ['path', 'fragments', 'repository']}),
        ('search_code', {'query': '(timeout) repo:shop/runbooks'}),
        ('get_file_contents', {'path': 'docs/timeout.md', 'owner': 'shop', 'repo': 'runbooks'}),
        ('get_file_contents', {'owner': 'shop', 'repo': 'runbooks', 'path': 'docs/timeout.md', 'ref': 'main'}),
    ]


def test_two_dots_inside_a_name_pass():
    # given
    call = FakeCall()

    # when
    run(GITHUB, 'get_file_contents', {'path': 'notes..md'}, REPO, call)
    run(GITHUB, 'get_file_contents', {'path': 'a/b..c/d.md', 'ref': 'release..2'}, REPO, call)

    # then
    assert call.seen == [('get_file_contents', {'path': 'notes..md', 'owner': 'shop', 'repo': 'runbooks'}),
                         ('get_file_contents', {'path': 'a/b..c/d.md', 'ref': 'release..2', 'owner': 'shop', 'repo': 'runbooks'})]


def test_the_scope_id_is_spliced_as_data():
    # given
    call = FakeCall()

    # when
    run(DRIVE, 'search_files', {'query': "title contains 'a'"}, Scope("f'1\\", 'Odd', ''), call)
    with pytest.raises(ScopeRefused) as code:
        run(GITHUB, 'search_code', {'query': 'timeout'}, Scope('shop/runbooks or', 'x', ''), call)
    with pytest.raises(ScopeRefused) as contents:
        run(GITHUB, 'get_file_contents', {'path': 'a.md'}, Scope('shop', 'shop', ''), call)
    owner_wide = GITHUB.after('get_file_contents', types.CallToolResult(content=[resource('repo://shop/other/a.md', 'other text')]), Scope('shop', 'shop', ''))

    # then
    assert call.seen == [('search_files', {'query': "parentId = 'f\\'1\\\\' and (title contains 'a')", 'pageSize': 50})]
    assert code.value.text == contents.value.text == msg('mcptools.scope.outside', noun='repository')
    assert owner_wide == (none_kept(GITHUB, Scope('shop', 'shop', '')), 0)


def test_a_search_over_256_characters_with_its_scope_is_refused():
    # given
    call = FakeCall()
    fits = 'a' * (256 - len('() repo:shop/runbooks'))

    # when
    run(GITHUB, 'search_code', {'query': fits}, REPO, call)
    text = refused(GITHUB, 'search_code', {'query': fits + 'b'}, REPO, call)

    # then
    assert len(call.seen[0][1]['query']) == 256
    assert text == msg('mcptools.scope.query_long', limit=256)
    assert len(call.seen) == 1


def test_an_out_of_folder_result_is_dropped(caplog):
    # given
    drive = FakeCall({'search_files': as_text({'files': [drive_file('a', 'f2'), drive_file('b', 'f1'), drive_file('c', 'f2')]})})
    github = FakeCall({'search_code': as_text({'total_count': 4, 'items': [code_item('shop/other'), code_item('Shop/Runbooks', 'ops/a.md'),
                                                                            code_item('shop/runbooks-x'), code_item('shop/runbooks', 'ops/b.md')]})})

    # when
    with caplog.at_level(logging.INFO, logger='skopax.mcptools'):
        drive_text, drive_count = run(DRIVE, 'search_files', {'query': "title contains 'runbook'"}, FOLDER, drive)
        github_text, github_count = run(GITHUB, 'search_code', {'query': 'timeout'}, REPO, github)

    # then
    assert drive_count == 1
    assert json.loads(drive_text) == [{'id': 'b', 'title': 'b runbook', 'mimeType': 'application/vnd.google-apps.document',
                                       'modifiedTime': '2026-11-01T10:00:00Z', 'viewUrl': 'https://docs.google.com/document/d/b', 'contentSnippet': 'restart b'}]
    assert github_count == 2
    assert json.loads(github_text) == [
        {'path': 'ops/a.md', 'html_url': 'https://github.com/Shop/Runbooks/blob/main/ops/a.md', 'fragments': ['timeout after 30 s']},
        {'path': 'ops/b.md', 'html_url': 'https://github.com/shop/runbooks/blob/main/ops/b.md', 'fragments': ['timeout after 30 s']},
    ]
    assert 'shop/other' not in github_text and 'runbooks-x' not in github_text and 'alice@example.com' not in drive_text
    assert logged(caplog) == ['Knowledge search left out results outside its folder: 2', 'Knowledge search left out results outside its repository: 2']


def test_the_model_is_never_told_that_results_were_dropped(caplog):
    # given
    only_outside = as_text({'files': [drive_file('a', 'f2'), drive_file('c', 'f2')]})
    all_inside = as_text({'files': [drive_file('b', 'f1')]})

    # when
    with caplog.at_level(logging.INFO, logger='skopax.mcptools'):
        none = DRIVE.after('search_files', only_outside, FOLDER)
        quiet = logged(caplog)[:]
        caplog.clear()
        kept_text, _ = DRIVE.after('search_files', all_inside, FOLDER)

    # then
    assert none == (none_kept(DRIVE, FOLDER), 0)
    assert quiet == ['Knowledge search left out results outside its folder: 2']
    assert '2' not in none[0] and 'left out' not in kept_text
    assert logged(caplog) == []


def test_metadata_and_contents_outside_the_scope_are_dropped():
    # given
    call = FakeCall({'get_file_metadata': as_text(drive_file(OUTSIDE, 'f2')),
                     'get_file_contents': types.CallToolResult(content=[
                         resource('repo://shop/other/refs/heads/main/contents/a.md', 'other text'),
                         resource('repo://shop/runbooks-x/refs/heads/main/contents/a.md', 'lookalike text'),
                         resource('repo://Shop/Runbooks/refs/heads/main/contents/a.md', 'kept text'),
                         resource('repo://shop/runbooks/../other/b.md', 'climbed text'),
                     ])})

    # when
    metadata = run(DRIVE, 'get_file_metadata', {'fileId': OUTSIDE}, FOLDER, call)
    contents_text, contents_count = run(GITHUB, 'get_file_contents', {'path': 'a.md'}, REPO, call)

    # then
    assert metadata == (none_kept(DRIVE, FOLDER), 0)
    assert contents_count == 1
    assert contents_text == 'repo://Shop/Runbooks/refs/heads/main/contents/a.md\nkept text'


def test_metadata_inside_the_folder_is_kept_and_trimmed():
    # given
    call = FakeCall({'get_file_metadata': as_text(drive_file(INSIDE, 'f1'))})

    # when
    text, count = run(DRIVE, 'get_file_metadata', {'fileId': INSIDE}, FOLDER, call)

    # then
    assert count == 1
    assert json.loads(text) == {'id': INSIDE, 'title': f'{INSIDE} runbook', 'mimeType': 'application/vnd.google-apps.document',
                                'modifiedTime': '2026-11-01T10:00:00Z', 'viewUrl': f'https://docs.google.com/document/d/{INSIDE}', 'contentSnippet': f'restart {INSIDE}'}


@pytest.mark.parametrize('adapter, tool, answer', [
    (DRIVE, 'search_files', as_text({'files': [drive_file('a', None), 'b', drive_file('c', None, parentId=None)]})),
    (GITHUB, 'search_code', as_text({'items': [code_item(None), code_item(None, repository=['shop/runbooks']), code_item(None, repository={'name': 'runbooks'})]})),
    (GITHUB, 'get_file_contents', types.CallToolResult(content=[types.TextContent(type='text', text='successfully downloaded text file (SHA: abc123)'),
                                                                types.TextContent(type='text', text='secret line from shop/other')])),
    (DRIVE, 'get_file_metadata', as_text({'id': 'a', 'title': 'no parent'})),
    (DRIVE, 'read_file_content', types.CallToolResult(content=[types.TextContent(type='text', text='plain text, not the read shape')])),
    (DRIVE, 'search_files', types.CallToolResult(content=[types.TextContent(type='text', text='not json')])),
    (DRIVE, 'search_files', types.CallToolResult(content=[types.TextContent(type='text', text='[' * 100000)])),
], ids=['drive-no-parent', 'github-no-repository', 'contents-text-only', 'metadata-no-parent', 'read-not-json', 'drive-not-json', 'drive-too-deep'])
def test_a_result_with_no_location_is_dropped(adapter, tool, answer):
    # given
    scope = FOLDER if adapter is DRIVE else REPO

    # when
    found = adapter.after(tool, answer, scope)

    # then
    assert found == (none_kept(adapter, scope), 0)


def test_a_result_with_no_location_counts_as_dropped_beside_a_kept_one(caplog):
    # given
    answer = as_text({'files': [drive_file('a', 'f1'), drive_file('b', None)]})

    # when
    with caplog.at_level(logging.INFO, logger='skopax.mcptools'):
        text, count = DRIVE.after('search_files', answer, FOLDER)

    # then
    assert count == 1
    assert [f['id'] for f in json.loads(text)] == ['a']
    assert logged(caplog) == ['Knowledge search left out results outside its folder: 1']


def test_structured_content_is_read_before_the_text_parts():
    # given
    answer = types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps({'files': [drive_file('x', 'f2')]}))],
                                  structuredContent={'files': [drive_file('a', 'f1')]})

    # when
    text, count = DRIVE.after('search_files', answer, FOLDER)

    # then
    assert count == 1
    assert [f['id'] for f in json.loads(text)] == ['a']


def test_fragments_are_cut_at_500_characters_per_item():
    # given
    answer = as_text({'items': [code_item('shop/runbooks', fragments=['a' * 300, 'b' * 300, 'c' * 10]), code_item('shop/runbooks', fragments='d' * 700)]})

    # when
    text, _ = GITHUB.after('search_code', answer, REPO)

    # then
    items = json.loads(text)
    assert items[0]['fragments'] == ['a' * 300, 'b' * 200]
    assert items[1]['fragments'] == ['d' * 500]


def test_a_file_is_read_only_after_its_metadata_places_it():
    # given
    outside = FakeCall({'get_file_metadata': as_text(drive_file(OUTSIDE, 'f2'))})
    inside = FakeCall({'get_file_metadata': as_text(drive_file(INSIDE, 'f1')),
                       'read_file_content': as_text({'fileContent': 'Restart checkout-service first.', 'viewUrl': f'https://docs.google.com/document/d/{INSIDE}'})})

    # when
    text = refused(DRIVE, 'read_file_content', {'fileId': OUTSIDE}, FOLDER, outside)
    found = run(DRIVE, 'read_file_content', {'fileId': INSIDE, 'includeComments': True, 'driveId': 'other'}, FOLDER, inside)

    # then
    assert text == msg('mcptools.scope.outside', noun='folder')
    assert outside.seen == [('get_file_metadata', {'fileId': OUTSIDE})]
    assert inside.seen == [('get_file_metadata', {'fileId': INSIDE}), ('read_file_content', {'fileId': INSIDE, 'includeComments': False})]
    assert found == (f'https://docs.google.com/document/d/{INSIDE}\nRestart checkout-service first.', 1)


@pytest.mark.parametrize('metadata', [
    as_text({'id': INSIDE}),
    as_text(drive_file(INSIDE, 'F1')),
    types.CallToolResult(content=[types.TextContent(type='text', text='not json')]),
    as_text(drive_file(INSIDE, 'f1', mimeType='application/vnd.google-apps.folder')),
    as_text(drive_file(INSIDE, 'f1', mimeType='application/vnd.google-apps.shortcut', shortcutDetails={'targetId': OUTSIDE})),
], ids=['no-parent', 'parent-case', 'metadata-not-json', 'a-folder', 'a-shortcut'])
def test_a_file_whose_place_is_not_confirmed_is_never_read(metadata):
    # given
    call = FakeCall({'get_file_metadata': metadata, 'read_file_content': as_text({'fileContent': 'x'})})

    # when
    text = refused(DRIVE, 'read_file_content', {'fileId': INSIDE}, FOLDER, call)

    # then
    assert text == msg('mcptools.scope.outside', noun='folder')
    assert call.tools() == ['get_file_metadata']


@pytest.mark.parametrize('tool', ['read_file_content', 'get_file_metadata'])
@pytest.mark.parametrize('args', [
    {},
    {'fileId': 7},
    {'fileId': 'short1234'},
    {'fileId': 'x' * 201},
    {'fileId': 'docInside01/../docOutside1'},
    {'fileId': 'docInside01?alt=media'},
    {'fileId': '%2e%2e%2fdocOutside1'},
    {'fileId': 'docInside01\\x'},
], ids=['no-id', 'id-not-text', 'id-too-short', 'id-too-long', 'id-climbs', 'id-query', 'id-percent', 'id-backslash'])
def test_a_malformed_file_id_is_refused_before_any_call(tool, args):
    # given
    call = FakeCall({'get_file_metadata': as_text(drive_file(INSIDE, 'f1')), 'read_file_content': as_text({'fileContent': 'x'})})

    # when
    text = refused(DRIVE, tool, args, FOLDER, call)

    # then
    assert text == msg('mcptools.scope.outside', noun='folder')
    assert call.seen == []


def test_file_ids_of_ten_to_two_hundred_characters_pass():
    # given
    call = FakeCall({'get_file_metadata': as_text(drive_file('a', 'f1'))})

    # when
    run(DRIVE, 'get_file_metadata', {'fileId': 'a_b-c12345'}, FOLDER, call)
    run(DRIVE, 'read_file_content', {'fileId': 'Z' * 200}, FOLDER, call)

    # then
    assert call.seen == [('get_file_metadata', {'fileId': 'a_b-c12345'}), ('get_file_metadata', {'fileId': 'Z' * 200}),
                         ('read_file_content', {'fileId': 'Z' * 200, 'includeComments': False})]


def test_a_failed_metadata_call_is_not_rewritten():
    # given
    class Down(Exception):
        pass

    def fail(args):
        raise Down()

    call = FakeCall({'get_file_metadata': fail})

    # when
    with pytest.raises(Down):
        DRIVE.before('read_file_content', {'fileId': INSIDE}, FOLDER, call)

    # then
    assert call.tools() == ['get_file_metadata']


def test_a_read_without_a_view_link_keeps_the_content():
    # given
    answer = as_text({'fileContent': 'Restart checkout-service first.'})

    # when
    found = DRIVE.after('read_file_content', answer, FOLDER)

    # then
    assert found == ('Restart checkout-service first.', 1)


def test_tools_outside_the_adapter_fail_closed():
    # given
    call = FakeCall()

    # when
    drive = refused(DRIVE, 'list_recent_files', {}, FOLDER, call)
    github = refused(GITHUB, 'search_repositories', {'query': 'x'}, REPO, call)
    after = DRIVE.after('create_file', as_text({'files': [drive_file('a', 'f1')]}), FOLDER)

    # then
    assert drive == msg('mcptools.scope.outside', noun='folder')
    assert github == msg('mcptools.scope.outside', noun='repository')
    assert after == (none_kept(DRIVE, FOLDER), 0)
    assert call.seen == []


def test_a_refusal_is_authored_text():
    # given
    refusal = ScopeRefused(msg('mcptools.scope.widen'))

    # then
    assert refusal.text == text_of(refusal) == msg('mcptools.scope.widen')


def test_drive_folders_are_listed_from_the_server():
    # given
    folders = [{'id': 'f1', 'title': 'Runbooks', 'viewUrl': 'https://drive.google.com/drive/folders/f1'},
               {'id': 'f2', 'title': 'R' * 100, 'viewUrl': 'https://drive.google.com/drive/folders/f2'},
               {'id': 'f3', 'title': 'No link'}, {'title': 'no id'}, {'id': '', 'title': 'empty id'}, 'not a folder']
    call = FakeCall({'search_files': as_text({'files': folders})})

    # when
    found = DRIVE.folders(call, "it's \\ " + 'q' * 120)
    plain = DRIVE.folders(call, '')

    # then
    assert found == plain == [Scope('f1', 'Runbooks', 'https://drive.google.com/drive/folders/f1'),
                              Scope('f2', 'R' * 100, 'https://drive.google.com/drive/folders/f2'), Scope('f3', 'No link', '')]
    folder_type = "mimeType = 'application/vnd.google-apps.folder'"
    assert call.seen == [
        ('search_files', {'query': folder_type + " and title contains 'it\\'s \\\\ " + 'q' * 93 + "'", 'pageSize': 50, 'excludeContentSnippets': True}),
        ('search_files', {'query': folder_type, 'pageSize': 50, 'excludeContentSnippets': True}),
    ]


def test_a_folder_list_stops_at_its_limit():
    # given
    call = FakeCall({'search_files': as_text({'files': [{'id': f'f{i}', 'title': f'F{i}'} for i in range(FOLDERS_MAX + 10)]})})

    # when
    found = DRIVE.folders(call, '')

    # then
    assert len(found) == FOLDERS_MAX


def test_github_repositories_are_listed_from_the_server():
    # given
    items = [{'full_name': 'shop/runbooks', 'html_url': 'https://github.com/shop/runbooks'},
             {'full_name': 'shop/no-link'}, {'html_url': 'https://github.com/shop/no-name'},
             {'full_name': 'shop/runbooks extra', 'html_url': 'https://github.com/x'}, 'not a repository']
    call = FakeCall({'search_repositories': as_text({'total_count': 5, 'items': items})})

    # when
    short = GITHUB.folders(call, ' r ')
    found = GITHUB.folders(call, 'runbooks')

    # then
    assert short == []
    assert found == [Scope('shop/runbooks', 'shop/runbooks', 'https://github.com/shop/runbooks')]
    assert call.seen == [('search_repositories', {'query': 'runbooks in:name', 'perPage': 50, 'minimal_output': False})]


def test_each_adapter_declares_its_tools_hosts_and_noun():
    # then
    assert {key: (a.id, a.noun, a.tools, a.link_hosts) for key, a in ADAPTERS.items()} == {
        'drive': ('drive', 'folder', frozenset({'search_files', 'get_file_metadata', 'read_file_content'}), ('docs.google.com', 'drive.google.com')),
        'github': ('github', 'repository', frozenset({'search_code', 'get_file_contents'}), ('github.com',)),
    }


def vendor_item(repository, path: str = 'ops/a.md', fragments: tuple[str, ...] = ('timeout after 30 s',)) -> dict:
    return {'name': path.rsplit('/', 1)[-1], 'path': path, 'sha': 'abc123', 'repository': repository,
            'text_matches': [{'object_type': 'FileContent', 'property': 'content', 'fragment': f, 'matches': [{'text': 'timeout', 'indices': [0, 7]}]} for f in fragments]}


def test_a_search_code_result_in_the_vendor_shape_is_kept_inside_its_repository():
    # given
    answer = as_text({'total_count': 4, 'items': [vendor_item('Shop/Runbooks', 'ops/a.md', ('a' * 300, 'b' * 300)), vendor_item('shop/other', 'ops/b.md'),
                                                   vendor_item('shop/runbooks-x'), vendor_item({'full_name': 'shop/runbooks'}, 'ops/c.md')]})

    # when
    text, count = GITHUB.after('search_code', answer, REPO)

    # then
    assert count == 2
    assert json.loads(text) == [{'path': 'ops/a.md', 'fragments': ['a' * 300, 'b' * 200]}, {'path': 'ops/c.md', 'fragments': ['timeout after 30 s']}]
