import base64
from urllib.parse import quote

import pytest

from tests.conftest import anomaly, settings, state
from tests.fake_http import FakeHttp
from tests.test_enrich import PROJECTS, WEB, projects
from tests.test_investigator_tools import NOW, box, sources
from tests.test_tickets import FakeJira, text_of
from tests.test_tool_inputs import report_box
from skopax import actions as page_actions
from skopax.plugins.gitlab.client import GITLAB_API, GitLab
from skopax.investigator import prompt
from skopax.investigator.tools import CODE_ITEM, FIX_CHARS, FIX_LINES, FIX_PER_CARD, WRITE_TOOLS, Effects, Toolbox
from skopax.remote import HttpError
from skopax.store import Store

KEY = 'log|a|ERROR|x'
PATH = 'src/main/kotlin/com/example/shop/CartService.kt'
FILE = f'{GITLAB_API}/projects/11/repository/files/{quote(PATH, safe="")}'
MASTER = 'a' * 40
DEPLOYED = '1a1a1a1a' + 'b' * 32
AT_DEPLOYED = 'package com.example.shop\n\nfun total(cart: Cart): Double {\n    return cart.total!!\n}\n' + ''.join(f'// line {n}\n' for n in range(6, 81))
AT_MASTER = 'package com.example.shop\n\nfun total(cart: Cart): Double {\n    return cart.total ?: 0.0\n}\n'
SUGGESTION = 'fun total(cart: Cart): Double? {\n    return cart.total\n}'
BLAME = [{'commit': {'id': 'c' * 40, 'author_name': 'Nina', 'committed_date': '2026-10-20T10:00:00Z', 'message': 'OPS-1 total from the cart\n\nbody'}, 'lines': ['a', 'b', 'c']}]


def gitlab_http() -> FakeHttp:
    files = {'master': AT_MASTER, MASTER: AT_MASTER, DEPLOYED: AT_DEPLOYED}

    def file(q, _b):
        text = files.get(q['ref'][0])
        return {'content': base64.b64encode(text.encode()).decode()} if text is not None else HttpError(404, 'file')

    return FakeHttp({
        ('GET', PROJECTS): projects,
        ('GET', f'{GITLAB_API}/projects/11/repository/branches/master'): {'commit': {'id': MASTER}},
        ('GET', f'{GITLAB_API}/projects/11/repository/commits/1a1a1a1a'): {'id': DEPLOYED},
        ('GET', FILE): file,
        ('GET', f'{FILE}/blame'): BLAME,
    })


def fix_box(store: Store, http: FakeHttp, mode: str = 'live'):
    store.save_state(state(anomaly(KEY), anomaly('log|b|ERROR|y')), None)
    return box(store, mode=mode, gitlab=GitLab('glpat', http, group='acme'))


def item(**over) -> dict:
    return {'repo': 'checkout-service', 'path': PATH, 'lines': '3-5', 'what': 'check that callers handle a null total', 'ref': '1a1a1a1a', 'suggestion': SUGGESTION, **over}


def assess(b, *code) -> tuple[str, bool]:
    return b.call('assess', {'key': KEY, 'suspects': ['total is null on 40 carts'], 'recommend': 'guard the null total', 'code': list(code)})


def test_the_code_item_takes_an_optional_ref_and_suggestion_and_the_description_states_the_limits():
    # given
    desc = next(t['description'] for t in WRITE_TOOLS if t['name'] == 'assess')

    # then
    assert CODE_ITEM['properties']['ref'] == {'type': 'string'}
    assert CODE_ITEM['properties']['suggestion'] == {'type': 'string', 'maxLength': FIX_CHARS}
    assert CODE_ITEM['required'] == ['repo', 'path', 'lines', 'what'] and CODE_ITEM['additionalProperties'] is False
    assert (FIX_LINES, FIX_CHARS, FIX_PER_CARD) == (60, 4000, 2)
    assert 'default: the default branch' in desc and 'at most 60 lines' in desc and '4,000 characters' in desc and 'at most 2 suggestions' in desc
    assert 'the server reads the current lines' in desc and 'points at that commit' in desc
    assert 'code links are replaced only when you send some' in desc


def test_a_suggestion_stores_the_current_lines_the_server_read_at_the_ref_and_links_that_commit(store: Store):
    # given
    http = gitlab_http()
    b = fix_box(store, http)

    # when
    text, err = assess(b, item(suggestion=SUGGESTION + '\r\n'))
    code = store.anomaly(KEY)['code']

    # then
    assert not err, text
    assert code == [{'label': 'CartService.kt:3-5', 'repo': 'checkout-service', 'dir': 'checkout-service', 'path': PATH, 'lines': '3-5', 'note': 'check that callers handle a null total',
                     'url': f'{WEB}/-/blob/{DEPLOYED}/{PATH}#L3-5', 'sha': '1a1a1a1a', 'ref': '1a1a1a1a',
                     'by': [{'name': 'Nina', 'lines': 3, 'sha': 'cccccccc', 'date': '2026-10-20', 'title': 'OPS-1 total from the cart', 'url': f'{WEB}/-/commit/{"c" * 40}'}],
                     'current': 'fun total(cart: Cart): Double {\n    return cart.total!!\n}', 'suggestion': SUGGESTION, 'language': 'kotlin'}]
    assert [c[1].split('ref=')[1].split('&')[0] for c in http.hits(FILE)] == [DEPLOYED], 'the current lines come from the commit, never from master'
    assert 'ref=' + DEPLOYED in http.hits(f'{FILE}/blame')[0][1]


def test_a_code_link_without_a_ref_reads_master_and_an_image_ref_reads_the_sha_after_the_colon(store: Store):
    # given
    http = gitlab_http()
    b = fix_box(store, http)

    # when
    plain = assess(b, {'repo': 'checkout-service', 'path': PATH, 'lines': '4', 'what': 'the null total'})
    first = store.anomaly(KEY)['code'][0]
    image = assess(b, item(ref='registry.example.com/shop/checkout-service/master:1a1a1a1a', lines='4', suggestion='    return cart.total ?: 0.0'))
    second = store.anomaly(KEY)['code'][0]

    # then
    assert (plain[1], image[1]) == (False, False)
    assert (first['url'], first['sha'], first['ref']) == (f'{WEB}/-/blob/{MASTER}/{PATH}#L4', 'aaaaaaaa', 'master')
    assert not {'current', 'suggestion', 'language'} & set(first)
    assert (second['url'], second['ref'], second['current'], second['suggestion']) == (f'{WEB}/-/blob/{DEPLOYED}/{PATH}#L4', '1a1a1a1a', '    return cart.total!!', '    return cart.total ?: 0.0')


@pytest.mark.parametrize('code, message', [
    ([item(lines='3-63')], 'a suggestion covers at most 60 lines; 3-63 is 61'),
    ([item(), item(lines='4'), item(lines='5', suggestion='}\n')], 'at most 2 suggestions per card; this assess has 3'),
    ([item(suggestion='fun total(cart: Cart): Double {\n    return cart.total!!\n}\n')], 'the suggestion is the same as the current lines 3-5'),
    ([item(suggestion=' \n\t')], 'the suggestion is empty'),
    ([item(lines='79-82')], f'{PATH} has 80 lines at 1a1a1a1a; lines 79-82 run past its end'),
    ([item(ref='0000ffff')], 'GitLab found no 0000ffff in checkout-service'),
    ([item(ref='master..x')], 'code[0].ref must be one commit sha, branch or tag'),
    ([item(path='src/Missing.kt')], 'src/Missing.kt is not in checkout-service at 1a1a1a1a'),
], ids=['span', 'count', 'same', 'empty', 'past-end', 'no-ref', 'bad-ref', 'no-file'])
def test_a_suggestion_past_the_limits_is_refused_with_a_clear_error_and_nothing_is_written(store: Store, code, message):
    # given
    b = fix_box(store, gitlab_http())

    # when
    text, err = assess(b, *code)

    # then
    assert err and message in text, text
    assert store.anomaly(KEY).get('code') is None and b.writes == []


def test_the_size_limit_of_a_suggestion_is_the_schema_check_and_a_long_link_without_one_still_works(store: Store):
    # given
    b = fix_box(store, gitlab_http())

    # when
    big = assess(b, item(suggestion='x' * (FIX_CHARS + 1)))
    wide = assess(b, {'repo': 'checkout-service', 'path': PATH, 'lines': '1-80', 'what': 'the whole service', 'ref': '1a1a1a1a'})

    # then
    assert big[1] and big[0] == f'code[0].suggestion must be at most {FIX_CHARS} characters, got {FIX_CHARS + 1}'
    assert not wide[1] and store.anomaly(KEY)['code'][0]['lines'] == '1-80'


def test_a_model_written_before_side_is_refused_by_the_schema(store: Store):
    # given
    b = fix_box(store, gitlab_http())

    # when
    text, err = assess(b, item(current='made up'))

    # then
    assert err and text == 'code[0] has no field current; its fields are repo, path, lines, what, ref, suggestion'


def test_a_shadow_run_reads_the_lines_but_writes_nothing(store: Store):
    # given
    http = gitlab_http()
    b = fix_box(store, http, mode='shadow')

    # when
    text, err = assess(b, item())

    # then
    assert not err and 'shadow' in text
    assert store.anomaly(KEY).get('code') is None and len(http.hits(FILE)) == 1


def test_the_stored_suggestion_round_trips_through_the_state_the_page_reads(store: Store):
    # given
    b = fix_box(store, gitlab_http())
    assess(b, item())

    # when
    card = next(a for a in store.load()['anomalies'] if a['key'] == KEY)

    # then
    assert (card['code'][0]['current'], card['code'][0]['suggestion'], card['code'][0]['language'], card['code'][0]['sha']) == \
           ('fun total(cart: Cart): Double {\n    return cart.total!!\n}', SUGGESTION, 'kotlin', '1a1a1a1a')


def test_the_brief_says_a_suggested_fix_is_on_the_card_without_the_code():
    # given
    card = {'key': KEY, 'svc': 'checkout-service', 'label': 'ERROR pattern', 'sev': 'warn', 'status': 'open', 'firstSeen': '2026-10-26T06:00:00Z',
            'code': [{'repo': 'checkout-service', 'path': PATH, 'lines': '3-5', 'sha': '1a1a1a1a', 'current': 'old()', 'suggestion': 'new()', 'url': 'https://gitlab.com/acme/x'},
                     {'repo': 'checkout-service', 'path': 'src/Other.kt', 'lines': '9', 'sha': 'aaaaaaaa', 'url': 'https://gitlab.com/acme/y'}]}

    # when
    text = prompt.card_brief(card, 'https://skopax.example.com')

    # then
    assert f'  suggested fix (untested) on the card: checkout-service · {PATH}:3-5 @ 1a1a1a1a' in text
    assert 'new()' not in text and 'old()' not in text and 'src/Other.kt' not in text


FIX_CARD = dict(suspects=['total is null'], recommend='guard the null total',
                code=[{'label': 'CartService.kt:3-5', 'repo': 'checkout-service', 'path': PATH, 'lines': '3-5', 'sha': '1a1a1a1a', 'note': 'check that callers handle a null total',
                       'url': f'{WEB}/-/blob/{DEPLOYED}/{PATH}#L3-5', 'current': 'fun total(cart: Cart): Double {\n    return cart.total!!\n}', 'suggestion': SUGGESTION, 'language': 'kotlin'},
                      {'label': 'Other.kt:9', 'url': f'{WEB}/-/blob/{MASTER}/src/Other.kt#L9'}])


def fix_nodes(adf: dict) -> list[dict]:
    content = adf['content']
    start = next(i for i, c in enumerate(content) if c['type'] == 'paragraph' and c['content'][0]['text'] == 'Suggested fix (untested)')
    return content[start:start + 7]


def test_create_task_adds_a_suggested_fix_section_with_the_address_link_and_two_code_blocks(store: Store):
    # given
    store.save_state(state(anomaly(KEY, **FIX_CARD)), None)
    jira = FakeJira()

    # when
    page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'requestedBy': 'oscar.green@example.com'}, jira, settings(), 'https://skopax.example.com')
    adf = jira.created[0]['description']
    nodes = fix_nodes(adf)

    # then
    assert nodes == [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Suggested fix (untested)', 'marks': [{'type': 'strong'}]}]},
                     {'type': 'paragraph', 'content': [{'type': 'text', 'text': f'checkout-service · {PATH}:3-5 @ 1a1a1a1a', 'marks': [{'type': 'link', 'attrs': {'href': f'{WEB}/-/blob/{DEPLOYED}/{PATH}#L3-5'}}]}]},
                     {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'check that callers handle a null total'}]},
                     {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Current:'}]},
                     {'type': 'codeBlock', 'attrs': {'language': 'kotlin'}, 'content': [{'type': 'text', 'text': 'fun total(cart: Cart): Double {\n    return cart.total!!\n}'}]},
                     {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Suggested:'}]},
                     {'type': 'codeBlock', 'attrs': {'language': 'kotlin'}, 'content': [{'type': 'text', 'text': SUGGESTION}]}]
    assert text_of(adf)[text_of(adf).index('Suggested fix (untested)') - 1] == 'Recommended: guard the null total', 'the section follows the card facts and comes before the links'
    assert sum(1 for c in adf['content'] if c['type'] == 'codeBlock') == 2, 'the link without a suggestion adds no section'
    assert text_of(adf)[-2:] == ['Link: CartService.kt:3-5', 'Link: Other.kt:9']


def test_a_typed_description_gets_the_same_section_and_a_blank_current_side_or_an_unknown_language_stays_valid_adf(store: Store):
    # given
    code = [dict(FIX_CARD['code'][0], current='', language=None, path='Makefile', label='Makefile:3')]
    store.save_state(state(anomaly(KEY, code=code)), None)
    jira = FakeJira()

    # when
    page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'description': 'Line 1', 'requestedBy': 'oscar.green@example.com'}, jira, settings())
    adf = jira.created[0]['description']
    blocks = [c for c in adf['content'] if c['type'] == 'codeBlock']

    # then
    assert text_of({'content': adf['content'][:1]}) == ['Line 1']
    assert blocks == [{'type': 'codeBlock', 'content': []}, {'type': 'codeBlock', 'content': [{'type': 'text', 'text': SUGGESTION}]}]
    assert all(n.get('text') for c in adf['content'] for n in c['content']), 'ADF refuses empty text nodes'


def test_no_code_block_goes_to_the_main_channel_from_a_draft_a_thread_reply_or_a_report_draft(store: Store):
    # given
    store.save_state(state(anomaly(KEY), anomaly('log|b|ERROR|y', svc='catalog-service', slackPost={'channel': 'C1', 'ts': '1.2', 'at': '2026-10-26T06:00:00Z', 'by': 'auto'})), None)
    updates = []
    b = Toolbox(sources(), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: updates.append(text)), [KEY, 'log|b|ERROR|y'], 'live', NOW, 'https://skopax.example.com')
    report, rid = report_box(store)
    code = 'checkout-service NPE, the fix:\n```kotlin\nval base = cart.total ?: return null\n```'

    # when
    draft = b.call('slack_draft', {'key': KEY, 'text': code})
    reply = b.call('slack_update', {'key': 'log|b|ERROR|y', 'text': code})
    brief = report.call('report_slack_draft', {'text': code})
    inline = b.call('slack_draft', {'key': KEY, 'text': '`CartService.total` throws an NPE on a null total; the card has an untested fix proposal.'})

    # then
    for text, err in (draft, reply, brief):
        assert err and text == 'no code blocks (```) in the main channel: say that a suggested fix is on the card, the code stays there', text
    assert updates == [] and store.deploy_report(rid).get('slackDraft') is None
    assert not inline[1] and store.anomaly(KEY)['slackDraft']['text'].startswith('`CartService.total`'), 'inline backticks around a name are not code'


def test_a_suggestion_saved_by_assess_asks_for_its_thread_post_and_a_failing_hook_never_fails_the_tool(store: Store):
    # given
    asked = []
    b = fix_box(store, gitlab_http())
    b.effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, fixes=lambda key: asked.append(key) or 1)
    broken = fix_box(store, gitlab_http())
    broken.effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, fixes=lambda key: 1 / 0)

    # when
    _, err = assess(b, item())
    _, plain_err = assess(b, item(suggestion=None))
    _, broken_err = assess(broken, item())

    # then
    assert (err, plain_err, broken_err) == (False, False, False)
    assert asked == [KEY], 'only the assess that saved a suggestion asks for the thread post'
