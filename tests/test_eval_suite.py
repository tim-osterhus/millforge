"""Focused tests for the public 08A eval-suite contract surface."""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

import millforge
import millforge.eval_suite as eval_suite
from millforge.eval_modes import (
    EVAL_DEFAULT_MODEL_PROFILE_ID,
    calculate_eval_mode_fairness_fingerprint,
    default_eval_small_millforge_mode,
    default_eval_small_pi_mode,
)
from millforge.eval_suite import (
    EVAL_SUITE_DEFAULT_CAMPAIGN_CREATED_AT,
    EVAL_SUITE_DEFAULT_CAMPAIGN_ID,
    EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID,
    EVAL_SUITE_DEFAULT_SCORER_VERSION,
    EVAL_SUITE_MODEL_MANIFEST_HASH_KIND,
    EvalCampaignManifest,
    EvalCampaignKind,
    EvalCapabilityAuditSummary,
    EvalCheckResult,
    EvalDifficultyLevel,
    EvalDifficultyMetadata,
    EvalExpectedMutationKind,
    EvalExpectedMutationPolicy,
    EvalFailureTaxonomyLabel,
    EvalFixturePackSummary,
    EvalHashRecord,
    EvalHiddenCheck,
    EvalModelManifest,
    EvalModelPricingMetadata,
    EvalModelRateLimitMetadata,
    EvalPublicArtifactProjection,
    EvalRunnerAcceptanceProjection,
    EvalRunnerContextProjection,
    EvalRunnerTaskProjection,
    EvalScorerInput,
    EvalScorerResult,
    EvalSuiteExecutionMode,
    EvalTaskCategory,
    EvalTaskFixture,
    EvalTrialOutcome,
    EvalVisibleCheck,
    calculate_eval_campaign_manifest_hash,
    calculate_eval_fixture_pack_hash,
    calculate_eval_model_manifest_hash,
    calculate_eval_scorer_input_hash,
    calculate_eval_scorer_result_hash,
    calculate_eval_task_fixture_hash,
    canonical_eval_suite_bytes,
    default_eval_suite_campaign_manifest,
    eval_model_manifest_from_profile,
    eval_public_artifact_projection,
    eval_runner_acceptance_projection,
    eval_runner_context_projection,
    eval_runner_task_projection,
    load_eval_fixture_pack_summary,
    load_eval_task_fixture,
    load_eval_task_fixtures,
    score_eval_trial,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_eval_suite_public_contracts_are_root_exports() -> None:
    for public_name in eval_suite.__all__:
        assert public_name in millforge.__all__
        assert getattr(millforge, public_name) is getattr(eval_suite, public_name)


def test_default_campaign_is_offline_only_and_reuses_shared_model_profile() -> None:
    model_manifest = eval_model_manifest_from_profile()
    campaign = default_eval_suite_campaign_manifest(model_manifest=model_manifest)
    fixture_pack_summary = load_eval_fixture_pack_summary()

    assert campaign.campaign_id == EVAL_SUITE_DEFAULT_CAMPAIGN_ID
    assert campaign.created_at == EVAL_SUITE_DEFAULT_CAMPAIGN_CREATED_AT
    assert campaign.campaign_kind == EvalCampaignKind.LOCAL_OPENAI_COMPATIBLE
    assert campaign.execution_mode == EvalSuiteExecutionMode.OFFLINE_FAKE
    assert campaign.live_execution_admitted is False
    assert campaign.live_denial_diagnostics
    assert campaign.pi_eval_mode_id == "eval_small_pi"
    assert campaign.millforge_eval_mode_id == "eval_small_millforge"
    assert model_manifest.model_profile_id == EVAL_DEFAULT_MODEL_PROFILE_ID
    assert campaign.model_manifest_hash == model_manifest.model_manifest_hash
    assert (
        campaign.fixture_pack_hash
        == "9a9f9721ebacc8a323a4722e5a77c6c7c1ec58927b9d68ad25f7387dad30e342"
    )
    assert campaign.fixture_pack_hash == fixture_pack_summary.fixture_pack_hash
    assert campaign.campaign_manifest_hash == calculate_eval_campaign_manifest_hash(
        campaign
    )


def test_default_campaign_canonical_bytes_and_hash_are_stable_by_default() -> None:
    first = default_eval_suite_campaign_manifest()
    second = default_eval_suite_campaign_manifest()

    assert canonical_eval_suite_bytes(first) == canonical_eval_suite_bytes(second)
    assert first.campaign_manifest_hash == second.campaign_manifest_hash
    assert first.created_at == EVAL_SUITE_DEFAULT_CAMPAIGN_CREATED_AT


def test_default_campaign_accepts_fixed_timestamp_and_fixture_pack_hash_override() -> None:
    campaign = default_eval_suite_campaign_manifest(
        created_at="2026-06-24T00:00:00Z",
        fixture_pack_hash="1" * 64,
    )

    assert campaign.created_at == "2026-06-24T00:00:00Z"
    assert campaign.fixture_pack_hash == "1" * 64
    assert campaign.campaign_manifest_hash == calculate_eval_campaign_manifest_hash(
        campaign
    )


def test_default_campaign_validates_explicit_fixture_pack_hash_override() -> None:
    with pytest.raises(ValidationError, match="sha256"):
        default_eval_suite_campaign_manifest(fixture_pack_hash="")


def test_default_campaign_does_not_change_eval_mode_fairness_fingerprints() -> None:
    pi_before = default_eval_small_pi_mode()
    millforge_before = default_eval_small_millforge_mode()
    default_eval_suite_campaign_manifest()
    pi_after = default_eval_small_pi_mode()
    millforge_after = default_eval_small_millforge_mode()

    assert calculate_eval_mode_fairness_fingerprint(
        pi_after
    ) == calculate_eval_mode_fairness_fingerprint(pi_before)
    assert calculate_eval_mode_fairness_fingerprint(
        millforge_after
    ) == calculate_eval_mode_fairness_fingerprint(millforge_before)


def test_model_manifest_hash_and_canonical_bytes_are_stable_ascii() -> None:
    manifest = eval_model_manifest_from_profile()

    assert manifest.model_manifest_hash == calculate_eval_model_manifest_hash(manifest)
    assert manifest.model_manifest_hash_kind == EVAL_SUITE_MODEL_MANIFEST_HASH_KIND
    assert canonical_eval_suite_bytes(manifest).decode("ascii").endswith("\n")
    assert canonical_eval_suite_bytes(manifest) == canonical_eval_suite_bytes(
        eval_model_manifest_from_profile()
    )


def test_model_manifest_exposes_numeric_public_model_metadata() -> None:
    manifest = eval_model_manifest_from_profile()
    payload = manifest.model_dump(mode="json")

    assert isinstance(manifest.public_pricing, EvalModelPricingMetadata)
    assert isinstance(manifest.public_rate_limits, EvalModelRateLimitMetadata)
    assert payload["public_pricing"] == {
        "cached_input_cost_per_million_tokens": 0.0,
        "currency_label": "none",
        "input_cost_per_million_tokens": 0.0,
        "output_cost_per_million_tokens": 0.0,
        "source_label": "static_descriptor",
    }
    assert payload["public_rate_limits"] == {
        "concurrent_request_limit": 0,
        "request_rate_per_window": 0,
        "source_label": "not_applicable",
        "token_rate_per_window": 0,
        "window_seconds": 0,
    }
    assert all(isinstance(value, str) for value in manifest.public_pricing_snapshot.values())
    assert all(
        isinstance(value, str)
        for value in manifest.public_rate_limit_snapshot.values()
    )
    assert manifest.model_manifest_hash == calculate_eval_model_manifest_hash(manifest)


def test_model_manifest_rejects_nonnumeric_public_model_metadata() -> None:
    payload = eval_model_manifest_from_profile().model_dump(mode="json")

    with pytest.raises(ValidationError, match="float_type|float_parsing"):
        EvalModelManifest.model_validate(
            payload
            | {
                "public_pricing": payload["public_pricing"]
                | {"input_cost_per_million_tokens": "free"}
            }
        )

    with pytest.raises(ValidationError, match="int_type|int_parsing"):
        EvalModelManifest.model_validate(
            payload
            | {
                "public_rate_limits": payload["public_rate_limits"]
                | {"request_rate_per_window": "not_applicable"}
            }
        )


def test_model_and_campaign_validation_reject_private_material() -> None:
    model_payload = eval_model_manifest_from_profile().model_dump(mode="json")

    with pytest.raises(
        ValidationError,
        match="forbidden private material|secret-like field name|extra_forbidden",
    ):
        EvalModelManifest.model_validate(
            model_payload | {"endpoint_url": "https://provider.example/v1"}
        )

    with pytest.raises(ValidationError, match="endpoint URLs|forbidden"):
        EvalModelManifest.model_validate(
            model_payload
            | {
                "public_rate_limit_snapshot": {
                    "source": "https://provider.example/limits"
                }
            }
        )

    with pytest.raises(ValidationError, match="host paths"):
        EvalModelManifest.model_validate(
            model_payload | {"release_or_snapshot": "/home/user/model.bin"}
        )

    with pytest.raises(ValidationError, match="max_total_tokens"):
        EvalModelManifest.model_validate(model_payload | {"max_total_tokens": 1})

    campaign_payload = default_eval_suite_campaign_manifest().model_dump(mode="json")
    with pytest.raises(ValidationError, match="live execution"):
        EvalCampaignManifest.model_validate(
            campaign_payload
            | {
                "execution_mode": "live_runner",
                "live_execution_admitted": True,
                "live_denial_diagnostics": (),
            }
        )


@pytest.mark.parametrize(
    "provider_label",
    ("sk-live-abc1234567890", "sk-proj-abc1234567890", "AKIAIOSFODNN7EXAMPLE"),
)
def test_model_manifest_rejects_api_key_values_with_recomputed_hash(
    provider_label: str,
) -> None:
    payload = _model_manifest_payload_with_recomputed_hash(
        provider_label=provider_label
    )

    with pytest.raises(ValidationError, match="credential-shaped API key"):
        EvalModelManifest.model_validate(payload)


def test_model_manifest_mapping_fields_are_immutable() -> None:
    manifest = eval_model_manifest_from_profile()

    for mapping in (
        manifest.reasoning_controls,
        manifest.public_pricing_snapshot,
        manifest.public_rate_limit_snapshot,
        manifest.local_serving_snapshot,
    ):
        with pytest.raises(TypeError, match="immutable"):
            mapping["new"] = "value"  # type: ignore[index]

    assert manifest.model_manifest_hash == calculate_eval_model_manifest_hash(manifest)
    assert canonical_eval_suite_bytes(manifest) == canonical_eval_suite_bytes(
        eval_model_manifest_from_profile()
    )


def test_fixture_pack_category_counts_are_immutable_and_hashable() -> None:
    summary = _valid_fixture_pack_summary()
    before = canonical_eval_suite_bytes(summary)

    with pytest.raises(TypeError, match="immutable"):
        summary.category_counts[EvalTaskCategory.BUG_DIAGNOSIS] = 1  # type: ignore[index]

    assert summary.fixture_pack_hash == calculate_eval_fixture_pack_hash(summary)
    assert canonical_eval_suite_bytes(summary) == before


def test_fixture_contract_hashes_and_runner_projection_hide_scorer_fields() -> None:
    fixture = _valid_fixture()
    projection = eval_runner_task_projection(fixture)
    projection_json = json.dumps(projection.model_dump(mode="json"), sort_keys=True)

    assert fixture.fixture_hash == calculate_eval_task_fixture_hash(fixture)
    assert "hidden_checks" not in type(projection).model_fields
    assert "expected_mutation_policy" not in type(projection).model_fields
    assert "expected_final_outcome" not in type(projection).model_fields
    assert "hidden" not in projection_json.lower()
    assert "scorer" not in projection_json.lower()
    assert projection.visible_prompt == fixture.visible_prompt


def test_packaged_fixture_pack_loads_with_required_coverage_and_stable_hashes() -> None:
    first_load = load_eval_task_fixtures()
    second_load = load_eval_task_fixtures()
    summary = load_eval_fixture_pack_summary()
    repeated_summary = load_eval_fixture_pack_summary()

    assert len(first_load) >= 6
    assert {fixture.category for fixture in first_load} == set(EvalTaskCategory)
    assert tuple(fixture.fixture_hash for fixture in first_load) == tuple(
        fixture.fixture_hash for fixture in second_load
    )
    assert summary.fixture_pack_id == EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID
    assert summary.fixture_pack_hash == repeated_summary.fixture_pack_hash
    assert summary.fixture_pack_hash == calculate_eval_fixture_pack_hash(summary)
    assert summary.fixture_ids == tuple(fixture.fixture_id for fixture in first_load)
    assert tuple(record.sha256 for record in summary.fixture_hashes) == tuple(
        fixture.fixture_hash for fixture in first_load
    )

    insufficient_visible_only = [
        fixture
        for fixture in first_load
        if any(
            "withheld" in check.scorer_rubric.lower()
            or "visible checks alone are insufficient" in check.scorer_rubric.lower()
            or "partial implementation" in check.scorer_rubric.lower()
            for check in fixture.hidden_checks
        )
    ]
    assert len(insufficient_visible_only) >= 2
    assert any(
        fixture.expected_mutation_policy.mutation_kind
        == EvalExpectedMutationKind.NO_SOURCE_CHANGE
        for fixture in first_load
    )
    assert any(
        "missing or malformed public artifacts fail" in check.summary.lower()
        for fixture in first_load
        for check in fixture.hidden_checks
    )
    for fixture in first_load:
        assert load_eval_task_fixture(fixture.fixture_id).fixture_hash == (
            fixture.fixture_hash
        )
        assert canonical_eval_suite_bytes(fixture) == canonical_eval_suite_bytes(
            load_eval_task_fixture(fixture.fixture_id)
        )


def test_packaged_fixture_pack_data_is_present_in_wheel_and_sdist(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(out_dir)],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wheel_path = next(out_dir.glob("millforge-*.whl"))
    sdist_path = next(out_dir.glob("millforge-*.tar.gz"))
    fixtures = load_eval_task_fixtures()

    expected_wheel_names = {
        "millforge/eval_fixtures/__init__.py",
        "millforge/eval_fixtures/default_pack/__init__.py",
        "millforge/eval_fixtures/default_pack/manifest.json",
        *{
            f"millforge/eval_fixtures/default_pack/fixtures/{fixture.fixture_id}.json"
            for fixture in fixtures
        },
    }
    expected_sdist_names = {
        "src/" + name for name in expected_wheel_names if name.startswith("millforge/")
    }

    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_names = set(wheel.namelist())
    with tarfile.open(sdist_path) as sdist:
        sdist_names = {
            "/".join(Path(name).parts[1:])
            for name in sdist.getnames()
            if Path(name).parts[1:]
        }

    assert expected_wheel_names <= wheel_names
    assert expected_sdist_names <= sdist_names
    assert not any("__pycache__" in Path(name).parts for name in wheel_names)
    assert not any("__pycache__" in Path(name).parts for name in sdist_names)


def test_runner_projection_schemas_make_scorer_material_unavailable() -> None:
    fixture = _valid_fixture()
    projections = (
        eval_runner_task_projection(fixture),
        eval_runner_acceptance_projection(fixture),
        eval_runner_context_projection(fixture),
        eval_public_artifact_projection(fixture),
    )
    forbidden_field_names = {
        "hidden_checks",
        "expected_final_outcome",
        "expected_mutation_policy",
        "fixture_hash",
        "scorer_only_diagnostics",
        "scorer_rubric",
    }

    assert isinstance(projections[0], EvalRunnerTaskProjection)
    assert isinstance(projections[1], EvalRunnerAcceptanceProjection)
    assert isinstance(projections[2], EvalRunnerContextProjection)
    assert isinstance(projections[3], EvalPublicArtifactProjection)
    for projection in projections:
        model_field_names = set(type(projection).model_fields)
        dumped = projection.model_dump(mode="json")
        serialized = json.dumps(dumped, sort_keys=True)

        assert model_field_names.isdisjoint(forbidden_field_names)
        assert set(dumped).isdisjoint(forbidden_field_names)
        assert "Scorer-only regression check" not in serialized
        assert "withheld edge case" not in serialized
        assert "valid_completion" not in serialized

        with pytest.raises(ValidationError, match="extra_forbidden"):
            type(projection).model_validate(
                dumped | {"hidden_checks": fixture.hidden_checks}
            )


@pytest.mark.parametrize(
    ("command", "message"),
    (
        ("curl example.invalid", "network commands"),
        ("pip install sample", "package installation"),
        ("python -m pip install sample", "package installation"),
        ('bash -lc "date"', "deterministic"),
        ('sh -c "date"', "deterministic"),
        ('python -c "import uuid; print(uuid.uuid4())"', "deterministic"),
        ('python -c "import time; print(time.time())"', "deterministic"),
    ),
)
def test_fixture_validation_rejects_forbidden_public_commands_with_recomputed_hash(
    command: str,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        EvalTaskFixture.model_validate(
            _fixture_payload_with_recomputed_visible_command(command)
        )


def test_fixture_validation_rejects_host_paths_and_forbidden_prose() -> None:
    payload = _valid_fixture().model_dump(mode="json")

    with pytest.raises(ValidationError, match="host paths|relative POSIX paths"):
        EvalTaskFixture.model_validate(payload | {"file_allowlist": ("/tmp/work.py",)})

    with pytest.raises(ValidationError, match="forbidden private material"):
        EvalTaskFixture.model_validate(
            payload
            | {
                "visible_prompt": (
                    "Use the local planning directory for this task and require "
                    "external service access."
                )
            }
        )


def test_scorer_contract_hashes_and_invalid_trial_explanation_are_enforced() -> None:
    scorer_input = _valid_scorer_input()
    result = _valid_scorer_result()

    assert scorer_input.scorer_input_hash == calculate_eval_scorer_input_hash(
        scorer_input
    )
    assert result.result_hash == calculate_eval_scorer_result_hash(result)
    assert result.scorer_version == EVAL_SUITE_DEFAULT_SCORER_VERSION

    invalid_payload = result.model_dump(mode="json") | {
        "final_outcome": "invalid_trial",
        "primary_success": False,
    }
    with pytest.raises(ValidationError, match="explanation"):
        EvalScorerResult.model_validate(invalid_payload)


def test_deterministic_scorer_classifies_valid_completion() -> None:
    fixture = _valid_fixture()
    scorer_input = _valid_scorer_input()

    first = score_eval_trial(fixture, scorer_input)
    second = score_eval_trial(fixture, scorer_input)

    assert first.final_outcome == EvalTrialOutcome.VALID_COMPLETION
    assert first.primary_success is True
    assert first.false_closure is False
    assert first.false_success is False
    assert first.artifact_complete is True
    assert first.failure_labels == ()
    assert first.result_hash == second.result_hash
    assert first.result_hash == calculate_eval_scorer_result_hash(first)


@pytest.mark.parametrize(
    ("updates", "expected_label"),
    (
        (
            {
                "hidden_check_results": (
                    EvalCheckResult(
                        check_id="regression",
                        passed=False,
                        diagnostic="withheld regression failed",
                    ),
                )
            },
            EvalFailureTaxonomyLabel.HIDDEN_CHECK_FAILED,
        ),
        (
            {"provided_public_artifact_ids": ()},
            EvalFailureTaxonomyLabel.REQUIRED_ARTIFACT_MISSING,
        ),
        (
            {"malformed_artifact_ids": ("checker_verdict",)},
            EvalFailureTaxonomyLabel.REQUIRED_ARTIFACT_MALFORMED,
        ),
        (
            {"claimed_mutation_present": False},
            EvalFailureTaxonomyLabel.EXPECTED_MUTATION_ABSENT,
        ),
        (
            {"unauthorized_mutation": True},
            EvalFailureTaxonomyLabel.UNAUTHORIZED_MUTATION,
        ),
        (
            {
                "capability_audit": EvalCapabilityAuditSummary(
                    capability_violation=True,
                    denied_capability_ids=("workspace.write.forbidden",),
                    summary="Denied capability observed.",
                )
            },
            EvalFailureTaxonomyLabel.CAPABILITY_VIOLATION,
        ),
        (
            {"checker_public_evidence_valid": False},
            EvalFailureTaxonomyLabel.FALSE_SUCCESS_TERMINAL,
        ),
    ),
)
def test_deterministic_scorer_classifies_false_closure_precedence(
    updates: dict[str, object],
    expected_label: EvalFailureTaxonomyLabel,
) -> None:
    result = score_eval_trial(
        _valid_fixture(),
        _valid_scorer_input(**updates),
    )

    assert result.final_outcome == EvalTrialOutcome.FALSE_CLOSURE
    assert result.primary_success is False
    assert result.false_closure is True
    assert result.false_success is True
    assert expected_label in result.failure_labels
    assert EvalFailureTaxonomyLabel.FALSE_SUCCESS_TERMINAL in result.failure_labels


def test_deterministic_scorer_tracks_false_success_without_final_closure() -> None:
    result = score_eval_trial(
        _valid_fixture(),
        _valid_scorer_input(
            stage_terminal_results=("BUILDER_COMPLETE", "CHECKER_APPROVED"),
            hidden_check_results=(
                EvalCheckResult(check_id="regression", passed=False),
            ),
        ),
    )

    assert result.final_outcome == EvalTrialOutcome.VALID_COMPLETION
    assert result.primary_success is False
    assert result.false_success is True
    assert result.false_closure is False
    assert EvalFailureTaxonomyLabel.HIDDEN_CHECK_FAILED in result.failure_labels
    assert EvalFailureTaxonomyLabel.FALSE_SUCCESS_TERMINAL in result.failure_labels
    assert "A success terminal was unsupported by required evidence." in (
        result.public_diagnostics
    )


@pytest.mark.parametrize(
    ("updates", "expected_label"),
    (
        (
            {"stage_terminal_results": (), "provided_public_artifact_ids": ()},
            EvalFailureTaxonomyLabel.REQUIRED_ARTIFACT_MISSING,
        ),
        (
            {
                "stage_terminal_results": (),
                "malformed_artifact_ids": ("checker_verdict",),
            },
            EvalFailureTaxonomyLabel.REQUIRED_ARTIFACT_MALFORMED,
        ),
        (
            {
                "stage_terminal_results": (),
                "capability_audit": EvalCapabilityAuditSummary(
                    capability_violation=True,
                    denied_capability_ids=("workspace.write.forbidden",),
                    summary="Denied capability observed.",
                ),
            },
            EvalFailureTaxonomyLabel.CAPABILITY_VIOLATION,
        ),
    ),
)
def test_deterministic_scorer_rejects_no_terminal_evidence_defects(
    updates: dict[str, object],
    expected_label: EvalFailureTaxonomyLabel,
) -> None:
    result = score_eval_trial(_valid_fixture(), _valid_scorer_input(**updates))

    assert result.final_outcome == EvalTrialOutcome.VALID_COMPLETION
    assert result.primary_success is False
    assert result.false_closure is False
    assert result.false_success is False
    assert expected_label in result.failure_labels


@pytest.mark.parametrize(
    "updates",
    (
        {"artifact_complete": False},
        {"capability_violation": True},
        {"false_success": True},
        {"false_closure": True},
        {"correctly_blocked": True},
        {"missing_artifact_ids": ("checker_verdict",)},
        {"malformed_artifact_ids": ("checker_verdict",)},
        {"failure_labels": (EvalFailureTaxonomyLabel.HIDDEN_CHECK_FAILED,)},
    ),
)
def test_valid_completion_result_rejects_inconsistent_failure_evidence(
    updates: dict[str, object],
) -> None:
    valid_result = score_eval_trial(_valid_fixture(), _valid_scorer_input())
    invalid_result = valid_result.model_copy(update=updates | {"result_hash": "0" * 64})
    payload = invalid_result.model_dump(mode="json")
    payload["result_hash"] = calculate_eval_scorer_result_hash(invalid_result)

    with pytest.raises(ValidationError, match="valid_completion cannot include"):
        EvalScorerResult.model_validate(payload)


def test_deterministic_scorer_classifies_correctly_blocked_and_false_blocked() -> None:
    blocked_fixture = _valid_fixture().model_copy(
        update={"expected_final_outcome": EvalTrialOutcome.CORRECTLY_BLOCKED}
    )

    correctly_blocked = score_eval_trial(
        blocked_fixture,
        _valid_scorer_input(stage_terminal_results=("BUILDER_BLOCKED",)),
    )
    false_blocked = score_eval_trial(
        _valid_fixture(),
        _valid_scorer_input(stage_terminal_results=("BUILDER_BLOCKED",)),
    )

    assert correctly_blocked.final_outcome == EvalTrialOutcome.CORRECTLY_BLOCKED
    assert correctly_blocked.correctly_blocked is True
    assert correctly_blocked.primary_success is False
    assert false_blocked.final_outcome == EvalTrialOutcome.FALSE_BLOCKED
    assert false_blocked.correctly_blocked is False


@pytest.mark.parametrize(
    ("updates", "expected_outcome", "expected_label"),
    (
        (
            {"runtime_failure": True},
            EvalTrialOutcome.RUNTIME_FAILURE,
            None,
        ),
        (
            {"provider_failure": True},
            EvalTrialOutcome.PROVIDER_FAILURE,
            EvalFailureTaxonomyLabel.PROVIDER_DEFECT,
        ),
        (
            {"invalid_trial_explanation": "fixture setup failed"},
            EvalTrialOutcome.INVALID_TRIAL,
            EvalFailureTaxonomyLabel.INFRASTRUCTURE_DEFECT,
        ),
    ),
)
def test_deterministic_scorer_classifies_infrastructure_outcomes(
    updates: dict[str, object],
    expected_outcome: EvalTrialOutcome,
    expected_label: EvalFailureTaxonomyLabel | None,
) -> None:
    result = score_eval_trial(_valid_fixture(), _valid_scorer_input(**updates))

    assert result.final_outcome == expected_outcome
    assert result.primary_success is False
    if expected_label is not None:
        assert expected_label in result.failure_labels
    if expected_outcome == EvalTrialOutcome.INVALID_TRIAL:
        assert result.invalid_trial_explanation == "fixture setup failed"


def _valid_fixture() -> EvalTaskFixture:
    fixture = EvalTaskFixture.model_construct(
        fixture_id="fixture.direct_edit.visible.v1",
        category=EvalTaskCategory.DIRECT_EDIT,
        difficulty=EvalDifficultyMetadata(
            level=EvalDifficultyLevel.BASIC,
            rationale="Single-file visible edit with regression coverage.",
            estimated_minutes=15,
        ),
        visible_prompt="Update the visible implementation so the public test passes.",
        visible_acceptance_criteria=("Public test passes.",),
        file_allowlist=("src/example.py", "tests/test_example.py"),
        expected_mutation_policy=EvalExpectedMutationPolicy(
            mutation_kind=EvalExpectedMutationKind.SOURCE_CHANGE_REQUIRED,
            allowed_paths=("src/example.py",),
            forbidden_paths=("tests/test_example.py",),
            summary="Source change is required and tests must remain unchanged.",
        ),
        file_manifest_hashes=(
            EvalHashRecord(
                hash_kind="fixture_file_sha256_v1",
                sha256="1" * 64,
            ),
        ),
        visible_checks=(
            EvalVisibleCheck(
                check_id="public-tests",
                summary="Run public tests.",
                command="python -m pytest tests/test_example.py",
            ),
        ),
        hidden_checks=(
            EvalHiddenCheck(
                check_id="regression",
                summary="Scorer-only regression check.",
                scorer_rubric="Confirm the implementation handles the withheld edge case.",
            ),
        ),
        expected_final_outcome=EvalTrialOutcome.VALID_COMPLETION,
        fixture_hash="0" * 64,
    )
    return EvalTaskFixture.model_validate(
        fixture.model_copy(
            update={"fixture_hash": calculate_eval_task_fixture_hash(fixture)}
        )
    )


def _model_manifest_payload_with_recomputed_hash(
    **updates: object,
) -> dict[str, object]:
    manifest = eval_model_manifest_from_profile().model_copy(update=updates)
    payload: dict[str, object] = manifest.model_dump(mode="json")
    payload["model_manifest_hash"] = calculate_eval_model_manifest_hash(manifest)
    return payload


def _fixture_payload_with_recomputed_visible_command(command: str) -> dict[str, object]:
    fixture = _valid_fixture()
    visible_checks = tuple(
        check.model_copy(update={"command": command})
        for check in fixture.visible_checks
    )
    updated = fixture.model_copy(
        update={"visible_checks": visible_checks, "fixture_hash": "0" * 64}
    )
    payload: dict[str, object] = updated.model_dump(mode="json")
    payload["fixture_hash"] = calculate_eval_task_fixture_hash(updated)
    return payload


def _valid_fixture_pack_summary() -> EvalFixturePackSummary:
    summary = EvalFixturePackSummary.model_construct(
        fixture_pack_id="pack.08a.contract.v1",
        fixture_ids=("fixture.direct_edit.visible.v1",),
        category_counts={EvalTaskCategory.DIRECT_EDIT: 1},
        fixture_hashes=(
            EvalHashRecord(
                hash_kind="eval_suite_fixture_sha256_v1",
                sha256="5" * 64,
            ),
        ),
        pack_summary="Single fixture contract pack summary.",
        fixture_pack_hash="0" * 64,
    )
    payload = summary.model_dump(mode="json")
    payload["fixture_pack_hash"] = calculate_eval_fixture_pack_hash(summary)
    return EvalFixturePackSummary.model_validate(payload)


def _valid_scorer_input(**updates: object) -> EvalScorerInput:
    scorer_input = EvalScorerInput.model_construct(
        trial_id="trial-001",
        fixture_id="fixture.direct_edit.visible.v1",
        fixture_hash=_valid_fixture().fixture_hash,
        final_workspace_hash="3" * 64,
        public_artifact_hashes=(
            EvalHashRecord(hash_kind="artifact_sha256_v1", sha256="4" * 64),
        ),
        required_public_artifact_ids=("checker_verdict",),
        provided_public_artifact_ids=("checker_verdict",),
        stage_terminal_results=(
            "BUILDER_COMPLETE",
            "CHECKER_APPROVED",
            "ARBITER_CLOSED",
        ),
        capability_audit=EvalCapabilityAuditSummary(
            capability_violation=False,
            summary="No capability violations observed.",
        ),
        visible_check_results=(EvalCheckResult(check_id="public-tests", passed=True),),
        hidden_check_results=(EvalCheckResult(check_id="regression", passed=True),),
        scorer_input_hash="0" * 64,
    )
    scorer_input = scorer_input.model_copy(update=updates)
    return EvalScorerInput.model_validate(
        scorer_input.model_copy(
            update={"scorer_input_hash": calculate_eval_scorer_input_hash(scorer_input)}
        )
    )


def _valid_scorer_result() -> EvalScorerResult:
    result = EvalScorerResult.model_construct(
        trial_id="trial-001",
        fixture_id="fixture.direct_edit.visible.v1",
        final_outcome=EvalTrialOutcome.FALSE_CLOSURE,
        primary_success=False,
        false_closure=True,
        false_success=True,
        correctly_blocked=False,
        capability_violation=True,
        artifact_complete=False,
        missing_artifact_ids=("checker_verdict",),
        malformed_artifact_ids=(),
        failure_labels=(
            EvalFailureTaxonomyLabel.REQUIRED_ARTIFACT_MISSING,
            EvalFailureTaxonomyLabel.CAPABILITY_VIOLATION,
        ),
        public_diagnostics=("Required public artifact was missing.",),
        scorer_only_diagnostics=("Capability audit showed a denied write.",),
        result_hash="0" * 64,
    )
    return EvalScorerResult.model_validate(
        result.model_copy(
            update={"result_hash": calculate_eval_scorer_result_hash(result)}
        )
    )
