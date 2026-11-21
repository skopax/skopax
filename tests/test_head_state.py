from types import SimpleNamespace

import pytest

from tests.conftest import anomaly, state
from tests.fake_slack import fake_slack
from tests.test_slack_layout import BASE, CARD, LINK, bar_of, head_of, posted, rewrites
from skopax import locales, messages
from skopax.cardposts import CardPosts
from skopax.slack import SlackSlowed, anomaly_message
from skopax.tagreport import SlackIds
from skopax import actions
from skopax.store import Store

KEY = 'log|checkout-service|ERROR|boom'
TS = '1792224000.000200'
OLD = '2026-11-01T07:00:00Z'
NAMES = {'alice@example.com': 'Alice Smith', 'carol@example.com': 'carol@example.com'}
CLEARED = CARD | {'status': 'cleared'}
DOT = '\u00b7'


def name_of(email: str) -> str:
    return NAMES.get(email, '')


def seed(store: Store, head: dict | None = None, **extra) -> CardPosts:
    store.save(state(anomaly(KEY, num=20, **extra)), None)
    posted(store, KEY, TS)
    if head:
        store.slack_post_head(KEY, TS, head)
    slack, _, api = fake_slack()
    posts = CardPosts(store, slack, BASE, autostart=False, name_of=name_of)
    posts.api = api
    return posts


def stored_head(store: Store) -> dict:
    return store.anomaly(KEY)['slackPost']['head']


@pytest.fixture
def speak():
    def use(lang: str) -> None:
        messages.set_catalogs([(pack.lang, dict(pack.messages)) for pack in locales.packs()])
        messages.use_language(lambda: lang)
    yield use
    messages.reset()


def test_a_resolved_head_on_a_reopened_taken_card_that_stopped_firing_is_replaced_on_the_everyday_rewrite(store: Store):
    # given
    posts = seed(store, {'kind': 'resolved', 'by': 'bob@example.com', 'at': OLD}, status='cleared', triage='progress', owner='alice@example.com')

    # when
    posts.reconcile()
    posts.run_batch()

    # then
    sent = rewrites(posts.api)
    assert [head_of(u) for u in sent] == [f':large_yellow_circle: #20 stopped firing {DOT} Alice Smith is on it']
    assert bar_of(sent[0])[0].startswith(f'Stopped firing {DOT} Alice Smith is on it {DOT} ')
    assert stored_head(store)['kind'] == 'cleared' and stored_head(store)['at'] == '2026-11-11T07:00:00Z'


def test_a_refresh_after_the_fix_redraws_the_same_post_and_keeps_the_fixed_head(store: Store):
    # given
    posts = seed(store, {'kind': 'resolved', 'by': 'bob@example.com', 'at': OLD}, status='cleared', triage='progress', owner='alice@example.com')
    posts.reconcile()
    posts.run_batch()

    # when
    posts.reconcile()
    posts.run_batch()
    posts.refresh(KEY)
    posts.run_batch()
    again = len(rewrites(posts.api))

    # then
    assert again == 2 and rewrites(posts.api)[0] == rewrites(posts.api)[1]
    assert stored_head(store)['kind'] == 'cleared'


def test_a_reconcile_after_the_fix_has_nothing_to_do(store: Store):
    # given
    posts = seed(store, {'kind': 'resolved', 'by': 'bob@example.com', 'at': OLD}, status='cleared', triage='progress', owner='alice@example.com')
    posts.reconcile()
    posts.run_batch()

    # when
    posts.reconcile()
    posts.run_batch()

    # then
    assert len(rewrites(posts.api)) == 1


def test_a_stale_took_head_on_a_card_nobody_has_is_replaced(store: Store):
    # given
    posts = seed(store, {'kind': 'took', 'by': 'alice@example.com', 'at': OLD})

    # when
    posts.refresh(KEY)
    posts.run_batch()

    # then
    head = head_of(rewrites(posts.api)[0])
    assert 'took' not in head and stored_head(store)['kind'] == 'opened', head


def test_a_muted_head_after_unmute_is_replaced(store: Store):
    # given
    posts = seed(store, {'kind': 'muted', 'by': 'alice@example.com', 'at': OLD})

    # when
    posts.reconcile()
    posts.run_batch()

    # then
    head = head_of(rewrites(posts.api)[0])
    assert 'muted' not in head and stored_head(store)['kind'] == 'opened', head


def test_a_cleared_head_on_a_card_firing_again_is_replaced(store: Store):
    # given
    posts = seed(store, {'kind': 'cleared', 'by': 'server', 'at': OLD})

    # when
    posts.reconcile()
    posts.run_batch()

    # then
    head = head_of(rewrites(posts.api)[0])
    assert 'stopped firing' not in head and stored_head(store)['kind'] == 'opened', head


def test_a_took_head_that_still_fits_keeps_who_took_it(store: Store):
    # given
    posts = seed(store, {'kind': 'took', 'by': 'alice@example.com', 'at': OLD}, triage='progress', owner='alice@example.com')

    # when
    posts.refresh(KEY)
    posts.run_batch()

    # then
    assert head_of(rewrites(posts.api)[0]).endswith('alice took #20') and stored_head(store)['at'] == OLD


def test_a_took_head_becomes_the_is_on_it_head_once_the_taken_card_stops_firing(store: Store):
    # given
    posts = seed(store, {'kind': 'took', 'by': 'alice@example.com', 'at': OLD}, status='cleared', triage='progress', owner='alice@example.com')

    # when
    posts.refresh(KEY)
    posts.run_batch()

    # then
    assert head_of(rewrites(posts.api)[0]) == f':large_yellow_circle: #20 stopped firing {DOT} Alice Smith is on it'
    assert stored_head(store)['kind'] == 'cleared'


def test_a_stopped_card_reads_unassigned_assigned_or_taken():
    # when
    alone = anomaly_message(CLEARED, LINK, 'server', head={'kind': 'cleared'}, name_of=name_of)
    assigned = anomaly_message(CLEARED | {'owner': 'bob@example.com'}, LINK, 'server', head={'kind': 'cleared'}, name_of=name_of)
    taken = anomaly_message(CLEARED | {'owner': 'alice@example.com', 'triage': 'progress'}, LINK, 'server', head={'kind': 'cleared'}, name_of=name_of)

    # then
    assert head_of(alone) == f':large_yellow_circle: #14 stopped firing {DOT} unassigned'
    assert head_of(assigned) == f':large_yellow_circle: #14 stopped firing {DOT} awaiting action {DOT} assigned to bob'
    assert head_of(taken) == f':large_yellow_circle: #14 stopped firing {DOT} Alice Smith is on it'
    assert bar_of(alone)[0].startswith(f'Stopped firing {DOT} unassigned {DOT} Warning')
    assert bar_of(assigned)[0].startswith(f'Stopped firing {DOT} awaiting action {DOT} assigned to bob {DOT} Warning')
    assert bar_of(taken)[0].startswith(f'Stopped firing {DOT} Alice Smith is on it {DOT} Warning')
    assert 'Assignee' not in bar_of(assigned)[0] + bar_of(taken)[0]
    assert alone['text'].startswith(f'Stopped firing {DOT} unassigned') and taken['text'].startswith(f'Stopped firing {DOT} Alice Smith is on it')


def test_who_is_never_an_email():
    # when
    named_email = anomaly_message(CLEARED | {'owner': 'carol@example.com', 'triage': 'progress'}, LINK, 'server', head={'kind': 'cleared'}, name_of=name_of)
    nobody = anomaly_message(CLEARED | {'triage': 'progress'}, LINK, 'server', head={'kind': 'cleared'}, name_of=name_of)
    unlooked = anomaly_message(CLEARED | {'owner': 'dave@example.com', 'triage': 'progress'}, LINK, 'server', head={'kind': 'cleared'})

    # then
    assert head_of(named_email).endswith(f'stopped firing {DOT} carol is on it')
    assert head_of(nobody).endswith(f'stopped firing {DOT} someone is on it')
    assert head_of(unlooked).endswith(f'stopped firing {DOT} dave is on it')
    assert '@' not in ' '.join(head_of(m) + bar_of(m)[0] for m in (named_email, nobody, unlooked))


def test_the_event_variant_follows_the_same_three_endings():
    # given
    event = {'kind': 'event', 'what': 'deploy', 'firstSeen': CARD['firstSeen']}
    card = CLEARED | {'cause': event}

    # when
    alone = bar_of(anomaly_message(card, LINK, 'server', head={'kind': 'cleared'}))[0]
    assigned = bar_of(anomaly_message(card | {'owner': 'bob@example.com'}, LINK, 'server', head={'kind': 'cleared'}))[0]
    taken = bar_of(anomaly_message(card | {'owner': 'bob@example.com', 'triage': 'progress'}, LINK, 'server', head={'kind': 'cleared'}))[0]

    # then
    assert alone.startswith(f'Stopped after the event {DOT} unassigned')
    assert assigned.startswith(f'Stopped after the event {DOT} awaiting action {DOT} assigned to bob')
    assert taken.startswith(f'Stopped after the event {DOT} bob is on it')


def test_the_turkish_words_are_exact(speak):
    # given
    speak('tr')

    # when
    alone = anomaly_message(CLEARED, LINK, 'server', head={'kind': 'cleared'})
    assigned = anomaly_message(CLEARED | {'owner': 'bob@example.com'}, LINK, 'server', head={'kind': 'cleared'})
    taken = anomaly_message(CLEARED | {'owner': 'alice@example.com', 'triage': 'progress'}, LINK, 'server', head={'kind': 'cleared'}, name_of=name_of)
    nobody = anomaly_message(CLEARED | {'triage': 'progress'}, LINK, 'server', head={'kind': 'cleared'})

    # then
    assert head_of(alone) == ':large_yellow_circle: #14 sinyal durdu \u00b7 atama bekleniyor'
    assert head_of(assigned) == ':large_yellow_circle: #14 sinyal durdu \u00b7 aksiyon bekleniyor \u00b7 atanan: bob'
    assert head_of(taken) == ':large_yellow_circle: #14 sinyal durdu \u00b7 Alice Smith ilgileniyor'
    assert head_of(nobody) == ':large_yellow_circle: #14 sinyal durdu \u00b7 biri ilgileniyor'
    assert bar_of(alone)[0].startswith('Sinyal durdu \u00b7 atama bekleniyor \u00b7 ')
    assert bar_of(assigned)[0].startswith('Sinyal durdu \u00b7 aksiyon bekleniyor \u00b7 atanan: bob \u00b7 ')
    assert bar_of(taken)[0].startswith('Sinyal durdu \u00b7 Alice Smith ilgileniyor \u00b7 ')


@pytest.mark.parametrize('lang,words', [
    ('tr', ('{card} sinyal durdu \u00b7 atama bekleniyor', '{card} sinyal durdu \u00b7 aksiyon bekleniyor \u00b7 atanan: {who}', '{card} sinyal durdu \u00b7 {who} ilgileniyor',
            'Olaydan sonra sinyal durdu \u00b7 atama bekleniyor', 'biri', 'Olaydan sonra sinyal durdu \u00b7 aksiyon bekleniyor \u00b7 atanan: {who}', 'Olaydan sonra sinyal durdu \u00b7 {who} ilgileniyor')),
    ('de', ('{card} nicht mehr aktiv \u00b7 nicht zugewiesen', '{card} nicht mehr aktiv \u00b7 Aktion ausstehend \u00b7 zugewiesen: {who}', '{card} nicht mehr aktiv \u00b7 {who} k\u00fcmmert sich',
            'Nach dem Ereignis beendet \u00b7 nicht zugewiesen', 'jemand', 'Nach dem Ereignis beendet \u00b7 Aktion ausstehend \u00b7 zugewiesen: {who}', 'Nach dem Ereignis beendet \u00b7 {who} k\u00fcmmert sich')),
    ('es', ('{card} dej\u00f3 de dispararse \u00b7 sin asignar', '{card} dej\u00f3 de dispararse \u00b7 pendiente de acci\u00f3n \u00b7 asignada a {who}', '{card} dej\u00f3 de dispararse \u00b7 {who} se encarga',
            'Detenida tras el evento \u00b7 sin asignar', 'alguien', 'Detenida tras el evento \u00b7 pendiente de acci\u00f3n \u00b7 asignada a {who}', 'Detenida tras el evento \u00b7 {who} se encarga')),
    ('fr', ('{card} n\u2019\u00e9met plus \u00b7 non assign\u00e9e', '{card} n\u2019\u00e9met plus \u00b7 action attendue \u00b7 assign\u00e9e \u00e0 {who}', '{card} n\u2019\u00e9met plus \u00b7 {who} s\u2019en occupe',
            'Arr\u00eat\u00e9e apr\u00e8s l\u2019\u00e9v\u00e9nement \u00b7 non assign\u00e9e', 'quelqu\u2019un', 'Arr\u00eat\u00e9e apr\u00e8s l\u2019\u00e9v\u00e9nement \u00b7 action attendue \u00b7 assign\u00e9e \u00e0 {who}',
            'Arr\u00eat\u00e9e apr\u00e8s l\u2019\u00e9v\u00e9nement \u00b7 {who} s\u2019en occupe')),
])
def test_every_pack_carries_the_three_endings(lang, words):
    # given
    pack = locales.read(lang).messages

    # then
    alone, assigned, taken, event, someone, event_assigned, event_taken = words
    assert (pack['slack.head.cleared'], pack['slack.head.cleared_assigned'], pack['slack.head.cleared_taken']) == (alone, assigned, taken)
    assert pack['slack.status.cleared'] == alone.replace('{card} ', '')[:1].upper() + alone.replace('{card} ', '')[1:]
    assert pack['slack.status.cleared_assigned'] == assigned.replace('{card} ', '')[:1].upper() + assigned.replace('{card} ', '')[1:]
    assert pack['slack.status.cleared_taken'] == taken.replace('{card} ', '')[:1].upper() + taken.replace('{card} ', '')[1:]
    assert pack['slack.status.event'] == event and pack['slack.someone'] == someone
    assert (pack['slack.status.event_assigned'], pack['slack.status.event_taken']) == (event_assigned, event_taken)


@pytest.mark.parametrize('extra,mute', [({'triage': 'handled'}, 'mute'), ({'autoResolvedAt': OLD}, None), ({'triage': 'progress', 'status': 'cleared'}, 'mute'), ({'status': 'cleared'}, 'known')])
def test_every_replacement_head_fits_so_a_second_reconcile_sends_nothing(store: Store, extra, mute):
    # given
    posts = seed(store, {'kind': 'opened', 'by': 'server', 'at': OLD} if extra.get('triage') != 'handled' else {'kind': 'took', 'by': 'alice@example.com', 'at': OLD}, **extra)
    if mute:
        store.mute(KEY, 'quiet for now', None if mute == 'known' else '2099-01-01T00:00:00Z', mute)
    posts.reconcile()
    posts.run_batch()
    first = len(rewrites(posts.api))

    # when
    posts.reconcile()
    posts.run_batch()

    # then
    assert first == 1 and len(rewrites(posts.api)) == 1, stored_head(store)


def test_a_replaced_head_on_a_taken_card_firing_again_names_who_took_it(store: Store):
    # given
    posts = seed(store, {'kind': 'cleared', 'by': 'server', 'at': OLD}, triage='progress', owner='alice@example.com')

    # when
    posts.reconcile()
    posts.run_batch()

    # then
    assert head_of(rewrites(posts.api)[0]) == ':large_orange_circle: alice took #20'
    assert stored_head(store)['kind'] == 'took'


def test_a_fresh_head_that_does_not_fit_keeps_the_stored_head_that_does(store: Store):
    # given
    posts = seed(store, {'kind': 'resolved', 'by': 'alice@example.com', 'at': OLD}, status='cleared', triage='handled')

    # when
    posts.swept(SimpleNamespace(events=[{'key': KEY, 'ev': 'cleared'}], resolved=[]))
    posts.run_batch()

    # then
    assert head_of(rewrites(posts.api)[0]) == ':white_check_mark: alice resolved #20' and stored_head(store)['kind'] == 'resolved'


def test_an_auto_closed_head_is_kept_after_a_later_mute_or_assign(store: Store):
    # given
    auto = {'kind': 'auto', 'span': '2 days', 'by': 'server', 'at': OLD}
    posts = seed(store, auto, status='cleared', triage='handled', autoResolvedAt=OLD)

    # when
    store.mute(KEY, 'quiet for now', '2099-01-01T00:00:00Z', 'mute')
    posts.after({'type': 'mute', 'key': KEY, 'requestedBy': 'alice@example.com'})
    posts.run_batch()
    posts.assigned(KEY, 'Bob Smith', 'alice@example.com')
    posts.run_batch()

    # then
    heads = [head_of(u) for u in rewrites(posts.api)]
    assert len(heads) == 2 and all('closed automatically' in h for h in heads) and stored_head(store) == auto, heads


def test_an_assign_on_a_stopped_card_ends_as_the_stopped_head_naming_the_assignee(store: Store):
    # given
    posts = seed(store, None, status='cleared', owner='bob@example.com')

    # when
    posts.assigned(KEY, 'bob@example.com', 'alice@example.com')
    posts.run_batch()

    # then
    assert head_of(rewrites(posts.api)[0]) == f':large_yellow_circle: #20 stopped firing {DOT} awaiting action {DOT} assigned to bob'
    assert stored_head(store)['kind'] == 'cleared'


def test_a_jira_reopened_head_on_a_stopped_card_ends_as_the_stopped_head(store: Store):
    # given
    posts = seed(store, None, status='cleared')

    # when
    posts.jira([{'card': KEY}])
    posts.run_batch()

    # then
    assert head_of(rewrites(posts.api)[0]) == f':large_yellow_circle: #20 stopped firing {DOT} unassigned'


def test_a_triaged_took_head_names_the_owner(store: Store):
    # given
    posts = seed(store, None, triage='progress', owner='alice@example.com')

    # when
    posts.triaged(KEY, {})
    posts.run_batch()

    # then
    assert head_of(rewrites(posts.api)[0]) == ':large_orange_circle: alice took #20'


def test_who_is_resolved_once_per_draw():
    # given
    calls = []

    def counted(email):
        calls.append(email)
        return 'Alice Smith'

    # when
    message = anomaly_message(CLEARED | {'owner': 'alice@example.com', 'triage': 'progress'}, LINK, 'server', head={'kind': 'cleared'}, name_of=counted)

    # then
    assert calls == ['alice@example.com'] and 'Alice Smith' in head_of(message) and 'Alice Smith' in bar_of(message)[0] and 'Alice Smith' in message['text']


@pytest.mark.parametrize('extra,mute,lead', [({'triage': 'handled'}, None, 'Resolved'), ({}, 'mute', 'Muted'), ({}, 'known', 'Known noise')])
def test_the_fallback_text_of_a_closed_or_muted_stopped_card_never_says_awaiting_action(extra, mute, lead):
    # when
    message = anomaly_message(CLEARED | {'owner': 'bob@example.com'} | extra, LINK, 'server', head={'kind': 'cleared'}, mute=mute)

    # then
    assert message['text'].startswith(lead + f' {DOT} ') and 'awaiting action' not in message['text']


class FlakySlack:
    def __init__(self):
        self.enabled = True
        self.calls = 0

    def user_by_email(self, email):
        self.calls += 1
        if self.calls == 1:
            raise SlackSlowed(5)
        return {'id': 'U1', 'name': 'Alice Smith'}


def test_a_slow_slack_lookup_while_drawing_is_unknown_this_time_and_does_not_stop_later_lookups():
    # given
    ids = SlackIds(FlakySlack())

    # when
    first = ids.name_now('alice@example.com')
    second = ids.name_now('alice@example.com')

    # then
    assert (first, second) == ('', 'Alice Smith') and ids.slack.calls == 2


def test_a_post_is_redrawn_once_when_the_name_becomes_known(store: Store):
    # given
    known = {}
    posts = seed(store, {'kind': 'cleared', 'by': 'server', 'at': OLD}, status='cleared', triage='progress', owner='dave@example.com')
    posts.name_of = lambda email: known.get(email, '')
    posts.refresh(KEY)
    posts.run_batch()

    # when
    posts.reconcile()
    posts.run_batch()
    known['dave@example.com'] = 'Dave Jones'
    posts.reconcile()
    posts.run_batch()
    posts.reconcile()
    posts.run_batch()

    # then
    heads = [head_of(u) for u in rewrites(posts.api)]
    assert heads == [f':large_yellow_circle: #20 stopped firing {DOT} dave is on it', f':large_yellow_circle: #20 stopped firing {DOT} Dave Jones is on it'], heads


def test_a_take_keeps_the_display_name_of_who_took_it_without_marking_it_assigned(store: Store):
    # given
    store.save(state(anomaly(KEY, num=20, status='cleared')), None)

    # when
    actions.apply(store, {'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com'}, name_of=name_of)

    # then
    card = store.anomaly(KEY)
    message = anomaly_message(card, LINK, 'server', head={'kind': 'cleared'})
    assert (card['owner'], card['triage'], card['ownerName']) == ('alice@example.com', 'progress', 'Alice Smith') and 'assigned' not in card
    assert head_of(message).endswith(f'#20 stopped firing {DOT} Alice Smith is on it') and bar_of(message)[0].startswith(f'Stopped firing {DOT} Alice Smith is on it')


def test_create_task_keeps_the_display_name_of_who_created_it(store: Store):
    # given
    from tests.conftest import settings
    from tests.test_tickets import FakeJira
    store.save(state(anomaly(KEY, num=20, status='cleared')), None)

    # when
    actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'requestedBy': 'alice@example.com'}, FakeJira(), settings(), 'https://skopax.example.com', name_of=name_of)

    # then
    card = store.anomaly(KEY)
    assert (card['owner'], card['triage'], card['ownerName']) == ('alice@example.com', 'progress', 'Alice Smith') and 'assigned' not in card
    assert head_of(anomaly_message(card, LINK, 'server', head={'kind': 'cleared'})).endswith(f'stopped firing {DOT} Alice Smith is on it')


def test_a_jira_link_on_a_taken_card_still_takes_the_jira_developer(store: Store):
    # given
    from tests.conftest import jira_links
    from tests.test_jiralinks import FakeJira, issue
    store.save(state(anomaly(KEY, num=20)), None)
    actions.apply(store, {'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com'}, name_of=name_of)
    jira = FakeJira([issue('OPS-126', 'In Development', 'https://skopax.example.com/#a=' + KEY.replace('|', '%7C'), 'Oscar Green')])

    # when
    jira_links(store, jira).run_once()

    # then
    card = store.anomaly(KEY)
    assert (card['owner'], card['ticket'], card.get('ownerName')) == ('Oscar Green', 'OPS-126', 'Oscar Green')


def test_an_assign_or_unassign_clears_the_display_name_a_take_left(store: Store):
    # given
    from tests.test_assign import assigner
    from tests.test_slack import FakeSlack
    store.save(state(anomaly(KEY, num=20)), None)
    actions.apply(store, {'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com'}, name_of=name_of)
    a = assigner(store, FakeSlack())

    # when
    a.assign(KEY, 'Bob Smith', None, 'alice@example.com')
    named = store.anomaly(KEY).get('ownerName')
    actions.apply(store, {'type': 'take', 'key': KEY, 'requestedBy': 'alice@example.com', 'owner': 'alice@example.com'}, name_of=name_of)
    a.assign(KEY, '', None, 'alice@example.com')

    # then
    assert named is None and 'ownerName' not in store.anomaly(KEY)


class RefusingSlack:
    def __init__(self):
        self.enabled = True
        self.calls = 0

    def user_by_email(self, email):
        self.calls += 1
        if self.calls == 1:
            from skopax.slack import SlackError
            raise SlackError('refused', code='ratelimited_other')
        return {'id': 'U1', 'name': 'Alice Smith'}


def test_a_refused_slack_lookup_while_drawing_does_not_stop_later_lookups():
    # given
    ids = SlackIds(RefusingSlack())

    # when
    first = ids.name_now('alice@example.com')
    second = ids.name_now('alice@example.com')

    # then
    assert (first, second) == ('', 'Alice Smith') and ids.slack.calls == 2
