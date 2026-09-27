"""Bounded DOCX text import. Never execute Office, macros or external links."""
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from lxml import etree

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
M = '{http://schemas.openxmlformats.org/officeDocument/2006/math}'
MAX_XML = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024


def read_word_paragraphs(source: Path) -> list[str]:
    if source.suffix.lower() != '.docx':
        raise ValueError('请将旧版 .doc 另存为 .docx 或 PDF 后上传')
    if source.stat().st_size > 50 * 1024 * 1024:
        raise ValueError('Word 文件不能超过 50MB')
    try:
        with ZipFile(source) as archive:
            items = archive.infolist()
            names = [item.filename for item in items]
            if len(items) > 2048 or len(set(names)) != len(names) or sum(x.file_size for x in items) > MAX_EXPANDED:
                raise ValueError('Word 压缩包过大或结构异常，请拆分资料')
            if any(x.flag_bits & 1 or x.file_size > MAX_EXPANDED for x in items):
                raise ValueError('不支持加密或超大 Word 文件')
            if any('vbaproject' in name.lower() or name.startswith('word/embeddings/') for name in names):
                raise ValueError('Word 包含宏或嵌入对象，请转成 PDF 后上传')
            if 'word/document.xml' not in names or '[Content_Types].xml' not in names:
                raise ValueError('这不是有效的 Word .docx 文件')
            item = archive.getinfo('word/document.xml')
            if item.file_size > MAX_XML:
                raise ValueError('Word 正文结构过大，请拆分资料')
            xml = archive.read(item)
    except BadZipFile as error:
        raise ValueError('Word 文件损坏、已加密或格式不正确，请另存为 .docx') from error
    if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
        raise ValueError('Word 包含不支持的 XML 声明')
    try:
        root = etree.fromstring(xml, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False))
    except etree.XMLSyntaxError as error:
        raise ValueError('Word 正文格式损坏，请重新另存为 .docx') from error
    body = root.find(W + 'body')
    if body is None:
        raise ValueError('Word 缺少正文')
    # Never silently drop diagrams/equations or flatten structured math into
    # misleading strings. The PDF path preserves their visual representation.
    unsupported = {'drawing', 'pict', 'object', 'altChunk', 'txbxContent', 'footnoteReference', 'endnoteReference'}
    if any(node.tag in {W + name for name in unsupported} or node.tag in {M + 'oMath', M + 'oMathPara'} for node in body.iter()):
        raise ValueError('这份 Word 含图片、公式、文本框或脚注，请另存为 PDF 上传，避免遗漏内容')
    paragraphs = []
    total = 0
    for paragraph in body.iter(W + 'p'):
        parts = []
        for node in paragraph.iter():
            if any(parent.tag == W + 'del' for parent in node.iterancestors()):
                continue
            if node.tag == W + 't':
                parts.append(node.text or '')
            elif node.tag == W + 'tab':
                parts.append('\t')
            elif node.tag in {W + 'br', W + 'cr'}:
                parts.append('\n')
        text = ''.join(parts).strip()
        if text:
            total += len(text)
            if total > 240000:
                raise ValueError('资料文字超过 24 万字，请拆分文件后重试')
            paragraphs.append(text)
    if not paragraphs:
        raise ValueError('Word 中没有可读取正文，请转成 PDF 或上传清晰图片')
    return paragraphs


def process_study_word(source: Path, progress_callback=None) -> list[dict]:
    from .study_material_pipeline import _blocks_from_pages
    paragraphs = read_word_paragraphs(source)
    # DOCX does not provide reliable rendered page numbers. page=1 is a logical
    # import segment, not a claim about original Word pagination.
    blocks = _blocks_from_pages([(1, '\n\n'.join(paragraphs), 1.0, 'word_text')])
    if progress_callback:
        progress_callback(1, 1, 'Word 正文和表格读取完成')
    return blocks
