from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageOps


QUESTION_PREFIX = re.compile(
    r"^\s*(?:第\s*)?(\d{1,3})\s*(?:[.．。、:：)）]|题(?:\s|$))"
)
PREFIX_TO_REMOVE = re.compile(
    r"^\s*(?:第\s*)?\d{1,3}\s*(?:[.．。、:：)）]|题)?\s*"
)
OCR_DIGIT_TRANSLATION = str.maketrans("０１２３４５６７８９", "0123456789")


def _normalize_ocr_text(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().translate(OCR_DIGIT_TRANSLATION)
    # A leading capital I/l or vertical stroke is a common scan OCR error for
    # the number 1. Restrict this correction to the question-marker position.
    return re.sub(r"^[Il|]\s*([.．。、:：)）])", r"1\1", text)


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
    recognized_text: str
    text_source: str
    text_confidence: float
    image_paths: tuple[Path, ...]
    source_pages: tuple[int, ...]
    detection_source: str
    image_quality: dict[str, object]


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

    @staticmethod
    def _variants(image: Image.Image) -> tuple[Image.Image, ...]:
        """Build a small, deterministic set of scan variants for RapidOCR.

        Upscaling only low-resolution scans improves digit separation without
        making normal 220 DPI pages larger. The last variant uses a threshold
        derived from the page background instead of a fixed global cutoff.
        """
        source = image.convert("RGB")
        if source.width < 1200:
            scale = min(1.6, 1200 / max(1, source.width))
            source = source.resize(
                (max(16, round(source.width * scale)), max(16, round(source.height * scale))),
                Image.Resampling.LANCZOS,
            )
        gray = ImageOps.autocontrast(ImageOps.grayscale(source), cutoff=1)
        gray_values = np.asarray(gray, dtype=np.uint8)
        background = float(np.percentile(gray_values, 85)) if gray_values.size else 255.0
        threshold = int(max(160, min(230, round(background * 0.78))))
        binary = gray.point(lambda value: 255 if value >= threshold else 0)
        return (source, gray.convert("RGB"), binary.convert("RGB"))

    def __call__(self, page_image: Image.Image, page_index: int) -> list[Marker]:
        scan_width = max(1, round(page_image.width * 0.68))
        scan_image = page_image.crop((0, 0, scan_width, page_image.height)).convert("RGB")
        for variant in self._variants(scan_image):
            result, _ = self._engine(np.asarray(variant))
            markers: list[Marker] = []
            for row in result or []:
                if not row or len(row) < 3:
                    continue
                box, value, confidence = row
                if float(confidence) < 0.40:
                    continue
                recognized = _normalize_ocr_text(value)
                match = QUESTION_PREFIX.match(recognized)
                if not match:
                    continue
                points = np.asarray(box, dtype=float)
                y = float(points[:, 1].min())
                number_label = match.group(1)
                markers.append(
                    Marker(
                        page_index=page_index,
                        y_ratio=max(0.0, min(1.0, y / float(variant.height))),
                        number_label=number_label,
                        summary=_clean_summary(recognized, number_label),
                        source="rapidocr_number",
                    )
                )
            markers = _deduplicate_markers(markers)
            if markers:
                return markers
        return []

    def extract_text(self, question_image: Image.Image) -> dict[str, object]:
        """Recognize a crop while retaining its image as the source of truth."""
        image = question_image.convert("RGB")
        if image.width > 2600:
            height = max(1, round(image.height * 2600 / image.width))
            image = image.resize((2600, height), Image.Resampling.LANCZOS)
        pixels = np.asarray(image, dtype=np.int16)
        red, green, blue = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
        blue_green_ink = (
            (pixels.max(axis=2) - pixels.min(axis=2) >= 40)
            & (pixels.min(axis=2) <= 225)
            & (((blue - red) >= 28) | ((green - red) >= 34))
        )
        suppressed_pixels = np.asarray(image).copy()
        suppressed_pixels[blue_green_ink] = 255
        prepared_variants = self._variants(image)
        variants = (
            (prepared_variants[0], "rapidocr_full_text"),
            (prepared_variants[1], "rapidocr_full_text_grayscale"),
            (Image.fromarray(suppressed_pixels), "rapidocr_annotation_suppressed"),
            (prepared_variants[2], "rapidocr_adaptive_binary"),
        )
        candidates: list[dict[str, object]] = []
        for variant, source in variants:
            result, _ = self._engine(np.asarray(variant))
            rows: list[tuple[float, float, str, float]] = []
            for row in result or []:
                if not row or len(row) < 3:
                    continue
                box, value, confidence = row
                confidence = float(confidence)
                recognized = _normalize_ocr_text(value)
                if confidence < 0.25 or not recognized:
                    continue
                points = np.asarray(box, dtype=float)
                rows.append(
                    (
                        float(points[:, 1].min()),
                        float(points[:, 0].min()),
                        recognized,
                        confidence,
                    )
                )
            rows.sort(key=lambda item: (round(item[0] / 12), item[1]))
            if rows:
                candidates.append(
                    {
                        "text": "\n".join(item[2] for item in rows).strip()[:12000],
                        "confidence": sum(item[3] for item in rows) / len(rows),
                        "source": source,
                    }
                )
        if not candidates:
            return {"text": "", "confidence": 0.0, "source": "rapidocr_full_text"}
        best = max(
            candidates,
            key=lambda item: (len(str(item["text"])), float(item["confidence"])),
        )
        return {
            "text": str(best["text"]),
            "confidence": round(float(best["confidence"]), 4),
            "source": str(best["source"]),
        }


def _contiguous_ranges(values: Sequence[int], max_gap: int = 1) -> list[tuple[int, int]]:
    if len(values) == 0:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = int(values[0])
    for value in values[1:]:
        value = int(value)
        if value > previous + max_gap:
            ranges.append((start, previous))
            start = value
        previous = value
    ranges.append((start, previous))
    return ranges


def detect_colored_question_boxes(
    page_image: Image.Image,
    page_index: int,
    first_number: int,
) -> list[Marker]:
    """Detect the pink numbered labels used by the 660 scanned workbook."""
    rgb = np.asarray(page_image.convert("RGB"))
    height, width = rgb.shape[:2]
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    mask = (red >= 180) & ((red - green) >= 25) & ((red - blue) >= 10)
    mask[:, round(width * 0.35) :] = False

    row_threshold = max(8, round(width * 0.006))
    rows = np.flatnonzero(mask.sum(axis=1) >= row_threshold)
    markers: list[Marker] = []
    for offset, (top, bottom) in enumerate(_contiguous_ranges(rows, max_gap=2)):
        region = mask[top : bottom + 1]
        column_threshold = max(4, round((bottom - top + 1) * 0.15))
        columns = np.flatnonzero(region.sum(axis=0) >= column_threshold)
        if not columns.size:
            continue
        left, right = int(columns[0]), int(columns[-1])
        box_width = right - left + 1
        box_height = bottom - top + 1
        if not (width * 0.025 <= box_width <= width * 0.15):
            continue
        if not (height * 0.01 <= box_height <= height * 0.08):
            continue
        number = str(first_number + len(markers))
        markers.append(
            Marker(
                page_index=page_index,
                y_ratio=max(0.0, min(1.0, top / float(height))),
                number_label=number,
                summary=f"扫描版第 {number} 题",
                source="colored_question_box",
            )
        )
    return markers


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


def analyze_image_quality(image: Image.Image) -> dict[str, object]:
    """Flag likely annotation/scan issues without altering the source crop."""
    sample = image.copy()
    sample.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
    rgb = np.asarray(sample.convert("RGB"), dtype=np.int16)
    gray = np.asarray(sample.convert("L"), dtype=np.int16)
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    chroma = rgb.max(axis=2) - rgb.min(axis=2)
    colored = (chroma >= 45) & (rgb.min(axis=2) <= 220)
    blue_green_ink = colored & (((blue - red) >= 35) | ((green - red) >= 40))
    red_ink = colored & ((red - green) >= 45) & ((red - blue) >= 25)
    colored_ratio = float(colored.mean())
    blue_green_ratio = float(blue_green_ink.mean())
    red_ratio = float(red_ink.mean())
    dark_ratio = float((gray < 210).mean())
    horizontal_gradient = np.abs(np.diff(gray, axis=1)) if gray.shape[1] > 1 else np.zeros((1, 1))
    vertical_gradient = np.abs(np.diff(gray, axis=0)) if gray.shape[0] > 1 else np.zeros((1, 1))
    sharpness = float((horizontal_gradient.mean() + vertical_gradient.mean()) / 2)

    annotation_risk = "low"
    # Blue/green marks are rarely part of monochrome exercise text. Red remains
    # medium risk because some workbooks use printed red question markers.
    if blue_green_ratio >= 0.0015 or red_ratio >= 0.012:
        annotation_risk = "high"
    elif blue_green_ratio >= 0.0003 or red_ratio >= 0.003 or colored_ratio >= 0.02:
        annotation_risk = "medium"
    scan_quality = "good"
    if sharpness < 2.2 or dark_ratio < 0.002:
        scan_quality = "poor"
    elif sharpness < 3.5:
        scan_quality = "fair"
    return {
        "annotationRisk": annotation_risk,
        "scanQuality": scan_quality,
        "coloredInkRatio": round(colored_ratio, 6),
        "blueGreenInkRatio": round(blue_green_ratio, 6),
        "redInkRatio": round(red_ratio, 6),
        "darkPixelRatio": round(dark_ratio, 6),
        "sharpnessScore": round(sharpness, 3),
        "originalPreserved": True,
        "requiresVisualReview": annotation_risk != "low" or scan_quality != "good",
    }


def _safe_number(value: str, fallback: int) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", value).strip("-")
    return normalized or str(fallback)


def _detect_markers(
    pdf_path: Path,
    document: pdfium.PdfDocument,
    options: PipelineOptions,
    ocr_detector: Callable[[Image.Image, int], list[Marker]] | None,
    text_markers: list[Marker] | None = None,
) -> list[Marker]:
    text_markers = detect_text_markers(pdf_path) if text_markers is None else text_markers
    if text_markers:
        return text_markers
    try:
        detector = ocr_detector or RapidOcrMarkerDetector()
    except PdfProcessingError:
        detector = None
    markers: list[Marker] = []
    if detector:
        for page_index in range(len(document)):
            try:
                markers.extend(
                    detector(_render_page(document, page_index, options.dpi), page_index)
                )
            except Exception:
                continue
    markers = _deduplicate_markers(markers)
    if markers:
        return markers

    colored_markers: list[Marker] = []
    next_number = 1
    for page_index in range(len(document)):
        page_markers = detect_colored_question_boxes(
            _render_page(document, page_index, options.dpi),
            page_index,
            next_number,
        )
        colored_markers.extend(page_markers)
        next_number += len(page_markers)
    return colored_markers


def process_pdf(
    pdf_path: Path,
    output_dir: Path,
    options: PipelineOptions | None = None,
    ocr_detector: Callable[[Image.Image, int], list[Marker]] | None = None,
    ocr_text_extractor: Callable[[Image.Image], dict[str, object]] | None = None,
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
    text_markers = detect_text_markers(pdf_path)
    resolved_ocr_detector = ocr_detector
    if not text_markers and resolved_ocr_detector is None:
        try:
            resolved_ocr_detector = RapidOcrMarkerDetector()
        except PdfProcessingError:
            resolved_ocr_detector = None
    markers = _detect_markers(
        pdf_path,
        document,
        options,
        resolved_ocr_detector,
        text_markers=text_markers,
    )
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
        image_quality = analyze_image_quality(stitched)
        recognized_text = marker.summary
        text_source = "pdf_text_index" if marker.source == "text_layer" else "number_summary"
        text_confidence = 1.0 if marker.source == "text_layer" else 0.0
        extractor = ocr_text_extractor
        if extractor is None and hasattr(resolved_ocr_detector, "extract_text"):
            extractor = getattr(resolved_ocr_detector, "extract_text")
        if marker.source != "text_layer" and extractor:
            try:
                text_result = extractor(stitched) or {}
                candidate_text = re.sub(
                    r"[ \t]+", " ", str(text_result.get("text") or "")
                ).strip()
                if len(candidate_text) >= 2:
                    recognized_text = candidate_text[:12000]
                    text_source = str(text_result.get("source") or "machine_ocr")[:40]
                    text_confidence = max(
                        0.0,
                        min(1.0, float(text_result.get("confidence") or 0.0)),
                    )
            except Exception:
                # The OCR layer is editable convenience text. Its failure must
                # not discard the authoritative crop or fail the import job.
                pass
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
                recognized_text=recognized_text,
                text_source=text_source,
                text_confidence=text_confidence,
                image_paths=tuple(paths),
                source_pages=tuple(source_pages),
                detection_source=marker.source,
                image_quality=image_quality,
            )
        )
    return results
