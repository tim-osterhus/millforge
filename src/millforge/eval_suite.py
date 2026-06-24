"""Public 08A offline eval-suite contract boundary."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from importlib.resources import files
from collections.abc import Mapping
from enum import Enum
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


def _validate_public_artifact_ids(values: tuple[str, ...]) -> None:
    for value in values:
        if not value or "/" in value or "\\" in value or value.startswith("."):
            raise ValueError("public artifact IDs must be stable bare identifiers")
        _reject_forbidden_material(value)


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
    "EVAL_SUITE_DEFAULT_CAMPAIGN_CREATED_AT",
    "EVAL_SUITE_DEFAULT_CAMPAIGN_ID",
    "EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID",
    "EVAL_SUITE_DEFAULT_SCORER_VERSION",
    "EVAL_SUITE_FIXTURE_HASH_KIND",
    "EVAL_SUITE_FIXTURE_PACK_HASH_KIND",
    "EVAL_SUITE_MODEL_MANIFEST_HASH_KIND",
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
    "calculate_eval_scorer_input_hash",
    "calculate_eval_scorer_result_hash",
    "calculate_eval_task_fixture_hash",
    "canonical_eval_suite_bytes",
    "default_eval_suite_campaign_manifest",
    "eval_public_artifact_projection",
    "eval_model_manifest_from_profile",
    "eval_runner_acceptance_projection",
    "eval_runner_context_projection",
    "eval_runner_task_projection",
    "load_eval_fixture_pack_summary",
    "load_eval_task_fixture",
    "load_eval_task_fixtures",
    "score_eval_trial",
]
