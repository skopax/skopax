import dataclasses
import json
import time

from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.kube import Kube
from skopax.slack import Slack
from skopax.engine import PassResult, iso as iso_at
from skopax.plugins import email_route, mail_server, teams
from skopax.routes import Routes
from skopax.store import Store
from tests.conftest import AUTH, ready_app, settings, state
from tests.fake_slack import FakeSlackApi
from tests.mail_kit import FakeSMTP
from tests.teams_kit import SEO, TECH, Transport, add_route, live_of, open_card, route, routes_of, runtime_of, tag, texts
from tests.test_email_routes import Box, fresh, mail_route, parts, sent, server
from tests.test_route_slack_echo import Fake, SlackT

KEY = 'log|checkout-service|ERROR|boom'
NOW = 1_794_240_000.0
FOUND = 'The checkout pods ran out of memory after the 14:02 deploy. Orders failed for nine minutes and recovered after the rollback.'
SAID = {'text': 'We recommend adding a redirect for the old spring sale page.', 'why': 'Visitors from the old campaign land on an empty page.', 'by': 'pod', 'at': '2026-11-09T16:00:00Z'}
PLUGINS = (teams.PLUGIN, mail_server.PLUGIN, email_route.PLUGIN)

MAIL_FIXTURE = fresh


def card(sev: str = 'high', key: str = KEY, **extra) -> dict:
    return open_card(key, sev, firstSeen=iso_at(NOW), pushedAt=iso_at(NOW), **extra)


def waiting(routes: Routes, post_sev: str = 'crit', live: bool = True, minutes: int = 30) -> Routes:
    routes.settings = dataclasses.replace(routes.settings, slack_post_sev=post_sev, routes_wait_minutes=minutes)
    routes.investigating = lambda: live
    return routes


def keys_route(store: Store, t: Transport, **given) -> Routes:
    return waiting(routes_of(store, t, route('tech', TECH, keys='log|*'), old=False), **given)


def pushed(store: Store) -> list[dict]:
    return store.load()['anomalies']


def investigated(store: Store, key: str = KEY) -> None:
    store.note(key, FOUND, by='pod')


def said(t: Transport) -> str:
    return ' '.join(texts(t.calls[-1][1]))


def test_a_key_route_holds_a_high_card_until_the_investigation_when_post_sev_is_critical(store: Store):
    # given
    t = Transport()
    store.save(state(card()), None)
    routes = keys_route(store, t)

    # when
    first = routes.deliver([], NOW, pushed(store))
    later = routes.deliver(store.load()['anomalies'], NOW + 60)

    # then
    assert (first, later) == (0, 0) and t.calls == []
    assert not store.anomaly(KEY).get('teamsPosts')


def test_a_key_route_sends_after_the_note_with_the_summary_of_the_finding(store: Store):
    # given
    t = Transport()
    store.save(state(card()), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW, pushed(store))

    # when
    investigated(store)
    sent_now = routes.deliver(store.load()['anomalies'], NOW + 120)
    again = routes.deliver(store.load()['anomalies'], NOW + 180)

    # then
    assert (sent_now, again) == (1, 0) and len(t.calls) == 1
    shown = said(t)
    assert 'The checkout pods ran out of memory after the 14:02 deploy' in shown and 'recovered after the rollback' in shown
    assert 'NullPointerException in OrderService' in shown and 'Finding:' in shown, 'the card text stays next to the finding'
    assert [(p['route'], p.get('ok')) for p in store.anomaly(KEY)['teamsPosts']] == [('tech', True)]
    assert store.route_retries() == []


def test_a_key_route_sends_a_high_card_at_once_when_post_sev_is_high(store: Store):
    # given
    t = Transport()
    store.save(state(card()), None)
    routes = keys_route(store, t, post_sev='high')

    # when
    first = routes.deliver([], NOW, pushed(store))

    # then
    assert first == 1 and len(t.calls) == 1
    assert 'Finding' not in said(t)


def test_a_critical_card_goes_at_once_when_post_sev_is_critical(store: Store):
    # given
    t = Transport()
    store.save(state(card('crit')), None)
    routes = keys_route(store, t)

    # when
    first = routes.deliver([], NOW, pushed(store))

    # then
    assert first == 1 and len(t.calls) == 1


def test_post_sev_off_holds_every_card_until_the_investigation(store: Store):
    # given
    t = Transport()
    store.save(state(card('crit')), None)
    routes = keys_route(store, t, post_sev='off')

    # when
    first = routes.deliver([], NOW, pushed(store))

    # then
    assert first == 0 and t.calls == []


def test_with_no_finished_investigation_the_route_sends_the_plain_card_after_the_wait(store: Store):
    # given
    t = Transport()
    store.save(state(card()), None)
    routes = keys_route(store, t, minutes=30)
    routes.deliver([], NOW, pushed(store))

    # when
    early = routes.deliver(store.load()['anomalies'], NOW + 29 * 60)
    due = routes.deliver(store.load()['anomalies'], NOW + 30 * 60)
    again = routes.deliver(store.load()['anomalies'], NOW + 31 * 60)

    # then
    assert (early, due, again) == (0, 1, 0) and len(t.calls) == 1
    assert 'NullPointerException in OrderService' in said(t) and 'Finding' not in said(t)


def test_the_wait_follows_the_setting(store: Store):
    # given
    t = Transport()
    store.save(state(card()), None)
    routes = keys_route(store, t, minutes=5)
    routes.deliver([], NOW, pushed(store))

    # when
    due = routes.deliver(store.load()['anomalies'], NOW + 5 * 60)

    # then
    assert due == 1 and len(t.calls) == 1


def test_an_investigator_that_is_off_or_not_live_sends_at_once(store: Store):
    # given
    t = Transport()
    store.save(state(card()), None)
    routes = keys_route(store, t, live=False)

    # when
    first = routes.deliver([], NOW, pushed(store))

    # then
    assert first == 1 and len(t.calls) == 1


def test_an_old_note_from_an_earlier_firing_is_no_finished_investigation(store: Store):
    # given
    t = Transport()
    store.save(state(card(note={'text': FOUND, 'at': iso_at(NOW - 86400)}, noteBy='pod')), None)
    routes = keys_route(store, t)

    # when
    first = routes.deliver([], NOW, pushed(store))

    # then
    assert first == 0 and t.calls == []


def test_a_card_a_person_took_is_not_held(store: Store):
    # given
    t = Transport()
    store.save(state(card(triage='progress')), None)
    routes = keys_route(store, t)

    # when
    first = routes.deliver([], NOW, pushed(store))

    # then
    assert first == 1 and len(t.calls) == 1


def test_a_held_card_that_is_resolved_meanwhile_is_not_sent(store: Store):
    # given
    t = Transport()
    store.save(state(card()), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW, pushed(store))
    gone = store.load()
    gone['anomalies'][0] |= {'status': 'cleared', 'clearedAt': iso_at(NOW + 60), 'triage': 'handled'}
    store.save(gone, None)

    # when
    later = routes.deliver([], NOW + 31 * 60)

    # then
    assert later == 0 and t.calls == [] and store.route_retries() == []


def test_a_technical_tag_route_holds_the_card_the_same_way(store: Store):
    # given
    t = Transport()
    store.save(state(card(tags=tag('developer'))), None)
    routes = waiting(routes_of(store, t, route('dev', SEO, tags='developer'), old=False))

    # when
    first = routes.deliver(store.load()['anomalies'], NOW)
    investigated(store)
    then = routes.deliver(store.load()['anomalies'], NOW + 60)
    again = routes.deliver(store.load()['anomalies'], NOW + 120)

    # then
    assert (first, then, again) == (0, 1, 0) and len(t.calls) == 1
    assert 'recovered after the rollback' in said(t)


def test_a_technical_tag_route_falls_back_after_the_wait(store: Store):
    # given
    t = Transport()
    store.save(state(card(tags=tag('developer'))), None)
    routes = waiting(routes_of(store, t, route('dev', SEO, tags='developer'), old=False))

    # when
    early = routes.deliver(store.load()['anomalies'], NOW + 60)
    due = routes.deliver(store.load()['anomalies'], NOW + 30 * 60)

    # then
    assert (early, due) == (0, 1) and len(t.calls) == 1


def test_a_business_route_still_waits_only_for_its_sentence(store: Store):
    # given
    t = Transport()
    store.save(state(card(key='log|a|ERROR|said', tags=tag('seo'), business=SAID), card(key='log|a|ERROR|quiet', tags=tag('seo'))), None)
    routes = waiting(routes_of(store, t, route('seo', SEO, tags='seo'), old=False))

    # when
    first = routes.deliver(store.load()['anomalies'], NOW)
    much_later = routes.deliver(store.load()['anomalies'], NOW + 240 * 60)

    # then
    assert (first, much_later) == (1, 0) and len(t.calls) == 1
    assert SAID['text'] in said(t)
    assert not store.anomaly('log|a|ERROR|quiet').get('teamsPosts'), 'a business route never falls back to the plain card'


def test_two_pods_and_many_passes_send_a_held_card_once(store: Store):
    # given
    t = Transport()
    store.save(state(card()), None)
    one, two = keys_route(store, t), keys_route(store, t)
    one.deliver([], NOW, pushed(store))
    two.deliver([], NOW, pushed(store))

    # when
    investigated(store)
    counts = [r.deliver(store.load()['anomalies'], NOW + 60 * i) for i in (1, 2) for r in (one, two)]

    # then
    assert sum(counts) == 1 and len(t.calls) == 1
    assert len(store.anomaly(KEY)['teamsPosts']) == 1


def test_a_hand_send_is_never_held(store: Store):
    # given
    t = Transport()
    store.save(state(card()), None)
    routes = keys_route(store, t)

    # when
    answer = routes.send(KEY, 'teams/tech', 'alice@example.com')

    # then
    assert answer == {'ok': True, 'sent': ['teams/tech']} and len(t.calls) == 1


def test_the_echo_does_not_post_early_and_posts_once_the_route_sends(store: Store):
    # given
    t = Transport()
    fake = Fake(store)
    store.save(state(card()), None)
    routes = keys_route(store, t)
    routes.settings = dataclasses.replace(routes.settings, routes_slack_echo=True)
    routes.echo = fake.echo

    # when
    routes.deliver([], NOW, pushed(store))
    early = (list(fake.posted), list(fake.said))
    investigated(store)
    routes.deliver(store.load()['anomalies'], NOW + 60)

    # then
    assert early == ([], [])
    assert fake.posted == [KEY] and len(fake.said) == 1 and 'tech on Microsoft Teams' in fake.said[0][1]


def test_an_email_key_route_mails_after_the_investigation_with_the_finding(store: Store):
    # given
    runtime = runtime_of(store, Transport(), server(), mail_route(keys='log|*'), plugins=PLUGINS)
    runtime.live = live_of(store, PLUGINS, {'email.min-sev': 'warn'})
    routes = waiting(Routes(store, runtime, settings()))
    store.save(state(card()), None)

    # when
    first = routes.deliver([], NOW, pushed(store))
    early = len(sent())
    investigated(store)
    then = routes.deliver(store.load()['anomalies'], NOW + 60)

    # then
    assert (first, early, then) == (0, 0, 1)
    text, html = parts(sent()[0][1])
    assert 'recovered after the rollback' in text and 'recovered after the rollback' in html
    assert len(Box.tried) == 2 and len(FakeSMTP.made) >= 1
    assert json.dumps(store.route_retries()) == '[]'


def test_in_the_app_the_echo_of_an_investigated_card_fills_its_slack_post_with_the_finding(store: Store, tmp_path):
    # given
    store.save({**state(card(note={'text': FOUND, 'at': iso_at(NOW + 60)}, noteBy='pod')), 'initialized': True}, None)
    slack_t, api = SlackT(), FakeSlackApi()
    given = dataclasses.replace(settings(), routes_slack_echo=True)

    # when
    with TestClient(create_app(given, store, kube=Kube(root=tmp_path), transport=Transport(), slack=Slack('xoxb-test', '#ops', slack_t, api))) as c:
        add_route(c, 'tech', TECH, keys='log|*')
        sent = c.post('/api/routes/send', json={'key': KEY, 'route': 'teams/tech'}, headers=AUTH)
        assert c.app.state.routes.drain(5)
        assert c.app.state.card_posts.drain(5)

    # then
    assert sent.status_code == 200, sent.text
    updates = [p for m, p in api.calls if m == 'chat.update']
    assert updates and 'recovered after the rollback' in json.dumps(updates[-1]), 'the echo post is filled with the finding at once'
    assert store.anomaly(KEY)['slackPost']['body']['filled'] is True


def test_a_held_card_the_investigator_closes_or_groups_is_not_sent(store: Store):
    # given
    t = Transport()
    store.save(state(card(), card(key='log|a|ERROR|kid'), card(key='log|a|ERROR|host', sev='crit')), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW, [store.anomaly(KEY), store.anomaly('log|a|ERROR|kid')])
    given = store.load()
    for a in given['anomalies']:
        if a['key'] == KEY:
            a |= {'triage': 'handled', 'triagedBy': 'pod'}
        if a['key'] == 'log|a|ERROR|kid':
            a |= {'parent': 'log|a|ERROR|host'}
    store.save(given, None)
    investigated(store)
    investigated(store, 'log|a|ERROR|kid')

    # when
    later = routes.deliver([], NOW + 60)

    # then
    assert later == 0 and t.calls == [] and store.route_retries() == []


def test_in_the_app_a_key_route_holds_a_new_high_card_until_the_note_when_post_sev_is_critical(store: Store, tmp_path):
    # given
    found = open_card(KEY, 'high') | {'src': 'log', 'sig': 'err', 'ns': 'shop', 'sub': '', 'now': 5, 'd1': None, 'd7': None, 'unit': '', 'short': 'x', 'slack': 1, 'ranKey': 'log'}
    store.save({**state(), 'initialized': True}, None)
    t = Transport()
    given = dataclasses.replace(settings(), slack_post_sev='crit')
    now = time.time()

    # when
    with TestClient(ready_app(given, store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', TECH, keys='log|*')
        c.app.state.server_apply([PassResult('log', True, 'ok', [found], {}, ('log',))], now, None)
        assert c.app.state.routes.drain(5)
        early = len(t.calls)
        investigated(store)
        c.app.state.server_apply([PassResult('log', True, 'ok', [found], {}, ('log',))], now + 60, None)
        assert c.app.state.routes.drain(5)

    # then
    assert early == 0, 'the High card waits for the investigation'
    assert len(t.calls) == 1 and 'recovered after the rollback' in said(t)


def test_a_finding_from_the_investigator_s_slack_draft_reaches_the_route_as_plain_text(store: Store):
    # given
    t = Transport()
    draft = {'text': 'Orders fail since *14:02*, see <https://grafana.example.com/d/1|the checkout graph> and <https://example.com/status>.', 'by': 'Claude (pod)', 'at': iso_at(NOW + 60)}
    store.save(state(card(note={'text': FOUND, 'at': iso_at(NOW + 60)}, noteBy='pod', slackDraft=draft)), None)
    routes = keys_route(store, t)

    # when
    routes.deliver([], NOW + 60, pushed(store))

    # then
    shown = said(t)
    assert 'Orders fail since 14:02, see the checkout graph (https://grafana.example.com/d/1) and https://example.com/status.' in shown.replace('\\', ''), shown
    assert '<https' not in shown and '|the checkout' not in shown


def test_in_the_app_a_paused_investigator_lets_route_posts_go_at_once(store: Store, tmp_path):
    # given
    found = open_card(KEY, 'high') | {'src': 'log', 'sig': 'err', 'ns': 'shop', 'sub': '', 'now': 5, 'd1': None, 'd7': None, 'unit': '', 'short': 'x', 'slack': 1, 'ranKey': 'log'}
    store.save({**state(), 'initialized': True}, None)
    t = Transport()
    given = dataclasses.replace(settings(), slack_post_sev='crit')
    store.set_investigator_stop(True, 'alice@example.com')

    # when
    with TestClient(ready_app(given, store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', TECH, keys='log|*')
        c.app.state.server_apply([PassResult('log', True, 'ok', [found], {}, ('log',))], time.time(), None)
        assert c.app.state.routes.drain(5)

    # then
    assert len(t.calls) == 1


def test_the_route_wait_from_the_environment_is_kept_within_five_and_240_minutes(monkeypatch):
    # given
    from skopax.config import load_settings

    # when
    monkeypatch.setenv('ROUTES_WAIT_MINUTES', '1')
    low = load_settings(lambda: {})
    monkeypatch.setenv('ROUTES_WAIT_MINUTES', '999')
    high = load_settings(lambda: {})
    monkeypatch.setenv('ROUTES_WAIT_MINUTES', '45')
    fine = load_settings(lambda: {})

    # then
    assert (low.routes_wait_minutes, high.routes_wait_minutes, fine.routes_wait_minutes) == (5, 240, 45)
