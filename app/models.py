from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProcessingOptions(BaseModel):
    dpi: int = Field(default=220, ge=150, le=300)
    maxQuestions: int = Field(default=2000, ge=1, le=2000)


class PdfJobRequest(BaseModel):
    jobId: str = Field(min_length=1, max_length=120)
    sourceFileID: str = Field(min_length=1, max_length=1000)
    storageOwnerKey: str = Field(min_length=16, max_length=64)
    title: str = Field(min_length=1, max_length=80)
    subjectId: str = Field(min_length=1, max_length=30)
    subjectName: str = Field(min_length=1, max_length=40)
    questionType: str = Field(default="未分类", max_length=30)
    options: ProcessingOptions = Field(default_factory=ProcessingOptions)


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()

