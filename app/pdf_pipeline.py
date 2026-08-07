from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image


QUESTION_PREFIX = re.compile(
    r"^\s*(?:第\s*)?(\d{1,3})\s*(?:[.．。、:：)）]|题(?:\s|$))"
)
PREFIX_TO_REMOVE = re.compile(
    r"^\s*(?:第\s*)?\d{1,3}\s*(?:[.．。、:：)）]|题)?\s*"
)


class PdfProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Marker:
    page_index: int
    y_ratio: float
    number_label: str
    summary: str
    source: str


@dataclass(frozen=True)
class ProcessedQuestion:
    number_label: str
    summary: str
    image_paths: tuple[Path, ...]
    source_pages: tuple[int, ...]
    detection_source: str


@dataclass(frozen=True)
class PipelineOptions:
    dpi: int = 220
    top_margin_ratio: float = 0.035
    bottom_margin_ratio: float = 0.965
    marker_padding_px: int = 14
    vertical_padding_px: int = 12
    max_output_height: int = 20000
    jpeg_quality: int = 90
    max_questions: int = 2000


def _clean_summary(value: str, number_label: str) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()
    compact = PREFIX_TO_REMOVE.sub("", compact).strip(" -_·")
    if len(compact) < 2:
        return f"第 {number_label} 题"
    return compact[:200]


def _group_words_into_lines(words: Sequence[dict], tolerance: float = 3.0) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        top = float(word["top"])
        if not lines or abs(top - float(lines[-1][0]["top"])) > tolerance:
            lines.append([word])
        else:
            lines[-1].append(word)
    for line in lines:
        line.sort(key=lambda item: float(item["x0"]))
    return lines


def _line_text(line: Sequence[dict]) -> str:
    return " ".join(str(item.get("text", "")).strip() for item in line).strip()


def detect_text_markers(pdf_path: Path) -> list[Marker]:
    markers: list[Marker] = []
    with pdfplumber.open(str(pdf_path)) as document:
        for page_index, page in enumerate(document.pages):
            words = page.extract_words(
                keep_blank_chars=False,
                use_text_flow=True,
                extra_attrs=["size"],
            )
            for line in _group_words_into_lines(words):
                if not line:
                    continue
                first_x = float(line[0]["x0"])
                top = float(min(item["top"] for item in line))
                if first_x > float(page.width) * 0.35:
                    continue
                if top < float(page.height) * 0.03 or top > float(page.height) * 0.95:
                    continue
                line_text = _line_text(line)
                match = QUESTION_PREFIX.match(line_text)
                if not match:
                    continue
                number_label = match.group(1)
                markers.append(
                    Marker(
                        page_index=page_index,
                        y_ratio=max(0.0, min(1.0, top / float(page.height))),
                        number_label=number_label,
                        summary=_clean_summary(line_text, number_label),
                        source="text_layer",
                    )
                )
    return _deduplicate_markers(markers)


def _deduplicate_markers(markers: Iterable[Marker]) -> list[Marker]:
    ordered = sorted(markers, key=lambda item: (item.page_index, item.y_ratio))
    result: list[Marker] = []
    for marker in ordered:
        if result:
            previous = result[-1]
            if (
                marker.page_index == previous.page_index
                and marker.number_label == previous.number_label
                and abs(marker.y_ratio - previous.y_ratio) < 0.015
            ):
                continue
        result.append(marker)
    return result


class RapidOcrMarkerDetector:
    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as error:
            raise PdfProcessingError(
                f"扫描版 PDF OCR 导入失败: {type(error).__name__}: {error}"
            ) from error
        self._engine = RapidOCR()

    def __call__(self, page_image: Image.Image, page_index: int) -> list[Marker]:
        scan_width = max(1, round(page_image.width * 0.48))
        scan_image = page_image.crop((0, 0, scan_width, page_image.height)).convert("RGB")
        result, _ = self._engine(np.asarray(scan_image))
        markers: list[Marker] = []
        for row in result or []:
            if not row or len(row) < 3:
                continue
            box, value, confidence = row
            if float(confidence) < 0.48:
                continue
            recognized = re.sub(r"\s+", " ", str(value)).strip()
            match = QUESTION_PREFIX.match(recognized)
            if not match:
                continue
            points = np.asarray(box, dtype=float)
            y = float(points[:, 1].min())
            number_label = match.group(1)
            markers.append(
                Marker(
                    page_index=page_index,
                    y_ratio=max(0.0, min(1.0, y / page_image.height)),
                    number_label=number_label,
                    summary=_clean_summary(recognized, number_label),
                    source="rapidocr_number",
                )
            )
        return _deduplicate_markers(markers)


def _render_page(document: pdfium.PdfDocument, page_index: int, dpi: int) -> Image.Image:
    scale = dpi / 72.0
    return document[page_index].render(scale=scale).to_pil().convert("RGB")


def _trim_vertical(image: Image.Image, padding: int) -> Image.Image:
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    grayscale = np.asarray(image.convert("L"))
    chroma = rgb.max(axis=2) - rgb.min(axis=2)
    content = (grayscale < 245) | (chroma > 7)
    minimum_ink = max(3, round(image.width * 0.002))
    rows = np.flatnonzero(content.sum(axis=1) >= minimum_ink)
    if not rows.size:
        raise PdfProcessingError("检测到空白题目区域")
    top = max(0, int(rows[0]) - padding)
    bottom = min(image.height, int(rows[-1]) + 1 + padding)
    return image.crop((0, top, image.width, bottom)).convert("RGB")


def _stitch_vertical(images: Sequence[Image.Image], gap: int = 8) -> Image.Image:
    if not images:
        raise PdfProcessingError("没有可拼接的题目图片")
    width = max(image.width for image in images)
    height = sum(image.height for image in images) + gap * (len(images) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for image in images:
        canvas.paste(image, (0, y))
        y += image.height + gap
    return canvas


def _find_safe_split(image: Image.Image, desired: int, radius: int = 160) -> int:
    grayscale = np.asarray(image.convert("L"))
    start = max(1, desired - radius)
    end = min(image.height - 1, desired + radius)
    if end <= start:
        return min(image.height - 1, max(1, desired))
    row_ink = (grayscale[start:end] < 245).sum(axis=1)
    return start + int(np.argmin(row_ink))


def _split_tall_image(image: Image.Image, max_height: int) -> list[Image.Image]:
    if image.height <= max_height:
        return [image]
    parts: list[Image.Image] = []
    top = 0
    while image.height - top > max_height:
        split = _find_safe_split(image, top + max_height)
        if split <= top:
            split = min(image.height, top + max_height)
        parts.append(image.crop((0, top, image.width, split)).convert("RGB"))
        top = split
    if top < image.height:
        parts.append(image.crop((0, top, image.width, image.height)).convert("RGB"))
    return parts


def _safe_number(value: str, fallback: int) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", value).strip("-")
    return normalized or str(fallback)


def _detect_markers(
    pdf_path: Path,
    document: pdfium.PdfDocument,
    options: PipelineOptions,
    ocr_detector: Callable[[Image.Image, int], list[Marker]] | None,
) -> list[Marker]:
    text_markers = detect_text_markers(pdf_path)
    if text_markers:
        return text_markers
    detector = ocr_detector or RapidOcrMarkerDetector()
    markers: list[Marker] = []
    for page_index in range(len(document)):
        markers.extend(detector(_render_page(document, page_index, options.dpi), page_index))
    return _deduplicate_markers(markers)


def process_pdf(
    pdf_path: Path,
    output_dir: Path,
    options: PipelineOptions | None = None,
    ocr_detector: Callable[[Image.Image, int], list[Marker]] | None = None,
) -> list[ProcessedQuestion]:
    options = options or PipelineOptions()
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    if not pdf_path.is_file():
        raise PdfProcessingError(f"PDF 文件不存在: {pdf_path}")
    output_dir.mkdir(parents=True, exist_ok=True)

    document = pdfium.PdfDocument(str(pdf_path))
    if len(document) < 1:
        raise PdfProcessingError("PDF 没有可处理页面")
    markers = _detect_markers(pdf_path, document, options, ocr_detector)
    if not markers:
        raise PdfProcessingError("没有检测到题号，请确认 PDF 页面中包含清晰题号")
    if len(markers) > options.max_questions:
        raise PdfProcessingError("检测到的题目数量超过处理上限")

    page_cache: dict[int, Image.Image] = {}

    def page_image(page_index: int) -> Image.Image:
        if page_index not in page_cache:
            page_cache[page_index] = _render_page(document, page_index, options.dpi)
        return page_cache[page_index]

    results: list[ProcessedQuestion] = []
    for marker_index, marker in enumerate(markers):
        next_marker = markers[marker_index + 1] if marker_index + 1 < len(markers) else None
        end_page = next_marker.page_index if next_marker else marker.page_index
        pieces: list[Image.Image] = []
        source_pages: list[int] = []
        for page_index in range(marker.page_index, end_page + 1):
            image = page_image(page_index)
            top_ratio = (
                max(options.top_margin_ratio, marker.y_ratio)
                if page_index == marker.page_index
                else options.top_margin_ratio
            )
            bottom_ratio = options.bottom_margin_ratio
            if next_marker and page_index == next_marker.page_index:
                bottom_ratio = min(options.bottom_margin_ratio, next_marker.y_ratio)
            top = max(0, round(top_ratio * image.height) - options.marker_padding_px)
            bottom = min(image.height, round(bottom_ratio * image.height))
            if bottom <= top:
                continue
            try:
                piece = _trim_vertical(
                    image.crop((0, top, image.width, bottom)).convert("RGB"),
                    options.vertical_padding_px,
                )
            except PdfProcessingError:
                continue
            pieces.append(piece)
            source_pages.append(page_index + 1)
        if not pieces:
            raise PdfProcessingError(f"第 {marker.number_label} 题裁剪结果为空")

        stitched = _stitch_vertical(pieces)
        image_parts = _split_tall_image(stitched, options.max_output_height)
        safe_number = _safe_number(marker.number_label, marker_index + 1)
        paths: list[Path] = []
        for part_index, part in enumerate(image_parts, start=1):
            suffix = f"-part-{part_index}" if len(image_parts) > 1 else ""
            output_path = output_dir / f"q-{marker_index + 1:04d}-{safe_number}{suffix}.jpg"
            part.save(
                output_path,
                "JPEG",
                quality=options.jpeg_quality,
                optimize=True,
                progressive=True,
                subsampling=0,
            )
            paths.append(output_path)
        results.append(
            ProcessedQuestion(
                number_label=marker.number_label,
                summary=marker.summary,
                image_paths=tuple(paths),
                source_pages=tuple(source_pages),
                detection_source=marker.source,
            )
        )
    return results
