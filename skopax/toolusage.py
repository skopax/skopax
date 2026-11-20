from skopax import tags
from skopax.investigator.tools import ERROR_LIMIT as TOOL_ERROR_LIMIT, NO_TOOL, PROPOSE_CARD_ACTION, PROPOSE_INVESTIGATION, PROPOSE_TICKET, READ_TOOLS, WRITE_TOOLS, tag_tool, filled
from skopax.investigator.toolset import CORE
from skopax.mcptools import PREFIX as BRIDGE_PREFIX

RECENT_ERRORS = 3
PRIMARY = 'primary'
ERROR_LIMIT = 300
CHECK = 'check'
GONE = 'gone'
OUTSIDE = 'mcp'
CHECK_TOOLS = {
    'selfreview': 'self_review',
}


def descriptions(extra: dict | None = None) -> dict[str, str]:
    tagged = [tag_tool()] if tags.all_tags() else []
    return {**(extra or {}), **{t['name']: filled(t)['description'] for t in READ_TOOLS + WRITE_TOOLS + tagged + [PROPOSE_TICKET, PROPOSE_CARD_ACTION, PROPOSE_INVESTIGATION]}}


def catalog(extra: dict | None = None) -> dict[str, str]:
    return dict(extra or {}) | {name: spec.kind for name, spec in CORE.items()}


def percentile(values: list[int], share: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def blank() -> dict:
    return {'calls': 0, 'runs': 0, 'errors': 0, 'truncated': 0, 'ms': [], 'lastAt': None, 'byTrigger': {}, 'recentErrors': [], 'held': 0, 'recentHeld': [], 'refused': 0, 'recentRefused': [], 'unknown': 0}


def kind_of(name: str, row: dict, kinds: dict[str, str]) -> str:
    if name in kinds:
        return kinds[name]
    return OUTSIDE if name.startswith(BRIDGE_PREFIX) and row['unknown'] < row['calls'] else GONE


def add_checks(rows: dict[str, dict], checks: list[dict], tools: dict[str, str] | None = None) -> int:
    counted = 0
    known = {**CHECK_TOOLS, **(tools or {})}
    for c in checks:
        tool = known.get(str(c.get('check') or c.get('name') or ''))
        if not tool:
            continue
        counted += 1
        row = rows.setdefault(tool, blank())
        row['calls'] += 1
        if c.get('ms') is not None:
            row['ms'].append(int(c['ms']))
        row['byTrigger'][CHECK] = row['byTrigger'].get(CHECK, 0) + 1
        by = row.setdefault('byCheck', {}).setdefault(c['name'], {'runs': 0, 'errors': 0})
        by['runs'] += 1
        at = c.get('at')
        if at and (row['lastAt'] is None or at > row['lastAt']):
            row['lastAt'] = at
        if not c.get('ok', True):
            row['errors'] += 1
            by['errors'] += 1
            if len(row['recentErrors']) < RECENT_ERRORS:
                row['recentErrors'].append({'at': at, 'check': c['name'], 'trigger': CHECK, 'error': str(c.get('error') or 'failed')[:ERROR_LIMIT]})
    return counted


def summary(runs: list[dict], days: int, since: str, checks: list[dict] | None = None, plugin_tools=None) -> dict:
    kinds, said = catalog(plugin_tools.catalog() if plugin_tools else None), descriptions(plugin_tools.descriptions() if plugin_tools else None)
    rows: dict[str, dict] = {}
    with_tools = 0
    for r in runs:
        calls = r.get('toolCalls') or []
        with_tools += 1 if calls else 0
        seen: set[str] = set()
        for c in calls:
            name = c.get('name') or '?'
            row = rows.setdefault(name, blank())
            row['calls'] += 1
            row['ms'].append(int(c.get('ms') or 0))
            row['truncated'] += 1 if c.get('truncated') else 0
            trigger = r.get('trigger') or 'other'
            row['byTrigger'][trigger] = row['byTrigger'].get(trigger, 0) + 1
            at = r.get('startedAt')
            if at and (row['lastAt'] is None or at > row['lastAt']):
                row['lastAt'] = at
            if name == 'kube' or c.get('cluster'):
                fallback = (plugin_tools.default_instance(name) if plugin_tools is not None else None) or PRIMARY
                where = row.setdefault('byCluster', {}).setdefault(c.get('cluster') or fallback, {'calls': 0, 'errors': 0})
                where['calls'] += 1
                where['errors'] += 1 if c.get('error') and not c.get('held') and not c.get('refused') else 0
            if c.get('error') and c.get('held'):
                row['held'] += 1
                if len(row['recentHeld']) < RECENT_ERRORS:
                    row['recentHeld'].append({'at': at, 'runId': r.get('_id'), 'trigger': trigger, 'held': c['held']})
            elif c.get('error') and c.get('refused'):
                row['refused'] += 1
                if len(row['recentRefused']) < RECENT_ERRORS:
                    row['recentRefused'].append({'at': at, 'runId': r.get('_id'), 'trigger': trigger, 'error': str(c['error'])[:ERROR_LIMIT]})
            elif c.get('error'):
                row['errors'] += 1
                row['unknown'] += 1 if c['error'] == NO_TOOL.format(name=name)[:TOOL_ERROR_LIMIT] else 0
                if len(row['recentErrors']) < RECENT_ERRORS:
                    row['recentErrors'].append({'at': at, 'runId': r.get('_id'), 'trigger': trigger, 'error': str(c['error'])[:ERROR_LIMIT]})
            if name not in seen:
                seen.add(name)
                row['runs'] += 1
    check_runs = add_checks(rows, checks or [], plugin_tools.check_tools() if plugin_tools else None)
    tools = []
    for name in sorted(set(kinds) | set(rows)):
        row = rows.get(name)
        if row is None:
            tools.append({'name': name, 'kind': kinds[name], 'calls': 0, 'runs': 0, 'errors': 0, 'errorRate': None, 'truncated': 0,
                          'p50Ms': None, 'p95Ms': None, 'maxMs': None, 'lastAt': None, 'byTrigger': {}, 'recentErrors': [],
                          'held': 0, 'recentHeld': [], 'refused': 0, 'recentRefused': []})
            continue
        tools.append({'name': name, 'kind': kind_of(name, row, kinds), 'calls': row['calls'], 'runs': row['runs'], 'errors': row['errors'],
                      'errorRate': round(row['errors'] / row['calls'], 4), 'truncated': row['truncated'],
                      'p50Ms': percentile(row['ms'], 0.5), 'p95Ms': percentile(row['ms'], 0.95), 'maxMs': max(row['ms']) if row['ms'] else None,
                      'lastAt': row['lastAt'], 'byTrigger': row['byTrigger'], 'recentErrors': row['recentErrors'],
                      'held': row['held'], 'recentHeld': row['recentHeld'], 'refused': row['refused'], 'recentRefused': row['recentRefused'],
                      **({'byCluster': row['byCluster']} if row.get('byCluster') else {}), **({'byCheck': row['byCheck']} if row.get('byCheck') else {})})
    for t in tools:
        if t['name'] in said:
            t['description'] = said[t['name']]
    tools.sort(key=lambda t: (t['calls'] == 0, -t['calls'], t['name']))
    used = [t for t in tools if t['calls']]
    for t in tools:
        for field in ('recentErrors', 'recentHeld', 'recentRefused'):
            t[field] = sorted(t[field], key=lambda e: str(e.get('at') or ''), reverse=True)[:RECENT_ERRORS]
    return {'days': days, 'since': since, 'runs': len(runs), 'runsWithTools': with_tools, 'checkRuns': check_runs, 'calls': sum(t['calls'] for t in used),
            'errors': sum(t['errors'] for t in used), 'held': sum(t['held'] for t in used), 'refused': sum(t['refused'] for t in used), 'toolsUsed': len(used), 'toolsDefined': len(kinds) + sum(1 for t in tools if t['kind'] == OUTSIDE), 'tools': tools}


def with_status(usage: dict, toolset, probe: dict | None) -> dict:
    for row in usage['tools']:
        if row['kind'] == 'read' or (row['kind'] not in (GONE, OUTSIDE) and (row['name'] not in CORE or CORE[row['name']].needs)):
            row['status'] = toolset.status(row['name'], probe)
    return usage
