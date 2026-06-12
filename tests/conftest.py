"""Shared 02B-shaped test helpers for Millforge tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from millforge.compiled_plan import (
    CompilerIdentity,
    CompiledArtifactPolicy,
    CompiledBudgetPolicy,
    CompiledContextPolicy,
    CompiledHarnessNode,
    CompiledHarnessPlan,
    CompiledModelProfile,
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
)
from millforge.contracts import (
    ArtifactRef,
    CancellationRef,
    CapabilityEnvelope,
    CapabilityGrant,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    Deadline,
    GuardedSessionRequest,
    GuardedSessionResult,
    GuardedSessionStatus,
    HarnessExecutionRequest,
    ModelProfileRef,
    RunDirRef,
    SecretRef,
    StageIdentity,
    TerminalIntent,
    TimeoutRef,
    TimingMetadata,
    TokenUsage,
    UsageMetadata,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _make_test_compiler_identity(
    name: str = "millforge-test-compiler",
    version: str = "1.0.0",
    build_id: str = "test-build",
) -> CompilerIdentity:
    return CompilerIdentity(name=name, version=version, build_id=build_id)


def _make_test_model_profile(
    profile_id: str = "deepseek_flash_high",
) -> CompiledModelProfile:
    return CompiledModelProfile(profile_id=profile_id)


def _make_test_prompt_policy(
    policy_id: str = "policy-test",
    system_instructions: str = "You are a helpful assistant.",
    include_request_context: bool = True,
) -> CompiledPromptPolicy:
    return CompiledPromptPolicy(
        policy_id=policy_id,
        system_instructions=system_instructions,
        include_request_context=include_request_context,
    )


def _make_test_budget_policy() -> CompiledBudgetPolicy:
    return CompiledBudgetPolicy(
        max_iterations=2,
        max_validation_retries=1,
        max_tool_errors=1,
        max_prerequisite_violations=1,
        max_premature_terminal_attempts=1,
    )


def _make_test_context_policy() -> CompiledContextPolicy:
    return CompiledContextPolicy(
        strategy_id="forge.tiered.v1",
        budget_tokens=4096,
        keep_recent_iterations=1,
        phase_thresholds=(0.25, 0.5, 1.0),
    )


def _make_test_tool_binding(
    tool_id: str = "get_weather",
    tool_version: int = 1,
    descriptor_sha256: str = SHA_A,
    implementation_id: str = "impl-weather-v1",
) -> ToolBindingRef:
    return ToolBindingRef(
        tool_id=tool_id,
        tool_version=tool_version,
        descriptor_sha256=descriptor_sha256,
        implementation_id=implementation_id,
    )


def _make_test_harness_node(
    node_id: str = "node-001",
    model_tool_name: str = "get_weather",
    terminal_result: str | None = "success",
    required: bool = False,
    produced_artifact_ids: tuple[str, ...] = ("art-output-001",),
) -> CompiledHarnessNode:
    return CompiledHarnessNode(
        node_id=node_id,
        model_tool_name=model_tool_name,
        description="Test node",
        input_schema={"type": "object"},
        binding=_make_test_tool_binding(),
        prerequisites=(),
        required=required,
        terminal_result=terminal_result,
        required_capabilities=("workspace.read",),
        produced_artifact_ids=produced_artifact_ids,
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    )


def _make_test_terminal_artifact_requirement(
    terminal_result: str = "success",
    artifact_ids: tuple[str, ...] = ("art-output-001",),
) -> TerminalArtifactRequirement:
    return TerminalArtifactRequirement(
        terminal_result=terminal_result,
        artifact_ids=artifact_ids,
    )


def _make_test_artifact_policy() -> CompiledArtifactPolicy:
    return CompiledArtifactPolicy(
        declared_artifact_ids=("art-output-001",),
        required_by_terminal=(_make_test_terminal_artifact_requirement(),),
    )


def make_test_compiled_plan(
    plan_id: str = "compiled-test-001",
    harness_id: str = "harness-test-001",
    harness_version: int = 1,
    compiled_sha256: str | None = None,
    model_profile_id: str = "deepseek_flash_high",
    stage_kind_ids: tuple[str, ...] = ("builder",),
    required_capabilities: tuple[str, ...] = ("workspace.read",),
    nodes: tuple[CompiledHarnessNode, ...] | None = None,
) -> CompiledHarnessPlan:
    """Build a complete 02B ``CompiledHarnessPlan``."""
    _ = plan_id
    plan = CompiledHarnessPlan(
        schema_version="1.0",
        kind="compiled_millforge_harness",
        harness_id=harness_id,
        harness_version=harness_version,
        source_sha256=SHA_A,
        compiled_sha256=compiled_sha256 or SHA_B,
        stage_kind_ids=stage_kind_ids,
        model_profile=_make_test_model_profile(profile_id=model_profile_id),
        prompt_policy=_make_test_prompt_policy(),
        budgets=_make_test_budget_policy(),
        context_policy=_make_test_context_policy(),
        nodes=nodes or (_make_test_harness_node(),),
        required_capabilities=required_capabilities,
        terminal_result_map={"node-001": "success"},
        artifact_policy=_make_test_artifact_policy(),
        compiler_identity=_make_test_compiler_identity(),
    )
    if compiled_sha256 is not None:
        return plan

    body = plan.model_dump(mode="json")
    body.pop("compiled_sha256")
    digest = hashlib.sha256(canonical_json_serialize(body).encode("utf-8")).hexdigest()
    return plan.model_copy(update={"compiled_sha256": digest})


def make_test_session_event(
    sequence: int = 1,
    session_id: str = "sess-test-001",
    event_type: SessionEventType = SessionEventType.RUNTIME_RECEIVED,
    request_id: str = "req-test-001",
    run_id: str = "run-test-001",
    stage: StageIdentity | None = None,
) -> SessionEvent:
    return SessionEvent(
        schema_version="1.0",
        sequence=sequence,
        occurred_at="2026-06-10T12:00:00Z",
        monotonic_offset_ms=float(sequence),
        event_type=event_type,
        request_id=request_id,
        run_id=run_id,
        session_id=session_id,
        stage=stage
        or StageIdentity(plane="execution", node_id="builder", stage_kind_id="builder"),
        node_id="node-001",
        model_turn=0,
        tool_call_id=None,
        code=event_type.value.upper(),
        fields=(),
    )


def make_test_tool_trace_record(
    sequence: int = 1,
    session_id: str = "sess-test-001",
    execution_status: ToolExecutionStatus = ToolExecutionStatus.SUCCESS,
) -> ToolTraceRecord:
    return ToolTraceRecord(
        schema_version="1.0",
        sequence=sequence,
        occurred_at="2026-06-10T12:00:01Z",
        monotonic_offset_ms=float(sequence + 1),
        request_id="req-test-001",
        run_id="run-test-001",
        session_id=session_id,
        stage=StageIdentity(
            plane="execution", node_id="builder", stage_kind_id="builder"
        ),
        node_id="node-001",
        model_turn=0,
        tool_call_id="call-001",
        model_tool_name="get_weather",
        binding=_make_test_tool_binding(),
        input_sha256=SHA_B,
        prerequisite_decisions=(),
        capability_decisions=(
            ToolTraceDecisionRecord(
                key="workspace.read", decision=ToolTraceDecision.ALLOWED
            ),
        ),
        execution_status=execution_status,
        retryable=False,
        side_effect_class=ToolTraceSideEffectClass.READ_ONLY,
        idempotency=ToolTraceIdempotency.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        output_sha256=SHA_C,
        duration_ms=1.0,
        summary="tool completed",
    )


def make_test_harness_execution_request(
    request_id: str = "req-test-001",
    run_id: str = "run-test-001",
    work_item_id: str = "task-test-001",
    stage_plane: Literal["execution", "planning", "learning"] = "execution",
    stage_node_id: str = "builder",
    stage_kind_id: str = "builder",
    harness_plan_id: str = "compiled-test-001",
    harness_id: str = "harness-test-001",
    harness_version: int = 1,
    hash_digest: str = SHA_B,
    profile_id: str = "deepseek_flash_high",
    timeout_seconds: float = 3600.0,
    deadline_str: str | None = "2026-06-10T18:00:00+00:00",
    cancellation_id: str = "cancel-001",
) -> HarnessExecutionRequest:
    run_directory = Path(f"/tmp/runs/{run_id}")
    input_path = Path("millforge/input.json")
    input_target = run_directory / input_path
    input_target.parent.mkdir(parents=True, exist_ok=True)
    input_target.write_text('{"schema_version":"test"}\n', encoding="utf-8")

    return HarnessExecutionRequest(
        request_id=request_id,
        run_id=run_id,
        work_item_id=work_item_id,
        stage=StageIdentity(
            plane=stage_plane,
            node_id=stage_node_id,
            stage_kind_id=stage_kind_id,
        ),
        compiled_harness=CompiledHarnessRef(
            identity=CompiledHarnessIdentity(
                compiled_plan_id=harness_plan_id,
                harness_id=harness_id,
                harness_version=harness_version,
            ),
            path=Path(f"/tmp/millforge/harnesses/{harness_plan_id}"),
            expected_hash=CompiledHarnessHash(algorithm="sha256", digest=hash_digest),
        ),
        capability_envelope=CapabilityEnvelope(
            grants=(
                CapabilityGrant(capability_id="workspace.read"),
                CapabilityGrant(capability_id="artifact.write"),
            )
        ),
        input_artifacts=(
            ArtifactRef(
                artifact_id="art-input-001",
                path=input_path,
                content_type="application/json",
            ),
        ),
        run_directory=RunDirRef(run_id=run_id, path=run_directory),
        timeout=TimeoutRef(timeout_seconds=timeout_seconds, deadline=deadline_str),
        cancellation=CancellationRef(cancellation_id=cancellation_id),
        secret_refs=(SecretRef(secret_id="db-password", env_var="DATABASE_PASSWORD"),),
        model_profile=ModelProfileRef(profile_id=profile_id),
    )


def make_test_terminal_intent(
    request_id: str = "req-test-001",
    run_id: str = "run-test-001",
    terminal_result: str = "success",
    disposition: Literal["success", "blocked", "rejected", "escalated"] = "success",
) -> TerminalIntent:
    return TerminalIntent(
        request_id=request_id,
        run_id=run_id,
        stage=StageIdentity(
            plane="execution", node_id="builder", stage_kind_id="builder"
        ),
        terminal_node_id="node-001",
        terminal_result=terminal_result,
        disposition=disposition,
        summary="Task completed successfully.",
        artifact_refs=(
            ArtifactRef(
                artifact_id="art-output-001",
                path=Path("millforge/output.json"),
                content_type="application/json",
            ),
        ),
    )


def make_test_guarded_session_request(
    session_id: str = "sess-test-001",
) -> GuardedSessionRequest:
    return GuardedSessionRequest(
        session_id=session_id,
        execution_request=make_test_harness_execution_request(),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=300.0,
            effective_deadline_monotonic=300.0,
            source="request",
        ),
    )


def make_test_guarded_session_result(
    session_id: str = "sess-test-001",
    status: GuardedSessionStatus = GuardedSessionStatus.TERMINAL,
    with_terminal_intent: bool = True,
    with_events: bool = True,
    with_tool_trace: bool = True,
    request_id: str = "req-runtime-001",
    run_id: str = "run-runtime-001",
) -> GuardedSessionResult:
    return GuardedSessionResult(
        session_id=session_id,
        status=status,
        terminal_intent=make_test_terminal_intent(request_id=request_id, run_id=run_id)
        if with_terminal_intent
        else None,
        artifact_refs=(),
        usage=UsageMetadata(
            model_calls=5,
            tool_calls=3,
            token_usage=TokenUsage(
                input_tokens=150,
                output_tokens=42,
                total_tokens=192,
                provider_reported=True,
            ),
        ),
        timing=TimingMetadata(
            started_at="2026-06-10T12:00:00Z",
            completed_at="2026-06-10T12:00:05Z",
            duration_ms=5000.0,
        ),
        diagnostic=None,
        events=(make_test_session_event(session_id=session_id),) if with_events else (),
        tool_trace=(make_test_tool_trace_record(session_id=session_id),)
        if with_tool_trace
        else (),
    )


class FakeCancellationToken:
    """Synchronous invocation-scoped cancellation token test double."""

    def __init__(
        self,
        cancellation_id: str = "cancel-001",
        is_cancelled_return: bool = False,
        reason: str | None = None,
    ) -> None:
        self._cancellation_id = cancellation_id
        self._is_cancelled_return = is_cancelled_return
        self._reason = reason

    @property
    def cancellation_id(self) -> str:
        return self._cancellation_id

    def is_cancelled(self) -> bool:
        return self._is_cancelled_return

    async def wait(self) -> None:
        return None

    @property
    def reason(self) -> str | None:
        return self._reason


class FakeCancellationResolver:
    """Synchronous resolver that records resolved cancellation refs."""

    def __init__(self, is_cancelled: bool = False) -> None:
        self._is_cancelled = is_cancelled
        self.resolve_calls: list[CancellationRef] = []

    def resolve(self, ref: CancellationRef) -> FakeCancellationToken:
        self.resolve_calls.append(ref)
        return FakeCancellationToken(
            cancellation_id=ref.cancellation_id,
            is_cancelled_return=self._is_cancelled,
        )


class FakeClock:
    """Deterministic clock test double."""

    def __init__(
        self,
        fixed_time: datetime | None = None,
        monotonic_value: float = 0.0,
    ) -> None:
        self._fixed_time = fixed_time or datetime(
            2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc
        )
        self._monotonic_value = monotonic_value

    def utc_now(self) -> datetime:
        return self._fixed_time

    def monotonic(self) -> float:
        return self._monotonic_value


class FakePlanLoader:
    """Compiled plan loader test double."""

    def __init__(
        self,
        plan: CompiledHarnessPlan | None = None,
        exception: Exception | None = None,
    ) -> None:
        self._plan = plan
        self._exception = exception
        self.load_calls: list[CompiledHarnessRef] = []

    async def load(self, ref: CompiledHarnessRef) -> CompiledHarnessPlan:
        self.load_calls.append(ref)
        if self._exception is not None:
            raise self._exception
        if self._plan is not None:
            return self._plan
        raise FileNotFoundError(f"No compiled plan at {ref.path}")


class FakeArtifactWriter:
    """Runtime artifact writer test double."""

    def __init__(self) -> None:
        self.terminal_result_calls: list[tuple[ArtifactRef, Any]] = []
        self.execution_summary_calls: list[tuple[ArtifactRef, Any]] = []
        self.events_calls: list[tuple[ArtifactRef, Any]] = []
        self.tool_trace_calls: list[tuple[ArtifactRef, Any]] = []
        self.metrics_calls: list[tuple[ArtifactRef, Any]] = []
        self.manifest_calls: list[tuple[ArtifactRef, Any]] = []
        self.diagnostic_calls: list[tuple[ArtifactRef, Any]] = []

    async def write_terminal_result(self, ref: ArtifactRef, data: Any) -> None:
        self.terminal_result_calls.append((ref, data))

    async def write_execution_summary(self, ref: ArtifactRef, data: Any) -> None:
        self.execution_summary_calls.append((ref, data))

    async def write_events(self, ref: ArtifactRef, data: Any) -> None:
        self.events_calls.append((ref, data))

    async def write_tool_trace(self, ref: ArtifactRef, data: Any) -> None:
        self.tool_trace_calls.append((ref, data))

    async def write_metrics(self, ref: ArtifactRef, data: Any) -> None:
        self.metrics_calls.append((ref, data))

    async def write_artifact_manifest(self, ref: ArtifactRef, data: Any) -> None:
        self.manifest_calls.append((ref, data))

    async def write_diagnostic(self, ref: ArtifactRef, data: Any) -> None:
        self.diagnostic_calls.append((ref, data))
