from __future__ import annotations

"""Formula-region detection derived from Pix2Text's Apache-2.0 MFD workflow."""

import os
import threading
from io import BytesIO
from typing import Any

from PIL import Image


class FormulaDetectorLoadingError(RuntimeError):
    """The formula detector is being initialized in the background."""


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


def normalized_box(
    raw_box: list[float], image_width: int, image_height: int, margin: int = 8
) -> dict[str, int]:
    """Convert an xyxy detector box to a valid, slightly padded crop box."""
    left, top, right, bottom = [int(round(value)) for value in raw_box]
    horizontal_margin = max(margin, int((right - left) * 0.04))
    vertical_margin = max(margin, int((bottom - top) * 0.16))
    left = _clamp(left - horizontal_margin, 0, image_width - 1)
    top = _clamp(top - vertical_margin, 0, image_height - 1)
    right = _clamp(right + horizontal_margin, left + 1, image_width)
    bottom = _clamp(bottom + vertical_margin, top + 1, image_height)
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def crop_region(image: Image.Image, region: dict[str, int]) -> Image.Image:
    width, height = image.size
    left = _clamp(int(region.get("left", 0)), 0, max(0, width - 1))
    top = _clamp(int(region.get("top", 0)), 0, max(0, height - 1))
    crop_width = _clamp(int(region.get("width", 0)), 1, width - left)
    crop_height = _clamp(int(region.get("height", 0)), 1, height - top)
    if crop_width < 16 or crop_height < 16:
        raise ValueError("selected formula region is too small")
    return image.crop((left, top, left + crop_width, top + crop_height))


def candidate_label(formula_type: str, top: int, image_height: int) -> str:
    position = "顶部" if top < image_height * 0.34 else "底部" if top > image_height * 0.67 else "中部"
    return f"{position}{'行间' if formula_type == 'isolated' else '行内'}公式"


class FormulaDetector:
    def __init__(self) -> None:
        self._model: Any = None
        self._load_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._detect_lock = threading.Lock()
        self._loading = False
        self._load_error = ""

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def loading(self) -> bool:
        with self._state_lock:
            return self._loading

    @property
    def load_error(self) -> str:
        with self._state_lock:
            return self._load_error

    def preload(self) -> None:
        with self._state_lock:
            if self._model is not None or self._loading:
                return
            self._loading = True
        threading.Thread(target=self._run_preload, name="formula-detector-preload", daemon=True).start()

    def _run_preload(self) -> None:
        try:
            self.load()
        except Exception:
            # Health exposes the actual error. Avoid taking down the service while retry remains possible.
            pass

    def load(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            with self._state_lock:
                self._loading = True
                self._load_error = ""
            try:
                from ultralytics import YOLO

                model_path = os.getenv("FORMULA_DETECTOR_MODEL", "/opt/formula-detector/pix2text-mfd-1.5.onnx")
                self._model = YOLO(model_path, task="detect")
            except Exception as error:
                with self._state_lock:
                    self._load_error = f"{type(error).__name__}: {error}"[:500]
                raise
            finally:
                with self._state_lock:
                    self._loading = False
        return self._model

    def require_loaded(self):
        if self._model is not None:
            return self._model
        self.preload()
        raise FormulaDetectorLoadingError("formula detector is loading")

    def detect(self, image_bytes: bytes) -> dict[str, Any]:
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        if image.width < 16 or image.height < 16:
            raise ValueError("image is too small")
        if image.width * image.height > 20_000_000:
            raise ValueError("image dimensions are too large")
        model = self.require_loaded()
        confidence = float(os.getenv("FORMULA_DETECTOR_CONFIDENCE", "0.20"))
        # Region proposals do not need the recognizer's detail level. Keeping this
        # lower improves CPU latency for camera photos; deployments can override it.
        input_size = int(os.getenv("FORMULA_DETECTOR_INPUT_SIZE", "768"))
        with self._detect_lock:
            results = model.predict(
                image,
                imgsz=input_size,
                conf=confidence,
                device="cpu",
                verbose=False,
            )
        result = results[0] if results else None
        candidates: list[dict[str, Any]] = []
        if result is not None and result.boxes is not None:
            boxes = result.boxes.xyxy.cpu().tolist()
            scores = result.boxes.conf.cpu().tolist()
            labels = result.boxes.cls.cpu().int().tolist()
            names = result.names or {}
            for index, (box, score, label) in enumerate(zip(boxes, scores, labels)):
                region = normalized_box(box, image.width, image.height)
                if region["width"] < 20 or region["height"] < 12:
                    continue
                formula_type = str(names.get(label, label)) if isinstance(names, dict) else str(label)
                candidates.append(
                    {
                        "id": f"formula-{index}",
                        "type": formula_type,
                        "label": candidate_label(formula_type, region["top"], image.height),
                        "score": round(float(score), 3),
                        "box": region,
                    }
                )
        candidates.sort(key=lambda item: (item["box"]["top"], item["box"]["left"]))
        return {"imageWidth": image.width, "imageHeight": image.height, "candidates": candidates[:12]}
