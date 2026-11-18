import dataclasses
import threading

from skopax.contract import Check, PassResult, Sweep
from skopax.engine import anomaly
from tests.plugins import demo_plugin

PLAN: dict = {}
STARTED = threading.Event()
RELEASE = threading.Event()
OWN = {'pulse': ('err', 'routes'), 'beat': ('slow', 'beats')}


def reset() -> None:
    PLAN.clear()
    STARTED.clear()
    RELEASE.clear()


def svc_of(check: str, name: str) -> str:
    return f'{name}-{check}'


def key_of(check: str, name: str) -> str:
    return f'{OWN[check][0]}|shop/{svc_of(check, name)}'


def _run(check: str, src: str, ctx) -> Sweep:
    name = ctx.instance.name
    plan = PLAN.get(f'{check}:{name}', {})
    if plan.get('gate'):
        STARTED.set()
        RELEASE.wait(5)
    if plan.get('fail'):
        return Sweep(PassResult(check, False, f"error: {plan['fail']}"))
    sig, own = OWN[check]
    svc = svc_of(check, name)
    card = anomaly(src, sig, 'shop', svc, '', 'warn', 5, None, None, '', 'ERROR logs', 'five errors')
    return Sweep(PassResult(check, True, 'ok', [card], {}, ()), {'services': [{'ns': 'shop', 'svc': svc, 'kind': 'app'}], own: [{'ns': 'shop', 'svc': svc}]})


def pulse(ctx) -> Sweep:
    return _run('pulse', 'demo', ctx)


def beat(ctx) -> Sweep:
    return _run('beat', 'beat', ctx)


PULSE = Check('pulse', 'Demo pulse', 900, 0, ('demo',), pulse, sections=('services', 'routes'))
BEAT = Check('beat', 'Demo beat', 900, 0, ('beat',), beat, sections=('services', 'beats'))
PLUGIN = dataclasses.replace(demo_plugin.PLUGIN, checks=(PULSE, BEAT))
