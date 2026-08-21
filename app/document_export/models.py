from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ExportImage(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    alt: str = Field(default="", max_length=200)


class ExportQuestion(BaseModel):
    number: str = Field(default="", max_length=30)
    stem: str = Field(default="", max_length=20000)
    options: list[str] = Field(default_factory=list, max_length=20)
    answer: str = Field(default="", max_length=12000)
    analysis: str = Field(default="", max_length=30000)
    solutionImages: list[ExportImage] = Field(default_factory=list, max_length=20)


class ExportGroup(BaseModel):
    order: int = Field(ge=1, le=1000)
    title: str = Field(default="", max_length=300)
    meta: str = Field(default="", max_length=300)
    questionImages: list[ExportImage] = Field(default_factory=list, max_length=20)
    questions: list[ExportQuestion] = Field(default_factory=list, min_length=1, max_length=100)


class ExportSettings(BaseModel):
    paper: Literal["A4", "A5"] = "A4"
    columns: Literal[1, 2] = 1
    fontSize: int = Field(default=11, ge=8, le=20)
    questionSpacing: int = Field(default=20, ge=0, le=80)
    blankLines: int = Field(default=3, ge=0, le=12)
    packageMode: Literal["combined", "separate"] = "combined"
    solutionMode: Literal["inline", "end"] = "end"
    includeAnalysis: bool = True
    avoidSplit: bool = True


class ExportRequest(BaseModel):
    format: Literal["pdf", "docx"]
    title: str = Field(default="我的考研错题集", min_length=1, max_length=80)
    settings: ExportSettings = Field(default_factory=ExportSettings)
    groups: list[ExportGroup] = Field(min_length=1, max_length=100)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        return " ".join(value.split()) or "我的考研错题集"


class ClozeBlank(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    start: int = Field(ge=0, le=12000)
    end: int = Field(ge=1, le=12000)
    answer: str = Field(min_length=1, max_length=120)
    enabled: bool = True


class ClozeBlock(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    page: int = Field(default=1, ge=1, le=9999)
    order: int = Field(default=1, ge=1, le=9999)
    text: str = Field(min_length=1, max_length=12000)
    blanks: list[ClozeBlank] = Field(default_factory=list, max_length=100)


class ClozeExportRequest(BaseModel):
    format: Literal["docx"] = "docx"
    title: str = Field(default="专业课填空练习", min_length=1, max_length=80)
    courseName: str = Field(default="专业课", min_length=1, max_length=40)
    blocks: list[ClozeBlock] = Field(min_length=1, max_length=200)

    @field_validator("title", "courseName")
    @classmethod
    def clean_cloze_text(cls, value: str) -> str:
        return " ".join(value.split()) or "专业课填空练习"


class SignedExportRequest(BaseModel):
    request: ExportRequest
    expiresAt: int
    signature: str = Field(min_length=64, max_length=64)


class SignedClozeExportRequest(BaseModel):
    request: ClozeExportRequest
    expiresAt: int
    signature: str = Field(min_length=64, max_length=64)
