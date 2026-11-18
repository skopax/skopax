import io
import json
import logging
import sys

import mcp.client.streamable_http as sdk_stream
import mcp.shared.session as sdk_session
import pytest

from fastapi.testclient import TestClient

from skopax import logs
from skopax.app import create_app
from skopax.config import load_settings
from skopax.kube import Kube
from skopax.logs import HIDDEN, HideQuery, JsonFormatter, LibraryText, Log4jFormatter, SkipHealth, TextFormatter, setup
from skopax.settingsources import Follower, boot
from skopax.tunables import Live, TunableStore
from tests.conftest import settings
from tests.plugin_kit import registry_with
from tests.plugins import demo_plugin

TOUCHED = ('mcp', 'httpx', 'pymongo', 'httpcore', 'urllib3', 'anthropic', 'uvicorn', 'uvicorn.error', 'uvicorn.access', 'skopax.logs')
STAMP = 1_795_010_712.345
AT = '2026-11-18T14:05:12.345Z'
ESC = '\x1b'


@pytest.fixture
def kept_logging():
    root = logging.getLogger()
    saved = (list(root.handlers), root.level, {n: (logging.getLogger(n).level, list(logging.getLogger(n).filters), list(logging.getLogger(n).handlers),
                                                   logging.getLogger(n).propagate) for n in TOUCHED})
    held, applied, warned, plain = list(logs.HELD), list(logs.APPLIED), set(logs.WARNED), list(logs.PLAIN)
    logs.WARNED.clear()
    yield
    logs.PLAIN[:] = plain
    root.handlers, root.level = saved[0], saved[1]
    for n, (level, filters, handlers, propagate) in saved[2].items():
        logger = logging.getLogger(n)
        logger.level, logger.filters, logger.handlers, logger.propagate = level, filters, handlers, propagate
    logs.HELD[:], logs.APPLIED[:] = held, applied
    logs.WARNED.clear()
    logs.WARNED.update(warned)


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def record(level: int, msg: str, *args, exc_info=None, name: str = 'skopax.investigator') -> logging.LogRecord:
    made = logging.LogRecord(name, level, 'x.py', 1, msg, args, exc_info)
    made.created, made.msecs = STAMP, 345.0
    return made


def crashed_record() -> logging.LogRecord:
    try:
        try:
            raise KeyError('inner')
        except KeyError as e:
            raise RuntimeError('mongo down') from e
    except RuntimeError:
        return record(logging.ERROR, 'investigation %s crashed', 'r1', exc_info=sys.exc_info())


def started(env: dict, stream=None) -> io.StringIO:
    out = stream if stream is not None else io.StringIO()
    setup(env, out)
    return out


def lines_of(out: io.StringIO) -> list[str]:
    return out.getvalue().splitlines()


def test_log4j_lines_are_the_java_layout_with_the_level_names_and_the_exception():
    # given
    failed = crashed_record()

    # when
    info = json.loads(Log4jFormatter().format(record(logging.INFO, 'investigation %s: %s', 'r1', 'done')))
    warn = json.loads(Log4jFormatter().format(record(logging.WARNING, 'reaped')))
    crashed = json.loads(Log4jFormatter().format(failed))

    # then
    assert (info['level'], info['message'], info['loggerName'], info['thread']) == ('INFO', 'investigation r1: done', 'skopax.investigator', 'MainThread')
    assert isinstance(info['timeMillis'], int) and 'thrown' not in info
    assert list(info) == ['timeMillis', 'thread', 'level', 'loggerName', 'message', 'endOfBatch', 'loggerFqcn', 'threadId', 'threadPriority']
    assert warn['level'] == 'WARN'
    assert crashed['level'] == 'ERROR' and crashed['thrown']['name'] == 'RuntimeError' and crashed['thrown']['message'] == 'mongo down'
    assert 'Traceback' in crashed['thrown']['extendedStackTrace'] and crashed['thrown']['cause'] == {'name': 'KeyError', 'message': "'inner'"}


def test_text_lines_are_readable_with_utc_milliseconds_a_padded_level_and_the_trace_below():
    # given
    failed = crashed_record()

    # when
    warn = TextFormatter(color=False).format(record(logging.WARNING, 'the role resolver of plug-in %s failed', 'crm', name='skopax.roles'))
    info = TextFormatter(color=False).format(record(logging.INFO, 'done'))
    fatal = TextFormatter(color=False).format(record(logging.CRITICAL, 'gone'))
    crashed = TextFormatter(color=False).format(failed).split('\n')

    # then
    assert warn == f'{AT} WARN  skopax.roles  the role resolver of plug-in crm failed'
    assert info == f'{AT} INFO  skopax.investigator  done'
    assert fatal == f'{AT} FATAL skopax.investigator  gone'
    assert crashed[0] == f'{AT} ERROR skopax.investigator  investigation r1 crashed'
    assert crashed[1] == 'Traceback (most recent call last):' and crashed[-1] == 'RuntimeError: mongo down'
    assert "KeyError: 'inner'" in crashed and ESC not in '\n'.join(crashed)


def test_a_long_trace_keeps_its_last_part_within_the_trace_limit():
    # given
    try:
        raise ValueError('x' * (logs.TRACE_LIMIT * 2))
    except ValueError:
        failed = record(logging.ERROR, 'failed', exc_info=sys.exc_info())

    # when
    text = TextFormatter(color=False).format(failed)
    doc = json.loads(JsonFormatter().format(failed))

    # then
    head, trace = text.split('\n', 1)
    assert head.endswith('failed') and len(trace) == logs.TRACE_LIMIT - 1 and trace.endswith('x')
    assert len(doc['error']['trace']) == logs.TRACE_LIMIT


def test_text_keeps_one_line_per_record_and_shows_control_characters_escaped():
    # when
    forged = TextFormatter(color=True).format(record(logging.INFO, 'name %s', 'eve\n2026-11-18T00:00:00.000Z ERROR skopax.auth  forged\x1b[2J'))

    # then
    assert '\n' not in forged and forged.count(ESC) == 4
    assert forged.endswith('name eve\\n2026-11-18T00:00:00.000Z ERROR skopax.auth  forged\\x1b[2J')


def test_colour_paints_the_level_and_dims_the_logger_and_leaves_the_message_plain():
    # given
    painted = TextFormatter(color=True)

    # when
    debug, info, warn, error, fatal = (painted.format(record(level, 'plain words')) for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL))

    # then
    assert debug == f'{AT} \x1b[2mDEBUG\x1b[0m \x1b[2mskopax.investigator\x1b[0m  plain words'
    assert info == f'{AT} \x1b[32mINFO\x1b[0m  \x1b[2mskopax.investigator\x1b[0m  plain words'
    assert warn == f'{AT} \x1b[33mWARN\x1b[0m  \x1b[2mskopax.investigator\x1b[0m  plain words'
    assert error.startswith(f'{AT} \x1b[1;31mERROR\x1b[0m ') and fatal.startswith(f'{AT} \x1b[1;31mFATAL\x1b[0m ')


def test_json_lines_are_flat_standard_objects_with_the_error_and_its_cause():
    # given
    failed = crashed_record()

    # when
    info = json.loads(JsonFormatter().format(record(logging.INFO, 'investigation %s: %s', 'r1', 'done')))
    warn = json.loads(JsonFormatter().format(record(logging.WARNING, 'reaped')))
    fatal = json.loads(JsonFormatter().format(record(logging.CRITICAL, 'gone')))
    crashed = json.loads(JsonFormatter().format(failed))

    # then
    assert info == {'time': AT, 'level': 'INFO', 'logger': 'skopax.investigator', 'message': 'investigation r1: done', 'thread': 'MainThread'}
    assert (warn['level'], fatal['level']) == ('WARN', 'FATAL')
    assert set(crashed['error']) == {'type', 'message', 'trace', 'cause'} and (crashed['error']['type'], crashed['error']['message']) == ('RuntimeError', 'mongo down')
    assert crashed['error']['cause'] == {'type': 'KeyError', 'message': "'inner'"} and crashed['error']['trace'].startswith('Traceback')


def test_the_json_formats_never_carry_an_escape_character_even_when_the_message_does(kept_logging):
    # given
    planted = record(logging.ERROR, 'bad \x1b[31mred\x1b[0m', exc_info=crashed_record().exc_info)
    json_out = started({'SKOPAX_LOG_FORMAT': 'json', 'SKOPAX_LOG_COLOR': 'always'})

    # when
    logging.getLogger().handlers[0].handle(planted)
    log4j_out = started({'SKOPAX_LOG_FORMAT': 'log4j', 'SKOPAX_LOG_COLOR': 'always'})
    logging.getLogger().handlers[0].handle(planted)

    # then
    for out in (json_out, log4j_out):
        assert out.getvalue() and ESC not in out.getvalue()
        assert json.loads(lines_of(out)[0])
    assert json.loads(lines_of(json_out)[0])['message'] == 'bad \x1b[31mred\x1b[0m'


def test_setup_defaults_to_coloured_text_at_info(kept_logging):
    # given
    out = started({})

    # when
    logging.getLogger('skopax.x').info('hello')
    logging.getLogger('skopax.x').debug('hidden')

    # then
    root = logging.getLogger()
    assert len(root.handlers) == 1 and isinstance(root.handlers[0].formatter, TextFormatter) and root.level == logging.INFO
    assert lines_of(out) == [f'{lines_of(out)[0][:24]} \x1b[32mINFO\x1b[0m  \x1b[2mskopax.x\x1b[0m  hello']


@pytest.mark.parametrize('env, painted', [
    ({'SKOPAX_LOG_COLOR': 'always'}, True),
    ({'SKOPAX_LOG_COLOR': 'never'}, False),
    ({'SKOPAX_LOG_COLOR': 'auto'}, False),
    ({'SKOPAX_LOG_COLOR': 'always', 'NO_COLOR': '1'}, False),
    ({'SKOPAX_LOG_COLOR': 'always', 'NO_COLOR': ''}, True),
    ({'NO_COLOR': 'yes'}, False),
])
def test_colour_follows_the_setting_and_no_color(kept_logging, env, painted):
    # given
    out = started(env)

    # when
    logging.getLogger('skopax.x').warning('hello')

    # then
    assert (ESC in out.getvalue()) is painted and out.getvalue().endswith('skopax.x' + ('\x1b[0m' if painted else '') + '  hello\n')


def test_auto_colour_is_on_only_for_a_terminal(kept_logging):
    # given
    tty = started({'SKOPAX_LOG_COLOR': 'auto'}, Tty())
    logging.getLogger('skopax.x').warning('to a terminal')
    pipe = started({'SKOPAX_LOG_COLOR': 'auto'})
    logging.getLogger('skopax.x').warning('to a pipe')
    quiet = started({'SKOPAX_LOG_COLOR': 'auto', 'NO_COLOR': '1'}, Tty())
    logging.getLogger('skopax.x').warning('to a quiet terminal')

    # then
    assert ESC in tty.getvalue() and ESC not in pipe.getvalue() and ESC not in quiet.getvalue()


@pytest.mark.parametrize('given, level', [('DEBUG', logging.DEBUG), ('info', logging.INFO), ('WARNING', logging.WARNING), ('WARN', logging.WARNING), (' error ', logging.ERROR)])
def test_the_level_setting_takes_the_names_and_warn(kept_logging, given, level):
    # given
    out = started({'SKOPAX_LOG_LEVEL': given, 'SKOPAX_LOG_COLOR': 'never'})

    # when
    logging.getLogger('skopax.x').debug('d')
    logging.getLogger('skopax.x').info('i')
    logging.getLogger('skopax.x').warning('w')
    logging.getLogger('skopax.x').error('e')

    # then
    assert logging.getLogger().level == level
    assert [line.rsplit('  ', 1)[1] for line in lines_of(out)] == [m for m, at in (('d', 10), ('i', 20), ('w', 30), ('e', 40)) if at >= level]


def test_bad_values_start_with_the_defaults_and_one_warning_naming_each(kept_logging):
    # given
    out = started({'SKOPAX_LOG_FORMAT': 'xml', 'SKOPAX_LOG_COLOR': 'rainbow', 'SKOPAX_LOG_LEVEL': 'LOUD'})

    # when
    setup({'SKOPAX_LOG_FORMAT': 'xml', 'SKOPAX_LOG_COLOR': 'rainbow', 'SKOPAX_LOG_LEVEL': 'LOUD'}, out)
    logging.getLogger('skopax.x').debug('hidden')
    warned = [line for line in lines_of(out) if 'WARN' in line]

    # then
    assert isinstance(logging.getLogger().handlers[0].formatter, TextFormatter) and logging.getLogger().level == logging.INFO
    assert len(warned) == 3 and all(ESC in line for line in warned)
    assert "SKOPAX_LOG_FORMAT is 'xml'" in warned[0] and "SKOPAX_LOG_COLOR is 'rainbow'" in warned[1] and "SKOPAX_LOG_LEVEL is 'LOUD'" in warned[2]
    assert 'using text' in warned[0] and 'using always' in warned[1] and 'using INFO' in warned[2]
    assert 'hidden' not in out.getvalue()


def test_the_settings_read_the_same_variables_and_warn_only_once_with_setup(kept_logging):
    # given
    env = {'SKOPAX_LOG_FORMAT': 'XML', 'SKOPAX_LOG_COLOR': 'never', 'SKOPAX_LOG_LEVEL': 'warn'}
    out = started(env)

    # when
    loaded = load_settings(env=env)
    quiet = load_settings(env={'NO_COLOR': '1', 'SKOPAX_LOG_FORMAT': 'json'})
    plain = load_settings(env={})

    # then
    assert (loaded.log_format, loaded.log_color, loaded.log_level) == ('text', 'never', 'WARNING')
    assert (quiet.log_format, quiet.log_color, quiet.log_level) == ('json', 'never', 'INFO')
    assert (plain.log_format, plain.log_color, plain.log_level) == ('text', 'always', 'INFO')
    assert out.getvalue().count("SKOPAX_LOG_FORMAT is 'XML'") == 1


def test_setup_routes_uvicorn_through_the_root_handler(kept_logging):
    # when
    setup({'SKOPAX_LOG_FORMAT': 'log4j'})

    # then
    root = logging.getLogger()
    assert len(root.handlers) == 1 and isinstance(root.handlers[0].formatter, Log4jFormatter)
    assert all(logging.getLogger(n).handlers == [] and logging.getLogger(n).propagate for n in ('uvicorn', 'uvicorn.error', 'uvicorn.access'))
    assert any(isinstance(f, SkipHealth) for f in logging.getLogger('uvicorn.access').filters)


def test_health_probes_are_dropped_from_the_access_log_and_the_api_is_kept():
    # given
    probe = record(logging.INFO, '%s - "%s %s HTTP/%s" %d', '10.0.0.1:1', 'GET', '/actuator/health/liveness', '1.1', 200, name='uvicorn.access')
    api = record(logging.INFO, '%s - "%s %s HTTP/%s" %d', '10.0.0.1:1', 'GET', '/api/state', '1.1', 200, name='uvicorn.access')

    # then
    assert SkipHealth().filter(probe) is False
    assert SkipHealth().filter(api) is True


def library_records() -> list[logging.LogRecord]:
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
    return [callback, sso, other, failed, rooted, own, named]


def test_oauth_queries_and_mcp_library_text_never_reach_the_log(kept_logging):
    # given
    callback, sso, other, failed, rooted, own, named = library_records()

    # when
    hide, quiet = HideQuery(), LibraryText()
    kept = [hide.filter(r) for r in (callback, sso, other)] + [quiet.filter(r) for r in (failed, rooted, own, named)]
    lines = [json.loads(Log4jFormatter().format(r)) for r in (callback, sso, other, failed, rooted, own, named)]
    setup({})

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


@pytest.mark.parametrize('form', ['text', 'json', 'log4j'])
def test_every_format_keeps_the_filters(kept_logging, form):
    # given
    out = started({'SKOPAX_LOG_FORMAT': form, 'SKOPAX_LOG_LEVEL': 'DEBUG'})
    callback, sso, other, failed, rooted, own, named = library_records()
    probe = record(logging.INFO, '%s - "%s %s HTTP/%s" %d', '10.0.0.1:1', 'GET', '/actuator/health/liveness', '1.1', 200, name='uvicorn.access')

    # when
    for r in (callback, sso, probe):
        logging.getLogger('uvicorn.access').handle(r)
    for r in (failed, rooted, named):
        logging.getLogger().handle(r)
    logging.getLogger('mcp.client').info('library chatter')
    logging.getLogger('httpx').info('library chatter')
    text = out.getvalue()

    # then
    assert 's3cr3t' not in text and 'st4te' not in text and 'liveness' not in text and 'library chatter' not in text
    assert 'GET /authorized HTTP/1.1' in text and 'ValueError at mcp/client/streamable_http.py:417' in text and f'{HIDDEN} at mcp/shared/session.py:383' in text
    assert len(lines_of(out)) == 5


def test_an_admin_value_swaps_the_format_and_level_of_the_next_record_in_place(kept_logging, store):
    # given
    out = started({'SKOPAX_LOG_COLOR': 'never'})
    handler = logging.getLogger().handlers[0]
    live = Live(load_settings(env={'SKOPAX_LOG_COLOR': 'never'}), TunableStore(store.db))
    live.current()
    logging.getLogger('skopax.x').info('before')

    # when
    live.set('core.log-format', 'json', 'admin@example.com')
    live.set('core.log-level', 'DEBUG', 'admin@example.com')
    logging.getLogger('skopax.x').debug('after')
    logging.getLogger('uvicorn.access').handle(library_records()[0])
    live.set('core.log-format', None, 'admin@example.com')
    live.set('core.log-level', 'ERROR', 'admin@example.com')
    logging.getLogger('skopax.x').warning('dropped')
    logging.getLogger('skopax.x').error('back')

    # then
    first, second, third, fourth = lines_of(out)
    assert first.endswith('INFO  skopax.x  before')
    assert json.loads(second) | {'time': '', 'thread': ''} == {'time': '', 'level': 'DEBUG', 'logger': 'skopax.x', 'message': 'after', 'thread': ''}
    assert json.loads(third)['message'] == '10.0.0.1:1 - "GET /api/admin/mcp-tools/oauth/callback HTTP/1.1" 303'
    assert fourth.endswith('ERROR skopax.x  back') and 'dropped' not in out.getvalue()
    assert logging.getLogger().handlers == [handler] and any(isinstance(f, LibraryText) for f in handler.filters)
    assert [(h['key'], h['old'], h['new']) for h in TunableStore(store.db).history()][-2:] == [('core.log-level', None, 'DEBUG'), ('core.log-format', None, 'json')]


def test_without_an_admin_value_the_environment_stays_and_no_color_wins_over_an_admin_colour(kept_logging, store):
    # given
    env = {'SKOPAX_LOG_FORMAT': 'json', 'SKOPAX_LOG_LEVEL': 'WARN', 'NO_COLOR': '1'}
    out = started(env)
    live = Live(load_settings(env=env), TunableStore(store.db))

    # when
    live.current()
    logging.getLogger('skopax.x').info('hidden')
    logging.getLogger('skopax.x').warning('kept')
    live.set('core.log-format', 'text', 'admin@example.com')
    live.set('core.log-color', 'always', 'admin@example.com')
    logging.getLogger('skopax.x').warning('plain')

    # then
    first, second = lines_of(out)
    assert json.loads(first)['message'] == 'kept' and logging.getLogger().level == logging.WARNING
    assert second.endswith('WARN  skopax.x  plain') and ESC not in out.getvalue()


def test_an_admin_colour_applies_when_no_color_is_empty(kept_logging, store):
    # given
    env = {'SKOPAX_LOG_COLOR': 'never', 'NO_COLOR': ''}
    out = started(env)
    live = Live(load_settings(env=env), TunableStore(store.db))

    # when
    live.current()
    live.set('core.log-color', 'always', 'admin@example.com')
    logging.getLogger('skopax.x').warning('painted')

    # then
    assert lines_of(out)[-1].endswith('\x1b[33mWARN\x1b[0m  \x1b[2mskopax.x\x1b[0m  painted')


def test_no_color_wins_over_a_colour_from_the_config_or_a_settings_source(kept_logging):
    # given
    config = lambda: {'core.log-color': 'always'}

    # when
    quiet = load_settings(config, env={'NO_COLOR': '1'})
    empty = load_settings(config, env={'NO_COLOR': ''})

    # then
    assert (quiet.log_color, empty.log_color) == ('never', 'always')


def test_at_debug_the_database_and_http_libraries_stay_quiet_and_skopax_speaks(kept_logging):
    # given
    out = started({'SKOPAX_LOG_LEVEL': 'INFO', 'SKOPAX_LOG_COLOR': 'never'})
    logging.getLogger('pymongo').setLevel(logging.NOTSET)
    booted = started({'SKOPAX_LOG_LEVEL': 'DEBUG', 'SKOPAX_LOG_COLOR': 'never'})

    # when
    logging.getLogger('pymongo.command').debug('{"pw": "scrypt$16384$8$1$c2FsdA$aGFzaA"}')
    logging.getLogger('skopax.x').debug('at boot')
    logs.use('text', 'never', 'INFO')
    logging.getLogger('pymongo').setLevel(logging.NOTSET)
    logs.use('text', 'never', 'DEBUG')
    for name in ('pymongo.command', 'pymongo.connection', 'pymongo.serverSelection', 'httpcore.http11', 'httpcore.connection', 'urllib3.connectionpool', 'anthropic._base_client', 'mcp.client', 'httpx'):
        logging.getLogger(name).debug('library %s', name)
        logging.getLogger(name).info('library %s', name)
    logging.getLogger('skopax.x').debug('live')

    # then
    assert out.getvalue() == ''
    assert [line.rsplit('  ', 1)[1] for line in lines_of(booted)] == ['at boot', 'live']
    assert all(logging.getLogger(n).level == logging.WARNING for n in ('pymongo', 'httpcore', 'urllib3', 'anthropic', 'mcp', 'httpx'))


def test_admin_values_leave_a_logging_setup_that_is_not_ours_alone(kept_logging, store):
    # given
    other = logging.StreamHandler(io.StringIO())
    logging.getLogger().handlers = [other]
    logging.getLogger().setLevel(logging.DEBUG)
    live = Live(settings(), TunableStore(store.db))

    # when
    live.set('core.log-level', 'ERROR', 'admin@example.com')

    # then
    assert logging.getLogger().handlers == [other] and other.formatter is None and logging.getLogger().level == logging.DEBUG


def test_the_log_settings_are_admin_choices_in_general(store):
    # given
    live = Live(settings(), TunableStore(store.db))

    # when
    items = {e['key']: e for e in live.entries()}

    # then
    assert [(items[k]['group'], items[k]['kind'], items[k]['choices']) for k in ('core.log-format', 'core.log-color', 'core.log-level')] == [
        ('General', 'choice', ['text', 'json', 'log4j']), ('General', 'choice', ['always', 'auto', 'never']), ('General', 'choice', ['DEBUG', 'INFO', 'WARNING', 'ERROR'])]


def test_a_settings_source_gives_the_log_settings_and_a_change_there_swaps_them_live(kept_logging, store, tmp_path, monkeypatch):
    # given
    env = {'DEMO_URL': 'https://demo.example.com', 'SKOPAX_LOG_COLOR': 'never'}
    out = started(env)
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'core.log-format': 'json', 'core.log-level': 'LOUD'}})
    b = boot(env, registry_with(demo_plugin))

    # when
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        runtime = c.app.state.runtime
        runtime.live.current()
        logging.getLogger('skopax.x').info('from the source')
        monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'core.log-format': 'log4j', 'core.log-level': 'WARNING'}})
        Follower(b.source_set(), runtime.live, runtime.instances, env).tick()
        logging.getLogger('skopax.x').info('hidden')
        logging.getLogger('skopax.x').warning('after the change')
    mine = [line for line in lines_of(out) if 'skopax.x' in line]

    # then
    assert (b.settings.log_format, b.settings.log_level) == ('json', 'INFO')
    assert out.getvalue().count("core.log-level is 'LOUD'") == 1
    assert len(mine) == 2 and 'hidden' not in out.getvalue()
    assert json.loads(mine[0])['message'] == 'from the source' and json.loads(mine[0])['logger'] == 'skopax.x'
    assert json.loads(mine[1])['message'] == 'after the change' and json.loads(mine[1])['loggerName'] == 'skopax.x'
