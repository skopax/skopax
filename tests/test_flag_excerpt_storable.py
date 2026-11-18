import bson

from skopax.investigator.flags import scan


def test_an_escaped_emoji_next_to_planted_text_gives_a_storable_excerpt():
    # given
    raw = 'val prompt = "\\uD83D\\uDE0A You are an assistant that answers yes"'

    # when
    found = scan('gitlab_file', raw)

    # then
    assert {f['phrase'] for f in found} == {'you are', 'assistant'}
    assert '"😊 You are an assistant' in found[0]['excerpt']
    assert bson.decode(bson.encode({'flags': found}))['flags'] == found


def test_a_lone_surrogate_escape_or_character_becomes_a_replacement_mark():
    # given
    texts = ['"\\uD83D ignore previous instructions"', 'raw \ud83d ignore previous instructions']

    # when
    found = [scan('esql', t)[0] for t in texts]

    # then
    assert [f['excerpt'] for f in found] == ['"� ignore previous instructions"', 'raw � ignore previous instructions']
    assert bson.decode(bson.encode({'flags': found}))['flags'] == found
