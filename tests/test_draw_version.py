import dataclasses
import datetime as dt

from tests.conftest import anomaly, settings, state
from tests.fake_slack import fake_slack
from tests.test_slack_layout import posted, posts_of, rewrites
from skopax import app as app_module, cardposts, locales, messages
from skopax.app import create_app
from skopax.cardposts import drawn_sig
from skopax.slackhealth import PASS as SLACK_POSTS
from skopax.store import REDRAW_RUN, Store, iso, utc_now

KEYS = ('log|a|ERROR|x', 'log|b|ERROR|x')
NOW = 1_792_224_000.0
TS = ('1792224000.000400', '1792224000.000401')


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


def boom(*args, **kwargs):
    raise RuntimeError('the pack step failed')


def seed(store: Store) -> None:
    store.save(state(anomaly(KEYS[0], num=40), anomaly(KEYS[1], num=41, status='cleared')), None)
    for key, ts in zip(KEYS, TS):
        posted(store, key, ts)


def new_posts(api, post) -> list:
    return post.calls + [m for m, _ in api.calls if m in ('chat.postMessage', 'conversations.replies', 'chat.postEphemeral')]


def reconciled(posts) -> int:
    posts.reconcile()
    return posts.run_batch()


def speak_tr() -> None:
    messages.set_catalogs([(pack.lang, dict(pack.messages)) for pack in locales.packs()])
    messages.use_language(lambda: 'tr')


def test_the_drawn_signature_changes_with_the_draw_version_and_with_the_language(monkeypatch):
    # given
    card = anomaly(KEYS[0], num=40)
    before = drawn_sig(card, None)

    # when
    monkeypatch.setattr(cardposts, 'DRAW_VERSION', cardposts.DRAW_VERSION + 1)
    bumped = drawn_sig(card, None)
    speak_tr()
    switched = drawn_sig(card, None)

    # then
    assert len({before, bumped, switched}) == 3
    assert switched == drawn_sig(card, None), 'the same card, version and language draw the same signature'


def test_a_draw_version_bump_redraws_each_tracked_post_once_through_reconcile_and_posts_nothing_new(store: Store, monkeypatch):
    # given
    seed(store)
    slack, post, api = fake_slack()
    posts = posts_of(store, slack)
    assert reconciled(posts) == 0, 'a post drawn with the current version is left alone'

    # when
    monkeypatch.setattr(cardposts, 'DRAW_VERSION', cardposts.DRAW_VERSION + 1)
    first = reconciled(posts)
    second = reconciled(posts)

    # then
    assert (first, second) == (2, 0)
    assert sorted(u['ts'] for u in rewrites(api)) == sorted(TS)
    assert new_posts(api, post) == []


def test_a_language_switch_redraws_the_tracked_posts_once_in_the_new_language(store: Store):
    # given
    seed(store)
    slack, post, api = fake_slack()
    posts = posts_of(store, slack)

    # when
    speak_tr()
    first = reconciled(posts)
    second = reconciled(posts)

    # then
    assert (first, second) == (2, 0)
    cleared = next(u for u in rewrites(api) if u['ts'] == TS[1])
    assert messages.catalog('tr')['slack.status.cleared'] in str(cleared['blocks'])
    assert new_posts(api, post) == []


def test_the_first_start_redraw_skips_posts_reconcile_already_drew_with_the_current_version(store: Store):
    # given
    seed(store)
    store.anomalies.update_many({}, {'$set': {'slackPost.drawn.sig': 'old'}})
    slack, post, api = fake_slack()
    posts = posts_of(store, slack, pause=lambda s: None)
    assert reconciled(posts) == 2

    # when
    result = posts.redraw(stale_only=True)

    # then
    assert (result['redrawn'], result['skipped'], result['result']) == (0, 2, 'done')
    assert len(rewrites(api)) == 2 and new_posts(api, post) == []


def test_reconcile_after_the_first_start_redraw_draws_nothing_again(store: Store):
    # given
    seed(store)
    store.anomalies.update_many({}, {'$set': {'slackPost.drawn.sig': 'old'}})
    slack, post, api = fake_slack()
    posts = posts_of(store, slack, pause=lambda s: None)

    # when
    result = posts.redraw(stale_only=True)
    again = reconciled(posts)

    # then
    assert (result['redrawn'], again) == (2, 0)
    assert len(rewrites(api)) == 2 and new_posts(api, post) == []


def test_the_admin_redraw_still_redraws_posts_that_are_already_current(store: Store):
    # given
    seed(store)
    slack, _, api = fake_slack()
    posts = posts_of(store, slack, pause=lambda s: None)

    # when
    result = posts.redraw()

    # then
    assert result['redrawn'] == 2 and len(rewrites(api)) == 2


def test_the_card_post_pass_waits_for_the_language_packs_before_it_reconciles(store: Store):
    # given
    seed(store)
    store.anomalies.update_many({}, {'$set': {'slackPost.drawn.sig': 'old'}})
    slack, post, api = fake_slack()
    app = create_app(dataclasses.replace(settings(), language='tr'), store, slack=slack)
    job = {j.name: j for j in app.state.scheduler.jobs}[SLACK_POSTS]
    asked = []
    app.state.card_posts.reconcile = lambda: asked.append(messages.language())

    # when
    job.run(NOW)
    before = list(asked)
    app.state.runtime.refresh()
    job.run(NOW)

    # then
    assert before == [], 'no reconcile while the packs are not loaded, so nothing is drawn in the wrong language'
    assert asked == ['tr']


def test_reconcile_waits_while_a_redraw_holds_its_lease_and_not_for_a_stale_one(store: Store, monkeypatch):
    # given
    seed(store)
    monkeypatch.setattr(cardposts, 'DRAW_VERSION', cardposts.DRAW_VERSION + 1)
    slack, _, api = fake_slack()
    posts = posts_of(store, slack)
    store.redraw_claim('other-copy', iso(utc_now() - dt.timedelta(seconds=cardposts.REDRAW_STALE_S)))

    # when
    held = reconciled(posts)
    store.counters.update_one({'_id': REDRAW_RUN}, {'$set': {'heartbeat': iso(utc_now() - dt.timedelta(seconds=cardposts.REDRAW_STALE_S + 60))}})
    stale = reconciled(posts)

    # then
    assert (held, stale) == (0, 2)
    assert len(rewrites(api)) == 2


def test_a_failing_step_after_the_packs_load_does_not_keep_reconcile_off(store: Store, monkeypatch):
    # given
    seed(store)
    slack, _, _ = fake_slack()
    app = create_app(dataclasses.replace(settings(), language='tr'), store, slack=slack)
    monkeypatch.setattr(app_module, 'set_chat_texts', boom)
    job = {j.name: j for j in app.state.scheduler.jobs}[SLACK_POSTS]
    asked = []
    app.state.card_posts.reconcile = lambda: asked.append(messages.language())

    # when
    app.state.runtime.refresh()
    job.run(NOW)

    # then
    assert asked == ['tr']


def test_reconcile_and_the_first_start_redraw_resume_after_two_minutes_when_the_packs_never_load(store: Store, monkeypatch):
    # given
    seed(store)
    slack, _, _ = fake_slack()
    clock = Clock()
    monkeypatch.setattr(messages, 'set_catalogs', boom)
    app = create_app(settings(), store, slack=slack, clock=clock)
    job = {j.name: j for j in app.state.scheduler.jobs}[SLACK_POSTS]
    asked, started = [], []
    app.state.card_posts.reconcile = lambda: asked.append(clock())
    app.state.card_posts.redraw_start = lambda **kw: started.append(kw) or True
    app.state.runtime.refresh()

    # when
    job.run(clock())
    early = app.state.redraw_at_start()
    clock.now += app_module.LANGUAGE_WAIT_S + 1
    job.run(clock())
    late = app.state.redraw_at_start()

    # then
    assert (early, late) == (False, True)
    assert asked == [clock.now] and len(started) == 1 and started[0]['stale_only'] is True


def test_the_first_start_redraw_waits_for_the_packs(store: Store):
    # given
    seed(store)
    slack, _, _ = fake_slack()
    app = create_app(settings(), store, slack=slack)
    started = []
    app.state.card_posts.redraw_start = lambda **kw: started.append(kw) or True

    # when
    early = app.state.redraw_at_start()
    app.state.runtime.refresh()
    late = app.state.redraw_at_start()

    # then
    assert (early, late) == (False, True) and len(started) == 1
