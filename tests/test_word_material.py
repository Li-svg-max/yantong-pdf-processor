import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.word_material import read_word_paragraphs
from app.models import WordJobRequest, SignedWordJobRequest, PdfJobRequest

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


class WordImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'fixture.docx'

    def document(self, body, extra=None):
        xml = f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
        with zipfile.ZipFile(self.file, 'w') as z:
            z.writestr('[Content_Types].xml', '<Types/>')
            z.writestr('word/document.xml', xml)
            for k, v in (extra or {}).items():
                z.writestr(k, v)
        return self.file

    def test_chinese_paragraphs_tables_hyperlinks(self):
        source = self.document('<w:p><w:r><w:t>GIS 工程</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>需求分析</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>系统设计</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '<w:p><w:hyperlink><w:r><w:t>系统实施</w:t></w:r></w:hyperlink></w:p>')
        self.assertEqual(read_word_paragraphs(source), ['GIS 工程', '需求分析', '系统设计', '系统实施'])

    def test_revisions_and_line_breaks(self):
        source = self.document('<w:p><w:del><w:r><w:t>删除</w:t></w:r></w:del>'
            '<w:ins><w:r><w:t>保留</w:t><w:tab/><w:t>内容</w:t><w:br/><w:t>下一行</w:t></w:r></w:ins></w:p>')
        self.assertEqual(read_word_paragraphs(source), ['保留\t内容\n下一行'])

    def test_rejects_silent_content_loss(self):
        for tag in ['drawing', 'pict', 'object', 'altChunk', 'footnoteReference', 'txbxContent']:
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, 'PDF'):
                read_word_paragraphs(self.document(f'<w:p><w:{tag}/></w:p>'))

    def test_equations_require_pdf(self):
        with self.assertRaisesRegex(ValueError, 'PDF'):
            read_word_paragraphs(self.document('<w:p><m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"/></w:p>'))

    def test_macro_and_embedded_object_rejected(self):
        for name in ['word/vbaProject.bin', 'word/embeddings/object.bin']:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, '宏'):
                read_word_paragraphs(self.document('<w:p/>', {name: b'x'}))

    def test_empty_broken_and_old_word(self):
        with self.assertRaisesRegex(ValueError, '没有可读取'):
            read_word_paragraphs(self.document('<w:p/>'))
        self.file.write_bytes(b'not zip')
        with self.assertRaises(ValueError):
            read_word_paragraphs(self.file)
        with self.assertRaisesRegex(ValueError, '另存'):
            read_word_paragraphs(self.file.with_suffix('.doc'))

    def test_entities_forbidden(self):
        with zipfile.ZipFile(self.file, 'w') as z:
            z.writestr('[Content_Types].xml', '<Types/>')
            z.writestr('word/document.xml', '<!DOCTYPE a [<!ENTITY b SYSTEM "file:///secret">]><a>&b;</a>')
        with self.assertRaisesRegex(ValueError, 'XML'):
            read_word_paragraphs(self.file)

    def test_ticket_limits_word_to_cloze(self):
        payload = dict(inputKind='docx', jobId='word-test', sourceFileID='cloud://test/private/source.docx',
            storageOwnerKey='a'*32, title='资料', subjectId='professional', subjectName='GIS', workflowType='cloze_document')
        word = WordJobRequest(**payload)
        self.assertEqual(SignedWordJobRequest(job=word, expiresAt=2000000000000, signature='a'*64).job.inputKind, 'docx')
        with self.assertRaises(ValueError):
            WordJobRequest(**{**payload, 'workflowType': 'question_import'})
        with self.assertRaises(ValueError):
            PdfJobRequest(**payload)


if __name__ == '__main__':
    unittest.main()
