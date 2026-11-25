import base64
import ipaddress
import json
import threading
from dataclasses import dataclass, field

from websockets.sync.server import serve

JPEG = b'\xff\xd8\xff\xe0' + b'fake-jpeg-body' * 40 + b'\xff\xd9'
MAIN = 'F-MAIN'
SUB = 'F-SUB'
WS_PATH = '/devtools/browser/fake-1'


@dataclass
class Req:
    url: str
    type: str = 'Script'
    method: str = 'GET'
    frame: str = MAIN


@dataclass
class Site:
    hops: list[str] = field(default_factory=list)
    status: int = 200
    title: str = 'Garden Chairs | shop.example.com'
    height: int = 3400
    subs: list[Req] = field(default_factory=list)
    after_load: list[str] = field(default_factory=list)
    error: str | None = None
    headers: dict = field(default_factory=dict)
    probe: dict | None = None
    focus: dict | None = None
    mark: dict | None = None
    overflow: dict | None = None
    scroll: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)
    dialogs: list[str] = field(default_factory=list)
    ip: str = '93.184.216.34'
    frames: list[tuple[str, str]] = field(default_factory=list)
    hang: bool = False


def ip_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host.rsplit(':', 1)[0].strip('[]'))
        return True
    except ValueError:
        return False


class FakeChrome:
    def __init__(self, sites: dict[str, Site] | None = None, version: str = 'Chrome/157.0.8103.15', fail_methods: frozenset = frozenset(), fail_from: dict | None = None,
                 answers: dict | None = None):
        self.sites = sites or {}
        self.version = version
        self.fail_methods = fail_methods
        self.fail_from = dict(fail_from or {})
        self.answers = dict(answers or {})
        self.counts: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []
        self.decisions: dict[str, str] = {}
        self.hosts: list[str] = []
        self.disposed: list[str] = []
        self.paused: dict[str, tuple[str, str]] = {}
        self.forwarded: dict[str, dict[str, str]] = {}
        self.lock = threading.Lock()
        self.server = serve(self._handler, '127.0.0.1', 0, process_request=self._http)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f'http://browser.test:{self.port}'

    def close(self) -> None:
        self.server.shutdown()

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def params(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]

    def _http(self, connection, request):
        host = request.headers.get('Host', '')
        self.hosts.append(host)
        if not ip_host(host):
            return connection.respond(500, 'Host header is specified and is not an IP address or localhost.')
        if request.path == '/json/version':
            return connection.respond(200, json.dumps({'Browser': self.version, 'Protocol-Version': '1.3', 'webSocketDebuggerUrl': f'ws://127.0.0.1:9223{WS_PATH}'}))
        if request.path != WS_PATH:
            return connection.respond(404, 'no such target')
        return None

    def _handler(self, ws):
        nav: dict = {}
        for raw in ws:
            m = json.loads(raw)
            method, params, mid, sid = m['method'], m.get('params') or {}, m['id'], m.get('sessionId')
            with self.lock:
                self.calls.append((method, params))

            def reply(result=None, error=None):
                body = {'id': mid, **({'error': error} if error else {'result': result or {}})}
                ws.send(json.dumps(body | ({'sessionId': sid} if sid else {})))

            self.counts[method] = self.counts.get(method, 0) + 1
            if method in self.fail_methods or self.counts[method] >= self.fail_from.get(method, self.counts[method] + 1):
                reply(error={'code': -32000, 'message': f'{method} failed'})
            elif method in self.answers:
                reply(self.answers[method])
            elif method == 'Target.createBrowserContext':
                reply({'browserContextId': 'ctx-1'})
            elif method == 'Target.createTarget':
                reply({'targetId': 'tg-1'})
            elif method == 'Target.attachToTarget':
                reply({'sessionId': 'S1'})
            elif method == 'Target.disposeBrowserContext':
                self.disposed.append(params.get('browserContextId'))
                reply()
            elif method == 'Page.getFrameTree':
                reply({'frameTree': {'frame': {'id': MAIN, 'url': 'about:blank'}}})
            elif method == 'Page.navigate':
                site = self.sites.get(params['url']) or Site()
                nav.clear()
                nav.update(id=mid, site=site, chain=[params['url'], *site.hops], pending=0, url=params['url'])
                if not site.hang:
                    self._pause(ws, 'doc-0', params['url'], 'Document', MAIN, None)
            elif method in ('Fetch.continueRequest', 'Fetch.failRequest'):
                if 'headers' in params:
                    self.forwarded[self.paused.get(params['requestId'], ('?', '?'))[0]] = {h['name']: h['value'] for h in params['headers']}
                reply()
                self._decided(ws, nav, params['requestId'], method == 'Fetch.continueRequest')
            elif method == 'Runtime.evaluate':
                expression = str(params.get('expression') or '')
                if expression.startswith('((needle') and nav:
                    reply({'result': {'type': 'object', 'value': nav['site'].focus}})
                elif expression.startswith('((marks)') and nav:
                    reply({'result': {'type': 'object', 'value': nav['site'].mark}})
                elif expression.startswith('((limit)') and nav:
                    reply({'result': {'type': 'object', 'value': nav['site'].overflow}})
                elif expression.startswith('((box)') and nav:
                    reply({'result': {'type': 'object', 'value': nav['site'].names.get(int(expression.rsplit('(', 1)[1].rstrip(')')))}})
                elif expression.startswith('((index)') and nav:
                    found = nav['site'].scroll.get(int(expression.rsplit('(', 1)[1].rstrip(')')))
                    reply(error={'code': -32000, 'message': 'Runtime.evaluate failed'}) if found == 'error' else reply({'result': {'type': 'object', 'value': found}})
                elif params.get('expression') != 'document.title' and nav and nav['site'].probe is not None:
                    reply({'result': {'type': 'object', 'value': nav['site'].probe}})
                else:
                    reply({'result': {'type': 'string', 'value': nav['site'].title if nav else ''}})
            elif method == 'Page.getLayoutMetrics':
                height = nav['site'].height if nav else 800
                reply({'cssContentSize': {'x': 0, 'y': 0, 'width': 1280, 'height': height}, 'cssLayoutViewport': {'pageX': 0, 'pageY': 0, 'clientWidth': 1280, 'clientHeight': 800}})
            elif method == 'Page.captureScreenshot':
                reply({'data': base64.b64encode(JPEG).decode()})
            else:
                reply()

    def _event(self, ws, method: str, params: dict) -> None:
        ws.send(json.dumps({'method': method, 'params': params, 'sessionId': 'S1'}))

    def _pause(self, ws, rid: str, url: str, rtype: str, frame: str, redirected: str | None, method: str = 'GET', headers: dict | None = None) -> None:
        self.paused[rid] = (url, rtype)
        self._event(ws, 'Fetch.requestPaused', {'requestId': rid, 'request': {'url': url, 'method': method, 'headers': headers or {'User-Agent': 'fake'}}, 'frameId': frame,
                                                'resourceType': rtype,
                                                **({'redirectedRequestId': redirected} if redirected else {})})

    def _decided(self, ws, nav: dict, rid: str, go: bool) -> None:
        url, _ = self.paused.get(rid, ('?', '?'))
        self.decisions[url] = 'continue' if go else 'fail'
        site: Site = nav['site']
        if rid.startswith('doc-'):
            i = int(rid[4:])
            if not go:
                ws.send(json.dumps({'id': nav['id'], 'result': {'frameId': MAIN, 'loaderId': 'L1', 'errorText': 'net::ERR_BLOCKED_BY_CLIENT'}, 'sessionId': 'S1'}))
                return
            if i + 1 < len(nav['chain']):
                self._pause(ws, f'doc-{i + 1}', nav['chain'][i + 1], 'Document', MAIN, rid, headers={'User-Agent': 'fake'} | self.forwarded.get(url, {}))
                return
            if site.error:
                ws.send(json.dumps({'id': nav['id'], 'result': {'frameId': MAIN, 'loaderId': 'L1', 'errorText': site.error}, 'sessionId': 'S1'}))
                return
            ws.send(json.dumps({'id': nav['id'], 'result': {'frameId': MAIN, 'loaderId': 'L1'}, 'sessionId': 'S1'}))
            for kind in site.dialogs:
                self._event(ws, 'Page.javascriptDialogOpening', {'url': url, 'message': 'hello', 'type': kind, 'hasBrowserHandler': False})
            self._committed(ws, url, site.status, site.headers, site.ip)
            for k, (frame_url, frame_ip) in enumerate(site.frames):
                self._event(ws, 'Network.responseReceived', {'requestId': f'f{k}', 'loaderId': 'L2', 'type': 'Document', 'frameId': SUB,
                                                             'response': {'url': frame_url, 'status': 200, 'headers': {}, 'remoteIPAddress': frame_ip}})
            nav['pending'] = len(site.subs)
            for k, r in enumerate(site.subs):
                self._pause(ws, f'sub-{k}', r.url, r.type, r.frame, None, r.method)
            if not site.subs:
                self._loaded(ws, nav)
        elif rid.startswith('sub-'):
            nav['pending'] -= 1
            if nav['pending'] == 0:
                self._loaded(ws, nav)
        elif rid.startswith('js-') and go:
            self._committed(ws, url, 200)
            self._event(ws, 'Page.loadEventFired', {'timestamp': 2.0})

    def _committed(self, ws, url: str, status: int, headers: dict | None = None, ip: str = '93.184.216.34') -> None:
        self._event(ws, 'Network.responseReceived', {'requestId': 'n1', 'loaderId': 'L1', 'type': 'Document', 'frameId': MAIN,
                                                     'response': {'url': url, 'status': status, 'headers': headers or {}, 'remoteIPAddress': ip}})
        self._event(ws, 'Page.frameNavigated', {'frame': {'id': MAIN, 'url': url}})

    def _loaded(self, ws, nav: dict) -> None:
        self._event(ws, 'Page.loadEventFired', {'timestamp': 1.0})
        for k, url in enumerate(nav['site'].after_load):
            self._pause(ws, f'js-{k}', url, 'Document', MAIN, None)
