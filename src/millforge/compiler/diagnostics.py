"""Compiler diagnostic contracts and deterministic helpers."""

from __future__ import annotations

import json
import re
from enum import Enum
from collections.abc import Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from millforge.contracts import RedactionPolicy, redact_diagnostic_text
from millforge.compiler.validators import (
    validate_harness_id,
    validate_lower_field_key,
    validate_node_id,
    validate_utf8_size,
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^&\s]{8,}"),
    re.compile(r"(?i)://[^/\s:@]+:[^/\s:@]+@"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(sk|pk|org|sess)-[A-Za-z0-9]{16,}\b"),
)
MAX_DIAGNOSTIC_MESSAGE_UTF8 = 1024
MAX_DIAGNOSTIC_FIELDS = 16
MAX_DIAGNOSTIC_FIELD_STRING_UTF8 = 512
MAX_RELATED_IDS = 16
MAX_DIAGNOSTIC_REPORT_BYTES = 256 * 1024
MAX_DIAGNOSTICS_PER_REPORT = 128
MAX_DIAGNOSTICS_WITHOUT_TRUNCATION_WARNING = MAX_DIAGNOSTICS_PER_REPORT - 1
_DIAGNOSTICS_TRUNCATED_MESSAGE = "Diagnostics were truncated."
_TRUNCATED_SUFFIX = "[truncated]"


class CompilerPhase(str, Enum):
    """Closed compiler phase values used in diagnostics and results."""

    REQUEST = "request"
    PARSE = "parse"
    SCHEMA = "schema"
    RESOLUTION = "resolution"
    GRAPH = "graph"
    CAPABILITY = "capability"
    ARTIFACT = "artifact"
    LOWERING = "lowering"
    OUTPUT = "output"
    INTERNAL = "internal"


class DiagnosticSeverity(str, Enum):
    """Closed diagnostic severity values."""

    ERROR = "error"
    WARNING = "warning"


DIAGNOSTIC_REGISTRY: dict[str, tuple[CompilerPhase, DiagnosticSeverity]] = {
    **{
        f"MF-S{number:03d}": (CompilerPhase.REQUEST, DiagnosticSeverity.ERROR)
        for number in (1, 2, 13, 14, 15, 16, 17, 18)
    },
    **{
        f"MF-S{number:03d}": (CompilerPhase.PARSE, DiagnosticSeverity.ERROR)
        for number in range(3, 13)
    },
    **{
        f"MF-S{number:03d}": (CompilerPhase.SCHEMA, DiagnosticSeverity.ERROR)
        for number in range(20, 30)
    },
    "MF-D001": (CompilerPhase.INTERNAL, DiagnosticSeverity.WARNING),
}

_REQUEST_DIAGNOSTIC_ORDER = {
    "MF-S018": 0,
    "MF-S001": 1,
    "MF-S002": 2,
    "MF-S016": 3,
    "MF-S017": 4,
    "MF-S013": 5,
    "MF-S014": 6,
    "MF-S015": 7,
}


class SourceLocation(BaseModel):
    """One-based source location using Unicode code point columns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    line: StrictInt = Field(ge=1)
    column: StrictInt = Field(ge=1)
    end_line: StrictInt | None = Field(default=None, ge=1)
    end_column: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _end_not_before_start(self) -> SourceLocation:
        if self.end_line is None and self.end_column is not None:
            raise ValueError("end_column requires end_line")
        if self.end_line is not None and self.end_column is None:
            raise ValueError("end_line requires end_column")
        if self.end_line is not None and self.end_column is not None:
            if (self.end_line, self.end_column) < (self.line, self.column):
                raise ValueError("end location must not precede start location")
        return self


class SourceReference(BaseModel):
    """Reference to a source field and optional source location."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_path: StrictStr
    field_path: StrictStr
    location: SourceLocation | None = None

    @field_validator("logical_path")
    @classmethod
    def _logical_path_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("logical_path must be nonblank")
        return value

    @field_validator("field_path")
    @classmethod
    def _field_path_valid(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("field_path must be an RFC 6901 absolute pointer")
        return value


DiagnosticFieldValue = StrictStr | StrictInt | float | StrictBool


class DiagnosticField(BaseModel):
    """Immutable bounded scalar diagnostic field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: StrictStr
    value: DiagnosticFieldValue

    @field_validator("key")
    @classmethod
    def _key_valid(cls, value: str) -> str:
        return validate_lower_field_key(value)

    @field_validator("value")
    @classmethod
    def _value_valid(
        cls, value: DiagnosticFieldValue, info: ValidationInfo
    ) -> DiagnosticFieldValue:
        if isinstance(value, str):
            key = info.data.get("key")
            field_name = key if isinstance(key, str) else "value"
            policy = RedactionPolicy()
            if detect_secret_candidate(
                field_path=f"/{field_name}",
                field_name=field_name,
                value=value,
                policy=policy,
            ):
                return policy.replacement
            redacted = redact_diagnostic_text(value, policy=policy)
            return _truncate_utf8(redacted, MAX_DIAGNOSTIC_FIELD_STRING_UTF8)
        if isinstance(value, float) and not value.is_integer() and not (value == value):
            raise ValueError("value must be finite")
        if isinstance(value, float) and not (float("-inf") < value < float("inf")):
            raise ValueError("value must be finite")
        return value

    def __str__(self) -> str:
        return repr(self)

    def __repr__(self) -> str:
        value = (
            redact_diagnostic_text(self.value)
            if isinstance(self.value, str)
            else self.value
        )
        return f"DiagnosticField(key={self.key!r}, value={value!r})"


class CompilerDiagnostic(BaseModel):
    """Closed immutable compiler diagnostic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: StrictStr
    severity: DiagnosticSeverity
    phase: CompilerPhase
    message: StrictStr
    source_reference: SourceReference | None = None
    harness_id: StrictStr | None = None
    node_id: StrictStr | None = None
    related_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    suggested_fix: StrictStr | None = None
    fields: tuple[DiagnosticField, ...] = Field(default_factory=tuple)

    @field_validator("message")
    @classmethod
    def _message_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must be nonblank")
        return validate_utf8_size(
            redact_diagnostic_text(value), "message", MAX_DIAGNOSTIC_MESSAGE_UTF8
        )

    @field_validator("harness_id")
    @classmethod
    def _harness_id_valid(cls, value: str | None) -> str | None:
        return None if value is None else validate_harness_id(value)

    @field_validator("node_id")
    @classmethod
    def _node_id_valid(cls, value: str | None) -> str | None:
        return None if value is None else validate_node_id(value)

    @field_validator("related_ids")
    @classmethod
    def _related_ids_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_RELATED_IDS:
            raise ValueError("related_ids may contain at most 16 entries")
        for item in value:
            if not item.strip():
                raise ValueError("related_ids must be nonblank")
        return value

    @field_validator("suggested_fix")
    @classmethod
    def _suggested_fix_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("suggested_fix must be nonblank")
        return validate_utf8_size(
            redact_diagnostic_text(value),
            "suggested_fix",
            MAX_DIAGNOSTIC_MESSAGE_UTF8,
        )

    @field_validator("fields")
    @classmethod
    def _fields_valid(
        cls, value: tuple[DiagnosticField, ...]
    ) -> tuple[DiagnosticField, ...]:
        if len(value) > MAX_DIAGNOSTIC_FIELDS:
            raise ValueError("fields may contain at most 16 entries")
        keys = tuple(field.key for field in value)
        if len(set(keys)) != len(keys):
            raise ValueError("diagnostic field keys must be unique")
        return value

    @model_validator(mode="after")
    def _registry_matches(self) -> CompilerDiagnostic:
        expected = DIAGNOSTIC_REGISTRY.get(self.code)
        if expected is None:
            raise ValueError("diagnostic code is not registered")
        if (self.phase, self.severity) != expected:
            raise ValueError("diagnostic phase and severity must match registry")
        return self

    def __str__(self) -> str:
        return repr(self)

    def __repr__(self) -> str:
        return (
            "CompilerDiagnostic("
            f"code={self.code!r}, severity={self.severity.value!r}, "
            f"phase={self.phase.value!r}, message={redact_diagnostic_text(self.message)!r}, "
            f"source_reference={self.source_reference!r}, harness_id={self.harness_id!r}, "
            f"node_id={self.node_id!r}, related_ids={self.related_ids!r}, "
            f"suggested_fix={redact_diagnostic_text(self.suggested_fix) if self.suggested_fix is not None else None!r}, "
            f"fields={self.fields!r})"
        )


def diagnostic_sort_key(
    diagnostic: CompilerDiagnostic,
) -> tuple[int, int, int, int, str, str, str, str]:
    """Return the deterministic diagnostic ordering key."""
    phase_order = tuple(CompilerPhase)
    source = diagnostic.source_reference
    location = source.location if source is not None else None
    if diagnostic.phase == CompilerPhase.REQUEST:
        return (
            phase_order.index(diagnostic.phase),
            _REQUEST_DIAGNOSTIC_ORDER.get(
                diagnostic.code, len(_REQUEST_DIAGNOSTIC_ORDER)
            ),
            location.line if location is not None else 0,
            location.column if location is not None else 0,
            source.field_path if source is not None else "",
            diagnostic.code,
            diagnostic.node_id or "",
            diagnostic.message,
        )
    return (
        phase_order.index(diagnostic.phase),
        0,
        location.line if location is not None else 0,
        location.column if location is not None else 0,
        source.field_path if source is not None else "",
        diagnostic.code,
        diagnostic.node_id or "",
        diagnostic.message,
    )


def sort_diagnostics(
    diagnostics: Sequence[CompilerDiagnostic],
) -> tuple[CompilerDiagnostic, ...]:
    """Sort diagnostics according to the compiler contract."""
    return tuple(sorted(diagnostics, key=diagnostic_sort_key))


def bound_diagnostics(
    diagnostics: Sequence[CompilerDiagnostic],
) -> tuple[CompilerDiagnostic, ...]:
    """Sort and truncate diagnostics to the fixed report bound."""
    ordered = list(sort_diagnostics(diagnostics))
    if (
        len(ordered) <= MAX_DIAGNOSTICS_PER_REPORT
        and _diagnostic_report_size_bytes(ordered) <= MAX_DIAGNOSTIC_REPORT_BYTES
    ):
        return tuple(ordered)

    warning = _diagnostics_truncated_warning()
    kept = [diagnostic for diagnostic in ordered if diagnostic.code != "MF-D001"]
    kept = kept[:MAX_DIAGNOSTICS_WITHOUT_TRUNCATION_WARNING]
    while True:
        candidate = tuple((*kept, warning))
        if _diagnostic_report_size_bytes(candidate) <= MAX_DIAGNOSTIC_REPORT_BYTES:
            return candidate
        if not kept:
            return (warning,)
        kept.pop()


def detect_secret_candidate(
    *,
    field_path: str,
    field_name: str,
    value: str,
    policy: RedactionPolicy,
) -> bool:
    """Return whether a scalar resembles a secret without exposing it."""
    lowered = f"{field_path}/{field_name}".lower()
    compact = lowered.replace("_", "").replace("-", "")
    marker_match = any(
        marker in lowered or marker.replace("-", "") in compact
        for marker in policy.sensitive_field_markers
    )
    if marker_match and value.strip():
        return True
    return any(pattern.search(value) is not None for pattern in _SECRET_PATTERNS)


def _truncate_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    suffix = _TRUNCATED_SUFFIX.encode("utf-8")
    prefix = encoded[: max(0, maximum - len(suffix))].decode("utf-8", errors="ignore")
    return f"{prefix}{_TRUNCATED_SUFFIX}"


def _diagnostic_report_size_bytes(
    diagnostics: Sequence[CompilerDiagnostic],
) -> int:
    payload = [diagnostic.model_dump(mode="json") for diagnostic in diagnostics]
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return len(serialized.encode("utf-8"))


def _diagnostics_truncated_warning() -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code="MF-D001",
        phase=CompilerPhase.INTERNAL,
        severity=DiagnosticSeverity.WARNING,
        message=_DIAGNOSTICS_TRUNCATED_MESSAGE,
    )
