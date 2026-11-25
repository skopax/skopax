import re

from skopax.messages import msg

CARD = 'application/vnd.microsoft.card.adaptive'
SCHEMA = 'http://adaptivecards.io/schemas/adaptive-card.json'
DOT = ' \u00b7 '
SEVS = ('crit', 'high', 'warn', 'info')
MARKS = re.compile(r'([\\`*_\[\]()<>#~])')


def text_block(text: str, **extra) -> dict:
    return {'type': 'TextBlock', 'text': text, 'wrap': True, **extra}


def person(email: str) -> str:
    local = email.split('@')[0]
    return ' '.join(w[:1].upper() + w[1:] for w in re.split(r'[._-]+', local) if w) or local


def escaped(text: object) -> str:
    return MARKS.sub(r'\\\1', ''.join(ch for ch in str(text or '') if ch.isprintable()))


def link(text: str, href: str) -> str:
    return f'[{text}]({href})'


def sev_label(sev: str) -> str:
    return msg(f'routes.sev.{sev}') if sev in SEVS else str(sev or '')


def lead(message: dict) -> str:
    return msg('routes.urgent') + DOT if message.get('urgent') else ''


def finding_rows(row: dict) -> list[dict]:
    lines = [line.strip() for line in str(row.get('finding') or '').splitlines() if line.strip()]
    return [text_block(f"**{msg('routes.finding')}:** {escaped(line)}" if i == 0 else escaped(line), size='Small') for i, line in enumerate(lines)]


def thread_rows(card: dict) -> list[dict]:
    bullets = [b for b in ((card or {}).get('threadSummary') or {}).get('bullets') or [] if isinstance(b, str) and b.strip()][:6]
    if not bullets:
        return []
    return [text_block(f"**{msg('routes.thread')}:**", size='Small')] + [text_block('\u2022 ' + escaped(b), size='Small') for b in bullets]


def card_rows(message: dict) -> list[dict]:
    blocks = []
    for c in message.get('cards') or []:
        head = f"{sev_label(c.get('sev'))}{DOT}**{escaped(c.get('svc') or '-')} / {escaped(c.get('label') or '?')}**" + (f"{DOT}#{c['number']}" if c.get('number') else '')
        blocks += [text_block(head, separator=True)] + ([text_block(escaped(c['cameBack']), weight='Bolder', size='Small')] if c.get('cameBack') else []) + [text_block(escaped(c.get('what')), isSubtle=True, size='Small')] + finding_rows(c) + thread_rows(c) + [
                   text_block(link(msg('routes.open'), c.get('link') or '') + DOT + link(msg('routes.chat'), c.get('chat') or ''), size='Small')]
    if message.get('more'):
        blocks.append(text_block(msg('routes.more', count=message['more']) + DOT + link(msg('routes.all'), message.get('board') or ''), size='Small'))
    return blocks


def kid_rows(message: dict) -> list[dict]:
    rows = []
    for k in message.get('kids') or []:
        page = DOT + link(msg('routes.open_page'), k['page']) if k.get('page') else ''
        num = DOT + link(f"#{k['number']}", k.get('link') or '') if k.get('number') else ''
        rows.append(text_block('\u2022 ' + str(k.get('text') or '') + page + num, size='Small'))
    return rows


def open_link(message: dict) -> str:
    return link(msg('routes.open_card') if (message.get('card') or {}).get('number') else msg('routes.open'), message.get('link') or '')


def links_row(message: dict) -> dict:
    number = (message.get('card') or {}).get('number')
    parts = ([f'#{number}'] if number else []) + ([link(msg('routes.open_page'), message['page'])] if message.get('page') else [])
    return text_block(DOT.join(parts + [open_link(message), link(msg('routes.chat'), message.get('chat') or '')]), size='Small')


def line_row(message: dict, line: dict, first: bool) -> dict:
    label, text = str(line.get('label') or ''), str(line.get('text') or '')
    if line.get('role') == 'why':
        return text_block(lead(message) + f'{label}: {text}', separator=True, weight='Bolder') if first else text_block(f'{label}: {text}', isSubtle=True, size='Small')
    return text_block(lead(message) + f'**{label}:** {text}', separator=True) if first else text_block(f'**{label}:** {text}')


def business_rows(message: dict) -> list[dict]:
    lines = [line for line in message.get('lines') or [] if isinstance(line, dict)]
    return [line_row(message, line, i == 0) for i, line in enumerate(lines)] + kid_rows(message) + thread_rows(message.get('card')) + [links_row(message)]


def fixed_rows(message: dict) -> list[dict]:
    return [text_block(f"\U0001F7E1 **{msg('routes.fixed')}:** {message.get('text') or ''}", separator=True), text_block(open_link(message), size='Small')]


def body_of(message: dict) -> list[dict]:
    kind = message.get('kind')
    if kind == 'card':
        return card_rows(message)
    if kind in ('action', 'info'):
        return business_rows(message)
    if kind == 'fixed':
        return fixed_rows(message)
    return [text_block(str(message.get('text') or ''), size='Small')]


def attribution_row(message: dict) -> list[dict]:
    line = message.get('attribution')
    if not isinstance(line, dict) or not str(line.get('text') or '').strip():
        return []
    href = str(line.get('href') or '')
    shown = escaped(line['text'])
    return [text_block(link(shown, href) if href.startswith('https://') and not any(ch in href for ch in ' ()') else shown, isSubtle=True, size='Small')]


def render(message: dict) -> dict:
    blocks = [text_block(str(message.get('title') or ''), weight='Bolder', size='Medium')]
    teams = {'width': 'Full'}
    mention = [e for e in message.get('mentions') or [] if isinstance(e, str) and e]
    if mention:
        blocks.append(text_block(msg('routes.cc', names=', '.join(f'<at>{person(e)}</at>' for e in mention)), size='Small'))
        teams['entities'] = [{'type': 'mention', 'text': f'<at>{person(e)}</at>', 'mentioned': {'id': e, 'name': person(e)}} for e in mention]
    content = {'$schema': SCHEMA, 'type': 'AdaptiveCard', 'version': '1.4', 'body': blocks + body_of(message) + attribution_row(message), 'msteams': teams}
    return {'type': 'message', 'attachments': [{'contentType': CARD, 'contentUrl': None, 'content': content}]}
