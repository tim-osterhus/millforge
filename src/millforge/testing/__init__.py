"""Test doubles (fakes) for Millforge runtime protocols.

All fakes are deterministic, make no network calls, and record every
request they receive. They support scripting success/failure scenarios
so tests can verify both happy-path and error-handling behaviour.
"""

from __future__ import annotations

from typing import Callable, Optional

from millforge.contracts import (
    GuardedSessionRequest,
    GuardedSessionResult,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ToolExecutionContext,
    ToolExecutionResult,
    ValidatedToolCall,
)


class FakeModelClient:
    """Fake implementation of ``ModelClient``.

    Supports scripting a sequence of ``ModelCompletionResponse`` objects
    (success path) and exceptions (failure path). Records every
    ``ModelCompletionRequest`` passed to ``complete()``.

    Parameters
    ----------
    responses : list[ModelCompletionResponse], optional
        Scripted success responses, returned in order.
    exceptions : list[Exception], optional
        Scripted exceptions, raised in order.
    """

    def __init__(
        self,
        responses: Optional[list[ModelCompletionResponse]] = None,
        exceptions: Optional[list[Exception]] = None,
    ) -> None:
        self._responses: list[ModelCompletionResponse] = list(responses or [])
        self._exceptions: list[Exception] = list(exceptions or [])
        self._request_log: list[ModelCompletionRequest] = []

    @property
    def requests(self) -> list[ModelCompletionRequest]:
        """Recorded model completion calls."""
        return self._request_log

    @property
    def call_count(self) -> int:
        """Number of completed ``complete()`` invocations recorded."""
        return len(self._request_log)

    def assert_not_called(self) -> None:
        """Assert that no model calls were attempted."""
        if self._request_log:
            raise AssertionError(
                f"Expected no model calls, recorded {len(self._request_log)}"
            )

    async def complete(
        self, request: ModelCompletionRequest
    ) -> ModelCompletionResponse:
        """Send a validated model request and return the response.

        Returns the next scripted response, or raises the next
        scripted exception if one is set. Raises ``IndexError``
        when no more scripted items remain.

        Parameters
        ----------
        request : ModelCompletionRequest
            The model completion request.

        Returns
        -------
        ModelCompletionResponse
            The next scripted response.

        Raises
        ------
        IndexError
            If no scripted responses or exceptions remain.
        Exception
            If the next scripted item is an exception.
        """
        self._request_log.append(request)

        if self._exceptions:
            raise self._exceptions.pop(0)

        if self._responses:
            return self._responses.pop(0)

        raise IndexError(
            f"No scripted responses remain for {type(self).__name__}. "
            f"Add responses via the constructor or extend scripted items."
        )


class FakeGuardrailBackend:
    """Fake implementation of ``GuardrailBackend``.

    Supports scripting success/failure responses and records every
    ``GuardedSessionRequest`` passed to ``run_session()``.

    Parameters
    ----------
    responses : list[GuardedSessionResult], optional
        Scripted guardrail results, returned in order.
    exceptions : list[Exception], optional
        Scripted exceptions, raised in order.
    """

    def __init__(
        self,
        responses: Optional[list[GuardedSessionResult]] = None,
        exceptions: Optional[list[Exception]] = None,
        expected_cancellation_id: str | None = None,
    ) -> None:
        self._responses: list[GuardedSessionResult] = list(responses or [])
        self._exceptions: list[Exception] = list(exceptions or [])
        self._expected_cancellation_id = expected_cancellation_id
        self._request_log: list[GuardedSessionRequest] = []

    @property
    def requests(self) -> list[GuardedSessionRequest]:
        """Recorded guardrail session calls."""
        return self._request_log

    @property
    def call_count(self) -> int:
        """Number of ``run_session()`` invocations recorded."""
        return len(self._request_log)

    def assert_not_called(self) -> None:
        """Assert that no guarded sessions were attempted."""
        if self._request_log:
            raise AssertionError(
                f"Expected no guardrail calls, recorded {len(self._request_log)}"
            )

    async def run_session(self, request: GuardedSessionRequest) -> GuardedSessionResult:
        """Evaluate guardrails against a session request.

        Returns the next scripted response, or raises the next
        scripted exception. Raises ``IndexError`` when no scripted
        items remain.

        Parameters
        ----------
        request : GuardedSessionRequest
            The guarded session request to evaluate.

        Returns
        -------
        GuardedSessionResult
            The next scripted guardrail result.

        Raises
        ------
        IndexError
            If no scripted responses or exceptions remain.
        Exception
            If the next scripted item is an exception.
        """
        if self._expected_cancellation_id is not None:
            actual = request.execution_request.cancellation.cancellation_id
            if actual != self._expected_cancellation_id:
                raise AssertionError(
                    f"Expected cancellation ID {self._expected_cancellation_id!r}, "
                    f"got {actual!r}"
                )

        self._request_log.append(request)

        if self._exceptions:
            raise self._exceptions.pop(0)

        if self._responses:
            return self._responses.pop(0)

        raise IndexError(
            f"No scripted responses remain for {type(self).__name__}. "
            f"Add responses via the constructor or extend scripted items."
        )


class FakeToolExecutor:
    """Fake implementation of ``ToolExecutor``.

    Supports scripting per-canonical-tool-id results and records every
    ``ValidatedToolCall`` passed to ``execute()`` in ``calls``.

    Parameters
    ----------
    results : dict[str, list[ToolExecutionResult]], optional
        Mapping of ``ValidatedToolCall.binding.tool_id`` values to lists of
        scripted results. Results are consumed in order per canonical tool id.
    exceptions : dict[str, list[Exception]], optional
        Mapping of ``ValidatedToolCall.binding.tool_id`` values to lists of
        scripted exceptions. Exceptions are consumed in order per canonical
        tool id.
    supported_tools : set[str], optional
        Set of tool names that ``supports_tool`` returns True for.
        Defaults to all tool names present in ``results``.
    """

    def __init__(
        self,
        results: Optional[dict[str, list[ToolExecutionResult]]] = None,
        exceptions: Optional[dict[str, list[Exception]]] = None,
        supported_tools: Optional[set[str]] = None,
        forbidden_tools: Optional[set[str]] = None,
        deadline_clock: Optional[Callable[[], float]] = None,
        minimum_remaining_seconds: Optional[float] = None,
        expected_cancellation_id: str | None = None,
    ) -> None:
        self._results: dict[str, list[ToolExecutionResult]] = {}
        for name, result_items in (results or {}).items():
            self._results[name] = list(result_items)
        self._exceptions: dict[str, list[Exception]] = {}
        for name, exc_items in (exceptions or {}).items():
            self._exceptions[name] = list(exc_items)
        self._supported_tools: set[str] = (
            set(supported_tools)
            if supported_tools is not None
            else set((results or {}).keys())
        )
        self._forbidden_tools: set[str] = set(forbidden_tools or set())
        self._deadline_clock = deadline_clock
        self._minimum_remaining_seconds = minimum_remaining_seconds
        self._expected_cancellation_id = expected_cancellation_id
        self.calls: list[ValidatedToolCall] = []
        self.contexts: list[ToolExecutionContext] = []

    @property
    def call_count(self) -> int:
        """Number of ``execute()`` invocations recorded."""
        return len(self.calls)

    def assert_not_called(self) -> None:
        """Assert that no tool execution was attempted."""
        if self.calls:
            raise AssertionError(f"Expected no tool calls, recorded {len(self.calls)}")

    def assert_tool_not_called(self, name: str) -> None:
        """Assert that a specific tool name was not invoked."""
        if any(call.name == name for call in self.calls):
            raise AssertionError(f"Expected tool {name!r} not to be called")

    async def execute(
        self, call: ValidatedToolCall, context: ToolExecutionContext
    ) -> ToolExecutionResult:
        """Execute a validated tool call.

        Returns the next scripted result for the tool name, or raises
        the next scripted exception. Raises ``IndexError`` when no
        scripted items remain for that tool.

        Parameters
        ----------
        call : ValidatedToolCall
            The validated tool call to execute.
        context : ToolExecutionContext
            The execution context.

        Returns
        -------
        ToolExecutionResult
            The next scripted tool result for this tool name.

        Raises
        ------
        IndexError
            If no scripted responses or exceptions remain for the tool.
        Exception
            If the next scripted item for this tool is an exception.
        """
        name = call.name
        if name in self._forbidden_tools:
            raise AssertionError(f"Forbidden tool {name!r} was called")
        if self._expected_cancellation_id is not None:
            actual = context.cancellation.cancellation_id
            if actual != self._expected_cancellation_id:
                raise AssertionError(
                    f"Expected cancellation ID {self._expected_cancellation_id!r}, "
                    f"got {actual!r}"
                )
        if self._deadline_clock is not None:
            remaining = context.deadline.remaining(self._deadline_clock)
            if (
                self._minimum_remaining_seconds is not None
                and remaining < self._minimum_remaining_seconds
            ):
                raise AssertionError(
                    f"Deadline remaining {remaining} is below required "
                    f"{self._minimum_remaining_seconds}"
                )

        self.calls.append(call)
        self.contexts.append(context)

        # Check per-tool exceptions first
        if name in self._exceptions and self._exceptions[name]:
            raise self._exceptions[name].pop(0)

        # Check per-tool results
        if name in self._results and self._results[name]:
            return self._results[name].pop(0)

        raise IndexError(
            f"No scripted results remain for tool {name!r} in "
            f"{type(self).__name__}. Add results via the constructor."
        )

    def supports_tool(self, name: str) -> bool:
        """Check whether a tool is supported.

        Returns True if *name* is in ``supported_tools`` (set at
        construction time) or if results were scripted for *name*.
        """
        return name in self._supported_tools


__all__: list[str] = [
    "FakeModelClient",
    "FakeGuardrailBackend",
    "FakeToolExecutor",
]
