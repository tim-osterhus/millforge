"""Connector admission diagnostic contracts."""

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

MAX_CONNECTOR_DIAGNOSTIC_MESSAGE_UTF8 = 1024
MAX_CONNECTOR_DIAGNOSTIC_EVIDENCE_UTF8 = 512


class ConnectorDiagnosticSeverity(str, Enum):
    """Closed connector diagnostic severity values."""

    ERROR = "error"
    WARNING = "warning"


class ConnectorDiagnosticPhase(str, Enum):
    """Closed connector diagnostic phase values."""

    IDENTITY = "identity"
    DISCOVERY = "discovery"
    MANIFEST = "manifest"
    POLICY = "policy"
    ADMISSION = "admission"
    DIAGNOSTIC = "diagnostic"


class ConnectorDiagnosticCode(str, Enum):
    """Stable connector diagnostic codes."""

    DISCOVERY_NOT_CATALOG = "MF-C001_DISCOVERY_NOT_CATALOG"
    IDENTITY_INVALID = "MF-C002_IDENTITY_INVALID"
    SECRET_MATERIAL = "MF-C003_SECRET_MATERIAL"
    PROTOCOL_UNSUPPORTED = "MF-C004_PROTOCOL_UNSUPPORTED"
    TRANSPORT_UNSUPPORTED = "MF-C005_TRANSPORT_UNSUPPORTED"
    CONNECTOR_ID_MISMATCH = "MF-C006_CONNECTOR_ID_MISMATCH"
    EXPECTED_IDENTITY_MISMATCH = "MF-C007_EXPECTED_IDENTITY_MISMATCH"
    DISCOVERY_DUPLICATE_PROVIDER_TOOL = "MF-C008_DISCOVERY_DUPLICATE_PROVIDER_TOOL"
    ADMITTED_PROVIDER_TOOL_MISSING = "MF-C009_ADMITTED_PROVIDER_TOOL_MISSING"
    DENIED_TOOL_INVALID = "MF-C010_DENIED_TOOL_INVALID"
    DUPLICATE_ADMITTED_TOOL = "MF-C011_DUPLICATE_ADMITTED_TOOL"
    DUPLICATE_MODEL_TOOL_NAME = "MF-C012_DUPLICATE_MODEL_TOOL_NAME"
    DUPLICATE_IMPLEMENTATION_ID = "MF-C013_DUPLICATE_IMPLEMENTATION_ID"
    INPUT_SCHEMA_UNSUPPORTED = "MF-C014_INPUT_SCHEMA_UNSUPPORTED"
    OUTPUT_SCHEMA_UNSUPPORTED = "MF-C015_OUTPUT_SCHEMA_UNSUPPORTED"
    DESCRIPTION_REQUIRES_OPERATOR_TEXT = "MF-C016_DESCRIPTION_REQUIRES_OPERATOR_TEXT"
    CAPABILITY_MISSING = "MF-C017_CAPABILITY_MISSING"
    CAPABILITY_UNKNOWN = "MF-C018_CAPABILITY_UNKNOWN"
    APPROVAL_POLICY_INVALID = "MF-C019_APPROVAL_POLICY_INVALID"
    FORBIDDEN_TOOL_ADMITTED = "MF-C020_FORBIDDEN_TOOL_ADMITTED"
    HASH_MISMATCH = "MF-C021_HASH_MISMATCH"


class ConnectorDiagnosticEvidence(BaseModel):
    """Immutable redacted scalar evidence for connector diagnostics."""

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
        return redact_connector_text(
            value, maximum=MAX_CONNECTOR_DIAGNOSTIC_EVIDENCE_UTF8
        )


class ConnectorDiagnostic(BaseModel):
    """Stable connector diagnostic with redacted evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ConnectorDiagnosticCode
    severity: ConnectorDiagnosticSeverity
    phase: ConnectorDiagnosticPhase
    location: StrictStr | None = None
    path: StrictStr | None = None
    message: StrictStr
    evidence: tuple[ConnectorDiagnosticEvidence, ...] = Field(default_factory=tuple)

    @field_validator("location", "path")
    @classmethod
    def _optional_location_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_utf8_size(value, "diagnostic location", 256)

    @field_validator("message")
    @classmethod
    def _message_redacted(cls, value: str) -> str:
        return redact_connector_text(
            value, maximum=MAX_CONNECTOR_DIAGNOSTIC_MESSAGE_UTF8
        )

    @model_validator(mode="after")
    def _location_or_path_required(self) -> ConnectorDiagnostic:
        if self.location is None and self.path is None:
            raise ValueError("diagnostic requires location or path")
        return self


def redact_connector_text(value: Any, *, maximum: int) -> str:
    """Return bounded diagnostic text with secret-looking material redacted."""
    text = str(value)
    policy = RedactionPolicy()
    if detect_secret_candidate(
        field_path="/connector_diagnostic",
        field_name="connector_diagnostic",
        value=text,
        policy=policy,
    ):
        return policy.replacement
    return validate_utf8_size(
        redact_diagnostic_text(text, policy=policy), "diagnostic", maximum
    )


def connector_diagnostic(
    code: ConnectorDiagnosticCode,
    *,
    phase: ConnectorDiagnosticPhase,
    message: str,
    location: str | None = None,
    path: str | None = None,
    severity: ConnectorDiagnosticSeverity = ConnectorDiagnosticSeverity.ERROR,
    evidence: Mapping[str, Any] | None = None,
) -> ConnectorDiagnostic:
    """Build a deterministic connector diagnostic."""
    fields = tuple(
        ConnectorDiagnosticEvidence(key=key, value=str(value))
        for key, value in sorted(
            (evidence or {}).items(), key=lambda item: str(item[0])
        )
    )
    return ConnectorDiagnostic(
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
    phase: ConnectorDiagnosticPhase,
    model_name: str,
    path: str = "/",
    missing_field: str | None = None,
    code: ConnectorDiagnosticCode = ConnectorDiagnosticCode.IDENTITY_INVALID,
) -> ConnectorDiagnostic:
    """Represent malformed raw input without exposing validator exception text."""
    evidence: dict[str, str] = {"model": model_name}
    message = "Connector input is malformed."
    if missing_field is not None:
        message = "Connector input is missing a required field."
        evidence["field"] = missing_field
    return connector_diagnostic(
        code,
        phase=phase,
        path=path,
        message=message,
        evidence=evidence,
    )
