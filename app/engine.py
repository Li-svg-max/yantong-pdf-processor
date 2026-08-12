from __future__ import annotations

import os
import threading
import traceback
from io import BytesIO

from PIL import Image

from .normalization import editable_expression


class FormulaModelLoadingError(RuntimeError):
    """The model is being downloaded or initialized in the background."""


class FormulaEngine:
    def __init__(self) -> None:
        self._engine = None
        self._load_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._recognize_lock = threading.Lock()
        self._loading = False
        self._load_error = ""

    @property
    def loaded(self) -> bool:
        return self._engine is not None

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
            if self._engine is not None or self._loading:
                return
            self._loading = True
        threading.Thread(target=self._run_preload, name="formula-model-preload", daemon=True).start()

    def _run_preload(self) -> None:
        try:
            self.load()
        except Exception:
            traceback.print_exc()

    def load(self):
        if self._engine is not None:
            return self._engine
        with self._load_lock:
            if self._engine is not None:
                return self._engine
            with self._state_lock:
                self._loading = True
                self._load_error = ""
            try:
                from optimum.onnxruntime import ORTModelForVision2Seq
                from transformers import TrOCRProcessor

                model_id = os.getenv("FORMULA_OCR_MODEL", "/opt/formula-model")
                device = os.getenv("FORMULA_OCR_DEVICE", "cpu")
                processor = TrOCRProcessor.from_pretrained(model_id, local_files_only=True)
                model = ORTModelForVision2Seq.from_pretrained(
                    model_id,
                    provider="CPUExecutionProvider",
                    use_cache=False,
                    use_merged=False,
                    encoder_file_name="encoder_model.onnx",
                    decoder_file_name="decoder_model.onnx",
                    local_files_only=True,
                )
                model.to(device)
                self._engine = {"model": model, "processor": processor, "modelId": model_id}
            except Exception as error:
                with self._state_lock:
                    self._load_error = f"{type(error).__name__}: {error}"[:500]
                raise
            finally:
                with self._state_lock:
                    self._loading = False
        return self._engine

    def require_loaded(self):
        if self._engine is not None:
            return self._engine
        self.preload()
        raise FormulaModelLoadingError("formula model is loading")

    def recognize(self, image_bytes: bytes) -> dict[str, str]:
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        if image.width < 16 or image.height < 16:
            raise ValueError("image is too small")
        if image.width * image.height > 20_000_000:
            raise ValueError("image dimensions are too large")
        engine = self.require_loaded()
        with self._recognize_lock:
            processor = engine["processor"]
            model = engine["model"]
            pixel_values = processor(images=[image], return_tensors="pt").pixel_values
            generated_ids = model.generate(
                pixel_values.to(os.getenv("FORMULA_OCR_DEVICE", "cpu")),
                max_new_tokens=int(os.getenv("FORMULA_OCR_MAX_NEW_TOKENS", "512")),
            )
            result = processor.batch_decode(generated_ids, skip_special_tokens=True)
        latex = str(result[0] if result else "").strip()
        if not latex:
            raise ValueError("no formula detected")
        return {
            "latex": latex,
            "editableExpression": editable_expression(latex),
            "engine": "pix2text-mfr-onnx",
            "model": "breezedeus/pix2text-mfr-1.5",
        }
