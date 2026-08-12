from __future__ import annotations

import threading
import time
import unittest

from app.engine import FormulaEngine


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


if __name__ == "__main__":
    unittest.main()
