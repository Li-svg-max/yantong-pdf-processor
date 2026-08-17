from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProcessingOptions(BaseModel):
    dpi: int = Field(default=220, ge=150, le=300)
    maxQuestions: int = Field(default=2000, ge=1, le=2000)


class PdfJobRequest(BaseModel):
    inputKind: Literal["pdf"] = "pdf"
    jobId: str = Field(min_length=1, max_length=120)
    sourceFileID: str = Field(min_length=1, max_length=1000)
    storageOwnerKey: str = Field(min_length=16, max_length=64)
    title: str = Field(min_length=1, max_length=80)
    subjectId: str = Field(min_length=1, max_length=30)
    subjectName: str = Field(min_length=1, max_length=40)
    questionType: str = Field(default="未分类", max_length=30)
    options: ProcessingOptions = Field(default_factory=ProcessingOptions)


class SignedPdfJobRequest(BaseModel):
    job: PdfJobRequest
    expiresAt: int
    signature: str = Field(min_length=64, max_length=64)


class ImageSource(BaseModel):
    sourceFileID: str = Field(min_length=1, max_length=1000)
    numberLabel: str = Field(min_length=1, max_length=20)


class ImageBatchJobRequest(BaseModel):
    inputKind: Literal["image_batch"]
    jobId: str = Field(min_length=1, max_length=120)
    storageOwnerKey: str = Field(min_length=16, max_length=64)
    title: str = Field(min_length=1, max_length=80)
    subjectId: str = Field(min_length=1, max_length=30)
    subjectName: str = Field(min_length=1, max_length=40)
    questionType: str = Field(default="未分类", max_length=30)
    images: list[ImageSource] = Field(min_length=1, max_length=20)


class SignedImageBatchJobRequest(BaseModel):
    job: ImageBatchJobRequest
    expiresAt: int
    signature: str = Field(min_length=64, max_length=64)


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()
