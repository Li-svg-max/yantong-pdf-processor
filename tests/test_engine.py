from __future__ import annotations

import threading
import time
import unittest
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from app.engine import FormulaEngine, FormulaModelLoadingError


class StubFormulaEngine(FormulaEngine):
    def __init__(self) -> None:
        super().__init__()
        self.load_started = threading.Event()
        self.allow_load_to_finish = threading.Event()

    def load(self):
        self.load_started.set()
        self.allow_load_to_finish.wait(timeout=1)
        return object()


class FormulaEngineTests(unittest.TestCase):
    def test_preload_does_not_block_service_startup(self) -> None:
        engine = StubFormulaEngine()

        started_at = time.monotonic()
        engine.preload()

        self.assertLess(time.monotonic() - started_at, 0.1)
        self.assertTrue(engine.load_started.wait(timeout=0.5))
        engine.allow_load_to_finish.set()

    def test_preload_is_idempotent_while_loading(self) -> None:
        engine = StubFormulaEngine()

        engine.preload()
        self.assertTrue(engine.load_started.wait(timeout=0.5))
        engine.preload()

        self.assertTrue(engine.loading)
        engine.allow_load_to_finish.set()

    def test_recognize_uses_formula_only_onnx_engine(self) -> None:
        class PixelValues:
            def to(self, device):
                self.device = device
                return self

        class Processor:
            def __call__(self, images, return_tensors):
                self.images = images
                self.return_tensors = return_tensors
                return SimpleNamespace(pixel_values=PixelValues())

            def batch_decode(self, generated_ids, skip_special_tokens):
                self.generated_ids = generated_ids
                self.skip_special_tokens = skip_special_tokens
                return [r"$y=\\frac{x^2}{2}$"]

        class Model:
            def generate(self, pixel_values, max_new_tokens):
                self.pixel_values = pixel_values
                self.max_new_tokens = max_new_tokens
                return [1, 2, 3]

        image = Image.new("RGB", (32, 32), "white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")

        processor = Processor()
        model = Model()
        engine = FormulaEngine()
        engine._engine = {
            "model": model,
            "processor": processor,
            "modelId": "breezedeus/pix2text-mfr-1.5",
        }

        result = engine.recognize(buffer.getvalue())

        self.assertEqual(result["latex"], r"$y=\\frac{x^2}{2}$")
        self.assertEqual(result["editableExpression"], r"\\frac{x^2}{2}")
        self.assertEqual(result["engine"], "pix2text-mfr-onnx")
        self.assertEqual(result["model"], "breezedeus/pix2text-mfr-1.5")
        self.assertEqual(processor.return_tensors, "pt")
        self.assertEqual(model.max_new_tokens, 512)

    def test_recognize_starts_background_load_without_blocking_request(self) -> None:
        engine = StubFormulaEngine()
        image = Image.new("RGB", (32, 32), "white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")

        started_at = time.monotonic()
        with self.assertRaises(FormulaModelLoadingError):
            engine.recognize(buffer.getvalue())

        self.assertLess(time.monotonic() - started_at, 0.1)
        self.assertTrue(engine.load_started.wait(timeout=0.5))
        engine.allow_load_to_finish.set()


if __name__ == "__main__":
    unittest.main()
