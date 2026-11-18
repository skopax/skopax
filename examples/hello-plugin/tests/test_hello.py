import json
from dataclasses import dataclass, field

from skopax.contract import HttpError, InstanceInfo, Unreachable
from skopax.remote import Reply

import skopax_hello

SECRET = 's3cr3t-key'
URL = f'https://shop.example.com/health?key={SECRET}'
SHOWN = 'https://shop.example.com/health'
KEY = 'hello|shop.example.com'
NOW = 1_792_224_000.0


@dataclass
class Ctx:
    answer: object
    now: float = NOW
    asked: list = field(default_factory=list)

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo('main', True, None, {'url': URL})

    def http(self):
        def send(method, url, headers, data, timeout):
            self.asked.append(url)
            if isinstance(self.answer, Exception):
                raise self.answer
            return Reply(b'', {}, self.answer)

        return send


def test_a_url_that_answers_200_opens_no_card():
    # given
    ctx = Ctx(200)

    # when
    sweep = skopax_hello.run(ctx)

    # then
    assert sweep.result.ok and sweep.result.found == [] and sweep.result.ran == ('hello',)
    assert sweep.sections['hello'] == [{'url': SHOWN, 'ok': True, 'said': f'{SHOWN} answered 200', 'at': '2026-10-17T08:00:00Z'}]
    assert ctx.asked == [URL]


def test_a_url_that_does_not_answer_200_opens_a_card_keyed_to_the_plugin():
    # when
    sweeps = [skopax_hello.run(Ctx(answer)) for answer in (503, HttpError(404, URL), Unreachable(URL))]

    # then
    assert [[(c['key'], c['src'], c['sev'], c['what']) for c in s.result.found] for s in sweeps] == [
        [(KEY, 'hello', 'warn', f'{SHOWN} answered HTTP 503')],
        [(KEY, 'hello', 'warn', f'{SHOWN} answered HTTP 404')],
        [(KEY, 'hello', 'warn', f'{SHOWN} did not answer')],
    ]
    assert not any(SECRET in json.dumps([s.result.found, s.sections]) for s in sweeps)


def test_test_connection_says_what_the_url_answered_without_its_query():
    # when
    good, bad = skopax_hello.test(Ctx(200)), skopax_hello.test(Ctx(503))

    # then
    assert (good.ok, good.summary) == (True, f'{SHOWN} answered 200')
    assert (bad.ok, bad.summary) == (False, f'{SHOWN} answered HTTP 503')
