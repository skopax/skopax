import asyncio
import hashlib
import hmac
import html
import json
import datetime as dt
import logging
import math
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Callable, Mapping
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError
from starlette.concurrency import run_in_threadpool

from skopax.cardlink import card_url, chat_url
from skopax.cardposts import CardPosts, drawn_sig
from skopax.codehosts import CodeHosts
from skopax.auth import LOGIN_COOKIE, LOGIN_SECONDS, SESSION_COOKIE, bearer_token, safe_next
from skopax import actions as page_actions
from skopax import __version__, backfill, brand, causes, clock as clocks, costs, drafts, history, messages, redact as redaction, tags, toolusage
from skopax.jiralinks import JiraLinks
from skopax.kube import Kube
from skopax import mcpserver
from skopax import mutemarks
from skopax.mcpauth import McpAuth
from skopax.mcptools import PLUGIN_ID as MCPTOOLS_ID
from skopax.mcptools import routes as mcptools_routes
from skopax.mcptools.bridge import ToolBridge, asker_role
from skopax.mcptools.catalog import Catalog as McpCatalog
from skopax.mcptools.oauth import McpOAuth
from skopax.actors import BOOTSTRAP_TOKEN as BOOTSTRAP, is_token
from skopax.apitokens import Actor, ApiTokens, TokenNameError, UnknownToken
from skopax.messages import msg
from skopax.oidc import OidcError
from skopax.operators import OperatorUnknown, Operators
from skopax.postdeploy import JOB_OFFSET as POSTDEPLOY_OFFSET, JOB_PERIOD as POSTDEPLOY_PERIOD, SERVER_BY, PostDeploy
from skopax.attach import Attacher
from skopax.autosend import AutoSend, CritNow, Fallback, waiting as auto_send_waiting
from skopax.config import ConfigurationError, Settings
from skopax.contract import CodeHost, OrgPack, SourceDown
from skopax.engine import Applied, PassResult, apply, iso as epoch_iso, new_pushes
from skopax.enrich import Enricher
from skopax.investigator import JOB_OFFSET as INVESTIGATE_OFFSET, JOB_PERIOD as INVESTIGATE_PERIOD, Investigator, prompt, select
from skopax.investigator.gate import Gate, GateDown
from skopax.investigator.smalltalk import DEFAULTS as SMALL_TALK_DEFAULTS, Reply, SmallTalk, WINDOW_S as SMALL_TALK_WINDOW_S
from skopax.investigator.tools import CORE_TOOLS, FIX_FIELDS, Effects, Sources, allowed_link_hosts
from skopax.investigator.toolset import NoProposal, ProposalClosed, ProposalUnavailable, Proposals, ToolSet
from skopax import chatui
from skopax.chatui import CHAT_ID
from skopax.chatui.text import set_texts as set_chat_texts
from skopax.assign import Assigner, Invalid as AssignInvalid, people_of, slug
from skopax.jiralinks import ticket_projects
from skopax.models import email_allowed, NoticesReadIn, PasswordIn, AssigneeIn, ChatIn, ChatShareIn, ActionIn, ActionPatch, ActionStatus, Assessment, DeployIn, DeployReportIn, GateExamplesIn, InvestigateIn, PostdeployIgnoreIn, Note, ReportBriefIn, SelfReviewIn, ReportSlackDraftIn, SlackDraftIn, SlackHoldIn, SlackPostIn, SlackUpdateIn, SmallTalkIn, TagIn, BusinessIn, AdminSettingIn, InstanceIn, InstancePatch, PluginSwitchIn, PluginTestIn, TokenIn, UserIn, UserPatch, FeatureIn, AiKeyIn, AiKeyTestIn
from skopax.selfreview import BY as SELF_REVIEW_BY, SelfReview, self_service, gather as review_facts, review_slot_offset
from skopax.slackhealth import OFFSET as SLACK_POSTS_OFFSET, PASS as SLACK_POSTS, PERIOD as SLACK_POSTS_PERIOD, slack_posts_pass
from skopax.jiraaccess import PASS as JIRA_ACCESS, access_pass as jira_access_pass
from skopax.slacklink import SlackLink
from skopax.kit.promql import Prometheus, client_for
from skopax.kit.promql import CHECK as PROMETHEUS_CHECK, ID as PROMETHEUS_ID
from skopax.tickets import PROJECT_KEY, projects_of
from skopax.users import IDP_SOURCES, NAME_MAX, PASSWORD_MAX, PASSWORD_MIN, ROLES, IdpManaged, InvalidUser, LastAdmin, LINK_PENDING_S, UserExists, Users, admin_row, clean_email, clean_name, clean_password, clean_role, leaves_admin, link_admits
from skopax.settingsources import Follower, SourceSet, boot, labelled, merged
from skopax.settingsources import core_knowledge_ids, core_setting_ids
from skopax.tunables import SESSIONS_AFTER_KEY, SLACK_CHANNEL_KEY, SSO_SWITCH_KEY, Invalid as TunableInvalid, Live, TunableStore, View, apply as apply_tunables, parse as parse_tunable, plugin_tunables, stored as stored_text
from skopax.tunables import instance_menu
from skopax.push import Pusher, push_users
from skopax.redact import redact
from skopax.scheduler import Job, Scheduler
from skopax.settle import Settle, ignore_doc, ignored
from skopax.slackevents import Clicks, Intake, Listener, Replier, TicketRefused, socket_client
from skopax.clearnotes import ClearNotes
from skopax.notices import Notices
from skopax.versioncheck import OFFSET_S as VERSION_OFFSET, PERIOD_S as VERSION_PERIOD, START_DELAY_S as VERSION_START_DELAY, VersionCheck, jitter_of
from skopax.reminders import Reminders
from skopax.routes import TRIES, Echo, Refused as RouteRefused, Routes
from skopax.models import RouteSendIn, SsoIn, SsoTestIn
from skopax.statenotes import StateNotes, who_of
from skopax.slack import anomaly_message, fix_hash, fix_message, AUTO_BY, same_person, built_link, cut, esc, esc_keep_mentions, menu_kinds, NoThread, opening, reason_of, recurring_line, report_message, Slack, SlackError
from skopax.setup import BadCode, SetupDone, SetupGate, render_form
from skopax import migrations
from skopax.errkind import NoAccess, Refusal, kind, text_of
from skopax.features import BY_ID as FEATURE_BY_ID, FEATURES, Features, Locked, off_text
from skopax.instances import InstanceExists, InstanceMissing, Instances, InvalidInstance, PluginState, SourceOwned, Writer, checks_off
from skopax.registry import Registry, card_types_view, describe, locale_drops
from skopax.plugai import PluginAi
from skopax.plugintools import PluginTools
from skopax.runtime import Runtime
from skopax.monitor import rules_of as monitor_rules
from skopax.probe import CERT_RULES, Probe, cert_pass, public as public_reach, summary as probe_summary
from skopax import promqlroutes
from skopax.promqlchecks import JOB_OFFSET as CUSTOM_CHECKS_OFFSET, JOB_PERIOD as CUSTOM_CHECKS_PERIOD, PASS as CUSTOM_CHECKS, CustomChecks
from skopax.sweepout import Outputs
from skopax.sweeps import Reads, Sweeper, owned, pass_name, watched
from skopax.sealer import Sealer, master_key, stored_key
from skopax.aikey import AiKey, InvalidKey, TestBusy
from skopax.remote import Transport
from skopax.sessions import LOGIN_KIND, SESSION_KIND, Sessions, User, role_at_least, session_secret
from skopax.roles import Resolvers, role_map
from skopax.signin import Refused, SignIn
from skopax import ssobutton
from skopax.ssoicon import ICON_MAX as SSO_ICON_MAX, IconRefused, SsoIcon, check as icon_check
from skopax.sso import FIELDS as SSO_FIELDS, SECRET_FIELD as SSO_SECRET_FIELD, SECRET_ID as SSO_SECRET_ID, SECRET_MAX as SSO_SECRET_MAX, InvalidSecret, SignOn, SourceOwned as SecretOwned, SsoConfig, SsoHttp, SsoSecret, TestBusy as SsoTestBusy
from skopax.throttle import Throttle
from skopax.store import ActionMoved, ChatPrivate, Conflict, NotFound, Store, iso, new_id, utc_now

OPEN_PATHS = ('/actuator/health', '/actuator/health/*', '/static/*', '/icons/*', '/favicon.ico', '/plugins/*', '/manifest.webmanifest', '/setup', '/login', '/login/sso', '/authorized', '/logout', '/mcp',
              '/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/*', '/login/sso-icon')
READ_METHODS = ('GET', 'HEAD')
FORM_BODY_MAX = 16384
LOGIN_PAGE = Path(__file__).parent / 'web' / 'login.html'
LOGIN_SLOT = re.compile(r'\[\[(\w+)\]\]')
LOGIN_FIELDS = ('title', 'email', 'password', 'submit')
EMAIL_KEY_MAX = 254
PROXY_NOTE_SECONDS = 3600
NOT_ADDED_S = 3600
NOT_ADDED_KEEP = 1000
PROXY_THREAD = 'skopax-proxy-sighting'
ERROR_FIELDS = ('type', 'loc', 'msg')
USER_FIELDS = {'email': ('user.email_invalid', {}), 'name': ('user.name_length', {'max': NAME_MAX}), 'role': ('user.role_invalid', {}),
               'password': ('user.password_length', {'min': PASSWORD_MIN, 'max': PASSWORD_MAX}), 'sso': ('access.user_fields', {}), 'disabled': ('access.user_fields', {}),
               'ssoLink': ('access.user_fields', {})}
SHARE_ID = re.compile(r'[A-Za-z0-9-]{1,64}')
OAUTH_ERROR = re.compile(r'[a-z_]{1,40}')
LOGIN_RESTARTS = {'cancelled': 'login.cancelled', 'state': 'login.mismatch', 'changed': 'login.restart'}
LOGIN_UNAVAILABLE = ('unreachable', 'hook')
USER_CACHE_SECONDS = 30
FAILURE_PAUSE_SECONDS = 5
DROPS_SHOWN = 5
ORIGIN_VIEW = {'builtin': 'builtin', 'entry-point': 'package', 'env': 'env'}
DUPLICATE_KEY_CODE = 11000
CHAT_PAGE = re.compile(r'/chat/(?:thread|share)/[A-Za-z0-9-]{1,64}/?')
WEB = Path(__file__).parent / 'web'
TEMPLATE = WEB / 'template.html'
CARD_PAGE = WEB / 'card.html'
CARD_FIELDS = ('num', 'key', 'svc', 'label', 'sub', 'sev', 'status', 'muted', 'triage', 'ticket', 'owner', 'assigned', 'firstSeen', 'lastSeen', 'clearedAt', 'what', 'note', 'business',
               'tags', 'url', 'gone', 'now', 'd1', 'd7', 'unit', 'recommend', 'suspects', 'outcome', 'resolution', 'triagedBy', 'autoResolvedAt', 'clearNote')
PLACEHOLDER = '/*__SKOPAX_DATA__*/'
PLUGIN_SLOT = '<script type="text/plain" id="plugin-scripts"></script>'
PEOPLE_MIN = 2
WARN_PARTS = ('browser down',)
PART_DOT = ' \u00b7 '
CARD_ACTION_TYPES = {'investigate': 'dive', 'resolve': 'resolve', 'take': 'take', 'known': 'known', 'mute': 'mute'}
CHAT_LIST = 50
CHAT_THREAD = 6
CHAT_CARDS = 3
PEOPLE_HITS = 8
SCRIPT_SRC = re.compile(r'<script src="/static/([\w.-]+)"></script>')
SMALL_TALK_CAPPED = 'Small talk limit for this hour is full ({cap} an hour); prod questions still work.'
SMALL_TALK_FIELDS = {'enabled': 'small-talk.enabled', 'hourlyCap': 'small-talk.hourly-cap', 'rules': 'small-talk.rules'}
DAY_S = 86400
SWEEP_PERIOD = 300
ENRICH_OFFSET = 60
SETTLE_OFFSET = 150
JIRA_LINKS_PERIOD = 900
JIRA_LINKS_OFFSET = 200
SLACK_FALLBACK_PERIOD = 120
SLACK_FALLBACK_OFFSET = 90
SOCKET_WAIT_S = 1
REMINDER_PERIOD = 900
REMINDER_OFFSET = 330
CLEAR_NOTE_PERIOD = 300
CLEAR_NOTE_OFFSET = 210
PROBE_PERIOD = 900
PROBE_OFFSET = 240
ASK_WINDOW_S = 3600
PAGE_ASK_SOURCES = ('page', 'api')
ACCESS_KIND = 'access'
ADMIN_KIND = 'admin'
TEST_TIMEOUT_S = 15
CHECK_MS_MAX_S = 1800
INVESTIGATOR_WORK = ('ask', 'dive', 'assess')

log = logging.getLogger('skopax')


class FeatureOff(HTTPException):
    def __init__(self, feature_id: str):
        super().__init__(status.HTTP_409_CONFLICT, off_text(feature_id))
        self.feature_id = feature_id


class NotReady(HTTPException):
    def __init__(self, text: str, needs: tuple[str, ...] = ()):
        super().__init__(status.HTTP_409_CONFLICT, text)
        self.needs = needs


class FieldRefused(Exception):
    def __init__(self, field: str, detail: str):
        super().__init__(detail)
        self.field = field
        self.detail = detail


def load_assets() -> dict[str, tuple[bytes, str]]:
    assets = {}
    for f in sorted((WEB / 'static').glob('*.js')):
        body = f.read_bytes()
        assets[f.name] = (body, hashlib.sha256(body).hexdigest()[:12])
    return assets


ASSETS = load_assets()


ICON_TYPES = {'.png': 'image/png', '.svg': 'image/svg+xml', '.ico': 'image/x-icon'}


def load_icons() -> dict[str, tuple[bytes, str]]:
    return {f.name: (f.read_bytes(), hashlib.sha256(f.read_bytes()).hexdigest()[:12]) for f in sorted((WEB / 'icons').iterdir()) if f.suffix in ICON_TYPES}


ICONS = load_icons()


def load_sso_icons() -> dict[str, bytes]:
    return {f.name: f.read_bytes() for f in sorted(ssobutton.ICON_DIR.iterdir()) if f.suffix == '.svg'}


SSO_ICONS = load_sso_icons()
SSO_ICON_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
SSO_ICON_CACHE = 'public, max-age=31536000, immutable'
APP_DARK = '#0e1420'
MANIFEST = {'id': '/', 'start_url': '/', 'scope': '/', 'display': 'standalone',
            'description': 'Production health board', 'theme_color': APP_DARK, 'background_color': APP_DARK,
            'icons': [{'src': '/icons/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
                      {'src': '/icons/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
                      {'src': '/icons/icon-maskable-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'}]}
BRAND_SLOT = re.compile(r'\[\[(brand|wordmark|mark|attribution_html|attribution_tail_html)\]\]')
BRAND_PLACEHOLDER = '/*__BRAND_DATA__*/'


def json_in_html(value) -> str:
    return json.dumps(value, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def manifest_json() -> str:
    name = brand.product()
    return json_in_html({'name': name, 'short_name': name} | MANIFEST)


def fill_slots(text: str) -> str:
    values = {'brand': html.escape(brand.product()), 'wordmark': brand.wordmark_html('dark'), 'mark': brand.mark_tile_html(), 'attribution_html': brand.footer_html(),
              'attribution_tail_html': brand.footer_html(element_id=None)}
    return BRAND_SLOT.sub(lambda m: values[m[1]], text)


def fill_brand(page: str) -> str:
    return json_in_html(brand.page_info()).join(fill_slots(part) for part in page.split(BRAND_PLACEHOLDER))


def plugin_script_tags(paths: list[str]) -> str:
    return ''.join(f'<script src="{html.escape(path, quote=True)}"></script>' for path in paths)


def render_page(state: dict, plugin_scripts: list[str] = ()) -> str:
    blob = json.dumps(state, ensure_ascii=False).replace('<', '\\u003c')
    page = SCRIPT_SRC.sub(lambda m: f'<script src="/static/{m[1]}?v={ASSETS[m[1]][1]}"></script>', TEMPLATE.read_text(encoding='utf-8'))
    page = page.replace(PLUGIN_SLOT, plugin_script_tags(list(plugin_scripts)))
    return blob.join(fill_brand(part) for part in page.split(PLACEHOLDER))


def render_card_page(plugin_scripts: list[str] = ()) -> str:
    page = SCRIPT_SRC.sub(lambda m: f'<script src="/static/{m[1]}?v={ASSETS[m[1]][1]}"></script>', CARD_PAGE.read_text(encoding='utf-8'))
    return fill_brand(page.replace(PLUGIN_SLOT, plugin_script_tags(list(plugin_scripts))))


MERGED_FIELDS = ('num', 'key', 'svc', 'label', 'sub', 'sev', 'status', 'url', 'what', 'business')


def card_view(card: dict, live: bool, merged: list[dict] = (), parent: dict | None = None) -> dict:
    out = {k: card[k] for k in CARD_FIELDS if card.get(k) not in (None, '', [])}
    cause = causes.cause_of(card, {parent['key']: parent} if parent else {})
    if cause:
        out['cause'] = cause
    shots = (card.get('slackPost') or {}).get('screenshots') or (card.get('slackDraft') or {}).get('screenshots') or card.get('screenshots') or []
    kids = [{k: c[k] for k in MERGED_FIELDS if c.get(k) not in (None, '', [])} for c in merged]
    return out | {'live': live, 'screenshots': list(shots)[:3]} | ({'merged': kids} if kids else {})


TICKET_HITS = 8
ROUTES_STOP_S = 5
MARKS_STOP_S = 10
TICKET_KEY = re.compile(r'[A-Za-z][A-Za-z0-9]+-\d+')
TICKET_WORD = re.compile(r'[^\w]+', re.UNICODE)


def last_post_channel(store: Store) -> str | None:
    try:
        posts = [p for p in (a.get('slackPost') or {} for a in store.load().get('anomalies') or []) if p.get('at') and p.get('channel')]
    except Exception as e:
        log.warning('last Slack channel not read: %s', kind(e))
        return None
    return max(posts, key=lambda p: str(p['at']))['channel'] if posts else None


def ticket_jql(q: str, epic: bool = False, project: str = '') -> str | None:
    text = str(q or '').strip()
    if TICKET_KEY.fullmatch(text):
        return f'key = "{text.upper()}"'
    words = [w for w in TICKET_WORD.split(text) if len(w) >= 2][:4]
    if not words:
        return None
    scope = (' AND issuetype = Epic' if epic else '') + (f' AND project = "{project}"' if project else '')
    return ' AND '.join(f'summary ~ "{w}*"' for w in words) + scope + ' AND statusCategory != Done ORDER BY updated DESC'


PAGE_STYLE = (
    '<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:15vh auto;padding:0 16px;line-height:1.5;color:#0e1420;background:#f2f4f7}'
    'a{color:#1d58d4}button{padding:9px 16px;font:inherit;color:#fff;background:#1d58d4;border:0;border-radius:6px;cursor:pointer}'
    '@media (prefers-color-scheme:dark){body{color:#e6eaf0;background:#0a0d12}a{color:#79a6ff}button{background:#3a6fe0}}</style>'
)


def page_head() -> str:
    name = html.escape(brand.product())
    return f'<title>{name}</title>{PAGE_STYLE}<h1 style="font-size:1.3rem">{name}</h1>'


def message_page(text: str, status_code: int, login: bool = True) -> HTMLResponse:
    links = ([f'<a href="/login">{html.escape(msg("page.login_again"))}</a>'] if login else []) + [f'<a href="/">{html.escape(msg("page.back"))}</a>']
    body = page_head() + f'<p>{html.escape(text)}</p><p>{" \u00b7 ".join(links)}</p>'
    return HTMLResponse(body, status_code=status_code)


def logout_page() -> HTMLResponse:
    body = page_head() + f'<p>{html.escape(msg("logout.title"))}</p><form method="post" action="/logout"><button type="submit">{html.escape(msg("logout.submit"))}</button></form>'
    return HTMLResponse(body, headers={'Cache-Control': 'no-store'})


def is_open(path: str) -> bool:
    return any(path.startswith(p[:-1]) if p.endswith('*') else path in (p, f'{p}/') for p in OPEN_PATHS)


def same_origin(request: Request) -> bool:
    origin = request.headers.get('origin')
    return not origin or urlsplit(origin).netloc == request.url.netloc


def render_login(error: str = '', email: str = '', next_page: str = '/', sso_link: bool = False, button: dict | None = None) -> str:
    target = safe_next(next_page)
    shown = button or ssobutton.button('', '', 'auto', None)
    values = {key: html.escape(msg(f'login.{key}')) for key in LOGIN_FIELDS}
    values |= {'error': f'<p class="error" role="alert">{html.escape(error)}</p>' if error else '',
               'wordmark': brand.wordmark_html(), 'emailvalue': html.escape(email, quote=True), 'nextvalue': html.escape(target, quote=True), 'footer': brand.footer_html(),
               'sso': f'<p class="sso-or"><span>{html.escape(msg("login.or"))}</span></p><p class="sso">{ssobutton.button_html(shown, "/login/sso?next=" + quote(target, safe=""))}</p>' if sso_link else ''}
    return LOGIN_SLOT.sub(lambda m: values.get(m.group(1), ''), LOGIN_PAGE.read_text(encoding='utf-8'))


def ip_keys(request: Request) -> tuple[str, ...]:
    return (f'ip:{request.client.host}',) if request.client and request.client.host else ()


def forwarded_host(entry: str) -> str:
    entry = entry.strip()
    if entry.startswith('['):
        return entry[1:].split(']', 1)[0].lower()
    return (entry.split(':', 1)[0] if entry.count(':') == 1 else entry).lower()


def forwarded_hosts(values: list[str]) -> set[str]:
    return {forwarded_host(part) for value in values for part in value.split(',')} - {''}


def refused_fields(e: RequestValidationError) -> list[dict]:
    return [{name: item[name] for name in ERROR_FIELDS if name in item} for item in e.errors()]


def refused_user(e: RequestValidationError) -> JSONResponse:
    first = next(iter(e.errors()), {})
    loc = tuple(first.get('loc') or ())
    field = loc[1] if len(loc) == 2 and loc[0] == 'body' and loc[1] in USER_FIELDS and first.get('type') != 'extra_forbidden' else None
    key, values = USER_FIELDS[field] if field else ('access.user_fields', {})
    if field == 'password' and first.get('type') != 'string_too_long':
        key, values = 'user.password_text', {}
    return JSONResponse({'detail': msg(key, **values)} | ({'field': field} if field else {}), status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


def user_field(clean: Callable[[str], str], field: str, value: str) -> str:
    try:
        return clean(value)
    except InvalidUser as e:
        raise FieldRefused(field, e.text) from None


async def refused_request(_: Request, e: RequestValidationError) -> Response:
    return Response(json.dumps({'detail': jsonable_encoder(refused_fields(e))}), status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, media_type='application/json')


def iso_time(text: str) -> dt.datetime:
    when = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    return when.astimezone(dt.timezone.utc) if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def stamp_time(text: str) -> dt.datetime | None:
    try:
        return iso_time(text)
    except (ValueError, OverflowError):
        return None


def wants_page(request: Request) -> bool:
    return request.method in READ_METHODS and 'text/html' in request.headers.get('accept', '')


async def read_form(request: Request, names: tuple[str, ...], limit: int = FORM_BODY_MAX) -> dict[str, str]:
    declared = request.headers.get('content-length', '')
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, msg('form.too_large'))
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, msg('form.too_large'))
        raw += chunk
    parsed = parse_qs(bytes(raw).decode('utf-8', 'replace'), keep_blank_values=True)
    return {name: (parsed.get(name) or [''])[0] for name in names}


def closed_bound(text: str, end: bool) -> dt.datetime:
    text = text.strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        day = dt.datetime.strptime(text, '%Y-%m-%d').replace(tzinfo=dt.timezone.utc)
        return day + dt.timedelta(days=1, seconds=-1) if end else day
    return iso_time(text)


def and_notify(result, notify):
    try:
        notify(result)
    except Exception as e:
        log.warning('notifications not saved: %s', kind(e))
    return result


def inbox_of(by: str) -> str:
    return slug(by) if '@' in by else ''


def public_base(settings: Settings) -> str:
    if settings.base_url:
        return settings.base_url.rstrip('/')
    host = urlsplit(settings.sso_redirect_uri).netloc if settings.sso_redirect_uri else ''
    return f'https://{host}' if host else ''


def create_app(settings: Settings | None = None, store: Store | None = None, slack: Slack | None = None,
               gitlab: CodeHost | None = None, jira: object | None = None,
               investigator: Investigator | None = None, anthropic_client=None, slack_socket=None,
               kube: Kube | None = None, operators_lookup: Callable[[str], bool] | None = None, registry: Registry | None = None,
               transport: Transport | None = None, sources: SourceSet | None = None, ai_factory: Callable[..., object] | None = None,
               clients: Mapping[str, object] | None = None, probe: Probe | None = None, sso_http: SsoHttp | None = None,
               clock: Callable[[], float] | None = None) -> FastAPI:
    clock = clock or time.time
    if settings is None:
        started = boot(os.environ, registry)
        settings = started.settings
        registry = started.registry
        sources = sources if sources is not None else started.source_set()
    sources = sources if sources is not None else SourceSet([], {})
    brand.use(brand.display_name(settings))
    try:
        clocks.set_zone(settings.timezone)
    except ValueError as e:
        raise ConfigurationError(text_of(e)) from e
    db = store.db if store is not None else MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=3000, tz_aware=True)[settings.mongo_db]
    live = Live(settings, TunableStore(db), extra=lambda: tuple(t for p in runtime.active() for t in plugin_tunables(p)), values=merged(sources.current()), labels=labelled(sources.current()))
    base_settings = settings
    settings = View(live)

    def watch_sso(current: Settings) -> None:
        if not current.sso_on:
            live.tunables.advance(SESSIONS_AFTER_KEY, clocks.stamp(clock()))

    live.watch = watch_sso
    sealer_box: list[Sealer] = []
    sealer_guard = threading.Lock()

    def get_sealer() -> Sealer:
        with sealer_guard:
            if not sealer_box:
                sealer_box.append(Sealer(master_key(db, base_settings), generated=not base_settings.secret_key))
            return sealer_box[0]

    registry = registry if registry is not None else Registry.load(os.environ, core_tools=CORE_TOOLS, core_knowledge=core_knowledge_ids(), core_settings=core_setting_ids())
    plugin_state = PluginState(db)
    instances = Instances(db, registry, get_sealer, sources=sources.current, extra_fields=lambda plugin_id: runtime.agent_fields(plugin_id))
    runtime = Runtime(registry, plugin_state, instances, transport, {**(clients or {}), **({'gitlab': gitlab} if gitlab is not None else {}), **({'jira': jira} if jira is not None else {})})
    runtime.live = live
    runtime.sources = sources
    probe = probe or Probe(runtime.probe_targets)
    features = Features(live, registry.edition, needs=lambda feature_id: needs_of(feature_id), started=lambda what: started_at_boot(what))
    brand.use_hidden(lambda: features.on('hide-attribution'))

    def chat_attribution() -> dict | None:
        return brand.attribution_line() if settings.chat_attribution else None

    follower = Follower(sources, live, instances, os.environ)
    redaction.use_rules(runtime.redaction_rules, runtime.redaction_timed_out)
    tags.use_rules(runtime.tag_rules)
    tags.use_tags(runtime.tag_defs)
    tags.use_texts(runtime.card_texts, runtime.hook_timeout)
    select.use_own_findings(registry.own_findings)
    outbound = [base_settings.language]
    writing: list[dict[str, str]] = [{}]
    language_state: dict = {'warned': None}
    messages.use_language(lambda: outbound[0])
    prompt.use_writing(lambda: writing[0])

    def plugin_projects() -> tuple[str, ...]:
        return runtime.org()['ticket_projects']

    def live_projects() -> tuple[str, ...]:
        return ticket_projects(settings, plugin_projects())

    def prom_for(name: str | None) -> Prometheus | None:
        found = runtime.instances_of(PROMETHEUS_ID)
        primary = runtime.instances.primary(PROMETHEUS_ID)
        chosen = next((i for i in found if i.name == name), None) if name else next((i for i in found if primary is not None and i.name == primary.name), None)
        loaded = registry.get(PROMETHEUS_ID)
        if chosen is None or loaded is None or loaded.plugin is None:
            return None
        ctx = runtime.context(loaded.plugin, chosen, time.time())
        return client_for(ctx.instance, ctx.http())

    def prom_watching() -> bool:
        return any(PROMETHEUS_CHECK not in i.off_checks for i in runtime.instances_of(PROMETHEUS_ID))

    kube = kube or Kube()
    runtime.kube = kube
    sso_http = sso_http or SsoHttp()

    def sso_source() -> tuple[str, str | None]:
        value = live.base.sso_client_secret or ''
        return (value, live.labels.get(SSO_SECRET_ID, 'environment')) if value else ('', None)

    sso_secret = SsoSecret(db, get_sealer, sso_source, lambda: stored_key(db), live.tunables)

    def sso_config() -> SsoConfig:
        secret = sso_secret.current()
        return SsoConfig.of(live.current(), secret, unreadable=not secret and sso_secret.status()['unreadable'])

    def fingerprint_key() -> bytes:
        return sessions_box[0].secret if sessions_box else b''

    signon = SignOn(sso_config, sso_http, token_request=runtime.token_request, fp_key=fingerprint_key, clock=clock)
    users = Users(db, now=clock)
    resolvers = Resolvers(runtime.role_resolvers_named)

    def note_bound(email: str) -> None:
        user_cache.pop(email, None)
        try:
            store.add_activity(ACCESS_KIND, email, msg('access.sso_bound', email=email))
        except PyMongoError as e:
            log.warning('the SSO link activity line was not saved: %s', kind(e))

    def note_not_added(email: str) -> None:
        now = clock()
        with not_added_guard:
            at = not_added_seen.get(email)
            if at is not None and 0 <= now - at < NOT_ADDED_S:
                return
            not_added_seen[email] = now
            not_added_seen.move_to_end(email)
            while len(not_added_seen) > NOT_ADDED_KEEP:
                not_added_seen.popitem(last=False)
        try:
            store.add_activity(ACCESS_KIND, email, msg('access.sso_not_added', email=email))
        except PyMongoError as e:
            log.warning('the refused SSO sign-in activity line was not saved: %s', kind(e))

    not_added_seen: OrderedDict[str, float] = OrderedDict()
    not_added_guard = threading.Lock()
    signin = SignIn(users, settings, resolvers, clock=clock, on_bound=note_bound)
    api_tokens = ApiTokens(db, clock=clock)
    sso_icon = SsoIcon(db)
    setup_gate = SetupGate(db, users)
    sessions_box: list[Sessions] = []
    user_cache: dict[str, tuple[float, dict | None]] = {}
    retry_after = {'key': 0.0, 'user': 0.0}
    probing = {'key': threading.Lock()}
    user_probes: dict[str, threading.Lock] = {}
    user_probes_guard = threading.Lock()
    failure_guard = threading.Lock()
    proxy_note = {'until': 0.0}
    proxy_pool: list[ThreadPoolExecutor] = []
    proxy_guard = threading.Lock()

    def get_sessions() -> Sessions | None:
        if sessions_box:
            return sessions_box[0]
        with probing['key']:
            if sessions_box:
                return sessions_box[0]
            if clock() < retry_after['key']:
                return None
            try:
                secret = session_secret(db, settings)
            except PyMongoError as e:
                retry_after['key'] = clock() + FAILURE_PAUSE_SECONDS
                log.warning('session key not available, the database is not reachable: %s', kind(e))
                return None
            sessions_box.append(Sessions(secret, settings.sso_session_idle_hours * 3600, settings.sso_session_max_days * 24 * 3600, clock=clock))
            chatui.use_secret(secret)
            return sessions_box[0]

    if settings.secret_key:
        get_sessions()
    link = SlackLink(runtime)
    slack = slack or Slack(link.token, lambda: settings.slack_channel, known_channel=lambda: last_post_channel(store))
    slack.attribution = chat_attribution
    gitlab = CodeHosts((gitlab if gitlab is not None else runtime.proxy('gitlab'), runtime.proxy('github')))
    jira = jira if jira is not None else runtime.proxy('jira')

    def jira_site() -> str:
        found = next((i for i in runtime.instances_of('jira') if i.primary), None)
        return str(found.values.get('url') or '').rstrip('/') if found is not None else ''

    if store is None:
        store = Store(db, history_days=settings.history_keep_days, investigate_days=settings.investigate_keep_days,
                      screenshot_days=settings.screenshot_keep_days)
    store.self_deploy = settings.self_deploy
    store.row_links = runtime.row_links
    store.release_ignore = settings.release_ignore
    base_url = public_base(settings)
    odd_stamp: list[str] = []

    def stop_time() -> float | None:
        entry = live.overrides.get(SESSIONS_AFTER_KEY)
        if entry is None:
            return None
        value = entry.get('value') if isinstance(entry, dict) else None
        at = clocks.parse_stamp(value)
        if at is None and odd_stamp != [repr(value)]:
            odd_stamp[:] = [repr(value)]
            log.warning('%s', msg('sso.stop_stamp_odd', key=SESSIONS_AFTER_KEY))
        return at

    mcp_auth = McpAuth(settings, signon, signin, base_url, clock=clock, sessions_after=stop_time)
    pusher = Pusher(slack, settings.slack_push_user, base_url, on=lambda: features.on('push'))
    routes = Routes(store, runtime, settings, attribution=chat_attribution, base=lambda: base_url, autostart=True)
    enricher = Enricher(store, gitlab, jira, live_projects)
    notes = StateNotes(store, slack, settings.slack_state_notes, jira_site=jira_site)
    card_posts = CardPosts(store, slack, base_url, menu_on=lambda: started_at_boot('slack-socket'))
    attacher = Attacher(store, slack, settings.slack_attach_screenshots, base_url)
    notices = Notices(store)
    versions = VersionCheck(store, transport, lambda: features.on('update-check'), clock=clock)
    reminders = Reminders(store, slack, settings.slack_remind_hours, base_url)
    clear_notes = ClearNotes(store, slack, settings.slack_clear_note_minutes, title_prefix=settings.jira_title_prefix)
    jira_links = JiraLinks(store, jira, settings.jira_links, notes, base_url, settings.jira_developer_field)
    assigner = Assigner(store, slack, settings, base_url, notes, notices, posts=card_posts)
    postdeploy = PostDeploy(store, prom_for, jira, lambda: features.on('postdeploy'), base_url, assess=lambda: investigator.available(), projects=settings.release_projects, serving=prom_watching,
                            primary_name=lambda: getattr(runtime.instances.primary(PROMETHEUS_ID), 'name', None), release_template=lambda: settings.jira_release_name, plugin_reports=lambda rows, started, now: runtime.deploy_reports(rows, started, now))
    settle = Settle(store, settings.release_settle)
    throttle = Throttle(db)

    def slack_operator(email: str) -> bool:
        return role_at_least(signin.slack_role(email), 'operator')

    operators = Operators(operators_lookup or slack_operator, slack, settings)

    def parent_of(card: dict) -> dict | None:
        if not card.get('parent') or card['parent'] == card.get('key'):
            return None
        try:
            return store.anomaly(card['parent'])
        except NotFound:
            return None

    def post_anomaly_to_slack(key: str, by: str, base: str = '') -> dict:
        anomaly = store.anomaly(key)
        link = card_url(base_url or base, key, anomaly.get('num'))
        draft = (anomaly.get('slackDraft') or {}).get('text') or None
        shots = (anomaly.get('slackDraft') or {}).get('screenshots') or []
        action = {'type': 'slack', 'key': key, 'target': f'slack:{key}', 'label': f"Send to Slack: {anomaly.get('svc')} {anomaly.get('label')}", 'requestedBy': by, 'draft': bool(draft)}
        thread = card_thread(anomaly)
        buttons = card_posts.new_menu(key, by) if started_at_boot('slack-socket') else None
        head, mute = opening(by) | {'at': iso(utc_now())}, card_posts.mute_of(key)
        message = anomaly_message(anomaly, link, by, draft, buttons['id'] if buttons else None, chat_url(base_url or base, key, anomaly.get('num')), head=head,
                                  attribution=None if thread else slack.signature(), mute=mute, kinds=menu_kinds(anomaly, mute), shots=shots, base=base_url,
                                  parent=parent_of(anomaly)) | ({'channel': thread['channel'], 'thread_ts': thread['ts']} if thread else {})
        try:
            posted = slack.post(message, sign=False)
        except SlackError as e:
            log.warning('slack post for %s failed: %s', key, kind(e))
            store.add_action(action, 'failed', f'not sent: {reason_of(e)}')
            raise
        sent = slack.linked(posted, thread['ts'] if thread else None)
        log.info('slack post for %s by %s, draft=%s, thread of %s', key, by, bool(draft), thread['key'] if thread else None)
        body = {'text': draft, **{k: v for k, v in (anomaly.get('slackDraft') or {}).items() if k in ('headline', 'by') and v}} if draft else None
        drawn = {'sig': drawn_sig(anomaly, mute), 'at': iso(utc_now())}
        post = {**sent, 'at': iso(utc_now()), 'by': by, 'draft': bool(draft), 'head': head, 'drawn': drawn, **({'body': body} if body else {}), **({'menu': buttons['id']} if buttons else {}),
                **({'screenshots': shots} if shots else {})}
        if thread:
            post |= {'ts': thread['ts'], 'replyTs': sent.get('ts'), 'threadOf': thread['key']}
        try:
            store.slack_posted(key, post)
        except (PyMongoError, NotFound, Conflict) as e:
            log.warning('slack post for %s went out but was not recorded on the anomaly: %s', key, kind(e))
        where = f"the thread of {thread['key']} in {slack.channel}" if thread else slack.channel
        pictures = attacher(posted['channel'], message.get('thread_ts') or posted['ts'], [s.get('id') for s in shots if isinstance(s, dict)], key) if shots else None
        done = store.add_action(action | {'slack': sent}, 'done', f"sent to {where}{' with the draft' if draft else ''}" + (f'; {pictures}' if pictures else ''))
        notices.posted(key, by)
        try:
            post_fixes(key, base)
        except Exception as e:
            log.warning('suggested fix of %s not posted in its thread: %s', key, kind(e))
        return done

    posting: set[str] = set()

    def send_anomaly_to_slack(key: str, by: str, base: str = '') -> dict:
        posting.add(key)
        try:
            return post_anomaly_to_slack(key, by, base)
        finally:
            posting.discard(key)

    def echo_ready(card: dict) -> bool:
        return bool(slack.enabled and features.on('slack-autosend') and not card.get('muted') and auto.live(card) and not auto_send_waiting(card) and card.get('key') not in posting)

    def echo_to_slack(key: str) -> dict | None:
        card = store.anomaly(key)
        post = card.get('slackPost') or {}
        if post.get('ts'):
            return post
        if not echo_ready(card):
            return None
        send_anomaly_to_slack(key, SERVER_BY)
        posted = store.anomaly(key).get('slackPost') or None
        if posted is None:
            raise RuntimeError('the Slack post was not recorded on the card')
        return posted

    def echo_in_thread(post: dict, text: str) -> None:
        slack.post({'text': text, 'channel': post['channel'], 'thread_ts': post['ts']}, sign=False)

    routes.echo = Echo(echo_to_slack, echo_in_thread, echo_ready)

    def post_fixes(key: str, base: str = '') -> int:
        if not slack.enabled:
            return 0
        try:
            card = store.anomaly(key)
        except NotFound:
            return 0
        post = card.get('slackPost') or {}
        if not post.get('ts') or not post.get('channel'):
            return 0
        claimed = [(c, h) for c in card.get('code') or [] if isinstance(c, dict) and c.get('suggestion') is not None
                   for h in (fix_hash(post['ts'], c),) if store.claim_fix(key, post['ts'], h)]
        if not claimed:
            return 0
        link = card_url(base_url or base, key, card.get('num'))
        try:
            slack.post(fix_message([c for c, _ in claimed], link) | {'channel': post['channel'], 'thread_ts': post['ts']})
        except SlackError as e:
            for _, h in claimed:
                store.unclaim_fix(key, h)
            log.warning('suggested fix of %s not posted in its thread: %s', key, kind(e))
            return 0
        store.add_activity('slack', 'server', f"suggested fix posted in the {slack.channel} thread · {', '.join(c.get('path') or '' for c, _ in claimed)}", key=key)
        return len(claimed)

    def card_thread(anomaly: dict) -> dict | None:
        if settings.slack_thread_hours <= 0 or (anomaly.get('slackPost') or {}).get('ts'):
            return None
        return store.related_post(anomaly, iso(utc_now() - dt.timedelta(hours=settings.slack_thread_hours)))

    def report_thread(report_id: str) -> dict | None:
        start = clocks.local(clocks.midnight(time.time()))
        return store.report_day_thread(iso(start.astimezone(dt.timezone.utc)), iso((start + dt.timedelta(days=1)).astimezone(dt.timezone.utc)), report_id)

    def send_report_to_slack(report_id: str, by: str, base: str = '') -> dict:
        report = store.deploy_report(report_id)
        link = f"{(base_url or base).rstrip('/')}/#r={quote(report_id, safe='')}"
        draft = (report.get('slackDraft') or {}).get('text') or None
        shots = (report.get('slackDraft') or {}).get('screenshots') or []
        action = {'type': 'slack', 'key': f'report:{report_id}', 'target': f'slack:report:{report_id}', 'label': f"Send to Slack: deploy report {cut(report.get('summary') or report_id, 80)}", 'requestedBy': by, 'draft': bool(draft)}
        thread = report_thread(report_id)
        message = report_message(report, link, by, draft, store.load().get('anomalies') or [], jira_site(), settings.jira_release_name)
        if thread:
            message |= {'channel': thread['channel'], 'thread_ts': thread['ts']}
        try:
            sent, pictures = attacher.post(message, shots, f'report:{report_id}')
        except SlackError as e:
            log.warning('slack post for report %s failed: %s', report_id, kind(e))
            store.add_action(action, 'failed', f'not sent: {reason_of(e)}')
            raise
        sent = slack.linked(sent, thread['ts'] if thread else None) | ({'threadTs': thread['ts']} if thread else {})
        log.info('slack post for report %s by %s, draft=%s, thread=%s', report_id, by, bool(draft), bool(thread))
        try:
            store.report_slack_posted(report_id, {**sent, 'at': iso(utc_now()), 'by': by, 'draft': bool(draft)})
        except (PyMongoError, NotFound) as e:
            log.warning('slack post for report %s went out but was not recorded on the report: %s', report_id, kind(e))
        where = f"today's deploy thread in {slack.channel}" if thread else slack.channel
        return store.add_action(action | {'slack': sent}, 'done', f"sent to {where}{' with the draft' if draft else ''}" + (f'; {pictures}' if pictures else ''))

    def slack_thread_update(key: str, text: str, by: str, shots: list[dict] | None = None, run_id: str | None = None, notice: str | None = None) -> dict:
        anomaly = store.anomaly(key)
        post = anomaly.get('slackPost') or {}
        if not post.get('channel') or not post.get('ts'):
            raise NoThread('this anomaly has no Slack message from this page to reply to')
        action = {'type': 'slack-update', 'key': key, 'target': f'slack-update:{key}', 'label': f"Slack thread update: {anomaly.get('svc')} {anomaly.get('label')}", 'requestedBy': by}
        try:
            sent, pictures = attacher.post({'channel': post['channel'], 'thread_ts': post['ts'], 'text': esc_keep_mentions(text) + recurring_line(anomaly)}, shots or [], key, run_id)
        except SlackError as e:
            log.warning('slack thread update for %s failed: %s', key, kind(e))
            store.add_action(action, 'failed', f'not sent: {reason_of(e)}')
            raise
        sent = slack.linked(sent, post['ts'])
        store.slack_thread_updated(key, post['ts'], epoch_iso(time.time()))
        log.info('slack thread update for %s by %s', key, by)
        done = store.add_action(action | {'slack': sent}, 'done', 'replied in the Slack thread' + (f'; {pictures}' if pictures else ''))
        notices.posted(key, by, thread=True, text=text if notice is None else notice)
        return done

    def task_notice(key: str, new_key: str, by: str) -> list[str]:
        card = store.anomaly(key)
        name = f"{card.get('svc')} {card.get('label')}"
        jira_url = f'{jira_site()}/browse/{quote(new_key, safe="")}'
        card_link = card_url(base_url, key, card.get('num'))
        told = []
        post = card.get('slackPost') or {}
        if post.get('channel') and post.get('ts') and slack.token:
            try:
                sent = slack.post({'channel': post['channel'], 'thread_ts': post['ts'], 'text': msg('slack.task.opened', url=jira_url, key=esc(new_key), who=esc(who_of(by)))})
                store.add_action({'type': 'slack-update', 'key': key, 'target': f'slack-update:{key}', 'label': f'Slack thread update: {new_key} created for {name}', 'requestedBy': by,
                                  'slack': slack.linked(sent, post['ts'])}, 'done', 'task notice in the Slack thread')
                told.append('thread')
            except SlackError as e:
                log.warning('task notice in the thread of %s failed: %s', key, kind(e))
        if pusher.enabled:
            dm = msg('push.task', key=esc(new_key), name=esc(name), by=esc(by), url=jira_url, card=card_link)
            if pusher.text(dm):
                told.append('DM')
        return told

    def slack_tell(key: str | None, email: str, text: str, by: str, name: str = '') -> dict:
        user = slack.user_by_email(email)
        if not user:
            raise Refusal(f'no Slack user has the email {email}')
        if not same_person(name, user['name']):
            raise Refusal(f"{email} belongs to {user['name'] or 'a Slack user without a name'} in Slack, not {name}; ask the asker who they meant")
        line = msg('slack.tell', id=user['id'], text=esc(text), who=esc(who_of(by)))
        root = None
        if key:
            if not (store.anomaly(key).get('slackPost') or {}).get('ts'):
                send_anomaly_to_slack(key, by)
            post = store.anomaly(key).get('slackPost') or {}
            root = post.get('ts')
            sent = slack.post({'channel': post.get('channel'), 'thread_ts': root, 'text': line})
        else:
            sent = slack.post({'text': line})
        action = {'type': 'slack-tell', 'key': key or 'ask', 'target': f"slack-tell:{key or 'channel'}:{email}", 'label': f'Slack: {who_of(by)} asked to tell {who_of(email)}'[:200],
                  'requestedBy': by, 'slack': slack.linked(sent, root)}
        log.info('slack tell to %s by %s on %s', email, by, key or 'the channel')
        return store.add_action(action, 'done', f"told {user['name'] or who_of(email)} in {'the card thread' if key else f'a new {slack.channel} thread'}")

    def pod_slack_send(key: str, by: str) -> dict:
        if (store.anomaly(key).get('slackPost') or {}).get('ts'):
            raise ValueError('this anomaly already has a Slack message from this page')
        return send_anomaly_to_slack(key, by)

    replier = Replier(store, slack, settings, attach=attacher, base_url=base_url)

    def state_note(key: str, before: dict) -> None:
        notes.triaged(key, before)
        notices.triaged(key, before)
        card_posts.triaged(key, before)

    aikey = AiKey(db, get_sealer, lambda: live.base.anthropic_api_key, lambda: stored_key(db), live.tunables, factory=ai_factory)

    def org_pack() -> OrgPack | None:
        found = runtime.org_pack()
        return None if found is None else found[1]

    mcptools_catalog = McpCatalog(db, note=lambda by, text: store.add_activity(ADMIN_KIND, by, text), public=lambda: bool(settings.public_read))
    mcptools_auth = McpOAuth(db, get_sealer, lambda: base_url, mcptools_catalog, note=lambda by, text: store.add_activity(ADMIN_KIND, by, text),
                             http=lambda ca: mcptools_deps.http(ca), local=lambda: mcptools_routes.local_http(mcptools_deps))
    mcptools_deps = mcptools_routes.Deps(mcptools_catalog, mcptools_auth, instances, lambda: getattr(registry.get(MCPTOOLS_ID), 'plugin', None),
                                         lambda key: runtime.setting(MCPTOOLS_ID, key), FieldRefused, oauth=mcptools_auth)
    mcptools_bridge = ToolBridge(mcptools_deps, runtime, features, store)

    talk = SmallTalk(store, settings, client=anthropic_client, key=aikey.current, pack=org_pack)
    investigator = investigator or Investigator(
        store, Sources(gitlab=gitlab, jira=jira, plugin_tools=PluginTools(runtime), selfreview=lambda now: review_facts(store, server_info() | store.server_runs(), runtime.client('elasticsearch') if settings.self_log_index else None, now, own_commits, self_service(settings), settings.self_log_index, PluginTools(runtime)),
                              link_hosts=lambda: allowed_link_hosts(settings, runtime.org()['link_hosts'] + runtime.ui_hosts()), org=runtime.org,
                              bridge=mcptools_bridge),
        Effects(pod_slack_send, slack_thread_update, replier.reply, state_note, tell=slack_tell, fixes=lambda key: post_fixes(key), slack_file=slack.file), settings, client=anthropic_client, base_url=base_url, talk=talk, key=aikey.current)

    if isinstance(investigator, Investigator):
        investigator.notices = notices
        investigator.on = lambda: features.on('investigator')
        investigator.pack = org_pack
        investigator.role_of = lambda action: asker_role(action, users)

    gate = Gate(store, settings, client=anthropic_client, key=aikey.current, pack=org_pack)

    intake = Intake(store, slack, operators, gate, talk, investigator, settings, base_url=base_url, ticket_extra=plugin_projects, on=lambda: features.on('slack-listener') and slack.enabled,
                    chat=lambda: features.on('chat'), not_ready=lambda: not_ready_text('slackevents.not_ready', 'slackevents.needs'), jira_site=jira_site)

    def apply_card_action(action: dict) -> str:
        before = notes.watch(action)
        seen = notices.watch(action)
        result = page_actions.apply(store, action, jira, settings, notify=task_notice, extra_projects=plugin_projects())
        notes.applied(action, before)
        notices.applied(action, seen)
        card_posts.after(action)
        return result

    def apply_from_slack(action: dict) -> str:
        if action.get('type') == 'ticket' and drafts.is_tech(action) and settings.jira_tech_project:
            action = action | {'project': settings.jira_tech_project}
        try:
            return apply_card_action(action)
        except (NotFound, page_actions.Refused) as e:
            raise TicketRefused(e.text) from None

    proposals = Proposals(store, runtime)
    clicks = Clicks(store, slack, operators, apply_from_slack, jira_site, on=lambda: features.on('slack-listener') and slack.enabled, confirm=proposals.confirm)
    listen_mode = settings.slack_listen
    listener: Listener | None = None

    def socket_listener() -> Listener | None:
        app_token = link.app_token()
        return Listener(app_token, intake, clicks, slack_socket or socket_client) if listen_mode != 'off' and bool(app_token) and slack.enabled and operators.enabled else None

    socket_state = {'started': False}

    def open_socket() -> None:
        nonlocal listener
        listener = app.state.listener = socket_listener()
        if listener is not None and investigator.enabled:
            listener.start()
            socket_state['started'] = True
            log.info('slack listener on, %s mode', listen_mode)
        elif listen_mode != 'off':
            log.warning('slack.listen is %s but the Slack listener stays off: it needs the Slack app and bot tokens (Admin \u203a Integrations \u203a Slack) and an enabled investigator', listen_mode)

    async def open_socket_once_loaded() -> None:
        while instances.age() is None:
            await asyncio.sleep(SOCKET_WAIT_S)
        await asyncio.to_thread(open_socket)

    def page_topic_check(question: str, keys: list[str], by: str, thread: list[dict] | tuple = (), source: str = 'page') -> Reply | None:
        cards = []
        for k in keys:
            try:
                cards.append(store.anomaly(k))
            except NotFound:
                continue
        sent = prompt.gate_input(prompt.page_gate_context(cards), thread, question)
        try:
            verdict = gate.check(sent, source, by, [c['key'] for c in cards])
        except GateDown as e:
            log.info('page ask let through, the gate is down: %s', e.code)
            return None
        if verdict.on_topic:
            return None
        now = time.time()
        halted = investigator.why_halted(now)
        if verdict.small_talk and halted == 'off':
            raise FeatureOff('investigator')
        if verdict.small_talk and halted is None:
            skipped = talk.blocked(now)
            if skipped == 'cap':
                raise HTTPException(status_code=422, detail=SMALL_TALK_CAPPED.format(cap=talk.rules()['hourlyCap']))
            said = talk.reply(sent, source, by, [c['key'] for c in cards]) if skipped is None else None
            if said is not None:
                return said
        raise HTTPException(status_code=422, detail=msg('page.off_topic', about=prompt.about(settings.org_about), reason=verdict.reason))

    auto = AutoSend(store, lambda key: send_anomaly_to_slack(key, AUTO_BY), lambda report_id: send_report_to_slack(report_id, AUTO_BY), on=lambda: features.on('slack-autosend') and slack.enabled)
    fallback = Fallback(store, settings.slack_fallback_minutes)
    crit_now = CritNow(store, lambda key: send_anomaly_to_slack(key, SERVER_BY), lambda: bool(settings.slack_crit_now and slack.enabled) and features.on('slack-autosend'))

    def server_apply(results: list[PassResult], now: float, links, after=None, check: str | None = None, screen=None, clusters=None) -> Applied | None:
        try:
            took = time.time() - now
            ms = int(took * 1000) if 0 <= took <= CHECK_MS_MAX_S else None
            store.add_check_runs([{'name': r.name, 'check': check or r.name, 'ok': r.ok, 'ms': ms, 'error': None if r.ok else r.status} for r in results])
        except PyMongoError as e:
            log.warning('check runs not recorded: %s', kind(e))
        applied = apply(store, results, now, links, dm=pusher.enabled, after=after, auto_resolve_hours=settings.auto_resolve_hours,
                        recurring_count=settings.recurring_count, recurring_days=settings.recurring_days, tag_rules=tags.rules(settings.tag_rules), alert_twins=settings.alert_twins, screen=screen, clusters=clusters, kind_windows=runtime.auto_windows())
        if applied and applied.resolved:
            for r in applied.resolved:
                notes.auto_resolved([r['key']], r.get('hours') or settings.auto_resolve_hours, r.get('since'), {r['key']} if r.get('event') else ())
            notices.auto_resolved([r['key'] for r in applied.resolved])
        if applied:
            notices.fired(applied.events, applied.cards)
        if applied and applied.push:
            pusher.send(applied.push)
            crit_now.run(applied.push, applied.cards, now)
            if investigator.enabled and any(a.get('sev') == 'crit' for a in applied.push):
                investigator.kick(time.time())
        if applied:
            routes.later(applied.cards, time.time(), applied.push)
        card_posts.swept(applied)
        return applied

    def enrich_posted(keys: list[tuple[str, str, str]]) -> None:
        try:
            enricher.run_keys(keys)
        except Exception as e:
            log.warning('deploy enrichment after a post failed: %s', kind(e))

    def own_commits(since: str) -> list[dict]:
        if not settings.self_project:
            return []
        if not gitlab.enabled:
            raise RuntimeError('GitLab is not set up')
        own = settings.self_project.strip('/').lower()
        found = gitlab.repo(settings.self_project)
        found = found if found and own in (str(found.get('path') or '').lower(), str(found.get('full') or '').lower()) else None
        return gitlab.commits_since(found or settings.self_project, str((found or {}).get('branch') or 'master'), since)

    def save_review(text: str, by: str, emails: list[str], members: list[str] | None = None) -> dict:
        text = redact(text)
        at = utc_now()
        link = f"{base_url.rstrip('/')}/#t=review"
        message = msg('review.dm', when=f"{clocks.local(at).strftime('%d %b %H:%M')} {clocks.label(at)}", text=esc(text), link=link)
        store.add_self_review({'at': iso(at), 'by': by, 'text': text, 'dm': []})
        told, failed = [], []
        for email in emails:
            try:
                user = slack.user_by_email(email)
                if not user:
                    failed.append(email)
                    continue
                slack.post({'channel': user['id'], 'text': message})
                told.append(email)
            except SlackError as e:
                log.warning('self-review DM to %s failed: %s', email, kind(e))
                failed.append(email)
        for member in members or []:
            try:
                slack.post({'channel': member, 'text': message})
                told.append(member)
            except SlackError as e:
                log.warning('self-review DM to %s failed: %s', member, kind(e))
                failed.append(member)
        review = {'at': iso(at), 'by': by, 'text': text, 'dm': told}
        if told:
            store.set_self_review_dm(review['at'], told)
        store.add_activity('review', by if is_token(by) else 'server', f"Self-review saved{' · DM ' + ', '.join(told) if told else ''}"[:300])
        return {'review': review, 'failed': failed}

    def review_targets() -> tuple[list[str], list[str]]:
        emails = [e.strip() for e in settings.selfreview_dm.split(',') if e.strip()]
        return (emails, []) if emails else ([], push_users(settings.slack_push_user) if pusher.enabled else [])

    self_review = SelfReview(store, settings, lambda now: review_facts(store, server_info() | store.server_runs(), runtime.client('elasticsearch') if settings.self_log_index else None, now, own_commits, self_service(settings), settings.self_log_index, PluginTools(runtime)),
                             lambda text: save_review(text, SELF_REVIEW_BY, *review_targets()) if slack.enabled else save_review(text, SELF_REVIEW_BY, []),
                             client=anthropic_client, key=aikey.current)

    def check_slack_scopes() -> None:
        try:
            missing = slack.missing_scopes(listen_mode != 'off' and bool(link.app_token()), settings.slack_dm_chats) if slack.enabled else []
        except SlackError as e:
            log.warning('the Slack scopes could not be checked: %s', kind(e))
            return
        try:
            before = store.slack_scopes()['missing']
            if missing and missing != before:
                log.warning('the Slack app lacks the scopes %s', ', '.join(missing))
            store.note_slack_scopes(missing, iso(utc_now()))
        except PyMongoError as e:
            log.warning('the Slack scope check was not stored: %s', kind(e))

    def slack_posts_run(now: float) -> None:
        card_posts.reconcile()
        check_slack_scopes()
        results = slack_posts_pass(store.load().get('anomalies') or [], now)
        if results[0].found or store.open_cards_of(SLACK_POSTS):
            server_apply(results, now, lambda a: None)

    jira_card: dict = {'open': None}

    def jira_links_on() -> bool:
        if jira_links.enabled:
            return True
        if jira_card['open'] is None:
            try:
                jira_card['open'] = bool(store.open_cards_of(JIRA_ACCESS))
            except PyMongoError as e:
                log.warning('the jira-links access card could not be read: %s', kind(e))
                return False
        return bool(jira_card['open'])

    def jira_links_run(now: float) -> list[dict]:
        if not jira_links.enabled:
            server_apply(jira_access_pass({}, runtime.plugin_name('jira')), now, lambda a: None)
            jira_card['open'] = False
            return []
        try:
            linked = and_notify(and_notify(jira_links.run_once(now), card_posts.jira), notices.jira_swept)
        except SourceDown as e:
            log.warning('jira-links run failed: %s', kind(e))
            server_apply([PassResult(JIRA_ACCESS, False, msg('jira.links_failed', part='issues', reason=text_of(e)), [], {}, ())], now, lambda a: None)
            return []
        results = jira_access_pass(jira_links.refused, runtime.plugin_name('jira'))
        if results[0].found or store.open_cards_of(JIRA_ACCESS):
            server_apply(results, now, lambda a: None)
        jira_card['open'] = bool(results[0].found)
        return linked

    def enrich_keys(keys: list[tuple[str, str, str]]) -> None:
        if enricher.enabled:
            enrich_posted(keys)

    def probe_run(now: float) -> dict:
        report = probe.run(now)
        store.save_probe(report)
        line = probe_summary(report)
        if line:
            log.info('%s', msg('probe.ran', summary=line))
        server_apply(cert_pass(report, now, monitor_rules(settings.cert_rules, CERT_RULES), settings.cert_skip), now, lambda a: None)
        return report

    def probe_at_start() -> None:
        if not settings.probe:
            return
        try:
            probe_run(time.time())
        except Exception as e:
            log.warning('%s', msg('probe.start_failed', error=kind(e)))

    sweeper = Sweeper(runtime, store, server_apply, enrich=enrich_keys, outputs=Outputs(store, slack, attacher, base_url, lambda: settings.screenshot_max_per_day, auto=lambda: features.on('slack-autosend')))
    custom_checks = CustomChecks(store, runtime, server_apply)

    def refresh_languages() -> None:
        locales = [locale for _, locale in runtime.locales()]
        messages.set_catalogs([(locale.lang, locale.messages) for locale in locales])
        set_chat_texts([(locale.lang, locale.chat) for locale in locales], [(locale.lang, locale.chainlit) for locale in locales])
        chatui.set_translations([(locale.lang, locale.chainlit) for locale in locales])
        writing[0] = {locale.lang: locale.writing for locale in locales if locale.writing}
        outbound_language()

    def outbound_language() -> None:
        lang = settings.language
        if not messages.LANG_TAG.fullmatch(lang) and language_state['warned'] != lang:
            language_state['warned'] = lang
            log.warning('%s', msg('settings.language_invalid', value=messages.printable(lang)))
        outbound[0] = next((known for known in messages.languages() if known.lower() == lang.lower()), lang)

    runtime.languages = refresh_languages
    runtime.after_refresh = lambda: sweeper.heal()
    instances.on_primary_moved = lambda plugin_id, old, new: sweeper.primary_changed(plugin_id, old, new, time.time())
    runtime.reader = lambda plugin: Reads(store, plugin, None)
    runtime.ai = PluginAi(store, aikey.current, lambda now: investigator.why_halted(now), client=anthropic_client).for_check
    runtime.memos = store.plugin_memo
    core_jobs = [
        Job('enrich', SWEEP_PERIOD, ENRICH_OFFSET, lambda now: enricher.sweep(), lambda: enricher.enabled),
        Job('settle', SWEEP_PERIOD, SETTLE_OFFSET, lambda now: settle.run_once(), settle.enabled),
        Job(JIRA_ACCESS, JIRA_LINKS_PERIOD, JIRA_LINKS_OFFSET, jira_links_run, jira_links_on),
        Job(SLACK_POSTS, SLACK_POSTS_PERIOD, SLACK_POSTS_OFFSET, slack_posts_run, lambda: slack.enabled),
        Job('investigate', INVESTIGATE_PERIOD, INVESTIGATE_OFFSET, investigator.kick, lambda: investigator.enabled),
        Job('slack-fallback', SLACK_FALLBACK_PERIOD, SLACK_FALLBACK_OFFSET, fallback.run_once, lambda: slack.enabled and fallback.enabled),
        Job('slack-reminders', REMINDER_PERIOD, REMINDER_OFFSET, lambda now: and_notify(reminders.run_once(now), notices.reminded), lambda: reminders.enabled),
        Job('slack-clear-notes', CLEAR_NOTE_PERIOD, CLEAR_NOTE_OFFSET, clear_notes.run_once, lambda: clear_notes.enabled),
        Job('self-review', DAY_S, lambda now, hour=max(settings.selfreview_hour, 0): review_slot_offset(hour, now), self_review.scheduled, lambda: self_review.configured, background=True),
        Job('probe', PROBE_PERIOD, PROBE_OFFSET, probe_run, enabled=lambda: settings.probe),
        Job('version-check', VERSION_PERIOD, VERSION_OFFSET + jitter_of(base_settings.mongo_uri + base_settings.mongo_db), versions.scheduled, lambda: features.on('update-check')),
        Job(CUSTOM_CHECKS, CUSTOM_CHECKS_PERIOD, CUSTOM_CHECKS_OFFSET, custom_checks.run_once, custom_checks.enabled),
    ]
    scheduler = Scheduler(store, boot=investigator.owner, jobs=lambda: core_jobs + [Job('postdeploy', POSTDEPLOY_PERIOD, POSTDEPLOY_OFFSET, lambda now: postdeploy.run_once(now), postdeploy.enabled)] + sweeper.jobs())

    def passed(status: str) -> bool:
        return status == 'ok' or status.startswith('ok ')

    def refused(status: str) -> bool:
        return passed(status) and ' no access to ' in status

    def warned(status: str) -> str | None:
        return next((part for part in status.split(PART_DOT)[1:] if part in WARN_PARTS), None) if passed(status) else None

    def instance_status(checks, inst, passes: dict) -> str | None:
        seen = [(passes.get(pass_name(check, inst)) or {}).get('status') for check in checks]
        seen = [x for x in seen if x]
        bad = [x for x in seen if not passed(x)] or [x for x in seen if refused(x)] or [x for x in seen if warned(x)]
        return bad[0] if bad else seen[0] if seen else None

    def plugin_tiles(passes: dict) -> list[dict]:
        tiles = []
        queued = scheduler.queued()
        for plugin in runtime.active():
            if plugin.id == MCPTOOLS_ID or not (plugin.fields or plugin.checks or plugin.panels):
                continue
            found = instances.all(plugin.id)
            if not found and plugin.fields:
                continue
            names = [pass_name(check, i.info() if i is not None else None) for check in plugin.checks for i in (found or [None]) if check in watched(plugin, i)]
            infos = [i.info() for i in found] if found else [None]
            instances_out = []
            for i, inf in zip(found or [None], infos):
                checks = watched(plugin, i)
                first = checks[0] if checks else None
                if i is None and first is None:
                    continue
                health = instance_health(plugin, i, passes, True) if i is not None else pass_health(checks, None, passes)
                instances_out.append({'name': i.name if i is not None else None, 'primary': i.primary if i is not None else True,
                                      'pass': pass_name(first, inf) if first else None, 'status': instance_status(checks, inf, passes),
                                      'state': health['state'], 'text': health['text']})
            serves = not plugin.fields or any(i.enabled and not i.problem for i in found)
            tab = next((p.tab for p in plugin.panels if p.tab), None) if serves else None
            icon = f'/plugins/{plugin.id}/icon.svg'
            pages = [{'tab': p.tab, 'label': p.label.strip(), 'group': p.group, 'icon': icon} for p in plugin.panels if p.label and serves]
            tiles.append({'id': plugin.id, 'name': plugin.name, 'icon': icon, 'tab': tab, 'passes': list(dict.fromkeys(names)), 'instances': instances_out, 'pages': pages,
                          'queued': any(f'{plugin.id}.{check.id}' in queued for check in plugin.checks), 'types': card_types_view(plugin)})
        return tiles

    def pass_health(checks, info, passes: dict) -> dict:
        if not checks:
            return {'state': 'none', 'text': msg('integrations.health.no_checks'), 'at': None}
        status = instance_status(checks, info, passes)
        if status is None:
            return {'state': 'pending', 'text': msg('integrations.health.pending'), 'at': None}
        runs = [passes.get(pass_name(check, info)) or {} for check in checks]
        times = [r['at'] for r in runs if isinstance(r.get('at'), str) and (passed(status) or r.get('status') == status)]
        if refused(status):
            return {'state': 'warn', 'text': msg('integrations.health.no_access'), 'at': max(times, default=None)}
        if part := warned(status):
            return {'state': 'warn', 'text': part, 'at': max(times, default=None)}
        if passed(status):
            return {'state': 'ok', 'text': msg('integrations.health.ok'), 'at': max(times, default=None)}
        return {'state': 'error', 'text': status, 'at': times[0] if times else None}

    def jira_links_health(passes: dict) -> dict | None:
        if not jira_links.enabled:
            return None
        run = passes.get(JIRA_ACCESS) or {}
        status = run.get('status')
        if not isinstance(status, str) or (passed(status) and not refused(status)):
            return None
        at = run.get('at') if isinstance(run.get('at'), str) else None
        if refused(status):
            return {'state': 'warn', 'text': msg('integrations.health.no_access'), 'at': at}
        return {'state': 'error', 'text': status, 'at': at}

    def instance_health(plugin, inst, passes: dict, on: bool) -> dict:
        checks = watched(plugin, inst)
        if not on or not inst.enabled:
            found = {'state': 'off', 'text': msg('integrations.health.off'), 'at': None}
        elif inst.problem:
            found = {'state': 'error', 'text': inst.problem, 'at': None}
        elif route_problem := runtime.route_problem(plugin, inst):
            found = {'state': 'error', 'text': route_problem, 'at': None}
        elif plugin.outbound is not None and (failed := routes.failure(plugin.id, inst.name)):
            found = {'state': 'error', 'text': failed, 'at': None}
        elif plugin.id == 'jira' and inst.primary and (linked := jira_links_health(passes)):
            found = linked
        elif plugin.id == MCPTOOLS_ID:
            found = {**mcptools_routes.health(mcptools_deps, inst), 'at': None}
        elif not checks and any(owned(c, inst) for c in plugin.checks):
            found = {'state': 'off', 'text': msg('integrations.health.unchecked'), 'at': None}
        else:
            found = pass_health(checks, inst.info(), passes)
        return {'name': inst.name, **found}

    def plugin_health(plugin, found, passes: dict) -> dict:
        on = any(p.id == plugin.id for p in runtime.active())
        rows = [instance_health(plugin, i, passes, on) for i in found]
        live = [r for r in rows if r['state'] != 'off']
        bad = [r for r in live if r['state'] == 'error'] or [r for r in live if r['state'] == 'warn']
        fine = [r for r in live if r['state'] == 'ok']
        waiting = [r for r in live if r['state'] == 'pending']
        if not found and plugin.fields:
            top = {'state': 'off', 'text': msg('integrations.health.unset'), 'at': None}
        elif not on:
            top = {'state': 'off', 'text': msg('integrations.health.off'), 'at': None}
        elif not found:
            top = pass_health(plugin.checks, None, passes)
        elif not live:
            top = {'state': 'off', 'text': rows[0]['text'], 'at': None}
        elif bad:
            top = bad[0]
        elif fine:
            top = max(fine, key=lambda r: r['at'] or '')
        else:
            top = {'state': (waiting or live)[0]['state'], 'text': (waiting or live)[0]['text'], 'at': None}
        return {'state': top['state'], 'text': top['text'], 'at': top['at'], 'instances': rows}

    def server_info() -> dict:
        return {'enrich': {'gitlab': gitlab.enabled, 'jira': jira.enabled}, 'settle': settle.enabled, 'jiraLinks': jira_links.enabled, 'postdeploy': postdeploy.enabled, 'push': pusher.enabled,
                'kube': kube.enabled, 'slack': {'listen': settings.slack_listen, 'socket': started_at_boot('slack-socket'), **({'link': listener.status()} if started_at_boot('slack-socket') else {})},
                'investigate': investigator.info(), 'probe': settings.probe}

    def slack_links_backfill() -> None:
        try:
            if not slack.enabled or not store.unlinked_slack_posts():
                return
            workspace = slack.workspace()
            if not workspace:
                log.info('old Slack posts stay without a link: auth.test gave no workspace url')
                return
            filled = store.fill_slack_links(lambda channel, ts: built_link(workspace, channel, ts))
        except Exception as e:
            log.warning('slack link backfill skipped: %s', kind(e))
            return
        log.info('slack links added to %d old posts', filled)

    def boot_backfills() -> None:
        backfill.run(store)
        card_history_backfill()
        slack_links_backfill()

    def card_history_backfill() -> None:
        try:
            written = history.backfill(store, time.time(), {i.name for i in instances.all()})
        except Exception as e:
            log.warning('card history backfill skipped: %s', kind(e))
            return
        if written:
            log.info('card history backfilled %d occurrences', written)

    def announce_code(code: str) -> None:
        log.warning('%s', msg('setup.announce', url=base_url, code=code))

    env_state = {'warned': False, 'conflict': False}

    def env_admin() -> bool:
        if not (settings.admin_email and settings.admin_password) or env_state['conflict']:
            return False
        try:
            users.create(settings.admin_email, msg('setup.admin_name'), 'admin', settings.admin_password)
            log.info('admin created from env: %s', settings.admin_email.strip().lower())
        except UserExists:
            if setup_gate.needed():
                env_state['conflict'] = True
                log.warning('SKOPAX_ADMIN_EMAIL %s already belongs to an account that is not an active admin, so no admin was created from the environment. '
                            'Finish setup at /setup, or make that account an active admin.', settings.admin_email.strip().lower())
                return False
        except InvalidUser as e:
            if not env_state['warned']:
                env_state['warned'] = True
                log.warning('SKOPAX_ADMIN_EMAIL and SKOPAX_ADMIN_PASSWORD were not used: %s', kind(e))
            return False
        return True

    def setup_needed() -> bool:
        if not setup_gate.needed():
            return False
        if env_admin():
            return setup_gate.needed()
        return True

    def first_run() -> None:
        if not setup_needed():
            return
        code = setup_gate.issue_code()
        if code:
            announce_code(code)
        else:
            log.info(msg('setup.pending'))

    def token_indexes() -> None:
        try:
            api_tokens.ensure_indexes()
        except OperationFailure as e:
            if e.code != DUPLICATE_KEY_CODE and not isinstance(e, DuplicateKeyError):
                raise
            log.warning('API token names are not unique, so the unique name index was not built: %s. Rename or delete the duplicates in the api_tokens collection.', kind(e))

    def investigator_shutdown() -> None:
        investigator.shutdown()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        boot_id = secrets.token_hex(8)
        writer = Writer(instances)
        runtime.migrate = lambda: migrations.attempt(db, registry, boot_id, writer=writer)
        checked = False
        try:
            store.ensure_indexes()
            users.ensure_indexes()
            token_indexes()
            throttle.ensure_indexes()
            live.tunables.ensure_indexes()
            get_sessions()
            first_run()
            await asyncio.to_thread(instances.ensure_indexes)
            await asyncio.to_thread(mcptools_auth.ensure_indexes)
            try:
                caught_up = await asyncio.to_thread(migrations.wait_and_run, db, registry, boot_id, writer=writer)
            except PyMongoError as e:
                log.warning('%s', msg('migrations.lease_error', error=kind(e)))
                caught_up = None
            checked = True
            if caught_up is not None:
                runtime.migrate = None
            await asyncio.to_thread(runtime.refresh)
            await asyncio.to_thread(aikey.refresh)
            await asyncio.to_thread(sso_secret.refresh)
        except PyMongoError as e:
            if not checked:
                migrations.stop_if_required(registry, e)
            log.warning('index setup skipped, Mongo not reachable (%s)', kind(e))
        else:
            await asyncio.to_thread(boot_backfills)
        task = asyncio.create_task(auto.loop())
        jobs = asyncio.create_task(scheduler.loop())
        ticking = asyncio.create_task(runtime.loop(instances.every))
        first_probe = asyncio.create_task(asyncio.to_thread(probe_at_start))

        async def version_at_start() -> None:
            await asyncio.sleep(VERSION_START_DELAY)
            await asyncio.to_thread(versions.start)

        first_version = asyncio.create_task(version_at_start())
        marked = threading.Event()

        def marks_job() -> None:
            try:
                mutemarks.run_once(store)
            finally:
                marked.set()

        threading.Thread(target=marks_job, name='skopax-mute-marks', daemon=True).start()

        async def follow_sources() -> None:
            while True:
                await asyncio.sleep(sources.every)
                await asyncio.to_thread(follower.tick)

        following = asyncio.create_task(follow_sources())
        keying = asyncio.create_task(aikey.loop())
        sso_keying = asyncio.create_task(sso_secret.loop())
        deciding = None
        if instances.age() is not None or listen_mode == 'off':
            await asyncio.to_thread(open_socket)
        else:
            log.info('the Slack listener is decided once the integrations load: their first load at boot failed')
            deciding = asyncio.create_task(open_socket_once_loaded())
        try:
            async with mcp_manager.run():
                yield
        finally:
            running = [t for t in (task, jobs, ticking, following, keying, sso_keying, first_probe, first_version, deciding) if t is not None]
            for t in running:
                t.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            try:
                await asyncio.to_thread(routes.stop, ROUTES_STOP_S)
            except Exception as e:
                log.warning('the route worker did not stop cleanly: %s', kind(e))
            try:
                await asyncio.to_thread(investigator_shutdown)
            except Exception as e:
                log.warning('investigator shutdown cleanup failed: %s', kind(e))
            if not await asyncio.to_thread(marked.wait, MARKS_STOP_S):
                log.info('the mute marks are still running at shutdown; they are left to finish, and an unfinished run starts again next time')
            if started_at_boot('slack-socket'):
                try:
                    await asyncio.to_thread(listener.stop)
                except Exception as e:
                    log.warning('slack listener stop failed: %s', kind(e))
            if proxy_pool:
                try:
                    await asyncio.to_thread(proxy_pool[0].shutdown)
                except Exception as e:
                    log.warning('the proxy sighting writer did not shut down: %s', kind(e))

    if settings.chat_ui:
        try:
            sso_secret.refresh()
        except PyMongoError as e:
            log.warning('the stored client secret was not read before the chat check, so the chat waits for SSO: %s', kind(e))
    chat_at_boot = bool(settings.chat_ui and signon.active())

    def investigator_ready() -> bool:
        return features.on('investigator') and investigator.enabled

    def no_ai_key() -> bool:
        return not aikey.current()

    feature_needs = {
        'investigator': (('anthropic_key', no_ai_key), ('mode', lambda: investigator.mode == 'off')),
        'chat': (('investigator', lambda: not investigator_ready()), ('chat_ui', lambda: not (settings.chat_ui and signon.active()))),
        'slack-autosend': (('slack_token', lambda: not slack.enabled),),
        'slack-listener': (('slack_app_token', lambda: link.app_token() == ''), ('slack_token', lambda: not slack.enabled), ('investigator', lambda: not investigator_ready()),
                           ('listen_mode', lambda: settings.slack_listen == 'off')),
        'mcp': (('sso', lambda: not signon.active()),),
        'push': (('bot_token', lambda: link.token() == ''), ('push_users', lambda: not pusher.users)),
        'postdeploy': (('prometheus', lambda: not prom_watching()),),
        'self-review': (('anthropic_key', no_ai_key), ('self_project', lambda: not (settings.self_project and settings.self_log_index))),
    }

    def need_text(family: str, need: str) -> str:
        key = f'{family}.{need}'
        return msg(key if key in messages.MESSAGES else f'features.needs.{need}')

    def needs_of(feature_id: str, family: str = 'features.needs') -> list[str]:
        return [need_text(family, need) for need, missing in feature_needs.get(feature_id, ()) if missing()]

    def not_ready_text(head: str = 'chat.not_ready', family: str = 'features.needs') -> str:
        return ' '.join([msg(head)] + needs_of('investigator', family))

    def started_at_boot(what: str) -> bool:
        return {'chat-ui': chat_at_boot, 'slack-socket': socket_state['started']}[what]

    app = FastAPI(title=brand.product(), version=__version__, lifespan=lifespan)
    app.state.features = features
    app.state.feature_needs = feature_needs
    app.state.auto_send = auto
    app.state.slack_fallback = fallback
    app.state.scheduler = scheduler
    app.state.sweeper = sweeper
    app.state.registry = registry
    app.state.instances = instances
    app.state.plugin_state = plugin_state
    app.state.runtime = runtime
    app.state.routes = routes
    app.state.signon = signon
    app.state.sso_secret = sso_secret
    app.state.live = live
    app.state.signin = signin
    app.state.mcp_auth = mcp_auth
    app.state.resolvers = resolvers
    app.state.server_apply = server_apply
    app.state.slack_tell = slack_tell
    app.state.slack_link = link
    app.state.chat_attribution = chat_attribution
    app.state.card_posts = card_posts
    app.state.jira_links = jira_links
    app.state.state_notes = notes
    app.state.enricher = enricher
    app.state.settle = settle
    app.state.pusher = pusher
    app.state.investigator = investigator
    app.state.server_apply = server_apply
    app.state.post_fixes = post_fixes
    app.state.operators = operators
    app.state.gate = gate
    app.state.aikey = aikey
    app.state.self_review = self_review
    app.state.replier = replier
    app.state.intake = intake
    app.state.clicks = clicks
    app.state.listener = listener
    app.state.proxy_write = None

    secure_cookies = (live.base.base_url or live.base.sso_redirect_uri).startswith('https://')

    def cookie_secure() -> bool:
        return secure_cookies

    def set_session(resp: Response, value: str, seconds: int) -> None:
        resp.set_cookie(SESSION_COOKIE, value, max_age=seconds, httponly=True, secure=cookie_secure(), samesite='lax')

    def cached_user(name: str, now: float) -> tuple[float, dict | None] | None:
        cached = user_cache.get(name)
        return cached if cached is not None and cached[0] > now else None

    def stored_user(name: str) -> dict | None:
        cached = cached_user(name, clock())
        if cached is not None:
            return cached[1]
        with user_probes_guard:
            probe = user_probes.setdefault(name, threading.Lock())
        with probe:
            now = clock()
            cached = cached_user(name, now)
            if cached is not None:
                return cached[1]
            if now < retry_after['user']:
                return None
            try:
                found = users.get(name)
            except PyMongoError as e:
                with failure_guard:
                    paused = clock() < retry_after['user']
                    retry_after['user'] = clock() + FAILURE_PAUSE_SECONDS
                if not paused:
                    log.warning('user lookup failed, treating the request as signed out: %s', kind(e))
                return None
            user_cache[name] = (now + USER_CACHE_SECONDS, found)
            return found

    def sso_session_ok(payload: dict) -> bool:
        if not settings.sso_on:
            return False
        after = stop_time()
        if after is None:
            return True
        login = payload.get('login')
        return isinstance(login, (int, float)) and not isinstance(login, bool) and math.floor(login) >= after

    def user_of(cookie: str | None) -> User | None:
        sessions = get_sessions() if cookie else None
        payload = sessions.unseal(cookie, SESSION_KIND) if sessions else None
        name = str(payload.get('name') or '').strip().lower() if payload else ''
        if name and payload.get('src') == 'sso' and not sso_session_ok(payload):
            return None
        doc = stored_user(name) if name else None
        if doc is None or doc.get('disabled'):
            return None
        if payload.get('src') == 'sso' and doc.get('source') != 'sso' and not link_admits(doc, payload.get('login')):
            return None
        return User(name=name, role=doc['role'], source=doc.get('source', 'local'))

    def current_user(request: Request) -> User | None:
        cookie = request.cookies.get(SESSION_COOKIE)
        resolved = getattr(request.state, 'resolved_user', None)
        if resolved is not None and resolved[0] == cookie:
            return resolved[1]
        return user_of(cookie)

    def proxy_writer() -> ThreadPoolExecutor:
        with proxy_guard:
            if not proxy_pool:
                proxy_pool.append(ThreadPoolExecutor(max_workers=1, thread_name_prefix=PROXY_THREAD))
            return proxy_pool[0]

    def save_sighting(peer: str, at: str) -> None:
        try:
            store.note_proxy(peer, at)
        except Exception as e:
            log.warning('the proxy sighting was not saved: %s', kind(e))

    def note_forwarded(request: Request) -> None:
        forwarded = request.headers.getlist('x-forwarded-for')
        if not forwarded or request.client is None:
            return
        if request.client.host.lower() in forwarded_hosts(forwarded):
            return
        now = clock()
        if now >= proxy_note['until']:
            proxy_note['until'] = now + PROXY_NOTE_SECONDS
            log.warning(msg('proxy.untrusted', peer=request.client.host))
            try:
                app.state.proxy_write = proxy_writer().submit(save_sighting, request.client.host, iso(utc_now()))
            except RuntimeError as e:
                log.warning('the proxy sighting was not saved: %s', kind(e))

    async def resolve_user(request: Request) -> User | None:
        cookie = request.cookies.get(SESSION_COOKIE)
        resolved = getattr(request.state, 'resolved_user', None)
        if resolved is None or resolved[0] != cookie:
            user = await run_in_threadpool(user_of, cookie) if cookie else None
            request.state.resolved_user = (cookie, user)
            resolved = request.state.resolved_user
        return resolved[1]

    @app.middleware('http')
    async def slide_session(request: Request, call_next) -> Response:
        note_forwarded(request)
        cookie = request.cookies.get(SESSION_COOKIE)
        user = await resolve_user(request)
        if chatui.needs_session(request.url.path) and user is None:
            if wants_page(request) and CHAT_PAGE.fullmatch(request.url.path):
                return RedirectResponse(f'/login?next={quote(request.url.path)}', status_code=status.HTTP_303_SEE_OTHER)
            return JSONResponse({'detail': msg('chat.login_first')}, status_code=status.HTTP_401_UNAUTHORIZED)
        resp = await call_next(request)
        if request.url.path == '/logout' or any(c.startswith(f'{SESSION_COOKIE}=') for c in resp.headers.getlist('set-cookie')):
            return resp
        renewed = sessions_box[0].renew(cookie) if user and sessions_box else None
        if renewed:
            set_session(resp, *renewed)
        return resp

    def database_trouble() -> bool:
        now = clock()
        return now < retry_after['user'] or now < retry_after['key']

    def public_reads() -> bool:
        return bool(settings.public_read)

    def gate_bearer(request: Request, authorization: str) -> bool:
        try:
            request.state.bearer = (authorization, bearer_actor(authorization))
        except HTTPException:
            return False
        return True

    def database_down(request: Request) -> Response:
        if wants_page(request):
            return message_page(msg('db.page_unavailable'), status.HTTP_503_SERVICE_UNAVAILABLE)
        return JSONResponse({'detail': msg('db.unavailable')}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    @app.middleware('http')
    async def read_gate(request: Request, call_next) -> Response:
        path = request.url.path
        if is_open(path) or chatui.needs_session(path):
            return await call_next(request)
        try:
            if request.method in READ_METHODS and await run_in_threadpool(public_reads):
                return await call_next(request)
            authorization = request.headers.get('authorization')
            if bearer_token(authorization):
                if await run_in_threadpool(gate_bearer, request, authorization):
                    return await call_next(request)
                return JSONResponse({'detail': msg('access.token_invalid')}, status_code=status.HTTP_401_UNAUTHORIZED)
            if await resolve_user(request) is not None:
                return await call_next(request)
            if request.cookies.get(SESSION_COOKIE) and database_trouble():
                log.error('read gate could not check the session: the database is not reachable')
                return database_down(request)
        except PyMongoError as e:
            log.error('read gate could not check access, the database is not reachable: %s', kind(e))
            return database_down(request)
        if wants_page(request) and path != mcptools_routes.CALLBACK_PATH:
            target = path + (f'?{request.url.query}' if request.url.query else '')
            return RedirectResponse(f'/login?next={quote(safe_next(target), safe="")}', status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({'detail': msg('access.login_first')}, status_code=status.HTTP_401_UNAUTHORIZED)

    @app.middleware('http')
    async def setup_guard(request: Request, call_next) -> Response:
        if setup_gate.settled or is_open(request.url.path):
            return await call_next(request)
        try:
            needed = await run_in_threadpool(setup_needed)
        except PyMongoError as e:
            log.warning('setup check failed, the database is not reachable: %s', kind(e))
            if wants_page(request):
                return message_page(msg('setup.page_unavailable'), status.HTTP_503_SERVICE_UNAVAILABLE, login=False)
            return JSONResponse({'detail': msg('setup.unavailable')}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        if not needed:
            return await call_next(request)
        if wants_page(request):
            return RedirectResponse('/setup', status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({'detail': msg('setup.detail')}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    def ended_by_stop(doc: dict) -> bool:
        if doc.get('via') != 'sso':
            return False
        if not settings.sso_on:
            return True
        made = clocks.parse_stamp(doc.get('createdAt'))
        if made is None:
            return True
        after = stop_time()
        return after is not None and made < after

    def bearer_actor(authorization: str) -> Actor:
        given = bearer_token(authorization) or ''
        if settings.token and hmac.compare_digest(given.encode(), settings.token.encode()):
            return Actor(BOOTSTRAP, 'admin', BOOTSTRAP)
        doc = api_tokens.resolve(given, refused=ended_by_stop)
        if doc is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, msg('access.token_invalid'))
        role = doc['role']
        if doc['createdBy'] != BOOTSTRAP:
            owner = users.get(doc['createdBy'])
            if owner is None or owner.get('disabled'):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, msg('access.token_invalid'))
            role = ROLES[min(ROLES.index(role), ROLES.index(owner['role']))] if owner.get('role') in ROLES else ROLES[0]
        return Actor(f"token:{doc['name']}", role, doc['createdBy'], doc.get('via'))

    def acting(request: Request, authorization: str | None, need: str | None = None) -> Actor:
        if bearer_token(authorization):
            kept = getattr(request.state, 'bearer', None)
            actor = kept[1] if kept and kept[0] == authorization else bearer_actor(authorization)
            if need and not role_at_least(actor.role, need):
                raise HTTPException(status.HTTP_403_FORBIDDEN, msg('access.token_role'))
        else:
            user = current_user(request)
            if user is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, msg('access.login_first'))
            if not same_origin(request):
                raise HTTPException(status.HTTP_403_FORBIDDEN, msg('login.cross_site'))
            if need and not role_at_least(user.role, need):
                raise HTTPException(status.HTTP_403_FORBIDDEN, msg(f'access.need_{need}'))
            actor = Actor(user.name, user.role, user.name)
        request.state.actor = actor
        return actor

    def signed_in(request: Request, authorization: str | None, role: str | None = None) -> str:
        return acting(request, authorization, role).name

    def require_operator(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
        return signed_in(request, authorization, 'operator')

    def require_login(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
        return signed_in(request, authorization)

    def require_admin(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
        return signed_in(request, authorization, 'admin')

    def require_machine(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
        if not bearer_token(authorization):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, msg('access.token_invalid'))
        return signed_in(request, authorization, 'operator')

    writer = [Depends(require_machine)]

    @app.exception_handler(Conflict)
    async def on_conflict(_, e: Conflict):
        return JSONResponse({'detail': e.text, 'rev': e.current}, status_code=status.HTTP_409_CONFLICT)

    @app.exception_handler(NotFound)
    async def on_not_found(_, e: NotFound):
        return JSONResponse({'detail': e.text}, status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(FieldRefused)
    async def on_field_refused(_, e: FieldRefused):
        return JSONResponse({'detail': e.detail, 'field': e.field}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    @app.exception_handler(PyMongoError)
    async def on_mongo_error(_, e: PyMongoError):
        log.error('mongo call failed: %s', kind(e))
        return JSONResponse({'detail': msg('db.unavailable')}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    @app.exception_handler(RequestValidationError)
    async def on_invalid_request(request: Request, e: RequestValidationError):
        if getattr(request.scope.get('route'), 'path', '').startswith('/api/admin/anthropic-key'):
            return JSONResponse({'detail': msg('aikey.invalid')}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
        if getattr(request.scope.get('route'), 'path', '').startswith('/api/admin/users'):
            return refused_user(e)
        return await refused_request(request, e)

    @app.exception_handler(ActionMoved)
    async def on_action_moved(_, e: ActionMoved):
        return JSONResponse({'detail': e.text, 'status': e.status}, status_code=status.HTTP_409_CONFLICT)

    @app.get('/actuator/health')
    @app.get('/actuator/health/readiness')
    def health() -> JSONResponse:
        state = 'UP' if store.ping() else 'DOWN'
        body = {'status': state, 'components': {'mongo': {'status': state}}, 'commit': settings.commit_id}
        return JSONResponse(body, status_code=status.HTTP_200_OK if state == 'UP' else status.HTTP_503_SERVICE_UNAVAILABLE)

    @app.get('/actuator/health/liveness')
    def liveness() -> dict:
        return {'status': 'UP'}

    @app.get('/', response_class=HTMLResponse)
    def page() -> str:
        return render_page({**store.load(), 'archiveHours': settings.archive_hours, 'plugins': plugin_tiles(store.server_runs()['passes'])}, runtime.page_scripts())

    @app.get('/c/{num}', response_class=HTMLResponse)
    def card_page(num: int) -> HTMLResponse:
        return HTMLResponse(render_card_page(runtime.page_packs()), headers={'Cache-Control': 'no-cache'})

    @app.get('/api/cards/{num}')
    def get_card(num: int) -> dict:
        key = store.key_of_number(num)
        if key is None:
            raise HTTPException(status_code=404, detail=f'no card #{num}')
        try:
            card = store.anomaly(key)
            return card_view(card, True, store.children(key), parent_of(card)) | {'last': store.last_action(key)}
        except NotFound:
            past = store.last_past_card(key)
            return card_view(past | {'num': num}, False) if past else {'num': num, 'key': key, 'live': False}

    @app.get('/static/{name}')
    def asset(name: str, if_none_match: Annotated[str | None, Header()] = None) -> Response:
        if name not in ASSETS:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'no such file')
        body, digest = ASSETS[name]
        headers = {'Cache-Control': 'no-cache', 'ETag': f'"{digest}"'}
        if if_none_match == headers['ETag']:
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        return Response(body, media_type='text/javascript; charset=utf-8', headers=headers)

    @app.get('/plugins/{plugin_id}/{file}')
    def plugin_asset(plugin_id: str, file: str) -> Response:
        found = runtime.asset(plugin_id, file)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'no such file')
        headers = {'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff'}
        if found[1].startswith(('image/svg', 'text/html')):
            headers['Content-Security-Policy'] = 'sandbox'
        return Response(found[0], media_type=found[1], headers=headers)

    @app.get('/manifest.webmanifest')
    def manifest() -> Response:
        return Response(manifest_json(), media_type='application/manifest+json', headers={'Cache-Control': 'no-cache'})

    @app.get('/favicon.ico')
    def favicon(if_none_match: Annotated[str | None, Header()] = None) -> Response:
        return icon('favicon.ico', if_none_match)

    @app.get('/icons/{name}')
    def icon(name: str, if_none_match: Annotated[str | None, Header()] = None) -> Response:
        if name not in ICONS:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'no such file')
        body, digest = ICONS[name]
        headers = {'Cache-Control': 'no-cache', 'ETag': f'"{digest}"'}
        if if_none_match == headers['ETag']:
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        return Response(body, media_type=ICON_TYPES[Path(name).suffix], headers=headers)

    def sso_icon_version() -> str | None:
        if settings.sso_button_icon != 'company':
            return None
        try:
            return sso_icon.status()['version']
        except PyMongoError as e:
            log.warning('the single sign-on button icon could not be read: %s', kind(e))
            return None

    def login_button() -> dict:
        return ssobutton.button(settings.sso_issuer, settings.sso_button_text, settings.sso_button_icon, sso_icon_version())

    def login_page(error: str = '', email: str = '', next_page: str | None = None, status_code: int = status.HTTP_200_OK) -> HTMLResponse:
        active = signon.active()
        return HTMLResponse(render_login(error, email, next_page or '/', active, login_button() if active else None), status_code=status_code, headers={'Cache-Control': 'no-store'})

    @app.get('/icons/sso/{name}')
    def sso_mark_icon(name: str) -> Response:
        if name not in SSO_ICONS:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'no such file')
        return Response(SSO_ICONS[name], media_type='image/svg+xml', headers={'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': SSO_ICON_CSP})

    @app.get('/login/sso-icon')
    def sso_company_icon(v: str = '') -> Response:
        try:
            version = sso_icon.status()['version']
            held = sso_icon.data(version) if version and (not v or v == version) else None
        except PyMongoError as e:
            log.warning('the single sign-on button icon could not be read: %s', kind(e))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('sso.icon_unavailable')) from None
        if held is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'no icon')
        cache = SSO_ICON_CACHE if v == version else 'no-cache'
        return Response(held[0], media_type=held[1], headers={'Cache-Control': cache, 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': SSO_ICON_CSP})

    @app.get('/login', response_model=None)
    def login_form(next_page: Annotated[str | None, Query(alias='next')] = None) -> HTMLResponse:
        return login_page(next_page=next_page)

    def finish_login(request: Request, fields: dict[str, str]) -> RedirectResponse | HTMLResponse:
        sessions = get_sessions()
        if sessions is None:
            return message_page(msg('login.failed'), status.HTTP_503_SERVICE_UNAVAILABLE)
        email = fields['email'].strip().lower()[:EMAIL_KEY_MAX]
        keys = (f'email:{email}',) + ip_keys(request)
        try:
            held = throttle.reserve(*keys)
            if held is None:
                return login_page(msg('login.throttled'), email, fields['next'], status.HTTP_429_TOO_MANY_REQUESTS)
            try:
                found = users.authenticate(email, fields['password'])
            except PyMongoError:
                throttle.give_up(held)
                raise
            if found is None:
                return login_page(msg('login.error'), email, fields['next'], status.HTTP_400_BAD_REQUEST)
            throttle.forgive(held, keys[0])
            users.touch_login(found['email'])
        except PyMongoError as e:
            log.warning('login could not be checked: %s', kind(e))
            return message_page(msg('login.failed'), status.HTTP_503_SERVICE_UNAVAILABLE)
        user_cache.pop(found['email'], None)
        log.info('local login by %s, role=%s', found['email'], found['role'])
        resp = RedirectResponse(safe_next(fields['next']), status_code=status.HTTP_303_SEE_OTHER)
        set_session(resp, sessions.issue(found['email'], found['role'], 'local'), sessions.idle_seconds)
        return resp

    @app.post('/login', response_model=None)
    async def login_submit(request: Request) -> RedirectResponse | HTMLResponse | JSONResponse:
        if not same_origin(request):
            return JSONResponse({'detail': msg('login.cross_site')}, status_code=status.HTTP_403_FORBIDDEN)
        fields = await read_form(request, ('email', 'password', 'next'))
        return await run_in_threadpool(finish_login, request, fields)

    def forget_login(resp: Response) -> Response:
        resp.delete_cookie(LOGIN_COOKIE, httponly=True, secure=cookie_secure(), samesite='lax')
        return resp

    @app.get('/login/sso', response_model=None)
    def sso_login(next_page: Annotated[str | None, Query(alias='next')] = None) -> RedirectResponse | HTMLResponse:
        if not signon.active():
            return message_page(msg('login.sso_off'), status.HTTP_404_NOT_FOUND)
        sessions = get_sessions()
        if sessions is None:
            return message_page(msg('login.failed'), status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            location, pending = signon.start(safe_next(next_page))
        except OidcError as e:
            log.warning('login could not start: %s', e.reason)
            return message_page(msg('login.failed'), status.HTTP_503_SERVICE_UNAVAILABLE)
        resp = RedirectResponse(location, status_code=status.HTTP_302_FOUND)
        resp.set_cookie(LOGIN_COOKIE, sessions.seal(pending, LOGIN_SECONDS, LOGIN_KIND), max_age=LOGIN_SECONDS, httponly=True, secure=cookie_secure(), samesite='lax')
        return resp

    def login_refused(e: OidcError) -> HTMLResponse:
        if e.reason in LOGIN_RESTARTS:
            log.warning('login refused: %s', e.reason)
            return message_page(msg(LOGIN_RESTARTS[e.reason]), status.HTTP_400_BAD_REQUEST)
        log.warning('login failed: %s', e.reason)
        unavailable = e.reason in LOGIN_UNAVAILABLE
        return message_page(msg('login.failed'), status.HTTP_503_SERVICE_UNAVAILABLE if unavailable else status.HTTP_401_UNAUTHORIZED)

    @app.get('/authorized', name='authorized', response_model=None)
    def authorized(request: Request, code: str | None = None, state: str | None = None, error: str | None = None) -> Response:
        sessions = get_sessions()
        if sessions is None:
            return forget_login(message_page(msg('login.failed'), status.HTTP_503_SERVICE_UNAVAILABLE))
        pending = sessions.unseal(request.cookies.get(LOGIN_COOKIE), LOGIN_KIND)
        if pending is None:
            return forget_login(message_page(msg('login.expired'), status.HTTP_400_BAD_REQUEST))
        if error or not code:
            log.warning('login returned without a code: %s', (error if OAUTH_ERROR.fullmatch(error) else 'an unexpected error value') if error else 'no code')
        try:
            claims = signon.finish(pending, code, state, error)
            stored = signin.page(claims, str(pending.get('issuer') or ''))
        except OidcError as e:
            return forget_login(login_refused(e))
        except Refused as e:
            log.warning('login refused: %s', e.reason)
            if e.reason == 'not-added' and e.email:
                note_not_added(e.email)
            return forget_login(message_page(msg('login.failed'), e.status))
        email = stored['email']
        user_cache.pop(email, None)
        log.info('login by %s, role=%s', email, stored['role'])
        resp = RedirectResponse(safe_next(pending.get('next')), status_code=status.HTTP_303_SEE_OTHER)
        set_session(resp, sessions.issue(email, stored['role'], 'sso'), sessions.idle_seconds)
        return forget_login(resp)

    def setup_page(error: str = '', email: str = '', name: str = '', status_code: int = status.HTTP_200_OK) -> HTMLResponse:
        return HTMLResponse(render_form(error, email, name), status_code=status_code, headers={'Cache-Control': 'no-store'})

    def setup_unavailable(request: Request) -> HTMLResponse | JSONResponse:
        if 'text/html' in request.headers.get('accept', ''):
            return message_page(msg('setup.page_unavailable'), status.HTTP_503_SERVICE_UNAVAILABLE, login=False)
        return JSONResponse({'detail': msg('setup.unavailable')}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    @app.get('/setup', response_model=None)
    def setup_form(request: Request) -> RedirectResponse | HTMLResponse | JSONResponse:
        try:
            if not setup_needed():
                return RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)
            code = setup_gate.issue_code()
        except PyMongoError as e:
            log.warning('setup page could not be served: %s', kind(e))
            return setup_unavailable(request)
        if code:
            announce_code(code)
        return setup_page()

    def finish_setup(request: Request, fields: dict[str, str]) -> RedirectResponse | HTMLResponse:
        sessions = get_sessions()
        if sessions is None:
            return setup_unavailable(request)
        email, name = fields['email'], fields['name']
        ips = ip_keys(request)
        if fields['password'] != fields['password2']:
            return setup_page(msg('setup.error'), email, name, status.HTTP_400_BAD_REQUEST)
        try:
            held = throttle.reserve(*ips)
        except PyMongoError as e:
            log.warning('setup throttle check failed: %s', kind(e))
            return setup_unavailable(request)
        if held is None:
            return setup_page(msg('setup.error'), email, name, status.HTTP_429_TOO_MANY_REQUESTS)
        def give_back() -> None:
            try:
                throttle.release(held)
            except PyMongoError as e:
                log.warning('setup attempt not released: %s', kind(e))

        try:
            admin = setup_gate.complete(fields['code'], email, name, fields['password'])
        except SetupDone:
            give_back()
            return RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)
        except BadCode:
            return setup_page(msg('setup.error'), email, name, status.HTTP_400_BAD_REQUEST)
        except (InvalidUser, UserExists):
            give_back()
            return setup_page(msg('setup.error'), email, name, status.HTTP_400_BAD_REQUEST)
        except PyMongoError as e:
            give_back()
            log.warning('setup could not be saved: %s', kind(e))
            return setup_unavailable(request)
        user_cache.pop(admin['email'], None)
        try:
            throttle.clear(*ips)
        except PyMongoError as e:
            log.warning('setup failures not cleared: %s', kind(e))
        log.info('first admin created: %s', admin['email'])
        resp = RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)
        set_session(resp, sessions.issue(admin['email'], 'admin', 'local'), sessions.idle_seconds)
        return resp

    @app.post('/setup', response_model=None)
    async def setup_submit(request: Request) -> RedirectResponse | HTMLResponse | JSONResponse:
        if not same_origin(request):
            return JSONResponse({'detail': msg('setup.cross_site')}, status_code=status.HTTP_403_FORBIDDEN)
        try:
            if not await run_in_threadpool(setup_needed):
                return RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)
        except PyMongoError as e:
            log.warning('setup check failed: %s', kind(e))
            return setup_unavailable(request)
        fields = await read_form(request, ('code', 'email', 'name', 'password', 'password2'))
        return await run_in_threadpool(finish_setup, request, fields)

    @app.get('/logout', response_class=HTMLResponse)
    def logout_form() -> HTMLResponse:
        return logout_page()

    @app.post('/logout', response_model=None)
    def logout(request: Request) -> RedirectResponse | JSONResponse:
        if not same_origin(request):
            return JSONResponse({'detail': msg('login.cross_site')}, status_code=status.HTTP_403_FORBIDDEN)
        resp = RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)
        resp.delete_cookie(SESSION_COOKIE)
        chatui.forget(resp)
        return resp

    @app.post('/api/me/password', status_code=status.HTTP_204_NO_CONTENT, response_model=None)
    def change_password(request: Request, body: PasswordIn, authorization: Annotated[str | None, Header()] = None) -> Response:
        if get_sessions() is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('login.failed'))
        user = current_user(request) if not bearer_token(authorization) else None
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, msg('password.needs_session'))
        if not same_origin(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, msg('login.cross_site'))
        keys = (f'email:{user.name}',) + ip_keys(request)
        try:
            held = throttle.reserve(*keys)
            if held is None:
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg('login.throttled'))
            try:
                found = users.authenticate(user.name, body.current)
            except PyMongoError:
                throttle.give_up(held)
                raise
            if found is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, msg('password.error'))
            try:
                users.set_password(user.name, body.new)
            except InvalidUser:
                throttle.release(held)
                raise HTTPException(status.HTTP_400_BAD_REQUEST, msg('password.error')) from None
            except PyMongoError:
                throttle.give_up(held)
                raise
            throttle.forgive(held, keys[0])
        except PyMongoError as e:
            log.warning('password could not be changed: %s', kind(e))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('login.failed')) from None
        log.info('password changed by %s', user.name)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get('/api/me')
    def me(request: Request) -> dict:
        user = current_user(request)
        name = (stored_user(user.name) or {}).get('name') if user else None
        out = {
            'chatMax': settings.chat_max_chars,
            'loginEnabled': True,
            'sso': signon.active(),
            'userRole': user.role if user else None,
            'source': user.source if user else None,
            'canChangePassword': bool(user and user.source == 'local'),
            'user': user.name if user else None,
            'name': name or None,
            'operator': bool(user and user.operator),
            'jira': jira.enabled,
            'jiraUrl': jira_site() if jira.enabled else '',
            'gitlab': gitlab.enabled,
            'envLabel': settings.env_label,
            'task': {'project': settings.jira_project, 'projects': list(dict.fromkeys([*projects_of(settings), *(k for k in plugin_projects() if PROJECT_KEY.fullmatch(k) and k != settings.jira_tech_project)])), 'parent': settings.jira_parent, 'points': settings.jira_points, 'techProject': settings.jira_tech_project, 'techAssignee': bool(settings.jira_tech_assignee), 'titlePrefix': settings.jira_title_prefix},
            'slack': slack.enabled,
            'slackChannel': slack.channel,
            'tags': tags.info(),
            'routes': routes.info() if user and user.operator else [],
            'admin': bool(user and user.admin),
            'chat': chat_at_boot,
        }
        out['features'] = {f.id: features.on(f.id) for f in FEATURES}
        out['featureOff'] = {f.id: off_text(f.id) for f in FEATURES if not features.on(f.id)}
        out['instances'] = instance_menu(settings, live.instance_links())
        return out

    @app.get('/api/state')
    def get_state() -> dict:
        return {**store.load(), 'archiveHours': settings.archive_hours}

    @app.get('/api/history/cards')
    def get_closed_cards(days: Annotated[int, Query(ge=1, le=history.CLOSED_DAYS_MAX)] = 7, start: Annotated[str | None, Query(alias='from', max_length=30)] = None,
                         end: Annotated[str | None, Query(alias='to', max_length=30)] = None, q: Annotated[str | None, Query(max_length=history.TEXT_QUERY_LIMIT)] = None,
                         outcome: Annotated[str | None, Query(max_length=20)] = None, page: Annotated[int, Query(ge=0, le=1000)] = 0,
                         size: Annotated[int, Query(ge=1, le=history.CLOSED_PAGE_MAX)] = 50) -> dict:
        if outcome and outcome not in history.OUTCOME_TEXT:
            raise HTTPException(status_code=422, detail='outcome is one of ' + ', '.join(history.OUTCOME_TEXT))
        now = utc_now()
        oldest = now - dt.timedelta(days=history.CLOSED_DAYS_MAX)
        try:
            lo = closed_bound(start, False) if start else now - dt.timedelta(days=days)
            hi = closed_bound(end, True) if end else now
        except (ValueError, OverflowError):
            raise HTTPException(status_code=422, detail='from and to are dates like 2026-11-04 or ISO times like 2026-11-04T09:00:00Z') from None
        lo, hi = max(lo, oldest), min(hi, now)
        if lo > hi:
            raise HTTPException(status_code=422, detail='from is after to')
        rows, total = store.closed_cards(iso(lo), iso(hi), q, outcome, page, size)
        return {'from': iso(lo), 'to': iso(hi), 'page': page, 'size': size, 'total': total, 'items': rows, 'outcomes': history.OUTCOME_TEXT}

    @app.get('/api/rev')
    def get_rev() -> dict:
        return store.rev()

    def admin_viewer(request: Request, authorization: str | None) -> bool:
        try:
            return role_at_least(acting(request, authorization).role, 'admin')
        except HTTPException:
            return False

    @app.get('/api/server')
    def get_server(request: Request, authorization: Annotated[str | None, Header()] = None) -> dict:
        runs = store.server_runs()
        reach = store.last_probe() if settings.probe else None
        shown = reach if reach is None or admin_viewer(request, authorization) else public_reach(reach)
        return server_info() | runs | {'plugins': plugin_tiles(runs['passes']), 'tz': settings.timezone, 'version': __version__, 'product': brand.DEFAULT_NAME, 'displayName': brand.product(),
                                       'reach': shown}

    @app.post('/api/probe', dependencies=writer)
    def post_probe(request: Request) -> dict:
        if not settings.probe:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('probe.off'))
        report = probe_run(time.time())
        return report if role_at_least(request.state.actor.role, 'admin') else public_reach(report)

    @app.post('/api/checks/{plugin_id}/{check_id}/run', status_code=status.HTTP_202_ACCEPTED)
    def post_check_run(plugin_id: str, check_id: str, by: Annotated[str, Depends(require_operator)]) -> dict:
        found = registry.get(plugin_id)
        plugin = found.plugin if found is not None else None
        check = next((c for c in plugin.checks if c.id == check_id), None) if plugin is not None else None
        if check is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('checks.unknown'))
        items = next((items for p, items in runtime.serving() if p.id == plugin.id), None)
        if items is None:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('checks.not_serving', name=plugin.name))
        if plugin.fields and not any(check.id not in i.off_checks and owned(check, i) for i in items):
            refused = 'checks.unchecked' if not check.once else 'checks.unchecked_once' if any(i.primary for i in items) else 'checks.primary_not_serving'
            raise HTTPException(status.HTTP_409_CONFLICT, msg(refused, check=check.label, name=plugin.name))
        job = f'{plugin.id}.{check.id}'
        scheduler.nudge(job)
        store.add_activity('check', by, msg('checks.run_now', check=check.label, by=by))
        return {'job': job}

    @app.get('/api/screenshots/{shot_id}')
    def get_screenshot(shot_id: str) -> Response:
        doc = store.screenshot(shot_id)
        return Response(bytes(doc['data']), media_type=doc.get('mime') or 'image/jpeg', headers={'Cache-Control': 'private, max-age=86400, immutable'})

    @app.get('/api/investigations')
    def get_investigations(key: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50, chats: bool = True) -> list[dict]:
        return store.list_investigations(key, limit, page=True, chats=chats)

    @app.get('/api/card-runs')
    def get_card_runs() -> dict:
        return store.card_runs(a.get('key') for a in store.load().get('anomalies') or [])

    def tagging(key: str, fields: dict, by: str, line: str) -> dict:
        rev, card = store.set_tagging(key, fields)
        store.add_activity('tags', by, f'{line} · {key}', key=key)
        log.info('%s on %s by %s', line, key, by)
        routes.later([card], time.time(), by=by)
        return {'rev': rev, 'tags': card.get('tags') or [], 'tagsOff': card.get('tagsOff') or [], 'business': card.get('business')}

    def chat_used(by: str) -> int:
        return store.count_actions('ask', iso(dt.datetime.fromtimestamp(clocks.midnight(time.time()), dt.timezone.utc)), source='chat', by=by)

    def chat_card_of(card: dict) -> dict:
        note = card.get('note') or {}
        out = {'key': card['key'], 'num': card.get('num'), 'svc': card.get('svc'), 'label': card.get('label'), 'title': f"{card.get('svc') or '-'} / {card.get('label') or '?'}",
               'status': card.get('status'), 'sev': card.get('sev'), 'what': card.get('what'), 'next': card.get('recommend'), 'note': note.get('text') if isinstance(note, dict) else None,
               'owner': card.get('owner'), 'ticket': card.get('ticket')}
        return {k: v for k, v in out.items() if v not in (None, '')}

    def chat_view(chat: dict) -> dict:
        cards = []
        for k in chat.get('cards') or []:
            try:
                card = store.anomaly(k)
            except NotFound:
                continue
            cards.append(chat_card_of(card))
        messages = [m | {'answer': redact(m['answer'])} if m.get('answer') else m for m in chat.get('messages') or []]
        return chat | {'cards': cards, 'messages': messages, 'halted': investigator.why_halted(time.time()) if chat.get('pending') else None}

    @app.get('/api/notifications')
    def get_notifications(by: Annotated[str, Depends(require_login)], limit: Annotated[int, Query(ge=1, le=100)] = 30,
                          before: Annotated[str | None, Query(max_length=64)] = None) -> dict:
        user = inbox_of(by)
        if not user:
            return {'items': [], 'unread': 0}
        return {'items': store.notices_of(user, limit, before), 'unread': store.unread_notices(user)}

    @app.get('/api/notifications/count')
    def get_notification_count(by: Annotated[str, Depends(require_login)]) -> dict:
        user = inbox_of(by)
        return {'unread': store.unread_notices(user) if user else 0}

    @app.post('/api/notifications/read')
    def post_notifications_read(body: NoticesReadIn, by: Annotated[str, Depends(require_login)]) -> dict:
        user = inbox_of(by)
        if not user:
            return {'read': 0, 'unread': 0}
        read = store.read_notices(user, None if body.all else body.ids)
        return {'read': read, 'unread': store.unread_notices(user)}

    @app.get('/api/chats')
    def get_chats(by: Annotated[str, Depends(require_login)]) -> dict:
        return {'chats': store.chats(by, CHAT_LIST), 'cap': settings.chat_daily_cap, 'usedToday': chat_used(by), 'model': settings.chat_model}

    @app.get('/api/chats/{chat_id}')
    def get_chat(chat_id: str, by: Annotated[str, Depends(require_login)]) -> dict:
        chat = store.chat_for(by, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail='no such chat, or it is not shared with you')
        return chat_view(chat)

    @app.put('/api/chats/{chat_id}/share')
    def put_chat_share(chat_id: str, body: ChatShareIn, by: Annotated[str, Depends(require_login)]) -> dict:
        try:
            return {'shared': store.share_chat(by, chat_id, body.on)}
        except NotFound as e:
            raise HTTPException(status_code=404, detail=e.text) from e
        except ChatPrivate as e:
            raise HTTPException(status_code=409, detail=e.text) from e

    @app.post('/api/chats/{chat_id}/continue', status_code=status.HTTP_201_CREATED)
    def post_chat_continue(chat_id: str, by: Annotated[str, Depends(require_login)]) -> dict:
        try:
            return {'id': store.continue_chat(by, chat_id)}
        except NotFound as e:
            raise HTTPException(status_code=404, detail=e.text) from e

    @app.get('/share/{chat_id}')
    def get_share(chat_id: str) -> Response:
        if not SHARE_ID.fullmatch(chat_id):
            raise HTTPException(status_code=404, detail='no such shared chat')
        return RedirectResponse(f'/chat/share/{chat_id}', status_code=status.HTTP_303_SEE_OTHER)

    @app.get('/chat-continue/{chat_id}')
    def get_chat_continue(chat_id: str, request: Request) -> Response:
        user = current_user(request)
        if user is None:
            return RedirectResponse(f'/login?next={quote("/chat-continue/" + chat_id)}', status_code=303)
        if not chat_at_boot:
            return RedirectResponse('/', status_code=303)
        try:
            fresh = store.continue_chat(user.name, chat_id)
        except NotFound:
            return RedirectResponse('/chat/', status_code=303)
        return RedirectResponse(f'/chat/thread/{fresh}', status_code=303)

    @app.post('/api/chats', status_code=status.HTTP_201_CREATED)
    def post_chat(body: ChatIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        return chat_post(by, body.text, body.chatId, body.cards)

    def chat_post(by: str, text: str, wanted_id: str | None, wanted_cards: list[str] | None, create: bool = False, via: str | None = None, role: str | None = None) -> dict:
        for feature_id in ('chat', 'investigator'):
            if not features.on(feature_id):
                raise FeatureOff(feature_id)
        if not investigator_ready():
            raise NotReady(not_ready_text(), tuple(needs_of('investigator')))
        question = str(text or '').strip()
        if not question:
            raise HTTPException(status_code=422, detail='write a message first')
        if len(question) > settings.chat_max_chars:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=f'a message is at most {settings.chat_max_chars} characters')
        chat = store.chat(by, wanted_id) if wanted_id else None
        fresh = bool(wanted_id) and chat is None and create and CHAT_ID.fullmatch(wanted_id) is not None and store.chat_owner(wanted_id) is None
        if wanted_id and chat is None and not fresh:
            raise HTTPException(status_code=404, detail='no such chat of yours')
        if chat and chat['pending']:
            raise HTTPException(status_code=409, detail='wait for the answer to your last message')
        cap = settings.chat_daily_cap
        if chat_used(by) >= cap:
            raise HTTPException(status_code=429, detail=f'{cap} chat messages a day is the cap; it starts again at midnight' if cap else 'the chat is switched off')
        cards = []
        for k in dict.fromkeys((chat['cards'] if chat else wanted_cards) or []):
            try:
                store.anomaly(k)
            except NotFound:
                continue
            cards.append(k)
        thread = []
        for m in (chat['messages'] if chat else [])[-CHAT_THREAD:]:
            thread += [{'who': 'person', 'text': m.get('text') or ''}] + ([{'who': 'bot', 'text': m['answer']}] if m.get('answer') else [])
        chat_id = chat['id'] if chat else wanted_id if fresh else new_id()
        action = {'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': f'chat:{chat_id}', 'text': question, 'label': question[:300], 'cards': cards[:CHAT_CARDS],
                  'requestedBy': by, 'chat': {'id': chat_id}, 'thread': thread} | ({'via': via} if via else {}) | ({'mcpRole': role} if role else {})
        try:
            said = page_topic_check(question, action['cards'], by, thread, 'chat')
        except HTTPException as e:
            if e.status_code != 422:
                raise
            store.add_action(action | {'refused': True}, 'done', str(e.detail))
            return chat_view(store.chat_for(by, chat_id))
        if said is not None:
            store.add_action(action | {'smallTalk': True, 'usd': round(said.usd, 6)}, 'done', said.text)
        else:
            store.add_action(action)
            if investigator.enabled:
                investigator.kick(time.time())
        return chat_view(store.chat_for(by, chat_id))

    @app.put('/api/assignee')
    def put_assignee(body: AssigneeIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        try:
            return assigner.assign(body.key, by if body.me else body.name, by if body.me and '@' in by else body.email, by)
        except NotFound as e:
            raise HTTPException(status_code=404, detail=e.text) from e
        except AssignInvalid as e:
            raise HTTPException(status_code=422, detail=e.text) from e

    @app.get('/api/people')
    def get_people(q: Annotated[str, Query(max_length=80)], by: Annotated[str, Depends(require_login)]) -> list[dict]:
        if not jira.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Jira is not set up on this service')
        if len(q.strip()) < PEOPLE_MIN:
            return []
        try:
            return people_of(jira.people(q.strip(), PEOPLE_HITS * 2))[:PEOPLE_HITS]
        except NoAccess as e:
            log.warning('jira people search refused: %s', kind(e))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, text_of(e)) from None
        except Exception as e:
            log.warning('jira people search failed: %s', kind(e))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, 'Jira did not answer') from None

    @app.post('/api/tags')
    def post_tags(body: TagIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        try:
            fields = tags.toggled(store.anomaly(body.key), body.tag, body.on, by, iso(utc_now()))
        except tags.Refused as e:
            raise HTTPException(status_code=422, detail=e.text) from e
        out = tagging(body.key, fields, by, f"{'tagged' if body.on else 'untagged'} {tags.label(body.tag)}")
        card_posts.refresh(body.key)
        return out

    @app.put('/api/business')
    def put_business(body: BusinessIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        store.anomaly(body.key)
        fields = tags.business(body.text, by, iso(utc_now()), body.why, body.kind)
        return tagging(body.key, fields, by, 'business sentence ' + ('saved' if fields['business'] else 'removed'))

    def end_sso_sessions() -> None:
        live.tunables.mark(SESSIONS_AFTER_KEY, clocks.stamp(clock()))
        live.refresh(force=True)
        log.warning('%s', msg('sso.stopped_log'))

    def turns_sso_off(value: str | None) -> bool:
        if not settings.sso_on:
            return False
        return not live.base.sso_on if value is None else str(value).strip() == 'false'

    def way_back_in(request: Request, authorization: str | None) -> bool:
        actor = getattr(request.state, 'actor', None)
        return bool(bearer_token(authorization)) and actor is not None and actor.via != 'sso'

    def may_switch_sso_off(bearer: bool) -> None:
        if not bearer and not users.has_password_admin():
            raise HTTPException(status.HTTP_409_CONFLICT, msg('sso.off_needs_local_admin'))

    def admin_change(key: str, value: str | None, by: str, bearer: bool = False) -> dict:
        try:
            features.guard(key, value)
        except Locked as e:
            raise HTTPException(status.HTTP_409_CONFLICT, e.detail) from None
        if key == SLACK_CHANNEL_KEY and value is not None:
            problem = slack.check_channel(str(value).strip())
            if problem:
                raise HTTPException(status_code=422, detail=f'Channel: {problem}')
        if key == SSO_SWITCH_KEY and turns_sso_off(value):
            may_switch_sso_off(bearer)
        was_on = key == SSO_SWITCH_KEY and bool(settings.sso_on)
        try:
            entry, change = live.set(key, value, by)
        except KeyError:
            raise HTTPException(status_code=404, detail=f'no setting named {key} on the admin page') from None
        except TunableInvalid as e:
            raise HTTPException(status_code=422, detail=e.text) from None
        if key in (SLACK_CHANNEL_KEY, 'slack.dm-chats'):
            check_slack_scopes()
        if key == 'core.language':
            outbound_language()
        if was_on and not settings.sso_on:
            end_sso_sessions()
        if key.startswith(('sso.', 'mcp.')):
            mcp_auth.clear()
        cut = lambda v: v if len(v) <= 80 else v[:79] + '…'
        if key.startswith('sso.'):
            store.add_activity(ACCESS_KIND, by, msg('sso.setting_changed', label=change['label'], key=key, before=cut(change['before'] or '\u2013'), after=cut(change['after'] or '\u2013')))
        else:
            store.add_activity('admin', by, f"{change['label']} ({key}): {cut(change['before'] or '\u2013')} \u2192 {cut(change['after'] or '\u2013')}" + ('' if entry['live'] else ' \u00b7 applies after a restart'))
        log.info('admin setting %s %s by %s', key, 'reset' if value is None else 'set', by)
        return {'item': entry, 'change': change}

    @app.get('/api/admin/users')
    def get_admin_users(by: Annotated[str, Depends(require_admin)]) -> list[dict]:
        now = clock()
        return [admin_row(doc, now) for doc in users.list()]

    @app.post('/api/admin/users', status_code=status.HTTP_201_CREATED)
    def post_admin_user(body: UserIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        email = user_field(clean_email, 'email', body.email)
        role = user_field(clean_role, 'role', body.role)
        name = email.partition('@')[0] if body.name == '' else user_field(clean_name, 'name', body.name)
        password = body.password or None
        if body.sso and not signon.active():
            raise FieldRefused('sso', msg('access.sso_off'))
        if password is None and not body.sso:
            raise FieldRefused('password', msg('access.password_or_sso'))
        if password is not None:
            user_field(clean_password, 'password', password)
        if body.sso and not sso_domain_allowed(email):
            raise FieldRefused('email', msg('access.sso_domain'))
        try:
            made = users.add_sso(email, name, role) if password is None else users.create(email, name, role, password, link_by=by if body.sso else None)
        except UserExists as e:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.user_exists')) from e
        except InvalidUser as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.text) from e
        if password is None:
            store.add_activity(ACCESS_KIND, by, msg('access.user_added_sso', email=made['email'], role=made['role']))
        else:
            store.add_activity(ACCESS_KIND, by, msg('access.user_created', email=made['email'], role=made['role']))
        if made.get('ssoLink'):
            store.add_activity(ACCESS_KIND, by, msg('access.sso_linked', email=made['email'], days=LINK_PENDING_S // 86400))
        return admin_row(made, clock())

    def sso_domain_allowed(email: str) -> bool:
        return email_allowed(email, tuple(settings.sso_allowed_domains or ()))

    def checked_patch(key: str, body: UserPatch, current: dict) -> None:
        to_local = body.password is not None and current.get('source') == 'sso'
        if to_local and current.get('opened') is not False:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg('access.sso_password'))
        if body.ssoLink is not None and current.get('source') != 'local' and not to_local:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg('access.sso_link_local_only'))
        try:
            fields, _ = users.prepare_change(body.role, body.disabled, None, current)
            if body.password is not None and not to_local:
                clean_password(body.password)
        except InvalidUser as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.text) from e
        except IdpManaged:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.role_from_idp')) from None
        if to_local:
            user_field(clean_password, 'password', body.password)
        if body.ssoLink and not sso_domain_allowed(key):
            raise FieldRefused('ssoLink', msg('access.sso_domain'))
        if leaves_admin(fields) and users.is_last_admin(key):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.last_admin'))

    def sso_to_local(key: str, body: UserPatch, fields: dict, by: str) -> dict:
        link = sso_domain_allowed(key)
        try:
            made = users.make_local(key, body.password, by, fields, link)
        except LastAdmin:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.last_admin')) from None
        finally:
            user_cache.pop(key, None)
            mcp_auth.forget(key)
        if made is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg('access.sso_password'))
        store.add_activity(ACCESS_KIND, by, msg('access.user_password', email=key))
        if link:
            store.add_activity(ACCESS_KIND, by, msg('access.sso_linked', email=key, days=LINK_PENDING_S // 86400))
        return made

    @app.patch('/api/admin/users/{email}')
    def patch_admin_user(email: str, body: UserPatch, by: Annotated[str, Depends(require_admin)]) -> dict:
        key = email.strip().lower()
        current = users.get(key)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('access.unknown_user'))
        checked_patch(key, body, current)
        try:
            fields, unset = users.prepare_change(body.role, body.disabled, body.password, current)
        except InvalidUser as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.text) from e
        except IdpManaged:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.role_from_idp')) from None
        if body.password is not None and current.get('source') == 'sso':
            fields.pop('pw', None)
            current = sso_to_local(key, body, fields, by)
            log_change(key, body.model_copy(update={'password': None}), by)
            fields = {}
        if fields:
            change_user(key, fields, unset, body, by)
        if body.ssoLink is not None:
            change_link(key, body.ssoLink, current.get('ssoLink'), by)
        return admin_row(users.get(key), clock())

    def change_link(key: str, allow: bool, held, by: str) -> None:
        try:
            after = users.set_sso_link(key, allow, by)
        except InvalidUser as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.text) from e
        finally:
            user_cache.pop(key, None)
            mcp_auth.forget(key)
        if after.get('ssoLink') != held:
            store.add_activity(ACCESS_KIND, by, msg('access.sso_linked', email=key, days=LINK_PENDING_S // 86400) if allow else msg('access.sso_unlinked', email=key))

    def change_user(key: str, fields: dict, unset: tuple[str, ...], body: UserPatch, by: str) -> None:
        if leaves_admin(fields) and users.is_last_admin(key):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.last_admin'))
        only = {'roleFrom': {'$nin': list(IDP_SOURCES)}} if 'role' in fields else None
        try:
            if users.guarded_update(key, fields, unset, only=only) is None:
                if only is not None and users.get(key) is not None:
                    raise HTTPException(status.HTTP_409_CONFLICT, msg('access.role_from_idp'))
                raise HTTPException(status.HTTP_404_NOT_FOUND, msg('access.unknown_user'))
        except LastAdmin:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.last_admin')) from None
        finally:
            user_cache.pop(key, None)
            mcp_auth.forget(key)
        log_change(key, body, by)

    def log_change(key: str, body: UserPatch, by: str) -> None:
        if body.role is not None:
            store.add_activity(ACCESS_KIND, by, msg('access.user_role', email=key, role=body.role))
        if body.disabled is not None:
            store.add_activity(ACCESS_KIND, by, msg('access.user_disabled' if body.disabled else 'access.user_enabled', email=key))
        if body.password is not None:
            store.add_activity(ACCESS_KIND, by, msg('access.user_password', email=key))

    @app.get('/api/admin/tokens')
    def get_admin_tokens(by: Annotated[str, Depends(require_admin)]) -> list[dict]:
        return [row | {'endedByStop': ended_by_stop(row)} for row in api_tokens.list()]

    def session_source(request: Request, authorization: str | None) -> str | None:
        sessions = get_sessions()
        if bearer_token(authorization) or sessions is None:
            return None
        payload = sessions.unseal(request.cookies.get(SESSION_COOKIE), SESSION_KIND)
        return payload.get('src') if payload else None

    @app.post('/api/admin/tokens', status_code=status.HTTP_201_CREATED)
    def post_admin_token(body: TokenIn, request: Request, by: Annotated[str, Depends(require_admin)], authorization: Annotated[str | None, Header()] = None) -> dict:
        actor: Actor = request.state.actor
        try:
            token, doc = api_tokens.create(body.name, body.role, actor.creator, via='sso' if 'sso' in (actor.via, session_source(request, authorization)) else None, expires_days=body.expiresDays)
        except TokenNameError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg(e.key)) from e
        except InvalidUser as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.text) from e
        store.add_activity(ACCESS_KIND, by, msg('access.token_created', name=doc['name'], role=doc['role']))
        return doc | {'token': token}

    @app.delete('/api/admin/tokens/{token_id}', status_code=status.HTTP_204_NO_CONTENT)
    def delete_admin_token(token_id: str, by: Annotated[str, Depends(require_admin)]) -> Response:
        try:
            doc = api_tokens.revoke(token_id, by)
        except UnknownToken as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('access.unknown_token')) from e
        if doc is not None:
            store.add_activity(ACCESS_KIND, by, msg('access.token_revoked', name=doc['name']))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get('/api/admin/settings')
    def get_admin_settings(by: Annotated[str, Depends(require_admin)]) -> dict:
        return {'items': live.entries(), 'rev': live.tunables.rev(), 'refreshSeconds': live.every}

    @app.put('/api/admin/settings')
    def put_admin_setting(body: AdminSettingIn, request: Request, by: Annotated[str, Depends(require_admin)], authorization: Annotated[str | None, Header()] = None) -> dict:
        return admin_change(body.key, body.value, by, way_back_in(request, authorization))

    @app.delete('/api/admin/settings')
    def delete_admin_setting(key: str, request: Request, by: Annotated[str, Depends(require_admin)], authorization: Annotated[str | None, Header()] = None) -> dict:
        return admin_change(key, None, by, way_back_in(request, authorization))

    def sso_tunables() -> dict:
        known = live.known()
        return {name: known[f'sso.{name}'] for name in SSO_FIELDS if name != SSO_SECRET_FIELD}

    def sso_layer(t, entry: dict) -> str:
        return live.label_of(t) if entry['default'] is None or entry['base'] != entry['default'] else 'default'

    def sso_view() -> dict:
        live.refresh(force=True)
        config = signon.config()
        values, bases = {}, {}
        for name, t in sso_tunables().items():
            entry = live.entry(t)
            values[name] = {'value': entry['value'], 'source': entry['source'], 'from': entry.get('from')}
            bases[name] = sso_layer(t, entry)
        switch_t = live.known()[SSO_SWITCH_KEY]
        held = live.entry(switch_t)
        secret = sso_secret.status()
        return {'enabled': config.on, 'active': config.active, 'problem': config.problem(), 'redirectUri': config.redirect_uri, 'provider': signon.provider(config).status(),
                'values': values, 'bases': bases, 'secret': secret,
                'switch': {'value': held['value'], 'source': held['source'], 'from': held.get('from'), 'base': sso_layer(switch_t, held), 'baseValue': bool(live.base.sso_on)},
                'roleMapInUse': [] if (settings.sso_role_map or '').strip() else [{'value': value, 'role': role} for value, role in role_map(settings)],
                'mcpBase': mcp_auth.base_url.rstrip('/'),
                'icon': sso_icon.status(),
                'button': {'defaultText': msg('login.sso'), 'textMax': ssobutton.TEXT_MAX, 'iconMax': SSO_ICON_MAX, 'marks': ssobutton.rules(), 'branded': ssobutton.branded()},
                'secretNote': msg('sso.secret_unreadable_note') if secret['unreadable'] and not secret['set'] else None}

    def sso_proposal(values: dict[str, str | None], enabled: bool | None, testing: bool) -> tuple[SsoConfig, dict[str, str | None], str | None]:
        live.refresh(force=True)
        overrides = {k: v['value'] for k, v in live.overrides.items()}
        writes: dict[str, str | None] = {}
        for name, t in sso_tunables().items():
            if name not in values:
                continue
            raw = values[name]
            if raw is None:
                if overrides.pop(t.key, None) is not None:
                    writes[t.key] = None
                continue
            try:
                text = stored_text(parse_tunable(t, raw, live.base))
            except TunableInvalid as e:
                raise FieldRefused(name, e.text) from None
            overrides[t.key] = text
            if text != live.now_text(t):
                writes[t.key] = text
        if enabled is not None:
            overrides[SSO_SWITCH_KEY] = 'true' if enabled else 'false'
        typed = values.get(SSO_SECRET_FIELD)
        if isinstance(typed, str) and typed and not typed.strip():
            raise FieldRefused(SSO_SECRET_FIELD, msg('sso.secret_invalid'))
        typed = typed.strip() if isinstance(typed, str) else None
        if typed and len(typed) > SSO_SECRET_MAX:
            raise FieldRefused(SSO_SECRET_FIELD, msg('sso.secret_invalid'))
        held = sso_secret.current()
        secret = (typed or held) if testing else held if typed is None else typed
        unreadable = not secret and sso_secret.status()['unreadable'] and (testing or typed is None)
        config = SsoConfig.of(apply_tunables(live.base, overrides, live.extra()), secret, unreadable=unreadable)
        return config, writes, None if testing else typed

    def note_sso_saved(saved: list[str], secret_changed: bool, by: str) -> None:
        fields = sso_tunables()
        names = [name for name in SSO_FIELDS if (secret_changed if name == SSO_SECRET_FIELD else f'sso.{name}' in saved)]
        if names:
            store.add_activity(ACCESS_KIND, by, msg('sso.saved', fields=', '.join(msg('sso.secret_label') if name == SSO_SECRET_FIELD else fields[name].label for name in names)))
            log.info('single sign-on settings saved by %s: %s', by, ', '.join(names))

    @app.get('/api/admin/sso')
    def get_admin_sso(by: Annotated[str, Depends(require_admin)]) -> dict:
        return sso_view()

    @app.put('/api/admin/sso')
    def put_admin_sso(body: SsoIn, request: Request, by: Annotated[str, Depends(require_admin)], authorization: Annotated[str | None, Header()] = None) -> dict:
        config, writes, secret = sso_proposal(body.values, body.enabled, testing=False)
        problem = config.problem()
        if body.enabled and problem:
            raise FieldRefused('enabled', problem)
        owner = sso_secret.status()['from']
        if secret is not None and owner is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('sso.secret_source_owned', source=owner))
        was_on = bool(settings.sso_on)
        switch = None if body.enabled is None or body.enabled == was_on else body.enabled
        if switch is False:
            may_switch_sso_off(way_back_in(request, authorization))
        saved: list[str] = []
        secret_changed = False
        try:
            for key, value in writes.items():
                live.set(key, value, by)
                saved.append(key)
            if secret is not None:
                try:
                    if secret:
                        if secret != sso_secret.current():
                            sso_secret.set(secret, by)
                            secret_changed = True
                    else:
                        secret_changed = sso_secret.clear(by)
                except SecretOwned as e:
                    raise HTTPException(status.HTTP_409_CONFLICT, msg('sso.secret_source_owned', source=e.source)) from None
                except InvalidSecret:
                    raise FieldRefused(SSO_SECRET_FIELD, msg('sso.secret_invalid')) from None
            if switch is not None:
                live.set(SSO_SWITCH_KEY, 'true' if switch else 'false', by)
        finally:
            mcp_auth.clear()
            if was_on and not settings.sso_on:
                try:
                    end_sso_sessions()
                except PyMongoError as e:
                    log.warning('the single sign-on stop was not written, the next settings refresh writes it: %s', kind(e))
            try:
                note_sso_saved(saved, secret_changed, by)
            except PyMongoError as e:
                log.warning('the single sign-on save was not written to the activity: %s', kind(e))
        if switch is not None and bool(settings.sso_on) == switch:
            store.add_activity(ACCESS_KIND, by, msg('sso.switched_on' if switch else 'sso.switched_off'))
            log.info('single sign-on switched %s by %s', 'on' if switch else 'off', by)
        return sso_view()

    @app.put('/api/admin/sso/icon')
    async def put_admin_sso_icon(request: Request, by: Annotated[str, Depends(require_admin)]) -> dict:
        declared = request.headers.get('content-length', '')
        if declared.isdigit() and int(declared) > SSO_ICON_MAX:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, msg('sso.icon_too_big', max=SSO_ICON_MAX // 1024))
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > SSO_ICON_MAX:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, msg('sso.icon_too_big', max=SSO_ICON_MAX // 1024))
            raw += chunk
        try:
            kind_of = await run_in_threadpool(icon_check, bytes(raw))
        except IconRefused as e:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE if e.key == 'sso.icon_too_big' else status.HTTP_422_UNPROCESSABLE_CONTENT,
                                msg(e.key, max=SSO_ICON_MAX // 1024) if e.key == 'sso.icon_too_big' else msg(e.key)) from None
        try:
            await run_in_threadpool(sso_icon.set, bytes(raw), kind_of, by)
        except PyMongoError as e:
            log.warning('the single sign-on button icon was not stored: %s', kind(e))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('sso.icon_unavailable')) from None
        try:
            await run_in_threadpool(store.add_activity, ACCESS_KIND, by, msg('sso.icon_saved', type=kind_of, size=len(raw)))
        except PyMongoError as e:
            log.warning('the single sign-on button icon upload was not written to the history: %s', kind(e))
        log.info('single sign-on button icon uploaded by %s (%s, %d bytes)', by, kind_of, len(raw))
        return await run_in_threadpool(sso_view)

    @app.delete('/api/admin/sso/icon')
    def delete_admin_sso_icon(by: Annotated[str, Depends(require_admin)]) -> dict:
        try:
            gone = sso_icon.clear()
        except PyMongoError as e:
            log.warning('the single sign-on button icon was not removed: %s', kind(e))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('sso.icon_unavailable')) from None
        if gone:
            log.info('single sign-on button icon removed by %s', by)
            try:
                store.add_activity(ACCESS_KIND, by, msg('sso.icon_removed'))
            except PyMongoError as e:
                log.warning('the single sign-on button icon removal was not written to the history: %s', kind(e))
        return sso_view()

    @app.post('/api/admin/sso/test')
    def post_admin_sso_test(body: SsoTestIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        config, _, _ = sso_proposal(body.values, None, testing=True)
        try:
            return signon.test(config)
        except SsoTestBusy:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('sso.test_busy')) from None
        except Exception as e:
            log.warning('the sign-in connection test could not run: %s', kind(e))
            return {'ok': False, 'summary': msg('sso.test.unavailable'), 'ms': 0, 'checks': []}

    @app.get('/api/admin/notices')
    def get_admin_notices(by: Annotated[str, Depends(require_admin)]) -> dict:
        items = []
        now = utc_now()
        sighting = store.proxy_sighting()
        seen = stamp_time(sighting['at']) if sighting else None
        if seen is not None and seen > now:
            store.clamp_proxy(sighting['at'], iso(now))
            seen, sighting = now, {**sighting, 'at': iso(now)}
        if seen is not None and now - seen < dt.timedelta(seconds=DAY_S):
            items.append({'id': 'proxy', 'level': 'warn', 'text': msg('proxy.untrusted', peer=sighting['peer']), 'at': sighting['at']})
        if not base_settings.secret_key:
            items.append({'id': 'secret-key', 'level': 'info', 'text': msg('secrets.generated_key')})
        for label, state in sources.status().items():
            if not state['ok']:
                items.append({'id': f'source:{label}', 'level': 'warn', 'text': msg('settings.source_failed', source=label, error=state['error']), 'at': state['since']})
        if aikey.status()['unreadable']:
            items.append({'id': 'ai-key', 'level': 'warn', 'text': msg('aikey.unreadable')})
        slack_scopes = store.slack_scopes() if slack.enabled else {'missing': []}
        if slack_scopes['missing']:
            items.append({'id': 'slack-scopes', 'level': 'warn', 'text': msg('integrations.slack_scopes', scopes=', '.join(slack_scopes['missing'])), 'at': slack_scopes['at']})
        items += [{'id': f'route-gave-up:{r["id"]}', 'level': 'warn', 'text': msg('routes.gave_up', plugin=r['plugin'], route=r['route'], count=r['count'], tries=TRIES, reason=r['reason'])}
                  for r in routes.gave_up()]
        items += [{'id': f'plugin:{found.id}', 'level': 'warn', 'text': found.reason} for found in registry.loaded() if found.state == 'broken']
        packs = runtime.org_packs()
        items += [{'id': f'org-pack:{p.id}', 'level': 'warn', 'text': msg('orgpack.ignored', name=p.name, used=packs[0].name)} for p in packs[1:]]
        items += [{'id': f'locale:{found.id}:{lang}', 'level': 'warn', 'text': msg('notices.locale_dropped_one' if count == 1 else 'notices.locale_dropped', lang=lang, plugin=found.plugin.name, count=count)}
                  for found in registry.loaded() if found.state == 'ok' and plugin_state.enabled(found.id) for lang, count, _ in locale_drops(found)]
        if any(p.id == MCPTOOLS_ID for p in runtime.active()):
            items += mcptools_routes.notices(mcptools_deps)
        if signon.active():
            provider = signon.status()
            if not provider['ok']:
                items.append({'id': 'sso-provider', 'level': 'warn', 'text': msg('sso.notice.provider'), 'at': provider['since']})
        update = versions.notice()
        if update is not None:
            items.append(update)
        return {'items': items}

    @app.get('/api/admin/features')
    def get_admin_features(by: Annotated[str, Depends(require_admin)]) -> dict:
        return {'edition': features.edition(), 'items': features.view()}

    @app.put('/api/admin/features/{feature_id}')
    def put_admin_feature(feature_id: str, body: FeatureIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        feature = FEATURE_BY_ID.get(feature_id)
        if feature is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('features.unknown', feature=feature_id[:60]))
        try:
            item = features.set(feature_id, body.on, by)
        except Locked as e:
            raise HTTPException(status.HTTP_409_CONFLICT, e.detail) from None
        store.add_activity(ADMIN_KIND, by, msg('features.switched', feature=feature.label, state='on' if body.on else 'off'))
        log.info('feature %s turned %s by %s', feature_id, 'on' if body.on else 'off', by)
        return item

    @app.get('/api/admin/history')
    def get_admin_history(by: Annotated[str, Depends(require_admin)], limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
        return live.tunables.history(limit)

    @app.get('/api/admin/anthropic-key')
    def get_admin_anthropic_key(by: Annotated[str, Depends(require_admin)]) -> dict:
        return aikey.status()

    @app.put('/api/admin/anthropic-key')
    def put_admin_anthropic_key(body: AiKeyIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        try:
            found = aikey.set(body.key, by)
        except InvalidKey:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg('aikey.invalid')) from None
        store.add_activity(ADMIN_KIND, by, msg('aikey.set'))
        log.info('Anthropic API key set in Admin by %s', by)
        return found

    @app.delete('/api/admin/anthropic-key', status_code=status.HTTP_204_NO_CONTENT)
    def delete_admin_anthropic_key(by: Annotated[str, Depends(require_admin)]) -> Response:
        if aikey.clear(by):
            store.add_activity(ADMIN_KIND, by, msg('aikey.cleared'))
            log.info('Anthropic API key cleared in Admin by %s', by)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post('/api/admin/anthropic-key/test')
    def post_admin_anthropic_key_test(body: AiKeyTestIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        try:
            return aikey.test(body.key, settings.investigate_model, lambda: store.add_activity(ADMIN_KIND, by, msg('aikey.test_used')))
        except InvalidKey:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg('aikey.invalid')) from None
        except TestBusy:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('aikey.test_busy')) from None
        except Exception as e:
            log.warning('the Anthropic connection test could not run: %s', kind(e))
            return {'ok': False, 'summary': msg('aikey.test_failed'), 'ms': 0}

    def plugin_found(plugin_id: str):
        found = registry.get(plugin_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('integrations.unknown_plugin'))
        return found

    def plugin_usable(plugin_id: str):
        found = plugin_found(plugin_id)
        if found.state != 'ok':
            raise HTTPException(status.HTTP_409_CONFLICT, found.reason or msg('integrations.unavailable'))
        return found.plugin

    def plugin_loaded(plugin_id: str):
        found = plugin_found(plugin_id)
        if found.plugin is None:
            raise HTTPException(status.HTTP_409_CONFLICT, found.reason or msg('integrations.unavailable'))
        return found.plugin

    def drops_view(found) -> list[dict]:
        return [{'lang': lang, 'count': count, 'text': msg('integrations.locale_dropped_one' if count == 1 else 'integrations.locale_dropped', lang=lang, count=count),
                 'keys': keys[:DROPS_SHOWN], 'more': count - len(keys[:DROPS_SHOWN])}
                for lang, count, keys in locale_drops(found)]

    def plugin_row(found, passes: dict) -> dict:
        origin = ORIGIN_VIEW[found.origin]
        if found.plugin is None:
            return {'id': found.id, 'module': found.module, 'reason': found.reason, 'origin': origin}
        if found.state == 'broken':
            return {'id': found.id, 'module': found.module, 'reason': found.reason, 'enabled': plugin_state.enabled(found.id), 'origin': origin}
        listed = instances.all(found.id)
        return {**describe(found.plugin, registry.icon(found.id) is not None), 'state': found.state, 'reason': found.reason, 'enabled': plugin_state.enabled(found.id),
                'origin': origin, **({} if origin == 'builtin' else {'module': found.module}),
                'localeDrops': drops_view(found) if found.state == 'ok' else [],
                'fields': runtime.fields_of(found.id), 'instances': [i.view() for i in listed], 'health': plugin_health(found.plugin, listed, passes)}

    def nudge_jobs(plugin) -> None:
        if plugin_state.enabled(plugin.id):
            for check in plugin.checks:
                scheduler.nudge(f'{plugin.id}.{check.id}')

    def once_off(plugin, primary: bool, sent, stored: frozenset[str] = frozenset()):
        if sent is None or primary:
            return sent
        once = {c.id for c in plugin.checks if c.once}
        return (frozenset(sent) - once) | (stored & once)

    def change_checks(plugin, name: str, was: frozenset[str], off: frozenset[str], by: str) -> None:
        instances.set_checks(plugin.id, name, off, by)
        now = time.time()
        for check in plugin.checks:
            if check.id in off and check.id not in was:
                store.add_activity(ADMIN_KIND, by, msg('integrations.check_off', plugin=plugin.name, instance=name, check=check.label))
                sweeper.retire(plugin.id, name, now, check.id)
            elif check.id in was and check.id not in off:
                store.add_activity(ADMIN_KIND, by, msg('integrations.check_on', plugin=plugin.name, instance=name, check=check.label))
                sweeper.revive(plugin.id, name, check.id)
                if plugin_state.enabled(plugin.id):
                    scheduler.nudge(f'{plugin.id}.{check.id}')

    @app.get('/api/admin/plugins')
    def get_admin_plugins(by: Annotated[str, Depends(require_admin)]) -> dict:
        passes = store.server_runs()['passes']
        return {'edition': registry.edition(), 'secretsKey': 'env' if base_settings.secret_key else 'generated', 'plugins': [plugin_row(x, passes) for x in registry.loaded()]}

    @app.put('/api/admin/plugins/{plugin_id}')
    def put_admin_plugin(plugin_id: str, body: PluginSwitchIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        plugin = plugin_usable(plugin_id) if body.enabled else plugin_loaded(plugin_id)
        was = plugin_state.enabled(plugin_id)
        runtime.set_enabled(plugin_id, body.enabled, by)
        if body.enabled:
            sweeper.revive(plugin_id, None)
            nudge_jobs(plugin)
        elif plugin_found(plugin_id).state != 'broken' or plugin_id in registry.forced():
            sweeper.retire(plugin_id, None, time.time())
        if was != body.enabled:
            store.add_activity(ADMIN_KIND, by, msg('integrations.plugin_on' if body.enabled else 'integrations.plugin_off', plugin=plugin.name))
        return plugin_row(plugin_found(plugin_id), store.server_runs()['passes'])

    @app.post('/api/admin/plugins/{plugin_id}/instances', status_code=status.HTTP_201_CREATED)
    def post_admin_instance(plugin_id: str, body: InstanceIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        plugin = plugin_usable(plugin_id)
        if plugin_id == MCPTOOLS_ID:
            mcptools_routes.check_create(mcptools_deps, body.values, body.enabled)
        try:
            made = instances.create(plugin_id, body.name, body.values, body.enabled, by, once_off(plugin, not instances.all(plugin_id), body.offChecks))
        except InstanceExists:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('instances.exists', name=body.name)) from None
        except InvalidInstance as e:
            raise FieldRefused(e.field, e.args[0]) from None
        if plugin_id == MCPTOOLS_ID:
            mcptools_routes.after_create(mcptools_deps, made.name)
        store.add_activity(ADMIN_KIND, by, msg('integrations.instance_created', plugin=plugin.name, instance=made.name))
        if made.enabled:
            store.add_activity(ADMIN_KIND, by, msg('integrations.instance_on', plugin=plugin.name, instance=made.name))
            sweeper.revive(plugin_id, made.name, checks=[c.id for c in watched(plugin, made)])
            nudge_jobs(plugin)
        return made.view()

    @app.patch('/api/admin/plugins/{plugin_id}/instances/{name}')
    def patch_admin_instance(plugin_id: str, name: str, body: InstancePatch, by: Annotated[str, Depends(require_admin)]) -> dict:
        plugin = plugin_usable(plugin_id) if body.enabled else plugin_loaded(plugin_id)
        before = instances.fresh(plugin_id, name)
        if before is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('instances.unknown', name=name))
        leader = instances.primary(plugin_id)
        leader_info = leader.info() if leader is not None else None
        notice = None
        if body.primary and not (before.enabled if body.enabled is None else body.enabled):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('instances.primary_disabled', name=name))
        if plugin_id == MCPTOOLS_ID:
            mcptools_routes.check_patch(mcptools_deps, before, body.values or {}, body.enabled)
        try:
            off = once_off(plugin, before.primary, None if body.offChecks is None else checks_off(plugin, body.offChecks), before.off_checks)
            given = {k: v for k, v in (body.values or {}).items() if v is not None}
            if plugin_id == MCPTOOLS_ID:
                given = mcptools_routes.patch_values(mcptools_deps, before, given)
            if given:
                after = instances.update(plugin_id, name, given, by)
                if plugin_id == MCPTOOLS_ID:
                    mcptools_routes.after_patch(mcptools_deps, name, before, after)
                was, now = {**before.values, **before.secrets}, {**after.values, **after.secrets}
                known = {f['key'] for f in runtime.fields_of(plugin_id)}
                changed = sorted(k for k in given if was.get(k) != now.get(k) or k not in known)
                if changed:
                    store.add_activity(ADMIN_KIND, by, msg('integrations.instance_changed', plugin=plugin.name, instance=name, fields=', '.join(changed)))
            if off is not None and off != before.off_checks:
                change_checks(plugin, name, before.off_checks, off, by)
            if body.enabled is not None and body.enabled != before.enabled:
                instances.set_enabled(plugin_id, name, body.enabled, by)
                store.add_activity(ADMIN_KIND, by, msg('integrations.instance_on' if body.enabled else 'integrations.instance_off', plugin=plugin.name, instance=name))
                if body.enabled:
                    sweeper.revive(plugin_id, name, checks=[c.id for c in watched(plugin, instances.get(plugin_id, name))])
                    nudge_jobs(plugin)
                else:
                    sweeper.retire(plugin_id, before.info(), time.time())
            if body.primary:
                if instances.make_primary(plugin_id, name, by):
                    store.add_activity(ADMIN_KIND, by, msg('integrations.instance_primary', plugin=plugin.name, instance=name))
                    if leader is not None and leader_info is not None:
                        sweeper.primary_changed(plugin_id, leader_info, instances.get(plugin_id, name).info(), time.time())
                        notice = msg('instances.primary_moved', plugin=plugin.name, instance=name, old=leader.name)
        except InstanceMissing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('instances.unknown', name=name)) from None
        except SourceOwned as e:
            raise HTTPException(status.HTTP_409_CONFLICT, e.args[0]) from None
        except InvalidInstance as e:
            raise FieldRefused(e.field, e.args[0]) from None
        except Conflict:
            if body.offChecks is None:
                raise
            now_on = instances.get(plugin_id, name)
            stays_off = bool(body.enabled) and not (now_on is not None and now_on.enabled)
            raise HTTPException(status.HTTP_409_CONFLICT, msg('integrations.checks_pending_off' if stays_off else 'integrations.checks_pending', instance=name)) from None
        out: dict = {'instance': instances.get(plugin_id, name).view()}
        if notice:
            out['notice'] = notice
        return out

    @app.delete('/api/admin/plugins/{plugin_id}/instances/{name}', status_code=status.HTTP_204_NO_CONTENT)
    def delete_admin_instance(plugin_id: str, name: str, by: Annotated[str, Depends(require_admin)]) -> Response:
        plugin = plugin_loaded(plugin_id)
        before = instances.fresh(plugin_id, name)
        if before is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('instances.unknown', name=name))
        try:
            instances.delete(plugin_id, name, by)
        except InstanceMissing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('instances.unknown', name=name)) from None
        except SourceOwned as e:
            raise HTTPException(status.HTTP_409_CONFLICT, e.args[0]) from None
        heir = instances.primary(plugin_id)
        if before.primary and heir is not None and heir.name != name:
            sweeper.primary_changed(plugin_id, before.info(), heir.info(), time.time())
        else:
            sweeper.retire(plugin_id, before.info(), time.time())
        if plugin_id == MCPTOOLS_ID:
            mcptools_routes.after_delete(mcptools_deps, name)
        store.add_activity(ADMIN_KIND, by, msg('integrations.instance_deleted', plugin=plugin.name, instance=name))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post('/api/admin/plugins/{plugin_id}/test')
    def post_admin_plugin_test(plugin_id: str, body: PluginTestIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        plugin = plugin_usable(plugin_id)
        try:
            tested = runtime.connection_test(plugin, body.name, body.values, TEST_TIMEOUT_S, time.time(), lambda: store.add_activity(ADMIN_KIND, by, msg('integrations.test_used_secret', plugin=plugin.name, instance=body.name)),
                                             off_checks=body.offChecks,
                                             tester=mcptools_routes.tester(mcptools_deps) if plugin_id == MCPTOOLS_ID else None)
            if plugin.outbound is not None and body.name and tested.get('ok') is True:
                routes.reset(plugin.id, body.name, body.values.get(plugin.outbound.link_field) if plugin.outbound.link_field else None)
            return tested
        except InvalidInstance as e:
            raise FieldRefused(e.field, e.args[0]) from None
        except InstanceMissing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('instances.unknown', name=body.name)) from None
        except Exception as e:
            log.warning('%s', msg('integrations.test_failed_log', plugin=plugin_id, error=kind(e)))
            return {'ok': False, 'summary': msg('integrations.test_failed', error=kind(e)), 'ms': 0}

    mcptools_routes.mount(app, mcptools_deps, require_admin)
    promqlroutes.mount(app, custom_checks, require_admin, lambda by, text: store.add_activity(ADMIN_KIND, by, text), lambda: scheduler.nudge(CUSTOM_CHECKS))

    @app.get('/api/jira/search')
    def jira_search(q: Annotated[str, Query(max_length=120)], by: Annotated[str, Depends(require_operator)], type: Annotated[str, Query(max_length=10)] = '',
                    project: Annotated[str, Query(max_length=20)] = '') -> list[dict]:
        if not jira.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Jira is not set up on this service')
        wanted = project.strip().upper()
        if wanted and not PROJECT_KEY.fullmatch(wanted):
            raise HTTPException(status_code=422, detail='project is a Jira project key such as OPS')
        jql = ticket_jql(q, type == 'epic', wanted)
        if jql is None:
            return []
        try:
            issues = jira.search(jql, ['summary', 'status'] + ([settings.jira_developer_field] if settings.jira_developer_field else []), TICKET_HITS)
        except NoAccess as e:
            log.warning('jira search for the Take field refused: %s', kind(e))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, text_of(e)) from None
        except Exception as e:
            log.warning('jira search for the Take field failed: %s', kind(e))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, 'Jira did not answer') from None
        return [{'key': i.get('key'), 'summary': (i.get('fields') or {}).get('summary'), 'status': ((i.get('fields') or {}).get('status') or {}).get('name'),
                 'developer': ((i.get('fields') or {}).get(settings.jira_developer_field) or {}).get('displayName') if settings.jira_developer_field else None} for i in issues if i.get('key')]

    @app.get('/api/requests')
    def get_requests(limit: Annotated[int, Query(ge=1, le=200)] = 100) -> list[dict]:
        return store.requests(limit)

    @app.get('/api/investigations/{run_id}')
    def get_investigation(run_id: str) -> dict:
        return store.investigation(run_id)

    @app.post('/api/investigations/{run_id}/proposal')
    def post_proposal(run_id: str, by: Annotated[str, Depends(require_operator)]) -> dict:
        try:
            return proposals.confirm(run_id, by)
        except NoProposal:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('proposal.none')) from None
        except ProposalClosed as e:
            raise HTTPException(status.HTTP_409_CONFLICT, e.text) from None
        except ProposalUnavailable as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, e.text) from None
        except Exception as e:
            log.warning('proposal of run %s not confirmed: %s', run_id, kind(e))
            return {'ok': False, 'summary': msg('proposal.failed'), 'outcome': 'failed'}

    @app.get('/api/investigator/status')
    def get_investigator_status() -> dict:
        board = investigator.board(time.time())
        since = iso(utc_now() - dt.timedelta(seconds=ASK_WINDOW_S))
        board['limits'].update(perPersonCap=settings.slack_user_ask_cap, pageAsks=sum(store.count_actions('ask', since, source=s) for s in PAGE_ASK_SOURCES), pageAskCap=settings.page_ask_cap)
        return board

    @app.get('/api/investigator/usage')
    def get_investigator_usage(days: Annotated[int, Query(ge=1, le=30)] = 7) -> dict:
        return store.investigation_usage(iso(utc_now() - dt.timedelta(days=days)))

    @app.get('/api/tool-usage')
    def get_tool_usage(days: Annotated[int, Query(ge=1, le=30)] = 7) -> dict:
        since = iso(utc_now() - dt.timedelta(days=days))
        usage = toolusage.summary(store.tool_usage_runs(since), days, since, store.check_runs_since(since), investigator.sources.plugin_tools)
        on = bool(settings.probe)
        return toolusage.with_status(usage, ToolSet(runtime), store.last_probe() if on else None) | {'probe': on}

    @app.get('/api/costs')
    def get_costs(_: Annotated[str, Depends(require_login)], days: Annotated[int, Query(ge=1, le=30)] = 7) -> dict:
        today = clocks.day(time.time())
        rows = store.ledger_since(costs.window(days, today)[0])
        meta = store.run_costs_meta({r['runId'] for r in rows if r.get('runId')})
        return costs.summary(rows, meta, today, days, store.spent(today), settings.investigate_daily_usd)

    @app.get('/api/slack-intake')
    def get_slack_intake(_: Annotated[str, Depends(require_operator)], limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict]:
        return store.list_intake(limit)

    @app.get('/api/operators/check')
    def check_operator(email: Annotated[str, Query(min_length=3, max_length=320)], _: Annotated[str, Depends(require_operator)]) -> dict:
        given = email.strip()
        try:
            principal, operator = operators.access_of(given)
        except OperatorUnknown as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f'role lookup failed: {e.text}') from None
        return {'email': given, 'operator': operator, 'access': principal is not None, 'principal': principal}

    def reads_access(request: Request, authorization: str | None) -> bool:
        try:
            return acting(request, authorization).role == 'admin'
        except HTTPException:
            return False

    @app.get('/api/activity')
    def get_activity(request: Request, since: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                     authorization: Annotated[str | None, Header()] = None) -> list[dict]:
        return store.list_activity(since, limit, () if reads_access(request, authorization) else (ACCESS_KIND,))

    def small_talk_info() -> dict:
        return talk.rules() | {'usedThisHour': store.count_small_talk(iso(utc_now() - dt.timedelta(seconds=SMALL_TALK_WINDOW_S)))}

    def small_talk_admin() -> dict:
        return small_talk_info() | {'custom': store.small_talk_rules() is not None,
                                    'defaults': {k: SMALL_TALK_DEFAULTS[k] for k in SMALL_TALK_FIELDS}}

    def small_talk_saved(before: dict, by: str, kind: str) -> dict:
        after = talk.rules()
        shown = {'enabled': lambda v: 'on' if v else 'off', 'hourlyCap': str, 'rules': str}
        changed = [f for f in SMALL_TALK_FIELDS if before.get(f) != after.get(f)]
        for field in changed:
            live.tunables.write_change(SMALL_TALK_FIELDS[field], shown[field](before.get(field)), shown[field](after.get(field)), by, talk=True)
        if not changed:
            return small_talk_admin()
        line = f"Small talk {'on' if after['enabled'] else 'off'} · {after['hourlyCap']} an hour · rules {len(after['rules'])} characters" + (' · back to the default' if store.small_talk_rules() is None else '')
        store.add_activity(kind, by, line)
        log.info('small talk rules changed by %s: %s', by, line)
        return small_talk_admin()

    @app.get('/api/small-talk')
    def get_small_talk() -> dict:
        return small_talk_info()

    @app.put('/api/small-talk', dependencies=writer)
    def put_small_talk(body: SmallTalkIn, by: Annotated[str, Depends(require_machine)]) -> dict:
        before = talk.rules()
        store.set_small_talk_rules(body.model_dump() | {'updatedAt': iso(utc_now()), 'updatedBy': by})
        small_talk_saved(before, by, 'switch')
        return small_talk_info()

    @app.get('/api/admin/small-talk')
    def get_admin_small_talk(by: Annotated[str, Depends(require_admin)]) -> dict:
        return small_talk_admin()

    @app.put('/api/admin/small-talk')
    def put_admin_small_talk(body: SmallTalkIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        before = talk.rules()
        store.set_small_talk_rules(body.model_dump() | {'updatedAt': iso(utc_now()), 'updatedBy': by})
        return small_talk_saved(before, by, 'admin')

    @app.delete('/api/admin/small-talk')
    def delete_admin_small_talk(by: Annotated[str, Depends(require_admin)]) -> dict:
        before = talk.rules()
        store.clear_small_talk_rules()
        return small_talk_saved(before, by, 'admin')

    @app.get('/api/gate-examples')
    def get_gate_examples() -> dict:
        return gate.examples()

    @app.put('/api/gate-examples', dependencies=writer)
    def put_gate_examples(body: GateExamplesIn, by: Annotated[str, Depends(require_machine)]) -> dict:
        store.set_gate_examples({'examples': body.examples, 'updatedAt': iso(utc_now()), 'updatedBy': by})
        line = f'Gate examples · {len(body.examples)} characters'
        store.add_activity('switch', by, line)
        log.info('gate examples changed by %s: %s', by, line)
        return gate.examples()

    @app.get('/api/postdeploy-ignore')
    def get_postdeploy_ignore() -> dict:
        return ignore_doc(store)

    @app.put('/api/postdeploy-ignore', dependencies=writer)
    def put_postdeploy_ignore(body: PostdeployIgnoreIn, by: Annotated[str, Depends(require_machine)]) -> dict:
        services = list(dict.fromkeys(body.services))
        store.set_postdeploy_ignore({'services': services, 'updatedAt': iso(utc_now()), 'updatedBy': by})
        line = f"Deploy check ignores {len(services)} services: {', '.join(services) or 'none'}"
        store.add_activity('switch', by, line[:300])
        log.info('deploy check ignore list changed by %s: %s', by, line)
        return ignore_doc(store)

    @app.get('/api/self-review')
    def get_self_review() -> dict:
        return {'reviews': store.self_reviews()}

    @app.put('/api/self-review', dependencies=writer)
    def put_self_review(body: SelfReviewIn, by: Annotated[str, Depends(require_machine)]) -> dict:
        org = runtime.org()
        domains = settings.email_domains + org['email_domains']
        refused = [e for e in body.dm if not email_allowed(e, domains, org['email_closed'])]
        if refused:
            raise HTTPException(status_code=422, detail=msg('org.email_closed') if org['email_closed'] else 'dm addresses must be in: ' + ', '.join(domains))
        return save_review(body.text, by, list(dict.fromkeys(body.dm)))

    @app.post('/api/self-review/run', dependencies=writer, status_code=status.HTTP_202_ACCEPTED)
    def run_self_review() -> dict:
        if not features.on('self-review'):
            raise FeatureOff('self-review')
        if not self_review.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'the self-review is not set up (no Anthropic key, no self project or log index, or self-review.hour is -1)')
        return {'started': self_review.kick()}

    @app.put('/api/investigate')
    def put_investigate(body: InvestigateIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        store.set_investigator_stop(body.stopped, by)
        store.add_activity('switch', by, 'Pause' if body.stopped else 'Resume')
        log.info('pod investigator %s by %s', 'stopped' if body.stopped else 'resumed', by)
        return investigator.info()

    @app.put('/api/state', dependencies=writer)
    def put_state(state: Annotated[dict, Body()], background: BackgroundTasks, x_base_rev: Annotated[int | None, Header()] = None) -> dict:
        if any(not isinstance(a, dict) or not a.get('key') for a in state.get('anomalies') or []):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, 'every anomaly needs a key')
        rev, pushed_before = store.save_state(state, x_base_rev)
        if (pusher.enabled or routes.enabled) and pushed_before is not None:
            pushed = new_pushes(pushed_before, state.get('anomalies') or [], time.time())
            if pushed and pusher.enabled:
                background.add_task(pusher.send, pushed)
            if pushed:
                routes.later([], time.time(), pushed)
        return {'rev': rev}

    @app.post('/api/assessments', dependencies=writer)
    def post_assessment(body: Assessment) -> dict:
        code = None if body.code is None else [{k: v for k, v in c.model_dump(mode='json').items() if k not in FIX_FIELDS} for c in body.code]
        return {'rev': store.assess(body.key, body.suspects, body.recommend, code)}

    @app.post('/api/notes', dependencies=writer)
    def post_note(body: Note) -> dict:
        return {'rev': store.note(body.key, body.text)}

    @app.get('/api/actions')
    def get_actions(action_status: Annotated[ActionStatus | None, Query(alias='status')] = None, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict]:
        return [a for a in store.list_actions(action_status, limit) if a.get('source') != 'chat']

    @app.post('/api/actions', status_code=status.HTTP_201_CREATED)
    def post_action(body: ActionIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        return add_action(body, by)

    def add_action(body: ActionIn, by: str, via: str | None = None) -> dict:
        action = body.model_dump(exclude_none=True)
        action.setdefault('target', body.key)
        if body.type != 'assess':
            action.pop('reportId', None)
        if body.type != 'resolve':
            action.pop('kind', None)
            action.pop('ref', None)
        action['requestedBy'] = by
        if via:
            action['via'] = via
        if body.type in INVESTIGATOR_WORK and not features.on('investigator'):
            raise FeatureOff('investigator')
        if body.type == 'postdeploy' and not features.on('postdeploy'):
            raise FeatureOff('postdeploy')
        if body.type == 'ask':
            question = (body.text or '').strip()
            if not question:
                raise HTTPException(status_code=422, detail='an ask needs the question in text')
            if len(question) > settings.chat_max_chars:
                raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=f'a question is at most {settings.chat_max_chars} characters (Admin \u203a Investigator, Chat message length)')
            if sum(store.count_actions('ask', iso(utc_now() - dt.timedelta(seconds=ASK_WINDOW_S)), source=s) for s in PAGE_ASK_SOURCES) >= settings.page_ask_cap:
                raise HTTPException(status_code=429, detail=f'{settings.page_ask_cap} asks an hour is the cap (Admin \u203a Investigator, Page asks per hour); try again later')
            action['cards'] = [k for k in dict.fromkeys(body.cards or []) if k]
            said = page_topic_check(question, action['cards'], by)
            action['text'] = question
            action['label'] = (body.label or question)[:300]
            action['source'] = 'api' if is_token(by) else 'page'
            if said is not None:
                return store.add_action(action | {'smallTalk': True, 'usd': round(said.usd, 6)}, 'done', said.text)
        if body.type == 'assess':
            report_id = (body.reportId or '').strip()
            if not report_id:
                raise HTTPException(status_code=422, detail='an assess needs reportId')
            try:
                report = store.deploy_report(report_id)
            except NotFound as e:
                raise HTTPException(status_code=404, detail=e.text) from e
            if any(a.get('type') == 'assess' and a.get('reportId') == report_id for st in ('requested', 'processing') for a in store.list_actions(st, 500)):
                raise HTTPException(status_code=409, detail='an assessment of this report is already queued or running')
            if store.count_actions('assess', iso(utc_now() - dt.timedelta(seconds=ASK_WINDOW_S)), source='page') >= settings.page_ask_cap:
                raise HTTPException(status_code=429, detail=f'{settings.page_ask_cap} re-assessments an hour is the cap (Admin \u203a Investigator, Page asks per hour); try again later')
            action.update({'reportId': report_id, 'key': f'report:{report_id}', 'target': f'report:{report_id}', 'source': 'page',
                           'label': f"Re-assess deploy report: {report.get('summary') or report_id}"[:300]})
        if body.type not in page_actions.INSTANT:
            return store.add_action(action)
        try:
            return store.add_action(action, 'done', apply_card_action(action))
        except (NotFound, page_actions.Refused) as e:
            return store.add_action(action, 'failed', e.text[:300])

    @app.patch('/api/actions/{action_id}')
    def patch_action(action_id: str, body: ActionPatch, _: Annotated[str, Depends(require_operator)]) -> dict:
        updated = store.update_action(action_id, body.status, body.result, body.expect, body.report)
        if updated.get('type') == 'ask' and updated.get('source') == 'slack' and body.status == 'cancelled':
            try:
                replier.reply(updated, 'stopped', '')
            except Exception as e:
                log.error('slack reply for cancelled ask %s failed: %s', action_id, kind(e))
        return updated

    @app.post('/api/slack-posts', status_code=status.HTTP_201_CREATED)
    def post_to_slack(body: SlackPostIn, request: Request, by: Annotated[str, Depends(require_operator)]) -> dict:
        if not slack.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Slack is not set up on this service yet')
        try:
            return send_anomaly_to_slack(body.key, by, str(request.base_url))
        except SlackError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f'Slack refused the message: {reason_of(e)}') from e

    @app.post('/api/slack-holds', status_code=status.HTTP_201_CREATED)
    def post_slack_hold(body: SlackHoldIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        held = store.hold_anomaly_auto_send(body.key, by)
        if held is None:
            raise HTTPException(status.HTTP_409_CONFLICT, 'nothing to hold: the auto-send already went out or was never armed')
        log.info('auto-send held on %s by %s', body.key, by)
        return {'autoSend': held}

    @app.post('/api/slack-updates', status_code=status.HTTP_201_CREATED)
    def post_slack_update(body: SlackUpdateIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        if not slack.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Slack is not set up on this service yet')
        text = body.text.strip()
        if not text:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, 'the update is empty')
        try:
            return slack_thread_update(body.key, text, by)
        except NoThread as e:
            raise HTTPException(status.HTTP_409_CONFLICT, e.text) from e
        except SlackError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f'Slack refused the message: {reason_of(e)}') from e

    @app.post('/api/routes/send')
    def post_routes_send(body: RouteSendIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        try:
            return routes.send(body.key, body.route, by)
        except RouteRefused as e:
            raise HTTPException(e.status, e.text) from None

    @app.put('/api/slack-drafts')
    def put_slack_draft(body: SlackDraftIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        rev, draft, auto = store.slack_draft(body.key, body.text.strip(), by)
        log.info('slack draft for %s %s by %s', body.key, 'saved' if draft else 'cleared', by)
        return {'rev': rev, 'slackDraft': draft, 'autoSend': auto}

    @app.get('/api/deploy-reports')
    def get_deploy_reports(limit: Annotated[int, Query(ge=1, le=200)] = 30) -> list[dict]:
        return store.list_deploy_reports(limit)

    @app.post('/api/deploy-reports', status_code=status.HTTP_201_CREATED, dependencies=writer)
    def post_deploy_report(body: DeployReportIn) -> dict:
        return store.add_deploy_report(body.model_dump(exclude_none=True))

    @app.delete('/api/deploy-reports/{report_id}', status_code=status.HTTP_204_NO_CONTENT, dependencies=writer)
    def delete_deploy_report(report_id: str) -> None:
        store.delete_deploy_report(report_id)

    @app.put('/api/deploy-reports/{report_id}/brief', dependencies=writer)
    def put_report_brief(report_id: str, body: ReportBriefIn) -> dict:
        return {'brief': store.report_brief(report_id, body.model_dump(exclude_none=True))}

    @app.put('/api/deploy-reports/{report_id}/slack-draft')
    def put_report_slack_draft(report_id: str, body: ReportSlackDraftIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        saved = store.report_slack_draft(report_id, body.text.strip(), by)
        log.info('slack draft for report %s %s by %s', report_id, 'saved' if saved['slackDraft'] else 'cleared', by)
        return saved

    @app.post('/api/deploy-reports/{report_id}/slack-hold', status_code=status.HTTP_201_CREATED)
    def post_report_slack_hold(report_id: str, by: Annotated[str, Depends(require_operator)]) -> dict:
        held = store.hold_report_auto_send(report_id, by)
        if held is None:
            raise HTTPException(status.HTTP_409_CONFLICT, 'nothing to hold: the auto-send already went out or was never armed')
        log.info('auto-send held on report %s by %s', report_id, by)
        return {'autoSend': held}

    @app.post('/api/deploy-reports/{report_id}/slack-posts', status_code=status.HTTP_201_CREATED)
    def post_report_to_slack(report_id: str, request: Request, by: Annotated[str, Depends(require_operator)]) -> dict:
        if not slack.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Slack is not set up on this service yet')
        try:
            return send_report_to_slack(report_id, by, str(request.base_url))
        except SlackError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f'Slack refused the message: {reason_of(e)}') from e

    @app.get('/api/deploys')
    def get_deploys(days: Annotated[int, Query(ge=1, le=3650)] = 30) -> list[dict]:
        skip = ignored(store)
        rows = [d | {'ignored': True} if d.get('svc') in skip else d for d in store.list_deploys(days)] + runtime.releases(store.state_section, time.time() - days * 86400)
        return sorted(rows, key=lambda d: d.get('at') or '', reverse=True)

    @app.post('/api/deploys', dependencies=writer)
    def post_deploys(body: Annotated[list[DeployIn], Body(max_length=1000)], background: BackgroundTasks) -> dict:
        docs = [d.model_dump(mode='json', exclude_none=True) for d in body]
        saved = store.add_deploys(docs)
        if enricher.enabled and docs:
            background.add_task(enrich_posted, [(d['ns'], d['svc'], d['toImage']) for d in docs])
        return saved

    @app.delete('/api/deploys/{deploy_id}', status_code=status.HTTP_204_NO_CONTENT, dependencies=writer)
    def delete_deploy(deploy_id: str) -> None:
        store.delete_deploy(deploy_id)

    def chat_user(cookie: str | None) -> dict | None:
        user = user_of(cookie)
        return {'login': user.name, 'operator': bool(user.operator)} if user else None

    def chat_card(ref: str | None) -> dict | None:
        ref = str(ref or '')
        try:
            card = store.anomaly(store.key_of_number(int(ref)) if ref.isdigit() else ref)
        except NotFound:
            return None
        return chat_card_of(card) if card else None

    def chat_refused(e: HTTPException) -> chatui.Refused:
        if isinstance(e, (FeatureOff, NotReady)):
            return chatui.Refused(str(e.detail), 'feature-off' if isinstance(e, FeatureOff) else 'not-ready', cap=settings.chat_daily_cap, limit=settings.chat_max_chars,
                                  feature=e.feature_id if isinstance(e, FeatureOff) else None, needs=' '.join(e.needs) if isinstance(e, NotReady) else '')
        code = {409: 'busy', 404: 'missing', 413: 'long', 422: 'empty', 429: 'cap' if settings.chat_daily_cap else 'off'}.get(e.status_code)
        return chatui.Refused(str(e.detail)[:300].capitalize() + '.', code, cap=settings.chat_daily_cap, limit=settings.chat_max_chars)

    def chat_send(by: str, text: str, chat_id: str, cards: list[str] | None) -> dict:
        try:
            return chat_post(by, text, chat_id, cards, create=True)
        except HTTPException as e:
            raise chat_refused(e) from None

    def chat_apply(by: str, operator: bool, proposed: dict) -> tuple[str, str]:
        kind = CARD_ACTION_TYPES.get(str(proposed.get('action')))
        key = str(proposed.get('key') or '')
        if not operator:
            raise chatui.Refused(msg('access.need_operator'), 'role')
        if not kind or not key:
            raise chatui.Refused('That proposal cannot be applied.', 'bad')
        try:
            body = ActionIn(type=kind, key=key, target=key, label=f'{kind} from chat', hours=proposed.get('hours') if kind == 'mute' else None,
                            ticket=proposed.get('ticket') if kind == 'take' else None,
                            kind=(proposed.get('kind') or 'handled') if kind == 'resolve' else None, ref=proposed.get('ref') if kind == 'resolve' else None)
        except ValidationError:
            raise chatui.Refused('That proposal cannot be applied.', 'bad') from None
        try:
            done = add_action(body, by, 'chat')
        except FeatureOff as e:
            raise chatui.Refused(str(e.detail), 'feature-off', feature=e.feature_id) from None
        if done.get('status') == 'failed':
            reason = str(done.get('result') or '')
            raise chatui.Refused(reason or 'Not applied.', 'failed' if reason else None, reason=reason)
        return ('queued', '') if kind == 'dive' else ('done', str(done.get('result') or kind))

    app.state.chat_bridge = chatui.Bridge(user=chat_user, post=chat_send, chat=lambda by, chat_id: chat_view(found) if (found := store.chat_for(by, chat_id)) else None,
                                          chats=lambda by: store.chats(by, CHAT_LIST), owner=store.chat_owner, shared=store.chat_shared, share=store.share_chat,
                                          hide=store.hide_chat, apply=chat_apply, card=chat_card)
    if chat_at_boot:
        chatui.mount(app, app.state.chat_bridge, sessions_box[0].secret if sessions_box else None, base_url, jira_site, live_projects)

    def investigator_board(now: float) -> dict:
        board = investigator.board(now)
        board['limits'].update(pageAsks=store.count_actions('ask', iso(utc_now() - dt.timedelta(seconds=ASK_WINDOW_S)), source='page'), pageAskCap=settings.page_ask_cap)
        return board

    mcp_bridge = mcpserver.Bridge(store=store, settings=settings, chat=lambda by, text, chat_id, cards, role: chat_post(by, text, chat_id, cards, via=mcpserver.MCP, role=role), chat_view=chat_view,
                                  act=post_action, assign=assigner.assign, card_view=card_view, board=investigator_board,
                                  kick=lambda: investigator.kick(time.time()) if investigator.enabled else None, chat_used=chat_used, base=base_url, feature_on=features.on)
    mcp_manager = mcpserver.mount(app, mcp_bridge, mcp_auth, on=lambda: features.on('mcp'))

    return app
