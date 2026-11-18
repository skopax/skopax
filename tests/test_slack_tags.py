from skopax.slack import anomaly_message

AT = '2026-11-11T07:00:00Z'


def card(**extra) -> dict:
    return {'key': 'log|checkout-service|ERROR|boom', 'svc': 'checkout-service', 'label': 'ERROR lines', 'sev': 'warn', 'status': 'open', 'now': 12, 'd1': 0, 'd7': 1,
            'firstSeen': AT, 'ns': 'shop', 'num': 42, **extra}


def facts(message: dict) -> str:
    return [b for b in message['blocks'] if b['type'] == 'context'][-1]['elements'][0]['text']


def test_the_card_post_names_the_card_tags_and_leaves_them_out_when_there_are_none():
    # given
    tagged = card(tags=[{'id': 'developer', 'by': 'pod', 'at': AT}, {'id': 'seo', 'by': 'rule', 'at': AT}, {'id': 'tech', 'by': 'pod', 'at': AT}])

    # when
    with_tags = facts(anomaly_message(tagged, 'https://skopax.example.com/c/42', 'auto'))
    without = facts(anomaly_message(card(), 'https://skopax.example.com/c/42', 'auto'))

    # then
    assert 'Tags: Developer, SEO' in with_tags and 'tech' not in with_tags
    assert 'Tags:' not in without
