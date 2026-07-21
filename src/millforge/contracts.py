"""Contract models for the Millforge runtime.

All models are defined using Pydantic v2 APIs with ``extra="forbid"``
(closed-world) validation. Immutable models use ``frozen=True``;
mutable working models are explicitly noted in their docstrings.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Callable,
    Dict,
    Literal,
    Optional,
    Self,
    Tuple,
    TypeAlias,
)
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_serializer,
    model_validator,
)

from millforge.compiled_plan import (
    CompiledArtifactPolicy,
    DiagnosticField,
    IdempotencyClass,
    SessionEvent,
    SideEffectCertainty,
    SideEffectClass,
    StageIdentity,
    ToolBindingRef,
    ToolExecutionStatus,
    ToolTraceRecord,
    canonical_json_serialize,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SANITIZED_METADATA_MAX_ITEMS = 32
_SANITIZED_METADATA_KEY_MAX_LENGTH = 64
_SANITIZED_METADATA_STRING_MAX_LENGTH = 2048
_SANITIZED_METADATA_BYTES_MAX_LENGTH = 32768
_REDACTION_DEFAULT_DEPTH = 8
_REDACTION_DEFAULT_COLLECTION_ITEMS = 64
_REDACTION_DEFAULT_STRING_LENGTH = 2048
_REDACTION_DEFAULT_TOTAL_BYTES = 32768
_REDACTION_MAX_DEPTH = 32
_REDACTION_MAX_COLLECTION_ITEMS = 1024
_REDACTION_MAX_STRING_LENGTH = 64 * 1024 * 1024
_REDACTION_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY)[A-Z0-9_]*=([^\s]+)"
    ),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^&\s]+)"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"),
    re.compile(r"\b(sk|pk|org|sess)-[a-zA-Z0-9]{8,}\b"),
)
_URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>'\"]+", re.IGNORECASE)


def _nonblank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _unique(values: tuple[str, ...], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} values must be unique")


def _validate_sha256(value: str, field_name: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be exactly 64 lowercase hex characters")
    return value


def _is_sensitive_field_name(value: str, policy: RedactionPolicy) -> bool:
    lowered = value.lower()
    compact = lowered.replace("_", "").replace("-", "")
    return any(
        marker in lowered or marker.replace("-", "") in compact
        for marker in policy.sensitive_field_markers
    )


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = Any
JsonObject: TypeAlias = dict[str, JsonValue]
SanitizedMetadataValue = JsonScalar

# Public global ceilings for one invocation-local selected JSON output.
MAX_SELECTED_OUTPUT_SCHEMA_BYTES = 64 * 1024
MAX_SELECTED_OUTPUT_PAYLOAD_BYTES = 1024 * 1024
MAX_SELECTED_OUTPUT_NESTING_DEPTH = 16
MAX_SELECTED_OUTPUT_OBJECT_PROPERTIES = 64
MAX_SELECTED_OUTPUT_ARRAY_ITEMS = 1024
MAX_SELECTED_OUTPUT_STRING_LENGTH = 64 * 1024

_SELECTED_OUTPUT_TYPES = {
    "object",
    "array",
    "string",
    "integer",
    "number",
    "boolean",
    "null",
}
_SELECTED_OUTPUT_SCHEMA_KEYWORDS = {
    "object": {"type", "properties", "required", "additionalProperties"},
    "array": {"type", "items", "minItems", "maxItems"},
    "string": {"type", "minLength", "maxLength"},
    "integer": {"type"},
    "number": {"type"},
    "boolean": {"type"},
    "null": {"type"},
}
_SELECTED_OUTPUT_VALUE_CONSTRAINTS = {"const", "enum"}


class _FrozenSelectedOutputDict(dict[str, Any]):
    """Internal recursively frozen dict that retains JSON serialization shape."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("selected output authority is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable  # type: ignore[assignment]
    setdefault = _immutable
    update = _immutable  # type: ignore[assignment]
    __ior__ = _immutable  # type: ignore[assignment]


class _FrozenSelectedOutputList(list[Any]):
    """Internal recursively frozen list that retains JSON serialization shape."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("selected output authority is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable  # type: ignore[assignment]
    __imul__ = _immutable  # type: ignore[assignment]
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _freeze_selected_output_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return _FrozenSelectedOutputDict(
            {key: _freeze_selected_output_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return _FrozenSelectedOutputList(
            _freeze_selected_output_json(item) for item in value
        )
    return value


def _reject_selected_output_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"selected output JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_selected_output_constant(value: str) -> None:
    raise ValueError(f"selected output JSON contains non-finite number {value}")


def _parse_selected_output_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("selected output JSON contains a non-finite number")
    return parsed


def _parse_selected_output_json(raw: str | bytes, *, field_name: str) -> JsonValue:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{field_name} must be UTF-8 JSON") from exc
    else:
        text = raw
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_selected_output_duplicate_keys,
            parse_constant=_reject_selected_output_constant,
            parse_float=_parse_selected_output_float,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be strict JSON: {exc}") from exc


class _RawJsonObject(list[tuple[str, Any]]):
    """A JSON object represented as its ordered raw key/value pairs."""


def _parse_json_objects_preserving_pairs(
    raw: str | bytes | bytearray,
) -> JsonValue | None:
    """Parse JSON while retaining object key pairs for strict raw validation."""
    try:
        return json.loads(raw, object_pairs_hook=_RawJsonObject)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        # Let Pydantic preserve its normal raw-JSON validation behavior.
        return None


def _validate_raw_json_strictness(value: JsonValue, *, field_name: str) -> None:
    """Reject duplicate object keys and non-finite numbers before conversion."""
    if isinstance(value, _RawJsonObject):
        seen: set[str] = set()
        for key, item in value:
            if key in seen:
                raise ValueError(f"{field_name} contains duplicate key {key!r}")
            seen.add(key)
            _validate_raw_json_strictness(item, field_name=field_name)
    elif isinstance(value, list):
        for item in value:
            _validate_raw_json_strictness(item, field_name=field_name)
    elif type(value) is float and not math.isfinite(value):
        raise ValueError(f"{field_name} contains a non-finite number")


def _validate_harness_request_raw_json(
    raw: str | bytes | bytearray,
) -> None:
    """Reject ambiguous or non-finite values throughout a raw request."""
    parsed = _parse_json_objects_preserving_pairs(raw)
    if parsed is None:
        return
    _validate_raw_json_strictness(parsed, field_name="request JSON")


def _validate_selected_output_bound(
    schema: Mapping[str, Any],
    *,
    minimum_key: str,
    maximum_key: str,
    global_maximum: int,
) -> None:
    minimum = schema.get(minimum_key, 0)
    maximum = schema.get(maximum_key, global_maximum)
    if type(minimum) is not int or type(maximum) is not int:
        raise ValueError(f"{minimum_key} and {maximum_key} must be integers")
    if minimum < 0 or maximum < 0:
        raise ValueError(f"{minimum_key} and {maximum_key} must be non-negative")
    if minimum > maximum:
        raise ValueError(f"{minimum_key} must not exceed {maximum_key}")
    if maximum > global_maximum:
        raise ValueError(f"{maximum_key} exceeds the selected output global ceiling")


def _normalize_selected_output_scalar(value: Any, *, field_name: str) -> JsonScalar:
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{field_name} contains a non-finite number")
        return value
    if type(value) is str:
        if len(value) > MAX_SELECTED_OUTPUT_STRING_LENGTH:
            raise ValueError(f"{field_name} string exceeds the string ceiling")
        return value
    raise ValueError(f"{field_name} must contain strict JSON scalar values")


def _selected_output_scalar_matches_type(value: JsonScalar, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "null":
        return value is None
    return False


def _canonical_selected_output_scalar_bytes(value: JsonScalar) -> bytes:
    return canonical_json_serialize(value).encode("utf-8")


def _normalize_selected_output_schema(
    schema: Any,
    *,
    depth: int,
) -> JsonObject:
    if depth > MAX_SELECTED_OUTPUT_NESTING_DEPTH:
        raise ValueError("selected output schema exceeds the nesting-depth ceiling")
    if not isinstance(schema, Mapping):
        raise ValueError("every selected output schema node must be a JSON object")
    if any(not isinstance(key, str) for key in schema):
        raise ValueError("selected output schema object keys must be strings")

    schema_type = schema.get("type")
    constraints = _SELECTED_OUTPUT_VALUE_CONSTRAINTS.intersection(schema)
    if len(constraints) > 1:
        raise ValueError("selected output schema cannot contain both const and enum")
    if schema_type is None:
        if len(constraints) != 1:
            raise ValueError(
                "selected output schema type is outside the admitted subset"
            )
        unsupported = set(schema) - constraints
        if unsupported:
            rendered = ", ".join(sorted(unsupported))
            raise ValueError(
                f"unsupported selected output schema keyword(s): {rendered}"
            )
        normalized: JsonObject = {}
    elif not isinstance(schema_type, str) or schema_type not in _SELECTED_OUTPUT_TYPES:
        raise ValueError("selected output schema type is outside the admitted subset")
    else:
        unsupported = set(schema) - (
            _SELECTED_OUTPUT_SCHEMA_KEYWORDS[schema_type]
            | _SELECTED_OUTPUT_VALUE_CONSTRAINTS
        )
        if unsupported:
            rendered = ", ".join(sorted(unsupported))
            raise ValueError(
                f"unsupported selected output schema keyword(s): {rendered}"
            )
        normalized = {"type": schema_type}

    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            raise ValueError("object schemas require additionalProperties=false")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, Mapping):
            raise ValueError("object schema properties must be a JSON object")
        if any(not isinstance(key, str) for key in properties):
            raise ValueError("selected output property names must be strings")
        if len(properties) > MAX_SELECTED_OUTPUT_OBJECT_PROPERTIES:
            raise ValueError(
                "selected output schema exceeds the object-property ceiling"
            )
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            raise ValueError("object schema required must be an array of strings")
        if len(set(required)) != len(required):
            raise ValueError("object schema required values must be unique")
        unknown_required = set(required) - set(properties)
        if unknown_required:
            raise ValueError(
                "object schema required values must name declared properties"
            )
        for property_name in properties:
            if len(property_name) > MAX_SELECTED_OUTPUT_STRING_LENGTH:
                raise ValueError("selected output property name exceeds string ceiling")
        normalized["properties"] = {
            key: _normalize_selected_output_schema(value, depth=depth + 1)
            for key, value in properties.items()
        }
        normalized["required"] = sorted(required)
        normalized["additionalProperties"] = False
    elif schema_type == "array":
        if "items" not in schema:
            raise ValueError("array schemas require an items schema")
        _validate_selected_output_bound(
            schema,
            minimum_key="minItems",
            maximum_key="maxItems",
            global_maximum=MAX_SELECTED_OUTPUT_ARRAY_ITEMS,
        )
        normalized["items"] = _normalize_selected_output_schema(
            schema["items"],
            depth=depth + 1,
        )
        if "minItems" in schema:
            normalized["minItems"] = schema["minItems"]
        if "maxItems" in schema:
            normalized["maxItems"] = schema["maxItems"]
    elif schema_type == "string":
        _validate_selected_output_bound(
            schema,
            minimum_key="minLength",
            maximum_key="maxLength",
            global_maximum=MAX_SELECTED_OUTPUT_STRING_LENGTH,
        )
        if "minLength" in schema:
            normalized["minLength"] = schema["minLength"]
        if "maxLength" in schema:
            normalized["maxLength"] = schema["maxLength"]

    if "const" in constraints:
        const = _normalize_selected_output_scalar(
            schema["const"], field_name="selected output const"
        )
        if schema_type is not None and not _selected_output_scalar_matches_type(
            const, schema_type
        ):
            raise ValueError("selected output const does not satisfy schema type")
        normalized["const"] = const
    elif "enum" in constraints:
        raw_enum = schema["enum"]
        if not isinstance(raw_enum, list):
            raise ValueError("selected output enum must be a JSON array")
        if not 1 <= len(raw_enum) <= 64:
            raise ValueError(
                "selected output enum must contain between 1 and 64 values"
            )
        canonical_values: list[tuple[bytes, JsonScalar]] = []
        seen: set[bytes] = set()
        for value in raw_enum:
            normalized_value = _normalize_selected_output_scalar(
                value, field_name="selected output enum"
            )
            if schema_type is not None and not _selected_output_scalar_matches_type(
                normalized_value, schema_type
            ):
                raise ValueError(
                    "selected output enum value does not satisfy schema type"
                )
            canonical = _canonical_selected_output_scalar_bytes(normalized_value)
            if canonical in seen:
                raise ValueError("selected output enum values must be unique")
            seen.add(canonical)
            canonical_values.append((canonical, normalized_value))
        normalized["enum"] = [
            value
            for _canonical, value in sorted(canonical_values, key=lambda item: item[0])
        ]
    return normalized


def canonical_selected_output_schema_bytes(
    schema: str | bytes | Mapping[str, Any],
) -> bytes:
    """Admit and canonically serialize the closed selected-schema subset."""
    if isinstance(schema, (str, bytes)):
        raw_size = (
            len(schema.encode("utf-8")) if isinstance(schema, str) else len(schema)
        )
        if raw_size > MAX_SELECTED_OUTPUT_SCHEMA_BYTES:
            raise ValueError("selected output schema exceeds the schema-byte ceiling")
        parsed = _parse_selected_output_json(
            schema, field_name="selected output schema"
        )
    else:
        parsed = schema
    normalized = _normalize_selected_output_schema(parsed, depth=1)
    canonical = canonical_json_serialize(normalized).encode("utf-8")
    if len(canonical) > MAX_SELECTED_OUTPUT_SCHEMA_BYTES:
        raise ValueError("selected output schema exceeds the schema-byte ceiling")
    return canonical


def selected_output_schema_sha256(
    schema: str | bytes | Mapping[str, Any],
) -> str:
    """Return the SHA-256 digest of an admitted canonical selected schema."""
    return hashlib.sha256(canonical_selected_output_schema_bytes(schema)).hexdigest()


def _normalize_selected_output_payload(value: Any, *, depth: int) -> JsonValue:
    if depth > MAX_SELECTED_OUTPUT_NESTING_DEPTH:
        raise ValueError("selected output payload exceeds the nesting-depth ceiling")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("selected output payload contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > MAX_SELECTED_OUTPUT_STRING_LENGTH:
            raise ValueError(
                "selected output payload string exceeds the string ceiling"
            )
        return value
    if isinstance(value, list):
        if len(value) > MAX_SELECTED_OUTPUT_ARRAY_ITEMS:
            raise ValueError("selected output payload exceeds the array-item ceiling")
        return [
            _normalize_selected_output_payload(item, depth=depth + 1) for item in value
        ]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("selected output payload object keys must be strings")
        if len(value) > MAX_SELECTED_OUTPUT_OBJECT_PROPERTIES:
            raise ValueError(
                "selected output payload exceeds the object-property ceiling"
            )
        for key in value:
            if len(key) > MAX_SELECTED_OUTPUT_STRING_LENGTH:
                raise ValueError(
                    "selected output payload key exceeds the string ceiling"
                )
        return {
            key: _normalize_selected_output_payload(item, depth=depth + 1)
            for key, item in value.items()
        }
    raise ValueError("selected output payload contains a non-JSON value")


def canonical_selected_output_payload_bytes(value: JsonValue) -> bytes:
    """Validate global JSON bounds and return canonical selected payload bytes."""
    normalized = _normalize_selected_output_payload(value, depth=1)
    canonical = canonical_json_serialize(normalized).encode("utf-8")
    if len(canonical) > MAX_SELECTED_OUTPUT_PAYLOAD_BYTES:
        raise ValueError("selected output payload exceeds the payload-byte ceiling")
    return canonical


def parse_selected_output_payload_json(raw: str | bytes) -> JsonValue:
    """Parse strict JSON, rejecting duplicate keys and all global bound violations."""
    raw_size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if raw_size > MAX_SELECTED_OUTPUT_PAYLOAD_BYTES:
        raise ValueError("selected output payload exceeds the payload-byte ceiling")
    parsed = _parse_selected_output_json(raw, field_name="selected output payload")
    canonical_selected_output_payload_bytes(parsed)
    return parsed


# ---------------------------------------------------------------------------
# Closed enums
# ---------------------------------------------------------------------------


class ExecutionStatus(str, Enum):
    """Closed enum of possible execution statuses for a run or session."""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ExecutionResultClass(str, Enum):
    """Closed enum of possible execution result classifications."""

    DOMAIN_TERMINAL = "domain_terminal"
    DOMAIN_REJECTED = "domain_rejected"
    BINDING_REJECTED = "binding_rejected"
    COMPILED_HARNESS_INVALID = "compiled_harness_invalid"
    BACKEND_FAILURE = "backend_failure"
    MODEL_FAILURE = "model_failure"
    TOOL_FAILURE = "tool_failure"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    TERMINAL_RESULT_INVALID = "terminal_result_invalid"
    ARTIFACT_FINALIZATION_FAILED = "artifact_finalization_failed"
    INTERNAL_FAILURE = "internal_failure"


class TerminalCertainty(str, Enum):
    """Certainty of terminal-result commit ordering."""

    NOT_APPLICABLE = "not_applicable"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


class GuardedSessionStatus(str, Enum):
    """Closed enum of possible guarded session statuses."""

    TERMINAL = "terminal"
    REJECTED = "rejected"
    BACKEND_FAILED = "backend_failed"
    MODEL_FAILED = "model_failed"
    TOOL_FAILED = "tool_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PREREQUISITE_BUDGET_EXHAUSTED = "prerequisite_budget_exhausted"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INVALID_TERMINAL = "invalid_terminal"


class TimeoutOrigin(str, Enum):
    """Closed timeout origins that preserve the stable timeout result class."""

    SESSION_DEADLINE = "session_deadline"
    MODEL_CONNECT_TIMEOUT = "model_connect_timeout"
    MODEL_READ_TIMEOUT = "model_read_timeout"
    MODEL_WRITE_TIMEOUT = "model_write_timeout"
    MODEL_POOL_TIMEOUT = "model_pool_timeout"
    TOOL_TIMEOUT = "tool_timeout"
    BACKEND_TIMEOUT = "backend_timeout"
    ARTIFACT_FINALIZATION_TIMEOUT = "artifact_finalization_timeout"
    CLEANUP_TIMEOUT = "cleanup_timeout"


# ---------------------------------------------------------------------------
# New standalone types
# ---------------------------------------------------------------------------


class Deadline(BaseModel):
    """Immutable monotonic deadline specification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    started_monotonic: float = Field(
        ge=0, description="Monotonic time when deadline evaluation started"
    )
    outer_deadline_monotonic: float = Field(
        ge=0, description="Outer request deadline as monotonic seconds"
    )
    compiled_harness_deadline_monotonic: float | None = Field(
        default=None,
        ge=0,
        description="Optional smaller compiled harness deadline as monotonic seconds",
    )
    effective_deadline_monotonic: float = Field(
        ge=0, description="Effective deadline after all bounds are applied"
    )
    source: Literal["request", "compiled_harness", "request_and_harness"] = Field(
        description="Source used to derive the effective deadline"
    )

    @model_validator(mode="after")
    def _check_deadline_ordering(self) -> Deadline:
        if self.outer_deadline_monotonic < self.started_monotonic:
            raise ValueError("outer_deadline_monotonic must not precede start")
        if self.effective_deadline_monotonic < self.started_monotonic:
            raise ValueError("effective_deadline_monotonic must not precede start")
        if self.effective_deadline_monotonic > self.outer_deadline_monotonic:
            raise ValueError(
                "effective_deadline_monotonic must not exceed outer deadline"
            )
        if self.compiled_harness_deadline_monotonic is not None:
            if self.compiled_harness_deadline_monotonic < self.started_monotonic:
                raise ValueError(
                    "compiled_harness_deadline_monotonic must not precede start"
                )
            expected = min(
                self.outer_deadline_monotonic,
                self.compiled_harness_deadline_monotonic,
            )
            if self.effective_deadline_monotonic != expected:
                raise ValueError(
                    "effective_deadline_monotonic must equal the smaller request "
                    "or compiled harness deadline"
                )
            if self.source == "request":
                raise ValueError(
                    "source=request is invalid when compiled harness deadline is present"
                )
        elif self.effective_deadline_monotonic != self.outer_deadline_monotonic:
            raise ValueError(
                "effective_deadline_monotonic must equal outer deadline when no "
                "compiled harness deadline is present"
            )
        return self

    @property
    def request_deadline_monotonic(self) -> float:
        """Return the absolute request deadline in monotonic seconds."""
        return self.outer_deadline_monotonic

    def remaining(self, clock: Callable[[], float] | Any) -> float:
        """Return non-negative seconds remaining against the effective deadline."""
        now = clock() if callable(clock) else clock.monotonic()
        return max(0.0, self.effective_deadline_monotonic - float(now))

    @classmethod
    def from_deadlines(
        cls,
        *,
        started_monotonic: float,
        request_deadline_monotonic: float,
        compiled_harness_deadline_monotonic: float | None = None,
    ) -> Deadline:
        """Build a deadline with effective time derived from admitted bounds."""
        if compiled_harness_deadline_monotonic is None:
            source: Literal["request", "compiled_harness", "request_and_harness"] = (
                "request"
            )
            effective = request_deadline_monotonic
        else:
            effective = min(
                request_deadline_monotonic,
                compiled_harness_deadline_monotonic,
            )
            source = (
                "compiled_harness"
                if compiled_harness_deadline_monotonic < request_deadline_monotonic
                else "request_and_harness"
            )
        return cls(
            started_monotonic=started_monotonic,
            outer_deadline_monotonic=request_deadline_monotonic,
            compiled_harness_deadline_monotonic=compiled_harness_deadline_monotonic,
            effective_deadline_monotonic=effective,
            source=source,
        )


class TokenUsage(BaseModel):
    """Immutable token usage breakdown for a model interaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0, description="Number of input (prompt) tokens")
    output_tokens: int = Field(ge=0, description="Number of output (completion) tokens")
    total_tokens: int = Field(ge=0, description="Total tokens consumed")
    provider_reported: bool = Field(
        description="Whether the usage was reported by the provider"
    )

    @model_validator(mode="after")
    def _total_matches_parts(self) -> TokenUsage:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


# ---------------------------------------------------------------------------
# Secret reference
# ---------------------------------------------------------------------------


class SecretRef(BaseModel):
    """Reference to a secret stored outside the contract boundary.

    Stores an opaque handle (``secret_id``) and the environment-variable
    name that resolves to the secret value. **The secret value itself
    must never appear in any contract field or serialization output.**
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    secret_id: str = Field(description="Unique secret identifier")
    env_var: str = Field(description="Environment variable name holding the secret")

    @field_validator("secret_id")
    @classmethod
    def _secret_id_must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("secret_id must be a non-empty string")
        return v

    @field_validator("env_var")
    @classmethod
    def _env_var_must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("env_var must be a non-empty string")
        return v


# ---------------------------------------------------------------------------
# Identity / Reference models (immutable snapshots)
# ---------------------------------------------------------------------------


class CompiledHarnessIdentity(BaseModel):
    """Immutable identity of a compiled harness plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    compiled_plan_id: str = Field(description="Unique identifier for the compiled plan")
    harness_id: str = Field(description="Harness identifier")
    harness_version: int = Field(gt=0, description="Positive harness version")

    @field_validator("compiled_plan_id", "harness_id")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


class CompiledHarnessHash(BaseModel):
    """Immutable cryptographic hash of a compiled harness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: Literal["sha256"] = Field(description="Hash algorithm (sha256 only)")
    digest: str = Field(description="Hex-encoded digest value")

    @field_validator("digest")
    @classmethod
    def _digest_valid(cls, value: str) -> str:
        return _validate_sha256(value, "digest")


class CompiledHarnessRef(BaseModel):
    """Immutable reference to a compiled harness, including identity, path, and hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: CompiledHarnessIdentity = Field(description="Compiled harness identity")
    path: Path = Field(description="Filesystem path to the compiled harness")
    expected_hash: CompiledHarnessHash = Field(
        description="Expected cryptographic hash of the harness"
    )


class RunDirRef(BaseModel):
    """Immutable reference to a run directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(description="Unique run identifier")
    path: Path = Field(description="Absolute or relative path to the run directory")

    @field_validator("run_id")
    @classmethod
    def _run_id_nonblank(cls, value: str) -> str:
        return _nonblank(value, "run_id")


class ArtifactRef(BaseModel):
    """Immutable reference to an artifact file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(description="Unique artifact identifier")
    path: Path = Field(description="Path to the artifact file")
    content_type: Optional[str] = Field(
        default=None, description="MIME type or content format"
    )

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id_nonblank(cls, value: str) -> str:
        return _nonblank(value, "artifact_id")

    @field_validator("content_type")
    @classmethod
    def _content_type_nonblank(cls, value: str | None) -> str | None:
        return None if value is None else _nonblank(value, "content_type")


class HarnessTaskInput(BaseModel):
    """Exact bounded instruction supplied to an executable harness request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1.0"] = "1.0"
    instruction: str

    @field_validator("instruction")
    @classmethod
    def _instruction_is_bounded(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "instruction must be non-empty after whitespace inspection"
            )
        if "\x00" in value:
            raise ValueError("instruction must not contain NUL")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("instruction must be valid UTF-8 text") from exc
        if len(encoded) > 65_536:
            raise ValueError("instruction must not exceed 65,536 UTF-8 bytes")
        return value

    @property
    def utf8_byte_count(self) -> int:
        """Return the exact UTF-8 encoded instruction size."""
        return len(self.instruction.encode("utf-8"))

    @property
    def sha256(self) -> str:
        """Return the lowercase SHA-256 of the exact UTF-8 instruction bytes."""
        return hashlib.sha256(self.instruction.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Stage identity
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Capability models
# ---------------------------------------------------------------------------

CAPABILITY_GRANT_CONSTRAINTS_DESC = (
    "Optional constraints applied to this capability grant"
)


class CapabilityGrant(BaseModel):
    """Immutable capability grant with capability identifier and optional constraints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(description="Capability identifier")
    constraints: Optional[Dict[str, Any]] = Field(
        default=None, description=CAPABILITY_GRANT_CONSTRAINTS_DESC
    )

    @field_validator("capability_id")
    @classmethod
    def _capability_id_nonblank(cls, value: str) -> str:
        return _nonblank(value, "capability_id")


class CapabilityEnvelope(BaseModel):
    """Immutable capability grant envelope containing a tuple of grants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: Tuple[CapabilityGrant, ...] = Field(
        description="Tuple of capability grants"
    )

    @field_validator("grants")
    @classmethod
    def _grant_capabilities_unique(
        cls, value: tuple[CapabilityGrant, ...]
    ) -> tuple[CapabilityGrant, ...]:
        _unique(tuple(grant.capability_id for grant in value), "capability_id")
        return value


# ---------------------------------------------------------------------------
# Profile and reference models
# ---------------------------------------------------------------------------


class ModelProfileRef(BaseModel):
    """Immutable reference to a model profile.

    Contains only the profile identifier — model provider, name, and
    details are resolved externally from the profile configuration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(description="Model profile identifier")

    @field_validator("profile_id")
    @classmethod
    def _profile_id_nonblank(cls, value: str) -> str:
        return _nonblank(value, "profile_id")


class TimeoutRef(BaseModel):
    """Immutable timeout reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(description="Timeout duration in seconds")
    deadline: Optional[str] = Field(
        default=None, description="ISO-8601 deadline timestamp"
    )

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("timeout_seconds must be a positive number")
        return v


class CancellationRef(BaseModel):
    """Immutable cancellation reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cancellation_id: str = Field(description="Cancellation identifier")

    @field_validator("cancellation_id")
    @classmethod
    def _cancellation_id_must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("cancellation_id must be a non-empty string")
        return v


# ---------------------------------------------------------------------------
class ConnectorApprovalGrant(BaseModel):
    """Runtime-only grant authorizing one exact connector approval scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector_id: str = Field(description="Connector identity authorized by runtime")
    provider_tool_name: str = Field(description="Provider tool name authorized")
    tool_id: str = Field(description="Compiled connector tool identifier")
    tool_version: int = Field(ge=1, description="Compiled connector tool version")
    descriptor_sha256: str = Field(description="Compiled descriptor hash")
    request_id: str = Field(description="Runtime request identifier")
    run_id: str = Field(description="Runtime run identifier")
    stage: StageIdentity = Field(description="Runtime stage identity")
    approval_policy: Literal["millrace_explicit"] = Field(
        description="Approval policy authorized by the runtime"
    )
    expires_at_monotonic: float = Field(
        ge=0,
        description="Trusted monotonic timestamp after which the grant is invalid",
    )
    approval_id: str | None = Field(
        default=None, description="Operator or runtime approval identifier"
    )
    nonce: str | None = Field(
        default=None, description="Opaque runtime nonce for one approval grant"
    )

    @field_validator(
        "connector_id",
        "provider_tool_name",
        "tool_id",
        "request_id",
        "run_id",
        "approval_id",
        "nonce",
    )
    @classmethod
    def _grant_text_nonblank(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _nonblank(value, info.field_name)

    @field_validator("descriptor_sha256")
    @classmethod
    def _descriptor_hash_valid(cls, value: str) -> str:
        return _validate_sha256(value, "descriptor_sha256")

    @model_validator(mode="after")
    def _has_approval_identity(self) -> ConnectorApprovalGrant:
        if self.approval_id is None and self.nonce is None:
            raise ValueError("connector approval grant requires approval_id or nonce")
        return self


# Tool execution context
# ---------------------------------------------------------------------------


class ToolExecutionContext(BaseModel):
    """Contextual information passed to ``ToolExecutor.execute()``.

    Provides the execution environment — request identity, stage,
    run directory, capability envelope, timeout, and cancellation
    reference — as the second argument to ``ToolExecutor.execute()``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(description="Unique request identifier")
    run_id: str = Field(description="Run this request belongs to")
    stage: StageIdentity = Field(description="Stage identity")
    run_directory: RunDirRef = Field(description="Run directory reference")
    capability_envelope: CapabilityEnvelope = Field(
        description="Capability grant envelope"
    )
    timeout: TimeoutRef = Field(description="Timeout reference")
    cancellation: CancellationRef = Field(description="Cancellation reference")
    deadline: Deadline = Field(description="Deadline specification")
    workspace_root: Path | None = Field(
        default=None, description="Trusted workspace root supplied by runtime"
    )
    artifact_root: Path | None = Field(
        default=None, description="Trusted artifact root supplied by runtime"
    )
    compiled_artifact_policy: CompiledArtifactPolicy | None = Field(
        default=None,
        description="Compiled artifact declarations supplied by the runtime",
    )
    input_artifacts: Tuple[ArtifactRef, ...] = Field(
        default_factory=tuple,
        description="Runtime-supplied request input artifact references",
    )
    work_item_id: str | None = Field(
        default=None, description="Runtime-supplied active work item identifier"
    )
    cancellation_requested: bool = Field(
        default=False, description="Trusted pre-entry cancellation state"
    )
    current_monotonic: float = Field(
        default=0.0,
        ge=0,
        description="Trusted monotonic timestamp used for pre-entry deadline checks",
    )
    connector_approval_grants: Tuple[ConnectorApprovalGrant, ...] = Field(
        default_factory=tuple,
        description="Runtime-only connector approval grants scoped to exact bindings",
    )


# ---------------------------------------------------------------------------
# Invocation-local selected output authority and admitted value
# ---------------------------------------------------------------------------


class SelectedOutputRequirement(BaseModel):
    """Immutable required/optional authority for one selected JSON output.

    ``json_schema`` may be supplied as a strict JSON string/bytes value or as a
    Python mapping. It is normalized to the documented closed subset and its
    canonical SHA-256 digest is pinned into the serialized request contract.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )

    required: StrictBool = Field(
        description="Whether the invocation must admit a present selected output"
    )
    json_schema: JsonObject = Field(description="Admitted closed selected JSON schema")
    schema_sha256: str = Field(
        default="",
        description="SHA-256 digest of the canonical admitted selected schema",
    )

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Any | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Validate raw requirement JSON without losing duplicate keys first."""
        parsed = _parse_json_objects_preserving_pairs(json_data)
        if parsed is not None:
            _validate_raw_json_strictness(parsed, field_name="selected output JSON")
        return super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )

    @model_validator(mode="before")
    @classmethod
    def _admit_and_pin_schema(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or "json_schema" not in value:
            return value
        canonical = canonical_selected_output_schema_bytes(value["json_schema"])
        admitted = json.loads(canonical)
        digest = hashlib.sha256(canonical).hexdigest()
        supplied_digest = value.get("schema_sha256")
        if supplied_digest not in (None, "", digest):
            raise ValueError("schema_sha256 does not match canonical selected schema")
        normalized = dict(value)
        normalized["json_schema"] = admitted
        normalized["schema_sha256"] = digest
        return normalized

    @field_validator("schema_sha256")
    @classmethod
    def _schema_digest_valid(cls, value: str) -> str:
        return _validate_sha256(value, "schema_sha256")

    @model_validator(mode="after")
    def _schema_digest_matches(self) -> SelectedOutputRequirement:
        if self.schema_sha256 != selected_output_schema_sha256(self.json_schema):
            raise ValueError("schema_sha256 does not match canonical selected schema")
        object.__setattr__(
            self,
            "json_schema",
            _freeze_selected_output_json(self.json_schema),
        )
        return self

    @property
    def canonical_schema_bytes(self) -> bytes:
        """Return the admitted schema as canonical UTF-8 JSON bytes."""
        return canonical_selected_output_schema_bytes(self.json_schema)


class TerminalSelectedOutputRequirement(BaseModel):
    """Immutable selected-output authority for one exact terminal result."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    terminal_result: str
    selected_output: SelectedOutputRequirement

    @field_validator("terminal_result")
    @classmethod
    def _terminal_result_is_nonblank(cls, value: str) -> str:
        return _nonblank(value, "terminal_result")


def _selected_output_requirements_by_terminal_result(
    requirements: tuple[TerminalSelectedOutputRequirement, ...],
) -> dict[str, SelectedOutputRequirement]:
    """Return the one canonical terminal-result lookup for selected output."""

    if len(requirements) > 64:
        raise ValueError("selected output requirements exceed the 64-record ceiling")
    lookup: dict[str, SelectedOutputRequirement] = {}
    for item in requirements:
        terminal_result = _nonblank(item.terminal_result, "terminal_result")
        if terminal_result in lookup:
            raise ValueError("selected output terminal_result values must be unique")
        lookup[terminal_result] = item.selected_output
    return dict(sorted(lookup.items(), key=lambda item: item[0].encode("utf-8")))


class SelectedOutputAbsent(BaseModel):
    """Explicit absence for an admitted optional selected-output authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    present: Literal[False] = False


class SelectedOutputPresent(BaseModel):
    """A present, globally bounded JSON value; ``value=None`` is JSON null."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )

    present: Literal[True] = True
    value: JsonValue = Field(description="Admitted JSON value, including JSON null")

    @field_validator("value", mode="before")
    @classmethod
    def _value_is_bounded_json(cls, value: Any) -> JsonValue:
        normalized = _normalize_selected_output_payload(value, depth=1)
        canonical_selected_output_payload_bytes(normalized)
        return _freeze_selected_output_json(normalized)


SelectedOutput: TypeAlias = Annotated[
    SelectedOutputAbsent | SelectedOutputPresent,
    Field(discriminator="present"),
]


def admit_selected_output(
    requirement: SelectedOutputRequirement,
    *,
    present: bool,
    value: Any = None,
) -> SelectedOutput:
    """Mechanically admit one invocation-local selected-output candidate.

    Absence is legal only for optional authorities.  Present values first pass
    the global payload ceilings owned by :class:`SelectedOutputPresent`, then
    the exact closed schema carried by ``requirement``.  No prose, artifact, or
    workspace fallback participates in admission.
    """

    if not present:
        if requirement.required:
            raise ValueError("required selected output candidate is missing")
        return SelectedOutputAbsent()

    candidate = SelectedOutputPresent(value=value)
    error = _selected_output_schema_error(
        candidate.value,
        requirement.json_schema,
        path="$",
    )
    if error is not None:
        raise ValueError(f"selected output candidate failed schema validation: {error}")
    return candidate


def _selected_output_schema_error(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str,
) -> str | None:
    """Return the first deterministic mismatch against the admitted subset."""

    if "const" in schema:
        if _canonical_selected_output_scalar_bytes(value) != (
            _canonical_selected_output_scalar_bytes(schema["const"])
        ):
            return f"{path} must equal const value"
    elif "enum" in schema:
        candidate = _canonical_selected_output_scalar_bytes(value)
        if all(
            candidate != _canonical_selected_output_scalar_bytes(item)
            for item in schema["enum"]
        ):
            return f"{path} must equal an enum value"

    schema_type = schema.get("type")
    if schema_type is None:
        return None
    if schema_type == "object":
        if not isinstance(value, Mapping):
            return f"{path} must be object"
        properties = schema["properties"]
        for key in schema["required"]:
            if key not in value:
                return f"{path}.{key} is required"
        extra = sorted(set(value) - set(properties))
        if extra:
            return f"{path}.{extra[0]} is not allowed"
        for key, item in value.items():
            error = _selected_output_schema_error(
                item,
                properties[key],
                path=f"{path}.{key}",
            )
            if error is not None:
                return error
        return None
    if schema_type == "array":
        if not isinstance(value, list):
            return f"{path} must be array"
        minimum = schema.get("minItems", 0)
        maximum = schema.get("maxItems", MAX_SELECTED_OUTPUT_ARRAY_ITEMS)
        if len(value) < minimum:
            return f"{path} has fewer than {minimum} items"
        if len(value) > maximum:
            return f"{path} has more than {maximum} items"
        for index, item in enumerate(value):
            error = _selected_output_schema_error(
                item,
                schema["items"],
                path=f"{path}[{index}]",
            )
            if error is not None:
                return error
        return None
    if schema_type == "string":
        if not isinstance(value, str):
            return f"{path} must be string"
        minimum = schema.get("minLength", 0)
        maximum = schema.get("maxLength", MAX_SELECTED_OUTPUT_STRING_LENGTH)
        if len(value) < minimum:
            return f"{path} is shorter than {minimum} characters"
        if len(value) > maximum:
            return f"{path} is longer than {maximum} characters"
        return None
    if schema_type == "integer":
        return (
            None
            if isinstance(value, int) and not isinstance(value, bool)
            else f"{path} must be integer"
        )
    if schema_type == "number":
        return (
            None
            if isinstance(value, int | float)
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
            else f"{path} must be finite number"
        )
    if schema_type == "boolean":
        return None if isinstance(value, bool) else f"{path} must be boolean"
    if schema_type == "null":
        return None if value is None else f"{path} must be null"
    raise AssertionError(f"unreachable selected output schema type: {schema_type}")


# ---------------------------------------------------------------------------
# Harness execution request (primary executable boundary)
# ---------------------------------------------------------------------------


class HarnessExecutionRequest(BaseModel):
    """Immutable executable boundary for harness execution.

    This is the primary input contract for ``HarnessRuntime.execute()``.
    ``stage`` is the provider-local identity admitted by the selected compiled
    Millforge harness; it does not carry a caller workflow plane, node, route,
    dispatch identity, or authority.  ``request_id`` and ``run_id`` are opaque
    caller-owned correlation values that Millforge validates and echoes without
    interpreting them as workflow or terminal authority.  Run IDs are checked
    for consistency and collection-level duplicates are rejected at
    construction time.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    request_id: str = Field(description="Opaque caller request correlation value")
    run_id: str = Field(description="Opaque caller run correlation value")
    work_item_id: str = Field(description="Active work item identifier")
    task: HarnessTaskInput = Field(description="Exact bounded task instruction")
    stage: StageIdentity = Field(
        description="Provider-local identity of the admitted compiled harness stage"
    )
    compiled_harness: CompiledHarnessRef = Field(
        description="Reference to the compiled harness"
    )
    capability_envelope: CapabilityEnvelope = Field(
        description="Capability grant envelope"
    )
    input_artifacts: Tuple[ArtifactRef, ...] = Field(
        description="Input artifact references"
    )
    run_directory: RunDirRef = Field(description="Run directory reference")
    timeout: TimeoutRef = Field(description="Timeout reference")
    cancellation: CancellationRef = Field(description="Cancellation reference")
    secret_refs: Tuple[SecretRef, ...] = Field(
        description="Secret references (handles only, never values)"
    )
    model_profile: ModelProfileRef = Field(description="Model profile reference")
    selected_output_requirements: tuple[TerminalSelectedOutputRequirement, ...] = Field(
        default_factory=tuple,
        max_length=64,
        description="Terminal-result-scoped selected JSON output requirements",
    )

    @field_validator("selected_output_requirements")
    @classmethod
    def _selected_output_requirements_are_canonical(
        cls,
        value: tuple[TerminalSelectedOutputRequirement, ...],
    ) -> tuple[TerminalSelectedOutputRequirement, ...]:
        canonical_lookup = _selected_output_requirements_by_terminal_result(value)
        records = {item.terminal_result: item for item in value}
        return tuple(records[terminal_result] for terminal_result in canonical_lookup)

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Any | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Validate raw request JSON before Pydantic can collapse key pairs."""
        _validate_harness_request_raw_json(json_data)
        return super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )

    # ------------------------------------------------------------------
    # Cross-field validators
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _check_run_id_consistency(self) -> HarnessExecutionRequest:
        """HarnessExecutionRequest.run_id must match RunDirRef.run_id."""
        if self.run_id != self.run_directory.run_id:
            raise ValueError(
                f"HarnessExecutionRequest.run_id ({self.run_id!r}) must match "
                f"RunDirRef.run_id ({self.run_directory.run_id!r})"
            )
        return self

    @model_validator(mode="after")
    def _check_sha256_digest(self) -> HarnessExecutionRequest:
        """When algorithm is sha256, digest must be exactly 64 lowercase hex chars."""
        h = self.compiled_harness.expected_hash
        if h.algorithm == "sha256":
            if not re.fullmatch(r"[0-9a-f]{64}", h.digest):
                raise ValueError(
                    f"CompiledHarnessHash digest must be exactly 64 lowercase hex "
                    f"characters when algorithm is 'sha256', got {h.digest!r}"
                )
        return self

    @model_validator(mode="after")
    def _check_duplicate_grant_capabilities(self) -> HarnessExecutionRequest:
        """Reject duplicate capability identifiers in the grants tuple."""
        seen: set[str] = set()
        for grant in self.capability_envelope.grants:
            if grant.capability_id in seen:
                raise ValueError(
                    f"Duplicate capability_id {grant.capability_id!r} in CapabilityEnvelope"
                )
            seen.add(grant.capability_id)
        return self

    @model_validator(mode="after")
    def _check_duplicate_artifact_ids(self) -> HarnessExecutionRequest:
        """Reject duplicate artifact_id values in input_artifacts."""
        seen: set[str] = set()
        for artifact in self.input_artifacts:
            if artifact.artifact_id in seen:
                raise ValueError(
                    f"Duplicate artifact_id {artifact.artifact_id!r} in input_artifacts"
                )
            seen.add(artifact.artifact_id)
        return self

    @model_validator(mode="after")
    def _check_duplicate_secret_ids(self) -> HarnessExecutionRequest:
        """Reject duplicate secret_id values in secret_refs."""
        seen: set[str] = set()
        for secret in self.secret_refs:
            if secret.secret_id in seen:
                raise ValueError(
                    f"Duplicate secret_id {secret.secret_id!r} in secret_refs"
                )
            seen.add(secret.secret_id)
        return self

    @model_validator(mode="after")
    def _check_duplicate_secret_env_vars(self) -> HarnessExecutionRequest:
        """Reject duplicate env_var values in secret_refs."""
        seen: set[str] = set()
        for secret in self.secret_refs:
            if secret.env_var in seen:
                raise ValueError(f"Duplicate env_var {secret.env_var!r} in secret_refs")
            seen.add(secret.env_var)
        return self


# ---------------------------------------------------------------------------
# Model, tool, and bridge-owned request/response models
# ---------------------------------------------------------------------------


class ModelCapabilityRequirements(BaseModel):
    """Exact model capabilities required by the 02C-02D Forge bridge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_calls: Literal[True] = True
    parallel_tool_calls: Literal[False] = False
    structured_output: Literal[False] = False
    reasoning_controls: Literal[False] = False
    usage_reporting: Literal[False] = False
    system_messages: Literal[True] = True
    tool_result_messages: Literal[True] = True


class SamplingRequest(BaseModel):
    """Canonical owned sampling controls for model calls."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = None
    stop: tuple[str, ...] | None = None
    reasoning_mode: str | None = None
    reasoning_effort: str | None = None

    @field_validator("stop")
    @classmethod
    def _stop_values_nonblank(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        for item in value:
            _nonblank(item, "stop")
        return value

    @field_validator("reasoning_mode", "reasoning_effort")
    @classmethod
    def _optional_strings_nonblank(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _nonblank(value, info.field_name)


class SanitizedMetadata(BaseModel):
    """Bounded metadata that is safe to persist across the public boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    values: Dict[str, SanitizedMetadataValue] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def _values_bounded(
        cls, value: dict[str, SanitizedMetadataValue]
    ) -> dict[str, SanitizedMetadataValue]:
        if len(value) > _SANITIZED_METADATA_MAX_ITEMS:
            raise ValueError("sanitized metadata contains too many items")
        total_bytes = 0
        for key, item in value.items():
            _nonblank(key, "sanitized metadata key")
            if len(key) > _SANITIZED_METADATA_KEY_MAX_LENGTH:
                raise ValueError("sanitized metadata key is too long")
            if isinstance(item, str):
                if len(item) > _SANITIZED_METADATA_STRING_MAX_LENGTH:
                    raise ValueError("sanitized metadata string value is too long")
                total_bytes += len(item.encode("utf-8"))
            else:
                total_bytes += len(str(item).encode("utf-8"))
        if total_bytes > _SANITIZED_METADATA_BYTES_MAX_LENGTH:
            raise ValueError("sanitized metadata payload is too large")
        return value


class RedactionPolicy(BaseModel):
    """Single bounded redaction policy for public summaries and diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_depth: int = Field(
        default=_REDACTION_DEFAULT_DEPTH, ge=1, le=_REDACTION_MAX_DEPTH
    )
    max_collection_items: int = Field(
        default=_REDACTION_DEFAULT_COLLECTION_ITEMS,
        ge=1,
        le=_REDACTION_MAX_COLLECTION_ITEMS,
    )
    max_string_length: int = Field(
        default=_REDACTION_DEFAULT_STRING_LENGTH,
        ge=1,
        le=_REDACTION_MAX_STRING_LENGTH,
    )
    max_total_bytes: int = Field(
        default=_REDACTION_DEFAULT_TOTAL_BYTES,
        ge=1,
        le=_REDACTION_MAX_TOTAL_BYTES,
    )
    replacement: str = "**redacted**"
    sensitive_field_markers: Tuple[str, ...] = (
        "authorization",
        "api-key",
        "apikey",
        "token",
        "secret",
        "password",
        "credential",
        "cookie",
        "set-cookie",
    )

    @field_validator("replacement")
    @classmethod
    def _replacement_nonblank(cls, value: str) -> str:
        return _nonblank(value, "replacement")

    @field_validator("sensitive_field_markers")
    @classmethod
    def _markers_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _nonblank(item, "sensitive marker").lower() for item in value
        )
        _unique(normalized, "sensitive marker")
        return normalized


class _RedactionBudget:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0

    def text(self, value: str) -> str:
        remaining = self._limit - self._used
        if remaining <= 0:
            return "[truncated]"
        encoded = value.encode("utf-8")
        if len(encoded) <= remaining:
            self._used += len(encoded)
            return value
        truncated = encoded[:remaining].decode("utf-8", errors="ignore")
        self._used = self._limit
        return f"{truncated}[truncated]"


def _redact_url(value: str, policy: RedactionPolicy) -> str:
    try:
        split = urlsplit(value)
    except ValueError:
        return _redact_secret_patterns(value, policy)
    host = split.hostname or ""
    if split.port is not None:
        host = f"{host}:{split.port}"
    query_parts = parse_qsl(split.query, keep_blank_values=True)
    query = "&".join(
        f"{key}={policy.replacement}"
        if _is_sensitive_field_name(key, policy)
        else f"{key}={value}"
        for key, value in query_parts
    )
    return urlunsplit(
        (
            split.scheme,
            host,
            split.path,
            query,
            policy.replacement if split.fragment else "",
        )
    )


def _redact_secret_patterns(text: str, policy: RedactionPolicy) -> str:
    text = _SECRET_PATTERNS[0].sub(
        lambda match: (
            match.group(0).split("=", 1)[0] + "=" + policy.replacement
            if match.group(1) != policy.replacement
            else match.group(0)
        ),
        text,
    )
    text = _SECRET_PATTERNS[1].sub(
        lambda match: (
            match.group(0)
            if match.group(2) == policy.replacement
            else f"{match.group(1)}{policy.replacement}"
        ),
        text,
    )
    for pattern in _SECRET_PATTERNS[2:]:
        text = pattern.sub(lambda match: f"{match.group(1)}{policy.replacement}", text)
    return text


def redact_diagnostic_text(
    value: str,
    *,
    policy: RedactionPolicy | None = None,
    secret_values: tuple[str, ...] = (),
) -> str:
    """Apply the shared deterministic redaction policy to text."""
    active_policy = policy or RedactionPolicy()
    text = value
    for secret in secret_values:
        if secret:
            text = text.replace(secret, active_policy.replacement)
    text = _URL_PATTERN.sub(
        lambda match: _redact_url(match.group(0), active_policy), text
    )
    text = _redact_secret_patterns(text, active_policy)
    if len(text) > active_policy.max_string_length:
        text = f"{text[: active_policy.max_string_length]}[truncated]"
    return text


def redact_diagnostic_value(
    value: object,
    *,
    policy: RedactionPolicy | None = None,
    secret_values: tuple[str, ...] = (),
) -> JsonValue:
    """Return a bounded JSON-safe diagnostic value without arbitrary repr calls."""
    active_policy = policy or RedactionPolicy()
    budget = _RedactionBudget(active_policy.max_total_bytes)
    return _redact_value(
        value,
        policy=active_policy,
        secret_values=secret_values,
        budget=budget,
        depth=0,
        seen=set(),
        sensitive_key=False,
    )


def redact_diagnostic_mapping(
    values: Mapping[str, object],
    *,
    policy: RedactionPolicy | None = None,
    secret_values: tuple[str, ...] = (),
) -> dict[str, JsonValue]:
    """Redact a diagnostic mapping using one bounded recursive policy."""
    redacted = redact_diagnostic_value(
        values,
        policy=policy,
        secret_values=secret_values,
    )
    return redacted if isinstance(redacted, dict) else {}


def _safe_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int | float | bool) or value is None:
        return str(value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, BaseException):
        parts = [
            _safe_text(arg)
            for arg in value.args
            if isinstance(arg, str | int | float | bool) or arg is None
        ]
        detail = ": " + " ".join(parts) if parts else ""
        return f"{type(value).__name__}{detail}"
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


def _redact_key(key: object, policy: RedactionPolicy, budget: _RedactionBudget) -> str:
    text = redact_diagnostic_text(_safe_text(key), policy=policy)
    return budget.text(text[: policy.max_string_length])


def _redact_value(
    value: object,
    *,
    policy: RedactionPolicy,
    secret_values: tuple[str, ...],
    budget: _RedactionBudget,
    depth: int,
    seen: set[int],
    sensitive_key: bool,
) -> JsonValue:
    if sensitive_key:
        return budget.text(policy.replacement)
    if depth >= policy.max_depth:
        return budget.text("[max_depth]")
    if isinstance(value, str):
        return budget.text(
            redact_diagnostic_text(
                value,
                policy=policy,
                secret_values=secret_values,
            )
        )
    if isinstance(value, int | float | bool) or value is None:
        return value
    if isinstance(value, Path | BaseException):
        return budget.text(
            redact_diagnostic_text(
                _safe_text(value),
                policy=policy,
                secret_values=secret_values,
            )
        )
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return budget.text("[cycle]")
        seen.add(identity)
        result: dict[str, JsonValue] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= policy.max_collection_items:
                result["[truncated]"] = budget.text("[truncated]")
                break
            clean_key = _redact_key(key, policy, budget)
            lowered = clean_key.lower()
            child_sensitive = _is_sensitive_field_name(lowered, policy)
            result[clean_key] = _redact_value(
                item,
                policy=policy,
                secret_values=secret_values,
                budget=budget,
                depth=depth + 1,
                seen=seen,
                sensitive_key=child_sensitive,
            )
        seen.remove(identity)
        return result
    if isinstance(value, tuple | list | set | frozenset):
        identity = id(value)
        if identity in seen:
            return budget.text("[cycle]")
        seen.add(identity)
        sequence_result: list[JsonValue] = [
            _redact_value(
                item,
                policy=policy,
                secret_values=secret_values,
                budget=budget,
                depth=depth + 1,
                seen=seen,
                sensitive_key=False,
            )
            for index, item in enumerate(value)
            if index < policy.max_collection_items
        ]
        if len(value) > policy.max_collection_items:
            sequence_result.append(budget.text("[truncated]"))
        seen.remove(identity)
        return sequence_result
    return budget.text(
        redact_diagnostic_text(
            _safe_text(value),
            policy=policy,
            secret_values=secret_values,
        )
    )


class ParsedToolArguments(BaseModel):
    """Parsed JSON object arguments for a model-requested tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["parsed"] = "parsed"
    value: JsonObject = Field(default_factory=dict)

    @property
    def values(self) -> JsonObject:
        """Compatibility accessor; ``value`` is the serialized contract field."""
        return self.value


class InvalidToolArguments(BaseModel):
    """A malformed tool-argument payload for the public model bridge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["invalid"] = "invalid"
    raw: JsonValue
    error_code: str

    @field_validator("error_code")
    @classmethod
    def _error_code_nonblank(cls, value: str) -> str:
        return _nonblank(value, "error_code")


ToolArguments = Annotated[
    ParsedToolArguments | InvalidToolArguments, Field(discriminator="kind")
]


class ModelToolCall(BaseModel):
    """Owned typed representation of an assistant-requested tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    name: str
    arguments: ToolArguments

    @field_validator("arguments", mode="before")
    @classmethod
    def _coerce_arguments(cls, value: Any) -> Any:
        if isinstance(value, dict) and not (
            value.get("kind") in {"parsed", "invalid"} or set(value) == {"value"}
        ):
            return ParsedToolArguments(value=value)
        return value

    @property
    def id(self) -> str:
        """Compatibility accessor; ``call_id`` is the serialized contract field."""
        return self.call_id

    @field_validator("call_id", "name")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


class SystemMessage(BaseModel):
    """System instructions sent through the model bridge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system"] = "system"
    content: str

    @property
    def kind(self) -> str:
        """Compatibility accessor; ``role`` is the serialized discriminator."""
        return self.role

    @field_validator("content")
    @classmethod
    def _content_nonblank(cls, value: str) -> str:
        return _nonblank(value, "content")


class UserMessage(BaseModel):
    """User message sent through the model bridge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user"] = "user"
    content: str

    @property
    def kind(self) -> str:
        """Compatibility accessor; ``role`` is the serialized discriminator."""
        return self.role

    @field_validator("content")
    @classmethod
    def _content_nonblank(cls, value: str) -> str:
        return _nonblank(value, "content")


class AssistantMessage(BaseModel):
    """Assistant response message containing text and/or tool calls."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: Tuple[ModelToolCall, ...] = Field(default_factory=tuple)
    reasoning_content: str | None = Field(default=None, repr=False)

    @property
    def kind(self) -> str:
        """Compatibility accessor; ``role`` is the serialized discriminator."""
        return self.role

    @field_validator("content", "reasoning_content")
    @classmethod
    def _content_nonblank(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _nonblank(value, info.field_name)

    @model_serializer(mode="wrap")
    def _omit_absent_reasoning_content(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        if self.reasoning_content is None:
            payload.pop("reasoning_content", None)
        return payload

    @model_validator(mode="after")
    def _assistant_has_content_or_tools(self) -> AssistantMessage:
        _unique(tuple(call.call_id for call in self.tool_calls), "assistant call_id")
        if self.content is None and not self.tool_calls:
            raise ValueError("assistant message requires content or tool_calls")
        return self


class ToolResultMessage(BaseModel):
    """Model-visible result for a prior assistant tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["tool"] = "tool"
    tool_call_id: str
    tool_name: str
    content: str

    @property
    def kind(self) -> str:
        """Compatibility accessor; ``role`` is the serialized discriminator."""
        return "tool_result"

    @field_validator("tool_call_id", "tool_name", "content")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


ModelMessage = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]


class ModelToolDefinition(BaseModel):
    """Owned model-visible tool definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    input_schema: JsonObject

    @field_validator("name", "description")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


class ModelCompletionRequest(BaseModel):
    """Immutable validated model inference request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    run_id: str
    model_profile_id: str
    messages: Tuple[ModelMessage, ...] = Field(description="Typed chat messages")
    tools: Tuple[ModelToolDefinition, ...] = Field(
        default_factory=tuple, description="Available tool definitions"
    )
    required_capabilities: ModelCapabilityRequirements = Field(
        default_factory=ModelCapabilityRequirements
    )
    sampling_overrides: SamplingRequest = Field(default_factory=SamplingRequest)
    maximum_output_tokens_override: int | None = Field(default=None, gt=0)
    request_options: JsonObject = Field(default_factory=dict)
    deadline: Deadline
    cancellation: CancellationRef
    secret_refs: Tuple[SecretRef, ...] = Field(default_factory=tuple)

    @property
    def model(self) -> str:
        """Compatibility accessor; ``model_profile_id`` is the contract field."""
        return self.model_profile_id

    @field_validator("request_id", "run_id", "model_profile_id")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)

    @model_validator(mode="after")
    def _request_invariants(self) -> ModelCompletionRequest:
        protected_options = {
            "model",
            "messages",
            "tools",
            "stream",
            "endpoint",
            "authentication",
            "timeout",
            "headers",
            "host",
            "content_type",
            "user_agent",
            "max_tokens",
            "maximum_output_tokens",
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "stop",
        }
        for option_name in self.request_options:
            if option_name in protected_options:
                raise ValueError(f"request option {option_name!r} is protected")
        _unique(tuple(tool.name for tool in self.tools), "tool name")
        _unique(tuple(secret.secret_id for secret in self.secret_refs), "secret_id")
        pending_tool_calls: dict[str, str] = {}
        answered_tool_call_ids: set[str] = set()
        for message in self.messages:
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls:
                    if (
                        call.call_id in pending_tool_calls
                        or call.call_id in answered_tool_call_ids
                    ):
                        raise ValueError("assistant tool-call IDs must be unique")
                    pending_tool_calls[call.call_id] = call.name
            elif isinstance(message, ToolResultMessage):
                expected_tool_name = pending_tool_calls.get(message.tool_call_id)
                if expected_tool_name is None:
                    raise ValueError("tool-result message has no matching tool call")
                if message.tool_name != expected_tool_name:
                    raise ValueError("tool-result message tool_name does not match")
                if message.tool_call_id in answered_tool_call_ids:
                    raise ValueError("tool-result message duplicates a tool call")
                answered_tool_call_ids.add(message.tool_call_id)
        return self


class UsageMetadata(BaseModel):
    """Token usage metadata for a model request/response pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_calls: int = Field(ge=0, description="Number of model calls made")
    tool_calls: int = Field(ge=0, description="Number of tool calls made")
    token_usage: TokenUsage | None = Field(
        default=None, description="Detailed token usage breakdown"
    )


class ModelCompletionResponse(BaseModel):
    """Immutable validated model inference response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_request_id: str | None = None
    model_id: str
    message: AssistantMessage
    finish_reason: Literal[
        "stop",
        "tool_calls",
        "length",
        "content_filter",
        "cancelled",
        "unknown",
    ]
    usage: TokenUsage | None = Field(default=None, description="Token usage metadata")
    provider_metadata: SanitizedMetadata | None = None

    @property
    def model(self) -> str:
        """Compatibility accessor; ``model_id`` is the contract field."""
        return self.model_id

    @property
    def content(self) -> str | None:
        """Compatibility accessor for the assistant message content."""
        return self.message.content

    @property
    def tool_calls(self) -> tuple[ModelToolCall, ...]:
        """Compatibility accessor for the assistant message tool calls."""
        return self.message.tool_calls

    @field_validator("provider_request_id", "model_id")
    @classmethod
    def _strings_nonblank(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _nonblank(value, info.field_name)

    @model_validator(mode="after")
    def _finish_reason_matches_message(self) -> ModelCompletionResponse:
        if self.message.tool_calls and self.finish_reason != "tool_calls":
            raise ValueError("tool call responses require finish_reason='tool_calls'")
        return self


class ValidatedToolCall(BaseModel):
    """Immutable validated tool call from a model response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(description="Unique tool call identifier")
    node_id: str = Field(description="Compiled node identifier")
    binding: ToolBindingRef = Field(description="Resolved tool binding")
    arguments: JsonObject = Field(description="Canonical JSON object arguments")

    @property
    def id(self) -> str:
        """Compatibility accessor; ``call_id`` is the serialized contract field."""
        return self.call_id

    @property
    def name(self) -> str:
        """Compatibility accessor for legacy fakes; not a serialized field."""
        return self.binding.tool_id

    @field_validator("call_id", "node_id")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


class ToolExecutionResult(BaseModel):
    """Immutable validated tool execution result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(description="Identifier of the originating tool call")
    status: ToolExecutionStatus = Field(description="Closed tool execution status")
    summary: str = Field(description="Bounded model-visible summary")
    structured_data: JsonValue = None
    artifact_refs: Tuple[ArtifactRef, ...] = Field(default_factory=tuple)
    error_code: str | None = Field(
        default=None, description="Stable error code when execution failed"
    )
    retryable: bool = Field(
        default=False, description="Whether retrying this tool call is safe"
    )
    side_effect_class: SideEffectClass
    idempotency: IdempotencyClass
    side_effect_certainty: SideEffectCertainty
    side_effect_record: SideEffectRecord | None = Field(
        default=None,
        description="Typed side-effect detail when certainty needs explanation",
    )
    input_sha256: str
    output_sha256: str | None = Field(
        default=None, description="SHA-256 hash of the serialized safe output"
    )
    timing: TimingMetadata = Field(description="Canonical timing metadata")

    @property
    def duration_ms(self) -> float:
        """Compatibility accessor; ``timing`` is the serialized contract field."""
        return self.timing.duration_ms

    @field_validator("call_id", "summary")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)

    @field_validator("error_code")
    @classmethod
    def _error_code_nonblank(cls, value: str | None) -> str | None:
        return None if value is None else _nonblank(value, "error_code")

    @field_validator("input_sha256")
    @classmethod
    def _input_sha256_valid(cls, value: str) -> str:
        return _validate_sha256(value, "input_sha256")

    @field_validator("output_sha256")
    @classmethod
    def _output_sha256_valid(cls, value: str | None) -> str | None:
        return None if value is None else _validate_sha256(value, "output_sha256")

    @model_validator(mode="after")
    def _result_consistency(self) -> ToolExecutionResult:
        if self.status == ToolExecutionStatus.SUCCESS:
            if self.error_code is not None:
                raise ValueError("successful tool results must not include error_code")
            if self.retryable:
                raise ValueError("successful tool results must not be retryable")
        else:
            if self.error_code is None:
                raise ValueError("failed tool results require error_code")
        if (
            self.side_effect_certainty == SideEffectCertainty.COMPLETION_UNKNOWN
            and self.idempotency
            in {IdempotencyClass.NON_IDEMPOTENT, IdempotencyClass.UNKNOWN}
            and self.retryable
        ):
            raise ValueError(
                "completion_unknown side effects are not retryable for "
                "non-idempotent or unknown-idempotency tool work"
            )
        if self.side_effect_record is not None:
            if self.side_effect_record.certainty != self.side_effect_certainty:
                raise ValueError(
                    "side_effect_record certainty must match side_effect_certainty"
                )
            if self.side_effect_record.retry_allowed != self.retryable:
                raise ValueError(
                    "side_effect_record retry_allowed must match retryable"
                )
        return self


class SideEffectRecord(BaseModel):
    """Typed side-effect detail for uncertain, rolled back, or absent tool effects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    certainty: SideEffectCertainty
    detail_code: str
    summary: str
    retry_allowed: bool

    @field_validator("detail_code", "summary")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


# ---------------------------------------------------------------------------
# Session models (mutable working models)
# ---------------------------------------------------------------------------


class GuardedSessionRequest(BaseModel):
    """Request wrapped in a guarded session.

    **Immutable** — once constructed, the session request is not modified.
    Includes a deadline for session-level time bounds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(description="Unique session identifier")
    execution_request: HarnessExecutionRequest = Field(
        description="The harness execution request"
    )
    deadline: Deadline = Field(description="Deadline for the guarded session")
    tool_execution_context: ToolExecutionContext | None = Field(
        default=None,
        description="Runtime-owned context passed through to tool execution",
    )


class GuardedSessionResult(BaseModel):
    """Result from a guarded session.

    **Immutable** — once produced, the result is not modified.
    Includes structured event and tool trace records alongside
    the existing terminal intent, usage, timing, and diagnostic fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(description="Unique session identifier")
    status: GuardedSessionStatus = Field(description="Session status")
    terminal_intent: TerminalIntent | None = Field(
        default=None, description="Terminal intent if session completed"
    )
    artifact_refs: Tuple[ArtifactRef, ...] = Field(
        default_factory=tuple, description="Artifact references produced"
    )
    usage: UsageMetadata | None = Field(
        default=None, description="Token usage metadata"
    )
    timing: TimingMetadata | None = Field(default=None, description="Timing metadata")
    diagnostic: DiagnosticMetadata | None = Field(
        default=None, description="Diagnostic metadata"
    )
    events: Tuple[SessionEvent, ...] = Field(
        default_factory=tuple, description="Session events recorded"
    )
    tool_trace: Tuple[ToolTraceRecord, ...] = Field(
        default_factory=tuple, description="Tool trace records"
    )


# ---------------------------------------------------------------------------
# Intent and result models (immutable snapshots)
# ---------------------------------------------------------------------------


class TerminalIntent(BaseModel):
    """Terminal intent expressing a provider-local stage disposition.

    Extended for 02B shape — includes request identity, stage
    identity, terminal node, closed disposition, summary, and
    artifact references.  Immutable snapshot — once emitted, the
    intent is not modified.  Correlation values are echoed from the execution
    request and do not grant caller workflow or terminal authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(description="Unique request identifier")
    run_id: str = Field(description="Run this request belongs to")
    stage: StageIdentity = Field(description="Stage identity")
    terminal_node_id: str = Field(description="Terminal node identifier")
    terminal_result: str = Field(description="Terminal result string")
    disposition: Literal["success", "blocked", "rejected", "escalated"] = Field(
        description="Terminal disposition (closed)"
    )
    summary: str = Field(description="Human-readable summary")
    artifact_refs: Tuple[ArtifactRef, ...] = Field(
        default_factory=tuple, description="Artifact references"
    )
    selected_output: SelectedOutput | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Admitted selected JSON output, explicitly present or absent",
    )
    selected_output_schema_sha256: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Digest of the invocation-local selected schema authority",
    )

    @field_validator("request_id")
    @classmethod
    def _request_id_must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("request_id must be a non-empty string")
        return v

    @field_validator("terminal_node_id")
    @classmethod
    def _terminal_node_id_must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("terminal_node_id must be a non-empty string")
        return v

    @field_validator("terminal_result")
    @classmethod
    def _terminal_result_must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("terminal_result must be a non-empty string")
        return v

    @field_validator("summary")
    @classmethod
    def _summary_must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("summary must be a non-empty string")
        return v

    @field_validator("selected_output_schema_sha256")
    @classmethod
    def _selected_schema_digest_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_sha256(value, "selected_output_schema_sha256")

    @model_validator(mode="after")
    def _selected_output_authority_is_paired(self) -> TerminalIntent:
        if (self.selected_output is None) != (
            self.selected_output_schema_sha256 is None
        ):
            raise ValueError(
                "selected_output and selected_output_schema_sha256 must be paired"
            )
        return self


class HarnessExecutionResult(BaseModel):
    """Result of a harness execution.

    Immutable snapshot with semantic result classification and
    structured metadata — 02B semantic shape replaces legacy
    process-shaped fields.  Its stage and terminal intent remain provider-local;
    request and run identifiers remain opaque caller correlation values.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ExecutionStatus = Field(description="Execution status")
    result_class: ExecutionResultClass = Field(description="Result classification")
    request_id: str = Field(description="Unique request identifier")
    run_id: str = Field(description="Run this request belongs to")
    stage: StageIdentity = Field(description="Stage identity")
    terminal_intent: TerminalIntent | None = Field(
        default=None, description="Terminal intent if session completed"
    )
    artifact_refs: Tuple[ArtifactRef, ...] = Field(
        default_factory=tuple, description="Artifact references produced"
    )
    compiled_harness: CompiledHarnessRef = Field(
        description="Reference to the compiled harness"
    )
    usage: UsageMetadata | None = Field(
        default=None, description="Token usage metadata"
    )
    timing: TimingMetadata = Field(description="Timing metadata")
    diagnostic: DiagnosticMetadata | None = Field(
        default=None, description="Diagnostic metadata"
    )
    terminal_certainty: TerminalCertainty = Field(
        default=TerminalCertainty.NOT_APPLICABLE,
        description="Certainty of terminal-result commit ordering",
    )
    selected_output: SelectedOutput | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Admitted selected JSON output, explicitly present or absent",
    )
    selected_output_schema_sha256: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Digest of the invocation-local selected schema authority",
    )

    @field_validator("selected_output_schema_sha256")
    @classmethod
    def _selected_schema_digest_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_sha256(value, "selected_output_schema_sha256")

    @model_validator(mode="after")
    def _check_result_class_invariants(self) -> HarnessExecutionResult:
        if (self.selected_output is None) != (
            self.selected_output_schema_sha256 is None
        ):
            raise ValueError(
                "selected_output and selected_output_schema_sha256 must be paired"
            )
        completed_classes = {
            ExecutionResultClass.DOMAIN_TERMINAL,
            ExecutionResultClass.DOMAIN_REJECTED,
        }
        if self.status == ExecutionStatus.COMPLETED:
            if self.result_class not in completed_classes:
                raise ValueError(
                    "status=completed is only valid for domain_terminal "
                    "or domain_rejected"
                )
        elif self.result_class in completed_classes:
            raise ValueError("domain result classes must use status=completed")

        if self.terminal_intent is not None:
            if (
                self.status != ExecutionStatus.COMPLETED
                or self.result_class not in completed_classes
            ):
                raise ValueError(
                    "terminal_intent is only valid for completed domain results"
                )
            if self.terminal_intent.request_id != self.request_id:
                raise ValueError("terminal_intent.request_id must match result")
            if self.terminal_intent.run_id != self.run_id:
                raise ValueError("terminal_intent.run_id must match result")
            if self.terminal_intent.stage != self.stage:
                raise ValueError("terminal_intent.stage must match result")
            if self.terminal_intent.selected_output != self.selected_output:
                raise ValueError("terminal_intent.selected_output must match result")
            if (
                self.terminal_intent.selected_output_schema_sha256
                != self.selected_output_schema_sha256
            ):
                raise ValueError(
                    "terminal_intent selected schema digest must match result"
                )
        elif (
            self.terminal_certainty == TerminalCertainty.COMMITTED
            and self.result_class
            not in {
                ExecutionResultClass.DOMAIN_TERMINAL,
                ExecutionResultClass.DOMAIN_REJECTED,
            }
        ):
            raise ValueError("committed terminal_certainty requires a domain result")
        if self.selected_output is not None and self.terminal_intent is None:
            raise ValueError(
                "selected_output authority requires a matching terminal_intent"
            )
        return self


# ---------------------------------------------------------------------------
# Timing and diagnostic models (immutable snapshots)
# ---------------------------------------------------------------------------


class TimingMetadata(BaseModel):
    """Timing and duration metadata.

    All fields are required — ``completed_at`` is now mandatory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    started_at: str = Field(description="ISO-8601 start timestamp")
    completed_at: str = Field(description="ISO-8601 completion timestamp")
    duration_ms: float = Field(ge=0, description="Duration in milliseconds")


class DiagnosticMetadata(BaseModel):
    """Immutable diagnostic metadata with structured field-level diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error_code: str = Field(description="Top-level error code identifier")
    category: Literal[
        "binding",
        "compiled_harness",
        "backend",
        "model",
        "tool",
        "budget",
        "timeout",
        "cancellation",
        "artifact",
        "internal",
    ] = Field(description="Closed diagnostic category")
    message: str = Field(description="Human-readable diagnostic message")
    retryable: bool = Field(description="Whether retrying may resolve this diagnostic")
    origin: str | TimeoutOrigin = Field(description="Failure origin or subsystem")
    fields: tuple[DiagnosticField, ...] = Field(
        default_factory=tuple,
        description="Tuple of bounded scalar diagnostic entries",
    )

    @model_validator(mode="after")
    def _field_keys_unique(self) -> DiagnosticMetadata:
        keys = [field.key for field in self.fields]
        if len(set(keys)) != len(keys):
            raise ValueError("Diagnostic field keys must be unique")
        return self


class TerminalResultArtifact(BaseModel):
    """Validated ``terminal_result.json`` artifact payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    request_id: str
    run_id: str
    stage: StageIdentity
    terminal_result: str
    result_class: Literal[
        ExecutionResultClass.DOMAIN_TERMINAL,
        ExecutionResultClass.DOMAIN_REJECTED,
    ]
    summary_artifact_paths: Tuple[str, ...] = Field(default_factory=tuple)
    compiled_harness_sha256: str
    terminal_certainty: TerminalCertainty = TerminalCertainty.COMMITTED

    @field_validator("request_id", "run_id", "terminal_result")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value

    @field_validator("summary_artifact_paths")
    @classmethod
    def _summary_paths_relative(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        seen: set[str] = set()
        for item in value:
            if not item.strip():
                raise ValueError("summary_artifact_paths values must be non-empty")
            path = Path(item)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    "summary_artifact_paths values must be safe relative paths"
                )
            if item in seen:
                raise ValueError("summary_artifact_paths values must be unique")
            seen.add(item)
        return value

    @field_validator("compiled_harness_sha256")
    @classmethod
    def _compiled_harness_sha256_valid(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(
                "compiled_harness_sha256 must be exactly 64 lowercase hex characters"
            )
        return value


class ExecutionSummaryArtifact(BaseModel):
    """Validated ``execution_summary.json`` artifact payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    request_id: str
    run_id: str
    stage: StageIdentity
    status: ExecutionStatus
    result_class: ExecutionResultClass
    diagnostic_error_code: str | None = None
    terminal_certainty: TerminalCertainty = TerminalCertainty.NOT_APPLICABLE

    @field_validator("request_id", "run_id", "diagnostic_error_code")
    @classmethod
    def _optional_strings_nonblank(cls, value: str | None, info: Any) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value


class MetricsArtifact(BaseModel):
    """Validated ``metrics.json`` artifact payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    request_id: str
    run_id: str
    session_id: str | None = None
    status: GuardedSessionStatus | ExecutionStatus
    usage: UsageMetadata | None = None

    @field_validator("request_id", "run_id", "session_id")
    @classmethod
    def _optional_strings_nonblank(cls, value: str | None, info: Any) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value


class DiagnosticArtifact(BaseModel):
    """Validated sanitized ``diagnostic.json`` artifact payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    diagnostic: DiagnosticMetadata


class ArtifactManifestEntry(BaseModel):
    """Single validated artifact entry in ``artifact_manifest.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    path: str
    media_type: str
    byte_size: int = Field(ge=0)
    sha256_hex: str
    complete: bool
    producer: str
    failure_code: str | None = Field(
        default=None,
        description="Stable failure code when this artifact is incomplete",
    )

    @field_validator("artifact_id", "path", "media_type", "producer")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value

    @field_validator("failure_code")
    @classmethod
    def _failure_code_nonblank(cls, value: str | None) -> str | None:
        return None if value is None else _nonblank(value, "failure_code")

    @field_validator("path")
    @classmethod
    def _path_relative_safe(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("manifest artifact paths must be safe relative paths")
        return value

    @field_validator("sha256_hex")
    @classmethod
    def _sha256_valid(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("sha256_hex must be exactly 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def _completion_failure_consistent(self) -> ArtifactManifestEntry:
        if self.complete and self.failure_code is not None:
            raise ValueError("complete artifacts must not include failure_code")
        if not self.complete and self.failure_code is None:
            raise ValueError("incomplete artifacts require failure_code")
        return self


class ArtifactManifestArtifact(BaseModel):
    """Validated ``artifact_manifest.json`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    request_id: str
    run_id: str
    artifacts: Tuple[ArtifactManifestEntry, ...]

    @field_validator("request_id", "run_id")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value

    @field_validator("artifacts")
    @classmethod
    def _artifact_ids_unique(
        cls, value: Tuple[ArtifactManifestEntry, ...]
    ) -> Tuple[ArtifactManifestEntry, ...]:
        artifact_ids = [entry.artifact_id for entry in value]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("manifest artifact_id values must be unique")
        if "artifact_manifest" in artifact_ids:
            raise ValueError("artifact_manifest must not reference itself")
        return value
