import dataclasses
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skopax import app as app_module
from skopax import registry
from skopax.app import create_app
from skopax.contract import Field, Refused, SuggestContext, Suggestions
from skopax.messages import msg
from skopax.registry import Registry
from skopax.remote import HttpError
from tests.conftest import AUTH, settings
from tests.plugins import demo_plugin
from tests.test_admin_plugins import SECRET
from tests.test_login import make_client, post_login, with_password

HERE = Path(demo_plugin.__file__).parent
OTHER = 'tok-east-5b2e-plain'
GOOD = {'url': 'https://demo.example.com', 'token': SECRET}
SEEN: list[SuggestContext] = []
HOLD = threading.Event()
SAID: dict = {}


def topics(ctx: SuggestContext) -> list[str]:
    SEEN.append(ctx)
    url = ctx.values.get('url', '')
    token = ctx.values.get('token', '')
    if 'wait.' in url:
        HOLD.wait(5)
    if 'long.' in url:
        raise Refused('x' * 2000 + token + 'y' * 3000)
    if 'noted.' in url:
        return Suggestions(('a', 'b', 'a'), f'only some, see {token}' + 'z' * 400)
    if 'refuse.' in url:
        raise Refused(f'token {token} cannot list topics')
    if 'boom.' in url:
        raise RuntimeError(f'boom {token}')
    if 'denied.' in url:
        raise HttpError(403, url)
    if 'odd.' in url:
        return 'not a list'
    if 'many.' in url:
        return [f'topic-{i:04d}' for i in range(2500)]
    if 'messy.' in url:
        return ['b', 'a', 'b', '', '  ', 'x' * 201, 'y' * 200, 'tab' + chr(9) + 'in', 7, None, 'with, comma', f'has {token}', ' padded ', 'caf' + chr(0xe9)]
    return SAID.get('values', ['orders', 'payments'])


def single(ctx: SuggestContext) -> list[str]:
    return ['eu', 'us']


FIELDS = demo_plugin.PLUGIN.fields + (Field('topics', 'Topics', 'text', suggest=topics, separator=','), Field('region', 'Region', 'text', suggest=single),
                                      Field('note', 'Note', 'text'))
PICKY = dataclasses.replace(demo_plugin.PLUGIN, fields=FIELDS)


@pytest.fixture(autouse=True)
def clean():
    SEEN.clear()
    HOLD.clear()
    SAID.clear()
    yield
    HOLD.set()


@contextmanager
def served(store, plugin=PICKY):
    with TestClient(create_app(settings(), store, registry=Registry.of(plugin, directory=HERE))) as c:
        yield c


def add(c, name='main', values=None):
    res = c.post('/api/admin/plugins/demo/instances', json={'name': name, 'values': GOOD if values is None else values, 'enabled': False}, headers=AUTH)
    assert res.status_code == 201, res.text
    return res


def ask(c, key='topics', **body):
    return c.post(f'/api/admin/plugins/demo/fields/{key}/suggestions', json={'values': {}, **body}, headers=AUTH)


def loaded(plugin):
    return Registry.of(plugin, directory=HERE).get(plugin.id)


@pytest.mark.parametrize('field', [Field('pick', 'Pick', 'secret', suggest=single), Field('pick', 'Pick', 'choice', choices=('a',), suggest=single),
                                   Field('pick', 'Pick', 'bool', suggest=single), Field('pick', 'Pick', 'text', suggest='nope'),
                                   Field('pick', 'Pick', 'text', suggest=single, separator='|'), Field('pick', 'Pick', 'text', separator=' ')],
                         ids=['secret', 'choice', 'bool', 'not-callable', 'pipe', 'space'])
def test_a_suggest_only_sits_on_a_text_field_and_the_separator_is_none_a_comma_or_a_semicolon(field):
    # given
    plugin = dataclasses.replace(demo_plugin.PLUGIN, fields=demo_plugin.PLUGIN.fields + (field,))

    # when
    found = loaded(plugin)

    # then
    assert found.state == 'broken' and 'pick' in found.reason


def test_a_text_or_url_field_may_suggest_and_the_view_says_so_without_the_function():
    # given
    plugin = dataclasses.replace(demo_plugin.PLUGIN, fields=demo_plugin.PLUGIN.fields + (Field('a', 'A', 'text', suggest=single, separator=';'), Field('b', 'B', 'url', suggest=single)))

    # when
    found = loaded(plugin)
    views = {f['key']: f for f in registry.describe(plugin)['fields']}

    # then
    assert found.state == 'ok', found.reason
    assert (views['a']['suggest'], views['a']['separator'], views['b']['suggest'], views['b']['separator']) == (True, ';', True, '')
    assert 'suggest' not in views['url'] and 'separator' not in views['url']


def test_a_new_form_gets_the_values_it_typed_and_never_a_saved_secret(store):
    # given
    with served(store) as c:
        add(c)

        # when
        typed = ask(c, values={'url': 'https://demo.example.com', 'token': 'typed-1234'})
        blank = ask(c, values={'url': 'https://demo.example.com'})

    # then
    assert typed.status_code == 200 and typed.json() == {'values': ['orders', 'payments']}
    assert SEEN[0].values['token'] == 'typed-1234' and SEEN[0].values['url'] == 'https://demo.example.com'
    assert blank.status_code == 200 and 'token' not in SEEN[1].values
    assert SEEN[0].deadline > 0 and callable(SEEN[0].transport)


def test_an_edited_instance_lends_only_its_own_saved_secret(store):
    # given
    with served(store) as c:
        add(c)
        add(c, 'east', {'url': 'https://east.example.com', 'token': OTHER})

        # when
        main = ask(c, name='main', values={'token': None})
        east = ask(c, name='east', values={'token': None})
        typed = ask(c, name='main', values={'token': 'typed-9876'})
        cleared = ask(c, name='main', values={'token': ''})
        unknown = ask(c, name='gone', values={})

    # then
    assert [r.status_code for r in (main, east, typed, cleared)] == [200] * 4
    assert [s.values.get('token') for s in SEEN] == [SECRET, OTHER, 'typed-9876', None]
    assert SEEN[0].values['url'] == GOOD['url'] and SEEN[1].values['url'] == 'https://east.example.com'
    assert unknown.status_code == 404 and unknown.json()['detail'] == msg('instances.unknown', name='gone')


def test_a_moved_address_never_gets_the_saved_secret(store):
    # given
    with served(store) as c:
        add(c)

        # when
        moved = ask(c, name='main', values={'url': 'https://elsewhere.example.com'})

    # then
    assert moved.status_code == 422 and moved.json()['field'] == 'token' and moved.json()['detail'] == msg('integrations.suggest_reenter', field='Token') == 'Re-enter Token to load the list'
    assert SEEN == []


def test_the_list_works_while_required_fields_are_still_empty(store):
    # given
    with served(store) as c:
        # when
        res = ask(c, values={'token': 'typed-1'})

    # then
    assert res.status_code == 200 and res.json() == {'values': ['orders', 'payments']}


def test_the_answer_is_capped_cleaned_and_deduplicated_in_the_order_the_plugin_gave(store):
    # given
    with served(store) as c:
        # when
        many = ask(c, values={'url': 'https://many.example.com'})
        messy = ask(c, values={'url': 'https://messy.example.com', 'token': SECRET})
        one = ask(c, 'region', values={'url': 'https://messy.example.com'})

    # then
    assert many.status_code == 200 and len(many.json()['values']) == 2000 and many.json()['values'][:2] == ['topic-0000', 'topic-0001']
    assert messy.json() == {'values': ['b', 'a', 'y' * 200, 'padded', 'caf' + chr(0xe9)]}
    assert SECRET not in messy.text
    assert one.json() == {'values': ['eu', 'us']}


def test_a_refusal_keeps_its_words_without_the_secret_and_a_failure_is_named_without_the_upstream_text(store):
    # given
    with served(store) as c:
        # when
        refused = ask(c, values={'url': 'https://refuse.example.com', 'token': SECRET})
        boom = ask(c, values={'url': 'https://boom.example.com', 'token': SECRET})
        denied = ask(c, values={'url': 'https://denied.example.com', 'token': SECRET})
        odd = ask(c, values={'url': 'https://odd.example.com'})

    # then
    assert refused.status_code == 400 and refused.json() == {'detail': 'token *** cannot list topics'}
    assert boom.status_code == 502 and boom.json() == {'detail': msg('integrations.suggest_failed', error='RuntimeError')}
    assert denied.status_code == 502 and 'HttpError' not in denied.text and '403' in denied.json()['detail']
    assert odd.status_code == 502 and odd.json() == {'detail': msg('integrations.suggest_bad')}
    assert all(SECRET not in r.text for r in (refused, boom, denied))


def test_a_slow_list_answers_with_a_timeout_and_a_second_ask_is_told_to_wait(store, monkeypatch):
    # given
    monkeypatch.setattr(app_module, 'SUGGEST_TIMEOUT_S', 0.2)
    with served(store) as c:
        # when
        slow = ask(c, values={'url': 'https://wait.example.com'})
        again = ask(c, values={'url': 'https://wait.example.com'})
        other = ask(c, 'region', values={'url': 'https://wait.example.com'})
        HOLD.set()

    # then
    assert slow.status_code == 502 and slow.json() == {'detail': msg('integrations.suggest_timeout', seconds='0.2')}
    assert again.status_code == 409 and again.json() == {'detail': msg('integrations.suggest_busy')}
    assert other.status_code == 200


def test_two_admins_on_two_new_instance_forms_of_one_field_each_get_their_own_list(store, monkeypatch):
    # given
    monkeypatch.setattr(app_module, 'SUGGEST_TIMEOUT_S', 0.2)
    with_password(store)
    with served(store) as c:
        other = TestClient(c.app, base_url='https://testserver', follow_redirects=False)
        post_login(other)

        # when
        slow = ask(c, values={'url': 'https://wait.example.com'})
        theirs = other.post('/api/admin/plugins/demo/fields/topics/suggestions', json={'values': {'url': 'https://demo.example.com'}}, headers={'Origin': 'https://testserver'})
        again = ask(c, values={'url': 'https://wait.example.com'})
        HOLD.set()

    # then
    assert slow.status_code == 502
    assert theirs.status_code == 200, theirs.text
    assert again.status_code == 409, 'the same admin asking again still waits for the first list'


def test_an_unknown_field_or_a_field_without_a_list_is_404(store):
    # given
    with served(store) as c:
        # when
        unknown = ask(c, 'nope')
        plain = ask(c, 'note')
        secret = ask(c, 'token')
        plugin = c.post('/api/admin/plugins/nope/fields/topics/suggestions', json={'values': {}}, headers=AUTH)

    # then
    assert unknown.status_code == 404 and unknown.json() == {'detail': msg('integrations.suggest_none', field='nope')}
    assert plain.status_code == 404 and plain.json() == {'detail': msg('integrations.suggest_none', field='note')}
    assert secret.status_code == 404
    assert plugin.status_code == 404
    assert SEEN == []


def test_the_route_needs_an_admin(store):
    # given
    with served(store) as c:
        # when
        anonymous = c.post('/api/admin/plugins/demo/fields/topics/suggestions', json={'values': {}})

    # then
    assert anonymous.status_code == 401 and SEEN == []


def test_a_signed_in_admin_from_another_site_is_refused(store):
    # given
    with_password(store)
    client = make_client(store)
    post_login(client)
    path = '/api/admin/plugins/cloudflare/fields/status-components/suggestions'

    # when
    other = client.post(path, json={'values': {}}, headers={'Origin': 'https://evil.example.com'})

    # then
    assert other.status_code == 403 and other.json() == {'detail': msg('login.cross_site')}


def test_the_catalog_serves_the_cloudflare_pickers_and_the_zones_list_asks_for_a_token_first(store):
    # given
    with TestClient(create_app(settings(), store)) as c:
        # when
        rows = {p['id']: p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins']}
        fields = {f['key']: f for f in rows['cloudflare']['fields']}
        zones = c.post('/api/admin/plugins/cloudflare/fields/zones/suggestions', json={'values': {}}, headers=AUTH)

    # then
    assert (fields['zones']['suggest'], fields['zones']['separator']) == (True, ',')
    assert (fields['status-components']['suggest'], fields['status-components']['separator']) == (True, ';')
    assert 'suggest' not in fields['account']
    assert zones.status_code == 400 and zones.json() == {'detail': 'Enter the API token first'}


def test_a_refusal_is_not_logged_and_a_failure_is_logged_by_its_type_only(store, caplog):
    # given
    with served(store) as c:
        # when
        ask(c, values={'url': 'https://refuse.example.com', 'token': SECRET})
        ask(c, values={'url': 'https://boom.example.com', 'token': SECRET})

    # then
    lines = [r.getMessage() for r in caplog.records if r.name == 'skopax.runtime']
    assert lines == [msg('integrations.suggest_failed_log', plugin='demo', field='topics', error='RuntimeError')]


def test_a_long_refusal_is_cut_after_its_secret_is_masked_and_a_plugin_may_add_a_note(store):
    # given
    with served(store) as c:
        # when
        long = ask(c, values={'url': 'https://long.example.com', 'token': SECRET})
        noted = ask(c, values={'url': 'https://noted.example.com', 'token': SECRET})

    # then
    assert long.status_code == 400 and len(long.json()['detail']) == 300
    assert long.json()['detail'] == 'x' * 300 and SECRET not in long.text
    assert noted.status_code == 200 and noted.json()['values'] == ['a', 'b'] and SECRET not in noted.text
    assert noted.json()['note'] == ('only some, see ***' + 'z' * 400)[:200]
