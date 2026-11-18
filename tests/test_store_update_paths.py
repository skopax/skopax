import mongomock
import pytest
from cryptography.fernet import Fernet

from skopax.instances import Instances
from skopax.sealer import Sealer
from skopax.store import AUTO_RETRIES
from tests.conftest import anomaly, state
from tests.plugin_kit import registry_with
from tests.plugins import demo_plugin

OPERATORS = ('$set', '$unset', '$setOnInsert', '$inc', '$push', '$pull', '$addToSet', '$min', '$max', '$rename', '$currentDate')


def paths_of(update: dict) -> list[str]:
    return [path for op in OPERATORS for path in (update.get(op) or {})]


def clashes(update: dict) -> list[tuple[str, str]]:
    paths = paths_of(update)
    return [(a, b) for i, a in enumerate(paths) for b in paths[i + 1:] if a == b or a.startswith(b + '.') or b.startswith(a + '.')]


@pytest.fixture
def sent(monkeypatch):
    seen = []
    for name in ('update_one', 'update_many', 'find_one_and_update'):
        original = getattr(mongomock.collection.Collection, name)

        def spy(self, filter, update, *a, _orig=original, **kw):
            if isinstance(update, dict):
                seen.append(update)
            return _orig(self, filter, update, *a, **kw)

        monkeypatch.setattr(mongomock.collection.Collection, name, spy)
    return seen


def test_the_clash_check_sees_a_path_in_both_set_and_unset():
    # given
    bad = {'$set': {'values.url': 'x'}, '$unset': {'values.url': ''}}
    nested = {'$set': {'values': {}}, '$unset': {'values.url': ''}}
    fine = {'$set': {'values.url': 'x'}, '$unset': {'values.token': ''}}

    # then
    assert clashes(bad) and clashes(nested) and not clashes(fine)


def test_no_update_the_store_sends_names_one_path_in_both_set_and_unset(store, sent):
    # given
    keys = ['log|a|ERROR|one', 'log|a|ERROR|two', 'log|a|ERROR|three']
    store.save({**state(*[anomaly(k) for k in keys]), 'initialized': True}, None)

    # when
    for resolution in (None, {'kind': 'fixed', 'by': 'nina.white@example.com', 'via': 'page'}):
        for st in ('progress', 'handled', 'investigating', 'dived', None):
            for by in (None, 'pod'):
                for ticket, owner in ((None, None), ('OPS-123', 'alice@example.com'), ('', '')):
                    store.triage(keys[0], st, ticket=ticket, owner=owner, by=by, resolution=resolution)
    for text in ('looks like a cache', ''):
        for by in ('alice@example.com', 'pod'):
            store.slack_draft(keys[1], text, by)
    store.pod_slack_draft(keys[1], 'draft', 'pod', hold='held')
    store.pod_slack_draft(keys[1], 'draft', 'pod')
    for suspects, recommend, code in ((['a'], 'restart', [{'file': 'x'}]), ([], '', []), (None, None, None)):
        store.assess(keys[2], suspects, recommend, code)
    for by in ('pod', 'alice@example.com'):
        store.note(keys[2], 'a note', by)
    for sev in ('crit', 'auto', 'warn'):
        store.set_severity(keys[0], sev, 'alice@example.com')
    store.group(keys[0], [keys[1], keys[2]])
    store.save({**state(anomaly(keys[0], sev='crit', extra='1'), anomaly(keys[1])), 'initialized': True}, None)
    for kind in ('known', 'muted'):
        store.mute(keys[0], 'reason', None, kind)
        store.unmute(keys[0])
    store.set_tagging(keys[1], {'tags': ['a'], 'tagsAt': 'x'})
    store.set_tagging(keys[1], {'tags': None, 'tagsAt': 'y'})
    card = {**anomaly(keys[0]), 'firstSeen': '2026-10-10T09:00:00Z'}
    for ev in ('muted', 'known', 'fired'):
        store.record_card(card, ev, ask={'runId': 'r1', 'at': 'x'}, said={'by': 'a'})
        store.record_card({**card, 'resolution': {'kind': 'fixed'}}, ev)
    for _ in range(AUTO_RETRIES + 2):
        store.slack_draft(keys[0], 'text', 'alice@example.com')
        store._auto_failed(store.anomalies, keys[0], 'boom')
    action = store.add_action({'type': 'mute', 'key': keys[0]}, status='processing')
    store.requeue_action(action['id'])

    # then
    assert len(sent) > 60
    assert [u for u in sent if clashes(u)] == []


def test_no_instance_update_names_one_path_in_both_set_and_unset(store, sent):
    # given
    key = Fernet.generate_key().decode()
    instances = Instances(store.db, registry_with(demo_plugin), lambda: Sealer(key))
    instances.create('demo', 'main', {'url': 'https://demo.example.com', 'token': 'pw', 'mode': 'fast'}, True, 'alice@example.com')

    # when
    instances.update('demo', 'main', {'url': 'https://two.example.com', 'mode': None, 'token': 'pw2'}, 'alice@example.com')
    instances.update('demo', 'main', {'mode': 'slow'}, 'alice@example.com')
    instances.update('demo', 'main', {'mode': '', 'url': 'https://three.example.com', 'token': 'pw3'}, 'alice@example.com')

    # then
    assert [u for u in sent if clashes(u)] == [] and any('$unset' in u for u in sent)
