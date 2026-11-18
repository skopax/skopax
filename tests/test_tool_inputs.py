from types import SimpleNamespace

import pytest

from tests.fake_http import FakeHttp
from tests.plugin_kit import prom_tools
from tests.test_argo_tool import argo_tools
from tests.test_investigator_tools import KEY, NOW, box, ro_box, seeded, sources
from skopax.investigator.tools import PROPOSE_CARD_ACTION, PROPOSE_TICKET, READ_TOOLS, WRITE_TOOLS, Effects, Toolbox, pass_table_tool, tag_tool, ticket_tool, filled
from skopax.store import Store

LEAKED_POINTS = '\n<parameter name="points">["Argo CD synced at 09:32 UTC …", …]'
STRICT_DROPS = ('minimum', 'maximum', 'minLength', 'maxLength', 'maxItems')


def report_box(store: Store, strict: bool = False) -> tuple[Toolbox, str]:
    rid = store.add_deploy_report({'summary': '1 deploy since 09:30 UTC', 'rows': [{'ns': 'platform', 'svc': 'skopax', 'flags': []}], 'usd': 0.0})['id']
    return Toolbox(sources(), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), [], 'live', NOW, 'https://skopax.example.com', report_id=rid, strict=strict), rid


def strict_box(store: Store, **kw) -> Toolbox:
    return Toolbox(sources(), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), [KEY], 'live', NOW, 'https://skopax.example.com', strict=True, **kw)


def test_the_leaked_markup_string_for_report_brief_points_is_refused_and_nothing_is_written(store: Store):
    # given
    b, rid = report_box(store)

    # when
    out, err = b.call('report_brief', {'level': 'ok', 'headline': 'skopax deploy clean', 'points': LEAKED_POINTS})

    # then
    assert (out, err) == ('points must be an array of strings, got a string', True)
    assert b.writes == [] and store.deploy_report(rid).get('brief') is None
    assert b.calls[-1]['error'] == 'points must be an array of strings, got a string'


@pytest.mark.parametrize('tool,args,message', [
    ('assess', {'key': KEY, 'suspects': 'pool leak after the deploy', 'recommend': 'r'}, 'suspects must be an array of strings, got a string'),
    ('group', {'parent': KEY, 'children': 'log|b|ERROR|y'}, 'children must be an array of strings, got a string'),
    ('jira_search', {'jql': 'project = OPS', 'fields': 'summary'}, 'fields must be an array of strings, got a string'),
], ids=['assess-suspects', 'group-children', 'jira_search-fields'])
def test_a_string_sent_for_an_array_is_refused_before_the_tool_runs(store: Store, tool, args, message):
    # given
    seeded(store)
    before = store.anomaly(KEY)
    asked = []
    b = box(store, jira=SimpleNamespace(search=lambda jql, fields, limit: asked.append(fields) or []))

    # when
    out, err = b.call(tool, args)

    # then
    assert (out, err) == (message, True)
    assert b.writes == [] and asked == [] and store.anomaly(KEY) == before


@pytest.mark.parametrize('tool,args,message', [
    ('note', {'key': KEY}, 'text is required'),
    ('note', {'key': KEY, 'text': None}, 'text is required'),
    ('note', {'key': KEY, 'text': 't', 'txt': 'x'}, 'note has no field txt; its fields are key, text'),
    ('open_cards', {'all': True}, 'open_cards has no field all; it takes no fields'),
    ('note', 'checked', 'note takes an object of fields, got a string'),
    ('triage', {'key': KEY, 'state': 'done'}, 'state must be one of progress, handled, got "done"'),
    ('deploys_since', {'hours': '6'}, 'hours must be an integer, got a string'),
    ('deploys_since', {'hours': True}, 'hours must be an integer, got a boolean'),
    ('deploys_since', {'hours': 6.5}, 'hours must be an integer, got a number'),
    ('deploys_since', {'hours': 100}, 'hours must be at most 72, got 100'),
    ('assess', {'key': KEY, 'suspects': ['a', 'b', 'c', 'd', 'e', 'f'], 'recommend': 'r'}, 'suspects takes at most 5 items, got 6'),
    ('assess', {'key': KEY, 'suspects': ['a', 7], 'recommend': 'r'}, 'suspects[1] must be a string, got an integer'),
    ('assess', {'key': KEY, 'suspects': ['a'], 'recommend': 'r', 'code': [{'repo': 'r', 'path': 'p', 'lines': 12, 'what': 'w'}]}, 'code[0].lines must be a string, got an integer'),
    ('assess', {'key': KEY, 'suspects': ['a'], 'recommend': 'r', 'code': [{'repo': 'r'}]}, 'code[0].path is required'),
    ('assess', {'key': KEY, 'suspects': ['a'], 'recommend': 'r', 'code': [{'repo': 'r', 'path': 'p', 'lines': '1', 'what': 'w', 'sha': 'x'}]}, 'code[0] has no field sha; its fields are repo, path, lines, what, ref, suggestion'),
    ('assess', {'key': KEY, 'suspects': ['a'], 'recommend': 'r', 'code': {'repo': 'r'}}, 'code must be an array of objects, got an object'),
    ('group', {'parent': KEY, 'children': []}, 'children needs at least 1 item, got 0'),
    ('jira_search', {'jql': 'x', 'fields': ['summary', 'labels']}, 'fields[1] must be one of summary, status, assignee, fixVersions, created, got "labels"'),
], ids=lambda v: v if isinstance(v, str) and len(v) < 20 else None)
def test_an_input_that_breaks_the_tool_schema_is_refused_with_the_field_and_what_was_expected(store: Store, tool, args, message):
    # given
    seeded(store)
    before = store.anomaly(KEY)
    b = box(store)

    # when
    out, err = b.call(tool, args)

    # then
    assert (out, err) == (message, True)
    assert b.writes == [] and store.anomaly(KEY) == before


def test_an_argo_input_that_breaks_the_tool_schema_is_refused_before_the_plugin_runs(store: Store):
    # given
    http = FakeHttp()
    b = box(store, plugin_tools=argo_tools(http))

    # when
    out, err = b.call('argo', {'op': 'apps', 'problem_only': 'no'})

    # then
    assert (out, err) == ('problem_only must be true or false, got a string', True)
    assert http.calls == []


def test_a_too_long_ticket_title_is_refused_by_its_schema(store: Store):
    # given
    seeded(store)

    # when
    out, err = ro_box(store).call('propose_ticket', {'title': 'x' * 201, 'description': 'd'})

    # then
    assert (out, err) == ('title must be at most 200 characters, got 201', True)


def test_null_on_an_optional_field_counts_as_left_out_and_numbers_take_integers(store: Store):
    # given
    prom = SimpleNamespace(enabled=True, asked=[], instant=lambda expr, at: prom.asked.append(at) or [])
    b = box(store, plugin_tools=prom_tools(prom))

    # when
    first = b.call('promql', {'expr': 'up', 'at': None, 'range_minutes': None})
    second = b.call('promql', {'expr': 'up', 'at': 1_792_220_000})

    # then
    assert (first[1], second[1]) == (False, False)
    assert prom.asked == [NOW, 1_792_220_000]


@pytest.mark.parametrize('marker', ['<parameter name="triage">', '</parameter>', '<invoke name="triage">', '</invoke>'])
def test_tool_call_markup_in_a_note_is_refused_and_the_card_keeps_its_note(store: Store, marker):
    # given
    seeded(store)
    b = box(store)

    # when
    out, err = b.call('note', {'key': KEY, 'text': f'Pool full, 40 errors (09:32 UTC).\n{marker}\nprogress'})

    # then
    assert err and out.startswith('text contains tool-call markup (') and out.endswith('); send the value again without it')
    assert b.writes == [] and store.anomaly(KEY).get('note') is None


@pytest.mark.parametrize('tool,args,field', [
    ('assess', {'key': KEY, 'suspects': ['pool', 'x</parameter>'], 'recommend': 'r'}, 'suspects[1]'),
    ('assess', {'key': KEY, 'suspects': ['pool'], 'recommend': 'r\n<parameter name="code">[]'}, 'recommend'),
    ('slack_draft', {'key': KEY, 'text': 'Errors went up.</invoke>'}, 'text'),
    ('slack_update', {'key': KEY, 'text': '<invoke name="slack_send">'}, 'text'),
], ids=['assess-suspects', 'assess-recommend', 'slack_draft', 'slack_update'])
def test_tool_call_markup_in_any_text_of_a_write_tool_is_refused_naming_the_field(store: Store, tool, args, field):
    # given
    seeded(store)
    before = store.anomaly(KEY)
    b = box(store)

    # when
    out, err = b.call(tool, args)

    # then
    assert err and out.startswith(f'{field} contains tool-call markup (')
    assert b.writes == [] and store.anomaly(KEY) == before


def test_tool_call_markup_in_report_and_ticket_texts_is_refused(store: Store):
    # given
    seeded(store)
    b, rid = report_box(store)
    ro = ro_box(store)

    # when
    headline = b.call('report_brief', {'level': 'ok', 'headline': 'clean</parameter>', 'points': ['err 5 vs 5']})
    point = b.call('report_brief', {'level': 'ok', 'headline': 'clean', 'points': ['err 5 vs 5', '<parameter name="level">warn']})
    draft = b.call('report_slack_draft', {'text': 'Deploy clean.\n</invoke>'})
    ticket = ro.call('propose_ticket', {'title': 'Pool leak', 'description': 'x\n<invoke name="note">'})

    # then
    assert [r[0].split(' contains')[0] for r in (headline, point, draft, ticket)] == ['headline', 'points[1]', 'text', 'description']
    assert all(r[1] for r in (headline, point, draft, ticket))
    assert b.writes == [] and store.deploy_report(rid).get('brief') is None and ro.proposal is None


def test_without_strict_the_definitions_are_the_module_schemas_untouched(store: Store):
    # given
    seeded(store)
    boxes = [box(store), ro_box(store), report_box(store)[0]]

    # when
    defs = [(b, d) for b in boxes for d in b.definitions()]

    # then
    assert all('strict' not in d for _, d in defs)
    for b, d in defs:
        known = {t['name']: t for t in READ_TOOLS + WRITE_TOOLS + [tag_tool(), ticket_tool(b.settings), PROPOSE_CARD_ACTION, pass_table_tool(b.toolset.sections())]}
        assert d == filled(known[d['name']]), d['name']


def schemas(s: dict):
    yield s
    for p in (s.get('properties') or {}).values():
        yield from schemas(p)
    if isinstance(s.get('items'), dict):
        yield from schemas(s['items'])


def optional_count(s: dict) -> int:
    return sum(len(set(x.get('properties') or {}) - set(x.get('required') or [])) for x in schemas(s) if x.get('type') == 'object')


def test_with_strict_only_the_write_tools_are_strict_and_their_schemas_fit_the_strict_limits(store: Store):
    # given
    seeded(store)
    boxes = {'pod': strict_box(store), 'page ask': strict_box(store, ask=True), 'slack ask': strict_box(store, ask=True, read_only=True), 'report': report_box(store, strict=True)[0]}
    reads = {t['name'] for t in READ_TOOLS}

    # when
    offered = {run: b.definitions() for run, b in boxes.items()}

    # then
    for run, defs in offered.items():
        strict = [d for d in defs if d.get('strict')]
        assert {d['name'] for d in strict} == {d['name'] for d in defs} - reads, run
        assert len(strict) <= 20 and sum(optional_count(d['input_schema']) for d in strict) <= 24, run
        for d in strict:
            for s in schemas(d['input_schema']):
                assert not set(STRICT_DROPS) & set(s), (run, d['name'], s)
                assert s.get('minItems', 0) <= 1, (run, d['name'])
                assert s.get('type') != 'object' or s.get('additionalProperties') is False, (run, d['name'])
        assert all('strict' not in d for d in defs if d['name'] in reads), run
    assert [len([d for d in defs if d.get('strict')]) for defs in offered.values()] == [8, 5, 2, 2]


def test_strict_moves_the_dropped_limits_into_the_field_description_and_leaves_the_module_schemas_alone(store: Store):
    # given
    seeded(store)
    b, _ = report_box(store, strict=True)
    asks = {d['name']: d for d in strict_box(store, ask=True, read_only=True).definitions()}

    # when
    brief = next(d for d in b.definitions() if d['name'] == 'report_brief')
    ticket = next(d for d in ro_box(store).definitions() if d['name'] == 'propose_ticket')
    strict_ticket, action = asks['propose_ticket'], asks['propose_card_action']

    # then
    assert brief['input_schema']['properties']['points'] == {'type': 'array', 'items': {'type': 'string'}, 'description': 'at most 5 items'}
    assert strict_ticket['input_schema']['properties']['title'] == {'type': 'string', 'description': 'at most 200 characters'}
    assert strict_ticket['input_schema']['properties']['points'] == {'type': 'integer', 'enum': [1, 2, 3, 5, 8, 13, 21]}
    assert action['input_schema']['properties']['hours'] == {'type': 'integer', 'description': 'from 1 to 720'}
    assert ticket == PROPOSE_TICKET and ticket['input_schema']['properties']['title']['maxLength'] == 200
    assert next(t for t in WRITE_TOOLS if t['name'] == 'report_brief')['input_schema']['properties']['points']['maxItems'] == 5


def test_with_strict_the_server_still_checks_the_limits_strict_does_not_send(store: Store):
    # given
    seeded(store)
    b = strict_box(store)

    # when
    out, err = b.call('assess', {'key': KEY, 'suspects': ['a', 'b', 'c', 'd', 'e', 'f'], 'recommend': 'r'})

    # then
    assert (out, err) == ('suspects takes at most 5 items, got 6', True)
    assert b.writes == []
