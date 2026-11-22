import collections
import datetime as dt
import logging
import re
from collections.abc import Callable

from skopax.codehosts import Undecided, name_of
from skopax.contract import CodeHost, Missing, Refused, SourceDown
from skopax.errkind import NoAccess, kind as error_kind, text_of
from skopax.jiralinks import listed, ticket_finder
from skopax.messages import msg
from skopax.store import Store, iso, utc_now

log = logging.getLogger('skopax.enrich')

IMAGE_SHA = re.compile(r'[0-9a-f]{7,40}')
IMAGE_COMMIT = re.compile(r'[0-9a-f]{8,40}')
MAX_NAMES = 10
ENRICH_DAYS = 7
NO_PROJECT = 'no {host} project with the service name in a subgroup of the group'
NOT_IN_HOST = 'image commits not in {host}, or not visible to the token'
NOT_A_COMMIT = 'tag is not a commit'
LOCAL_UNKNOWN = ('no local clone', 'image commits not in the clone')
NO_TICKETS = {'$or': [{'tickets': {'$exists': False}}, {'tickets': {'$size': 0}}]}


class JiraRefused(Refused):
    pass


class ProjectRefused(Exception):
    def __init__(self, project: dict, cause: NoAccess):
        super().__init__(cause.part)
        self.text = msg('enrich.project_refused', project=str(project.get('full') or project.get('path') or ''), text=text_of(cause))
        self.part = cause.part
        self.permission = cause.permission


ADF_BLOCKS = frozenset({'paragraph', 'heading', 'blockquote', 'codeBlock', 'panel', 'mediaSingle', 'mediaGroup', 'expand', 'nestedExpand', 'decisionItem'})
ADF_CARDS = ('inlineCard', 'blockCard', 'embedCard')


def adf_text(node) -> str:
    if node is None:
        return ''
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return ''.join(adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ''
    kind, attrs = node.get('type'), node.get('attrs') or {}
    if kind == 'doc':
        return re.sub(r'\n{3,}', '\n\n', adf_text(node.get('content'))).strip()
    if kind == 'text':
        text = node.get('text') or ''
        href = next(((m.get('attrs') or {}).get('href') for m in node.get('marks') or [] if m.get('type') == 'link'), None)
        return f'{text} ({href})' if href and href != text else text
    if kind == 'hardBreak':
        return '\n'
    if kind in ('mention', 'emoji', 'status'):
        return str(attrs.get('text') or attrs.get('shortName') or '')
    if kind in ADF_CARDS:
        return str(attrs.get('url') or '')
    if kind in ('media', 'mediaInline'):
        return '[attachment]'
    if kind == 'rule':
        return '\n'
    if kind == 'tableRow':
        return ' | '.join(adf_text(c.get('content')).strip() for c in node.get('content') or [] if isinstance(c, dict)) + '\n'
    inner = adf_text(node.get('content'))
    if kind in ('listItem', 'taskItem'):
        return '- ' + inner.strip() + '\n'
    if kind in ADF_BLOCKS:
        return inner.rstrip('\n') + '\n'
    return inner


def range_authors(commits: list[dict]) -> tuple[list[str], int]:
    authors = [c.get('author_name') or '' for c in commits]
    count = collections.Counter(a for a in authors if a)
    return sorted(count, key=lambda a: (-count[a], authors.index(a))), sum(count.values())


def ticket_keys(commits: list[dict], projects: tuple[str, ...] = ()) -> list[str]:
    if not projects:
        return []
    return sorted(set(ticket_finder(projects).findall('\n'.join(c.get('title') or '' for c in commits))))


def deploy_commit(gl: CodeHost, project: dict, image: str | None, old: str | None) -> dict | None:
    m = IMAGE_COMMIT.search(image or '')
    if not m:
        return None
    try:
        c = gl.commit(project, m.group(0))
    except Missing:
        return None
    full, title, date, parents = c['id'], c.get('title') or '', (c.get('committed_date') or '')[:10], c.get('parent_ids') or []
    changed = None
    if IMAGE_SHA.fullmatch(old or ''):
        try:
            changed = range_authors(gl.commits(project, old, full))
        except Missing:
            changed = None
    by = dict(title=title[:100], date=date, sha=full[:8], url=gl.commit_url(project, full))
    if changed and changed[1]:
        return dict(by, names=changed[0][:MAX_NAMES], commits=changed[1], compare=gl.compare_url(project, old, full))
    if len(parents) > 1:
        try:
            names = list(dict.fromkeys(x.get('author_name') for x in gl.commits(project, parents[0], full) if x.get('author_name')))[:3]
        except Missing:
            names = []
    else:
        names = [c['author_name']] if c.get('author_name') else []
    return dict(by, names=names)


def range_tickets(gl: CodeHost, project: dict, old: str, new: str, projects: tuple[str, ...] = ()) -> list[str] | None:
    try:
        return ticket_keys(gl.commits(project, old, new), projects)
    except Missing:
        return None


def unseen_text(keys: list[str]) -> str:
    return msg('enrich.issue_unseen' if len(keys) == 1 else 'enrich.issues_unseen', keys=', '.join(keys))


def releases_of(keys: list[str], versions: dict[str, list[str]]) -> list[str]:
    return sorted({v for k in keys for v in versions.get(k, [])})


class Enricher:
    def __init__(self, store: Store, gitlab: CodeHost, jira: object, projects: tuple[str, ...] | Callable[[], tuple[str, ...]] = ()):
        self.store = store
        self.projects = projects
        self.gitlab = gitlab
        self.jira = jira
        self._gitlab_off = False

    @property
    def enabled(self) -> bool:
        return self.gitlab.enabled or self.jira.enabled

    def needs_gitlab(self, d: dict) -> bool:
        return self.gitlab.enabled and not (d.get('enriched') or {}).get('gitlab') and (not d.get('imageBy') or not d.get('tickets'))

    def needs_jira(self, d: dict) -> bool:
        return self.jira.enabled and bool(d.get('tickets')) and ('releases' not in d or bool(d.get('releasesNote')))

    def _git(self, d: dict) -> None:
        project = self.gitlab.repo(d['svc'])
        path = str((project or {}).get('path') or '')
        if project is None or '/' not in path or path.rsplit('/', 1)[-1] != d['svc']:
            self._note(d, NO_PROJECT.format(host=name_of(self.gitlab)))
        else:
            try:
                self._git_project(d, project)
            except NoAccess as e:
                if e.part == 'projects':
                    raise
                raise ProjectRefused(project, e) from None
        self.store.update_deploy(d['id'], {}, {'$set': {'enriched.gitlab': iso(utc_now())}, '$unset': {'enriched.refused': ''}})

    def _git_project(self, d: dict, project: dict) -> None:
        if not d.get('imageBy'):
            by = deploy_commit(self.gitlab, project, d.get('toImage'), d.get('fromImage'))
            if by:
                self.store.update_deploy(d['id'], {'imageBy': {'$exists': False}}, {'$set': {'imageBy': by}})
        if not d.get('tickets'):
            if not all(IMAGE_SHA.fullmatch(d.get(k) or '') for k in ('fromImage', 'toImage')):
                self._note(d, NOT_A_COMMIT)
            else:
                keys = range_tickets(self.gitlab, project, d['fromImage'], d['toImage'], listed(self.projects))
                if keys is None:
                    self._note(d, NOT_IN_HOST.format(host=name_of(self.gitlab)))
                elif keys:
                    self.store.update_deploy(d['id'], NO_TICKETS, {'$set': {'tickets': keys}, '$unset': {'ticketsNote': ''}})
                else:
                    self.store.update_deploy(d['id'], {**NO_TICKETS, 'ticketsNote': {'$in': list(LOCAL_UNKNOWN)}}, {'$unset': {'ticketsNote': ''}})

    def _note(self, d: dict, note: str) -> None:
        if d.get('tickets'):
            return
        guard = {'$and': [NO_TICKETS, {'$or': [{'ticketsNote': {'$exists': False}}, {'ticketsNote': {'$in': list(LOCAL_UNKNOWN)}}]}]}
        self.store.update_deploy(d['id'], guard, {'$set': {'ticketsNote': note}})

    def _note_off(self) -> None:
        if self.gitlab.enabled:
            self._gitlab_off = False
        elif not self._gitlab_off:
            self._gitlab_off = True
            log.info('deploy enrichment through %s is skipped while %s is off', name_of(self.gitlab), name_of(self.gitlab))

    def _refused(self, docs: list[dict], e: NoAccess) -> None:
        log.warning('deploy enrichment through %s refused for %s: the token lacks %s', name_of(self.gitlab), e.part, e.permission)
        for d in docs:
            if self.needs_gitlab(d):
                self.store.update_deploy(d['id'], {}, {'$set': {'enriched.refused': text_of(e)}})

    def run(self, docs: list[dict]) -> dict:
        done = dict(gitlab=0, jira=0, errors=[])
        self._note_off()
        for i, d in enumerate(docs):
            if not self.needs_gitlab(d):
                continue
            try:
                self._git(d)
                done['gitlab'] += 1
            except Undecided as e:
                done['errors'].append(e.text)
                log.info('deploy enrichment left one deploy for a later sweep: %s', e.text)
            except ProjectRefused as e:
                done['errors'].append(e.text)
                log.warning('deploy enrichment through %s refused for %s of one project: the token lacks %s', name_of(self.gitlab), e.part, e.permission)
                self.store.update_deploy(d['id'], {}, {'$set': {'enriched.refused': e.text}})
            except NoAccess as e:
                done['errors'].append(text_of(e))
                self._refused(docs[i:], e)
                break
            except SourceDown as e:
                done['errors'].append(e.text)
                log.warning('deploy enrichment through %s stopped: %s', name_of(self.gitlab), error_kind(e))
                break
        wanted = [d for d in self.store.deploys_by_keys([(d['ns'], d['svc'], d['toImage']) for d in docs]) if self.needs_jira(d)]
        keys = sorted({k for d in wanted for k in d['tickets']})
        if keys:
            try:
                versions = self.jira.versions(keys)
            except NoAccess as e:
                done['errors'].append(text_of(e))
                log.warning('deploy release lookup through Jira refused for %s: the token lacks %s', e.part, e.permission)
                for d in wanted:
                    self.store.update_deploy(d['id'], {}, {'$set': {'releasesNote': text_of(e)}})
                return done
            except SourceDown as e:
                done['errors'].append(e.text)
                log.warning('deploy release lookup through Jira failed: %s', error_kind(e))
                return done
            for d in wanted:
                hidden = [k for k in d['tickets'] if k not in versions]
                change = {'$set': {'releases': releases_of(d['tickets'], versions)} | ({'releasesNote': unseen_text(hidden)} if hidden else {})} | ({} if hidden else {'$unset': {'releasesNote': ''}})
                if self.store.update_deploy(d['id'], {'$or': [{'releases': {'$exists': False}}, {'releasesNote': {'$exists': True}}]}, change):
                    done['jira'] += 1
        return done

    def run_keys(self, keys: list[tuple[str, str, str]]) -> dict:
        return self.run(self.store.deploys_by_keys(keys))

    def sweep(self, now: dt.datetime | None = None) -> dict:
        since = iso((now or utc_now()) - dt.timedelta(days=ENRICH_DAYS))
        return self.run(self.store.deploys_since(since))
