from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image

from .pdf_pipeline import ProcessedQuestion, RapidOcrMarkerDetector, _clean_summary, analyze_image_quality


def _compact(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _summary_from_text(value: str, number_label: str) -> str:
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    return _clean_summary(first_line, number_label)


def process_image_batch(
    sources: Sequence[tuple[Path, str]],
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[ProcessedQuestion]:
    """Recognize complete question images without exposing the source image.

    Each image is already one user-selected question. Unlike a PDF page, it
    therefore needs no question-boundary crop: RapidOCR reads the full image
    and the original stays private as a reprocessing asset.
    """
    try:
        extractor = RapidOcrMarkerDetector.shared()
    except Exception:
        extractor = None

    results: list[ProcessedQuestion] = []
    for index, (source_path, number_label) in enumerate(sources):
        try:
            with Image.open(source_path) as opened:
                image = opened.convert("RGB")
        except Exception as error:
            raise RuntimeError(f"无法读取第 {number_label} 张题图: {error}") from error

        text_result: dict[str, object] = {}
        if extractor is not None:
            try:
                text_result = extractor.extract_text(image) or {}
            except Exception:
                # An OCR failure should become a visible incomplete-text state,
                # not a fake title or an inaccessible image-only question.
                text_result = {}

        recognized_text = str(text_result.get("text") or "").strip()[:12000]
        confidence = max(0.0, min(1.0, float(text_result.get("confidence") or 0.0)))
        text_source = str(text_result.get("source") or "ocr_unavailable")[:40]
        results.append(
            ProcessedQuestion(
                number_label=str(number_label),
                summary=_summary_from_text(recognized_text, str(number_label)),
                recognized_text=recognized_text,
                text_source=text_source,
                text_confidence=confidence,
                image_paths=(),
                source_pages=(),
                detection_source="image_batch",
                image_quality=analyze_image_quality(image),
            )
        )
        if progress_callback:
            try:
                progress_callback(index + 1, len(sources))
            except Exception:
                pass
    return results
