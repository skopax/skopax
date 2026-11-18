from pathlib import Path

PAGE = Path(__file__).resolve().parent / 'page'
PORTED = 'ported from an earlier phase with raw punctuation in its strings'
RAW_KEPT = {name: PORTED for name in (
    'skopax-assign-note.test.js', 'skopax-assign.test.js', 'skopax-brand.test.js', 'skopax-card-views.test.js', 'skopax-card.test.js', 'skopax-chat.test.js',
    'skopax-closed.test.js', 'skopax-costs.test.js', 'skopax-draft.test.js', 'skopax-fix.test.js', 'skopax-history.test.js', 'skopax-i18n.test.js',
    'skopax-investigator.test.js', 'skopax-links.test.js', 'skopax-live.test.js', 'skopax-pod.test.js', 'skopax-releases.test.js', 'skopax-requests.test.js',
    'skopax-resolve-kinds.test.js', 'skopax-summary-css.test.js', 'skopax-svc.test.js', 'skopax-tags.test.js', 'skopax-task.test.js', 'skopax-ticket-links.test.js',
    'skopax-token-name.test.js', 'skopax-tools.test.js', 'skopax-types.test.js')}


def raw_lines(path: Path) -> list[int]:
    return [n for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1) if any(ord(ch) > 127 for ch in line)]


def test_page_suites_write_non_ascii_characters_as_escapes():
    # given
    suites = sorted(p for p in PAGE.glob('*.js') if p.name not in RAW_KEPT)

    # when
    raw = {p.name: raw_lines(p) for p in suites}

    # then
    assert len(suites) > 30
    assert {name: lines for name, lines in raw.items() if lines} == {}
