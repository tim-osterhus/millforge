"""Test doubles (fakes) for Millforge runtime protocols.

All fakes are deterministic, make no network calls, and record every
request they receive. They support scripting success/failure scenarios
so tests can verify both happy-path and error-handling behaviour.
"""

from __future__ import annotations

from typing import Optional

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
    ) -> None:
        self._responses: list[GuardedSessionResult] = list(responses or [])
        self._exceptions: list[Exception] = list(exceptions or [])
        self._request_log: list[GuardedSessionRequest] = []

    @property
    def requests(self) -> list[GuardedSessionRequest]:
        """Recorded guardrail session calls."""
        return self._request_log

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

    Supports scripting per-tool-name results and records every
    ``ValidatedToolCall`` passed to ``execute()`` in ``calls``.

    Parameters
    ----------
    results : dict[str, list[ToolExecutionResult]], optional
        Mapping of tool names to lists of scripted results. Results
        are consumed in order per tool name.
    exceptions : dict[str, list[Exception]], optional
        Mapping of tool names to lists of scripted exceptions.
        Exceptions are consumed in order per tool name.
    supported_tools : set[str], optional
        Set of tool names that ``supports_tool`` returns True for.
        Defaults to all tool names present in ``results``.
    """

    def __init__(
        self,
        results: Optional[dict[str, list[ToolExecutionResult]]] = None,
        exceptions: Optional[dict[str, list[Exception]]] = None,
        supported_tools: Optional[set[str]] = None,
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
        self.calls: list[ValidatedToolCall] = []

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
        self.calls.append(call)
        name = call.name

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
