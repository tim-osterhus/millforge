"""Focused tests for the runtime-consumed 02B contract models."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast, get_args

import millforge
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
    CancellationRef,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    Deadline,
    DiagnosticField,
    DiagnosticMetadata,
    ExecutionResultClass,
    ExecutionStatus,
    HarnessExecutionResult,
    AssistantMessage,
    InvalidToolArguments,
    ModelCapabilityRequirements,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelToolDefinition,
    ModelToolCall,
    ParsedToolArguments,
    SamplingRequest,
    SanitizedMetadata,
    StageIdentity,
    TerminalIntent,
    TimingMetadata,
    TokenUsage,
    ToolExecutionResult,
    ToolResultMessage,
    UsageMetadata,
    ValidatedToolCall,
    UserMessage,
    SystemMessage,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _deadline() -> Deadline:
    return Deadline(
        started_monotonic=0.0,
        outer_deadline_monotonic=60.0,
        effective_deadline_monotonic=60.0,
        source="request",
    )


def _cancellation() -> CancellationRef:
    return CancellationRef(cancellation_id="cancel-1")


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


def test_bridge_model_capability_requirements_are_exact_02c_shape() -> None:
    requirements = ModelCapabilityRequirements()

    assert requirements.model_dump() == {
        "tool_calls": True,
        "parallel_tool_calls": False,
        "structured_output": False,
        "reasoning_controls": False,
        "usage_reporting": False,
        "system_messages": True,
        "tool_result_messages": True,
    }
    with pytest.raises(ValidationError):
        ModelCapabilityRequirements(parallel_tool_calls=True)  # type: ignore[arg-type]


def test_sampling_request_exposes_canonical_nullable_override_record() -> None:
    assert SamplingRequest().model_dump(mode="json") == {
        "temperature": None,
        "top_p": None,
        "presence_penalty": None,
        "frequency_penalty": None,
        "seed": None,
        "stop": None,
        "reasoning_mode": None,
        "reasoning_effort": None,
    }
    overrides = SamplingRequest(
        temperature=0.2,
        top_p=0.9,
        presence_penalty=0.1,
        frequency_penalty=0.0,
        seed=42,
        stop=("END",),
        reasoning_mode="disabled",
        reasoning_effort="low",
    )
    assert overrides.seed == 42
    with pytest.raises(ValidationError, match="extra"):
        SamplingRequest(max_tokens=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        SamplingRequest(stop=("",))


def test_model_completion_request_only_allows_positive_output_token_override() -> None:
    assert (
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(UserMessage(content="Hi"),),
            maximum_output_tokens_override=1,
            deadline=_deadline(),
            cancellation=_cancellation(),
        ).maximum_output_tokens_override
        == 1
    )
    with pytest.raises(ValidationError):
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(UserMessage(content="Hi"),),
            maximum_output_tokens_override=0,
            deadline=_deadline(),
            cancellation=_cancellation(),
        )


def test_sanitized_metadata_is_closed_and_bounded() -> None:
    metadata = SanitizedMetadata(values={"status": "ok", "attempt": 1})
    assert metadata.values["status"] == "ok"

    with pytest.raises(ValidationError, match="extra"):
        SanitizedMetadata(values={}, raw={"secret": "no"})  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="too many"):
        SanitizedMetadata(values={f"k{i}": i for i in range(33)})
    with pytest.raises(ValidationError, match="too long"):
        SanitizedMetadata(values={"k": "x" * 2049})


def test_model_completion_request_uses_typed_messages_tools_and_pairing() -> None:
    request = ModelCompletionRequest(
        request_id="req-1",
        run_id="run-1",
        model_profile_id="gpt-test",
        messages=(
            SystemMessage(content="Follow instructions."),
            UserMessage(content="Use the tool."),
            AssistantMessage(
                tool_calls=(
                    ModelToolCall(
                        call_id="call-1",
                        name="weather",
                        arguments=ParsedToolArguments(value={"city": "London"}),
                    ),
                ),
            ),
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="weather",
                content="Sunny",
            ),
        ),
        tools=(
            ModelToolDefinition(
                name="weather",
                description="Get weather",
                input_schema={"type": "object", "additionalProperties": False},
            ),
        ),
        deadline=_deadline(),
        cancellation=_cancellation(),
    )

    assert request.tools[0].name == "weather"
    assistant_message = cast(AssistantMessage, request.messages[2])
    arguments = assistant_message.tool_calls[0].arguments
    assert isinstance(arguments, ParsedToolArguments)
    assert arguments.value == {"city": "London"}
    dumped = request.model_dump(mode="json")
    assert list(dumped["messages"][0]) == ["role", "content"]
    assert dumped["messages"][0]["role"] == "system"
    assert dumped["messages"][1]["role"] == "user"
    assert dumped["messages"][2]["role"] == "assistant"
    assert dumped["messages"][3]["role"] == "tool"
    assert "kind" not in dumped["messages"][0]
    assert request.model_dump(mode="json")["messages"][3]["tool_name"] == "weather"

    with pytest.raises(ValidationError, match="union_tag_not_found|role"):
        ModelCompletionRequest.model_validate(
            {
                "request_id": "req-1",
                "run_id": "run-1",
                "model_profile_id": "gpt-test",
                "messages": ({"kind": "user", "content": "old"},),
                "deadline": _deadline().model_dump(mode="json"),
                "cancellation": _cancellation().model_dump(mode="json"),
            }
        )
    with pytest.raises(ValidationError, match="extra"):
        UserMessage.model_validate({"role": "user", "kind": "user", "content": "old"})

    with pytest.raises(ValidationError, match="tool_name"):
        ToolResultMessage(tool_call_id="call-1", content="Sunny")  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="tool_name"):
        ToolResultMessage(tool_call_id="call-1", tool_name=" ", content="Sunny")

    with pytest.raises(ValidationError, match="tool name"):
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(UserMessage(content="Hi"),),
            tools=(
                ModelToolDefinition(name="same", description="A", input_schema={}),
                ModelToolDefinition(name="same", description="B", input_schema={}),
            ),
            deadline=_deadline(),
            cancellation=_cancellation(),
        )
    with pytest.raises(ValidationError, match="unique"):
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(
                AssistantMessage(
                    tool_calls=(
                        ModelToolCall(
                            call_id="dup",
                            name="a",
                            arguments=ParsedToolArguments(),
                        ),
                        ModelToolCall(
                            call_id="dup",
                            name="b",
                            arguments=ParsedToolArguments(),
                        ),
                    ),
                ),
            ),
            deadline=_deadline(),
            cancellation=_cancellation(),
        )
    with pytest.raises(ValidationError, match="no matching"):
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(
                ToolResultMessage(
                    tool_call_id="missing",
                    tool_name="weather",
                    content="x",
                ),
            ),
            deadline=_deadline(),
            cancellation=_cancellation(),
        )
    with pytest.raises(ValidationError, match="tool_name"):
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(
                AssistantMessage(
                    tool_calls=(
                        ModelToolCall(
                            call_id="call-1",
                            name="weather",
                            arguments=ParsedToolArguments(),
                        ),
                    ),
                ),
                ToolResultMessage(
                    tool_call_id="call-1",
                    tool_name="calendar",
                    content="x",
                ),
            ),
            deadline=_deadline(),
            cancellation=_cancellation(),
        )


def test_public_bridge_records_expose_exact_canonical_json_shapes() -> None:
    invalid_arguments = InvalidToolArguments(raw="not-json", error_code="invalid_json")
    request = ModelCompletionRequest(
        request_id="req-1",
        run_id="run-1",
        model_profile_id="gpt-test",
        messages=(
            SystemMessage(content="Follow instructions."),
            UserMessage(content="Use the tool."),
            AssistantMessage(
                content=None,
                tool_calls=(
                    ModelToolCall(
                        call_id="call-1",
                        name="weather",
                        arguments=invalid_arguments,
                    ),
                ),
            ),
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="weather",
                content="Sunny",
            ),
        ),
        tools=(
            ModelToolDefinition(
                name="weather",
                description="Get weather",
                input_schema={"type": "object"},
            ),
        ),
        deadline=_deadline(),
        cancellation=_cancellation(),
    )

    assert SystemMessage(content="sys").model_dump(mode="json") == {
        "role": "system",
        "content": "sys",
    }
    assert UserMessage(content="hi").model_dump(mode="json") == {
        "role": "user",
        "content": "hi",
    }
    assert AssistantMessage(content="ok").model_dump(mode="json") == {
        "role": "assistant",
        "content": "ok",
        "tool_calls": [],
    }
    assert ToolResultMessage(
        tool_call_id="call-1",
        tool_name="weather",
        content="Sunny",
    ).model_dump(mode="json") == {
        "role": "tool",
        "tool_call_id": "call-1",
        "tool_name": "weather",
        "content": "Sunny",
    }
    assert ModelToolDefinition(
        name="weather",
        description="Get weather",
        input_schema={"type": "object"},
    ).model_dump(mode="json") == {
        "name": "weather",
        "description": "Get weather",
        "input_schema": {"type": "object"},
    }
    assert invalid_arguments.model_dump(mode="json") == {
        "kind": "invalid",
        "raw": "not-json",
        "error_code": "invalid_json",
    }

    dumped = request.model_dump(mode="json")
    assert list(dumped) == [
        "request_id",
        "run_id",
        "model_profile_id",
        "messages",
        "tools",
        "required_capabilities",
        "sampling_overrides",
        "maximum_output_tokens_override",
        "deadline",
        "cancellation",
        "secret_refs",
    ]
    assert dumped["messages"][2]["tool_calls"][0]["arguments"] == {
        "kind": "invalid",
        "raw": "not-json",
        "error_code": "invalid_json",
    }
    assert "stream" not in dumped
    assert "metadata" not in dumped


def test_public_bridge_records_reject_stale_public_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        SystemMessage.model_validate(
            {"role": "system", "content": "sys", "metadata": {"values": {}}}
        )
    with pytest.raises(ValidationError, match="extra"):
        UserMessage.model_validate(
            {"role": "user", "content": "hi", "metadata": {"values": {}}}
        )
    with pytest.raises(ValidationError, match="extra"):
        AssistantMessage.model_validate(
            {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [],
                "metadata": {"values": {}},
            }
        )
    with pytest.raises(ValidationError, match="extra"):
        ToolResultMessage.model_validate(
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "tool_name": "weather",
                "content": "Sunny",
                "metadata": {"values": {}},
            }
        )
    with pytest.raises(ValidationError, match="extra"):
        ModelToolDefinition.model_validate(
            {
                "name": "weather",
                "description": "Get weather",
                "input_schema": {},
                "required_capabilities": ["network.read"],
            }
        )
    with pytest.raises(ValidationError, match="extra"):
        InvalidToolArguments.model_validate(
            {
                "kind": "invalid",
                "raw": "not-json",
                "error_code": "invalid_json",
                "metadata": {"values": {}},
            }
        )

    canonical = {
        "request_id": "req-1",
        "run_id": "run-1",
        "model_profile_id": "gpt-test",
        "messages": [UserMessage(content="hi").model_dump(mode="json")],
        "tools": [],
        "required_capabilities": ModelCapabilityRequirements().model_dump(mode="json"),
        "sampling_overrides": SamplingRequest().model_dump(mode="json"),
        "maximum_output_tokens_override": None,
        "deadline": _deadline().model_dump(mode="json"),
        "cancellation": _cancellation().model_dump(mode="json"),
        "secret_refs": [],
    }
    assert ModelCompletionRequest.model_validate(canonical).request_id == "req-1"
    for stale_key, stale_value in (("stream", False), ("metadata", {"values": {}})):
        with pytest.raises(ValidationError, match="extra"):
            ModelCompletionRequest.model_validate({**canonical, stale_key: stale_value})
    for required_key in ("deadline", "cancellation"):
        stale = dict(canonical)
        stale.pop(required_key)
        with pytest.raises(ValidationError, match="Field required"):
            ModelCompletionRequest.model_validate(stale)


def test_model_response_and_validated_tool_calls_are_typed() -> None:
    invalid_args = InvalidToolArguments(
        raw={"not": "valid"},
        error_code="E_ARGUMENTS",
    )
    response = ModelCompletionResponse(
        provider_request_id=None,
        model_id="gpt-test",
        message=AssistantMessage(
            tool_calls=(
                ModelToolCall(call_id="call-1", name="weather", arguments=invalid_args),
            ),
        ),
        finish_reason="tool_calls",
        provider_metadata=None,
    )
    assert response.provider_request_id is None
    assert response.provider_metadata is None
    assert isinstance(response.tool_calls[0].arguments, InvalidToolArguments)
    assert (
        response.model_dump(mode="json")["message"]["tool_calls"][0]["call_id"]
        == "call-1"
    )
    assert "id" not in response.model_dump(mode="json")["message"]["tool_calls"][0]
    assert (
        ModelCompletionResponse(
            model_id="gpt-test",
            message=AssistantMessage(content="cancelled"),
            finish_reason="cancelled",
        ).finish_reason
        == "cancelled"
    )
    assert (
        ModelCompletionResponse(
            model_id="gpt-test",
            message=AssistantMessage(content="unknown"),
            finish_reason="unknown",
        ).finish_reason
        == "unknown"
    )
    with pytest.raises(ValidationError):
        ModelCompletionResponse(
            model_id="gpt-test",
            message=AssistantMessage(content="error"),
            finish_reason="error",  # type: ignore[arg-type]
        )

    call = ValidatedToolCall(
        call_id="call-1",
        node_id="node-weather",
        binding=_binding(),
        arguments={"city": "London"},
    )
    assert call.arguments == {"city": "London"}
    assert call.model_dump(mode="json") == {
        "call_id": "call-1",
        "node_id": "node-weather",
        "binding": _binding().model_dump(mode="json"),
        "arguments": {"city": "London"},
    }
    with pytest.raises(ValidationError, match="extra"):
        ValidatedToolCall.model_validate(
            {
                "call_id": "call-1",
                "node_id": "node-weather",
                "binding": _binding().model_dump(mode="json"),
                "name": "weather",
                "arguments": {"kind": "parsed", "value": {"city": "London"}},
            }
        )


def test_old_arbiter_reported_public_bridge_shapes_are_rejected() -> None:
    message_request = {
        "request_id": "req-1",
        "run_id": "run-1",
        "model_profile_id": "gpt-test",
        "messages": [{"kind": "tool_result", "content": "old"}],
    }
    with pytest.raises(ValidationError, match="union_tag_not_found|role"):
        ModelCompletionRequest.model_validate(message_request)

    response = ModelCompletionResponse(
        model_id="gpt-test",
        message=AssistantMessage(content="ok"),
        finish_reason="stop",
    )
    assert response.model_dump(mode="json")["provider_request_id"] is None
    assert response.model_dump(mode="json")["provider_metadata"] is None
    assert "error" not in get_args(
        ModelCompletionResponse.model_fields["finish_reason"].annotation
    )

    with pytest.raises(ValidationError, match="extra"):
        ValidatedToolCall.model_validate(
            {
                "call_id": "call-1",
                "node_id": "node-weather",
                "binding": _binding().model_dump(mode="json"),
                "arguments": {"kind": "parsed", "value": {"city": "London"}},
                "name": "weather",
                "metadata": {},
            }
        )

    canonical_result = ToolExecutionResult(
        call_id="call-1",
        status=ToolExecutionStatus.SUCCESS,
        summary="ok",
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        input_sha256=SHA_B,
        output_sha256=SHA_A,
        timing=TimingMetadata(started_at="start", completed_at="end", duration_ms=0.0),
    )
    with pytest.raises(ValidationError, match="extra"):
        ToolExecutionResult.model_validate(
            {
                **canonical_result.model_dump(mode="json"),
                "node_id": "node-weather",
                "binding": _binding().model_dump(mode="json"),
                "started_monotonic": 0.0,
                "completed_monotonic": 1.0,
                "duration_ms": 1000.0,
                "metadata": {},
            }
        )


def test_tool_execution_result_success_error_and_hash_invariants() -> None:
    success = ToolExecutionResult(
        call_id="call-1",
        status=ToolExecutionStatus.SUCCESS,
        summary="ok",
        structured_data={"temperature": 70},
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        input_sha256=SHA_B,
        output_sha256=SHA_A,
        timing=TimingMetadata(
            started_at="2026-06-12T00:00:00Z",
            completed_at="2026-06-12T00:00:01Z",
            duration_ms=500.0,
        ),
    )
    assert success.status == ToolExecutionStatus.SUCCESS
    assert "success" not in success.model_dump(mode="json")
    assert "output" not in success.model_dump(mode="json")
    assert "error" not in success.model_dump(mode="json")
    assert "node_id" not in success.model_dump(mode="json")
    assert "binding" not in success.model_dump(mode="json")
    assert "started_monotonic" not in success.model_dump(mode="json")
    assert success.model_dump(mode="json")["timing"] == {
        "started_at": "2026-06-12T00:00:00Z",
        "completed_at": "2026-06-12T00:00:01Z",
        "duration_ms": 500.0,
    }
    assert not hasattr(success, "success")
    assert not hasattr(success, "output")
    assert not hasattr(success, "error")

    failure = ToolExecutionResult(
        call_id="call-2",
        status=ToolExecutionStatus.SOFT_FAILURE,
        summary="temporary failure",
        error_code="E_TOOL_TEMPORARY",
        retryable=True,
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_ABSENT,
        input_sha256=SHA_B,
        output_sha256=None,
        timing=TimingMetadata(
            started_at="2026-06-12T00:00:02Z",
            completed_at="2026-06-12T00:00:03Z",
            duration_ms=1000.0,
        ),
    )
    assert failure.retryable is True

    with pytest.raises(ValidationError, match="successful"):
        ToolExecutionResult(
            call_id="call-3",
            status=ToolExecutionStatus.SUCCESS,
            summary="ok",
            error_code="E_BAD",
            side_effect_class=SideEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT,
            side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
            input_sha256=SHA_B,
            timing=TimingMetadata(
                started_at="start", completed_at="end", duration_ms=0.0
            ),
        )
    with pytest.raises(ValidationError, match="require error_code"):
        ToolExecutionResult(
            call_id="call-4",
            status=ToolExecutionStatus.HARD_FAILURE,
            summary="failed",
            side_effect_class=SideEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT,
            side_effect_certainty=SideEffectCertainty.CONFIRMED_ABSENT,
            input_sha256=SHA_B,
            timing=TimingMetadata(
                started_at="start", completed_at="end", duration_ms=0.0
            ),
        )

    with pytest.raises(ValidationError, match="lowercase hex"):
        ToolExecutionResult(
            call_id="call-5",
            status=ToolExecutionStatus.SUCCESS,
            summary="ok",
            side_effect_class=SideEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT,
            side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
            input_sha256=SHA_B,
            output_sha256="bad",
            timing=TimingMetadata(
                started_at="start", completed_at="end", duration_ms=0.0
            ),
        )

    with pytest.raises(ValidationError, match="extra"):
        ToolExecutionResult.model_validate(
            {
                **success.model_dump(mode="json"),
                "node_id": "node-weather",
                "binding": _binding().model_dump(mode="json"),
                "started_monotonic": 0.0,
                "completed_monotonic": 0.0,
                "duration_ms": 0.0,
                "metadata": {},
            }
        )


def test_public_bridge_exports_canonical_tool_definition_name_only() -> None:
    assert "ModelToolDefinition" in millforge.__all__
    assert millforge.ModelToolDefinition is ModelToolDefinition
    assert "ToolDefinition" not in millforge.__all__
    assert not hasattr(millforge, "ToolDefinition")


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
