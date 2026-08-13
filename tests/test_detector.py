from __future__ import annotations

import unittest

from PIL import Image

from app.detector import candidate_label, crop_region, normalized_box


class FormulaDetectorGeometryTests(unittest.TestCase):
    def test_normalized_box_adds_margin_and_stays_inside_image(self) -> None:
        box = normalized_box([2, 3, 96, 42], 100, 60)
        self.assertEqual(box["left"], 0)
        self.assertEqual(box["top"], 0)
        self.assertLessEqual(box["left"] + box["width"], 100)
        self.assertLessEqual(box["top"] + box["height"], 60)

    def test_crop_region_rejects_tiny_selection(self) -> None:
        with self.assertRaises(ValueError):
            crop_region(Image.new("RGB", (100, 100)), {"left": 0, "top": 0, "width": 10, "height": 10})

    def test_candidate_label_describes_position_and_formula_kind(self) -> None:
        self.assertEqual(candidate_label("isolated", 5, 100), "顶部行间公式")
        self.assertEqual(candidate_label("embedding", 95, 100), "底部行内公式")


if __name__ == "__main__":
    unittest.main()
