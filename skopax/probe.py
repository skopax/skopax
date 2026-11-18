import logging
import queue
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlsplit

from cryptography import x509

from skopax import clock, remote
from skopax.engine import PassResult, anomaly, iso, parse_iso
from skopax.errkind import kind
from skopax.messages import msg

log = logging.getLogger('skopax.probe')

PASS = 'certs'
TIMEOUT = 8
ERROR_LIMIT = 160
WORKERS = 8
CERT_RULES = {'warn_days': 21, 'crit_days': 7}
DAY_S = 86400
THREAD = 'probe'
UNTRUSTED = 'tls: certificate not trusted'
EXPIRED = 'tls: certificate expired'
EXPIRED_CODE = 10
RENEW_DAYS = 30
SKIP_ENTRY = re.compile(r'(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?', re.IGNORECASE)
SKIP_ENTRY_MAX = 253
TIMED_OUT = 'timeout'
BAD_URL = 'url'
NAMED = ('name', 'plugin', 'instance', 'url')
PUBLIC = ('name', 'plugin', 'instance', 'ok', 'status', 'ms', 'certDays')


def http_status(url: str, context: ssl.SSLContext | None, timeout: float, opener: Callable = urllib.request.urlopen) -> int:
    req = urllib.request.Request(url, headers={'Accept': '*/*'}, method='GET')
    try:
        with opener(req, timeout=timeout, context=context) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        if isinstance(e.reason, BaseException):
            raise e.reason from None
        raise


def cert_expiry(host: str, port: int, timeout: float, connect: Callable = socket.create_connection) -> str | None:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with connect((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    return iso(x509.load_der_x509_certificate(der).not_valid_after_utc.timestamp()) if der else None


def pinned(pem: str, as_is: bool = False) -> ssl.SSLContext:
    return remote.tls_context(pem, as_is)


def blank(target: dict) -> dict:
    return {**{k: str(target.get(k) or '') for k in NAMED}, 'ok': False, 'status': None, 'ms': 0, 'error': None, 'certDays': None, 'certExpires': None}


def check(target: dict, timeout: float | None = None, resolve: Callable = socket.getaddrinfo, connect: Callable = socket.create_connection,
          fetch: Callable = http_status, cert: Callable = cert_expiry) -> dict:
    limit = TIMEOUT if timeout is None else timeout
    out = blank(target)
    start = time.monotonic()

    def done(error: str | None = None) -> dict:
        out['error'] = error[:ERROR_LIMIT] if error else None
        out['ms'] = round((time.monotonic() - start) * 1000)
        return out

    try:
        parts = urlsplit(out['url'])
        host, tls = parts.hostname, parts.scheme == 'https'
        port = parts.port or (443 if tls else 80)
    except ValueError:
        return done(BAD_URL)
    if parts.scheme not in ('http', 'https') or not host:
        return done(BAD_URL)
    try:
        resolve(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return done(f'dns: {kind(e)}')
    try:
        connect((host, port), timeout=limit).close()
    except OSError as e:
        return done(f'tcp: {kind(e)}')
    context, expires = None, None
    if tls:
        try:
            expires = cert(host, port, limit, connect)
        except Exception as e:
            log.warning('%s', msg('probe.cert_unread', host=host, error=kind(e)))
            expires = None
        pem = target.get('pem')
        try:
            context = pinned(pem, target.get('pinned') is True) if isinstance(pem, str) and pem else ssl.create_default_context()
        except (ssl.SSLError, ValueError) as e:
            return done(f'tls: {kind(e)}')
    try:
        status = int(fetch(out['url'], context, limit))
    except ssl.SSLCertVerificationError as e:
        if getattr(e, 'verify_code', None) == EXPIRED_CODE:
            out['certExpires'] = expired_at(expires)
            return done(EXPIRED)
        return done(UNTRUSTED)
    except ssl.SSLError as e:
        return done(f'tls: {kind(e)}')
    except Exception as e:
        return done(f'http: {kind(e)}')
    out.update(status=status, ok=status < 500, certExpires=expires if tls else None)
    return done(None if status < 500 else f'http {status}')


def expired_at(expires: str | None) -> str:
    now = time.time()
    try:
        return expires if isinstance(expires, str) and parse_iso(expires) <= now else iso(now)
    except ValueError:
        return iso(now)


def shaped(row: object, target: dict) -> dict:
    out = blank(target)
    if not isinstance(row, dict):
        out['error'] = 'probe: no result'
        return out
    out.update({k: row[k] for k in out if k in row and k not in NAMED})
    return out


def finish(row: dict, now: float) -> dict:
    expires = row.get('certExpires')
    row['certDays'] = None
    if isinstance(expires, str) and expires:
        try:
            row['certDays'] = round((parse_iso(expires) - now) / DAY_S, 1)
        except ValueError:
            row['certExpires'] = None
    else:
        row['certExpires'] = None
    return row


class Probe:
    def __init__(self, targets: Callable[[], list[dict]], timeout: float | None = None, workers: int = WORKERS, checker: Callable[[dict, float], dict] = check):
        self.targets = targets
        self.timeout = timeout
        self.workers = max(1, workers)
        self.checker = checker

    def run(self, now: float) -> dict:
        limit = TIMEOUT if self.timeout is None else self.timeout
        listed = [t for t in self.targets() or [] if isinstance(t, dict)]
        rows = [blank(t) for t in listed]
        finished: queue.SimpleQueue = queue.SimpleQueue()
        waiting = list(range(len(listed)))
        running: dict[int, float] = {}
        while waiting or running:
            while waiting and len(running) < self.workers:
                i = waiting.pop(0)
                running[i] = time.monotonic() + limit
                threading.Thread(target=self._one, args=(i, listed[i], limit, finished), name=THREAD, daemon=True).start()
            try:
                i, row = finished.get(timeout=max(0.0, min(running.values()) - time.monotonic()))
            except queue.Empty:
                late = time.monotonic()
                for i in [i for i, deadline in running.items() if deadline <= late]:
                    del running[i]
                    rows[i].update(error=TIMED_OUT, ms=round(limit * 1000))
                continue
            if running.pop(i, None) is not None:
                rows[i] = row
        return {'at': iso(now), 'targets': [finish(r, now) for r in rows]}

    def _one(self, i: int, target: dict, limit: float, finished: queue.SimpleQueue) -> None:
        try:
            row = shaped(self.checker(target, limit), target)
        except Exception as e:
            row = {**blank(target), 'error': f'probe: {kind(e)}'}
        finished.put((i, row))


def public(report: dict) -> dict:
    rows = report.get('targets') if isinstance(report, dict) else None
    return {'at': report.get('at') if isinstance(report, dict) else None, 'targets': [{k: r.get(k) for k in PUBLIC} for r in rows or [] if isinstance(r, dict)]}


def summary(report: dict) -> str:
    return '; '.join(f"{r.get('name')} ({r.get('instance')}) " + (f"ok {r.get('status')}" if r.get('ok') else str(r.get('error') or 'failed'))
                     for r in report.get('targets') or [] if isinstance(r, dict))


def skip_entry_ok(entry: str) -> bool:
    name = entry.rstrip('.')
    return len(name) <= SKIP_ENTRY_MAX and bool(SKIP_ENTRY.fullmatch(name))


def skipped(host: str, skip: tuple[str, ...]) -> bool:
    for s in (str(x).strip().lower().rstrip('.') for x in skip):
        if s.startswith('*.') and host.endswith(s[1:]):
            return True
        if s and not s.startswith('*.') and (host == s or host.endswith('.' + s)):
            return True
    return False


def cert_pass(report: dict, now: float, rules: dict, skip: tuple[str, ...] = ()) -> list[PassResult]:
    expires: dict[str, float] = {}
    seen: set[str] = set()
    gone: set[str] = set()
    for r in report.get('targets') or []:
        if not isinstance(r, dict) or not r.get('certExpires'):
            continue
        try:
            host = (urlsplit(str(r.get('url') or '')).hostname or '').lower()
            t = parse_iso(str(r['certExpires']))
        except ValueError:
            continue
        if not host:
            continue
        seen.add(host)
        if not skipped(host, skip):
            expires[host] = min(t, expires.get(host, t))
            if r.get('error') == EXPIRED:
                gone.add(host)
    found = []
    for host, t in sorted(expires.items()):
        days = (t - now) / DAY_S
        expired = days < 0 or host in gone
        if days >= rules['warn_days'] and not expired:
            continue
        sev = 'crit' if expired or days < rules['crit_days'] else 'warn'
        when = (msg('certs.has_expired') if days > -1 else msg('certs.expired', days=round(-days, 1))) if expired else msg('certs.expires_in', days=round(days, 1))
        tail = msg('certs.refused') if expired else msg('certs.renewal_failing', days=RENEW_DAYS) if days < RENEW_DAYS else msg('certs.will_stop')
        x = anomaly(PASS, 'cert-expiry', '-', host, 'tls', sev, round(days, 1), None, None, '', msg('certs.label'),
                    msg('certs.what', host=host, when=when, at=f'{clock.date(t)} {clock.label(t)}', tail=tail), 1, msg('certs.short', sev=sev.upper(), host=host, when=when))
        x['ranKey'] = f'{PASS}:{host}'
        found.append(x)
    return [PassResult(PASS, True, 'ok', found, {}, tuple(f'{PASS}:{h}' for h in sorted(seen)))]
