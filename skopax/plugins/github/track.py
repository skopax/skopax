import datetime as dt
import re

from skopax.contract import Context, Denied, InstanceInfo, Missing, PassResult, SourceDown, Sweep, anomaly, iso
from skopax.kit import access
from skopax.plugins.github.client import GITHUB_URL, PERMISSIONS, split_name

CHECK = 'ghdeploy'
SECTION = 'github'
NS = 'github'
MAX_REPOS = 20
DEPLOYMENTS = 10
RELEASES = 5
STATE_CALLS = 5
CARD_SECONDS = 24 * 3600
FINAL = ('success', 'failure', 'error', 'inactive')
FAILED = ('failure', 'error')
SHA = re.compile(r'[0-9a-f]{7,40}')
FULL_NAME = re.compile(r'[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}')
ENV_KEEP = re.compile(r'[^A-Za-z0-9._-]+')
SECTION_HELP = {'github': 'The tracked GitHub repositories of the instance: their last deployments with state and their last releases, newest first'}


def seconds(text: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def tracked(config) -> list[str]:
    owner = str(config.get('owner') or '').strip()
    names = [n for n in re.split(r'[,;\s]+', str(config.get('repos') or '')) if n]
    return list(dict.fromkeys(n for n in names if split_name(owner, n)))[:MAX_REPOS]


def prior_of(rows: list | None, full: str) -> dict:
    for row in rows or []:
        if isinstance(row, dict) and str(row.get('repo') or '').lower() == full.lower():
            return row
    return {}


def deployment_rows(github, repo: dict, listed: list[dict], before: dict, budget: list[int]) -> list[dict]:
    known = {d.get('id'): d for d in before.get('deployments') or [] if isinstance(d, dict)}
    out = []
    for d in listed[:DEPLOYMENTS]:
        if not isinstance(d, dict) or d.get('id') is None:
            continue
        seen = known.get(d['id']) or {}
        state, url = seen.get('state'), seen.get('url')
        if state not in FINAL and budget[0] > 0:
            budget[0] -= 1
            latest = github.deployment_state(repo, d['id']) or {}
            state, url = latest.get('state') or 'pending', latest.get('log_url') or latest.get('environment_url') or url
        out.append({'id': d['id'], 'sha': str(d.get('sha') or ''), 'ref': d.get('ref'), 'env': str(d.get('environment') or 'production'), 'at': d.get('created_at'),
                    'by': (d.get('creator') or {}).get('login'), 'state': state or 'pending', 'url': url, 'pull': seen.get('pull')})
    for i, row in enumerate(out):
        older = next((x for x in out[i + 1:] if x['env'] == row['env']), None)
        row['from'] = older['sha'] if older else None
    return out


def release_rows(listed: list[dict]) -> list[dict]:
    out = [{'id': r.get('id'), 'tag': r.get('tag_name'), 'name': (r.get('name') or '')[:200], 'at': r.get('published_at'), 'by': (r.get('author') or {}).get('login'), 'url': r.get('html_url'),
            'pre': bool(r.get('prerelease'))} for r in listed if isinstance(r, dict) and not r.get('draft') and r.get('published_at') and r.get('tag_name')]
    return out[:RELEASES]


def failed_cards(github, repo: dict, rows: list[dict], now: float) -> list[dict]:
    cards, envs = [], set()
    for d in rows:
        if d['env'] in envs:
            continue
        envs.add(d['env'])
        at = seconds(d.get('at'))
        if d['state'] not in FAILED or at is None or now - at >= CARD_SECONDS:
            continue
        if d.get('pull') is None:
            try:
                pulls = github.commit_pulls(repo, d['sha'])
                d['pull'] = next((p['number'] for p in pulls if isinstance(p, dict) and isinstance(p.get('number'), int)), 0)
            except (Missing, Denied):
                d['pull'] = 0
            except SourceDown:
                d['pull'] = None
        env = ENV_KEEP.sub('-', d['env'])[:40] or 'production'
        what = (f"GitHub deployment {d['id']} of {repo['full']} to {d['env']} ended {d['state']}: commit {d['sha'][:8]} from {d.get('ref') or '?'}"
                + (f", by {d['by']}" if d.get('by') else '') + (f", pull request #{d['pull']}" if d.get('pull') else '')
                + (f", previous deployment {d['from'][:8]}" if d.get('from') else '') + '. The commit and pull request links are on the card; github_compare from the previous deployment lists what went out.')
        x = anomaly(CHECK, 'github-deploy', NS, repo['name'], env, 'high', 1, None, None, '', 'deployment failed', what, short=f"HIGH {repo['name']} {d['env']} deployment {d['state']}")
        x['github'] = {'repo': repo['full'], 'sha': d['sha'], 'pull': d['pull'] or None, 'deployment': d['id']}
        cards.append(x)
    return cards


def run(ctx: Context) -> Sweep:
    config = ctx.instance.config
    names = tracked(config)
    if not names:
        return Sweep(PassResult(CHECK, True, 'ok \u00b7 no repositories to track'))
    github = ctx.client()
    if github is None or not github.enabled:
        return Sweep(PassResult(CHECK, False, 'error: GitHub is not set up'))
    previous = ctx.previous(SECTION)
    rows, cards, missing = [], [], []
    try:
        for name in names:
            repo = github.repo(name)
            if repo is None:
                missing.append(name)
                continue
            before = prior_of(previous, repo['full'])
            try:
                deployments = deployment_rows(github, repo, github.deployments(repo, DEPLOYMENTS), before, [STATE_CALLS])
                releases = release_rows(github.releases(repo, RELEASES + 2))
            except Missing:
                missing.append(name)
                continue
            cards += failed_cards(github, repo, deployments, ctx.now)
            rows.append({'repo': repo['full'], 'name': repo['name'], 'web': repo['web'], 'checkedAt': iso(ctx.now), 'deployments': deployments, 'releases': releases})
    except Denied as e:
        card = access.card(CHECK, 'GitHub', e.part, e.permission or PERMISSIONS.get(e.part, ''))
        return Sweep(PassResult(CHECK, True, f'ok \u00b7 no access to {e.part}', [card], {}, (access.ran_key(CHECK, e.part),)))
    except SourceDown as e:
        return Sweep(PassResult(CHECK, False, f'error: {e.text}'))
    status = 'ok' + (f" \u00b7 not found or not visible: {', '.join(missing)}" if missing else '')
    return Sweep(PassResult(CHECK, True, status, cards, {}, (CHECK,) + tuple(access.ran_key(CHECK, p) for p in ('deployments', 'releases', 'repositories'))), sections={SECTION: rows})


def release_rows_of(section: dict | None, since: float) -> list[dict]:
    if not isinstance(section, dict) or not isinstance(section.get('repo'), str) or not FULL_NAME.fullmatch(section['repo']):
        return []
    full, name = section['repo'], section['repo'].split('/')[-1]
    out = []
    for d in section.get('deployments') or []:
        at = seconds(d.get('at')) if isinstance(d, dict) else None
        if at is None or at < since or not d.get('sha'):
            continue
        out.append({'id': f"github-{full}-d{d['id']}", 'ns': NS, 'svc': name, 'at': iso(at), 'fromImage': str(d.get('from') or '?')[:8], 'toImage': str(d['sha'])[:8],
                    'title': f"{d.get('env')} deployment {d.get('state')}", NS: {'kind': 'deployment', 'env': d.get('env'), 'state': d.get('state'), 'by': d.get('by'), 'url': d.get('url')}})
    releases = [r for r in section.get('releases') or [] if isinstance(r, dict)]
    for i, r in enumerate(releases):
        at = seconds(r.get('at'))
        if at is None or at < since or not r.get('tag'):
            continue
        older = releases[i + 1]['tag'] if i + 1 < len(releases) else None
        out.append({'id': f"github-{full}-r{r['id']}", 'ns': NS, 'svc': name, 'at': iso(at), 'fromImage': str(older or '?'), 'toImage': str(r['tag']),
                    'title': r.get('name') or str(r['tag']), NS: {'kind': 'release', 'by': r.get('by'), 'url': r.get('url'), 'prerelease': r.get('pre')}})
    return out


def releases(rows: list[dict], since: float) -> list[dict]:
    return [r for row in rows for r in release_rows_of(row, since)]


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    if card.get('src') != CHECK:
        return None
    told = card.get('github') if isinstance(card.get('github'), dict) else {}
    full, sha, pull = str(told.get('repo') or ''), str(told.get('sha') or ''), told.get('pull')
    owner = str(info.config.get('owner') or '')
    if not FULL_NAME.fullmatch(full) or full.split('/')[0].lower() != owner.lower():
        return []
    web = f"{(info.config.get('url') or GITHUB_URL).rstrip('/')}/{full}"
    out = [{'label': f'Commit {sha[:8]}', 'url': f'{web}/commit/{sha}'}] if SHA.fullmatch(sha) else []
    if isinstance(pull, int) and not isinstance(pull, bool) and pull > 0:
        out.append({'label': f'Pull request #{pull}', 'url': f'{web}/pull/{pull}'})
    return out + [{'label': 'Deployments', 'url': f'{web}/deployments'}]
