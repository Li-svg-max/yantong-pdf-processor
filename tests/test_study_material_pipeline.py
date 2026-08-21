from __future__ import annotations

import unittest
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.study_material_pipeline import process_study_pdf, split_paragraphs, suggest_blanks


class StudyMaterialPipelineTests(unittest.TestCase):
    def test_paragraph_split_preserves_readable_units(self) -> None:
        value = "第一章 数据结构\n线性表是由有限个数据元素组成的序列。\n\n特点：顺序存储、链式存储。"
        paragraphs = split_paragraphs(value)
        self.assertGreaterEqual(len(paragraphs), 2)
        self.assertIn("线性表", "".join(paragraphs))
        self.assertTrue(all(paragraph.strip() for paragraph in paragraphs))

    def test_suggestions_are_explainable_and_non_overlapping(self) -> None:
        value = "线性表是指由有限个数据元素组成的序列，包括顺序存储、链式存储。"
        blanks = suggest_blanks(value, "block-1")
        self.assertTrue(blanks)
        self.assertTrue(any(item["answer"] == "线性表" for item in blanks))
        self.assertTrue(all(item["reason"] for item in blanks))
        ordered = sorted(blanks, key=lambda item: item["start"])
        self.assertTrue(all(left["end"] <= right["start"] for left, right in zip(ordered, ordered[1:])))

    def test_text_pdf_is_processed_as_full_document_without_question_numbers(self) -> None:
        workspace = Path(__file__).resolve().parents[3] / "tmp" / "study-material-tests"
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / "lecture.pdf"
        output = canvas.Canvas(str(path), pagesize=A4)
        output.setFont("Helvetica", 12)
        output.drawString(50, 780, "Operating systems coordinate hardware and software resources.")
        output.drawString(50, 750, "A process is an executing program with its own state.")
        output.save()
        blocks = process_study_pdf(path, dpi=160)
        self.assertTrue(blocks)
        self.assertIn("Operating systems", " ".join(item["text"] for item in blocks))
        self.assertTrue(all(item["source"] == "pdf_text" for item in blocks))


if __name__ == "__main__":
    unittest.main()
