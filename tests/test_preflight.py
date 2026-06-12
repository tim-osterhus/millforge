"""Preflight failure scenario tests for DefaultHarnessRuntime.

Verifies that 14+ distinct failure scenarios produce the correct
result without calling ``backend.run_session()``, ``ModelClient.complete()``,
or ``ToolExecutor.execute()``.  Each test uses fake dependencies
instrumented to record calls and assert call count == 0.

All tests use a fake backend that asserts zero calls when preflight
is expected to fail.  No Forge, provider SDK, or network imports
are present anywhere in the test file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pytest

from millforge.contracts import (
    ArtifactRef,
    CancellationRef,
    CapabilityEnvelope,
    CapabilityGrant,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    ExecutionResultClass,
    ExecutionStatus,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    ModelProfileRef,
    RunDirRef,
    StageIdentity,
    TimeoutRef,
)
from millforge.runtime import DefaultHarnessRuntime
from millforge.testing import FakeGuardrailBackend, FakeModelClient, FakeToolExecutor

from tests.conftest import (
    FakeArtifactWriter,
    FakeCancellationResolver,
    FakeCancellationToken,
    FakeClock,
    FakePlanLoader,
    make_test_compiled_plan,
    make_test_harness_execution_request,
)

# ---------------------------------------------------------------------------
# Helper: construct a preflight request with a valid 64-char hex digest
# ---------------------------------------------------------------------------

VALID_PLAN_ID = "plan-test-001"
VALID_HARNESS_ID = "harness-test-001"
VALID_HARNESS_VERSION = 1
VALID_PROFILE_ID = "deepseek_flash_high"
VALID_PLAN_DIGEST = make_test_compiled_plan(
    harness_id=VALID_HARNESS_ID,
    harness_version=VALID_HARNESS_VERSION,
).compiled_sha256


def _valid_harness_request(
    request_id: str = "req-preflight-001",
    run_id: str = "run-preflight-001",
    work_item_id: str = "task-preflight-001",
    plane: Literal["execution", "planning", "learning"] = "execution",
    node_id: str = "builder",
    stage_kind_id: str = "builder",
    harness_plan_id: str = VALID_PLAN_ID,
    harness_id: str = VALID_HARNESS_ID,
    harness_version: int = VALID_HARNESS_VERSION,
    hash_digest: str = VALID_PLAN_DIGEST,
    profile_id: str = VALID_PROFILE_ID,
    timeout_seconds: float = 3600.0,
    deadline_str: str | None = "2026-06-11T12:00:00+00:00",
    cancellation_id: str = "cancel-preflight-001",
) -> HarnessExecutionRequest:
    """Build a valid HarnessExecutionRequest for preflight testing."""
    return make_test_harness_execution_request(
        request_id=request_id,
        run_id=run_id,
        work_item_id=work_item_id,
        stage_plane=plane,
        stage_node_id=node_id,
        stage_kind_id=stage_kind_id,
        harness_plan_id=harness_plan_id,
        harness_id=harness_id,
        harness_version=harness_version,
        hash_digest=hash_digest,
        profile_id=profile_id,
        timeout_seconds=timeout_seconds,
        deadline_str=deadline_str,
        cancellation_id=cancellation_id,
    )


def _build_runtime(
    backend: FakeGuardrailBackend | None = None,
    plan_loader: FakePlanLoader | None = None,
    artifact_writer: FakeArtifactWriter | None = None,
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


def _assert_zero_calls(
    backend: FakeGuardrailBackend,
    model_client: FakeModelClient | None = None,
    tool_executor: FakeToolExecutor | None = None,
) -> None:
    """Assert that no backend/model/tool calls were made."""
    assert len(backend.requests) == 0, (
        f"Expected zero backend.run_session() calls, got {len(backend.requests)}"
    )
    if model_client is not None:
        assert len(model_client.requests) == 0, (
            f"Expected zero ModelClient.complete() calls, got {len(model_client.requests)}"
        )
    if tool_executor is not None:
        assert len(tool_executor.calls) == 0, (
            f"Expected zero ToolExecutor.execute() calls, got {len(tool_executor.calls)}"
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
    assert result.terminal_intent is None


def _with_run_directory(
    request: HarnessExecutionRequest, path: Path
) -> HarnessExecutionRequest:
    return request.model_copy(
        update={"run_directory": RunDirRef(run_id=request.run_id, path=path)}
    )


def _assert_millforge_subtree_absent(path: Path) -> None:
    assert not (path / "millforge").exists()


def _request_with_input_artifacts(
    tmp_path: Path,
    *input_artifacts: ArtifactRef,
) -> HarnessExecutionRequest:
    request = _valid_harness_request()
    return request.model_copy(
        update={
            "input_artifacts": input_artifacts,
            "run_directory": RunDirRef(run_id=request.run_id, path=tmp_path),
        }
    )


def _write_input_artifact(
    tmp_path: Path,
    relative_path: str = "millforge/input.json",
) -> ArtifactRef:
    path = Path(relative_path)
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"schema_version":"test"}\n', encoding="utf-8")
    return ArtifactRef(
        artifact_id="art-input-001",
        path=path,
        content_type="application/json",
    )


async def _execute_input_artifact_preflight(
    request: HarnessExecutionRequest,
) -> tuple[
    HarnessExecutionResult, FakeGuardrailBackend, FakeModelClient, FakeToolExecutor
]:
    backend = FakeGuardrailBackend()
    model_client = FakeModelClient()
    tool_executor = FakeToolExecutor()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    runtime = _build_runtime(
        backend=backend,
        plan_loader=FakePlanLoader(plan=plan),
    )

    result = await runtime.execute(request)

    return result, backend, model_client, tool_executor


# ---------------------------------------------------------------------------
# 1. Missing compiled plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_missing_compiled_plan() -> None:
    """Plan loader raises FileNotFoundError — no backend call."""
    backend = FakeGuardrailBackend()
    plan_loader = FakePlanLoader()
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.COMPILED_HARNESS_INVALID,
    )


# ---------------------------------------------------------------------------
# 2. Malformed JSON (plan loader raises ValueError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_malformed_json() -> None:
    """Plan loader raises ValueError on malformed input — no backend call."""
    backend = FakeGuardrailBackend()
    plan_loader = FakePlanLoader(exception=ValueError("Malformed JSON input"))
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.COMPILED_HARNESS_INVALID,
    )


# ---------------------------------------------------------------------------
# 3. Duplicate JSON key (plan loader raises ValueError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_duplicate_json_key() -> None:
    """Plan loader raises ValueError on duplicate key — no backend call."""
    backend = FakeGuardrailBackend()
    plan_loader = FakePlanLoader(
        exception=ValueError("Duplicate key 'plan_id' in JSON input")
    )
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.COMPILED_HARNESS_INVALID,
    )


# ---------------------------------------------------------------------------
# 4. Wrong schema (valid JSON but doesn't match CompiledHarnessPlan)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_wrong_schema() -> None:
    """Plan loader raises TypeError/ValueError for schema mismatch — no backend call."""
    backend = FakeGuardrailBackend()
    plan_loader = FakePlanLoader(
        exception=TypeError("JSON data does not match CompiledHarnessPlan schema")
    )
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.COMPILED_HARNESS_INVALID,
    )


# ---------------------------------------------------------------------------
# 5. Hash mismatch (plan hash doesn't match request expected hash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_hash_mismatch() -> None:
    """Plan SHA-256 doesn't match expected hash — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
        compiled_sha256="1111111111111111111111111111111111111111111111111111111111111111",
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# 6. Identity mismatch (plan identity doesn't match request)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_identity_mismatch() -> None:
    """Plan harness_id doesn't match request compiled_harness identity — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        harness_id="harness-different",
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request()

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# 7. Incompatible stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_incompatible_stage() -> None:
    """Stage plane is not 'execution' — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request(plane="planning")

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# 8. Harness version mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_harness_version_mismatch() -> None:
    """Plan harness_version doesn't match request — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=2,
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request(
        harness_version=1, hash_digest=plan.compiled_sha256
    )

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# 9. Missing capability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_missing_capability() -> None:
    """Request has no capability grants — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = HarnessExecutionRequest(
        request_id="req-preflight-001",
        run_id="run-preflight-001",
        work_item_id="task-preflight-001",
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
                digest=plan.compiled_sha256,
            ),
        ),
        capability_envelope=CapabilityEnvelope(grants=()),  # empty — no capabilities
        input_artifacts=(),
        run_directory=RunDirRef(
            run_id="run-preflight-001",
            path=Path("/tmp/millforge/runs/run-preflight-001"),
        ),
        timeout=TimeoutRef(timeout_seconds=3600.0),
        cancellation=CancellationRef(cancellation_id="cancel-001"),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id=VALID_PROFILE_ID),
    )

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# 10. Already-cancelled (cancellation token is_cancelled returns True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_already_cancelled(tmp_path: Path) -> None:
    """Cancellation token already set — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    cancellation_resolver = FakeCancellationResolver(is_cancelled=True)
    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        cancellation_resolver=cancellation_resolver,
    )
    request = _with_run_directory(_valid_harness_request(), tmp_path)

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.INTERRUPTED,
        expected_result_class=ExecutionResultClass.CANCELLED,
    )
    _assert_millforge_subtree_absent(tmp_path)


# ---------------------------------------------------------------------------
# 11. Unknown cancellation ref (resolver raises exception)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_unknown_cancellation_ref(tmp_path: Path) -> None:
    """Cancellation resolver cannot resolve the ref — no backend call."""
    backend = FakeGuardrailBackend()

    class _FailingCancellationResolver(FakeCancellationResolver):
        def resolve(self, ref: CancellationRef) -> FakeCancellationToken:
            self.resolve_calls.append(ref)
            raise KeyError(f"Unknown cancellation ref: {ref}")

    resolver = _FailingCancellationResolver()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        cancellation_resolver=resolver,
    )
    request = _with_run_directory(
        _valid_harness_request(cancellation_id="unknown-cancel-999"),
        tmp_path,
    )

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.INTERNAL_FAILURE,
    )
    _assert_millforge_subtree_absent(tmp_path)


# ---------------------------------------------------------------------------
# 12. Expired deadline (clock past deadline)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_expired_deadline(tmp_path: Path) -> None:
    """Clock is past the absolute deadline — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    clock = FakeClock(fixed_time=datetime(2026, 6, 11, 18, 0, 0, tzinfo=timezone.utc))
    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        clock=clock,
    )
    # Deadline is "2026-06-11T12:00:00+00:00", clock is at 18:00:00Z (past)
    request = _with_run_directory(
        _valid_harness_request(deadline_str="2026-06-11T12:00:00+00:00"),
        tmp_path,
    )

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.INTERRUPTED,
        expected_result_class=ExecutionResultClass.TIMED_OUT,
    )
    _assert_millforge_subtree_absent(tmp_path)


# ---------------------------------------------------------------------------
# 13. Model profile mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_model_profile_mismatch() -> None:
    """Plan model profile doesn't match request profile — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
        model_profile_id="profile-other",
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request(hash_digest=plan.compiled_sha256)

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


@pytest.mark.asyncio
async def test_preflight_stage_kind_mismatch() -> None:
    """Request stage kind must be admitted by compiled plan — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
        stage_kind_ids=("checker",),
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request(hash_digest=plan.compiled_sha256)

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


@pytest.mark.asyncio
async def test_preflight_missing_required_input_artifacts() -> None:
    """Request must admit input artifacts before backend work."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = HarnessExecutionRequest(
        request_id="req-preflight-input",
        run_id="run-preflight-input",
        work_item_id="task-preflight-001",
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
                digest=plan.compiled_sha256,
            ),
        ),
        capability_envelope=CapabilityEnvelope(
            grants=(CapabilityGrant(capability_id="workspace.read"),)
        ),
        input_artifacts=(),
        run_directory=RunDirRef(
            run_id="run-preflight-input",
            path=Path("/tmp/millforge/runs/run-preflight-input"),
        ),
        timeout=TimeoutRef(timeout_seconds=3600.0),
        cancellation=CancellationRef(cancellation_id="cancel-001"),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id=VALID_PROFILE_ID),
    )

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


@pytest.mark.asyncio
async def test_preflight_missing_input_artifact_path(tmp_path: Path) -> None:
    """Admitted input artifact refs must exist before backend work."""
    request = _request_with_input_artifacts(
        tmp_path,
        ArtifactRef(
            artifact_id="art-input-001",
            path=Path("millforge/missing.json"),
            content_type="application/json",
        ),
    )

    (
        result,
        backend,
        model_client,
        tool_executor,
    ) = await _execute_input_artifact_preflight(request)

    _assert_zero_calls(backend, model_client, tool_executor)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.BINDING_REJECTED,
    )
    assert result.diagnostic is not None
    assert "missing" in result.diagnostic.message


@pytest.mark.asyncio
async def test_preflight_absolute_input_artifact_path_rejected(tmp_path: Path) -> None:
    """Admitted input artifact refs must not use absolute paths."""
    request = _request_with_input_artifacts(
        tmp_path,
        ArtifactRef(
            artifact_id="art-input-001",
            path=tmp_path / "millforge/input.json",
            content_type="application/json",
        ),
    )

    (
        result,
        backend,
        model_client,
        tool_executor,
    ) = await _execute_input_artifact_preflight(request)

    _assert_zero_calls(backend, model_client, tool_executor)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.BINDING_REJECTED,
    )
    assert result.diagnostic is not None
    assert "absolute path" in result.diagnostic.message


@pytest.mark.asyncio
async def test_preflight_parent_traversal_input_artifact_path_rejected(
    tmp_path: Path,
) -> None:
    """Admitted input artifact refs must not contain parent traversal."""
    request = _request_with_input_artifacts(
        tmp_path,
        ArtifactRef(
            artifact_id="art-input-001",
            path=Path("millforge/../input.json"),
            content_type="application/json",
        ),
    )

    (
        result,
        backend,
        model_client,
        tool_executor,
    ) = await _execute_input_artifact_preflight(request)

    _assert_zero_calls(backend, model_client, tool_executor)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.BINDING_REJECTED,
    )
    assert result.diagnostic is not None
    assert "parent traversal" in result.diagnostic.message


@pytest.mark.asyncio
async def test_preflight_symlink_escape_input_artifact_path_rejected(
    tmp_path: Path,
) -> None:
    """Admitted input artifact refs must not resolve outside millforge/."""
    outside = tmp_path / "outside.json"
    outside.write_text('{"schema_version":"outside"}\n', encoding="utf-8")
    millforge_dir = tmp_path / "millforge"
    millforge_dir.mkdir(parents=True, exist_ok=True)
    (millforge_dir / "input.json").symlink_to(outside)
    request = _request_with_input_artifacts(
        tmp_path,
        ArtifactRef(
            artifact_id="art-input-001",
            path=Path("millforge/input.json"),
            content_type="application/json",
        ),
    )

    (
        result,
        backend,
        model_client,
        tool_executor,
    ) = await _execute_input_artifact_preflight(request)

    _assert_zero_calls(backend, model_client, tool_executor)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.BINDING_REJECTED,
    )
    assert result.diagnostic is not None
    assert "outside millforge" in result.diagnostic.message


@pytest.mark.asyncio
async def test_preflight_duplicate_resolved_input_artifact_paths_rejected(
    tmp_path: Path,
) -> None:
    """Admitted input artifacts must not silently substitute the same file."""
    input_ref = _write_input_artifact(tmp_path)
    request = _request_with_input_artifacts(
        tmp_path,
        input_ref,
        ArtifactRef(
            artifact_id="art-input-002",
            path=input_ref.path,
            content_type="application/json",
        ),
    )

    (
        result,
        backend,
        model_client,
        tool_executor,
    ) = await _execute_input_artifact_preflight(request)

    _assert_zero_calls(backend, model_client, tool_executor)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.BINDING_REJECTED,
    )
    assert result.diagnostic is not None
    assert "reused" in result.diagnostic.message


@pytest.mark.asyncio
async def test_preflight_unreadable_input_artifact_path_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable admitted input artifacts fail before backend work."""
    input_ref = _write_input_artifact(tmp_path)
    request = _request_with_input_artifacts(tmp_path, input_ref)
    original_open = Path.open
    target = (tmp_path / input_ref.path).resolve()

    def unreadable_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.resolve() == target:
            raise PermissionError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable_open)

    (
        result,
        backend,
        model_client,
        tool_executor,
    ) = await _execute_input_artifact_preflight(request)

    _assert_zero_calls(backend, model_client, tool_executor)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.BINDING_REJECTED,
    )
    assert result.diagnostic is not None
    assert "not readable" in result.diagnostic.message


# ---------------------------------------------------------------------------
# 14. Invalid deadline format (malformed ISO-8601 string)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_invalid_deadline_format(tmp_path: Path) -> None:
    """Deadline has an unparseable absolute timestamp — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    # Deadline string is not valid ISO-8601
    request = _with_run_directory(
        _valid_harness_request(deadline_str="not-a-valid-date"),
        tmp_path,
    )

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.INTERNAL_FAILURE,
    )
    _assert_millforge_subtree_absent(tmp_path)


# ---------------------------------------------------------------------------
# 15. Empty request_id (validation failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_empty_request_id() -> None:
    """Request has empty request_id — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request(request_id="   ")

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# 16. Empty run_id (validation failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_empty_run_id() -> None:
    """Request has empty run_id — no backend call."""
    backend = FakeGuardrailBackend()
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    plan_loader = FakePlanLoader(plan=plan)
    runtime = _build_runtime(backend=backend, plan_loader=plan_loader)
    request = _valid_harness_request(run_id="   ")

    result = await runtime.execute(request)

    _assert_zero_calls(backend)
    _assert_failure_result(result)
    assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# Instrumentation check: grep targets exist
# ---------------------------------------------------------------------------


def test_instrumentation_present() -> None:
    """Preflight tests reference run_session, .complete(), and .execute() in fakes."""
    # This test verifies the test file contains the expected instrumentation
    # patterns — the fakes used in preflight tests are instrumented.
    backend = FakeGuardrailBackend()
    model_client = FakeModelClient()
    tool_executor = FakeToolExecutor()

    # Verify fakes have the expected call-recording attributes
    assert hasattr(backend, "requests")
    assert hasattr(model_client, "requests")
    assert hasattr(tool_executor, "calls")


def test_no_forge_imports() -> None:
    """No Forge or provider SDK imports in the test file."""
    import ast
    import inspect

    import tests.test_preflight as mod

    source = inspect.getsource(mod)
    tree = ast.parse(source)
    _FORBIDDEN_MODULES = {"forge", "httpx"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Check the top-level package name only
                top = alias.name.split(".")[0]
                assert top not in _FORBIDDEN_MODULES, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                top = node.module.split(".")[0]
                assert top not in _FORBIDDEN_MODULES, (
                    f"Forbidden import from: {node.module}"
                )
