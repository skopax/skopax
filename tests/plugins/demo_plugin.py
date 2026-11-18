from skopax.contract import (AgentRead, Capabilities, Check, Field, Hooks, Knowledge, Locale, Migration, Panel, PassResult, Plugin, Redaction, RuleSet, Setting, Sweep, TagRule, TestResult, Tool)

SOURCE: dict = {}
BUILT: list = []


def env_instances(env):
    return [{'name': 'main', 'url': env['DEMO_URL']}] if env.get('DEMO_URL') else []


def test(ctx):
    return TestResult(True, 'demo answered', 1)


def pulse(ctx):
    return Sweep(PassResult('pulse', True, 'ok', [], {}, ('demo',)))


def echo(ctx, args):
    return {'echo': args.get('text', '')}


def write(ctx, args):
    return {'wrote': args.get('text', '')}


def propose(ctx, args):
    return {'title': args.get('title', ''), 'text': args.get('text', '')}


def confirm(ctx, args):
    return {'ok': True, 'summary': 'demo confirmed ' + str(args.get('title', ''))}


def links(card, instance):
    return [{'label': 'Demo', 'url': 'https://example.com/demo'}]


def row_links(row, window, instance):
    return {'demo': 'https://example.com/demo'}


def probe_targets(instance):
    return [{'name': 'demo', 'url': 'https://example.com/health'}]


def role_resolver(user, channel):
    return 'viewer' if channel == 'mcp' else None


def settings_source(env):
    return {'label': 'demo source', 'values': dict(SOURCE.get('values', {})), 'instances': list(SOURCE.get('instances', []))}


def oidc_token_request(request):
    return {'headers': {'X-Demo': '1'}}


def org_validators():
    return {'link_hosts': ['example.com'], 'email_domains': ['example.com'], 'ticket_projects': ['OPS']}


def entitlement():
    return 'community'


def migrate_1(db):
    db['demo_state'].replace_one({'_id': 'seed'}, {'_id': 'seed', 'v': 1}, upsert=True)


def migrate_2(db):
    db['demo_state'].update_one({'_id': 'seed'}, {'$set': {'v': 2}}, upsert=True)


def agent_sources(config):
    return ['demo']


def agent_echo(config, args):
    return {'echo': args.get('text', '')}


class DemoClient:
    def __init__(self, info, transport):
        self.url = info.config.get('url', '')
        self.transport = transport

    def ping(self):
        return self.url

    def _probe_private(self):
        return self.url


def client(info, transport):
    BUILT.append(info.name)
    return DemoClient(info, transport)


PLUGIN = Plugin(
    id='demo', name='Demo', version='0.1.0', category='other', description='Declares every contribution with tiny handlers.', icon='demo-icon.svg', author='Skopax contributors', licence='Apache-2.0',
    core='>=0.1,<1', capabilities=Capabilities(hosts=('@url',), secrets=('token',), agent_reads=True),
    fields=(Field('url', 'URL', 'url', required=True, ui=True), Field('token', 'Token', 'secret'), Field('ca', 'CA certificate', 'pem'), Field('mode', 'Mode', 'choice', default='fast', choices=('fast', 'slow'))),
    env_instances=env_instances, test=test, client=client,
    checks=(Check('pulse', 'Demo pulse', 900, 0, ('demo',), pulse, tool='demo_echo'),),
    rules=(RuleSet('limits', 'Demo limits', {'warn': 5.0, 'crit': 10.0}, {'warn': 'Warn at this level'}),),
    settings=(Setting('depth', 'Depth', 'How deep a check looks', 'int', '3', low=1, high=10),),
    tools=(Tool('demo_echo', 'Echo text back', {'type': 'object', 'properties': {'text': {'type': 'string'}}}, echo), Tool('demo_write', 'Write text somewhere', {'type': 'object', 'properties': {'text': {'type': 'string'}}}, write, kind='write'),
           Tool('demo_propose', 'Propose a demo change', {'type': 'object', 'properties': {'title': {'type': 'string'}, 'text': {'type': 'string'}}}, propose, kind='propose', confirm=confirm)),
    links=links, row_links=row_links, probe_targets=probe_targets, tag_rules=(TagRule('demo', ('demo',)),), panels=(Panel('demo', 'demo-panel.js'),),
    knowledge=(Knowledge('demo-notes', 'Demo notes', 'Demo notes for the investigator.'),), locales=(Locale('de', {'Demo': 'Demo'}, {'slack.open': 'In {product} oeffnen', 'slackevents.off_topic': 'Dabei kann ich nicht helfen; ich beantworte nur Fragen zu {product} und den Systemen, die es beobachtet.'}, writing='Write in German.'),),
    hooks=Hooks(role_resolver=role_resolver, settings_source=settings_source, oidc_token_request=oidc_token_request, org_validators=org_validators, entitlement=entitlement),
    migrations=(Migration(1, 'seed', migrate_1), Migration(2, 'bump', migrate_2)),
    agent_reads=(AgentRead('demo-echo', ('text',), ('url', 'token'), agent_sources, agent_echo),), redactions=(Redaction('ticket-id', r'DEMO-\d{4}'),),
)
