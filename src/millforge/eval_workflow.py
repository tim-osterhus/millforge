"""Compact public eval workflow contracts for Millforge."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from pydantic import field_serializer, field_validator, model_validator


class EvalStageId(str, Enum):
    """Closed stage IDs for the compact eval workflow."""

    PLANNER = "eval_planner"
    BUILDER = "eval_builder"
    CHECKER = "eval_checker"
    ARBITER = "eval_arbiter"


class EvalTerminalResult(str, Enum):
    """Closed terminal results emitted by compact eval stages."""

    PLAN_READY = "PLAN_READY"
    PLAN_BLOCKED = "PLAN_BLOCKED"
    BUILDER_COMPLETE = "BUILDER_COMPLETE"
    BUILDER_BLOCKED = "BUILDER_BLOCKED"
    CHECKER_APPROVED = "CHECKER_APPROVED"
    CHECKER_REJECTED = "CHECKER_REJECTED"
    CHECKER_BLOCKED = "CHECKER_BLOCKED"
    ARBITER_CLOSED = "ARBITER_CLOSED"
    ARBITER_REJECTED = "ARBITER_REJECTED"
    ARBITER_BLOCKED = "ARBITER_BLOCKED"


class EvalWorkflowOutcomeKind(str, Enum):
    """Closed workflow transition outcome kinds."""

    CONTINUE = "continue"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    INVALID = "invalid"


class EvalCandidateDisposition(str, Enum):
    """Closed candidate dispositions carried between compact eval stages."""

    NONE = "none"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"


OMITTED_COMPACT_EVAL_STAGE_IDS: tuple[str, ...] = (
    "manager",
    "fixer",
    "doublechecker",
    "troubleshooter",
    "consultant",
    "mechanic",
    "auditor",
    "updater",
    "librarian",
    "analyst",
    "professor",
    "curator",
)


class EvalStageContract(BaseModel):
    """Immutable contract for one compact eval workflow stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: EvalStageId
    role_summary: StrictStr
    input_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    output_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    legal_terminal_results: tuple[EvalTerminalResult, ...]
    domain_attempt_limit: StrictInt = Field(gt=0)
    infrastructure_retry_limit: StrictInt = Field(ge=0, le=1)
    may_complete_workflow: StrictBool

    @field_validator("role_summary")
    @classmethod
    def _role_summary_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("role_summary must be a non-empty string")
        return value

    @field_validator("input_artifact_ids", "output_artifact_ids")
    @classmethod
    def _artifact_ids_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("artifact ids must be unique")
        for artifact_id in value:
            if not artifact_id.strip():
                raise ValueError("artifact ids must be non-empty strings")
        return value

    @field_validator("legal_terminal_results")
    @classmethod
    def _legal_terminal_results_valid(
        cls, value: tuple[EvalTerminalResult, ...]
    ) -> tuple[EvalTerminalResult, ...]:
        if not value:
            raise ValueError("legal_terminal_results must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("legal_terminal_results values must be unique")
        return value


class EvalAttemptState(BaseModel):
    """Immutable domain-attempt and infrastructure-retry counters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    planner_attempts: StrictInt = Field(default=0, ge=0)
    builder_attempts: StrictInt = Field(default=0, ge=0)
    checker_attempts: StrictInt = Field(default=0, ge=0)
    arbiter_attempts: StrictInt = Field(default=0, ge=0)
    infrastructure_retries: Mapping[EvalStageId, StrictInt] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _freeze_retries(self) -> EvalAttemptState:
        ordered: dict[EvalStageId, int] = {}
        for stage_id in EvalStageId:
            retry_count = self.infrastructure_retries.get(stage_id, 0)
            if retry_count < 0:
                raise ValueError("infrastructure retry counts must be non-negative")
            if retry_count > 1:
                raise ValueError("infrastructure retry counts may not exceed 1")
            if retry_count:
                ordered[stage_id] = retry_count
        object.__setattr__(self, "infrastructure_retries", MappingProxyType(ordered))
        return self

    @field_serializer("infrastructure_retries")
    def _serialize_retries(self, value: Mapping[EvalStageId, int]) -> dict[str, int]:
        return {
            stage_id.value: value[stage_id]
            for stage_id in EvalStageId
            if stage_id in value
        }


class EvalTransitionDecision(BaseModel):
    """Immutable transition decision for a compact eval workflow result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome_kind: EvalWorkflowOutcomeKind
    current_stage_id: EvalStageId | StrictStr
    terminal_result: EvalTerminalResult | StrictStr
    next_stage_id: EvalStageId | None = None
    candidate_disposition: EvalCandidateDisposition = EvalCandidateDisposition.NONE
    diagnostic_code: StrictStr | None = None
    diagnostic_summary: StrictStr | None = None

    @model_validator(mode="after")
    def _decision_shape_valid(self) -> EvalTransitionDecision:
        if self.outcome_kind == EvalWorkflowOutcomeKind.CONTINUE:
            if self.next_stage_id is None:
                raise ValueError("continue decisions must declare next_stage_id")
            if self.diagnostic_code is not None or self.diagnostic_summary is not None:
                raise ValueError("continue decisions must not include diagnostics")
        else:
            if self.next_stage_id is not None:
                raise ValueError("terminal decisions must not declare next_stage_id")
            if self.outcome_kind == EvalWorkflowOutcomeKind.INVALID:
                if self.diagnostic_code is None or self.diagnostic_summary is None:
                    raise ValueError("invalid decisions must include diagnostics")
        if (self.diagnostic_code is None) != (self.diagnostic_summary is None):
            raise ValueError(
                "diagnostic_code and diagnostic_summary must appear together"
            )
        return self


class CompactEvalWorkflowGraph(BaseModel):
    """Immutable static compact eval workflow graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_id: StrictStr = "compact_eval_workflow.v1"
    stages: tuple[EvalStageContract, ...]

    @model_validator(mode="before")
    @classmethod
    def _reject_mapping_stages(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        if isinstance(values.get("stages"), Mapping):
            raise ValueError("stages must be an ordered tuple or list")
        return values

    @model_validator(mode="after")
    def _graph_shape_valid(self) -> CompactEvalWorkflowGraph:
        expected_stage_ids = tuple(stage_id for stage_id in EvalStageId)
        actual_stage_ids = tuple(stage.stage_id for stage in self.stages)
        if actual_stage_ids != expected_stage_ids:
            raise ValueError(
                "compact eval workflow stages must be exactly "
                "eval_planner, eval_builder, eval_checker, eval_arbiter"
            )
        expected_results = {
            EvalStageId.PLANNER: (
                EvalTerminalResult.PLAN_READY,
                EvalTerminalResult.PLAN_BLOCKED,
            ),
            EvalStageId.BUILDER: (
                EvalTerminalResult.BUILDER_COMPLETE,
                EvalTerminalResult.BUILDER_BLOCKED,
            ),
            EvalStageId.CHECKER: (
                EvalTerminalResult.CHECKER_APPROVED,
                EvalTerminalResult.CHECKER_REJECTED,
                EvalTerminalResult.CHECKER_BLOCKED,
            ),
            EvalStageId.ARBITER: (
                EvalTerminalResult.ARBITER_CLOSED,
                EvalTerminalResult.ARBITER_REJECTED,
                EvalTerminalResult.ARBITER_BLOCKED,
            ),
        }
        expected_domain_limits = {
            EvalStageId.PLANNER: 1,
            EvalStageId.BUILDER: 2,
            EvalStageId.CHECKER: 2,
            EvalStageId.ARBITER: 1,
        }
        for stage in self.stages:
            if stage.legal_terminal_results != expected_results[stage.stage_id]:
                raise ValueError(
                    f"{stage.stage_id.value} legal_terminal_results are invalid"
                )
            if stage.domain_attempt_limit != expected_domain_limits[stage.stage_id]:
                raise ValueError(
                    f"{stage.stage_id.value} domain_attempt_limit is invalid"
                )
            if stage.infrastructure_retry_limit > 1:
                raise ValueError(
                    f"{stage.stage_id.value} infrastructure_retry_limit is invalid"
                )
            expected_completion = stage.stage_id == EvalStageId.ARBITER
            if stage.may_complete_workflow is not expected_completion:
                raise ValueError(
                    f"{stage.stage_id.value} completion permission is invalid"
                )
        return self

    @property
    def stage_ids(self) -> tuple[EvalStageId, ...]:
        """Return stage IDs in graph order."""
        return tuple(stage.stage_id for stage in self.stages)

    @property
    def stage_contracts(self) -> Mapping[EvalStageId, EvalStageContract]:
        """Return stage contracts keyed by stage ID."""
        return MappingProxyType({stage.stage_id: stage for stage in self.stages})


def default_compact_eval_workflow_graph() -> CompactEvalWorkflowGraph:
    """Return the static compact eval workflow graph contract."""
    return CompactEvalWorkflowGraph(
        stages=(
            EvalStageContract(
                stage_id=EvalStageId.PLANNER,
                role_summary="Plan a compact eval candidate workflow.",
                input_artifact_ids=("task", "fixture_manifest", "acceptance_checks"),
                output_artifact_ids=("plan",),
                legal_terminal_results=(
                    EvalTerminalResult.PLAN_READY,
                    EvalTerminalResult.PLAN_BLOCKED,
                ),
                domain_attempt_limit=1,
                infrastructure_retry_limit=1,
                may_complete_workflow=False,
            ),
            EvalStageContract(
                stage_id=EvalStageId.BUILDER,
                role_summary="Build the candidate implementation under eval.",
                input_artifact_ids=(
                    "task",
                    "fixture_manifest",
                    "plan",
                    "checker_verdict",
                ),
                output_artifact_ids=(
                    "workspace_diff",
                    "patch_summary",
                    "test_results",
                ),
                legal_terminal_results=(
                    EvalTerminalResult.BUILDER_COMPLETE,
                    EvalTerminalResult.BUILDER_BLOCKED,
                ),
                domain_attempt_limit=2,
                infrastructure_retry_limit=1,
                may_complete_workflow=False,
            ),
            EvalStageContract(
                stage_id=EvalStageId.CHECKER,
                role_summary="Check the candidate implementation against the eval plan.",
                input_artifact_ids=(
                    "task",
                    "fixture_manifest",
                    "plan",
                    "workspace_diff",
                    "patch_summary",
                    "test_results",
                ),
                output_artifact_ids=("checker_verdict",),
                legal_terminal_results=(
                    EvalTerminalResult.CHECKER_APPROVED,
                    EvalTerminalResult.CHECKER_REJECTED,
                    EvalTerminalResult.CHECKER_BLOCKED,
                ),
                domain_attempt_limit=2,
                infrastructure_retry_limit=1,
                may_complete_workflow=False,
            ),
            EvalStageContract(
                stage_id=EvalStageId.ARBITER,
                role_summary="Settle the compact eval candidate outcome.",
                input_artifact_ids=(
                    "task",
                    "fixture_manifest",
                    "plan",
                    "workspace_diff",
                    "patch_summary",
                    "test_results",
                    "checker_verdict",
                ),
                output_artifact_ids=("arbiter_verdict",),
                legal_terminal_results=(
                    EvalTerminalResult.ARBITER_CLOSED,
                    EvalTerminalResult.ARBITER_REJECTED,
                    EvalTerminalResult.ARBITER_BLOCKED,
                ),
                domain_attempt_limit=1,
                infrastructure_retry_limit=1,
                may_complete_workflow=True,
            ),
        )
    )


def _canonical_json_serialize(obj: Any) -> str:
    return (
        json.dumps(
            obj,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).replace("\r\n", "\n")
        + "\n"
    )


def _diagnostic_decision(
    *,
    code: str,
    summary: str,
    current_stage_id: EvalStageId | str,
    terminal_result: EvalTerminalResult | str,
) -> EvalTransitionDecision:
    return EvalTransitionDecision(
        outcome_kind=EvalWorkflowOutcomeKind.INVALID,
        current_stage_id=current_stage_id,
        terminal_result=terminal_result,
        diagnostic_code=code,
        diagnostic_summary=summary,
    )


def _coerce_stage_id(value: EvalStageId | str) -> EvalStageId | None:
    if isinstance(value, EvalStageId):
        return value
    try:
        return EvalStageId(value)
    except ValueError:
        return None


def _coerce_terminal_result(
    value: EvalTerminalResult | str,
) -> EvalTerminalResult | None:
    if isinstance(value, EvalTerminalResult):
        return value
    try:
        return EvalTerminalResult(value)
    except ValueError:
        return None


def _raw_attempt_mapping(
    attempt_state: EvalAttemptState | Mapping[str, Any],
) -> Mapping[str, Any]:
    if isinstance(attempt_state, EvalAttemptState):
        return attempt_state.model_dump(mode="json")
    return attempt_state


def _attempt_state_diagnostic(
    *,
    current_stage_id: EvalStageId,
    terminal_result: EvalTerminalResult,
    attempt_state: EvalAttemptState | Mapping[str, Any],
    graph: CompactEvalWorkflowGraph,
) -> EvalTransitionDecision | None:
    raw_attempts = _raw_attempt_mapping(attempt_state)
    raw_retries = raw_attempts.get("infrastructure_retries", {})
    if not isinstance(raw_retries, Mapping):
        return _diagnostic_decision(
            code="MF-EVAL-G004",
            summary="infrastructure retry state is not a mapping",
            current_stage_id=current_stage_id,
            terminal_result=terminal_result,
        )
    for raw_stage_id, raw_retry_count in raw_retries.items():
        stage_id = _coerce_stage_id(raw_stage_id)
        if stage_id is None:
            return _diagnostic_decision(
                code="MF-EVAL-G004",
                summary="infrastructure retry state contains an unknown stage",
                current_stage_id=current_stage_id,
                terminal_result=terminal_result,
            )
        if not isinstance(raw_retry_count, int) or isinstance(raw_retry_count, bool):
            return _diagnostic_decision(
                code="MF-EVAL-G004",
                summary="infrastructure retry counts must be integers",
                current_stage_id=current_stage_id,
                terminal_result=terminal_result,
            )
        if raw_retry_count < 0 or raw_retry_count > 1:
            return _diagnostic_decision(
                code="MF-EVAL-G004",
                summary="infrastructure retry count exceeds the compact graph limit",
                current_stage_id=current_stage_id,
                terminal_result=terminal_result,
            )

    limits = {stage.stage_id: stage.domain_attempt_limit for stage in graph.stages}
    count_fields = {
        EvalStageId.PLANNER: "planner_attempts",
        EvalStageId.BUILDER: "builder_attempts",
        EvalStageId.CHECKER: "checker_attempts",
        EvalStageId.ARBITER: "arbiter_attempts",
    }
    counts: dict[EvalStageId, int] = {}
    for stage_id, field_name in count_fields.items():
        raw_count = raw_attempts.get(field_name, 0)
        if not isinstance(raw_count, int) or isinstance(raw_count, bool):
            return _diagnostic_decision(
                code="MF-EVAL-G003",
                summary="domain attempt counts must be integers",
                current_stage_id=current_stage_id,
                terminal_result=terminal_result,
            )
        if raw_count < 0 or raw_count > limits[stage_id]:
            return _diagnostic_decision(
                code="MF-EVAL-G003",
                summary="domain attempt count exceeds the compact graph limit",
                current_stage_id=current_stage_id,
                terminal_result=terminal_result,
            )
        counts[stage_id] = raw_count

    if counts[current_stage_id] < 1:
        return _diagnostic_decision(
            code="MF-EVAL-G003",
            summary="resolving stage must have at least one post-terminal attempt",
            current_stage_id=current_stage_id,
            terminal_result=terminal_result,
        )
    if current_stage_id != EvalStageId.PLANNER and counts[EvalStageId.PLANNER] != 1:
        return _diagnostic_decision(
            code="MF-EVAL-G005",
            summary="transition omits the required planner stage",
            current_stage_id=current_stage_id,
            terminal_result=terminal_result,
        )
    if current_stage_id == EvalStageId.CHECKER and counts[EvalStageId.BUILDER] < 1:
        return _diagnostic_decision(
            code="MF-EVAL-G005",
            summary="transition omits the required builder stage",
            current_stage_id=current_stage_id,
            terminal_result=terminal_result,
        )
    if current_stage_id == EvalStageId.ARBITER:
        if counts[EvalStageId.BUILDER] < 1 and counts[EvalStageId.CHECKER] < 1:
            return _diagnostic_decision(
                code="MF-EVAL-G005",
                summary="transition omits candidate evidence before arbiter",
                current_stage_id=current_stage_id,
                terminal_result=terminal_result,
            )
    if current_stage_id != EvalStageId.ARBITER and counts[EvalStageId.ARBITER] > 0:
        return _diagnostic_decision(
            code="MF-EVAL-G003",
            summary="arbiter attempts cannot precede a non-arbiter transition",
            current_stage_id=current_stage_id,
            terminal_result=terminal_result,
        )
    return None


def _continue_decision(
    *,
    current_stage_id: EvalStageId,
    terminal_result: EvalTerminalResult,
    next_stage_id: EvalStageId,
    candidate_disposition: EvalCandidateDisposition = EvalCandidateDisposition.NONE,
    graph: CompactEvalWorkflowGraph,
) -> EvalTransitionDecision:
    if next_stage_id not in graph.stage_contracts:
        return _diagnostic_decision(
            code="MF-EVAL-G005",
            summary="transition routes to a stage omitted from the compact graph",
            current_stage_id=current_stage_id,
            terminal_result=terminal_result,
        )
    return EvalTransitionDecision(
        outcome_kind=EvalWorkflowOutcomeKind.CONTINUE,
        current_stage_id=current_stage_id,
        terminal_result=terminal_result,
        next_stage_id=next_stage_id,
        candidate_disposition=candidate_disposition,
    )


def resolve_eval_transition(
    current_stage_id: EvalStageId | str,
    terminal_result: EvalTerminalResult | str,
    attempt_state: EvalAttemptState | Mapping[str, Any] | None = None,
    *,
    graph: CompactEvalWorkflowGraph | None = None,
) -> EvalTransitionDecision:
    """Resolve a compact eval terminal result into the next workflow decision."""
    graph = graph or default_compact_eval_workflow_graph()
    attempt_state = attempt_state or EvalAttemptState()
    stage_id = _coerce_stage_id(current_stage_id)
    if stage_id is None:
        return _diagnostic_decision(
            code="MF-EVAL-G001",
            summary="unknown compact eval stage id",
            current_stage_id=current_stage_id,
            terminal_result=terminal_result,
        )
    result = _coerce_terminal_result(terminal_result)
    if result is None:
        return _diagnostic_decision(
            code="MF-EVAL-G002",
            summary="unknown compact eval terminal result",
            current_stage_id=stage_id,
            terminal_result=terminal_result,
        )
    if stage_id != EvalStageId.ARBITER and result == EvalTerminalResult.ARBITER_CLOSED:
        return _diagnostic_decision(
            code="MF-EVAL-G006",
            summary="only arbiter may complete the compact eval workflow",
            current_stage_id=stage_id,
            terminal_result=result,
        )
    if result not in graph.stage_contracts[stage_id].legal_terminal_results:
        return _diagnostic_decision(
            code="MF-EVAL-G002",
            summary="terminal result is not legal for the resolving stage",
            current_stage_id=stage_id,
            terminal_result=result,
        )

    invalid_attempts = _attempt_state_diagnostic(
        current_stage_id=stage_id,
        terminal_result=result,
        attempt_state=attempt_state,
        graph=graph,
    )
    if invalid_attempts is not None:
        return invalid_attempts

    counts = EvalAttemptState.model_validate(attempt_state).model_dump(mode="json")
    builder_attempts = counts["builder_attempts"]
    checker_attempts = counts["checker_attempts"]

    if stage_id == EvalStageId.PLANNER:
        if result == EvalTerminalResult.PLAN_READY:
            return _continue_decision(
                current_stage_id=stage_id,
                terminal_result=result,
                next_stage_id=EvalStageId.BUILDER,
                graph=graph,
            )
        return EvalTransitionDecision(
            outcome_kind=EvalWorkflowOutcomeKind.BLOCKED,
            current_stage_id=stage_id,
            terminal_result=result,
        )

    if stage_id == EvalStageId.BUILDER:
        if result == EvalTerminalResult.BUILDER_COMPLETE:
            return _continue_decision(
                current_stage_id=stage_id,
                terminal_result=result,
                next_stage_id=EvalStageId.CHECKER,
                graph=graph,
            )
        return _continue_decision(
            current_stage_id=stage_id,
            terminal_result=result,
            next_stage_id=EvalStageId.ARBITER,
            candidate_disposition=EvalCandidateDisposition.BLOCKED,
            graph=graph,
        )

    if stage_id == EvalStageId.CHECKER:
        if result == EvalTerminalResult.CHECKER_APPROVED:
            return _continue_decision(
                current_stage_id=stage_id,
                terminal_result=result,
                next_stage_id=EvalStageId.ARBITER,
                candidate_disposition=EvalCandidateDisposition.APPROVED,
                graph=graph,
            )
        if result == EvalTerminalResult.CHECKER_BLOCKED:
            return _continue_decision(
                current_stage_id=stage_id,
                terminal_result=result,
                next_stage_id=EvalStageId.ARBITER,
                candidate_disposition=EvalCandidateDisposition.BLOCKED,
                graph=graph,
            )
        if builder_attempts < 2 and checker_attempts < 2:
            return _continue_decision(
                current_stage_id=stage_id,
                terminal_result=result,
                next_stage_id=EvalStageId.BUILDER,
                candidate_disposition=EvalCandidateDisposition.REJECTED,
                graph=graph,
            )
        return _continue_decision(
            current_stage_id=stage_id,
            terminal_result=result,
            next_stage_id=EvalStageId.ARBITER,
            candidate_disposition=EvalCandidateDisposition.REJECTED,
            graph=graph,
        )

    if result == EvalTerminalResult.ARBITER_CLOSED:
        contract = graph.stage_contracts[stage_id]
        if not contract.may_complete_workflow:
            return _diagnostic_decision(
                code="MF-EVAL-G006",
                summary="only arbiter may complete the compact eval workflow",
                current_stage_id=stage_id,
                terminal_result=result,
            )
        return EvalTransitionDecision(
            outcome_kind=EvalWorkflowOutcomeKind.COMPLETED,
            current_stage_id=stage_id,
            terminal_result=result,
        )
    disposition = (
        EvalCandidateDisposition.REJECTED
        if result == EvalTerminalResult.ARBITER_REJECTED
        else EvalCandidateDisposition.BLOCKED
    )
    return EvalTransitionDecision(
        outcome_kind=EvalWorkflowOutcomeKind.BLOCKED,
        current_stage_id=stage_id,
        terminal_result=result,
        candidate_disposition=disposition,
    )


def compact_eval_workflow_snapshot(
    graph: CompactEvalWorkflowGraph | None = None,
) -> dict[str, Any]:
    """Return the deterministic public snapshot for the compact eval graph."""
    graph = graph or default_compact_eval_workflow_graph()
    snapshot: dict[str, Any] = {
        "graph_id": graph.graph_id,
        "omitted_stage_ids": list(OMITTED_COMPACT_EVAL_STAGE_IDS),
        "schema_version": 1,
        "stages": [stage.model_dump(mode="json") for stage in graph.stages],
        "transitions": [
            {
                "current_stage_id": "eval_planner",
                "terminal_result": "PLAN_READY",
                "outcome_kind": "continue",
                "next_stage_id": "eval_builder",
                "candidate_disposition": "none",
            },
            {
                "current_stage_id": "eval_planner",
                "terminal_result": "PLAN_BLOCKED",
                "outcome_kind": "blocked",
                "candidate_disposition": "none",
            },
            {
                "current_stage_id": "eval_builder",
                "terminal_result": "BUILDER_COMPLETE",
                "outcome_kind": "continue",
                "next_stage_id": "eval_checker",
                "candidate_disposition": "none",
            },
            {
                "current_stage_id": "eval_builder",
                "terminal_result": "BUILDER_BLOCKED",
                "outcome_kind": "continue",
                "next_stage_id": "eval_arbiter",
                "candidate_disposition": "blocked",
            },
            {
                "current_stage_id": "eval_checker",
                "terminal_result": "CHECKER_APPROVED",
                "outcome_kind": "continue",
                "next_stage_id": "eval_arbiter",
                "candidate_disposition": "approved",
            },
            {
                "current_stage_id": "eval_checker",
                "terminal_result": "CHECKER_REJECTED",
                "condition": "builder_attempts < 2 and checker_attempts < 2",
                "outcome_kind": "continue",
                "next_stage_id": "eval_builder",
                "candidate_disposition": "rejected",
            },
            {
                "current_stage_id": "eval_checker",
                "terminal_result": "CHECKER_REJECTED",
                "condition": "builder_attempts >= 2 or checker_attempts >= 2",
                "outcome_kind": "continue",
                "next_stage_id": "eval_arbiter",
                "candidate_disposition": "rejected",
            },
            {
                "current_stage_id": "eval_checker",
                "terminal_result": "CHECKER_BLOCKED",
                "outcome_kind": "continue",
                "next_stage_id": "eval_arbiter",
                "candidate_disposition": "blocked",
            },
            {
                "current_stage_id": "eval_arbiter",
                "terminal_result": "ARBITER_CLOSED",
                "outcome_kind": "completed",
                "candidate_disposition": "none",
            },
            {
                "current_stage_id": "eval_arbiter",
                "terminal_result": "ARBITER_REJECTED",
                "outcome_kind": "blocked",
                "candidate_disposition": "rejected",
            },
            {
                "current_stage_id": "eval_arbiter",
                "terminal_result": "ARBITER_BLOCKED",
                "outcome_kind": "blocked",
                "candidate_disposition": "blocked",
            },
        ],
    }
    snapshot["graph_sha256"] = calculate_compact_eval_workflow_sha256(snapshot)
    return snapshot


def calculate_compact_eval_workflow_sha256(snapshot: Mapping[str, Any]) -> str:
    """Hash a compact eval snapshot without its ``graph_sha256`` field."""
    body = dict(snapshot)
    body.pop("graph_sha256", None)
    return hashlib.sha256(_canonical_json_serialize(body).encode("utf-8")).hexdigest()


def canonical_compact_eval_workflow_bytes(
    graph: CompactEvalWorkflowGraph | None = None,
) -> bytes:
    """Serialize the compact eval graph snapshot as canonical UTF-8 JSON bytes."""
    snapshot = compact_eval_workflow_snapshot(graph)
    expected = snapshot["graph_sha256"]
    computed = calculate_compact_eval_workflow_sha256(snapshot)
    if computed != expected:
        raise ValueError("compact eval graph fingerprint verification failed")
    return _canonical_json_serialize(snapshot).encode("utf-8")
