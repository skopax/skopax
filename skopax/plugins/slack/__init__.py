from collections.abc import Mapping
from time import monotonic

from skopax.contract import Capabilities, Context, Field, Plugin, Setup, ReadError, TestResult, call
from skopax.kit import connection
from skopax.messages import msg

ID = 'slack'
AUTH_TEST = 'https://slack.com/api/auth.test'
CONNECT = 'https://slack.com/api/apps.connections.open'
BOT_PREFIX = 'xoxb-'
APP_PREFIX = 'xapp-'
TEST_BUDGET_S = 12
SCOPES_HEADER = 'x-oauth-scopes'
READ_SCOPES = (('chat:write',), ('files:write',), ('channels:read', 'groups:read'), ('users:read',), ('users:read.email',))
LISTENER_SCOPES = (('channels:history', 'groups:history'), ('files:read',))


def env_instances(env: Mapping[str, str]) -> list[dict]:
    bot = (env.get('SLACK_BOT_TOKEN') or '').strip()
    if not bot:
        return []
    return [{'name': 'main', 'primary': 'true', 'bot-token': bot, 'app-token': (env.get('SLACK_APP_TOKEN') or '').strip()}]


def lacking(headers: Mapping[str, str], listener: bool) -> list[str]:
    found = {str(k).lower(): v for k, v in (headers or {}).items()}
    if SCOPES_HEADER not in found:
        return []
    granted = {s.strip() for s in str(found[SCOPES_HEADER] or '').split(',') if s.strip()}
    return [' or '.join(any_of) for any_of in READ_SCOPES + (LISTENER_SCOPES if listener else ()) if not granted & set(any_of)]


def left(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


def error_of(answer: dict) -> str:
    return str(answer.get('error') or 'unknown_error')[:60]


def test(ctx: Context) -> TestResult:
    config = ctx.instance.config

    def ask() -> str:
        deadline = monotonic() + TEST_BUDGET_S
        bot, app = config.get('bot-token') or '', config.get('app-token') or ''
        if not bot.startswith(BOT_PREFIX):
            raise ReadError(401, msg('integrations.slack_bot_shape'))
        if app and not app.startswith(APP_PREFIX):
            raise ReadError(401, msg('integrations.slack_app_shape'))
        reply = call(ctx.http(), 'GET', AUTH_TEST, {'Authorization': f'Bearer {bot}'}, timeout=left(deadline))
        answer = reply.body if isinstance(reply.body, dict) else {}
        if answer.get('ok') is not True:
            raise ReadError(401, msg('integrations.slack_refused', error=error_of(answer)))
        missing = lacking(reply.headers, bool(app))
        if missing:
            raise ReadError(403, msg('integrations.slack_scopes', scopes=', '.join(missing)))
        if app:
            opened = call(ctx.http(), 'POST', CONNECT, {'Authorization': f'Bearer {app}'}, timeout=left(deadline)).body
            opened = opened if isinstance(opened, dict) else {}
            if opened.get('ok') is not True:
                raise ReadError(401, msg('integrations.slack_app_refused', error=error_of(opened)))
        return msg('integrations.slack_ok', team=str(answer.get('team') or '-'), user=str(answer.get('user') or '-'))

    return connection.result(ask)


PLUGIN = Plugin(
    id=ID, name='Slack', version='0.1.0', category='chat', description='Posts cards and deploy reports to a Slack channel and answers questions and button clicks there.', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.4', edition='community', capabilities=Capabilities(hosts=('slack.com',), secrets=('bot-token', 'app-token')),
    max_instances=1,
    fields=(Field('bot-token', 'Bot token', 'secret', required=True, help='The bot token (xoxb-...) of the Slack app. The channel cards go to is set in Admin \u203a General \u203a Slack \u203a Channel.'),
            Field('app-token', 'App token', 'secret', help='The app-level token (xapp-...) for Socket Mode, which the Slack listener needs. A new app token takes effect after a restart.')),
    env_instances=env_instances, test=test,
    setup=Setup(steps=(
        'At https://api.slack.com/apps choose Create New App > From a manifest, pick your workspace and paste examples/slack-app-manifest.yaml from the Skopax repository. It sets the bot scopes, events and Socket Mode.',
        'Choose Install to Workspace, then Allow. Under OAuth & Permissions copy the bot token (xoxb-...) and paste it into Bot token.',
        'Under Basic Information, in the App-level tokens section, generate a token (Socket Mode needs one; if asked for a scope, choose connections:write) and paste the xapp-... token into App token.',
        'Choose Test connection, then Save & turn on. Restart Skopax once: an app token added or changed later is read only at start.',
        'Invite the bot to the channel (/invite @Skopax) and set that channel in Admin > General > Slack > Channel.',
    ), docs='https://docs.slack.dev/app-management/quickstart-app-settings', docs_label='Slack: app settings'),
)
