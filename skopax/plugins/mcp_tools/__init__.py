from skopax.contract import Capabilities, Field, Plugin, Setting

ID = 'mcp-tools'
PRESETS = ('atlassian', 'notion', 'github', 'google-drive')

PLUGIN = Plugin(
    id=ID, name='MCP server', version='0.1.0', category='other',
    description='Adds the tools of an outside MCP server to the investigator and the chat, and can search one folder of company documents', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.2', edition='community', capabilities=Capabilities(hosts=('@url',), secrets=('header-value', 'oauth-client-secret')),
    max_instances=20,
    fields=(Field('url', 'URL', 'url', required=True, help='The streamable HTTP endpoint of the MCP server.'),
            Field('preset', 'Catalog entry', 'choice', choices=PRESETS, help='Set by the catalog entry you added it from.'),
            Field('auth', 'Sign-in', 'choice', default='none', choices=('none', 'header', 'oauth'), help='No sign-in, a header with a token, or OAuth.'),
            Field('header-name', 'Header name', 'text', default='Authorization', show_if=('auth', 'header'), help='The header that carries the token.'),
            Field('header-value', 'Header value', 'secret', required=True, show_if=('auth', 'header'), help='For example Bearer and the token.'),
            Field('oauth-scopes', 'OAuth scopes', 'text', show_if=('auth', 'oauth'), help='Scopes to ask for, separated by spaces.'),
            Field('oauth-client-id', 'OAuth client id', 'text', show_if=('auth', 'oauth'), help='Only for servers that register no clients themselves.'),
            Field('oauth-client-secret', 'OAuth client secret', 'secret', show_if=('auth', 'oauth'), help='Only for servers that register no clients themselves.'),
            Field('chat-role', 'Who may use it in chat and asks', 'choice', default='operator', choices=('viewer', 'operator', 'admin'), help='Card runs always may.'),
            Field('knowledge', 'Knowledge source', 'bool', default='false', help='Works only for a catalog entry with a ready adapter.'),
            Field('ca', 'CA certificate', 'pem', help='PEM certificate to trust instead of the system roots.')),
    settings=(Setting('allow-local-http', 'Allow plain http to this machine', 'Lets a connection use http://localhost; for local testing only', 'bool', 'false'),),
)
