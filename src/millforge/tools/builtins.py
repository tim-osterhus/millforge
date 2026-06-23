"""Descriptor-only production built-in tool catalog data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from millforge import IdempotencyClass, SideEffectClass
from millforge.compiler.catalogs import ToolCatalogSnapshot
from millforge.tools.registry import (
    ToolDescriptor,
    ToolOutputPolicy,
    ToolRegistry,
    ToolTimeoutPolicy,
)

JsonObject = dict[str, Any]

BUILTIN_TOOL_VERSION = 1

BUILTIN_CAP_REQUEST_READ = "request.read"
BUILTIN_CAP_WORKSPACE_READ = "workspace.read"
BUILTIN_CAP_WORKSPACE_SEARCH = "workspace.search"
BUILTIN_CAP_WORKSPACE_WRITE = "workspace.write"
BUILTIN_CAP_WORKSPACE_DIFF_READ = "workspace.diff.read"
BUILTIN_CAP_PROCESS_TEST = "process.test"
BUILTIN_CAP_PROCESS_STATIC_CHECK = "process.static_check"
BUILTIN_CAP_ARTIFACT_READ = "artifact.read"
BUILTIN_CAP_ARTIFACT_WRITE = "artifact.write"
BUILTIN_CAP_TERMINAL_INTENT = "terminal.intent"

BUILTIN_CAPABILITY_IDS = (
    BUILTIN_CAP_ARTIFACT_READ,
    BUILTIN_CAP_ARTIFACT_WRITE,
    BUILTIN_CAP_PROCESS_STATIC_CHECK,
    BUILTIN_CAP_PROCESS_TEST,
    BUILTIN_CAP_REQUEST_READ,
    BUILTIN_CAP_TERMINAL_INTENT,
    BUILTIN_CAP_WORKSPACE_DIFF_READ,
    BUILTIN_CAP_WORKSPACE_READ,
    BUILTIN_CAP_WORKSPACE_SEARCH,
    BUILTIN_CAP_WORKSPACE_WRITE,
)

_DEFAULT_TIMEOUT_POLICY = ToolTimeoutPolicy(
    timeout_seconds=300,
    cancellation_grace_seconds=10,
)
_SHELL_TIMEOUT_POLICY = ToolTimeoutPolicy(
    timeout_seconds=1800,
    cancellation_grace_seconds=30,
)
_TERMINAL_TIMEOUT_POLICY = ToolTimeoutPolicy(
    timeout_seconds=30,
    cancellation_grace_seconds=5,
)
_DEFAULT_OUTPUT_POLICY = ToolOutputPolicy(
    max_output_bytes=1_048_576,
    max_summary_utf8=8192,
    redact_secrets=True,
)

_EMPTY_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
_STATUS_SCHEMA: JsonObject = {
    "type": "string",
    "enum": ["success", "soft_failure", "hard_failure"],
}
_STRING_ARRAY_SCHEMA: JsonObject = {
    "type": "array",
    "items": {"type": "string"},
}
_COMMON_STATUS_SUMMARY_PROPERTIES: JsonObject = {
    "status": _STATUS_SCHEMA,
    "summary": {"type": "string"},
}
_REQUEST_INSPECT_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "request_id": {"type": "string"},
        "stage_id": {"type": "string"},
        "objective": {"type": "string"},
        "artifact_refs": _STRING_ARRAY_SCHEMA,
        "truncated": {"type": "boolean"},
    },
    "required": [
        "status",
        "summary",
        "request_id",
        "stage_id",
        "objective",
        "artifact_refs",
    ],
    "additionalProperties": False,
}
_REQUEST_REQUIREMENTS_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "requirements": _STRING_ARRAY_SCHEMA,
        "artifact_refs": _STRING_ARRAY_SCHEMA,
        "truncated": {"type": "boolean"},
    },
    "required": ["status", "summary", "requirements", "artifact_refs"],
    "additionalProperties": False,
}
_WORKSPACE_LIST_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "root": {"type": "string"},
        "glob": {"type": "string"},
        "max_results": {"type": "integer"},
    },
    "required": ["root"],
    "additionalProperties": False,
}
_WORKSPACE_LIST_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "paths": _STRING_ARRAY_SCHEMA,
        "truncated": {"type": "boolean"},
    },
    "required": ["status", "summary", "paths", "truncated"],
    "additionalProperties": False,
}
_WORKSPACE_READ_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "max_bytes": {"type": "integer"},
    },
    "required": ["path"],
    "additionalProperties": False,
}
_WORKSPACE_READ_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "content": {"type": "string"},
        "truncated": {"type": "boolean"},
        "artifact_refs": _STRING_ARRAY_SCHEMA,
    },
    "required": ["status", "summary", "content", "truncated", "artifact_refs"],
    "additionalProperties": False,
}
_WORKSPACE_SEARCH_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "root": {"type": "string"},
        "glob": {"type": "string"},
        "max_results": {"type": "integer"},
    },
    "required": ["query"],
    "additionalProperties": False,
}
_WORKSPACE_SEARCH_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "snippet": {"type": "string"},
                },
                "required": ["path", "line", "snippet"],
                "additionalProperties": False,
            },
        },
        "truncated": {"type": "boolean"},
    },
    "required": ["status", "summary", "matches", "truncated"],
    "additionalProperties": False,
}
_WORKSPACE_WRITE_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
        "expected_sha256": {"type": "string"},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}
_WORKSPACE_WRITE_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "path": {"type": "string"},
        "content_sha256": {"type": "string"},
    },
    "required": ["status", "summary", "path", "content_sha256"],
    "additionalProperties": False,
}
_WORKSPACE_PATCH_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "patch": {"type": "string"},
        "expected_base_sha256": {"type": "string"},
    },
    "required": ["patch"],
    "additionalProperties": False,
}
_WORKSPACE_PATCH_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "changed_paths": _STRING_ARRAY_SCHEMA,
        "diff_sha256": {"type": "string"},
    },
    "required": ["status", "summary", "changed_paths", "diff_sha256"],
    "additionalProperties": False,
}
_WORKSPACE_DIFF_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "paths": _STRING_ARRAY_SCHEMA,
        "max_bytes": {"type": "integer"},
    },
    "required": [],
    "additionalProperties": False,
}
_WORKSPACE_DIFF_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "diff": {"type": "string"},
        "truncated": {"type": "boolean"},
        "diff_sha256": {"type": "string"},
    },
    "required": ["status", "summary", "diff", "truncated", "diff_sha256"],
    "additionalProperties": False,
}
_SHELL_TEST_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "profile": {"type": "string"},
        "selector": {"type": "string"},
        "max_output_bytes": {"type": "integer"},
    },
    "required": ["profile"],
    "additionalProperties": False,
}
_SHELL_STATIC_CHECK_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "profile": {"type": "string"},
        "selector": {"type": "string"},
        "max_output_bytes": {"type": "integer"},
    },
    "required": ["profile"],
    "additionalProperties": False,
}
_SHELL_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "exit_code": {"type": "integer"},
        "artifact_refs": _STRING_ARRAY_SCHEMA,
        "truncated": {"type": "boolean"},
    },
    "required": ["status", "summary", "exit_code", "artifact_refs", "truncated"],
    "additionalProperties": False,
}
_ARTIFACT_READ_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string"},
        "max_bytes": {"type": "integer"},
    },
    "required": ["artifact_id"],
    "additionalProperties": False,
}
_ARTIFACT_READ_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "artifact_id": {"type": "string"},
        "content": {"type": "string"},
        "content_sha256": {"type": "string"},
        "truncated": {"type": "boolean"},
    },
    "required": [
        "status",
        "summary",
        "artifact_id",
        "content",
        "content_sha256",
        "truncated",
    ],
    "additionalProperties": False,
}


def _fixed_artifact_read_output_schema(artifact_id: str) -> JsonObject:
    return {
        "type": "object",
        "properties": {
            **_COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_id": {"type": "string", "enum": [artifact_id]},
            "content": {"type": "string"},
            "content_sha256": {"type": "string"},
            "truncated": {"type": "boolean"},
        },
        "required": [
            "status",
            "summary",
            "artifact_id",
            "content",
            "content_sha256",
            "truncated",
        ],
        "additionalProperties": False,
    }


_ARTIFACT_READ_PLAN_OUTPUT_SCHEMA = _fixed_artifact_read_output_schema("plan")
_ARTIFACT_READ_PATCH_SUMMARY_OUTPUT_SCHEMA = _fixed_artifact_read_output_schema(
    "patch_summary"
)
_ARTIFACT_READ_TEST_RESULTS_OUTPUT_SCHEMA = _fixed_artifact_read_output_schema(
    "test_results"
)
_ARTIFACT_READ_WORKSPACE_DIFF_OUTPUT_SCHEMA = _fixed_artifact_read_output_schema(
    "workspace_diff"
)
_ARTIFACT_READ_CHECKER_VERDICT_OUTPUT_SCHEMA = _fixed_artifact_read_output_schema(
    "checker_verdict"
)
_ARTIFACT_WRITE_PLAN_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"plan": {"type": "string"}},
    "required": ["plan"],
    "additionalProperties": False,
}
_ARTIFACT_WRITE_PATCH_SUMMARY_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
_ARTIFACT_WRITE_TEST_RESULTS_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"results": {"type": "string"}},
    "required": ["results"],
    "additionalProperties": False,
}
_ARTIFACT_WRITE_CHECKER_VERDICT_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}
_ARTIFACT_WRITE_ARBITER_VERDICT_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}
_ARTIFACT_WRITE_VERDICT_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "artifact_id": {
            "type": "string",
            "enum": ["checker_verdict", "arbiter_verdict"],
        },
        "verdict": {"type": "string"},
    },
    "required": ["artifact_id", "verdict"],
    "additionalProperties": False,
}
_ARTIFACT_WRITE_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "artifact_id": {"type": "string"},
        "content_sha256": {"type": "string"},
    },
    "required": ["status", "summary", "artifact_id", "content_sha256"],
    "additionalProperties": False,
}
_TERMINAL_SUBMIT_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "terminal_result": {"type": "string"},
        "summary": {"type": "string"},
        "artifact_refs": _STRING_ARRAY_SCHEMA,
    },
    "required": ["terminal_result", "summary"],
    "additionalProperties": False,
}
_TERMINAL_REJECT_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "terminal_result": {"type": "string"},
        "summary": {"type": "string"},
        "artifact_refs": _STRING_ARRAY_SCHEMA,
    },
    "required": ["terminal_result", "summary"],
    "additionalProperties": False,
}
_TERMINAL_ESCALATE_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "terminal_result": {"type": "string"},
        "summary": {"type": "string"},
        "blocker": {"type": "string"},
        "artifact_refs": _STRING_ARRAY_SCHEMA,
    },
    "required": ["terminal_result", "summary", "blocker"],
    "additionalProperties": False,
}
_TERMINAL_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        **_COMMON_STATUS_SUMMARY_PROPERTIES,
        "terminal_result": {"type": "string"},
    },
    "required": ["status", "summary", "terminal_result"],
    "additionalProperties": False,
}


def _descriptor(
    *,
    tool_id: str,
    model_tool_name: str,
    description: str,
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    required_capabilities: tuple[str, ...],
    produced_artifact_ids: tuple[str, ...] = (),
    side_effect_class: SideEffectClass,
    idempotency: IdempotencyClass,
    timeout_policy: ToolTimeoutPolicy = _DEFAULT_TIMEOUT_POLICY,
) -> ToolDescriptor:
    return ToolDescriptor.model_validate(
        {
            "tool_id": tool_id,
            "tool_version": BUILTIN_TOOL_VERSION,
            "implementation_id": f"impl.{tool_id}.v1",
            "model_tool_name": model_tool_name,
            "description": description,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "required_capabilities": required_capabilities,
            "produced_artifact_ids": produced_artifact_ids,
            "side_effect_class": side_effect_class,
            "idempotency": idempotency,
            "timeout_policy": timeout_policy,
            "output_policy": _DEFAULT_OUTPUT_POLICY,
        }
    )


BUILTIN_TOOL_DESCRIPTORS: tuple[ToolDescriptor, ...] = (
    _descriptor(
        tool_id="builtin.request.inspect",
        model_tool_name="inspect_request",
        description="Inspect the active request context.",
        input_schema=_EMPTY_INPUT_SCHEMA,
        output_schema=_REQUEST_INSPECT_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_REQUEST_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.request.read_requirements",
        model_tool_name="read_request_requirements",
        description="Read the active request requirements.",
        input_schema=_EMPTY_INPUT_SCHEMA,
        output_schema=_REQUEST_REQUIREMENTS_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_REQUEST_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.workspace.list_files",
        model_tool_name="list_workspace_files",
        description="List workspace-relative files under a root.",
        input_schema=_WORKSPACE_LIST_INPUT_SCHEMA,
        output_schema=_WORKSPACE_LIST_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_WORKSPACE_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.workspace.read_file",
        model_tool_name="read_workspace_file",
        description="Read a workspace-relative file.",
        input_schema=_WORKSPACE_READ_INPUT_SCHEMA,
        output_schema=_WORKSPACE_READ_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_WORKSPACE_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.workspace.search_text",
        model_tool_name="search_workspace_text",
        description="Search workspace-relative text by query.",
        input_schema=_WORKSPACE_SEARCH_INPUT_SCHEMA,
        output_schema=_WORKSPACE_SEARCH_OUTPUT_SCHEMA,
        required_capabilities=(
            BUILTIN_CAP_WORKSPACE_READ,
            BUILTIN_CAP_WORKSPACE_SEARCH,
        ),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.workspace.write_file",
        model_tool_name="write_workspace_file",
        description="Write a workspace-relative file.",
        input_schema=_WORKSPACE_WRITE_INPUT_SCHEMA,
        output_schema=_WORKSPACE_WRITE_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_WORKSPACE_WRITE,),
        side_effect_class=SideEffectClass.WORKSPACE_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.workspace.apply_patch",
        model_tool_name="apply_workspace_patch",
        description="Apply an idempotent workspace patch.",
        input_schema=_WORKSPACE_PATCH_INPUT_SCHEMA,
        output_schema=_WORKSPACE_PATCH_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_WORKSPACE_WRITE,),
        side_effect_class=SideEffectClass.WORKSPACE_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.workspace.read_diff",
        model_tool_name="read_workspace_diff",
        description="Read workspace diff data.",
        input_schema=_WORKSPACE_DIFF_INPUT_SCHEMA,
        output_schema=_WORKSPACE_DIFF_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_WORKSPACE_DIFF_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.shell.run_tests",
        model_tool_name="run_test_profile",
        description="Run a controlled test profile.",
        input_schema=_SHELL_TEST_INPUT_SCHEMA,
        output_schema=_SHELL_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_PROCESS_TEST,),
        side_effect_class=SideEffectClass.PROCESS_EXECUTION,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
        timeout_policy=_SHELL_TIMEOUT_POLICY,
    ),
    _descriptor(
        tool_id="builtin.shell.run_static_check",
        model_tool_name="run_static_check_profile",
        description="Run a controlled static-check profile.",
        input_schema=_SHELL_STATIC_CHECK_INPUT_SCHEMA,
        output_schema=_SHELL_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_PROCESS_STATIC_CHECK,),
        side_effect_class=SideEffectClass.PROCESS_EXECUTION,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
        timeout_policy=_SHELL_TIMEOUT_POLICY,
    ),
    _descriptor(
        tool_id="builtin.artifact.read",
        model_tool_name="read_artifact",
        description="Read a declared artifact.",
        input_schema=_ARTIFACT_READ_INPUT_SCHEMA,
        output_schema=_ARTIFACT_READ_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.artifact.read_plan",
        model_tool_name="read_plan_artifact",
        description="Read the fixed plan artifact.",
        input_schema=_EMPTY_INPUT_SCHEMA,
        output_schema=_ARTIFACT_READ_PLAN_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.artifact.read_patch_summary",
        model_tool_name="read_patch_summary_artifact",
        description="Read the fixed patch summary artifact.",
        input_schema=_EMPTY_INPUT_SCHEMA,
        output_schema=_ARTIFACT_READ_PATCH_SUMMARY_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.artifact.read_test_results",
        model_tool_name="read_test_results_artifact",
        description="Read the fixed test results artifact.",
        input_schema=_EMPTY_INPUT_SCHEMA,
        output_schema=_ARTIFACT_READ_TEST_RESULTS_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.artifact.read_workspace_diff",
        model_tool_name="read_workspace_diff_artifact",
        description="Read the fixed workspace diff artifact.",
        input_schema=_EMPTY_INPUT_SCHEMA,
        output_schema=_ARTIFACT_READ_WORKSPACE_DIFF_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.artifact.read_checker_verdict",
        model_tool_name="read_checker_verdict_artifact",
        description="Read the fixed checker verdict artifact.",
        input_schema=_EMPTY_INPUT_SCHEMA,
        output_schema=_ARTIFACT_READ_CHECKER_VERDICT_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.artifact.write_plan",
        model_tool_name="write_plan_artifact",
        description="Write a plan artifact.",
        input_schema=_ARTIFACT_WRITE_PLAN_INPUT_SCHEMA,
        output_schema=_ARTIFACT_WRITE_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_WRITE,),
        produced_artifact_ids=("plan",),
        side_effect_class=SideEffectClass.ARTIFACT_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.artifact.write_patch_summary",
        model_tool_name="write_patch_summary_artifact",
        description="Write a patch summary artifact.",
        input_schema=_ARTIFACT_WRITE_PATCH_SUMMARY_INPUT_SCHEMA,
        output_schema=_ARTIFACT_WRITE_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_WRITE,),
        produced_artifact_ids=("patch_summary",),
        side_effect_class=SideEffectClass.ARTIFACT_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.artifact.write_test_results",
        model_tool_name="write_test_results_artifact",
        description="Write a test results artifact.",
        input_schema=_ARTIFACT_WRITE_TEST_RESULTS_INPUT_SCHEMA,
        output_schema=_ARTIFACT_WRITE_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_WRITE,),
        produced_artifact_ids=("test_results",),
        side_effect_class=SideEffectClass.ARTIFACT_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.artifact.write_workspace_diff",
        model_tool_name="write_workspace_diff_artifact",
        description="Write the current workspace diff artifact.",
        input_schema=_EMPTY_INPUT_SCHEMA,
        output_schema=_ARTIFACT_WRITE_OUTPUT_SCHEMA,
        required_capabilities=(
            BUILTIN_CAP_WORKSPACE_DIFF_READ,
            BUILTIN_CAP_ARTIFACT_WRITE,
        ),
        produced_artifact_ids=("workspace_diff",),
        side_effect_class=SideEffectClass.ARTIFACT_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.artifact.write_checker_verdict",
        model_tool_name="write_checker_verdict_artifact",
        description="Write a checker verdict artifact.",
        input_schema=_ARTIFACT_WRITE_CHECKER_VERDICT_INPUT_SCHEMA,
        output_schema=_ARTIFACT_WRITE_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_WRITE,),
        produced_artifact_ids=("checker_verdict",),
        side_effect_class=SideEffectClass.ARTIFACT_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.artifact.write_arbiter_verdict",
        model_tool_name="write_arbiter_verdict_artifact",
        description="Write an arbiter verdict artifact.",
        input_schema=_ARTIFACT_WRITE_ARBITER_VERDICT_INPUT_SCHEMA,
        output_schema=_ARTIFACT_WRITE_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_WRITE,),
        produced_artifact_ids=("arbiter_verdict",),
        side_effect_class=SideEffectClass.ARTIFACT_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.artifact.write_verdict",
        model_tool_name="write_verdict_artifact",
        description="Write checker or arbiter verdict artifacts.",
        input_schema=_ARTIFACT_WRITE_VERDICT_INPUT_SCHEMA,
        output_schema=_ARTIFACT_WRITE_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_ARTIFACT_WRITE,),
        produced_artifact_ids=("checker_verdict", "arbiter_verdict"),
        side_effect_class=SideEffectClass.ARTIFACT_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.terminal.submit",
        model_tool_name="submit_terminal_intent",
        description="Submit a terminal result intent.",
        input_schema=_TERMINAL_SUBMIT_INPUT_SCHEMA,
        output_schema=_TERMINAL_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_TERMINAL_INTENT,),
        side_effect_class=SideEffectClass.TERMINAL,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        timeout_policy=_TERMINAL_TIMEOUT_POLICY,
    ),
    _descriptor(
        tool_id="builtin.terminal.reject",
        model_tool_name="reject_terminal_intent",
        description="Submit a terminal rejection intent.",
        input_schema=_TERMINAL_REJECT_INPUT_SCHEMA,
        output_schema=_TERMINAL_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_TERMINAL_INTENT,),
        side_effect_class=SideEffectClass.TERMINAL,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        timeout_policy=_TERMINAL_TIMEOUT_POLICY,
    ),
    _descriptor(
        tool_id="builtin.terminal.escalate",
        model_tool_name="escalate_terminal_intent",
        description="Submit a terminal escalation intent.",
        input_schema=_TERMINAL_ESCALATE_INPUT_SCHEMA,
        output_schema=_TERMINAL_OUTPUT_SCHEMA,
        required_capabilities=(BUILTIN_CAP_TERMINAL_INTENT,),
        side_effect_class=SideEffectClass.TERMINAL,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        timeout_policy=_TERMINAL_TIMEOUT_POLICY,
    ),
)


def iter_builtin_tool_descriptors() -> tuple[ToolDescriptor, ...]:
    """Return the immutable descriptor-only built-in catalog."""
    return BUILTIN_TOOL_DESCRIPTORS


def create_builtin_tool_registry() -> ToolRegistry:
    """Create a registry populated with the descriptor-only built-in catalog."""
    registry = ToolRegistry()
    for descriptor in BUILTIN_TOOL_DESCRIPTORS:
        registry.register(descriptor)
    return registry


def create_builtin_tool_snapshot() -> ToolCatalogSnapshot:
    """Create a frozen exact-version snapshot for the built-in catalog."""
    return create_builtin_tool_registry().freeze()


__all__ = [
    "BUILTIN_CAPABILITY_IDS",
    "BUILTIN_TOOL_DESCRIPTORS",
    "BUILTIN_TOOL_VERSION",
    "create_builtin_tool_registry",
    "create_builtin_tool_snapshot",
    "iter_builtin_tool_descriptors",
]
