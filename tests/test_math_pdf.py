"""Pure layout/image tests only: no final PDF artifact is authored here."""
from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import patch

import numpy as np
from PIL import Image
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

from app.document_export.math_pdf import (
    MAX_DOCUMENT_FORMULAS,
    MAX_FORMULAS_PER_PARAGRAPH,
    MathParagraph,
    _raster_formula,
    _validate_formula,
    bounded_math_document,
    pdf_math_paragraph,
)
from app.document_export.renderer import _pdf_paragraph


class MathPdfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        cls.style = ParagraphStyle("MathTest", fontName="STSong-Light", fontSize=11, leading=17, wordWrap="CJK")

    def test_sin_limit_is_an_actual_png_with_fraction_and_lower_limit(self):
        png, width, height, depth = _raster_formula(r"\lim_{x\to0}\frac{\sin x}{x}=1", 11)
        self.assertTrue(png.startswith(b"\x89PNG"))
        with Image.open(BytesIO(png)) as image:
            alpha = np.asarray(image.getchannel("A"))
            self.assertGreater(np.count_nonzero(alpha), 100)
            self.assertGreater(image.height, 50)
            self.assertEqual(image.mode, "RGBA")
        self.assertGreater(width, 30)
        self.assertGreater(height, 11)
        self.assertGreater(depth, 0)

    def test_superscripts_subscripts_and_nested_fraction(self):
        for expression in (r"x_i^2+x_{i+1}", r"\frac{1-\cos x}{x^2}", r"\frac{\frac{1}{x}+1}{\sqrt{x+1}}", r"\int_0^1 x^2\,dx=\frac{1}{3}"):
            with self.subTest(expression=expression):
                png, width, height, _ = _raster_formula(expression, 11)
                self.assertGreater(len(png), 100)
                self.assertGreater(width, 1)
                self.assertGreater(height, 1)

    def test_pdf_renderer_replaces_dollar_math_instead_of_printing_control_words(self):
        paragraph = _pdf_paragraph(r"求极限 $\lim_{x\to0}\frac{\sin x}{x}$，并说明思路。", self.style)
        self.assertIsInstance(paragraph, MathParagraph)
        self.assertNotIn(r"\frac", paragraph.text)
        self.assertNotIn(r"\lim", paragraph.text)
        self.assertIn("求极限", paragraph.text)
        self.assertIn("data:image/png;base64,", paragraph.text)
        self.assertEqual(sum(getattr(getattr(fragment, "cbDefn", None), "kind", "") == "img" for fragment in paragraph.frags), 1)
        width, height = paragraph.wrap(220, 700)
        self.assertLessEqual(width, 220)
        self.assertGreater(height, 17)

    def test_inline_and_display_delimiters_preserve_intent(self):
        for source in (r"A $x^2$ B", r"A \(x^2\) B"):
            self.assertNotIn("<br/>", pdf_math_paragraph(source, self.style).text)
        for source in (r"A $$x^2$$ B", r"A \[x^2\] B"):
            self.assertEqual(pdf_math_paragraph(source, self.style).text.count("<br/>"), 2)

    def test_cjk_formula_at_line_boundary_does_not_crash(self):
        source = (r"设函数满足条件 $f'(x)+f^2(x)-\int_a^x f(t)dt=0$，"
                  r"求 $\int_a^b f(t)dt$，请写出完整推导。")
        for width in (210, 280, 450):
            paragraph = pdf_math_paragraph(source * 3, self.style)
            _, height = paragraph.wrap(width, 1000)
            self.assertGreater(height, 17)
            for line in paragraph.blPara.lines:
                self.assertGreaterEqual(line.extraSpace, -1)


    def test_plain_text_stays_escaped_and_never_becomes_remote_image_or_file(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            plain = pdf_math_paragraph('<img src="https://example.com/a.png"/>', self.style)
            self.assertIn("&lt;img", plain.text)
            paragraph = pdf_math_paragraph(r'<img src="C:/private.txt"/> $x^2$', self.style)
            self.assertIn("&lt;img", paragraph.text)
            self.assertEqual(paragraph.text.count('<img src="data:image/png;base64,'), 1)

    def test_forbidden_and_unsupported_commands_fail_explicitly(self):
        for expression in (r"\input{private.txt}", r"\write18{whoami}", r"\includegraphics{https://example.com}", r"\begin{cases}x\end{cases}", r"\notARealCommand{x}"):
            with self.subTest(expression=expression), self.assertRaisesRegex(ValueError, "打印|公式"):
                _raster_formula(expression, 11)

    def test_lengths_nesting_and_size_are_bounded_before_raster(self):
        for expression in ("x" * 1025, "{" * 13 + "x" + "}" * 13, "{x", "x}" ):
            with self.subTest(expression=expression[:20]), self.assertRaises(ValueError):
                _validate_formula(expression)
        with self.assertRaisesRegex(ValueError, "尺寸"):
            _raster_formula("x+" * 400 + "x", 11)
        with self.assertRaises(ValueError):
            _raster_formula("x", 100)

    def test_narrow_columns_fail_instead_of_clipping_or_silently_resizing(self):
        paragraph = pdf_math_paragraph(r"$\lim_{x\to0}\frac{\sin x}{x}=1$", self.style)
        with self.assertRaisesRegex(ValueError, "单栏"):
            paragraph.wrap(10, 200)

    def test_unbalanced_formula_delimiter_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "分隔符"):
            pdf_math_paragraph(r"求 $\frac{1}{x}", self.style)
        for value in (r"先看 $x^2$ 再看 $\frac{1}{x}", r"$x$ 以及 \[\frac{1}{2}"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "分隔符"):
                pdf_math_paragraph(value, self.style)
        self.assertIn("$5", pdf_math_paragraph("价格 $5", self.style).text)

    def test_per_paragraph_and_per_document_limits_and_bounded_cache(self):
        with self.assertRaisesRegex(ValueError, "64"):
            pdf_math_paragraph(" $x$ " * (MAX_FORMULAS_PER_PARAGRAPH + 1), self.style)

        @bounded_math_document
        def over_budget():
            for _ in range(MAX_DOCUMENT_FORMULAS + 1):
                pdf_math_paragraph("$x$", self.style)

        with self.assertRaisesRegex(ValueError, "分批"):
            over_budget()
        self.assertEqual(_raster_formula.cache_info().maxsize, 64)

    def test_never_uses_external_tex_even_when_global_setting_is_true(self):
        import matplotlib
        _raster_formula.cache_clear()
        with matplotlib.rc_context({"text.usetex": True}), patch("matplotlib.texmanager.TexManager.get_text_width_height_descent", side_effect=AssertionError("external TeX forbidden")):
            png, _, _, _ = _raster_formula(r"\frac{7}{x^2}", 11)
            self.assertTrue(png)
            self.assertTrue(matplotlib.rcParams["text.usetex"])


if __name__ == "__main__":
    unittest.main()
