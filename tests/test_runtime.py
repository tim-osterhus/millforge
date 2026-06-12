"""Runtime state transition tests and failure classification tests.

Tests for ``DefaultHarnessRuntime``:
- All 9 legal state transitions through the 9-state machine.
- All defined illegal transitions raise errors.
- The 12-origin failure classification matrix produces correct
  ``ExecutionResultClass`` for each failure root.
- Infrastructure failure handling does not produce domain terminal results.
- ``BaseException`` propagation is preserved.
- Owned exceptions are translated at the public boundary.
- ``backend.run_session()`` is called exactly once per execution.
- ``FAILED`` state reachable from all non-terminal states.
- ``INTERRUPTED`` state reachable from all non-terminal states.

All tests use fake/deterministic implementations (no real
backend, model, tool, or network). No Forge, provider SDK,
or network imports.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from millforge.artifacts import RuntimeArtifactWriter
from millforge.contracts import (
    ArtifactRef,
    CancellationRef,
    CapabilityEnvelope,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    ExecutionResultClass,
    ExecutionStatus,
    GuardedSessionResult,
    GuardedSessionStatus,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    ModelProfileRef,
    RunDirRef,
    StageIdentity,
    TimeoutRef,
)
from millforge.exceptions import (
    ArtifactWriteError,
    BackendTranslationError,
    MillforgeConfigError,
    ModelTransportError,
    OperationCancelledError,
    ToolInvokeError,
)
from millforge.runtime import (
    DefaultHarnessRuntime,
    FailureOrigin,
    RuntimeState,
    classify_failure,
    classify_guarded_session_status,
)
from millforge.testing import FakeGuardrailBackend

from tests.conftest import (
    FakeArtifactWriter,
    FakeCancellationResolver,
    FakeClock,
    FakePlanLoader,
    FakeCancellationToken,
    make_test_compiled_plan,
    make_test_harness_execution_request,
    make_test_guarded_session_result,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_HASH = "8ef5c8ffabc28b7ab2f4a910137bd256db74dd2d59d28644876bedceb72d692d"
VALID_PLAN_ID = "plan-test-001"
VALID_HARNESS_ID = "harness-test-001"
VALID_HARNESS_VERSION = 1
VALID_PROFILE_ID = "deepseek_flash_high"

# Non-terminal states (FAILED and INTERRUPTED are reachable from all)
_NON_TERMINAL_STATES: list[RuntimeState] = [
    RuntimeState.RECEIVED,
    RuntimeState.VERIFIED,
    RuntimeState.BACKEND_SESSION_CONSTRUCTED,
    RuntimeState.RUNNING,
    RuntimeState.TERMINAL_INTENT_RECEIVED,
    RuntimeState.FINALIZING,
]

_TERMINAL_STATES: list[RuntimeState] = [
    RuntimeState.COMPLETED,
    RuntimeState.FAILED,
    RuntimeState.INTERRUPTED,
]

_ALL_STATES: list[RuntimeState] = list(RuntimeState)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_harness_request(**kwargs: Any) -> HarnessExecutionRequest:
    """Build a valid HarnessExecutionRequest with optional overrides."""
    defaults: dict[str, Any] = {
        "request_id": "req-runtime-001",
        "run_id": "run-runtime-001",
        "work_item_id": "task-runtime-001",
        "hash_digest": VALID_HASH,
    }
    defaults.update(kwargs)
    return make_test_harness_execution_request(**defaults)


def _build_runtime(
    backend: FakeGuardrailBackend | None = None,
    plan_loader: FakePlanLoader | None = None,
    artifact_writer: Any | None = None,
    clock: FakeClock | None = None,
    cancellation_resolver: FakeCancellationResolver | None = None,
) -> DefaultHarnessRuntime:
    """Build a DefaultHarnessRuntime with the given fakes (defaults for missing)."""
    return DefaultHarnessRuntime(
        backend=backend or FakeGuardrailBackend(),
        plan_loader=plan_loader or FakePlanLoader(),
        artifact_writer=artifact_writer or FakeArtifactWriter(),
        clock=clock or FakeClock(),
        cancellation_resolver=cancellation_resolver or FakeCancellationResolver(),
    )


def test_default_harness_runtime_dependencies_are_keyword_only() -> None:
    """Runtime dependency injection rejects positional arguments."""
    runtime_cls: Any = DefaultHarnessRuntime
    with pytest.raises(TypeError):
        runtime_cls(
            FakeGuardrailBackend(),
            FakePlanLoader(),
            FakeArtifactWriter(),
            FakeClock(),
            FakeCancellationResolver(),
        )


def _assert_success_result(result: HarnessExecutionResult) -> None:
    """Assert the result is a success."""
    assert result.status == ExecutionStatus.COMPLETED, (
        f"Expected COMPLETED, got {result.status}"
    )
    assert result.result_class == ExecutionResultClass.DOMAIN_TERMINAL, (
        f"Expected SUCCESS, got {result.result_class}"
    )


def _assert_failure_result(
    result: HarnessExecutionResult,
    expected_status: ExecutionStatus | None = None,
    expected_result_class: ExecutionResultClass | None = None,
) -> None:
    """Assert the result is a failure (not SUCCESS/COMPLETED)."""
    assert result.result_class != ExecutionResultClass.DOMAIN_TERMINAL, (
        f"Expected non-SUCCESS result, got {result.result_class}"
    )
    if expected_status is not None:
        assert result.status == expected_status, (
            f"Expected status {expected_status}, got {result.status}"
        )
    if expected_result_class is not None:
        assert result.result_class == expected_result_class, (
            f"Expected result_class {expected_result_class}, got {result.result_class}"
        )


def _backend_with_result(
    result: GuardedSessionResult | None = None,
) -> FakeGuardrailBackend:
    """Build a backend that echoes the runtime-constructed session ID."""
    if result is None:
        result = make_test_guarded_session_result(
            session_id="sess-runtime-001",
            status=GuardedSessionStatus.TERMINAL,
            with_terminal_intent=True,
            with_events=True,
            with_tool_trace=True,
        )

    class _EchoSessionBackend(FakeGuardrailBackend):
        async def run_session(self, request: Any) -> GuardedSessionResult:
            session_result = await super().run_session(request)
            terminal_intent = session_result.terminal_intent
            if terminal_intent is not None:
                terminal_intent = terminal_intent.model_copy(
                    update={
                        "request_id": request.execution_request.request_id,
                        "run_id": request.execution_request.run_id,
                        "stage": request.execution_request.stage,
                    }
                )
            return session_result.model_copy(
                update={
                    "session_id": request.session_id,
                    "terminal_intent": terminal_intent,
                }
            )

    return _EchoSessionBackend(responses=[result])


class _RecordingRuntime(DefaultHarnessRuntime):
    """Runtime test double that records state transitions."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.transitions: list[RuntimeState] = []

    def _transition_to(self, target: RuntimeState) -> None:
        self.transitions.append(target)
        super()._transition_to(target)


def _build_recording_runtime(
    backend: FakeGuardrailBackend | None = None,
    plan_loader: FakePlanLoader | None = None,
    artifact_writer: FakeArtifactWriter | None = None,
    clock: FakeClock | None = None,
    cancellation_resolver: FakeCancellationResolver | None = None,
) -> _RecordingRuntime:
    return _RecordingRuntime(
        backend=backend or FakeGuardrailBackend(),
        plan_loader=plan_loader or FakePlanLoader(),
        artifact_writer=artifact_writer or FakeArtifactWriter(),
        clock=clock or FakeClock(),
        cancellation_resolver=cancellation_resolver or FakeCancellationResolver(),
    )


# ======================================================================
# 9-State Legal Transitions
# ======================================================================


def test_legal_transition_received_to_verified() -> None:
    """RECEIVED → VERIFIED is legal via execute()."""
    runtime = _build_runtime()
    runtime._state = RuntimeState.RECEIVED
    runtime._transition_to(RuntimeState.VERIFIED)
    assert runtime._state == RuntimeState.VERIFIED


def test_legal_transition_verified_to_backend_session_constructed() -> None:
    """VERIFIED → BACKEND_SESSION_CONSTRUCTED is legal."""
    runtime = _build_runtime()
    runtime._state = RuntimeState.VERIFIED
    runtime._transition_to(RuntimeState.BACKEND_SESSION_CONSTRUCTED)
    assert runtime._state == RuntimeState.BACKEND_SESSION_CONSTRUCTED


def test_legal_transition_backend_session_constructed_to_running() -> None:
    """BACKEND_SESSION_CONSTRUCTED → RUNNING is legal."""
    runtime = _build_runtime()
    runtime._state = RuntimeState.BACKEND_SESSION_CONSTRUCTED
    runtime._transition_to(RuntimeState.RUNNING)
    assert runtime._state == RuntimeState.RUNNING


def test_legal_transition_running_to_terminal_intent_received() -> None:
    """RUNNING → TERMINAL_INTENT_RECEIVED is legal."""
    runtime = _build_runtime()
    runtime._state = RuntimeState.RUNNING
    runtime._transition_to(RuntimeState.TERMINAL_INTENT_RECEIVED)
    assert runtime._state == RuntimeState.TERMINAL_INTENT_RECEIVED


def test_legal_transition_running_to_finalizing() -> None:
    """RUNNING → FINALIZING is legal for non-terminal finalization."""
    runtime = _build_runtime()
    runtime._state = RuntimeState.RUNNING
    runtime._transition_to(RuntimeState.FINALIZING)
    assert runtime._state == RuntimeState.FINALIZING


def test_legal_terminal_intent_path_still_reaches_finalizing() -> None:
    """RUNNING → TERMINAL_INTENT_RECEIVED → FINALIZING remains legal."""
    runtime = _build_runtime()
    runtime._state = RuntimeState.RUNNING
    runtime._transition_to(RuntimeState.TERMINAL_INTENT_RECEIVED)
    runtime._transition_to(RuntimeState.FINALIZING)
    assert runtime._state == RuntimeState.FINALIZING


def test_legal_transition_terminal_intent_received_to_finalizing() -> None:
    """TERMINAL_INTENT_RECEIVED → FINALIZING is legal."""
    runtime = _build_runtime()
    runtime._state = RuntimeState.TERMINAL_INTENT_RECEIVED
    runtime._transition_to(RuntimeState.FINALIZING)
    assert runtime._state == RuntimeState.FINALIZING


def test_legal_transition_finalizing_to_completed() -> None:
    """FINALIZING → COMPLETED is legal."""
    runtime = _build_runtime()
    runtime._state = RuntimeState.FINALIZING
    runtime._transition_to(RuntimeState.COMPLETED)
    assert runtime._state == RuntimeState.COMPLETED


@pytest.mark.asyncio
async def test_full_forward_chain_via_execute() -> None:
    """A full successful execution transitions through all 9 states
    and ends at COMPLETED."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = _backend_with_result()
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_success_result(result)
    assert result.status == ExecutionStatus.COMPLETED
    assert result.result_class == ExecutionResultClass.DOMAIN_TERMINAL

    # After a successful execution, state is COMPLETED
    assert runtime._state == RuntimeState.COMPLETED


@pytest.mark.asyncio
async def test_success_without_provider_usage_uses_empty_usage_metadata() -> None:
    """Missing provider usage preserves call counts and leaves token usage absent."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    session_result = make_test_guarded_session_result(
        session_id="sess-runtime-no-usage",
        status=GuardedSessionStatus.TERMINAL,
        with_terminal_intent=True,
    ).model_copy(update={"usage": None})
    runtime = _build_runtime(
        backend=_backend_with_result(session_result),
        plan_loader=FakePlanLoader(plan=plan),
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_success_result(result)
    assert result.usage is not None
    assert result.usage.model_dump(mode="json") == {
        "model_calls": 0,
        "tool_calls": 0,
        "token_usage": None,
    }


@pytest.mark.asyncio
async def test_domain_terminal_writes_terminal_result_artifact_class() -> None:
    """Domain terminal sessions persist terminal_result as domain_terminal."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=_backend_with_result(),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_success_result(result)
    assert len(writer.terminal_result_calls) == 1
    terminal_payload = writer.terminal_result_calls[0][1]
    assert (
        terminal_payload["result_class"] == ExecutionResultClass.DOMAIN_TERMINAL.value
    )
    assert len(writer.execution_summary_calls) == 1
    summary_payload = writer.execution_summary_calls[0][1]
    assert summary_payload["status"] == ExecutionStatus.COMPLETED.value
    assert summary_payload["result_class"] == ExecutionResultClass.DOMAIN_TERMINAL.value


@pytest.mark.asyncio
async def test_domain_rejected_writes_required_artifact_set() -> None:
    """Domain rejected sessions persist terminal artifacts as domain_rejected."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    session_result = make_test_guarded_session_result(
        session_id="sess-runtime-rejected",
        status=GuardedSessionStatus.REJECTED,
        with_terminal_intent=True,
        with_events=True,
        with_tool_trace=True,
    )
    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=_backend_with_result(session_result),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    assert result.status == ExecutionStatus.COMPLETED
    assert result.result_class == ExecutionResultClass.DOMAIN_REJECTED
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "success"
    assert len(writer.terminal_result_calls) == 1
    assert len(writer.execution_summary_calls) == 1
    assert len(writer.metrics_calls) == 1
    assert len(writer.manifest_calls) == 1
    terminal_payload = writer.terminal_result_calls[0][1]
    assert (
        terminal_payload["result_class"] == ExecutionResultClass.DOMAIN_REJECTED.value
    )
    summary_payload = writer.execution_summary_calls[0][1]
    assert summary_payload["status"] == ExecutionStatus.COMPLETED.value
    assert summary_payload["result_class"] == ExecutionResultClass.DOMAIN_REJECTED.value
    manifest_payload = writer.manifest_calls[0][1]
    manifest_ids = {
        artifact["artifact_id"] for artifact in manifest_payload["artifacts"]
    }
    assert {
        "terminal_result",
        "events",
        "tool_trace",
        "metrics",
        "execution_summary",
    }.issubset(manifest_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_status", "expected_result_class"),
    [
        (GuardedSessionStatus.TIMED_OUT, ExecutionResultClass.TIMED_OUT),
        (GuardedSessionStatus.CANCELLED, ExecutionResultClass.CANCELLED),
        (GuardedSessionStatus.BACKEND_FAILED, ExecutionResultClass.BACKEND_FAILURE),
        (GuardedSessionStatus.MODEL_FAILED, ExecutionResultClass.MODEL_FAILURE),
        (GuardedSessionStatus.TOOL_FAILED, ExecutionResultClass.TOOL_FAILURE),
        (GuardedSessionStatus.BUDGET_EXHAUSTED, ExecutionResultClass.BUDGET_EXHAUSTED),
        (
            GuardedSessionStatus.INVALID_TERMINAL,
            ExecutionResultClass.TERMINAL_RESULT_INVALID,
        ),
    ],
)
async def test_non_domain_session_results_do_not_write_terminal_result_artifact(
    session_status: GuardedSessionStatus,
    expected_result_class: ExecutionResultClass,
) -> None:
    """Non-domain guarded-session results do not persist terminal intent artifacts."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    session_result = make_test_guarded_session_result(
        session_id=f"sess-runtime-{session_status.value}",
        status=session_status,
        with_terminal_intent=True,
    )
    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=_backend_with_result(session_result),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_result_class=expected_result_class,
    )
    assert writer.terminal_result_calls == []
    assert len(writer.execution_summary_calls) == 1
    assert len(writer.metrics_calls) == 1
    assert len(writer.manifest_calls) == 1
    assert writer.events_calls == []
    assert writer.tool_trace_calls == []

    summary_payload = writer.execution_summary_calls[0][1]
    assert summary_payload["status"] == result.status.value
    assert summary_payload["result_class"] == expected_result_class.value
    metrics_payload = writer.metrics_calls[0][1]
    assert isinstance(metrics_payload["session_id"], str)
    assert metrics_payload["session_id"]
    assert metrics_payload["status"] == session_status.value
    manifest_ids = {
        artifact["artifact_id"] for artifact in writer.manifest_calls[0][1]["artifacts"]
    }
    assert manifest_ids == {"execution_summary", "metrics"}


@pytest.mark.asyncio
async def test_non_domain_session_result_finalizes_from_running_without_terminal_intent_state() -> (
    None
):
    """Non-domain backend results enter FINALIZING directly from RUNNING."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    session_result = make_test_guarded_session_result(
        status=GuardedSessionStatus.TIMED_OUT,
        with_terminal_intent=True,
    )
    runtime = _build_recording_runtime(
        backend=_backend_with_result(session_result),
        plan_loader=FakePlanLoader(plan=plan),
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.INTERRUPTED,
        expected_result_class=ExecutionResultClass.TIMED_OUT,
    )
    assert RuntimeState.TERMINAL_INTENT_RECEIVED not in runtime.transitions
    assert runtime.transitions.index(RuntimeState.RUNNING) < runtime.transitions.index(
        RuntimeState.FINALIZING
    )


@pytest.mark.asyncio
async def test_post_prepare_runtime_failure_writes_non_terminal_artifacts(
    tmp_path: Path,
) -> None:
    """Runtime-owned failures after run-directory preparation finalize artifacts."""
    writer = RuntimeArtifactWriter(tmp_path, producer="test/v1")
    runtime = _build_runtime(
        plan_loader=FakePlanLoader(exception=RuntimeError("loader exploded")),
        artifact_writer=writer,
    )
    request = _valid_harness_request().model_copy(
        update={"run_directory": RunDirRef(run_id="run-runtime-001", path=tmp_path)}
    )

    result = await runtime.execute(request)

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.INTERNAL_FAILURE
    millforge_dir = tmp_path / "millforge"
    assert not (millforge_dir / "terminal_result.json").exists()
    assert (millforge_dir / "execution_summary.json").exists()
    assert (millforge_dir / "metrics.json").exists()
    assert (millforge_dir / "artifact_manifest.json").exists()
    assert (millforge_dir / "diagnostic.json").exists()

    summary = json.loads((millforge_dir / "execution_summary.json").read_text())
    assert summary["result_class"] == ExecutionResultClass.INTERNAL_FAILURE.value
    assert summary["diagnostic_error_code"] == "infrastructure_failure"
    metrics = json.loads((millforge_dir / "metrics.json").read_text())
    assert metrics["status"] == ExecutionStatus.FAILED.value
    diagnostic = json.loads((millforge_dir / "diagnostic.json").read_text())
    assert set(diagnostic) == {"schema_version", "diagnostic"}
    assert diagnostic["schema_version"] == "1.0"
    diagnostic_payload = diagnostic["diagnostic"]
    assert set(diagnostic_payload) == {
        "category",
        "error_code",
        "fields",
        "message",
        "origin",
        "retryable",
    }
    assert diagnostic_payload["error_code"] == "infrastructure_failure"
    assert diagnostic_payload["category"] == "internal"
    assert diagnostic_payload["origin"] == "infrastructure_failure"
    assert diagnostic_payload["retryable"] is True
    assert diagnostic_payload["fields"] == []
    assert diagnostic_payload["message"] == "Plan load failed: loader exploded"
    manifest = json.loads((millforge_dir / "artifact_manifest.json").read_text())
    manifest_ids = {artifact["artifact_id"] for artifact in manifest["artifacts"]}
    assert manifest_ids == {"execution_summary", "metrics", "diagnostic"}


@pytest.mark.asyncio
async def test_non_terminal_artifact_finalization_failure_is_classified() -> None:
    """Failures while finalizing non-terminal artifacts do not write terminal_result."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    session_result = make_test_guarded_session_result(
        session_id="sess-runtime-timeout",
        status=GuardedSessionStatus.TIMED_OUT,
        with_terminal_intent=True,
    )

    class _ManifestFailingArtifactWriter(FakeArtifactWriter):
        async def write_artifact_manifest(self, ref: ArtifactRef, data: Any) -> None:
            raise ArtifactWriteError("manifest write failed")

    writer = _ManifestFailingArtifactWriter()
    runtime = _build_runtime(
        backend=_backend_with_result(session_result),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.ARTIFACT_FINALIZATION_FAILED
    assert writer.terminal_result_calls == []


@pytest.mark.asyncio
async def test_run_directory_created_before_plan_load(tmp_path: Path) -> None:
    """Runtime prepares run_directory/millforge before loading the plan."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )

    class _AssertingPlanLoader(FakePlanLoader):
        async def load(self, ref: Any) -> Any:
            assert (tmp_path / "millforge").is_dir()
            return await super().load(ref)

    backend = _backend_with_result()
    runtime = _build_runtime(
        backend=backend,
        plan_loader=_AssertingPlanLoader(plan=plan),
    )
    request = _valid_harness_request(
        run_id="run-runtime-dir",
        hash_digest=plan.compiled_sha256,
    ).model_copy(
        update={
            "run_directory": RunDirRef(
                run_id="run-runtime-dir",
                path=tmp_path,
            )
        }
    )
    input_target = tmp_path / "millforge/input.json"
    input_target.parent.mkdir(parents=True, exist_ok=True)
    input_target.write_text('{"schema_version":"test"}\n', encoding="utf-8")

    result = await runtime.execute(request)

    assert result.result_class == ExecutionResultClass.DOMAIN_TERMINAL
    assert (tmp_path / "millforge").is_dir()


@pytest.mark.asyncio
async def test_verified_recorded_after_stage_profile_capability_and_input_checks() -> (
    None
):
    """A pre-verified preflight failure must not record VERIFIED."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
        stage_kind_ids=("checker",),
    )
    backend = FakeGuardrailBackend()
    runtime = _build_recording_runtime(
        backend=backend,
        plan_loader=FakePlanLoader(plan=plan),
    )
    request = _valid_harness_request(hash_digest=plan.compiled_sha256)

    result = await runtime.execute(request)

    assert result.result_class == ExecutionResultClass.COMPILED_HARNESS_INVALID
    assert RuntimeState.VERIFIED not in runtime.transitions
    assert len(backend.requests) == 0


@pytest.mark.asyncio
async def test_input_artifact_preflight_failure_is_before_verified(
    tmp_path: Path,
) -> None:
    """Input artifact path failures must not record VERIFIED."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    backend = FakeGuardrailBackend()
    runtime = _build_recording_runtime(
        backend=backend,
        plan_loader=FakePlanLoader(plan=plan),
    )
    request = _valid_harness_request(hash_digest=plan.compiled_sha256).model_copy(
        update={"run_directory": RunDirRef(run_id="run-runtime-001", path=tmp_path)}
    )

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.BINDING_REJECTED,
    )
    assert RuntimeState.VERIFIED not in runtime.transitions
    assert len(backend.requests) == 0


@pytest.mark.asyncio
async def test_running_recorded_before_backend_invocation() -> None:
    """RUNNING is visible to the backend during run_session."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    runtime_ref: _RecordingRuntime | None = None

    class _AssertingBackend(FakeGuardrailBackend):
        async def run_session(self, request: Any) -> GuardedSessionResult:
            assert runtime_ref is not None
            assert runtime_ref._state == RuntimeState.RUNNING
            session_result = await super().run_session(request)
            return session_result.model_copy(update={"session_id": request.session_id})

    backend = _AssertingBackend(responses=[make_test_guarded_session_result()])
    runtime = _build_recording_runtime(
        backend=backend,
        plan_loader=FakePlanLoader(plan=plan),
    )
    runtime_ref = runtime
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_success_result(result)
    assert runtime.transitions.index(RuntimeState.RUNNING) < runtime.transitions.index(
        RuntimeState.TERMINAL_INTENT_RECEIVED
    )


@pytest.mark.asyncio
async def test_pre_backend_cancellation_recheck_prevents_backend_call() -> None:
    """Cancellation after initial preflight but before invocation is cancelled."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )

    class _SecondCheckCancelledToken(FakeCancellationToken):
        def __init__(self) -> None:
            super().__init__(is_cancelled_return=False)
            self.checks = 0

        def is_cancelled(self) -> bool:
            self.checks += 1
            return self.checks >= 2

    class _Resolver(FakeCancellationResolver):
        def __init__(self) -> None:
            super().__init__()
            self.token = _SecondCheckCancelledToken()

        def resolve(self, ref: CancellationRef) -> FakeCancellationToken:
            self.resolve_calls.append(ref)
            return self.token

    backend = FakeGuardrailBackend()
    runtime = _build_runtime(
        backend=backend,
        plan_loader=FakePlanLoader(plan=plan),
        cancellation_resolver=_Resolver(),
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    assert result.status == ExecutionStatus.INTERRUPTED
    assert result.result_class == ExecutionResultClass.CANCELLED
    assert len(backend.requests) == 0


@pytest.mark.asyncio
async def test_backend_operation_cancelled_classifies_as_cancelled() -> None:
    """Backend-owned OperationCancelledError is public cancellation."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    backend = FakeGuardrailBackend(
        exceptions=[OperationCancelledError("backend cancelled")]
    )
    runtime = _build_runtime(backend=backend, plan_loader=FakePlanLoader(plan=plan))
    request = _valid_harness_request()

    result = await runtime.execute(request)

    assert result.status == ExecutionStatus.INTERRUPTED
    assert result.result_class == ExecutionResultClass.CANCELLED
    assert result.terminal_intent is None


@pytest.mark.asyncio
async def test_returned_session_id_mismatch_is_invalid_terminal() -> None:
    """Backend must return the same session_id the runtime constructed."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    session_result = make_test_guarded_session_result(session_id="sess-wrong")
    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=FakeGuardrailBackend(responses=[session_result]),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.TERMINAL_RESULT_INVALID
    assert result.terminal_intent is None
    assert writer.terminal_result_calls == []


@pytest.mark.asyncio
async def test_terminal_intent_identity_mismatch_is_invalid_terminal() -> None:
    """Terminal intents must match the execution request identity."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    session_result = make_test_guarded_session_result(
        session_id="sess-runtime-001",
        request_id="req-other",
    )

    class _EchoSessionOnlyBackend(FakeGuardrailBackend):
        async def run_session(self, request: Any) -> GuardedSessionResult:
            result = await super().run_session(request)
            return result.model_copy(update={"session_id": request.session_id})

    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=_EchoSessionOnlyBackend(responses=[session_result]),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.TERMINAL_RESULT_INVALID
    assert result.terminal_intent is None
    assert writer.terminal_result_calls == []


# ======================================================================
# Illegal Transitions — from terminal states
# ======================================================================


@pytest.mark.parametrize("terminal_state", _TERMINAL_STATES)
def test_illegal_transition_from_terminal_state(terminal_state: RuntimeState) -> None:
    """Transitioning from any terminal state raises MillforgeConfigError."""
    runtime = _build_runtime()
    runtime._state = terminal_state

    with pytest.raises(MillforgeConfigError, match="Illegal transition from terminal"):
        runtime._transition_to(RuntimeState.RECEIVED)


@pytest.mark.parametrize("terminal_state", _TERMINAL_STATES)
def test_illegal_transition_to_failed_from_terminal(
    terminal_state: RuntimeState,
) -> None:
    """Even FAILED is illegal from a terminal state."""
    runtime = _build_runtime()
    runtime._state = terminal_state

    with pytest.raises(MillforgeConfigError, match="Illegal transition from terminal"):
        runtime._transition_to(RuntimeState.FAILED)


@pytest.mark.parametrize("terminal_state", _TERMINAL_STATES)
def test_illegal_transition_to_interrupted_from_terminal(
    terminal_state: RuntimeState,
) -> None:
    """Even INTERRUPTED is illegal from a terminal state."""
    runtime = _build_runtime()
    runtime._state = terminal_state

    with pytest.raises(MillforgeConfigError, match="Illegal transition from terminal"):
        runtime._transition_to(RuntimeState.INTERRUPTED)


# ======================================================================
# Illegal Forward Transitions — wrong forward paths
# ======================================================================


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (RuntimeState.RECEIVED, RuntimeState.BACKEND_SESSION_CONSTRUCTED),
        (RuntimeState.RECEIVED, RuntimeState.RUNNING),
        (RuntimeState.RECEIVED, RuntimeState.TERMINAL_INTENT_RECEIVED),
        (RuntimeState.RECEIVED, RuntimeState.FINALIZING),
        (RuntimeState.RECEIVED, RuntimeState.COMPLETED),
        (RuntimeState.VERIFIED, RuntimeState.RECEIVED),
        (RuntimeState.VERIFIED, RuntimeState.RUNNING),
        (RuntimeState.VERIFIED, RuntimeState.TERMINAL_INTENT_RECEIVED),
        (RuntimeState.VERIFIED, RuntimeState.FINALIZING),
        (RuntimeState.VERIFIED, RuntimeState.COMPLETED),
        (RuntimeState.BACKEND_SESSION_CONSTRUCTED, RuntimeState.VERIFIED),
        (
            RuntimeState.BACKEND_SESSION_CONSTRUCTED,
            RuntimeState.TERMINAL_INTENT_RECEIVED,
        ),
        (RuntimeState.BACKEND_SESSION_CONSTRUCTED, RuntimeState.FINALIZING),
        (RuntimeState.BACKEND_SESSION_CONSTRUCTED, RuntimeState.COMPLETED),
        (RuntimeState.RUNNING, RuntimeState.VERIFIED),
        (RuntimeState.RUNNING, RuntimeState.BACKEND_SESSION_CONSTRUCTED),
        (RuntimeState.RUNNING, RuntimeState.COMPLETED),
        (RuntimeState.TERMINAL_INTENT_RECEIVED, RuntimeState.RECEIVED),
        (RuntimeState.TERMINAL_INTENT_RECEIVED, RuntimeState.RUNNING),
        (RuntimeState.TERMINAL_INTENT_RECEIVED, RuntimeState.COMPLETED),
        (RuntimeState.FINALIZING, RuntimeState.RECEIVED),
        (RuntimeState.FINALIZING, RuntimeState.VERIFIED),
        (RuntimeState.FINALIZING, RuntimeState.RUNNING),
        (RuntimeState.FINALIZING, RuntimeState.TERMINAL_INTENT_RECEIVED),
    ],
)
def test_illegal_forward_transition(
    from_state: RuntimeState, to_state: RuntimeState
) -> None:
    """Illegal forward transitions raise MillforgeConfigError."""
    runtime = _build_runtime()
    runtime._state = from_state

    with pytest.raises(MillforgeConfigError, match="Illegal state transition"):
        runtime._transition_to(to_state)


# ======================================================================
# FAILED Reachable From All Non-Terminal States
# ======================================================================


@pytest.mark.parametrize("state", _NON_TERMINAL_STATES)
def test_failed_reachable_from_non_terminal(state: RuntimeState) -> None:
    """FAILED is reachable from every non-terminal state."""
    runtime = _build_runtime()
    runtime._state = state
    runtime._transition_to(RuntimeState.FAILED)
    assert runtime._state == RuntimeState.FAILED


# ======================================================================
# INTERRUPTED Reachable From All Non-Terminal States
# ======================================================================


@pytest.mark.parametrize("state", _NON_TERMINAL_STATES)
def test_interrupted_reachable_from_non_terminal(state: RuntimeState) -> None:
    """INTERRUPTED is reachable from every non-terminal state."""
    runtime = _build_runtime()
    runtime._state = state
    runtime._transition_to(RuntimeState.INTERRUPTED)
    assert runtime._state == RuntimeState.INTERRUPTED


# ======================================================================
# 12-Origin Failure Classification Matrix
# ======================================================================


def test_classify_failure_all_origins() -> None:
    """Verify the failure classification matrix."""
    expected: dict[FailureOrigin, ExecutionResultClass] = {
        FailureOrigin.COMPILED_HARNESS_INVALID: (
            ExecutionResultClass.COMPILED_HARNESS_INVALID
        ),
        FailureOrigin.HASH_MISMATCH: ExecutionResultClass.COMPILED_HARNESS_INVALID,
        FailureOrigin.IDENTITY_MISMATCH: ExecutionResultClass.COMPILED_HARNESS_INVALID,
        FailureOrigin.INCOMPATIBLE_STAGE: ExecutionResultClass.COMPILED_HARNESS_INVALID,
        FailureOrigin.MISSING_CAPABILITY: ExecutionResultClass.BINDING_REJECTED,
        FailureOrigin.ALREADY_CANCELLED: ExecutionResultClass.CANCELLED,
        FailureOrigin.EXPIRED_DEADLINE: ExecutionResultClass.TIMED_OUT,
        FailureOrigin.BACKEND_FAILURE: ExecutionResultClass.BACKEND_FAILURE,
        FailureOrigin.MODEL_FAILURE: ExecutionResultClass.MODEL_FAILURE,
        FailureOrigin.TOOL_FAILURE: ExecutionResultClass.TOOL_FAILURE,
        FailureOrigin.INVALID_TERMINAL: ExecutionResultClass.TERMINAL_RESULT_INVALID,
        FailureOrigin.ARTIFACT_WRITE_FAILURE: (
            ExecutionResultClass.ARTIFACT_FINALIZATION_FAILED
        ),
        FailureOrigin.INFRASTRUCTURE_FAILURE: ExecutionResultClass.INTERNAL_FAILURE,
    }

    assert len(expected) == len(FailureOrigin)

    for origin, expected_class in expected.items():
        result_class, status = classify_failure(origin)
        assert result_class == expected_class, (
            f"Origin {origin.value}: expected {expected_class.value}, "
            f"got {result_class.value}"
        )
        # Status must be one of the defined statuses
        assert isinstance(status, ExecutionStatus), (
            f"Origin {origin.value}: status is not ExecutionStatus"
        )


def test_classify_guarded_session_status_all_values() -> None:
    """Every guarded session status maps to the required public result class."""
    expected: dict[
        GuardedSessionStatus, tuple[ExecutionResultClass, ExecutionStatus]
    ] = {
        GuardedSessionStatus.TERMINAL: (
            ExecutionResultClass.DOMAIN_TERMINAL,
            ExecutionStatus.COMPLETED,
        ),
        GuardedSessionStatus.REJECTED: (
            ExecutionResultClass.DOMAIN_REJECTED,
            ExecutionStatus.COMPLETED,
        ),
        GuardedSessionStatus.BACKEND_FAILED: (
            ExecutionResultClass.BACKEND_FAILURE,
            ExecutionStatus.FAILED,
        ),
        GuardedSessionStatus.MODEL_FAILED: (
            ExecutionResultClass.MODEL_FAILURE,
            ExecutionStatus.FAILED,
        ),
        GuardedSessionStatus.TOOL_FAILED: (
            ExecutionResultClass.TOOL_FAILURE,
            ExecutionStatus.FAILED,
        ),
        GuardedSessionStatus.BUDGET_EXHAUSTED: (
            ExecutionResultClass.BUDGET_EXHAUSTED,
            ExecutionStatus.INTERRUPTED,
        ),
        GuardedSessionStatus.TIMED_OUT: (
            ExecutionResultClass.TIMED_OUT,
            ExecutionStatus.INTERRUPTED,
        ),
        GuardedSessionStatus.CANCELLED: (
            ExecutionResultClass.CANCELLED,
            ExecutionStatus.INTERRUPTED,
        ),
        GuardedSessionStatus.INVALID_TERMINAL: (
            ExecutionResultClass.TERMINAL_RESULT_INVALID,
            ExecutionStatus.FAILED,
        ),
    }

    assert set(expected) == set(GuardedSessionStatus)
    for session_status, public_mapping in expected.items():
        assert classify_guarded_session_status(session_status) == public_mapping


# ======================================================================
# Each origin produces the correct result via execute()
# ======================================================================


@pytest.mark.asyncio
async def test_failure_hash_mismatch() -> None:
    """Hash mismatch produces BLOCKED result."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
        compiled_sha256="1111111111111111111111111111111111111111111111111111111111111111",
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend()
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.COMPILED_HARNESS_INVALID,
    )


@pytest.mark.asyncio
async def test_failure_identity_mismatch() -> None:
    """Identity mismatch produces BLOCKED result."""
    plan = make_test_compiled_plan(
        plan_id="plan-different",
        harness_id="harness-different",
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend()
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.COMPILED_HARNESS_INVALID,
    )


@pytest.mark.asyncio
async def test_failure_incompatible_stage() -> None:
    """Incompatible stage produces BLOCKED result."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend()
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request(stage_plane="planning")

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.COMPILED_HARNESS_INVALID,
    )


@pytest.mark.asyncio
async def test_failure_missing_capability() -> None:
    """Missing capability produces BLOCKED result."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend()
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = HarnessExecutionRequest(
        request_id="req-runtime-001",
        run_id="run-runtime-001",
        work_item_id="task-runtime-001",
        stage=StageIdentity(
            plane="execution",
            node_id="builder",
            stage_kind_id="builder",
        ),
        compiled_harness=CompiledHarnessRef(
            identity=CompiledHarnessIdentity(
                compiled_plan_id=VALID_PLAN_ID,
                harness_id=VALID_HARNESS_ID,
                harness_version=VALID_HARNESS_VERSION,
            ),
            path=Path(f"/tmp/millforge/harnesses/{VALID_PLAN_ID}"),
            expected_hash=CompiledHarnessHash(
                algorithm="sha256",
                digest=VALID_HASH,
            ),
        ),
        capability_envelope=CapabilityEnvelope(grants=()),  # empty
        input_artifacts=(),
        run_directory=RunDirRef(
            run_id="run-runtime-001",
            path=Path("/tmp/millforge/runs/run-runtime-001"),
        ),
        timeout=TimeoutRef(timeout_seconds=3600.0),
        cancellation=CancellationRef(cancellation_id="cancel-001"),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id=VALID_PROFILE_ID),
    )

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.BINDING_REJECTED,
    )


@pytest.mark.asyncio
async def test_failure_already_cancelled() -> None:
    """Already-cancelled produces cancelled result."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    cancellation_resolver = FakeCancellationResolver(is_cancelled=True)
    backend = FakeGuardrailBackend()
    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=writer,
        cancellation_resolver=cancellation_resolver,
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.INTERRUPTED,
        expected_result_class=ExecutionResultClass.CANCELLED,
    )
    assert writer.terminal_result_calls == []


@pytest.mark.asyncio
async def test_failure_expired_deadline() -> None:
    """Expired deadline produces timed_out result."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    clock = FakeClock(fixed_time=datetime(2026, 6, 11, 18, 0, 0, tzinfo=timezone.utc))
    backend = FakeGuardrailBackend()
    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=writer,
        clock=clock,
    )
    request = _valid_harness_request(deadline_str="2026-06-11T12:00:00+00:00")

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.INTERRUPTED,
        expected_result_class=ExecutionResultClass.TIMED_OUT,
    )
    assert writer.terminal_result_calls == []


@pytest.mark.asyncio
async def test_failure_backend_failure() -> None:
    """Backend raises BackendTranslationError → RECOVERABLE_FAILURE."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend(
        exceptions=[BackendTranslationError("Backend refused request")]
    )
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.BACKEND_FAILURE,
    )


@pytest.mark.asyncio
async def test_failure_model_failure() -> None:
    """Backend raises ModelTransportError -> model_failure."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend(
        exceptions=[ModelTransportError("Model API unreachable")]
    )
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.MODEL_FAILURE,
    )


@pytest.mark.asyncio
async def test_failure_tool_failure() -> None:
    """Backend raises ToolInvokeError -> tool_failure."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend(
        exceptions=[ToolInvokeError("Tool execution failed")]
    )
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.TOOL_FAILURE,
    )


@pytest.mark.asyncio
async def test_failure_invalid_terminal() -> None:
    """Backend returns no terminal intent → TERMINAL_FAILURE."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    session_result = make_test_guarded_session_result(
        session_id="sess-runtime-001",
        status=GuardedSessionStatus.TERMINAL,
        with_terminal_intent=False,  # No terminal intent
    )
    backend = _backend_with_result(session_result)
    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=writer,
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.TERMINAL_RESULT_INVALID,
    )
    assert writer.terminal_result_calls == []


@pytest.mark.asyncio
async def test_failure_artifact_write_failure() -> None:
    """Artifact writer failure -> artifact_finalization_failed."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = _backend_with_result()

    # Use an artifact writer that raises on write_terminal_result
    class _FailingArtifactWriter(FakeArtifactWriter):
        async def write_terminal_result(self, ref: ArtifactRef, data: Any) -> None:
            raise ArtifactWriteError("Disk full")

    writer = _FailingArtifactWriter()
    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=writer,
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.ARTIFACT_FINALIZATION_FAILED,
    )
    assert writer.terminal_result_calls == []
    assert len(writer.execution_summary_calls) == 1
    assert len(writer.metrics_calls) == 1
    assert len(writer.manifest_calls) == 1
    assert writer.execution_summary_calls[0][1]["result_class"] == (
        ExecutionResultClass.ARTIFACT_FINALIZATION_FAILED.value
    )


@pytest.mark.asyncio
async def test_failure_infrastructure_failure() -> None:
    """Backend raises non-Millforge exception → TERMINAL_FAILURE
    via INFRASTRUCTURE_FAILURE."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend(
        exceptions=[RuntimeError("Unexpected infrastructure error")]
    )
    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=writer,
    )
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.INTERNAL_FAILURE,
    )
    assert writer.terminal_result_calls == []


# ======================================================================
# Infrastructure/Cancellation/Timeout/Artifact failures do NOT produce
# domain terminal results (they produce TERMINAL_FAILURE or
# RECOVERABLE_FAILURE, never SUCCESS).
# ======================================================================


def test_infrastructure_failure_not_domain_terminal() -> None:
    """INFRASTRUCTURE_FAILURE origin → status FAILED, not COMPLETED."""
    for origin in [
        FailureOrigin.INFRASTRUCTURE_FAILURE,
    ]:
        result_class, status = classify_failure(origin)
        assert result_class != ExecutionResultClass.DOMAIN_TERMINAL, (
            f"Origin {origin.value} should not produce SUCCESS"
        )
        assert status != ExecutionStatus.COMPLETED, (
            f"Origin {origin.value} should not produce COMPLETED"
        )


def test_cancellation_failure_not_domain_terminal() -> None:
    """Cancellation failures are not domain terminal."""
    for origin in [
        FailureOrigin.ALREADY_CANCELLED,
    ]:
        result_class, status = classify_failure(origin)
        assert result_class != ExecutionResultClass.DOMAIN_TERMINAL
        assert status != ExecutionStatus.COMPLETED


def test_timeout_failure_not_domain_terminal() -> None:
    """Timeout failures are not domain terminal."""
    for origin in [
        FailureOrigin.EXPIRED_DEADLINE,
    ]:
        result_class, status = classify_failure(origin)
        assert result_class != ExecutionResultClass.DOMAIN_TERMINAL
        assert status != ExecutionStatus.COMPLETED


def test_artifact_failure_not_domain_terminal() -> None:
    """Artifact write failures are not domain terminal (status FAILED)."""
    for origin in [
        FailureOrigin.ARTIFACT_WRITE_FAILURE,
    ]:
        result_class, status = classify_failure(origin)
        assert result_class != ExecutionResultClass.DOMAIN_TERMINAL
        assert status != ExecutionStatus.COMPLETED


# ======================================================================
# BaseException Propagation
# ======================================================================


@pytest.mark.asyncio
async def test_base_exception_propagates() -> None:
    """``BaseException`` raised by backend propagates without being caught."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)

    class _CancellingBackend(FakeGuardrailBackend):
        async def run_session(self, request: Any) -> Any:
            raise KeyboardInterrupt("User interrupted")

    backend = _CancellingBackend()
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    with pytest.raises(KeyboardInterrupt):
        await runtime.execute(request)


# ======================================================================
# Owned Exceptions Translated At Public Boundary
# ======================================================================


@pytest.mark.asyncio
async def test_owned_exception_translated_at_boundary() -> None:
    """Millforge-owned exceptions from the backend are caught and
    translated into failure results — they do not propagate."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)

    # BackendTranslationError is Millforge-owned and should be caught
    backend = FakeGuardrailBackend(
        exceptions=[BackendTranslationError("Backend refused")]
    )
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


# ======================================================================
# backend.run_session() called exactly once per execution
# ======================================================================


@pytest.mark.asyncio
async def test_backend_run_session_called_once_success() -> None:
    """``backend.run_session()`` is called exactly once on success."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = _backend_with_result()
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_success_result(result)
    assert len(backend.requests) == 1, (
        f"Expected exactly 1 run_session call, got {len(backend.requests)}"
    )


@pytest.mark.asyncio
async def test_backend_run_session_called_once_failure() -> None:
    """``backend.run_session()`` is called exactly once on backend failure."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend(exceptions=[ToolInvokeError("Tool failed")])
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(result)
    assert len(backend.requests) == 1, (
        f"Expected exactly 1 run_session call, got {len(backend.requests)}"
    )


@pytest.mark.asyncio
async def test_backend_run_session_not_called_on_preflight_failure() -> None:
    """``backend.run_session()`` is not called when preflight fails."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
        compiled_sha256="1111111111111111111111111111111111111111111111111111111111111111",
    )
    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend()
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_failure_result(result)
    assert len(backend.requests) == 0, (
        f"Expected zero run_session calls on preflight failure, "
        f"got {len(backend.requests)}"
    )


# ======================================================================
# Instrumentation check
# ======================================================================


def test_instrumentation_present() -> None:
    """Test file references the key instrumentation patterns."""
    backend = FakeGuardrailBackend()
    assert hasattr(backend, "requests")
    assert hasattr(backend, "run_session")


def test_no_forge_imports() -> None:
    """No Forge or provider SDK imports in the test file."""
    import ast
    import inspect

    import tests.test_runtime as mod

    source = inspect.getsource(mod)
    tree = ast.parse(source)
    _FORBIDDEN_MODULES = {"forge", "httpx"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top not in _FORBIDDEN_MODULES, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                top = node.module.split(".")[0]
                assert top not in _FORBIDDEN_MODULES, (
                    f"Forbidden import from: {node.module}"
                )
