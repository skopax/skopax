import asyncio
import dataclasses
import logging
import socket
import ssl
import threading
import time
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.plugin_kit import add_instance, registry_with
from tests.plugins import demo_plugin
from tests.plugins.base import minimal
from tests.test_auth import sso_settings
from tests.test_instances import pem
from tests.test_role_classes import OPERATOR, people
from skopax import probe, remote, runtime as runtime_module, selfreview, toolusage
from skopax.contract import Field
from skopax.actors import BOOTSTRAP_TOKEN
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.engine import apply, iso, reach_of
from skopax.instances import Instances, PluginState
from skopax.investigator import toolset
from skopax.investigator.toolset import ToolSet
from skopax.messages import msg
from skopax.probe import CERT_RULES, Probe, cert_pass, check, http_status, public, summary
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.sessions import Sessions, session_secret
from skopax.store import Store
from skopax.users import Users

DEMO = demo_plugin.PLUGIN
DEMO_DIR = Path(demo_plugin.__file__).parent
NOW = 1_792_224_240.0
DAY = 86400
ADMIN = 'dan@example.com'
PROM = {'name': 'prometheus-main', 'plugin': 'prometheus', 'instance': 'main', 'url': 'http://prometheus.example.com:9090/-/ready'}
ES = {'name': 'elastic-main', 'plugin': 'elastic', 'instance': 'main', 'url': 'https://elastic.example.com:9200/'}
EXPIRES = '2026-12-01T00:00:00Z'
REPORT = {'at': '2026-10-19T19:04:00Z', 'targets': [{'name': 'demo', 'plugin': 'demo', 'instance': 'main', 'url': 'https://demo.example.com/health', 'ok': False, 'status': None,
                                                    'ms': 12, 'error': 'dns: gaierror', 'certDays': None}]}


class Net:
    def __init__(self, ips=('10.0.0.7',), dns: Exception | None = None, tcp: Exception | None = None, answers: list | None = None, expires: str | None = EXPIRES, hold: float = 0.0):
        self.ips, self.dns, self.tcp, self.expires, self.hold = ips, dns, tcp, expires, hold
        self.answers = list(answers or [200])
        self.connects: list[tuple[str, int]] = []
        self.fetches: list[tuple[str, ssl.SSLContext | None]] = []

    def resolve(self, host, port, proto=0):
        if self.dns:
            raise self.dns
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, '', (ip, port)) for ip in self.ips]

    def connect(self, address, timeout=None):
        self.connects.append(address)
        if self.hold:
            time.sleep(self.hold)
        if self.tcp:
            raise self.tcp
        return Closable()

    def fetch(self, url, context, timeout):
        self.fetches.append((url, context))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def cert(self, host, port, timeout, connect):
        return self.expires

    def check(self, target, timeout=None):
        return check(target, timeout, resolve=self.resolve, connect=self.connect, fetch=self.fetch, cert=self.cert)


class Closable:
    def close(self):
        pass


class FakeProbe:
    def __init__(self, report: dict | None = None):
        self.report = report or REPORT
        self.runs: list[float] = []
        self.loops: list[bool] = []

    def run(self, now: float) -> dict:
        try:
            asyncio.get_running_loop()
            self.loops.append(True)
        except RuntimeError:
            self.loops.append(False)
        self.runs.append(now)
        return self.report


def cert_error(message: str, code: int | None = None) -> ssl.SSLCertVerificationError:
    e = ssl.SSLCertVerificationError(1, f'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: {message}')
    e.verify_message = message
    if code is not None:
        e.verify_code = code
    return e


def probed(host: str, days: float, now: float = NOW) -> dict:
    return {'name': host, 'plugin': 'demo', 'instance': 'main', 'url': f'https://{host}/health', 'ok': True, 'status': 200, 'ms': 5, 'error': None,
            'certDays': days, 'certExpires': iso(now + days * 86400)}


def wait_for(done, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not done() and time.monotonic() < deadline:
        time.sleep(0.02)


def demo_runtime(store: Store) -> Runtime:
    registry = registry_with(DEMO)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))
    runtime.refresh()
    return runtime


def demo_app(store: Store, s=None, **kw) -> TestClient:
    return TestClient(create_app(s or settings(), store, registry=Registry.of(DEMO, directory=DEMO_DIR), **kw), base_url='https://testserver')


def test_a_name_that_does_not_resolve_stops_at_dns_with_a_fixed_code():
    # given
    net = Net(dns=socket.gaierror(-2, 'Name or service not known for prometheus.example.com'))

    # when
    r = net.check(PROM)

    # then
    assert (r['ok'], r['status'], r['error'], r['certDays']) == (False, None, 'dns: gaierror', None)
    assert (r['name'], r['plugin'], r['instance'], r['url']) == ('prometheus-main', 'prometheus', 'main', PROM['url'])
    assert net.connects == []


def test_a_tcp_failure_stops_at_tcp_and_names_the_kind_only():
    # given
    net = Net(tcp=ConnectionRefusedError(111, 'Connection refused by 10.0.0.7'))
    slow = Net(tcp=TimeoutError('timed out'))

    # when
    refused = net.check(ES)
    late = slow.check(ES)

    # then
    assert (refused['ok'], refused['status'], refused['error']) == (False, None, 'tcp: ConnectionRefusedError')
    assert late['error'] == 'tcp: TimeoutError'
    assert net.connects == [('elastic.example.com', 9200)]
    assert net.fetches == []


def test_a_plain_http_service_answering_is_ok():
    # given
    net = Net()

    # when
    r = net.check(PROM)

    # then
    assert (r['ok'], r['status'], r['error'], r['certDays']) == (True, 200, None, None)
    assert isinstance(r['ms'], int)
    assert net.connects == [('prometheus.example.com', 9090)]
    assert net.fetches == [(PROM['url'], None)]


def test_https_with_a_trusted_cert_reports_the_status_even_when_it_needs_a_key():
    # given
    net = Net(answers=[401])

    # when
    r = net.check(ES)

    # then
    assert (r['ok'], r['status'], r['error'], r['certExpires']) == (True, 401, None, EXPIRES)
    context = net.fetches[0][1]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_a_server_error_answer_is_reached_but_not_ok():
    # given
    net = Net(answers=[503])

    # when
    r = net.check(ES)

    # then
    assert (r['ok'], r['status'], r['error']) == (False, 503, 'http 503')
    assert r['certExpires'] == EXPIRES, 'the certificate verified, so its expiry still counts'


def test_an_untrusted_cert_stops_at_tls_and_is_never_retried_without_verification():
    # given
    net = Net(answers=[cert_error('self-signed certificate in certificate chain')])

    # when
    r = net.check(ES)

    # then
    assert (r['ok'], r['status'], r['error'], r['certExpires']) == (False, None, 'tls: certificate not trusted', None)
    assert len(net.fetches) == 1


def test_other_tls_and_read_failures_name_their_stage_and_kind_only():
    # given
    handshake = Net(answers=[ssl.SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number at https://elastic.example.com/?token=abc')])
    read = Net(answers=[TimeoutError('The read operation timed out')])
    odd = Net(answers=[ValueError('bad status line from https://user:pw@prometheus.example.com')])

    # when
    tls = handshake.check(ES)
    http = read.check(PROM)
    other = odd.check(PROM)

    # then
    assert (tls['ok'], tls['error']) == (False, 'tls: SSLError')
    assert (http['ok'], http['error']) == (False, 'http: TimeoutError')
    assert (other['ok'], other['error']) == (False, 'http: ValueError')


def test_a_url_that_is_not_http_is_refused_without_a_lookup():
    # given
    net = Net()

    # when
    r = net.check({**PROM, 'url': 'ftp://files.example.com/'})

    # then
    assert (r['ok'], r['error']) == (False, 'url')
    assert net.connects == [] and net.fetches == []


def test_http_status_reads_error_codes_and_unwraps_connection_failures():
    # given
    def opener(answer):
        def open_(req, timeout=None, context=None):
            raise answer
        return open_

    refused = urllib.error.HTTPError('https://elastic.example.com/', 401, 'Unauthorized', {}, None)
    untrusted = urllib.error.URLError(cert_error('unable to get local issuer certificate'))
    slow = urllib.error.URLError(TimeoutError('timed out'))

    # then
    assert http_status('https://elastic.example.com/', None, 8, opener(refused)) == 401
    with pytest.raises(ssl.SSLCertVerificationError):
        http_status('https://elastic.example.com/', None, 8, opener(untrusted))
    with pytest.raises(TimeoutError):
        http_status('https://elastic.example.com/', None, 8, opener(slow))


def test_a_target_with_a_pem_is_verified_against_that_ca_and_a_changed_cert_is_not_trusted():
    # given
    ca = pem()
    net = Net(answers=[200, cert_error('unable to get local issuer certificate')])
    pinned = {**ES, 'pem': ca}

    # when
    ok = net.check(pinned)
    changed = net.check(pinned)

    # then
    assert (ok['ok'], ok['status'], ok['certExpires']) == (True, 200, EXPIRES)
    context = net.fetches[0][1]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()['x509'] == 1, 'only the pinned certificate is trusted, not the system roots'
    assert (changed['ok'], changed['error'], changed['certExpires']) == (False, 'tls: certificate not trusted', None)
    assert 'pem' not in ok


def test_the_probe_checks_every_target_and_keeps_their_order():
    # given
    targets = [PROM, ES, {**PROM, 'name': 'prometheus-east', 'instance': 'east'}]
    seen = []

    def fake(target, timeout):
        seen.append(target['name'])
        return {**Net().check(target), 'certExpires': EXPIRES if target['url'].startswith('https') else None}

    # when
    report = Probe(lambda: targets, checker=fake).run(NOW)

    # then
    assert [r['name'] for r in report['targets']] == ['prometheus-main', 'elastic-main', 'prometheus-east']
    assert sorted(seen) == sorted(t['name'] for t in targets)
    assert report['at'] == iso(NOW)
    assert set(report) == {'at', 'targets'}
    es = report['targets'][1]
    assert set(es) == {'name', 'plugin', 'instance', 'url', 'ok', 'status', 'ms', 'error', 'certDays', 'certExpires'}
    assert es['certDays'] == round((1_796_083_200.0 - NOW) / 86400, 1)
    assert report['targets'][0]['certDays'] is None


def test_a_probe_without_targets_reports_none():
    # when
    report = Probe(lambda: [], checker=lambda target, timeout: {}).run(NOW)

    # then
    assert report == {'at': iso(NOW), 'targets': []}


def test_a_hanging_target_is_bounded_and_off_the_loop(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(probe, 'TIMEOUT', 0.2)
    release = threading.Event()
    slow = Net(hold=2.0)
    stuck = Net()
    stuck.connect = lambda address, timeout=None: release.wait() and Closable()
    targets = [{**PROM, 'name': 'slow'}, {**PROM, 'name': 'stuck'}, PROM]

    def checker(target, timeout):
        return {'slow': slow, 'stuck': stuck}.get(target['name'], Net()).check(target, timeout)

    fake = FakeProbe()
    on = dataclasses.replace(settings(), probe=True)

    # when
    began = time.monotonic()
    report = Probe(lambda: targets, checker=checker).run(NOW)
    took = time.monotonic() - began
    rows = {r['name']: dict(r) for r in report['targets']}
    release.set()
    time.sleep(0.05)
    with TestClient(create_app(on, store, probe=fake)):
        wait_for(lambda: fake.runs)

    # then
    assert took < 1.0
    assert (rows['slow']['ok'], rows['slow']['error'], rows['slow']['status']) == (False, 'timeout', None)
    assert (rows['stuck']['ok'], rows['stuck']['error']) == (False, 'timeout')
    assert (rows['prometheus-main']['ok'], rows['prometheus-main']['status']) == (True, 200)
    assert {r['name']: dict(r) for r in report['targets']} == rows, 'a thread that finishes after its deadline never writes into the report'
    assert fake.loops == [False], 'the startup probe runs in a worker thread, never on the event loop'


def test_more_targets_than_workers_still_finish_by_their_deadlines(monkeypatch):
    # given
    monkeypatch.setattr(probe, 'TIMEOUT', 0.2)
    release = threading.Event()
    stuck = Net()
    stuck.connect = lambda address, timeout=None: release.wait() and Closable()
    targets = [{**PROM, 'name': f'stuck-{i}'} for i in range(4)]

    # when
    began = time.monotonic()
    report = Probe(lambda: targets, workers=2, checker=stuck.check).run(NOW)
    took = time.monotonic() - began
    release.set()

    # then
    assert 0.35 <= took < 2.0, 'two waves of two stuck targets, each cut at its 0.2 s deadline'
    assert [r['error'] for r in report['targets']] == ['timeout'] * 4


def test_a_raising_checker_fails_only_its_target():
    # given
    def checker(target, timeout):
        if target['name'] == 'elastic-main':
            raise RuntimeError('https://user:secret@elastic.example.com broke')
        return Net().check(target, timeout)

    # when
    report = Probe(lambda: [PROM, ES], checker=checker).run(NOW)

    # then
    assert [(r['name'], r['ok'], r['error']) for r in report['targets']] == [('prometheus-main', True, None), ('elastic-main', False, 'probe: RuntimeError')]
    assert 'secret' not in str(report)


def test_an_expired_certificate_keeps_its_expiry_and_a_crit_card():
    # given
    now = time.time()
    gone = iso(now - 2 * DAY)
    net = Net(answers=[cert_error('certificate has expired', 10)], expires=gone)
    chain = Net(answers=[cert_error('certificate has expired', 10)], expires=iso(now + 100 * DAY))
    other = Net(answers=[cert_error('unable to get local issuer certificate', 20)], expires=gone)

    # when
    report = Probe(lambda: [ES], checker=net.check).run(now)
    [result] = cert_pass(report, now, CERT_RULES)
    middle = Probe(lambda: [ES], checker=chain.check).run(now)
    [later] = cert_pass(middle, now, CERT_RULES)
    untrusted = other.check(ES)

    # then
    row = report['targets'][0]
    assert (row['ok'], row['status'], row['error'], row['certExpires'], row['certDays']) == (False, None, 'tls: certificate expired', gone, -2.0)
    [card] = result.found
    assert (card['svc'], card['sev'], card['key']) == ('elastic.example.com', 'crit', 'cert-expiry|-/elastic.example.com|tls')
    assert msg('certs.expired', days=2.0) in card['what'] and msg('certs.refused') in card['what']
    assert result.ran == ('certs:elastic.example.com',), 'the expired host still runs, so its crit card is refreshed and never ages out'
    assert middle['targets'][0]['error'] == 'tls: certificate expired' and [c['sev'] for c in later.found] == ['crit'], 'an expired certificate in the chain reads as expired now'
    assert (untrusted['error'], untrusted['certExpires']) == ('tls: certificate not trusted', None)


def test_a_failed_certificate_read_logs_the_host_and_kind_only(caplog):
    # given
    net = Net()
    net.cert = lambda host, port, timeout, connect: (_ for _ in ()).throw(ConnectionResetError(54, 'reset by https://user:hunter2@elastic.example.com'))

    # when
    with caplog.at_level('WARNING', logger='skopax.probe'):
        r = net.check(ES)

    # then
    assert (r['ok'], r['status'], r['certExpires']) == (True, 200, None)
    assert [x.getMessage() for x in caplog.records] == [msg('probe.cert_unread', host='elastic.example.com', error='ConnectionResetError')]
    assert 'hunter2' not in caplog.text


def test_a_result_that_comes_after_its_deadline_is_dropped_while_the_run_goes_on(monkeypatch):
    # given
    monkeypatch.setattr(probe, 'TIMEOUT', 0.3)
    second_started, first_returned = threading.Event(), threading.Event()

    def checker(target, timeout):
        if target['name'] == 'first':
            second_started.wait(3)
            first_returned.set()
            return Net().check(target, timeout)
        second_started.set()
        first_returned.wait(3)
        time.sleep(0.05)
        return Net().check(target, timeout)

    # when
    report = Probe(lambda: [{**PROM, 'name': 'first'}, {**PROM, 'name': 'second'}], workers=1, checker=checker).run(NOW)

    # then
    rows = {r['name']: r for r in report['targets']}
    assert first_returned.is_set(), 'the first result came back while the second target was still running'
    assert (rows['first']['ok'], rows['first']['error']) == (False, 'timeout')
    assert (rows['second']['ok'], rows['second']['status']) == (True, 200)


def test_targets_come_from_serving_plugins_only(store: Store):
    # given
    ca = pem()
    add_instance(store, 'demo', 'main', sealer_key='k', url='https://main.example.org', token='pw', ca=ca)
    add_instance(store, 'demo', 'east', sealer_key='k', url='https://east.example.org', token='pw')
    runtime = demo_runtime(store)

    # when
    both = runtime.probe_targets()
    add_instance(store, 'demo', 'east', sealer_key='k', enabled=False, url='https://east.example.org', token='pw')
    runtime.refresh()
    one = runtime.probe_targets()
    runtime.state.set('demo', False, 'alice')
    runtime.refresh()
    none = runtime.probe_targets()

    # then
    assert sorted(both, key=lambda t: t['instance']) == [{'name': 'demo', 'url': 'https://example.com/health', 'plugin': 'demo', 'instance': 'east'},
                                                        {'name': 'demo', 'url': 'https://example.com/health', 'plugin': 'demo', 'instance': 'main', 'pem': ca}]
    assert [t['instance'] for t in one] == ['main']
    assert none == []


def test_a_hook_target_without_a_name_or_a_url_is_dropped(store: Store):
    # given
    add_instance(store, 'demo', 'main', sealer_key='k', url='https://main.example.org', token='pw')
    odd = dataclasses.replace(DEMO, probe_targets=lambda info: [{'name': 'ok', 'url': 'https://example.com/', 'extra': 'x'}, {'name': 3, 'url': 'https://example.com/'}, {'url': 'https://example.com/'}, 'x'])
    registry = registry_with(odd)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))
    runtime.refresh()

    # when
    found = runtime.probe_targets()

    # then
    assert found == [{'name': 'ok', 'url': 'https://example.com/', 'plugin': 'demo', 'instance': 'main'}]


def test_a_hanging_probe_targets_hook_is_bounded_and_the_others_still_report(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(runtime_module, 'PROBE_HOOK_S', 0.2)
    release = threading.Event()
    slow = minimal('slow', 'Slow', fields=(Field('url', 'URL', 'url', required=True),), probe_targets=lambda info: release.wait() and [])
    add_instance(store, 'demo', 'main', sealer_key='k', url='https://main.example.org', token='pw')
    add_instance(store, 'slow', 'main', sealer_key='k', url='https://slow.example.org')
    registry = registry_with(DEMO, slow)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))
    runtime.refresh()

    # when
    began = time.monotonic()
    report = Probe(runtime.probe_targets, checker=lambda target, timeout: Net().check(target, timeout)).run(NOW)
    took = time.monotonic() - began
    release.set()

    # then
    assert took < 1.5
    assert [(r['plugin'], r['instance'], r['ok']) for r in report['targets']] == [('demo', 'main', True)]


def test_a_probe_targets_hook_that_still_hangs_gets_no_second_thread(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(runtime_module, 'PROBE_HOOK_S', 0.2)
    release, calls = threading.Event(), []

    def hangs(info):
        calls.append(info.name)
        release.wait(3)
        return []

    slow = minimal('slow', 'Slow', fields=(Field('url', 'URL', 'url', required=True),), probe_targets=hangs)
    add_instance(store, 'demo', 'main', sealer_key='k', url='https://main.example.org', token='pw')
    add_instance(store, 'slow', 'main', sealer_key='k', url='https://slow.example.org')
    registry = registry_with(DEMO, slow)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))
    runtime.refresh()

    # when
    began = time.monotonic()
    first, second = runtime.probe_targets(), runtime.probe_targets()
    took = time.monotonic() - began
    hung = [t for t in threading.enumerate() if t.name == 'probe-targets-slow']
    release.set()
    for worker in hung:
        worker.join(2)
    third = runtime.probe_targets()

    # then
    assert len(hung) == 1 and took < 1
    assert [t['plugin'] for t in first] == [t['plugin'] for t in second] == ['demo']
    assert calls == ['main', 'main'], 'a new call starts only once the last one for that instance has finished'
    assert [t['plugin'] for t in third] == ['demo']


def test_overlapping_probe_runs_share_the_call_of_a_healthy_slow_hook(store: Store, monkeypatch):
    # given
    calls, found = [], {}

    def slow(info):
        calls.append(info.name)
        time.sleep(0.3)
        return [{'name': 'slow-main', 'url': 'https://slow.example.org/health'}]

    plugin = minimal('slow', 'Slow', fields=(Field('url', 'URL', 'url', required=True),), probe_targets=slow)
    add_instance(store, 'slow', 'main', sealer_key='k', url='https://slow.example.org')
    registry = registry_with(plugin)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))
    runtime.refresh()
    runs = [threading.Thread(target=lambda name=name: found.__setitem__(name, runtime.probe_targets())) for name in ('scheduled', 'asked')]

    # when
    runs[0].start()
    time.sleep(0.05)
    runs[1].start()
    for run in runs:
        run.join(3)

    # then
    assert {name: [t['name'] for t in targets] for name, targets in found.items()} == {'scheduled': ['slow-main'], 'asked': ['slow-main']}
    assert calls == ['main'], 'the second run waits on the call in flight; no second thread starts'


def test_cert_pass_raises_cards_by_rule():
    # given
    report = {'at': iso(NOW), 'targets': [probed('warn.example.com', 20), probed('crit.example.com', 5), probed('fine.example.com', 30), probed('gone.example.com', -2),
                                          {**probed('plain.example.com', 3), 'url': 'http://plain.example.com/', 'certExpires': None},
                                          {**probed('untrusted.example.com', 3), 'ok': False, 'error': 'tls: certificate not trusted', 'certExpires': None, 'certDays': None}]}

    # when
    [default] = cert_pass(report, NOW, CERT_RULES)
    [wider] = cert_pass(report, NOW, {**CERT_RULES, 'warn_days': 40})
    [skipped] = cert_pass(report, NOW, CERT_RULES, ('crit.example.com', 'example.org'))
    [wild] = cert_pass(report, NOW, CERT_RULES, ('*.example.com',))
    [deeper] = cert_pass(report, NOW, CERT_RULES, ('*.crit.example.com',))

    # then
    cards = {a['svc']: a for a in default.found}
    assert {h: a['sev'] for h, a in cards.items()} == {'warn.example.com': 'warn', 'crit.example.com': 'crit', 'gone.example.com': 'crit'}
    assert cards['warn.example.com']['key'] == 'cert-expiry|-/warn.example.com|tls'
    assert cards['warn.example.com']['ranKey'] == 'certs:warn.example.com'
    assert (default.name, default.ok, default.status, default.sections) == ('certs', True, 'ok', {})
    assert sorted(default.ran) == ['certs:crit.example.com', 'certs:fine.example.com', 'certs:gone.example.com', 'certs:warn.example.com']
    assert {a['svc']: a['sev'] for a in wider.found}['fine.example.com'] == 'warn'
    assert 'crit.example.com' not in {a['svc'] for a in skipped.found} and 'certs:crit.example.com' in skipped.ran, 'a skipped host still ran, so its open card clears'
    assert 'warn.example.com' in {a['svc'] for a in skipped.found}
    assert wild.found == [] and 'certs:warn.example.com' in wild.ran
    assert 'crit.example.com' in {a['svc'] for a in deeper.found}, '*.crit.example.com covers only the subdomains'
    assert msg('certs.renewal_failing', days=30) in cards['warn.example.com']['what']
    assert msg('certs.refused') in cards['gone.example.com']['what'] and msg('certs.expired', days=2.0) in cards['gone.example.com']['what']
    far = {a['svc']: a for a in wider.found}['fine.example.com']['what']
    assert 'renewal is failing' not in far and msg('certs.will_stop') in far, 'a card raised by a wider warn_days does not claim renewal is failing'


def test_an_expiry_under_a_day_ago_or_taken_as_now_says_it_has_expired():
    # given
    chain = {**probed('chain.example.com', 1 / DAY), 'ok': False, 'status': None, 'error': 'tls: certificate expired'}
    report = {'at': iso(NOW), 'targets': [probed('hour.example.com', -1 / 24), chain, probed('gone.example.com', -2)]}

    # when
    [result] = cert_pass(report, NOW, CERT_RULES)
    [zero] = cert_pass(report, NOW, {'warn_days': 0, 'crit_days': 0})

    # then
    cards = {a['svc']: a for a in result.found}
    assert {h: a['sev'] for h, a in cards.items()} == {'hour.example.com': 'crit', 'chain.example.com': 'crit', 'gone.example.com': 'crit'}
    assert {a['svc']: a['sev'] for a in zero.found} == {'hour.example.com': 'crit', 'chain.example.com': 'crit', 'gone.example.com': 'crit'}, 'an expired chain is crit and kept even with zero-day rules'
    for host in ('hour.example.com', 'chain.example.com'):
        assert cards[host]['what'].startswith(msg('certs.what', host=host, when=msg('certs.has_expired'), at='', tail='').split(' (')[0] + ' ('), cards[host]['what']
        assert msg('certs.refused') in cards[host]['what'] and 'days ago' not in cards[host]['what'] and 'expires in' not in cards[host]['what']
        assert cards[host]['short'] == msg('certs.short', sev='CRIT', host=host, when=msg('certs.has_expired'))
    assert msg('certs.expired', days=2.0) in cards['gone.example.com']['what'], 'a day or more keeps its day count'


def test_a_skip_entry_with_a_trailing_dot_is_accepted_and_skips_its_host():
    # given
    report = {'at': iso(NOW), 'targets': [probed('shop.example.com', 5), probed('api.example.org', 5)]}

    # when
    entries = {e: probe.skip_entry_ok(e) for e in ('shop.example.com.', '*.example.org.', 'shop.example.com', '.', 'a..example.com', 'shop example.com.')}
    [result] = cert_pass(report, NOW, CERT_RULES, ('shop.example.com.', '*.example.org.'))

    # then
    assert entries == {'shop.example.com.': True, '*.example.org.': True, 'shop.example.com': True, '.': False, 'a..example.com': False, 'shop example.com.': False}
    assert result.found == [], 'an accepted entry skips the host the same way the pass reads it'


def test_a_cert_pass_that_sees_no_host_clears_no_open_cert_card(store: Store):
    # given
    later, last = NOW + 900, NOW + 1800
    apply(store, cert_pass({'at': iso(NOW), 'targets': [probed('warn.example.com', 20), probed('crit.example.com', 5)]}, NOW, CERT_RULES), NOW)
    opened = sorted((a['svc'], a['status']) for a in store.load()['anomalies'])

    # when
    [blind] = cert_pass({'at': iso(later), 'targets': [{**probed('plain.example.com', 3, later), 'url': 'http://plain.example.com/', 'certExpires': None}]}, later, CERT_RULES)
    apply(store, [blind], later)
    after = sorted((a['svc'], a['status']) for a in store.load()['anomalies'])
    [renewed] = cert_pass({'at': iso(last), 'targets': [probed('warn.example.com', 90, last)]}, last, CERT_RULES)
    apply(store, [renewed], last)
    final = sorted((a['svc'], a['status']) for a in store.load()['anomalies'])

    # then
    assert opened == [('crit.example.com', 'open'), ('warn.example.com', 'open')]
    assert blind.ran == () and reach_of([blind]) == {'certs': True}, 'a pass that saw no host falls back to its bare name'
    assert after == opened, 'the bare name matches no host card, so none is cleared'
    assert final == [('crit.example.com', 'open'), ('warn.example.com', 'cleared')], 'a pass that sees the host again does clear its card'


def test_the_cert_rules_setting_reaches_the_probe_pass(store: Store):
    # given
    fake = FakeProbe({'at': iso(NOW), 'targets': [probed('fine.example.com', 30, time.time())]})
    on = dataclasses.replace(settings(), probe=True)

    # when
    with TestClient(create_app(on, store, probe=fake)) as c:
        wait_for(lambda: fake.runs)
        before = c.post('/api/probe', headers=AUTH)
        quiet = [a for a in store.load().get('anomalies') or [] if a.get('src') == 'certs']
        c.put('/api/admin/settings', json={'key': 'core.certs', 'value': '{"warn_days": 40}'}, headers=AUTH)
        c.post('/api/probe', headers=AUTH)
        cards = [a for a in store.load().get('anomalies') or [] if a.get('src') == 'certs']

        c.put('/api/admin/settings', json={'key': 'core.cert-skip', 'value': 'example.com'}, headers=AUTH)
        c.post('/api/probe', headers=AUTH)
        open_after_skip = [a for a in store.load().get('anomalies') or [] if a.get('src') == 'certs' and a.get('status') == 'open']
        listed = {i['key']: i for i in c.get('/api/admin/settings', headers=AUTH).json()['items']}
        refused = {v: c.put('/api/admin/settings', json={'key': 'core.cert-skip', 'value': v}, headers=AUTH) for v in ('https://shop.example.com/', 'shop.example.com:443', 'a b')}
        wildcard = c.put('/api/admin/settings', json={'key': 'core.cert-skip', 'value': '*.example.com, shop.example.org'}, headers=AUTH)

    # then
    assert before.status_code == 200 and quiet == []
    assert {v: (r.status_code, r.json()['detail']) for v, r in refused.items()} == {v: (422, msg('certs.skip_invalid', entry=v)) for v in refused}
    assert wildcard.status_code == 200 and wildcard.json()['item']['value'] == '*.example.com, shop.example.org'
    assert [(a['svc'], a['sev']) for a in cards] == [('fine.example.com', 'warn')]
    assert open_after_skip == [], 'skipping a host clears its open card on the next pass'
    assert 'certs' not in store.load(), 'the pass publishes no host list in the state'
    assert (listed['core.certs']['kind'], listed['core.certs']['defaults'], listed['core.certs']['rules']) == ('rules', CERT_RULES, {'warn_days': 40, 'crit_days': 7})
    assert set(listed['core.certs']['ruleHelp']) == set(CERT_RULES)
    assert (listed['core.cert-skip']['kind'], listed['core.cert-skip']['value']) == ('list', 'example.com')


def test_post_probe_needs_the_token_and_stores_the_report_for_api_server(store: Store):
    # given
    fake = FakeProbe()
    on = dataclasses.replace(settings(), probe=True)

    # when
    with TestClient(create_app(on, store, probe=fake)) as c:
        wait_for(lambda: fake.runs)
        denied = c.post('/api/probe')
        ran = c.post('/api/probe', headers=AUTH)
        shown = c.get('/api/server').json()
        full = c.get('/api/server', headers=AUTH).json()
        made = c.post('/api/admin/tokens', json={'name': 'ci', 'role': 'operator'}, headers=AUTH).json()
        by_operator = c.post('/api/probe', headers={'Authorization': f"Bearer {made['token']}"})

    # then
    assert denied.status_code == 401
    assert ran.status_code == 200 and ran.json() == REPORT and shown['probe'] is True
    assert (by_operator.status_code, by_operator.json()) == (200, public(REPORT)), 'an operator token runs the probe but sees no URLs or errors'
    assert full['reach'] == REPORT
    assert shown['reach'] == {'at': REPORT['at'], 'targets': [{'name': 'demo', 'plugin': 'demo', 'instance': 'main', 'ok': False, 'status': None, 'ms': 12, 'certDays': None}]}
    assert store.load().get('reach') is None


def test_the_reach_shows_urls_and_errors_to_admins_only(store: Store):
    # given
    store.save_probe(REPORT)
    cookies = people(store)
    Users(store.db).create(ADMIN, 'Dan', 'admin')
    cookies['admin'] = Sessions(session_secret(store.db, sso_settings()), 3600, 7200).issue(ADMIN, 'admin', 'local')
    seen = {}

    # when
    fake = FakeProbe()
    with TestClient(create_app(dataclasses.replace(sso_settings(), public_read=True, probe=True), store, probe=fake), base_url='https://testserver') as c:
        wait_for(lambda: fake.runs)
        for who in ('anonymous', 'viewer', 'operator', 'admin'):
            c.cookies.clear()
            if who in cookies:
                c.cookies.set(SESSION_COOKIE, cookies[who])
            seen[who] = c.get('/api/server').json()['reach']
        c.cookies.clear()
        seen['token'] = c.get('/api/server', headers=AUTH).json()['reach']
        seen['bad token'] = c.get('/api/server', headers={'Authorization': 'Bearer nope'}).json()['reach']

    # then
    for who in ('anonymous', 'viewer', 'operator', 'bad token'):
        assert seen[who] == public(REPORT), who
        assert 'demo.example.com' not in str(seen[who]) and 'gaierror' not in str(seen[who]), who
    assert seen['admin'] == REPORT and seen['token'] == REPORT


def test_the_probe_report_survives_a_state_write(store: Store):
    # given
    store.save_probe(REPORT)

    # when
    store.save_state({'anomalies': [], 'sweptIso': '2026-10-19T19:05:00Z'}, None)

    # then
    assert store.last_probe() == REPORT
    assert 'targets' not in store.load()


def test_with_the_probe_on_it_runs_at_startup_and_every_quarter_hour(store: Store):
    # given
    fake = FakeProbe()
    on = dataclasses.replace(settings(), probe=True)

    # when
    with TestClient(create_app(on, store, probe=fake)) as c:
        wait_for(lambda: store.last_probe() is not None)
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        info = c.get('/api/server').json()

    # then
    assert store.last_probe() == REPORT and len(fake.runs) >= 1
    assert info['probe'] is True
    assert (jobs['probe'].period, jobs['probe'].offset) == (900, 240)


def test_with_the_probe_off_nothing_runs_on_its_own(store: Store):
    # given
    fake = FakeProbe()

    # when
    with TestClient(create_app(settings(), store, probe=fake)) as c:
        names = [j.name for j in c.app.state.scheduler.jobs]

    # then
    assert 'probe' not in names
    assert fake.runs == []
    assert store.last_probe() is None


def test_the_probe_switch_is_live(store: Store):
    # given
    fake = FakeProbe()
    on = dataclasses.replace(settings(), probe=True)

    # when
    with TestClient(create_app(on, store, probe=fake)) as c:
        scheduler = c.app.state.scheduler
        wait_for(lambda: fake.runs)
        started = len(fake.runs)
        scheduler.nudge('probe')
        scheduler.run_due()
        ran_on = len(fake.runs)
        switched = c.put('/api/admin/settings', json={'key': 'core.probe', 'value': 'false'}, headers=AUTH)
        names = [j.name for j in scheduler.jobs]
        scheduler.nudge('probe')
        scheduler.run_due()
        ran_off = len(fake.runs)

    # then
    assert switched.status_code == 200 and switched.json()['item']['group'] == 'Features'
    assert ran_on == started + 1
    assert 'probe' not in names and ran_off == ran_on


def test_the_probe_settings_have_their_defaults_and_env_names():
    # given
    from skopax.config import load_settings
    from skopax.rulehelp import RULE_HELP
    from skopax.tunables import RULES, STATIC

    # when
    default = load_settings(env={})
    given = load_settings(env={'SKOPAX_PROBE': 'false', 'SKOPAX_CERT_RULES': '{"warn_days": 30}', 'SKOPAX_CERT_SKIP': 'a.example.com, b.example.com'})
    rows = {t.key: t for t in STATIC}

    # then
    assert (default.probe, default.cert_rules, default.cert_skip) == (True, '', ())
    assert (given.probe, given.cert_rules, given.cert_skip) == (False, '{"warn_days": 30}', ('a.example.com', 'b.example.com'))
    assert (rows['core.probe'].group, rows['core.probe'].kind, rows['core.probe'].live) == ('Features', 'bool', True)
    assert (rows['core.certs'].group, rows['core.certs'].field, rows['core.certs'].defaults) == (RULES, 'cert_rules', CERT_RULES)
    assert set(RULE_HELP['core.certs']) == set(CERT_RULES)
    assert (rows['core.cert-skip'].group, rows['core.cert-skip'].kind) == (RULES, 'list')


def test_tool_status_says_unreachable_from_the_probe(store: Store):
    # given
    add_instance(store, 'demo', 'main', sealer_key='k', primary=True, url='https://main.example.org', token='pw')
    add_instance(store, 'demo', 'east', sealer_key='k', url='https://east.example.org', token='pw')
    tools = ToolSet(demo_runtime(store))
    down = {'at': iso(NOW), 'targets': [{**probed('example.com', 30), 'name': 'demo', 'instance': 'main', 'ok': False, 'error': 'dns: gaierror'},
                                        {**probed('example.com', 30), 'name': 'demo', 'instance': 'east', 'ok': False, 'error': 'tcp: TimeoutError'},
                                        {**PROM, 'ok': False, 'error': 'dns: gaierror'}]}
    half = {'at': iso(NOW), 'targets': [down['targets'][0], {**down['targets'][1], 'ok': True, 'error': None}]}

    # when
    cut = tools.status('demo_echo', down)
    fine = tools.status('demo_echo', half)
    core = tools.status('card', down)
    old = tools.status('demo_echo', {'at': iso(NOW), 'results': [{'source': 'demo', 'stage': 'dns'}]})

    # then
    assert cut == {'state': 'unreachable', 'reason': msg('tools.unreachable', targets='demo'), 'at': iso(NOW)}
    assert 'example' not in cut['reason'] and 'gaierror' not in cut['reason']
    assert fine == {'state': 'on', 'reason': None}
    assert core == {'state': 'on', 'reason': None}
    assert old == {'state': 'on', 'reason': None}


def test_tool_status_reads_the_stored_report_and_calls_no_plugin_hook(store: Store):
    # given
    add_instance(store, 'demo', 'main', sealer_key='k', primary=True, url='https://main.example.org', token='pw')
    calls = []
    hooked = dataclasses.replace(DEMO, probe_targets=lambda info: calls.append(info.name) or time.sleep(2) or [])
    registry = registry_with(hooked)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))
    runtime.refresh()
    tools = ToolSet(runtime)
    report = {'at': iso(NOW), 'targets': [{**probed('example.com', 30), 'name': 'demo', 'ok': False, 'error': 'dns: gaierror'}]}
    usage = {'tools': [{'name': 'demo_echo', 'kind': 'read'}, {'name': 'demo_write', 'kind': 'write'}, {'name': 'card', 'kind': 'read'}, {'name': 'note', 'kind': 'write'},
                       {'name': 'old_tool', 'kind': 'gone'}]}

    # when
    began = time.monotonic()
    rows = {r['name']: r for r in toolusage.with_status(usage, tools, report)['tools']}
    took = time.monotonic() - began

    # then
    assert calls == [] and took < 1.0
    assert rows['demo_echo']['status']['state'] == 'unreachable' and rows['demo_write']['status']['state'] == 'unreachable'
    assert rows['card']['status'] == {'state': 'on', 'reason': None}
    assert 'status' not in rows['note'] and 'status' not in rows['old_tool']


def test_a_core_tool_that_needs_a_plugin_reads_that_plugins_targets(store: Store, monkeypatch):
    # given
    monkeypatch.setitem(toolset.CORE, 'propose_ticket', dataclasses.replace(toolset.CORE['propose_ticket'], needs='demo'))
    add_instance(store, 'demo', 'main', sealer_key='k', primary=True, url='https://main.example.org', token='pw')
    tools = ToolSet(demo_runtime(store))
    down = {'at': iso(NOW), 'targets': [{**probed('example.com', 30), 'name': 'demo', 'ok': False, 'error': 'dns: gaierror'}]}
    usage = {'tools': [{'name': 'propose_ticket', 'kind': 'propose'}, {'name': 'propose_card_action', 'kind': 'propose'}]}

    # when
    cut = tools.status('propose_ticket', down)
    rows = {r['name']: r for r in toolusage.with_status(usage, tools, down)['tools']}

    # then
    assert cut == {'state': 'unreachable', 'reason': msg('tools.unreachable', targets='demo'), 'at': iso(NOW)}
    assert rows['propose_ticket']['status'] == cut
    assert 'status' not in rows['propose_card_action']


def test_with_the_probe_off_the_stored_report_is_not_shown_as_current(store: Store):
    # given
    s = settings()
    add_instance(store, DEMO, 'main', sealer_key=master_key(store.db, s), primary=True, url='https://main.example.org', token='pw')
    store.save_probe({'at': iso(NOW), 'targets': [{**probed('example.com', 30), 'name': 'demo', 'ok': False, 'error': 'dns: gaierror'}]})

    # when
    with demo_app(store, s) as c:
        shown = c.get('/api/server').json()
        full = c.get('/api/server', headers=AUTH).json()
        ran = c.post('/api/probe', headers=AUTH)
        usage = c.get('/api/tool-usage').json()
    facts = selfreview.gather(store, {'probe': False}, None, NOW)

    # then
    assert (shown['probe'], shown['reach'], full['reach']) == (False, None, None)
    assert (ran.status_code, ran.json()['detail']) == (409, msg('probe.off'))
    assert usage['probe'] is False
    assert {t['name']: t for t in usage['tools']}['demo_echo']['status'] == {'state': 'on', 'reason': None}
    assert facts['reachErrors'] == []


def test_an_explicit_reach_matches_target_names(store: Store):
    # given
    add_instance(store, 'demo', 'main', sealer_key='k', primary=True, url='https://main.example.org', token='pw')
    echo = dataclasses.replace(DEMO.tools[0], reach=('prometheus-main',))
    plugin = dataclasses.replace(DEMO, tools=(echo,) + DEMO.tools[1:])
    registry = registry_with(plugin)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))
    runtime.refresh()
    report = {'at': iso(NOW), 'targets': [{**PROM, 'ok': False, 'error': 'dns: gaierror'}, {**probed('example.com', 30), 'name': 'demo', 'ok': True}]}

    # when
    found = ToolSet(runtime).status('demo_echo', report)

    # then
    assert found == {'state': 'unreachable', 'reason': msg('tools.unreachable', targets='prometheus-main'), 'at': iso(NOW)}


def test_run_now_nudges_the_job_and_needs_an_operator(store: Store, monkeypatch):
    # given
    s = dataclasses.replace(sso_settings(), public_read=True)
    add_instance(store, DEMO, 'main', sealer_key=master_key(store.db, s), primary=True, url='https://main.example.org', token='pw')
    cookies = people(store)
    nudged = []

    # when
    with demo_app(store, s) as c:
        monkeypatch.setattr(c.app.state.scheduler, 'nudge', nudged.append)
        ok = c.post('/api/checks/demo/pulse/run', headers=AUTH)
        asked = list(nudged)
        nope = c.post('/api/checks/demo/nope/run', headers=AUTH)
        ghost = c.post('/api/checks/ghost/pulse/run', headers=AUTH)
        c.cookies.set(SESSION_COOKIE, cookies['viewer'])
        viewer = c.post('/api/checks/demo/pulse/run')
        c.cookies.set(SESSION_COOKIE, cookies['operator'])
        operator = c.post('/api/checks/demo/pulse/run')
        c.cookies.clear()
        c.patch('/api/admin/plugins/demo/instances/main', json={'offChecks': ['pulse']}, headers=AUTH)
        c.app.state.runtime.refresh()
        unchecked = c.post('/api/checks/demo/pulse/run', headers=AUTH)
        c.patch('/api/admin/plugins/demo/instances/main', json={'offChecks': []}, headers=AUTH)
        c.put('/api/admin/plugins/demo', json={'enabled': False}, headers=AUTH)
        c.app.state.runtime.refresh()
        off = c.post('/api/checks/demo/pulse/run', headers=AUTH)
        lines = [a['text'] for a in store.list_activity(None, 50) if a.get('kind') == 'check']

    # then
    assert (ok.status_code, ok.json()) == (202, {'job': 'demo.pulse'}) and asked == ['demo.pulse']
    assert (nope.status_code, nope.json()['detail']) == (404, msg('checks.unknown'))
    assert (ghost.status_code, ghost.json()['detail']) == (404, msg('checks.unknown'))
    assert viewer.status_code == 403 and operator.status_code == 202
    assert (unchecked.status_code, unchecked.json()['detail']) == (409, msg('checks.unchecked', check='Demo pulse', name='Demo'))
    assert (off.status_code, off.json()['detail']) == (409, msg('checks.not_serving', name='Demo'))
    assert sorted(lines) == sorted([msg('checks.run_now', check='Demo pulse', by=BOOTSTRAP_TOKEN), msg('checks.run_now', check='Demo pulse', by=OPERATOR)])


def test_a_probe_without_targets_logs_no_empty_probe_line(store: Store, caplog):
    # given
    caplog.set_level(logging.INFO, logger='skopax')
    empty = FakeProbe({'at': iso(NOW), 'targets': []})
    full = FakeProbe({'at': iso(NOW), 'targets': [{**PROM, 'ok': True, 'status': 200, 'error': None}]})
    on = dataclasses.replace(settings(), probe=True)

    # when
    with TestClient(create_app(on, store, probe=empty)) as c:
        wait_for(lambda: empty.runs)
        quiet = c.post('/api/probe', headers=AUTH)
    quiet_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('probe:')]
    caplog.clear()
    with TestClient(create_app(on, store, probe=full)) as c:
        wait_for(lambda: full.runs)
        c.post('/api/probe', headers=AUTH)
    full_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('probe:')]

    # then
    assert quiet.status_code == 200
    assert quiet_lines == []
    assert msg('probe.ran', summary='prometheus-main (main) ok 200') in full_lines


def test_summary_names_targets_and_codes_only():
    # given
    report = {'at': iso(NOW), 'targets': [{**PROM, 'ok': True, 'status': 200, 'error': None}, {**ES, 'ok': False, 'status': None, 'error': 'tls: certificate not trusted'}]}

    # when
    line = summary(report)

    # then
    assert line == 'prometheus-main (main) ok 200; elastic-main (main) tls: certificate not trusted'
    assert 'example.com' not in line


def test_the_self_review_reads_failed_targets_from_the_new_report(store: Store):
    # given
    store.save_probe({'at': iso(NOW), 'targets': [{**PROM, 'ok': True, 'status': 200, 'error': None}, {**ES, 'ok': False, 'status': None, 'error': 'dns: gaierror'}]})

    # when
    facts = selfreview.gather(store, {}, None, NOW)

    # then
    assert facts['reachErrors'] == [{'target': 'elastic-main', 'plugin': 'elastic', 'instance': 'main', 'error': 'dns: gaierror'}]


def test_a_pinned_target_trusts_its_certificate_as_it_is_and_says_so_in_its_target(store: Store):
    # given
    ca = pem()
    add_instance(store, 'demo', 'main', sealer_key='k', url='https://main.example.org', token='pw', ca=ca, **{'ca-pinned': 'true'})
    net = Net(answers=[200])
    pinning = dataclasses.replace(DEMO, fields=DEMO.fields + (Field('ca-pinned', 'Trust this certificate as it is', 'bool', default='false', show_if=('ca', '*')),))
    registry = registry_with(pinning)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')))
    runtime.refresh()

    # when
    [target] = runtime.probe_targets()
    net.check(target)

    # then
    assert (target['pem'], target['pinned']) == (ca, True)
    context = net.fetches[0][1]
    assert isinstance(context, remote.PinnedContext) and context.pins == {ssl.PEM_cert_to_DER_cert(ca)}, 'only the pasted certificate itself is accepted'
