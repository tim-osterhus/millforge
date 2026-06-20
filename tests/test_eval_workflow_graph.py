"""Focused tests for the compact eval workflow contract surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import millforge
from millforge.eval_workflow import (
    CompactEvalWorkflowGraph,
    EvalCandidateDisposition,
    EvalAttemptState,
    EvalStageContract,
    EvalStageId,
    EvalTerminalResult,
    EvalWorkflowOutcomeKind,
    calculate_compact_eval_workflow_sha256,
    canonical_compact_eval_workflow_bytes,
    compact_eval_workflow_snapshot,
    default_compact_eval_workflow_graph,
    resolve_eval_transition,
)

FIXTURE_PATH = Path("tests/fixtures/eval_workflow/compact_graph.json")
OMITTED_STAGE_IDS = (
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


def test_default_graph_contains_exact_four_eval_stages() -> None:
    graph = default_compact_eval_workflow_graph()

    assert graph.stage_ids == (
        EvalStageId.PLANNER,
        EvalStageId.BUILDER,
        EvalStageId.CHECKER,
        EvalStageId.ARBITER,
    )
    assert [stage.stage_id.value for stage in graph.stages] == [
        "eval_planner",
        "eval_builder",
        "eval_checker",
        "eval_arbiter",
    ]


def test_stage_contracts_declare_exact_legal_terminal_results() -> None:
    contracts = default_compact_eval_workflow_graph().stage_contracts

    assert contracts[EvalStageId.PLANNER].legal_terminal_results == (
        EvalTerminalResult.PLAN_READY,
        EvalTerminalResult.PLAN_BLOCKED,
    )
    assert contracts[EvalStageId.BUILDER].legal_terminal_results == (
        EvalTerminalResult.BUILDER_COMPLETE,
        EvalTerminalResult.BUILDER_BLOCKED,
    )
    assert contracts[EvalStageId.CHECKER].legal_terminal_results == (
        EvalTerminalResult.CHECKER_APPROVED,
        EvalTerminalResult.CHECKER_REJECTED,
        EvalTerminalResult.CHECKER_BLOCKED,
    )
    assert contracts[EvalStageId.ARBITER].legal_terminal_results == (
        EvalTerminalResult.ARBITER_CLOSED,
        EvalTerminalResult.ARBITER_REJECTED,
        EvalTerminalResult.ARBITER_BLOCKED,
    )


def test_stage_contracts_declare_exact_artifact_ids() -> None:
    contracts = default_compact_eval_workflow_graph().stage_contracts

    assert contracts[EvalStageId.PLANNER].input_artifact_ids == (
        "task",
        "fixture_manifest",
        "acceptance_checks",
    )
    assert contracts[EvalStageId.PLANNER].output_artifact_ids == ("plan",)
    assert contracts[EvalStageId.BUILDER].input_artifact_ids == (
        "task",
        "fixture_manifest",
        "plan",
        "checker_verdict",
    )
    assert contracts[EvalStageId.BUILDER].output_artifact_ids == (
        "workspace_diff",
        "patch_summary",
        "test_results",
    )
    assert contracts[EvalStageId.CHECKER].input_artifact_ids == (
        "task",
        "fixture_manifest",
        "plan",
        "workspace_diff",
        "patch_summary",
        "test_results",
    )
    assert contracts[EvalStageId.CHECKER].output_artifact_ids == ("checker_verdict",)
    assert contracts[EvalStageId.ARBITER].input_artifact_ids == (
        "task",
        "fixture_manifest",
        "plan",
        "workspace_diff",
        "patch_summary",
        "test_results",
        "checker_verdict",
    )
    assert contracts[EvalStageId.ARBITER].output_artifact_ids == ("arbiter_verdict",)


def test_attempt_and_retry_limits_are_separate_and_exact() -> None:
    contracts = default_compact_eval_workflow_graph().stage_contracts

    assert {
        stage_id: contract.domain_attempt_limit
        for stage_id, contract in contracts.items()
    } == {
        EvalStageId.PLANNER: 1,
        EvalStageId.BUILDER: 2,
        EvalStageId.CHECKER: 2,
        EvalStageId.ARBITER: 1,
    }
    assert {
        stage_id: contract.infrastructure_retry_limit
        for stage_id, contract in contracts.items()
    } == {
        EvalStageId.PLANNER: 1,
        EvalStageId.BUILDER: 1,
        EvalStageId.CHECKER: 1,
        EvalStageId.ARBITER: 1,
    }
    assert not contracts[EvalStageId.PLANNER].may_complete_workflow
    assert not contracts[EvalStageId.BUILDER].may_complete_workflow
    assert not contracts[EvalStageId.CHECKER].may_complete_workflow
    assert contracts[EvalStageId.ARBITER].may_complete_workflow


def test_stage_contracts_are_immutable_and_closed_world() -> None:
    contract = default_compact_eval_workflow_graph().stage_contracts[
        EvalStageId.BUILDER
    ]

    with pytest.raises(ValidationError):
        contract.domain_attempt_limit = 3  # type: ignore[misc]

    with pytest.raises(ValidationError):
        EvalStageContract.model_validate(
            {
                "stage_id": EvalStageId.BUILDER,
                "role_summary": "Build.",
                "input_artifact_ids": ("eval_plan",),
                "output_artifact_ids": ("candidate_patch",),
                "legal_terminal_results": (EvalTerminalResult.BUILDER_COMPLETE,),
                "domain_attempt_limit": 2,
                "infrastructure_retry_limit": 1,
                "may_complete_workflow": False,
                "unexpected": True,
            }
        )


def test_graph_validation_rejects_omitted_or_production_stages() -> None:
    graph = default_compact_eval_workflow_graph()
    stage_values = {stage_id.value for stage_id in graph.stage_ids}

    assert not set(OMITTED_STAGE_IDS) & stage_values
    assert not {"production", "recovery", "learning"} & stage_values

    with pytest.raises(ValueError, match="exactly"):
        CompactEvalWorkflowGraph(stages=graph.stages[:-1])

    with pytest.raises(ValueError):
        EvalStageId("manager")


def test_compact_snapshot_declares_exact_omitted_stage_ids() -> None:
    snapshot = compact_eval_workflow_snapshot()

    assert snapshot["omitted_stage_ids"] == list(OMITTED_STAGE_IDS)
    assert len(snapshot["omitted_stage_ids"]) == len(set(snapshot["omitted_stage_ids"]))
    assert not set(snapshot["omitted_stage_ids"]) & {
        stage.stage_id.value for stage in default_compact_eval_workflow_graph().stages
    }


def test_omitted_stage_ids_are_not_transition_reachability_targets() -> None:
    snapshot = compact_eval_workflow_snapshot()
    omitted_stage_ids = set(snapshot["omitted_stage_ids"])
    transition_next_stage_ids = {
        transition["next_stage_id"]
        for transition in snapshot["transitions"]
        if "next_stage_id" in transition
    }
    legal_next_stage_ids = {
        decision.next_stage_id.value
        for stage_id, terminal_result, attempts in [
            (
                EvalStageId.PLANNER,
                EvalTerminalResult.PLAN_READY,
                EvalAttemptState(planner_attempts=1),
            ),
            (
                EvalStageId.BUILDER,
                EvalTerminalResult.BUILDER_COMPLETE,
                EvalAttemptState(planner_attempts=1, builder_attempts=1),
            ),
            (
                EvalStageId.BUILDER,
                EvalTerminalResult.BUILDER_BLOCKED,
                EvalAttemptState(planner_attempts=1, builder_attempts=1),
            ),
            (
                EvalStageId.CHECKER,
                EvalTerminalResult.CHECKER_APPROVED,
                EvalAttemptState(
                    planner_attempts=1, builder_attempts=1, checker_attempts=1
                ),
            ),
            (
                EvalStageId.CHECKER,
                EvalTerminalResult.CHECKER_REJECTED,
                EvalAttemptState(
                    planner_attempts=1, builder_attempts=1, checker_attempts=1
                ),
            ),
            (
                EvalStageId.CHECKER,
                EvalTerminalResult.CHECKER_REJECTED,
                EvalAttemptState(
                    planner_attempts=1, builder_attempts=2, checker_attempts=1
                ),
            ),
            (
                EvalStageId.CHECKER,
                EvalTerminalResult.CHECKER_BLOCKED,
                EvalAttemptState(
                    planner_attempts=1, builder_attempts=1, checker_attempts=1
                ),
            ),
        ]
        if (
            decision := resolve_eval_transition(stage_id, terminal_result, attempts)
        ).next_stage_id
        is not None
    }

    assert not omitted_stage_ids & transition_next_stage_ids
    assert not omitted_stage_ids & legal_next_stage_ids


@pytest.mark.parametrize("stage_id", OMITTED_STAGE_IDS)
def test_omitted_stage_ids_are_rejected_as_current_stages(stage_id: str) -> None:
    decision = resolve_eval_transition(
        stage_id,
        EvalTerminalResult.PLAN_READY,
        {"planner_attempts": 1},
    )

    assert decision.outcome_kind == EvalWorkflowOutcomeKind.INVALID
    assert decision.current_stage_id == stage_id
    assert decision.diagnostic_code == "MF-EVAL-G001"
    assert decision.diagnostic_summary == "unknown compact eval stage id"


def test_attempt_state_serializes_retries_deterministically() -> None:
    state = EvalAttemptState(
        planner_attempts=1,
        builder_attempts=2,
        checker_attempts=1,
        infrastructure_retries={
            EvalStageId.CHECKER: 1,
            EvalStageId.PLANNER: 1,
        },
    )

    dumped = state.model_dump(mode="json")
    assert dumped["infrastructure_retries"] == {
        "eval_planner": 1,
        "eval_checker": 1,
    }
    assert json.dumps(dumped, sort_keys=True, separators=(",", ":")) == (
        '{"arbiter_attempts":0,"builder_attempts":2,"checker_attempts":1,'
        '"infrastructure_retries":{"eval_checker":1,"eval_planner":1},'
        '"planner_attempts":1}'
    )

    with pytest.raises(ValidationError, match="may not exceed 1"):
        EvalAttemptState(infrastructure_retries={EvalStageId.BUILDER: 2})


@pytest.mark.parametrize(
    (
        "stage_id",
        "terminal_result",
        "attempts",
        "outcome_kind",
        "next_stage_id",
        "candidate_disposition",
    ),
    [
        (
            EvalStageId.PLANNER,
            EvalTerminalResult.PLAN_READY,
            EvalAttemptState(planner_attempts=1),
            EvalWorkflowOutcomeKind.CONTINUE,
            EvalStageId.BUILDER,
            EvalCandidateDisposition.NONE,
        ),
        (
            EvalStageId.PLANNER,
            EvalTerminalResult.PLAN_BLOCKED,
            EvalAttemptState(planner_attempts=1),
            EvalWorkflowOutcomeKind.BLOCKED,
            None,
            EvalCandidateDisposition.NONE,
        ),
        (
            EvalStageId.BUILDER,
            EvalTerminalResult.BUILDER_COMPLETE,
            EvalAttemptState(planner_attempts=1, builder_attempts=1),
            EvalWorkflowOutcomeKind.CONTINUE,
            EvalStageId.CHECKER,
            EvalCandidateDisposition.NONE,
        ),
        (
            EvalStageId.BUILDER,
            EvalTerminalResult.BUILDER_BLOCKED,
            EvalAttemptState(planner_attempts=1, builder_attempts=1),
            EvalWorkflowOutcomeKind.CONTINUE,
            EvalStageId.ARBITER,
            EvalCandidateDisposition.BLOCKED,
        ),
        (
            EvalStageId.CHECKER,
            EvalTerminalResult.CHECKER_APPROVED,
            EvalAttemptState(
                planner_attempts=1, builder_attempts=1, checker_attempts=1
            ),
            EvalWorkflowOutcomeKind.CONTINUE,
            EvalStageId.ARBITER,
            EvalCandidateDisposition.APPROVED,
        ),
        (
            EvalStageId.CHECKER,
            EvalTerminalResult.CHECKER_REJECTED,
            EvalAttemptState(
                planner_attempts=1, builder_attempts=1, checker_attempts=1
            ),
            EvalWorkflowOutcomeKind.CONTINUE,
            EvalStageId.BUILDER,
            EvalCandidateDisposition.REJECTED,
        ),
        (
            EvalStageId.CHECKER,
            EvalTerminalResult.CHECKER_REJECTED,
            EvalAttemptState(
                planner_attempts=1, builder_attempts=2, checker_attempts=1
            ),
            EvalWorkflowOutcomeKind.CONTINUE,
            EvalStageId.ARBITER,
            EvalCandidateDisposition.REJECTED,
        ),
        (
            EvalStageId.CHECKER,
            EvalTerminalResult.CHECKER_REJECTED,
            EvalAttemptState(
                planner_attempts=1, builder_attempts=1, checker_attempts=2
            ),
            EvalWorkflowOutcomeKind.CONTINUE,
            EvalStageId.ARBITER,
            EvalCandidateDisposition.REJECTED,
        ),
        (
            EvalStageId.CHECKER,
            EvalTerminalResult.CHECKER_BLOCKED,
            EvalAttemptState(
                planner_attempts=1, builder_attempts=1, checker_attempts=1
            ),
            EvalWorkflowOutcomeKind.CONTINUE,
            EvalStageId.ARBITER,
            EvalCandidateDisposition.BLOCKED,
        ),
        (
            EvalStageId.ARBITER,
            EvalTerminalResult.ARBITER_CLOSED,
            EvalAttemptState(
                planner_attempts=1,
                builder_attempts=1,
                checker_attempts=1,
                arbiter_attempts=1,
            ),
            EvalWorkflowOutcomeKind.COMPLETED,
            None,
            EvalCandidateDisposition.NONE,
        ),
        (
            EvalStageId.ARBITER,
            EvalTerminalResult.ARBITER_REJECTED,
            EvalAttemptState(
                planner_attempts=1,
                builder_attempts=2,
                checker_attempts=1,
                arbiter_attempts=1,
            ),
            EvalWorkflowOutcomeKind.BLOCKED,
            None,
            EvalCandidateDisposition.REJECTED,
        ),
        (
            EvalStageId.ARBITER,
            EvalTerminalResult.ARBITER_BLOCKED,
            EvalAttemptState(
                planner_attempts=1, builder_attempts=1, arbiter_attempts=1
            ),
            EvalWorkflowOutcomeKind.BLOCKED,
            None,
            EvalCandidateDisposition.BLOCKED,
        ),
    ],
)
def test_resolve_eval_transition_implements_exact_transition_table(
    stage_id: EvalStageId,
    terminal_result: EvalTerminalResult,
    attempts: EvalAttemptState,
    outcome_kind: EvalWorkflowOutcomeKind,
    next_stage_id: EvalStageId | None,
    candidate_disposition: EvalCandidateDisposition,
) -> None:
    decision = resolve_eval_transition(stage_id, terminal_result, attempts)

    assert decision.outcome_kind == outcome_kind
    assert decision.next_stage_id == next_stage_id
    assert decision.candidate_disposition == candidate_disposition
    assert decision.diagnostic_code is None


def test_non_arbiter_stages_never_complete_workflow() -> None:
    checker_approval = resolve_eval_transition(
        EvalStageId.CHECKER,
        EvalTerminalResult.CHECKER_APPROVED,
        EvalAttemptState(planner_attempts=1, builder_attempts=1, checker_attempts=1),
    )
    builder_blocked = resolve_eval_transition(
        EvalStageId.BUILDER,
        EvalTerminalResult.BUILDER_BLOCKED,
        EvalAttemptState(planner_attempts=1, builder_attempts=1),
    )

    assert checker_approval.outcome_kind == EvalWorkflowOutcomeKind.CONTINUE
    assert checker_approval.next_stage_id == EvalStageId.ARBITER
    assert builder_blocked.outcome_kind == EvalWorkflowOutcomeKind.CONTINUE
    assert builder_blocked.next_stage_id == EvalStageId.ARBITER


@pytest.mark.parametrize(
    ("stage_id", "attempts"),
    [
        (EvalStageId.PLANNER, EvalAttemptState(planner_attempts=1)),
        (
            EvalStageId.BUILDER,
            EvalAttemptState(planner_attempts=1, builder_attempts=1),
        ),
        (
            EvalStageId.CHECKER,
            EvalAttemptState(
                planner_attempts=1, builder_attempts=1, checker_attempts=1
            ),
        ),
    ],
)
def test_non_arbiter_completion_attempts_return_g006(
    stage_id: EvalStageId, attempts: EvalAttemptState
) -> None:
    decision = resolve_eval_transition(
        stage_id,
        EvalTerminalResult.ARBITER_CLOSED,
        attempts,
    )

    assert decision.outcome_kind == EvalWorkflowOutcomeKind.INVALID
    assert decision.outcome_kind != EvalWorkflowOutcomeKind.COMPLETED
    assert decision.diagnostic_code == "MF-EVAL-G006"
    assert decision.diagnostic_summary


@pytest.mark.parametrize(
    ("stage_id", "terminal_result", "attempts", "diagnostic_code"),
    [
        ("manager", "PLAN_READY", {"planner_attempts": 1}, "MF-EVAL-G001"),
        (EvalStageId.PLANNER, "NOT_A_RESULT", {"planner_attempts": 1}, "MF-EVAL-G002"),
        (
            EvalStageId.BUILDER,
            EvalTerminalResult.PLAN_READY,
            {"planner_attempts": 1, "builder_attempts": 1},
            "MF-EVAL-G002",
        ),
        (
            EvalStageId.CHECKER,
            EvalTerminalResult.CHECKER_REJECTED,
            {"planner_attempts": 1, "builder_attempts": 1, "checker_attempts": 3},
            "MF-EVAL-G003",
        ),
        (
            EvalStageId.BUILDER,
            EvalTerminalResult.BUILDER_COMPLETE,
            {
                "planner_attempts": 1,
                "builder_attempts": 1,
                "infrastructure_retries": {"eval_builder": 2},
            },
            "MF-EVAL-G004",
        ),
        (
            EvalStageId.CHECKER,
            EvalTerminalResult.CHECKER_APPROVED,
            {"planner_attempts": 1, "checker_attempts": 1},
            "MF-EVAL-G005",
        ),
    ],
)
def test_resolve_eval_transition_returns_stable_invalid_diagnostics(
    stage_id: EvalStageId | str,
    terminal_result: EvalTerminalResult | str,
    attempts: dict[str, object],
    diagnostic_code: str,
) -> None:
    decision = resolve_eval_transition(stage_id, terminal_result, attempts)

    assert decision.outcome_kind == EvalWorkflowOutcomeKind.INVALID
    assert decision.diagnostic_code == diagnostic_code
    assert decision.diagnostic_summary


def test_attempt_validation_treats_counts_as_post_terminal_counts() -> None:
    not_yet_run_checker = resolve_eval_transition(
        EvalStageId.BUILDER,
        EvalTerminalResult.BUILDER_COMPLETE,
        EvalAttemptState(planner_attempts=1, builder_attempts=1, checker_attempts=0),
    )
    current_stage_not_counted = resolve_eval_transition(
        EvalStageId.BUILDER,
        EvalTerminalResult.BUILDER_COMPLETE,
        EvalAttemptState(planner_attempts=1, builder_attempts=0),
    )

    assert not_yet_run_checker.outcome_kind == EvalWorkflowOutcomeKind.CONTINUE
    assert current_stage_not_counted.outcome_kind == EvalWorkflowOutcomeKind.INVALID
    assert current_stage_not_counted.diagnostic_code == "MF-EVAL-G003"


def test_canonical_snapshot_fixture_and_fingerprint_are_stable() -> None:
    snapshot = compact_eval_workflow_snapshot()
    canonical_bytes = canonical_compact_eval_workflow_bytes()
    fixture_bytes = FIXTURE_PATH.read_bytes()

    fingerprint_body = dict(snapshot)
    fingerprint_body.pop("graph_sha256")
    assert "omitted_stage_ids" in fingerprint_body
    assert canonical_bytes == fixture_bytes
    assert json.loads(canonical_bytes) == snapshot
    assert calculate_compact_eval_workflow_sha256(snapshot) == snapshot["graph_sha256"]
    assert len(snapshot["graph_sha256"]) == 64
    assert "graph_sha256" in snapshot
    assert b"millrace-agents" not in canonical_bytes
    assert b"/mnt/f" not in canonical_bytes
    assert b"F:\\" not in canonical_bytes
    assert b"ideas/" not in canonical_bytes
    assert b"ref-forge" not in canonical_bytes


def test_canonical_snapshot_hash_excludes_graph_sha256_field() -> None:
    snapshot = compact_eval_workflow_snapshot()
    stale = {**snapshot, "graph_sha256": "0" * 64}

    assert calculate_compact_eval_workflow_sha256(stale) == snapshot["graph_sha256"]


def test_eval_workflow_contracts_are_public_exports() -> None:
    assert "resolve_eval_transition" in millforge.__all__
    assert millforge.resolve_eval_transition is resolve_eval_transition
