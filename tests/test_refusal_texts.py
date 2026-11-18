import pytest

from skopax import monitor, sweeps
from skopax.contract import Capabilities, Field, Tool
from skopax.kit import connection
from skopax.messages import msg
from skopax.remote import HttpError
from tests.plugins.base import minimal
from tests.test_plugin_tools import SCHEMA, Rig

URL = 'https://grafana.example.com/api/datasources'


def forbidden(ctx, args):
    raise HttpError(403, URL)


def rejected(ctx, args):
    raise HttpError(401, URL)


def failing(ctx, args):
    raise HttpError(502, URL)


GUARDED = minimal('guarded', 'Guarded', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True),),
                  tools=(Tool('guarded_forbidden', 'Answers 403', SCHEMA, forbidden, instance_input=None), Tool('guarded_rejected', 'Answers 401', SCHEMA, rejected, instance_input=None),
                         Tool('guarded_failing', 'Answers 502', SCHEMA, failing, instance_input=None)))


@pytest.mark.parametrize('code, key', [(401, 'connection.rejected'), (403, 'connection.forbidden')])
def test_a_refusal_names_its_kind_in_a_test_and_a_pass(code, key):
    # given
    error = HttpError(code, URL)

    # when
    tested = connection.result(lambda: (_ for _ in ()).throw(error))
    passes = (sweeps.failed(error), monitor.failed(error))

    # then
    assert (tested.ok, tested.summary) == (False, msg(key, status=code))
    assert passes == (f'error: {msg(key, status=code)}', f'error: {msg(key, status=code)}')


def test_the_two_refusal_texts_differ_and_other_statuses_keep_their_kind():
    # when
    texts = (msg('connection.rejected', status=401), msg('connection.forbidden', status=403))
    other = HttpError(502, URL)

    # then
    assert texts == ('The service refused the credentials (HTTP 401)', 'The service accepted the credentials but they lack a permission this needs (HTTP 403)')
    assert (sweeps.failed(other), monitor.failed(other)) == ('error: HttpError code 502', 'error: HttpError code 502')
    assert not hasattr(connection, 'REJECTED'), 'the refusal codes live in monitor.REFUSALS only'


def test_a_refused_tool_call_names_the_refusal(store):
    # given
    rig = Rig(store, GUARDED)
    rig.add(GUARDED, 'main', primary=True)
    b = rig.box(run_id='r1')

    # when
    lacking, lacking_error = b.call('guarded_forbidden', {})
    wrong, wrong_error = b.call('guarded_rejected', {})
    broken, broken_error = b.call('guarded_failing', {})

    # then
    assert lacking_error and lacking == msg('connection.forbidden', status=403)
    assert wrong_error and wrong == msg('connection.rejected', status=401)
    assert broken_error and broken == 'HttpError: the guarded_failing tool failed'
