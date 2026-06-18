"""Custom tool compiler diagnostic contracts."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

from millforge.compiler.diagnostics import detect_secret_candidate
from millforge.compiler.validators import validate_lower_field_key, validate_utf8_size
from millforge.contracts import RedactionPolicy, redact_diagnostic_text

MAX_CUSTOM_TOOL_DIAGNOSTIC_MESSAGE_UTF8 = 1024
MAX_CUSTOM_TOOL_DIAGNOSTIC_EVIDENCE_UTF8 = 512


class CustomToolDiagnosticSeverity(str, Enum):
    """Closed custom-tool diagnostic severity values."""

    ERROR = "error"
    WARNING = "warning"


class CustomToolDiagnosticPhase(str, Enum):
    """Closed custom-tool diagnostic phase values."""

    SOURCE = "source"
    DECLARATION = "declaration"
    POLICY = "policy"
    COMPILATION = "compilation"
    DIAGNOSTIC = "diagnostic"


class CustomToolDiagnosticCode(str, Enum):
    """Stable custom-tool compiler diagnostic codes."""

    SOURCE_MALFORMED = "MF-CT001_SOURCE_MALFORMED"
    SECRET_MATERIAL = "MF-CT002_SECRET_MATERIAL"
    RUNTIME_KIND_UNSUPPORTED = "MF-CT003_RUNTIME_KIND_UNSUPPORTED"
    EXECUTABLE_MATERIAL = "MF-CT004_EXECUTABLE_MATERIAL"
    DUPLICATE_TOOL = "MF-CT005_DUPLICATE_TOOL"
    DUPLICATE_MODEL_TOOL_NAME = "MF-CT006_DUPLICATE_MODEL_TOOL_NAME"
    DUPLICATE_IMPLEMENTATION_ID = "MF-CT007_DUPLICATE_IMPLEMENTATION_ID"
    INPUT_SCHEMA_UNSUPPORTED = "MF-CT008_INPUT_SCHEMA_UNSUPPORTED"
    OUTPUT_SCHEMA_UNSUPPORTED = "MF-CT009_OUTPUT_SCHEMA_UNSUPPORTED"
    DESCRIPTION_UNSAFE = "MF-CT010_DESCRIPTION_UNSAFE"
    CAPABILITY_MISSING = "MF-CT011_CAPABILITY_MISSING"
    CAPABILITY_UNKNOWN = "MF-CT012_CAPABILITY_UNKNOWN"
    ARTIFACT_POLICY_INVALID = "MF-CT013_ARTIFACT_POLICY_INVALID"
    APPROVAL_POLICY_INVALID = "MF-CT014_APPROVAL_POLICY_INVALID"
    FORBIDDEN_TOOL_COMPILED = "MF-CT015_FORBIDDEN_TOOL_COMPILED"
    TIMEOUT_POLICY_INVALID = "MF-CT016_TIMEOUT_POLICY_INVALID"
    OUTPUT_POLICY_INVALID = "MF-CT017_OUTPUT_POLICY_INVALID"
    HASH_MISMATCH = "MF-CT018_HASH_MISMATCH"
    DESCRIPTOR_PROJECTION_FAILED = "MF-CT019_DESCRIPTOR_PROJECTION_FAILED"

    SOURCE_INVALID = SOURCE_MALFORMED
    DECLARATION_INVALID = DESCRIPTOR_PROJECTION_FAILED
    LIMIT_EXCEEDED = SOURCE_MALFORMED


class CustomToolDiagnosticEvidence(BaseModel):
    """Immutable redacted scalar evidence for custom-tool diagnostics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: StrictStr
    value: StrictStr

    @field_validator("key")
    @classmethod
    def _key_valid(cls, value: str) -> str:
        return validate_lower_field_key(value)

    @field_validator("value")
    @classmethod
    def _value_redacted(cls, value: str) -> str:
        return redact_custom_tool_text(
            value, maximum=MAX_CUSTOM_TOOL_DIAGNOSTIC_EVIDENCE_UTF8
        )


class CustomToolDiagnostic(BaseModel):
    """Stable custom-tool diagnostic with redacted bounded evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: CustomToolDiagnosticCode
    severity: CustomToolDiagnosticSeverity
    phase: CustomToolDiagnosticPhase
    location: StrictStr | None = None
    path: StrictStr | None = None
    message: StrictStr
    evidence: tuple[CustomToolDiagnosticEvidence, ...] = Field(default_factory=tuple)

    @field_validator("location", "path")
    @classmethod
    def _optional_location_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_utf8_size(value, "diagnostic location", 256)

    @field_validator("message")
    @classmethod
    def _message_redacted(cls, value: str) -> str:
        return redact_custom_tool_text(
            value, maximum=MAX_CUSTOM_TOOL_DIAGNOSTIC_MESSAGE_UTF8
        )

    @model_validator(mode="after")
    def _location_or_path_required(self) -> CustomToolDiagnostic:
        if self.location is None and self.path is None:
            raise ValueError("diagnostic requires location or path")
        return self


def custom_tool_diagnostic_sort_key(
    diagnostic: CustomToolDiagnostic,
) -> tuple[str, str, str, str, str, str, tuple[tuple[str, str], ...]]:
    """Return the canonical ordering key for non-semantic diagnostic lists."""
    return (
        diagnostic.code.value,
        diagnostic.severity.value,
        diagnostic.phase.value,
        diagnostic.path or "",
        diagnostic.location or "",
        diagnostic.message,
        tuple((item.key, item.value) for item in diagnostic.evidence),
    )


def redact_custom_tool_text(value: Any, *, maximum: int) -> str:
    """Return bounded diagnostic text with secret-looking material redacted."""
    text = str(value)
    policy = RedactionPolicy()
    if detect_secret_candidate(
        field_path="/custom_tool_diagnostic",
        field_name="custom_tool_diagnostic",
        value=text,
        policy=policy,
    ):
        return policy.replacement
    return validate_utf8_size(
        redact_diagnostic_text(text, policy=policy), "diagnostic", maximum
    )


def custom_tool_diagnostic(
    code: CustomToolDiagnosticCode,
    *,
    phase: CustomToolDiagnosticPhase,
    message: str,
    location: str | None = None,
    path: str | None = None,
    severity: CustomToolDiagnosticSeverity = CustomToolDiagnosticSeverity.ERROR,
    evidence: Mapping[str, Any] | None = None,
) -> CustomToolDiagnostic:
    """Build a deterministic custom-tool diagnostic."""
    fields = tuple(
        CustomToolDiagnosticEvidence(key=str(key), value=str(value))
        for key, value in sorted(
            (evidence or {}).items(), key=lambda item: str(item[0])
        )
    )
    return CustomToolDiagnostic(
        code=code,
        severity=severity,
        phase=phase,
        location=location,
        path=path,
        message=message,
        evidence=fields,
    )


def malformed_input_diagnostic(
    *,
    phase: CustomToolDiagnosticPhase,
    model_name: str,
    path: str = "/",
    missing_field: str | None = None,
    code: CustomToolDiagnosticCode = CustomToolDiagnosticCode.SOURCE_MALFORMED,
) -> CustomToolDiagnostic:
    """Represent malformed raw input without exposing validator exception text."""
    evidence: dict[str, str] = {"model": model_name}
    message = "Custom tool input is malformed."
    if missing_field is not None:
        message = "Custom tool input is missing a required field."
        evidence["field"] = missing_field
    return custom_tool_diagnostic(
        code,
        phase=phase,
        path=path,
        message=message,
        evidence=evidence,
    )
