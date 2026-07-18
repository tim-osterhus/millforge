"""02B compiled harness plan and runtime trace contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
JsonObject = dict[str, Any]
ConnectorApprovalDecision = Literal[
    "approved",
    "forbidden",
    "pending",
    "missing",
    "wrong_stage",
    "wrong_run",
    "wrong_scope",
    "expired_or_stale",
]
ConnectorApprovalPolicyValue = Literal[
    "none",
    "millrace_explicit",
    "operator_out_of_band",
    "forbidden",
]
ConnectorDriftDecision = Literal["passed", "failed", "not_reached"]


class SideEffectClass(str, Enum):
    """Closed side-effect classification for compiled nodes."""

    READ_ONLY = "read_only"
    ARTIFACT_WRITE = "artifact_write"
    WORKSPACE_WRITE = "workspace_write"
    PROCESS_EXECUTION = "process_execution"
    NETWORK_READ = "network_read"
    NETWORK_WRITE = "network_write"
    TERMINAL = "terminal"


class IdempotencyClass(str, Enum):
    """Closed idempotency classification for compiled nodes."""

    IDEMPOTENT = "idempotent"
    IDEMPOTENT_WITH_KEY = "idempotent_with_key"
    NON_IDEMPOTENT = "non_idempotent"
    UNKNOWN = "unknown"


class ToolTraceDecision(str, Enum):
    """Closed prerequisite and capability decision values for tool traces."""

    ALLOWED = "allowed"
    DENIED = "denied"


class ToolExecutionStatus(str, Enum):
    """Closed tool execution status values used in tool traces."""

    NOT_EXECUTED = "not_executed"
    SUCCESS = "success"
    SOFT_FAILURE = "soft_failure"
    HARD_FAILURE = "hard_failure"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    AMBIGUOUS = "ambiguous"


class ToolTraceSideEffectClass(str, Enum):
    """Closed side-effect classification for persisted tool traces."""

    READ_ONLY = "read_only"
    ARTIFACT_WRITE = "artifact_write"
    WORKSPACE_WRITE = "workspace_write"
    PROCESS_EXECUTION = "process_execution"
    NETWORK_READ = "network_read"
    NETWORK_WRITE = "network_write"
    TERMINAL = "terminal"


class ToolTraceIdempotency(str, Enum):
    """Closed idempotency classification for persisted tool traces."""

    IDEMPOTENT = "idempotent"
    IDEMPOTENT_WITH_KEY = "idempotent_with_key"
    NON_IDEMPOTENT = "non_idempotent"
    UNKNOWN = "unknown"


class SideEffectCertainty(str, Enum):
    """Closed side-effect certainty values for persisted tool traces."""

    NOT_ATTEMPTED = "not_attempted"
    CONFIRMED_ABSENT = "confirmed_absent"
    CONFIRMED_COMPLETE = "confirmed_complete"
    ROLLED_BACK = "rolled_back"
    COMPLETION_UNKNOWN = "completion_unknown"


class SessionEventType(str, Enum):
    """Closed runtime session event values from the 02B contract."""

    RUNTIME_RECEIVED = "runtime_received"
    RUNTIME_VERIFIED = "runtime_verified"
    BACKEND_CONSTRUCTED = "backend_constructed"
    BINDING_REJECTED = "binding_rejected"
    COMPILED_HARNESS_INVALID = "compiled_harness_invalid"
    BACKEND_FAILED = "backend_failed"
    SESSION_STARTED = "session_started"
    WORKFLOW_CONSTRUCTED = "workflow_constructed"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_REQUEST_COMPLETED = "model_request_completed"
    MODEL_REQUEST_FAILED = "model_request_failed"
    CORRECTION_ISSUED = "correction_issued"
    PREMATURE_TERMINAL_REJECTED = "premature_terminal_rejected"
    PREREQUISITE_REJECTED = "prerequisite_rejected"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    CONTEXT_COMPACTED = "context_compacted"
    TERMINAL_INTENT_ACCEPTED = "terminal_intent_accepted"
    TERMINAL_INTENT_REJECTED = "terminal_intent_rejected"
    FINALIZATION_STARTED = "finalization_started"
    FINALIZATION_COMPLETED = "finalization_completed"
    FINALIZATION_FAILED = "finalization_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERNAL_FAILED = "internal_failed"


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


class DiagnosticField(BaseModel):
    """Immutable bounded scalar diagnostic field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(description="Diagnostic field key")
    value: str | int | float | bool | None = Field(
        description="Bounded JSON scalar diagnostic field value"
    )

    @field_validator("key")
    @classmethod
    def _key_nonblank(cls, value: str) -> str:
        return _nonblank(value, "key")


class StageIdentity(BaseModel):
    """Immutable provider-local identity of a compiled harness stage.

    On an execution request this identifies the stage that the selected
    Millforge harness admits.  It is not the caller's workflow plane, node,
    route, dispatch identity, or authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plane: Literal["execution", "planning", "learning"] = Field(
        description="Provider-local plane the compiled harness stage belongs to"
    )
    node_id: str = Field(description="Provider-local compiled stage node identifier")
    stage_kind_id: str = Field(
        description="Provider-local compiled stage kind identifier"
    )

    @field_validator("node_id", "stage_kind_id")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


class CompilerIdentity(BaseModel):
    """Identity of the compiler that produced the compiled plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    build_id: str

    @field_validator("name", "version", "build_id")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


class CompiledModelProfile(BaseModel):
    """Logical model profile reference only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str

    @field_validator("profile_id")
    @classmethod
    def _profile_id_nonblank(cls, value: str) -> str:
        return _nonblank(value, "profile_id")


class CompiledPromptPolicy(BaseModel):
    """Deterministic prompt policy included in the compiled plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str
    system_instructions: str
    include_request_context: bool

    @field_validator("policy_id", "system_instructions")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


class CompiledBudgetPolicy(BaseModel):
    """Closed correction and enforcement budget policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(gt=0)
    max_validation_retries: int = Field(gt=0)
    max_tool_errors: int = Field(gt=0)
    max_prerequisite_violations: int = Field(gt=0)
    max_premature_terminal_attempts: int = Field(gt=0)


class CompiledContextPolicy(BaseModel):
    """Context policy consumed by the deterministic runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: Literal["forge.tiered.v1"]
    budget_tokens: int = Field(gt=0)
    keep_recent_iterations: int = Field(ge=0)
    phase_thresholds: tuple[float, float, float]

    @field_validator("phase_thresholds")
    @classmethod
    def _thresholds_valid(
        cls, value: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("phase_thresholds must be finite")
        if any(item <= 0 or item > 1 for item in value):
            raise ValueError("phase_thresholds must be in (0, 1]")
        if tuple(sorted(value)) != value:
            raise ValueError("phase_thresholds must be non-decreasing")
        return value


class ToolBindingRef(BaseModel):
    """Resolved tool binding reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str
    tool_version: int = Field(gt=0)
    descriptor_sha256: str
    implementation_id: str

    @field_validator("tool_id", "implementation_id")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)

    @field_validator("descriptor_sha256")
    @classmethod
    def _descriptor_sha256_valid(cls, value: str) -> str:
        return _validate_sha256(value, "descriptor_sha256")


class ArgumentMatch(BaseModel):
    """Mapping from prerequisite output argument to current input argument."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prerequisite_argument: str
    current_argument: str

    @field_validator("prerequisite_argument", "current_argument")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


class CompiledPrerequisite(BaseModel):
    """Prerequisite node dependency for a compiled node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    argument_matches: tuple[ArgumentMatch, ...] = Field(default_factory=tuple)

    @field_validator("node_id")
    @classmethod
    def _node_id_nonblank(cls, value: str) -> str:
        return _nonblank(value, "node_id")


class CompiledHarnessNode(BaseModel):
    """A single node in a compiled harness plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    model_tool_name: str
    description: str
    input_schema: JsonObject
    binding: ToolBindingRef
    prerequisites: tuple[CompiledPrerequisite, ...] = Field(default_factory=tuple)
    required: bool
    terminal_result: str | None = None
    required_capabilities: tuple[str, ...] = Field(default_factory=tuple)
    produced_artifact_ids: tuple[str, ...] = Field(default_factory=tuple)
    side_effect_class: SideEffectClass
    idempotency: IdempotencyClass

    @field_validator("node_id", "model_tool_name", "description")
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)

    @field_validator("terminal_result")
    @classmethod
    def _terminal_result_nonblank(cls, value: str | None) -> str | None:
        return None if value is None else _nonblank(value, "terminal_result")

    @field_validator("required_capabilities", "produced_artifact_ids")
    @classmethod
    def _tuple_strings_nonblank(
        cls, value: tuple[str, ...], info: Any
    ) -> tuple[str, ...]:
        for item in value:
            _nonblank(item, info.field_name)
        _unique(value, info.field_name)
        return value


class TerminalArtifactRequirement(BaseModel):
    """Artifacts required for a specific terminal result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    terminal_result: str
    artifact_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("terminal_result")
    @classmethod
    def _terminal_result_nonblank(cls, value: str) -> str:
        return _nonblank(value, "terminal_result")

    @field_validator("artifact_ids")
    @classmethod
    def _artifact_ids_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _nonblank(item, "artifact_ids")
        _unique(value, "artifact_ids")
        return value


class CompiledArtifactPolicy(BaseModel):
    """Declared and terminal-required artifacts for a compiled plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    declared_artifact_ids: tuple[str, ...] = Field(default_factory=tuple)
    required_by_terminal: tuple[TerminalArtifactRequirement, ...] = Field(
        default_factory=tuple
    )

    @field_validator("declared_artifact_ids")
    @classmethod
    def _declared_artifact_ids_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _nonblank(item, "declared_artifact_ids")
        _unique(value, "declared_artifact_ids")
        return value


class CompiledHarnessPlan(BaseModel):
    """Immutable compiled harness plan consumed by the runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    kind: Literal["compiled_millforge_harness"]
    harness_id: str
    harness_version: int = Field(gt=0)
    source_sha256: str
    compiled_sha256: str
    stage_kind_ids: tuple[str, ...]
    model_profile: CompiledModelProfile
    prompt_policy: CompiledPromptPolicy
    budgets: CompiledBudgetPolicy
    context_policy: CompiledContextPolicy
    nodes: tuple[CompiledHarnessNode, ...] = Field(min_length=1)
    required_capabilities: tuple[str, ...] = Field(default_factory=tuple)
    terminal_result_map: dict[str, str]
    artifact_policy: CompiledArtifactPolicy
    compiler_identity: CompilerIdentity

    @field_validator("harness_id")
    @classmethod
    def _harness_id_nonblank(cls, value: str) -> str:
        return _nonblank(value, "harness_id")

    @field_validator("source_sha256", "compiled_sha256")
    @classmethod
    def _hashes_valid(cls, value: str, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @field_validator("stage_kind_ids", "required_capabilities")
    @classmethod
    def _tuple_strings_unique(
        cls, value: tuple[str, ...], info: Any
    ) -> tuple[str, ...]:
        for item in value:
            _nonblank(item, info.field_name)
        _unique(value, info.field_name)
        return value

    @field_validator("terminal_result_map")
    @classmethod
    def _terminal_result_map_nonblank(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            _nonblank(key, "terminal_result_map key")
            _nonblank(item, "terminal_result_map value")
        if len(set(value.values())) != len(value.values()):
            raise ValueError("terminal_result_map values must be unique")
        return value

    @model_validator(mode="after")
    def _check_plan_invariants(self) -> CompiledHarnessPlan:
        violations = self.validate_plan_invariants()
        if violations:
            raise ValueError("; ".join(violations))
        return self

    def validate_plan_invariants(self) -> list[str]:
        """Return cross-field invariant violations."""
        violations: list[str] = []
        node_ids = [node.node_id for node in self.nodes]
        model_tool_names = [node.model_tool_name for node in self.nodes]
        binding_pairs = [
            (node.binding.tool_id, node.binding.tool_version) for node in self.nodes
        ]
        terminal_nodes: dict[str, CompiledHarnessNode] = {
            node.node_id: node for node in self.nodes if node.terminal_result
        }
        terminal_results = {
            node.terminal_result
            for node in self.nodes
            if node.terminal_result is not None
        }
        declared_artifacts = set(self.artifact_policy.declared_artifact_ids)
        produced_artifacts = {
            artifact_id
            for node in self.nodes
            for artifact_id in node.produced_artifact_ids
        }
        required_capabilities = set(self.required_capabilities)

        if len(set(node_ids)) != len(node_ids):
            violations.append("node_id values must be unique")
        if len(set(model_tool_names)) != len(model_tool_names):
            violations.append("model_tool_name values must be unique")
        if len(set(binding_pairs)) != len(binding_pairs):
            violations.append("binding tool_id/tool_version pairs must be unique")
        if not terminal_nodes:
            violations.append("Plan must contain at least one terminal node")

        for node in self.nodes:
            if node.terminal_result is not None and node.required:
                violations.append(
                    f"Terminal node {node.node_id!r} must not be required"
                )
            for prereq in node.prerequisites:
                if prereq.node_id not in node_ids:
                    violations.append(
                        f"Node {node.node_id!r} prerequisite references unknown "
                        f"node_id {prereq.node_id!r}"
                    )
                if prereq.node_id == node.node_id:
                    violations.append(f"Node {node.node_id!r} cannot require itself")
            for capability in node.required_capabilities:
                if capability not in required_capabilities:
                    violations.append(
                        f"Node {node.node_id!r} requires undeclared capability "
                        f"{capability!r}"
                    )
            for artifact_id in node.produced_artifact_ids:
                if artifact_id not in declared_artifacts:
                    violations.append(
                        f"Node {node.node_id!r} produces undeclared artifact "
                        f"{artifact_id!r}"
                    )

        if set(self.terminal_result_map) != set(terminal_nodes):
            violations.append(
                "terminal_result_map keys must exactly name terminal nodes"
            )
        for node_id, terminal_result in self.terminal_result_map.items():
            terminal_node = terminal_nodes.get(node_id)
            if (
                terminal_node is not None
                and terminal_node.terminal_result != terminal_result
            ):
                violations.append(
                    f"terminal_result_map value for {node_id!r} must match node terminal_result"
                )

        for requirement in self.artifact_policy.required_by_terminal:
            if requirement.terminal_result not in terminal_results:
                violations.append(
                    f"Artifact requirement terminal_result "
                    f"{requirement.terminal_result!r} is not in terminal_result_map"
                )
            for artifact_id in requirement.artifact_ids:
                if artifact_id not in declared_artifacts:
                    violations.append(
                        f"Required artifact {artifact_id!r} is not declared"
                    )
                if artifact_id not in produced_artifacts:
                    violations.append(
                        f"Required artifact {artifact_id!r} has no producer"
                    )

        return violations


class SessionEvent(BaseModel):
    """Ordered runtime session event record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    sequence: int = Field(gt=0)
    occurred_at: str
    monotonic_offset_ms: float = Field(ge=0)
    event_type: SessionEventType
    request_id: str
    run_id: str
    session_id: str
    stage: StageIdentity
    node_id: str | None = None
    model_turn: int | None = Field(default=None, ge=0)
    tool_call_id: str | None = None
    code: str | None = None
    fields: tuple[DiagnosticField, ...] = Field(default_factory=tuple)

    @field_validator(
        "occurred_at",
        "request_id",
        "run_id",
        "session_id",
    )
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)

    @field_validator("node_id", "tool_call_id", "code")
    @classmethod
    def _optional_strings_nonblank(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _nonblank(value, info.field_name)

    @field_validator("fields")
    @classmethod
    def _field_keys_unique(
        cls, value: tuple[DiagnosticField, ...]
    ) -> tuple[DiagnosticField, ...]:
        keys = [field.key for field in value]
        if len(set(keys)) != len(keys):
            raise ValueError("diagnostic field keys must be unique")
        return value


class ToolTraceDecisionRecord(BaseModel):
    """Typed keyed support decision recorded with a tool trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    decision: ToolTraceDecision

    @field_validator("key")
    @classmethod
    def _key_nonblank(cls, value: str) -> str:
        return _nonblank(value, "key")


class ToolTraceRecord(BaseModel):
    """Ordered runtime trace record for a tool invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    sequence: int = Field(gt=0)
    occurred_at: str
    monotonic_offset_ms: float = Field(ge=0)
    request_id: str
    run_id: str
    session_id: str
    stage: StageIdentity
    node_id: str
    model_turn: int = Field(ge=0)
    tool_call_id: str
    model_tool_name: str
    binding: ToolBindingRef
    binding_resolution_status: Literal["resolved", "ambiguous", "uncompiled"] = (
        "resolved"
    )
    connector_id: str | None = None
    provider_tool_name: str | None = None
    connector_tool_id: str | None = None
    connector_tool_version: int | None = Field(default=None, ge=1)
    connector_descriptor_sha256: str | None = None
    connector_identity_sha256: str | None = None
    discovery_snapshot_sha256: str | None = None
    approval_policy: ConnectorApprovalPolicyValue | None = None
    approval_decision: ConnectorApprovalDecision | None = None
    approval_evidence: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )
    broker_attempted: bool | None = None
    request_sha256: str | None = None
    response_sha256: str | None = None
    retry_decision: Literal["retry_allowed", "retry_denied"] | None = None
    drift_decision: ConnectorDriftDecision | None = None
    redacted_evidence: dict[str, Any] = Field(default_factory=dict)
    input_sha256: str
    prerequisite_decisions: tuple[ToolTraceDecisionRecord, ...] = Field(
        default_factory=tuple
    )
    capability_decisions: tuple[ToolTraceDecisionRecord, ...] = Field(
        default_factory=tuple
    )
    execution_status: ToolExecutionStatus
    retryable: bool
    side_effect_class: ToolTraceSideEffectClass
    idempotency: ToolTraceIdempotency
    side_effect_certainty: SideEffectCertainty
    side_effect_detail_code: str | None = None
    side_effect_detail_summary: str | None = None
    side_effect_retry_allowed: bool | None = None
    output_sha256: str | None = None
    duration_ms: float = Field(ge=0)
    summary: str

    @field_validator(
        "occurred_at",
        "request_id",
        "run_id",
        "session_id",
        "node_id",
        "tool_call_id",
        "model_tool_name",
        "summary",
    )
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)

    @field_validator("input_sha256")
    @classmethod
    def _input_sha256_valid(cls, value: str) -> str:
        return _validate_sha256(value, "input_sha256")

    @field_validator(
        "connector_id",
        "provider_tool_name",
        "connector_tool_id",
        "approval_policy",
        "approval_decision",
    )
    @classmethod
    def _optional_connector_strings_nonblank(
        cls, value: str | None, info: Any
    ) -> str | None:
        return None if value is None else _nonblank(value, info.field_name)

    @field_validator(
        "connector_descriptor_sha256",
        "connector_identity_sha256",
        "discovery_snapshot_sha256",
    )
    @classmethod
    def _optional_connector_sha256_valid(
        cls, value: str | None, info: Any
    ) -> str | None:
        return None if value is None else _validate_sha256(value, info.field_name)

    @field_validator("request_sha256", "response_sha256", "output_sha256")
    @classmethod
    def _optional_sha256_valid(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _validate_sha256(value, info.field_name)

    @field_validator("side_effect_detail_code", "side_effect_detail_summary")
    @classmethod
    def _optional_detail_nonblank(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _nonblank(value, info.field_name)

    @field_validator("prerequisite_decisions", "capability_decisions")
    @classmethod
    def _decision_keys_unique(
        cls, value: tuple[ToolTraceDecisionRecord, ...], info: Any
    ) -> tuple[ToolTraceDecisionRecord, ...]:
        keys = [field.key for field in value]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{info.field_name} keys must be unique")
        return value

    @model_validator(mode="after")
    def _trace_consistency(self) -> ToolTraceRecord:
        connector_fields = (
            self.connector_id,
            self.provider_tool_name,
            self.connector_tool_id,
            self.connector_tool_version,
            self.connector_descriptor_sha256,
            self.connector_identity_sha256,
            self.discovery_snapshot_sha256,
            self.approval_policy,
            self.approval_decision,
            self.broker_attempted,
            self.request_sha256,
            self.response_sha256,
            self.retry_decision,
            self.drift_decision,
        )
        if any(item is not None for item in connector_fields):
            if not all(item is not None for item in connector_fields):
                raise ValueError(
                    "connector approval trace fields must be provided together"
                )
            if not self.approval_evidence:
                raise ValueError("connector approval trace requires approval_evidence")
            if not self.redacted_evidence:
                raise ValueError("connector trace requires redacted_evidence")
        if (
            self.binding_resolution_status != "resolved"
            and self.execution_status == ToolExecutionStatus.SUCCESS
        ):
            raise ValueError(
                "binding-resolution failures cannot be marked as successful"
            )
        denied = any(
            record.decision == ToolTraceDecision.DENIED
            for record in (*self.prerequisite_decisions, *self.capability_decisions)
        )
        if denied or self.execution_status == ToolExecutionStatus.NOT_EXECUTED:
            if self.side_effect_certainty != SideEffectCertainty.NOT_ATTEMPTED:
                raise ValueError(
                    "denied or not_executed calls must use "
                    "side_effect_certainty=not_attempted"
                )
            if self.output_sha256 is not None:
                raise ValueError(
                    "denied or not_executed calls must not have output_sha256"
                )
        detail_fields = (
            self.side_effect_detail_code,
            self.side_effect_detail_summary,
            self.side_effect_retry_allowed,
        )
        if any(item is not None for item in detail_fields) and not all(
            item is not None for item in detail_fields
        ):
            raise ValueError("side-effect detail fields must be provided together")
        if (
            self.side_effect_certainty == SideEffectCertainty.COMPLETION_UNKNOWN
            and self.idempotency
            in {ToolTraceIdempotency.NON_IDEMPOTENT, ToolTraceIdempotency.UNKNOWN}
            and self.retryable
        ):
            raise ValueError(
                "completion_unknown side effects are not retryable for "
                "non-idempotent or unknown-idempotency tool work"
            )
        if self.side_effect_retry_allowed is not None:
            if self.side_effect_retry_allowed != self.retryable:
                raise ValueError("side_effect_retry_allowed must match retryable")
        return self


def canonical_json_serialize(obj: Any, *, ensure_ascii: bool = True) -> str:
    """Serialize JSON-compatible data for deterministic SHA-256 hashing."""
    return (
        json.dumps(
            obj,
            sort_keys=True,
            ensure_ascii=ensure_ascii,
            allow_nan=False,
            separators=(",", ":"),
        ).replace("\r\n", "\n")
        + "\n"
    )


def canonical_compiled_plan_bytes(plan: CompiledHarnessPlan) -> bytes:
    """Serialize a finalized compiled plan as canonical UTF-8 JSON bytes."""
    verified, _computed, warnings, restored = verify_compiled_plan_sha256(
        canonical_json_serialize(plan.model_dump(mode="json")),
        expected_compiled_hash=plan.compiled_sha256,
        expected_harness_id=plan.harness_id,
        expected_harness_version=plan.harness_version,
    )
    if not verified or restored is None:
        joined = (
            "; ".join(warnings) if warnings else "compiled hash verification failed"
        )
        raise ValueError(joined)
    return canonical_json_serialize(restored.model_dump(mode="json")).encode("utf-8")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise json.JSONDecodeError(f"Duplicate key {key!r}", "", 0)
        seen.add(key)
        result[key] = value
    return result


def parse_and_strip_compiled_plan(
    raw: str,
) -> tuple[CompiledHarnessPlan, dict[str, Any]]:
    """Parse JSON with duplicate-key rejection and strip ``compiled_sha256``."""
    parsed: dict[str, Any] = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    plan = CompiledHarnessPlan.model_validate(parsed)
    stripped = dict(parsed)
    stripped.pop("compiled_sha256", None)
    return plan, stripped


def calculate_compiled_plan_sha256(plan_payload: dict[str, Any]) -> str:
    """Hash a complete JSON-mode compiled-plan payload without its digest field."""
    if "compiled_sha256" not in plan_payload:
        raise ValueError("compiled plan payload must include compiled_sha256")
    body = dict(plan_payload)
    body.pop("compiled_sha256")
    return hashlib.sha256(canonical_json_serialize(body).encode("utf-8")).hexdigest()


def finalize_compiled_plan_sha256(plan: CompiledHarnessPlan) -> CompiledHarnessPlan:
    """Return a fully validated plan with the canonical compiled SHA-256 set."""
    placeholder_payload = plan.model_dump(mode="json")
    digest = calculate_compiled_plan_sha256(placeholder_payload)
    finalized = CompiledHarnessPlan.model_validate(
        {**placeholder_payload, "compiled_sha256": digest}
    )
    verified, computed, warnings, _restored = verify_compiled_plan_sha256(
        canonical_json_serialize(finalized.model_dump(mode="json")),
        expected_compiled_hash=digest,
        expected_harness_id=finalized.harness_id,
        expected_harness_version=finalized.harness_version,
    )
    if not verified or computed != digest:
        joined = (
            "; ".join(warnings) if warnings else "compiled hash verification failed"
        )
        raise ValueError(joined)
    return finalized


def verify_compiled_plan_sha256(
    raw: str,
    *,
    expected_compiled_hash: str | None = None,
    expected_harness_id: str | None = None,
    expected_harness_version: int | None = None,
) -> tuple[bool, str, list[str], CompiledHarnessPlan | None]:
    """Verify a serialized 02B compiled plan canonical hash and identity."""
    warnings: list[str] = []

    try:
        parsed: dict[str, Any] = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        return False, "", [f"JSON parse error: {exc}"], None

    try:
        plan = CompiledHarnessPlan.model_validate(parsed)
    except Exception as exc:
        return False, "", [f"Plan validation error: {exc}"], None

    try:
        computed_hash = calculate_compiled_plan_sha256(parsed)
    except (TypeError, ValueError) as exc:
        return False, "", [f"Canonical serialisation error: {exc}"], None

    verified = True

    if computed_hash != plan.compiled_sha256:
        warnings.append(
            f"Computed hash {computed_hash} does not match "
            f"plan.compiled_sha256 {plan.compiled_sha256}"
        )
        verified = False

    if expected_compiled_hash is not None and computed_hash != expected_compiled_hash:
        warnings.append(
            f"Computed hash {computed_hash} does not match "
            f"expected_compiled_hash {expected_compiled_hash}"
        )
        verified = False

    if expected_harness_id is not None and plan.harness_id != expected_harness_id:
        warnings.append(
            f"Plan harness_id {plan.harness_id!r} does not match "
            f"expected_harness_id {expected_harness_id!r}"
        )
        verified = False

    if (
        expected_harness_version is not None
        and plan.harness_version != expected_harness_version
    ):
        warnings.append(
            f"Plan harness_version {plan.harness_version!r} does not match "
            f"expected_harness_version {expected_harness_version!r}"
        )
        verified = False

    return verified, computed_hash, warnings, plan


class FileCompiledHarnessLoader:
    """Filesystem loader for runtime compiled-plan references."""

    async def load(self, ref: Any) -> CompiledHarnessPlan:
        """Load and verify a compiled harness plan from ``ref.path``."""
        path = Path(ref.path)
        raw = path.read_text(encoding="utf-8")
        expected_hash = getattr(ref.expected_hash, "digest", None)
        identity = ref.identity
        verified, _computed, warnings, plan = verify_compiled_plan_sha256(
            raw,
            expected_compiled_hash=expected_hash,
            expected_harness_id=identity.harness_id,
            expected_harness_version=identity.harness_version,
        )
        if not verified or plan is None:
            joined = "; ".join(warnings) if warnings else "compiled plan invalid"
            raise ValueError(joined)
        return plan
