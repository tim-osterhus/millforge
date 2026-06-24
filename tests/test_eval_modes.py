"""Focused tests for static compact eval mode descriptors."""

from __future__ import annotations

import json
from pathlib import Path

import millforge
import millforge.eval_modes as eval_modes
import pytest
from pydantic import ValidationError

from millforge.eval_artifacts import canonical_eval_artifact_layout
from millforge.eval_artifacts import EvalValidatorVisibilityRecord
from millforge.eval_boundary import (
    EvalContextTier,
    EvalFixtureWorkspacePolicy,
    compact_eval_boundary_baseline,
    default_eval_capability_envelopes,
    default_eval_stage_context_policies,
    default_eval_trial_resource_ceiling,
)
from millforge.eval_modes import (
    EVAL_SPEC_07_HARNESS_IDS,
    EvalModeConfound,
    EvalModeDescriptor,
    EvalModelProfile,
    EvalRunnerBinding,
    EvalRunnerKind,
    admit_eval_mode_live_execution,
    calculate_eval_mode_fairness_fingerprint,
    calculate_eval_mode_fingerprint,
    calculate_eval_model_profile_hash,
    canonical_eval_mode_fairness_bytes,
    canonical_eval_mode_bytes,
    compare_eval_modes_for_fairness,
    default_eval_model_profile,
    default_eval_small_millforge_mode,
    default_eval_small_pi_mode,
)
from millforge.eval_presets import (
    eval_preset_readiness_report,
    eval_spec_07_static_readiness_proven,
)
from millforge.eval_workflow import (
    EvalStageId,
    EvalTerminalResult,
    compact_eval_workflow_snapshot,
    default_compact_eval_workflow_graph,
)

FIXTURE_DIR = Path("tests/fixtures/eval_workflow")
DENIED_FIXTURE_TOKENS = (
    b"api_key",
    b"credential",
    b"password",
    b"/mnt/f",
    b"f:\\",
    b"/home/",
    b"\\users\\",
    b"millrace-agents",
    b"ideas/",
    b"ref-forge/",
    b"daemon state",
    b"hidden scorer",
    b"hidden_scorer",
    b"timestamp",
)


def test_default_descriptors_bind_exact_compact_stages_to_closed_runners() -> None:
    pi_mode = default_eval_small_pi_mode()
    millforge_mode = default_eval_small_millforge_mode()
    expected_stage_ids = (
        EvalStageId.PLANNER,
        EvalStageId.BUILDER,
        EvalStageId.CHECKER,
        EvalStageId.ARBITER,
    )

    assert pi_mode.mode_id == "eval_small_pi"
    assert millforge_mode.mode_id == "eval_small_millforge"
    assert tuple(binding.stage_id for binding in pi_mode.runner_bindings) == (
        expected_stage_ids
    )
    assert tuple(binding.stage_id for binding in millforge_mode.runner_bindings) == (
        expected_stage_ids
    )
    assert {binding.runner_kind for binding in pi_mode.runner_bindings} == {
        EvalRunnerKind.PI
    }
    assert {binding.runner_kind for binding in millforge_mode.runner_bindings} == {
        EvalRunnerKind.MILLFORGE
    }
    assert all(binding.harness_id is None for binding in pi_mode.runner_bindings)
    assert {
        binding.stage_id: binding.harness_id
        for binding in millforge_mode.runner_bindings
    } == dict(EVAL_SPEC_07_HARNESS_IDS)


def test_runner_kind_validation_rejects_unknown_and_distinct_identifiers() -> None:
    with pytest.raises(ValidationError):
        EvalRunnerBinding.model_validate(
            {"stage_id": "eval_builder", "runner_kind": "eval_builder"}
        )

    with pytest.raises(ValidationError):
        EvalRunnerBinding.model_validate(
            {
                "stage_id": "eval_builder",
                "runner_kind": "gpt-5.5",
            }
        )

    with pytest.raises(ValidationError):
        EvalRunnerBinding.model_validate(
            {
                "stage_id": "eval_builder",
                "runner_kind": "eval_small_millforge",
            }
        )


def test_default_descriptors_share_06a_graph_contracts_and_transitions() -> None:
    graph = default_compact_eval_workflow_graph()
    snapshot = compact_eval_workflow_snapshot(graph)

    for descriptor in (
        default_eval_small_pi_mode(),
        default_eval_small_millforge_mode(),
    ):
        assert descriptor.graph_id == graph.graph_id
        assert descriptor.graph_sha256 == snapshot["graph_sha256"]
        assert descriptor.stage_ids == graph.stage_ids
        assert descriptor.stage_contracts == graph.stages
        assert descriptor.terminal_results == tuple(EvalTerminalResult)
        assert descriptor.transition_semantics == tuple(snapshot["transitions"])


def test_default_descriptors_share_06b_boundary_and_artifact_contracts() -> None:
    expected_context_policies = tuple(
        default_eval_stage_context_policies()[stage_id] for stage_id in EvalStageId
    )
    expected_layout = tuple(
        canonical_eval_artifact_layout()[artifact_id]
        for artifact_id in canonical_eval_artifact_layout()
    )

    for descriptor in (
        default_eval_small_pi_mode(),
        default_eval_small_millforge_mode(),
    ):
        assert descriptor.boundary_baseline == compact_eval_boundary_baseline()
        assert descriptor.capability_envelopes == tuple(
            default_eval_capability_envelopes()[stage_id] for stage_id in EvalStageId
        )
        assert descriptor.fixture_policy == EvalFixtureWorkspacePolicy()
        assert descriptor.artifact_policy == expected_layout
        assert descriptor.context_tier == EvalContextTier.COMPACT
        assert descriptor.stage_context_policies == expected_context_policies
        assert (
            descriptor.trial_resource_ceiling == default_eval_trial_resource_ceiling()
        )
        assert descriptor.closure_boundary_id == (
            "millforge.eval_boundary.validate_eval_closure.v1"
        )


def test_model_profile_hash_is_shared_and_backend_neutral() -> None:
    profile = default_eval_model_profile()
    pi_mode = default_eval_small_pi_mode()
    millforge_mode = default_eval_small_millforge_mode()

    assert profile.serving_class == "local_openai_compatible"
    assert profile.serving_protocol == "openai_compatible_responses"
    assert profile.tool_calling_mode == "parser"
    assert profile.model_profile_hash == calculate_eval_model_profile_hash(profile)
    assert pi_mode.model_profile.model_profile_hash == profile.model_profile_hash
    assert millforge_mode.model_profile.model_profile_hash == profile.model_profile_hash

    with pytest.raises(ValidationError, match="serving_class"):
        EvalModelProfile.model_validate(
            profile.model_copy(update={"serving_class": "private_daemon"}).model_dump(
                mode="json"
            )
        )

    with pytest.raises(ValidationError, match="serving_protocol"):
        EvalModelProfile.model_validate(
            profile.model_copy(update={"serving_protocol": "ssh"}).model_dump(
                mode="json"
            )
        )

    with pytest.raises(ValidationError, match="tool_calling_mode"):
        EvalModelProfile.model_validate(
            profile.model_copy(update={"tool_calling_mode": "hidden"}).model_dump(
                mode="json"
            )
        )


def test_canonical_serialization_is_stable_ascii_and_descriptor_sensitive() -> None:
    pi_mode = default_eval_small_pi_mode()
    pi_mode_again = default_eval_small_pi_mode()
    millforge_mode = default_eval_small_millforge_mode()

    assert pi_mode.description
    assert millforge_mode.description
    assert canonical_eval_mode_bytes(pi_mode) == canonical_eval_mode_bytes(
        pi_mode_again
    )
    assert canonical_eval_mode_bytes(pi_mode).decode("ascii").endswith("\n")
    assert pi_mode.descriptor_fingerprint == calculate_eval_mode_fingerprint(pi_mode)
    assert millforge_mode.descriptor_fingerprint == calculate_eval_mode_fingerprint(
        millforge_mode
    )
    assert pi_mode.fairness_fingerprint == calculate_eval_mode_fairness_fingerprint(
        pi_mode
    )
    assert millforge_mode.fairness_fingerprint == (
        calculate_eval_mode_fairness_fingerprint(millforge_mode)
    )
    assert pi_mode.fairness_fingerprint == millforge_mode.fairness_fingerprint
    assert pi_mode.descriptor_fingerprint != millforge_mode.descriptor_fingerprint


def test_default_descriptors_share_fairness_fingerprint_only() -> None:
    pi_mode = default_eval_small_pi_mode()
    millforge_mode = default_eval_small_millforge_mode()
    pi_fairness = calculate_eval_mode_fairness_fingerprint(pi_mode)
    millforge_fairness = calculate_eval_mode_fairness_fingerprint(millforge_mode)
    rendered = canonical_eval_mode_fairness_bytes(pi_mode).decode("ascii")

    assert pi_fairness == millforge_fairness
    assert pi_mode.fairness_fingerprint == pi_fairness
    assert millforge_mode.fairness_fingerprint == millforge_fairness
    assert pi_mode.descriptor_fingerprint != millforge_mode.descriptor_fingerprint
    assert 'fairness_fingerprint":' not in rendered
    assert "runner_bindings" not in rendered
    assert "pi_live_runtime_support" not in rendered
    assert "spec_07_harness_presets" not in rendered
    assert "graph_id" in rendered
    assert "graph_sha256" in rendered
    assert "attempt_limits" in rendered
    assert "capability_envelopes" in rendered
    assert "fixture_policy" in rendered
    assert "artifact_policy" in rendered
    assert "redaction_categories" in rendered
    assert "validator_visibility_policy" in rendered
    assert "visible_acceptance_check_policy" in rendered
    assert "trial_resource_ceiling" in rendered
    assert "model_profile_hash" in rendered


def test_default_descriptors_declare_deterministic_confound_surface() -> None:
    expected_kinds = {
        "runner_capability_enforcement",
        "tool_calling",
        "parser_fallback",
        "context_packing",
        "model_endpoint",
        "token_accounting",
        "wall_clock_measurement",
        "deferred_millforge_harness",
        "deferred_pi_runtime",
    }

    for descriptor in (
        default_eval_small_pi_mode(),
        default_eval_small_millforge_mode(),
    ):
        assert {
            confound.kind for confound in descriptor.declared_confounds
        } == expected_kinds
        for confound in descriptor.declared_confounds:
            assert confound.applies_to
            assert confound.evidence
            assert confound.comparison_effect
            assert confound.mitigation

    millforge_harness_confound = next(
        confound
        for confound in default_eval_small_millforge_mode().declared_confounds
        if confound.kind == "deferred_millforge_harness"
    )
    rendered_evidence = " ".join(millforge_harness_confound.evidence)
    assert millforge_harness_confound.summary == (
        "Spec 07 Millforge harness live execution is deferred"
    )
    assert "Planner source record is implemented" in rendered_evidence
    assert "Builder source record is implemented" in rendered_evidence
    assert "Checker source record is implemented" in rendered_evidence
    assert "Arbiter source record is implemented" in rendered_evidence
    assert EVAL_SPEC_07_HARNESS_IDS[EvalStageId.PLANNER] in rendered_evidence
    assert EVAL_SPEC_07_HARNESS_IDS[EvalStageId.BUILDER] in rendered_evidence
    assert EVAL_SPEC_07_HARNESS_IDS[EvalStageId.CHECKER] in rendered_evidence
    assert EVAL_SPEC_07_HARNESS_IDS[EvalStageId.ARBITER] in rendered_evidence
    assert "source record is absent" not in rendered_evidence
    assert "all presets unimplemented" not in rendered_evidence
    assert "named but not implemented" not in rendered_evidence


def test_default_deferred_dependencies_include_live_admission_semantics() -> None:
    pi_dependency = default_eval_small_pi_mode().deferred_dependencies[0]
    millforge_dependency = default_eval_small_millforge_mode().deferred_dependencies[0]

    assert pi_dependency.dependency_kind == "runner_runtime"
    assert pi_dependency.affected_mode == "eval_small_pi"
    assert pi_dependency.all_stage_scope is True
    assert pi_dependency.affected_stage_id is None
    assert pi_dependency.static_descriptor_admission_behavior == (
        "static descriptor validation passes"
    )
    assert pi_dependency.live_execution_behavior == (
        "live execution admission fails closed until resolved"
    )
    assert millforge_dependency.dependency_kind == "runner_harness"
    assert millforge_dependency.affected_mode == "eval_small_millforge"
    assert set(millforge_dependency.reference_ids) == set(
        (
            EVAL_SPEC_07_HARNESS_IDS[EvalStageId.PLANNER],
            EVAL_SPEC_07_HARNESS_IDS[EvalStageId.BUILDER],
            EVAL_SPEC_07_HARNESS_IDS[EvalStageId.CHECKER],
            EVAL_SPEC_07_HARNESS_IDS[EvalStageId.ARBITER],
        )
    )
    assert millforge_dependency.summary == (
        "Spec 07 harness source records are available; live harness execution is "
        "not admitted"
    )
    assert "source records are absent" not in millforge_dependency.summary
    assert "named but not implemented" not in millforge_dependency.summary


def test_static_spec_07_readiness_removes_only_harness_preset_dependency() -> None:
    readiness_proven = eval_spec_07_static_readiness_proven(
        eval_preset_readiness_report()
    )
    before_readiness = default_eval_small_millforge_mode(
        spec_07_static_presets_ready=False
    )
    after_readiness = default_eval_small_millforge_mode(
        spec_07_static_presets_ready=readiness_proven
    )

    assert readiness_proven is True
    assert {
        dependency.dependency_id
        for dependency in before_readiness.deferred_dependencies
    } == {"spec_07_harness_presets"}
    assert after_readiness.deferred_dependencies == ()
    assert before_readiness.fairness_fingerprint == (
        after_readiness.fairness_fingerprint
    )
    assert calculate_eval_mode_fairness_fingerprint(before_readiness) == (
        calculate_eval_mode_fairness_fingerprint(after_readiness)
    )
    assert calculate_eval_mode_fairness_fingerprint(default_eval_small_pi_mode()) == (
        calculate_eval_mode_fairness_fingerprint(after_readiness)
    )


def test_default_fairness_comparison_is_engineering_smoke_only() -> None:
    report = compare_eval_modes_for_fairness()

    assert report.comparable is True
    assert report.classification == "engineering_smoke_only"
    assert report.shared_fairness_fingerprint == report.left_fairness_fingerprint
    assert report.left_fairness_fingerprint == report.right_fairness_fingerprint
    assert report.left_descriptor_fingerprint != report.right_descriptor_fingerprint
    assert {difference.field_path for difference in report.allowed_differences} == {
        "mode_id",
        "runner_bindings",
        "deferred_dependencies",
    }
    assert report.disallowed_differences == ()
    assert {
        dependency.dependency_id for dependency in report.deferred_dependencies
    } == {
        "pi_live_runtime_support",
        "spec_07_harness_presets",
    }
    assert {confound.severity for confound in report.confounds} == {
        "info",
        "warning",
        "invalidating",
    }
    assert all(confound.evidence for confound in report.confounds)
    assert all(confound.comparison_effect for confound in report.confounds)
    assert all(confound.mitigation for confound in report.confounds)
    assert "controlled" not in report.classification
    assert "score" not in report.classification


def test_fairness_comparison_reports_disallowed_graph_drift() -> None:
    pi_mode = default_eval_small_pi_mode()
    millforge_mode = default_eval_small_millforge_mode()
    drifted = millforge_mode.model_copy(update={"graph_id": "other.graph.v1"})

    report = compare_eval_modes_for_fairness(pi_mode, drifted)

    assert report.comparable is False
    assert report.shared_fairness_fingerprint is None
    assert {difference.field_path for difference in report.disallowed_differences} == {
        "graph_id"
    }


def test_fairness_comparison_reports_disallowed_model_profile_hash_drift() -> None:
    pi_mode = default_eval_small_pi_mode()
    millforge_mode = default_eval_small_millforge_mode()
    drifted_profile = millforge_mode.model_profile.model_copy(
        update={"model_profile_hash": "1" * 64}
    )
    drifted = millforge_mode.model_copy(update={"model_profile": drifted_profile})

    report = compare_eval_modes_for_fairness(pi_mode, drifted)

    assert report.comparable is False
    assert {difference.field_path for difference in report.disallowed_differences} == {
        "model_profile_hash"
    }


def test_confound_severity_validation_is_closed() -> None:
    EvalModeConfound(
        confound_id="token_accounting",
        kind="token_accounting",
        severity="warning",
        summary="token accounting differs by live runner",
        applies_to=("eval_small_pi", "eval_small_millforge"),
        evidence=("static descriptors do not include live token usage",),
        comparison_effect="live token parity is unproven",
        mitigation="compare model usage artifacts",
    )

    with pytest.raises(ValidationError, match="severity"):
        EvalModeConfound(
            confound_id="bad",
            kind="token_accounting",
            severity="critical",
            summary="unsupported severity",
            applies_to=("eval_small_pi",),
            evidence=("static descriptors do not include live token usage",),
            comparison_effect="live token parity is unproven",
            mitigation="compare model usage artifacts",
        )


def test_live_admission_fails_closed_with_structured_deferred_dependencies() -> None:
    pi_admission = admit_eval_mode_live_execution(default_eval_small_pi_mode())
    millforge_admission = admit_eval_mode_live_execution(
        default_eval_small_millforge_mode()
    )

    assert pi_admission.admitted is False
    assert pi_admission.rule_id == "eval.mode.live_admission.deferred_dependency"
    assert pi_admission.diagnostic_code == "MF-EVAL-M001"
    assert {
        dependency.dependency_id for dependency in pi_admission.deferred_dependencies
    } == {
        "pi_live_runtime_support",
        "model_backend_configuration",
        "resource_ceiling_enforcement",
        "fixture_workspace_creation",
    }
    assert millforge_admission.admitted is False
    millforge_harness_dependency = next(
        dependency
        for dependency in millforge_admission.deferred_dependencies
        if dependency.dependency_id == "spec_07_harness_presets"
    )
    assert millforge_harness_dependency.summary == (
        "Spec 07 harness source records are available; live harness execution is "
        "not admitted"
    )
    assert set(millforge_harness_dependency.reference_ids) == {
        EVAL_SPEC_07_HARNESS_IDS[EvalStageId.PLANNER],
        EVAL_SPEC_07_HARNESS_IDS[EvalStageId.BUILDER],
        EVAL_SPEC_07_HARNESS_IDS[EvalStageId.CHECKER],
        EVAL_SPEC_07_HARNESS_IDS[EvalStageId.ARBITER],
    }
    assert {
        dependency.dependency_id
        for dependency in millforge_admission.deferred_dependencies
    } == {
        "spec_07_harness_presets",
        "model_backend_configuration",
        "resource_ceiling_enforcement",
        "fixture_workspace_creation",
    }


def test_live_admission_readds_harness_dependency_after_static_readiness() -> None:
    readiness_mode = default_eval_small_millforge_mode(
        spec_07_static_presets_ready=True
    )

    admission = admit_eval_mode_live_execution(readiness_mode)
    live_presets_admitted = admit_eval_mode_live_execution(
        readiness_mode,
        spec_07_harness_presets_available=True,
    )

    assert readiness_mode.deferred_dependencies == ()
    assert {
        dependency.dependency_id for dependency in admission.deferred_dependencies
    } == {
        "spec_07_harness_presets",
        "model_backend_configuration",
        "resource_ceiling_enforcement",
        "fixture_workspace_creation",
    }
    assert {
        dependency.dependency_id
        for dependency in live_presets_admitted.deferred_dependencies
    } == {
        "model_backend_configuration",
        "resource_ceiling_enforcement",
        "fixture_workspace_creation",
    }
    assert admission.admitted is False
    assert live_presets_admitted.admitted is False


def test_descriptor_owned_mappings_are_immutable_after_validation() -> None:
    profile = default_eval_model_profile()
    descriptor = default_eval_small_pi_mode()
    canonical_before = canonical_eval_mode_bytes(descriptor)
    fingerprint_before = descriptor.descriptor_fingerprint

    with pytest.raises(TypeError):
        EVAL_SPEC_07_HARNESS_IDS[EvalStageId.PLANNER] = "tampered"  # type: ignore[index]

    with pytest.raises(TypeError):
        profile.cost_accounting["new"] = "value"  # type: ignore[index]

    with pytest.raises(TypeError):
        descriptor.transition_semantics[0]["outcome_kind"] = "tampered"  # type: ignore[index]

    assert canonical_eval_mode_bytes(descriptor) == canonical_before
    assert descriptor.descriptor_fingerprint == fingerprint_before
    assert calculate_eval_mode_fingerprint(descriptor) == fingerprint_before


def test_descriptor_validation_rejects_06b_visibility_policy_drift() -> None:
    descriptor = default_eval_small_pi_mode()
    drifted = descriptor.model_copy(
        update={
            "validator_visibility_policy": EvalValidatorVisibilityRecord(
                visible_acceptance_check_ids=("other-public-check",)
            )
        }
    )
    drifted = drifted.model_copy(
        update={"descriptor_fingerprint": calculate_eval_mode_fingerprint(drifted)}
    )

    with pytest.raises(ValidationError, match="validator visibility"):
        EvalModeDescriptor.model_validate(drifted.model_dump(mode="json"))


def test_descriptor_validation_rejects_06b_closure_boundary_drift() -> None:
    descriptor = default_eval_small_pi_mode()
    drifted = descriptor.model_copy(update={"closure_boundary_id": "other.boundary.v1"})
    drifted = drifted.model_copy(
        update={"descriptor_fingerprint": calculate_eval_mode_fingerprint(drifted)}
    )

    with pytest.raises(ValidationError, match="closure boundary"):
        EvalModeDescriptor.model_validate(drifted.model_dump(mode="json"))


@pytest.mark.parametrize(
    "leaking_key",
    (
        "credential=secret-value",
        "C:\\private\\costs",
    ),
)
def test_model_profile_rejects_private_material_in_mapping_keys(
    leaking_key: str,
) -> None:
    profile = default_eval_model_profile()
    drifted = profile.model_copy(
        update={
            "cost_accounting": {leaking_key: "none"},
            "model_profile_hash": "0" * 64,
        }
    )
    drifted = drifted.model_copy(
        update={"model_profile_hash": calculate_eval_model_profile_hash(drifted)}
    )

    with pytest.raises(ValidationError, match="private material|host paths"):
        EvalModelProfile.model_validate(drifted.model_dump(mode="json"))


def test_descriptor_canonical_json_excludes_secrets_paths_and_private_runtime() -> None:
    rendered = canonical_eval_mode_bytes(default_eval_small_millforge_mode()).decode(
        "ascii"
    )
    parsed = json.loads(rendered)

    assert parsed["mode_id"] == "eval_small_millforge"
    for denied in (
        "API_KEY",
        "credential_value",
        "F:\\",
        "/mnt/f",
        "/home/",
        "\\Users\\",
        "millrace-agents",
        "ideas/",
        "ref-forge/",
        "hidden scorer material body",
        "private daemon state",
    ):
        assert denied not in rendered


def test_default_descriptor_fixtures_are_canonical_path_free_snapshots() -> None:
    fixtures = {
        "default_eval_small_pi_mode.json": default_eval_small_pi_mode(),
        "default_eval_small_millforge_mode.json": default_eval_small_millforge_mode(),
    }
    fixture_payloads: dict[str, bytes] = {}

    for filename, descriptor in fixtures.items():
        fixture_bytes = (FIXTURE_DIR / filename).read_bytes()
        canonical_bytes = canonical_eval_mode_bytes(descriptor)
        parsed = json.loads(fixture_bytes)
        validated = EvalModeDescriptor.model_validate(parsed)
        lowered = fixture_bytes.lower()

        assert fixture_bytes == canonical_bytes
        assert fixture_bytes.decode("ascii").encode("ascii") == fixture_bytes
        assert fixture_bytes.endswith(b"\n")
        assert validated == descriptor
        assert validated.descriptor_fingerprint == descriptor.descriptor_fingerprint
        for denied in DENIED_FIXTURE_TOKENS:
            assert denied not in lowered

        fixture_payloads[filename] = fixture_bytes

    pi_descriptor = EvalModeDescriptor.model_validate(
        json.loads(fixture_payloads["default_eval_small_pi_mode.json"])
    )
    millforge_descriptor = EvalModeDescriptor.model_validate(
        json.loads(fixture_payloads["default_eval_small_millforge_mode.json"])
    )

    assert calculate_eval_mode_fairness_fingerprint(
        pi_descriptor
    ) == calculate_eval_mode_fairness_fingerprint(millforge_descriptor)
    assert pi_descriptor.descriptor_fingerprint != (
        millforge_descriptor.descriptor_fingerprint
    )


def test_eval_mode_public_exports_match_module_objects() -> None:
    for public_name in eval_modes.__all__:
        assert public_name in millforge.__all__
        assert getattr(millforge, public_name) is getattr(eval_modes, public_name)
