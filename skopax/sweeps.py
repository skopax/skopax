import copy
import dataclasses
import logging
import math
import threading
import time
from collections.abc import Callable, Iterable

import bson

from skopax import engine, sections, summary, sweepout
from skopax.actors import SYSTEM
from skopax.contract import KEY_SEGMENT, Check, InstanceInfo, PassResult, Plugin, ReadError, Sweep
from skopax.engine import ALREADY_ACTIVE, APPLY_ATTEMPTS, CONNECTED, LONG_AGO, SEV_RANK, SHOTS, Applied, clearing_key, iso, no_data, parse_iso, read_any, started_before
from skopax.errkind import kind
from skopax.instances import RECONNECT_AFTER_S, REFRESH_S, Instance, node_of
from skopax.messages import msg
from skopax.monitor import refusal
from skopax.redact import redact_text
from skopax.registry import CORE_RAN
from skopax.runtime import Runtime
from skopax.scheduler import Job
from skopax.store import Conflict, Store

log = logging.getLogger('skopax.sweeps')

CHECK_TIMEOUT_S = 240
ERROR_LIMIT = 120
RETIRED_KEEP_S = 3600
WINDOW_S = 2 * REFRESH_S
CARD_TEXT = ('key', 'src', 'svc', 'label', 'short')
ROW_TEXT = ('ns', 'svc', 'kind')
DEPLOY_TEXT = ('ns', 'svc', 'toImage', 'at')
JSON_DEPTH = 32
INT64 = 2 ** 63


def clean(text: object) -> str:
    return redact_text(text, ERROR_LIMIT)


def failed(e: BaseException) -> str:
    if isinstance(e, ReadError):
        return f'error: {clean(e.message)}'
    said = refusal(e)
    return f'error: {kind(e)}' if said is None else f'error: {said}'


def tombs(state: dict) -> list[dict]:
    out = []
    for t in state.get('retired') or []:
        if (isinstance(t, dict) and isinstance(t.get('plugin'), str) and (t.get('name') is None or isinstance(t.get('name'), str)) and (t.get('check') is None or isinstance(t.get('check'), str))
                and isinstance(t.get('at'), (int, float)) and not isinstance(t.get('at'), bool)):
            out.append(t)
    return out


def _primary(inst: InstanceInfo | None) -> bool:
    return inst is None or inst.primary


def _segment(inst: InstanceInfo | None) -> str | None:
    return None if _primary(inst) else inst.name


def owned(check: Check, inst: InstanceInfo | Instance | None) -> bool:
    return not check.once or _primary(inst)


def watched(plugin: Plugin, inst: Instance | None) -> list[Check]:
    return [c for c in plugin.checks if inst is None or (c.id not in inst.off_checks and owned(c, inst) and not node_of(inst))]


def pass_name(check: Check, inst: InstanceInfo | None) -> str:
    return check.id if _primary(inst) else f'{check.id}-{inst.name}'


def _in_segment(check: Check, name: str, card: dict) -> dict:
    short = card.get('short')
    if short:
        head, _, rest = short.partition(' ')
        short = f'{head} {name.upper()} {rest}'.rstrip()
    return dict(card, key=f"{card['key']}{KEY_SEGMENT}{name}", cluster=name, ranKey=f"{card.get('ranKey') or card.get('src') or check.id}-{name}", short=short)


def scope(sweep: Sweep, check: Check, inst: InstanceInfo | None) -> Sweep:
    result = sweep.result
    ran = tuple(result.ran or check.ran)
    if _primary(inst):
        return dataclasses.replace(sweep, result=dataclasses.replace(result, name=check.id, ran=ran))
    read = tuple(k for k in ran if not no_data(k))
    keys = tuple(f'{k}-{inst.name}' for k in read) + tuple(k for k in ran if no_data(k)) + ((inst.name,) if check.legacy_alias and any(k in check.ran for k in read) else ())
    found = [_in_segment(check, inst.name, card) for card in result.found]
    deploys = [dict(d, cluster=inst.name) for d in sweep.deploys]
    return dataclasses.replace(sweep, result=dataclasses.replace(result, name=pass_name(check, inst), found=found, ran=keys), deploys=deploys)


def connection_key(plugin_id: str, check_id: str, name: str) -> str:
    return f'{plugin_id}/{check_id}/{name}'


def stamp_of(text: object) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return parse_iso(text)
    except (TypeError, ValueError, OverflowError):
        return None


def went_off(state: dict, key: str, ran: bool, now: float, forget: bool) -> None:
    table = state.setdefault(CONNECTED, {})
    if forget:
        table.pop(key, None)
        return
    entry = table.get(key)
    if isinstance(entry, dict):
        return
    at = entry if isinstance(entry, str) else LONG_AGO if key not in table and ran else None
    table[key] = {'at': at, 'off': iso(now)}


def quiet_start(sweep: Sweep, plugin: Plugin, connected: float) -> Sweep:
    found = []
    for card in sweep.result.found:
        since = started_before(card, connected)
        found.append(card if since is None else dict(card, **{ALREADY_ACTIVE: {'since': since, 'source': plugin.name}}))
    return dataclasses.replace(sweep, result=dataclasses.replace(sweep.result, found=found))


def allowed(key: str, check: Check) -> bool:
    return key in check.ran or (':' in key and key.split(':', 1)[0] in check.ran)


def _owns(check: Check, inst: InstanceInfo | None, key: object) -> bool:
    if not isinstance(key, str):
        return False
    if _primary(inst):
        return allowed(key, check)
    if check.legacy_alias and key == inst.name:
        return True
    tail = f'-{inst.name}'
    return key.endswith(tail) and allowed(key[:-len(tail)], check)


def _as_raised(card: dict, segment: str | None) -> dict:
    tail = f'{KEY_SEGMENT}{segment}'
    key = card.get('key')
    if segment is None or not isinstance(key, str) or not key.endswith(tail):
        return card
    return dict(card, key=key[:-len(tail)])


def _at_or_after(row: dict, since: float) -> bool:
    try:
        return parse_iso(row['at']) >= since
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


class Reads:
    def __init__(self, store: Store, plugin: Plugin, check: Check | None, releases: Callable[[dict, float], list[dict]] | None = None):
        self.store = store
        self.plugin = plugin
        self.check = check
        self.releases = releases
        self._guard = threading.Lock()
        self._state: dict | None = None
        self._deploys: dict[str, list[dict]] = {}

    def _loaded(self) -> dict:
        with self._guard:
            if self._state is None:
                self._state = self.store.load()
            return self._state

    def previous(self, inst: InstanceInfo | None, section: str) -> list | None:
        if not any(section in c.sections for c in self.plugin.checks):
            return None
        rows = self._loaded().get(section)
        if not isinstance(rows, list):
            return None
        segment = _segment(inst)
        return copy.deepcopy([r for r in rows if isinstance(r, dict) and sections.plugin_of(r) == self.plugin.id and (r.get('cluster') or None) == segment])

    def last_status(self, inst: InstanceInfo | None) -> str | None:
        if self.check is None:
            return None
        runs = self._loaded().get('serverPasses')
        found = runs.get(pass_name(self.check, inst)) if isinstance(runs, dict) else None
        status = found.get('status') if isinstance(found, dict) else None
        return status if isinstance(status, str) else None

    def connection(self, name: str, inst: InstanceInfo) -> tuple[bool, object, bool]:
        table = self._loaded().get(CONNECTED)
        key = connection_key(self.plugin.id, self.check.id, name)
        if isinstance(table, dict) and key in table:
            return True, table[key], False
        runs = self._loaded().get('serverPasses')
        return False, None, isinstance(runs, dict) and pass_name(self.check, inst) in runs

    def deploys(self, since: float) -> list[dict]:
        if not _number(since):
            return []
        at = iso(since)
        with self._guard:
            if at not in self._deploys:
                self._deploys[at] = self.store.deploys_since(at)
            stored = self._deploys[at]
        rows = [d for d in self._loaded().get('deploys') or [] if isinstance(d, dict) and _at_or_after(d, since)]
        released = self.releases(self._loaded().get, since) if self.releases is not None else []
        return copy.deepcopy(list(stored) + rows + released)

    def _open(self, inst: InstanceInfo | None, checks: tuple[Check, ...]) -> list[dict]:
        segment = _segment(inst)
        return [a for a in self._loaded().get('anomalies') or []
                if isinstance(a, dict) and a.get('status') == 'open' and (a.get('cluster') or None) == segment and any(_owns(c, inst, clearing_key(a)) for c in checks)]

    def open_cards(self, inst: InstanceInfo | None, plugin_wide: bool = False) -> list[dict]:
        checks = self.plugin.checks if plugin_wide else (() if self.check is None else (self.check,))
        if not checks:
            return []
        segment = _segment(inst)
        return [_as_raised(a, segment) for a in copy.deepcopy(self._open(inst, checks))]

    def keymap(self, inst: InstanceInfo | None) -> dict[str, str]:
        segment = _segment(inst)
        return {_as_raised(a, segment)['key']: a['key'] for a in self._open(inst, self.plugin.checks) if isinstance(a.get('key'), str)}


def retires(check: Check, inst: InstanceInfo | None, key: object) -> bool:
    if not isinstance(key, str) or not owned(check, inst):
        return False
    return _owns(check, inst, key) or (check.legacy_alias and not _primary(inst) and allowed(key, check))


def retired(state: dict, check: Check, inst: InstanceInfo | None) -> set[str]:
    keys = {clearing_key(a) for a in state.get('anomalies') or [] if isinstance(clearing_key(a), str)}
    return {k for k in keys if retires(check, inst, k)}


def _pass_owner(checks: tuple[Check, ...], key: str) -> Check | None:
    return max((c for c in checks if key == c.id or key.startswith(f'{c.id}-')), key=lambda c: len(c.id), default=None)


def _text(item: object, names: tuple[str, ...]) -> bool:
    return isinstance(item, dict) and all(isinstance(item.get(n), str) and item.get(n) != '' for n in names)


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _json(value: object, depth: int = 0) -> bool:
    if depth > JSON_DEPTH:
        return False
    if value is None or isinstance(value, (bool, str)):
        return True
    if isinstance(value, int):
        return -INT64 <= value < INT64
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _json(v, depth + 1) for k, v in value.items())
    return False


def _storable(item: object) -> bool:
    try:
        if not _json(item):
            return False
        bson.encode(item)
    except Exception:
        return False
    return True


def _row_ok(section: str, row: object) -> bool:
    if section == 'services':
        return _text(row, ROW_TEXT) and all(_number(row[k]) for k in ('req', 'gw-req', 'ing-req') if k in row) and isinstance(row.get('image', ''), str)
    if section == 'deploys':
        return _text(row, ('ns', 'svc', 'at'))
    return isinstance(row, dict)


class _Call:
    def __init__(self, fn: Callable[[], Sweep], name: str):
        self.done = threading.Event()
        self.value: object = None
        self.error: BaseException | None = None
        self._fn = fn
        threading.Thread(target=self._go, name=name, daemon=True).start()

    def _go(self) -> None:
        try:
            self.value = self._fn()
        except BaseException as e:
            self.error = e
        finally:
            self.done.set()


class Sweeper:
    def __init__(self, runtime: Runtime, store: Store, apply: Callable[..., Applied | None], timeout: float = CHECK_TIMEOUT_S, clock: Callable[[], float] = time.time,
                 enrich: Callable[[list[tuple[str, str, str]]], None] | None = None, outputs: sweepout.Outputs | None = None):
        self.runtime = runtime
        self.store = store
        self.apply = apply
        self.timeout = timeout
        self.clock = clock
        self.enrich = enrich
        self.outputs = outputs
        self._warned: set[tuple[str, str]] = set()
        self._busy: dict[tuple[str, str, str], _Call] = {}
        self._stale: set[str] = set()
        self._guard = threading.Lock()

    def jobs(self) -> list[Job]:
        return [Job(f'{plugin.id}.{check.id}', check.period, check.offset, lambda now, p=plugin.id, c=check.id: self.run(p, c, now), background=check.background)
                for plugin, _ in self.runtime.serving() for check in plugin.checks]

    def run(self, plugin_id: str, check_id: str, now: float | None = None) -> Applied | None:
        now = self.clock() if now is None else now
        started = self.clock()
        plugin, targets = self._targets(plugin_id, check_id)
        check = next((c for c in plugin.checks if c.id == check_id), None) if plugin else None
        if check is None or not targets:
            return None
        age = self.runtime.instances.live_age()
        if plugin.fields and age is not None and age > WINDOW_S:
            if plugin.id not in self._stale:
                self._stale.add(plugin.id)
                log.warning('%s', msg('sweeps.stale_instances', plugin=plugin.id, seconds=int(age)))
            return None
        self._stale.discard(plugin.id)
        seen: set[str] = set()
        results, taken, owner, keymaps = [], [], {}, []
        reads = Reads(self.store, plugin, check, self.runtime.releases)
        connects: dict[str, tuple[str | None, bool]] = {}
        for inst, sweep in self._execute(plugin, check, targets, now, reads):
            info = None if inst is None else inst.info()
            scoped = scope(sweep, check, info)
            connected = None if inst is None else self._connected(plugin, check, inst, info, scoped.result, reads, now, connects)
            if scoped.result.ok and connected is not None:
                scoped = quiet_start(scoped, plugin, connected)
            if scoped.result.ok:
                keymaps.append(reads.keymap(info) | {a['key']: b['key'] for a, b in zip(sweep.result.found, scoped.result.found)})
                scoped = self._without_repeats(plugin, scoped, seen)
                taken.append((info, scoped))
            owner[scoped.result.name] = info
            results.append(dataclasses.replace(scoped.result, sections={}))
        known: dict[tuple, str | None] = {}
        added: list[tuple] = []
        outputs = self.outputs
        pictures = [outputs.shots(plugin, check, info, scoped, keymap) if outputs is not None else [] for (info, scoped), keymap in zip(taken, keymaps)]
        live: list[int] = []

        def screen(state: dict, all_results: list[PassResult]) -> list[PassResult]:
            return [r for r in all_results if self._live(state, plugin.id, owner[r.name], started, check.id)]

        def owner_of(info: InstanceInfo | None) -> Callable[[dict], bool]:
            return lambda card: (card.get('cluster') or None) == _segment(info) and any(_owns(c, info, clearing_key(card)) for c in plugin.checks)

        def after(state: dict) -> None:
            docs = [d for info, s in taken if self._live(state, plugin.id, info, started, check.id) for d in s.deploys if (d['ns'], d['svc'], d['toImage']) not in known]
            keys = [(d['ns'], d['svc'], d['toImage']) for d in docs]
            if docs:
                self.store.add_deploys(docs)
                added.extend(keys)
                known.update(dict.fromkeys(keys))
                known.update({(d['ns'], d['svc'], d['toImage']): d.get('imageBy') for d in self.store.deploys_by_keys(keys)})
            live.clear()
            for i, (info, scoped) in enumerate(taken):
                if self._live(state, plugin.id, info, started, check.id):
                    live.append(i)
                    for section, rows in scoped.sections.items():
                        sections.merge_rows(state, section, rows, plugin.id, _segment(info))
            if outputs is not None:
                outputs.attach(state, [(taken[i][1], pictures[i]) for i in live])
            for i in live:
                if taken[i][1].cleared:
                    sweepout.mark_cleared(state, plugin, taken[i][1].cleared, keymaps[i], now, owner_of(taken[i][0]))
            if connects:
                table = dict(state.get(CONNECTED) or {})
                for key, (at, force) in connects.items():
                    if force or not table.get(key):
                        table[key] = at
                state[CONNECTED] = table
            self._heal(state, plugin)
            state['retired'] = [t for t in tombs(state) if t['at'] >= started - RETIRED_KEEP_S]
            sections.finish(state, lambda ns, svc, image: known.get((ns, svc, image)))

        clusters = {name: (None, info.name) if info.primary else (info.name,) for name, info in owner.items() if info is not None}
        applied = self.apply(results, now, self.runtime.linker(), after, check=check.id, screen=screen, clusters=clusters)
        for i in list(live) if outputs is not None else []:
            info, scoped = taken[i]
            owns = owner_of(info)
            try:
                outputs.after(plugin, check, info, scoped, keymaps[i], pictures[i], now, owns)
            except Exception as e:
                log.warning('the side outputs of pass %s were not finished: %s', check.id, kind(e))
        if added and self.enrich is not None:
            try:
                self.enrich(added)
            except Exception as e:
                log.warning('deploy enrichment after a sweep failed (%s)', kind(e))
        return applied

    def _connected(self, plugin: Plugin, check: Check, inst: Instance, info: InstanceInfo, result: PassResult, reads: Reads, now: float,
                   connects: dict[str, str | None]) -> float | None:
        own = stamp_of(getattr(inst, 'connected', None))
        kept = self._kept(plugin, check, inst, info, result, reads, now, connects)
        found = [at for at in (own, kept) if at is not None]
        return min(found) if found else None

    def _kept(self, plugin: Plugin, check: Check, inst: Instance, info: InstanceInfo, result: PassResult, reads: Reads, now: float,
              connects: dict[str, tuple[str | None, bool]]) -> float | None:
        key = connection_key(plugin.id, check.id, inst.name)
        known, stored, legacy = reads.connection(inst.name, info)
        if isinstance(stored, dict):
            off = stamp_of(stored.get('off'))
            kept = stored.get('at') if isinstance(stored.get('at'), str) else None
            if off is not None and now - off <= RECONNECT_AFTER_S and kept is not None:
                connects[key] = (kept, True)
                return stamp_of(kept)
            if read_any(result):
                connects[key] = (iso(now), True)
                return now
            return None
        if isinstance(stored, str):
            return stamp_of(stored)
        if legacy:
            connects[key] = (LONG_AGO, False)
            return 0.0
        if read_any(result):
            connects[key] = (iso(now), False)
            return now
        if not known:
            connects[key] = (None, False)
        return None

    def forget(self, plugin_id: str, name: str) -> None:
        plugin = self._plugin(plugin_id)
        if plugin is None:
            return
        for _ in range(APPLY_ATTEMPTS):
            state = self.store.load()
            before = copy.deepcopy(state)
            for check in plugin.checks:
                went_off(state, connection_key(plugin_id, check.id, name), False, 0.0, True)
            if state == before:
                return
            try:
                self.store.save_state(state, before.get('rev', 0), before)
            except Conflict:
                continue
            return
        raise Conflict(self.store.rev()['rev'])

    def retire(self, plugin_id: str, target: 'InstanceInfo | str | None', now: float, check_id: str | None = None) -> int:
        plugin = self._plugin(plugin_id)
        if plugin is None or not plugin.checks:
            return 0
        if target is None:
            if check_id is not None:
                return 0
            infos = [i.info() for i in self.runtime.instances.all(plugin_id)] or self._seen_targets(plugin)
            note = msg('cards.retired_plugin', plugin=plugin.name)
            return self._close(plugin, [(i, note) for i in infos], [None], now, f'{plugin.name}')
        if isinstance(target, str):
            stored = self.runtime.instances.get(plugin_id, target)
            target = stored.info() if stored is not None else InstanceInfo(target, False, target, {})
        if check_id is None:
            note = msg('cards.retired', plugin=plugin.name, instance=target.name)
            return self._close(plugin, [(target, note)], [target.name], now, f'{plugin.name} {target.name}')
        check = next((c for c in plugin.checks if c.id == check_id), None)
        if check is None or not owned(check, target):
            return 0
        kept = set().union(*(c.sections for c in watched(plugin, self.runtime.instances.get(plugin_id, target.name)) if c.id != check.id))
        note = msg('cards.retired_check', plugin=plugin.name, instance=target.name, check=check.label)
        return self._close(plugin, [(target, note)], [target.name], now, f'{plugin.name} {target.name}', check, set(check.sections) - kept)

    def _seen_targets(self, plugin: Plugin) -> list:
        if not plugin.fields:
            return [None]
        state = self.store.load()
        names = {a.get('cluster') for a in state.get('anomalies') or [] if isinstance(a.get('cluster'), str) and a.get('cluster')}
        owners = {k: _pass_owner(plugin.checks, k) for t in ('sources', 'serverPasses') for k in state.get(t) or {}}
        names |= {k[len(o.id) + 1:] for k, o in owners.items() if o is not None and k != o.id and not o.once}
        return [InstanceInfo('-', True, None, {})] + [InstanceInfo(n, False, n, {}) for n in sorted(names)]

    def primary_changed(self, plugin_id: str, old: InstanceInfo, new: InstanceInfo, now: float) -> int:
        plugin = self._plugin(plugin_id)
        if plugin is None or not plugin.checks:
            return 0
        promoted = InstanceInfo(new.name, False, new.name, new.config)
        items = [(old, msg('cards.rescoped', plugin=plugin.name, instance=old.name)), (promoted, msg('cards.rescoped', plugin=plugin.name, instance=new.name))]
        moved = tuple(c for c in plugin.checks if not c.once)
        kept = set().union(*(c.sections for c in plugin.checks if c.once))
        return self._close(plugin, items, [None], now, f'{plugin.name} {old.name} {new.name}', touched=set().union(*(c.sections for c in moved)) - kept, among=moved, keep_connected=True)

    def revive(self, plugin_id: str, name: str | None, check_id: str | None = None, checks: Iterable[str] = ()) -> None:
        scopes = {check_id, *checks}
        for _ in range(APPLY_ATTEMPTS):
            state = self.store.load()
            before = copy.deepcopy(state)
            kept = [t for t in tombs(state) if not (t['plugin'] == plugin_id and (name is None or t.get('name') == name) and t.get('check') in scopes)]
            if len(kept) == len(state.get('retired') or []):
                return
            state['retired'] = kept
            try:
                self.store.save_state(state, before.get('rev', 0), before)
            except Conflict:
                continue
            return
        raise Conflict(self.store.rev()['rev'])

    def _plugin(self, plugin_id: str) -> Plugin | None:
        found = self.runtime.registry.get(plugin_id)
        return found.plugin if found is not None else None

    def _close(self, plugin: Plugin, items: list[tuple[InstanceInfo | None, str]], names: list[str | None], now: float, what: str, only: Check | None = None,
               touched: set[str] | None = None, among: tuple[Check, ...] | None = None, keep_connected: bool = False, forget: bool = False) -> int:
        at = self.clock()
        checks = (only,) if only is not None else plugin.checks if among is None else among
        touched = set().union(*(c.sections for c in plugin.checks)) if touched is None else touched
        mark = {} if only is None else {'check': only.id}
        for _ in range(APPLY_ATTEMPTS):
            state = self.store.load()
            before = copy.deepcopy(state)
            closed = []
            for info, note in items:
                for check in checks:
                    closed += engine.retire(state, retired(state, check, info), note, now, _segment(info))
                for section in touched:
                    sections.drop_rows(state, section, plugin.id, _segment(info))
                for check in checks:
                    if not owned(check, info):
                        continue
                    ran = pass_name(check, info) in (state.get('serverPasses') or {})
                    for table in ('sources', 'serverPasses'):
                        (state.get(table) or {}).pop(pass_name(check, info), None)
                    if not keep_connected and info is not None:
                        went_off(state, connection_key(plugin.id, check.id, info.name), ran, now, forget)
            state['retired'] = [t for t in tombs(state) if t['at'] >= at - RETIRED_KEEP_S] + [{'plugin': plugin.id, 'name': name, **mark, 'at': at} for name in names]
            if state.get('services'):
                sections.finish(state)
            state['summary'] = summary.build(state, self.store, now)
            try:
                self.store.save_state(state, before.get('rev', 0), before)
            except Conflict:
                continue
            if closed:
                line = msg('sweeps.retired', what=what, count=len(closed)) if only is None else msg('sweeps.retired_check', what=what, check=only.label, count=len(closed))
                self.store.add_activity('auto', SYSTEM, line)
            return len(closed)
        raise Conflict(self.store.rev()['rev'])

    def heal(self) -> None:
        age = self.runtime.instances.live_age()
        if age is None or age > WINDOW_S:
            return
        plugins = [p for p in self.runtime.registry.usable() if p.checks and p.fields and self.runtime.instances.all(p.id)]
        for _ in range(APPLY_ATTEMPTS):
            state = self.store.load()
            before = copy.deepcopy(state)
            for plugin in plugins:
                self._heal(state, plugin)
            if state == before:
                break
            if state.get('services'):
                sections.finish(state)
            state['summary'] = summary.build(state, self.store, self.clock())
            try:
                self.store.save_state(state, before.get('rev', 0), before)
            except Conflict:
                continue
            break
        for plugin, info, check in self._unchecked_leftovers(state):
            self.retire(plugin.id, info, self.clock(), check.id)

    def _unchecked_leftovers(self, state: dict) -> list[tuple[Plugin, InstanceInfo, Check]]:
        out = []
        for plugin, found in self.runtime.serving():
            for inst in found:
                info = inst.info()
                segment = _segment(info)
                kept = set().union(*(c.sections for c in watched(plugin, inst)))
                for check in plugin.checks:
                    if (check.id not in inst.off_checks and not node_of(inst)) or not owned(check, info):
                        continue
                    passes = any(pass_name(check, info) in (state.get(t) or {}) for t in ('sources', 'serverPasses'))
                    cards = any(a.get('status') == 'open' and retires(check, info, clearing_key(a)) and (a.get('cluster') or None) == segment for a in state.get('anomalies') or [])
                    rows = any(sections.plugin_of(r) == plugin.id and (r.get('cluster') or None) == segment for s in set(check.sections) - kept for r in sections.stored_rows(state, s))
                    if passes or cards or rows:
                        out.append((plugin, info, check))
        return out

    def _heal(self, state: dict, plugin: Plugin) -> None:
        found = next((f for p, f in self.runtime.serving() if p.id == plugin.id), [])
        names = {i.name for i in found if not i.primary}
        has_primary = any(i.primary for i in found) or not plugin.fields
        touched = set().union(*(c.sections for c in plugin.checks))

        def alive(segment: str | None) -> bool:
            return has_primary if segment is None else segment in names

        for section in touched:
            if section in state:
                state[section] = [r for r in sections.stored_rows(state, section) if sections.plugin_of(r) != plugin.id or alive(r.get('cluster') or None)]
        for table in ('sources', 'serverPasses'):
            for key in list(state.get(table) or {}):
                if key in CORE_RAN:
                    continue
                owner = _pass_owner(plugin.checks, key)
                if owner is None:
                    continue
                if not (has_primary if key == owner.id else not owner.once and key[len(owner.id) + 1:] in names):
                    del state[table][key]

    def _live(self, state: dict, plugin_id: str, info: InstanceInfo | None, started: float, check_id: str) -> bool:
        for plugin, found in self.runtime.serving():
            if plugin.id == plugin_id:
                break
        else:
            return False
        name = None if info is None else info.name
        if name is not None and not any(i.name == name and check_id not in i.off_checks for i in found):
            return False
        return not any(t['plugin'] == plugin_id and t.get('name') in (None, name) and t.get('check') in (None, check_id) and started < t['at'] + WINDOW_S for t in tombs(state))

    def _targets(self, plugin_id: str, check_id: str) -> tuple[Plugin | None, list]:
        for plugin, found in self.runtime.serving():
            if plugin.id == plugin_id:
                if not found:
                    return plugin, [] if plugin.fields else [None]
                return plugin, sorted((i for i in found if any(c.id == check_id for c in watched(plugin, i))), key=lambda i: (not i.primary, i.name))
        return None, []

    def _execute(self, plugin: Plugin, check: Check, targets: list, now: float, reads: Reads | None = None) -> list[tuple]:
        calls = []
        for inst in targets:
            key = (plugin.id, check.id, '' if inst is None else inst.name)
            with self._guard:
                busy = self._busy.get(key)
                if busy is not None and not busy.done.is_set():
                    calls.append((inst, None))
                    continue
                call = _Call(lambda inst=inst: check.run(self.runtime.context(plugin, inst, now, reads)), f'sweep-{plugin.id}-{key[2] or "plugin"}')
                self._busy[key] = call
            calls.append((inst, call))
        limit = self.timeout if check.timeout is None else check.timeout
        deadline = time.monotonic() + limit
        out = []
        for inst, call in calls:
            if call is None:
                sweep = self._refused(check, msg('sweeps.still_running'))
            elif not call.done.wait(max(0.0, deadline - time.monotonic())):
                sweep = self._refused(check, msg('sweeps.timed_out', seconds=limit))
            elif call.error is not None:
                sweep = self._refused(check, failed(call.error)) if isinstance(call.error, Exception) else self._refused(check, msg('sweeps.no_result'))
            else:
                sweep = self._checked(check, call.value)
            out.append((inst, sweep))
        return out

    def _refused(self, check: Check, status: str) -> Sweep:
        return Sweep(PassResult(check.id, False, status))

    def _bad(self, check: Check, what: str) -> Sweep:
        return self._refused(check, msg('sweeps.bad_result', what=what))

    def _checked(self, check: Check, sweep: object) -> Sweep:
        try:
            return self._validated(check, sweep)
        except Exception:
            return self._bad(check, 'result')

    def _validated(self, check: Check, sweep: object) -> Sweep:
        if not isinstance(sweep, Sweep) or not isinstance(sweep.result, PassResult):
            return self._refused(check, msg('sweeps.no_result'))
        result = sweep.result
        if not result.ok:
            return Sweep(PassResult(check.id, False, clean(result.status)))
        if not isinstance(result.ran, (tuple, list)) or not all(isinstance(k, str) for k in result.ran):
            return self._bad(check, 'ran')
        outside = {k for k in result.ran if not allowed(k, check)}
        if outside:
            return self._refused(check, msg('sweeps.ran_outside', keys=', '.join(sorted(outside))))
        if result.sections:
            return self._refused(check, msg('sweeps.pass_sections'))
        if not isinstance(result.found, list) or not isinstance(result.status, str):
            return self._bad(check, 'card')
        found = [{k: v for k, v in card.items() if k not in (SHOTS, ALREADY_ACTIVE) and not (check.once and k == 'cluster')} if isinstance(card, dict) else card for card in result.found]
        for card in found:
            if not _text(card, CARD_TEXT) or not _storable(card) or not isinstance(card['sev'], str) or card['sev'] not in SEV_RANK or not _number(card.get('now')) or not isinstance(card.get('ranKey', ''), str):
                return self._bad(check, 'card')
            key = clearing_key(card)
            if not isinstance(key, str) or not allowed(key, check):
                return self._refused(check, msg('sweeps.card_not_ran'))
        if not isinstance(sweep.sections, dict):
            return self._bad(check, 'section')
        for name, rows in sweep.sections.items():
            if name not in check.sections:
                return self._refused(check, msg('sweeps.undeclared_section', name=name if isinstance(name, str) else '?'))
            if not isinstance(rows, list):
                return self._bad(check, 'section')
            if not all(_row_ok(name, r) and _storable(r) for r in rows):
                return self._bad(check, 'row')
        if not isinstance(sweep.deploys, list) or not all(_text(d, DEPLOY_TEXT) and _storable(d) for d in sweep.deploys):
            return self._bad(check, 'deploy')
        try:
            shots, posts, memos, digest, cleared, dropped = sweepout.outputs_of(sweep, _storable)
        except sweepout.Malformed as e:
            return self._bad(check, e.what)
        if dropped:
            log.warning('%s', msg('sweeps.outputs_dropped', check=check.id, what=', '.join(dropped)))
        return dataclasses.replace(sweep, result=dataclasses.replace(result, found=found), shots=shots, posts=posts, memos=memos, digest=digest, cleared=cleared)

    def _without_repeats(self, plugin: Plugin, scoped: Sweep, seen: set[str]) -> Sweep:
        result = scoped.result
        kept = [card for card in result.found if card['key'] not in seen]
        dropped = [card['key'] for card in result.found if card['key'] in seen]
        seen.update(card['key'] for card in result.found)
        if not dropped:
            return scoped
        for key in dropped:
            with self._guard:
                first = (plugin.id, key) not in self._warned
                self._warned.add((plugin.id, key))
            if first:
                log.warning('%s: the card %s was reported by more than one instance, the earlier one is kept', plugin.id, key)
        status = f"{result.status} \u00b7 {msg('sweeps.duplicates', count=len(dropped), first=dropped[0])}"
        return dataclasses.replace(scoped, result=dataclasses.replace(result, found=kept, status=status))
