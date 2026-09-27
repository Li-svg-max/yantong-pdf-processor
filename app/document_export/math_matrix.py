"""Restricted matrix grammar shared by PDF and native Word export."""
import re

ENVIRONMENTS = {"matrix", "pmatrix", "bmatrix", "vmatrix", "cases", "aligned"}
TOKEN = re.compile(r"\\(begin|end)\{([^{}]+)\}")


def split_cells(body, separator):
    parts, start, depth, index = [], 0, 0, 0
    while index < len(body):
        char = body[index]
        if char == "{": depth += 1
        elif char == "}": depth -= 1
        if depth == 0 and body.startswith(separator, index):
            parts.append(body[start:index].strip())
            index += len(separator)
            start = index
        else:
            index += 1
    parts.append(body[start:].strip())
    return parts


def matrix_parts(expression):
    """Only top-level, non-nested, rectangular matrices; fail closed."""
    tokens = list(TOKEN.finditer(expression))
    if not tokens or len(tokens) % 2:
        raise ValueError("打印公式矩阵环境不完整")
    if len(tokens) > 8:
        raise ValueError("打印公式矩阵过多，请拆分")
    parts, end = [], 0
    for opening, closing in zip(tokens[::2], tokens[1::2]):
        env = opening.group(2)
        if opening.group(1) != "begin" or closing.group(1) != "end" or env != closing.group(2) or env not in ENVIRONMENTS:
            raise ValueError("打印公式仅支持常规矩阵，不支持嵌套或其他环境")
        before = expression[end:opening.start()]
        # Do not flatten a matrix occurring in a fraction, exponent or root.
        prefix = expression[:opening.start()]
        if prefix.count("{") != prefix.count("}"):
            raise ValueError("打印公式请将矩阵单独列出，不要嵌入分式或上下标")
        if before.strip(): parts.append(before.strip())
        body = expression[opening.end():closing.start()]
        rows = split_cells(body, r"\\")
        if rows and not rows[-1]: rows.pop()
        cells = [split_cells(row, "&") for row in rows]
        if env == 'cases' and any(len(row) != 2 for row in cells):
            raise ValueError('打印分段函数须包含表达式与对应条件两列')
        if not cells or len(cells) > 8 or not 1 <= len(cells[0]) <= 8 or any(len(row) != len(cells[0]) or env != 'aligned' and any(not cell for cell in row) for row in cells):
            raise ValueError("打印公式矩阵须完整且不超过8行8列")
        parts.append((env, cells))
        end = closing.end()
    if expression[end:].strip(): parts.append(expression[end:].strip())
    if any(isinstance(part, str) and re.search(r"\\(?:begin|end)\b", part) for part in parts):
        raise ValueError("打印公式矩阵环境不完整")
    return parts
