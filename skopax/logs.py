import importlib.util
import json
import logging
import os
import threading
import time
import traceback
from collections.abc import Mapping
from pathlib import Path

LEVELS = {'WARNING': 'WARN', 'CRITICAL': 'FATAL'}
QUIET_PATHS = ('/actuator/health',)
HIDDEN_QUERIES = ('/api/admin/mcp-tools/oauth/callback', '/authorized')
LIBRARY = 'mcp'
QUIET_LIBRARIES = (LIBRARY, 'httpx', 'httpcore', 'pymongo', 'urllib3', 'anthropic')
HIDDEN = 'message hidden'
TRACE_LIMIT = 8000
FORMAT_ENV, COLOR_ENV, LEVEL_ENV, NO_COLOR_ENV = 'SKOPAX_LOG_FORMAT', 'SKOPAX_LOG_COLOR', 'SKOPAX_LOG_LEVEL', 'NO_COLOR'
TEXT, JSON, LOG4J = 'text', 'json', 'log4j'
FORMATS = (TEXT, JSON, LOG4J)
ALWAYS, AUTO, NEVER = 'always', 'auto', 'never'
COLORS = (ALWAYS, AUTO, NEVER)
LEVEL_NAMES = ('DEBUG', 'INFO', 'WARNING', 'ERROR')
DEFAULT_LEVEL = 'INFO'
LEVEL_ALIASES = {'WARN': 'WARNING'}
VALUE_SHOWN = 40
RESET = '\x1b[0m'
DIM = '\x1b[2m'
PAINT = {'DEBUG': DIM, 'INFO': '\x1b[32m', 'WARN': '\x1b[33m', 'ERROR': '\x1b[1;31m', 'FATAL': '\x1b[1;31m'}
LEVEL_WIDTH = 5
CONTROL = {c: f'\\x{c:02x}' for c in (*range(0x20), *range(0x7f, 0xa0)) if c != 0x09} | {0x0a: '\\n', 0x0d: '\\r'}
TRACE_CONTROL = {c: v for c, v in CONTROL.items() if c != 0x0a}
HELD: list = [None]
APPLIED: list = [None]
PLAIN: list = [False]
WARNED: set = set()
GUARD = threading.Lock()

log = logging.getLogger('skopax.logs')


def level_name(record: logging.LogRecord) -> str:
    return LEVELS.get(record.levelname, record.levelname)


def stamp(record: logging.LogRecord) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created)) + '.%03dZ' % int(record.msecs)


def trace_of(record: logging.LogRecord) -> str:
    return ''.join(traceback.format_exception(*record.exc_info))[-TRACE_LIMIT:]


def thrown(record: logging.LogRecord) -> BaseException | None:
    return record.exc_info[1] if record.exc_info else None


class Log4jFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {'timeMillis': int(record.created * 1000), 'thread': record.threadName, 'level': level_name(record), 'loggerName': record.name,
               'message': record.getMessage(), 'endOfBatch': False, 'loggerFqcn': 'logging.Logger', 'threadId': record.thread, 'threadPriority': 5}
        exc = thrown(record)
        if exc is not None:
            doc['thrown'] = {'name': type(exc).__name__, 'message': str(exc), 'extendedStackTrace': trace_of(record)}
            cause = exc.__cause__ or exc.__context__
            if cause is not None:
                doc['thrown']['cause'] = {'name': type(cause).__name__, 'message': str(cause)}
        return json.dumps(doc, ensure_ascii=False, default=str)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {'time': stamp(record), 'level': level_name(record), 'logger': record.name, 'message': record.getMessage(), 'thread': record.threadName}
        exc = thrown(record)
        if exc is not None:
            doc['error'] = {'type': type(exc).__name__, 'message': str(exc), 'trace': trace_of(record)}
            cause = exc.__cause__ or exc.__context__
            if cause is not None:
                doc['error']['cause'] = {'type': type(cause).__name__, 'message': str(cause)}
        return json.dumps(doc, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self, color: bool = False):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        level = level_name(record)
        pad = ' ' * max(0, LEVEL_WIDTH - len(level))
        name = record.name.translate(CONTROL)
        if self.color:
            level, name = f'{PAINT.get(level, "")}{level}{RESET}', f'{DIM}{name}{RESET}'
        line = f'{stamp(record)} {level}{pad} {name}  {record.getMessage().translate(CONTROL)}'
        if thrown(record) is None:
            return line
        return line + '\n' + trace_of(record).rstrip('\n').translate(TRACE_CONTROL)


def normal(text: str, allowed: tuple[str, ...]) -> str:
    return LEVEL_ALIASES.get(text.upper(), text.upper()) if allowed is LEVEL_NAMES else text.lower()


def chosen(name: str, raw, allowed: tuple[str, ...], default: str) -> str:
    text = str(raw if raw is not None else '').strip()
    if not text:
        return default
    if normal(text, allowed) in allowed:
        return normal(text, allowed)
    with GUARD:
        first = (name, text) not in WARNED
        WARNED.add((name, text))
    if first:
        log.warning('%s is %r, not one of %s; using %s', name, text[:VALUE_SHOWN], ', '.join(allowed), default)
    return default


BOOT = ((FORMAT_ENV, FORMATS, TEXT), (COLOR_ENV, COLORS, ALWAYS), (LEVEL_ENV, LEVEL_NAMES, DEFAULT_LEVEL))


def boot_values(env: Mapping[str, str]) -> tuple[str, str, str]:
    picked = []
    for name, allowed, default in BOOT:
        value = normal(str(env.get(name) or '').strip(), allowed)
        picked.append(value if value in allowed else default)
    form, color, level = picked
    return form, NEVER if env.get(NO_COLOR_ENV) else color, level


def painted(color: str, stream) -> bool:
    if color == AUTO:
        try:
            return bool(stream.isatty())
        except (AttributeError, OSError, ValueError):
            return False
    return color != NEVER


def formatter_for(form: str, color: str, stream) -> logging.Formatter:
    if form == JSON:
        return JsonFormatter()
    if form == LOG4J:
        return Log4jFormatter()
    return TextFormatter(color=painted(color, stream))


def level_of(name: str) -> int:
    return getattr(logging, name) if name in LEVEL_NAMES else logging.INFO


def pin_libraries() -> None:
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


def use(form: str, color: str, level: str) -> None:
    root = logging.getLogger()
    with GUARD:
        handler = HELD[0]
        if handler is not None and handler in root.handlers:
            pin_libraries()
        color = NEVER if PLAIN[0] else color
        wanted = (form, color, level)
        if handler is None or handler not in root.handlers or APPLIED[0] == wanted:
            return
        made = formatter_for(form if form in FORMATS else TEXT, color if color in COLORS else ALWAYS, handler.stream)
        handler.acquire()
        try:
            handler.setFormatter(made)
        finally:
            handler.release()
        root.setLevel(level_of(level))
        APPLIED[0] = wanted


class SkipHealth(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        path = str(args[2]) if len(args) > 2 else ''
        return not path.startswith(QUIET_PATHS)


class HideQuery(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        path = str(args[2]) if len(args) > 2 else ''
        bare = path.split('?', 1)[0]
        if bare != path and bare in HIDDEN_QUERIES:
            record.args = args[:2] + (bare,) + args[3:]
        return True


def library_home() -> str | None:
    found = importlib.util.find_spec(LIBRARY)
    return str(Path(found.origin).parent) + os.sep if found is not None and found.origin else None


class LibraryText(logging.Filter):
    def __init__(self):
        super().__init__()
        self.home = library_home()

    def filter(self, record: logging.LogRecord) -> bool:
        named = record.name == LIBRARY or record.name.startswith(LIBRARY + '.')
        placed = self.home is not None and str(record.pathname).startswith(self.home)
        if named or placed:
            exc = record.exc_info[1] if record.exc_info else None
            record.msg, record.args = '%s at %s:%d', (type(exc).__name__ if exc is not None else HIDDEN, self.where(record, placed), record.lineno)
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True

    def where(self, record: logging.LogRecord, placed: bool) -> str:
        if not placed or self.home is None:
            return record.filename
        return LIBRARY + '/' + str(record.pathname)[len(self.home):].replace(os.sep, '/')


def setup(env: Mapping[str, str] | None = None, stream=None) -> None:
    env = os.environ if env is None else env
    form, color, level = boot_values(env)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter_for(form, color, handler.stream))
    handler.addFilter(LibraryText())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level_of(level))
    with GUARD:
        HELD[0], APPLIED[0], PLAIN[0] = handler, (form, color, level), bool(env.get(NO_COLOR_ENV))
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    logging.getLogger('uvicorn.access').addFilter(SkipHealth())
    logging.getLogger('uvicorn.access').addFilter(HideQuery())
    pin_libraries()
    for name, allowed, default in BOOT:
        chosen(name, env.get(name), allowed, default)
