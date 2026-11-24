import re
import time
from collections.abc import Callable, Mapping

from skopax.contract import PassResult, Sweep, anomaly, iso
from skopax.kit import access
from skopax.plugins.cloudwatch.alarms import DOT, HELD, REASON_LIMIT, clean, masked, names_of, within
from skopax.plugins.cloudwatch.aws import NAME, Aws, AwsError

PASS = 'cloudwatch-logs'
SIG = 'log'
KEY = 'cloudwatch-logs'
SECTION = 'cloudwatchLogs'
PART = 'logs'
PERMISSION = 'the logs:StartQuery and logs:GetQueryResults permissions'
WINDOW_S = 900
WAIT_S = 8.0
POLL_S = 1.0
MAX_GROUPS = 50
GROUP_LIMIT = 512
PREFIX_GROUPS = 50
MAX_ROWS = 50
SAMPLE_LIMIT = 200
QUERY_LIMIT = 2000
DEFAULT_MIN = 20
HOUR_S = 3600
MAX_WINDOWS = 6
ROW_BYTES = 40000
HIGH_TIMES = 10
DONE = 'Complete'
FAILED = ('Failed', 'Cancelled', 'Timeout', 'Unknown')
FINISHED = 'QueryFinished'
NOT_FOUND = 'ResourceNotFoundException'
DEFAULT_QUERY = ('fields @log, @message | filter @message like /(?i)(error|exception|fatal)/ '
                 '| stats count(*) as n, latest(@message) as sample by @log | sort n desc | limit 50')
SOURCE_COMMAND = re.compile(r'(?:^|\|)\s*source\b', re.I | re.M)
ACCOUNT_PREFIX = re.compile(r'^\d{12}:')


class QueryTimedOut(AwsError):
    pass


def query_problem(text: str) -> str | None:
    if len(text or '') > QUERY_LIMIT:
        return f'a query is at most {QUERY_LIMIT} characters'
    if SOURCE_COMMAND.search(text or ''):
        return 'a query cannot pick its own log groups with SOURCE; set Log groups instead'
    return None


def groups_problem(raw: str) -> str | None:
    names = names_of(raw or '')
    if len(names) > MAX_GROUPS:
        return f'at most {MAX_GROUPS} log groups'
    if any(len(n) > GROUP_LIMIT or not re.fullmatch(r'[\w./#-]+\*?', n) for n in names):
        return 'give log group names (letters, digits, _ - / . #), comma-separated; end one with * for a prefix'
    return None


def wanted(config: Mapping[str, str]) -> list[str]:
    return names_of(config.get('log-groups') or '')[:MAX_GROUPS]


def resolve(aws: Aws, entries: list[str]) -> tuple[list[str], bool]:
    found: list[str] = []
    capped = False
    for entry in entries:
        if not entry.endswith('*'):
            found.append(entry)
            continue
        answer = aws.logs('DescribeLogGroups', {'logGroupNamePrefix': entry[:-1], 'limit': PREFIX_GROUPS})
        names = [str(g.get('logGroupName')) for g in (answer.get('logGroups') or []) if isinstance(g, dict) and g.get('logGroupName')]
        found += names
        capped = capped or bool(answer.get('nextToken'))
    unique = list(dict.fromkeys(found))
    return unique[:MAX_GROUPS], capped or len(unique) > MAX_GROUPS


def run_query(aws: Aws, groups: list[str], query: str, start: int, end: int, limit: int, wait: float = WAIT_S,
              pause: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> dict:
    started = aws.logs('StartQuery', {'logGroupNames': groups, 'queryString': query, 'startTime': start, 'endTime': end, 'limit': limit, 'queryLanguage': 'CWLI'})
    query_id = started.get('queryId')
    if not isinstance(query_id, str) or not query_id:
        raise AwsError('AWS logs:StartQuery: the answer has no query id', action='logs:StartQuery', answered=True)
    ends = monotonic() + wait
    try:
        while True:
            answer = aws.logs('GetQueryResults', {'queryId': query_id})
            status = str(answer.get('status') or '')
            if status == DONE:
                return answer
            if status in FAILED:
                raise AwsError(f'the Logs Insights query ended as {masked(status, 20)}', action='logs:GetQueryResults', answered=True, name=FINISHED)
            if monotonic() + POLL_S > ends:
                break
            pause(POLL_S)
    except AwsError as e:
        if e.name != FINISHED:
            stop(aws, query_id)
        raise
    stop(aws, query_id)
    raise QueryTimedOut(f'the Logs Insights query did not finish within {wait:g} s and was stopped', action='logs:GetQueryResults')


def stop(aws: Aws, query_id: str) -> None:
    try:
        aws.logs('StopQuery', {'queryId': query_id})
    except AwsError:
        pass


def missing_of(aws: Aws, groups: list[str]) -> list[str]:
    gone = []
    for name in groups:
        answer = aws.logs('DescribeLogGroups', {'logGroupNamePrefix': name, 'limit': 5})
        if name not in {str(g.get('logGroupName')) for g in (answer.get('logGroups') or []) if isinstance(g, dict)}:
            gone.append(name)
    return gone


def query_existing(aws: Aws, groups: list[str], query: str, start: int, end: int, limit: int, wait: float = WAIT_S,
                   pause: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> tuple[dict, list[str], list[str]]:
    try:
        return run_query(aws, groups, query, start, end, limit, wait, pause, monotonic), groups, []
    except AwsError as e:
        if e.name != NOT_FOUND or e.action != 'logs:StartQuery':
            raise
        gone = missing_of(aws, groups)
        kept = [g for g in groups if g not in gone]
        if not gone:
            raise
        if not kept:
            raise AwsError('no log group named ' + ', '.join(clean(g, GROUP_LIMIT) for g in gone), action='logs:StartQuery', answered=True) from None
    return run_query(aws, kept, query, start, end, limit, wait, pause, monotonic), kept, gone


def rows_of(answer: dict, limit: int) -> list[dict[str, str]]:
    out = []
    for row in (answer.get('results') or [])[:limit]:
        if isinstance(row, list):
            out.append({str(c.get('field')): str(c.get('value') if c.get('value') is not None else '') for c in row if isinstance(c, dict) and c.get('field')})
    return out


def group_of(value: str) -> str:
    return ACCOUNT_PREFIX.sub('', value or '')


def count_of(row: dict[str, str]) -> int:
    for name in ('n', 'count', 'count(*)'):
        try:
            return int(float(row.get(name, '')))
        except ValueError:
            continue
    return 0


def card_of(row: dict[str, str], region: str, least: int) -> dict | None:
    count = count_of(row)
    group = clean(group_of(row.get('@log') or row.get('logGroup') or ''), GROUP_LIMIT)
    if count < least or not group:
        return None
    sev = 'high' if count >= HIGH_TIMES * least else 'warn'
    sample = clean(row.get('sample') or '', SAMPLE_LIMIT)
    keyed = group if group != HELD else 'held log group'
    what = f'{count} error, exception or fatal lines in the last {WINDOW_S // 60} min in CloudWatch log group {group} ({region}), at or above {least}'
    if sample:
        what += f'; the latest: {sample}'
    label = f'{count} error lines'
    x = anomaly(PASS, SIG, '-', keyed, '', sev, count, None, None, '', label, what + '.', 5)
    x.update(key=f'{KEY}|{region}|{keyed}', ranKey=PASS, region=region, logGroup=group, msg=sample, short=f'{sev.upper()} CloudWatch {keyed} \u00d7{count} error lines')
    return x


def windows_of(row: object, now: int) -> list[dict]:
    if not isinstance(row, dict) or not isinstance(row.get('windows'), list):
        return []
    kept = []
    for w in row['windows']:
        if not isinstance(w, dict) or not all(isinstance(w.get(k), int) and not isinstance(w.get(k), bool) for k in ('from', 'to', 'n')):
            continue
        if now - HOUR_S < w['to'] <= now and w['from'] < w['to']:
            kept.append({'from': w['from'], 'to': w['to'], 'n': max(0, w['n']), 'sample': clean(w.get('sample') if isinstance(w.get('sample'), str) else '', SAMPLE_LIMIT)})
    return sorted(kept, key=lambda w: w['to'])[-MAX_WINDOWS:]


def hour_of(windows: list[dict], now: int) -> tuple[int, int]:
    total, seen, reach, floor = 0.0, 0, now, now - HOUR_S
    for w in sorted(windows, key=lambda w: w['to'], reverse=True):
        high, low = min(w['to'], reach), max(w['from'], floor)
        if high > low:
            total += w['n'] * (high - low) / (w['to'] - w['from'])
            seen += high - low
            reach = low
    return round(total), round(seen / 60)


def group_rows(groups: list[str], rows: list[dict[str, str]], now: float, previous: list | None) -> tuple[list[dict], bool]:
    found: dict[str, tuple[int, str]] = {}
    for row in rows:
        group = clean(group_of(row.get('@log') or row.get('logGroup') or ''), GROUP_LIMIT)
        if group and group not in found:
            found[group] = (count_of(row), clean(row.get('sample') or '', SAMPLE_LIMIT))
    old = {r.get('group'): r for r in previous or [] if isinstance(r, dict) and r.get('part') == 'group'}
    end = int(now)
    start = end - WINDOW_S
    out = []
    for raw in dict.fromkeys(groups):
        name = clean(raw, GROUP_LIMIT)
        n, sample = found.get(name, (0, ''))
        windows = windows_of(old.get(name), end) + [{'from': start, 'to': end, 'n': n, 'sample': sample}]
        hour, minutes = hour_of(windows, end)
        row = {'part': 'group', 'group': name, 'n': n, 'hour': hour, 'minutes': minutes, 'windows': windows}
        if name == HELD:
            row['keyName'] = 'held log group'
        if name != raw:
            row['noLink'] = True
        out.append(row)
    return within(sorted(out, key=lambda r: (-r['hour'], r['group'])), ROW_BYTES)


def least_of(config: Mapping[str, str]) -> int:
    try:
        return max(1, int(config.get('log-min-lines') or DEFAULT_MIN))
    except ValueError:
        return DEFAULT_MIN


def sweep(aws: Aws, config: Mapping[str, str], now: float, pause: Callable[[float], None] = time.sleep,
          monotonic: Callable[[], float] = time.monotonic, previous: list | None = None) -> Sweep:
    entries = wanted(config)
    if not entries:
        return Sweep(PassResult(PASS, True, 'off: no log groups set', [], {}, (PASS,)), sections={SECTION: []})
    query = (config.get('error-query') or '').strip() or DEFAULT_QUERY
    problem = query_problem(query)
    if problem:
        return Sweep(PassResult(PASS, False, f'error: {problem}'))
    try:
        groups, capped = resolve(aws, entries)
        if not groups:
            return Sweep(PassResult(PASS, False, 'error: no log group matches Log groups'))
        answer, groups, gone = query_existing(aws, groups, query, int(now) - WINDOW_S, int(now), MAX_ROWS, pause=pause, monotonic=monotonic)
    except AwsError as e:
        if e.kind == 'denied':
            card = access.card(PASS, NAME, PART, f'the {e.action} permission' if e.action else PERMISSION, 403, 'credentials', plural=True)
            return Sweep(PassResult(PASS, True, access.status('ok', [PART]), [card], {}, (access.ran_key(PASS, PART),)))
        return Sweep(PassResult(PASS, False, f'error: {clean(e.reason, REASON_LIMIT)}'))
    least = least_of(config)
    cards = {}
    found = rows_of(answer, MAX_ROWS)
    for row in found:
        card = card_of(row, aws.region, least)
        if card is not None:
            cards.setdefault(card['key'], card)
    stats = answer.get('statistics') if isinstance(answer.get('statistics'), dict) else {}
    scanned = stats.get('bytesScanned') if isinstance(stats.get('bytesScanned'), (int, float)) else 0
    tails = [f'{len(groups)} log groups'] + (['more log groups match than are read'] if capped else [])
    if gone:
        tails.append('no log group named ' + ', '.join(clean(g, GROUP_LIMIT) for g in gone))
    summary = {'part': 'summary', 'at': iso(now), 'region': aws.region, 'groups': len(groups), 'cards': len(cards), 'bytesScanned': int(scanned), 'capped': capped,
               'missingGroups': [clean(g, GROUP_LIMIT) for g in gone], 'least': least}
    shown, summary['rowsCut'] = group_rows(groups, found, now, previous)
    return Sweep(PassResult(PASS, True, DOT.join(['ok'] + tails), list(cards.values()), {}, (PASS, access.ran_key(PASS, PART))), sections={SECTION: [summary] + shown})
