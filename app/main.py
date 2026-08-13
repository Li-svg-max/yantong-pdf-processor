from __future__ import annotations

import asyncio
import base64
import binascii
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .detector import FormulaDetector, FormulaDetectorLoadingError
from .engine import FormulaEngine, FormulaModelLoadingError


MAX_IMAGE_BYTES = max(256 * 1024, int(os.getenv("FORMULA_OCR_MAX_IMAGE_BYTES", str(4 * 1024 * 1024))))
SERVICE_RELEASE = os.getenv("FORMULA_OCR_RELEASE", "mfd-mfr-onnx-v3")
ENGINE = FormulaEngine()
DETECTOR = FormulaDetector()


class RecognizeRequest(BaseModel):
    imageBase64: str = Field(min_length=16)
    mimeType: str = "image/jpeg"


class Region(BaseModel):
    left: int = Field(ge=0)
    top: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class RecognizeRegionRequest(RecognizeRequest):
    region: Region


class WarmupRequest(BaseModel):
    targets: list[str] = ["recognizer", "detector"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("FORMULA_OCR_PRELOAD", "0") == "1":
        ENGINE.preload()
        DETECTOR.preload()
    yield


app = FastAPI(
    title="Yantong Formula OCR",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/")
@app.get("/__tcb_probe__")
def cloudbase_probe() -> dict:
    return {"ok": True}


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "yantong-formula-ocr",
        "release": SERVICE_RELEASE,
        "modelLoaded": ENGINE.loaded,
        "modelLoading": ENGINE.loading,
        "modelError": ENGINE.load_error,
        "detectorLoaded": DETECTOR.loaded,
        "detectorLoading": DETECTOR.loading,
        "detectorError": DETECTOR.load_error,
        "engine": "pix2text-mfr-onnx",
        "serverTimeMs": int(time.time() * 1000),
    }


@app.post("/warmup", status_code=status.HTTP_202_ACCEPTED)
def warmup(request: WarmupRequest | None = None) -> dict:
    targets = set((request.targets if request else ["recognizer", "detector"]))
    if "recognizer" in targets:
        ENGINE.preload()
    if "detector" in targets:
        DETECTOR.preload()
    return {
        "accepted": True,
        "modelLoaded": ENGINE.loaded,
        "modelLoading": ENGINE.loading,
        "modelError": ENGINE.load_error,
        "detectorLoaded": DETECTOR.loaded,
        "detectorLoading": DETECTOR.loading,
        "detectorError": DETECTOR.load_error,
    }


def decode_image(request: RecognizeRequest) -> bytes:
    if not request.mimeType.startswith("image/"):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="image required")
    try:
        image_bytes = base64.b64decode(request.imageBase64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid base64 image") from error
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="image exceeds limit")
    return image_bytes


@app.post("/recognize")
async def recognize(request: RecognizeRequest) -> dict:
    image_bytes = decode_image(request)
    try:
        result = await asyncio.to_thread(ENGINE.recognize, image_bytes)
    except FormulaModelLoadingError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "MODEL_LOADING", "message": str(error)},
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="formula model unavailable") from error
    return {"success": True, "data": result}


@app.post("/analyze")
async def analyze(request: RecognizeRequest) -> dict:
    image_bytes = decode_image(request)
    try:
        result = await asyncio.to_thread(DETECTOR.detect, image_bytes)
    except FormulaDetectorLoadingError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "DETECTOR_LOADING", "message": str(error)},
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="formula detector unavailable") from error
    return {"success": True, "data": result}


@app.post("/recognize-region")
async def recognize_region(request: RecognizeRegionRequest) -> dict:
    image_bytes = decode_image(request)
    try:
        result = await asyncio.to_thread(ENGINE.recognize, image_bytes, request.region.model_dump())
    except FormulaModelLoadingError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "MODEL_LOADING", "message": str(error)},
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="formula model unavailable") from error
    return {"success": True, "data": result}
