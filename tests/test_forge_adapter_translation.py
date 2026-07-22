from __future__ import annotations

import asyncio
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
    ForgeGuardrailBackend,
    ForgeModelBridge,
    ForgeSessionInputBuilder,
    ForgeToolBridge,
    ForgeWorkflowFactory,
)
from millforge._forge.errors import (
    MaxIterationsError,
    NonRetryableToolError,
    ToolResolutionError,
)
from millforge._forge.context.manager import ContextManager
from millforge._forge.context.strategies import (
    NoCompact,
    TieredCompact,
    _estimate_tokens,
)
from millforge._forge.core.messages import (
    Message,
    MessageMeta,
    MessageRole,
    MessageType,
    ToolCallInfo,
)
from millforge._forge.core.runner import WorkflowRunner
from millforge._forge.core.workflow import ToolCall, ToolDef, ToolSpec, Workflow
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
    finalize_compiled_plan_sha256,
)
from millforge.contracts import (
    ArtifactRef,
    AssistantMessage,
    Deadline,
    GuardedSessionRequest,
    GuardedSessionStatus,
    InvalidToolArguments,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelToolCall,
    ParsedToolArguments,
    SelectedOutputAbsent,
    SelectedOutputPresent,
    SelectedOutputRequirement,
    SideEffectRecord,
    TerminalSelectedOutputRequirement,
    TimingMetadata,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolResultMessage,
    TokenUsage,
)
from millforge.model_backend import (
    AuthenticationPolicy,
    AuthenticationScheme,
    CapabilityDeclarations,
    CapabilitySupport,
    DefaultModelClient,
    EndpointConfig,
    ModelBackendConfigError,
    ModelProviderError,
    ModelRequestDeadlineExceededError,
    ProviderErrorCategory,
    ReasoningMode,
    ReasoningPolicy,
    RequestOptionAllowlist,
    ResolvedModelProfile,
    StaticModelProfileResolver,
    StaticSecretResolver,
    TransportRequest,
    TransportResponse,
)
from millforge.exceptions import DeadlineExceededError, OperationCancelledError
from millforge.testing import (
    BUILDER_WORKSPACE_FIXED,
    BUILDER_WORKSPACE_INITIAL,
    BUILDER_WORKSPACE_PATH,
    BuilderArtifactStore,
    BuilderFakeToolExecutor,
    FakeModelClient,
    FakeToolExecutor,
)
from tests.conftest import (
    FakeCancellationResolver,
    FakeClock,
    FakePlanLoader,
    SHA_A,
    SHA_B,
    make_canonical_builder_compiled_plan,
    make_canonical_builder_execution_request,
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


def _four_result_plan() -> CompiledHarnessPlan:
    terminal_results = ("ALPHA", "BETA", "DELTA", "GAMMA")
    nodes = tuple(
        _terminal_node().model_copy(
            update={
                "node_id": f"node-{index}",
                "model_tool_name": f"terminal_{terminal_result.lower()}",
                "description": f"Return opaque result {terminal_result}",
                "binding": _terminal_node().binding.model_copy(
                    update={
                        "tool_id": f"terminal.{terminal_result.lower()}",
                        "implementation_id": f"impl-terminal-{terminal_result.lower()}-v1",
                    }
                ),
                "prerequisites": (),
                "terminal_result": terminal_result,
                "produced_artifact_ids": (),
            }
        )
        for index, terminal_result in enumerate(terminal_results, start=1)
    )
    base_plan = _plan()
    plan = base_plan.model_copy(
        update={
            "compiled_sha256": SHA_B,
            "nodes": nodes,
            "terminal_result_map": {
                node.node_id: node.terminal_result for node in nodes
            },
            "artifact_policy": base_plan.artifact_policy.model_copy(
                update={"required_by_terminal": ()}
            ),
        }
    )
    return finalize_compiled_plan_sha256(plan)


def _four_result_requirements() -> tuple[TerminalSelectedOutputRequirement, ...]:
    return (
        TerminalSelectedOutputRequirement(
            terminal_result="ALPHA",
            selected_output=SelectedOutputRequirement(
                required=True,
                json_schema={
                    "type": "object",
                    "properties": {"alpha": {"const": "fixed"}},
                    "required": ["alpha"],
                    "additionalProperties": False,
                },
            ),
        ),
        TerminalSelectedOutputRequirement(
            terminal_result="BETA",
            selected_output=SelectedOutputRequirement(
                required=True,
                json_schema={"type": "array", "items": {"enum": [1, 2]}},
            ),
        ),
        TerminalSelectedOutputRequirement(
            terminal_result="GAMMA",
            selected_output=SelectedOutputRequirement(
                required=False,
                json_schema={"enum": [None, "optional"]},
            ),
        ),
    )


def _four_result_session_request(plan: CompiledHarnessPlan) -> GuardedSessionRequest:
    request = make_test_guarded_session_request()
    execution = request.execution_request
    compiled_harness = execution.compiled_harness.model_copy(
        update={
            "expected_hash": execution.compiled_harness.expected_hash.model_copy(
                update={"digest": plan.compiled_sha256}
            )
        }
    )
    return request.model_copy(
        update={
            "execution_request": execution.model_copy(
                update={
                    "compiled_harness": compiled_harness,
                    "selected_output_requirements": _four_result_requirements(),
                }
            )
        }
    )


def _four_result_tool_result(
    terminal_result: str,
    *,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=f"call-{terminal_result.lower()}",
        status=ToolExecutionStatus.SUCCESS,
        summary=f"completed {terminal_result}",
        retryable=False,
        side_effect_class=SideEffectClass.TERMINAL,
        idempotency=IdempotencyClass.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        input_sha256=hashlib.sha256(
            canonical_json_serialize(arguments).encode("utf-8")
        ).hexdigest(),
        timing=TimingMetadata(started_at="start", completed_at="end", duration_ms=0.0),
    )


def _four_result_tool_bridge(
    terminal_result: str,
) -> tuple[ForgeToolBridge, FakeToolExecutor]:
    plan = _four_result_plan()
    tool_name = f"terminal_{terminal_result.lower()}"
    executor = FakeToolExecutor(
        supported_tools={tool_name},
        results={
            f"terminal.{terminal_result.lower()}": [
                _four_result_tool_result(
                    terminal_result,
                    arguments={"summary": "done"},
                )
            ]
        },
    )
    bridge = ForgeToolBridge(
        plan=plan,
        session_request=_four_result_session_request(plan),
        executor=executor,
        cancellation_resolver=FakeCancellationResolver(),
        clock=FakeClock(monotonic_value=1.0),
    )
    return bridge, executor


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
    reasoning_content: str | None = None,
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
    assistant_fields: dict[str, Any] = {}
    if reasoning_content is not None:
        assistant_fields["reasoning_content"] = reasoning_content
    return ModelCompletionResponse(
        provider_request_id=f"provider-{call_id or 'text'}",
        model_id="profile-test",
        message=AssistantMessage(
            content=content,
            tool_calls=tool_calls,
            **assistant_fields,
        ),
        finish_reason=finish_reason,
        usage=usage,
    )


class _ReasoningTransport:
    def __init__(self, responses: list[TransportResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[TransportRequest] = []

    async def send(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class _ReasoningBackendClock:
    def monotonic(self) -> float:
        return 0.0


def _reasoning_profile(
    *,
    profile_id: str = "profile.reasoning-a",
    provider_id: str = "provider.opaque-a",
) -> ResolvedModelProfile:
    return ResolvedModelProfile(
        profile_id=profile_id,
        provider_id=provider_id,
        model_id="model.opaque-reasoning",
        endpoint=EndpointConfig(base_url="https://reasoning.example.test/v1"),
        authentication=AuthenticationPolicy(scheme=AuthenticationScheme.NONE),
        reasoning=ReasoningPolicy(
            mode=ReasoningMode.ENABLED,
            mode_field="thinking",
            mode_values={ReasoningMode.ENABLED: {"type": "enabled"}},
            tool_call_replay_field="reasoning_content",
        ),
        capabilities=CapabilityDeclarations(
            support={
                "tool_calls": CapabilitySupport.SUPPORTED,
                "system_messages": CapabilitySupport.SUPPORTED,
                "tool_result_messages": CapabilitySupport.SUPPORTED,
                "parallel_tool_calls": CapabilitySupport.UNSUPPORTED,
                "structured_output": CapabilitySupport.UNSUPPORTED,
                "reasoning_controls": CapabilitySupport.UNSUPPORTED,
                "usage_reporting": CapabilitySupport.UNKNOWN,
            }
        ),
        request_options=RequestOptionAllowlist(
            allowed_options=("parallel_tool_calls",),
        ),
        source_name="reasoning-test",
        source_digest="reasoning-test-digest",
    )


def _reasoning_transport_response(
    *,
    call_id: str,
    tool_name: str,
    arguments: object,
    continuation: str,
    content: str,
) -> TransportResponse:
    return TransportResponse(
        status_code=200,
        body={
            "model": "model.opaque-reasoning",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": continuation,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": arguments,
                                },
                            }
                        ],
                    },
                }
            ],
        },
    )


def _private_tool(
    name: str,
    *,
    prerequisites: list[str] | None = None,
) -> ToolDef:
    def run(value: str) -> str:
        return f"result:{name}:{value}"

    return ToolDef(
        spec=ToolSpec.from_json_schema(
            name,
            f"Run {name}",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        ),
        callable=run,
        prerequisites=list(prerequisites or ()),
    )


def _private_workflow(
    *,
    required_steps: list[str] | None = None,
    include_dependent: bool = False,
) -> Workflow:
    tools = {
        "work": _private_tool("work"),
        "finish": _private_tool("finish"),
    }
    if include_dependent:
        tools["dependent"] = _private_tool(
            "dependent",
            prerequisites=["work"],
        )
    return Workflow(
        name="opaque-workflow",
        description="Reasoning replay test workflow",
        tools=tools,
        required_steps=list(required_steps or ()),
        terminal_tool="finish",
        system_prompt_template="system instructions",
    )


def _reasoning_stack(
    responses: list[TransportResponse],
    *,
    profile: ResolvedModelProfile | None = None,
) -> tuple[ForgeModelBridge, _ReasoningTransport]:
    resolved_profile = profile or _reasoning_profile()
    transport = _ReasoningTransport(responses)
    client = DefaultModelClient(
        profile_resolver=StaticModelProfileResolver(
            {resolved_profile.profile_id: resolved_profile}
        ),
        secret_resolver=StaticSecretResolver({}),
        transport=transport,
        clock=_ReasoningBackendClock(),
    )
    return (
        ForgeModelBridge(
            model_client=client,
            model=resolved_profile.profile_id,
        ),
        transport,
    )


def _expected_tool_call_message(
    *,
    call_id: str,
    tool_name: str,
    continuation: str,
    content: str,
    arguments: str = '{"value":"same"}',
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            }
        ],
        "reasoning_content": continuation,
    }


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


class _ControlledCancellationToken:
    def __init__(self) -> None:
        self._cancelled = False
        self._reason: str | None = None
        self._event = asyncio.Event()
        self.wait_started = asyncio.Event()
        self.wait_finished = asyncio.Event()

    @property
    def cancellation_id(self) -> str:
        return "cancel-001"

    def is_cancelled(self) -> bool:
        return self._cancelled

    async def wait(self) -> None:
        self.wait_started.set()
        try:
            await self._event.wait()
        finally:
            self.wait_finished.set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str = "workflow cancelled") -> None:
        self._cancelled = True
        self._reason = reason
        self._event.set()


class _ControlledCancellationResolver:
    def __init__(self, token: _ControlledCancellationToken) -> None:
        self.token = token

    def resolve(self, ref: Any) -> _ControlledCancellationToken:
        assert ref.cancellation_id == self.token.cancellation_id
        return self.token


def _backend(
    *,
    model_client: Any,
    tool_executor: Any,
    token: _ControlledCancellationToken,
) -> ForgeGuardrailBackend:
    return ForgeGuardrailBackend(
        model_client=model_client,
        tool_executor=tool_executor,
        plan_loader=FakePlanLoader(plan=_plan()),
        context_factory=ForgeContextFactory(),
        clock=FakeClock(monotonic_value=1.0),
        cancellation_resolver=_ControlledCancellationResolver(token),
    )


def _guarded_request() -> GuardedSessionRequest:
    request = make_test_guarded_session_request()
    execution = request.execution_request
    compiled_harness = execution.compiled_harness.model_copy(
        update={
            "expected_hash": execution.compiled_harness.expected_hash.model_copy(
                update={"digest": _plan().compiled_sha256}
            )
        }
    )
    return request.model_copy(
        update={
            "execution_request": execution.model_copy(
                update={"compiled_harness": compiled_harness}
            )
        }
    )


async def _assert_no_cancellation_watcher() -> None:
    await asyncio.sleep(0)
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("millforge-cancellation-watcher:")
    ]


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
) -> ForgeToolBridge:
    return ForgeToolBridge(
        plan=plan or _plan(),
        session_request=session_request or make_test_guarded_session_request(),
        executor=executor
        or FakeToolExecutor(supported_tools={"prepare", "submit"}, results={}),
        cancellation_resolver=cancellation_resolver or FakeCancellationResolver(),
        clock=FakeClock(monotonic_value=monotonic_value),
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


@pytest.mark.asyncio
async def test_model_bridge_scopes_selected_output_schema_to_exact_terminal_result() -> (
    None
):
    plan = _four_result_plan()
    workflow_input = ForgeWorkflowFactory(
        {node.binding.implementation_id: _callable for node in plan.nodes}
    ).build(plan)
    client = FakeModelClient(responses=[_model_response(content="inspect")])
    bridge = ForgeModelBridge(
        model_client=client,
        model="profile-test",
        selected_output_requirements=_four_result_requirements(),
        terminal_result_by_tool=workflow_input.terminal_result_by_tool,
    )

    await bridge.send(
        [{"role": "user", "content": "run"}],
        tools=[tool.spec for tool in workflow_input.workflow.tools.values()],
    )

    schemas = {tool.name: tool.input_schema for tool in client.requests[0].tools}
    requirements = {
        item.terminal_result: item.selected_output
        for item in _four_result_requirements()
    }
    for terminal_result in ("ALPHA", "BETA", "GAMMA"):
        tool_schema = schemas[f"terminal_{terminal_result.lower()}"]
        assert (
            tool_schema["properties"]["candidate"]
            == requirements[terminal_result].json_schema
        )
        assert ("candidate" in tool_schema["required"]) is requirements[
            terminal_result
        ].required
    assert "candidate" not in schemas["terminal_delta"]["properties"]
    assert "candidate" not in schemas["terminal_delta"]["required"]


@pytest.mark.asyncio
async def test_tool_bridge_admits_selected_output_only_for_exact_terminal_result() -> (
    None
):
    alpha_bridge, alpha_executor = _four_result_tool_bridge("ALPHA")
    await alpha_bridge.invoke(
        "terminal_alpha",
        {"summary": "done", "candidate": {"alpha": "fixed"}},
        call_id="call-alpha",
    )
    assert alpha_bridge.terminal_intent is not None
    assert alpha_bridge.terminal_intent.terminal_result == "ALPHA"
    assert alpha_bridge.terminal_intent.selected_output == SelectedOutputPresent(
        value={"alpha": "fixed"}
    )
    assert alpha_bridge.terminal_intent.selected_output_schema_sha256 == (
        _four_result_requirements()[0].selected_output.schema_sha256
    )
    assert alpha_executor.calls[0].arguments == {"summary": "done"}

    gamma_bridge, _gamma_executor = _four_result_tool_bridge("GAMMA")
    await gamma_bridge.invoke(
        "terminal_gamma", {"summary": "done"}, call_id="call-gamma"
    )
    assert gamma_bridge.terminal_intent is not None
    assert gamma_bridge.terminal_intent.selected_output == SelectedOutputAbsent()
    assert gamma_bridge.terminal_intent.selected_output_schema_sha256 == (
        _four_result_requirements()[2].selected_output.schema_sha256
    )

    delta_bridge, _delta_executor = _four_result_tool_bridge("DELTA")
    await delta_bridge.invoke(
        "terminal_delta", {"summary": "done"}, call_id="call-delta"
    )
    assert delta_bridge.terminal_intent is not None
    assert delta_bridge.terminal_intent.selected_output is None
    assert delta_bridge.terminal_intent.selected_output_schema_sha256 is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_result", "arguments"),
    (
        ("ALPHA", {"summary": "done", "candidate": [1]}),
        ("BETA", {"summary": "done"}),
        ("DELTA", {"summary": "done", "candidate": None}),
    ),
)
async def test_tool_bridge_refuses_foreign_missing_or_unbound_selected_output(
    terminal_result: str,
    arguments: dict[str, Any],
) -> None:
    bridge, executor = _four_result_tool_bridge(terminal_result)

    with pytest.raises(ToolResolutionError, match="selected output"):
        await bridge.invoke(
            f"terminal_{terminal_result.lower()}",
            arguments,
            call_id=f"call-{terminal_result.lower()}",
        )

    executor.assert_not_called()
    assert bridge.terminal_candidate is None
    assert bridge.terminal_intent is None


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
    assert messages[1].content == (
        "Complete the test harness task.\n\n"
        "--- millforge request context ---\n"
        '{"input_artifacts":[{"artifact_id":"art-input-001",'
        '"content_type":"application/json","path":"millforge/input.json"}],'
        '"request_id":"req-test-001","run_id":"run-test-001",'
        '"stage":{"node_id":"builder","plane":"execution",'
        '"stage_kind_id":"builder"},"work_item_id":"task-test-001"}'
    )
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

    assert messages[1].content == session_request.execution_request.task.instruction


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


@pytest.mark.asyncio
async def test_forge_backend_rejects_tampered_plan_before_model_or_tool_calls() -> None:
    plan = _plan()
    stale = plan.model_copy(update={"harness_id": "tampered-harness"})
    request = make_test_guarded_session_request()
    model_client = FakeModelClient()
    tool_executor = FakeToolExecutor(supported_tools={"prepare", "submit"})
    backend = ForgeGuardrailBackend(
        model_client=model_client,
        tool_executor=tool_executor,
        plan_loader=FakePlanLoader(plan=stale),
        context_factory=ForgeContextFactory(),
        clock=FakeClock(monotonic_value=1.0),
        cancellation_resolver=FakeCancellationResolver(),
    )

    result = await backend.run_session(request)

    assert result.status == GuardedSessionStatus.BACKEND_FAILED
    assert result.diagnostic is not None
    assert result.diagnostic.error_code == "binding_rejected"
    model_client.assert_not_called()
    tool_executor.assert_not_called()
    await _assert_no_cancellation_watcher()


@pytest.mark.asyncio
async def test_forge_backend_propagates_live_cancellation_to_blocked_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_started = asyncio.Event()

    class _EventBlockingRunner:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def run(self, *_args: Any, **kwargs: Any) -> None:
            cancel_event = kwargs["cancel_event"]
            runner_started.set()
            await cancel_event.wait()

    monkeypatch.setattr(forge_adapter, "WorkflowRunner", _EventBlockingRunner)
    token = _ControlledCancellationToken()
    model_client = FakeModelClient()
    tool_executor = FakeToolExecutor(supported_tools={"prepare", "submit"})
    backend = _backend(
        model_client=model_client,
        tool_executor=tool_executor,
        token=token,
    )
    completion = asyncio.create_task(backend.run_session(_guarded_request()))
    await runner_started.wait()
    await token.wait_started.wait()

    token.cancel()
    result = await completion

    assert result.status is GuardedSessionStatus.CANCELLED
    assert result.terminal_intent is None
    model_client.assert_not_called()
    tool_executor.assert_not_called()
    assert token.wait_finished.is_set()
    await _assert_no_cancellation_watcher()


@pytest.mark.asyncio
async def test_forge_backend_classifies_cancellation_while_model_is_blocked() -> None:
    token = _ControlledCancellationToken()
    model_started = asyncio.Event()

    class _CancellationBlockingModelClient:
        def __init__(self) -> None:
            self.requests: list[ModelCompletionRequest] = []

        async def complete(
            self, request: ModelCompletionRequest
        ) -> ModelCompletionResponse:
            self.requests.append(request)
            model_started.set()
            await token.wait()
            raise ModelProviderError(
                category=ProviderErrorCategory.CANCELLED,
                message="Authorization: Bearer sk-model-cancel-secret",
                retryable=False,
            )

    model_client = _CancellationBlockingModelClient()
    tool_executor = FakeToolExecutor(supported_tools={"prepare", "submit"})
    backend = _backend(
        model_client=model_client,
        tool_executor=tool_executor,
        token=token,
    )
    completion = asyncio.create_task(backend.run_session(_guarded_request()))
    await model_started.wait()

    token.cancel("Authorization: Bearer sk-token-cancel-secret")
    result = await completion

    assert result.status is GuardedSessionStatus.CANCELLED
    assert result.terminal_intent is None
    assert result.diagnostic is not None
    assert result.diagnostic.category == "cancellation"
    assert "sk-model-cancel-secret" not in result.diagnostic.message
    assert "sk-token-cancel-secret" not in result.diagnostic.message
    assert model_client.requests[0].cancellation.cancellation_id == "cancel-001"
    tool_executor.assert_not_called()
    assert token.wait_finished.is_set()
    await _assert_no_cancellation_watcher()


@pytest.mark.asyncio
async def test_forge_backend_classifies_effective_deadline_while_model_is_blocked() -> (
    None
):
    token = _ControlledCancellationToken()
    transport_started = asyncio.Event()
    release_transport = asyncio.Event()

    class _DeadlineBlockingModelClient:
        async def complete(
            self, _request: ModelCompletionRequest
        ) -> ModelCompletionResponse:
            transport_started.set()
            await release_transport.wait()
            raise ModelRequestDeadlineExceededError()

    tool_executor = FakeToolExecutor(supported_tools={"prepare", "submit"})
    backend = _backend(
        model_client=_DeadlineBlockingModelClient(),
        tool_executor=tool_executor,
        token=token,
    )
    completion = asyncio.create_task(backend.run_session(_guarded_request()))
    await transport_started.wait()

    release_transport.set()
    result = await completion

    assert result.status is GuardedSessionStatus.TIMED_OUT
    assert result.terminal_intent is None
    assert result.diagnostic is not None
    assert result.diagnostic.category == "timeout"
    assert result.diagnostic.error_code == "deadline_expired"
    assert result.diagnostic.retryable is False
    tool_executor.assert_not_called()
    assert token.wait_finished.is_set()
    await _assert_no_cancellation_watcher()


@pytest.mark.asyncio
async def test_forge_backend_keeps_retryable_provider_timeout_as_model_failure() -> (
    None
):
    token = _ControlledCancellationToken()
    transport_started = asyncio.Event()
    release_transport = asyncio.Event()

    class _ProviderTimeoutBlockingModelClient:
        async def complete(
            self, _request: ModelCompletionRequest
        ) -> ModelCompletionResponse:
            transport_started.set()
            await release_transport.wait()
            raise ModelProviderError(
                category=ProviderErrorCategory.TIMEOUT,
                message="provider read timed out",
            )

    tool_executor = FakeToolExecutor(supported_tools={"prepare", "submit"})
    backend = _backend(
        model_client=_ProviderTimeoutBlockingModelClient(),
        tool_executor=tool_executor,
        token=token,
    )
    completion = asyncio.create_task(backend.run_session(_guarded_request()))
    await transport_started.wait()

    release_transport.set()
    result = await completion

    assert result.status is GuardedSessionStatus.MODEL_FAILED
    assert result.terminal_intent is None
    assert result.diagnostic is not None
    assert result.diagnostic.category == "model"
    assert result.diagnostic.error_code == "model_transport_failed"
    assert result.diagnostic.retryable is True
    tool_executor.assert_not_called()
    assert token.wait_finished.is_set()
    await _assert_no_cancellation_watcher()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_status", "expected_status", "expected_event"),
    [
        (
            ToolExecutionStatus.CANCELLED,
            GuardedSessionStatus.CANCELLED,
            SessionEventType.CANCELLED,
        ),
        (
            ToolExecutionStatus.TIMED_OUT,
            GuardedSessionStatus.TIMED_OUT,
            SessionEventType.TIMED_OUT,
        ),
    ],
)
async def test_forge_backend_preserves_interrupted_tool_evidence_and_status(
    tool_status: ToolExecutionStatus,
    expected_status: GuardedSessionStatus,
    expected_event: SessionEventType,
) -> None:
    token = _ControlledCancellationToken()
    model_client = FakeModelClient(
        responses=[
            _model_response(
                content="prepare",
                call_id="call-prepare",
                arguments={"path": "input.txt"},
            )
        ]
    )
    tool_executor = FakeToolExecutor(
        supported_tools={"prepare", "submit"},
        results={
            "prepare": [
                _tool_result(
                    "call-prepare",
                    tool_status.value,
                    status=tool_status,
                    error_code=tool_status.value,
                )
            ]
        },
    )
    backend = _backend(
        model_client=model_client,
        tool_executor=tool_executor,
        token=token,
    )

    result = await backend.run_session(_guarded_request())

    assert result.status is expected_status
    assert result.terminal_intent is None
    assert result.tool_trace[0].execution_status is tool_status
    assert any(event.event_type is expected_event for event in result.events)
    assert tool_executor.call_count == 1
    await _assert_no_cancellation_watcher()


@pytest.mark.asyncio
async def test_real_workflow_runner_stops_before_next_model_call_on_cancellation() -> (
    None
):
    token = _ControlledCancellationToken()

    class _BetweenIterationsExecutor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        def supports_tool(self, name: str) -> bool:
            return name in {"prepare", "submit"}

        async def execute(
            self, call: Any, _context: ToolExecutionContext
        ) -> ToolExecutionResult:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return _tool_result(call.call_id, "prepared")

    model_client = FakeModelClient(
        responses=[
            _model_response(
                content="prepare",
                call_id="call-prepare",
                arguments={"path": "input.txt"},
            ),
            _model_response(content="must not be called"),
        ]
    )
    tool_executor = _BetweenIterationsExecutor()
    backend = _backend(
        model_client=model_client,
        tool_executor=tool_executor,
        token=token,
    )
    completion = asyncio.create_task(backend.run_session(_guarded_request()))
    await tool_executor.started.wait()

    token.cancel("cancel between workflow iterations")
    await token.wait_finished.wait()
    tool_executor.release.set()
    result = await completion

    assert result.status is GuardedSessionStatus.CANCELLED
    assert result.terminal_intent is None
    assert model_client.call_count == 1
    assert tool_executor.calls == 1
    assert token.wait_finished.is_set()
    await _assert_no_cancellation_watcher()


@pytest.mark.asyncio
async def test_forge_backend_cancellation_wins_at_terminal_commit_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _ControlledCancellationToken()

    class _TerminalBoundaryRunner:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def run(self, workflow: Any, *_args: Any, **_kwargs: Any) -> None:
            await token.wait_started.wait()
            await workflow.get_callable("prepare")(path="input.txt")
            await workflow.get_callable("submit")(path="input.txt")
            token.cancel("cancelled before terminal commitment")

    monkeypatch.setattr(forge_adapter, "WorkflowRunner", _TerminalBoundaryRunner)
    prepare_call_id = "bridge_call_000000000"
    submit_call_id = "bridge_call_000000001"
    submit_result = _tool_result(
        submit_call_id,
        _artifact_output(),
        artifact_refs=(
            ArtifactRef(
                artifact_id="art-output-001",
                path=Path("millforge/output.json"),
                content_type="application/json",
            ),
        ),
    ).model_copy(update={"side_effect_class": SideEffectClass.TERMINAL})
    tool_executor = FakeToolExecutor(
        supported_tools={"prepare", "submit"},
        results={
            "prepare": [_tool_result(prepare_call_id, "prepared")],
            "submit": [submit_result],
        },
    )
    backend = _backend(
        model_client=FakeModelClient(),
        tool_executor=tool_executor,
        token=token,
    )

    result = await backend.run_session(_guarded_request())

    assert tool_executor.call_count == 2
    assert result.status is GuardedSessionStatus.CANCELLED
    assert result.terminal_intent is None
    assert result.artifact_refs == ()
    assert token.wait_finished.is_set()
    await _assert_no_cancellation_watcher()


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
    assert request.request_options == {"parallel_tool_calls": False}
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
    assert response[0].call_id == "model-call-001"
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
async def test_model_bridge_refuses_unallowlisted_serial_tool_call_option_before_transport() -> (
    None
):
    profile = _reasoning_profile().model_copy(
        update={"request_options": RequestOptionAllowlist()}
    )
    bridge, transport = _reasoning_stack(
        [
            _reasoning_transport_response(
                call_id="provider-call-unreachable",
                tool_name="prepare",
                arguments='{"path":"input.txt"}',
                continuation="unreachable",
                content="unreachable",
            )
        ],
        profile=profile,
    )
    spec = (
        ForgeWorkflowFactory(
            {"impl-prepare-v1": _callable, "impl-submit-v1": _callable}
        )
        .build(_plan())
        .workflow.tools["prepare"]
        .spec
    )

    with pytest.raises(ModelBackendConfigError, match="parallel_tool_calls"):
        await bridge.send([{"role": "user", "content": "run"}], tools=[spec])

    assert transport.requests == []


@pytest.mark.asyncio
async def test_three_requests_replay_two_tool_turn_continuations_and_provider_ids_in_order() -> (
    None
):
    bridge, transport = _reasoning_stack(
        [
            _reasoning_transport_response(
                call_id="provider-call-001",
                tool_name="work",
                arguments='{"value":"same"}',
                continuation="continuation one λ",
                content="ordinary one",
            ),
            _reasoning_transport_response(
                call_id="provider-call-002",
                tool_name="work",
                arguments='{"value":"same"}',
                continuation="continuation two 雪",
                content="ordinary two",
            ),
            _reasoning_transport_response(
                call_id="provider-call-003",
                tool_name="finish",
                arguments='{"value":"done"}',
                continuation="terminal continuation",
                content="ordinary terminal",
            ),
        ]
    )
    runner = WorkflowRunner(
        bridge,
        ContextManager(NoCompact(), budget_tokens=100_000),
        max_iterations=3,
    )

    result = await runner.run(_private_workflow(), "run")

    assert result == "result:finish:done"
    assert len(transport.requests) == 3
    initial_messages = [
        {"role": "system", "content": "system instructions"},
        {"role": "user", "content": "run"},
    ]
    first_turn = _expected_tool_call_message(
        call_id="provider-call-001",
        tool_name="work",
        continuation="continuation one λ",
        content="ordinary one",
    )
    first_result = {
        "role": "tool",
        "tool_call_id": "provider-call-001",
        "name": "work",
        "content": "result:work:same",
    }
    second_turn = _expected_tool_call_message(
        call_id="provider-call-002",
        tool_name="work",
        continuation="continuation two 雪",
        content="ordinary two",
    )
    second_result = {
        "role": "tool",
        "tool_call_id": "provider-call-002",
        "name": "work",
        "content": "result:work:same",
    }
    first_body = transport.requests[0].body
    assert transport.requests[1].body == {
        **first_body,
        "messages": [*initial_messages, first_turn, first_result],
    }
    assert transport.requests[2].body == {
        **first_body,
        "messages": [
            *initial_messages,
            first_turn,
            first_result,
            second_turn,
            second_result,
        ],
    }
    assert [request.profile.provider_id for request in transport.requests] == [
        "provider.opaque-a",
        "provider.opaque-a",
        "provider.opaque-a",
    ]


@pytest.mark.asyncio
async def test_repeated_sequential_tool_calls_do_not_cross_associate_continuation() -> (
    None
):
    client = FakeModelClient(
        responses=[
            _model_response(
                content="ordinary first",
                call_id="provider-repeat-1",
                arguments={"path": "same.txt"},
                reasoning_content="repeat continuation first",
            ),
            _model_response(
                content="ordinary second",
                call_id="provider-repeat-2",
                arguments={"path": "same.txt"},
                reasoning_content="repeat continuation second",
            ),
        ]
    )
    bridge = ForgeModelBridge(model_client=client, model="profile-test")

    first = await bridge.send([{"role": "user", "content": "first"}])
    second = await bridge.send([{"role": "user", "content": "second"}])

    assert first == [
        ToolCall(
            tool="prepare",
            args={"path": "same.txt"},
            reasoning="ordinary first",
            call_id="provider-repeat-1",
            reasoning_content="repeat continuation first",
        )
    ]
    assert second == [
        ToolCall(
            tool="prepare",
            args={"path": "same.txt"},
            reasoning="ordinary second",
            call_id="provider-repeat-2",
            reasoning_content="repeat continuation second",
        )
    ]
    assert not hasattr(bridge, "_pending_call_ids")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "first_tool", "first_arguments", "workflow", "expected_error"),
    (
        (
            "malformed_args",
            "work",
            "not-json",
            _private_workflow(),
            "[ToolArgValidationError] Tool call to 'work' had malformed arguments. "
            "Got args='not-json' (type: str). Required: args must be a JSON object "
            "(dict). Re-emit the tool call with args as an object — {} for no-arg "
            'tools or {"key": value} otherwise.',
        ),
        (
            "malformed_args_empty",
            "work",
            "",
            _private_workflow(),
            "[ToolArgValidationError] Tool call to 'work' had malformed arguments. "
            "Got args='' (type: str). Required: args must be a JSON object "
            "(dict). Re-emit the tool call with args as an object — {} for no-arg "
            'tools or {"key": value} otherwise.',
        ),
        (
            "unknown_tool",
            "missing",
            '{"value":"same"}',
            _private_workflow(),
            "[UnknownTool] Tool 'missing' does not exist. Available tools: work, "
            "finish. Call one of them.",
        ),
        (
            "prerequisite",
            "dependent",
            '{"value":"same"}',
            _private_workflow(include_dependent=True),
            "[PrerequisiteError] You cannot call dependent yet. You must first "
            "call: work. Call the prerequisite tool now.",
        ),
        (
            "premature_terminal",
            "finish",
            '{"value":"same"}',
            _private_workflow(required_steps=["work"]),
            "[StepEnforcementError] You cannot call finish yet. You must first "
            "complete these required steps: work. Call one of them now.",
        ),
    ),
)
async def test_each_tool_error_and_enforcement_retry_replays_exact_continuation_and_call_id(
    case: str,
    first_tool: str,
    first_arguments: object,
    workflow: Workflow,
    expected_error: str,
) -> None:
    second_tool = "work" if case == "premature_terminal" else "finish"
    bridge, transport = _reasoning_stack(
        [
            _reasoning_transport_response(
                call_id=f"provider-{case}-1",
                tool_name=first_tool,
                arguments=first_arguments,
                continuation=f"continuation {case}",
                content=f"ordinary {case}",
            ),
            _reasoning_transport_response(
                call_id=f"provider-{case}-2",
                tool_name=second_tool,
                arguments='{"value":"done"}',
                continuation=f"continuation {case} second",
                content=f"ordinary {case} second",
            ),
        ]
    )
    runner = WorkflowRunner(
        bridge,
        ContextManager(NoCompact(), budget_tokens=100_000),
        max_iterations=2,
    )

    if case == "premature_terminal":
        with pytest.raises(MaxIterationsError):
            await runner.run(workflow, "run")
    else:
        await runner.run(workflow, "run")

    assert len(transport.requests) == 2
    expected_arguments = (
        first_arguments
        if isinstance(first_arguments, str)
        else json.dumps(first_arguments)
    )
    expected_messages = [
        {"role": "system", "content": "system instructions"},
        {"role": "user", "content": "run"},
        _expected_tool_call_message(
            call_id=f"provider-{case}-1",
            tool_name=first_tool,
            continuation=f"continuation {case}",
            content=f"ordinary {case}",
            arguments=expected_arguments,
        ),
        {
            "role": "tool",
            "tool_call_id": f"provider-{case}-1",
            "name": first_tool,
            "content": expected_error,
        },
    ]
    assert transport.requests[1].body == {
        **transport.requests[0].body,
        "messages": expected_messages,
    }


def test_both_estimators_count_continuation_and_compaction_preserves_complete_turns() -> (
    None
):
    continuation = "c" * 80
    replay_turn = Message(
        role=MessageRole.ASSISTANT,
        content="",
        metadata=MessageMeta(MessageType.TOOL_CALL, step_index=0),
        tool_calls=[
            ToolCallInfo(name="work", args={"value": "same"}, call_id="call-1")
        ],
        reasoning_content=continuation,
    )
    result = Message(
        role=MessageRole.TOOL,
        content="result",
        metadata=MessageMeta(MessageType.TOOL_RESULT, step_index=0),
        tool_name="work",
        tool_call_id="call-1",
    )
    assert _estimate_tokens([replay_turn]) == len(continuation) // 4
    assert (
        ContextManager(NoCompact(), budget_tokens=100).estimate_tokens([replay_turn])
        == len(continuation) // 4
    )

    messages = [
        Message(
            MessageRole.SYSTEM,
            "system",
            MessageMeta(MessageType.SYSTEM_PROMPT),
        ),
        Message(MessageRole.USER, "user", MessageMeta(MessageType.USER_INPUT)),
        Message(
            MessageRole.ASSISTANT,
            "ordinary",
            MessageMeta(MessageType.REASONING, step_index=0),
        ),
        replay_turn,
        result,
        Message(
            MessageRole.ASSISTANT,
            "recent",
            MessageMeta(MessageType.TEXT_RESPONSE, step_index=1),
        ),
    ]
    compacted = TieredCompact(keep_recent=1)._phase3(messages, eligible_end=5)

    assert all(message.metadata.step_index != 0 for message in compacted)
    assert compacted == [messages[0], messages[1], messages[5]]


def test_compaction_preserves_tool_result_pairing_fields_for_correction_records() -> (
    None
):
    continuation = "private continuation"
    call = Message(
        role=MessageRole.ASSISTANT,
        content="ordinary",
        metadata=MessageMeta(MessageType.TOOL_CALL, step_index=0),
        tool_calls=[
            ToolCallInfo(name="work", args={"value": "same"}, call_id="call-1")
        ],
        reasoning_content=continuation,
    )
    correction = Message(
        role=MessageRole.TOOL,
        content="x" * 500,
        metadata=MessageMeta(
            MessageType.RETRY_NUDGE,
            step_index=0,
            original_type=MessageType.TOOL_RESULT,
        ),
        tool_name="work",
        tool_call_id="call-1",
    )
    phase_one = TieredCompact()._phase1(
        [
            Message(
                MessageRole.SYSTEM,
                "system",
                MessageMeta(MessageType.SYSTEM_PROMPT),
            ),
            Message(MessageRole.USER, "user", MessageMeta(MessageType.USER_INPUT)),
            call,
            correction,
        ],
        eligible_end=4,
    )

    assert phase_one[2] is call
    compacted_result = phase_one[3]
    assert compacted_result.role is MessageRole.TOOL
    assert compacted_result.tool_call_id == "call-1"
    assert compacted_result.tool_name == "work"
    assert compacted_result.metadata == correction.metadata
    assert compacted_result.content.startswith("x" * 200)
    assert continuation == phase_one[2].reasoning_content


@pytest.mark.asyncio
async def test_reasoning_continuation_never_enters_events_evidence_or_diagnostics() -> (
    None
):
    continuation = "raw private reasoning that must stay out"
    response = _model_response(
        content="ordinary",
        call_id="provider-private-1",
        arguments={"path": "input.txt"},
        reasoning_content=continuation,
    )
    translator = ForgeEventTranslator(
        session_request=make_test_guarded_session_request(),
        clock=FakeClock(monotonic_value=2.0),
    )
    bridge = ForgeModelBridge(
        model_client=FakeModelClient(responses=[response]),
        model="profile-test",
        event_translator=translator,
    )

    calls = await bridge.send([{"role": "user", "content": "run"}])

    assert continuation not in repr(response.message)
    assert continuation not in repr(calls[0])
    assert continuation not in json.dumps(
        [event.model_dump(mode="json") for event in bridge.events]
    )
    diagnostic = ModelProviderError(
        category=ProviderErrorCategory.MALFORMED_RESPONSE,
        message="reasoning continuation is malformed",
        retryable=False,
    )
    assert continuation not in str(diagnostic)
    assert all(
        continuation not in json.dumps(event.model_dump(mode="json"))
        for event in translator.events
    )


@pytest.mark.asyncio
async def test_reasoning_continuation_cannot_cross_profile_session_or_client() -> None:
    first_client = FakeModelClient(
        responses=[
            _model_response(
                content="first ordinary",
                call_id="first-call",
                arguments={"path": "same.txt"},
                reasoning_content="first continuation",
            ),
            _model_response(content="first final"),
        ]
    )
    second_client = FakeModelClient(
        responses=[
            _model_response(
                content="second ordinary",
                call_id="second-call",
                arguments={"path": "same.txt"},
                reasoning_content="second continuation",
            ),
            _model_response(content="second final"),
        ]
    )
    first_bridge = ForgeModelBridge(
        model_client=first_client,
        model="profile.first",
    )
    second_bridge = ForgeModelBridge(
        model_client=second_client,
        model="profile.second",
    )

    await first_bridge.send([{"role": "user", "content": "first"}])
    await second_bridge.send([{"role": "user", "content": "second"}])
    await first_bridge.send(
        [
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": "first ordinary",
                "reasoning_content": "first continuation",
                "tool_calls": [
                    {
                        "id": "first-call",
                        "function": {
                            "name": "prepare",
                            "arguments": {"path": "same.txt"},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "first-call",
                "tool_name": "prepare",
                "content": "first result",
            },
        ]
    )
    await second_bridge.send(
        [
            {"role": "user", "content": "second"},
            {
                "role": "assistant",
                "content": "second ordinary",
                "reasoning_content": "second continuation",
                "tool_calls": [
                    {
                        "id": "second-call",
                        "function": {
                            "name": "prepare",
                            "arguments": {"path": "same.txt"},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "second-call",
                "tool_name": "prepare",
                "content": "second result",
            },
        ]
    )

    first_dump = first_client.requests[1].model_dump(mode="json")
    second_dump = second_client.requests[1].model_dump(mode="json")
    assert first_client.requests[1].model_profile_id == "profile.first"
    assert second_client.requests[1].model_profile_id == "profile.second"
    assert "first continuation" in json.dumps(first_dump)
    assert "second continuation" not in json.dumps(first_dump)
    assert "second continuation" in json.dumps(second_dump)
    assert "first continuation" not in json.dumps(second_dump)
    fresh_client = FakeModelClient(responses=[_model_response(content="fresh")])
    fresh_bridge = ForgeModelBridge(model_client=fresh_client, model="profile.first")
    await fresh_bridge.send([{"role": "user", "content": "fresh session"}])
    assert "continuation" not in json.dumps(
        fresh_client.requests[0].model_dump(mode="json")
    )


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
    assert response[0].call_id == "model-call-invalid"


@pytest.mark.asyncio
async def test_model_returned_tool_batch_executes_serially() -> None:
    execution_order: list[str] = []
    active_invocations = 0
    maximum_active_invocations = 0
    client = FakeModelClient(
        responses=[
            ModelCompletionResponse(
                provider_request_id="provider-batch",
                model_id="profile-test",
                message=AssistantMessage(
                    content="serial batch",
                    tool_calls=(
                        ModelToolCall(
                            call_id="model-call-001",
                            name="work",
                            arguments=ParsedToolArguments(value={"value": "a"}),
                        ),
                        ModelToolCall(
                            call_id="model-call-002",
                            name="work",
                            arguments=ParsedToolArguments(value={"value": "b"}),
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelCompletionResponse(
                provider_request_id="provider-terminal",
                model_id="profile-test",
                message=AssistantMessage(
                    content="finish",
                    tool_calls=(
                        ModelToolCall(
                            call_id="model-call-003",
                            name="finish",
                            arguments=ParsedToolArguments(value={"value": "done"}),
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ]
    )
    bridge = ForgeModelBridge(model_client=client, model="profile-test")

    async def invoke(call: ToolCall) -> str:
        nonlocal active_invocations, maximum_active_invocations
        active_invocations += 1
        maximum_active_invocations = max(
            maximum_active_invocations,
            active_invocations,
        )
        execution_order.append(call.tool)
        await asyncio.sleep(0)
        active_invocations -= 1
        return f"result:{call.tool}:{call.args['value']}"

    result = await WorkflowRunner(
        bridge,
        ContextManager(NoCompact(), budget_tokens=100_000),
        max_iterations=2,
        tool_call_invoker=invoke,
    ).run(
        _private_workflow(required_steps=["work"]),
        "run",
    )

    assert result == "result:finish:done"
    assert execution_order == ["work", "work", "finish"]
    assert maximum_active_invocations == 1
    assert client.call_count == 2


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
    bridge = ForgeToolBridge(
        plan=plan,
        session_request=session_request,
        executor=executor,
        cancellation_resolver=FakeCancellationResolver(),
        clock=FakeClock(monotonic_value=1.0),
    )

    assert (
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")
        == "prepared"
    )
    assert (
        await bridge.invoke("submit", {"path": "input.txt"}, call_id="call-submit")
        == _artifact_output()
    )

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
async def test_tool_bridge_accepts_canonical_builder_fake_terminal_path(
    tmp_path: Path,
) -> None:
    plan = make_canonical_builder_compiled_plan()
    execution_request = make_canonical_builder_execution_request(tmp_path, plan=plan)
    session_request = GuardedSessionRequest(
        session_id="session-builder-001",
        execution_request=execution_request,
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=60.0,
            effective_deadline_monotonic=60.0,
            source="request",
        ),
    )
    executor = BuilderFakeToolExecutor(
        plan=plan,
        artifact_store=BuilderArtifactStore(execution_request.run_directory.path),
    )
    bridge = ForgeToolBridge(
        plan=plan,
        session_request=session_request,
        executor=executor,
        cancellation_resolver=FakeCancellationResolver(),
        clock=FakeClock(monotonic_value=1.0),
    )

    await bridge.invoke("inspect_request", {}, call_id="call-inspect_request")
    await bridge.invoke("read_plan", {}, call_id="call-read_plan")
    await bridge.invoke(
        "read_file",
        {"path": BUILDER_WORKSPACE_PATH},
        call_id="call-read_file",
    )
    await bridge.invoke(
        "apply_patch",
        {
            "path": BUILDER_WORKSPACE_PATH,
            "expected_text": BUILDER_WORKSPACE_INITIAL,
            "replacement_text": BUILDER_WORKSPACE_FIXED,
        },
        call_id="call-apply_patch",
    )
    await bridge.invoke("read_diff", {}, call_id="call-read_diff")
    await bridge.invoke(
        "run_validator", {"validator": "unit"}, call_id="call-run_validator"
    )
    await bridge.invoke(
        "write_patch_summary",
        {"summary": "fixed add", "changed_files": [BUILDER_WORKSPACE_PATH]},
        call_id="call-write_patch_summary",
    )
    await bridge.invoke(
        "write_validation_results",
        {"validator": "unit", "passed": True, "summary": "unit passed"},
        call_id="call-write_validation_results",
    )
    await bridge.invoke(
        "submit_patch",
        {"summary_artifact_ids": ["patch_summary.json", "validation_results.json"]},
        call_id="call-submit_patch",
    )

    assert executor.call_count == 9
    assert [record.call.call_id for record in executor.call_records] == [
        "call-inspect_request",
        "call-read_plan",
        "call-read_file",
        "call-apply_patch",
        "call-read_diff",
        "call-run_validator",
        "call-write_patch_summary",
        "call-write_validation_results",
        "call-submit_patch",
    ]
    assert executor.rejected_calls == []
    assert bridge.terminal_intent is not None
    assert bridge.terminal_intent.terminal_result == "BUILDER_COMPLETE"
    assert executor.call_records[-1].result.structured_data == {
        "terminal_result": "BUILDER_COMPLETE",
        "summary_artifact_ids": ["patch_summary.json", "validation_results.json"],
    }
    assert {ref.artifact_id for ref in bridge.terminal_intent.artifact_refs} == {
        "workspace_diff",
        "patch_summary.json",
        "validation_results.json",
    }
    assert bridge.tool_trace[-1].execution_status == ToolExecutionStatus.SUCCESS


@pytest.mark.asyncio
async def test_tool_bridge_forwards_runtime_owned_tool_execution_context(
    tmp_path: Path,
) -> None:
    plan = _plan()
    request = make_test_guarded_session_request()
    trusted_workspace = tmp_path / "trusted-workspace"
    trusted_artifact_root = tmp_path / "trusted-run" / "millforge"
    trusted_context = ToolExecutionContext(
        request_id=request.execution_request.request_id,
        run_id=request.execution_request.run_id,
        stage=request.execution_request.stage,
        run_directory=request.execution_request.run_directory,
        capability_envelope=request.execution_request.capability_envelope,
        timeout=request.execution_request.timeout,
        cancellation=request.execution_request.cancellation,
        deadline=request.deadline,
        workspace_root=trusted_workspace,
        artifact_root=trusted_artifact_root,
        compiled_artifact_policy=plan.artifact_policy,
        input_artifacts=request.execution_request.input_artifacts,
        work_item_id=request.execution_request.work_item_id,
        current_monotonic=0.0,
    )
    request = request.model_copy(update={"tool_execution_context": trusted_context})
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"prepare": [_tool_result("call-prepare", "prepared")]},
    )
    bridge = ForgeToolBridge(
        plan=plan,
        session_request=request,
        executor=executor,
        cancellation_resolver=FakeCancellationResolver(),
        clock=FakeClock(monotonic_value=12.5),
    )

    assert (
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")
        == "prepared"
    )

    assert executor.contexts[0].workspace_root == trusted_workspace
    assert executor.contexts[0].artifact_root == trusted_artifact_root
    assert executor.contexts[0].compiled_artifact_policy == plan.artifact_policy
    assert executor.contexts[0].current_monotonic == 12.5


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
    )

    with pytest.raises(ToolResolutionError):
        await bridge.invoke("submit", {"path": "input.txt"}, call_id="call-submit")

    assert executor.call_count == 0
    assert bridge.terminal_intent is None
    assert bridge.tool_trace[0].execution_status.value == "not_executed"
    assert bridge.tool_trace[0].side_effect_certainty.value == "not_attempted"


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
    )

    with pytest.raises(ToolResolutionError):
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

    assert executor.call_count == 0
    assert bridge.tool_trace[0].capability_decisions[0].decision.value == "denied"
    assert bridge.tool_trace[0].execution_status.value == "not_executed"
    assert bridge.tool_trace[0].side_effect_certainty.value == "not_attempted"


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
    )

    with pytest.raises(OperationCancelledError):
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

    assert executor.call_count == 0
    assert bridge.tool_trace[0].execution_status.value == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "result",
        "expected_status",
        "expected_exception",
        "expected_code",
        "expected_event",
    ),
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
            SessionEventType.TOOL_FAILED,
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
            SessionEventType.TOOL_FAILED,
        ),
        (
            _tool_result(
                "call-prepare",
                "timed out",
                status=ToolExecutionStatus.TIMED_OUT,
                error_code="timeout",
            ),
            ToolExecutionStatus.TIMED_OUT,
            DeadlineExceededError,
            "timeout",
            SessionEventType.TIMED_OUT,
        ),
        (
            _tool_result(
                "call-prepare",
                "cancelled",
                status=ToolExecutionStatus.CANCELLED,
                error_code="cancelled",
            ),
            ToolExecutionStatus.CANCELLED,
            OperationCancelledError,
            "cancelled",
            SessionEventType.CANCELLED,
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
            SessionEventType.TOOL_FAILED,
        ),
    ],
)
async def test_tool_bridge_records_failed_executor_outcomes_without_terminal_acceptance(
    result: ToolExecutionResult,
    expected_status: ToolExecutionStatus,
    expected_exception: type[Exception],
    expected_code: str,
    expected_event: SessionEventType,
) -> None:
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"prepare": [result]},
    )
    bridge = _tool_bridge(executor=executor)

    with pytest.raises(expected_exception):
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

    assert executor.call_count == 1
    assert bridge.tool_trace[0].execution_status == expected_status
    assert bridge.events[-1].event_type == expected_event
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
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

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
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

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
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

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
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

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
            "side_effect_record": SideEffectRecord(
                certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                detail_code="network_completion_unknown",
                summary="Remote write may have completed",
                retry_allowed=False,
            ),
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
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

    trace = bridge.tool_trace[0]
    assert trace.execution_status == ToolExecutionStatus.AMBIGUOUS
    assert trace.side_effect_class == ToolTraceSideEffectClass.NETWORK_WRITE
    assert trace.idempotency == ToolTraceIdempotency.NON_IDEMPOTENT
    assert trace.side_effect_certainty == SideEffectCertainty.COMPLETION_UNKNOWN
    assert trace.side_effect_detail_code == "network_completion_unknown"
    assert trace.side_effect_detail_summary == "Remote write may have completed"
    assert trace.side_effect_retry_allowed is False
    assert trace.duration_ms == 12.5
    assert trace.model_dump(mode="json")["duration_ms"] == 12.5
    assert result.model_dump(mode="json")["timing"] == {
        "started_at": "start",
        "completed_at": "end",
        "duration_ms": 12.5,
    }


@pytest.mark.asyncio
async def test_tool_bridge_records_mutating_failure_before_side_effect() -> None:
    result = _tool_result(
        "call-prepare",
        "workspace write rejected before mutation",
        status=ToolExecutionStatus.HARD_FAILURE,
        error_code="precondition_failed",
    ).model_copy(
        update={
            "side_effect_class": SideEffectClass.WORKSPACE_WRITE,
            "idempotency": IdempotencyClass.NON_IDEMPOTENT,
            "side_effect_certainty": SideEffectCertainty.CONFIRMED_ABSENT,
            "retryable": False,
        }
    )
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"prepare": [result]},
    )
    bridge = _tool_bridge(executor=executor)

    with pytest.raises(NonRetryableToolError):
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

    trace = bridge.tool_trace[0]
    assert trace.execution_status == ToolExecutionStatus.HARD_FAILURE
    assert trace.side_effect_class == ToolTraceSideEffectClass.WORKSPACE_WRITE
    assert trace.idempotency == ToolTraceIdempotency.NON_IDEMPOTENT
    assert trace.side_effect_certainty == SideEffectCertainty.CONFIRMED_ABSENT
    assert trace.retryable is False
    assert trace.side_effect_retry_allowed is None
    assert bridge.events[-1].event_type == SessionEventType.TOOL_FAILED
    assert bridge.terminal_intent is None


@pytest.mark.asyncio
async def test_tool_bridge_records_mutating_failure_after_side_effect() -> None:
    result = _tool_result(
        "call-prepare",
        "workspace write completion unknown",
        status=ToolExecutionStatus.AMBIGUOUS,
        error_code="completion_unknown",
    ).model_copy(
        update={
            "side_effect_class": SideEffectClass.WORKSPACE_WRITE,
            "idempotency": IdempotencyClass.NON_IDEMPOTENT,
            "side_effect_certainty": SideEffectCertainty.COMPLETION_UNKNOWN,
            "retryable": False,
            "side_effect_record": SideEffectRecord(
                certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                detail_code="workspace_completion_unknown",
                summary="Workspace write may have completed",
                retry_allowed=False,
            ),
        }
    )
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"prepare": [result]},
    )
    bridge = _tool_bridge(executor=executor)

    with pytest.raises(NonRetryableToolError):
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

    trace = bridge.tool_trace[0]
    assert trace.execution_status == ToolExecutionStatus.AMBIGUOUS
    assert trace.side_effect_class == ToolTraceSideEffectClass.WORKSPACE_WRITE
    assert trace.idempotency == ToolTraceIdempotency.NON_IDEMPOTENT
    assert trace.side_effect_certainty == SideEffectCertainty.COMPLETION_UNKNOWN
    assert trace.retryable is False
    assert trace.side_effect_detail_code == "workspace_completion_unknown"
    assert trace.side_effect_detail_summary == "Workspace write may have completed"
    assert trace.side_effect_retry_allowed is False
    assert bridge.events[-1].event_type == SessionEventType.TOOL_FAILED
    assert bridge.terminal_intent is None


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
        await bridge.invoke("prepare", {"path": "input.txt"}, call_id="call-prepare")

    assert executor.call_count == 1
    assert bridge.tool_trace[0].execution_status == ToolExecutionStatus.HARD_FAILURE
    assert bridge.tool_trace[0].summary == "tool implementation defect: RuntimeError"
    assert bridge.tool_trace[0].side_effect_certainty == (
        SideEffectCertainty.COMPLETION_UNKNOWN
    )
    assert (
        bridge.tool_trace[0].side_effect_detail_code
        == "implementation_completion_unknown"
    )
    assert bridge.tool_trace[0].side_effect_retry_allowed is False
    assert bridge.events[-1].code == "implementation_defect"
    assert bridge.terminal_intent is None
