"""Explicit runtime implementations for accepted built-in tool bindings."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from millforge import (
    ArtifactRef,
    SideEffectCertainty,
    ToolExecutionStatus,
)
from millforge.compiled_plan import CompiledHarnessPlan
from millforge.contracts import (
    SideEffectRecord,
    TerminalIntent,
    ToolExecutionContext,
    ToolExecutionResult,
    ValidatedToolCall,
)
from millforge.tools.builtins import (
    create_builtin_tool_snapshot,
    iter_builtin_tool_descriptors,
)
from millforge.tools.execution import (
    CompiledToolBindingExecutor,
    RuntimeToolRegistry,
    create_tool_executor,
)
from millforge.tools.path_policy import (
    PathPolicyError,
    atomic_write_contained,
    canonical_sha256_bytes,
    logical_from_resolved,
    resolve_existing_contained,
    resolve_write_contained,
    validate_logical_path,
)
from millforge.tools.results import (
    ToolExecutionErrorCode,
    canonical_sha256,
    make_denial_result,
    make_tool_result,
)

_DEFAULT_MAX_BYTES = 32_768
_MAX_BYTES = 1_048_576
_DEFAULT_MAX_RESULTS = 200
_MAX_RESULTS = 1_000
_ARTIFACT_FILENAMES = {
    "plan": "plan.md",
    "patch_summary": "patch_summary.md",
    "test_results": "test_results.md",
    "checker_verdict": "checker_verdict.md",
    "arbiter_verdict": "arbiter_verdict.md",
}
_ARTIFACT_CONTENT_TYPE = "text/markdown"
_WRITE_TOOL_CONTENT_FIELD = {
    "builtin.artifact.write_plan": ("plan", "plan"),
    "builtin.artifact.write_patch_summary": ("patch_summary", "summary"),
    "builtin.artifact.write_test_results": ("test_results", "results"),
}
_SHELL_TEST_PROFILES = {
    "tool-boundary": (
        "python",
        "-m",
        "pytest",
        "tests/test_tool_execution_boundary.py",
        "tests/test_builtin_tool_catalog.py",
    ),
}
_SHELL_STATIC_CHECK_PROFILES = {
    "tool-boundary-ruff": (
        "python",
        "-m",
        "ruff",
        "check",
        "src/millforge/tools",
        "src/millforge/contracts.py",
        "src/millforge/compiled_plan.py",
        "tests/test_tool_execution_boundary.py",
    ),
    "tool-boundary-mypy": (
        "python",
        "-m",
        "mypy",
        "src/millforge/tools",
        "src/millforge/contracts.py",
        "src/millforge/compiled_plan.py",
    ),
}
_SHELL_ALLOWED_SELECTORS = {None, "", "all"}
_SHELL_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "WINDIR",
)
_SHELL_DEFAULT_TIMEOUT_SECONDS = 60.0
_SHELL_MAX_TIMEOUT_SECONDS = 1800.0


BuiltinImplementation = Callable[
    [ValidatedToolCall, ToolExecutionContext], ToolExecutionResult
]


def create_builtin_runtime_registry() -> RuntimeToolRegistry:
    """Create the explicit source-owned built-in implementation registry."""
    registry = RuntimeToolRegistry()
    implementations: dict[str, BuiltinImplementation] = {
        "builtin.request.inspect": _request_inspect,
        "builtin.request.read_requirements": _request_requirements,
        "builtin.workspace.list_files": _workspace_list_files,
        "builtin.workspace.read_file": _workspace_read_file,
        "builtin.workspace.search_text": _workspace_search_text,
        "builtin.workspace.write_file": _workspace_write_file,
        "builtin.workspace.apply_patch": _workspace_apply_patch,
        "builtin.workspace.read_diff": _workspace_read_diff,
        "builtin.shell.run_tests": _shell_run_profile,
        "builtin.shell.run_static_check": _shell_run_profile,
        "builtin.artifact.read": _artifact_read,
        "builtin.artifact.write_plan": _artifact_write,
        "builtin.artifact.write_patch_summary": _artifact_write,
        "builtin.artifact.write_test_results": _artifact_write,
        "builtin.artifact.write_verdict": _artifact_write,
        "builtin.terminal.submit": _terminal_intent,
        "builtin.terminal.reject": _terminal_intent,
        "builtin.terminal.escalate": _terminal_intent,
    }
    for descriptor in iter_builtin_tool_descriptors():
        implementation = implementations.get(descriptor.tool_id, _not_implemented)
        registry.register(descriptor.implementation_id, implementation)
    return registry


def create_builtin_tool_executor(
    plan: CompiledHarnessPlan,
) -> CompiledToolBindingExecutor:
    """Create a built-in executor admitted by the compiled plan and 04B snapshot."""
    return create_tool_executor(
        plan=plan,
        descriptor_snapshot=create_builtin_tool_snapshot(),
        runtime_registry=create_builtin_runtime_registry(),
    )


def validate_builtin_pre_entry_policy(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
    *,
    input_sha256: str | None = None,
) -> ToolExecutionResult | None:
    """Return a denial result when built-in side-effect policy rejects a call.

    The validator intentionally performs no writes, process launches, terminal
    handoff, queue/status mutation, or network access. Production built-in
    implementations keep their internal checks as a second line of defense.
    """
    input_digest = input_sha256 or canonical_sha256(call.arguments)
    try:
        error = _builtin_policy_error(call, context)
    except (OSError, PathPolicyError, ValueError) as exc:
        error = _policy_error(_policy_error_code(call), str(exc))
    if error is None:
        return None
    descriptor = _descriptor_for_call(call)
    return make_denial_result(
        call_id=call.call_id,
        code=error.code,
        summary=error.summary,
        evidence={
            "tool_id": call.binding.tool_id,
            "node_id": call.node_id,
            "policy": error.policy,
        },
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
        input_sha256=input_digest,
    )


def _request_inspect(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    artifact_refs = [ref.artifact_id for ref in context.input_artifacts]
    work_item = context.work_item_id or ""
    objective = work_item[:512]
    output = {
        "status": "success",
        "summary": "request context inspected",
        "request_id": context.request_id,
        "stage_id": context.stage.stage_kind_id,
        "objective": objective,
        "artifact_refs": artifact_refs[:_DEFAULT_MAX_RESULTS],
        "truncated": len(artifact_refs) > _DEFAULT_MAX_RESULTS,
    }
    return _success(call, output)


def _request_requirements(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    requirements = [
        f"capability:{grant.capability_id}"
        for grant in context.capability_envelope.grants
    ]
    policy = context.compiled_artifact_policy
    if policy is not None:
        requirements.extend(
            f"artifact:{artifact_id}" for artifact_id in policy.declared_artifact_ids
        )
        for item in policy.required_by_terminal:
            joined = ",".join(item.artifact_ids)
            requirements.append(f"terminal:{item.terminal_result}:artifacts:{joined}")
    artifact_refs = [ref.artifact_id for ref in context.input_artifacts]
    truncated = (
        len(requirements) > _MAX_RESULTS or len(artifact_refs) > _DEFAULT_MAX_RESULTS
    )
    output = {
        "status": "success",
        "summary": "request requirements inspected",
        "requirements": requirements[:_MAX_RESULTS],
        "artifact_refs": artifact_refs[:_DEFAULT_MAX_RESULTS],
        "truncated": truncated,
    }
    return _success(call, output)


def _workspace_list_files(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    root = _workspace_root(context)
    if root is None:
        return _denied(
            call, ToolExecutionErrorCode.POLICY_DENIED, "workspace root is unavailable"
        )
    max_results = _bounded_int(
        call.arguments.get("max_results"), _DEFAULT_MAX_RESULTS, _MAX_RESULTS
    )
    try:
        base = resolve_existing_contained(
            root, str(call.arguments["root"]), allow_dot=True
        )
        pattern = str(call.arguments.get("glob") or "*")
        paths: list[str] = []
        truncated = False
        for item in sorted(base.rglob("*")):
            if not item.is_file():
                continue
            try:
                logical = logical_from_resolved(root, item)
            except PathPolicyError:
                continue
            if fnmatch.fnmatch(logical, pattern) or fnmatch.fnmatch(item.name, pattern):
                paths.append(logical)
                if len(paths) >= max_results:
                    truncated = True
                    break
    except (OSError, PathPolicyError) as exc:
        return _denied(call, ToolExecutionErrorCode.POLICY_DENIED, str(exc))
    return _success(
        call,
        {
            "status": "success",
            "summary": f"listed {len(paths)} workspace file(s)",
            "paths": paths,
            "truncated": truncated,
        },
    )


def _workspace_read_file(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    root = _workspace_root(context)
    if root is None:
        return _denied(
            call, ToolExecutionErrorCode.POLICY_DENIED, "workspace root is unavailable"
        )
    max_bytes = _bounded_int(
        call.arguments.get("max_bytes"), _DEFAULT_MAX_BYTES, _MAX_BYTES
    )
    try:
        target = resolve_existing_contained(root, str(call.arguments["path"]))
        if not target.is_file():
            return _denied(
                call, ToolExecutionErrorCode.NOT_FOUND, "workspace file not found"
            )
        data = target.read_bytes()
    except (OSError, PathPolicyError) as exc:
        return _denied(call, ToolExecutionErrorCode.POLICY_DENIED, str(exc))
    content, truncated = _decode_bounded(data, max_bytes)
    return _success(
        call,
        {
            "status": "success",
            "summary": "workspace file read",
            "content": content,
            "truncated": truncated,
            "artifact_refs": [],
        },
    )


def _workspace_search_text(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    root = _workspace_root(context)
    if root is None:
        return _denied(
            call, ToolExecutionErrorCode.POLICY_DENIED, "workspace root is unavailable"
        )
    query = str(call.arguments["query"])
    max_results = _bounded_int(
        call.arguments.get("max_results"), _DEFAULT_MAX_RESULTS, _MAX_RESULTS
    )
    try:
        base = resolve_existing_contained(
            root, str(call.arguments.get("root") or "."), allow_dot=True
        )
        pattern = str(call.arguments.get("glob") or "*")
        matches: list[dict[str, Any]] = []
        truncated = False
        for item in sorted(base.rglob("*")):
            if not item.is_file():
                continue
            try:
                logical = logical_from_resolved(root, item)
            except PathPolicyError:
                continue
            if not (
                fnmatch.fnmatch(logical, pattern) or fnmatch.fnmatch(item.name, pattern)
            ):
                continue
            try:
                lines = item.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append(
                        {
                            "path": logical,
                            "line": line_number,
                            "snippet": line[:512],
                        }
                    )
                    if len(matches) >= max_results:
                        truncated = True
                        raise StopIteration
    except StopIteration:
        pass
    except (OSError, PathPolicyError) as exc:
        return _denied(call, ToolExecutionErrorCode.POLICY_DENIED, str(exc))
    return _success(
        call,
        {
            "status": "success",
            "summary": f"found {len(matches)} match(es)",
            "matches": matches,
            "truncated": truncated,
        },
    )


def _workspace_write_file(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    root = _workspace_root(context)
    if root is None:
        return _denied(
            call, ToolExecutionErrorCode.POLICY_DENIED, "workspace root is unavailable"
        )
    path = str(call.arguments["path"])
    content = str(call.arguments["content"]).encode("utf-8")
    try:
        target = resolve_write_contained(root, path)
        expected = call.arguments.get("expected_sha256")
        if expected is not None and target.exists():
            actual = canonical_sha256_bytes(target.read_bytes())
            if actual != expected:
                return _denied(
                    call,
                    ToolExecutionErrorCode.CONFLICT,
                    "expected_sha256 does not match current file",
                )
        digest = atomic_write_contained(root, path, content)
    except (OSError, PathPolicyError) as exc:
        return _denied(call, ToolExecutionErrorCode.POLICY_DENIED, str(exc))
    return _success(
        call,
        {
            "status": "success",
            "summary": "workspace file written atomically",
            "path": validate_logical_path(path).as_posix(),
            "content_sha256": digest,
        },
    )


def _workspace_apply_patch(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    root = _workspace_root(context)
    if root is None:
        return _denied(
            call, ToolExecutionErrorCode.POLICY_DENIED, "workspace root is unavailable"
        )
    patch = str(call.arguments["patch"])
    try:
        changed_paths = _changed_paths_from_patch(root, patch)
        before = _git_diff(root, changed_paths, _MAX_BYTES)
        expected = call.arguments.get("expected_base_sha256")
        if expected is not None and canonical_sha256(before) != expected:
            return _denied(
                call,
                ToolExecutionErrorCode.CONFLICT,
                "expected_base_sha256 does not match current diff",
            )
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=root,
            input=patch.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        diff = _git_diff(root, changed_paths, _MAX_BYTES)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace")[:512]
        return _denied(
            call, ToolExecutionErrorCode.CONFLICT, detail or "patch rejected"
        )
    except (OSError, PathPolicyError, ValueError) as exc:
        return _denied(call, ToolExecutionErrorCode.POLICY_DENIED, str(exc))
    return _success(
        call,
        {
            "status": "success",
            "summary": f"applied patch touching {len(changed_paths)} path(s)",
            "changed_paths": changed_paths,
            "diff_sha256": canonical_sha256(diff),
        },
    )


def _workspace_read_diff(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    root = _workspace_root(context)
    if root is None:
        return _denied(
            call, ToolExecutionErrorCode.POLICY_DENIED, "workspace root is unavailable"
        )
    max_bytes = _bounded_int(
        call.arguments.get("max_bytes"), _DEFAULT_MAX_BYTES, _MAX_BYTES
    )
    try:
        paths = [
            validate_logical_path(str(item)).as_posix()
            for item in call.arguments.get("paths", [])
        ]
        diff = _git_diff(root, paths, max_bytes)
    except (OSError, PathPolicyError) as exc:
        return _denied(call, ToolExecutionErrorCode.POLICY_DENIED, str(exc))
    encoded = diff.encode("utf-8")
    truncated = len(encoded) > max_bytes
    if truncated:
        diff = encoded[:max_bytes].decode("utf-8", errors="ignore") + "[truncated]"
    return _success(
        call,
        {
            "status": "success",
            "summary": "workspace diff read",
            "diff": diff,
            "truncated": truncated,
            "diff_sha256": canonical_sha256(diff),
        },
    )


def _artifact_read(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    artifact_id = str(call.arguments["artifact_id"])
    max_bytes = _bounded_int(
        call.arguments.get("max_bytes"), _DEFAULT_MAX_BYTES, _MAX_BYTES
    )
    error = _artifact_policy_error(context, artifact_id, produced_ids=None)
    if error is not None:
        return _denied(call, ToolExecutionErrorCode.POLICY_DENIED, error)
    ref = _artifact_ref(artifact_id)
    try:
        path = _artifact_logical_path(artifact_id)
        root = _artifact_root(context)
        if root is None:
            return _denied(
                call,
                ToolExecutionErrorCode.POLICY_DENIED,
                "artifact root is unavailable",
            )
        target = resolve_existing_contained(root, path)
        data = target.read_bytes()
    except (OSError, PathPolicyError) as exc:
        return _denied(call, ToolExecutionErrorCode.POLICY_DENIED, str(exc))
    content, truncated = _decode_bounded(data, max_bytes)
    return _success(
        call,
        {
            "status": "success",
            "summary": "artifact read",
            "artifact_id": artifact_id,
            "content": content,
            "content_sha256": canonical_sha256_bytes(data),
            "truncated": truncated,
        },
        artifact_refs=(ref,),
    )


def _artifact_write(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    root = _artifact_root(context)
    if root is None:
        return _denied(
            call, ToolExecutionErrorCode.POLICY_DENIED, "artifact root is unavailable"
        )
    if call.binding.tool_id == "builtin.artifact.write_verdict":
        artifact_id = str(call.arguments["artifact_id"])
        content = str(call.arguments["verdict"])
    else:
        artifact_id, field = _WRITE_TOOL_CONTENT_FIELD[call.binding.tool_id]
        content = str(call.arguments[field])
    error = _artifact_policy_error(
        context, artifact_id, produced_ids=call_node_produced_ids(call)
    )
    if error is not None:
        return _denied(call, ToolExecutionErrorCode.POLICY_DENIED, error)
    ref = _artifact_ref(artifact_id)
    try:
        digest = atomic_write_contained(
            root, _artifact_logical_path(artifact_id), content.encode("utf-8")
        )
    except (OSError, PathPolicyError) as exc:
        return _denied(call, ToolExecutionErrorCode.POLICY_DENIED, str(exc))
    return _success(
        call,
        {
            "status": "success",
            "summary": "artifact written atomically",
            "artifact_id": artifact_id,
            "content_sha256": digest,
        },
        artifact_refs=(ref,),
    )


def _shell_run_profile(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    root = _workspace_root(context)
    if root is None:
        return _denied(
            call, ToolExecutionErrorCode.POLICY_DENIED, "workspace root is unavailable"
        )
    if not root.is_dir():
        return _denied(
            call,
            ToolExecutionErrorCode.POLICY_DENIED,
            "workspace root is not a directory",
        )
    selector = call.arguments.get("selector")
    if selector not in _SHELL_ALLOWED_SELECTORS:
        return _denied(
            call,
            ToolExecutionErrorCode.INVALID_ARGUMENTS,
            "selector is not an approved shell profile selector",
        )
    profile = str(call.arguments["profile"])
    command = _shell_profile_command(call.binding.tool_id, profile)
    if command is None:
        return _denied(
            call,
            ToolExecutionErrorCode.INVALID_ARGUMENTS,
            "profile is not an approved shell profile",
        )
    max_bytes = _bounded_int(
        call.arguments.get("max_output_bytes"), _DEFAULT_MAX_BYTES, _MAX_BYTES
    )
    timeout_seconds = min(
        context.timeout.timeout_seconds,
        _descriptor_for_call(call).timeout_policy.timeout_seconds,
        _SHELL_MAX_TIMEOUT_SECONDS,
    )
    if timeout_seconds <= 0:
        timeout_seconds = _SHELL_DEFAULT_TIMEOUT_SECONDS
    env = _shell_environment()
    try:
        completed = subprocess.run(
            list(command),
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        output, truncated = _join_process_output(exc.stdout, exc.stderr, max_bytes)
        return _process_failure(
            call,
            ToolExecutionStatus.TIMED_OUT,
            ToolExecutionErrorCode.TIMEOUT,
            f"shell profile timed out: {output}",
            -1,
            truncated,
            SideEffectCertainty.COMPLETION_UNKNOWN,
        )
    except OSError as exc:
        return _process_failure(
            call,
            ToolExecutionStatus.HARD_FAILURE,
            ToolExecutionErrorCode.IMPLEMENTATION_ERROR,
            f"shell profile failed to start: {type(exc).__name__}",
            -1,
            False,
            SideEffectCertainty.NOT_ATTEMPTED,
        )
    output, truncated = _join_process_output(
        completed.stdout, completed.stderr, max_bytes
    )
    if completed.returncode == 0:
        return _success(
            call,
            {
                "status": "success",
                "summary": f"shell profile completed successfully: {output}",
                "exit_code": completed.returncode,
                "artifact_refs": [],
                "truncated": truncated,
            },
        )
    return _process_failure(
        call,
        ToolExecutionStatus.SOFT_FAILURE,
        ToolExecutionErrorCode.POLICY_DENIED,
        f"shell profile failed with exit code {completed.returncode}: {output}",
        completed.returncode,
        truncated,
        SideEffectCertainty.CONFIRMED_COMPLETE,
    )


def _terminal_intent(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    terminal_result = str(call.arguments["terminal_result"])
    try:
        missing = _missing_required_terminal_artifacts(call, context, terminal_result)
        if missing:
            return _denied(
                call,
                ToolExecutionErrorCode.TERMINAL_INTENT_INVALID,
                "terminal intent is missing required terminal artifacts",
            )
        summary = str(call.arguments["summary"])
        artifact_refs = _terminal_artifact_refs(call, context)
        TerminalIntent(
            request_id=context.request_id,
            run_id=context.run_id,
            stage=context.stage,
            terminal_node_id=call.node_id,
            terminal_result=terminal_result,
            disposition=_terminal_disposition(call),
            summary=summary,
            artifact_refs=artifact_refs,
        )
    except (PathPolicyError, ValueError) as exc:
        return _denied(call, ToolExecutionErrorCode.TERMINAL_INTENT_INVALID, str(exc))
    return _success(
        call,
        {
            "status": "success",
            "summary": "terminal intent validated",
            "terminal_result": terminal_result,
        },
        artifact_refs=artifact_refs,
    )


def call_node_produced_ids(call: ValidatedToolCall) -> tuple[str, ...]:
    """Return produced IDs admitted by descriptor binding for artifact write tools."""
    for descriptor in iter_builtin_tool_descriptors():
        if descriptor.tool_id == call.binding.tool_id:
            return descriptor.produced_artifact_ids
    return ()


def _not_implemented(
    call: ValidatedToolCall,
    _context: ToolExecutionContext,
) -> ToolExecutionResult:
    return _result(
        call,
        ToolExecutionStatus.SOFT_FAILURE,
        ToolExecutionErrorCode.IMPLEMENTATION_ERROR,
        "built-in runtime implementation is not wired in this process",
        {
            "status": "soft_failure",
            "summary": "built-in runtime implementation is not wired in this process",
        },
        SideEffectCertainty.NOT_ATTEMPTED,
    )


def _success(
    call: ValidatedToolCall,
    output: dict[str, Any],
    *,
    artifact_refs: tuple[ArtifactRef, ...] = (),
) -> ToolExecutionResult:
    return _result(
        call,
        ToolExecutionStatus.SUCCESS,
        None,
        str(output["summary"]),
        output,
        SideEffectCertainty.CONFIRMED_COMPLETE,
        artifact_refs=artifact_refs,
    )


def _denied(
    call: ValidatedToolCall,
    code: ToolExecutionErrorCode,
    summary: str,
) -> ToolExecutionResult:
    output = _failure_output(call, summary)
    return _result(
        call,
        ToolExecutionStatus.NOT_EXECUTED,
        code,
        summary,
        output,
        SideEffectCertainty.NOT_ATTEMPTED,
    )


def _result(
    call: ValidatedToolCall,
    status: ToolExecutionStatus,
    code: ToolExecutionErrorCode | None,
    summary: str,
    output: dict[str, Any],
    certainty: SideEffectCertainty,
    *,
    artifact_refs: tuple[ArtifactRef, ...] = (),
    side_effect_record: SideEffectRecord | None = None,
) -> ToolExecutionResult:
    descriptor = _descriptor_for_call(call)
    return make_tool_result(
        call_id=call.call_id,
        status=status,
        code=code,
        summary=summary,
        structured_data=output,
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
        side_effect_certainty=certainty,
        input_sha256=canonical_sha256(call.arguments),
        artifact_refs=artifact_refs,
        side_effect_record=side_effect_record,
    )


def _descriptor_for_call(call: ValidatedToolCall) -> Any:
    for descriptor in iter_builtin_tool_descriptors():
        if descriptor.implementation_id == call.binding.implementation_id:
            return descriptor
    raise KeyError(call.binding.implementation_id)


def _failure_output(call: ValidatedToolCall, summary: str) -> dict[str, Any]:
    common = {"status": "soft_failure", "summary": summary}
    tool_id = call.binding.tool_id
    if tool_id == "builtin.request.inspect":
        return {
            **common,
            "request_id": "",
            "stage_id": "",
            "objective": "",
            "artifact_refs": [],
            "truncated": False,
        }
    if tool_id == "builtin.request.read_requirements":
        return {**common, "requirements": [], "artifact_refs": [], "truncated": False}
    if tool_id == "builtin.workspace.list_files":
        return {**common, "paths": [], "truncated": False}
    if tool_id == "builtin.workspace.read_file":
        return {**common, "content": "", "truncated": False, "artifact_refs": []}
    if tool_id == "builtin.workspace.search_text":
        return {**common, "matches": [], "truncated": False}
    if tool_id == "builtin.workspace.write_file":
        return {**common, "path": "", "content_sha256": "0" * 64}
    if tool_id == "builtin.workspace.apply_patch":
        return {**common, "changed_paths": [], "diff_sha256": "0" * 64}
    if tool_id == "builtin.workspace.read_diff":
        return {**common, "diff": "", "truncated": False, "diff_sha256": "0" * 64}
    if tool_id.startswith("builtin.shell."):
        return {**common, "exit_code": -1, "artifact_refs": [], "truncated": False}
    if tool_id == "builtin.artifact.read":
        return {
            **common,
            "artifact_id": str(call.arguments.get("artifact_id", "")),
            "content": "",
            "content_sha256": "0" * 64,
            "truncated": False,
        }
    if tool_id.startswith("builtin.artifact.write_"):
        artifact_id = str(call.arguments.get("artifact_id") or "")
        if not artifact_id:
            artifact_id = _WRITE_TOOL_CONTENT_FIELD.get(tool_id, ("", ""))[0]
        return {**common, "artifact_id": artifact_id, "content_sha256": "0" * 64}
    if tool_id.startswith("builtin.terminal."):
        return {
            **common,
            "terminal_result": str(call.arguments.get("terminal_result", "")),
        }
    return common


class _BuiltinPolicyError:
    def __init__(
        self,
        code: ToolExecutionErrorCode,
        summary: str,
        policy: str,
    ) -> None:
        self.code = code
        self.summary = summary
        self.policy = policy


def _policy_error(
    code: ToolExecutionErrorCode,
    summary: str,
    *,
    policy: str = "built-in side-effect policy",
) -> _BuiltinPolicyError:
    return _BuiltinPolicyError(code, summary, policy)


def _policy_error_code(call: ValidatedToolCall) -> ToolExecutionErrorCode:
    return (
        ToolExecutionErrorCode.TERMINAL_INTENT_INVALID
        if call.binding.tool_id.startswith("builtin.terminal.")
        else ToolExecutionErrorCode.POLICY_DENIED
    )


def _builtin_policy_error(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> _BuiltinPolicyError | None:
    tool_id = call.binding.tool_id
    if tool_id.startswith("builtin.workspace."):
        return _workspace_policy_error(call, context)
    if tool_id.startswith("builtin.artifact."):
        return _artifact_pre_entry_policy_error(call, context)
    if tool_id.startswith("builtin.shell."):
        return _shell_policy_error(call, context)
    if tool_id.startswith("builtin.terminal."):
        return _terminal_policy_error(call, context)
    return None


def _workspace_policy_error(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> _BuiltinPolicyError | None:
    root = _workspace_root(context)
    if root is None:
        return _policy_error(
            ToolExecutionErrorCode.POLICY_DENIED, "workspace root is unavailable"
        )
    tool_id = call.binding.tool_id
    if tool_id == "builtin.workspace.list_files":
        resolve_existing_contained(root, str(call.arguments["root"]), allow_dot=True)
    elif tool_id == "builtin.workspace.read_file":
        target = resolve_existing_contained(root, str(call.arguments["path"]))
        if not target.is_file():
            return _policy_error(
                ToolExecutionErrorCode.NOT_FOUND, "workspace file not found"
            )
    elif tool_id == "builtin.workspace.search_text":
        resolve_existing_contained(
            root, str(call.arguments.get("root") or "."), allow_dot=True
        )
    elif tool_id == "builtin.workspace.write_file":
        target = resolve_write_contained(root, str(call.arguments["path"]))
        expected = call.arguments.get("expected_sha256")
        if expected is not None and target.exists():
            actual = canonical_sha256_bytes(target.read_bytes())
            if actual != expected:
                return _policy_error(
                    ToolExecutionErrorCode.CONFLICT,
                    "expected_sha256 does not match current file",
                )
    elif tool_id == "builtin.workspace.apply_patch":
        _changed_paths_from_patch(root, str(call.arguments["patch"]))
    elif tool_id == "builtin.workspace.read_diff":
        if not root.is_dir():
            return _policy_error(
                ToolExecutionErrorCode.POLICY_DENIED,
                "workspace root is unavailable",
            )
        for item in call.arguments.get("paths", []):
            validate_logical_path(str(item))
    return None


def _artifact_pre_entry_policy_error(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> _BuiltinPolicyError | None:
    root = _artifact_root(context)
    if root is None:
        return _policy_error(
            ToolExecutionErrorCode.POLICY_DENIED, "artifact root is unavailable"
        )
    root.resolve(strict=True)
    if call.binding.tool_id == "builtin.artifact.read":
        artifact_id = str(call.arguments["artifact_id"])
        error = _artifact_policy_error(context, artifact_id, produced_ids=None)
        if error is not None:
            return _policy_error(ToolExecutionErrorCode.POLICY_DENIED, error)
        resolve_existing_contained(root, _artifact_logical_path(artifact_id))
        return None
    if call.binding.tool_id == "builtin.artifact.write_verdict":
        artifact_id = str(call.arguments["artifact_id"])
    else:
        artifact_id = _WRITE_TOOL_CONTENT_FIELD[call.binding.tool_id][0]
    error = _artifact_policy_error(
        context, artifact_id, produced_ids=call_node_produced_ids(call)
    )
    if error is not None:
        return _policy_error(ToolExecutionErrorCode.POLICY_DENIED, error)
    resolve_write_contained(root, _artifact_logical_path(artifact_id))
    return None


def _shell_policy_error(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> _BuiltinPolicyError | None:
    root = _workspace_root(context)
    if root is None:
        return _policy_error(
            ToolExecutionErrorCode.POLICY_DENIED, "workspace root is unavailable"
        )
    if not root.is_dir():
        return _policy_error(
            ToolExecutionErrorCode.POLICY_DENIED,
            "workspace root is not a directory",
        )
    selector = call.arguments.get("selector")
    if selector not in _SHELL_ALLOWED_SELECTORS:
        return _policy_error(
            ToolExecutionErrorCode.INVALID_ARGUMENTS,
            "selector is not an approved shell profile selector",
        )
    profile = str(call.arguments["profile"])
    if _shell_profile_command(call.binding.tool_id, profile) is None:
        return _policy_error(
            ToolExecutionErrorCode.INVALID_ARGUMENTS,
            "profile is not an approved shell profile",
        )
    timeout_seconds = min(
        context.timeout.timeout_seconds,
        _descriptor_for_call(call).timeout_policy.timeout_seconds,
        _SHELL_MAX_TIMEOUT_SECONDS,
    )
    if timeout_seconds <= 0:
        return _policy_error(
            ToolExecutionErrorCode.TIMEOUT, "shell timeout policy is exhausted"
        )
    return None


def _terminal_policy_error(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> _BuiltinPolicyError | None:
    terminal_result = str(call.arguments["terminal_result"])
    missing = _missing_required_terminal_artifacts(call, context, terminal_result)
    if missing:
        return _policy_error(
            ToolExecutionErrorCode.TERMINAL_INTENT_INVALID,
            "terminal intent is missing required terminal artifacts",
            policy="terminal artifact policy",
        )
    _terminal_artifact_refs(call, context)
    TerminalIntent(
        request_id=context.request_id,
        run_id=context.run_id,
        stage=context.stage,
        terminal_node_id=call.node_id,
        terminal_result=terminal_result,
        disposition=_terminal_disposition(call),
        summary=str(call.arguments["summary"]),
        artifact_refs=_terminal_artifact_refs(call, context),
    )
    return None


def _shell_profile_command(tool_id: str, profile: str) -> tuple[str, ...] | None:
    profiles = (
        _SHELL_TEST_PROFILES
        if tool_id == "builtin.shell.run_tests"
        else _SHELL_STATIC_CHECK_PROFILES
    )
    return profiles.get(profile)


def _shell_environment() -> dict[str, str]:
    env = {
        key: value
        for key in _SHELL_ENV_ALLOWLIST
        if (value := os.environ.get(key)) is not None
    }
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


def _join_process_output(stdout: Any, stderr: Any, max_bytes: int) -> tuple[str, bool]:
    out = stdout or b""
    err = stderr or b""
    if isinstance(out, str):
        out = out.encode("utf-8", errors="replace")
    if isinstance(err, str):
        err = err.encode("utf-8", errors="replace")
    data = out + (b"\n" if out and err else b"") + err
    content, truncated = _decode_bounded(data, max_bytes)
    return content.strip() or "[no output]", truncated


def _process_failure(
    call: ValidatedToolCall,
    status: ToolExecutionStatus,
    code: ToolExecutionErrorCode,
    summary: str,
    exit_code: int,
    truncated: bool,
    certainty: SideEffectCertainty,
) -> ToolExecutionResult:
    side_effect_record = None
    if certainty is SideEffectCertainty.COMPLETION_UNKNOWN:
        side_effect_record = SideEffectRecord(
            certainty=certainty,
            detail_code=ToolExecutionErrorCode.AMBIGUOUS_SIDE_EFFECT.value,
            summary="process completion could not be proven after timeout",
            retry_allowed=False,
        )
    return _result(
        call,
        status,
        code,
        summary,
        {
            "status": "soft_failure",
            "summary": summary,
            "exit_code": exit_code,
            "artifact_refs": [],
            "truncated": truncated,
        },
        certainty,
        side_effect_record=side_effect_record,
    )


def _terminal_disposition(
    call: ValidatedToolCall,
) -> Literal["success", "blocked", "rejected", "escalated"]:
    if call.binding.tool_id == "builtin.terminal.reject":
        return "rejected"
    if call.binding.tool_id == "builtin.terminal.escalate":
        return "escalated"
    terminal_result = str(call.arguments["terminal_result"])
    return (
        "blocked"
        if terminal_result.endswith("_BLOCKED") or terminal_result == "BLOCKED"
        else "success"
    )


def _missing_required_terminal_artifacts(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
    terminal_result: str,
) -> list[str]:
    policy = context.compiled_artifact_policy
    if policy is None:
        return []
    provided = set(_terminal_artifact_ids(call, context))
    required = {
        artifact_id
        for requirement in policy.required_by_terminal
        if requirement.terminal_result == terminal_result
        for artifact_id in requirement.artifact_ids
    }
    return sorted(required - provided)


def _terminal_artifact_ids(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> tuple[str, ...]:
    explicit = call.arguments.get("artifact_refs")
    if explicit is None:
        return tuple(ref.artifact_id for ref in context.input_artifacts)
    refs_by_id = {ref.artifact_id: ref for ref in context.input_artifacts}
    ids: list[str] = []
    for item in explicit:
        artifact_id = str(item)
        if artifact_id not in refs_by_id:
            raise ValueError(
                f"terminal artifact_ref {artifact_id!r} is not provided by the runtime"
            )
        ids.append(artifact_id)
    return tuple(ids)


def _terminal_artifact_refs(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
) -> tuple[ArtifactRef, ...]:
    ids = _terminal_artifact_ids(call, context)
    refs_by_id = {ref.artifact_id: ref for ref in context.input_artifacts}
    return tuple(refs_by_id[artifact_id] for artifact_id in ids)


def _workspace_root(context: ToolExecutionContext) -> Path | None:
    return None if context.workspace_root is None else Path(context.workspace_root)


def _artifact_root(context: ToolExecutionContext) -> Path | None:
    return None if context.artifact_root is None else Path(context.artifact_root)


def _artifact_policy_error(
    context: ToolExecutionContext,
    artifact_id: str,
    *,
    produced_ids: tuple[str, ...] | None,
) -> str | None:
    policy = context.compiled_artifact_policy
    if policy is None:
        return "compiled artifact policy is unavailable"
    if artifact_id not in policy.declared_artifact_ids:
        return "artifact_id is not declared by the compiled artifact policy"
    if produced_ids is not None and artifact_id not in produced_ids:
        return "artifact_id is not produced by this compiled tool binding"
    if artifact_id not in _ARTIFACT_FILENAMES:
        return "artifact_id has no runtime-managed artifact path"
    return None


def _artifact_logical_path(artifact_id: str) -> str:
    try:
        return _ARTIFACT_FILENAMES[artifact_id]
    except KeyError as exc:
        raise PathPolicyError(
            "artifact_id has no runtime-managed artifact path"
        ) from exc


def _artifact_ref(artifact_id: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        path=Path("millforge") / _artifact_logical_path(artifact_id),
        content_type=_ARTIFACT_CONTENT_TYPE,
    )


def _bounded_int(value: Any, default: int, maximum: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return default
    return min(value, maximum)


def _decode_bounded(data: bytes, max_bytes: int) -> tuple[str, bool]:
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    content = data.decode("utf-8", errors="replace")
    if truncated:
        content += "[truncated]"
    return content, truncated


def _changed_paths_from_patch(root: Path, patch: str) -> list[str]:
    changed: set[str] = set()
    for line in patch.splitlines():
        if not (
            line.startswith("--- ")
            or line.startswith("+++ ")
            or line.startswith("rename from ")
            or line.startswith("rename to ")
        ):
            continue
        raw = line.split(maxsplit=1)[1].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        logical = validate_logical_path(raw).as_posix()
        resolve_write_contained(root, logical)
        changed.add(logical)
    if not changed:
        raise ValueError("patch does not declare any changed paths")
    return sorted(changed)


def _git_diff(root: Path, paths: list[str], max_bytes: int) -> str:
    command = ["git", "diff", "--"]
    command.extend(paths)
    completed = subprocess.run(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    data = completed.stdout[: max_bytes + 1]
    return data.decode("utf-8", errors="replace")
