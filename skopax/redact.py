import logging
import re
import threading
import time
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path

import regex

log = logging.getLogger('skopax.redact')

REDACTED = '[redacted]'
TIMEOUT_MARK = '[redacted: a redaction pattern timed out]'
BUDGET_S = 0.1
WARN_EVERY = 100
EMAIL = re.compile(r'(?<![\w-])[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+')
PHONE = re.compile(r'(?<![\w+])\+\d(?:[ ()-]{0,2}\d){6,14}(?!\d)')
WORDS = re.compile(r'[A-Za-z0-9]+(?: [A-Za-z0-9]+)*')
IBAN_HEAD = re.compile(r'[A-Za-z]{2}\d{2}[A-Za-z0-9]*')
IBAN_GROUPS = 9
BEARER = re.compile(r'\bBearer\s+[\w.~+/=-]{8,}', re.I)
JWT = re.compile(r'(?<![\w-])eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}')
DIGIT_RUN = re.compile(r'(?<![\d.])(?:\d[ -]?){10,18}\d(?!\d)(?!\.\d)')
NATIONAL_ID = re.compile(r'(?<![\w.])[1-9]\d{10}(?!\w)(?!\.\d)(?!,\d{1,2}(?!\d))')
MOBILE = r'5(?:0[15-9]|[3-6]\d)'
COUNTRY = r'(?:00)?90[ .-]?'
GROUPS = r'[ .-]\d{3}[ .-](?:\d{2}[ .-]\d{2}|\d{4})'
SPLIT = rf'{GROUPS}|[ .-]\d{{7}}'
NATIONAL_PHONE = re.compile(r'(?<![\w+./])(?<!\d-)(?:'
                            rf'(?:(?:{COUNTRY})?\((?:0[ .-]?)?{MOBILE}\)|0[ .-]?\({MOBILE}\))[ .-]?(?:\d{{3}}[ .-]?\d{{2}}[ .-]?\d{{2}}|\d{{3}}[ .-]?\d{{4}})'
                            rf'|0{MOBILE}(?:\d{{7}}|{SPLIT})'
                            rf'|0[ .-]{MOBILE}(?:{GROUPS})'
                            rf'|{COUNTRY}{MOBILE}(?:{SPLIT})'
                            rf'|(?<!\d[ .-]){MOBILE}(?:{GROUPS})'
                            r')(?!\w)(?![.:/-]\d)(?! \d{1,3}(?!\d))')
LOCALE_ROOT = Path(__file__).with_name('locale')
SECRET_WORDS_FILE = 'secret-words.txt'
SECRET_WORD = re.compile(r'[^\W\d_]+(?: [^\W\d_]+)*')
WORD_LENGTH_MAX = 40


def language_secret_words(root: Path = LOCALE_ROOT) -> tuple[str, ...]:
    words: dict[str, str] = {}
    try:
        files = sorted(root.glob(f'*/{SECRET_WORDS_FILE}'))
    except OSError as e:
        log.warning('language secret words not read (%s)', type(e).__name__)
        return ()
    for path in files:
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError) as e:
            log.warning('secret words of language %s not read (%s)', path.parent.name, type(e).__name__)
            continue
        for line in lines:
            word = ' '.join(line.split())
            if word and len(word) <= WORD_LENGTH_MAX and SECRET_WORD.fullmatch(word):
                words.setdefault(word.casefold(), word)
            elif word:
                log.warning('a secret word in %s is left out: use letters and spaces, at most %d characters', path.parent.name, WORD_LENGTH_MAX)
    return tuple(sorted(words.values(), key=lambda w: (-len(w), w.casefold())))


def _word_pattern(word: str) -> str:
    return r'[ _-]?'.join(re.escape(part) for part in word.split(' '))


SECRET_WORDS = '|'.join([r'pass(?:word|wd|phrase)?|pwd|secret|client[ _-]?secret|api[ _-]?key|access[ _-]?key|secret[ _-]?key|private[ _-]?key|token|credentials?']
                        + [_word_pattern(w) for w in language_secret_words()])
SECRET_KEY = (r'(?:\b|(?<=_)|(?-i:(?<=[a-z\d])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])))'
              rf'(?:(?:{SECRET_WORDS})s?)(?:[\'\u2019][^\W\d_]{{1,8}})?')
SECRET_VALUE = re.compile(rf'(?i)({SECRET_KEY}["\'\u2019\u201d`]?)([ \t]*(?:[:=]|\||=>|->)[ \t]*)("(?:[^"\\\n]|\\.)*"|\'[^\'\n]*\'|\u201c[^\u201d\n]*\u201d|\S+)')
SECRET_ELEMENT = re.compile(rf'(?i)(<[\w.:-]*?{SECRET_KEY}(?:\s[^<>]*)?>)([^<]+)(?=</)')
SECRET_HEADER = re.compile(rf'(?i){SECRET_KEY}\b')
CREDENTIALS = re.compile(r'://[^@/\s]+@')
PEM = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)', re.S)
HTML_BLOCKS = frozenset({'p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'blockquote', 'ul', 'ol', 'hr'})
HTML_CELLS = frozenset({'td', 'th'})
HTML_TABLE = re.compile(r'<table\b', re.I)
PIPE_RULE = re.compile(r':?-+:?')
RULES: list = [(), None]


class SlowRules:
    def __init__(self, logger: logging.Logger):
        self.log = logger
        self._counts: dict[tuple[str, str], int] = {}
        self._guard = threading.Lock()

    def __call__(self, owner: tuple[str, str] | None) -> None:
        if owner is None:
            return
        with self._guard:
            count = self._counts.get(owner, 0) + 1
            self._counts[owner] = count
        if count == 1 or count % WARN_EVERY == 0:
            plugin_id, rule_id = owner
            self.log.warning('plug-in %s: redaction %s was the slowest rule when the redaction budget ran out (count %d)', plugin_id, rule_id, count)


def use_rules(patterns: tuple | Callable[[], tuple], on_timeout: Callable | None = None) -> None:
    RULES[0] = patterns
    RULES[1] = on_timeout


def extra() -> tuple:
    given = RULES[0]
    try:
        return tuple(given() if callable(given) else given)
    except Exception as e:
        log.warning('redaction rules not read, using the built-in ones (%s)', type(e).__name__)
        return ()


def iban_ok(raw: str) -> bool:
    s = raw.replace(' ', '').upper()
    return int(''.join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def luhn(d: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(d)):
        v = int(c) * (2 if i % 2 else 1)
        total += v - 9 if v > 9 else v
    return total % 10 == 0


def national_id_ok(d: str) -> bool:
    if len(d) != 11 or not d.isdecimal() or d[0] == '0':
        return False
    n = [int(c) for c in d]
    return n[9] == (sum(n[0:9:2]) * 7 - sum(n[1:8:2])) % 10 and n[10] == sum(n[:10]) % 10


def _national_id(m: re.Match) -> str:
    return REDACTED if national_id_ok(m.group(0)) else m.group(0)


def _sensitive(d: str) -> bool:
    return 13 <= len(d) <= 19 and d[0] in '23456789' and luhn(d)


def _digit_run(m: re.Match) -> str:
    groups = re.findall(r'\d+', m.group(0))
    for i in range(len(groups)):
        for j in range(i + 1, len(groups) + 1):
            if _sensitive(''.join(groups[i:j])):
                return REDACTED
    return m.group(0)


def iban_at(words: list[str], i: int) -> int:
    if not IBAN_HEAD.fullmatch(words[i]):
        return 0
    for j in range(min(len(words), i + IBAN_GROUPS), i, -1):
        if j - i > 1 and any(len(w) != 4 for w in words[i:j - 1]):
            continue
        compact = ''.join(words[i:j])
        if 15 <= len(compact) <= 34 and iban_ok(compact):
            return j
    return 0


def _ibans(m: re.Match) -> str:
    words, out, i = m.group(0).split(' '), [], 0
    while i < len(words):
        end = iban_at(words, i)
        out.append(REDACTED if end else words[i])
        i = end or i + 1
    return ' '.join(out)


def _mask(m) -> str:
    return REDACTED if m.group(0) else ''


def _timed_out(took: list[tuple[float, object]]) -> str:
    sink = RULES[1]
    if sink is not None and took:
        try:
            sink(max(took, key=lambda t: t[0])[1])
        except Exception as e:
            log.warning('a redaction timeout could not be recorded (%s)', type(e).__name__)
    return TIMEOUT_MARK


def _core(text: str, cards: bool = True) -> str:
    for pattern in (BEARER, JWT, EMAIL):
        text = pattern.sub(REDACTED, text)
    text = WORDS.sub(_ibans, text)
    text = PHONE.sub(REDACTED, text)
    text = NATIONAL_PHONE.sub(REDACTED, text)
    text = NATIONAL_ID.sub(_national_id, text)
    return DIGIT_RUN.sub(_digit_run, text) if cards else text


def redact_lines(texts: list[str], cards: bool = True) -> list[str]:
    rules = extra()
    spent = 0.0
    took: list[tuple[float, object]] = []
    out: list[str] = []
    for text in texts:
        text = _core(text, cards)
        for item in rules:
            owner, pattern = item if isinstance(item, tuple) else (None, item)
            left = BUDGET_S - spent
            if left <= 0:
                return out + [_timed_out(took)] * (len(texts) - len(out))
            started = time.monotonic()
            try:
                text = pattern.sub(_mask, text, timeout=left) if isinstance(pattern, regex.Pattern) else pattern.sub(_mask, text)
            except TimeoutError:
                took.append((time.monotonic() - started, owner))
                return out + [_timed_out(took)] * (len(texts) - len(out))
            took.append((time.monotonic() - started, owner))
            spent += took[-1][0]
        out.append(text)
    return out


def redact(text: str) -> str:
    return redact_lines([text])[0]


def secrets_masked(text: str) -> str:
    text = PEM.sub(REDACTED, text)
    text = CREDENTIALS.sub('://***@', text)
    text = html_columns_masked(text)
    text = pipe_columns_masked(text)
    text = SECRET_ELEMENT.sub(lambda m: m.group(1) + REDACTED, text)
    return SECRET_VALUE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)


def redact_secrets(text: str) -> str:
    return redact(secrets_masked(text))


def colspan(attrs) -> int:
    value = str(dict(attrs).get('colspan') or '').strip()
    return max(1, int(value)) if value.isdecimal() and len(value) <= 3 else 1


def secret_cells(table: dict, row: list[tuple[str, str, int]]) -> list[int]:
    spans, at = [], 0
    for _, _, n in row:
        spans.append(range(at, at + n))
        at += n
    named = {c for (text, _, _), span in zip(row, spans) if SECRET_HEADER.search(text) for c in span}
    titled = len(row) == 1 and row[0][2] > 1
    header = not table['rows'] or (all(kind == 'th' for _, kind, _ in row) and bool(named or titled))
    table['rows'] += 1
    if header:
        table['secret'] = named or table['secret']
        return []
    return [i for i, ((text, _, _), span) in enumerate(zip(row, spans)) if text and table['secret'].intersection(span)]


class _Columns(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.starts = [0] + [m.end() for m in re.finditer('\n', text)]
        self.tables: list[dict] = []
        self.row: list[tuple[str, str, int]] | None = None
        self.places: list[tuple[int, int]] = []
        self.cell: list[str] | None = None
        self.kind = ('td', 1)
        self.opened = 0
        self.hidden: list[tuple[int, int]] = []

    def _at(self) -> int:
        line, column = self.getpos()
        return self.starts[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.tables.append({'secret': set(), 'rows': 0})
        elif tag == 'tr' and self.tables:
            self.row, self.places = [], []
        elif tag in HTML_CELLS and self.row is not None:
            self.cell = []
            self.kind = (tag, colspan(attrs))
            self.opened = self._at() + len(self.get_starttag_text() or '')
        elif tag in HTML_BLOCKS and self.cell is not None:
            self.cell.append('\n')

    def handle_endtag(self, tag):
        if tag in HTML_CELLS and self.row is not None and self.cell is not None:
            self.row.append((' '.join(''.join(self.cell).split()), *self.kind))
            self.places.append((self.opened, max(self.opened, self._at())))
            self.cell = None
        elif tag == 'tr' and self.row is not None and self.tables:
            self.hidden += [self.places[i] for i in secret_cells(self.tables[-1], self.row)]
            self.row = None
        elif tag == 'table' and self.tables:
            self.tables.pop()
        elif tag in HTML_BLOCKS and self.cell is not None:
            self.cell.append('\n')

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def unknown_decl(self, data):
        if data.startswith('CDATA[') and self.cell is not None:
            self.cell.append('\n' + data[6:] + '\n')


def html_columns_masked(text: str) -> str:
    if not HTML_TABLE.search(text):
        return text
    reader = _Columns(text)
    reader.feed(text)
    reader.close()
    parts, at = [], 0
    for start, end in sorted(reader.hidden):
        if start < at:
            continue
        parts += [text[at:start], REDACTED]
        at = end
    return ''.join(parts) + text[at:]


def _masked_cell(cell: str) -> str:
    body = cell.strip()
    if not body:
        return cell
    return cell[:len(cell) - len(cell.lstrip())] + REDACTED + cell[len(cell.rstrip()):]


def _delimiter(cells: list[str]) -> bool:
    filled = [c.strip() for c in cells if c.strip()]
    return bool(filled) and all(PIPE_RULE.fullmatch(c) for c in filled)


def pipe_columns_masked(text: str) -> str:
    if '|' not in text:
        return text
    lines = text.split('\n')
    marked: set[int] | None = None
    for i, line in enumerate(lines):
        if '|' not in line:
            marked = None
            continue
        cells = line.split('|')
        if marked is None:
            if i and '|' in lines[i - 1] and _delimiter(cells):
                marked = {j for j, cell in enumerate(lines[i - 1].split('|')) if SECRET_HEADER.search(cell)}
            continue
        if marked and not _delimiter(cells):
            lines[i] = '|'.join(_masked_cell(c) if j in marked else c for j, c in enumerate(cells))
    return '\n'.join(lines)


def redact_text(text: object, limit: int | None = None) -> str:
    raw = str(text)
    spaced = ''.join(' ' if ch.isspace() else ch for ch in raw if ch.isprintable() or ch.isspace())
    blanked = ''.join(ch if ch.isprintable() else ' ' for ch in raw)
    joined = ''.join(ch for ch in raw if ch.isprintable())
    outs = {form: redact(form) for form in dict.fromkeys((spaced, blanked, joined))}
    caught = [out for form, out in outs.items() if out != form]
    if TIMEOUT_MARK in outs.values():
        shown = TIMEOUT_MARK
    elif not caught:
        shown = spaced
    elif len({out.replace(' ', '') for out in caught}) == 1:
        shown = caught[0]
    else:
        shown = REDACTED
    return shown if limit is None else shown[:limit]
