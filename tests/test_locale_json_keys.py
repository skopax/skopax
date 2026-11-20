import json
from collections import Counter
from pathlib import Path

import pytest

LOCALE_ROOT = Path(__file__).resolve().parent.parent / 'skopax' / 'locale'
FILES = sorted(LOCALE_ROOT.glob('*/*.json'))


def repeated(pairs):
    names = Counter(key for key, _ in pairs)
    dupes = sorted(key for key, count in names.items() if count > 1)
    if dupes:
        raise ValueError(dupes)
    return dict(pairs)


def test_locale_files_are_found():
    # given
    names = {path.parent.name for path in FILES}

    # then
    assert {'de', 'es', 'fr', 'tr'} <= names


def test_repeated_key_is_caught_at_any_depth():
    # given
    text = '{"a": {"x": 1, "x": 2}}'

    # when
    with pytest.raises(ValueError) as caught:
        json.loads(text, object_pairs_hook=repeated)

    # then
    assert caught.value.args[0] == ['x']


def test_same_key_in_two_sections_is_not_a_repeat():
    # given
    text = '{"page": {"Requests": "a"}, "headers": {"Requests": "b"}}'

    # then
    assert json.loads(text, object_pairs_hook=repeated)['headers']['Requests'] == 'b'


@pytest.mark.parametrize('path', FILES, ids=lambda p: f'{p.parent.name}/{p.name}')
def test_pack_json_has_no_duplicate_keys(path):
    # when
    text = path.read_text(encoding='utf-8')

    # then
    try:
        json.loads(text, object_pairs_hook=repeated)
    except ValueError as exc:
        pytest.fail(f'{path.parent.name}/{path.name} repeats keys {exc}')
