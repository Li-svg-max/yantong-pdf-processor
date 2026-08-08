from __future__ import annotations

import base64
import unittest
from io import BytesIO

from docx import Document
from pypdf import PdfReader

from app.document_export.models import ExportRequest
from app.document_export.renderer import render_export


def build_request(format_name: str, package_mode: str = "combined") -> ExportRequest:
    return ExportRequest(
        format=format_name,
        title="数学二错题集",
        settings={
            "paper": "A4",
            "columns": 2,
            "fontSize": 11,
            "blankLines": 1,
            "packageMode": package_mode,
            "solutionMode": "end",
            "includeAnalysis": True,
        },
        groups=[{
            "order": 1,
            "title": "函数极限",
            "meta": "数学二 · 极限与连续",
            "questions": [{
                "number": "1",
                "stem": "求函数在 x=0 处的极限。",
                "options": ["A. 0", "B. 1"],
                "answer": "B",
                "analysis": "使用等价无穷小。",
            }],
        }],
    )


class DocumentExportTests(unittest.TestCase):
    def test_pdf_is_readable(self) -> None:
        files = render_export(build_request("pdf"))
        reader = PdfReader(BytesIO(base64.b64decode(files[0]["contentBase64"])))
        self.assertGreaterEqual(len(reader.pages), 2)

    def test_separate_docx_returns_two_readable_files(self) -> None:
        files = render_export(build_request("docx", "separate"))
        self.assertEqual(len(files), 2)
        for item in files:
            self.assertTrue(Document(BytesIO(base64.b64decode(item["contentBase64"]))).paragraphs)


if __name__ == "__main__":
    unittest.main()
