from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.cloud_client import CloudClient, CloudSettings, _bucket_for_cloud_host
from app.models import PdfJobRequest
from app.pdf_pipeline import (
    Marker,
    PipelineOptions,
    RapidOcrMarkerDetector,
    analyze_image_quality,
    process_pdf,
    _normalize_math_ocr_text,
)
from app.queue_store import QueueStore
from app.request_auth import sign_ticket, ticket_validation_error, verify_ticket


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


def build_colored_box_scan_pdf(path: Path) -> None:
    page = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(page)
    for top in (170, 520, 870):
        draw.rectangle((80, top, 185, top + 64), fill=(250, 145, 160))
        draw.line((220, top + 32, 1080, top + 32), fill="black", width=2)
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
        self.assertIn("first limit", questions[0].recognized_text)
        self.assertEqual(questions[0].text_source, "pdf_text")
        self.assertIn("\n", questions[0].recognized_text)
        self.assertEqual(questions[1].source_pages, (1, 2))
        widths = set()
        for question in questions:
            for path in question.image_paths:
                with Image.open(path) as image:
                    widths.add(image.width)
        self.assertEqual(len(widths), 1, "question crops must retain one fixed page width")
        self.assertTrue(all(item.image_quality["originalPreserved"] for item in questions))
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
            ocr_text_extractor=lambda _: {
                "text": "1. scanned question content with x^2",
                "confidence": 0.91,
                "source": "test_full_text_ocr",
            },
        )
        self.assertEqual(len(questions), 2)
        self.assertTrue(all(item.detection_source == "test_ocr" for item in questions))
        self.assertTrue(all("x^2" in item.recognized_text for item in questions))
        self.assertTrue(all(item.text_source == "test_full_text_ocr" for item in questions))
        self.assertTrue(all(item.text_confidence == 0.91 for item in questions))
        build_contact_sheet(
            [path for question in questions for path in question.image_paths],
            WORK / "scan-crops-contact-sheet.jpg",
        )

    def test_scan_pdf_reuses_page_ocr_rows_and_reports_phase_progress(self) -> None:
        source = WORK / "scan-page-ocr-source.pdf"
        output = WORK / "scan-page-ocr-output"
        build_scan_pdf(source)

        class PageOcrDetector:
            def __init__(self) -> None:
                self.page_calls = 0
                self.crop_calls = 0

            def start_document(self) -> None:
                pass

            def __call__(self, _image: Image.Image, page_index: int) -> list[Marker]:
                return [
                    Marker(0, 0.10, "1", "scanned question content", "test_ocr"),
                    Marker(0, 0.52, "2", "another scanned question", "test_ocr"),
                ] if page_index == 0 else []

            def extract_page_rows(self, _image: Image.Image, _page_index: int):
                self.page_calls += 1
                return [
                    (0.10, 0.13, 0.04, "1. scanned question content with x^2", 0.94),
                    (0.16, 0.19, 0.06, "Keep the complete formula line.", 0.94),
                    (0.52, 0.55, 0.04, "2. another scanned question with y^2", 0.94),
                    (0.58, 0.61, 0.06, "Use the complete question text.", 0.94),
                ]

            def extract_text(self, _image: Image.Image):
                self.crop_calls += 1
                return {"text": "crop fallback", "confidence": 0.91, "source": "crop"}

        detector = PageOcrDetector()
        phases: list[str] = []
        questions = process_pdf(
            source,
            output,
            PipelineOptions(dpi=180),
            ocr_detector=detector,
            progress_callback=lambda _done, _total, phase="": phases.append(phase),
        )
        self.assertEqual(detector.page_calls, 1)
        self.assertEqual(detector.crop_calls, 0)
        self.assertTrue(all(item.text_source == "rapidocr_page_text" for item in questions))
        self.assertTrue(all("complete" in item.recognized_text for item in questions))
        self.assertIn("正在识别题号", phases)
        self.assertIn("正在整理题目文字", phases)

    def test_colored_question_box_fallback(self) -> None:
        source = WORK / "colored-box-source.pdf"
        output = WORK / "colored-box-output"
        build_colored_box_scan_pdf(source)
        questions = process_pdf(source, output, PipelineOptions(dpi=180))
        self.assertEqual([item.number_label for item in questions], ["1", "2", "3"])
        self.assertTrue(all(item.detection_source == "colored_question_box" for item in questions))

    def test_colored_markers_win_when_ocr_only_finds_one_question(self) -> None:
        source = WORK / "colored-box-ocr-partial-source.pdf"
        output = WORK / "colored-box-ocr-partial-output"
        build_colored_box_scan_pdf(source)

        def partial_ocr(_: Image.Image, page_index: int) -> list[Marker]:
            return [Marker(page_index, 0.10, "10", "10 lim 2 -> 0", "test_ocr")]

        questions = process_pdf(
            source,
            output,
            PipelineOptions(dpi=180),
            ocr_detector=partial_ocr,
        )
        self.assertEqual([item.number_label for item in questions], ["1", "2", "3"])
        self.assertTrue(all(item.detection_source == "colored_question_box" for item in questions))

    def test_annotation_risk_preserves_original_crop(self) -> None:
        clean = Image.new("RGB", (1200, 800), "white")
        draw = ImageDraw.Draw(clean)
        for y in range(60, 760, 45):
            draw.line((40, y, 1160, y), fill="black", width=3)
        original_size = clean.size
        clean_quality = analyze_image_quality(clean)
        self.assertEqual(clean.size, original_size)
        self.assertEqual(clean_quality["annotationRisk"], "low")
        self.assertTrue(clean_quality["originalPreserved"])

        annotated = clean.copy()
        annotated_draw = ImageDraw.Draw(annotated)
        for offset in range(5):
            annotated_draw.line(
                (100, 100 + offset * 80, 1000, 180 + offset * 80),
                fill=(20, 80, 220),
                width=14,
            )
        annotated_size = annotated.size
        annotated_quality = analyze_image_quality(annotated)
        self.assertEqual(annotated.size, annotated_size)
        self.assertEqual(annotated_quality["annotationRisk"], "high")
        self.assertTrue(annotated_quality["requiresVisualReview"])

    def test_full_text_ocr_tries_colored_annotation_suppression(self) -> None:
        class FakeEngine:
            def __call__(self, pixels):
                sample = pixels[8, 8]
                if int(sample[0]) == 255 and int(sample[1]) == 255 and int(sample[2]) == 255:
                    value, confidence = "clean printed question text", 0.94
                elif int(sample[0]) == int(sample[1]) == int(sample[2]):
                    value, confidence = "gray text", 0.70
                else:
                    value, confidence = "bad", 0.45
                return [[[[0, 0], [100, 0], [100, 20], [0, 20]], value, confidence]], None

        image = Image.new("RGB", (180, 80), "white")
        ImageDraw.Draw(image).rectangle((0, 0, 20, 20), fill=(20, 80, 230))
        detector = RapidOcrMarkerDetector.__new__(RapidOcrMarkerDetector)
        detector._engine = FakeEngine()
        result = detector.extract_text(image)
        self.assertEqual(result["source"], "rapidocr_annotation_suppressed")
        self.assertEqual(result["text"], "clean printed question text")
        self.assertGreater(result["confidence"], 0.9)

    def test_math_ocr_text_normalization_is_conservative(self) -> None:
        value = "I= lim x0 1+e 2 sinx-cosx"
        normalized = _normalize_math_ocr_text(value)
        self.assertIn("lim x → 0", normalized)
        self.assertIn("e²", normalized)
        self.assertIn("sin x", normalized)
        self.assertIn("cos x", normalized)
        self.assertNotIn("xsinx", normalized)

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
            callback["questionGroups"][0]["processingAssets"]["questionCrops"][0]["fileID"].startswith(
                "cloud://local.test/private/"
            )
        )
        self.assertEqual(callback["questionGroups"][0]["material"]["images"], [])
        self.assertTrue(callback["questionGroups"][0]["textFirstPresentation"])

    def test_short_lived_cloudbase_job_ticket(self) -> None:
        job = PdfJobRequest(
            jobId="signed-job",
            sourceFileID="cloud://test/private/owner/imports/signed-job/source.pdf",
            storageOwnerKey="0123456789abcdef0123456789abcdef",
            title="签名任务",
            subjectId="math2",
            subjectName="数学二",
            questionType="解答题",
        )
        now_ms = 2_000_000_000_000
        expires_at = now_ms + 300_000
        token = "test-processor-token"
        signature = sign_ticket(job, expires_at, token)
        self.assertEqual(
            signature,
            "4327d4cf0b2ed8b75071c301d5e3d3afda8963d81949270426ac05f705d3ea47",
        )
        self.assertTrue(verify_ticket(job, expires_at, signature, token, now_ms))
        self.assertFalse(verify_ticket(job, now_ms - 1, signature, token, now_ms))
        self.assertFalse(verify_ticket(job, expires_at, "0" * 64, token, now_ms))
        self.assertEqual(
            ticket_validation_error(job, now_ms - 1, signature, token, now_ms),
            "job ticket expired",
        )
        self.assertEqual(
            ticket_validation_error(job, expires_at, "0" * 64, token, now_ms),
            "job ticket signature does not match",
        )
        self.assertIsNone(
            ticket_validation_error(job, expires_at, signature, f" {token} ", now_ms)
        )

    def test_cloud_file_host_resolves_embedded_cos_bucket(self) -> None:
        self.assertEqual(
            _bucket_for_cloud_host(
                "cloud1-example.636c-cloud1-example-1462091206",
                "configured-fallback-1462091206",
            ),
            "636c-cloud1-example-1462091206",
        )
        self.assertEqual(
            _bucket_for_cloud_host(
                "cloud1-example",
                "configured-fallback-1462091206",
            ),
            "configured-fallback-1462091206",
        )


if __name__ == "__main__":
    unittest.main()
