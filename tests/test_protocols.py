"""Tests for the Millforge protocol definitions.

Verifies structural subtyping: classes that match each protocol pass
``isinstance`` checks, classes missing required methods fail, and
protocol methods accept/return the expected contract types.
"""

from __future__ import annotations

from typing import Protocol

import pytest

from millforge.contracts import (
    CompiledHarnessIdentity,
    GuardedSessionRequest,
    GuardedSessionResult,
    HarnessExecutionResult,
    StageExecutionRequest,
    ValidatedModelRequest,
    ValidatedModelResponse,
    ValidatedToolCall,
    ValidatedToolResult,
)
from millforge.protocols import (
    GuardrailBackend,
    HarnessRuntime,
    ModelClient,
    ToolExecutor,
)

# ---------------------------------------------------------------------------
# All protocols are exported in millforge.__all__
# ---------------------------------------------------------------------------


def test_all_protocols_exported() -> None:
    from millforge import __all__ as exported

    protocol_names = {
        "GuardrailBackend",
        "HarnessRuntime",
        "ModelClient",
        "ToolExecutor",
    }
    exported_set = set(exported)
    assert protocol_names.issubset(exported_set), (
        f"Missing: {protocol_names - exported_set}"
    )


# ---------------------------------------------------------------------------
# Conforming implementations per protocol
# ---------------------------------------------------------------------------


class _ConformingHarnessRuntime:
    """Minimal conforming implementation of HarnessRuntime."""

    async def execute(self, request: StageExecutionRequest) -> HarnessExecutionResult:
        return HarnessExecutionResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            success=True,
        )

    def get_identity(self) -> CompiledHarnessIdentity:
        return CompiledHarnessIdentity(
            compiled_plan_id="plan-123",
            harness_id="harness-a",
            version="1.0.0",
        )


class _ConformingGuardrailBackend:
    """Minimal conforming implementation of GuardrailBackend."""

    async def check(self, request: GuardedSessionRequest) -> GuardedSessionResult:
        return GuardedSessionResult(
            session_id=request.session_id,
            result_type="allowed",
            payload={},
        )


class _ConformingModelClient:
    """Minimal conforming implementation of ModelClient."""

    async def send(self, request: ValidatedModelRequest) -> ValidatedModelResponse:
        return ValidatedModelResponse(
            model=request.model,
            content="Hello!",
        )


class _ConformingToolExecutor:
    """Minimal conforming implementation of ToolExecutor."""

    async def execute(self, call: ValidatedToolCall) -> ValidatedToolResult:
        return ValidatedToolResult(
            call_id=call.id,
            output=f"Executed {call.name}",
        )

    def supports_tool(self, name: str) -> bool:
        return name in {"get_weather", "search"}


# ---------------------------------------------------------------------------
# Non-conforming classes (missing one required method each)
# ---------------------------------------------------------------------------


class _HarnessRuntimeMissingExecute:
    def get_identity(self) -> CompiledHarnessIdentity:
        return CompiledHarnessIdentity(
            compiled_plan_id="plan-x",
            harness_id="harness-x",
            version="1.0.0",
        )


class _HarnessRuntimeMissingGetIdentity:
    async def execute(self, request: StageExecutionRequest) -> HarnessExecutionResult:
        return HarnessExecutionResult(
            exit_code=0,
            stdout="",
            stderr="",
            success=True,
        )


class _GuardrailBackendMissingCheck:
    pass


class _ModelClientMissingSend:
    pass


class _ToolExecutorMissingExecute:
    def supports_tool(self, name: str) -> bool:
        return False


class _ToolExecutorMissingSupportsTool:
    async def execute(self, call: ValidatedToolCall) -> ValidatedToolResult:
        return ValidatedToolResult(call_id=call.id, output="ok")


# ---------------------------------------------------------------------------
# Tests: conforming implementations pass isinstance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("protocol", "instance"),
    [
        pytest.param(HarnessRuntime, _ConformingHarnessRuntime(), id="HarnessRuntime"),
        pytest.param(
            GuardrailBackend, _ConformingGuardrailBackend(), id="GuardrailBackend"
        ),
        pytest.param(ModelClient, _ConformingModelClient(), id="ModelClient"),
        pytest.param(ToolExecutor, _ConformingToolExecutor(), id="ToolExecutor"),
    ],
)
def test_conforming_class_passes_isinstance(
    protocol: type[Protocol],
    instance: object,
) -> None:
    assert isinstance(instance, protocol), (
        f"Instance of {type(instance).__name__} should be structurally "
        f"assignable to {protocol.__name__}"
    )


# ---------------------------------------------------------------------------
# Tests: non-conforming classes fail isinstance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("protocol", "instance", "missing"),
    [
        pytest.param(
            HarnessRuntime,
            _HarnessRuntimeMissingExecute(),
            "execute",
            id="HarnessRuntime-missing-execute",
        ),
        pytest.param(
            HarnessRuntime,
            _HarnessRuntimeMissingGetIdentity(),
            "get_identity",
            id="HarnessRuntime-missing-get_identity",
        ),
        pytest.param(
            GuardrailBackend,
            _GuardrailBackendMissingCheck(),
            "check",
            id="GuardrailBackend-missing-check",
        ),
        pytest.param(
            ModelClient,
            _ModelClientMissingSend(),
            "send",
            id="ModelClient-missing-send",
        ),
        pytest.param(
            ToolExecutor,
            _ToolExecutorMissingExecute(),
            "execute",
            id="ToolExecutor-missing-execute",
        ),
        pytest.param(
            ToolExecutor,
            _ToolExecutorMissingSupportsTool(),
            "supports_tool",
            id="ToolExecutor-missing-supports_tool",
        ),
    ],
)
def test_non_conforming_class_fails_isinstance(
    protocol: type[Protocol],
    instance: object,
    missing: str,
) -> None:
    assert not isinstance(instance, protocol), (
        f"Instance of {type(instance).__name__} should NOT be structurally "
        f"assignable to {protocol.__name__} because {missing!r} is missing"
    )


# ---------------------------------------------------------------------------
# Tests: protocol methods accept/return expected contract types
#
# These tests verify that the method signatures compile correctly by
# exercising them through a conforming implementation and checking the
# returned types are the expected Millforge contract types.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("protocol", "instance"),
    [
        pytest.param(HarnessRuntime, _ConformingHarnessRuntime(), id="HarnessRuntime"),
        pytest.param(
            GuardrailBackend, _ConformingGuardrailBackend(), id="GuardrailBackend"
        ),
        pytest.param(ModelClient, _ConformingModelClient(), id="ModelClient"),
        pytest.param(ToolExecutor, _ConformingToolExecutor(), id="ToolExecutor"),
    ],
)
def test_protocol_is_runtime_checkable(
    protocol: type[Protocol],
    instance: object,
) -> None:
    """Ensure the protocol is decorated with @runtime_checkable."""
    assert hasattr(protocol, "_is_runtime_protocol"), (
        f"{protocol.__name__} is not runtime_checkable"
    )


@pytest.mark.asyncio
async def test_harness_runtime_execute_returns_expected_type() -> None:
    impl = _ConformingHarnessRuntime()
    request = StageExecutionRequest(
        request_id="req-1",
        run_id="run-1",
        stage="builder",
        task_id="task-1",
        mode_id="mode-1",
        compiled_plan_id="plan-1",
    )
    result = await impl.execute(request)
    assert isinstance(result, HarnessExecutionResult)
    assert result.exit_code == 0
    assert result.success is True


@pytest.mark.asyncio
async def test_harness_runtime_get_identity_returns_expected_type() -> None:
    impl = _ConformingHarnessRuntime()
    identity = impl.get_identity()
    assert isinstance(identity, CompiledHarnessIdentity)
    assert identity.compiled_plan_id == "plan-123"


@pytest.mark.asyncio
async def test_guardrail_backend_check_returns_expected_type() -> None:
    impl = _ConformingGuardrailBackend()
    request = GuardedSessionRequest(
        session_id="session-1",
        request_type="inference",
        payload={},
    )
    result = await impl.check(request)
    assert isinstance(result, GuardedSessionResult)
    assert result.session_id == "session-1"


@pytest.mark.asyncio
async def test_model_client_send_returns_expected_type() -> None:
    impl = _ConformingModelClient()
    request = ValidatedModelRequest(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello"}],
    )
    response = await impl.send(request)
    assert isinstance(response, ValidatedModelResponse)
    assert response.model == "gpt-4"
    assert response.content == "Hello!"


@pytest.mark.asyncio
async def test_tool_executor_execute_returns_expected_type() -> None:
    impl = _ConformingToolExecutor()
    call = ValidatedToolCall(
        id="call-1",
        name="get_weather",
        arguments={"city": "London"},
    )
    result = await impl.execute(call)
    assert isinstance(result, ValidatedToolResult)
    assert result.call_id == "call-1"


def test_tool_executor_supports_tool_returns_bool() -> None:
    impl = _ConformingToolExecutor()
    assert impl.supports_tool("get_weather") is True
    assert impl.supports_tool("unknown_tool") is False
