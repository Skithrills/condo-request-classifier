from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Category(StrEnum):
    MAINTENANCE = "maintenance"
    SECURITY = "security"
    NOISE = "noise"
    CLEANLINESS = "cleanliness"
    FACILITIES = "facilities"
    PARKING = "parking"
    BILLING = "billing"
    RENOVATION = "renovation"
    GENERAL = "general_enquiry"
    OTHER = "other"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EMERGENCY = "emergency"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResidentRequest(StrictModel):
    text: str = Field(min_length=3, max_length=4000)
    channel: Literal["Web", "WhatsApp", "Email", "Phone transcript"] = "Web"
    location: str | None = Field(default=None, max_length=120)

    @field_validator("text")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        if not any(char.isalnum() for char in value):
            raise ValueError("Describe the request using words.")
        return value

    @field_validator("location")
    @classmethod
    def blank_location(cls, value: str | None) -> str | None:
        return value or None


class ModelDecision(StrictModel):
    """Only these fields can come from an LLM. Routing stays in application code."""

    category: Category
    priority: Priority
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    evidence: list[str] = Field(max_length=3, description="Short exact quotes from the input.")
    location: str | None = Field(max_length=120, description="Exact input span, or null.")
    multiple_issues: bool = Field(strict=True)


class Prediction(StrictModel):
    category: Category
    priority: Priority = Priority.NORMAL
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    confidence_kind: Literal["uncalibrated_probability", "self_reported", "unavailable"]
    category_scores: dict[Category, float] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    location: str | None = None
    multiple_issues: bool = False
    backend: str
    model: str


class Classification(StrictModel):
    schema_version: str = "1.0"
    category: Category
    priority: Priority
    suggested_team: str
    review_required: bool
    review_reasons: list[str]
    missing_details: list[str]
    location: str | None
    confidence: float | None
    confidence_kind: str
    category_scores: dict[Category, float]
    evidence: list[str]
    backend: str
    model: str
    status: Literal["classified", "needs_review", "provider_error", "safety_escalation"]
    elapsed_ms: float
