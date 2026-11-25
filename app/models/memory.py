"""Validated contracts for manual profiles and incident experience."""

from datetime import timezone
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, StrictBool, StringConstraints, field_validator, model_validator,
)

Identifier = Annotated[str, StringConstraints(
    strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
)]
Language = Annotated[str, StringConstraints(
    strip_whitespace=True, max_length=32, pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
)]
Namespace = Annotated[str, StringConstraints(
    strip_whitespace=True, max_length=63, pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$",
)]
ResponsePreference = Annotated[str, StringConstraints(
    strip_whitespace=True, min_length=1, max_length=500, pattern=r"^[^\r\n]+$",
)]


class StructuredProfileValues(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    preferred_language: Language | None = None
    answer_detail: Literal["concise", "normal", "detailed"] | None = None
    preferred_output_order: Literal["summary_first", "steps_first"] | None = None
    default_cluster: Identifier | None = None
    default_environment: Identifier | None = None
    default_namespace: Namespace | None = None
    responsible_services: list[Identifier] | None = Field(default=None, max_length=50)

    @field_validator("responsible_services")
    @classmethod
    def deduplicate_services(cls, value):
        return list(dict.fromkeys(value)) if value is not None else None


class ProfileValues(StructuredProfileValues):
    response_preferences: list[ResponsePreference] | None = Field(default=None, max_length=50)

    @field_validator("response_preferences")
    @classmethod
    def normalize_response_preferences(cls, value):
        return list(dict.fromkeys(value)) if value else None


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(strict=True, ge=0)
    changes: ProfileValues = Field(default_factory=ProfileValues)
    use_enabled: StrictBool | None = None
    auto_extract_enabled: StrictBool | None = None

    @model_validator(mode="after")
    def settings_cannot_be_null(self):
        for field in ("use_enabled", "auto_extract_enabled"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must be true or false")
        return self


class ProfileSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: ProfileValues
    field_sources: dict[str, dict[str, str]]
    revision: int
    generation: int
    use_enabled: bool
    auto_extract_enabled: bool
    updated_at: str


# Bounded user-authored text. Raw logs and arbitrary metadata are excluded from the contract.
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
IncidentText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2400)]
SessionReference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
MessageID = Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]
IncidentStatus = Literal["draft", "confirmed", "invalidated", "withdrawn"]
IncidentOutcome = Literal["resolved", "unresolved"]


class RecoveryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: IncidentText
    reference_url: HttpUrl | None = None
    message_ids: list[MessageID] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def require_reference(self):
        if not self.reference_url and not self.message_ids:
            raise ValueError("恢复证据须引用监控链接或来源消息")
        if self.reference_url and (self.reference_url.username or self.reference_url.password):
            raise ValueError("证据链接不能包含登录凭据")
        self.message_ids = list(dict.fromkeys(self.message_ids))
        return self


class IncidentFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: ShortText | None = None
    symptoms: IncidentText | None = None
    services: list[Identifier] | None = Field(default=None, max_length=50)
    cluster: Identifier | None = None
    environment: Identifier | None = None
    namespace: Namespace | None = None
    occurred_at: AwareDatetime | None = None
    version_constraints: ShortText | None = None
    diagnostic_actions: IncidentText | None = None
    excluded_hypotheses: IncidentText | None = None
    root_cause: IncidentText | None = None
    actions_taken: IncidentText | None = None
    lessons: IncidentText | None = None
    error_codes: list[Identifier] | None = Field(default=None, max_length=30)
    recovery_evidence: RecoveryEvidence | None = None
    source_session_id: SessionReference | None = None
    source_message_ids: list[MessageID] | None = Field(default=None, max_length=50)
    expires_at: AwareDatetime | None = None

    @field_validator("services", "error_codes", "source_message_ids")
    @classmethod
    def deduplicate(cls, value):
        return list(dict.fromkeys(value)) if value is not None else None

    @field_validator("occurred_at", "expires_at")
    @classmethod
    def utc_timestamp(cls, value):
        return value.astimezone(timezone.utc) if value is not None else None


class IncidentCreate(IncidentFields):
    title: ShortText


class IncidentRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(strict=True, ge=0, le=2**63 - 1)


class IncidentPatch(IncidentRevision):
    changes: IncidentFields


class IncidentConfirm(IncidentRevision):
    outcome: IncidentOutcome
    recovery_evidence: RecoveryEvidence | None = None


class IncidentInvalidate(IncidentRevision):
    reason: ShortText


class MemoryScope(BaseModel):
    """Explicit target supplied by the current user for this request only."""

    model_config = ConfigDict(extra="forbid")

    environment: Identifier | None = None
    cluster: Identifier | None = None
    namespace: Namespace | None = None


class SessionExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: SessionReference
    request_id: Identifier
    start_message_id: MessageID | None = None
    end_message_id: MessageID | None = None


class ForgetMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_generation: int = Field(strict=True, ge=0, le=2**63 - 1)
    confirm: StrictBool


class MemoryReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["apply", "dismiss"]
    fields: list[str] | None = Field(default=None, max_length=8)
