import dataclasses
import re
from pathlib import Path

import pytest

import skopax
from skopax.contract import Panel
from skopax.plugins import prometheus
from skopax.registry import CORE_TABS, NAV_GROUPS, PAGE_LABEL_MAX, RESERVED_TABS, Registry
from tests.conftest import AUTH
from tests.plugins.base import minimal
from tests.test_admin_plugins import DEMO, HERE, add, served

WEB = Path(skopax.__file__).parent / 'web'


def pages(c) -> dict:
    return {p['id']: p.get('pages') for p in c.get('/api/server', headers=AUTH).json()['plugins']}


def solo(plugin):
    return Registry.of(plugin, directory=HERE).loaded()[0]


def test_a_plugin_page_becomes_a_sidebar_entry(store):
    # given
    paged = dataclasses.replace(DEMO, panels=(Panel('demo', 'demo-panel.js', label='Demo board', group='observe'), Panel('svcs')))
    with served(store, paged) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        c.app.state.runtime.refresh()

        # when
        serving = pages(c)
        c.patch('/api/admin/plugins/demo/instances/main', json={'enabled': False}, headers=AUTH)
        c.app.state.runtime.refresh()
        stopped = pages(c)

    # then
    assert serving['demo'] == [{'tab': 'demo', 'label': 'Demo board', 'group': 'observe', 'icon': '/plugins/demo/icon.svg'}]
    assert not stopped.get('demo'), 'the tile stays with its instance shown off (P5-C203), but its page leaves the sidebar'


def test_a_panel_without_a_label_adds_no_page(store):
    # given
    with served(store) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        c.app.state.runtime.refresh()

        # when
        listed = pages(c)

    # then
    assert listed['demo'] == []


@pytest.mark.parametrize('panel, fragment', [
    (Panel('anoms', 'demo-panel.js'), 'built-in page'),
    (Panel('svcs', label='Second services'), 'built-in page'),
    (Panel('settings', fragment='demo-panel.js'), 'built-in page'),
    (Panel('Bad Id', 'demo-panel.js', label='Bad'), 'panel tab Bad Id is not valid'),
    (Panel('x', 'demo-panel.js', label='Short'), 'panel tab x is not valid'),
    (Panel('demo', 'demo-panel.js', label='Demo', group='elsewhere'), 'unknown sidebar group elsewhere'),
    (Panel('demo', 'demo-panel.js', label=5), 'a panel is malformed'),
    (Panel('sub-anoms', 'demo-panel.js', label='Filters'), 'panel tab sub-anoms is reserved'),
    (Panel('fold-anoms', 'demo-panel.js', label='Fold'), 'panel tab fold-anoms is reserved'),
    (Panel('sub-anoms'), 'panel tab sub-anoms is reserved'),
    (Panel('demo', 'demo-panel.js', label='   '), 'needs a label of 1 to 24 printable characters'),
    (Panel('demo', 'demo-panel.js', label='D' * 25), 'needs a label of 1 to 24 printable characters'),
    (Panel('demo', 'demo-panel.js', label='Demo\nboard'), 'needs a label of 1 to 24 printable characters'),
    (Panel('demo', 'demo-panel.js', label='Demo' + chr(7)), 'needs a label of 1 to 24 printable characters'),
    (Panel('demo', label='Demo board'), 'panel demo has a label but no script'),
], ids=['core-tab-script', 'core-tab-label', 'core-tab-fragment', 'bad-id', 'too-short', 'unknown-group', 'label-type', 'filter-sub-id', 'filter-fold-id', 'reserved-bare-link',
        'blank-label', 'long-label', 'two-line-label', 'control-label', 'label-without-script'])
def test_a_panel_that_clashes_or_lies_breaks_the_plugin(panel, fragment):
    # given
    plugin = dataclasses.replace(DEMO, panels=(panel,))

    # when
    loaded = solo(plugin)

    # then
    assert loaded.state == 'broken'
    assert fragment in loaded.reason and loaded.reason.startswith('Demo ')


def test_a_bare_link_to_a_core_tab_keeps_the_plugin_loaded():
    # given
    linked = dataclasses.replace(DEMO, panels=(Panel('svcs'), Panel('demo', 'demo-panel.js', label='Demo board', group='more')))

    # when
    states = [solo(linked).state, Registry.of(prometheus.PLUGIN).loaded()[0].state]

    # then
    assert states == ['ok', 'ok']
    assert prometheus.PLUGIN.panels == (Panel('svcs'),)
    assert set(NAV_GROUPS) == {'main', 'observe', 'activity', 'connect', 'more'}


def test_core_tab_ids_match_the_page():
    # given
    sidebar = (WEB / 'static' / 'sidebar.js').read_text(encoding='utf-8')
    template = (WEB / 'template.html').read_text(encoding='utf-8')

    # when
    rows = re.search(r'^var SIDE_ROWS = \{\n(.*?)\n\};', sidebar, re.S | re.M)[1]
    row_ids = set(re.findall(r"^\s*'?([a-z][a-z0-9-]*)'?: \[", rows, re.M))
    admin_ids = set(re.findall(r"'([a-z][a-z0-9-]*)'", re.search(r'^var ADMIN_TABS = \[([^\]]*)\];', sidebar, re.M)[1]))
    panel_ids = set(re.findall(r'id="panel-([a-z][a-z0-9-]*)"', template))

    # then
    assert row_ids and admin_ids and not row_ids & admin_ids
    assert row_ids | admin_ids == set(CORE_TABS)
    assert panel_ids == set(CORE_TABS)


def test_a_page_label_is_stripped_and_a_24_character_one_loads(store):
    # given
    paged = dataclasses.replace(DEMO, panels=(Panel('demo', 'demo-panel.js', label='  Demo board  '),))
    longest = dataclasses.replace(DEMO, panels=(Panel('demo', 'demo-panel.js', label='L' * PAGE_LABEL_MAX),))
    with served(store, paged) as c:
        c.app.state.scheduler.nudge = lambda name: None
        add(c, 'main', True)
        c.app.state.runtime.refresh()

        # when
        listed = pages(c)

    # then
    assert [p['label'] for p in listed['demo']] == ['Demo board']
    assert PAGE_LABEL_MAX == 24 and solo(longest).state == 'ok'


def test_a_second_plugin_with_the_same_page_tab_is_broken():
    # given
    first = dataclasses.replace(DEMO, panels=(Panel('pages', 'demo-panel.js', label='Pages'),))
    second = minimal('other-pages', 'Other pages', panels=(Panel('pages', 'demo-panel.js', label='Other pages'),))
    twice = dataclasses.replace(DEMO, panels=(Panel('pages', 'demo-panel.js', label='Pages'), Panel('pages', 'demo-panel.js', label='Again')))

    # when
    states = {x.id: (x.state, x.reason) for x in Registry.of(first, second, directory=HERE).loaded()}
    alone = solo(twice)

    # then
    assert states['demo'] == ('ok', None)
    assert states['other-pages'][0] == 'broken' and 'page tab pages is already taken by demo' in states['other-pages'][1]
    assert alone.state == 'broken' and 'page tab pages is already taken by demo' in alone.reason


def test_reserved_page_tabs_cover_every_page_id_that_is_not_a_core_page():
    # given
    texts = [(WEB / 'template.html').read_text(encoding='utf-8')] + [p.read_text(encoding='utf-8') for p in sorted((WEB / 'static').glob('*.js'))]

    # when
    ids = {m[1] for text in texts for m in re.finditer(r"(?:id=\"|\.id = '|getElementById\('|'#)(?:tab|panel|count)-([a-z][a-z0-9-]*)", text)}

    # then
    assert ids - set(CORE_TABS) == set(RESERVED_TABS)
    assert not set(RESERVED_TABS) & set(CORE_TABS)
