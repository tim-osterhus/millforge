"""Runtime adapters for the Pi-compatible unrestricted tool catalog."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from millforge import (
    CancellationResolver,
    SideEffectCertainty,
    SideEffectRecord,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolExecutionStatus,
    ValidatedToolCall,
)
from millforge.compiled_plan import CompiledHarnessPlan
from millforge.tools.execution import CompiledToolBindingExecutor, RuntimeToolRegistry
from millforge.tools.pi_compat.contracts import (
    PiCompatErrorKind,
    PiCompatOperationResult,
)
from millforge.tools.pi_compat.editing import execute_edit
from millforge.tools.pi_compat.operations import execute_ls, execute_read, execute_write
from millforge.tools.pi_compat.process import PiCompatShellConfig, execute_bash
from millforge.tools.pi_compat.search import execute_find, execute_grep
from millforge.tools.results import (
    ToolExecutionErrorCode,
    canonical_sha256,
    make_tool_result,
)
from millforge.tools.registry import ToolDescriptor

from .pi_compat_catalog import (
    PI_COMPAT_TOOL_DESCRIPTORS,
    create_pi_compat_tool_snapshot,
)

__all__ = ["create_pi_compat_tool_executor"]


_ERROR_STATUS_AND_CODE: dict[
    PiCompatErrorKind, tuple[ToolExecutionStatus, ToolExecutionErrorCode]
] = {
    PiCompatErrorKind.INVALID_ARGUMENTS: (
        ToolExecutionStatus.NOT_EXECUTED,
        ToolExecutionErrorCode.INVALID_ARGUMENTS,
    ),
    PiCompatErrorKind.NOT_FOUND: (
        ToolExecutionStatus.SOFT_FAILURE,
        ToolExecutionErrorCode.NOT_FOUND,
    ),
    PiCompatErrorKind.PERMISSION_DENIED: (
        ToolExecutionStatus.SOFT_FAILURE,
        ToolExecutionErrorCode.PERMISSION_DENIED,
    ),
    PiCompatErrorKind.CONFLICT: (
        ToolExecutionStatus.SOFT_FAILURE,
        ToolExecutionErrorCode.CONFLICT,
    ),
    PiCompatErrorKind.IO_ERROR: (
        ToolExecutionStatus.SOFT_FAILURE,
        ToolExecutionErrorCode.IO_ERROR,
    ),
    PiCompatErrorKind.PROCESS_EXIT_NONZERO: (
        ToolExecutionStatus.SOFT_FAILURE,
        ToolExecutionErrorCode.PROCESS_EXIT_NONZERO,
    ),
    PiCompatErrorKind.PROCESS_TIMEOUT: (
        ToolExecutionStatus.TIMED_OUT,
        ToolExecutionErrorCode.TIMEOUT,
    ),
    PiCompatErrorKind.CANCELLED: (
        ToolExecutionStatus.CANCELLED,
        ToolExecutionErrorCode.CANCELLED,
    ),
    PiCompatErrorKind.PROCESS_LAUNCH_ERROR: (
        ToolExecutionStatus.HARD_FAILURE,
        ToolExecutionErrorCode.PROCESS_LAUNCH_ERROR,
    ),
}

_SIDE_EFFECT_RECORD_CERTAINTIES = frozenset(
    {
        SideEffectCertainty.CONFIRMED_ABSENT,
        SideEffectCertainty.ROLLED_BACK,
        SideEffectCertainty.COMPLETION_UNKNOWN,
    }
)


class _PiCompatCancellationBridge:
    """Expose only the poll/wait protocol owned by the 11A bash operation."""

    def __init__(self, token: Any) -> None:
        self._token = token

    def is_cancelled(self) -> bool:
        return self._token.is_cancelled()

    async def wait(self) -> None:
        await self._token.wait()


def create_pi_compat_tool_executor(
    plan: CompiledHarnessPlan,
    *,
    cwd: Path,
    cancellation_resolver: CancellationResolver,
    shell_config: PiCompatShellConfig | None = None,
) -> CompiledToolBindingExecutor:
    """Create a plan-scoped executor for the Pi-compatible tool catalog."""

    if not cwd.is_absolute():
        raise ValueError("cwd must be absolute")
    if _plan_uses_bash(plan) and shell_config is None:
        raise ValueError("Pi-compatible bash requires a resolved shell_config")

    descriptors = {
        descriptor.tool_id: descriptor for descriptor in PI_COMPAT_TOOL_DESCRIPTORS
    }
    read = descriptors["builtin.pi_compat.read"]
    bash = descriptors["builtin.pi_compat.bash"]
    edit = descriptors["builtin.pi_compat.edit"]
    write = descriptors["builtin.pi_compat.write"]
    grep = descriptors["builtin.pi_compat.grep"]
    find = descriptors["builtin.pi_compat.find"]
    ls = descriptors["builtin.pi_compat.ls"]
    submit = descriptors["builtin.pi_compat.submit"]
    block = descriptors["builtin.pi_compat.block"]
    reject = descriptors["builtin.pi_compat.reject"]

    registry = RuntimeToolRegistry()

    def read_implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return _adapt_operation_result(
            call,
            read,
            execute_read(
                cwd=cwd,
                path=cast(str, call.arguments["path"]),
                offset=cast(int | float | None, call.arguments.get("offset")),
                limit=cast(int | float | None, call.arguments.get("limit")),
            ),
        )

    async def bash_implementation(
        call: ValidatedToolCall, context: ToolExecutionContext
    ) -> ToolExecutionResult:
        timeout_seconds = _bash_timeout_seconds(call, context, bash)
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            return _pre_entry_result(
                call,
                bash,
                ToolExecutionErrorCode.TIMEOUT,
                "tool deadline expired before implementation entry",
            )
        if context.cancellation_requested:
            return _pre_entry_result(
                call,
                bash,
                ToolExecutionErrorCode.CANCELLED,
                "tool call was cancelled before implementation entry",
            )
        token = cancellation_resolver.resolve(context.cancellation)
        cancellation = _PiCompatCancellationBridge(token)
        if cancellation.is_cancelled():
            return _pre_entry_result(
                call,
                bash,
                ToolExecutionErrorCode.CANCELLED,
                "tool call was cancelled before implementation entry",
            )
        assert shell_config is not None
        operation_result = await execute_bash(
            cwd=cwd,
            command=cast(str, call.arguments["command"]),
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            shell_config=shell_config,
        )
        return _adapt_operation_result(call, bash, operation_result)

    def edit_implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return _adapt_operation_result(
            call,
            edit,
            execute_edit(
                cwd=cwd,
                path=cast(str, call.arguments["path"]),
                edits=cast(Sequence[Mapping[str, object]], call.arguments["edits"]),
            ),
        )

    def write_implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return _adapt_operation_result(
            call,
            write,
            execute_write(
                cwd=cwd,
                path=cast(str, call.arguments["path"]),
                content=cast(str, call.arguments["content"]),
            ),
        )

    def grep_implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return _adapt_operation_result(
            call,
            grep,
            execute_grep(
                cwd=cwd,
                pattern=cast(str, call.arguments["pattern"]),
                path=cast(str | None, call.arguments.get("path")),
                glob=cast(str | None, call.arguments.get("glob")),
                ignoreCase=cast(bool | None, call.arguments.get("ignoreCase")),
                literal=cast(bool | None, call.arguments.get("literal")),
                context=cast(int | float | None, call.arguments.get("context")),
                limit=cast(int | float | None, call.arguments.get("limit")),
            ),
        )

    def find_implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return _adapt_operation_result(
            call,
            find,
            execute_find(
                cwd=cwd,
                pattern=cast(str, call.arguments["pattern"]),
                path=cast(str | None, call.arguments.get("path")),
                limit=cast(int | float | None, call.arguments.get("limit")),
            ),
        )

    def ls_implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return _adapt_operation_result(
            call,
            ls,
            execute_ls(
                cwd=cwd,
                path=cast(str | None, call.arguments.get("path")),
                limit=cast(int | float | None, call.arguments.get("limit")),
            ),
        )

    def submit_implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return _terminal_result(call, submit)

    def block_implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return _terminal_result(call, block)

    def reject_implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return _terminal_result(call, reject)

    registry.register(read.implementation_id, read_implementation)
    registry.register(bash.implementation_id, bash_implementation)
    registry.register(edit.implementation_id, edit_implementation)
    registry.register(write.implementation_id, write_implementation)
    registry.register(grep.implementation_id, grep_implementation)
    registry.register(find.implementation_id, find_implementation)
    registry.register(ls.implementation_id, ls_implementation)
    registry.register(submit.implementation_id, submit_implementation)
    registry.register(block.implementation_id, block_implementation)
    registry.register(reject.implementation_id, reject_implementation)

    return CompiledToolBindingExecutor(
        plan=plan,
        descriptor_snapshot=create_pi_compat_tool_snapshot(),
        runtime_registry=registry,
    )


def _plan_uses_bash(plan: CompiledHarnessPlan) -> bool:
    return any(node.binding.tool_id == "builtin.pi_compat.bash" for node in plan.nodes)


def _bash_timeout_seconds(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
    descriptor: ToolDescriptor,
) -> float:
    supplied_timeout = cast(int | float | None, call.arguments.get("timeout"))
    model_timeout = (
        float(supplied_timeout)
        if supplied_timeout is not None and supplied_timeout > 0
        else math.inf
    )
    return min(
        float(descriptor.timeout_policy.timeout_seconds),
        model_timeout,
        context.timeout.timeout_seconds,
        context.deadline.remaining(lambda: context.current_monotonic),
    )


def _adapt_operation_result(
    call: ValidatedToolCall,
    descriptor: ToolDescriptor,
    operation_result: PiCompatOperationResult,
) -> ToolExecutionResult:
    error_code = _operation_error_code(operation_result.error_kind)
    status = (
        ToolExecutionStatus.SUCCESS
        if operation_result.error_kind is None
        else _ERROR_STATUS_AND_CODE[operation_result.error_kind][0]
    )
    certainty = SideEffectCertainty(operation_result.side_effect_state.value)
    side_effect_record = _side_effect_record(
        certainty=certainty,
        error_code=error_code,
        summary=operation_result.model_text,
    )
    structured_data: dict[str, Any] = {
        "model_text": operation_result.model_text,
        "truncated": operation_result.truncated,
    }
    if operation_result.exit_code is not None:
        structured_data["exit_code"] = operation_result.exit_code
    if (
        operation_result.error_kind is None
        and operation_result.changed_path is not None
        and descriptor.tool_id in {"builtin.pi_compat.edit", "builtin.pi_compat.write"}
    ):
        structured_data["changed_path"] = operation_result.changed_path.as_posix()
    return make_tool_result(
        call_id=call.call_id,
        status=status,
        code=error_code,
        summary=operation_result.model_text,
        structured_data=structured_data,
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
        side_effect_certainty=certainty,
        side_effect_record=side_effect_record,
        input_sha256=canonical_sha256(call.arguments),
        retryable=False,
        output_policy=descriptor.output_policy,
    )


def _operation_error_code(
    error_kind: PiCompatErrorKind | None,
) -> ToolExecutionErrorCode | None:
    if error_kind is None:
        return None
    return _ERROR_STATUS_AND_CODE[error_kind][1]


def _side_effect_record(
    *,
    certainty: SideEffectCertainty,
    error_code: ToolExecutionErrorCode | None,
    summary: str,
) -> SideEffectRecord | None:
    if certainty not in _SIDE_EFFECT_RECORD_CERTAINTIES:
        return None
    if error_code is None:
        raise ValueError("uncertain Pi-compatible operation result requires an error")
    return SideEffectRecord(
        certainty=certainty,
        detail_code=error_code.value,
        summary=summary,
        retry_allowed=False,
    )


def _terminal_result(
    call: ValidatedToolCall,
    descriptor: ToolDescriptor,
) -> ToolExecutionResult:
    summary = cast(str, call.arguments["summary"])
    if not summary.strip():
        return make_tool_result(
            call_id=call.call_id,
            status=ToolExecutionStatus.NOT_EXECUTED,
            code=ToolExecutionErrorCode.INVALID_ARGUMENTS,
            summary="terminal summary must not be blank",
            structured_data={
                "model_text": "terminal summary must not be blank",
                "truncated": False,
            },
            side_effect_class=descriptor.side_effect_class,
            idempotency=descriptor.idempotency,
            side_effect_certainty=SideEffectCertainty.NOT_ATTEMPTED,
            input_sha256=canonical_sha256(call.arguments),
            retryable=False,
            output_policy=descriptor.output_policy,
        )
    return make_tool_result(
        call_id=call.call_id,
        status=ToolExecutionStatus.SUCCESS,
        code=None,
        summary=summary,
        structured_data={"model_text": summary, "truncated": False},
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        input_sha256=canonical_sha256(call.arguments),
        retryable=False,
        output_policy=descriptor.output_policy,
    )


def _pre_entry_result(
    call: ValidatedToolCall,
    descriptor: ToolDescriptor,
    code: ToolExecutionErrorCode,
    summary: str,
) -> ToolExecutionResult:
    return make_tool_result(
        call_id=call.call_id,
        status=ToolExecutionStatus.NOT_EXECUTED,
        code=code,
        summary=summary,
        structured_data={"model_text": summary, "truncated": False},
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
        side_effect_certainty=SideEffectCertainty.NOT_ATTEMPTED,
        input_sha256=canonical_sha256(call.arguments),
        retryable=False,
        output_policy=descriptor.output_policy,
    )
