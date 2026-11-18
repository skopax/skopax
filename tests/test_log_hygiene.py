import ast
from pathlib import Path

import skopax
from tests.test_exception_text import holders

ROOT = Path(skopax.__file__).parent
LOGGERS = frozenset({'log', 'logger', 'logging'})
LEVELS = frozenset({'debug', 'info', 'warning', 'error', 'critical', 'exception'})
SAFE_CALLS = frozenset({'kind', 'error_kind', 'exc_kind', 'type', 'isinstance', 'getattr'})


def log_calls(node: ast.AST):
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in LEVELS:
            owner = call.func.value
            if isinstance(owner, ast.Name) and owner.id in LOGGERS:
                yield call


def raw_uses(call: ast.Call, name: str) -> list[int]:
    parents = {child: parent for parent in ast.walk(call) for child in ast.iter_child_nodes(parent)}
    found = []
    for node in ast.walk(call):
        if isinstance(node, ast.Name) and node.id == name and node is not call.func:
            parent = parents.get(node)
            safe = isinstance(parent, ast.Attribute) or (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id in SAFE_CALLS)
            if not safe:
                found.append(node.lineno)
    return found


def caught(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            yield node.name, node.body
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.args.args) == 2:
            if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == 'exception_handler' for d in node.decorator_list):
                yield node.args.args[1].arg, node.body


def leaks(path: Path, base: Path = ROOT.parent) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    where = path.relative_to(base)
    out = []
    for _, body in caught(tree):
        for statement in body:
            out += [f'{where}:{call.lineno} log.exception' for call in log_calls(statement) if call.func.attr == 'exception']
    for name, body, _ in holders(tree):
        for statement in body:
            for call in log_calls(statement):
                out += [f'{where}:{line} logs {name}' for line in raw_uses(call, name)]
    return sorted(set(out))


def test_no_log_line_carries_the_text_of_a_caught_exception():
    # when
    found = sorted({leak for path in ROOT.rglob('*.py') for leak in leaks(path)})

    # then
    assert found == []


def test_the_scan_finds_each_way_of_logging_a_caught_exception(tmp_path):
    # given
    sample = tmp_path / 'sample.py'
    sample.write_text(
        'def one():\n'
        '    try:\n'
        '        go()\n'
        '    except ValueError as e:\n'
        "        log.warning('failed: %s', e)\n"
        "        log.warning('failed: %s', str(e))\n"
        "        log.warning(f'failed: {e}')\n"
        "        log.warning('failed: %s', kind(e))\n"
        "        log.warning('failed: %s', type(e).__name__)\n"
        '    except KeyError:\n'
        "        log.exception('failed')\n"
        '\n'
        '@app.exception_handler(KeyError)\n'
        'async def handler(_, err):\n'
        "    log.error('failed: %s', err)\n"
        '\n'
        'def two():\n'
        '    try:\n'
        '        go()\n'
        '    except OSError as denied:\n'
        '        three(denied)\n'
        '\n'
        'def three(why):\n'
        "    log.warning('refused: %s', why)\n", encoding='utf-8')

    # when
    found = leaks(sample, tmp_path)

    # then
    assert sorted(found) == sorted(['sample.py:5 logs e', 'sample.py:6 logs e', 'sample.py:7 logs e', 'sample.py:11 log.exception', 'sample.py:15 logs err', 'sample.py:24 logs why'])


STATUS_FILES = ('clearnotes.py', 'reminders.py', 'selfreview.py', 'operators.py', 'postdeploy.py')


def status_leaks(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    out = []
    for name, body in caught(tree):
        if not name:
            continue
        for statement in body:
            for node in ast.walk(statement):
                if isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name) and node.value.id == name:
                    out.append(f'{path.name}:{node.lineno} formats {name}')
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'str' and any(isinstance(a, ast.Name) and a.id == name for a in node.args):
                    out.append(f'{path.name}:{node.lineno} str({name})')
    return out


def test_no_status_string_in_these_modules_carries_exception_text():
    # when
    found = [leak for name in STATUS_FILES for leak in status_leaks(ROOT / name)]

    # then
    assert found == []
