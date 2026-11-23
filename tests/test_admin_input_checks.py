import pytest

from tests.conftest import settings, state
from skopax.config import load_settings
from skopax.store import Store
from skopax.tunables import Invalid, Live, TunableStore, parse, registry


def known(key: str):
    return {t.key: t for t in registry()}[key]


@pytest.mark.parametrize('key, raw', [('investigate.daily-usd', 'nan'), ('investigate.daily-usd', 'NaN'), ('jira.points', 'nan'), ('slack.thread-hours', 'inf')])
def test_a_number_setting_refuses_values_that_are_not_finite(key: str, raw: str):
    # when
    with pytest.raises(Invalid) as e:
        parse(known(key), raw, settings())

    # then
    assert str(e.value) == f'{known(key).label} must be a number'


def test_a_stored_not_finite_value_is_skipped_and_the_daily_cap_keeps_working(store: Store):
    # given
    store.save(state(), None)
    TunableStore(store.db).write('investigate.daily-usd', 'nan', 'nina.white@example.com', None)

    # when
    live = Live(settings(), TunableStore(store.db))

    # then
    assert live.current().investigate_daily_usd == settings().investigate_daily_usd


def test_every_admin_default_is_what_the_service_falls_back_to_without_a_config_key_or_variable(store: Store):
    # given
    live = Live(load_settings(lambda: {}, env={}), TunableStore(store.db))

    # when
    sources = {e['key']: e['source'] for e in live.entries()}

    # then
    assert [k for k, s in sources.items() if s != 'default'] == []


def test_an_unrecorded_write_stores_the_value_bumps_the_revision_and_leaves_no_history_line(store):
    # given
    from skopax.tunables import TunableStore
    tunables = TunableStore(store.db)
    before = tunables.rev()

    # when
    answer = tunables.write('investigate.daily-usd', '5', 'nina.white@example.com', None, record=False)

    # then
    assert tunables.rev() == before + 1 and tunables.all()['investigate.daily-usd']['value'] == '5'
    assert (answer['key'], answer['old'], answer['new'], answer['by']) == ('investigate.daily-usd', None, '5', 'nina.white@example.com')
    assert 'id' not in answer and 'seq' not in answer and tunables.history() == []
