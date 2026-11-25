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


SMTP = ('method', 'smtp')
GRAPH = ('method', 'graph')

PLUGIN = Plugin(
    id=ID, name='Mail server', version='0.1.0', category='chat',
    description='The connection that sends mail: your SMTP server, or Microsoft 365 through Microsoft Graph. Set it up once; Email routes and personal notification mail send through it.', icon='icon.svg',
    author='Skopax', licence='AGPL-3.0-only', core='>=0.1,<0.7', edition='community',
    capabilities=Capabilities(hosts=('@authority', '@graph-url'), secrets=('password', 'client-cert', 'client-secret')),
    max_instances=1,
    fields=(Field('method', 'Send with', 'choice', required=True, default='smtp', choices=mail.METHODS, group='connection',
                  choice_labels={'smtp': 'SMTP', 'graph': 'Microsoft 365 (Graph)'},
                  help='SMTP sends through a mail server. Microsoft 365 (Graph) sends as one mailbox through the Microsoft Graph API, with an app registration and no SMTP relay.'),
            Field('host', 'Host', 'text', required=True, check=mail.host_problem, show_if=SMTP, group='connection', help='The host name or IP address of the SMTP server, such as smtp.example.com.'),
            Field('port', 'Port', 'int', default=str(mail.DEFAULT_PORT), low=1, high=65535, show_if=SMTP, group='connection', help='587 for STARTTLS, 465 for TLS, 25 for a relay inside your network.'),
            Field('security', 'Security', 'choice', default='starttls', choices=mail.SECURITIES, show_if=SMTP, group='connection',
                  choice_labels={'starttls': 'STARTTLS', 'tls': 'TLS from the start', 'none': 'None (private network only)'},
                  help='The server certificate is always checked. None sends unencrypted and is allowed only to a private or loopback address.'),
            Field('tenant-id', 'Tenant ID', 'text', required=True, check=mail.graph_tenant_problem, show_if=GRAPH, group='connection',
                  help='The directory (tenant) ID of your Microsoft 365 organisation, from the app registration overview in Microsoft Entra, or its domain such as example.onmicrosoft.com.'),
            Field('username', 'Username', 'text', show_if=SMTP, group='sign-in', help='Leave empty when the server takes mail without a sign-in.'),
            Field('password', 'Password', 'secret', show_if=SMTP, group='sign-in', help='The password or app password of the username; it is kept secret.'),
            Field('client-id', 'Client ID', 'text', required=True, check=mail.graph_client_problem, show_if=GRAPH, group='sign-in',
                  help='The application (client) ID of the app registration in Microsoft Entra.'),
            Field('client-cert', 'Certificate and key', 'secret', check=mail.graph_credential_problem, show_if=GRAPH, group='sign-in',
                  help='The RSA private key (PEM, without a password) followed by its certificate (PEM), whose public part is uploaded to the app registration. Preferred over a client secret; kept secret. The field holds 4000 characters, so use a 2048 bit key: a larger key and its certificate do not fit.'),
            Field('client-secret', 'Client secret', 'secret', show_if=GRAPH, group='sign-in',
                  help='A client secret of the app registration, used only when Certificate and key is empty; kept secret.'),
            Field('from', 'From', 'text', required=True, check=mail.sender_problem, show_if=SMTP, group='message',
                  help='The sender of every mail, such as alerts@example.com or Alerts <alerts@example.com>. The server must allow this sender for the username.'),
            Field('sender', 'Sender', 'text', required=True, check=mail.sender_problem, show_if=GRAPH, group='message',
                  help='The mailbox the app sends as, such as alerts@example.com or Alerts <alerts@example.com>; it is the From of every mail. The Exchange role assignment must scope the app to this mailbox.'),
            Field('reply-to', 'Reply-To', 'text', check=mail.sender_problem, group='message', help='Where replies go, such as ops@example.com; empty sends replies to From.'),
            Field('authority', 'Authority', 'url', default=mail.GRAPH_AUTHORITY, check=mail.https_problem, show_if=GRAPH, group='advanced',
                  help='The Microsoft sign-in address; change it only for a national cloud, such as https://login.microsoftonline.us.'),
            Field('graph-url', 'Graph URL', 'url', default=mail.GRAPH_URL, check=mail.https_problem, show_if=GRAPH, group='advanced',
                  help='The Microsoft Graph address; change it only for a national cloud, such as https://graph.microsoft.us.')),
    test=test,
    setup=Setup(steps=(
        'Ask your mail administrator which way Skopax sends: through an SMTP server, or as one Microsoft 365 mailbox through Microsoft Graph.',
        'For SMTP: fill in Host, Port and Security, Username and Password when the server needs a sign-in, and From with the sender address.',
        'For Microsoft 365 (Graph): fill in Tenant ID, Client ID and Sender, and Certificate and key (or a Client secret). The Set up email part of the setup guide lists the Microsoft 365 admin steps.',
        'Fill in Reply-To when replies should go elsewhere. Choose Test connection: for SMTP it connects, starts TLS, signs in and checks the server answers; for Graph it gets a token. Neither sends a mail. Then Save & turn on.',
        'Choose Send test mail on the saved instance: it sends one real mail to your own address. A green Test connection alone does not prove that mail is delivered.',
    )),
)
