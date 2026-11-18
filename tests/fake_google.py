import json
from urllib.parse import parse_qs

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from skopax.kit.google import TOKEN_URL
from skopax.remote import Reply
from tests.fake_http import FakeHttp

ACCESS = 'ya29.access-token-77f1'
PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = PRIVATE.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
KEY = json.dumps({'type': 'service_account', 'client_email': 'reader@analytics.example.com', 'private_key_id': 'kid-1', 'private_key': PEM})
PLANTED = 's3cr3t'
BROKEN_KEY = json.dumps({'type': 'service_account', 'client_email': 'reader@analytics.example.com', 'private_key': f'-----BEGIN PRIVATE KEY-----{PLANTED}'})


class Google(FakeHttp):
    def __init__(self, routes: dict | None = None, token: object = None):
        super().__init__(routes)
        self.token = token if token is not None else {'access_token': ACCESS, 'expires_in': 3599}
        self.forms: list[dict] = []

    def __call__(self, method, url, headers, data, timeout):
        if url != TOKEN_URL:
            return super().__call__(method, url, headers, data, timeout)
        self.calls.append((method, url, headers, None))
        self.forms.append({k: v[0] for k, v in parse_qs(data.decode()).items()})
        if isinstance(self.token, Exception):
            raise self.token
        return Reply(self.token, {})
