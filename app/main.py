from __future__ import annotations

import asyncio
import base64
import binascii
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .engine import FormulaEngine


MAX_IMAGE_BYTES = max(256 * 1024, int(os.getenv("FORMULA_OCR_MAX_IMAGE_BYTES", str(4 * 1024 * 1024))))
ENGINE = FormulaEngine()


class RecognizeRequest(BaseModel):
    imageBase64: str = Field(min_length=16)
    mimeType: str = "image/jpeg"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("FORMULA_OCR_PRELOAD", "1") == "1":
        ENGINE.preload()
    yield


app = FastAPI(
    title="Yantong Formula OCR",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/__tcb_probe__")
def cloudbase_probe() -> dict:
    return {"ok": True}


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "yantong-formula-ocr",
        "modelLoaded": ENGINE.loaded,
        "modelLoading": ENGINE.loading,
        "modelError": ENGINE.load_error,
        "engine": "pix2text-text-formula-ocr",
        "serverTimeMs": int(time.time() * 1000),
    }


@app.post("/warmup", status_code=status.HTTP_202_ACCEPTED)
def warmup() -> dict:
    ENGINE.preload()
    return {
        "accepted": True,
        "modelLoaded": ENGINE.loaded,
        "modelLoading": ENGINE.loading,
        "modelError": ENGINE.load_error,
    }


@app.post("/recognize")
async def recognize(request: RecognizeRequest) -> dict:
    if not request.mimeType.startswith("image/"):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="image required")
    try:
        image_bytes = base64.b64decode(request.imageBase64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid base64 image") from error
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="image exceeds limit")
    try:
        result = await asyncio.to_thread(ENGINE.recognize, image_bytes)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="formula model unavailable") from error
    return {"success": True, "data": result}
