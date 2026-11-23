import dataclasses
import json
import logging
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit

import mongomock
import pytest

from skopax import bounded, remote
from skopax.config import settings_env_names
from skopax.contract import Denied, Hooks, SourceData, SourceDown
from skopax.instances import Instances
from skopax.plugins import slack, vault
from skopax.plugins.vault import BadAnswer, BadSetup, TooMuch, Vault
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply
from skopax.sealer import Sealer
from skopax.settingsources import SourceSet, boot

ADDR = 'https://vault.example.com:8200'
TOKEN = 'test-vault-token-1'
JWT = 'test-k8s-jwt-value-1'
ROLE_ID = 'test-role-id-1'
SECRET_ID = 'test-secret-id-value-1'
BOT = 'test-slack-bot-value-1'
BASE = {'VAULT_ADDR': ADDR, 'VAULT_TOKEN': TOKEN}
PEM = '-----BEGIN CERTIFICATE-----\nMIIBfakefakefake\n-----END CERTIFICATE-----\n'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeVault:
    def __init__(self, settings=None, instances=None, lease=3600):
        self.settings = {'slack.post-sev': 'high', 'core.timezone': 'Europe/Berlin'} if settings is None else settings
        self.instances = {('slack', 'main'): {'bot-token': BOT}} if instances is None else instances
        self.calls: list[tuple] = []
        self.valid = {TOKEN}
        self.lease = lease
        self.issued = 0
        self.fail: BaseException | None = None
        self.login_fail: BaseException | None = None
        self.denied: set[str] = set()
        self.extra: dict[str, list[str]] = {}

    def __call__(self, method, url, headers, data, timeout):
        parts = urlsplit(url)
        path = parts.path
        self.calls.append((method, path, parse_qs(parts.query), dict(headers), json.loads(data) if data else None, timeout))
        if self.fail is not None:
            raise self.fail
        if path.startswith('/v1/auth/') and path.endswith('/login'):
            if self.login_fail is not None:
                raise self.login_fail
            self.issued += 1
            token = f'test-vault-issued-{self.issued}'
            self.valid.add(token)
            return Reply({'auth': {'client_token': token, 'lease_duration': self.lease, 'renewable': True}}, {})
        if headers.get('X-Vault-Token') not in self.valid or path in self.denied:
            raise HttpError(403, url, '{"errors":["permission denied"]}')
        if path == '/v1/secret/data/skopax/settings':
            if self.settings is None:
                raise HttpError(404, url)
            return Reply({'data': {'data': self.settings, 'metadata': {'version': 1}}}, {})
        if path == '/v1/secret/metadata/skopax/instances':
            folders = sorted({f'{p}/' for p, _ in self.instances} | set(self.extra.get('', [])))
            if not folders:
                raise HttpError(404, url)
            return Reply({'data': {'keys': folders}}, {})
        if path.startswith('/v1/secret/metadata/skopax/instances/'):
            plugin = path.rsplit('/', 1)[1]
            return Reply({'data': {'keys': sorted(n for p, n in self.instances if p == plugin) + self.extra.get(plugin, [])}}, {})
        if path.startswith('/v1/secret/data/skopax/instances/'):
            plugin, name = path.split('/')[-2:]
            if (plugin, name) not in self.instances:
                raise HttpError(404, url)
            return Reply({'data': {'data': self.instances[(plugin, name)], 'metadata': {'version': 3}}}, {})
        raise HttpError(404, url)

    def paths(self) -> list[str]:
        return [c[1] for c in self.calls]


def source(server=None, clock=None, made=None, direct=None) -> Vault:
    server = server if server is not None else FakeVault()

    def transport_for(pem: str, unproxied: bool = False):
        if made is not None:
            made.append(pem)
        if direct is not None:
            direct.append(unproxied)
        return server

    return Vault(transport_for, clock if clock is not None else Clock())


def read(env=None, server=None) -> dict:
    return source(server)({**BASE, **(env or {})})


def test_the_plugin_is_built_in_listed_and_offers_only_the_settings_source_hook():
    # when
    found = Registry.load({}).get('vault')

    # then
    assert 'skopax.plugins.vault' in BUILTIN
    assert found is not None and found.state == 'ok' and found.plugin is vault.PLUGIN
    assert found.plugin.name == 'Vault' and found.plugin.edition == 'community'
    assert describe(vault.PLUGIN, icon=True)['hooks'] == ['settings_source']
    assert vault.PLUGIN.fields == () and vault.PLUGIN.checks == () and vault.PLUGIN.settings == ()


def test_without_an_address_the_hook_is_inert_and_asks_nothing():
    # given
    server = FakeVault()

    # when
    out = source(server)({'VAULT_TOKEN': TOKEN})

    # then
    assert out is None and server.calls == []


def test_the_setup_comes_from_the_environment_only_and_never_becomes_a_setting():
    # given
    names = {'VAULT_ADDR', 'VAULT_NAMESPACE', 'VAULT_CACERT', 'VAULT_AUTH', 'VAULT_TOKEN', 'VAULT_TOKEN_FILE', 'VAULT_K8S_ROLE', 'VAULT_K8S_TOKEN_FILE', 'VAULT_K8S_MOUNT',
             'VAULT_ROLE_ID', 'VAULT_SECRET_ID', 'VAULT_SECRET_ID_FILE', 'VAULT_KV_MOUNT', 'VAULT_SKOPAX_PATH'}

    # when
    out = read()

    # then
    assert not names & settings_env_names()
    assert not any(k.startswith('vault.') for k in out['values'])
    assert vault.PLUGIN.settings == () and vault.PLUGIN.env_instances is None


def test_token_auth_reads_settings_and_instances_with_the_token_and_namespace_headers():
    # given
    server = FakeVault()

    # when
    out = read({'VAULT_NAMESPACE': 'team-a'}, server)

    # then
    assert server.paths() == ['/v1/secret/data/skopax/settings', '/v1/secret/metadata/skopax/instances', '/v1/secret/metadata/skopax/instances/slack',
                              '/v1/secret/data/skopax/instances/slack/main']
    assert all(c[0] == 'GET' and c[3]['X-Vault-Token'] == TOKEN and c[3]['X-Vault-Namespace'] == 'team-a' for c in server.calls)
    assert server.calls[1][2] == {'list': ['true']} and server.calls[0][2] == {}
    assert out['values'] == {'slack.post-sev': 'high', 'core.timezone': 'Europe/Berlin'}
    assert out['instances'] == [{'bot-token': BOT, 'plugin': 'slack', 'name': 'main'}]
    assert out['label'] == 'Vault (vault.example.com:8200)'


def test_the_token_can_come_from_a_file_and_the_mount_and_path_are_set_by_the_environment(tmp_path):
    # given
    held = tmp_path / 'token'
    held.write_text(TOKEN + '\n', encoding='utf-8')
    server = FakeVault()

    # when
    with pytest.raises(vault.EmptyAnswer):
        source(server)({'VAULT_ADDR': ADDR, 'VAULT_TOKEN_FILE': str(held), 'VAULT_KV_MOUNT': 'kv', 'VAULT_SKOPAX_PATH': 'apps/skopax'})

    # then
    assert server.paths() == ['/v1/kv/data/apps/skopax/settings', '/v1/kv/metadata/apps/skopax/instances'] and server.calls[0][3]['X-Vault-Token'] == TOKEN


def test_kubernetes_login_posts_the_role_and_the_service_account_jwt_and_uses_the_token_it_gets(tmp_path):
    # given
    jwt = tmp_path / 'jwt'
    jwt.write_text(JWT, encoding='utf-8')
    server = FakeVault()
    env = {'VAULT_ADDR': ADDR, 'VAULT_AUTH': 'kubernetes', 'VAULT_K8S_ROLE': 'skopax', 'VAULT_K8S_TOKEN_FILE': str(jwt)}

    # when
    out = source(server)(env)
    other = FakeVault()
    source(other)({**env, 'VAULT_K8S_MOUNT': 'k8s-east'})

    # then
    method, path, _, _, body, _ = server.calls[0]
    assert (method, path, body) == ('POST', '/v1/auth/kubernetes/login', {'role': 'skopax', 'jwt': JWT})
    assert all(c[3]['X-Vault-Token'] == 'test-vault-issued-1' for c in server.calls[1:])
    assert out['values']['slack.post-sev'] == 'high'
    assert other.calls[0][1] == '/v1/auth/k8s-east/login'
    assert vault.K8S_JWT == '/var/run/secrets/kubernetes.io/serviceaccount/token'


def test_approle_login_posts_the_role_id_and_the_secret_id_from_a_file(tmp_path):
    # given
    held = tmp_path / 'secret-id'
    held.write_text(SECRET_ID + '\n', encoding='utf-8')
    server, other = FakeVault(), FakeVault()

    # when
    source(server)({'VAULT_ADDR': ADDR, 'VAULT_AUTH': 'approle', 'VAULT_ROLE_ID': ROLE_ID, 'VAULT_SECRET_ID_FILE': str(held)})
    source(other)({'VAULT_ADDR': ADDR, 'VAULT_AUTH': 'approle', 'VAULT_ROLE_ID': ROLE_ID, 'VAULT_SECRET_ID': SECRET_ID})

    # then
    assert (server.calls[0][1], server.calls[0][4]) == ('/v1/auth/approle/login', {'role_id': ROLE_ID, 'secret_id': SECRET_ID})
    assert other.calls[0][4] == {'role_id': ROLE_ID, 'secret_id': SECRET_ID}


def test_the_token_is_kept_until_its_ttl_less_a_margin_and_then_a_new_login_is_made(tmp_path):
    # given
    jwt = tmp_path / 'jwt'
    jwt.write_text(JWT, encoding='utf-8')
    server, clock = FakeVault(lease=600), Clock()
    feed = source(server, clock)
    env = {'VAULT_ADDR': ADDR, 'VAULT_AUTH': 'kubernetes', 'VAULT_K8S_ROLE': 'skopax', 'VAULT_K8S_TOKEN_FILE': str(jwt)}

    # when
    feed(env)
    clock.now += 600 - vault.TTL_MARGIN_S - 1
    feed(env)
    first = server.issued
    clock.now += 2
    feed(env)

    # then
    assert first == 1 and server.issued == 2
    assert server.calls[-1][3]['X-Vault-Token'] == 'test-vault-issued-2'


def test_a_403_drops_the_token_logs_in_again_once_and_a_second_403_is_denied(tmp_path):
    # given
    jwt = tmp_path / 'jwt'
    jwt.write_text(JWT, encoding='utf-8')
    server = FakeVault()
    feed = source(server)
    env = {'VAULT_ADDR': ADDR, 'VAULT_AUTH': 'kubernetes', 'VAULT_K8S_ROLE': 'skopax', 'VAULT_K8S_TOKEN_FILE': str(jwt)}
    feed(env)
    server.valid.discard('test-vault-issued-1')

    # when
    again = feed(env)
    server.denied.add('/v1/secret/data/skopax/settings')
    with pytest.raises(Denied) as raised:
        feed(env)

    # then
    assert server.issued == 3 and again['values']['slack.post-sev'] == 'high'
    assert isinstance(raised.value, SourceDown) and raised.value.permission == 'read'
    assert JWT not in str(raised.value) and 'test-vault-issued' not in str(raised.value)


def test_a_rotated_token_file_is_read_again_after_a_403(tmp_path):
    # given
    held = tmp_path / 'token'
    held.write_text(TOKEN, encoding='utf-8')
    server = FakeVault()
    feed = source(server)
    env = {'VAULT_ADDR': ADDR, 'VAULT_TOKEN_FILE': str(held)}
    feed(env)
    server.valid = {'test-vault-token-2'}
    held.write_text('test-vault-token-2', encoding='utf-8')

    # when
    out = feed(env)

    # then
    assert out['values'] and server.calls[-1][3]['X-Vault-Token'] == 'test-vault-token-2'


@pytest.mark.parametrize('failure', [HttpError(400, 'x', '{"errors":["invalid role name"]}'), HttpError(403, 'x'), HttpError(401, 'x')])
def test_a_refused_login_is_denied(tmp_path, failure):
    # given
    held = tmp_path / 'secret-id'
    held.write_text(SECRET_ID, encoding='utf-8')
    server = FakeVault()
    server.login_fail = failure

    # when
    with pytest.raises(Denied) as raised:
        source(server)({'VAULT_ADDR': ADDR, 'VAULT_AUTH': 'approle', 'VAULT_ROLE_ID': ROLE_ID, 'VAULT_SECRET_ID_FILE': str(held)})

    # then
    assert raised.value.permission == 'login' and SECRET_ID not in str(raised.value)


def test_values_become_text_and_a_missing_settings_secret_or_instance_folder_is_empty():
    # given
    server = FakeVault(settings={'investigate.max-cards': 9, 'slack.listen': True, 'gone.key': None}, instances={})
    bare = FakeVault()
    bare.settings = None

    # when
    out = read(server=server)
    empty = read(server=bare)

    # then
    assert out == {'label': 'Vault (vault.example.com:8200)', 'values': {'investigate.max-cards': '9', 'slack.listen': 'true'}, 'instances': []}
    assert empty['values'] == {} and len(empty['instances']) == 1


def test_a_path_with_neither_settings_nor_instances_is_a_failure_not_an_empty_source(caplog):
    # given
    server = FakeVault(instances={})
    server.settings = None

    # when
    with caplog.at_level(logging.WARNING):
        with pytest.raises(vault.EmptyAnswer) as raised:
            read({'VAULT_KV_MOUNT': 'kv'}, server)

    # then
    assert isinstance(raised.value, SourceDown) and 'VAULT_KV_MOUNT' in str(raised.value) and 'VAULT_SKOPAX_PATH' in str(raised.value)
    assert 'VAULT_KV_MOUNT' in caplog.text


def test_an_instance_takes_its_plugin_and_name_from_the_path_and_stray_keys_are_skipped(caplog):
    # given
    server = FakeVault(instances={('slack', 'main'): {'bot-token': BOT, 'plugin': 'jira', 'name': 'other', 'primary': True}})
    server.extra = {'': ['loose'], 'slack': ['deeper/']}

    # when
    with caplog.at_level(logging.WARNING):
        out = read(server=server)

    # then
    assert out['instances'] == [{'bot-token': BOT, 'primary': 'true', 'plugin': 'slack', 'name': 'main'}]
    assert 'loose' in caplog.text and 'deeper' in caplog.text and BOT not in caplog.text


def test_a_nested_value_is_refused_as_a_bad_answer_that_names_the_path():
    # given
    server = FakeVault(instances={('slack', 'main'): {'bot-token': {'nested': BOT}}})

    # when
    with pytest.raises(BadAnswer) as raised:
        read(server=server)

    # then
    assert isinstance(raised.value, SourceDown) and 'instances/slack/main' in str(raised.value) and BOT not in str(raised.value)


def test_secret_fields_from_vault_are_owned_by_the_source_and_never_plain_in_the_listing():
    # given
    data = SourceData.of(read(), vault.NAME)
    registry = Registry.of(slack.PLUGIN)
    instances = Instances(mongomock.MongoClient(tz_aware=True)['vault'], registry, lambda: Sealer('vault-test-key'), sources=lambda: [data])

    # when
    instances.refresh()
    found = instances.get('slack', 'main')
    shown = found.view()

    # then
    assert found.source == 'Vault (vault.example.com:8200)' and found.secrets['bot-token'] == BOT and 'bot-token' in found.owned
    assert shown['secrets']['bot-token'] == {'set': True, 'from': 'Vault (vault.example.com:8200)'} and BOT not in json.dumps(shown)


@pytest.mark.parametrize('settings, instances, where', [
    ({f'core.key-{i}': 'x' for i in range(vault.SETTINGS_MAX + 1)}, {}, 'settings'),
    ({'core.timezone': 'x' * (vault.VALUE_MAX + 1)}, {}, 'settings'),
    ({}, {('slack', f'n{i}'): {'bot-token': BOT} for i in range(vault.INSTANCES_MAX + 1)}, 'instances'),
    ({}, {('slack', 'main'): {'bot-token': 'y' * (vault.VALUE_MAX + 1)}}, 'instances/slack/main'),
    ({}, {**{('slack', f'n{i}'): {'bot-token': BOT} for i in range(150)}, **{('jira', f'n{i}'): {'url': 'https://jira.example.com'} for i in range(vault.INSTANCES_MAX - 149)}}, 'instances'),
    ({}, {('slack', 'main'): {f'f{i}': 'x' for i in range(vault.FIELDS_MAX + 1)}}, 'instances/slack/main'),
])
def test_the_caps_refuse_the_read_with_one_warning_that_names_the_path_and_no_value(caplog, settings, instances, where):
    # given
    server = FakeVault(settings=settings, instances=instances)
    feed = source(server)

    # when
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(TooMuch) as raised:
                feed(BASE)

    # then
    lines = [r.getMessage() for r in caplog.records if r.name == 'skopax.plugins.vault']
    assert isinstance(raised.value, SourceDown) and where in str(raised.value)
    assert len(lines) == 1 and where in lines[0] and 'xxxx' not in caplog.text and 'yyyy' not in caplog.text and BOT not in caplog.text


@pytest.mark.parametrize('addr', ['http://vault.example.com:8200', 'ftp://vault.example.com', 'https://user:pw@vault.example.com', 'https://vault.example.com?x=1', 'vault.example.com'])
def test_an_address_that_is_not_https_or_carries_credentials_is_refused_before_any_request(addr):
    # given
    server = FakeVault()

    # when
    with pytest.raises(BadSetup) as raised:
        read({'VAULT_ADDR': addr}, server)

    # then
    assert server.calls == [] and 'VAULT_ADDR' in str(raised.value) and 'pw' not in str(raised.value)


@pytest.mark.parametrize('addr', ['http://127.0.0.1:8200', 'http://localhost:8200', 'http://[::1]:8200'])
def test_plain_http_is_allowed_only_for_a_loopback_address(addr):
    # given
    server = FakeVault()

    # when
    out = read({'VAULT_ADDR': addr}, server)

    # then
    assert out['values'] and server.calls


@pytest.mark.parametrize('env', [{'VAULT_AUTH': 'ldap'}, {'VAULT_TOKEN': ''}, {'VAULT_AUTH': 'kubernetes'}, {'VAULT_AUTH': 'approle', 'VAULT_ROLE_ID': ROLE_ID},
                                 {'VAULT_KV_MOUNT': '../sys'}, {'VAULT_SKOPAX_PATH': 'a/../b'}, {'VAULT_TOKEN_FILE': '/nonexistent/vault/token', 'VAULT_TOKEN': ''}])
def test_a_wrong_setup_is_refused_before_any_request_and_names_the_variable(env):
    # given
    server = FakeVault()

    # when
    with pytest.raises(BadSetup) as raised:
        read(env, server)

    # then
    assert server.calls == [] and 'VAULT_' in str(raised.value) and TOKEN not in str(raised.value)


def test_the_ca_certificate_file_is_read_and_handed_to_the_transport(tmp_path):
    # given
    ca = tmp_path / 'ca.pem'
    ca.write_text(PEM, encoding='utf-8')
    made: list[str] = []

    # when
    source(made=made)({**BASE, 'VAULT_CACERT': str(ca)})
    source(made=made)(BASE)

    # then
    assert made == [PEM.strip(), '']


def test_a_ca_certificate_file_that_cannot_be_read_is_a_bad_setup(tmp_path):
    # given
    server = FakeVault()

    # when
    with pytest.raises(BadSetup) as raised:
        read({'VAULT_CACERT': str(tmp_path / 'missing.pem')}, server)

    # then
    assert 'VAULT_CACERT' in str(raised.value) and server.calls == []


def test_the_default_transport_follows_no_redirect_and_trusts_the_given_pem(monkeypatch):
    # given
    made: list[tuple] = []
    monkeypatch.setattr(remote, 'sender', lambda **kw: made.append(('plain', kw)) or 'plain')
    monkeypatch.setattr(remote, 'pem_sender', lambda pem, **kw: made.append(('pem', pem, kw)) or 'pem')

    # when
    plain, pinned, local = vault.transport_for('', False), vault.transport_for(PEM, False), vault.transport_for('', True)

    # then
    assert (plain, pinned, local) == ('plain', 'pem', 'plain')
    assert made[0] == ('plain', {'follow': False, 'limit': vault.ANSWER_LIMIT, 'proxied': True})
    assert made[1] == ('pem', PEM, {'follow': False, 'limit': vault.ANSWER_LIMIT, 'proxied': True})
    assert made[2] == ('plain', {'follow': False, 'limit': vault.ANSWER_LIMIT, 'proxied': False})


def test_the_remote_pem_sender_verifies_with_the_pem_and_can_refuse_redirects(monkeypatch):
    # given
    made: list[tuple] = []
    monkeypatch.setattr(remote, 'tls_context', lambda pem, pinned=False: ('ctx', pem))
    monkeypatch.setattr(remote, 'sender', lambda context=None, follow=True, limit=None, proxied=True: made.append((context, follow, limit, proxied)) or 'sent')

    # when
    got = remote.pem_sender(PEM, follow=False, limit=5, proxied=False)

    # then
    assert got == 'sent' and made == [(('ctx', PEM), False, 5, False)]


@pytest.mark.parametrize('failure', [remote.TimedOut('x'), remote.Unreachable('x'), remote.NotJson('x'), HttpError(503, 'https://vault.example.com/v1/secret/data/skopax/settings')])
def test_vault_down_or_late_is_a_source_down_without_the_path_or_token(failure):
    # given
    server = FakeVault()
    server.fail = failure

    # when
    with pytest.raises(SourceDown) as raised:
        read(server=server)

    # then
    assert not isinstance(raised.value, Denied) and TOKEN not in str(raised.value) and '/v1/' not in str(raised.value)


def test_every_call_fits_one_deadline_under_the_hook_timeout_and_a_spent_deadline_stops_the_read():
    # given
    server, clock = FakeVault(), Clock()
    slow = FakeVault()

    def late(method, url, headers, data, timeout):
        clock.now += 3
        return slow(method, url, headers, data, timeout)

    # when
    source(server, clock)(BASE)
    with pytest.raises(vault.TimedOut):
        source(late, clock)(BASE)

    # then
    assert vault.DEADLINE_S < bounded.HOOK_TIMEOUT_S
    assert all(c[5] * remote.PAGE_DEADLINE <= vault.DEADLINE_S for c in server.calls)
    assert len(slow.calls) < 4


def plugin_with(feed) -> object:
    return dataclasses.replace(vault.PLUGIN, hooks=Hooks(settings_source=feed))


def test_vault_down_later_keeps_the_last_good_settings_and_instances_through_the_real_refresh():
    # given
    server = FakeVault()
    registry = Registry.of(plugin_with(source(server)), slack.PLUGIN)
    b = boot(dict(BASE), registry)
    live = SourceSet(b.loaders, b.loaded, 60)
    instances = Instances(mongomock.MongoClient(tz_aware=True)['vault'], registry, lambda: Sealer('vault-test-key'), sources=live.current)
    instances.refresh()
    before = instances.get('slack', 'main')
    server.fail = remote.TimedOut('x')

    # when
    changed = live.refresh()
    instances.refresh()
    after = instances.get('slack', 'main')

    # then
    assert b.settings.timezone == 'Europe/Berlin' and before.secrets['bot-token'] == BOT
    assert changed is False and after.secrets['bot-token'] == BOT and after.source == before.source
    assert [d.values for d in live.current() if d.label.startswith('Vault')] == [{'slack.post-sev': 'high', 'core.timezone': 'Europe/Berlin'}]
    assert live.status()['Vault']['ok'] is False


def test_an_unconfigured_install_has_no_vault_source_at_all():
    # when
    b = boot({}, Registry.of(vault.PLUGIN))

    # then
    assert b.loaded['Vault'] is None and not [d for d in b.sources if d.label.startswith('Vault')]


def test_no_token_jwt_secret_id_or_secret_value_reaches_the_log(caplog, tmp_path):
    # given
    held = tmp_path / 'secret-id'
    held.write_text(SECRET_ID, encoding='utf-8')
    env = {'VAULT_ADDR': ADDR, 'VAULT_AUTH': 'approle', 'VAULT_ROLE_ID': ROLE_ID, 'VAULT_SECRET_ID_FILE': str(held)}
    server = FakeVault(instances={('slack', 'main'): {'bot-token': BOT, 'app-token': 'z' * (vault.VALUE_MAX + 1)}})
    feed = source(server)

    # when
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TooMuch):
            feed(env)
        server.instances = {('slack', 'main'): {'bot-token': BOT}}
        feed(env)
        server.denied.add('/v1/secret/data/skopax/settings')
        with pytest.raises(Denied):
            feed(env)
        server.fail = HttpError(500, f'{ADDR}/v1/secret/data/skopax/settings', BOT)
        with pytest.raises(SourceDown):
            feed(env)

    # then
    for secret in (SECRET_ID, BOT, 'test-vault-issued', 'zzzz', '/v1/secret/data'):
        assert secret not in caplog.text


def test_the_pem_sender_context_checks_the_host_requires_a_certificate_and_trusts_only_the_given_pem(monkeypatch):
    # given
    from tests.test_pinned_ca import made as made_cert
    pem = made_cert('vault.example.com', ca=True).pem
    contexts: list = []
    monkeypatch.setattr(remote, 'sender', lambda context=None, follow=True, limit=None, proxied=True: contexts.append(context) or 'sent')

    # when
    remote.pem_sender(pem, follow=False)

    # then
    context = contexts[0]
    assert context.check_hostname is True and context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()['x509_ca'] == 1 and len(context.get_ca_certs()) == 1


def test_a_loopback_http_address_is_sent_without_a_proxy_and_https_through_it():
    # given
    direct: list[bool] = []

    # when
    source(direct=direct)({**BASE, 'VAULT_ADDR': 'http://127.0.0.1:8200'})
    source(direct=direct)(BASE)

    # then
    assert direct == [True, False]


class SlowVault(FakeVault):
    def __init__(self, delay: float, **kw):
        super().__init__(**kw)
        self.delay = delay
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def __call__(self, method, url, headers, data, timeout):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.delay)
            return super().__call__(method, url, headers, data, timeout)
        finally:
            with self.lock:
                self.running -= 1


def test_many_instances_are_read_in_parallel_with_one_login_inside_the_deadline(monkeypatch, tmp_path):
    # given
    held = tmp_path / 'secret-id'
    held.write_text(SECRET_ID, encoding='utf-8')
    monkeypatch.setattr(vault, 'DEADLINE_S', 2.0)
    server = SlowVault(0.04, instances={('slack', f'n{i}'): {'bot-token': BOT} for i in range(120)})
    feed = Vault(lambda pem, unproxied=False: server)

    # when
    started = time.monotonic()
    out = feed({'VAULT_ADDR': ADDR, 'VAULT_AUTH': 'approle', 'VAULT_ROLE_ID': ROLE_ID, 'VAULT_SECRET_ID_FILE': str(held)})
    took = time.monotonic() - started

    # then
    assert len(out['instances']) == 120 and server.issued == 1
    assert 1 < server.peak <= vault.WORKERS == 8 and took < 2.0
    assert sorted(i['name'] for i in out['instances']) == sorted(f'n{i}' for i in range(120))


def test_a_policy_denial_keeps_the_token_so_three_denied_reads_cost_one_login(tmp_path):
    # given
    held = tmp_path / 'secret-id'
    held.write_text(SECRET_ID, encoding='utf-8')
    server = FakeVault()
    server.denied.add('/v1/secret/data/skopax/settings')
    feed = source(server)
    env = {'VAULT_ADDR': ADDR, 'VAULT_AUTH': 'approle', 'VAULT_ROLE_ID': ROLE_ID, 'VAULT_SECRET_ID_FILE': str(held)}

    # when
    for _ in range(3):
        with pytest.raises(Denied):
            feed(env)

    # then
    assert server.issued == 1


def test_a_bad_env_token_is_denied_at_once():
    # given
    server = FakeVault()
    server.valid = set()

    # when
    with pytest.raises(Denied):
        read(server=server)

    # then
    assert server.paths() == ['/v1/secret/data/skopax/settings']


@pytest.mark.parametrize('lease', [0, 10, 1e999])
def test_a_lease_that_is_zero_short_or_endless_is_cached_for_the_floor_only(tmp_path, lease):
    # given
    jwt = tmp_path / 'jwt'
    jwt.write_text(JWT, encoding='utf-8')
    server, clock = FakeVault(lease=lease), Clock()
    feed = source(server, clock)
    env = {'VAULT_ADDR': ADDR, 'VAULT_AUTH': 'kubernetes', 'VAULT_K8S_ROLE': 'skopax', 'VAULT_K8S_TOKEN_FILE': str(jwt)}

    # when
    feed(env)
    clock.now += vault.LEASE_FLOOR_S - 1
    feed(env)
    first = server.issued
    clock.now += 2
    feed(env)

    # then
    assert vault.LEASE_FLOOR_S == 30 and first == 1 and server.issued == 2


@pytest.mark.parametrize('failure', [RecursionError('deep'), ValueError('bad json')])
def test_a_json_answer_that_cannot_be_read_is_a_source_down_with_a_plain_message(failure):
    # given
    server = FakeVault()
    server.fail = failure

    # when
    with pytest.raises(BadAnswer) as raised:
        read(server=server)

    # then
    assert isinstance(raised.value, SourceDown) and str(raised.value) == 'Vault did not answer with readable JSON'


def test_names_in_warnings_are_quoted_and_cut_at_100_characters(caplog):
    # given
    long_key = 'k' * 300
    server = FakeVault(settings={long_key: {'nested': 1}})
    stray = FakeVault()
    stray.extra = {'': ['f' * 300]}

    # when
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BadAnswer):
            read(server=server)
        read(server=stray)

    # then
    assert repr('k' * 100) in caplog.text and 'k' * 101 not in caplog.text
    assert repr('f' * 100) in caplog.text and 'f' * 101 not in caplog.text


def test_the_warning_memory_holds_at_most_200_entries():
    # given
    feed = Vault(lambda pem, unproxied=False: FakeVault())

    # when
    for i in range(250):
        feed.warn(f'warning {i}')

    # then
    assert len(feed._said) <= vault.SAID_MAX == 200


class FirstDenied(FakeVault):
    def __call__(self, method, url, headers, data, timeout):
        path = urlsplit(url).path
        if path.startswith('/v1/secret/data/skopax/instances/') and not path.endswith('/n007'):
            time.sleep(0.05)
        return super().__call__(method, url, headers, data, timeout)


def test_the_first_worker_error_cancels_the_queued_instance_reads():
    # given
    server = FirstDenied(instances={('slack', f'n{i:03d}'): {'bot-token': BOT} for i in range(100)})
    server.denied.add('/v1/secret/data/skopax/instances/slack/n007')

    # when
    with pytest.raises(Denied):
        read(server=server)

    # then
    reads = [p for p in server.paths() if p.startswith('/v1/secret/data/skopax/instances/')]
    assert len(reads) < 10
