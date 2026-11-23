from skopax import tags
from skopax.contract import Capabilities, Context, Field, HttpError, Outbound, Plugin, Setup, ReadError, Setting, TestResult, call
from skopax.kit import connection
from skopax.kit.links import sendable
from skopax.messages import msg
from skopax.plugins.teams import cards

ID = 'teams'
TIMEOUT_S = 8
SEVS = ('warn', 'high', 'crit')
TOOK = 'Teams took the test post'


def webhook_of(ctx: Context) -> str:
    return str((ctx.instance.config.get('webhook') if ctx.instance is not None else '') or '').strip()


def send(ctx: Context, message: dict) -> None:
    call(ctx.http(), 'POST', webhook_of(ctx), body=cards.render(message), timeout=TIMEOUT_S)


def post(ctx: Context, message: dict) -> dict:
    if not sendable(webhook_of(ctx)):
        return {'ok': False, 'status': 0}
    try:
        send(ctx, message)
    except HttpError as e:
        return {'ok': False, 'status': e.code}
    return {'ok': True, 'status': 200}


def tested(ctx: Context) -> str:
    try:
        send(ctx, {'kind': 'test', 'title': msg('routes.test_title'), 'text': msg('routes.test')})
    except HttpError as e:
        if e.code == 403:
            raise ReadError(403, msg('teams.denied', status=403)) from None
        if e.code == 404:
            raise ReadError(404, msg('teams.gone')) from None
        raise
    return TOOK


def test(ctx: Context) -> TestResult:
    config = ctx.instance.config if ctx.instance is not None else {}
    if not sendable(webhook_of(ctx)):
        return TestResult(False, msg('integrations.route_link'), 0)
    if not str(config.get('tags') or '').strip() and not str(config.get('keys') or '').strip():
        return TestResult(False, msg('integrations.route_empty'), 0)
    return connection.result(lambda: tested(ctx))


PLUGIN = Plugin(
    id=ID, name='Microsoft Teams', version='0.1.0', category='chat',
    description='Posts the cards a route takes to a Microsoft Teams channel through the Workflows URL configured for it.', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.6', edition='community', capabilities=Capabilities(hosts=(), secrets=('webhook',)),
    fields=(Field('webhook', 'Webhook', 'secret', required=True, help='The https address Teams Workflows gives for posting to the channel; it is kept secret.'),
            Field('tags', 'Card tags', 'text', check=tags.terms_problem, suggest=tags.suggested, separator=',', help='Comma list of card tag ids or names this route takes, such as infra, developer. A route needs card tags or card keys.'),
            Field('mentions', 'Mentions', 'text', help='Comma list of email addresses mentioned in each post.'),
            Field('keys', 'Card keys', 'text', help='Comma list of card key patterns this route takes, with * and ?, such as log|checkout-*|*.'),
            Field('min-sev', 'Lowest severity', 'choice', choices=SEVS, choice_labels={'warn': 'Warning', 'high': 'High', 'crit': 'Critical'}, help='Cards below it are not posted on their own; empty uses the Teams lowest severity setting.')),
    settings=(Setting('repeat-days', 'Teams repeat days', 'Days before a card already posted to a channel is posted there again when it fires again or gets another tag; 0 posts every time',
                      'float', '7', low=0, high=365),
              Setting('min-sev', 'Teams lowest severity', 'Cards below this severity are not posted to a Teams channel on their own; it is also the default of a key route with no lowest severity of its own, and a key route\'s own lowest severity replaces it, higher or lower. While it is not set, tag routes take cards from Warning up and key routes from High up',
                      'choice', 'high', choices=SEVS),
              Setting('fixed-posts', 'Teams fixed posts', 'Posts a fixed message to a business channel when a card it was told about as info closes after that post', 'bool', 'false')),
    test=test, outbound=Outbound(post, 'tags', 'mentions', link_field='webhook', keys_field='keys', sev_field='min-sev'),
    setup=Setup(steps=(
        "In Teams open Workflows (the Workflows app, or a channel's More options > Workflows) and create a flow that receives a webhook request and posts to a channel. Pick the team and channel, then Save.",
        'Each flow type has its own rule for who may trigger it; pick one that lets the Skopax server post.',
        "On the flow's details page copy the HTTP POST URL and paste it into Webhook. The URL is a secret: anyone who has it can post to the channel.",
        'Fill in Card tags or Card keys, since a route needs one of them, then choose Test connection. The workflow owner must be allowed to post in that channel.',
    ), docs='https://support.microsoft.com/en-us/teams/apps-service/create-incoming-webhooks-with-workflows-for-microsoft-teams', docs_label='Microsoft Teams: webhooks with Workflows'),
)
