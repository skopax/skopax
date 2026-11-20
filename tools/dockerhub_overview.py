import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

OVERVIEW = Path(__file__).resolve().parent.parent / 'docs' / 'dockerhub.md'
MARK = '{version}'
MINOR = '{minor}'
LIMIT = 25000
HUB = 'https://hub.docker.com/v2'
REPO = 'skopax/skopax'
TIMEOUT = 20


def render(version: str, path: Path = OVERVIEW) -> str:
    text = Path(path).read_text(encoding='utf-8')
    if MARK not in text:
        raise ValueError(f'{path.name} has no {MARK} mark')
    text = text.replace(MARK, version).replace(MINOR, '.'.join(version.split('.')[:2]))
    if len(text) > LIMIT:
        raise ValueError(f'{path.name} is {len(text)} characters, Docker Hub takes {LIMIT}')
    return text


def call(url: str, body: dict, method: str, token: str | None = None) -> dict:
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read() or b'{}')


def publish(version: str, user: str, secret: str) -> None:
    text = render(version)
    token = call(f'{HUB}/auth/token', {'identifier': user, 'secret': secret}, 'POST').get('access_token')
    if not token:
        raise RuntimeError('Docker Hub login gave no token')
    call(f'{HUB}/repositories/{REPO}/', {'full_description': text}, 'PATCH', token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('version')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    if args.check:
        render(args.version)
        print(f'overview renders for {args.version}')
        return 0
    try:
        publish(args.version, os.environ['HUB_USER'], os.environ['HUB_TOKEN'])
    except Exception as e:
        print(f'::warning::Docker Hub overview not updated: {type(e).__name__}', file=sys.stderr)
        return 0
    print(f'Docker Hub overview updated for {args.version}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
