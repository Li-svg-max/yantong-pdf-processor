from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_serializer


class ExportImage(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    alt: str = Field(default="", max_length=200)


class ExportExtensionModel(BaseModel):
    @model_serializer(mode="wrap")
    def preserve_ticket_fields(self, handler):
        # The cloud signs before validation. New optional fields must not be
        # inserted into old clients' signed payloads by model defaults.
        data = handler(self)
        for key in ("aiGenerated", "materialParagraphs", "marginMm",
                    "lineSpacing", "optionColumns", "includeAnswers", "includeMeta"):
            if key not in self.model_fields_set:
                data.pop(key, None)
        if isinstance(self, ExportQuestion) and "blankLines" not in self.model_fields_set:
            data.pop("blankLines", None)
        if "lineSpacing" in data and float(data["lineSpacing"]).is_integer():
            data["lineSpacing"] = int(data["lineSpacing"])
        return data


class ExportQuestion(ExportExtensionModel):
    number: str = Field(default="", max_length=30)
    stem: str = Field(default="", max_length=20000)
    options: list[str] = Field(default_factory=list, max_length=20)
    answer: str = Field(default="", max_length=12000)
    analysis: str = Field(default="", max_length=30000)
    solutionImages: list[ExportImage] = Field(default_factory=list, max_length=20)
    blankLines: int | None = Field(default=None, ge=0, le=12)
    aiGenerated: bool = False


class ExportGroup(ExportExtensionModel):
    order: int = Field(ge=1, le=1000)
    title: str = Field(default="", max_length=300)
    meta: str = Field(default="", max_length=300)
    questionImages: list[ExportImage] = Field(default_factory=list, max_length=20)
    questions: list[ExportQuestion] = Field(default_factory=list, min_length=1, max_length=100)
    materialParagraphs: list[str] = Field(default_factory=list, max_length=200)


class ExportSettings(ExportExtensionModel):
    paper: Literal["A4", "A5"] = "A4"
    columns: Literal[1, 2] = 1
    fontSize: int = Field(default=11, ge=8, le=20)
    questionSpacing: int = Field(default=20, ge=0, le=80)
    blankLines: int = Field(default=3, ge=0, le=12)
    packageMode: Literal["combined", "separate"] = "combined"
    solutionMode: Literal["inline", "end"] = "end"
    includeAnalysis: bool = True
    avoidSplit: bool = True
    marginMm: int = Field(default=17, ge=10, le=25)
    lineSpacing: float = Field(default=1.55, ge=1.2, le=2.2)
    optionColumns: Literal[1, 2] = 1
    includeAnswers: bool = True
    includeMeta: bool = True


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


class ClozeWordStyle(BaseModel):
    fontFamily: Literal['SimSun', 'SimHei', 'KaiTi', 'Microsoft YaHei'] = 'SimSun'
    fontSize: Literal[10.5, 12, 14, 16, 18] = 12
    showTitle: bool = True


class ClozeExportRequest(BaseModel):
    format: Literal["docx", "pdf"] = "docx"
    variant: Literal["practice", "answers", "combined"] = "combined"
    title: str = Field(default="专业课填空练习", min_length=1, max_length=80)
    courseName: str = Field(default="专业课", min_length=1, max_length=40)
    blocks: list[ClozeBlock] = Field(min_length=1, max_length=200)
    wordStyle: ClozeWordStyle | None = None

    @field_validator("title", "courseName")
    @classmethod
    def clean_cloze_text(cls, value: str) -> str:
        return " ".join(value.split()) or "专业课填空练习"

    @model_serializer(mode="wrap")
    def preserve_signed_variant_presence(self, handler):
        # Existing cloud tickets never carried variant. Do not let a Pydantic
        # default change their canonical HMAC payload. Explicit variants remain
        # signed, including explicit combined; tampering must fail verification.
        result = handler(self)
        if "variant" not in self.model_fields_set:
            result.pop("variant", None)
        if "wordStyle" not in self.model_fields_set:
            result.pop("wordStyle", None)
        return result


class SignedExportRequest(BaseModel):
    request: ExportRequest
    expiresAt: int
    signature: str = Field(min_length=64, max_length=64)


class SignedClozeExportRequest(BaseModel):
    request: ClozeExportRequest
    expiresAt: int
    signature: str = Field(min_length=64, max_length=64)
