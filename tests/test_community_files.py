import re
import tomllib
from pathlib import Path

import yaml

from skopax import brand

from tests.test_rename_sweep import tracked_and_untracked
from tests.yaml_kit import yaml_comment_lines

ROOT = Path(__file__).resolve().parent.parent
SKIPPED_PARTS = ('.venv/', 'node_modules/', '.git/')
PLACEHOLDER = re.compile(r'\[\[[A-Z][A-Z0-9_]*\]\]')
NOT_ADVICE = 'not legal ' + 'advice'
CODE_SUFFIXES = ('.py', '.js')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})')
CONTACT = 'hello@skopax.io'
RESERVED_DOMAINS = re.compile(r'(?:^|\.)(?:example\.com|example\.org|example\.net)$|\.(?:example|test|invalid|localhost|internal)$')
FIXTURE_ADDRESSES = {
    'docs/extending.md': {'name' + '@host.tld'},
    'tests/test_kube.py': {'pa55word' + '@mongo-0.svc'},
    'tests/test_sharepoint_plugin.py': {'user' + '@contoso.sharepoint.com'},
}


def shipped_text() -> dict[str, str]:
    found = {}
    for rel in tracked_and_untracked(ROOT):
        if any(part in f'/{rel}' for part in ('/' + p for p in SKIPPED_PARTS)):
            continue
        raw = (ROOT / rel).read_bytes()
        if b'\0' in raw:
            continue
        try:
            found[rel] = raw.decode('utf-8')
        except UnicodeDecodeError:
            continue
    return found


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding='utf-8')


def marker_hits(texts: dict[str, str]) -> list[str]:
    hits = []
    for rel, text in texts.items():
        for n, line in enumerate(text.splitlines(), 1):
            if NOT_ADVICE in line.lower():
                hits.append(f'{rel}:{n}')
            elif not rel.endswith(CODE_SUFFIXES) and (PLACEHOLDER.search(line) or re.search(r'\bDRAFT\b', line)):
                hits.append(f'{rel}:{n}')
    return hits


def stray_addresses(texts: dict[str, str]) -> list[str]:
    return sorted({f'{rel}: {m[0]}' for rel, text in texts.items() for m in EMAIL.finditer(text)
                   if m[0].lower() != CONTACT and m[0].lower() not in FIXTURE_ADDRESSES.get(rel, set()) and not RESERVED_DOMAINS.search(m[1].lower())})


def test_no_placeholder_or_draft_marker_ships():
    # given
    texts = shipped_text()

    # when
    hits = marker_hits(texts)

    # then
    assert texts
    assert hits == []


def test_the_marker_check_flags_markdown_and_yaml_and_ignores_code_and_templates():
    # given
    flagged = {'a.md': 'Version [[TM_POLICY_VERSION]]', 'b.yml': 'x: "[[PLACEHOLDER]]"', 'c.md': 'DRAFT for review', 'd.md': 'Not Legal ' + 'Advice'}
    clean = {'t.html': '[[brand]]', 'x.py': 'rows = [[KEY]]\nDRAFT = 1', 'y.js': 'const DRAFT = 1'}

    # when
    found = marker_hits(flagged)
    ignored = marker_hits(clean)

    # then
    assert found == ['a.md:1', 'b.yml:1', 'c.md:1', 'd.md:1']
    assert ignored == []


def test_the_only_contact_address_is_hello():
    # given
    texts = shipped_text()

    # when
    strays = stray_addresses(texts)

    # then
    assert strays == []
    assert CONTACT in read('CODE_OF_CONDUCT.md')
    assert CONTACT in read('TRADEMARKS.md')


def test_the_contact_check_rejects_strays_and_keeps_fixtures_to_their_files():
    # given
    texts = {'x.md': 'write to security' + '@skopax.io or bob' + '@gmail.com', 'ok.md': 'hello@skopax.io, alice@example.com, t@db-1.internal',
             'docs/extending.md': 'name' + '@host.tld', 'other.md': 'name' + '@host.tld'}

    # when
    found = stray_addresses(texts)

    # then
    assert found == ['other.md: name' + '@host.tld', 'x.md: bob' + '@gmail.com', 'x.md: security' + '@skopax.io']


def test_contributing_welcomes_pull_requests_with_conventional_titles_and_dco_sign_off():
    # given
    text = read('CONTRIBUTING.md')

    # when
    flat = ' '.join(text.split())

    # then
    assert 'Issues are welcome' in flat
    assert 'Pull requests are welcome' in flat
    assert 'open with 0.1.0' not in flat
    assert 'link each one to an issue' in flat
    assert 'Conventional Commits' in flat
    assert 'type: summary' in flat
    assert all(f'`{kind}`' in flat for kind in ('feat', 'fix', 'docs', 'ci', 'test', 'chore', 'build', 'perf', 'refactor'))
    assert 'becomes the squash commit' in flat
    assert 'GNU Affero General Public License, version 3 (AGPL-3.0-only)' in flat
    assert 'Developer Certificate of Origin' in flat
    assert 'https://developercertificate.org' in flat
    assert 'git commit -s' in flat
    assert 'Signed-off-by' in flat
    assert 'docs/development.md' in flat
    assert 'CLA' not in text
    assert 'By signing off a contribution, you also license it to the project under Apache-2.0' in flat
    assert 'Skopax itself is distributed under AGPL-3.0-only' in flat
    assert 'comes in under the terms it goes out under' not in flat
    assert 'Contributor License Agreement' not in text


def test_security_points_to_private_vulnerability_reporting():
    # given
    text = read('SECURITY.md')

    # when
    flat = ' '.join(text.split())

    # then
    assert 'private vulnerability reporting' in flat.lower()
    assert 'Security' in flat and 'Report a vulnerability' in flat
    assert 'https://github.com/skopax/skopax' in flat
    assert '5 working days' in flat
    assert '90 days' in flat
    assert 'latest release' in flat and 'release candidate' in flat
    assert '@' not in text


def test_trademarks_reserves_the_name_and_logo_and_licenses_the_files_like_the_code():
    # given
    text = read('TRADEMARKS.md')

    # when
    flat = ' '.join(text.split())

    # then
    assert 'Version 1.0, 2026-11-17' in text
    assert 'reserves the Skopax name, the Beam X logo and the Skopax wordmark' in flat
    assert 'The logo files are licensed under AGPL-3.0-only like the code' in flat
    assert 'does not give permission to use the Skopax name or logo' in flat
    assert 'Creative Commons Attribution 4.0' in flat
    assert 'Derived from Model Trademark Guidelines, available at www.modeltrademarkguidelines.org.' in text
    assert 'not registered' not in flat
    assert 'brand page' not in flat.lower()
    assert 'whether registered' not in flat


def test_trademarks_lets_a_plug_in_package_carry_the_skopax_prefix_as_a_functional_use():
    # given
    flat = ' '.join(read('TRADEMARKS.md').split())
    functional = flat[flat.index('## 3. The basic rule'):flat.index('## 4.')]
    permission = flat[flat.index('## 5. Uses that need written permission'):flat.index('## 6.')]
    example = read('examples/hello-plugin/pyproject.toml')
    fuller = {name: read(f'examples/{name}-plugin/pyproject.toml') for name in ('couchbase', 'managed-redis', 'batch-jobs')}

    # then
    assert 'a plug-in package named `skopax-<name>` or `skopax_<name>`' in functional
    assert 'says it is "for Skopax", does not use the logo and does not claim to be official' in functional
    assert 'package name that contains "Skopax" (other than a plug-in package named as in section 3)' in permission
    assert '"SkopaxOps" (other than a plug-in, and its repository, named as in section 3);' in permission
    assert 'name = "skopax-hello"' in example and 'description = "Example plug-in for Skopax:' in example
    assert all(f'name = "skopax-{name}"' in text and 'description = "Example plug-in for Skopax:' in text for name, text in fuller.items())
    assert all('license = "Apache-2.0"' in text and 'license-files = ["LICENSE"]' in text for text in [example, *fuller.values()])
    assert all(read(f'examples/{name}-plugin/LICENSE') == read('examples/hello-plugin/LICENSE') for name in fuller)
    assert read('examples/hello-plugin/LICENSE').lstrip().startswith('Apache License')


def test_code_of_conduct_is_the_contributor_covenant_with_the_hello_contact():
    # given
    text = read('CODE_OF_CONDUCT.md')

    # when
    reports = re.findall(r'reported to the community leaders responsible for enforcement at (\S+?)\.\s', text)

    # then
    assert text.startswith('# Contributor Covenant Code of Conduct')
    assert 'version 2.1' in text
    assert reports == [CONTACT]
    assert 'Creative Commons Attribution 4.0 International' in text
    assert 'https://creativecommons.org/licenses/by/4.0/' in text


def test_issue_forms_block_blank_issues_and_link_security_to_the_policy():
    # given
    config = yaml.safe_load(read('.github/ISSUE_TEMPLATE/config.yml'))
    forms = {name: yaml.safe_load(read(f'.github/ISSUE_TEMPLATE/{name}.yml')) for name in ('bug', 'feature')}

    # when
    links = config['contact_links']

    # then
    assert config['blank_issues_enabled'] is False
    assert len(links) == 1
    assert links[0]['url'] == 'https://github.com/skopax/skopax/security/advisories/new'
    for form in forms.values():
        assert form['name'] and form['description'] and form['body']
        assert any(item['type'] == 'textarea' and item['validations']['required'] for item in form['body'])
    assert forms['bug']['labels'] == ['bug']
    assert forms['feature']['labels'] == ['enhancement']


def test_issue_forms_and_the_pull_request_template_carry_no_comments_or_secrets_prompts():
    # given
    paths = ['.github/ISSUE_TEMPLATE/bug.yml', '.github/ISSUE_TEMPLATE/feature.yml', '.github/ISSUE_TEMPLATE/config.yml']

    # when
    comment_lines = {p: yaml_comment_lines(read(p)) for p in paths if yaml_comment_lines(read(p))}
    template = ' '.join(read('.github/PULL_REQUEST_TEMPLATE.md').split())

    # then
    assert comment_lines == {}
    assert 'git commit -s' in template
    assert 'open with 0.1.0' not in template
    assert 'Pull requests are welcome' in template
    assert 'Conventional Commits' in template and 'type: summary' in template
    assert 'Closes #' in template and 'linked to an issue' in template
    assert 'By signing off, I license this contribution to the project under Apache-2.0; Skopax is distributed under AGPL-3.0-only' in template
    assert 'CLA' not in template


def test_community_files_and_docs_are_ascii():
    # given
    docs = sorted(str(path.relative_to(ROOT)) for path in (ROOT / 'docs').rglob('*.md'))
    paths = ['CONTRIBUTING.md', 'CODE_OF_CONDUCT.md', 'TRADEMARKS.md', 'SECURITY.md', '.github/PULL_REQUEST_TEMPLATE.md',
             '.github/ISSUE_TEMPLATE/bug.yml', '.github/ISSUE_TEMPLATE/feature.yml', '.github/ISSUE_TEMPLATE/config.yml',
             'README.md', 'THIRD_PARTY_NOTICES.md', 'CHANGELOG.md', 'examples/hello-plugin/README.md', 'examples/couchbase-plugin/README.md', 'examples/managed-redis-plugin/README.md', 'examples/batch-jobs-plugin/README.md', *docs]
    examples = sorted(str(path.relative_to(ROOT)) for path in (ROOT / 'examples').rglob('*') if path.is_file() and '__pycache__' not in path.parts)

    # when
    odd = [p for p in paths if not read(p).isascii()]
    odd_examples = [p for p in examples if not (ROOT / p).read_bytes().isascii()]

    # then
    assert len(docs) >= 8
    assert odd == []
    assert len(examples) >= 24 and odd_examples == []


def test_the_yaml_comment_check_sees_trailing_comments_but_not_block_text():
    # given
    commented = 'a: 1 # note\n# full line\nb: 2\n'
    block = 'a: |\n  ## heading\n  text # not a comment\nb: 2\n'

    # when
    found = yaml_comment_lines(commented)
    inside = yaml_comment_lines(block)

    # then
    assert found == [1, 2]
    assert inside == []


def test_trademarks_describes_the_attribution_line_as_the_code_renders_it():
    # given
    flat = ' '.join(read('TRADEMARKS.md').split())

    # then
    assert brand.attribution_of('Skopax')[0] == 'Skopax ' + brand.display_version() + ' \u00b7 skopax.io'
    assert '"Skopax <version>", a middle dot, then "skopax.io"' in flat
    assert 'the login and setup forms and the Chat page end with a small line' in flat
    assert 'Slack and Teams messages carry no line unless the chat attribution setting is on' in flat
    assert 'is planned as an Enterprise feature; until then it stays locked' in flat
    assert 'exists as an Enterprise feature' not in flat


def test_contributing_and_the_readme_state_the_fork_rule_and_the_licence_rule_as_trademarks_does():
    # given
    contributing = ' '.join(read('CONTRIBUTING.md').split())
    readme = ' '.join(read('README.md').split())

    # then
    assert 'Skopax Cloud and the Enterprise edition, if they come, will not be open for contributions' in contributing
    assert 'when you distribute it or offer it as a service' in contributing and 'when you distribute it or offer it as a service' in readme
    assert 'licences that add terms the AGPL does not allow' in contributing and 'MPL-2.0 is accepted for dependencies' in contributing
    assert 'We cannot accept copyleft licences' not in contributing


def test_the_license_is_agpl_3_only():
    # given
    license_text = read('LICENSE').splitlines()
    pyproject = tomllib.loads(read('pyproject.toml'))['project']
    dockerfile = read('Dockerfile')

    # then
    assert license_text[0].strip() == 'GNU AFFERO GENERAL PUBLIC LICENSE'
    assert license_text[1].strip() == 'Version 3, 19 November 2007'
    assert pyproject['license'] == 'AGPL-3.0-only'
    assert 'License :: OSI Approved :: GNU Affero General Public License v3' in pyproject['classifiers']
    assert 'org.opencontainers.image.licenses="AGPL-3.0-only"' in dockerfile
    assert 'Apache' not in read('LICENSE')
    assert 'AGPL-3.0-only' in read('NOTICE')


def test_the_plugin_exception_is_in_notice_and_named_where_plugin_authors_read():
    # given
    notice = ' '.join(read('NOTICE').split())
    readme = read('README.md')
    contributing = read('CONTRIBUTING.md')
    extending = read('docs/extending.md')

    # when
    heading = 'Additional permission under GNU AGPL version 3 section 7'

    # then
    assert heading in notice
    assert 'only through its documented plug-in interface' in notice
    assert 'under terms of your choice for those plug-ins' in notice
    assert 'does not cover Skopax itself or any modification of Skopax' in notice
    assert 'You may remove this additional permission' in notice
    assert 'GNU AFFERO GENERAL PUBLIC LICENSE' in read('LICENSE') and heading not in read('LICENSE')
    assert 'plug-in exception' in readme and 'plug-in exception' in contributing
    assert 'any licence' in extending and 'NOTICE' in extending
