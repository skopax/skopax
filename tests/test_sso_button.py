import base64
import re
import time

import pytest
from pymongo.errors import PyMongoError

from skopax import ssobutton, ssoicon
from skopax.messages import msg
from skopax.store import Store
from tests.conftest import AUTH
from tests.sso_kit import FakeIdp, app_client, idp_settings

SSO = '/api/admin/sso'
ICON = '/api/admin/sso/icon'
OPEN_ICON = '/login/sso-icon'
HTML = {'Accept': 'text/html'}
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40
WEBP = b'RIFF\x24\x00\x00\x00WEBPVP8 ' + b'\x00' * 40
GIF = b'GIF89a' + b'\x00' * 40
S = b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
LOGO_SVG = (b'<?xml version="1.0" encoding="UTF-8"?>\n<!-- exported -->\n<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="64" height="64" viewBox="0 0 64 64">'
            b'<title>Acme</title><desc>Acme logo</desc><defs><linearGradient id="grad-a" x1="0" y1="0" x2="1" y2="1" gradientUnits="objectBoundingBox" gradientTransform="rotate(45)">'
            b'<stop offset="0" stop-color="#0f766e" stop-opacity="1"/><stop offset="1" stop-color="rgb(20, 184, 166)"/></linearGradient>'
            b'<radialGradient id="grad-b" cx="32" cy="32" r="30" fx="28" fy="28" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#ccc"/></radialGradient>'
            b'<clipPath id="clip"><circle cx="32" cy="32" r="30"/></clipPath><symbol id="mark" viewBox="0 0 10 10"><polygon points="0,10 5,0 10,10"/></symbol></defs>'
            b'<g clip-path="url(#clip)" transform="translate(0 0) scale(1)"><rect width="64" height="64" rx="12" fill="url(#grad-a)"/>'
            b'<path d="M12 44 L32 12 L52 44 Z" fill="none" stroke="url( #grad-b )" stroke-width="4" stroke-linejoin="round" stroke-linecap="round" fill-rule="evenodd"/>'
            b'<ellipse cx="32" cy="50" rx="8" ry="3" opacity="0.5"/><line x1="10" y1="58" x2="54" y2="58" stroke="#fff"/><polyline points="1,1 2,2" fill="none" stroke="black"/></g>'
            b'<use xlink:href="#mark" x="27" y="27" width="10" height="10"/><use href="#mark" x="0" y="0" width="5" height="5"/></svg>\n')
R = b'<rect id="a" width="9" height="9"/>'
USE_BOMB = (S + b'<defs><g id="a0"><rect width="9" height="9"/></g>' + b''.join(b'<g id="a%d">' % i + b'<use href="#a%d"/>' % (i - 1) * 10 + b'</g>' for i in range(1, 10))
            + b'</defs><use href="#a9"/></svg>')
CLEAN_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 20 20"><defs><linearGradient id="g"/></defs><rect width="20" height="20" fill="url(#g)"/><use xlink:href="#g"/></svg>'
ICON_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def upload(client, data: bytes, kind: str = 'image/png'):
    return client.put(ICON, content=data, headers={**AUTH, 'Content-Type': kind})


def login_page(client) -> str:
    return client.get('/login?next=/c/7', headers=HTML).text


def sso_button(page: str) -> str:
    found = re.search(r'<a class="sso-btn[^"]*"[^>]*>.*?</a>', page, re.S)
    assert found, 'the login page has an SSO button'
    return found.group(0)


@pytest.mark.parametrize('issuer, mark', [
    ('https://accounts.google.com', 'google'),
    ('https://login.microsoftonline.com/0000-tenant/v2.0', 'microsoft'),
    ('https://sts.windows.net/0000-tenant/', 'microsoft'),
    ('https://acme.okta.com/oauth2/default', 'okta'),
    ('https://acme.oktapreview.com', 'okta'),
    ('https://acme.eu.auth0.com/', 'auth0'),
    ('https://sso.example.com/realms/shop', 'keycloak'),
    ('https://gitlab.com', 'gitlab'),
    ('https://gitlab.example.com', 'gitlab'),
    ('https://acme.onelogin.com/oidc/2', 'onelogin'),
    ('https://oauth.id.jumpcloud.com/', 'jumpcloud'),
    ('https://authentik.example.com/application/o/skopax/', 'authentik'),
    ('https://zitadel.example.com', 'zitadel'),
    ('https://acme-x1.zitadel.cloud', 'zitadel'),
    ('https://ACCOUNTS.GOOGLE.COM', 'google'),
    ('https://accounts.google.com.evil.example', 'key'),
    ('https://evil-accounts.google.com.example', 'key'),
    ('https://login.microsoftonline.com.evil.example/x', 'key'),
    ('https://okta.com.evil.example', 'key'),
    ('https://evilokta.com', 'key'),
    ('https://idp.example.com/accounts.google.com', 'key'),
    ('https://idp.example.com', 'key'),
    ('', 'key'),
    ('not a url', 'key'),
])
def test_the_auto_icon_follows_the_issuer_host_and_never_a_look_alike(issuer: str, mark: str):
    # when
    found = ssobutton.mark_of(issuer)

    # then
    assert found == mark


def test_every_mark_ships_a_file_and_the_rules_are_sent_whole():
    # when
    rules = ssobutton.rules()

    # then
    assert [r['id'] for r in rules] == [m for m in ssobutton.MARK_IDS if m != 'key']
    for mark in ssobutton.MARK_IDS:
        assert (ssobutton.ICON_DIR / f'{mark}.svg').is_file()


@pytest.mark.parametrize('data, kind', [(PNG, 'image/png'), (WEBP, 'image/webp'), (CLEAN_SVG, 'image/svg+xml'), (LOGO_SVG, 'image/svg+xml'),
                                        (S + b'<rect width="9" height="9" fill="url( #a )"/></svg>', 'image/svg+xml'),
                                        (S + b'<rect id="curly" class="url-ish logo" width="9" height="9"/></svg>', 'image/svg+xml'),
                                        (b'<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve"><title>Acme \xc2\xb7 logo</title></svg>', 'image/svg+xml'),
                                        (S + R + b''.join(b'<use href="#a" x="%d"/>' % i for i in range(32)) + b'</svg>', 'image/svg+xml')])
def test_a_png_webp_or_clean_svg_is_taken_by_its_bytes(data: bytes, kind: str):
    # when
    found = ssoicon.check(data)

    # then
    assert found == kind


@pytest.mark.parametrize('data, reason', [
    (b'', 'sso.icon_type'),
    (GIF, 'sso.icon_type'),
    (b'%PDF-1.4', 'sso.icon_type'),
    (PNG + b'\x00' * ssoicon.ICON_MAX, 'sso.icon_too_big'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><s:script xmlns:s="http://www.w3.org/2000/svg">alert(1)</s:script></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><rect ONCLICK="x()"/></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div/></foreignObject></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><image xlink:href="https://evil.example/x.png"/></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><use href="https://evil.example/s.svg#a"/></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,AAAA"/></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(https://evil.example/f)"/></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://evil.example/a.css";</style></svg>', 'sso.icon_svg'),
    (b'<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><rect></svg>', 'sso.icon_svg'),
    (b'<?xml version="1.0" encoding="UTF-16"?><svg xmlns="http://www.w3.org/2000/svg"/>', 'sso.icon_svg'),
    (b"<?xml version='1.0' encoding='ISO-8859-1'?><svg xmlns=\"http://www.w3.org/2000/svg\"/>", 'sso.icon_svg'),
    (b'<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>', 'sso.icon_type'),
    (S + b'<a href="#x"><set attributeName="href" to="javascript:alert(1)"/><rect width="9" height="9"/></a></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9"><animate attributeName="fill" values="red;blue"/></rect></svg>', 'sso.icon_svg'),
    (S + b'<image href="#x"><animate attributeName="href" values="https://evil.example/x.png"/></image></svg>', 'sso.icon_svg'),
    (S + b'<style>rect{fill:u\\72l(https://evil.example/f)}</style></svg>', 'sso.icon_svg'),
    (S + b'<style>@\\69mport "https://evil.example/a.css";</style></svg>', 'sso.icon_svg'),
    (S + b'<style>rect{background:image-set("https://evil.example/a.png" 1x)}</style></svg>', 'sso.icon_svg'),
    (S + b'<style><g/>@import url(https://evil.example/a.css);</style></svg>', 'sso.icon_svg'),
    (b'<?xml-stylesheet href="https://evil.example/a.css"?>' + S + b'</svg>', 'sso.icon_svg'),
    (S + b'<?xml-stylesheet href="https://evil.example/a.css"?></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/1999/xhtml"><meta http-equiv="refresh" content="0;url=https://evil.example/"/></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg" xml:base="https://evil.example/"><use href="#a"/></svg>', 'sso.icon_svg'),
    (S + b'<style>rect{fill:red}</style></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" style="fill:red"/></svg>', 'sso.icon_svg'),
    (S + b'<a href="#x"><rect width="9" height="9"/></a></svg>', 'sso.icon_svg'),
    (S + b'<text x="1" y="9">Hi</text></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" fill="u\\72l(https://evil.example/f)"/></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" fill="url(https://evil.example/f)"/></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" fill="image-set(&quot;https://evil.example/a.png&quot; 1x)"/></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" data-x="1"/></svg>', 'sso.icon_svg'),
    (S + b'<path xlink:href="#a" d="M0 0"/></svg>', 'sso.icon_svg'),
    (S + b'<use xlink:href="other.svg#a"/></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:i="http://ns.example/i"><rect i:x="1"/></svg>', 'sso.icon_svg'),
    (S + b'<g>' * 200 + b'</g>' * 200 + b'</svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg" ' + b' '.join(b'x%d="1"' % i for i in range(300)) + b'/>', 'sso.icon_svg'),
    (S + R + b'<use href="&#160;#a"/></svg>', 'sso.icon_svg'),
    (S + R + b'<use href=" #a"/></svg>', 'sso.icon_svg'),
    (S + R + b'<use href="&#133;#a"/></svg>', 'sso.icon_svg'),
    (S + R + b'<use xlink:href="&#160;#a"/></svg>', 'sso.icon_svg'),
    (S + R + b'<use xlink:href=" #a"/></svg>', 'sso.icon_svg'),
    (S + R + b'<use xlink:href="&#x2003;#a"/></svg>', 'sso.icon_svg'),
    (S + R + b'<use xlink:href="&#133;#a"/></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" fill="url(&#160;#a)"/></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" fill="url(\xc2\xa0#a)"/></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" clip-path="url(#a\xe2\x80\x83)"/></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" fill="' + b'a' * 4097 + b'"/></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" id="url(x)"/></svg>', 'sso.icon_svg'),
    (S + b'<rect width="9" height="9" class="a&#160;b"/></svg>', 'sso.icon_svg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg" xml:space="keep"/>', 'sso.icon_svg'),
    (USE_BOMB, 'sso.icon_svg'),
    (S + R + b''.join(b'<use href="#a"/>' for _ in range(33)) + b'</svg>', 'sso.icon_svg'),
    (S + R + b'<g id="b"><use href="#a"/></g><use href="#b"/></svg>', 'sso.icon_svg'),
    (S + R + b'<use id="c" href="#a"/><use href="#c"/></svg>', 'sso.icon_svg'),
])
def test_an_unsafe_or_unknown_upload_is_refused_with_its_reason(data: bytes, reason: str):
    # when
    with pytest.raises(ssoicon.IconRefused) as refused:
        ssoicon.check(data)

    # then
    assert refused.value.key == reason


def test_uploading_serving_and_removing_the_company_icon(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    nobody = client

    # when
    before = nobody.get(OPEN_ICON)
    saved = upload(client, CLEAN_SVG, 'image/svg+xml')
    version = saved.json()['icon']['version']
    served = nobody.get(OPEN_ICON, params={'v': version})
    stale = nobody.get(OPEN_ICON, params={'v': 'old'})
    lines = [a['text'] for a in store.activity.find({'kind': 'access'})]
    removed = client.delete(ICON, headers=AUTH)
    after = nobody.get(OPEN_ICON)
    removed_again = client.delete(ICON, headers=AUTH)

    # then
    assert before.status_code == 404
    assert saved.status_code == 200 and saved.json()['icon'] == {'set': True, 'type': 'image/svg+xml', 'size': len(CLEAN_SVG), 'version': version}
    assert served.status_code == 200 and served.content == CLEAN_SVG
    assert served.headers['content-type'] == 'image/svg+xml'
    assert served.headers['content-security-policy'] == ICON_CSP
    assert served.headers['x-content-type-options'] == 'nosniff'
    assert 'immutable' in served.headers['cache-control'] and 'max-age=' in served.headers['cache-control']
    assert stale.status_code == 404
    assert lines[-1] == msg('sso.icon_saved', type='image/svg+xml', size=len(CLEAN_SVG))
    assert not any('<svg' in line or base64.b64encode(CLEAN_SVG).decode()[:20] in line for line in lines)
    assert removed.status_code == 200 and removed.json()['icon'] == {'set': False, 'type': None, 'size': 0, 'version': None}
    assert after.status_code == 404
    assert [a['text'] for a in store.activity.find({'kind': 'access'})][-1] == msg('sso.icon_removed')
    assert removed_again.status_code == 200 and store.activity.count_documents({'kind': 'access', 'text': msg('sso.icon_removed')}) == 1


def test_icon_uploads_are_refused_when_too_big_wrong_or_unsafe_and_only_admins_may_upload(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    nobody = app_client(store, idp)

    # when
    big = upload(client, PNG + b'\x00' * ssoicon.ICON_MAX)
    gif = upload(client, GIF, 'image/png')
    script = upload(client, b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', 'image/svg+xml')
    anonymous = nobody.put(ICON, content=PNG, headers={'Content-Type': 'image/png'})

    # then
    assert (big.status_code, big.json()) == (413, {'detail': msg('sso.icon_too_big', max=ssoicon.ICON_MAX // 1024)})
    assert (gif.status_code, gif.json()) == (422, {'detail': msg('sso.icon_type')})
    assert (script.status_code, script.json()) == (422, {'detail': msg('sso.icon_svg')})
    assert anonymous.status_code in (401, 403)
    assert store.db[ssoicon.COLLECTION].count_documents({}) == 0


def test_the_sso_view_carries_the_button_settings_icon_state_and_detection_rules(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)

    # when
    body = client.get(SSO, headers=AUTH).json()
    too_long = client.put(SSO, json={'values': {'button-text': 'x' * 41}}, headers=AUTH)
    wrong = client.put(SSO, json={'values': {'button-icon': 'logo'}}, headers=AUTH)
    saved = client.put(SSO, json={'values': {'button-text': 'Sign in with Acme', 'button-icon': 'none'}}, headers=AUTH)

    # then
    assert body['values']['button-text'] == {'value': '', 'source': 'default', 'from': None}
    assert body['values']['button-icon'] == {'value': 'auto', 'source': 'default', 'from': None}
    assert body['icon'] == {'set': False, 'type': None, 'size': 0, 'version': None}
    assert body['button'] == {'defaultText': msg('login.sso'), 'textMax': ssobutton.TEXT_MAX, 'iconMax': ssoicon.ICON_MAX, 'marks': ssobutton.rules(), 'branded': ssobutton.branded()}
    assert too_long.status_code == 422 and too_long.json()['field'] == 'button-text'
    assert wrong.status_code == 422 and wrong.json()['field'] == 'button-icon'
    assert saved.status_code == 200
    assert saved.json()['values']['button-text']['value'] == 'Sign in with Acme' and saved.json()['values']['button-icon']['value'] == 'none'


@pytest.mark.parametrize('issuer, text, icon, src, label, mark', [
    ('https://acme.okta.com', '', 'auto', '/icons/sso/okta.svg', 'Sign in with SSO', 'okta'),
    ('https://login.microsoftonline.com/t/v2.0', '', 'auto', '/icons/sso/microsoft.svg', 'Sign in with Microsoft', 'microsoft'),
    ('https://login.microsoftonline.com/t/v2.0', 'Sign in with Acme', 'auto', '/icons/sso/key.svg', 'Sign in with Acme', 'key'),
    ('https://accounts.google.com.evil.example', '', 'auto', '/icons/sso/key.svg', 'Sign in with SSO', 'key'),
    ('https://acme.okta.com', 'Sign in with Acme', 'none', None, 'Sign in with Acme', 'none'),
    ('https://acme.okta.com', '', 'company', None, 'Sign in with SSO', 'company'),
])
def test_the_login_page_button_in_each_mode(store: Store, issuer, text, icon, src, label, mark):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_issuer=issuer, sso_button_text=text, sso_button_icon=icon))

    # when
    button = sso_button(login_page(client))

    # then
    assert 'href="/login/sso?next=%2Fc%2F7"' in button and f'sso-btn-{mark}' in button
    assert f'<span class="sso-txt">{label}</span>' in button
    if src:
        assert f'<img class="sso-ico" src="{src}" alt="" width="20" height="20">' in button
    else:
        assert '<img' not in button


def test_the_company_mode_shows_the_uploaded_icon_with_its_version(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_button_icon='company'))
    version = upload(client, PNG).json()['icon']['version']

    # when
    button = sso_button(login_page(client))
    served = client.get('/icons/sso/key.svg')

    # then
    assert f'<img class="sso-ico" src="/login/sso-icon?v={version}" alt="" width="20" height="20">' in button
    assert served.status_code == 200 and served.headers['content-type'] == 'image/svg+xml' and served.headers['x-content-type-options'] == 'nosniff'


def test_the_button_text_is_escaped_and_a_broken_icon_store_never_breaks_the_login_page(store: Store, monkeypatch):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_button_text='<b>Acme</b> & "co"', sso_button_icon='company'))

    def broken(*a, **k):
        raise PyMongoError('down')

    monkeypatch.setattr(ssoicon.SsoIcon, 'status', broken)

    # when
    page = client.get('/login', headers=HTML)

    # then
    assert page.status_code == 200
    assert '<span class="sso-txt">&lt;b&gt;Acme&lt;/b&gt; &amp; &quot;co&quot;</span>' in page.text and '<b>Acme' not in page.text
    assert '<img' not in sso_button(page.text)


def test_a_signed_in_admin_uploads_an_icon_of_exactly_the_limit_and_gets_it_back_intact(store: Store):
    # given
    from tests.test_api_tokens import admin_client
    admin = admin_client(store)
    exact = PNG + bytes(range(256)) * ((ssoicon.ICON_MAX - len(PNG)) // 256) + b'\x01' * ((ssoicon.ICON_MAX - len(PNG)) % 256)

    # when
    saved = admin.put(ICON, content=exact, headers={'Content-Type': 'image/png'})
    version = saved.json()['icon']['version']
    served = admin.get(OPEN_ICON, params={'v': version})

    # then
    assert len(exact) == ssoicon.ICON_MAX
    assert saved.status_code == 200 and saved.json()['icon']['size'] == ssoicon.ICON_MAX
    assert served.status_code == 200 and served.content == exact and served.headers['content-type'] == 'image/png'


def test_the_icon_state_is_cached_by_version_and_read_without_the_image(store: Store):
    # given
    now = [100.0]
    ssoicon.SsoIcon(store.db).set(PNG, 'image/png', 'alice@example.com')
    icons = ssoicon.SsoIcon(store.db, clock=lambda: now[0])
    reads = []
    real = icons.col.find_one
    icons.col.find_one = lambda query, projection=None: reads.append(dict(projection) if projection else None) or real(query, projection)

    # when
    first = icons.status()
    again = icons.status()
    data = icons.data(first['version'])
    data_again = icons.data(first['version'])
    now[0] += ssoicon.CACHE_S + 1
    later = icons.status()
    icons.clear()
    gone = icons.status()

    # then
    assert first == again == later and first['version'] and gone['set'] is False
    assert data == data_again == (PNG, 'image/png')
    assert reads[0] == {'data': 0} and reads.count({'data': 0}) == 2, 'the state is read without the image and cached until the TTL'
    assert sum(1 for r in reads if r is None) == 1, 'the image is read once per version'


def test_the_login_page_reads_the_icon_state_once_per_ttl(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_button_icon='company'))
    version = upload(client, PNG).json()['icon']['version']
    calls = []
    real = store.db[ssoicon.COLLECTION].find_one
    store.db[ssoicon.COLLECTION].find_one = lambda *a, **k: calls.append(a) or real(*a, **k)

    # when
    pages = [login_page(client) for _ in range(3)]

    # then
    assert all(f'/login/sso-icon?v={version}' in p for p in pages)
    assert calls == [], 'a fresh upload primes the cache, so the renders read nothing'


def test_an_upload_that_cannot_be_stored_answers_503_and_a_lost_history_line_still_saves(store: Store, monkeypatch, caplog):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)

    def down(*a, **k):
        raise PyMongoError('down')

    # when
    monkeypatch.setattr(ssoicon.SsoIcon, 'set', down)
    refused = upload(client, PNG)
    monkeypatch.undo()
    monkeypatch.setattr(Store, 'add_activity', down)
    saved = upload(client, PNG)

    # then
    assert (refused.status_code, refused.json()) == (503, {'detail': msg('sso.icon_unavailable')})
    assert saved.status_code == 200 and saved.json()['icon']['set'] is True
    assert any('history' in r.getMessage() for r in caplog.records)


def test_a_button_text_from_the_environment_over_the_limit_is_cut_with_one_warning(caplog):
    # given
    from skopax.config import SSO_BUTTON_TEXT_MAX, load_settings
    long = 'Sign in with ' + 'A' * 60

    # when
    s = load_settings(env={'SSO_BUTTON_TEXT': long})

    # then
    assert ssobutton.TEXT_MAX == SSO_BUTTON_TEXT_MAX
    assert s.sso_button_text == long[:SSO_BUTTON_TEXT_MAX]
    assert sum('sso.button-text' in r.getMessage() for r in caplog.records) == 1


def test_the_login_page_puts_an_or_divider_between_the_two_ways_in(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)

    # when
    page = login_page(client)

    # then
    assert re.search(r'</form>\s*<p class="sso-or"><span>or</span></p>\s*<p class="sso"><a class="sso-btn', page)


def test_a_huge_attribute_value_is_refused_quickly():
    # given
    data = S + b'<rect width="9" height="9" fill="' + b'a' * 60000 + b'"/></svg>'
    dashes = S + b'<rect width="9" height="9" transform="' + b'-a' * 2000 + b'(1)"/></svg>'

    # when
    started = time.perf_counter()
    for each in (data, dashes):
        with pytest.raises(ssoicon.IconRefused):
            ssoicon.check(each)
    took = time.perf_counter() - started

    # then
    assert len(data) <= ssoicon.ICON_MAX
    assert took < 0.5


def test_a_version_that_is_not_the_current_one_answers_404_from_the_cache(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    version = upload(client, PNG).json()['icon']['version']
    reads = []
    real = store.db[ssoicon.COLLECTION].find_one
    store.db[ssoicon.COLLECTION].find_one = lambda *a, **k: reads.append(a) or real(*a, **k)

    # when
    answers = [client.get(OPEN_ICON, params={'v': f'other{n}'}).status_code for n in range(5)]
    current = client.get(OPEN_ICON, params={'v': version})

    # then
    assert answers == [404] * 5
    assert current.status_code == 200 and current.content == PNG
    assert reads == [], 'a fresh cache answers without reading the database'
