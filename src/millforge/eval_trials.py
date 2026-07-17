"""Public 08B offline eval-trial contract boundary."""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Mapping
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from pydantic import field_serializer
from pydantic import model_validator

from millforge.eval_artifacts import (
    EvalArtifactId,
    EvalArtifactManifestArtifact,
    EvalArtifactManifestEntry,
    calculate_eval_artifact_manifest_sha256,
    eval_artifact_layout_entry,
)
from millforge.eval_modes import (
    EVAL_SMALL_MILLFORGE_MODE_ID,
    EVAL_SMALL_PI_MODE_ID,
    EvalModeDescriptor,
    default_eval_small_millforge_mode,
    default_eval_small_pi_mode,
)
from millforge.eval_presets import (
    EvalPresetReadinessReport,
    eval_preset_readiness_report,
)
from millforge.eval_suite import (
    EvalCampaignManifest,
    EvalFixturePackSummary,
    EvalHashRecord,
    EvalPublicArtifactProjection,
    EvalScorerInput,
    EvalScorerResult,
    EvalSuiteExecutionMode,
    EvalCapabilityAuditSummary,
    EvalCheckResult,
    EvalTaskFixture,
    EvalTrialOutcome,
    calculate_eval_scorer_result_hash,
    calculate_eval_scorer_input_hash,
    default_eval_suite_campaign_manifest,
    eval_public_artifact_projection,
    load_eval_fixture_pack_summary,
    load_eval_task_fixture,
    load_eval_task_fixtures,
    score_eval_trial,
)
from millforge.eval_workflow import EvalStageId, EvalTerminalResult

EVAL_TRIAL_SCHEMA_VERSION = 1
EVAL_TRIAL_PLAN_HASH_KIND = "eval_trial_plan_sha256_v1"
EVAL_TRIAL_ARTIFACT_BUNDLE_HASH_KIND = "eval_trial_artifact_bundle_sha256_v1"
EVAL_TRIAL_RECORD_HASH_KIND = "eval_trial_record_sha256_v1"
EVAL_TRIAL_STORE_MANIFEST_HASH_KIND = "eval_trial_store_manifest_sha256_v1"
EVAL_TRIAL_RESUME_INDEX_HASH_KIND = "eval_trial_resume_index_sha256_v1"
EVAL_TRIAL_DEFAULT_CREATED_AT = "1970-01-01T00:00:00Z"
EVAL_TRIAL_ADMITTED_ARM_IDS: tuple[str, str] = (
    EVAL_SMALL_PI_MODE_ID,
    EVAL_SMALL_MILLFORGE_MODE_ID,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DENIED_TEXT_TOKENS = (
    "api_key",
    "authorization:",
    "bearer ",
    "credential",
    "password",
    "access token",
    "auth token",
    "millrace-agents",
    ".millrace",
    "daemon state",
    "endpoint_url",
    "external service",
    "hidden_checks",
    "hidden scorer",
    "hidden answer",
    "private runtime",
    "network access",
    "package install",
    "scorer_rubric",
    "ref-forge",
    ".claude",
    ".codex",
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'`([{<])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`)\]}>,]*"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'`([{<])/(?!/)[^\s\"'`)\]}>,]*")
_USER_HOME_PATH = re.compile(r"(?:^|[\s\"'`([{<])~(?:/|\\)[^\s\"'`)\]}>,]*")
_ENDPOINT_URL = re.compile(r"https?://|localhost(?::|/|$)|127\.0\.0\.1|0\.0\.0\.0")
_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"\bsk-(?:live|proj|test)-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9_]{20,}\b"),
)
_SECRET_FIELD_MARKERS = (
    "api_key",
    "apikey",
    "auth_header",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "password",
    "private_key",
    "secret",
    "access_token",
    "auth_token",
    "refresh_token",
)


class EvalTrialContractModel(BaseModel):
    """Closed, frozen base for public eval-trial contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_private_material(cls, data: Any) -> Any:
        _reject_forbidden_material(data)
        return data


class EvalTrialArmId(str, Enum):
    """Exactly admitted offline comparison arms for 08B trials."""

    EVAL_SMALL_PI = EVAL_SMALL_PI_MODE_ID
    EVAL_SMALL_MILLFORGE = EVAL_SMALL_MILLFORGE_MODE_ID


class EvalFakeOutcomeScriptKind(str, Enum):
    """Closed offline fake outcome script kinds."""

    VALID_COMPLETION = "valid_completion"
    CORRECT_BLOCK = "correct_block"
    FALSE_CLOSURE = "false_closure"
    FALSE_SUCCESS_WITHOUT_CLOSURE = "false_success_without_closure"
    RUNTIME_FAILURE = "runtime_failure"
    PROVIDER_FAILURE = "provider_failure"
    INVALID_TRIAL = "invalid_trial"


class EvalTrialInvalidDiagnosticCode(str, Enum):
    """Closed invalid-trial diagnostic codes emitted by offline contracts."""

    LIVE_EXECUTION_UNAVAILABLE = "live_execution_unavailable"
    ARM_NOT_ADMITTED = "arm_not_admitted"
    ARM_PARITY_DRIFT = "arm_parity_drift"
    FIXTURE_COPY_UNAVAILABLE = "fixture_copy_unavailable"
    INVALID_PLANNING_REQUEST = "invalid_planning_request"
    PLAN_HASH_MISMATCH = "plan_hash_mismatch"
    ARTIFACT_BUNDLE_HASH_MISMATCH = "artifact_bundle_hash_mismatch"
    RECORD_HASH_MISMATCH = "record_hash_mismatch"
    RESUME_INDEX_HASH_MISMATCH = "resume_index_hash_mismatch"
    STORE_MANIFEST_HASH_MISMATCH = "store_manifest_hash_mismatch"
    TREATMENT_HARNESS_HASH_MISMATCH = "treatment_harness_hash_mismatch"
    BASELINE_RUNTIME_HASH_UNAVAILABLE = "baseline_runtime_hash_unavailable"
    TRIAL_LOG_DUPLICATE_TRIAL_ID = "trial_log_duplicate_trial_id"
    TRIAL_LOG_INVALID_RECORD = "trial_log_invalid_record"
    TRIAL_LOG_MALFORMED_TRAILING_LINE = "trial_log_malformed_trailing_line"
    TRIAL_LOG_MISSING_FINAL_NEWLINE = "trial_log_missing_final_newline"
    TRIAL_LOG_PARTIAL_TRAILING_RECORD = "trial_log_partial_trailing_record"


class EvalTrialInvalidDiagnostic(EvalTrialContractModel):
    """Typed fail-closed diagnostic for invalid or unavailable trials."""

    diagnostic_code: EvalTrialInvalidDiagnosticCode
    rule_id: StrictStr
    summary: StrictStr


class EvalTrialArmDefinition(EvalTrialContractModel):
    """One admitted comparison arm bound to an existing eval mode descriptor."""

    arm_id: EvalTrialArmId
    mode_id: StrictStr
    descriptor_fingerprint: StrictStr
    fairness_fingerprint: StrictStr
    runner_kind: StrictStr
    descriptor: EvalModeDescriptor

    @model_validator(mode="after")
    def _arm_valid(self) -> EvalTrialArmDefinition:
        if self.mode_id != self.arm_id.value:
            raise ValueError("arm mode_id must match arm_id")
        if self.descriptor.mode_id != self.arm_id.value:
            raise ValueError("arm descriptor mode_id must match arm_id")
        if self.descriptor.descriptor_fingerprint != self.descriptor_fingerprint:
            raise ValueError("descriptor_fingerprint must match descriptor")
        if self.descriptor.fairness_fingerprint != self.fairness_fingerprint:
            raise ValueError("fairness_fingerprint must match descriptor")
        runner_kinds = {
            binding.runner_kind.value for binding in self.descriptor.runner_bindings
        }
        if runner_kinds != {self.runner_kind}:
            raise ValueError("runner_kind must match descriptor bindings")
        return self


class EvalTrialArmParityEvidence(EvalTrialContractModel):
    """Hash evidence proving the two offline arms share fairness-critical inputs."""

    left_arm_id: EvalTrialArmId
    right_arm_id: EvalTrialArmId
    shared_fairness_fingerprint: StrictStr
    left_descriptor_fingerprint: StrictStr
    right_descriptor_fingerprint: StrictStr
    workflow_graph_hash: StrictStr
    model_profile_hash: StrictStr
    fixture_pack_hash: StrictStr
    comparable_offline: StrictBool
    diagnostics: tuple[EvalTrialInvalidDiagnostic, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _parity_valid(self) -> EvalTrialArmParityEvidence:
        if self.left_arm_id == self.right_arm_id:
            raise ValueError("parity evidence requires distinct arms")
        for digest in (
            self.shared_fairness_fingerprint,
            self.left_descriptor_fingerprint,
            self.right_descriptor_fingerprint,
            self.workflow_graph_hash,
            self.model_profile_hash,
            self.fixture_pack_hash,
        ):
            _validate_sha256(digest)
        if self.comparable_offline and self.diagnostics:
            raise ValueError("comparable offline arms must not carry diagnostics")
        if not self.comparable_offline and not self.diagnostics:
            raise ValueError("non-comparable parity evidence requires diagnostics")
        return self


class EvalTrialFixtureInstance(EvalTrialContractModel):
    """One deterministic fixture instance selected for an offline trial."""

    fixture_instance_id: StrictStr
    fixture_id: StrictStr
    fixture_hash: StrictStr
    fixture_snapshot_hash: StrictStr
    fixture_pack_id: StrictStr
    fixture_pack_hash: StrictStr
    hidden_check_set_ids: tuple[StrictStr, ...]
    hidden_check_set_hash: StrictStr
    public_projection: EvalPublicArtifactProjection
    fixture_pack_summary: EvalFixturePackSummary
    physical_copy_available: StrictBool = False
    copy_unavailable_diagnostic: EvalTrialInvalidDiagnostic | None = None

    @model_validator(mode="after")
    def _fixture_instance_valid(self) -> EvalTrialFixtureInstance:
        if self.public_projection.fixture_id != self.fixture_id:
            raise ValueError("fixture_id must match public fixture projection")
        if self.fixture_pack_summary.fixture_pack_id != self.fixture_pack_id:
            raise ValueError("fixture_pack_id must match fixture pack summary")
        if self.fixture_pack_summary.fixture_pack_hash != self.fixture_pack_hash:
            raise ValueError("fixture_pack_hash must match fixture pack summary")
        pack_hashes = dict(
            zip(
                self.fixture_pack_summary.fixture_ids,
                self.fixture_pack_summary.fixture_hashes,
            )
        )
        fixture_hash_record = pack_hashes.get(self.fixture_id)
        if fixture_hash_record is None:
            raise ValueError("fixture_id must be present in fixture pack summary")
        if fixture_hash_record.sha256 != self.fixture_hash:
            raise ValueError("fixture_hash must match fixture pack summary")
        _validate_sha256(self.fixture_hash)
        _validate_sha256(self.fixture_snapshot_hash)
        _validate_sha256(self.fixture_pack_hash)
        _validate_sha256(self.hidden_check_set_hash)
        if not self.hidden_check_set_ids:
            raise ValueError("fixture instances must include hidden-check-set IDs")
        if self.hidden_check_set_hash != _hidden_check_set_hash(
            self.hidden_check_set_ids
        ):
            raise ValueError("hidden-check-set hash must match hidden-check-set IDs")
        if (
            not self.physical_copy_available
            and self.copy_unavailable_diagnostic is None
        ):
            raise ValueError("deferred fixture copying requires a typed diagnostic")
        if (
            self.physical_copy_available
            and self.copy_unavailable_diagnostic is not None
        ):
            raise ValueError(
                "available fixture copies must not carry unavailable diagnostics"
            )
        return self


class EvalTrialArmPlan(EvalTrialContractModel):
    """Per-arm deterministic planning evidence for one paired trial."""

    trial_id: StrictStr
    trial_index: StrictInt
    arm_id: EvalTrialArmId
    arm_order_index: StrictInt
    paired_seed: StrictInt
    artifact_root: StrictStr
    runner_kind: StrictStr
    runner_descriptor_id: StrictStr
    campaign_manifest_hash: StrictStr
    model_manifest_hash: StrictStr
    workflow_graph_hash: StrictStr
    fixture_pack_hash: StrictStr
    fixture_id: StrictStr
    fixture_hash: StrictStr
    visible_acceptance_criteria_hash: StrictStr
    hidden_check_set_ids: tuple[StrictStr, ...]
    hidden_check_set_hash: StrictStr
    scorer_version: StrictStr
    treatment_compiled_harness_hashes: Mapping[StrictStr, StrictStr] = Field(
        default_factory=dict
    )
    baseline_pi_runtime_hash: StrictStr | None = None
    baseline_runtime_diagnostic: EvalTrialInvalidDiagnostic | None = None

    @model_validator(mode="after")
    def _arm_plan_valid(self) -> EvalTrialArmPlan:
        if self.trial_index < 0:
            raise ValueError("trial_index must be non-negative")
        if self.arm_order_index not in {0, 1}:
            raise ValueError("arm_order_index must be 0 or 1")
        if self.paired_seed < 0:
            raise ValueError("paired_seed must be non-negative")
        _validate_relative_artifact_root(self.artifact_root)
        for digest in (
            self.campaign_manifest_hash,
            self.model_manifest_hash,
            self.workflow_graph_hash,
            self.fixture_pack_hash,
            self.fixture_hash,
            self.visible_acceptance_criteria_hash,
            self.hidden_check_set_hash,
        ):
            _validate_sha256(digest)
        if self.hidden_check_set_hash != _hidden_check_set_hash(
            self.hidden_check_set_ids
        ):
            raise ValueError("hidden-check-set hash must match hidden-check-set IDs")
        object.__setattr__(
            self,
            "treatment_compiled_harness_hashes",
            _freeze_trial_mapping(self.treatment_compiled_harness_hashes),
        )
        for digest in self.treatment_compiled_harness_hashes.values():
            _validate_sha256(digest)
        if self.arm_id is EvalTrialArmId.EVAL_SMALL_MILLFORGE:
            if self.baseline_pi_runtime_hash is not None:
                raise ValueError("treatment arms must not carry Pi runtime hashes")
            if self.baseline_runtime_diagnostic is not None:
                raise ValueError("treatment arms must not carry Pi runtime diagnostics")
        if self.arm_id is EvalTrialArmId.EVAL_SMALL_PI:
            if self.treatment_compiled_harness_hashes:
                raise ValueError(
                    "baseline arms must not carry treatment harness hashes"
                )
            if self.baseline_pi_runtime_hash is not None:
                _validate_sha256(self.baseline_pi_runtime_hash)
                _reject_fake_pi_runtime_hash(self.baseline_pi_runtime_hash)
            if (
                self.baseline_pi_runtime_hash is None
                and self.baseline_runtime_diagnostic is None
            ):
                raise ValueError(
                    "baseline arms require a Pi runtime hash or deferred diagnostic"
                )
        return self


class EvalTrialFakeRunnerScript(EvalTrialContractModel):
    """Offline fake runner script descriptor without live execution claims."""

    script_id: StrictStr
    script_kind: EvalFakeOutcomeScriptKind
    terminal_results: tuple[EvalTerminalResult, ...]
    expected_outcome: EvalTrialOutcome
    stage_result_summaries: Mapping[EvalStageId, StrictStr] = Field(
        default_factory=dict
    )
    zero_usage: StrictBool = True

    @model_validator(mode="after")
    def _script_valid(self) -> EvalTrialFakeRunnerScript:
        if not self.terminal_results:
            raise ValueError("fake scripts must declare terminal results")
        if not self.zero_usage:
            raise ValueError("offline fake scripts must preserve zero-usage semantics")
        object.__setattr__(
            self,
            "stage_result_summaries",
            _freeze_trial_mapping(self.stage_result_summaries),
        )
        return self


class EvalTrialPlan(EvalTrialContractModel):
    """Hashable paired offline eval-trial plan."""

    schema_version: StrictInt = EVAL_TRIAL_SCHEMA_VERSION
    trial_id: StrictStr
    trial_index: StrictInt = 0
    trial_plan_id: StrictStr
    paired_seed: StrictInt = 0
    campaign_store_root: StrictStr
    campaign_manifest: EvalCampaignManifest
    arms: tuple[EvalTrialArmDefinition, EvalTrialArmDefinition]
    arm_order: tuple[EvalTrialArmId, EvalTrialArmId]
    arm_plans: tuple[EvalTrialArmPlan, EvalTrialArmPlan]
    parity_evidence: EvalTrialArmParityEvidence
    fixture_instance: EvalTrialFixtureInstance
    fake_runner_script: EvalTrialFakeRunnerScript
    spec_07_readiness: EvalPresetReadinessReport
    created_at: StrictStr = EVAL_TRIAL_DEFAULT_CREATED_AT
    plan_hash_kind: StrictStr = EVAL_TRIAL_PLAN_HASH_KIND
    plan_hash: StrictStr

    @model_validator(mode="after")
    def _plan_valid(self) -> EvalTrialPlan:
        if self.schema_version != EVAL_TRIAL_SCHEMA_VERSION:
            raise ValueError("unsupported eval-trial schema_version")
        if not _UTC_TIMESTAMP_RE.fullmatch(self.created_at):
            raise ValueError("trial plan created_at must be a UTC timestamp")
        if self.trial_index < 0:
            raise ValueError("trial_index must be non-negative")
        if self.paired_seed < 0:
            raise ValueError("paired_seed must be non-negative")
        _validate_relative_artifact_root(self.campaign_store_root)
        arm_ids = tuple(arm.arm_id for arm in self.arms)
        if arm_ids != (
            EvalTrialArmId.EVAL_SMALL_PI,
            EvalTrialArmId.EVAL_SMALL_MILLFORGE,
        ):
            raise ValueError("trial plans must contain exactly the admitted arms")
        if set(self.arm_order) != set(arm_ids) or len(set(self.arm_order)) != 2:
            raise ValueError("arm_order must contain each admitted arm exactly once")
        if {plan.arm_id for plan in self.arm_plans} != set(arm_ids):
            raise ValueError("arm_plans must contain each admitted arm exactly once")
        if self.campaign_manifest.execution_mode != EvalSuiteExecutionMode.OFFLINE_FAKE:
            raise ValueError("eval-trial plans are offline fake contracts only")
        if self.campaign_manifest.live_execution_admitted:
            raise ValueError("eval-trial planning does not admit live execution")
        if self.campaign_manifest.pi_eval_mode_id != EvalTrialArmId.EVAL_SMALL_PI.value:
            raise ValueError("campaign pi arm must match admitted pi mode")
        if (
            self.campaign_manifest.millforge_eval_mode_id
            != EvalTrialArmId.EVAL_SMALL_MILLFORGE.value
        ):
            raise ValueError(
                "campaign millforge arm must match admitted millforge mode"
            )
        left_arm, right_arm = self.arms
        if self.parity_evidence.left_arm_id != left_arm.arm_id:
            raise ValueError("parity left arm must match trial arms")
        if self.parity_evidence.right_arm_id != right_arm.arm_id:
            raise ValueError("parity right arm must match trial arms")
        if (
            self.parity_evidence.left_descriptor_fingerprint
            != left_arm.descriptor_fingerprint
        ):
            raise ValueError("parity left descriptor fingerprint must match arm")
        if (
            self.parity_evidence.right_descriptor_fingerprint
            != right_arm.descriptor_fingerprint
        ):
            raise ValueError("parity right descriptor fingerprint must match arm")
        if (
            self.parity_evidence.shared_fairness_fingerprint
            not in {
                left_arm.fairness_fingerprint,
                right_arm.fairness_fingerprint,
            }
            or left_arm.fairness_fingerprint != right_arm.fairness_fingerprint
        ):
            raise ValueError("parity fairness fingerprint must match both arms")
        if self.parity_evidence.workflow_graph_hash not in {
            left_arm.descriptor.graph_sha256,
            right_arm.descriptor.graph_sha256,
            self.campaign_manifest.workflow_graph_hash,
        } or not (
            left_arm.descriptor.graph_sha256
            == right_arm.descriptor.graph_sha256
            == self.campaign_manifest.workflow_graph_hash
        ):
            raise ValueError("parity workflow graph hash must match arms and campaign")
        if self.parity_evidence.model_profile_hash not in {
            left_arm.descriptor.model_profile.model_profile_hash,
            right_arm.descriptor.model_profile.model_profile_hash,
        } or (
            left_arm.descriptor.model_profile.model_profile_hash
            != right_arm.descriptor.model_profile.model_profile_hash
        ):
            raise ValueError("parity model profile hash must match both arms")
        if (
            self.parity_evidence.fixture_pack_hash
            != self.campaign_manifest.fixture_pack_hash
            or self.parity_evidence.fixture_pack_hash
            != self.fixture_instance.fixture_pack_hash
        ):
            raise ValueError(
                "parity fixture pack hash must match campaign and fixture instance"
            )
        expected_treatment_hashes = _compiled_harness_hashes_by_stage(
            self.spec_07_readiness
        )
        shared_arm_values = {
            "trial_id": self.trial_id,
            "trial_index": self.trial_index,
            "paired_seed": self.paired_seed,
            "campaign_manifest_hash": self.campaign_manifest.campaign_manifest_hash,
            "model_manifest_hash": self.campaign_manifest.model_manifest_hash,
            "workflow_graph_hash": self.campaign_manifest.workflow_graph_hash,
            "fixture_pack_hash": self.fixture_instance.fixture_pack_hash,
            "fixture_id": self.fixture_instance.fixture_id,
            "fixture_hash": self.fixture_instance.fixture_hash,
            "visible_acceptance_criteria_hash": _visible_acceptance_criteria_hash(
                self.fixture_instance.public_projection.visible_acceptance_criteria
            ),
            "hidden_check_set_hash": self.fixture_instance.hidden_check_set_hash,
            "scorer_version": self.campaign_manifest.scorer_version,
        }
        arms_by_id = {arm.arm_id: arm for arm in self.arms}
        for arm_plan in self.arm_plans:
            _validate_artifact_root_under_campaign(
                arm_plan.artifact_root,
                self.campaign_store_root,
            )
            for field_name, expected_value in shared_arm_values.items():
                if getattr(arm_plan, field_name) != expected_value:
                    raise ValueError(
                        f"arm plan {field_name} must match paired trial inputs"
                    )
            if (
                arm_plan.hidden_check_set_ids
                != self.fixture_instance.hidden_check_set_ids
            ):
                raise ValueError("arm plan hidden-check-set IDs must match fixture")
            descriptor = arms_by_id[arm_plan.arm_id]
            if arm_plan.runner_kind != descriptor.runner_kind:
                raise ValueError("arm plan runner_kind must match descriptor")
            if arm_plan.runner_descriptor_id != descriptor.descriptor_fingerprint:
                raise ValueError("arm plan runner descriptor must match descriptor")
            if arm_plan.arm_order_index != self.arm_order.index(arm_plan.arm_id):
                raise ValueError("arm plan order index must match arm_order")
            if arm_plan.arm_id is EvalTrialArmId.EVAL_SMALL_MILLFORGE and dict(
                arm_plan.treatment_compiled_harness_hashes
            ) != dict(expected_treatment_hashes):
                raise ValueError(
                    "treatment compiled harness hashes must match Spec 07E readiness"
                )
        pairings = {
            (plan.fixture_id, plan.arm_id.value, plan.trial_index)
            for plan in self.arm_plans
        }
        if len(pairings) != len(self.arm_plans):
            raise ValueError("duplicate fixture/arm/trial-index pairings are invalid")
        if not self.parity_evidence.comparable_offline:
            raise ValueError("trial plans require comparable offline parity evidence")
        if self.plan_hash_kind != EVAL_TRIAL_PLAN_HASH_KIND:
            raise ValueError("unsupported trial plan hash kind")
        _validate_sha256(self.plan_hash)
        expected = calculate_eval_trial_plan_hash(self)
        if self.plan_hash != expected:
            raise ValueError("plan_hash does not match trial plan payload")
        return self


class EvalFakeRunnerArtifactBundle(EvalTrialContractModel):
    """Offline fake-runner artifact bundle manifest for one arm attempt."""

    schema_version: StrictInt = EVAL_TRIAL_SCHEMA_VERSION
    artifact_bundle_id: StrictStr
    trial_plan_hash: StrictStr
    arm_id: EvalTrialArmId
    artifact_root: StrictStr
    artifact_manifest: EvalArtifactManifestArtifact
    artifact_hashes: tuple[EvalHashRecord, ...] = Field(default_factory=tuple)
    zero_model_usage: StrictBool = True
    zero_external_usage: StrictBool = True
    artifact_bundle_hash_kind: StrictStr = EVAL_TRIAL_ARTIFACT_BUNDLE_HASH_KIND
    artifact_bundle_hash: StrictStr

    @model_validator(mode="after")
    def _bundle_valid(self) -> EvalFakeRunnerArtifactBundle:
        if self.schema_version != EVAL_TRIAL_SCHEMA_VERSION:
            raise ValueError("unsupported eval-trial artifact schema_version")
        _validate_sha256(self.trial_plan_hash)
        _validate_relative_artifact_root(self.artifact_root)
        if not self.zero_model_usage or not self.zero_external_usage:
            raise ValueError("offline fake bundles must preserve zero-usage semantics")
        if len({record.sha256 for record in self.artifact_hashes}) != len(
            self.artifact_hashes
        ):
            raise ValueError("artifact bundle hashes must be unique")
        if self.artifact_bundle_hash_kind != EVAL_TRIAL_ARTIFACT_BUNDLE_HASH_KIND:
            raise ValueError("unsupported artifact bundle hash kind")
        _validate_sha256(self.artifact_bundle_hash)
        expected = calculate_eval_fake_runner_artifact_bundle_hash(self)
        if self.artifact_bundle_hash != expected:
            raise ValueError("artifact_bundle_hash does not match payload")
        return self


class EvalOfflineFakeTrialRun(EvalTrialContractModel):
    """Deterministic paired fake-runner output for one planned offline trial."""

    schema_version: StrictInt = EVAL_TRIAL_SCHEMA_VERSION
    trial_id: StrictStr
    trial_plan_hash: StrictStr
    artifact_bundles: tuple[EvalFakeRunnerArtifactBundle, EvalFakeRunnerArtifactBundle]
    execution_results: tuple[EvalTrialExecutionResult, EvalTrialExecutionResult]
    trial_record: EvalTrialRecord
    live_execution_admitted: StrictBool = False
    diagnostics: tuple[EvalTrialInvalidDiagnostic, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _run_valid(self) -> EvalOfflineFakeTrialRun:
        if self.schema_version != EVAL_TRIAL_SCHEMA_VERSION:
            raise ValueError("unsupported eval-trial fake-run schema_version")
        _validate_sha256(self.trial_plan_hash)
        if self.live_execution_admitted:
            raise ValueError("offline fake trial runs do not admit live execution")
        bundle_arm_ids = tuple(bundle.arm_id for bundle in self.artifact_bundles)
        result_arm_ids = tuple(result.arm_id for result in self.execution_results)
        expected_arm_ids = (
            EvalTrialArmId.EVAL_SMALL_PI,
            EvalTrialArmId.EVAL_SMALL_MILLFORGE,
        )
        if bundle_arm_ids != expected_arm_ids or result_arm_ids != expected_arm_ids:
            raise ValueError("fake trial runs must contain exactly the admitted arms")
        if self.trial_record.arm_results != self.execution_results:
            raise ValueError("trial_record must mirror execution_results")
        return self


class EvalTrialExecutionResult(EvalTrialContractModel):
    """Scorer-facing execution result for one offline arm."""

    schema_version: StrictInt = EVAL_TRIAL_SCHEMA_VERSION
    trial_id: StrictStr
    trial_plan_hash: StrictStr
    arm_id: EvalTrialArmId
    terminal_results: tuple[EvalTerminalResult, ...]
    artifact_bundle_hash: StrictStr
    scorer_input: EvalScorerInput
    scorer_result: EvalScorerResult
    live_execution_admitted: StrictBool = False
    diagnostics: tuple[EvalTrialInvalidDiagnostic, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _execution_result_valid(self) -> EvalTrialExecutionResult:
        if self.schema_version != EVAL_TRIAL_SCHEMA_VERSION:
            raise ValueError("unsupported eval-trial result schema_version")
        _validate_sha256(self.trial_plan_hash)
        _validate_sha256(self.artifact_bundle_hash)
        if self.scorer_input.trial_id != self.trial_id:
            raise ValueError("scorer_input trial_id must match result")
        if self.scorer_result.trial_id != self.trial_id:
            raise ValueError("scorer_result trial_id must match result")
        if self.live_execution_admitted:
            raise ValueError("eval-trial contracts do not admit live execution")
        return self


class EvalTrialRunnerRecordSummary(EvalTrialContractModel):
    """Public runner identity and runtime diagnostic for one arm record."""

    arm_id: EvalTrialArmId
    runner_kind: StrictStr
    runner_descriptor_id: StrictStr
    runner_descriptor_version: StrictStr
    baseline_pi_runtime_hash: StrictStr | None = None
    baseline_runtime_diagnostic: EvalTrialInvalidDiagnostic | None = None

    @model_validator(mode="after")
    def _runner_summary_valid(self) -> EvalTrialRunnerRecordSummary:
        if self.arm_id is EvalTrialArmId.EVAL_SMALL_PI:
            if self.baseline_pi_runtime_hash is not None:
                _validate_sha256(self.baseline_pi_runtime_hash)
                _reject_fake_pi_runtime_hash(self.baseline_pi_runtime_hash)
            if (
                self.baseline_pi_runtime_hash is None
                and self.baseline_runtime_diagnostic is None
            ):
                raise ValueError(
                    "Pi baseline records require a runtime hash or deferred diagnostic"
                )
        else:
            if self.baseline_pi_runtime_hash is not None:
                raise ValueError("treatment runner summaries must not carry Pi hashes")
            if self.baseline_runtime_diagnostic is not None:
                raise ValueError(
                    "treatment runner summaries must not carry Pi diagnostics"
                )
        return self


class EvalTrialScorerPublicSummary(EvalTrialContractModel):
    """Public scorer result digest without hidden scorer material."""

    arm_id: EvalTrialArmId
    scorer_version: StrictStr
    final_outcome: EvalTrialOutcome
    primary_success: StrictBool
    false_closure: StrictBool
    false_success: StrictBool
    correctly_blocked: StrictBool
    capability_violation: StrictBool
    artifact_complete: StrictBool
    missing_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    malformed_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    failure_labels: tuple[StrictStr, ...] = Field(default_factory=tuple)
    public_diagnostics: tuple[StrictStr, ...] = Field(default_factory=tuple)
    invalid_trial_explanation: StrictStr | None = None


class EvalTrialResourceSummary(EvalTrialContractModel):
    """Public resource-use summary for deterministic offline records."""

    artifact_count: StrictInt
    artifact_bytes: StrictInt = 0
    turn_count: StrictInt = 0
    invalid_tool_call_count: StrictInt = 0
    malformed_argument_count: StrictInt = 0
    prerequisite_violation_count: StrictInt = 0
    premature_terminal_count: StrictInt = 0
    tool_recovery_count: StrictInt = 0
    resource_artifact_hashes: tuple[EvalHashRecord, ...] = Field(default_factory=tuple)
    zero_external_usage: StrictBool = True

    @model_validator(mode="after")
    def _resource_summary_valid(self) -> EvalTrialResourceSummary:
        for field_name in (
            "artifact_count",
            "artifact_bytes",
            "turn_count",
            "invalid_tool_call_count",
            "malformed_argument_count",
            "prerequisite_violation_count",
            "premature_terminal_count",
            "tool_recovery_count",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if not self.zero_external_usage:
            raise ValueError("offline trial records must preserve zero external usage")
        return self


class EvalTrialModelUsageSummary(EvalTrialContractModel):
    """Public model-use summary for deterministic offline records."""

    zero_model_usage: StrictBool = True
    input_tokens: StrictInt = 0
    output_tokens: StrictInt = 0
    model_call_count: StrictInt = 0

    @model_validator(mode="after")
    def _model_usage_summary_valid(self) -> EvalTrialModelUsageSummary:
        if not self.zero_model_usage:
            raise ValueError("offline trial records must preserve zero model usage")
        if (
            self.input_tokens != 0
            or self.output_tokens != 0
            or self.model_call_count != 0
        ):
            raise ValueError("offline trial records must report zero model usage")
        return self


class EvalTrialRecord(EvalTrialContractModel):
    """Append-only paired trial record."""

    schema_version: StrictInt = EVAL_TRIAL_SCHEMA_VERSION
    campaign_id: StrictStr
    task_fixture_id: StrictStr
    task_category: StrictStr
    trial_id: StrictStr
    trial_index: StrictInt
    arm: EvalTrialArmId
    arm_order_index: StrictInt
    arm_order: tuple[EvalTrialArmId, EvalTrialArmId]
    seed_marker: StrictStr
    trial_plan_hash: StrictStr
    campaign_manifest_hash: StrictStr
    model_manifest_hash: StrictStr
    workflow_graph_hash: StrictStr
    fixture_pack_hash: StrictStr
    fixture_instance_id: StrictStr
    fixture_id: StrictStr
    fixture_hash: StrictStr
    fixture_snapshot_hash: StrictStr
    runner_summaries: Mapping[EvalTrialArmId, EvalTrialRunnerRecordSummary]
    compiled_harness_hashes: Mapping[StrictStr, StrictStr]
    arm_results: tuple[EvalTrialExecutionResult, EvalTrialExecutionResult]
    final_outcomes: Mapping[EvalTrialArmId, EvalTrialOutcome]
    scorer_result_hashes: Mapping[EvalTrialArmId, StrictStr]
    scorer_public_summaries: Mapping[EvalTrialArmId, EvalTrialScorerPublicSummary]
    artifact_manifest_hashes: Mapping[EvalTrialArmId, StrictStr]
    artifact_roots: Mapping[EvalTrialArmId, StrictStr]
    resource_summary: EvalTrialResourceSummary
    model_usage_summary: EvalTrialModelUsageSummary
    invalid_trial_explanations: Mapping[EvalTrialArmId, StrictStr] = Field(
        default_factory=dict
    )
    started_at: StrictStr
    ended_at: StrictStr
    created_at: StrictStr = EVAL_TRIAL_DEFAULT_CREATED_AT
    record_hash_kind: StrictStr = EVAL_TRIAL_RECORD_HASH_KIND
    record_hash: StrictStr

    @field_serializer("arm_results")
    def _serialize_public_arm_results(
        self,
        value: tuple[EvalTrialExecutionResult, EvalTrialExecutionResult],
    ) -> tuple[dict[str, Any], ...]:
        rendered: list[dict[str, Any]] = []
        for result in value:
            payload = result.model_dump(mode="json")
            scorer_input = dict(payload["scorer_input"])
            scorer_input.pop("hidden_check_results", None)
            payload["scorer_input"] = scorer_input
            scorer_result = dict(payload["scorer_result"])
            scorer_result.pop("scorer_only_diagnostics", None)
            payload["scorer_result"] = scorer_result
            rendered.append(payload)
        return tuple(rendered)

    @model_validator(mode="after")
    def _record_valid(self) -> EvalTrialRecord:
        if self.schema_version != EVAL_TRIAL_SCHEMA_VERSION:
            raise ValueError("unsupported eval-trial record schema_version")
        for field_name in ("created_at", "started_at", "ended_at"):
            if not _UTC_TIMESTAMP_RE.fullmatch(getattr(self, field_name)):
                raise ValueError(f"trial record {field_name} must be a UTC timestamp")
        if self.ended_at < self.started_at:
            raise ValueError("trial record ended_at must not precede started_at")
        if self.task_fixture_id != self.fixture_id:
            raise ValueError("task_fixture_id must match fixture_id")
        if not self.fixture_instance_id:
            raise ValueError("fixture_instance_id must not be empty")
        if self.trial_index < 0:
            raise ValueError("trial_index must be non-negative")
        if self.arm_order_index not in {0, 1}:
            raise ValueError("arm_order_index must be 0 or 1")
        if self.arm_order_index != self.arm_order.index(self.arm):
            raise ValueError("arm_order_index must match record arm")
        for digest in (
            self.trial_plan_hash,
            self.campaign_manifest_hash,
            self.model_manifest_hash,
            self.workflow_graph_hash,
            self.fixture_pack_hash,
            self.fixture_hash,
            self.fixture_snapshot_hash,
        ):
            _validate_sha256(digest)
        result_arm_ids = tuple(result.arm_id for result in self.arm_results)
        if result_arm_ids != (
            EvalTrialArmId.EVAL_SMALL_PI,
            EvalTrialArmId.EVAL_SMALL_MILLFORGE,
        ):
            raise ValueError("trial records must contain exactly the admitted arms")
        expected_outcomes = {
            result.arm_id: result.scorer_result.final_outcome
            for result in self.arm_results
        }
        if dict(self.final_outcomes) != expected_outcomes:
            raise ValueError("final_outcomes must mirror scorer results")
        expected_hashes = {
            result.arm_id: result.scorer_result.result_hash
            for result in self.arm_results
        }
        if dict(self.scorer_result_hashes) != expected_hashes:
            raise ValueError("scorer_result_hashes must mirror scorer results")
        for result in self.arm_results:
            if result.scorer_result.result_hash != calculate_eval_scorer_result_hash(
                result.scorer_result
            ):
                raise ValueError("scorer result hash must match scorer payload")
        expected_invalid = {
            result.arm_id: result.scorer_result.invalid_trial_explanation
            for result in self.arm_results
            if result.scorer_result.invalid_trial_explanation
        }
        if dict(self.invalid_trial_explanations) != expected_invalid:
            raise ValueError(
                "invalid_trial_explanations must mirror public scorer explanations"
            )
        for arm_id, summary in self.scorer_public_summaries.items():
            matching_result = next(
                (
                    candidate
                    for candidate in self.arm_results
                    if candidate.arm_id == arm_id
                ),
                None,
            )
            if (
                matching_result is None
                or summary.final_outcome != matching_result.scorer_result.final_outcome
            ):
                raise ValueError("scorer public summaries must mirror scorer results")
        expected_treatment_hashes = _compiled_harness_hashes_by_stage(
            eval_preset_readiness_report()
        )
        if dict(self.compiled_harness_hashes) != dict(expected_treatment_hashes):
            raise ValueError(
                "treatment compiled harness hashes must match current Spec 07E readiness"
            )
        if set(self.runner_summaries) != set(result_arm_ids):
            raise ValueError("runner summaries must cover all trial arms")
        if set(self.artifact_manifest_hashes) != set(result_arm_ids):
            raise ValueError("artifact manifest hashes must cover all trial arms")
        if set(self.artifact_roots) != set(result_arm_ids):
            raise ValueError("artifact roots must cover all trial arms")
        for digest in self.artifact_manifest_hashes.values():
            _validate_sha256(digest)
        for path in self.artifact_roots.values():
            _validate_relative_artifact_root(path)
        object.__setattr__(
            self, "final_outcomes", _freeze_trial_mapping(self.final_outcomes)
        )
        for field_name in (
            "runner_summaries",
            "compiled_harness_hashes",
            "scorer_result_hashes",
            "scorer_public_summaries",
            "artifact_manifest_hashes",
            "artifact_roots",
            "invalid_trial_explanations",
        ):
            object.__setattr__(
                self, field_name, _freeze_trial_mapping(getattr(self, field_name))
            )
        if self.record_hash_kind != EVAL_TRIAL_RECORD_HASH_KIND:
            raise ValueError("unsupported trial record hash kind")
        _validate_sha256(self.record_hash)
        expected = calculate_eval_trial_record_hash(self)
        if self.record_hash != expected:
            raise ValueError("record_hash does not match trial record payload")
        return self


class EvalTrialStoreManifest(EvalTrialContractModel):
    """Append-only campaign store manifest."""

    schema_version: StrictInt = EVAL_TRIAL_SCHEMA_VERSION
    store_manifest_id: StrictStr
    campaign_manifest_hash: StrictStr
    record_hashes: tuple[StrictStr, ...] = Field(default_factory=tuple)
    append_only: StrictBool = True
    store_manifest_hash_kind: StrictStr = EVAL_TRIAL_STORE_MANIFEST_HASH_KIND
    store_manifest_hash: StrictStr

    @model_validator(mode="after")
    def _store_manifest_valid(self) -> EvalTrialStoreManifest:
        if self.schema_version != EVAL_TRIAL_SCHEMA_VERSION:
            raise ValueError("unsupported eval-trial store schema_version")
        _validate_sha256(self.campaign_manifest_hash)
        for digest in self.record_hashes:
            _validate_sha256(digest)
        if len(set(self.record_hashes)) != len(self.record_hashes):
            raise ValueError("record_hashes must be unique")
        if not self.append_only:
            raise ValueError("eval-trial stores are append-only")
        if self.store_manifest_hash_kind != EVAL_TRIAL_STORE_MANIFEST_HASH_KIND:
            raise ValueError("unsupported store manifest hash kind")
        _validate_sha256(self.store_manifest_hash)
        expected = calculate_eval_trial_store_manifest_hash(self)
        if self.store_manifest_hash != expected:
            raise ValueError("store_manifest_hash does not match payload")
        return self


class EvalTrialResumeIndex(EvalTrialContractModel):
    """Deterministic resume index for append-only offline campaigns."""

    schema_version: StrictInt = EVAL_TRIAL_SCHEMA_VERSION
    resume_index_id: StrictStr
    campaign_manifest_hash: StrictStr
    completed_trial_record_hashes: tuple[StrictStr, ...] = Field(default_factory=tuple)
    pending_trial_plan_hashes: tuple[StrictStr, ...] = Field(default_factory=tuple)
    invalid_trial_diagnostics: tuple[EvalTrialInvalidDiagnostic, ...] = Field(
        default_factory=tuple
    )
    resume_index_hash_kind: StrictStr = EVAL_TRIAL_RESUME_INDEX_HASH_KIND
    resume_index_hash: StrictStr

    @model_validator(mode="after")
    def _resume_index_valid(self) -> EvalTrialResumeIndex:
        if self.schema_version != EVAL_TRIAL_SCHEMA_VERSION:
            raise ValueError("unsupported eval-trial resume schema_version")
        _validate_sha256(self.campaign_manifest_hash)
        for digest in (
            self.completed_trial_record_hashes + self.pending_trial_plan_hashes
        ):
            _validate_sha256(digest)
        if len(set(self.completed_trial_record_hashes)) != len(
            self.completed_trial_record_hashes
        ):
            raise ValueError("completed trial record hashes must be unique")
        if len(set(self.pending_trial_plan_hashes)) != len(
            self.pending_trial_plan_hashes
        ):
            raise ValueError("pending trial plan hashes must be unique")
        if self.resume_index_hash_kind != EVAL_TRIAL_RESUME_INDEX_HASH_KIND:
            raise ValueError("unsupported resume index hash kind")
        _validate_sha256(self.resume_index_hash)
        expected = calculate_eval_trial_resume_index_hash(self)
        if self.resume_index_hash != expected:
            raise ValueError("resume_index_hash does not match payload")
        return self


class EvalTrialCampaignStoreAppendResult(EvalTrialContractModel):
    """Result from one append-only campaign-store record write."""

    campaign_store_root: StrictStr
    manifest: EvalTrialStoreManifest
    plan: EvalTrialPlan
    appended_trial_id: StrictStr
    appended_record_hash: StrictStr
    completed_trial_ids: tuple[StrictStr, ...]
    resume_index: EvalTrialResumeIndex


class EvalTrialCampaignStoreRecordSummary(EvalTrialContractModel):
    """Public record identity reconstructed from an append-only trial log."""

    trial_id: StrictStr
    trial_plan_hash: StrictStr
    record_hash: StrictStr

    @model_validator(mode="after")
    def _summary_valid(self) -> EvalTrialCampaignStoreRecordSummary:
        _validate_sha256(self.trial_plan_hash)
        _validate_sha256(self.record_hash)
        return self


class EvalTrialCampaignStoreResumeResult(EvalTrialContractModel):
    """Non-mutating resume view plus diagnostics for one campaign store."""

    campaign_store_root: StrictStr
    plan: EvalTrialPlan
    completed_trial_ids: tuple[StrictStr, ...]
    pending_trial_ids: tuple[StrictStr, ...]
    records: tuple[EvalTrialCampaignStoreRecordSummary, ...]
    resume_index: EvalTrialResumeIndex | None = None
    diagnostics: tuple[EvalTrialInvalidDiagnostic, ...] = Field(default_factory=tuple)


def default_eval_trial_arm_definitions() -> tuple[
    EvalTrialArmDefinition, EvalTrialArmDefinition
]:
    """Return the two admitted offline comparison arm definitions."""
    return (
        _arm_definition(default_eval_small_pi_mode(), EvalTrialArmId.EVAL_SMALL_PI),
        _arm_definition(
            default_eval_small_millforge_mode(spec_07_static_presets_ready=True),
            EvalTrialArmId.EVAL_SMALL_MILLFORGE,
        ),
    )


def default_eval_trial_parity_evidence(
    arms: tuple[EvalTrialArmDefinition, EvalTrialArmDefinition] | None = None,
) -> EvalTrialArmParityEvidence:
    """Return deterministic offline parity evidence for the admitted arms."""
    arms = arms or default_eval_trial_arm_definitions()
    fixture_pack_hash = load_eval_fixture_pack_summary().fixture_pack_hash
    left, right = arms
    comparable = left.fairness_fingerprint == right.fairness_fingerprint
    return EvalTrialArmParityEvidence(
        left_arm_id=left.arm_id,
        right_arm_id=right.arm_id,
        shared_fairness_fingerprint=left.fairness_fingerprint,
        left_descriptor_fingerprint=left.descriptor_fingerprint,
        right_descriptor_fingerprint=right.descriptor_fingerprint,
        workflow_graph_hash=left.descriptor.graph_sha256,
        model_profile_hash=left.descriptor.model_profile.model_profile_hash,
        fixture_pack_hash=fixture_pack_hash,
        comparable_offline=comparable,
        diagnostics=()
        if comparable
        else (
            EvalTrialInvalidDiagnostic(
                diagnostic_code=EvalTrialInvalidDiagnosticCode.ARM_PARITY_DRIFT,
                rule_id="eval_trials.arm_parity.fairness_fingerprint",
                summary="admitted arms do not share fairness-critical fingerprints",
            ),
        ),
    )


def default_eval_trial_fixture_instance(
    fixture: EvalTaskFixture,
    *,
    fixture_instance_id: str | None = None,
    trial_id: str = "trial.08b.default.v1",
    trial_index: int = 0,
    paired_seed: int = 0,
) -> EvalTrialFixtureInstance:
    """Bind one 08A fixture to a deterministic trial fixture instance."""
    pack = load_eval_fixture_pack_summary()
    instance_id = fixture_instance_id or _fixture_instance_id(
        trial_id=trial_id,
        trial_index=trial_index,
        fixture_id=fixture.fixture_id,
        paired_seed=paired_seed,
    )
    hidden_check_ids = tuple(check.check_id for check in fixture.hidden_checks)
    return EvalTrialFixtureInstance(
        fixture_instance_id=instance_id,
        fixture_id=fixture.fixture_id,
        fixture_hash=fixture.fixture_hash,
        fixture_snapshot_hash=_fixture_snapshot_hash(
            fixture=fixture,
            fixture_instance_id=instance_id,
            paired_seed=paired_seed,
        ),
        fixture_pack_id=pack.fixture_pack_id,
        fixture_pack_hash=pack.fixture_pack_hash,
        hidden_check_set_ids=hidden_check_ids,
        hidden_check_set_hash=_hidden_check_set_hash(hidden_check_ids),
        public_projection=eval_public_artifact_projection(fixture),
        fixture_pack_summary=pack,
        physical_copy_available=False,
        copy_unavailable_diagnostic=fixture_copy_unavailable_diagnostic(),
    )


def default_eval_trial_plan(
    *,
    trial_plan_id: str,
    fixture: EvalTaskFixture,
    fake_runner_script: EvalTrialFakeRunnerScript,
    trial_id: str | None = None,
    trial_index: int = 0,
    paired_seed: int = 0,
    arm_order: tuple[EvalTrialArmId, EvalTrialArmId] | None = None,
    campaign_store_root: str | None = None,
    artifact_roots_by_arm: Mapping[EvalTrialArmId | str, str] | None = None,
    campaign_manifest: EvalCampaignManifest | None = None,
    created_at: str = EVAL_TRIAL_DEFAULT_CREATED_AT,
) -> EvalTrialPlan:
    """Build a hash-finalized offline paired trial plan."""
    campaign_manifest = campaign_manifest or default_eval_suite_campaign_manifest()
    trial_id = trial_id or trial_plan_id.removesuffix(".plan")
    campaign_store_root = campaign_store_root or _default_campaign_store_root(
        campaign_manifest
    )
    arm_order = arm_order or (
        EvalTrialArmId.EVAL_SMALL_PI,
        EvalTrialArmId.EVAL_SMALL_MILLFORGE,
    )
    arms = default_eval_trial_arm_definitions()
    readiness = eval_preset_readiness_report()
    fixture_instance = default_eval_trial_fixture_instance(
        fixture,
        trial_id=trial_id,
        trial_index=trial_index,
        paired_seed=paired_seed,
    )
    plan = EvalTrialPlan.model_construct(
        schema_version=EVAL_TRIAL_SCHEMA_VERSION,
        trial_id=trial_id,
        trial_index=trial_index,
        trial_plan_id=trial_plan_id,
        paired_seed=paired_seed,
        campaign_store_root=campaign_store_root,
        campaign_manifest=campaign_manifest,
        arms=arms,
        arm_order=arm_order,
        arm_plans=_default_eval_trial_arm_plans(
            trial_id=trial_id,
            trial_index=trial_index,
            paired_seed=paired_seed,
            campaign_store_root=campaign_store_root,
            campaign_manifest=campaign_manifest,
            arms=arms,
            arm_order=arm_order,
            fixture=fixture,
            readiness=readiness,
            artifact_roots_by_arm=artifact_roots_by_arm,
        ),
        parity_evidence=default_eval_trial_parity_evidence(arms),
        fixture_instance=fixture_instance,
        fake_runner_script=fake_runner_script,
        spec_07_readiness=readiness,
        created_at=created_at,
        plan_hash_kind=EVAL_TRIAL_PLAN_HASH_KIND,
        plan_hash="0" * 64,
    )
    return EvalTrialPlan.model_validate(
        plan.model_copy(update={"plan_hash": calculate_eval_trial_plan_hash(plan)})
    )


def plan_paired_eval_trials(
    *,
    fixtures: tuple[EvalTaskFixture, ...] | None = None,
    fake_runner_script: EvalTrialFakeRunnerScript,
    seed: int = 0,
    campaign_manifest: EvalCampaignManifest | None = None,
    arms: tuple[EvalTrialArmDefinition, EvalTrialArmDefinition] | None = None,
    trial_indexes: tuple[int, ...] | None = None,
    trial_ids: tuple[str, ...] | None = None,
    campaign_store_root: str | None = None,
    artifact_roots_by_trial_arm: Mapping[tuple[str, EvalTrialArmId | str], str]
    | None = None,
    created_at: str = EVAL_TRIAL_DEFAULT_CREATED_AT,
) -> tuple[EvalTrialPlan, ...]:
    """Generate deterministic paired Pi-vs-Millforge trial plans."""
    campaign_manifest = campaign_manifest or default_eval_suite_campaign_manifest()
    if campaign_manifest.execution_mode != EvalSuiteExecutionMode.OFFLINE_FAKE:
        raise ValueError("eval-trial planning rejects live execution requests")
    if campaign_manifest.live_execution_admitted:
        raise ValueError("eval-trial planning rejects live execution requests")
    arms = arms or default_eval_trial_arm_definitions()
    _validate_admitted_planning_arms(arms)
    fixtures = fixtures or load_eval_task_fixtures()
    if not fixtures:
        raise ValueError("paired trial planning requires at least one fixture")
    if any(not fixture.fixture_hash for fixture in fixtures):
        raise ValueError("paired trial planning rejects missing fixture hashes")
    if trial_indexes is None:
        trial_indexes = tuple(range(len(fixtures)))
    if len(trial_indexes) != len(fixtures):
        raise ValueError("trial_indexes must match fixtures")
    if len(set(trial_indexes)) != len(trial_indexes):
        raise ValueError("duplicate fixture/arm/trial-index pairings are invalid")
    campaign_store_root = campaign_store_root or _default_campaign_store_root(
        campaign_manifest
    )
    _validate_relative_artifact_root(campaign_store_root)

    planned_trial_ids = trial_ids or tuple(
        _trial_id(
            campaign_id=campaign_manifest.campaign_id,
            fixture_id=fixture.fixture_id,
            trial_index=trial_index,
            seed=seed,
        )
        for fixture, trial_index in zip(fixtures, trial_indexes)
    )
    if len(planned_trial_ids) != len(fixtures):
        raise ValueError("trial_ids must match fixtures")
    if len(set(planned_trial_ids)) != len(planned_trial_ids):
        raise ValueError("duplicate trial IDs are invalid")

    plans: list[EvalTrialPlan] = []
    seen_pairings: set[tuple[str, str, int]] = set()
    for fixture, trial_index, planned_trial_id in zip(
        fixtures, trial_indexes, planned_trial_ids
    ):
        paired_seed = _paired_seed(
            seed=seed,
            fixture_id=fixture.fixture_id,
            trial_index=trial_index,
        )
        arm_order = _randomized_arm_order(paired_seed)
        per_trial_roots = {
            arm_id: root
            for (root_trial_id, arm_id), root in (
                artifact_roots_by_trial_arm or {}
            ).items()
            if root_trial_id == planned_trial_id
        }
        for arm_id in arm_order:
            pairing = (fixture.fixture_id, arm_id.value, trial_index)
            if pairing in seen_pairings:
                raise ValueError(
                    "duplicate fixture/arm/trial-index pairings are invalid"
                )
            seen_pairings.add(pairing)
        plans.append(
            default_eval_trial_plan(
                trial_plan_id=f"{planned_trial_id}.plan",
                trial_id=planned_trial_id,
                trial_index=trial_index,
                paired_seed=paired_seed,
                arm_order=arm_order,
                campaign_store_root=campaign_store_root,
                artifact_roots_by_arm=per_trial_roots,
                fixture=fixture,
                fake_runner_script=fake_runner_script,
                campaign_manifest=campaign_manifest,
                created_at=created_at,
            )
        )
    return tuple(plans)


def deny_eval_trial_live_execution(
    summary: str | None = None,
) -> EvalTrialInvalidDiagnostic:
    """Return the stable fail-closed diagnostic for live trial execution attempts."""
    return EvalTrialInvalidDiagnostic(
        diagnostic_code=EvalTrialInvalidDiagnosticCode.LIVE_EXECUTION_UNAVAILABLE,
        rule_id="eval_trials.live_execution.unavailable",
        summary=summary
        or "08B eval-trial contracts are offline-only and do not admit live execution.",
    )


def fixture_copy_unavailable_diagnostic(
    summary: str | None = None,
) -> EvalTrialInvalidDiagnostic:
    """Return the typed diagnostic for deferred physical fixture copying."""
    return EvalTrialInvalidDiagnostic(
        diagnostic_code=EvalTrialInvalidDiagnosticCode.FIXTURE_COPY_UNAVAILABLE,
        rule_id="eval_trials.fixture_copy.unavailable",
        summary=summary
        or (
            "Physical fixture copying is deferred; planning records a fresh "
            "fixture instance identity and snapshot hash without admitting live execution."
        ),
    )


def run_offline_fake_eval_trial(
    plan: EvalTrialPlan,
    *,
    fixture: EvalTaskFixture | None = None,
    execution_mode: EvalSuiteExecutionMode = EvalSuiteExecutionMode.OFFLINE_FAKE,
    live_execution_admitted: bool = False,
    allow_live_model_call: bool = False,
    allow_pi_execution: bool = False,
    allow_millforge_harness_execution: bool = False,
) -> EvalOfflineFakeTrialRun:
    """Execute one planned trial through the deterministic offline fake runner."""
    diagnostics = _offline_fake_runner_live_denials(
        execution_mode=execution_mode,
        live_execution_admitted=live_execution_admitted,
        allow_live_model_call=allow_live_model_call,
        allow_pi_execution=allow_pi_execution,
        allow_millforge_harness_execution=allow_millforge_harness_execution,
    )
    if diagnostics:
        raise ValueError(diagnostics[0].summary)
    if plan.campaign_manifest.execution_mode != EvalSuiteExecutionMode.OFFLINE_FAKE:
        raise ValueError("offline fake runner accepts only offline fake trial plans")
    if plan.campaign_manifest.live_execution_admitted:
        raise ValueError("offline fake runner rejects live execution admission")

    fixture = fixture or load_eval_task_fixture(plan.fixture_instance.fixture_id)
    if fixture.fixture_id != plan.fixture_instance.fixture_id:
        raise ValueError("offline fake runner fixture_id must match the planned trial")
    if fixture.fixture_hash != plan.fixture_instance.fixture_hash:
        raise ValueError("offline fake runner fixture_hash must match the trial plan")

    bundles_by_arm: dict[EvalTrialArmId, EvalFakeRunnerArtifactBundle] = {}
    results_by_arm: dict[EvalTrialArmId, EvalTrialExecutionResult] = {}
    for arm_plan in plan.arm_plans:
        bundle = _fake_runner_artifact_bundle(plan=plan, arm_plan=arm_plan)
        scorer_input = _fake_runner_scorer_input(
            plan=plan,
            fixture=fixture,
            arm_id=arm_plan.arm_id,
            artifact_hashes=bundle.artifact_hashes,
        )
        scorer_result = score_eval_trial(fixture, scorer_input)
        _validate_fake_script_outcome(plan.fake_runner_script, scorer_result)
        result = EvalTrialExecutionResult(
            trial_id=plan.trial_id,
            trial_plan_hash=plan.plan_hash,
            arm_id=arm_plan.arm_id,
            terminal_results=plan.fake_runner_script.terminal_results,
            artifact_bundle_hash=bundle.artifact_bundle_hash,
            scorer_input=scorer_input,
            scorer_result=scorer_result,
            live_execution_admitted=False,
            diagnostics=(),
        )
        bundles_by_arm[arm_plan.arm_id] = bundle
        results_by_arm[arm_plan.arm_id] = result

    ordered_bundles = (
        bundles_by_arm[EvalTrialArmId.EVAL_SMALL_PI],
        bundles_by_arm[EvalTrialArmId.EVAL_SMALL_MILLFORGE],
    )
    ordered_results = (
        results_by_arm[EvalTrialArmId.EVAL_SMALL_PI],
        results_by_arm[EvalTrialArmId.EVAL_SMALL_MILLFORGE],
    )
    record = EvalTrialRecord.model_construct(
        schema_version=EVAL_TRIAL_SCHEMA_VERSION,
        campaign_id=plan.campaign_manifest.campaign_id,
        task_fixture_id=plan.fixture_instance.fixture_id,
        task_category=fixture.category.value,
        trial_id=plan.trial_id,
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
        fixture_hash=plan.fixture_instance.fixture_hash,
        fixture_snapshot_hash=plan.fixture_instance.fixture_snapshot_hash,
        runner_summaries=_trial_runner_summaries(plan),
        compiled_harness_hashes=_compiled_harness_hashes_by_stage(
            plan.spec_07_readiness
        ),
        arm_results=ordered_results,
        final_outcomes={
            result.arm_id: result.scorer_result.final_outcome
            for result in ordered_results
        },
        scorer_result_hashes={
            result.arm_id: result.scorer_result.result_hash
            for result in ordered_results
        },
        scorer_public_summaries=_trial_scorer_public_summaries(ordered_results),
        artifact_manifest_hashes={
            bundle.arm_id: calculate_eval_artifact_manifest_sha256(
                bundle.artifact_manifest
            )
            for bundle in ordered_bundles
        },
        artifact_roots={
            bundle.arm_id: bundle.artifact_root for bundle in ordered_bundles
        },
        resource_summary=_trial_resource_summary(ordered_bundles),
        model_usage_summary=EvalTrialModelUsageSummary(),
        invalid_trial_explanations={
            result.arm_id: result.scorer_result.invalid_trial_explanation
            for result in ordered_results
            if result.scorer_result.invalid_trial_explanation
        },
        started_at=plan.created_at,
        ended_at=plan.created_at,
        created_at=EVAL_TRIAL_DEFAULT_CREATED_AT,
        record_hash_kind=EVAL_TRIAL_RECORD_HASH_KIND,
        record_hash="0" * 64,
    )
    record = EvalTrialRecord.model_validate(
        record.model_copy(
            update={"record_hash": calculate_eval_trial_record_hash(record)}
        )
    )
    return EvalOfflineFakeTrialRun(
        trial_id=plan.trial_id,
        trial_plan_hash=plan.plan_hash,
        artifact_bundles=ordered_bundles,
        execution_results=ordered_results,
        trial_record=record,
        live_execution_admitted=False,
        diagnostics=(),
    )


def append_eval_trial_record_to_campaign_store(
    store_root: str | Path,
    *,
    plan: EvalTrialPlan,
    record: EvalTrialRecord,
    plans: tuple[EvalTrialPlan, ...] | None = None,
) -> EvalTrialCampaignStoreAppendResult:
    """Append one complete newline-terminated record to a caller-selected store."""
    _validate_store_record_matches_plan(plan=plan, record=record)
    plan_set = _campaign_store_plan_set(plan=plan, plans=plans)
    campaign_dir = _campaign_store_dir(store_root, plan)
    manifest_path = campaign_dir / "manifest.json"
    plan_path = campaign_dir / "plan.json"
    trials_path = campaign_dir / "trials.jsonl"

    manifest = _campaign_store_manifest(plan=plan, record_hashes=())
    manifest_bytes = canonical_eval_trial_store_manifest_bytes(manifest)
    plan_bytes = _canonical_campaign_store_plan_set_bytes(plan_set)

    if manifest_path.exists() and manifest_path.read_bytes() != manifest_bytes:
        raise ValueError("campaign manifest mismatch; refusing to overwrite")
    if plan_path.exists() and plan_path.read_bytes() != plan_bytes:
        raise ValueError("campaign plan mismatch; refusing to overwrite")

    existing_records, diagnostics = _read_campaign_trial_log(trials_path)
    if diagnostics:
        raise ValueError(diagnostics[0].summary)
    if record.trial_id in {existing.trial_id for existing in existing_records}:
        raise ValueError("duplicate trial IDs are rejected by append-only stores")

    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / "artifacts").mkdir(exist_ok=True)
    if not manifest_path.exists():
        manifest_path.write_bytes(manifest_bytes)
    if not plan_path.exists():
        plan_path.write_bytes(plan_bytes)
    with trials_path.open("ab") as handle:
        handle.write(canonical_eval_trial_record_bytes(record))

    records = existing_records + (_record_summary(record),)
    index = _campaign_resume_index(plan=plan, records=records, plans=plan_set)
    return EvalTrialCampaignStoreAppendResult(
        campaign_store_root=plan.campaign_store_root,
        manifest=manifest,
        plan=plan,
        appended_trial_id=record.trial_id,
        appended_record_hash=record.record_hash,
        completed_trial_ids=tuple(item.trial_id for item in records),
        resume_index=index,
    )


def resume_eval_trial_campaign_store(
    store_root: str | Path,
    *,
    plan: EvalTrialPlan,
    plans: tuple[EvalTrialPlan, ...] | None = None,
) -> EvalTrialCampaignStoreResumeResult:
    """Reconstruct append-only campaign progress without mutating trial records."""
    plan_set = _campaign_store_plan_set(plan=plan, plans=plans)
    campaign_dir = _campaign_store_dir(store_root, plan)
    manifest_path = campaign_dir / "manifest.json"
    plan_path = campaign_dir / "plan.json"
    trials_path = campaign_dir / "trials.jsonl"
    index_path = campaign_dir / "index.json"

    diagnostics: list[EvalTrialInvalidDiagnostic] = []
    manifest = _campaign_store_manifest(plan=plan, record_hashes=())
    if manifest_path.exists() and manifest_path.read_bytes() != (
        canonical_eval_trial_store_manifest_bytes(manifest)
    ):
        diagnostics.append(
            _store_diagnostic(
                EvalTrialInvalidDiagnosticCode.STORE_MANIFEST_HASH_MISMATCH,
                "eval_trials.store.manifest_mismatch",
                "campaign manifest does not match the expected immutable manifest",
            )
        )
    if plan_path.exists() and plan_path.read_bytes() != (
        _canonical_campaign_store_plan_set_bytes(plan_set)
    ):
        diagnostics.append(
            _store_diagnostic(
                EvalTrialInvalidDiagnosticCode.PLAN_HASH_MISMATCH,
                "eval_trials.store.plan_mismatch",
                "campaign plan does not match the expected immutable plan",
            )
        )
    records, log_diagnostics = _read_campaign_trial_log(trials_path)
    diagnostics.extend(log_diagnostics)
    duplicate_ids = _duplicate_trial_ids(records)
    diagnostics.extend(
        _store_diagnostic(
            EvalTrialInvalidDiagnosticCode.TRIAL_LOG_DUPLICATE_TRIAL_ID,
            "eval_trials.store.duplicate_trial_id",
            f"duplicate trial ID in append-only log: {trial_id}",
        )
        for trial_id in duplicate_ids
    )

    index: EvalTrialResumeIndex | None = None
    if not diagnostics:
        campaign_dir.mkdir(parents=True, exist_ok=True)
        (campaign_dir / "artifacts").mkdir(exist_ok=True)
        if not manifest_path.exists():
            manifest_path.write_bytes(
                canonical_eval_trial_store_manifest_bytes(manifest)
            )
        if not plan_path.exists():
            plan_path.write_bytes(_canonical_campaign_store_plan_set_bytes(plan_set))
        index = _campaign_resume_index(plan=plan, records=records, plans=plan_set)
        index_bytes = canonical_eval_trial_resume_index_bytes(index)
        if index_path.exists():
            if index_path.read_bytes() != index_bytes:
                diagnostics.append(
                    _store_diagnostic(
                        EvalTrialInvalidDiagnosticCode.RESUME_INDEX_HASH_MISMATCH,
                        "eval_trials.store.resume_index_mismatch",
                        "campaign resume index does not match reconstructed records",
                    )
                )
                index = None
        else:
            index_path.write_bytes(index_bytes)

    completed = tuple(record.trial_id for record in records)
    pending = tuple(
        item.trial_id for item in plan_set if item.trial_id not in completed
    )
    return EvalTrialCampaignStoreResumeResult(
        campaign_store_root=plan.campaign_store_root,
        plan=plan,
        completed_trial_ids=completed,
        pending_trial_ids=pending,
        records=records,
        resume_index=index,
        diagnostics=tuple(diagnostics),
    )


def canonical_eval_trial_plan_bytes(plan: EvalTrialPlan) -> bytes:
    """Return canonical ASCII JSON bytes for a trial plan."""
    return _canonical_eval_trial_bytes(plan.model_dump(mode="json"))


def canonical_eval_fake_runner_artifact_bundle_bytes(
    bundle: EvalFakeRunnerArtifactBundle,
) -> bytes:
    """Return canonical ASCII JSON bytes for an artifact bundle."""
    return _canonical_eval_trial_bytes(bundle.model_dump(mode="json"))


def canonical_eval_trial_record_bytes(record: EvalTrialRecord) -> bytes:
    """Return canonical ASCII JSON bytes for a trial record."""
    return _canonical_eval_trial_bytes(record.model_dump(mode="json"))


def canonical_eval_trial_store_manifest_bytes(
    manifest: EvalTrialStoreManifest,
) -> bytes:
    """Return canonical ASCII JSON bytes for a store manifest."""
    return _canonical_eval_trial_bytes(manifest.model_dump(mode="json"))


def canonical_eval_trial_resume_index_bytes(index: EvalTrialResumeIndex) -> bytes:
    """Return canonical ASCII JSON bytes for a resume index."""
    return _canonical_eval_trial_bytes(index.model_dump(mode="json"))


def calculate_eval_trial_plan_hash(plan: EvalTrialPlan) -> str:
    payload = plan.model_dump(mode="json")
    payload.pop("plan_hash", None)
    return hashlib.sha256(_canonical_eval_trial_bytes(payload)).hexdigest()


def calculate_eval_fake_runner_artifact_bundle_hash(
    bundle: EvalFakeRunnerArtifactBundle,
) -> str:
    payload = bundle.model_dump(mode="json")
    payload.pop("artifact_bundle_hash", None)
    return hashlib.sha256(_canonical_eval_trial_bytes(payload)).hexdigest()


def calculate_eval_trial_record_hash(record: EvalTrialRecord) -> str:
    payload = record.model_dump(mode="json")
    payload.pop("record_hash", None)
    return hashlib.sha256(_canonical_eval_trial_bytes(payload)).hexdigest()


def calculate_eval_trial_store_manifest_hash(
    manifest: EvalTrialStoreManifest,
) -> str:
    payload = manifest.model_dump(mode="json")
    payload.pop("store_manifest_hash", None)
    return hashlib.sha256(_canonical_eval_trial_bytes(payload)).hexdigest()


def calculate_eval_trial_resume_index_hash(index: EvalTrialResumeIndex) -> str:
    payload = index.model_dump(mode="json")
    payload.pop("resume_index_hash", None)
    return hashlib.sha256(_canonical_eval_trial_bytes(payload)).hexdigest()


def _campaign_store_plan_set(
    *,
    plan: EvalTrialPlan,
    plans: tuple[EvalTrialPlan, ...] | None,
) -> tuple[EvalTrialPlan, ...]:
    plan_set = plans or (plan,)
    if not plan_set:
        raise ValueError("campaign plan set must include at least one trial plan")
    _validate_campaign_store_root(plan)
    if not any(
        item.trial_id == plan.trial_id and item.plan_hash == plan.plan_hash
        for item in plan_set
    ):
        raise ValueError("campaign plan set must include the active trial plan")
    campaign_manifest_hash = plan.campaign_manifest.campaign_manifest_hash
    campaign_store_root = plan.campaign_store_root
    trial_ids = tuple(item.trial_id for item in plan_set)
    plan_hashes = tuple(item.plan_hash for item in plan_set)
    if len(set(trial_ids)) != len(trial_ids):
        raise ValueError("campaign plan set rejects duplicate trial IDs")
    if len(set(plan_hashes)) != len(plan_hashes):
        raise ValueError("campaign plan set rejects duplicate plan hashes")
    for item in plan_set:
        _validate_campaign_store_root(item)
        if item.campaign_store_root != campaign_store_root:
            raise ValueError("campaign plan set must share one campaign store root")
        if item.campaign_manifest.campaign_manifest_hash != campaign_manifest_hash:
            raise ValueError("campaign plan set must share one campaign manifest")
    return tuple(
        sorted(
            plan_set,
            key=lambda item: (item.trial_index, item.trial_id, item.plan_hash),
        )
    )


def _canonical_campaign_store_plan_set_bytes(
    plans: tuple[EvalTrialPlan, ...],
) -> bytes:
    first = plans[0]
    return _canonical_eval_trial_bytes(
        {
            "schema_version": EVAL_TRIAL_SCHEMA_VERSION,
            "campaign_manifest_hash": (first.campaign_manifest.campaign_manifest_hash),
            "campaign_store_root": first.campaign_store_root,
            "plan_hashes": tuple(item.plan_hash for item in plans),
            "plans": tuple(item.model_dump(mode="json") for item in plans),
        }
    )


def _campaign_store_dir(store_root: str | Path, plan: EvalTrialPlan) -> Path:
    _validate_campaign_store_root(plan)
    return Path(store_root) / PurePosixPath(plan.campaign_store_root)


def _validate_campaign_store_root(plan: EvalTrialPlan) -> None:
    _validate_relative_artifact_root(plan.campaign_store_root)
    parts = PurePosixPath(plan.campaign_store_root).parts
    if parts != ("eval", "campaigns", plan.campaign_manifest.campaign_id):
        raise ValueError("campaign store root must be eval/campaigns/<campaign-id>")


def _validate_store_record_matches_plan(
    *,
    plan: EvalTrialPlan,
    record: EvalTrialRecord,
) -> None:
    _validate_campaign_store_root(plan)
    if record.campaign_id != plan.campaign_manifest.campaign_id:
        raise ValueError("trial record campaign_id must match campaign plan")
    if record.trial_id != plan.trial_id:
        raise ValueError("trial record trial_id must match campaign plan")
    if record.trial_plan_hash != plan.plan_hash:
        raise ValueError("trial record plan hash must match campaign plan")
    if record.campaign_manifest_hash != plan.campaign_manifest.campaign_manifest_hash:
        raise ValueError("trial record campaign manifest hash must match plan")
    expected_record_values = {
        "model_manifest_hash": plan.campaign_manifest.model_manifest_hash,
        "workflow_graph_hash": plan.campaign_manifest.workflow_graph_hash,
        "fixture_pack_hash": plan.fixture_instance.fixture_pack_hash,
        "fixture_instance_id": plan.fixture_instance.fixture_instance_id,
        "fixture_id": plan.fixture_instance.fixture_id,
        "fixture_hash": plan.fixture_instance.fixture_hash,
        "fixture_snapshot_hash": plan.fixture_instance.fixture_snapshot_hash,
        "started_at": plan.created_at,
        "ended_at": plan.created_at,
    }
    for field_name, expected_value in expected_record_values.items():
        if getattr(record, field_name) != expected_value:
            raise ValueError(f"trial record {field_name} must match campaign plan")
    for artifact_root in record.artifact_roots.values():
        _validate_artifact_root_under_campaign(
            artifact_root,
            plan.campaign_store_root,
        )


def _campaign_store_manifest(
    *,
    plan: EvalTrialPlan,
    record_hashes: tuple[str, ...],
) -> EvalTrialStoreManifest:
    manifest = EvalTrialStoreManifest.model_construct(
        schema_version=EVAL_TRIAL_SCHEMA_VERSION,
        store_manifest_id=f"{plan.campaign_manifest.campaign_id}.store.v1",
        campaign_manifest_hash=plan.campaign_manifest.campaign_manifest_hash,
        record_hashes=record_hashes,
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


def _campaign_resume_index(
    *,
    plan: EvalTrialPlan,
    records: tuple[EvalTrialCampaignStoreRecordSummary, ...],
    plans: tuple[EvalTrialPlan, ...],
) -> EvalTrialResumeIndex:
    completed_plan_hashes = {record.trial_plan_hash for record in records}
    index = EvalTrialResumeIndex.model_construct(
        schema_version=EVAL_TRIAL_SCHEMA_VERSION,
        resume_index_id=f"{plan.campaign_manifest.campaign_id}.resume.v1",
        campaign_manifest_hash=plan.campaign_manifest.campaign_manifest_hash,
        completed_trial_record_hashes=tuple(record.record_hash for record in records),
        pending_trial_plan_hashes=tuple(
            item.plan_hash
            for item in plans
            if item.plan_hash not in completed_plan_hashes
        ),
        invalid_trial_diagnostics=(),
        resume_index_hash_kind=EVAL_TRIAL_RESUME_INDEX_HASH_KIND,
        resume_index_hash="0" * 64,
    )
    return EvalTrialResumeIndex.model_validate(
        index.model_copy(
            update={"resume_index_hash": calculate_eval_trial_resume_index_hash(index)}
        )
    )


def _read_campaign_trial_log(
    trials_path: Path,
) -> tuple[
    tuple[EvalTrialCampaignStoreRecordSummary, ...],
    tuple[EvalTrialInvalidDiagnostic, ...],
]:
    if not trials_path.exists():
        return (), ()
    data = trials_path.read_bytes()
    if data == b"":
        return (), ()

    records: list[EvalTrialCampaignStoreRecordSummary] = []
    diagnostics: list[EvalTrialInvalidDiagnostic] = []
    lines = data.splitlines(keepends=True)
    for index, line in enumerate(lines, start=1):
        final_line = index == len(lines)
        has_newline = line.endswith(b"\n")
        payload = line[:-1] if has_newline else line
        if not payload:
            continue
        if not has_newline and final_line:
            diagnostics.append(
                _store_diagnostic(
                    EvalTrialInvalidDiagnosticCode.TRIAL_LOG_MISSING_FINAL_NEWLINE,
                    "eval_trials.store.missing_final_newline",
                    "trials.jsonl is missing the required final newline",
                )
            )
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            diagnostics.append(
                _store_diagnostic(
                    EvalTrialInvalidDiagnosticCode.TRIAL_LOG_PARTIAL_TRAILING_RECORD
                    if final_line and not has_newline
                    else EvalTrialInvalidDiagnosticCode.TRIAL_LOG_MALFORMED_TRAILING_LINE,
                    "eval_trials.store.partial_trailing_record"
                    if final_line and not has_newline
                    else "eval_trials.store.malformed_trailing_line",
                    "trials.jsonl contains a partial trailing record"
                    if final_line and not has_newline
                    else "trials.jsonl contains malformed JSON",
                )
            )
            continue
        if not has_newline and final_line:
            continue
        try:
            records.append(_public_trial_record_from_json(parsed))
        except ValueError:
            diagnostics.append(
                _store_diagnostic(
                    EvalTrialInvalidDiagnosticCode.TRIAL_LOG_INVALID_RECORD,
                    "eval_trials.store.invalid_record",
                    "trials.jsonl contains an invalid trial record",
                )
            )
    return tuple(records), tuple(diagnostics)


def _public_trial_record_from_json(value: Any) -> EvalTrialCampaignStoreRecordSummary:
    if not isinstance(value, dict):
        raise ValueError("trial log record must be a JSON object")
    for field_name in (
        "campaign_id",
        "campaign_manifest_hash",
        "record_hash",
        "schema_version",
        "trial_id",
        "trial_plan_hash",
    ):
        if field_name not in value:
            raise ValueError("trial log record is missing required public fields")
    if value["schema_version"] != EVAL_TRIAL_SCHEMA_VERSION:
        raise ValueError("trial log record has unsupported schema version")
    for field_name in ("campaign_manifest_hash", "record_hash", "trial_plan_hash"):
        if not isinstance(value[field_name], str):
            raise ValueError("trial log record hash fields must be strings")
        _validate_sha256(value[field_name])
    payload = dict(value)
    record_hash = payload.pop("record_hash")
    if hashlib.sha256(_canonical_eval_trial_bytes(payload)).hexdigest() != record_hash:
        raise ValueError("trial log record hash does not match public payload")
    if not isinstance(value["trial_id"], str):
        raise ValueError("trial log trial_id must be a string")
    return EvalTrialCampaignStoreRecordSummary(
        trial_id=value["trial_id"],
        trial_plan_hash=value["trial_plan_hash"],
        record_hash=record_hash,
    )


def _record_summary(record: EvalTrialRecord) -> EvalTrialCampaignStoreRecordSummary:
    return EvalTrialCampaignStoreRecordSummary(
        trial_id=record.trial_id,
        trial_plan_hash=record.trial_plan_hash,
        record_hash=record.record_hash,
    )


def _duplicate_trial_ids(
    records: tuple[EvalTrialCampaignStoreRecordSummary, ...],
) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for record in records:
        if record.trial_id in seen and record.trial_id not in duplicates:
            duplicates.append(record.trial_id)
        seen.add(record.trial_id)
    return tuple(duplicates)


def _store_diagnostic(
    code: EvalTrialInvalidDiagnosticCode,
    rule_id: str,
    summary: str,
) -> EvalTrialInvalidDiagnostic:
    return EvalTrialInvalidDiagnostic(
        diagnostic_code=code,
        rule_id=rule_id,
        summary=summary,
    )


def _offline_fake_runner_live_denials(
    *,
    execution_mode: EvalSuiteExecutionMode,
    live_execution_admitted: bool,
    allow_live_model_call: bool,
    allow_pi_execution: bool,
    allow_millforge_harness_execution: bool,
) -> tuple[EvalTrialInvalidDiagnostic, ...]:
    diagnostics: list[EvalTrialInvalidDiagnostic] = []
    if execution_mode != EvalSuiteExecutionMode.OFFLINE_FAKE:
        diagnostics.append(
            deny_eval_trial_live_execution(
                "Offline fake trial runner rejects live runner execution mode."
            )
        )
    if live_execution_admitted:
        diagnostics.append(
            deny_eval_trial_live_execution(
                "Offline fake trial runner rejects live execution admission."
            )
        )
    if allow_live_model_call:
        diagnostics.append(
            deny_eval_trial_live_execution(
                "Offline fake trial runner rejects model call execution."
            )
        )
    if allow_pi_execution:
        diagnostics.append(
            deny_eval_trial_live_execution(
                "Offline fake trial runner rejects Pi execution."
            )
        )
    if allow_millforge_harness_execution:
        diagnostics.append(
            deny_eval_trial_live_execution(
                "Offline fake trial runner rejects Millforge harness execution."
            )
        )
    return tuple(diagnostics)


def _fake_runner_artifact_bundle(
    *,
    plan: EvalTrialPlan,
    arm_plan: EvalTrialArmPlan,
) -> EvalFakeRunnerArtifactBundle:
    public_artifact_ids = _fake_runner_public_artifact_ids()
    entries = tuple(
        _fake_runner_artifact_manifest_entry(
            trial_id=plan.trial_id,
            arm_id=arm_plan.arm_id,
            artifact_id=artifact_id,
            script_kind=plan.fake_runner_script.script_kind,
        )
        for artifact_id in public_artifact_ids
    )
    artifact_manifest = EvalArtifactManifestArtifact(
        trial_id=plan.trial_id,
        created_by="offline_fake_runner",
        summary="offline fake runner artifact manifest",
        entries=entries,
    )
    manifest_hash = calculate_eval_artifact_manifest_sha256(artifact_manifest)
    artifact_hashes = tuple(
        EvalHashRecord(hash_kind="eval_artifact_sha256_v1", sha256=entry.sha256)
        for entry in entries
    ) + (
        EvalHashRecord(
            hash_kind="eval_artifact_manifest_sha256_v1",
            sha256=manifest_hash,
        ),
    )
    bundle = EvalFakeRunnerArtifactBundle.model_construct(
        schema_version=EVAL_TRIAL_SCHEMA_VERSION,
        artifact_bundle_id=f"{plan.trial_id}.{arm_plan.arm_id.value}.bundle.v1",
        trial_plan_hash=plan.plan_hash,
        arm_id=arm_plan.arm_id,
        artifact_root=arm_plan.artifact_root,
        artifact_manifest=artifact_manifest,
        artifact_hashes=artifact_hashes,
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


def _fake_runner_artifact_manifest_entry(
    *,
    trial_id: str,
    arm_id: EvalTrialArmId,
    artifact_id: EvalArtifactId,
    script_kind: EvalFakeOutcomeScriptKind,
) -> EvalArtifactManifestEntry:
    layout = eval_artifact_layout_entry(artifact_id)
    payload = {
        "artifact_id": artifact_id.value,
        "arm_id": arm_id.value,
        "script_kind": script_kind.value,
        "trial_id": trial_id,
    }
    artifact_bytes = _canonical_eval_trial_bytes(payload)
    return EvalArtifactManifestEntry(
        artifact_id=artifact_id,
        layout_path=layout.layout_path,
        media_type=layout.media_type,
        schema_id=layout.schema_id,
        byte_size=len(artifact_bytes),
        sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        producer="offline_fake_runner",
        model_visible=layout.model_visible,
    )


def _fake_runner_scorer_input(
    *,
    plan: EvalTrialPlan,
    fixture: EvalTaskFixture,
    arm_id: EvalTrialArmId,
    artifact_hashes: tuple[EvalHashRecord, ...],
) -> EvalScorerInput:
    script_kind = plan.fake_runner_script.script_kind
    artifact_ids = tuple(
        artifact_id.value for artifact_id in _fake_runner_public_artifact_ids()
    )
    terminal_results = tuple(
        terminal.value for terminal in plan.fake_runner_script.terminal_results
    )
    visible_passed = script_kind not in {
        EvalFakeOutcomeScriptKind.FALSE_CLOSURE,
        EvalFakeOutcomeScriptKind.FALSE_SUCCESS_WITHOUT_CLOSURE,
    }
    hidden_passed = visible_passed
    scorer_input = EvalScorerInput.model_construct(
        trial_id=plan.trial_id,
        fixture_id=fixture.fixture_id,
        fixture_hash=fixture.fixture_hash
        if script_kind is not EvalFakeOutcomeScriptKind.INVALID_TRIAL
        else "f" * 64,
        final_workspace_hash=_fake_workspace_hash(
            trial_id=plan.trial_id,
            arm_id=arm_id,
            script_kind=script_kind,
        ),
        path_limited_workspace_hashes=(),
        public_artifact_hashes=artifact_hashes,
        required_public_artifact_ids=artifact_ids,
        provided_public_artifact_ids=artifact_ids,
        malformed_artifact_ids=(),
        stage_terminal_results=terminal_results,
        capability_audit=EvalCapabilityAuditSummary(
            capability_violation=False,
            denied_capability_ids=(),
            summary="offline fake runner used no denied capabilities",
        ),
        visible_check_results=tuple(
            EvalCheckResult(check_id=check.check_id, passed=visible_passed)
            for check in fixture.visible_checks
        ),
        hidden_check_results=tuple(
            EvalCheckResult(check_id=check.check_id, passed=hidden_passed)
            for check in fixture.hidden_checks
        ),
        claimed_mutation_present=True,
        unauthorized_mutation=False,
        checker_public_evidence_valid=True,
        runtime_failure=script_kind is EvalFakeOutcomeScriptKind.RUNTIME_FAILURE,
        provider_failure=script_kind is EvalFakeOutcomeScriptKind.PROVIDER_FAILURE,
        invalid_trial_explanation=(
            "offline fake runner scripted invalid trial"
            if script_kind is EvalFakeOutcomeScriptKind.INVALID_TRIAL
            else None
        ),
        scorer_input_hash_kind="eval_suite_scorer_input_sha256_v1",
        scorer_input_hash="0" * 64,
    )
    return EvalScorerInput.model_validate(
        scorer_input.model_copy(
            update={"scorer_input_hash": calculate_eval_scorer_input_hash(scorer_input)}
        )
    )


def _validate_fake_script_outcome(
    script: EvalTrialFakeRunnerScript,
    scorer_result: EvalScorerResult,
) -> None:
    if scorer_result.final_outcome != script.expected_outcome:
        raise ValueError(
            "offline fake script expected_outcome does not match scorer result"
        )
    if (
        script.script_kind is EvalFakeOutcomeScriptKind.FALSE_SUCCESS_WITHOUT_CLOSURE
        and not scorer_result.false_success
    ):
        raise ValueError("offline fake script did not produce false success")


def _trial_runner_summaries(
    plan: EvalTrialPlan,
) -> Mapping[EvalTrialArmId, EvalTrialRunnerRecordSummary]:
    return _freeze_trial_mapping(
        {
            arm_plan.arm_id: EvalTrialRunnerRecordSummary(
                arm_id=arm_plan.arm_id,
                runner_kind=arm_plan.runner_kind,
                runner_descriptor_id=arm_plan.runner_descriptor_id,
                runner_descriptor_version="eval-trial.runner-descriptor.v1",
                baseline_pi_runtime_hash=arm_plan.baseline_pi_runtime_hash,
                baseline_runtime_diagnostic=arm_plan.baseline_runtime_diagnostic,
            )
            for arm_plan in plan.arm_plans
        }
    )


def _trial_scorer_public_summaries(
    results: tuple[EvalTrialExecutionResult, EvalTrialExecutionResult],
) -> Mapping[EvalTrialArmId, EvalTrialScorerPublicSummary]:
    return _freeze_trial_mapping(
        {
            result.arm_id: EvalTrialScorerPublicSummary(
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
            for result in results
        }
    )


def _trial_resource_summary(
    bundles: tuple[EvalFakeRunnerArtifactBundle, EvalFakeRunnerArtifactBundle],
) -> EvalTrialResourceSummary:
    hashes = tuple(record for bundle in bundles for record in bundle.artifact_hashes)
    return EvalTrialResourceSummary(
        artifact_count=sum(len(bundle.artifact_hashes) for bundle in bundles),
        artifact_bytes=sum(
            entry.byte_size
            for bundle in bundles
            for entry in bundle.artifact_manifest.entries
        ),
        resource_artifact_hashes=hashes,
        zero_external_usage=all(bundle.zero_external_usage for bundle in bundles),
    )


def _fake_runner_public_artifact_ids() -> tuple[EvalArtifactId, ...]:
    return (
        EvalArtifactId.TASK,
        EvalArtifactId.ACCEPTANCE_CHECKS,
        EvalArtifactId.PLAN,
        EvalArtifactId.WORKSPACE_DIFF,
        EvalArtifactId.PATCH_SUMMARY,
        EvalArtifactId.TEST_RESULTS,
        EvalArtifactId.CHECKER_VERDICT,
        EvalArtifactId.ARBITER_VERDICT,
        EvalArtifactId.STAGE_RESULT,
        EvalArtifactId.EVENT_LOG,
        EvalArtifactId.RESOURCE_USAGE,
        EvalArtifactId.MODEL_USAGE,
        EvalArtifactId.VALIDATOR_RESULT,
    )


def _fake_workspace_hash(
    *,
    trial_id: str,
    arm_id: EvalTrialArmId,
    script_kind: EvalFakeOutcomeScriptKind,
) -> str:
    return hashlib.sha256(
        _canonical_eval_trial_bytes(
            {
                "arm_id": arm_id.value,
                "script_kind": script_kind.value,
                "trial_id": trial_id,
            }
        )
    ).hexdigest()


def _arm_definition(
    descriptor: EvalModeDescriptor,
    arm_id: EvalTrialArmId,
) -> EvalTrialArmDefinition:
    return EvalTrialArmDefinition(
        arm_id=arm_id,
        mode_id=descriptor.mode_id,
        descriptor_fingerprint=descriptor.descriptor_fingerprint,
        fairness_fingerprint=descriptor.fairness_fingerprint,
        runner_kind=descriptor.runner_bindings[0].runner_kind.value,
        descriptor=descriptor,
    )


def _default_eval_trial_arm_plans(
    *,
    trial_id: str,
    trial_index: int,
    paired_seed: int,
    campaign_store_root: str,
    campaign_manifest: EvalCampaignManifest,
    arms: tuple[EvalTrialArmDefinition, EvalTrialArmDefinition],
    arm_order: tuple[EvalTrialArmId, EvalTrialArmId],
    fixture: EvalTaskFixture,
    readiness: EvalPresetReadinessReport,
    artifact_roots_by_arm: Mapping[EvalTrialArmId | str, str] | None = None,
) -> tuple[EvalTrialArmPlan, EvalTrialArmPlan]:
    roots = artifact_roots_by_arm or {}
    compiled_hashes = _compiled_harness_hashes_by_stage(readiness)
    hidden_check_set_ids = tuple(check.check_id for check in fixture.hidden_checks)
    hidden_check_set_hash = _hidden_check_set_hash(hidden_check_set_ids)
    visible_acceptance_hash = _visible_acceptance_criteria_hash(
        fixture.visible_acceptance_criteria
    )
    plans: list[EvalTrialArmPlan] = []
    for arm in arms:
        root = roots.get(arm.arm_id) or roots.get(arm.arm_id.value)
        root = root or _default_artifact_root(
            campaign_store_root=campaign_store_root,
            trial_id=trial_id,
            arm_id=arm.arm_id,
        )
        plans.append(
            EvalTrialArmPlan(
                trial_id=trial_id,
                trial_index=trial_index,
                arm_id=arm.arm_id,
                arm_order_index=arm_order.index(arm.arm_id),
                paired_seed=paired_seed,
                artifact_root=root,
                runner_kind=arm.runner_kind,
                runner_descriptor_id=arm.descriptor_fingerprint,
                campaign_manifest_hash=campaign_manifest.campaign_manifest_hash,
                model_manifest_hash=campaign_manifest.model_manifest_hash,
                workflow_graph_hash=campaign_manifest.workflow_graph_hash,
                fixture_pack_hash=campaign_manifest.fixture_pack_hash,
                fixture_id=fixture.fixture_id,
                fixture_hash=fixture.fixture_hash,
                visible_acceptance_criteria_hash=visible_acceptance_hash,
                hidden_check_set_ids=hidden_check_set_ids,
                hidden_check_set_hash=hidden_check_set_hash,
                scorer_version=campaign_manifest.scorer_version,
                treatment_compiled_harness_hashes=compiled_hashes
                if arm.arm_id is EvalTrialArmId.EVAL_SMALL_MILLFORGE
                else {},
                baseline_pi_runtime_hash=None,
                baseline_runtime_diagnostic=EvalTrialInvalidDiagnostic(
                    diagnostic_code=(
                        EvalTrialInvalidDiagnosticCode.LIVE_EXECUTION_UNAVAILABLE
                    ),
                    rule_id="eval_trials.pi_runtime.deferred",
                    summary="Pi runtime hash is deferred for offline planning.",
                )
                if arm.arm_id is EvalTrialArmId.EVAL_SMALL_PI
                else None,
            )
        )
    return (plans[0], plans[1])


def _validate_admitted_planning_arms(
    arms: tuple[EvalTrialArmDefinition, EvalTrialArmDefinition],
) -> None:
    if tuple(arm.arm_id.value for arm in arms) != EVAL_TRIAL_ADMITTED_ARM_IDS:
        raise ValueError("paired trial planning rejects unsupported arms")


def _default_campaign_store_root(campaign_manifest: EvalCampaignManifest) -> str:
    return f"eval/campaigns/{campaign_manifest.campaign_id}"


def _default_artifact_root(
    *,
    campaign_store_root: str,
    trial_id: str,
    arm_id: EvalTrialArmId,
) -> str:
    _validate_relative_artifact_root(campaign_store_root)
    return f"artifacts/{trial_id}/{arm_id.value}"


def _trial_id(
    *,
    campaign_id: str,
    fixture_id: str,
    trial_index: int,
    seed: int,
) -> str:
    digest = hashlib.sha256(
        _canonical_eval_trial_bytes(
            {
                "campaign_id": campaign_id,
                "fixture_id": fixture_id,
                "seed": seed,
                "trial_index": trial_index,
            }
        )
    ).hexdigest()[:12]
    safe_fixture = re.sub(r"[^a-zA-Z0-9_.-]+", "_", fixture_id)
    return f"{campaign_id}.trial.{trial_index:04d}.{safe_fixture}.{digest}"


def _paired_seed(*, seed: int, fixture_id: str, trial_index: int) -> int:
    digest = hashlib.sha256(
        _canonical_eval_trial_bytes(
            {"fixture_id": fixture_id, "seed": seed, "trial_index": trial_index}
        )
    ).hexdigest()
    return int(digest[:16], 16)


def _randomized_arm_order(
    paired_seed: int,
) -> tuple[EvalTrialArmId, EvalTrialArmId]:
    arms = [EvalTrialArmId.EVAL_SMALL_PI, EvalTrialArmId.EVAL_SMALL_MILLFORGE]
    random.Random(paired_seed).shuffle(arms)
    return (arms[0], arms[1])


def _fixture_instance_id(
    *,
    trial_id: str,
    trial_index: int,
    fixture_id: str,
    paired_seed: int,
) -> str:
    digest = hashlib.sha256(
        _canonical_eval_trial_bytes(
            {
                "fixture_id": fixture_id,
                "paired_seed": paired_seed,
                "trial_id": trial_id,
                "trial_index": trial_index,
            }
        )
    ).hexdigest()[:16]
    return f"{trial_id}.fixture_instance.{digest}"


def _fixture_snapshot_hash(
    *,
    fixture: EvalTaskFixture,
    fixture_instance_id: str,
    paired_seed: int,
) -> str:
    return hashlib.sha256(
        _canonical_eval_trial_bytes(
            {
                "fixture_hash": fixture.fixture_hash,
                "fixture_id": fixture.fixture_id,
                "fixture_instance_id": fixture_instance_id,
                "paired_seed": paired_seed,
            }
        )
    ).hexdigest()


def _visible_acceptance_criteria_hash(criteria: tuple[str, ...]) -> str:
    return hashlib.sha256(
        _canonical_eval_trial_bytes({"visible_acceptance_criteria": criteria})
    ).hexdigest()


def _hidden_check_set_hash(check_ids: tuple[str, ...]) -> str:
    return hashlib.sha256(
        _canonical_eval_trial_bytes({"hidden_check_set_ids": check_ids})
    ).hexdigest()


def _compiled_harness_hashes_by_stage(
    readiness: EvalPresetReadinessReport,
) -> Mapping[str, str]:
    required_stage_ids = tuple(stage_id.value for stage_id in EvalStageId)
    compiled = {
        plan.stage_id: plan.compiled_sha256 for plan in readiness.compiled_plans
    }
    if tuple(compiled) != required_stage_ids:
        raise ValueError(
            "Spec 07E readiness must include Planner, Builder, Checker, Arbiter"
        )
    return _freeze_trial_mapping(compiled)


def _reject_fake_pi_runtime_hash(value: str) -> None:
    if value in {"0" * 64, "f" * 64} or len(set(value)) == 1:
        raise ValueError("Pi baseline runtime hash must not be a fake concrete hash")


def _validate_relative_artifact_root(path: str) -> None:
    if (
        not path
        or _WINDOWS_ABSOLUTE_PATH.search(path)
        or _POSIX_ABSOLUTE_PATH.search(path)
        or _USER_HOME_PATH.search(path)
    ):
        raise ValueError("artifact roots must be stable relative paths")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("artifact roots must be stable relative paths")
    if "\\" in path:
        raise ValueError("artifact roots must use relative POSIX paths")


def _validate_artifact_root_under_campaign(
    artifact_root: str,
    campaign_store_root: str,
) -> None:
    _validate_relative_artifact_root(artifact_root)
    _validate_relative_artifact_root(campaign_store_root)
    artifact_parts = PurePosixPath(artifact_root).parts
    if len(artifact_parts) < 2 or artifact_parts[0] != "artifacts":
        raise ValueError("artifact roots must stay inside the campaign store")


def _canonical_eval_trial_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).replace("\r\n", "\n")
        + "\n"
    ).encode("ascii")


def _validate_sha256(value: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("expected lowercase sha256 hex digest")


def _reject_forbidden_material(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            _reject_secret_like_field_name(key_text)
            _reject_forbidden_material(key_text)
            _reject_forbidden_material(child)
        return
    if isinstance(value, (tuple, list, set, frozenset)):
        for child in value:
            _reject_forbidden_material(child)
        return
    if isinstance(value, BaseModel):
        _reject_forbidden_material(value.model_dump(mode="json"))
        return
    if isinstance(value, Enum):
        _reject_forbidden_material(value.value)
        return
    if isinstance(value, str):
        lowered = value.lower()
        if any(token in lowered for token in _DENIED_TEXT_TOKENS):
            raise ValueError("eval-trial payload contains forbidden private material")
        if any(pattern.search(value) for pattern in _CREDENTIAL_VALUE_PATTERNS):
            raise ValueError("eval-trial payload contains credential-shaped API key")
        if _ENDPOINT_URL.search(value):
            raise ValueError("eval-trial payloads must not contain endpoint URLs")
        if (
            _WINDOWS_ABSOLUTE_PATH.search(value)
            or _POSIX_ABSOLUTE_PATH.search(value)
            or _USER_HOME_PATH.search(value)
        ):
            raise ValueError("eval-trial payloads must not contain host paths")


def _reject_secret_like_field_name(field_name: str) -> None:
    normalized = field_name.lower().replace("-", "_")
    if normalized.endswith("_free"):
        return
    if any(marker in normalized for marker in _SECRET_FIELD_MARKERS):
        raise ValueError("eval-trial payload contains secret-like field name")


class _FrozenTrialDict(dict[Any, Any]):
    """Dict-shaped immutable mapping that remains serializable by Pydantic."""

    def __readonly(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("eval-trial mappings are immutable")

    __setitem__ = __readonly
    __delitem__ = __readonly
    clear = __readonly
    pop = __readonly
    popitem = __readonly  # type: ignore[assignment]
    setdefault = __readonly
    update = __readonly
    __ior__ = __readonly  # type: ignore[assignment]


def _freeze_trial_mapping(value: Mapping[Any, Any]) -> Mapping[Any, Any]:
    return _FrozenTrialDict({key: child for key, child in value.items()})


__all__ = [
    "EVAL_TRIAL_ADMITTED_ARM_IDS",
    "EVAL_TRIAL_ARTIFACT_BUNDLE_HASH_KIND",
    "EVAL_TRIAL_DEFAULT_CREATED_AT",
    "EVAL_TRIAL_PLAN_HASH_KIND",
    "EVAL_TRIAL_RECORD_HASH_KIND",
    "EVAL_TRIAL_RESUME_INDEX_HASH_KIND",
    "EVAL_TRIAL_SCHEMA_VERSION",
    "EVAL_TRIAL_STORE_MANIFEST_HASH_KIND",
    "EvalFakeOutcomeScriptKind",
    "EvalFakeRunnerArtifactBundle",
    "EvalOfflineFakeTrialRun",
    "EvalTrialCampaignStoreAppendResult",
    "EvalTrialCampaignStoreRecordSummary",
    "EvalTrialCampaignStoreResumeResult",
    "EvalTrialArmDefinition",
    "EvalTrialArmId",
    "EvalTrialArmParityEvidence",
    "EvalTrialArmPlan",
    "EvalTrialContractModel",
    "EvalTrialExecutionResult",
    "EvalTrialFakeRunnerScript",
    "EvalTrialFixtureInstance",
    "EvalTrialInvalidDiagnostic",
    "EvalTrialInvalidDiagnosticCode",
    "EvalTrialPlan",
    "EvalTrialRecord",
    "EvalTrialModelUsageSummary",
    "EvalTrialResumeIndex",
    "EvalTrialResourceSummary",
    "EvalTrialRunnerRecordSummary",
    "EvalTrialScorerPublicSummary",
    "EvalTrialStoreManifest",
    "calculate_eval_fake_runner_artifact_bundle_hash",
    "calculate_eval_trial_plan_hash",
    "calculate_eval_trial_record_hash",
    "calculate_eval_trial_resume_index_hash",
    "calculate_eval_trial_store_manifest_hash",
    "canonical_eval_fake_runner_artifact_bundle_bytes",
    "canonical_eval_trial_plan_bytes",
    "canonical_eval_trial_record_bytes",
    "canonical_eval_trial_resume_index_bytes",
    "canonical_eval_trial_store_manifest_bytes",
    "append_eval_trial_record_to_campaign_store",
    "default_eval_trial_arm_definitions",
    "default_eval_trial_fixture_instance",
    "default_eval_trial_parity_evidence",
    "default_eval_trial_plan",
    "deny_eval_trial_live_execution",
    "fixture_copy_unavailable_diagnostic",
    "plan_paired_eval_trials",
    "resume_eval_trial_campaign_store",
    "run_offline_fake_eval_trial",
]
