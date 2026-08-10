from __future__ import annotations

import os
import threading
from io import BytesIO

from PIL import Image

from .normalization import editable_expression


class FormulaEngine:
    def __init__(self) -> None:
        self._engine = None
        self._load_lock = threading.Lock()
        self._recognize_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    def load(self):
        if self._engine is not None:
            return self._engine
        with self._load_lock:
            if self._engine is not None:
                return self._engine
            from pix2text.text_formula_ocr import TextFormulaOCR

            self._engine = TextFormulaOCR.from_config(
                enable_formula=True,
                enable_spell_checker=False,
                device=os.getenv("FORMULA_OCR_DEVICE", "cpu"),
            )
        return self._engine

    def recognize(self, image_bytes: bytes) -> dict[str, str]:
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        if image.width < 16 or image.height < 16:
            raise ValueError("image is too small")
        if image.width * image.height > 20_000_000:
            raise ValueError("image dimensions are too large")
        engine = self.load()
        with self._recognize_lock:
            result = engine.recognize(
                image,
                return_text=True,
                contain_formula=True,
                resized_shape=int(os.getenv("FORMULA_OCR_RESIZED_SHAPE", "900")),
                auto_line_break=False,
            )
        latex = str(result or "").strip()
        if not latex:
            raise ValueError("no formula detected")
        return {
            "latex": latex,
            "editableExpression": editable_expression(latex),
            "engine": "pix2text-text-formula-ocr",
        }
