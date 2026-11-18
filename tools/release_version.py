import argparse
import re
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / 'pyproject.toml'
NUMBER = r'(0|[1-9][0-9]*)'
TAG = re.compile(rf'v{NUMBER}\.{NUMBER}\.{NUMBER}(?:-rc\.{NUMBER})?')
VERSION = re.compile(rf'{NUMBER}\.{NUMBER}\.{NUMBER}(?:rc{NUMBER})?')


def package_version(path: Path = PYPROJECT) -> str:
    return tomllib.loads(Path(path).read_text(encoding='utf-8'))['project']['version']


def parse(tag: str) -> tuple[str, str, str, str | None] | None:
    match = TAG.fullmatch(tag)
    return match.groups() if match else None


def expected(tag: str) -> str | None:
    parts = parse(tag)
    if parts is None:
        return None
    major, minor, patch, rc = parts
    return f'{major}.{minor}.{patch}' + (f'rc{rc}' if rc is not None else '')


def release_tag(version: str) -> str | None:
    match = VERSION.fullmatch(version)
    if match is None:
        return None
    major, minor, patch, rc = match.groups()
    return f'v{major}.{minor}.{patch}' + (f'-rc.{rc}' if rc is not None else '')


def image_tags(tag: str) -> list[str] | None:
    parts = parse(tag)
    if parts is None:
        return None
    major, minor, patch, rc = parts
    if rc is not None:
        return [f'{major}.{minor}.{patch}-rc.{rc}']
    return [f'{major}.{minor}.{patch}', f'{major}.{minor}', 'latest']


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='release_version')
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check')
    check.add_argument('tag')
    check.add_argument('--pyproject', type=Path, default=PYPROJECT)
    tags = commands.add_parser('tags')
    tags.add_argument('tag')
    args = parser.parse_args(argv)
    if args.command == 'check':
        version = package_version(args.pyproject)
        if expected(args.tag) != version:
            print(f'tag {args.tag} does not match package version {version}')
            return 1
        return 0
    found = image_tags(args.tag)
    if found is None:
        print(f'tag {args.tag} is not vX.Y.Z or vX.Y.Z-rc.N')
        return 1
    print('\n'.join(found))
    return 0


if __name__ == '__main__':
    sys.exit(main())
