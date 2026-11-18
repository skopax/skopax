import re

MAX_FLAGS = 5
EXCERPT = 120
PHRASES = (
    ('ignore previous', re.compile(r'\bignore\s+(?:all\s+|the\s+)?(?:previous|prior|above)\b', re.I)),
    ('do not report', re.compile(r"\b(?:do\s+not|don[\u2019']?t)\s+(?:report|flag|alert)", re.I)),
    ('disregard', re.compile(r'\bdisregard\b', re.I)),
    ('mark it handled', re.compile(r'\bmark\s+(?:(?:it|this|the\s+card|them|these)\s+)?(?:as\s+)?handled\b', re.I)),
    ('known noise', re.compile(r'\bknown[\s_-]+noise\b', re.I)),
    ('system prompt', re.compile(r'\bsystem[\s_-]+prompt', re.I)),
    ('ignore all', re.compile(r'\bignore\s+all\b', re.I)),
    ('you are', re.compile(r'\byou\s+are\s+(?:an?|the|now)\b', re.I)),
    ('assistant', re.compile(r'(?<![\w/-])assistant(?![\w/-])(?!\s+service)', re.I)),
)
ESCAPE = re.compile(r'\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))')
SIMPLE = {'n': '\n', 'r': '\n', 't': ' ', 'b': ' ', 'f': ' ', '"': '"', '\\': '\\', '/': '/'}


def unescaped(text: str) -> str:
    text = ESCAPE.sub(lambda m: chr(int(m.group(1), 16)) if m.group(1) else SIMPLE[m.group(2)], text)
    return text.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'replace')


def scan(tool: str, text: str) -> list[dict]:
    if not isinstance(text, str):
        return []
    text = unescaped(text)
    out = []
    for phrase, pattern in PHRASES:
        m = pattern.search(text)
        if not m:
            continue
        start = max(0, m.start() - 40)
        out.append({'tool': tool, 'phrase': phrase, 'excerpt': text[start:start + EXCERPT]})
        if len(out) == MAX_FLAGS:
            break
    return out
