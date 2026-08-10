from __future__ import annotations

import unittest

from app.normalization import editable_expression


class FormulaNormalizationTests(unittest.TestCase):
    def test_removes_display_wrappers(self) -> None:
        self.assertEqual(editable_expression(r"$y=\frac{x^2}{2}$"), r"\frac{x^2}{2}")

    def test_keeps_radical_structure(self) -> None:
        self.assertEqual(editable_expression(r"\left(\sqrt{x}+1\right)"), r"(\sqrt{x}+1)")


if __name__ == "__main__":
    unittest.main()
