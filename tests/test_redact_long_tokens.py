import time

from skopax.plugins.confluence.client import masked
from skopax.redact import redact

HEX = '9f' * 15_000


def test_a_long_token_is_redacted_in_linear_time():
    # given
    lines = [HEX, 'a+' * 15_000, 'a.' * 15_000, '-eyJ' * 7_500]

    # when
    started = time.perf_counter()
    out = [redact(x) for x in lines] + [masked(HEX)]
    took = time.perf_counter() - started

    # then
    assert took < 1.0
    assert out[0] == HEX and out[-1] == HEX


def test_addresses_joined_by_a_plus_a_plus_address_and_a_jwt_are_still_redacted():
    # when
    out = redact('to=nina.white@example.com+paul.stone@example.com&cc=nina+test@example.com auth=eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl')

    # then
    assert 'white' not in out and 'paul' not in out and 'test@' not in out and 'eyJ' not in out
    assert out.startswith('to=[redacted]') and out.endswith('&cc=[redacted] auth=[redacted]')
