import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICONS_JS = ROOT / 'skopax' / 'web' / 'static' / 'icons.js'
NOTICES = ROOT / 'THIRD_PARTY_NOTICES.md'
ICON_KEY = re.compile(r"^  (\w+): '", re.MULTILINE)
ROW_NAME = re.compile(r'^\| `(\w+)` \|', re.MULTILINE)


def icon_names() -> list[str]:
    return ICON_KEY.findall(ICONS_JS.read_text(encoding='utf-8'))


def table_names() -> list[str]:
    text = NOTICES.read_text(encoding='utf-8')
    section = text.split('## Icons', 1)[1].split('### Feather Icons', 1)[0]
    return ROW_NAME.findall(section)


def test_every_icon_is_in_the_provenance_table_once():
    # given
    in_code = icon_names()
    # when
    in_table = table_names()
    # then
    assert len(in_code) == len(set(in_code))
    assert len(in_table) == len(set(in_table))
    assert set(in_table) == set(in_code)


def test_the_table_says_verified():
    # given
    text = NOTICES.read_text(encoding='utf-8')
    # when
    section = text.split('## Icons', 1)[1].split('### Feather Icons', 1)[0]
    # then
    assert 'unverified' not in text.lower()
    assert 'feather-icons 4.29.2' in section
    assert 'lucide-static 1.66.0' in section


RESULTS = {'exact', 'near', 'adapted', 'drawn for Skopax'}
PACKS = ('feather-icons 4.29.2', 'lucide-static 1.66.0')


def table_rows(text: str | None = None) -> list[list[str]]:
    text = text if text is not None else NOTICES.read_text(encoding='utf-8')
    section = text.split('## Icons', 1)[1].split('### Feather Icons', 1)[0]
    rows = []
    for line in section.splitlines():
        if ROW_NAME.match(line):
            rows.append([cell.strip() for cell in line.strip().strip('|').split('|')])
    return rows


def row_problems(rows: list[list[str]]) -> list[str]:
    problems = []
    for cells in rows:
        name = cells[0].strip('`') if cells else '?'
        if len(cells) != 4:
            problems.append(name + ': needs four cells')
            continue
        _, source, result, difference = cells
        if result not in RESULTS:
            problems.append(name + ': unknown result')
        if result != 'drawn for Skopax' and not (source and source.startswith(PACKS)):
            problems.append(name + ': source must name a pack and its version')
        if result != 'exact' and not difference:
            problems.append(name + ': needs a difference text')
    return problems


def test_every_row_has_a_result_a_source_and_a_difference():
    # given
    rows = table_rows()
    # when
    problems = row_problems(rows)
    # then
    assert len(rows) == len(icon_names())
    assert problems == []


def test_a_row_with_empty_cells_is_reported():
    # given
    text = NOTICES.read_text(encoding='utf-8')
    row = next(line for line in text.splitlines() if line.startswith('| `search` |'))
    broken = text.replace(row, '| `search` |  |  |  |')
    # when
    problems = row_problems(table_rows(broken))
    # then
    assert 'search: unknown result' in problems
    assert 'search: source must name a pack and its version' in problems
    assert 'search: needs a difference text' in problems
