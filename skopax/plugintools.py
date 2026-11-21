import logging
import re
import threading
from collections.abc import MutableMapping, Sequence

from skopax.contract import Knowledge, Plugin, ReadError, RunState, Tool
from skopax.instances import Instance
from skopax.investigator.tools import ERROR_LIMIT, READ_LIMIT_ROWS, BadInput, ToolError, render, test_cluster, test_error
from skopax.investigator.toolset import held, primary_of, runnable
from skopax.messages import msg
from skopax.monitor import refusal
from skopax.redact import REDACTED, SECRET_HEADER, redact_lines, redact_text, secrets_masked
from skopax.runtime import Runtime

log = logging.getLogger('skopax.plugintools')

TOOL_TIMEOUT_S = 120
ERROR_ROOM_MAX = 800
FIELD = re.compile(r'\{field:([a-z][a-z0-9-]{0,30})\}')


class _TimedOut(Exception):
    pass


class _Leaf(int):
    pass


def masked(out: object, secrets: list[str]) -> object:
    texts: list[str] = []

    def strip(x: object) -> object:
        if isinstance(x, str):
            for secret in secrets:
                x = x.replace(secret, '***')
            texts.append(secrets_masked(x))
            return _Leaf(len(texts) - 1)
        if isinstance(x, (list, tuple)):
            return [strip(v) for v in x]
        if isinstance(x, dict):
            return {k: REDACTED if isinstance(k, str) and isinstance(v, str) and v and SECRET_HEADER.search(k) else v if k == 'image' else strip(v) for k, v in x.items()}
        return x

    def fill(x: object) -> object:
        if isinstance(x, _Leaf):
            return done[x]
        if isinstance(x, list):
            return [fill(v) for v in x]
        if isinstance(x, dict):
            return {k: fill(v) for k, v in x.items()}
        return x

    shape = [strip(v) for v in out[:READ_LIMIT_ROWS]] + list(out[READ_LIMIT_ROWS:]) if isinstance(out, list) else strip(out)
    done = redact_lines(texts, cards=False)
    return fill(shape)


def unwatched(field: str, asked: str) -> str:
    return f'. "{asked}" is not configured, so it may be a {field} this install does not watch; do not use another one in its place without saying so in your answer'


def error_room(room: object) -> int:
    if not isinstance(room, int) or isinstance(room, bool) or room <= 0:
        return ERROR_LIMIT
    return min(room, ERROR_ROOM_MAX)


class PluginTools:
    def __init__(self, runtime: Runtime, timeout: float = TOOL_TIMEOUT_S):
        self.runtime = runtime
        self.timeout = timeout

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
        return runnable(self.runtime)

    def _entry(self, name: str) -> tuple[Plugin, Tool, list[Instance]] | None:
        return next((e for e in self._offered() if e[1].name == name), None)

    def _primary(self, plugin: Plugin, found: list[Instance]) -> Instance | None:
        return primary_of(self.runtime, plugin, found)

    def _described(self, plugin: Plugin, text: str) -> str:
        if '{field:' not in text:
            return text
        plain = {f.key for f in plugin.fields if f.kind != 'secret'}
        primary = self.runtime.instances.primary(plugin.id)
        values = primary.values if primary is not None else {}
        return FIELD.sub(lambda m: str(values.get(m.group(1)) or '') if m.group(1) in plain else '', text)

    def _definition(self, plugin: Plugin, tool: Tool, found: list[Instance]) -> dict:
        schema = tool.input_schema
        if tool.instance_input and plugin.fields and found:
            named = self.runtime.instances.primary(plugin.id)
            usable = self._primary(plugin, found)
            note = f'Instance (default: {usable.name})' if usable is not None else f'Instance ({named.name} is not available, name one)' if named is not None else 'Instance'
            pick = {'type': 'string', 'enum': [i.name for i in found], 'description': note}
            schema = {**schema, 'properties': {**(schema.get('properties') or {}), tool.instance_input: pick}}
        return {'name': tool.name, 'description': self._described(plugin, tool.description), 'input_schema': schema}

    def definitions(self) -> list[dict]:
        return [self._definition(*e) for e in self._offered()]

    def get(self, name: str) -> tuple[Plugin, Tool] | None:
        entry = self._entry(name)
        return None if entry is None else (entry[0], entry[1])

    @staticmethod
    def _refuse(text: str, name: object, refused: bool = False) -> ToolError:
        if refused:
            return BadInput(test_error(text) if isinstance(name, str) and test_cluster(name) else text)
        return ToolError(test_error(text) if isinstance(name, str) and test_cluster(name) else text)

    def refuse_input(self, name: str, args: object, text: str) -> ToolError:
        entry = self._entry(name)
        field = entry[1].instance_input if entry is not None else None
        asked = args.get(field) if field and isinstance(args, dict) else None
        if entry is not None and field and isinstance(asked, str) and asked not in [i.name for i in entry[2]] and text.startswith(f'{field} must be one of'):
            text += unwatched(field, asked)
        return self._refuse(text, asked)

    def default_instance(self, name: str) -> str | None:
        entry = self._entry(name)
        if entry is None or not entry[0].fields:
            return None
        primary = self.runtime.instances.primary(entry[0].id)
        return None if primary is None else primary.name

    def _instance(self, plugin: Plugin, tool: Tool, found: list[Instance], args: dict) -> Instance | None:
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
            raise ToolError(f"the default instance{f' {named.name}' if named is not None else ''} of {plugin.id} is not available; {field or 'instance'} must be one of {names}")
        chosen = next((i for i in found if i.name == asked), None) if isinstance(asked, str) else None
        if chosen is None:
            raise self._refuse(f'{field} must be one of {names}' + (unwatched(field, asked) if isinstance(asked, str) else ''), asked, True)
        return chosen

    def call(self, name: str, args: dict, now: float, rendered: bool = False, states: MutableMapping[str, RunState] | None = None) -> object:
        entry = self._entry(name)
        if entry is None:
            raise ToolError(f'no tool named {name}')
        plugin, tool, found = entry
        given = dict(args or {})
        instance = self._instance(plugin, tool, found, given)
        try:
            out = self._bounded(tool.run, self.runtime.context(plugin, instance, now, run_state=None if states is None else states.setdefault(plugin.id, RunState())), given)
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

    def knowledge_parts(self, cards: Sequence[dict] = ()) -> tuple[list[Knowledge], list[tuple[str, Knowledge]]]:
        return self.runtime.knowledge_parts(cards)

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
