import unittest
from lxml import etree
from app.document_export.math_pdf import _raster_formula, _canonical_arguments, pdf_math_paragraph
from reportlab.lib.styles import ParagraphStyle
from app.document_export.math_docx import equation

class MatrixTests(unittest.TestCase):
    def test_bracing_aliases_and_tall_first_line(self):
        self.assertEqual(_canonical_arguments(r'\frac1{\sqrt2}+\frac12'), r'\frac{1}{\sqrt{2}}+\frac{1}{2}')
        self.assertEqual(_canonical_arguments(r'x\le2'), r'x\leq 2')
        p = pdf_math_paragraph(r'Q=$\begin{pmatrix}1&2\\3&4\\5&6\end{pmatrix}$', ParagraphStyle('test', fontSize=11, leading=17, autoLeading='max', wordWrap='CJK'))
        p.wrap(450, 700)
        self.assertGreater(p.blPara.lines[0].ascent, 11)
        self.assertGreater(p.height, 30)

    def test_common_matrices_and_adjacent_math(self):
        for env in ('matrix', 'pmatrix', 'bmatrix', 'vmatrix'):
            formula = r'A=\begin{' + env + r'}1&-\frac1{\sqrt2}\\0&3\end{' + env + '}'
            png, width, height, depth = _raster_formula(formula, 11)
            self.assertGreater(len(png), 200)
            self.assertGreater(height, 25)
            self.assertGreater(width, 20)
            self.assertGreater(depth, 0)
            xml = etree.tostring(equation(formula), encoding='unicode')
            self.assertIn('<m:m>', xml)
            self.assertEqual(xml.count('<m:mr>'), 2)
            if env != 'matrix': self.assertIn('<m:d>', xml)
            self.assertNotIn(r'\begin', xml)

    def test_malformed_oversized_nested_and_unsafe_are_rejected(self):
        cases = [
            r'\begin{pmatrix}1&2\\3\end{pmatrix}',
            r'\begin{pmatrix}1\end{bmatrix}',
            r'\begin{pmatrix}\input{x}\end{pmatrix}',
            r'\begin{pmatrix}\begin{matrix}1\end{matrix}\end{pmatrix}',
            r'\frac{\begin{pmatrix}1\end{pmatrix}}{2}',
            r'\begin{array}{cc}1&2\end{array}',
            r'\begin{pmatrix}' + '&'.join(['1'] * 9) + r'\end{pmatrix}',
            r'\begin{pmatrix}' + r'\\'.join(['1'] * 9) + r'\end{pmatrix}',
        ]
        for formula in cases:
            with self.subTest(formula=formula):
                with self.assertRaises(ValueError): _raster_formula(formula, 11)
                with self.assertRaises(ValueError): equation(formula)

if __name__ == '__main__': unittest.main()
