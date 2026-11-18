import json

from skopax.slack import POST_URL, Slack, SlackError


class FakeSlackApi:
    def __init__(self, emails: dict | None = None, replies: list | None = None, fail: set | None = None, bot: str = 'UBOT', scopes: str = 'chat:write,files:write',
                 url: str | None = 'https://example.slack.com/'):
        self.emails = emails or {}
        self.thread = replies or []
        self.fail = set(fail or ())
        self.bot = bot
        self.scopes = scopes
        self.url = url
        self.files = 0
        self.history: list[dict] = []
        self.names: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.sleeps: list[float] = []

    def upload(self, url: str, data: bytes) -> None:
        self.calls.append(('upload', {'url': url, 'size': len(data)}))
        if 'upload' in self.fail:
            raise SlackError('upload failed')

    def __call__(self, token: str, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        if method in self.fail:
            raise SlackError(f'{method} failed')
        if method == 'auth.test':
            return {'ok': True, 'user_id': self.bot, 'bot_id': 'B0BOT', '_scopes': self.scopes, **({'url': self.url} if self.url else {})}
        if method == 'files.getUploadURLExternal':
            self.files += 1
            return {'ok': True, 'upload_url': f'https://files.slack.com/upload/v1/F{self.files}', 'file_id': f'F{self.files}'}
        if method == 'files.completeUploadExternal':
            return {'ok': True, 'files': [{'id': f['id']} for f in json.loads(payload['files'])]}
        if method == 'users.info':
            email = self.emails.get(payload['user'])
            return {'ok': True, 'user': {'id': payload['user'], 'profile': {'email': email} if email else {}}}
        if method == 'conversations.replies':
            return {'ok': True, 'messages': list(self.thread)}
        if method == 'users.lookupByEmail':
            found = next((u for u, e in self.emails.items() if e == payload['email']), None)
            if not found:
                raise SlackError('users_not_found')
            return {'ok': True, 'user': {'id': found, 'profile': {'real_name': self.names.get(found, '')}}}
        if method == 'conversations.history':
            return {'ok': True, 'messages': list(self.history)}
        if method == 'conversations.info':
            return {'ok': True, 'channel': {'id': payload['channel'], 'name': {'C2': 'dev-backend', 'C3': 'support'}.get(payload['channel'], 'ops')}}
        if method == 'chat.getPermalink':
            return {'ok': True, 'permalink': f"https://example.slack.com/archives/{payload['channel']}/p{payload['message_ts'].replace('.', '')}"}
        return {'ok': True, 'channel': payload.get('channel'), 'ts': payload.get('ts')}

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def images(payload: dict) -> list[str]:
    blocks = list(payload.get('blocks') or []) + [b for a in payload.get('attachments') or [] for b in a.get('blocks') or []]
    return [b['slack_file']['id'] for b in blocks if b.get('type') == 'image']


class FakePost:
    def __init__(self, fail: bool = False, refuse_images: int = 0, refusal: str = 'invalid_blocks'):
        self.fail = fail
        self.refuse_images = refuse_images
        self.refusal = refusal
        self.calls: list[dict] = []
        self.posted: list[dict] = []

    def __call__(self, token: str, payload: dict) -> dict:
        self.calls.append(payload)
        if self.fail:
            raise self.fail if isinstance(self.fail, Exception) else SlackError('channel_not_found', code='channel_not_found')
        if self.refuse_images and images(payload):
            self.refuse_images -= 1
            raise self.refusal if isinstance(self.refusal, Exception) else SlackError(self.refusal)
        self.posted.append(payload)
        return {'ok': True, 'channel': payload['channel'], 'ts': f'1792224000.{len(self.calls):06d}'}


def fake_slack(emails=None, replies=None, fail=None, post_fail=False, scopes='chat:write,files:write', refuse_images=0, refusal='invalid_blocks') -> tuple[Slack, FakePost, FakeSlackApi]:
    post, api = FakePost(post_fail, refuse_images, refusal), FakeSlackApi(emails, replies, fail, scopes=scopes)
    return Slack('xoxb-test', '#ops', post, api, upload=api.upload, sleep=api.sleeps.append), post, api


class SlackReply:
    def __init__(self, body: dict):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self.body).encode()


class SlackWeb:
    def __init__(self, posted_to: str = 'C0POSTED01'):
        self.posted_to = posted_to
        self.seen: list = []

    def __call__(self, req, timeout=None) -> SlackReply:
        self.seen.append(req)
        method = req.full_url.split('/api/')[-1].split('?')[0]
        if method == 'chat.postMessage':
            return SlackReply({'ok': True, 'channel': self.posted_to, 'ts': f'1792224000.{len(self.seen):06d}'})
        if method == 'conversations.info':
            return SlackReply({'ok': True, 'channel': {'id': 'C0NEWCHAN1', 'name': 'ops-new', 'is_member': True}})
        if method == 'auth.test':
            return SlackReply({'ok': True, 'user_id': 'UBOT', 'url': 'https://example.slack.com/'})
        return SlackReply({'ok': True})

    def posts(self) -> list[tuple[str, dict]]:
        return [(r.get_header('Authorization'), json.loads(r.data)) for r in self.seen if r.full_url == POST_URL]

    def tokens(self) -> list[str]:
        return [r.get_header('Authorization') for r in self.seen]


def slack_instance(app, app_token: str = '', bot: str = 'xoxb-test') -> None:
    instances = app.state.instances
    if instances.fresh('slack', 'main') is None:
        instances.create('slack', 'main', {'bot-token': bot, **({'app-token': app_token} if app_token else {})}, True, 'test')
