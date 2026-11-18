import datetime as dt

import pytest

from skopax import clock
from skopax.apitokens import ApiTokens
from skopax.setup import SetupGate
from skopax.users import Users


def test_default_zone_is_utc():
    clock.set_zone('UTC')
    assert clock.date(0) == '1 Jan 00:00' and clock.label(0) == 'UTC'


def test_local_day_across_dst():
    clock.set_zone('Europe/Berlin')
    before = dt.datetime(2026, 3, 28, 23, 30, tzinfo=dt.timezone.utc).timestamp()
    after = dt.datetime(2026, 3, 29, 22, 30, tzinfo=dt.timezone.utc).timestamp()
    assert clock.day(before) == '2026-03-29' and clock.day(after) == '2026-03-30'
    assert clock.label(before) == 'CET' and clock.label(after) == 'CEST'
    assert clock.midnight(after) == dt.datetime(2026, 3, 29, 22, 0, tzinfo=dt.timezone.utc).timestamp()


def test_unknown_timezone_is_refused():
    with pytest.raises(ValueError, match='Mars/Olympus'):
        clock.set_zone('Mars/Olympus')


def test_stamp_is_utc_to_the_second_whatever_the_display_zone():
    # given
    clock.set_zone('Asia/Tokyo')

    # when
    text = clock.stamp(1_800_000_000.5)

    # then
    assert text == '2027-01-15T08:00:00Z'


def test_user_token_and_setup_times_are_clock_stamps(store, monkeypatch):
    # given
    monkeypatch.setattr('time.time', lambda: 1_800_000_000.5)
    clock.set_zone('Asia/Tokyo')
    users = Users(store.db)
    tokens = ApiTokens(store.db, clock=lambda: 1_800_000_060.0)

    # when
    made = users.create('erin@example.com', 'Erin', 'viewer')
    _, token = tokens.create('ci', 'viewer', 'erin@example.com')
    SetupGate(store.db, users, clock=lambda: 1_800_000_120.0).issue_code()

    # then
    assert made['createdAt'] == '2027-01-15T08:00:00Z'
    assert token['createdAt'] == '2027-01-15T08:01:00Z'
    assert store.db['meta'].find_one({'_id': 'setup-code'})['at'] == '2027-01-15T08:02:00Z'
