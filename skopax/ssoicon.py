import hashlib
import re
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable

from bson.binary import Binary

from skopax import clock
from skopax.errkind import Authored

COLLECTION = 'sso_icon'
DOC_ID = 'icon'
ICON_MAX = 64 * 1024
CACHE_S = 10
PNG = b'\x89PNG\r\n\x1a\n'
SVG_TYPE = 'image/svg+xml'
SVG_NS = 'http://www.w3.org/2000/svg'
XLINK_HREF = '{http://www.w3.org/1999/xlink}href'
SVG_NAMESPACES = ('', SVG_NS, 'http://www.w3.org/1999/xlink')
SVG_TAGS = ('svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'defs', 'linearGradient', 'radialGradient', 'stop', 'clipPath', 'mask', 'symbol',
            'use', 'title', 'desc')
SVG_ATTRS = ('fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'stroke-miterlimit', 'stroke-dasharray', 'stroke-dashoffset',
             'stroke-opacity', 'd', 'x', 'y', 'width', 'height', 'viewBox', 'transform', 'cx', 'cy', 'r', 'rx', 'ry', 'points', 'offset', 'stop-color', 'stop-opacity', 'opacity',
             'clip-rule', 'clip-path', 'mask', 'gradientUnits', 'gradientTransform', 'spreadMethod', 'clipPathUnits', 'maskUnits', 'maskContentUnits', 'x1', 'y1', 'x2', 'y2',
             'fx', 'fy', 'id', 'class', 'version', 'preserveAspectRatio')
SVG_FUNCTIONS = ('matrix', 'translate', 'scale', 'rotate', 'skewx', 'skewy', 'rgb', 'rgba', 'hsl', 'hsla')
SVG_DEPTH = 64
SVG_ATTR_MAX = 64
SVG_VALUE_MAX = 4096
SVG_USE_MAX = 32
PAINT_ATTRS = ('fill', 'stroke', 'clip-path', 'mask')
XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'
LOCAL_REF = re.compile(r'#[A-Za-z_][\w.:-]{0,127}', re.ASCII)
PAINT_REF = re.compile(r'[ \t\r\n]*url\([ \t\r\n]*#[A-Za-z_][\w.:-]{0,127}[ \t\r\n]*\)[ \t\r\n]*', re.ASCII)
NAME_VALUE = re.compile(r'[A-Za-z_][\w.:-]{0,127}', re.ASCII)
CLASS_VALUE = re.compile(r'[A-Za-z_][\w-]{0,63}(?: [A-Za-z_][\w-]{0,63}){0,15}', re.ASCII)
FUNCTION = re.compile(r'(?<![A-Za-z-])([A-Za-z-]+)[ \t\r\n]*\(', re.ASCII)
VALUE_BAD = re.compile(r'[\\<>&"\'`;{}]')
DECLARATION = re.compile(rb'<!(DOCTYPE|ENTITY)', re.I)
ENCODING = re.compile(rb'^\s*<\?xml[^>]*\bencoding\s*=\s*[\'"]([^\'"]*)[\'"]', re.I)
XML_DECL = re.compile(rb'^\s*<\?xml\s[^?]*\?>')


class IconRefused(Authored, ValueError):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def refuse() -> None:
    raise IconRefused('sso.icon_svg')


def value_safe(name: str, value: str) -> bool:
    if len(value) > SVG_VALUE_MAX or not value.isascii() or VALUE_BAD.search(value):
        return False
    if name == 'id':
        return NAME_VALUE.fullmatch(value) is not None
    if name == 'class':
        return CLASS_VALUE.fullmatch(value) is not None
    if name in PAINT_ATTRS and 'url' in value.lower():
        return PAINT_REF.fullmatch(value) is not None
    return all(f.lower() in SVG_FUNCTIONS for f in FUNCTION.findall(value))


def attrs_safe(tag: str, attrib: dict) -> bool:
    if len(attrib) > SVG_ATTR_MAX:
        return False
    for name, value in attrib.items():
        if name in ('href', XLINK_HREF):
            if tag != 'use' or not value.isascii() or not LOCAL_REF.fullmatch(value):
                return False
        elif name == XML_SPACE:
            if value not in ('preserve', 'default'):
                return False
        elif name not in SVG_ATTRS or not value_safe(name, value):
            return False
    return True


def uses_safe(root) -> bool:
    use = '{%s}use' % SVG_NS
    uses = [e for e in root.iter(use)]
    if len(uses) > SVG_USE_MAX:
        return False
    named = {e.get('id'): e for e in root.iter() if e.get('id')}
    for e in uses:
        ref = e.get('href') or e.get(XLINK_HREF) or ''
        target = named.get(ref[1:])
        if target is not None and any(x.tag == use for x in target.iter()):
            return False
    return True


def svg_safe(data: bytes) -> None:
    named = ENCODING.match(data)
    if DECLARATION.search(data) or (named and named.group(1).lower() not in (b'utf-8', b'utf8')):
        refuse()
    head = XML_DECL.match(data)
    if b'<?' in data[head.end() if head else 0:]:
        refuse()
    parser = ElementTree.XMLPullParser(events=('start-ns', 'start', 'end'))
    depth, root, foreign = 0, None, False
    try:
        parser.feed(data.decode('utf-8'))
        parser.close()
        events = list(parser.read_events())
    except (ElementTree.ParseError, UnicodeDecodeError, ValueError):
        refuse()
    for event, item in events:
        if event == 'start-ns':
            foreign = foreign or item[1] not in SVG_NAMESPACES
            continue
        if event == 'end':
            depth -= 1
            continue
        depth += 1
        if root is None:
            root = item
            if item.tag.rsplit('}', 1)[-1] != 'svg':
                raise IconRefused('sso.icon_type')
        if foreign:
            refuse()
        ns, _, tag = item.tag[1:].partition('}') if item.tag.startswith('{') else ('', '', item.tag)
        if depth > SVG_DEPTH or ns != SVG_NS or tag not in SVG_TAGS or not attrs_safe(tag, item.attrib):
            refuse()
    if root is None or not uses_safe(root):
        refuse()


def check(data: bytes) -> str:
    if len(data) > ICON_MAX:
        raise IconRefused('sso.icon_too_big')
    if data.startswith(PNG):
        return 'image/png'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.lstrip()[:1] == b'<':
        svg_safe(data)
        return SVG_TYPE
    raise IconRefused('sso.icon_type')


def version_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def status_of(doc: dict | None) -> dict:
    ok = bool(doc) and isinstance(doc.get('type'), str) and isinstance(doc.get('version'), str) and isinstance(doc.get('size'), int)
    if not ok:
        return {'set': False, 'type': None, 'size': 0, 'version': None}
    return {'set': True, 'type': doc['type'], 'size': doc['size'], 'version': doc['version']}


class SsoIcon:
    def __init__(self, db, clock: Callable[[], float] = time.monotonic, collection: str = COLLECTION):
        self.col = db[collection]
        self.clock = clock
        self.meta: tuple[float, dict] | None = None
        self.held: tuple[str, bytes, str] | None = None

    def status(self) -> dict:
        now = self.clock()
        if self.meta is None or now - self.meta[0] > CACHE_S:
            self.meta = (now, status_of(self.col.find_one({'_id': DOC_ID}, {'data': 0})))
        return dict(self.meta[1])

    def data(self, version: str) -> tuple[bytes, str] | None:
        if self.held and self.held[0] == version:
            return self.held[1], self.held[2]
        doc = self.col.find_one({'_id': DOC_ID})
        if not doc or not isinstance(doc.get('data'), (bytes, Binary)) or doc.get('version') != version or not isinstance(doc.get('type'), str):
            return None
        self.held = (version, bytes(doc['data']), doc['type'])
        return self.held[1], self.held[2]

    def set(self, data: bytes, kind: str, by: str) -> dict:
        doc = {'data': Binary(data), 'type': kind, 'size': len(data), 'version': version_of(data), 'by': str(by), 'at': clock.stamp(time.time())}
        self.col.replace_one({'_id': DOC_ID}, doc, upsert=True)
        self.held = (doc['version'], data, kind)
        self.meta = (self.clock(), status_of(doc))
        return dict(self.meta[1])

    def clear(self) -> bool:
        gone = self.col.delete_one({'_id': DOC_ID}).deleted_count > 0
        self.held = None
        self.meta = (self.clock(), status_of(None))
        return gone
