import pytest

from skopax.contract import iso
from skopax.kit.browser import BrowserDown
from skopax.plugins import website
from skopax.plugins.website import pagescan as ps
from tests.test_pagescan import CHAIRS, NOW, Ctx, FakeBrowser, probe, run

MINUTE = 60


class ReleaseCtx(Ctx):
    def __init__(self, deploys=(), services='shop-*', previous=None, last_status=None, **settings):
        super().__init__(FakeBrowser({CHAIRS: probe('Garden chairs for every patio')}), previous=previous, config={'release-services': services},
                         **({'extra': (CHAIRS,), 'release-delay': 10} | settings))
        self.rows = list(deploys)
        self.asked = []
        self.status = last_status

    def last_status(self):
        return self.status

    def deploys(self, since):
        self.asked.append(since)
        return [d for d in self.rows if ps.seconds_of(d['at']) >= since]


def deploy(svc, minutes_after_scan, ns='web'):
    return {'ns': ns, 'svc': svc, 'at': iso(NOW + minutes_after_scan * MINUTE), 'toImage': f'{svc}:2'}


def after_first_scan(deploys, services='shop-*', **settings):
    prior = run(ReleaseCtx(services=services, **settings))
    return ReleaseCtx(deploys, services, {ps.SECTION: prior.sections[ps.SECTION]}, **settings)


def scanned(sweep):
    return ps.SECTION in sweep.sections


def test_a_matching_release_scans_once_the_delay_has_passed_even_inside_the_scan_hours():
    # given
    ctx = after_first_scan([deploy('shop-front', 5)])

    # when
    ctx.now = NOW + 14 * MINUTE
    early = run(ctx)
    ctx.now = NOW + 15 * MINUTE
    due = run(ctx)

    # then
    assert not scanned(early) and early.result.ran == (ps.NO_DATA,)
    assert scanned(due)


def test_several_releases_inside_the_delay_give_one_scan_after_the_last_one_and_none_after_that():
    # given
    ctx = after_first_scan([deploy('shop-front', 5), deploy('shop-cart', 9)])

    # when
    ctx.now = NOW + 18 * MINUTE
    early = run(ctx)
    ctx.now = NOW + 19 * MINUTE
    due = run(ctx)
    again = ReleaseCtx(ctx.rows, previous={ps.SECTION: due.sections[ps.SECTION]})
    again.now = NOW + 25 * MINUTE
    later = run(again)

    # then
    assert not scanned(early) and scanned(due) and not scanned(later)


def test_a_service_that_is_not_listed_or_a_field_left_empty_never_scans_inside_the_scan_hours():
    # given
    other = after_first_scan([deploy('billing', 5)])
    empty = after_first_scan([deploy('shop-front', 5)], services='')
    other.now = empty.now = NOW + 30 * MINUTE

    # when
    quiet = [run(other), run(empty)]

    # then
    assert [scanned(s) for s in quiet] == [False, False] and empty.asked == []


def test_globs_match_the_service_or_namespace_slash_service_without_regard_to_case_and_the_delay_setting_counts():
    # given
    ctx = after_first_scan([deploy('Cart', 5, ns='Web')], services='x-*, web/c*', **{'release-delay': 30})

    # when
    ctx.now = NOW + 34 * MINUTE
    early = run(ctx)
    ctx.now = NOW + 35 * MINUTE
    due = run(ctx)

    # then
    assert not scanned(early) and scanned(due)


def test_a_release_older_than_the_last_scan_by_more_than_the_delay_is_already_covered():
    # given
    ctx = after_first_scan([deploy('shop-front', -20)])
    ctx.now = NOW + 30 * MINUTE

    # when
    sweep = run(ctx)

    # then
    assert not scanned(sweep)


def test_the_delay_setting_is_declared_from_1_to_120_minutes_with_default_10():
    # given
    # when
    found = {s.key: s for s in website.PLUGIN.settings}['release-delay']
    field = next(f for f in website.PLUGIN.fields if f.key == 'release-services')

    # then
    assert (found.default, found.low, found.high, found.kind) == ('10', 1, 120, 'int') and field.kind == 'text'


def failing(deploys=(), previous=None, now=NOW, last_status=None):
    ctx = ReleaseCtx(deploys, previous=previous, now=now, last_status=last_status)
    ctx.browser = FakeBrowser({}, fail={CHAIRS: BrowserDown('the screenshot browser does not answer (refused)')})
    return ctx


def old_scan():
    prior = run(ReleaseCtx(now=NOW - 3 * 3600))
    return {ps.SECTION: prior.sections[ps.SECTION]}


def test_a_failed_scan_is_not_tried_again_at_the_next_wake_and_keeps_showing_its_error_until_scan_hours_pass():
    # given
    previous = old_scan()
    failed = run(failing(previous=previous))

    # when
    wake = failing(previous=previous, now=NOW + 5 * MINUTE, last_status=failed.result.status)
    soon = run(wake)
    late = failing(previous=previous, now=NOW + 3600, last_status=failed.result.status)
    again = run(late)

    # then
    assert failed.result.ok is False and failed.result.status.startswith(f'error: pages (tried {iso(NOW)}):')
    assert wake.browser.calls == [] and (soon.result.ok, soon.result.status) == (False, failed.result.status)
    assert len(late.browser.calls) == 1 and again.result.ok is False and again.result.status.startswith(f'error: pages (tried {iso(NOW + 3600)}):')


def test_a_release_does_not_retry_a_failed_scan_before_scan_hours_have_passed():
    # given
    previous = old_scan()
    failed = run(failing(previous=previous))

    # when
    wake = failing([deploy('shop-front', -2)], previous=previous, now=NOW + 20 * MINUTE, last_status=failed.result.status)
    sweep = run(wake)

    # then
    assert wake.browser.calls == [] and (sweep.result.ok, sweep.result.status) == (False, failed.result.status)


def test_an_old_error_status_without_a_try_time_or_a_status_that_is_ok_does_not_hold_the_scan():
    # given
    previous = old_scan()

    # when
    plain = failing(previous=previous, now=NOW + 5 * MINUTE, last_status='error: pages: no page was read (old)')
    fine = failing(previous=previous, now=NOW + 5 * MINUTE, last_status='ok')
    run(plain)
    run(fine)

    # then
    assert len(plain.browser.calls) == 1 and len(fine.browser.calls) == 1


def test_a_scan_with_no_pages_to_read_counts_as_tried():
    # given
    previous = old_scan()
    empty = ReleaseCtx(previous=previous, extra=())
    first = run(empty)
    again = ReleaseCtx(previous={ps.SECTION: first.sections[ps.SECTION]}, extra=(), now=NOW + 5 * MINUTE)

    # when
    second = run(again)

    # then
    assert ps.SECTION in first.sections and second.result.ran == (ps.NO_DATA,)


def test_a_deploy_shortly_before_the_last_scan_still_scans_once_its_delay_has_passed():
    # given
    ctx = after_first_scan([deploy('shop-front', -5)])

    # when
    ctx.now = NOW + 4 * MINUTE
    early = run(ctx)
    ctx.now = NOW + 5 * MINUTE
    due = run(ctx)

    # then
    assert not scanned(early) and scanned(due)


@pytest.mark.parametrize('text', ['shop-*, web/cart?', 'shop-front', '', 'web/' + 'a' * 96])
def test_release_services_that_are_plain_globs_are_taken(text):
    # given
    field = next(f for f in website.PLUGIN.fields if f.key == 'release-services')

    # then
    assert field.check(text) is None


@pytest.mark.parametrize('text', ['shop front', 'shop-[ab]', 'caf\u00e9', 'a' * 101, 'shop-*,,x y', 'a/b/c', 'web/' + 'a' * 97, 'shop,' + ','.join(f's{i}' for i in range(60))])
def test_release_services_with_a_space_bracket_non_ascii_or_a_long_name_are_refused_on_save(text):
    # given
    field = next(f for f in website.PLUGIN.fields if f.key == 'release-services')

    # then
    assert isinstance(field.check(text), str) and field.check(text).isascii()
