from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable, Sequence

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image

from .pdf_pipeline import RapidOcrMarkerDetector


MAX_BLOCKS = 200
MAX_BLOCK_CHARS = 1200
MAX_TOTAL_CHARS = 240000
_SPACE = re.compile(r"[ \t\u3000]+")
_HEADING = re.compile(r"^(?:第[一二三四五六七八九十百零\d]+[章节部分]|[一二三四五六七八九十]+[、.]|\d+[.)、])")
_SENTENCE_END = re.compile(r"[。！？!?；;：:]$")
_STOP_CANDIDATES = {
    "这种方法", "这个问题", "主要内容", "基本概念", "有关规定", "具体情况",
    "上述内容", "下列内容", "一般来说", "因此", "所以", "其中", "它们",
}


def _clean_line(value: object) -> str:
    line = _SPACE.sub(" ", str(value or "")).strip()
    return re.sub(r"\s+([，。；：！？、,.!?;:])", r"\1", line)


def split_paragraphs(value: str) -> list[str]:
    """Convert page OCR/text lines into compact, editable paragraphs."""
    lines = [_clean_line(line) for line in str(value or "").replace("\r", "\n").split("\n")]
    paragraphs: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            paragraphs.append(current[:MAX_BLOCK_CHARS])
            overflow = current[MAX_BLOCK_CHARS:]
            while overflow:
                paragraphs.append(overflow[:MAX_BLOCK_CHARS])
                overflow = overflow[MAX_BLOCK_CHARS:]
        current = ""

    for line in lines:
        if not line:
            flush()
            continue
        if current and (_HEADING.match(line) or _SENTENCE_END.search(current)):
            flush()
        if not current:
            current = line
        elif current.endswith(("-", "—")):
            current = current[:-1] + line
        elif re.search(r"[A-Za-z0-9)]$", current) and re.match(r"^[A-Za-z0-9(]", line):
            current += " " + line
        else:
            current += line
        if len(current) >= MAX_BLOCK_CHARS:
            flush()
    flush()
    return [item for item in paragraphs if re.search(r"[\w\u4e00-\u9fff]", item)]


def _candidate_key(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _trim_candidate(value: str) -> str:
    value = str(value or "").strip(" ，。；：、,.!?！？()（）[]【】\"'“”")
    value = re.sub(r"^(?:所谓|其中|一般|通常|主要|基本)", "", value).strip()
    return value


def suggest_blanks(text: str, block_id: str) -> list[dict[str, object]]:
    """Offer conservative, explainable blanks for later human confirmation."""
    proposals: list[tuple[int, int, str, float, str]] = []

    def add(start: int, end: int, confidence: float, reason: str) -> None:
        answer = _trim_candidate(text[start:end])
        if not (2 <= len(answer) <= 24) or answer in _STOP_CANDIDATES:
            return
        adjusted = text.find(answer, start, end + 1)
        if adjusted < 0:
            return
        end_adjusted = adjusted + len(answer)
        if re.fullmatch(r"[\W_]+", answer):
            return
        proposals.append((adjusted, end_adjusted, answer, confidence, reason))

    definition_patterns = [
        re.compile(r"([^，。；：]{2,24}?)(?:是指|指的是|是指称|称为|叫作)"),
        re.compile(r"([^，。；：]{2,20}?)(?:是由).{1,24}?(?:组成|构成)"),
    ]
    for pattern in definition_patterns:
        for match in pattern.finditer(text):
            add(match.start(1), match.end(1), 0.92, "定义或组成关系")

    for match in re.finditer(r"[“\"《]([^”\"》]{2,24})[”\"》]", text):
        add(match.start(1), match.end(1), 0.88, "原文重点标记")

    heading = re.match(r"^([^：:]{2,20})[：:]", text)
    if heading:
        add(heading.start(1), heading.end(1), 0.84, "段落主题")

    for match in re.finditer(r"(?:包括|分为|有|如下)[：:]?([^。；]{3,60})", text):
        fragment = match.group(1)
        fragment_start = match.start(1)
        for part in re.finditer(r"[^，、和及与]{2,18}", fragment):
            add(
                fragment_start + part.start(),
                fragment_start + part.end(),
                0.80,
                "列举要点",
            )

    for match in re.finditer(r"(?<![A-Za-z])(?:\d{4}年|\d+(?:\.\d+)?%|[A-Z]{2,8})(?![A-Za-z])", text):
        add(match.start(), match.end(), 0.78, "数字或缩写要点")

    proposals.sort(key=lambda item: (-item[3], item[0], -(item[1] - item[0])))
    accepted: list[tuple[int, int, str, float, str]] = []
    seen: set[str] = set()
    for item in proposals:
        start, end, answer, _, _ = item
        key = _candidate_key(answer)
        if key in seen or any(start < other[1] and end > other[0] for other in accepted):
            continue
        if sum(other[1] - other[0] for other in accepted) + len(answer) > max(18, len(text) * 0.28):
            continue
        accepted.append(item)
        seen.add(key)
        if len(accepted) >= 4:
            break
    accepted.sort(key=lambda item: item[0])
    return [
        {
            "id": f"{block_id}-blank-{index + 1}",
            "start": start,
            "end": end,
            "answer": answer,
            "enabled": True,
            "source": "machine_suggestion",
            "reason": reason,
            "confidence": round(confidence, 3),
        }
        for index, (start, end, answer, confidence, reason) in enumerate(accepted)
    ]


def _blocks_from_pages(pages: Iterable[tuple[int, str, float, str]]) -> list[dict]:
    blocks: list[dict] = []
    total_chars = 0
    for page_number, page_text, confidence, source in pages:
        for paragraph in split_paragraphs(page_text):
            total_chars += len(paragraph)
            if total_chars > MAX_TOTAL_CHARS:
                raise RuntimeError("资料文字超过 24 万字，请拆分文件后重试")
            if len(blocks) >= MAX_BLOCKS:
                raise RuntimeError(f"资料超过 {MAX_BLOCKS} 个段落，请拆分文件后重试")
            block_id = f"block-{len(blocks) + 1}"
            blocks.append(
                {
                    "id": block_id,
                    "page": page_number,
                    "order": len(blocks) + 1,
                    "text": paragraph,
                    "confidence": round(max(0.0, min(1.0, confidence)), 4),
                    "source": source,
                    "blanks": suggest_blanks(paragraph, block_id),
                }
            )
    if not blocks:
        raise RuntimeError("没有识别到可编辑文字，请检查文件清晰度")
    return blocks


def process_study_pdf(
    source: Path,
    dpi: int = 200,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[dict]:
    document = pdfium.PdfDocument(str(source))
    page_count = len(document)
    if page_count < 1:
        raise RuntimeError("PDF 没有可处理页面")
    if page_count > 120:
        raise RuntimeError("单份资料最多处理 120 页，请拆分后重试")
    try:
        extractor = RapidOcrMarkerDetector.shared()
        extractor.start_document()
    except Exception:
        extractor = None
    pages: list[tuple[int, str, float, str]] = []
    with pdfplumber.open(source) as text_document:
        for page_index in range(page_count):
            direct = ""
            if page_index < len(text_document.pages):
                direct = _clean_line(text_document.pages[page_index].extract_text() or "")
                # Restore likely paragraph breaks retained by pdfplumber before whitespace cleanup.
                raw = text_document.pages[page_index].extract_text() or ""
                direct = "\n".join(_clean_line(line) for line in raw.splitlines())
            compact = re.sub(r"\s+", "", direct)
            if len(compact) >= 24:
                pages.append((page_index + 1, direct, 1.0, "pdf_text"))
            else:
                if extractor is None:
                    raise RuntimeError("扫描版 PDF 需要安装 rapidocr-onnxruntime")
                scale = dpi / 72.0
                image = document[page_index].render(scale=scale).to_pil().convert("RGB")
                result = extractor.extract_text_fast(image) or {}
                pages.append(
                    (
                        page_index + 1,
                        str(result.get("text") or ""),
                        float(result.get("confidence") or 0.0),
                        str(result.get("source") or "rapidocr_full_text"),
                    )
                )
            if progress_callback:
                progress_callback(page_index + 1, page_count, "正在识别整页文字")
    return _blocks_from_pages(pages)


def process_study_images(
    sources: Sequence[Path],
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[dict]:
    try:
        extractor = RapidOcrMarkerDetector.shared()
        extractor.start_document()
    except Exception as error:
        raise RuntimeError("图片文字识别需要安装 rapidocr-onnxruntime") from error
    pages: list[tuple[int, str, float, str]] = []
    for index, source in enumerate(sources):
        try:
            with Image.open(source) as opened:
                image = opened.convert("RGB")
        except Exception as error:
            raise RuntimeError(f"无法读取第 {index + 1} 张图片: {error}") from error
        result = extractor.extract_text_fast(image) or {}
        pages.append(
            (
                index + 1,
                str(result.get("text") or ""),
                float(result.get("confidence") or 0.0),
                str(result.get("source") or "rapidocr_full_text"),
            )
        )
        if progress_callback:
            progress_callback(index + 1, len(sources), "正在识别图片文字")
    return _blocks_from_pages(pages)
