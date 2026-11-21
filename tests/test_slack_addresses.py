import dataclasses
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_slack import FakeSlackApi, fake_slack
from tests.test_slack import FakeSlack
from skopax.app import create_app
from skopax.contract import SourceData
from skopax.investigator import prompt
from skopax.operators import Operators
from skopax.settingsources import SourceSet
from skopax.slack import PROPOSAL_CONFIRM, SECTION_LIMIT, WORD_JOINER, Slack, answer_blocks, anomaly_message, fix_message, link_addresses, public_hosts, report_message
from skopax.slackevents import Clicks, Replier

ELLIPSIS = '\u2026'
KEY = 'log|checkout-service|ERROR|x'
SITE = ('www.example.com', 'example.com')


def test_an_elided_address_in_inline_code_gets_no_link_and_reads_the_same():
    # given
    recap = f'The page `example.com/shop/garden/brand-a/plan?size={ELLIPSIS}&color={ELLIPSIS}` answered 500.'

    # when
    said = Replier.text_for({'slack': {'user': 'U1'}}, 'done', recap)

    # then
    assert WORD_JOINER in said
    assert said.replace(WORD_JOINER, '') == f'<@U1> The page `example.com/shop/garden/brand-a/plan?size={ELLIPSIS}&amp;color={ELLIPSIS}` answered 500.'
    assert '<http' not in said
    assert 'example.' + WORD_JOINER + 'com/shop' in said


@pytest.mark.parametrize('text', [
    'see example.com/shop/x?size=',
    'see example.com/shop/.../x',
    f'see www.example.com/shop/{ELLIPSIS}',
    f'```\nGET example.com/shop?a={ELLIPSIS}\n```',
])
def test_every_elided_bare_address_gets_a_word_joiner_in_each_host_dot(text):
    # when
    out = link_addresses(text, SITE)

    # then
    assert out.replace(WORD_JOINER, '') == text
    host = 'www.example.com' if 'www.' in text else 'example.com'
    assert host.replace('.', '.' + WORD_JOINER) in out
    assert '<http' not in out


def test_a_full_bare_address_becomes_an_explicit_https_link():
    # when
    out = link_addresses('Look at example.com/shop/x?a=1 now', SITE)

    # then
    assert out == 'Look at <https://example.com/shop/x?a=1|example.com/shop/x?a=1> now'


def test_an_ampersand_in_an_answer_stays_escaped_inside_the_link():
    # when
    said = Replier.text_for({'slack': {'where': 'dm'}}, 'done', 'Open shop.example.com/cart?a=1&b=2.', SITE)

    # then
    assert said == 'Open <https://shop.example.com/cart?a=1&amp;b=2|shop.example.com/cart?a=1&amp;b=2>.'


@pytest.mark.parametrize('text', [
    'http://checkout-service.shop.svc:8080/health is down',
    'http://10.0.0.5/metrics answered 503',
    'https://example.com/shop/x is fine',
])
def test_an_explicit_link_to_another_host_stays_as_written(text):
    # then
    assert link_addresses(text, SITE) == text


@pytest.mark.parametrize('text, expected', [
    ('http://www.example.com/shop answered 200', 'https://www.example.com/shop answered 200'),
    ('http://example.com/a, and http://WWW.example.com.', 'https://example.com/a, and https://WWW.example.com.'),
])
def test_an_http_link_to_a_known_website_host_becomes_https(text, expected):
    # then
    assert link_addresses(text, SITE) == expected


def test_mentions_and_existing_links_are_left_alone():
    # given
    text = '<@U1> <#C123|ops> <!here> <https://example.com/a|example.com/a> &lt;https://example.com/b|b&gt; &lt;@U2&gt;'

    # then
    assert link_addresses(text, SITE) == text


def test_a_code_block_keeps_its_addresses_apart_from_the_elided_host():
    # given
    text = f'```\nGET example.com/a/b\nGET http://www.example.com/c\nGET example.com/c?x={ELLIPSIS}\n```'

    # when
    out = link_addresses(text, SITE)

    # then
    assert out == text.replace(f'GET example.com/c?x=', f'GET example.{WORD_JOINER}com/c?x=')


def test_inline_code_keeps_a_full_address_of_another_host_as_written():
    # then
    assert link_addresses('the probe hit `api.example.org/health?a=1` and `https://www.example.com/a`', SITE) == 'the probe hit `api.example.org/health?a=1` and `https://www.example.com/a`'


@pytest.mark.parametrize('text, expected', [
    ('the probe hit `www.example.com/health?a=1`', f'the probe hit `www.{WORD_JOINER}example.{WORD_JOINER}com/health?a=1`'),
    ('the probe hit `http://example.com/health`', f'the probe hit `http:{WORD_JOINER}//example.{WORD_JOINER}com/health`'),
])
def test_inline_code_with_a_known_host_address_reads_the_same_and_gets_no_link(text, expected):
    # when
    out = link_addresses(text, SITE)

    # then
    assert out == expected and out.replace(WORD_JOINER, '') == text


@pytest.mark.parametrize('text', [
    'config.py line 4',
    'OrderService.kt:69 throws',
    'the key app.checkout-timeout is missing',
    'com.example.checkout.OrderService failed',
    'mail alice@example.com/x',
    'the host api.example.org answers',
    'skopax/investigator/process.md says so',
])
def test_text_that_is_not_an_address_to_link_stays_as_written(text):
    # then
    assert link_addresses(text, SITE) == text


def test_a_known_bare_host_without_a_path_is_linked_too():
    # then
    assert link_addresses('www.example.com is up', SITE) == '<https://www.example.com|www.example.com> is up'


@pytest.mark.parametrize('text, expected', [
    ('(example.com/x)', '(<https://example.com/x|example.com/x>)'),
    ('*example.com/x*', '*<https://example.com/x|example.com/x>*'),
    ('see example.com/x.', 'see <https://example.com/x|example.com/x>.'),
    ('example.com/wiki/A_(b)', '<https://example.com/wiki/A_(b)|example.com/wiki/A_(b)>'),
])
def test_punctuation_around_an_address_stays_outside_the_link(text, expected):
    # then
    assert link_addresses(text, SITE) == expected


@pytest.mark.parametrize('text', [
    'Look at example.com/shop/x?a=1 now',
    f'`example.com/shop?a={ELLIPSIS}` and www.example.com/a?b=',
    'http://www.example.com/shop and www.example.com',
])
def test_linking_twice_changes_nothing_more(text):
    # when
    once = link_addresses(text, SITE)

    # then
    assert link_addresses(once, SITE) == once


def test_no_room_keeps_a_full_address_as_written_but_still_breaks_an_elided_one():
    # given
    text = f'example.com/a?b=1 and example.com/c?d={ELLIPSIS}'

    # when
    out = link_addresses(text, SITE, limit=len(text))

    # then
    assert out == f'example.com/a?b=1 and example.{WORD_JOINER}com/c?d={ELLIPSIS}'


def test_a_long_answer_stays_within_the_section_limit():
    # given
    recap = ' '.join(['example.com/shop/x?a=1'] * 200)
    link = '<https://example.com/shop/x?a=1|example.com/shop/x?a=1>'

    # when
    said = Replier.text_for({'slack': {'user': 'U1'}}, 'done', recap, SITE)

    # then
    words = said.removeprefix('<@U1> ').removesuffix(f' {ELLIPSIS}').split(' ')
    assert SECTION_LIMIT - len(link) < len(said) <= SECTION_LIMIT and said.endswith(f'x?a=1 {ELLIPSIS}')
    assert set(words) == {'example.com/shop/x?a=1'} and WORD_JOINER not in said


def test_a_long_answer_still_breaks_its_last_elided_address_when_no_room_is_left():
    # given
    recap = ' '.join(['example.com/shop/x?a=1'] * 118) + f' example.com/end?a={ELLIPSIS}'

    # when
    said = Replier.text_for({'slack': {'user': 'U1'}}, 'done', recap, SITE)

    # then
    assert len(said) <= SECTION_LIMIT
    assert said.endswith(f'example.{WORD_JOINER}com/end?a={ELLIPSIS}')
    assert '<https://example.com/shop/x?a=1|' in said


def test_a_cut_never_lands_inside_an_address():
    # given
    recap = 'word ' * 575 + 'shop.example.com/checkout/step/two?a=1 tail'

    # when
    said = Replier.text_for({'slack': {'where': 'dm'}}, 'done', recap, SITE)

    # then
    assert len(said) <= SECTION_LIMIT
    assert said.endswith(f'word{ELLIPSIS}') and 'shop.' not in said


def test_a_cut_right_after_an_address_keeps_the_address_whole_and_linked():
    # given
    address = 'www.example.com/a?b=1'
    head = 'word ' * 575 + 'ab '
    recap = head[:SECTION_LIMIT - 1 - len(address)] + address + ' and more words after the address'

    # when
    said = Replier.text_for({'slack': {'where': 'dm'}}, 'done', recap, SITE)

    # then
    assert len(said) <= SECTION_LIMIT and WORD_JOINER not in said
    assert not said.endswith(f'b=1{ELLIPSIS}') and not said.endswith(f'b=1|{address}>{ELLIPSIS}')


def test_a_known_host_answer_link_uses_the_hosts_given():
    # when
    said = Replier.text_for({'slack': {'where': 'dm'}}, 'done', 'It was http://www.example.com/shop', SITE)

    # then
    assert said == 'It was https://www.example.com/shop'


def test_the_card_post_links_the_draft_addresses():
    # given
    card = anomaly(KEY, num=7)
    draft = f'Checkout fails on shop.example.com/checkout?step=2 and `shop.example.com/cart?id={ELLIPSIS}` since 14:05 UTC.'

    # when
    message = anomaly_message(card, 'https://board.example.com/c/7', 'auto', draft, hosts=SITE)

    # then
    section = next(b['text']['text'] for b in message['blocks'] if b['type'] == 'section' and 'Checkout fails' in b['text']['text'])
    assert '<https://shop.example.com/checkout?step=2|shop.example.com/checkout?step=2>' in section
    assert f'`shop.{WORD_JOINER}example.{WORD_JOINER}com/cart?id={ELLIPSIS}`' in section
    assert '<https://shop.example.com/checkout?step=2|' in message['text']


def test_the_report_post_and_the_fix_note_link_their_addresses():
    # given
    report = {'id': 'r1', 'level': 'warn', 'summary': 'x', 'windowFrom': '2026-11-20T10:00:00+00:00'}

    # when
    message = report_message(report, 'https://board.example.com/#r=r1', 'auto', 'Errors on http://www.example.com/pay since the deploy.', hosts=SITE)
    fixed = fix_message([{'repo': 'shop', 'path': 'a.py', 'lines': '1-2', 'note': 'Seen on example.com/pay?x=1', 'current': 'a', 'suggestion': 'b'}], 'https://board.example.com/c/7', SITE)

    # then
    assert message['text'] == 'Errors on https://www.example.com/pay since the deploy.'
    assert any('<https://example.com/pay?x=1|example.com/pay?x=1>' in e['text'] for b in fixed['blocks'] if b['type'] == 'context' for e in b['elements'])


def test_public_hosts_are_an_https_base_url_host_and_every_website_host():
    # given
    sites = [{'url': 'https://www.example.com/', 'hosts': 'example.net, *.shop.example.org'}, {'url': 'https://example.org'}, {'url': ''}]

    # when
    hosts = public_hosts(sites, 'https://board.example.com')
    plain = public_hosts(sites, 'http://localhost:8000')

    # then
    assert hosts == ('board.example.com', 'www.example.com', 'example.net', 'shop.example.org', 'example.org')
    assert plain == hosts[1:]


def test_the_writing_rule_asks_for_full_https_links_and_path_only_shortened_addresses():
    # given
    process = (Path(prompt.__file__).with_name('process.md')).read_text()

    # when
    rule = next(line for line in process.splitlines() if 'path only' in line)

    # then
    assert 'https://' in rule and '/shop/garden?size=...' in rule and 'even when logs or ingress show http' in rule
    assert prompt.ADDRESS_RULE in prompt.SLACK_CLOSE
    assert 'path only' in prompt.ADDRESS_RULE and 'https://' in prompt.ADDRESS_RULE
    assert rule in prompt.slack_sections(list(prompt.SLACK_SECTIONS))
    assert process.isascii()


def test_an_investigator_thread_update_links_its_addresses_with_the_website_hosts(store):
    # given
    store.save(state(anomaly(KEY, sev='crit')), None)
    feed = SourceData('config file', {}, ({'plugin': 'website', 'name': 'shop', 'url': 'https://www.example.com', 'hosts': 'example.net'},))
    transport = FakeSlack()
    text = f'Back on http://www.example.com/cart and http://example.net/a, not http://10.0.0.5/x; was `example.com/shop?size={ELLIPSIS}`, see www.example.com/deals?b=1 or example.org/status?b=1'

    # when
    with TestClient(create_app(settings(), store, slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi()), sources=SourceSet([('config file', lambda: feed)], {'config file': feed})),
                    base_url='https://skopax.dev.example.com') as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        client.app.state.investigator.effects.thread_update(KEY, text, 'investigator')

    # then
    _, payload = transport.calls[1]
    assert payload['text'] == (f'Back on https://www.example.com/cart and https://example.net/a, not http://10.0.0.5/x; was `example.{WORD_JOINER}com/shop?size={ELLIPSIS}`, '
                               'see <https://www.example.com/deals?b=1|www.example.com/deals?b=1> or example.org/status?b=1')


def test_a_live_slack_answer_links_its_addresses_with_the_hosts_given(store):
    # given
    slack, post, _ = fake_slack()
    replier = Replier(store, slack, dataclasses.replace(settings(), slack_listen='live'), hosts=lambda: SITE)
    act = store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'slack:C1:x', 'text': 'q', 'cards': [],
                            'slack': {'where': 'channel', 'channel': 'C1', 'threadTs': '1792224000.000100', 'ts': '1792224000.000200', 'user': 'U1'}})

    # when
    replier.reply(act, 'done', f'It is http://www.example.com/a again; the page `example.com/b?c={ELLIPSIS}` failed.')

    # then
    [payload] = post.calls
    expected = f'<@U1> It is https://www.example.com/a again; the page `example.{WORD_JOINER}com/b?c={ELLIPSIS}` failed.'
    assert payload['text'] == expected and payload['blocks'][0]['text']['text'] == expected


def test_an_escaped_comparison_does_not_hide_an_elided_address():
    # given
    text = f'p95 &lt;200 ms on example.com/x?a={ELLIPSIS} and &gt;1 s'

    # then
    assert link_addresses(text, ()) == text.replace('example.com', f'example.{WORD_JOINER}com')


@pytest.mark.parametrize('text', [
    'http://www.example.com:8080/x is up',
    'http://shop.www.example.com/x is up',
    'http://user@www.example.com/x is up',
])
def test_the_https_rewrite_takes_only_the_listed_host_on_the_default_port(text):
    # then
    assert link_addresses(text, SITE) == text


@pytest.mark.parametrize('text', [
    'deployment.apps/checkout-service restarted',
    'checkout-service.shop.internal.example/health answered 503',
    'see example.org/status?b=1',
])
def test_a_full_address_of_an_unknown_host_stays_as_written(text):
    # then
    assert link_addresses(text, SITE) == text


@pytest.mark.parametrize('text, expected', [
    (f'see https://example.org/a?b={ELLIPSIS} now', f'see https:{WORD_JOINER}//example.{WORD_JOINER}org/a?b={ELLIPSIS} now'),
    ('see https://www.example.com/a?b= now', f'see https:{WORD_JOINER}//www.{WORD_JOINER}example.{WORD_JOINER}com/a?b= now'),
    (f'&lt;https://example.org/a/{ELLIPSIS}|the page&gt;', f'&lt;https:{WORD_JOINER}//example.{WORD_JOINER}org/a/{ELLIPSIS}|the page&gt;'),
])
def test_an_elided_explicit_link_is_kept_as_inert_text(text, expected):
    # when
    out = link_addresses(text, SITE)

    # then
    assert out == expected and out.replace(WORD_JOINER, '') == text and link_addresses(out, SITE) == out


def test_text_an_operator_types_is_posted_as_written_and_the_investigator_text_is_linked(store):
    # given
    store.save(state(anomaly(KEY, sev='crit')), None)
    feed = SourceData('config file', {}, ({'plugin': 'website', 'name': 'shop', 'url': 'https://www.example.com'},))
    transport = FakeSlack()
    text = 'Fixed on http://www.example.com/cart and www.example.com/deals?b=1'

    # when
    with TestClient(create_app(settings(), store, slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi()), sources=SourceSet([('config file', lambda: feed)], {'config file': feed})),
                    base_url='https://skopax.dev.example.com') as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        typed = client.post('/api/slack-updates', json={'key': KEY, 'text': text}, headers=AUTH)
        client.app.state.investigator.effects.thread_update(KEY, text, 'investigator')

    # then
    assert typed.status_code == 201, typed.text
    assert [p['text'] for _, p in transport.calls[1:]] == [text, 'Fixed on https://www.example.com/cart and <https://www.example.com/deals?b=1|www.example.com/deals?b=1>']


@pytest.mark.parametrize('text', [f'plan at example.org{ELLIPSIS}/plan', 'plan at example.org.../plan', 'see example.com... then'])
def test_an_ellipsis_right_after_the_host_is_elided(text):
    # when
    out = link_addresses(text, SITE)

    # then
    assert out.replace(WORD_JOINER, '') == text and WORD_JOINER in out and '<http' not in out


@pytest.mark.parametrize('text', ['www.example.com:8080/x is up', '`www.example.com:8443/health`', 'see example.com:8080'])
def test_a_known_host_with_a_port_is_left_as_written(text):
    # then
    assert link_addresses(text, SITE) == text


def test_the_confirmed_proposal_summary_links_its_addresses():
    # given
    slack, post, _ = fake_slack(emails={'U1': 'adam.baker@example.com'})
    done = {'ok': True, 'outcome': 'done', 'summary': f'Restarted; http://www.example.com/a answers and `example.com/b?c={ELLIPSIS}` too.'}
    box = Clicks(FakeStore(), slack, Operators(lambda email: True, slack, settings(), lambda: 0), lambda action: 'done', confirm=lambda run, login: done,
                 background=lambda job: job(), hosts=lambda: SITE)
    blocks = answer_blocks('A restart is proposed.', None, None, confirm={'run': 'r1', 'title': 'Restart'})

    # when
    box.handle({'type': 'block_actions', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'}, 'message': {'ts': '1792224000.000300', 'thread_ts': '1792224000.000100', 'text': 'x', 'blocks': blocks},
                'actions': [{'action_id': PROPOSAL_CONFIRM, 'value': 'r1'}]})

    # then
    assert [p['text'] for p in post.posted] == [f'Restarted; https://www.example.com/a answers and `example.{WORD_JOINER}com/b?c={ELLIPSIS}` too.']


class FakeStore:
    def run_proposal(self, run_id):
        return {'title': 'Restart'}

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.mark.parametrize('text', ['&lt;@' * 50_000, 'ab.' * 50_000, '`' + 'example.com/' * 10_000, '&lt;https://' + 'a' * 100_000, 'x' * 100_000])
def test_a_huge_text_is_linked_in_linear_time(text):
    # given
    started = time.perf_counter()

    # when
    link_addresses(text, SITE, SECTION_LIMIT)

    # then
    assert time.perf_counter() - started < 3.0


def test_an_italic_address_is_linked_inside_the_marks():
    # then
    assert link_addresses('_www.example.com/a_ and _https://example.org/b_', SITE) == '_<https://www.example.com/a|www.example.com/a>_ and _https://example.org/b_'


def test_a_full_address_with_an_escaped_bracket_in_its_query_is_not_elided():
    # given
    text = 'see www.example.com/a?b=&lt;1&gt; now'

    # then
    assert link_addresses(text, SITE) == text


@pytest.mark.parametrize('text, expected', [
    ('see example.com/x?a=&amp;', 'see <https://example.com/x?a=&amp;|example.com/x?a=&amp;>'),
    ('see example.com/x?a=1&amp;b=2;', 'see <https://example.com/x?a=1&amp;b=2|example.com/x?a=1&amp;b=2>;'),
])
def test_a_semicolon_that_closes_an_entity_stays_in_the_address(text, expected):
    # then
    assert link_addresses(text, SITE) == expected
