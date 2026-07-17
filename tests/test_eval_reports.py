"""Focused tests for the public 08C eval-report contract surface."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

import millforge
import millforge.eval_reports as eval_reports
from millforge.eval_reports import (
    EvalBudgetDiagnosticCode,
    EvalBudgetUsageEstimate,
    EvalDecisionRuleKind,
    EvalFailureTaxonomyAssignment,
    EvalLiveAdmissionDiagnosticCode,
    EvalLiveAdmissionStatus,
    EvalPromotionalFreeWindow,
    EvalReportBudgetPolicy,
    EvalReportFailureTaxonomyCategory,
    EvalReportMetricId,
    EvalReportConfoundId,
    EvalReportPricingClass,
    admit_eval_report_campaign,
    build_eval_report_artifact_bytes,
    build_eval_report_payload,
    calculate_eval_report_hash,
    canonical_eval_report_bytes,
    canonical_eval_report_json_bytes,
    default_eval_report_budget_policy,
    render_eval_markdown_report,
    validate_eval_budget_policy,
    wilson_score_interval,
)
from millforge.eval_suite import (
    EvalCampaignKind,
    EvalCampaignManifest,
    EvalFailureTaxonomyLabel,
    EvalSuiteExecutionMode,
    EvalTrialOutcome,
    calculate_eval_campaign_manifest_hash,
    default_eval_suite_campaign_manifest,
    load_eval_task_fixtures,
)
from millforge.eval_trials import (
    EvalFakeOutcomeScriptKind,
    EvalTrialFakeRunnerScript,
    EvalTrialModelUsageSummary,
    EvalTrialResourceSummary,
    append_eval_trial_record_to_campaign_store,
    default_eval_trial_plan,
    plan_paired_eval_trials,
    resume_eval_trial_campaign_store,
    run_offline_fake_eval_trial,
)
from millforge.eval_workflow import EvalStageId, EvalTerminalResult


def test_eval_reports_public_contracts_are_root_exports() -> None:
    for public_name in eval_reports.__all__:
        assert public_name in millforge.__all__
        assert getattr(millforge, public_name) is getattr(eval_reports, public_name)


def test_offline_fake_campaign_admits_only_zero_cost_bounded_policy() -> None:
    campaign = default_eval_suite_campaign_manifest()
    policy = default_eval_report_budget_policy()

    result = admit_eval_report_campaign(campaign, budget_policy=policy)

    assert result.status is EvalLiveAdmissionStatus.ADMITTED
    assert result.diagnostics == ()
    assert result.budget_result.valid is True

    denied = admit_eval_report_campaign(
        campaign,
        budget_policy=policy.model_copy(update={"max_model_calls": None}),
    )

    assert denied.status is EvalLiveAdmissionStatus.DENIED
    assert denied.budget_result.valid is False
    assert {
        diagnostic.diagnostic_code for diagnostic in denied.budget_result.diagnostics
    } >= {
        EvalBudgetDiagnosticCode.OFFLINE_POLICY_UNBOUNDED,
    }


def test_budget_validation_fails_closed_for_required_budget_gates() -> None:
    campaign = _live_campaign_manifest()
    policy = default_eval_report_budget_policy().model_copy(
        update={
            "pricing_class": EvalReportPricingClass.PROMOTIONAL_FREE_WINDOW,
            "max_spend_usd": 1.0,
            "max_prompt_tokens": None,
            "max_trials_per_campaign": None,
            "promotional_free_window": None,
            "rate_limit_policy_by_arm": {},
        }
    )

    result = validate_eval_budget_policy(
        policy,
        campaign_manifest=campaign,
        usage=EvalBudgetUsageEstimate(
            estimated_spend_usd=2.0,
            prompt_tokens=10,
            completion_tokens=1,
            model_calls=1,
            retries_per_trial=1,
            wall_clock_seconds=1,
            trial_count=1,
        ),
    )

    codes = {diagnostic.diagnostic_code for diagnostic in result.diagnostics}
    assert result.valid is False
    assert codes >= {
        EvalBudgetDiagnosticCode.MISSING_TOKEN_CEILING,
        EvalBudgetDiagnosticCode.MISSING_TRIAL_COUNT_CEILING,
        EvalBudgetDiagnosticCode.INCOMPLETE_PROMOTIONAL_FREE_WINDOW,
        EvalBudgetDiagnosticCode.UNFAIR_PAIRED_ARM_RATE_LIMIT,
        EvalBudgetDiagnosticCode.SPEND_CEILING_EXCEEDED,
    }


def test_free_tier_pricing_class_is_distinct_and_bounded_live_policy() -> None:
    assert EvalReportPricingClass.FREE_TIER.value == "free_tier"
    assert EvalReportPricingClass.FREE_TIER not in {
        EvalReportPricingClass.PROMOTIONAL_FREE_WINDOW,
        EvalReportPricingClass.PAID_PROVIDER,
        EvalReportPricingClass.LOCAL_METERED,
        EvalReportPricingClass.OFFLINE_ZERO_COST,
    }

    policy = default_eval_report_budget_policy().model_copy(
        update={
            "pricing_class": EvalReportPricingClass.FREE_TIER,
            "max_spend_usd": 0.0,
            "max_prompt_tokens": 10,
            "max_completion_tokens": 5,
            "max_model_calls": 2,
            "max_retries_per_trial": 1,
            "max_wall_clock_seconds": 30,
            "max_trials_per_campaign": 2,
            "promotional_free_window": None,
        }
    )

    result = validate_eval_budget_policy(
        policy,
        campaign_manifest=_live_campaign_manifest(),
        usage=EvalBudgetUsageEstimate(
            estimated_spend_usd=0.0,
            prompt_tokens=10,
            completion_tokens=5,
            model_calls=2,
            retries_per_trial=1,
            wall_clock_seconds=30,
            trial_count=2,
        ),
    )

    assert result.valid is True
    assert result.diagnostics == ()


def test_free_tier_policy_fails_closed_for_missing_or_exceeded_live_bounds() -> None:
    policy = default_eval_report_budget_policy().model_copy(
        update={
            "pricing_class": EvalReportPricingClass.FREE_TIER,
            "max_spend_usd": 0.0,
            "max_prompt_tokens": None,
            "max_completion_tokens": 5,
            "max_model_calls": None,
            "max_trials_per_campaign": None,
            "rate_limit_policy_by_arm": {},
            "promotional_free_window": None,
        }
    )

    result = validate_eval_budget_policy(
        policy,
        campaign_manifest=_live_campaign_manifest(),
        usage=EvalBudgetUsageEstimate(
            estimated_spend_usd=1.0,
            prompt_tokens=11,
            completion_tokens=6,
            model_calls=3,
            retries_per_trial=1,
            wall_clock_seconds=1,
            trial_count=3,
        ),
    )

    codes = {diagnostic.diagnostic_code for diagnostic in result.diagnostics}
    assert result.valid is False
    assert codes >= {
        EvalBudgetDiagnosticCode.MISSING_TOKEN_CEILING,
        EvalBudgetDiagnosticCode.MISSING_TRIAL_COUNT_CEILING,
        EvalBudgetDiagnosticCode.MISSING_LIVE_BUDGET_METADATA,
        EvalBudgetDiagnosticCode.UNFAIR_PAIRED_ARM_RATE_LIMIT,
        EvalBudgetDiagnosticCode.SPEND_CEILING_EXCEEDED,
        EvalBudgetDiagnosticCode.COMPLETION_TOKEN_CEILING_EXCEEDED,
        EvalBudgetDiagnosticCode.RETRY_CEILING_EXCEEDED,
        EvalBudgetDiagnosticCode.WALL_CLOCK_CEILING_EXCEEDED,
    }
    assert EvalBudgetDiagnosticCode.INCOMPLETE_PROMOTIONAL_FREE_WINDOW not in codes


def test_free_tier_policy_requires_retry_and_wall_clock_ceilings() -> None:
    policy = default_eval_report_budget_policy().model_copy(
        update={
            "pricing_class": EvalReportPricingClass.FREE_TIER,
            "max_spend_usd": 0.0,
            "max_prompt_tokens": 10,
            "max_completion_tokens": 5,
            "max_model_calls": 2,
            "max_retries_per_trial": None,
            "max_wall_clock_seconds": None,
            "max_trials_per_campaign": 2,
            "promotional_free_window": None,
        }
    )

    result = validate_eval_budget_policy(
        policy,
        campaign_manifest=_live_campaign_manifest(),
        usage=EvalBudgetUsageEstimate(
            estimated_spend_usd=0.0,
            prompt_tokens=0,
            completion_tokens=0,
            model_calls=0,
            retries_per_trial=999,
            wall_clock_seconds=999,
            trial_count=1,
        ),
    )

    codes = {diagnostic.diagnostic_code for diagnostic in result.diagnostics}
    assert result.valid is False
    assert EvalBudgetDiagnosticCode.MISSING_LIVE_BUDGET_METADATA in codes
    assert EvalBudgetDiagnosticCode.INCOMPLETE_PROMOTIONAL_FREE_WINDOW not in codes


def test_offline_fake_campaign_denies_free_tier_policy() -> None:
    result = admit_eval_report_campaign(
        default_eval_suite_campaign_manifest(),
        budget_policy=default_eval_report_budget_policy().model_copy(
            update={"pricing_class": EvalReportPricingClass.FREE_TIER}
        ),
    )

    assert result.status is EvalLiveAdmissionStatus.DENIED
    assert result.budget_result.valid is False
    assert {
        diagnostic.diagnostic_code for diagnostic in result.budget_result.diagnostics
    } >= {EvalBudgetDiagnosticCode.OFFLINE_POLICY_NOT_ZERO_COST}


def test_promotional_free_window_requires_complete_timestamp_metadata() -> None:
    with pytest.raises(ValidationError, match="UTC timestamp"):
        EvalPromotionalFreeWindow(
            window_id="window.1",
            source_label="provider_public_terms",
            starts_at="today",
            ends_at="tomorrow",
            max_free_usd=1.0,
            terms_summary="bounded provider credit",
        )


def test_live_admission_returns_all_unresolved_dependency_diagnostics() -> None:
    result = admit_eval_report_campaign(
        _live_campaign_manifest(),
        budget_policy=default_eval_report_budget_policy().model_copy(
            update={
                "pricing_class": EvalReportPricingClass.PAID_PROVIDER,
                "max_spend_usd": 10.0,
                "max_model_calls": 10,
            }
        ),
    )

    assert result.status is EvalLiveAdmissionStatus.DENIED
    assert {diagnostic.diagnostic_code for diagnostic in result.diagnostics} >= {
        EvalLiveAdmissionDiagnosticCode.PI_RUNTIME_UNAVAILABLE,
        EvalLiveAdmissionDiagnosticCode.MILLFORGE_LIVE_HARNESS_UNAVAILABLE,
        EvalLiveAdmissionDiagnosticCode.SHARED_BACKEND_CONFIGURATION_MISSING,
        EvalLiveAdmissionDiagnosticCode.FIXTURE_WORKSPACE_LIFECYCLE_UNAVAILABLE,
        EvalLiveAdmissionDiagnosticCode.RESOURCE_ENFORCEMENT_UNAVAILABLE,
        EvalLiveAdmissionDiagnosticCode.BUDGET_POLICY_INVALID,
        EvalLiveAdmissionDiagnosticCode.APPEND_ONLY_STORE_SAFETY_UNPROVEN,
        EvalLiveAdmissionDiagnosticCode.DETERMINISTIC_SCORER_UNAVAILABLE,
    }


def test_report_payload_metrics_hashes_and_markdown_are_deterministic() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08c.default.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    run = run_offline_fake_eval_trial(plan, fixture=fixture)
    policy = default_eval_report_budget_policy()

    first = build_eval_report_payload(
        report_id="report.08c.default.v1",
        campaign_manifest=plan.campaign_manifest,
        plans=(plan,),
        records=(run.trial_record,),
        budget_policy=policy,
    )
    second = build_eval_report_payload(
        report_id="report.08c.default.v1",
        campaign_manifest=plan.campaign_manifest,
        plans=(plan,),
        records=(run.trial_record,),
        budget_policy=policy,
    )

    assert first.report_hash == calculate_eval_report_hash(first)
    assert canonical_eval_report_bytes(first) == canonical_eval_report_bytes(second)
    assert canonical_eval_report_json_bytes(first) == canonical_eval_report_json_bytes(
        second
    )
    assert first.report_hash == second.report_hash
    report_json = json.loads(canonical_eval_report_json_bytes(first))
    assert {
        "campaign_id",
        "admission",
        "arms",
        "controlled_variables",
        "budget_policy",
        "budget_usage",
        "primary_metrics",
        "taxonomy_summaries",
        "invalid_trials",
        "confounds",
        "decision_rules",
        "reproducibility_hashes",
        "claim_boundaries",
    } <= set(report_json)
    metrics = {metric.metric_id: metric for metric in first.primary_metrics}
    assert metrics[EvalReportMetricId.VALID_COMPLETION].count == 2
    assert metrics[EvalReportMetricId.FALSE_CLOSURE].severity_one is True
    assert metrics[EvalReportMetricId.INVALID_TRIAL].denominator.count == 2
    assert first.paired_comparisons[0].paired_denominator == 1
    assert first.paired_comparisons[0].missing_pair_count == 0
    assert first.invalid_trials.invalid_trial_count == 0
    assert first.decision_rules
    assert first.confounds
    assert first.statistical_summaries

    markdown = render_eval_markdown_report(first)
    assert markdown.content_hash == render_eval_markdown_report(first).content_hash
    assert "## Controlled Variables" in markdown.content
    assert "## Budget Summary" in markdown.content
    assert "## Primary Metrics" in markdown.content
    assert "## Per-Category Summary" in markdown.content
    assert "## Decision Rules" in markdown.content
    assert "## Reproducibility" in markdown.content
    assert "No Pi-vs-Millforge model-performance conclusion can be drawn." in (
        markdown.content
    )
    assert "offline fake" in markdown.content.lower()


def test_report_artifact_bytes_are_deterministic_and_hygienic() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08c.artifacts.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    run = run_offline_fake_eval_trial(plan, fixture=fixture)
    payload = build_eval_report_payload(
        report_id="report.08c.artifacts.v1",
        campaign_manifest=plan.campaign_manifest,
        plans=(plan,),
        records=(run.trial_record,),
        budget_policy=default_eval_report_budget_policy(),
    )

    first = build_eval_report_artifact_bytes(payload)
    second = build_eval_report_artifact_bytes(payload)

    assert first == second
    assert set(first) == {"report.json", "report.md", "report.sha256"}
    assert first["report.json"] == canonical_eval_report_json_bytes(payload)
    assert (
        b'"report_hash":"' + payload.report_hash.encode("ascii") in first["report.json"]
    )
    assert b"# Eval Report report.08c.artifacts.v1\n" in first["report.md"]
    expected_json_hash_line = (
        f"eval_report_json_sha256_v1 "
        f"{hashlib.sha256(first['report.json']).hexdigest()}\n"
    ).encode("ascii")
    assert expected_json_hash_line in first["report.sha256"]
    serialized = b"\n".join(first.values()).decode("utf-8")
    forbidden_fragments = (
        "api_key",
        "authorization:",
        "bearer ",
        "endpoint_url",
        "https://",
        "/mnt/",
        "millrace-agents",
        "hidden expected",
        "expected output",
    )
    assert not any(fragment in serialized.lower() for fragment in forbidden_fragments)


def test_report_taxonomy_confounds_decision_rules_and_statistics_are_closed() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08c.taxonomy.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    run = run_offline_fake_eval_trial(plan, fixture=fixture)
    scorer_result = run.trial_record.arm_results[0].scorer_result.model_copy(
        update={
            "final_outcome": EvalTrialOutcome.FALSE_CLOSURE,
            "primary_success": False,
            "false_closure": True,
            "false_success": True,
            "artifact_complete": False,
            "missing_artifact_ids": ("builder_summary",),
            "failure_labels": (
                EvalFailureTaxonomyLabel.REQUIRED_ARTIFACT_MISSING,
                EvalFailureTaxonomyLabel.FALSE_SUCCESS_TERMINAL,
                EvalFailureTaxonomyLabel.CAPABILITY_VIOLATION,
                EvalFailureTaxonomyLabel.PROVIDER_DEFECT,
                EvalFailureTaxonomyLabel.INFRASTRUCTURE_DEFECT,
            ),
        }
    )
    arm_result = run.trial_record.arm_results[0].model_copy(
        update={"scorer_result": scorer_result}
    )
    record = run.trial_record.model_copy(
        update={
            "arm_results": (arm_result, run.trial_record.arm_results[1]),
            "final_outcomes": {
                **run.trial_record.final_outcomes,
                arm_result.arm_id: EvalTrialOutcome.FALSE_CLOSURE,
            },
        }
    )
    manual = {
        "review.1": {
            "primary_category": "test_not_run",
            "contributing_categories": ("tool_schema_failure",),
            "explanation": "manual review found test and tool issues",
            "category_explanations": {
                "test_not_run": "required check was not run",
                "tool_schema_failure": "tool schema failed during recovery",
            },
        }
    }

    payload = build_eval_report_payload(
        report_id="report.08c.taxonomy.v1",
        campaign_manifest=plan.campaign_manifest,
        plans=(plan,),
        records=(record,),
        budget_policy=default_eval_report_budget_policy(),
        manual_taxonomy=manual,
    )

    assert {category.value for category in EvalReportFailureTaxonomyCategory} == {
        "task_misunderstanding",
        "wrong_file",
        "unread_before_edit",
        "invalid_patch",
        "test_not_run",
        "test_misread",
        "missing_artifact",
        "unsupported_success_claim",
        "checker_evidence_failure",
        "arbiter_false_closure",
        "premature_terminal",
        "tool_schema_failure",
        "tool_recovery_failure",
        "context_loss",
        "budget_exhaustion",
        "provider_failure",
        "runner_failure",
        "capability_violation",
        "invalid_trial_infrastructure",
    }
    assert {entry.confound_id for entry in payload.confounds} == set(
        EvalReportConfoundId
    )
    assert {rule.rule_kind for rule in payload.decision_rules} == set(
        EvalDecisionRuleKind
    )

    taxonomy = {
        summary.category: summary.count for summary in payload.taxonomy_summaries
    }
    assert taxonomy[EvalReportFailureTaxonomyCategory.MISSING_ARTIFACT] == 1
    assert taxonomy[EvalReportFailureTaxonomyCategory.UNSUPPORTED_SUCCESS_CLAIM] == 1
    assert taxonomy[EvalReportFailureTaxonomyCategory.CAPABILITY_VIOLATION] == 1
    assert taxonomy[EvalReportFailureTaxonomyCategory.PROVIDER_FAILURE] == 1
    assert taxonomy[EvalReportFailureTaxonomyCategory.INVALID_TRIAL_INFRASTRUCTURE] == 1
    assert taxonomy[EvalReportFailureTaxonomyCategory.TEST_NOT_RUN] == 1
    assert taxonomy[EvalReportFailureTaxonomyCategory.TOOL_SCHEMA_FAILURE] == 1

    metrics = {metric.metric_id: metric for metric in payload.primary_metrics}
    assert metrics[EvalReportMetricId.FALSE_CLOSURE].count == 1
    assert metrics[EvalReportMetricId.VALID_COMPLETION].count == 1
    assert record.arm_results[1].scorer_result.primary_success is True
    valid_completion_stats = next(
        summary
        for summary in payload.statistical_summaries
        if summary.metric_id is EvalReportMetricId.VALID_COMPLETION
    )
    assert valid_completion_stats.raw_count == 1
    assert valid_completion_stats.paired_differences == (1,)
    assert valid_completion_stats.wilson_interval is None
    assert valid_completion_stats.descriptive_only is True
    cost_stats = next(
        summary
        for summary in payload.statistical_summaries
        if summary.metric_id is EvalReportMetricId.ESTIMATED_COST
    )
    assert cost_stats.distributions[0].median == 0.0


def test_report_aggregation_uses_plan_and_resume_index_denominators(
    tmp_path,
) -> None:
    fixtures = load_eval_task_fixtures()[:2]
    plans = plan_paired_eval_trials(
        fixtures=fixtures,
        fake_runner_script=_fake_script(),
        seed=42,
    )
    completed_run = run_offline_fake_eval_trial(plans[0], fixture=fixtures[0])
    append_result = append_eval_trial_record_to_campaign_store(
        tmp_path,
        plan=plans[0],
        plans=plans,
        record=completed_run.trial_record,
    )
    resume_result = resume_eval_trial_campaign_store(
        tmp_path,
        plan=plans[0],
        plans=plans,
    )

    assert resume_result.resume_index == append_result.resume_index
    assert resume_result.pending_trial_ids == (plans[1].trial_id,)

    payload = build_eval_report_payload(
        report_id="report.08c.denominators.v1",
        campaign_manifest=plans[0].campaign_manifest,
        plans=plans,
        records=(completed_run.trial_record,),
        resume_index=resume_result.resume_index,
        budget_policy=default_eval_report_budget_policy(),
    )

    metrics = {metric.metric_id: metric for metric in payload.primary_metrics}
    assert metrics[EvalReportMetricId.VALID_COMPLETION].count == 2
    assert metrics[EvalReportMetricId.VALID_COMPLETION].denominator.count == 2
    assert metrics[EvalReportMetricId.RUNTIME_FAILURE].denominator.count == 4
    assert metrics[EvalReportMetricId.PROVIDER_FAILURE].denominator.count == 4
    assert metrics[EvalReportMetricId.MISSING_PAIR].count == 1
    assert metrics[EvalReportMetricId.MISSING_PAIR].denominator.count == 2
    assert metrics[EvalReportMetricId.PENDING_TRIAL].count == 1
    assert metrics[EvalReportMetricId.INCOMPLETE_TRIAL].count == 1
    assert payload.paired_comparisons[0].paired_denominator == 1
    assert payload.paired_comparisons[0].missing_pair_count == 1

    arm_metrics = {
        summary.arm_id: {metric.metric_id: metric for metric in summary.metrics}
        for summary in payload.arm_summaries
    }
    for summary in payload.arm_summaries:
        valid_completion = arm_metrics[summary.arm_id][
            EvalReportMetricId.VALID_COMPLETION
        ]
        runtime_failure = arm_metrics[summary.arm_id][
            EvalReportMetricId.RUNTIME_FAILURE
        ]
        assert valid_completion.count == 1
        assert valid_completion.denominator.count == 1
        assert runtime_failure.denominator.count == 2

    pending_task = next(
        summary
        for summary in payload.task_summaries
        if summary.fixture_id == plans[1].fixture_instance.fixture_id
    )
    pending_metrics = {metric.metric_id: metric for metric in pending_task.metrics}
    assert pending_metrics[EvalReportMetricId.INCOMPLETE_TRIAL].count == 1
    assert pending_metrics[EvalReportMetricId.INCOMPLETE_TRIAL].denominator.count == 1


def test_report_aggregation_includes_source_present_model_usage_metrics() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08c.usage.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    run = run_offline_fake_eval_trial(plan, fixture=fixture)
    record = run.trial_record.model_copy(
        update={
            "model_usage_summary": EvalTrialModelUsageSummary.model_construct(
                zero_model_usage=False,
                input_tokens=123,
                output_tokens=45,
                model_call_count=3,
            )
        }
    )

    payload = build_eval_report_payload(
        report_id="report.08c.usage.v1",
        campaign_manifest=plan.campaign_manifest,
        plans=(plan,),
        records=(record,),
        budget_policy=default_eval_report_budget_policy(),
    )

    metrics = {metric.metric_id: metric for metric in payload.primary_metrics}
    assert metrics[EvalReportMetricId.MODEL_CALLS].value == 3.0
    assert metrics[EvalReportMetricId.MODEL_CALLS].count == 1
    assert metrics[EvalReportMetricId.MODEL_CALLS].denominator.count == 1
    assert metrics[EvalReportMetricId.PROMPT_TOKENS].value == 123.0
    assert metrics[EvalReportMetricId.COMPLETION_TOKENS].value == 45.0

    task_metrics = {
        metric.metric_id: metric for metric in payload.task_summaries[0].metrics
    }
    assert task_metrics[EvalReportMetricId.MODEL_CALLS].value == 3.0
    assert task_metrics[EvalReportMetricId.PROMPT_TOKENS].value == 123.0
    assert task_metrics[EvalReportMetricId.COMPLETION_TOKENS].value == 45.0

    category_metrics = {
        metric.metric_id: metric for metric in payload.category_summaries[0].metrics
    }
    assert category_metrics[EvalReportMetricId.MODEL_CALLS].value == 3.0
    assert category_metrics[EvalReportMetricId.PROMPT_TOKENS].value == 123.0
    assert category_metrics[EvalReportMetricId.COMPLETION_TOKENS].value == 45.0


def test_report_aggregation_includes_source_present_budget_model_usage() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08c.budget-usage.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    run = run_offline_fake_eval_trial(plan, fixture=fixture)

    payload = build_eval_report_payload(
        report_id="report.08c.budget-usage.v1",
        campaign_manifest=plan.campaign_manifest,
        plans=(plan,),
        records=(run.trial_record,),
        budget_policy=default_eval_report_budget_policy(),
        usage=EvalBudgetUsageEstimate(
            estimated_spend_usd=0.5,
            prompt_tokens=10,
            completion_tokens=5,
            model_calls=1,
            retries_per_trial=2,
            wall_clock_seconds=40,
            trial_count=1,
        ),
    )

    metric_ids = [metric.metric_id for metric in payload.primary_metrics]
    assert len(metric_ids) == len(set(metric_ids))
    metrics = {metric.metric_id: metric for metric in payload.primary_metrics}
    assert run.trial_record.model_usage_summary.model_call_count == 0
    assert metrics[EvalReportMetricId.MODEL_CALLS].value == 1.0
    assert metrics[EvalReportMetricId.PROMPT_TOKENS].value == 10.0
    assert metrics[EvalReportMetricId.COMPLETION_TOKENS].value == 5.0
    assert metrics[EvalReportMetricId.MODEL_CALLS].denominator.count == 1

    markdown = render_eval_markdown_report(payload)
    assert "- model_calls: 1.000000 across 1/1 source records" in markdown.content
    assert "- prompt_tokens: 10.000000 across 1/1 source records" in markdown.content
    assert "- completion_tokens: 5.000000 across 1/1 source records" in markdown.content


def test_report_aggregation_includes_source_present_resource_metrics() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08c.resource-usage.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    run = run_offline_fake_eval_trial(plan, fixture=fixture)
    record = run.trial_record.model_copy(
        update={
            "resource_summary": EvalTrialResourceSummary(
                artifact_count=2,
                artifact_bytes=2048,
                turn_count=4,
                invalid_tool_call_count=1,
                malformed_argument_count=2,
                prerequisite_violation_count=1,
                premature_terminal_count=1,
                tool_recovery_count=3,
                resource_artifact_hashes=(
                    run.trial_record.resource_summary.resource_artifact_hashes[:2]
                ),
            )
        }
    )

    payload = build_eval_report_payload(
        report_id="report.08c.resource-usage.v1",
        campaign_manifest=plan.campaign_manifest,
        plans=(plan,),
        records=(record,),
        budget_policy=default_eval_report_budget_policy(),
        usage=EvalBudgetUsageEstimate(
            estimated_spend_usd=0.25,
            prompt_tokens=10,
            completion_tokens=5,
            model_calls=1,
            retries_per_trial=2,
            wall_clock_seconds=30,
            trial_count=1,
        ),
    )

    metrics = {metric.metric_id: metric for metric in payload.primary_metrics}
    assert metrics[EvalReportMetricId.ESTIMATED_COST].value == 0.25
    assert metrics[EvalReportMetricId.WALL_CLOCK_SECONDS].value == 30.0
    assert metrics[EvalReportMetricId.RETRIES].value == 2.0
    assert metrics[EvalReportMetricId.ESTIMATED_COST].denominator.count == 1
    assert metrics[EvalReportMetricId.ARTIFACT_COUNT].value == 2.0
    assert metrics[EvalReportMetricId.ARTIFACT_BYTES].value == 2048.0
    assert metrics[EvalReportMetricId.ARTIFACT_BYTES].count == 1
    assert metrics[EvalReportMetricId.ARTIFACT_BYTES].denominator.count == 1
    assert metrics[EvalReportMetricId.TURNS].value == 4.0
    assert metrics[EvalReportMetricId.INVALID_TOOL_CALLS].value == 1.0
    assert metrics[EvalReportMetricId.MALFORMED_ARGUMENTS].value == 2.0
    assert metrics[EvalReportMetricId.PREREQUISITE_VIOLATIONS].value == 1.0
    assert metrics[EvalReportMetricId.PREMATURE_TERMINALS].value == 1.0
    assert metrics[EvalReportMetricId.TOOL_RECOVERIES].value == 3.0

    task_metrics = {
        metric.metric_id: metric for metric in payload.task_summaries[0].metrics
    }
    assert task_metrics[EvalReportMetricId.ARTIFACT_BYTES].value == 2048.0
    assert task_metrics[EvalReportMetricId.TOOL_RECOVERIES].value == 3.0

    category_metrics = {
        metric.metric_id: metric for metric in payload.category_summaries[0].metrics
    }
    assert category_metrics[EvalReportMetricId.ARTIFACT_BYTES].value == 2048.0
    assert category_metrics[EvalReportMetricId.TOOL_RECOVERIES].value == 3.0

    zero_metric_record = run.trial_record.model_copy(
        update={"resource_summary": EvalTrialResourceSummary(artifact_count=0)}
    )
    zero_payload = build_eval_report_payload(
        report_id="report.08c.resource-zero.v1",
        campaign_manifest=plan.campaign_manifest,
        plans=(plan,),
        records=(zero_metric_record,),
        budget_policy=default_eval_report_budget_policy(),
    )
    markdown = render_eval_markdown_report(zero_payload)
    assert "## Secondary Metrics" in markdown.content
    assert "- artifact_bytes: 0.000000 across 1/1 source records" in markdown.content
    assert "- turns: 0.000000 across 1/1 source records" in markdown.content
    assert canonical_eval_report_json_bytes(zero_payload) == (
        canonical_eval_report_json_bytes(zero_payload)
    )


def test_report_rejects_mismatched_record_inputs() -> None:
    fixture = load_eval_task_fixtures()[0]
    plan = default_eval_trial_plan(
        trial_plan_id="trial.plan.08c.mismatch.v1",
        fixture=fixture,
        fake_runner_script=_fake_script(),
    )
    run = run_offline_fake_eval_trial(plan, fixture=fixture)

    with pytest.raises(ValueError, match="campaign ID"):
        build_eval_report_payload(
            report_id="report.08c.mismatch.v1",
            campaign_manifest=plan.campaign_manifest,
            plans=(plan,),
            records=(run.trial_record.model_copy(update={"campaign_id": "wrong"}),),
            budget_policy=default_eval_report_budget_policy(),
        )


def test_public_report_contracts_reject_private_material() -> None:
    policy = default_eval_report_budget_policy()

    with pytest.raises(ValidationError, match="forbidden|host absolute paths") as (
        private_path_error
    ):
        EvalReportBudgetPolicy.model_validate(
            policy.model_dump(mode="json")
            | {"summary": "open /mnt/f/private hidden expected output"}
        )

    with pytest.raises(ValidationError, match="endpoint URLs|forbidden") as (
        endpoint_error
    ):
        EvalReportBudgetPolicy.model_validate(
            policy.model_dump(mode="json") | {"summary": "see https://example.test"}
        )

    with pytest.raises(ValidationError, match="secret-like") as secret_error:
        EvalReportBudgetPolicy.model_validate(
            policy.model_dump(mode="json") | {"api_key": "sk-live-abc1234567890"}
        )

    exception_text = "\n".join(
        str(error.value) for error in (private_path_error, endpoint_error, secret_error)
    ).lower()
    leaked_fragments = (
        "/mnt/f/private",
        "https://example.test",
        "hidden expected",
        "expected output",
        "sk-live-abc1234567890",
    )
    assert not any(fragment in exception_text for fragment in leaked_fragments)


def test_manual_taxonomy_requires_explanation_and_small_sample_stats_are_bounded() -> (
    None
):
    with pytest.raises(ValidationError, match="explanation"):
        EvalFailureTaxonomyAssignment(
            primary_category=EvalReportFailureTaxonomyCategory.TEST_NOT_RUN,
            explanation="",
        )

    with pytest.raises(ValidationError, match="category requires"):
        EvalFailureTaxonomyAssignment(
            primary_category=EvalReportFailureTaxonomyCategory.TEST_NOT_RUN,
            contributing_categories=(
                EvalReportFailureTaxonomyCategory.TOOL_SCHEMA_FAILURE,
            ),
            explanation="manual review",
            category_explanations={
                EvalReportFailureTaxonomyCategory.TEST_NOT_RUN: "not run",
                EvalReportFailureTaxonomyCategory.TOOL_SCHEMA_FAILURE: "",
            },
        )

    with pytest.raises(ValidationError, match="category"):
        EvalFailureTaxonomyAssignment.model_validate(
            {
                "primary_category": "unknown",
                "explanation": "manual review",
            }
        )

    assert wilson_score_interval(1, 2) is None
    interval = wilson_score_interval(15, 30)
    assert interval is not None
    assert interval[0] < 0.5 < interval[1]


def _live_campaign_manifest() -> EvalCampaignManifest:
    base = default_eval_suite_campaign_manifest()
    manifest = EvalCampaignManifest.model_construct(
        **(
            base.model_dump(mode="python")
            | {
                "campaign_kind": EvalCampaignKind.LOCAL_OPENAI_COMPATIBLE,
                "execution_mode": EvalSuiteExecutionMode.LIVE_RUNNER,
                "live_execution_admitted": False,
                "live_denial_diagnostics": (),
                "campaign_manifest_hash": "0" * 64,
            }
        )
    )
    payload = manifest.model_dump(mode="json")
    payload["campaign_manifest_hash"] = calculate_eval_campaign_manifest_hash(manifest)
    return EvalCampaignManifest.model_construct(**payload)


def _fake_script() -> EvalTrialFakeRunnerScript:
    return EvalTrialFakeRunnerScript(
        script_id="fake.valid_completion.08c.v1",
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
