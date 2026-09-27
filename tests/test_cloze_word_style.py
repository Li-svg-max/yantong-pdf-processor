"""Offline typography checks; no model calls or private source material."""
import base64
import hashlib
import hmac
import json
import unittest
from io import BytesIO
from zipfile import ZipFile

from docx import Document
from app.document_export.models import ClozeExportRequest
from app.document_export.renderer import render_cloze_export
from app.request_auth import sign_ticket


def request_data(family='SimSun', size=12, show_title=False):
    return dict(format='docx', variant='combined', title='排版测试', courseName='测试',
                wordStyle=dict(fontFamily=family, fontSize=size, showTitle=show_title),
                blocks=[dict(id='one', page=1, order=1, text='概念是原文中的重点。\n保留换行。',
                             blanks=[dict(id='blank', start=0, end=2, answer='概念', enabled=True)])])


class ClozeWordStyleTests(unittest.TestCase):
    def test_all_fonts_and_sizes_are_applied_to_actual_runs(self):
        for family, east in [('SimSun', '宋体'), ('SimHei', '黑体'), ('KaiTi', '楷体'), ('Microsoft YaHei', '微软雅黑')]:
            for size in [10.5, 12, 14, 16, 18]:
                with self.subTest(family=family, size=size):
                    req = ClozeExportRequest(**request_data(family, size))
                    data = base64.b64decode(render_cloze_export(req)[0]['contentBase64'])
                    doc = Document(BytesIO(data))
                    self.assertEqual(doc.paragraphs[0].runs[0].font.size.pt, size)
                    self.assertEqual(doc.paragraphs[0].runs[0].font.name, family)
                    self.assertEqual(doc.paragraphs[-1].runs[0].font.size.pt, size)
                    self.assertNotIn('排版测试', '\n'.join(p.text for p in doc.paragraphs))
                    self.assertFalse(doc.tables)
                    with ZipFile(BytesIO(data)) as archive:
                        self.assertIn(f'w:eastAsia="{east}"', archive.read('word/document.xml').decode())

    def test_typography_is_signed_without_float_or_default_drift(self):
        for size in [10.5, 12, 14, 16, 18]:
            data = request_data(size=size)
            payload = json.dumps(dict(expiresAt=2000000300000, request=data), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            signature = hmac.new(b'test-token', payload, hashlib.sha256).hexdigest()
            self.assertEqual(sign_ticket(ClozeExportRequest(**data), 2000000300000, 'test-token', 'request'), signature)

    def test_native_formula_is_preserved_and_raw_latex_is_not_printed(self):
        data = request_data()
        data['blocks'][0]['text'] += r' 公式 $\frac{1}{2}+x^2$。'
        file = render_cloze_export(ClozeExportRequest(**data))[0]
        with ZipFile(BytesIO(base64.b64decode(file['contentBase64']))) as archive:
            xml = archive.read('word/document.xml').decode()
            self.assertIn('<m:oMath>', xml)
            self.assertIn('<m:f>', xml)
            self.assertNotIn('\\frac', xml)


if __name__ == '__main__':
    unittest.main()
