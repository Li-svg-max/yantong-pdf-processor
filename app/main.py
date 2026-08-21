from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status

from .cloud_client import CloudClient, CloudSettings
from .document_export.models import SignedClozeExportRequest, SignedExportRequest
from .document_export.renderer import render_cloze_export, render_export
from .models import (
    ImageBatchJobRequest,
    PdfJobRequest,
    SignedImageBatchJobRequest,
    SignedPdfJobRequest,
    model_to_dict,
)
from .queue_store import QueueStore, StoredTask
from .request_auth import ticket_validation_error


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("yantong-pdf-processor")

DATA_DIR = Path(os.getenv("PROCESSOR_DATA_DIR", "/data"))
QUEUE = QueueStore(DATA_DIR / "queue.sqlite3")
MAX_ATTEMPTS = max(1, min(10, int(os.getenv("MAX_PROCESS_ATTEMPTS", "3"))))
POLL_INTERVAL = max(0.2, float(os.getenv("QUEUE_POLL_INTERVAL", "1")))
MAX_CONCURRENT_EXPORTS = max(
    1,
    min(4, int(os.getenv("MAX_CONCURRENT_EXPORTS", "2"))),
)
EXPORT_GATE = asyncio.Semaphore(MAX_CONCURRENT_EXPORTS)
PROCESSOR_TICKET_SCHEMA_VERSION = "2026-08-21-image-cloze-v1"


class ProgressReporter:
    """Coalesce progress writes so a slow callback never pauses OCR."""

    def __init__(self, cloud_client: CloudClient, job_id: str) -> None:
        self._client = cloud_client
        self._job_id = job_id
        self._condition = threading.Condition()
        self._pending: tuple[int, str] | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"progress-{job_id[-12:]}",
            daemon=True,
        )
        self._thread.start()

    def submit(self, progress: int, status_text: str) -> None:
        with self._condition:
            if self._closed:
                return
            self._pending = (progress, status_text)
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify()
        # A terminal callback runs only after the last progress value has
        # either reached the database or timed out.
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._pending is None and self._closed:
                    return
                progress, status_text = self._pending
                self._pending = None
            try:
                self._client.report_progress(self._job_id, progress, status_text)
            except Exception:
                LOGGER.warning(
                    "progress callback failed job=%s", self._job_id, exc_info=True
                )


def _expected_token() -> str:
    return os.getenv("YANTONG_PROCESSOR_TOKEN", "").strip()


def _token_fingerprint() -> str:
    token = _expected_token()
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else ""


def _ticket_schema_error(ticket_schema_version: str | None) -> str | None:
    """Reject a known incompatible client before comparing its HMAC."""
    if ticket_schema_version and ticket_schema_version != PROCESSOR_TICKET_SCHEMA_VERSION:
        return "processor ticket schema is incompatible"
    return None


def require_processor_token(authorization: str = Header(default="")) -> None:
    expected = _expected_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="processor token is not configured",
        )
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def _process_task(task: StoredTask) -> None:
    from .image_pipeline import process_image_batch
    from .pdf_pipeline import PipelineOptions, process_pdf
    from .study_material_pipeline import process_study_images, process_study_pdf

    workspace = DATA_DIR / "work" / task.job_id
    if workspace.exists():
        shutil.rmtree(workspace)
    cloud_client = CloudClient(CloudSettings.from_environment())
    progress_reporter = ProgressReporter(cloud_client, task.job_id)
    last_reported_progress = 0
    last_reported_status = ""

    def report(progress: int, status_text: str) -> None:
        nonlocal last_reported_progress, last_reported_status
        progress = max(5, min(95, int(progress)))
        if (
            status_text == last_reported_status
            and progress < 95
            and progress - last_reported_progress < 3
        ):
            return
        progress_reporter.submit(progress, status_text)
        last_reported_progress = progress
        last_reported_status = status_text

    try:
        if task.payload.get("inputKind") == "image_batch":
            job = ImageBatchJobRequest(**task.payload)
            image_sources = []
            report(8, "正在下载题目图片")
            for index, item in enumerate(job.images, start=1):
                suffix = Path(item.sourceFileID).suffix or ".jpg"
                path = workspace / "images" / f"{index:03d}{suffix}"
                cloud_client.download_image_source(item.sourceFileID, path)
                image_sources.append((path, item.numberLabel))
            report(15, "正在识别文字")
            if job.workflowType == "cloze_document":
                blocks = process_study_images(
                    [path for path, _ in image_sources],
                    progress_callback=lambda done, total, phase: report(
                        15 + round(done / max(1, total) * 70), phase
                    ),
                )
            else:
                questions = process_image_batch(
                    image_sources,
                    progress_callback=lambda done, total: report(
                        15 + round(done / max(1, total) * 70), "正在识别文字"
                    ),
                )
            report(88, "正在保存识别结果")
            progress_reporter.close()
            if job.workflowType == "cloze_document":
                cloud_client.complete_cloze_job(job, blocks)
            else:
                cloud_client.complete_image_job(job, questions)
        else:
            job = PdfJobRequest(**task.payload)
            input_path = workspace / "source.pdf"
            output_dir = workspace / "questions"

            def report_pdf_progress(done: int, total: int, phase: str = "") -> None:
                ratio = done / max(1, total)
                if phase in {"正在识别题号", "正在定位题号区域", "题号已定位"}:
                    report(15 + round(ratio * 20), phase or "正在识别题号")
                else:
                    report(35 + round(ratio * 47), phase or "正在整理题目文字")

            report(8, "正在下载 PDF")
            cloud_client.download_source(job, input_path)
            report(15, "正在识别整份资料")
            if job.workflowType == "cloze_document":
                blocks = process_study_pdf(
                    input_path,
                    dpi=job.options.dpi,
                    progress_callback=lambda done, total, phase: report(
                        15 + round(done / max(1, total) * 70), phase
                    ),
                )
            else:
                questions = process_pdf(
                    input_path,
                    output_dir,
                    PipelineOptions(
                        dpi=job.options.dpi,
                        max_questions=job.options.maxQuestions,
                    ),
                    progress_callback=report_pdf_progress,
                )
            if job.workflowType == "cloze_document":
                report(92, "正在保存识别结果")
                progress_reporter.close()
                cloud_client.complete_cloze_job(job, blocks)
                return
            LOGGER.info(
                "pdf detected questions job=%s count=%s sources=%s pages=%s",
                job.jobId,
                len(questions),
                sorted({question.detection_source for question in questions}),
                sorted({page for question in questions for page in question.source_pages}),
            )
            report(82, "正在保存识别结果")
            uploaded_images = cloud_client.upload_question_assets(job, questions)
            LOGGER.info(
                "pdf upload assets job=%s questions=%s image_parts=%s",
                job.jobId,
                len(questions),
                sum(len(parts) for parts in uploaded_images),
            )
            report(92, "正在完成题目整理")
            progress_reporter.close()
            cloud_client.complete_job(job, questions, uploaded_images)
    finally:
        progress_reporter.close()
        shutil.rmtree(workspace, ignore_errors=True)


async def _worker_loop() -> None:
    while True:
        task = QUEUE.claim_next(MAX_ATTEMPTS)
        if not task:
            await asyncio.sleep(POLL_INTERVAL)
            continue
        try:
            LOGGER.info("processing job=%s attempt=%s", task.job_id, task.attempts)
            await asyncio.to_thread(_process_task, task)
        except asyncio.CancelledError:
            QUEUE.mark_failed(task.job_id, "processor shutdown", retry=True)
            raise
        except Exception as error:
            LOGGER.exception("processing failed job=%s", task.job_id)
            retry = task.attempts < MAX_ATTEMPTS
            QUEUE.mark_failed(task.job_id, str(error), retry=retry)
            if not retry:
                try:
                    client = CloudClient(CloudSettings.from_environment())
                    await asyncio.to_thread(client.fail_job, task.job_id, str(error))
                except Exception:
                    LOGGER.exception("failed to report terminal error job=%s", task.job_id)
        else:
            QUEUE.mark_complete(task.job_id)
            LOGGER.info("processing complete job=%s", task.job_id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    worker = asyncio.create_task(_worker_loop(), name="pdf-worker")
    try:
        yield
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Yantong PDF Processor",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/__tcb_probe__")
def cloudbase_probe() -> dict:
    """Return immediately for CloudBase's container readiness probe."""
    return {"ok": True}


@app.get("/health")
def health() -> dict:
    settings = CloudSettings.from_environment()
    required = {
        "YANTONG_PROCESSOR_TOKEN": settings.processor_token,
    }
    if not settings.local_mode:
        required.update(
            {
                "PRIVATE_MATERIAL_CALLBACK_URL": settings.callback_url,
                "COS_BUCKET": settings.cos_bucket,
                "COS_REGION": settings.cos_region,
                "TENCENT_SECRET_ID": settings.secret_id,
                "TENCENT_SECRET_KEY": settings.secret_key,
            }
        )
    missing = [name for name, value in required.items() if not value]
    return {
        "ok": not missing,
        "service": "yantong-pdf-processor",
        "documentExportFormats": ["pdf", "docx"],
        "queue": QUEUE.counts(),
        "missing": missing,
        "tokenFingerprint": _token_fingerprint(),
        "processorTicketSchemaVersion": PROCESSOR_TICKET_SCHEMA_VERSION,
        "serverTimeMs": int(time.time() * 1000),
        "storage": {
            "configuredBucket": settings.cos_bucket,
            "region": settings.cos_region,
            "fileIdBucketResolution": "embedded-host-first",
        },
    }


@app.post("/export")
async def export_document(envelope: SignedExportRequest) -> dict:
    validation_error = ticket_validation_error(
        envelope.request,
        envelope.expiresAt,
        envelope.signature,
        _expected_token(),
        payload_key="request",
    )
    if validation_error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=validation_error,
        )
    async with EXPORT_GATE:
        try:
            files = await asyncio.to_thread(render_export, envelope.request)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from error
        except Exception as error:
            LOGGER.exception("document export failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="文档生成服务暂时不可用",
            ) from error
    return {"success": True, "data": {"files": files}}


@app.post("/cloze-export")
async def cloze_export_document(envelope: SignedClozeExportRequest) -> dict:
    validation_error = ticket_validation_error(
        envelope.request,
        envelope.expiresAt,
        envelope.signature,
        _expected_token(),
        payload_key="request",
    )
    if validation_error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=validation_error,
        )
    async with EXPORT_GATE:
        try:
            files = await asyncio.to_thread(render_cloze_export, envelope.request)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from error
        except Exception as error:
            LOGGER.exception("cloze document export failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="填空文档生成服务暂时不可用",
            ) from error
    return {"success": True, "data": {"files": files}}


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_job(
    job: PdfJobRequest,
    _: None = Depends(require_processor_token),
) -> dict:
    created = QUEUE.enqueue(job.jobId, model_to_dict(job))
    current = QUEUE.status(job.jobId)
    return {
        "accepted": True,
        "created": created,
        "jobId": job.jobId,
        "status": current["status"] if current else "queued",
    }


@app.post("/cloudbase/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_cloudbase_job(request: SignedPdfJobRequest) -> dict:
    schema_error = _ticket_schema_error(request.ticketSchemaVersion)
    if schema_error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=schema_error,
        )
    validation_error = ticket_validation_error(
        request.job,
        request.expiresAt,
        request.signature,
        _expected_token(),
    )
    if validation_error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=validation_error,
        )
    created = QUEUE.enqueue(request.job.jobId, model_to_dict(request.job))
    current = QUEUE.status(request.job.jobId)
    return {
        "accepted": True,
        "created": created,
        "jobId": request.job.jobId,
        "status": current["status"] if current else "queued",
    }


@app.post("/cloudbase/image-jobs", status_code=status.HTTP_202_ACCEPTED)
def create_cloudbase_image_job(request: SignedImageBatchJobRequest) -> dict:
    schema_error = _ticket_schema_error(request.ticketSchemaVersion)
    if schema_error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=schema_error,
        )
    validation_error = ticket_validation_error(
        request.job,
        request.expiresAt,
        request.signature,
        _expected_token(),
    )
    if validation_error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=validation_error,
        )
    created = QUEUE.enqueue(request.job.jobId, model_to_dict(request.job))
    current = QUEUE.status(request.job.jobId)
    return {
        "accepted": True,
        "created": created,
        "jobId": request.job.jobId,
        "status": current["status"] if current else "queued",
    }


@app.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    _: None = Depends(require_processor_token),
) -> dict:
    current = QUEUE.status(job_id)
    if not current:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return current
