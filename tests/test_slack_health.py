import time

from tests.conftest import anomaly, state
from tests.test_slack import FakeSlack, client_with
from skopax.engine import iso, parse_iso
from skopax.investigator.select import OWN_FINDINGS
from skopax.slackhealth import PASS, slack_posts_pass

NOW = parse_iso('2026-11-12T06:00:00Z')


def card(num: int, sev: str = 'warn', **extra) -> dict:
    return {'key': f'log|svc{num}|ERROR|x', 'num': num, 'sev': sev, 'status': 'open', **extra}


def failed(at: str = '2026-11-12T05:23:04Z', error: str = 'channel_not_found') -> dict:
    return {'autoSend': {'failedAt': at, 'error': error, 'retries': 1}}


def test_failed_posts_open_one_card_critical_when_a_critical_card_was_not_posted():
    # given
    cards = [card(172, 'crit', **failed()), card(168, **failed('2026-11-12T05:30:00Z')), card(150),
             card(140, **failed('2026-11-12T04:00:00Z')), card(141, **failed(), slackPost={'at': '2026-11-12T05:40:00Z'})]

    # when
    [result] = slack_posts_pass(cards, NOW)

    # then
    [x] = result.found
    assert (x['src'], x['sev'], x['now']) == (PASS, 'crit', 2), 'two posts failed in the last hour; one older and one posted later do not count'
    assert 'Not posted: #172, #168' in x['what'] and 'Slack answered channel_not_found' in x['what'] and 'since 05:23 UTC' in x['what']
    assert PASS in OWN_FINDINGS, 'the card explains itself and is not posted to the Slack that fails'


def test_no_failed_post_means_no_card_and_any_failed_post_is_critical():
    # when
    [clean] = slack_posts_pass([card(1), card(2, slackPost={'at': '2026-11-12T05:00:00Z'})], NOW)
    [warn] = slack_posts_pass([card(3, **failed(error='invalid_auth'))], NOW)

    # then
    assert clean.ok and clean.found == [] and clean.ran == (PASS,)
    assert warn.found[0]['sev'] == 'crit' and 'invalid_auth' in warn.found[0]['short'], 'a broken Slack is critical whatever failed: Teams tech takes critical cards only, and the phone DM is Slack too'


def test_the_job_opens_the_card_and_clears_it_once_posts_go_through(store):
    # given
    now = time.time()
    bad = anomaly('log|checkout-service|ERROR|x', sev='crit', firstSeen=iso(now - 900), lastSeen=iso(now - 600), autoSend={'failedAt': iso(now - 300), 'error': 'channel_not_found', 'retries': 3})
    store.save({**state(bad), 'initialized': True}, None)

    # when
    with client_with(store, FakeSlack()) as c:
        job = next(j for j in c.app.state.scheduler.jobs if j.name == PASS)
        job.run(now)
        opened = [a for a in store.load()['anomalies'] if a.get('src') == PASS]
        store.anomalies.update_one({'_id': 'log|checkout-service|ERROR|x'}, {'$set': {'slackPost': {'at': iso(now), 'ts': '1.1', 'channel': 'C1'}}})
        job.run(now + 300)
        later = [a for a in store.load()['anomalies'] if a.get('src') == PASS]

    # then
    assert (job.period, len(opened), opened[0]['sev'], opened[0]['status']) == (300, 1, 'crit', 'open')
    assert later[0]['status'] == 'cleared', 'a post that went through clears the card'
