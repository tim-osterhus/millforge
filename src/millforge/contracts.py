"""Contract models for the Millforge runtime.

All models are defined using Pydantic v2 APIs with ``extra="forbid"``
(closed-world) validation. Immutable snapshot models use ``frozen=True``;
mutable working models are explicitly noted in their docstrings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Secret reference
# ---------------------------------------------------------------------------


class SecretRef(BaseModel):
    """Reference to a secret stored outside the contract boundary.

    This model stores an opaque handle or environment-variable name that
    resolves to a secret value. **The secret value itself must never appear
    in any contract field or serialization output.**
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    env_var: str = Field(description="Environment variable name holding the secret")
    description: Optional[str] = Field(
        default=None, description="Optional human-readable description"
    )

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
    version: str = Field(description="Harness version string")


class CompiledHarnessHash(BaseModel):
    """Immutable cryptographic hash of a compiled harness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: str = Field(description="Hash algorithm (e.g. sha256, blake2b)")
    digest: str = Field(description="Hex-encoded digest value")


class RunDirRef(BaseModel):
    """Immutable reference to a run directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(description="Unique run identifier")
    path: Path = Field(description="Absolute or relative path to the run directory")


class ArtifactRef(BaseModel):
    """Immutable reference to an artifact file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(description="Unique artifact identifier")
    path: Path = Field(description="Path to the artifact file")
    content_type: Optional[str] = Field(
        default=None, description="MIME type or content format"
    )


# ---------------------------------------------------------------------------
# Stage and execution models
# ---------------------------------------------------------------------------


class StageExecutionRequest(BaseModel):
    """Immutable request to execute a stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(description="Unique request identifier")
    run_id: str = Field(description="Run this request belongs to")
    stage: str = Field(description="Stage name to execute")
    task_id: str = Field(description="Task identifier")
    mode_id: str = Field(description="Mode identifier for model assignment")
    compiled_plan_id: str = Field(description="Compiled plan identifier")


class CapabilityEnvelope(BaseModel):
    """Immutable capability grant envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(description="Capability name")
    decision: str = Field(description="Grant decision (granted, denied, pending)")
    enforcement: str = Field(
        description=("Enforcement mode (runtime_enforced, advisory_only, not_enforced)")
    )


class ModelProfile(BaseModel):
    """Immutable model profile describing an LLM configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_name: str = Field(description="Model name identifier")
    provider: str = Field(description="Backend provider name")
    assigned_alias: str = Field(
        description="Assignment alias used in mode configuration"
    )
    source: str = Field(description="Assignment source (e.g. mode:stage:builder)")
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Model parameters (temperature, top_p, etc.)",
    )


class TimeoutRef(BaseModel):
    """Immutable timeout reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(description="Timeout duration in seconds")
    deadline: Optional[str] = Field(
        default=None, description="ISO-8601 deadline timestamp"
    )


class CancellationRef(BaseModel):
    """Immutable cancellation reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cancellation_token: str = Field(description="Cancellation token identifier")
    reason: Optional[str] = Field(
        default=None, description="Optional reason for cancellation"
    )


# ---------------------------------------------------------------------------
# Model request/response models
# ---------------------------------------------------------------------------


class ValidatedModelRequest(BaseModel):
    """Immutable validated model inference request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(description="Target model name")
    messages: List[Dict[str, Any]] = Field(
        description="Chat messages (role/content pairs)"
    )
    tools: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Available tool definitions"
    )
    temperature: Optional[float] = Field(
        default=None, ge=0, le=2, description="Sampling temperature"
    )
    max_tokens: Optional[int] = Field(
        default=None, ge=1, description="Maximum output tokens"
    )
    stream: bool = Field(default=False, description="Whether to stream the response")


class UsageMetadata(BaseModel):
    """Token usage metadata for a model request/response pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0, description="Number of input (prompt) tokens")
    output_tokens: int = Field(ge=0, description="Number of output (completion) tokens")
    total_tokens: int = Field(ge=0, description="Total tokens consumed")


class ValidatedModelResponse(BaseModel):
    """Immutable validated model inference response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(description="Model that produced the response")
    content: Optional[str] = Field(default=None, description="Response text content")
    tool_calls: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Tool calls requested by the model"
    )
    finish_reason: Optional[str] = Field(
        default=None, description="Reason the response finished"
    )
    usage: Optional[UsageMetadata] = Field(
        default=None, description="Token usage metadata"
    )


class ValidatedToolCall(BaseModel):
    """Immutable validated tool call from a model response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(description="Unique tool call identifier")
    name: str = Field(description="Tool name to invoke")
    arguments: Dict[str, Any] = Field(description="Tool arguments as a dictionary")


class ValidatedToolResult(BaseModel):
    """Immutable validated tool execution result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(description="Identifier of the originating tool call")
    output: Optional[str] = Field(default=None, description="Tool output text")
    error: Optional[str] = Field(
        default=None, description="Error message if the tool failed"
    )
    duration_ms: Optional[int] = Field(
        default=None, ge=0, description="Execution duration in milliseconds"
    )


# ---------------------------------------------------------------------------
# Session models (mutable working models)
# ---------------------------------------------------------------------------


class GuardedSessionRequest(BaseModel):
    """Request wrapped in a guarded session.

    **Mutable working model** — the session may accumulate guardrail
    annotations during processing. Does *not* use ``frozen=True``.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(description="Unique session identifier")
    request_type: str = Field(description="Type of the wrapped request")
    payload: Dict[str, Any] = Field(description="Request payload data")


class GuardedSessionResult(BaseModel):
    """Result from a guarded session.

    **Mutable working model** — the result may be updated as guardrail
    checks complete. Does *not* use ``frozen=True``.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(description="Unique session identifier")
    result_type: str = Field(description="Type of the wrapped result")
    payload: Dict[str, Any] = Field(description="Result payload data")
    blocked: bool = Field(
        default=False,
        description="Whether the session was blocked by guardrails",
    )
    reason: Optional[str] = Field(default=None, description="Block reason if blocked")


# ---------------------------------------------------------------------------
# Intent and result models (immutable snapshots)
# ---------------------------------------------------------------------------


class TerminalIntent(BaseModel):
    """Terminal intent expressing a desired stage disposition.

    Immutable snapshot — once emitted, the intent is not modified.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: str = Field(
        description="Terminal disposition (success, blocked, fail, etc.)"
    )
    message: str = Field(description="Human-readable explanation")
    run_id: str = Field(description="Run this intent belongs to")
    stage: str = Field(description="Stage that emitted the intent")


class HarnessExecutionResult(BaseModel):
    """Result of a harness execution.

    Immutable snapshot — once produced, the result is not modified.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    exit_code: int = Field(description="Process exit code")
    stdout: str = Field(default="", description="Standard output captured")
    stderr: str = Field(default="", description="Standard error captured")
    success: bool = Field(description="Whether execution completed successfully")
    metadata: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional execution metadata"
    )


# ---------------------------------------------------------------------------
# Timing and diagnostic models (immutable snapshots)
# ---------------------------------------------------------------------------


class TimingMetadata(BaseModel):
    """Timing and duration metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    started_at: str = Field(description="ISO-8601 start timestamp")
    completed_at: Optional[str] = Field(
        default=None, description="ISO-8601 completion timestamp"
    )
    duration_ms: float = Field(ge=0, description="Duration in milliseconds")


class DiagnosticMetadata(BaseModel):
    """Diagnostic error metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error_code: Optional[str] = Field(default=None, description="Error code identifier")
    error_message: Optional[str] = Field(
        default=None, description="Human-readable error message"
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None, description="Additional diagnostic context"
    )
