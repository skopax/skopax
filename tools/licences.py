import argparse
import importlib.metadata
import re
import sys
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

OVERRIDES = Path(__file__).with_name('licence_overrides.toml')
ALLOWED = frozenset(('MIT', 'BSD-2-Clause', 'BSD-3-Clause', 'Apache-2.0', 'ISC', 'PSF-2.0', 'Python-2.0', 'MPL-2.0'))
ONLY_FOR = {'regex': frozenset(('CNRI-Python',))}
TEXT_FREE = frozenset(('Apache-2.0', 'PSF-2.0', 'MPL-2.0'))
LICENCE_CLASSIFIER = re.compile(r'license\s*::', re.I)
OSI = 'License :: OSI Approved :: '
CLASSIFIERS = {OSI + name: spdx for name, spdx in (
    ('MIT License', 'MIT'),
    ('Apache Software License', 'Apache-2.0'),
    ('ISC License (ISCL)', 'ISC'),
    ('Python Software Foundation License', 'PSF-2.0'),
    ('Mozilla Public License 2.0 (MPL 2.0)', 'MPL-2.0'),
    ('GNU General Public License v2 (GPLv2)', 'GPL-2.0-only'),
    ('GNU General Public License v2 or later (GPLv2+)', 'GPL-2.0-or-later'),
    ('GNU General Public License v3 (GPLv3)', 'GPL-3.0-only'),
    ('GNU General Public License v3 or later (GPLv3+)', 'GPL-3.0-or-later'),
    ('GNU Lesser General Public License v2 (LGPLv2)', 'LGPL-2.0-only'),
    ('GNU Lesser General Public License v2 or later (LGPLv2+)', 'LGPL-2.0-or-later'),
    ('GNU Lesser General Public License v3 (LGPLv3)', 'LGPL-3.0-only'),
    ('GNU Lesser General Public License v3 or later (LGPLv3+)', 'LGPL-3.0-or-later'),
    ('GNU Affero General Public License v3', 'AGPL-3.0-only'),
    ('GNU Affero General Public License v3 or later (AGPLv3+)', 'AGPL-3.0-or-later'),
)}
BARE_BSD = OSI + 'BSD License'
BSD_MARKERS = ('redistribution and use in source and binary forms', 'redistributions in binary form must reproduce')
NOT_BSD = ('advertising materials', 'gnu general public license', 'common development and distribution', 'eclipse public license', 'non-commercial', 'commercial purposes',
           'general public license', 'affero', 'server side public license', 'lesser general', 'library general')
THIRD_CLAUSE = 'neither the name'
RIDERS = ('commons clause', 'proprietary', 'hosted or managed service')
TEXT_NAMES = ('LICEN', 'COPYING', 'NOTICE')
PYTHON_KEYS = frozenset(('version', 'licence', 'reason', 'text'))
DEBIAN_KEYS = frozenset(('licence', 'reason'))
PROSE_TOKENS = frozenset(('nc', 'noncommercial'))
PROSE_REFUSED = ('gpl', 'general public', 'affero', 'proprietary', 'commercial', 'copyleft', 'gnu', 'non-commercial', 'noncommercial', 'all rights reserved',
                 'cddl', 'sspl', 'eclipse', 'business source', 'closed source', 'eupl', 'osl', 'ms-rl', 'elastic license', 'cpal', 'free software foundation',
                 'open software license', 'microsoft reciprocal', 'common public attribution', 'unlicensed')
PROSE_TEXT_MIN = 200
KNOWN_TEXTS = (
    ('permission is hereby granted free of charge to any person obtaining a copy',),
    ('distribute this software for any purpose with or without fee is hereby granted',),
    BSD_MARKERS,
    ('apache license version 2 0 january 2004',),
    ('licensed under the apache license version 2 0',),
)
UNKNOWN_TEXT = 'licence field is a text that is not a known allowed licence'
TOKEN = re.compile(r'\(|\)|[^\s()]+')
REFUSED = 'refused'


class OverrideError(ValueError):
    pass


@dataclass(frozen=True)
class PythonOverride:
    version: str
    licence: str
    reason: str
    text_name: str | None = None
    text_body: str | None = None


@dataclass(frozen=True)
class DebianEntry:
    licence: str
    reason: str


@dataclass(frozen=True)
class Overrides:
    python: dict[str, PythonOverride] = field(default_factory=dict)
    debian: dict[str, DebianEntry] = field(default_factory=dict)
    bundled: dict[str, DebianEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class Entry:
    name: str
    version: str
    licence: str | None
    problem: str | None
    note: str
    texts: tuple[tuple[str, str], ...]

    @property
    def ok(self) -> bool:
        return self.problem is None


def normal(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def _canonical(text: str) -> str | None:
    try:
        return canonicalize_license_expression(text)
    except InvalidLicenseExpression:
        return None


def _entry(section: str, name: str, table, keys: frozenset[str], needed: tuple[str, ...], words: str) -> str:
    if not isinstance(table, dict) or not all(isinstance(table.get(k), str) and table[k].strip() for k in needed):
        raise OverrideError(f'override {section}.{name} needs {words}')
    unknown = sorted(set(table) - keys)
    if unknown:
        raise OverrideError(f'override {section}.{name} has unknown keys: {", ".join(unknown)}')
    licence = _canonical(table['licence'])
    if licence is None:
        raise OverrideError(f'override {section}.{name} licence is not an SPDX expression: {table["licence"]}')
    return licence


def _vendored(base: Path, name: str, text) -> tuple[str | None, str | None]:
    if text is None:
        return None, None
    if not isinstance(text, str) or not (base / text).is_file():
        raise OverrideError(f'override python.{name} names a text that is not there: {text}')
    return (base / text).name, (base / text).read_text(encoding='utf-8')


def load_overrides(path: Path) -> Overrides:
    data = tomllib.loads(Path(path).read_text(encoding='utf-8'))
    python, debian, bundled = {}, {}, {}
    for name, table in data.get('python', {}).items():
        licence = _entry('python', name, table, PYTHON_KEYS, ('version', 'licence', 'reason'), 'a version, a licence and a reason')
        python[normal(name)] = PythonOverride(table['version'], licence, table['reason'].strip(), *_vendored(Path(path).parent, name, table.get('text')))
    for name, table in data.get('debian', {}).items():
        licence = _entry('debian', name, table, DEBIAN_KEYS, ('licence', 'reason'), 'a licence and a reason')
        debian[name] = DebianEntry(licence, table['reason'].strip())
    for name, table in data.get('bundled', {}).items():
        licence = _entry('bundled', name, table, DEBIAN_KEYS, ('licence', 'reason'), 'a licence and a reason')
        bundled[name] = DebianEntry(licence, table['reason'].strip())
    return Overrides(python, debian, bundled)


def parse(expression: str) -> tuple:
    tokens = TOKEN.findall(expression)
    at = 0

    def joined(word: str, part) -> tuple:
        nonlocal at
        parts = [part()]
        while at < len(tokens) and tokens[at] == word:
            at += 1
            parts.append(part())
        return parts[0] if len(parts) == 1 else (word, tuple(parts))

    def either() -> tuple:
        return joined('OR', both)

    def both() -> tuple:
        return joined('AND', one)

    def one() -> tuple:
        nonlocal at
        token = tokens[at]
        at += 1
        if token == '(':
            node = either()
            at += 1
            return node
        if at < len(tokens) and tokens[at] == 'WITH':
            at += 2
            return 'WITH', f'{token} WITH {tokens[at - 1]}'
        return 'ID', token

    return either()


def _passes(node: tuple, allowed: frozenset[str]) -> bool:
    kind, value = node
    if kind == 'ID':
        return value in allowed
    if kind == 'WITH':
        return False
    results = [_passes(part, allowed) for part in value]
    return any(results) if kind == 'OR' else all(results)


def _chosen(node: tuple, allowed: frozenset[str]) -> str:
    kind, value = node
    if kind in ('ID', 'WITH'):
        return value
    if kind == 'OR':
        return _chosen(next(part for part in value if _passes(part, allowed)), allowed)
    return ' AND '.join(_chosen(part, allowed) for part in value)


def permitted(expression: str, allowed: frozenset[str]) -> bool:
    return _passes(parse(expression), allowed)


def used_under(expression: str, allowed: frozenset[str]) -> str | None:
    node = parse(expression)
    if 'OR' not in TOKEN.findall(expression) or not _passes(node, allowed):
        return None
    return _chosen(node, allowed)


def _bare_bsd(texts: tuple[tuple[str, str], ...]) -> str | None:
    bodies = [' '.join(text.split()).lower() for _, text in texts]
    if any(word in body for body in bodies for word in NOT_BSD):
        return None
    bsd = [body for body in bodies if all(marker in body for marker in BSD_MARKERS)]
    if not bsd:
        return None
    return 'BSD-3-Clause' if any(THIRD_CLAUSE in body for body in bsd) else 'BSD-2-Clause'


def _classified(meta, texts: tuple[tuple[str, str], ...]) -> tuple[str | None, bool]:
    found, known = set(), True
    for raw in meta.get_all('Classifier') or ():
        classifier = raw.strip()
        if not LICENCE_CLASSIFIER.match(classifier):
            continue
        spdx = _bare_bsd(texts) if classifier == BARE_BSD else CLASSIFIERS.get(classifier)
        if spdx is None:
            known = False
        else:
            found.add(spdx)
    return ' AND '.join(sorted(found)) or None, known


def judge(key: str, meta, texts: tuple[tuple[str, str], ...]) -> tuple[str | None, str | None, str]:
    allowed = ALLOWED | ONLY_FOR.get(key, frozenset())
    raw = (meta.get('License-Expression') or '').strip()
    expression = _canonical(raw) if raw else None
    if raw and expression is None:
        return raw, REFUSED, ''
    field_text = (meta.get('License') or '').strip()
    field_expression = _canonical(field_text) if field_text else None
    classified, known = _classified(meta, texts)
    sources = [s for s in (expression, field_expression, classified) if s]
    licence = sources[0] if sources else None
    for header in ('License-Expression', 'License'):
        if len(meta.get_all(header) or ()) > 1:
            return licence, f'more than one {header} header', ''
    if not known:
        primary = expression or field_expression
        return primary, 'unmapped licence classifier' if primary else REFUSED, ''
    prose = ' '.join(field_text.split())
    if field_text and field_expression is None and len(prose) > PROSE_TEXT_MIN:
        found = next((word for word in NOT_BSD if word in prose.lower()), None)
        if found:
            return licence, f'licence field text names {found}', ''
        if not _known_text(prose):
            return licence, UNKNOWN_TEXT, ''
        rider = next((word for word in RIDERS if word in prose.lower()), None)
        if rider:
            return licence, f'licence field text names {rider}', ''
    elif field_text and field_expression is None and _prose_refused(prose):
        return licence, f'licence field names {prose[:80]}', ''
    if licence is None or not permitted(licence, allowed):
        return licence, REFUSED, ''
    for lower in sources[1:]:
        if not permitted(lower, allowed):
            return licence, f'also declares {lower}', ''
    return licence, _textless(licence, texts, allowed), _under(licence, allowed, texts)


def _folded(text: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', text).lower().replace('-', ' ').split())


def _known_text(prose: str) -> bool:
    plain = ' '.join(re.findall(r'[a-z0-9]+', unicodedata.normalize('NFKC', prose).lower()))
    return any(all(marker in plain for marker in markers) for markers in KNOWN_TEXTS)


def _prose_refused(prose: str) -> bool:
    folded = _folded(prose)
    return any(_folded(word) in folded for word in PROSE_REFUSED) or not PROSE_TOKENS.isdisjoint(re.split(r'[^a-z0-9]+', folded))


def _textless(licence: str, texts: tuple, allowed: frozenset[str]) -> str | None:
    return None if texts or permitted(licence, TEXT_FREE & allowed) else 'no licence text'


def _under(licence: str, allowed: frozenset[str], texts: tuple) -> str:
    chosen = used_under(licence, allowed if texts else TEXT_FREE & allowed) or used_under(licence, allowed)
    return f'used under {chosen}' if chosen else ''


def _read(path: Path) -> str:
    return path.read_bytes().decode('utf-8', errors='replace')


def licence_texts(folder: Path, dist) -> tuple[tuple[str, str], ...]:
    names = dist.metadata.get_all('License-File') or []
    files = []
    for name in names:
        found = next((folder / c for c in (f'licenses/{name}', name) if (folder / c).is_file()), None)
        if found is not None:
            files.append((name, found))
    if not names:
        for path in dist.files or ():
            parts = path.parts
            if len(parts) < 2 or not parts[0].endswith('.dist-info'):
                continue
            if parts[1] == 'licenses' and len(parts) > 2:
                files.append(('/'.join(parts[2:]), Path(path.locate())))
            elif len(parts) == 2 and parts[1].upper().startswith(TEXT_NAMES):
                files.append((parts[1], Path(path.locate())))
    files += _unlisted(folder, {path.resolve() for _, path in files})
    return tuple((name, text) for name, text in ((name, _read(path)) for name, path in files) if text.strip())


def _unlisted(folder: Path, known: set[Path]) -> list[tuple[str, Path]]:
    root = folder / 'licenses'
    if not folder.name.endswith('.dist-info') or not root.is_dir():
        return []
    return [(path.relative_to(root).as_posix(), path) for path in sorted(root.rglob('*')) if path.is_file() and path.resolve() not in known]


def survey(site: Path, rules: Overrides) -> list[Entry]:
    return _survey(site, rules)[0]


def _distributions(site: Path) -> list[tuple[Path, importlib.metadata.Distribution]]:
    folders = sorted([*Path(site).glob('*.dist-info'), *Path(site).glob('*.egg-info')])
    return [(folder, importlib.metadata.PathDistribution(folder)) for folder in folders if folder.is_dir() or folder.is_file()]


def _broken(folder: Path, dist) -> str | None:
    if not folder.is_dir():
        return f'unsupported one-file {folder.suffix.removeprefix(".")}'
    try:
        meta = dist.metadata
    except UnicodeDecodeError:
        return 'unreadable metadata'
    except importlib.metadata.MetadataNotFound:
        return 'no metadata file'
    for header in ('Name', 'Version'):
        if not (meta.get(header) or '').strip():
            return f'no {header} in metadata'
    return None


def _survey(site: Path, rules: Overrides) -> tuple[list[Entry], list[str], list[str], list[str]]:
    entries, used, seen, broken = {}, set(), {}, []
    for folder, dist in _distributions(site):
        problem = _broken(folder, dist)
        if problem:
            broken.append(f'{folder.name}: {problem}')
            continue
        name, version = dist.metadata['Name'], dist.version
        key = normal(name)
        seen[key] = seen.get(key, 0) + 1
        if key in entries:
            continue
        rule, texts = rules.python.get(key), licence_texts(folder, dist)
        if rule and rule.version == version:
            used.add(key)
            texts += ((rule.text_name, rule.text_body),) if rule.text_body is not None and rule.text_body.strip() else ()
            allowed = ALLOWED | ONLY_FOR.get(key, frozenset())
            note = '; '.join(part for part in (f'reviewed: {rule.reason}', _under(rule.licence, allowed, texts)) if part)
            entries[key] = Entry(name, version, rule.licence, _textless(rule.licence, texts, allowed), note, texts)
            continue
        licence, problem, note = judge(key, dist.metadata, texts)
        entries[key] = Entry(name, version, licence, problem, note, texts)
    stale = [f'override python.{key} {rule.version} matches no distribution' for key, rule in sorted(rules.python.items()) if key not in used]
    repeated = [key for key, count in sorted(seen.items()) if count > 1]
    return [entries[k] for k in sorted(entries)], stale, repeated, broken


def _cell(text: str) -> str:
    return text.replace('|', '\\|').replace('\n', ' ')


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r'`+', text)), default=0)
    return '`' * max(3, longest + 1)


def python_list(entries: list[Entry]) -> str:
    lines = ['# Third-party Python distributions', '',
             'The Python distributions installed in this image and the licence each one declares, generated at build time by `tools/licences.py`.', '',
             '| Distribution | Version | Licence | Note |', '|---|---|---|---|']
    lines += [f'| {_cell(e.name)} | {_cell(e.version)} | {_cell(e.licence or "unknown")} | {_cell(e.note)} |' for e in entries]
    for e in entries:
        if not e.texts:
            continue
        lines += ['', f'## {e.name} {e.version}']
        for name, text in e.texts:
            fence = _fence(text)
            lines += ['', f'### {name}', '', f'{fence}text', text.rstrip('\n'), fence]
    return '\n'.join(lines) + '\n'


def read_libs(path: Path) -> dict[str, list[str]]:
    packages: dict[str, list[str]] = {}
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        if not raw.strip():
            continue
        lib, package = raw.split()
        packages.setdefault(package.split(':', 1)[0], []).append(lib)
    return {k: sorted(v) for k, v in sorted(packages.items())}


def debian_list(packages: dict[str, list[str]], rules: Overrides) -> str:
    lines = ['# Third-party Debian libraries', '',
             'The shared libraries this image copies, or relies on the base image for, from Debian packages and the licence reviewed for each package, generated at build time by `tools/licences.py`. '
             'Each package ships its copyright file at the path given.', '',
             '| Package | Licence | Libraries | Review | Copyright file |', '|---|---|---|---|---|']
    for package, libs in packages.items():
        entry = rules.debian[package]
        files = '<br>'.join(f'`{lib}`' for lib in libs)
        lines.append(f'| {package} | {_cell(entry.licence)} | {files} | {_cell(entry.reason)} | `/usr/share/doc/{package}/copyright` |')
    return '\n'.join(lines) + '\n'


def bundled_list(libs: dict[str, str], rules: Overrides) -> str:
    lines = ['# Third-party libraries bundled in Python wheels', '',
             'The native libraries that Python wheels in this image bundle beside their own code, and the licence reviewed for each one, generated at build time by `tools/licences.py`.', '',
             '| Library | Licence | Review |', '|---|---|---|']
    lines += [f'| `{_cell(key)}` | {_cell(rules.bundled[key].licence)} | {_cell(rules.bundled[key].reason)} |' for key in sorted(libs)]
    return '\n'.join(lines) + '\n'


def _write(out: Path, name: str, text: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(text, encoding='utf-8')


def _refusal(e: Entry) -> str:
    line = f'{e.name} {e.version}: {e.licence or "unknown"}'
    return line if e.problem == REFUSED else f'{line}; {e.problem}'


def check(site: Path, out: Path, rules: Overrides) -> int:
    entries, stale, repeated, broken = _survey(site, rules)
    if not entries and not broken:
        print(f'no distributions found in {site}')
        return 1
    refused = [e for e in entries if not e.ok and normal(e.name) not in repeated]
    for line in [*broken, *(f'{key}: more than one distribution' for key in repeated), *map(_refusal, refused), *stale]:
        print(line)
    if broken or refused or repeated:
        return 1
    _write(out, 'third-party-python.md', python_list(entries))
    return 0


def debian(libs: Path, out: Path, rules: Overrides) -> int:
    packages = read_libs(libs)
    missing = [(package, lib) for package, found in packages.items() if package not in rules.debian for lib in found]
    for package, lib in missing:
        print(f'{package}: no reviewed licence for {lib}')
    for package in sorted(set(rules.debian) - set(packages)):
        print(f'override debian.{package} matches no library')
    if missing:
        return 1
    _write(out, 'third-party-debian.md', debian_list(packages, rules))
    return 0


def bundled(site: Path, listing: Path, out: Path, rules: Overrides) -> int:
    root = Path(site)
    libs = {Path(raw.strip()).relative_to(root).as_posix(): raw.strip() for raw in Path(listing).read_text(encoding='utf-8').splitlines() if raw.strip()}
    missing = [path for key, path in sorted(libs.items()) if key not in rules.bundled]
    for path in missing:
        print(f'{path}: no reviewed licence for a library bundled in a wheel')
    for key in sorted(set(rules.bundled) - set(libs)):
        print(f'override bundled.{key} matches no library')
    if missing:
        return 1
    if libs:
        _write(out, 'third-party-bundled.md', bundled_list(libs, rules))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='licences')
    commands = parser.add_subparsers(dest='command', required=True)
    python = commands.add_parser('check')
    python.add_argument('--site', type=Path, required=True)
    python.add_argument('--out', type=Path, required=True)
    python.add_argument('--overrides', type=Path, default=OVERRIDES)
    deb = commands.add_parser('debian')
    deb.add_argument('--libs', type=Path, required=True)
    deb.add_argument('--out', type=Path, required=True)
    deb.add_argument('--overrides', type=Path, default=OVERRIDES)
    wheel = commands.add_parser('bundled')
    wheel.add_argument('--site', type=Path, required=True)
    wheel.add_argument('--list', type=Path, required=True)
    wheel.add_argument('--out', type=Path, required=True)
    wheel.add_argument('--overrides', type=Path, default=OVERRIDES)
    args = parser.parse_args(argv)
    try:
        rules = load_overrides(args.overrides)
    except OverrideError as error:
        print(error)
        return 1
    if args.command == 'check':
        return check(args.site, args.out, rules)
    if args.command == 'bundled':
        return bundled(args.site, args.list, args.out, rules)
    return debian(args.libs, args.out, rules)


if __name__ == '__main__':
    sys.exit(main())
