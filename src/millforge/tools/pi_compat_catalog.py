"""Descriptor-only Pi-compatible unrestricted tool catalog data."""

from __future__ import annotations

import hashlib
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

PI_COMPAT_TOOL_VERSION = 1
DEFAULT_BASE_TERMINAL_RESULTS: tuple[str, ...] = ("BLOCKED", "COMPLETE", "REJECTED")

PI_COMPAT_CAP_FILESYSTEM_READ = "unrestricted.filesystem.read"
PI_COMPAT_CAP_FILESYSTEM_WRITE = "unrestricted.filesystem.write"
PI_COMPAT_CAP_PROCESS_EXECUTE = "unrestricted.process.execute"
PI_COMPAT_CAP_TERMINAL_INTENT = "terminal.intent"

PI_COMPAT_CAPABILITY_IDS = (
    PI_COMPAT_CAP_FILESYSTEM_READ,
    PI_COMPAT_CAP_FILESYSTEM_WRITE,
    PI_COMPAT_CAP_PROCESS_EXECUTE,
    PI_COMPAT_CAP_TERMINAL_INTENT,
)

_PI_TIMEOUT_POLICY = ToolTimeoutPolicy(
    timeout_seconds=1800,
    cancellation_grace_seconds=30,
)
_TERMINAL_TIMEOUT_POLICY = ToolTimeoutPolicy(
    timeout_seconds=30,
    cancellation_grace_seconds=5,
)
_OUTPUT_POLICY = ToolOutputPolicy(
    max_output_bytes=131_072,
    max_summary_utf8=65_536,
    redact_secrets=False,
)

_STRING_SCHEMA: JsonObject = {"type": "string"}
_NUMBER_SCHEMA: JsonObject = {"type": "number"}
_BOOLEAN_SCHEMA: JsonObject = {"type": "boolean"}


def _object_schema(properties: JsonObject, required: list[str]) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_READ_INPUT_SCHEMA = _object_schema(
    {
        "path": _STRING_SCHEMA,
        "offset": _NUMBER_SCHEMA,
        "limit": _NUMBER_SCHEMA,
    },
    ["path"],
)
_BASH_INPUT_SCHEMA = _object_schema(
    {
        "command": _STRING_SCHEMA,
        "timeout": _NUMBER_SCHEMA,
    },
    ["command"],
)
_EDIT_INPUT_SCHEMA = _object_schema(
    {
        "path": _STRING_SCHEMA,
        "edits": {
            "type": "array",
            "items": _object_schema(
                {
                    "oldText": _STRING_SCHEMA,
                    "newText": _STRING_SCHEMA,
                },
                ["oldText", "newText"],
            ),
        },
    },
    ["path", "edits"],
)
_WRITE_INPUT_SCHEMA = _object_schema(
    {
        "path": _STRING_SCHEMA,
        "content": _STRING_SCHEMA,
    },
    ["path", "content"],
)
_GREP_INPUT_SCHEMA = _object_schema(
    {
        "pattern": _STRING_SCHEMA,
        "path": _STRING_SCHEMA,
        "glob": _STRING_SCHEMA,
        "ignoreCase": _BOOLEAN_SCHEMA,
        "literal": _BOOLEAN_SCHEMA,
        "context": _NUMBER_SCHEMA,
        "limit": _NUMBER_SCHEMA,
    },
    ["pattern"],
)
_FIND_INPUT_SCHEMA = _object_schema(
    {
        "pattern": _STRING_SCHEMA,
        "path": _STRING_SCHEMA,
        "limit": _NUMBER_SCHEMA,
    },
    ["pattern"],
)
_LS_INPUT_SCHEMA = _object_schema(
    {
        "path": _STRING_SCHEMA,
        "limit": _NUMBER_SCHEMA,
    },
    [],
)
_OUTPUT_SCHEMA = _object_schema(
    {
        "model_text": _STRING_SCHEMA,
        "truncated": _BOOLEAN_SCHEMA,
        "exit_code": {"type": "integer"},
        "changed_path": _STRING_SCHEMA,
    },
    ["model_text", "truncated"],
)


def _terminal_input_schema(terminal_result: str) -> JsonObject:
    return _object_schema(
        {
            "terminal_result": {"type": "string", "enum": [terminal_result]},
            "summary": _STRING_SCHEMA,
        },
        ["terminal_result", "summary"],
    )


def _descriptor(
    *,
    tool_id: str,
    model_tool_name: str,
    description: str,
    input_schema: Mapping[str, Any],
    required_capabilities: tuple[str, ...],
    side_effect_class: SideEffectClass,
    idempotency: IdempotencyClass,
    timeout_policy: ToolTimeoutPolicy = _PI_TIMEOUT_POLICY,
) -> ToolDescriptor:
    return ToolDescriptor.model_validate(
        {
            "tool_id": tool_id,
            "tool_version": PI_COMPAT_TOOL_VERSION,
            "implementation_id": f"impl.{tool_id}.v1",
            "model_tool_name": model_tool_name,
            "description": description,
            "input_schema": input_schema,
            "output_schema": _OUTPUT_SCHEMA,
            "required_capabilities": required_capabilities,
            "produced_artifact_ids": (),
            "side_effect_class": side_effect_class,
            "idempotency": idempotency,
            "timeout_policy": timeout_policy,
            "output_policy": _OUTPUT_POLICY,
        }
    )


PI_COMPAT_TOOL_DESCRIPTORS: tuple[ToolDescriptor, ...] = (
    _descriptor(
        tool_id="builtin.pi_compat.read",
        model_tool_name="read",
        description=(
            "Read the contents of a file. Supports text files. Supported image files "
            "return bounded text-only metadata. Text output is truncated to 2000 "
            "lines or 50KB, whichever is reached first. Use offset and limit to "
            "continue."
        ),
        input_schema=_READ_INPUT_SCHEMA,
        required_capabilities=(PI_COMPAT_CAP_FILESYSTEM_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.pi_compat.bash",
        model_tool_name="bash",
        description=(
            "Execute a command in the current working directory using Millforge's "
            "resolved shell. Returns combined stdout and stderr. Output is truncated "
            "to the last 2000 lines or 50KB, whichever is reached first. If "
            "truncated, full output is saved to a temporary file. Timeout may only "
            "lower the harness maximum."
        ),
        input_schema=_BASH_INPUT_SCHEMA,
        required_capabilities=(PI_COMPAT_CAP_PROCESS_EXECUTE,),
        side_effect_class=SideEffectClass.PROCESS_EXECUTION,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.pi_compat.edit",
        model_tool_name="edit",
        description=(
            "Edit a single file using exact text replacement. Every edits[].oldText "
            "must match a unique, non-overlapping region of the original file. If two "
            "changes affect the same block or nearby lines, merge them into one edit "
            "instead of emitting overlapping edits. Do not include large unchanged "
            "regions just to connect distant changes."
        ),
        input_schema=_EDIT_INPUT_SCHEMA,
        required_capabilities=(
            PI_COMPAT_CAP_FILESYSTEM_READ,
            PI_COMPAT_CAP_FILESYSTEM_WRITE,
        ),
        side_effect_class=SideEffectClass.WORKSPACE_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.pi_compat.write",
        model_tool_name="write",
        description=(
            "Write content to a file. Creates the file if it doesn't exist, overwrites "
            "if it does. Automatically creates parent directories."
        ),
        input_schema=_WRITE_INPUT_SCHEMA,
        required_capabilities=(PI_COMPAT_CAP_FILESYSTEM_WRITE,),
        side_effect_class=SideEffectClass.WORKSPACE_WRITE,
        idempotency=IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    _descriptor(
        tool_id="builtin.pi_compat.grep",
        model_tool_name="grep",
        description=(
            "Search file contents for a pattern. Returns matching lines with file "
            "paths and line numbers. Respects .gitignore. Output is truncated to 100 "
            "matches or 50KB, whichever is reached first. Long lines are truncated to "
            "500 characters."
        ),
        input_schema=_GREP_INPUT_SCHEMA,
        required_capabilities=(PI_COMPAT_CAP_FILESYSTEM_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.pi_compat.find",
        model_tool_name="find",
        description=(
            "Search for files by glob pattern. Returns matching paths relative to the "
            "search directory. Respects .gitignore. Output is truncated to 1000 "
            "results or 50KB, whichever is reached first."
        ),
        input_schema=_FIND_INPUT_SCHEMA,
        required_capabilities=(PI_COMPAT_CAP_FILESYSTEM_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.pi_compat.ls",
        model_tool_name="ls",
        description=(
            "List directory contents. Returns entries sorted alphabetically, with '/' "
            "suffix for directories. Includes dotfiles. Output is truncated to 500 "
            "entries or 50KB, whichever is reached first."
        ),
        input_schema=_LS_INPUT_SCHEMA,
        required_capabilities=(PI_COMPAT_CAP_FILESYSTEM_READ,),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    _descriptor(
        tool_id="builtin.pi_compat.submit",
        model_tool_name="submit",
        description="Complete the current Millforge run.",
        input_schema=_terminal_input_schema("COMPLETE"),
        required_capabilities=(PI_COMPAT_CAP_TERMINAL_INTENT,),
        side_effect_class=SideEffectClass.TERMINAL,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        timeout_policy=_TERMINAL_TIMEOUT_POLICY,
    ),
    _descriptor(
        tool_id="builtin.pi_compat.block",
        model_tool_name="block",
        description="Report that the current Millforge run is blocked.",
        input_schema=_terminal_input_schema("BLOCKED"),
        required_capabilities=(PI_COMPAT_CAP_TERMINAL_INTENT,),
        side_effect_class=SideEffectClass.TERMINAL,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        timeout_policy=_TERMINAL_TIMEOUT_POLICY,
    ),
    _descriptor(
        tool_id="builtin.pi_compat.reject",
        model_tool_name="reject",
        description="Reject the current Millforge run.",
        input_schema=_terminal_input_schema("REJECTED"),
        required_capabilities=(PI_COMPAT_CAP_TERMINAL_INTENT,),
        side_effect_class=SideEffectClass.TERMINAL,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        timeout_policy=_TERMINAL_TIMEOUT_POLICY,
    ),
)


def _configured_terminal_descriptors(
    legal_terminal_results: tuple[str, ...],
) -> tuple[ToolDescriptor, ...]:
    tokens = tuple(_terminal_token(result) for result in legal_terminal_results)
    if len(set(tokens)) != len(tokens):
        raise ValueError("configured terminal tool identities collide")
    return tuple(
        _descriptor(
            tool_id=f"builtin.pi_compat.terminal.{token}",
            model_tool_name=f"terminal_{token}",
            description=(
                f"Return terminal result {result_id} with a nonblank summary."
            ),
            input_schema=_terminal_input_schema(result_id),
            required_capabilities=(PI_COMPAT_CAP_TERMINAL_INTENT,),
            side_effect_class=SideEffectClass.TERMINAL,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            timeout_policy=_TERMINAL_TIMEOUT_POLICY,
        )
        for result_id, token in zip(legal_terminal_results, tokens, strict=True)
    )


def _terminal_token(result_id: str) -> str:
    slug = result_id.lower()
    if len(slug) <= 48:
        return slug
    return f"{slug[:39]}_{hashlib.sha256(result_id.encode()).hexdigest()[:8]}"


def _descriptors_for_terminal_results(
    legal_terminal_results: tuple[str, ...],
) -> tuple[ToolDescriptor, ...]:
    if legal_terminal_results == DEFAULT_BASE_TERMINAL_RESULTS:
        return PI_COMPAT_TOOL_DESCRIPTORS
    return (
        *PI_COMPAT_TOOL_DESCRIPTORS[:-3],
        *_configured_terminal_descriptors(legal_terminal_results),
    )


def create_pi_compat_tool_registry() -> ToolRegistry:
    """Create a registry populated only with Pi-compatible descriptors."""

    return _create_pi_compat_tool_registry_for_terminal_results(
        DEFAULT_BASE_TERMINAL_RESULTS
    )


def _create_pi_compat_tool_registry_for_terminal_results(
    legal_terminal_results: tuple[str, ...],
) -> ToolRegistry:
    """Create a registry from already-canonical terminal configuration."""

    registry = ToolRegistry()
    for descriptor in _descriptors_for_terminal_results(legal_terminal_results):
        registry.register(descriptor)
    return registry


def create_pi_compat_tool_snapshot() -> ToolCatalogSnapshot:
    """Create a frozen exact-version snapshot for the Pi-compatible catalog."""

    return _create_pi_compat_tool_snapshot_for_terminal_results(
        DEFAULT_BASE_TERMINAL_RESULTS
    )


def _create_pi_compat_tool_snapshot_for_terminal_results(
    legal_terminal_results: tuple[str, ...],
) -> ToolCatalogSnapshot:
    """Create a frozen snapshot from already-canonical terminal configuration."""

    return _create_pi_compat_tool_registry_for_terminal_results(
        legal_terminal_results
    ).freeze()


__all__ = [
    "PI_COMPAT_CAPABILITY_IDS",
    "PI_COMPAT_TOOL_DESCRIPTORS",
    "PI_COMPAT_TOOL_VERSION",
    "create_pi_compat_tool_registry",
    "create_pi_compat_tool_snapshot",
]
