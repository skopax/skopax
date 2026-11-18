from skopax.messages import msg
from skopax.mcptools import LABEL_MAX

WARNINGS_VERSION = 1
IDS = ('readers', 'account', 'provider', 'kept', 'steer', 'personal', 'cost')
TOOL_SLOT = '{tool}'


def quoted(folder: str) -> str:
    flat = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in str(folder)).split())
    return '"' + flat[:LABEL_MAX] + '"'


def readers(knowledge: bool, folder: str | None, public_read: bool) -> str:
    tail = '_public' if public_read else ''
    if knowledge and folder is not None:
        return msg(f'mcptools.warn.readers_folder{tail}', folder=quoted(folder))
    return msg(f'mcptools.warn.readers_account{tail}')


def items(knowledge: bool, folder: str | None, public_read: bool = False) -> list[dict]:
    return [{'id': IDS[0], 'text': readers(knowledge, folder, public_read)}] + [{'id': i, 'text': msg(f'mcptools.warn.{i}')} for i in IDS[1:]]


def view(knowledge: bool, folder: str | None, public_read: bool = False) -> dict:
    return {'title': msg('mcptools.warn.title'), 'button': msg('mcptools.warn.button'), 'toolText': msg('mcptools.warn.tool', tool=TOOL_SLOT), 'version': WARNINGS_VERSION,
            'items': items(knowledge, folder, public_read)}
