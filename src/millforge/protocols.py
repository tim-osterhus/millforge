"""Protocol definitions for the Millforge runtime.

Each protocol defines the interface contract that real implementations
and test doubles must satisfy. All types used in method signatures are
Millforge-owned contract types — Forge types are never exposed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class HarnessRuntime(Protocol):
    """Interface for compiled-harness interactions.

    Implementations wrap access to compiled harness runtimes, providing
    identity resolution and stage execution. All types used in method
    signatures are Millforge-owned contract types.
    """

    async def execute(self, request: StageExecutionRequest) -> HarnessExecutionResult:
        """Execute a compiled-harness stage.

        Parameters
        ----------
        request : StageExecutionRequest
            The stage execution request describing which stage to run
            and under what identity.

        Returns
        -------
        HarnessExecutionResult
            The result of the harness execution, including exit code,
            captured output, and success indicator.
        """
        ...

    def get_identity(self) -> CompiledHarnessIdentity:
        """Return the identity of the compiled harness.

        Returns
        -------
        CompiledHarnessIdentity
            Immutable identity containing the compiled plan ID, harness
            ID, and version.
        """
        ...


@runtime_checkable
class GuardrailBackend(Protocol):
    """Interface for guardrail translation and execution.

    Implementations translate requests through guardrail policies and
    return verdicts wrapped in Millforge session types. All types used
    in method signatures are Millforge-owned contract types.
    """

    async def check(self, request: GuardedSessionRequest) -> GuardedSessionResult:
        """Evaluate guardrails against a session request.

        Parameters
        ----------
        request : GuardedSessionRequest
            The guarded session request to evaluate.

        Returns
        -------
        GuardedSessionResult
            The guardrail evaluation result, including whether the
            session was blocked and the blocking reason (if any).
        """
        ...


@runtime_checkable
class ModelClient(Protocol):
    """Interface for model invocation.

    Implementations wrap LLM backends (HTTP, SDK, local inference, etc.)
    and expose a single ``send`` method that accepts a validated request
    and returns a validated response. All types used in method signatures
    are Millforge-owned contract types.
    """

    async def send(self, request: ValidatedModelRequest) -> ValidatedModelResponse:
        """Send a validated model request and return the response.

        Parameters
        ----------
        request : ValidatedModelRequest
            The validated inference request containing messages, tools,
            temperature, and other parameters.

        Returns
        -------
        ValidatedModelResponse
            The validated inference response including content, tool
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

    async def execute(self, call: ValidatedToolCall) -> ValidatedToolResult:
        """Execute a validated tool call and return the result.

        Parameters
        ----------
        call : ValidatedToolCall
            The validated tool call specifying the tool name, call ID,
            and arguments.

        Returns
        -------
        ValidatedToolResult
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
