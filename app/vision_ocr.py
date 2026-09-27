from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image

from .pdf_pipeline import ProcessedQuestion, _clean_summary


DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_VISION_MODEL = "deepseek-v4-flash-vision-exp"
MAX_IMAGE_PARTS = 4


def _integer(value: object, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class VisionOcrSettings:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_VISION_MODEL
    timeout_seconds: int = 35
    max_workers: int = 2
    mode: str = "all"

    @classmethod
    def from_environment(cls) -> "VisionOcrSettings":
        api_key = (
            os.getenv("DEEPSEEK_API_KEY", "").strip()
            or os.getenv("YANTONG_DEEPSEEK_API_KEY", "").strip()
        )
        return cls(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/"),
            model=os.getenv("DEEPSEEK_VISION_MODEL", DEFAULT_VISION_MODEL).strip(),
            timeout_seconds=_integer(os.getenv("PDF_AI_OCR_TIMEOUT_SECONDS"), 35, 15, 90),
            max_workers=_integer(os.getenv("PDF_AI_OCR_CONCURRENCY"), 2, 1, 4),
            mode=os.getenv("PDF_AI_OCR_MODE", "all").strip().lower(),
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.api_key
            and self.base_url.startswith("https://")
            and self.model
            and self.mode not in {"off", "disabled", "rapidocr"}
        )

    @property
    def requested(self) -> bool:
        return self.mode not in {"off", "disabled", "rapidocr", "optional"}

    @property
    def configuration_error(self) -> str:
        if not self.requested or self.enabled:
            return ""
        if not self.api_key:
            return "PDF 结构化视觉识别未配置 DEEPSEEK_API_KEY"
        if not self.base_url.startswith("https://"):
            return "DEEPSEEK_BASE_URL 必须使用 HTTPS"
        if not self.model:
            return "DEEPSEEK_VISION_MODEL 未配置"
        return "PDF 结构化视觉识别配置无效"


def _chat_endpoint(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    root = re.sub(r"/v1$", "", base_url.rstrip("/"))
    return f"{root}/v1/chat/completions"


def _image_parts(paths: Sequence[Path]) -> list[str]:
    parts: list[str] = []
    for path in paths:
        with Image.open(path) as opened:
            source = opened.convert("RGB")
            target_width = min(1800, source.width)
            scale = target_width / max(1, source.width)
            resized_height = max(1, round(source.height * scale))
            source = source.resize((target_width, resized_height), Image.Resampling.LANCZOS)
            segment_height = 2600
            for top in range(0, source.height, segment_height):
                if len(parts) >= MAX_IMAGE_PARTS:
                    raise RuntimeError("题目图片过长，超过 4 个视觉分片；请缩小单题范围后重试，未截断题干进行识别")
                segment = source.crop((0, top, source.width, min(source.height, top + segment_height)))
                buffer = io.BytesIO()
                segment.save(buffer, "JPEG", quality=88, optimize=True, subsampling=0)
                parts.append(
                    "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                )
    return parts


def _extract_json(value: object) -> dict:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("视觉模型没有返回有效题目 JSON")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise RuntimeError("视觉模型题目 JSON 格式错误")
    return parsed


def _compact(value: object, maximum: int = 12000) -> str:
    return re.sub(r"[ \t]+", " ", str(value or "")).strip()[:maximum]


def _unique_lines(values: Sequence[object], existing: str = "") -> list[str]:
    seen = {re.sub(r"\s+", "", line) for line in existing.splitlines() if line.strip()}
    result: list[str] = []
    for value in values:
        line = _compact(value, 1800)
        key = re.sub(r"\s+", "", line)
        if not line or not key or key in seen:
            continue
        seen.add(key)
        result.append(line)
    return result


def _question_text(question: dict, fallback_number: str) -> tuple[str, str, float]:
    number = _compact(question.get("number") or fallback_number, 20)
    stem = _compact(question.get("stem"))
    statements = _unique_lines(
        question.get("statements") if isinstance(question.get("statements"), list) else [],
        stem,
    )
    options = _unique_lines(
        question.get("options") if isinstance(question.get("options"), list) else [],
        "\n".join([stem] + statements),
    )
    text = "\n".join(part for part in [stem, *statements, *options] if part).strip()[:12000]
    try:
        confidence = float(question.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return number or fallback_number, text, max(0.0, min(1.0, confidence))


def _usage(value: object) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    prompt = int(source.get("prompt_tokens") or source.get("input_tokens") or 0)
    completion = int(source.get("completion_tokens") or source.get("output_tokens") or 0)
    return {
        "inputTokens": prompt,
        "outputTokens": completion,
        "totalTokens": int(source.get("total_tokens") or prompt + completion),
        "cacheHitTokens": int(source.get("prompt_cache_hit_tokens") or 0),
        "cacheMissTokens": int(source.get("prompt_cache_miss_tokens") or 0),
    }


def _combined_usage(responses: Sequence[dict]) -> dict[str, int]:
    values = [_usage(response.get("usage")) for response in responses]
    return {
        name: sum(item.get(name, 0) for item in values)
        for name in ("inputTokens", "outputTokens", "totalTokens", "cacheHitTokens", "cacheMissTokens")
    }


def _looks_incomplete(question: dict, text: str, draft: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    draft_compact = re.sub(r"\s+", "", draft or "")
    if len(compact) < 8 or compact.count("$") % 2:
        return True
    if draft_compact and len(draft_compact) >= 30 and len(compact) < len(draft_compact) * 0.42:
        return True
    # A half-open interval such as [0,1) is valid math, not a broken bracket.
    if compact.count("{") != compact.count("}"):
        return True
    if "[图片未包含后续内容]" in text:
        return True
    last_line = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
    if re.search(r"(?:则|等于|为|lim|\\lim|[=＋+−\-×÷/])\s*\$?$", last_line, re.I):
        return True
    uncertain = question.get("uncertainParts")
    return isinstance(uncertain, list) and any("后半" in str(item) or "截断" in str(item) for item in uncertain)


def _default_requester(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"视觉模型返回 HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"视觉模型连接失败: {error}") from error


class VisionQuestionRecognizer:
    def __init__(
        self,
        settings: VisionOcrSettings,
        requester: Callable[[str, dict, dict, int], dict] | None = None,
    ) -> None:
        self.settings = settings
        self.requester = requester or _default_requester

    def recognize(self, question: ProcessedQuestion, subject_name: str) -> ProcessedQuestion:
        started = time.monotonic()
        data_urls = _image_parts(question.image_paths)
        if not data_urls:
            return question
        prompt = "\n\n".join(
            [
                "你是考研题目识别校对器。只识别图片中的印刷题目，不要把手写演算、圈画、红笔批改或答案标记混入题干。",
                "图片只对应一道题。保留完整定义条件、①②③④等分项命题和A-D全部选项；不要重复题干、命题或选项。",
                "数学公式必须使用标准 LaTeX，并用单个 $...$ 包围。重点复核导数撇号、偏导下标、极限趋向、分式、根号、积分上下限、矩阵和正负号。无法辨认时写[无法辨认]，不能猜造。",
                "stem、statements、options 本身必须包含图片中对应的全部公式；formulas 只是额外索引，绝不能把题干后半段只放进 formulas 而从 stem 中省略。输出前检查题干不能以‘则’、‘为’、‘lim’、等号或未闭合括号结束。",
                "只返回JSON对象，不要Markdown。结构：",
                '{"questions":[{"number":"","stem":"","statements":[""],"options":[""],"formulas":[{"latex":"","plainText":""}],"confidence":0.0,"uncertainParts":[""]}],"overallConfidence":0.0,"notes":""}',
                f"科目：{subject_name or '未分类'}",
                f"RapidOCR草稿（只用于比对，必须以图片为准）：\n{question.recognized_text or '无'}",
            ]
        )
        content = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": value}} for value in data_urls)
        payload = {
            "model": self.settings.model,
            "temperature": 0,
            "reasoning_effort": "low",
            "thinking": {"type": "disabled"},
            "max_tokens": 3000,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你必须输出有效JSON，不能输出Markdown。"},
                {"role": "user", "content": content},
            ],
        }
        response = self.requester(
            _chat_endpoint(self.settings.base_url),
            {
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            payload,
            self.settings.timeout_seconds,
        )
        choices = response.get("choices") if isinstance(response, dict) else None
        message = choices[0].get("message", {}) if isinstance(choices, list) and choices else {}
        parsed = _extract_json(message.get("content"))
        questions = parsed.get("questions") if isinstance(parsed.get("questions"), list) else []
        if not questions or not isinstance(questions[0], dict):
            raise RuntimeError("视觉模型没有识别出完整题目")
        number, text, confidence = _question_text(questions[0], question.number_label)
        responses = [response]
        calls = 1
        incomplete = _looks_incomplete(questions[0], text, question.recognized_text)
        repair_error = ""
        if incomplete:
            repair_prompt = "\n\n".join(
                [
                    "上一次输出被完整性检查判定为截断或结构不完整。请重新查看原图，从题号开始到该题最后一个选项或问号为止，完整重抄这一道印刷题。",
                    "不要沿用上一次的残缺句子，不要补造被图片裁掉的内容。若原图确实缺失，必须在对应位置写[图片未包含后续内容]。",
                    "stem、statements、options 中必须直接包含各自公式，不能只把公式放在 formulas 数组。只返回与第一次相同结构的JSON对象。",
                    f"第一次输出：\n{message.get('content') or ''}",
                    f"RapidOCR草稿：\n{question.recognized_text or '无'}",
                ]
            )
            repair_content = [{"type": "text", "text": repair_prompt}]
            repair_content.extend(
                {"type": "image_url", "image_url": {"url": value}} for value in data_urls
            )
            repair_payload = dict(payload)
            repair_payload["messages"] = [
                {"role": "system", "content": "你必须输出完整有效JSON，不能输出Markdown。"},
                {"role": "user", "content": repair_content},
            ]
            calls += 1
            try:
                repaired_response = self.requester(
                    _chat_endpoint(self.settings.base_url),
                    {
                        "Authorization": f"Bearer {self.settings.api_key}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    repair_payload,
                    self.settings.timeout_seconds,
                )
                responses.append(repaired_response)
                repaired_choices = repaired_response.get("choices") if isinstance(repaired_response, dict) else None
                repaired_message = (
                    repaired_choices[0].get("message", {})
                    if isinstance(repaired_choices, list) and repaired_choices
                    else {}
                )
                repaired = _extract_json(repaired_message.get("content"))
                repaired_questions = repaired.get("questions") if isinstance(repaired.get("questions"), list) else []
                if repaired_questions and isinstance(repaired_questions[0], dict):
                    candidate_number, candidate_text, candidate_confidence = _question_text(
                        repaired_questions[0], question.number_label
                    )
                    candidate_incomplete = _looks_incomplete(repaired_questions[0], candidate_text, question.recognized_text)
                    if candidate_text and (not candidate_incomplete or len(candidate_text) >= len(text)):
                        number, text, confidence = candidate_number, candidate_text, candidate_confidence
                        incomplete = candidate_incomplete
            except Exception as error:
                repair_error = str(error)[:200]
                # Preserve the first usable response and its billed usage.
                incomplete = True
        if len(re.sub(r"\s+", "", text)) < 4:
            raise RuntimeError("视觉模型返回的题干过短")
        elapsed_ms = round((time.monotonic() - started) * 1000)
        metrics = {
            "provider": "deepseek",
            "model": self.settings.model,
            "operation": "pdf_question_recognition",
            "status": "needs_review" if incomplete else "success",
            "incomplete": incomplete,
            "repairError": repair_error,
            "calls": calls,
            "successfulCalls": len(responses),
            "failedCalls": calls - len(responses),
            "durationMs": elapsed_ms,
            **_combined_usage(responses),
        }
        return replace(
            question,
            number_label=number,
            summary=_clean_summary(text.splitlines()[0], number),
            recognized_text=text,
            text_source="deepseek_vision",
            text_confidence=min(confidence, 0.5) if incomplete else confidence,
            recognition_metrics=metrics,
        )


def enhance_questions_with_vision(
    questions: Sequence[ProcessedQuestion],
    subject_name: str,
    settings: VisionOcrSettings | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    recognizer: VisionQuestionRecognizer | None = None,
) -> list[ProcessedQuestion]:
    resolved = settings or VisionOcrSettings.from_environment()
    if not resolved.enabled:
        return list(questions)
    client = recognizer or VisionQuestionRecognizer(resolved)
    results = list(questions)
    completed = 0
    with ThreadPoolExecutor(max_workers=resolved.max_workers) as executor:
        futures = {
            executor.submit(client.recognize, question, subject_name): index
            for index, question in enumerate(questions)
        }
        for future in as_completed(futures):
            index = futures[future]
            original = questions[index]
            try:
                results[index] = future.result()
            except Exception as error:
                results[index] = replace(
                    original,
                    recognition_metrics={
                        "provider": "deepseek",
                        "model": resolved.model,
                        "operation": "pdf_question_recognition",
                        "status": "failed",
                        "calls": 1,
                        "durationMs": 0,
                        "inputTokens": 0,
                        "outputTokens": 0,
                        "totalTokens": 0,
                        "cacheHitTokens": 0,
                        "cacheMissTokens": 0,
                        "error": str(error)[:300],
                    },
                )
            completed += 1
            if progress_callback:
                progress_callback(completed, len(questions))
    return results
