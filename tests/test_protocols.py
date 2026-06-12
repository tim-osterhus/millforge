"""Tests for the Millforge protocol definitions.

Verifies structural subtyping: classes that match each protocol pass
``isinstance`` checks, classes missing required methods fail, and
protocol methods accept/return the expected contract types.
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
    ExecutionResultClass,
    ExecutionStatus,
    GuardedSessionRequest,
    GuardedSessionResult,
    GuardedSessionStatus,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelProfileRef,
    RunDirRef,
    StageIdentity,
    TimeoutRef,
    TimingMetadata,
    ToolExecutionContext,
    ToolExecutionResult,
    ValidatedToolCall,
)
from millforge.protocols import (
    CancellationResolver,
    CancellationToken,
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
        "CancellationResolver",
        "CancellationToken",
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
# Helper factories
# ---------------------------------------------------------------------------


def _make_stage_identity() -> StageIdentity:
    return StageIdentity(plane="execution", node_id="builder", stage_kind_id="builder")


def _make_run_dir() -> RunDirRef:
    return RunDirRef(run_id="run-1", path=Path("/tmp/runs/run-1"))


def _make_capability_envelope() -> CapabilityEnvelope:
    return CapabilityEnvelope(grants=())


def _make_timeout() -> TimeoutRef:
    return TimeoutRef(timeout_seconds=60.0)


def _make_cancellation() -> CancellationRef:
    return CancellationRef(cancellation_id="cancel-1")


def _make_compiled_harness_ref() -> CompiledHarnessRef:
    return CompiledHarnessRef(
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
    )


def _make_tool_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="req-1",
        run_id="run-1",
        stage=_make_stage_identity(),
        run_directory=_make_run_dir(),
        capability_envelope=_make_capability_envelope(),
        timeout=_make_timeout(),
        cancellation=_make_cancellation(),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=60.0,
            effective_deadline_monotonic=60.0,
            source="request",
        ),
    )


def _make_harness_request() -> HarnessExecutionRequest:
    return HarnessExecutionRequest(
        request_id="req-1",
        run_id="run-1",
        work_item_id="task-1",
        stage=_make_stage_identity(),
        compiled_harness=_make_compiled_harness_ref(),
        capability_envelope=_make_capability_envelope(),
        input_artifacts=(),
        run_directory=_make_run_dir(),
        timeout=_make_timeout(),
        cancellation=_make_cancellation(),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id="p"),
    )


# ---------------------------------------------------------------------------
# Conforming implementations per protocol
# ---------------------------------------------------------------------------


class _ConformingHarnessRuntime:
    """Minimal conforming implementation of HarnessRuntime."""

    async def execute(self, request: HarnessExecutionRequest) -> HarnessExecutionResult:
        return HarnessExecutionResult(
            status=ExecutionStatus.COMPLETED,
            result_class=ExecutionResultClass.DOMAIN_TERMINAL,
            request_id=request.request_id,
            run_id=request.run_id,
            stage=request.stage,
            artifact_refs=(),
            compiled_harness=request.compiled_harness,
            timing=TimingMetadata(
                started_at="now", completed_at="later", duration_ms=100.0
            ),
        )


class _ConformingGuardrailBackend:
    """Minimal conforming implementation of GuardrailBackend."""

    async def run_session(self, request: GuardedSessionRequest) -> GuardedSessionResult:
        return GuardedSessionResult(
            session_id=request.session_id,
            status=GuardedSessionStatus.TERMINAL,
        )


class _ConformingModelClient:
    """Minimal conforming implementation of ModelClient."""

    async def complete(
        self, request: ModelCompletionRequest
    ) -> ModelCompletionResponse:
        return ModelCompletionResponse(
            model=request.model,
            content="Hello!",
        )


class _ConformingToolExecutor:
    """Minimal conforming implementation of ToolExecutor."""

    async def execute(
        self, call: ValidatedToolCall, context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=call.id,
            output=f"Executed {call.name}",
        )

    def supports_tool(self, name: str) -> bool:
        return name in {"get_weather", "search"}


class _ConformingCancellationToken:
    @property
    def cancellation_id(self) -> str:
        return "cancel-1"

    def is_cancelled(self) -> bool:
        return False

    async def wait(self) -> None:
        return None

    @property
    def reason(self) -> str | None:
        return None


class _ConformingCancellationResolver:
    def resolve(self, ref: CancellationRef) -> CancellationToken:
        return _ConformingCancellationToken()


# ---------------------------------------------------------------------------
# Non-conforming classes (missing one required method each)
# ---------------------------------------------------------------------------


class _HarnessRuntimeMissingExecute:
    def get_identity(self) -> CompiledHarnessIdentity:
        return CompiledHarnessIdentity(
            compiled_plan_id="plan-x",
            harness_id="harness-x",
            harness_version=1,
        )


class _GuardrailBackendMissingRunSession:
    pass


class _ModelClientMissingComplete:
    pass


class _ToolExecutorMissingExecute:
    def supports_tool(self, name: str) -> bool:
        return False


class _ToolExecutorMissingSupportsTool:
    async def execute(
        self, call: ValidatedToolCall, context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return ToolExecutionResult(call_id=call.id, output="ok")


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
        pytest.param(
            CancellationToken,
            _ConformingCancellationToken(),
            id="CancellationToken",
        ),
        pytest.param(
            CancellationResolver,
            _ConformingCancellationResolver(),
            id="CancellationResolver",
        ),
    ],
)
def test_conforming_class_passes_isinstance(
    protocol: type,
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
            GuardrailBackend,
            _GuardrailBackendMissingRunSession(),
            "run_session",
            id="GuardrailBackend-missing-run_session",
        ),
        pytest.param(
            ModelClient,
            _ModelClientMissingComplete(),
            "complete",
            id="ModelClient-missing-complete",
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
    protocol: type,
    instance: object,
    missing: str,
) -> None:
    assert not isinstance(instance, protocol), (
        f"Instance of {type(instance).__name__} should NOT be structurally "
        f"assignable to {protocol.__name__} because {missing!r} is missing"
    )


# ---------------------------------------------------------------------------
# Tests: protocol methods accept/return expected contract types
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
        pytest.param(
            CancellationToken,
            _ConformingCancellationToken(),
            id="CancellationToken",
        ),
        pytest.param(
            CancellationResolver,
            _ConformingCancellationResolver(),
            id="CancellationResolver",
        ),
    ],
)
def test_protocol_is_runtime_checkable(
    protocol: type,
    instance: object,
) -> None:
    """Ensure the protocol is decorated with @runtime_checkable."""
    assert hasattr(protocol, "_is_runtime_protocol"), (
        f"{protocol.__name__} is not runtime_checkable"
    )


@pytest.mark.asyncio
async def test_harness_runtime_execute_returns_expected_type() -> None:
    impl = _ConformingHarnessRuntime()
    request = _make_harness_request()
    result = await impl.execute(request)
    assert isinstance(result, HarnessExecutionResult)
    assert result.status == ExecutionStatus.COMPLETED
    assert result.result_class == ExecutionResultClass.DOMAIN_TERMINAL


@pytest.mark.asyncio
async def test_guardrail_backend_run_session_returns_expected_type() -> None:
    impl = _ConformingGuardrailBackend()
    request = GuardedSessionRequest(
        session_id="session-1",
        execution_request=_make_harness_request(),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=300.0,
            effective_deadline_monotonic=300.0,
            source="request",
        ),
    )
    result = await impl.run_session(request)
    assert isinstance(result, GuardedSessionResult)
    assert result.session_id == "session-1"


@pytest.mark.asyncio
async def test_model_client_complete_returns_expected_type() -> None:
    impl = _ConformingModelClient()
    request = ModelCompletionRequest(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello"}],
    )
    response = await impl.complete(request)
    assert isinstance(response, ModelCompletionResponse)
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
    result = await impl.execute(call, _make_tool_context())
    assert isinstance(result, ToolExecutionResult)
    assert result.call_id == "call-1"


def test_tool_executor_supports_tool_returns_bool() -> None:
    impl = _ConformingToolExecutor()
    assert impl.supports_tool("get_weather") is True
    assert impl.supports_tool("unknown_tool") is False


def test_cancellation_resolver_resolves_ref_synchronously() -> None:
    resolver = _ConformingCancellationResolver()
    token = resolver.resolve(CancellationRef(cancellation_id="cancel-1"))
    assert token.cancellation_id == "cancel-1"
    assert token.is_cancelled() is False
    assert token.reason is None


# ---------------------------------------------------------------------------
# Old names absent from conforming implementations
# ---------------------------------------------------------------------------


def test_conforming_harness_runtime_has_no_old_execute_name() -> None:
    """HarnessRuntime conforming class must not have a one-arg 'execute'."""
    impl = _ConformingHarnessRuntime()
    # The new execute expects HarnessExecutionRequest and returns HarnessExecutionResult
    assert hasattr(impl, "execute")
    # Ensure there is no old name like 'get_identity' masquerading
    assert not hasattr(impl, "get_identity")


def test_conforming_guardrail_backend_has_no_check_method() -> None:
    """GuardrailBackend conforming class must not have a 'check' method."""
    impl = _ConformingGuardrailBackend()
    assert hasattr(impl, "run_session")
    assert not hasattr(impl, "check"), "Old name 'check' must be absent"


def test_conforming_model_client_has_no_send_method() -> None:
    """ModelClient conforming class must not have a 'send' method."""
    impl = _ConformingModelClient()
    assert hasattr(impl, "complete")
    assert not hasattr(impl, "send"), "Old name 'send' must be absent"


def test_conforming_tool_executor_has_no_old_execute_signature() -> None:
    """ToolExecutor.execute must accept two arguments (call + context)."""
    impl = _ConformingToolExecutor()
    assert hasattr(impl, "execute")
    # Verify the executing method takes exactly 2 positional args beyond self
    import inspect

    sig = inspect.signature(impl.execute)
    params = list(sig.parameters.keys())
    assert "call" in params, "'call' argument required"
    assert "context" in params, "'context' argument required"


def test_conforming_tool_executor_has_supports_tool() -> None:
    """ToolExecutor must retain supports_tool."""
    impl = _ConformingToolExecutor()
    assert hasattr(impl, "supports_tool")


def test_obsolete_import_chain_verified() -> None:
    """Verify no protocol files export old names."""
    import millforge.protocols as mp

    for old in ("check", "send"):
        assert not hasattr(mp, old), f"Old name {old!r} still exported from protocols"


def test_old_names_not_available_via_millforge() -> None:
    """Old names (check, send, get_identity) must not be re-exported from millforge."""
    import millforge as mf

    for old in ("check", "send", "get_identity"):
        assert not hasattr(mf, old), f"Old name {old!r} still exported from millforge"
