from skopax import settingcards
from skopax.tunables import STATIC, Tunable
from tests.conftest import AUTH


def test_every_core_setting_of_a_carded_section_sits_in_a_named_card():
    # given
    carded = [t for t in STATIC if t.group in settingcards.CARDS and not t.hidden]

    # when
    loose = [t.key for t in carded if settingcards.placed(t)['card'] == settingcards.MORE]

    # then
    assert len(carded) > 60 and loose == []


def test_each_card_lists_only_known_keys_of_its_own_section_once():
    # given
    known = {t.key: t.group for t in STATIC}

    # when
    listed = [(section, key) for section, cards in settingcards.CARDS.items() for _, keys in cards for key in keys]

    # then
    assert [(s, k) for s, k in listed if known.get(k) != s] == []
    assert len({k for _, k in listed}) == len(listed)


def test_the_investigator_section_has_the_agreed_cards_in_order():
    # when
    titles = [title for title, _ in settingcards.CARDS['Investigator']]

    # then
    assert titles == ['Provider', 'Models', 'Spend and limits', 'Web', 'Behaviour', 'Langfuse tracing']
    assert [title for title, _ in settingcards.CARDS['Slack']] == ['Channel and posting', 'Questions', 'Threads and reminders']


def test_an_unknown_key_falls_back_to_more_and_plugin_rules_to_thresholds():
    # given
    stray = Tunable('investigate.new-thing', '', 'Investigator', 'New thing', 'help', 'int')
    rules = Tunable('demo.lag', '', 'Demo', 'Lag', 'help', 'rules', plugin='demo')
    plain = Tunable('demo.every', '', 'Demo', 'Every (min)', 'help', 'int', plugin='demo')

    # when
    out = [settingcards.placed(t)['card'] for t in (stray, rules, plain)]

    # then
    assert out == [settingcards.MORE, 'Thresholds', 'Settings']


def test_numbers_carry_a_unit_from_the_table_or_the_label():
    # given
    by_key = {t.key: t for t in STATIC}

    # when
    units = {k: settingcards.placed(by_key[k]).get('unit') for k in ('investigate.daily-usd', 'slack.thread-hours', 'slack.clear-note-minutes', 'mcp.wait-seconds', 'page.ask-cap',
                                                                     'cards.archive-hours', 'recurring.days', 'investigate.max-cards', 'general.timezone')}

    # then
    assert units == {'investigate.daily-usd': '$', 'slack.thread-hours': 'h', 'slack.clear-note-minutes': 'min', 'mcp.wait-seconds': 's', 'page.ask-cap': 'per hour',
                     'cards.archive-hours': 'h', 'recurring.days': 'days', 'investigate.max-cards': 'cards', 'general.timezone': None}


def test_the_settings_api_and_a_save_answer_carry_card_and_unit(client):
    # when
    items = {i['key']: i for i in client.get('/api/admin/settings', headers=AUTH).json()['items']}
    saved = client.put('/api/admin/settings', json={'key': 'slack.thread-hours', 'value': '6'}, headers=AUTH).json()['item']

    # then
    assert (items['slack.thread-hours']['card'], items['slack.thread-hours']['unit']) == ('Threads and reminders', 'h')
    assert items['general.timezone']['card'] == 'Time' and 'unit' not in items['general.timezone']
    inv = [i for i in items.values() if i['group'] == 'Investigator']
    titles = [t for t, _ in settingcards.CARDS['Investigator']]
    by_order = sorted(inv, key=lambda i: i['order'])
    assert [i['card'] for i in by_order] == sorted([i['card'] for i in inv], key=titles.index), 'order sorts a section card by card, in table order'
    assert items['investigate.model']['order'] < items['investigate.effort']['order'], 'and key by key inside a card'
    assert (saved['card'], saved['unit']) == ('Threads and reminders', 'h')


def test_a_unit_taken_from_the_label_gives_a_short_label_without_it():
    # given
    by_key = {t.key: t for t in STATIC}

    # when
    shown = {k: settingcards.placed(by_key[k]).get('shortLabel') for k in ('slack.thread-hours', 'investigate.daily-usd', 'chat.max-chars', 'page.ask-cap', 'self-review.hour', 'general.timezone')}

    # then
    assert shown == {'slack.thread-hours': 'Thread window', 'investigate.daily-usd': 'Daily limit', 'chat.max-chars': 'Chat message length', 'page.ask-cap': None, 'self-review.hour': None,
                     'general.timezone': None}


def test_no_hidden_tunable_is_listed_on_a_card():
    # given
    hidden = {t.key for t in STATIC if t.hidden}

    # when
    listed = {key for cards in settingcards.CARDS.values() for _, keys in cards for key in keys}

    # then
    assert hidden and sorted(hidden & listed) == []
