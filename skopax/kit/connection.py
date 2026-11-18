import ssl
import time
import urllib.error
from collections.abc import Callable

from skopax.contract import ReadError, SourceDown, TestResult
from skopax.errkind import kind
from skopax.messages import msg
from skopax.monitor import refusal
from skopax.redact import redact_text
from skopax.remote import HttpError, NotJson, Unreachable

CHAIN_MAX = 16


def causes(error: BaseException) -> list[BaseException]:
    found, todo = [], [error]
    while todo and len(found) < CHAIN_MAX:
        current = todo.pop(0)
        if any(current is seen for seen in found):
            continue
        found.append(current)
        reason = current.reason if isinstance(current, urllib.error.URLError) else None
        todo += [x for x in (current.__cause__, current.__context__, reason) if isinstance(x, BaseException)]
    return found


def status(error: HttpError) -> str:
    return refusal(error) or msg('connection.status', status=error.code)


def failure(error: Exception) -> str:
    if isinstance(error, SourceDown):
        return error.text
    if isinstance(error, ReadError):
        return redact_text(error.message)
    if isinstance(error, HttpError):
        return status(error)
    chain = causes(error)
    if any(isinstance(x, ssl.SSLError) for x in chain):
        return msg('connection.tls')
    if any(isinstance(x, TimeoutError) for x in chain):
        return msg('connection.timeout')
    if any(isinstance(x, NotJson) for x in chain):
        return msg('connection.not_api')
    if isinstance(error, Unreachable):
        return msg('connection.unreachable')
    wrapped = next((x for x in chain if isinstance(x, (HttpError, Unreachable))), None)
    if isinstance(wrapped, HttpError):
        return status(wrapped)
    if isinstance(wrapped, Unreachable):
        return msg('connection.unreachable')
    return msg('connection.failed', error=kind(error))


def result(run: Callable[[], str], clock: Callable[[], float] = time.monotonic) -> TestResult:
    started = clock()
    try:
        summary = run()
    except Exception as e:
        return TestResult(False, failure(e), int((clock() - started) * 1000))
    return TestResult(True, summary, int((clock() - started) * 1000))
