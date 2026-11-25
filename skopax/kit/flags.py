import re

MAX_FLAGS = 5
EXCERPT = 120
NOT_SAID = r"(?<!\bnot\s)(?<!n't\s)(?<!n\u2019t\s)(?<!\bcannot\s)(?<!\bnever\s)"
HAND_OUT = NOT_SAID + r'\b(?:give|send|tell|show|print|reveal|dump|paste|share|display|output|leak|expose|list|pull|extract|download)'
TO_ME = r'\s+(?:me|us)'
WHOSE = r'(?:\s+(?:the|your|its|our|all(?:\s+the)?))?(?:\s+(?!for\b|of\b|about\b|on\b|with\b|in\b|from\b|to\b|per\b)[\w-]+)?'
NOT_OPS = (r'(?![\w-])(?![\s_-]*(?:resets?|errors?|failures?|attempts?|policy|changes?|expir\w*|rotations?|count\w*|usage|use[ds]?|limits?|budget|costs?|spend|validation|checks?|logs?'
           r'|refresh\w*|status|length|format|type|fields?|inputs?|box|prompts?|screens?|pages?|forms?|dialogs?|hints?|strength|masked|issued|per|minted|created|granted|revoked|rate))')
FOR_WHAT = r'(?=\s+(?:for|of)\s+(?!(?:the\s+)?(?:last|past|this|today|yesterday)\b)[\w-])'
PEOPLE = r'(?:customers?|users?|clients?|members?|subscribers?|employees?|staff|accounts?|people)'
DATA = (r'(?:phone\s+numbers?|phones|e-?mails?|e-?mail\s+addresses|(?:home\s+)?addresses|contact\s+(?:details|data|info)|personal\s+data|customer\s+data|user\s+data'
        r'|birth\s*dates?|dates\s+of\s+birth|national\s+ids?|card\s+numbers?|ibans?|salar(?:y|ies))')
WITH_DATA = rf'(?:\s+(?:with|and|including|plus)\s+(?:their\s+|the\s+|all\s+|its\s+)?(?:[\w-]+\s+)?{DATA}|\s+(?:table|list|data|database|records|details|dump|csv)\b)'
ALL = r'\s+(?:all|every|the\s+(?:whole|full|entire)(?:\s+list\s+of)?)(?:\s+(?:of\s+)?(?:the|our))?(?:\s+[\w-]+)?'
JOB = r'(?:job|jobs|task|tasks|batch|cron|endpoint|api|report|failed|failures?|failing|errors?|service|worker|queue|step|count|metric|bounced)'
NOT_JOB = rf'(?![\w-])(?!(?:\s+[\w-]+){{0,2}}\s+{JOB}\b)'


def secret(nouns: str) -> re.Pattern:
    return re.compile(rf'(?:{HAND_OUT}{TO_ME}{WHOSE}\s+{nouns}{NOT_OPS}|{HAND_OUT}{WHOSE}\s+{nouns}{NOT_OPS}{FOR_WHAT})', re.I)


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
    ('ignore instructions', re.compile(r'\bignore\s+(?:all\s+)?(?:of\s+)?your\s+(?:(?:previous|prior|earlier)\s+)?instructions\b', re.I)),
    ('give me the password', secret(r'(?:passwords?|passwd|passphrases?|credentials)')),
    ('print the api key', secret(r'(?:api[\s_-]?keys?|access[\s_-]?keys?|secret[\s_-]?keys?|private[\s_-]?keys?|client[\s_-]?secrets?|connection[\s_-]?strings?)')),
    ('show the token', secret(r'(?:tokens?|bearer|jwt)')),
    ('list all customers', re.compile(rf'{HAND_OUT}(?:{TO_ME})?{ALL}\s+(?:{PEOPLE}{WITH_DATA}|{DATA}){NOT_JOB}', re.I)),
    ('export the users', re.compile(rf'{NOT_SAID}\bexport\s+(?:the|all|every|our)(?:\s+(?:of\s+)?(?:the|our))?(?:\s+[\w-]+)?\s+(?:{PEOPLE}|{DATA}){NOT_JOB}', re.I)),
)
MOVE = r'[^\n.]{0,80}?\b(?:post|send|slack|mail|e-?mail|jira|forward|upload)(?:s|ed|ing)?\b'
ASKED = tuple(pattern.pattern for phrase, pattern in PHRASES if phrase in ('give me the password', 'print the api key', 'show the token', 'list all customers', 'export the users'))
PHRASES += (('move a secret out', re.compile(rf'(?:{"|".join(ASKED)}){MOVE}', re.I)),)
SOFT = ('known noise', 'assistant')
REQUESTS = ('give me the password', 'print the api key', 'show the token', 'list all customers', 'export the users')
PLANTED = tuple(p for p, _ in PHRASES if p not in REQUESTS)
ESCAPE = re.compile(r'\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))')
SIMPLE = {'n': '\n', 'r': '\n', 't': ' ', 'b': ' ', 'f': ' ', '"': '"', '\\': '\\', '/': '/'}


def unescaped(text: str) -> str:
    text = ESCAPE.sub(lambda m: chr(int(m.group(1), 16)) if m.group(1) else SIMPLE[m.group(2)], text)
    return text.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'replace')


def scan(tool: str, text: str, skip: tuple[str, ...] = ()) -> list[dict]:
    if not isinstance(text, str):
        return []
    text = unescaped(text)
    out = []
    for phrase, pattern in PHRASES:
        if phrase in skip:
            continue
        m = pattern.search(text)
        if not m:
            continue
        start = max(0, m.start() - 40)
        out.append({'tool': tool, 'phrase': phrase, 'excerpt': text[start:start + EXCERPT]})
        if len(out) == MAX_FLAGS:
            break
    return out


def asked(tool: str, text: str) -> list[dict]:
    return scan(tool, text, skip=PLANTED)
