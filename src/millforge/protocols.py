"""Protocol definitions for the Millforge runtime.

Each protocol defines the interface contract that real implementations
and test doubles must satisfy. All types used in method signatures are
Millforge-owned contract types — Forge types are never exposed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from millforge.compiled_plan import CompiledHarnessPlan
from millforge.contracts import (
    ArtifactRef,
    CancellationRef,
    CompiledHarnessRef,
    GuardedSessionRequest,
    GuardedSessionResult,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ToolExecutionContext,
    ToolExecutionResult,
    ValidatedToolCall,
)


@runtime_checkable
class AsyncHttpTransport(Protocol):
    """Caller-owned async HTTP transport accepted by the live factory.

    ``httpx.AsyncBaseTransport`` implements this shape. Keeping the public
    protocol provider-neutral preserves the package boundary that confines the
    concrete HTTP dependency to ``millforge.model_backend``.
    """

    async def handle_async_request(self, request: Any) -> Any:
        """Handle one async HTTP request."""
        ...

    async def aclose(self) -> None:
        """Close transport resources when the caller chooses."""
        ...


@runtime_checkable
class CancellationToken(Protocol):
    """Protocol for cancellation tokens.

    Provides an invocation-scoped identifier and synchronous check for
    whether cancellation has been requested. Implementations may back
    this with in-memory state, a remote signal, or a composite
    of multiple sources.

    This is intentionally a plain Protocol (not a Pydantic model)
    to keep it independent of the Pydantic hierarchy — cancellation
    state spans across run boundaries and should not be serialised
    as a contract value.
    """

    @property
    def cancellation_id(self) -> str:
        """Return the cancellation identifier."""
        ...

    def is_cancelled(self) -> bool:
        """Check whether cancellation has been requested."""
        ...

    async def wait(self) -> None:
        """Wait until cancellation is requested."""
        ...

    @property
    def reason(self) -> str | None:
        """Return a bounded cancellation reason, when present."""
        ...


@runtime_checkable
class CancellationResolver(Protocol):
    """Protocol for resolving cancellation tokens by reference.

    Given a cancellation reference, returns a CancellationToken
    that can be polled for cancellation status.  This allows
    passing lightweight string refs through serialisation boundaries
    and resolving them into checkable tokens at the point of use.
    """

    def resolve(self, ref: CancellationRef) -> CancellationToken:
        """Resolve a cancellation reference into a CancellationToken.

        Parameters
        ----------
        ref : CancellationRef
            The cancellation reference to resolve.

        Returns
        -------
        CancellationToken
            A checkable cancellation token.
        """
        ...


@runtime_checkable
class CompiledHarnessLoader(Protocol):
    """Interface for loading compiled harness plans.

    Implementations load a CompiledHarnessPlan from a CompiledHarnessRef,
    performing any necessary deserialization and cryptographic verification
    of the plan body before returning it.
    """

    async def load(self, ref: CompiledHarnessRef) -> CompiledHarnessPlan:
        """Load a compiled harness plan from the given reference.

        Parameters
        ----------
        ref : CompiledHarnessRef
            Reference to the compiled harness, including its identity,
            filesystem path, and expected cryptographic hash.

        Returns
        -------
        CompiledHarnessPlan
            The loaded and verified compiled harness plan.
        """
        ...


@runtime_checkable
class RuntimeArtifactWriter(Protocol):
    """Interface for writing runtime artifacts.

    Implementations write the seven standard artifact types that
    the runtime produces during a harness run: terminal result,
    execution summary, events, tool trace, metrics, artifact
    manifest, and diagnostic output.

    Each method accepts an ``ArtifactRef`` identifying the target
    location and a ``data`` payload.  Implementations handle
    serialisation, atomic writes, and error reporting internally.
    """

    async def write_terminal_result(self, ref: ArtifactRef, data: Any) -> None:
        """Write the terminal result artifact."""
        ...

    async def write_execution_summary(self, ref: ArtifactRef, data: Any) -> None:
        """Write the execution summary artifact."""
        ...

    async def write_events(self, ref: ArtifactRef, data: Any) -> None:
        """Write the events artifact (JSONL format)."""
        ...

    async def write_tool_trace(self, ref: ArtifactRef, data: Any) -> None:
        """Write the tool trace artifact (JSONL format)."""
        ...

    async def write_metrics(self, ref: ArtifactRef, data: Any) -> None:
        """Write the metrics artifact."""
        ...

    async def write_artifact_manifest(self, ref: ArtifactRef, data: Any) -> None:
        """Write the artifact manifest artifact."""
        ...

    async def write_diagnostic(self, ref: ArtifactRef, data: Any) -> None:
        """Write the diagnostic artifact."""
        ...


@runtime_checkable
class RuntimeClock(Protocol):
    """Interface for deterministic clock operations.

    Provides UTC datetime and monotonic float access in a fully
    deterministic manner.  Implementations are synchronous by
    design — no async dependencies — so that the clock can be
    used in fake implementations without requiring an event loop.
    """

    def utc_now(self) -> datetime:
        """Return the current UTC datetime.

        Returns
        -------
        datetime
            The current UTC datetime.
        """
        ...

    def monotonic(self) -> float:
        """Return a monotonic float value (fractional seconds).

        The returned value is strictly increasing across calls and
        is suitable for measuring elapsed time.  The absolute value
        has no meaning; only differences between successive calls
        are meaningful.

        Returns
        -------
        float
            Monotonic time value in fractional seconds.
        """
        ...


@runtime_checkable
class HarnessRuntime(Protocol):
    """Interface for compiled-harness interactions.

    Implementations wrap access to compiled harness runtimes, providing
    identity resolution and stage execution. All types used in method
    signatures are Millforge-owned contract types.
    """

    async def execute(self, request: HarnessExecutionRequest) -> HarnessExecutionResult:
        """Execute a compiled-harness stage.

        Parameters
        ----------
        request : HarnessExecutionRequest
            The harness execution request describing which stage to run
            and under what identity, with capability grants and artifacts.

        Returns
        -------
        HarnessExecutionResult
            The result of the harness execution, including execution
            status, result classification, timing, and diagnostics.
        """
        ...


@runtime_checkable
class GuardrailBackend(Protocol):
    """Interface for guardrail translation and execution.

    Implementations translate requests through guardrail policies and
    return verdicts wrapped in Millforge session types. All types used
    in method signatures are Millforge-owned contract types.
    """

    async def run_session(self, request: GuardedSessionRequest) -> GuardedSessionResult:
        """Evaluate guardrails against a session request.

        Parameters
        ----------
        request : GuardedSessionRequest
            The guarded session request to evaluate.

        Returns
        -------
        GuardedSessionResult
            The guardrail evaluation result, including session status,
            terminal intent, events, and tool trace records.
        """
        ...


@runtime_checkable
class ModelClient(Protocol):
    """Interface for model invocation.

    Implementations wrap LLM backends (HTTP, SDK, local inference, etc.)
    and expose a single ``complete`` method that accepts a validated request
    and returns a validated response. All types used in method signatures
    are Millforge-owned contract types.
    """

    async def complete(
        self, request: ModelCompletionRequest
    ) -> ModelCompletionResponse:
        """Send a validated model request and return the response.

        Parameters
        ----------
        request : ModelCompletionRequest
            The model completion request containing messages, tools,
            temperature, and other parameters.

        Returns
        -------
        ModelCompletionResponse
            The model completion response including content, tool
            calls, finish reason, and usage metadata.
        """
        ...


@runtime_checkable
class ToolExecutor(Protocol):
    """Interface for tool execution.

    Implementations wrap callable tools and provide a uniform interface
    for invocation and capability discovery. All types used in method
    signatures are Millforge-owned contract types.
    """

    async def execute(
        self, call: ValidatedToolCall, context: ToolExecutionContext
    ) -> ToolExecutionResult:
        """Execute a validated tool call with execution context and return the result.

        Parameters
        ----------
        call : ValidatedToolCall
            The validated tool call specifying the tool name, call ID,
            and arguments.
        context : ToolExecutionContext
            The execution context including request identity, stage,
            run directory, capability envelope, timeout, and cancellation.

        Returns
        -------
        ToolExecutionResult
            The tool execution result including output text, error
            information, and execution duration.
        """
        ...

    def supports_tool(self, name: str) -> bool:
        """Check whether a tool is supported by this executor.

        Parameters
        ----------
        name : str
            The name of the tool to check.

        Returns
        -------
        bool
            True if the tool is supported, False otherwise.
        """
        ...
