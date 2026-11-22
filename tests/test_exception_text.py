import ast
import functools
from pathlib import Path

import skopax

ROOT = Path(skopax.__file__).parent
TEXT_CALLS = frozenset({'str', 'repr', 'ascii', 'format'})
SAFE_CALLS = frozenset({'kind', 'error_kind', 'exc_kind', 'text_of', 'reason_of', 'type', 'isinstance', 'issubclass', 'getattr', 'id'})
SAFE_FROM = {'failure_text': 'skopax.mcptools.client'}
KEPT_ATTRS = frozenset({'text', 'code'})
LOGGERS = frozenset({'log', 'logger', 'logging'})
LEVELS = frozenset({'debug', 'info', 'warning', 'error', 'critical', 'exception'})
SELF_NAMES = frozenset({'self', 'cls'})
AUTHORED_BASE = ('skopax.errkind', 'Authored')
ALLOWED = {
    'skopax/mail.py Mailer._mapped: builds MailDown from text in MailDown(text)': (2, "the text is msg('mailserver.refused') around the SMTP server's own reply code and words: reply_of drops the password, joins the whitespace, keeps printable characters only and cuts it to 160 characters"),
    'skopax/mail.py Mailer._mapped: builds MailRefused from text in MailRefused(text)': (1, "the same SMTP reply text as the MailDown branch above, for a 5xx answer; reply_of drops the password, keeps printable characters only and cuts it to 160 characters"),
    'skopax/mail.py Mailer._mapped: builds MailRefused from text in MailRefused(text, recipient=True)': (1, "the same SMTP reply text for a 5xx refusal of one recipient; reply_of drops the password, keeps printable characters only and cuts it to 160 characters"),
    'skopax/slack.py slowed: builds SlackSlowed from after in SlackSlowed(min(max(float(after), 1.0), RATE_WAIT_MAX_S))': (1, 'after is the Retry-After header of a Slack 429, parsed to a number of seconds and bounded; no exception text is used'),
    'skopax/slack.py Slack.post: str(e) in str(e)': (1, 'compares the Slack error code with channel_not_found to pick the fallback channel; the text is neither kept nor shown'),
    'skopax/slack.py Slack.react: str(e) in str(e)': (1, 'compares the Slack error code with already_reacted; the text is neither kept nor shown'),
    'skopax/slack.py Slack.user_by_email: str(e) in str(e)': (1, 'compares the Slack error code with users_not_found; the text is neither kept nor shown'),
    'skopax/plugintools.py PluginTools._refuse: builds ToolError from text in ToolError(test_error(text) if isinstance(name, str) and test_cluster(name) else text)': (1, 'a contract ReadError message is the plug-in\'s own refusal for the model (P3-C9, P3-C27); call() keeps only its printable characters and cuts it before _refuse wraps it'),
    'skopax/plugintools.py PluginTools._refuse: builds BadInput from text in BadInput(test_error(text) if isinstance(name, str) and test_cluster(name) else text)': (1, 'the same refusal text as the ToolError branch above, raised as BadInput when the model sent an instance name outside the declared choices: it is the plug-in\'s own refusal for the model, call() keeps only its printable characters and cuts it before _refuse wraps it'),
    'skopax/mcpserver.py Tools.start_ask: builds ToolError from e in ToolError(str(e.detail))': (1, 'the HTTPException comes from chat_post, whose details are all fixed texts of app.py (FeatureOff, NotReady, the caps and the checks)'),
    'skopax/mcpserver.py Tools.card_action: builds ToolError from e in ToolError(str(e.detail))': (1, 'the HTTPException comes from add_action, whose details are all fixed texts of app.py (FeatureOff and the action checks)'),
    'skopax/mcptools/bridge.py RunTools.call: builds ToolError from e in ToolError(read_refused(tool, e, secrets))':
        (1, "ReadRefused carries the outside server's own error words; read_refused scrubs the connection's secrets from them, redacts them, cuts them to 300 characters and frames them as outside data, as the isError branch does"),
    "skopax/plugins/sharepoint/client.py Graph._failed: builds SourceDown from wait in SourceDown(msg('sharepoint.throttled', seconds=wait if wait.isdigit() else '60'))":
        (1, 'wait is the Retry-After header, used only when it is all digits, else the fixed 60'),
    "skopax/plugins/sharepoint/client.py Graph.token: builds Denied from e in Denied(msg('sharepoint.token_refused', status=e.code, error=error_code(e)), 'token', 'Client secret')":
        (1, 'only the numeric status and the OAuth error code pass; error_code keeps a code only when it matches [a-z_]{1,60}, never error_description or the secret'),
    "skopax/plugins/sharepoint/client.py Graph.token: msg of e in msg('sharepoint.token_refused', status=e.code, error=error_code(e))":
        (1, 'the same call as the Denied above: the numeric status and an OAuth error code that matches [a-z_]{1,60}'),
}
MODULES: dict[int, tuple[str, 'Index']] = {}


def module_of(path: Path, base: Path) -> str:
    parts = path.relative_to(base).with_suffix('').parts
    return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)


class Index:
    def __init__(self, trees: dict[str, ast.Module]):
        self.trees = trees
        MODULES.update({id(t): (m, self) for m, t in trees.items()})
        self.classes = {m: {n.name: n for n in t.body if isinstance(n, ast.ClassDef)} for m, t in trees.items()}
        self.imports = {m: self._imports(t) for m, t in trees.items()}
        self.authored = set()
        while True:
            more = {(m, c) for m, found in self.classes.items() for c, node in found.items()
                    if (m, c) not in self.authored and any(self.resolve(m, b) in self.authored | {AUTHORED_BASE} for b in node.bases)}
            if not more:
                break
            self.authored |= more

    @functools.cached_property
    def parents(self) -> dict[ast.AST, ast.AST]:
        return {child: parent for tree in self.trees.values() for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def _imports(self, tree: ast.Module) -> dict[str, tuple[str, str]]:
        out = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    full = f'{node.module}.{a.name}'
                    out[a.asname or a.name] = ('', full) if full in self.trees else (node.module, a.name)
        return out

    def resolve(self, module: str, node: ast.AST, depth: int = 0) -> tuple[str, str] | None:
        if depth > 8:
            return None
        if isinstance(node, ast.Name):
            if node.id in self.classes.get(module, {}):
                return module, node.id
            found = self.imports.get(module, {}).get(node.id)
            if found is None:
                return None
            if found[0] == '':
                return found
            return self.resolve(found[0], ast.Name(found[1]), depth + 1) or found
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner = self.resolve(module, node.value, depth + 1)
            if owner and owner[0] == '':
                return self.resolve(owner[1], ast.Name(node.attr), depth + 1) or (owner[1], node.attr)
        return None

    def builds_authored(self, module: str, call: ast.Call) -> str | None:
        found = self.resolve(module, call.func)
        return found[1] if found in self.authored else None


@functools.cache
def tree_index() -> Index:
    return Index({module_of(p, ROOT.parent): ast.parse(p.read_text(encoding='utf-8')) for p in ROOT.rglob('*.py')})


def is_exception_type(node: ast.AST | None) -> bool:
    for n in ast.walk(node) if node is not None else ():
        name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else ''
        if name == 'BaseException' or name.endswith(('Exception', 'Error')):
            return True
    return False


def params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    return fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs


def trusted(call: ast.Call, name: str, parents: dict) -> bool:
    owner_module = SAFE_FROM.get(name)
    root = call
    while root in parents:
        root = parents[root]
    module, index = MODULES.get(id(root), ('', None))
    return owner_module is not None and index is not None and (module == owner_module or index.imports.get(module, {}).get(name) == (owner_module, name))


def safe_parent(node: ast.AST, parents: dict, kept: frozenset | None) -> bool:
    parent = parents.get(node)
    if isinstance(parent, ast.Attribute):
        return kept is None or parent.attr in kept
    if not (isinstance(parent, ast.Call) and node is not parent.func and isinstance(parent.func, ast.Name)):
        return False
    return parent.func.id in SAFE_CALLS or trusted(parent, parent.func.id, parents)


def uses(node: ast.AST | None, name: str, parents: dict, kept: frozenset | None = None) -> bool:
    return node is not None and any(isinstance(n, ast.Name) and n.id == name and not safe_parent(n, parents, kept) for n in ast.walk(node))


def roots(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name:
            yield node.name, node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if len(node.args.args) == 2 and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == 'exception_handler' for d in node.decorator_list):
                yield node.args.args[1].arg, node.body
            for a in params(node):
                if is_exception_type(a.annotation):
                    yield a.arg, node.body


def holders(tree: ast.Module, derive: bool = False, index: Index | None = None, module: str = ''):
    parents = index.parents if index is not None else {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    defs: dict[str, dict[str, list]] = {}

    def tree_of(mod: str) -> ast.Module:
        return tree if mod == module else index.trees[mod]

    def defs_of(mod: str) -> dict[str, list]:
        if mod not in defs:
            defs[mod] = {}
            for node in ast.walk(tree_of(mod)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defs[mod].setdefault(node.name, []).append(node)
        return defs[mod]

    def imports_of(mod: str) -> dict[str, tuple[str, str]]:
        return index.imports.get(mod, {}) if index is not None else {}

    def scope_of(node: ast.AST, mod: str) -> list[ast.stmt]:
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.body
        return tree_of(mod).body

    def carries(value: ast.AST | None, name: str) -> bool:
        if isinstance(value, ast.Name):
            return value.id == name
        return derive and uses(value, name, parents, KEPT_ATTRS)

    def top(mod: str, fn_name: str, depth: int = 0) -> list[tuple]:
        if index is None or mod not in index.trees or depth > 8:
            return []
        found = [(n, mod) for n in index.trees[mod].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name]
        via = imports_of(mod).get(fn_name)
        return found or (top(via[0], via[1], depth + 1) if via and via[0] else [])

    def callees(call: ast.Call, mod: str) -> list[tuple]:
        func = call.func
        if isinstance(func, ast.Name):
            via = imports_of(mod).get(func.id)
            return [(fn, mod) for fn in defs_of(mod).get(func.id) or []] or (top(via[0], via[1]) if via and via[0] else [])
        if not isinstance(func, ast.Attribute):
            return []
        via = imports_of(mod).get(func.value.id) if isinstance(func.value, ast.Name) else None
        return (top(via[1], func.attr) if via and via[0] == '' else []) + [(fn, mod) for fn in defs_of(mod).get(func.attr) or []]

    def passed(call: ast.Call, name: str, mod: str):
        for fn, at in callees(call, mod):
            given = fn.args.posonlyargs + fn.args.args
            skip = 1 if isinstance(call.func, ast.Attribute) and given and given[0].arg in SELF_NAMES else 0
            for i, a in enumerate(call.args):
                if carries(a, name) and i + skip < len(given):
                    yield given[i + skip].arg, fn.body, at
            for k in call.keywords:
                if k.arg and carries(k.value, name) and any(p.arg == k.arg for p in params(fn)):
                    yield k.arg, fn.body, at

    todo, seen = [(name, body, module) for name, body in roots(tree)], set()
    while todo:
        name, body, mod = todo.pop()
        if (name, id(body)) in seen:
            continue
        seen.add((name, id(body)))
        yield name, body, mod
        for node in (n for statement in body for n in ast.walk(statement)):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and carries(node.value, name):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                todo += [(t.id, scope_of(node, mod), mod) for t in targets if isinstance(t, ast.Name)]
            if isinstance(node, ast.Call):
                todo += list(passed(node, name, mod))


def is_log_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LEVELS
            and isinstance(node.func.value, ast.Name) and node.func.value.id in LOGGERS)


def texts(node: ast.AST, name: str, parents: dict) -> str | None:
    raw = lambda x: uses(x, name, parents)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in TEXT_CALLS and any(raw(a) for a in node.args):
        return f'{node.func.id}({name})'
    if isinstance(node, ast.FormattedValue) and raw(node.value):
        return f'f-string of {name}'
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'format' and (any(raw(a) for a in node.args) or any(raw(k.value) for k in node.keywords)):
        return f'format of {name}'
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'msg' and any(raw(k.value) for k in node.keywords):
        return f'msg of {name}'
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and raw(node.right):
        return f'% of {name}'
    if is_log_call(node) and (any(raw(a) for a in node.args) or any(raw(k.value) for k in node.keywords)):
        return f'logs {name}'
    return None


def built(node: ast.AST, name: str, parents: dict, index: Index, module: str) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    cls = index.builds_authored(module, node)
    if cls and (any(uses(a, name, parents, KEPT_ATTRS) for a in node.args) or any(uses(k.value, name, parents, KEPT_ATTRS) for k in node.keywords)):
        return f'builds {cls} from {name}'
    return None


def owner(node: ast.AST, parents: dict) -> str:
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return '.'.join(reversed(names)) or '<module>'


def site(where: str, node: ast.AST, how: str, parents: dict, stable: bool) -> str:
    return f'{where} {owner(node, parents)}: {how} in {ast.unparse(node)}' if stable else f'{where}:{node.lineno} {how}'


def leaks(path: Path, base: Path = ROOT.parent, index: Index | None = None, stable: bool = False) -> list[str]:
    module = module_of(path, base)
    if index is None or module not in index.trees:
        index = Index({**(index or tree_index()).trees, module: ast.parse(path.read_text(encoding='utf-8'))})
    tree = index.trees[module]
    where = path.relative_to(base).as_posix()
    parents = index.parents
    out = {}
    for name, body, _ in holders(tree, index=index, module=module):
        for node in (n for statement in body for n in ast.walk(statement)):
            how = texts(node, name, parents)
            if how:
                out[(node.lineno, node.col_offset, how)] = site(where, node, how, parents, stable)
    for name, body, at in holders(tree, derive=True, index=index, module=module):
        for node in (n for statement in body for n in ast.walk(statement)):
            how = built(node, name, parents, index, at)
            if how:
                out[(node.lineno, node.col_offset, how)] = site(where, node, how, parents, stable)
    return sorted(out.values())


def found_in_tree() -> list[str]:
    return sorted(leak for path in ROOT.rglob('*.py') for leak in leaks(path, index=tree_index(), stable=True))


def test_no_caught_exception_text_becomes_a_string_outside_the_allowed_sites():
    # when
    found = found_in_tree()

    # then
    assert [leak for leak in found if leak not in ALLOWED] == []


def test_every_allowed_site_is_still_there_and_has_a_reason():
    # when
    found = found_in_tree()

    # then
    assert {key: found.count(key) for key in ALLOWED} == {key: count for key, (count, _) in ALLOWED.items()}
    assert all(len(reason) > 20 for _, reason in ALLOWED.values())


def test_the_authored_classes_are_found_through_their_bases_and_imports():
    # when
    index = tree_index()
    authored = {c for _, c in index.authored}

    # then
    assert {'NotFound', 'Refused', 'ToolError', 'SlackDown', 'Interrupted', 'Refusal', 'TicketRefused', 'InvalidInstance'} <= authored
    assert index.resolve('skopax.app', ast.Attribute(ast.Name('page_actions'), 'Refused')) == ('skopax.actions', 'Refused')
    assert index.resolve('skopax.app', ast.Attribute(ast.Name('chatui'), 'Refused')) not in index.authored


def test_the_scan_finds_each_way_of_turning_a_caught_exception_into_text(tmp_path):
    # given
    sample = tmp_path / 'sample.py'
    sample.write_text(
        'def one():\n'
        '    try:\n'
        '        go()\n'
        '    except ValueError as e:\n'
        '        a = str(e)\n'
        "        b = f'failed: {e}'\n"
        "        c = 'failed: {}'.format(e)\n"
        "        d = 'failed: %s' % e\n"
        "        f = 'failed: %s %s' % (a, e)\n"
        "        g = msg('x.failed', error=e)\n"
        '        h = repr(e)\n'
        '        i = kind(e)\n'
        '        j = type(e).__name__\n'
        '        k = e.code\n'
        "        m = str(e or '')\n"
        "        n = f'{e.code}: {error_kind(e)}'\n"
        '\n'
        '\n'
        'def two():\n'
        '    crash = None\n'
        '    try:\n'
        '        go()\n'
        '    except Exception as e:\n'
        '        crash = e\n'
        '    finally:\n'
        '        three(1, crash)\n'
        '        Box().four(failure=crash)\n'
        '\n'
        '\n'
        'def three(n, why):\n'
        "    log.warning('%s failed: %s', n, why)\n"
        "    return f'{n}: {why}'\n"
        '\n'
        '\n'
        'class Box:\n'
        '    def four(self, failure=None):\n'
        '        alias = failure\n'
        '        return str(alias)\n'
        '\n'
        '\n'
        'def five(error: BaseException) -> str:\n'
        "    return f'error: {error}'\n"
        '\n'
        '\n'
        '@app.exception_handler(KeyError)\n'
        'async def handler(_, err):\n'
        '    return str(err)\n'
        '\n'
        '\n'
        'def six(e):\n'
        "    return f'{e}'\n", encoding='utf-8')

    # when
    found = leaks(sample, tmp_path)

    # then
    assert found == sorted(['sample.py:5 str(e)', 'sample.py:6 f-string of e', 'sample.py:7 format of e', 'sample.py:8 % of e', 'sample.py:9 % of e', 'sample.py:10 msg of e',
                            'sample.py:11 repr(e)', 'sample.py:15 str(e)', 'sample.py:31 logs why', 'sample.py:32 f-string of why', 'sample.py:38 str(alias)',
                            'sample.py:42 f-string of error', 'sample.py:47 str(err)'])


def test_the_scan_finds_an_authored_exception_built_from_a_caught_one(tmp_path):
    # given
    sample = tmp_path / 'sample.py'
    sample.write_text(
        'from skopax.actions import Refused\n'
        'from skopax import mcpserver\n'
        'from skopax import chatui\n'
        'from skopax.errkind import Authored\n'
        '\n'
        '\n'
        'class Mine(Authored, Exception):\n'
        '    pass\n'
        '\n'
        '\n'
        'def one():\n'
        '    try:\n'
        '        go()\n'
        '    except ValueError as e:\n'
        '        raise Refused(e)\n'
        '    except KeyError as e:\n'
        '        raise mcpserver.ToolError(e.args[0])\n'
        '    except OSError as e:\n'
        "        text = ''.join(c for c in e.strerror if c.isprintable())\n"
        '        raise wrap(text)\n'
        '    except TypeError as e:\n'
        '        raise Mine(e.text if isinstance(e, Mine) else kind(e), e.code)\n'
        '    except LookupError as e:\n'
        '        raise chatui.Refused(e.args[0])\n'
        '\n'
        '\n'
        'def wrap(text):\n'
        '    return Mine(text)\n', encoding='utf-8')

    # when
    found = leaks(sample, tmp_path)

    # then
    assert found == ['sample.py:15 builds Refused from e', 'sample.py:17 builds ToolError from e', 'sample.py:28 builds Mine from text']


def test_only_the_mcp_client_failure_mapper_is_a_safe_call_and_its_body_is_still_scanned(tmp_path):
    # given
    sample = tmp_path / 'sample.py'
    sample.write_text(
        'from skopax.errkind import Authored\n'
        '\n'
        '\n'
        'class Failure(Authored, Exception):\n'
        '    pass\n'
        '\n'
        '\n'
        'def failure_text(e: BaseException) -> str:\n'
        "    return f'failed: {e}'\n"
        '\n'
        '\n'
        'def run():\n'
        '    try:\n'
        '        go()\n'
        '    except Exception as e:\n'
        '        raise Failure(failure_text(e)) from None\n', encoding='utf-8')

    # when
    found = leaks(sample, tmp_path)

    # then
    assert found == ['sample.py:16 builds Failure from e', 'sample.py:9 f-string of e'], 'a failure_text that is not the MCP client\'s own is not trusted, and a body is still scanned'


def test_a_site_is_keyed_by_its_function_and_expression_not_its_line(tmp_path):
    # given
    body = ('class Box:\n'
            '    def ask(self):\n'
            '        try:\n'
            '            go()\n'
            '        except KeyError as e:\n'
            '            return str(e)\n')
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    first, moved = tmp_path / 'a' / 'm.py', tmp_path / 'b' / 'm.py'
    first.write_text(body, encoding='utf-8')
    moved.write_text('\n\n\n' + body + '\n\n    def tell(self):\n        try:\n            go()\n        except KeyError as e:\n            return str(e)\n', encoding='utf-8')

    # when
    keys, moved_keys = leaks(first, tmp_path / 'a', stable=True), leaks(moved, tmp_path / 'b', stable=True)

    # then
    assert keys == ['m.py Box.ask: str(e) in str(e)']
    assert moved_keys == ['m.py Box.ask: str(e) in str(e)', 'm.py Box.tell: str(e) in str(e)']
    assert (leaks(first, tmp_path / 'a'), leaks(moved, tmp_path / 'b')) == (['m.py:6 str(e)'], ['m.py:16 str(e)', 'm.py:9 str(e)'])


def test_two_same_sites_in_one_function_count_twice(tmp_path):
    # given
    sample = tmp_path / 'm.py'
    sample.write_text('class Box:\n'
                      '    def ask(self):\n'
                      '        try:\n'
                      '            go()\n'
                      '        except KeyError as e:\n'
                      "            if str(e) == 'x':\n"
                      '                return str(e)\n', encoding='utf-8')

    # when
    keys = leaks(sample, tmp_path, stable=True)

    # then
    assert keys == ['m.py Box.ask: str(e) in str(e)', 'm.py Box.ask: str(e) in str(e)']


def test_the_scan_follows_a_caught_exception_into_a_helper_of_another_module(tmp_path):
    # given
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'pkg' / '__init__.py').write_text('', encoding='utf-8')
    (tmp_path / 'pkg' / 'helper.py').write_text('def refusal(error, lang):\n'
                                                '    return str(error)\n'
                                                '\n'
                                                '\n'
                                                'def named(error, lang):\n'
                                                '    return kind(error)\n', encoding='utf-8')
    caller = tmp_path / 'pkg' / 'caller.py'
    caller.write_text('from pkg.helper import named, refusal\n'
                      '\n'
                      '\n'
                      'def answer():\n'
                      '    try:\n'
                      '        go()\n'
                      '    except Refused as e:\n'
                      "        named(e, 'en')\n"
                      "        return refusal(e, 'en')\n", encoding='utf-8')
    index = Index({module_of(p, tmp_path): ast.parse(p.read_text(encoding='utf-8')) for p in tmp_path.rglob('*.py')})

    # when
    found = leaks(caller, tmp_path, index=index, stable=True)

    # then
    assert found == ['pkg/caller.py refusal: str(error) in str(error)'], 'the helper that turns the caught exception into text is named, the one that only names its kind is not'


def test_the_scan_follows_a_caught_exception_through_a_helper_that_passes_it_on(tmp_path):
    # given
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'pkg' / '__init__.py').write_text('', encoding='utf-8')
    (tmp_path / 'pkg' / 'deep.py').write_text('def deep(error):\n'
                                              '    return str(error)\n', encoding='utf-8')
    (tmp_path / 'pkg' / 'mid.py').write_text('from pkg.deep import deep\n'
                                             '\n'
                                             '\n'
                                             'def mid(e):\n'
                                             '    return deep(e)\n'
                                             '\n'
                                             '\n'
                                             'def near(e):\n'
                                             '    return local(e)\n'
                                             '\n'
                                             '\n'
                                             'def local(failure):\n'
                                             "    return f'{failure}'\n", encoding='utf-8')
    caller = tmp_path / 'pkg' / 'caller.py'
    caller.write_text('from pkg.mid import mid, near\n'
                      '\n'
                      '\n'
                      'def answer():\n'
                      '    try:\n'
                      '        go()\n'
                      '    except Refused as e:\n'
                      '        near(e)\n'
                      '        return mid(e)\n', encoding='utf-8')
    index = Index({module_of(p, tmp_path): ast.parse(p.read_text(encoding='utf-8')) for p in tmp_path.rglob('*.py')})

    # when
    found = leaks(caller, tmp_path, index=index, stable=True)

    # then
    assert found == ['pkg/caller.py deep: str(error) in str(error)', 'pkg/caller.py local: f-string of failure in {failure}'], 'each hop resolves its callee through its own module'


def test_the_scan_follows_a_caught_exception_into_a_function_called_through_its_module(tmp_path):
    # given
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'pkg' / '__init__.py').write_text('', encoding='utf-8')
    (tmp_path / 'pkg' / 'c.py').write_text('def deep(error):\n'
                                           '    return str(error)\n'
                                           '\n'
                                           '\n'
                                           'def named(error):\n'
                                           '    return kind(error)\n', encoding='utf-8')
    caller = tmp_path / 'pkg' / 'caller.py'
    caller.write_text('from pkg import c\n'
                      '\n'
                      '\n'
                      'def answer():\n'
                      '    try:\n'
                      '        go()\n'
                      '    except Refused as e:\n'
                      '        c.named(e)\n'
                      '        return c.deep(e)\n', encoding='utf-8')
    index = Index({module_of(p, tmp_path): ast.parse(p.read_text(encoding='utf-8')) for p in tmp_path.rglob('*.py')})

    # when
    found = leaks(caller, tmp_path, index=index, stable=True)

    # then
    assert found == ['pkg/caller.py deep: str(error) in str(error)'], 'module.func(...) is resolved through the import index'
