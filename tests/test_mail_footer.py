import pytest

from skopax import brand, mail, partmail
from skopax.noticemail import plain_mail, with_why
from tests.conftest import AUTH

BASE = 'https://ops.example.com'
CARD = {'key': 'log|checkout-service|ERROR', 'num': 7, 'sev': 'crit', 'status': 'open', 'svc': 'checkout-service', 'label': 'ERROR pattern'}
PREFS = BASE + '/#t=prefs'


def mails() -> dict[str, tuple[str, str, str]]:
    return {'card': mail.card_mail(CARD, BASE + '/c/7', head='Route', body_lines=['A line']),
            'notice': with_why(mail.card_mail(CARD, BASE + '/c/7', head='Assigned to you', body_lines=['A line']), PREFS),
            'plain': plain_mail('A title', 'Assigned to you', ['A line'], BASE + '/c/7', 'Open', PREFS),
            'test': mail.trial_mail('main', '2026-11-20 10:00 UTC'),
            'part': partmail.compose('[Acme Ops] Daily', 'Daily', 'Daily', [partmail.Section('Developer', None, (partmail.Row('a card'),))])}


def after_dashes(text: str) -> list[str]:
    return text.rstrip('\n').rpartition('\n-- \n')[2].split('\n')


@pytest.fixture
def acme():
    brand.use('Acme Ops')
    mail.use_site(BASE)
    yield
    mail.use_site('')


def test_every_mail_footer_starts_with_the_own_address_then_the_sent_line_then_a_small_attribution(acme):
    # given
    attribution = brand.attribution_line()

    # when
    found = mails()

    # then
    assert attribution['text'].startswith('Acme Ops · powered by Skopax ')
    for name, (_, text, html) in found.items():
        lines = after_dashes(text)
        assert lines[0] == BASE and lines[1] == 'Sent automatically by Acme Ops.', name
        assert lines[-1] == f"{attribution['text']} {attribution['href']}", name
        site = html.index(f'<a href="{BASE}" style="color:{mail.INK};">ops.example.com</a>')
        sent = html.index('Sent automatically by Acme Ops.')
        small = html.index(f'<div style="padding-top:8px;font-size:11px;color:{mail.MUTED};"><a href="{brand.SITE}" style="color:{mail.MUTED};">{mail.esc(attribution["text"])}</a></div>')
        assert site < sent < small, name
        assert html.count('powered by') == 1, name


def test_the_why_line_of_a_notification_mail_stays_between_the_sent_line_and_the_attribution(acme):
    # when
    _, text, html = mails()['notice']

    # then
    lines = after_dashes(text)
    assert lines[1] == 'Sent automatically by Acme Ops.' and lines[2].startswith('You get this because you turned on email notifications') and lines[3].startswith('Acme Ops · powered by')
    assert html.index('Sent automatically by Acme Ops.') < html.index('You get this because') < html.index('font-size:11px')


def test_without_a_base_url_the_footer_has_no_address_line():
    # given
    mail.use_site('')

    # when
    found = mails()

    # then
    for name, (_, text, html) in found.items():
        assert after_dashes(text)[0] == 'Sent automatically by Skopax.', name
        assert f'font-size:12px;color:{mail.MUTED};background-color:{mail.PAPER};">Sent automatically by Skopax.' in html, name


def test_the_hide_attribution_switch_drops_only_the_attribution(acme):
    # given
    brand.use_hidden(lambda: True)

    # when
    found = mails()

    # then
    for name, (_, text, html) in found.items():
        assert after_dashes(text)[0] == BASE and 'powered by' not in text and 'powered by' not in html and 'font-size:11px' not in html, name


def test_the_own_skopax_name_is_not_doubled_with_powered_by():
    # given
    mail.use_site(BASE)

    # when
    _, text, html = mails()['part']
    mail.use_site('')

    # then
    assert after_dashes(text)[-1] == f'Skopax {brand.display_version()} · skopax.io {brand.SITE}'
    assert 'powered by' not in html and html.count('skopax.io</a>') == 1


def test_the_host_is_escaped():
    # given
    mail.use_site('https://a&b.example.com/')

    # when
    _, text, html = mails()['test']
    found = mail.site()
    mail.use_site('')

    # then
    assert found == ('a&b.example.com', 'https://a&b.example.com')
    assert '<a href="https://a&amp;b.example.com" style="color:#0f1720;">a&amp;b.example.com</a>' in html
    assert after_dashes(text)[0] == 'https://a&b.example.com'


def test_an_address_that_is_not_a_web_link_gives_no_line():
    # given
    mail.use_site('ftp://ops.example.com')

    # when
    found = mail.site()
    mail.use_site('')

    # then
    assert found is None


def test_the_app_mails_with_its_public_base_url(client):
    # when
    found = mail.site()

    # then
    assert found == ('skopax.example.com', 'https://skopax.example.com')
