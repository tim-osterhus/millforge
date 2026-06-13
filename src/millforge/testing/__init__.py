"""Test doubles (fakes) for Millforge runtime protocols.

All fakes are deterministic, make no network calls, and record every
request they receive. They support scripting success/failure scenarios
so tests can verify both happy-path and error-handling behaviour.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from millforge.artifacts import RuntimeArtifactWriter
from millforge.compiled_plan import (
    CompiledHarnessNode,
    CompiledHarnessPlan,
    IdempotencyClass,
    SideEffectCertainty,
    SideEffectClass,
    ToolExecutionStatus,
    canonical_json_serialize,
)
from millforge.contracts import (
    ArtifactRef,
    GuardedSessionRequest,
    GuardedSessionResult,
    ModelCompletionRequest,
    ModelCompletionResponse,
    TimingMetadata,
    ToolExecutionContext,
    ToolExecutionResult,
    ValidatedToolCall,
)


BUILDER_WORKSPACE_PATH = "src/example.py"
BUILDER_WORKSPACE_INITIAL = "def add(a, b): return a - b\n"
BUILDER_WORKSPACE_FIXED = "def add(a, b): return a + b\n"


@dataclass(frozen=True)
class BuilderModelCallRecord:
    """Typed public record for deterministic Builder model calls."""

    sequence: int
    request: ModelCompletionRequest


@dataclass(frozen=True)
class BuilderToolCallRecord:
    """Typed public record for accepted deterministic Builder tool calls."""

    sequence: int
    call: ValidatedToolCall
    result: ToolExecutionResult


@dataclass(frozen=True)
class BuilderRejectedToolCallRecord:
    """Typed public record for rejected deterministic Builder tool calls."""

    sequence: int
    tool_name: str
    arguments: Any
    error_code: str
    summary: str
    side_effect_certainty: SideEffectCertainty


@dataclass(frozen=True)
class BuilderWorkspaceMutationRecord:
    """Typed public record for in-memory Builder workspace mutations."""

    sequence: int
    path: str
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True)
class BuilderArtifactRecord:
    """Typed public record for deterministic Builder artifact writes."""

    sequence: int
    artifact_ref: ArtifactRef
    sha256: str


class FixedClock:
    """Deterministic runtime clock for tests."""

    def __init__(
        self,
        *,
        utc: datetime | None = None,
        monotonic_value: float = 0.0,
    ) -> None:
        self._utc = utc or datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        self._monotonic = monotonic_value

    def utc_now(self) -> datetime:
        """Return a fixed timezone-aware UTC timestamp."""
        return self._utc

    def monotonic(self) -> float:
        """Return a fixed monotonic timestamp."""
        return self._monotonic


class DeterministicIdSource:
    """Simple deterministic ID source for tests."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._next = 0

    def next(self) -> str:
        """Return the next stable ID."""
        value = f"{self._prefix}-{self._next:03d}"
        self._next += 1
        return value


class DeterministicDurationSource:
    """Deterministic duration source for tool results."""

    def __init__(self, duration_ms: float = 7.0) -> None:
        self._duration_ms = duration_ms

    def next_ms(self) -> float:
        """Return the fixed duration in milliseconds."""
        return self._duration_ms


class BuilderInMemoryWorkspace:
    """In-memory Builder workspace admitted by the canonical 02D fixture."""

    def __init__(self) -> None:
        self._files: dict[str, str] = {
            BUILDER_WORKSPACE_PATH: BUILDER_WORKSPACE_INITIAL
        }
        self.mutations: list[BuilderWorkspaceMutationRecord] = []

    @property
    def allowed_paths(self) -> tuple[str, ...]:
        """Return admitted workspace paths."""
        return tuple(self._files)

    def list_files(self) -> tuple[str, ...]:
        """List admitted in-memory workspace files."""
        return self.allowed_paths

    def read_file(self, path: str) -> str:
        """Read an admitted in-memory workspace file."""
        self._ensure_path(path)
        return self._files[path]

    def replace_file(self, path: str, content: str) -> None:
        """Replace an admitted in-memory workspace file."""
        self._ensure_path(path)
        before = self._files[path]
        self._files[path] = content
        self.mutations.append(
            BuilderWorkspaceMutationRecord(
                sequence=len(self.mutations) + 1,
                path=path,
                before_sha256=_sha256_text(before),
                after_sha256=_sha256_text(content),
            )
        )

    def diff(self) -> str:
        """Return a deterministic tiny diff for the canonical file."""
        current = self._files[BUILDER_WORKSPACE_PATH]
        if current == BUILDER_WORKSPACE_INITIAL:
            return ""
        return (
            f"--- a/{BUILDER_WORKSPACE_PATH}\n"
            f"+++ b/{BUILDER_WORKSPACE_PATH}\n"
            "@@\n"
            f"-{BUILDER_WORKSPACE_INITIAL}"
            f"+{current}"
        )

    def _ensure_path(self, path: str) -> None:
        if path not in self._files:
            raise KeyError(f"Path {path!r} is outside the Builder workspace")


class BuilderArtifactStore:
    """Deterministic artifact writer backed by ``RuntimeArtifactWriter`` paths."""

    def __init__(self, run_directory: Path) -> None:
        self._writer = RuntimeArtifactWriter(run_directory)
        self.records: list[BuilderArtifactRecord] = []

    def write_json(self, artifact_id: str, data: Any) -> ArtifactRef:
        """Write canonical JSON under ``run_directory/millforge``."""
        ref = ArtifactRef(
            artifact_id=artifact_id,
            path=Path("millforge") / artifact_id,
            content_type="application/json",
        )
        content = self._writer._serialize_json(data)
        target = self._writer._resolve_target(ref.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        meta = self._writer._atomic_write(target, content)
        self.records.append(
            BuilderArtifactRecord(
                sequence=len(self.records) + 1,
                artifact_ref=ref,
                sha256=meta["sha256_hex"],
            )
        )
        return ref

    def write_text(
        self,
        artifact_id: str,
        text: str,
        *,
        content_type: str = "text/plain",
    ) -> ArtifactRef:
        """Write UTF-8 text under ``run_directory/millforge``."""
        ref = ArtifactRef(
            artifact_id=artifact_id,
            path=Path("millforge") / artifact_id,
            content_type=content_type,
        )
        target = self._writer._resolve_target(ref.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        meta = self._writer._atomic_write(target, text.encode("utf-8"))
        self.records.append(
            BuilderArtifactRecord(
                sequence=len(self.records) + 1,
                artifact_ref=ref,
                sha256=meta["sha256_hex"],
            )
        )
        return ref


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


class BuilderFakeModelClient(FakeModelClient):
    """Deterministic Builder model fake with typed call-order records."""

    def __init__(
        self,
        responses: Optional[list[ModelCompletionResponse]] = None,
        exceptions: Optional[list[Exception]] = None,
    ) -> None:
        super().__init__(responses=responses, exceptions=exceptions)
        self.call_records: list[BuilderModelCallRecord] = []

    async def complete(
        self, request: ModelCompletionRequest
    ) -> ModelCompletionResponse:
        self.call_records.append(
            BuilderModelCallRecord(sequence=len(self.call_records) + 1, request=request)
        )
        return await super().complete(request)


class BuilderFakeToolExecutor:
    """Deterministic in-memory ToolExecutor for the canonical Builder fixture."""

    def __init__(
        self,
        *,
        plan: CompiledHarnessPlan,
        workspace: BuilderInMemoryWorkspace | None = None,
        artifact_store: BuilderArtifactStore | None = None,
        duration_source: DeterministicDurationSource | None = None,
    ) -> None:
        self._nodes_by_name = {node.model_tool_name: node for node in plan.nodes}
        self._nodes_by_binding_id = {node.binding.tool_id: node for node in plan.nodes}
        self._workspace = workspace or BuilderInMemoryWorkspace()
        self._artifact_store = artifact_store
        self._duration_source = duration_source or DeterministicDurationSource()
        self.calls: list[ValidatedToolCall] = []
        self.contexts: list[ToolExecutionContext] = []
        self.call_records: list[BuilderToolCallRecord] = []
        self.rejected_calls: list[BuilderRejectedToolCallRecord] = []
        self._accepted_arguments_by_node: dict[str, list[dict[str, Any]]] = {}

    @property
    def workspace(self) -> BuilderInMemoryWorkspace:
        """Return the isolated in-memory workspace."""
        return self._workspace

    @property
    def artifact_records(self) -> tuple[BuilderArtifactRecord, ...]:
        """Return artifact records from the backing store, if configured."""
        if self._artifact_store is None:
            return ()
        return tuple(self._artifact_store.records)

    @property
    def call_count(self) -> int:
        """Number of accepted tool calls dispatched to the fake executor."""
        return len(self.calls)

    def assert_not_called(self) -> None:
        """Assert that no accepted tool calls were dispatched."""
        if self.calls:
            raise AssertionError(f"Expected no tool calls, recorded {len(self.calls)}")

    def assert_tool_not_called(self, name: str) -> None:
        """Assert that a specific tool name was not accepted."""
        if any(call.name == name for call in self.calls):
            raise AssertionError(f"Expected tool {name!r} not to be called")

    def supports_tool(self, name: str) -> bool:
        """Return whether *name* is one of the compiled Builder tool names."""
        return name in self._nodes_by_name

    async def invoke_raw(
        self,
        tool_name: str,
        arguments: Any,
        context: ToolExecutionContext,
        *,
        call_id: str | None = None,
    ) -> ToolExecutionResult:
        """Validate a raw model tool call and dispatch only if it is accepted."""
        node = self._nodes_by_name.get(tool_name)
        if node is None:
            return self._reject(tool_name, arguments, "uncompiled_tool")
        if not isinstance(arguments, dict):
            return self._reject(tool_name, arguments, "malformed_arguments")
        defect = _schema_defect(node.input_schema, arguments)
        if defect is not None:
            return self._reject(tool_name, arguments, defect)
        constraint_defect = self._request_constraint_defect(node, arguments, context)
        if constraint_defect is not None:
            return self._reject(tool_name, arguments, constraint_defect)
        return await self.execute(
            ValidatedToolCall(
                call_id=call_id or f"builder-call-{len(self.calls) + 1:03d}",
                node_id=node.node_id,
                binding=node.binding,
                arguments=dict(arguments),
            ),
            context,
        )

    async def execute(
        self, call: ValidatedToolCall, context: ToolExecutionContext
    ) -> ToolExecutionResult:
        """Execute a validated Builder tool call against deterministic state."""
        node = self._nodes_by_binding_id.get(call.name) or self._nodes_by_name.get(
            call.name
        )
        if node is None:
            return self._reject(call.name, call.arguments, "uncompiled_tool")
        defect = _schema_defect(node.input_schema, call.arguments)
        if defect is not None:
            return self._reject(call.name, call.arguments, defect)
        constraint_defect = self._request_constraint_defect(
            node, call.arguments, context
        )
        if constraint_defect is not None:
            return self._reject(call.name, call.arguments, constraint_defect)
        state_defect = self._state_defect(node, call.arguments)
        if state_defect is not None:
            return self._reject(call.name, call.arguments, state_defect)

        structured_data, artifact_refs = self._execute_node(node, call.arguments)
        self._record_accepted_arguments(node, call.arguments)
        result = _builder_tool_result(
            call=call,
            node=node,
            structured_data=structured_data,
            artifact_refs=artifact_refs,
            duration_ms=self._duration_source.next_ms(),
        )
        self.calls.append(call)
        self.contexts.append(context)
        self.call_records.append(
            BuilderToolCallRecord(
                sequence=len(self.call_records) + 1,
                call=call,
                result=result,
            )
        )
        return result

    def _execute_node(
        self, node: CompiledHarnessNode, arguments: Mapping[str, Any]
    ) -> tuple[Any, tuple[ArtifactRef, ...]]:
        artifact_refs: tuple[ArtifactRef, ...] = ()
        if node.model_tool_name == "inspect_request":
            return {"ok": True}, artifact_refs
        if node.model_tool_name == "read_plan":
            return {"ok": True}, artifact_refs
        if node.model_tool_name == "list_files":
            return {"files": list(self._workspace.list_files())}, artifact_refs
        if node.model_tool_name == "read_file":
            path = str(arguments["path"])
            return {"path": path, "content": self._workspace.read_file(path)}, ()
        if node.model_tool_name == "apply_patch":
            path = str(arguments["path"])
            self._workspace.replace_file(path, str(arguments["replacement_text"]))
            return {"mutated": True}, ()
        if node.model_tool_name == "read_diff":
            diff = self._workspace.diff()
            if self._artifact_store is not None:
                artifact_refs = (
                    self._artifact_store.write_text("workspace_diff", diff),
                )
            return {"diff": diff}, artifact_refs
        if node.model_tool_name == "run_validator":
            return {
                "validator": arguments["validator"],
                "passed": self._workspace.read_file(BUILDER_WORKSPACE_PATH)
                == BUILDER_WORKSPACE_FIXED,
            }, ()
        if node.model_tool_name == "write_patch_summary":
            payload = {
                "summary": arguments["summary"],
                "changed_files": arguments["changed_files"],
            }
            if self._artifact_store is not None:
                artifact_refs = (
                    self._artifact_store.write_json("patch_summary.json", payload),
                )
            return payload, artifact_refs
        if node.model_tool_name == "write_validation_results":
            payload = {
                "validator": arguments["validator"],
                "passed": arguments["passed"],
                "summary": arguments["summary"],
            }
            if self._artifact_store is not None:
                artifact_refs = (
                    self._artifact_store.write_json("validation_results.json", payload),
                )
            return payload, artifact_refs
        if node.model_tool_name == "submit_patch":
            return {
                "terminal_result": "BUILDER_COMPLETE",
                "summary_artifact_ids": arguments["summary_artifact_ids"],
            }, ()
        if node.model_tool_name == "block_builder":
            payload = {
                "reason": arguments["reason"],
                "blocker_artifact_id": arguments["blocker_artifact_id"],
            }
            if self._artifact_store is not None:
                artifact_refs = (
                    self._artifact_store.write_json("blocker_report.json", payload),
                )
            return payload, artifact_refs
        raise AssertionError(f"Unhandled Builder tool {node.model_tool_name!r}")

    def _request_constraint_defect(
        self,
        node: CompiledHarnessNode,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> str | None:
        grants = {
            grant.capability_id: grant.constraints or {}
            for grant in context.capability_envelope.grants
        }
        for capability in node.required_capabilities:
            if capability not in grants:
                return "capability_denied"
        path = arguments.get("path")
        if isinstance(path, str):
            for capability in ("workspace.read", "workspace.write"):
                if capability in node.required_capabilities and path not in set(
                    grants.get(capability, {}).get("allowed_paths", [])
                ):
                    return "path_denied"
        if node.model_tool_name == "run_validator":
            allowed = set(grants["shell.run"].get("allowed_validators", []))
            if arguments.get("validator") not in allowed:
                return "validator_denied"
        if node.terminal_result is not None:
            allowed = set(
                grants.get("evidence.emit", {}).get("allowed_terminal_results", [])
            )
            if node.terminal_result not in allowed:
                return "terminal_result_denied"
        for artifact_id in node.produced_artifact_ids:
            allowed = set(
                grants.get("artifact.write", {}).get("allowed_artifact_ids", [])
            )
            if artifact_id not in allowed:
                return "artifact_denied"
        return None

    def _state_defect(
        self,
        node: CompiledHarnessNode,
        arguments: Mapping[str, Any],
    ) -> str | None:
        if node.model_tool_name != "apply_patch":
            return None
        path = str(arguments["path"])
        prior_reads = self._accepted_arguments_by_node.get("read_file", [])
        if not any(read.get("path") == path for read in prior_reads):
            return "read_before_write_required"
        expected_text = str(arguments["expected_text"])
        if self._workspace.read_file(path) != expected_text:
            return "expected_text_mismatch"
        return None

    def _reject(
        self,
        tool_name: str,
        arguments: Any,
        error_code: str,
    ) -> ToolExecutionResult:
        record = BuilderRejectedToolCallRecord(
            sequence=len(self.rejected_calls) + 1,
            tool_name=tool_name,
            arguments=arguments,
            error_code=error_code,
            summary="tool rejected before execution",
            side_effect_certainty=SideEffectCertainty.NOT_ATTEMPTED,
        )
        self.rejected_calls.append(record)
        return ToolExecutionResult(
            call_id=f"rejected-{record.sequence:03d}",
            status=ToolExecutionStatus.NOT_EXECUTED,
            summary=record.summary,
            error_code=error_code,
            retryable=False,
            side_effect_class=SideEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT,
            side_effect_certainty=SideEffectCertainty.NOT_ATTEMPTED,
            input_sha256=_sha256_json(arguments),
            output_sha256=None,
            timing=TimingMetadata(
                started_at="2026-06-12T12:00:00+00:00",
                completed_at="2026-06-12T12:00:00+00:00",
                duration_ms=self._duration_source.next_ms(),
            ),
        )

    def _record_accepted_arguments(
        self,
        node: CompiledHarnessNode,
        arguments: Mapping[str, Any],
    ) -> None:
        accepted = self._accepted_arguments_by_node.setdefault(node.node_id, [])
        accepted.append(dict(arguments))


def _builder_tool_result(
    *,
    call: ValidatedToolCall,
    node: CompiledHarnessNode,
    structured_data: Any,
    artifact_refs: tuple[ArtifactRef, ...],
    duration_ms: float,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call.call_id,
        status=ToolExecutionStatus.SUCCESS,
        summary=f"{node.model_tool_name} ok",
        structured_data=structured_data,
        artifact_refs=artifact_refs,
        side_effect_class=node.side_effect_class,
        idempotency=node.idempotency,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        input_sha256=_sha256_json(call.arguments),
        output_sha256=None,
        timing=TimingMetadata(
            started_at="2026-06-12T12:00:00+00:00",
            completed_at="2026-06-12T12:00:00+00:00",
            duration_ms=duration_ms,
        ),
    )


def _schema_defect(
    schema: Mapping[str, Any], arguments: Mapping[str, Any]
) -> str | None:
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not isinstance(properties, dict):
        return "invalid_schema"
    missing = required - set(arguments)
    if missing:
        return "missing_required_field"
    extra = set(arguments) - set(properties)
    if extra:
        return "extra_field"
    for key, value in arguments.items():
        spec = properties[key]
        if not isinstance(spec, dict):
            return "invalid_schema"
        if "const" in spec and value != spec["const"]:
            return "invalid_enum_value"
        if "enum" in spec and value not in spec["enum"]:
            return "invalid_enum_value"
        expected_type = spec.get("type")
        if expected_type == "string" and not isinstance(value, str):
            return "incorrect_scalar_type"
        if expected_type == "boolean" and not isinstance(value, bool):
            return "incorrect_scalar_type"
        if expected_type == "array":
            if not isinstance(value, list):
                return "incorrect_scalar_type"
            item_spec = spec.get("items")
            if (
                isinstance(item_spec, dict)
                and item_spec.get("type") == "string"
                and not all(isinstance(item, str) for item in value)
            ):
                return "incorrect_scalar_type"
    return None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_serialize(value).encode("utf-8")).hexdigest()


__all__: list[str] = [
    "BUILDER_WORKSPACE_FIXED",
    "BUILDER_WORKSPACE_INITIAL",
    "BUILDER_WORKSPACE_PATH",
    "BuilderArtifactRecord",
    "BuilderArtifactStore",
    "BuilderFakeModelClient",
    "BuilderFakeToolExecutor",
    "BuilderInMemoryWorkspace",
    "BuilderModelCallRecord",
    "BuilderRejectedToolCallRecord",
    "BuilderToolCallRecord",
    "BuilderWorkspaceMutationRecord",
    "DeterministicDurationSource",
    "DeterministicIdSource",
    "FakeModelClient",
    "FakeGuardrailBackend",
    "FakeToolExecutor",
    "FixedClock",
]
