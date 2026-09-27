import unittest
from lxml import etree
from docx import Document
from app.document_export.math_docx import append_math_text, equation

class MathDocxTests(unittest.TestCase):
    def test_fraction_subscript_and_integral_are_native_math(self):
        for value in (r'\frac{4a}{3\pi}', r'\int_a^b f(t)dt=0', r'x_i^2', r'\sqrt{x+1}'):
            node = equation(value)
            self.assertEqual(node.tag.rsplit('}',1)[-1], 'oMath')
            xml = etree.tostring(node, encoding='unicode')
            self.assertNotIn('\\frac', xml)
            self.assertNotIn('pic:pic', xml)
        self.assertIn('<m:f>', etree.tostring(equation(r'\frac{1}{2}'), encoding='unicode'))

    def test_surrounding_text_is_not_lost(self):
        paragraph = Document().add_paragraph()
        append_math_text(paragraph, r'题目 $x^2$，答案 $\frac{1}{2}$。', lambda run: None)
        self.assertEqual(paragraph.text, '题目 ，答案 。')
        self.assertEqual(len(paragraph._p.xpath('./m:oMath')), 2)

    def test_forbidden_input_fails(self):
        with self.assertRaises(ValueError): equation(r'\input{private}')
