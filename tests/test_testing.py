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
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelProfileRef,
    RunDirRef,
    StageIdentity,
    TimeoutRef,
    ToolExecutionContext,
    ToolExecutionResult,
    ValidatedToolCall,
)
from millforge.protocols import (
    GuardrailBackend,
    ModelClient,
    ToolExecutor,
)
from millforge.testing import (
    FakeGuardrailBackend,
    FakeModelClient,
    FakeToolExecutor,
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
        return ModelCompletionResponse(model="test", content="ok")


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
    response_a = ModelCompletionResponse(
        model="gpt-4", content="Hello!", finish_reason="stop"
    )
    response_b = ModelCompletionResponse(
        model="gpt-4", content="World!", finish_reason="stop"
    )
    client = FakeModelClient(responses=[response_a, response_b])

    result_1 = await client.complete(
        ModelCompletionRequest(
            model="gpt-4", messages=[{"role": "user", "content": "Hi"}]
        )
    )
    result_2 = await client.complete(
        ModelCompletionRequest(
            model="gpt-4", messages=[{"role": "user", "content": "Again"}]
        )
    )

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
                model="gpt-4", messages=[{"role": "user", "content": "Hi"}]
            )
        )


@pytest.mark.asyncio
async def test_fake_model_client_exception_precedes_response() -> None:
    """Exceptions are consumed before responses when both are set."""
    client = FakeModelClient(
        responses=[ModelCompletionResponse(model="gpt-4", content="ok")],
        exceptions=[ValueError("fail")],
    )

    with pytest.raises(ValueError):
        await client.complete(ModelCompletionRequest(model="gpt-4", messages=[]))


# ---------------------------------------------------------------------------
# FakeModelClient — unscripted calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_model_client_unscripted_raises_index_error() -> None:
    client = FakeModelClient()

    with pytest.raises(IndexError, match="No scripted responses remain"):
        await client.complete(ModelCompletionRequest(model="gpt-4", messages=[]))


# ---------------------------------------------------------------------------
# FakeModelClient — request recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_model_client_records_requests() -> None:
    response = ModelCompletionResponse(model="gpt-4", content="ok")
    client = FakeModelClient(responses=[response, response])

    req_1 = ModelCompletionRequest(
        model="gpt-4", messages=[{"role": "user", "content": "A"}]
    )
    req_2 = ModelCompletionRequest(
        model="gpt-4", messages=[{"role": "user", "content": "B"}]
    )

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


# ---------------------------------------------------------------------------
# FakeToolExecutor — scripted success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_tool_executor_scripted_success() -> None:
    result_a = ToolExecutionResult(call_id="call-1", output="Sunny")
    result_b = ToolExecutionResult(call_id="call-2", output="Rainy")
    executor = FakeToolExecutor(
        results={
            "get_weather": [result_a, result_b],
        }
    )

    r1 = await executor.execute(
        ValidatedToolCall(
            id="call-1", name="get_weather", arguments={"city": "London"}
        ),
        _make_tool_context(),
    )
    r2 = await executor.execute(
        ValidatedToolCall(id="call-2", name="get_weather", arguments={"city": "Paris"}),
        _make_tool_context(),
    )

    assert r1 == result_a
    assert r2 == result_b


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
            ValidatedToolCall(id="call-1", name="get_weather", arguments={}),
            _make_tool_context(),
        )


@pytest.mark.asyncio
async def test_fake_tool_executor_mixed_success_exception() -> None:
    """Exceptions and results are consumed independently per tool name."""
    result = ToolExecutionResult(call_id="call-1", output="Sunny")
    executor = FakeToolExecutor(
        results={"get_weather": [result]},
        exceptions={"search": [RuntimeError("search failed")]},
    )

    # get_weather works
    r = await executor.execute(
        ValidatedToolCall(id="call-1", name="get_weather", arguments={}),
        _make_tool_context(),
    )
    assert r == result

    # search raises
    with pytest.raises(RuntimeError, match="search failed"):
        await executor.execute(
            ValidatedToolCall(id="call-2", name="search", arguments={}),
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
            ValidatedToolCall(id="call-1", name="unknown", arguments={}),
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


# ---------------------------------------------------------------------------
# FakeToolExecutor — call recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_tool_executor_records_calls() -> None:
    result = ToolExecutionResult(call_id="call-1", output="done")
    executor = FakeToolExecutor(results={"echo": [result, result]})

    call_1 = ValidatedToolCall(id="c1", name="echo", arguments={"text": "hello"})
    call_2 = ValidatedToolCall(id="c2", name="echo", arguments={"text": "world"})

    await executor.execute(call_1, _make_tool_context())
    await executor.execute(call_2, _make_tool_context())

    assert len(executor.calls) == 2
    assert executor.calls[0] == call_1
    assert executor.calls[1] == call_2


# ---------------------------------------------------------------------------
# Determinism — same script, same outputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_model_client_deterministic() -> None:
    """Given the same script, produce the same outputs."""
    response = ModelCompletionResponse(model="gpt-4", content="Hello")
    req = ModelCompletionRequest(
        model="gpt-4", messages=[{"role": "user", "content": "Hi"}]
    )

    client_a = FakeModelClient(responses=[response])
    client_b = FakeModelClient(responses=[response])

    r_a = await client_a.complete(req)
    r_b = await client_b.complete(req)

    assert r_a == r_b


@pytest.mark.asyncio
async def test_fake_tool_executor_deterministic() -> None:
    result = ToolExecutionResult(call_id="c1", output="Sunny")
    call = ValidatedToolCall(id="c1", name="get_weather", arguments={"city": "London"})

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
