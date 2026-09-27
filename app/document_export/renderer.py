from __future__ import annotations

import base64
import html
import ipaddress
import os
import re
import socket
import urllib.request
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from PIL import Image as PillowImage
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, A5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    Image,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    LongTable,
    TableStyle,
    KeepTogether,
    Table,
)

from .models import (
    ClozeExportRequest,
    ExportGroup,
    ExportImage,
    ExportQuestion,
    ExportRequest,
)
from .math_pdf import bounded_math_document, pdf_math_paragraph
from .math_docx import append_math_text


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 40 * 1024 * 1024
MAX_OUTPUT_BYTES = 12 * 1024 * 1024


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n]+", "-", value).strip(" .-")
    return cleaned[:70] or "错题集"


def _public_host(hostname: str) -> bool:
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def _download_image(image: ExportImage) -> bytes:
    parsed = urlparse(image.url)
    if parsed.scheme != "https" or not parsed.hostname or not _public_host(parsed.hostname):
        raise ValueError("题目图片地址不是可访问的 HTTPS 云存储地址")
    request = urllib.request.Request(image.url, headers={"User-Agent": "YantongDocumentExport/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        content_type = str(response.headers.get("Content-Type", ""))
        if content_type and not content_type.lower().startswith("image/"):
            raise ValueError("云存储地址返回的不是图片")
        length = int(response.headers.get("Content-Length") or 0)
        if length > MAX_IMAGE_BYTES:
            raise ValueError("单张题目图片超过 8MB")
        data = response.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("单张题目图片超过 8MB 或内容为空")
    with PillowImage.open(BytesIO(data)) as source:
        source.verify()
    return data


class ImageCache:
    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}
        self._total = 0

    def get(self, image: ExportImage) -> bytes:
        if image.url not in self._items:
            data = _download_image(image)
            self._total += len(data)
            if self._total > MAX_TOTAL_IMAGE_BYTES:
                raise ValueError("本次导出的图片总量超过 40MB，请拆分错题篮后重试")
            self._items[image.url] = data
        return self._items[image.url]


def _document_modes(request: ExportRequest) -> list[str]:
    if not request.settings.includeAnswers:
        return ["questions"]
    return ["questions", "solutions"] if request.settings.packageMode == "separate" else ["combined"]


def _suffix(mode: str) -> str:
    return {"questions": "-试题", "solutions": "-答案解析", "combined": ""}[mode]


def _question_label(group: ExportGroup, question: ExportQuestion, index: int) -> str:
    return question.number or (str(group.order) if index == 0 else f"{group.order}.{index + 1}")


def _pdf_paragraph(value: str, style: ParagraphStyle) -> Paragraph:
    return pdf_math_paragraph(value, style)


def _pdf_image(data: bytes, max_width: float) -> Image:
    with PillowImage.open(BytesIO(data)) as source:
        width, height = source.size
    ratio = min(1.0, max_width / max(1, width))
    return Image(BytesIO(data), width=width * ratio, height=height * ratio)


@bounded_math_document
def _build_pdf(request: ExportRequest, mode: str, cache: ImageCache) -> bytes:
    output = BytesIO()
    has_ai_content = any(q.aiGenerated for g in request.groups for q in g.questions)
    page_size = A5 if request.settings.paper == "A5" else A4
    margin = request.settings.marginMm * mm
    document = BaseDocTemplate(
        output,
        pagesize=page_size,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title=request.title,
        subject="AI 生成内容" if has_ai_content else "用户组卷",
    )
    column_gap = 7 * mm
    column_count = request.settings.columns
    frame_width = (
        document.width
        if column_count == 1
        else (document.width - column_gap) / 2
    )
    frames = [
        Frame(
            margin + index * (frame_width + column_gap),
            margin,
            frame_width,
            document.height,
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        for index in range(column_count)
    ]
    def page_footer(canvas, doc):
        canvas.saveState()
        if has_ai_content:
            canvas.setFont("STSong-Light", 10)
            canvas.drawString(margin, margin * .55, "含 AI 生成内容 · 请核对")
        canvas.setFont("STSong-Light", 9)
        canvas.drawRightString(page_size[0] - margin, margin * .55, f"第 {doc.page} 页")
        canvas.restoreState()
    document.addPageTemplates(PageTemplate(id="content", frames=frames, onPage=page_footer))
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    except KeyError:
        pass
    font = "STSong-Light"
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "YantongBody",
        parent=styles["BodyText"],
        fontName=font,
        fontSize=request.settings.fontSize,
        leading=request.settings.fontSize * request.settings.lineSpacing,
        spaceAfter=4,
        wordWrap="CJK",
    )
    title_style = ParagraphStyle(
        "YantongTitle",
        parent=body,
        fontSize=request.settings.fontSize + 7,
        leading=request.settings.fontSize + 12,
        alignment=TA_CENTER,
        spaceAfter=10,
    )
    meta_style = ParagraphStyle(
        "YantongMeta",
        parent=body,
        fontSize=max(8, request.settings.fontSize - 2),
        textColor="#66736d",
    )
    group_heading_style = ParagraphStyle("YantongGroupHeading", parent=body, keepWithNext=True)
    group_meta_style = ParagraphStyle("YantongGroupMeta", parent=meta_style, keepWithNext=True)
    story = [_pdf_paragraph(request.title + _suffix(mode), title_style)]
    story.append(_pdf_paragraph(f"共 {len(request.groups)} 个题组", meta_style))
    story.append(Spacer(1, 6))

    def add_solution(group: ExportGroup, question: ExportQuestion, index: int) -> None:
        label = _question_label(group, question, index)
        story.append(_pdf_paragraph(f"{label}. 答案：{question.answer or '未录入'}", body))
        if request.settings.includeAnalysis and question.analysis:
            story.append(_pdf_paragraph(f"解析：{question.analysis}", body))

    if mode in {"questions", "combined"}:
        for group in request.groups:
            story.append(_pdf_paragraph(f"{group.order}. {group.title}", group_heading_style))
            if request.settings.includeMeta and group.meta:
                story.append(_pdf_paragraph(group.meta, group_meta_style))
            for paragraph in group.materialParagraphs:
                story.append(_pdf_paragraph(paragraph, body))
            for index, question in enumerate(group.questions):
                label = _question_label(group, question, index)
                block = [_pdf_paragraph(f"{label}. {question.stem}", body)]
                options = [_pdf_paragraph(option, body) for option in question.options]
                if request.settings.optionColumns == 2 and options:
                    rows = [options[i:i+2] for i in range(0, len(options), 2)]
                    if len(rows[-1]) == 1: rows[-1].append("")
                    table = Table(rows, colWidths=[frame_width / 2] * 2, hAlign="LEFT")
                    table.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 0), ("RIGHTPADDING", (0,0), (-1,-1), 6)]))
                    block.append(table)
                else:
                    block.extend(options)
                for _ in range(question.blankLines if question.blankLines is not None else request.settings.blankLines):
                    block.append(Spacer(1, request.settings.fontSize * 1.25))
                    block.append(HRFlowable(width="100%", thickness=0.35, color="#d9dfdc"))
                story.extend([KeepTogether(block)] if request.settings.avoidSplit else block)
                if request.settings.includeAnswers and request.settings.packageMode == "combined" and request.settings.solutionMode == "inline":
                    add_solution(group, question, index)
                story.append(Spacer(1, request.settings.questionSpacing))
    if mode == "solutions" or (
        mode == "combined" and request.settings.includeAnswers and request.settings.solutionMode == "end"
    ):
        if mode == "combined":
            story.append(PageBreak())
            story.append(_pdf_paragraph("答案与解析", title_style))
        for group in request.groups:
            story.append(_pdf_paragraph(f"{group.order}. {group.title}", group_heading_style))
            for index, question in enumerate(group.questions):
                add_solution(group, question, index)
            story.append(Spacer(1, 6))
    document.build(story)
    data = output.getvalue()
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("生成的 PDF 超过 12MB，请减少题目数量后重试")
    return data


def _docx_font(run, size: int, bold: bool = False) -> None:
    run.font.name = "Microsoft YaHei"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    run.font.size = Pt(size)
    run.bold = bold


def _docx_text(document: Document, value: str, size: int, bold: bool = False):
    paragraph = document.add_paragraph()
    append_math_text(paragraph, value, lambda run: _docx_font(run, size, bold))
    return paragraph


@bounded_math_document
def _build_docx(request: ExportRequest, mode: str, cache: ImageCache) -> bytes:
    document = Document()
    has_ai_content = any(q.aiGenerated for g in request.groups for q in g.questions)
    section = document.sections[0]
    if request.settings.paper == "A5":
        section.page_width, section.page_height = Mm(148), Mm(210)
    else:
        section.page_width, section.page_height = Mm(210), Mm(297)
    section.top_margin = section.bottom_margin = Mm(request.settings.marginMm)
    section.left_margin = section.right_margin = Mm(request.settings.marginMm)
    document.styles['Normal'].paragraph_format.line_spacing = request.settings.lineSpacing
    document.styles['Normal'].paragraph_format.space_after = Pt(4)
    document.core_properties.title = request.title
    if has_ai_content:
        document.core_properties.subject = 'AI 生成内容'
        document.core_properties.comments = '包含 AI 生成讲解，请核对。服务提供者：四川省成都市无限回响智创科技有限公司'
    columns = section._sectPr.xpath("./w:cols")[0]
    columns.set(qn("w:num"), str(request.settings.columns))
    columns.set(qn("w:space"), "360")
    footer = section.footer.paragraphs[0]
    section.footer_distance = Mm(request.settings.marginMm * .4)
    footer.paragraph_format.space_after = Pt(0)
    footer.paragraph_format.line_spacing = 1
    if has_ai_content:
        footer.alignment = WD_ALIGN_PARAGRAPH.LEFT
        footer.paragraph_format.tab_stops.add_tab_stop(
            section.page_width - section.left_margin - section.right_margin,
            WD_TAB_ALIGNMENT.RIGHT,
        )
        _docx_font(footer.add_run("含 AI 生成内容 · 请核对\t"), 10)
    else:
        footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _docx_font(footer.add_run("第 "), 9)
    page_field = OxmlElement('w:fldSimple')
    page_field.set(qn('w:instr'), 'PAGE')
    footer._p.append(page_field)
    _docx_font(footer.add_run(" 页"), 9)
    title = document.add_paragraph(style='Title')
    title.alignment = 1
    document.styles['Title'].font.color.rgb = RGBColor(0, 0, 0)
    _docx_font(title.add_run(request.title + _suffix(mode)), request.settings.fontSize + 7, True)
    _docx_text(document, f"共 {len(request.groups)} 个题组", max(8, request.settings.fontSize - 2))

    def add_picture(image: ExportImage) -> None:
        data = cache.get(image)
        max_width = 112 if request.settings.paper == "A5" else 170
        document.add_picture(BytesIO(data), width=Mm(max_width))

    def add_solution(group: ExportGroup, question: ExportQuestion, index: int) -> None:
        label = _question_label(group, question, index)
        _docx_text(document, f"{label}. 答案：{question.answer or '未录入'}", request.settings.fontSize)
        if request.settings.includeAnalysis and question.analysis:
            _docx_text(document, f"解析：{question.analysis}", request.settings.fontSize)

    if mode in {"questions", "combined"}:
        for group in request.groups:
            heading = _docx_text(document, f"{group.order}. {group.title}", request.settings.fontSize, True)
            heading.paragraph_format.keep_with_next = True
            if request.settings.includeMeta and group.meta:
                meta = _docx_text(document, group.meta, max(8, request.settings.fontSize - 2))
                meta.paragraph_format.keep_with_next = True
            for paragraph in group.materialParagraphs:
                _docx_text(document, paragraph, request.settings.fontSize)
            for index, question in enumerate(group.questions):
                label = _question_label(group, question, index)
                stem = _docx_text(document, f"{label}. {question.stem}", request.settings.fontSize)
                stem.paragraph_format.keep_with_next = bool(question.options) and request.settings.avoidSplit
                stem.paragraph_format.widow_control = True
                if request.settings.optionColumns == 2 and question.options:
                    table = document.add_table(rows=(len(question.options)+1)//2, cols=2)
                    table.autofit = False
                    column_width = (section.page_width - section.left_margin - section.right_margin
                                    - Mm(6.35) * (request.settings.columns-1)) / request.settings.columns
                    for column in table.columns:
                        column.width = int(column_width/2)
                    for i, option in enumerate(question.options):
                        cell = table.cell(i//2, i%2)
                        cell.width = int(column_width/2)
                        append_math_text(cell.paragraphs[0], option, lambda run: _docx_font(run, request.settings.fontSize))
                        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
                else:
                    for i, option in enumerate(question.options):
                        paragraph = _docx_text(document, option, request.settings.fontSize)
                        paragraph.paragraph_format.keep_with_next = request.settings.avoidSplit and i < len(question.options)-1
                for _ in range(question.blankLines if question.blankLines is not None else request.settings.blankLines):
                    blank = _docx_text(document, "", request.settings.fontSize)
                    blank.paragraph_format.space_after = Pt(request.settings.fontSize * 1.25)
                if request.settings.includeAnswers and request.settings.packageMode == "combined" and request.settings.solutionMode == "inline":
                    add_solution(group, question, index)
                document.paragraphs[-1].paragraph_format.space_after = Pt(request.settings.questionSpacing)
    if mode == "solutions" or (
        mode == "combined" and request.settings.includeAnswers and request.settings.solutionMode == "end"
    ):
        if mode == "combined":
            document.add_section(WD_SECTION.NEW_PAGE)
            _docx_text(document, "答案与解析", request.settings.fontSize + 5, True)
        for group in request.groups:
            heading = _docx_text(document, f"{group.order}. {group.title}", request.settings.fontSize, True)
            heading.paragraph_format.keep_with_next = True
            for index, question in enumerate(group.questions):
                add_solution(group, question, index)
    output = BytesIO()
    document.save(output)
    data = output.getvalue()
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("生成的 Word 超过 12MB，请减少题目数量后重试")
    return data


def render_export(request: ExportRequest) -> list[dict[str, object]]:
    cache = ImageCache()
    extension = request.format
    mime_type = (
        "application/pdf"
        if extension == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    files = []
    for mode in _document_modes(request):
        data = (
            _build_pdf(request, mode, cache)
            if extension == "pdf"
            else _build_docx(request, mode, cache)
        )
        files.append(
            {
                "name": f"{_safe_name(request.title)}{_suffix(mode)}.{extension}",
                "mimeType": mime_type,
                "size": len(data),
                "contentBase64": base64.b64encode(data).decode("ascii"),
            }
        )
    return files


def _docx_set_cell_shading(cell, color: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shading = tc_pr.makeelement(qn("w:shd"))
    shading.set(qn("w:fill"), color)
    tc_pr.append(shading)


def _set_repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)


def _set_table_geometry(table, widths_dxa: list[int], indent_dxa: int = 120) -> None:
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    total = sum(widths_dxa)
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.first_child_found_in("w:tblBorders")
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.append(borders)
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = borders.find(qn(f"w:{side}"))
        if border is None:
            border = OxmlElement(f"w:{side}")
            borders.append(border)
        border.set(qn("w:val"), "single")
        border.set(qn("w:sz"), "4")
        border.set(qn("w:color"), "D9D9D9")
    tbl_width = tbl_pr.first_child_found_in("w:tblW")
    if tbl_width is None:
        tbl_width = OxmlElement("w:tblW")
        tbl_pr.append(tbl_width)
    tbl_width.set(qn("w:type"), "dxa")
    tbl_width.set(qn("w:w"), str(total))
    indent = tbl_pr.first_child_found_in("w:tblInd")
    if indent is None:
        indent = OxmlElement("w:tblInd")
        tbl_pr.append(indent)
    indent.set(qn("w:type"), "dxa")
    indent.set(qn("w:w"), str(indent_dxa))
    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths_dxa:
        column = OxmlElement("w:gridCol")
        column.set(qn("w:w"), str(width))
        grid.append(column)
    for row in table.rows:
        # A short answer row should stay intact, but its height is not fixed.
        cannot_split = OxmlElement("w:cantSplit")
        row._tr.get_or_add_trPr().append(cannot_split)
        for index, cell in enumerate(row.cells):
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_width = tc_pr.first_child_found_in("w:tcW")
            if tc_width is None:
                tc_width = OxmlElement("w:tcW")
                tc_pr.append(tc_width)
            tc_width.set(qn("w:type"), "dxa")
            tc_width.set(qn("w:w"), str(widths_dxa[index]))
            margins = tc_pr.first_child_found_in("w:tcMar")
            if margins is None:
                margins = OxmlElement("w:tcMar")
                tc_pr.append(margins)
            for side, value in (("top", 80), ("bottom", 80), ("start", 120), ("end", 120)):
                node = margins.find(qn(f"w:{side}"))
                if node is None:
                    node = OxmlElement(f"w:{side}")
                    margins.append(node)
                node.set(qn("w:w"), str(value))
                node.set(qn("w:type"), "dxa")


def _add_page_number(section, variant: str = "combined") -> None:
    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    label = {"practice": "填空练习版", "answers": "填空答案版", "combined": "填空练习与答案"}[variant]

    def add_text(value):
        run = paragraph.add_run(value)
        _docx_font(run, 9)
        run.font.color.rgb = RGBColor(80, 80, 80)

    def add_field(instruction_text):
        run = paragraph.add_run()
        _docx_font(run, 9)
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        instruction = OxmlElement("w:instrText")
        instruction.set(qn("xml:space"), "preserve")
        instruction.text = f" {instruction_text} "
        separate = OxmlElement("w:fldChar")
        separate.set(qn("w:fldCharType"), "separate")
        cached = OxmlElement("w:t")
        cached.text = "1"
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        run._r.extend([begin, instruction, separate, cached, end])

    add_text(f"{label}    第 ")
    add_field("PAGE")
    add_text(" 页 / 共 ")
    add_field("NUMPAGES")
    add_text(" 页")


def _clozed_text(block, answer_number_start: int) -> tuple[list[tuple[str, bool]], list[dict]]:
    blanks = sorted(
        [item for item in block.blanks if item.enabled], key=lambda item: (item.start, item.end)
    )
    cursor = 0
    runs: list[tuple[str, bool]] = []
    answers: list[dict] = []
    # JavaScript selections are UTF-16 offsets, not Python code-point indexes.
    # Mapping only valid boundaries also rejects half-surrogate selections.
    boundaries = {0: 0}
    units = 0
    for index, char in enumerate(block.text):
        units += 2 if ord(char) > 0xFFFF else 1
        boundaries[units] = index + 1
    for offset, blank in enumerate(blanks):
        start = boundaries.get(blank.start)
        end = boundaries.get(blank.end)
        if start is None or end is None or end <= start or start < cursor or block.text[start:end] != blank.answer:
            raise ValueError("挖空位置与段落文字不一致，请重新核对后导出")
        if start > cursor:
            runs.append((block.text[cursor:start], False))
        number = answer_number_start + offset
        # Length gives students usable handwriting space without embedding the
        # answer in a field, comment, hidden run, alt text, or document property.
        underline = "_" * max(8, min(24, len(blank.answer) * 2))
        runs.append((f"〔{number}〕{underline}", True))
        answers.append(
            {
                "number": number,
                "answer": blank.answer,
                "context": block.text[max(0, start - 18):min(len(block.text), end + 18)],
                "page": block.page,
                "block_order": block.order,
            }
        )
        cursor = end
    if cursor < len(block.text):
        runs.append((block.text[cursor:], False))
    return runs, answers


@bounded_math_document
def render_cloze_export(request: ClozeExportRequest) -> list[dict[str, object]]:
    # One stable numbering plan is shared by all variants. Answers never enter
    # the practice document's OOXML, including its properties and hidden parts.
    blocks = sorted(enumerate(request.blocks), key=lambda pair: (pair[1].order, pair[0]))
    prepared = []
    answers: list[dict] = []
    for _, block in blocks:
        runs, current_answers = _clozed_text(block, len(answers) + 1)
        answers.extend(current_answers)
        prepared.append((block, runs, current_answers))
    if not answers:
        raise ValueError("请至少保留一处挖空后再生成练习")
    variant = request.variant
    if request.format == "pdf":
        return [_cloze_output_file(request, _build_cloze_pdf(request, prepared, answers))]
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.top_margin = section.bottom_margin = Mm(20)
    section.left_margin = section.right_margin = Mm(20)
    section.footer_distance = Mm(10)
    style = request.wordStyle
    family = style.fontFamily if style else "SimSun"
    size = style.fontSize if style else 12
    show_title = style.showTitle if style else True
    east_asia = {"SimSun": "宋体", "SimHei": "黑体", "KaiTi": "楷体", "Microsoft YaHei": "微软雅黑"}[family]

    def font(run, point_size=size, bold=False):
        run.font.name = family
        fonts = run._element.get_or_add_rPr().get_or_add_rFonts()
        for key in ("ascii", "hAnsi", "cs"):
            fonts.set(qn("w:" + key), family)
        fonts.set(qn("w:eastAsia"), east_asia)
        run.font.size = Pt(point_size)
        run.font.color.rgb = RGBColor(0, 0, 0)
        run.bold = bold

    def paragraph(value, bold=False, point_size=size, style_name=None):
        result = document.add_paragraph(style=style_name)
        append_math_text(result, value, lambda run: font(run, point_size, bold))
        return result

    document.core_properties.title = request.title
    document.core_properties.subject = request.courseName
    document.core_properties.author = ""
    document.core_properties.last_modified_by = ""
    document.core_properties.comments = ""
    document.core_properties.keywords = ""
    for name in ("Normal", "Title", "Heading 1"):
        preset = document.styles[name]
        preset.font.name = family
        preset._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), east_asia)
        preset.font.size = Pt(size)
        preset.font.color.rgb = RGBColor(0, 0, 0)
        preset.font.underline = False
        for border in preset.element.xpath("./w:pPr/w:pBdr"):
            border.getparent().remove(border)
    normal = document.styles["Normal"].paragraph_format
    normal.space_before = Pt(0)
    normal.space_after = Pt(8)
    normal.line_spacing = 1.5
    normal.widow_control = True

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.paragraph_format.space_after = Pt(0)
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)

    if show_title:
        title = paragraph(request.title + (" 答案" if variant == "answers" else ""), True, size + 4, "Title")
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.paragraph_format.space_after = Pt(14)
        title.paragraph_format.keep_with_next = True

    if variant in {"practice", "combined"}:
        for block, runs, current_answers in prepared:
            # Keep source paragraphs and math, without prefixed paragraph labels,
            # instructions, identity fields, decorative tables or writing lines.
            paragraph("".join(value for value, _ in runs))

    if variant in {"answers", "combined"}:
        if variant == "combined":
            heading = paragraph("答案", True, size + 2, "Heading 1")
            heading.paragraph_format.page_break_before = True
            heading.paragraph_format.keep_with_next = True
        for answer in answers:
            paragraph(f"〔{answer['number']}〕{answer['answer']}")

    output = BytesIO()
    document.save(output)
    data = output.getvalue()
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("生成的 Word 超过 12MB，请减少资料内容后重试")
    return [_cloze_output_file(request, data)]


def _cloze_output_file(request, data: bytes) -> dict[str, object]:
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("生成的文档超过 12MB，请减少资料内容后重试")
    label = {"practice": "练习版", "answers": "答案版", "combined": "练习与答案合集"}[request.variant]
    return {
        "name": f"{_safe_name(request.title)}-{label}.{request.format}",
        "variant": request.variant,
        "format": request.format,
        "mimeType": "application/pdf" if request.format == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size": len(data),
        "contentBase64": base64.b64encode(data).decode("ascii"),
    }


@bounded_math_document
def _build_cloze_pdf(request, prepared, answers) -> bytes:
    # PDF is generated directly from the same verified blank plan. It is not a
    # claimed Word rendering, and needs no installed desktop office software.
    output = BytesIO()
    font = "STSong-Light"
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    pdfmetrics.registerFontFamily(font, normal=font, bold=font, italic=font, boldItalic=font)
    margin = 18 * mm
    page_width, page_height = A4
    width = page_width - 2 * margin
    doc = BaseDocTemplate(output, pagesize=A4, leftMargin=margin, rightMargin=margin,
                          topMargin=16 * mm, bottomMargin=19 * mm, title=request.title, author="研通")
    frame = Frame(margin, doc.bottomMargin, width, page_height - doc.topMargin - doc.bottomMargin,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id="cloze", frames=[frame]))
    body = ParagraphStyle("ClozeBody", fontName=font, fontSize=11, leading=18,
                          spaceAfter=7, wordWrap="CJK", textColor="#000000", allowWidows=0, allowOrphans=0)
    small = ParagraphStyle("ClozeSmall", parent=body, fontSize=10, leading=15, spaceAfter=7)
    title = ParagraphStyle("ClozeTitle", parent=body, fontSize=18, leading=24, alignment=TA_CENTER, spaceAfter=5)
    subtitle = ParagraphStyle("ClozeSubtitle", parent=body, fontSize=11, leading=17, alignment=TA_CENTER, spaceAfter=12)
    heading = ParagraphStyle("ClozeHeading", parent=body, fontSize=16, leading=22, spaceBefore=0, spaceAfter=8, keepWithNext=True)
    cell_style = ParagraphStyle("ClozeCell", parent=body, fontSize=10, leading=15, spaceAfter=0)
    number_style = ParagraphStyle("ClozeNumber", parent=cell_style, alignment=TA_CENTER)
    variant = request.variant
    label = {"practice": "练习版", "answers": "答案版", "combined": "练习与答案合集"}[variant]
    clean_title = re.sub(r"[\W_]+", " ", request.title, flags=re.UNICODE).strip() or "专业课填空练习"
    clean_course = re.sub(r"[\W_]+", " ", request.courseName, flags=re.UNICODE).strip()
    story = [_pdf_paragraph(clean_title, title), _pdf_paragraph(f"{clean_course}  {label}", subtitle)]
    story.append(_pdf_paragraph(f"共 {len(prepared)} 段，{len(answers)} 处填空。题号在练习版、答案版和合集中保持一致。", small))
    if variant in {"practice", "combined"}:
        story.append(_pdf_paragraph("请按原文顺序填写空缺内容，再核对答案。空格下方可记录补充关键词。", small))
        story.append(_pdf_paragraph("姓名：____________________    日期：____________________", small))
        story.append(Spacer(1, 5 * mm))
        for block, runs, current_answers in prepared:
            block_flow = [_pdf_paragraph(f"第 {block.order} 段  " + "".join(value for value, _ in runs), body)]
            if current_answers:
                for index in range(max(1, min(3, (len(current_answers) + 3) // 4))):
                    block_flow.append(_pdf_paragraph(("补充：" if index == 0 else "　　　") + "＿" * 31, small))
            block_flow.append(Spacer(1, 3 * mm))
            height = sum(item.wrap(width, doc.height)[1] + item.getSpaceBefore() + item.getSpaceAfter() for item in block_flow)
            # Keep a normal paragraph with its handwriting line. Very long
            # source paragraphs must still split rather than exceed a page.
            if height <= doc.height * 0.75:
                story.append(KeepTogether(block_flow))
            else:
                story.extend(block_flow)
    if variant in {"answers", "combined"}:
        if variant == "combined":
            story.append(PageBreak())
        story.append(_pdf_paragraph("答案与核对", heading))
        story.append(_pdf_paragraph("请按填空编号核对。原文定位保留所选词及前后文字，便于复习。", small))
        rows = [[_pdf_paragraph(value, cell_style) for value in ("填空编号", "答案", "原文定位")]]
        for answer in answers:
            rows.append([
                _pdf_paragraph(f"〔{answer['number']}〕\n第 {answer['block_order']} 段", number_style),
                _pdf_paragraph(answer["answer"], cell_style),
                _pdf_paragraph(answer["context"], cell_style),
            ])
        table = LongTable(rows, colWidths=[20 * mm, 35 * mm, width - 55 * mm], repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, "#D9D9D9"),
            ("BACKGROUND", (0, 0), (-1, 0), "#E8EEF8"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(table)

    class NumberedCanvas(pdf_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.page_states = []

        def showPage(self):
            self.page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            count = len(self.page_states)
            for page_state in self.page_states:
                self.__dict__.update(page_state)
                self.setFont(font, 9)
                self.setFillColor("#505050")
                self.drawRightString(page_width - margin, 10 * mm, f"{label}    第 {self._pageNumber} 页 / 共 {count} 页")
                pdf_canvas.Canvas.showPage(self)
            pdf_canvas.Canvas.save(self)

    doc.build(story, canvasmaker=NumberedCanvas)
    return output.getvalue()
