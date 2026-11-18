from skopax.contract import Denied, SourceDown
from skopax.investigator import prompt
from skopax.plugins.jira.client import PERMISSIONS, denied_text
from skopax.postdeploy import PostDeploy
from skopax.store import Store
from tests.test_postdeploy import NOW, NS, OLD_RELEASE, at, only, prom, settled_action


class Refusing:
    enabled = True

    def __init__(self, error: Exception):
        self.error = error

    def search(self, jql: str, fields: list[str], limit: int = 20) -> list[dict]:
        raise self.error


def checked(store: Store, jira) -> dict:
    store.add_deploys([{'ns': NS, 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': at(1800), 'tickets': ['OPS-121']}])
    settled_action(store, 1800, 1800)
    saved = PostDeploy(store, only(prom({})), jira, clock=lambda: NOW, projects=('OPS',), release_template=lambda: OLD_RELEASE).run_once(NOW)
    return store.deploy_report(saved['id'])


def test_a_refused_release_search_is_named_on_the_report_and_keeps_the_keys(store: Store):
    # given
    jira = Refusing(Denied(denied_text('issues', 403), 'issues', PERMISSIONS['issues']))

    # when
    report = checked(store, jira)

    # then
    assert report['issues'] == [{'key': 'OPS-121'}]
    assert report['issuesNote'] == denied_text('issues', 403) and 'Browse projects' in report['issuesNote']


def test_an_outage_keeps_the_keys_without_a_refusal_note(store: Store):
    # given
    jira = Refusing(SourceDown('Jira HTTP 503'))

    # when
    report = checked(store, jira)

    # then
    assert report['issues'] == [{'key': 'OPS-121'}] and 'issuesNote' not in report


def test_the_assessment_brief_names_a_refused_release_search_instead_of_none():
    # given
    note = denied_text('issues', 403)

    # when
    refused = prompt.report_text({'rows': [], 'issues': [], 'issuesNote': note})
    kept = prompt.report_text({'rows': [], 'issues': [{'key': 'OPS-121'}], 'issuesNote': note})
    empty = prompt.report_text({'rows': [], 'issues': []})

    # then
    assert refused.endswith(f'Released issues:\n- details not read: {note}')
    assert kept.endswith(f'Released issues:\n- OPS-121\n- details not read: {note}')
    assert empty.endswith('Released issues:\n- none')
