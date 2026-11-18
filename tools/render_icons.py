import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

TILE = '#14171c'
RADIUS = 22
CHROME_CANDIDATES = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
]

PATH_CANDIDATES = ['google-chrome', 'chromium', 'chromium-browser']

ICONS = [
    ('icon-192.png', 192, 60, True),
    ('icon-512.png', 512, 60, True),
    ('icon-maskable-512.png', 512, 52, False),
    ('apple-touch-icon.png', 180, 60, False),
]


def find_chrome(explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    for path in CHROME_CANDIDATES:
        if os.path.isfile(path):
            return path
    for name in PATH_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def inline_svg(text):
    text = re.sub(r'<\?xml[^>]*\?>', '', text)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    return re.sub(
        r'<svg\b([^>]*)>',
        lambda m: '<svg' + re.sub(r'\s(width|height)="[^"]*"', '', m.group(1)) + ' width="100%" height="100%">',
        text,
        count=1,
    ).strip()


def page(svg, size, percent, rounded):
    radius = '%d%%' % RADIUS if rounded else '0'
    mark = size * percent / 100.0
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        'html,body{margin:0;padding:0;background:transparent;overflow:hidden}'
        '.t{width:%dpx;height:%dpx;background:%s;border-radius:%s;display:flex;align-items:center;justify-content:center}'
        '.m{width:%.3fpx;height:%.3fpx;display:block}'
        '</style></head><body><div class="t"><div class="m">%s</div></div></body></html>'
        % (size, size, TILE, radius, mark, mark, svg)
    )


def png_size(path):
    with open(path, 'rb') as handle:
        head = handle.read(24)
    if head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        raise ValueError('not a PNG: ' + path)
    return struct.unpack('>II', head[16:24])


def render(chrome, html_path, out_path, size, transparent):
    background = '00000000' if transparent else 'ffffffff'
    profile = tempfile.mkdtemp(prefix='skopax-chrome-')
    cmd = [
        chrome,
        '--headless=new',
        '--disable-gpu',
        '--no-first-run',
        '--user-data-dir=' + profile,
        '--hide-scrollbars',
        '--force-device-scale-factor=1',
        '--default-background-color=' + background,
        '--window-size=%d,%d' % (size, size),
        '--screenshot=' + out_path,
        'file://' + html_path,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


def main():
    parser = argparse.ArgumentParser(description='Render Skopax app icons from the mark SVG.')
    parser.add_argument('svg')
    parser.add_argument('outdir')
    parser.add_argument('--chrome', help='path to a Chrome or Chromium binary')
    args = parser.parse_args()

    chrome = find_chrome(args.chrome)
    if chrome is None:
        print('Chrome not found: pass --chrome PATH, or install Chrome or Chromium (looked in /Applications and for google-chrome, chromium, chromium-browser on PATH)', file=sys.stderr)
        return 1

    with open(args.svg, 'r', encoding='utf-8') as handle:
        svg = inline_svg(handle.read())

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    work = tempfile.mkdtemp(prefix='skopax-html-')

    for name, size, percent, rounded in ICONS:
        html_path = os.path.join(work, name + '.html')
        with open(html_path, 'w', encoding='utf-8') as handle:
            handle.write(page(svg, size, percent, rounded))
        out_path = os.path.join(outdir, name)
        render(chrome, html_path, out_path, size, rounded)
        got = png_size(out_path)
        if got != (size, size):
            print('%s: expected %dx%d, got %dx%d' % (name, size, size, got[0], got[1]), file=sys.stderr)
            return 1
        print('%s %dx%d ok' % (name, got[0], got[1]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
