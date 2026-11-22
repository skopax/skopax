import contextlib
import email.policy
import email.utils
import html as markup
import ipaddress
import logging
import re
import smtplib
import socket
import ssl
import threading
from collections.abc import Mapping
from email.headerregistry import Address
from email.message import EmailMessage

from skopax import brand
from skopax.errkind import Authored
from skopax.messages import msg, speaking

log = logging.getLogger('skopax.mail')

PLUGIN_ID = 'mail-server'
TRIAL_KIND = 'test'
TIMEOUT_S = 20
LOOKUP_TIMEOUT_S = 5
RECIPIENTS_MAX = 50
DEFAULT_PORT = 587
SECURITIES = ('starttls', 'tls', 'none')
SECURITY_NAMES = {'starttls': 'STARTTLS', 'tls': 'TLS', 'none': 'no encryption'}
ADDRESS_MAX = 254
SUBJECT_MAX = 200
REPLY_MAX = 160
LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
DOMAIN = re.compile(rf'{LABEL}(?:\.{LABEL})+')
HOST = re.compile(rf'{LABEL}(?:\.{LABEL})*|\[[0-9A-Fa-f:.]+\]|[0-9A-Fa-f:.]+')
KIND = re.compile(r'[A-Za-z0-9._-]{1,40}')
BREAKS = re.compile(r'[\r\n\x00]')
WEB_LINK = re.compile(r'https?://[^\s"<>]+')
SEV_COLOURS = {'crit': '#b42318', 'warn': '#b54708', 'info': '#175cd3'}
INK, MUTED, PAPER, PAGE, EDGE, BUTTON = '#0f1720', '#4a5560', '#ffffff', '#f4f6f8', '#d5dbe1', '#1f5fbf'
FONT = '-apple-system,Segoe UI,Helvetica,Arial,sans-serif'


class MailDown(Authored, Exception):
    pass


class MailRefused(Authored, Exception):
    def __init__(self, *args: object, recipient: bool = False):
        super().__init__(*args)
        self.recipient = recipient


def address_ok(value: object) -> bool:
    if not isinstance(value, str) or len(value) > ADDRESS_MAX or value.count('@') != 1:
        return False
    local, domain = value.split('@')
    return bool(LOCAL.fullmatch(local)) and len(local) <= 64 and bool(DOMAIN.fullmatch(domain))


def named(value: str) -> tuple[str, str] | None:
    if not isinstance(value, str) or BREAKS.search(value):
        return None
    text = value.strip()
    if address_ok(text):
        return '', text
    if not text.endswith('>') or text.count('<') != 1:
        return None
    name, addr = email.utils.parseaddr(text, strict=True)
    if not address_ok(addr) or any(ch in name for ch in '<>@'):
        return None
    return name, addr


def sender_problem(value: str) -> str | None:
    return None if named(value) is not None else msg('mailserver.from')


def host_problem(value: str) -> str | None:
    text = str(value or '').strip()
    return None if HOST.fullmatch(text) and len(text) <= 253 else msg('mailserver.host')


def loopback_or_private(value: str) -> bool:
    try:
        found = ipaddress.ip_address(value.strip('[]'))
    except ValueError:
        return False
    return found.is_loopback or found.is_private


def looked_up(host: str, timeout: float) -> list[str] | None:
    box: dict[str, list[str]] = {}

    def go() -> None:
        try:
            box['found'] = [str(item[4][0]) for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM) or ()]
        except (OSError, TypeError, IndexError):
            box['found'] = []

    worker = threading.Thread(target=go, name='skopax-mail-lookup', daemon=True)
    worker.start()
    worker.join(timeout)
    return box.get('found')


def reply_of(code: object, text: object, secret: str) -> str:
    said = text.decode('utf-8', 'replace') if isinstance(text, bytes) else str(text or '')
    said = said.replace(secret, '***') if secret else said
    said = ' '.join(said.split())
    said = ''.join(ch for ch in said if ch.isprintable())[:REPLY_MAX]
    return f'{code} {said}'.strip() if code else said


def _hidden(text: str, to: tuple[str, ...]) -> str:
    for address in to:
        local, _, domain = str(address or '').partition('@')
        if domain:
            text = re.sub(re.escape(address), f'{local[:1]}***@{domain}', text, flags=re.IGNORECASE)
    return text


def reason_of(error: BaseException) -> str:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return f'no answer in {TIMEOUT_S} s'
    if isinstance(error, ConnectionRefusedError):
        return 'connection refused'
    if isinstance(error, socket.gaierror):
        return 'host not found'
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return 'the server closed the connection'
    return type(error).__name__


class Mailer:
    def __init__(self, config: Mapping[str, str], timeout: float = TIMEOUT_S):
        self._config = {str(k): str(v if v is not None else '') for k, v in (config or {}).items()}
        self.timeout = timeout
        self._target = ''

    def __repr__(self) -> str:
        return f'Mailer({self.host}:{self.port_text})'

    @property
    def host(self) -> str:
        return self._config.get('host', '').strip()

    @property
    def port_text(self) -> str:
        return self._config.get('port', '').strip() or str(DEFAULT_PORT)

    @property
    def port(self) -> int:
        try:
            port = int(self.port_text)
        except ValueError:
            return 0
        return port if 1 <= port <= 65535 else 0

    @property
    def security(self) -> str:
        return self._config.get('security', '').strip() or SECURITIES[0]

    @property
    def username(self) -> str:
        return self._config.get('username', '').strip()

    def _problem(self) -> str | None:
        if not self.host or host_problem(self.host) is not None or not self.port or self.security not in SECURITIES:
            return msg('mailserver.not_ready')
        if named(self._config.get('from', '')) is None:
            return msg('mailserver.not_ready')
        reply = self._config.get('reply-to', '')
        if reply.strip() and named(reply) is None:
            return msg('mailserver.not_ready')
        if not (self.username.isascii() and self._config.get('password', '').isascii()):
            return msg('mailserver.sign_in_ascii')
        return None

    def ready(self) -> bool:
        return self._problem() is None

    def _checked(self) -> None:
        problem = self._problem()
        if problem is not None:
            raise MailRefused(problem)
        self._target = self._plain_target() if self.security == 'none' else self.host

    def _plain_target(self) -> str:
        bare = self.host.strip('[]')
        if self.host.lower() == 'localhost':
            return '127.0.0.1'
        if loopback_or_private(bare):
            return bare
        found = looked_up(self.host, LOOKUP_TIMEOUT_S)
        if found is None:
            raise MailDown(msg('mailserver.connect', host=self.host, port=self.port, reason='the host lookup took too long'))
        if not found:
            raise MailDown(msg('mailserver.connect', host=self.host, port=self.port, reason='host not found'))
        if not all(loopback_or_private(address) for address in found):
            raise MailRefused(msg('mailserver.plain_public', host=self.host))
        return found[0]

    def _connect(self, stage: list[str]) -> smtplib.SMTP:
        context = ssl.create_default_context()
        stage[0] = 'connect'
        if self.security == 'tls':
            client = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            client = smtplib.SMTP(self._target or self.host, self.port, timeout=self.timeout)
        try:
            client.ehlo()
            if self.security == 'starttls':
                stage[0] = 'tls'
                client.starttls(context=context)
                client.ehlo()
            if self.username:
                stage[0] = 'login'
                client.login(self.username, self._config.get('password', ''))
            stage[0] = 'session'
        except BaseException:
            with contextlib.suppress(Exception):
                client.close()
            raise
        return client

    def _mapped(self, error: BaseException, stage: str) -> Exception:
        secret = self._config.get('password', '')
        where = {'host': self.host, 'port': self.port}
        if isinstance(error, smtplib.SMTPAuthenticationError):
            return MailRefused(msg('mailserver.login'))
        if isinstance(error, smtplib.SMTPRecipientsRefused):
            codes = [code for code, _ in error.recipients.values()]
            first = next(iter(error.recipients.values()), (0, b''))
            text = msg('mailserver.refused', reply=reply_of(first[0], first[1], secret))
            return MailDown(text) if codes and all(400 <= c < 500 for c in codes) else MailRefused(text, recipient=True)
        if isinstance(error, smtplib.SMTPResponseException):
            text = msg('mailserver.refused', reply=reply_of(error.smtp_code, error.smtp_error, secret))
            if stage == 'login' and error.smtp_code >= 500:
                return MailRefused(msg('mailserver.login'))
            return MailDown(text) if error.smtp_code < 500 else MailRefused(text)
        if isinstance(error, smtplib.SMTPNotSupportedError):
            if stage == 'tls':
                return MailRefused(msg('mailserver.tls', host=self.host, reason='the server does not offer STARTTLS'))
            return MailRefused(msg('mailserver.refused', reply='the server offers no sign-in' if stage == 'login' else reason_of(error)))
        if isinstance(error, ssl.SSLCertVerificationError):
            return MailRefused(msg('mailserver.tls', host=self.host, reason=reply_of(0, getattr(error, 'verify_message', '') or 'the certificate is not trusted', secret)))
        if isinstance(error, ssl.SSLError):
            return MailDown(msg('mailserver.tls', host=self.host, reason=type(error).__name__))
        if stage == 'connect':
            return MailDown(msg('mailserver.connect', reason=reason_of(error), **where))
        return MailDown(msg('mailserver.down', reason=reason_of(error), **where))

    @contextlib.contextmanager
    def _session(self, to: tuple[str, ...] = ()):
        self._checked()
        stage = ['connect']
        client = None
        try:
            client = self._connect(stage)
            yield client
            with contextlib.suppress(Exception):
                client.quit()
        except (MailDown, MailRefused):
            raise
        except (smtplib.SMTPException, OSError) as e:
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()
            mapped = self._mapped(e, stage[0])
            log.info('%s', _hidden(mapped.text, to))
            raise mapped from None

    def probe(self) -> str:
        with self._session() as client:
            client.noop()
        shown = SECURITY_NAMES[self.security]
        if self.username:
            return msg('mailserver.ok_signed', host=self.host, port=self.port, security=shown, user=self.username)
        return msg('mailserver.ok', host=self.host, port=self.port, security=shown)

    def _message(self, to: list[str], subject: str, text: str, html: str | None, kind: str) -> EmailMessage:
        if not isinstance(to, (list, tuple)) or not 1 <= len(to) <= RECIPIENTS_MAX:
            raise MailRefused(msg('mailserver.recipients', max=RECIPIENTS_MAX, count=len(to) if isinstance(to, (list, tuple)) else 0))
        if not isinstance(subject, str) or BREAKS.search(subject) or any(isinstance(a, str) and BREAKS.search(a) for a in to):
            raise MailRefused(msg('mailserver.header'))
        bad = next((a for a in to if not address_ok(a)), None)
        if bad is not None:
            raise MailRefused(msg('mailserver.address', address=reply_of(0, bad, '')), recipient=True)
        if kind and not KIND.fullmatch(kind):
            raise MailRefused(msg('mailserver.kind'))
        name, addr = named(self._config.get('from', '')) or ('', '')
        message = EmailMessage(policy=email.policy.SMTP)
        message['From'] = Address(display_name=name, addr_spec=addr)
        message['To'] = ', '.join(to)
        message['Subject'] = subject
        message['Date'] = email.utils.formatdate(usegmt=True)
        message['Message-ID'] = email.utils.make_msgid(domain=addr.split('@')[1])
        reply = named(self._config.get('reply-to', '')) if self._config.get('reply-to', '').strip() else None
        if reply is not None:
            message['Reply-To'] = Address(display_name=reply[0], addr_spec=reply[1])
        message['Auto-Submitted'] = 'auto-generated'
        if kind:
            message['X-Skopax-Kind'] = kind
        message.set_content(text or '', charset='utf-8', cte='quoted-printable')
        if html is not None:
            message.add_alternative(html, subtype='html', charset='utf-8', cte='quoted-printable')
        return message

    def _delivered(self, to: list[str], subject: str, text: str, html: str | None, kind: str, said: list | None = None) -> str:
        problem = self._problem()
        if problem is not None:
            raise MailRefused(problem)
        message = self._message(list(to) if isinstance(to, (list, tuple)) else to, subject, text, html, kind)
        addr = (named(self._config.get('from', '')) or ('', ''))[1]
        with self._session(tuple(to)) as client:
            if said is not None:
                data = client.data

                def heard(content):
                    reply = data(content)
                    said.append(reply)
                    return reply

                client.data = heard
            client.send_message(message, from_addr=addr, to_addrs=list(to))
        return message['Message-ID']

    def send(self, to: list[str], subject: str, text: str, html: str | None = None, *, kind: str = '') -> str:
        return self._delivered(to, subject, text, html, kind)

    def send_answered(self, to: list[str], subject: str, text: str, html: str | None = None, *, kind: str = '') -> str:
        said: list[tuple] = []
        self._delivered(to, subject, text, html, kind, said)
        if not said:
            return ''
        code, words = said[-1]
        return reply_of(code, words, self._config.get('password', ''))


def mailer(runtime) -> Mailer | None:
    found = runtime.instances_of(PLUGIN_ID)
    if not found:
        return None
    chosen = next((i for i in found if i.primary), found[0])
    made = Mailer(chosen.info().config)
    return made if made.ready() else None


def title_of(card: dict) -> str:
    num = card.get('num') if card.get('num') is not None else card.get('number')
    head = f'#{num} ' if num not in (None, '') else ''
    return ' '.join(f"{head}{card.get('svc') or '-'} / {card.get('label') or '?'}".split())


def state_of(card: dict) -> str:
    if card.get('muted'):
        return 'muted'
    if card.get('resolution') or card.get('status') not in ('open', 'cleared'):
        return 'closed'
    if card.get('triage') == 'progress':
        return 'progress'
    if card.get('triage') == 'handled':
        return 'handled'
    return 'firing' if card.get('status') == 'open' else 'stopped'


def sev_of(card: dict) -> str:
    sev = card.get('sev')
    return sev if sev in SEV_COLOURS else 'info'


def flat(text: object) -> str:
    return ' '.join(str(text or '').split())


def esc(text: object) -> str:
    return markup.escape(str(text or ''), quote=True)


def html_lines(text: object) -> str:
    return '<br>'.join(esc(part) for part in str(text or '').splitlines())


def cell(content: str, style: str) -> str:
    return f'<tr><td style="{style}">{content}</td></tr>'


def card_html(title: str, head: str, sev: str, sev_text: str, status: str, lines: list[str], link: str | None, footer: str, attribution: dict | None) -> str:
    side = 'padding-left:24px;padding-right:24px;'
    rows = [cell(esc(head), f'padding-top:20px;padding-bottom:8px;{side}font-size:14px;color:{MUTED};background-color:{PAPER};'),
            cell(esc(title), f'padding-bottom:12px;{side}font-size:20px;font-weight:bold;color:{INK};background-color:{PAPER};'),
            cell(f'<span style="background-color:{SEV_COLOURS[sev]};color:#ffffff;padding:2px 8px;border-radius:4px;font-weight:bold;">{esc(sev_text)}</span>'
                 f'<span style="color:{MUTED};"> &nbsp;{esc(msg("mail.status"))}: {esc(status)}</span>', f'padding-bottom:16px;{side}font-size:14px;background-color:{PAPER};')]
    rows += [cell(html_lines(line), f'padding-bottom:8px;{side}font-size:15px;line-height:1.5;color:{INK};background-color:{PAPER};') for line in lines]
    if link:
        rows.append(cell(f'<a href="{esc(link)}" style="display:inline-block;background-color:{BUTTON};color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:6px;font-weight:bold;">'
                         f'{esc(msg("mail.open"))}</a>', f'padding-top:16px;padding-bottom:24px;{side}background-color:{PAPER};'))
    tail = esc(footer)
    if attribution is not None:
        tail += f'<br><a href="{esc(attribution["href"])}" style="color:{MUTED};">{esc(attribution["text"])}</a>'
    rows.append(cell(tail, f'padding-top:12px;padding-bottom:12px;{side}border-top:1px solid {EDGE};font-size:12px;color:{MUTED};background-color:{PAPER};'))
    return framed(title, ''.join(rows))


def framed(title: str, body: str) -> str:
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
            f'<title>{esc(title)}</title></head><body style="margin:0;padding:0;background-color:{PAGE};color:{INK};">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{PAGE};"><tr><td align="center" style="padding:24px 12px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;background-color:{PAPER};color:{INK};border:1px solid {EDGE};'
            f'border-radius:8px;font-family:{FONT};">{body}</table></td></tr></table></body></html>')


def card_text(title: str, head: str, sev_text: str, status: str, lines: list[str], link: str | None, footer: str, attribution: dict | None) -> str:
    parts = [head, '', title, f"{msg('mail.severity')}: {sev_text}", f"{msg('mail.status')}: {status}"]
    if lines:
        parts += [''] + lines
    if link:
        parts += ['', f"{msg('mail.open')}: {link}"]
    parts += ['', '-- ', footer]
    if attribution is not None:
        parts.append(f"{attribution['text']} {attribution['href']}")
    return '\n'.join(parts) + '\n'


def card_mail(card: dict, link: str, *, head: str, body_lines: list[str], lang: str | None = None) -> tuple[str, str, str]:
    with speaking(lang) if lang else contextlib.nullcontext():
        title = title_of(card)
        sev = sev_of(card)
        sev_text = msg(f'mail.sev.{sev}')
        status = msg(f'mail.state.{state_of(card)}')
        shown_head = flat(head)
        lines = [str(line) for line in body_lines or [] if str(line or '').strip()]
        web = link if isinstance(link, str) and WEB_LINK.fullmatch(link) else None
        footer = msg('mail.footer')
        attribution = brand.attribution_line()
        subject = flat(msg('mail.subject', head=shown_head, card=title))[:SUBJECT_MAX]
        text = card_text(title, shown_head, sev_text, status, lines, web, footer, attribution)
        html = card_html(title, shown_head, sev, sev_text, status, lines, web, footer, attribution)
    return subject, text, html


def trial_mail(instance: str, at: str) -> tuple[str, str, str]:
    title, head, footer = msg('mail.test.title'), msg('mail.test.head'), msg('mail.footer')
    lines = [msg('mail.test.line'), msg('mail.test.instance', instance=instance), msg('mail.test.at', at=at)]
    attribution = brand.attribution_line()
    subject = flat(msg('mail.test.subject'))[:SUBJECT_MAX]
    parts = [head, '', title, ''] + lines + ['', '-- ', footer] + ([f"{attribution['text']} {attribution['href']}"] if attribution is not None else [])
    side = 'padding-left:24px;padding-right:24px;'
    rows = [cell(esc(head), f'padding-top:20px;padding-bottom:8px;{side}font-size:14px;color:{MUTED};background-color:{PAPER};'),
            cell(esc(title), f'padding-bottom:12px;{side}font-size:20px;font-weight:bold;color:{INK};background-color:{PAPER};')]
    rows += [cell(esc(line), f'padding-bottom:8px;{side}font-size:15px;line-height:1.5;color:{INK};background-color:{PAPER};') for line in lines]
    tail = esc(footer)
    if attribution is not None:
        tail += f'<br><a href="{esc(attribution["href"])}" style="color:{MUTED};">{esc(attribution["text"])}</a>'
    rows.append(cell(tail, f'padding-top:12px;padding-bottom:12px;{side}border-top:1px solid {EDGE};font-size:12px;color:{MUTED};background-color:{PAPER};'))
    return subject, '\n'.join(parts) + '\n', framed(title, ''.join(rows))
