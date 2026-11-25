import dataclasses
import functools
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from pymongo import DESCENDING, ReturnDocument

from skopax import brand, clock as zone_clock, jirafields, logs, settingcards, tags
from skopax.config import ALERT_GLOB, ALERT_GLOBS_MAX, AI_PROVIDERS, DEFAULT_ROLES, JIRA_BACKLINK_MODES, INVESTIGATE_MODES, ORG_ABOUT_MAX, SETTING_TEXT_MAX, SLACK_LISTEN_MODES, SLACK_POST_SEV, SLACK_POST_SEVS, SSO_CLAIM_MODES, retired_info, SSO_CLIENT_AUTH, ConfigurationError, Settings, load_settings, release_problem, resolve, TRACE_CONTENTS
from skopax.config import MODEL_KEYS, SSO_BUTTON_ICONS, SSO_BUTTON_TEXT_MAX, WORKSPACE_ICONS, read_token_headers, token_headers_reason
from skopax.contract import Plugin
from skopax.errkind import Authored, kind as error_kind
from skopax.features import BY_KEY as FEATURE_OF
from skopax.ai import PROVIDERS, explicit, extra_models, label_of, provider_of_ref
from skopax.investigator.pricing import ALL_MODELS, OPENAI, default_model, known as known_model, provider_of, resolve as resolve_model
from skopax.investigator import web as web_sites
from skopax.investigator.prompt import writing_languages
from skopax.messages import languages, msg
from skopax.oidc import url_problem
from skopax.probe import CERT_RULES, EXTRA_KEY, EXTRA_MAX, EXTRA_NAME_MAX, SKIP_ENTRY, read_extra, skip_entry_ok
from skopax.rulehelp import RULE_HELP
from skopax.store import iso, utc_now

log = logging.getLogger('skopax.tunables')

MODELS = ALL_MODELS
SAME_AS_CARD = 'Same as Card model'
OVERRIDE_SEVS = {'investigate.model-critical': 'Critical', 'investigate.model-high': 'High'}
MODEL_ROLES = {'investigate.model': 'card', 'investigate.model-critical': 'card', 'investigate.model-high': 'card', 'investigate.slack-model': 'slack', 'chat.model': 'chat', 'investigate.gate-model': 'gate', 'self-review.model': 'review'}
MODEL_FIELDS = frozenset(f for _, f in MODEL_KEYS.values())
OFF_WITH = {OPENAI: {'investigate.web-fetches': 'ai.off_openai', 'investigate.gate-effort': 'ai.off_openai_gate_effort'}}
OFF_USES = {'investigate.web-fetches': ('investigate_model', 'card'), 'investigate.gate-effort': ('investigate_gate_model', 'gate')}
CHAT_CALLS_KEY = 'chat.max-tool-calls'
WEB_DOMAINS_KEY = 'investigate.web-domains'
OWN_SITES_KEY = 'core.own-sites'
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
SEVS = ('info', 'warn', 'high', 'crit')
REFRESH_S = 15
TEXT_LIMIT = 1000
TEXT_LIMITS = {'core.org-about': ORG_ABOUT_MAX, EXTRA_KEY: 4000}
TEXT_LIMITS['sso.button-text'] = SSO_BUTTON_TEXT_MAX
TEXT_LINES = {'core.org-about': 8, EXTRA_KEY: 6}
INPUT_LIMIT = 4000
TEXT_BOXES = ('text', 'list', 'tagrules')
TERM_LIMIT = 200
HISTORY_LIMIT = 200
ACTIVITY_VALUE = 80
REV_ID = 'rev'
UPGRADE_MARK = 'upgrade:ai-uses'
UPGRADE_BY = 'upgrade'
UPGRADE_PENDING = 'upgrade:ai-uses:pending'
NO_COLOR_SHOWN = 'off (NO_COLOR)'


class Invalid(Authored, ValueError):
    pass


@dataclass(frozen=True)
class Tunable:
    key: str
    field: str
    group: str
    label: str
    help: str
    kind: str
    low: float | None = None
    high: float | None = None
    choices: tuple | Callable[[], tuple[str, ...]] = ()
    live: bool = True
    defaults: dict | None = None
    part: str | None = None
    plugin: str | None = None
    default: str | None = None
    rule_help: Mapping[str, str] | None = None
    hidden: bool = False
    choice_labels: Mapping[str, str] | None = None
    items: str | None = None


def T(key, field, group, label, help, kind, low=None, high=None, choices=(), live=True, defaults=None, part=None, hidden=False, choice_labels=None, items=None) -> Tunable:
    return Tunable(key, field, group, label, help, kind, low, high, choices if callable(choices) else tuple(choices), live, defaults, part, hidden=hidden, choice_labels=choice_labels, items=items)


def choices_of(t: Tunable) -> tuple[str, ...]:
    return tuple(t.choices()) if callable(t.choices) else t.choices


def is_model_setting(t: Tunable) -> bool:
    offered = set(choices_of(t)) - {''}
    return bool(offered) and offered <= set(MODELS)


def kept_ref(t: Tunable, text: str) -> bool:
    return is_model_setting(t) and str(provider_of_ref(text) or '').startswith('compat:') and len(text) <= 300 and not any(c.isspace() for c in text)


def extra_models_of(t: Tunable) -> tuple[str, ...]:
    return extra_models() if is_model_setting(t) else ()


def offered_tag(t: Tunable, text: str) -> str:
    if not callable(t.choices) or not text:
        return text
    return next((choice for choice in choices_of(t) if choice.lower() == text.lower()), text)


JIRA_FIELD_KEYS = ('jira.scope-field', 'jira.points-field', 'jira.developer-field', 'jira.sprint-field')
TRACE_INSTANCE_KEY = 'langfuse.trace-instance'
TRACE_NAMES: list[Callable[[], tuple[str, ...]]] = [tuple]


def trace_instances() -> tuple[str, ...]:
    try:
        return ('',) + tuple(n for n in TRACE_NAMES[0]() if n)
    except Exception as e:
        log.warning('the Langfuse instances were not listed: %s', error_kind(e))
        return ('',)


def card_languages() -> tuple[str, ...]:
    return tuple(dict.fromkeys(('en',) + writing_languages()))


HTTPS_KEYS = ()
SLACK_CHANNEL_KEY = 'slack.channel'
POST_SEV_KEY = 'slack.post-sev'
ROUTE_SEV = 'min-sev'
CRIT_NOW_KEY = 'slack.crit-now'
POST_SEV_LABELS = {'crit': 'Critical', 'high': 'High', 'warn': 'Warning', 'off': 'Off'}
RENAMED = {CRIT_NOW_KEY: POST_SEV_KEY}
OLD_HOURS_KEYS = ('core.auto-resolve-hours', 'core.auto-resolve-days')
SEV_HOURS_KEYS = {'core.auto-resolve-hours-crit': ('auto_resolve_hours_crit', 'crit'), 'core.auto-resolve-hours-high': ('auto_resolve_hours_high', 'high'),
                  'core.auto-resolve-hours-warn': ('auto_resolve_hours_warn', 'warn')}
INFO_DAYS_KEY = 'core.info-auto-close-days'
TIMEZONE_KEY = 'general.timezone'
TAG_REPORT_KEY = 'tags.report-at'
WARN_DIGEST_KEY = 'slack.warn-digest-at'
DAILY_AT_KEYS = (TAG_REPORT_KEY, WARN_DIGEST_KEY)
RELEASE_NAME_KEY = 'jira.release-name'
LINKS_KEY = 'core.workspace-links'
OLD_LINKS_KEY = 'core.instance-links'
KEY_ALIASES = {OLD_LINKS_KEY: LINKS_KEY}
RENAME_MARK = f'rename:{LINKS_KEY}'
LINKS_MAX = 10
ITEM_RULES = {
    'email': (r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '', ''),
    'host': (SKIP_ENTRY.pattern, 'i', '.'),
    'web-host': (web_sites.DOMAIN.pattern, 'i', ''),
    'own-site': (web_sites.DOMAIN.pattern, 'i', ''),
    'domain': (r'(?=.{1,253}$)(?!.*\.[0-9]+$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', 'i', ''),
    'slack-user-id': (r'[UW][A-Z0-9]{6,14}', '', ''),
    'client-id': (r'[^\s,]{1,200}', '', ''),
    'project-key': (r'[A-Za-z][A-Za-z0-9_]{0,49}', '', ''),
    'channel': (r'[CG][A-Z0-9]{8,12}', '', ''),
    'scope': (r'[\x21-\x7e]{1,100}', '', ''),
    'alert-name': (ALERT_GLOB.pattern, '', ''),
}
ITEM_RULES_CHECKED = ('host', 'web-host', 'own-site', 'email', 'slack-user-id', 'client-id', 'project-key', 'channel', 'scope', 'alert-name')
ALERTS_WARN_KEY = 'alerts.warn'
LINK_NAME_MAX = 40
LINK_NAME = re.compile(r'[A-Za-z0-9 ._-]{1,%d}' % LINK_NAME_MAX)
LINK_COLOURS = ('#e3a744', '#9a7ce3', '#5cbf80', '#f0857a', '#7eaaf2', '#c98500')
GENERAL, ACCESS, SIGNIN, INV, SLACK, REVIEW, CARDS, JIRA, RULES, TAGS, FEATURES = ('General', 'Access', 'Sign-in', 'Investigator', 'Slack', 'Self-review', 'Cards', 'Jira tasks',
                                                                           'Monitor rules', 'Card tags', 'Features')
INSTANCES = 'Workspaces'
ITEM_SEPARATORS = {'scope': ' '}
ITEM_KEYWORDS = {'channel': ('all', 'main'), 'web-host': (web_sites.ANY,) + web_sites.OFF_WORDS}
ITEM_MINUS = ('web-host',)
ITEM_LOOSE = ('own-site',)
LINK_RULES = ('count', 'pair', 'name', 'url', 'userinfo', 'repeat')
MCP = 'MCP'
GROUPS = (GENERAL, INSTANCES, ACCESS, SIGNIN, MCP, INV, SLACK, REVIEW, CARDS, JIRA, RULES, TAGS, FEATURES)
SSO_URL_KEYS = ('sso.issuer', 'sso.authorize-url', 'sso.token-url', 'sso.jwks-url')
MCP_URL_KEYS = ('mcp.issuer', 'mcp.resource-url')
MCP_RESOURCE_KEY = 'mcp.resource-url'
SESSIONS_AFTER_KEY = 'sso.sessions-after'
SSO_SWITCH_KEY = 'sso.enabled'
TOKEN_HEADERS_KEY = 'sso.extra-token-headers'
RULE_SETS = (('core.certs', 'cert_rules', 'Certificates', CERT_RULES),)
CERT_SKIP_KEY = 'core.cert-skip'
SKIP_SHOWN = 80

def tag_tunable(tag: str, label: str) -> Tunable:
    return T(f'tags.{tag}.rules', 'tag_rules', TAGS, label, 'Cards tagged ' + label + ' by rule: type:<signal or source>, gone (a removed page) or a card key pattern, separated by commas', 'tagrules', part=tag)


def feature_tunable(key: str, field: str) -> Tunable:
    feature = FEATURE_OF[key]
    return T(key, field, FEATURES, feature.label, feature.help, 'bool')


SIGN_IN_ROLES = (
    T('sso.roles-claim', 'sso_roles_claim', SIGNIN, 'Roles claim', 'Where the role values sit in the identity provider token: a claim name such as roles, groups or https://example.com/roles, '
      'or a dot path such as realm_access.roles. A text value is one value and a list gives its text items', 'text'),
    T('sso.role-map', 'sso_role_map', SIGNIN, 'Role map', 'Which claim value gives which role, as a JSON list such as [{"value": "ops-admins", "role": "admin"}]; at most 50 pairs, '
      'matched exactly, and several matches give the highest role. Empty uses the admin and operator role settings. A plug-in role resolver comes first', 'rolemap'),
    T('sso.default-role', 'sso_default_role', SIGNIN, 'Default role', 'The role of a person who signs in with SSO when no plug-in, claim or Admin-set role gives one: viewer or operator. '
      'Admins come from the role map', 'choice', choices=DEFAULT_ROLES),
    T('sso.new-accounts', 'sso_new_accounts', SIGNIN, 'New accounts', 'Who may sign in with SSO: anyone the identity provider signs in gets an account, or added-only lets in only people who already have one, '
      'added in Admin or by an earlier sign-in. With a public provider, anyone means any account there', 'choice', choices=('anyone', 'added-only')),
    T('sso.allowed-domains', 'sso_allowed_domains', SIGNIN, 'Allowed email domains', 'Email domains that may sign in with SSO, such as example.com, separated by commas; matched exactly, without wildcards. '
      'Empty lets every domain in', 'list', items='domain'),
    T('roles.stale-max-minutes', 'roles_stale_max_minutes', SIGNIN, 'Last known role (minutes)', 'While a plug-in role resolver fails, a person whose role that resolver gave in the last N minutes '
      'signs in with that role, and {product} logs a warning; anyone else is refused. 0 refuses everyone while the resolver fails', 'int', 0, 1440),
)


MCP_ACCESS = (
    T('mcp.client-id', 'mcp_client_id', MCP, 'MCP client id', 'The client id MCP clients sign in with at the identity provider; empty uses skopax-mcp', 'text'),
    T('mcp.audience', 'mcp_audience', MCP, 'MCP audience', 'The audience an MCP access token must name, unless it names the MCP address itself; empty uses the MCP client id', 'text'),
    T('mcp.required-role', 'mcp_required_role', MCP, 'MCP required role', 'A value the MCP access token must carry in the roles claim set on the Single sign-on card before {product} looks at the person; empty lets any token through', 'text'),
    T('mcp.issuer', 'mcp_issuer', MCP, 'MCP issuer', 'The sign-in address MCP clients are sent to, which tokens may also name as their issuer; empty uses the origin of the authorize URL, else the issuer URL', 'text'),
    T('mcp.scope', 'mcp_scope', MCP, 'MCP scope', 'The scope MCP clients ask for; empty uses mcp', 'text'),
    T('mcp.resource-url', 'mcp_resource_url', MCP, 'Skopax address for MCP (without /mcp)', 'The public address of {product} that MCP clients see, when it differs from the base URL', 'text'),
    T('mcp.allowed-clients', 'mcp_allowed_clients', MCP, 'MCP allowed clients', 'Client ids whose MCP access tokens are taken, separated by commas; a token then needs azp, client_id or cid to name one of them. Empty takes any client', 'list', items='client-id'),
)


STATIC = (
    T('general.timezone', 'timezone', GENERAL, 'Timezone', 'IANA timezone name, such as UTC or Europe/Berlin: every time {product} shows and every day boundary (daily spend cap, cost buckets) follows it', 'text'),
    T('core.language', 'language', GENERAL, 'Outbound language', 'Language of what {product} sends to Slack, Teams, Jira and direct messages: en, or a language a locale pack adds', 'choice', choices=languages),
    T(LINKS_KEY, 'workspace_links', INSTANCES, 'Other workspaces', 'Other workspaces of {product} that the workspace switcher at the top of the sidebar offers, as name=url pairs separated by commas, '
      f'such as staging=https://staging.example.com; at most {LINKS_MAX}. A name is 1 to {LINK_NAME_MAX} letters, digits, spaces, dots, dashes or underscores, '
      'and an address is http:// or https:// without a user name or password', 'text'),
    T('core.workspace-icon', 'workspace_icon', INSTANCES, 'Workspace icon', 'The picture on the workspace tile at the top of the sidebar: letter shows the first letter of the workspace name, company shows the image uploaded on the Single sign-on card, '
      'upload shows the image uploaded on the Workspaces page. Without an image the letter is shown', 'choice', choices=WORKSPACE_ICONS),
    T('core.log-format', 'log_format', GENERAL, 'Log format', 'How {product} writes its log lines: text (one readable line per record), json (one flat JSON object per line, for a log store) '
      'or log4j (the Log4j JSON layout). Lines written at start, before the settings are read, follow SKOPAX_LOG_FORMAT', 'choice', choices=logs.FORMATS),
    T('core.log-color', 'log_color', GENERAL, 'Log colour', 'Colour of text log lines: always, auto (only when the log goes to a terminal) or never. The JSON formats never carry colour', 'choice',
      choices=logs.COLORS),
    T('core.log-level', 'log_level', GENERAL, 'Log level', 'The lowest level written to the log: DEBUG, INFO, WARNING or ERROR', 'choice', choices=logs.LEVEL_NAMES),
    T('access.public-read', 'public_read', ACCESS, 'Public read', 'Let anyone who can reach the page read the board without logging in. Changes still need a login.', 'bool'),
    T('sso.enabled', 'sso_on', SIGNIN, 'Single sign-on', 'Lets people sign in through the identity provider; switching it off ends single sign-on sessions within 30 seconds and turns MCP off, while password logins go on. API tokens made from single sign-on sessions are refused and stay refused after switching back on; make new ones afterwards', 'bool'),
    T('sso.issuer', 'sso_issuer', SIGNIN, 'Issuer URL', 'The issuer URL of the identity provider, whose /.well-known/openid-configuration document gives the endpoints', 'text'),
    T('sso.client-id', 'sso_client_id', SIGNIN, 'Client ID', 'The client id {product} signs in with at the identity provider', 'text'),
    T('sso.scope', 'sso_scope', SIGNIN, 'Scope', 'The scopes a sign-in asks for, separated by spaces; it must include openid unless the claims come from the access token', 'text', items='scope'),
    T('sso.claims', 'sso_claims', SIGNIN, 'Claims come from', 'Which token gives the email, name and roles: id-token, or access-token for providers that put the roles only there', 'choice', choices=SSO_CLAIM_MODES),
    T('sso.client-auth', 'sso_client_auth', SIGNIN, 'Client authentication', 'How the client secret reaches the token endpoint: auto follows what the provider lists, post sends it in the form and basic in an Authorization header', 'choice',
      choices=SSO_CLIENT_AUTH),
    T('sso.require-verified-email', 'sso_require_verified_email', SIGNIN, 'Require verified email', 'Refuses a sign-in whose token does not say email_verified is true; when off, only an email_verified of false is refused. The first sign-in to an account added in Admin, and every sign-in while Allowed email domains or New accounts limits who gets in, always needs a verified email', 'bool'),
    T('sso.authorize-url', 'sso_authorize_url', SIGNIN, 'Authorize URL', 'Replaces the authorization endpoint the provider lists; empty uses the listed one', 'text'),
    T('sso.token-url', 'sso_token_url', SIGNIN, 'Token URL', 'Replaces the token endpoint the provider lists; empty uses the listed one', 'text'),
    T('sso.extra-token-headers', 'sso_extra_token_headers', SIGNIN, 'Extra token request headers', 'Headers added to the token request only, such as an API gateway key name or a channel, '
      'as Name: value pairs separated by semicolons; at most 10. Authorization, Proxy-Authorization, Cookie, Host, Content-Length, Content-Type, Accept, Accept-Encoding, '
      'Transfer-Encoding, Connection, Expect, TE, Upgrade, Keep-Alive, Trailer, Forwarded, Origin and X-Forwarded-* headers cannot be set. The values are shown in Admin and kept in plain text, so a secret does not belong here', 'text'),
    T('sso.jwks-url', 'sso_jwks_url', SIGNIN, 'Keys (JWKS) URL', 'Replaces the signing keys (JWKS) address the provider lists; empty uses the listed one', 'text'),
    T('sso.button-text', 'sso_button_text', SIGNIN, 'Button text', 'The text of the single sign-on button on the login page, at most 40 characters, such as Sign in with Acme; empty uses Sign in with SSO', 'text'),
    T('sso.button-icon', 'sso_button_icon', SIGNIN, 'Button icon', 'The icon of the single sign-on button: auto picks a mark from the issuer host, company shows the image uploaded on the Single sign-on card, none shows the text only', 'choice', choices=SSO_BUTTON_ICONS),
    T('investigate.provider', 'ai_provider', INV, 'Default provider', 'The AI provider, anthropic or openai, whose default model serves each use whose model is not set. Each use can pick a model of any provider that has a key; '
      'a use whose provider has no working key falls back to the default model of this provider for that use, and Admin says so. A change applies from the next run or ask', 'choice', choices=AI_PROVIDERS),
    T('core.allow-private-ai', 'allow_private_ai', INV, 'Private AI addresses', 'Lets an OpenAI-compatible AI connection point at a private address, for example a local Ollama', 'bool'),
    T('investigate.model', 'investigate_model', INV, 'Card model', 'Model for Warning and Info cards, and for Critical and High cards that have no model of their own below. Also used for page asks and deploy-report assessments', 'choice', choices=MODELS),
    T('investigate.model-critical', 'investigate_model_critical', INV, 'Model for Critical cards', 'Model for cards whose worst alert is Critical; empty uses the Card model', 'choice',
      choices=('',) + MODELS, choice_labels={'': SAME_AS_CARD}),
    T('investigate.model-high', 'investigate_model_high', INV, 'Model for High cards', 'Model for cards whose worst alert is High; empty uses the Card model', 'choice',
      choices=('',) + MODELS, choice_labels={'': SAME_AS_CARD}),
    T('investigate.effort', 'investigate_effort', INV, 'Card effort', 'Thinking effort of the card model', 'choice', choices=EFFORTS),
    T('investigate.slack-model', 'investigate_slack_model', INV, 'Slack model', 'Model for questions asked in Slack', 'choice', choices=MODELS),
    T('investigate.language', 'investigate_language', INV, 'Card language', 'Language of what the investigator writes on cards, in its Slack posts and on deploy reports: en (English), or a language a locale pack adds. Answers follow the language of the question', 'choice', choices=card_languages),
    T('chat.model', 'chat_model', INV, 'Chat model', 'Model for messages on the Chat page', 'choice', choices=MODELS),
    T('chat.effort', 'chat_effort', INV, 'Chat effort', 'Thinking effort of the chat model; empty follows the Slack effort', 'choice', choices=('',) + EFFORTS, choice_labels={'': 'Same as Slack'}),
    T('chat.max-tool-calls', 'chat_max_tool_calls', INV, 'Chat tool calls', 'Most tool calls one chat answer may use, 3 to 30; 0 follows the Slack cap', 'int', 0, 30),
    T('page.ask-cap', 'page_ask_cap', INV, 'Page asks per hour', 'Ask the investigator questions from the page per hour, everyone together; re-assessing a deploy report has its own count with the same limit; 0 turns both off', 'int', 0, 100),
    T('chat.max-chars', 'chat_max_chars', INV, 'Chat message length (characters)', 'The longest message one person can send: the Chat page, Slack direct messages and Ask the investigator on the page. Longer messages make bigger prompts, so each costs a little more', 'int', 100, 8000),
    T('chat.daily-cap', 'chat_daily_cap', INV, 'Chat messages per person per day', 'Messages one person can send on the Chat page per day; 0 turns the chat off', 'int', 0, 500),
    T('mcp.dive-cap', 'mcp_dive_cap', INV, 'MCP investigations per person per hour', 'Investigate calls one person can start over MCP per hour; 0 turns MCP investigations off. MCP questions count against the chat messages per day', 'int', 0, 50),
    T('mcp.wait-seconds', 'mcp_wait_seconds', INV, 'MCP wait (seconds)', 'How long an ask or investigate call over MCP waits for the answer before it hands back the chat or run to read later; 0 never waits', 'int', 0, 55),
    T('investigate.slack-effort', 'investigate_slack_effort', INV, 'Slack effort', 'Thinking effort of the Slack model', 'choice', choices=EFFORTS),
    T('investigate.gate-model', 'investigate_gate_model', INV, 'Topic gate model', 'Checks that a page or Slack ask is about {product}, and answers small talk', 'choice', choices=MODELS),
    T('investigate.gate-effort', 'investigate_gate_effort', INV, 'Topic gate effort', 'Effort of the topic gate model on every page, chat and Slack ask; low keeps it fast and cheap', 'choice', choices=EFFORTS),
    T('core.org-about', 'org_about', INV, 'About your company', 'A few sentences about the company and what {product} watches: products, main services, who uses them. Given to the investigator and the topic gate', 'text'),
    T('investigate.daily-usd', 'investigate_daily_usd', INV, 'Daily limit ($)', 'Daily spend cap shared by the investigator and the self-review; 0 stops them all', 'float', 0, 500),
    T('investigate.max-cards', 'investigate_max_cards', INV, 'Cards per run', 'How many open cards one investigator run takes at most', 'int', 1, 20),
    T('investigate.refire-hours', 'investigate_refire_hours', INV, 'Hours before a reopened card is investigated again',
      'A warning card that clears and comes back keeps its earlier finding for this long instead of being investigated again. Critical cards and cards that got worse are always investigated', 'float', 1, 720),
    T('investigate.follow-up-hours', 'investigate_follow_up_hours', INV, 'Hours between follow-up checks',
      'How long a waiting card waits before the investigator checks its follow-up again: the done-condition of a fix outside {product}, or the settle review of a card past its auto-resolve window', 'int', 1, 72),
    T('investigate.follow-up-days', 'investigate_follow_up_days', INV, 'Days of follow-up checks', 'After this many days of checks that found the fix not done yet, the follow-up check gives up and leaves the card to people', 'int', 1, 30),
    T('investigate.follow-up-close-hours', 'investigate_follow_up_close_hours', INV, 'Hours before an announced close',
      'After a check finds a waiting card fixed or over, {product} says so in its Slack thread and closes it this many hours later unless it fires again, a person acts on it or someone presses Keep open', 'int', 1, 48),
    T('investigate.settle-max-per-day', 'investigate_settle_max_per_day', INV, 'Follow-up checks per day',
      'Follow-up checks and settle reviews the investigator runs per day, all cards together; they also count against the daily limit; 0 turns them off', 'int', 0, 200),
    T('investigate.rooms', 'investigate_rooms', INV, 'Parallel rooms', 'How many asks, clicks and assessments run at once next to the card run', 'int', 1, 6),
    T('investigate.max-tool-calls', 'investigate_max_tool_calls', INV, 'Tool calls per card run', 'Tool call budget of a card run', 'int', 5, 60),
    T('investigate.assess-max-tool-calls', 'investigate_assess_max_tool_calls', INV, 'Tool calls per assessment', 'Tool call budget of a deploy-report assessment', 'int', 5, 40),
    T('investigate.slack-max-tool-calls', 'investigate_slack_max_tool_calls', INV, 'Tool calls per Slack ask', 'Tool call budget of a Slack question', 'int', 3, 30),
    T('investigate.strict-tools', 'investigate_strict_tools', INV, 'Strict tool inputs', 'Ask the model for schema-exact write tool inputs', 'bool'),
    T(OWN_SITES_KEY, 'own_sites', INV, 'Your websites',
      'The sites of your company the investigator may read and search, such as example.com. Host names are separated by commas, without https:// or a path, and a host covers its subdomains. '
      'Empty uses the sites of the Website integrations', 'list', items='own-site'),
    T('investigate.web-domains', 'investigate_web_domains', INV, 'Web sites',
      'Your sites are added to the built-in list; write -host to leave one out; any lifts the limit; off turns web search and reading off. Host names are separated by commas, and a host covers its subdomains', 'text', items='web-host'),
    T('investigate.web-searches', 'investigate_web_searches', INV, 'Web searches per run', 'Web searches one run may make, $0.01 each; 0 turns search off', 'int', 0, 10),
    T('investigate.web-fetches', 'investigate_web_fetches', INV, 'Web pages per run', 'Web pages one run may read; 0 turns page reads off', 'int', 0, 10),
    T('screenshot.max-per-day', 'screenshot_max_per_day', INV, 'Screenshots per day', 'Screenshots plug-in tools may store per day, all runs together; it resets at local midnight', 'int', 0, 500),
    T(TRACE_INSTANCE_KEY, 'langfuse_trace_instance', INV, 'Langfuse instance', 'The Langfuse instance (Connect \u203a Integrations) that {product} sends its own traces to. Pick one for a separate Langfuse project, for example one named skopax, '
      'not the project of an app {product} watches: the picked instance stops being watched. Its keys must be allowed to write traces; a read-only key cannot. '
      'None sends nothing. Turn tracing on in Admin \u203a Features', 'choice', choices=trace_instances, choice_labels={'': 'None'}),
    T('langfuse.trace-content', 'langfuse_trace_content', INV, 'Trace content', 'metadata sends names, models, tokens, cost, timing, status, card keys and run ids only. '
      'full also sends prompts, questions, answers and tool output, redacted and cut; Warning: they can hold customer data and pass to the Langfuse project and everyone who can read it',
      'choice', choices=TRACE_CONTENTS),
    T('langfuse.trace-sample', 'langfuse_trace_sample', INV, 'Traces sampled', 'The share of runs, answers and checks sent to Langfuse, decided once per trace; failed ones are always sent', 'int', 0, 100),
    T('investigate.mode', 'investigate_mode', INV, 'Mode', 'off, shadow (records what it would write) or live; a change applies from the next run, and a run already started keeps its mode. To stop it at once use the Stop investigator button, or a daily limit of 0', 'choice', choices=INVESTIGATE_MODES),
    T(SLACK_CHANNEL_KEY, 'slack_channel', SLACK, 'Channel', 'The channel new cards and deploy reports are posted to: its name, like #ops, or its id (C…, Slack › the channel › About shows it at the bottom). A name is looked up in Slack and saved as the channel id, so renaming the channel later does not break it. Slack is asked before saving whether the channel exists and the bot is in it. Reset goes back to the config value', 'text'),
    T('slack.ask-cap', 'slack_ask_cap', SLACK, 'Slack asks per hour', 'Questions to the bot per hour, everyone together', 'int', 0, 100),
    T('slack.user-ask-cap', 'slack_user_ask_cap', SLACK, 'Slack asks per person per hour', 'Questions one person can ask the bot per hour', 'int', 0, 50),
    T('slack.ask-channels', 'slack_ask_channels', SLACK, 'Channels that take questions', 'all, main (the channel cards are posted to), or channel ids separated by commas', 'text', items='channel'),
    T('slack.dm-chats', 'slack_dm_chats', SLACK, 'Direct message chats', 'Answers direct messages to the {product} bot like the Chat page: private, kept in the person\'s chats, within the chat messages per day', 'bool'),
    T('slack.thread-summary', 'slack_thread_summary', SLACK, 'Summarise Slack threads onto cards', 'Writes a short summary of a card\'s Slack thread onto the card: corrections, decisions, who does what and open questions. It is updated after {product} answers in the thread and when the thread has been quiet for 30 minutes, with at least 3 messages from people, at most once per card per half hour and 6 times per card per day. The investigator reads it as leads to check. It uses the gate model, counts toward the AI spend and pauses while the investigator is off, stopped or at its daily spend cap', 'bool'),
    T('slack.thread-hours', 'slack_thread_hours', SLACK, 'Thread window (h)', 'A new card of a service posted in the last N hours goes into that thread; 0 posts each card on its own', 'float', 0, 72),
    T('slack.great-emoji', 'slack_great_emoji', SLACK, 'Thank-you emoji', 'The reaction the bot puts on a thank-you', 'text'),
    T('slack.remind-hours', 'slack_remind_hours', SLACK, 'Reminder after (h)', 'Reminds a posted card nobody acted on after N hours; 0 turns reminders off', 'float', 0, 168, live=False),
    T('slack.clear-note-minutes', 'slack_clear_note_minutes', SLACK, 'Cleared note after (min)', 'Says in the thread that a posted card cleared after N minutes', 'float', 0, 240, live=False),
    T(WARN_DIGEST_KEY, 'warn_digest_at', SLACK, 'Warnings digest at', 'Time of day (HH:MM, install time zone) of the daily warnings digest, the Slack post and the mail to responsibles that list the open Warning cards nobody is assigned to or working on; '
      'both run Monday to Friday only, never on Saturday or Sunday, and send nothing on a day with no such card. The latest time is 23:55; empty turns it off', 'text'),
    T('routes.wait-minutes', 'routes_wait_minutes', SLACK, 'Routes wait for the investigation (min)', 'A route (Teams, Email) holds a card below Post immediately from until the investigator has finished it, '
      'then sends it with the finding; with no finished investigation after N minutes it sends the plain card. Business routes keep waiting for their business sentence', 'int', 5, 240),
    T('routes.slack-echo', 'routes_slack_echo', SLACK, 'Echo route posts to Slack', 'Also posts a card a route (such as a Teams channel) takes to the Slack channel when Slack has not posted it yet, and says in its thread which route got it; a route can override this with its own Also post to Slack field', 'bool'),
    T(POST_SEV_KEY, 'slack_post_sev', SLACK, 'Post immediately from', 'Posts a card of this severity or higher to the main channel with its numbers when it is pushed (new, escalated or reopened; information is never posted); the investigator\'s findings follow as a reply in its thread. '
      'Many cards opening together are not posted at once, and the investigator, when it runs, groups them. Off leaves every card to the investigator\'s posts and to people', 'choice', choices=SLACK_POST_SEVS, choice_labels=POST_SEV_LABELS),
    T(CRIT_NOW_KEY, 'slack_crit_now', SLACK, 'Critical cards at once', 'Deprecated: use Post immediately from. false turns posting at once off while Post immediately from is not set', 'bool', live=False, hidden=True),
    T('slack.fallback-minutes', 'slack_fallback_minutes', SLACK, 'Numbers fallback after (min)', 'Posts the numbers of a critical card when no investigation finished in N minutes; 0 turns it off', 'int', 0, 240, live=False),
    T('slack.state-notes', 'slack_state_notes', SLACK, 'Status notes in threads', 'Posts a short line in the thread when a posted card is taken, resolved or muted', 'bool', live=False),
    T('slack.listen', 'slack_listen', SLACK, 'Listening', 'off, shadow (reads only) or live (answers)', 'choice', choices=SLACK_LISTEN_MODES, live=False),
    T('slack.push-user', 'slack_push_user', SLACK, 'Slack DM people', 'Slack user ids (U...) that get new critical cards and task notices as a Slack direct message, separated by commas; Admin \u203a Features \u203a Slack DMs for critical cards turns it on or off', 'text', live=False, items='slack-user-id'),
    T('slack.operator-cache-seconds', 'slack_operator_cache_s', SLACK, 'Operator check cache (s)', 'How long the answer to whether a Slack user may act as an operator is kept before Slack and the roles are asked again', 'int', 0, 86400, live=False),
    T('slack.attach-screenshots', 'slack_attach_screenshots', SLACK, 'Screenshots in posts', 'Puts the screenshots a post carries into its thread as images; the post links them either way', 'bool', live=False),
    T('self-review.model', 'selfreview_model', REVIEW, 'Model', 'Model of the daily self-review', 'choice', choices=MODELS),
    T('self-review.dm', 'selfreview_dm', REVIEW, 'Send to', 'Emails that get the self-review as a Slack DM, separated by commas; empty uses the push users', 'text', items='email'),
    T('self-review.hour', 'selfreview_hour', REVIEW, 'Hour (local time)', 'Hour of the daily self-review; -1 turns it off', 'int', -1, 23, live=False),
    T('cards.archive-hours', 'archive_hours', CARDS, 'Archive after (hours)', 'A card that is not firing (or is muted) and had no change for N hours moves to Archived: hidden on the board, the lists and the counts, still under the Archived list. Waiting cards nobody acted on stay', 'int', 1, 720),
    T('core.auto-resolve-hours', 'auto_resolve_hours', CARDS, 'Auto-resolve cleared cards after (hours)', 'Deprecated: use the critical, high and warning settings. Seeds the severities that have no setting of their own', 'int', 0, 720, hidden=True),
    T('core.auto-resolve-hours-crit', 'auto_resolve_hours_crit', CARDS, 'Auto-resolve cleared critical cards after (hours)', 'Closes a critical card that stopped firing and that nobody acted on after N hours; 0 turns it off for critical cards. With Business days only on, only Monday to Friday counts', 'int', 0, 720),
    T(ALERTS_WARN_KEY, 'alerts_warn', CARDS, 'Alerts kept at Warning', 'Alert names whose warning label stays Warning instead of High, such as noisy forecast alerts, separated by commas; '
      'a name may use * and ?, such as *Forecast* or CephPoolGrowthForecast, matched with case. A critical alert is never lowered', 'list', items='alert-name'),
    T('core.auto-resolve-hours-high', 'auto_resolve_hours_high', CARDS, 'Auto-resolve cleared high cards after (hours)', 'Closes a high card that stopped firing and that nobody acted on after N hours; 0 turns it off for high cards. '
      'Without a value of its own it follows the warning setting. With Business days only on, only Monday to Friday counts', 'int', 0, 720),
    T('core.auto-resolve-hours-warn', 'auto_resolve_hours_warn', CARDS, 'Auto-resolve cleared warning cards after (hours)', 'Closes a warning card that stopped firing and that nobody acted on after N hours; 0 turns it off for warning cards. With Business days only on, only Monday to Friday counts', 'int', 0, 720),
    T('core.info-auto-close-days', 'info_auto_close_days', CARDS, 'Auto-resolve info cards still firing after (days)', 'Deprecated and ignored: information items are never closed, they end when their signal stops', 'int', 0, 90, hidden=True),
    T('core.business-days', 'business_days', CARDS, 'Business days only', 'Count only Monday to Friday (install time zone) for the auto-resolve timers', 'bool'),
    T('core.auto-resolve-days', 'auto_resolve_days', CARDS, 'Auto-resolve after (days)', 'Deprecated: use the hours setting. Closes waiting cards nobody acted on after N days; 0 turns it off', 'int', 0, 30, hidden=True),
    T('recurring.count', 'recurring_count', CARDS, 'Recurring after (times)', 'A card that fired N times in the window with no ticket is marked recurring', 'int', 2, 20),
    T('recurring.days', 'recurring_days', CARDS, 'Recurring window (days)', 'Window of the recurring count', 'int', 1, 60),
    T('jira.projects', 'jira_projects', JIRA, 'Projects', 'Projects the Create task dialog offers, separated by commas; the Infra project is added to the list by itself', 'list', items='project-key'),
    T('jira.project', 'jira_project', JIRA, 'Default project', 'Project of a task created from the Slack button, where nobody picks one', 'text'),
    T('jira.parent', 'jira_parent', JIRA, 'Default epic', 'Epic of a new task in the default project when none is picked; empty means no epic', 'text'),
    T('jira.points', 'jira_points', JIRA, 'Story points', 'Story points of new tasks', 'float', 0, 21),
    T('jira.dev-scope', 'jira_dev_scope', JIRA, 'Scope value', 'Value set in the scope field (jira.scope-field) of new tasks, and the one the Create task dialog picks first; empty sends no scope', 'text'),
    T('jira.sprint-default', 'jira_sprint_default', JIRA, 'Add to the current sprint', 'Whether a new task goes into the active sprint of its project: the Create task dialog starts with the box ticked or not, '
      'and tasks from Slack and MCP follow it. It needs the sprint field', 'bool'),
    T('jira.scope-field', 'jira_scope_field', JIRA, 'Scope field', 'Id of the Jira option field that holds the scope of a task, such as customfield_10100; the Create task dialog offers its values. Empty sends no scope', 'text'),
    T('jira.points-field', 'jira_points_field', JIRA, 'Story points field', 'Id of the Jira number field for story points, such as customfield_10200; empty sends no points', 'text'),
    T('jira.developer-field', 'jira_developer_field', JIRA, 'Developer field', 'Id of a Jira user field set to whoever creates the task, next to the assignee; empty leaves it out', 'text'),
    T('jira.sprint-field', 'jira_sprint_field', JIRA, 'Sprint field', 'Id of the Jira sprint field (Jira Software), such as customfield_10020; empty puts no task in a sprint', 'text'),
    T('jira.tech-project', 'jira_tech_project', JIRA, 'Infra project', 'Project for infrastructure tasks', 'text'),
    T('jira.tech-assignee', 'jira_tech_assignee', JIRA, 'Infra assignee', 'Jira account id that gets infrastructure tasks', 'text'),
    T('jira.backlink', 'jira_backlink', JIRA, 'Back-link on the issue', 'When a card gets a Jira ticket, {product} adds the card link to that issue: link adds a web link (Link issues permission), '
      'comment posts one short comment per card and issue (Add comments permission), off adds nothing', 'choice', choices=JIRA_BACKLINK_MODES),
    T(RELEASE_NAME_KEY, 'jira_release_name', JIRA, 'Release name', 'Name of the Jira release of a deploy day, from {project}, {yyyy}, {mm} and {dd} in the local time zone, such as '
      '{project} {yyyy}-{mm}-{dd}. It needs {project}, {mm} and {dd} once each; deploy reports look up the issues of these releases', 'text'),
    T('core.release-settle', 'release_settle', FEATURES, 'Release settle', 'Settles releases from deploy events', 'bool', live=False),
    T('core.assign-dm', 'assign_dm', FEATURES, 'Assignee message', 'Sends the person a card is assigned to one Slack direct message', 'bool'),
    T('core.jira-links', 'jira_links', FEATURES, 'Jira links', 'Moves cards to In progress when a Jira ticket names them', 'bool', live=False),
    T('core.postdeploy', 'postdeploy', FEATURES, 'Post-deploy checks', 'Checks every prod deploy and writes a deploy report', 'bool'),
    T('self-review.enabled', 'self_review_enabled', FEATURES, 'Self-review', 'Writes the daily self-review of {product} itself and sends it as a Slack direct message; it also needs the key of the AI provider in use, the self project and its log index. Switched on after the review hour, the first scheduled review comes the next day; Run now works at once', 'bool'),
    T('core.probe', 'probe', FEATURES, 'Reachability probe', 'Checks from this server that every probe target of a serving integration answers, every 15 minutes and at start; '
      'its report gives tool status and certificate expiry cards', 'bool'),
    feature_tunable('core.investigator', 'investigator_on'),
    feature_tunable('core.chat', 'chat_on'),
    feature_tunable('core.slack-autosend', 'slack_autosend'),
    feature_tunable('core.slack-listener', 'slack_listener_on'),
    feature_tunable('core.mcp', 'mcp_on'),
    feature_tunable('core.push', 'push_on'),
    feature_tunable('core.update-check', 'update_check'),
    feature_tunable('core.tag-report', 'tag_report_on'),
    feature_tunable('core.warn-digest', 'warn_digest_on'),
    feature_tunable('core.settle', 'settle_on'),
    feature_tunable('core.langfuse-tracing', 'langfuse_tracing_on'),
    feature_tunable('core.hide-attribution', 'hide_attribution'),
) + tuple(T(key, field, RULES, label, 'Thresholds of the ' + label + ' cards; an empty field uses the default', 'rules', defaults=dict(defaults)) for key, field, label, defaults in RULE_SETS) + (
    T(CERT_SKIP_KEY, 'cert_skip', RULES, 'Certificates not watched', 'Hosts whose certificate expiry raises no card, separated by commas: shop.example.com also covers its subdomains, '
      '*.example.com covers only the subdomains', 'list', items='host'),
) + (
    T('core.probe-extra', 'probe_extra', RULES, 'Extra probe targets', 'More addresses the reachability probe calls, one per line as name=url, such as status=https://status.example.com; '
      f'at most {EXTRA_MAX}. A name is 1 to {EXTRA_NAME_MAX} letters, digits, spaces, dots, dashes or underscores, and an address is http:// or https:// without a user name or password. '
      'Only reachability is checked, and no credentials are sent', 'text'),
) + (
    T(TAG_REPORT_KEY, 'tag_report_at', TAGS, 'Daily report at', 'Time of day (HH:MM, install time zone) of the daily tag report, the Slack post and the mail to responsibles: the post names the responsibles of each tag with open cards, the mail gives each of them their part; '
      'both run Monday to Friday only, never on Saturday or Sunday. The latest time is 23:55; empty turns it off', 'text'),
    T('tags.mail', 'tags_mail', TAGS, 'Mail responsibles their part', 'Mails each responsible with an email address their part, apart from Slack: the daily tag report with the open cards of their tags, '
      'the daily warnings digest with the waiting Warning cards of their tags, and the plug-in digests that name one of their tags. It needs a Mail server that is on, and goes out even while Slack is not set up '
      'or Slack auto-send is off; a responsible with nothing in their tags gets no mail', 'bool'),
) + tuple(
    tag_tunable(tag, label) for tag, label in tags.TAGS.items()) + SIGN_IN_ROLES + MCP_ACCESS

CONFIG_KEY = {'general.timezone': 'core.timezone', 'access.public-read': 'core.public-read'}


@functools.cache
def fallback() -> Settings:
    return load_settings(env={})


@functools.cache
def defaults() -> dict:
    base = fallback()
    return {f.name: getattr(base, f.name) for f in dataclasses.fields(Settings)}


def plugin_tunables(plugin: Plugin) -> tuple[Tunable, ...]:
    own = tuple(Tunable(f'{plugin.id}.{s.key}', '', plugin.name, s.label, s.help, s.kind, s.low, s.high, tuple(s.choices), s.live, None, None, plugin.id, s.default) for s in plugin.settings)
    sets = tuple(Tunable(f'{plugin.id}.{r.id}', '', plugin.name, r.label, 'Thresholds of ' + r.label + '; an empty field uses the default', 'rules', defaults=dict(r.defaults), plugin=plugin.id, default='', rule_help=dict(r.help))
                 for r in plugin.rules)
    return own + sets


def registry(extra: tuple[Tunable, ...] = ()) -> tuple[Tunable, ...]:
    return STATIC + tuple(tag_tunable(tag, label) for tag, label in tags.plugin_tags().items()) + tuple(extra)


def number(value) -> str:
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def text_limit(t: Tunable) -> int:
    high = t.high
    if t.plugin is not None and t.kind == 'text' and isinstance(high, (int, float)) and not isinstance(high, bool) and math.isfinite(high) and high >= 1:
        return min(int(high), SETTING_TEXT_MAX)
    return TEXT_LIMITS.get(t.key, TEXT_LIMIT)


def text_of(t: Tunable, settings: Settings) -> str:
    if t.plugin is not None:
        return t.default or ''
    if t.kind == 'tagrules':
        return ', '.join(dict(settings.tag_rules or tags.RULES).get(t.part, ()))
    value = getattr(settings, t.field)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, tuple):
        return ', '.join(value)
    return number(value) if isinstance(value, (int, float)) else str(value or '')


def default_of(t: Tunable) -> str | None:
    if t.plugin is not None:
        return t.default or ''
    if t.kind == 'tagrules':
        return ', '.join(dict(tags.RULES).get(t.part, ()))
    if t.kind == 'rules':
        return ''
    if t.field not in defaults():
        return None
    value = defaults()[t.field]
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, tuple):
        return ', '.join(value)
    return number(value) if isinstance(value, (int, float)) else str(value)


def effective_rules(t: Tunable, raw: str) -> dict:
    out = dict(t.defaults or {})
    try:
        given = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return out
    if isinstance(given, dict):
        out.update({k: v for k, v in given.items() if k in out and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0})
    return out


def parse(t: Tunable, raw, base: Settings, loading: bool = False):
    text = str(raw if raw is not None else '').strip()
    if t.kind in ('int', 'float'):
        if t.key == CHAT_CALLS_KEY and text in ('', '0'):
            return 0
        try:
            value = float(text) if t.kind == 'float' else int(text)
        except ValueError:
            raise Invalid(f'{t.label} must be a {"number" if t.kind == "float" else "whole number"}') from None
        if not math.isfinite(value):
            raise Invalid(f'{t.label} must be a number')
        if t.key == CHAT_CALLS_KEY and value < 3:
            raise Invalid(f'{t.label} must be 0 or between 3 and 30')
        if (t.low is not None and value < t.low) or (t.high is not None and value > t.high):
            raise Invalid(f'{t.label} must be between {number(t.low)} and {number(t.high)}')
        return value
    if t.kind == 'bool':
        if text not in ('true', 'false'):
            raise Invalid(f'{t.label} must be true or false')
        return text == 'true'
    if t.kind == 'choice':
        text = retired_info(t.key, offered_tag(t, text)) if (loading and t.key == POST_SEV_KEY) or (t.plugin is not None and t.key.rpartition('.')[2] == ROUTE_SEV) else offered_tag(t, text)
        allowed = choices_of(t) + extra_models_of(t) + ((text_of(t, base),) if text_of(t, base) else ())
        if text not in allowed and not (loading and (callable(t.choices) or kept_ref(t, text))):
            raise Invalid(f'{t.label} must be one of ' + ', '.join(dict.fromkeys(allowed)))
        return text
    if t.kind == 'text':
        limit = text_limit(t)
        if len(text) > limit:
            raise Invalid(f'{t.label} is longer than {limit} characters')
        if t.key in HTTPS_KEYS and text and (not text.startswith('https://') or any(c.isspace() for c in text)):
            raise Invalid(f'{t.label} is one https:// address')
        if t.key == TIMEZONE_KEY:
            try:
                zone_clock.check(text)
            except ValueError:
                raise Invalid(f'{t.label} must be an IANA timezone name, such as UTC or Europe/Berlin') from None
        if t.key in DAILY_AT_KEYS and text and not tags.report_at_ok(text):
            raise Invalid(f'{t.label} must be a time as HH:MM from 00:00 to {tags.REPORT_LAST}, such as 10:00, or empty for off')
        if t.key == LINKS_KEY:
            instance_links(text)
        if t.key == EXTRA_KEY and (refused := read_extra(text)[1]):
            raise Invalid(msg('probe.extra.invalid', entry=refused[0], rule=refused[1]))
        if t.items and not loading:
            check_items(t, item_entries(t, text))
        if t.key in JIRA_FIELD_KEYS and not jirafields.field_id_ok(text):
            raise Invalid(f'{t.label} is a Jira field id, such as customfield_10200')
        if t.key == RELEASE_NAME_KEY and (problem := release_problem(text)):
            raise Invalid(problem)
        if t.key == TOKEN_HEADERS_KEY and (reason := read_token_headers(text)[1]):
            said = msg('sso.token_headers_invalid', reason=token_headers_reason(reason))
            if loading:
                log.warning('admin setting %s dropped: %s', t.key, said)
            raise Invalid(said)
        if t.key in SSO_URL_KEYS + MCP_URL_KEYS and text and (problem := url_problem(text)):
            raise Invalid(msg(problem, field=t.label))
        if t.key == MCP_RESOURCE_KEY and not loading and urlsplit(text).path.rstrip('/').lower().endswith('/mcp'):
            raise Invalid(msg('mcp.resource_url_mcp', field=t.label))
        return text.rstrip('/') if t.key in HTTPS_KEYS else text
    if t.kind == 'rules':
        try:
            given = json.loads(text) if text else {}
        except ValueError:
            raise Invalid(f'{t.label} rules must be a JSON object of numbers') from None
        if not isinstance(given, dict):
            raise Invalid(f'{t.label} rules must be a JSON object of numbers')
        unknown = [k for k in given if k not in (t.defaults or {})]
        if unknown:
            raise Invalid(f'{t.label} has no rule named ' + ', '.join(unknown))
        bad = [k for k, v in given.items() if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0]
        if bad:
            raise Invalid(f'{t.label}: ' + ', '.join(bad) + ' must be a number of 0 or more')
        kept = {k: v for k, v in sorted(given.items()) if v != t.defaults[k]}
        return json.dumps(kept, separators=(',', ':')) if kept else ''
    if t.kind == 'list':
        entries = tuple(item_entries(t, text)) if t.items in ITEM_LOOSE else tuple(p.strip() for p in text.split(',') if p.strip())
        bad = [p for p in entries if t.key == CERT_SKIP_KEY and not skip_entry_ok(p)]
        if bad:
            raise Invalid(msg('certs.skip_invalid', entry=bad[0][:SKIP_SHOWN]))
        if t.key == 'sso.allowed-domains':
            from skopax.config import bad_domains

            if wrong := bad_domains(entries):
                raise Invalid(msg('sso.domain_invalid', entry=wrong[0][:SKIP_SHOWN]))
        if t.items and not loading:
            check_items(t, entries)
        if t.key == ALERTS_WARN_KEY:
            if len(entries) > ALERT_GLOBS_MAX and not loading:
                raise Invalid(msg('alerts.warn_too_many', limit=ALERT_GLOBS_MAX))
            entries = entries[:ALERT_GLOBS_MAX]
        return entries
    if t.kind == 'rolemap':
        from skopax.roles import map_text, parse_role_map

        return map_text(parse_role_map(text, limit=not loading)) if text else ''
    if t.kind in ('tagrules', 'match'):
        terms = tuple(p.strip() for p in text.split(',') if p.strip())
        if any(len(p) > TERM_LIMIT for p in terms):
            raise Invalid(f'{t.label}: one pattern is longer than {TERM_LIMIT} characters')
        if t.kind == 'match' and not terms:
            raise Invalid(f'{t.label} needs at least one pattern')
        return terms
    raise Invalid(f'{t.key} cannot be set here')


def item_view(t: Tunable) -> dict:
    pattern, flags, strip = ITEM_RULES[t.items]
    out = {'kind': t.items, 'separator': ITEM_SEPARATORS.get(t.items, ','), 'pattern': pattern, 'flags': flags, 'strip': strip, 'why': msg(f'items.why.{t.items}')}
    if t.items in ITEM_MINUS:
        out['minus'] = True
    if t.items in ITEM_LOOSE:
        out['loose'] = True
    if t.items in ITEM_KEYWORDS:
        out['keywords'] = list(ITEM_KEYWORDS[t.items])
    return out


def item_entries(t: Tunable, text: str) -> list[str]:
    sep = ITEM_SEPARATORS.get(t.items, ',')
    if t.items in ITEM_MINUS + ITEM_LOOSE:
        return [p for p in re.split(r'[\s,]+', text) if p]
    return [p.strip() for p in (text.split() if sep == ' ' else text.split(',')) if p.strip()]


def check_items(t: Tunable, entries: list[str] | tuple[str, ...]) -> None:
    if t.items not in ITEM_RULES_CHECKED:
        return
    pattern, flags, strip = ITEM_RULES[t.items]
    rule = re.compile(pattern, re.IGNORECASE if 'i' in flags else 0)
    keywords = ITEM_KEYWORDS.get(t.items, ())
    word = (lambda e: e.lower()) if t.items in ITEM_MINUS else (lambda e: e)
    if keywords and any(word(e) in keywords for e in entries) and len(entries) > 1:
        raise Invalid(msg('items.exclusive' if t.items == 'channel' else f'items.exclusive.{t.items}', field=t.label))
    for n, entry in enumerate(entries, 1):
        if word(entry) in keywords:
            continue
        bare = entry[1:] if t.items in ITEM_MINUS and entry.startswith('-') else entry
        if not rule.fullmatch(bare.rstrip(strip) if strip else bare):
            raise Invalid(msg('items.invalid', field=t.label, entry=n, reason=msg(f'items.why.{t.items}')))


def link_problem(url: str) -> str | None:
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return 'url'
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port
    except ValueError:
        return 'url'
    if parts.scheme.lower() not in ('http', 'https') or not host:
        return 'url'
    if '@' in parts.netloc:
        return 'userinfo'
    return None


def read_links(raw) -> tuple[list[dict], tuple[int, str] | None]:
    out, seen = [], set()
    for entry, text in enumerate((p.strip() for p in str(raw or '').split(',') if p.strip()), 1):
        name, sep, url = (part.strip() for part in text.partition('='))
        if entry > LINKS_MAX:
            problem = msg('links.rule.count', max=LINKS_MAX)
        elif not sep or not url:
            problem = msg('links.rule.pair')
        elif not LINK_NAME.fullmatch(name):
            problem = msg('links.rule.name', max=LINK_NAME_MAX)
        elif rule := link_problem(url):
            problem = msg(f'links.rule.{rule}')
        elif name.casefold() in seen:
            problem = msg('links.rule.repeat')
        else:
            seen.add(name.casefold())
            out.append({'name': name, 'url': url})
            continue
        return out, (entry, problem)
    return out, None


def link_refusal(raw) -> tuple[int, str] | None:
    return read_links(raw)[1]


def instance_links(raw: str) -> list[dict]:
    out, refused = read_links(raw)
    if refused:
        raise Invalid(msg('links.invalid', entry=refused[0], rule=refused[1]))
    return out


def refused_note(source: str, refused: tuple[int, str]) -> str:
    return msg('links.ignored', source=source, entry=refused[0], rule=refused[1])


def hide_refused(key: str, text):
    return msg('links.hidden') if key == LINKS_KEY and link_refusal(text) else text


def link_colour(name: str) -> str:
    return LINK_COLOURS[hashlib.sha256(name.encode()).digest()[0] % len(LINK_COLOURS)]


def current_instance(settings: Settings) -> str:
    label = (settings.workspace_label or '').strip()
    if label:
        return label
    try:
        host = urlsplit(settings.base_url).hostname if settings.base_url else None
    except ValueError:
        host = None
    return host or 'local'


def instance_menu(settings: Settings, links: list[dict], icon: str | None = None) -> dict:
    name = current_instance(settings)
    return {'current': {'name': name, 'letter': name[:1].upper()} | ({'icon': icon} if icon else {}),
            'links': [{'name': link['name'], 'url': link['url'], 'letter': link['name'][:1].upper(), 'colour': link_colour(link['name'])} for link in links]}


def stored(value) -> str:
    if isinstance(value, tuple):
        return ', '.join(value)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return number(value) if isinstance(value, (int, float)) else str(value)


def apply(base: Settings, overrides: dict[str, str], extra: tuple[Tunable, ...] = ()) -> Settings:
    known = {t.key: t for t in registry(extra)}
    changes, tag_parts = {}, {}
    for key, raw in overrides.items():
        t = known.get(key)
        if t is None and (tag := tags.RULES_KEY.fullmatch(key)):
            t = tag_tunable(tag[1], tag[1])
        if t is None or t.plugin is not None:
            continue
        try:
            value = parse(t, raw, base, loading=True)
        except Invalid as e:
            log.warning('admin setting %s skipped: %s', key, error_kind(e))
            continue
        if t.kind == 'tagrules':
            tag_parts[t.part] = value
        else:
            changes[t.field] = value
    if 'auto_resolve_days' in changes and 'auto_resolve_hours' not in changes and not base.auto_resolve_hours_set:
        changes['auto_resolve_hours'] = changes['auto_resolve_days'] * 24
    if 'auto_resolve_hours' in changes:
        for sev in ('crit', 'warn'):
            if f'auto_resolve_hours_{sev}' not in changes and sev not in base.auto_resolve_sev_set:
                changes[f'auto_resolve_hours_{sev}'] = changes['auto_resolve_hours']
        if 'info_auto_close_days' not in changes and not base.info_auto_close_days_set:
            changes['info_auto_close_days'] = 0 if changes['auto_resolve_hours'] == 0 else defaults()['info_auto_close_days']
    if 'auto_resolve_hours_warn' in changes and 'auto_resolve_hours_high' not in changes and 'high' not in base.auto_resolve_sev_set:
        changes['auto_resolve_hours_high'] = changes['auto_resolve_hours_warn']
    if 'slack_crit_now' in changes and 'slack_post_sev' not in changes and not base.slack_post_sev_set:
        changes['slack_post_sev'] = SLACK_POST_SEV if changes['slack_crit_now'] else 'off'
    given = tuple(t.field for t in known.values() if t.key in overrides and t.field in MODEL_FIELDS)
    if given:
        changes['ai_models_set'] = tuple(dict.fromkeys(base.ai_models_set + given))
    if tag_parts:
        held = dict(base.tag_rules or tags.RULES)
        held.update(tag_parts)
        changes['tag_rules'] = tuple(held.items())
    return dataclasses.replace(base, **changes) if changes else base


class TunableStore:
    def __init__(self, db):
        self.values = db['tunables']
        self.changes = db['tunable_changes']
        self.meta = db['tunables_rev']

    def ensure_indexes(self) -> None:
        self.changes.create_index([('seq', DESCENDING)])

    def rev(self) -> int:
        doc = self.meta.find_one({'_id': REV_ID})
        return int(doc['n']) if doc else 0

    def all(self) -> dict[str, dict]:
        return {d['_id']: {'value': d.get('value'), 'by': d.get('by'), 'at': d.get('at')} for d in self.values.find({})}

    def write(self, key: str, value: str | None, by: str, old: str | None, record: bool = True, **extra) -> dict:
        if value is None:
            self.values.delete_one({'_id': key})
        else:
            self.values.replace_one({'_id': key}, {'_id': key, 'value': value, 'by': by, 'at': iso(utc_now())}, upsert=True)
        if not record:
            self.meta.find_one_and_update({'_id': REV_ID}, {'$inc': {'n': 1}}, upsert=True)
            return {'key': key, 'old': old, 'new': value, 'by': by, 'at': iso(utc_now()), **extra}
        return self.write_change(key, old, value, by, **extra)

    def migrate_renamed(self) -> bool:
        old = self.values.find_one({'_id': OLD_LINKS_KEY})
        if old is None:
            return False
        wrote = False
        if old.get('value') is not None:
            at = iso(utc_now())
            done = self.values.update_one({'_id': LINKS_KEY}, {'$setOnInsert': {'value': old['value'], 'by': 'system', 'at': at}}, upsert=True)
            if done.upserted_id is not None or self._unrecorded_copy(old['value']):
                wrote = self.meta.update_one({'_id': RENAME_MARK}, {'$setOnInsert': {'at': at}}, upsert=True).upserted_id is not None
                if wrote:
                    self.write_change(LINKS_KEY, None, old['value'], 'system', renamed=OLD_LINKS_KEY)
        self.values.delete_one({'_id': OLD_LINKS_KEY})
        if not wrote:
            self.meta.find_one_and_update({'_id': REV_ID}, {'$inc': {'n': 1}}, upsert=True)
        return wrote

    def _unrecorded_copy(self, value: str) -> bool:
        held = self.values.find_one({'_id': LINKS_KEY})
        if held is None or held.get('by') != 'system' or held.get('value') != value:
            return False
        return self.changes.find_one({'key': LINKS_KEY, 'renamed': OLD_LINKS_KEY}) is None

    def advance(self, key: str, value: str) -> None:
        found = self.values.find_one({'_id': key})
        if found is not None and zone_clock.parse_stamp(found.get('value')) is None:
            self.values.update_one({'_id': key, 'value': {'$eq': found.get('value')}}, {'$set': {'value': value}})
        self.values.update_one({'_id': key}, {'$max': {'value': value}}, upsert=True)

    def mark(self, key: str, value: str) -> None:
        self.advance(key, value)
        self.meta.find_one_and_update({'_id': REV_ID}, {'$inc': {'n': 1}}, upsert=True)

    def write_change(self, key: str, old, new, by: str, **extra) -> dict:
        seq = self.meta.find_one_and_update({'_id': REV_ID}, {'$inc': {'n': 1}}, upsert=True, return_document=ReturnDocument.AFTER)['n']
        change = {'_id': uuid.uuid4().hex, 'seq': seq, 'key': key, 'old': old, 'new': new, 'by': by, 'at': iso(utc_now()), **extra}
        self.changes.insert_one(change)
        return {k: v for k, v in change.items() if k != '_id'} | {'id': change['_id']}

    def history(self, limit: int = 50) -> list[dict]:
        rows = [{k: v for k, v in d.items() if k != '_id'} | {'id': d['_id']} for d in self.changes.find({}).sort('seq', DESCENDING).limit(min(limit, HISTORY_LIMIT))]
        return [r | {f: hide_refused(r.get('key'), r.get(f)) for f in ('old', 'new') if f in r} | ({'current': KEY_ALIASES[r['key']]} if r.get('key') in KEY_ALIASES else {}) for r in rows]


class Live:
    def __init__(self, base: Settings, tunables: TunableStore, every: float = REFRESH_S, clock=time.monotonic, extra: Callable[[], tuple[Tunable, ...]] = tuple,
                 values: Mapping[str, str] = {}, labels: Mapping[str, str] = {}):
        self.base = base
        self.tunables = tunables
        self.every = every
        self.clock = clock
        self.extra = extra
        self.values: dict[str, str] = dict(values)
        self.labels: dict[str, str] = dict(labels)
        self.overrides: dict[str, dict] = {}
        self._warned: dict[tuple[str, int], str] = {}
        self._told: set[str] = set()
        self._current = base
        self.own_sites: Callable[[], tuple[tuple[str, ...], str]] = lambda: web_sites.own_sites_of(self.current().own_sites)
        self._rev: int | None = None
        self._checked: float | None = None
        self._lock = threading.Lock()
        self.watch: Callable[[Settings], None] | None = None
        self.keyed: Callable[[str], bool] = lambda provider: True

    def current(self) -> Settings:
        if self._checked is None or self.clock() - self._checked >= self.every:
            self.refresh()
        return self._current

    def refresh(self, force: bool = False) -> None:
        if not self._lock.acquire(blocking=self._checked is None or force):
            return
        try:
            self._checked = self.clock()
            rev = self.tunables.rev()
            if rev != self._rev or force:
                overrides = self.tunables.all()
                self._tell_renamed(overrides)
                self._current = apply(self.base, {k: v['value'] for k, v in overrides.items()}, self.extra())
                zone_clock.set_zone(self._current.timezone)
                logs.use(self._current.log_format, self._current.log_color, self._current.log_level)
                self.overrides = overrides
                self._rev = rev
            held = self._held_fields()
            if held != self._current.ai_models_held:
                self._current = dataclasses.replace(self._current, ai_models_held=held)
            self._seen()
        except Exception as e:
            log.warning('admin settings not read, keeping the last ones: %s', error_kind(e))
        finally:
            self._lock.release()

    def _held_fields(self) -> tuple[str, ...]:
        found = self.tunables.meta.find_one({'_id': UPGRADE_PENDING}, {'keys': 1})
        if not found:
            return ()
        known = self.known()
        return tuple(sorted({known[k].field for k in found.get('keys') or () if isinstance(k, str) and k in known}))

    def _tell_renamed(self, overrides: Mapping[str, dict]) -> None:
        for old, new in RENAMED.items():
            if old in overrides and old not in self._told:
                self._told.add(old)
                log.warning('admin setting %s is replaced by %s; it still counts while %s is not set', old, new, new)

    def _seen(self) -> None:
        if self.watch is None:
            return
        try:
            self.watch(self._current)
        except Exception as e:
            log.warning('a settings listener failed after the refresh: %s', error_kind(e))

    def rebase(self, base: Settings, values: Mapping[str, str], labels: Mapping[str, str]) -> None:
        self.base = base
        self.values = dict(values)
        self.labels = dict(labels)
        self._rev = None
        try:
            self._current = apply(base, {k: v['value'] for k, v in self.overrides.items()}, self.extra())
        except Exception as e:
            log.warning('settings not recomputed, keeping the last ones: %s', error_kind(e))
        self.refresh(force=True)

    def known(self) -> dict[str, Tunable]:
        return {t.key: t for t in registry(self.extra())}

    def settable(self, key: str) -> Tunable | None:
        t = self.known().get(key)
        return None if t is None or t.hidden else t

    def base_text(self, t: Tunable) -> str:
        if t.plugin is not None:
            return self.values.get(t.key, t.default or '')
        return text_of(t, self.base)

    def now_text(self, t: Tunable) -> str:
        if t.plugin is not None:
            given = self.overrides.get(t.key)
            return given['value'] if given else self.base_text(t)
        return text_of(t, self.current())

    def plugin_value_given(self, key: str) -> bool:
        self.current()
        return (self.overrides.get(key) or {}).get('value') is not None or self.values.get(key) is not None

    def plugin_value(self, key: str) -> object:
        self.current()
        t = self.known().get(key)
        if t is None or t.plugin is None:
            raise KeyError(key)
        if t.kind == 'rules':
            return effective_rules(t, self.now_text(t))
        given = (self.overrides.get(key) or {}).get('value')
        for where, raw in enumerate((given, self.values.get(key))):
            if raw is None:
                continue
            try:
                return parse(t, raw, self.base)
            except Invalid as e:
                mark = hashlib.sha256(str(raw).encode()).hexdigest()
                if self._warned.get((key, where)) != mark:
                    self._warned[(key, where)] = mark
                    log.warning('setting %s ignored, using the next value: %s', key, error_kind(e))
        return parse(t, t.default or '', self.base)

    def instance_links(self) -> list[dict]:
        raw = self.current().workspace_links
        try:
            return instance_links(raw)
        except Invalid as e:
            mark = hashlib.sha256(str(raw).encode()).hexdigest()
            if self._warned.get((LINKS_KEY, 0)) != mark:
                self._warned[(LINKS_KEY, 0)] = mark
                log.warning('%s ignored, the workspace switcher lists no other workspaces: %s', LINKS_KEY, error_kind(e))
            return []

    def rules_of(self, key: str) -> dict:
        value = self.plugin_value(key)
        if not isinstance(value, dict):
            raise KeyError(key)
        return value

    def entry(self, t: Tunable) -> dict:
        self.current()
        given = self.overrides.get(t.key)
        base_text, default = self.base_text(t), default_of(t)
        configured = t.key in self.values if t.plugin is not None else default is None or base_text != default
        out = {'key': t.key, 'group': t.group, 'label': t.label, 'help': brand.fill(t.help), 'kind': 'choice' if t.kind == 'choice' else t.kind, 'live': t.live,
               'value': self.now_text(t), 'base': base_text, 'default': default,
               'source': 'admin' if given else 'config' if configured else 'default',
               'by': (given or {}).get('by'), 'at': (given or {}).get('at')}
        out.update(settingcards.placed(t))
        if out['source'] == 'config':
            out['from'] = self.label_of(t)
        if callable(t.choices):
            out.update(value=offered_tag(t, out['value']), base=offered_tag(t, base_text))
        if t.choices:
            offered = choices_of(t)
            out['choices'] = list(dict.fromkeys(offered + ((out['base'],) if out['base'] else ())))
            if callable(t.choices) and out['value'] and out['value'] not in offered:
                out['choices'] = list(dict.fromkeys(out['choices'] + [out['value']]))
                out['problem'] = msg('settings.pack_missing', value=out['value'])
        if t.choice_labels:
            out['choiceLabels'] = dict(t.choice_labels)
        self.provider_view(t, out)
        if t.key == 'investigate.model':
            out.update(self.card_model_summary())
        if t.key == POST_SEV_KEY and not given and not self.base.slack_post_sev_set:
            self.mark_old_switch(out)
        if t.key in SEV_HOURS_KEYS and not given and SEV_HOURS_KEYS[t.key][1] not in self.base.auto_resolve_sev_set:
            self.mark_old_hours(out)
        if t.key == INFO_DAYS_KEY and not given and not self.base.info_auto_close_days_set and self.current().auto_resolve_hours == 0:
            held = self.stored_old_hours()
            out['oldSwitch'] = held[0][0] if held else next((k for k in OLD_HOURS_KEYS if k in self.values), OLD_HOURS_KEYS[0])
        if t.key == 'core.log-color' and logs.PLAIN[0]:
            out['value'] = NO_COLOR_SHOWN
            out['choices'] = list(dict.fromkeys(out['choices'] + [NO_COLOR_SHOWN]))
        if t.low is not None:
            out.update(low=t.low, high=t.high)
        if t.key == WEB_DOMAINS_KEY:
            own = tuple(self.own_sites()[0])
            out['builtIn'] = list(web_sites.built_in(own))
            out['ownDomains'] = list(own)
            unknown = web_sites.unknown_removed(out['value'], own)
            if unknown and not out.get('problem'):
                out['problem'] = msg('items.minus_unknown.web-host', field=t.label, hosts=', '.join('-' + d for d in unknown))
        if t.key == OWN_SITES_KEY:
            hosts, source = self.own_sites()
            out['ownHosts'] = list(hosts)
            out['ownFrom'] = source
        if t.kind == 'tagrules':
            out['pluginTerms'] = list(tags.plugin_rules().get(t.part, ()))
        if t.kind in TEXT_BOXES:
            out['limit'] = text_limit(t)
            if t.key in TEXT_LINES:
                out['lines'] = TEXT_LINES[t.key]
        if t.kind == 'rules':
            help_of = t.rule_help if t.plugin is not None else RULE_HELP.get(t.key, {})
            out.update(defaults=t.defaults, rules=effective_rules(t, out['value']), baseRules=effective_rules(t, base_text), ruleHelp=dict(help_of or {}))
        if t.items:
            out['items'] = item_view(t)
        if t.key == LINKS_KEY:
            out['links'] = {'max': LINKS_MAX, 'nameMax': LINK_NAME_MAX, 'rules': {rule: msg(f'links.rule.{rule}', max=LINK_NAME_MAX if rule == 'name' else LINKS_MAX) for rule in LINK_RULES}}
            self.hide_refused_links(t, out, given, configured)
        return out

    def card_used(self) -> str:
        now = self.current()
        return resolve_model(now.investigate_model, 'card', now.ai_provider, self.keyed, explicit(now, 'investigate_model'))[1]

    def routed(self, field: str, role: str) -> str:
        now = self.current()
        return resolve_model(getattr(now, field), role, now.ai_provider, self.keyed, explicit(now, field))[0]

    def card_model_summary(self) -> dict:
        offered = list(MODELS) + [m for m in extra_models() if m not in MODELS]
        owners = {m: provider_of_ref(m) for m in offered}
        found = list(dict.fromkeys(list(AI_PROVIDERS) + [p for p in owners.values() if p]))
        return {'modelProviders': owners, 'cardDefaults': {p: resolve_model('', 'card', p, chosen=False)[1] for p in AI_PROVIDERS},
                'keyedProviders': [p for p in found if self.keyed(p)]}

    def provider_view(self, t: Tunable, out: dict) -> None:
        provider = self.current().ai_provider
        off = OFF_WITH.get(self.routed(*OFF_USES[t.key]) if t.key in OFF_USES else provider, {}).get(t.key)
        if off:
            out['off'] = msg(off)
        if not out.get('choices') or not is_model_setting(t):
            return
        offered = list(MODELS) + [m for m in extra_models() if m not in MODELS]
        extra = [out['value']] if out['value'] and out['value'] not in offered else []
        out['choices'] = [''] * ('' in out['choices']) + offered + extra
        out['choiceGroups'] = {m: label_of(provider_of_ref(m)) for m in offered}
        if t.key in OVERRIDE_SEVS and out['value'] and not known_model(out['value']) and provider_of_ref(out['value']) in (provider, None):
            out['problem'] = msg('ai.model_unknown_override', model=out['value'], sev=OVERRIDE_SEVS[t.key], used=self.card_used())
            return
        role = MODEL_ROLES.get(t.key, 'plugin')
        for part in ('base', 'default'):
            if isinstance(out.get(part), str) and out[part] and out[part] == out.get('default'):
                out[part] = resolve_model(out[part], role, provider, chosen=False)[1]
        if not out['value']:
            return
        if out['source'] == 'default':
            out['value'] = resolve_model(out['value'], role, provider, chosen=False)[1]
            return
        owner = provider_of_ref(out['value'])
        if owner in (provider, None) or self.keyed(owner):
            return
        out['problem'] = msg('ai.use_held', owner=label_of(owner))

    def upgrade_models(self, upgraded: Callable[[], bool] = lambda: True) -> list[dict]:
        meta = self.tunables.meta
        if meta.find_one({'_id': UPGRADE_MARK}) is not None:
            return []
        pending = meta.find_one({'_id': UPGRADE_PENDING})
        if pending is None and not upgraded():
            meta.update_one({'_id': UPGRADE_MARK}, {'$setOnInsert': {'at': iso(utc_now()), 'changed': [], 'fresh': True}}, upsert=True)
            return []
        self.refresh(force=True)
        wanted = self.upgrade_wanted()
        if pending is not None:
            held = pending.get('values') or {}
            wanted = [(k, v, before) for k, v, before in wanted if k in held and held[k] == before]
        done, failed = [], {}
        for key, value, before in wanted:
            try:
                t = self.known()[key]
                done.append(self.tunables.write(key, stored(parse(t, value, self.base)), UPGRADE_BY, before))
            except Exception as e:
                failed[key] = before
                log.warning('the upgrade did not write %s and tries again on the next start: %s', key, error_kind(e))
        if failed:
            meta.replace_one({'_id': UPGRADE_PENDING}, {'_id': UPGRADE_PENDING, 'keys': list(failed), 'values': failed, 'at': iso(utc_now())}, upsert=True)
        else:
            meta.update_one({'_id': UPGRADE_MARK}, {'$setOnInsert': {'at': iso(utc_now()), 'changed': [c['key'] for c in done]}}, upsert=True)
            meta.delete_one({'_id': UPGRADE_PENDING})
        self.refresh(force=True)
        if done:
            log.info('the upgrade wrote the models each use billed before as settings: %s', ', '.join(c['key'] for c in done))
        return done

    def upgrade_wanted(self) -> list[tuple[str, str, str]]:
        now = dataclasses.replace(self.current(), ai_models_held=())
        provider = now.ai_provider
        wanted = []
        for key, role in MODEL_ROLES.items():
            t = self.known().get(key)
            if t is None:
                continue
            value = self.now_text(t)
            if not value or not (key in self.overrides or explicit(now, t.field)):
                continue
            owner = provider_of_ref(value)
            if owner is not None and owner not in PROVIDERS:
                continue
            if key in OVERRIDE_SEVS:
                if known_model(value) and owner != provider:
                    wanted.append((key, '', value))
            elif owner not in (provider, None):
                wanted.append((key, self.billed_before(value, role, provider), value))
        for t in self.known().values():
            if t.plugin is not None and t.key.endswith('.model') and self.plugin_value_given(t.key):
                value = str(self.plugin_value(t.key) or '')
                if provider_of_ref(value) not in (provider, None) and provider_of_ref(value) in PROVIDERS:
                    wanted.append((t.key, default_model(provider, 'plugin'), value))
        return wanted

    @staticmethod
    def billed_before(value: str, role: str, provider: str) -> str:
        return value if provider_of(value) in (provider, None) else default_model(provider, role)

    def set_post_sev(self, value: str | None, by: str, old: str | None) -> dict:
        stored_old = self.overrides.get(CRIT_NOW_KEY)
        if value is not None and old is None and not self.base.slack_post_sev_set and (stored_old or not self.base.slack_crit_now):
            old = self.current().slack_post_sev
        cleared = self.tunables.write(CRIT_NOW_KEY, None, by, stored_old.get('value'), legacy=True) if stored_old else None
        if value is None and POST_SEV_KEY not in self.overrides and cleared:
            return cleared
        return self.tunables.write(POST_SEV_KEY, value, by, old)

    def stored_old_hours(self, every: bool = False) -> list[tuple[str, dict]]:
        held = [(k, self.overrides[k]) for k in OLD_HOURS_KEYS if k in self.overrides]
        if not every and (self.base.auto_resolve_hours_set or OLD_HOURS_KEYS[0] in self.overrides):
            held = [h for h in held if h[0] == OLD_HOURS_KEYS[0]]
        return held

    def mark_old_hours(self, out: dict) -> None:
        held = self.stored_old_hours()
        if held:
            key, row = held[0]
            out.update(source='admin', by=row.get('by'), at=row.get('at'), oldSwitch=key)

    def set_severity(self, key: str, value: str | None, by: str, old: str | None) -> dict:
        held = self.stored_old_hours(True)
        field, sev = SEV_HOURS_KEYS[key]
        if not held or key in self.overrides or sev in self.base.auto_resolve_sev_set:
            return self.tunables.write(key, value, by, old)
        now = self.current()
        if value is not None and old is None:
            old = stored(getattr(now, field))
        for other, (other_field, other_sev) in SEV_HOURS_KEYS.items():
            if other != key and other not in self.overrides and other_sev not in self.base.auto_resolve_sev_set and getattr(now, other_field) != getattr(self.base, other_field):
                self.tunables.write(other, stored(getattr(now, other_field)), by, None, legacy=True)
        if INFO_DAYS_KEY not in self.overrides and not self.base.info_auto_close_days_set and now.info_auto_close_days != self.base.info_auto_close_days:
            self.tunables.write(INFO_DAYS_KEY, stored(now.info_auto_close_days), by, None, legacy=True)
        cleared = [self.tunables.write(k, None, by, row.get('value'), legacy=True) for k, row in held]
        if value is None:
            return cleared[0]
        return self.tunables.write(key, value, by, old, record=value != old)

    def mark_old_switch(self, out: dict) -> None:
        stored_old = self.overrides.get(CRIT_NOW_KEY)
        if stored_old:
            out.update(source='admin', by=stored_old.get('by'), at=stored_old.get('at'), oldSwitch=CRIT_NOW_KEY)
        elif not self.base.slack_crit_now:
            out['oldSwitch'] = CRIT_NOW_KEY

    def label_of(self, t: Tunable) -> str:
        key = CONFIG_KEY.get(t.key, t.key)
        if key == LINKS_KEY and key not in self.values and OLD_LINKS_KEY in self.values:
            key = OLD_LINKS_KEY
        if (key in SEV_HOURS_KEYS or key == INFO_DAYS_KEY) and key not in self.values:
            key = next((k for k in OLD_HOURS_KEYS if k in self.values), key)
        raw = self.values.get(key)
        if t.key in TEXT_LIMITS and raw is not None:
            try:
                used = resolve(str(raw), dict(self.values))
            except ConfigurationError:
                used = str(raw)
            if len(used.strip()) > TEXT_LIMITS[t.key]:
                return 'environment'
        return self.labels.get(key, 'environment')

    def hide_refused_links(self, t: Tunable, out: dict, given: dict | None, configured: bool) -> None:
        problems = []
        label = self.label_of(t)
        stored = link_refusal(given.get('value')) if given else None
        if stored:
            problems.append(refused_note('Admin', stored))
            out.update(source='config' if configured else 'default', by=None, at=None)
            if configured:
                out['from'] = label
        base = link_refusal(out['base'])
        if base:
            problems.append(refused_note(label, base))
        for field in ('value', 'base'):
            if link_refusal(out[field]):
                out[field] = None
        if problems:
            out['problem'] = ' '.join(problems)

    def entries(self) -> list[dict]:
        self.refresh(force=True)
        return [self.entry(t) for t in registry(self.extra()) if not t.hidden]

    def set(self, key: str, raw, by: str) -> tuple[dict, dict]:
        t = self.known().get(key) if raw is None else self.settable(key)
        if t is None:
            raise KeyError(key)
        self.refresh(force=True)
        if raw is not None and t.kind not in ('text', 'rolemap') and len(str(raw)) > INPUT_LIMIT:
            raise Invalid(f'{t.label} is longer than {INPUT_LIMIT} characters')
        old = hide_refused(key, (self.overrides.get(key) or {}).get('value'))
        now = self.now_text(t)
        before = hide_refused(key, now)
        if raw is not None and callable(t.choices) and now and str(raw).strip() == now:
            value = now
        else:
            value = None if raw is None else stored(parse(t, raw, self.base))
        if key == POST_SEV_KEY:
            change = self.set_post_sev(value, by, old)
        elif key in SEV_HOURS_KEYS:
            change = self.set_severity(key, value, by, old)
        else:
            change = self.tunables.write(key, value, by, old)
        self.refresh(force=True)
        return self.entry(t), {**change, 'label': t.label, 'before': before, 'after': hide_refused(key, self.now_text(t))}


class View:
    __slots__ = ('_live',)

    def __init__(self, live: Live):
        object.__setattr__(self, '_live', live)

    def __getattr__(self, name: str):
        return getattr(self._live.current(), name)

    def __setattr__(self, name: str, value) -> None:
        raise AttributeError('settings are read only; change them on the Admin tab')
