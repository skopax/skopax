import asyncio
import functools
import hashlib
import hmac
import html
import json
import datetime as dt
import logging
import copy
import math
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Callable, Mapping
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Path as PathParam, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError
from starlette.concurrency import run_in_threadpool

from skopax.cardlink import card_url, chat_url
from skopax.investigator.web import WEBSITE_PLUGIN, own_sites_of
from skopax.cardposts import CardPosts, REDRAW_MARK, drawn_sig
from skopax.codehosts import CodeHosts
from skopax.auth import LOGIN_COOKIE, LOGIN_SECONDS, SESSION_COOKIE, bearer_token, safe_next
from skopax import actions as page_actions
from skopax import __version__, backfill, brand, causes, clock as clocks, costs, drafts, history, information, messages, redact as redaction, tags, toolusage
from skopax.jiralinks import JiraLinks
from skopax.jirafields import ROLES as JIRA_FIELD_ROLES, JiraFields, dialog_view, field_choices
from skopax.backlink import Backlinks
from skopax.kube import Kube
from skopax import locales as core_locales
from skopax import mcpserver
from skopax import mutemarks
from skopax import tracing
from skopax import tunables as tunable_defs
from skopax import sections
from skopax.mcpauth import McpAuth
from skopax.mcptools import PLUGIN_ID as MCPTOOLS_ID
from skopax.mcptools import routes as mcptools_routes
from skopax.mcptools.bridge import ToolBridge, asker_role
from skopax.mcptools.catalog import Catalog as McpCatalog
from skopax.mcptools.oauth import McpOAuth
from skopax import actors
from skopax.actors import BOOTSTRAP_TOKEN as BOOTSTRAP, SYSTEM, is_token
from skopax.apitokens import Actor, ApiTokens, TokenNameError, UnknownToken
from skopax.messages import msg
from skopax.oidc import OidcError
from skopax.operators import OperatorUnknown, Operators
from skopax.postdeploy import JOB_OFFSET as POSTDEPLOY_OFFSET, JOB_PERIOD as POSTDEPLOY_PERIOD, SERVER_BY, PostDeploy
from skopax.attach import Attacher
from skopax.autosend import AutoSend, Fallback, PostNow, fresh as fresh_cards, kept as kept_text, waiting as auto_send_waiting
from skopax.config import EMBED_MODEL, MCP_CLIENT_ID, ConfigurationError, Settings
from skopax.contract import CodeHost, OrgPack, SourceDown
from skopax import engine
from skopax.engine import Applied, PassResult, apply, iso as epoch_iso, new_pushes
from skopax.enrich import Enricher
from skopax.investigator import HELD_OFFSET, HELD_PERIOD, JOB_OFFSET as INVESTIGATE_OFFSET, JOB_PERIOD as INVESTIGATE_PERIOD, Investigator, ReleaseFailed, ReleaseTaken, prompt, public_guard, public_run, select
from skopax.investigator.gate import Gate, GateDown
from skopax.investigator.guard import Guard
from skopax.investigator.pricing import ANTHROPIC, OPENAI, default_model, known as known_model, provider_of
from skopax.investigator.smalltalk import DEFAULTS as SMALL_TALK_DEFAULTS, Reply, SmallTalk, WINDOW_S as SMALL_TALK_WINDOW_S
from skopax.investigator.tools import CORE_TOOLS, FIX_FIELDS, Effects, Sources, allowed_link_hosts, draft_problem
from skopax.investigator.toolset import NoProposal, ProposalClosed, ProposalUnavailable, Proposals, ToolSet
from skopax import chatui
from skopax.chatui import CHAT_ID
from skopax.chatui.text import set_texts as set_chat_texts
from skopax.assign import Assigner, Invalid as AssignInvalid, people_of, slug
from skopax.jiralinks import ticket_projects
from skopax.autoassign import AutoAssign
from skopax.models import email_allowed, NoticesReadIn, NameIn, PasswordIn, AssigneeIn, ChatIn, ChatShareIn, ActionIn, ActionPatch, ActionStatus, Assessment, DeployIn, DeployReportIn, GateExamplesIn, InvestigateIn, PostdeployIgnoreIn, Note, ReportBriefIn, SelfReviewIn, ReportSlackDraftIn, SlackDraftIn, SlackHoldIn, SlackPostIn, SlackUpdateIn, SmallTalkIn, TagIn, TagOwnersIn, TagAutoIn, TagColorIn, BusinessIn, AdminSettingIn, InstanceIn, InstancePatch, PluginSwitchIn, PluginTestIn, FieldSuggestIn, TokenIn, UserIn, UserPatch, FeatureIn, AiKeyIn, AiKeyTestIn, AiConnectionIn, AiConnectionTestIn, VertexConnectionIn, VertexConnectionTestIn
from skopax.selfreview import BY as SELF_REVIEW_BY, SelfReview, self_service, gather as review_facts, review_slot_offset
from skopax.slackhealth import OFFSET as SLACK_POSTS_OFFSET, PASS as SLACK_POSTS, PERIOD as SLACK_POSTS_PERIOD, slack_posts_pass
from skopax.jiraaccess import PASS as JIRA_ACCESS, access_pass as jira_access_pass
from skopax.slacklink import SlackLink
from skopax.kit.promql import Prometheus, client_for
from skopax.kit.promql import CHECK as PROMETHEUS_CHECK, ID as PROMETHEUS_ID
from skopax.tickets import PROJECT_KEY, projects_of
from skopax.users import EMAIL, IDP_SOURCES, LOCAL_AFTER, PW_GEN, NAME_MAX, PASSWORD_MAX, PASSWORD_MIN, ROLES, IdpManaged, InvalidUser, ChangeReverted, ChangeUnsure, LastAdmin, LINK_PENDING_S, NoPasswordAdmin, UserExists, Users, admin_row, mcp_mark, clean_email, clean_name, clean_password, clean_role, cut_admits, leaves_admin, link_admits
from skopax.settingsources import Follower, SourceSet, boot, labelled, merged
from skopax.settingsources import core_knowledge_ids, core_setting_ids
from skopax.tunables import SESSIONS_AFTER_KEY, SLACK_CHANNEL_KEY, SSO_SWITCH_KEY, Invalid as TunableInvalid, Live, TunableStore, View, apply as apply_tunables, parse as parse_tunable, plugin_tunables, stored as stored_text
from skopax.tunables import KEY_ALIASES, current_instance, instance_menu
from skopax.push import Pusher, push_users
from skopax.investigator.flags import REQUESTS, masked_asked, masked_scan
from skopax.redact import redact
from skopax.scheduler import Job, Scheduler
from skopax.embedclient import KNOWN_DIMS, EmbedClient, dims_of
from skopax.semantic import indexer as semantic_indexer
from skopax.semantic import search as semantic_search
from skopax.semantic import uishape as semantic_shape
from skopax.semantic.index import VectorIndex
from skopax.settle import Settle, ignore_doc, ignored
from skopax.slackevents import Clicks, Intake, Listener, Replier, TicketRefused, socket_client, way_in
from skopax.clearnotes import ClearNotes
from skopax import threadsummary
from skopax.threadsummary import ThreadSummaries, email_names
from skopax.notices import Notices
from skopax import mail as mail_out, noticemail
from skopax.noticemail import NoticeMail
from skopax.versioncheck import OFFSET_S as VERSION_OFFSET, PERIOD_S as VERSION_PERIOD, START_DELAY_S as VERSION_START_DELAY, VersionCheck, jitter_of
from skopax.reminders import Reminders
from skopax import followup, partmail, tagreport, warndigest
from skopax.followup import FollowUps
from skopax.routes import TRIES, Echo, Refused as RouteRefused, Routes, finished as investigation_finished
from skopax.models import NoticeMailIn, RouteSendIn, SsoIn, SsoTestIn, TestMailIn
from skopax.mailtrial import MailTrials, PER_HOUR as TRIALS_PER_HOUR
from skopax.statenotes import StateNotes, who_of
from skopax.slack import CHANNEL_ID, stopped_who, thread_why, anomaly_message, fix_hash, fix_message, AUTO_BY, same_person, built_link, cut, esc, esc_keep_mentions, link_addresses, menu_kinds, NoThread, opening, public_hosts, reason_of, recurring_line, report_message, Slack, SlackError, SECTION_LIMIT
from skopax.setup import BadCode, SetupDone, SetupGate, render_form
from skopax import migrations
from skopax.errkind import NoAccess, Refusal, kind, text_of
from skopax.features import BY_ID as FEATURE_BY_ID, FEATURES, Features, Locked, off_text
from skopax.integrationview import reader_row
from skopax.instances import AGENT_PLUGIN as KUBE_ID, InstanceExists, InstanceMissing, Instances, InvalidInstance, PluginState, SourceOwned, Writer, checks_off, node_of, ADMIN
from skopax.registry import Registry, card_types_view, describe, locale_drops, standing_of
from skopax.plugai import PluginAi
from skopax.plugintools import PluginTools
from skopax.runtime import Runtime, SuggestFailed
from skopax.monitor import rules_of as monitor_rules
from skopax.probe import CERT_RULES, Probe, cert_pass, core_targets, google_hosts, public as public_reach, summary as probe_summary
from skopax import promqlroutes
from skopax.promqlchecks import JOB_OFFSET as CUSTOM_CHECKS_OFFSET, JOB_PERIOD as CUSTOM_CHECKS_PERIOD, PASS as CUSTOM_CHECKS, CustomChecks
from skopax.sweepout import Outputs
from skopax.sweeps import Reads, Sweeper, owned, pass_name, watched
from skopax.sealer import Sealer, master_key, stored_key
from skopax import ai as ai_module, aiuses
from skopax.ai import Clients, compat_ai, vertex_ai, route as ai_route
from skopax.aikey import AiConnections, AiKey, AiKeys, InvalidKey, TestBusy, UnknownConnection
from skopax.vertexkey import VertexConnections
from skopax.remote import Transport
from skopax.sessions import LOGIN_KIND, SESSION_KIND, Sessions, User, role_at_least, session_secret
from skopax.roles import Resolvers, role_map
from skopax.signin import Refused, SignIn, domain_refused
from skopax import ssobutton, workspaceicon
from skopax.ssoicon import ICON_MAX as SSO_ICON_MAX, IconRefused, SsoIcon, check as icon_check
from skopax.sso import FIELDS as SSO_FIELDS, SECRET_FIELD as SSO_SECRET_FIELD, SECRET_ID as SSO_SECRET_ID, SECRET_MAX as SSO_SECRET_MAX, InvalidSecret, SignOn, SourceOwned as SecretOwned, SsoConfig, SsoHttp, SsoSecret, TestBusy as SsoTestBusy
from skopax.throttle import KEY_MAX, Throttle
from skopax.store import CARD_HISTORY_MAX, HELD_OUTCOMES, SEVERITY_HIGH_MARK, SEVERITY_RESCORE_MARK, TAG_HUE_NAMES, ActionMoved, ChatPrivate, Conflict, NotFound, Store, iso, new_id, utc_now

OPEN_PATHS = ('/actuator/health', '/actuator/health/*', '/static/*', '/icons/*', '/favicon.ico', '/plugins/*', '/manifest.webmanifest', '/setup', '/login', '/login/sso', '/authorized', '/logout', '/mcp',
              '/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/*', '/login/sso-icon')
READ_METHODS = ('GET', 'HEAD')
SEARCH_PER_MINUTE = 30
SEARCH_WINDOW_S = 60
SEARCH_Q_MIN = 3
SEARCH_Q_MAX = 200
SEARCH_LIMIT_MAX = 20
SEARCH_LIMIT_DEFAULT = 10
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
ADDRESS = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
OWNERS_LINE = 'Responsibles of '
SSO_SETTING_LINE = re.compile(r'\(sso\.[a-z0-9.-]+\): ')
NO_DRAFTS = 'Slack drafts and holds are gone: the investigator posts to Slack itself when Slack auto-send is on; use Send to Slack on a card or a report to post by hand'
DROPS_SHOWN = 5
ORIGIN_VIEW = {'builtin': 'builtin', 'entry-point': 'package', 'env': 'env'}
DUPLICATE_KEY_CODE = 11000
CHAT_PAGE = re.compile(r'/chat/(?:thread|share)/[A-Za-z0-9-]{1,64}/?')
WEB = Path(__file__).parent / 'web'
TEMPLATE = WEB / 'template.html'
CARD_FIELDS = ('num', 'key', 'svc', 'label', 'sub', 'sev', 'status', 'muted', 'triage', 'ticket', 'owner', 'ownerName', 'assigned', 'firstSeen', 'lastSeen', 'clearedAt', 'what', 'note', 'business',
               'tags', 'url', 'gone', 'now', 'd1', 'd7', 'unit', 'recommend', 'suspects', 'outcome', 'resolution', 'triagedBy', 'autoResolvedAt', 'clearNote',
               'knownCause', 'alreadyActive')
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
REDRAW_START_DELAY_S = 30
LANGUAGE_WAIT_S = 120
LANGUAGE_POLL_S = 5
REDRAW_MARK_STALE_S = 1800
BROWSER_MOVE_MARK = 'browser-move'
TEST_TIMEOUT_S = 15
SUGGEST_TIMEOUT_S = 10
CHECK_MS_MAX_S = 1800
INVESTIGATOR_WORK = ('ask', 'dive', 'assess')
UPGRADE_EVIDENCE = (('tunables', {'by': {'$exists': True}}), ('runs', {}), ('investigations', {}), ('investigator_spend', {}))

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
    for name, body in core_locales.page_files().items():
        assets[name] = (body, hashlib.sha256(body).hexdigest()[:12])
    return assets


ASSETS = load_assets()


ICON_TYPES = {'.png': 'image/png', '.svg': 'image/svg+xml', '.ico': 'image/x-icon'}


def load_icons() -> dict[str, tuple[bytes, str]]:
    return {f.name: (f.read_bytes(), hashlib.sha256(f.read_bytes()).hexdigest()[:12]) for f in sorted((WEB / 'icons').iterdir()) if f.suffix in ICON_TYPES}


ICONS = load_icons()


MCP_DAYS = 7


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


def core_packs() -> list[str]:
    return [f'/static/{name}?v={ASSETS[name][1]}' for name in core_locales.page_files() if name in ASSETS]


def plugin_script_tags(paths: list[str]) -> str:
    return ''.join(f'<script src="{html.escape(path, quote=True)}"></script>' for path in paths)


def render_page(state: dict, plugin_scripts: list[str] = ()) -> str:
    blob = json.dumps(state, ensure_ascii=False).replace('<', '\\u003c')
    page = SCRIPT_SRC.sub(lambda m: f'<script src="/static/{m[1]}?v={ASSETS[m[1]][1]}"></script>', TEMPLATE.read_text(encoding='utf-8'))
    page = page.replace(PLUGIN_SLOT, plugin_script_tags(core_packs() + list(plugin_scripts)))
    return blob.join(fill_brand(part) for part in page.split(PLACEHOLDER))


MERGED_FIELDS = ('num', 'key', 'svc', 'label', 'sub', 'sev', 'status', 'url', 'what', 'business')
FOLLOW_UP_FIELDS = ('doneWhen', 'checkedAt', 'lastResult', 'lastEvidence', 'closeAt', 'closeKind', 'owner', 'keptBy', 'gaveUp')


def card_view(card: dict, live: bool, merged: list[dict] = (), parent: dict | None = None) -> dict:
    out = {k: card[k] for k in CARD_FIELDS if card.get(k) not in (None, '', [])}
    cause = causes.cause_of(card, {parent['key']: parent} if parent else {})
    if cause:
        out['cause'] = cause
    shots = (card.get('slackPost') or {}).get('screenshots') or (card.get('slackDraft') or {}).get('screenshots') or card.get('screenshots') or []
    kids = [{k: c[k] for k in MERGED_FIELDS if c.get(k) not in (None, '', [])} for c in merged]
    follow = card.get('followUp') if isinstance(card.get('followUp'), dict) else {}
    follow = {k: follow[k] for k in FOLLOW_UP_FIELDS if follow.get(k) not in (None, '', [])}
    return out | {'live': live, 'screenshots': list(shots)[:3]} | ({'merged': kids} if kids else {}) | ({'followUp': follow} if follow else {})


TICKET_HITS = 8
EMAIL_MAX = 254
ROUTES_STOP_S = 5
AUTO_STOP_S = 3
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
               transport: Transport | None = None, sources: SourceSet | None = None, ai_factory: Callable[..., object] | None = None, openai_factory: Callable[..., object] | None = None,
               compat_factory: Callable[..., object] | None = None, vertex_factory: Callable[..., object] | None = None, clients: Mapping[str, object] | None = None, probe: Probe | None = None, sso_http: SsoHttp | None = None,
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
    links_renamed = TunableStore(db).migrate_renamed()
    live = Live(settings, TunableStore(db), extra=lambda: tuple(t for p in runtime.active() for t in plugin_tunables(p)), values=merged(sources.current()), labels=labelled(sources.current()))
    base_settings = settings
    settings = View(live)
    live.own_sites = lambda: own_sites_of(live.current().own_sites, [i.values for i in runtime.instances_of(WEBSITE_PLUGIN)])

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
    def probe_all() -> list[dict]:
        found = runtime.probe_targets()
        try:
            more = core_targets(slack.enabled, routed_providers(), True, os.environ, google_hosts([p for p, _ in runtime.serving()]), settings.probe_extra)
        except Exception as e:
            log.warning('probe: the core targets were skipped (%s)', kind(e))
            more = []
        return found + more

    probe = probe or Probe(probe_all)
    features = Features(live, registry.edition, needs=lambda feature_id: needs_of(feature_id), started=lambda what: started_at_boot(what))
    brand.use_hidden(lambda: features.on('hide-attribution'))
    tunable_defs.TRACE_NAMES[0] = lambda: tuple(i.name for i in instances.all(tracing.PLUGIN))

    trace_targets: list[tuple[Settings, float, tracing.Target | None]] = []

    def trace_target() -> tracing.Target | None:
        current, now = live.current(), time.monotonic()
        if trace_targets and trace_targets[0][0] is current and now - trace_targets[0][1] < tracing.TARGET_CACHE_S:
            return trace_targets[0][2]
        name = current.langfuse_trace_instance
        plugin = next((p for p in runtime.active() if p.id == tracing.PLUGIN), None) if name else None
        found = next((i for i in runtime.instances_of(tracing.PLUGIN) if i.name == name), None) if plugin is not None else None
        target = tracing.target_of(plugin, found.info(), runtime.transport_for) if found is not None else None
        trace_targets[:] = [(current, now, target)]
        return target

    tracer = tracing.Tracer(lambda: features.on('langfuse-tracing'), trace_target, lambda: settings.langfuse_trace_content, lambda: settings.langfuse_trace_sample,
                            lambda: current_instance(settings))
    tracing.install(tracer)
    runtime.shared[tracing.TRACED_SETTING] = lambda: settings.langfuse_trace_instance if features.on('langfuse-tracing') and trace_target() is not None else ''

    def chat_attribution() -> dict | None:
        return brand.attribution_line()

    follower = Follower(sources, live, instances, os.environ)
    redaction.use_rules(runtime.redaction_rules, runtime.redaction_timed_out)
    tags.use_rules(runtime.tag_rules)
    tags.use_tags(runtime.tag_defs)
    tags.use_texts(runtime.card_texts, runtime.hook_timeout)
    select.use_own_findings(registry.own_findings)
    outbound = [base_settings.language]
    writing: list[dict[str, str]] = [{}]
    languages_loaded = [False]
    booted_at = clock()

    def languages_ready() -> bool:
        return languages_loaded[0] or clock() - booted_at >= LANGUAGE_WAIT_S

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
    workspace_icon = workspaceicon.store(db)
    setup_gate = SetupGate(db, users)
    sessions_box: list[Sessions] = []
    user_cache: dict[str, tuple] = {}
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
    if links_renamed:
        store.add_activity('admin', 'system', msg('settings.renamed', label=msg('settings.links_label'), key='core.workspace-links'))
    store.self_deploy = settings.self_deploy
    store.row_links = runtime.row_links
    store.release_ignore = settings.release_ignore
    base_url = public_base(settings)
    mail_out.use_site(base_url)
    odd_stamp: list[str] = []

    def site_hosts() -> tuple[str, ...]:
        return public_hosts([i.values for i in runtime.instances_of('website')], base_url)

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
    routes = Routes(store, runtime, settings, attribution=chat_attribution, base=lambda: base_url, autostart=True, only_new=True)
    jira_fields = JiraFields(jira)
    enricher = Enricher(store, gitlab, jira, live_projects)
    notes = StateNotes(store, slack, settings.slack_state_notes, jira_site=jira_site, mention=lambda email: slack_ids.mention(email), name_of=lambda email: slack_ids.name(email),
                       by_name=lambda name: tagreport.name_mentions(slack_people, slack_ids)(name))
    def user_name(email: str) -> str:
        known = str((users.get(email) or {}).get('name') or '').strip()
        return known if known and '@' not in known else ''

    def slack_name(email: str) -> str:
        return user_name(email) or slack_ids.name_now(email)

    def post_check() -> Callable[[str], str | None]:
        hosts = allowed_link_hosts(settings, runtime.org()['link_hosts'] + runtime.ui_hosts())
        try:
            rule = gitlab.link_problem if gitlab is not None and gitlab.enabled else None
        except SourceDown:
            rule = None
        return lambda text: draft_problem(text, hosts, rule)

    card_posts = CardPosts(store, slack, base_url, menu_on=lambda: started_at_boot('slack-socket'), name_of=slack_name, plugin_name=runtime.plugin_name, check=post_check, hosts=site_hosts)
    attacher = Attacher(store, slack, settings.slack_attach_screenshots, base_url)
    notice_mail = NoticeMail(store, lambda: mail_out.mailer(runtime), base_url=base_url)
    mail_trials = MailTrials(db, clock=clock)
    notices = Notices(store, mail=notice_mail)
    versions = VersionCheck(store, transport, lambda: features.on('update-check'), clock=clock)
    reminders = Reminders(store, slack, settings.slack_remind_hours, base_url, mention=lambda email: slack_ids.mention(email), name_of=lambda email: slack_ids.name(email),
                          by_name=lambda name: tagreport.name_mentions(slack_people, slack_ids)(name))
    slack_people = tagreport.SlackPeople(slack)
    slack_ids = tagreport.SlackIds(slack, on_scope=lambda missing: store.note_email_scope(missing, iso(utc_now())), flagged=lambda: store.email_scope() is not None)

    def active_people() -> dict[str, str]:
        return {u['email']: u.get('name') or '' for u in users.list() if u and u.get('email') and not u.get('disabled')}

    def disabled_people() -> set[str]:
        return {u['email'] for u in users.list() if u and u.get('email') and u.get('disabled')}

    part_mail = partmail.PartMail(lambda: mail_out.mailer(runtime), lambda: settings.tags_mail, store.tag_owners, left_out=disabled_people)
    tag_report = tagreport.TagReport(store, slack, slack_ids, store.tag_owners, active_people,
                                     lambda: settings.tag_report_at, lambda: features.on(tagreport.FEATURE) and features.on('slack-autosend'), base_url, left_out=disabled_people,
                                     mail=part_mail, mail_on=lambda: features.on(tagreport.FEATURE))
    warn_digest = warndigest.WarnDigest(store, slack, lambda: settings.warn_digest_at, lambda: features.on(warndigest.FEATURE) and features.on('slack-autosend'), base_url,
                                        mail=part_mail, mail_on=lambda: features.on(warndigest.FEATURE))
    clear_notes = ClearNotes(store, slack, settings.slack_clear_note_minutes, title_prefix=settings.jira_title_prefix)
    jira_links = JiraLinks(store, jira, settings.jira_links, notes, base_url, settings.jira_developer_field)
    backlinks = Backlinks(store, jira, base_url, mode=lambda: settings.jira_backlink, projects=live_projects,
                          pool=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix='skopax-jira-backlink'))
    assigner = Assigner(store, slack, settings, base_url, notes, notices, posts=card_posts, by_name=slack_people.email_of)
    auto_pool: list[ThreadPoolExecutor] = []
    auto_guard = threading.Lock()

    def auto_later(job) -> None:
        with auto_guard:
            if not auto_pool:
                auto_pool.append(ThreadPoolExecutor(max_workers=1, thread_name_prefix='skopax-auto-assign'))
        auto_pool[0].submit(job)

    auto_assign = AutoAssign(store, assigner, active_people, later=auto_later)

    def stop_auto_pool() -> None:
        last = auto_pool[0].submit(lambda: None)
        try:
            last.result(timeout=AUTO_STOP_S)
        except FutureTimeout:
            log.info('auto-assign messages were still going out at shutdown; the rest are dropped')
        auto_pool[0].shutdown(wait=False, cancel_futures=True)
    postdeploy = PostDeploy(store, prom_for, jira, lambda: features.on('postdeploy'), base_url, assess=lambda: investigator.available(), projects=settings.release_projects, serving=prom_watching,
                            primary_name=lambda: getattr(runtime.instances.primary(PROMETHEUS_ID), 'name', None), release_template=lambda: settings.jira_release_name, plugin_reports=lambda rows, started, now: runtime.deploy_reports(rows, started, now),
                            autopost=lambda: features.on('slack-autosend'))
    settle = Settle(store, settings.release_settle)
    throttle = Throttle(db)

    def slack_operator(email: str) -> bool:
        return role_at_least(signin.slack_role(email), 'operator')

    operators = Operators(operators_lookup or slack_operator, slack, settings)

    def card_of(key: str) -> dict | None:
        try:
            return store.anomaly(key)
        except NotFound:
            return None

    def parent_of(card: dict) -> dict | None:
        if not card.get('parent') or card['parent'] == card.get('key'):
            return None
        try:
            return store.anomaly(card['parent'])
        except NotFound:
            return None

    def post_anomaly_to_slack(key: str, by: str, base: str = '', skip_kept: bool = False) -> dict:
        anomaly = store.anomaly(key)
        link = card_url(base_url or base, key, anomaly.get('num'))
        kept_out = {} if skip_kept and kept_text(anomaly) else anomaly.get('slackDraft') or {}
        draft = kept_out.get('text') or None
        shots = kept_out.get('screenshots') or []
        action = {'type': 'slack', 'key': key, 'target': f'slack:{key}', 'label': f"Send to Slack: {anomaly.get('svc')} {anomaly.get('label')}", 'requestedBy': by, 'draft': bool(draft)}
        thread = card_thread(anomaly)
        buttons = card_posts.new_menu(key, by) if started_at_boot('slack-socket') else None
        head, mute = opening(by) | {'at': iso(utc_now())}, card_posts.mute_of(key)
        who = stopped_who(anomaly, mute, slack_name)
        placed = thread_why(anomaly, card_of(thread['key']), thread['key'], thread.get('why')) if thread else ''
        message = anomaly_message(anomaly, link, by, draft, buttons['id'] if buttons else None, chat_url(base_url or base, key, anomaly.get('num')), head=head,
                                  attribution=None if thread else slack.signature(), mute=mute, kinds=menu_kinds(anomaly, mute), shots=shots, base=base_url,
                                  parent=parent_of(anomaly), who=who, hosts=site_hosts(), placed=placed) | ({'channel': thread['channel'], 'thread_ts': thread['ts']} if thread else {})
        try:
            posted = slack.post(message, sign=False)
        except SlackError as e:
            log.warning('slack post for %s failed: %s', key, kind(e))
            store.add_action(action, 'failed', f'not sent: {reason_of(e)}')
            raise
        sent = slack.linked(posted, thread['ts'] if thread else None)
        log.info('slack post for %s by %s, draft=%s, thread of %s', key, by, bool(draft), thread['key'] if thread else None)
        body = {'text': draft, **{k: v for k, v in kept_out.items() if k in ('headline', 'by') and v}} if draft else None
        drawn = {'sig': drawn_sig(anomaly, mute, who), 'at': iso(utc_now())}
        post = {**sent, 'at': iso(utc_now()), 'by': by, 'draft': bool(draft), 'head': head, 'drawn': drawn, **({'body': body} if body else {}), **({'menu': buttons['id']} if buttons else {}),
                **({'screenshots': shots} if shots else {})}
        if thread:
            post |= {'ts': thread['ts'], 'replyTs': sent.get('ts'), 'threadOf': thread['key'], **({'threadWhy': thread['why']} if thread.get('why') else {})}
        try:
            store.slack_posted(key, post)
        except (PyMongoError, NotFound, Conflict) as e:
            log.warning('slack post for %s went out but was not recorded on the anomaly: %s', key, kind(e))
            try:
                store.post_unrecorded(key, f'sent to Slack but not recorded on the card ({kind(e)}); not sent again')
            except PyMongoError as again:
                log.warning('the unrecorded Slack post on %s was not marked either: %s', key, kind(again))
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

    def send_anomaly_to_slack(key: str, by: str, base: str = '', skip_kept: bool = False) -> dict:
        posting.add(key)
        try:
            return post_anomaly_to_slack(key, by, base, skip_kept)
        finally:
            posting.discard(key)

    @contextmanager
    def posting_cards(keys: list[str]):
        mine = [k for k in dict.fromkeys(keys) if k not in posting]
        posting.update(mine)
        try:
            yield
        finally:
            posting.difference_update(mine)

    def echo_ready(card: dict) -> bool:
        return bool(slack.enabled and features.on('slack-autosend') and not card.get('muted') and auto.live(card) and not auto_send_waiting(card) and card.get('key') not in posting)

    def echo_to_slack(key: str) -> dict | None:
        card = store.anomaly(key)
        post = card.get('slackPost') or {}
        if post.get('ts'):
            return post
        if not echo_ready(card):
            return None
        send_anomaly_to_slack(key, SERVER_BY, skip_kept=True)
        now_card = store.anomaly(key)
        posted = now_card.get('slackPost') or None
        if posted is None:
            raise RuntimeError('the Slack post was not recorded on the card')
        if investigation_finished(now_card):
            card_posts.refresh(key)
        return posted

    def echo_in_thread(post: dict, text: str) -> None:
        slack.post({'text': text, 'channel': post['channel'], 'thread_ts': post['ts']}, sign=False)

    routes.echo = Echo(echo_to_slack, echo_in_thread, echo_ready)
    routes.investigating = lambda: investigator_ready() and settings.investigate_mode == 'live' and investigator.available()
    routes.check = post_check

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
            slack.post(fix_message([c for c, _ in claimed], link, site_hosts()) | {'channel': post['channel'], 'thread_ts': post['ts']})
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
        message = report_message(report, link, by, draft, store.load().get('anomalies') or [], jira_site(), settings.jira_release_name, site_hosts())
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
            try:
                store.post_unrecorded(report_id, f'sent to Slack but not recorded on the report ({kind(e)}); not sent again', 'report')
            except PyMongoError as again:
                log.warning('the unrecorded Slack post on report %s was not marked either: %s', report_id, kind(again))
        where = f"today's deploy thread in {slack.channel}" if thread else slack.channel
        return store.add_action(action | {'slack': sent}, 'done', f"sent to {where}{' with the draft' if draft else ''}" + (f'; {pictures}' if pictures else ''))

    def slack_thread_update(key: str, text: str, by: str, shots: list[dict] | None = None, run_id: str | None = None, notice: str | None = None, linked: bool = False) -> dict:
        anomaly = store.anomaly(key)
        post = anomaly.get('slackPost') or {}
        if not post.get('channel') or not post.get('ts'):
            raise NoThread('this anomaly has no Slack message from this page to reply to')
        action = {'type': 'slack-update', 'key': key, 'target': f'slack-update:{key}', 'label': f"Slack thread update: {anomaly.get('svc')} {anomaly.get('label')}", 'requestedBy': by}
        try:
            tail = recurring_line(anomaly)
            sent, pictures = attacher.post({'channel': post['channel'], 'thread_ts': post['ts'], 'text': (link_addresses(esc_keep_mentions(text), site_hosts(), SECTION_LIMIT - len(tail)) if linked else esc_keep_mentions(text)) + tail},
                                           shots or [], key, run_id)
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
        line = msg('slack.tell', id=user['id'], text=link_addresses(esc(text), site_hosts()), who=esc(who_of(by)))
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
        said = auto.send_soon(key)
        if said == 'busy':
            raise ValueError('this card is being posted to Slack already')
        return {'result': 'posting to Slack now' if said == 'queued' else 'not posted: Slack auto-send is off'}

    replier = Replier(store, slack, settings, attach=attacher, base_url=base_url, hosts=site_hosts, summarise=lambda action: threads.answered(action))

    def state_note(key: str, before: dict) -> None:
        notes.triaged(key, before)
        notices.triaged(key, before)
        card_posts.triaged(key, before)
        backlinks.triaged(key, before)

    aikey = AiKey(db, get_sealer, lambda: live.base.anthropic_api_key, lambda: stored_key(db), live.tunables, factory=ai_factory)
    openai_key = AiKey(db, get_sealer, lambda: live.base.openai_api_key, lambda: stored_key(db), live.tunables, factory=openai_factory, provider=OPENAI)
    ai_keys = AiKeys(lambda: settings.ai_provider, {ANTHROPIC: aikey, OPENAI: openai_key})
    ai_connections = AiConnections(db, get_sealer, lambda: stored_key(db), live.tunables, lambda: bool(settings.allow_private_ai), make=compat_factory)
    compat_ai.bind(ai_connections, lambda: bool(settings.allow_private_ai), compat_factory)
    compat_ai.register(ai_module)
    ai_connections.changed = lambda: live.refresh(force=True)
    vertex_connections = VertexConnections(db, get_sealer, lambda: stored_key(db), live.tunables, lambda: settings.vertex_token_dir, make=vertex_factory)
    vertex_ai.bind(vertex_connections, lambda: settings.vertex_token_dir, vertex_factory)
    vertex_ai.register(ai_module)
    vertex_connections.changed = lambda: live.refresh(force=True)
    vertex_connections.taken = lambda name: ai_connections.get(name) is not None
    ai_connections.taken = lambda name: vertex_connections.get(name) is not None
    try:
        ai_connections.refresh()
    except Exception as e:
        log.warning('the stored AI connections were not read at start, reading them again shortly: %s', kind(e))
    try:
        vertex_connections.refresh()
    except Exception as e:
        log.warning('the stored Vertex AI connections were not read at start, reading them again shortly: %s', kind(e))
    provider_keys = {ANTHROPIC: aikey.current, OPENAI: openai_key.current}
    keyed_clients = Clients(lambda: settings.ai_provider, provider_keys, lambda k: None, 10.0, 0, anthropic_client)
    live.keyed = keyed_clients.keyed

    def upgrade_chat_effort() -> None:
        live.upgrade_chat_effort()

    def upgrade_models() -> None:
        live.upgrade_models(lambda: any(db[name].find_one(found, {'_id': 1}) is not None for name, found in UPGRADE_EVIDENCE))

    def org_pack() -> OrgPack | None:
        found = runtime.org_pack()
        return None if found is None else found[1]

    mcptools_catalog = McpCatalog(db, note=lambda by, text: store.add_activity(ADMIN_KIND, by, text), public=lambda: bool(settings.public_read))
    mcptools_auth = McpOAuth(db, get_sealer, lambda: base_url, mcptools_catalog, note=lambda by, text: store.add_activity(ADMIN_KIND, by, text),
                             http=lambda ca: mcptools_deps.http(ca), local=lambda: mcptools_routes.local_http(mcptools_deps))
    mcptools_deps = mcptools_routes.Deps(mcptools_catalog, mcptools_auth, instances, lambda: getattr(registry.get(MCPTOOLS_ID), 'plugin', None),
                                         lambda key: runtime.setting(MCPTOOLS_ID, key), FieldRefused, oauth=mcptools_auth)
    mcptools_bridge = ToolBridge(mcptools_deps, runtime, features, store)

    talk = SmallTalk(store, settings, client=anthropic_client, keys=provider_keys, pack=org_pack)
    investigator = investigator or Investigator(
        store, Sources(gitlab=gitlab, jira=jira, plugin_tools=PluginTools(runtime), selfreview=lambda now: review_facts(store, server_info() | store.server_runs(), runtime.client('elasticsearch') if settings.self_log_index else None, now, own_commits, self_service(settings), settings.self_log_index, PluginTools(runtime)),
                              link_hosts=lambda: allowed_link_hosts(settings, runtime.org()['link_hosts'] + runtime.ui_hosts()), org=runtime.org,
                              bridge=mcptools_bridge, own_sites=lambda: live.own_sites()[0]),
        Effects(pod_slack_send, functools.partial(slack_thread_update, linked=True), replier.reply, state_note, tell=slack_tell, fixes=lambda key: post_fixes(key), slack_file=slack.file, autopost=lambda: auto.on(), wake=lambda: auto.wake()), settings, client=anthropic_client, base_url=base_url, talk=talk, keys=provider_keys)

    def after_follow_close(key: str, before: dict) -> None:
        notes.triaged(key, before)
        card_posts.triaged(key, before)
        backlinks.triaged(key, before)

    follow_ups = FollowUps(store, lambda: settings, investigator, slack, features.on, notices, base_url=base_url, windows=lambda: runtime.auto_windows(),
                           mention=lambda email: slack_ids.mention(email), name_of=lambda email: slack_ids.name(email), after_close=after_follow_close)
    if isinstance(getattr(investigator, 'effects', None), Effects):
        investigator.effects.follow_up = follow_ups.verdict

    if isinstance(investigator, Investigator):
        investigator.notices = notices
        investigator.on = lambda: features.on('investigator')
        investigator.pack = org_pack
        investigator.role_of = lambda action: asker_role(action, users)
        investigator.guard = Guard(store, settings, client=anthropic_client, keys=provider_keys)
        investigator.admins = lambda: [str(u.get('email') or '') for u in users.list() if u.get('role') == 'admin' and not u.get('disabled')]
    if (settings.guard_check_questions or settings.guard_check_answers) and not settings.investigate_guard_model:
        store.add_activity(ADMIN_KIND, 'server', msg('guard.checks_off_no_model'))
    if settings.guard_mode == 'watch':
        store.add_activity(ADMIN_KIND, 'server', msg('guard.watch_at_startup'))

    gate = Gate(store, settings, client=anthropic_client, keys=provider_keys, pack=org_pack)
    threads = ThreadSummaries(store, slack, settings, client=anthropic_client, keys=provider_keys, names=email_names(operators), halted=lambda now: investigator.why_halted(now))

    intake = Intake(store, slack, operators, gate, talk, investigator, settings, base_url=base_url, ticket_extra=plugin_projects, on=lambda: features.on('slack-listener') and slack.enabled,
                    chat=lambda: features.on('chat'), not_ready=lambda: not_ready_text('slackevents.not_ready', 'slackevents.needs'), jira_site=jira_site, threads=threads)

    def refuse_information(key: str) -> None:
        try:
            card = store.anomaly(key)
        except NotFound:
            return
        if information.is_item(card):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('cards.information_item', key=key))

    def apply_card_action(action: dict) -> str:
        before = notes.watch(action)
        seen = notices.watch(action)
        linked = backlinks.watch(action)
        result = page_actions.apply(store, action, jira, settings, notify=task_notice, extra_projects=plugin_projects(), name_of=user_name, meta=jira_fields.create_meta, field_kind=jira_fields.field_kind)
        notes.applied(action, before)
        notices.applied(action, seen)
        card_posts.after(action)
        backlinks.applied(action, linked)
        return result

    def apply_from_slack(action: dict) -> str:
        if action.get('type') == 'ticket' and drafts.is_tech(action) and settings.jira_tech_project:
            action = action | {'project': settings.jira_tech_project}
        try:
            return apply_card_action(action)
        except (NotFound, page_actions.Refused) as e:
            raise TicketRefused(e.text) from None

    proposals = Proposals(store, runtime)

    def slack_way_in(slack_user: str) -> str:
        if operators_lookup is not None:
            return ''
        email = operators.email_of(slack_user)
        if not email:
            return ''
        doc = signin.users.get(email)
        closed = bool(doc.get('disabled')) if doc is not None else domain_refused(email, settings)
        mapped = bool(settings.sso_role_map) or bool(runtime.role_resolvers_named()) or (doc is not None and doc.get('roleFrom') in IDP_SOURCES)
        return way_in(bool(settings.sso_on), doc is not None, base_url, added_only=settings.sso_new_accounts == 'added-only', viewer_default=settings.sso_default_role != 'operator',
                      mapped=mapped, closed=closed)

    clicks = Clicks(store, slack, operators, apply_from_slack, jira_site, on=lambda: features.on('slack-listener') and slack.enabled, confirm=proposals.confirm, way_in=slack_way_in, hosts=site_hosts, base_url=base_url)
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
            log.warning('slack.listen is %s but the Slack listener stays off: it needs the Slack app and bot tokens (Connect \u203a Integrations \u203a Slack) and an enabled investigator', listen_mode)

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

    auto = AutoSend(store, lambda key: send_anomaly_to_slack(key, AUTO_BY), lambda report_id: send_report_to_slack(report_id, AUTO_BY), on=lambda: features.on('slack-autosend'), ready=lambda: slack.enabled)
    fallback = Fallback(store, settings.slack_fallback_minutes, auto.wake, on=lambda: features.on('slack-autosend'))
    post_now = PostNow(store, lambda key: send_anomaly_to_slack(key, SERVER_BY), lambda: settings.slack_post_sev if slack.enabled and features.on('slack-autosend') else 'off',
                       lambda: investigator_ready() and settings.investigate_mode == 'live')

    def assign_back(cards: list[dict]) -> None:
        for card in cards:
            email = card.get('assignBack')
            if not isinstance(email, str) or '@' not in email:
                continue
            try:
                store.set_tagging(card['key'], {'assignBack': None})
                done = assigner.assign(card['key'], user_name(email) or email.split('@')[0], email, SYSTEM)
                card.update(owner=done.get('owner'), assigned=done.get('assigned'))
                card.pop('assignBack', None)
            except Exception as e:
                log.warning('the card %s that came back was not assigned back: %s', card.get('key'), kind(e))

    def server_apply(results: list[PassResult], now: float, links, after=None, check: str | None = None, screen=None, clusters=None) -> Applied | None:
        try:
            took = time.time() - now
            ms = int(took * 1000) if 0 <= took <= CHECK_MS_MAX_S else None
            store.add_check_runs([{'name': r.name, 'check': check or r.name, 'ok': r.ok, 'ms': ms, 'error': None if r.ok else r.status} for r in results])
        except PyMongoError as e:
            log.warning('check runs not recorded: %s', kind(e))
        applied = apply(store, results, now, links, dm=pusher.enabled, after=after, auto_resolve_hours=settings.auto_resolve_hours,
                        recurring_count=settings.recurring_count, recurring_days=settings.recurring_days, tag_rules=tags.rules(settings.tag_rules), alert_twins=settings.alert_twins, screen=screen, clusters=clusters, kind_windows=runtime.auto_windows(),
                        severity_hours={'crit': settings.auto_resolve_hours_crit, 'high': settings.auto_resolve_hours_high, 'warn': settings.auto_resolve_hours_warn},
                        business=clocks.zone() if settings.business_days else None, alerts_warn=settings.alerts_warn, follow_up_days=settings.investigate_follow_up_days)
        if applied and applied.resolved:
            for r in applied.resolved:
                notes.auto_resolved([r['key']], r.get('hours') or settings.auto_resolve_hours, r.get('since'), {r['key']} if r.get('event') else (), bool(r.get('businessTime')))
            notices.auto_resolved([r['key'] for r in applied.resolved if not r.get('progress')], {r['key']: r['why'] for r in applied.resolved if r.get('why')})
            notices.followed([r['key'] for r in applied.resolved if r.get('progress')])
        if applied:
            assign_back(applied.cards)
            notices.fired(applied.events, applied.cards)
            auto_assign.sweep(applied.cards)
        if applied and applied.push:
            pusher.send(applied.push)
        if applied:
            post_now.run(fresh_cards(applied, now), applied.cards, now)
        if applied and investigator.enabled and any(a.get('sev') == 'crit' for a in applied.push):
            investigator.kick(time.time())
        if applied:
            routes.later(applied.cards, now, applied.push)
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
                             client=anthropic_client, keys=provider_keys)

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
        if languages_ready():
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
            linked = and_notify(and_notify(and_notify(jira_links.run_once(now), card_posts.jira), notices.jira_swept), backlinks.swept)
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

    sweeper = Sweeper(runtime, store, server_apply, enrich=enrich_keys, outputs=Outputs(store, slack, attacher, base_url, lambda: settings.screenshot_max_per_day, auto=lambda: features.on('slack-autosend'), posting=posting_cards, mail=part_mail))
    custom_checks = CustomChecks(store, runtime, server_apply)

    def refresh_languages() -> None:
        core = list(core_locales.packs())
        added = [locale for _, locale in runtime.locales()]
        locales = core + added
        trees = core_locales.chainlit_trees(core, added)
        messages.set_catalogs([(locale.lang, locale.messages) for locale in locales])
        outbound_language()
        languages_loaded[0] = True
        set_chat_texts([(locale.lang, locale.chat) for locale in locales], trees)
        chatui.set_translations(trees)
        writing[0] = {locale.lang: locale.writing for locale in locales if locale.writing}

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
    plugin_ai = PluginAi(store, None, lambda now: investigator.why_halted(now), client=anthropic_client, provider=lambda: settings.ai_provider, keys=provider_keys,
                         given=lambda plugin_id: live.plugin_value_given(f'{plugin_id}.model'))
    runtime.ai = plugin_ai.for_check
    runtime.memos = store.plugin_memo
    embed_indexer = semantic_indexer.Indexer(store, lambda: settings, EmbedClient(lambda: settings), lambda: features.on('semantic-search'), lambda: semantic_indexer.usable_knowledge(runtime), lambda: semantic_indexer.usable_docs(runtime))
    store.embed_gate = embed_indexer.wants
    embed_client = EmbedClient(lambda: settings)
    semantic_searcher = semantic_search.Searcher(store, lambda: settings, embed_client, VectorIndex(dims_of(settings.embed_model) or KNOWN_DIMS[EMBED_MODEL], max_vectors=settings.embed_max_vectors),
                                                 lambda: features.on('semantic-search'), knowledge=lambda allowed: semantic_search.usable_knowledge(runtime, allowed),
                                                 serving=runtime.serving)
    investigator.sources.similar = semantic_searcher
    core_jobs = [
        Job('enrich', SWEEP_PERIOD, ENRICH_OFFSET, lambda now: enricher.sweep(), lambda: enricher.enabled),
        Job('settle', SWEEP_PERIOD, SETTLE_OFFSET, lambda now: settle.run_once(), settle.enabled),
        Job(JIRA_ACCESS, JIRA_LINKS_PERIOD, JIRA_LINKS_OFFSET, jira_links_run, jira_links_on),
        Job(SLACK_POSTS, SLACK_POSTS_PERIOD, SLACK_POSTS_OFFSET, slack_posts_run, lambda: slack.enabled),
        Job('investigate', INVESTIGATE_PERIOD, INVESTIGATE_OFFSET, investigator.kick, lambda: investigator.enabled),
        Job('held-answers', HELD_PERIOD, HELD_OFFSET, lambda now: investigator.expire_held(now),
            lambda: isinstance(investigator, Investigator) and (bool(getattr(investigator.guard, 'checks_answers', False)) or store.has_held())),
        Job('slack-fallback', SLACK_FALLBACK_PERIOD, SLACK_FALLBACK_OFFSET, fallback.run_once, lambda: slack.enabled and fallback.enabled),
        Job('slack-reminders', REMINDER_PERIOD, REMINDER_OFFSET, lambda now: and_notify(reminders.run_once(now), notices.reminded), lambda: reminders.enabled),
        Job('slack-clear-notes', CLEAR_NOTE_PERIOD, CLEAR_NOTE_OFFSET, clear_notes.run_once, lambda: clear_notes.enabled),
        Job('slack-thread-summary', threadsummary.SWEEP_PERIOD, threadsummary.SWEEP_OFFSET, threads.sweep, lambda: slack.enabled and threads.on),
        Job('self-review', DAY_S, lambda now, hour=max(settings.selfreview_hour, 0): review_slot_offset(hour, now), self_review.scheduled, lambda: self_review.configured, background=True),
        Job('probe', PROBE_PERIOD, PROBE_OFFSET, probe_run, enabled=lambda: settings.probe),
        Job('version-check', VERSION_PERIOD, VERSION_OFFSET + jitter_of(base_settings.mongo_uri + base_settings.mongo_db), versions.scheduled, lambda: features.on('update-check')),
        Job(CUSTOM_CHECKS, CUSTOM_CHECKS_PERIOD, CUSTOM_CHECKS_OFFSET, custom_checks.run_once, custom_checks.enabled),
        Job(tagreport.RUN, tagreport.PERIOD, tagreport.OFFSET, tag_report.run_once, lambda: tag_report.scheduled),
        Job(warndigest.RUN, warndigest.PERIOD, warndigest.OFFSET, warn_digest.run_once, lambda: warn_digest.scheduled),
        Job(followup.RUN, followup.PERIOD, followup.OFFSET, follow_ups.run_once, lambda: follow_ups.enabled),
        Job('notice-mail', noticemail.PERIOD, noticemail.OFFSET, notice_mail.run_once, notice_mail.ready),
        Job(semantic_indexer.NAME, semantic_indexer.PERIOD, semantic_indexer.OFFSET, embed_indexer.run_once, embed_indexer.active, background=True),
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
            beats = kube_rows(plugin) if plugin.id == KUBE_ID else None
            instances_out = []
            for i, inf in zip(found or [None], infos):
                checks = watched(plugin, i)
                first = checks[0] if checks else None
                if i is None and first is None:
                    continue
                health = instance_health(plugin, i, passes, True, beats) if i is not None else pass_health(checks, None, passes)
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

    def kube_rows(plugin) -> list[dict]:
        beat = next((c for c in plugin.checks if c.once), None)
        if beat is None or not beat.sections:
            return []
        stored = sections.stored_rows({beat.sections[0]: store.state_section(beat.sections[0])}, beat.sections[0])
        return [r for r in stored if isinstance(r, dict) and sections.plugin_of(r) == plugin.id and not r.get('cluster')]

    def kube_agent_health(plugin, inst, passes: dict, rows: list[dict]) -> dict | None:
        primary = next((i for i in instances.all(plugin.id) if i.primary), None)
        beat = next((c for c in plugin.checks if c.once), None)
        if primary is None or beat is None or not primary.enabled or beat.id in primary.off_checks:
            return None
        run = passes.get(beat.id) or {}
        mine = next((r for r in rows if r.get('name') == inst.name), None)
        if mine is None:
            return {'state': 'pending', 'text': msg('integrations.health.pending'), 'at': None} if (inst.values.get('mode') or 'agent') == 'agent' else None
        fresh = isinstance(run.get('status'), str) and passed(run['status']) and isinstance(run.get('at'), str)
        at = run['at'] if fresh else mine.get('lastSeen')
        if mine.get('status') == 'NotReady':
            return {'state': 'error', 'text': mine.get('error') or msg('kubernetes.nodes.no_answer'), 'at': at}
        if mine.get('clusterError'):
            return {'state': 'warn', 'text': mine['clusterError'], 'at': at}
        return {'state': 'ok', 'text': msg('integrations.health.ok'), 'at': at}

    def instance_health(plugin, inst, passes: dict, on: bool, beats: list[dict] | None = None) -> dict:
        checks = watched(plugin, inst)
        if not on:
            found = {'state': 'off', 'text': msg('integrations.health.plugin_off', plugin=runtime.plugin_name(plugin.id)), 'at': None}
        elif not inst.enabled:
            found = {'state': 'off', 'text': msg('integrations.health.off'), 'at': None}
        elif inst.problem:
            found = {'state': 'error', 'text': inst.problem, 'at': None}
        elif route_problem := runtime.route_problem(plugin, inst):
            found = {'state': 'error', 'text': route_problem, 'at': None}
        elif plugin.outbound is not None and (failed := routes.failure(plugin.id, inst.name)):
            found = {'state': 'error', 'text': failed, 'at': None}
        elif plugin.id == 'jira' and inst.primary and (linked := jira_links_health(passes)):
            found = linked
        elif plugin.id == KUBE_ID and not inst.primary and (agent := kube_agent_health(plugin, inst, passes, kube_rows(plugin) if beats is None else beats)):
            found = agent
        elif plugin.id == MCPTOOLS_ID:
            found = {**mcptools_routes.health(mcptools_deps, inst), 'at': None}
        elif plugin.checks and node_of(inst):
            found = {'state': 'off', 'text': msg('integrations.health.through_node', node=node_of(inst), checks=', '.join(c.label for c in plugin.checks)), 'at': None}
        elif not checks and any(owned(c, inst) for c in plugin.checks):
            found = {'state': 'off', 'text': msg('integrations.health.unchecked'), 'at': None}
        else:
            found = pass_health(checks, inst.info(), passes)
        return {'name': inst.name, **found}

    def plugin_health(plugin, found, passes: dict) -> dict:
        on = any(p.id == plugin.id for p in runtime.active())
        beats = kube_rows(plugin) if plugin.id == KUBE_ID else None
        rows = [instance_health(plugin, i, passes, on, beats) for i in found]
        live = [r for r in rows if r['state'] != 'off']
        bad = [r for r in live if r['state'] == 'error'] or [r for r in live if r['state'] == 'warn']
        fine = [r for r in live if r['state'] == 'ok']
        waiting = [r for r in live if r['state'] == 'pending']
        if not found and plugin.fields:
            top = {'state': 'off', 'text': msg('integrations.health.unset'), 'at': None}
        elif not on:
            top = {'state': 'off', 'text': msg('integrations.health.plugin_off', plugin=runtime.plugin_name(plugin.id)), 'at': None}
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
        severity_upgrade()
        severity_rescore()
        card_history_backfill()
        slack_links_backfill()

    def severity_upgrade() -> None:
        try:
            if not store.begin_once(SEVERITY_HIGH_MARK, iso(utc_now() - dt.timedelta(seconds=REDRAW_MARK_STALE_S))):
                return
            lifted = store.lift_warnings(iso(utc_now() - dt.timedelta(hours=settings.archive_hours or 48)))
            if lifted:
                store.add_activity(ADMIN_KIND, 'server', msg('severity.high_added_one' if lifted == 1 else 'severity.high_added', count=lifted))
            store.finish_once(SEVERITY_HIGH_MARK)
        except Exception as e:
            log.warning('the warning cards were not moved to High this start: %s', kind(e))

    def severity_rescore() -> None:
        try:
            if not store.begin_once(SEVERITY_RESCORE_MARK, iso(utc_now() - dt.timedelta(seconds=REDRAW_MARK_STALE_S))):
                return
            lifted = store.rescore_warnings(iso(utc_now() - dt.timedelta(hours=settings.archive_hours or 48)), settings.alerts_warn)
            if lifted:
                store.add_activity(ADMIN_KIND, 'server', msg('severity.rescored_one' if lifted == 1 else 'severity.rescored', count=lifted))
            store.finish_once(SEVERITY_RESCORE_MARK)
        except Exception as e:
            log.warning('the warning cards were not checked against the High rules this start: %s', kind(e))

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
            users.create(settings.admin_email, msg('setup.admin_name'), 'admin', settings.admin_password, name_from='default')
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
        try:
            investigator.shutdown()
        finally:
            tracer.shutdown()
            tracing.uninstall(tracer)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        boot_id = secrets.token_hex(8)
        writer = Writer(instances)
        runtime.migrate = lambda: migrations.attempt(db, registry, boot_id, writer=writer, steps=(upgrade_models, upgrade_chat_effort))
        checked = False
        try:
            store.ensure_indexes()
            users.ensure_indexes()
            token_indexes()
            throttle.ensure_indexes()
            live.tunables.ensure_indexes()
            notice_mail.ensure_indexes()
            mail_trials.ensure_indexes()
            get_sessions()
            first_run()
            try:
                auto_assign.since()
            except Exception as e:
                log.warning('the auto-assign start time was not stored: %s', kind(e))
            await asyncio.to_thread(instances.ensure_indexes)
            await asyncio.to_thread(mcptools_auth.ensure_indexes)
            try:
                caught_up = await asyncio.to_thread(migrations.wait_and_run, db, registry, boot_id, writer=writer, steps=(upgrade_models, upgrade_chat_effort))
            except PyMongoError as e:
                log.warning('%s', msg('migrations.lease_error', error=kind(e)))
                caught_up = None
            checked = True
            if caught_up is not None:
                runtime.migrate = None
            await asyncio.to_thread(runtime.refresh)
            try:
                if await asyncio.to_thread(browser_upgrade):
                    await asyncio.to_thread(runtime.refresh)
            except Exception as e:
                log.warning('%s', msg('integrations.browser_move_failed', error=kind(e), instance='-'))
            await asyncio.to_thread(aikey.refresh)
            await asyncio.to_thread(openai_key.refresh)
            await asyncio.to_thread(ai_connections.refresh)
            await asyncio.to_thread(vertex_connections.refresh)
            await asyncio.to_thread(sso_secret.refresh)
            await asyncio.to_thread(lambda: slack.enabled and slack_people.lookup())
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

        async def redraw_later() -> None:
            await asyncio.sleep(REDRAW_START_DELAY_S)
            while not await asyncio.to_thread(redraw_at_start):
                await asyncio.sleep(LANGUAGE_POLL_S)

        redrawing = asyncio.create_task(redraw_later())

        async def version_at_start() -> None:
            await asyncio.sleep(VERSION_START_DELAY)
            await asyncio.to_thread(versions.start)

        first_version = asyncio.create_task(version_at_start())
        marked = threading.Event()

        def marks_job() -> None:
            try:
                mutemarks.run_once(store)
                noticemail.answer_off_once(store.db)
            finally:
                marked.set()

        threading.Thread(target=marks_job, name='skopax-mute-marks', daemon=True).start()

        async def follow_sources() -> None:
            while True:
                await asyncio.sleep(sources.every)
                await asyncio.to_thread(follower.tick)

        following = asyncio.create_task(follow_sources())
        keying = asyncio.create_task(aikey.loop())
        openai_keying = asyncio.create_task(openai_key.loop())
        compat_keying = asyncio.create_task(ai_connections.loop())
        vertex_keying = asyncio.create_task(vertex_connections.loop())
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
            running = [t for t in (task, jobs, ticking, following, keying, openai_keying, compat_keying, vertex_keying, sso_keying, first_probe, first_version, deciding, redrawing) if t is not None]
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
            if auto_pool:
                try:
                    await asyncio.to_thread(stop_auto_pool)
                except Exception as e:
                    log.warning('the auto-assign messages did not finish: %s', kind(e))
            try:
                await asyncio.to_thread(backlinks.stop)
            except Exception as e:
                log.warning('the Jira back-links did not stop: %s', kind(e))
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

    def no_ai_key(provider: str) -> bool:
        return provider_keys.get(provider) is not None and not provider_keys[provider]()

    def needs_key(owner, field: str, role: str, provider: str) -> bool:
        used = ai_route(owner, field, role)[0] if isinstance(owner, (Investigator, SelfReview)) else settings.ai_provider
        return used == provider and no_ai_key(provider)

    feature_needs = {
        'investigator': (('anthropic_key', lambda: needs_key(investigator, 'investigate_model', 'card', ANTHROPIC)), ('openai_key', lambda: needs_key(investigator, 'investigate_model', 'card', OPENAI)),
                         ('mode', lambda: investigator.mode == 'off')),
        'chat': (('investigator', lambda: not investigator_ready()), ('chat_ui', lambda: not (settings.chat_ui and signon.active()))),
        'slack-autosend': (('slack_token', lambda: not slack.enabled),),
        'tag-report': (('slack_post', lambda: not slack.enabled), ('slack_post_autosend', lambda: not features.on('slack-autosend'))),
        'warn-digest': (('slack_post', lambda: not slack.enabled), ('slack_post_autosend', lambda: not features.on('slack-autosend'))),
        'slack-listener': (('slack_app_token', lambda: link.app_token() == ''), ('slack_token', lambda: not slack.enabled), ('investigator', lambda: not investigator_ready()),
                           ('listen_mode', lambda: settings.slack_listen == 'off')),
        'mcp': (('sso', lambda: not signon.active()),),
        'push': (('bot_token', lambda: link.token() == ''), ('push_users', lambda: not pusher.users)),
        'postdeploy': (('prometheus', lambda: not prom_watching()),),
        'langfuse-tracing': (('trace_instance', lambda: trace_target() is None),),
        'self-review': (('anthropic_key', lambda: needs_key(self_review, 'selfreview_model', 'review', ANTHROPIC)), ('openai_key', lambda: needs_key(self_review, 'selfreview_model', 'review', OPENAI)), ('self_project', lambda: not (settings.self_project and settings.self_log_index))),
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
    app.state.embed_indexer = embed_indexer
    app.state.semantic_searcher = semantic_searcher
    app.state.tracer = tracer
    app.state.feature_needs = feature_needs
    app.state.auto_send = auto
    app.state.tag_report = tag_report
    app.state.warn_digest = warn_digest
    app.state.follow_ups = follow_ups
    app.state.slack_people = slack_people
    app.state.auto_assign = auto_assign
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
    app.state.jira_fields = jira_fields
    app.state.backlinks = backlinks
    app.state.state_notes = notes
    app.state.enricher = enricher
    app.state.settle = settle
    app.state.pusher = pusher
    app.state.investigator = investigator
    app.state.probe = probe
    app.state.server_apply = server_apply
    app.state.post_fixes = post_fixes
    app.state.operators = operators
    app.state.gate = gate
    app.state.aikey = aikey
    app.state.ai_connections = ai_connections
    app.state.vertex_connections = vertex_connections
    app.state.openai_key = openai_key
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
            user_cache[name] = (now + USER_CACHE_SECONDS, found, False)
            return found

    def recheck_user(name: str) -> dict | None:
        with user_probes_guard:
            probe = user_probes.setdefault(name, threading.Lock())
        with probe:
            now = clock()
            cached = cached_user(name, now)
            held = cached[1] if cached is not None else None
            if (cached is not None and cached[2]) or now < retry_after['user']:
                return held
            try:
                found = users.get(name)
            except PyMongoError as e:
                with failure_guard:
                    paused = clock() < retry_after['user']
                    retry_after['user'] = clock() + FAILURE_PAUSE_SECONDS
                if not paused:
                    log.warning('user lookup failed, treating the request as signed out: %s', kind(e))
                return held
            user_cache[name] = (now + USER_CACHE_SECONDS, found, True)
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
        if doc is not None and payload.get('src') != 'sso' and payload.get('pg') != doc.get(PW_GEN):
            doc = recheck_user(name)
        if doc is None or doc.get('disabled'):
            return None
        if payload.get('src') == 'sso' and doc.get('source') != 'sso' and not link_admits(doc, payload.get('login')):
            return None
        if not cut_admits(doc, payload.get('src'), payload.get('login')):
            return None
        if payload.get('src') != 'sso' and payload.get('pg') != doc.get(PW_GEN):
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

    def password_made_before_cut(doc: dict, owner: dict) -> bool:
        cut = owner.get(LOCAL_AFTER)
        if doc.get('via') == 'sso' or not isinstance(cut, (int, float)) or isinstance(cut, bool):
            return False
        made = clocks.parse_stamp(doc.get('createdAt'))
        return made is None or made <= cut

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
            if password_made_before_cut(doc, owner):
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
        if getattr(request.scope.get('route'), 'path', '').startswith('/api/admin/openai-key'):
            return JSONResponse({'detail': msg('openaikey.invalid')}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
        if getattr(request.scope.get('route'), 'path', '').startswith('/api/admin/ai-connections'):
            return JSONResponse({'detail': msg('compat.invalid')}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
        if getattr(request.scope.get('route'), 'path', '').startswith('/api/admin/ai-vertex'):
            return JSONResponse({'detail': msg('vertex.invalid')}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
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
        return render_page({**information.page_state(store.load(), time.time()), 'archiveHours': settings.archive_hours, 'plugins': plugin_tiles(store.server_runs()['passes'])}, runtime.page_scripts())

    @app.get('/c/{num}')
    def card_page(num: int) -> RedirectResponse:
        return RedirectResponse(f'/#c={num}', status_code=status.HTTP_302_FOUND)

    @app.get('/api/cards/{num}')
    def get_card(num: int) -> dict:
        key = store.key_of_number(num)
        if key is None:
            raise HTTPException(status_code=404, detail=f'no card #{num}')
        try:
            card = store.anomaly(key)
            if not information.is_card(card):
                raise HTTPException(status_code=404, detail=f'no card #{num}')
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

    def workspace_icon_source() -> SsoIcon | None:
        return {'company': sso_icon, 'upload': workspace_icon}.get(settings.workspace_icon)

    def workspace_icon_url() -> str | None:
        source = workspace_icon_source()
        if source is None:
            return None
        try:
            return workspaceicon.url(source.status()['version'])
        except PyMongoError as e:
            log.warning('the workspace icon could not be read: %s', kind(e))
            return None

    @app.get(workspaceicon.PATH)
    def workspace_icon_image(v: str = '') -> Response:
        source = workspace_icon_source()
        if source is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'no icon')
        try:
            version = source.status()['version']
            held = source.data(version) if version and (not v or v == version) else None
        except PyMongoError as e:
            log.warning('the workspace icon could not be read: %s', kind(e))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('sso.icon_unavailable')) from None
        if held is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'no icon')
        cache = workspaceicon.CACHE if v == version else 'no-cache'
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
        set_session(resp, sessions.issue(found['email'], found['role'], 'local', found.get(PW_GEN)), sessions.idle_seconds)
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
        set_session(resp, sessions.issue(admin['email'], 'admin', 'local', admin.get(PW_GEN)), sessions.idle_seconds)
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
                gen = users.set_password(user.name, body.new)
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
        user_cache.pop(user.name, None)
        log.info('password changed by %s', user.name)
        resp = Response(status_code=status.HTTP_204_NO_CONTENT)
        keep_password_session(request, resp, user.name, gen, user.name)
        return resp

    def keep_password_session(request: Request, resp: Response, email: str, gen: str | None, by: str) -> None:
        sessions = get_sessions()
        if not gen or not sessions or by != email:
            return
        user = current_user(request)
        payload = sessions.unseal(request.cookies.get(SESSION_COOKIE), SESSION_KIND)
        if user is None or user.name != email or not payload or payload.get('src') == 'sso':
            return
        doc = stored_user(email)
        if doc is not None and not doc.get('disabled'):
            set_session(resp, sessions.issue(email, doc['role'], 'local', gen), sessions.idle_seconds)

    @app.patch('/api/me')
    def rename_me(request: Request, body: NameIn, authorization: Annotated[str | None, Header()] = None) -> dict:
        user = current_user(request) if not bearer_token(authorization) else None
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, msg('password.needs_session'))
        if not same_origin(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, msg('login.cross_site'))
        name = user_field(clean_name, 'name', body.name)
        held = users.get(user.name) or {}
        if held.get('name') == name and held.get('nameFrom') == 'self':
            return {'name': name}
        try:
            users.rename(user.name, name, 'self')
        except InvalidUser as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, e.text) from e
        finally:
            user_cache.pop(user.name, None)
        store.add_activity(ACCESS_KIND, user.name, msg('access.self_renamed', name=name))
        return {'name': name}

    def own_mail_user(request: Request, authorization: str | None) -> User:
        user = current_user(request) if not bearer_token(authorization) else None
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, msg('password.needs_session'))
        return user

    def own_mail_view(email: str) -> dict:
        doc = users.get(email)
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('noticemail.no_account'))
        return noticemail.choice(doc) | {'all': list(noticemail.KINDS), 'email': doc['_id'], 'ready': notice_mail.ready()}

    @app.get('/api/me/notice-mail')
    def get_notice_mail(request: Request, authorization: Annotated[str | None, Header()] = None) -> dict:
        instances.reread()
        return own_mail_view(own_mail_user(request, authorization).name)

    @app.put('/api/me/notice-mail')
    def put_notice_mail(request: Request, body: NoticeMailIn, authorization: Annotated[str | None, Header()] = None) -> dict:
        user = own_mail_user(request, authorization)
        if not same_origin(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, msg('login.cross_site'))
        if any(k not in noticemail.KINDS for k in body.kinds):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg('noticemail.kind'))
        instances.reread()
        if body.on and not notice_mail.ready():
            instances.reread(within=0)
        if body.on and not notice_mail.ready():
            raise HTTPException(status.HTTP_409_CONFLICT, msg('noticemail.no_server'))
        try:
            users.update(user.name, {'mailNotices': {'on': body.on, 'kinds': [k for k in noticemail.KINDS if k in body.kinds]}})
        except InvalidUser as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('noticemail.no_account')) from e
        finally:
            user_cache.pop(user.name, None)
        return own_mail_view(user.name)

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
            'canEditName': bool(user),
            'user': user.name if user else None,
            'name': name or None,
            'operator': bool(user and user.operator),
            'jira': jira.enabled,
            'jiraUrl': jira_site() if jira.enabled else '',
            'gitlab': gitlab.enabled,
            'codeHost': ' and '.join(n for n, h in zip(('GitLab', 'GitHub'), gitlab.hosts) if h.enabled),
            'workspaceLabel': settings.workspace_label,
            'task': {'project': settings.jira_project, 'projects': list(dict.fromkeys([*projects_of(settings), *(k for k in plugin_projects() if PROJECT_KEY.fullmatch(k) and k != settings.jira_tech_project)])), 'parent': settings.jira_parent, 'points': settings.jira_points, 'techProject': settings.jira_tech_project, 'techAssignee': bool(settings.jira_tech_assignee), 'titlePrefix': settings.jira_title_prefix,
                     'scopeField': bool(settings.jira_scope_field), 'scope': settings.jira_dev_scope, 'sprintField': bool(settings.jira_sprint_field), 'sprint': settings.jira_sprint_default},
            'slack': slack.enabled,
            'slackChannel': slack.channel,
            'tags': tag_info(),
            'routes': routes.info() if user and user.operator else [],
            'admin': bool(user and user.admin),
            'chat': chat_at_boot,
        }
        out['features'] = {f.id: features.on(f.id) for f in FEATURES}
        out['featureOff'] = {f.id: off_text(f.id) for f in FEATURES if not features.on(f.id)}
        out['instances'] = instance_menu(settings, live.instance_links(), workspace_icon_url())
        return out

    @app.get('/api/state')
    def get_state() -> dict:
        return {**information.page_state(store.load(), time.time()), 'archiveHours': settings.archive_hours}

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

    search_throttle = Throttle(db, limit=SEARCH_PER_MINUTE, window_seconds=SEARCH_WINDOW_S)

    def search_caller(request: Request, authorization: str | None) -> tuple[str, str]:
        try:
            actor = acting(request, authorization)
        except HTTPException as e:
            if e.status_code != status.HTTP_401_UNAUTHORIZED:
                raise
            if not settings.public_read:
                raise
            host = request.client.host if request.client and request.client.host else 'unknown'
            return 'anonymous', f'search:ip:{host}'
        return actor.role, f'search:{actor.name}'

    def search_retry_after(who: str) -> int:
        try:
            since = search_throttle.when(-SEARCH_WINDOW_S)
            oldest = search_throttle.col.find_one({'key': who[:KEY_MAX], 'at': {'$gt': since}}, sort=[('at', 1)])
            at = oldest['at'] if oldest else None
            if at is None:
                return SEARCH_WINDOW_S
            at = at if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)
            return max(1, min(SEARCH_WINDOW_S, math.ceil(at.timestamp() + SEARCH_WINDOW_S - search_throttle.clock())))
        except Exception:
            return SEARCH_WINDOW_S

    @app.get('/api/search')
    def get_search(request: Request, q: Annotated[str, Query(min_length=SEARCH_Q_MIN, max_length=SEARCH_Q_MAX)], kinds: Annotated[str | None, Query(max_length=100)] = None,
                   limit: Annotated[int, Query(ge=1, le=SEARCH_LIMIT_MAX)] = SEARCH_LIMIT_DEFAULT, authorization: Annotated[str | None, Header()] = None) -> dict:
        text = q.strip()
        if len(text) < SEARCH_Q_MIN:
            raise HTTPException(422, f'q is at least {SEARCH_Q_MIN} characters')
        wanted = [k.strip() for k in (kinds or '').split(',') if k.strip()]
        unknown = [k for k in wanted if k not in semantic_shape.UI_KINDS]
        if unknown:
            raise HTTPException(422, 'kinds is a comma list of ' + ', '.join(semantic_shape.UI_KINDS))
        role, who = search_caller(request, authorization)
        try:
            held = search_throttle.reserve(who)
        except PyMongoError as e:
            log.warning('search throttle check failed: %s', kind(e))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('db.unavailable')) from None
        if held is None:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg('search.throttled'), headers={'Retry-After': str(search_retry_after(who))})
        index_kinds = [i for k in wanted for i in semantic_shape.UI_KINDS[k]] if wanted else None
        if index_kinds is None or 'errtpl' in index_kinds:
            text = semantic_search.normalise_query(text)
        try:
            found = app.state.semantic_searcher.search(semantic_search.SearchContext.ui(role), text, index_kinds, limit)
        except semantic_search.SearchUnavailable:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('db.unavailable')) from None
        return {'semantic': found.semantic, 'note': found.note, 'rebuilding': found.rebuilding, 'items': semantic_shape.items_of(found)}

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
        if plugin.fields and not any(check in watched(plugin, i) for i in items):
            refused = 'checks.unchecked' if not check.once else 'checks.unchecked_once' if any(i.primary for i in items) else 'checks.primary_not_serving'
            raise HTTPException(status.HTTP_409_CONFLICT, msg(refused, check=check.label, name=plugin.name))
        job = f'{plugin.id}.{check.id}'
        scheduler.nudge(job)
        store.add_activity('check', by, msg('checks.run_now', check=check.label, by=by))
        return {'job': job}

    def unshot(rows: list[dict], admin: bool) -> list[dict]:
        if admin:
            return rows
        shown = [{k: v for k, v in r.items() if k != 'screenshots'} if r.get('outcome') in HELD_OUTCOMES else r for r in rows]
        return [{k: v for k, v in r.items() if k != 'guard'} | ({'guard': g} if r.get('trigger') != 'chat' and (g := public_guard(r['guard'])) is not None else {}) if isinstance(r.get('guard'), dict) else r
                for r in shown]

    @app.get('/api/screenshots/{shot_id}')
    def get_screenshot(request: Request, shot_id: str, authorization: Annotated[str | None, Header()] = None) -> Response:
        doc = store.screenshot(shot_id)
        if doc.get('runId') and not reads_access(request, authorization):
            outcome, shown = store.run_shot_ids(str(doc['runId']))
            if outcome is None or outcome in HELD_OUTCOMES or shot_id not in shown:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f'no screenshot {shot_id}')
        return Response(bytes(doc['data']), media_type=doc.get('mime') or 'image/jpeg', headers={'Cache-Control': 'private, max-age=86400, immutable'})

    @app.get('/api/investigations')
    def get_investigations(request: Request, key: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50, chats: bool = True, held: bool = False,
                           authorization: Annotated[str | None, Header()] = None) -> list[dict]:
        if held and not reads_access(request, authorization):
            raise HTTPException(status.HTTP_403_FORBIDDEN, 'Admins only')
        return unshot(store.list_investigations(key, limit, page=True, chats=chats, held=held), reads_access(request, authorization))

    @app.get('/api/card-history')
    def get_card_history(request: Request, key: Annotated[str, Query(min_length=1, max_length=400)], limit: Annotated[int, Query(ge=1, le=CARD_HISTORY_MAX)] = 100,
                         authorization: Annotated[str | None, Header()] = None) -> dict:
        try:
            card = store.anomaly(key)
        except NotFound:
            card = store.last_past_card(key)
        if not card or not information.is_card(card):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f'no card {key[:80]}')
        admin = reads_access(request, authorization)
        return {'key': key, 'actions': [a for a in store.card_actions(key, limit) if a.get('source') != 'chat'],
                'activity': unshot(store.card_activity(key, limit, () if admin else (ACCESS_KIND,)), admin)}

    @app.get('/api/card-runs')
    def get_card_runs(request: Request, authorization: Annotated[str | None, Header()] = None) -> dict:
        found = store.card_runs(a.get('key') for a in store.load().get('anomalies') or [])
        if reads_access(request, authorization):
            return found
        return {k: v | ({'latest': unshot([v['latest']], False)[0]} if isinstance(v.get('latest'), dict) else {}) for k, v in found.items()}

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
        return {'chats': store.chats(by, CHAT_LIST), 'cap': settings.chat_daily_cap, 'usedToday': chat_used(by), 'model': ai_route(investigator, 'chat_model', 'chat')[1] if isinstance(investigator, Investigator) else settings.chat_model}

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
        earlier = (chat['messages'] if chat else [])[-CHAT_THREAD:]
        planted = masked_scan('chat', '\n'.join([question] + [t for m in earlier for t in (m.get('text') or '', m.get('answer') or '')]), REQUESTS)
        planted += masked_asked('chat', '\n'.join([question] + [m.get('text') or '' for m in earlier]))
        question = redact(question)
        thread = []
        for m in earlier:
            thread += [{'who': 'person', 'text': redact(m.get('text') or '')}] + ([{'who': 'bot', 'text': redact(m['answer'])}] if m.get('answer') else [])
        chat_id = chat['id'] if chat else wanted_id if fresh else new_id()
        action = {'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': f'chat:{chat_id}', 'text': question, 'label': question[:300], 'cards': cards[:CHAT_CARDS],
                  'requestedBy': by, 'chat': {'id': chat_id}, 'thread': thread} | ({'via': via} if via else {}) | ({'mcpRole': role} if role else {}) | ({'askFlags': planted} if planted else {})
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
        refuse_information(body.key)
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
        refuse_information(body.key)
        try:
            fields = tags.toggled(store.anomaly(body.key), body.tag, body.on, by, iso(utc_now()))
        except tags.Refused as e:
            raise HTTPException(status_code=422, detail=e.text) from e
        out = tagging(body.key, fields, by, f"{'tagged' if body.on else 'untagged'} {tags.label(body.tag)}")
        card_posts.refresh(body.key)
        if body.on:
            auto_assign.card(body.key)
        return out

    @app.put('/api/business')
    def put_business(body: BusinessIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        store.anomaly(body.key)
        refuse_information(body.key)
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
        key = KEY_ALIASES.get(key, key)
        try:
            features.guard(key, value)
        except Locked as e:
            raise HTTPException(status.HTTP_409_CONFLICT, e.detail) from None
        if key == SLACK_CHANNEL_KEY and value is not None:
            found, problem = slack.find_channel(str(value))
            value = found or str(value).strip()
            problem = problem or slack.check_channel(value)
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
        if change['before'] != change['after'] and key.startswith('sso.'):
            store.add_activity(ACCESS_KIND, by, msg('sso.setting_changed', label=change['label'], key=key, before=cut(change['before'] or '\u2013'), after=cut(change['after'] or '\u2013')))
        elif change['before'] != change['after']:
            store.add_activity('admin', by, f"{change['label']} ({key}): {cut(change['before'] or '\u2013')} \u2192 {cut(change['after'] or '\u2013')}" + ('' if entry['live'] else ' \u00b7 applies after a restart'))
        log.info('admin setting %s %s by %s', key, 'reset' if value is None else 'set', by)
        return {'item': entry, 'change': change}

    @app.get('/api/admin/users')
    def get_admin_users(by: Annotated[str, Depends(require_admin)]) -> list[dict]:
        now = clock()
        used = users.mcp_uses()
        return [admin_row(doc, now) | {'mcp': mcp_mark(doc, used.get(doc['_id']))} for doc in users.list()]

    @app.post('/api/admin/users', status_code=status.HTTP_201_CREATED)
    def post_admin_user(body: UserIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        email = user_field(clean_email, 'email', body.email)
        role = user_field(clean_role, 'role', body.role)
        named = body.name != ''
        name = user_field(clean_name, 'name', body.name) if named else email.partition('@')[0]
        name_from = 'admin' if named else 'default'
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
            made = users.add_sso(email, name, role, name_from) if password is None else users.create(email, name, role, password, link_by=by if body.sso else None, name_from=name_from)
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
        if body.ssoLink is not None and current.get('source') != 'local' and not to_local:
            raise FieldRefused('ssoLink', msg('access.sso_link_local_only'))
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

    def sso_to_local(key: str, body: UserPatch, fields: dict, by: str, current_from: str | None = None) -> dict:
        link = sso_domain_allowed(key)
        try:
            made = users.make_local(key, body.password, by, fields, link)
        except LastAdmin:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.last_admin')) from None
        finally:
            user_cache.pop(key, None)
            mcp_auth.forget(key)
        if made is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg('access.changed_meanwhile'))
        store.add_activity(ACCESS_KIND, by, msg('access.user_password', email=key))
        if current_from in IDP_SOURCES and made.get('roleFrom') == 'admin':
            store.add_activity(ACCESS_KIND, by, msg('access.user_role_admin_set', email=key, role=made.get('role')))
        if link:
            store.add_activity(ACCESS_KIND, by, msg('access.sso_linked', email=key, days=LINK_PENDING_S // 86400))
        return made

    def unsure_sso_only(key: str, by: str) -> None:
        log.error('the change of %s to SSO only failed half way and could not be put back, so the person may now sign in with SSO only', key)
        try:
            store.add_activity(ACCESS_KIND, by, msg('access.user_sso_only', email=key))
        except PyMongoError as e:
            log.error('the SSO-only activity line for %s was not saved: %s', key, kind(e))

    def make_sso_only(key: str, body: UserPatch, current: dict, by: str) -> dict:
        if body.ssoOnly is not True or body.model_dump(exclude={'ssoOnly'}, exclude_none=True):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg('access.user_fields'))
        if current.get('source') != 'local':
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.sso_only_local'))
        if not signon.active():
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.sso_only_off'))
        if not sso_domain_allowed(key):
            raise FieldRefused('ssoOnly', msg('access.sso_only_domain'))
        try:
            made = users.make_sso_only(key)
        except NoPasswordAdmin:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.sso_only_last_password_admin')) from None
        except ChangeReverted:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('access.not_changed')) from None
        except ChangeUnsure:
            unsure_sso_only(key, by)
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, msg('access.sso_only_unsure')) from None
        finally:
            user_cache.pop(key, None)
            mcp_auth.forget(key)
        if made is None:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.sso_only_local'))
        store.add_activity(ACCESS_KIND, by, msg('access.user_sso_only', email=key))
        return admin_row(made, clock())

    @app.patch('/api/admin/users/{email}')
    def patch_admin_user(email: str, body: UserPatch, request: Request, response: Response, by: Annotated[str, Depends(require_admin)]) -> dict:
        key = email.strip().lower()
        current = users.get(key)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('access.unknown_user'))
        if body.ssoOnly is not None:
            return make_sso_only(key, body, current, by)
        checked_patch(key, body, current)
        to_local = body.password is not None and current.get('source') == 'sso'
        try:
            fields, unset = users.prepare_change(body.role, body.disabled, None if to_local else body.password, current)
        except InvalidUser as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.text) from e
        except IdpManaged:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.role_from_idp')) from None
        if body.name is not None:
            name = user_field(clean_name, 'name', body.name)
            if current.get('name') == name and current.get('nameFrom') == 'admin':
                body = body.model_copy(update={'name': None})
            else:
                fields |= {'name': name, 'nameFrom': 'admin'}
        if to_local:
            current = sso_to_local(key, body, fields, by, current.get('roleFrom'))
            log_change(key, body.model_copy(update={'password': None}), by)
            fields = {}
        if fields:
            change_user(key, fields, unset, body, by)
            keep_password_session(request, response, key, fields.get(PW_GEN), by)
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
        if leaves_admin(fields) and users.is_last_password_admin(key):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.last_password_admin'))
        only = {'roleFrom': {'$nin': list(IDP_SOURCES)}} if 'role' in fields else None
        try:
            if users.guarded_update(key, fields, unset, only=only) is None:
                if only is not None and users.get(key) is not None:
                    raise HTTPException(status.HTTP_409_CONFLICT, msg('access.role_from_idp'))
                raise HTTPException(status.HTTP_404_NOT_FOUND, msg('access.unknown_user'))
        except LastAdmin:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.last_admin')) from None
        except NoPasswordAdmin:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('access.last_password_admin')) from None
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
        if body.name is not None:
            store.add_activity(ACCESS_KIND, by, msg('access.user_renamed', email=key, name=clean_name(body.name)))

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

    def scope_warning() -> dict | None:
        found = store.email_scope()
        return found if found is not None and tag_report.wanted else None

    def scope_marked(items: list[dict]) -> list[dict]:
        if scope_warning() is None:
            return items
        return [i | {'problem': msg('tags.scope_missing')} if i.get('key') == tagreport.KEY else i for i in items]

    def instance_icon_view() -> dict:
        try:
            return {'mode': settings.workspace_icon, 'company': sso_icon.status(), 'upload': workspace_icon.status(), 'max': SSO_ICON_MAX}
        except PyMongoError as e:
            log.warning('the workspace icon could not be read: %s', kind(e))
            return {'mode': settings.workspace_icon, 'company': None, 'upload': None, 'max': SSO_ICON_MAX, 'problem': msg('sso.icon_unavailable')}

    def instance_name_view() -> dict:
        label = (settings.workspace_label or '').strip()
        source = live.labels.get('core.workspace-label') or live.labels.get('core.env-label', 'environment')
        return {'name': current_instance(settings), 'label': label, 'from': source if label else '', 'icon': instance_icon_view()}

    def channel_named(item: dict) -> dict:
        if item.get('key') != SLACK_CHANNEL_KEY or not CHANNEL_ID.fullmatch(str(item.get('value') or '')) or not slack.token:
            return item
        name = slack.channel_name(item['value'])
        return {**item, 'channelName': name} if name else item

    @app.get('/api/admin/settings')
    def get_admin_settings(by: Annotated[str, Depends(require_admin)]) -> dict:
        return {'items': [channel_named(i) for i in scope_marked(live.entries())], 'rev': live.tunables.rev(), 'refreshSeconds': live.every, 'instance': instance_name_view()}

    def responsible_email(email: str) -> bool:
        domain = email.rpartition('@')[2]
        return len(email) <= EMAIL_MAX and bool(EMAIL.match(email)) and '@' not in email.partition('@')[2] and '.' in domain.strip('.') and not domain.startswith('.') and not domain.endswith('.')

    def all_people() -> dict[str, str]:
        return {u['email']: u.get('name') or '' for u in users.list() if u and u.get('email')}

    def owner_name(email: str, everyone: dict[str, str]) -> str:
        return next((n for n in (everyone.get(email), slack_ids.name(email)) if n and '@' not in n), email.split('@')[0])

    def owner_rows(emails: list[str], named: dict[str, str], everyone: dict[str, str] | None = None) -> list[dict]:
        everyone = all_people() if everyone is None else everyone
        return [{'email': e, 'name': owner_name(e, everyone), 'slack': slack_ids.peek(e)} | ({} if e in named or e not in everyone else {'stale': True}) for e in emails]

    def tag_view(tag: str, label: str, held: dict[str, list[str]], named: dict[str, str], off: set[str] | None = None, colors: dict[str, int] | None = None,
                 everyone: dict[str, str] | None = None) -> dict:
        off = store.tag_auto_off() if off is None else off
        colors = store.tag_colors() if colors is None else colors
        return {'id': tag, 'label': label, 'responsibles': owner_rows(held.get(tag, []), named, everyone), 'autoAssign': tag not in off, 'autoReady': auto_assign.ready(tag, held, named), 'color': colors.get(tag)}

    def tag_info() -> list[dict]:
        colors = store.tag_colors()
        return [t | ({'color': colors[t['id']]} if t['id'] in colors else {}) for t in tags.info()]

    def hue_name(n: int | None) -> str:
        return 'default' if n is None else TAG_HUE_NAMES[n]

    def people_named(names: list[str]) -> str:
        everyone = all_people()
        return ', '.join(owner_name(e, everyone) for e in names) or '\u2013'

    @app.get('/api/admin/tags')
    def get_admin_tags(by: Annotated[str, Depends(require_admin)]) -> dict:
        listed = [u for u in users.list() if u and u.get('email')]
        everyone = {u['email']: u.get('name') or '' for u in listed}
        named = {u['email']: u.get('name') or '' for u in listed if not u.get('disabled')}
        held = store.tag_owners()
        off, colors = store.tag_auto_off(), store.tag_colors()
        return {'tags': [tag_view(tag, label, held, named, off, colors, everyone) for tag, label in tags.all_tags().items()], 'canEdit': True,
                'users': [{'email': u['email'], 'name': u.get('name') or u['email'].split('@')[0], 'disabled': bool(u.get('disabled'))} for u in listed if u and u.get('email')],
                'reportAt': settings.tag_report_at, 'emailScopeMissing': scope_warning() is not None}

    @app.get('/api/admin/people')
    def get_admin_people(by: Annotated[str, Depends(require_admin)], q: str = '') -> dict:
        wanted = q.strip()[:tagreport.QUERY_MAX]
        if len(wanted) < tagreport.QUERY_MIN:
            return {'people': [], 'slack': 'idle'}
        everyone, active = all_people(), active_people()
        rows = [{'email': e, 'name': n or e.split('@')[0], 'source': 'skopax'} for e, n in active.items()]
        rows += [{'email': e, 'name': owner_name(e, everyone), 'source': 'responsible'} for held in store.tag_owners().values() for e in held if e in active or e not in everyone]
        found, state = slack_people.lookup()
        rows += [p | {'source': 'slack'} for p in found if p['email'] in active or p['email'] not in everyone]
        return {'people': tagreport.find_people(wanted, rows), 'slack': state}

    @app.put('/api/admin/tags/{tag}/responsibles')
    def put_tag_owners(tag: str, body: TagOwnersIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        known = tags.all_tags()
        if tag not in known:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('tags.no_tag', tag=tag[:40]))
        wanted = list(dict.fromkeys(e.strip().lower() for e in body.emails if e.strip()))
        if len(wanted) > tagreport.OWNERS_MAX:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg('tags.too_many', n=tagreport.OWNERS_MAX))
        invalid = [e for e in wanted if not responsible_email(e)]
        if invalid:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg('tags.bad_email', email=cut(invalid[0], 80)))
        named = active_people()
        before = store.tag_owners().get(tag, [])
        if wanted != before:
            store.set_tag_owners(tag, wanted)
            slack_ids.check_all([e for e in wanted if e not in before])
            everyone = all_people()
            live.tunables.write_change(f'tags.{tag}.responsibles', ', '.join(owner_name(e, everyone) for e in before) or None, ', '.join(owner_name(e, everyone) for e in wanted) or None, by, owners=True, tag=tag)
            store.add_activity(ACCESS_KIND, by, f"Responsibles of {known[tag]}: {people_named(before)} \u2192 {people_named(wanted)}"[:300])
            log.info('responsibles of tag %s changed by %s', tag, by)
        return tag_view(tag, known[tag], store.tag_owners(), named)

    @app.put('/api/admin/tags/{tag}/auto-assign')
    def put_tag_auto(tag: str, body: TagAutoIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        known = tags.all_tags()
        if tag not in known:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('tags.no_tag', tag=tag[:40]))
        was = tag not in store.tag_auto_off()
        if body.on != was:
            store.set_tag_auto(tag, body.on)
            store.add_activity(ACCESS_KIND, by, f"Auto-assign of {known[tag]}: {'on' if was else 'off'} \u2192 {'on' if body.on else 'off'}"[:300])
            log.info('auto-assign of tag %s turned %s by %s', tag, 'on' if body.on else 'off', by)
        return tag_view(tag, known[tag], store.tag_owners(), active_people())

    @app.put('/api/admin/tags/{tag}/color')
    def put_tag_color(tag: str, body: TagColorIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        known = tags.all_tags()
        if tag not in known:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('tags.no_tag', tag=tag[:40]))
        was = store.tag_colors().get(tag)
        if body.color != was:
            store.set_tag_color(tag, body.color)
            store.add_activity(ACCESS_KIND, by, f"Colour of {known[tag]}: {hue_name(was)} \u2192 {hue_name(body.color)}"[:300])
            log.info('colour of tag %s changed by %s', tag, by)
        return tag_view(tag, known[tag], store.tag_owners(), active_people())

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

    def mcp_status(sso_on: bool) -> dict:
        on = bool(features.on('mcp'))
        explicit = {'client-id': (settings.mcp_client_id or '').strip() not in ('', MCP_CLIENT_ID), 'audience': bool((settings.mcp_audience or '').strip()),
                    'issuer': bool((settings.mcp_issuer or '').strip())}
        values = {'client-id': mcp_auth.client_id(), 'audience': mcp_auth.audiences()[0], 'issuer': mcp_auth.issuer()}
        missing = 'sso' if not sso_on else 'switch' if not on else 'issuer' if not values['issuer'] else None
        seen = users.mcp_summary(MCP_DAYS)
        state = 'not-set-up' if missing else 'connected' if seen['people'] else 'waiting'
        return {'state': state, 'missing': missing, 'ssoOn': sso_on, 'on': on, 'mounted': True, 'days': MCP_DAYS,
                'values': {k: {'value': v, 'default': not explicit[k]} for k, v in values.items()}, **seen}

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
                'mcp': mcp_status(config.active),
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

    async def icon_upload(request: Request) -> tuple[bytes, str]:
        declared = request.headers.get('content-length', '')
        if declared.isdigit() and int(declared) > SSO_ICON_MAX:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, msg('sso.icon_too_big', max=SSO_ICON_MAX // 1024))
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > SSO_ICON_MAX:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, msg('sso.icon_too_big', max=SSO_ICON_MAX // 1024))
            raw += chunk
        try:
            return bytes(raw), await run_in_threadpool(icon_check, bytes(raw))
        except IconRefused as e:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE if e.key == 'sso.icon_too_big' else status.HTTP_422_UNPROCESSABLE_CONTENT,
                                msg(e.key, max=SSO_ICON_MAX // 1024) if e.key == 'sso.icon_too_big' else msg(e.key)) from None

    @app.put('/api/admin/sso/icon')
    async def put_admin_sso_icon(request: Request, by: Annotated[str, Depends(require_admin)]) -> dict:
        raw, kind_of = await icon_upload(request)
        try:
            await run_in_threadpool(sso_icon.set, raw, kind_of, by)
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

    @app.put('/api/admin/workspace/icon')
    async def put_admin_workspace_icon(request: Request, by: Annotated[str, Depends(require_admin)]) -> dict:
        raw, kind_of = await icon_upload(request)
        try:
            await run_in_threadpool(workspace_icon.set, raw, kind_of, by)
        except PyMongoError as e:
            log.warning('the workspace icon was not stored: %s', kind(e))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('sso.icon_unavailable')) from None
        try:
            await run_in_threadpool(store.add_activity, ACCESS_KIND, by, msg('workspace.icon_saved', type=kind_of, size=len(raw)))
        except PyMongoError as e:
            log.warning('the workspace icon upload was not written to the history: %s', kind(e))
        log.info('workspace icon uploaded by %s (%s, %d bytes)', by, kind_of, len(raw))
        return {'icon': await run_in_threadpool(instance_icon_view)}

    @app.delete('/api/admin/workspace/icon')
    def delete_admin_workspace_icon(by: Annotated[str, Depends(require_admin)]) -> dict:
        try:
            gone = workspace_icon.clear()
        except PyMongoError as e:
            log.warning('the workspace icon was not removed: %s', kind(e))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg('sso.icon_unavailable')) from None
        if gone:
            log.info('workspace icon removed by %s', by)
            try:
                store.add_activity(ACCESS_KIND, by, msg('workspace.icon_removed'))
            except PyMongoError as e:
                log.warning('the workspace icon removal was not written to the history: %s', kind(e))
        return {'icon': instance_icon_view()}

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

    def running_guard() -> Guard | None:
        found = getattr(investigator, 'guard', None) if isinstance(investigator, Investigator) else None
        return found if isinstance(found, Guard) else None

    def ai_routes() -> dict[str, tuple[str, str, str | None]]:
        found: dict[str, tuple[str, str, str | None]] = {}
        if isinstance(investigator, Investigator):
            found |= {use: investigator.use_route(use) for use in ('card', 'slack', 'chat')}
            for use, sev, field in (('critical', 'crit', 'investigate_model_critical'), ('high', 'high', 'investigate_model_high')):
                provider, model, fallback, _, _ = investigator.card_route({sev})
                found[use] = (provider, model, fallback if getattr(settings, field) else None)
        else:
            found |= {use: (settings.ai_provider, settings.investigate_model, None) for use in ('card', 'critical', 'high', 'slack', 'chat')}
        found['gate'] = ai_route(gate, 'investigate_gate_model', 'gate')
        found['review'] = ai_route(self_review, 'selfreview_model', 'review')
        found['guard'] = ai_route(running_guard() or gate, 'investigate_guard_model', 'guard')
        try:
            plugin_model = live.plugin_value(aiuses.PLUGIN_MODEL_KEY)
        except KeyError:
            plugin_model = ''
        found['plugin'] = plugin_ai.route(plugin_model or default_model(settings.ai_provider, 'plugin'), aiuses.PLUGIN_MODEL_KEY.split('.')[0])
        return found

    def routed_providers() -> tuple[str, ...]:
        return tuple(sorted({provider for provider, _, _ in ai_routes().values() if keyed_clients.keyed(provider)}))

    def ai_routes_listed() -> list[dict]:
        found = ai_routes()
        return [{'use': use, 'label': label, 'provider': found[use][0], 'model': found[use][1], 'fallbackFrom': found[use][2]} for use, label, _ in aiuses.USES if use != 'plugin']

    def plugin_model_notices() -> list[dict]:
        out = []
        for t in live.known().values():
            if t.plugin is None or not t.key.endswith('.model') or not live.plugin_value_given(t.key):
                continue
            provider, model, fallback = plugin_ai.route(str(live.plugin_value(t.key) or ''), t.plugin)
            if fallback or (model and provider != settings.ai_provider and not keyed_clients.keyed(provider)) or (str(provider).startswith('vertex:') and model and not known_model(str(model))):
                out.append({'id': f'ai-plugin:{t.key}', 'level': 'warn', 'text': msg('ai.use_problem', use=f'{t.group}: {t.label}', problem=aiuses.problem_of('plugin', provider, model, fallback, settings.ai_provider, keyed=keyed_clients.keyed))})
        return out

    @app.get('/api/admin/ai-uses')
    def get_admin_ai_uses(by: Annotated[str, Depends(require_admin)]) -> dict:
        live.refresh(force=True)
        guard = running_guard()
        return {'uses': aiuses.rows(ai_routes(), settings.ai_provider, store.ledger_since(aiuses.since(clock())), keyed_clients.capabilities, keyed_clients.keyed, settings,
                                    problems={aiuses.GUARD: guard.problem()} if guard else None)}

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
            if state['leftOut']:
                more = state['leftOutMore']
                shown = ', '.join(state['leftOut']) + (msg('settings.source_left_out_more', count=more) if more else '')
                items.append({'id': f'source-left-out:{label}', 'level': 'info', 'text': msg('settings.source_left_out', keys=shown)})
        if aikey.status()['unreadable']:
            items.append({'id': 'ai-key', 'level': 'warn', 'text': msg('aikey.unreadable')})
        if openai_key.status()['unreadable']:
            items.append({'id': 'openai-key', 'level': 'warn', 'text': msg('openaikey.unreadable')})
        items += live.chat_effort_notices()
        items += aiuses.notices(ai_routes_listed(), settings.ai_provider, keyed_clients.keyed) + plugin_model_notices()
        items += [{'id': f'ai-connection:{name}', 'level': 'warn', 'text': msg('compat.unreadable', name=name)} for name in ai_connections.unreadable()]
        items += [{'id': f'ai-name-clash:{name}', 'level': 'warn', 'text': msg('vertex.name_clash', name=name)} for name in sorted(set(vertex_connections.clashes()) | set(ai_connections.clashes()))]
        items += [{'id': f'ai-vertex:{name}', 'level': 'warn', 'text': msg('vertex.unreadable', name=name)} for name in vertex_connections.unreadable()]
        items += [{'id': f'ai-vertex-token:{name}', 'level': 'warn', 'text': msg('vertex.token_path_blocked' if vertex_ai.token_dir_ok(vertex_connections.token_dir()) else 'vertex.token_dir_invalid', name=name)} for name in vertex_connections.blocked()]
        slack_scopes = store.slack_scopes() if slack.enabled else {'missing': []}
        if slack_scopes['missing']:
            items.append({'id': 'slack-scopes', 'level': 'warn', 'text': msg('integrations.slack_scopes', scopes=', '.join(slack_scopes['missing'])), 'at': slack_scopes['at']})
        items += [{'id': f'route-gave-up:{r["id"]}', 'level': 'warn', 'text': msg('routes.gave_up_one' if r['tries'] == 1 else 'routes.gave_up', plugin=r['plugin'], route=r['route'], count=r['count'], tries=r['tries'], reason=r['reason'])}
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
        scope = scope_warning()
        if scope is not None:
            items.append({'id': 'tag-report-scope', 'level': 'warn', 'text': msg('tags.scope_missing'), 'at': scope['at']})
        update = versions.notice()
        if update is not None:
            items.append(update)
        return {'items': items}

    @app.get('/api/admin/features')
    def get_admin_features(by: Annotated[str, Depends(require_admin)]) -> dict:
        live.refresh(force=True)
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
        everyone = all_people()

        def named(text: str) -> str:
            return ADDRESS.sub(lambda m: owner_name(m.group(0).lower(), everyone), text)

        access = [{'id': a['id'], 'key': ACCESS_KIND, 'access': True, 'at': a['t'], 'by': named(str(a.get('who') or '')), 'text': named(a['text'])}
                  for a in store.list_activity(None, limit, only=(ACCESS_KIND,)) if not a['text'].startswith(OWNERS_LINE) and not SSO_SETTING_LINE.search(a['text'])]
        settings_rows = [h | {f: named(h[f]) for f in ('old', 'new') if isinstance(h.get(f), str)} if h.get('owners') else h for h in live.tunables.history(limit)]
        return sorted(settings_rows + access, key=lambda h: str(h.get('at') or ''), reverse=True)[:limit]

    def key_routes(path: str, key: AiKey, provider: str) -> None:
        prefix = key.prefix

        @app.get(path)
        def get_admin_ai_key(by: Annotated[str, Depends(require_admin)]) -> dict:
            return key.status()

        @app.put(path)
        def put_admin_ai_key(body: AiKeyIn, by: Annotated[str, Depends(require_admin)]) -> dict:
            try:
                found = key.set(body.key, by)
            except InvalidKey:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg(prefix + 'invalid')) from None
            store.add_activity(ADMIN_KIND, by, msg(prefix + 'set'))
            log.info('%s API key set in Admin by %s', key.label, by)
            return found

        @app.delete(path, status_code=status.HTTP_204_NO_CONTENT)
        def delete_admin_ai_key(by: Annotated[str, Depends(require_admin)]) -> Response:
            if key.clear(by):
                store.add_activity(ADMIN_KIND, by, msg(prefix + 'cleared'))
                log.info('%s API key cleared in Admin by %s', key.label, by)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        @app.post(path + '/test')
        def post_admin_ai_key_test(body: AiKeyTestIn, by: Annotated[str, Depends(require_admin)]) -> dict:
            try:
                card = ai_route(investigator, 'investigate_model', 'card')[1] if isinstance(investigator, Investigator) else settings.investigate_model
                return key.test(body.key, card if provider_of(card) == provider else default_model(provider, 'card'), lambda: store.add_activity(ADMIN_KIND, by, msg(prefix + 'test_used')))
            except InvalidKey:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg(prefix + 'invalid')) from None
            except TestBusy:
                raise HTTPException(status.HTTP_409_CONFLICT, msg(prefix + 'test_busy')) from None
            except Exception as e:
                log.warning('the %s connection test could not run: %s', key.label, kind(e))
                return {'ok': False, 'summary': msg(prefix + 'test_failed'), 'ms': 0}

    key_routes('/api/admin/anthropic-key', aikey, ANTHROPIC)
    key_routes('/api/admin/openai-key', openai_key, OPENAI)

    @app.get('/api/admin/ai-connections')
    def get_admin_ai_connections(by: Annotated[str, Depends(require_admin)]) -> dict:
        live.refresh(force=True)
        return {'items': ai_connections.views(), 'allowPrivate': bool(settings.allow_private_ai)}

    @app.put('/api/admin/ai-connections/{name}')
    def put_admin_ai_connection(name: str, body: AiConnectionIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        try:
            found = ai_connections.put(name, body.model_dump(), by)
        except compat_ai.Refused as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.text) from None
        store.add_activity(ADMIN_KIND, by, msg('compat.set', name=name))
        log.info('OpenAI-compatible AI connection %s saved in Admin by %s', name, by)
        return found

    @app.delete('/api/admin/ai-connections/{name}', status_code=status.HTTP_204_NO_CONTENT)
    def delete_admin_ai_connection(name: str, by: Annotated[str, Depends(require_admin)]) -> Response:
        if ai_connections.delete(name, by):
            store.add_activity(ADMIN_KIND, by, msg('compat.cleared', name=name))
            log.info('OpenAI-compatible AI connection %s removed in Admin by %s', name, by)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post('/api/admin/ai-connections/{name}/test')
    def post_admin_ai_connection_test(name: str, body: AiConnectionTestIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        try:
            return ai_connections.test(name, body.model_dump(exclude_none=True), lambda: store.add_activity(ADMIN_KIND, by, msg('compat.test_used', name=name)))
        except compat_ai.Refused as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.text) from None
        except UnknownConnection:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('compat.unknown')) from None
        except TestBusy:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('compat.test_busy')) from None
        except Exception as e:
            log.warning('the connection test of %s could not run: %s', name, kind(e))
            return {'ok': False, 'tools': False, 'summary': msg('compat.test_failed'), 'ms': 0}

    @app.get('/api/admin/ai-vertex')
    def get_admin_ai_vertex_list(by: Annotated[str, Depends(require_admin)]) -> dict:
        return {'items': vertex_connections.views(), 'regions': list(vertex_ai.REGIONS)}

    @app.get('/api/admin/ai-vertex/{name}')
    def get_admin_ai_vertex(name: str, by: Annotated[str, Depends(require_admin)]) -> dict:
        found = vertex_connections.get(name)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('vertex.unknown'))
        return found.view()

    @app.put('/api/admin/ai-vertex/{name}')
    def put_admin_ai_vertex(name: str, body: VertexConnectionIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        try:
            found = vertex_connections.put(name, body.model_dump(), by)
        except compat_ai.Refused as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.text) from None
        store.add_activity(ADMIN_KIND, by, msg('vertex.set', name=name))
        log.info('Vertex AI connection %s saved in Admin by %s', name, by)
        return found

    @app.delete('/api/admin/ai-vertex/{name}', status_code=status.HTTP_204_NO_CONTENT)
    def delete_admin_ai_vertex(name: str, by: Annotated[str, Depends(require_admin)]) -> Response:
        if vertex_connections.delete(name, by):
            store.add_activity(ADMIN_KIND, by, msg('vertex.cleared', name=name))
            log.info('Vertex AI connection %s removed in Admin by %s', name, by)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post('/api/admin/ai-vertex/{name}/test')
    def post_admin_ai_vertex_test(name: str, body: VertexConnectionTestIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        try:
            return vertex_connections.test(name, body.model_dump(exclude_none=True), lambda: store.add_activity(ADMIN_KIND, by, msg('vertex.test_used', name=name)))
        except compat_ai.Refused as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.text) from None
        except UnknownConnection:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('vertex.unknown')) from None
        except TestBusy:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('vertex.test_busy')) from None
        except Exception as e:
            log.warning('the connection test of %s could not run: %s', name, kind(e))
            return {'ok': False, 'tools': False, 'summary': msg('vertex.test_failed'), 'ms': 0}

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
            return {'id': found.id, 'module': found.module, 'reason': found.reason, 'origin': origin, 'standing': standing_of(found, plugin_state.enabled)}
        if found.state == 'broken':
            return {'id': found.id, 'module': found.module, 'reason': found.reason, 'enabled': plugin_state.enabled(found.id), 'switchedAt': plugin_state.stamp(found.id), 'origin': origin, 'standing': standing_of(found, plugin_state.enabled)}
        listed = instances.all(found.id)
        return {**describe(found.plugin, registry.icon(found.id) is not None), 'state': found.state, 'standing': standing_of(found, plugin_state.enabled), 'reason': found.reason, 'enabled': plugin_state.enabled(found.id), 'switchedAt': plugin_state.stamp(found.id),
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
        plugin_state.refresh()
        instances.reread(within=0)
        passes = store.server_runs()['passes']
        return {'edition': registry.edition(), 'secretsKey': 'env' if base_settings.secret_key else 'generated', 'plugins': [plugin_row(x, passes) for x in registry.loaded()]}

    @app.get('/api/integrations')
    def get_integrations(by: Annotated[str, Depends(require_login)]) -> dict:
        plugin_state.refresh()
        instances.reread(within=0)
        passes = store.server_runs()['passes']
        return {'edition': registry.edition(), 'readOnly': True,
                'plugins': [reader_row(plugin_row(x, passes)) for x in registry.loaded()]}

    @app.put('/api/admin/plugins/{plugin_id}')
    def put_admin_plugin(plugin_id: str, body: PluginSwitchIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        plugin = plugin_usable(plugin_id) if body.enabled else plugin_loaded(plugin_id)
        plugin_state.refresh()
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
            made = instances.create(plugin_id, body.name, only_browser(dict(body.values)) if plugin_id == 'website' else body.values, body.enabled, by, once_off(plugin, not instances.all(plugin_id), body.offChecks))
        except InstanceExists:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('instances.exists', name=body.name)) from None
        except InvalidInstance as e:
            raise FieldRefused(e.field, e.args[0]) from None
        if plugin_id == MCPTOOLS_ID:
            mcptools_routes.after_create(mcptools_deps, made.name)
        if plugin_id == 'website' and body.values.get('browser') == '':
            mark_blank_browser(made.name, True)
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
            if plugin_id == 'website':
                if 'browser' in given:
                    mark_blank_browser(name, given['browser'] == '')
                else:
                    given = only_browser(given, before.values, blank_browser(name))
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

    def adopt_browser(url: str, by: str, prefer: str = '') -> tuple:
        headless = plugin_usable('browser')
        held = instances.all('browser')
        same = next((i for i in held if str(i.values.get('url') or '').strip() == url), None)
        if same is not None:
            return same, False
        taken = {i.name for i in held}
        made = None
        for chosen in [n for n in (prefer, 'headless', *(f'headless-{k}' for k in range(2, 100))) if n and n not in taken]:
            try:
                made = instances.create('browser', chosen, {'url': url}, True, by)
                break
            except InstanceExists:
                continue
            except InvalidInstance as e:
                if e.field != 'name':
                    raise
        if made is None:
            raise InvalidInstance('name', msg('integrations.browser_name_none'))
        store.add_activity(ADMIN_KIND, by, msg('integrations.instance_created', plugin=headless.name, instance=made.name))
        store.add_activity(ADMIN_KIND, by, msg('integrations.instance_on', plugin=headless.name, instance=made.name))
        sweeper.revive('browser', made.name, checks=[c.id for c in watched(headless, made)])
        nudge_jobs(headless)
        return made, True

    def tidy_browser_state(site_names: list[str]) -> None:
        infos = [i.info() for i in instances.all('browser') if i.enabled and not i.problem]
        valid = {(f"browser-down|{urlsplit(str(i.config.get('url') or '')).hostname or ''}" + ('' if i.primary else f'@{i.name}'), None if i.primary else i.name) for i in infos}
        segments = {None if i.primary else i.name for i in infos}
        for _ in range(5):
            state = store.load()
            before = copy.deepcopy(state)
            at = iso(utc_now())
            state['browser'] = [r for r in state.get('browser') or [] if not (isinstance(r, dict) and r.get('plugin') == 'website')] if 'browser' in state else state.get('browser')
            if state.get('browser') is None:
                state.pop('browser', None)
            for table in ('sources', 'serverPasses'):
                for name in site_names:
                    (state.get(table) or {}).pop(f'browser-{name}', None)
            for a in state.get('anomalies') or []:
                if a.get('status') != 'open' or a.get('src') != 'browser':
                    continue
                segment = a.get('cluster') or None
                key = str(a.get('key') or '')
                if key.startswith('browser-down|'):
                    live = (key, segment) in valid
                elif key.startswith('access|-/website|browser'):
                    live = segment in segments
                else:
                    continue
                if live:
                    continue
                engine.expire(a, at, msg('integrations.browser_card_closed'))
                state['history'] = [{'t': at, 'key': a['key'], 'ev': 'cleared', 'sev': a['sev'], 'text': a['short'], 'muted': bool(a.get('muted'))}] + state.get('history', [])
            if state == before:
                return
            try:
                store.save_state(state, before.get('rev', 0), before)
                return
            except Conflict:
                continue

    def browser_upgrade() -> bool:
        if registry.get('website') is None or registry.get('browser') is None or plugin_found('browser').state != 'ok' or plugin_found('website').state != 'ok':
            return False
        if not store.begin_once(BROWSER_MOVE_MARK, iso(utc_now() - dt.timedelta(minutes=10))):
            return False
        changed = False
        moved: list[str] = []
        sites = sorted((s for s in instances.all('website') if s.enabled and not s.problem and str(s.values.get('browser-url') or '').strip()), key=lambda s: (not s.primary, s.name))
        for site in sites:
            try:
                made, created = adopt_browser(str(site.values.get('browser-url')).strip(), SERVER_BY, site.name)
                changed = changed or created
                if site.source != ADMIN or 'browser-url' in site.owned:
                    continue
                instances.update('website', site.name, {'browser': made.name, 'browser-url': ''}, SERVER_BY)
                store.add_activity(ADMIN_KIND, SERVER_BY, msg('integrations.browser_moved', browser=made.name, instance=site.name))
                moved.append(site.name)
                changed = True
            except (SourceOwned, InvalidInstance, InstanceExists, InstanceMissing) as e:
                log.warning('%s', msg('integrations.browser_move_failed', error=kind(e), instance=site.name))
        try:
            tidy_browser_state([i.name for i in instances.all('website')])
        except Exception as e:
            log.warning('%s', msg('integrations.browser_move_failed', error=kind(e), instance='-'))
        store.finish_once(BROWSER_MOVE_MARK)
        return changed

    def blank_browser(name: str) -> bool:
        return bool((instances.col.find_one({'_id': f'website/{name}'}) or {}).get('noBrowser'))

    def mark_blank_browser(name: str, blank: bool) -> None:
        instances.col.update_one({'_id': f'website/{name}'}, {'$set': {'noBrowser': True}} if blank else {'$unset': {'noBrowser': ''}})

    def only_browser(values: dict, held: dict | None = None, blank: bool = False) -> dict:
        if blank or 'browser' in values or str((held or {}).get('browser') or '').strip() or str(values.get('browser-url') or (held or {}).get('browser-url') or '').strip():
            return values
        found = instances.all('browser')
        enabled = [i for i in found if i.enabled and not i.problem]
        return {**values, 'browser': enabled[0].name} if len(enabled) == 1 and plugin_found('browser').state == 'ok' else values

    @app.post('/api/admin/plugins/website/instances/{name}/move-browser')
    def post_admin_move_browser(name: str, by: Annotated[str, Depends(require_admin)]) -> dict:
        website = plugin_usable('website')
        headless = plugin_usable('browser')
        before = instances.fresh('website', name)
        if before is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('instances.unknown', name=name))
        url = str(before.values.get('browser-url') or '').strip()
        if not url:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('integrations.move_browser_none', instance=name))
        try:
            made, created = adopt_browser(url, by, name)
        except InvalidInstance as e:
            raise FieldRefused(e.field, e.args[0]) from None
        try:
            instances.update('website', name, {'browser': made.name, 'browser-url': ''}, by)
        except InvalidInstance as e:
            raise FieldRefused(e.field, e.args[0]) from None
        except SourceOwned as e:
            raise HTTPException(status.HTTP_409_CONFLICT, e.args[0]) from None
        store.add_activity(ADMIN_KIND, by, msg('integrations.instance_changed', plugin=website.name, instance=name, fields='browser, browser-url'))
        return {'browser': made.name, 'created': created}

    @app.post('/api/admin/plugins/mail-server/instances/{name}/test-mail')
    def post_admin_test_mail(name: str, request: Request, by: Annotated[str, Depends(require_admin)], body: TestMailIn | None = None) -> dict:
        plugin = plugin_usable(mail_out.PLUGIN_ID)
        held = instances.fresh(plugin.id, name)
        plugin_state.refresh()
        if held is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('instances.unknown', name=name))
        if held.problem:
            raise HTTPException(status.HTTP_409_CONFLICT, held.problem)
        if not held.enabled or not plugin_state.enabled(plugin.id):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mailserver.test_off', instance=name))
        creator = request.state.actor.creator
        person = users.get(creator) if creator != BOOTSTRAP else None
        address = str((person or {}).get('email') or '')
        if not address or person.get('disabled'):
            raise HTTPException(status.HTTP_409_CONFLICT, msg('mailserver.test_no_person'))
        if not mail_trials.reserve(address):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg('mailserver.test_limit', max=TRIALS_PER_HOUR))
        subject, text, html = mail_out.trial_mail(name, time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(clock())))
        try:
            reply = mail_out.Mailer(held.info().config).send_answered([address], subject, text, html, kind=mail_out.TRIAL_KIND)
        except (mail_out.MailDown, mail_out.MailRefused) as e:
            store.add_activity(ADMIN_KIND, by, msg('integrations.test_mail_failed', instance=name))
            return {'ok': False, 'text': noticemail.scrub(e.text, address)}
        store.add_activity(ADMIN_KIND, by, msg('integrations.test_mail_sent', instance=name))
        if not reply:
            return {'ok': True, 'text': msg('mailserver.test_sent_plain', address=noticemail.masked(address))}
        return {'ok': True, 'text': msg('mailserver.test_sent', address=noticemail.masked(address), reply=noticemail.scrub(reply, address))}

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
        try:
            sweeper.forget(plugin_id, name)
        except Exception as e:
            log.warning('the connection time of %s %s was not forgotten: %s', plugin_id, name, kind(e))
        if plugin_id == MCPTOOLS_ID:
            mcptools_routes.after_delete(mcptools_deps, name)
        store.add_activity(ADMIN_KIND, by, msg('integrations.instance_deleted', plugin=plugin.name, instance=name))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    def redrawn(by: str):
        def done(result: dict) -> None:
            if result.get('result') in ('done', 'slow'):
                store.add_activity('slack', by, msg('slack.redrawn', count=result['redrawn']))
        return done

    def redraw_at_start() -> bool:
        if not languages_ready():
            return False
        try:
            if not slack.enabled or not store.begin_once(REDRAW_MARK, iso(utc_now() - dt.timedelta(seconds=REDRAW_MARK_STALE_S))):
                return True
            card_posts.redraw_start(on_done=lambda result: (redrawn('server')(result), store.finish_once(REDRAW_MARK) if result.get('result') == 'done' else None), stale_only=True)
        except Exception as e:
            log.warning('the redraw of the old Slack posts did not start: %s', kind(e))
        return True

    app.state.redraw_at_start = redraw_at_start

    @app.get('/api/admin/slack-redraw')
    def get_admin_slack_redraw(by: Annotated[str, Depends(require_admin)]) -> dict:
        return card_posts.redraw_status()

    @app.post('/api/admin/slack-redraw', status_code=status.HTTP_202_ACCEPTED)
    def post_admin_slack_redraw(by: Annotated[str, Depends(require_admin)]) -> dict:
        if not slack.enabled:
            raise HTTPException(status.HTTP_409_CONFLICT, msg('slack.redraw_off'))
        started = card_posts.redraw_start(on_done=redrawn(by))
        return {'started': started, **card_posts.redraw_status()}

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

    @app.post('/api/admin/plugins/{plugin_id}/fields/{key}/suggestions')
    def post_admin_field_suggestions(plugin_id: str, key: Annotated[str, PathParam(max_length=64)], body: FieldSuggestIn, by: Annotated[str, Depends(require_admin)]) -> dict:
        plugin = plugin_usable(plugin_id)
        try:
            return runtime.suggestions(plugin, key, body.name, body.values, SUGGEST_TIMEOUT_S, asker=by)
        except SuggestFailed as e:
            raise HTTPException(e.status, e.text) from None
        except InvalidInstance as e:
            raise FieldRefused(e.field, e.args[0]) from None
        except InstanceMissing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg('instances.unknown', name=body.name)) from None

    mcptools_routes.mount(app, mcptools_deps, require_admin)
    promqlroutes.mount(app, custom_checks, require_admin, lambda by, text: store.add_activity(ADMIN_KIND, by, text), lambda: scheduler.nudge(CUSTOM_CHECKS), reader=require_login)

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

    @app.get('/api/jira/create-fields')
    def jira_create_fields(project: Annotated[str, Query(max_length=20)], by: Annotated[str, Depends(require_operator)]) -> dict:
        if not jira.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Jira is not set up on this service')
        wanted = project.strip().upper()
        offered = [*projects_of(settings), *(k for k in plugin_projects() if PROJECT_KEY.fullmatch(k) and k != settings.jira_tech_project)]
        if wanted not in offered:
            raise HTTPException(status_code=422, detail='project must be one of ' + ', '.join(dict.fromkeys(offered)))
        return dialog_view(jira_fields.create_meta(wanted, dict(settings.jira_issue_types).get(wanted, 'Task')), settings)

    @app.get('/api/admin/jira/fields')
    def admin_jira_fields(role: Annotated[str, Query(max_length=20)], by: Annotated[str, Depends(require_admin)]) -> dict:
        if role not in JIRA_FIELD_ROLES:
            raise HTTPException(status_code=422, detail='role is one of ' + ', '.join(JIRA_FIELD_ROLES))
        if not jira.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Jira is not set up on this service')
        found = jira_fields.all()
        if found is None:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, 'The Jira fields could not be read; type the field id')
        return field_choices(found, role)

    @app.get('/api/requests')
    def get_requests(limit: Annotated[int, Query(ge=1, le=200)] = 100) -> list[dict]:
        return store.requests(limit)

    @app.get('/api/investigations/{run_id}')
    def get_investigation(request: Request, run_id: str, authorization: Annotated[str | None, Header()] = None) -> dict:
        run = store.investigation(run_id)
        if not reads_access(request, authorization):
            return public_run(run)
        return run | store.run_guard_parts(run_id) if run.get('trigger') == 'chat' and run.get('outcome') in HELD_OUTCOMES else run

    def settle_held(run_id: str, by: str, release: bool) -> dict:
        store.investigation(run_id)
        verb = 'release' if release else 'discard'
        said = 'not held'
        try:
            done = investigator.release(run_id, by) if release else investigator.discard(run_id, by)
            if done is None:
                raise HTTPException(status.HTTP_409_CONFLICT, msg('slackevents.guard.not_held'))
            said = 'released' if release else 'discarded'
            return done | store.run_guard_parts(run_id)
        except ReleaseTaken:
            said = 'taken over by another release'
            raise HTTPException(status.HTTP_409_CONFLICT, msg('slackevents.guard.not_held')) from None
        except ReleaseFailed:
            said = 'release failed, still held'
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, msg('slackevents.guard.release_failed')) from None
        finally:
            store.add_activity(ADMIN_KIND, by, f'held answer {verb} \u00b7 run {run_id} \u00b7 {said}', runId=run_id)

    @app.post('/api/admin/runs/{run_id}/release')
    def post_run_release(run_id: str, by: Annotated[str, Depends(require_admin)]) -> dict:
        return settle_held(run_id, by, True)

    @app.post('/api/admin/runs/{run_id}/discard')
    def post_run_discard(run_id: str, by: Annotated[str, Depends(require_admin)]) -> dict:
        return settle_held(run_id, by, False)

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
        return toolusage.with_status(usage, ToolSet(runtime), store.last_probe() if on else None) | {'probe': on, 'langfuse': tracer.project_url()}

    @app.get('/api/costs')
    def get_costs(request: Request, _: Annotated[str, Depends(require_login)], days: Annotated[int, Query(ge=1, le=30)] = 7,
                  runs: Annotated[str, Query(pattern='^(newest|all)$')] = 'newest', authorization: Annotated[str | None, Header()] = None) -> dict:
        today = clocks.day(time.time())
        rows = store.ledger_since(costs.window(days, today)[0])
        admin = reads_access(request, authorization)
        meta = store.run_costs_meta({r['runId'] for r in rows if r.get('runId')} | ({r['guardRun'] for r in rows if r.get('guardRun')} if admin else set()))
        guard_on = bool(settings.investigate_guard_model) and bool(settings.guard_check_questions or settings.guard_check_answers)
        return costs.summary(rows, meta, today, days, store.spent(today), settings.investigate_daily_usd, guard_on, admin, runs == 'all')

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
        admin = reads_access(request, authorization)
        return unshot(store.list_activity(since, limit, () if admin else (ACCESS_KIND,)), admin)

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
        state = information.written_back(state, store.load())
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
        if body.type not in page_actions.ANY_RECORD:
            refuse_information(body.key)
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
            for k in action['cards']:
                refuse_information(k)
            planted = masked_scan('page', question + '\n' + (body.label or ''))
            if planted:
                action['askFlags'] = planted
            question = redact(question)
            said = page_topic_check(question, action['cards'], by)
            action['text'] = question
            action['label'] = redact(body.label or question)[:300]
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
        refuse_information(body.key)
        if not slack.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Slack is not set up on this service yet')
        try:
            return send_anomaly_to_slack(body.key, by, str(request.base_url))
        except SlackError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f'Slack refused the message: {reason_of(e)}') from e

    @app.post('/api/slack-holds', status_code=status.HTTP_201_CREATED)
    def post_slack_hold(body: SlackHoldIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        raise HTTPException(status.HTTP_410_GONE, NO_DRAFTS)

    @app.post('/api/slack-updates', status_code=status.HTTP_201_CREATED)
    def post_slack_update(body: SlackUpdateIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        refuse_information(body.key)
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
        refuse_information(body.key)
        try:
            return routes.send(body.key, body.route, by)
        except RouteRefused as e:
            raise HTTPException(e.status, e.text) from None

    def refuse_post_text(text: str) -> None:
        problem = draft_problem(text, allowed_link_hosts(settings, runtime.org()['link_hosts'] + runtime.ui_hosts())) if text else None
        if problem:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)

    @app.put('/api/slack-drafts')
    def put_slack_draft(body: SlackDraftIn, by: Annotated[str, Depends(require_operator)]) -> dict:
        if not is_token(by):
            raise HTTPException(status.HTTP_410_GONE, NO_DRAFTS)
        refuse_information(body.key)
        refuse_post_text(body.text.strip())
        rev, draft, armed = store.slack_draft(body.key, body.text.strip(), by)
        log.info('slack post for %s %s by %s', body.key, 'queued' if armed else 'saved' if draft else 'cleared', by)
        return {'rev': rev, 'slackDraft': draft, 'autoSend': armed}

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
        if not is_token(by):
            raise HTTPException(status.HTTP_410_GONE, NO_DRAFTS)
        refuse_post_text(body.text.strip())
        saved = store.report_slack_draft(report_id, body.text.strip(), by)
        log.info('slack post for report %s %s by %s', report_id, 'queued' if saved['autoSend'] else 'cleared', by)
        return saved

    @app.post('/api/deploy-reports/{report_id}/slack-hold', status_code=status.HTTP_201_CREATED)
    def post_report_slack_hold(report_id: str, by: Annotated[str, Depends(require_operator)]) -> dict:
        raise HTTPException(status.HTTP_410_GONE, NO_DRAFTS)

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
                                  act=lambda body, by: add_action(body, by, via=mcpserver.MCP), assign=assigner.assign, card_view=card_view, board=investigator_board,
                                  kick=lambda: investigator.kick(time.time()) if investigator.enabled else None, chat_used=chat_used, base=base_url, feature_on=features.on, registry=registry, plugin_on=plugin_state.enabled)
    mcp_manager = mcpserver.mount(app, mcp_bridge, mcp_auth, on=lambda: features.on('mcp'))

    return app
