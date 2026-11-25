import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from skopax import tags
from skopax.contract import QUESTIONS_FIELD
from skopax.errkind import Authored, kind as error_kind
from skopax.messages import msg
from skopax.probe import PROBE_PLUGIN
from skopax.redact import redact_text

log = logging.getLogger('skopax.investigator.toolset')

CORE_OWNER = 'core'
TAG = 'tag'
KNOWLEDGE = 'plugin_knowledge'
CONFIRM_TIMEOUT_S = 30
SUMMARY_LIMIT = 300
CARD_WRITES = ('assess', 'note', 'triage', 'group', 'tag')
SLACK_WRITES = ('slack_draft', 'slack_send', 'slack_update')
REPORT_WRITES = ('report_brief', 'report_slack_draft')
REVIEW_VERDICTS = {'verify': 'follow_up_verdict', 'settle': 'settle_verdict'}
ASKED_ROLES = ('operator', 'admin')


@dataclass(frozen=True)
class Spec:
    name: str
    kind: str
    owner: str
    needs: str | None = None
    per_run: int | None = None
    reach: tuple[str, ...] = ()
    scan: bool = False


@dataclass(frozen=True)
class Run:
    read_only: bool
    ask: bool
    report: bool
    chat: bool
    keys: bool
    asker: bool
    shadow: bool
    role: str | None = None
    review: str | None = None


def _core(name: str, kind: str, per_run: int | None = None, scan: bool = False, needs: str | None = None) -> Spec:
    return Spec(name, kind, CORE_OWNER, needs=needs, per_run=per_run, scan=scan)


CORE: dict[str, Spec] = {s.name: s for s in (
    _core('jira_search', 'read', scan=True, needs='jira'),
    _core('jira_issue', 'read', scan=True, needs='jira'),
    _core('card', 'read'),
    _core('open_cards', 'read'),
    _core('deploys_since', 'read'),
    _core('past_cards', 'read', per_run=10),
    _core('self_review', 'read', per_run=2),
    _core('past_runs', 'read', per_run=8),
    _core('pass_table', 'read', scan=True),
    _core(KNOWLEDGE, 'read'),
    _core('assess', 'write'),
    _core('note', 'write'),
    _core('triage', 'write'),
    _core('group', 'write'),
    _core('slack_draft', 'write'),
    _core('report_brief', 'write'),
    _core('report_slack_draft', 'write'),
    _core('slack_send', 'write'),
    _core('slack_update', 'write'),
    _core('slack_tell', 'write'),
    _core('follow_up_verdict', 'write'),
    _core('settle_verdict', 'write'),
    _core(TAG, 'write'),
    _core('propose_ticket', 'propose', needs='jira'),
    _core('propose_card_action', 'propose'),
    _core('propose_investigation', 'propose'),
)}


def primary_of(runtime, plugin, found: list) -> object | None:
    primary = runtime.instances.primary(plugin.id)
    return primary if primary is not None and any(i.name == primary.name for i in found) else None


def has_value(instance, field: str) -> bool:
    return bool(str(instance.values.get(field) or instance.secrets.get(field) or '').strip())


def _reaches(runtime, plugin, tool, found: list) -> bool:
    return bool(tool.instance_input) or not plugin.fields or primary_of(runtime, plugin, found) is not None


def asked_gate(run: Run | None) -> Callable[[object], bool] | None:
    if run is None or not run.ask or run.review:
        return None
    trusted = run.role in ASKED_ROLES

    def allowed(instance) -> bool:
        wanted = str((getattr(instance, 'values', None) or {}).get(QUESTIONS_FIELD) or '')
        return wanted != 'none' and (wanted != 'operators' or trusted)
    return allowed


def _gated(runtime, allowed: Callable[[object], bool] | None = None) -> list[tuple]:
    if runtime is None:
        return []
    out = []
    for plugin, found in runtime.serving(allowed):
        for tool in plugin.tools:
            if not _reaches(runtime, plugin, tool, found):
                continue
            usable = [i for i in found if (tool.available(runtime, i) if tool.available is not None else has_value(i, tool.needs_field))] if tool.needs_field else found
            out.append((plugin, tool, usable, not tool.needs_field or (bool(usable) and _reaches(runtime, plugin, tool, usable))))
    return out


def runnable(runtime, allowed: Callable[[object], bool] | None = None) -> list[tuple]:
    return [(plugin, tool, found) for plugin, tool, found, ready in _gated(runtime, allowed) if ready]


def held(runtime) -> list[tuple]:
    return [(plugin, tool) for plugin, tool, _, ready in _gated(runtime) if not ready]


def field_label(plugin, key: str) -> str:
    return next((f.label for f in plugin.fields if f.key == key), key)


def bounded(call: Callable[[], object], timeout: float, name: str) -> tuple[str, object]:
    box: dict = {}
    done = threading.Event()

    def go() -> None:
        try:
            box['out'] = call()
        except BaseException as e:
            box['error'] = error_kind(e)
        finally:
            done.set()

    threading.Thread(target=go, name=name, daemon=True).start()
    if not done.wait(timeout):
        return 'timeout', None
    return ('error', box['error']) if 'error' in box else ('done', box.get('out'))


class ToolSet:
    def __init__(self, runtime=None):
        self.runtime = runtime

    def serving(self, allowed: Callable[[object], bool] | None = None) -> set[str]:
        return set() if self.runtime is None else {plugin.id for plugin, _ in self.runtime.serving(allowed)}

    def held_in_question(self, name: str, run: Run) -> bool:
        spec = CORE.get(name)
        allowed = asked_gate(run)
        return allowed is not None and spec is not None and spec.needs is not None and spec.needs in self.serving() and spec.needs not in self.serving(allowed)

    def declared(self) -> dict:
        return {} if self.runtime is None else {tool.name: tool for plugin in self.runtime.registry.usable() for tool in plugin.tools}

    def specs(self) -> dict[str, Spec]:
        out = dict(CORE)
        if self.runtime is None:
            return out
        for plugin, _ in self.runtime.serving():
            for tool in plugin.tools:
                out[tool.name] = Spec(tool.name, tool.kind, plugin.id, per_run=tool.per_run, reach=tuple(tool.reach), scan=tool.scan)
        return out

    def needed(self, name: str, serving: set[str] | None = None) -> bool:
        spec = CORE.get(name)
        return spec is None or spec.needs is None or spec.needs in (self.serving() if serving is None else serving)

    def offered(self, run: Run) -> list[str]:
        names = [n for n, s in CORE.items() if s.kind == 'read']
        if run.review:
            serving = self.serving()
            return [n for n in names + [REVIEW_VERDICTS[run.review]] if self.needed(n, serving)] + [tool.name for _, tool, _ in runnable(self.runtime) if tool.kind == 'read']
        if run.read_only:
            names += ['propose_card_action'] + (['propose_investigation'] if run.chat else ['propose_ticket'] if run.keys else [])
        elif run.report:
            names += list(REPORT_WRITES)
        elif run.ask:
            names += (list(CARD_WRITES) + (['slack_update'] if run.asker else []) if run.keys else []) + (['slack_tell'] if run.asker else [])
        else:
            names += list(CARD_WRITES + SLACK_WRITES)
        serving = self.serving(asked_gate(run))
        names = [n for n in names if (n != TAG or tags.all_tags()) and self.needed(n, serving)]
        writes = not run.read_only and not run.report and (not run.ask or run.keys)
        proposes = run.read_only and not run.report and not run.chat
        for _, tool, _ in runnable(self.runtime, asked_gate(run)):
            if tool.kind == 'read' or (tool.kind == 'write' and writes) or (tool.kind == 'propose' and proposes):
                names.append(tool.name)
        return names

    def sections(self, allowed: Callable[[object], bool] | None = None) -> list[tuple[str, str, str, str]]:
        if self.runtime is None:
            return []
        return [(section, check.id, plugin.name, str(check.section_help.get(section) or '')) for plugin, _ in self.runtime.serving(allowed) for check in plugin.checks
                for section in check.sections]

    def status(self, name: str, probe: dict | None) -> dict:
        if name not in CORE and name not in self.declared():
            return {'state': 'off', 'reason': msg('toolset.unknown', name=name)}
        runs = self.needed(name) if name in CORE else name in {tool.name for _, tool, _ in runnable(self.runtime)}
        if not runs:
            gated = next((field_label(plugin, tool.needs_field) for plugin, tool in held(self.runtime) if tool.name == name), None)
            if gated is not None:
                return {'state': 'off', 'reason': msg('tools.off.needs_field', field=gated)}
            return {'state': 'off', 'reason': msg('toolset.off', name=name)}
        failed = self.cut_off(name, probe)
        if failed:
            return {'state': 'unreachable', 'reason': msg('tools.unreachable', targets=', '.join(failed)), 'at': probe.get('at')}
        return {'state': 'on', 'reason': None}

    def cut_off(self, name: str, probe: dict | None) -> list[str]:
        rows = probe.get('targets') if isinstance(probe, dict) else None
        if name in CORE:
            owner, reach = CORE[name].needs or CORE_OWNER, CORE[name].reach
        else:
            owner, reach = next(((plugin.id, tuple(tool.reach)) for plugin, _ in (self.runtime.serving() if self.runtime else []) for tool in plugin.tools if tool.name == name), (None, ()))
        named = [r for r in rows or [] if isinstance(r, dict) and r.get('plugin') != PROBE_PLUGIN and (r.get('name') in reach if reach else r.get('plugin') == owner)]
        if not named or any(r.get('ok') for r in named):
            return []
        return list(dict.fromkeys(str(r.get('name')) for r in named))


class NoProposal(Authored, LookupError):
    pass


class ProposalRefused(Authored, Exception):
    def __init__(self, code: str):
        super().__init__(msg(f'proposal.{code}'))
        self.code = code


class ProposalClosed(ProposalRefused):
    pass


class ProposalUnavailable(ProposalRefused):
    pass


class Proposals:
    def __init__(self, store, runtime, clock=time.time, timeout: float = CONFIRM_TIMEOUT_S):
        self.store = store
        self.runtime = runtime
        self.clock = clock
        self.timeout = timeout

    def confirm(self, run_id: str, by: str) -> dict:
        try:
            proposal = self.store.run_proposal(run_id)
            open_now = proposal is not None and not proposal.get('confirmedAt')
            found = self._serving(proposal) if open_now else None
            claimed = found is not None and self.store.claim_proposal(run_id, by) is not None
        except Exception as e:
            log.warning('proposal of run %s could not be checked: %s', run_id, error_kind(e))
            raise ProposalUnavailable('unavailable') from None
        if proposal is None:
            raise NoProposal(msg('proposal.none'))
        if not open_now:
            raise ProposalClosed('done')
        if found is None:
            raise ProposalClosed('gone')
        if not claimed:
            raise ProposalClosed('done')
        plugin, tool, instance = found
        result, said = self._run(plugin, tool, instance, dict(proposal.get('args') or {}))
        line = msg('proposal.confirmed', title=str(proposal.get('title') or ''), by=by) + ('' if result['ok'] else ' \u00b7 ' + redact_text(said, SUMMARY_LIMIT))
        error = None if result['ok'] else 'declined' if result['outcome'] == 'done' else result['outcome']
        for write in (lambda: self.store.proposal_result(run_id, result), lambda: self.store.add_activity('proposal', by, line, runId=run_id, ok=result['ok'], outcome=result['outcome'], error=error)):
            try:
                write()
            except Exception as e:
                log.warning('proposal of run %s: its result was not saved: %s', run_id, error_kind(e))
        return result

    def _serving(self, proposal: dict) -> tuple | None:
        for plugin, found in self.runtime.serving():
            if plugin.id != proposal.get('plugin'):
                continue
            tool = next((t for t in plugin.tools if t.name == proposal.get('tool') and t.kind == 'propose' and t.confirm is not None), None)
            if tool is None:
                return None
            if not plugin.fields:
                return plugin, tool, None
            instance = next((i for i in found if i.name == proposal.get('instance')), None)
            return None if instance is None else (plugin, tool, instance)
        return None

    def _run(self, plugin, tool, instance, args: dict) -> tuple[dict, str]:
        failed = {'ok': False, 'summary': msg('proposal.failed'), 'outcome': 'failed'}
        state, out = bounded(lambda: tool.confirm(self.runtime.context(plugin, instance, self.clock()), args), self.timeout, 'proposal-confirm')
        if state == 'timeout':
            log.warning('%s', msg('plugintools.timed_out', tool=tool.name, plugin=plugin.id, seconds=f'{self.timeout:g}'))
            return {'ok': False, 'summary': msg('proposal.timed_out'), 'outcome': 'timeout'}, msg('proposal.timed_out')
        if state == 'error':
            log.warning('%s', msg('plugintools.failed', tool=tool.name, plugin=plugin.id, error=out))
            return failed, failed['summary']
        if not isinstance(out, dict) or not isinstance(out.get('ok'), bool) or not isinstance(out.get('summary'), str):
            return failed, failed['summary']
        return {'ok': out['ok'], 'summary': ''.join(c for c in out['summary'] if c.isprintable())[:SUMMARY_LIMIT], 'outcome': 'done'}, out['summary']
