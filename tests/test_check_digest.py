import logging

import pytest

from skopax import sweepout
from skopax.contract import Check, Digest, Field, PassResult, Sweep
from skopax.slack import Slack
from skopax.store import Store
from tests.conftest import AUTH
from tests.fake_slack import fake_slack
from tests.plugins.base import minimal
from tests.test_page_scan_seams import NOW, running

PLAN: dict = {}


def weekly(ctx) -> Sweep:
    plan = PLAN.get(ctx.instance.name) or PLAN.get('all', {})
    if plan.get('raise'):
        raise RuntimeError('no summary')
    return Sweep(PassResult('weekly', plan.get('ok', True), 'ok' if plan.get('ok', True) else 'error: down', [], {}, ('weekly',)), digest=plan.get('digest'))


WEEKLY = minimal('weekly', 'Weekly', fields=(Field('url', 'Site URL', 'url'),), checks=(Check('weekly', 'Weekly summary', 604800, 0, ('weekly',), weekly),))


@pytest.fixture(autouse=True)
def clean_plan():
    PLAN.clear()
    yield
    PLAN.clear()


def digests(c) -> list[dict]:
    return [a for a in c.get('/api/actions', headers=AUTH).json() if a['type'] == sweepout.CHECK_DIGEST]


def test_a_check_digest_goes_to_the_main_channel_as_one_plain_message_with_no_card(store: Store, tmp_path):
    # given
    slack, post, _ = fake_slack()
    PLAN['all'] = {'digest': Digest('Weekly summary <!channel> & co', ('12 deploys, 2 rolled back', '  ', 'Slowest page: /checkout <@U123>', 'Errors \n down 30%'))}

    # when
    with running(store, tmp_path, plugins=(WEEKLY,), slack=slack) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)
        done = digests(c)

    # then
    [top] = post.posted
    assert top['channel'] == '#ops' and 'thread_ts' not in top and 'blocks' not in top
    assert top['text'].split('\n')[:4] == ['Weekly summary &lt;!channel&gt; &amp; co', '\u2022 12 deploys, 2 rolled back', '\u2022 Slowest page: /checkout &lt;@U123&gt;', '\u2022 Errors down 30%']
    assert store.load()['anomalies'] == []
    [action] = done
    assert (action['status'], action['requestedBy'], action['plugin'], action['label']) == ('done', 'server', 'weekly', 'Weekly summary <!channel> & co')
    assert action['target'] == f'{sweepout.CHECK_DIGEST}:weekly/weekly/main' and action['slack']['ts']


@pytest.mark.parametrize('given', [None, Digest('Weekly summary'), Digest('Weekly summary', ('', '   ', '\n'))])
def test_an_empty_digest_posts_nothing(store: Store, tmp_path, given):
    # given
    slack, post, _ = fake_slack()
    PLAN['all'] = {'digest': given}

    # when
    with running(store, tmp_path, plugins=(WEEKLY,), slack=slack) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)
        done = digests(c)

    # then
    assert post.posted == [] and done == []


def test_a_huge_digest_is_cut_to_the_limits_with_a_more_line_and_one_warning(store: Store, tmp_path, caplog):
    # given
    slack, post, _ = fake_slack()
    words = ' '.join(f'w{i:04d}' for i in range(200))
    PLAN['all'] = {'digest': Digest(words, tuple(f'line {i} ' + words for i in range(sweepout.DIGEST_LINES_MAX + 7)))}

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.sweeps'), running(store, tmp_path, plugins=(WEEKLY,), slack=slack) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)

    # then
    [top] = post.posted
    rows = top['text'].split('\n')
    assert len(top['text']) <= sweepout.POST_TEXT_MAX
    assert len(rows[0]) <= sweepout.TITLE_MAX and all(len(r) <= sweepout.LINE_MAX + 2 for r in rows[1:])
    assert rows[-1].startswith('\u2022 and ') and rows[-1].endswith(' more')
    shown = len(rows) - 2
    assert rows[-1] == f'\u2022 and {sweepout.DIGEST_LINES_MAX - shown} more'
    assert [r.getMessage() for r in caplog.records if 'digest line' in r.getMessage()] == [f'pass weekly: dropped 7 digest lines over the {sweepout.DIGEST_LINES_MAX} per pass']


def test_a_digest_is_not_sent_while_slack_is_not_set_up_or_auto_send_is_off(store: Store, tmp_path):
    # given
    _, post, api = fake_slack()
    unset = Slack('', '#ops', post, api, upload=api.upload, sleep=api.sleeps.append)
    on, posted_on, _ = fake_slack()
    PLAN['all'] = {'digest': Digest('Weekly summary', ('12 deploys',))}

    # when
    with running(store, tmp_path, plugins=(WEEKLY,), slack=unset) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)
        none_set = digests(c)
    with running(store, tmp_path, plugins=(WEEKLY,), slack=on, slack_autosend=False) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)
        none_auto = digests(c)

    # then
    assert post.calls == [] and posted_on.calls == [] and none_set == none_auto == []


def test_a_failed_pass_or_a_failed_post_sends_nothing_and_a_failed_post_is_recorded(store: Store, tmp_path):
    # given
    slack, post, _ = fake_slack()
    broken, broken_post, _ = fake_slack(post_fail=True)

    # when
    with running(store, tmp_path, plugins=(WEEKLY,), slack=slack) as c:
        PLAN['all'] = {'digest': Digest('Weekly summary', ('12 deploys',)), 'ok': False}
        c.app.state.sweeper.run('weekly', 'weekly', NOW)
        PLAN['all'] = {'raise': True}
        c.app.state.sweeper.run('weekly', 'weekly', NOW + 60)
    with running(store, tmp_path, plugins=(WEEKLY,), slack=broken) as c:
        PLAN['all'] = {'digest': Digest('Weekly summary', ('12 deploys',))}
        c.app.state.sweeper.run('weekly', 'weekly', NOW + 120)
        failed = digests(c)

    # then
    assert post.calls == []
    assert len(broken_post.calls) == 1 and [(a['status'], a['result']) for a in failed] == [('failed', 'not sent: SlackError')]


def test_a_second_digest_within_the_gap_is_held_and_one_after_it_goes(store: Store, tmp_path, monkeypatch):
    # given
    slack, post, _ = fake_slack()
    PLAN['all'] = {'digest': Digest('Weekly summary', ('12 deploys',))}

    # when
    with running(store, tmp_path, plugins=(WEEKLY,), slack=slack) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)
        c.app.state.sweeper.run('weekly', 'weekly', NOW + 60)
        held = len(post.posted)
        monkeypatch.setattr(sweepout, 'DIGEST_GAP_S', 0)
        c.app.state.sweeper.run('weekly', 'weekly', NOW + 120)

    # then
    assert held == 1 and len(post.posted) == 2


@pytest.mark.parametrize('given', ['Weekly summary', Digest(42, ('x',)), Digest('t', 'one line'), Digest('t', ('ok', 7))])
def test_a_malformed_digest_fails_the_pass(store: Store, tmp_path, given):
    # given
    slack, post, _ = fake_slack()
    PLAN['all'] = {'digest': given}

    # when
    with running(store, tmp_path, plugins=(WEEKLY,), slack=slack) as c:
        c.app.state.sweeper.run('weekly', 'weekly', NOW)

    # then
    assert post.posted == []
    assert store.load()['sources']['weekly'].startswith('error:') and 'digest' in store.load()['sources']['weekly']
