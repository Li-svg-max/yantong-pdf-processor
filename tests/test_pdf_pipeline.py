from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.cloud_client import CloudClient, CloudSettings
from app.models import PdfJobRequest
from app.pdf_pipeline import Marker, PipelineOptions, process_pdf
from app.queue_store import QueueStore


ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / "tmp" / "pdfs" / "pdf-processor-tests"


def build_text_pdf(path: Path) -> None:
    width, height = A4
    document = canvas.Canvas(str(path), pagesize=A4)
    document.setFont("Helvetica", 13)
    document.drawString(42, height - 92, "1. Evaluate the first limit expression")
    document.drawString(68, height - 122, "Keep the complete formula line and all options.")
    document.drawString(42, height - 330, "2. Find the derivative on the interval")
    document.drawString(68, height - 360, "This question continues on the next page.")
    document.showPage()
    document.setFont("Helvetica", 13)
    document.drawString(68, height - 70, "Continuation of question two with a displayed expression.")
    document.drawString(42, height - 260, "3. Determine the convergence of the series")
    document.drawString(68, height - 290, "Use a justified convergence test.")
    document.save()


def build_scan_pdf(path: Path) -> None:
    page = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(page)
    draw.text((70, 170), "1. scanned question content", fill="black")
    draw.rectangle((80, 250, 1050, 430), outline="black", width=3)
    draw.text((70, 850), "2. another scanned question", fill="black")
    draw.ellipse((180, 950, 900, 1250), outline="black", width=3)
    document = canvas.Canvas(str(path), pagesize=A4)
    document.drawImage(ImageReader(page), 0, 0, width=A4[0], height=A4[1])
    document.save()


def build_contact_sheet(paths: list[Path], destination: Path) -> None:
    images = [Image.open(path).convert("RGB") for path in paths]
    target_width = 900
    scaled = [
        image.resize(
            (target_width, max(1, round(image.height * target_width / image.width))),
            Image.Resampling.LANCZOS,
        )
        for image in images
    ]
    sheet = Image.new("RGB", (target_width, sum(image.height for image in scaled) + 30), "white")
    y = 10
    for image in scaled:
        sheet.paste(image, (0, y))
        y += image.height + 10
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, quality=90)


class PdfPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if WORK.exists():
            shutil.rmtree(WORK)
        WORK.mkdir(parents=True)

    def test_text_layer_detection_and_vertical_only_crop(self) -> None:
        source = WORK / "text-source.pdf"
        output = WORK / "text-output"
        build_text_pdf(source)
        questions = process_pdf(source, output, PipelineOptions(dpi=180))
        self.assertEqual([item.number_label for item in questions], ["1", "2", "3"])
        self.assertIn("first limit", questions[0].summary)
        self.assertEqual(questions[1].source_pages, (1, 2))
        widths = set()
        for question in questions:
            for path in question.image_paths:
                with Image.open(path) as image:
                    widths.add(image.width)
        self.assertEqual(len(widths), 1, "question crops must retain one fixed page width")
        build_contact_sheet(
            [path for question in questions for path in question.image_paths],
            WORK / "text-crops-contact-sheet.jpg",
        )

    def test_scan_pdf_uses_injected_number_detector(self) -> None:
        source = WORK / "scan-source.pdf"
        output = WORK / "scan-output"
        build_scan_pdf(source)

        def fake_ocr(_: Image.Image, page_index: int) -> list[Marker]:
            self.assertEqual(page_index, 0)
            return [
                Marker(0, 0.10, "1", "scanned question content", "test_ocr"),
                Marker(0, 0.52, "2", "another scanned question", "test_ocr"),
            ]

        questions = process_pdf(
            source,
            output,
            PipelineOptions(dpi=180),
            ocr_detector=fake_ocr,
        )
        self.assertEqual(len(questions), 2)
        self.assertTrue(all(item.detection_source == "test_ocr" for item in questions))
        build_contact_sheet(
            [path for question in questions for path in question.image_paths],
            WORK / "scan-crops-contact-sheet.jpg",
        )

    def test_queue_and_local_cloud_callback(self) -> None:
        text_source = WORK / "text-source.pdf"
        if not text_source.exists():
            build_text_pdf(text_source)
        queue = QueueStore(WORK / "queue.sqlite3")
        payload = {
            "jobId": "job-test",
            "sourceFileID": str(text_source),
            "storageOwnerKey": "0123456789abcdef0123456789abcdef",
            "title": "测试资料",
            "subjectId": "math2",
            "subjectName": "数学二",
            "questionType": "解答题",
        }
        self.assertTrue(queue.enqueue("job-test", payload))
        self.assertFalse(queue.enqueue("job-test", payload))
        task = queue.claim_next(3)
        self.assertIsNotNone(task)
        queue.mark_complete("job-test")
        self.assertEqual(queue.status("job-test")["status"], "complete")

        job = PdfJobRequest(**payload)
        questions = process_pdf(
            text_source,
            WORK / "local-cloud-output",
            PipelineOptions(dpi=150),
        )
        settings = CloudSettings(
            processor_token="test-token",
            callback_url="",
            cos_bucket="",
            cos_region="",
            cloud_file_host="local.test",
            secret_id="",
            secret_key="",
            session_token="",
            local_mode=True,
            local_storage_dir=WORK / "local-cloud",
        )
        client = CloudClient(settings)
        uploaded = client.upload_question_assets(job, questions)
        client.complete_job(job, questions, uploaded)
        callback = json.loads(
            (WORK / "local-cloud" / "callbacks" / "job-test.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(callback["complete"])
        self.assertEqual(len(callback["questionGroups"]), 3)
        self.assertTrue(
            callback["questionGroups"][0]["material"]["images"][0]["fileID"].startswith(
                "cloud://local.test/private/"
            )
        )


if __name__ == "__main__":
    unittest.main()
