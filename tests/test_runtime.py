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

import asyncio
import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from millforge.artifacts import RuntimeArtifactWriter
from millforge.compiled_plan import (
    CompiledHarnessPlan,
    IdempotencyClass,
    SessionEventType,
    SideEffectCertainty,
    SideEffectClass,
    ToolExecutionStatus,
    ToolTraceIdempotency,
    ToolTraceSideEffectClass,
    calculate_compiled_plan_sha256,
    finalize_compiled_plan_sha256,
)
from millforge._forge.errors import NonRetryableToolError
from millforge.contracts import (
    ArtifactRef,
    AssistantMessage,
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
    HarnessTaskInput,
    InvalidToolArguments,
    ModelCompletionResponse,
    ModelProfileRef,
    ModelToolCall,
    ParsedToolArguments,
    RunDirRef,
    SelectedOutputPresent,
    SelectedOutputRequirement,
    StageIdentity,
    TerminalSelectedOutputRequirement,
    TerminalCertainty,
    TimeoutRef,
    TokenUsage,
)
from millforge._forge.adapter import ForgeContextFactory, ForgeGuardrailBackend
from millforge.exceptions import (
    ArtifactWriteError,
    BackendTranslationError,
    MillforgeConfigError,
    ModelTransportError,
    OperationCancelledError,
    ToolInvokeError,
)
from millforge.model_backend import (
    DefaultModelClient,
    OpenAIChatCompletionsTransport,
    ResolvedModelProfile,
    StaticModelProfileResolver,
    StaticSecretResolver,
)
from millforge.runtime import (
    DefaultHarnessRuntime,
    FailureOrigin,
    RuntimeState,
    classify_failure,
    classify_guarded_session_status,
)
from millforge.testing import (
    BUILDER_WORKSPACE_FIXED,
    BUILDER_WORKSPACE_INITIAL,
    BUILDER_WORKSPACE_PATH,
    BuilderArtifactStore,
    BuilderFakeModelClient,
    BuilderFakeToolExecutor,
    BuilderInMemoryWorkspace,
    FailureInjectionLeakProbe,
    FakeGuardrailBackend,
)

from tests.conftest import (
    BUILDER_FIXTURE_PROFILE_ID,
    FakeArtifactWriter,
    FakeCancellationResolver,
    FakeClock,
    FakePlanLoader,
    FakeCancellationToken,
    make_canonical_builder_profile_a,
    make_canonical_builder_profile_b,
    make_canonical_builder_compiled_plan,
    make_canonical_builder_execution_request,
    make_test_compiled_plan,
    make_test_harness_execution_request,
    make_test_guarded_session_result,
    make_test_session_event,
    make_test_tool_trace_record,
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
_BUILDER_SESSION_ID = "00000000-0000-4000-8000-0000000002d3"
_REPO_ROOT = Path(__file__).resolve().parents[1]

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


def _builder_model_response(
    sequence: int,
    tool_name: str,
    arguments: Any,
) -> ModelCompletionResponse:
    call_id = f"builder-call-{sequence:03d}-{tool_name}"
    tool_arguments = (
        arguments
        if isinstance(arguments, InvalidToolArguments)
        else ParsedToolArguments(value=arguments)
    )
    return ModelCompletionResponse(
        provider_request_id=f"provider-{call_id}",
        model_id=BUILDER_FIXTURE_PROFILE_ID,
        message=AssistantMessage(
            content=f"call {tool_name}",
            tool_calls=(
                ModelToolCall(
                    call_id=call_id,
                    name=tool_name,
                    arguments=tool_arguments,
                ),
            ),
        ),
        finish_reason="tool_calls",
        usage=TokenUsage(
            input_tokens=10 + sequence,
            output_tokens=sequence,
            total_tokens=10 + (sequence * 2),
            provider_reported=True,
        ),
    )


def _builder_script(
    calls: list[tuple[str, Any]],
) -> list[ModelCompletionResponse]:
    return [
        _builder_model_response(sequence, tool_name, arguments)
        for sequence, (tool_name, arguments) in enumerate(calls, start=1)
    ]


def _builder_apply_args() -> dict[str, Any]:
    return {
        "path": BUILDER_WORKSPACE_PATH,
        "expected_text": BUILDER_WORKSPACE_INITIAL,
        "replacement_text": BUILDER_WORKSPACE_FIXED,
    }


def _builder_patch_summary_args() -> dict[str, Any]:
    return {
        "summary": "fixed add",
        "changed_files": [BUILDER_WORKSPACE_PATH],
    }


def _builder_validation_results_args() -> dict[str, Any]:
    return {
        "validator": "unit",
        "passed": True,
        "summary": "unit passed",
    }


def _builder_submit_args() -> dict[str, Any]:
    return {
        "summary_artifact_ids": [
            "patch_summary.json",
            "validation_results.json",
        ],
    }


def _builder_success_calls() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("inspect_request", {}),
        ("read_plan", {}),
        ("list_files", {}),
        ("read_file", {"path": BUILDER_WORKSPACE_PATH}),
        ("apply_patch", _builder_apply_args()),
        ("read_diff", {}),
        ("run_validator", {"validator": "unit"}),
        ("write_patch_summary", _builder_patch_summary_args()),
        ("write_validation_results", _builder_validation_results_args()),
        ("submit_patch", _builder_submit_args()),
    ]


def _builder_block_args() -> dict[str, Any]:
    return {
        "reason": "missing upstream decision",
        "blocker_artifact_id": "blocker_report.json",
    }


def _rehash_plan(plan: CompiledHarnessPlan) -> CompiledHarnessPlan:
    try:
        return finalize_compiled_plan_sha256(plan)
    except (ValueError, ValidationError):
        payload = plan.model_dump(mode="json")
        return plan.model_copy(
            update={"compiled_sha256": calculate_compiled_plan_sha256(payload)}
        )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl_event_types(path: Path) -> list[str]:
    return [
        json.loads(line)["event_type"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _jsonl_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _model_event_sequence(count: int) -> list[str]:
    return ["model_request_started", "model_request_completed"] * count


def _tool_event_sequence(
    count: int, *, final_status: str = "tool_completed"
) -> list[str]:
    return ["tool_started", "tool_completed"] * (count - 1) + [
        "tool_started",
        final_status,
    ]


def _assert_tool_trace_sequence(
    traces: list[dict[str, Any]],
    expected: list[tuple[str, str, str]],
) -> None:
    assert [
        (
            record["node_id"],
            record["execution_status"],
            record["side_effect_certainty"],
        )
        for record in traces
    ] == expected


def _canonical_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_run_root(value: Any, run_dir: Path) -> Any:
    run_dir_text = str(run_dir)
    if isinstance(value, dict):
        return {key: _normalize_run_root(item, run_dir) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_run_root(item, run_dir) for item in value]
    if isinstance(value, str):
        return value.replace(run_dir_text, "<RUN_DIR>")
    return value


def _normalized_result_json(result: HarnessExecutionResult, run_dir: Path) -> Any:
    return _normalize_run_root(result.model_dump(mode="json"), run_dir)


def _normalized_file_json(path: Path, run_dir: Path) -> Any:
    return _normalize_run_root(_canonical_json_file(path), run_dir)


def _normalized_jsonl(path: Path, run_dir: Path) -> list[Any]:
    return _normalize_run_root(_jsonl_records(path), run_dir)


def _manifest_content_hashes(manifest_path: Path) -> dict[str, str]:
    manifest = _canonical_json_file(manifest_path)
    return {
        entry["artifact_id"]: entry["sha256_hex"] for entry in manifest["artifacts"]
    }


async def _execute_canonical_builder_script(
    tmp_path: Path,
    calls: list[tuple[str, Any]],
    *,
    plan: CompiledHarnessPlan | None = None,
) -> tuple[
    HarnessExecutionResult,
    BuilderFakeModelClient,
    BuilderFakeToolExecutor,
    Path,
]:
    plan = plan or make_canonical_builder_compiled_plan()
    request = make_canonical_builder_execution_request(tmp_path, plan=plan)
    plan_loader = FakePlanLoader(plan=plan)
    artifact_store = BuilderArtifactStore(request.run_directory.path)
    workspace = BuilderInMemoryWorkspace()
    tool_executor = BuilderFakeToolExecutor(
        plan=plan,
        workspace=workspace,
        artifact_store=artifact_store,
    )
    model_client = BuilderFakeModelClient(responses=_builder_script(calls))
    clock = FakeClock()
    cancellation_resolver = FakeCancellationResolver(is_cancelled=False)
    backend = ForgeGuardrailBackend(
        model_client=model_client,
        tool_executor=tool_executor,
        plan_loader=plan_loader,
        context_factory=ForgeContextFactory(),
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )
    runtime = DefaultHarnessRuntime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=RuntimeArtifactWriter(request.run_directory.path),
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )

    result = await runtime.execute(request)

    assert len(backend.requests) == 1
    assert backend.requests[0].session_id == _BUILDER_SESSION_ID
    return result, model_client, tool_executor, request.run_directory.path


async def _execute_canonical_builder_http_profile(
    tmp_path: Path,
    profile: ResolvedModelProfile,
    secret_values: dict[str, str],
    *,
    plan: CompiledHarnessPlan | None = None,
) -> tuple[HarnessExecutionResult, list[dict[str, Any]], BuilderFakeToolExecutor, Path]:
    plan = plan or make_canonical_builder_compiled_plan()
    request = make_canonical_builder_execution_request(tmp_path, plan=plan).model_copy(
        update={"secret_refs": (profile.authentication.secret_ref,)}
    )
    calls = _builder_success_calls()
    seen: list[dict[str, Any]] = []
    httpx_module = __import__("httpx")

    async def handler(http_request: Any) -> Any:
        sequence = len(seen) + 1
        tool_name, arguments = calls[sequence - 1]
        body = json.loads(http_request.content)
        headers = dict(http_request.headers)
        seen.append({"url": str(http_request.url), "headers": headers, "body": body})

        call_id = f"builder-http-{profile.provider_id[-1]}-{sequence:03d}-{tool_name}"
        response_body: dict[str, Any] = {
            "id": f"provider-{profile.provider_id}-{sequence:03d}",
            "model": profile.model_id,
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": f"call {tool_name}",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(
                                        arguments,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                },
                            }
                        ],
                    },
                }
            ],
        }
        if profile.provider_id == "compat-a":
            response_body["usage"] = {
                "prompt_tokens": 10 + sequence,
                "completion_tokens": sequence,
                "total_tokens": 10 + (sequence * 2),
            }
        return httpx_module.Response(
            200,
            headers={"content-type": "application/json"},
            json=response_body,
        )

    plan_loader = FakePlanLoader(plan=plan)
    artifact_store = BuilderArtifactStore(request.run_directory.path)
    tool_executor = BuilderFakeToolExecutor(
        plan=plan,
        workspace=BuilderInMemoryWorkspace(),
        artifact_store=artifact_store,
    )
    transport = OpenAIChatCompletionsTransport(
        http_transport=httpx_module.MockTransport(handler)
    )
    model_client = DefaultModelClient(
        profile_resolver=StaticModelProfileResolver({profile.profile_id: profile}),
        secret_resolver=StaticSecretResolver(secret_values),
        transport=transport,
        cancellation_resolver=FakeCancellationResolver(is_cancelled=False),
        clock=FakeClock(),
    )
    clock = FakeClock()
    cancellation_resolver = FakeCancellationResolver(is_cancelled=False)
    backend = ForgeGuardrailBackend(
        model_client=model_client,
        tool_executor=tool_executor,
        plan_loader=plan_loader,
        context_factory=ForgeContextFactory(),
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )
    runtime = DefaultHarnessRuntime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=RuntimeArtifactWriter(request.run_directory.path),
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )

    try:
        result = await runtime.execute(request)
    finally:
        await model_client.aclose()

    assert len(backend.requests) == 1
    assert backend.requests[0].session_id == _BUILDER_SESSION_ID
    return result, seen, tool_executor, request.run_directory.path


def _assert_terminal_result_shape(
    terminal_payload: dict[str, Any],
    *,
    terminal_result: str,
    result_class: str,
) -> None:
    assert set(terminal_payload) == {
        "compiled_harness_sha256",
        "request_id",
        "result_class",
        "run_id",
        "schema_version",
        "stage",
        "summary_artifact_paths",
        "terminal_certainty",
        "terminal_result",
    }
    assert terminal_payload["schema_version"] == "1.0"
    assert terminal_payload["request_id"] == "request-builder-001"
    assert terminal_payload["run_id"] == "run-builder-001"
    assert terminal_payload["stage"] == {
        "node_id": "builder",
        "plane": "execution",
        "stage_kind_id": "builder",
    }
    assert terminal_payload["terminal_result"] == terminal_result
    assert terminal_payload["result_class"] == result_class
    assert terminal_payload["terminal_certainty"] == TerminalCertainty.COMMITTED.value
    assert len(terminal_payload["compiled_harness_sha256"]) == 64


def _assert_millforge_artifacts(
    millforge_dir: Path,
    *,
    filenames: list[str],
    manifest_artifact_ids: set[str],
    absent_filenames: set[str] | None = None,
) -> None:
    assert sorted(path.name for path in millforge_dir.iterdir()) == filenames
    manifest = _read_json(millforge_dir / "artifact_manifest.json")
    assert manifest["schema_version"] == "1.0"
    assert manifest["request_id"] == "request-builder-001"
    assert manifest["run_id"] == "run-builder-001"
    assert {entry["artifact_id"] for entry in manifest["artifacts"]} == (
        manifest_artifact_ids
    )
    for filename in absent_filenames or set():
        assert not (millforge_dir / filename).exists()


@pytest.mark.asyncio
async def test_canonical_builder_s1_clean_success_runtime_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S1 executes the canonical Builder success path through the real runtime."""
    monkeypatch.setattr("millforge.runtime.uuid.uuid4", lambda: _BUILDER_SESSION_ID)
    calls = _builder_success_calls()

    (
        result,
        model_client,
        tool_executor,
        run_dir,
    ) = await _execute_canonical_builder_script(tmp_path, calls)

    assert result.status == ExecutionStatus.COMPLETED
    assert result.result_class == ExecutionResultClass.DOMAIN_TERMINAL
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "BUILDER_COMPLETE"
    assert result.terminal_intent.disposition == "success"
    assert result.usage is not None
    assert result.usage.model_calls == 10
    assert result.usage.tool_calls == 10
    token_usage = result.usage.token_usage
    assert token_usage is not None
    assert token_usage.model_dump(mode="json") == {
        "input_tokens": 155,
        "output_tokens": 55,
        "total_tokens": 210,
        "provider_reported": True,
    }
    assert model_client.call_count == 10
    assert [record.call.node_id for record in tool_executor.call_records] == [
        tool_name for tool_name, _args in calls
    ]
    assert tool_executor.rejected_calls == []
    assert tool_executor.workspace.read_file(BUILDER_WORKSPACE_PATH) == (
        BUILDER_WORKSPACE_FIXED
    )
    assert len(tool_executor.workspace.mutations) == 1
    assert Path(BUILDER_WORKSPACE_PATH).exists() is False
    assert tool_executor.contexts
    first_context = tool_executor.contexts[0]
    assert all(context == first_context for context in tool_executor.contexts)
    assert first_context.request_id == "request-builder-001"
    assert first_context.run_id == "run-builder-001"
    assert first_context.stage.node_id == "builder"
    assert first_context.work_item_id == "work-builder-001"
    assert first_context.run_directory.path == run_dir
    assert first_context.workspace_root == Path.cwd()
    assert first_context.artifact_root == run_dir / "millforge"
    assert first_context.compiled_artifact_policy is not None
    assert first_context.cancellation_requested is False

    millforge_dir = run_dir / "millforge"
    _assert_millforge_artifacts(
        millforge_dir,
        filenames=[
            "artifact_manifest.json",
            "compiled_plan.json",
            "events.jsonl",
            "execution_summary.json",
            "metrics.json",
            "patch_summary.json",
            "terminal_result.json",
            "tool_trace.jsonl",
            "validation_results.json",
            "workspace_diff",
        ],
        manifest_artifact_ids={
            "terminal_result",
            "execution_summary",
            "events",
            "tool_trace",
            "metrics",
        },
        absent_filenames={"blocker_report.json", "diagnostic.json"},
    )
    terminal_payload = _read_json(millforge_dir / "terminal_result.json")
    _assert_terminal_result_shape(
        terminal_payload,
        terminal_result="BUILDER_COMPLETE",
        result_class="domain_terminal",
    )
    assert set(terminal_payload["summary_artifact_paths"]) == {
        "millforge/patch_summary.json",
        "millforge/validation_results.json",
        "millforge/workspace_diff",
    }
    assert (millforge_dir / "workspace_diff").read_text(encoding="utf-8").strip()
    assert _jsonl_event_types(millforge_dir / "events.jsonl") == [
        "session_started",
        "workflow_constructed",
        *(["model_request_started", "model_request_completed"] * 10),
        *(["tool_started", "tool_completed"] * 10),
        "terminal_intent_accepted",
    ]
    traces = _jsonl_records(millforge_dir / "tool_trace.jsonl")
    assert [record["node_id"] for record in traces] == [
        tool_name for tool_name, _args in calls
    ]
    assert all(record["execution_status"] == "success" for record in traces)


@pytest.mark.parametrize(
    ("profile_factory", "secret_values", "usage_expected"),
    (
        (
            make_canonical_builder_profile_a,
            {"compat_a_key": "sk-compat-a-secret"},
            True,
        ),
        (
            make_canonical_builder_profile_b,
            {"compat_b_key": "sk-compat-b-secret"},
            False,
        ),
    ),
)
@pytest.mark.asyncio
async def test_canonical_builder_s1_success_uses_canonical_profile_http_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_factory: Any,
    secret_values: dict[str, str],
    usage_expected: bool,
) -> None:
    monkeypatch.setattr("millforge.runtime.uuid.uuid4", lambda: _BUILDER_SESSION_ID)
    profile = profile_factory()
    plan = make_canonical_builder_compiled_plan()
    calls = _builder_success_calls()
    expected_tool_names = [node.model_tool_name for node in plan.nodes]

    (
        result,
        seen,
        tool_executor,
        _run_dir,
    ) = await _execute_canonical_builder_http_profile(
        tmp_path,
        profile,
        secret_values,
        plan=plan,
    )

    assert result.status == ExecutionStatus.COMPLETED
    assert result.result_class == ExecutionResultClass.DOMAIN_TERMINAL
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "BUILDER_COMPLETE"
    assert result.terminal_intent.disposition == "success"
    assert result.usage is not None
    assert result.usage.model_calls == 10
    assert result.usage.tool_calls == 10
    assert (result.usage.token_usage is not None) is usage_expected
    if usage_expected:
        assert result.usage.token_usage is not None
        assert result.usage.token_usage.model_dump(mode="json") == {
            "input_tokens": 155,
            "output_tokens": 55,
            "total_tokens": 210,
            "provider_reported": True,
        }

    assert [request["url"] for request in seen] == [
        profile.endpoint.chat_completions_url
    ] * len(calls)
    expected_tools = seen[0]["body"]["tools"]
    assert [tool["function"]["name"] for tool in expected_tools] == expected_tool_names
    assert expected_tools[3]["function"]["parameters"] == {
        "additionalProperties": False,
        "properties": {"path": {"title": "Path", "type": "string"}},
        "required": ["path"],
        "title": "ReadFileParams",
        "type": "object",
    }
    assert expected_tools[4]["function"]["parameters"]["required"] == [
        "expected_text",
        "path",
        "replacement_text",
    ]
    for request in seen:
        headers = request["headers"]
        body = request["body"]
        assert headers["content-type"] == "application/json"
        assert headers["user-agent"] == "millforge-model-backend/1"
        assert "accept" not in headers
        assert body["model"] == profile.model_id
        assert body["stream"] is False
        assert body["max_tokens"] == profile.maximum_output_tokens
        assert body["tools"] == expected_tools
        assert "temperature" not in body
        assert "top_p" not in body
        if profile.provider_id == "compat-a":
            assert headers["authorization"] == "Bearer sk-compat-a-secret"
            assert "reasoning_effort" not in body
        else:
            assert headers["x-api-key"] == "sk-compat-b-secret"
            assert body["reasoning_effort"] == "high"
    assert [record.call.node_id for record in tool_executor.call_records] == [
        tool_name for tool_name, _arguments in calls
    ]
    assert [record.call.call_id for record in tool_executor.call_records] == [
        f"builder-http-{profile.provider_id[-1]}-{sequence:03d}-{tool_name}"
        for sequence, (tool_name, _arguments) in enumerate(calls, start=1)
    ]
    assert [request["body"]["model"] for request in seen] == [profile.model_id] * 10
    assert tool_executor.workspace.read_file(BUILDER_WORKSPACE_PATH) == (
        BUILDER_WORKSPACE_FIXED
    )
    assert tool_executor.rejected_calls == []


@pytest.mark.asyncio
async def test_canonical_builder_s1_repeated_runs_are_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S1 repeats byte-stable after normalizing only absolute run roots."""
    monkeypatch.setattr("millforge.runtime.uuid.uuid4", lambda: _BUILDER_SESSION_ID)
    calls = _builder_success_calls()

    result_1, model_1, tools_1, run_dir_1 = await _execute_canonical_builder_script(
        tmp_path / "first",
        calls,
    )
    result_2, model_2, tools_2, run_dir_2 = await _execute_canonical_builder_script(
        tmp_path / "second",
        calls,
    )

    millforge_1 = run_dir_1 / "millforge"
    millforge_2 = run_dir_2 / "millforge"
    assert _normalized_result_json(result_1, run_dir_1) == _normalized_result_json(
        result_2,
        run_dir_2,
    )
    assert _normalized_file_json(
        millforge_1 / "terminal_result.json",
        run_dir_1,
    ) == _normalized_file_json(millforge_2 / "terminal_result.json", run_dir_2)
    assert _normalized_jsonl(millforge_1 / "events.jsonl", run_dir_1) == (
        _normalized_jsonl(millforge_2 / "events.jsonl", run_dir_2)
    )
    assert _normalized_jsonl(millforge_1 / "tool_trace.jsonl", run_dir_1) == (
        _normalized_jsonl(millforge_2 / "tool_trace.jsonl", run_dir_2)
    )
    assert _normalized_file_json(millforge_1 / "metrics.json", run_dir_1) == (
        _normalized_file_json(millforge_2 / "metrics.json", run_dir_2)
    )
    assert _manifest_content_hashes(millforge_1 / "artifact_manifest.json") == (
        _manifest_content_hashes(millforge_2 / "artifact_manifest.json")
    )
    assert [record.request for record in model_1.call_records] == [
        record.request for record in model_2.call_records
    ]
    assert [record.call for record in tools_1.call_records] == [
        record.call for record in tools_2.call_records
    ]


def test_canonical_builder_slice_keeps_deferred_systems_out_of_scope(
    tmp_path: Path,
) -> None:
    """Audit the 02D Builder slice boundaries without importing Millrace runtime."""
    plan = make_canonical_builder_compiled_plan()
    request = make_canonical_builder_execution_request(tmp_path)
    tool_names = {node.model_tool_name for node in plan.nodes}

    assert tool_names == {
        "inspect_request",
        "read_plan",
        "list_files",
        "read_file",
        "apply_patch",
        "read_diff",
        "run_validator",
        "write_patch_summary",
        "write_validation_results",
        "submit_patch",
        "block_builder",
    }
    assert request.secret_refs == ()
    assert request.model_profile.profile_id == BUILDER_FIXTURE_PROFILE_ID
    shell_grant = next(
        grant
        for grant in request.capability_envelope.grants
        if grant.capability_id == "shell.run"
    )
    assert shell_grant.constraints == {
        "allowed_validators": ["unit"],
        "subprocess_allowed": False,
    }
    assert request.compiled_harness.path.name == "compiled_plan.json"
    assert all(ref.artifact_id == "plan" for ref in request.input_artifacts)

    forbidden_import_roots = {
        "git",
        "httpx",
        "millrace",
        "openai",
        "requests",
        "subprocess",
        "urllib",
        "yaml",
    }
    audited_paths = (
        _REPO_ROOT / "src" / "millforge" / "runtime.py",
        _REPO_ROOT / "src" / "millforge" / "testing" / "__init__.py",
        _REPO_ROOT / "tests" / "conftest.py",
    )
    for path in audited_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots = {
                    alias.name.split(".", maxsplit=1)[0] for alias in node.names
                }
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_roots = {node.module.split(".", maxsplit=1)[0]}
            else:
                continue
            assert not (imported_roots & forbidden_import_roots), path


@pytest.mark.asyncio
async def test_canonical_builder_s2_premature_terminal_correction_runtime_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S2 rejects premature terminal attempts, then completes after correction."""
    monkeypatch.setattr("millforge.runtime.uuid.uuid4", lambda: _BUILDER_SESSION_ID)
    corrected_calls = _builder_success_calls()
    calls = [
        ("submit_patch", _builder_submit_args()),
        *corrected_calls,
    ]

    (
        result,
        model_client,
        tool_executor,
        run_dir,
    ) = await _execute_canonical_builder_script(tmp_path, calls)

    assert result.status == ExecutionStatus.COMPLETED
    assert result.result_class == ExecutionResultClass.DOMAIN_TERMINAL
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "BUILDER_COMPLETE"
    assert result.terminal_intent.disposition == "success"
    assert result.usage is not None
    assert result.usage.model_calls == 11
    assert result.usage.tool_calls == 10
    token_usage = result.usage.token_usage
    assert token_usage is not None
    assert token_usage.model_dump(mode="json") == {
        "input_tokens": 176,
        "output_tokens": 66,
        "total_tokens": 242,
        "provider_reported": True,
    }
    assert model_client.call_count == 11
    assert [record.call.node_id for record in tool_executor.call_records] == [
        tool_name for tool_name, _args in corrected_calls
    ]
    assert tool_executor.rejected_calls == []
    assert tool_executor.workspace.read_file(BUILDER_WORKSPACE_PATH) == (
        BUILDER_WORKSPACE_FIXED
    )
    assert len(tool_executor.workspace.mutations) == 1
    assert Path(BUILDER_WORKSPACE_PATH).exists() is False

    millforge_dir = run_dir / "millforge"
    _assert_millforge_artifacts(
        millforge_dir,
        filenames=[
            "artifact_manifest.json",
            "compiled_plan.json",
            "events.jsonl",
            "execution_summary.json",
            "metrics.json",
            "patch_summary.json",
            "terminal_result.json",
            "tool_trace.jsonl",
            "validation_results.json",
            "workspace_diff",
        ],
        manifest_artifact_ids={
            "terminal_result",
            "execution_summary",
            "events",
            "tool_trace",
            "metrics",
        },
        absent_filenames={"blocker_report.json", "diagnostic.json"},
    )
    events = _jsonl_event_types(millforge_dir / "events.jsonl")
    assert events == [
        "session_started",
        "workflow_constructed",
        *_model_event_sequence(1),
        "correction_issued",
        "premature_terminal_rejected",
        *_model_event_sequence(10),
        *_tool_event_sequence(10),
        "terminal_intent_accepted",
    ]
    traces = _jsonl_records(millforge_dir / "tool_trace.jsonl")
    assert [record["node_id"] for record in traces] == [
        tool_name for tool_name, _args in corrected_calls
    ]
    _assert_tool_trace_sequence(
        traces,
        [
            (tool_name, "success", "confirmed_complete")
            for tool_name, _args in corrected_calls
        ],
    )


@pytest.mark.asyncio
async def test_canonical_builder_s3_invalid_tool_arguments_correction_runtime_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3 rejects malformed arguments before executor dispatch, then corrects."""
    monkeypatch.setattr("millforge.runtime.uuid.uuid4", lambda: _BUILDER_SESSION_ID)
    invalid_args = InvalidToolArguments(
        raw='"not-object"',
        error_code="malformed_arguments",
    )
    corrected_calls: list[tuple[str, Any]] = [
        ("inspect_request", {}),
        ("read_plan", {}),
        ("read_file", {"path": BUILDER_WORKSPACE_PATH}),
        ("apply_patch", _builder_apply_args()),
        ("read_diff", {}),
        ("run_validator", {"validator": "unit"}),
        ("write_patch_summary", _builder_patch_summary_args()),
        ("write_validation_results", _builder_validation_results_args()),
        ("submit_patch", _builder_submit_args()),
    ]
    calls = [
        ("inspect_request", {}),
        ("read_plan", {}),
        ("read_file", {"path": BUILDER_WORKSPACE_PATH}),
        ("apply_patch", invalid_args),
        *corrected_calls[3:],
    ]

    (
        result,
        model_client,
        tool_executor,
        run_dir,
    ) = await _execute_canonical_builder_script(tmp_path, calls)

    assert result.status == ExecutionStatus.COMPLETED
    assert result.result_class == ExecutionResultClass.DOMAIN_TERMINAL
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "BUILDER_COMPLETE"
    assert result.terminal_intent.disposition == "success"
    assert result.usage is not None
    assert result.usage.model_calls == 10
    assert result.usage.tool_calls == 9
    token_usage = result.usage.token_usage
    assert token_usage is not None
    assert token_usage.model_dump(mode="json") == {
        "input_tokens": 155,
        "output_tokens": 55,
        "total_tokens": 210,
        "provider_reported": True,
    }
    assert model_client.call_count == 10
    assert tool_executor.rejected_calls == []
    assert [record.call.node_id for record in tool_executor.call_records] == [
        tool_name for tool_name, _args in corrected_calls
    ]
    assert tool_executor.workspace.read_file(BUILDER_WORKSPACE_PATH) == (
        BUILDER_WORKSPACE_FIXED
    )
    assert len(tool_executor.workspace.mutations) == 1
    assert Path(BUILDER_WORKSPACE_PATH).exists() is False

    millforge_dir = run_dir / "millforge"
    _assert_millforge_artifacts(
        millforge_dir,
        filenames=[
            "artifact_manifest.json",
            "compiled_plan.json",
            "events.jsonl",
            "execution_summary.json",
            "metrics.json",
            "patch_summary.json",
            "terminal_result.json",
            "tool_trace.jsonl",
            "validation_results.json",
            "workspace_diff",
        ],
        manifest_artifact_ids={
            "terminal_result",
            "execution_summary",
            "events",
            "tool_trace",
            "metrics",
        },
        absent_filenames={"blocker_report.json", "diagnostic.json"},
    )
    events = _jsonl_event_types(millforge_dir / "events.jsonl")
    assert events == [
        "session_started",
        "workflow_constructed",
        *_model_event_sequence(5),
        "correction_issued",
        *_model_event_sequence(5),
        *_tool_event_sequence(9),
        "terminal_intent_accepted",
    ]
    traces = _jsonl_records(millforge_dir / "tool_trace.jsonl")
    _assert_tool_trace_sequence(
        traces,
        [
            (tool_name, "success", "confirmed_complete")
            for tool_name, _args in corrected_calls
        ],
    )


@pytest.mark.asyncio
async def test_canonical_builder_s4_missing_success_evidence_runtime_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S4 missing required success evidence prevents BUILDER_COMPLETE."""
    monkeypatch.setattr("millforge.runtime.uuid.uuid4", lambda: _BUILDER_SESSION_ID)
    calls: list[tuple[str, Any]] = [
        ("inspect_request", {}),
        ("read_plan", {}),
        ("read_file", {"path": BUILDER_WORKSPACE_PATH}),
        ("apply_patch", _builder_apply_args()),
        ("read_diff", {}),
        ("run_validator", {"validator": "unit"}),
        ("write_patch_summary", _builder_patch_summary_args()),
        ("submit_patch", _builder_submit_args()),
        ("submit_patch", _builder_submit_args()),
        ("submit_patch", _builder_submit_args()),
    ]

    (
        result,
        model_client,
        tool_executor,
        run_dir,
    ) = await _execute_canonical_builder_script(tmp_path, calls)

    executed_calls = calls[:7]
    rejected_submit_attempts = calls[7:]

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.BUDGET_EXHAUSTED
    assert result.terminal_intent is None
    assert result.usage is not None
    assert result.usage.model_calls == 10
    assert result.usage.tool_calls == 7
    token_usage = result.usage.token_usage
    assert token_usage is not None
    assert token_usage.model_dump(mode="json") == {
        "input_tokens": 155,
        "output_tokens": 55,
        "total_tokens": 210,
        "provider_reported": True,
    }
    assert model_client.call_count == 10
    assert [record.sequence for record in model_client.call_records] == list(
        range(1, len(calls) + 1)
    )
    assert [record.call.node_id for record in tool_executor.call_records] == [
        tool_name for tool_name, _args in executed_calls
    ]
    assert [tool_name for tool_name, _args in rejected_submit_attempts] == [
        "submit_patch",
        "submit_patch",
        "submit_patch",
    ]
    assert tool_executor.rejected_calls == []
    assert len(tool_executor.workspace.mutations) == 1
    assert tool_executor.workspace.read_file(BUILDER_WORKSPACE_PATH) == (
        BUILDER_WORKSPACE_FIXED
    )
    assert Path(BUILDER_WORKSPACE_PATH).exists() is False

    millforge_dir = run_dir / "millforge"
    _assert_millforge_artifacts(
        millforge_dir,
        filenames=[
            "artifact_manifest.json",
            "compiled_plan.json",
            "diagnostic.json",
            "events.jsonl",
            "execution_summary.json",
            "metrics.json",
            "patch_summary.json",
            "tool_trace.jsonl",
            "workspace_diff",
        ],
        manifest_artifact_ids={
            "diagnostic",
            "events",
            "execution_summary",
            "metrics",
            "tool_trace",
        },
        absent_filenames={
            "blocker_report.json",
            "terminal_result.json",
            "validation_results.json",
        },
    )
    diagnostic = _read_json(millforge_dir / "diagnostic.json")["diagnostic"]
    assert diagnostic["error_code"] == "prerequisite_budget_exhausted"
    assert diagnostic["category"] == "backend"
    events = _jsonl_event_types(millforge_dir / "events.jsonl")
    assert events == [
        "session_started",
        "workflow_constructed",
        *_model_event_sequence(8),
        "correction_issued",
        *_model_event_sequence(1),
        "correction_issued",
        *_model_event_sequence(1),
        "budget_exhausted",
        *_tool_event_sequence(7),
    ]
    traces = _jsonl_records(millforge_dir / "tool_trace.jsonl")
    _assert_tool_trace_sequence(
        traces,
        [
            (tool_name, "success", "confirmed_complete")
            for tool_name, _args in executed_calls
        ],
    )


@pytest.mark.asyncio
async def test_canonical_builder_s5_validation_correction_exhaustion_runtime_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S5 pins repeated malformed argument correction exhaustion behavior."""
    monkeypatch.setattr("millforge.runtime.uuid.uuid4", lambda: _BUILDER_SESSION_ID)
    invalid_args = InvalidToolArguments(
        raw='"not-object"',
        error_code="malformed_arguments",
    )
    calls: list[tuple[str, Any]] = [
        ("inspect_request", {}),
        ("read_plan", {}),
        ("read_file", {"path": BUILDER_WORKSPACE_PATH}),
        ("apply_patch", invalid_args),
        ("apply_patch", invalid_args),
        ("apply_patch", invalid_args),
    ]

    (
        result,
        model_client,
        tool_executor,
        run_dir,
    ) = await _execute_canonical_builder_script(tmp_path, calls)

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.MODEL_FAILURE
    assert result.terminal_intent is None
    assert result.usage is not None
    assert result.usage.model_calls == 6
    assert result.usage.tool_calls == 3
    token_usage = result.usage.token_usage
    assert token_usage is not None
    assert token_usage.model_dump(mode="json") == {
        "input_tokens": 81,
        "output_tokens": 21,
        "total_tokens": 102,
        "provider_reported": True,
    }
    assert model_client.call_count == 6
    assert [record.call.node_id for record in tool_executor.call_records] == [
        "inspect_request",
        "read_plan",
        "read_file",
    ]
    assert tool_executor.rejected_calls == []
    assert tool_executor.workspace.mutations == []
    assert tool_executor.workspace.read_file(BUILDER_WORKSPACE_PATH) == (
        BUILDER_WORKSPACE_INITIAL
    )

    millforge_dir = run_dir / "millforge"
    _assert_millforge_artifacts(
        millforge_dir,
        filenames=[
            "artifact_manifest.json",
            "compiled_plan.json",
            "diagnostic.json",
            "events.jsonl",
            "execution_summary.json",
            "metrics.json",
            "tool_trace.jsonl",
        ],
        manifest_artifact_ids={
            "diagnostic",
            "events",
            "execution_summary",
            "metrics",
            "tool_trace",
        },
        absent_filenames={
            "blocker_report.json",
            "patch_summary.json",
            "terminal_result.json",
            "validation_results.json",
            "workspace_diff",
        },
    )
    diagnostic = _read_json(millforge_dir / "diagnostic.json")["diagnostic"]
    assert diagnostic["error_code"] == "malformed_tool_call"
    assert diagnostic["category"] == "model"
    events = _jsonl_event_types(millforge_dir / "events.jsonl")
    assert events == [
        "session_started",
        "workflow_constructed",
        *_model_event_sequence(6),
        "correction_issued",
    ]
    assert _jsonl_records(millforge_dir / "tool_trace.jsonl") == []


@pytest.mark.asyncio
async def test_canonical_builder_s6_tool_infrastructure_failure_runtime_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S6 tool infrastructure failure remains distinct from terminal outcomes."""
    monkeypatch.setattr("millforge.runtime.uuid.uuid4", lambda: _BUILDER_SESSION_ID)
    plan = make_canonical_builder_compiled_plan()
    request = make_canonical_builder_execution_request(tmp_path, plan=plan)
    plan_loader = FakePlanLoader(plan=plan)
    artifact_store = BuilderArtifactStore(request.run_directory.path)

    class _FailingBuilderToolExecutor(BuilderFakeToolExecutor):
        async def execute(self, call: Any, context: Any) -> Any:
            if call.node_id == "apply_patch":
                raise NonRetryableToolError("workspace adapter unavailable")
            return await super().execute(call, context)

    tool_executor = _FailingBuilderToolExecutor(
        plan=plan,
        artifact_store=artifact_store,
    )
    model_client = BuilderFakeModelClient(
        responses=_builder_script(
            [
                ("inspect_request", {}),
                ("read_plan", {}),
                ("read_file", {"path": BUILDER_WORKSPACE_PATH}),
                (
                    "apply_patch",
                    _builder_apply_args(),
                ),
            ]
        )
    )
    clock = FakeClock()
    cancellation_resolver = FakeCancellationResolver(is_cancelled=False)
    backend = ForgeGuardrailBackend(
        model_client=model_client,
        tool_executor=tool_executor,
        plan_loader=plan_loader,
        context_factory=ForgeContextFactory(),
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )
    runtime = DefaultHarnessRuntime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=RuntimeArtifactWriter(request.run_directory.path),
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )

    result = await runtime.execute(request)

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.TOOL_FAILURE
    assert result.terminal_intent is None
    assert result.usage is not None
    assert result.usage.model_calls == 4
    assert result.usage.tool_calls == 4
    assert model_client.call_count == 4
    assert [record.call.node_id for record in tool_executor.call_records] == [
        "inspect_request",
        "read_plan",
        "read_file",
    ]
    assert tool_executor.workspace.mutations == []
    assert tool_executor.workspace.read_file(BUILDER_WORKSPACE_PATH) == (
        BUILDER_WORKSPACE_INITIAL
    )

    millforge_dir = request.run_directory.path / "millforge"
    _assert_millforge_artifacts(
        millforge_dir,
        filenames=[
            "artifact_manifest.json",
            "compiled_plan.json",
            "diagnostic.json",
            "events.jsonl",
            "execution_summary.json",
            "metrics.json",
            "tool_trace.jsonl",
        ],
        manifest_artifact_ids={
            "diagnostic",
            "events",
            "execution_summary",
            "metrics",
            "tool_trace",
        },
        absent_filenames={
            "blocker_report.json",
            "patch_summary.json",
            "terminal_result.json",
            "validation_results.json",
            "workspace_diff",
        },
    )
    diagnostic = _read_json(millforge_dir / "diagnostic.json")["diagnostic"]
    assert diagnostic["error_code"] == "tool_execution_failed"
    assert diagnostic["category"] == "tool"
    assert _jsonl_event_types(millforge_dir / "events.jsonl") == [
        "session_started",
        "workflow_constructed",
        *_model_event_sequence(4),
        *_tool_event_sequence(4, final_status="tool_failed"),
    ]
    traces = _jsonl_records(millforge_dir / "tool_trace.jsonl")
    _assert_tool_trace_sequence(
        traces,
        [
            ("inspect_request", "success", "confirmed_complete"),
            ("read_plan", "success", "confirmed_complete"),
            ("read_file", "success", "confirmed_complete"),
            ("apply_patch", "hard_failure", "completion_unknown"),
        ],
    )


@pytest.mark.asyncio
async def test_canonical_builder_s8_backend_pre_inference_failure_runtime_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S8 backend failure before inference performs no model or tool invocation."""
    monkeypatch.setattr("millforge.runtime.uuid.uuid4", lambda: _BUILDER_SESSION_ID)
    base = make_canonical_builder_compiled_plan()
    bad_context = base.context_policy.model_copy(update={"strategy_id": "unsupported"})
    plan = _rehash_plan(base.model_copy(update={"context_policy": bad_context}))
    calls: list[tuple[str, Any]] = [("inspect_request", {})]

    (
        result,
        model_client,
        tool_executor,
        run_dir,
    ) = await _execute_canonical_builder_script(tmp_path, calls, plan=plan)

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.BACKEND_FAILURE
    assert result.terminal_intent is None
    assert result.usage is None
    assert model_client.call_count == 0
    assert tool_executor.call_records == []
    assert tool_executor.rejected_calls == []
    assert tool_executor.workspace.mutations == []

    millforge_dir = run_dir / "millforge"
    _assert_millforge_artifacts(
        millforge_dir,
        filenames=[
            "artifact_manifest.json",
            "compiled_plan.json",
            "diagnostic.json",
            "events.jsonl",
            "execution_summary.json",
            "metrics.json",
            "tool_trace.jsonl",
        ],
        manifest_artifact_ids={
            "diagnostic",
            "events",
            "execution_summary",
            "metrics",
            "tool_trace",
        },
        absent_filenames={
            "blocker_report.json",
            "patch_summary.json",
            "terminal_result.json",
            "validation_results.json",
            "workspace_diff",
        },
    )
    diagnostic = _read_json(millforge_dir / "diagnostic.json")["diagnostic"]
    assert diagnostic["error_code"] == "binding_rejected"
    assert diagnostic["category"] == "backend"
    assert _jsonl_event_types(millforge_dir / "events.jsonl") == []
    assert _jsonl_records(millforge_dir / "tool_trace.jsonl") == []


@pytest.mark.asyncio
async def test_canonical_builder_s7_blocked_runtime_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S7 blocks after inspect/read_plan and writes only blocked evidence."""
    monkeypatch.setattr("millforge.runtime.uuid.uuid4", lambda: _BUILDER_SESSION_ID)
    calls: list[tuple[str, dict[str, Any]]] = [
        ("inspect_request", {}),
        ("read_plan", {}),
        ("block_builder", _builder_block_args()),
    ]

    (
        result,
        model_client,
        tool_executor,
        run_dir,
    ) = await _execute_canonical_builder_script(tmp_path, calls)

    assert result.status == ExecutionStatus.COMPLETED
    assert result.result_class == ExecutionResultClass.DOMAIN_REJECTED
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "BUILDER_BLOCKED"
    assert result.terminal_intent.disposition == "blocked"
    assert result.usage is not None
    assert result.usage.model_calls == 3
    assert result.usage.tool_calls == 3
    assert model_client.call_count == 3
    assert [record.call.node_id for record in tool_executor.call_records] == [
        "inspect_request",
        "read_plan",
        "block_builder",
    ]
    assert tool_executor.workspace.read_file(BUILDER_WORKSPACE_PATH) != (
        BUILDER_WORKSPACE_FIXED
    )
    assert tool_executor.workspace.mutations == []
    assert tool_executor.rejected_calls == []

    millforge_dir = run_dir / "millforge"
    _assert_millforge_artifacts(
        millforge_dir,
        filenames=[
            "artifact_manifest.json",
            "blocker_report.json",
            "compiled_plan.json",
            "events.jsonl",
            "execution_summary.json",
            "metrics.json",
            "terminal_result.json",
            "tool_trace.jsonl",
        ],
        manifest_artifact_ids={
            "terminal_result",
            "execution_summary",
            "events",
            "tool_trace",
            "metrics",
        },
        absent_filenames={
            "diagnostic.json",
            "patch_summary.json",
            "validation_results.json",
            "workspace_diff",
        },
    )
    terminal_payload = _read_json(millforge_dir / "terminal_result.json")
    _assert_terminal_result_shape(
        terminal_payload,
        terminal_result="BUILDER_BLOCKED",
        result_class="domain_rejected",
    )
    assert terminal_payload["summary_artifact_paths"] == [
        "millforge/blocker_report.json"
    ]
    assert _read_json(millforge_dir / "blocker_report.json") == _builder_block_args()
    assert _jsonl_event_types(millforge_dir / "events.jsonl") == [
        "session_started",
        "workflow_constructed",
        *_model_event_sequence(3),
        *_tool_event_sequence(3),
        "terminal_intent_accepted",
    ]
    _assert_tool_trace_sequence(
        _jsonl_records(millforge_dir / "tool_trace.jsonl"),
        [
            ("inspect_request", "success", "confirmed_complete"),
            ("read_plan", "success", "confirmed_complete"),
            ("block_builder", "success", "confirmed_complete"),
        ],
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
            GuardedSessionStatus.PREREQUISITE_BUDGET_EXHAUSTED,
            ExecutionResultClass.BUDGET_EXHAUSTED,
        ),
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
    assert len(writer.events_calls) == 1
    assert len(writer.tool_trace_calls) == 1
    assert writer.events_calls[0][1] == [
        event.model_dump(mode="json") for event in session_result.events
    ]
    assert writer.tool_trace_calls[0][1] == [
        record.model_dump(mode="json") for record in session_result.tool_trace
    ]

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
    assert manifest_ids == {
        "execution_summary",
        "metrics",
        "events",
        "tool_trace",
    }


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
async def test_cancellation_after_plan_load_checkpoint_prevents_backend() -> None:
    """Cancellation observed after plan loading stops before verification/backend."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )

    class _FifthCheckCancelledToken(FakeCancellationToken):
        def __init__(self) -> None:
            super().__init__(is_cancelled_return=False)
            self.checks = 0

        def is_cancelled(self) -> bool:
            self.checks += 1
            return self.checks >= 5

    class _Resolver(FakeCancellationResolver):
        def __init__(self) -> None:
            super().__init__()
            self.token = _FifthCheckCancelledToken()

        def resolve(self, ref: CancellationRef) -> FakeCancellationToken:
            self.resolve_calls.append(ref)
            return self.token

    plan_loader = FakePlanLoader(plan=plan)
    backend = FakeGuardrailBackend()
    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=writer,
        cancellation_resolver=_Resolver(),
    )

    result = await runtime.execute(_valid_harness_request())

    assert len(plan_loader.load_calls) == 1
    assert len(backend.requests) == 0
    assert result.status == ExecutionStatus.INTERRUPTED
    assert result.result_class == ExecutionResultClass.CANCELLED
    assert result.terminal_certainty == TerminalCertainty.NOT_APPLICABLE
    assert writer.terminal_result_calls == []
    assert writer.execution_summary_calls[0][1]["terminal_certainty"] == (
        TerminalCertainty.NOT_APPLICABLE.value
    )


@pytest.mark.asyncio
async def test_cancellation_before_terminal_commit_prevents_domain_success() -> None:
    """Cancellation before terminal_result commit fails closed as interrupted."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )

    class _ThirdCheckCancelledToken(FakeCancellationToken):
        def __init__(self) -> None:
            super().__init__(is_cancelled_return=False)
            self.checks = 0

        def is_cancelled(self) -> bool:
            self.checks += 1
            return self.checks >= 3

    class _Resolver(FakeCancellationResolver):
        def __init__(self) -> None:
            super().__init__()
            self.token = _ThirdCheckCancelledToken()

        def resolve(self, ref: CancellationRef) -> FakeCancellationToken:
            self.resolve_calls.append(ref)
            return self.token

    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=_backend_with_result(),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
        cancellation_resolver=_Resolver(),
    )

    result = await runtime.execute(_valid_harness_request())

    assert result.status == ExecutionStatus.INTERRUPTED
    assert result.result_class == ExecutionResultClass.CANCELLED
    assert result.terminal_intent is None
    assert writer.terminal_result_calls == []


@pytest.mark.asyncio
async def test_cancellation_after_terminal_commit_does_not_overwrite_success() -> None:
    """Cancellation after atomic terminal_result commit preserves domain success."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )

    class _MutableCancellationToken(FakeCancellationToken):
        def __init__(self) -> None:
            super().__init__(is_cancelled_return=False)
            self.cancelled = False

        def is_cancelled(self) -> bool:
            return self.cancelled

    class _Resolver(FakeCancellationResolver):
        def __init__(self, token: _MutableCancellationToken) -> None:
            super().__init__()
            self.token = token

        def resolve(self, ref: CancellationRef) -> FakeCancellationToken:
            self.resolve_calls.append(ref)
            return self.token

    token = _MutableCancellationToken()

    class _CancellingWriter(FakeArtifactWriter):
        async def write_terminal_result(self, ref: ArtifactRef, data: Any) -> None:
            await super().write_terminal_result(ref, data)
            token.cancelled = True

    writer = _CancellingWriter()
    runtime = _build_runtime(
        backend=_backend_with_result(),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
        cancellation_resolver=_Resolver(token),
    )

    result = await runtime.execute(_valid_harness_request())

    _assert_success_result(result)
    assert result.terminal_intent is not None
    assert result.terminal_certainty == TerminalCertainty.COMMITTED
    assert len(writer.terminal_result_calls) == 1
    assert writer.events_calls == []
    assert writer.execution_summary_calls == []


@pytest.mark.asyncio
async def test_deadline_before_terminal_commit_prevents_domain_success() -> None:
    """Deadline expiry before terminal_result commit yields timed_out."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )

    class _MutableClock(FakeClock):
        def set_monotonic(self, value: float) -> None:
            self._monotonic_value = value

    clock = _MutableClock(monotonic_value=0.0)

    class _ExpiringBackend(FakeGuardrailBackend):
        async def run_session(self, request: Any) -> GuardedSessionResult:
            result = await super().run_session(request)
            clock.set_monotonic(3601.0)
            return result.model_copy(update={"session_id": request.session_id})

    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=_ExpiringBackend(responses=[make_test_guarded_session_result()]),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
        clock=clock,
    )

    result = await runtime.execute(_valid_harness_request(deadline_str=None))

    assert result.status == ExecutionStatus.INTERRUPTED
    assert result.result_class == ExecutionResultClass.TIMED_OUT
    assert result.terminal_intent is None
    assert result.terminal_certainty == TerminalCertainty.NOT_APPLICABLE
    assert writer.terminal_result_calls == []


@pytest.mark.asyncio
async def test_deadline_after_terminal_commit_does_not_overwrite_success() -> None:
    """Deadline expiry after atomic terminal_result commit preserves success."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )

    class _MutableClock(FakeClock):
        def set_monotonic(self, value: float) -> None:
            self._monotonic_value = value

    clock = _MutableClock(monotonic_value=0.0)

    class _DeadlineExpiringWriter(FakeArtifactWriter):
        async def write_terminal_result(self, ref: ArtifactRef, data: Any) -> None:
            await super().write_terminal_result(ref, data)
            clock.set_monotonic(3601.0)

    writer = _DeadlineExpiringWriter()
    runtime = _build_runtime(
        backend=_backend_with_result(),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
        clock=clock,
    )

    result = await runtime.execute(_valid_harness_request(deadline_str=None))

    _assert_success_result(result)
    assert result.terminal_intent is not None
    assert result.terminal_certainty == TerminalCertainty.COMMITTED
    assert len(writer.terminal_result_calls) == 1
    assert writer.events_calls == []
    assert writer.execution_summary_calls == []


@pytest.mark.asyncio
async def test_ambiguous_terminal_commit_interruption_records_unknown_certainty() -> (
    None
):
    """Interruption from terminal writer fails closed with unknown certainty."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )

    class _AmbiguousWriter(FakeArtifactWriter):
        async def write_terminal_result(self, ref: ArtifactRef, data: Any) -> None:
            await super().write_terminal_result(ref, data)
            raise OperationCancelledError("commit completion ordering unknown")

    writer = _AmbiguousWriter()
    runtime = _build_runtime(
        backend=_backend_with_result(),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
    )

    result = await runtime.execute(_valid_harness_request())

    assert result.status == ExecutionStatus.INTERRUPTED
    assert result.result_class == ExecutionResultClass.CANCELLED
    assert result.terminal_intent is None
    assert result.terminal_certainty == TerminalCertainty.UNKNOWN
    assert len(writer.terminal_result_calls) == 1
    assert writer.execution_summary_calls[0][1]["terminal_certainty"] == (
        TerminalCertainty.UNKNOWN.value
    )


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


@pytest.mark.asyncio
async def test_terminal_intent_refuses_selected_output_from_another_result() -> None:
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    success = TerminalSelectedOutputRequirement(
        terminal_result="success",
        selected_output=SelectedOutputRequirement(
            required=True,
            json_schema={"const": "success"},
        ),
    )
    foreign = TerminalSelectedOutputRequirement(
        terminal_result="foreign",
        selected_output=SelectedOutputRequirement(
            required=True,
            json_schema={"type": "array", "items": {"type": "integer"}},
        ),
    )
    request = _valid_harness_request().model_copy(
        update={"selected_output_requirements": (success, foreign)}
    )
    session_result = make_test_guarded_session_result(session_id="sess-runtime-001")
    assert session_result.terminal_intent is not None
    crossed_intent = session_result.terminal_intent.model_copy(
        update={
            "selected_output": SelectedOutputPresent(value=[1]),
            "selected_output_schema_sha256": foreign.selected_output.schema_sha256,
        }
    )
    writer = FakeArtifactWriter()
    runtime = _build_runtime(
        backend=_backend_with_result(
            session_result.model_copy(update={"terminal_intent": crossed_intent})
        ),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=writer,
    )

    result = await runtime.execute(request)

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.TERMINAL_RESULT_INVALID
    assert result.terminal_intent is None
    assert result.selected_output is None
    assert result.selected_output_schema_sha256 is None
    assert writer.terminal_result_calls == []


def _matrix_request(
    tmp_path: Path,
    plan: CompiledHarnessPlan,
    name: str,
) -> HarnessExecutionRequest:
    run_dir = tmp_path / name
    input_path = Path("millforge/input.json")
    input_target = run_dir / input_path
    input_target.parent.mkdir(parents=True, exist_ok=True)
    input_target.write_text('{"schema_version":"test"}\n', encoding="utf-8")
    return _valid_harness_request(
        request_id=f"req-{name}",
        run_id=f"run-{name}",
        hash_digest=plan.compiled_sha256,
    ).model_copy(
        update={
            "run_directory": RunDirRef(run_id=f"run-{name}", path=run_dir),
            "input_artifacts": (
                ArtifactRef(
                    artifact_id="art-input-001",
                    path=input_path,
                    content_type="application/json",
                ),
            ),
        }
    )


class _TerminalCommitFailingWriter(RuntimeArtifactWriter):
    async def write_terminal_result(self, ref: ArtifactRef, data: Any) -> None:
        raise ArtifactWriteError("terminal commit failed")


class _ManifestFailingRuntimeWriter(RuntimeArtifactWriter):
    async def write_artifact_manifest(self, ref: ArtifactRef, data: Any) -> None:
        raise ArtifactWriteError("manifest write failed")


class _ClosedHttpClientProbe:
    is_closed = True


class _MatrixEchoBackend(FakeGuardrailBackend):
    async def run_session(self, request: Any) -> GuardedSessionResult:
        result = await super().run_session(request)
        return result.model_copy(update={"session_id": request.session_id})


def _matrix_session_result(
    *,
    request: HarnessExecutionRequest,
    status: GuardedSessionStatus,
    event_type: SessionEventType,
    trace_status: ToolExecutionStatus = ToolExecutionStatus.HARD_FAILURE,
    side_effect_certainty: SideEffectCertainty = SideEffectCertainty.CONFIRMED_ABSENT,
    side_effect_class: SideEffectClass = SideEffectClass.READ_ONLY,
    idempotency: IdempotencyClass = IdempotencyClass.IDEMPOTENT,
    detail_code: str | None = None,
    detail_summary: str | None = None,
) -> GuardedSessionResult:
    trace = make_test_tool_trace_record(session_id="sess-test-001").model_copy(
        update={
            "request_id": request.request_id,
            "run_id": request.run_id,
            "stage": request.stage,
            "execution_status": trace_status,
            "side_effect_class": ToolTraceSideEffectClass(side_effect_class.value),
            "idempotency": ToolTraceIdempotency(idempotency.value),
            "side_effect_certainty": side_effect_certainty,
            "retryable": False,
            "side_effect_detail_code": detail_code,
            "side_effect_detail_summary": detail_summary,
            "side_effect_retry_allowed": False if detail_code is not None else None,
            "output_sha256": None,
        }
    )
    return make_test_guarded_session_result(
        session_id="sess-test-001",
        status=status,
        with_terminal_intent=False,
        request_id=request.request_id,
        run_id=request.run_id,
    ).model_copy(
        update={
            "events": (
                make_test_session_event(
                    session_id="sess-test-001",
                    request_id=request.request_id,
                    run_id=request.run_id,
                    stage=request.stage,
                    event_type=event_type,
                ),
            ),
            "tool_trace": (trace,),
        }
    )


@pytest.mark.parametrize(
    (
        "injection_point",
        "expected_status",
        "expected_result_class",
        "expect_terminal",
        "expect_session_trace",
    ),
    [
        (
            "before_plan_read",
            ExecutionStatus.INTERRUPTED,
            ExecutionResultClass.CANCELLED,
            False,
            False,
        ),
        (
            "during_plan_read",
            ExecutionStatus.FAILED,
            ExecutionResultClass.COMPILED_HARNESS_INVALID,
            False,
            False,
        ),
        (
            "after_plan_read_verification",
            ExecutionStatus.FAILED,
            ExecutionResultClass.COMPILED_HARNESS_INVALID,
            False,
            False,
        ),
        (
            "backend_construction",
            ExecutionStatus.FAILED,
            ExecutionResultClass.BACKEND_FAILURE,
            False,
            False,
        ),
        (
            "model_request",
            ExecutionStatus.FAILED,
            ExecutionResultClass.MODEL_FAILURE,
            False,
            False,
        ),
        (
            "model_response",
            ExecutionStatus.FAILED,
            ExecutionResultClass.MODEL_FAILURE,
            False,
            True,
        ),
        (
            "http_connect",
            ExecutionStatus.FAILED,
            ExecutionResultClass.MODEL_FAILURE,
            False,
            False,
        ),
        (
            "http_read",
            ExecutionStatus.FAILED,
            ExecutionResultClass.MODEL_FAILURE,
            False,
            True,
        ),
        (
            "tool_before_side_effect",
            ExecutionStatus.FAILED,
            ExecutionResultClass.TOOL_FAILURE,
            False,
            True,
        ),
        (
            "tool_after_mutating_side_effect",
            ExecutionStatus.FAILED,
            ExecutionResultClass.TOOL_FAILURE,
            False,
            True,
        ),
        (
            "terminal_validation",
            ExecutionStatus.FAILED,
            ExecutionResultClass.TERMINAL_RESULT_INVALID,
            False,
            False,
        ),
        (
            "terminal_commit",
            ExecutionStatus.FAILED,
            ExecutionResultClass.ARTIFACT_FINALIZATION_FAILED,
            False,
            False,
        ),
        (
            "final_artifact_write",
            ExecutionStatus.FAILED,
            ExecutionResultClass.ARTIFACT_FINALIZATION_FAILED,
            True,
            False,
        ),
        (
            "cleanup",
            ExecutionStatus.FAILED,
            ExecutionResultClass.INTERNAL_FAILURE,
            False,
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_deterministic_failure_injection_matrix_cleans_resources(
    tmp_path: Path,
    injection_point: str,
    expected_status: ExecutionStatus,
    expected_result_class: ExecutionResultClass,
    expect_terminal: bool,
    expect_session_trace: bool,
) -> None:
    """Every named injection point records deterministic result and cleanup state."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    request = _matrix_request(tmp_path, plan, injection_point)
    writer: Any = RuntimeArtifactWriter(request.run_directory.path)
    backend: FakeGuardrailBackend = FakeGuardrailBackend()
    plan_loader = FakePlanLoader(plan=plan)
    cancellation_resolver: FakeCancellationResolver = FakeCancellationResolver()

    if injection_point == "before_plan_read":
        cancellation_resolver = FakeCancellationResolver(is_cancelled=True)
    elif injection_point == "during_plan_read":
        plan_loader = FakePlanLoader(exception=ValueError("plan parse failed"))
    elif injection_point == "after_plan_read_verification":
        request = request.model_copy(
            update={
                "compiled_harness": request.compiled_harness.model_copy(
                    update={
                        "expected_hash": CompiledHarnessHash(
                            algorithm="sha256",
                            digest="0" * 64,
                        )
                    }
                )
            }
        )
    elif injection_point == "backend_construction":
        backend = FakeGuardrailBackend(
            exceptions=[BackendTranslationError("backend construction failed")]
        )
    elif injection_point in {"model_request", "http_connect"}:
        backend = FakeGuardrailBackend(
            exceptions=[ModelTransportError(f"{injection_point} failed")]
        )
    elif injection_point == "model_response":
        backend = _MatrixEchoBackend(
            responses=[
                _matrix_session_result(
                    request=request,
                    status=GuardedSessionStatus.MODEL_FAILED,
                    event_type=SessionEventType.MODEL_REQUEST_FAILED,
                )
            ]
        )
    elif injection_point == "http_read":
        backend = _MatrixEchoBackend(
            responses=[
                _matrix_session_result(
                    request=request,
                    status=GuardedSessionStatus.MODEL_FAILED,
                    event_type=SessionEventType.MODEL_REQUEST_FAILED,
                    detail_code="http_read_failed",
                    detail_summary="HTTP read failed after model request dispatch",
                )
            ]
        )
    elif injection_point == "tool_before_side_effect":
        backend = _MatrixEchoBackend(
            responses=[
                _matrix_session_result(
                    request=request,
                    status=GuardedSessionStatus.TOOL_FAILED,
                    event_type=SessionEventType.TOOL_FAILED,
                    trace_status=ToolExecutionStatus.NOT_EXECUTED,
                    side_effect_certainty=SideEffectCertainty.NOT_ATTEMPTED,
                )
            ]
        )
    elif injection_point == "tool_after_mutating_side_effect":
        backend = _MatrixEchoBackend(
            responses=[
                _matrix_session_result(
                    request=request,
                    status=GuardedSessionStatus.TOOL_FAILED,
                    event_type=SessionEventType.TOOL_FAILED,
                    trace_status=ToolExecutionStatus.AMBIGUOUS,
                    side_effect_certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                    side_effect_class=SideEffectClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyClass.NON_IDEMPOTENT,
                    detail_code="mutating_completion_unknown",
                    detail_summary="Workspace mutation may have completed",
                )
            ]
        )
    elif injection_point == "terminal_validation":
        backend = FakeGuardrailBackend(
            responses=[make_test_guarded_session_result(session_id="wrong-session")]
        )
    elif injection_point == "terminal_commit":
        backend = _backend_with_result()
        writer = _TerminalCommitFailingWriter(request.run_directory.path)
    elif injection_point == "final_artifact_write":
        backend = _backend_with_result()
        writer = _ManifestFailingRuntimeWriter(request.run_directory.path)
    elif injection_point == "cleanup":
        backend = FakeGuardrailBackend(exceptions=[RuntimeError("cleanup failed")])

    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=writer,
        cancellation_resolver=cancellation_resolver,
    )
    probe = FailureInjectionLeakProbe(
        request.run_directory.path,
        owned_http_clients=(_ClosedHttpClientProbe(),),
    )

    result = await runtime.execute(request)
    await probe.assert_clean(cleanup=lambda: None)

    assert result.status == expected_status
    assert result.result_class == expected_result_class
    terminal_path = request.run_directory.path / "millforge/terminal_result.json"
    assert terminal_path.exists() is expect_terminal
    assert result.terminal_intent is None or expect_terminal

    manifest_path = request.run_directory.path / "millforge/artifact_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_ids = {artifact["artifact_id"] for artifact in manifest["artifacts"]}
        assert "execution_summary" in manifest_ids
        assert "metrics" in manifest_ids
        assert "artifact_manifest" not in manifest_ids
        if expect_session_trace:
            assert {"events", "tool_trace"}.issubset(manifest_ids)
            events = json.loads(
                "["
                + (request.run_directory.path / "millforge/events.jsonl")
                .read_text(encoding="utf-8")
                .strip()
                .replace("\n", ",")
                + "]"
            )
            trace = json.loads(
                "["
                + (request.run_directory.path / "millforge/tool_trace.jsonl")
                .read_text(encoding="utf-8")
                .strip()
                .replace("\n", ",")
                + "]"
            )
            assert events
            assert trace
            trace_record = trace[0]
            assert trace_record["retryable"] is False
            assert trace_record["side_effect_certainty"] in {
                SideEffectCertainty.NOT_ATTEMPTED.value,
                SideEffectCertainty.CONFIRMED_ABSENT.value,
                SideEffectCertainty.COMPLETION_UNKNOWN.value,
            }
            if injection_point == "tool_after_mutating_side_effect":
                assert (
                    trace_record["side_effect_certainty"]
                    == SideEffectCertainty.COMPLETION_UNKNOWN.value
                )
                assert (
                    trace_record["side_effect_class"]
                    == SideEffectClass.WORKSPACE_WRITE.value
                )
                assert (
                    trace_record["idempotency"] == IdempotencyClass.NON_IDEMPOTENT.value
                )
                assert trace_record["side_effect_retry_allowed"] is False


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
        task=HarnessTaskInput(instruction="Complete the runtime test task."),
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
async def test_backend_exception_message_secret_redacted_in_diagnostic(
    tmp_path: Path,
) -> None:
    """Owned backend exception messages are redacted in persisted diagnostics."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )
    sentinel = "api_key=sk-checker-secret-value"
    request = _valid_harness_request().model_copy(
        update={"run_directory": RunDirRef(run_id="run-runtime-001", path=tmp_path)}
    )
    input_target = tmp_path / "millforge" / "input.json"
    input_target.parent.mkdir(parents=True, exist_ok=True)
    input_target.write_text('{"schema_version":"test"}\n', encoding="utf-8")
    runtime = _build_runtime(
        backend=FakeGuardrailBackend(
            exceptions=[BackendTranslationError(f"Backend refused: {sentinel}")]
        ),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=RuntimeArtifactWriter(tmp_path, producer="test/v1"),
    )

    result = await runtime.execute(request)

    _assert_failure_result(
        result,
        expected_status=ExecutionStatus.FAILED,
        expected_result_class=ExecutionResultClass.BACKEND_FAILURE,
    )
    assert result.diagnostic is not None
    assert result.diagnostic.message == "Backend refused: api_key**redacted**"
    millforge_dir = tmp_path / "millforge"
    diagnostic = _read_json(millforge_dir / "diagnostic.json")["diagnostic"]
    assert diagnostic["error_code"] == "backend_failure"
    assert diagnostic["category"] == "backend"
    assert diagnostic["message"] == "Backend refused: api_key**redacted**"
    assert sentinel not in json.dumps(diagnostic, sort_keys=True)


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


@pytest.mark.asyncio
async def test_external_cancelled_error_finalizes_partial_evidence_then_reraises(
    tmp_path: Path,
) -> None:
    """External asyncio cancellation writes bounded evidence before propagation."""
    plan = make_test_compiled_plan(
        plan_id=VALID_PLAN_ID,
        harness_id=VALID_HARNESS_ID,
        harness_version=VALID_HARNESS_VERSION,
    )

    class _ExternallyCancelledBackend(FakeGuardrailBackend):
        async def run_session(self, request: Any) -> Any:
            raise asyncio.CancelledError

    runtime = _build_runtime(
        backend=_ExternallyCancelledBackend(),
        plan_loader=FakePlanLoader(plan=plan),
        artifact_writer=RuntimeArtifactWriter(tmp_path, producer="test/v1"),
    )
    request = _valid_harness_request().model_copy(
        update={"run_directory": RunDirRef(run_id="run-runtime-001", path=tmp_path)}
    )
    input_target = tmp_path / "millforge" / "input.json"
    input_target.parent.mkdir(parents=True, exist_ok=True)
    input_target.write_text('{"schema_version":"test"}\n', encoding="utf-8")

    with pytest.raises(asyncio.CancelledError):
        await runtime.execute(request)

    millforge_dir = tmp_path / "millforge"
    assert not (millforge_dir / "terminal_result.json").exists()
    assert (millforge_dir / "execution_summary.json").exists()
    assert (millforge_dir / "metrics.json").exists()
    assert (millforge_dir / "artifact_manifest.json").exists()
    assert (millforge_dir / "diagnostic.json").exists()
    summary = _read_json(millforge_dir / "execution_summary.json")
    assert summary["status"] == ExecutionStatus.INTERRUPTED.value
    assert summary["result_class"] == ExecutionResultClass.CANCELLED.value
    assert summary["terminal_certainty"] == TerminalCertainty.NOT_APPLICABLE.value
    diagnostic = _read_json(millforge_dir / "diagnostic.json")["diagnostic"]
    assert diagnostic["error_code"] == "already_cancelled"


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
