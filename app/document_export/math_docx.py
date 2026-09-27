"""A bounded MathML -> native Word equation path (no shell or external TeX)."""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from .math_pdf import _MATH, _validate_formula, _BUDGET, MAX_FORMULAS_PER_PARAGRAPH, MAX_DOCUMENT_FORMULAS
from .math_matrix import matrix_parts


def _element(name, children=()):
    node = OxmlElement('m:' + name)
    for child in children:
        node.append(child)
    return node


def _run(text):
    node = _element('r')
    properties = OxmlElement('w:rPr')
    fonts = OxmlElement('w:rFonts')
    fonts.set(qn('w:ascii'), 'Cambria Math')
    fonts.set(qn('w:hAnsi'), 'Cambria Math')
    properties.append(fonts)
    node.append(properties)
    content = _element('t')
    content.set(qn('xml:space'), 'preserve')
    content.text = text
    node.append(content)
    return node


def _convert(node, depth=0):
    if depth > 32:
        raise ValueError('Word 公式嵌套过深，请拆分后重试')
    tag = node.tag.rsplit('}', 1)[-1]
    children = list(node)
    def contents(child):
        return _convert(child, depth + 1)
    if tag in {'math', 'mrow', 'mstyle', 'semantics'}:
        result, index = [], 0
        while index < len(children):
            # Stretch paired delimiters around a matrix, not fixed-height runs.
            if index + 2 < len(children) and children[index].tag.endswith('}mo') and children[index+1].tag.endswith('}mtable') and children[index+2].tag.endswith('}mo'):
                left, right = children[index].text, children[index+2].text
                if (left, right) in {('(', ')'), ('[', ']'), ('|', '|')}:
                    props = _element('dPr')
                    for name, value in [('begChr', left), ('endChr', right)]:
                        prop = _element(name); prop.set(qn('m:val'), value); props.append(prop)
                    result.append(_element('d', [props, _element('e', contents(children[index+1]))]))
                    index += 3
                    continue
            result.extend(contents(children[index])); index += 1
        return result
    if tag == 'mtable':
        if not children or len(children) > 8:
            raise ValueError('Word 矩阵行数超出限制')
        columns = len(children[0])
        if not 1 <= columns <= 8 or any(len(row) != columns for row in children):
            raise ValueError('Word 矩阵列数不一致')
        return [_element('m', [_element('mr', [_element('e', contents(cell)) for cell in row]) for row in children])]
    if tag == 'mtd':
        return [item for child in children for item in contents(child)]
    if tag in {'mi', 'mn', 'mo', 'mtext'}:
        return [_run(node.text or '')]
    if tag == 'mspace':
        return [_run(' ')]
    layouts = {
        'mfrac': ('f', ('num', 'den')),
        'msub': ('sSub', ('e', 'sub')),
        'msup': ('sSup', ('e', 'sup')),
        'msubsup': ('sSubSup', ('e', 'sub', 'sup')),
        'munder': ('limLow', ('e', 'lim')),
        'mover': ('limUpp', ('e', 'lim')),
    }
    if tag in layouts:
        outer, slots = layouts[tag]
        if len(children) != len(slots):
            raise ValueError('Word 公式结构不完整')
        return [_element(outer, [_element(slot, contents(child)) for slot, child in zip(slots, children)])]
    if tag == 'munderover' and len(children) == 3:
        lower = _element('limLow', [_element('e', contents(children[0])), _element('lim', contents(children[1]))])
        return [_element('limUpp', [_element('e', [lower]), _element('lim', contents(children[2]))])]
    if tag in {'msqrt', 'mroot'}:
        radical = _element('rad')
        if tag == 'msqrt':
            props = _element('radPr')
            hidden = _element('degHide'); hidden.set(qn('m:val'), '1'); props.append(hidden)
            radical.append(props); radical.append(_element('deg'))
            radical.append(_element('e', [item for child in children for item in contents(child)]))
        elif len(children) == 2:
            radical.append(_element('deg', contents(children[1])))
            radical.append(_element('e', contents(children[0])))
        else:
            raise ValueError('Word 根式结构不完整')
        return [radical]
    # Never silently discard an unfamiliar operation or print its raw TeX.
    raise ValueError('此公式暂不支持 Word 可编辑排版，请改写为常规分式或上下标')


def equation(expression):
    expression = _validate_formula(expression)
    budget = _BUDGET.get()
    if budget is not None:
        budget['count'] += 1
        if budget['count'] > MAX_DOCUMENT_FORMULAS:
            raise ValueError('本次打印公式过多，请分批组卷')
    try:
        from latex2mathml.converter import convert
        if re.search(r'\\begin\{(?:cases|aligned)\}', expression):
            children = []
            for part in matrix_parts(expression):
                if isinstance(part, str):
                    children.extend(list(equation(part)))
                    continue
                env, rows = part
                columns = _element('mcs')
                for index in range(len(rows[0])):
                    align = 'left' if env == 'cases' or env == 'aligned' and index % 2 else 'right' if env == 'aligned' else 'center'
                    count = _element('count'); count.set(qn('m:val'), '1')
                    jc = _element('mcJc'); jc.set(qn('m:val'), align)
                    columns.append(_element('mc', [_element('mcPr', [count, jc])]))
                matrix = _element('m', [_element('mPr', [columns])] + [_element('mr', [_element('e', list(equation(cell)) if cell else []) for cell in row]) for row in rows])
                delimiters = {'cases': ('{', ''), 'pmatrix': ('(', ')'), 'bmatrix': ('[', ']'), 'vmatrix': ('|', '|')}
                if env in delimiters:
                    properties = _element('dPr')
                    for key, value in zip(('begChr', 'endChr'), delimiters[env]):
                        prop = _element(key); prop.set(qn('m:val'), value); properties.append(prop)
                    matrix = _element('d', [properties, _element('e', [matrix])])
                children.append(matrix)
            return _element('oMath', children)
        markup = convert(expression)
        if len(markup) > 100_000:
            raise ValueError('Word 公式结构过大')
        root = ET.fromstring(markup)
        if any('\\' in (node.text or '') for node in root.iter()):
            raise ValueError('Word 公式包含不支持的命令，请检查后重试')
        if len(list(root.iter())) > 2048:
            raise ValueError('Word 公式结构过大')
        return _element('oMath', _convert(root))
    except ImportError as error:
        raise ValueError('打印服务缺少 Word 公式依赖，请管理员更新服务') from error
    except (ET.ParseError, RecursionError) as error:
        raise ValueError('Word 公式无法排版，请检查公式后重试') from error


def append_math_text(paragraph, value, format_run):
    source = str(value or ' ')
    matches = list(_MATH.finditer(source))
    unmatched = _MATH.sub('', source)
    if re.search(r'(?<!\\)\$|\\[\[(]', unmatched) and re.search(r'\\[A-Za-z]+', unmatched):
        raise ValueError('公式分隔符不完整，请检查后重试')
    if len(matches) > MAX_FORMULAS_PER_PARAGRAPH:
        raise ValueError('单段公式超过64个，请拆分题目后重试')
    position = 0
    for match in matches:
        if match.start() > position:
            format_run(paragraph.add_run(source[position:match.start()]))
        display = match.group(1) is not None or match.group(4) is not None
        if display: paragraph.add_run().add_break()
        expression = next(group for group in match.groups() if group is not None)
        paragraph._p.append(equation(expression))
        if display: paragraph.add_run().add_break()
        position = match.end()
    if position < len(source):
        format_run(paragraph.add_run(source[position:]))
