"""Public 08A offline eval-suite contract boundary."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from importlib.resources import files
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt
from pydantic import StrictStr, field_validator, model_validator

from millforge.eval_modes import (
    EVAL_SMALL_MILLFORGE_MODE_ID,
    EVAL_SMALL_PI_MODE_ID,
    EvalModelProfile,
    default_eval_model_profile,
)
from millforge.eval_workflow import compact_eval_workflow_snapshot

EVAL_SUITE_SCHEMA_VERSION = 1
EVAL_SUITE_CAMPAIGN_MANIFEST_HASH_KIND = "eval_suite_campaign_manifest_sha256_v1"
EVAL_SUITE_MODEL_MANIFEST_HASH_KIND = "eval_suite_model_manifest_sha256_v1"
EVAL_SUITE_FIXTURE_HASH_KIND = "eval_suite_fixture_sha256_v1"
EVAL_SUITE_FIXTURE_PACK_HASH_KIND = "eval_suite_fixture_pack_sha256_v1"
EVAL_SUITE_SCORER_INPUT_HASH_KIND = "eval_suite_scorer_input_sha256_v1"
EVAL_SUITE_SCORER_RESULT_HASH_KIND = "eval_suite_scorer_result_sha256_v1"
EVAL_SUITE_DEFAULT_CAMPAIGN_ID = "eval.08a.default.offline.v1"
EVAL_SUITE_DEFAULT_CAMPAIGN_CREATED_AT = "1970-01-01T00:00:00Z"
EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID = "pack.08a.offline.default.v1"
EVAL_SUITE_DEFAULT_SCORER_VERSION = "eval_suite.scorer.contract.v1"
EVAL_SUITE_OUTPUT_ROOT_HASH_KIND = "eval_suite_output_root_sha256_v1"
EVAL_SUITE_CLOSURE_EVIDENCE_HASH_KIND = "eval_suite_offline_closure_evidence_sha256_v1"

_EVAL_FIXTURE_PACK_PACKAGE = "millforge.eval_fixtures.default_pack"
_EVAL_FIXTURE_PACK_MANIFEST = "manifest.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
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
_DENIED_TEXT_TOKENS = (
    "api_key",
    "authorization:",
    "bearer ",
    "credential",
    "password",
    "secret",
    "access token",
    "auth token",
    "bearer token",
    "millrace-agents",
    ".millrace",
    "daemon state",
    "endpoint_url",
    "endpoint url",
    "external service",
    "hidden answer",
    "local planning",
    "private runtime",
    "network access",
    "package install",
    "ref-forge",
    ".claude",
    ".codex",
)
_NETWORK_COMMANDS = frozenset(
    {"curl", "ftp", "nc", "netcat", "scp", "sftp", "ssh", "telnet", "wget"}
)
_PACKAGE_COMMANDS = frozenset(
    {"cargo", "gem", "npm", "pip", "pip3", "pnpm", "poetry", "uv", "yarn"}
)
_NONDETERMINISTIC_COMMAND_TOKENS = frozenset(
    {"date", "random", "sleep", "time", "uuid"}
)
_NONDETERMINISTIC_PYTHON_PRIMITIVES = (
    re.compile(r"\buuid\.uuid4\s*\("),
    re.compile(r"\btime\.time\s*\("),
)


class _FrozenEvalSuiteDict(dict[Any, Any]):
    """Dict-shaped immutable mapping that remains serializable by Pydantic."""

    def __readonly(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("eval-suite mappings are immutable")

    __setitem__ = __readonly
    __delitem__ = __readonly
    clear = __readonly
    pop = __readonly
    popitem = __readonly  # type: ignore[assignment]
    setdefault = __readonly
    update = __readonly
    __ior__ = __readonly  # type: ignore[assignment]


class EvalSuiteContractModel(BaseModel):
    """Closed, frozen base for public eval-suite contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_payload(cls, data: Any) -> Any:
        _reject_forbidden_material(data)
        return data


class EvalCampaignKind(str, Enum):
    """Closed campaign backend classes."""

    HOSTED_API = "hosted_api"
    LOCAL_OPENAI_COMPATIBLE = "local_openai_compatible"
    LOCAL_NATIVE = "local_native"


class EvalSuiteExecutionMode(str, Enum):
    """Closed eval-suite execution modes."""

    OFFLINE_FAKE = "offline_fake"
    LIVE_RUNNER = "live_runner"


class EvalTaskCategory(str, Enum):
    """Closed 08A fixture task categories."""

    DIRECT_EDIT = "direct_edit"
    MULTI_FILE_CONSISTENCY = "multi_file_consistency"
    BUG_DIAGNOSIS = "bug_diagnosis"
    EVIDENCE_DISCIPLINE = "evidence_discipline"
    RECOVERY = "recovery"
    FALSE_CLOSURE_TRAP = "false_closure_trap"


class EvalDifficultyLevel(str, Enum):
    """Coarse public fixture difficulty labels."""

    BASIC = "basic"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class EvalExpectedMutationKind(str, Enum):
    """Expected workspace mutation policy for a fixture."""

    NO_SOURCE_CHANGE = "no_source_change"
    SOURCE_CHANGE_REQUIRED = "source_change_required"
    DOCUMENTATION_ONLY = "documentation_only"
    TEST_ONLY = "test_only"


class EvalTrialOutcome(str, Enum):
    """Closed final scorer outcome classes."""

    VALID_COMPLETION = "valid_completion"
    CORRECTLY_BLOCKED = "correctly_blocked"
    FALSE_CLOSURE = "false_closure"
    FALSE_BLOCKED = "false_blocked"
    RUNTIME_FAILURE = "runtime_failure"
    PROVIDER_FAILURE = "provider_failure"
    INVALID_TRIAL = "invalid_trial"


class EvalOfflineDryCampaignDiagnosticCode(str, Enum):
    """Fail-closed diagnostics for offline dry-campaign preflight."""

    MISSING_BUDGET_POLICY = "missing_budget_policy"
    INVALID_BUDGET_POLICY = "invalid_budget_policy"
    LIVE_EXECUTION_UNAVAILABLE = "live_execution_unavailable"
    UNSAFE_OUTPUT_ROOT = "unsafe_output_root"
    FIXTURE_PACK_UNAVAILABLE = "fixture_pack_unavailable"
    INVALID_TRIAL_COUNT = "invalid_trial_count"
    MANIFEST_CONFLICT = "manifest_conflict"


class EvalFailureTaxonomyLabel(str, Enum):
    """Closed public failure taxonomy labels for scorer results."""

    VISIBLE_CHECK_FAILED = "visible_check_failed"
    HIDDEN_CHECK_FAILED = "hidden_check_failed"
    REQUIRED_ARTIFACT_MISSING = "required_artifact_missing"
    REQUIRED_ARTIFACT_MALFORMED = "required_artifact_malformed"
    EXPECTED_MUTATION_ABSENT = "expected_mutation_absent"
    UNAUTHORIZED_MUTATION = "unauthorized_mutation"
    CAPABILITY_VIOLATION = "capability_violation"
    FALSE_SUCCESS_TERMINAL = "false_success_terminal"
    INFRASTRUCTURE_DEFECT = "infrastructure_defect"
    PROVIDER_DEFECT = "provider_defect"


class EvalHashRecord(EvalSuiteContractModel):
    """Typed hash reference for eval-suite records."""

    hash_kind: StrictStr
    sha256: StrictStr

    @field_validator("sha256")
    @classmethod
    def _sha256_valid(cls, value: str) -> str:
        _validate_sha256(value)
        return value


class EvalBudgetPolicyReference(EvalSuiteContractModel):
    """Public reference to a campaign budget policy."""

    policy_id: StrictStr
    summary: StrictStr


class EvalLiveDenialDiagnostic(EvalSuiteContractModel):
    """Fail-closed live execution denial diagnostic."""

    diagnostic_code: StrictStr
    summary: StrictStr
    rule_id: StrictStr


class EvalOfflineDryCampaignDiagnostic(EvalSuiteContractModel):
    """Structured public diagnostic for dry-campaign validation failures."""

    diagnostic_code: EvalOfflineDryCampaignDiagnosticCode
    rule_id: StrictStr
    summary: StrictStr


class EvalOfflineDryCampaignConfig(EvalSuiteContractModel):
    """Public offline dry-campaign configuration after preflight validation."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    fixture_pack_id: StrictStr = EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID
    fixture_ids: tuple[StrictStr, ...]
    deterministic_seed: StrictInt = Field(ge=0)
    trial_count_per_fixture_per_arm: StrictInt = Field(gt=0)
    output_root_hash_kind: StrictStr = EVAL_SUITE_OUTPUT_ROOT_HASH_KIND
    output_root_hash: StrictStr
    output_root_policy: StrictStr = "caller_provided_preflight_validated"

    @model_validator(mode="after")
    def _config_valid(self) -> EvalOfflineDryCampaignConfig:
        if self.schema_version != EVAL_SUITE_SCHEMA_VERSION:
            raise ValueError("unsupported eval-suite dry-campaign schema_version")
        if self.fixture_pack_id != EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID:
            raise ValueError("only the default offline fixture pack is installed")
        if not self.fixture_ids:
            raise ValueError("dry campaigns require at least one fixture")
        if len(set(self.fixture_ids)) != len(self.fixture_ids):
            raise ValueError("dry-campaign fixture IDs must be unique")
        if self.output_root_hash_kind != EVAL_SUITE_OUTPUT_ROOT_HASH_KIND:
            raise ValueError("unsupported output root hash kind")
        _validate_sha256(self.output_root_hash)
        return self


class EvalOfflineDryCampaignPlan(EvalSuiteContractModel):
    """Public preflight result for a deterministic offline fake campaign."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    config: EvalOfflineDryCampaignConfig
    campaign_manifest: EvalCampaignManifest
    fixture_pack_summary: EvalFixturePackSummary
    admitted_arm_ids: tuple[StrictStr, StrictStr]
    paired_plan_count: StrictInt = Field(ge=0)
    planned_arm_trial_count: StrictInt = Field(ge=0)
    trial_plan_hashes: tuple[StrictStr, ...]
    campaign_store_root: StrictStr
    manifest_relative_path: StrictStr
    budget_policy_ref: EvalBudgetPolicyReference
    live_execution_admitted: StrictBool = False
    diagnostics: tuple[EvalOfflineDryCampaignDiagnostic, ...] = Field(
        default_factory=tuple
    )

    @model_validator(mode="after")
    def _dry_plan_valid(self) -> EvalOfflineDryCampaignPlan:
        if self.schema_version != EVAL_SUITE_SCHEMA_VERSION:
            raise ValueError("unsupported eval-suite dry-campaign schema_version")
        if (
            self.campaign_manifest.execution_mode
            is not EvalSuiteExecutionMode.OFFLINE_FAKE
        ):
            raise ValueError("dry campaigns must use offline fake execution")
        if (
            self.live_execution_admitted
            or self.campaign_manifest.live_execution_admitted
        ):
            raise ValueError("dry campaigns do not admit live execution")
        if self.fixture_pack_summary.fixture_pack_id != self.config.fixture_pack_id:
            raise ValueError("fixture pack summary must match dry-campaign config")
        if self.fixture_pack_summary.fixture_pack_hash != (
            self.campaign_manifest.fixture_pack_hash
        ):
            raise ValueError("campaign manifest must reference fixture pack summary")
        if self.paired_plan_count != len(self.trial_plan_hashes):
            raise ValueError("paired_plan_count must match trial_plan_hashes")
        if self.planned_arm_trial_count != self.paired_plan_count * len(
            self.admitted_arm_ids
        ):
            raise ValueError("planned_arm_trial_count must match admitted arms")
        for digest in self.trial_plan_hashes:
            _validate_sha256(digest)
        _validate_relative_artifact_root(self.campaign_store_root)
        _validate_relative_artifact_root(self.manifest_relative_path)
        if self.diagnostics:
            raise ValueError("valid dry-campaign plans must not carry diagnostics")
        return self


class EvalOfflineDryCampaignRunResult(EvalSuiteContractModel):
    """Filesystem result from one deterministic offline fake campaign run."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    dry_campaign_plan: EvalOfflineDryCampaignPlan
    report_id: StrictStr
    campaign_store_root: StrictStr
    manifest_relative_path: StrictStr
    plan_relative_path: StrictStr
    trials_relative_path: StrictStr
    index_relative_path: StrictStr
    artifact_root_relative_path: StrictStr
    report_relative_paths: Mapping[StrictStr, StrictStr]
    completed_trial_ids: tuple[StrictStr, ...]
    pending_trial_ids: tuple[StrictStr, ...]
    appended_trial_ids: tuple[StrictStr, ...]
    trial_plan_hashes: tuple[StrictStr, ...]
    trial_record_hashes: tuple[StrictStr, ...]
    resume_index_hash: StrictStr
    report_hash: StrictStr
    report_json_hash: StrictStr
    report_markdown_hash: StrictStr
    closure_evidence_relative_path: StrictStr
    closure_evidence_hash: StrictStr
    live_execution_admitted: StrictBool = False

    @model_validator(mode="after")
    def _run_result_valid(self) -> EvalOfflineDryCampaignRunResult:
        if self.schema_version != EVAL_SUITE_SCHEMA_VERSION:
            raise ValueError("unsupported eval-suite dry-campaign schema_version")
        if self.live_execution_admitted:
            raise ValueError("dry campaigns do not admit live execution")
        if self.campaign_store_root != self.dry_campaign_plan.campaign_store_root:
            raise ValueError("campaign store root must match dry-campaign plan")
        for path_value in (
            self.campaign_store_root,
            self.manifest_relative_path,
            self.plan_relative_path,
            self.trials_relative_path,
            self.index_relative_path,
            self.artifact_root_relative_path,
            self.closure_evidence_relative_path,
            *self.report_relative_paths.values(),
        ):
            _validate_relative_artifact_root(path_value)
        expected_prefix = f"{self.campaign_store_root}/"
        for path_value in (
            self.manifest_relative_path,
            self.plan_relative_path,
            self.trials_relative_path,
            self.index_relative_path,
            self.artifact_root_relative_path,
            self.closure_evidence_relative_path,
            *self.report_relative_paths.values(),
        ):
            if not path_value.startswith(expected_prefix):
                raise ValueError("dry-campaign output paths must be campaign-relative")
        for digest in (
            *self.trial_plan_hashes,
            *self.trial_record_hashes,
            self.resume_index_hash,
            self.report_hash,
            self.report_json_hash,
            self.report_markdown_hash,
            self.closure_evidence_hash,
        ):
            _validate_sha256(digest)
        if set(self.report_relative_paths) != {
            "report.json",
            "report.md",
            "report.sha256",
        }:
            raise ValueError("dry-campaign reports must include the public report set")
        object.__setattr__(
            self,
            "report_relative_paths",
            _freeze_eval_suite_mapping(self.report_relative_paths),
        )
        return self


class EvalOfflineDryCampaignClosureEvidence(EvalSuiteContractModel):
    """Compact public closure evidence for one offline dry-campaign run."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    campaign_id: StrictStr
    report_id: StrictStr
    fixture_pack_hash: StrictStr
    campaign_manifest_hash: StrictStr
    model_manifest_hash: StrictStr
    workflow_graph_hash: StrictStr
    trial_plan_hashes: tuple[StrictStr, ...]
    trial_record_hashes: tuple[StrictStr, ...]
    resume_index_hash: StrictStr
    report_hash: StrictStr
    report_json_hash: StrictStr
    report_markdown_hash: StrictStr
    counts: Mapping[StrictStr, StrictInt]
    unresolved_live_dependencies: tuple[StrictStr, ...]
    live_denial_diagnostic_codes: tuple[StrictStr, ...]
    live_denial_test_coverage: tuple[StrictStr, ...]
    treatment_compiled_harness_hashes: Mapping[StrictStr, StrictStr]
    public_hygiene_checks: Mapping[StrictStr, StrictBool]
    claim_boundary: StrictStr
    closure_evidence_hash_kind: StrictStr = EVAL_SUITE_CLOSURE_EVIDENCE_HASH_KIND
    closure_evidence_hash: StrictStr

    @model_validator(mode="after")
    def _closure_evidence_valid(self) -> EvalOfflineDryCampaignClosureEvidence:
        if self.schema_version != EVAL_SUITE_SCHEMA_VERSION:
            raise ValueError("unsupported eval-suite closure-evidence schema_version")
        for digest in (
            self.fixture_pack_hash,
            self.campaign_manifest_hash,
            self.model_manifest_hash,
            self.workflow_graph_hash,
            *self.trial_plan_hashes,
            *self.trial_record_hashes,
            self.resume_index_hash,
            self.report_hash,
            self.report_json_hash,
            self.report_markdown_hash,
            *self.treatment_compiled_harness_hashes.values(),
            self.closure_evidence_hash,
        ):
            _validate_sha256(digest)
        if self.closure_evidence_hash_kind != EVAL_SUITE_CLOSURE_EVIDENCE_HASH_KIND:
            raise ValueError("unsupported closure evidence hash kind")
        required_counts = {
            "fixture_count",
            "paired_plan_count",
            "arm_trial_count",
            "completed_trial_count",
            "pending_trial_count",
            "stored_trial_count",
            "report_artifact_count",
        }
        if set(self.counts) != required_counts:
            raise ValueError("closure evidence counts must use the compact count set")
        if any(count < 0 for count in self.counts.values()):
            raise ValueError("closure evidence counts must be non-negative")
        required_hygiene = {
            "absolute_paths_absent",
            "home_paths_absent",
            "urls_absent",
            "auth_material_absent",
            "runtime_state_absent",
            "hidden_material_absent",
            "raw_logs_absent",
        }
        if set(self.public_hygiene_checks) != required_hygiene:
            raise ValueError("closure evidence must declare all public hygiene checks")
        if not all(self.public_hygiene_checks.values()):
            raise ValueError("closure evidence public hygiene checks must pass")
        if not self.unresolved_live_dependencies:
            raise ValueError("closure evidence must retain live dependency boundaries")
        if not self.live_denial_diagnostic_codes:
            raise ValueError("closure evidence must cite live denial diagnostics")
        if not self.live_denial_test_coverage:
            raise ValueError("closure evidence must cite denial regression coverage")
        if not self.treatment_compiled_harness_hashes:
            raise ValueError("closure evidence must include Spec 07E harness hashes")
        object.__setattr__(self, "counts", _freeze_eval_suite_mapping(self.counts))
        object.__setattr__(
            self,
            "treatment_compiled_harness_hashes",
            _freeze_eval_suite_mapping(self.treatment_compiled_harness_hashes),
        )
        object.__setattr__(
            self,
            "public_hygiene_checks",
            _freeze_eval_suite_mapping(self.public_hygiene_checks),
        )
        return self


class EvalModelPricingMetadata(EvalSuiteContractModel):
    """Public numeric model pricing metadata with labels kept separate."""

    input_cost_per_million_tokens: StrictFloat = Field(ge=0.0)
    output_cost_per_million_tokens: StrictFloat = Field(ge=0.0)
    cached_input_cost_per_million_tokens: StrictFloat = Field(ge=0.0)
    currency_label: StrictStr
    source_label: StrictStr


class EvalModelRateLimitMetadata(EvalSuiteContractModel):
    """Public numeric model rate-limit metadata with labels kept separate."""

    request_rate_per_window: StrictInt = Field(ge=0)
    token_rate_per_window: StrictInt = Field(ge=0)
    concurrent_request_limit: StrictInt = Field(ge=0)
    window_seconds: StrictInt = Field(ge=0)
    source_label: StrictStr


class EvalModelManifest(EvalSuiteContractModel):
    """Backend-neutral campaign model manifest without endpoints or secrets."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    model_manifest_id: StrictStr
    model_profile_id: StrictStr
    model_profile_hash: StrictStr
    provider_label: StrictStr
    model_or_artifact_id: StrictStr
    release_or_snapshot: StrictStr
    serving_protocol: StrictStr
    endpoint_class: EvalCampaignKind
    temperature: StrictFloat = Field(ge=0.0, le=2.0)
    top_p: StrictFloat = Field(gt=0.0, le=1.0)
    max_prompt_tokens: StrictInt = Field(gt=0)
    max_completion_tokens: StrictInt = Field(gt=0)
    max_total_tokens: StrictInt = Field(gt=0)
    tool_calling_mode: StrictStr
    parser_id: StrictStr
    seed_policy: StrictStr
    context_window_tokens: StrictInt = Field(gt=0)
    reasoning_controls: Mapping[StrictStr, StrictStr] = Field(default_factory=dict)
    public_pricing: EvalModelPricingMetadata
    public_rate_limits: EvalModelRateLimitMetadata
    public_pricing_snapshot: Mapping[StrictStr, StrictStr] = Field(default_factory=dict)
    public_rate_limit_snapshot: Mapping[StrictStr, StrictStr] = Field(
        default_factory=dict
    )
    local_serving_snapshot: Mapping[StrictStr, StrictStr] = Field(default_factory=dict)
    model_manifest_hash_kind: StrictStr = EVAL_SUITE_MODEL_MANIFEST_HASH_KIND
    model_manifest_hash: StrictStr

    @model_validator(mode="after")
    def _model_manifest_valid(self) -> EvalModelManifest:
        if self.schema_version != EVAL_SUITE_SCHEMA_VERSION:
            raise ValueError("unsupported eval-suite model schema_version")
        if self.max_total_tokens != self.max_prompt_tokens + self.max_completion_tokens:
            raise ValueError(
                "max_total_tokens must equal prompt plus completion tokens"
            )
        if self.context_window_tokens < self.max_total_tokens:
            raise ValueError("context_window_tokens must cover max_total_tokens")
        object.__setattr__(
            self,
            "reasoning_controls",
            _freeze_eval_suite_mapping(self.reasoning_controls),
        )
        object.__setattr__(
            self,
            "public_pricing_snapshot",
            _freeze_eval_suite_mapping(self.public_pricing_snapshot),
        )
        object.__setattr__(
            self,
            "public_rate_limit_snapshot",
            _freeze_eval_suite_mapping(self.public_rate_limit_snapshot),
        )
        object.__setattr__(
            self,
            "local_serving_snapshot",
            _freeze_eval_suite_mapping(self.local_serving_snapshot),
        )
        if self.model_manifest_hash_kind != EVAL_SUITE_MODEL_MANIFEST_HASH_KIND:
            raise ValueError("unsupported eval-suite model hash kind")
        _validate_sha256(self.model_profile_hash)
        _validate_sha256(self.model_manifest_hash)
        expected = calculate_eval_model_manifest_hash(self)
        if self.model_manifest_hash != expected:
            raise ValueError("model_manifest_hash does not match manifest payload")
        return self


class EvalDifficultyMetadata(EvalSuiteContractModel):
    """Public fixture difficulty metadata."""

    level: EvalDifficultyLevel
    rationale: StrictStr
    estimated_minutes: StrictInt = Field(gt=0)


class EvalVisibleCheck(EvalSuiteContractModel):
    """Runner-visible check descriptor."""

    check_id: StrictStr
    summary: StrictStr
    command: StrictStr | None = None

    @model_validator(mode="after")
    def _visible_check_valid(self) -> EvalVisibleCheck:
        if self.command is not None:
            _validate_public_command(self.command)
        return self


class EvalHiddenCheck(EvalSuiteContractModel):
    """Scorer-only hidden check descriptor."""

    check_id: StrictStr
    summary: StrictStr
    scorer_rubric: StrictStr


class EvalExpectedMutationPolicy(EvalSuiteContractModel):
    """Expected workspace mutation policy for scorer use."""

    mutation_kind: EvalExpectedMutationKind
    allowed_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    forbidden_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    summary: StrictStr

    @model_validator(mode="after")
    def _mutation_policy_valid(self) -> EvalExpectedMutationPolicy:
        for path in self.allowed_paths + self.forbidden_paths:
            _validate_relative_path(path)
        if (
            self.mutation_kind == EvalExpectedMutationKind.NO_SOURCE_CHANGE
            and self.allowed_paths
        ):
            raise ValueError("no-source-change policies cannot allow mutation paths")
        return self


class EvalTaskFixture(EvalSuiteContractModel):
    """Immutable scorer-owned fixture contract."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    fixture_id: StrictStr
    category: EvalTaskCategory
    difficulty: EvalDifficultyMetadata
    visible_prompt: StrictStr
    visible_acceptance_criteria: tuple[StrictStr, ...]
    file_allowlist: tuple[StrictStr, ...]
    expected_mutation_policy: EvalExpectedMutationPolicy
    file_manifest_hashes: tuple[EvalHashRecord, ...] = Field(default_factory=tuple)
    visible_checks: tuple[EvalVisibleCheck, ...]
    hidden_checks: tuple[EvalHiddenCheck, ...]
    expected_final_outcome: EvalTrialOutcome
    fixture_hash_kind: StrictStr = EVAL_SUITE_FIXTURE_HASH_KIND
    fixture_hash: StrictStr

    @model_validator(mode="after")
    def _fixture_valid(self) -> EvalTaskFixture:
        if self.schema_version != EVAL_SUITE_SCHEMA_VERSION:
            raise ValueError("unsupported eval-suite fixture schema_version")
        if not self.visible_acceptance_criteria:
            raise ValueError("fixtures must include visible acceptance criteria")
        if not self.visible_checks:
            raise ValueError("fixtures must include visible checks")
        if not self.hidden_checks:
            raise ValueError("fixtures must include scorer-only hidden checks")
        for path in self.file_allowlist:
            _validate_relative_path(path)
        if self.fixture_hash_kind != EVAL_SUITE_FIXTURE_HASH_KIND:
            raise ValueError("unsupported fixture hash kind")
        _validate_sha256(self.fixture_hash)
        expected = calculate_eval_task_fixture_hash(self)
        if self.fixture_hash != expected:
            raise ValueError("fixture_hash does not match fixture payload")
        return self


class EvalRunnerTaskProjection(EvalSuiteContractModel):
    """Runner-facing task projection without scorer-only fixture material."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    fixture_id: StrictStr
    category: EvalTaskCategory
    difficulty: EvalDifficultyMetadata
    visible_prompt: StrictStr
    visible_acceptance_criteria: tuple[StrictStr, ...]
    file_allowlist: tuple[StrictStr, ...]
    visible_checks: tuple[EvalVisibleCheck, ...]


class EvalRunnerAcceptanceProjection(EvalSuiteContractModel):
    """Runner-facing acceptance projection with visible criteria and checks only."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    fixture_id: StrictStr
    visible_acceptance_criteria: tuple[StrictStr, ...]
    visible_checks: tuple[EvalVisibleCheck, ...]


class EvalRunnerContextProjection(EvalSuiteContractModel):
    """Runner-facing context projection with only visible fixture context."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    fixture_id: StrictStr
    category: EvalTaskCategory
    difficulty: EvalDifficultyMetadata
    visible_prompt: StrictStr
    visible_acceptance_criteria: tuple[StrictStr, ...]
    file_allowlist: tuple[StrictStr, ...]
    visible_checks: tuple[EvalVisibleCheck, ...]


class EvalPublicArtifactProjection(EvalSuiteContractModel):
    """Public artifact projection that excludes scorer-only fixture answers."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    fixture_id: StrictStr
    category: EvalTaskCategory
    visible_acceptance_criteria: tuple[StrictStr, ...]
    file_allowlist: tuple[StrictStr, ...]
    visible_checks: tuple[EvalVisibleCheck, ...]


class EvalFixturePackSummary(EvalSuiteContractModel):
    """Hashable public summary of a fixture pack."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    fixture_pack_id: StrictStr
    fixture_ids: tuple[StrictStr, ...]
    category_counts: Mapping[EvalTaskCategory, StrictInt]
    fixture_hashes: tuple[EvalHashRecord, ...]
    pack_summary: StrictStr
    fixture_pack_hash_kind: StrictStr = EVAL_SUITE_FIXTURE_PACK_HASH_KIND
    fixture_pack_hash: StrictStr

    @model_validator(mode="after")
    def _fixture_pack_valid(self) -> EvalFixturePackSummary:
        if self.schema_version != EVAL_SUITE_SCHEMA_VERSION:
            raise ValueError("unsupported eval-suite fixture pack schema_version")
        if len(set(self.fixture_ids)) != len(self.fixture_ids):
            raise ValueError("fixture pack fixture_ids must be unique")
        if len(self.fixture_hashes) != len(self.fixture_ids):
            raise ValueError("fixture pack hashes must match fixture_ids")
        object.__setattr__(
            self,
            "category_counts",
            _freeze_eval_suite_mapping(self.category_counts),
        )
        if self.fixture_pack_hash_kind != EVAL_SUITE_FIXTURE_PACK_HASH_KIND:
            raise ValueError("unsupported fixture pack hash kind")
        _validate_sha256(self.fixture_pack_hash)
        expected = calculate_eval_fixture_pack_hash(self)
        if self.fixture_pack_hash != expected:
            raise ValueError("fixture_pack_hash does not match summary payload")
        return self


class EvalCapabilityAuditSummary(EvalSuiteContractModel):
    """Scorer input summary of capability-envelope enforcement."""

    capability_violation: StrictBool
    denied_capability_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    summary: StrictStr


class EvalCheckResult(EvalSuiteContractModel):
    """Visible or hidden check result consumed by the scorer."""

    check_id: StrictStr
    passed: StrictBool
    diagnostic: StrictStr | None = None


class EvalScorerInput(EvalSuiteContractModel):
    """Deterministic scorer input contract without live runner behavior."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    trial_id: StrictStr
    fixture_id: StrictStr
    fixture_hash: StrictStr
    final_workspace_hash: StrictStr | None = None
    path_limited_workspace_hashes: tuple[EvalHashRecord, ...] = Field(
        default_factory=tuple
    )
    public_artifact_hashes: tuple[EvalHashRecord, ...] = Field(default_factory=tuple)
    required_public_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    provided_public_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    malformed_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    stage_terminal_results: tuple[StrictStr, ...]
    capability_audit: EvalCapabilityAuditSummary
    visible_check_results: tuple[EvalCheckResult, ...]
    hidden_check_results: tuple[EvalCheckResult, ...]
    claimed_mutation_present: StrictBool = True
    unauthorized_mutation: StrictBool = False
    checker_public_evidence_valid: StrictBool = True
    runtime_failure: StrictBool = False
    provider_failure: StrictBool = False
    invalid_trial_explanation: StrictStr | None = None
    scorer_input_hash_kind: StrictStr = EVAL_SUITE_SCORER_INPUT_HASH_KIND
    scorer_input_hash: StrictStr

    @model_validator(mode="after")
    def _scorer_input_valid(self) -> EvalScorerInput:
        if self.schema_version != EVAL_SUITE_SCHEMA_VERSION:
            raise ValueError("unsupported eval-suite scorer input schema_version")
        _validate_sha256(self.fixture_hash)
        if self.final_workspace_hash is not None:
            _validate_sha256(self.final_workspace_hash)
        _validate_public_artifact_ids(self.required_public_artifact_ids)
        _validate_public_artifact_ids(self.provided_public_artifact_ids)
        _validate_public_artifact_ids(self.malformed_artifact_ids)
        if len(set(self.required_public_artifact_ids)) != len(
            self.required_public_artifact_ids
        ):
            raise ValueError("required public artifact IDs must be unique")
        if len(set(self.provided_public_artifact_ids)) != len(
            self.provided_public_artifact_ids
        ):
            raise ValueError("provided public artifact IDs must be unique")
        if len(set(self.malformed_artifact_ids)) != len(self.malformed_artifact_ids):
            raise ValueError("malformed public artifact IDs must be unique")
        if self.scorer_input_hash_kind != EVAL_SUITE_SCORER_INPUT_HASH_KIND:
            raise ValueError("unsupported scorer input hash kind")
        _validate_sha256(self.scorer_input_hash)
        expected = calculate_eval_scorer_input_hash(self)
        if self.scorer_input_hash != expected:
            raise ValueError("scorer_input_hash does not match input payload")
        return self


class EvalScorerResult(EvalSuiteContractModel):
    """Deterministic scorer result contract."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    trial_id: StrictStr
    fixture_id: StrictStr
    final_outcome: EvalTrialOutcome
    primary_success: StrictBool
    false_closure: StrictBool
    false_success: StrictBool
    correctly_blocked: StrictBool
    capability_violation: StrictBool
    artifact_complete: StrictBool
    missing_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    malformed_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    failure_labels: tuple[EvalFailureTaxonomyLabel, ...] = Field(default_factory=tuple)
    public_diagnostics: tuple[StrictStr, ...] = Field(default_factory=tuple)
    scorer_only_diagnostics: tuple[StrictStr, ...] = Field(default_factory=tuple)
    invalid_trial_explanation: StrictStr | None = None
    scorer_version: StrictStr = EVAL_SUITE_DEFAULT_SCORER_VERSION
    result_hash_kind: StrictStr = EVAL_SUITE_SCORER_RESULT_HASH_KIND
    result_hash: StrictStr

    @model_validator(mode="after")
    def _scorer_result_valid(self) -> EvalScorerResult:
        if self.schema_version != EVAL_SUITE_SCHEMA_VERSION:
            raise ValueError("unsupported eval-suite scorer result schema_version")
        if (
            self.final_outcome == EvalTrialOutcome.INVALID_TRIAL
            and not self.invalid_trial_explanation
        ):
            raise ValueError("invalid_trial outcomes require an explanation")
        if (
            self.final_outcome != EvalTrialOutcome.INVALID_TRIAL
            and self.invalid_trial_explanation
        ):
            raise ValueError(
                "invalid_trial_explanation is only valid for invalid_trial"
            )
        if self.final_outcome == EvalTrialOutcome.VALID_COMPLETION:
            valid_completion_has_failure_evidence = (
                self.false_closure
                or self.false_success
                or self.correctly_blocked
                or self.capability_violation
                or not self.artifact_complete
                or self.missing_artifact_ids
                or self.malformed_artifact_ids
                or self.failure_labels
            )
            if self.primary_success and valid_completion_has_failure_evidence:
                raise ValueError(
                    "valid_completion cannot include failure evidence or "
                    "inconsistent success flags"
                )
            if not self.primary_success and not valid_completion_has_failure_evidence:
                raise ValueError(
                    "non-primary valid_completion requires failure evidence"
                )
            if self.false_closure:
                raise ValueError("valid_completion cannot include false_closure")
        if (
            self.false_closure
            and EvalFailureTaxonomyLabel.CAPABILITY_VIOLATION in self.failure_labels
        ):
            if not self.capability_violation:
                raise ValueError(
                    "capability violation label requires capability_violation"
                )
        if self.result_hash_kind != EVAL_SUITE_SCORER_RESULT_HASH_KIND:
            raise ValueError("unsupported scorer result hash kind")
        _validate_sha256(self.result_hash)
        expected = calculate_eval_scorer_result_hash(self)
        if self.result_hash != expected:
            raise ValueError("result_hash does not match result payload")
        return self


class EvalCampaignManifest(EvalSuiteContractModel):
    """Campaign contract tying modes, model, workflow, fixtures, and scorer."""

    schema_version: StrictInt = EVAL_SUITE_SCHEMA_VERSION
    campaign_id: StrictStr
    campaign_kind: EvalCampaignKind
    execution_mode: EvalSuiteExecutionMode
    pi_eval_mode_id: StrictStr
    millforge_eval_mode_id: StrictStr
    model_manifest_ref: StrictStr
    model_manifest_hash: StrictStr
    workflow_graph_hash: StrictStr
    fixture_pack_hash: StrictStr
    scorer_version: StrictStr
    created_at: StrictStr
    budget_policy_ref: EvalBudgetPolicyReference
    live_execution_admitted: StrictBool
    live_denial_diagnostics: tuple[EvalLiveDenialDiagnostic, ...]
    campaign_manifest_hash_kind: StrictStr = EVAL_SUITE_CAMPAIGN_MANIFEST_HASH_KIND
    campaign_manifest_hash: StrictStr

    @model_validator(mode="after")
    def _campaign_manifest_valid(self) -> EvalCampaignManifest:
        if self.schema_version != EVAL_SUITE_SCHEMA_VERSION:
            raise ValueError("unsupported eval-suite campaign schema_version")
        if not _UTC_TIMESTAMP_RE.fullmatch(self.created_at):
            raise ValueError("campaign created_at must be a UTC timestamp")
        for digest in (
            self.model_manifest_hash,
            self.workflow_graph_hash,
            self.fixture_pack_hash,
            self.campaign_manifest_hash,
        ):
            _validate_sha256(digest)
        if self.execution_mode == EvalSuiteExecutionMode.OFFLINE_FAKE:
            if self.live_execution_admitted:
                raise ValueError(
                    "offline eval-suite campaigns cannot admit live execution"
                )
            if not self.live_denial_diagnostics:
                raise ValueError(
                    "offline campaigns must include live-denial diagnostics"
                )
        if self.execution_mode == EvalSuiteExecutionMode.LIVE_RUNNER:
            raise ValueError(
                "08A eval-suite campaign contracts do not admit live execution"
            )
        if self.campaign_manifest_hash_kind != EVAL_SUITE_CAMPAIGN_MANIFEST_HASH_KIND:
            raise ValueError("unsupported campaign manifest hash kind")
        expected = calculate_eval_campaign_manifest_hash(self)
        if self.campaign_manifest_hash != expected:
            raise ValueError("campaign_manifest_hash does not match manifest payload")
        return self


def eval_model_manifest_from_profile(
    profile: EvalModelProfile | None = None,
    *,
    model_manifest_id: str = "eval.08a.model.backend_neutral.default.v1",
) -> EvalModelManifest:
    """Build a campaign-grade manifest from the shared eval model profile."""
    profile = profile or default_eval_model_profile()
    manifest = EvalModelManifest.model_construct(
        schema_version=EVAL_SUITE_SCHEMA_VERSION,
        model_manifest_id=model_manifest_id,
        model_profile_id=profile.profile_id,
        model_profile_hash=profile.model_profile_hash,
        provider_label=profile.provider_label,
        model_or_artifact_id=profile.model_label,
        release_or_snapshot="static-backend-neutral-profile",
        serving_protocol=profile.serving_protocol,
        endpoint_class=EvalCampaignKind.LOCAL_OPENAI_COMPATIBLE,
        temperature=profile.temperature,
        top_p=profile.top_p,
        max_prompt_tokens=profile.max_prompt_tokens,
        max_completion_tokens=profile.max_completion_tokens,
        max_total_tokens=profile.max_total_tokens,
        tool_calling_mode=profile.tool_calling_mode,
        parser_id=profile.parser_id,
        seed_policy="no_seed",
        context_window_tokens=profile.max_total_tokens,
        reasoning_controls={"reasoning_effort": profile.reasoning_effort},
        public_pricing=EvalModelPricingMetadata(
            input_cost_per_million_tokens=0.0,
            output_cost_per_million_tokens=0.0,
            cached_input_cost_per_million_tokens=0.0,
            currency_label="none",
            source_label="static_descriptor",
        ),
        public_rate_limits=EvalModelRateLimitMetadata(
            request_rate_per_window=0,
            token_rate_per_window=0,
            concurrent_request_limit=0,
            window_seconds=0,
            source_label="not_applicable",
        ),
        public_pricing_snapshot=dict(profile.cost_accounting),
        public_rate_limit_snapshot={"rate_limit_source": "not_applicable"},
        local_serving_snapshot={"serving_snapshot": "not_applicable"},
        model_manifest_hash_kind=EVAL_SUITE_MODEL_MANIFEST_HASH_KIND,
        model_manifest_hash="0" * 64,
    )
    payload = manifest.model_dump(mode="json")
    payload["model_manifest_hash"] = calculate_eval_model_manifest_hash(manifest)
    return EvalModelManifest.model_validate(payload)


def default_eval_suite_campaign_manifest(
    *,
    model_manifest: EvalModelManifest | None = None,
    fixture_pack_hash: str | None = None,
    created_at: str = EVAL_SUITE_DEFAULT_CAMPAIGN_CREATED_AT,
) -> EvalCampaignManifest:
    """Return the default 08A offline-only campaign manifest."""
    model_manifest = model_manifest or eval_model_manifest_from_profile()
    if fixture_pack_hash is None:
        fixture_pack_hash = load_eval_fixture_pack_summary().fixture_pack_hash
    manifest = EvalCampaignManifest.model_construct(
        schema_version=EVAL_SUITE_SCHEMA_VERSION,
        campaign_id=EVAL_SUITE_DEFAULT_CAMPAIGN_ID,
        campaign_kind=EvalCampaignKind.LOCAL_OPENAI_COMPATIBLE,
        execution_mode=EvalSuiteExecutionMode.OFFLINE_FAKE,
        pi_eval_mode_id=EVAL_SMALL_PI_MODE_ID,
        millforge_eval_mode_id=EVAL_SMALL_MILLFORGE_MODE_ID,
        model_manifest_ref=model_manifest.model_manifest_id,
        model_manifest_hash=model_manifest.model_manifest_hash,
        workflow_graph_hash=compact_eval_workflow_snapshot()["graph_sha256"],
        fixture_pack_hash=fixture_pack_hash,
        scorer_version=EVAL_SUITE_DEFAULT_SCORER_VERSION,
        created_at=created_at,
        budget_policy_ref=EvalBudgetPolicyReference(
            policy_id="eval.08a.default.offline_budget.v1",
            summary="Static offline fixture and scorer contract budget reference.",
        ),
        live_execution_admitted=False,
        live_denial_diagnostics=(
            EvalLiveDenialDiagnostic(
                diagnostic_code="MF-EVAL-SUITE-001",
                summary="08A default campaign is offline-only and denies live execution.",
                rule_id="eval_suite.default_campaign.offline_only",
            ),
        ),
        campaign_manifest_hash_kind=EVAL_SUITE_CAMPAIGN_MANIFEST_HASH_KIND,
        campaign_manifest_hash="0" * 64,
    )
    payload = manifest.model_dump(mode="json")
    payload["campaign_manifest_hash"] = calculate_eval_campaign_manifest_hash(manifest)
    return EvalCampaignManifest.model_validate(payload)


def configure_offline_fake_eval_campaign(
    *,
    output_root: str | Path,
    budget_policy: Any,
    fixture_pack_id: str = EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID,
    fixture_ids: tuple[str, ...] | None = None,
    deterministic_seed: int = 0,
    trial_count_per_fixture_per_arm: int = 1,
    fake_runner_script: Any | None = None,
    allow_live_execution: bool = False,
    allow_live_model_call: bool = False,
    allow_pi_execution: bool = False,
    allow_millforge_harness_execution: bool = False,
    created_at: str = EVAL_SUITE_DEFAULT_CAMPAIGN_CREATED_AT,
) -> EvalOfflineDryCampaignPlan:
    """Preflight a deterministic offline fake campaign without writing records."""
    _reject_offline_dry_live_flags(
        allow_live_execution=allow_live_execution,
        allow_live_model_call=allow_live_model_call,
        allow_pi_execution=allow_pi_execution,
        allow_millforge_harness_execution=allow_millforge_harness_execution,
    )
    if fixture_pack_id != EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID:
        raise ValueError(
            _offline_dry_diagnostic(
                EvalOfflineDryCampaignDiagnosticCode.FIXTURE_PACK_UNAVAILABLE,
                "eval_suite.dry_campaign.fixture_pack",
                "Only the installed default offline fixture pack is available.",
            ).summary
        )
    if trial_count_per_fixture_per_arm <= 0:
        raise ValueError(
            _offline_dry_diagnostic(
                EvalOfflineDryCampaignDiagnosticCode.INVALID_TRIAL_COUNT,
                "eval_suite.dry_campaign.trial_count",
                "Dry campaigns require a positive trial count per fixture per arm.",
            ).summary
        )
    _validate_offline_dry_output_root(output_root)

    fixture_pack = load_eval_fixture_pack_summary()
    fixtures_by_id = {
        fixture.fixture_id: fixture for fixture in load_eval_task_fixtures()
    }
    selected_ids = fixture_ids or fixture_pack.fixture_ids
    missing_ids = tuple(
        fixture_id for fixture_id in selected_ids if fixture_id not in fixtures_by_id
    )
    if missing_ids:
        raise ValueError("unknown dry-campaign fixture ID")
    selected_fixtures = tuple(fixtures_by_id[fixture_id] for fixture_id in selected_ids)
    expanded_fixtures = tuple(
        fixture
        for fixture in selected_fixtures
        for _ in range(trial_count_per_fixture_per_arm)
    )
    trial_indexes = tuple(range(len(expanded_fixtures)))

    campaign_manifest = default_eval_suite_campaign_manifest(
        fixture_pack_hash=fixture_pack.fixture_pack_hash,
        created_at=created_at,
    )
    from millforge.eval_reports import (
        EvalBudgetUsageEstimate,
        EvalReportBudgetPolicy,
        validate_eval_budget_policy,
    )
    from millforge.eval_trials import (
        EvalTrialArmId,
        canonical_eval_trial_store_manifest_bytes,
        plan_paired_eval_trials,
    )

    if budget_policy is None:
        raise ValueError(
            _offline_dry_diagnostic(
                EvalOfflineDryCampaignDiagnosticCode.MISSING_BUDGET_POLICY,
                "eval_suite.dry_campaign.budget_policy",
                "Budget policy metadata is required for dry-campaign preflight.",
            ).summary
        )
    budget_policy = EvalReportBudgetPolicy.model_validate(budget_policy)
    budget_result = validate_eval_budget_policy(
        budget_policy,
        campaign_manifest=campaign_manifest,
        usage=EvalBudgetUsageEstimate(
            estimated_spend_usd=0.0,
            prompt_tokens=0,
            completion_tokens=0,
            model_calls=0,
            retries_per_trial=0,
            wall_clock_seconds=0,
            trial_count=len(expanded_fixtures) * 2,
        ),
    )
    if not budget_result.valid:
        raise ValueError(
            _offline_dry_diagnostic(
                EvalOfflineDryCampaignDiagnosticCode.INVALID_BUDGET_POLICY,
                "eval_suite.dry_campaign.budget_policy",
                budget_result.diagnostics[0].summary,
            ).summary
        )

    script = fake_runner_script or _default_offline_fake_runner_script()
    plans = plan_paired_eval_trials(
        fixtures=expanded_fixtures,
        fake_runner_script=script,
        seed=deterministic_seed,
        campaign_manifest=campaign_manifest,
        trial_indexes=trial_indexes,
        created_at=created_at,
    )
    store_manifest = _offline_dry_store_manifest(plans[0])
    manifest_bytes = canonical_eval_trial_store_manifest_bytes(store_manifest)
    manifest_path = Path(output_root) / plans[0].campaign_store_root / "manifest.json"
    if manifest_path.exists() and manifest_path.read_bytes() != manifest_bytes:
        raise ValueError(
            _offline_dry_diagnostic(
                EvalOfflineDryCampaignDiagnosticCode.MANIFEST_CONFLICT,
                "eval_suite.dry_campaign.manifest_conflict",
                "Existing campaign manifest differs from dry-campaign preflight.",
            ).summary
        )

    config = EvalOfflineDryCampaignConfig(
        fixture_pack_id=fixture_pack_id,
        fixture_ids=selected_ids,
        deterministic_seed=deterministic_seed,
        trial_count_per_fixture_per_arm=trial_count_per_fixture_per_arm,
        output_root_hash=hashlib.sha256(str(output_root).encode("utf-8")).hexdigest(),
    )
    return EvalOfflineDryCampaignPlan(
        config=config,
        campaign_manifest=campaign_manifest,
        fixture_pack_summary=fixture_pack,
        admitted_arm_ids=(
            EvalTrialArmId.EVAL_SMALL_PI.value,
            EvalTrialArmId.EVAL_SMALL_MILLFORGE.value,
        ),
        paired_plan_count=len(plans),
        planned_arm_trial_count=len(plans) * 2,
        trial_plan_hashes=tuple(plan.plan_hash for plan in plans),
        campaign_store_root=plans[0].campaign_store_root,
        manifest_relative_path=f"{plans[0].campaign_store_root}/manifest.json",
        budget_policy_ref=EvalBudgetPolicyReference(
            policy_id=budget_policy.policy_id,
            summary=budget_policy.summary,
        ),
        live_execution_admitted=False,
        diagnostics=(),
    )


def run_offline_fake_eval_campaign(
    *,
    output_root: str | Path,
    budget_policy: Any,
    fixture_pack_id: str = EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID,
    fixture_ids: tuple[str, ...] | None = None,
    deterministic_seed: int = 0,
    trial_count_per_fixture_per_arm: int = 1,
    fake_runner_script: Any | None = None,
    report_id: str | None = None,
    allow_live_execution: bool = False,
    allow_live_model_call: bool = False,
    allow_pi_execution: bool = False,
    allow_millforge_harness_execution: bool = False,
    created_at: str = EVAL_SUITE_DEFAULT_CAMPAIGN_CREATED_AT,
) -> EvalOfflineDryCampaignRunResult:
    """Run a deterministic offline fake campaign under a caller-selected root."""
    dry_plan = configure_offline_fake_eval_campaign(
        output_root=output_root,
        budget_policy=budget_policy,
        fixture_pack_id=fixture_pack_id,
        fixture_ids=fixture_ids,
        deterministic_seed=deterministic_seed,
        trial_count_per_fixture_per_arm=trial_count_per_fixture_per_arm,
        fake_runner_script=fake_runner_script,
        allow_live_execution=allow_live_execution,
        allow_live_model_call=allow_live_model_call,
        allow_pi_execution=allow_pi_execution,
        allow_millforge_harness_execution=allow_millforge_harness_execution,
        created_at=created_at,
    )

    from millforge.eval_reports import (
        EvalReportBudgetPolicy,
        build_eval_report_artifact_bytes,
        build_eval_report_payload,
    )
    from millforge.eval_trials import (
        append_eval_trial_record_to_campaign_store,
        plan_paired_eval_trials,
        resume_eval_trial_campaign_store,
        run_offline_fake_eval_trial,
    )

    budget = EvalReportBudgetPolicy.model_validate(budget_policy)
    fixtures_by_id = {
        fixture.fixture_id: fixture for fixture in load_eval_task_fixtures()
    }
    selected_fixtures = tuple(
        fixtures_by_id[fixture_id] for fixture_id in dry_plan.config.fixture_ids
    )
    expanded_fixtures = tuple(
        fixture
        for fixture in selected_fixtures
        for _ in range(trial_count_per_fixture_per_arm)
    )
    plans = plan_paired_eval_trials(
        fixtures=expanded_fixtures,
        fake_runner_script=fake_runner_script or _default_offline_fake_runner_script(),
        seed=deterministic_seed,
        campaign_manifest=dry_plan.campaign_manifest,
        trial_indexes=tuple(range(len(expanded_fixtures))),
        created_at=created_at,
    )
    if tuple(plan.plan_hash for plan in plans) != dry_plan.trial_plan_hashes:
        raise ValueError("dry-campaign plan hashes changed after preflight")

    generated_records = {}
    for plan in plans:
        fixture = fixtures_by_id[plan.fixture_instance.fixture_id]
        generated_records[plan.trial_id] = run_offline_fake_eval_trial(
            plan,
            fixture=fixture,
        ).trial_record
    existing_records = _read_offline_dry_campaign_record_summaries(
        output_root,
        plans[0],
    )
    _validate_offline_dry_record_summaries_match_plans(
        records=existing_records,
        plans=plans,
        generated_records=generated_records,
    )
    completed_ids = {record["trial_id"] for record in existing_records}
    appended_trial_ids: list[str] = []
    for plan in plans:
        if plan.trial_id in completed_ids:
            continue
        append_eval_trial_record_to_campaign_store(
            output_root,
            plan=plan,
            record=generated_records[plan.trial_id],
            plans=plans,
        )
        appended_trial_ids.append(plan.trial_id)
        completed_ids.add(plan.trial_id)

    if appended_trial_ids:
        _offline_dry_output_path(
            output_root,
            f"{plans[0].campaign_store_root}/index.json",
        ).unlink(missing_ok=True)
    final_resume = resume_eval_trial_campaign_store(
        output_root,
        plan=plans[0],
        plans=plans,
    )
    if final_resume.diagnostics:
        raise ValueError(final_resume.diagnostics[0].summary)
    if final_resume.resume_index is None:
        raise ValueError("dry-campaign resume index was not written")

    records = tuple(generated_records[plan.trial_id] for plan in plans)
    payload = build_eval_report_payload(
        report_id=report_id or f"{dry_plan.campaign_manifest.campaign_id}.report.v1",
        campaign_manifest=dry_plan.campaign_manifest,
        plans=plans,
        records=records,
        resume_index=final_resume.resume_index,
        budget_policy=budget,
        generated_at=created_at,
    )
    report_artifacts = build_eval_report_artifact_bytes(payload)
    report_relative_paths = {
        name: f"{dry_plan.campaign_store_root}/reports/{name}"
        for name in sorted(report_artifacts)
    }
    for name, data in report_artifacts.items():
        path = _offline_dry_output_path(output_root, report_relative_paths[name])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    closure_evidence = build_offline_dry_campaign_closure_evidence(
        run_plan=dry_plan,
        report_id=payload.report_id,
        completed_trial_ids=final_resume.completed_trial_ids,
        pending_trial_ids=final_resume.pending_trial_ids,
        trial_record_hashes=tuple(record.record_hash for record in records),
        resume_index_hash=final_resume.resume_index.resume_index_hash,
        report_hash=payload.report_hash,
        report_json_hash=hashlib.sha256(report_artifacts["report.json"]).hexdigest(),
        report_markdown_hash=hashlib.sha256(report_artifacts["report.md"]).hexdigest(),
        report_artifact_count=len(report_artifacts),
        treatment_compiled_harness_hashes=records[0].compiled_harness_hashes,
    )
    closure_evidence_relative_path = (
        f"{dry_plan.campaign_store_root}/closure_evidence.json"
    )
    _offline_dry_output_path(output_root, closure_evidence_relative_path).write_bytes(
        canonical_offline_dry_campaign_closure_evidence_bytes(closure_evidence)
    )

    return EvalOfflineDryCampaignRunResult(
        dry_campaign_plan=dry_plan,
        report_id=payload.report_id,
        campaign_store_root=dry_plan.campaign_store_root,
        manifest_relative_path=dry_plan.manifest_relative_path,
        plan_relative_path=f"{dry_plan.campaign_store_root}/plan.json",
        trials_relative_path=f"{dry_plan.campaign_store_root}/trials.jsonl",
        index_relative_path=f"{dry_plan.campaign_store_root}/index.json",
        artifact_root_relative_path=f"{dry_plan.campaign_store_root}/artifacts",
        report_relative_paths=report_relative_paths,
        completed_trial_ids=final_resume.completed_trial_ids,
        pending_trial_ids=final_resume.pending_trial_ids,
        appended_trial_ids=tuple(appended_trial_ids),
        trial_plan_hashes=tuple(plan.plan_hash for plan in plans),
        trial_record_hashes=tuple(record.record_hash for record in records),
        resume_index_hash=final_resume.resume_index.resume_index_hash,
        report_hash=payload.report_hash,
        report_json_hash=hashlib.sha256(report_artifacts["report.json"]).hexdigest(),
        report_markdown_hash=hashlib.sha256(report_artifacts["report.md"]).hexdigest(),
        closure_evidence_relative_path=closure_evidence_relative_path,
        closure_evidence_hash=closure_evidence.closure_evidence_hash,
        live_execution_admitted=False,
    )


def build_offline_dry_campaign_closure_evidence(
    *,
    run_plan: EvalOfflineDryCampaignPlan,
    report_id: str,
    completed_trial_ids: tuple[str, ...],
    pending_trial_ids: tuple[str, ...],
    trial_record_hashes: tuple[str, ...],
    resume_index_hash: str,
    report_hash: str,
    report_json_hash: str,
    report_markdown_hash: str,
    report_artifact_count: int,
    treatment_compiled_harness_hashes: Mapping[str, str],
) -> EvalOfflineDryCampaignClosureEvidence:
    """Return compact public closure evidence for a dry-campaign result."""
    evidence = EvalOfflineDryCampaignClosureEvidence.model_construct(
        schema_version=EVAL_SUITE_SCHEMA_VERSION,
        campaign_id=run_plan.campaign_manifest.campaign_id,
        report_id=report_id,
        fixture_pack_hash=run_plan.fixture_pack_summary.fixture_pack_hash,
        campaign_manifest_hash=run_plan.campaign_manifest.campaign_manifest_hash,
        model_manifest_hash=run_plan.campaign_manifest.model_manifest_hash,
        workflow_graph_hash=run_plan.campaign_manifest.workflow_graph_hash,
        trial_plan_hashes=run_plan.trial_plan_hashes,
        trial_record_hashes=trial_record_hashes,
        resume_index_hash=resume_index_hash,
        report_hash=report_hash,
        report_json_hash=report_json_hash,
        report_markdown_hash=report_markdown_hash,
        counts={
            "fixture_count": len(run_plan.config.fixture_ids),
            "paired_plan_count": run_plan.paired_plan_count,
            "arm_trial_count": run_plan.planned_arm_trial_count,
            "completed_trial_count": len(completed_trial_ids),
            "pending_trial_count": len(pending_trial_ids),
            "stored_trial_count": len(completed_trial_ids),
            "report_artifact_count": report_artifact_count,
        },
        unresolved_live_dependencies=(
            "pi_runtime",
            "millforge_live_harness_execution",
            "shared_model_backend_configuration",
            "fixture_workspace_lifecycle_reset",
            "resource_enforcement",
        ),
        live_denial_diagnostic_codes=(
            "pi_runtime_unavailable",
            "millforge_live_harness_unavailable",
            "shared_backend_configuration_missing",
            "fixture_workspace_lifecycle_unavailable",
            "resource_enforcement_unavailable",
        ),
        live_denial_test_coverage=(
            "tests/test_eval_reports.py::test_live_admission_returns_all_unresolved_dependency_diagnostics",
            "tests/test_eval_modes.py::test_live_admission_fails_closed_with_structured_deferred_dependencies",
            "tests/test_eval_trials.py::test_execution_result_rejects_live_admission",
        ),
        treatment_compiled_harness_hashes=dict(
            sorted(treatment_compiled_harness_hashes.items())
        ),
        public_hygiene_checks={
            "absolute_paths_absent": True,
            "home_paths_absent": True,
            "urls_absent": True,
            "auth_material_absent": True,
            "runtime_state_absent": True,
            "hidden_material_absent": True,
            "raw_logs_absent": True,
        },
        claim_boundary=(
            "Offline fake closure evidence proves deterministic public contract "
            "outputs only; live Pi-vs-Millforge remains denied."
        ),
        closure_evidence_hash_kind=EVAL_SUITE_CLOSURE_EVIDENCE_HASH_KIND,
        closure_evidence_hash="0" * 64,
    )
    payload = evidence.model_dump(mode="json")
    payload["closure_evidence_hash"] = (
        calculate_offline_dry_campaign_closure_evidence_hash(evidence)
    )
    return EvalOfflineDryCampaignClosureEvidence.model_validate(payload)


def eval_runner_task_projection(fixture: EvalTaskFixture) -> EvalRunnerTaskProjection:
    """Return a runner-visible projection with scorer-only fields omitted."""
    return EvalRunnerTaskProjection(
        fixture_id=fixture.fixture_id,
        category=fixture.category,
        difficulty=fixture.difficulty,
        visible_prompt=fixture.visible_prompt,
        visible_acceptance_criteria=fixture.visible_acceptance_criteria,
        file_allowlist=fixture.file_allowlist,
        visible_checks=fixture.visible_checks,
    )


def eval_runner_acceptance_projection(
    fixture: EvalTaskFixture,
) -> EvalRunnerAcceptanceProjection:
    """Return visible acceptance criteria and checks for runner consumption."""
    return EvalRunnerAcceptanceProjection(
        fixture_id=fixture.fixture_id,
        visible_acceptance_criteria=fixture.visible_acceptance_criteria,
        visible_checks=fixture.visible_checks,
    )


def eval_runner_context_projection(
    fixture: EvalTaskFixture,
) -> EvalRunnerContextProjection:
    """Return visible fixture context for runner prompt assembly."""
    return EvalRunnerContextProjection(
        fixture_id=fixture.fixture_id,
        category=fixture.category,
        difficulty=fixture.difficulty,
        visible_prompt=fixture.visible_prompt,
        visible_acceptance_criteria=fixture.visible_acceptance_criteria,
        file_allowlist=fixture.file_allowlist,
        visible_checks=fixture.visible_checks,
    )


def eval_public_artifact_projection(
    fixture: EvalTaskFixture,
) -> EvalPublicArtifactProjection:
    """Return public fixture metadata safe to serialize into trial artifacts."""
    return EvalPublicArtifactProjection(
        fixture_id=fixture.fixture_id,
        category=fixture.category,
        visible_acceptance_criteria=fixture.visible_acceptance_criteria,
        file_allowlist=fixture.file_allowlist,
        visible_checks=fixture.visible_checks,
    )


def load_eval_task_fixtures() -> tuple[EvalTaskFixture, ...]:
    """Load the built-in offline eval fixtures from package resources."""
    manifest = _load_default_eval_fixture_pack_manifest()
    fixture_ids = tuple(manifest["fixture_ids"])
    fixture_root = files(_EVAL_FIXTURE_PACK_PACKAGE).joinpath("fixtures")

    fixtures = tuple(
        _load_eval_task_fixture_resource(
            fixture_root.joinpath(f"{fixture_id}.json"),
        )
        for fixture_id in fixture_ids
    )
    if tuple(fixture.fixture_id for fixture in fixtures) != fixture_ids:
        raise ValueError("fixture resource order does not match pack manifest")
    return fixtures


def load_eval_task_fixture(fixture_id: str) -> EvalTaskFixture:
    """Load a single built-in offline eval fixture by fixture ID."""
    fixtures_by_id = {
        fixture.fixture_id: fixture for fixture in load_eval_task_fixtures()
    }
    try:
        return fixtures_by_id[fixture_id]
    except KeyError as exc:
        raise KeyError(f"unknown eval-suite fixture_id: {fixture_id}") from exc


def load_eval_fixture_pack_summary() -> EvalFixturePackSummary:
    """Load the deterministic summary for the built-in offline fixture pack."""
    manifest = _load_default_eval_fixture_pack_manifest()
    fixtures = load_eval_task_fixtures()
    fixture_ids = tuple(fixture.fixture_id for fixture in fixtures)
    if fixture_ids != tuple(manifest["fixture_ids"]):
        raise ValueError("fixture pack manifest does not match loaded fixtures")

    category_counts = {
        category: sum(1 for fixture in fixtures if fixture.category == category)
        for category in EvalTaskCategory
        if any(fixture.category == category for fixture in fixtures)
    }
    summary = EvalFixturePackSummary.model_construct(
        fixture_pack_id=str(manifest["fixture_pack_id"]),
        fixture_ids=fixture_ids,
        category_counts=category_counts,
        fixture_hashes=tuple(
            EvalHashRecord(
                hash_kind=EVAL_SUITE_FIXTURE_HASH_KIND,
                sha256=fixture.fixture_hash,
            )
            for fixture in fixtures
        ),
        pack_summary=str(manifest["pack_summary"]),
        fixture_pack_hash_kind=EVAL_SUITE_FIXTURE_PACK_HASH_KIND,
        fixture_pack_hash="0" * 64,
    )
    payload = summary.model_dump(mode="json")
    payload["fixture_pack_hash"] = calculate_eval_fixture_pack_hash(summary)
    return EvalFixturePackSummary.model_validate(payload)


_SUCCESS_TERMINAL_RESULTS = frozenset(
    {
        "PLAN_READY",
        "BUILDER_COMPLETE",
        "CHECKER_APPROVED",
        "ARBITER_CLOSED",
    }
)
_CLOSURE_TERMINAL_RESULTS = frozenset({"ARBITER_CLOSED"})
_BLOCKED_TERMINAL_RESULTS = frozenset(
    {
        "PLAN_BLOCKED",
        "BUILDER_BLOCKED",
        "CHECKER_BLOCKED",
        "ARBITER_BLOCKED",
    }
)


def score_eval_trial(
    fixture: EvalTaskFixture,
    scorer_input: EvalScorerInput,
) -> EvalScorerResult:
    """Classify one offline eval trial with deterministic precedence."""
    if scorer_input.fixture_id != fixture.fixture_id:
        return _build_eval_scorer_result(
            scorer_input,
            final_outcome=EvalTrialOutcome.INVALID_TRIAL,
            primary_success=False,
            failure_labels=(EvalFailureTaxonomyLabel.INFRASTRUCTURE_DEFECT,),
            public_diagnostics=("Scorer input fixture_id does not match fixture.",),
            scorer_only_diagnostics=(
                f"expected fixture_id {fixture.fixture_id}; "
                f"got {scorer_input.fixture_id}",
            ),
            invalid_trial_explanation="scorer input fixture_id does not match fixture",
        )
    if scorer_input.fixture_hash != fixture.fixture_hash:
        return _build_eval_scorer_result(
            scorer_input,
            final_outcome=EvalTrialOutcome.INVALID_TRIAL,
            primary_success=False,
            failure_labels=(EvalFailureTaxonomyLabel.INFRASTRUCTURE_DEFECT,),
            public_diagnostics=("Scorer input fixture hash does not match fixture.",),
            scorer_only_diagnostics=(
                f"expected fixture_hash {fixture.fixture_hash}; "
                f"got {scorer_input.fixture_hash}",
            ),
            invalid_trial_explanation="scorer input fixture_hash does not match fixture",
        )
    if scorer_input.invalid_trial_explanation:
        return _build_eval_scorer_result(
            scorer_input,
            final_outcome=EvalTrialOutcome.INVALID_TRIAL,
            primary_success=False,
            failure_labels=(EvalFailureTaxonomyLabel.INFRASTRUCTURE_DEFECT,),
            public_diagnostics=("Evaluation infrastructure defect invalidated trial.",),
            scorer_only_diagnostics=(scorer_input.invalid_trial_explanation,),
            invalid_trial_explanation=scorer_input.invalid_trial_explanation,
        )

    missing_artifact_ids = tuple(
        artifact_id
        for artifact_id in scorer_input.required_public_artifact_ids
        if artifact_id not in set(scorer_input.provided_public_artifact_ids)
    )
    malformed_artifact_ids = scorer_input.malformed_artifact_ids
    visible_failed = any(
        not result.passed for result in scorer_input.visible_check_results
    )
    hidden_failed = any(
        not result.passed for result in scorer_input.hidden_check_results
    )
    expected_mutation_absent = (
        fixture.expected_mutation_policy.mutation_kind
        != EvalExpectedMutationKind.NO_SOURCE_CHANGE
        and not scorer_input.claimed_mutation_present
    )
    unauthorized_mutation = scorer_input.unauthorized_mutation
    artifact_complete = not missing_artifact_ids and not malformed_artifact_ids
    capability_violation = scorer_input.capability_audit.capability_violation
    invalid_public_evidence = not scorer_input.checker_public_evidence_valid
    evidence_defect = bool(
        visible_failed
        or hidden_failed
        or not artifact_complete
        or expected_mutation_absent
        or unauthorized_mutation
        or capability_violation
        or invalid_public_evidence
    )

    failure_labels = _eval_failure_labels(
        visible_failed=visible_failed,
        hidden_failed=hidden_failed,
        missing_artifact_ids=missing_artifact_ids,
        malformed_artifact_ids=malformed_artifact_ids,
        expected_mutation_absent=expected_mutation_absent,
        unauthorized_mutation=unauthorized_mutation,
        capability_violation=capability_violation,
        success_terminal_unsupported=_success_terminal_unsupported(
            scorer_input,
            visible_failed=visible_failed,
            hidden_failed=hidden_failed,
            artifact_complete=artifact_complete,
            expected_mutation_absent=expected_mutation_absent,
            unauthorized_mutation=unauthorized_mutation,
            capability_violation=capability_violation,
            invalid_public_evidence=invalid_public_evidence,
        ),
        invalid_public_evidence=invalid_public_evidence,
        provider_failure=scorer_input.provider_failure,
    )
    unsupported_success = (
        EvalFailureTaxonomyLabel.FALSE_SUCCESS_TERMINAL in failure_labels
    )
    final_closure_claimed = any(
        terminal in _CLOSURE_TERMINAL_RESULTS
        for terminal in scorer_input.stage_terminal_results
    )
    false_closure = final_closure_claimed and evidence_defect
    blocked = any(
        terminal in _BLOCKED_TERMINAL_RESULTS
        for terminal in scorer_input.stage_terminal_results
    )

    if scorer_input.provider_failure:
        outcome = EvalTrialOutcome.PROVIDER_FAILURE
    elif scorer_input.runtime_failure:
        outcome = EvalTrialOutcome.RUNTIME_FAILURE
    elif false_closure:
        outcome = EvalTrialOutcome.FALSE_CLOSURE
    elif (
        blocked and fixture.expected_final_outcome == EvalTrialOutcome.CORRECTLY_BLOCKED
    ):
        outcome = EvalTrialOutcome.CORRECTLY_BLOCKED
    elif blocked:
        outcome = EvalTrialOutcome.FALSE_BLOCKED
    else:
        outcome = EvalTrialOutcome.VALID_COMPLETION
    primary_success = (
        outcome == EvalTrialOutcome.VALID_COMPLETION
        and not evidence_defect
        and not unsupported_success
    )

    return _build_eval_scorer_result(
        scorer_input,
        final_outcome=outcome,
        primary_success=primary_success,
        false_closure=false_closure,
        false_success=unsupported_success,
        correctly_blocked=outcome == EvalTrialOutcome.CORRECTLY_BLOCKED,
        capability_violation=capability_violation,
        artifact_complete=artifact_complete,
        missing_artifact_ids=missing_artifact_ids,
        malformed_artifact_ids=malformed_artifact_ids,
        failure_labels=failure_labels,
        public_diagnostics=_eval_public_diagnostics(
            outcome,
            missing_artifact_ids=missing_artifact_ids,
            malformed_artifact_ids=malformed_artifact_ids,
            capability_violation=capability_violation,
            visible_failed=visible_failed,
            unsupported_success=unsupported_success,
        ),
        scorer_only_diagnostics=_eval_scorer_only_diagnostics(
            scorer_input,
            hidden_failed=hidden_failed,
            expected_mutation_absent=expected_mutation_absent,
            unauthorized_mutation=unauthorized_mutation,
            invalid_public_evidence=invalid_public_evidence,
        ),
    )


def canonical_eval_suite_bytes(value: BaseModel | Mapping[str, Any]) -> bytes:
    """Return canonical ASCII JSON bytes for an eval-suite payload."""
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    else:
        payload = dict(value)
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def canonical_offline_dry_campaign_closure_evidence_bytes(
    evidence: EvalOfflineDryCampaignClosureEvidence,
) -> bytes:
    """Return canonical ASCII JSON bytes for offline closure evidence."""
    return canonical_eval_suite_bytes(evidence)


def calculate_offline_dry_campaign_closure_evidence_hash(
    evidence: EvalOfflineDryCampaignClosureEvidence,
) -> str:
    """Return the closure evidence hash with the self-hash field omitted."""
    payload = evidence.model_dump(mode="json")
    payload.pop("closure_evidence_hash", None)
    return hashlib.sha256(canonical_eval_suite_bytes(payload)).hexdigest()


def calculate_eval_model_manifest_hash(manifest: EvalModelManifest) -> str:
    payload = manifest.model_dump(mode="json")
    payload.pop("model_manifest_hash", None)
    return hashlib.sha256(canonical_eval_suite_bytes(payload)).hexdigest()


def calculate_eval_campaign_manifest_hash(manifest: EvalCampaignManifest) -> str:
    payload = manifest.model_dump(mode="json")
    payload.pop("campaign_manifest_hash", None)
    return hashlib.sha256(canonical_eval_suite_bytes(payload)).hexdigest()


def calculate_eval_task_fixture_hash(fixture: EvalTaskFixture) -> str:
    payload = fixture.model_dump(mode="json")
    payload.pop("fixture_hash", None)
    return hashlib.sha256(canonical_eval_suite_bytes(payload)).hexdigest()


def calculate_eval_fixture_pack_hash(summary: EvalFixturePackSummary) -> str:
    payload = summary.model_dump(mode="json")
    payload.pop("fixture_pack_hash", None)
    return hashlib.sha256(canonical_eval_suite_bytes(payload)).hexdigest()


def calculate_eval_scorer_input_hash(scorer_input: EvalScorerInput) -> str:
    payload = scorer_input.model_dump(mode="json")
    payload.pop("scorer_input_hash", None)
    return hashlib.sha256(canonical_eval_suite_bytes(payload)).hexdigest()


def calculate_eval_scorer_result_hash(result: EvalScorerResult) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("result_hash", None)
    return hashlib.sha256(canonical_eval_suite_bytes(payload)).hexdigest()


def _build_eval_scorer_result(
    scorer_input: EvalScorerInput,
    *,
    final_outcome: EvalTrialOutcome,
    primary_success: bool,
    false_closure: bool = False,
    false_success: bool = False,
    correctly_blocked: bool = False,
    capability_violation: bool | None = None,
    artifact_complete: bool = True,
    missing_artifact_ids: tuple[str, ...] = (),
    malformed_artifact_ids: tuple[str, ...] = (),
    failure_labels: tuple[EvalFailureTaxonomyLabel, ...] = (),
    public_diagnostics: tuple[str, ...] = (),
    scorer_only_diagnostics: tuple[str, ...] = (),
    invalid_trial_explanation: str | None = None,
) -> EvalScorerResult:
    result = EvalScorerResult.model_construct(
        trial_id=scorer_input.trial_id,
        fixture_id=scorer_input.fixture_id,
        final_outcome=final_outcome,
        primary_success=primary_success,
        false_closure=false_closure,
        false_success=false_success,
        correctly_blocked=correctly_blocked,
        capability_violation=(
            scorer_input.capability_audit.capability_violation
            if capability_violation is None
            else capability_violation
        ),
        artifact_complete=artifact_complete,
        missing_artifact_ids=missing_artifact_ids,
        malformed_artifact_ids=malformed_artifact_ids,
        failure_labels=failure_labels,
        public_diagnostics=public_diagnostics,
        scorer_only_diagnostics=scorer_only_diagnostics,
        invalid_trial_explanation=invalid_trial_explanation,
        scorer_version=EVAL_SUITE_DEFAULT_SCORER_VERSION,
        result_hash_kind=EVAL_SUITE_SCORER_RESULT_HASH_KIND,
        result_hash="0" * 64,
    )
    return EvalScorerResult.model_validate(
        result.model_copy(
            update={"result_hash": calculate_eval_scorer_result_hash(result)}
        )
    )


def _success_terminal_unsupported(
    scorer_input: EvalScorerInput,
    *,
    visible_failed: bool,
    hidden_failed: bool,
    artifact_complete: bool,
    expected_mutation_absent: bool,
    unauthorized_mutation: bool,
    capability_violation: bool,
    invalid_public_evidence: bool,
) -> bool:
    success_terminal_emitted = any(
        terminal in _SUCCESS_TERMINAL_RESULTS
        for terminal in scorer_input.stage_terminal_results
    )
    return success_terminal_emitted and bool(
        visible_failed
        or hidden_failed
        or not artifact_complete
        or expected_mutation_absent
        or unauthorized_mutation
        or capability_violation
        or invalid_public_evidence
        or scorer_input.runtime_failure
        or scorer_input.provider_failure
    )


def _eval_failure_labels(
    *,
    visible_failed: bool,
    hidden_failed: bool,
    missing_artifact_ids: tuple[str, ...],
    malformed_artifact_ids: tuple[str, ...],
    expected_mutation_absent: bool,
    unauthorized_mutation: bool,
    capability_violation: bool,
    success_terminal_unsupported: bool,
    invalid_public_evidence: bool,
    provider_failure: bool,
) -> tuple[EvalFailureTaxonomyLabel, ...]:
    labels: list[EvalFailureTaxonomyLabel] = []
    if visible_failed:
        labels.append(EvalFailureTaxonomyLabel.VISIBLE_CHECK_FAILED)
    if hidden_failed:
        labels.append(EvalFailureTaxonomyLabel.HIDDEN_CHECK_FAILED)
    if missing_artifact_ids:
        labels.append(EvalFailureTaxonomyLabel.REQUIRED_ARTIFACT_MISSING)
    if malformed_artifact_ids:
        labels.append(EvalFailureTaxonomyLabel.REQUIRED_ARTIFACT_MALFORMED)
    if expected_mutation_absent:
        labels.append(EvalFailureTaxonomyLabel.EXPECTED_MUTATION_ABSENT)
    if unauthorized_mutation:
        labels.append(EvalFailureTaxonomyLabel.UNAUTHORIZED_MUTATION)
    if capability_violation:
        labels.append(EvalFailureTaxonomyLabel.CAPABILITY_VIOLATION)
    if success_terminal_unsupported or invalid_public_evidence:
        labels.append(EvalFailureTaxonomyLabel.FALSE_SUCCESS_TERMINAL)
    if provider_failure:
        labels.append(EvalFailureTaxonomyLabel.PROVIDER_DEFECT)
    return tuple(dict.fromkeys(labels))


def _eval_public_diagnostics(
    final_outcome: EvalTrialOutcome,
    *,
    missing_artifact_ids: tuple[str, ...],
    malformed_artifact_ids: tuple[str, ...],
    capability_violation: bool,
    visible_failed: bool,
    unsupported_success: bool,
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if missing_artifact_ids:
        diagnostics.append("Required public artifacts are missing.")
    if malformed_artifact_ids:
        diagnostics.append("Required public artifacts are malformed.")
    if capability_violation:
        diagnostics.append("Capability envelope violation was observed.")
    if visible_failed:
        diagnostics.append("One or more visible checks failed.")
    if unsupported_success:
        diagnostics.append("A success terminal was unsupported by required evidence.")
    if final_outcome == EvalTrialOutcome.PROVIDER_FAILURE:
        diagnostics.append("Provider failure prevented a valid trial completion.")
    if final_outcome == EvalTrialOutcome.RUNTIME_FAILURE:
        diagnostics.append("Runtime failure prevented a valid trial completion.")
    return tuple(diagnostics)


def _eval_scorer_only_diagnostics(
    scorer_input: EvalScorerInput,
    *,
    hidden_failed: bool,
    expected_mutation_absent: bool,
    unauthorized_mutation: bool,
    invalid_public_evidence: bool,
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if hidden_failed:
        failed_ids = tuple(
            result.check_id
            for result in scorer_input.hidden_check_results
            if not result.passed
        )
        diagnostics.append(f"Hidden check failures: {', '.join(failed_ids)}.")
    if expected_mutation_absent:
        diagnostics.append("Expected workspace mutation was absent.")
    if unauthorized_mutation:
        diagnostics.append("Unauthorized workspace mutation was observed.")
    if invalid_public_evidence:
        diagnostics.append("Checker approved using invalid public evidence.")
    return tuple(diagnostics)


def _validate_sha256(value: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("expected lowercase sha256 hex digest")


def _validate_relative_path(value: str) -> None:
    if (
        not value
        or value.startswith(".")
        or "\\" in value
        or value.startswith("/")
        or ".." in value.split("/")
    ):
        raise ValueError("fixture paths must be stable relative POSIX paths")
    _reject_forbidden_material(value)


def _validate_relative_artifact_root(value: str) -> None:
    if (
        not value
        or value.startswith(".")
        or value.startswith("/")
        or "\\" in value
        or "//" in value
        or ".." in value.split("/")
    ):
        raise ValueError("eval-suite artifact roots must be stable relative paths")
    _reject_forbidden_material(value)


def _validate_public_artifact_ids(values: tuple[str, ...]) -> None:
    for value in values:
        if not value or "/" in value or "\\" in value or value.startswith("."):
            raise ValueError("public artifact IDs must be stable bare identifiers")
        _reject_forbidden_material(value)


def _reject_offline_dry_live_flags(
    *,
    allow_live_execution: bool,
    allow_live_model_call: bool,
    allow_pi_execution: bool,
    allow_millforge_harness_execution: bool,
) -> None:
    if any(
        (
            allow_live_execution,
            allow_live_model_call,
            allow_pi_execution,
            allow_millforge_harness_execution,
        )
    ):
        raise ValueError(
            _offline_dry_diagnostic(
                EvalOfflineDryCampaignDiagnosticCode.LIVE_EXECUTION_UNAVAILABLE,
                "eval_suite.dry_campaign.live_execution",
                "Offline dry-campaign preflight rejects live execution flags.",
            ).summary
        )


def _validate_offline_dry_output_root(output_root: str | Path) -> None:
    root_text = str(output_root)
    if not root_text.strip():
        raise ValueError("output root is required")
    parts = tuple(part.lower() for part in Path(root_text).parts)
    unsafe_parts = {
        ".claude",
        ".codex",
        ".eval-scratch",
        ".millrace",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "ideas",
        "millrace-agents",
        "ref-forge",
    }
    if any(part in unsafe_parts for part in parts):
        raise ValueError(
            _offline_dry_diagnostic(
                EvalOfflineDryCampaignDiagnosticCode.UNSAFE_OUTPUT_ROOT,
                "eval_suite.dry_campaign.output_root",
                "Output root is under ignored control state.",
            ).summary
        )


def _offline_dry_output_path(output_root: str | Path, relative_path: str) -> Path:
    _validate_relative_artifact_root(relative_path)
    return Path(output_root).joinpath(*relative_path.split("/"))


def _read_offline_dry_campaign_record_summaries(
    output_root: str | Path,
    plan: Any,
) -> tuple[dict[str, str], ...]:
    trials_path = _offline_dry_output_path(
        output_root,
        f"{plan.campaign_store_root}/trials.jsonl",
    )
    if not trials_path.exists():
        return ()
    records: list[dict[str, str]] = []
    for line in trials_path.read_bytes().splitlines():
        if not line:
            continue
        payload = json.loads(line.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("existing trial record must be a JSON object")
        for field_name in ("trial_id", "trial_plan_hash", "record_hash"):
            if not isinstance(payload.get(field_name), str):
                raise ValueError("existing trial record is missing public hash fields")
        record_hash = payload["record_hash"]
        hash_payload = dict(payload)
        hash_payload.pop("record_hash")
        expected = hashlib.sha256(canonical_eval_suite_bytes(hash_payload)).hexdigest()
        if record_hash != expected:
            raise ValueError("existing trial record hash does not match public payload")
        records.append(
            {
                "trial_id": payload["trial_id"],
                "trial_plan_hash": payload["trial_plan_hash"],
                "record_hash": record_hash,
            }
        )
    return tuple(records)


def _validate_offline_dry_record_summaries_match_plans(
    *,
    records: tuple[dict[str, str], ...],
    plans: tuple[Any, ...],
    generated_records: Mapping[str, Any],
) -> None:
    plans_by_trial_id = {plan.trial_id: plan for plan in plans}
    if len(plans_by_trial_id) != len(plans):
        raise ValueError("dry-campaign plans must have unique trial IDs")
    seen_trial_ids: set[str] = set()
    for record in records:
        trial_id = record["trial_id"]
        if trial_id in seen_trial_ids:
            raise ValueError("duplicate trial IDs are rejected by append-only stores")
        seen_trial_ids.add(trial_id)
        plan = plans_by_trial_id.get(trial_id)
        if plan is None:
            raise ValueError(
                "existing trial record is not present in dry-campaign plan"
            )
        if record["trial_plan_hash"] != plan.plan_hash:
            raise ValueError("existing trial record plan hash does not match plan")
        generated_record = generated_records.get(trial_id)
        if generated_record is None or record["record_hash"] != (
            generated_record.record_hash
        ):
            raise ValueError("existing trial record hash does not match dry run")


def _default_offline_fake_runner_script() -> Any:
    from millforge.eval_trials import (
        EvalFakeOutcomeScriptKind,
        EvalTrialFakeRunnerScript,
    )
    from millforge.eval_workflow import EvalStageId, EvalTerminalResult

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
            EvalStageId.PLANNER: "plan ready",
            EvalStageId.BUILDER: "builder complete",
            EvalStageId.CHECKER: "checker approved",
            EvalStageId.ARBITER: "arbiter closed",
        },
    )


def _offline_dry_store_manifest(plan: Any) -> Any:
    from millforge.eval_trials import (
        EVAL_TRIAL_SCHEMA_VERSION,
        EVAL_TRIAL_STORE_MANIFEST_HASH_KIND,
        EvalTrialStoreManifest,
        calculate_eval_trial_store_manifest_hash,
    )

    manifest = EvalTrialStoreManifest.model_construct(
        schema_version=EVAL_TRIAL_SCHEMA_VERSION,
        store_manifest_id=f"{plan.campaign_manifest.campaign_id}.store.v1",
        campaign_manifest_hash=plan.campaign_manifest.campaign_manifest_hash,
        record_hashes=(),
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


def _offline_dry_diagnostic(
    code: EvalOfflineDryCampaignDiagnosticCode,
    rule_id: str,
    summary: str,
) -> EvalOfflineDryCampaignDiagnostic:
    return EvalOfflineDryCampaignDiagnostic(
        diagnostic_code=code,
        rule_id=rule_id,
        summary=summary,
    )


def _validate_public_command(command: str) -> None:
    stripped = command.strip()
    if not stripped:
        raise ValueError("commands must be non-empty")
    try:
        parts = shlex.split(stripped)
    except ValueError as exc:
        raise ValueError("commands must be shell-parseable") from exc
    if not parts:
        raise ValueError("commands must be non-empty")
    for token in _iter_public_command_tokens(parts):
        token_root = token.split(".", 1)[0]
        if token_root in _NETWORK_COMMANDS:
            raise ValueError("eval-suite checks must not require network commands")
        if token_root in _PACKAGE_COMMANDS:
            raise ValueError("eval-suite checks must not require package installation")
        if token in _NONDETERMINISTIC_COMMAND_TOKENS:
            raise ValueError("eval-suite checks must be deterministic")
    if any(pattern.search(stripped) for pattern in _NONDETERMINISTIC_PYTHON_PRIMITIVES):
        raise ValueError("eval-suite checks must be deterministic")
    _reject_forbidden_material(command)


def _iter_public_command_tokens(parts: list[str]) -> tuple[str, ...]:
    tokens: list[str] = []
    for part in parts:
        for word in part.split():
            normalized = word.split("/")[-1].strip(" \t\r\n\"'`()[]{};,")
            if normalized.endswith(".exe"):
                normalized = normalized[:-4]
            if normalized:
                tokens.append(normalized.lower())
    return tuple(tokens)


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
    if isinstance(value, Enum):
        _reject_forbidden_material(value.value)
        return
    if isinstance(value, str):
        lowered = value.lower()
        if any(token in lowered for token in _DENIED_TEXT_TOKENS):
            raise ValueError("eval-suite payload contains forbidden private material")
        if any(pattern.search(value) for pattern in _CREDENTIAL_VALUE_PATTERNS):
            raise ValueError("eval-suite payload contains credential-shaped API key")
        if _ENDPOINT_URL.search(value):
            raise ValueError("eval-suite payloads must not contain endpoint URLs")
        if (
            _WINDOWS_ABSOLUTE_PATH.search(value)
            or _POSIX_ABSOLUTE_PATH.search(value)
            or _USER_HOME_PATH.search(value)
        ):
            raise ValueError("eval-suite payloads must not contain host paths")


def _reject_secret_like_field_name(field_name: str) -> None:
    normalized = field_name.lower().replace("-", "_")
    if any(marker in normalized for marker in _SECRET_FIELD_MARKERS):
        raise ValueError("eval-suite payload contains secret-like field name")


def _freeze_eval_suite_mapping(value: Mapping[Any, Any]) -> Mapping[Any, Any]:
    return _FrozenEvalSuiteDict(
        {key: _freeze_eval_suite_value(child) for key, child in value.items()}
    )


def _freeze_eval_suite_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_eval_suite_mapping(value)
    if isinstance(value, tuple):
        return tuple(_freeze_eval_suite_value(child) for child in value)
    if isinstance(value, list):
        return tuple(_freeze_eval_suite_value(child) for child in value)
    return value


def _load_default_eval_fixture_pack_manifest() -> Mapping[str, Any]:
    manifest_resource = files(_EVAL_FIXTURE_PACK_PACKAGE).joinpath(
        _EVAL_FIXTURE_PACK_MANIFEST
    )
    manifest = json.loads(manifest_resource.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise ValueError("fixture pack manifest must be a JSON object")
    if manifest.get("fixture_pack_id") != EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID:
        raise ValueError("fixture pack manifest has unexpected fixture_pack_id")
    fixture_ids = manifest.get("fixture_ids")
    if not isinstance(fixture_ids, list) or not fixture_ids:
        raise ValueError("fixture pack manifest must list fixture_ids")
    if not all(isinstance(fixture_id, str) for fixture_id in fixture_ids):
        raise ValueError("fixture pack manifest fixture_ids must be strings")
    return manifest


def _load_eval_task_fixture_resource(resource: Any) -> EvalTaskFixture:
    payload = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("fixture resource must be a JSON object")
    payload["fixture_hash"] = _calculate_eval_suite_payload_hash(
        payload,
        hash_field="fixture_hash",
    )
    return EvalTaskFixture.model_validate(payload)


def _calculate_eval_suite_payload_hash(
    payload: Mapping[str, Any],
    *,
    hash_field: str,
) -> str:
    hash_payload = dict(payload)
    hash_payload.pop(hash_field, None)
    return hashlib.sha256(canonical_eval_suite_bytes(hash_payload)).hexdigest()


__all__ = [
    "EVAL_SUITE_CAMPAIGN_MANIFEST_HASH_KIND",
    "EVAL_SUITE_CLOSURE_EVIDENCE_HASH_KIND",
    "EVAL_SUITE_DEFAULT_CAMPAIGN_CREATED_AT",
    "EVAL_SUITE_DEFAULT_CAMPAIGN_ID",
    "EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID",
    "EVAL_SUITE_DEFAULT_SCORER_VERSION",
    "EVAL_SUITE_FIXTURE_HASH_KIND",
    "EVAL_SUITE_FIXTURE_PACK_HASH_KIND",
    "EVAL_SUITE_MODEL_MANIFEST_HASH_KIND",
    "EVAL_SUITE_OUTPUT_ROOT_HASH_KIND",
    "EVAL_SUITE_SCHEMA_VERSION",
    "EVAL_SUITE_SCORER_INPUT_HASH_KIND",
    "EVAL_SUITE_SCORER_RESULT_HASH_KIND",
    "EvalBudgetPolicyReference",
    "EvalCampaignKind",
    "EvalCampaignManifest",
    "EvalCapabilityAuditSummary",
    "EvalCheckResult",
    "EvalDifficultyLevel",
    "EvalDifficultyMetadata",
    "EvalExpectedMutationKind",
    "EvalExpectedMutationPolicy",
    "EvalFailureTaxonomyLabel",
    "EvalFixturePackSummary",
    "EvalHashRecord",
    "EvalHiddenCheck",
    "EvalLiveDenialDiagnostic",
    "EvalModelPricingMetadata",
    "EvalModelRateLimitMetadata",
    "EvalModelManifest",
    "EvalOfflineDryCampaignConfig",
    "EvalOfflineDryCampaignClosureEvidence",
    "EvalOfflineDryCampaignDiagnostic",
    "EvalOfflineDryCampaignDiagnosticCode",
    "EvalOfflineDryCampaignPlan",
    "EvalOfflineDryCampaignRunResult",
    "EvalPublicArtifactProjection",
    "EvalRunnerAcceptanceProjection",
    "EvalRunnerContextProjection",
    "EvalRunnerTaskProjection",
    "EvalScorerInput",
    "EvalScorerResult",
    "EvalSuiteContractModel",
    "EvalSuiteExecutionMode",
    "EvalTaskCategory",
    "EvalTaskFixture",
    "EvalTrialOutcome",
    "EvalVisibleCheck",
    "calculate_eval_campaign_manifest_hash",
    "calculate_eval_fixture_pack_hash",
    "calculate_eval_model_manifest_hash",
    "calculate_offline_dry_campaign_closure_evidence_hash",
    "calculate_eval_scorer_input_hash",
    "calculate_eval_scorer_result_hash",
    "calculate_eval_task_fixture_hash",
    "canonical_eval_suite_bytes",
    "canonical_offline_dry_campaign_closure_evidence_bytes",
    "configure_offline_fake_eval_campaign",
    "default_eval_suite_campaign_manifest",
    "build_offline_dry_campaign_closure_evidence",
    "eval_public_artifact_projection",
    "eval_model_manifest_from_profile",
    "eval_runner_acceptance_projection",
    "eval_runner_context_projection",
    "eval_runner_task_projection",
    "load_eval_fixture_pack_summary",
    "load_eval_task_fixture",
    "load_eval_task_fixtures",
    "run_offline_fake_eval_campaign",
    "score_eval_trial",
]
