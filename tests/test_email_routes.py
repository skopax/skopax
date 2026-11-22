import dataclasses
import email
import email.policy
import hashlib
import smtplib
import time

import pytest

from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import Outbound
from skopax.engine import iso as iso_at
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import email_route, mail_server, teams
from skopax.registry import Registry
from skopax.routes import Route, Routes
from skopax.store import Store
from skopax import mail
from tests.conftest import AUTH, settings, state
from tests.mail_kit import CONFIG, FakeSMTP, faked
from tests.plugin_kit import KIT_KEY, add_instance
from tests.plugins.hook_plugin import Hook, hook_plugin
from tests.teams_kit import AT, SEO, Transport, live_of, open_card, route as teams_route, runtime_of, tag
from tests.test_route_slack_echo import Fake

NOW = 1_794_240_000.0
ALICE = 'alice@example.com'
BOB = 'bob@example.com'
CAROL = 'carol@example.com'
PLUGINS = (teams.PLUGIN, mail_server.PLUGIN, email_route.PLUGIN)
SAID = {'text': 'We recommend adding a redirect for the old spring sale page.', 'why': 'Visitors from the old campaign land on an empty page.', 'by': 'pod', 'at': AT}
INFO = {'text': 'Orders cannot be finished right now; engineering is fixing it.', 'kind': 'info', 'by': 'pod', 'at': AT}
DEFAULTS = {'email.repeat-days': '0', 'email.fixed-posts': 'true', 'email.min-sev': 'warn'}


class Box(FakeSMTP):
    refuse: set[str] = set()
    down: set[str] = set()
    tried: list[str] = []

    def send_message(self, message, from_addr=None, to_addrs=None):
        [to] = to_addrs
        Box.tried.append(to)
        if to in Box.down:
            raise smtplib.SMTPServerDisconnected('gone')
        if to in Box.refuse:
            raise smtplib.SMTPRecipientsRefused({to: (550, b'5.1.1 no such user')})
        return super().send_message(message, from_addr, to_addrs)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    faked(monkeypatch)
    Box.refuse, Box.down, Box.tried = set(), set(), []
    monkeypatch.setattr(smtplib, 'SMTP', Box)
    email_route.DELIVERED.clear()
    yield
    email_route.DELIVERED.clear()


def mail_route(name: str = 'ops', recipients: str = f'{ALICE}, {BOB}', **values) -> tuple:
    return email_route.PLUGIN, name, {'recipients': recipients} | {k.replace('_', '-'): v for k, v in values.items()}


def server() -> tuple:
    return mail_server.PLUGIN, 'mail', dict(CONFIG)


def routes_with(store, *specs, values: dict | None = None, attribution=None):
    runtime = runtime_of(store, Transport(), *specs, plugins=PLUGINS)
    runtime.live = live_of(store, PLUGINS, DEFAULTS | (values or {}))
    return Routes(store, runtime, settings(), attribution=attribution or (lambda: None))


def sent() -> list[tuple[str, email.message.EmailMessage]]:
    out = []
    for client in FakeSMTP.made:
        for message, _, to in client.sent:
            [addr] = to
            out.append((addr, email.message_from_bytes(message.as_bytes(), policy=email.policy.default)))
    return out


def parts(message) -> tuple[str, str]:
    return message.get_body(('plain',)).get_content(), message.get_body(('html',)).get_content()


def test_an_email_instance_is_a_route_with_its_recipients_tags_keys_and_lowest_severity(store: Store):
    # given
    specs = (server(), mail_route('ops', f'{BOB}  {ALICE},{ALICE}', tags='seo, developer', keys='log|checkout-*|*', min_sev='crit'))

    # when
    found = routes_with(store, *specs).routes()

    # then
    target = hashlib.sha256(f'{ALICE} {BOB}'.encode()).hexdigest()[:16]
    assert found == [Route('email/ops', 'ops', 'email', 'ops', frozenset({'seo', 'developer'}), (), target, ('log|checkout-*|*',), 'crit')]
    assert Registry.of(*PLUGINS).get('email').state == 'ok'
    assert (email_route.PLUGIN.name, email_route.PLUGIN.category, email_route.PLUGIN.outbound.mentions_field) == ('Email', 'chat', None)
    assert {f.key for f in email_route.PLUGIN.fields} >= {'recipients', 'tags', 'keys', 'min-sev'} and not email_route.PLUGIN.capabilities.secrets


@pytest.mark.parametrize('given, ok', [(ALICE, True), (f'{ALICE}, {BOB} {CAROL}', True), ('', False), (f'{ALICE}, bob', False), ('Alice <alice@example.com>', False),
                                       (', '.join(f'u{i}@example.com' for i in range(51)), False), (', '.join(f'u{i}@example.com' for i in range(50)), True)])
def test_recipients_are_one_to_fifty_plain_addresses(given, ok):
    # when
    problem = email_route.recipients_problem(given)

    # then
    assert (problem is None) is ok


def test_a_due_card_is_mailed_once_to_each_recipient_and_recorded_and_not_again_on_the_next_pass(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(tags='developer'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', num=12, tags=tag('developer'))), None)

    # when
    first = routes.deliver(store.load()['anomalies'], NOW)
    again = routes.deliver(store.load()['anomalies'], NOW + 60)

    # then
    assert (first, again) == (1, 0)
    mails = sent()
    assert [to for to, _ in mails] == [ALICE, BOB]
    assert all(m['To'] == to for to, m in mails), 'each recipient gets a mail of their own'
    subject = mails[0][1]['Subject']
    assert subject.startswith('[Skopax] ') and '#12 checkout-service / ERROR lines' in subject
    text, html = parts(mails[0][1])
    assert 'NullPointerException in OrderService' in text and 'Status: Firing' in text and 'Severity: Warning' in text
    assert 'Open the card in Skopax: https://skopax.example.com/c/12' in text
    assert mails[0][1]['X-Skopax-Kind'] == 'route'
    posts = store.anomaly('log|checkout-service|ERROR|boom')['teamsPosts']
    assert [(p['route'], p['plugin'], p.get('ok')) for p in posts] == [('ops', 'email', True)]


def test_a_tag_route_mails_the_business_sentence_and_recommendation(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(tags='seo'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('seo'), business=SAID)), None)

    # when
    routes.deliver(store.load()['anomalies'], NOW)

    # then
    [(_, first), (_, second)] = sent()
    text, _ = parts(first)
    assert 'Why: Visitors from the old campaign land on an empty page.' in text
    assert 'Recommended: We recommend adding a redirect for the old spring sale page.' in text
    assert 'Status: Firing' in text
    assert first['Subject'] == second['Subject']


def test_a_key_route_post_of_two_cards_mails_each_card(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(keys='log|*'))
    cards = [open_card('log|checkout-service|ERROR|one'), open_card('log|checkout-service|ERROR|two')]

    # when
    out = routes.push(cards)

    # then
    assert sorted(out['email/ops']) == ['log|checkout-service|ERROR|one', 'log|checkout-service|ERROR|two']
    assert len(sent()) == 4


def test_mail_down_is_tried_again_later_without_mailing_a_recipient_twice(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(recipients=f'{ALICE}, {BOB}, {CAROL}', tags='developer'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), None)
    Box.down = {BOB}

    # when
    first = routes.deliver(store.load()['anomalies'], NOW)
    rows = store.route_retries()
    Box.down = set()
    later = routes.deliver(store.load()['anomalies'], NOW + 60)

    # then
    assert first == 0 and [(r['tries'], r['gaveUp']) for r in rows] == [(1, False)]
    assert 'stopped answering' in rows[0]['reason']
    assert later == 1 and [to for to, _ in sent()] == [ALICE, BOB, CAROL], 'alice got hers on the first try and is not mailed again'
    assert store.route_retries() == [] and routes.failure('email', 'ops') is None


def test_a_slow_list_is_mailed_in_parts_within_the_post_time_and_each_address_once(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(email_route, 'BUDGET_S', 0.0)
    routes = routes_with(store, server(), mail_route(recipients=f'{ALICE}, {BOB}, {CAROL}', tags='developer'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), None)

    # when
    first = routes.deliver(store.load()['anomalies'], NOW)
    [row] = store.route_retries()
    counts = [routes.deliver(store.load()['anomalies'], NOW + offset) for offset in (60, 120, 180)]

    # then
    assert (first, counts) == (0, [0, 1, 0])
    assert (row['tries'], row['gaveUp'], row['reason']) == (0, False, msg('email.partial', sent=1, total=3)), 'a post that made progress costs no try'
    assert Box.tried == [ALICE, BOB, CAROL]
    assert store.route_retries() == [] and len(store.anomaly('log|checkout-service|ERROR|boom')['teamsPosts']) == 1


def test_a_refused_mail_is_not_tried_again_and_the_tile_says_why(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(recipients=ALICE, tags='developer'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), None)
    Box.refuse = {ALICE}

    # when
    first = routes.deliver(store.load()['anomalies'], NOW)
    later = routes.deliver(store.load()['anomalies'], NOW + 3600)

    # then
    assert (first, later) == (0, 0) and sent() == []
    [row] = store.route_retries()
    assert row['gaveUp'] is True and row['tries'] == 1
    assert routes.failure('email', 'ops') == 'error: ops: The mail server refused: 550 5.1.1 no such user'
    assert routes.gave_up() == [{'id': 'email/ops', 'plugin': 'Email', 'route': 'ops', 'count': 1, 'reason': 'The mail server refused: 550 5.1.1 no such user', 'tries': 1}]


def test_one_refused_address_is_skipped_and_the_others_still_get_every_card(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(keys='log|*'))
    Box.refuse = {BOB}
    cards = [open_card('log|checkout-service|ERROR|one'), open_card('log|checkout-service|ERROR|two')]

    # when
    out = routes.push(cards)

    # then
    assert sorted(out['email/ops']) == ['log|checkout-service|ERROR|one', 'log|checkout-service|ERROR|two']
    assert [to for to, _ in sent()] == [ALICE, ALICE]
    assert Box.tried == [ALICE, BOB, ALICE], 'bob is not tried again after his first refusal'
    assert BOB in routes.failure('email', 'ops') and '550' in routes.failure('email', 'ops')


def test_an_info_card_that_closes_gets_one_fixed_mail(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(tags='seo'))
    store.save(state(open_card('log|a|ERROR|cart', tags=tag('seo'), business=INFO)), None)
    now = time.time()
    routes.tagged(store.load()['anomalies'], now=now)
    store.anomalies.update_one({'_id': 'log|a|ERROR|cart'}, {'$set': {'status': 'cleared', 'clearedAt': iso_at(now + 60)}})

    # when
    later = routes.tagged(store.load()['anomalies'], now=now + 760)

    # then
    assert later == {'email/ops:fixed': ['log|a|ERROR|cart']}
    mails = sent()
    assert len(mails) == 4
    text, _ = parts(mails[2][1])
    assert 'Stopped firing: Orders cannot be finished right now; engineering is fixing it.' in text and 'Status: Stopped firing' in text
    assert 'stopped firing' in mails[2][1]['Subject']


def test_an_email_route_echoes_to_slack(store: Store):
    # given
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('seo'), business=INFO)), None)
    fake = Fake(store)
    routes = routes_with(store, server(), mail_route(tags='seo', **{'slack-echo': 'on'}))
    routes.echo = fake.echo

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert fake.posted == ['log|checkout-service|ERROR|boom']
    [(_, line)] = fake.said
    assert 'ops on Email' in line


def test_without_a_mail_server_the_route_is_off_and_says_why_and_nothing_is_sent(store: Store):
    # given
    routes = routes_with(store, mail_route(tags='developer'))
    runtime = routes.runtime
    item = runtime.instances_of('email')[0]
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), None)

    # when
    found = routes.routes()
    problem = runtime.route_problem(email_route.PLUGIN, item)
    delivered = routes.deliver(store.load()['anomalies'], NOW)
    tested = runtime.connection_test(email_route.PLUGIN, 'ops', {}, 30, NOW)

    # then
    assert found == [] and delivered == 0 and FakeSMTP.made == []
    assert problem == msg('email.no_server')
    assert tested['ok'] is False and tested['summary'] == msg('email.no_server')


def test_open_cards_are_mailed_once_the_mail_server_is_turned_on(store: Store):
    # given
    routes = routes_with(store, mail_route(tags='developer'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), None)
    off = routes.deliver(store.load()['anomalies'], NOW)

    # when
    add_instance(store, mail_server.PLUGIN, 'mail', sealer_key=KIT_KEY, primary=True, **CONFIG)
    routes.runtime.refresh()
    on = routes.deliver(store.load()['anomalies'], NOW + 60)

    # then
    assert (off, on) == (0, 1) and [to for to, _ in sent()] == [ALICE, BOB]


def test_test_connection_checks_the_mail_server_and_sends_nothing(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(keys='log|*'))

    # when
    tested = routes.runtime.connection_test(email_route.PLUGIN, 'ops', {}, 30, NOW)
    empty = routes.runtime.connection_test(email_route.PLUGIN, 'ops', {'keys': ''}, 30, NOW)

    # then
    assert (tested['ok'], tested['summary']) == (True, 'The mail server at smtp.example.com:587 answered over STARTTLS and took the sign-in of alerts@example.com. Each card goes to 2 recipients, one mail each; no mail was sent')
    assert sent() == [] and 'noop' in FakeSMTP.made[0].steps
    assert (empty['ok'], empty['summary']) == (False, msg('integrations.route_empty'))


def test_card_text_is_html_escaped(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(recipients=ALICE, keys='log|*'))
    evil = open_card('log|checkout-service|ERROR|boom', label='<script>alert(1)</script>', what='a & b <img src=x onerror=alert(1)>')

    # when
    routes.push([evil])

    # then
    [(_, message)] = sent()
    _, html = parts(message)
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '<img' not in html and 'a &amp; b &lt;img' in html


def test_teams_routes_are_unchanged_next_to_an_email_route(store: Store):
    # given
    t = Transport()
    runtime = runtime_of(store, t, server(), mail_route(keys='log|*'), (teams.PLUGIN, *teams_route('seo', SEO, keys='log|*')), plugins=PLUGINS)
    runtime.live = live_of(store, PLUGINS, DEFAULTS)
    routes = Routes(store, runtime, settings())

    # when
    out = routes.push([open_card('log|checkout-service|ERROR|boom')])

    # then
    assert sorted(out) == ['email/ops', 'teams/seo']
    assert [u for u, _ in t.calls] == [SEO] and len(sent()) == 2
    assert [r.link for r in routes.routes() if r.plugin == 'teams'] == [hashlib.sha256(SEO.encode()).hexdigest()[:16]]


@pytest.mark.parametrize('over', [{'link_field': None, 'target_field': 'nope'}, {'link_field': None, 'target_field': 'url'}, {'target_field': 'tags', 'link_field': 'url'}, {'ready': 'ready'}])
def test_outbound_target_and_ready_must_be_well_formed(over):
    # given
    hook = Hook()
    outbound = dataclasses.replace(Outbound(hook.post, 'tags', 'mentions', link_field='url', keys_field='keys'), **over)

    # when
    found = Registry.of(hook_plugin(hook, outbound=outbound)).get('hook')

    # then
    assert found.state == 'broken' and found.reason.endswith(': ' + msg('plugins.why.outbound'))


def tiles(client) -> dict:
    row = next(p for p in client.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'email')
    return {r['name']: r for r in row['health']['instances']}


def add(client, plugin: str, name: str, values: dict):
    return client.post(f'/api/admin/plugins/{plugin}/instances', json={'name': name, 'values': values, 'enabled': True}, headers=AUTH)


def test_the_app_shows_email_needs_the_mail_server_until_one_is_added_and_refuses_a_bad_list(store: Store, tmp_path):
    # given
    store.save({**state(), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=Transport())) as c:
        bad = add(c, 'email', 'bad', {'recipients': f'{ALICE}, bob', 'tags': 'developer'})
        made = add(c, 'email', 'ops', {'recipients': f'{ALICE} {BOB}', 'tags': 'developer'})
        before = tiles(c)['ops']
        refused = c.post('/api/admin/plugins/email/test', json={'name': 'ops'}, headers=AUTH).json()
        server = add(c, 'mail-server', 'mail', dict(CONFIG))
        after = tiles(c)['ops']
        tested = c.post('/api/admin/plugins/email/test', json={'name': 'ops'}, headers=AUTH).json()
        found = [r.id for r in c.app.state.routes.routes()]

    # then
    assert bad.status_code >= 400 and 'bob is not an email address' in bad.text
    assert (made.status_code, server.status_code) == (201, 201)
    assert before == {'name': 'ops', 'state': 'error', 'text': msg('email.no_server'), 'at': None}
    assert (refused['ok'], refused['summary']) == (False, msg('email.no_server'))
    assert after['state'] != 'error' and found == ['email/ops']
    assert tested['ok'] is True and 'no mail was sent' in tested['summary'] and sent() == []


def test_a_refused_list_gives_up_at_once_and_the_notice_says_one_try(store: Store, tmp_path):
    # given
    store.save({**state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), 'initialized': True}, None)
    Box.refuse = {ALICE}

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=Transport())) as c:
        add(c, 'mail-server', 'mail', dict(CONFIG))
        add(c, 'email', 'ops', {'recipients': ALICE, 'tags': 'developer'})
        c.app.state.routes.deliver(store.load()['anomalies'], time.time())
        notices = c.get('/api/admin/notices', headers=AUTH).json()['items']
        tile = tiles(c)['ops']

    # then
    [notice] = [i for i in notices if i['id'].startswith('route')]
    assert notice['id'] == 'route-gave-up:email/ops' and 'after 1 failed try ' in notice['text'] and '550 5.1.1 no such user' in notice['text']
    assert tile['state'] == 'error' and tile['text'] == 'error: ops: The mail server refused: 550 5.1.1 no such user'


def test_a_text_target_without_a_link_loads():
    # given
    hook = Hook()
    outbound = Outbound(hook.post, 'tags', 'mentions', keys_field='keys', target_field='mentions', ready=lambda ctx: None)

    # when
    found = Registry.of(hook_plugin(hook, outbound=outbound)).get('hook')

    # then
    assert found.state == 'ok'


def test_a_refused_sign_in_costs_one_session_and_the_card_stays_retryable(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(recipients=f'{ALICE}, {BOB}, {CAROL}', tags='developer'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), None)
    FakeSMTP.fail = {'login': smtplib.SMTPAuthenticationError(535, b'5.7.8 bad credentials')}

    # when
    first = routes.deliver(store.load()['anomalies'], NOW)
    sessions = len(FakeSMTP.made)
    [row] = store.route_retries()
    FakeSMTP.fail = {}
    later = routes.deliver(store.load()['anomalies'], NOW + 60)

    # then
    assert first == 0 and sessions == 1, 'one refused sign-in, not one per address and no probe'
    assert (row['tries'], row['gaveUp'], row['reason']) == (1, False, msg('mailserver.login'))
    assert later == 1 and [to for to, _ in sent()] == [ALICE, BOB, CAROL]


def test_a_refused_sender_marks_no_address_refused_and_the_card_stays_retryable(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(recipients=f'{ALICE}, {BOB}, {CAROL}', tags='developer'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), None)
    FakeSMTP.fail = {'send': smtplib.SMTPSenderRefused(553, b'5.7.1 sender not owned', 'alerts@example.com')}

    # when
    first = routes.deliver(store.load()['anomalies'], NOW)
    tried, sessions = list(Box.tried), len(FakeSMTP.made)
    [row] = store.route_retries()
    failure = routes.failure('email', 'ops')
    FakeSMTP.fail = {}
    later = routes.deliver(store.load()['anomalies'], NOW + 60)

    # then
    assert first == 0 and tried == [ALICE] and sessions == 1, 'the post stops at the first server-wide refusal'
    assert (row['tries'], row['gaveUp']) == (1, False) and '553' in row['reason']
    assert failure == f"error: ops: {row['reason']}"
    assert later == 1 and Box.tried == [ALICE, ALICE, BOB, CAROL], 'no address was marked refused'


def test_a_refused_recipient_marks_only_that_address_and_opens_no_probe(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(recipients=f'{ALICE}, {BOB}, {CAROL}', tags='developer'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), None)
    Box.refuse = {BOB}

    # when
    first = routes.deliver(store.load()['anomalies'], NOW)

    # then
    assert first == 1 and [to for to, _ in sent()] == [ALICE, CAROL]
    assert Box.tried == [ALICE, BOB, CAROL] and len(FakeSMTP.made) == 3, 'one session per address and no probe'
    assert BOB in routes.failure('email', 'ops') and ALICE not in routes.failure('email', 'ops') and CAROL not in routes.failure('email', 'ops')


def test_two_refused_recipients_are_both_marked_and_the_third_is_still_mailed(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(recipients=f'{ALICE}, {BOB}, {CAROL}', tags='developer'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), None)
    Box.refuse = {ALICE, BOB}

    # when
    first = routes.deliver(store.load()['anomalies'], NOW)

    # then
    assert first == 1 and [to for to, _ in sent()] == [CAROL]
    assert Box.tried == [ALICE, BOB, CAROL], 'no server-wide stop after two refused recipients'
    assert routes.failure('email', 'ops') == 'error: ops: ' + msg('email.some_refused', addresses=f'{ALICE}, {BOB}', reason='The mail server refused: 550 5.1.1 no such user')
    assert store.route_retries() == []


def test_a_refused_sender_after_a_refused_recipient_keeps_that_mark_and_the_card_retryable(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(recipients=f'{ALICE}, {BOB}, {CAROL}', tags='developer'))
    store.save(state(open_card('log|checkout-service|ERROR|boom', tags=tag('developer'))), None)
    Box.refuse = {ALICE}
    real = Box.send_message

    def then_sender_refused(box, message, from_addr=None, to_addrs=None):
        if to_addrs == [BOB]:
            Box.tried.append(BOB)
            raise smtplib.SMTPSenderRefused(553, b'5.7.1 sender not owned', 'alerts@example.com')
        return real(box, message, from_addr, to_addrs)
    Box.send_message = then_sender_refused

    # when
    try:
        first = routes.deliver(store.load()['anomalies'], NOW)
    finally:
        Box.send_message = real
    marks = {mark[3]: kept for mark, (_, kept, _) in email_route.DELIVERED.items()}
    [row] = store.route_retries()
    later = routes.deliver(store.load()['anomalies'], NOW + 60)

    # then
    assert first == 0 and marks == {ALICE: email_route.REFUSED}, 'alice stays refused and bob is not marked'
    assert (row['tries'], row['gaveUp']) == (1, False) and '553' in row['reason']
    assert later == 1 and Box.tried == [ALICE, BOB, BOB, CAROL], 'alice is not tried again'


def test_the_mailer_gives_up_on_a_connection_within_the_post_time(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(recipients=ALICE, keys='log|*'))

    # when
    routes.push([open_card('log|checkout-service|ERROR|boom')])

    # then
    assert FakeSMTP.made and all(c.timeout <= email_route.BUDGET_S for c in FakeSMTP.made)


def test_a_post_with_nothing_to_mail_says_so(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(keys='log|*'))
    runtime = routes.runtime
    plugin = next(p for p in runtime.active() if p.id == 'email')
    ctx = runtime.context(plugin, runtime.instances_of('email')[0], NOW)

    # when
    answer = email_route.post(ctx, {'kind': 'card', 'title': 'Skopax', 'cards': []})

    # then
    assert answer == {'ok': False, 'status': 0, 'reason': msg('email.nothing'), 'retry': False} and FakeSMTP.made == []


def test_one_recipient_and_one_try_read_in_the_singular(store: Store):
    # given
    routes = routes_with(store, server(), mail_route(recipients=ALICE, keys='log|*'))

    # when
    tested = routes.runtime.connection_test(email_route.PLUGIN, 'ops', {}, 30, NOW)

    # then
    assert 'goes to 1 recipient, one mail' in tested['summary']
    assert msg('routes.gave_up_one', plugin='Email', route='ops', count=1, reason='x').count('after 1 failed try ') == 1
