"""Focused tests for the runtime-consumed 02B contract models."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from millforge.compiled_plan import (
    ArgumentMatch,
    CompilerIdentity,
    CompiledArtifactPolicy,
    CompiledBudgetPolicy,
    CompiledContextPolicy,
    CompiledHarnessNode,
    CompiledHarnessPlan,
    CompiledModelProfile,
    CompiledPrerequisite,
    CompiledPromptPolicy,
    IdempotencyClass,
    SessionEvent,
    SessionEventType,
    SideEffectCertainty,
    SideEffectClass,
    TerminalArtifactRequirement,
    ToolBindingRef,
    ToolTraceDecision,
    ToolTraceDecisionRecord,
    ToolTraceIdempotency,
    ToolTraceSideEffectClass,
    ToolExecutionStatus,
    ToolTraceRecord,
    canonical_json_serialize,
    parse_and_strip_compiled_plan,
    verify_compiled_plan_sha256,
)
from millforge.contracts import (
    ArtifactRef,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    Deadline,
    DiagnosticField,
    DiagnosticMetadata,
    ExecutionResultClass,
    ExecutionStatus,
    HarnessExecutionResult,
    StageIdentity,
    TerminalIntent,
    TimingMetadata,
    TokenUsage,
    UsageMetadata,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _binding(tool_id: str = "tool.weather") -> ToolBindingRef:
    return ToolBindingRef(
        tool_id=tool_id,
        tool_version=1,
        descriptor_sha256=SHA_A,
        implementation_id="impl.weather.v1",
    )


def _node(
    node_id: str = "terminal",
    *,
    terminal_result: str | None = "success",
    required: bool = False,
    model_tool_name: str = "complete_success",
    produced_artifact_ids: tuple[str, ...] = ("summary",),
    prerequisites: tuple[CompiledPrerequisite, ...] = (),
) -> CompiledHarnessNode:
    return CompiledHarnessNode(
        node_id=node_id,
        model_tool_name=model_tool_name,
        description="Complete the stage",
        input_schema={"type": "object", "properties": {}},
        binding=_binding(),
        prerequisites=prerequisites,
        required=required,
        terminal_result=terminal_result,
        required_capabilities=("workspace.read",),
        produced_artifact_ids=produced_artifact_ids,
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    )


def _plan(compiled_sha256: str = SHA_B) -> CompiledHarnessPlan:
    return CompiledHarnessPlan(
        schema_version="1.0",
        kind="compiled_millforge_harness",
        harness_id="harness-1",
        harness_version=1,
        source_sha256=SHA_A,
        compiled_sha256=compiled_sha256,
        stage_kind_ids=("builder",),
        model_profile=CompiledModelProfile(profile_id="standard"),
        prompt_policy=CompiledPromptPolicy(
            policy_id="policy-1",
            system_instructions="Follow the stage contract.",
            include_request_context=True,
        ),
        budgets=CompiledBudgetPolicy(
            max_iterations=1,
            max_validation_retries=1,
            max_tool_errors=1,
            max_prerequisite_violations=1,
            max_premature_terminal_attempts=1,
        ),
        context_policy=CompiledContextPolicy(
            strategy_id="forge.tiered.v1",
            budget_tokens=4096,
            keep_recent_iterations=1,
            phase_thresholds=(0.25, 0.5, 1.0),
        ),
        nodes=(_node(),),
        required_capabilities=("workspace.read",),
        terminal_result_map={"terminal": "success"},
        artifact_policy=CompiledArtifactPolicy(
            declared_artifact_ids=("summary",),
            required_by_terminal=(
                TerminalArtifactRequirement(
                    terminal_result="success", artifact_ids=("summary",)
                ),
            ),
        ),
        compiler_identity=CompilerIdentity(
            name="millforge-test", version="1.0.0", build_id="test-build"
        ),
    )


def _plan_with_valid_hash() -> tuple[CompiledHarnessPlan, str]:
    body = _plan(compiled_sha256=SHA_B).model_dump(mode="json")
    body.pop("compiled_sha256")
    digest = hashlib.sha256(canonical_json_serialize(body).encode("utf-8")).hexdigest()
    return _plan(compiled_sha256=digest), digest


def _compiled_ref() -> CompiledHarnessRef:
    return CompiledHarnessRef(
        identity=CompiledHarnessIdentity(
            compiled_plan_id="compiled-1",
            harness_id="harness-1",
            harness_version=1,
        ),
        path=Path("/tmp/compiled-1.json"),
        expected_hash=CompiledHarnessHash(algorithm="sha256", digest=SHA_A),
    )


def _stage() -> StageIdentity:
    return StageIdentity(plane="execution", node_id="builder", stage_kind_id="builder")


def test_compiled_harness_plan_round_trip_and_hash_verification() -> None:
    plan, digest = _plan_with_valid_hash()
    raw = plan.model_dump_json()

    restored, stripped = parse_and_strip_compiled_plan(raw)
    assert restored == plan
    assert "compiled_sha256" not in stripped

    verified, computed, warnings, result = verify_compiled_plan_sha256(
        raw,
        expected_compiled_hash=digest,
        expected_harness_id="harness-1",
        expected_harness_version=1,
    )
    assert verified is True
    assert computed == digest
    assert warnings == []
    assert result == plan


def test_compiled_plan_rejects_off_contract_aliases() -> None:
    payload = _plan().model_dump(mode="json")
    payload["plan_id"] = "old"
    with pytest.raises(ValidationError, match="extra"):
        CompiledHarnessPlan.model_validate(payload)

    with pytest.raises(ValidationError):
        CompiledModelProfile(profile_id="standard", model_id="gpt-4")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ToolBindingRef(tool_name="weather", binding_ref="latest")  # type: ignore[call-arg]


def test_compiled_plan_invariants_reject_invalid_references() -> None:
    terminal = _node(produced_artifact_ids=())
    with pytest.raises(ValidationError, match="no producer"):
        CompiledHarnessPlan(
            **{
                **_plan().model_dump(),
                "nodes": (terminal,),
            }
        )

    with pytest.raises(ValidationError, match="Terminal node"):
        CompiledHarnessPlan(
            **{
                **_plan().model_dump(),
                "nodes": (_node(required=True),),
            }
        )

    with pytest.raises(ValidationError, match="unknown node_id"):
        CompiledHarnessPlan(
            **{
                **_plan().model_dump(),
                "nodes": (
                    _node(
                        terminal_result=None,
                        required=True,
                        prerequisites=(
                            CompiledPrerequisite(
                                node_id="missing",
                                argument_matches=(
                                    ArgumentMatch(
                                        prerequisite_argument="result",
                                        current_argument="input",
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
                "terminal_result_map": {},
                "artifact_policy": CompiledArtifactPolicy(),
            }
        )


def test_deadline_remaining_and_closed_source() -> None:
    deadline = Deadline(
        started_monotonic=10.0,
        outer_deadline_monotonic=30.0,
        effective_deadline_monotonic=25.0,
        source="request_and_harness",
    )
    assert deadline.remaining(lambda: 15.0) == 10.0
    assert deadline.remaining(lambda: 40.0) == 0.0
    with pytest.raises(ValidationError):
        Deadline.model_validate(
            {
                "started_monotonic": 10.0,
                "outer_deadline_monotonic": 30.0,
                "effective_deadline_monotonic": 25.0,
                "source": "manual",
            }
        )


def test_diagnostic_metadata_and_token_usage_are_closed() -> None:
    token_usage = TokenUsage(
        input_tokens=2,
        output_tokens=3,
        total_tokens=5,
        provider_reported=True,
    )
    assert token_usage.total_tokens == 5

    with pytest.raises(ValidationError):
        TokenUsage(
            input_tokens=2,
            output_tokens=3,
            total_tokens=6,
            provider_reported=True,
        )
    with pytest.raises(ValidationError):
        TokenUsage.model_validate(
            {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
                "provider_reported": True,
                "reasoning_tokens": 1,
            }
        )

    diagnostic = DiagnosticMetadata(
        error_code="E_BINDING",
        category="binding",
        message="Binding was rejected",
        retryable=False,
        origin="runtime",
        fields=(DiagnosticField(key="node_id", value="n1"),),
    )
    assert diagnostic.category == "binding"
    with pytest.raises(ValidationError, match="unique"):
        DiagnosticMetadata(
            error_code="E_DUP",
            category="internal",
            message="Duplicate fields",
            retryable=False,
            origin="runtime",
            fields=(
                DiagnosticField(key="same", value=1),
                DiagnosticField(key="same", value=2),
            ),
        )


def test_usage_metadata_rejects_top_level_token_counters() -> None:
    usage = UsageMetadata(
        model_calls=1,
        tool_calls=2,
        token_usage=TokenUsage(
            input_tokens=2,
            output_tokens=3,
            total_tokens=5,
            provider_reported=True,
        ),
    )
    assert usage.model_calls == 1
    assert usage.tool_calls == 2
    assert usage.token_usage is not None
    assert usage.token_usage.total_tokens == 5

    usage_without_provider_tokens = UsageMetadata(
        model_calls=0,
        tool_calls=0,
        token_usage=None,
    )
    assert usage_without_provider_tokens.token_usage is None

    with pytest.raises(ValidationError):
        UsageMetadata.model_validate(
            {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
                "model_calls": 1,
                "tool_calls": 2,
                "token_usage": None,
            }
        )


def test_session_event_and_tool_trace_round_trip() -> None:
    stage = _stage()
    event = SessionEvent(
        schema_version="1.0",
        sequence=1,
        occurred_at="2026-06-12T00:00:00Z",
        monotonic_offset_ms=10.5,
        event_type=SessionEventType.RUNTIME_VERIFIED,
        request_id="req-1",
        run_id="run-1",
        session_id="sess-1",
        stage=stage,
        node_id="terminal",
        model_turn=0,
        tool_call_id="call-1",
        code=None,
        fields=(DiagnosticField(key="capability", value="workspace.read"),),
    )
    assert event.code is None
    assert event.stage == stage
    assert (
        SessionEvent.model_validate(event.model_dump()).model_dump()
        == event.model_dump()
    )

    trace = ToolTraceRecord(
        schema_version="1.0",
        sequence=2,
        occurred_at="2026-06-12T00:00:01Z",
        monotonic_offset_ms=20.5,
        request_id="req-1",
        run_id="run-1",
        session_id="sess-1",
        stage=stage,
        node_id="terminal",
        model_turn=0,
        tool_call_id="call-1",
        model_tool_name="complete_success",
        binding=_binding(),
        input_sha256=SHA_B,
        prerequisite_decisions=(
            ToolTraceDecisionRecord(key="prereq", decision=ToolTraceDecision.ALLOWED),
        ),
        capability_decisions=(
            ToolTraceDecisionRecord(
                key="workspace.read", decision=ToolTraceDecision.ALLOWED
            ),
        ),
        execution_status=ToolExecutionStatus.SUCCESS,
        retryable=False,
        side_effect_class=ToolTraceSideEffectClass.READ_ONLY,
        idempotency=ToolTraceIdempotency.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        output_sha256=SHA_C,
        duration_ms=2.0,
        summary="Tool completed",
    )
    assert trace.stage == stage
    assert (
        ToolTraceRecord.model_validate(trace.model_dump()).model_dump()
        == trace.model_dump()
    )
    with pytest.raises(ValidationError):
        ToolTraceRecord.model_validate({**trace.model_dump(), "trace_id": "old"})


def test_session_event_rejects_off_contract_shapes() -> None:
    event_payload = {
        "schema_version": "1.0",
        "sequence": 1,
        "occurred_at": "2026-06-12T00:00:00Z",
        "monotonic_offset_ms": 10.0,
        "event_type": SessionEventType.RUNTIME_VERIFIED,
        "request_id": "req-1",
        "run_id": "run-1",
        "session_id": "sess-1",
        "stage": _stage(),
        "node_id": "terminal",
        "model_turn": 0,
        "tool_call_id": "call-1",
        "code": None,
        "fields": (DiagnosticField(key="capability", value="workspace.read"),),
    }
    with pytest.raises(ValidationError):
        SessionEvent.model_validate({**event_payload, "sequence": 0})
    with pytest.raises(ValidationError):
        SessionEvent.model_validate({**event_payload, "stage": "builder"})
    with pytest.raises(ValidationError, match="extra"):
        SessionEvent.model_validate({**event_payload, "message": "off contract"})
    with pytest.raises(ValidationError, match="unique"):
        SessionEvent.model_validate(
            {
                **event_payload,
                "fields": (
                    DiagnosticField(key="duplicate", value=1),
                    DiagnosticField(key="duplicate", value=2),
                ),
            }
        )


def test_tool_trace_rejects_off_contract_shapes() -> None:
    trace_payload = {
        "schema_version": "1.0",
        "sequence": 1,
        "occurred_at": "2026-06-12T00:00:01Z",
        "monotonic_offset_ms": 20.0,
        "request_id": "req-1",
        "run_id": "run-1",
        "session_id": "sess-1",
        "stage": _stage(),
        "node_id": "terminal",
        "model_turn": 0,
        "tool_call_id": "call-1",
        "model_tool_name": "complete_success",
        "binding": _binding(),
        "input_sha256": SHA_B,
        "prerequisite_decisions": (
            ToolTraceDecisionRecord(key="prereq", decision=ToolTraceDecision.ALLOWED),
        ),
        "capability_decisions": (
            ToolTraceDecisionRecord(
                key="workspace.read", decision=ToolTraceDecision.ALLOWED
            ),
        ),
        "execution_status": ToolExecutionStatus.SUCCESS,
        "retryable": False,
        "side_effect_class": ToolTraceSideEffectClass.READ_ONLY,
        "idempotency": ToolTraceIdempotency.IDEMPOTENT,
        "side_effect_certainty": SideEffectCertainty.CONFIRMED_COMPLETE,
        "output_sha256": SHA_C,
        "duration_ms": 2.0,
        "summary": "Tool completed",
    }
    with pytest.raises(ValidationError):
        ToolTraceRecord.model_validate({**trace_payload, "sequence": 0})
    with pytest.raises(ValidationError):
        ToolTraceRecord.model_validate({**trace_payload, "stage": "builder"})
    with pytest.raises(ValidationError, match="unique"):
        ToolTraceRecord.model_validate(
            {
                **trace_payload,
                "capability_decisions": (
                    ToolTraceDecisionRecord(
                        key="duplicate", decision=ToolTraceDecision.ALLOWED
                    ),
                    ToolTraceDecisionRecord(
                        key="duplicate", decision=ToolTraceDecision.DENIED
                    ),
                ),
            }
        )


def test_session_event_and_tool_trace_closed_values() -> None:
    assert {item.value for item in SessionEventType} == {
        "runtime_received",
        "runtime_verified",
        "backend_constructed",
        "binding_rejected",
        "compiled_harness_invalid",
        "backend_failed",
        "session_started",
        "workflow_constructed",
        "model_request_started",
        "model_request_completed",
        "model_request_failed",
        "correction_issued",
        "premature_terminal_rejected",
        "prerequisite_rejected",
        "tool_started",
        "tool_completed",
        "tool_failed",
        "context_compacted",
        "terminal_intent_accepted",
        "terminal_intent_rejected",
        "finalization_started",
        "finalization_completed",
        "finalization_failed",
        "budget_exhausted",
        "timed_out",
        "cancelled",
        "internal_failed",
    }
    assert {item.value for item in ToolExecutionStatus} == {
        "not_executed",
        "success",
        "soft_failure",
        "hard_failure",
        "cancelled",
        "timed_out",
        "ambiguous",
    }


def test_compiled_harness_node_enum_values_are_closed() -> None:
    assert CompiledHarnessNode.model_fields["side_effect_class"].annotation is (
        SideEffectClass
    )
    assert CompiledHarnessNode.model_fields["idempotency"].annotation is (
        IdempotencyClass
    )
    assert {item.value for item in SideEffectClass} == {
        "read_only",
        "artifact_write",
        "workspace_write",
        "process_execution",
        "network_read",
        "network_write",
        "terminal",
    }
    assert {item.value for item in IdempotencyClass} == {
        "idempotent",
        "idempotent_with_key",
        "non_idempotent",
        "unknown",
    }


def test_tool_trace_enum_values_are_separate_from_compiled_node_enums() -> None:
    assert ToolTraceRecord.model_fields["side_effect_class"].annotation is (
        ToolTraceSideEffectClass
    )
    assert (
        ToolTraceRecord.model_fields["idempotency"].annotation is ToolTraceIdempotency
    )
    assert ToolTraceSideEffectClass is not SideEffectClass
    assert ToolTraceIdempotency is not IdempotencyClass
    assert {item.value for item in ToolTraceSideEffectClass} == {
        "read_only",
        "artifact_write",
        "workspace_write",
        "process_execution",
        "network_read",
        "network_write",
        "terminal",
    }
    assert {item.value for item in ToolTraceIdempotency} == {
        "idempotent",
        "idempotent_with_key",
        "non_idempotent",
        "unknown",
    }
    assert {item.value for item in SideEffectCertainty} == {
        "not_attempted",
        "confirmed_absent",
        "confirmed_complete",
        "rolled_back",
        "completion_unknown",
    }


def test_harness_execution_result_invariants() -> None:
    timing = TimingMetadata(started_at="start", completed_at="end", duration_ms=1.0)
    terminal = TerminalIntent(
        request_id="req-1",
        run_id="run-1",
        stage=_stage(),
        terminal_node_id="terminal",
        terminal_result="success",
        disposition="success",
        summary="Done",
        artifact_refs=(),
    )
    result = HarnessExecutionResult(
        status=ExecutionStatus.COMPLETED,
        result_class=ExecutionResultClass.DOMAIN_TERMINAL,
        request_id="req-1",
        run_id="run-1",
        stage=_stage(),
        terminal_intent=terminal,
        compiled_harness=_compiled_ref(),
        timing=timing,
    )
    assert result.terminal_intent == terminal

    with pytest.raises(ValidationError, match="status=completed"):
        HarnessExecutionResult(
            status=ExecutionStatus.COMPLETED,
            result_class=ExecutionResultClass.BACKEND_FAILURE,
            request_id="req-1",
            run_id="run-1",
            stage=_stage(),
            compiled_harness=_compiled_ref(),
            timing=timing,
        )

    with pytest.raises(ValidationError, match="terminal_intent"):
        HarnessExecutionResult(
            status=ExecutionStatus.FAILED,
            result_class=ExecutionResultClass.BACKEND_FAILURE,
            request_id="req-1",
            run_id="run-1",
            stage=_stage(),
            terminal_intent=terminal,
            compiled_harness=_compiled_ref(),
            timing=timing,
        )


def test_public_models_are_frozen_and_forbid_extras() -> None:
    frozen_models = [
        CompiledHarnessPlan,
        SessionEvent,
        ToolTraceRecord,
        Deadline,
        DiagnosticMetadata,
        HarnessExecutionResult,
    ]
    for model in frozen_models:
        config = getattr(model, "model_config")
        assert config["frozen"] is True
        assert config["extra"] == "forbid"

    with pytest.raises(ValidationError):
        ArtifactRef(artifact_id="a", path=Path("/tmp/a"), old_path="/tmp/old")  # type: ignore[call-arg]
