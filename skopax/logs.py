import importlib.util
import json
import logging
import os
import traceback
from pathlib import Path

LEVELS = {'WARNING': 'WARN', 'CRITICAL': 'FATAL'}
QUIET_PATHS = ('/actuator/health',)
HIDDEN_QUERIES = ('/api/admin/mcp-tools/oauth/callback', '/authorized')
LIBRARY = 'mcp'
QUIET_LIBRARIES = (LIBRARY, 'httpx')
HIDDEN = 'message hidden'
TRACE_LIMIT = 8000


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {'timeMillis': int(record.created * 1000), 'thread': record.threadName, 'level': LEVELS.get(record.levelname, record.levelname), 'loggerName': record.name,
               'message': record.getMessage(), 'endOfBatch': False, 'loggerFqcn': 'logging.Logger', 'threadId': record.thread, 'threadPriority': 5}
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None:
            doc['thrown'] = {'name': type(exc).__name__, 'message': str(exc), 'extendedStackTrace': ''.join(traceback.format_exception(*record.exc_info))[-TRACE_LIMIT:]}
            cause = exc.__cause__ or exc.__context__
            if cause is not None:
                doc['thrown']['cause'] = {'name': type(cause).__name__, 'message': str(cause)}
        return json.dumps(doc, ensure_ascii=False, default=str)


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


def setup(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(LibraryText())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    logging.getLogger('uvicorn.access').addFilter(SkipHealth())
    logging.getLogger('uvicorn.access').addFilter(HideQuery())
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
