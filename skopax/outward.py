import re
from collections.abc import Callable

from skopax import brand
from skopax.redact import EMAIL, NATIONAL_PHONE, PHONE, REDACTED, SECRET_KEY, redact, redact_secrets

BULK_ROWS = 20
SPACED = ('plain', 'list')
KEPT_ROWS = 5
CUT_NOTE = 'Cut: {product} shows at most 5 records here'
FENCE = re.compile(r'^\s*(?:```|~~~)')
LIST_ROW = re.compile(r'^\s*(?:[-*+\u2022]|\d{1,4}[.)])\s+\S')
PIPE_ROW = re.compile(r'^\s*\|.*\|\s*$')
DELIMITER = re.compile(r'^\s*(?:\|\s*)?:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*(?:\|\s*)?$')
LOG_ROW = re.compile(r'\b(?:ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE|FATAL|CRITICAL|Exception|Traceback)\b')
ADDRESS = re.compile(r'\b\d{1,5}\s{1,5}[A-Z][\w.-]{0,40}(?:\s{1,5}[A-Z][\w.-]{0,40})?\s{1,5}(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Boulevard|Blvd|Drive|Dr|Way|Square|Sq)\b')
FULL_NAME = re.compile(r'^\W{0,5}[A-Z][a-z]{1,40}\s{1,5}[A-Z]\w{0,40}\s{0,5}[,;\t|]')
RECORD_MARKS = (EMAIL, PHONE, NATIONAL_PHONE, ADDRESS, FULL_NAME)
PERSONAL_KEY = r'(?:\w+[_-])?(?:e-?mail|phone|mobile|msisdn|iban|card|pan|tckn|national[_-]?id|ssn|(?:first|last|full|user|customer)?[_-]?name|address|street|birth|dob)s?(?:[_-]?(?:address|number|no|id|data|date|day))?\b'
PERSONAL_MARK = re.compile(rf'\b{PERSONAL_KEY}\s*[=:]\s*["\']?' + re.escape(REDACTED), re.I)
SECRET_MARK = re.compile(rf'(?i)(?:=|{SECRET_KEY}["\']?\s*:|\bBearer)\s*["\']?' + re.escape(REDACTED))
ID_FIELDS = frozenset({'key', 'keys', 'kind', 'ref', 'repo', 'path', 'lines', 'owner', 'state', 'num', 'id', 'url', 'email', 'name'})


def record_row(line: str) -> bool:
    if LOG_ROW.search(line):
        return False
    return bool(PERSONAL_MARK.search(line)) or line.count(REDACTED) > len(SECRET_MARK.findall(line)) or any(p.search(line) for p in RECORD_MARKS)


def row_kind(line: str) -> str | None:
    if PIPE_ROW.match(line):
        return 'pipe'
    if LIST_ROW.match(line):
        return 'list'
    return 'plain' if line.strip() and record_row(line) else None


def kinds_of(lines: list[str]) -> list[str | None]:
    out, fenced = [], False
    for line in lines:
        if FENCE.match(line):
            fenced = not fenced
            out.append(None)
        else:
            out.append('fence' if fenced else row_kind(line))
    after: list[str | None] = [None] * len(lines)
    following = None
    for i in range(len(lines) - 1, -1, -1):
        after[i] = following
        if lines[i].strip():
            following = out[i]
    before = None
    for i, line in enumerate(lines):
        if line.strip():
            before = out[i]
        elif out[i] is None and before in SPACED and before == after[i]:
            out[i] = before
    return out


def blocks(lines: list[str]) -> list[tuple[int, int, str]]:
    out, start, kind = [], 0, None
    for i, current in enumerate(kinds_of(lines)):
        if current != kind:
            if kind is not None:
                out.append((start, i, kind))
            start, kind = i, current
    if kind is not None:
        out.append((start, len(lines), kind))
    return out


def cut_block(rows: list[str], kind: str) -> list[str] | None:
    head = rows[:2] if kind == 'pipe' and len(rows) > 1 and DELIMITER.match(rows[1]) else []
    body = rows[len(head):]
    filled = [i for i, r in enumerate(body) if r.strip()]
    if len(filled) <= BULK_ROWS or sum(record_row(body[i]) for i in filled) * 2 <= len(filled):
        return None
    return head + body[:filled[KEPT_ROWS - 1] + 1] + [brand.fill(CUT_NOTE)]


def cut_bulk(text: str) -> str:
    lines = text.split('\n')
    done, at = [], 0
    for start, end, kind in blocks(lines):
        cut = cut_block(lines[start:end], kind)
        if cut is not None:
            done += lines[at:start] + cut
            at = end
    return '\n'.join(done + lines[at:]) if at else text


def outward(text: str, secrets: bool = True) -> str:
    if not isinstance(text, str) or not text:
        return text
    return cut_bulk(redact_secrets(text) if secrets else redact(text))


def masked_value(value: object, checked: Callable[[str], bool]) -> object:
    if isinstance(value, str):
        return outward(value, checked(value))
    if isinstance(value, list):
        return [masked_value(v, checked) if isinstance(v, str) else outward_args(v, checked, False) for v in value]
    return outward_args(value, checked, False)


def kept(key: str, value: object):
    if key == 'url':
        return redact_secrets(value) if isinstance(value, str) else value
    return value


def outward_args(args: object, checked: Callable[[str], bool] = lambda text: True, top: bool = True) -> object:
    if isinstance(args, list):
        return [outward_args(v, checked, top) for v in args]
    if not isinstance(args, dict):
        return args
    return {k: kept(k, v) if top and k in ID_FIELDS and not isinstance(v, (dict, list)) else masked_value(v, checked) for k, v in args.items()}
