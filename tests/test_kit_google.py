import json

import jwt
import pytest

from skopax.contract import ReadError
from skopax.kit import google as google_kit
from skopax.kit.google import NEEDED, RETRY_CODES, SCOPES, TOKEN_URL, GoogleAuth, GoogleError, access_card, access_key, bounded, connection_test
from skopax.messages import msg
from skopax.remote import HttpError, Reply, Unreachable
from tests.fake_google import ACCESS, BROKEN_KEY, KEY, PEM, PLANTED, PRIVATE, Google

URL = 'https://firebasecrashlytics.googleapis.com/v1alpha/projects/1/apps/x/reports/topIssues'
BUSY = '{"error": {"code": 500, "message": "Internal error encountered.", "status": "INTERNAL"}}'
NOW = 1_794_000_000.0


def transport(answers: list):
    calls = []

    def send(method, url, headers, data, timeout):
        if url == TOKEN_URL:
            return Reply({'access_token': ACCESS, 'expires_in': 3599}, {})
        calls.append(method)
        got = answers.pop(0)
        if isinstance(got, Exception):
            raise got
        return Reply(got, {})
    return send, calls


def auth(answers: list):
    send, calls = transport(answers)
    pauses = []
    return GoogleAuth(KEY, send, lambda: NOW, SCOPES, pause=pauses.append), calls, pauses


@pytest.mark.parametrize('code', RETRY_CODES)
def test_a_read_that_google_answers_with_a_server_error_is_tried_once_more_after_a_pause(code):
    # given
    g, calls, pauses = auth([HttpError(code, URL, BUSY), {'groups': [{'issue': {'id': 'a1'}}]}])

    # when
    got = g.call('GET', URL, None, 'Crashlytics', 'no access')

    # then
    assert got == {'groups': [{'issue': {'id': 'a1'}}]}
    assert calls == ['GET', 'GET'], 'one retry'
    assert len(pauses) == 1, 'after one short pause'


def test_a_second_server_error_fails_with_googles_reason_and_no_doubled_full_stop():
    # given
    g, calls, _ = auth([HttpError(500, URL, BUSY), HttpError(500, URL, BUSY)])

    # when
    with pytest.raises(GoogleError) as e:
        g.call('GET', URL, None, 'Crashlytics', 'no access')

    # then
    assert e.value.message == 'Crashlytics HTTP 500: Internal error encountered', 'the banner adds its own full stop'
    assert e.value.reason == 'Crashlytics HTTP 500', 'a pass status names the code only'
    assert isinstance(e.value, ReadError) and e.value.status == 500
    assert calls == ['GET', 'GET'], 'never more than one retry'


@pytest.mark.parametrize('method, code', [('POST', 500), ('GET', 400), ('GET', 404), ('GET', 429)])
def test_writes_and_client_errors_are_not_retried(method, code):
    # given
    g, calls, pauses = auth([HttpError(code, URL, BUSY), {'ok': True}])

    # when
    with pytest.raises(GoogleError):
        g.call(method, URL, {} if method == 'POST' else None, 'Crashlytics', 'no access')

    # then
    assert calls == [method] and pauses == [], 'a POST may not be safe to repeat, and a 4xx answers the same the second time'


def test_a_read_asked_not_to_retry_fails_at_the_first_server_error():
    # given
    g, calls, pauses = auth([HttpError(503, URL, BUSY), {'ok': True}])

    # when
    with pytest.raises(GoogleError):
        g.call('GET', URL, None, 'Crashlytics', 'no access', retry=False)

    # then
    assert calls == ['GET'] and pauses == []


def test_the_conftest_takes_the_retry_pause_down_to_nothing():
    # then
    assert google_kit.RETRY_PAUSE == 0


@pytest.mark.parametrize('credentials', ['', '{not json', '[1]', json.dumps({'client_email': 'reader@example.com'}), BROKEN_KEY], ids=['empty', 'not-json', 'list', 'no-key', 'broken-pem'])
def test_a_missing_malformed_or_broken_key_fails_with_one_fixed_text_and_never_calls_google(credentials):
    # given
    http = Google()
    g = GoogleAuth(credentials, http, lambda: NOW, SCOPES)

    # when
    with pytest.raises(GoogleError) as e:
        g.call('GET', URL, None, 'Crashlytics', 'no access')

    # then
    assert (e.value.status, e.value.message, e.value.reason) == (400, msg('google.bad_credentials'), msg('google.bad_credentials'))
    assert e.value.__context__ is None and e.value.__cause__ is None, 'raised outside the except, so no library text rides along'
    assert http.calls == []
    assert PLANTED not in msg('google.bad_credentials') and 'PRIVATE KEY' not in msg('google.bad_credentials')


def test_the_key_signs_an_assertion_for_the_scopes_asked_and_the_token_is_reused():
    # given
    http = Google({('GET', URL): {'groups': []}})
    g = GoogleAuth(KEY, http, lambda: NOW, ('https://www.googleapis.com/auth/cloud-platform',))

    # when
    g.call('GET', URL, None, 'Crashlytics', 'no access')
    g.call('GET', URL, None, 'Crashlytics', 'no access')

    # then
    claims = jwt.decode(http.forms[0]['assertion'], PRIVATE.public_key(), algorithms=['RS256'], audience=TOKEN_URL, options={'verify_exp': False, 'verify_iat': False})
    assert claims['scope'] == 'https://www.googleapis.com/auth/cloud-platform' and claims['iss'] == 'reader@analytics.example.com'
    assert jwt.get_unverified_header(http.forms[0]['assertion'])['kid'] == 'kid-1'
    assert len(http.forms) == 1 and http.hits(URL)[1][2]['Authorization'] == f'Bearer {ACCESS}'


def test_a_refused_key_names_googles_error_code_and_nothing_else():
    # given
    refused = Google(token=HttpError(400, TOKEN_URL, '{"error": "invalid_grant", "error_description": "Invalid JWT Signature."}'))
    worded = Google(token=HttpError(401, TOKEN_URL, '{"error": "Not a code: see https://example.com"}'))

    # when
    with pytest.raises(GoogleError) as first:
        GoogleAuth(KEY, refused, lambda: NOW, SCOPES).access()
    with pytest.raises(GoogleError) as second:
        GoogleAuth(KEY, worded, lambda: NOW, SCOPES).access()

    # then
    assert (first.value.status, first.value.reason, first.value.key_refused) == (400, 'Google refused the service account key (HTTP 400 invalid_grant)', True)
    assert (second.value.status, second.value.reason, second.value.key_refused) == (401, 'Google refused the service account key (HTTP 401)', True)


def test_a_401_names_both_causes_with_the_own_token_masked_and_drops_the_token():
    # given
    body = f'{{"error": {{"message": "The caller {ACCESS} does not have permission."}}}}'
    http = Google({('GET', URL): HttpError(401, URL, body)})
    g = GoogleAuth(KEY, http, lambda: NOW, SCOPES)

    # when
    with pytest.raises(GoogleError) as e:
        g.call('GET', URL, None, 'Crashlytics', 'the key has no Crashlytics access', needs='the Viewer role')

    # then
    assert e.value.reason == 'the service account is invalid or lacks the Viewer role (HTTP 401)'
    assert e.value.message == 'the service account is invalid or lacks the Viewer role (HTTP 401: The caller *** does not have permission.)'
    assert (e.value.denied, e.value.lacks) == (True, 'is invalid or lacks the Viewer role')
    assert g.token is None, 'the next call signs in again'


def test_a_403_keeps_the_plugins_own_permission_text():
    # given
    body = '{"error": {"code": 403, "message": "The caller does not have permission", "status": "PERMISSION_DENIED"}}'
    http = Google({('GET', URL): HttpError(403, URL, body)})

    # when
    with pytest.raises(GoogleError) as e:
        GoogleAuth(KEY, http, lambda: NOW, SCOPES).call('GET', URL, None, 'Crashlytics', 'the key has no Crashlytics access', needs='the Viewer role')

    # then
    assert (e.value.reason, e.value.lacks) == ('the key has no Crashlytics access (HTTP 403)', 'lacks the Viewer role')
    assert e.value.message == 'the key has no Crashlytics access (HTTP 403: The caller does not have permission)'


def test_the_key_scrub_masks_the_keys_own_pem_and_id_in_googles_message():
    # given
    body = json.dumps({'error': {'message': f'bad key kid-1 for {PEM}'}})
    http = Google({('GET', URL): HttpError(400, URL, body)})

    # when
    with pytest.raises(GoogleError) as e:
        GoogleAuth(KEY, http, lambda: NOW, SCOPES).call('GET', URL, None, 'Crashlytics', 'no access')

    # then
    assert e.value.message == 'Crashlytics HTTP 400: bad key *** for ***'


@pytest.mark.parametrize('answers, message, posts, pauses', [
    ([HttpError(503, TOKEN_URL), {'access_token': ACCESS, 'expires_in': 3599}], None, 2, 1),
    ([HttpError(503, TOKEN_URL), HttpError(503, TOKEN_URL)], 'Google sign-in HTTP 503', 2, 1),
    ([HttpError(429, TOKEN_URL)], 'Google sign-in HTTP 429', 1, 0),
], ids=['one-retry', 'still-down', 'quota'])
def test_a_sign_in_outage_is_not_called_a_refused_key(answers, message, posts, pauses):
    # given
    tried, slept = [], []

    def send(method, url, headers, data, timeout):
        tried.append(url)
        got = answers.pop(0)
        if isinstance(got, Exception):
            raise got
        return Reply(got, {})

    g = GoogleAuth(KEY, send, lambda: NOW, SCOPES, pause=slept.append)

    # when
    if message is None:
        got = g.access()
    else:
        with pytest.raises(GoogleError) as e:
            g.access()

    # then
    assert (len(tried), len(slept)) == (posts, pauses)
    if message is None:
        assert got == ACCESS
    else:
        assert (e.value.message, e.value.key_refused) == (message, False)


def test_a_used_up_quota_and_an_unreachable_google_have_fixed_texts():
    # given
    quota = Google({('GET', URL): HttpError(429, URL, BUSY)})
    gone = Google({('GET', URL): Unreachable(f'{URL} unreachable (timed out)')})
    sign_in = Google(token=Unreachable(f'{TOKEN_URL} unreachable (timed out)'))

    # when
    errors = []
    for http in (quota, gone, sign_in):
        with pytest.raises(GoogleError) as e:
            GoogleAuth(KEY, http, lambda: NOW, SCOPES).call('GET', URL, None, 'Crashlytics', 'no access')
        errors.append(e.value)

    # then
    assert [(x.message, x.reached) for x in errors] == [('the Crashlytics quota is used up for now (HTTP 429)', True), ('Crashlytics: Unreachable', False),
                                                        ('Google sign-in: Unreachable', False)]


def test_a_bounded_transport_stops_at_its_budget_and_shortens_the_last_wait():
    # given
    times = [100.0]
    waits = []

    def send(method, url, headers, data, timeout):
        waits.append(timeout)
        return Reply({}, {})

    sender = bounded(send, 10, clock=lambda: times[0])

    # when
    sender('GET', URL, {}, None, 30)
    times[0] = 106.0
    sender('GET', URL, {}, None, 30)
    times[0] = 110.5
    with pytest.raises(GoogleError) as e:
        sender('GET', URL, {}, None, 30)

    # then
    assert waits == [10, 4]
    assert e.value.reason == 'Google reads stopped at their 10 s budget' and e.value.reached


def test_the_connection_test_shows_a_google_refusal_and_calls_an_unreachable_google_unreachable():
    # given
    def refused() -> str:
        raise GoogleError(401, 'no access (HTTP 401: denied)', 'no access (HTTP 401)')

    def gone() -> str:
        raise GoogleError(502, 'Crashlytics: Unreachable', reached=False)

    def minted() -> str:
        return GoogleAuth(KEY, Google(token=HttpError(400, TOKEN_URL, '{"error": "invalid_grant"}')), lambda: NOW, SCOPES).access()

    # when
    got = [connection_test(ask) for ask in (refused, gone, minted, lambda: 'Crashlytics \u00b7 fine')]

    # then
    assert [(r.ok, r.summary) for r in got] == [(False, 'no access (HTTP 401: denied)'), (False, msg('connection.unreachable')),
                                                (False, msg('connection.rejected', status=400)), (True, 'Crashlytics \u00b7 fine')]


@pytest.mark.parametrize('code, body', [(401, ''), (403, ''), (400, '{"error": {"code": 400, "message": "Permission denied.", "status": "PERMISSION_DENIED"}}')])
def test_a_permission_refusal_is_marked_with_the_permission_the_plugin_names(code, body):
    # given
    http = Google({('GET', URL): HttpError(code, URL, body)})

    # when
    with pytest.raises(GoogleError) as named:
        GoogleAuth(KEY, http, lambda: NOW, SCOPES).call('GET', URL, None, 'Crashlytics', 'no access', needs='the Viewer role')
    with pytest.raises(GoogleError) as unnamed:
        GoogleAuth(KEY, http, lambda: NOW, SCOPES).call('GET', URL, None, 'Crashlytics', 'no access')

    # then
    assert (named.value.denied, named.value.needs, named.value.status) == (True, 'the Viewer role', code)
    assert (unnamed.value.denied, unnamed.value.needs) == (True, NEEDED)


def test_server_errors_quotas_and_a_refused_key_are_not_permission_refusals():
    # given
    answers = [Google({('GET', URL): HttpError(500, URL, BUSY)}), Google({('GET', URL): HttpError(429, URL, BUSY)}),
               Google(token=HttpError(401, TOKEN_URL, '{"error": "invalid_client"}')), Google({('GET', URL): Unreachable('down')})]

    # when
    errors = []
    for http in answers:
        with pytest.raises(GoogleError) as e:
            GoogleAuth(KEY, http, lambda: NOW, SCOPES).call('GET', URL, None, 'Crashlytics', 'no access', needs='the Viewer role')
        errors.append(e.value)

    # then
    assert [x.denied for x in errors] == [False, False, False, False]


def test_the_access_card_names_the_part_and_the_permission_and_clears_through_its_own_key():
    # given
    error = GoogleError(403, 'no access (HTTP 403: denied)', 'no access (HTTP 403)', needs='the Viewer role')

    # when
    card = access_card('play', 'App stores', 'vitals', 'Play vitals', error)

    # then
    assert (card['key'], card['src'], card['sev'], card['ranKey']) == ('no-access|-/play|vitals', 'play', 'warn', 'play:access:vitals')
    assert card['label'] == 'App stores cannot read Play vitals: the service account lacks the Viewer role'
    assert access_card('play', 'App stores', 'vitals', 'Play vitals', GoogleError(401, 'x', needs='the Viewer role', invalid=True))['label'] == (
        'App stores cannot read Play vitals: the service account is invalid or lacks the Viewer role')
    assert 'no access (HTTP 403)' in card['what'] and 'denied' not in card['what'], 'the fixed reason only, never Google text'
    assert access_key('play', 'vitals') == 'play:access:vitals'


def test_googles_message_is_scrubbed_before_it_is_cut_so_no_token_fragment_survives_the_cut():
    # given
    said = 'x' * 190 + f' {ACCESS} tail'
    http = Google({('GET', URL): HttpError(400, URL, json.dumps({'error': {'message': said}}))})
    g = GoogleAuth(KEY, http, lambda: NOW, SCOPES)

    # when
    with pytest.raises(GoogleError) as e:
        g.call('GET', URL, None, 'Crashlytics', 'no access')

    # then
    assert 'ya29' not in e.value.message and e.value.message.startswith('Crashlytics HTTP 400: xxx')


def test_a_named_refusal_says_which_part_it_was():
    # given
    error = GoogleError(403, 'no access (HTTP 403: denied)', 'no access (HTTP 403)', needs='the Viewer role')

    # when
    named = error.named('Play vitals')

    # then
    assert (named.status, named.message, named.reason, named.needs, named.reached) == (403, 'Play vitals: no access (HTTP 403: denied)', 'Play vitals: no access (HTTP 403)', 'the Viewer role', True)
    assert GoogleError(401, 'x', needs='y', invalid=True).named('p').lacks == 'is invalid or lacks y'


def test_a_refused_key_read_by_a_pass_or_a_tool_names_both_causes_and_test_still_says_rejected():
    # given
    http = Google(token=HttpError(400, TOKEN_URL, '{"error": "invalid_grant", "error_description": "Invalid JWT Signature."}'))
    with pytest.raises(GoogleError) as minted:
        GoogleAuth(KEY, http, lambda: NOW, SCOPES).access()
    other = GoogleError(500, 'Crashlytics HTTP 500: boom', 'Crashlytics HTTP 500')

    # when
    refused = minted.value.as_refusal('the Viewer role')
    plain = minted.value.as_refusal('')

    # then
    text = 'the service account is invalid or lacks the Viewer role: Google refused the service account key (HTTP 400 invalid_grant)'
    assert (refused.status, refused.message, refused.reason) == (400, text, text)
    assert (refused.denied, refused.needs, refused.invalid, refused.key_refused, refused.lacks) == (True, 'the Viewer role', True, True, 'is invalid or lacks the Viewer role')
    assert (plain.needs, plain.denied) == (NEEDED, True)
    assert other.as_refusal('the Viewer role') is other, 'only a refused key is turned into a refusal'
    assert connection_test(lambda: (_ for _ in ()).throw(refused)).summary == msg('connection.rejected', status=400), 'Test keeps the credentials text (P5-C127)'
    assert access_card('ga', 'Google Analytics 4', 'web', 'the web property', refused)['label'] == (
        'Google Analytics 4 cannot read the web property: the service account is invalid or lacks the Viewer role')
