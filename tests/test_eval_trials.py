"""Focused tests for the public 08B eval-trial contract surface."""

from __future__ import annotations

import json

import millforge
import millforge.eval_trials as eval_trials
import pytest
from pydantic import ValidationError

from millforge.eval_artifacts import (
    EvalArtifactId,
    EvalArtifactManifestArtifact,
    EvalArtifactManifestEntry,
    calculate_eval_artifact_manifest_sha256,
)
from millforge.eval_suite import (
    EVAL_SUITE_DEFAULT_SCORER_VERSION,
    EvalCapabilityAuditSummary,
    EvalCheckResult,
    EvalHashRecord,
    EvalScorerInput,
    EvalScorerResult,
    EvalSuiteExecutionMode,
    EvalTaskFixture,
    EvalTrialOutcome,
    calculate_eval_scorer_input_hash,
    calculate_eval_scorer_result_hash,
    load_eval_task_fixtures,
)
from millforge.eval_trials import (
    EVAL_TRIAL_ADMITTED_ARM_IDS,
    EVAL_TRIAL_ARTIFACT_BUNDLE_HASH_KIND,
    EVAL_TRIAL_PLAN_HASH_KIND,
    EVAL_TRIAL_RECORD_HASH_KIND,
    EVAL_TRIAL_RESUME_INDEX_HASH_KIND,
    EVAL_TRIAL_STORE_MANIFEST_HASH_KIND,
    EvalFakeOutcomeScriptKind,
    EvalFakeRunnerArtifactBundle,
    EvalTrialArmId,
    EvalTrialExecutionResult,
    EvalTrialFakeRunnerScript,
    EvalTrialInvalidDiagnostic,
    EvalTrialInvalidDiagnosticCode,
    EvalTrialRecord,
    EvalTrialResumeIndex,
    EvalTrialStoreManifest,
    calculate_eval_fake_runner_artifact_bundle_hash,
    calculate_eval_trial_plan_hash,
    calculate_eval_trial_record_hash,
    calculate_eval_trial_resume_index_hash,
    calculate_eval_trial_store_manifest_hash,
    append_eval_trial_record_to_campaign_store,
    canonical_eval_fake_runner_artifact_bundle_bytes,
    canonical_eval_trial_plan_bytes,
    canonical_eval_trial_record_bytes,
    canonical_eval_trial_resume_index_bytes,
    canonical_eval_trial_store_manifest_bytes,
    default_eval_trial_arm_definitions,
    default_eval_trial_plan,
    default_eval_trial_parity_evidence,
    deny_eval_trial_live_execution,
    fixture_copy_unavailable_diagnostic,
    plan_paired_eval_trials,
    resume_eval_trial_campaign_store,
    run_offline_fake_eval_trial,
)
from millforge.eval_workflow import EvalTerminalResult


def test_eval_trials_public_contracts_are_root_exports() -> None:
    for public_name in eval_trials.__all__:
        assert public_name in millforge.__all__
        assert getattr(millforge, public_name) is getattr(eval_trials, public_name)


def test_admitted_trial_arms_are_exactly_pi_and_millforge() -> None:
    arms = default_eval_trial_arm_definitions()
    parity = default_eval_trial_parity_evidence(arms)

    assert EVAL_TRIAL_ADMITTED_ARM_IDS == ("eval_small_pi", "eval_small_millforge")
    assert tuple(arm.arm_id.value for arm in arms) == EVAL_TRIAL_ADMITTED_ARM_IDS
    assert tuple(arm.mode_id for arm in arms) == EVAL_TRIAL_ADMITTED_ARM_IDS
    assert parity.left_arm_id is EvalTrialArmId.EVAL_SMALL_PI
    assert parity.right_arm_id is EvalTrialArmId.EVAL_SMALL_MILLFORGE
    assert parity.comparable_offline is True
    assert parity.diagnostics == ()
    assert parity.shared_fairness_fingerprint == arms[0].fairness_fingerprint
    assert parity.shared_fairness_fingerprint == arms[1].fairness_fingerprint


def test_default_trial_plan_reuses_suite_campaign_fixture_and_readiness_contracts() -> (
    None
):
    fixture = load_eval_task_fixtures()[0]
    script = _fake_script()
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.default.v1",
        fixture=fixture,
        fake_runner_script=script,
    )

    assert plan.campaign_manifest.execution_mode.value == "offline_fake"
    assert plan.campaign_manifest.campaign_manifest_hash == (
        plan.campaign_manifest.campaign_manifest_hash
    )
    assert plan.fixture_instance.public_projection.fixture_id == fixture.fixture_id
    assert plan.fixture_instance.fixture_hash == fixture.fixture_hash
    assert plan.fixture_instance.fixture_instance_id.startswith(plan.trial_id)
    assert plan.fixture_instance.copy_unavailable_diagnostic == (
        fixture_copy_unavailable_diagnostic()
    )
    assert plan.fake_runner_script == script
    assert plan.spec_07_readiness.available is True
    assert plan.plan_hash_kind == EVAL_TRIAL_PLAN_HASH_KIND
    assert plan.plan_hash == calculate_eval_trial_plan_hash(plan)
    assert canonical_eval_trial_plan_bytes(plan).decode("ascii").endswith("\n")
    assert canonical_eval_trial_plan_bytes(plan) == canonical_eval_trial_plan_bytes(
        default_eval_trial_plan(
            trial_plan_id="trial.plan.08b.default.v1",
            fixture=fixture,
            fake_runner_script=script,
        )
    )


def test_paired_trial_planning_is_deterministic_and_records_parity_inputs() -> None:
    fixtures = load_eval_task_fixtures()[:2]
    script = _fake_script()

    first = plan_paired_eval_trials(
        fixtures=fixtures, fake_runner_script=script, seed=42
    )
    second = plan_paired_eval_trials(
        fixtures=fixtures, fake_runner_script=script, seed=42
    )

    assert tuple(plan.plan_hash for plan in first) == tuple(
        plan.plan_hash for plan in second
    )
    assert tuple(canonical_eval_trial_plan_bytes(plan) for plan in first) == tuple(
        canonical_eval_trial_plan_bytes(plan) for plan in second
    )
    assert len({plan.trial_id for plan in first}) == len(first)
    assert len({plan.fixture_instance.fixture_instance_id for plan in first}) == len(
        first
    )
    assert {tuple(plan.arm_order) for plan in first} == {
        (EvalTrialArmId.EVAL_SMALL_PI, EvalTrialArmId.EVAL_SMALL_MILLFORGE),
        (EvalTrialArmId.EVAL_SMALL_MILLFORGE, EvalTrialArmId.EVAL_SMALL_PI),
    }

    readiness_hashes = {
        compiled.stage_id: compiled.compiled_sha256
        for compiled in first[0].spec_07_readiness.compiled_plans
    }
    assert set(readiness_hashes) == {
        "eval_planner",
        "eval_builder",
        "eval_checker",
        "eval_arbiter",
    }
    for plan in first:
        assert set(plan.arm_order) == {
            EvalTrialArmId.EVAL_SMALL_PI,
            EvalTrialArmId.EVAL_SMALL_MILLFORGE,
        }
        assert plan.campaign_store_root.startswith("eval/campaigns/")
        shared = {
            (
                arm_plan.campaign_manifest_hash,
                arm_plan.model_manifest_hash,
                arm_plan.workflow_graph_hash,
                arm_plan.fixture_pack_hash,
                arm_plan.fixture_hash,
                arm_plan.visible_acceptance_criteria_hash,
                arm_plan.hidden_check_set_hash,
                arm_plan.scorer_version,
            )
            for arm_plan in plan.arm_plans
        }
        assert len(shared) == 1
        for arm_plan in plan.arm_plans:
            assert arm_plan.artifact_root.startswith(f"artifacts/{plan.trial_id}/")
            if arm_plan.arm_id is EvalTrialArmId.EVAL_SMALL_MILLFORGE:
                assert dict(arm_plan.treatment_compiled_harness_hashes) == (
                    readiness_hashes
                )
            else:
                assert arm_plan.baseline_pi_runtime_hash is None
                assert arm_plan.baseline_runtime_diagnostic is not None


def test_paired_trial_planning_rejects_invalid_requests() -> None:
    fixtures = load_eval_task_fixtures()
    fixture = fixtures[0]
    script = _fake_script()
    arms = default_eval_trial_arm_definitions()

    with pytest.raises(ValueError, match="unsupported arms"):
        plan_paired_eval_trials(
            fixtures=(fixture,),
            fake_runner_script=script,
            arms=(arms[1], arms[0]),
        )

    with pytest.raises(ValueError, match="duplicate trial IDs"):
        plan_paired_eval_trials(
            fixtures=fixtures[:2],
            fake_runner_script=script,
            trial_ids=("trial.duplicate.v1", "trial.duplicate.v1"),
        )

    with pytest.raises(ValueError, match="duplicate fixture/arm/trial-index"):
        plan_paired_eval_trials(
            fixtures=fixtures[:2],
            fake_runner_script=script,
            trial_indexes=(0, 0),
        )

    with pytest.raises(ValueError, match="missing fixture hashes"):
        plan_paired_eval_trials(
            fixtures=(fixture.model_copy(update={"fixture_hash": ""}),),
            fake_runner_script=script,
        )

    campaign = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.live-template.v1",
        fixture=fixture,
        fake_runner_script=script,
    ).campaign_manifest
    live_campaign = type(campaign).model_construct(
        **(
            campaign.model_dump(mode="python")
            | {"execution_mode": EvalSuiteExecutionMode.LIVE_RUNNER}
        )
    )
    with pytest.raises(ValueError, match="live execution"):
        plan_paired_eval_trials(
            fixtures=(fixture,),
            fake_runner_script=script,
            campaign_manifest=live_campaign,
        )

    with pytest.raises(ValueError, match="relative paths"):
        plan_paired_eval_trials(
            fixtures=(fixture,),
            fake_runner_script=script,
            campaign_store_root="/tmp/eval/campaigns/default",
        )

    with pytest.raises(ValueError, match="inside the campaign store"):
        plan_paired_eval_trials(
            fixtures=(fixture,),
            fake_runner_script=script,
            trial_ids=("trial.outside.v1",),
            artifact_roots_by_trial_arm={
                ("trial.outside.v1", EvalTrialArmId.EVAL_SMALL_PI): (
                    "other-root/artifacts/pi"
                )
            },
        )


def test_trial_plan_validation_rejects_arm_drift_and_private_material() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.default.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    payload = plan.model_dump(mode="json")

    with pytest.raises(ValidationError, match="exactly the admitted arms"):
        eval_trials.EvalTrialPlan.model_validate(
            payload | {"arms": (payload["arms"][1], payload["arms"][0])}
        )

    with pytest.raises(ValidationError, match="forbidden private material"):
        eval_trials.EvalTrialPlan.model_validate(
            payload | {"trial_plan_id": "trial.with.api_key"}
        )


def test_trial_plan_validation_rejects_rehashed_parity_and_fixture_drift() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.default.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )

    swapped_parity = plan.parity_evidence.model_copy(
        update={
            "left_arm_id": EvalTrialArmId.EVAL_SMALL_MILLFORGE,
            "right_arm_id": EvalTrialArmId.EVAL_SMALL_PI,
        }
    )
    with pytest.raises(ValidationError, match="parity left arm"):
        eval_trials.EvalTrialPlan.model_validate(
            _rehash_plan_payload(plan, parity_evidence=swapped_parity)
        )

    parity_mutations = (
        ("left_descriptor_fingerprint", "parity left descriptor"),
        ("right_descriptor_fingerprint", "parity right descriptor"),
        ("shared_fairness_fingerprint", "parity fairness"),
        ("workflow_graph_hash", "parity workflow graph"),
        ("model_profile_hash", "parity model profile"),
        ("fixture_pack_hash", "parity fixture pack"),
    )
    for field_name, expected_error in parity_mutations:
        drifted_parity = plan.parity_evidence.model_copy(update={field_name: "f" * 64})
        with pytest.raises(ValidationError, match=expected_error):
            eval_trials.EvalTrialPlan.model_validate(
                _rehash_plan_payload(plan, parity_evidence=drifted_parity)
            )

    drifted_fixture = plan.fixture_instance.model_copy(
        update={"fixture_pack_hash": "f" * 64}
    )
    with pytest.raises(ValidationError, match="fixture_pack_hash"):
        eval_trials.EvalTrialPlan.model_validate(
            _rehash_plan_payload(plan, fixture_instance=drifted_fixture)
        )


def test_trial_plan_rejects_nested_pydantic_public_projection_private_material() -> (
    None
):
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.default.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    private_projection = plan.fixture_instance.public_projection.model_copy(
        update={"visible_acceptance_criteria": ("hidden_checks scorer_rubric",)}
    )
    draft_fixture = plan.fixture_instance.model_copy(
        update={"public_projection": private_projection}
    )
    payload = plan.model_dump(mode="json")
    fixture_payload = dict(payload["fixture_instance"])
    fixture_payload["public_projection"] = private_projection
    payload["fixture_instance"] = fixture_payload
    draft_plan = plan.model_copy(
        update={"fixture_instance": draft_fixture, "plan_hash": "0" * 64}
    )
    payload["plan_hash"] = calculate_eval_trial_plan_hash(draft_plan)

    with pytest.raises(ValidationError, match="forbidden private material"):
        eval_trials.EvalTrialPlan.model_validate(payload)


def test_invalid_diagnostics_reject_endpoint_credentials_and_scorer_markers() -> None:
    diagnostic_payload = {
        "diagnostic_code": EvalTrialInvalidDiagnosticCode.ARM_NOT_ADMITTED,
        "rule_id": "eval_trials.invalid.public_payload",
    }

    with pytest.raises(ValidationError, match="endpoint URLs"):
        EvalTrialInvalidDiagnostic.model_validate(
            diagnostic_payload | {"summary": "see https://example.com/model"}
        )
    with pytest.raises(ValidationError, match="credential-shaped API key"):
        EvalTrialInvalidDiagnostic.model_validate(
            diagnostic_payload | {"summary": "token sk-live-1234567890abcdefghi"}
        )
    with pytest.raises(ValidationError, match="forbidden private material"):
        EvalTrialInvalidDiagnostic.model_validate(
            diagnostic_payload | {"summary": "contains scorer_rubric hidden_checks"}
        )
    with pytest.raises(ValidationError, match="secret-like field name"):
        EvalTrialFakeRunnerScript.model_validate(
            _fake_script().model_dump(mode="json")
            | {"stage_result_summaries": {"client_secret": "redacted"}}
        )


def test_live_execution_is_fail_closed_with_typed_diagnostic() -> None:
    diagnostic = deny_eval_trial_live_execution()

    assert diagnostic.diagnostic_code is (
        EvalTrialInvalidDiagnosticCode.LIVE_EXECUTION_UNAVAILABLE
    )
    assert diagnostic.rule_id == "eval_trials.live_execution.unavailable"


def test_artifact_bundle_hash_and_zero_usage_contract() -> None:
    bundle = _artifact_bundle("a" * 64, EvalTrialArmId.EVAL_SMALL_PI)

    assert bundle.zero_model_usage is True
    assert bundle.zero_external_usage is True
    assert bundle.artifact_bundle_hash_kind == EVAL_TRIAL_ARTIFACT_BUNDLE_HASH_KIND
    assert bundle.artifact_bundle_hash == (
        calculate_eval_fake_runner_artifact_bundle_hash(bundle)
    )
    assert (
        canonical_eval_fake_runner_artifact_bundle_bytes(bundle)
        .decode("ascii")
        .endswith("\n")
    )

    payload = bundle.model_dump(mode="json")
    with pytest.raises(ValidationError, match="zero-usage"):
        EvalFakeRunnerArtifactBundle.model_validate(
            payload
            | {
                "zero_model_usage": False,
                "artifact_bundle_hash": "0" * 64,
            }
        )


@pytest.mark.parametrize(
    ("script_kind", "expected_outcome", "terminal_results", "expected_false_success"),
    (
        (
            EvalFakeOutcomeScriptKind.VALID_COMPLETION,
            EvalTrialOutcome.VALID_COMPLETION,
            (
                EvalTerminalResult.PLAN_READY,
                EvalTerminalResult.BUILDER_COMPLETE,
                EvalTerminalResult.CHECKER_APPROVED,
                EvalTerminalResult.ARBITER_CLOSED,
            ),
            False,
        ),
        (
            EvalFakeOutcomeScriptKind.CORRECT_BLOCK,
            EvalTrialOutcome.CORRECTLY_BLOCKED,
            (EvalTerminalResult.BUILDER_BLOCKED,),
            False,
        ),
        (
            EvalFakeOutcomeScriptKind.FALSE_CLOSURE,
            EvalTrialOutcome.FALSE_CLOSURE,
            (
                EvalTerminalResult.BUILDER_COMPLETE,
                EvalTerminalResult.CHECKER_APPROVED,
                EvalTerminalResult.ARBITER_CLOSED,
            ),
            True,
        ),
        (
            EvalFakeOutcomeScriptKind.FALSE_SUCCESS_WITHOUT_CLOSURE,
            EvalTrialOutcome.VALID_COMPLETION,
            (
                EvalTerminalResult.BUILDER_COMPLETE,
                EvalTerminalResult.CHECKER_APPROVED,
            ),
            True,
        ),
        (
            EvalFakeOutcomeScriptKind.RUNTIME_FAILURE,
            EvalTrialOutcome.RUNTIME_FAILURE,
            (EvalTerminalResult.BUILDER_COMPLETE,),
            True,
        ),
        (
            EvalFakeOutcomeScriptKind.PROVIDER_FAILURE,
            EvalTrialOutcome.PROVIDER_FAILURE,
            (EvalTerminalResult.BUILDER_COMPLETE,),
            True,
        ),
        (
            EvalFakeOutcomeScriptKind.INVALID_TRIAL,
            EvalTrialOutcome.INVALID_TRIAL,
            (EvalTerminalResult.PLAN_READY,),
            False,
        ),
    ),
)
def test_offline_fake_runner_emits_scorer_compatible_outcome_shapes(
    script_kind: EvalFakeOutcomeScriptKind,
    expected_outcome: EvalTrialOutcome,
    terminal_results: tuple[EvalTerminalResult, ...],
    expected_false_success: bool,
) -> None:
    fixture = load_eval_task_fixtures()[0]
    if script_kind is EvalFakeOutcomeScriptKind.CORRECT_BLOCK:
        fixture = fixture.model_copy(
            update={"expected_final_outcome": EvalTrialOutcome.CORRECTLY_BLOCKED}
        )
    plan = default_eval_trial_plan(
        trial_plan_id=f"trial.plan.08b.{script_kind.value}.v1",
        fixture=fixture,
        fake_runner_script=EvalTrialFakeRunnerScript(
            script_id=f"fake.{script_kind.value}.v1",
            script_kind=script_kind,
            terminal_results=terminal_results,
            expected_outcome=expected_outcome,
        ),
    )

    run = run_offline_fake_eval_trial(plan, fixture=fixture)

    assert tuple(bundle.arm_id for bundle in run.artifact_bundles) == (
        EvalTrialArmId.EVAL_SMALL_PI,
        EvalTrialArmId.EVAL_SMALL_MILLFORGE,
    )
    assert tuple(result.arm_id for result in run.execution_results) == (
        EvalTrialArmId.EVAL_SMALL_PI,
        EvalTrialArmId.EVAL_SMALL_MILLFORGE,
    )
    assert set(run.trial_record.final_outcomes.values()) == {expected_outcome}
    assert run.trial_record.record_hash == calculate_eval_trial_record_hash(
        run.trial_record
    )
    serialized_record = json.loads(
        canonical_eval_trial_record_bytes(run.trial_record).decode("ascii")
    )
    assert serialized_record["campaign_id"] == plan.campaign_manifest.campaign_id
    assert serialized_record["task_fixture_id"] == fixture.fixture_id
    assert serialized_record["task_category"] == fixture.category.value
    assert serialized_record["trial_index"] == plan.trial_index
    assert serialized_record["arm"] == plan.arm_order[0].value
    assert serialized_record["arm_order_index"] == 0
    assert serialized_record["seed_marker"] == str(plan.paired_seed)
    assert serialized_record["model_manifest_hash"] == (
        plan.campaign_manifest.model_manifest_hash
    )
    assert serialized_record["workflow_graph_hash"] == (
        plan.campaign_manifest.workflow_graph_hash
    )
    assert serialized_record["fixture_pack_hash"] == (
        plan.fixture_instance.fixture_pack_hash
    )
    assert serialized_record["fixture_instance_id"] == (
        plan.fixture_instance.fixture_instance_id
    )
    assert serialized_record["fixture_snapshot_hash"] == (
        plan.fixture_instance.fixture_snapshot_hash
    )
    assert serialized_record["started_at"] == plan.created_at
    assert serialized_record["ended_at"] == plan.created_at
    assert set(serialized_record["runner_summaries"]) == {
        EvalTrialArmId.EVAL_SMALL_PI.value,
        EvalTrialArmId.EVAL_SMALL_MILLFORGE.value,
    }
    assert serialized_record["resource_summary"]["zero_external_usage"] is True
    assert serialized_record["model_usage_summary"]["zero_model_usage"] is True
    assert "compiled_harness_hashes" in serialized_record
    assert "artifact_manifest_hashes" in serialized_record
    assert "scorer_public_summaries" in serialized_record
    assert "scorer_result_hashes" in serialized_record
    assert calculate_eval_trial_record_hash(
        run.trial_record.model_copy(
            update={
                "fixture_snapshot_hash": "f" * 64,
                "record_hash": "0" * 64,
            }
        )
    ) != run.trial_record.record_hash
    if expected_outcome is EvalTrialOutcome.INVALID_TRIAL:
        assert set(serialized_record["invalid_trial_explanations"]) == {
            EvalTrialArmId.EVAL_SMALL_PI.value,
            EvalTrialArmId.EVAL_SMALL_MILLFORGE.value,
        }
        assert all(
            explanation
            for explanation in serialized_record["invalid_trial_explanations"].values()
        )
    record_text = json.dumps(serialized_record, sort_keys=True)
    assert "scorer_only_diagnostics" not in record_text
    assert "hidden_checks" not in record_text
    assert "/mnt/f" not in record_text
    for bundle, result in zip(run.artifact_bundles, run.execution_results):
        planned_root = {
            arm_plan.arm_id: arm_plan.artifact_root for arm_plan in plan.arm_plans
        }[bundle.arm_id]
        assert bundle.artifact_root == planned_root
        assert bundle.zero_model_usage is True
        assert bundle.zero_external_usage is True
        assert bundle.artifact_bundle_hash == (
            calculate_eval_fake_runner_artifact_bundle_hash(bundle)
        )
        assert result.scorer_result.final_outcome is expected_outcome
        assert result.scorer_input.capability_audit.capability_violation is False
        assert result.scorer_input.public_artifact_hashes == bundle.artifact_hashes
        assert result.scorer_result.false_success is expected_false_success
        rendered = canonical_eval_fake_runner_artifact_bundle_bytes(bundle).decode(
            "ascii"
        )
        assert "hidden_checks" not in rendered
        assert "scorer_rubric" not in rendered
        assert "/mnt/f" not in rendered


def test_offline_fake_runner_refuses_live_execution_paths() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.live-denial.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )

    denial_cases = (
        {"execution_mode": EvalSuiteExecutionMode.LIVE_RUNNER},
        {"live_execution_admitted": True},
        {"allow_live_model_call": True},
        {"allow_pi_execution": True},
        {"allow_millforge_harness_execution": True},
    )
    for kwargs in denial_cases:
        with pytest.raises(ValueError, match="rejects"):
            run_offline_fake_eval_trial(plan, fixture=fixture, **kwargs)


def test_offline_fake_runner_requires_planned_fixture_identity() -> None:
    fixture, other_fixture = load_eval_task_fixtures()[:2]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.fixture-denial.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )

    with pytest.raises(ValueError, match="fixture_id"):
        run_offline_fake_eval_trial(plan, fixture=other_fixture)


def test_trial_records_fail_closed_for_stale_treatment_and_fake_pi_hashes() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.validation.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    run = run_offline_fake_eval_trial(plan, fixture=fixture)
    payload = dict(run.trial_record.__dict__)
    payload["compiled_harness_hashes"] = dict(run.trial_record.compiled_harness_hashes)
    payload["compiled_harness_hashes"]["eval_builder"] = "f" * 64
    payload["record_hash"] = "0" * 64

    with pytest.raises(ValidationError, match="current Spec 07E readiness"):
        EvalTrialRecord(**payload)

    for field_name, malformed_value in (
        ("model_manifest_hash", "not-a-sha"),
        ("workflow_graph_hash", "not-a-sha"),
        ("fixture_pack_hash", "not-a-sha"),
        ("fixture_snapshot_hash", "not-a-sha"),
        ("started_at", "2026-06-24T00:00:00+00:00"),
        ("ended_at", "2026-06-24T00:00:00+00:00"),
    ):
        malformed_payload = dict(run.trial_record.__dict__)
        malformed_payload[field_name] = malformed_value
        malformed_payload["record_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            EvalTrialRecord(**malformed_payload)

    with pytest.raises(ValidationError, match="fake concrete hash"):
        eval_trials.EvalTrialRunnerRecordSummary(
            arm_id=EvalTrialArmId.EVAL_SMALL_PI,
            runner_kind="pi_runtime",
            runner_descriptor_id="pi.runtime.public.v1",
            runner_descriptor_version="eval-trial.runner-descriptor.v1",
            baseline_pi_runtime_hash="f" * 64,
            baseline_runtime_diagnostic=None,
        )


def test_trial_record_store_manifest_and_resume_index_are_hashable_and_append_only() -> (
    None
):
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.default.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    pi_bundle = _artifact_bundle(plan.plan_hash, EvalTrialArmId.EVAL_SMALL_PI)
    millforge_bundle = _artifact_bundle(
        plan.plan_hash, EvalTrialArmId.EVAL_SMALL_MILLFORGE
    )
    pi_result = _execution_result(
        trial_id="trial.08b.fixture1.v1",
        plan_hash=plan.plan_hash,
        fixture_hash=fixture.fixture_hash,
        arm_id=EvalTrialArmId.EVAL_SMALL_PI,
        bundle_hash=pi_bundle.artifact_bundle_hash,
    )
    millforge_result = _execution_result(
        trial_id="trial.08b.fixture1.v1",
        plan_hash=plan.plan_hash,
        fixture_hash=fixture.fixture_hash,
        arm_id=EvalTrialArmId.EVAL_SMALL_MILLFORGE,
        bundle_hash=millforge_bundle.artifact_bundle_hash,
    )
    record = _trial_record(
        plan,
        fixture,
        pi_bundle,
        millforge_bundle,
        pi_result,
        millforge_result,
    )
    store = _store_manifest(
        plan.campaign_manifest.campaign_manifest_hash, record.record_hash
    )
    resume = _resume_index(
        plan.campaign_manifest.campaign_manifest_hash, record.record_hash
    )

    assert record.record_hash_kind == EVAL_TRIAL_RECORD_HASH_KIND
    assert record.record_hash == calculate_eval_trial_record_hash(record)
    assert store.store_manifest_hash_kind == EVAL_TRIAL_STORE_MANIFEST_HASH_KIND
    assert store.store_manifest_hash == calculate_eval_trial_store_manifest_hash(store)
    assert resume.resume_index_hash_kind == EVAL_TRIAL_RESUME_INDEX_HASH_KIND
    assert resume.resume_index_hash == calculate_eval_trial_resume_index_hash(resume)
    assert canonical_eval_trial_record_bytes(record).isascii()
    assert canonical_eval_trial_store_manifest_bytes(store).isascii()
    assert canonical_eval_trial_resume_index_bytes(resume).isascii()

    with pytest.raises(TypeError, match="immutable"):
        record.final_outcomes[EvalTrialArmId.EVAL_SMALL_PI] = (  # type: ignore[index]
            EvalTrialOutcome.FALSE_CLOSURE
        )
    with pytest.raises(ValidationError, match="append-only"):
        EvalTrialStoreManifest.model_validate(
            store.model_dump(mode="json")
            | {"append_only": False, "store_manifest_hash": "0" * 64}
        )


def test_campaign_store_appends_records_and_rejects_duplicates(tmp_path) -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.store-append.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    record = run_offline_fake_eval_trial(plan, fixture=fixture).trial_record

    result = append_eval_trial_record_to_campaign_store(
        tmp_path,
        plan=plan,
        record=record,
    )
    campaign_dir = tmp_path / plan.campaign_store_root
    trials_path = campaign_dir / "trials.jsonl"

    assert result.completed_trial_ids == (record.trial_id,)
    assert (campaign_dir / "manifest.json").read_bytes() == (
        canonical_eval_trial_store_manifest_bytes(result.manifest)
    )
    stored_plan = json.loads((campaign_dir / "plan.json").read_text(encoding="ascii"))
    assert stored_plan["campaign_store_root"] == plan.campaign_store_root
    assert stored_plan["plan_hashes"] == [plan.plan_hash]
    assert trials_path.read_bytes() == canonical_eval_trial_record_bytes(record)
    assert trials_path.read_bytes().endswith(b"\n")
    assert (campaign_dir / "artifacts").is_dir()
    serialized = json.loads(trials_path.read_text(encoding="ascii"))
    assert serialized["fixture_instance_id"] == plan.fixture_instance.fixture_instance_id
    assert serialized["fixture_snapshot_hash"] == (
        plan.fixture_instance.fixture_snapshot_hash
    )
    assert serialized["started_at"] == plan.created_at
    assert serialized["ended_at"] == plan.created_at
    assert set(serialized["artifact_roots"]) == {
        EvalTrialArmId.EVAL_SMALL_PI.value,
        EvalTrialArmId.EVAL_SMALL_MILLFORGE.value,
    }
    for artifact_root in serialized["artifact_roots"].values():
        assert artifact_root.startswith("artifacts/")
        assert not artifact_root.startswith("/")

    before = trials_path.read_bytes()
    with pytest.raises(ValueError, match="duplicate trial IDs"):
        append_eval_trial_record_to_campaign_store(tmp_path, plan=plan, record=record)
    assert trials_path.read_bytes() == before


def test_campaign_store_rejects_records_with_plan_mismatched_direct_fields(
    tmp_path,
) -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.direct-field-denial.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    record = run_offline_fake_eval_trial(plan, fixture=fixture).trial_record

    for field_name, mismatch_value in (
        ("model_manifest_hash", "f" * 64),
        ("workflow_graph_hash", "f" * 64),
        ("fixture_pack_hash", "f" * 64),
        ("fixture_instance_id", "fixture.instance.mismatch.v1"),
        ("fixture_snapshot_hash", "f" * 64),
        ("started_at", "1969-12-31T23:59:59Z"),
        ("ended_at", "1970-01-01T00:00:01Z"),
    ):
        draft = record.model_copy(
            update={field_name: mismatch_value, "record_hash": "0" * 64}
        )
        mismatched = EvalTrialRecord.model_validate(
            draft.model_copy(
                update={"record_hash": calculate_eval_trial_record_hash(draft)}
            )
        )
        with pytest.raises(ValueError, match=field_name):
            append_eval_trial_record_to_campaign_store(
                tmp_path,
                plan=plan,
                record=mismatched,
            )


def test_campaign_store_appends_plan_set_records_in_order(tmp_path) -> None:
    fixtures = load_eval_task_fixtures()[:2]
    plans = plan_paired_eval_trials(
        fixtures=fixtures,
        fake_runner_script=_fake_script(),
        seed=17,
    )
    first_record = run_offline_fake_eval_trial(
        plans[0],
        fixture=fixtures[0],
    ).trial_record
    second_record = run_offline_fake_eval_trial(
        plans[1],
        fixture=fixtures[1],
    ).trial_record
    campaign_dir = tmp_path / plans[0].campaign_store_root
    trials_path = campaign_dir / "trials.jsonl"
    plan_path = campaign_dir / "plan.json"

    first_result = append_eval_trial_record_to_campaign_store(
        tmp_path,
        plan=plans[0],
        record=first_record,
        plans=plans,
    )
    first_record_bytes = canonical_eval_trial_record_bytes(first_record)
    assert trials_path.read_bytes() == first_record_bytes
    plan_bytes_after_first = plan_path.read_bytes()
    assert first_result.resume_index.pending_trial_plan_hashes == (plans[1].plan_hash,)

    second_result = append_eval_trial_record_to_campaign_store(
        tmp_path,
        plan=plans[1],
        record=second_record,
        plans=plans,
    )
    second_record_bytes = canonical_eval_trial_record_bytes(second_record)

    assert trials_path.read_bytes() == first_record_bytes + second_record_bytes
    assert trials_path.read_bytes().count(b"\n") == 2
    assert plan_path.read_bytes() == plan_bytes_after_first
    assert second_result.completed_trial_ids == (
        first_record.trial_id,
        second_record.trial_id,
    )
    stored_plan = json.loads(plan_path.read_text(encoding="ascii"))
    assert stored_plan["plan_hashes"] == [plans[0].plan_hash, plans[1].plan_hash]


def test_campaign_store_rejects_immutable_manifest_and_plan_mismatch(tmp_path) -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.store-immutable.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    record = run_offline_fake_eval_trial(plan, fixture=fixture).trial_record
    append_eval_trial_record_to_campaign_store(tmp_path, plan=plan, record=record)
    campaign_dir = tmp_path / plan.campaign_store_root

    plan_path = campaign_dir / "plan.json"
    original_plan_bytes = plan_path.read_bytes()
    plan_path.write_bytes(b'{"different":true}\n')
    with pytest.raises(ValueError, match="plan mismatch"):
        append_eval_trial_record_to_campaign_store(tmp_path, plan=plan, record=record)
    assert plan_path.read_bytes() == b'{"different":true}\n'
    plan_path.write_bytes(original_plan_bytes)

    manifest_path = campaign_dir / "manifest.json"
    manifest_path.write_bytes(b'{"different":true}\n')
    with pytest.raises(ValueError, match="manifest mismatch"):
        append_eval_trial_record_to_campaign_store(tmp_path, plan=plan, record=record)
    assert manifest_path.read_bytes() == b'{"different":true}\n'


def test_campaign_store_resume_reconstructs_completed_and_pending(tmp_path) -> None:
    fixtures = load_eval_task_fixtures()[:2]
    plans = plan_paired_eval_trials(
        fixtures=fixtures,
        fake_runner_script=_fake_script(),
        seed=12,
    )
    first_record = run_offline_fake_eval_trial(
        plans[0],
        fixture=fixtures[0],
    ).trial_record
    append_eval_trial_record_to_campaign_store(
        tmp_path,
        plan=plans[0],
        record=first_record,
        plans=plans,
    )

    result = resume_eval_trial_campaign_store(
        tmp_path,
        plan=plans[0],
        plans=plans,
    )

    assert result.diagnostics == ()
    assert result.completed_trial_ids == (plans[0].trial_id,)
    assert result.pending_trial_ids == (plans[1].trial_id,)
    assert tuple(record.record_hash for record in result.records) == (
        first_record.record_hash,
    )
    assert result.resume_index is not None
    assert result.resume_index.completed_trial_record_hashes == (
        first_record.record_hash,
    )
    assert result.resume_index.pending_trial_plan_hashes == (plans[1].plan_hash,)
    assert (tmp_path / plans[0].campaign_store_root / "index.json").read_bytes() == (
        canonical_eval_trial_resume_index_bytes(result.resume_index)
    )


def test_campaign_store_resume_writes_plan_set_on_empty_store(tmp_path) -> None:
    fixtures = load_eval_task_fixtures()[:2]
    plans = plan_paired_eval_trials(
        fixtures=fixtures,
        fake_runner_script=_fake_script(),
        seed=19,
    )

    result = resume_eval_trial_campaign_store(tmp_path, plan=plans[0], plans=plans)

    campaign_dir = tmp_path / plans[0].campaign_store_root
    stored_plan = json.loads((campaign_dir / "plan.json").read_text(encoding="ascii"))
    assert result.diagnostics == ()
    assert result.completed_trial_ids == ()
    assert result.pending_trial_ids == (plans[0].trial_id, plans[1].trial_id)
    assert stored_plan["plan_hashes"] == [plans[0].plan_hash, plans[1].plan_hash]


@pytest.mark.parametrize(
    ("mutate_log", "expected_code"),
    (
        (
            lambda original: original.rstrip(b"\n"),
            eval_trials.EvalTrialInvalidDiagnosticCode.TRIAL_LOG_MISSING_FINAL_NEWLINE,
        ),
        (
            lambda original: original + b'{"trial_id":',
            eval_trials.EvalTrialInvalidDiagnosticCode.TRIAL_LOG_PARTIAL_TRAILING_RECORD,
        ),
        (
            lambda original: original + b"not-json\n",
            eval_trials.EvalTrialInvalidDiagnosticCode.TRIAL_LOG_MALFORMED_TRAILING_LINE,
        ),
    ),
)
def test_campaign_store_resume_diagnoses_malformed_logs_without_mutation(
    tmp_path,
    mutate_log,
    expected_code,
) -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.store-diagnostic.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    record = run_offline_fake_eval_trial(plan, fixture=fixture).trial_record
    append_eval_trial_record_to_campaign_store(tmp_path, plan=plan, record=record)
    clean_resume = resume_eval_trial_campaign_store(tmp_path, plan=plan)
    assert clean_resume.diagnostics == ()
    campaign_dir = tmp_path / plan.campaign_store_root
    trials_path = campaign_dir / "trials.jsonl"
    index_path = campaign_dir / "index.json"
    trials_path.write_bytes(mutate_log(trials_path.read_bytes()))
    before_trials = trials_path.read_bytes()
    before_index = index_path.read_bytes()

    result = resume_eval_trial_campaign_store(tmp_path, plan=plan)

    assert expected_code in {
        diagnostic.diagnostic_code for diagnostic in result.diagnostics
    }
    assert trials_path.read_bytes() == before_trials
    assert index_path.read_bytes() == before_index


def test_campaign_store_resume_diagnoses_duplicate_trial_ids_without_rewrite(
    tmp_path,
) -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.store-duplicate-resume.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    record = run_offline_fake_eval_trial(plan, fixture=fixture).trial_record
    append_eval_trial_record_to_campaign_store(tmp_path, plan=plan, record=record)
    clean_resume = resume_eval_trial_campaign_store(tmp_path, plan=plan)
    assert clean_resume.diagnostics == ()
    campaign_dir = tmp_path / plan.campaign_store_root
    trials_path = campaign_dir / "trials.jsonl"
    index_path = campaign_dir / "index.json"
    trials_path.write_bytes(
        canonical_eval_trial_record_bytes(record)
        + canonical_eval_trial_record_bytes(record)
    )
    before_trials = trials_path.read_bytes()
    before_index = index_path.read_bytes()

    result = resume_eval_trial_campaign_store(tmp_path, plan=plan)

    assert {diagnostic.diagnostic_code for diagnostic in result.diagnostics} == {
        eval_trials.EvalTrialInvalidDiagnosticCode.TRIAL_LOG_DUPLICATE_TRIAL_ID
    }
    assert trials_path.read_bytes() == before_trials
    assert index_path.read_bytes() == before_index


def test_execution_result_rejects_live_admission() -> None:
    result = _execution_result(
        trial_id="trial.08b.fixture1.v1",
        plan_hash="a" * 64,
        fixture_hash="b" * 64,
        arm_id=EvalTrialArmId.EVAL_SMALL_PI,
        bundle_hash="c" * 64,
    )

    with pytest.raises(ValidationError, match="do not admit live execution"):
        EvalTrialExecutionResult.model_validate(
            result.model_dump(mode="json") | {"live_execution_admitted": True}
        )


def test_canonical_trial_payloads_are_ascii_and_private_material_free() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08b.default.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    rendered = canonical_eval_trial_plan_bytes(plan).decode("ascii")

    json.loads(rendered)
    assert "millrace-agents" not in rendered
    assert "/mnt/f" not in rendered
    assert "api_key" not in rendered
    assert "endpoint_url" not in rendered
    assert "hidden_checks" not in rendered
    assert "scorer_rubric" not in rendered


def _fake_script() -> EvalTrialFakeRunnerScript:
    return EvalTrialFakeRunnerScript(
        script_id="fake.valid_completion.v1",
        script_kind=EvalFakeOutcomeScriptKind.VALID_COMPLETION,
        terminal_results=(
            EvalTerminalResult.PLAN_READY,
            EvalTerminalResult.BUILDER_COMPLETE,
            EvalTerminalResult.CHECKER_APPROVED,
            EvalTerminalResult.ARBITER_CLOSED,
        ),
        expected_outcome=EvalTrialOutcome.VALID_COMPLETION,
        stage_result_summaries={
            "eval_planner": "plan ready",
            "eval_builder": "builder complete",
            "eval_checker": "checker approved",
            "eval_arbiter": "arbiter closed",
        },
    )


def _artifact_manifest() -> EvalArtifactManifestArtifact:
    return EvalArtifactManifestArtifact(
        artifact_id=EvalArtifactId.ARTIFACT_MANIFEST,
        trial_id="trial.08b.fixture1.v1",
        created_by="offline_fake_runner",
        summary="offline fake artifact manifest",
        entries=(
            EvalArtifactManifestEntry(
                artifact_id=EvalArtifactId.TASK,
                layout_path="trial/input/task.json",
                media_type="application/json",
                schema_id="eval_task_artifact_v1",
                byte_size=2,
                sha256="1" * 64,
                producer="offline_fake_runner",
            ),
        ),
    )


def _artifact_bundle(
    plan_hash: str, arm_id: EvalTrialArmId
) -> EvalFakeRunnerArtifactBundle:
    bundle = EvalFakeRunnerArtifactBundle.model_construct(
        artifact_bundle_id=f"bundle.{arm_id.value}.v1",
        trial_plan_hash=plan_hash,
        arm_id=arm_id,
        artifact_root=f"artifacts/trial.08b.fixture1.v1/{arm_id.value}",
        artifact_manifest=_artifact_manifest(),
        artifact_hashes=(
            EvalHashRecord(hash_kind="artifact_sha256_v1", sha256="1" * 64),
        ),
        zero_model_usage=True,
        zero_external_usage=True,
        artifact_bundle_hash_kind=EVAL_TRIAL_ARTIFACT_BUNDLE_HASH_KIND,
        artifact_bundle_hash="0" * 64,
    )
    return EvalFakeRunnerArtifactBundle.model_validate(
        bundle.model_copy(
            update={
                "artifact_bundle_hash": (
                    calculate_eval_fake_runner_artifact_bundle_hash(bundle)
                )
            }
        )
    )


def _rehash_plan_payload(
    plan: eval_trials.EvalTrialPlan,
    **updates: object,
) -> dict[str, object]:
    draft = plan.model_copy(update=updates | {"plan_hash": "0" * 64})
    payload = draft.model_dump(mode="json")
    payload["plan_hash"] = calculate_eval_trial_plan_hash(draft)
    return payload


def _scorer_input(trial_id: str, fixture_hash: str) -> EvalScorerInput:
    scorer_input = EvalScorerInput.model_construct(
        trial_id=trial_id,
        fixture_id="fixture.08b.v1",
        fixture_hash=fixture_hash,
        path_limited_workspace_hashes=(),
        public_artifact_hashes=(),
        required_public_artifact_ids=("task",),
        provided_public_artifact_ids=("task",),
        malformed_artifact_ids=(),
        stage_terminal_results=("ARBITER_CLOSED",),
        capability_audit=EvalCapabilityAuditSummary(
            capability_violation=False,
            summary="no capability violations",
        ),
        visible_check_results=(EvalCheckResult(check_id="visible", passed=True),),
        hidden_check_results=(EvalCheckResult(check_id="hidden", passed=True),),
        scorer_input_hash="0" * 64,
    )
    return EvalScorerInput.model_validate(
        scorer_input.model_copy(
            update={"scorer_input_hash": calculate_eval_scorer_input_hash(scorer_input)}
        )
    )


def _scorer_result(scorer_input: EvalScorerInput) -> EvalScorerResult:
    result = EvalScorerResult.model_construct(
        trial_id=scorer_input.trial_id,
        fixture_id=scorer_input.fixture_id,
        final_outcome=EvalTrialOutcome.VALID_COMPLETION,
        primary_success=True,
        false_closure=False,
        false_success=False,
        correctly_blocked=False,
        capability_violation=False,
        artifact_complete=True,
        scorer_version=EVAL_SUITE_DEFAULT_SCORER_VERSION,
        result_hash="0" * 64,
    )
    return EvalScorerResult.model_validate(
        result.model_copy(
            update={"result_hash": calculate_eval_scorer_result_hash(result)}
        )
    )


def _execution_result(
    *,
    trial_id: str,
    plan_hash: str,
    fixture_hash: str,
    arm_id: EvalTrialArmId,
    bundle_hash: str,
) -> EvalTrialExecutionResult:
    scorer_input = _scorer_input(trial_id, fixture_hash)
    return EvalTrialExecutionResult(
        trial_id=trial_id,
        trial_plan_hash=plan_hash,
        arm_id=arm_id,
        terminal_results=(EvalTerminalResult.ARBITER_CLOSED,),
        artifact_bundle_hash=bundle_hash,
        scorer_input=scorer_input,
        scorer_result=_scorer_result(scorer_input),
    )


def _trial_record(
    plan: eval_trials.EvalTrialPlan,
    fixture: EvalTaskFixture,
    pi_bundle: EvalFakeRunnerArtifactBundle,
    millforge_bundle: EvalFakeRunnerArtifactBundle,
    pi_result: EvalTrialExecutionResult,
    millforge_result: EvalTrialExecutionResult,
) -> EvalTrialRecord:
    ordered_results = (pi_result, millforge_result)
    ordered_bundles = (pi_bundle, millforge_bundle)
    record = EvalTrialRecord.model_construct(
        campaign_id=plan.campaign_manifest.campaign_id,
        task_fixture_id=plan.fixture_instance.fixture_id,
        task_category=fixture.category.value,
        trial_id=pi_result.trial_id,
        trial_index=plan.trial_index,
        arm=plan.arm_order[0],
        arm_order_index=0,
        arm_order=plan.arm_order,
        seed_marker=str(plan.paired_seed),
        trial_plan_hash=plan.plan_hash,
        campaign_manifest_hash=plan.campaign_manifest.campaign_manifest_hash,
        model_manifest_hash=plan.campaign_manifest.model_manifest_hash,
        workflow_graph_hash=plan.campaign_manifest.workflow_graph_hash,
        fixture_pack_hash=plan.fixture_instance.fixture_pack_hash,
        fixture_instance_id=plan.fixture_instance.fixture_instance_id,
        fixture_id=plan.fixture_instance.fixture_id,
        fixture_hash=fixture.fixture_hash,
        fixture_snapshot_hash=plan.fixture_instance.fixture_snapshot_hash,
        runner_summaries={
            arm_plan.arm_id: eval_trials.EvalTrialRunnerRecordSummary(
                arm_id=arm_plan.arm_id,
                runner_kind=arm_plan.runner_kind,
                runner_descriptor_id=arm_plan.runner_descriptor_id,
                runner_descriptor_version="eval-trial.runner-descriptor.v1",
                baseline_pi_runtime_hash=arm_plan.baseline_pi_runtime_hash,
                baseline_runtime_diagnostic=arm_plan.baseline_runtime_diagnostic,
            )
            for arm_plan in plan.arm_plans
        },
        compiled_harness_hashes={
            compiled.stage_id: compiled.compiled_sha256
            for compiled in plan.spec_07_readiness.compiled_plans
        },
        arm_results=ordered_results,
        final_outcomes={
            pi_result.arm_id: pi_result.scorer_result.final_outcome,
            millforge_result.arm_id: millforge_result.scorer_result.final_outcome,
        },
        scorer_result_hashes={
            result.arm_id: result.scorer_result.result_hash
            for result in ordered_results
        },
        scorer_public_summaries={
            result.arm_id: eval_trials.EvalTrialScorerPublicSummary(
                arm_id=result.arm_id,
                scorer_version=result.scorer_result.scorer_version,
                final_outcome=result.scorer_result.final_outcome,
                primary_success=result.scorer_result.primary_success,
                false_closure=result.scorer_result.false_closure,
                false_success=result.scorer_result.false_success,
                correctly_blocked=result.scorer_result.correctly_blocked,
                capability_violation=result.scorer_result.capability_violation,
                artifact_complete=result.scorer_result.artifact_complete,
                missing_artifact_ids=result.scorer_result.missing_artifact_ids,
                malformed_artifact_ids=result.scorer_result.malformed_artifact_ids,
                failure_labels=tuple(
                    label.value for label in result.scorer_result.failure_labels
                ),
                public_diagnostics=result.scorer_result.public_diagnostics,
                invalid_trial_explanation=(
                    result.scorer_result.invalid_trial_explanation
                ),
            )
            for result in ordered_results
        },
        artifact_manifest_hashes={
            bundle.arm_id: calculate_eval_artifact_manifest_sha256(
                bundle.artifact_manifest
            )
            for bundle in ordered_bundles
        },
        artifact_roots={
            bundle.arm_id: bundle.artifact_root for bundle in ordered_bundles
        },
        resource_summary=eval_trials.EvalTrialResourceSummary(
            artifact_count=sum(
                len(bundle.artifact_hashes) for bundle in ordered_bundles
            ),
            resource_artifact_hashes=tuple(
                artifact_hash
                for bundle in ordered_bundles
                for artifact_hash in bundle.artifact_hashes
            ),
            zero_external_usage=True,
        ),
        model_usage_summary=eval_trials.EvalTrialModelUsageSummary(),
        invalid_trial_explanations={
            result.arm_id: result.scorer_result.invalid_trial_explanation
            for result in ordered_results
            if result.scorer_result.invalid_trial_explanation
        },
        started_at=plan.created_at,
        ended_at=plan.created_at,
        record_hash_kind=EVAL_TRIAL_RECORD_HASH_KIND,
        record_hash="0" * 64,
    )
    return EvalTrialRecord.model_validate(
        record.model_copy(
            update={"record_hash": calculate_eval_trial_record_hash(record)}
        )
    )


def _store_manifest(campaign_hash: str, record_hash: str) -> EvalTrialStoreManifest:
    manifest = EvalTrialStoreManifest.model_construct(
        store_manifest_id="store.08b.v1",
        campaign_manifest_hash=campaign_hash,
        record_hashes=(record_hash,),
        append_only=True,
        store_manifest_hash_kind=EVAL_TRIAL_STORE_MANIFEST_HASH_KIND,
        store_manifest_hash="0" * 64,
    )
    return EvalTrialStoreManifest.model_validate(
        manifest.model_copy(
            update={
                "store_manifest_hash": calculate_eval_trial_store_manifest_hash(
                    manifest
                )
            }
        )
    )


def _resume_index(campaign_hash: str, record_hash: str) -> EvalTrialResumeIndex:
    index = EvalTrialResumeIndex.model_construct(
        resume_index_id="resume.08b.v1",
        campaign_manifest_hash=campaign_hash,
        completed_trial_record_hashes=(record_hash,),
        pending_trial_plan_hashes=(),
        invalid_trial_diagnostics=(),
        resume_index_hash_kind=EVAL_TRIAL_RESUME_INDEX_HASH_KIND,
        resume_index_hash="0" * 64,
    )
    return EvalTrialResumeIndex.model_validate(
        index.model_copy(
            update={"resume_index_hash": calculate_eval_trial_resume_index_hash(index)}
        )
    )
