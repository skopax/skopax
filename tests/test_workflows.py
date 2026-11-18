import copy
import json
import re
import subprocess
import tomllib
from pathlib import Path

import yaml

from tests.yaml_kit import yaml_comment_lines
from tools import release_version

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'
EXAMPLE_DOCKERFILE = ROOT / 'examples' / 'hello-plugin' / 'Dockerfile'
PINNED = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}')
DIGEST = re.compile(r'[^\s@]+@sha256:[0-9a-f]{64}')
LOCAL_CALLS = {'./.github/workflows/ci.yml'}
READ_ONLY = {'contents': 'read'}
PERMISSIONS = {
    **{('ci.yml', job): READ_ONLY for job in ('tests', 'pages', 'licences', 'secrets')},
    ('image.yml', 'ci'): READ_ONLY,
    ('image.yml', 'version'): READ_ONLY,
    ('image.yml', 'build'): {'contents': 'read', 'packages': 'write', 'id-token': 'write', 'attestations': 'write'},
    ('image.yml', 'layer-smoke'): {'contents': 'read', 'packages': 'read'},
    ('image.yml', 'promote'): {'contents': 'read', 'packages': 'write'},
}
CI_JOBS = {'tests', 'pages', 'licences', 'secrets'}
IMAGE_JOBS = {'ci', 'version', 'build', 'layer-smoke', 'promote'}
NEEDS = {'version': {'ci'}, 'build': {'version'}, 'layer-smoke': {'version', 'build'}, 'promote': {'version', 'build', 'layer-smoke'}}
STATUS_FUNCTIONS = ('always()', 'cancelled()', 'failure()', 'success()')
NOT_RC = "needs.version.outputs.rc == 'false'"
PUBLIC_ONLY = 'github.event.repository.private == false'
IMAGE = '${{ needs.version.outputs.image }}'
VERSION = '${{ needs.version.outputs.version }}'
HUB_SECRETS = {'DOCKERHUB_USERNAME', 'DOCKERHUB_TOKEN'}
SECRET_REF = re.compile(r'\$\{\{\s*secrets\.([A-Za-z0-9_]+)')
FIRST_ADMIN_PASSWORD = re.compile(r'''SKOPAX_ADMIN_PASSWORD="\$\(python3 -c 'import secrets; print\(secrets\.token_urlsafe\((\d+)\)\)'\)"''')
HUB = 'docker.io/skopax/skopax'
SITE = '.venv/lib/python3.15/site-packages'
PAGE_SUITES = 'node tests/page/run.js'
GITLEAKS_URL = 'https://github.com/gitleaks/gitleaks/releases/download/v'
OUTSIDE = ('/Users/', '/home/', '~/', '$HOME')
PIPED_SHELL = re.compile(r'\|\s*(?:sudo\s+)?(?:ba|z)?sh\b')
GITLEAKS_FIXTURES = {
    'tests/page/skopax-access.test.js': 'generic-api-key',
    'tests/test_admin_plugins.py': 'generic-api-key',
    'tests/test_instances.py': 'private-key',
    'tests/test_mcp_access.py': 'jwt',
    'tests/test_plugin_tools.py': 'jwt',
    'tests/test_redact_long_tokens.py': 'generic-api-key',
    'tests/test_settings_sources.py': 'generic-api-key',
    'tests/test_slack_plugin.py': 'generic-api-key',
    'tests/test_umami_plugin.py': 'generic-api-key',
}
BYTECODE_PATH = r'^tests/(?:[^/]+/)*__pycache__/[^/]+\.pyc$'
BYTECODE_RULES = ['generic-api-key', 'jwt', 'private-key']
LITERAL_REGEX = re.compile(r'\^((?:[A-Za-z0-9_= -]|\\\.|\\\\|-\{5\})+)\$')
REGEX_TOKEN = re.compile(r'\\\.|\\\\|-\{5\}|[A-Za-z0-9_= -]')
REGEX_LITERAL = {'\\.': '.', '\\\\': '\\', '-{5}': '-----'}


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding='utf-8'))


def triggers(doc: dict) -> dict:
    found = doc.get('on', doc.get(True))
    return found if isinstance(found, dict) else {}


def steps(doc: dict, job: str | None = None) -> list[dict]:
    return [s for name, j in doc.get('jobs', {}).items() if job in (None, name) for s in j.get('steps', [])]


def runs(doc: dict, job: str | None = None) -> list[str]:
    return [s['run'] for s in steps(doc, job) if 'run' in s]


def uses(doc: dict, action: str, job: str | None = None) -> list[dict]:
    return [s for s in steps(doc, job) if s.get('uses', '').split('@')[0] == action]


def index_of(doc: dict, job: str, needle: str) -> int:
    for n, s in enumerate(steps(doc, job)):
        if needle in s.get('run', '') or needle == s.get('uses', '').split('@')[0]:
            return n
    return -1


def needs_of(job: dict) -> set[str]:
    needs = job.get('needs')
    return {needs} if isinstance(needs, str) else set(needs or [])


def version_script(image: dict) -> str:
    return '\n'.join(runs(image, 'version'))


def tag_form(version: str) -> str:
    return release_version.release_tag(version).removeprefix('v')


def run_version_script(script: str, tmp_path: Path, ref: str, tags: list[str], repository: str = 'skopax/skopax', check: int = 0) -> tuple[int, dict[str, str]]:
    stubs = tmp_path / 'bin'
    stubs.mkdir(parents=True)
    stub = stubs / 'python3'
    stub.write_text('#!/bin/sh\nif [ "$3" = check ]; then exit "$FAKE_CHECK"; fi\nprintf \'%s\\n\' $FAKE_TAGS\n', encoding='utf-8')
    stub.chmod(0o755)
    output = tmp_path / 'output'
    output.write_text('', encoding='utf-8')
    env = {'PATH': f'{stubs}:/usr/bin:/bin', 'GITHUB_REF_NAME': ref, 'GITHUB_REPOSITORY': repository, 'GITHUB_OUTPUT': str(output),
           'FAKE_TAGS': ' '.join(tags), 'FAKE_CHECK': str(check)}
    done = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], env=env, capture_output=True, text=True, check=False)
    return done.returncode, dict(line.split('=', 1) for line in output.read_text(encoding='utf-8').splitlines() if '=' in line)


def unpinned_actions(docs: dict[str, dict]) -> list[str]:
    found = []
    for name, doc in docs.items():
        found += [f'{name}: {s["uses"]}' for s in steps(doc) if 'uses' in s and not PINNED.fullmatch(s['uses'])]
        found += [f'{name}: {j["uses"]}' for j in doc.get('jobs', {}).values() if 'uses' in j and j['uses'] not in LOCAL_CALLS]
    return found


def unpinned_images(docs: dict[str, dict]) -> list[str]:
    found = []
    for name, doc in docs.items():
        for job, body in doc.get('jobs', {}).items():
            container = body.get('container')
            images = [container.get('image') if isinstance(container, dict) else container] if container else []
            images += [service.get('image') if isinstance(service, dict) else service for service in (body.get('services') or {}).values()]
            found += [f'{name} {job}: {image}' for image in images if not DIGEST.fullmatch(str(image))]
        for s in uses(doc, 'docker/setup-qemu-action'):
            if not DIGEST.fullmatch(str(s.get('with', {}).get('image', ''))):
                found.append(f'{name}: binfmt image {s.get("with", {}).get("image")!r}')
        for s in uses(doc, 'docker/setup-buildx-action'):
            if not re.fullmatch(r'v\d+\.\d+\.\d+', str((s.get('with') or {}).get('version', ''))):
                found.append(f'{name}: buildx version {(s.get("with") or {}).get("version")!r}')
    return found


def permission_problems(docs: dict[str, dict]) -> list[str]:
    problems = []
    for name, doc in docs.items():
        if doc.get('permissions') != {}:
            problems.append(f'{name}: top-level permissions are {doc.get("permissions")!r}')
        for job, body in doc.get('jobs', {}).items():
            if body.get('permissions') != PERMISSIONS.get((name, job)):
                problems.append(f'{name} {job}: {body.get("permissions")!r}')
    return problems


def fork_problems(ci: dict, ci_text: str, image: dict, image_text: str) -> list[str]:
    problems = []
    on = triggers(ci)
    if 'pull_request' not in on or 'workflow_call' not in on:
        problems.append('ci.yml does not run on pull_request and workflow_call')
    if set(on) - {'pull_request', 'push', 'workflow_call'}:
        problems.append(f'ci.yml runs on {sorted(set(on) - {"pull_request", "push", "workflow_call"})}')
    if on.get('workflow_call'):
        problems.append(f'ci.yml takes {sorted(on["workflow_call"])} from its caller')
    if (on.get('push') or {}).get('tags'):
        problems.append('ci.yml runs on tag pushes')
    if 'secrets.' in ci_text or 'pull_request_target' in ci_text:
        problems.append('ci.yml references secrets or pull_request_target')
    for action in ('docker/login-action', 'docker/build-push-action', 'actions/attest-build-provenance'):
        if uses(ci, action):
            problems.append(f'ci.yml uses {action}')
    if any(word in r for r in runs(ci) for word in ('docker push', 'imagetools create', 'docker login')):
        problems.append('ci.yml pushes images')
    if triggers(image) != {'push': {'tags': ['v*']}}:
        problems.append(f'image.yml triggers are {triggers(image)!r}')
    caller = image.get('jobs', {}).get('ci', {})
    if caller.get('uses') != './.github/workflows/ci.yml':
        problems.append('image.yml does not run ci.yml before building')
    if 'secrets' in caller or 'with' in caller or 'secrets: inherit' in image_text:
        problems.append('image.yml hands ci.yml secrets or inputs')
    return problems


def rc_problems(image: dict, image_text: str) -> list[str]:
    problems = []
    script = version_script(image)
    if 'tools.release_version tags "$GITHUB_REF_NAME"' not in script:
        problems.append('the tag list does not come from tools.release_version tags')
    if image_text.count('latest') != script.count('latest'):
        problems.append('image.yml spells latest outside the rc decision')
    outputs = image['jobs']['version'].get('outputs', {})
    if not {'tags', 'rc', 'version', 'image'} <= set(outputs):
        problems.append(f'version outputs are {sorted(outputs)}')
    builds = uses(image, 'docker/build-push-action', 'build')
    if len(builds) != 1 or builds[0].get('with', {}).get('tags') != f'{IMAGE}:{VERSION}':
        problems.append('the build step does not push only the exact version tag')
    for job, body in image['jobs'].items():
        for s in body.get('steps', []):
            text = json.dumps(s)
            if (HUB in text or 'DOCKERHUB' in text or s.get('with', {}).get('registry') == 'docker.io') and (s.get('if') != NOT_RC or job != 'promote'):
                problems.append(f'Docker Hub step outside promote or without the non-rc condition: {job} {s.get("name")}')
    hub = [s for s in steps(image, 'promote') if s.get('if') == NOT_RC]
    if not any(s.get('uses', '').startswith('docker/login-action@') and s.get('with', {}).get('registry') == 'docker.io' for s in hub):
        problems.append('no Docker Hub login behind the non-rc condition')
    if not any(f'{HUB}:$tag' in s.get('run', '') and 'imagetools create' in s.get('run', '') and '"$IMAGE@$DIGEST"' in s.get('run', '') for s in hub):
        problems.append('no Docker Hub copy of the GHCR digest behind the non-rc condition')
    moving = [r for r in runs(image, 'promote') if '"$IMAGE:$tag"' in r and '"$tag" != "$VERSION"' in r and 'imagetools create' in r and '"$IMAGE@$DIGEST"' in r]
    if len(moving) != 1:
        problems.append('promote does not add the moving GHCR tags from the digest')
    if image['jobs'].get('promote', {}).get('environment') != 'release':
        problems.append('promote does not run in the release environment')
    named = set(SECRET_REF.findall(image_text))
    in_promote = set(SECRET_REF.findall(json.dumps(image['jobs'].get('promote', {}))))
    if named != HUB_SECRETS or in_promote != HUB_SECRETS:
        problems.append(f'image.yml secrets are {sorted(named)}, promote holds {sorted(in_promote)}')
    return problems


def order_problems(image: dict) -> list[str]:
    problems = []
    jobs = image.get('jobs', {})
    if set(jobs) != IMAGE_JOBS:
        return [f'image.yml jobs are {sorted(jobs)}']
    script = version_script(image)
    check = script.find('tools.release_version check "$GITHUB_REF_NAME"')
    tags = script.find('tools.release_version tags')
    if check < 0 or tags < 0 or check > tags:
        problems.append('the version check does not run before the tag list')
    if steps(image, 'version') and steps(image, 'version')[0].get('uses', '').split('@')[0] != 'actions/checkout':
        problems.append('the version job does more than check out before the check')
    if any(s.get('uses', '').split('@')[0].startswith('docker/') for s in steps(image, 'version')):
        problems.append('the version job builds or logs in')
    for job, needed in NEEDS.items():
        if not needed <= needs_of(jobs[job]):
            problems.append(f'{job} does not need {sorted(needed - needs_of(jobs[job]))}')
    for job, body in jobs.items():
        if 'if' in body:
            problems.append(f'{job} has a job-level if: {body["if"]}')
        if any(f in str(body.get('if', '')) for f in STATUS_FUNCTIONS):
            problems.append(f'{job} runs whatever its needs did')
    return problems


def regex_literal(regex: str) -> str | None:
    match = LITERAL_REGEX.fullmatch(regex)
    return ''.join(REGEX_LITERAL.get(token, token) for token in REGEX_TOKEN.findall(match[1])) if match else None


def continue_on_error_problems(docs: dict[str, dict]) -> list[str]:
    problems = []
    for name, doc in docs.items():
        for job, body in doc.get('jobs', {}).items():
            if 'continue-on-error' in body:
                problems.append(f'{name} {job}: the job continues on error')
            problems += [f'{name} {job}: a step continues on error: {s.get("name") or s.get("uses") or s.get("run", "")[:40]}'
                         for s in body.get('steps', []) if 'continue-on-error' in s]
    return problems


def shell_problems(docs: dict[str, dict]) -> list[str]:
    problems = []
    for name, doc in docs.items():
        if doc.get('defaults') != {'run': {'shell': 'bash'}}:
            problems.append(f'{name}: defaults are {doc.get("defaults")!r}')
        for job, body in doc.get('jobs', {}).items():
            for s in body.get('steps', []):
                if '${{' in s.get('run', ''):
                    problems.append(f'{name} {job}: an expression inside run: {s.get("name") or s["run"][:40]}')
                if 'shell' in s:
                    problems.append(f'{name} {job}: a step picks its own shell')
    return problems


def tree_command_problems(ci: dict, texts: dict[str, str]) -> list[str]:
    problems = []
    jobs = ci.get('jobs', {})
    if set(jobs) != CI_JOBS:
        return [f'ci.yml jobs are {sorted(jobs)}']
    for job in ('tests', 'licences'):
        if not any(r.startswith('uv sync --frozen') for r in runs(ci, job)):
            problems.append(f'{job} does not run uv sync --frozen')
    tests_runs = runs(ci, 'tests')
    if 'uv lock --check' not in tests_runs or tests_runs.index('uv lock --check') > min((i for i, r in enumerate(tests_runs) if r.startswith('uv sync')), default=-1):
        problems.append('tests does not check the lock against pyproject.toml before the sync')
    if not any('uv run --frozen pytest' in r and '-n auto' in r for r in runs(ci, 'tests')):
        problems.append('tests does not run uv run --frozen pytest -n auto')
    if not any(s.get('run') == 'npm ci' and s.get('working-directory') == 'tests/page' for s in steps(ci, 'pages')):
        problems.append('pages does not run npm ci in tests/page')
    zones = sorted(s.get('env', {}).get('SKOPAX_TEST_TZ', '') for s in steps(ci, 'pages') if s.get('run') == PAGE_SUITES)
    if zones != ['Asia/Tokyo', 'UTC']:
        problems.append(f'page suites run in {zones}')
    if not any(f'-m tools.licences check --site {SITE} --out' in r for r in runs(ci, 'licences')):
        problems.append('licences does not check the synced venv')
    secrets_job = jobs['secrets']
    script = '\n'.join(runs(ci, 'secrets'))
    version = secrets_job.get('env', {}).get('GITLEAKS_VERSION', '')
    digest = secrets_job.get('env', {}).get('GITLEAKS_SHA256', '')
    if not re.fullmatch(r'\d+\.\d+\.\d+', str(version)) or not re.fullmatch(r'[0-9a-f]{64}', str(digest)):
        problems.append(f'gitleaks is not pinned: {version!r} {digest!r}')
    if 'sha256sum --check --strict' not in script or 'gitleaks" git --config .gitleaks.toml' not in script:
        problems.append('gitleaks is not checksum-verified or not run with the tree config')
    if script.find('sha256sum --check') > script.find('tar -xzf'):
        problems.append('gitleaks is unpacked before its checksum is checked')
    everything = '\n'.join(runs(ci))
    if len(re.findall(r'\b(?:curl|wget)\b', everything)) != 1 or len(re.findall(r'https?://', everything)) != 1:
        problems.append('ci.yml fetches more than the one gitleaks archive')
    for url in re.findall(r'https?://\S+', everything):
        if not url.strip('"\'').startswith(GITLEAKS_URL):
            problems.append(f'ci.yml fetches {url}')
    if PIPED_SHELL.search(everything):
        problems.append('ci.yml pipes a download into a shell')
    for s in steps(ci):
        where = s.get('working-directory', '')
        if where.startswith('/') or '..' in where:
            problems.append(f'ci.yml works in {where}')
    for name, text in texts.items():
        problems += [f'{name}: {word}' for word in OUTSIDE if word in text]
    return problems


def image_problems(image: dict) -> list[str]:
    problems = []
    build = image['jobs']['build']
    pushes = uses(image, 'docker/build-push-action', 'build')
    if len(pushes) != 1:
        return ['the build job needs exactly one build-push step']
    given = pushes[0].get('with', {})
    if given.get('platforms') != 'linux/amd64,linux/arm64':
        problems.append(f'platforms are {given.get("platforms")!r}')
    if given.get('push') is not True or given.get('sbom') is not True or given.get('provenance') != 'mode=max':
        problems.append('the build does not push with SBOM and max provenance')
    args = (given.get('build-args') or '').splitlines()
    if args != [f'SKOPAX_VERSION={VERSION}', 'COMMIT_ID=${{ github.sha }}']:
        problems.append(f'build args are {args}')
    if build.get('outputs', {}).get('digest') != '${{ steps.build.outputs.digest }}':
        problems.append('build does not hand on the pushed digest')
    for action in ('docker/setup-qemu-action', 'docker/setup-buildx-action'):
        if not 0 <= index_of(image, 'build', action) < index_of(image, 'build', 'docker/build-push-action'):
            problems.append(f'{action} does not run before the build')
    attest = uses(image, 'actions/attest-build-provenance', 'build')
    given = attest[0].get('with', {}) if attest else {}
    if given.get('subject-digest') != '${{ steps.build.outputs.digest }}' or given.get('subject-name') != IMAGE:
        problems.append('the pushed GHCR digest is not attested')
    if given.get('push-to-registry') is not True or given.get('create-storage-record') is not False:
        problems.append('the attestation is not pushed to GHCR or asks for a storage record without artifact-metadata')
    if attest and attest[0].get('if') != PUBLIC_ONLY:
        problems.append('the attestation also runs in a private repository')
    if 'arm64' not in json.dumps(uses(image, 'docker/setup-qemu-action', 'build')):
        problems.append('QEMU is not set up for arm64')
    smoke = image['jobs']['layer-smoke']
    if not str(smoke.get('services', {}).get('mongo', {}).get('image', '')).startswith('mongo:8.0.4@sha256:'):
        problems.append('layer-smoke does not run Mongo 8.0.4 as a service')
    if smoke.get('env', {}).get('SKOPAX_VERSION') != VERSION or smoke.get('env', {}).get('IMAGE') != IMAGE:
        problems.append('layer-smoke does not build on the pushed image and tag')
    script = '\n'.join(runs(image, 'layer-smoke'))
    for needle in ('docker build --build-arg BASE_IMAGE="$IMAGE" --build-arg SKOPAX_VERSION="$SKOPAX_VERSION"', 'examples/hello-plugin',
                   '/actuator/health/liveness', '/api/admin/plugins', 'p["hello"]', '== "ok"', '/plugins/hello/icon.svg', '[ "$icon" = 200 ]',
                   'import secrets; print(secrets.token_urlsafe(32))', '-e SKOPAX_TOKEN ', '-e SKOPAX_SECRET_KEY ', '$(seq 60)'):
        if needle not in script:
            problems.append(f'layer-smoke lacks {needle}')
    if any('--max-time' not in line for line in script.splitlines() if re.search(r'\bcurl\b', line)):
        problems.append('a layer-smoke curl has no --max-time')
    if SECRET_REF.search(json.dumps(smoke)):
        problems.append('layer-smoke uses a repository secret')
    admin = FIRST_ADMIN_PASSWORD.search(script)
    if '-e SKOPAX_ADMIN_EMAIL=admin@example.com ' not in script or '-e SKOPAX_ADMIN_PASSWORD ' not in script or not admin or not 9 <= int(admin[1]) <= 192:
        problems.append('layer-smoke does not create the first admin from a generated password')
    last = (steps(image, 'layer-smoke') or [{}])[-1]
    if last.get('if') != 'always()' or last.get('run', '').strip() != 'docker rm -f skopax-smoke':
        problems.append('layer-smoke does not remove its container at the end')
    promote = image['jobs'].get('promote', {})
    if promote.get('env', {}).get('IMAGE') != IMAGE or promote.get('env', {}).get('DIGEST') != '${{ needs.build.outputs.digest }}':
        problems.append('promote does not work from the image and digest that were smoked')
    return problems


def example_problems(dockerfile: str) -> list[str]:
    lines = [line.strip() for line in dockerfile.splitlines()]
    problems = []
    if 'ARG BASE_IMAGE=ghcr.io/skopax/skopax' not in lines:
        problems.append('the example layer has no BASE_IMAGE default')
    if not any(re.fullmatch(r'ARG SKOPAX_VERSION=\S+', line) for line in lines):
        problems.append('the example layer has no SKOPAX_VERSION default')
    if 'FROM ${BASE_IMAGE}:${SKOPAX_VERSION}' not in lines or any(line.startswith('FROM ghcr.io/') for line in lines):
        problems.append('the example layer does not build on BASE_IMAGE')
    return problems


def all_docs() -> dict[str, dict]:
    return {p.name: yaml.safe_load(p.read_text(encoding='utf-8')) for p in sorted(WORKFLOWS.glob('*.yml'))}


def all_texts() -> dict[str, str]:
    paths = sorted(WORKFLOWS.glob('*.yml')) + [ROOT / '.github' / 'dependabot.yml', ROOT / '.gitleaks.toml']
    return {str(p.relative_to(ROOT)): p.read_text(encoding='utf-8') for p in paths}


def test_every_action_is_pinned_by_sha():
    # given
    docs = all_docs()
    broken = copy.deepcopy(docs)
    broken['ci.yml']['jobs']['tests']['steps'][0]['uses'] = 'actions/checkout@v7'
    elsewhere = copy.deepcopy(docs)
    elsewhere['image.yml']['jobs']['ci']['uses'] = 'other/repo/.github/workflows/ci.yml@main'

    # when
    found = unpinned_actions(docs)

    # then
    assert set(docs) == {'ci.yml', 'image.yml'}
    assert sum(1 for doc in docs.values() for s in steps(doc) if 'uses' in s) >= 10
    assert found == []
    assert unpinned_actions(broken) == ['ci.yml: actions/checkout@v7']
    assert unpinned_actions(elsewhere) == ['image.yml: other/repo/.github/workflows/ci.yml@main']


def test_every_image_and_tool_the_workflows_pull_is_pinned():
    # given
    docs = all_docs()
    container = copy.deepcopy(docs)
    container['ci.yml']['jobs']['tests']['container'] = 'node:latest'
    service = copy.deepcopy(docs)
    service['image.yml']['jobs']['layer-smoke']['services']['mongo']['image'] = 'mongo:8.0.4'
    floating = copy.deepcopy(docs)
    for s in uses(floating['image.yml'], 'docker/setup-qemu-action') + uses(floating['image.yml'], 'docker/setup-buildx-action'):
        s['with'].pop('image', None)
        s['with'].pop('version', None)

    # when
    found = unpinned_images(docs)

    # then
    assert found == []
    assert unpinned_images(container) == ['ci.yml tests: node:latest']
    assert unpinned_images(service) == ['image.yml layer-smoke: mongo:8.0.4']
    assert set(unpinned_images(floating)) == {'image.yml: binfmt image None', 'image.yml: buildx version None'}


def test_permissions_are_empty_at_the_top_and_minimal_per_job():
    # given
    docs = all_docs()
    broken = copy.deepcopy(docs)
    broken['ci.yml']['jobs']['tests']['permissions']['packages'] = 'write'
    widened = copy.deepcopy(docs)
    widened['image.yml']['permissions'] = 'write-all'
    unbounded_call = copy.deepcopy(docs)
    del unbounded_call['image.yml']['jobs']['ci']['permissions']

    # when
    found = permission_problems(docs)

    # then
    assert found == []
    assert permission_problems(broken) == ["ci.yml tests: {'contents': 'read', 'packages': 'write'}"]
    assert permission_problems(widened) == ["image.yml: top-level permissions are 'write-all'"]
    assert permission_problems(unbounded_call) == ['image.yml ci: None']
    assert [job for (name, job), granted in PERMISSIONS.items() if 'id-token' in granted] == ['build']


def test_fork_pull_requests_get_no_secrets_and_publish_nothing():
    # given
    ci = load('ci.yml')
    ci_text = (WORKFLOWS / 'ci.yml').read_text(encoding='utf-8')
    image = load('image.yml')
    image_text = (WORKFLOWS / 'image.yml').read_text(encoding='utf-8')
    targeted = copy.deepcopy(ci)
    targeted[True]['pull_request_target'] = None
    on_branches = copy.deepcopy(image)
    on_branches[True]['push']['branches'] = ['main']
    inherited = copy.deepcopy(image)
    inherited['jobs']['ci']['secrets'] = 'inherit'
    asks = copy.deepcopy(ci)
    asks[True]['workflow_call'] = {'secrets': {'DOCKERHUB_TOKEN': {'required': True}}}

    # when
    found = fork_problems(ci, ci_text, image, image_text)

    # then
    assert found == []
    assert any('pull_request_target' in p for p in fork_problems(targeted, ci_text, image, image_text))
    assert fork_problems(ci, ci_text + '\n${{ secrets.DOCKERHUB_TOKEN }}', image, image_text) == ['ci.yml references secrets or pull_request_target']
    assert any('image.yml triggers' in p for p in fork_problems(ci, ci_text, on_branches, image_text))
    assert fork_problems(ci, ci_text, inherited, image_text) == ['image.yml hands ci.yml secrets or inputs']
    assert fork_problems(asks, ci_text, image, image_text) == ["ci.yml takes ['secrets'] from its caller"]
    assert all(s['with']['persist-credentials'] is False for s in uses(ci, 'actions/checkout') + uses(image, 'actions/checkout'))


def test_rc_tags_never_push_latest_or_docker_hub():
    # given
    image = load('image.yml')
    text = (WORKFLOWS / 'image.yml').read_text(encoding='utf-8')
    ungated = copy.deepcopy(image)
    for s in steps(ungated, 'promote'):
        if s.get('with', {}).get('registry') == 'docker.io':
            s['if'] = "needs.version.outputs.rc != 'true'"
    early = copy.deepcopy(image)
    uses(early, 'docker/build-push-action', 'build')[0]['with']['tags'] = '${{ steps.refs.outputs.all }}'
    retagged = copy.deepcopy(image)
    for s in steps(retagged, 'promote'):
        if s.get('name') == 'Add the moving tags on GHCR':
            s['run'] = s['run'].replace('"$IMAGE@$DIGEST"', '"$IMAGE:$VERSION"')

    # when
    found = rc_problems(image, text)

    # then
    assert found == []
    assert rc_problems(ungated, text) == ['Docker Hub step outside promote or without the non-rc condition: promote Log in to Docker Hub',
                                          'no Docker Hub login behind the non-rc condition']
    assert rc_problems(image, text + '\nlatest') == ['image.yml spells latest outside the rc decision']
    assert rc_problems(early, text) == ['the build step does not push only the exact version tag']
    assert rc_problems(retagged, text) == ['promote does not add the moving GHCR tags from the digest']


def test_the_version_step_derives_rc_image_and_tags_from_the_tool(tmp_path):
    # given
    script = version_script(load('image.yml'))

    # when
    rc = run_version_script(script, tmp_path / 'rc', 'v0.1.0-rc.1', ['0.1.0-rc.1'], repository='Skopax/Skopax-Trial')
    release = run_version_script(script, tmp_path / 'release', 'v0.1.0', ['0.1.0', '0.1', 'latest'])
    mismatch = run_version_script(script, tmp_path / 'mismatch', 'v9.9.9', ['9.9.9', '9.9', 'latest'], check=1)
    empty = run_version_script(script, tmp_path / 'empty', 'v0.1.0', [])

    # then
    assert rc == (0, {'tags': '0.1.0-rc.1', 'rc': 'true', 'version': '0.1.0-rc.1', 'image': 'ghcr.io/skopax/skopax-trial'})
    assert release == (0, {'tags': '0.1.0 0.1 latest', 'rc': 'false', 'version': '0.1.0', 'image': 'ghcr.io/skopax/skopax'})
    assert mismatch[0] != 0 and mismatch[1] == {}
    assert empty[0] != 0 and empty[1] == {}


def test_the_version_check_runs_before_any_build():
    # given
    image = load('image.yml')
    unordered = copy.deepcopy(image)
    unordered['jobs']['build']['needs'] = []
    swapped = copy.deepcopy(image)
    check = 'python3 -m tools.release_version check "$GITHUB_REF_NAME"\n'
    for s in steps(swapped, 'version'):
        if check in s.get('run', ''):
            s['run'] = s['run'].replace(check, '') + check
    rescued = copy.deepcopy(image)
    rescued['jobs']['build']['if'] = '${{ !cancelled() }}'
    unchecked = copy.deepcopy(image)
    unchecked['jobs']['version']['needs'] = []
    hasty = copy.deepcopy(image)
    hasty['jobs']['promote']['needs'] = ['version', 'build']

    # when
    found = order_problems(image)

    # then
    assert found == []
    assert order_problems(unordered) == ["build does not need ['version']"]
    assert 'the version check does not run before the tag list' in order_problems(swapped)
    assert order_problems(rescued) == ['build has a job-level if: ${{ !cancelled() }}', 'build runs whatever its needs did']
    assert order_problems(unchecked) == ["version does not need ['ci']"]
    assert order_problems(hasty) == ["promote does not need ['layer-smoke']"]


def test_no_gate_continues_on_error():
    # given
    docs = all_docs()
    smoke_step = copy.deepcopy(docs)
    for s in steps(smoke_step['image.yml'], 'layer-smoke'):
        if s.get('name') == 'Run the layer and check the hello plug-in':
            s['continue-on-error'] = True
    smoke_job = copy.deepcopy(docs)
    smoke_job['image.yml']['jobs']['layer-smoke']['continue-on-error'] = True
    version_job = copy.deepcopy(docs)
    version_job['image.yml']['jobs']['version']['continue-on-error'] = True
    version_step = copy.deepcopy(docs)
    for s in steps(version_step['image.yml'], 'version'):
        if s.get('id') == 'version':
            s['continue-on-error'] = True
    ci_job = copy.deepcopy(docs)
    ci_job['ci.yml']['jobs']['secrets']['continue-on-error'] = True
    ci_step = copy.deepcopy(docs)
    for s in steps(ci_step['ci.yml'], 'tests'):
        if 'pytest' in s.get('run', ''):
            s['continue-on-error'] = True

    # when
    found = continue_on_error_problems(docs)

    # then
    assert found == []
    assert continue_on_error_problems(smoke_step) == ['image.yml layer-smoke: a step continues on error: Run the layer and check the hello plug-in']
    assert continue_on_error_problems(smoke_job) == ['image.yml layer-smoke: the job continues on error']
    assert continue_on_error_problems(version_job) == ['image.yml version: the job continues on error']
    assert continue_on_error_problems(version_step) == ['image.yml version: a step continues on error: Check the tag against the package version']
    assert continue_on_error_problems(ci_job) == ['ci.yml secrets: the job continues on error']
    assert continue_on_error_problems(ci_step) == ['ci.yml tests: a step continues on error: uv run --frozen pytest -q -n auto']


def test_scripts_run_in_bash_with_pipefail_and_take_no_expressions():
    # given
    docs = all_docs()
    plain = copy.deepcopy(docs)
    del plain['image.yml']['defaults']
    injected = copy.deepcopy(docs)
    injected['ci.yml']['jobs']['tests']['steps'].append({'name': 'title', 'run': 'echo "${{ github.event.pull_request.title }}"'})

    # when
    found = shell_problems(docs)

    # then
    assert found == []
    assert shell_problems(plain) == ['image.yml: defaults are None']
    assert shell_problems(injected) == ['ci.yml tests: an expression inside run: title']


def test_ci_cancels_only_superseded_pull_request_runs():
    # given
    ci = load('ci.yml')

    # when
    concurrency = ci.get('concurrency')

    # then
    assert concurrency == {'group': 'ci-${{ github.ref }}', 'cancel-in-progress': "${{ github.event_name == 'pull_request' }}"}


def test_ci_runs_only_tree_commands():
    # given
    ci = load('ci.yml')
    texts = all_texts()
    leaky = copy.deepcopy(ci)
    leaky['jobs']['tests']['steps'].append({'run': 'LIST=/Users/x/list curl -fsSL https://example.com/x.sh'})
    piped = copy.deepcopy(ci)
    piped['jobs']['secrets']['steps'].append({'run': 'curl -fsSL https://github.com/gitleaks/gitleaks/releases/download/v1/install.sh | sh'})
    unlocked = copy.deepcopy(ci)
    unlocked['jobs']['tests']['steps'] = [s for s in steps(unlocked, 'tests') if s.get('run') != 'uv lock --check']
    late = copy.deepcopy(ci)
    late['jobs']['tests']['steps'].sort(key=lambda s: s.get('run') == 'uv lock --check')

    # when
    found = tree_command_problems(ci, texts)

    # then
    assert found == []
    assert tree_command_problems(leaky, texts | {'leak': 'LIST=/Users/x /home/y'}) == [
        'ci.yml fetches more than the one gitleaks archive', 'ci.yml fetches https://example.com/x.sh', 'leak: /Users/', 'leak: /home/']
    assert tree_command_problems(piped, texts) == ['ci.yml fetches more than the one gitleaks archive', 'ci.yml pipes a download into a shell']
    assert tree_command_problems(unlocked, texts) == ['tests does not check the lock against pyproject.toml before the sync']
    assert tree_command_problems(late, texts) == ['tests does not check the lock against pyproject.toml before the sync']
    assert OUTSIDE == ('/Users/', '/home/', '~/', '$HOME')


def test_ci_uses_the_tool_versions_the_tree_pins():
    # given
    ci = load('ci.yml')
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    engines = json.loads((ROOT / 'tests' / 'page' / 'package.json').read_text(encoding='utf-8')).get('engines', {})
    jsdom = json.loads((ROOT / 'tests' / 'page' / 'package-lock.json').read_text(encoding='utf-8'))['packages']['node_modules/jsdom']['engines']['node']

    # when
    uv_pin = re.search(r'ghcr\.io/astral-sh/uv:(\d+\.\d+\.\d+)', dockerfile)[1]
    uv_versions = {s['with']['version'] for s in uses(ci, 'astral-sh/setup-uv')}
    node_versions = {s['with']['node-version'] for s in uses(ci, 'actions/setup-node')}
    pythons = {r for r in runs(ci) if r.startswith('uv python install')}
    floor = re.search(r'\^22\.(\d+)\.\d+', jsdom)

    # then
    assert uv_versions == {uv_pin}
    assert floor and node_versions == {engines.get('node', '22.' + floor[1])}
    assert pythons == {'uv python install 3.15'}
    assert {j for j in ci['jobs'] if uses(ci, 'astral-sh/setup-uv', j)} == {'tests', 'licences'}
    assert {j for j in ci['jobs'] if uses(ci, 'actions/setup-node', j)} == {'tests', 'pages'}


def test_the_image_job_builds_amd64_and_arm64_with_labels_and_runs_the_layer_smoke():
    # given
    image = load('image.yml')
    single = copy.deepcopy(image)
    uses(single, 'docker/build-push-action', 'build')[0]['with']['platforms'] = 'linux/amd64'
    seeded = copy.deepcopy(image)
    seeded['jobs']['layer-smoke']['env']['SKOPAX_TOKEN'] = '${{ secrets.SKOPAX_TOKEN }}'
    handed = copy.deepcopy(image)
    for s in steps(handed, 'layer-smoke'):
        if 'SKOPAX_ADMIN_PASSWORD=' in s.get('run', ''):
            s['run'] = FIRST_ADMIN_PASSWORD.sub('SKOPAX_ADMIN_PASSWORD="${{ secrets.FIRST_ADMIN }}"', s['run'])
    private = copy.deepcopy(image)
    for s in uses(private, 'actions/attest-build-provenance', 'build'):
        s.pop('if', None)
    patient = copy.deepcopy(image)
    for s in steps(patient, 'layer-smoke'):
        if 'run' in s:
            s['run'] = s['run'].replace('curl --max-time 10 ', 'curl ')
    lingering = copy.deepcopy(image)
    lingering['jobs']['layer-smoke']['steps'] = [s for s in steps(lingering, 'layer-smoke') if 'docker rm' not in s.get('run', '')]

    # when
    found = image_problems(image)

    # then
    assert found == []
    assert image_problems(single) == ["platforms are 'linux/amd64'"]
    assert image_problems(seeded) == ['layer-smoke uses a repository secret']
    assert image_problems(handed) == ['layer-smoke uses a repository secret', 'layer-smoke does not create the first admin from a generated password']
    assert image_problems(private) == ['the attestation also runs in a private repository']
    assert image_problems(patient) == ['a layer-smoke curl has no --max-time']
    assert image_problems(lingering) == ['layer-smoke does not remove its container at the end']


def test_the_example_layer_builds_on_the_named_image_at_the_package_version():
    # given
    dockerfile = EXAMPLE_DOCKERFILE.read_text(encoding='utf-8')
    package = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']

    # when
    found = example_problems(dockerfile)
    default = re.search(r'^ARG SKOPAX_VERSION=(\S+)$', dockerfile, re.M)

    # then
    assert tag_form('0.1.0rc1') == '0.1.0-rc.1'
    assert tag_form('0.1.0') == '0.1.0'
    assert found == []
    assert example_problems(dockerfile.replace('FROM ${BASE_IMAGE}:${SKOPAX_VERSION}', 'FROM ghcr.io/skopax/skopax:${SKOPAX_VERSION}')) == [
        'the example layer does not build on BASE_IMAGE']
    assert default and default[1] == tag_form(package)
    main_default = re.search(r'^ARG SKOPAX_VERSION=(\S+)$', (ROOT / 'Dockerfile').read_text(encoding='utf-8'), re.M)
    assert main_default and main_default[1] == tag_form(package)


def test_dependabot_watches_actions_uv_docker_and_npm_weekly():
    # given
    config = yaml.safe_load((ROOT / '.github' / 'dependabot.yml').read_text(encoding='utf-8'))

    # when
    found = {u['package-ecosystem']: u for u in config['updates']}

    # then
    assert config['version'] == 2
    assert set(found) == {'github-actions', 'uv', 'docker', 'npm'}
    assert {u['schedule']['interval'] for u in found.values()} == {'weekly'}
    assert found['docker']['directories'] == ['/', '/examples/hello-plugin']
    assert found['docker']['groups'] == {'docker': {'group-by': 'dependency-name'}}
    assert found['npm']['directory'] == '/tests/page'
    assert {u['cooldown']['default-days'] for u in found.values()} == {14}


def test_gitleaks_extends_the_default_rules_and_allowlists_only_named_fixture_values():
    # given
    config = tomllib.loads((ROOT / '.gitleaks.toml').read_text(encoding='utf-8'))

    # when
    bytecode = [e for e in config.get('allowlists', []) if e.get('paths') == [BYTECODE_PATH]]
    entries = [e for e in config.get('allowlists', []) if e not in bytecode]
    tracked = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines() if (ROOT / '.git').exists() else None
    named = {e['paths'][0][1:-1].replace('\\.', '.'): e.get('targetRules') for e in entries}
    literals = {e['paths'][0][1:-1].replace('\\.', '.'): [regex_literal(r) for r in e.get('regexes', [])] for e in entries}
    prefix_only = regex_literal('MIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEA')

    # then
    assert config['extend'] == {'useDefault': True}
    assert set(config) == {'title', 'extend', 'allowlists'}
    assert all(set(e) == {'description', 'condition', 'targetRules', 'paths', 'regexes'} and e['condition'] == 'AND' and e['description'].strip() for e in entries)
    assert all(len(e['paths']) == 1 and re.fullmatch(r'\^(?:[A-Za-z0-9_/-]|\\\.)+\$', e['paths'][0]) for e in entries)
    assert len(entries) == len(named) == len(GITLEAKS_FIXTURES)
    assert named == {path: [rule] for path, rule in GITLEAKS_FIXTURES.items()}
    assert prefix_only is None
    assert all(found and all(found) for found in literals.values())
    assert all(literal in (ROOT / rel).read_text(encoding='utf-8') for rel, found in literals.items() for literal in found)
    assert len(bytecode) == 1 and set(bytecode[0]) == {'description', 'targetRules', 'paths'} and bytecode[0]['description'].strip()
    assert sorted(bytecode[0]['targetRules']) == BYTECODE_RULES == sorted(set(GITLEAKS_FIXTURES.values()))
    assert tracked is None or (tracked and not [path for path in tracked if re.search(BYTECODE_PATH, path) or '__pycache__' in path])
    assert re.search(BYTECODE_PATH, 'tests/__pycache__/test_instances.cpython-315-pytest-8.4.2.pyc') and not re.search(BYTECODE_PATH, 'tests/test_instances.py')
    assert re.search(BYTECODE_PATH, 'tests/plugins/__pycache__/base.cpython-315.pyc')
    assert not [path for path in ('skopax/__pycache__/app.cpython-315.pyc', 'tools/__pycache__/licences.cpython-315.pyc', 'x/tests/__pycache__/a.pyc') if re.search(BYTECODE_PATH, path)]
    assert literals['tests/test_instances.py'] == [re.search(r"key_block = '([^']+)\\n'", (ROOT / 'tests' / 'test_instances.py').read_text(encoding='utf-8'))[1]]


def test_workflow_files_carry_no_comments_and_are_ascii():
    # given
    texts = all_texts()

    # when
    commented = {name: yaml_comment_lines(text) for name, text in texts.items() if name.endswith('.yml') and yaml_comment_lines(text)}
    hashed = [name for name, text in texts.items() if name.endswith('.toml') and any(line.lstrip().startswith('#') for line in text.splitlines())]
    odd = [name for name, text in texts.items() if not text.isascii()]

    # then
    assert commented == {}
    assert hashed == []
    assert odd == []
