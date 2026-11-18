import re


def yaml_comment_lines(text: str) -> list[int]:
    found = []
    block_indent = None
    for n, line in enumerate(text.splitlines(), 1):
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                continue
            block_indent = None
        if line.lstrip().startswith('#') or re.search(r'\s#', line):
            found.append(n)
        if re.search(r':\s+[|>][+-]?\s*$', line) or re.search(r'^\s*-\s+[|>][+-]?\s*$', line):
            block_indent = indent
    return found
