"""Contract models for the Millforge runtime.

All models are defined using Pydantic v2 APIs with ``extra="forbid"``
(closed-world) validation. Immutable models use ``frozen=True``;
mutable working models are explicitly noted in their docstrings.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, Literal, Optional, Tuple, TypeAlias
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


# ---------------------------------------------------------------------------
# Harness execution request (primary executable boundary)
# ---------------------------------------------------------------------------


class HarnessExecutionRequest(BaseModel):
    """Immutable executable boundary for harness execution.

    This is the primary input contract for ``HarnessRuntime.execute()``.
    All identifiers are validated for non-blank values, run IDs are
    checked for consistency, and collection-level duplicates are
    rejected at construction time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(description="Unique request identifier")
    run_id: str = Field(description="Run this request belongs to")
    work_item_id: str = Field(description="Active work item identifier")
    stage: StageIdentity = Field(description="Stage identity")
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

    @property
    def kind(self) -> str:
        """Compatibility accessor; ``role`` is the serialized discriminator."""
        return self.role

    @field_validator("content")
    @classmethod
    def _content_nonblank(cls, value: str | None) -> str | None:
        return None if value is None else _nonblank(value, "content")

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

    @model_validator(mode="after")
    def _unknown_mutation_is_not_retryable(self) -> SideEffectRecord:
        if (
            self.certainty == SideEffectCertainty.COMPLETION_UNKNOWN
            and self.retry_allowed
        ):
            raise ValueError("completion_unknown side effects must not be retryable")
        return self


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
    """Terminal intent expressing a desired stage disposition.

    Extended for 02B shape — includes request identity, stage
    identity, terminal node, closed disposition, summary, and
    artifact references.  Immutable snapshot — once emitted, the
    intent is not modified.
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


class HarnessExecutionResult(BaseModel):
    """Result of a harness execution.

    Immutable snapshot with semantic result classification and
    structured metadata — 02B semantic shape replaces legacy
    process-shaped fields.
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

    @model_validator(mode="after")
    def _check_result_class_invariants(self) -> HarnessExecutionResult:
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
        elif (
            self.terminal_certainty == TerminalCertainty.COMMITTED
            and self.result_class
            not in {
                ExecutionResultClass.DOMAIN_TERMINAL,
                ExecutionResultClass.DOMAIN_REJECTED,
            }
        ):
            raise ValueError("committed terminal_certainty requires a domain result")
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
