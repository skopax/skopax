from skopax import mail
from skopax.contract import Capabilities, Context, Field, Plugin, ReadError, Setup, TestResult
from skopax.kit import connection

ID = mail.PLUGIN_ID


def probed(ctx: Context) -> str:
    try:
        return mail.Mailer(dict(ctx.instance.config) if ctx.instance is not None else {}).probe()
    except (mail.MailDown, mail.MailRefused) as e:
        raise ReadError(0, e.text) from None


def test(ctx: Context) -> TestResult:
    return connection.result(lambda: probed(ctx))


PLUGIN = Plugin(
    id=ID, name='Mail server', version='0.1.0', category='chat',
    description="Sends email through the organisation's SMTP server, for the mail that other parts of Skopax send.", icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.6', edition='community', capabilities=Capabilities(hosts=(), secrets=('password',)),
    max_instances=1,
    fields=(Field('host', 'Host', 'text', required=True, check=mail.host_problem, group='connection', help='The host name or IP address of the SMTP server, such as smtp.example.com.'),
            Field('port', 'Port', 'int', default=str(mail.DEFAULT_PORT), low=1, high=65535, group='connection', help='587 for STARTTLS, 465 for TLS, 25 for a relay inside your network.'),
            Field('security', 'Security', 'choice', default='starttls', choices=mail.SECURITIES, group='connection',
                  choice_labels={'starttls': 'STARTTLS', 'tls': 'TLS from the start', 'none': 'None (private network only)'},
                  help='The server certificate is always checked. None sends unencrypted and is allowed only to a private or loopback address.'),
            Field('username', 'Username', 'text', group='sign-in', help='Leave empty when the server takes mail without a sign-in.'),
            Field('password', 'Password', 'secret', group='sign-in', help='The password or app password of the username; it is kept secret.'),
            Field('from', 'From', 'text', required=True, check=mail.sender_problem, group='message',
                  help='The sender of every mail, such as alerts@example.com or Alerts <alerts@example.com>. The server must allow this sender for the username.'),
            Field('reply-to', 'Reply-To', 'text', check=mail.sender_problem, group='message', help='Where replies go, such as ops@example.com; empty sends replies to From.')),
    test=test,
    setup=Setup(steps=(
        'Ask your mail administrator for the SMTP host, port and security, and for a sender address Skopax may send as.',
        'Fill in Host, Port and Security. Fill in Username and Password when the server needs a sign-in.',
        'Fill in From with the sender address, and Reply-To when replies should go elsewhere.',
        'Choose Test connection: it connects, starts TLS, signs in and checks the server answers, without sending a mail. Then Save & turn on.',
    )),
)
