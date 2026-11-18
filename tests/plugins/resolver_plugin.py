import dataclasses
import time

from skopax.contract import Hooks
from tests.plugins.base import minimal

ANSWERS: dict = {}
SEEN: list = []


def reset() -> None:
    ANSWERS.clear()
    SEEN.clear()


def role_resolver(user, channel):
    SEEN.append((user, channel))
    answer = ANSWERS.get((user.get('email'), channel))
    if answer == 'raise':
        raise RuntimeError('the role service is down')
    if answer == 'bad':
        return 'root'
    if answer == 'sleep':
        time.sleep(1)
        return 'admin'
    return answer


PLUGIN = dataclasses.replace(minimal('resolver-test', 'Resolver test'), hooks=Hooks(role_resolver=role_resolver))
