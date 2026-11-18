import subprocess
import threading
import time

import pytest

from skopax.kit import browser as browser_kit
from skopax.kit.browser import Connection, verdict
from tools.render_icons import find_chrome

START_S = 30.0
CARD = 'position:relative;margin:16px;width:358px;box-sizing:border-box;padding:16px;border-radius:12px;overflow:hidden;background:#f4f6f8'
BOX = 'margin:16px;width:358px;overflow:hidden'
DOT = '<div style="position:absolute;right:-96px;bottom:-96px;width:192px;height:192px;border-radius:50%;background:#cfe3ff"></div>'
TABLE = ('<div style="' + BOX + '"><table style="width:700px"><tr><th>Term</th><th>Rate</th><th>Fees</th><th>Total cost</th></tr>'
         '<tr><td>12</td><td>2.1</td><td>40</td><td>1200</td></tr></table></div>')
STYLE = ('body{margin:0;font:16px sans-serif} .glow::after{content:"";position:absolute;top:0;right:-120px;width:200px;height:100%;background:#ffe9c7} '
         '@keyframes drift{from{opacity:1}to{opacity:.9}}')


class Tab:
    def __init__(self, conn: Connection, session: str, frame: str):
        self.conn = conn
        self.session = session
        self.frame = frame

    def items(self, body: str) -> tuple[float, list[dict]]:
        page = f'<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>{STYLE}</style></head><body>{body}</body></html>'
        deadline = time.monotonic() + 10
        self.conn.call('Page.setDocumentContent', {'frameId': self.frame, 'html': page}, self.session, deadline)
        got = self.conn.call('Runtime.evaluate', {'expression': f'{browser_kit.OVERFLOW}({browser_kit.OVERFLOW_MAX})', 'returnByValue': True}, self.session, deadline)
        found = got['result']['value']
        return found['vw'], found['items']

    def verdicts(self, body: str) -> list[str]:
        vw, items = self.items(body)
        return [v for item in items for v in (verdict(item, vw),) if v][:browser_kit.OVERFLOW_MAX]


@pytest.fixture(scope='module')
def tab(tmp_path_factory):
    chrome = find_chrome()
    if chrome is None:
        pytest.skip('no Chrome or Chromium to run the overflow probe in')
    profile = tmp_path_factory.mktemp('probe-chrome')
    proc = subprocess.Popen([chrome, '--headless=new', '--no-sandbox', '--remote-debugging-port=0', f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check',
                             '--disable-background-networking', '--disable-component-update', '--disable-extensions', '--disable-sync', 'about:blank'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    found: dict = {}
    ready = threading.Event()

    def read():
        for line in proc.stderr:
            if 'DevTools listening on ' in line and not ready.is_set():
                found['ws'] = line.split('DevTools listening on ', 1)[1].strip()
                ready.set()
        ready.set()

    threading.Thread(target=read, daemon=True).start()
    conn = None
    try:
        ready.wait(START_S)
        assert found.get('ws'), 'the test Chrome did not open its DevTools endpoint'
        conn = Connection.open(found['ws'], 10)
        deadline = time.monotonic() + 10
        target = conn.call('Target.createTarget', {'url': 'about:blank'}, deadline=deadline)['targetId']
        session = conn.call('Target.attachToTarget', {'targetId': target, 'flatten': True}, deadline=deadline)['sessionId']
        conn.call('Page.enable', {}, session, deadline)
        conn.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True}, session, deadline)
        conn.call('Emulation.setScrollbarsHidden', {'hidden': True}, session, deadline)
        frame = conn.call('Page.getFrameTree', {}, session, deadline)['frameTree']['frame']['id']
        yield Tab(conn, session, frame)
    finally:
        if conn is not None:
            conn.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.parametrize('body', [
    '<div style="' + CARD + '"><h2>Check your score</h2><p>See where you stand today.</p>' + DOT + '</div>',
    ('<div style="' + CARD + '"><p>Your score</p><svg width="460" height="120" viewBox="0 0 460 120"><path d="M10 110 A 220 220 0 0 1 450 110" stroke="#2a8" stroke-width="12" fill="none"/>'
     '<text x="400" y="100">850</text></svg></div>'),
    '<div class="glow" style="' + CARD + '"><p>Plans for every budget</p></div>',
    '<div style="' + CARD + '"><p>Plans</p><span aria-hidden="true" style="position:absolute;left:200px;top:0;font-size:96px;white-space:nowrap">SALE SALE</span></div>',
    '<div style="margin:16px;width:262px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Garden chairs, folding tables, parasols and a few more</div>',
    ('<button style="position:relative">Open<span style="position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0">'
     'More options here</span></button>'),
    ('<div style="' + BOX + '"><div style="display:flex;transform:translateX(0)"><div style="flex:0 0 358px">Slide one</div><div style="flex:0 0 358px">Slide two</div>'
     '<div style="flex:0 0 358px">Slide three</div></div></div>'),
    '<div style="' + BOX + '"><div style="display:flex;animation:drift 10s infinite"><p style="flex:0 0 358px">News one</p><p style="flex:0 0 358px">News two</p></div></div>',
    '<div style="' + BOX + '"><div style="width:361px;text-align:right">Total</div></div>',
    '<div style="overflow:hidden;width:390px;position:relative"><p>Offers</p><div style="position:absolute;left:300px;top:0;width:200px;height:50px;background:#eee"></div></div>',
    '<div style="' + CARD + '">' + ''.join(f'<p>Line {n}</p>' for n in range(500)) + DOT + '</div>',
], ids=['decor-circle', 'svg-gauge', 'pseudo-element', 'aria-hidden', 'ellipsis', 'screen-reader-only', 'transformed-track', 'animated-track', 'few-pixel-spill',
        'decor-under-a-clipping-parent', 'long-decorated-section'])
def test_a_clipped_box_that_hides_only_decoration_is_not_cut(tab, body):
    # when
    shown = tab.verdicts(body)

    # then
    assert shown == []


@pytest.mark.parametrize('body', [
    TABLE,
    '<div style="' + BOX + ';white-space:nowrap">Read the full terms and conditions before you sign the agreement</div>',
    '<div style="' + CARD + ';height:80px"><input aria-label="Amount" style="position:absolute;left:300px;top:20px;width:150px"></div>',
    '<div style="overflow:hidden;width:390px"><div style="width:600px">' + 'Every fee and rate of the plan is listed in this paragraph. ' * 4 + '</div></div>',
], ids=['table', 'nowrap-text', 'control', 'text-past-a-clipping-parent'])
def test_readable_content_past_a_clipping_edge_is_cut(tab, body):
    # when
    shown = tab.verdicts(body)

    # then
    assert shown == ['cut']


def test_decorated_cards_before_a_cut_table_leave_it_a_slot(tab):
    # given
    cards = ''.join('<div style="' + CARD + '"><p>Card ' + str(n) + '</p>' + DOT + '</div>' for n in range(browser_kit.OVERFLOW_MAX + 2))

    # when
    vw, items = tab.items(cards + TABLE)

    # then
    assert [verdict(item, vw) for item in items].count('cut') == 1
    assert [item['lost'] for item in items if verdict(item, vw) == 'cut'] == [True]


def test_a_clipped_drawing_with_many_parts_still_leaves_a_later_cut_table_a_slot(tab):
    # given
    parts = ''.join(f'<g><rect x="{n}" y="10" width="900" height="10" fill="#89a"/></g>' for n in range(60))
    drawing = '<div style="' + CARD + '"><p>Chart</p><svg width="900" height="80">' + parts + '</svg></div>'

    # when
    shown = tab.verdicts(drawing + TABLE)

    # then
    assert shown == ['cut']
