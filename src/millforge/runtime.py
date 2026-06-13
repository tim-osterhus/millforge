"""DefaultHarnessRuntime — 21-step algorithm, 9-state machine, 12-origin failure classification.

The runtime wires five dependencies (backend, plan_loader, artifact_writer, clock,
cancellation_resolver) and executes one compiled-harness request through the full
21-step pipeline. It never calls ``ModelClient`` or ``ToolExecutor`` independently —
the backend is the sole owner of model/tool interaction.

BaseException is never caught — cancellation semantics are preserved.  Only
Millforge-owned exceptions are translated at the public boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from millforge.artifacts import STANDARD_ARTIFACT_FILENAMES
from millforge.compiled_plan import CompiledHarnessPlan
from millforge.contracts import (
    ArtifactRef,
    Deadline,
    DiagnosticMetadata,
    ExecutionResultClass,
    ExecutionStatus,
    GuardedSessionRequest,
    GuardedSessionResult,
    GuardedSessionStatus,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    TerminalIntent,
    TimingMetadata,
    UsageMetadata,
)
from millforge.exceptions import (
    ArtifactWriteError,
    BackendTranslationError,
    DeadlineExceededError,
    HarnessMismatchError,
    MillforgeConfigError,
    MillforgeError,
    ModelTransportError,
    OperationCancelledError,
    ToolInvokeError,
)
from millforge.protocols import (
    CancellationResolver,
    CompiledHarnessLoader,
    GuardrailBackend,
    RuntimeArtifactWriter,
    RuntimeClock,
)

# ======================================================================
# 9-State State Machine
# ======================================================================


class RuntimeState(str, Enum):
    """Nine-state state machine for harness execution.

    Forward transitions (must follow this order):
        RECEIVED → VERIFIED
        VERIFIED → BACKEND_SESSION_CONSTRUCTED
        BACKEND_SESSION_CONSTRUCTED → RUNNING
        RUNNING → TERMINAL_INTENT_RECEIVED
        RUNNING → FINALIZING
        TERMINAL_INTENT_RECEIVED → FINALIZING
        FINALIZING → COMPLETED

    ``FAILED`` and ``INTERRUPTED`` are reachable from all non-terminal
    states.  Terminal states (COMPLETED, FAILED, INTERRUPTED) reject
    all further transitions.
    """

    RECEIVED = "received"
    VERIFIED = "verified"
    BACKEND_SESSION_CONSTRUCTED = "backend_session_constructed"
    RUNNING = "running"
    TERMINAL_INTENT_RECEIVED = "terminal_intent_received"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @classmethod
    def _is_terminal(cls, state: RuntimeState) -> bool:
        return state in (cls.COMPLETED, cls.FAILED, cls.INTERRUPTED)


# ======================================================================
# 12-Origin Failure Classification
# ======================================================================


class FailureOrigin(str, Enum):
    """Failure origins for the runtime failure classification matrix."""

    COMPILED_HARNESS_INVALID = "compiled_harness_invalid"
    HASH_MISMATCH = "hash_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    INCOMPATIBLE_STAGE = "incompatible_stage"
    MISSING_CAPABILITY = "missing_capability"
    ALREADY_CANCELLED = "already_cancelled"
    EXPIRED_DEADLINE = "expired_deadline"
    BACKEND_FAILURE = "backend_failure"
    MODEL_FAILURE = "model_failure"
    TOOL_FAILURE = "tool_failure"
    INVALID_TERMINAL = "invalid_terminal"
    ARTIFACT_WRITE_FAILURE = "artifact_write_failure"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"


# Failure classification matrix: each origin → ExecutionResultClass
_FAILURE_MATRIX: dict[FailureOrigin, ExecutionResultClass] = {
    FailureOrigin.COMPILED_HARNESS_INVALID: ExecutionResultClass.COMPILED_HARNESS_INVALID,
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
    FailureOrigin.ARTIFACT_WRITE_FAILURE: ExecutionResultClass.ARTIFACT_FINALIZATION_FAILED,
    FailureOrigin.INFRASTRUCTURE_FAILURE: ExecutionResultClass.INTERNAL_FAILURE,
}

# Failure origin → ExecutionStatus mapping
_FAILURE_STATUS: dict[FailureOrigin, ExecutionStatus] = {
    FailureOrigin.COMPILED_HARNESS_INVALID: ExecutionStatus.FAILED,
    FailureOrigin.HASH_MISMATCH: ExecutionStatus.FAILED,
    FailureOrigin.IDENTITY_MISMATCH: ExecutionStatus.FAILED,
    FailureOrigin.INCOMPATIBLE_STAGE: ExecutionStatus.FAILED,
    FailureOrigin.MISSING_CAPABILITY: ExecutionStatus.FAILED,
    FailureOrigin.ALREADY_CANCELLED: ExecutionStatus.INTERRUPTED,
    FailureOrigin.EXPIRED_DEADLINE: ExecutionStatus.INTERRUPTED,
    FailureOrigin.BACKEND_FAILURE: ExecutionStatus.FAILED,
    FailureOrigin.MODEL_FAILURE: ExecutionStatus.FAILED,
    FailureOrigin.TOOL_FAILURE: ExecutionStatus.FAILED,
    FailureOrigin.INVALID_TERMINAL: ExecutionStatus.FAILED,
    FailureOrigin.ARTIFACT_WRITE_FAILURE: ExecutionStatus.FAILED,
    FailureOrigin.INFRASTRUCTURE_FAILURE: ExecutionStatus.FAILED,
}

_SESSION_STATUS_MATRIX: dict[
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
    GuardedSessionStatus.PREREQUISITE_BUDGET_EXHAUSTED: (
        ExecutionResultClass.BUDGET_EXHAUSTED,
        ExecutionStatus.FAILED,
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


def classify_failure(
    origin: FailureOrigin,
) -> tuple[ExecutionResultClass, ExecutionStatus]:
    """Classify a failure origin into result class and execution status.

    Parameters
    ----------
    origin : FailureOrigin
        The failure origin to classify.

    Returns
    -------
    tuple[ExecutionResultClass, ExecutionStatus]
        The (result_class, status) pair for this origin.
    """
    return _FAILURE_MATRIX[origin], _FAILURE_STATUS[origin]


def classify_guarded_session_status(
    status: GuardedSessionStatus,
) -> tuple[ExecutionResultClass, ExecutionStatus]:
    """Classify a guarded-session status into public result class and status."""
    return _SESSION_STATUS_MATRIX[status]


def _diagnostic_category(
    origin: FailureOrigin,
) -> Literal[
    "binding",
    "compiled_harness",
    "backend",
    "model",
    "tool",
    "timeout",
    "cancellation",
    "artifact",
    "internal",
]:
    categories: dict[
        FailureOrigin,
        Literal[
            "binding",
            "compiled_harness",
            "backend",
            "model",
            "tool",
            "timeout",
            "cancellation",
            "artifact",
            "internal",
        ],
    ] = {
        FailureOrigin.COMPILED_HARNESS_INVALID: "compiled_harness",
        FailureOrigin.HASH_MISMATCH: "compiled_harness",
        FailureOrigin.IDENTITY_MISMATCH: "compiled_harness",
        FailureOrigin.INCOMPATIBLE_STAGE: "compiled_harness",
        FailureOrigin.MISSING_CAPABILITY: "binding",
        FailureOrigin.ALREADY_CANCELLED: "cancellation",
        FailureOrigin.EXPIRED_DEADLINE: "timeout",
        FailureOrigin.BACKEND_FAILURE: "backend",
        FailureOrigin.MODEL_FAILURE: "model",
        FailureOrigin.TOOL_FAILURE: "tool",
        FailureOrigin.INVALID_TERMINAL: "internal",
        FailureOrigin.ARTIFACT_WRITE_FAILURE: "artifact",
        FailureOrigin.INFRASTRUCTURE_FAILURE: "internal",
    }
    return categories[origin]


# ======================================================================
# Helpers
# ======================================================================


def _build_artifact_ref(artifact_id: str) -> ArtifactRef:
    """Build an ``ArtifactRef`` for a standard millforge artifact.

    Parameters
    ----------
    artifact_id : str
        One of ``STANDARD_ARTIFACT_FILENAMES`` keys.

    Returns
    -------
    ArtifactRef
        A ref with a ``millforge/<filename>`` path.
    """
    filename = STANDARD_ARTIFACT_FILENAMES[artifact_id]
    return ArtifactRef(
        artifact_id=artifact_id,
        path=Path("millforge") / filename,
    )


def _build_success_artifact_refs(
    *, include_diagnostic: bool
) -> tuple[ArtifactRef, ...]:
    """Build ordered refs for standard artifacts written on the success path."""
    artifact_ids = [
        "terminal_result",
        "execution_summary",
        "events",
        "tool_trace",
        "metrics",
    ]
    if include_diagnostic:
        artifact_ids.append("diagnostic")
    artifact_ids.append("artifact_manifest")
    return tuple(_build_artifact_ref(artifact_id) for artifact_id in artifact_ids)


def _build_non_terminal_artifact_refs(
    *, include_diagnostic: bool
) -> tuple[ArtifactRef, ...]:
    """Build refs for non-terminal artifacts written after run preparation."""
    artifact_ids = [
        "execution_summary",
        "metrics",
    ]
    if include_diagnostic:
        artifact_ids.append("diagnostic")
    artifact_ids.append("artifact_manifest")
    return tuple(_build_artifact_ref(artifact_id) for artifact_id in artifact_ids)


# ======================================================================
# DefaultHarnessRuntime
# ======================================================================


class DefaultHarnessRuntime:
    """Default implementation of the ``HarnessRuntime`` protocol.

    Executes one compiled-harness request through the 21-step algorithm
    with the 9-state state machine and 12-origin failure classification.
    Never calls ``ModelClient`` or ``ToolExecutor`` independently —
    the backend is the sole owner of model/tool interaction.

    Parameters
    ----------
    backend : GuardrailBackend
        Guardrail backend that owns all model/tool interaction.
    plan_loader : CompiledHarnessLoader
        Loads compiled harness plans from references.
    artifact_writer : RuntimeArtifactWriter
        Writes the 7 standard runtime artifacts.
    clock : RuntimeClock
        Deterministic clock for timestamps and timing.
    cancellation_resolver : CancellationResolver
        Resolves cancellation references into checkable tokens.
    """

    def __init__(
        self,
        *,
        backend: GuardrailBackend,
        plan_loader: CompiledHarnessLoader,
        artifact_writer: RuntimeArtifactWriter,
        clock: RuntimeClock,
        cancellation_resolver: CancellationResolver,
    ) -> None:
        self._backend = backend
        self._plan_loader = plan_loader
        self._artifact_writer = artifact_writer
        self._clock = clock
        self._cancellation_resolver = cancellation_resolver
        self._state: RuntimeState = RuntimeState.RECEIVED
        self._started_at: datetime | None = None

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _transition_to(self, target: RuntimeState) -> None:
        """Transition the state machine from the current state to *target*.

        Parameters
        ----------
        target : RuntimeState
            Target state.

        Raises
        ------
        MillforgeConfigError
            If the transition is illegal.
        """
        current = self._state

        if RuntimeState._is_terminal(current):
            raise MillforgeConfigError(
                f"Illegal transition from terminal state {current.value} → {target.value}"
            )

        # FAILED and INTERRUPTED are reachable from any non-terminal state.
        if target in (RuntimeState.FAILED, RuntimeState.INTERRUPTED):
            self._state = target
            return

        # Legal forward transitions.
        forward: dict[RuntimeState, tuple[RuntimeState, ...]] = {
            RuntimeState.RECEIVED: (RuntimeState.VERIFIED,),
            RuntimeState.VERIFIED: (RuntimeState.BACKEND_SESSION_CONSTRUCTED,),
            RuntimeState.BACKEND_SESSION_CONSTRUCTED: (RuntimeState.RUNNING,),
            RuntimeState.RUNNING: (
                RuntimeState.TERMINAL_INTENT_RECEIVED,
                RuntimeState.FINALIZING,
            ),
            RuntimeState.TERMINAL_INTENT_RECEIVED: (RuntimeState.FINALIZING,),
            RuntimeState.FINALIZING: (RuntimeState.COMPLETED,),
        }

        allowed = forward.get(current, ())
        if target not in allowed:
            raise MillforgeConfigError(
                f"Illegal state transition: {current.value} → {target.value}"
            )

        self._state = target

    def _enter_non_terminal_finalizing(self) -> None:
        """Enter FINALIZING for post-backend non-terminal artifact writes."""
        if self._state == RuntimeState.RUNNING:
            self._transition_to(RuntimeState.FINALIZING)

    # ------------------------------------------------------------------
    # Failure result factory
    # ------------------------------------------------------------------

    def _failure_result(
        self,
        origin: FailureOrigin,
        request: HarnessExecutionRequest,
        terminal_intent: TerminalIntent | None = None,
        message: str | None = None,
    ) -> HarnessExecutionResult:
        """Build a failure ``HarnessExecutionResult`` with the correct classification.

        Also transitions the state machine to ``FAILED`` or ``INTERRUPTED``
        (for ``ALREADY_CANCELLED``) before building the result.
        """
        # Transition state machine before building the result.
        if origin == FailureOrigin.ALREADY_CANCELLED:
            self._transition_to(RuntimeState.INTERRUPTED)
        else:
            self._transition_to(RuntimeState.FAILED)

        result_class, status = classify_failure(origin)

        diagnostic: DiagnosticMetadata | None = None
        if message is not None:
            diagnostic = DiagnosticMetadata(
                error_code=origin.value,
                category=_diagnostic_category(origin),
                message=message,
                retryable=origin
                in {
                    FailureOrigin.BACKEND_FAILURE,
                    FailureOrigin.MODEL_FAILURE,
                    FailureOrigin.TOOL_FAILURE,
                    FailureOrigin.INFRASTRUCTURE_FAILURE,
                },
                origin=origin.value,
                fields=(),
            )

        return HarnessExecutionResult(
            status=status,
            result_class=result_class,
            request_id=request.request_id,
            run_id=request.run_id,
            stage=request.stage,
            terminal_intent=terminal_intent,
            artifact_refs=(),
            compiled_harness=request.compiled_harness,
            usage=None,
            timing=self._timing(request),
            diagnostic=diagnostic,
        )

    async def _write_non_terminal_artifacts(
        self,
        request: HarnessExecutionRequest,
        *,
        status: ExecutionStatus,
        result_class: ExecutionResultClass,
        session_result: GuardedSessionResult | None = None,
        diagnostic: DiagnosticMetadata | None = None,
    ) -> tuple[ArtifactRef, ...]:
        """Write non-terminal runtime artifacts without terminal_result intent."""
        exec_summary_ref = _build_artifact_ref("execution_summary")
        await self._artifact_writer.write_execution_summary(
            exec_summary_ref,
            {
                "schema_version": "1.0",
                "request_id": request.request_id,
                "run_id": request.run_id,
                "stage": request.stage.model_dump(mode="json"),
                "status": status.value,
                "result_class": result_class.value,
                "diagnostic_error_code": diagnostic.error_code
                if diagnostic is not None
                else None,
            },
        )

        metrics_ref = _build_artifact_ref("metrics")
        metrics_data: dict[str, Any] = {
            "schema_version": "1.0",
            "request_id": request.request_id,
            "run_id": request.run_id,
            "session_id": session_result.session_id if session_result else None,
            "status": session_result.status.value if session_result else status.value,
        }
        if session_result is not None and session_result.usage is not None:
            metrics_data["usage"] = session_result.usage.model_dump(mode="json")
        await self._artifact_writer.write_metrics(metrics_ref, metrics_data)

        manifest_artifacts = [
            {"artifact_id": "execution_summary"},
            {"artifact_id": "metrics"},
        ]
        artifact_ids = [
            "execution_summary",
            "metrics",
        ]
        if session_result is not None:
            events_ref = _build_artifact_ref("events")
            await self._artifact_writer.write_events(
                events_ref,
                [event.model_dump(mode="json") for event in session_result.events],
            )
            tool_trace_ref = _build_artifact_ref("tool_trace")
            await self._artifact_writer.write_tool_trace(
                tool_trace_ref,
                [
                    record.model_dump(mode="json")
                    for record in session_result.tool_trace
                ],
            )
            manifest_artifacts.extend(
                [
                    {"artifact_id": "events"},
                    {"artifact_id": "tool_trace"},
                ]
            )
            artifact_ids.extend(["events", "tool_trace"])
        if diagnostic is not None:
            diagnostic_ref = _build_artifact_ref("diagnostic")
            await self._artifact_writer.write_diagnostic(diagnostic_ref, diagnostic)
            manifest_artifacts.append({"artifact_id": "diagnostic"})
            artifact_ids.append("diagnostic")

        manifest_ref = _build_artifact_ref("artifact_manifest")
        await self._artifact_writer.write_artifact_manifest(
            manifest_ref,
            {
                "schema_version": "1.0",
                "request_id": request.request_id,
                "run_id": request.run_id,
                "artifacts": manifest_artifacts,
            },
        )
        artifact_ids.append("artifact_manifest")
        return tuple(_build_artifact_ref(artifact_id) for artifact_id in artifact_ids)

    async def _finalize_non_terminal_failure(
        self,
        origin: FailureOrigin,
        request: HarnessExecutionRequest,
        *,
        message: str | None = None,
    ) -> HarnessExecutionResult:
        """Finalize a runtime-owned failure without writing terminal_result."""
        self._enter_non_terminal_finalizing()
        result_class, status = classify_failure(origin)
        diagnostic: DiagnosticMetadata | None = None
        if message is not None:
            diagnostic = DiagnosticMetadata(
                error_code=origin.value,
                category=_diagnostic_category(origin),
                message=message,
                retryable=origin
                in {
                    FailureOrigin.BACKEND_FAILURE,
                    FailureOrigin.MODEL_FAILURE,
                    FailureOrigin.TOOL_FAILURE,
                    FailureOrigin.INFRASTRUCTURE_FAILURE,
                },
                origin=origin.value,
                fields=(),
            )

        try:
            artifact_refs = await self._write_non_terminal_artifacts(
                request,
                status=status,
                result_class=result_class,
                diagnostic=diagnostic,
            )
        except (ArtifactWriteError, OSError) as e:
            return self._failure_result(
                FailureOrigin.ARTIFACT_WRITE_FAILURE,
                request=request,
                message=f"Artifact finalization failed: {e}",
            )
        except Exception as e:
            return self._failure_result(
                FailureOrigin.ARTIFACT_WRITE_FAILURE,
                request=request,
                message=f"Artifact finalization failed: {e}",
            )

        if status == ExecutionStatus.INTERRUPTED:
            self._transition_to(RuntimeState.INTERRUPTED)
        else:
            self._transition_to(RuntimeState.FAILED)

        return HarnessExecutionResult(
            status=status,
            result_class=result_class,
            request_id=request.request_id,
            run_id=request.run_id,
            stage=request.stage,
            terminal_intent=None,
            artifact_refs=artifact_refs,
            compiled_harness=request.compiled_harness,
            usage=None,
            timing=self._timing(request),
            diagnostic=diagnostic,
        )

    async def _finalize_non_domain_session_result(
        self,
        request: HarnessExecutionRequest,
        session_result: GuardedSessionResult,
        *,
        status: ExecutionStatus,
        result_class: ExecutionResultClass,
    ) -> HarnessExecutionResult:
        """Finalize a guarded-session result that has no legal terminal artifact."""
        self._enter_non_terminal_finalizing()
        try:
            artifact_refs = await self._write_non_terminal_artifacts(
                request,
                status=status,
                result_class=result_class,
                session_result=session_result,
                diagnostic=session_result.diagnostic,
            )
        except (ArtifactWriteError, OSError) as e:
            return self._failure_result(
                FailureOrigin.ARTIFACT_WRITE_FAILURE,
                request=request,
                message=f"Artifact finalization failed: {e}",
            )
        except Exception as e:
            return self._failure_result(
                FailureOrigin.ARTIFACT_WRITE_FAILURE,
                request=request,
                message=f"Artifact finalization failed: {e}",
            )

        if status == ExecutionStatus.INTERRUPTED:
            self._transition_to(RuntimeState.INTERRUPTED)
        else:
            self._transition_to(RuntimeState.FAILED)
        return HarnessExecutionResult(
            status=status,
            result_class=result_class,
            request_id=request.request_id,
            run_id=request.run_id,
            stage=request.stage,
            terminal_intent=None,
            artifact_refs=tuple(session_result.artifact_refs) + artifact_refs,
            compiled_harness=request.compiled_harness,
            usage=session_result.usage,
            timing=self._timing(request),
            diagnostic=session_result.diagnostic,
        )

    def _success_result(
        self,
        request: HarnessExecutionRequest,
        terminal_intent: TerminalIntent | None,
        session_result: GuardedSessionResult | None,
        result_class: ExecutionResultClass = ExecutionResultClass.DOMAIN_TERMINAL,
    ) -> HarnessExecutionResult:
        """Build a success ``HarnessExecutionResult``."""
        usage: UsageMetadata | None = None
        if session_result is not None and session_result.usage is not None:
            usage = session_result.usage
        if usage is None:
            usage = UsageMetadata(
                model_calls=0,
                tool_calls=0,
                token_usage=None,
            )

        diagnostic: DiagnosticMetadata | None = None
        if session_result is not None and session_result.diagnostic is not None:
            diagnostic = session_result.diagnostic

        return HarnessExecutionResult(
            status=ExecutionStatus.COMPLETED,
            result_class=result_class,
            request_id=request.request_id,
            run_id=request.run_id,
            stage=request.stage,
            terminal_intent=terminal_intent,
            artifact_refs=_build_success_artifact_refs(
                include_diagnostic=diagnostic is not None
            ),
            compiled_harness=request.compiled_harness,
            usage=usage,
            timing=self._timing(request),
            diagnostic=diagnostic,
        )

    def _timing(self, request: HarnessExecutionRequest | None) -> TimingMetadata:
        """Build ``TimingMetadata`` from the current clock time."""
        now = self._clock.utc_now()
        iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        started = (
            self._started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if self._started_at is not None
            else iso
        )
        duration = 0.0
        if self._started_at is not None:
            duration = (now - self._started_at).total_seconds() * 1000.0
        return TimingMetadata(
            started_at=started,
            completed_at=iso,
            duration_ms=duration,
        )

    def _now_iso(self) -> str:
        """Current UTC time as ISO-8601."""
        return self._clock.utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------
    # Preflight helpers
    # ------------------------------------------------------------------

    async def _validate_request(self, request: HarnessExecutionRequest) -> None:
        """Validate the execution request fields."""
        if not request.request_id.strip():
            raise MillforgeConfigError("request_id must be non-empty")
        if not request.run_id.strip():
            raise MillforgeConfigError("run_id must be non-empty")
        if not request.work_item_id.strip():
            raise MillforgeConfigError("work_item_id must be non-empty")

    async def _check_cancelled(self, token: Any) -> None:
        """Check the cancellation token; raise ``OperationCancelledError`` if set."""
        if token.is_cancelled():
            raise OperationCancelledError("Request was cancelled before execution")

    async def _check_deadline(self, deadline: Deadline) -> None:
        """Check the deadline; raise ``DeadlineExceededError`` if expired."""
        now = self._clock.monotonic()
        if now >= deadline.effective_deadline_monotonic:
            raise DeadlineExceededError("Effective monotonic deadline has expired")

    def _deadline_from_timeout(
        self,
        timeout_seconds: float,
        absolute_deadline: str | None,
        *,
        source: Literal["request", "request_and_harness"] = "request",
    ) -> Deadline:
        """Build a rubric-shaped deadline and validate any absolute timestamp."""
        started = self._clock.monotonic()
        outer = started + timeout_seconds
        effective = outer

        if absolute_deadline is not None:
            try:
                normalized = absolute_deadline.replace("Z", "+00:00")
                deadline_dt = datetime.fromisoformat(normalized)
            except ValueError:
                raise MillforgeConfigError(
                    f"Invalid deadline format: {absolute_deadline!r}"
                )
            if self._clock.utc_now() >= deadline_dt:
                raise DeadlineExceededError(f"Deadline {absolute_deadline} has expired")

        return Deadline(
            started_monotonic=started,
            outer_deadline_monotonic=outer,
            effective_deadline_monotonic=effective,
            source=source,
        )

    async def _verify_plan_identity(
        self, plan: CompiledHarnessPlan, request: HarnessExecutionRequest
    ) -> None:
        """Verify compiled-plan identity fields against the request.

        Checks harness_id and harness_version.
        Raises ``HarnessMismatchError`` on any mismatch.
        """
        ref = request.compiled_harness

        if plan.harness_id != ref.identity.harness_id:
            raise HarnessMismatchError(
                f"Harness ID mismatch: plan={plan.harness_id!r} ref={ref.identity.harness_id!r}"
            )
        if plan.harness_version != ref.identity.harness_version:
            raise HarnessMismatchError(
                f"Harness version mismatch: plan={plan.harness_version!r} ref={ref.identity.harness_version!r}"
            )

    async def _verify_plan_hash(
        self, plan: CompiledHarnessPlan, request: HarnessExecutionRequest
    ) -> None:
        """Verify compiled-plan hash against the request.

        Checks compiled_sha256 against expected_hash.digest.
        Raises ``HarnessMismatchError`` on mismatch.
        """
        ref = request.compiled_harness

        if plan.compiled_sha256 != ref.expected_hash.digest:
            raise HarnessMismatchError(
                f"Compiled SHA-256 mismatch: plan={plan.compiled_sha256!r} expected={ref.expected_hash.digest!r}"
            )

    async def _check_stage(
        self, plan: CompiledHarnessPlan, request: HarnessExecutionRequest
    ) -> None:
        """Check the stage is compatible with execution."""
        if request.stage.plane != "execution":
            raise MillforgeConfigError(
                f"Incompatible stage plane: expected 'execution', got {request.stage.plane!r}"
            )
        if request.stage.stage_kind_id not in plan.stage_kind_ids:
            raise MillforgeConfigError(
                f"Incompatible stage kind: {request.stage.stage_kind_id!r} not in compiled plan"
            )

    async def _check_model_profile(
        self, plan: CompiledHarnessPlan, request: HarnessExecutionRequest
    ) -> None:
        """Check the request model profile matches the compiled plan."""
        if request.model_profile.profile_id != plan.model_profile.profile_id:
            raise MillforgeConfigError(
                f"Model profile mismatch: plan={plan.model_profile.profile_id!r} request={request.model_profile.profile_id!r}"
            )

    async def _check_capabilities(
        self, plan: CompiledHarnessPlan, request: HarnessExecutionRequest
    ) -> None:
        """Check required capabilities are present."""
        granted = {grant.capability_id for grant in request.capability_envelope.grants}
        missing = [
            capability
            for capability in plan.required_capabilities
            if capability not in granted
        ]
        if missing:
            raise MillforgeConfigError(
                f"Missing capability grants: {', '.join(missing)}"
            )

    async def _check_input_artifacts(self, request: HarnessExecutionRequest) -> None:
        """Validate admitted input artifact refs before backend work."""
        if not request.input_artifacts:
            raise MillforgeConfigError(
                "No input artifact references present in request"
            )

        resolved_base = (request.run_directory.path / "millforge").resolve()
        resolved_paths: set[Path] = set()

        for artifact in request.input_artifacts:
            ref_path = artifact.path
            if ref_path.is_absolute():
                raise MillforgeConfigError(
                    f"Input artifact {artifact.artifact_id!r} uses an absolute path: {ref_path!r}"
                )
            if ".." in ref_path.parts:
                raise MillforgeConfigError(
                    f"Input artifact {artifact.artifact_id!r} contains parent traversal: {ref_path!r}"
                )

            try:
                candidate = (request.run_directory.path / ref_path).resolve(strict=True)
            except FileNotFoundError as e:
                raise MillforgeConfigError(
                    f"Input artifact {artifact.artifact_id!r} is missing: {ref_path!r}"
                ) from e
            except OSError as e:
                raise MillforgeConfigError(
                    f"Input artifact {artifact.artifact_id!r} cannot be resolved: {ref_path!r}: {e}"
                ) from e

            try:
                candidate.relative_to(resolved_base)
            except ValueError:
                raise MillforgeConfigError(
                    f"Input artifact {artifact.artifact_id!r} resolves outside millforge/: {ref_path!r}"
                )

            if candidate in resolved_paths:
                raise MillforgeConfigError(
                    f"Input artifact path is reused by multiple admitted artifacts: {ref_path!r}"
                )
            resolved_paths.add(candidate)

            if not candidate.is_file():
                raise MillforgeConfigError(
                    f"Input artifact {artifact.artifact_id!r} is not a file: {ref_path!r}"
                )

            try:
                with candidate.open("rb") as handle:
                    handle.read(1)
            except OSError as e:
                raise MillforgeConfigError(
                    f"Input artifact {artifact.artifact_id!r} is not readable: {ref_path!r}: {e}"
                ) from e

    async def _prepare_run_directory(self, request: HarnessExecutionRequest) -> None:
        """Validate the request run directory and create its millforge subtree."""
        if request.run_directory.run_id != request.run_id:
            raise MillforgeConfigError(
                "Run directory run_id must match execution request run_id"
            )

        run_path = request.run_directory.path
        try:
            if run_path.exists() and not run_path.is_dir():
                raise MillforgeConfigError(
                    f"Run directory path is not a directory: {run_path!r}"
                )
            (run_path / "millforge").mkdir(parents=True, exist_ok=True)
        except MillforgeConfigError:
            raise
        except OSError as e:
            raise MillforgeConfigError(
                f"Run directory preparation failed: {run_path!r}: {e}"
            ) from e

    async def _validate_session_result(
        self,
        session_request: GuardedSessionRequest,
        session_result: GuardedSessionResult,
    ) -> None:
        """Validate backend session identity before interpreting the result."""
        if session_result.session_id != session_request.session_id:
            raise MillforgeConfigError(
                "Backend returned mismatched session_id: "
                f"{session_result.session_id!r} != {session_request.session_id!r}"
            )

    async def _validate_terminal_intent(
        self,
        plan: CompiledHarnessPlan,
        request: HarnessExecutionRequest,
        terminal_intent: TerminalIntent,
    ) -> None:
        """Validate terminal intent identity and compiled-plan terminal legality."""
        if terminal_intent.request_id != request.request_id:
            raise MillforgeConfigError("terminal_intent.request_id must match request")
        if terminal_intent.run_id != request.run_id:
            raise MillforgeConfigError("terminal_intent.run_id must match request")
        if terminal_intent.stage != request.stage:
            raise MillforgeConfigError("terminal_intent.stage must match request")

        expected_terminal = plan.terminal_result_map.get(
            terminal_intent.terminal_node_id
        )
        if expected_terminal is None:
            raise MillforgeConfigError(
                "terminal_intent.terminal_node_id is not declared by compiled plan"
            )
        if terminal_intent.terminal_result != expected_terminal:
            raise MillforgeConfigError(
                "terminal_intent.terminal_result does not match compiled plan"
            )

    # ------------------------------------------------------------------
    # Artifact writing helpers
    # ------------------------------------------------------------------

    async def _write_artifacts(
        self,
        request: HarnessExecutionRequest,
        session_result: GuardedSessionResult,
        terminal_intent: TerminalIntent,
        result_class: ExecutionResultClass,
        status: ExecutionStatus,
    ) -> None:
        """Write the standard runtime artifacts (steps 16-20)."""
        # Step 16: terminal result
        terminal_ref = _build_artifact_ref("terminal_result")
        await self._artifact_writer.write_terminal_result(
            terminal_ref,
            {
                "schema_version": "1.0",
                "request_id": request.request_id,
                "run_id": request.run_id,
                "stage": request.stage.model_dump(mode="json"),
                "terminal_result": terminal_intent.terminal_result,
                "result_class": result_class.value,
                "summary_artifact_paths": tuple(
                    str(artifact.path) for artifact in terminal_intent.artifact_refs
                ),
                "compiled_harness_sha256": request.compiled_harness.expected_hash.digest,
            },
        )

        # Step 17: events
        events_ref = _build_artifact_ref("events")
        events_list = (
            [e.model_dump(mode="json") for e in session_result.events]
            if session_result.events
            else []
        )
        await self._artifact_writer.write_events(events_ref, events_list)

        # Step 18: tool trace
        tool_trace_ref = _build_artifact_ref("tool_trace")
        tool_trace_list = (
            [t.model_dump(mode="json") for t in session_result.tool_trace]
            if session_result.tool_trace
            else []
        )
        await self._artifact_writer.write_tool_trace(tool_trace_ref, tool_trace_list)

        # Step 19: metrics
        metrics_ref = _build_artifact_ref("metrics")
        metrics_data: dict[str, Any] = {
            "schema_version": "1.0",
            "request_id": request.request_id,
            "run_id": request.run_id,
            "session_id": session_result.session_id,
            "status": session_result.status.value,
        }
        if session_result.usage is not None:
            metrics_data["usage"] = session_result.usage.model_dump(mode="json")
        await self._artifact_writer.write_metrics(metrics_ref, metrics_data)

        # Step 19b: execution summary (always written for every return path)
        exec_summary_ref = _build_artifact_ref("execution_summary")
        await self._artifact_writer.write_execution_summary(
            exec_summary_ref,
            {
                "schema_version": "1.0",
                "request_id": request.request_id,
                "run_id": request.run_id,
                "stage": request.stage.model_dump(mode="json"),
                "status": status.value,
                "result_class": result_class.value,
                "diagnostic_error_code": None,
            },
        )

        if session_result.diagnostic is not None:
            diagnostic_ref = _build_artifact_ref("diagnostic")
            await self._artifact_writer.write_diagnostic(
                diagnostic_ref, session_result.diagnostic
            )

        # Step 20: manifest
        manifest_ref = _build_artifact_ref("artifact_manifest")
        manifest_artifacts = [
            {"artifact_id": "terminal_result"},
            {"artifact_id": "events"},
            {"artifact_id": "tool_trace"},
            {"artifact_id": "metrics"},
            {"artifact_id": "execution_summary"},
        ]
        if session_result.diagnostic is not None:
            manifest_artifacts.append({"artifact_id": "diagnostic"})
        await self._artifact_writer.write_artifact_manifest(
            manifest_ref,
            {
                "schema_version": "1.0",
                "request_id": request.request_id,
                "run_id": request.run_id,
                "artifacts": manifest_artifacts,
            },
        )

    # ------------------------------------------------------------------
    # 21-step execution algorithm
    # ------------------------------------------------------------------

    async def execute(self, request: HarnessExecutionRequest) -> HarnessExecutionResult:
        """Execute the 21-step algorithm against *request*.

        Parameters
        ----------
        request : HarnessExecutionRequest
            The harness execution request.

        Returns
        -------
        HarnessExecutionResult
            The result of execution.
        """
        # Reset state at start.
        self._state = RuntimeState.RECEIVED
        self._started_at = self._clock.utc_now()
        cancellation_token: Any = None
        run_directory_prepared = False

        async def fail(
            origin: FailureOrigin,
            *,
            message: str | None = None,
        ) -> HarnessExecutionResult:
            if run_directory_prepared:
                return await self._finalize_non_terminal_failure(
                    origin,
                    request=request,
                    message=message,
                )
            return self._failure_result(
                origin,
                request=request,
                message=message,
            )

        try:
            # ----------------------------------------------------------
            # Steps 1-2: Receive & validate request
            # ----------------------------------------------------------
            # Step 1: request received (implicit via method entry).
            # Step 2: validate request.
            try:
                await self._validate_request(request)
            except MillforgeConfigError as e:
                return await fail(
                    FailureOrigin.INFRASTRUCTURE_FAILURE,
                    message=str(e),
                )

            # ----------------------------------------------------------
            # Steps 3-4: Resolve cancellation & check cancelled
            # ----------------------------------------------------------
            try:
                cancellation_token = self._cancellation_resolver.resolve(
                    request.cancellation
                )
            except Exception as e:
                return await fail(
                    FailureOrigin.INFRASTRUCTURE_FAILURE,
                    message=f"Cancellation resolution failed: {e}",
                )

            try:
                await self._check_cancelled(cancellation_token)
            except OperationCancelledError as e:
                return await fail(
                    FailureOrigin.ALREADY_CANCELLED,
                    message=str(e),
                )

            # ----------------------------------------------------------
            # Step 5: Check deadline
            # ----------------------------------------------------------
            try:
                deadline = self._deadline_from_timeout(
                    request.timeout.timeout_seconds,
                    request.timeout.deadline,
                )
                await self._check_deadline(deadline)
            except DeadlineExceededError as e:
                return await fail(
                    FailureOrigin.EXPIRED_DEADLINE,
                    message=str(e),
                )
            except MillforgeConfigError as e:
                return await fail(
                    FailureOrigin.INFRASTRUCTURE_FAILURE,
                    message=str(e),
                )

            # ----------------------------------------------------------
            # Step 6: Record received (stay in RECEIVED)
            # ----------------------------------------------------------
            # State remains RECEIVED — no explicit transition needed.

            # ----------------------------------------------------------
            # Step 6b: Validate/create run directory before plan load
            # ----------------------------------------------------------
            try:
                await self._prepare_run_directory(request)
                run_directory_prepared = True
            except MillforgeConfigError as e:
                return await fail(
                    FailureOrigin.INFRASTRUCTURE_FAILURE,
                    message=str(e),
                )

            # ----------------------------------------------------------
            # Steps 7-8: Load & verify compiled plan
            # ----------------------------------------------------------
            try:
                plan = await self._plan_loader.load(request.compiled_harness)
            except (FileNotFoundError, ValueError, TypeError) as e:
                return await fail(
                    FailureOrigin.COMPILED_HARNESS_INVALID,
                    message=f"Plan load failed: {e}",
                )
            except Exception as e:
                return await fail(
                    FailureOrigin.INFRASTRUCTURE_FAILURE,
                    message=f"Plan load failed: {e}",
                )

            try:
                await self._verify_plan_identity(plan, request)
            except HarnessMismatchError as e:
                return await fail(
                    FailureOrigin.IDENTITY_MISMATCH,
                    message=str(e),
                )

            try:
                await self._verify_plan_hash(plan, request)
            except HarnessMismatchError as e:
                return await fail(
                    FailureOrigin.HASH_MISMATCH,
                    message=str(e),
                )

            # ----------------------------------------------------------
            # Step 9: Verify stage, profile, capabilities, and inputs
            # ----------------------------------------------------------
            try:
                await self._check_stage(plan, request)
            except MillforgeConfigError as e:
                return await fail(
                    FailureOrigin.INCOMPATIBLE_STAGE,
                    message=str(e),
                )

            try:
                await self._check_model_profile(plan, request)
            except MillforgeConfigError as e:
                return await fail(
                    FailureOrigin.IDENTITY_MISMATCH,
                    message=str(e),
                )

            try:
                await self._check_capabilities(plan, request)
            except MillforgeConfigError as e:
                return await fail(
                    FailureOrigin.MISSING_CAPABILITY,
                    message=str(e),
                )

            try:
                await self._check_input_artifacts(request)
            except MillforgeConfigError as e:
                return await fail(
                    FailureOrigin.MISSING_CAPABILITY,
                    message=str(e),
                )

            # ----------------------------------------------------------
            # Step 10: Record verified → VERIFIED
            # ----------------------------------------------------------
            self._transition_to(RuntimeState.VERIFIED)

            # ----------------------------------------------------------
            # Step 11: Construct guarded session
            # ----------------------------------------------------------
            session_request = GuardedSessionRequest(
                session_id=str(uuid.uuid4()),
                execution_request=request,
                deadline=self._deadline_from_timeout(
                    request.timeout.timeout_seconds,
                    request.timeout.deadline,
                    source="request_and_harness",
                ),
            )

            # ----------------------------------------------------------
            # Step 12: Record session constructed → BACKEND_SESSION_CONSTRUCTED
            # ----------------------------------------------------------
            self._transition_to(RuntimeState.BACKEND_SESSION_CONSTRUCTED)

            # ----------------------------------------------------------
            # Step 13: Recheck invocation boundaries immediately before backend
            # ----------------------------------------------------------
            try:
                await self._check_deadline(session_request.deadline)
            except DeadlineExceededError as e:
                return await fail(
                    FailureOrigin.EXPIRED_DEADLINE,
                    message=str(e),
                )

            try:
                await self._check_cancelled(cancellation_token)
            except OperationCancelledError as e:
                return await fail(
                    FailureOrigin.ALREADY_CANCELLED,
                    message=str(e),
                )

            # ----------------------------------------------------------
            # Step 14: Record running → RUNNING before backend invocation
            # ----------------------------------------------------------
            self._transition_to(RuntimeState.RUNNING)

            # ----------------------------------------------------------
            # Step 15: Run backend session (exactly one call)
            # ----------------------------------------------------------
            try:
                session_result = await self._backend.run_session(session_request)
            except OperationCancelledError as e:
                return await fail(
                    FailureOrigin.ALREADY_CANCELLED,
                    message=str(e),
                )
            except DeadlineExceededError as e:
                return await fail(
                    FailureOrigin.EXPIRED_DEADLINE,
                    message=str(e),
                )
            except BackendTranslationError as e:
                return await fail(
                    FailureOrigin.BACKEND_FAILURE,
                    message=str(e),
                )
            except ModelTransportError as e:
                return await fail(
                    FailureOrigin.MODEL_FAILURE,
                    message=str(e),
                )
            except ToolInvokeError as e:
                return await fail(
                    FailureOrigin.TOOL_FAILURE,
                    message=str(e),
                )
            except ArtifactWriteError as e:
                return await fail(
                    FailureOrigin.ARTIFACT_WRITE_FAILURE,
                    message=str(e),
                )
            except MillforgeError as e:
                # Any other Millforge-owned exception from the backend.
                return await fail(
                    FailureOrigin.BACKEND_FAILURE,
                    message=str(e),
                )
            except Exception as e:
                # Non-Millforge exceptions at the public boundary → infrastructure.
                return await fail(
                    FailureOrigin.INFRASTRUCTURE_FAILURE,
                    message=str(e),
                )

            # ----------------------------------------------------------
            # Step 16: Validate returned session identity
            # ----------------------------------------------------------
            try:
                await self._validate_session_result(session_request, session_result)
            except MillforgeConfigError as e:
                return await fail(
                    FailureOrigin.INVALID_TERMINAL,
                    message=str(e),
                )

            session_result_class, session_status = classify_guarded_session_status(
                session_result.status
            )
            domain_artifact_statuses = {
                GuardedSessionStatus.TERMINAL,
                GuardedSessionStatus.REJECTED,
            }
            if session_result.status not in domain_artifact_statuses:
                return await self._finalize_non_domain_session_result(
                    request,
                    session_result,
                    status=session_status,
                    result_class=session_result_class,
                )

            # ----------------------------------------------------------
            # Step 17: Check terminal intent
            # ----------------------------------------------------------
            terminal_intent = session_result.terminal_intent
            if terminal_intent is None:
                diagnostic = DiagnosticMetadata(
                    error_code=FailureOrigin.INVALID_TERMINAL.value,
                    category=_diagnostic_category(FailureOrigin.INVALID_TERMINAL),
                    message="Backend session produced no terminal intent",
                    retryable=False,
                    origin=FailureOrigin.INVALID_TERMINAL.value,
                    fields=(),
                )
                return await self._finalize_non_domain_session_result(
                    request,
                    session_result.model_copy(update={"diagnostic": diagnostic}),
                    status=ExecutionStatus.FAILED,
                    result_class=ExecutionResultClass.TERMINAL_RESULT_INVALID,
                )
            try:
                await self._validate_terminal_intent(plan, request, terminal_intent)
            except MillforgeConfigError as e:
                return await fail(
                    FailureOrigin.INVALID_TERMINAL,
                    message=str(e),
                )

            # ----------------------------------------------------------
            # Step 18: Record terminal intent received → TERMINAL_INTENT_RECEIVED
            # ----------------------------------------------------------
            self._transition_to(RuntimeState.TERMINAL_INTENT_RECEIVED)

            # ----------------------------------------------------------
            # Steps 19-20: Write artifacts (in FINALIZING state)
            # ----------------------------------------------------------
            self._transition_to(RuntimeState.FINALIZING)
            try:
                await self._write_artifacts(
                    request,
                    session_result,
                    terminal_intent,
                    session_result_class,
                    session_status,
                )
            except (ArtifactWriteError, OSError) as e:
                return await fail(
                    FailureOrigin.ARTIFACT_WRITE_FAILURE,
                    message=f"Artifact write failed: {e}",
                )
            except Exception as e:
                return await fail(
                    FailureOrigin.ARTIFACT_WRITE_FAILURE,
                    message=f"Artifact write failed: {e}",
                )

            # ----------------------------------------------------------
            # Step 21: Return result
            # ----------------------------------------------------------
            self._transition_to(RuntimeState.COMPLETED)
            return self._success_result(
                request,
                terminal_intent,
                session_result,
                result_class=session_result_class,
            )

        # At the public boundary, only Millforge-owned exceptions are translated.
        # BaseException is never caught.
        except MillforgeError:
            raise


__all__: list[str] = [
    "classify_guarded_session_status",
    "classify_failure",
    "DefaultHarnessRuntime",
    "FailureOrigin",
    "RuntimeState",
]
