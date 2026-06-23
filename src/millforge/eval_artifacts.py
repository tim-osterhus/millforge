"""Typed compact-eval artifact schemas and canonical layout metadata."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
import re
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from pydantic import field_validator, model_validator

from millforge.eval_boundary import (
    EVAL_CONTEXT_DEFAULT_REDACTION_CATEGORIES,
    EvalContextRedaction,
    EvalContextTier,
    EvalFixtureManifest,
    EvalResourceCeiling,
)
from millforge.eval_workflow import (
    EvalCandidateDisposition,
    EvalStageId,
    EvalTerminalResult,
)

EVAL_ARTIFACT_SCHEMA_VERSION = 1
EVAL_ARTIFACT_FIXED_TIMESTAMP = "1970-01-01T00:00:00Z"
EVAL_ARTIFACT_LAYOUT_ROOT = "trial"
EVAL_ARTIFACT_MEDIA_TYPE_JSON = "application/json"
EVAL_ARTIFACT_MEDIA_TYPE_JSONL = "application/x-ndjson"
EVAL_PUBLIC_ARTIFACT_IDS: tuple[str, ...] = (
    "task",
    "fixture_manifest",
    "acceptance_checks",
    "plan",
    "workspace_diff",
    "patch_summary",
    "test_results",
    "checker_verdict",
    "arbiter_verdict",
    "stage_result",
    "event_log",
    "resource_usage",
    "model_usage",
    "validator_result",
    "context_snapshot",
    "artifact_manifest",
)
EVAL_LOGICAL_06A_ARTIFACT_IDS: tuple[str, ...] = (
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
EVAL_RUNTIME_MEASUREMENT_ARTIFACT_IDS: tuple[str, ...] = (
    "stage_result",
    "event_log",
    "resource_usage",
    "model_usage",
    "validator_result",
    "context_snapshot",
    "artifact_manifest",
)
_PUBLIC_LEAK_TOKENS: tuple[str, ...] = (
    "F:\\",
    "/mnt/f",
    "millrace-agents",
    "ideas/",
    "ref-forge/",
    "/home/",
    "\\Users\\",
    "API_KEY",
    "DAEMON_STATE",
    "daemon state",
    "hidden check",
    "hidden checks",
    "hidden_check",
    "hidden_checks",
    "hidden score",
    "hidden_score",
    "hidden_scores",
    "scoring rubric",
    "scoring_rubric",
    "expected output",
    "expected_output",
    "private runtime",
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'`([{<])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`)\]}>,]*"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'`([{<])/(?!/)[^\s\"'`)\]}>,]*")
_USER_HOME_PATH = re.compile(r"(?:^|[\s\"'`([{<])~(?:/|\\)[^\s\"'`)\]}>,]*")


class EvalArtifactId(str, Enum):
    """Closed public compact-eval artifact IDs."""

    TASK = "task"
    FIXTURE_MANIFEST = "fixture_manifest"
    ACCEPTANCE_CHECKS = "acceptance_checks"
    PLAN = "plan"
    WORKSPACE_DIFF = "workspace_diff"
    PATCH_SUMMARY = "patch_summary"
    TEST_RESULTS = "test_results"
    CHECKER_VERDICT = "checker_verdict"
    ARBITER_VERDICT = "arbiter_verdict"
    STAGE_RESULT = "stage_result"
    EVENT_LOG = "event_log"
    RESOURCE_USAGE = "resource_usage"
    MODEL_USAGE = "model_usage"
    VALIDATOR_RESULT = "validator_result"
    CONTEXT_SNAPSHOT = "context_snapshot"
    ARTIFACT_MANIFEST = "artifact_manifest"


class EvalArtifactSection(str, Enum):
    """Canonical artifact layout sections under the trial root."""

    INPUT = "trial/input"
    PLANNING = "trial/planning"
    EXECUTION = "trial/execution"
    CHECKING = "trial/checking"
    CLOSURE = "trial/closure"
    RUNTIME = "trial/runtime"


class EvalCheckerVerdictValue(str, Enum):
    """Closed Checker verdict values."""

    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class EvalArbiterVerdictValue(str, Enum):
    """Closed Arbiter verdict values."""

    CLOSED = "closed"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class EvalArtifactLayoutEntry(BaseModel):
    """Path-free canonical layout metadata for one public artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: EvalArtifactId
    section: EvalArtifactSection
    canonical_filename: StrictStr
    media_type: StrictStr = EVAL_ARTIFACT_MEDIA_TYPE_JSON
    schema_id: StrictStr
    model_visible: StrictBool = True

    @field_validator("canonical_filename")
    @classmethod
    def _filename_valid(cls, value: str) -> str:
        if (
            not value
            or "/" in value
            or "\\" in value
            or value.startswith(".")
            or ".." in value
        ):
            raise ValueError("canonical_filename must be a stable relative filename")
        return value

    @property
    def layout_path(self) -> str:
        """Return the canonical trial-relative layout path."""
        return f"{self.section.value}/{self.canonical_filename}"


class EvalArtifactReference(BaseModel):
    """Reference to another public artifact without host paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: EvalArtifactId
    summary: StrictStr

    @field_validator("summary")
    @classmethod
    def _summary_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("artifact reference summaries must be non-empty")
        _reject_public_material_leaks(value)
        return value


class EvalArtifactBase(BaseModel):
    """Shared closed-world fields required on every compact-eval artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = EVAL_ARTIFACT_SCHEMA_VERSION
    artifact_id: EvalArtifactId
    trial_id: StrictStr
    stage_id: EvalStageId | None = None
    created_by: StrictStr
    created_at: StrictStr = EVAL_ARTIFACT_FIXED_TIMESTAMP
    references: tuple[EvalArtifactReference, ...] = Field(default_factory=tuple)
    summary: StrictStr

    @field_validator("trial_id", "created_by", "created_at", "summary")
    @classmethod
    def _stable_text_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("artifact text fields must be non-empty")
        _reject_public_material_leaks(value)
        return value

    @model_validator(mode="after")
    def _base_artifact_valid(self) -> EvalArtifactBase:
        if self.schema_version != EVAL_ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported eval artifact schema_version")
        _reject_public_material_leaks(self.model_dump(mode="json"))
        return self


class EvalTaskArtifact(EvalArtifactBase):
    """Model-visible task artifact."""

    artifact_id: Literal[EvalArtifactId.TASK] = EvalArtifactId.TASK
    stage_id: None = None
    task_id: StrictStr
    prompt: StrictStr
    fixture_id: StrictStr
    acceptance_criteria: tuple[StrictStr, ...]
    command_hints: tuple[StrictStr, ...] = Field(default_factory=tuple)
    required_output_artifact_ids: tuple[EvalArtifactId, ...]


class EvalAcceptanceCheck(BaseModel):
    """One model-visible public acceptance check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: StrictStr
    check_kind: StrictStr
    descriptor: StrictStr
    expected_success: StrictStr
    public_rationale: StrictStr

    @model_validator(mode="after")
    def _visible_check_valid(self) -> EvalAcceptanceCheck:
        _reject_public_material_leaks(self.model_dump(mode="json"))
        return self


class EvalAcceptanceChecksArtifact(EvalArtifactBase):
    """Artifact containing only model-visible acceptance checks."""

    artifact_id: Literal[EvalArtifactId.ACCEPTANCE_CHECKS] = (
        EvalArtifactId.ACCEPTANCE_CHECKS
    )
    stage_id: None = None
    visible_acceptance_checks: tuple[EvalAcceptanceCheck, ...]

    @field_validator("visible_acceptance_checks")
    @classmethod
    def _checks_nonempty(
        cls, value: tuple[EvalAcceptanceCheck, ...]
    ) -> tuple[EvalAcceptanceCheck, ...]:
        if not value:
            raise ValueError("acceptance_checks must include visible checks")
        return value


class EvalFixtureManifestArtifact(EvalArtifactBase):
    """Public artifact wrapper for an expanded fixture manifest payload."""

    artifact_id: Literal[EvalArtifactId.FIXTURE_MANIFEST] = (
        EvalArtifactId.FIXTURE_MANIFEST
    )
    stage_id: None = None
    fixture_manifest: EvalFixtureManifest


class EvalPlanArtifact(EvalArtifactBase):
    """Planner output artifact."""

    artifact_id: Literal[EvalArtifactId.PLAN] = EvalArtifactId.PLAN
    stage_id: Literal[EvalStageId.PLANNER] = EvalStageId.PLANNER
    implementation_steps: tuple[StrictStr, ...]
    expected_files_to_inspect: tuple[StrictStr, ...]
    expected_files_to_mutate: tuple[StrictStr, ...]
    checks_to_run: tuple[StrictStr, ...]
    risk_notes: tuple[StrictStr, ...] = Field(default_factory=tuple)
    no_hidden_checks_known: StrictBool

    @model_validator(mode="after")
    def _plan_valid(self) -> EvalPlanArtifact:
        if not self.no_hidden_checks_known:
            raise ValueError("plan must explicitly claim no hidden checks are known")
        return self


class EvalFileHashChange(BaseModel):
    """Per-file before/after hash record for workspace diffs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: StrictStr
    before_sha256: StrictStr | None = None
    after_sha256: StrictStr | None = None


class EvalWorkspaceDiffArtifact(EvalArtifactBase):
    """Builder workspace diff artifact."""

    artifact_id: Literal[EvalArtifactId.WORKSPACE_DIFF] = EvalArtifactId.WORKSPACE_DIFF
    stage_id: Literal[EvalStageId.BUILDER] = EvalStageId.BUILDER
    added_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    modified_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    deleted_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    ignored_generated_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    file_hashes: tuple[EvalFileHashChange, ...] = Field(default_factory=tuple)
    unauthorized_mutation_diagnostics: tuple[StrictStr, ...] = Field(
        default_factory=tuple
    )


class EvalCommandOutcome(BaseModel):
    """Public command outcome summary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: StrictStr
    exit_code: StrictInt
    summary: StrictStr


class EvalPatchSummaryArtifact(EvalArtifactBase):
    """Builder patch summary artifact."""

    artifact_id: Literal[EvalArtifactId.PATCH_SUMMARY] = EvalArtifactId.PATCH_SUMMARY
    stage_id: Literal[EvalStageId.BUILDER] = EvalStageId.BUILDER
    changed_files: tuple[StrictStr, ...]
    behavior_summary: StrictStr
    commands_run: tuple[StrictStr, ...] = Field(default_factory=tuple)
    command_outcomes: tuple[EvalCommandOutcome, ...] = Field(default_factory=tuple)
    unresolved_issues: tuple[StrictStr, ...] = Field(default_factory=tuple)


class EvalTestResultsArtifact(EvalArtifactBase):
    """Builder test results artifact."""

    artifact_id: Literal[EvalArtifactId.TEST_RESULTS] = EvalArtifactId.TEST_RESULTS
    stage_id: Literal[EvalStageId.BUILDER] = EvalStageId.BUILDER
    command: tuple[StrictStr, ...]
    exit_code: StrictInt
    duration_seconds: StrictInt = Field(ge=0)
    output_summary: StrictStr
    passed_count: StrictInt = Field(ge=0)
    failed_count: StrictInt = Field(ge=0)
    skipped_count: StrictInt = Field(ge=0)
    deterministic: StrictBool
    allowed_by_policy: StrictBool


class EvalCheckerVerdictArtifact(EvalArtifactBase):
    """Checker verdict artifact."""

    artifact_id: Literal[EvalArtifactId.CHECKER_VERDICT] = (
        EvalArtifactId.CHECKER_VERDICT
    )
    stage_id: Literal[EvalStageId.CHECKER] = EvalStageId.CHECKER
    verdict: EvalCheckerVerdictValue
    evidence_references: tuple[EvalArtifactReference, ...]
    failed_public_checks: tuple[StrictStr, ...] = Field(default_factory=tuple)
    unresolved_required_issues: tuple[StrictStr, ...] = Field(default_factory=tuple)
    requested_remediation_summary: StrictStr | None = None
    blocker_summary: StrictStr | None = None

    @model_validator(mode="after")
    def _checker_verdict_valid(self) -> EvalCheckerVerdictArtifact:
        if not self.evidence_references:
            raise ValueError("checker verdicts must cite public evidence")
        if self.verdict == EvalCheckerVerdictValue.APPROVED:
            if self.failed_public_checks or self.unresolved_required_issues:
                raise ValueError(
                    "approved checker verdicts must not include failed checks or "
                    "unresolved required issues"
                )
            if self.requested_remediation_summary or self.blocker_summary:
                raise ValueError(
                    "approved checker verdicts must not request remediation or blockers"
                )
        elif self.verdict == EvalCheckerVerdictValue.REJECTED:
            if not self.unresolved_required_issues:
                raise ValueError(
                    "rejected checker verdicts must include unresolved required issues"
                )
            if not self.requested_remediation_summary:
                raise ValueError(
                    "rejected checker verdicts must include remediation summary"
                )
        elif not self.blocker_summary:
            raise ValueError("blocked checker verdicts must include blocker summary")
        return self


class EvalArbiterVerdictArtifact(EvalArtifactBase):
    """Arbiter verdict artifact."""

    artifact_id: Literal[EvalArtifactId.ARBITER_VERDICT] = (
        EvalArtifactId.ARBITER_VERDICT
    )
    stage_id: Literal[EvalStageId.ARBITER] = EvalStageId.ARBITER
    verdict: EvalArbiterVerdictValue
    candidate_disposition: EvalCandidateDisposition
    closure_evidence_references: tuple[EvalArtifactReference, ...]
    missing_artifact_diagnostics: tuple[StrictStr, ...] = Field(default_factory=tuple)
    unauthorized_mutation_diagnostics: tuple[StrictStr, ...] = Field(
        default_factory=tuple
    )
    public_acceptance_status: StrictStr
    open_acceptance_check_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _arbiter_verdict_valid(self) -> EvalArbiterVerdictArtifact:
        if not self.closure_evidence_references:
            raise ValueError("arbiter verdicts must cite public closure evidence")
        if self.verdict == EvalArbiterVerdictValue.CLOSED:
            if self.candidate_disposition != EvalCandidateDisposition.APPROVED:
                raise ValueError(
                    "closed arbiter verdicts require approved candidate disposition"
                )
            if (
                self.missing_artifact_diagnostics
                or self.unauthorized_mutation_diagnostics
                or self.open_acceptance_check_ids
            ):
                raise ValueError(
                    "closed arbiter verdicts must not include unresolved diagnostics"
                )
        elif self.verdict == EvalArbiterVerdictValue.REJECTED:
            if self.candidate_disposition != EvalCandidateDisposition.REJECTED:
                raise ValueError(
                    "rejected arbiter verdicts require rejected candidate disposition"
                )
            if not (
                self.missing_artifact_diagnostics
                or self.unauthorized_mutation_diagnostics
                or self.open_acceptance_check_ids
                or self.public_acceptance_status.strip()
            ):
                raise ValueError("rejected arbiter verdicts must include rationale")
        elif self.candidate_disposition != EvalCandidateDisposition.BLOCKED:
            raise ValueError(
                "blocked arbiter verdicts require blocked candidate disposition"
            )
        return self


class EvalStageResultArtifact(EvalArtifactBase):
    """Runtime stage result artifact."""

    artifact_id: Literal[EvalArtifactId.STAGE_RESULT] = EvalArtifactId.STAGE_RESULT
    stage_id: EvalStageId
    terminal_result: EvalTerminalResult
    attempt_count: StrictInt = Field(ge=0)
    infrastructure_retry_count: StrictInt = Field(ge=0, le=1)
    duration_seconds: StrictInt = Field(ge=0)
    output_artifact_refs: tuple[EvalArtifactReference, ...] = Field(
        default_factory=tuple
    )
    diagnostics: tuple[StrictStr, ...] = Field(default_factory=tuple)


class EvalEventRecord(BaseModel):
    """Append-only structured public event record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: StrictStr
    stage_id: EvalStageId
    event_type: StrictStr
    summary: StrictStr

    @model_validator(mode="after")
    def _event_valid(self) -> EvalEventRecord:
        _reject_public_material_leaks(self.model_dump(mode="json"))
        return self


class EvalEventLogArtifact(EvalArtifactBase):
    """Public event log artifact."""

    artifact_id: Literal[EvalArtifactId.EVENT_LOG] = EvalArtifactId.EVENT_LOG
    records: tuple[EvalEventRecord, ...]


class EvalResourceUsageArtifact(EvalArtifactBase):
    """Runtime resource usage artifact."""

    artifact_id: Literal[EvalArtifactId.RESOURCE_USAGE] = EvalArtifactId.RESOURCE_USAGE
    wall_clock_seconds: StrictInt = Field(ge=0)
    shell_command_count: StrictInt = Field(ge=0)
    shell_command_seconds: StrictInt = Field(ge=0)
    writable_bytes: StrictInt = Field(ge=0)
    artifact_bytes: StrictInt = Field(ge=0)
    retry_count: StrictInt = Field(ge=0)


class EvalModelUsageArtifact(EvalArtifactBase):
    """Runtime model usage artifact with deterministic placeholder support."""

    artifact_id: Literal[EvalArtifactId.MODEL_USAGE] = EvalArtifactId.MODEL_USAGE
    provider: StrictStr | None = None
    model: StrictStr | None = None
    prompt_tokens: StrictInt = Field(ge=0)
    completion_tokens: StrictInt = Field(ge=0)
    total_tokens: StrictInt = Field(ge=0)
    reasoning_tokens: StrictInt | None = Field(default=None, ge=0)
    cached_tokens: StrictInt | None = Field(default=None, ge=0)
    estimated_cost_micros: StrictInt = Field(ge=0)
    wall_clock_seconds: StrictInt = Field(ge=0)
    retry_count: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _tokens_valid(self) -> EvalModelUsageArtifact:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError(
                "total_tokens must equal prompt_tokens plus completion_tokens"
            )
        return self


class EvalValidatorResultArtifact(EvalArtifactBase):
    """Model-visible validator result artifact."""

    artifact_id: Literal[EvalArtifactId.VALIDATOR_RESULT] = (
        EvalArtifactId.VALIDATOR_RESULT
    )
    visible_check_results: tuple[EvalCommandOutcome, ...]
    public_diagnostics: tuple[StrictStr, ...] = Field(default_factory=tuple)
    model_visible: StrictBool = True

    @model_validator(mode="after")
    def _validator_result_valid(self) -> EvalValidatorResultArtifact:
        if not self.model_visible:
            raise ValueError("validator_result public artifact must be model-visible")
        return self


class EvalValidatorVisibilityRecord(BaseModel):
    """Model-facing visibility boundary for validator inputs and outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    visible_acceptance_check_ids: tuple[StrictStr, ...]
    model_visible_artifact_ids: tuple[EvalArtifactId, ...] = (
        EvalArtifactId.ACCEPTANCE_CHECKS,
        EvalArtifactId.VALIDATOR_RESULT,
    )
    visible_validator_filename: StrictStr = "validator_result.visible.json"
    scorer_only_opaque_check_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    scorer_only_definitions_excluded: StrictBool = True
    scorer_only_expected_outputs_excluded: StrictBool = True
    scorer_only_rubrics_excluded: StrictBool = True
    scorer_only_final_scores_excluded: StrictBool = True

    @model_validator(mode="after")
    def _visibility_record_valid(self) -> EvalValidatorVisibilityRecord:
        if not self.visible_acceptance_check_ids:
            raise ValueError("visibility records require visible check ids")
        if len(set(self.visible_acceptance_check_ids)) != len(
            self.visible_acceptance_check_ids
        ):
            raise ValueError("visible acceptance check ids must be unique")
        if len(set(self.scorer_only_opaque_check_ids)) != len(
            self.scorer_only_opaque_check_ids
        ):
            raise ValueError("opaque scorer-only check ids must be unique")
        if set(self.visible_acceptance_check_ids) & set(
            self.scorer_only_opaque_check_ids
        ):
            raise ValueError("visible and scorer-only check ids must not overlap")
        if EvalArtifactId.VALIDATOR_RESULT not in self.model_visible_artifact_ids:
            raise ValueError("validator_result.visible.json must be model-visible")
        if EvalArtifactId.ACCEPTANCE_CHECKS not in self.model_visible_artifact_ids:
            raise ValueError("visible acceptance checks must be model-visible")
        if self.visible_validator_filename != "validator_result.visible.json":
            raise ValueError("visible validator result filename is fixed")
        if not all(
            (
                self.scorer_only_definitions_excluded,
                self.scorer_only_expected_outputs_excluded,
                self.scorer_only_rubrics_excluded,
                self.scorer_only_final_scores_excluded,
            )
        ):
            raise ValueError("scorer-only material must be structurally excluded")
        _reject_public_material_leaks(self.model_dump(mode="json"))
        return self


class EvalContextSnapshotArtifact(EvalArtifactBase):
    """Path-free model-visible context snapshot metadata."""

    artifact_id: Literal[EvalArtifactId.CONTEXT_SNAPSHOT] = (
        EvalArtifactId.CONTEXT_SNAPSHOT
    )
    stage_id: EvalStageId
    context_tier: EvalContextTier
    allowed_capabilities: tuple[StrictStr, ...]
    allowed_paths: tuple[StrictStr, ...]
    current_stage_contract: Mapping[StrictStr, Any]
    required_artifact_summaries: tuple[EvalArtifactReference, ...]
    visible_acceptance_check_ids: tuple[StrictStr, ...]
    redaction: EvalContextRedaction
    redaction_summary: StrictStr
    byte_budget: StrictInt = Field(gt=0)
    token_budget: StrictInt = Field(gt=0)
    resource_ceiling: EvalResourceCeiling
    fingerprint: StrictStr

    @model_validator(mode="after")
    def _context_snapshot_valid(self) -> EvalContextSnapshotArtifact:
        _validate_sha256(self.fingerprint)
        if self.resource_ceiling.stage_id != self.stage_id:
            raise ValueError("context snapshot resource ceiling must match stage")
        _reject_public_material_leaks(self.model_dump(mode="json"))
        return self


class EvalArtifactManifestEntry(BaseModel):
    """Deterministic public artifact manifest entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: EvalArtifactId
    layout_path: StrictStr
    media_type: StrictStr
    schema_id: StrictStr
    byte_size: StrictInt = Field(ge=0)
    sha256: StrictStr
    producer: StrictStr
    model_visible: StrictBool = True

    @model_validator(mode="after")
    def _manifest_entry_valid(self) -> EvalArtifactManifestEntry:
        layout = eval_artifact_layout_entry(self.artifact_id)
        if self.layout_path != layout.layout_path:
            raise ValueError("manifest entry layout_path must match canonical layout")
        if self.media_type != layout.media_type or self.schema_id != layout.schema_id:
            raise ValueError("manifest entry metadata must match canonical layout")
        _validate_sha256(self.sha256)
        _reject_public_material_leaks(self.model_dump(mode="json"))
        return self


class EvalArtifactManifestArtifact(EvalArtifactBase):
    """Deterministic manifest of declared public artifacts."""

    artifact_id: Literal[EvalArtifactId.ARTIFACT_MANIFEST] = (
        EvalArtifactId.ARTIFACT_MANIFEST
    )
    entries: tuple[EvalArtifactManifestEntry, ...]

    @model_validator(mode="after")
    def _manifest_valid(self) -> EvalArtifactManifestArtifact:
        artifact_ids = tuple(entry.artifact_id for entry in self.entries)
        if EvalArtifactId.ARTIFACT_MANIFEST in artifact_ids:
            raise ValueError("artifact_manifest must not reference itself")
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("artifact_manifest entries must be unique")
        object.__setattr__(
            self,
            "entries",
            tuple(
                sorted(
                    self.entries,
                    key=lambda entry: tuple(EvalArtifactId).index(entry.artifact_id),
                )
            ),
        )
        return self


_EVAL_ARTIFACT_LAYOUT: Mapping[EvalArtifactId, EvalArtifactLayoutEntry] = (
    MappingProxyType(
        {
            EvalArtifactId.TASK: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.TASK,
                section=EvalArtifactSection.INPUT,
                canonical_filename="task.json",
                schema_id="eval_task_artifact_v1",
            ),
            EvalArtifactId.FIXTURE_MANIFEST: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.FIXTURE_MANIFEST,
                section=EvalArtifactSection.INPUT,
                canonical_filename="fixture_manifest.json",
                schema_id="eval_fixture_manifest_artifact_v1",
            ),
            EvalArtifactId.ACCEPTANCE_CHECKS: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.ACCEPTANCE_CHECKS,
                section=EvalArtifactSection.INPUT,
                canonical_filename="acceptance_checks.json",
                schema_id="eval_acceptance_checks_artifact_v1",
            ),
            EvalArtifactId.PLAN: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.PLAN,
                section=EvalArtifactSection.PLANNING,
                canonical_filename="plan.json",
                schema_id="eval_plan_artifact_v1",
            ),
            EvalArtifactId.WORKSPACE_DIFF: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.WORKSPACE_DIFF,
                section=EvalArtifactSection.EXECUTION,
                canonical_filename="workspace_diff.json",
                schema_id="eval_workspace_diff_artifact_v1",
            ),
            EvalArtifactId.PATCH_SUMMARY: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.PATCH_SUMMARY,
                section=EvalArtifactSection.EXECUTION,
                canonical_filename="patch_summary.json",
                schema_id="eval_patch_summary_artifact_v1",
            ),
            EvalArtifactId.TEST_RESULTS: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.TEST_RESULTS,
                section=EvalArtifactSection.EXECUTION,
                canonical_filename="test_results.json",
                schema_id="eval_test_results_artifact_v1",
            ),
            EvalArtifactId.CHECKER_VERDICT: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.CHECKER_VERDICT,
                section=EvalArtifactSection.CHECKING,
                canonical_filename="checker_verdict.json",
                schema_id="eval_checker_verdict_artifact_v1",
            ),
            EvalArtifactId.ARBITER_VERDICT: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.ARBITER_VERDICT,
                section=EvalArtifactSection.CLOSURE,
                canonical_filename="arbiter_verdict.json",
                schema_id="eval_arbiter_verdict_artifact_v1",
            ),
            EvalArtifactId.STAGE_RESULT: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.STAGE_RESULT,
                section=EvalArtifactSection.RUNTIME,
                canonical_filename="stage_result.json",
                schema_id="eval_stage_result_artifact_v1",
            ),
            EvalArtifactId.EVENT_LOG: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.EVENT_LOG,
                section=EvalArtifactSection.RUNTIME,
                canonical_filename="event_log.jsonl",
                media_type=EVAL_ARTIFACT_MEDIA_TYPE_JSONL,
                schema_id="eval_event_log_artifact_v1",
            ),
            EvalArtifactId.RESOURCE_USAGE: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.RESOURCE_USAGE,
                section=EvalArtifactSection.RUNTIME,
                canonical_filename="resource_usage.json",
                schema_id="eval_resource_usage_artifact_v1",
            ),
            EvalArtifactId.MODEL_USAGE: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.MODEL_USAGE,
                section=EvalArtifactSection.RUNTIME,
                canonical_filename="model_usage.json",
                schema_id="eval_model_usage_artifact_v1",
            ),
            EvalArtifactId.VALIDATOR_RESULT: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.VALIDATOR_RESULT,
                section=EvalArtifactSection.RUNTIME,
                canonical_filename="validator_result.visible.json",
                schema_id="eval_validator_result_visible_artifact_v1",
            ),
            EvalArtifactId.CONTEXT_SNAPSHOT: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.CONTEXT_SNAPSHOT,
                section=EvalArtifactSection.RUNTIME,
                canonical_filename="context_snapshot.<stage>.json",
                schema_id="eval_context_snapshot_artifact_v1",
            ),
            EvalArtifactId.ARTIFACT_MANIFEST: EvalArtifactLayoutEntry(
                artifact_id=EvalArtifactId.ARTIFACT_MANIFEST,
                section=EvalArtifactSection.RUNTIME,
                canonical_filename="artifact_manifest.json",
                schema_id="eval_artifact_manifest_artifact_v1",
            ),
        }
    )
)

EVAL_ARTIFACT_SCHEMAS: Mapping[EvalArtifactId, type[BaseModel]] = MappingProxyType(
    {
        EvalArtifactId.TASK: EvalTaskArtifact,
        EvalArtifactId.FIXTURE_MANIFEST: EvalFixtureManifestArtifact,
        EvalArtifactId.ACCEPTANCE_CHECKS: EvalAcceptanceChecksArtifact,
        EvalArtifactId.PLAN: EvalPlanArtifact,
        EvalArtifactId.WORKSPACE_DIFF: EvalWorkspaceDiffArtifact,
        EvalArtifactId.PATCH_SUMMARY: EvalPatchSummaryArtifact,
        EvalArtifactId.TEST_RESULTS: EvalTestResultsArtifact,
        EvalArtifactId.CHECKER_VERDICT: EvalCheckerVerdictArtifact,
        EvalArtifactId.ARBITER_VERDICT: EvalArbiterVerdictArtifact,
        EvalArtifactId.STAGE_RESULT: EvalStageResultArtifact,
        EvalArtifactId.EVENT_LOG: EvalEventLogArtifact,
        EvalArtifactId.RESOURCE_USAGE: EvalResourceUsageArtifact,
        EvalArtifactId.MODEL_USAGE: EvalModelUsageArtifact,
        EvalArtifactId.VALIDATOR_RESULT: EvalValidatorResultArtifact,
        EvalArtifactId.CONTEXT_SNAPSHOT: EvalContextSnapshotArtifact,
        EvalArtifactId.ARTIFACT_MANIFEST: EvalArtifactManifestArtifact,
    }
)


def canonical_eval_artifact_layout() -> Mapping[
    EvalArtifactId, EvalArtifactLayoutEntry
]:
    """Return immutable canonical layout metadata keyed by artifact ID."""
    return _EVAL_ARTIFACT_LAYOUT


def eval_artifact_layout_entry(
    artifact_id: EvalArtifactId | str,
) -> EvalArtifactLayoutEntry:
    """Return canonical layout metadata for one declared artifact ID."""
    return _EVAL_ARTIFACT_LAYOUT[EvalArtifactId(artifact_id)]


def canonical_eval_artifact_manifest_bytes(
    manifest: EvalArtifactManifestArtifact,
) -> bytes:
    """Return deterministic ASCII JSON bytes for an artifact manifest."""
    return _canonical_json_bytes(manifest.model_dump(mode="json"))


def calculate_eval_artifact_manifest_sha256(
    manifest: EvalArtifactManifestArtifact,
) -> str:
    """Return the deterministic SHA-256 for a canonical manifest."""
    return hashlib.sha256(canonical_eval_artifact_manifest_bytes(manifest)).hexdigest()


def validate_eval_artifact_record(
    artifact_id: EvalArtifactId | str, record: Mapping[str, Any]
) -> BaseModel:
    """Validate one artifact record against the closed public schema registry."""
    resolved_artifact_id = EvalArtifactId(artifact_id)
    schema = EVAL_ARTIFACT_SCHEMAS[resolved_artifact_id]
    return schema.model_validate(record)


def _canonical_json_bytes(value: Any) -> bytes:
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
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")


def _reject_public_material_leaks(value: Any) -> None:
    for text in _public_material_text_values(value):
        if text in EVAL_CONTEXT_DEFAULT_REDACTION_CATEGORIES:
            continue
        lowered = text.lower()
        for token in _PUBLIC_LEAK_TOKENS:
            if token.lower() in lowered:
                raise ValueError(
                    "public eval artifacts must not expose private material"
                )
        if (
            _WINDOWS_ABSOLUTE_PATH.search(text)
            or _POSIX_ABSOLUTE_PATH.search(text)
            or _USER_HOME_PATH.search(text)
        ):
            raise ValueError("public eval artifacts must not expose host paths")


def _public_material_text_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(
            text
            for child in value.values()
            for text in _public_material_text_values(child)
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        return tuple(
            text for child in value for text in _public_material_text_values(child)
        )
    return ()


__all__ = [
    "EVAL_ARTIFACT_FIXED_TIMESTAMP",
    "EVAL_ARTIFACT_LAYOUT_ROOT",
    "EVAL_ARTIFACT_MEDIA_TYPE_JSON",
    "EVAL_ARTIFACT_MEDIA_TYPE_JSONL",
    "EVAL_ARTIFACT_SCHEMA_VERSION",
    "EVAL_ARTIFACT_SCHEMAS",
    "EVAL_LOGICAL_06A_ARTIFACT_IDS",
    "EVAL_PUBLIC_ARTIFACT_IDS",
    "EVAL_RUNTIME_MEASUREMENT_ARTIFACT_IDS",
    "EvalAcceptanceCheck",
    "EvalAcceptanceChecksArtifact",
    "EvalArbiterVerdictArtifact",
    "EvalArbiterVerdictValue",
    "EvalArtifactBase",
    "EvalArtifactId",
    "EvalArtifactLayoutEntry",
    "EvalArtifactManifestArtifact",
    "EvalArtifactManifestEntry",
    "EvalArtifactReference",
    "EvalArtifactSection",
    "EvalCheckerVerdictArtifact",
    "EvalCheckerVerdictValue",
    "EvalCommandOutcome",
    "EvalContextSnapshotArtifact",
    "EvalEventLogArtifact",
    "EvalEventRecord",
    "EvalFileHashChange",
    "EvalFixtureManifestArtifact",
    "EvalModelUsageArtifact",
    "EvalPatchSummaryArtifact",
    "EvalPlanArtifact",
    "EvalResourceUsageArtifact",
    "EvalStageResultArtifact",
    "EvalTaskArtifact",
    "EvalTestResultsArtifact",
    "EvalValidatorResultArtifact",
    "EvalValidatorVisibilityRecord",
    "EvalWorkspaceDiffArtifact",
    "calculate_eval_artifact_manifest_sha256",
    "canonical_eval_artifact_layout",
    "canonical_eval_artifact_manifest_bytes",
    "eval_artifact_layout_entry",
    "validate_eval_artifact_record",
]
