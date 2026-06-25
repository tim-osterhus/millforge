"""Public 08C eval-report, budget, and live-admission contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from enum import Enum
from statistics import median
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt
from pydantic import StrictStr, field_validator, model_validator

from millforge.eval_suite import (
    EvalCampaignManifest,
    EvalFailureTaxonomyLabel,
    EvalSuiteExecutionMode,
    EvalTaskCategory,
    EvalTrialOutcome,
)
from millforge.eval_trials import (
    EvalTrialArmId,
    EvalTrialPlan,
    EvalTrialRecord,
    EvalTrialResumeIndex,
)

EVAL_REPORT_SCHEMA_VERSION = 1
EVAL_REPORT_HASH_KIND = "eval_report_sha256_v1"
EVAL_REPORT_JSON_HASH_KIND = "eval_report_json_sha256_v1"
EVAL_REPORT_MARKDOWN_HASH_KIND = "eval_report_markdown_sha256_v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ENDPOINT_URL = re.compile(r"https?://|localhost(?::|/|$)|127\.0\.0\.1|0\.0\.0\.0")
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'`([{<])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`)\]}>,]*"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'`([{<])/(?!/)[^\s\"'`)\]}>,]*")
_USER_HOME_PATH = re.compile(r"(?:^|[\s\"'`([{<])~(?:/|\\)[^\s\"'`)\]}>,]*")
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
    "endpoint_url",
    "endpoint url",
    "millrace-agents",
    ".millrace",
    "daemon state",
    "private workspace",
    "private runtime",
    "hidden scorer",
    "hidden answer",
    "hidden expected",
    "expected output",
    "scorer_rubric",
    ".claude",
    ".codex",
)


class _FrozenEvalReportDict(dict[Any, Any]):
    """Dict-shaped immutable mapping that remains serializable by Pydantic."""

    def __readonly(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("eval-report mappings are immutable")

    __setitem__ = __readonly
    __delitem__ = __readonly
    clear = __readonly
    pop = __readonly
    popitem = __readonly  # type: ignore[assignment]
    setdefault = __readonly
    update = __readonly
    __ior__ = __readonly  # type: ignore[assignment]


class EvalReportContractModel(BaseModel):
    """Closed, frozen base for public eval-report contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_payload(cls, data: Any) -> Any:
        _reject_forbidden_material(data)
        return data


class EvalReportPricingClass(str, Enum):
    """Closed pricing classes for campaign budget accounting."""

    OFFLINE_ZERO_COST = "offline_zero_cost"
    FREE_TIER = "free_tier"
    PROMOTIONAL_FREE_WINDOW = "promotional_free_window"
    PAID_PROVIDER = "paid_provider"
    LOCAL_METERED = "local_metered"


class EvalLiveAdmissionStatus(str, Enum):
    """Live campaign admission states."""

    ADMITTED = "admitted"
    DENIED = "denied"


class EvalLiveAdmissionDiagnosticCode(str, Enum):
    """Structured fail-closed live-admission diagnostic codes."""

    PI_RUNTIME_UNAVAILABLE = "pi_runtime_unavailable"
    MILLFORGE_LIVE_HARNESS_UNAVAILABLE = "millforge_live_harness_unavailable"
    SHARED_BACKEND_CONFIGURATION_MISSING = "shared_backend_configuration_missing"
    FIXTURE_WORKSPACE_LIFECYCLE_UNAVAILABLE = "fixture_workspace_lifecycle_unavailable"
    RESOURCE_ENFORCEMENT_UNAVAILABLE = "resource_enforcement_unavailable"
    BUDGET_POLICY_INVALID = "budget_policy_invalid"
    APPEND_ONLY_STORE_SAFETY_UNPROVEN = "append_only_store_safety_unproven"
    DETERMINISTIC_SCORER_UNAVAILABLE = "deterministic_scorer_unavailable"


class EvalBudgetDiagnosticCode(str, Enum):
    """Structured budget validation diagnostic codes."""

    MISSING_BUDGET_POLICY = "missing_budget_policy"
    MISSING_LIVE_BUDGET_METADATA = "missing_live_budget_metadata"
    MISSING_TOKEN_CEILING = "missing_token_ceiling"
    MISSING_TRIAL_COUNT_CEILING = "missing_trial_count_ceiling"
    INCOMPLETE_PROMOTIONAL_FREE_WINDOW = "incomplete_promotional_free_window"
    UNFAIR_PAIRED_ARM_RATE_LIMIT = "unfair_paired_arm_rate_limit"
    OFFLINE_POLICY_NOT_ZERO_COST = "offline_policy_not_zero_cost"
    OFFLINE_POLICY_UNBOUNDED = "offline_policy_unbounded"
    SPEND_CEILING_EXCEEDED = "spend_ceiling_exceeded"
    PROMPT_TOKEN_CEILING_EXCEEDED = "prompt_token_ceiling_exceeded"
    COMPLETION_TOKEN_CEILING_EXCEEDED = "completion_token_ceiling_exceeded"
    MODEL_CALL_CEILING_EXCEEDED = "model_call_ceiling_exceeded"
    RETRY_CEILING_EXCEEDED = "retry_ceiling_exceeded"
    WALL_CLOCK_CEILING_EXCEEDED = "wall_clock_ceiling_exceeded"
    TRIAL_COUNT_CEILING_EXCEEDED = "trial_count_ceiling_exceeded"


class EvalMetricDenominatorKind(str, Enum):
    """Metric denominator sources pinned in reports."""

    PLANNED_TRIALS = "planned_trials"
    APPENDED_RECORDS = "appended_records"
    VALID_RECORDS = "valid_records"
    PAIRED_RECORDS = "paired_records"


class EvalReportMetricId(str, Enum):
    """Closed primary and secondary metric IDs."""

    VALID_COMPLETION = "valid_completion"
    FALSE_CLOSURE = "false_closure"
    FALSE_SUCCESS = "false_success"
    ARTIFACT_COMPLETE = "artifact_complete"
    CAPABILITY_VIOLATION = "capability_violation"
    CORRECTLY_BLOCKED = "correctly_blocked"
    FALSE_BLOCKED = "false_blocked"
    RUNTIME_FAILURE = "runtime_failure"
    PROVIDER_FAILURE = "provider_failure"
    INVALID_TRIAL = "invalid_trial"
    MISSING_PAIR = "missing_pair"
    PENDING_TRIAL = "pending_trial"
    INCOMPLETE_TRIAL = "incomplete_trial"
    MODEL_CALLS = "model_calls"
    PROMPT_TOKENS = "prompt_tokens"
    COMPLETION_TOKENS = "completion_tokens"
    ESTIMATED_COST = "estimated_cost"
    WALL_CLOCK_SECONDS = "wall_clock_seconds"
    RETRIES = "retries"
    ARTIFACT_COUNT = "artifact_count"
    ARTIFACT_BYTES = "artifact_bytes"
    TURNS = "turns"
    INVALID_TOOL_CALLS = "invalid_tool_calls"
    MALFORMED_TOOL_CALLS = "malformed_tool_calls"
    MALFORMED_ARGUMENTS = "malformed_arguments"
    PREREQUISITE_VIOLATIONS = "prerequisite_violations"
    PREMATURE_TERMINALS = "premature_terminals"
    TOOL_RECOVERIES = "tool_recoveries"
    COMPLETION_IMPROVEMENT = "completion_improvement"
    COST_MULTIPLIER = "cost_multiplier"
    LATENCY_MULTIPLIER = "latency_multiplier"


class EvalReportFailureTaxonomyCategory(str, Enum):
    """Closed public report failure taxonomy."""

    TASK_MISUNDERSTANDING = "task_misunderstanding"
    WRONG_FILE = "wrong_file"
    UNREAD_BEFORE_EDIT = "unread_before_edit"
    INVALID_PATCH = "invalid_patch"
    TEST_NOT_RUN = "test_not_run"
    TEST_MISREAD = "test_misread"
    MISSING_ARTIFACT = "missing_artifact"
    UNSUPPORTED_SUCCESS_CLAIM = "unsupported_success_claim"
    CHECKER_EVIDENCE_FAILURE = "checker_evidence_failure"
    ARBITER_FALSE_CLOSURE = "arbiter_false_closure"
    PREMATURE_TERMINAL = "premature_terminal"
    TOOL_SCHEMA_FAILURE = "tool_schema_failure"
    TOOL_RECOVERY_FAILURE = "tool_recovery_failure"
    CONTEXT_LOSS = "context_loss"
    BUDGET_EXHAUSTION = "budget_exhaustion"
    PROVIDER_FAILURE = "provider_failure"
    RUNNER_FAILURE = "runner_failure"
    CAPABILITY_VIOLATION = "capability_violation"
    INVALID_TRIAL_INFRASTRUCTURE = "invalid_trial_infrastructure"


class EvalReportConfoundId(str, Enum):
    """Closed confounds that must remain visible in pilot reports."""

    PI_PROMPT_TOOL_BEHAVIOR = "pi_prompt_tool_behavior"
    MILLFORGE_PROMPT_TOOL_BEHAVIOR = "millforge_prompt_tool_behavior"
    HARNESS_BEHAVIOR = "harness_behavior"
    CONTEXT_PACKING = "context_packing"
    PARSER_FALLBACK = "parser_fallback"
    PROVIDER_NONDETERMINISM = "provider_nondeterminism"
    RATE_LIMITING = "rate_limiting"
    CACHED_PROVIDER_RESPONSES = "cached_provider_responses"
    TOKEN_ACCOUNTING_DIFFERENCES = "token_accounting_differences"
    SAMPLING_PARAMETER_MISMATCH = "sampling_parameter_mismatch"
    OFFLINE_FAKE_LIMITATIONS = "offline_fake_limitations"


class EvalDecisionRuleStatus(str, Enum):
    """Decision-rule evaluation states."""

    PASSED = "passed"
    FAILED = "failed"
    DESCRIPTIVE_ONLY = "descriptive_only"
    NOT_APPLICABLE = "not_applicable"


class EvalDecisionRuleKind(str, Enum):
    """Closed pre-registered decision-rule contract classes."""

    MAX_FALSE_CLOSURE_RATE = "max_false_closure_rate"
    MIN_COMPLETION_IMPROVEMENT = "min_completion_improvement"
    MAX_COST_MULTIPLIER = "max_cost_multiplier"
    MAX_LATENCY_MULTIPLIER = "max_latency_multiplier"
    ACCEPTABLE_FALSE_BLOCKED_TRADEOFF = "acceptable_false_blocked_tradeoff"
    SEVERITY_ONE_ABORT_THRESHOLD = "severity_one_abort_threshold"


class EvalBudgetDiagnostic(EvalReportContractModel):
    """One fail-closed budget diagnostic."""

    diagnostic_code: EvalBudgetDiagnosticCode
    rule_id: StrictStr
    summary: StrictStr


class EvalLiveAdmissionDiagnostic(EvalReportContractModel):
    """One structured live-admission diagnostic."""

    diagnostic_code: EvalLiveAdmissionDiagnosticCode
    rule_id: StrictStr
    summary: StrictStr


class EvalPromotionalFreeWindow(EvalReportContractModel):
    """Complete metadata required for promotional free execution."""

    window_id: StrictStr
    source_label: StrictStr
    starts_at: StrictStr
    ends_at: StrictStr
    max_free_usd: StrictFloat = Field(ge=0.0)
    terms_summary: StrictStr

    @model_validator(mode="after")
    def _window_valid(self) -> EvalPromotionalFreeWindow:
        if not _UTC_TIMESTAMP_RE.fullmatch(self.starts_at):
            raise ValueError("promotional window starts_at must be a UTC timestamp")
        if not _UTC_TIMESTAMP_RE.fullmatch(self.ends_at):
            raise ValueError("promotional window ends_at must be a UTC timestamp")
        if self.ends_at <= self.starts_at:
            raise ValueError("promotional window ends_at must follow starts_at")
        return self


class EvalReportRateLimitPolicy(EvalReportContractModel):
    """Public paired-arm rate-limit and retry/backoff budget policy."""

    request_rate_per_window: StrictInt = Field(gt=0)
    token_rate_per_window: StrictInt = Field(gt=0)
    concurrent_request_limit: StrictInt = Field(gt=0)
    window_seconds: StrictInt = Field(gt=0)
    max_backoff_seconds: StrictInt = Field(ge=0)
    max_retries_per_trial: StrictInt = Field(ge=0)


class EvalReportAbortThresholds(EvalReportContractModel):
    """Severity-one abort thresholds for a campaign."""

    max_false_closure_rate: StrictFloat = Field(ge=0.0, le=1.0)
    max_capability_violation_rate: StrictFloat = Field(ge=0.0, le=1.0)
    max_invalid_trial_rate: StrictFloat = Field(ge=0.0, le=1.0)


class EvalReportBudgetPolicy(EvalReportContractModel):
    """Bounded campaign budget policy used by admission and reports."""

    policy_id: StrictStr
    pricing_class: EvalReportPricingClass
    max_spend_usd: StrictFloat | None = Field(default=None, ge=0.0)
    max_prompt_tokens: StrictInt | None = Field(default=None, ge=0)
    max_completion_tokens: StrictInt | None = Field(default=None, ge=0)
    max_model_calls: StrictInt | None = Field(default=None, ge=0)
    max_retries_per_trial: StrictInt | None = Field(default=None, ge=0)
    max_wall_clock_seconds: StrictInt | None = Field(default=None, ge=0)
    max_trials_per_campaign: StrictInt | None = Field(default=None, ge=0)
    promotional_free_window: EvalPromotionalFreeWindow | None = None
    rate_limit_policy_by_arm: Mapping[EvalTrialArmId, EvalReportRateLimitPolicy] = (
        Field(default_factory=dict)
    )
    abort_thresholds: EvalReportAbortThresholds
    summary: StrictStr

    @model_validator(mode="after")
    def _policy_valid(self) -> EvalReportBudgetPolicy:
        object.__setattr__(
            self,
            "rate_limit_policy_by_arm",
            _freeze_eval_report_mapping(self.rate_limit_policy_by_arm),
        )
        return self


class EvalBudgetUsageEstimate(EvalReportContractModel):
    """Deterministic campaign budget consumption estimate."""

    estimated_spend_usd: StrictFloat = Field(ge=0.0)
    prompt_tokens: StrictInt = Field(ge=0)
    completion_tokens: StrictInt = Field(ge=0)
    model_calls: StrictInt = Field(ge=0)
    retries_per_trial: StrictInt = Field(ge=0)
    wall_clock_seconds: StrictInt = Field(ge=0)
    trial_count: StrictInt = Field(ge=0)


class EvalBudgetValidationResult(EvalReportContractModel):
    """Fail-closed result for budget policy validation."""

    valid: StrictBool
    diagnostics: tuple[EvalBudgetDiagnostic, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _result_valid(self) -> EvalBudgetValidationResult:
        if self.valid and self.diagnostics:
            raise ValueError("valid budget results must not include diagnostics")
        if not self.valid and not self.diagnostics:
            raise ValueError("invalid budget results require diagnostics")
        return self


class EvalLiveAdmissionResult(EvalReportContractModel):
    """Structured live or offline campaign admission result."""

    status: EvalLiveAdmissionStatus
    diagnostics: tuple[EvalLiveAdmissionDiagnostic, ...] = Field(default_factory=tuple)
    budget_result: EvalBudgetValidationResult

    @model_validator(mode="after")
    def _admission_valid(self) -> EvalLiveAdmissionResult:
        if self.status is EvalLiveAdmissionStatus.ADMITTED and self.diagnostics:
            raise ValueError("admitted campaigns must not include denial diagnostics")
        if self.status is EvalLiveAdmissionStatus.DENIED and not self.diagnostics:
            raise ValueError("denied campaigns require diagnostics")
        return self


class EvalMetricDenominator(EvalReportContractModel):
    """Explicit metric denominator evidence."""

    denominator_kind: EvalMetricDenominatorKind
    count: StrictInt = Field(ge=0)
    summary: StrictStr


class EvalMetricValue(EvalReportContractModel):
    """One metric count/rate, with an optional total value, and denominator."""

    metric_id: EvalReportMetricId
    count: StrictInt = Field(ge=0)
    denominator: EvalMetricDenominator
    rate: StrictFloat = Field(ge=0.0, le=1.0)
    value: StrictFloat | None = Field(default=None, ge=0.0)
    severity_one: StrictBool = False

    @model_validator(mode="after")
    def _metric_valid(self) -> EvalMetricValue:
        expected = (
            0.0 if self.denominator.count == 0 else self.count / self.denominator.count
        )
        if abs(self.rate - expected) > 0.000000001:
            raise ValueError("metric rate must match count and denominator")
        if self.count > self.denominator.count:
            raise ValueError("metric count must not exceed denominator")
        return self


class EvalPairedComparison(EvalReportContractModel):
    """Per-metric paired comparison across the two admitted arms."""

    metric_id: EvalReportMetricId
    left_arm_id: EvalTrialArmId
    right_arm_id: EvalTrialArmId
    left_count: StrictInt = Field(ge=0)
    right_count: StrictInt = Field(ge=0)
    paired_denominator: StrictInt = Field(ge=0)
    difference: StrictInt
    missing_pair_count: StrictInt = Field(ge=0)


class EvalWilsonScoreInterval(EvalReportContractModel):
    """Wilson score interval emitted only when sample-size rules allow it."""

    successes: StrictInt = Field(ge=0)
    total: StrictInt = Field(gt=0)
    confidence_level: StrictFloat = Field(gt=0.0, lt=1.0)
    lower: StrictFloat = Field(ge=0.0, le=1.0)
    upper: StrictFloat = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _interval_valid(self) -> EvalWilsonScoreInterval:
        if self.successes > self.total:
            raise ValueError("Wilson successes must not exceed total")
        if self.lower > self.upper:
            raise ValueError("Wilson interval lower must not exceed upper")
        return self


class EvalDistributionSummary(EvalReportContractModel):
    """Descriptive distribution summary for cost and latency-style values."""

    statistic_id: StrictStr
    sample_count: StrictInt = Field(ge=0)
    raw_values: tuple[StrictFloat, ...] = Field(default_factory=tuple)
    median: StrictFloat | None = None
    p90: StrictFloat | None = None
    p95: StrictFloat | None = None
    descriptive_only: StrictBool = True

    @model_validator(mode="after")
    def _distribution_valid(self) -> EvalDistributionSummary:
        if self.sample_count != len(self.raw_values):
            raise ValueError("distribution sample_count must match raw_values")
        if self.sample_count == 0 and any(
            value is not None for value in (self.median, self.p90, self.p95)
        ):
            raise ValueError("empty distributions must not include percentiles")
        return self


class EvalReportStatisticalSummary(EvalReportContractModel):
    """Small-N descriptive statistics and eligibility diagnostics."""

    metric_id: EvalReportMetricId
    raw_count: StrictInt = Field(ge=0)
    denominator_count: StrictInt = Field(ge=0)
    rate: StrictFloat = Field(ge=0.0, le=1.0)
    wilson_interval: EvalWilsonScoreInterval | None = None
    paired_differences: tuple[StrictInt, ...] = Field(default_factory=tuple)
    distributions: tuple[EvalDistributionSummary, ...] = Field(default_factory=tuple)
    diagnostic: StrictStr
    descriptive_only: StrictBool = True

    @model_validator(mode="after")
    def _statistical_summary_valid(self) -> EvalReportStatisticalSummary:
        expected = (
            0.0
            if self.denominator_count == 0
            else self.raw_count / self.denominator_count
        )
        if abs(self.rate - expected) > 0.000000001:
            raise ValueError("statistical summary rate must match raw counts")
        if self.wilson_interval is not None and self.descriptive_only:
            raise ValueError("Wilson-eligible summaries are not descriptive-only")
        if not self.diagnostic.strip():
            raise ValueError("statistical summaries require diagnostics")
        return self


class EvalTaskSummary(EvalReportContractModel):
    """Per-task report summary."""

    fixture_id: StrictStr
    trial_index: StrictInt = Field(ge=0)
    category: StrictStr
    metrics: tuple[EvalMetricValue, ...]


class EvalArmSummary(EvalReportContractModel):
    """Per-arm report summary with explicit arm-local denominators."""

    arm_id: EvalTrialArmId
    metrics: tuple[EvalMetricValue, ...]


class EvalCategorySummary(EvalReportContractModel):
    """Per-category aggregate report summary."""

    category: EvalTaskCategory | StrictStr
    metrics: tuple[EvalMetricValue, ...]


class EvalFailureTaxonomyAssignment(EvalReportContractModel):
    """Optional manual taxonomy assignment that cannot affect scorer success."""

    primary_category: EvalReportFailureTaxonomyCategory
    contributing_categories: tuple[EvalReportFailureTaxonomyCategory, ...] = Field(
        default_factory=tuple
    )
    explanation: StrictStr
    category_explanations: Mapping[EvalReportFailureTaxonomyCategory, StrictStr] = (
        Field(default_factory=dict)
    )

    @field_validator("explanation")
    @classmethod
    def _explanation_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("manual taxonomy assignments require an explanation")
        return value

    @model_validator(mode="after")
    def _manual_assignment_valid(self) -> EvalFailureTaxonomyAssignment:
        categories = (self.primary_category, *self.contributing_categories)
        if len(set(categories)) != len(categories):
            raise ValueError("manual taxonomy categories must be unique")
        explanations = dict(self.category_explanations)
        if not explanations:
            explanations = {category: self.explanation for category in categories}
        missing = [category for category in categories if category not in explanations]
        blank = [
            category
            for category, category_explanation in explanations.items()
            if not category_explanation.strip()
        ]
        if missing or blank:
            raise ValueError("each manual taxonomy category requires an explanation")
        object.__setattr__(
            self,
            "category_explanations",
            _freeze_eval_report_mapping(explanations),
        )
        return self


class EvalFailureTaxonomySummary(EvalReportContractModel):
    """Closed taxonomy rollup for report data."""

    category: EvalReportFailureTaxonomyCategory
    count: StrictInt = Field(ge=0)
    examples: tuple[StrictStr, ...] = Field(default_factory=tuple)


class EvalInvalidTrialSummary(EvalReportContractModel):
    """Top-line invalid-trial visibility."""

    invalid_trial_count: StrictInt = Field(ge=0)
    appended_record_count: StrictInt = Field(ge=0)
    invalid_trial_rate: StrictFloat = Field(ge=0.0, le=1.0)
    diagnostics: tuple[StrictStr, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _invalid_valid(self) -> EvalInvalidTrialSummary:
        expected = (
            0.0
            if self.appended_record_count == 0
            else self.invalid_trial_count / self.appended_record_count
        )
        if abs(self.invalid_trial_rate - expected) > 0.000000001:
            raise ValueError("invalid trial rate must match counts")
        return self


class EvalConfoundEntry(EvalReportContractModel):
    """Visible confound entry in JSON and Markdown reports."""

    confound_id: EvalReportConfoundId
    summary: StrictStr
    affects_claims: StrictBool = True


class EvalDecisionRule(EvalReportContractModel):
    """Pre-registered report decision rule."""

    rule_id: StrictStr
    rule_kind: EvalDecisionRuleKind
    summary: StrictStr
    metric_id: EvalReportMetricId
    threshold: StrictFloat = Field(ge=0.0)
    status: EvalDecisionRuleStatus
    observed_value: StrictFloat | None = Field(default=None, ge=0.0)
    diagnostic: StrictStr | None = None

    @model_validator(mode="after")
    def _rule_valid(self) -> EvalDecisionRule:
        if (
            self.status is EvalDecisionRuleStatus.DESCRIPTIVE_ONLY
            and not self.diagnostic
        ):
            raise ValueError("descriptive-only decision rules require diagnostics")
        return self


class EvalReportReproducibilityHashes(EvalReportContractModel):
    """Hash references that make a report reproducible."""

    campaign_manifest_hash: StrictStr
    plan_hashes: tuple[StrictStr, ...]
    resume_index_hash: StrictStr | None = None
    record_hashes: tuple[StrictStr, ...] = Field(default_factory=tuple)
    report_input_hash: StrictStr

    @model_validator(mode="after")
    def _hashes_valid(self) -> EvalReportReproducibilityHashes:
        for digest in (
            (self.campaign_manifest_hash, self.report_input_hash)
            + self.plan_hashes
            + self.record_hashes
        ):
            _validate_sha256(digest)
        if self.resume_index_hash is not None:
            _validate_sha256(self.resume_index_hash)
        return self


class EvalReportPayload(EvalReportContractModel):
    """Deterministic JSON report payload."""

    schema_version: StrictInt = EVAL_REPORT_SCHEMA_VERSION
    report_id: StrictStr
    campaign_id: StrictStr
    generated_at: StrictStr
    admission: EvalLiveAdmissionResult
    arms: tuple[EvalTrialArmId, EvalTrialArmId]
    controlled_variables: Mapping[StrictStr, StrictStr] = Field(default_factory=dict)
    budget_policy: EvalReportBudgetPolicy
    budget_usage: EvalBudgetUsageEstimate
    primary_metrics: tuple[EvalMetricValue, ...]
    arm_summaries: tuple[EvalArmSummary, ...] = Field(default_factory=tuple)
    paired_comparisons: tuple[EvalPairedComparison, ...] = Field(default_factory=tuple)
    task_summaries: tuple[EvalTaskSummary, ...] = Field(default_factory=tuple)
    category_summaries: tuple[EvalCategorySummary, ...] = Field(default_factory=tuple)
    taxonomy_summaries: tuple[EvalFailureTaxonomySummary, ...] = Field(
        default_factory=tuple
    )
    invalid_trials: EvalInvalidTrialSummary
    statistical_summaries: tuple[EvalReportStatisticalSummary, ...] = Field(
        default_factory=tuple
    )
    confounds: tuple[EvalConfoundEntry, ...]
    decision_rules: tuple[EvalDecisionRule, ...]
    reproducibility_hashes: EvalReportReproducibilityHashes
    claim_boundaries: tuple[StrictStr, ...]
    report_hash_kind: StrictStr = EVAL_REPORT_HASH_KIND
    report_hash: StrictStr

    @model_validator(mode="after")
    def _payload_valid(self) -> EvalReportPayload:
        if self.schema_version != EVAL_REPORT_SCHEMA_VERSION:
            raise ValueError("unsupported eval-report schema_version")
        if not _UTC_TIMESTAMP_RE.fullmatch(self.generated_at):
            raise ValueError("report generated_at must be a UTC timestamp")
        if len(set(self.arms)) != 2:
            raise ValueError("reports require two distinct arms")
        object.__setattr__(
            self,
            "controlled_variables",
            _freeze_eval_report_mapping(self.controlled_variables),
        )
        if not self.claim_boundaries:
            raise ValueError("reports require explicit claim boundaries")
        if not self.confounds:
            raise ValueError("reports require explicit confounds")
        if self.report_hash_kind != EVAL_REPORT_HASH_KIND:
            raise ValueError("unsupported report hash kind")
        _validate_sha256(self.report_hash)
        expected = calculate_eval_report_hash(self)
        if self.report_hash != expected:
            raise ValueError("report_hash does not match payload")
        return self


class EvalMarkdownReport(EvalReportContractModel):
    """Deterministic Markdown report content."""

    schema_version: StrictInt = EVAL_REPORT_SCHEMA_VERSION
    report_id: StrictStr
    content: StrictStr
    content_hash_kind: StrictStr = EVAL_REPORT_MARKDOWN_HASH_KIND
    content_hash: StrictStr

    @model_validator(mode="after")
    def _markdown_valid(self) -> EvalMarkdownReport:
        if self.schema_version != EVAL_REPORT_SCHEMA_VERSION:
            raise ValueError("unsupported markdown report schema_version")
        if self.content_hash_kind != EVAL_REPORT_MARKDOWN_HASH_KIND:
            raise ValueError("unsupported markdown report hash kind")
        _validate_sha256(self.content_hash)
        if (
            self.content_hash
            != hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        ):
            raise ValueError("markdown content hash does not match content")
        return self


def validate_eval_budget_policy(
    policy: EvalReportBudgetPolicy | Mapping[str, Any] | None,
    *,
    campaign_manifest: EvalCampaignManifest,
    usage: EvalBudgetUsageEstimate | Mapping[str, Any] | None = None,
) -> EvalBudgetValidationResult:
    """Validate a campaign budget policy and fail closed with diagnostics."""
    diagnostics: list[EvalBudgetDiagnostic] = []
    if policy is None:
        return EvalBudgetValidationResult(
            valid=False,
            diagnostics=(
                _budget_diagnostic(
                    EvalBudgetDiagnosticCode.MISSING_BUDGET_POLICY,
                    "eval_reports.budget.required",
                    "Budget policy metadata is required for admission.",
                ),
            ),
        )
    try:
        valid_policy = EvalReportBudgetPolicy.model_validate(policy)
    except ValueError as exc:
        return EvalBudgetValidationResult(
            valid=False,
            diagnostics=(
                _budget_diagnostic(
                    EvalBudgetDiagnosticCode.MISSING_LIVE_BUDGET_METADATA,
                    "eval_reports.budget.model_validate",
                    f"Budget policy metadata is invalid: {exc}",
                ),
            ),
        )
    usage_estimate = (
        EvalBudgetUsageEstimate.model_validate(usage)
        if usage is not None
        else EvalBudgetUsageEstimate(
            estimated_spend_usd=0.0,
            prompt_tokens=0,
            completion_tokens=0,
            model_calls=0,
            retries_per_trial=0,
            wall_clock_seconds=0,
            trial_count=0,
        )
    )
    if (
        valid_policy.max_prompt_tokens is None
        or valid_policy.max_completion_tokens is None
    ):
        diagnostics.append(
            _budget_diagnostic(
                EvalBudgetDiagnosticCode.MISSING_TOKEN_CEILING,
                "eval_reports.budget.token_ceilings",
                "Budget policy must declare prompt and completion token ceilings.",
            )
        )
    if valid_policy.max_trials_per_campaign is None:
        diagnostics.append(
            _budget_diagnostic(
                EvalBudgetDiagnosticCode.MISSING_TRIAL_COUNT_CEILING,
                "eval_reports.budget.trial_ceiling",
                "Budget policy must declare a trial-count ceiling.",
            )
        )
    if campaign_manifest.execution_mode is EvalSuiteExecutionMode.OFFLINE_FAKE:
        diagnostics.extend(_offline_budget_diagnostics(valid_policy))
    else:
        diagnostics.extend(_live_budget_metadata_diagnostics(valid_policy))
    diagnostics.extend(_budget_usage_diagnostics(valid_policy, usage_estimate))
    return EvalBudgetValidationResult(
        valid=not diagnostics,
        diagnostics=tuple(diagnostics),
    )


def admit_eval_report_campaign(
    campaign_manifest: EvalCampaignManifest,
    *,
    budget_policy: EvalReportBudgetPolicy | Mapping[str, Any] | None,
    usage: EvalBudgetUsageEstimate | Mapping[str, Any] | None = None,
) -> EvalLiveAdmissionResult:
    """Return deterministic live/offline admission with structured diagnostics."""
    budget_result = validate_eval_budget_policy(
        budget_policy,
        campaign_manifest=campaign_manifest,
        usage=usage,
    )
    if campaign_manifest.execution_mode is EvalSuiteExecutionMode.OFFLINE_FAKE:
        diagnostics = (
            ()
            if budget_result.valid
            else (
                EvalLiveAdmissionDiagnostic(
                    diagnostic_code=EvalLiveAdmissionDiagnosticCode.BUDGET_POLICY_INVALID,
                    rule_id="eval_reports.admission.offline_budget_policy",
                    summary="Offline fake admission requires a complete zero-cost bounded budget policy.",
                ),
            )
        )
        return EvalLiveAdmissionResult(
            status=EvalLiveAdmissionStatus.ADMITTED
            if budget_result.valid
            else EvalLiveAdmissionStatus.DENIED,
            diagnostics=diagnostics,
            budget_result=budget_result,
        )
    return EvalLiveAdmissionResult(
        status=EvalLiveAdmissionStatus.DENIED,
        diagnostics=_live_unresolved_dependency_diagnostics(budget_result),
        budget_result=budget_result,
    )


def build_eval_report_payload(
    *,
    report_id: str,
    campaign_manifest: EvalCampaignManifest,
    plans: Sequence[EvalTrialPlan],
    records: Sequence[EvalTrialRecord],
    budget_policy: EvalReportBudgetPolicy,
    usage: EvalBudgetUsageEstimate | None = None,
    resume_index: EvalTrialResumeIndex | None = None,
    generated_at: str = "1970-01-01T00:00:00Z",
    decision_rules: Sequence[EvalDecisionRule] = (),
    manual_taxonomy: Mapping[str, EvalFailureTaxonomyAssignment | Mapping[str, Any]]
    | None = None,
) -> EvalReportPayload:
    """Build a deterministic pilot report from existing offline contracts."""
    _validate_report_inputs(campaign_manifest, plans, records, resume_index)
    usage = usage or _default_budget_usage_estimate(plans=plans, records=records)
    admission = admit_eval_report_campaign(
        campaign_manifest,
        budget_policy=budget_policy,
        usage=usage,
    )
    manual_taxonomy = _validate_manual_taxonomy_assignments(manual_taxonomy or {})
    metrics = _primary_metrics(
        plans=plans,
        records=records,
        resume_index=resume_index,
        usage=usage,
    )
    input_hash = _report_input_hash(campaign_manifest, plans, records, resume_index)
    payload = EvalReportPayload.model_construct(
        schema_version=EVAL_REPORT_SCHEMA_VERSION,
        report_id=report_id,
        campaign_id=campaign_manifest.campaign_id,
        generated_at=generated_at,
        admission=admission,
        arms=(EvalTrialArmId.EVAL_SMALL_PI, EvalTrialArmId.EVAL_SMALL_MILLFORGE),
        controlled_variables={
            "model_manifest_hash": campaign_manifest.model_manifest_hash,
            "workflow_graph_hash": campaign_manifest.workflow_graph_hash,
            "fixture_pack_hash": campaign_manifest.fixture_pack_hash,
            "scorer_version": campaign_manifest.scorer_version,
        },
        budget_policy=budget_policy,
        budget_usage=usage,
        primary_metrics=metrics,
        arm_summaries=_arm_summaries(plans=plans, records=records),
        paired_comparisons=_paired_comparisons(plans=plans, records=records),
        task_summaries=_task_summaries(plans=plans, records=records),
        category_summaries=_category_summaries(plans=plans, records=records),
        taxonomy_summaries=_taxonomy_summaries(records, manual_taxonomy),
        invalid_trials=_invalid_trial_summary(records),
        statistical_summaries=_statistical_summaries(
            metrics=metrics,
            paired_comparisons=_paired_comparisons(plans=plans, records=records),
            usage=usage,
        ),
        confounds=default_eval_report_confounds(
            offline_fake=campaign_manifest.execution_mode
            is EvalSuiteExecutionMode.OFFLINE_FAKE
        ),
        decision_rules=tuple(decision_rules)
        or default_eval_report_decision_rules(metrics),
        reproducibility_hashes=EvalReportReproducibilityHashes(
            campaign_manifest_hash=campaign_manifest.campaign_manifest_hash,
            plan_hashes=tuple(plan.plan_hash for plan in plans),
            resume_index_hash=resume_index.resume_index_hash
            if resume_index is not None
            else None,
            record_hashes=tuple(record.record_hash for record in records),
            report_input_hash=input_hash,
        ),
        claim_boundaries=(
            "Offline fake reports are contract and harness-surface evidence only.",
            "No Pi-vs-Millforge model-performance conclusion can be drawn.",
            "Small pilot samples are descriptive unless a decision rule says otherwise.",
        ),
        report_hash_kind=EVAL_REPORT_HASH_KIND,
        report_hash="0" * 64,
    )
    return EvalReportPayload.model_validate(
        payload.model_copy(update={"report_hash": calculate_eval_report_hash(payload)})
    )


def render_eval_markdown_report(payload: EvalReportPayload) -> EvalMarkdownReport:
    """Render a deterministic human-readable Markdown report."""
    primary_metric_ids = {
        EvalReportMetricId.VALID_COMPLETION,
        EvalReportMetricId.FALSE_CLOSURE,
        EvalReportMetricId.FALSE_SUCCESS,
        EvalReportMetricId.ARTIFACT_COMPLETE,
        EvalReportMetricId.CAPABILITY_VIOLATION,
        EvalReportMetricId.CORRECTLY_BLOCKED,
        EvalReportMetricId.FALSE_BLOCKED,
        EvalReportMetricId.RUNTIME_FAILURE,
        EvalReportMetricId.PROVIDER_FAILURE,
        EvalReportMetricId.INVALID_TRIAL,
    }
    primary_metrics = tuple(
        metric
        for metric in payload.primary_metrics
        if metric.metric_id in primary_metric_ids
    )
    secondary_metrics = tuple(
        metric
        for metric in payload.primary_metrics
        if metric.metric_id not in primary_metric_ids
    )
    lines = [
        f"# Eval Report {payload.report_id}",
        "",
        f"- Campaign: {payload.campaign_id}",
        f"- Admission status: {payload.admission.status.value}",
        f"- Arms: {payload.arms[0].value}, {payload.arms[1].value}",
        "",
        "## Controlled Variables",
        *[
            f"- {key}: {payload.controlled_variables[key]}"
            for key in sorted(payload.controlled_variables)
        ],
        "",
        "## Budget Summary",
        f"- Policy: {payload.budget_policy.policy_id}",
        f"- Pricing class: {payload.budget_policy.pricing_class.value}",
        f"- Estimated spend USD: {payload.budget_usage.estimated_spend_usd:.6f}",
        f"- Prompt tokens: {payload.budget_usage.prompt_tokens}",
        f"- Completion tokens: {payload.budget_usage.completion_tokens}",
        f"- Model calls: {payload.budget_usage.model_calls}",
        f"- Trial count: {payload.budget_usage.trial_count}",
        "",
        "## Claim Boundary",
        *[f"- {boundary}" for boundary in payload.claim_boundaries],
        "",
        "## Primary Metrics",
        *[_markdown_metric_line(metric) for metric in primary_metrics],
        *(
            (
                "",
                "## Secondary Metrics",
                *[_markdown_metric_line(metric) for metric in secondary_metrics],
            )
            if secondary_metrics
            else ()
        ),
        "",
        "## Severity-One Outcomes",
        *[
            f"- {metric.metric_id.value}: {metric.count}"
            for metric in payload.primary_metrics
            if metric.severity_one
        ],
        "",
        "## Invalid Trials",
        f"- Invalid records: {payload.invalid_trials.invalid_trial_count}/"
        f"{payload.invalid_trials.appended_record_count}",
        "",
        "## Per-Category Summary",
        *[
            "- "
            + str(
                summary.category.value
                if hasattr(summary.category, "value")
                else summary.category
            )
            + ": "
            + ", ".join(_inline_metric_summary(metric) for metric in summary.metrics)
            for summary in payload.category_summaries
        ],
        "",
        "## Statistics",
        *[
            f"- {summary.metric_id.value}: {summary.raw_count}/"
            f"{summary.denominator_count} ({summary.rate:.6f}); "
            f"{summary.diagnostic}"
            for summary in payload.statistical_summaries
        ],
        "",
        "## Failure Taxonomy",
        *[
            f"- {summary.category.value}: {summary.count}"
            for summary in payload.taxonomy_summaries
        ],
        "",
        "## Confounds",
        *[
            f"- {entry.confound_id.value}: {entry.summary}"
            for entry in payload.confounds
        ],
        "",
        "## Decision Rules",
        *[
            f"- {rule.rule_id}: {rule.status.value}; threshold={rule.threshold:.6f}; "
            f"observed={rule.observed_value if rule.observed_value is not None else 'n/a'}"
            for rule in payload.decision_rules
        ],
        "",
        "## Reproducibility",
        f"- Campaign manifest: {payload.reproducibility_hashes.campaign_manifest_hash}",
        *[
            f"- Plan hash: {plan_hash}"
            for plan_hash in payload.reproducibility_hashes.plan_hashes
        ],
        *[
            f"- Record hash: {record_hash}"
            for record_hash in payload.reproducibility_hashes.record_hashes
        ],
        *(
            (f"- Resume index: {payload.reproducibility_hashes.resume_index_hash}",)
            if payload.reproducibility_hashes.resume_index_hash is not None
            else ()
        ),
        f"- Report input: {payload.reproducibility_hashes.report_input_hash}",
        f"- Report JSON hash: {calculate_eval_report_json_hash(payload)}",
        f"- Report hash: {payload.report_hash}",
        "",
    ]
    content = "\n".join(lines)
    _reject_forbidden_material(content)
    return EvalMarkdownReport(
        report_id=payload.report_id,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _markdown_metric_line(metric: EvalMetricValue) -> str:
    return f"- {metric.metric_id.value}: {_inline_metric_summary(metric)}"


def _inline_metric_summary(metric: EvalMetricValue) -> str:
    if metric.value is not None:
        return (
            f"{metric.value:.6f} across {metric.count}/"
            f"{metric.denominator.count} source records"
        )
    return f"{metric.count}/{metric.denominator.count} ({metric.rate:.6f})"


def canonical_eval_report_bytes(value: BaseModel | Mapping[str, Any]) -> bytes:
    """Return canonical ASCII JSON bytes for an eval-report payload."""
    payload = (
        value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    )
    _reject_forbidden_material(payload)
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def calculate_eval_report_hash(report: EvalReportPayload) -> str:
    payload = report.model_dump(mode="json")
    payload.pop("report_hash", None)
    return hashlib.sha256(canonical_eval_report_bytes(payload)).hexdigest()


def calculate_eval_report_json_hash(report: EvalReportPayload) -> str:
    return hashlib.sha256(canonical_eval_report_bytes(report)).hexdigest()


def canonical_eval_report_json_bytes(report: EvalReportPayload) -> bytes:
    """Return deterministic report.json bytes for an eval-report payload."""
    return canonical_eval_report_bytes(report)


def canonical_eval_markdown_report_bytes(
    report: EvalMarkdownReport | EvalReportPayload,
) -> bytes:
    """Return deterministic report.md bytes from a Markdown report or payload."""
    markdown = (
        render_eval_markdown_report(report)
        if isinstance(report, EvalReportPayload)
        else report
    )
    _reject_forbidden_material(markdown.content)
    return markdown.content.encode("utf-8")


def build_eval_report_artifact_bytes(
    payload: EvalReportPayload,
) -> Mapping[str, bytes]:
    """Return deterministic report.json, report.md, and hash artifact bytes."""
    json_bytes = canonical_eval_report_json_bytes(payload)
    markdown_bytes = canonical_eval_markdown_report_bytes(payload)
    artifacts = {
        "report.json": json_bytes,
        "report.md": markdown_bytes,
        "report.sha256": (
            f"{EVAL_REPORT_HASH_KIND} {payload.report_hash}\n"
            f"{EVAL_REPORT_JSON_HASH_KIND} "
            f"{hashlib.sha256(json_bytes).hexdigest()}\n"
            f"{EVAL_REPORT_MARKDOWN_HASH_KIND} "
            f"{hashlib.sha256(markdown_bytes).hexdigest()}\n"
        ).encode("ascii"),
    }
    return _freeze_eval_report_mapping(artifacts)


def default_eval_report_budget_policy() -> EvalReportBudgetPolicy:
    """Return the bounded zero-cost policy admitted for offline fake reports."""
    rate_limit = EvalReportRateLimitPolicy(
        request_rate_per_window=1,
        token_rate_per_window=1,
        concurrent_request_limit=1,
        window_seconds=1,
        max_backoff_seconds=0,
        max_retries_per_trial=0,
    )
    return EvalReportBudgetPolicy(
        policy_id="eval.08c.default.offline_zero_cost.v1",
        pricing_class=EvalReportPricingClass.OFFLINE_ZERO_COST,
        max_spend_usd=0.0,
        max_prompt_tokens=0,
        max_completion_tokens=0,
        max_model_calls=0,
        max_retries_per_trial=0,
        max_wall_clock_seconds=0,
        max_trials_per_campaign=64,
        rate_limit_policy_by_arm={
            EvalTrialArmId.EVAL_SMALL_PI: rate_limit,
            EvalTrialArmId.EVAL_SMALL_MILLFORGE: rate_limit,
        },
        abort_thresholds=EvalReportAbortThresholds(
            max_false_closure_rate=0.0,
            max_capability_violation_rate=0.0,
            max_invalid_trial_rate=0.0,
        ),
        summary="Bounded zero-cost offline fake report policy.",
    )


def default_eval_report_confounds(
    *, offline_fake: bool = True
) -> tuple[EvalConfoundEntry, ...]:
    """Return the required public confound registry for pilot reports."""
    entries = tuple(
        EvalConfoundEntry(
            confound_id=confound_id,
            summary=_confound_summary(confound_id),
            affects_claims=True,
        )
        for confound_id in EvalReportConfoundId
    )
    if offline_fake:
        return entries
    return tuple(
        entry
        for entry in entries
        if entry.confound_id is not EvalReportConfoundId.OFFLINE_FAKE_LIMITATIONS
    )


def default_eval_report_decision_rules(
    metrics: Sequence[EvalMetricValue],
) -> tuple[EvalDecisionRule, ...]:
    """Return pre-registered descriptive pilot decision rules."""
    by_id = {metric.metric_id: metric for metric in metrics}
    false_closure = by_id.get(EvalReportMetricId.FALSE_CLOSURE)
    valid_completion = by_id.get(EvalReportMetricId.VALID_COMPLETION)
    false_blocked = by_id.get(EvalReportMetricId.FALSE_BLOCKED)
    capability_violation = by_id.get(EvalReportMetricId.CAPABILITY_VIOLATION)
    invalid_trial = by_id.get(EvalReportMetricId.INVALID_TRIAL)
    return (
        EvalDecisionRule(
            rule_id="eval_reports.rules.max_false_closure_rate",
            rule_kind=EvalDecisionRuleKind.MAX_FALSE_CLOSURE_RATE,
            summary="Severity-one false-closure rate must remain at or below the threshold.",
            metric_id=EvalReportMetricId.FALSE_CLOSURE,
            threshold=0.0,
            status=_threshold_status(false_closure, 0.0),
            observed_value=false_closure.rate if false_closure else None,
            diagnostic="Pilot samples are descriptive unless confirmed later.",
        ),
        EvalDecisionRule(
            rule_id="eval_reports.rules.min_completion_improvement",
            rule_kind=EvalDecisionRuleKind.MIN_COMPLETION_IMPROVEMENT,
            summary="Minimum completion improvement worth pursuing must be positive in paired follow-up campaigns.",
            metric_id=EvalReportMetricId.COMPLETION_IMPROVEMENT,
            threshold=0.05,
            status=EvalDecisionRuleStatus.DESCRIPTIVE_ONLY,
            observed_value=valid_completion.rate if valid_completion else None,
            diagnostic="Pilot report records raw completion rates; improvement claims require powered paired follow-up.",
        ),
        EvalDecisionRule(
            rule_id="eval_reports.rules.max_cost_multiplier",
            rule_kind=EvalDecisionRuleKind.MAX_COST_MULTIPLIER,
            summary="Cost multiplier must stay within the pre-registered ceiling.",
            metric_id=EvalReportMetricId.COST_MULTIPLIER,
            threshold=1.5,
            status=EvalDecisionRuleStatus.DESCRIPTIVE_ONLY,
            observed_value=0.0,
            diagnostic="Offline fake cost is zero; live cost multipliers are descriptive until source-present usage exists.",
        ),
        EvalDecisionRule(
            rule_id="eval_reports.rules.max_latency_multiplier",
            rule_kind=EvalDecisionRuleKind.MAX_LATENCY_MULTIPLIER,
            summary="Latency multiplier must stay within the pre-registered ceiling.",
            metric_id=EvalReportMetricId.LATENCY_MULTIPLIER,
            threshold=1.5,
            status=EvalDecisionRuleStatus.DESCRIPTIVE_ONLY,
            observed_value=0.0,
            diagnostic="Offline fake latency is not a live performance measurement.",
        ),
        EvalDecisionRule(
            rule_id="eval_reports.rules.acceptable_false_blocked_tradeoff",
            rule_kind=EvalDecisionRuleKind.ACCEPTABLE_FALSE_BLOCKED_TRADEOFF,
            summary="False-blocked rate must be weighed against false-closure reduction.",
            metric_id=EvalReportMetricId.FALSE_BLOCKED,
            threshold=0.05,
            status=EvalDecisionRuleStatus.DESCRIPTIVE_ONLY,
            observed_value=false_blocked.rate if false_blocked else None,
            diagnostic="False-blocked tradeoff is descriptive in small pilot samples.",
        ),
        EvalDecisionRule(
            rule_id="eval_reports.rules.severity_one_abort_thresholds",
            rule_kind=EvalDecisionRuleKind.SEVERITY_ONE_ABORT_THRESHOLD,
            summary="Severity-one false closure, capability violation, and invalid trial rates remain abort-visible.",
            metric_id=EvalReportMetricId.INVALID_TRIAL,
            threshold=0.0,
            status=(
                EvalDecisionRuleStatus.FAILED
                if any(
                    metric is not None and metric.rate > 0.0
                    for metric in (false_closure, capability_violation, invalid_trial)
                )
                else EvalDecisionRuleStatus.PASSED
            ),
            observed_value=invalid_trial.rate if invalid_trial else None,
            diagnostic="Pilot samples are descriptive unless confirmed later.",
        ),
    )


def wilson_score_interval(
    successes: int,
    total: int,
    *,
    z: float = 1.959963984540054,
    min_total: int = 30,
) -> tuple[float, float] | None:
    """Return a Wilson interval only when the sample is large enough."""
    if total < min_total:
        return None
    if successes < 0 or total < 0 or successes > total:
        raise ValueError("invalid Wilson interval counts")
    phat = successes / total
    denominator = 1.0 + z * z / total
    center = phat + z * z / (2 * total)
    spread = z * ((phat * (1.0 - phat) + z * z / (4 * total)) / total) ** 0.5
    return ((center - spread) / denominator, (center + spread) / denominator)


def _offline_budget_diagnostics(
    policy: EvalReportBudgetPolicy,
) -> tuple[EvalBudgetDiagnostic, ...]:
    diagnostics: list[EvalBudgetDiagnostic] = []
    if policy.pricing_class is not EvalReportPricingClass.OFFLINE_ZERO_COST:
        diagnostics.append(
            _budget_diagnostic(
                EvalBudgetDiagnosticCode.OFFLINE_POLICY_NOT_ZERO_COST,
                "eval_reports.budget.offline_zero_cost",
                "Offline fake campaigns require the offline_zero_cost pricing class.",
            )
        )
    bounded_fields = (
        policy.max_prompt_tokens,
        policy.max_completion_tokens,
        policy.max_model_calls,
        policy.max_retries_per_trial,
        policy.max_wall_clock_seconds,
        policy.max_trials_per_campaign,
    )
    if any(value is None for value in bounded_fields):
        diagnostics.append(
            _budget_diagnostic(
                EvalBudgetDiagnosticCode.OFFLINE_POLICY_UNBOUNDED,
                "eval_reports.budget.offline_bounded",
                "Offline fake campaigns must declare token, model-call, retry, wall-clock, and trial ceilings.",
            )
        )
    if policy.max_spend_usd != 0.0:
        diagnostics.append(
            _budget_diagnostic(
                EvalBudgetDiagnosticCode.OFFLINE_POLICY_NOT_ZERO_COST,
                "eval_reports.budget.offline_spend",
                "Offline fake campaigns require a zero spend ceiling.",
            )
        )
    return tuple(diagnostics)


def _live_budget_metadata_diagnostics(
    policy: EvalReportBudgetPolicy,
) -> tuple[EvalBudgetDiagnostic, ...]:
    diagnostics: list[EvalBudgetDiagnostic] = []
    required_live_ceilings = (
        policy.max_spend_usd,
        policy.max_model_calls,
        policy.max_retries_per_trial,
        policy.max_wall_clock_seconds,
    )
    if any(ceiling is None for ceiling in required_live_ceilings):
        diagnostics.append(
            _budget_diagnostic(
                EvalBudgetDiagnosticCode.MISSING_LIVE_BUDGET_METADATA,
                "eval_reports.budget.live_metadata",
                "Live campaigns require explicit spend, model-call, retry, and wall-clock ceilings.",
            )
        )
    if (
        policy.pricing_class is EvalReportPricingClass.PROMOTIONAL_FREE_WINDOW
        and policy.promotional_free_window is None
    ):
        diagnostics.append(
            _budget_diagnostic(
                EvalBudgetDiagnosticCode.INCOMPLETE_PROMOTIONAL_FREE_WINDOW,
                "eval_reports.budget.promotional_window",
                "Promotional free-window pricing requires complete window metadata.",
            )
        )
    arms = {
        EvalTrialArmId.EVAL_SMALL_PI,
        EvalTrialArmId.EVAL_SMALL_MILLFORGE,
    }
    if set(policy.rate_limit_policy_by_arm) != arms:
        diagnostics.append(
            _budget_diagnostic(
                EvalBudgetDiagnosticCode.UNFAIR_PAIRED_ARM_RATE_LIMIT,
                "eval_reports.budget.paired_rate_limits",
                "Paired arms require explicit rate-limit metadata for both arms.",
            )
        )
    elif (
        len(
            {
                canonical_eval_report_bytes(item)
                for item in policy.rate_limit_policy_by_arm.values()
            }
        )
        != 1
    ):
        diagnostics.append(
            _budget_diagnostic(
                EvalBudgetDiagnosticCode.UNFAIR_PAIRED_ARM_RATE_LIMIT,
                "eval_reports.budget.paired_rate_limit_parity",
                "Paired arm rate-limit metadata must be identical for fair admission.",
            )
        )
    return tuple(diagnostics)


def _budget_usage_diagnostics(
    policy: EvalReportBudgetPolicy,
    usage: EvalBudgetUsageEstimate,
) -> tuple[EvalBudgetDiagnostic, ...]:
    checks = (
        (
            policy.max_spend_usd,
            usage.estimated_spend_usd,
            EvalBudgetDiagnosticCode.SPEND_CEILING_EXCEEDED,
            "spend",
        ),
        (
            policy.max_prompt_tokens,
            usage.prompt_tokens,
            EvalBudgetDiagnosticCode.PROMPT_TOKEN_CEILING_EXCEEDED,
            "prompt tokens",
        ),
        (
            policy.max_completion_tokens,
            usage.completion_tokens,
            EvalBudgetDiagnosticCode.COMPLETION_TOKEN_CEILING_EXCEEDED,
            "completion tokens",
        ),
        (
            policy.max_model_calls,
            usage.model_calls,
            EvalBudgetDiagnosticCode.MODEL_CALL_CEILING_EXCEEDED,
            "model calls",
        ),
        (
            policy.max_retries_per_trial,
            usage.retries_per_trial,
            EvalBudgetDiagnosticCode.RETRY_CEILING_EXCEEDED,
            "retries per trial",
        ),
        (
            policy.max_wall_clock_seconds,
            usage.wall_clock_seconds,
            EvalBudgetDiagnosticCode.WALL_CLOCK_CEILING_EXCEEDED,
            "wall-clock seconds",
        ),
        (
            policy.max_trials_per_campaign,
            usage.trial_count,
            EvalBudgetDiagnosticCode.TRIAL_COUNT_CEILING_EXCEEDED,
            "trials",
        ),
    )
    diagnostics: list[EvalBudgetDiagnostic] = []
    for ceiling, observed, code, label in checks:
        if ceiling is not None and observed > ceiling:
            diagnostics.append(
                _budget_diagnostic(
                    code,
                    f"eval_reports.budget.{code.value}",
                    f"Campaign exceeds configured {label} ceiling.",
                )
            )
    return tuple(diagnostics)


def _live_unresolved_dependency_diagnostics(
    budget_result: EvalBudgetValidationResult,
) -> tuple[EvalLiveAdmissionDiagnostic, ...]:
    diagnostics = [
        EvalLiveAdmissionDiagnostic(
            diagnostic_code=EvalLiveAdmissionDiagnosticCode.PI_RUNTIME_UNAVAILABLE,
            rule_id="eval_reports.live.pi_runtime",
            summary="Pi runtime support is not available for live eval campaigns.",
        ),
        EvalLiveAdmissionDiagnostic(
            diagnostic_code=EvalLiveAdmissionDiagnosticCode.MILLFORGE_LIVE_HARNESS_UNAVAILABLE,
            rule_id="eval_reports.live.millforge_harness",
            summary="Millforge live harness execution is not available for live eval campaigns.",
        ),
        EvalLiveAdmissionDiagnostic(
            diagnostic_code=EvalLiveAdmissionDiagnosticCode.SHARED_BACKEND_CONFIGURATION_MISSING,
            rule_id="eval_reports.live.shared_backend",
            summary="Shared backend configuration is unresolved.",
        ),
        EvalLiveAdmissionDiagnostic(
            diagnostic_code=EvalLiveAdmissionDiagnosticCode.FIXTURE_WORKSPACE_LIFECYCLE_UNAVAILABLE,
            rule_id="eval_reports.live.fixture_workspace",
            summary="Fixture workspace creation and reset lifecycle is unresolved.",
        ),
        EvalLiveAdmissionDiagnostic(
            diagnostic_code=EvalLiveAdmissionDiagnosticCode.RESOURCE_ENFORCEMENT_UNAVAILABLE,
            rule_id="eval_reports.live.resource_enforcement",
            summary="Resource ceiling enforcement is unresolved.",
        ),
        EvalLiveAdmissionDiagnostic(
            diagnostic_code=EvalLiveAdmissionDiagnosticCode.BUDGET_POLICY_INVALID,
            rule_id="eval_reports.live.budget_policy",
            summary="Live budget policy enforcement is unresolved.",
        ),
        EvalLiveAdmissionDiagnostic(
            diagnostic_code=EvalLiveAdmissionDiagnosticCode.APPEND_ONLY_STORE_SAFETY_UNPROVEN,
            rule_id="eval_reports.live.append_only_store",
            summary="Append-only store safety has not been proven for live runs.",
        ),
        EvalLiveAdmissionDiagnostic(
            diagnostic_code=EvalLiveAdmissionDiagnosticCode.DETERMINISTIC_SCORER_UNAVAILABLE,
            rule_id="eval_reports.live.deterministic_scorer",
            summary="Deterministic scorer availability is unresolved for live runs.",
        ),
    ]
    return tuple(diagnostics)


def _primary_metrics(
    *,
    plans: Sequence[EvalTrialPlan],
    records: Sequence[EvalTrialRecord],
    resume_index: EvalTrialResumeIndex | None,
    usage: EvalBudgetUsageEstimate,
) -> tuple[EvalMetricValue, ...]:
    planned_denominator = EvalMetricDenominator(
        denominator_kind=EvalMetricDenominatorKind.PLANNED_TRIALS,
        count=len(plans) * 2,
        summary="All planned per-arm trial outcomes.",
    )
    record_denominator = EvalMetricDenominator(
        denominator_kind=EvalMetricDenominatorKind.APPENDED_RECORDS,
        count=len(records) * 2,
        summary="All appended per-arm trial records.",
    )
    planned_pair_denominator = EvalMetricDenominator(
        denominator_kind=EvalMetricDenominatorKind.PLANNED_TRIALS,
        count=len(plans),
        summary="All planned fixture/trial-index pairs.",
    )
    pending_trial_count = _pending_trial_count(
        plans=plans,
        records=records,
        resume_index=resume_index,
    )
    missing_pair_count = len(_missing_pair_keys(plans=plans, records=records))
    outcomes = [
        result.scorer_result.final_outcome
        for record in records
        for result in record.arm_results
    ]
    results = [
        result.scorer_result for record in records for result in record.arm_results
    ]
    return (
        _metric(
            EvalReportMetricId.VALID_COMPLETION,
            outcomes.count(EvalTrialOutcome.VALID_COMPLETION),
            record_denominator,
        ),
        _metric(
            EvalReportMetricId.FALSE_CLOSURE,
            outcomes.count(EvalTrialOutcome.FALSE_CLOSURE),
            record_denominator,
            severity_one=True,
        ),
        _metric(
            EvalReportMetricId.FALSE_SUCCESS,
            sum(result.false_success for result in results),
            record_denominator,
        ),
        _metric(
            EvalReportMetricId.ARTIFACT_COMPLETE,
            sum(result.artifact_complete for result in results),
            record_denominator,
        ),
        _metric(
            EvalReportMetricId.CAPABILITY_VIOLATION,
            sum(result.capability_violation for result in results),
            record_denominator,
            severity_one=True,
        ),
        _metric(
            EvalReportMetricId.CORRECTLY_BLOCKED,
            outcomes.count(EvalTrialOutcome.CORRECTLY_BLOCKED),
            record_denominator,
        ),
        _metric(
            EvalReportMetricId.FALSE_BLOCKED,
            outcomes.count(EvalTrialOutcome.FALSE_BLOCKED),
            record_denominator,
        ),
        _metric(
            EvalReportMetricId.RUNTIME_FAILURE,
            outcomes.count(EvalTrialOutcome.RUNTIME_FAILURE),
            planned_denominator,
        ),
        _metric(
            EvalReportMetricId.PROVIDER_FAILURE,
            outcomes.count(EvalTrialOutcome.PROVIDER_FAILURE),
            planned_denominator,
        ),
        _metric(
            EvalReportMetricId.INVALID_TRIAL,
            outcomes.count(EvalTrialOutcome.INVALID_TRIAL),
            record_denominator,
            severity_one=True,
        ),
        _metric(
            EvalReportMetricId.MISSING_PAIR,
            missing_pair_count,
            planned_pair_denominator,
        ),
        _metric(
            EvalReportMetricId.PENDING_TRIAL,
            pending_trial_count,
            planned_pair_denominator,
        ),
        _metric(
            EvalReportMetricId.INCOMPLETE_TRIAL,
            pending_trial_count,
            planned_pair_denominator,
        ),
        *_budget_usage_metrics(usage),
        *_resource_usage_metrics(
            records,
            summary_prefix=(
                "Total source-present resource usage in appended trial records."
            ),
        ),
    )


def _value_metric(
    metric_id: EvalReportMetricId,
    value: float,
    denominator: EvalMetricDenominator,
    *,
    count: int | None = None,
) -> EvalMetricValue:
    source_count = denominator.count if count is None else count
    return EvalMetricValue(
        metric_id=metric_id,
        count=source_count,
        denominator=denominator,
        rate=0.0 if denominator.count == 0 else source_count / denominator.count,
        value=float(value),
    )


def _metric(
    metric_id: EvalReportMetricId,
    count: int,
    denominator: EvalMetricDenominator,
    *,
    severity_one: bool = False,
) -> EvalMetricValue:
    return EvalMetricValue(
        metric_id=metric_id,
        count=count,
        denominator=denominator,
        rate=0.0 if denominator.count == 0 else count / denominator.count,
        severity_one=severity_one,
    )


def _budget_usage_metrics(
    usage: EvalBudgetUsageEstimate,
) -> tuple[EvalMetricValue, ...]:
    denominator = EvalMetricDenominator(
        denominator_kind=EvalMetricDenominatorKind.PLANNED_TRIALS,
        count=usage.trial_count,
        summary=(
            "Campaign-level source-present budget usage covering planned trial pairs."
        ),
    )
    return (
        _value_metric(
            EvalReportMetricId.ESTIMATED_COST,
            usage.estimated_spend_usd,
            denominator,
        ),
        _value_metric(
            EvalReportMetricId.WALL_CLOCK_SECONDS,
            float(usage.wall_clock_seconds),
            denominator,
        ),
        _value_metric(
            EvalReportMetricId.RETRIES,
            float(usage.retries_per_trial),
            denominator,
        ),
        _value_metric(
            EvalReportMetricId.MODEL_CALLS,
            float(usage.model_calls),
            denominator,
        ),
        _value_metric(
            EvalReportMetricId.PROMPT_TOKENS,
            float(usage.prompt_tokens),
            denominator,
        ),
        _value_metric(
            EvalReportMetricId.COMPLETION_TOKENS,
            float(usage.completion_tokens),
            denominator,
        ),
    )


def _default_budget_usage_estimate(
    *,
    plans: Sequence[EvalTrialPlan],
    records: Sequence[EvalTrialRecord],
) -> EvalBudgetUsageEstimate:
    return EvalBudgetUsageEstimate(
        estimated_spend_usd=0.0,
        prompt_tokens=sum(
            record.model_usage_summary.input_tokens for record in records
        ),
        completion_tokens=sum(
            record.model_usage_summary.output_tokens for record in records
        ),
        model_calls=sum(
            record.model_usage_summary.model_call_count for record in records
        ),
        retries_per_trial=0,
        wall_clock_seconds=0,
        trial_count=len(plans),
    )


def _model_usage_metrics(
    records: Sequence[EvalTrialRecord],
    *,
    summary_prefix: str,
) -> tuple[EvalMetricValue, ...]:
    if not records:
        return ()
    usage_totals = (
        (
            EvalReportMetricId.MODEL_CALLS,
            sum(record.model_usage_summary.model_call_count for record in records),
        ),
        (
            EvalReportMetricId.PROMPT_TOKENS,
            sum(record.model_usage_summary.input_tokens for record in records),
        ),
        (
            EvalReportMetricId.COMPLETION_TOKENS,
            sum(record.model_usage_summary.output_tokens for record in records),
        ),
    )
    return tuple(
        _value_metric(
            metric_id,
            float(count),
            EvalMetricDenominator(
                denominator_kind=EvalMetricDenominatorKind.APPENDED_RECORDS,
                count=len(records),
                summary=summary_prefix,
            ),
        )
        for metric_id, count in usage_totals
    )


def _resource_usage_metrics(
    records: Sequence[EvalTrialRecord],
    *,
    summary_prefix: str,
) -> tuple[EvalMetricValue, ...]:
    if not records:
        return ()
    usage_totals = (
        (
            EvalReportMetricId.ARTIFACT_COUNT,
            sum(record.resource_summary.artifact_count for record in records),
        ),
        (
            EvalReportMetricId.ARTIFACT_BYTES,
            sum(record.resource_summary.artifact_bytes for record in records),
        ),
        (
            EvalReportMetricId.TURNS,
            sum(record.resource_summary.turn_count for record in records),
        ),
        (
            EvalReportMetricId.INVALID_TOOL_CALLS,
            sum(record.resource_summary.invalid_tool_call_count for record in records),
        ),
        (
            EvalReportMetricId.MALFORMED_ARGUMENTS,
            sum(record.resource_summary.malformed_argument_count for record in records),
        ),
        (
            EvalReportMetricId.PREREQUISITE_VIOLATIONS,
            sum(
                record.resource_summary.prerequisite_violation_count
                for record in records
            ),
        ),
        (
            EvalReportMetricId.PREMATURE_TERMINALS,
            sum(record.resource_summary.premature_terminal_count for record in records),
        ),
        (
            EvalReportMetricId.TOOL_RECOVERIES,
            sum(record.resource_summary.tool_recovery_count for record in records),
        ),
    )
    return tuple(
        _value_metric(
            metric_id,
            float(count),
            EvalMetricDenominator(
                denominator_kind=EvalMetricDenominatorKind.APPENDED_RECORDS,
                count=len(records),
                summary=summary_prefix,
            ),
        )
        for metric_id, count in usage_totals
    )


def _paired_comparisons(
    *, plans: Sequence[EvalTrialPlan], records: Sequence[EvalTrialRecord]
) -> tuple[EvalPairedComparison, ...]:
    record_by_trial = {record.trial_id: record for record in records}
    paired = [
        record
        for plan in plans
        if (record := record_by_trial.get(plan.trial_id)) is not None
    ]
    left_count = sum(
        result.scorer_result.primary_success
        for record in paired
        for result in record.arm_results
        if result.arm_id is EvalTrialArmId.EVAL_SMALL_PI
    )
    right_count = sum(
        result.scorer_result.primary_success
        for record in paired
        for result in record.arm_results
        if result.arm_id is EvalTrialArmId.EVAL_SMALL_MILLFORGE
    )
    return (
        EvalPairedComparison(
            metric_id=EvalReportMetricId.VALID_COMPLETION,
            left_arm_id=EvalTrialArmId.EVAL_SMALL_PI,
            right_arm_id=EvalTrialArmId.EVAL_SMALL_MILLFORGE,
            left_count=left_count,
            right_count=right_count,
            paired_denominator=len(paired),
            difference=right_count - left_count,
            missing_pair_count=len(_missing_pair_keys(plans=plans, records=records)),
        ),
    )


def _arm_summaries(
    *, plans: Sequence[EvalTrialPlan], records: Sequence[EvalTrialRecord]
) -> tuple[EvalArmSummary, ...]:
    summaries: list[EvalArmSummary] = []
    planned_count = len(plans)
    for arm_id in (EvalTrialArmId.EVAL_SMALL_PI, EvalTrialArmId.EVAL_SMALL_MILLFORGE):
        arm_results = [
            result.scorer_result
            for record in records
            for result in record.arm_results
            if result.arm_id is arm_id
        ]
        outcomes = [result.final_outcome for result in arm_results]
        record_denominator = EvalMetricDenominator(
            denominator_kind=EvalMetricDenominatorKind.APPENDED_RECORDS,
            count=len(arm_results),
            summary=f"All valid and invalid appended records for {arm_id.value}.",
        )
        planned_denominator = EvalMetricDenominator(
            denominator_kind=EvalMetricDenominatorKind.PLANNED_TRIALS,
            count=planned_count,
            summary=f"All planned trials for {arm_id.value}.",
        )
        summaries.append(
            EvalArmSummary(
                arm_id=arm_id,
                metrics=(
                    _metric(
                        EvalReportMetricId.VALID_COMPLETION,
                        outcomes.count(EvalTrialOutcome.VALID_COMPLETION),
                        record_denominator,
                    ),
                    _metric(
                        EvalReportMetricId.INVALID_TRIAL,
                        outcomes.count(EvalTrialOutcome.INVALID_TRIAL),
                        record_denominator,
                        severity_one=True,
                    ),
                    _metric(
                        EvalReportMetricId.RUNTIME_FAILURE,
                        outcomes.count(EvalTrialOutcome.RUNTIME_FAILURE),
                        planned_denominator,
                    ),
                    _metric(
                        EvalReportMetricId.PROVIDER_FAILURE,
                        outcomes.count(EvalTrialOutcome.PROVIDER_FAILURE),
                        planned_denominator,
                    ),
                    _metric(
                        EvalReportMetricId.ARTIFACT_COMPLETE,
                        sum(result.artifact_complete for result in arm_results),
                        record_denominator,
                    ),
                ),
            )
        )
    return tuple(summaries)


def _task_summaries(
    *, plans: Sequence[EvalTrialPlan], records: Sequence[EvalTrialRecord]
) -> tuple[EvalTaskSummary, ...]:
    record_by_trial = {record.trial_id: record for record in records}
    summaries: list[EvalTaskSummary] = []
    for plan in plans:
        record = record_by_trial.get(plan.trial_id)
        denominator = EvalMetricDenominator(
            denominator_kind=EvalMetricDenominatorKind.APPENDED_RECORDS,
            count=2 if record else 0,
            summary="Per-task appended arm records.",
        )
        success_count = (
            sum(result.scorer_result.primary_success for result in record.arm_results)
            if record
            else 0
        )
        invalid_count = (
            sum(
                result.scorer_result.final_outcome is EvalTrialOutcome.INVALID_TRIAL
                for result in record.arm_results
            )
            if record
            else 0
        )
        summaries.append(
            EvalTaskSummary(
                fixture_id=plan.fixture_instance.fixture_id,
                trial_index=plan.trial_index,
                category=plan.fixture_instance.public_projection.category.value,
                metrics=(
                    _metric(
                        EvalReportMetricId.VALID_COMPLETION,
                        success_count,
                        denominator,
                    ),
                    _metric(
                        EvalReportMetricId.INVALID_TRIAL,
                        invalid_count,
                        denominator,
                        severity_one=True,
                    ),
                    _metric(
                        EvalReportMetricId.INCOMPLETE_TRIAL,
                        0 if record else 1,
                        EvalMetricDenominator(
                            denominator_kind=EvalMetricDenominatorKind.PLANNED_TRIALS,
                            count=1,
                            summary="One planned fixture/trial-index pair.",
                        ),
                    ),
                    *_model_usage_metrics(
                        (record,) if record else (),
                        summary_prefix="Per-task source-present model usage.",
                    ),
                    *_resource_usage_metrics(
                        (record,) if record else (),
                        summary_prefix="Per-task source-present resource usage.",
                    ),
                ),
            )
        )
    return tuple(summaries)


def _category_summaries(
    *, plans: Sequence[EvalTrialPlan], records: Sequence[EvalTrialRecord]
) -> tuple[EvalCategorySummary, ...]:
    planned_by_category: Counter[str] = Counter(
        plan.fixture_instance.public_projection.category.value for plan in plans
    )
    by_category: dict[str, list[EvalTrialOutcome]] = {
        category: [] for category in planned_by_category
    }
    records_by_category: dict[str, list[EvalTrialRecord]] = {
        category: [] for category in planned_by_category
    }
    for record in records:
        records_by_category.setdefault(record.task_category, []).append(record)
        by_category.setdefault(record.task_category, []).extend(
            result.scorer_result.final_outcome for result in record.arm_results
        )
    summaries: list[EvalCategorySummary] = []
    for category in sorted(by_category):
        outcomes = by_category[category]
        denominator = EvalMetricDenominator(
            denominator_kind=EvalMetricDenominatorKind.APPENDED_RECORDS,
            count=len(outcomes),
            summary="Per-category appended arm records.",
        )
        summaries.append(
            EvalCategorySummary(
                category=category,
                metrics=(
                    _metric(
                        EvalReportMetricId.VALID_COMPLETION,
                        outcomes.count(EvalTrialOutcome.VALID_COMPLETION),
                        denominator,
                    ),
                    _metric(
                        EvalReportMetricId.INVALID_TRIAL,
                        outcomes.count(EvalTrialOutcome.INVALID_TRIAL),
                        denominator,
                        severity_one=True,
                    ),
                    _metric(
                        EvalReportMetricId.INCOMPLETE_TRIAL,
                        max(0, planned_by_category[category] - len(outcomes) // 2),
                        EvalMetricDenominator(
                            denominator_kind=EvalMetricDenominatorKind.PLANNED_TRIALS,
                            count=planned_by_category[category],
                            summary="Planned trial pairs for this category.",
                        ),
                    ),
                    *_model_usage_metrics(
                        records_by_category.get(category, ()),
                        summary_prefix="Per-category source-present model usage.",
                    ),
                    *_resource_usage_metrics(
                        records_by_category.get(category, ()),
                        summary_prefix="Per-category source-present resource usage.",
                    ),
                ),
            )
        )
    return tuple(summaries)


def _missing_pair_keys(
    *, plans: Sequence[EvalTrialPlan], records: Sequence[EvalTrialRecord]
) -> set[tuple[str, int]]:
    recorded_pairs = {(record.fixture_id, record.trial_index) for record in records}
    return {
        (plan.fixture_instance.fixture_id, plan.trial_index)
        for plan in plans
        if (plan.fixture_instance.fixture_id, plan.trial_index) not in recorded_pairs
    }


def _pending_trial_count(
    *,
    plans: Sequence[EvalTrialPlan],
    records: Sequence[EvalTrialRecord],
    resume_index: EvalTrialResumeIndex | None,
) -> int:
    if resume_index is None:
        completed_plan_hashes = {record.trial_plan_hash for record in records}
        return sum(plan.plan_hash not in completed_plan_hashes for plan in plans)
    plan_hashes = {plan.plan_hash for plan in plans}
    return sum(
        plan_hash in plan_hashes for plan_hash in resume_index.pending_trial_plan_hashes
    )


def _validate_manual_taxonomy_assignments(
    manual_taxonomy: Mapping[str, EvalFailureTaxonomyAssignment | Mapping[str, Any]],
) -> Mapping[str, EvalFailureTaxonomyAssignment]:
    assignments: dict[str, EvalFailureTaxonomyAssignment] = {}
    for key, assignment in manual_taxonomy.items():
        if not key.strip():
            raise ValueError("manual taxonomy assignment keys must be non-empty")
        assignments[key] = EvalFailureTaxonomyAssignment.model_validate(assignment)
    return _freeze_eval_report_mapping(assignments)


def _statistical_summaries(
    *,
    metrics: Sequence[EvalMetricValue],
    paired_comparisons: Sequence[EvalPairedComparison],
    usage: EvalBudgetUsageEstimate,
) -> tuple[EvalReportStatisticalSummary, ...]:
    paired_by_metric = {
        comparison.metric_id: comparison for comparison in paired_comparisons
    }
    summaries = [
        _metric_statistical_summary(
            metric,
            paired_comparison=paired_by_metric.get(metric.metric_id),
        )
        for metric in metrics
        if metric.metric_id
        in {
            EvalReportMetricId.VALID_COMPLETION,
            EvalReportMetricId.FALSE_CLOSURE,
            EvalReportMetricId.FALSE_BLOCKED,
            EvalReportMetricId.CAPABILITY_VIOLATION,
            EvalReportMetricId.PROVIDER_FAILURE,
            EvalReportMetricId.RUNTIME_FAILURE,
            EvalReportMetricId.INVALID_TRIAL,
        }
    ]
    summaries.append(
        EvalReportStatisticalSummary(
            metric_id=EvalReportMetricId.ESTIMATED_COST,
            raw_count=0,
            denominator_count=0,
            rate=0.0,
            distributions=(
                _distribution_summary(
                    "estimated_cost_usd",
                    (usage.estimated_spend_usd,),
                ),
            ),
            diagnostic="Cost summaries are descriptive and use source-present campaign usage only.",
            descriptive_only=True,
        )
    )
    summaries.append(
        EvalReportStatisticalSummary(
            metric_id=EvalReportMetricId.WALL_CLOCK_SECONDS,
            raw_count=0,
            denominator_count=0,
            rate=0.0,
            distributions=(
                _distribution_summary(
                    "wall_clock_seconds",
                    (float(usage.wall_clock_seconds),),
                ),
            ),
            diagnostic="Latency summaries are descriptive and use source-present campaign usage only.",
            descriptive_only=True,
        )
    )
    return tuple(summaries)


def _metric_statistical_summary(
    metric: EvalMetricValue,
    *,
    paired_comparison: EvalPairedComparison | None,
) -> EvalReportStatisticalSummary:
    interval = wilson_score_interval(metric.count, metric.denominator.count)
    paired_differences = (
        (paired_comparison.difference,) if paired_comparison is not None else ()
    )
    if interval is None:
        return EvalReportStatisticalSummary(
            metric_id=metric.metric_id,
            raw_count=metric.count,
            denominator_count=metric.denominator.count,
            rate=metric.rate,
            paired_differences=paired_differences,
            diagnostic=(
                "Small-N pilot diagnostic only; Wilson interval omitted until "
                "the eligible denominator is at least 30."
            ),
            descriptive_only=True,
        )
    return EvalReportStatisticalSummary(
        metric_id=metric.metric_id,
        raw_count=metric.count,
        denominator_count=metric.denominator.count,
        rate=metric.rate,
        wilson_interval=EvalWilsonScoreInterval(
            successes=metric.count,
            total=metric.denominator.count,
            confidence_level=0.95,
            lower=interval[0],
            upper=interval[1],
        ),
        paired_differences=paired_differences,
        diagnostic="Wilson score interval emitted for eligible descriptive counts.",
        descriptive_only=False,
    )


def _distribution_summary(
    statistic_id: str,
    values: Sequence[float],
) -> EvalDistributionSummary:
    raw_values = tuple(float(value) for value in values)
    if not raw_values:
        return EvalDistributionSummary(statistic_id=statistic_id, sample_count=0)
    ordered = tuple(sorted(raw_values))
    return EvalDistributionSummary(
        statistic_id=statistic_id,
        sample_count=len(raw_values),
        raw_values=raw_values,
        median=float(median(ordered)),
        p90=_nearest_rank_percentile(ordered, 0.90),
        p95=_nearest_rank_percentile(ordered, 0.95),
    )


def _nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    index = max(0, min(len(values) - 1, int(round(percentile * len(values) + 0.5)) - 1))
    return float(values[index])


def _taxonomy_summaries(
    records: Sequence[EvalTrialRecord],
    manual_taxonomy: Mapping[str, EvalFailureTaxonomyAssignment],
) -> tuple[EvalFailureTaxonomySummary, ...]:
    counts: Counter[EvalReportFailureTaxonomyCategory] = Counter()
    for assignment in manual_taxonomy.values():
        counts[assignment.primary_category] += 1
        counts.update(assignment.contributing_categories)
    for record in records:
        for result in record.arm_results:
            for label in result.scorer_result.failure_labels:
                category = _failure_label_to_report_category(label)
                if category is not None:
                    counts[category] += 1
    return tuple(
        EvalFailureTaxonomySummary(category=category, count=counts[category])
        for category in sorted(counts, key=lambda item: item.value)
    )


def _invalid_trial_summary(
    records: Sequence[EvalTrialRecord],
) -> EvalInvalidTrialSummary:
    explanations = tuple(
        explanation
        for record in records
        for explanation in record.invalid_trial_explanations.values()
    )
    invalid = sum(
        result.scorer_result.final_outcome is EvalTrialOutcome.INVALID_TRIAL
        for record in records
        for result in record.arm_results
    )
    appended = len(records) * 2
    return EvalInvalidTrialSummary(
        invalid_trial_count=invalid,
        appended_record_count=appended,
        invalid_trial_rate=0.0 if appended == 0 else invalid / appended,
        diagnostics=explanations,
    )


def _validate_report_inputs(
    campaign_manifest: EvalCampaignManifest,
    plans: Sequence[EvalTrialPlan],
    records: Sequence[EvalTrialRecord],
    resume_index: EvalTrialResumeIndex | None,
) -> None:
    if not plans:
        raise ValueError("reports require at least one plan")
    if any(
        plan.campaign_manifest.campaign_id != campaign_manifest.campaign_id
        for plan in plans
    ):
        raise ValueError("plan campaign ID must match campaign manifest")
    if any(
        plan.campaign_manifest.campaign_manifest_hash
        != campaign_manifest.campaign_manifest_hash
        for plan in plans
    ):
        raise ValueError("plan campaign hash must match campaign manifest")
    plan_by_id = {plan.trial_id: plan for plan in plans}
    if len(plan_by_id) != len(plans):
        raise ValueError("reports reject duplicate planned trial IDs")
    plan_hashes = {plan.plan_hash for plan in plans}
    for record in records:
        plan = plan_by_id.get(record.trial_id)
        if plan is None:
            raise ValueError("record trial_id must be present in plans")
        checks = {
            "campaign ID": record.campaign_id == campaign_manifest.campaign_id,
            "campaign hash": record.campaign_manifest_hash
            == campaign_manifest.campaign_manifest_hash,
            "trial ID": record.trial_id == plan.trial_id,
            "trial index": record.trial_index == plan.trial_index,
            "trial hash": record.trial_plan_hash == plan.plan_hash,
            "fixture ID": record.fixture_id == plan.fixture_instance.fixture_id,
            "fixture instance ID": record.fixture_instance_id
            == plan.fixture_instance.fixture_instance_id,
            "fixture hash": record.fixture_hash == plan.fixture_instance.fixture_hash,
            "fixture snapshot hash": record.fixture_snapshot_hash
            == plan.fixture_instance.fixture_snapshot_hash,
            "model hash": record.model_manifest_hash
            == campaign_manifest.model_manifest_hash,
            "workflow hash": record.workflow_graph_hash
            == campaign_manifest.workflow_graph_hash,
            "fixture pack hash": record.fixture_pack_hash
            == campaign_manifest.fixture_pack_hash,
        }
        for label, valid in checks.items():
            if not valid:
                raise ValueError(f"record {label} must match report inputs")
        if tuple(record.arm_order) != tuple(plan.arm_order):
            raise ValueError("record arm order must match campaign plan")
        arm_plans = {arm_plan.arm_id: arm_plan for arm_plan in plan.arm_plans}
        for result in record.arm_results:
            arm_plan = arm_plans.get(result.arm_id)
            if arm_plan is None:
                raise ValueError("record arm must be present in campaign plan")
            result_checks = {
                "trial ID": result.trial_id == plan.trial_id,
                "trial hash": result.trial_plan_hash == plan.plan_hash,
                "fixture ID": result.scorer_result.fixture_id
                == plan.fixture_instance.fixture_id,
                "scorer input trial ID": result.scorer_input.trial_id == plan.trial_id,
                "scorer input fixture hash": result.scorer_input.fixture_hash
                == plan.fixture_instance.fixture_hash,
                "arm": result.arm_id == arm_plan.arm_id,
            }
            for label, valid in result_checks.items():
                if not valid:
                    raise ValueError(f"scorer result {label} must match report inputs")
            if result.scorer_result.scorer_version != campaign_manifest.scorer_version:
                raise ValueError("scorer version must match campaign manifest")
    if resume_index is not None:
        if (
            resume_index.campaign_manifest_hash
            != campaign_manifest.campaign_manifest_hash
        ):
            raise ValueError("resume index campaign hash must match campaign manifest")
        record_hashes = {record.record_hash for record in records}
        completed_hashes = set(resume_index.completed_trial_record_hashes)
        pending_hashes = set(resume_index.pending_trial_plan_hashes)
        if not completed_hashes.issubset(record_hashes):
            raise ValueError("resume index completed records must be included")
        if not pending_hashes.issubset(plan_hashes):
            raise ValueError("resume index pending plans must be included")
        completed_plan_hashes = {record.trial_plan_hash for record in records}
        if completed_plan_hashes & pending_hashes:
            raise ValueError("resume index cannot mark completed plans as pending")
        if not completed_plan_hashes.issubset(plan_hashes):
            raise ValueError("resume index completed plans must be included")


def _report_input_hash(
    campaign_manifest: EvalCampaignManifest,
    plans: Sequence[EvalTrialPlan],
    records: Sequence[EvalTrialRecord],
    resume_index: EvalTrialResumeIndex | None,
) -> str:
    payload = {
        "campaign_manifest_hash": campaign_manifest.campaign_manifest_hash,
        "plan_hashes": tuple(plan.plan_hash for plan in plans),
        "record_hashes": tuple(record.record_hash for record in records),
        "resume_index_hash": resume_index.resume_index_hash
        if resume_index is not None
        else None,
    }
    return hashlib.sha256(canonical_eval_report_bytes(payload)).hexdigest()


def _threshold_status(
    metric: EvalMetricValue | None,
    threshold: float,
) -> EvalDecisionRuleStatus:
    if metric is None or metric.denominator.count < 30:
        return EvalDecisionRuleStatus.DESCRIPTIVE_ONLY
    return (
        EvalDecisionRuleStatus.PASSED
        if metric.rate <= threshold
        else EvalDecisionRuleStatus.FAILED
    )


def _budget_diagnostic(
    code: EvalBudgetDiagnosticCode,
    rule_id: str,
    summary: str,
) -> EvalBudgetDiagnostic:
    return EvalBudgetDiagnostic(diagnostic_code=code, rule_id=rule_id, summary=summary)


def _failure_label_to_report_category(
    label: EvalFailureTaxonomyLabel,
) -> EvalReportFailureTaxonomyCategory | None:
    return {
        EvalFailureTaxonomyLabel.REQUIRED_ARTIFACT_MISSING: (
            EvalReportFailureTaxonomyCategory.MISSING_ARTIFACT
        ),
        EvalFailureTaxonomyLabel.REQUIRED_ARTIFACT_MALFORMED: (
            EvalReportFailureTaxonomyCategory.INVALID_PATCH
        ),
        EvalFailureTaxonomyLabel.FALSE_SUCCESS_TERMINAL: (
            EvalReportFailureTaxonomyCategory.UNSUPPORTED_SUCCESS_CLAIM
        ),
        EvalFailureTaxonomyLabel.CAPABILITY_VIOLATION: (
            EvalReportFailureTaxonomyCategory.CAPABILITY_VIOLATION
        ),
        EvalFailureTaxonomyLabel.PROVIDER_DEFECT: (
            EvalReportFailureTaxonomyCategory.PROVIDER_FAILURE
        ),
        EvalFailureTaxonomyLabel.INFRASTRUCTURE_DEFECT: (
            EvalReportFailureTaxonomyCategory.INVALID_TRIAL_INFRASTRUCTURE
        ),
    }.get(label)


def _confound_summary(confound_id: EvalReportConfoundId) -> str:
    return {
        EvalReportConfoundId.PI_PROMPT_TOOL_BEHAVIOR: "Pi prompt and tool behavior can differ from Millforge.",
        EvalReportConfoundId.MILLFORGE_PROMPT_TOOL_BEHAVIOR: "Millforge prompt and tool behavior can affect outcomes.",
        EvalReportConfoundId.HARNESS_BEHAVIOR: "Harness behavior can affect trial setup, execution, and evidence capture.",
        EvalReportConfoundId.CONTEXT_PACKING: "Context packing choices can affect task evidence.",
        EvalReportConfoundId.PARSER_FALLBACK: "Parser fallback behavior can affect terminal interpretation.",
        EvalReportConfoundId.PROVIDER_NONDETERMINISM: "Provider nondeterminism can affect live results.",
        EvalReportConfoundId.RATE_LIMITING: "Rate limiting can affect latency and retry behavior.",
        EvalReportConfoundId.CACHED_PROVIDER_RESPONSES: "Cached provider responses can affect cost and latency.",
        EvalReportConfoundId.TOKEN_ACCOUNTING_DIFFERENCES: "Token accounting can differ across backends.",
        EvalReportConfoundId.SAMPLING_PARAMETER_MISMATCH: "Sampling-parameter mismatches can affect comparability.",
        EvalReportConfoundId.OFFLINE_FAKE_LIMITATIONS: "Offline fake execution cannot support model-performance claims.",
    }[confound_id]


def _freeze_eval_report_mapping(mapping: Mapping[Any, Any]) -> _FrozenEvalReportDict:
    return _FrozenEvalReportDict(mapping)


def _validate_sha256(value: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("value must be a lowercase sha256 digest")


def _reject_forbidden_material(value: Any, *, field_name: str | None = None) -> None:
    if field_name is not None:
        lowered = field_name.lower()
        if any(marker in lowered for marker in _SECRET_FIELD_MARKERS):
            raise ValueError("secret-like field names are forbidden in eval reports")
        if "endpoint" in lowered or "url" in lowered:
            raise ValueError("endpoint URLs are forbidden in eval reports")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_forbidden_material(item, field_name=str(key))
        return
    if isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            _reject_forbidden_material(item)
        return
    if isinstance(value, str):
        lowered = value.lower()
        if any(token in lowered for token in _DENIED_TEXT_TOKENS):
            raise ValueError("forbidden private material in eval report payload")
        if _ENDPOINT_URL.search(value):
            raise ValueError("endpoint URLs are forbidden in eval reports")
        if (
            _WINDOWS_ABSOLUTE_PATH.search(value)
            or _POSIX_ABSOLUTE_PATH.search(value)
            or _USER_HOME_PATH.search(value)
        ):
            raise ValueError("host absolute paths are forbidden in eval reports")
        if any(pattern.search(value) for pattern in _CREDENTIAL_VALUE_PATTERNS):
            raise ValueError("credential-like values are forbidden in eval reports")


__all__ = [
    "EVAL_REPORT_HASH_KIND",
    "EVAL_REPORT_JSON_HASH_KIND",
    "EVAL_REPORT_MARKDOWN_HASH_KIND",
    "EVAL_REPORT_SCHEMA_VERSION",
    "EvalArmSummary",
    "EvalBudgetDiagnostic",
    "EvalBudgetDiagnosticCode",
    "EvalBudgetUsageEstimate",
    "EvalBudgetValidationResult",
    "EvalCategorySummary",
    "EvalConfoundEntry",
    "EvalDecisionRuleKind",
    "EvalDecisionRule",
    "EvalDecisionRuleStatus",
    "EvalDistributionSummary",
    "EvalFailureTaxonomyAssignment",
    "EvalFailureTaxonomySummary",
    "EvalInvalidTrialSummary",
    "EvalLiveAdmissionDiagnostic",
    "EvalLiveAdmissionDiagnosticCode",
    "EvalLiveAdmissionResult",
    "EvalLiveAdmissionStatus",
    "EvalMarkdownReport",
    "EvalMetricDenominator",
    "EvalMetricDenominatorKind",
    "EvalMetricValue",
    "EvalPairedComparison",
    "EvalPromotionalFreeWindow",
    "EvalReportAbortThresholds",
    "EvalReportBudgetPolicy",
    "EvalReportConfoundId",
    "EvalReportContractModel",
    "EvalReportFailureTaxonomyCategory",
    "EvalReportMetricId",
    "EvalReportPayload",
    "EvalReportPricingClass",
    "EvalReportRateLimitPolicy",
    "EvalReportReproducibilityHashes",
    "EvalReportStatisticalSummary",
    "EvalTaskSummary",
    "EvalWilsonScoreInterval",
    "admit_eval_report_campaign",
    "build_eval_report_artifact_bytes",
    "build_eval_report_payload",
    "calculate_eval_report_hash",
    "calculate_eval_report_json_hash",
    "canonical_eval_markdown_report_bytes",
    "canonical_eval_report_bytes",
    "canonical_eval_report_json_bytes",
    "default_eval_report_budget_policy",
    "default_eval_report_confounds",
    "default_eval_report_decision_rules",
    "render_eval_markdown_report",
    "validate_eval_budget_policy",
    "wilson_score_interval",
]
