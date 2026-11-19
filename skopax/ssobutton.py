import html
from pathlib import Path
from urllib.parse import urlsplit

from skopax.config import SSO_BUTTON_ICONS, SSO_BUTTON_TEXT_MAX
from skopax.messages import msg

ICON_DIR = Path(__file__).parent / 'web' / 'icons' / 'sso'
TEXT_MAX = SSO_BUTTON_TEXT_MAX
ICON_MODES = SSO_BUTTON_ICONS
KEY = 'key'
MARKS: tuple[dict, ...] = (
    {'id': 'google', 'hosts': ['accounts.google.com']},
    {'id': 'microsoft', 'hosts': ['login.microsoftonline.com', 'sts.windows.net']},
    {'id': 'okta', 'suffixes': ['okta.com', 'oktapreview.com']},
    {'id': 'auth0', 'suffixes': ['auth0.com']},
    {'id': 'onelogin', 'suffixes': ['onelogin.com']},
    {'id': 'jumpcloud', 'suffixes': ['jumpcloud.com']},
    {'id': 'zitadel', 'suffixes': ['zitadel.cloud'], 'labels': ['zitadel']},
    {'id': 'authentik', 'labels': ['authentik'], 'paths': ['/application/o/']},
    {'id': 'gitlab', 'hosts': ['gitlab.com'], 'labels': ['gitlab']},
    {'id': 'keycloak', 'labels': ['keycloak'], 'paths': ['/realms/']},
)
MARK_IDS = tuple(m['id'] for m in MARKS) + (KEY,)
BRANDED = {'microsoft': 'Sign in with Microsoft'}


def rules() -> list[dict]:
    return [{'id': m['id'], 'hosts': list(m.get('hosts', ())), 'suffixes': list(m.get('suffixes', ())), 'labels': list(m.get('labels', ())), 'paths': list(m.get('paths', ()))} for m in MARKS]


def branded() -> dict[str, str]:
    return dict(BRANDED)


def matches(rule: dict, host: str, path: str) -> bool:
    return (host in rule.get('hosts', ()) or any(host == s or host.endswith('.' + s) for s in rule.get('suffixes', ()))
            or host.split('.', 1)[0] in rule.get('labels', ()) or any(p in path for p in rule.get('paths', ())))


def mark_of(issuer: str) -> str:
    try:
        parts = urlsplit(str(issuer or '').strip())
        host = (parts.hostname or '').lower()
    except ValueError:
        return KEY
    if parts.scheme not in ('http', 'https') or not host:
        return KEY
    return next((m['id'] for m in MARKS if matches(m, host, parts.path or '')), KEY)


def button(issuer: str, text: str, mode: str, icon_version: str | None) -> dict:
    label = (text or '').strip()
    if mode == 'none':
        return {'mark': 'none', 'text': label or msg('login.sso'), 'icon': None}
    if mode == 'company':
        return {'mark': 'company', 'text': label or msg('login.sso'), 'icon': f'/login/sso-icon?v={icon_version}' if icon_version else None}
    mark = mark_of(issuer)
    if mark in BRANDED:
        if label:
            mark = KEY
        else:
            label = BRANDED[mark]
    return {'mark': mark, 'text': label or msg('login.sso'), 'icon': f'/icons/sso/{mark}.svg'}


def button_html(shown: dict, href: str) -> str:
    icon = f'<img class="sso-ico" src="{html.escape(shown["icon"], quote=True)}" alt="" width="20" height="20">' if shown['icon'] else ''
    return (f'<a class="sso-btn sso-btn-{html.escape(shown["mark"], quote=True)}" href="{html.escape(href, quote=True)}">{icon}'
            f'<span class="sso-txt">{html.escape(shown["text"], quote=True)}</span></a>')
