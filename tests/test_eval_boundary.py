"""Baseline tests for the public compact eval boundary surface."""

from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath
from types import MappingProxyType

import millforge
import pytest
from pydantic import ValidationError

from millforge.eval_boundary import (
    AUTHORITATIVE_COMPACT_EVAL_WORKFLOW_NAMES,
    EVAL_BOUNDARY_ARTIFACT_MODULE_DECISION,
    EVAL_BOUNDARY_ARTIFACT_MODULE_REQUIRED,
    EVAL_BOUNDARY_MODULE_NAME,
    EVAL_CONTEXT_DEFAULT_REDACTION_CATEGORIES,
    EVAL_CONTEXT_FINGERPRINT_KIND,
    EVAL_DENIED_CAPABILITY_IDS,
    EvalBoundaryBaseline,
    EvalCapabilityId,
    EvalClosureOutcomeKind,
    EvalCommandDescriptor,
    EvalCommandEnvironmentPolicy,
    EvalContextArtifactSummary,
    EvalContextTier,
    EvalFixtureFile,
    EvalFixtureManifest,
    EvalFixtureWorkspacePolicy,
    EvalFixtureWorkspaceSnapshot,
    EvalResourceCeiling,
    compact_eval_boundary_baseline,
    compact_eval_boundary_baseline_snapshot,
    build_eval_context_snapshot,
    calculate_eval_context_fingerprint,
    default_eval_capability_envelopes,
    default_eval_stage_context_policy,
    default_eval_stage_resource_ceiling,
    default_eval_trial_resource_ceiling,
    eval_fixture_manifest_from_paths,
    eval_fixture_manifest_sha256,
    eval_fixture_workspace_snapshot,
    validate_eval_closure,
    validate_eval_stage_capability,
    validate_eval_stage_command,
    validate_eval_fixture_path,
)
from millforge.eval_artifacts import (
    EVAL_LOGICAL_06A_ARTIFACT_IDS,
    EVAL_PUBLIC_ARTIFACT_IDS,
    EVAL_RUNTIME_MEASUREMENT_ARTIFACT_IDS,
    EvalAcceptanceCheck,
    EvalAcceptanceChecksArtifact,
    EvalArbiterVerdictArtifact,
    EvalArbiterVerdictValue,
    EvalArtifactId,
    EvalArtifactManifestArtifact,
    EvalArtifactManifestEntry,
    EvalArtifactReference,
    EvalCheckerVerdictArtifact,
    EvalCheckerVerdictValue,
    EvalCommandOutcome,
    EvalContextSnapshotArtifact,
    EvalFixtureManifestArtifact,
    EvalModelUsageArtifact,
    EvalPatchSummaryArtifact,
    EvalPlanArtifact,
    EvalTaskArtifact,
    EvalValidatorResultArtifact,
    EvalValidatorVisibilityRecord,
    EvalTestResultsArtifact,
    EvalWorkspaceDiffArtifact,
    calculate_eval_artifact_manifest_sha256,
    canonical_eval_artifact_layout,
    canonical_eval_artifact_manifest_bytes,
    validate_eval_artifact_record,
)
from millforge.eval_workflow import (
    EvalCandidateDisposition,
    EvalStageId,
    EvalTerminalResult,
    EvalWorkflowOutcomeKind,
    canonical_compact_eval_workflow_bytes,
    compact_eval_workflow_snapshot,
    default_compact_eval_workflow_graph,
)

FIXTURE_WORKSPACE = Path("tests/fixtures/eval_workflow/workspace")


def test_boundary_declares_06a_public_names_as_authoritative() -> None:
    assert AUTHORITATIVE_COMPACT_EVAL_WORKFLOW_NAMES == (
        "EvalStageId",
        "EvalTerminalResult",
        "EvalWorkflowOutcomeKind",
        "EvalCandidateDisposition",
        "default_compact_eval_workflow_graph",
        "compact_eval_workflow_snapshot",
    )

    for public_name in AUTHORITATIVE_COMPACT_EVAL_WORKFLOW_NAMES:
        assert hasattr(millforge, public_name)


def test_boundary_baseline_is_derived_from_accepted_06a_graph() -> None:
    graph = default_compact_eval_workflow_graph()
    workflow_snapshot = compact_eval_workflow_snapshot()
    baseline = compact_eval_boundary_baseline()

    assert isinstance(baseline, EvalBoundaryBaseline)
    assert baseline.module_name == EVAL_BOUNDARY_MODULE_NAME
    assert baseline.graph_id == graph.graph_id
    assert baseline.graph_sha256 == workflow_snapshot["graph_sha256"]
    assert baseline.stage_ids == graph.stage_ids
    assert baseline.terminal_results == tuple(EvalTerminalResult)
    assert baseline.outcome_kinds == tuple(EvalWorkflowOutcomeKind)
    assert baseline.candidate_dispositions == tuple(EvalCandidateDisposition)


def test_boundary_stage_artifacts_match_06a_stage_contracts() -> None:
    graph = default_compact_eval_workflow_graph()
    baseline = compact_eval_boundary_baseline()

    assert tuple(stage.stage_id for stage in baseline.stage_artifacts) == (
        EvalStageId.PLANNER,
        EvalStageId.BUILDER,
        EvalStageId.CHECKER,
        EvalStageId.ARBITER,
    )
    assert tuple(
        (
            stage.stage_id,
            stage.input_artifact_ids,
            stage.output_artifact_ids,
        )
        for stage in baseline.stage_artifacts
    ) == tuple(
        (
            stage.stage_id,
            stage.input_artifact_ids,
            stage.output_artifact_ids,
        )
        for stage in graph.stages
    )


def test_boundary_setup_does_not_broaden_or_rewrite_graph_snapshot() -> None:
    before = canonical_compact_eval_workflow_bytes()
    baseline_snapshot = compact_eval_boundary_baseline_snapshot()
    after = canonical_compact_eval_workflow_bytes()

    assert before == after
    assert baseline_snapshot["stage_ids"] == [
        "eval_planner",
        "eval_builder",
        "eval_checker",
        "eval_arbiter",
    ]
    assert "manager" not in json.dumps(baseline_snapshot, sort_keys=True)
    assert (
        baseline_snapshot["graph_sha256"]
        == compact_eval_workflow_snapshot()["graph_sha256"]
    )


def test_boundary_module_shape_declares_artifact_schema_module() -> None:
    assert EVAL_BOUNDARY_ARTIFACT_MODULE_REQUIRED is True
    assert "eval_artifacts" in EVAL_BOUNDARY_ARTIFACT_MODULE_DECISION
    assert millforge.EvalArtifactId is EvalArtifactId


def test_eval_boundary_contracts_are_public_exports() -> None:
    assert "EvalBoundaryBaseline" in millforge.__all__
    assert "compact_eval_boundary_baseline" in millforge.__all__
    assert "EvalCapabilityId" in millforge.__all__
    assert "EvalCommandDescriptor" in millforge.__all__
    assert "EvalFixtureManifest" in millforge.__all__
    assert "EvalFixtureManifestArtifact" in millforge.__all__
    assert "EvalFixtureWorkspaceSnapshot" in millforge.__all__
    assert "eval_fixture_manifest_sha256" in millforge.__all__
    assert "validate_eval_stage_capability" in millforge.__all__
    assert "validate_eval_stage_command" in millforge.__all__
    assert "validate_eval_fixture_path" in millforge.__all__
    assert "EvalContextTier" in millforge.__all__
    assert "EvalContextArtifactSummary" in millforge.__all__
    assert "EvalContextRedaction" in millforge.__all__
    assert "EvalContextSnapshot" in millforge.__all__
    assert "EvalStageContextPolicy" in millforge.__all__
    assert "EvalResourceCeiling" in millforge.__all__
    assert "build_eval_context_snapshot" in millforge.__all__
    assert "calculate_eval_context_fingerprint" in millforge.__all__
    assert "default_eval_stage_context_policy" in millforge.__all__
    assert "default_eval_stage_resource_ceiling" in millforge.__all__
    assert "default_eval_trial_resource_ceiling" in millforge.__all__
    assert "EvalArtifactId" in millforge.__all__
    assert "EvalArtifactManifestArtifact" in millforge.__all__
    assert "EvalValidatorVisibilityRecord" in millforge.__all__
    assert "canonical_eval_artifact_layout" in millforge.__all__
    assert "validate_eval_artifact_record" in millforge.__all__
    assert millforge.EvalBoundaryBaseline is EvalBoundaryBaseline
    assert millforge.compact_eval_boundary_baseline is compact_eval_boundary_baseline
    assert millforge.EvalCapabilityId is EvalCapabilityId
    assert millforge.EvalCommandDescriptor is EvalCommandDescriptor
    assert millforge.EvalFixtureManifest is EvalFixtureManifest
    assert millforge.EvalFixtureManifestArtifact is EvalFixtureManifestArtifact
    assert millforge.eval_fixture_manifest_sha256 is eval_fixture_manifest_sha256
    assert millforge.eval_fixture_workspace_snapshot is eval_fixture_workspace_snapshot
    assert millforge.validate_eval_stage_capability is validate_eval_stage_capability
    assert millforge.validate_eval_stage_command is validate_eval_stage_command
    assert millforge.validate_eval_fixture_path is validate_eval_fixture_path
    assert millforge.EvalContextTier is EvalContextTier
    assert millforge.EvalContextArtifactSummary is EvalContextArtifactSummary
    assert millforge.EvalResourceCeiling is EvalResourceCeiling
    assert millforge.build_eval_context_snapshot is build_eval_context_snapshot
    assert (
        millforge.calculate_eval_context_fingerprint
        is calculate_eval_context_fingerprint
    )
    assert (
        millforge.default_eval_stage_context_policy is default_eval_stage_context_policy
    )
    assert millforge.EvalArtifactManifestArtifact is EvalArtifactManifestArtifact
    assert millforge.EvalValidatorVisibilityRecord is EvalValidatorVisibilityRecord
    assert millforge.canonical_eval_artifact_layout is canonical_eval_artifact_layout
    assert millforge.validate_eval_artifact_record is validate_eval_artifact_record


def test_eval_artifact_ids_cover_06a_logical_and_runtime_artifacts() -> None:
    assert EVAL_LOGICAL_06A_ARTIFACT_IDS == (
        "task",
        "fixture_manifest",
        "acceptance_checks",
        "plan",
        "workspace_diff",
        "patch_summary",
        "test_results",
        "checker_verdict",
        "arbiter_verdict",
    )
    assert EVAL_RUNTIME_MEASUREMENT_ARTIFACT_IDS == (
        "stage_result",
        "event_log",
        "resource_usage",
        "model_usage",
        "validator_result",
        "context_snapshot",
        "artifact_manifest",
    )
    assert (
        tuple(artifact.value for artifact in EvalArtifactId) == EVAL_PUBLIC_ARTIFACT_IDS
    )


def test_eval_artifact_layout_is_canonical_path_free_and_complete() -> None:
    layout = canonical_eval_artifact_layout()

    assert set(layout) == set(EvalArtifactId)
    assert {
        artifact.value: entry.layout_path for artifact, entry in layout.items()
    } == {
        "task": "trial/input/task.json",
        "fixture_manifest": "trial/input/fixture_manifest.json",
        "acceptance_checks": "trial/input/acceptance_checks.json",
        "plan": "trial/planning/plan.json",
        "workspace_diff": "trial/execution/workspace_diff.json",
        "patch_summary": "trial/execution/patch_summary.json",
        "test_results": "trial/execution/test_results.json",
        "checker_verdict": "trial/checking/checker_verdict.json",
        "arbiter_verdict": "trial/closure/arbiter_verdict.json",
        "stage_result": "trial/runtime/stage_result.json",
        "event_log": "trial/runtime/event_log.jsonl",
        "resource_usage": "trial/runtime/resource_usage.json",
        "model_usage": "trial/runtime/model_usage.json",
        "validator_result": "trial/runtime/validator_result.visible.json",
        "context_snapshot": "trial/runtime/context_snapshot.<stage>.json",
        "artifact_manifest": "trial/runtime/artifact_manifest.json",
    }
    rendered = json.dumps(
        {
            artifact.value: entry.model_dump(mode="json")
            for artifact, entry in layout.items()
        },
        sort_keys=True,
    )
    for denied in ("F:\\", "/mnt/f", "millrace-agents", "ideas/", "/home/"):
        assert denied not in rendered


def _artifact_reference(artifact_id: EvalArtifactId) -> EvalArtifactReference:
    return EvalArtifactReference(artifact_id=artifact_id, summary="public evidence")


def _acceptance_check() -> EvalAcceptanceCheck:
    return EvalAcceptanceCheck(
        check_id="check-public-tests",
        check_kind="pytest",
        descriptor="python -m pytest tests/test_eval_boundary.py",
        expected_success="exit code 0",
        public_rationale="proves public boundary behavior",
    )


def _fixture_manifest_artifact(
    manifest: EvalFixtureManifest,
) -> EvalFixtureManifestArtifact:
    return EvalFixtureManifestArtifact(
        trial_id="trial-001",
        created_by="fixture_author",
        summary="public fixture manifest",
        fixture_manifest=manifest,
    )


def test_eval_artifact_schemas_reject_missing_unknown_and_hidden_material() -> None:
    valid_plan = EvalPlanArtifact(
        trial_id="trial-001",
        created_by="eval_planner",
        summary="public implementation plan",
        references=(_artifact_reference(EvalArtifactId.TASK),),
        implementation_steps=("inspect source", "edit artifacts"),
        expected_files_to_inspect=("src/millforge/eval_boundary.py",),
        expected_files_to_mutate=("src/millforge/eval_artifacts.py",),
        checks_to_run=("python -m pytest tests/test_eval_boundary.py",),
        no_hidden_checks_known=True,
    )
    assert valid_plan.artifact_id == EvalArtifactId.PLAN

    with pytest.raises(ValidationError):
        EvalPlanArtifact(  # type: ignore[call-arg]
            trial_id="trial-001",
            created_by="eval_planner",
            summary="public implementation plan",
            implementation_steps=("inspect source",),
            expected_files_to_inspect=(),
            expected_files_to_mutate=(),
            checks_to_run=(),
            no_hidden_checks_known=True,
            unexpected=True,
        )
    with pytest.raises(ValidationError):
        EvalPlanArtifact(
            trial_id="trial-001",
            created_by="eval_planner",
            summary="public implementation plan",
            implementation_steps=("inspect hidden checks",),
            expected_files_to_inspect=(),
            expected_files_to_mutate=(),
            checks_to_run=(),
            no_hidden_checks_known=True,
        )
    with pytest.raises(ValidationError):
        EvalPlanArtifact(  # type: ignore[call-arg]
            created_by="eval_planner",
            summary="public implementation plan",
            implementation_steps=("inspect source",),
            expected_files_to_inspect=(),
            expected_files_to_mutate=(),
            checks_to_run=(),
            no_hidden_checks_known=True,
        )


@pytest.mark.parametrize(
    "host_path",
    (
        "C:/Users/alice/project/src/app.py",
        "C:\\Users\\alice\\project\\src\\app.py",
        "/tmp/project/src/app.py",
        "/Users/alice/project/src/app.py",
        "/home/alice/project/src/app.py",
        "~/project/src/app.py",
        "relative text before /tmp/project/src/app.py",
    ),
)
def test_eval_public_artifact_records_reject_host_specific_paths(
    host_path: str,
) -> None:
    with pytest.raises(ValidationError, match="host paths|private material"):
        EvalPlanArtifact(
            trial_id="trial-001",
            created_by="eval_planner",
            summary="public implementation plan",
            references=(_artifact_reference(EvalArtifactId.TASK),),
            implementation_steps=("inspect source",),
            expected_files_to_inspect=(host_path,),
            expected_files_to_mutate=("src/millforge/eval_artifacts.py",),
            checks_to_run=("python -m pytest tests/test_eval_boundary.py",),
            no_hidden_checks_known=True,
        )

    valid_plan = EvalPlanArtifact(
        trial_id="trial-001",
        created_by="eval_planner",
        summary="public implementation plan",
        references=(_artifact_reference(EvalArtifactId.TASK),),
        implementation_steps=("inspect source",),
        expected_files_to_inspect=("src/millforge/eval_artifacts.py",),
        expected_files_to_mutate=("tests/test_eval_boundary.py",),
        checks_to_run=("python -m pytest tests/test_eval_boundary.py",),
        no_hidden_checks_known=True,
    )
    assert valid_plan.expected_files_to_inspect == ("src/millforge/eval_artifacts.py",)


def test_eval_fixture_manifest_validates_through_public_artifact_registry() -> None:
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        FIXTURE_WORKSPACE,
        ("tests/test_app.py", "src/app/main.py"),
        task_id="task-06b-r1-01",
        visible_acceptance_checks=("check-public-tests",),
        hidden_check_ids=("opaque-evaluator-001",),
    )
    artifact = _fixture_manifest_artifact(manifest)
    validated = validate_eval_artifact_record(
        "fixture_manifest", artifact.model_dump(mode="json")
    )

    assert isinstance(validated, EvalFixtureManifestArtifact)
    assert validated.artifact_id == EvalArtifactId.FIXTURE_MANIFEST
    assert validated.stage_id is None
    assert validated.trial_id == "trial-001"
    assert validated.created_at == "1970-01-01T00:00:00Z"
    assert validated.fixture_manifest.fixture_id == "compact_fixture"
    assert validated.fixture_manifest.schema_version == 1
    assert validated.fixture_manifest.task_id == "task-06b-r1-01"
    assert validated.fixture_manifest.visible_acceptance_checks == (
        "check-public-tests",
    )
    assert validated.fixture_manifest.hidden_check_ids == ("opaque-evaluator-001",)
    assert tuple(file.path for file in validated.fixture_manifest.files) == (
        "src/app/main.py",
        "tests/test_app.py",
    )
    fixture_layout = canonical_eval_artifact_layout()[EvalArtifactId.FIXTURE_MANIFEST]
    assert fixture_layout.layout_path == "trial/input/fixture_manifest.json"
    assert (
        canonical_eval_artifact_layout()[EvalArtifactId.FIXTURE_MANIFEST].schema_id
        == "eval_fixture_manifest_artifact_v1"
    )
    assert (
        validate_eval_artifact_record(
            EvalArtifactId.FIXTURE_MANIFEST, artifact.model_dump(mode="json")
        )
        == artifact
    )

    missing_metadata_record = artifact.model_dump(mode="json")
    del missing_metadata_record["trial_id"]
    with pytest.raises(ValidationError):
        validate_eval_artifact_record("fixture_manifest", missing_metadata_record)

    unknown_field_record = artifact.model_dump(mode="json")
    unknown_field_record["unexpected"] = "not declared"
    with pytest.raises(ValidationError):
        validate_eval_artifact_record("fixture_manifest", unknown_field_record)

    required_manifest_fields = (
        "schema_version",
        "fixture_id",
        "fixture_revision",
        "task_id",
        "source_root_label",
        "allowed_read_paths",
        "allowed_write_paths",
        "allowed_command_roots",
        "visible_acceptance_checks",
        "hidden_check_ids",
        "expected_mutation_paths",
        "files",
    )
    for field_name in required_manifest_fields:
        missing_required_record = artifact.model_dump(mode="json")
        del missing_required_record["fixture_manifest"][field_name]
        with pytest.raises(ValidationError):
            validate_eval_artifact_record("fixture_manifest", missing_required_record)

    missing_file_field_record = artifact.model_dump(mode="json")
    del missing_file_field_record["fixture_manifest"]["files"][0]["role"]
    with pytest.raises(ValidationError):
        validate_eval_artifact_record("fixture_manifest", missing_file_field_record)


def test_eval_artifact_visible_validator_rejects_scorer_only_material() -> None:
    checks = EvalAcceptanceChecksArtifact(
        trial_id="trial-001",
        created_by="fixture_author",
        summary="visible acceptance checks",
        visible_acceptance_checks=(_acceptance_check(),),
    )
    assert checks.visible_acceptance_checks[0].check_id == "check-public-tests"

    with pytest.raises(ValidationError):
        EvalValidatorResultArtifact(
            trial_id="trial-001",
            stage_id=EvalStageId.CHECKER,
            created_by="eval_checker",
            summary="visible validator result",
            visible_check_results=(
                EvalCommandOutcome(
                    command_id="pytest",
                    exit_code=0,
                    summary="hidden score was high",
                ),
            ),
            public_diagnostics=(),
        )


def test_eval_validator_visibility_records_exclude_scorer_only_material() -> None:
    visibility = EvalValidatorVisibilityRecord(
        visible_acceptance_check_ids=("check-public-tests",),
        scorer_only_opaque_check_ids=("opaque-evaluator-001",),
    )

    rendered = json.dumps(visibility.model_dump(mode="json"), sort_keys=True)
    assert "validator_result.visible.json" in rendered
    assert "opaque-evaluator-001" in rendered
    assert "expected output" not in rendered
    assert "scoring rubric" not in rendered
    assert "hidden score" not in rendered

    with pytest.raises(ValidationError, match="must not overlap"):
        EvalValidatorVisibilityRecord(
            visible_acceptance_check_ids=("check-public-tests",),
            scorer_only_opaque_check_ids=("check-public-tests",),
        )
    with pytest.raises(ValidationError, match="filename is fixed"):
        EvalValidatorVisibilityRecord(
            visible_acceptance_check_ids=("check-public-tests",),
            visible_validator_filename="validator_result.json",
        )
    with pytest.raises(ValidationError, match="structurally excluded"):
        EvalValidatorVisibilityRecord(
            visible_acceptance_check_ids=("check-public-tests",),
            scorer_only_final_scores_excluded=False,
        )


def test_eval_artifact_manifest_is_deterministic_and_rejects_undeclared_ids() -> None:
    layout = canonical_eval_artifact_layout()
    plan_layout = layout[EvalArtifactId.PLAN]
    task_layout = layout[EvalArtifactId.TASK]
    manifest = EvalArtifactManifestArtifact(
        trial_id="trial-001",
        created_by="eval_runtime",
        summary="public artifact manifest",
        entries=(
            EvalArtifactManifestEntry(
                artifact_id=EvalArtifactId.PLAN,
                layout_path=plan_layout.layout_path,
                media_type=plan_layout.media_type,
                schema_id=plan_layout.schema_id,
                byte_size=2,
                sha256="b" * 64,
                producer="eval_planner",
            ),
            EvalArtifactManifestEntry(
                artifact_id=EvalArtifactId.TASK,
                layout_path=task_layout.layout_path,
                media_type=task_layout.media_type,
                schema_id=task_layout.schema_id,
                byte_size=2,
                sha256="a" * 64,
                producer="fixture_author",
            ),
        ),
    )

    assert tuple(entry.artifact_id for entry in manifest.entries) == (
        EvalArtifactId.TASK,
        EvalArtifactId.PLAN,
    )
    assert canonical_eval_artifact_manifest_bytes(manifest) == (
        canonical_eval_artifact_manifest_bytes(manifest)
    )
    assert len(calculate_eval_artifact_manifest_sha256(manifest)) == 64
    rendered = canonical_eval_artifact_manifest_bytes(manifest).decode("ascii")
    assert "/mnt/f" not in rendered
    assert "millrace-agents" not in rendered

    with pytest.raises(ValueError):
        validate_eval_artifact_record("undeclared_artifact", {})
    with pytest.raises(ValidationError):
        EvalArtifactManifestArtifact(
            trial_id="trial-001",
            created_by="eval_runtime",
            summary="public artifact manifest",
            entries=(
                EvalArtifactManifestEntry(
                    artifact_id=EvalArtifactId.ARTIFACT_MANIFEST,
                    layout_path=layout[EvalArtifactId.ARTIFACT_MANIFEST].layout_path,
                    media_type=layout[EvalArtifactId.ARTIFACT_MANIFEST].media_type,
                    schema_id=layout[EvalArtifactId.ARTIFACT_MANIFEST].schema_id,
                    byte_size=2,
                    sha256="c" * 64,
                    producer="eval_runtime",
                ),
            ),
        )


def test_eval_model_usage_schema_is_bounded_and_deterministic() -> None:
    usage = EvalModelUsageArtifact(
        trial_id="trial-001",
        stage_id=EvalStageId.BUILDER,
        created_by="eval_runtime",
        summary="deterministic fake model usage",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        reasoning_tokens=0,
        cached_tokens=0,
        estimated_cost_micros=0,
        wall_clock_seconds=0,
        retry_count=0,
    )

    assert usage.total_tokens == 15
    with pytest.raises(ValidationError):
        EvalModelUsageArtifact(
            trial_id="trial-001",
            stage_id=EvalStageId.BUILDER,
            created_by="eval_runtime",
            summary="deterministic fake model usage",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=99,
            estimated_cost_micros=0,
            wall_clock_seconds=0,
            retry_count=0,
        )


def test_eval_context_snapshot_fingerprint_and_redaction_are_deterministic() -> None:
    policy = default_eval_stage_context_policy(
        EvalStageId.BUILDER,
        context_tier=EvalContextTier.COMPACT,
    )
    summary = EvalContextArtifactSummary(
        artifact_id="plan",
        summary="public implementation plan summary",
    )
    snapshot = build_eval_context_snapshot(
        trial_id="trial-001",
        policy=policy,
        required_artifact_summaries=(summary,),
        visible_acceptance_check_ids=("check-public-tests",),
    )

    assert snapshot.stage_id == EvalStageId.BUILDER
    assert snapshot.context_tier == EvalContextTier.COMPACT
    assert snapshot.fingerprint_kind == EVAL_CONTEXT_FINGERPRINT_KIND
    assert snapshot.fingerprint == calculate_eval_context_fingerprint(snapshot)
    assert snapshot.redaction.categories == EVAL_CONTEXT_DEFAULT_REDACTION_CATEGORIES
    assert snapshot.current_stage_contract["stage_id"] == "eval_builder"
    assert snapshot.allowed_capabilities == (
        "artifact.read",
        "artifact.write",
        "evidence.emit",
        "runner.invoke",
        "workspace.read",
        "workspace.write",
        "shell.run",
    )
    rendered = snapshot.model_dump_json()
    for denied in (
        "/mnt/f",
        "millrace-agents",
        "ideas/",
        "ref-forge/",
        "API_KEY",
        "hidden check",
        "expected output",
        "scoring rubric",
        "hidden score",
    ):
        assert denied not in rendered

    repeat = build_eval_context_snapshot(
        trial_id="trial-001",
        policy=policy,
        required_artifact_summaries=(summary,),
        visible_acceptance_check_ids=("check-public-tests",),
    )
    assert repeat.fingerprint == snapshot.fingerprint

    changed = build_eval_context_snapshot(
        trial_id="trial-001",
        policy=policy,
        required_artifact_summaries=(summary,),
        visible_acceptance_check_ids=("check-public-tests", "check-public-style"),
    )
    assert changed.fingerprint != snapshot.fingerprint

    with pytest.raises(ValidationError, match="private material|host paths"):
        EvalContextArtifactSummary(
            artifact_id="plan",
            summary="inspect /mnt/f private runtime details",
        )
    with pytest.raises(ValidationError, match="private material"):
        EvalContextArtifactSummary(
            artifact_id="plan",
            summary="inspect hidden check definition",
        )
    for leaked_summary in (
        "inspect Git history details",
        "inspect private conversations",
        "inspect unrelated repository outlines",
    ):
        with pytest.raises(ValidationError, match="private material"):
            EvalContextArtifactSummary(
                artifact_id="plan",
                summary=leaked_summary,
            )


def test_eval_context_snapshot_artifact_matches_compact_snapshot_contract() -> None:
    policy = default_eval_stage_context_policy(EvalStageId.CHECKER)
    snapshot = build_eval_context_snapshot(
        trial_id="trial-001",
        policy=policy,
        required_artifact_summaries=(
            EvalContextArtifactSummary(
                artifact_id="test_results",
                summary="public test result summary",
            ),
        ),
        visible_acceptance_check_ids=("check-public-tests",),
    )
    artifact = EvalContextSnapshotArtifact(
        trial_id=snapshot.trial_id,
        stage_id=snapshot.stage_id,
        created_by="eval_runtime",
        summary="compact public context snapshot",
        context_tier=snapshot.context_tier,
        allowed_capabilities=snapshot.allowed_capabilities,
        allowed_paths=snapshot.allowed_paths,
        current_stage_contract=snapshot.current_stage_contract,
        required_artifact_summaries=(
            EvalArtifactReference(
                artifact_id=EvalArtifactId.TEST_RESULTS,
                summary="public test result summary",
            ),
        ),
        visible_acceptance_check_ids=snapshot.visible_acceptance_check_ids,
        redaction=snapshot.redaction,
        redaction_summary=snapshot.redaction.summary,
        byte_budget=snapshot.byte_budget,
        token_budget=snapshot.token_budget,
        resource_ceiling=snapshot.resource_ceiling,
        fingerprint=snapshot.fingerprint,
    )

    validated = validate_eval_artifact_record(
        "context_snapshot", artifact.model_dump(mode="json")
    )
    assert isinstance(validated, EvalContextSnapshotArtifact)
    assert validated.resource_ceiling.stage_id == EvalStageId.CHECKER

    with pytest.raises(ValidationError, match="sha256"):
        EvalContextSnapshotArtifact(
            **{**artifact.model_dump(mode="json"), "fingerprint": "not-a-sha"}
        )
    with pytest.raises(ValidationError, match="must not expose"):
        EvalContextSnapshotArtifact(
            **{
                **artifact.model_dump(mode="json"),
                "redaction_summary": "omits hidden check details",
            }
        )


def test_eval_resource_ceilings_are_positive_bounded_and_scope_checked() -> None:
    trial_ceiling = default_eval_trial_resource_ceiling()
    stage_ceiling = default_eval_stage_resource_ceiling(EvalStageId.BUILDER)

    assert trial_ceiling.scope == "trial"
    assert trial_ceiling.stage_id is None
    assert stage_ceiling.scope == "stage"
    assert stage_ceiling.stage_id == EvalStageId.BUILDER
    for ceiling in (trial_ceiling, stage_ceiling):
        values = ceiling.model_dump(mode="json")
        assert values["prompt_tokens"] > 0
        assert values["completion_tokens"] > 0
        assert values["model_calls"] > 0
        assert values["wall_clock_seconds"] > 0
        assert values["shell_commands"] > 0
        assert values["shell_command_seconds"] > 0
        assert values["writable_bytes"] > 0
        assert values["artifact_bytes"] > 0
    for stage_id in EvalStageId:
        assert default_eval_stage_resource_ceiling(stage_id).shell_commands > 0

    with pytest.raises(ValidationError):
        EvalResourceCeiling(
            scope="trial",
            prompt_tokens=0,
            completion_tokens=1,
            model_calls=1,
            wall_clock_seconds=1,
            shell_commands=1,
            shell_command_seconds=1,
            writable_bytes=1,
            artifact_bytes=1,
        )
    with pytest.raises(ValidationError):
        EvalResourceCeiling(
            scope="trial",
            prompt_tokens=1,
            completion_tokens=1,
            model_calls=1,
            wall_clock_seconds=1,
            shell_commands=0,
            shell_command_seconds=1,
            writable_bytes=1,
            artifact_bytes=1,
        )
    with pytest.raises(ValidationError, match="stage resource ceilings"):
        EvalResourceCeiling(
            scope="stage",
            prompt_tokens=1,
            completion_tokens=1,
            model_calls=1,
            wall_clock_seconds=1,
            shell_commands=1,
            shell_command_seconds=1,
            writable_bytes=1,
            artifact_bytes=1,
        )


def test_eval_capability_ids_are_closed_and_exact() -> None:
    assert tuple(capability.value for capability in EvalCapabilityId) == (
        "artifact.read",
        "artifact.write",
        "evidence.emit",
        "runner.invoke",
        "workspace.read",
        "workspace.write",
        "shell.run",
        "network.access",
        "package.install",
        "git.mutate",
        "runtime.control",
    )


def test_eval_stage_capability_envelopes_are_exact_and_immutable() -> None:
    envelopes = default_eval_capability_envelopes()

    assert isinstance(envelopes, MappingProxyType)
    assert {
        stage_id: tuple(capability.value for capability in envelope.capability_ids)
        for stage_id, envelope in envelopes.items()
    } == {
        EvalStageId.PLANNER: (
            "artifact.read",
            "artifact.write",
            "evidence.emit",
            "runner.invoke",
        ),
        EvalStageId.BUILDER: (
            "artifact.read",
            "artifact.write",
            "evidence.emit",
            "runner.invoke",
            "workspace.read",
            "workspace.write",
            "shell.run",
        ),
        EvalStageId.CHECKER: (
            "workspace.read",
            "artifact.read",
            "artifact.write",
            "shell.run",
            "evidence.emit",
            "runner.invoke",
        ),
        EvalStageId.ARBITER: (
            "workspace.read",
            "artifact.read",
            "artifact.write",
            "evidence.emit",
            "runner.invoke",
        ),
    }

    with pytest.raises(TypeError):
        envelopes[EvalStageId.PLANNER] = envelopes[EvalStageId.BUILDER]  # type: ignore[index]
    with pytest.raises(ValidationError):
        envelopes[EvalStageId.BUILDER].capability_ids = ()  # type: ignore[misc]


def test_eval_stage_capability_validation_returns_stable_denials() -> None:
    allowed = validate_eval_stage_capability(
        EvalStageId.BUILDER, EvalCapabilityId.WORKSPACE_WRITE
    )
    assert allowed.allowed is True
    assert allowed.rule_id == "eval.capability.allowed"
    assert allowed.diagnostic_code is None

    unknown = validate_eval_stage_capability(EvalStageId.BUILDER, "database.drop")
    assert unknown.allowed is False
    assert unknown.rule_id == "eval.capability.unknown_capability"
    assert unknown.diagnostic_code == "MF-EVAL-C002"
    assert unknown.diagnostic_summary == "unknown compact eval capability id"

    planner_shell = validate_eval_stage_capability(
        EvalStageId.PLANNER, EvalCapabilityId.SHELL_RUN
    )
    assert planner_shell.allowed is False
    assert planner_shell.rule_id == "eval.capability.denied.eval_planner"
    assert planner_shell.diagnostic_code == "MF-EVAL-C004"

    for stage_id in EvalStageId:
        for capability in EVAL_DENIED_CAPABILITY_IDS:
            denied = validate_eval_stage_capability(stage_id, capability)
            assert denied.allowed is False
            assert denied.rule_id == "eval.capability.denied_dangerous_all_stages"
            assert denied.diagnostic_code == "MF-EVAL-C003"


def _command_descriptor(
    *,
    write_roots: tuple[str, ...] = (),
    command_id: str = "pytest_eval_boundary",
    argv: tuple[str, ...] = ("python", "-m", "pytest", "tests/test_eval_boundary.py"),
) -> EvalCommandDescriptor:
    return EvalCommandDescriptor(
        command_id=command_id,
        argv=argv,
        relative_working_directory=".",
        admitted_read_roots=("src", "tests"),
        admitted_write_roots=write_roots,
        timeout_seconds=120,
        environment_policy=EvalCommandEnvironmentPolicy(
            variables={"PYTHONHASHSEED": "0"}
        ),
        expected_output_artifact_ids=("test_results",),
    )


def _constructed_command_descriptor(argv: tuple[str, ...]) -> EvalCommandDescriptor:
    return EvalCommandDescriptor.model_construct(
        command_id="unsafe_wrapper_probe",
        argv=argv,
        relative_working_directory=".",
        admitted_read_roots=("src", "tests"),
        admitted_write_roots=("src/millforge",),
        timeout_seconds=120,
        environment_policy=EvalCommandEnvironmentPolicy(
            variables={"PYTHONHASHSEED": "0"}
        ),
        expected_output_artifact_ids=("test_results",),
    )


def test_eval_command_descriptor_rejects_unsafe_command_shapes() -> None:
    with pytest.raises(ValidationError, match="shell interpolation"):
        _command_descriptor(argv=("python -m pytest tests",))
    with pytest.raises(ValidationError, match="package manager"):
        _command_descriptor(argv=("pip", "install", "requests"))
    with pytest.raises(ValidationError, match="network commands"):
        _command_descriptor(argv=("curl", "https://example.com"))
    with pytest.raises(ValidationError, match="git commands"):
        _command_descriptor(argv=("git", "commit", "-am", "change"))
    with pytest.raises(ValidationError, match="runtime control"):
        _command_descriptor(argv=("millrace", "daemon", "start"))
    with pytest.raises(ValidationError, match="inherit ambient environment"):
        EvalCommandEnvironmentPolicy(inherit_environment=True)
    with pytest.raises(ValidationError, match="secrets"):
        EvalCommandEnvironmentPolicy(variables={"OPENAI_API_KEY": "redacted"})
    with pytest.raises(ValidationError, match="relative POSIX"):
        EvalCommandDescriptor(
            command_id="bad_root",
            argv=("python", "-m", "pytest"),
            relative_working_directory=".",
            admitted_read_roots=("../src",),
            timeout_seconds=120,
            expected_output_artifact_ids=("test_results",),
        )


def test_eval_command_descriptor_rejects_wrapper_command_escapes() -> None:
    unsafe_wrappers: tuple[tuple[tuple[str, ...], str], ...] = (
        (("python", "-m", "pip", "install", "requests"), "package manager"),
        (("python", "-m", "uv", "pip", "install", "requests"), "package manager"),
        (("bash", "-lc", "pip install requests"), "shell wrapper"),
        (("sh", "-c", "git commit -am x"), "shell wrapper"),
        (("zsh", "-c", "curl https://example.com"), "shell wrapper"),
        (("bash", "-lc", "systemctl restart millrace"), "shell wrapper"),
    )

    for argv, expected_message in unsafe_wrappers:
        with pytest.raises(ValidationError, match=expected_message):
            _command_descriptor(argv=argv)


def test_eval_command_admission_denies_constructed_wrapper_escapes() -> None:
    unsafe_wrappers: tuple[tuple[str, ...], ...] = (
        ("python", "-m", "pip", "install", "requests"),
        ("python", "-m", "uv", "pip", "install", "requests"),
        ("bash", "-lc", "pip install requests"),
        ("sh", "-c", "git commit -am x"),
    )

    for argv in unsafe_wrappers:
        denial = validate_eval_stage_command(
            EvalStageId.BUILDER, _constructed_command_descriptor(argv)
        )
        assert denial.allowed is False
        assert denial.rule_id == "eval.command.descriptor_unsafe"
        assert denial.diagnostic_code == "MF-EVAL-D005"


def test_eval_command_admission_matches_stage_boundaries() -> None:
    read_only_descriptor = _command_descriptor()
    builder_write_descriptor = _command_descriptor(write_roots=("src/millforge",))
    checker_scratch_descriptor = _command_descriptor(write_roots=(".eval-scratch",))
    checker_mutating_descriptor = _command_descriptor(write_roots=("src",))
    builder_forbidden_descriptor = _command_descriptor(write_roots=("millrace-agents",))

    assert validate_eval_stage_command(
        EvalStageId.BUILDER, builder_write_descriptor
    ).allowed
    assert validate_eval_stage_command(
        EvalStageId.CHECKER, read_only_descriptor
    ).allowed
    assert validate_eval_stage_command(
        EvalStageId.CHECKER, checker_scratch_descriptor
    ).allowed

    planner_denial = validate_eval_stage_command(
        EvalStageId.PLANNER, read_only_descriptor
    )
    assert planner_denial.allowed is False
    assert planner_denial.rule_id == "eval.command.stage_has_no_shell"
    assert planner_denial.diagnostic_code == "MF-EVAL-D002"

    arbiter_denial = validate_eval_stage_command(
        EvalStageId.ARBITER, read_only_descriptor
    )
    assert arbiter_denial.allowed is False
    assert arbiter_denial.rule_id == "eval.command.stage_has_no_shell"

    checker_denial = validate_eval_stage_command(
        EvalStageId.CHECKER, checker_mutating_descriptor
    )
    assert checker_denial.allowed is False
    assert checker_denial.rule_id == "eval.command.checker_write_denied"
    assert checker_denial.diagnostic_code == "MF-EVAL-D003"

    builder_denial = validate_eval_stage_command(
        EvalStageId.BUILDER, builder_forbidden_descriptor
    )
    assert builder_denial.allowed is False
    assert builder_denial.rule_id == "eval.command.builder_write_root_denied"
    assert builder_denial.diagnostic_code == "MF-EVAL-D004"


def test_eval_fixture_manifest_hashes_declared_files_deterministically() -> None:
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        FIXTURE_WORKSPACE,
        ("tests/test_app.py", "src/app/main.py"),
        task_id="task-06b-r1-01",
        allowed_write_paths=("src",),
        allowed_command_roots=("tests",),
        expected_mutation_paths=("src/app/main.py",),
        visible_acceptance_checks=("check-public-tests",),
        hidden_check_ids=("opaque-evaluator-001",),
    )

    assert isinstance(manifest, EvalFixtureManifest)
    assert manifest.schema_version == 1
    assert manifest.fixture_revision == "fixture-revision-1"
    assert manifest.task_id == "task-06b-r1-01"
    assert manifest.source_root_label == "fixture_workspace"
    assert manifest.allowed_read_paths == ("src/app/main.py", "tests/test_app.py")
    assert manifest.allowed_write_paths == ("src",)
    assert manifest.allowed_command_roots == ("tests",)
    assert manifest.visible_acceptance_checks == ("check-public-tests",)
    assert manifest.hidden_check_ids == ("opaque-evaluator-001",)
    assert manifest.expected_mutation_paths == ("src/app/main.py",)
    assert tuple(file.path for file in manifest.files) == (
        "src/app/main.py",
        "tests/test_app.py",
    )
    assert all(isinstance(file, EvalFixtureFile) for file in manifest.files)
    assert [file.size_bytes for file in manifest.files] == [35, 87]
    assert manifest.model_dump(mode="json")["files"] == [
        {
            "path": "src/app/main.py",
            "sha256": "7ca5abf65adee67782e5c831d9ee108b323c5a9d9fda11f97a9910b18f6b1bdc",
            "size_bytes": 35,
            "role": "source",
            "model_readable": True,
            "builder_mutable": True,
        },
        {
            "path": "tests/test_app.py",
            "sha256": "061740743656882b312ae9fe0922c2909eaf927fffa046b2e74edb3a016702e0",
            "size_bytes": 87,
            "role": "test",
            "model_readable": True,
            "builder_mutable": False,
        },
    ]
    assert manifest.model_dump(mode="json") == {
        "schema_version": 1,
        "fixture_id": "compact_fixture",
        "fixture_revision": "fixture-revision-1",
        "task_id": "task-06b-r1-01",
        "source_root_label": "fixture_workspace",
        "allowed_read_paths": ["src/app/main.py", "tests/test_app.py"],
        "allowed_write_paths": ["src"],
        "allowed_command_roots": ["tests"],
        "visible_acceptance_checks": ["check-public-tests"],
        "hidden_check_ids": ["opaque-evaluator-001"],
        "expected_mutation_paths": ["src/app/main.py"],
        "files": [
            {
                "path": "src/app/main.py",
                "sha256": "7ca5abf65adee67782e5c831d9ee108b323c5a9d9fda11f97a9910b18f6b1bdc",
                "size_bytes": 35,
                "role": "source",
                "model_readable": True,
                "builder_mutable": True,
            },
            {
                "path": "tests/test_app.py",
                "sha256": "061740743656882b312ae9fe0922c2909eaf927fffa046b2e74edb3a016702e0",
                "size_bytes": 87,
                "role": "test",
                "model_readable": True,
                "builder_mutable": False,
            },
        ],
        "workspace_policy": {
            "source_fixture_root_read_only": True,
            "workspace_isolation": "fresh_copy",
            "stage_write_roots": {
                "eval_planner": [],
                "eval_builder": ["src", "tests", "README.md", "ROADMAP.md"],
                "eval_checker": [],
                "eval_arbiter": [],
            },
            "ignored_generated_roots": [
                ".eval-scratch",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "__pycache__",
            ],
            "ignored_generated_suffixes": [
                ".coverage",
                ".coverage.json",
                ".log",
                ".pyc",
                ".pyo",
            ],
        },
    }

    repeated = eval_fixture_manifest_from_paths(
        "compact_fixture",
        FIXTURE_WORKSPACE,
        ("src/app/main.py", "tests/test_app.py"),
        task_id="task-06b-r1-01",
        allowed_write_paths=("src",),
        allowed_command_roots=("tests",),
        expected_mutation_paths=("src/app/main.py",),
        visible_acceptance_checks=("check-public-tests",),
        hidden_check_ids=("opaque-evaluator-001",),
    )
    assert eval_fixture_manifest_sha256(repeated) == eval_fixture_manifest_sha256(
        manifest
    )


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("allowed_read_paths", ("/tmp/source.py",)),
        ("allowed_write_paths", ("../src",)),
        ("allowed_command_roots", ("C:/fixture/tests",)),
        ("expected_mutation_paths", ("src/../app.py",)),
    ),
)
def test_eval_fixture_manifest_rejects_unsafe_path_fields(
    field_name: str,
    replacement: tuple[str, ...],
) -> None:
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        FIXTURE_WORKSPACE,
        ("src/app/main.py", "tests/test_app.py"),
        task_id="task-06b-r1-01",
        expected_mutation_paths=("src/app/main.py",),
    )
    record = _fixture_manifest_artifact(manifest).model_dump(mode="json")
    record["fixture_manifest"][field_name] = list(replacement)

    with pytest.raises(ValidationError, match="fixture manifest paths"):
        validate_eval_artifact_record("fixture_manifest", record)


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "",
        ".",
        "..",
        "../src/app.py",
        "src/../app.py",
        "/src/app.py",
        "\\\\server\\share\\file.py",
        "C:/fixture/file.py",
        "src\\app.py",
        "src//app.py",
        "src/./app.py",
    ),
)
def test_eval_fixture_manifest_file_paths_share_fixture_path_policy(
    unsafe_path: str,
) -> None:
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        FIXTURE_WORKSPACE,
        ("src/app/main.py",),
        task_id="task-06b-r1-01",
    )
    record = _fixture_manifest_artifact(manifest).model_dump(mode="json")
    record["fixture_manifest"]["files"][0]["path"] = unsafe_path

    assert validate_eval_fixture_path(unsafe_path) is not None
    with pytest.raises(ValidationError, match="relative POSIX|root paths"):
        validate_eval_artifact_record("fixture_manifest", record)


def test_eval_fixture_manifest_workspace_policy_rejects_unsafe_stage_roots() -> None:
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        FIXTURE_WORKSPACE,
        ("src/app/main.py",),
        task_id="task-06b-r1-01",
    )
    record = _fixture_manifest_artifact(manifest).model_dump(mode="json")
    record["fixture_manifest"]["workspace_policy"]["stage_write_roots"][
        "eval_builder"
    ] = ["/tmp/src"]

    with pytest.raises(ValidationError, match="relative POSIX"):
        validate_eval_artifact_record("fixture_manifest", record)


@pytest.mark.parametrize(
    "hidden_check_id",
    (
        "hidden-definition-001",
        "expected-output-001",
        "scoring-rubric-001",
        "final-score-001",
        "secret-token-001",
        "C:/private/check",
        "/tmp/private/check",
        "millrace-agents-private-root",
    ),
)
def test_eval_fixture_manifest_hidden_check_ids_remain_opaque(
    hidden_check_id: str,
) -> None:
    with pytest.raises(ValidationError, match="opaque IDs|paths|private material"):
        eval_fixture_manifest_from_paths(
            "compact_fixture",
            FIXTURE_WORKSPACE,
            ("src/app/main.py",),
            task_id="task-06b-r1-01",
            hidden_check_ids=(hidden_check_id,),
        )


def test_eval_fixture_manifest_wrapper_rejects_non_opaque_hidden_check_ids() -> None:
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        FIXTURE_WORKSPACE,
        ("src/app/main.py",),
        task_id="task-06b-r1-01",
        hidden_check_ids=("opaque-evaluator-001",),
    )
    record = _fixture_manifest_artifact(manifest).model_dump(mode="json")
    record["fixture_manifest"]["hidden_check_ids"] = ["hidden-score-rubric"]

    with pytest.raises(ValidationError, match="opaque IDs"):
        validate_eval_artifact_record("fixture_manifest", record)


def test_eval_fixture_manifest_rejects_unsafe_expanded_fields() -> None:
    with pytest.raises(ValidationError, match="fixture manifest paths"):
        EvalFixtureManifest(
            schema_version=1,
            fixture_id="compact_fixture",
            fixture_revision="fixture-revision-1",
            task_id="task-06b-r1-01",
            source_root_label="fixture_workspace",
            allowed_read_paths=("/tmp/source.py",),
            allowed_write_paths=("src",),
            allowed_command_roots=("tests",),
            visible_acceptance_checks=("check-public-tests",),
            hidden_check_ids=("opaque-evaluator-001",),
            expected_mutation_paths=("src/app/main.py",),
            files=(
                EvalFixtureFile(
                    path="src/app/main.py",
                    sha256="7ca5abf65adee67782e5c831d9ee108b323c5a9d9fda11f97a9910b18f6b1bdc",
                    size_bytes=35,
                    role="source",
                    model_readable=True,
                    builder_mutable=True,
                ),
            ),
        )

    with pytest.raises(ValidationError, match="opaque IDs"):
        EvalFixtureManifest(
            schema_version=1,
            fixture_id="compact_fixture",
            fixture_revision="fixture-revision-1",
            task_id="task-06b-r1-01",
            source_root_label="fixture_workspace",
            allowed_read_paths=("src/app/main.py",),
            allowed_write_paths=("src",),
            allowed_command_roots=("tests",),
            visible_acceptance_checks=("check-public-tests",),
            hidden_check_ids=("hidden-score-rubric",),
            expected_mutation_paths=("src/app/main.py",),
            files=(
                EvalFixtureFile(
                    path="src/app/main.py",
                    sha256="7ca5abf65adee67782e5c831d9ee108b323c5a9d9fda11f97a9910b18f6b1bdc",
                    size_bytes=35,
                    role="source",
                    model_readable=True,
                    builder_mutable=True,
                ),
            ),
        )

    with pytest.raises(ValidationError, match="closed role set"):
        EvalFixtureFile(
            path="src/app/main.py",
            sha256="7ca5abf65adee67782e5c831d9ee108b323c5a9d9fda11f97a9910b18f6b1bdc",
            size_bytes=35,
            role="secret",
            model_readable=True,
            builder_mutable=True,
        )


def test_eval_fixture_path_validation_rejects_unsafe_paths(tmp_path: Path) -> None:
    absolute_path = tmp_path / "fixture" / "file.py"
    unsafe_paths = (
        "",
        ".",
        "..",
        "../src/app.py",
        "src/../app.py",
        "/src/app.py",
        "\\\\server\\share\\file.py",
        "C:/fixture/file.py",
        "src\\app.py",
        "src//app.py",
        "src/./app.py",
        str(absolute_path),
    )

    for path in unsafe_paths:
        violation = validate_eval_fixture_path(path)
        assert violation is not None
        assert violation.diagnostic_code == "MF-EVAL-F001"
        assert "/mnt/f" not in violation.model_dump_json()
        assert str(tmp_path) not in violation.model_dump_json()

    fixture_root = tmp_path / "fixture"
    outside = tmp_path / "outside"
    fixture_root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (fixture_root / "linked.txt").symlink_to(outside / "secret.txt")

    symlink_violation = validate_eval_fixture_path(
        "linked.txt", filesystem_root=fixture_root
    )
    assert symlink_violation is not None
    assert symlink_violation.diagnostic_code == "MF-EVAL-F002"
    assert str(tmp_path) not in symlink_violation.model_dump_json()


def test_eval_fixture_workspace_snapshot_reports_ignored_generated_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_WORKSPACE, workspace)
    (workspace / ".pytest_cache").mkdir()
    (workspace / ".pytest_cache" / "README.md").write_text(
        "generated", encoding="utf-8"
    )
    (workspace / "src" / "app" / "__pycache__").mkdir()
    (workspace / "src" / "app" / "__pycache__" / "main.cpython-312.pyc").write_bytes(
        b"generated"
    )
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        workspace,
        ("src/app/main.py", "tests/test_app.py"),
        task_id="task-06b-r1-01",
    )

    snapshot = eval_fixture_workspace_snapshot(manifest, workspace)

    assert snapshot.added_paths == ()
    assert snapshot.modified_paths == ()
    assert snapshot.deleted_paths == ()
    assert snapshot.unchanged_paths == ("src/app/main.py", "tests/test_app.py")
    assert snapshot.fixture_manifest_sha256 == eval_fixture_manifest_sha256(manifest)
    ignored_generated_paths = set(snapshot.ignored_generated_paths)
    assert {
        ".pytest_cache/README.md",
        "src/app/__pycache__/main.cpython-312.pyc",
    } <= ignored_generated_paths
    assert all(
        any(
            root in PurePosixPath(path).parts
            for root in manifest.workspace_policy.ignored_generated_roots
        )
        or any(
            path.endswith(suffix)
            for suffix in manifest.workspace_policy.ignored_generated_suffixes
        )
        for path in snapshot.ignored_generated_paths
    )
    assert snapshot.unauthorized_mutation_paths == ()
    assert str(tmp_path) not in snapshot.model_dump_json()


def test_eval_fixture_workspace_snapshot_reports_unauthorized_mutations(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_WORKSPACE, workspace)
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        workspace,
        ("src/app/main.py", "tests/test_app.py"),
        task_id="task-06b-r1-01",
        workspace_policy=EvalFixtureWorkspacePolicy(
            stage_write_roots={EvalStageId.BUILDER: ("src",)}
        ),
    )
    (workspace / "src" / "app" / "main.py").write_text(
        "def answer() -> int:\n    return 43\n", encoding="utf-8"
    )
    (workspace / "tests" / "test_app.py").unlink()
    (workspace / "README.md").write_text("unexpected", encoding="utf-8")

    snapshot = eval_fixture_workspace_snapshot(manifest, workspace)

    assert snapshot.added_paths == ("README.md",)
    assert snapshot.modified_paths == ("src/app/main.py",)
    assert snapshot.deleted_paths == ("tests/test_app.py",)
    assert snapshot.unchanged_paths == ()
    assert snapshot.unauthorized_mutation_paths == (
        "README.md",
        "tests/test_app.py",
    )


def test_eval_fixture_workspace_snapshot_reports_symlink_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    shutil.copytree(FIXTURE_WORKSPACE, workspace)
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "leak.txt").symlink_to(outside / "secret.txt")
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        workspace,
        ("src/app/main.py", "tests/test_app.py"),
        task_id="task-06b-r1-01",
    )

    snapshot = eval_fixture_workspace_snapshot(manifest, workspace)

    assert snapshot.added_paths == ()
    assert snapshot.violations == (snapshot.violations[0],)
    assert snapshot.violations[0].path == "leak.txt"
    assert snapshot.violations[0].diagnostic_code == "MF-EVAL-F002"
    assert snapshot.unauthorized_mutation_paths == ("leak.txt",)
    assert str(tmp_path) not in snapshot.model_dump_json()


def _closure_fixture_snapshot(
    tmp_path: Path,
) -> tuple[EvalFixtureManifest, EvalFixtureWorkspaceSnapshot]:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_WORKSPACE, workspace)
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        workspace,
        ("src/app/main.py", "tests/test_app.py"),
        task_id="task-06b-r1-01",
    )
    return manifest, eval_fixture_workspace_snapshot(manifest, workspace)


def _closure_artifact_bundle(
    manifest: EvalFixtureManifest,
    *,
    arbiter_verdict: EvalArbiterVerdictValue = EvalArbiterVerdictValue.CLOSED,
    disposition: EvalCandidateDisposition = EvalCandidateDisposition.APPROVED,
    context_visible_check_ids: tuple[str, ...] = ("check-public-tests",),
    closure_evidence_references: tuple[EvalArtifactReference, ...] | None = None,
) -> dict[str, object]:
    check = _acceptance_check()
    context_snapshot = build_eval_context_snapshot(
        trial_id="trial-001",
        policy=default_eval_stage_context_policy(EvalStageId.ARBITER),
        required_artifact_summaries=(
            EvalContextArtifactSummary(
                artifact_id="checker_verdict",
                summary="public checker verdict summary",
            ),
        ),
        visible_acceptance_check_ids=context_visible_check_ids,
    )
    context_artifact = EvalContextSnapshotArtifact(
        trial_id=context_snapshot.trial_id,
        stage_id=context_snapshot.stage_id,
        created_by="eval_runtime",
        summary="compact public context snapshot",
        context_tier=context_snapshot.context_tier,
        allowed_capabilities=context_snapshot.allowed_capabilities,
        allowed_paths=context_snapshot.allowed_paths,
        current_stage_contract=context_snapshot.current_stage_contract,
        required_artifact_summaries=(
            EvalArtifactReference(
                artifact_id=EvalArtifactId.CHECKER_VERDICT,
                summary="public checker verdict summary",
            ),
        ),
        visible_acceptance_check_ids=context_snapshot.visible_acceptance_check_ids,
        redaction=context_snapshot.redaction,
        redaction_summary=context_snapshot.redaction.summary,
        byte_budget=context_snapshot.byte_budget,
        token_budget=context_snapshot.token_budget,
        resource_ceiling=context_snapshot.resource_ceiling,
        fingerprint=context_snapshot.fingerprint,
    )
    checker_verdict = EvalCheckerVerdictArtifact(
        trial_id="trial-001",
        created_by="eval_checker",
        summary="public checker verdict",
        verdict=EvalCheckerVerdictValue.APPROVED,
        evidence_references=(
            _artifact_reference(EvalArtifactId.WORKSPACE_DIFF),
            _artifact_reference(EvalArtifactId.TEST_RESULTS),
        ),
    )
    return {
        "task": EvalTaskArtifact(
            trial_id="trial-001",
            created_by="fixture_author",
            summary="public task",
            task_id="task-001",
            prompt="make the public tests pass",
            fixture_id=manifest.fixture_id,
            acceptance_criteria=("public tests pass",),
            required_output_artifact_ids=(
                EvalArtifactId.WORKSPACE_DIFF,
                EvalArtifactId.PATCH_SUMMARY,
                EvalArtifactId.TEST_RESULTS,
            ),
        ),
        "fixture_manifest": _fixture_manifest_artifact(manifest),
        "acceptance_checks": EvalAcceptanceChecksArtifact(
            trial_id="trial-001",
            created_by="fixture_author",
            summary="visible acceptance checks",
            visible_acceptance_checks=(check,),
        ),
        "plan": EvalPlanArtifact(
            trial_id="trial-001",
            created_by="eval_planner",
            summary="public implementation plan",
            references=(_artifact_reference(EvalArtifactId.TASK),),
            implementation_steps=("inspect source", "run public tests"),
            expected_files_to_inspect=("src/app/main.py",),
            expected_files_to_mutate=("src/app/main.py",),
            checks_to_run=("python -m pytest tests/test_app.py",),
            no_hidden_checks_known=True,
        ),
        "workspace_diff": EvalWorkspaceDiffArtifact(
            trial_id="trial-001",
            created_by="eval_builder",
            summary="public workspace diff",
            modified_paths=("src/app/main.py",),
        ),
        "patch_summary": EvalPatchSummaryArtifact(
            trial_id="trial-001",
            created_by="eval_builder",
            summary="public patch summary",
            changed_files=("src/app/main.py",),
            behavior_summary="updated public implementation",
        ),
        "test_results": EvalTestResultsArtifact(
            trial_id="trial-001",
            created_by="eval_builder",
            summary="public test results",
            command=("python", "-m", "pytest", "tests/test_app.py"),
            exit_code=0,
            duration_seconds=1,
            output_summary="public tests passed",
            passed_count=1,
            failed_count=0,
            skipped_count=0,
            deterministic=True,
            allowed_by_policy=True,
        ),
        "checker_verdict": checker_verdict,
        "arbiter_verdict": EvalArbiterVerdictArtifact(
            trial_id="trial-001",
            created_by="eval_arbiter",
            summary="public arbiter verdict",
            verdict=arbiter_verdict,
            candidate_disposition=disposition,
            closure_evidence_references=closure_evidence_references
            if closure_evidence_references is not None
            else (
                _artifact_reference(EvalArtifactId.WORKSPACE_DIFF),
                _artifact_reference(EvalArtifactId.PATCH_SUMMARY),
                _artifact_reference(EvalArtifactId.TEST_RESULTS),
                _artifact_reference(EvalArtifactId.CHECKER_VERDICT),
            ),
            public_acceptance_status="visible public checks accounted for",
        ),
        "context_snapshot": context_artifact,
    }


def test_eval_closure_validation_returns_valid_success_rejection_and_blocked(
    tmp_path: Path,
) -> None:
    manifest, fixture_snapshot = _closure_fixture_snapshot(tmp_path)

    success = validate_eval_closure(
        _closure_artifact_bundle(manifest),
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )
    assert success.valid is True
    assert success.outcome_kind == EvalClosureOutcomeKind.VALID_CLOSED_SUCCESS
    assert success.terminal_result == EvalTerminalResult.ARBITER_CLOSED
    assert success.candidate_disposition == EvalCandidateDisposition.APPROVED
    assert success.evidence_artifact_ids == (
        "workspace_diff",
        "patch_summary",
        "test_results",
        "checker_verdict",
    )

    rejection = validate_eval_closure(
        _closure_artifact_bundle(
            manifest,
            arbiter_verdict=EvalArbiterVerdictValue.REJECTED,
            disposition=EvalCandidateDisposition.REJECTED,
        ),
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )
    assert rejection.valid is True
    assert rejection.outcome_kind == EvalClosureOutcomeKind.VALID_CLOSED_REJECTION
    assert rejection.terminal_result == EvalTerminalResult.ARBITER_REJECTED

    blocked = validate_eval_closure(
        _closure_artifact_bundle(
            manifest,
            arbiter_verdict=EvalArbiterVerdictValue.BLOCKED,
            disposition=EvalCandidateDisposition.BLOCKED,
        ),
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )
    assert blocked.valid is True
    assert blocked.outcome_kind == EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME
    assert blocked.terminal_result == EvalTerminalResult.ARBITER_BLOCKED


def test_eval_closure_validation_accepts_builder_blocked_shortened_path(
    tmp_path: Path,
) -> None:
    manifest, fixture_snapshot = _closure_fixture_snapshot(tmp_path)
    artifact_bundle = _closure_artifact_bundle(
        manifest,
        arbiter_verdict=EvalArbiterVerdictValue.BLOCKED,
        disposition=EvalCandidateDisposition.BLOCKED,
        closure_evidence_references=(
            _artifact_reference(EvalArtifactId.TASK),
            _artifact_reference(EvalArtifactId.FIXTURE_MANIFEST),
            _artifact_reference(EvalArtifactId.ACCEPTANCE_CHECKS),
            _artifact_reference(EvalArtifactId.PLAN),
        ),
    )
    for artifact_id in (
        "workspace_diff",
        "patch_summary",
        "test_results",
        "checker_verdict",
    ):
        del artifact_bundle[artifact_id]

    result = validate_eval_closure(
        artifact_bundle,
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )

    assert result.valid is True
    assert result.outcome_kind == EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME
    assert result.terminal_result == EvalTerminalResult.ARBITER_BLOCKED
    assert result.candidate_disposition == EvalCandidateDisposition.BLOCKED
    assert result.missing_artifact_ids == ()
    assert result.evidence_artifact_ids == (
        "task",
        "fixture_manifest",
        "acceptance_checks",
        "plan",
    )


def test_eval_closure_validation_rejects_mutated_shortened_blocked_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_WORKSPACE, workspace)
    manifest = eval_fixture_manifest_from_paths(
        "compact_fixture",
        workspace,
        ("src/app/main.py", "tests/test_app.py"),
        task_id="task-06b-r1-01",
    )
    (workspace / "src" / "app" / "main.py").write_text(
        "def answer() -> int:\n    return 43\n", encoding="utf-8"
    )
    fixture_snapshot = eval_fixture_workspace_snapshot(manifest, workspace)
    artifact_bundle = _closure_artifact_bundle(
        manifest,
        arbiter_verdict=EvalArbiterVerdictValue.BLOCKED,
        disposition=EvalCandidateDisposition.BLOCKED,
        closure_evidence_references=(
            _artifact_reference(EvalArtifactId.TASK),
            _artifact_reference(EvalArtifactId.FIXTURE_MANIFEST),
            _artifact_reference(EvalArtifactId.ACCEPTANCE_CHECKS),
            _artifact_reference(EvalArtifactId.PLAN),
        ),
    )
    for artifact_id in (
        "workspace_diff",
        "patch_summary",
        "test_results",
        "checker_verdict",
    ):
        del artifact_bundle[artifact_id]

    result = validate_eval_closure(
        artifact_bundle,
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )

    assert fixture_snapshot.modified_paths == ("src/app/main.py",)
    assert fixture_snapshot.unauthorized_mutation_paths == ()
    assert result.valid is False
    assert result.outcome_kind == EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY
    assert result.diagnostics == (
        "shortened blocked terminal path requires unmodified fixture snapshot",
    )


def test_eval_closure_validation_preserves_missing_artifact_failures(
    tmp_path: Path,
) -> None:
    manifest, fixture_snapshot = _closure_fixture_snapshot(tmp_path)

    success_missing_execution = _closure_artifact_bundle(manifest)
    del success_missing_execution["workspace_diff"]
    success = validate_eval_closure(
        success_missing_execution,
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )
    assert success.valid is False
    assert success.outcome_kind == EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY
    assert success.missing_artifact_ids == ("workspace_diff",)

    rejection_missing_checker = _closure_artifact_bundle(
        manifest,
        arbiter_verdict=EvalArbiterVerdictValue.REJECTED,
        disposition=EvalCandidateDisposition.REJECTED,
    )
    del rejection_missing_checker["checker_verdict"]
    rejection = validate_eval_closure(
        rejection_missing_checker,
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )
    assert rejection.valid is False
    assert rejection.outcome_kind == EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY
    assert rejection.missing_artifact_ids == ("checker_verdict",)


def test_eval_closure_validation_rejects_contradictory_shortened_blocked_path(
    tmp_path: Path,
) -> None:
    manifest, fixture_snapshot = _closure_fixture_snapshot(tmp_path)
    artifact_bundle = _closure_artifact_bundle(
        manifest,
        arbiter_verdict=EvalArbiterVerdictValue.BLOCKED,
        disposition=EvalCandidateDisposition.APPROVED,
        closure_evidence_references=(
            _artifact_reference(EvalArtifactId.TASK),
            _artifact_reference(EvalArtifactId.PLAN),
        ),
    )
    for artifact_id in (
        "workspace_diff",
        "patch_summary",
        "test_results",
        "checker_verdict",
    ):
        del artifact_bundle[artifact_id]

    result = validate_eval_closure(
        artifact_bundle,
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )

    assert result.valid is False
    assert result.outcome_kind == EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY
    assert result.diagnostics == (
        "blocked terminal path requires blocked candidate disposition",
    )


def test_eval_closure_validation_classifies_invalid_boundaries(
    tmp_path: Path,
) -> None:
    manifest, fixture_snapshot = _closure_fixture_snapshot(tmp_path)
    artifact_bundle = _closure_artifact_bundle(manifest)

    missing_artifact = dict(artifact_bundle)
    del missing_artifact["plan"]
    artifact_result = validate_eval_closure(
        missing_artifact,
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )
    assert artifact_result.valid is False
    assert (
        artifact_result.outcome_kind == EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY
    )
    assert artifact_result.missing_artifact_ids == ("plan",)

    denied_capability = validate_eval_closure(
        artifact_bundle,
        fixture_snapshot,
        {
            EvalStageId.PLANNER: (
                EvalCapabilityId.ARTIFACT_READ,
                EvalCapabilityId.SHELL_RUN,
            ),
            EvalStageId.BUILDER: default_eval_capability_envelopes()[
                EvalStageId.BUILDER
            ].capability_ids,
            EvalStageId.CHECKER: default_eval_capability_envelopes()[
                EvalStageId.CHECKER
            ].capability_ids,
            EvalStageId.ARBITER: default_eval_capability_envelopes()[
                EvalStageId.ARBITER
            ].capability_ids,
        },
    )
    assert denied_capability.valid is False
    assert (
        denied_capability.outcome_kind
        == EvalClosureOutcomeKind.INVALID_CAPABILITY_BOUNDARY
    )

    invalid_fixture = validate_eval_closure(
        artifact_bundle,
        fixture_snapshot.model_copy(
            update={"unauthorized_mutation_paths": ("README.md",)}
        ),
        default_eval_capability_envelopes(),
    )
    assert invalid_fixture.valid is False
    assert (
        invalid_fixture.outcome_kind == EvalClosureOutcomeKind.INVALID_FIXTURE_BOUNDARY
    )

    mismatched_manifest = manifest.model_copy(
        update={"fixture_revision": "fixture-revision-2"}
    )
    invalid_manifest_payload = validate_eval_closure(
        _closure_artifact_bundle(mismatched_manifest),
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )
    assert invalid_manifest_payload.valid is False
    assert (
        invalid_manifest_payload.outcome_kind
        == EvalClosureOutcomeKind.INVALID_FIXTURE_BOUNDARY
    )
    assert invalid_manifest_payload.diagnostics == (
        "fixture snapshot manifest digest does not match expanded manifest",
    )

    invalid_context = validate_eval_closure(
        _closure_artifact_bundle(
            manifest,
            context_visible_check_ids=("check-public-tests", "extra-visible-check"),
        ),
        fixture_snapshot,
        default_eval_capability_envelopes(),
    )
    assert invalid_context.valid is False
    assert (
        invalid_context.outcome_kind == EvalClosureOutcomeKind.INVALID_CONTEXT_BOUNDARY
    )
