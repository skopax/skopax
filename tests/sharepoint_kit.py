import io
import json
import struct
import zipfile
from urllib.parse import parse_qs, quote, urlsplit

from skopax.remote import HttpError, Reply

TENANT = '11111111-2222-3333-4444-555555555555'
CLIENT = '66666666-7777-8888-9999-000000000000'
SECRET = 'fake-client-secret-for-tests'
HOST = 'contoso.sharepoint.com'
SITE = f'https://{HOST}/sites/ops'
VALUES = {'tenant-id': TENANT, 'client-id': CLIENT, 'client-secret': SECRET, 'site': SITE, 'folder': 'Runbooks/Prod'}
LOGIN = f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token'
GRAPH = 'https://graph.microsoft.com/v1.0'
SITE_ID = f'{HOST},aaaa,bbbb'
DRIVE = 'b!drive-1'
FOLDER_ID = 'FOLDER1'
SUB_ID = 'FOLDER2'
WEB = f'https://{HOST}/sites/ops/Shared%20Documents'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


PARENTS = {'/Runbooks/Prod': 'FOLDER1', '/Runbooks/Prod/Checkout': 'FOLDER2', '/Runbooks': 'RUNBOOKS', '': 'ROOT'}


def parent(path: str, drive: str = DRIVE, pid: str | None = None) -> dict:
    return {'driveId': drive, 'driveType': 'documentLibrary', 'id': pid or PARENTS.get(path, 'OTHER'),
            'path': f'/drives/{drive}/root:{quote(path)}' if path else f'/drives/{drive}/root:'}


def folder_json(name: str, fid: str, at: str, count: int = 1, pid: str | None = None) -> dict:
    return {'id': fid, 'name': name, 'folder': {'childCount': count}, 'webUrl': f'{WEB}{quote(at)}/{quote(name)}', 'parentReference': parent(at, pid=pid)}


def file_json(name: str, fid: str, at: str, size: int | None = 100, drive: str = DRIVE, pid: str | None = None, **extra) -> dict:
    out = {'id': fid, 'name': name, 'size': size, 'file': {'mimeType': 'text/plain'}, 'webUrl': f'{WEB}{quote(at)}/{quote(name)}', 'parentReference': parent(at, drive, pid)} | extra
    return {k: v for k, v in out.items() if v is not None}


def docx(paragraphs: list[str], extra: dict[str, bytes] | None = None, xml: bytes | None = None) -> bytes:
    body = ''.join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs)
    document = xml if xml is not None else f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'.encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('word/document.xml', document)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return out.getvalue()


def by_path(rel: str) -> str:
    return f'{GRAPH}/drives/{DRIVE}/items/{FOLDER_ID}:/{quote(rel)}:'


def token_reply(token: str = 'tok-1', expires: int = 3599) -> dict:
    return {'token_type': 'Bearer', 'expires_in': expires, 'access_token': token}


class FakeGraph:
    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> Reply:
        self.calls.append((method, url, dict(headers), data))
        path = url.split('?')[0]
        for (m, where), answer in self.routes.items():
            if m == method and where == path:
                got = answer(parse_qs(urlsplit(url).query), headers, data) if callable(answer) else answer
                if isinstance(got, Exception):
                    raise got
                if isinstance(got, Reply):
                    return got
                return Reply(got, {})
        raise HttpError(404, path, json.dumps({'error': {'code': 'itemNotFound'}}))

    def hits(self, where: str) -> list[tuple[str, str, dict, bytes | None]]:
        return [c for c in self.calls if c[1].split('?')[0] == where]

    def graph_calls(self) -> list[str]:
        return [c[1] for c in self.calls if c[1].startswith(GRAPH)]


def base_routes(**more) -> dict:
    return {
        ('POST', LOGIN): token_reply(),
        ('GET', f'{GRAPH}/sites/{HOST}:/sites/ops'): {'id': SITE_ID, 'webUrl': SITE, 'displayName': 'Ops'},
        ('GET', f'{GRAPH}/sites/{SITE_ID}/drive/root:/Runbooks/Prod'): folder_json('Prod', FOLDER_ID, '/Runbooks', 3),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/{FOLDER_ID}/children'): {'value': [
            file_json('cart-restart.md', 'F1', '/Runbooks/Prod'),
            file_json('Payments outage.docx', 'F2', '/Runbooks/Prod', size=5000),
            folder_json('Checkout', SUB_ID, '/Runbooks/Prod'),
            file_json('diagram.png', 'F3', '/Runbooks/Prod'),
        ]},
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/{SUB_ID}/children'): {'value': [
            file_json('checkout-service.md', 'F4', '/Runbooks/Prod/Checkout'),
            file_json('stray.md', 'F5', '/Runbooks/Prod-old'),
            file_json('remote.md', 'F6', '/Runbooks/Prod/Checkout', drive='b!other'),
        ]},
    } | more


def plain_zip(parts: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return out.getvalue()


def understated(data: bytes, name: str, size: int) -> bytes:
    info = zipfile.ZipFile(io.BytesIO(data)).getinfo(name)
    real = struct.pack('<I', info.file_size)
    assert data.count(real) == 2
    return data.replace(real, struct.pack('<I', size))
