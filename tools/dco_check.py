import re
import subprocess
import sys

BOT_EMAILS = {'49699333+dependabot[bot]@users.noreply.github.com'}
BOT_NAMES = {'dependabot[bot]', 'github-actions[bot]'}
SIGN_OFF = re.compile(r'^Signed-off-by: .+? <([^<>\s]+)>[ \t]*$', re.MULTILINE)
FIELD = '\x1f'
RECORD = '\x1e'
FORMAT = f'%H{FIELD}%an{FIELD}%ae{FIELD}%s{FIELD}%b{RECORD}'
FIX = 'Fix it with `git commit --amend -s` for the last commit, or `git rebase --signoff` for several, then push again.'


def is_bot(name: str, email: str) -> bool:
    return email.lower() in BOT_EMAILS or name in BOT_NAMES


def failing(base: str, head: str) -> list[str]:
    log = subprocess.run(['git', 'log', '--no-merges', f'--format={FORMAT}', f'{base}..{head}'],
                         capture_output=True, text=True, check=True).stdout
    found = []
    for record in filter(None, (r.strip('\n') for r in log.split(RECORD))):
        sha, name, email, subject, body = record.split(FIELD, 4)
        if is_bot(name, email):
            continue
        signed = {address.lower() for address in SIGN_OFF.findall(body)}
        if email.lower() not in signed:
            found.append(f'{sha[:7]} {subject}')
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print('usage: dco_check.py BASE_SHA HEAD_SHA', file=sys.stderr)
        return 2
    found = failing(argv[1], argv[2])
    if not found:
        return 0
    print('These commits have no Signed-off-by line matching their author email:')
    for line in found:
        print(f'  {line}')
    print(FIX)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
