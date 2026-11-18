import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, Protocol

PLUGIN_ID = 'mcp-tools'
PREFIX = 'mcp_'
KNOWLEDGE_SECTION = 'knowledge-sources'

CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30
CALL_DEADLINE_S = 45
TEARDOWN_S = 2
CALL_THREAD_S = 50
LIST_DEADLINE_S = 20
RUN_LIST_DEADLINE_S = 10
LIVE_CACHE_S = 60
TEST_DEADLINE_S = 12
OAUTH_DEADLINE_S = 15
START_DEADLINE_S = 30
RESPONSE_BYTES_MAX = 2 * 1024 * 1024
LIST_PAGES_MAX = 10
TOOLS_MAX = 500
ON_MAX = 40
RUN_TOOLS_MAX = 60
RUN_DEFINITIONS_MAX = 60000
DESCRIPTION_MAX = 1000
PARAM_DESCRIPTION_MAX = 300
SCHEMA_MAX = 8000
OUTPUT_MAX = 20000
PER_TOOL_CALLS = 8
RUN_CALLS_MAX = 20
RUN_OUTPUT_MAX = 80000
ERROR_TEXT_MAX = 300
SERVER_NAME_MAX = 60
LABEL_MAX = 80
FOLDERS_MAX = 50
FOLDERS_TTL_S = 900
STATE_TTL_S = 600
STATES_PER_INSTANCE = 5
REFRESH_EARLY_S = 300
RETRY_AFTER_S = 60
RETRY_AFTER_MAX_S = 3600
SECRET_FIELDS = ('header-value', 'oauth-client-secret')
SECRET_PART_MIN = 8
MASK = '***'

State = Literal['none', 'connected', 'reconnect']
PARK_REASONS = frozenset({'moved', 'lost', 'expired', 'unreadable', 'client_changed'})
DEADLINE: ContextVar[float | None] = ContextVar('mcptools_deadline', default=None)


class Reconnect(Exception):
    pass


class Moved(Reconnect):
    pass


class TokenSource(Protocol):
    def state(self, name: str) -> State: ...

    def token(self, name: str, instance: Mapping[str, str]) -> str: ...

    def after_unauthorized(self, name: str, instance: Mapping[str, str]) -> str: ...

    def forget(self, name: str) -> None: ...

    def secrets(self, name: str) -> tuple[str, ...]: ...

    def park(self, name: str, reason: str) -> None: ...

    def moved(self, name: str, url: str) -> bool: ...


class NoTokens:
    def state(self, name: str) -> State:
        return 'none'

    def token(self, name: str, instance: Mapping[str, str]) -> str:
        raise Reconnect()

    def after_unauthorized(self, name: str, instance: Mapping[str, str]) -> str:
        raise Reconnect()

    def forget(self, name: str) -> None:
        return None

    def secrets(self, name: str) -> tuple[str, ...]:
        return ()

    def park(self, name: str, reason: str) -> None:
        return None

    def moved(self, name: str, url: str) -> bool:
        return False


def secrets_of(values: Mapping[str, str], tokens: Iterable[str] = ()) -> tuple[str, ...]:
    found: set[str] = set(tokens)
    for key in SECRET_FIELDS:
        value = values.get(key) or ''
        found.add(value)
        found.update(part for part in value.split() if len(part) >= SECRET_PART_MIN)
    return tuple(sorted((s for s in found if s), key=lambda s: (-len(s), s)))


def scrub(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


@contextmanager
def within(seconds: float) -> Iterator[None]:
    mark = DEADLINE.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        DEADLINE.reset(mark)


def left(cap: float) -> float:
    at = DEADLINE.get()
    return cap if at is None else max(0.0, min(cap, at - time.monotonic()))
