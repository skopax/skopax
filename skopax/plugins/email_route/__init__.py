import threading
import time

from skopax import mail
from skopax.contract import Capabilities, Context, Field, Outbound, Plugin, ReadError, Setting, Setup, TestResult
from skopax.kit import connection
from skopax.messages import msg

ID = 'email'
SEVS = ('warn', 'high', 'crit')
KIND = 'route'
BUDGET_S = 7.0
KEEP_S = 6 * 3600
DOT = ' \u00b7 '
SHOWN_MAX = 80
SENT = 'sent'
REFUSED = 'refused'
DELIVERED: dict[tuple[str, str, str, str], tuple[float, str, str]] = {}
GUARD = threading.Lock()


def addresses(raw: object) -> list[str]:
    found: dict[str, str] = {}
    for part in str(raw or '').replace(',', ' ').split():
        found.setdefault(part.lower(), mail.tidy(part))
    return list(found.values())


def recipients_problem(value: str) -> str | None:
    found = addresses(value)
    if not 1 <= len(found) <= mail.RECIPIENTS_MAX:
        return msg('email.recipients', max=mail.RECIPIENTS_MAX)
    bad = next((a for a in found if not mail.address_ok(a)), None)
    return None if bad is None else msg('email.recipient', address=bad[:SHOWN_MAX])


def config_of(ctx: Context) -> dict:
    return dict(ctx.instance.config) if ctx.instance is not None else {}


def mailer_of(ctx: Context) -> mail.Mailer | None:
    found = ctx.peers(mail.PLUGIN_ID)
    if not found:
        return None
    chosen = mail.Mailer(dict(found[0].info.config), timeout=min(mail.TIMEOUT_S, BUDGET_S))
    return chosen if chosen.ready() else None


def ready(ctx: Context) -> str | None:
    return None if mailer_of(ctx) is not None else msg('email.no_server')


def head_of(message: dict) -> str:
    title = str(message.get('title') or '')
    lead = msg('routes.title', label='')
    return title[len(lead):] if lead and title.startswith(lead) and len(title) > len(lead) else title


def link_line(label: str, href: object) -> str:
    return f'{label}: {href}' if href else ''


def business_lines(message: dict) -> list[str]:
    lines = [line for line in message.get('lines') or [] if isinstance(line, dict)]
    out = [f"{line.get('label') or ''}: {line.get('text') or ''}" for line in lines]
    if out and message.get('urgent'):
        out[0] = msg('routes.urgent') + DOT + out[0]
    for kid in message.get('kids') or []:
        page = DOT + link_line(msg('routes.open_page'), kid['page']) if kid.get('page') else ''
        num = DOT + f"#{kid['number']} {kid.get('link') or ''}".strip() if kid.get('number') else ''
        out.append('\u2022 ' + str(kid.get('text') or '') + page + num)
    return out + [link_line(msg('routes.open_page'), message.get('page')), link_line(msg('routes.chat'), message.get('chat'))]


def mails_of(message: dict) -> list[tuple[str, tuple[str, str, str]]]:
    kind = message.get('kind')
    head = head_of(message)
    card = dict(message.get('card') or {})
    link = str(message.get('link') or '')
    if kind == 'card':
        rows = [c for c in message.get('cards') or [] if isinstance(c, dict)]
        out = []
        for index, row in enumerate(rows):
            lines = [str(row.get('what') or ''), link_line(msg('routes.chat'), row.get('chat'))]
            if index == len(rows) - 1 and message.get('more'):
                lines.append(link_line(msg('routes.more', count=message['more']), message.get('board')))
            out.append((str(row.get('key') or ''), mail.card_mail(row | {'status': 'open'}, str(row.get('link') or ''), head=head, body_lines=lines)))
        return out
    if kind in ('action', 'info'):
        return [(str(card.get('key') or ''), mail.card_mail(card | {'status': 'open'}, link, head=head, body_lines=business_lines(message)))]
    if kind == 'fixed':
        lines = [f"{msg('routes.fixed')}: {message.get('text') or ''}"]
        return [(str(card.get('key') or ''), mail.card_mail(card | {'status': 'cleared'}, link, head=head, body_lines=lines))]
    return [(str(card.get('key') or ''), mail.card_mail(card | {'status': 'open'}, link, head=head, body_lines=[str(message.get('text') or '')]))]


def remember(mark: tuple[str, str, str, str], state: str, text: str) -> None:
    with GUARD:
        DELIVERED[mark] = (time.monotonic(), state, text)


def known(marks: list[tuple[str, str, str, str]]) -> dict:
    now = time.monotonic()
    with GUARD:
        for mark in [m for m, (at, _, _) in DELIVERED.items() if now - at > KEEP_S]:
            del DELIVERED[mark]
        return {m: DELIVERED[m] for m in marks if m in DELIVERED}


def forget(marks: list[tuple[str, str, str, str]]) -> None:
    with GUARD:
        for mark in marks:
            DELIVERED.pop(mark, None)


def failed(reason: str, retry: bool = True, progress: bool = False) -> dict:
    return {'ok': False, 'status': 0, 'reason': reason} | ({} if retry else {'retry': False}) | ({'progress': True} if progress else {})


def post(ctx: Context, message: dict) -> dict:
    mailer = mailer_of(ctx)
    if mailer is None:
        return failed(msg('email.no_server'))
    given = config_of(ctx).get('recipients')
    problem = recipients_problem(given)
    if problem is not None:
        return failed(problem, retry=False)
    to = addresses(given)
    name = ctx.instance.name if ctx.instance is not None else ''
    mails = mails_of(message)
    marks = [(name, str(message.get('kind') or ''), key, addr.lower()) for key, _ in mails for addr in to]
    seen = known(marks)
    refused = {m[3]: text for m, (_, state, text) in seen.items() if state == REFUSED}
    started = time.monotonic()
    tried = False
    for key, (subject, text, html) in mails:
        for addr in to:
            mark = (name, str(message.get('kind') or ''), key, addr.lower())
            if mark in seen:
                continue
            if addr.lower() in refused:
                remember(mark, REFUSED, refused[addr.lower()])
                continue
            if tried and time.monotonic() - started > BUDGET_S:
                done = sum(1 for _, state, _ in known(marks).values() if state == SENT)
                return failed(msg('email.partial', sent=done, total=len(marks)), progress=True)
            tried = True
            try:
                mailer.send([addr], subject, text, html, kind=KIND)
            except mail.MailDown as e:
                return failed(e.text)
            except mail.MailRefused as e:
                if not e.recipient:
                    return failed(e.text)
                refused[addr.lower()] = e.text
                remember(mark, REFUSED, e.text)
                continue
            remember(mark, SENT, '')
    if not marks:
        return failed(msg('email.nothing'), retry=False)
    outcome = known(marks)
    forget(marks)
    if not any(state == SENT for _, state, _ in outcome.values()):
        return failed(next(iter(refused.values()), msg('email.nothing')), retry=False)
    if refused:
        shown = ', '.join(sorted(refused))[:SHOWN_MAX * 2]
        return {'ok': True, 'status': 200, 'reason': msg('email.some_refused', addresses=shown, reason=next(iter(refused.values())))}
    return {'ok': True, 'status': 200}


def probed(ctx: Context, count: int) -> str:
    found = ctx.peers(mail.PLUGIN_ID)
    try:
        said = mail.Mailer(dict(found[0].info.config), timeout=min(mail.TIMEOUT_S, BUDGET_S)).probe()
    except (mail.MailDown, mail.MailRefused) as e:
        raise ReadError(0, e.text) from None
    return msg('email.ready_one' if count == 1 else 'email.ready', server=said, count=count)


def test(ctx: Context) -> TestResult:
    config = config_of(ctx)
    problem = recipients_problem(config.get('recipients'))
    if problem is not None:
        return TestResult(False, problem, 0)
    if not str(config.get('tags') or '').strip() and not str(config.get('keys') or '').strip():
        return TestResult(False, msg('integrations.route_empty'), 0)
    if mailer_of(ctx) is None:
        return TestResult(False, msg('email.no_server'), 0)
    return connection.result(lambda: probed(ctx, len(addresses(config.get('recipients')))))


PLUGIN = Plugin(
    id=ID, name='Email route', version='0.1.0', category='chat',
    description='A route that mails chosen cards to a list of addresses, like a Teams route. Add one per list; it sends through the Mail server.', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.6', edition='community', capabilities=Capabilities(), uses=(mail.PLUGIN_ID,),
    fields=(Field('recipients', 'Recipients', 'text', required=True, check=recipients_problem,
                  help='Comma or space list of 1 to 50 email addresses, such as alice@example.com, ops@example.com. Each address gets a mail of its own, so no one sees the others.'),
            Field('tags', 'Card tags', 'text', help='Comma list of card tags this route takes, such as seo, developer. A route needs card tags or card keys.'),
            Field('keys', 'Card keys', 'text', help='Comma list of card key patterns this route takes, with * and ?, such as log|checkout-*|*.'),
            Field('min-sev', 'Lowest severity', 'choice', choices=SEVS, choice_labels={'warn': 'Warning', 'high': 'High', 'crit': 'Critical'}, help='Cards below it are not mailed on their own; empty uses the Email route lowest severity setting.')),
    settings=(Setting('repeat-days', 'Email route repeat days', 'Days before a card already mailed to a list is mailed to it again when it fires again or gets another tag; 0 mails every time',
                      'float', '7', low=0, high=365),
              Setting('min-sev', 'Email route lowest severity', 'Cards below this severity are not mailed to a list on their own; it is also the default of a key route with no lowest severity of its own, and a key route\'s own lowest severity replaces it, higher or lower. While it is not set, tag routes take cards from Warning up and key routes from High up',
                      'choice', 'high', choices=SEVS),
              Setting('fixed-posts', 'Email route fixed mails', 'Mails a fixed message to a business list when a card it was told about as info closes after that mail', 'bool', 'false')),
    test=test, outbound=Outbound(post, 'tags', None, keys_field='keys', sev_field='min-sev', target_field='recipients', ready=ready),
    setup=Setup(steps=(
        'Set up the Mail server integration first: Email routes send through it.',
        'Fill in Recipients with the addresses of the list, such as alice@example.com, ops@example.com.',
        'Fill in Card tags or Card keys, since a route needs one of them.',
        'Choose Test connection: it checks the Mail server answers, without sending a mail. Then Save & turn on.',
    )),
)
