PROTOCOL = 1
MINOR = 0
RELEASE = f'{PROTOCOL}.{MINOR}'


def major(release) -> int | None:
    head = str(release or '').split('.', 1)[0]
    return int(head) if head.isdigit() else None
