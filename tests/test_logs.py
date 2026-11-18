import json
import logging
import sys

import mcp.client.streamable_http as sdk_stream
import mcp.shared.session as sdk_session
import pytest

from skopax.logs import HIDDEN, HideQuery, JsonFormatter, LibraryText, SkipHealth, setup

TOUCHED = ('mcp', 'httpx', 'uvicorn', 'uvicorn.error', 'uvicorn.access')


@pytest.fixture
def kept_logging():
    root = logging.getLogger()
    saved = (list(root.handlers), root.level, {n: (logging.getLogger(n).level, list(logging.getLogger(n).filters), list(logging.getLogger(n).handlers),
                                                   logging.getLogger(n).propagate) for n in TOUCHED})
    yield
    root.handlers, root.level = saved[0], saved[1]
    for n, (level, filters, handlers, propagate) in saved[2].items():
        logger = logging.getLogger(n)
        logger.level, logger.filters, logger.handlers, logger.propagate = level, filters, handlers, propagate


def record(level: int, msg: str, *args, exc_info=None, name: str = 'skopax.investigator') -> logging.LogRecord:
    return logging.LogRecord(name, level, 'x.py', 1, msg, args, exc_info)


def test_lines_are_json_with_the_java_level_names_and_the_exception():
    # given
    try:
        try:
            raise KeyError('inner')
        except KeyError as e:
            raise RuntimeError('mongo down') from e
    except RuntimeError:
        import sys
        failed = record(logging.ERROR, 'investigation %s crashed', 'r1', exc_info=sys.exc_info())

    # when
    info = json.loads(JsonFormatter().format(record(logging.INFO, 'investigation %s: %s', 'r1', 'done')))
    warn = json.loads(JsonFormatter().format(record(logging.WARNING, 'reaped')))
    crashed = json.loads(JsonFormatter().format(failed))

    # then
    assert (info['level'], info['message'], info['loggerName'], info['thread']) == ('INFO', 'investigation r1: done', 'skopax.investigator', 'MainThread')
    assert isinstance(info['timeMillis'], int) and 'thrown' not in info
    assert warn['level'] == 'WARN'
    assert crashed['level'] == 'ERROR' and crashed['thrown']['name'] == 'RuntimeError' and crashed['thrown']['message'] == 'mongo down'
    assert 'Traceback' in crashed['thrown']['extendedStackTrace'] and crashed['thrown']['cause'] == {'name': 'KeyError', 'message': "'inner'"}


def test_health_probes_are_dropped_from_the_access_log_and_the_api_is_kept():
    # given
    probe = record(logging.INFO, '%s - "%s %s HTTP/%s" %d', '10.0.0.1:1', 'GET', '/actuator/health/liveness', '1.1', 200, name='uvicorn.access')
    api = record(logging.INFO, '%s - "%s %s HTTP/%s" %d', '10.0.0.1:1', 'GET', '/api/state', '1.1', 200, name='uvicorn.access')

    # then
    assert SkipHealth().filter(probe) is False
    assert SkipHealth().filter(api) is True


def test_setup_routes_uvicorn_through_the_json_root_handler(kept_logging):
    # when
    setup()

    # then
    root = logging.getLogger()
    assert len(root.handlers) == 1 and isinstance(root.handlers[0].formatter, JsonFormatter)
    assert all(logging.getLogger(n).handlers == [] and logging.getLogger(n).propagate for n in ('uvicorn', 'uvicorn.error', 'uvicorn.access'))
    assert any(isinstance(f, SkipHealth) for f in logging.getLogger('uvicorn.access').filters)


def test_oauth_queries_and_mcp_library_text_never_reach_the_log(kept_logging):
    # given
    line = '%s - "%s %s HTTP/%s" %d'
    callback = record(logging.INFO, line, '10.0.0.1:1', 'GET', '/api/admin/mcp-tools/oauth/callback?code=c0de-s3cr3t&state=st4te', '1.1', 303, name='uvicorn.access')
    sso = record(logging.INFO, line, '10.0.0.1:1', 'GET', '/authorized?code=c0de-s3cr3t&state=st4te', '1.1', 303, name='uvicorn.access')
    other = record(logging.INFO, line, '10.0.0.1:1', 'GET', '/api/state?view=board', '1.1', 200, name='uvicorn.access')
    try:
        raise ValueError('s3cr3t from the server')
    except ValueError:
        failed = logging.LogRecord('mcp.client.streamable_http', logging.ERROR, sdk_stream.__file__, 417, 'Error parsing JSON response %s', ('s3cr3t',), sys.exc_info())
    rooted = logging.LogRecord('root', logging.WARNING, sdk_session.__file__, 383, 'Failed to validate request: s3cr3t', (), None)
    named = record(logging.WARNING, 'Raw result: %s', 's3cr3t', name='mcp.client.streamable_http')
    own = record(logging.WARNING, 'kept %s', 'text', name='skopax.mcptools')

    # when
    hide, quiet = HideQuery(), LibraryText()
    kept = [hide.filter(r) for r in (callback, sso, other)] + [quiet.filter(r) for r in (failed, rooted, own, named)]
    lines = [json.loads(JsonFormatter().format(r)) for r in (callback, sso, other, failed, rooted, own, named)]
    setup()

    # then
    assert kept == [True] * 7
    assert lines[0]['message'] == '10.0.0.1:1 - "GET /api/admin/mcp-tools/oauth/callback HTTP/1.1" 303'
    assert lines[1]['message'] == '10.0.0.1:1 - "GET /authorized HTTP/1.1" 303' and lines[2]['message'].endswith('/api/state?view=board HTTP/1.1" 200')
    assert (lines[3]['message'], lines[4]['message'], lines[5]['message']) == ('ValueError at mcp/client/streamable_http.py:417', f'{HIDDEN} at mcp/shared/session.py:383', 'kept text')
    assert (lines[3]['loggerName'], lines[4]['loggerName'], lines[6]['message']) == ('mcp.client.streamable_http', 'root', f'{HIDDEN} at x.py:1')
    assert 'thrown' not in lines[3] and 's3cr3t' not in json.dumps(lines) and 'st4te' not in json.dumps(lines)
    assert logging.getLogger('mcp').level == logging.WARNING and logging.getLogger('httpx').level == logging.WARNING
    assert any(isinstance(f, HideQuery) for f in logging.getLogger('uvicorn.access').filters)
    assert any(isinstance(f, LibraryText) for f in logging.getLogger().handlers[0].filters)
