"""Bounded local MathText rendering for PDF paragraphs; never run a TeX engine.

Only generated in-memory PNG data is inserted into ReportLab. User text is
escaped; user paths/URLs cannot become image sources or ReportLab markup.
"""
from __future__ import annotations

import base64
import html
import math
import re
import threading
import warnings
from contextvars import ContextVar
from functools import lru_cache, wraps
from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw
from .math_matrix import matrix_parts
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph

MAX_FORMULA_CHARS = 1024
MAX_NESTING = 12
MAX_FORMULAS_PER_PARAGRAPH = 64
MAX_WIDTH_POINTS = 520
MAX_HEIGHT_POINTS = 180
MAX_RASTER_PIXELS = 1_500_000
MAX_PNG_BYTES = 128 * 1024
MAX_DOCUMENT_FORMULAS = 512
MAX_DOCUMENT_PNG_BYTES = 8 * 1024 * 1024
DPI = 300
_LOCK = threading.RLock()
_BUDGET = ContextVar("pdf_math_budget", default=None)
_MATH = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$|(?<!\\)\$(?!\$)(.+?)(?<!\\)\$|\\\((.+?)\\\)|\\\[(.+?)\\\]", re.DOTALL)
_COMMAND = re.compile(r"\\([A-Za-z]+|.)", re.DOTALL)
# MathText has no I/O execution facility. The explicit deny-list also makes
# unsupported full-TeX input fail before layout, with a user-readable message.
_FORBIDDEN = {"input", "include", "includegraphics", "write", "write18", "openout", "read", "usepackage", "documentclass", "catcode", "def", "newcommand", "renewcommand", "csname", "special", "href", "url", "htmlClass"}


def bounded_math_document(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _BUDGET.set({"count": 0, "bytes": 0})
        try:
            return function(*args, **kwargs)
        finally:
            _BUDGET.reset(token)
    return wrapped


def _validate_formula(expression: str) -> str:
    expression = expression.strip()
    if not expression or len(expression) > MAX_FORMULA_CHARS:
        raise ValueError("打印公式为空或超过1024字符，请拆分后重试")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in expression):
        raise ValueError("打印公式包含不支持的控制字符")
    if any(match.group(1) in _FORBIDDEN for match in _COMMAND.finditer(expression)):
        raise ValueError("打印暂不支持此公式命令，请改写为常规分式、上下标或补充题目图片")
    depth = 0
    for match in re.finditer(r"(?<!\\)[{}]", expression):
        depth += 1 if match.group() == "{" else -1
        if depth < 0 or depth > MAX_NESTING:
            raise ValueError("打印公式括号不完整或嵌套超过12层，请检查后重试")
    if depth:
        raise ValueError("打印公式括号不完整，请检查后重试")
    if re.search(r"\\(?:begin|end)\b", expression):
        matrix_parts(expression)
    # Do not strip unknown commands or change any mathematical operands.
    return expression


def _canonical_arguments(source):
    """Brace valid one-token TeX arguments for MathText; preserve every operand."""
    index = 0
    def argument():
        nonlocal index
        while index < len(source) and source[index].isspace(): index += 1
        if index >= len(source): raise ValueError("打印公式参数不完整")
        if source[index] == "{":
            index += 1
            result = sequence("}")
            if index >= len(source): raise ValueError("打印公式括号不完整")
            index += 1
            return "{" + result + "}"
        return "{" + token() + "}"
    def token():
        nonlocal index
        if source[index] != "\\":
            char = source[index]; index += 1; return char
        match = _COMMAND.match(source, index)
        if not match: raise ValueError("打印公式命令不完整")
        name = match.group(1); index = match.end()
        if name in {"le", "ge"}: return "\\" + name + "q "
        if name in {"frac", "dfrac", "tfrac"}:
            return r"\frac" + argument() + argument()
        if name == "sqrt":
            optional = ""
            if index < len(source) and source[index] == "[":
                end = source.find("]", index)
                if end < 0: raise ValueError("打印公式根指数不完整")
                optional = source[index:end+1]; index = end+1
            return r"\sqrt" + optional + argument()
        return match.group()
    def sequence(stop=None):
        nonlocal index
        value = ""
        while index < len(source) and source[index] != stop:
            if source[index] == "{":
                value += argument()
            else:
                value += token()
        return value
    return sequence()


@lru_cache(maxsize=64)
def _raster_formula(expression: str, font_size: float) -> tuple[bytes, float, float, float]:
    expression = _validate_formula(expression)
    if not 6 <= font_size <= 32:
        raise ValueError("打印公式字号超出安全范围")
    if r"\begin" in expression:
        return _raster_matrices(expression, font_size)
    try:
        import matplotlib
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import MathTextParser
    except ImportError as error:
        raise ValueError("打印服务尚未安装数学公式排版依赖，请管理员更新服务") from error
    # MathText parser and rc settings are process-global; serialize this small
    # bounded operation. Never import pyplot, call a shell, or enable usetex.
    with _LOCK, warnings.catch_warnings(), matplotlib.rc_context({"text.usetex": False, "mathtext.fontset": "stix", "mathtext.fallback": "stix"}):
        warnings.simplefilter("error", UserWarning)
        prop = FontProperties(family="STIXGeneral", size=font_size)
        wrapped = "$" + _canonical_arguments(expression) + "$"
        try:
            # Measure vector layout BEFORE any pixel-buffer allocation.
            vector = MathTextParser("path").parse(wrapped, dpi=72, prop=prop)
            width, height = float(vector.width), float(vector.height)
            pixels = math.ceil(width * DPI / 72 + 4) * math.ceil(height * DPI / 72 + 4)
            if not (0 < width <= MAX_WIDTH_POINTS and 0 < height <= MAX_HEIGHT_POINTS) or pixels > MAX_RASTER_PIXELS:
                raise ValueError("打印公式尺寸过大，请拆分公式或使用单栏排版")
            raster = MathTextParser("agg").parse(wrapped, dpi=DPI, prop=prop)
            alpha = np.asarray(raster.image, dtype=np.uint8)
            if alpha.size > MAX_RASTER_PIXELS or not alpha.size:
                raise ValueError("打印公式图片超出尺寸限制")
            image = Image.new("RGBA", (alpha.shape[1], alpha.shape[0]), (0, 0, 0, 0))
            image.putalpha(Image.fromarray(alpha))
            output = BytesIO()
            image.save(output, format="PNG")
            data = output.getvalue()
            if len(data) > MAX_PNG_BYTES:
                raise ValueError("打印公式图片过大，请拆分后重试")
            return data, alpha.shape[1] * 72 / DPI, alpha.shape[0] * 72 / DPI, float(raster.depth) * 72 / DPI
        except (ValueError, RuntimeError, UserWarning, RecursionError) as error:
            # Do not return parser internals or silently substitute a bad glyph.
            if isinstance(error, ValueError) and str(error).startswith("打印公式"):
                raise
            raise ValueError("有一处公式暂无法安全排版，请核对LaTeX写法后重试；未生成含错误公式的PDF") from error


def _bounded_canvas(width, height):
    if width * 72 / DPI > MAX_WIDTH_POINTS or height * 72 / DPI > MAX_HEIGHT_POINTS or width * height > MAX_RASTER_PIXELS:
        raise ValueError("打印公式尺寸过大，请拆分公式或使用单栏排版")
    return Image.new("RGBA", (width, height), (0, 0, 0, 0))


def _raster_matrices(expression, font_size):
    scale = DPI / 72
    gap = max(4, round(font_size * scale * 0.5))
    def image_of(source):
        if not source.strip():
            return Image.new('RGBA', (1, 1)), 0
        png, _, _, depth = _raster_formula(source, font_size)
        return Image.open(BytesIO(png)).convert("RGBA"), round(depth * scale)
    pieces = []
    for part in matrix_parts(expression):
        if isinstance(part, str):
            pieces.append(image_of(part))
            continue
        env, rows = part
        images = [[image_of(cell) for cell in row] for row in rows]
        widths = [max(row[c][0].width for row in images) for c in range(len(rows[0]))]
        ascents = [max(img.height - depth for img, depth in row) for row in images]
        descents = [max(depth for _, depth in row) for row in images]
        height = sum(a + d for a, d in zip(ascents, descents)) + gap * (len(rows) + 1)
        width = sum(widths) + gap * (len(widths) + 3)
        canvas = _bounded_canvas(width, height)
        y = gap
        for row, ascent, descent in zip(images, ascents, descents):
            x = 2 * gap
            for column, ((img, depth), col_width) in enumerate(zip(row, widths)):
                offset = 0 if env == 'cases' or env == 'aligned' and column % 2 else col_width - img.width if env == 'aligned' else (col_width - img.width) // 2
                canvas.alpha_composite(img, (x + offset, y + ascent - img.height + depth))
                x += col_width + gap
            y += ascent + descent + gap
        draw = ImageDraw.Draw(canvas)
        stroke = max(1, round(scale * 0.6))
        if env == "pmatrix":
            draw.arc((1, 1, 2 * gap, height - 2), 90, 270, fill="black", width=stroke)
            draw.arc((width - 2 * gap - 1, 1, width - 2, height - 2), -90, 90, fill="black", width=stroke)
        elif env == 'cases':
            brace, _ = image_of(r'\{')
            brace = brace.resize((gap, height - 2), Image.Resampling.LANCZOS)
            canvas.alpha_composite(brace, (0, 1))
        elif env in {"bmatrix", "vmatrix"}:
            for x in (gap // 2, width - gap // 2 - 1):
                draw.line((x, 1, x, height - 2), fill="black", width=stroke)
                if env == "bmatrix":
                    tip = gap if x < width / 2 else width - gap - 1
                    draw.line((x, 1, tip, 1), fill="black", width=stroke)
                    draw.line((x, height - 2, tip, height - 2), fill="black", width=stroke)
        pieces.append((canvas, round(height / 2 - font_size * scale * 0.25)))
    ascent = max(img.height - depth for img, depth in pieces)
    descent = max(depth for _, depth in pieces)
    canvas = _bounded_canvas(sum(img.width for img, _ in pieces) + 4 * (len(pieces) - 1), ascent + descent)
    x = 0
    for img, depth in pieces:
        canvas.alpha_composite(img, (x, ascent - img.height + depth))
        x += img.width + 4
    output = BytesIO()
    canvas.save(output, format="PNG")
    data = output.getvalue()
    if len(data) > MAX_PNG_BYTES:
        raise ValueError("打印公式图片过大，请拆分后重试")
    return data, canvas.width / scale, canvas.height / scale, descent / scale


class MathParagraph(Paragraph):
    def draw(self):
        # ReportLab defaults the first baseline to fontSize below the top even
        # when an inline image has a much larger ascent (e.g. a matrix).
        # Correct locally, without changing its process-global layout option.
        from reportlab.platypus.paragraph import paraFontSizeHeightOffset
        first = self.blPara.lines[0] if self.blPara.kind == 1 and self.blPara.lines else self.blPara
        shift = max(0, getattr(first, "ascent", 0) - getattr(first, "fontSize", self.style.fontSize)) if paraFontSizeHeightOffset else 0
        self.canv.saveState()
        try:
            self.canv.translate(0, -shift)
            super().draw()
        finally:
            self.canv.restoreState()

    def wrap(self, availWidth, availHeight):
        for fragment in self.frags:
            callback = getattr(fragment, "cbDefn", None)
            if callback is not None and getattr(callback, "kind", "") == "img":
                if float(callback.width) > availWidth:
                    raise ValueError("公式宽度超过当前栏位，请选择单栏排版或拆分公式")
                # ReportLab's CJK splitter calls ord() on each fragment when
                # wrapping. An image normally has empty text, which crashes at
                # a line boundary. U+FFFC represents one indivisible inline
                # object; cbDefn still supplies its width and draws the image,
                # not this placeholder. Do not patch ReportLab globally.
                if not fragment.text:
                    fragment.text = "\ufffc"
        return super().wrap(availWidth, availHeight)


def pdf_math_paragraph(value: str, style: ParagraphStyle) -> Paragraph:
    source = str(value or "")
    matches = list(_MATH.finditer(source))
    if not matches:
        if re.search(r"(?<!\\)\$|\\[\[(]", source) and re.search(r"\\[A-Za-z]+", source):
            raise ValueError("公式分隔符不完整，请用成对的$或数学括号包裹公式后重试")
        return Paragraph(html.escape(source).replace("\n", "<br/>") or " ", style)
    if len(matches) > MAX_FORMULAS_PER_PARAGRAPH:
        raise ValueError("单段公式超过64个，请拆分题目后重试")
    # A valid formula earlier in the paragraph must not hide an unclosed later
    # one. Check only unmatched text, preserving ordinary currency without TeX.
    unmatched = _MATH.sub("", source)
    if re.search(r"(?<!\\)\$|\\[\[(]", unmatched) and re.search(r"\\[A-Za-z]+", unmatched):
        raise ValueError("公式分隔符不完整，请核对所有公式后重试")
    parts: list[str] = []
    position = 0
    for match in matches:
        parts.append(html.escape(source[position:match.start()]).replace("\n", "<br/>"))
        expression = next(group for group in match.groups() if group is not None)
        png, width, height, depth = _raster_formula(expression, float(style.fontSize))
        budget = _BUDGET.get()
        if budget is not None:
            budget["count"] += 1
            budget["bytes"] += len(png)
            if budget["count"] > MAX_DOCUMENT_FORMULAS or budget["bytes"] > MAX_DOCUMENT_PNG_BYTES:
                raise ValueError("本次打印公式数量或图片总量过大，请分批组卷")
        uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        image = f'<img src="{uri}" width="{width:.3f}" height="{height:.3f}" valign="{-depth:.3f}"/>'
        # Preserve display-vs-inline intent, without injecting user markup.
        parts.append("<br/>" + image + "<br/>" if match.group(1) is not None or match.group(4) is not None else image)
        position = match.end()
    parts.append(html.escape(source[position:]).replace("\n", "<br/>"))
    math_style = ParagraphStyle(style.name + "Math", parent=style, autoLeading="max")
    return MathParagraph("".join(parts) or " ", math_style)
