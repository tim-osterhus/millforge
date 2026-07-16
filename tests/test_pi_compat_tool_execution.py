from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from millforge._forge import adapter as forge_adapter
import millforge.tools.pi_compat_runtime as pi_compat_runtime
from millforge import (
    CancellationRef,
    CapabilityEnvelope,
    CapabilityGrant,
    CompilerIdentity,
    CompiledArtifactPolicy,
    CompiledBudgetPolicy,
    CompiledContextPolicy,
    CompiledHarnessNode,
    CompiledHarnessPlan,
    CompiledModelProfile,
    CompiledPromptPolicy,
    Deadline,
    RunDirRef,
    SideEffectCertainty,
    StageIdentity,
    TimeoutRef,
    ToolBindingRef,
    ToolExecutionContext,
    ToolExecutionStatus,
    canonical_json_serialize,
)
from millforge.compiled_plan import finalize_compiled_plan_sha256
from millforge.contracts import (
    AssistantMessage,
    GuardedSessionRequest,
    GuardedSessionStatus,
    ModelCompletionResponse,
    ModelToolCall,
    ParsedToolArguments,
)
from millforge.testing import FakeModelClient
from millforge.tools.builtins import BUILTIN_TOOL_DESCRIPTORS
from millforge.tools.pi_compat.contracts import (
    PiCompatErrorKind,
    PiCompatOperationResult,
    PiCompatSideEffectState,
)
from millforge.tools.pi_compat.process import PiCompatShellConfig
from millforge.tools.pi_compat_catalog import PI_COMPAT_TOOL_DESCRIPTORS
from millforge.tools.results import make_tool_result
from millforge.tools.registry import ToolDescriptor
from tests.conftest import (
    FakeCancellationResolver,
    FakeClock,
    FakePlanLoader,
    make_test_harness_execution_request,
)


_DESCRIPTORS = {
    descriptor.model_tool_name: descriptor for descriptor in PI_COMPAT_TOOL_DESCRIPTORS
}
_TERMINAL_RESULTS = {
    "builtin.pi_compat.submit": "COMPLETE",
    "builtin.pi_compat.block": "BLOCKED",
    "builtin.pi_compat.reject": "REJECTED",
}


class _CancellationToken:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.poll_count = 0
        self.wait_count = 0

    @property
    def cancellation_id(self) -> str:
        return "cancel-1"

    @property
    def reason(self) -> str | None:
        return None

    def is_cancelled(self) -> bool:
        self.poll_count += 1
        return self.cancelled

    async def wait(self) -> None:
        self.wait_count += 1


class _CancellationResolver:
    def __init__(self, token: _CancellationToken) -> None:
        self.token = token
        self.refs: list[CancellationRef] = []

    def resolve(self, ref: CancellationRef) -> _CancellationToken:
        self.refs.append(ref)
        return self.token


def _operation_result(
    *,
    model_text: str = "fake operation result",
    error_kind: PiCompatErrorKind | None = None,
    side_effect_state: PiCompatSideEffectState = PiCompatSideEffectState.NOT_ATTEMPTED,
    exit_code: int | None = None,
    changed_path: Path | None = None,
) -> PiCompatOperationResult:
    return PiCompatOperationResult(
        model_text=model_text,
        truncated=True,
        error_kind=error_kind,
        exit_code=exit_code,
        changed_path=changed_path,
        side_effect_state=side_effect_state,
    )


def _plan(*descriptors: ToolDescriptor) -> CompiledHarnessPlan:
    if not any(descriptor.tool_id in _TERMINAL_RESULTS for descriptor in descriptors):
        descriptors = (*descriptors, _DESCRIPTORS["submit"])
    nodes = tuple(_node(index, descriptor) for index, descriptor in enumerate(descriptors))
    plan = CompiledHarnessPlan(
        schema_version="1.0",
        kind="compiled_millforge_harness",
        harness_id="millforge.test.pi-compat-runtime.v1",
        harness_version=1,
        source_sha256="1" * 64,
        compiled_sha256="0" * 64,
        stage_kind_ids=("builder",),
        model_profile=CompiledModelProfile(profile_id="profile.standard"),
        prompt_policy=CompiledPromptPolicy(
            policy_id="prompt.standard",
            system_instructions="Run tools.",
            include_request_context=True,
        ),
        budgets=CompiledBudgetPolicy(
            max_iterations=3,
            max_validation_retries=1,
            max_tool_errors=1,
            max_prerequisite_violations=1,
            max_premature_terminal_attempts=1,
        ),
        context_policy=CompiledContextPolicy(
            strategy_id="forge.tiered.v1",
            budget_tokens=4096,
            keep_recent_iterations=1,
            phase_thresholds=(0.5, 0.75, 0.9),
        ),
        nodes=nodes,
        required_capabilities=tuple(
            sorted(
                {
                    capability
                    for descriptor in descriptors
                    for capability in descriptor.required_capabilities
                }
            )
        ),
        terminal_result_map={
            node.node_id: node.terminal_result
            for node in nodes
            if node.terminal_result is not None
        },
        artifact_policy=CompiledArtifactPolicy(
            declared_artifact_ids=(), required_by_terminal=()
        ),
        compiler_identity=CompilerIdentity(
            name="test-compiler", version="1", build_id="test"
        ),
    )
    return finalize_compiled_plan_sha256(plan)


def _node(index: int, descriptor: ToolDescriptor) -> CompiledHarnessNode:
    return CompiledHarnessNode(
        node_id=f"node-{index}-{descriptor.model_tool_name}",
        model_tool_name=descriptor.model_tool_name,
        description=descriptor.description,
        input_schema=descriptor.model_dump(mode="json")["input_schema"],
        binding=ToolBindingRef(
            tool_id=descriptor.tool_id,
            tool_version=descriptor.tool_version,
            descriptor_sha256=descriptor.descriptor_sha256,
            implementation_id=descriptor.implementation_id,
        ),
        prerequisites=(),
        required=descriptor.tool_id not in _TERMINAL_RESULTS,
        terminal_result=_TERMINAL_RESULTS.get(descriptor.tool_id),
        required_capabilities=descriptor.required_capabilities,
        produced_artifact_ids=(),
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
    )


def _context(
    descriptor: ToolDescriptor,
    *,
    timeout_seconds: float = 60.0,
    effective_deadline: float = 60.0,
    current_monotonic: float = 0.0,
    cancellation_requested: bool = False,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="request-1",
        run_id="run-1",
        stage=StageIdentity(
            plane="execution", node_id="builder", stage_kind_id="builder"
        ),
        run_directory=RunDirRef(run_id="run-1", path=Path("run-1")),
        capability_envelope=CapabilityEnvelope(
            grants=tuple(
                CapabilityGrant(capability_id=capability)
                for capability in descriptor.required_capabilities
            )
        ),
        timeout=TimeoutRef(timeout_seconds=timeout_seconds),
        cancellation=CancellationRef(cancellation_id="cancel-1"),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=effective_deadline,
            effective_deadline_monotonic=effective_deadline,
            source="request",
        ),
        workspace_root=Path("workspace"),
        artifact_root=Path("artifacts"),
        compiled_artifact_policy=CompiledArtifactPolicy(
            declared_artifact_ids=(), required_by_terminal=()
        ),
        input_artifacts=(),
        cancellation_requested=cancellation_requested,
        current_monotonic=current_monotonic,
    )


def _executor(
    tmp_path: Path,
    *descriptors: ToolDescriptor,
    token: _CancellationToken | None = None,
    shell_config: PiCompatShellConfig | None = None,
) -> tuple[pi_compat_runtime.CompiledToolBindingExecutor, _CancellationResolver]:
    resolver = _CancellationResolver(token or _CancellationToken())
    executor = pi_compat_runtime.create_pi_compat_tool_executor(
        _plan(*descriptors),
        cwd=tmp_path,
        cancellation_resolver=resolver,
        shell_config=shell_config,
    )
    return executor, resolver


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "operation_name",
        "tool_name",
        "arguments",
        "expected_arguments",
        "side_effect_state",
        "changed_path",
    ),
    [
        (
            "execute_read",
            "read",
            {"path": "note.txt", "offset": 2, "limit": 4},
            {"path": "note.txt", "offset": 2, "limit": 4},
            PiCompatSideEffectState.NOT_ATTEMPTED,
            None,
        ),
        (
            "execute_edit",
            "edit",
            {
                "path": "note.txt",
                "edits": [{"oldText": "old", "newText": "new"}],
            },
            {
                "path": "note.txt",
                "edits": [{"oldText": "old", "newText": "new"}],
            },
            PiCompatSideEffectState.CONFIRMED_COMPLETE,
            Path("changed.txt"),
        ),
        (
            "execute_write",
            "write",
            {"path": "note.txt", "content": "new text"},
            {"path": "note.txt", "content": "new text"},
            PiCompatSideEffectState.CONFIRMED_COMPLETE,
            Path("changed.txt"),
        ),
        (
            "execute_grep",
            "grep",
            {
                "pattern": "needle",
                "path": "src",
                "glob": "*.py",
                "ignoreCase": True,
                "literal": False,
                "context": 2,
                "limit": 3,
            },
            {
                "pattern": "needle",
                "path": "src",
                "glob": "*.py",
                "ignoreCase": True,
                "literal": False,
                "context": 2,
                "limit": 3,
            },
            PiCompatSideEffectState.NOT_ATTEMPTED,
            None,
        ),
        (
            "execute_find",
            "find",
            {"pattern": "*.py", "path": "src", "limit": 8},
            {"pattern": "*.py", "path": "src", "limit": 8},
            PiCompatSideEffectState.NOT_ATTEMPTED,
            None,
        ),
        (
            "execute_ls",
            "ls",
            {"path": "src", "limit": 6},
            {"path": "src", "limit": 6},
            PiCompatSideEffectState.NOT_ATTEMPTED,
            None,
        ),
    ],
)
async def test_filesystem_operation_wrappers_adapt_arguments_and_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation_name: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    expected_arguments: Mapping[str, Any],
    side_effect_state: PiCompatSideEffectState,
    changed_path: Path | None,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_operation(**kwargs: Any) -> PiCompatOperationResult:
        calls.append(kwargs)
        return _operation_result(
            side_effect_state=side_effect_state, changed_path=changed_path
        )

    monkeypatch.setattr(pi_compat_runtime, operation_name, fake_operation)
    descriptor = _DESCRIPTORS[tool_name]
    executor, _ = _executor(tmp_path, descriptor)

    result = await executor.execute_model_tool(
        model_tool_name=tool_name,
        call_id=f"call-{tool_name}",
        arguments=arguments,
        context=_context(descriptor),
    )

    assert calls == [{"cwd": tmp_path, **expected_arguments}]
    assert result.status is ToolExecutionStatus.SUCCESS
    assert result.summary == "fake operation result"
    assert result.structured_data == {
        "model_text": "fake operation result",
        "truncated": True,
        **({"changed_path": "changed.txt"} if changed_path is not None else {}),
    }
    assert result.side_effect_certainty is SideEffectCertainty(side_effect_state.value)
    assert result.side_effect_record is None


@pytest.mark.asyncio
async def test_bash_wrapper_adapts_timeout_and_cancellation_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    token = _CancellationToken()
    shell_config = PiCompatShellConfig(executable="fake-shell", arguments=("-c",))

    async def fake_execute_bash(**kwargs: Any) -> PiCompatOperationResult:
        calls.append(kwargs)
        cancellation = kwargs["cancellation"]
        assert cancellation.is_cancelled() is False
        await cancellation.wait()
        return _operation_result(
            side_effect_state=PiCompatSideEffectState.CONFIRMED_COMPLETE,
            exit_code=0,
        )

    monkeypatch.setattr(pi_compat_runtime, "execute_bash", fake_execute_bash)
    descriptor = _DESCRIPTORS["bash"]
    executor, resolver = _executor(
        tmp_path,
        descriptor,
        token=token,
        shell_config=shell_config,
    )

    result = await executor.execute_model_tool(
        model_tool_name="bash",
        call_id="call-bash",
        arguments={"command": "fake command", "timeout": 700},
        context=_context(
            descriptor,
            timeout_seconds=900,
            effective_deadline=1_500,
            current_monotonic=100,
        ),
    )

    assert result.status is ToolExecutionStatus.SUCCESS
    assert len(calls) == 1
    assert calls[0]["cwd"] == tmp_path
    assert calls[0]["command"] == "fake command"
    assert calls[0]["timeout_seconds"] == 700
    assert math.isfinite(calls[0]["timeout_seconds"])
    assert calls[0]["shell_config"] is shell_config
    assert resolver.refs == [CancellationRef(cancellation_id="cancel-1")]
    assert token.poll_count >= 2
    assert token.wait_count == 1
    assert result.structured_data["exit_code"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_timeout", "expected_timeout"),
    [(700, 700), (-1, 900)],
)
async def test_bash_timeout_only_lowers_the_harness_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model_timeout: int,
    expected_timeout: int,
) -> None:
    timeouts: list[float] = []

    async def fake_execute_bash(**kwargs: Any) -> PiCompatOperationResult:
        timeouts.append(kwargs["timeout_seconds"])
        return _operation_result(
            side_effect_state=PiCompatSideEffectState.CONFIRMED_COMPLETE
        )

    monkeypatch.setattr(pi_compat_runtime, "execute_bash", fake_execute_bash)
    descriptor = _DESCRIPTORS["bash"]
    executor, _ = _executor(
        tmp_path,
        descriptor,
        shell_config=PiCompatShellConfig(executable="fake-shell", arguments=()),
    )

    result = await executor.execute_model_tool(
        model_tool_name="bash",
        call_id="call-bash-timeout",
        arguments={"command": "fake command", "timeout": model_timeout},
        context=_context(
            descriptor,
            timeout_seconds=900,
            effective_deadline=1_500,
            current_monotonic=100,
        ),
    )

    assert result.status is ToolExecutionStatus.SUCCESS
    assert timeouts == [expected_timeout]


@pytest.mark.asyncio
async def test_bash_pre_entry_denials_do_not_call_11a(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_execute_bash(**kwargs: Any) -> PiCompatOperationResult:
        calls.append(kwargs)
        return _operation_result()

    monkeypatch.setattr(pi_compat_runtime, "execute_bash", fake_execute_bash)
    descriptor = _DESCRIPTORS["bash"]
    executor, _ = _executor(
        tmp_path,
        descriptor,
        shell_config=PiCompatShellConfig(executable="fake-shell", arguments=()),
    )

    expired = await executor.execute_model_tool(
        model_tool_name="bash",
        call_id="call-expired",
        arguments={"command": "fake command"},
        context=_context(
            descriptor, effective_deadline=10, current_monotonic=10
        ),
    )
    cancelled = await executor.execute_model_tool(
        model_tool_name="bash",
        call_id="call-cancelled",
        arguments={"command": "fake command"},
        context=_context(descriptor, cancellation_requested=True),
    )

    assert calls == []
    assert expired.status is ToolExecutionStatus.NOT_EXECUTED
    assert expired.error_code == "timeout"
    assert expired.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert cancelled.status is ToolExecutionStatus.NOT_EXECUTED
    assert cancelled.error_code == "cancelled"
    assert cancelled.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED


@pytest.mark.asyncio
async def test_bash_adapter_token_cancellation_does_not_call_11a(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_execute_bash(**kwargs: Any) -> PiCompatOperationResult:
        calls.append(kwargs)
        return _operation_result()

    monkeypatch.setattr(pi_compat_runtime, "execute_bash", fake_execute_bash)
    descriptor = _DESCRIPTORS["bash"]
    token = _CancellationToken(cancelled=True)
    executor, resolver = _executor(
        tmp_path,
        descriptor,
        token=token,
        shell_config=PiCompatShellConfig(executable="fake-shell", arguments=()),
    )

    result = await executor.execute_model_tool(
        model_tool_name="bash",
        call_id="call-token-cancelled",
        arguments={"command": "fake command"},
        context=_context(descriptor),
    )

    assert calls == []
    assert resolver.refs == [CancellationRef(cancellation_id="cancel-1")]
    assert result.status is ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == "cancelled"
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "error_kind",
        "status",
        "error_code",
        "certainty",
        "expects_record",
    ),
    [
        (
            PiCompatErrorKind.INVALID_ARGUMENTS,
            ToolExecutionStatus.NOT_EXECUTED,
            "invalid_arguments",
            PiCompatSideEffectState.NOT_ATTEMPTED,
            False,
        ),
        (
            PiCompatErrorKind.NOT_FOUND,
            ToolExecutionStatus.SOFT_FAILURE,
            "not_found",
            PiCompatSideEffectState.CONFIRMED_ABSENT,
            True,
        ),
        (
            PiCompatErrorKind.PERMISSION_DENIED,
            ToolExecutionStatus.SOFT_FAILURE,
            "permission_denied",
            PiCompatSideEffectState.ROLLED_BACK,
            True,
        ),
        (
            PiCompatErrorKind.CONFLICT,
            ToolExecutionStatus.SOFT_FAILURE,
            "conflict",
            PiCompatSideEffectState.COMPLETION_UNKNOWN,
            True,
        ),
        (
            PiCompatErrorKind.IO_ERROR,
            ToolExecutionStatus.SOFT_FAILURE,
            "io_error",
            PiCompatSideEffectState.NOT_ATTEMPTED,
            False,
        ),
        (
            PiCompatErrorKind.PROCESS_EXIT_NONZERO,
            ToolExecutionStatus.SOFT_FAILURE,
            "process_exit_nonzero",
            PiCompatSideEffectState.CONFIRMED_COMPLETE,
            False,
        ),
        (
            PiCompatErrorKind.PROCESS_TIMEOUT,
            ToolExecutionStatus.TIMED_OUT,
            "timeout",
            PiCompatSideEffectState.COMPLETION_UNKNOWN,
            True,
        ),
        (
            PiCompatErrorKind.CANCELLED,
            ToolExecutionStatus.CANCELLED,
            "cancelled",
            PiCompatSideEffectState.COMPLETION_UNKNOWN,
            True,
        ),
        (
            PiCompatErrorKind.PROCESS_LAUNCH_ERROR,
            ToolExecutionStatus.HARD_FAILURE,
            "process_launch_error",
            PiCompatSideEffectState.NOT_ATTEMPTED,
            False,
        ),
    ],
)
async def test_operation_error_mapping_is_exhaustive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_kind: PiCompatErrorKind,
    status: ToolExecutionStatus,
    error_code: str,
    certainty: PiCompatSideEffectState,
    expects_record: bool,
) -> None:
    def fake_execute_read(**_kwargs: Any) -> PiCompatOperationResult:
        return _operation_result(
            error_kind=error_kind,
            side_effect_state=certainty,
            exit_code=17
            if error_kind is PiCompatErrorKind.PROCESS_EXIT_NONZERO
            else None,
        )

    monkeypatch.setattr(pi_compat_runtime, "execute_read", fake_execute_read)
    descriptor = _DESCRIPTORS["read"]
    executor, _ = _executor(tmp_path, descriptor)

    result = await executor.execute_model_tool(
        model_tool_name="read",
        call_id=f"call-{error_kind.value}",
        arguments={"path": "note.txt"},
        context=_context(descriptor),
    )

    assert result.status is status
    assert result.error_code == error_code
    assert result.retryable is False
    assert result.summary == "fake operation result"
    assert result.structured_data["model_text"] == "fake operation result"
    assert result.structured_data["truncated"] is True
    assert result.side_effect_certainty is SideEffectCertainty(certainty.value)
    if error_kind is PiCompatErrorKind.PROCESS_EXIT_NONZERO:
        assert result.structured_data["exit_code"] == 17
    else:
        assert "exit_code" not in result.structured_data
    if expects_record:
        assert result.side_effect_record is not None
        assert result.side_effect_record.certainty is SideEffectCertainty(certainty.value)
        assert result.side_effect_record.detail_code == error_code
        assert result.side_effect_record.summary == "fake operation result"
        assert result.side_effect_record.retry_allowed is False
    else:
        assert result.side_effect_record is None


@pytest.mark.asyncio
async def test_terminal_results_and_blank_summary_validation(tmp_path: Path) -> None:
    descriptors = (_DESCRIPTORS["submit"], _DESCRIPTORS["block"], _DESCRIPTORS["reject"])
    executor, _ = _executor(tmp_path, *descriptors)

    for tool_name, terminal_result in (
        ("submit", "COMPLETE"),
        ("block", "BLOCKED"),
        ("reject", "REJECTED"),
    ):
        descriptor = _DESCRIPTORS[tool_name]
        result = await executor.execute_model_tool(
            model_tool_name=tool_name,
            call_id=f"call-{tool_name}",
            arguments={"terminal_result": terminal_result, "summary": "finished"},
            context=_context(descriptor),
        )
        assert result.status is ToolExecutionStatus.SUCCESS
        assert result.error_code is None
        assert result.retryable is False
        assert result.summary == "finished"
        assert result.structured_data == {"model_text": "finished", "truncated": False}
        assert result.side_effect_certainty is SideEffectCertainty.CONFIRMED_COMPLETE
        assert result.side_effect_record is None

    blank = await executor.execute_model_tool(
        model_tool_name="submit",
        call_id="call-blank-terminal",
        arguments={"terminal_result": "COMPLETE", "summary": " \n\t "},
        context=_context(_DESCRIPTORS["submit"]),
    )
    assert blank.status is ToolExecutionStatus.NOT_EXECUTED
    assert blank.error_code == "invalid_arguments"
    assert blank.retryable is False
    assert blank.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert blank.side_effect_record is None
    assert blank.structured_data == {
        "model_text": "terminal summary must not be blank",
        "truncated": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "terminal_result", "disposition", "expected_status"),
    [
        ("submit", "COMPLETE", "success", GuardedSessionStatus.TERMINAL),
        ("block", "BLOCKED", "blocked", GuardedSessionStatus.REJECTED),
        ("reject", "REJECTED", "rejected", GuardedSessionStatus.REJECTED),
    ],
)
async def test_forge_backend_projects_pi_terminal_dispositions(
    tmp_path: Path,
    tool_name: str,
    terminal_result: str,
    disposition: str,
    expected_status: GuardedSessionStatus,
) -> None:
    descriptor = _DESCRIPTORS[tool_name]
    plan = _plan(descriptor)
    executor = pi_compat_runtime.create_pi_compat_tool_executor(
        plan,
        cwd=tmp_path,
        cancellation_resolver=_CancellationResolver(_CancellationToken()),
    )
    execution_request = make_test_harness_execution_request(
        harness_id=plan.harness_id,
        harness_version=plan.harness_version,
        hash_digest=plan.compiled_sha256,
        profile_id=plan.model_profile.profile_id,
    ).model_copy(
        update={
            "capability_envelope": CapabilityEnvelope(
                grants=tuple(
                    CapabilityGrant(capability_id=capability)
                    for capability in plan.required_capabilities
                )
            )
        }
    )
    request = GuardedSessionRequest(
        session_id=f"session-pi-{tool_name}",
        execution_request=execution_request,
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=60.0,
            effective_deadline_monotonic=60.0,
            source="request",
        ),
    )
    model_client = FakeModelClient(
        responses=[
            ModelCompletionResponse(
                provider_request_id=f"provider-{tool_name}",
                model_id=plan.model_profile.profile_id,
                message=AssistantMessage(
                    content="terminal",
                    tool_calls=(
                        ModelToolCall(
                            call_id=f"call-{tool_name}",
                            name=tool_name,
                            arguments=ParsedToolArguments(
                                value={
                                    "terminal_result": terminal_result,
                                    "summary": "finished",
                                }
                            ),
                        ),
                    ),
                ),
                finish_reason="tool_calls",
                usage=None,
            )
        ]
    )
    backend = forge_adapter.ForgeGuardrailBackend(
        model_client=model_client,
        tool_executor=executor,
        plan_loader=FakePlanLoader(plan=plan),
        context_factory=forge_adapter.ForgeContextFactory(),
        clock=FakeClock(monotonic_value=1.0),
        cancellation_resolver=FakeCancellationResolver(),
    )

    result = await backend.run_session(request)

    assert result.status is expected_status
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == terminal_result
    assert result.terminal_intent.disposition == disposition


@pytest.mark.asyncio
async def test_unrestricted_output_preserves_model_content_while_trace_redacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    descriptor = _DESCRIPTORS["write"]
    secret_path = "/srv/example/private.txt"
    changed_path = Path("/srv/example/changed.txt")
    raw_text = f"SECRET_TOKEN=abc123 {secret_path} " + ("content " * 30_000)

    def fake_execute_write(**_kwargs: Any) -> PiCompatOperationResult:
        return _operation_result(
            model_text=raw_text,
            changed_path=changed_path,
            side_effect_state=PiCompatSideEffectState.CONFIRMED_COMPLETE,
        )

    monkeypatch.setattr(pi_compat_runtime, "execute_write", fake_execute_write)
    executor, _ = _executor(tmp_path, descriptor)

    result = await executor.execute_model_tool(
        model_tool_name="write",
        call_id="call-unrestricted-output",
        arguments={"path": "note.txt", "content": "new text"},
        context=_context(descriptor),
    )

    assert "SECRET_TOKEN=abc123" in result.summary
    assert secret_path in result.summary
    assert "SECRET_TOKEN=abc123" in result.structured_data["model_text"]
    assert secret_path in result.structured_data["model_text"]
    assert result.structured_data["changed_path"] == changed_path.as_posix()
    assert len(result.summary.encode("utf-8")) <= descriptor.output_policy.max_summary_utf8
    assert len(result.summary.encode("utf-8")) > 8_192
    assert (
        len(canonical_json_serialize(result.structured_data).encode("utf-8"))
        <= descriptor.output_policy.max_output_bytes
    )
    assert "[truncated]" in result.summary
    assert "[truncated]" in result.structured_data["model_text"]

    trace = executor.trace_records[-1]
    assert "SECRET_TOKEN=abc123" not in trace.summary
    assert secret_path not in trace.summary
    assert "**redacted**" in trace.summary
    assert "[path]" in trace.summary


def test_governed_output_policy_still_redacts_model_visible_content() -> None:
    descriptor = next(
        item
        for item in BUILTIN_TOOL_DESCRIPTORS
        if item.tool_id == "builtin.workspace.read_file"
    )
    raw_text = "SECRET_TOKEN=abc123 /srv/example/private.txt"

    result = make_tool_result(
        call_id="call-governed-output",
        status=ToolExecutionStatus.SUCCESS,
        code=None,
        summary=raw_text,
        structured_data={"content": raw_text},
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
        side_effect_certainty=SideEffectCertainty.NOT_ATTEMPTED,
        input_sha256="a" * 64,
        output_policy=descriptor.output_policy,
    )

    assert "SECRET_TOKEN=abc123" not in result.summary
    assert "/srv/example/private.txt" not in result.summary
    assert "SECRET_TOKEN=abc123" not in result.structured_data["content"]
    assert "/srv/example/private.txt" not in result.structured_data["content"]


@pytest.mark.parametrize(
    ("terminal_result", "disposition"),
    [
        ("COMPLETE", "success"),
        ("BLOCKED", "blocked"),
        ("REJECTED", "rejected"),
        ("CHECKER_REJECTED", "success"),
    ],
)
def test_forge_terminal_disposition_is_exact(
    terminal_result: str,
    disposition: str,
) -> None:
    assert forge_adapter._terminal_disposition(terminal_result) == disposition


def test_executor_factory_requires_absolute_cwd_and_bash_shell_config(
    tmp_path: Path,
) -> None:
    read = _DESCRIPTORS["read"]
    bash = _DESCRIPTORS["bash"]
    resolver = _CancellationResolver(_CancellationToken())

    with pytest.raises(ValueError, match="cwd must be absolute"):
        pi_compat_runtime.create_pi_compat_tool_executor(
            _plan(read),
            cwd=Path("relative"),
            cancellation_resolver=resolver,
        )
    with pytest.raises(ValueError, match="requires a resolved shell_config"):
        pi_compat_runtime.create_pi_compat_tool_executor(
            _plan(bash), cwd=tmp_path, cancellation_resolver=resolver
        )

    executor = pi_compat_runtime.create_pi_compat_tool_executor(
        _plan(read), cwd=tmp_path, cancellation_resolver=resolver
    )
    assert executor.supports_tool("read") is True
