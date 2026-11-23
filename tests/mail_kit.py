import smtplib

PASSWORD = 'plain-word-mail-61'
CONFIG = {'host': 'smtp.example.com', 'port': '587', 'security': 'starttls', 'username': 'alerts@example.com', 'password': PASSWORD, 'from': 'Skopax <alerts@example.com>'}


class FakeSMTP:
    made: list['FakeSMTP'] = []
    fail: dict[str, BaseException] = {}
    reply: tuple[int, bytes] = (250, b'2.0.0 OK queued as 4ABC')
    partial: dict = {}

    def __init__(self, host='', port=0, local_hostname=None, timeout=None, source_address=None, context=None, **kw):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.ssl = False
        self.steps: list[str] = []
        self.sent: list[tuple] = []
        FakeSMTP.made.append(self)
        self.step('connect')

    def step(self, name: str) -> None:
        self.steps.append(name)
        if name in FakeSMTP.fail:
            raise FakeSMTP.fail[name]

    def ehlo(self, name=''):
        self.step('ehlo')
        return 250, b'ok'

    def starttls(self, context=None):
        self.tls_context = context
        self.step('starttls')
        return 220, b'ready'

    def login(self, user, password):
        self.login_as = (user, password)
        self.step('login')
        return 235, b'ok'

    def noop(self):
        self.step('noop')
        return 250, b'ok'

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.sent.append((message, from_addr, to_addrs))
        self.step('send')
        self.data(message)
        return dict(FakeSMTP.partial)

    def data(self, message):
        return FakeSMTP.reply

    def quit(self):
        self.steps.append('quit')
        return 221, b'bye'

    def close(self):
        self.steps.append('close')


class FakeSSL(FakeSMTP):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.ssl = True


def faked(monkeypatch, **fail: BaseException) -> type[FakeSMTP]:
    FakeSMTP.made = []
    FakeSMTP.fail = dict(fail)
    FakeSMTP.partial = {}
    monkeypatch.setattr(smtplib, 'SMTP', FakeSMTP)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', FakeSSL)
    return FakeSMTP
