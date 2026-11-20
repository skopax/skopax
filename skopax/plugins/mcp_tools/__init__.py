from skopax.contract import Capabilities, Field, Plugin, Setting, Setup

ID = 'mcp-tools'
PRESETS = ('atlassian', 'notion', 'github', 'google-drive')
PRESET_NAMES = {'atlassian': 'Atlassian (Jira and Confluence)', 'notion': 'Notion', 'github': 'GitHub', 'google-drive': 'Google Drive'}

PLUGIN = Plugin(
    id=ID, name='MCP server', version='0.1.0', category='other',
    description='Adds the tools of an outside MCP server to the investigator and the chat, and can search one folder of company documents', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.5', edition='community', capabilities=Capabilities(hosts=('@url',), secrets=('header-value', 'oauth-client-secret')),
    max_instances=20,
    setup=Setup(
        steps=(
            'Get the streamable HTTP address of the MCP server from its vendor or your platform team and paste it into URL. It must be https.',
            'Token in a header: create an access token on the server with read permissions only. Set Method to Token in a header, keep Header name as Authorization, and write Bearer, a space and the token in Header value.',
            'OAuth: set Method to OAuth and save, then choose Connect in the connection panel and sign in as an account that can read what the investigator should see. Skopax finds the sign-in details the server publishes.',
            'OAuth scopes: list scopes separated by spaces, or leave it empty. If the server does not register clients itself, create an OAuth client in its admin console, register <SKOPAX_BASE_URL>/api/admin/mcp-tools/oauth/callback as the redirect address, and enter OAuth client id and OAuth client secret.',
            'Test connection lists the tools. Then turn on only the read tools you want in the panel; a new connection starts off.',
        ),
        docs='https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization', docs_label='MCP: authorization'),
    fields=(Field('url', 'URL', 'url', required=True, group='connection', help='The streamable HTTP endpoint of the MCP server.'),
            Field('preset', 'Catalog entry', 'choice', choices=PRESETS, choice_labels=PRESET_NAMES, group='connection', help='Set by the catalog entry you added it from.'),
            Field('auth', 'Method', 'choice', default='none', choices=('none', 'header', 'oauth'),
                  choice_labels={'none': 'No sign-in', 'header': 'Token in a header', 'oauth': 'OAuth'}, group='sign-in', help='No sign-in, a header with a token, or OAuth.'),
            Field('header-name', 'Header name', 'text', default='Authorization', show_if=('auth', 'header'), group='sign-in', help='The header that carries the token.'),
            Field('header-value', 'Header value', 'secret', required=True, show_if=('auth', 'header'), group='sign-in', help='For example Bearer and the token.'),
            Field('oauth-scopes', 'OAuth scopes', 'text', show_if=('auth', 'oauth'), group='sign-in', help='Scopes to ask for, separated by spaces.'),
            Field('oauth-client-id', 'OAuth client id', 'text', show_if=('auth', 'oauth'), group='sign-in', help='Only for servers that register no clients themselves.'),
            Field('oauth-client-secret', 'OAuth client secret', 'secret', show_if=('auth', 'oauth'), group='sign-in', help='Only for servers that register no clients themselves.'),
            Field('chat-role', 'Who may use it in chat and asks', 'choice', default='operator', choices=('viewer', 'operator', 'admin'),
                  choice_labels={'viewer': 'Everyone', 'operator': 'Operators and admins', 'admin': 'Admins only'}, group='access', help='Card runs always may.'),
            Field('knowledge', 'Knowledge source', 'bool', default='false', group='access', help='Works only for a catalog entry with a ready adapter.'),
            Field('ca', 'CA certificate', 'pem', group='advanced', help='PEM certificate to trust instead of the system roots.')),
    settings=(Setting('allow-local-http', 'Allow plain http to this machine', 'Lets a connection use http://localhost; for local testing only', 'bool', 'false'),),
)
