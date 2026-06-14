"""Immutable source-language contracts for Millforge harness files."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from millforge.compiler.validators import (
    validate_argument_name,
    validate_artifact_id,
    validate_harness_id,
    validate_harness_version,
    validate_policy_id,
    validate_profile_id,
    validate_stage_kind_id,
    validate_terminal_result,
    validate_threshold,
    validate_tool_reference,
    validate_unique,
    validate_utf8_size,
)

MAX_NODES = 512
MAX_PREREQUISITES_PER_NODE = 64
MAX_ARGUMENT_MATCHES_PER_PREREQUISITE = 32
MAX_TERMINAL_NODES = 64
MAX_DECLARED_ARTIFACTS = 512
MAX_TERMINAL_ARTIFACT_POLICIES = 64
MAX_ARTIFACTS_PER_TERMINAL = 512
MAX_STAGE_KIND_IDS = 64
MAX_SYSTEM_INSTRUCTIONS_UTF8 = 65_536


class ArgumentMatchSource(BaseModel):
    """Immutable prerequisite argument equality mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prior_argument: StrictStr
    current_argument: StrictStr

    @field_validator("prior_argument", "current_argument")
    @classmethod
    def _arguments_valid(cls, value: str) -> str:
        return validate_argument_name(value)


class PrerequisiteSource(BaseModel):
    """Immutable source prerequisite declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: StrictStr
    argument_matches: tuple[ArgumentMatchSource, ...] = Field(default_factory=tuple)

    @model_validator(mode="before")
    @classmethod
    def _convert_argument_mapping(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        raw_matches = values.get("argument_matches")
        if isinstance(raw_matches, Mapping):
            values["argument_matches"] = tuple(
                {"prior_argument": key, "current_argument": value}
                for key, value in raw_matches.items()
            )
        return values

    @field_validator("node_id")
    @classmethod
    def _node_id_valid(cls, value: str) -> str:
        from millforge.compiler.validators import validate_node_id

        return validate_node_id(value)

    @field_validator("argument_matches")
    @classmethod
    def _argument_matches_limited(
        cls, value: tuple[ArgumentMatchSource, ...]
    ) -> tuple[ArgumentMatchSource, ...]:
        if len(value) > MAX_ARGUMENT_MATCHES_PER_PREREQUISITE:
            raise ValueError("argument_matches may contain at most 32 entries")
        validate_unique(tuple(item.prior_argument for item in value), "prior_argument")
        validate_unique(
            tuple(item.current_argument for item in value), "current_argument"
        )
        return value


class HarnessNodeSource(BaseModel):
    """Immutable source graph node declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: StrictStr
    tool_ref: StrictStr
    required: StrictBool = False
    prerequisites: tuple[PrerequisiteSource, ...] = Field(default_factory=tuple)
    terminal_result: StrictStr | None = None
    produces: tuple[StrictStr, ...] = Field(default_factory=tuple)

    @field_validator("node_id")
    @classmethod
    def _node_id_valid(cls, value: str) -> str:
        from millforge.compiler.validators import validate_node_id

        return validate_node_id(value)

    @field_validator("tool_ref")
    @classmethod
    def _tool_ref_valid(cls, value: str) -> str:
        return validate_tool_reference(value)

    @field_validator("prerequisites")
    @classmethod
    def _prerequisites_limited(
        cls, value: tuple[PrerequisiteSource, ...]
    ) -> tuple[PrerequisiteSource, ...]:
        if len(value) > MAX_PREREQUISITES_PER_NODE:
            raise ValueError("prerequisites may contain at most 64 entries")
        return value

    @field_validator("terminal_result")
    @classmethod
    def _terminal_result_valid(cls, value: str | None) -> str | None:
        return None if value is None else validate_terminal_result(value)

    @field_validator("produces")
    @classmethod
    def _produces_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            validate_artifact_id(item)
        return validate_unique(value, "produces")


class HarnessGraphSource(BaseModel):
    """Immutable source graph carrying node IDs from the authoring mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: tuple[HarnessNodeSource, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _convert_node_mapping(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        raw_nodes = values.get("nodes")
        if isinstance(raw_nodes, Mapping):
            converted = []
            for node_id, raw_node in raw_nodes.items():
                if not isinstance(raw_node, Mapping):
                    converted.append({"node_id": node_id})
                    continue
                if "node_id" in raw_node:
                    raise ValueError(
                        "graph node bodies must not define node_id; the mapping key is authoritative"
                    )
                converted.append({**dict(raw_node), "node_id": node_id})
            values["nodes"] = tuple(converted)
        return values

    @field_validator("nodes")
    @classmethod
    def _nodes_valid(
        cls, value: tuple[HarnessNodeSource, ...]
    ) -> tuple[HarnessNodeSource, ...]:
        if len(value) > MAX_NODES:
            raise ValueError("nodes may contain at most 512 entries")
        validate_unique(tuple(node.node_id for node in value), "node_id")
        terminal_count = sum(node.terminal_result is not None for node in value)
        if terminal_count > MAX_TERMINAL_NODES:
            raise ValueError("nodes may contain at most 64 terminal nodes")
        return value


class StageScopeSource(BaseModel):
    """Immutable source stage-scope declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_kind_ids: tuple[StrictStr, ...] = Field(min_length=1)

    @field_validator("stage_kind_ids")
    @classmethod
    def _stage_kind_ids_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_STAGE_KIND_IDS:
            raise ValueError("stage_kind_ids may contain at most 64 entries")
        for item in value:
            validate_stage_kind_id(item)
        return validate_unique(value, "stage_kind_ids")


class PromptSource(BaseModel):
    """Immutable source prompt-policy declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: StrictStr
    system_instructions: StrictStr
    include_request_context: StrictBool

    @field_validator("policy_id")
    @classmethod
    def _policy_id_valid(cls, value: str) -> str:
        return validate_policy_id(value)

    @field_validator("system_instructions")
    @classmethod
    def _system_instructions_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("system_instructions must be nonblank")
        return validate_utf8_size(
            value, "system_instructions", MAX_SYSTEM_INSTRUCTIONS_UTF8
        )


class BudgetSource(BaseModel):
    """Immutable source budget declaration with no implicit defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: StrictInt = Field(ge=1, le=256)
    max_validation_retries: StrictInt = Field(ge=1, le=64)
    max_tool_errors: StrictInt = Field(ge=1, le=64)
    max_prerequisite_violations: StrictInt = Field(ge=1, le=64)
    max_premature_terminal_attempts: StrictInt = Field(ge=1, le=64)


class ContextPolicySource(BaseModel):
    """Immutable source context-policy declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: Literal["forge.tiered.v1"]
    budget_tokens: StrictInt = Field(ge=256, le=1_000_000)
    keep_recent_iterations: StrictInt = Field(ge=0, le=64)
    phase_thresholds: tuple[float, float, float]

    @field_validator("phase_thresholds", mode="before")
    @classmethod
    def _thresholds_scalar_valid(cls, value: Any) -> tuple[float, float, float]:
        if not isinstance(value, (tuple, list)) or len(value) != 3:
            raise ValueError("phase_thresholds must contain exactly three values")
        thresholds = tuple(
            validate_threshold(item, "phase_thresholds") for item in value
        )
        return (thresholds[0], thresholds[1], thresholds[2])

    @field_validator("phase_thresholds")
    @classmethod
    def _thresholds_ordered(
        cls, value: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("phase_thresholds must be finite")
        if tuple(sorted(value)) != value:
            raise ValueError("phase_thresholds must be non-decreasing")
        return value


class TerminalArtifactPolicySource(BaseModel):
    """Immutable terminal-to-required-artifacts policy entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    terminal_result: StrictStr
    artifact_ids: tuple[StrictStr, ...] = Field(min_length=1)

    @field_validator("terminal_result")
    @classmethod
    def _terminal_result_valid(cls, value: str) -> str:
        return validate_terminal_result(value)

    @field_validator("artifact_ids")
    @classmethod
    def _artifact_ids_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_ARTIFACTS_PER_TERMINAL:
            raise ValueError("artifact_ids may contain at most 512 entries")
        for item in value:
            validate_artifact_id(item)
        return validate_unique(value, "artifact_ids")


class ArtifactPolicySource(BaseModel):
    """Immutable source artifact policy declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    declared_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    required_by_terminal: tuple[TerminalArtifactPolicySource, ...] = Field(
        default_factory=tuple
    )

    @model_validator(mode="before")
    @classmethod
    def _convert_terminal_mapping(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        raw_required = values.get("required_by_terminal")
        if isinstance(raw_required, Mapping):
            values["required_by_terminal"] = tuple(
                {"terminal_result": terminal_result, "artifact_ids": artifact_ids}
                for terminal_result, artifact_ids in raw_required.items()
            )
        return values

    @field_validator("declared_artifact_ids")
    @classmethod
    def _declared_artifact_ids_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_DECLARED_ARTIFACTS:
            raise ValueError("declared_artifact_ids may contain at most 512 entries")
        for item in value:
            validate_artifact_id(item)
        return validate_unique(value, "declared_artifact_ids")

    @field_validator("required_by_terminal")
    @classmethod
    def _required_by_terminal_limited(
        cls, value: tuple[TerminalArtifactPolicySource, ...]
    ) -> tuple[TerminalArtifactPolicySource, ...]:
        if len(value) > MAX_TERMINAL_ARTIFACT_POLICIES:
            raise ValueError("required_by_terminal may contain at most 64 entries")
        validate_unique(
            tuple(item.terminal_result for item in value), "required_by_terminal"
        )
        return value


class HarnessSource(BaseModel):
    """Immutable validated Millforge harness source contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    kind: Literal["millforge_harness"]
    harness_id: StrictStr
    harness_version: StrictInt
    stage_scope: StageScopeSource
    model_profile_id: StrictStr
    prompt: PromptSource
    budgets: BudgetSource
    context: ContextPolicySource
    graph: HarnessGraphSource
    artifacts: ArtifactPolicySource

    @field_validator("harness_id")
    @classmethod
    def _harness_id_valid(cls, value: str) -> str:
        return validate_harness_id(value)

    @field_validator("harness_version")
    @classmethod
    def _harness_version_valid(cls, value: int) -> int:
        return validate_harness_version(value)

    @field_validator("model_profile_id")
    @classmethod
    def _model_profile_id_valid(cls, value: str) -> str:
        return validate_profile_id(value)
