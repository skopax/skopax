import functools
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping
from urllib.parse import urlsplit

from skopax import brand, logs
from skopax.messages import msg
from skopax.users import ROLES
from skopax.ai import PROVIDERS as AI_PROVIDERS

log = logging.getLogger('skopax.config')

INVESTIGATE_MODES = ('off', 'shadow', 'live')
SLACK_LISTEN_MODES = ('off', 'shadow', 'live')
SLACK_POST_SEVS = ('crit', 'warn', 'info', 'off')
SLACK_POST_SEV = 'warn'
CRIT_NOW_TOLD = 'slack.crit-now'
TOLD: set[str] = set()

PLACEHOLDER = re.compile(r'\$\{([^}:]+)(?::([^}]*))?}')
SESSION_IDLE_HOURS = 72
SESSION_MAX_DAYS = 7
MCP_CLIENT_ID = 'skopax-mcp'
MCP_ROLE = 'SKOPAX_MCP'
MCP_SCOPE = 'mcp'
MCP_WAIT_SECONDS = 45
MCP_DIVE_CAP = 6
SSO_SCOPE = 'openid email profile'
SSO_CLAIM_MODES = ('id-token', 'access-token')
SSO_CLIENT_AUTH = ('auto', 'post', 'basic')
SSO_BUTTON_ICONS = ('auto', 'company', 'none')
SSO_BUTTON_TEXT_MAX = 40
SSO_NEW_ACCOUNTS = ('anyone', 'added-only')
DOMAIN = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', re.IGNORECASE)
RELEASE_NAME = '{project} {yyyy}-{mm}-{dd}'
RELEASE_PLACEHOLDER = re.compile(r'\{([^{}]*)\}')
RELEASE_PARTS = ('project', 'yyyy', 'mm', 'dd')
RELEASE_NEEDS = ('project', 'dd', 'mm')
NEW_ENV_PREFIX = 'SKOPAX_'
ROLES_STALE_MINUTES = 60
LOG_FORMAT_KEY, LOG_COLOR_KEY, LOG_LEVEL_KEY = 'core.log-format', 'core.log-color', 'core.log-level'
BOOT_ENV = ('SKOPAX_PLUGINS', 'SKOPAX_CONFIG_FILE', 'SKOPAX_MODE', 'SKOPAX_AGENT_TOKEN', 'SKOPAX_AGENT_RELAY_HOSTS', 'SKOPAX_KUBE_LOCAL_NAME')
ORG_ABOUT_MAX = 300
SETTING_TEXT_MAX = 20000
DEFAULT_ROLES = tuple(role for role in ROLES if role != 'admin')


def placeholder(name: str) -> str:
    return '{' + name + '}'


def release_problem(template: str) -> str | None:
    if '"' in template or '\\' in template:
        return msg('jira.release_name.quoted')
    found = RELEASE_PLACEHOLDER.findall(template)
    unknown = next((p for p in found if p not in RELEASE_PARTS), None)
    if unknown is not None:
        return msg('jira.release_name.unknown', placeholder=placeholder(unknown), known=', '.join(placeholder(p) for p in RELEASE_PARTS))
    twice = next((p for p in found if found.count(p) > 1), None)
    if twice is not None:
        return msg('jira.release_name.repeated', placeholder=placeholder(twice))
    missing = next((p for p in RELEASE_NEEDS if p not in found), None)
    if missing is not None:
        return msg('jira.release_name.missing', placeholder=placeholder(missing))
    return None


class ConfigurationError(Exception):
    pass


class EnvRecorder(Mapping):
    def __init__(self):
        self.names: set[str] = set()

    def __getitem__(self, key: str) -> str:
        self.names.add(key)
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0


@functools.cache
def settings_env_names() -> frozenset[str]:
    recorder = EnvRecorder()
    load_settings(env=recorder)
    return frozenset(recorder.names) | frozenset(BOOT_ENV)


@dataclass(frozen=True)
class Settings:
    mongo_uri: str
    mongo_db: str
    token: str
    commit_id: str
    secret_key: str = field(default='', repr=False)
    display_name: str = brand.DEFAULT_NAME
    admin_email: str = ''
    admin_password: str = field(default='', repr=False)
    sso_default_role: str = 'viewer'
    public_read: bool = False
    sso_client_id: str = 'skopax'
    sso_client_secret: str = ''
    sso_authorize_url: str = ''
    sso_token_url: str = ''
    sso_jwks_url: str = ''
    sso_issuer: str = ''
    sso_redirect_uri: str = ''
    sso_scope: str = SSO_SCOPE
    sso_on: bool = True
    sso_claims: str = 'id-token'
    sso_client_auth: str = 'auto'
    sso_require_verified_email: bool = False
    sso_new_accounts: str = 'anyone'
    sso_allowed_domains: tuple[str, ...] = ()
    sso_roles_claim: str = 'roles'
    sso_role_map: str = ''
    sso_button_text: str = ''
    sso_button_icon: str = 'auto'
    sso_session_idle_hours: int = SESSION_IDLE_HOURS
    sso_session_max_days: int = SESSION_MAX_DAYS
    operator_role: str = 'SKOPAX_OPERATOR'
    admin_role: str = 'SKOPAX_ADMIN'
    slack_channel: str = '#skopax'
    slack_push_user: str = ''
    slack_listen: str = 'off'
    slack_great_emoji: str = 'snake'
    slack_ask_cap: int = 10
    slack_ask_channels: str = 'all'
    slack_user_ask_cap: int = 4
    page_ask_cap: int = 6
    slack_dm_chats: bool = True
    slack_operator_cache_s: int = 600
    slack_fallback_minutes: int = 15
    slack_crit_now: bool = True
    slack_post_sev: str = SLACK_POST_SEV
    slack_post_sev_set: bool = False
    routes_slack_echo: bool = False
    release_settle: bool = False
    jira_links: bool = True
    assign_dm: bool = True
    postdeploy: bool = True
    probe: bool = False
    cert_rules: str = ''
    cert_skip: tuple[str, ...] = ()
    investigator_on: bool = True
    chat_on: bool = True
    slack_autosend: bool = True
    slack_listener_on: bool = True
    mcp_on: bool = True
    push_on: bool = True
    update_check: bool = True
    tag_report_on: bool = True
    hide_attribution: bool = False
    jira_project: str = ''
    jira_parent: str = ''
    jira_projects: tuple[str, ...] = ()
    jira_release_projects: tuple[str, ...] = ()
    jira_points: float = 1.0
    jira_dev_scope: str = ''
    jira_scope_field: str = ''
    jira_points_field: str = ''
    jira_sprint_field: str = ''
    jira_issue_types: tuple[tuple[str, str], ...] = ()
    jira_accounts: str = ''
    jira_tech_project: str = ''
    jira_tech_assignee: str = ''
    jira_title_prefix: str = ''
    jira_release_name: str = RELEASE_NAME
    anthropic_api_key: str = field(default='', repr=False)
    openai_api_key: str = field(default='', repr=False)
    ai_provider: str = 'anthropic'
    investigate_mode: str = 'off'
    investigate_model: str = 'claude-opus-5-5'
    investigate_effort: str = 'medium'
    investigate_daily_usd: float = 25.0
    investigate_max_cards: int = 5
    investigate_refire_hours: float = 72.0
    investigate_rooms: int = 2
    investigate_max_tool_calls: int = 30
    investigate_assess_max_tool_calls: int = 16
    investigate_gate_model: str = 'claude-haiku-5-5'
    investigate_gate_effort: str = 'low'
    investigate_slack_model: str = 'claude-sonnet-5-5'
    chat_model: str = 'claude-sonnet-5-5'
    investigate_language: str = 'en'
    chat_daily_cap: int = 30
    chat_max_chars: int = 1000
    chat_ui: bool = False
    investigate_slack_effort: str = 'medium'
    investigate_slack_max_tool_calls: int = 10
    investigate_strict_tools: bool = False
    investigate_web_domains: str = ''
    investigate_web_searches: int = 3
    investigate_web_fetches: int = 4
    auto_resolve_hours: int = 6
    auto_resolve_days: int | None = None
    auto_resolve_hours_set: bool = False
    auto_resolve_hours_crit: int = 6
    auto_resolve_hours_warn: int = 6
    auto_resolve_sev_set: tuple[str, ...] = ()
    info_auto_close_days: int = 3
    info_auto_close_days_set: bool = False
    archive_hours: int = 48
    history_keep_days: int = 365
    recurring_count: int = 3
    recurring_days: int = 7
    investigate_keep_days: int = 365
    screenshot_keep_days: int = 30
    screenshot_max_per_day: int = 60
    slack_attach_screenshots: bool = True
    slack_state_notes: bool = True
    slack_remind_hours: float = 24
    slack_clear_note_minutes: float = 15
    selfreview_hour: int = 9
    selfreview_model: str = 'claude-sonnet-5-5'
    selfreview_dm: str = ''
    self_review_enabled: bool = False
    slack_thread_hours: float = 6
    tag_rules: tuple = ()
    tag_report_at: str = '10:00'
    mcp_issuer: str = ''
    mcp_client_id: str = MCP_CLIENT_ID
    mcp_audience: str = ''
    mcp_allowed_clients: tuple[str, ...] = ()
    mcp_required_role: str = MCP_ROLE
    mcp_scope: str = MCP_SCOPE
    mcp_resource_url: str = ''
    mcp_wait_seconds: int = MCP_WAIT_SECONDS
    mcp_dive_cap: int = MCP_DIVE_CAP
    base_url: str = ''
    env_label: str = ''
    instance_links: str = ''
    timezone: str = 'UTC'
    language: str = 'en'
    org_about: str = ''
    link_hosts: tuple[str, ...] = ()
    email_domains: tuple[str, ...] = ()
    self_project: str = ''
    self_log_index: str = ''
    self_deploy: str = ''
    sso_token_headers: str = ''
    jira_developer_field: str = ''
    alert_twins: tuple[str, ...] = ()
    release_ignore: tuple[str, ...] = ()
    log_format: str = logs.TEXT
    log_color: str = logs.ALWAYS
    log_level: str = logs.DEFAULT_LEVEL
    roles_stale_max_minutes: int = ROLES_STALE_MINUTES

    @property
    def release_projects(self) -> tuple[str, ...]:
        return self.jira_release_projects or self.jira_projects

    @property
    def token_headers(self) -> dict[str, str]:
        return headers_of(self.sso_token_headers)


def csv_of(raw: str) -> tuple[str, ...]:
    return tuple(p for p in (part.strip() for part in (raw or '').split(',')) if p)


def bad_domains(entries: tuple[str, ...]) -> list[str]:
    return [p for p in entries if not DOMAIN.fullmatch(p) or p.rsplit('.', 1)[-1].isdigit()]


def sso_domains(raw: str) -> tuple[str, ...]:
    entries = csv_of(raw)
    if wrong := bad_domains(entries):
        raise ConfigurationError(msg('config.sso_domains_invalid', entry=repr(wrong[0][:80])))
    return entries


def sso_new_accounts(raw: str) -> str:
    value = (raw or '').strip().lower() or SSO_NEW_ACCOUNTS[0]
    if value not in SSO_NEW_ACCOUNTS:
        raise ConfigurationError(msg('config.sso_new_accounts_invalid', value=repr(value[:80])))
    return value


def pairs_of(raw: str) -> tuple[tuple[str, str], ...]:
    out = []
    for part in csv_of(raw):
        name, sep, sig = part.partition('=')
        if sep and name.strip() and sig.strip():
            out.append((name.strip(), sig.strip()))
    return tuple(out)


def headers_of(raw: str) -> dict[str, str]:
    out = {}
    for part in (raw or '').split(';'):
        name, sep, value = part.partition(':')
        if sep and name.strip():
            out[name.strip()] = value.strip()
    return out


def origin_of(url: str) -> str:
    parts = urlsplit((url or '').strip())
    return f'{parts.scheme}://{parts.netloc}' if parts.scheme and parts.netloc else ''


def resolve(value: str, config: dict[str, str], seen: frozenset[str] = frozenset()) -> str:
    def replace(m: re.Match) -> str:
        key, default = m.group(1), m.group(2)
        if key in seen:
            raise ConfigurationError(f'circular placeholder ${{{key}}}')
        found = config.get(key, os.environ.get(key))
        if found is None:
            if default is None:
                raise ConfigurationError(f'unresolved placeholder ${{{key}}}')
            return default
        return resolve(found, config, seen | {key})

    return PLACEHOLDER.sub(replace, value)


class Setting:
    def __init__(self, field: str, conv: Callable | None = None):
        self.field = field
        self.conv = conv

    def __set_name__(self, owner, name: str) -> None:
        self.name = name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        if self.name in obj.__dict__:
            return obj.__dict__[self.name]
        value = getattr(obj.settings, self.field)
        return self.conv(value) if self.conv else value

    def __set__(self, obj, value) -> None:
        obj.__dict__[self.name] = value


def tags_rules_of(config: dict[str, str]) -> tuple:
    from skopax.tags import rules_of
    return rules_of({k: resolve(v, config) for k, v in config.items() if k.startswith('tags.')})


def whole_in(raw: str, low: int, high: int) -> bool:
    try:
        return low <= int(raw) <= high
    except ValueError:
        return False


def bounded(key: str, raw: str, low: int, high: int, default: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is not None and low <= value <= high:
        return value
    log.warning('%s %r is not a whole number from %d to %d; using %d', key, raw, low, high, default)
    return default


def sso_role_map(raw: str) -> str:
    from skopax.roles import map_text, parse_role_map
    from skopax.tunables import Invalid

    text = str(raw or '').strip()
    if not text:
        return ''
    try:
        return map_text(parse_role_map(text))
    except Invalid as e:
        raise ConfigurationError(msg('config.role_map_invalid', problem=e.text)) from None


def button_text(raw: str) -> str:
    text = str(raw or '').strip()
    if len(text) > SSO_BUTTON_TEXT_MAX:
        log.warning('sso.button-text is longer than %d characters; using the first %d', SSO_BUTTON_TEXT_MAX, SSO_BUTTON_TEXT_MAX)
    return text[:SSO_BUTTON_TEXT_MAX]


def choice(key: str, raw: str, allowed: tuple[str, ...]) -> str:
    value = raw.strip().lower()
    if value in allowed:
        return value
    log.warning('%s %r is not one of %s; using %s', key, raw, ', '.join(allowed), allowed[0])
    return allowed[0]


def load_settings(fetch: Callable[[], dict[str, str]] | None = None, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    config = fetch() if fetch else {}

    def get(key: str, var: str, default: str) -> str:
        if key in config:
            return resolve(config[key], config)
        return env.get(var, default)

    mongo_uri = get('mongo.uri', 'MONGO_URI', 'mongodb://localhost:27017')
    uri_db = urlsplit(mongo_uri).path.lstrip('/')

    mode = get('investigate.mode', 'INVESTIGATE_MODE', 'off').strip().lower()
    if mode not in INVESTIGATE_MODES:
        log.warning('investigate.mode %r is not one of %s; the investigator stays off', mode, ', '.join(INVESTIGATE_MODES))
        mode = 'off'

    provider = get('investigate.provider', 'SKOPAX_AI_PROVIDER', 'anthropic').strip().lower() or 'anthropic'
    if provider not in AI_PROVIDERS:
        log.warning('investigate.provider %r is not one of %s; Anthropic is used', provider, ', '.join(AI_PROVIDERS))
        provider = 'anthropic'

    listen = get('slack.listen', 'SLACK_LISTEN', 'off').strip().lower()
    if listen not in SLACK_LISTEN_MODES:
        log.warning('slack.listen %r is not one of %s; Slack listening stays off', listen, ', '.join(SLACK_LISTEN_MODES))
        listen = 'off'

    max_days = bounded('sso.session-max-days', get('sso.session-max-days', 'SSO_SESSION_MAX_DAYS', str(SESSION_MAX_DAYS)), 1, 30, SESSION_MAX_DAYS)
    idle_hours = bounded('sso.session-idle-hours', get('sso.session-idle-hours', 'SSO_SESSION_IDLE_HOURS', str(SESSION_IDLE_HOURS)), 1, 168, SESSION_IDLE_HOURS)
    if idle_hours > max_days * 24:
        fallback = min(SESSION_IDLE_HOURS, max_days * 24)
        log.warning('sso.session-idle-hours %d is above sso.session-max-days %d; using %d', idle_hours, max_days, fallback)
        idle_hours = fallback

    auto_hours_raw = get('core.auto-resolve-hours', 'SKOPAX_AUTO_RESOLVE_HOURS', '').strip()
    auto_days_raw = get('core.auto-resolve-days', 'SKOPAX_AUTO_RESOLVE_DAYS', '').strip()
    try:
        auto_days = int(auto_days_raw) if auto_days_raw else None
    except ValueError:
        log.warning('core.auto-resolve-days %r is not a whole number; it is ignored', auto_days_raw)
        auto_days = None
    if auto_hours_raw:
        auto_hours = bounded('core.auto-resolve-hours', auto_hours_raw, 0, 720, 6)
    else:
        auto_hours = min(max(auto_days, 0), 30) * 24 if auto_days is not None else 6
    sev_hours, sev_set = {}, []
    for sev in ('crit', 'warn'):
        sev_raw = get(f'core.auto-resolve-hours-{sev}', f'SKOPAX_AUTO_RESOLVE_HOURS_{sev.upper()}', '').strip()
        sev_hours[sev] = bounded(f'core.auto-resolve-hours-{sev}', sev_raw, 0, 720, auto_hours) if sev_raw else auto_hours
        if sev_raw and whole_in(sev_raw, 0, 720):
            sev_set.append(sev)
    info_raw = get('core.info-auto-close-days', 'SKOPAX_INFO_AUTO_CLOSE_DAYS', '').strip()
    info_fallback = 0 if auto_hours == 0 else 3
    info_days = bounded('core.info-auto-close-days', info_raw, 0, 90, info_fallback) if info_raw else info_fallback
    info_set = bool(info_raw) and whole_in(info_raw, 0, 90)

    default_role = get('sso.default-role', 'SSO_DEFAULT_ROLE', 'viewer').strip() or 'viewer'
    if default_role not in DEFAULT_ROLES:
        raise ConfigurationError(msg('config.default_role_invalid', value=repr(default_role)))
    role_map = sso_role_map(get('sso.role-map', 'SSO_ROLE_MAP', ''))

    org_about = get('core.org-about', 'SKOPAX_ORG_ABOUT', '').strip()
    if len(org_about) > ORG_ABOUT_MAX and 'core.org-about' in config:
        log.warning('%s', msg('settings.source_too_long', key='core.org-about', limit=ORG_ABOUT_MAX))
        org_about = env.get('SKOPAX_ORG_ABOUT', '').strip()
    if len(org_about) > ORG_ABOUT_MAX:
        raise ConfigurationError(msg('settings.env_too_long', name='SKOPAX_ORG_ABOUT', limit=ORG_ABOUT_MAX))

    def log_setting(key: str, var: str, allowed: tuple[str, ...], default: str) -> str:
        return logs.chosen(key, resolve(config[key], config), allowed, default) if key in config else logs.chosen(var, env.get(var, ''), allowed, default)

    log_color = logs.NEVER if env.get(logs.NO_COLOR_ENV) else log_setting(LOG_COLOR_KEY, logs.COLOR_ENV, logs.COLORS, logs.ALWAYS)

    post_sev_raw = get('slack.post-sev', 'SLACK_POST_SEV', '').strip().lower()
    crit_now_raw = get('slack.crit-now', 'SLACK_CRIT_NOW', '').strip().lower()
    if crit_now_raw and CRIT_NOW_TOLD not in TOLD:
        TOLD.add(CRIT_NOW_TOLD)
        log.warning('slack.crit-now (SLACK_CRIT_NOW) is replaced by slack.post-sev (SLACK_POST_SEV); it still counts while slack.post-sev is not set')
    if post_sev_raw and post_sev_raw not in SLACK_POST_SEVS:
        log.warning('slack.post-sev %r is not one of %s; using %s', post_sev_raw, ', '.join(SLACK_POST_SEVS), SLACK_POST_SEV)
    post_sev_set = post_sev_raw in SLACK_POST_SEVS
    crit_now = crit_now_raw in ('', 'true')
    post_sev = post_sev_raw if post_sev_set else SLACK_POST_SEV if crit_now else 'off'

    release_template = get('jira.release-name', 'JIRA_RELEASE_NAME', RELEASE_NAME).strip() or RELEASE_NAME
    release_refused = release_problem(release_template)
    if release_refused:
        raise ConfigurationError(release_refused)

    return Settings(
        mongo_uri=mongo_uri,
        mongo_db=get('mongo.database', 'MONGO_DB', uri_db or 'skopax'),
        token=get('core.token', 'SKOPAX_TOKEN', ''),
        commit_id=env.get('COMMIT_ID', 'dev'),
        secret_key=get('core.secret-key', 'SKOPAX_SECRET_KEY', '').strip(),
        display_name=brand.clean_name(get('core.display-name', 'SKOPAX_DISPLAY_NAME', '')),
        admin_email=get('core.admin-email', 'SKOPAX_ADMIN_EMAIL', '').strip(),
        admin_password=get('core.admin-password', 'SKOPAX_ADMIN_PASSWORD', ''),
        sso_default_role=default_role,
        public_read=get('core.public-read', 'SKOPAX_PUBLIC_READ', 'false').strip().lower() == 'true',
        sso_client_id=get('sso.client-id', 'SSO_CLIENT_ID', 'skopax'),
        sso_client_secret=get('sso.client-secret', 'SSO_CLIENT_SECRET', ''),
        sso_authorize_url=get('sso.authorize-url', 'SSO_AUTHORIZE_URL', ''),
        sso_token_url=get('sso.token-url', 'SSO_TOKEN_URL', ''),
        sso_jwks_url=get('sso.jwks-url', 'SSO_JWKS_URL', ''),
        sso_issuer=get('sso.issuer', 'SSO_ISSUER', ''),
        sso_redirect_uri=get('sso.redirect-uri', 'SSO_REDIRECT_URI', ''),
        sso_scope=get('sso.scope', 'SSO_SCOPE', SSO_SCOPE).strip() or SSO_SCOPE,
        sso_on=get('sso.enabled', 'SSO_ENABLED', 'true').strip().lower() == 'true',
        sso_claims=choice('sso.claims', get('sso.claims', 'SSO_CLAIMS', SSO_CLAIM_MODES[0]), SSO_CLAIM_MODES),
        sso_client_auth=choice('sso.client-auth', get('sso.client-auth', 'SSO_CLIENT_AUTH', SSO_CLIENT_AUTH[0]), SSO_CLIENT_AUTH),
        sso_require_verified_email=get('sso.require-verified-email', 'SSO_REQUIRE_VERIFIED_EMAIL', 'false').strip().lower() == 'true',
        sso_new_accounts=sso_new_accounts(get('sso.new-accounts', 'SSO_NEW_ACCOUNTS', SSO_NEW_ACCOUNTS[0])),
        sso_allowed_domains=sso_domains(get('sso.allowed-domains', 'SSO_ALLOWED_DOMAINS', '')),
        sso_roles_claim=get('sso.roles-claim', 'SSO_ROLES_CLAIM', 'roles').strip() or 'roles',
        sso_role_map=role_map,
        sso_button_text=button_text(get('sso.button-text', 'SSO_BUTTON_TEXT', '')),
        sso_button_icon=choice('sso.button-icon', get('sso.button-icon', 'SSO_BUTTON_ICON', SSO_BUTTON_ICONS[0]), SSO_BUTTON_ICONS),
        sso_session_idle_hours=idle_hours,
        sso_session_max_days=max_days,
        operator_role=get('core.operator-role', 'SKOPAX_OPERATOR_ROLE', 'SKOPAX_OPERATOR'),
        admin_role=get('core.admin-role', 'SKOPAX_ADMIN_ROLE', 'SKOPAX_ADMIN').strip() or 'SKOPAX_ADMIN',
        slack_channel=get('slack.channel', 'SLACK_CHANNEL', '#skopax'),
        slack_push_user=get('slack.push-user', 'SLACK_PUSH_USER', '').strip(),
        slack_listen=listen,
        slack_ask_cap=int(get('slack.ask-cap', 'SLACK_ASK_CAP', '10')),
        slack_ask_channels=get('slack.ask-channels', 'SLACK_ASK_CHANNELS', 'all').strip() or 'all',
        slack_user_ask_cap=int(get('slack.user-ask-cap', 'SLACK_USER_ASK_CAP', '4').strip() or 4),
        page_ask_cap=int(get('page.ask-cap', 'PAGE_ASK_CAP', '6').strip() or 6),
        slack_dm_chats=get('slack.dm-chats', 'SLACK_DM_CHATS', 'true').strip().lower() == 'true',
        slack_operator_cache_s=int(get('slack.operator-cache-seconds', 'SLACK_OPERATOR_CACHE_SECONDS', '600')),
        slack_fallback_minutes=int(get('slack.fallback-minutes', 'SLACK_FALLBACK_MINUTES', '15').strip() or 15),
        routes_slack_echo=get('routes.slack-echo', 'ROUTES_SLACK_ECHO', 'false').strip().lower() == 'true',
        slack_crit_now=crit_now,
        slack_post_sev=post_sev,
        slack_post_sev_set=post_sev_set,
        release_settle=get('core.release-settle', 'SKOPAX_RELEASE_SETTLE', 'false').strip().lower() == 'true',
        jira_links=get('core.jira-links', 'SKOPAX_JIRA_LINKS', 'true').strip().lower() == 'true',
        assign_dm=get('core.assign-dm', 'SKOPAX_ASSIGN_DM', 'true').strip().lower() == 'true',
        postdeploy=get('core.postdeploy', 'SKOPAX_POSTDEPLOY', 'true').strip().lower() == 'true',
        probe=get('core.probe', 'SKOPAX_PROBE', 'true').strip().lower() == 'true',
        cert_rules=get('core.certs', 'SKOPAX_CERT_RULES', '').strip(),
        cert_skip=csv_of(get('core.cert-skip', 'SKOPAX_CERT_SKIP', '')),
        investigator_on=get('core.investigator', 'SKOPAX_FEATURE_INVESTIGATOR', 'true').strip().lower() == 'true',
        chat_on=get('core.chat', 'SKOPAX_FEATURE_CHAT', 'true').strip().lower() == 'true',
        slack_autosend=get('core.slack-autosend', 'SKOPAX_FEATURE_SLACK_AUTOSEND', 'true').strip().lower() == 'true',
        slack_listener_on=get('core.slack-listener', 'SKOPAX_FEATURE_SLACK_LISTENER', 'true').strip().lower() == 'true',
        mcp_on=get('core.mcp', 'SKOPAX_FEATURE_MCP', 'true').strip().lower() == 'true',
        push_on=get('core.push', 'SKOPAX_FEATURE_PUSH', 'true').strip().lower() == 'true',
        update_check=get('core.update-check', 'SKOPAX_UPDATE_CHECK', 'true').strip().lower() == 'true',
        tag_report_on=get('core.tag-report', 'SKOPAX_FEATURE_TAG_REPORT', 'true').strip().lower() == 'true',
        hide_attribution=get('core.hide-attribution', 'SKOPAX_FEATURE_HIDE_ATTRIBUTION', 'false').strip().lower() == 'true',
        jira_project=get('jira.project', 'JIRA_PROJECT', '').strip(),
        jira_parent=get('jira.parent', 'JIRA_PARENT', '').strip(),
        jira_projects=csv_of(get('jira.projects', 'JIRA_PROJECTS', '')),
        jira_release_projects=csv_of(get('jira.release-projects', 'JIRA_RELEASE_PROJECTS', '')),
        jira_points=float(get('jira.points', 'JIRA_POINTS', '1').strip() or 1),
        jira_dev_scope=get('jira.dev-scope', 'JIRA_DEV_SCOPE', '').strip(),
        jira_scope_field=get('jira.scope-field', 'JIRA_SCOPE_FIELD', '').strip(),
        jira_points_field=get('jira.points-field', 'JIRA_POINTS_FIELD', '').strip(),
        jira_sprint_field=get('jira.sprint-field', 'JIRA_SPRINT_FIELD', '').strip(),
        jira_issue_types=tuple((k.upper(), v) for k, v in pairs_of(get('jira.issue-types', 'JIRA_ISSUE_TYPES', ''))),
        jira_accounts=get('jira.accounts', 'JIRA_ACCOUNTS', '').strip(),
        jira_tech_project=get('jira.tech-project', 'JIRA_TECH_PROJECT', '').strip(),
        jira_tech_assignee=get('jira.tech-assignee', 'JIRA_TECH_ASSIGNEE', '').strip(),
        jira_title_prefix=get('jira.title-prefix', 'JIRA_TITLE_PREFIX', ''),
        jira_release_name=release_template,
        anthropic_api_key=get('anthropic.api-key', 'ANTHROPIC_API_KEY', '').strip(),
        openai_api_key=get('openai.api-key', 'OPENAI_API_KEY', '').strip(),
        ai_provider=provider,
        investigate_mode=mode,
        investigate_model=get('investigate.model', 'INVESTIGATE_MODEL', 'claude-opus-5-5').strip(),
        investigate_effort=get('investigate.effort', 'INVESTIGATE_EFFORT', 'medium').strip(),
        investigate_daily_usd=float(get('investigate.daily-usd', 'INVESTIGATE_DAILY_USD', '25')),
        investigate_max_cards=int(get('investigate.max-cards', 'INVESTIGATE_MAX_CARDS', '5')),
        investigate_refire_hours=float(get('investigate.refire-hours', 'INVESTIGATE_REFIRE_HOURS', '72').strip() or 72),
        investigate_rooms=max(1, int(get('investigate.rooms', 'INVESTIGATE_ROOMS', '2').strip() or 2)),
        investigate_max_tool_calls=int(get('investigate.max-tool-calls', 'INVESTIGATE_MAX_TOOL_CALLS', '30')),
        investigate_assess_max_tool_calls=int(get('investigate.assess-max-tool-calls', 'INVESTIGATE_ASSESS_MAX_TOOL_CALLS', '16')),
        investigate_gate_model=get('investigate.gate-model', 'INVESTIGATE_GATE_MODEL', 'claude-haiku-5-5').strip(),
        investigate_gate_effort=get('investigate.gate-effort', 'INVESTIGATE_GATE_EFFORT', 'low').strip() or 'low',
        investigate_slack_model=get('investigate.slack-model', 'INVESTIGATE_SLACK_MODEL', 'claude-sonnet-5-5').strip(),
        chat_model=get('chat.model', 'CHAT_MODEL', 'claude-sonnet-5-5').strip(),
        investigate_language=(get('investigate.language', 'INVESTIGATE_LANGUAGE', 'en').strip() or 'en'),
        chat_daily_cap=int(get('chat.daily-cap', 'CHAT_DAILY_CAP', '30').strip() or 30),
        chat_max_chars=int(get('chat.max-chars', 'CHAT_MAX_CHARS', '1000').strip() or 1000),
        chat_ui=get('chat.ui', 'CHAT_UI', 'true').strip().lower() == 'true',
        investigate_slack_effort=get('investigate.slack-effort', 'INVESTIGATE_SLACK_EFFORT', 'medium').strip(),
        investigate_slack_max_tool_calls=int(get('investigate.slack-max-tool-calls', 'INVESTIGATE_SLACK_MAX_TOOL_CALLS', '10')),
        investigate_strict_tools=get('investigate.strict-tools', 'INVESTIGATE_STRICT_TOOLS', 'false').strip().lower() == 'true',
        investigate_web_domains=get('investigate.web-domains', 'INVESTIGATE_WEB_DOMAINS', '').strip(),
        investigate_web_searches=int(get('investigate.web-searches', 'INVESTIGATE_WEB_SEARCHES', '3').strip() or 3),
        investigate_web_fetches=int(get('investigate.web-fetches', 'INVESTIGATE_WEB_FETCHES', '4').strip() or 4),
        auto_resolve_hours=auto_hours,
        auto_resolve_days=auto_days,
        auto_resolve_hours_set=bool(auto_hours_raw),
        auto_resolve_hours_crit=sev_hours['crit'],
        auto_resolve_hours_warn=sev_hours['warn'],
        auto_resolve_sev_set=tuple(sev_set),
        info_auto_close_days=info_days,
        info_auto_close_days_set=info_set,
        archive_hours=int(get('cards.archive-hours', 'CARDS_ARCHIVE_HOURS', '48').strip() or 48),
        history_keep_days=int(get('history.keep-days', 'HISTORY_KEEP_DAYS', '365').strip() or 365),
        recurring_count=int(get('recurring.count', 'RECURRING_COUNT', '3').strip() or 3),
        recurring_days=int(get('recurring.days', 'RECURRING_DAYS', '7').strip() or 7),
        investigate_keep_days=int(get('investigate.keep-days', 'INVESTIGATE_KEEP_DAYS', '365').strip() or 365),
        screenshot_keep_days=int(get('screenshot.keep-days', 'SCREENSHOT_KEEP_DAYS', '30').strip() or 30),
        screenshot_max_per_day=int(get('screenshot.max-per-day', 'SCREENSHOT_MAX_PER_DAY', '60').strip() or 60),
        slack_attach_screenshots=get('slack.attach-screenshots', 'SLACK_ATTACH_SCREENSHOTS', 'true').strip().lower() == 'true',
        slack_state_notes=get('slack.state-notes', 'SLACK_STATE_NOTES', 'true').strip().lower() == 'true',
        slack_remind_hours=float(get('slack.remind-hours', 'SLACK_REMIND_HOURS', '24').strip() or 24),
        slack_clear_note_minutes=float(get('slack.clear-note-minutes', 'SLACK_CLEAR_NOTE_MINUTES', '15').strip() or 15),
        selfreview_hour=bounded('self-review.hour', get('self-review.hour', 'SELF_REVIEW_HOUR', '9').strip() or '9', -1, 23, 9),
        selfreview_model=get('self-review.model', 'SELF_REVIEW_MODEL', 'claude-sonnet-5-5').strip(),
        selfreview_dm=get('self-review.dm', 'SELF_REVIEW_DM', '').strip(),
        self_review_enabled=get('self-review.enabled', 'SKOPAX_SELF_REVIEW', 'false').strip().lower() == 'true',
        slack_great_emoji=get('slack.great-emoji', 'SLACK_GREAT_EMOJI', 'snake').strip(),
        slack_thread_hours=float(get('slack.thread-hours', 'SLACK_THREAD_HOURS', '6').strip() or 6),
        tag_rules=tags_rules_of(config),
        tag_report_at=get('tags.report-at', 'SKOPAX_TAG_REPORT_AT', '10:00').strip(),
        mcp_issuer=get('mcp.issuer', 'MCP_ISSUER', '').strip().rstrip('/'),
        mcp_client_id=get('mcp.client-id', 'MCP_CLIENT_ID', MCP_CLIENT_ID).strip() or MCP_CLIENT_ID,
        mcp_audience=get('mcp.audience', 'MCP_AUDIENCE', '').strip(),
        mcp_allowed_clients=csv_of(get('mcp.allowed-clients', 'MCP_ALLOWED_CLIENTS', '')),
        mcp_required_role=get('mcp.required-role', 'MCP_REQUIRED_ROLE', MCP_ROLE).strip(),
        mcp_scope=get('mcp.scope', 'MCP_SCOPE', MCP_SCOPE).strip() or MCP_SCOPE,
        mcp_resource_url=get('mcp.resource-url', 'MCP_RESOURCE_URL', '').strip().rstrip('/'),
        mcp_wait_seconds=bounded('mcp.wait-seconds', get('mcp.wait-seconds', 'MCP_WAIT_SECONDS', str(MCP_WAIT_SECONDS)).strip() or str(MCP_WAIT_SECONDS), 0, 55, MCP_WAIT_SECONDS),
        mcp_dive_cap=bounded('mcp.dive-cap', get('mcp.dive-cap', 'MCP_DIVE_CAP', str(MCP_DIVE_CAP)).strip() or str(MCP_DIVE_CAP), 0, 50, MCP_DIVE_CAP),
        base_url=get('core.base-url', 'SKOPAX_BASE_URL', '').strip(),
        env_label=get('core.env-label', 'SKOPAX_ENV_LABEL', '').strip(),
        instance_links=get('core.instance-links', 'SKOPAX_INSTANCE_LINKS', '').strip(),
        timezone=get('core.timezone', 'SKOPAX_TIMEZONE', 'UTC').strip() or 'UTC',
        language=get('core.language', 'SKOPAX_LANGUAGE', 'en').strip() or 'en',
        org_about=org_about,
        link_hosts=csv_of(get('core.link-hosts', 'SKOPAX_LINK_HOSTS', '')),
        email_domains=csv_of(get('core.email-domains', 'SKOPAX_EMAIL_DOMAINS', '')),
        self_project=get('core.self-project', 'SKOPAX_SELF_PROJECT', '').strip(),
        self_log_index=get('core.self-log-index', 'SKOPAX_SELF_LOG_INDEX', '').strip(),
        self_deploy=get('core.self-deploy', 'SKOPAX_SELF_DEPLOY', '').strip(),
        sso_token_headers=get('sso.token-headers', 'SSO_TOKEN_HEADERS', '').strip(),
        jira_developer_field=get('jira.developer-field', 'JIRA_DEVELOPER_FIELD', '').strip(),
        alert_twins=csv_of(get('core.alert-twins', 'SKOPAX_ALERT_TWINS', '')),
        release_ignore=csv_of(get('core.release-ignore', 'SKOPAX_RELEASE_IGNORE', '')),
        log_format=log_setting(LOG_FORMAT_KEY, logs.FORMAT_ENV, logs.FORMATS, logs.TEXT),
        log_color=log_color,
        log_level=log_setting(LOG_LEVEL_KEY, logs.LEVEL_ENV, logs.LEVEL_NAMES, logs.DEFAULT_LEVEL),
        roles_stale_max_minutes=bounded('roles.stale-max-minutes', get('roles.stale-max-minutes', 'SSO_ROLE_STALE_MAX_MINUTES', str(ROLES_STALE_MINUTES)).strip() or str(ROLES_STALE_MINUTES), 0, 1440,
                                        ROLES_STALE_MINUTES),
    )
