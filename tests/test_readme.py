import importlib
import re
from collections.abc import Mapping
import tomllib
from pathlib import Path

import yaml

import skopax
from skopax import registry
from skopax.config import settings_env_names
from skopax.features import BY_ID, FEATURES
from tools import release_version

ROOT = Path(__file__).resolve().parent.parent
SIDEBAR = (ROOT / 'skopax' / 'web' / 'static' / 'sidebar.js').read_text(encoding='utf-8')
NUMBERS = dict(enumerate(('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve')))
LINK_HOSTS = ('skopax.io', 'docs.astral.sh', 'localhost:8080')
LINK_TREES = ('github.com/skopax', 'ghcr.io/skopax')
GENERATED_SECRET = '"$(openssl rand -hex 32)"'
SECRET_NAME = r'[A-Z_]*(?:SECRET|TOKEN|PASSWORD|KEY)[A-Z_]*'
DOCS = ('README.md', 'setup.md', 'configuration.md', 'sign-in.md', 'plugins.md', 'extending.md', 'operations.md', 'development.md')
COMMUNITY = ('CONTRIBUTING.md', 'CODE_OF_CONDUCT.md', 'TRADEMARKS.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md', 'examples/hello-plugin/README.md')
COMMUNITY_HOSTS = ('modeltrademarkguidelines.org', 'www.modeltrademarkguidelines.org', 'creativecommons.org', 'www.contributor-covenant.org', 'contributor-covenant.org', 'developercertificate.org')
COMMUNITY_TREES = ('github.com/mozilla',)
ENV_NAME = re.compile(r'`\$?\{?([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(_\*)?')
OUTSIDE_NAMES = {'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'FORWARDED_ALLOW_IPS', 'GITHUB_TOKEN', 'THIRD_PARTY_NOTICES'}


def read(rel: str) -> str:
    path = ROOT / rel
    return path.read_text(encoding='utf-8') if path.is_file() else ''


def every_doc() -> dict[str, str]:
    return {rel: read(rel) for rel in ('README.md', *(f'docs/{name}' for name in DOCS))}


def all_docs() -> str:
    return '\n'.join(every_doc().values())


def community() -> dict[str, str]:
    return {rel: read(rel) for rel in COMMUNITY}


class AnyEnv(Mapping):
    def __init__(self):
        self.names: set[str] = set()

    def __getitem__(self, key: str) -> str:
        self.names.add(key)
        return 'https://x.example.com'

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0


def plugin_env_names() -> set[str]:
    env = AnyEnv()
    for name in registry.BUILTIN:
        reader = importlib.import_module(name).PLUGIN.env_instances
        if reader:
            reader(env)
    return env.names


def known_names() -> set[str]:
    code = '\n'.join(path.read_text(encoding='utf-8') for path in (ROOT / 'skopax').rglob('*.py'))
    constants = set(re.findall(r'^([A-Z][A-Z0-9_]*) = ', code, re.MULTILINE)) | set(re.findall(r"'([A-Z][A-Z0-9_]+)'", code))
    args = set(re.findall(r'^ARG ([A-Z_]+)', read('Dockerfile') + read('examples/hello-plugin/Dockerfile'), re.MULTILINE))
    ci = set(re.findall(r'\b([A-Z][A-Z0-9_]+)\b', read('.github/workflows/ci.yml') + read('.github/workflows/image.yml')))
    runner = set(re.findall(r'process\.env\.([A-Z_]+)', read('tests/page/run.js')))
    return settings_env_names() | plugin_env_names() | constants | args | ci | runner | OUTSIDE_NAMES


def unknown_names(texts: dict[str, str], known: set[str]) -> list[str]:
    return sorted({f'{rel}: {name}{star}' for rel, text in texts.items() for name, star in ENV_NAME.findall(text)
                   if not (any(k.startswith(f'{name}_') for k in known) if star else name in known)})


def switch_rows() -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r'^\| (.+?) \| `SKOPAX_[A-Z_]+` \| (.+?) \|$', read('docs/setup.md'), re.MULTILINE)}


def image_tag() -> str:
    return release_version.image_tags(release_version.release_tag(skopax.__version__))[0]


def workflow(name: str) -> dict:
    return yaml.safe_load(read(f'.github/workflows/{name}'))


def setup_steps(doc: dict, action: str) -> list[dict]:
    return [step for job in doc['jobs'].values() for step in job.get('steps', ()) if action in step.get('uses', '')]


def test_the_setup_page_names_the_admin_sections_in_the_order_of_the_sub_nav():
    # given
    tabs = re.findall(r"'([a-z]+)'", re.search(r'var ADMIN_TABS = \[([^\]]+)\]', SIDEBAR).group(1))
    labels = dict(re.findall(r"(\w+): '([^']+)'", re.search(r'var ADMIN_LABELS = \{([^}]+)\}', SIDEBAR).group(1)))
    named = [labels[t] for t in tabs]

    # then
    assert f"Admin has six sections: {', '.join(named[:-1])} and {named[-1]}." in read('docs/setup.md')


def test_the_setup_page_lists_every_feature_switch_and_the_docs_promise_no_restart_or_reset_the_page_lacks():
    # when
    rows = switch_rows()

    # then
    assert sorted(rows) == sorted(f.label for f in FEATURES)
    assert '(after restart)' not in all_docs() and 'reset there' not in all_docs()
    assert rows['Chat'].endswith('needs `CHAT_UI` and SSO at boot')
    assert 'every pod' in read('docs/setup.md') and 'on every instance' not in all_docs()


def test_the_phone_push_card_and_the_setup_page_both_cover_task_notices():
    # then
    assert 'task notices' in BY_ID['push'].help and 'task notices' in switch_rows()['Phone push']


def test_the_plugins_page_puts_a_wrong_version_plug_in_in_the_catalog_and_a_broken_one_on_the_notices_strip():
    # given
    app = (ROOT / 'skopax' / 'app.py').read_text(encoding='utf-8')

    # then
    assert "if found.state == 'broken']" in app, 'the notices strip lists broken plug-ins only'
    assert 'A plug-in that needs a different Skopax version is listed there too, but cannot be set up' in read('docs/plugins.md')
    assert 'needs a different Skopax version or fails to load' not in all_docs()


def mcp_section() -> str:
    text = read('docs/plugins.md') + '\n## '
    start = text.index('## Outside MCP servers')
    return text[start:text.index('\n## ', start + 1)]


def test_the_mcp_section_is_pinned_to_the_mcp_constants():
    # given
    from skopax import mcptools as m
    from skopax.investigator.tools import ERROR_LIMIT, INPUT_LIMIT
    from skopax.plugins.mcp_tools import PLUGIN
    text = mcp_section()
    rows = dict(re.findall(r'^\| (.+?) \| (.+?) \|$', text, re.MULTILINE))

    # then
    assert rows['Tools offered from MCP servers'] == f'{m.RUN_TOOLS_MAX}, with at most {m.RUN_DEFINITIONS_MAX:,} characters of their definitions'
    assert rows['Calls of one tool'] == str(m.PER_TOOL_CALLS) and rows['Calls to MCP servers'] == str(m.RUN_CALLS_MAX)
    assert rows['Characters read before calls stop'] == f'{m.RUN_OUTPUT_MAX:,}'
    assert rows['One answer'] == f'{m.OUTPUT_MAX:,} characters ({m.RESPONSE_BYTES_MAX // (1024 * 1024)} MB from the server)'
    assert rows['One call'] == f'{m.CALL_DEADLINE_S} seconds' and rows['The run-start listing'] == f'{m.RUN_LIST_DEADLINE_S} seconds, reused for {m.LIVE_CACHE_S} seconds'
    for phrase in (f'At most {PLUGIN.max_instances} connections', f'within {m.TEST_DEADLINE_S} seconds', f'at most {m.TOOLS_MAX}', f'up to {m.ON_MAX}',
                   f'At most {m.ON_MAX} tools of one connection', f'over {m.SCHEMA_MAX:,} characters', f'cut at {m.DESCRIPTION_MAX:,} characters',
                   f'within {m.STATE_TTL_S // 60} minutes', f'renewed {m.REFRESH_EARLY_S // 60} minutes before', f'after {m.RETRY_AFTER_S // 60} minute',
                   f'at most {m.RETRY_AFTER_MAX_S // 3600} hour', f'last {m.FOLDERS_TTL_S // 60} minutes', f'cut at {m.LABEL_MAX} characters',
                   f'cut at {INPUT_LIMIT:,} characters', f'every connection again ({m.RUN_LIST_DEADLINE_S} seconds', f'fit {ERROR_LIMIT} characters'):
        assert phrase in text, phrase


def test_the_mcp_section_states_the_wave_rules():
    # given
    text = mcp_section()

    # then
    for phrase in ('starts with `execute` and contains `write` or `destructive`', 'any connection to its server address', 'holds a secret of the connection',
                   'when its URL changes, wherever it is set', 'shows as an error with the text of its notice', 'A switched-off connection raises no notices'):
        assert phrase in text, phrase


def test_the_web_section_states_the_wave_rules():
    # then
    for said in ('It needs `browser-url`. Without the Anthropic key', 'only that site\'s hosts are opened', '`slack.attach-screenshots`', 'Separate headers with semicolons',
                 '`internal-subdomains`', 'can still be reached by panning', 'is held back from the model', 'CONNECT tunnel to the checked address', 'the check is best-effort', 'must be an `http://` URL', 'no proxy sits in front of the browser service', 'its pictures are not sent', 'a day after it'):
        assert said in read('docs/plugins.md'), said
    assert 'It needs `browser-url`, the Anthropic key and the AI investigator switched on' not in all_docs()


def slug(heading: str) -> str:
    return re.sub(r'[^a-z0-9 _-]', '', heading.strip().lower()).replace(' ', '-')


def anchors(text: str) -> set[str]:
    seen: dict[str, int] = {}
    found = set()
    for m in re.finditer(r'^#{1,6} (.+?)\s*$', blocks_out(text), re.MULTILINE):
        base = slug(m.group(1).replace('`', ''))
        n = seen.get(base, 0)
        seen[base] = n + 1
        found.add(base if n == 0 else f'{base}-{n}')
    return found


def without_code_blocks(text: str) -> str:
    kept, code, listed, blank = [], False, False, True
    for line in text.split('\n'):
        indented = bool(re.match(r'(?: {4}|\t)', line))
        code = (code and (indented or not line.strip())) or (indented and blank and not listed)
        if not code and line.strip() and not indented:
            listed = bool(re.match(r'(?:[-*+]|[0-9]+[.)])\s', line)) or (listed and not blank)
        blank = not line.strip()
        kept.append('' if code else line)
    return '\n'.join(kept)


def blocks_out(text: str) -> str:
    return without_code_blocks(re.sub(r'^```.*?^```', '', text, flags=re.MULTILINE | re.DOTALL))


def prose(text: str) -> str:
    return re.sub(r'`[^`\n]*`', '', blocks_out(text))


def foreign_links(texts: dict[str, str], hosts: tuple[str, ...] = (), trees: tuple[str, ...] = ()) -> list[str]:
    found = []
    hosts, trees = LINK_HOSTS + hosts, LINK_TREES + trees
    for rel, text in texts.items():
        for url in re.findall(r'(?i)\bhttps?://[^\s)<>\]`"\']+|(?<![\w/.@:-])www\.[^\s)<>\]`"\']+', prose(text)):
            url = re.sub(r'(?i)^https?://', '', url.rstrip('.,;:'))
            dotted = any(part in ('.', '..') for part in url.split('/')[1:])
            if dotted or url.split('/', 1)[0].lower() not in hosts and not any(url == tree or url.startswith(f'{tree}/') for tree in trees):
                found.append(f'{rel}: {url}')
    return found


def links(text: str) -> list[str]:
    return re.findall(r'\]\(([^)\s]+)\)', prose(text))


def broken_links(texts: dict[str, str]) -> list[str]:
    broken = []
    for rel, text in texts.items():
        for target in links(text):
            if re.match(r'[a-z]+:', target):
                continue
            path, _, anchor = target.partition('#')
            file = (ROOT / rel).parent / path if path else ROOT / rel
            if path.startswith('/') or not file.resolve().is_relative_to(ROOT) or not file.exists():
                broken.append(f'{rel}: {target}')
            elif anchor and (file.suffix != '.md' or anchor not in anchors(file.read_text(encoding='utf-8'))):
                broken.append(f'{rel}: {target}')
    return broken


def test_every_docs_link_resolves():
    # given
    texts = every_doc()

    # when
    broken = broken_links(texts)

    # then
    assert [rel for rel, text in texts.items() if not text] == []
    assert broken == []
    assert sum(len(links(text)) for text in texts.values()) > 50
    for name in DOCS[1:]:
        assert f'](docs/{name})' in texts['README.md'] and f']({name})' in texts['docs/README.md'], name


def test_the_link_check_finds_a_missing_file_a_missing_anchor_and_an_absolute_path():
    # given
    texts = {'README.md': '[a](docs/nowhere.md) [b](docs/README.md#no-such-heading) [c](/etc/hosts) [d](LICENSE) [e](https://example.com) `[f](gone.md)`\n\n    [g](gone.md)\n'}

    # when
    broken = broken_links(texts)

    # then
    assert broken == ['README.md: docs/nowhere.md', 'README.md: docs/README.md#no-such-heading', 'README.md: /etc/hosts']
    assert anchors('# Code, tickets and chat\n## Prometheus\n### Prometheus\n') == {'code-tickets-and-chat', 'prometheus', 'prometheus-1'}
    assert anchors('### `pyproject.toml`: the package\n```sh\n# not a heading\n```\n') == {'pyprojecttoml-the-package'}
    assert 'pyprojecttoml-the-package-and-its-entry-point' in anchors(read('docs/extending.md'))


def test_every_community_file_link_resolves_and_points_only_at_the_allowed_hosts():
    # given
    texts = community()

    # when
    broken = broken_links(texts)
    found = foreign_links(texts, COMMUNITY_HOSTS, COMMUNITY_TREES)
    outside = foreign_links({'docs/README.md': 'https://creativecommons.org/licenses/by/4.0/ https://github.com/mozilla/x'})

    # then
    assert [rel for rel, text in texts.items() if not text] == []
    assert broken == [] and found == []
    assert outside == ['docs/README.md: creativecommons.org/licenses/by/4.0/', 'docs/README.md: github.com/mozilla/x'], 'the extra hosts hold for the community files only'


def test_the_link_check_reads_links_in_nested_list_items_and_skips_only_real_code_blocks():
    # given
    texts = {'README.md': '- item\n    - inner [x](nowhere.md)\n- next\n\n    more of next [y](gone-too.md)\n\nA paragraph:\n\n    [g](gone.md)\n    [h](gone.md)\n\nAfter [z](absent.md)\n\n'
                          '- another item\nlazy line\n\n    [w](lazy-gone.md)\n'}

    # when
    broken = broken_links(texts)

    # then
    assert broken == ['README.md: nowhere.md', 'README.md: gone-too.md', 'README.md: absent.md', 'README.md: lazy-gone.md']


def test_links_point_only_at_the_allowed_hosts():
    # given
    probes = {'README.md': 'https://bsky.app/profile/skopax https://www.linkedin.com/company/skopax [v](https://youtube.com/@skopax) https://github.com/other/skopax '
                           'https://github.com/skopaxx https://skopax.io.example.com/x https://ghcr.io/other/skopax www.linkedin.com/company/skopax '
                           'https://github.com/skopax/../other/repo HTTPS://bsky.app/x\n\n    https://code.example.com/in-a-block\n',
              'docs/README.md': 'https://github.com/skopax https://github.com/skopax/skopax/issues. https://ghcr.io/skopax/skopax https://skopax.io/docs, '
                                'https://docs.astral.sh/uv/ http://localhost:8080 `https://gitlab.com`\n'}

    # when
    found = foreign_links(probes)
    real = foreign_links(every_doc())

    # then
    assert found == ['README.md: bsky.app/profile/skopax', 'README.md: www.linkedin.com/company/skopax', 'README.md: youtube.com/@skopax', 'README.md: github.com/other/skopax',
                     'README.md: github.com/skopaxx', 'README.md: skopax.io.example.com/x', 'README.md: ghcr.io/other/skopax', 'README.md: www.linkedin.com/company/skopax',
                     'README.md: github.com/skopax/../other/repo', 'README.md: bsky.app/x']
    assert real == []


def test_readme_states_the_release_candidate_status():
    # given
    text = read('README.md')

    # then
    assert 'Status: Release candidate, not yet stable.' in text
    assert 'pre-release, not ready for use' not in text
    assert re.search(rf'ghcr\.io/skopax/skopax:{re.escape(image_tag())}(?![\w.-])', text)
    assert 'docker.io/skopax/skopax' in text and 'comes with 0.1.0' in text
    assert 'docker compose up --build' in text and '## First run' in text


def test_docs_name_no_contact_but_hello_and_github():
    # given
    from tests.test_community_files import stray_addresses
    texts = every_doc()
    flat = ' '.join(all_docs().split())

    # when
    strays = stray_addresses(texts)
    skopax_addresses = set(re.findall(r'[A-Za-z0-9._%+-]+@skopax\.io', flat))
    mailto = set(re.findall(r'mailto:([^)\s>`]+)', flat))
    github = set(re.findall(r'https?://github\.com/([^/\s)`]+)', flat))

    # then
    assert strays == []
    assert skopax_addresses == {'hello@skopax.io'}
    assert mailto <= {'hello@skopax.io'}
    assert github <= {'skopax'}
    assert foreign_links(texts) == []
    assert 'private vulnerability reporting' in ' '.join(read('README.md').split())


def test_every_image_tag_and_git_tag_the_docs_name_is_the_current_release():
    # given
    text = all_docs() + read('examples/hello-plugin/README.md')
    named = re.findall(r'ghcr\.io/skopax/skopax:([^\s`\'"]+)', text) + re.findall(r'SKOPAX_VERSION=([^\s`\'"]+)', text)
    refs = re.findall(r'git clone --branch (\S+) https://github\.com/skopax/skopax\b', text)
    bug = yaml.safe_load(read('.github/ISSUE_TEMPLATE/bug.yml'))
    placeholder = next(item['attributes']['placeholder'] for item in bug['body'] if item.get('id') == 'version')

    # then
    assert len(named) >= 4 and 'SKOPAX_VERSION=' in read('examples/hello-plugin/README.md')
    assert set(named) == {image_tag()}
    assert refs and set(refs) == {release_version.release_tag(skopax.__version__)}
    assert placeholder == image_tag()


def placeholder_secrets(text: str) -> list[str]:
    found = re.findall(rf'\b({SECRET_NAME})=("[^"]*"|\'[^\']*\'|[^\s`)]+)', text)
    return [f'{name}={value}' for name, value in found if value not in (GENERATED_SECRET, f'"${name}"')]


def test_the_docs_pass_only_generated_secrets_and_say_to_keep_the_key():
    # given
    probes = ('Set SKOPAX_SECRET_KEY=changeme first.\n      --env SKOPAX_TOKEN=changeme \\\n'
              "      -e SKOPAX_SECRET_KEY='<a long random value>' \\\n      -e SKOPAX_TOKEN=\"$SKOPAX_SECRET_KEY\" \\\n"
              '    SKOPAX_SECRET_KEY="$(openssl rand -hex 32)"\n      -e SKOPAX_SECRET_KEY="$SKOPAX_SECRET_KEY" \\\n')
    readme, flat = read('README.md'), ' '.join(all_docs().split())

    # when
    found = placeholder_secrets(probes)

    # then
    assert found == ['SKOPAX_SECRET_KEY=changeme', 'SKOPAX_TOKEN=changeme', "SKOPAX_SECRET_KEY='<a long random value>'", 'SKOPAX_TOKEN="$SKOPAX_SECRET_KEY"']
    assert placeholder_secrets(all_docs()) == []
    assert f'    SKOPAX_SECRET_KEY={GENERATED_SECRET}\n' in readme and '-e SKOPAX_SECRET_KEY="$SKOPAX_SECRET_KEY"' in readme
    assert 'Keep that key' in readme and 'cannot open the integration secrets sealed with the old one' in flat
    assert 'long random value>' not in all_docs()


def test_the_compose_quickstart_makes_the_key_once_in_the_env_file_compose_passes_on():
    # given
    readme = read('README.md')
    compose = yaml.safe_load(read('docker-compose.yml'))
    quick = readme[readme.index('## Quickstart'):readme.index('Or run the published image')]

    # then
    assert compose['services']['skopax']['environment']['SKOPAX_SECRET_KEY'] == '${SKOPAX_SECRET_KEY:-}'
    assert f'    echo SKOPAX_SECRET_KEY={GENERATED_SECRET} >> .env\n    docker compose up --build\n' in quick
    assert '`.env`' in quick and 'once' in quick
    assert '.env' in read('.gitignore').split()


def test_every_env_name_the_docs_mention_exists_and_the_settings_list_points_at_the_setting_ids():
    # given
    known = known_names()
    flat = ' '.join(read('docs/configuration.md').split())

    # when
    missing = unknown_names(every_doc(), known)
    planted = unknown_names({'docs/x.md': '`SKOPAX_NO_SUCH_SETTING`, `SKOPAX_NOTHING_*`, `SKOPAX_FEATURE_*` and `SKOPAX_TIMEZONE`'}, known)

    # then
    assert missing == []
    assert planted == ['docs/x.md: SKOPAX_NOTHING_*', 'docs/x.md: SKOPAX_NO_SUCH_SETTING']
    assert {'PROMETHEUS_UI_URL', 'PROMETHEUS_NAMESPACES', 'GITLAB_TOKEN'} <= plugin_env_names()
    assert 'Every setting has an id, such as `core.timezone`, shown under its row in Admin > General' in flat
    assert 'give it under `settings:` in the config file' in flat


def test_the_configuration_page_keeps_the_compose_rules_in_one_paragraph():
    # given
    text = read('docs/configuration.md')
    compose = next(part for part in text.split('\n\n') if 'passes exactly five variables' in part)
    start = text.index('## Branding')
    branding = text[start:text.index('\n## ', start + 1)]

    # then
    assert '`.env`' in compose
    assert 'Compose' not in branding


def test_the_docs_describe_the_current_release_only():
    # given
    text = ' '.join(all_docs().lower().split())

    # then
    for phrase in ('earlier version', 'another prefix', 'new in this version', 'previous version', 'used to be', 'older release', 'as before', 'from before', 'keep working',
                   'still works', 'do not break'):
        assert phrase not in text, phrase


def test_the_operations_page_says_how_to_upgrade_and_back_up():
    # given
    from skopax.migrations import LOCK_S
    text = read('docs/operations.md')
    flat = ' '.join(text.split())

    # then
    assert '\n## Upgrades\n' in text and '[Upgrades](operations.md#upgrades)' in read('docs/README.md')
    for said in ('mongodump', 'keep the same `SKOPAX_SECRET_KEY`', 'docker compose up -d', 'one pod at a time', f'{LOCK_S // 60}-minute lease', 'no downgrade path',
                 'rebuild your layer images on the new tag'):
        assert said in flat, said


def test_the_notices_name_the_python_interpreter_the_image_ships():
    # given
    python = re.search(r'^FROM python:(\d+\.\d+)-slim', read('Dockerfile'), re.MULTILINE)[1]
    said = f'CPython {python}'
    licence = f'/usr/local/lib/python{python}/LICENSE.txt'

    # then
    for rel in ('THIRD_PARTY_NOTICES.md', 'docs/operations.md', 'README.md'):
        flat = ' '.join(read(rel).split())
        assert said in flat and 'PSF-2.0' in flat and licence in flat, rel
    notices = ' '.join(read('THIRD_PARTY_NOTICES.md').split())
    assert '/usr/share/doc/' in notices and 'distroless base' in notices


def test_the_development_page_matches_the_workflows_the_dockerfile_and_the_gitleaks_config():
    # given
    flat = ' '.join(read('docs/development.md').split())
    image, ci, image_text = workflow('image.yml'), workflow('ci.yml'), read('.github/workflows/image.yml')
    section = flat[flat.index('- **image**'):flat.index('- **Dependabot**')]
    jobs = [job for job in image['jobs'] if job != 'ci']
    writers = [job for job, body in image['jobs'].items() if (body.get('permissions') or {}).get('packages') == 'write']
    uv = {re.search(r'ghcr\.io/astral-sh/uv:([^@\s]+)', read('Dockerfile'))[1]} | {str(step['with']['version']) for step in setup_steps(ci, 'astral-sh/setup-uv')}
    node = {str(step['with']['node-version']) for step in setup_steps(ci, 'actions/setup-node')}
    allowlists = sum(1 for entry in tomllib.loads(read('.gitleaks.toml'))['allowlists'] if entry.get('regexes'))

    # then
    assert len(uv) == 1 and f'uv {min(uv)}' in flat
    assert len(node) == 1 and f'`pages`: Node {min(node)}, ' in flat
    major, contributing = min(node).split('.')[0], ' '.join(read('CONTRIBUTING.md').split())
    assert all(f'Node.js {major} ({min(node)}.' in text for text in (flat, contributing))
    assert f'gitleaks {ci["jobs"]["secrets"]["env"]["GITLEAKS_VERSION"]}' in flat
    assert f'allowlists only {NUMBERS[allowlists]} named fake values' in flat
    assert f'Then it runs {NUMBERS[len(jobs)]} jobs, in order:' in section
    assert re.findall(r' - `([a-z-]+)`: ', section) == jobs
    assert image['jobs']['promote']['environment'] == 'release' and '`promote`: runs only after the smoke passes, in the `release` environment.' in section
    assert "tr '[:upper:]' '[:lower:]'" in image_text and 'lowercased: `ghcr.io/<owner>/<repo>`' in section
    assert 'It pushes only the exact version tag' in section and 'adds `X.Y` and `latest` on GHCR' in section
    assert writers == ['build', 'promote'] and f'`packages: write` in {" and ".join(f"`{job}`" for job in writers)} only' in flat
    assert 'as environment secrets of `release`, not repository secrets' in flat
    assert '`/var/lib/dpkg/status.d/`' in flat and 'Syft' in flat
    assert 'the `SKOPAX_VERSION` defaults in `Dockerfile` and `examples/hello-plugin/Dockerfile`' in flat
    tests_runs = [step['run'] for step in ci['jobs']['tests']['steps'] if 'run' in step and not step['run'].startswith('uv python install')]
    tests_bullet = flat[flat.index('- `tests`:'):flat.index('- `pages`:')]
    assert 'uv lock --check' in tests_runs and [run for run in tests_runs if f'`{run}`' in tests_bullet] == tests_runs
    assert [tests_bullet.index(f'`{run}`') for run in tests_runs] == sorted(tests_bullet.index(f'`{run}`') for run in tests_runs)
    assert 'under `tests/__pycache__/`' in flat
    held = flat[flat.index('Every Debian library copied into the image'):flat.index('Fork pull requests')]
    assert 'must come from a package the distroless base ships' in held and '`[bundled.' in held and '`third-party-bundled.md`' in held
    gate = flat[flat.index('## The licence gate'):flat.index('## Releases')]
    assert '**Bundled.**' in gate and '`third-party-bundled.md`' in gate
    for gone in ('runs three jobs', 'in the build job only', 'Actions secrets', 'nine named test fixture files'):
        assert gone not in flat, gone


def test_the_operations_and_plugins_pages_describe_readiness_bundled_libraries_and_the_prometheus_settings():
    # given
    operations = ' '.join(read('docs/operations.md').split())
    plugins = read('docs/plugins.md').splitlines()

    # when
    profiles = next(line for line in plugins if line.startswith('- **Profiles**'))
    excluded = next(line for line in plugins if line.startswith('- **Ingress exclusion pattern**'))
    agent = operations[operations.index('## Agent mode'):]

    # then
    assert '`third-party-bundled.md`' in operations
    assert 'Switching `kube` off also stops image-change detection' in profiles and 'Switching' not in excluded
    assert '`GET /actuator/health/readiness`' in agent and 'An agent uses no database' in agent


def test_the_extending_page_shows_the_example_dockerfile_as_it_is():
    # given
    text = read('docs/extending.md')
    blocks = re.findall(r'^```dockerfile\n(.*?)^```', text, re.MULTILINE | re.DOTALL)
    flat = ' '.join(text.split())

    # then
    assert blocks == [read('examples/hello-plugin/Dockerfile')]
    assert '`BASE_IMAGE` (default `ghcr.io/skopax/skopax`)' in flat and '`SKOPAX_VERSION` (default' in flat


def test_the_extending_page_says_how_to_test_a_plug_in_outside_this_repository_and_keeps_to_its_key_rule():
    # given
    text = read('docs/extending.md')
    flat = ' '.join(text.split())
    clone = f'git clone --branch {release_version.release_tag(skopax.__version__)} https://github.com/skopax/skopax ../skopax'
    steps = f'    {clone}\n    (cd ../skopax && uv sync --frozen)\n    PYTHONPATH=../skopax:. ../skopax/.venv/bin/python -m pytest -q tests\n'

    # then
    assert '### Working outside this repository' in text
    assert steps in text and 'not published to a package index' in flat
    assert '`--no-deps`' in flat and 'not a runtime dependency of the plug-in wheel' in flat
    assert '`browser-down|<host>`' not in text
