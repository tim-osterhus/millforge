from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import re
from typing import Any, Literal

import pytest

from millforge._forge import adapter as forge_adapter
from millforge._forge.adapter import (
    ForgeBindingRejectedError,
    ForgeBridgeError,
    ForgeContextFactory,
    ForgeEventTranslator,
    ForgeModelBridge,
    ForgeSessionInputBuilder,
    ForgeToolBridge,
    ForgeWorkflowFactory,
    cancellation_event_from_ref,
)
from millforge._forge.errors import NonRetryableToolError, ToolResolutionError
from millforge._forge.context.strategies import TieredCompact
from millforge._forge.core.messages import MessageRole, MessageType
from millforge.compiled_plan import (
    ArgumentMatch,
    CompiledHarnessNode,
    CompiledHarnessPlan,
    CompiledPrerequisite,
    SessionEventType,
    IdempotencyClass,
    SideEffectCertainty,
    SideEffectClass,
    ToolBindingRef,
    ToolExecutionStatus,
    ToolTraceIdempotency,
    ToolTraceSideEffectClass,
    canonical_json_serialize,
)
from millforge.contracts import (
    ArtifactRef,
    AssistantMessage,
    InvalidToolArguments,
    ModelCompletionResponse,
    ModelToolCall,
    ParsedToolArguments,
    TimingMetadata,
    ToolExecutionResult,
    ToolResultMessage,
    TokenUsage,
)
from millforge.testing import FakeModelClient, FakeToolExecutor
from tests.conftest import (
    FakeCancellationResolver,
    FakeClock,
    SHA_A,
    SHA_B,
    make_test_compiled_plan,
    make_test_guarded_session_request,
)


def _prepare_node(
    *,
    prerequisites: tuple[CompiledPrerequisite, ...] = (),
    schema: dict[str, Any] | None = None,
) -> CompiledHarnessNode:
    return CompiledHarnessNode(
        node_id="node-002",
        model_tool_name="prepare",
        description="Prepare input",
        input_schema=schema
        or {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "description": "Path to prepare"},
                "mode": {"type": "string", "default": "read"},
            },
            "required": ["path"],
        },
        binding=ToolBindingRef(
            tool_id="prepare",
            tool_version=1,
            descriptor_sha256=SHA_B,
            implementation_id="impl-prepare-v1",
        ),
        prerequisites=prerequisites,
        required=True,
        terminal_result=None,
        required_capabilities=("workspace.read",),
        produced_artifact_ids=(),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    )


def _lookup_node() -> CompiledHarnessNode:
    return CompiledHarnessNode(
        node_id="node-003",
        model_tool_name="lookup",
        description="Look up source data",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"source_path": {"type": "string"}},
            "required": ["source_path"],
        },
        binding=ToolBindingRef(
            tool_id="lookup",
            tool_version=1,
            descriptor_sha256=SHA_B,
            implementation_id="impl-lookup-v1",
        ),
        prerequisites=(),
        required=True,
        terminal_result=None,
        required_capabilities=("workspace.read",),
        produced_artifact_ids=(),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    )


def _terminal_node() -> CompiledHarnessNode:
    return CompiledHarnessNode(
        node_id="node-001",
        model_tool_name="submit",
        description="Submit result",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
        binding=ToolBindingRef(
            tool_id="submit",
            tool_version=1,
            descriptor_sha256=SHA_A,
            implementation_id="impl-submit-v1",
        ),
        prerequisites=(
            CompiledPrerequisite(
                node_id="node-002",
                argument_matches=(
                    ArgumentMatch(
                        prerequisite_argument="path",
                        current_argument="path",
                    ),
                ),
            ),
        ),
        required=False,
        terminal_result="success",
        required_capabilities=("workspace.read",),
        produced_artifact_ids=("art-output-001",),
        side_effect_class=SideEffectClass.TERMINAL,
        idempotency=IdempotencyClass.IDEMPOTENT,
    )


def _plan(
    *,
    prepare_node: CompiledHarnessNode | None = None,
    terminal_node: CompiledHarnessNode | None = None,
) -> CompiledHarnessPlan:
    return make_test_compiled_plan(
        nodes=(prepare_node or _prepare_node(), terminal_node or _terminal_node())
    )


def _callable(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "args": kwargs}


def _artifact_output() -> str:
    return json.dumps(
        {
            "summary": "done",
            "artifact_refs": [
                {
                    "artifact_id": "art-output-001",
                    "path": "millforge/output.json",
                    "content_type": "application/json",
                }
            ],
        },
        sort_keys=True,
    )


def _model_response(
    *,
    content: str,
    call_id: str | None = None,
    tool_name: str = "prepare",
    arguments: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
) -> ModelCompletionResponse:
    tool_calls: tuple[ModelToolCall, ...] = ()
    finish_reason: Literal["stop", "tool_calls"] = "stop"
    if call_id is not None:
        tool_calls = (
            ModelToolCall(
                call_id=call_id,
                name=tool_name,
                arguments=ParsedToolArguments(value=arguments or {}),
            ),
        )
        finish_reason = "tool_calls"
    return ModelCompletionResponse(
        provider_request_id=f"provider-{call_id or 'text'}",
        model_id="profile-test",
        message=AssistantMessage(content=content, tool_calls=tool_calls),
        finish_reason=finish_reason,
        usage=usage,
    )


def _tool_result(
    call_id: str,
    summary: str,
    *,
    status: ToolExecutionStatus = ToolExecutionStatus.SUCCESS,
    error_code: str | None = None,
    retryable: bool = False,
    output_sha256: str | None = None,
    artifact_refs: tuple[ArtifactRef, ...] = (),
    input_arguments: dict[str, Any] | None = None,
) -> ToolExecutionResult:
    node = _terminal_node() if "submit" in call_id else _prepare_node()
    input_arguments = input_arguments or {"path": "input.txt"}
    return ToolExecutionResult(
        call_id=call_id,
        status=status,
        summary=summary,
        artifact_refs=artifact_refs,
        error_code=error_code,
        retryable=retryable,
        side_effect_class=node.side_effect_class,
        idempotency=node.idempotency,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE
        if status == ToolExecutionStatus.SUCCESS
        else SideEffectCertainty.CONFIRMED_ABSENT,
        input_sha256=hashlib.sha256(
            canonical_json_serialize(input_arguments).encode("utf-8")
        ).hexdigest(),
        output_sha256=output_sha256,
        timing=TimingMetadata(started_at="start", completed_at="end", duration_ms=0.0),
    )


def test_forge_tool_bridge_source_uses_canonical_result_boundary() -> None:
    source = inspect.getsource(forge_adapter)
    assert re.search(r"\bToolDefinition\b", source) is None
    assert re.search(r"result\.(success|output|error)\b", source) is None


def _tool_bridge(
    *,
    plan: CompiledHarnessPlan | None = None,
    session_request: Any | None = None,
    executor: FakeToolExecutor | None = None,
    cancellation_resolver: FakeCancellationResolver | None = None,
    monotonic_value: float = 1.0,
    call_id: str = "call-prepare",
) -> ForgeToolBridge:
    return ForgeToolBridge(
        plan=plan or _plan(),
        session_request=session_request or make_test_guarded_session_request(),
        executor=executor
        or FakeToolExecutor(supported_tools={"prepare", "submit"}, results={}),
        cancellation_resolver=cancellation_resolver or FakeCancellationResolver(),
        clock=FakeClock(monotonic_value=monotonic_value),
        call_id_resolver=lambda _name, _args: call_id,
    )


def test_workflow_factory_maps_compiled_plan_semantics_to_private_forge() -> None:
    plan = _plan()
    workflow_input = ForgeWorkflowFactory(
        {
            "impl-prepare-v1": _callable,
            "impl-submit-v1": _callable,
        },
        cancellation_id="cancel-001",
    ).build(plan)

    workflow = workflow_input.workflow
    assert workflow.name == plan.harness_id
    assert workflow.system_prompt_template == plan.prompt_policy.system_instructions
    assert list(workflow.tools) == ["prepare", "submit"]
    assert workflow.required_steps == ["prepare"]
    assert workflow.terminal_tools == frozenset({"submit"})
    assert workflow.tools["submit"].prerequisites == [
        {"tool": "prepare", "match_arg": "path"}
    ]
    assert workflow_input.runner_options.max_iterations == plan.budgets.max_iterations
    assert (
        workflow_input.runner_options.max_retries_per_step
        == plan.budgets.max_validation_retries
    )
    assert workflow_input.runner_options.max_tool_errors == plan.budgets.max_tool_errors
    assert (
        workflow_input.runner_options.max_premature_attempts
        == plan.budgets.max_premature_terminal_attempts
    )
    assert (
        workflow_input.runner_options.max_prereq_violations
        == plan.budgets.max_prerequisite_violations
    )
    assert workflow_input.binding_by_tool == {
        "prepare": "impl-prepare-v1",
        "submit": "impl-submit-v1",
    }
    assert workflow_input.node_id_by_tool == {
        "prepare": "node-002",
        "submit": "node-001",
    }
    assert workflow_input.terminal_result_by_tool == {"submit": "success"}
    assert workflow_input.cancellation_id == "cancel-001"


def test_session_input_builder_emits_two_deterministic_private_messages() -> None:
    plan = _plan()
    session_request = make_test_guarded_session_request()

    messages = ForgeSessionInputBuilder().build(plan, session_request)

    assert len(messages) == 2
    assert messages[0].role == MessageRole.SYSTEM
    assert messages[0].metadata.type == MessageType.SYSTEM_PROMPT
    assert messages[0].content == plan.prompt_policy.system_instructions
    assert messages[1].role == MessageRole.USER
    assert messages[1].metadata.type == MessageType.USER_INPUT
    assert messages[1].content == json.dumps(
        json.loads(messages[1].content),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    payload = json.loads(messages[1].content)
    assert payload == {
        "input_artifacts": [
            {
                "artifact_id": "art-input-001",
                "content_type": "application/json",
                "path": "millforge/input.json",
            }
        ],
        "kind": "millforge_stage_request",
        "request_id": "req-test-001",
        "run_id": "run-test-001",
        "schema_version": "1.0",
        "stage": {
            "node_id": "builder",
            "plane": "execution",
            "stage_kind_id": "builder",
        },
        "work_item_id": "task-test-001",
    }
    forbidden_fields = {
        "cancellation",
        "compiled_harness",
        "context_policy",
        "prompt_policy",
        "run_directory",
        "timeout",
        "secret_refs",
        "capability_envelope",
        "model_profile",
        "session_id",
    }
    assert forbidden_fields.isdisjoint(payload)
    forbidden_fragments = (
        "DATABASE_PASSWORD",
        '{"schema_version":"test"}',
        "/tmp/runs/run-test-001",
        "/tmp/millforge/harnesses/compiled-test-001",
        "cancel-001",
        "deepseek_flash_high",
        "workspace.read",
        "artifact.write",
        "2026-01-01T00:05:00Z",
    )
    for fragment in forbidden_fragments:
        assert fragment not in messages[1].content


def test_session_input_builder_omits_request_context_when_policy_disables_it() -> None:
    base = _plan()
    plan = base.model_copy(
        update={
            "prompt_policy": base.prompt_policy.model_copy(
                update={"include_request_context": False}
            )
        }
    )
    session_request = make_test_guarded_session_request()

    messages = ForgeSessionInputBuilder().build(plan, session_request)
    payload = json.loads(messages[1].content)

    assert payload == {
        "kind": "millforge_stage_request",
        "request_id": "req-test-001",
        "run_id": "run-test-001",
        "schema_version": "1.0",
        "stage": {
            "node_id": "builder",
            "plane": "execution",
            "stage_kind_id": "builder",
        },
    }
    assert "work_item_id" not in payload
    assert "input_artifacts" not in payload


def test_context_factory_uses_only_supported_tiered_policy_values() -> None:
    manager = ForgeContextFactory().build(_plan().context_policy)

    assert manager.budget_tokens == 4096
    assert manager._context_thresholds == []
    assert manager._on_context_threshold is None
    assert manager.on_compact is forge_adapter._context_compaction_callback
    assert isinstance(manager.strategy, TieredCompact)
    assert manager.strategy.keep_recent == 1
    assert manager.strategy._phase_triggers == (0.25, 0.5, 1.0)


def test_context_factory_rejects_unknown_strategy_before_calls() -> None:
    policy = _plan().context_policy.model_copy(update={"strategy_id": "unknown"})

    with pytest.raises(ForgeBindingRejectedError) as exc_info:
        ForgeContextFactory().build(policy)

    assert exc_info.value.code == "binding_rejected"


def test_unsupported_schema_rejects_before_tool_callable_is_invoked() -> None:
    called = False

    def side_effecting_callable(**kwargs: Any) -> str:
        nonlocal called
        called = True
        return "called"

    plan = _plan(
        prepare_node=_prepare_node(
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string", "minLength": 1}},
                "required": ["path"],
            }
        )
    )

    with pytest.raises(ForgeBindingRejectedError) as exc_info:
        ForgeWorkflowFactory(
            {
                "impl-prepare-v1": side_effecting_callable,
                "impl-submit-v1": side_effecting_callable,
            }
        ).build(plan)

    assert exc_info.value.code == "binding_rejected"
    assert "Unsupported input schema" in str(exc_info.value)
    assert called is False


@pytest.mark.parametrize(
    "schema",
    [
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": [],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"path": {"type": "string"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "metadata": {
                    "type": "object",
                    "properties": {"source": {"type": "string"}},
                    "required": ["source"],
                }
            },
            "required": ["metadata"],
        },
    ],
)
def test_incomplete_object_schema_rejects_before_tool_callable_is_invoked(
    schema: dict[str, Any],
) -> None:
    called = False

    def side_effecting_callable(**kwargs: Any) -> str:
        nonlocal called
        called = True
        return "called"

    plan = _plan(prepare_node=_prepare_node(schema=schema))

    with pytest.raises(ForgeBindingRejectedError) as exc_info:
        ForgeWorkflowFactory(
            {
                "impl-prepare-v1": side_effecting_callable,
                "impl-submit-v1": side_effecting_callable,
            }
        ).build(plan)

    assert exc_info.value.code == "binding_rejected"
    assert "Unsupported input schema" in str(exc_info.value)
    assert called is False


def test_missing_binding_rejects_before_available_callable_is_invoked() -> None:
    called = False

    def available_callable(**kwargs: Any) -> str:
        nonlocal called
        called = True
        return "called"

    with pytest.raises(ForgeBindingRejectedError) as exc_info:
        ForgeWorkflowFactory({"impl-submit-v1": available_callable}).build(_plan())

    assert exc_info.value.code == "binding_rejected"
    assert "Missing tool binding implementation" in str(exc_info.value)
    assert called is False


def test_unsupported_prerequisite_argument_mapping_rejects_before_calls() -> None:
    terminal = _terminal_node().model_copy(
        update={
            "prerequisites": (
                CompiledPrerequisite(
                    node_id="node-002",
                    argument_matches=(
                        ArgumentMatch(
                            prerequisite_argument="path",
                            current_argument="path",
                        ),
                        ArgumentMatch(
                            prerequisite_argument="mode",
                            current_argument="mode",
                        ),
                    ),
                ),
            )
        }
    )

    with pytest.raises(ForgeBindingRejectedError, match="at most one argument match"):
        ForgeWorkflowFactory(
            {
                "impl-prepare-v1": _callable,
                "impl-submit-v1": _callable,
            }
        ).build(_plan(terminal_node=terminal))


def test_prerequisite_argument_mapping_preserves_distinct_match_fields() -> None:
    lookup = _lookup_node()
    terminal = _terminal_node().model_copy(
        update={
            "prerequisites": (
                CompiledPrerequisite(
                    node_id="node-003",
                    argument_matches=(
                        ArgumentMatch(
                            prerequisite_argument="source_path",
                            current_argument="path",
                        ),
                    ),
                ),
            )
        }
    )
    plan = make_test_compiled_plan(nodes=(lookup, terminal))

    workflow_input = ForgeWorkflowFactory(
        {
            "impl-lookup-v1": _callable,
            "impl-submit-v1": _callable,
        }
    ).build(plan)

    assert workflow_input.workflow.tools["submit"].prerequisites == [
        {
            "tool": "lookup",
            "prerequisite_arg": "source_path",
            "current_arg": "path",
        }
    ]


def test_duplicate_identity_rejects_even_if_model_copy_bypasses_plan_validation() -> (
    None
):
    plan = _plan()
    duplicate_tool = _prepare_node().model_copy(update={"node_id": "node-003"})
    invalid = plan.model_copy(
        update={"nodes": (plan.nodes[0], duplicate_tool, plan.nodes[1])}
    )

    with pytest.raises(ForgeBindingRejectedError, match="model_tool_name"):
        ForgeWorkflowFactory(
            {
                "impl-prepare-v1": _callable,
                "impl-submit-v1": _callable,
            }
        ).build(invalid)


def test_cancellation_ref_can_seed_private_forge_cancel_event() -> None:
    assert cancellation_event_from_ref(cancel_requested=False).is_set() is False
    assert cancellation_event_from_ref(cancel_requested=True).is_set() is True


@pytest.mark.asyncio
async def test_model_bridge_translates_private_forge_request_once_and_tracks_usage() -> (
    None
):
    client = FakeModelClient(
        responses=[
            _model_response(
                content="reasoning",
                call_id="model-call-001",
                arguments={"path": "input.txt"},
                usage=TokenUsage(
                    input_tokens=5,
                    output_tokens=7,
                    total_tokens=12,
                    provider_reported=False,
                ),
            )
        ]
    )
    spec = (
        ForgeWorkflowFactory(
            {"impl-prepare-v1": _callable, "impl-submit-v1": _callable}
        )
        .build(_plan())
        .workflow.tools["prepare"]
        .spec
    )
    translator = ForgeEventTranslator(
        session_request=make_test_guarded_session_request(),
        clock=FakeClock(monotonic_value=2.0),
    )
    bridge = ForgeModelBridge(
        model_client=client,
        model="profile-test",
        event_translator=translator,
    )

    response = await bridge.send(
        [
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "history-call-001",
                        "function": {
                            "name": "prepare",
                            "arguments": {"path": "input.txt"},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "history-call-001",
                "tool_name": "prepare",
                "content": "prepared",
            },
        ],
        tools=[spec],
    )

    assert client.call_count == 1
    request = client.requests[0]
    session_request = translator._session_request.execution_request
    assert request.request_id == session_request.request_id
    assert request.run_id == session_request.run_id
    assert request.model_profile_id == "profile-test"
    assert request.maximum_output_tokens_override is None
    assert request.required_capabilities.tool_calls is True
    assert request.required_capabilities.parallel_tool_calls is False
    assert request.required_capabilities.structured_output is False
    assert request.required_capabilities.reasoning_controls is False
    assert request.required_capabilities.usage_reporting is False
    assert request.required_capabilities.system_messages is True
    assert request.required_capabilities.tool_result_messages is True
    assert request.sampling_overrides.temperature is None
    assert request.deadline == translator._session_request.deadline
    assert request.cancellation == session_request.cancellation
    assert request.secret_refs == session_request.secret_refs
    assert request.tools[0].name == "prepare"
    assert request.messages[0].kind == "user"
    dumped_request = request.model_dump(mode="json")
    assert "stream" not in dumped_request
    assert dumped_request["sampling_overrides"] == {
        "temperature": None,
        "top_p": None,
        "presence_penalty": None,
        "frequency_penalty": None,
        "seed": None,
        "stop": None,
        "reasoning_mode": None,
        "reasoning_effort": None,
    }
    assert [message["role"] for message in dumped_request["messages"]] == [
        "user",
        "assistant",
        "tool",
    ]
    assert all("kind" not in message for message in dumped_request["messages"])
    tool_result_message = request.messages[2]
    assert isinstance(tool_result_message, ToolResultMessage)
    assert tool_result_message.tool_call_id == "history-call-001"
    assert tool_result_message.tool_name == "prepare"
    assert response[0].tool == "prepare"
    assert response[0].args == {"path": "input.txt"}
    assert (
        bridge.resolve_tool_call_id("prepare", {"path": "input.txt"})
        == "model-call-001"
    )
    assert bridge.last_usage[0].total_tokens == 12
    assert [event.event_type for event in bridge.events] == [
        SessionEventType.MODEL_REQUEST_STARTED,
        SessionEventType.MODEL_REQUEST_COMPLETED,
    ]
    assert bridge.events[0].fields[0].key == "message_count"
    assert bridge.events[0].fields[0].value == 3
    assert bridge.events[1].fields[0].key == "tool_call_count"
    assert bridge.events[1].fields[0].value == 1


@pytest.mark.asyncio
async def test_model_bridge_preserves_invalid_tool_arguments_as_malformed() -> None:
    invalid = InvalidToolArguments(
        raw='{"path":',
        error_code="invalid_json",
    )
    client = FakeModelClient(
        responses=[
            ModelCompletionResponse(
                provider_request_id="provider-invalid",
                model_id="profile-test",
                message=AssistantMessage(
                    content="reasoning",
                    tool_calls=(
                        ModelToolCall(
                            call_id="model-call-invalid",
                            name="prepare",
                            arguments=invalid,
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
        ]
    )
    spec = (
        ForgeWorkflowFactory(
            {"impl-prepare-v1": _callable, "impl-submit-v1": _callable}
        )
        .build(_plan())
        .workflow.tools["prepare"]
        .spec
    )
    bridge = ForgeModelBridge(model_client=client, model="profile-test")

    response = await bridge.send(
        [{"role": "user", "content": "run"}],
        tools=[spec],
    )

    assert client.call_count == 1
    assert response[0].tool == "prepare"
    assert response[0].args == '{"path":'
    assert (
        bridge.resolve_tool_call_id("prepare", {"_invalid_arguments": "invalid_json"})
        is None
    )


@pytest.mark.asyncio
async def test_model_bridge_rejects_parallel_tool_calls_before_tool_dispatch() -> None:
    client = FakeModelClient(
        responses=[
            ModelCompletionResponse(
                provider_request_id="provider-parallel",
                model_id="profile-test",
                message=AssistantMessage(
                    content="parallel",
                    tool_calls=(
                        ModelToolCall(
                            call_id="model-call-001",
                            name="prepare",
                            arguments=ParsedToolArguments(value={"path": "a.txt"}),
                        ),
                        ModelToolCall(
                            call_id="model-call-002",
                            name="prepare",
                            arguments=ParsedToolArguments(value={"path": "b.txt"}),
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
        ]
    )
    spec = (
        ForgeWorkflowFactory(
            {"impl-prepare-v1": _callable, "impl-submit-v1": _callable}
        )
        .build(_plan())
        .workflow.tools["prepare"]
        .spec
    )
    bridge = ForgeModelBridge(model_client=client, model="profile-test")

    with pytest.raises(ForgeBridgeError, match="parallel tool calls"):
        await bridge.send(
            [{"role": "user", "content": "run"}],
            tools=[spec],
        )

    assert client.call_count == 1
    assert bridge.resolve_tool_call_id("prepare", {"path": "a.txt"}) is None
    assert bridge.resolve_tool_call_id("prepare", {"path": "b.txt"}) is None


@pytest.mark.asyncio
async def test_model_bridge_emits_failed_model_request_event_without_payloads() -> None:
    translator = ForgeEventTranslator(
        session_request=make_test_guarded_session_request(),
        clock=FakeClock(monotonic_value=2.0),
    )
    bridge = ForgeModelBridge(
        model_client=FakeModelClient(exceptions=[RuntimeError("secret prompt body")]),
        model="profile-test",
        event_translator=translator,
    )

    with pytest.raises(RuntimeError):
        await bridge.send([{"role": "user", "content": "raw private prompt"}])

    assert [event.event_type for event in bridge.events] == [
        SessionEventType.MODEL_REQUEST_STARTED,
        SessionEventType.MODEL_REQUEST_FAILED,
    ]
    assert bridge.events[1].code == "RuntimeError"
    serialized = json.dumps([event.model_dump(mode="json") for event in bridge.events])
    assert "raw private prompt" not in serialized
    assert "secret prompt body" not in serialized


def test_event_translator_maps_runner_owned_activity_without_raw_payloads() -> None:
    translator = ForgeEventTranslator(
        session_request=make_test_guarded_session_request(),
        clock=FakeClock(monotonic_value=3.0),
    )

    translator.workflow_constructed(tool_count=2)
    translator.correction_issued(code="tool_arg_validation")
    translator.premature_terminal_rejected(node_id="node-001")
    translator.context_compacted(kept_messages=4)
    translator.budget_exhausted(code="max_iterations")
    metadata = translator.sanitized_metadata(
        provider_payload="x" * 300,
        retry_count=1,
    )

    assert [event.event_type for event in translator.events] == [
        SessionEventType.WORKFLOW_CONSTRUCTED,
        SessionEventType.CORRECTION_ISSUED,
        SessionEventType.PREMATURE_TERMINAL_REJECTED,
        SessionEventType.CONTEXT_COMPACTED,
        SessionEventType.BUDGET_EXHAUSTED,
    ]
    assert translator.events[0].fields[0].value == 2
    assert len(metadata[0].value) == 256
    assert metadata[1].value == 1


@pytest.mark.asyncio
async def test_model_bridge_rejects_streaming_and_provider_passthrough() -> None:
    bridge = ForgeModelBridge(
        model_client=FakeModelClient(responses=[_model_response(content="ok")]),
        model="profile-test",
    )

    with pytest.raises(ForgeBridgeError):
        await bridge.send(
            [{"role": "user", "content": "run"}],
            passthrough={"provider": "field"},
        )
    with pytest.raises(NotImplementedError):
        async for _chunk in bridge.send_stream([{"role": "user", "content": "run"}]):
            pass
    assert await bridge.get_context_length() is None


@pytest.mark.asyncio
async def test_tool_bridge_uses_owned_history_for_terminal_acceptance() -> None:
    plan = _plan()
    session_request = make_test_guarded_session_request()
    executor = FakeToolExecutor(
        supported_tools={"prepare", "submit"},
        results={
            "prepare": [_tool_result("call-prepare", "prepared")],
            "submit": [
                _tool_result(
                    "call-submit",
                    _artifact_output(),
                    artifact_refs=(
                        ArtifactRef(
                            artifact_id="art-output-001",
                            path=Path("millforge/output.json"),
                            content_type="application/json",
                        ),
                    ),
                )
            ],
        },
    )
    call_ids = {
        ("prepare", "input.txt"): "call-prepare",
        ("submit", "input.txt"): "call-submit",
    }
    bridge = ForgeToolBridge(
        plan=plan,
        session_request=session_request,
        executor=executor,
        cancellation_resolver=FakeCancellationResolver(),
        clock=FakeClock(monotonic_value=1.0),
        call_id_resolver=lambda name, args: call_ids[(name, args["path"])],
    )

    assert await bridge.invoke("prepare", {"path": "input.txt"}) == "prepared"
    assert await bridge.invoke("submit", {"path": "input.txt"}) == _artifact_output()

    assert executor.call_count == 2
    assert [call.id for call in executor.calls] == ["call-prepare", "call-submit"]
    assert executor.calls[0].model_dump(mode="json") == {
        "call_id": "call-prepare",
        "node_id": "node-002",
        "binding": plan.nodes[0].binding.model_dump(mode="json"),
        "arguments": {"path": "input.txt"},
    }
    assert "name" not in executor.calls[0].model_dump(mode="json")
    assert "metadata" not in executor.calls[0].model_dump(mode="json")
    assert bridge.tool_trace[0].execution_status.value == "success"
    assert bridge.tool_trace[1].execution_status.value == "success"
    assert bridge.terminal_candidate is not None
    assert bridge.terminal_intent is not None
    assert bridge.terminal_intent.terminal_node_id == "node-001"
    assert bridge.terminal_intent.artifact_refs == (
        ArtifactRef(
            artifact_id="art-output-001",
            path=Path("millforge/output.json"),
            content_type="application/json",
        ),
    )


@pytest.mark.asyncio
async def test_tool_bridge_rejects_terminal_without_owned_required_history() -> None:
    plan = _plan()
    executor = FakeToolExecutor(
        supported_tools={"prepare", "submit"},
        results={
            "submit": [_tool_result("call-submit", _artifact_output())],
        },
    )
    bridge = ForgeToolBridge(
        plan=plan,
        session_request=make_test_guarded_session_request(),
        executor=executor,
        cancellation_resolver=FakeCancellationResolver(),
        clock=FakeClock(monotonic_value=1.0),
        call_id_resolver=lambda _name, _args: "call-submit",
    )

    with pytest.raises(ToolResolutionError):
        await bridge.invoke("submit", {"path": "input.txt"})

    assert executor.call_count == 0
    assert bridge.terminal_intent is None
    assert bridge.tool_trace[0].execution_status.value == "not_executed"


@pytest.mark.asyncio
async def test_tool_bridge_denies_capability_without_executor_call() -> None:
    plan = _plan()
    request = make_test_guarded_session_request()
    request = request.model_copy(
        update={
            "execution_request": request.execution_request.model_copy(
                update={
                    "capability_envelope": request.execution_request.capability_envelope.model_copy(
                        update={"grants": ()}
                    )
                }
            )
        }
    )
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"prepare": [_tool_result("call-prepare", "ok")]},
    )
    bridge = ForgeToolBridge(
        plan=plan,
        session_request=request,
        executor=executor,
        cancellation_resolver=FakeCancellationResolver(),
        clock=FakeClock(monotonic_value=1.0),
        call_id_resolver=lambda _name, _args: "call-prepare",
    )

    with pytest.raises(ToolResolutionError):
        await bridge.invoke("prepare", {"path": "input.txt"})

    assert executor.call_count == 0
    assert bridge.tool_trace[0].capability_decisions[0].decision.value == "denied"


@pytest.mark.asyncio
async def test_tool_bridge_handles_cancellation_before_executor_call() -> None:
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"prepare": [_tool_result("call-prepare", "ok")]},
    )
    bridge = ForgeToolBridge(
        plan=_plan(),
        session_request=make_test_guarded_session_request(),
        executor=executor,
        cancellation_resolver=FakeCancellationResolver(is_cancelled=True),
        clock=FakeClock(monotonic_value=1.0),
        call_id_resolver=lambda _name, _args: "call-prepare",
    )

    with pytest.raises(NonRetryableToolError):
        await bridge.invoke("prepare", {"path": "input.txt"})

    assert executor.call_count == 0
    assert bridge.tool_trace[0].execution_status.value == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_status", "expected_exception", "expected_code"),
    [
        (
            _tool_result(
                "call-prepare",
                "try again",
                status=ToolExecutionStatus.SOFT_FAILURE,
                error_code="soft",
                retryable=True,
            ),
            ToolExecutionStatus.SOFT_FAILURE,
            ToolResolutionError,
            "soft",
        ),
        (
            _tool_result(
                "call-prepare",
                "hard stop",
                status=ToolExecutionStatus.HARD_FAILURE,
                error_code="hard",
            ),
            ToolExecutionStatus.HARD_FAILURE,
            NonRetryableToolError,
            "hard",
        ),
        (
            _tool_result(
                "call-prepare",
                "timed out",
                status=ToolExecutionStatus.TIMED_OUT,
                error_code="timeout",
            ),
            ToolExecutionStatus.TIMED_OUT,
            NonRetryableToolError,
            "timeout",
        ),
        (
            _tool_result(
                "call-prepare",
                "unknown completion",
                status=ToolExecutionStatus.AMBIGUOUS,
                error_code="ambiguous",
            ),
            ToolExecutionStatus.AMBIGUOUS,
            NonRetryableToolError,
            "ambiguous",
        ),
    ],
)
async def test_tool_bridge_records_failed_executor_outcomes_without_terminal_acceptance(
    result: ToolExecutionResult,
    expected_status: ToolExecutionStatus,
    expected_exception: type[Exception],
    expected_code: str,
) -> None:
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"prepare": [result]},
    )
    bridge = _tool_bridge(executor=executor)

    with pytest.raises(expected_exception):
        await bridge.invoke("prepare", {"path": "input.txt"})

    assert executor.call_count == 1
    assert bridge.tool_trace[0].execution_status == expected_status
    assert bridge.events[-1].event_type == SessionEventType.TOOL_FAILED
    assert bridge.events[-1].code == expected_code
    assert bridge.terminal_intent is None


@pytest.mark.asyncio
async def test_tool_bridge_records_policy_denial_without_executor_call() -> None:
    executor = FakeToolExecutor(
        supported_tools=set(),
        results={"prepare": [_tool_result("call-prepare", "ok")]},
    )
    bridge = _tool_bridge(executor=executor)

    with pytest.raises(ToolResolutionError):
        await bridge.invoke("prepare", {"path": "input.txt"})

    assert executor.call_count == 0
    assert bridge.tool_trace[0].execution_status == ToolExecutionStatus.NOT_EXECUTED
    assert bridge.tool_trace[0].capability_decisions[-1].key == "tool:prepare"
    assert bridge.tool_trace[0].capability_decisions[-1].decision.value == "denied"
    assert bridge.terminal_intent is None


@pytest.mark.asyncio
async def test_tool_bridge_records_binding_defect_trace_before_raising() -> None:
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"prepare": [_tool_result("different-call", "wrong binding")]},
    )
    bridge = _tool_bridge(executor=executor)

    with pytest.raises(NonRetryableToolError):
        await bridge.invoke("prepare", {"path": "input.txt"})

    assert executor.call_count == 1
    assert bridge.tool_trace[0].execution_status == ToolExecutionStatus.HARD_FAILURE
    assert (
        bridge.tool_trace[0].summary == "tool binding defect: result call_id mismatch"
    )
    assert bridge.events[-1].code == "binding_defect"
    assert bridge.terminal_intent is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_updates", "expected_summary"),
    [
        (
            {"input_sha256": SHA_B},
            "tool binding defect: result input_sha256 mismatch",
        ),
    ],
)
async def test_tool_bridge_validates_owned_result_identity_and_input_hash(
    result_updates: dict[str, Any],
    expected_summary: str,
) -> None:
    result = _tool_result("call-prepare", "wrong boundary").model_copy(
        update=result_updates
    )
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"prepare": [result]},
    )
    bridge = _tool_bridge(executor=executor)

    with pytest.raises(NonRetryableToolError):
        await bridge.invoke("prepare", {"path": "input.txt"})

    assert executor.call_count == 1
    assert bridge.tool_trace[0].execution_status == ToolExecutionStatus.HARD_FAILURE
    assert bridge.tool_trace[0].summary == expected_summary
    assert bridge.events[-1].code == "binding_defect"
    assert bridge.terminal_intent is None


@pytest.mark.asyncio
async def test_tool_bridge_records_output_hash_defect_trace_before_raising() -> None:
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={
            "prepare": [
                _tool_result("call-prepare", "safe output", output_sha256=SHA_A)
            ]
        },
    )
    bridge = _tool_bridge(executor=executor)

    with pytest.raises(NonRetryableToolError):
        await bridge.invoke("prepare", {"path": "input.txt"})

    assert executor.call_count == 1
    assert bridge.tool_trace[0].execution_status == ToolExecutionStatus.HARD_FAILURE
    assert bridge.tool_trace[0].output_sha256 == SHA_A
    assert bridge.events[-1].code == "output_hash_mismatch"
    assert bridge.terminal_intent is None


@pytest.mark.asyncio
async def test_tool_bridge_preserves_result_owned_trace_metadata() -> None:
    result = _tool_result(
        "call-prepare",
        "partial network write",
        status=ToolExecutionStatus.AMBIGUOUS,
        error_code="ambiguous",
    ).model_copy(
        update={
            "side_effect_class": SideEffectClass.NETWORK_WRITE,
            "idempotency": IdempotencyClass.NON_IDEMPOTENT,
            "side_effect_certainty": SideEffectCertainty.COMPLETION_UNKNOWN,
            "timing": TimingMetadata(
                started_at="start", completed_at="end", duration_ms=12.5
            ),
        }
    )
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"prepare": [result]},
    )
    bridge = _tool_bridge(executor=executor)

    with pytest.raises(NonRetryableToolError):
        await bridge.invoke("prepare", {"path": "input.txt"})

    trace = bridge.tool_trace[0]
    assert trace.execution_status == ToolExecutionStatus.AMBIGUOUS
    assert trace.side_effect_class == ToolTraceSideEffectClass.NETWORK_WRITE
    assert trace.idempotency == ToolTraceIdempotency.NON_IDEMPOTENT
    assert trace.side_effect_certainty == SideEffectCertainty.COMPLETION_UNKNOWN
    assert trace.duration_ms == 12.5
    assert trace.model_dump(mode="json")["duration_ms"] == 12.5
    assert result.model_dump(mode="json")["timing"] == {
        "started_at": "start",
        "completed_at": "end",
        "duration_ms": 12.5,
    }


@pytest.mark.asyncio
async def test_tool_bridge_records_implementation_defect_trace_before_reraising() -> (
    None
):
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        exceptions={"prepare": [RuntimeError("raw exception body")]},
    )
    bridge = _tool_bridge(executor=executor)

    with pytest.raises(RuntimeError):
        await bridge.invoke("prepare", {"path": "input.txt"})

    assert executor.call_count == 1
    assert bridge.tool_trace[0].execution_status == ToolExecutionStatus.HARD_FAILURE
    assert bridge.tool_trace[0].summary == "tool implementation defect: RuntimeError"
    assert bridge.events[-1].code == "implementation_defect"
    assert bridge.terminal_intent is None
