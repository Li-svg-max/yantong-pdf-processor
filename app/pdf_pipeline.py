from __future__ import annotations

import re
import threading
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


OcrRow = tuple[float, float, float, str, float]


def _notify_progress(
    callback: Callable[..., None] | None,
    done: int,
    total: int,
    status_text: str,
) -> None:
    """Keep the public two-argument callback compatible with older callers."""
    if not callback:
        return
    try:
        callback(done, total, status_text)
    except TypeError:
        callback(done, total)


def _rows_to_text(rows: Sequence[OcrRow]) -> tuple[str, float]:
    ordered = sorted(rows, key=lambda item: (item[0], item[2]))
    if not ordered:
        return "", 0.0
    lines: list[list[OcrRow]] = []
    for row in ordered:
        row_height = max(0.0001, row[1] - row[0])
        if not lines:
            lines.append([row])
            continue
        previous = lines[-1]
        line_top = sum(item[0] for item in previous) / len(previous)
        line_height = max(
            row_height,
            sum(max(0.0001, item[1] - item[0]) for item in previous) / len(previous),
        )
        if abs(row[0] - line_top) <= max(0.004, line_height * 0.72):
            previous.append(row)
        else:
            lines.append([row])
    text_lines = []
    for line in lines:
        line.sort(key=lambda item: item[2])
        text_lines.append(" ".join(item[3] for item in line).strip())
    text = "\n".join(line for line in text_lines if line).strip()[:12000]
    confidence = sum(item[4] for item in ordered) / len(ordered)
    return text, round(confidence, 4)


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


def extract_text_layer_question(
    pdf_path: Path,
    marker: Marker,
    next_marker: Marker | None,
    options: PipelineOptions,
) -> str:
    """Read the actual text of one text-layer question without flattening lines.

    A PDF text layer already contains much better punctuation and symbol data
    than OCR. We keep its visual line boundaries so the client can wrap a
    passage and a formula naturally instead of receiving one long sentence.
    """
    lines: list[str] = []
    final_page = next_marker.page_index if next_marker else marker.page_index
    with pdfplumber.open(str(pdf_path)) as document:
        for page_index in range(marker.page_index, final_page + 1):
            page = document.pages[page_index]
            top_ratio = (
                max(options.top_margin_ratio, marker.y_ratio)
                if page_index == marker.page_index
                else options.top_margin_ratio
            )
            bottom_ratio = options.bottom_margin_ratio
            if next_marker and page_index == next_marker.page_index:
                bottom_ratio = min(options.bottom_margin_ratio, next_marker.y_ratio)
            top = float(page.height) * top_ratio
            bottom = float(page.height) * bottom_ratio
            words = page.extract_words(
                keep_blank_chars=False,
                use_text_flow=True,
                extra_attrs=["size"],
            )
            visible_words = [
                word
                for word in words
                if top <= (float(word["top"]) + float(word["bottom"])) / 2 < bottom
            ]
            lines.extend(_line_text(line) for line in _group_words_into_lines(visible_words))
    return "\n".join(line for line in lines if line).strip()


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
    _shared_instance: "RapidOcrMarkerDetector | None" = None
    _shared_lock = threading.Lock()

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as error:
            raise PdfProcessingError(
                f"扫描版 PDF OCR 导入失败: {type(error).__name__}: {error}"
            ) from error
        self._engine = RapidOCR()
        self._page_rows: dict[int, tuple[OcrRow, ...]] = {}

    @classmethod
    def shared(cls) -> "RapidOcrMarkerDetector":
        """Reuse the ONNX session across queued jobs in the same container."""
        if cls._shared_instance is None:
            with cls._shared_lock:
                if cls._shared_instance is None:
                    cls._shared_instance = cls()
        return cls._shared_instance

    def start_document(self) -> None:
        """Drop page OCR rows from the previous PDF before starting a new one."""
        self._page_rows = {}

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

    @staticmethod
    def _fit_width(image: Image.Image, max_width: int = 2200) -> Image.Image:
        source = image.convert("RGB")
        if source.width <= max_width:
            return source
        height = max(1, round(source.height * max_width / source.width))
        return source.resize((max_width, height), Image.Resampling.LANCZOS)

    def _read_rows_from_variant(
        self,
        variant: Image.Image,
        minimum_confidence: float = 0.25,
    ) -> list[OcrRow]:
        result, _ = self._engine(np.asarray(variant))
        rows: list[OcrRow] = []
        width = max(1.0, float(variant.width))
        height = max(1.0, float(variant.height))
        for row in result or []:
            if not row or len(row) < 3:
                continue
            box, value, confidence = row
            confidence = float(confidence)
            recognized = _normalize_ocr_text(value)
            if confidence < minimum_confidence or not recognized:
                continue
            points = np.asarray(box, dtype=float)
            rows.append(
                (
                    float(points[:, 1].min()) / height,
                    float(points[:, 1].max()) / height,
                    float(points[:, 0].min()) / width,
                    recognized,
                    confidence,
                )
            )
        return sorted(rows, key=lambda item: (item[0], item[2]))

    def extract_page_rows(self, page_image: Image.Image, page_index: int) -> list[OcrRow]:
        """OCR one rendered page and retain rows for all questions on that page."""
        cached = self._page_rows.get(page_index)
        if cached is not None:
            return list(cached)
        image = self._fit_width(page_image)
        variants = self._variants(image)
        best: list[OcrRow] = []
        # The original color pass is fastest and preserves formula glyphs.
        for variant in variants[:2]:
            rows = self._read_rows_from_variant(variant)
            if len(rows) > len(best) or (
                rows and sum(item[4] for item in rows) / len(rows)
                > (sum(item[4] for item in best) / len(best) if best else 0)
            ):
                best = rows
            if len(rows) >= 4 and sum(item[4] for item in rows) / len(rows) >= 0.78:
                break
        self._page_rows[page_index] = tuple(best)
        return list(best)

    def __call__(self, page_image: Image.Image, page_index: int) -> list[Marker]:
        rows = self.extract_page_rows(page_image, page_index)
        markers: list[Marker] = []
        for y_min, _, x_min, recognized, confidence in rows:
            if x_min > 0.68 or confidence < 0.40:
                continue
            match = QUESTION_PREFIX.match(recognized)
            if not match:
                continue
            number_label = match.group(1)
            markers.append(
                Marker(
                    page_index=page_index,
                    y_ratio=max(0.0, min(1.0, y_min)),
                    number_label=number_label,
                    summary=_clean_summary(recognized, number_label),
                    source="rapidocr_number",
                )
            )
        return _deduplicate_markers(markers)

    def extract_text(self, question_image: Image.Image) -> dict[str, object]:
        """Recognize a crop while preserving visual line structure in text."""
        image = self._fit_width(question_image)
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
        has_colored_annotation = bool(blue_green_ink.any())
        for variant_index, (variant, source) in enumerate(variants):
            rows = self._read_rows_from_variant(variant)
            if rows:
                text, confidence = _rows_to_text(rows)
                candidates.append(
                    {
                        "text": text,
                        "confidence": confidence,
                        "source": source,
                    }
                )
                best_confidence = max(
                    float(item["confidence"]) for item in candidates
                )
                if (
                    best_confidence >= 0.90
                    and len(str(candidates[-1]["text"])) >= 12
                    and (not has_colored_annotation or variant_index >= 2)
                ):
                    break
        if not candidates:
            return {"text": "", "confidence": 0.0, "source": "rapidocr_full_text"}
        best = max(
            candidates,
            key=lambda item: (float(item["confidence"]), min(300, len(str(item["text"])))),
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
    progress_callback: Callable[..., None] | None = None,
) -> list[Marker]:
    text_markers = detect_text_markers(pdf_path) if text_markers is None else text_markers
    if text_markers:
        _notify_progress(progress_callback, len(document), len(document), "题号已定位")
        return text_markers
    try:
        detector = ocr_detector or RapidOcrMarkerDetector.shared()
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
            finally:
                _notify_progress(
                    progress_callback,
                    page_index + 1,
                    len(document),
                    "正在识别题号",
                )
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
        _notify_progress(
            progress_callback,
            page_index + 1,
            len(document),
            "正在定位题号区域",
        )
    return colored_markers


def process_pdf(
    pdf_path: Path,
    output_dir: Path,
    options: PipelineOptions | None = None,
    ocr_detector: Callable[[Image.Image, int], list[Marker]] | None = None,
    ocr_text_extractor: Callable[[Image.Image], dict[str, object]] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
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
            resolved_ocr_detector = RapidOcrMarkerDetector.shared()
        except PdfProcessingError:
            resolved_ocr_detector = None
    if hasattr(resolved_ocr_detector, "start_document"):
        try:
            resolved_ocr_detector.start_document()
        except Exception:
            pass
    markers = _detect_markers(
        pdf_path,
        document,
        options,
        resolved_ocr_detector,
        text_markers=text_markers,
        progress_callback=progress_callback,
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

    page_ocr_rows: dict[int, list[OcrRow]] = {}
    page_ocr_extractor = (
        getattr(resolved_ocr_detector, "extract_page_rows", None)
        if resolved_ocr_detector is not None
        else None
    )
    if not text_markers and page_ocr_extractor:
        # __call__ already populated these rows while locating markers. This
        # lookup is therefore normally free and gives every question on a page
        # one shared OCR pass instead of one OCR pass per crop.
        for page_index in sorted({marker.page_index for marker in markers}):
            try:
                page_ocr_rows[page_index] = page_ocr_extractor(
                    page_image(page_index), page_index
                )
            except Exception:
                page_ocr_rows[page_index] = []

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
        if marker.source == "text_layer":
            text_layer_value = extract_text_layer_question(
                pdf_path, marker, next_marker, options
            )
            if text_layer_value:
                recognized_text = text_layer_value[:12000]
                text_source = "pdf_text"
        extractor = ocr_text_extractor
        if extractor is None and hasattr(resolved_ocr_detector, "extract_text"):
            extractor = getattr(resolved_ocr_detector, "extract_text")
        page_rows_for_question: list[OcrRow] = []
        if marker.source != "text_layer" and page_ocr_rows:
            for page_index in range(marker.page_index, end_page + 1):
                rows = page_ocr_rows.get(page_index, [])
                top_ratio = (
                    max(options.top_margin_ratio, marker.y_ratio)
                    if page_index == marker.page_index
                    else options.top_margin_ratio
                )
                bottom_ratio = options.bottom_margin_ratio
                if next_marker and page_index == next_marker.page_index:
                    bottom_ratio = min(options.bottom_margin_ratio, next_marker.y_ratio)
                page_rows_for_question.extend(
                    row for row in rows if top_ratio <= row[0] < bottom_ratio
                )
            page_text, page_confidence = _rows_to_text(page_rows_for_question)
            if len(page_text) >= 12:
                recognized_text = page_text[:12000]
                text_source = "rapidocr_page_text"
                text_confidence = page_confidence

        needs_crop_ocr = (
            marker.source != "text_layer"
            and extractor
            and (
                not page_rows_for_question
                or text_confidence < 0.78
                or len(recognized_text.strip()) < 12
            )
        )
        if needs_crop_ocr:
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
        _notify_progress(
            progress_callback,
            marker_index + 1,
            len(markers),
            "正在整理题目文字",
        )
    return results
