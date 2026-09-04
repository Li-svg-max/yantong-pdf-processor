from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from app.pdf_pipeline import ProcessedQuestion
from app.vision_ocr import VisionOcrSettings, VisionQuestionRecognizer, enhance_questions_with_vision


ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / "tmp" / "pdfs" / "vision-ocr-tests"


class VisionOcrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if WORK.exists():
            shutil.rmtree(WORK)
        WORK.mkdir(parents=True)

    def test_pdf_crop_uses_structured_vision_and_records_usage(self) -> None:
        image_path = WORK / "question-412.jpg"
        image = Image.new("RGB", (900, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.text((40, 40), "412. printed math question", fill="black")
        image.save(image_path, quality=90)

        captured: dict = {}

        def requester(url: str, headers: dict, payload: dict, timeout: int) -> dict:
            captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"questions":[{"number":"412","stem":"设 $f(x,y)$，则下列命题成立的是",'
                            '"statements":["② $\\\\lim_{x\\\\to0}f\'_x(x,0)=f\'_x(0,0)$",'
                            '"② $\\\\lim_{x\\\\to0}f\'_x(x,0)=f\'_x(0,0)$"],'
                            '"options":["(A) 1","(B) 2","(A) 1"],"confidence":0.96}]}'
                        )
                    }
                }],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 220,
                    "total_tokens": 1420,
                    "prompt_cache_hit_tokens": 800,
                    "prompt_cache_miss_tokens": 400,
                },
            }

        question = ProcessedQuestion(
            number_label="412",
            summary="第 412 题",
            recognized_text="RapidOCR draft",
            text_source="rapidocr_full_crop",
            text_confidence=0.55,
            image_paths=(image_path,),
            source_pages=(1,),
            detection_source="rapidocr",
            image_quality={},
        )
        settings = VisionOcrSettings(
            api_key="test-key",
            base_url="https://api.deepseek.com/v1",
            model="vision-test",
            timeout_seconds=25,
            max_workers=1,
        )
        recognizer = VisionQuestionRecognizer(settings, requester=requester)
        result = enhance_questions_with_vision(
            [question],
            "数学一",
            settings=settings,
            recognizer=recognizer,
        )[0]

        self.assertEqual(result.text_source, "deepseek_vision")
        self.assertEqual(result.recognized_text.count("②"), 1)
        self.assertEqual(result.recognized_text.count("(A) 1"), 1)
        self.assertIn("f'_x", result.recognized_text)
        self.assertEqual(result.recognition_metrics["totalTokens"], 1420)
        self.assertEqual(result.recognition_metrics["cacheHitTokens"], 800)
        self.assertEqual(captured["url"], "https://api.deepseek.com/v1/chat/completions")
        image_items = captured["payload"]["messages"][1]["content"]
        self.assertTrue(any(item.get("type") == "image_url" for item in image_items))

    def test_disabled_vision_keeps_rapidocr_result(self) -> None:
        question = ProcessedQuestion(
            number_label="1",
            summary="第 1 题",
            recognized_text="draft",
            text_source="rapidocr_full_crop",
            text_confidence=0.5,
            image_paths=(),
            source_pages=(1,),
            detection_source="rapidocr",
            image_quality={},
        )
        settings = VisionOcrSettings(api_key="", mode="all")
        self.assertEqual(enhance_questions_with_vision([question], "数学一", settings), [question])


if __name__ == "__main__":
    unittest.main()
