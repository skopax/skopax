import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path

from skopax import promqlchecks
from skopax.promqlchecks import DATASOURCE_MAX, FIELDS, ID, INSTANCE_MAX, LABEL, LINE_BREAKS, SOURCES, Definitions, Exists, Invalid, clean

DIR = Path(__file__).parent / 'presets'
MARK = re.compile(r'\$\{([a-z][a-z0-9_]*)\}')
KEY = re.compile(r'[a-z][a-z0-9_]*')
CHECK_KEY = re.compile(r'[a-z][a-z0-9-]{0,30}')
NAME = re.compile(r'[a-z][a-z0-9-]*[a-z0-9]')
MATCHER = r' *[a-zA-Z_][a-zA-Z0-9_]* *(?:=~|!~|!=|=) *"(?:[^"\\\n]|\\.)*" *'
MATCHERS = re.compile(f'{MATCHER}(?:,{MATCHER})*')
PREFIX = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]*')
KINDS = {
    'instance': ('instance',),
    'datasource': ('datasource',),
    'name': ('id', 'name', 'title', 'text'),
    'matchers': ('query',),
    'prefix': ('query', 'text'),
    'label': ('query', 'by', 'title', 'text'),
}
MAXES = {'instance': INSTANCE_MAX, 'datasource': DATASOURCE_MAX, 'name': 20, 'matchers': 300, 'prefix': 100, 'label': 100}
ONE_OF = ('instance', 'name')
PRESET_KEYS = frozenset({'id', 'name', 'summary', 'source', 'needs', 'placeholders', 'checks'})
PLACEHOLDER_KEYS = frozenset({'key', 'kind', 'label', 'help', 'example', 'default', 'optional', 'max'})
CHECK_KEYS = frozenset({'key'} | set(FIELDS) - {'enabled'})
TEXT_MAX = 400
RESERVED = frozenset({'value', 'base', '__name__'})
NAMED = 5


class PresetError(ValueError):
    pass


def _shaped(v: str, p: dict) -> bool:
    kind = p['kind']
    if kind == 'name':
        return bool(NAME.fullmatch(v))
    if kind == 'matchers':
        return bool(MATCHERS.fullmatch(v)) or (v == '' and p['optional'])
    if kind == 'prefix':
        return bool(PREFIX.fullmatch(v))
    if kind == 'label':
        return bool(LABEL.fullmatch(v)) and v not in RESERVED and not v.startswith('__')
    return bool(v) and not LINE_BREAKS.search(v)


SHAPES = {
    'instance': 'the name of an instance set up under Integrations',
    'datasource': 'the name or uid of a Grafana datasource',
    'name': '2 to {max} lowercase letters, digits or dashes, starting with a letter',
    'matchers': 'label matchers such as {example}: label, operator, quoted value, joined by commas',
    'prefix': 'the start of a metric name, such as {example}',
    'label': 'a label name, such as {example}',
}


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    summary: str
    source: str
    needs: str
    placeholders: tuple
    checks: tuple

    def values(self, raw: object) -> dict:
        if not isinstance(raw, dict):
            raise Invalid('the placeholder values must be an object')
        known = {p['key'] for p in self.placeholders}
        extra = sorted(str(k)[:40] for k in raw if k not in known)
        if extra:
            raise Invalid(f"unknown placeholder {', '.join(extra)} for the preset {self.name}")
        out = {}
        for p in self.placeholders:
            v = raw.get(p['key'])
            if v is None:
                v = ''
            if not isinstance(v, str):
                raise Invalid(f"{p['label']} must be text")
            v = v.strip()
            if v == '' and 'default' in p:
                v = p['default']
            if len(v) > p['max'] or not _shaped(v, p) or (p['kind'] == 'name' and len(v) < 2):
                raise Invalid(f"{p['label']} must be " + SHAPES[p['kind']].format(max=p['max'], example=p['example']) + f" (at most {p['max']} characters)")
            out[p['key']] = v
        return out

    def fill(self, raw: object) -> tuple[dict, list[dict]]:
        values = self.values(raw)

        def put(text: str) -> str:
            return MARK.sub(lambda m: values[m[1]], text)

        out = []
        for c in self.checks:
            filled = {k: put(v) if isinstance(v, str) else [put(b) for b in v] if k == 'by' else v for k, v in c.items() if k != 'key'}
            try:
                out.append(clean(filled))
            except Invalid as e:
                raise Invalid(f"{self.name} check {c['key']}: {e.text}") from None
        return values, out

    def view(self) -> dict:
        def hint(text: str) -> str:
            return MARK.sub(lambda m: f'<{m[1]}>', text)

        return {'id': self.id, 'name': self.name, 'summary': self.summary, 'source': self.source, 'needs': self.needs,
                'placeholders': [{k: p[k] for k in ('key', 'kind', 'label', 'help', 'example', 'default', 'optional', 'max') if k in p} for p in self.placeholders],
                'checks': [{'key': c['key'], 'name': hint(c['name']), 'severity': c.get('severity') or 'warn'} for c in self.checks]}


def _fail(where: str, why: str) -> PresetError:
    return PresetError(f'preset {where}: {why}')


def _text(raw: dict, name: str, where: str) -> str:
    v = raw.get(name)
    if not isinstance(v, str) or not v.strip() or len(v) > TEXT_MAX or LINE_BREAKS.search(v):
        raise _fail(where, f'{name} must be one line of text')
    return v


def _placeholder(raw: object, where: str) -> dict:
    if not isinstance(raw, dict) or not set(raw) <= PLACEHOLDER_KEYS or not {'key', 'kind', 'label', 'example'} <= set(raw):
        raise _fail(where, f'a placeholder must hold key, kind, label and example, and only {", ".join(sorted(PLACEHOLDER_KEYS))}')
    if not isinstance(raw['key'], str) or not KEY.fullmatch(raw['key']):
        raise _fail(where, f"the placeholder key {str(raw['key'])[:40]} is not a lowercase name")
    if raw['kind'] not in KINDS:
        raise _fail(where, f"the placeholder {raw['key']} has the kind {str(raw['kind'])[:40]}, not one of {', '.join(KINDS)}")
    most = raw.get('max', MAXES[raw['kind']])
    if isinstance(most, bool) or not isinstance(most, int) or not 1 <= most <= MAXES[raw['kind']]:
        raise _fail(where, f"the placeholder {raw['key']} has a max outside 1 to {MAXES[raw['kind']]}")
    optional = raw.get('optional', False)
    if not isinstance(optional, bool) or (optional and raw['kind'] != 'matchers'):
        raise _fail(where, f"only a matchers placeholder may be optional, not {raw['key']}")
    out = {'key': raw['key'], 'kind': raw['kind'], 'label': _text(raw, 'label', where), 'help': raw.get('help') or '', 'example': raw['example'], 'optional': optional,
           'max': most}
    if 'default' in raw:
        out['default'] = raw['default']
    if not isinstance(out['help'], str) or len(out['help']) > TEXT_MAX or not all(isinstance(out.get(k, ''), str) for k in ('example', 'default')):
        raise _fail(where, f"the placeholder {raw['key']} has a help, example or default that is not text")
    if 'default' in out and (len(out['default']) > most or out['default'] != out['default'].strip() or not _shaped(out['default'], out)
                             or (out['kind'] == 'name' and len(out['default']) < 2)):
        raise _fail(where, f"the default of the placeholder {raw['key']} does not have the shape of its kind {raw['kind']}")
    return out


def _marks(text: object) -> list[str]:
    if isinstance(text, str):
        return MARK.findall(text)
    if isinstance(text, list):
        return [m for t in text if isinstance(t, str) for m in MARK.findall(t)]
    return []


def _check(raw: object, where: str, kinds: dict, source: str) -> dict:
    if not isinstance(raw, dict) or not set(raw) <= CHECK_KEYS:
        raise _fail(where, f'a check may hold only {", ".join(sorted(CHECK_KEYS))}')
    key = raw.get('key')
    if not isinstance(key, str) or not CHECK_KEY.fullmatch(key):
        raise _fail(where, f'the check key {str(key)[:40]} is not 1 to 31 lowercase letters, digits or dashes')
    if raw.get('source') != source:
        raise _fail(where, f'the check {key} must read from the source {source}')
    own = {k for k, kind in kinds.items() if kind == 'instance'}
    if raw.get('instance') not in {f'${{{k}}}' for k in own}:
        raise _fail(where, f'the check {key} must take its instance from the instance placeholder')
    ds = {k for k, kind in kinds.items() if kind == 'datasource'}
    if source == 'grafana' and raw.get('datasource') not in {f'${{{k}}}' for k in ds}:
        raise _fail(where, f'the check {key} must take its datasource from the datasource placeholder')
    for field_name, value in raw.items():
        for m in _marks(value):
            if m not in kinds:
                raise _fail(where, f'the check {key} names the unknown placeholder {m} in {field_name}')
            if field_name not in KINDS[kinds[m]]:
                raise _fail(where, f'the check {key} may not use the {kinds[m]} placeholder {m} in {field_name}')
        if isinstance(value, str) and '${' in MARK.sub('', value):
            raise _fail(where, f'the check {key} holds a stray ${{ in {field_name}')
    return dict(raw)


def _sample(p: dict, longest: bool) -> str:
    if not longest:
        return p['example']
    most = p['max']
    return {'matchers': 'a="' + 'x' * (most - 4) + '"', 'prefix': 'p' * most, 'label': 'l' * most, 'name': 'n' + 'x' * (most - 1), 'instance': 'i' * most,
            'datasource': 'd' * most}[p['kind']]


def _preset(raw: object, stem: str) -> Preset:
    where = stem
    if not isinstance(raw, dict) or set(raw) != PRESET_KEYS:
        extra = sorted(set(raw) - PRESET_KEYS) if isinstance(raw, dict) else []
        raise _fail(where, f"it must hold exactly {', '.join(sorted(PRESET_KEYS))}" + (f" (not {', '.join(extra)})" if extra else ''))
    if not isinstance(raw['id'], str) or not ID.fullmatch(raw['id']):
        raise _fail(where, 'its id must be 2 to 41 lowercase letters, digits or dashes')
    if raw['id'] != stem:
        raise _fail(where, f"the file {stem}.json must be named after its id {raw['id']}")
    if raw['source'] not in SOURCES:
        raise _fail(where, f"its source must be one of {', '.join(SOURCES)}")
    texts = {k: _text(raw, k, where) for k in ('name', 'summary', 'needs')}
    if not isinstance(raw['placeholders'], list):
        raise _fail(where, 'placeholders must be a list')
    placeholders = [_placeholder(p, where) for p in raw['placeholders']]
    kinds = {p['key']: p['kind'] for p in placeholders}
    if len(kinds) != len(placeholders):
        raise _fail(where, 'two placeholders share a key')
    counts = {k: list(kinds.values()).count(k) for k in KINDS}
    wanted = {k: 1 for k in ONE_OF} | {'datasource': 1 if raw['source'] == 'grafana' else 0}
    for kind, n in wanted.items():
        if counts[kind] != n:
            raise _fail(where, f'it needs exactly {n} {kind} placeholder' + ('' if n == 1 else 's'))
    if not isinstance(raw['checks'], list) or not 1 <= len(raw['checks']) <= promqlchecks.CHECKS_MAX:
        raise _fail(where, f'checks must be a list of 1 to {promqlchecks.CHECKS_MAX} checks')
    checks = [_check(c, where, kinds, raw['source']) for c in raw['checks']]
    keys = [c['key'] for c in checks]
    twice = sorted({k for k in keys if keys.count(k) > 1})
    if twice:
        raise _fail(where, f"two checks share the key {', '.join(twice)}")
    preset = Preset(raw['id'], texts['name'], texts['summary'], raw['source'], texts['needs'], tuple(placeholders), tuple(checks))
    for longest in (False, True):
        try:
            values, made = preset.fill({p['key']: _sample(p, longest) for p in placeholders})
        except Invalid as e:
            raise _fail(where, ('with the longest values: ' if longest else 'with the example values: ') + e.text) from None
        ids = [d['id'] for d in made]
        if len(set(ids)) != len(ids):
            raise _fail(where, 'two checks get the same id')
        if not longest and values != {p['key']: p['example'] for p in placeholders}:
            raise _fail(where, 'an example value is not in its normal form (no spaces around it)')
    return preset


def load(folder: Path = DIR) -> dict[str, Preset]:
    out = {}
    for path in sorted(folder.glob('*.json')):
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            raise _fail(path.stem, f'it is not JSON ({type(e).__name__})') from None
        out[path.stem] = _preset(raw, path.stem)
    return out


@functools.cache
def presets() -> dict[str, Preset]:
    return load()


def named(docs: list[dict], order: list[str]) -> str:
    rank = {check_id: n for n, check_id in enumerate(order)}
    names = [str(d.get('name') or d.get('id')) for d in sorted(docs, key=lambda d: (rank.get(d.get('id'), len(rank)), str(d.get('id'))))]
    return ', '.join(names[:NAMED]) + (f' and {len(names) - NAMED} more' if len(names) > NAMED else '')


def add(defs: Definitions, preset: Preset, raw: object, by: str) -> list[dict]:
    values, made = preset.fill(raw)
    same = [d for d in defs.list() if d.get('preset') == preset.id and d.get('presetValues') == values]
    if same:
        raise Exists(f'the preset {preset.name} is already added with these values: {named(same, [d['id'] for d in made])}; delete those checks to add it again')
    taken = [d['id'] for d in made if defs.get(d['id']) is not None]
    if taken:
        raise Exists(f"custom checks with the ids {', '.join(taken[:NAMED])} exist: choose another {label_of(preset, 'name')}")
    return defs.create_all(made, by, {'preset': preset.id, 'presetValues': values}, f'the preset {preset.name}')


def label_of(preset: Preset, kind: str) -> str:
    return next(p['label'] for p in preset.placeholders if p['kind'] == kind)


def name_key(preset: Preset) -> str:
    return next(p['key'] for p in preset.placeholders if p['kind'] == 'name')


def title_of(preset: Preset, values: dict) -> str:
    return values[name_key(preset)]
