"""Public Spec 07 compact-eval preset metadata.

This module exposes the intended Millforge compact-eval harness surface without
claiming that any preset has been implemented, compiled, admitted, or made
execution-ready.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from pydantic import field_serializer, field_validator, model_validator

from millforge.eval_modes import EVAL_DEFAULT_MODEL_PROFILE_ID
from millforge.eval_modes import EVAL_SPEC_07_HARNESS_IDS
from millforge.eval_workflow import EvalStageId
from millforge.eval_workflow import EvalTerminalResult
from millforge.eval_workflow import default_compact_eval_workflow_graph

EVAL_PRESET_SCHEMA_VERSION = 1
EVAL_PRESET_MODEL_PROFILE_ID = EVAL_DEFAULT_MODEL_PROFILE_ID
EVAL_PRESET_HARNESS_IDS: Mapping[EvalStageId, str] = MappingProxyType(
    dict(EVAL_SPEC_07_HARNESS_IDS)
)

_DENIED_PUBLIC_TOKENS = (
    "millrace-agents",
    "ideas/",
    "ref-forge",
    "api_key",
    "credential",
    "password",
    "provider endpoint",
    "live run",
    "live-run",
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'`([{<])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`)\]}>,]*"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'`([{<])/(?!/)[^\s\"'`)\]}>,]*")
_USER_HOME_PATH = re.compile(r"(?:^|[\s\"'`([{<])~(?:/|\\)[^\s\"'`)\]}>,]*")


class EvalPresetId(str, Enum):
    """Closed public IDs for planned Spec 07 compact-eval presets."""

    PLANNER_SINGLE_TASK = "millforge.eval.preset.planner.single_task.v1"
    BUILDER_CODE_PATCH = "millforge.eval.preset.builder.code_patch.v1"
    CHECKER_EVIDENCE_REVIEW = "millforge.eval.preset.checker.evidence_review.v1"
    ARBITER_CLOSURE = "millforge.eval.preset.arbiter.closure.v1"


class EvalPresetReadinessStatus(str, Enum):
    """Readiness states that deliberately exclude execution-ready claims."""

    MISSING = "missing"
    BLOCKED_BY_CONTRACT_GAP = "blocked_by_contract_gap"


class EvalPresetSourceRecord(BaseModel):
    """Immutable metadata for one planned Spec 07 preset source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = EVAL_PRESET_SCHEMA_VERSION
    preset_id: EvalPresetId
    harness_id: StrictStr
    stage_id: EvalStageId
    model_profile_id: StrictStr = EVAL_PRESET_MODEL_PROFILE_ID
    readiness_status: EvalPresetReadinessStatus
    contract_gap_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    compiler_capability_ids: tuple[StrictStr, ...]
    workspace_tool_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    workspace_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    implemented: StrictBool = False
    compiled: StrictBool = False
    statically_available: StrictBool = False
    live_admitted: StrictBool = False
    execution_ready: StrictBool = False

    @model_validator(mode="after")
    def _source_record_valid(self) -> EvalPresetSourceRecord:
        if self.schema_version != EVAL_PRESET_SCHEMA_VERSION:
            raise ValueError("unsupported eval preset schema version")
        if self.harness_id != EVAL_PRESET_HARNESS_IDS[self.stage_id]:
            raise ValueError("preset harness_id must match the Spec 07 harness map")
        if self.model_profile_id != EVAL_PRESET_MODEL_PROFILE_ID:
            raise ValueError("preset model profile must match the default eval profile")
        if (
            self.implemented
            or self.compiled
            or self.statically_available
            or self.live_admitted
            or self.execution_ready
        ):
            raise ValueError("Spec 07 presets must not claim implementation readiness")
        if (
            self.readiness_status == EvalPresetReadinessStatus.BLOCKED_BY_CONTRACT_GAP
            and not self.contract_gap_ids
        ):
            raise ValueError("blocked presets must identify at least one contract gap")
        if self.stage_id == EvalStageId.PLANNER:
            if self.workspace_tool_ids or self.workspace_paths:
                raise ValueError("planner preset must not declare workspace authority")
            if "workspace.read" in self.compiler_capability_ids:
                raise ValueError("planner preset must not declare workspace.read")
        _reject_public_material_leaks(self.model_dump(mode="json"))
        return self


class EvalPresetCompileCase(BaseModel):
    """Compiler-facing metadata for one planned Spec 07 preset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = EVAL_PRESET_SCHEMA_VERSION
    preset_id: EvalPresetId
    harness_id: StrictStr
    stage_id: EvalStageId
    legal_terminal_results: tuple[EvalTerminalResult, ...]
    input_artifact_ids: tuple[StrictStr, ...]
    output_artifact_ids: tuple[StrictStr, ...]
    required_capability_ids: tuple[StrictStr, ...]
    readiness_status: EvalPresetReadinessStatus
    contract_gap_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _compile_case_valid(self) -> EvalPresetCompileCase:
        graph = default_compact_eval_workflow_graph()
        contract = graph.stage_contracts[self.stage_id]
        if self.schema_version != EVAL_PRESET_SCHEMA_VERSION:
            raise ValueError("unsupported eval preset schema version")
        if self.harness_id != EVAL_PRESET_HARNESS_IDS[self.stage_id]:
            raise ValueError("compile case harness_id must match the Spec 07 map")
        if self.legal_terminal_results != contract.legal_terminal_results:
            raise ValueError("compile case terminal results must match the graph")
        if self.input_artifact_ids != contract.input_artifact_ids:
            raise ValueError("compile case input artifacts must match the graph")
        if self.output_artifact_ids != contract.output_artifact_ids:
            raise ValueError("compile case output artifacts must match the graph")
        _reject_public_material_leaks(self.model_dump(mode="json"))
        return self


class EvalPresetContractGap(BaseModel):
    """Known contract gap blocking Spec 07 preset readiness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gap_id: StrictStr
    owner: StrictStr
    summary: StrictStr
    affected_stage_ids: tuple[EvalStageId, ...]

    @field_validator("gap_id", "owner", "summary")
    @classmethod
    def _text_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("contract gap fields must be non-empty")
        _reject_public_material_leaks(value)
        return value

    @field_validator("affected_stage_ids")
    @classmethod
    def _affected_stage_ids_valid(
        cls, value: tuple[EvalStageId, ...]
    ) -> tuple[EvalStageId, ...]:
        if not value:
            raise ValueError("contract gaps must identify affected stages")
        if len(set(value)) != len(value):
            raise ValueError("affected stages must be unique")
        return value

    @field_serializer("affected_stage_ids")
    def _serialize_stage_ids(self, value: tuple[EvalStageId, ...]) -> tuple[str, ...]:
        return tuple(stage_id.value for stage_id in value)


_PRESET_IDS: Mapping[EvalStageId, EvalPresetId] = MappingProxyType(
    {
        EvalStageId.PLANNER: EvalPresetId.PLANNER_SINGLE_TASK,
        EvalStageId.BUILDER: EvalPresetId.BUILDER_CODE_PATCH,
        EvalStageId.CHECKER: EvalPresetId.CHECKER_EVIDENCE_REVIEW,
        EvalStageId.ARBITER: EvalPresetId.ARBITER_CLOSURE,
    }
)
_STAGE_GAP_IDS: Mapping[EvalStageId, tuple[str, ...]] = MappingProxyType(
    {
        EvalStageId.PLANNER: (
            "planner-workspace-authority-preservation",
            "readiness-before-live-admission",
        ),
        EvalStageId.BUILDER: (
            "duplicate-exact-tool-references",
            "artifact-evidence-production",
            "capability-vocabulary-projection",
            "readiness-before-live-admission",
        ),
        EvalStageId.CHECKER: (
            "duplicate-exact-tool-references",
            "artifact-evidence-production",
            "capability-vocabulary-projection",
            "stale-review-draft-model-profile",
            "readiness-before-live-admission",
        ),
        EvalStageId.ARBITER: (
            "artifact-evidence-production",
            "capability-vocabulary-projection",
            "readiness-before-live-admission",
        ),
    }
)
_STAGE_CAPABILITY_IDS: Mapping[EvalStageId, tuple[str, ...]] = MappingProxyType(
    {
        EvalStageId.PLANNER: (
            "request.read",
            "artifact.read",
            "artifact.write",
            "terminal.intent",
        ),
        EvalStageId.BUILDER: (
            "request.read",
            "artifact.read",
            "artifact.write",
            "workspace.diff.read",
            "process.static_check",
            "process.test",
            "terminal.intent",
        ),
        EvalStageId.CHECKER: (
            "request.read",
            "artifact.read",
            "artifact.write",
            "process.static_check",
            "process.test",
            "terminal.intent",
        ),
        EvalStageId.ARBITER: (
            "request.read",
            "artifact.read",
            "artifact.write",
            "terminal.intent",
        ),
    }
)
_STAGE_WORKSPACE_TOOL_IDS: Mapping[EvalStageId, tuple[str, ...]] = MappingProxyType(
    {
        EvalStageId.PLANNER: (),
        EvalStageId.BUILDER: ("workspace.diff.read",),
        EvalStageId.CHECKER: (),
        EvalStageId.ARBITER: (),
    }
)
_STAGE_WORKSPACE_PATHS: Mapping[EvalStageId, tuple[str, ...]] = MappingProxyType(
    {
        EvalStageId.PLANNER: (),
        EvalStageId.BUILDER: ("src", "tests"),
        EvalStageId.CHECKER: (),
        EvalStageId.ARBITER: (),
    }
)


def _reject_public_material_leaks(value: Any) -> None:
    rendered = value if isinstance(value, str) else _stable_json(value)
    lowered = rendered.lower()
    for token in _DENIED_PUBLIC_TOKENS:
        if token.lower() in lowered:
            raise ValueError("eval preset metadata contains non-public material")
    if (
        _WINDOWS_ABSOLUTE_PATH.search(rendered)
        or _POSIX_ABSOLUTE_PATH.search(rendered)
        or _USER_HOME_PATH.search(rendered)
    ):
        raise ValueError("eval preset metadata must not contain host paths")


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        default=str,
    )


_CONTRACT_GAPS: tuple[EvalPresetContractGap, ...] = (
    EvalPresetContractGap(
        gap_id="duplicate-exact-tool-references",
        owner="07B",
        summary="Compiler fixture source still needs a resolution for duplicate exact tool references.",
        affected_stage_ids=(EvalStageId.BUILDER, EvalStageId.CHECKER),
    ),
    EvalPresetContractGap(
        gap_id="artifact-evidence-production",
        owner="07C",
        summary="Preset compilation has not yet defined artifact evidence production.",
        affected_stage_ids=(
            EvalStageId.BUILDER,
            EvalStageId.CHECKER,
            EvalStageId.ARBITER,
        ),
    ),
    EvalPresetContractGap(
        gap_id="capability-vocabulary-projection",
        owner="07B/07C",
        summary="Compiler grants still need projection from preset metadata to the tool catalog vocabulary.",
        affected_stage_ids=(
            EvalStageId.BUILDER,
            EvalStageId.CHECKER,
            EvalStageId.ARBITER,
        ),
    ),
    EvalPresetContractGap(
        gap_id="stale-review-draft-model-profile",
        owner="07B",
        summary="Review-draft model profile references need reconciliation with the default eval profile.",
        affected_stage_ids=(EvalStageId.CHECKER,),
    ),
    EvalPresetContractGap(
        gap_id="readiness-before-live-admission",
        owner="07D/07E",
        summary="Readiness semantics must remain blocked until live admission is defined.",
        affected_stage_ids=tuple(EvalStageId),
    ),
    EvalPresetContractGap(
        gap_id="planner-workspace-authority-preservation",
        owner="07B",
        summary="Planner preset metadata must preserve the no-workspace-authority contract.",
        affected_stage_ids=(EvalStageId.PLANNER,),
    ),
)


def iter_eval_preset_source_records() -> tuple[EvalPresetSourceRecord, ...]:
    """Return deterministic planned Spec 07 preset source records."""
    return _SOURCE_RECORDS


def eval_preset_source_record(harness_id: str) -> EvalPresetSourceRecord:
    """Return one planned Spec 07 preset source record by harness ID."""
    try:
        return _SOURCE_RECORD_BY_HARNESS_ID[harness_id]
    except KeyError as exc:
        raise KeyError(f"unknown Spec 07 eval preset harness_id: {harness_id}") from exc


def iter_eval_preset_compile_cases() -> tuple[EvalPresetCompileCase, ...]:
    """Return deterministic compiler-facing cases for the planned presets."""
    return _COMPILE_CASES


def eval_preset_contract_gaps() -> tuple[EvalPresetContractGap, ...]:
    """Return known contract gaps that keep all planned presets unready."""
    return _CONTRACT_GAPS


def _source_record_for_stage(stage_id: EvalStageId) -> EvalPresetSourceRecord:
    status = (
        EvalPresetReadinessStatus.MISSING
        if stage_id == EvalStageId.PLANNER
        else EvalPresetReadinessStatus.BLOCKED_BY_CONTRACT_GAP
    )
    return EvalPresetSourceRecord(
        preset_id=_PRESET_IDS[stage_id],
        harness_id=EVAL_PRESET_HARNESS_IDS[stage_id],
        stage_id=stage_id,
        readiness_status=status,
        contract_gap_ids=_STAGE_GAP_IDS[stage_id],
        compiler_capability_ids=_STAGE_CAPABILITY_IDS[stage_id],
        workspace_tool_ids=_STAGE_WORKSPACE_TOOL_IDS[stage_id],
        workspace_paths=_STAGE_WORKSPACE_PATHS[stage_id],
    )


def _compile_case_for_stage(stage_id: EvalStageId) -> EvalPresetCompileCase:
    graph = default_compact_eval_workflow_graph()
    contract = graph.stage_contracts[stage_id]
    return EvalPresetCompileCase(
        preset_id=_PRESET_IDS[stage_id],
        harness_id=EVAL_PRESET_HARNESS_IDS[stage_id],
        stage_id=stage_id,
        legal_terminal_results=contract.legal_terminal_results,
        input_artifact_ids=contract.input_artifact_ids,
        output_artifact_ids=contract.output_artifact_ids,
        required_capability_ids=_STAGE_CAPABILITY_IDS[stage_id],
        readiness_status=EvalPresetReadinessStatus.BLOCKED_BY_CONTRACT_GAP,
        contract_gap_ids=_STAGE_GAP_IDS[stage_id],
    )


_SOURCE_RECORDS: tuple[EvalPresetSourceRecord, ...] = tuple(
    _source_record_for_stage(stage_id) for stage_id in EvalStageId
)
_SOURCE_RECORD_BY_HARNESS_ID: Mapping[str, EvalPresetSourceRecord] = MappingProxyType(
    {record.harness_id: record for record in _SOURCE_RECORDS}
)
_COMPILE_CASES: tuple[EvalPresetCompileCase, ...] = tuple(
    _compile_case_for_stage(stage_id) for stage_id in EvalStageId
)

__all__ = [
    "EVAL_PRESET_HARNESS_IDS",
    "EVAL_PRESET_MODEL_PROFILE_ID",
    "EVAL_PRESET_SCHEMA_VERSION",
    "EvalPresetCompileCase",
    "EvalPresetContractGap",
    "EvalPresetId",
    "EvalPresetReadinessStatus",
    "EvalPresetSourceRecord",
    "eval_preset_contract_gaps",
    "eval_preset_source_record",
    "iter_eval_preset_compile_cases",
    "iter_eval_preset_source_records",
]
