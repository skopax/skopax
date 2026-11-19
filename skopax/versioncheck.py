import calendar
import hashlib
import logging
import re
import time
from typing import Callable

from skopax import brand, __version__
from skopax.errkind import kind as error_kind
from skopax.messages import msg
from skopax.remote import Transport, call, send

log = logging.getLogger('skopax.versioncheck')

API = 'https://api.github.com/repos/skopax/skopax/releases'
LIST_URL = f'{API}?per_page=20'
RELEASE_HOST = 'https://github.com/'
PERIOD_S = 86400
OFFSET_S = 3 * 3600
JITTER_S = 600
START_DELAY_S = 45
FRESH_S = 20 * 3600
TIMEOUT_S = 10.0
BODY_LIMIT = 4 * 1024 * 1024
NUMBER = '(0|[1-9][0-9]*)'
TAG = re.compile(f'v{NUMBER}\\.{NUMBER}\\.{NUMBER}')
PLAIN = re.compile(f'{NUMBER}\\.{NUMBER}\\.{NUMBER}')
SECURITY_PREFIX = 'Security:'
UPDATE_ID = 'update'
Version = tuple[int, int, int]


def parse_tag(tag) -> Version | None:
    found = TAG.fullmatch(tag) if isinstance(tag, str) else None
    return tuple(int(n) for n in found.groups()) if found else None


def parse_running(version) -> Version | None:
    found = PLAIN.fullmatch(version) if isinstance(version, str) else None
    return tuple(int(n) for n in found.groups()) if found else None


def dotted(version: Version) -> str:
    return '.'.join(str(n) for n in version)


def is_security(body) -> bool:
    return isinstance(body, str) and any(line.startswith(SECURITY_PREFIX) for line in body.splitlines())


def usable(release) -> tuple[Version, dict] | None:
    if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
        return None
    parsed = parse_tag(release.get('tag_name'))
    url = release.get('html_url')
    if parsed is None or not isinstance(url, str) or not url.startswith(RELEASE_HOST):
        return None
    return parsed, release


def jitter_of(seed: str) -> int:
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16) % JITTER_S


class VersionCheck:
    def __init__(self, store, transport: Transport | None = None, enabled: Callable[[], bool] = lambda: True, version: str = __version__, clock: Callable[[], float] = time.time):
        self.store = store
        self.transport = transport or send
        self.enabled = enabled
        self.running = parse_running(version)
        self.agent = f'Skopax/{brand.display_version(version)}'
        self.clock = clock

    def get(self, url: str):
        return call(self.transport, 'GET', url, {'Accept': 'application/vnd.github+json', 'User-Agent': self.agent}, timeout=TIMEOUT_S, limit=BODY_LIMIT).body

    def run_once(self, now: float | None = None) -> dict | None:
        if not self.enabled() or self.running is None:
            return None
        try:
            found = self.look()
            if found is None:
                return None
            found['at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.clock() if now is None else now))
            self.store.note_version_check(found)
            return found
        except Exception as e:
            log.debug('the new-version check did not finish: %s', error_kind(e))
            return None

    def look(self) -> dict | None:
        listed = self.get(LIST_URL)
        if not isinstance(listed, list):
            return None
        found = sorted(((v, r) for v, r in filter(None, (usable(x) for x in listed))), key=lambda pair: pair[0])
        if not found:
            return None
        version, release = found[-1]
        return {'latest': dotted(version), 'url': release['html_url'], 'securityIn': [dotted(v) for v, r in found if is_security(r.get('body'))]}

    def fresh(self) -> bool:
        try:
            at = (self.store.version_check() or {}).get('at')
            return isinstance(at, str) and self.clock() - calendar.timegm(time.strptime(at, '%Y-%m-%dT%H:%M:%SZ')) <= FRESH_S
        except Exception:
            return False

    def scheduled(self, now: float | None = None) -> dict | None:
        return None if self.fresh() else self.run_once(now)

    def start(self) -> dict | None:
        if not self.due_at_start():
            return None
        try:
            if not self.store.claim_run(f'version-check:start:{int(self.clock() // FRESH_S)}'):
                return None
        except Exception as e:
            log.debug('the new-version start check was not claimed: %s', error_kind(e))
            return None
        return self.run_once()

    def due_at_start(self) -> bool:
        return self.enabled() and self.running is not None and not self.fresh()

    def notice(self) -> dict | None:
        if not self.enabled() or self.running is None:
            return None
        found = self.store.version_check()
        latest = parse_running((found or {}).get('latest'))
        url = (found or {}).get('url')
        if latest is None or latest <= self.running or not isinstance(url, str) or not url.startswith(RELEASE_HOST):
            return None
        listed = (found or {}).get('securityIn')
        warn = any(v is not None and v > self.running for v in (parse_running(x) for x in (listed if isinstance(listed, list) else [])))
        shown = dict(upstream=brand.DEFAULT_NAME, latest=dotted(latest), current=dotted(self.running))
        return {'id': UPDATE_ID, 'level': 'warn' if warn else 'info', 'text': msg('update.security' if warn else 'update.available', **shown),
                'href': url, 'linkText': msg('update.link')}
