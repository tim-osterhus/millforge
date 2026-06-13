"""Tests for Millforge test doubles (fakes).

Verifies that ``FakeModelClient``, ``FakeGuardrailBackend``, and
``FakeToolExecutor`` implement their respective protocols, support
scripted success/failure scenarios, record requests, and raise clear
errors for unscripted calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from millforge.contracts import (
    AssistantMessage,
    CancellationRef,
    CapabilityEnvelope,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    Deadline,
    GuardedSessionRequest,
    GuardedSessionResult,
    GuardedSessionStatus,
    HarnessExecutionRequest,
    IdempotencyClass,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelProfileRef,
    RunDirRef,
    StageIdentity,
    SideEffectCertainty,
    SideEffectClass,
    TimeoutRef,
    TimingMetadata,
    ToolBindingRef,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolExecutionStatus,
    UserMessage,
    ValidatedToolCall,
)
from millforge.protocols import (
    GuardrailBackend,
    ModelClient,
    ToolExecutor,
)
from millforge.testing import (
    BUILDER_WORKSPACE_FIXED,
    BUILDER_WORKSPACE_INITIAL,
    BUILDER_WORKSPACE_PATH,
    BuilderArtifactStore,
    BuilderFakeToolExecutor,
    BuilderInMemoryWorkspace,
    FakeGuardrailBackend,
    FakeModelClient,
    FakeToolExecutor,
)
from tests.conftest import (
    make_canonical_builder_compiled_plan,
    make_canonical_builder_execution_request,
)

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_harness_request() -> HarnessExecutionRequest:
    return HarnessExecutionRequest(
        request_id="req-1",
        run_id="run-1",
        work_item_id="task-1",
        stage=StageIdentity(
            plane="execution", node_id="builder", stage_kind_id="builder"
        ),
        compiled_harness=CompiledHarnessRef(
            identity=CompiledHarnessIdentity(
                compiled_plan_id="plan-1",
                harness_id="harness-1",
                harness_version=1,
            ),
            path=Path("/tmp/harnesses/plan-1"),
            expected_hash=CompiledHarnessHash(
                algorithm="sha256",
                digest="abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
            ),
        ),
        capability_envelope=CapabilityEnvelope(grants=()),
        input_artifacts=(),
        run_directory=RunDirRef(run_id="run-1", path=Path("/tmp/runs/run-1")),
        timeout=TimeoutRef(timeout_seconds=60.0),
        cancellation=CancellationRef(cancellation_id="cancel-1"),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id="p"),
    )


def _make_tool_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="req-1",
        run_id="run-1",
        stage=StageIdentity(
            plane="execution", node_id="builder", stage_kind_id="builder"
        ),
        run_directory=RunDirRef(run_id="run-1", path=Path("/tmp/runs/run-1")),
        capability_envelope=CapabilityEnvelope(grants=()),
        timeout=TimeoutRef(timeout_seconds=60.0),
        cancellation=CancellationRef(cancellation_id="cancel-1"),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=60.0,
            effective_deadline_monotonic=60.0,
            source="request",
        ),
    )


def _builder_tool_context(tmp_path: Path) -> ToolExecutionContext:
    request = make_canonical_builder_execution_request(tmp_path)
    return ToolExecutionContext(
        request_id=request.request_id,
        run_id=request.run_id,
        stage=request.stage,
        run_directory=request.run_directory,
        capability_envelope=request.capability_envelope,
        timeout=request.timeout,
        cancellation=request.cancellation,
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=60.0,
            effective_deadline_monotonic=60.0,
            source="request",
        ),
    )


def _binding(tool_id: str = "echo") -> ToolBindingRef:
    return ToolBindingRef(
        tool_id=tool_id,
        tool_version=1,
        descriptor_sha256="abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        implementation_id="impl.echo.v1",
    )


def _model_request(content: str = "Hi") -> ModelCompletionRequest:
    return ModelCompletionRequest(
        request_id="req-1",
        run_id="run-1",
        model_profile_id="gpt-4",
        messages=(UserMessage(content=content),),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=60.0,
            effective_deadline_monotonic=60.0,
            source="request",
        ),
        cancellation=CancellationRef(cancellation_id="cancel-1"),
    )


def _model_response(content: str = "ok") -> ModelCompletionResponse:
    return ModelCompletionResponse(
        provider_request_id=None,
        model_id="gpt-4",
        message=AssistantMessage(content=content),
        finish_reason="stop",
    )


def _validated_call(
    call_id: str = "c1",
    name: str = "echo",
    arguments: dict[str, object] | None = None,
    binding_tool_id: str | None = None,
) -> ValidatedToolCall:
    return ValidatedToolCall(
        call_id=call_id,
        node_id=f"node-{name}",
        binding=_binding(binding_tool_id or name),
        arguments=arguments or {},
    )


def _tool_result(call_id: str = "c1", summary: str = "done") -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call_id,
        status=ToolExecutionStatus.SUCCESS,
        summary=summary,
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        input_sha256="abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        output_sha256=None,
        timing=TimingMetadata(started_at="start", completed_at="end", duration_ms=0.0),
    )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def test_all_fakes_exported() -> None:
    """All three fakes are importable from the testing module."""
    from millforge.testing import __all__ as exported

    names = {"FakeModelClient", "FakeGuardrailBackend", "FakeToolExecutor"}
    assert names.issubset(set(exported)), f"Missing: {names - set(exported)}"


def test_import_via_millforge_testing() -> None:
    """Fakes are importable via 'from millforge.testing import ...'."""
    assert FakeModelClient is not None
    assert FakeGuardrailBackend is not None
    assert FakeToolExecutor is not None


# ---------------------------------------------------------------------------
# Structural subtyping (protocol conformance)
# ---------------------------------------------------------------------------


class _ConformingModelClient:
    """Minimal conforming ModelClient (reference for isinstance checks)."""

    async def complete(
        self, request: ModelCompletionRequest
    ) -> ModelCompletionResponse:
        return _model_response()


@pytest.mark.parametrize(
    ("protocol", "instance"),
    [
        pytest.param(ModelClient, FakeModelClient(), id="FakeModelClient"),
        pytest.param(
            GuardrailBackend, FakeGuardrailBackend(), id="FakeGuardrailBackend"
        ),
        pytest.param(ToolExecutor, FakeToolExecutor(), id="FakeToolExecutor"),
    ],
)
def test_fake_passes_isinstance_check(
    protocol: type,
    instance: object,
) -> None:
    """Each fake passes isinstance against its protocol."""
    assert isinstance(instance, protocol), (
        f"{type(instance).__name__} should be structurally assignable "
        f"to {protocol.__name__}"
    )


# ---------------------------------------------------------------------------
# FakeModelClient — scripted success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_model_client_scripted_success() -> None:
    response_a = _model_response("Hello!")
    response_b = _model_response("World!")
    client = FakeModelClient(responses=[response_a, response_b])

    result_1 = await client.complete(_model_request("Hi"))
    result_2 = await client.complete(_model_request("Again"))

    assert result_1 == response_a, "First scripted response should be returned"
    assert result_2 == response_b, "Second scripted response should be returned"


# ---------------------------------------------------------------------------
# FakeModelClient — scripted failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_model_client_scripted_exception() -> None:
    exc = ValueError("model unavailable")
    client = FakeModelClient(exceptions=[exc])

    with pytest.raises(ValueError, match="model unavailable"):
        await client.complete(
            ModelCompletionRequest(
                request_id="req-1",
                run_id="run-1",
                model_profile_id="gpt-4",
                messages=(UserMessage(content="Hi"),),
                deadline=Deadline(
                    started_monotonic=0.0,
                    outer_deadline_monotonic=60.0,
                    effective_deadline_monotonic=60.0,
                    source="request",
                ),
                cancellation=CancellationRef(cancellation_id="cancel-1"),
            )
        )


@pytest.mark.asyncio
async def test_fake_model_client_exception_precedes_response() -> None:
    """Exceptions are consumed before responses when both are set."""
    client = FakeModelClient(
        responses=[_model_response("ok")],
        exceptions=[ValueError("fail")],
    )

    with pytest.raises(ValueError):
        await client.complete(_model_request())


# ---------------------------------------------------------------------------
# FakeModelClient — unscripted calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_model_client_unscripted_raises_index_error() -> None:
    client = FakeModelClient()

    with pytest.raises(IndexError, match="No scripted responses remain"):
        await client.complete(_model_request())


# ---------------------------------------------------------------------------
# FakeModelClient — request recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_model_client_records_requests() -> None:
    response = _model_response("ok")
    client = FakeModelClient(responses=[response, response])

    req_1 = _model_request("A")
    req_2 = _model_request("B")

    await client.complete(req_1)
    await client.complete(req_2)

    assert len(client.requests) == 2
    assert client.requests[0] == req_1
    assert client.requests[1] == req_2


# ---------------------------------------------------------------------------
# FakeGuardrailBackend — scripted success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_guardrail_backend_scripted_success() -> None:
    result_allowed = GuardedSessionResult(
        session_id="sess-1", status=GuardedSessionStatus.TERMINAL
    )
    result_blocked = GuardedSessionResult(
        session_id="sess-1",
        status=GuardedSessionStatus.REJECTED,
    )
    backend = FakeGuardrailBackend(responses=[result_allowed, result_blocked])

    r1 = await backend.run_session(
        GuardedSessionRequest(
            session_id="sess-1",
            execution_request=_make_harness_request(),
            deadline=Deadline(
                started_monotonic=0.0,
                outer_deadline_monotonic=300.0,
                effective_deadline_monotonic=300.0,
                source="request",
            ),
        )
    )
    r2 = await backend.run_session(
        GuardedSessionRequest(
            session_id="sess-1",
            execution_request=_make_harness_request(),
            deadline=Deadline(
                started_monotonic=0.0,
                outer_deadline_monotonic=300.0,
                effective_deadline_monotonic=300.0,
                source="request",
            ),
        )
    )

    assert r1 == result_allowed
    assert r2 == result_blocked
    assert r2.status == GuardedSessionStatus.REJECTED


# ---------------------------------------------------------------------------
# FakeGuardrailBackend — scripted failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_guardrail_backend_scripted_exception() -> None:
    backend = FakeGuardrailBackend(exceptions=[RuntimeError("guardrail error")])

    with pytest.raises(RuntimeError, match="guardrail error"):
        await backend.run_session(
            GuardedSessionRequest(
                session_id="sess-1",
                execution_request=_make_harness_request(),
                deadline=Deadline(
                    started_monotonic=0.0,
                    outer_deadline_monotonic=300.0,
                    effective_deadline_monotonic=300.0,
                    source="request",
                ),
            )
        )


# ---------------------------------------------------------------------------
# FakeGuardrailBackend — unscripted calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_guardrail_backend_unscripted_raises_index_error() -> None:
    backend = FakeGuardrailBackend()

    with pytest.raises(IndexError, match="No scripted responses remain"):
        await backend.run_session(
            GuardedSessionRequest(
                session_id="sess-1",
                execution_request=_make_harness_request(),
                deadline=Deadline(
                    started_monotonic=0.0,
                    outer_deadline_monotonic=300.0,
                    effective_deadline_monotonic=300.0,
                    source="request",
                ),
            )
        )


# ---------------------------------------------------------------------------
# FakeGuardrailBackend — request recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_guardrail_backend_records_requests() -> None:
    result = GuardedSessionResult(
        session_id="sess-1", status=GuardedSessionStatus.TERMINAL
    )
    backend = FakeGuardrailBackend(responses=[result, result])

    req_1 = GuardedSessionRequest(
        session_id="sess-1",
        execution_request=_make_harness_request(),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=300.0,
            effective_deadline_monotonic=300.0,
            source="request",
        ),
    )
    req_2 = GuardedSessionRequest(
        session_id="sess-1",
        execution_request=_make_harness_request(),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=300.0,
            effective_deadline_monotonic=300.0,
            source="request",
        ),
    )

    await backend.run_session(req_1)
    await backend.run_session(req_2)

    assert len(backend.requests) == 2
    assert backend.requests[0] == req_1
    assert backend.requests[1] == req_2


@pytest.mark.asyncio
async def test_fake_guardrail_backend_accepts_expected_cancellation_id() -> None:
    result = GuardedSessionResult(
        session_id="sess-1", status=GuardedSessionStatus.TERMINAL
    )
    backend = FakeGuardrailBackend(
        responses=[result], expected_cancellation_id="cancel-1"
    )
    request = GuardedSessionRequest(
        session_id="sess-1",
        execution_request=_make_harness_request(),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=300.0,
            effective_deadline_monotonic=300.0,
            source="request",
        ),
    )

    assert await backend.run_session(request) == result
    assert backend.requests == [request]


@pytest.mark.asyncio
async def test_fake_guardrail_backend_rejects_unexpected_cancellation_id() -> None:
    result = GuardedSessionResult(
        session_id="sess-1", status=GuardedSessionStatus.TERMINAL
    )
    backend = FakeGuardrailBackend(
        responses=[result], expected_cancellation_id="cancel-expected"
    )
    request = GuardedSessionRequest(
        session_id="sess-1",
        execution_request=_make_harness_request(),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=300.0,
            effective_deadline_monotonic=300.0,
            source="request",
        ),
    )

    with pytest.raises(AssertionError, match="Expected cancellation ID"):
        await backend.run_session(request)

    assert backend.call_count == 0
    assert backend.requests == []


@pytest.mark.asyncio
async def test_fake_guardrail_backend_cancellation_mismatch_preserves_script() -> None:
    result = GuardedSessionResult(
        session_id="sess-1", status=GuardedSessionStatus.TERMINAL
    )
    backend = FakeGuardrailBackend(
        responses=[result], expected_cancellation_id="cancel-1"
    )
    bad_execution_request = _make_harness_request().model_copy(
        update={"cancellation": CancellationRef(cancellation_id="cancel-other")}
    )
    bad_request = GuardedSessionRequest(
        session_id="sess-1",
        execution_request=bad_execution_request,
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=300.0,
            effective_deadline_monotonic=300.0,
            source="request",
        ),
    )
    good_request = GuardedSessionRequest(
        session_id="sess-1",
        execution_request=_make_harness_request(),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=300.0,
            effective_deadline_monotonic=300.0,
            source="request",
        ),
    )

    with pytest.raises(AssertionError, match="Expected cancellation ID"):
        await backend.run_session(bad_request)

    assert backend.call_count == 0
    assert await backend.run_session(good_request) == result
    assert backend.requests == [good_request]


# ---------------------------------------------------------------------------
# FakeToolExecutor — scripted success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_tool_executor_scripted_success() -> None:
    result_a = _tool_result("call-1", "Sunny")
    result_b = _tool_result("call-2", "Rainy")
    executor = FakeToolExecutor(
        results={
            "get_weather": [result_a, result_b],
        }
    )

    r1 = await executor.execute(
        _validated_call(
            call_id="call-1",
            name="get_weather",
            arguments={"city": "London"},
        ),
        _make_tool_context(),
    )
    r2 = await executor.execute(
        _validated_call(
            call_id="call-2",
            name="get_weather",
            arguments={"city": "Paris"},
        ),
        _make_tool_context(),
    )

    assert r1 == result_a
    assert r2 == result_b


@pytest.mark.asyncio
async def test_fake_tool_executor_scripts_by_canonical_binding_tool_id() -> None:
    result = _tool_result("call-1", "Prepared")
    executor = FakeToolExecutor(
        supported_tools={"prepare"},
        results={"tool.prepare": [result]},
    )

    response = await executor.execute(
        _validated_call(
            call_id="call-1",
            name="prepare",
            binding_tool_id="tool.prepare",
            arguments={"path": "input.json"},
        ),
        _make_tool_context(),
    )

    assert response == result


# ---------------------------------------------------------------------------
# FakeToolExecutor — scripted failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_tool_executor_scripted_exception() -> None:
    executor = FakeToolExecutor(
        exceptions={
            "get_weather": [ValueError("API key missing")],
        }
    )

    with pytest.raises(ValueError, match="API key missing"):
        await executor.execute(
            _validated_call(call_id="call-1", name="get_weather"),
            _make_tool_context(),
        )


@pytest.mark.asyncio
async def test_fake_tool_executor_mixed_success_exception() -> None:
    """Exceptions and results are consumed independently per tool name."""
    result = _tool_result("call-1", "Sunny")
    executor = FakeToolExecutor(
        results={"get_weather": [result]},
        exceptions={"search": [RuntimeError("search failed")]},
    )

    # get_weather works
    r = await executor.execute(
        _validated_call(call_id="call-1", name="get_weather"),
        _make_tool_context(),
    )
    assert r == result

    # search raises
    with pytest.raises(RuntimeError, match="search failed"):
        await executor.execute(
            _validated_call(call_id="call-2", name="search"),
            _make_tool_context(),
        )


# ---------------------------------------------------------------------------
# FakeToolExecutor — unscripted calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_tool_executor_unscripted_tool_raises_index_error() -> None:
    executor = FakeToolExecutor()

    with pytest.raises(
        IndexError, match="No scripted results remain for tool 'unknown'"
    ):
        await executor.execute(
            _validated_call(call_id="call-1", name="unknown"),
            _make_tool_context(),
        )


# ---------------------------------------------------------------------------
# FakeToolExecutor — supports_tool
# ---------------------------------------------------------------------------


def test_fake_tool_executor_supports_tool_default() -> None:
    """By default, supported_tools matches the keys in results."""
    executor = FakeToolExecutor(
        results={"get_weather": []},
    )
    assert executor.supports_tool("get_weather") is True
    assert executor.supports_tool("search") is False


def test_fake_tool_executor_supports_tool_explicit() -> None:
    """supported_tools can be set explicitly."""
    executor = FakeToolExecutor(
        results={"get_weather": []},
        supported_tools={"get_weather", "search"},
    )
    assert executor.supports_tool("get_weather") is True
    assert executor.supports_tool("search") is True
    assert executor.supports_tool("unknown") is False


def test_fake_tool_executor_supports_tool_empty() -> None:
    """An executor with no results and no explicit set reports nothing supported."""
    executor = FakeToolExecutor()
    assert executor.supports_tool("anything") is False


def test_fakes_expose_call_counts_and_negative_side_effect_assertions() -> None:
    model = FakeModelClient()
    backend = FakeGuardrailBackend()
    executor = FakeToolExecutor(forbidden_tools={"mutate"})

    assert model.call_count == 0
    assert backend.call_count == 0
    assert executor.call_count == 0
    model.assert_not_called()
    backend.assert_not_called()
    executor.assert_not_called()
    executor.assert_tool_not_called("mutate")


def test_builder_workspace_is_in_memory_and_path_limited() -> None:
    workspace = BuilderInMemoryWorkspace()

    assert workspace.list_files() == (BUILDER_WORKSPACE_PATH,)
    assert workspace.read_file(BUILDER_WORKSPACE_PATH) == BUILDER_WORKSPACE_INITIAL

    workspace.replace_file(BUILDER_WORKSPACE_PATH, BUILDER_WORKSPACE_FIXED)

    assert workspace.read_file(BUILDER_WORKSPACE_PATH) == BUILDER_WORKSPACE_FIXED
    assert "return a + b" in workspace.diff()
    assert workspace.mutations[0].path == BUILDER_WORKSPACE_PATH
    with pytest.raises(KeyError):
        workspace.read_file("pyproject.toml")


@pytest.mark.asyncio
async def test_builder_fake_executor_exposes_exact_compiled_tool_set(
    tmp_path: Path,
) -> None:
    plan = make_canonical_builder_compiled_plan()
    executor = BuilderFakeToolExecutor(plan=plan)

    assert {node.model_tool_name for node in plan.nodes} == {
        "inspect_request",
        "read_plan",
        "list_files",
        "read_file",
        "apply_patch",
        "read_diff",
        "run_validator",
        "write_patch_summary",
        "write_validation_results",
        "submit_patch",
        "block_builder",
    }
    assert all(executor.supports_tool(node.model_tool_name) for node in plan.nodes)
    assert executor.supports_tool("shell") is False

    rejected = await executor.invoke_raw("shell", {}, _builder_tool_context(tmp_path))

    assert rejected.status == ToolExecutionStatus.NOT_EXECUTED
    assert rejected.error_code == "uncompiled_tool"
    assert executor.call_count == 0
    assert executor.rejected_calls[0].side_effect_certainty == (
        SideEffectCertainty.NOT_ATTEMPTED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "error_code"),
    [
        ("read_file", "not-object", "malformed_arguments"),
        ("read_file", {}, "missing_required_field"),
        ("read_file", {"path": BUILDER_WORKSPACE_PATH, "extra": True}, "extra_field"),
        ("read_file", {"path": 1}, "incorrect_scalar_type"),
        (
            "write_patch_summary",
            {"summary": "fixed", "changed_files": [BUILDER_WORKSPACE_PATH, 1]},
            "incorrect_scalar_type",
        ),
        ("submit_patch", {}, "missing_required_field"),
        (
            "submit_patch",
            {"summary_artifact_ids": "patch_summary.json"},
            "incorrect_scalar_type",
        ),
        ("block_builder", {"reason": "blocked"}, "missing_required_field"),
        ("run_validator", {"validator": "integration"}, "invalid_enum_value"),
    ],
)
async def test_builder_fake_executor_rejects_bad_arguments_before_state_mutation(
    tmp_path: Path,
    tool_name: str,
    arguments: object,
    error_code: str,
) -> None:
    executor = BuilderFakeToolExecutor(plan=make_canonical_builder_compiled_plan())

    result = await executor.invoke_raw(
        tool_name,
        arguments,
        _builder_tool_context(tmp_path),
    )

    assert result.status == ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == error_code
    assert result.side_effect_certainty == SideEffectCertainty.NOT_ATTEMPTED
    assert executor.call_count == 0
    assert executor.workspace.mutations == []
    assert executor.rejected_calls[0].arguments == arguments
    assert executor.rejected_calls[0].side_effect_certainty == (
        SideEffectCertainty.NOT_ATTEMPTED
    )


@pytest.mark.asyncio
async def test_builder_fake_executor_enforces_request_constraints_without_mutation(
    tmp_path: Path,
) -> None:
    request = make_canonical_builder_execution_request(tmp_path)
    denied_request = request.model_copy(
        update={"capability_envelope": CapabilityEnvelope(grants=())}
    )
    context = ToolExecutionContext(
        request_id=denied_request.request_id,
        run_id=denied_request.run_id,
        stage=denied_request.stage,
        run_directory=denied_request.run_directory,
        capability_envelope=denied_request.capability_envelope,
        timeout=denied_request.timeout,
        cancellation=denied_request.cancellation,
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=60.0,
            effective_deadline_monotonic=60.0,
            source="request",
        ),
    )
    executor = BuilderFakeToolExecutor(plan=make_canonical_builder_compiled_plan())

    result = await executor.invoke_raw(
        "apply_patch",
        {
            "path": BUILDER_WORKSPACE_PATH,
            "expected_text": BUILDER_WORKSPACE_INITIAL,
            "replacement_text": BUILDER_WORKSPACE_FIXED,
        },
        context,
    )

    assert result.status == ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == "capability_denied"
    assert result.side_effect_certainty == SideEffectCertainty.NOT_ATTEMPTED
    assert executor.workspace.read_file(BUILDER_WORKSPACE_PATH) == (
        BUILDER_WORKSPACE_INITIAL
    )
    assert executor.call_count == 0
    assert executor.rejected_calls[0].error_code == "capability_denied"


@pytest.mark.asyncio
async def test_builder_fake_executor_rejects_apply_patch_without_matching_read(
    tmp_path: Path,
) -> None:
    context = _builder_tool_context(tmp_path)
    executor = BuilderFakeToolExecutor(plan=make_canonical_builder_compiled_plan())

    no_read = await executor.invoke_raw(
        "apply_patch",
        {
            "path": BUILDER_WORKSPACE_PATH,
            "expected_text": BUILDER_WORKSPACE_INITIAL,
            "replacement_text": BUILDER_WORKSPACE_FIXED,
        },
        context,
    )
    await executor.invoke_raw(
        "read_file",
        {"path": BUILDER_WORKSPACE_PATH},
        context,
        call_id="call-read",
    )
    mismatch = await executor.invoke_raw(
        "apply_patch",
        {
            "path": BUILDER_WORKSPACE_PATH,
            "expected_text": "def add(a, b): return 0\n",
            "replacement_text": BUILDER_WORKSPACE_FIXED,
        },
        context,
    )

    assert no_read.status == ToolExecutionStatus.NOT_EXECUTED
    assert no_read.error_code == "read_before_write_required"
    assert mismatch.status == ToolExecutionStatus.NOT_EXECUTED
    assert mismatch.error_code == "expected_text_mismatch"
    assert no_read.side_effect_certainty == SideEffectCertainty.NOT_ATTEMPTED
    assert mismatch.side_effect_certainty == SideEffectCertainty.NOT_ATTEMPTED
    assert executor.workspace.read_file(BUILDER_WORKSPACE_PATH) == (
        BUILDER_WORKSPACE_INITIAL
    )
    assert executor.workspace.mutations == []


@pytest.mark.asyncio
async def test_builder_fake_executor_mutates_only_in_memory_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    plan = make_canonical_builder_compiled_plan()
    request = make_canonical_builder_execution_request(tmp_path, plan=plan)
    context = _builder_tool_context(tmp_path)
    artifact_store = BuilderArtifactStore(request.run_directory.path)
    executor = BuilderFakeToolExecutor(plan=plan, artifact_store=artifact_store)

    await executor.invoke_raw(
        "read_file",
        {"path": BUILDER_WORKSPACE_PATH},
        context,
        call_id="call-read",
    )
    apply_result = await executor.invoke_raw(
        "apply_patch",
        {
            "path": BUILDER_WORKSPACE_PATH,
            "expected_text": BUILDER_WORKSPACE_INITIAL,
            "replacement_text": BUILDER_WORKSPACE_FIXED,
        },
        context,
        call_id="call-apply",
    )
    diff_result = await executor.invoke_raw(
        "read_diff",
        {},
        context,
        call_id="call-diff",
    )
    validation_result = await executor.invoke_raw(
        "run_validator",
        {"validator": "unit"},
        context,
        call_id="call-validator",
    )
    summary_result = await executor.invoke_raw(
        "write_patch_summary",
        {"summary": "fixed add", "changed_files": [BUILDER_WORKSPACE_PATH]},
        context,
        call_id="call-summary",
    )
    validation_artifact_result = await executor.invoke_raw(
        "write_validation_results",
        {"validator": "unit", "passed": True, "summary": "unit passed"},
        context,
        call_id="call-validation-artifact",
    )
    submit_result = await executor.invoke_raw(
        "submit_patch",
        {"summary_artifact_ids": ["patch_summary.json", "validation_results.json"]},
        context,
        call_id="call-submit",
    )

    assert executor.call_count == 7
    assert [record.call.call_id for record in executor.call_records] == [
        "call-read",
        "call-apply",
        "call-diff",
        "call-validator",
        "call-summary",
        "call-validation-artifact",
        "call-submit",
    ]
    assert apply_result.structured_data == {"mutated": True}
    assert validation_result.structured_data == {"validator": "unit", "passed": True}
    assert summary_result.structured_data == {
        "summary": "fixed add",
        "changed_files": [BUILDER_WORKSPACE_PATH],
    }
    assert validation_artifact_result.structured_data == {
        "validator": "unit",
        "passed": True,
        "summary": "unit passed",
    }
    assert submit_result.structured_data == {
        "terminal_result": "BUILDER_COMPLETE",
        "summary_artifact_ids": ["patch_summary.json", "validation_results.json"],
    }
    assert diff_result.duration_ms == 7.0
    assert summary_result.artifact_refs[0].artifact_id == "patch_summary.json"
    assert (
        (request.run_directory.path / "millforge" / "workspace_diff")
        .read_text(encoding="utf-8")
        .startswith("--- a/src/example.py")
    )
    assert (request.run_directory.path / "millforge" / "patch_summary.json").read_text(
        encoding="utf-8"
    ) == '{"changed_files":["src/example.py"],"summary":"fixed add"}\n'
    assert (
        request.run_directory.path / "millforge" / "validation_results.json"
    ).read_text(encoding="utf-8") == (
        '{"passed":true,"summary":"unit passed","validator":"unit"}\n'
    )
    assert Path(BUILDER_WORKSPACE_PATH).exists() is False


# ---------------------------------------------------------------------------
# FakeToolExecutor — call recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_tool_executor_records_calls() -> None:
    result = _tool_result("call-1", "done")
    executor = FakeToolExecutor(results={"echo": [result, result]})

    call_1 = _validated_call(
        call_id="c1",
        name="echo",
        arguments={"text": "hello"},
    )
    call_2 = _validated_call(
        call_id="c2",
        name="echo",
        arguments={"text": "world"},
    )

    await executor.execute(call_1, _make_tool_context())
    await executor.execute(call_2, _make_tool_context())

    assert len(executor.calls) == 2
    assert executor.call_count == 2
    assert executor.calls[0] == call_1
    assert executor.calls[1] == call_2
    assert executor.contexts[0] == _make_tool_context()


@pytest.mark.asyncio
async def test_fake_tool_executor_rejects_forbidden_tool_call() -> None:
    executor = FakeToolExecutor(
        results={"mutate": [_tool_result("c1", "done")]},
        forbidden_tools={"mutate"},
    )

    with pytest.raises(AssertionError, match="Forbidden tool"):
        await executor.execute(
            _validated_call(call_id="c1", name="mutate"),
            _make_tool_context(),
        )
    assert executor.call_count == 0


@pytest.mark.asyncio
async def test_fake_tool_executor_asserts_deadline_remaining() -> None:
    executor = FakeToolExecutor(
        results={"echo": [_tool_result("c1", "done")]},
        deadline_clock=lambda: 59.5,
        minimum_remaining_seconds=1.0,
    )

    with pytest.raises(AssertionError, match="Deadline remaining"):
        await executor.execute(
            _validated_call(call_id="c1", name="echo"),
            _make_tool_context(),
        )


@pytest.mark.asyncio
async def test_fake_tool_executor_accepts_expected_cancellation_id() -> None:
    result = _tool_result("c1", "done")
    executor = FakeToolExecutor(
        results={"echo": [result]}, expected_cancellation_id="cancel-1"
    )
    call = _validated_call(call_id="c1", name="echo")

    assert await executor.execute(call, _make_tool_context()) == result
    assert executor.calls == [call]


@pytest.mark.asyncio
async def test_fake_tool_executor_rejects_unexpected_cancellation_id() -> None:
    executor = FakeToolExecutor(
        results={"echo": [_tool_result("c1", "done")]},
        expected_cancellation_id="cancel-expected",
    )

    with pytest.raises(AssertionError, match="Expected cancellation ID"):
        await executor.execute(
            _validated_call(call_id="c1", name="echo"),
            _make_tool_context(),
        )

    assert executor.call_count == 0
    assert executor.contexts == []


@pytest.mark.asyncio
async def test_fake_tool_executor_cancellation_mismatch_preserves_script() -> None:
    result = _tool_result("c1", "done")
    executor = FakeToolExecutor(
        results={"echo": [result]}, expected_cancellation_id="cancel-1"
    )
    call = _validated_call(call_id="c1", name="echo")
    bad_context = _make_tool_context().model_copy(
        update={"cancellation": CancellationRef(cancellation_id="cancel-other")}
    )

    with pytest.raises(AssertionError, match="Expected cancellation ID"):
        await executor.execute(call, bad_context)

    assert executor.call_count == 0
    assert await executor.execute(call, _make_tool_context()) == result
    assert executor.calls == [call]


# ---------------------------------------------------------------------------
# Determinism — same script, same outputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_model_client_deterministic() -> None:
    """Given the same script, produce the same outputs."""
    response = _model_response("Hello")
    req = _model_request("Hi")

    client_a = FakeModelClient(responses=[response])
    client_b = FakeModelClient(responses=[response])

    r_a = await client_a.complete(req)
    r_b = await client_b.complete(req)

    assert r_a == r_b


@pytest.mark.asyncio
async def test_fake_tool_executor_deterministic() -> None:
    result = _tool_result("c1", "Sunny")
    call = _validated_call(
        call_id="c1",
        name="get_weather",
        arguments={"city": "London"},
    )

    exec_a = FakeToolExecutor(results={"get_weather": [result]})
    exec_b = FakeToolExecutor(results={"get_weather": [result]})

    r_a = await exec_a.execute(call, _make_tool_context())
    r_b = await exec_b.execute(call, _make_tool_context())

    assert r_a == r_b


# ---------------------------------------------------------------------------
# No network/filesystem - basic smoke
# ---------------------------------------------------------------------------


def test_fake_constructors_do_not_require_network() -> None:
    """Constructing fakes never requires network access."""
    FakeModelClient()
    FakeGuardrailBackend()
    FakeToolExecutor()
