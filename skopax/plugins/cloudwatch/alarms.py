import hashlib
import json
import logging
import re
from collections.abc import Mapping

from skopax import clock
from skopax.contract import PassResult, Sweep, anomaly, iso
from skopax.kit import access, flags
from skopax.redact import redact_secrets
from skopax.plugins.cloudwatch.aws import NAME, Aws, AwsError, seconds

log = logging.getLogger('skopax.plugins.cloudwatch')

PASS = 'cloudwatch'
SIG = 'cloudwatch'
NO_DATA = f'{PASS}:no-data'
SECTION = 'cloudwatchAlarms'
PART = 'alarms'
PERMISSION = 'the cloudwatch:DescribeAlarms permission'
HELD = '[held: the text reads like instructions to an AI]'
ALARM_TYPES = ['MetricAlarm', 'CompositeAlarm']
PAGE_SIZE = 100
MAX_ALARMS = 500
MAX_PREFIXES = 10
MAX_PAGES = 10
PREFIX_LIMIT = 255
NAME_LIMIT = 255
LABEL_LIMIT = 60
TEXT_LIMIT = 300
REASON_LIMIT = 200
SERVICE_LIMIT = 120
DOT = ' \u00b7 '
LEVELS = ('info', 'warn', 'high', 'crit')
LEVEL_WORDS = {'info': 'Info', 'warn': 'Warning', 'high': 'High', 'crit': 'Critical'}
DEFAULT_SEVERITY = 'high'
DEFAULT_MAP = 'crit=crit, critical=crit, warn=warn, warning=warn'
SERVICE_DIMENSIONS = ('ServiceName', 'FunctionName', 'DBInstanceIdentifier', 'DBClusterIdentifier', 'ClusterName', 'LoadBalancer', 'LoadBalancerName', 'TargetGroup',
                      'QueueName', 'TableName', 'CacheClusterId', 'ReplicationGroupId', 'AutoScalingGroupName', 'ApiName', 'StateMachineArn', 'TopicName',
                      'DomainName', 'BucketName', 'InstanceId')
WORD = re.compile(r'[a-z0-9]+')
CAPPED = f'read {MAX_ALARMS} alarms, more not read'
STATES = {'ALARM': 'in ALARM', 'INSUFFICIENT_DATA': 'without enough data (INSUFFICIENT_DATA)'}
LISTED = 200
COUNT_PAGES = 10
ROW_BYTES = 60000
METRIC_LIMIT = 200


def flat(text: object) -> str:
    return ' '.join(str(text if text is not None else '').split())


def masked(text: object, limit: int) -> str:
    return redact_secrets(flat(text))[:limit]


def planted(text: str) -> bool:
    return bool(flags.scan(PASS, text))


def clean(text: object, limit: int) -> str:
    shown = masked(text, limit)
    return HELD if planted(shown) else shown


def names_of(raw: str) -> list[str]:
    return list(dict.fromkeys(n.strip() for n in (raw or '').split(',') if n.strip()))


def severity_map(raw: str) -> tuple[dict[str, str], str]:
    terms: dict[str, str] = {}
    default = DEFAULT_SEVERITY
    for item in names_of(raw if raw is not None and raw.strip() else DEFAULT_MAP):
        term, sep, level = item.partition('=')
        term, level = term.strip().lower(), level.strip().lower()
        if not sep or level not in LEVELS:
            continue
        if term == '*':
            default = level
        elif WORD.fullmatch(term):
            terms[term] = level
    return terms, default


def severity_of(name: str, description: str, mapping: tuple[dict[str, str], str]) -> str:
    terms, default = mapping
    words = set(WORD.findall(f'{name} {description}'.lower()))
    found = [level for term, level in terms.items() if term in words]
    return max(found, key=LEVELS.index) if found else default


def map_problem(raw: str) -> str | None:
    for item in names_of(raw or ''):
        term, sep, level = item.partition('=')
        if not sep or level.strip().lower() not in LEVELS or not (term.strip() == '*' or WORD.fullmatch(term.strip().lower())):
            return 'give pairs such as crit=crit, warn=warn: a word, =, and crit, high, warn or info; *=level sets the default'
    return None


def dimensions_of(alarm: dict) -> list[tuple[str, str]]:
    out = []
    rows = [d for d in (alarm.get('Dimensions') or []) if isinstance(d, dict)]
    for m in alarm.get('Metrics') or []:
        metric = ((m or {}).get('MetricStat') or {}).get('Metric') if isinstance(m, dict) else None
        if isinstance(metric, dict):
            rows += [d for d in (metric.get('Dimensions') or []) if isinstance(d, dict)]
    for d in rows:
        name, value = str(d.get('Name') or ''), d.get('Value')
        if name and value is not None:
            out.append((name, str(value)))
    return out


def service_of(alarm: dict) -> str | None:
    found = dimensions_of(alarm)
    for wanted in SERVICE_DIMENSIONS:
        for name, value in found:
            if name == wanted:
                shown = clean(value, SERVICE_LIMIT)
                if shown and shown != HELD:
                    return shown
    return None


def metric_text(alarm: dict) -> str:
    if alarm.get('AlarmRule'):
        return f"composite alarm, rule {clean(alarm.get('AlarmRule'), TEXT_LIMIT)}"
    name, space = clean(alarm.get('MetricName') or '', NAME_LIMIT), clean(alarm.get('Namespace') or '', NAME_LIMIT)
    if not name:
        return 'metric math alarm' if alarm.get('Metrics') else ''
    dims = ', '.join(f'{clean(n, 60)}={clean(v, 80)}' for n, v in dimensions_of(alarm)[:5])
    stat = alarm.get('Statistic') or alarm.get('ExtendedStatistic') or ''
    parts = [f'metric {space} {name}'.replace('  ', ' ') + (f' ({dims})' if dims else '')]
    if stat:
        parts.append(clean(stat, 40))
    if alarm.get('ComparisonOperator') and alarm.get('Threshold') is not None:
        parts.append(f"{clean(alarm.get('ComparisonOperator'), 60)} {clean(alarm.get('Threshold'), 40)}")
    if alarm.get('EvaluationPeriods') and alarm.get('Period'):
        parts.append(f"over {clean(alarm.get('EvaluationPeriods'), 10)} x {clean(alarm.get('Period'), 10)} s")
    return ' '.join(parts)


def since_text(at: object) -> str:
    stamp = seconds(at)
    if stamp is None or stamp <= 0:
        return ''
    try:
        return f' since {clock.date(stamp)} {clock.label(stamp)}'
    except (OverflowError, OSError, ValueError):
        return ''


def started_of(at: object) -> str | None:
    stamp = seconds(at)
    if stamp is None or stamp <= 0:
        return None
    try:
        return iso(stamp)
    except (OverflowError, OSError, ValueError):
        return None


def key_name(alarm: dict, shown: str) -> str:
    if shown and shown != HELD:
        return shown
    arn = str(alarm.get('AlarmArn') or alarm.get('AlarmName') or '')
    return 'alarm ' + hashlib.sha256(arn.encode('utf-8')).hexdigest()[:12]


def card_of(alarm: dict, region: str, mapping: tuple[dict[str, str], str]) -> dict:
    name = clean(alarm.get('AlarmName'), NAME_LIMIT)
    description = clean(alarm.get('AlarmDescription') or '', TEXT_LIMIT)
    reason = clean(alarm.get('StateReason') or '', TEXT_LIMIT)
    state = str(alarm.get('StateValue') or 'ALARM')
    sev = severity_of(masked(alarm.get('AlarmName'), NAME_LIMIT), masked(alarm.get('AlarmDescription') or '', TEXT_LIMIT), mapping)
    if state != 'ALARM' and LEVELS.index(sev) > LEVELS.index('warn'):
        sev = 'warn'
    svc = service_of(alarm) or '-'
    kind = 'CompositeAlarm' if alarm.get('AlarmRule') else 'MetricAlarm'
    parts = [f"CloudWatch alarm {name} in {region} is {STATES.get(state, state)}{since_text(alarm.get('StateUpdatedTimestamp'))}"]
    if reason:
        parts.append(f'reason: {reason}')
    if description:
        parts.append(f'description: {description}')
    metric = metric_text(alarm)
    if metric:
        parts.append(metric)
    if alarm.get('ActionsEnabled') is False:
        parts.append('its actions are switched off in CloudWatch')
    label = name[:LABEL_LIMIT]
    keyed = key_name(alarm, name)
    x = anomaly(PASS, SIG, '-', svc, keyed, sev, 1, None, None, '', label, '; '.join(parts) + '.')
    x.update(key=f'{SIG}|{region}|{keyed}', ranKey=PASS, region=region, alarmName=name, alarmArn=masked(alarm.get('AlarmArn') or '', 400), alarmType=kind,
             alarmState=state, short=f'{sev.upper()} CloudWatch {label}' + (f' on {svc}' if svc != '-' else '') + f' in {region}')
    started = started_of(alarm.get('StateTransitionedTimestamp')) or started_of(alarm.get('StateUpdatedTimestamp'))
    if started:
        x['startedAt'] = started
    return x


def since_of(at: object) -> str | None:
    stamp = seconds(at)
    if stamp is None or stamp <= 0:
        return None
    try:
        return iso(stamp)
    except (OverflowError, OSError, ValueError):
        return None


def row_of(alarm: dict) -> dict:
    raw = str(alarm.get('AlarmName') or '')
    name = clean(raw, NAME_LIMIT)
    row = {'part': 'alarm', 'name': name, 'type': 'composite' if alarm.get('AlarmRule') else 'metric', 'state': clean(alarm.get('StateValue') or 'ALARM', 40),
           'reason': clean(alarm.get('StateReason') or '', REASON_LIMIT), 'since': since_of(alarm.get('StateUpdatedTimestamp')), 'metric': metric_text(alarm)[:METRIC_LIMIT]}
    keyed = key_name(alarm, name)
    if keyed != name:
        row['keyName'] = keyed
    if name != raw:
        row['noLink'] = True
    return row


def within(rows: list[dict], budget: int) -> tuple[list[dict], bool]:
    kept, used = [], 0
    for r in rows:
        used += len(json.dumps(r, ensure_ascii=False).encode('utf-8'))
        if used > budget:
            return kept, True
        kept.append(r)
    return kept, False


def scan(aws: Aws, config: Mapping[str, str], budget: int = COUNT_PAGES) -> tuple[list[dict], bool]:
    prefixes = [p[:PREFIX_LIMIT] for p in names_of(config.get('alarm-prefixes') or '')][:MAX_PREFIXES] or [None]
    seen: dict[str, dict] = {}
    calls = 0
    for prefix in prefixes:
        token = None
        while True:
            if calls >= budget:
                return list(seen.values()), True
            body: dict = {'AlarmTypes': ALARM_TYPES, 'MaxRecords': PAGE_SIZE}
            if prefix:
                body['AlarmNamePrefix'] = prefix
            if token:
                body['NextToken'] = token
            answer = aws.cloudwatch('DescribeAlarms', body)
            calls += 1
            for group in ('MetricAlarms', 'CompositeAlarms'):
                for a in answer.get(group) or []:
                    if isinstance(a, dict) and a.get('AlarmName'):
                        seen.setdefault(str(a.get('AlarmArn') or a.get('AlarmName')), a)
            token = answer.get('NextToken') if isinstance(answer.get('NextToken'), str) else None
            if not token:
                break
    return list(seen.values()), False


def board(aws: Aws, config: Mapping[str, str], rows: list[dict], capped: bool) -> dict:
    listed = {str(a.get('AlarmArn') or a.get('AlarmName')): a for a in rows}
    count = {'ALARM': sum(1 for a in rows if str(a.get('StateValue') or 'ALARM') == 'ALARM')}
    both = (config.get('insufficient-data') or '') == 'true'
    out: dict = {'alarmsCut': capped}
    try:
        everything, cut = scan(aws, config)
    except AwsError as e:
        out.update(counts={'ALARM': count['ALARM'], 'INSUFFICIENT_DATA': None, 'OK': None}, countsCut=False, countsError=clean(e.reason, REASON_LIMIT))
        everything, cut = [], False
    else:
        by = {s: sum(1 for a in everything if a.get('StateValue') == s) for s in ('INSUFFICIENT_DATA', 'OK')}
        if both:
            by['INSUFFICIENT_DATA'] = max(by['INSUFFICIENT_DATA'], sum(1 for a in rows if a.get('StateValue') == 'INSUFFICIENT_DATA'))
        out.update(counts=count | by, countsCut=cut)
    for a in everything:
        if a.get('StateValue') == 'INSUFFICIENT_DATA':
            listed.setdefault(str(a.get('AlarmArn') or a.get('AlarmName')), a)
    shown = sorted(listed.values(), key=lambda a: (str(a.get('StateValue') or 'ALARM') != 'ALARM', -(seconds(a.get('StateUpdatedTimestamp')) or 0)))
    out['listed'], cut = within([row_of(a) for a in shown[:LISTED]], ROW_BYTES)
    out['listCut'] = cut or len(shown) > LISTED
    return out


def pages(aws: Aws, state: str, prefix: str | None, room: int) -> tuple[list[dict], bool]:
    found: list[dict] = []
    token = None
    for _ in range(MAX_PAGES):
        body: dict = {'AlarmTypes': ALARM_TYPES, 'StateValue': state, 'MaxRecords': PAGE_SIZE}
        if prefix:
            body['AlarmNamePrefix'] = prefix
        if token:
            body['NextToken'] = token
        answer = aws.cloudwatch('DescribeAlarms', body)
        for group in ('MetricAlarms', 'CompositeAlarms'):
            found += [a for a in (answer.get(group) or []) if isinstance(a, dict) and a.get('AlarmName')]
        token = answer.get('NextToken') if isinstance(answer.get('NextToken'), str) else None
        if len(found) > room:
            return found[:room], True
        if not token:
            return found, False
    return found[:room], True


def alarms(aws: Aws, config: Mapping[str, str]) -> tuple[list[dict], bool]:
    prefixes = [p[:PREFIX_LIMIT] for p in names_of(config.get('alarm-prefixes') or '')][:MAX_PREFIXES] or [None]
    states = ['ALARM'] + (['INSUFFICIENT_DATA'] if (config.get('insufficient-data') or '') == 'true' else [])
    seen: dict[str, dict] = {}
    for state in states:
        for prefix in prefixes:
            rows, capped = pages(aws, state, prefix, MAX_ALARMS - len(seen) + 1)
            for a in rows:
                seen.setdefault(str(a.get('AlarmArn') or a.get('AlarmName')), a)
            if capped or len(seen) > MAX_ALARMS:
                return list(seen.values())[:MAX_ALARMS], True
    return list(seen.values()), False


def sweep(aws: Aws, config: Mapping[str, str], now: float, last_status: str | None) -> Sweep:
    try:
        rows, capped = alarms(aws, config)
    except AwsError as e:
        if e.kind == 'denied':
            card = access.card(PASS, NAME, PART, f'the {e.action} permission' if e.action else PERMISSION, 403, 'credentials', plural=True)
            return Sweep(PassResult(PASS, True, access.status('ok', [PART]), [card], {}, (access.ran_key(PASS, PART),)))
        return Sweep(PassResult(PASS, False, f'error: {clean(e.reason, REASON_LIMIT)}'))
    mapping = severity_map(config.get('severity-map') or '')
    cards: dict[str, dict] = {}
    for a in rows:
        card = card_of(a, aws.region, mapping)
        cards.setdefault(card['key'], card)
    tails = []
    if capped:
        tails.append(CAPPED)
        if CAPPED not in (last_status or ''):
            log.warning('a CloudWatch pass read only %d alarms; the other alarms keep their cards and none clear until it is under the cap', MAX_ALARMS)
    status = DOT.join(['ok'] + tails)
    seen = board(aws, config, rows, capped)
    listed = seen.pop('listed')
    summary = {'part': 'summary', 'at': iso(now), 'region': aws.region, 'alarms': len(rows), 'cards': len(cards), 'capped': capped,
               'insufficientData': (config.get('insufficient-data') or '') == 'true'} | seen
    keys = ((NO_DATA,) if capped else (PASS,)) + (access.ran_key(PASS, PART),)
    return Sweep(PassResult(PASS, True, status, list(cards.values()), {}, keys), sections={SECTION: [summary] + listed})
