from __future__ import annotations

import base64
import re
import unittest
from io import BytesIO
from zipfile import ZipFile

from docx import Document
from pydantic import ValidationError
from pypdf import PdfReader

from app.document_export.models import (
    ClozeExportRequest,
    ExportRequest,
    SignedClozeExportRequest,
)
from app.document_export.renderer import render_cloze_export, render_export
from app.request_auth import sign_ticket, ticket_validation_error


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


def build_cloze(variant="combined", format_name="docx", repeats=1):
    # Deliberately synthetic unit-test text, never presented as course material.
    blocks = []
    for index in range(repeats):
        first, second = f"ALPHA_SECRET_{index}", f"BETA_SECRET_{index}"
        text = f"排版测试中，{first}用于第一个空；{second}用于第二个空。保留词不挖空。"
        blocks.append({
            "id": f"block-{index}", "page": index + 1, "order": index + 1, "text": text,
            "blanks": [
                {"id": f"b-{index}", "start": text.index(second), "end": text.index(second) + len(second), "answer": second},
                {"id": f"a-{index}", "start": text.index(first), "end": text.index(first) + len(first), "answer": first},
                {"id": f"disabled-{index}", "start": text.index("保留词"), "end": text.index("保留词") + 3, "answer": "保留词", "enabled": False},
            ],
        })
    return ClozeExportRequest(title="中文打印排版测试", courseName="排版测试", variant=variant, format=format_name, blocks=blocks)


def docx_parts(file):
    raw = base64.b64decode(file["contentBase64"])
    with ZipFile(BytesIO(raw)) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".xml")}


class DocumentExportTests(unittest.TestCase):
    def test_cloze_all_six_format_variants_have_unambiguous_file_metadata(self):
        for format_name in ("docx", "pdf"):
            for variant in ("practice", "answers", "combined"):
                with self.subTest(format=format_name, variant=variant):
                    files = render_cloze_export(build_cloze(variant, format_name))
                    self.assertEqual(len(files), 1)
                    file = files[0]
                    self.assertEqual(file["variant"], variant)
                    self.assertEqual(file["format"], format_name)
                    self.assertTrue(file["name"].endswith("." + format_name))
                    self.assertEqual(file["size"], len(base64.b64decode(file["contentBase64"])))

    def test_practice_docx_contains_no_answer_in_any_xml_part(self):
        file = render_cloze_export(build_cloze("practice"))[0]
        parts = docx_parts(file)
        for name, content in parts.items():
            self.assertNotIn("ALPHA_SECRET", content, name)
            self.assertNotIn("BETA_SECRET", content, name)
        self.assertIn("保留词", parts["word/document.xml"])
        self.assertIn("〔1〕", parts["word/document.xml"])
        self.assertIn("〔2〕", parts["word/document.xml"])
        self.assertNotIn("<w:tbl>", parts["word/document.xml"])
        for extra in ('补充：', '姓名：', '请按原文顺序', '第 1 段', '处填空', '题号在练习版'):
            self.assertNotIn(extra, parts['word/document.xml'])

    def test_answer_only_docx_has_no_exercise_section_and_stable_blank_order(self):
        request = build_cloze("answers", repeats=2)
        request.blocks.reverse()
        file = render_cloze_export(request)[0]
        doc = Document(BytesIO(base64.b64decode(file["contentBase64"])))
        text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
        self.assertNotIn("姓名：", text)
        self.assertNotIn("补充：", text)
        self.assertFalse(doc.tables)
        self.assertEqual([p.text for p in doc.paragraphs[1:]], ['〔1〕ALPHA_SECRET_0','〔2〕BETA_SECRET_0','〔3〕ALPHA_SECRET_1','〔4〕BETA_SECRET_1'])

    def test_docx_print_layout_has_clean_title_page_number_and_answer_page(self):
        file = render_cloze_export(build_cloze("combined"))[0]
        parts = docx_parts(file)
        doc = Document(BytesIO(base64.b64decode(file["contentBase64"])))
        self.assertEqual(doc.paragraphs[0].style.name, "Title")
        self.assertEqual(str(doc.paragraphs[0].runs[0].font.color.rgb), "000000")
        self.assertIn("w:pageBreakBefore", parts["word/document.xml"])
        self.assertFalse(doc.tables)
        self.assertIn('w:instr="PAGE"', parts['word/footer1.xml'])
        self.assertNotIn('练习与答案合集', parts['word/footer1.xml'])

    def test_pdf_practice_has_readable_chinese_footer_but_no_answers(self):
        file = render_cloze_export(build_cloze("practice", "pdf", 28))[0]
        reader = PdfReader(BytesIO(base64.b64decode(file["contentBase64"])))
        self.assertGreater(len(reader.pages), 1)
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text()
            self.assertNotIn("ALPHA_SECRET", text)
            self.assertNotIn("BETA_SECRET", text)
            self.assertIn(f"第 {number} 页 / 共 {len(reader.pages)} 页", text)
            self.assertEqual(text.count("补充："), len(re.findall(r"第\s+\d+\s+段", text)), "Short paragraph and handwriting area must stay on the same page")
        self.assertIn("中文打印排版测试", reader.pages[0].extract_text())

    def test_pdf_combined_starts_answer_section_on_a_new_page(self):
        file = render_cloze_export(build_cloze("combined", "pdf", 2))[0]
        reader = PdfReader(BytesIO(base64.b64decode(file["contentBase64"])))
        self.assertGreaterEqual(len(reader.pages), 2)
        first = reader.pages[0].extract_text()
        self.assertNotIn("ALPHA_SECRET", first)
        self.assertIn("答案与核对", reader.pages[-1].extract_text())
        self.assertIn("ALPHA_SECRET_0", reader.pages[-1].extract_text())

    def test_long_pdf_paragraph_is_allowed_to_split_without_layout_error(self):
        text = "用于验证长段落自然分页的中文说明。" * 250 + "LONG_END_ANSWER。"
        start = text.index("LONG_END_ANSWER")
        request = ClozeExportRequest(format="pdf", variant="practice", blocks=[{
            "id": "long", "text": text, "blanks": [{"id": "end", "start": start, "end": start + 15, "answer": "LONG_END_ANSWER"}],
        }])
        file = render_cloze_export(request)[0]
        reader = PdfReader(BytesIO(base64.b64decode(file["contentBase64"])))
        self.assertGreater(len(reader.pages), 2)
        self.assertNotIn("LONG_END_ANSWER", "\n".join(page.extract_text() for page in reader.pages))

    def test_utf16_browser_offsets_are_resolved_without_cutting_surrogates(self):
        request = ClozeExportRequest(variant="practice", blocks=[{
            "id": "astral", "text": "🧭前导文字测试词后续文字", "blanks": [{"id": "blank", "start": 6, "end": 9, "answer": "测试词"}],
        }])
        parts = docx_parts(render_cloze_export(request)[0])
        self.assertIn("🧭前导文字", parts["word/document.xml"])
        self.assertNotIn("测试词", parts["word/document.xml"])
        request.blocks[0].blanks[0].start = 1
        with self.assertRaises(ValueError):
            render_cloze_export(request)

    def test_invalid_blank_ranges_overlap_and_zero_enabled_blanks_fail_closed(self):
        for failure in ("mismatch", "overlap", "no-enabled", "backwards"):
            request = build_cloze("practice")
            if failure == "mismatch": request.blocks[0].blanks[0].answer = "不是该范围的文字"
            if failure == "overlap": request.blocks[0].blanks.append(request.blocks[0].blanks[0].model_copy(update={"id": "duplicate-range"}))
            if failure == "no-enabled":
                for blank in request.blocks[0].blanks: blank.enabled = False
            if failure == "backwards": request.blocks[0].blanks[0].end = request.blocks[0].blanks[0].start
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                render_cloze_export(request)

    def test_new_format_and_variant_are_signed_and_cannot_be_downgraded(self):
        request = build_cloze("practice", "pdf")
        now = 2_000_000_000_000
        signature = sign_ticket(request, now + 300_000, "test-processor-token", payload_key="request")
        self.assertIsNone(ticket_validation_error(request, now + 300_000, signature, "test-processor-token", now, payload_key="request"))
        for update in ({"variant": "answers"}, {"variant": "combined"}, {"format": "docx"}):
            tampered = request.model_copy(update=update)
            self.assertEqual(ticket_validation_error(tampered, now + 300_000, signature, "test-processor-token", now, payload_key="request"), "job ticket signature does not match")
        self.assertEqual(request.model_dump()["variant"], "practice")
        for update in ({"variant": "unknown"}, {"format": "html"}, {"variant": None}):
            with self.assertRaises(ValidationError):
                ClozeExportRequest.model_validate({**request.model_dump(), **update})

    def test_pdf_is_readable(self) -> None:
        files = render_export(build_request("pdf"))
        reader = PdfReader(BytesIO(base64.b64decode(files[0]["contentBase64"])))
        self.assertGreaterEqual(len(reader.pages), 2)

    def test_separate_docx_returns_two_readable_files(self) -> None:
        files = render_export(build_request("docx", "separate"))
        self.assertEqual(len(files), 2)
        for item in files:
            self.assertTrue(Document(BytesIO(base64.b64decode(item["contentBase64"]))).paragraphs)

    def test_cloze_docx_contains_exercise_and_clean_answers(self) -> None:
        request = ClozeExportRequest(
            title="数据结构第三章",
            courseName="数据结构",
            blocks=[{
                "id": "block-1",
                "page": 2,
                "order": 1,
                "text": "线性表是由有限个数据元素组成的序列。",
                "blanks": [{
                    "id": "blank-1",
                    "start": 0,
                    "end": 3,
                    "answer": "线性表",
                    "enabled": True,
                }],
            }],
        )
        files = render_cloze_export(request)
        self.assertEqual(len(files), 1)
        document = Document(BytesIO(base64.b64decode(files[0]["contentBase64"])))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        self.assertIn("〔1〕", text)
        self.assertIn("答案", text)
        self.assertIn('〔1〕线性表', text)
        self.assertFalse(document.tables)

    def test_cloze_export_requires_a_short_lived_unchanged_ticket(self) -> None:
        request = ClozeExportRequest(
            title="操作系统复习",
            courseName="操作系统",
            blocks=[{
                "id": "block-1",
                "page": 1,
                "order": 1,
                "text": "进程是资源分配的基本单位。",
                "blanks": [{
                    "id": "blank-1",
                    "start": 0,
                    "end": 2,
                    "answer": "进程",
                    "enabled": True,
                }],
            }],
        )
        now_ms = 2_000_000_000_000
        expires_at = now_ms + 300_000
        token = "test-processor-token"
        signature = sign_ticket(
            request,
            expires_at,
            token,
            payload_key="request",
        )
        self.assertEqual(
            signature,
            "2e86f75fd2e6bda8db16fb10961301c0f75058177df135b41e75350defda038e",
        )

        self.assertIsNone(
            ticket_validation_error(
                request,
                expires_at,
                signature,
                token,
                now_ms,
                payload_key="request",
            )
        )
        self.assertEqual(
            ticket_validation_error(
                request,
                now_ms - 1,
                signature,
                token,
                now_ms,
                payload_key="request",
            ),
            "job ticket expired",
        )
        modified = request.model_copy(update={"title": "被篡改的标题"})
        self.assertEqual(
            ticket_validation_error(
                modified,
                expires_at,
                signature,
                token,
                now_ms,
                payload_key="request",
            ),
            "job ticket signature does not match",
        )
        with self.assertRaises(ValidationError):
            SignedClozeExportRequest(
                request=request,
                expiresAt=expires_at,
                signature="",
            )


if __name__ == "__main__":
    unittest.main()
