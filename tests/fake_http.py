import json
from urllib.parse import parse_qs, urlsplit

from skopax.remote import MARKER, PAGE_READ, PIN, HttpError, Reply


class FakeHttp:
    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str, dict, object]] = []

    def __call__(self, method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> Reply:
        body = json.loads(data) if data else None
        self.calls.append((method, url, headers, body))
        path = url.split('?')[0]
        for (m, prefix), answer in self.routes.items():
            if m == method and path == prefix:
                got = answer(parse_qs(urlsplit(url).query), body) if callable(answer) else answer
                if isinstance(got, Exception):
                    raise got
                if isinstance(got, Reply):
                    return got
                return Reply(got, {})
        raise HttpError(404, path)

    def hits(self, prefix: str) -> list[tuple[str, str, dict, object]]:
        return [c for c in self.calls if c[1].split('?')[0] == prefix]


class FakePages:
    def __init__(self, routes: dict | None = None, clock=None, took: float = 0.0):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, dict, float]] = []
        self.marks: list[str | None] = []
        self.pins: list[str | None] = []
        self.clock = clock
        self.took = took

    def __call__(self, method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> Reply:
        self.marks.append(headers.get(PAGE_READ))
        self.pins.append(headers.get(PIN))
        self.calls.append((url, {k: v for k, v in headers.items() if not k.lower().startswith(MARKER)}, timeout))
        if self.clock is not None:
            self.clock.now += self.took
        if url not in self.routes:
            raise AssertionError(f'unexpected fetch of {url}')
        got = self.routes[url]
        if isinstance(got, BaseException):
            raise got
        return got

    def urls(self) -> list[str]:
        return [c[0] for c in self.calls]
