import copy
import datetime
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.yaml_kit import yaml_comment_lines
from tools import release_version

ROOT = Path(__file__).resolve().parent.parent
DCO_CHECK = ROOT / 'tools' / 'dco_check.py'
PR_EVENTS = ['opened', 'edited', 'synchronize', 'reopened', 'ready_for_review']
DEPENDABOT_EMAIL = '49699333+dependabot[bot]@users.noreply.github.com'
WORKFLOWS = ROOT / '.github' / 'workflows'
EXAMPLE_DOCKERFILE = ROOT / 'examples' / 'hello-plugin' / 'Dockerfile'
EXAMPLE_DOCKERFILES = [EXAMPLE_DOCKERFILE] + [ROOT / 'examples' / name / 'Dockerfile' for name in ('couchbase-plugin', 'managed-redis-plugin', 'batch-jobs-plugin')]
PINNED = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}')
DIGEST = re.compile(r'[^\s@]+@sha256:[0-9a-f]{64}')
LOCAL_CALLS = {'./.github/workflows/ci.yml'}
READ_ONLY = {'contents': 'read'}
PERMISSIONS = {
    **{('ci.yml', job): READ_ONLY for job in ('tests', 'pages', 'licences', 'secrets')},
    ('pr.yml', 'title'): READ_ONLY,
    ('pr.yml', 'dco'): READ_ONLY,
    ('image.yml', 'ci'): READ_ONLY,
    ('image.yml', 'version'): READ_ONLY,
    ('image.yml', 'build'): {'contents': 'read', 'packages': 'write', 'id-token': 'write', 'attestations': 'write'},
    ('image.yml', 'layer-smoke'): {'contents': 'read', 'packages': 'read'},
    ('image.yml', 'scan'): {'contents': 'read', 'packages': 'read', 'security-events': 'write'},
    ('scan.yml', 'scan'): {'contents': 'read', 'packages': 'read', 'security-events': 'write', 'issues': 'write'},
    ('image.yml', 'promote'): {'contents': 'read', 'packages': 'write'},
}
CI_JOBS = {'tests', 'pages', 'licences', 'secrets'}
IMAGE_JOBS = {'ci', 'version', 'build', 'layer-smoke', 'scan', 'promote'}
NEEDS = {'version': {'ci'}, 'build': {'version'}, 'layer-smoke': {'version', 'build'}, 'scan': {'version', 'build'},
         'promote': {'version', 'build', 'layer-smoke', 'scan'}}
SCAN_ACTION = 'anchore/scan-action@27805bf3b4e84b4a5c980df22ed233c00390a439'
SCAN_ACTION_RELEASE = 'v7.4.2'
GRYPE_VERSION = 'v0.121.1'
UPLOAD_ACTION = 'github/codeql-action/upload-sarif@1fc58d04a4d4374f921bdac573c3811c240f9c26'
SCAN_ISSUE_TITLE = 'Image scan: fixable findings in latest'
SCAN_IMAGE = 'ghcr.io/skopax/skopax:latest'
GRYPE_IGNORE = ROOT / '.grype.yaml'
IGNORE_KEYS = {'vulnerability', 'package', 'reason', 'fix-state', 'namespace', 'vex-status', 'vex-justification', 'match-type'}
SCAN_DIGEST = '${{ needs.build.outputs.digest }}'
STATUS_FUNCTIONS = ('always()', 'cancelled()', 'failure()', 'success()')
NOT_RC = "needs.version.outputs.rc == 'false'"
HUB_IF = NOT_RC + " && env.HUB_USER != ''"
HUB_SKIP = NOT_RC + " && env.HUB_USER == ''"
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
    'tests/page/skopax-access-page.test.js': 'generic-api-key',
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
            if (HUB in text or 'DOCKERHUB' in text or s.get('with', {}).get('registry') == 'docker.io') and (s.get('if') != HUB_IF or job != 'promote'):
                problems.append(f'Docker Hub step outside promote or without the non-rc condition: {job} {s.get("name")}')
    if image['jobs']['promote'].get('env', {}).get('HUB_USER') != '${{ secrets.DOCKERHUB_USERNAME }}':
        problems.append('promote does not read HUB_USER from the Docker Hub username secret')
    if not any(s.get('if') == HUB_SKIP and '::notice' in s.get('run', '') for s in steps(image, 'promote')):
        problems.append('no notice when the Docker Hub mirror is skipped')
    hub = [s for s in steps(image, 'promote') if s.get('if') == HUB_IF]
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
                   'import secrets; print(secrets.token_urlsafe(32))', '-e SKOPAX_TOKEN ', '-e SKOPAX_SECRET_KEY ', '$(seq 60)',
                   '--build-arg BASE_IMAGE=skopax-hello --build-arg SKOPAX_VERSION=smoke -t skopax-couchbase:smoke examples/couchbase-plugin',
                   '--build-arg BASE_IMAGE=skopax-couchbase --build-arg SKOPAX_VERSION=smoke -t skopax-managed-redis:smoke examples/managed-redis-plugin',
                   '--build-arg BASE_IMAGE=skopax-managed-redis --build-arg SKOPAX_VERSION=smoke -t skopax-examples:smoke examples/batch-jobs-plugin',
                   '-e SKOPAX_SECRET_KEY skopax-examples:smoke', 'for i in ("couchbase", "managed-redis", "batch-jobs")', 'for plugin in couchbase managed-redis batch-jobs; do',
                   '/plugins/$plugin/icon.svg'):
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


def run_title_script(script: str, title: str) -> tuple[int, str]:
    done = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], env={'PATH': '/usr/bin:/bin', 'TITLE': title}, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    base = {'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin', 'HOME': str(repo), 'GIT_CONFIG_NOSYSTEM': '1'}
    return subprocess.run(['git', *args], cwd=repo, env=base | (env or {}), capture_output=True, text=True, check=True).stdout.strip()


def commit(repo: Path, subject: str, name: str, email: str, sign_off: str | None) -> None:
    message = subject if sign_off is None else f'{subject}\n\nSigned-off-by: {sign_off}'
    (repo / 'file.txt').write_text(message, encoding='utf-8')
    git(repo, 'add', 'file.txt')
    git(repo, 'commit', '-m', message, env={'GIT_AUTHOR_NAME': name, 'GIT_AUTHOR_EMAIL': email, 'GIT_COMMITTER_NAME': name, 'GIT_COMMITTER_EMAIL': email})


def dco_result(tmp_path: Path, name: str, email: str, sign_off: str | None) -> tuple[int, str]:
    repo = tmp_path / 'repo'
    repo.mkdir(parents=True)
    git(repo, 'init', '-q', '-b', 'main')
    commit(repo, 'base', 'Base Person', 'base@example.com', 'Base Person <base@example.com>')
    base = git(repo, 'rev-parse', 'HEAD')
    commit(repo, 'change the file', name, email, sign_off)
    head = git(repo, 'rev-parse', 'HEAD')
    done = subprocess.run([sys.executable, str(DCO_CHECK), base, head], cwd=repo, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


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
    assert set(docs) == {'ci.yml', 'image.yml', 'pr.yml', 'scan.yml'}
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
    hasty['jobs']['promote']['needs'] = ['version', 'build', 'scan']
    unscanned = copy.deepcopy(image)
    unscanned['jobs']['promote']['needs'] = ['version', 'build', 'layer-smoke']

    # when
    found = order_problems(image)

    # then
    assert found == []
    assert order_problems(unordered) == ["build does not need ['version']"]
    assert 'the version check does not run before the tag list' in order_problems(swapped)
    assert order_problems(rescued) == ['build has a job-level if: ${{ !cancelled() }}', 'build runs whatever its needs did']
    assert order_problems(unchecked) == ["version does not need ['ci']"]
    assert order_problems(hasty) == ["promote does not need ['layer-smoke']"]
    assert order_problems(unscanned) == ["promote does not need ['scan']"]


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


@pytest.mark.parametrize('path', EXAMPLE_DOCKERFILES, ids=lambda path: path.parent.name)
def test_the_example_layer_builds_on_the_named_image_at_the_package_version(path):
    # given
    dockerfile = path.read_text(encoding='utf-8')
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
    assert found['docker']['directories'] == ['/', '/examples/hello-plugin', '/examples/couchbase-plugin', '/examples/managed-redis-plugin', '/examples/batch-jobs-plugin']
    assert found['docker']['groups'] == {'docker': {'group-by': 'dependency-name'}}
    assert found['npm']['directory'] == '/tests/page'
    assert {e: u['cooldown']['default-days'] for e, u in found.items() if e != 'docker'} == {'github-actions': 14, 'uv': 14, 'npm': 14}
    assert 'cooldown' not in found['docker']
    assert found['uv']['allow'] == [{'dependency-type': 'direct'}]
    assert {u['commit-message']['prefix'] for u in found.values()} == {'chore(deps)'}


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


def test_the_pr_workflow_runs_on_pull_request_only_with_read_access_and_no_secrets():
    # given
    pr = load('pr.yml')
    text = (WORKFLOWS / 'pr.yml').read_text(encoding='utf-8')
    targeted = copy.deepcopy(pr)
    targeted[True]['pull_request_target'] = None

    # when
    on = triggers(pr)

    # then
    assert set(on) == {'pull_request'}
    assert on['pull_request'] == {'types': PR_EVENTS}
    assert set(triggers(targeted)) == {'pull_request', 'pull_request_target'}
    assert 'pull_request_target' not in text and 'secrets.' not in text and 'secrets:' not in text
    assert pr['permissions'] == {}
    assert set(pr['jobs']) == {'title', 'dco'}
    assert all(job['permissions'] == READ_ONLY and job['runs-on'] == 'ubuntu-24.04' and 0 < job['timeout-minutes'] <= 30 for job in pr['jobs'].values())
    assert all(s['with']['persist-credentials'] is False for s in uses(pr, 'actions/checkout'))
    assert [s['with']['fetch-depth'] for s in uses(pr, 'actions/checkout', 'dco')] == [0]
    assert uses(pr, 'actions/checkout', 'title') == []


def test_the_pr_workflow_reads_the_title_and_the_shas_only_through_env():
    # given
    pr = load('pr.yml')
    title = steps(pr, 'title')
    dco = steps(pr, 'dco')

    # when
    exposed = [r for r in runs(pr) if '${{' in r or 'github.event' in r]

    # then
    assert exposed == []
    assert [s['env'] for s in title if 'run' in s] == [{'TITLE': '${{ github.event.pull_request.title }}'}]
    assert [s['env'] for s in dco if 'run' in s] == [{'BASE_SHA': '${{ github.event.pull_request.base.sha }}', 'HEAD_SHA': '${{ github.event.pull_request.head.sha }}'}]
    assert any('"$BASE_SHA" "$HEAD_SHA"' in r and 'tools/dco_check.py' in r for r in runs(pr, 'dco'))


def test_the_title_check_accepts_conventional_commit_titles_and_rejects_the_rest():
    # given
    script = '\n'.join(runs(load('pr.yml'), 'title'))
    good = ['feat: x', 'fix(page): x', 'feat!: x', 'chore(deps): bump a from 1 to 2', 'refactor(a.b/c_d-e)!: rename']
    bad = ['Fix bug', 'feat:x', 'feature: x', 'feat(Bad Scope): x', 'feat(): x', 'feat: ', 'feat', 'Feat: x', '']

    # when
    accepted = {title: run_title_script(script, title) for title in good}
    rejected = {title: run_title_script(script, title) for title in bad}

    # then
    assert all(code == 0 for code, out in accepted.values())
    assert all(code == 1 and out.count('\n') == 1 and 'type: summary' in out for code, out in rejected.values())
    assert '$TITLE' in script and '${{' not in script


def test_the_dco_check_passes_a_commit_signed_off_by_its_author(tmp_path):
    # given
    name, email = 'Alice Example', 'alice@example.com'

    # when
    code, out = dco_result(tmp_path, name, email, f'{name} <{email}>')

    # then
    assert (code, out) == (0, '')


def test_the_dco_check_compares_the_sign_off_email_without_case(tmp_path):
    # given
    name, email = 'Alice Example', 'Alice@Example.com'

    # when
    code, out = dco_result(tmp_path, name, email, 'Alice Example <alice@example.COM>')

    # then
    assert (code, out) == (0, '')


def test_the_dco_check_fails_an_unsigned_commit_and_names_it_and_the_fix(tmp_path):
    # given
    name, email = 'Alice Example', 'alice@example.com'

    # when
    code, out = dco_result(tmp_path, name, email, None)

    # then
    assert code == 1
    assert re.search(r'^  [0-9a-f]{7} change the file$', out, re.M)
    assert 'git commit --amend -s' in out and 'git rebase --signoff' in out


def test_the_dco_check_fails_a_sign_off_with_another_email(tmp_path):
    # given
    name, email = 'Alice Example', 'alice@example.com'

    # when
    code, out = dco_result(tmp_path, name, email, 'Alice Example <bob@example.com>')

    # then
    assert code == 1
    assert re.search(r'^  [0-9a-f]{7} change the file$', out, re.M)


def test_the_dco_check_exempts_dependabot_and_github_actions_commits(tmp_path):
    # given
    bots = {'dependabot': ('dependabot[bot]', DEPENDABOT_EMAIL), 'by_name': ('dependabot[bot]', 'other@example.com'),
            'actions': ('github-actions[bot]', '41898282+github-actions[bot]@users.noreply.github.com')}

    # when
    results = {key: dco_result(tmp_path / key, name, email, None) for key, (name, email) in bots.items()}

    # then
    assert results == {key: (0, '') for key in bots}


def test_the_dco_check_lists_only_the_failing_commits_of_a_range(tmp_path):
    # given
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    commit(repo, 'base', 'Base Person', 'base@example.com', None)
    base = git(repo, 'rev-parse', 'HEAD')
    commit(repo, 'good one', 'Alice Example', 'alice@example.com', 'Alice Example <alice@example.com>')
    commit(repo, 'bad one', 'Alice Example', 'alice@example.com', None)
    commit(repo, 'good two', 'Alice Example', 'alice@example.com', 'Alice Example <alice@example.com>')
    head = git(repo, 'rev-parse', 'HEAD')

    # when
    done = subprocess.run([sys.executable, str(DCO_CHECK), base, head], cwd=repo, capture_output=True, text=True, check=False)

    # then
    assert done.returncode == 1
    assert [line.strip().split(' ', 1)[1] for line in done.stdout.splitlines() if line.startswith('  ')] == ['bad one']


def scan_step_problems(doc: dict, job: str, fail_build: bool) -> list[str]:
    problems = []
    scans = uses(doc, 'anchore/scan-action', job)
    if len(scans) != 1 or scans[0]['uses'] != SCAN_ACTION:
        return [f'{job} does not run the pinned Grype action once']
    given = scans[0].get('with', {})
    if not re.fullmatch(r'v\d+\.\d+\.\d+', str(given.get('grype-version', ''))) or given.get('grype-version') != GRYPE_VERSION:
        problems.append('Grype is not pinned to the chosen version')
    if str(given.get('only-fixed')).lower() != 'true':
        problems.append('the scan does not skip findings without a fix')
    if given.get('severity-cutoff') != 'high':
        problems.append(f'the severity cutoff is {given.get("severity-cutoff")!r}')
    if given.get('output-format') != 'sarif':
        problems.append('the scan does not write SARIF')
    if str(given.get('fail-build', 'true')).lower() != str(fail_build).lower():
        problems.append(f'fail-build is {given.get("fail-build")!r}')
    if 'config' in given:
        problems.append('the scan names its own config, which turns off .grype.yaml')
    if scans[0].get('id') != 'grype':
        problems.append('the scan step has no id grype')
    uploads = uses(doc, 'github/codeql-action/upload-sarif', job)
    if len(uploads) != 1 or uploads[0]['uses'] != UPLOAD_ACTION:
        problems.append('the SARIF upload is missing or not pinned')
    else:
        if uploads[0].get('with', {}).get('sarif_file') != '${{ steps.grype.outputs.sarif }}':
            problems.append('the upload does not take the scan SARIF')
        if 'always()' not in str(uploads[0].get('if', '')):
            problems.append('the SARIF upload is skipped when the scan fails')
    if index_of(doc, job, 'actions/checkout') < 0 or index_of(doc, job, 'actions/checkout') > index_of(doc, job, 'anchore/scan-action'):
        problems.append('the scan does not check out the tree for .grype.yaml first')
    if index_of(doc, job, 'docker/login-action') < 0 or index_of(doc, job, 'docker/login-action') > index_of(doc, job, 'anchore/scan-action'):
        problems.append('the scan does not log in to GHCR first')
    return problems


def release_scan_problems(image: dict) -> list[str]:
    problems = scan_step_problems(image, 'scan', True)
    scan = image['jobs']['scan']
    scans = uses(image, 'anchore/scan-action', 'scan')
    if scans and scans[0].get('with', {}).get('image') != f'{IMAGE}@{SCAN_DIGEST}':
        problems.append('the scan does not take the digest that promote copies')
    if needs_of(scan) != {'version', 'build'}:
        problems.append(f'scan needs {sorted(needs_of(scan))}')
    wide = [job for job, body in image['jobs'].items() if 'security-events' in (body.get('permissions') or {})]
    if wide != ['scan']:
        problems.append(f'security-events is granted to {wide}')
    if image['jobs']['promote'].get('env', {}).get('DIGEST') != SCAN_DIGEST:
        problems.append('promote does not copy the build digest')
    return problems


def schedule_problems(doc: dict, text: str) -> list[str]:
    problems = []
    on = triggers(doc)
    crons = [item.get('cron') for item in on.get('schedule', [])]
    if len(crons) != 1 or len(str(crons[0]).split()) != 5:
        problems.append(f'schedule is {crons!r}')
    elif str(crons[0]).split()[4] in ('*', ''):
        problems.append('the scan runs more than weekly')
    if set(on) - {'schedule', 'workflow_dispatch'}:
        problems.append(f'scan.yml runs on {sorted(set(on) - {"schedule", "workflow_dispatch"})}')
    problems += scan_step_problems(doc, 'scan', False)
    scans = uses(doc, 'anchore/scan-action', 'scan')
    if scans and scans[0].get('with', {}).get('image') != SCAN_IMAGE:
        problems.append('the scan does not take the published image')
    if SCAN_ISSUE_TITLE not in text:
        problems.append('the issue title is not the agreed one')
    if SECRET_REF.search(text.replace('secrets.GITHUB_TOKEN', '')):
        problems.append('scan.yml names a repository secret')
    script = '\n'.join(runs(doc, 'scan'))
    issue = [s for s in steps(doc, 'scan') if 'gh issue' in s.get('run', '')]
    if len(issue) != 1 or issue[0].get('if') != "always() && steps.grype.outputs.sarif != ''":
        problems.append('the issue step is skipped when the upload fails')
    expiry = [s for s in steps(doc, 'scan') if '.grype.yaml' in s.get('run', '')]
    if len(expiry) != 1 or expiry[0].get('if') != 'always()':
        problems.append('no always-run check of the ignore review dates')
    for needle in ('gh issue create', 'gh issue edit', 'gh issue close'):
        if needle not in script:
            problems.append(f'scan.yml lacks {needle}')
    return problems


def test_the_release_scan_gates_the_promoted_digest_and_uploads_sarif():
    # given
    image = load('image.yml')
    pins = {SCAN_ACTION_RELEASE: SCAN_ACTION.split('@')[1]}
    floating = copy.deepcopy(image)
    uses(floating, 'anchore/scan-action', 'scan')[0]['uses'] = 'anchore/scan-action@v7'
    floating_grype = copy.deepcopy(image)
    uses(floating_grype, 'anchore/scan-action', 'scan')[0]['with'].pop('grype-version')
    unfixed = copy.deepcopy(image)
    uses(unfixed, 'anchore/scan-action', 'scan')[0]['with']['only-fixed'] = False
    lenient = copy.deepcopy(image)
    uses(lenient, 'anchore/scan-action', 'scan')[0]['with']['severity-cutoff'] = 'critical'
    soft = copy.deepcopy(image)
    uses(soft, 'anchore/scan-action', 'scan')[0]['with']['fail-build'] = False
    tagged = copy.deepcopy(image)
    uses(tagged, 'anchore/scan-action', 'scan')[0]['with']['image'] = f'{IMAGE}:{VERSION}'
    silent = copy.deepcopy(image)
    silent['jobs']['scan']['steps'] = [s for s in steps(silent, 'scan') if 'upload-sarif' not in s.get('uses', '')]
    blind = copy.deepcopy(image)
    uses(blind, 'github/codeql-action/upload-sarif', 'scan')[0].pop('if')
    broad = copy.deepcopy(image)
    broad['jobs']['promote']['permissions']['security-events'] = 'write'
    open_scan = copy.deepcopy(image)
    open_scan['jobs']['promote']['needs'] = ['version', 'build', 'layer-smoke']

    # when
    found = release_scan_problems(image)

    # then
    assert pins == {'v7.4.2': '27805bf3b4e84b4a5c980df22ed233c00390a439'}
    assert found == []
    assert order_problems(image) == []
    assert permission_problems({'image.yml': image}) == []
    assert release_scan_problems(floating) == ['scan does not run the pinned Grype action once']
    assert release_scan_problems(floating_grype) == ['Grype is not pinned to the chosen version']
    assert release_scan_problems(unfixed) == ['the scan does not skip findings without a fix']
    assert release_scan_problems(lenient) == ["the severity cutoff is 'critical'"]
    assert release_scan_problems(soft) == ['fail-build is False']
    assert release_scan_problems(tagged) == ['the scan does not take the digest that promote copies']
    assert release_scan_problems(silent) == ['the SARIF upload is missing or not pinned']
    assert release_scan_problems(blind) == ['the SARIF upload is skipped when the scan fails']
    assert release_scan_problems(broad) == ["security-events is granted to ['scan', 'promote']"]
    assert order_problems(open_scan) == ["promote does not need ['scan']"]
    assert any(s.get('with', {}).get('registry') == 'ghcr.io' for s in uses(image, 'docker/login-action', 'scan'))
    assert all(s['with']['persist-credentials'] is False for s in uses(image, 'actions/checkout', 'scan'))


def test_the_weekly_scan_reads_the_published_image_and_never_fails_the_schedule():
    # given
    doc = load('scan.yml')
    text = (WORKFLOWS / 'scan.yml').read_text(encoding='utf-8')
    daily = copy.deepcopy(doc)
    daily[True]['schedule'] = [{'cron': '0 5 * * *'}]
    pushed = copy.deepcopy(doc)
    pushed[True]['push'] = {'branches': ['main']}
    floating = copy.deepcopy(doc)
    uses(floating, 'anchore/scan-action', 'scan')[0]['with'].pop('grype-version')
    quiet = copy.deepcopy(doc)
    for s in steps(quiet, 'scan'):
        if 'gh issue' in s.get('run', ''):
            s.pop('if')
    strict = copy.deepcopy(doc)
    uses(strict, 'anchore/scan-action', 'scan')[0]['with']['fail-build'] = True
    other = copy.deepcopy(doc)
    uses(other, 'anchore/scan-action', 'scan')[0]['with']['image'] = 'ghcr.io/skopax/skopax:0.1.0'

    # when
    found = schedule_problems(doc, text)

    # then
    assert found == []
    assert doc['permissions'] == {}
    assert doc['jobs']['scan']['permissions'] == PERMISSIONS[('scan.yml', 'scan')]
    assert schedule_problems(daily, text) == ['the scan runs more than weekly']
    assert schedule_problems(pushed, text) == ["scan.yml runs on ['push']"]
    assert schedule_problems(floating, text) == ['Grype is not pinned to the chosen version']
    assert schedule_problems(quiet, text) == ['the issue step is skipped when the upload fails']
    assert schedule_problems(strict, text) == ['fail-build is True']
    assert schedule_problems(other, text) == ['the scan does not take the published image']
    assert schedule_problems(doc, text.replace(SCAN_ISSUE_TITLE, 'Image scan')) == ['the issue title is not the agreed one']
    assert continue_on_error_problems({'scan.yml': doc}) == []
    assert shell_problems({'scan.yml': doc}) == []


def grype_sarif(findings: list[tuple[str, str, str, str]]) -> dict:
    rules = [{'id': rule, 'properties': {'security-severity': score}} for rule, score, level, text in findings if score]
    results = [{'ruleId': rule, 'level': level, 'message': {'text': text}} for rule, score, level, text in findings]
    return {'runs': [{'tool': {'driver': {'name': 'Grype', 'rules': rules}}, 'results': results}]}


def high(n: int) -> tuple[str, str, str, str]:
    return (f'CVE-2026-{n}-pkg{n}', '8.1', 'error', f'A High vulnerability in python package: pkg{n}, version 1.0 was found at: /x')


def medium(n: int) -> tuple[str, str, str, str]:
    return (f'CVE-2026-{n}-pkg{n}', '5.5', 'warning', f'A Medium vulnerability in python package: pkg{n}, version 1.0 was found at: /x')


def run_issue_script(script: str, tmp_path: Path, findings: list[tuple[str, str, str, str]], existing: str) -> tuple[list[str], str]:
    stubs = tmp_path / 'bin'
    stubs.mkdir(parents=True)
    log = tmp_path / 'gh.log'
    log.write_text('', encoding='utf-8')
    stub = stubs / 'gh'
    stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GH_LOG"\nif [ "$2" = list ]; then printf \'%s\' "$GH_LIST"; fi\n', encoding='utf-8')
    stub.chmod(0o755)
    sarif = tmp_path / 'results.sarif'
    sarif.write_text(json.dumps(grype_sarif(findings)), encoding='utf-8')
    listing = json.dumps([{'number': 7, 'title': SCAN_ISSUE_TITLE}] if existing == 'open' else [{'number': 3, 'title': 'Another issue'}])
    env = {'PATH': f'{stubs}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin', 'SARIF': str(sarif), 'GH_LOG': str(log), 'GH_LIST': listing,
           'GH_TOKEN': 'token', 'GITHUB_REPOSITORY': 'skopax/skopax', 'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '42',
           'RUNNER_TEMP': str(tmp_path)}
    done = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    calls = [line for line in log.read_text(encoding='utf-8').splitlines() if not line.startswith('issue list')]
    body = tmp_path / 'body.md'
    return calls, body.read_text(encoding='utf-8') if body.exists() else ''


def issue_script() -> str:
    return '\n'.join(s['run'] for s in steps(load('scan.yml'), 'scan') if 'gh issue' in s.get('run', ''))


@pytest.mark.skipif(shutil.which('jq') is None, reason='jq is not installed')
@pytest.mark.parametrize('findings, existing, verb', [([high(1), high(2)], 'none', 'issue create'), ([high(1), medium(3)], 'open', 'issue edit 7'),
                                                      ([medium(3)], 'open', 'issue close 7'), ([medium(3)], 'none', None), ([], 'none', None)])
def test_the_weekly_scan_opens_updates_and_closes_one_issue(tmp_path, findings, existing, verb):
    # given
    script = issue_script()

    # when
    calls, body = run_issue_script(script, tmp_path, findings, existing)

    # then
    assert len(calls) == (0 if verb is None else 1)
    assert verb is None or calls[0].startswith(verb)
    assert verb != 'issue create' or f'--title {SCAN_ISSUE_TITLE}' in calls[0]
    if verb in ('issue create', 'issue edit 7'):
        assert re.findall(r'^- `(CVE-[^`]+)`', body, re.M) == [f'CVE-2026-{n}-pkg{n}' for n in (1, 2)][:len([f for f in findings if f[1] == '8.1'])]
        assert 'CVE-2026-3-pkg3' not in body
        assert f'found {len([f for f in findings if f[1] == "8.1"])} fixable' in body


@pytest.mark.skipif(shutil.which('jq') is None, reason='jq is not installed')
def test_the_weekly_scan_cleans_the_text_it_puts_in_the_issue(tmp_path):
    # given
    hostile = ('bad id!', '9.8', 'error', 'A Critical vulnerability in python package: x, version 1 was found')
    noisy = ('CVE-2026-9-evil', '9.8', 'error', 'A Critical vulnerability in python package: ev`il@team, version 1.0 was found with `ticks` and @alice')
    unscored = ('GHSA-aaaa-bbbb-cccc', '', 'error', 'A High vulnerability in python package: lib, version 2 was found')
    skipped = ('GHSA-dddd-eeee-ffff', '', 'warning', 'A Medium vulnerability in python package: lib2, version 2 was found')

    # when
    calls, body = run_issue_script(issue_script(), tmp_path, [hostile, noisy, unscored, skipped], 'none')

    # then
    assert calls[0].startswith('issue create')
    assert 'bad id' not in body and 'GHSA-dddd' not in body
    assert '@' not in body.split('Run:')[0] and body.count('`') % 2 == 0
    assert '- `CVE-2026-9-evil` in `evilteam`:' in body and '- `GHSA-aaaa-bbbb-cccc` in `lib`:' in body
    assert 'found 2 fixable' in body


def run_expiry_script(script: str, tmp_path: Path, reason_date: str) -> int:
    stubs = tmp_path / 'bin'
    stubs.mkdir(parents=True)
    (stubs / 'python3').symlink_to(sys.executable)
    (tmp_path / '.grype.yaml').write_text(
        f'ignore:\n  - vulnerability: CVE-2026-0001\n    package:\n      name: libexample\n    reason: not reachable (review-by {reason_date})\n', encoding='utf-8')
    env = {'PATH': f'{stubs}:/usr/bin:/bin'}
    return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False).returncode


@pytest.mark.parametrize('date, code', [('2020-01-01', 1), ('2999-12-31', 0)])
def test_the_weekly_scan_fails_its_own_run_when_an_ignore_entry_has_expired(tmp_path, date, code):
    # given
    script = '\n'.join(s['run'] for s in steps(load('scan.yml'), 'scan') if '.grype.yaml' in s.get('run', ''))

    # when
    result = run_expiry_script(script, tmp_path, date)

    # then
    assert result == code
    assert script


def test_the_grype_ignore_file_lists_only_reviewed_findings():
    # given
    doc = yaml.safe_load(GRYPE_IGNORE.read_text(encoding='utf-8'))
    entries = doc['ignore']
    bare = {'vulnerability': 'CVE-2026-0001', 'package': {'name': 'libexample'}}
    complete = {**bare, 'reason': 'no fix upstream and not reachable (review-by 2027-01-31)'}

    # when
    found = ignore_problems(entries)

    # then
    assert list(doc) == ['ignore']
    assert found == []
    assert ignore_problems([bare]) == ['entry 0 has no reason', 'entry 0 has no review-by date']
    assert ignore_problems([complete]) == []
    assert ignore_problems([{**complete, 'reason': 'not reachable (review-by next year)'}]) == ['entry 0 has no review-by date']
    assert ignore_problems([{**complete, 'reason': '(review-by 2027-01-31)'}]) == ['entry 0 has no reason']
    assert ignore_problems([{**complete, 'review-by': '2027-01-31'}]) == ["entry 0 has keys Grype does not know: ['review-by']"]
    assert ignore_problems([{**complete, 'package': {}}]) == ['entry 0 has no package name']
    assert ignore_problems([{k: v for k, v in complete.items() if k != 'vulnerability'}]) == ['entry 0 has no vulnerability id']


def ignore_problems(entries: list) -> list[str]:
    problems = []
    for n, entry in enumerate(entries):
        if not re.fullmatch(r'(?:CVE-\d{4}-\d+|GHSA(?:-[a-z0-9]{4}){3})', str(entry.get('vulnerability', ''))):
            problems.append(f'entry {n} has no vulnerability id')
        if not str((entry.get('package') or {}).get('name', '')).strip():
            problems.append(f'entry {n} has no package name')
        match = re.fullmatch(r'(.*?)\s*\(review-by (\d{4}-\d{2}-\d{2})\)', str(entry.get('reason', '')).strip())
        if not (match[1].strip() if match else str(entry.get('reason', '')).strip()):
            problems.append(f'entry {n} has no reason')
        try:
            datetime.date.fromisoformat(match[2] if match else '')
        except ValueError:
            problems.append(f'entry {n} has no review-by date')
        unknown = sorted(set(entry) - IGNORE_KEYS)
        if unknown:
            problems.append(f'entry {n} has keys Grype does not know: {unknown}')
    return problems
