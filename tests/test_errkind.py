import logging

import pytest
from pymongo.errors import AutoReconnect, OperationFailure

from skopax.apitokens import ApiTokens
from skopax.errkind import Refusal, kind, text_of
from skopax.notices import Notices
from skopax.remote import HttpError
from skopax.scheduler import Scheduler
from skopax.store import NotFound
from skopax.sweeps import failed
from tests.conftest import ADMIN_EMAIL

SENTINEL = 'not authorized on db at mongodb://user:SENTINEL-77@db.example.com'


class Odd(Exception):
    code = True


class Worded(Exception):
    code = 'E13'


def test_kind_names_the_type_and_an_integer_code_only():
    # given
    refused = OperationFailure(SENTINEL, code=13)

    # then
    assert kind(refused) == 'OperationFailure code 13'
    assert kind(OperationFailure(SENTINEL)) == 'OperationFailure'
    assert kind(AutoReconnect(SENTINEL)) == 'AutoReconnect'
    assert kind(HttpError(502, 'prom')) == 'HttpError code 502'
    assert (kind(Odd()), kind(Worded()), kind(ValueError(SENTINEL))) == ('Odd', 'Worded', 'ValueError')


def test_the_scheduler_beat_log_carries_the_type_and_code_but_never_the_text(store, caplog, monkeypatch):
    # given
    def refuse(boot, now):
        raise OperationFailure(SENTINEL, code=13)

    monkeypatch.setattr(store, 'beat_run', refuse)

    # when
    with caplog.at_level(logging.WARNING):
        Scheduler(store, []).beat()

    # then
    assert 'OperationFailure code 13' in caplog.text and 'SENTINEL' not in caplog.text


def test_the_api_token_last_use_log_carries_the_type_and_code_but_never_the_text(store, caplog, monkeypatch):
    # given
    tokens = ApiTokens(store.db)
    token, _ = tokens.create('ci', 'operator', ADMIN_EMAIL)

    def refuse(*a, **kw):
        raise OperationFailure(SENTINEL, code=13)

    monkeypatch.setattr(tokens.col, 'update_one', refuse)

    # when
    with caplog.at_level(logging.WARNING):
        found = tokens.resolve(token)

    # then
    assert found is not None
    assert 'OperationFailure code 13' in caplog.text and 'SENTINEL' not in caplog.text


def test_the_notification_logs_carry_the_type_and_code_but_never_the_text(store, caplog, monkeypatch):
    # given
    def refuse(*a, **kw):
        raise OperationFailure(SENTINEL, code=13)

    monkeypatch.setattr(store, 'add_notices', refuse)
    monkeypatch.setattr(store, 'anomaly', refuse)
    notices = Notices(store)

    # when
    with caplog.at_level(logging.WARNING):
        saved = notices.add({'alice@example.com'}, 'resolved', 'bob@example.com', key='k1')
        card = notices._card('k1')

    # then
    assert (saved, card) == (0, None)
    assert caplog.text.count('OperationFailure code 13') == 2 and 'SENTINEL' not in caplog.text


def test_a_failed_pass_status_keeps_the_http_code_of_the_error():
    # then
    assert failed(HttpError(502, 'prom')) == 'error: HttpError code 502'
    assert failed(ValueError(SENTINEL)) == 'error: ValueError'


def test_text_of_gives_the_text_skopax_wrote_and_only_the_kind_of_anything_else():
    # then
    assert text_of(NotFound('no card #12')) == 'no card #12'
    assert text_of(Refusal('a card cannot be a duplicate of itself')) == 'a card cannot be a duplicate of itself'
    assert text_of(ValueError(SENTINEL)) == 'ValueError'
    assert text_of(OperationFailure(SENTINEL, code=13)) == 'OperationFailure code 13'
    assert isinstance(Refusal('x'), ValueError)
