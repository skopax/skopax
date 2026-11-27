import re
from urllib.parse import quote

from skopax import history

UI_KINDS = {'cards': ('card', 'open'), 'runs': ('run',), 'changes': ('change',), 'pages': ('knowledge', 'doc'), 'errors': ('errtpl',)}
GROUP_OF = {index_kind: ui_kind for ui_kind, index_kinds in UI_KINDS.items() for index_kind in index_kinds}
TITLE = 120
SUB = 140
ARROW = ' -> '
WEB = re.compile(r'https?://[^\s<>"\\]{1,500}', re.IGNORECASE)
LINKLESS = frozenset({'errtpl'})


def text_of(value, limit: int) -> str:
    return history.cut(value, limit) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''


def joined(*parts) -> str:
    return ' \u00b7 '.join(p for p in parts if p)


def card_row(row: dict) -> tuple[str, str, str, str]:
    key, num = row.get('key'), row.get('num')
    number = f'#{num}' if isinstance(num, int) and not isinstance(num, bool) else ''
    link = f'#c={num}' if number else f'#a={quote(str(key), safe="")}' if key else ''
    title = text_of(row.get('label'), TITLE) or text_of(key, TITLE)
    return title, joined(number, text_of(row.get('svc'), 60)), text_of(row.get('lastSeen') or row.get('clearedAt') or row.get('firstSeen'), 40), link


def run_row(row: dict) -> tuple[str, str, str, str]:
    ask = row.get('ask') if isinstance(row.get('ask'), dict) else {}
    question, recap = text_of(ask.get('text'), TITLE), text_of(row.get('recap'), SUB)
    run_id = row.get('id')
    link = f'/api/investigations/{quote(str(run_id), safe="")}' if run_id else ''
    return question or recap or text_of(row.get('mode'), 40), recap if question else '', text_of(row.get('startedAt'), 40), link


def change_row(row: dict) -> tuple[str, str, str, str]:
    ident = quote(str(row.get('id') or ''), safe='')
    if row.get('type') == 'report':
        return text_of(row.get('headline'), TITLE) or text_of(row.get('summary'), TITLE), text_of(row.get('summary'), SUB) if row.get('headline') else '', text_of(row.get('at'), 40), f'#r={ident}' if ident else ''
    moved = ARROW.join(x for x in (text_of(row.get('fromImage'), 60), text_of(row.get('toImage'), 60)) if x)
    return text_of(row.get('svc'), TITLE) or text_of(row.get('ns'), TITLE), moved or text_of(row.get('ns'), 60), text_of(row.get('at'), 40), f'#rel={ident}' if ident else ''


def page_row(row: dict) -> tuple[str, str, str, str]:
    return text_of(row.get('title'), TITLE), text_of(row.get('excerpt'), SUB), '', '#t=integrations'


def error_row(row: dict) -> tuple[str, str, str, str]:
    key = row.get('card')
    title = joined(text_of(row.get('svc'), 60), text_of(row.get('level'), 20))
    return title, text_of(row.get('template'), SUB), text_of(row.get('lastSeen'), 40), f'#a={quote(str(key), safe="")}' if isinstance(key, str) and key else ''


def doc_row(row: dict) -> tuple[str, str, str, str]:
    url = row.get('url')
    link = url if isinstance(url, str) and WEB.fullmatch(url) else '#t=integrations'
    return text_of(row.get('title'), TITLE), text_of(row.get('excerpt'), SUB), text_of(row.get('modified'), 40), link


SHAPERS = {'card': card_row, 'open': card_row, 'run': run_row, 'change': change_row, 'knowledge': page_row, 'doc': doc_row, 'errtpl': error_row}


def item_of(hit, semantic: bool) -> dict | None:
    shaper, group = SHAPERS.get(hit.kind), GROUP_OF.get(hit.kind)
    if shaper is None or group is None or not isinstance(hit.row, dict):
        return None
    title, sub, when, link = shaper(hit.row)
    if not title or (not link and hit.kind not in LINKLESS):
        return None
    item = {'kind': group, 'title': title, 'sub': sub, 'when': when, 'link': link, 'match': 'meaning' if semantic else 'words'}
    if semantic and hit.score is not None:
        item['score'] = hit.score
    return item


def items_of(result) -> list[dict]:
    out = [item_of(h, result.semantic) for h in result.items]
    return [i for i in out if i is not None]
