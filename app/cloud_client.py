from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .models import PdfJobRequest

if TYPE_CHECKING:
    from .pdf_pipeline import ProcessedQuestion


class CloudClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudSettings:
    processor_token: str
    callback_url: str
    cos_bucket: str
    cos_region: str
    cloud_file_host: str
    secret_id: str
    secret_key: str
    session_token: str
    local_mode: bool
    local_storage_dir: Path

    @classmethod
    def from_environment(cls) -> "CloudSettings":
        local_mode = os.getenv("LOCAL_PROCESSOR_MODE", "").lower() in {"1", "true", "yes"}
        return cls(
            processor_token=os.getenv("YANTONG_PROCESSOR_TOKEN", "").strip(),
            callback_url=os.getenv("PRIVATE_MATERIAL_CALLBACK_URL", ""),
            cos_bucket=os.getenv("COS_BUCKET", ""),
            cos_region=os.getenv("COS_REGION", ""),
            cloud_file_host=os.getenv("CLOUD_FILE_HOST", ""),
            secret_id=os.getenv("TENCENT_SECRET_ID", ""),
            secret_key=os.getenv("TENCENT_SECRET_KEY", ""),
            session_token=os.getenv("TENCENT_SESSION_TOKEN", ""),
            local_mode=local_mode,
            local_storage_dir=Path(os.getenv("LOCAL_STORAGE_DIR", "./data/local-cloud")),
        )


def _cloud_file_parts(file_id: str) -> tuple[str, str]:
    parsed = urlparse(file_id)
    if parsed.scheme != "cloud" or not parsed.netloc or not parsed.path:
        raise CloudClientError("无效的云存储 fileID")
    return parsed.netloc, parsed.path.lstrip("/")


class CloudClient:
    def __init__(self, settings: CloudSettings) -> None:
        self.settings = settings
        self._cos_client = None
        if not settings.processor_token:
            raise CloudClientError("缺少 YANTONG_PROCESSOR_TOKEN")
        if not settings.local_mode and not settings.callback_url:
            raise CloudClientError("缺少 PRIVATE_MATERIAL_CALLBACK_URL")

    def _cos(self):
        if self._cos_client is not None:
            return self._cos_client
        if not all(
            [
                self.settings.cos_bucket,
                self.settings.cos_region,
                self.settings.secret_id,
                self.settings.secret_key,
            ]
        ):
            raise CloudClientError("缺少 COS_BUCKET、COS_REGION 或腾讯云密钥")
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as error:
            raise CloudClientError("缺少 cos-python-sdk-v5") from error
        config = CosConfig(
            Region=self.settings.cos_region,
            SecretId=self.settings.secret_id,
            SecretKey=self.settings.secret_key,
            Token=self.settings.session_token or None,
            Scheme="https",
        )
        self._cos_client = CosS3Client(config)
        return self._cos_client

    def download_source(self, job: PdfJobRequest, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.settings.local_mode:
            source = Path(job.sourceFileID)
            if not source.is_file():
                raise CloudClientError(f"本地 PDF 不存在: {source}")
            shutil.copy2(source, destination)
            return
        _, key = _cloud_file_parts(job.sourceFileID)
        response = self._cos().get_object(Bucket=self.settings.cos_bucket, Key=key)
        response["Body"].get_stream_to_file(str(destination))

    def upload_question_assets(
        self,
        job: PdfJobRequest,
        questions: list[ProcessedQuestion],
    ) -> list[list[dict]]:
        source_host = ""
        if not self.settings.local_mode:
            source_host, _ = _cloud_file_parts(job.sourceFileID)
        cloud_host = self.settings.cloud_file_host or source_host or "local.test"
        uploaded: list[list[dict]] = []
        for question_index, question in enumerate(questions, start=1):
            question_images: list[dict] = []
            for part_index, image_path in enumerate(question.image_paths, start=1):
                suffix = f"-part-{part_index}" if len(question.image_paths) > 1 else ""
                key = (
                    f"private/{job.storageOwnerKey}/questions/{job.jobId}/"
                    f"q-{question_index:04d}{suffix}.jpg"
                )
                if self.settings.local_mode:
                    target = self.settings.local_storage_dir / key
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(image_path, target)
                else:
                    self._cos().upload_file(
                        Bucket=self.settings.cos_bucket,
                        LocalFilePath=str(image_path),
                        Key=key,
                        PartSize=10,
                        MAXThread=3,
                        EnableMD5=False,
                    )
                question_images.append(
                    {
                        "fileID": f"cloud://{cloud_host}/{key}",
                        "alt": f"{job.title}第 {question.number_label} 题原图",
                    }
                )
            uploaded.append(question_images)
        return uploaded

    def complete_job(
        self,
        job: PdfJobRequest,
        questions: list[ProcessedQuestion],
        uploaded_images: list[list[dict]],
    ) -> None:
        groups = [
            self._build_group(job, question, uploaded_images[index], index)
            for index, question in enumerate(questions)
        ]
        for offset in range(0, len(groups), 100):
            batch = groups[offset : offset + 100]
            complete = offset + len(batch) >= len(groups)
            progress = round(((offset + len(batch)) / max(1, len(groups))) * 100)
            self._callback(
                {
                    "type": "completeImportFromProcessor",
                    "processorToken": self.settings.processor_token,
                    "jobId": job.jobId,
                    "complete": complete,
                    "progress": progress,
                    "questionGroups": batch,
                }
            )

    def fail_job(self, job_id: str, message: str) -> None:
        self._callback(
            {
                "type": "failImportFromProcessor",
                "processorToken": self.settings.processor_token,
                "jobId": job_id,
                "message": message[:500],
            }
        )

    def _callback(self, payload: dict) -> dict:
        if self.settings.local_mode:
            target = self.settings.local_storage_dir / "callbacks" / f"{payload['jobId']}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"success": True}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.settings.callback_url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.settings.processor_token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as error:
            raise CloudClientError(f"回写私人题库失败: {error}") from error
        try:
            result = json.loads(response_body)
        except json.JSONDecodeError as error:
            raise CloudClientError("回写接口返回了无效 JSON") from error
        if result.get("success") is not True:
            raise CloudClientError(result.get("message") or "回写私人题库失败")
        return result

    @staticmethod
    def _build_group(
        job: PdfJobRequest,
        question: ProcessedQuestion,
        images: list[dict],
        index: int,
    ) -> dict:
        group_id = f"private-{job.jobId}-{index + 1:04d}"
        question_id = f"{group_id}-q1"
        summary = question.summary or f"第 {question.number_label} 题"
        return {
            "id": group_id,
            "paper": job.title,
            "year": datetime.now().year,
            "numberLabel": question.number_label,
            "chapter": "我的资料",
            "knowledgePoint": summary,
            "questionType": job.questionType,
            "type": job.questionType,
            "difficulty": "未标注",
            "title": f"第 {question.number_label} 题",
            "stem": summary,
            "overviewText": summary,
            "overviewContent": {
                "format": "text",
                "plainText": summary,
                "latexText": "",
            },
            "material": {
                "paper": job.title,
                "section": "我的资料",
                "title": f"第 {question.number_label} 题",
                "topic": job.questionType,
                "paragraphs": [],
                "images": images,
            },
            "subquestions": [
                {
                    "id": question_id,
                    "number": question.number_label,
                    "subjectId": job.subjectId,
                    "subjectName": job.subjectName,
                    "stem": summary,
                    "content": {"format": "text", "plainText": summary, "latexText": ""},
                    "options": [],
                    "answer": "",
                    "analysis": "",
                    "solutionImages": [],
                    "sourceType": "private_upload",
                    "status": "未掌握",
                }
            ],
            "questionCount": 1,
            "sourceType": "private_upload",
            "sourceFormat": "user_pdf_crop",
            "selectionMode": "number_summary",
            "textReviewStatus": (
                "pdf_text_index"
                if question.detection_source == "text_layer"
                else "machine_number_index"
            ),
            "detectionSource": question.detection_source,
            "sourcePages": list(question.source_pages),
            "status": "未掌握",
            "reviewStatus": "private",
            "useCroppedQuestionImage": True,
            "detailsLoaded": True,
            "summaryOnly": False,
        }
