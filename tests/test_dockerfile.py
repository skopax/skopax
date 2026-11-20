import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = '/usr/share/doc/skopax/'
STATUS = '/deps/var/lib/dpkg/status.d/'
PINNED = re.compile(r'[a-z0-9][a-z0-9./_-]*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}')
OWN_BASE = '${BASE_IMAGE}:${SKOPAX_VERSION}'
BASE_HOLDS_FILE = '[ ! -e "/base-status/$pkg" ] || awk -v real="${real#/}" \'$2 == real {found = 1} END {exit !found}\' "/base-status/$pkg.md5sums" || exit 1'
SKIP = '[ ! -e "/base-status/$pkg" ] || continue'
BASE = 'base'
DISTROLESS = 'gcr.io/distroless/cc-debian13:'
SITE = '/usr/local/lib/python3.15/site-packages'
HELD_STEPS = [
    'pkg="$(dpkg -S "$held_lib" | head -n 1 | cut -d: -f1)" || exit 1',
    '[[ "$pkg" =~ ^[a-z0-9][a-z0-9.+-]+$ ]] || exit 1',
    'test -e "/base-status/$pkg" || exit 1',
    'real="$(readlink -f "$held_lib")" || exit 1',
    'awk -v real="${real#/}" \'$2 == real {found = 1} END {exit !found}\' "/base-status/$pkg.md5sums" || exit 1',
]
FIELDS = {'Package', 'Status', 'Priority', 'Section', 'Installed-Size', 'Maintainer', 'Architecture', 'Multi-Arch', 'Source', 'Version', 'Depends', 'Description', 'Homepage'}


def instructions(path: Path = ROOT / 'Dockerfile') -> list[tuple[str, str]]:
    joined, parts = [], []
    for raw in path.read_text(encoding='utf-8').splitlines():
        if not raw.strip() and not parts:
            continue
        if raw.rstrip().endswith('\\'):
            parts.append(raw.rstrip()[:-1].strip())
            continue
        parts.append(raw.strip())
        joined.append(' '.join(p for p in parts if p))
        parts = []
    return [(line.split(None, 1)[0].upper(), line.split(None, 1)[1] if ' ' in line else '') for line in joined]


def stages() -> list[list[tuple[str, str]]]:
    out = []
    for keyword, args in instructions():
        if keyword == 'FROM':
            out.append([])
        out[-1].append((keyword, args))
    return out


def final() -> list[tuple[str, str]]:
    return stages()[-1]


def named(name: str) -> list[tuple[str, str]]:
    return next(stage for stage in stages() if stage[0][1].split()[-1] == name and len(stage[0][1].split()) == 3)


def build() -> list[tuple[str, str]]:
    return named('build')


def labels() -> dict[str, str]:
    found = {}
    for keyword, args in final():
        if keyword == 'LABEL':
            for pair in shlex.split(args):
                key, value = pair.split('=', 1)
                found[key] = value
    return found


def runs(stage: list[tuple[str, str]]) -> str:
    return '\n'.join(args for keyword, args in stage if keyword == 'RUN')


def images(path: Path) -> list[str]:
    found, stage_names = [], set()
    for keyword, args in instructions(path):
        parts = args.split()
        if keyword == 'FROM':
            if parts[0] not in stage_names:
                found.append(parts[0])
            if len(parts) == 3 and parts[1].upper() == 'AS':
                stage_names.add(parts[2])
        if keyword == 'COPY':
            found += [part.removeprefix('--from=') for part in shlex.split(args) if part.startswith('--from=') and part.removeprefix('--from=') not in stage_names]
    return found


def loop_steps(head: str = 'while read -r lib; do', tail: str = 'done < /build/libs ') -> list[str]:
    text = runs(build()) + ' '
    body = text[text.index(head) + len(head):text.index(tail)]
    return [step.strip() for step in body.split(';') if step.strip()]


def held_pattern() -> str:
    found = re.search(r"held='([^']+)'", runs(build()))
    return found[1] if found else ''


def test_the_final_stage_carries_the_open_container_labels():
    # when
    found = labels()

    # then
    assert found['org.opencontainers.image.licenses'] == 'Apache-2.0'
    assert found['org.opencontainers.image.source'] == 'https://github.com/skopax/skopax'
    assert found['org.opencontainers.image.title'] == 'Skopax'
    assert found['org.opencontainers.image.url'] == 'https://skopax.io'


def test_version_and_revision_labels_come_from_build_args():
    # given
    stage = final()
    names = [args.split('=', 1)[0] for keyword, args in stage if keyword == 'ARG']
    first_label = next(i for i, (keyword, _) in enumerate(stage) if keyword == 'LABEL')
    declared = {args.split('=', 1)[0] for keyword, args in stage[:first_label] if keyword == 'ARG'}

    # then
    assert {'SKOPAX_VERSION', 'COMMIT_ID'} <= set(names)
    assert {'SKOPAX_VERSION', 'COMMIT_ID'} <= declared
    assert labels()['org.opencontainers.image.version'] == '${SKOPAX_VERSION}'
    assert labels()['org.opencontainers.image.revision'] == '${COMMIT_ID}'
    assert [args for keyword, args in stage if keyword == 'ARG' and args.startswith('COMMIT_ID')] == ['COMMIT_ID=dev']


def test_the_build_stage_runs_the_licence_gate_over_python_and_debian():
    # given
    text = runs(build())
    copies = [args for keyword, args in build() if keyword == 'COPY']

    # then
    assert any(args.split()[0] == 'tools/' for args in copies)
    assert 'python3 -m tools.licences check --site /usr/local/lib/python3.15/site-packages --out /notices' in text
    assert 'python3 -m tools.licences debian --libs /build/libs.txt --out /notices' in text
    assert 'dpkg -S' in text
    assert 'cp "/usr/share/doc/$pkg/copyright" "/deps/usr/share/doc/$pkg/" || exit 1' in text
    assert '[[ "$pkg" =~ ^[a-z0-9][a-z0-9.+-]+$ ]] || exit 1' in text
    assert text.index('pip uninstall') < text.index('dpkg -S') < text.index('tools.licences check')
    steps = loop_steps()
    assert len(steps) == 16 and steps.count(SKIP) == 1 and all(step.endswith('|| exit 1') for step in steps if step != SKIP)


def test_a_package_the_base_ships_at_the_same_version_is_skipped_and_another_version_stops_the_build():
    # given
    steps = loop_steps()
    base = BASE
    copies = [shlex.split(args) for keyword, args in build() if keyword == 'COPY']
    version_check = '[ ! -e "/base-status/$pkg" ] || [ "$shipped" = "$have" ] || exit 1'

    # then
    assert [f'--from={base}', '/var/lib/dpkg/status.d/', '/base-status/'] in copies
    assert 'have="$(dpkg-query -W -f=\'${Version}\' "$pkg")" || exit 1' in steps
    assert 'shipped="$(test ! -e "/base-status/$pkg" || sed -n \'s/^Version: //p\' "/base-status/$pkg")" || exit 1' in steps
    assert steps.index('echo "$lib $pkg" >> /build/libs.txt || exit 1') < steps.index(version_check) < steps.index(SKIP) < steps.index('cp --parents -L "$lib" /deps || exit 1')
    assert steps.index('[[ "$pkg" =~ ^[a-z0-9][a-z0-9.+-]+$ ]] || exit 1') < min(i for i, step in enumerate(steps) if 'base-status' in step)
    assert steps.index('real="$(readlink -f "$lib")" || exit 1') < steps.index(BASE_HOLDS_FILE) < steps.index(SKIP)
    assert all(i > steps.index(SKIP) for i, step in enumerate(steps) if '/deps' in step or '/build/sums' in step)


def test_every_image_the_dockerfiles_pull_is_pinned_by_digest():
    # given
    main, example = images(ROOT / 'Dockerfile'), images(ROOT / 'examples' / 'hello-plugin' / 'Dockerfile')
    fuller = [images(ROOT / 'examples' / name / 'Dockerfile') for name in ('couchbase-plugin', 'managed-redis-plugin', 'batch-jobs-plugin')]
    pulled = [image for image in main + example + sum(fuller, []) if image != OWN_BASE]
    text = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    froms = [args.split() for keyword, args in instructions() if keyword == 'FROM']

    # then
    assert len(main) == 3 and OWN_BASE in example and len(pulled) == 7
    assert all(layer == example for layer in fuller)
    assert sorted(p.parent.name for p in (ROOT / 'examples').glob('*/Dockerfile')) == ['batch-jobs-plugin', 'couchbase-plugin', 'hello-plugin', 'managed-redis-plugin']
    assert [image for image in pulled if not PINNED.fullmatch(image)] == []
    assert len({image for image in pulled if image.startswith('python:')}) == 1
    assert froms[0][1:] == ['AS', BASE] and froms[0][0].startswith(DISTROLESS) and final()[0][1] == BASE
    assert text.count(DISTROLESS) == 1 and text.count('@sha256:') == 3
    assert [line for line in text.splitlines() if '@sha256:' in line and not line.startswith('FROM ') and 'astral-sh/uv:' not in line] == []


def test_every_library_left_to_the_base_must_come_from_a_package_the_base_ships():
    # given
    text = runs(build())
    held = loop_steps('while read -r held_lib; do', 'done < /build/held ') if 'while read -r held_lib; do' in text else []

    # then
    assert held_pattern() and all(name in held_pattern() for name in ('libc', 'libssl', 'libcrypto', 'libz', 'libstdc'))
    assert '{ grep -E "$held" /build/linked || true; } > /build/held' in text
    assert '{ grep -Ev "$held" /build/linked || true; } > /build/libs' in text
    assert held == HELD_STEPS
    assert 'test -s /build/held' in text
    assert text.index('> /build/held') < text.index('done < /build/held') < text.index('tools.licences check')


def test_native_libraries_bundled_in_wheels_are_listed_and_must_be_reviewed():
    # given
    text = runs(build())
    listing = f"find {SITE} -path '*.libs/*' -type f | sort > /build/bundled"

    # then
    assert listing in text
    assert f'python3 -m tools.licences bundled --site {SITE} --list /build/bundled --out /notices' in text
    assert text.index('uv sync') < text.index(listing) < text.index('tools.licences bundled')
    assert ['--from=build', '/notices/', DOCS] in [shlex.split(args) for keyword, args in final() if keyword == 'COPY']


def test_the_loop_writes_a_dpkg_status_entry_for_each_copied_package():
    # given
    steps = loop_steps()
    name_check = steps.index('[[ "$pkg" =~ ^[a-z0-9][a-z0-9.+-]+$ ]] || exit 1')
    status = next(step for step in steps if step.startswith('dpkg-query -s "$pkg" | '))
    fields = re.fullmatch(r"dpkg-query -s \"\$pkg\" \| grep -E '\^\(([A-Za-z|-]+)\):' > (\S+) \|\| exit 1", status)
    sums = next(step for step in steps if '/var/lib/dpkg/info/' in step)
    copies = [shlex.split(args) for keyword, args in final() if keyword == 'COPY']

    # then
    assert fields and set(fields.group(1).split('|')) == FIELDS and fields.group(2) == f'"{STATUS}$pkg"'
    assert 'real="$(readlink -f "$lib")" || exit 1' in steps
    assert 'owner="$(dpkg-query -W -f=\'${binary:Package}\' "$pkg")" || exit 1' in steps
    assert sums.startswith('awk -v real="${real#/}" -v lib="${lib#/}" -v doc="usr/share/doc/$pkg/copyright" ')
    assert sums.endswith('"/var/lib/dpkg/info/$owner.md5sums" >> "/build/sums/$pkg" || exit 1')
    assert '$2 == real {print $1 "  " lib}' in sums and 'END {exit !found}' in sums
    assert f'sort -u -k 2 "/build/sums/$pkg" > "{STATUS}$pkg.md5sums" || exit 1' in steps
    assert name_check < min(i for i, step in enumerate(steps) if 'status.d' in step or 'sums' in step)
    assert ['--from=build', '/deps/', '/'] in copies


def test_the_build_stage_drops_the_stdlib_modules_whose_libraries_the_gate_refuses():
    # given
    text = runs(build())
    dynload = '/usr/local/lib/python3.15/lib-dynload/'

    # then
    assert 'python3 -m pip uninstall -y --root-user-action=ignore pip' in text
    for module in ('readline', '_gdbm', '_dbm'):
        assert f'{dynload}{module}.cpython-*.so' in text
    assert text.index(f'{dynload}readline.cpython-*.so') < text.index('ldd')


def test_the_final_stage_copies_the_licence_and_the_notices():
    # given
    copies = [shlex.split(args) for keyword, args in final() if keyword == 'COPY']

    # then
    assert ['LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md', DOCS] in copies
    assert ['--from=build', '/notices/', DOCS] in copies
    assert not any('tools' in part for parts in copies for part in parts)


def test_the_final_stage_runs_nothing():
    # then
    assert [args for keyword, args in final() if keyword == 'RUN'] == []


def test_docs_examples_and_workflows_stay_out_of_the_build_context():
    # given
    ignored = (ROOT / '.dockerignore').read_text(encoding='utf-8').splitlines()

    # then
    assert {'docs', 'examples', '.github', '.env', '.env.*'} <= set(ignored)
