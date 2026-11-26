import copy
import difflib
import logging
import re
import threading
from collections.abc import Callable, Collection, MutableMapping, Sequence

from skopax.contract import Knowledge, Plugin, ReadError, RunState, Tool
from skopax.instances import Instance, asks_gated
from skopax.investigator.tools import ERROR_LIMIT, READ_LIMIT_ROWS, BadInput, ToolError, render, test_cluster, test_error
from skopax.investigator.toolset import Run, asked_gate, held, primary_of, runnable
from skopax.messages import msg
from skopax.monitor import refusal
from skopax.redact import masked_tree, redact_text
from skopax.runtime import Runtime

log = logging.getLogger('skopax.plugintools')

TOOL_TIMEOUT_S = 120
NEAR_NAMES = 3
ERROR_ROOM_MAX = 800
FIELD = re.compile(r'\{field:([a-z][a-z0-9-]{0,30})\}')


class _TimedOut(Exception):
    pass


def masked(out: object, secrets: list[str]) -> object:
    return masked_tree(out, secrets, READ_LIMIT_ROWS)


def unwatched(field: str, asked: str) -> str:
    return f'. "{asked}" is not configured, so it may be a {field} this install does not watch; do not use another one in its place without saying so in your answer'


def related_names(asked: str, names: list[str]) -> list[str]:
    low = asked.lower()
    return [n for n in names if low in n.lower() or n.lower() in low]


def named_choice(tool: Tool, field: str, names: list[str], args: dict, text: str) -> str | None:
    key = text.partition(' must be one of ')[0]
    choices = ((tool.input_schema.get('properties') or {}).get(key) or {}).get('enum') if key != field and ' must be one of ' in text else None
    value = args.get(key)
    if not choices or not isinstance(value, str):
        return None
    found = re.fullmatch('(.+?)[-_ ]+(' + '|'.join(re.escape(str(c)) for c in choices) + ')', value.strip(), re.I)
    if found is None:
        return None
    if found.group(1) not in names:
        return msg('plugintools.named_unknown', key=key, value=value.strip()[:60], field=field, name=found.group(1)[:60], names=', '.join(names), choices=', '.join(str(c) for c in choices))
    choice = next(str(c) for c in choices if str(c).lower() == found.group(2).lower())
    return msg('plugintools.named_choice', key=key, value=value.strip()[:60], field=field, name=found.group(1), choice=choice)


def error_room(room: object) -> int:
    if not isinstance(room, int) or isinstance(room, bool) or room <= 0:
        return ERROR_LIMIT
    return min(room, ERROR_ROOM_MAX)


class PluginTools:
    def __init__(self, runtime: Runtime, timeout: float = TOOL_TIMEOUT_S):
        self.runtime = runtime
        self.timeout = timeout
        self.allowed: Callable[[Instance], bool] | None = None

    def scoped(self, run: Run) -> 'PluginTools':
        allowed = asked_gate(run)
        if allowed is None:
            return self
        view = copy.copy(self)
        view.allowed = allowed
        return view

    def _kept(self, instance: Instance | None) -> bool:
        return instance is None or self.allowed is None or self.allowed(instance)

    def _held_back(self, plugin_id: str) -> list[Instance]:
        if self.allowed is None:
            return []
        seen: dict[str, Instance] = {}
        for plugin, _, found in runnable(self.runtime):
            if plugin.id == plugin_id:
                seen |= {i.name: i for i in found if not self.allowed(i)}
        return list(seen.values())

    def refused(self) -> list[tuple[Plugin, Instance]]:
        if self.allowed is None:
            return []
        return [(plugin, i) for plugin, found in self.runtime.serving() if asks_gated(plugin) for i in found if not self.allowed(i)]

    def not_in_questions(self) -> list[str]:
        return sorted({f'{plugin.name} {i.name}' for plugin, i in self.refused()}, key=str.lower)

    def refused_rows(self) -> set[tuple[str, str | None]]:
        return {(plugin.id, None if i.primary else i.name) for plugin, i in self.refused()}

    def held_back(self, name: str) -> bool:
        return self.allowed is not None and self._entry(name) is None and any(tool.name == name for _, tool, _ in runnable(self.runtime))

    def _asked_refused(self, plugin: Plugin, asked: object) -> ToolError | None:
        if isinstance(asked, str) and any(i.name == asked for i in self._held_back(plugin.id)):
            return BadInput(msg('plugintools.not_in_questions', name=asked[:60]))
        return None

    def _bounded(self, run, ctx, given: dict) -> object:
        box: dict = {}
        done = threading.Event()

        def go() -> None:
            try:
                box['out'] = run(ctx, given)
            except BaseException as e:
                box['error'] = e
            finally:
                done.set()

        threading.Thread(target=go, name='plugin-tool', daemon=True).start()
        if not done.wait(self.timeout):
            raise _TimedOut()
        if 'error' in box:
            raise box['error']
        return box['out']

    def _offered(self) -> list[tuple[Plugin, Tool, list[Instance]]]:
        return runnable(self.runtime, self.allowed)

    def _entry(self, name: str) -> tuple[Plugin, Tool, list[Instance]] | None:
        return next((e for e in self._offered() if e[1].name == name), None)

    def _primary(self, plugin: Plugin, found: list[Instance]) -> Instance | None:
        return primary_of(self.runtime, plugin, found)

    def _others(self, among: Collection[str] | None) -> list[tuple[Plugin, Tool, list[Instance]]]:
        return [e for e in self._offered() if among is None or e[1].name in among]

    def _elsewhere(self, tool: Tool, field: str, asked: str, among: Collection[str] | None = None) -> list[str]:
        return [other.name for _, other, found in self._others(among) if other.name != tool.name and other.instance_input == field and any(i.name == asked for i in found)]

    def _not_here(self, tool: Tool, field: str, asked: str, names: list[str], among: Collection[str] | None = None) -> str:
        owners = self._elsewhere(tool, field, asked, among)
        close = related_names(asked, names)
        if not owners:
            near = close or difflib.get_close_matches(asked, names, NEAR_NAMES, 0.6)
            return unwatched(field, asked) + (f'; closest configured: {", ".join(near)}' if near else '')
        return (f'. "{asked}" is an instance of {", ".join(owners)}, not of {tool.name}'
                + (f'; did you mean {close[0]}?' if len(close) == 1 else f'; did you mean {" or ".join(close[:NEAR_NAMES])}?' if close else
                   f'; do not use another {field} in its place without saying so in your answer'))

    def _described(self, plugin: Plugin, text: str) -> str:
        if '{field:' not in text:
            return text
        plain = {f.key for f in plugin.fields if f.kind != 'secret'}
        primary = self.runtime.instances.primary(plugin.id)
        values = primary.values if primary is not None and self._kept(primary) else {}
        return FIELD.sub(lambda m: str(values.get(m.group(1)) or '') if m.group(1) in plain else '', text)

    def _definition(self, plugin: Plugin, tool: Tool, found: list[Instance], offered: list[tuple[Plugin, Tool, list[Instance]]]) -> dict:
        schema = tool.input_schema
        if tool.instance_input and plugin.fields and found:
            named = self.runtime.instances.primary(plugin.id)
            named = named if self._kept(named) else None
            usable = self._primary(plugin, found)
            note = f'Instance (default: {usable.name})' if usable is not None else f'Instance ({named.name} is not available, name one)' if named is not None else 'Instance'
            names = [i.name for i in found]
            others = [other.name for _, other, elsewhere in offered if other.name != tool.name and other.instance_input == tool.instance_input and any(i.name not in names for i in elsewhere)]
            if others:
                note += f"; {tool.name} takes only {', '.join(names)}, not the {tool.instance_input} names of {', '.join(others)}"
            pick = {'type': 'string', 'enum': names, 'description': note}
            schema = {**schema, 'properties': {**(schema.get('properties') or {}), tool.instance_input: pick}}
        return {'name': tool.name, 'description': self._described(plugin, tool.description), 'input_schema': schema}

    def definitions(self, among: Collection[str] | None = None) -> list[dict]:
        others = self._others(among)
        return [self._definition(*e, others) for e in self._offered()]

    def get(self, name: str) -> tuple[Plugin, Tool] | None:
        entry = self._entry(name)
        return None if entry is None else (entry[0], entry[1])

    @staticmethod
    def _refuse(text: str, name: object, refused: bool = False) -> ToolError:
        if refused:
            return BadInput(test_error(text) if isinstance(name, str) and test_cluster(name) else text)
        return ToolError(test_error(text) if isinstance(name, str) and test_cluster(name) else text)

    def refuse_input(self, name: str, args: object, text: str, among: Collection[str] | None = None) -> ToolError:
        entry = self._entry(name)
        field = entry[1].instance_input if entry is not None else None
        asked = args.get(field) if field and isinstance(args, dict) else None
        held = self._asked_refused(entry[0], asked) if entry is not None and field else None
        if held is not None:
            return held
        if entry is not None and field and isinstance(asked, str) and asked not in [i.name for i in entry[2]] and text.startswith(f'{field} must be one of'):
            text += self._not_here(entry[1], field, asked, [i.name for i in entry[2]], among)
        elif entry is not None and field and isinstance(args, dict):
            text = named_choice(entry[1], field, [i.name for i in entry[2]], args, text) or text
        return self._refuse(text, asked)

    def default_instance(self, name: str) -> str | None:
        entry = self._entry(name)
        if entry is None or not entry[0].fields:
            return None
        primary = self.runtime.instances.primary(entry[0].id)
        return None if primary is None else primary.name

    def _instance(self, plugin: Plugin, tool: Tool, found: list[Instance], args: dict, among: Collection[str] | None = None) -> Instance | None:
        if not plugin.fields:
            return None
        field = tool.instance_input
        names = ', '.join(i.name for i in found)
        asked = args.pop(field, None) if field else None
        if asked in (None, ''):
            usable = self._primary(plugin, found)
            if usable is not None:
                return usable
            named = self.runtime.instances.primary(plugin.id)
            named = named if self._kept(named) else None
            raise ToolError(f"the default instance{f' {named.name}' if named is not None else ''} of {plugin.id} is not available; {field or 'instance'} must be one of {names}")
        chosen = next((i for i in found if i.name == asked), None) if isinstance(asked, str) else None
        held = self._asked_refused(plugin, asked) if chosen is None else None
        if held is not None:
            raise held
        if chosen is None:
            raise self._refuse(f'{field} must be one of {names}' + (self._not_here(tool, field, asked, [i.name for i in found], among) if isinstance(asked, str) else ''), asked, True)
        return chosen

    def call(self, name: str, args: dict, now: float, rendered: bool = False, states: MutableMapping[str, RunState] | None = None, among: Collection[str] | None = None) -> object:
        entry = self._entry(name)
        if entry is None:
            raise ToolError(f'no tool named {name}')
        plugin, tool, found = entry
        given = dict(args or {})
        instance = self._instance(plugin, tool, found, given, among)
        try:
            out = self._bounded(tool.run, self.runtime.context(plugin, instance, now, run_state=None if states is None else states.setdefault(plugin.id, RunState()), allowed=self.allowed), given)
            if rendered and tool.scan and tool.kind == 'read':
                out = masked(out, sorted((s for s in (instance.secrets.values() if instance is not None else ()) if isinstance(s, str) and s), key=len, reverse=True))
            if rendered and isinstance(out, dict) and isinstance(out.get('image'), dict):
                return out
            return render(out) if rendered else out
        except _TimedOut:
            log.warning('%s', msg('plugintools.timed_out', tool=name, plugin=plugin.id, seconds=f'{self.timeout:g}'))
            raise ToolError(f'the {name} tool timed out after {self.timeout:g} s') from None
        except ReadError as e:
            room = error_room(getattr(e, 'room', None))
            text = redact_text(e.message, room)
            refused = self._refuse(text, None if instance is None else instance.name)
            refused.room = room
            raise refused from None
        except Exception as e:
            said = refusal(e)
            if said is not None:
                raise self._refuse(said, None if instance is None else instance.name) from None
            log.warning('%s', msg('plugintools.failed', tool=name, plugin=plugin.id, error=type(e).__name__))
            raise ToolError(f'{type(e).__name__}: the {name} tool failed') from None

    def proposal(self, name: str, args: dict, now: float, states: MutableMapping[str, RunState] | None = None) -> dict:
        entry = self._entry(name)
        if entry is None:
            raise ToolError(f'no tool named {name}')
        plugin, tool, found = entry
        given = dict(args or {})
        instance = self._instance(plugin, tool, found, given)
        out = self.call(name, args, now, states=states)
        return {'tool': name, 'plugin': plugin.id, 'instance': None if instance is None else instance.name, 'args': given, 'out': out}

    def scan(self, name: str) -> bool:
        entry = self._entry(name)
        return entry is not None and entry[1].scan

    def cap(self, name: str) -> int | None:
        entry = self._entry(name)
        return None if entry is None else entry[1].per_run

    def knowledge(self) -> list[Knowledge]:
        return self.runtime.knowledge()

    def knowledge_parts(self, cards: Sequence[dict] = (), allowed: Callable[[Instance], bool] | None = None) -> tuple[list[Knowledge], list[tuple[str, Knowledge]]]:
        return self.runtime.knowledge_parts(cards, allowed) if allowed is not None else self.runtime.knowledge_parts(cards)

    def catalog(self) -> dict[str, str]:
        return {tool.name: tool.kind for _, tool in held(self.runtime)} | {tool.name: tool.kind for _, tool, _ in self._offered()}

    def descriptions(self) -> dict[str, str]:
        listed = held(self.runtime) + [(plugin, tool) for plugin, tool, _ in self._offered()]
        return {tool.name: self._labelled(plugin, tool.description) for plugin, tool in listed}

    @staticmethod
    def _labelled(plugin: Plugin, text: str) -> str:
        if '{field:' not in text:
            return text
        labels = {f.key: f.label for f in plugin.fields if f.kind != 'secret'}
        return FIELD.sub(lambda m: msg('plugintools.field', label=labels[m.group(1)]) if m.group(1) in labels else '', text)

    def check_tools(self) -> dict[str, str]:
        return {check.id: check.tool for plugin in self.runtime.active() for check in plugin.checks if check.tool}
