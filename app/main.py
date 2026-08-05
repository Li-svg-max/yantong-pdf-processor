from __future__ import annotations

import asyncio
import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status

from .cloud_client import CloudClient, CloudSettings
from .models import PdfJobRequest, SignedPdfJobRequest, model_to_dict
from .pdf_pipeline import PipelineOptions, process_pdf
from .queue_store import QueueStore, StoredTask
from .request_auth import verify_ticket


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("yantong-pdf-processor")

DATA_DIR = Path(os.getenv("PROCESSOR_DATA_DIR", "/data"))
QUEUE = QueueStore(DATA_DIR / "queue.sqlite3")
MAX_ATTEMPTS = max(1, min(10, int(os.getenv("MAX_PROCESS_ATTEMPTS", "3"))))
POLL_INTERVAL = max(0.2, float(os.getenv("QUEUE_POLL_INTERVAL", "1")))


def _expected_token() -> str:
    return os.getenv("YANTONG_PROCESSOR_TOKEN", "")


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
    job = PdfJobRequest(**task.payload)
    workspace = DATA_DIR / "work" / job.jobId
    if workspace.exists():
        shutil.rmtree(workspace)
    input_path = workspace / "source.pdf"
    output_dir = workspace / "questions"
    cloud_client = CloudClient(CloudSettings.from_environment())
    cloud_client.download_source(job, input_path)
    questions = process_pdf(
        input_path,
        output_dir,
        PipelineOptions(
            dpi=job.options.dpi,
            max_questions=job.options.maxQuestions,
        ),
    )
    uploaded_images = cloud_client.upload_question_assets(job, questions)
    cloud_client.complete_job(job, questions, uploaded_images)
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
        "queue": QUEUE.counts(),
        "missing": missing,
    }


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
    if not verify_ticket(
        request.job,
        request.expiresAt,
        request.signature,
        _expected_token(),
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid job ticket")
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
