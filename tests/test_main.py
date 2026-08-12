from __future__ import annotations

import os
import unittest

os.environ["FORMULA_OCR_PRELOAD"] = "0"

try:
    from fastapi.testclient import TestClient

    from app.main import app
except ImportError:
    TestClient = None
    app = None


@unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
class FormulaServiceTests(unittest.TestCase):
    def test_root_and_cloudbase_probe_are_lightweight(self) -> None:
        with TestClient(app) as client:
            self.assertEqual(client.get("/").json(), {"ok": True})
            self.assertEqual(client.get("/__tcb_probe__").json(), {"ok": True})

    def test_health_reports_model_state(self) -> None:
        with TestClient(app) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertIn("modelLoaded", body)
        self.assertIn("modelLoading", body)
        self.assertIn("modelError", body)


if __name__ == "__main__":
    unittest.main()
