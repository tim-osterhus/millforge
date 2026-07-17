from __future__ import annotations

from typing import Any

from millforge.compiler.catalogs import CatalogLookupClassification
from millforge.tools.builtins import (
    BUILTIN_TOOL_DESCRIPTORS,
    create_builtin_tool_snapshot,
)
from millforge.tools.pi_compat_catalog import (
    PI_COMPAT_CAPABILITY_IDS,
    PI_COMPAT_TOOL_DESCRIPTORS,
    PI_COMPAT_TOOL_VERSION,
    create_pi_compat_tool_registry,
    create_pi_compat_tool_snapshot,
)

JsonObject = dict[str, Any]


def _object_schema(properties: JsonObject, required: list[str]) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required),
        "additionalProperties": False,
    }


STRING_SCHEMA: JsonObject = {"type": "string"}
NUMBER_SCHEMA: JsonObject = {"type": "number"}
BOOLEAN_SCHEMA: JsonObject = {"type": "boolean"}
OUTPUT_SCHEMA = _object_schema(
    {
        "model_text": STRING_SCHEMA,
        "truncated": BOOLEAN_SCHEMA,
        "exit_code": {"type": "integer"},
        "changed_path": STRING_SCHEMA,
    },
    ["model_text", "truncated"],
)

EXPECTED_DESCRIPTORS = {
    "builtin.pi_compat.read": {
        "model_tool_name": "read",
        "description": (
            "Read the contents of a file. Supports text files. Supported image files "
            "return bounded text-only metadata. Text output is truncated to 2000 "
            "lines or 50KB, whichever is reached first. Use offset and limit to "
            "continue."
        ),
        "input_schema": _object_schema(
            {
                "path": STRING_SCHEMA,
                "offset": NUMBER_SCHEMA,
                "limit": NUMBER_SCHEMA,
            },
            ["path"],
        ),
        "required_capabilities": ("unrestricted.filesystem.read",),
        "side_effect_class": "read_only",
        "idempotency": "idempotent",
        "timeout_policy": {
            "timeout_seconds": 1800,
            "cancellation_grace_seconds": 30,
        },
        "descriptor_sha256": "8bc27d694649e2237a7e141da1dfd389054e8503095c7210c97a41529c0dd3f1",
    },
    "builtin.pi_compat.bash": {
        "model_tool_name": "bash",
        "description": (
            "Execute a command in the current working directory using Millforge's "
            "resolved shell. Returns combined stdout and stderr. Output is truncated "
            "to the last 2000 lines or 50KB, whichever is reached first. If "
            "truncated, full output is saved to a temporary file. Timeout may only "
            "lower the harness maximum."
        ),
        "input_schema": _object_schema(
            {
                "command": STRING_SCHEMA,
                "timeout": NUMBER_SCHEMA,
            },
            ["command"],
        ),
        "required_capabilities": ("unrestricted.process.execute",),
        "side_effect_class": "process_execution",
        "idempotency": "non_idempotent",
        "timeout_policy": {
            "timeout_seconds": 1800,
            "cancellation_grace_seconds": 30,
        },
        "descriptor_sha256": "65ce4b715f6c452e683a9c92535fd2ff4b942bc23a826a7a55239ae787f63539",
    },
    "builtin.pi_compat.edit": {
        "model_tool_name": "edit",
        "description": (
            "Edit a single file using exact text replacement. Every edits[].oldText "
            "must match a unique, non-overlapping region of the original file. If two "
            "changes affect the same block or nearby lines, merge them into one edit "
            "instead of emitting overlapping edits. Do not include large unchanged "
            "regions just to connect distant changes."
        ),
        "input_schema": _object_schema(
            {
                "path": STRING_SCHEMA,
                "edits": {
                    "type": "array",
                    "items": _object_schema(
                        {
                            "oldText": STRING_SCHEMA,
                            "newText": STRING_SCHEMA,
                        },
                        ["oldText", "newText"],
                    ),
                },
            },
            ["path", "edits"],
        ),
        "required_capabilities": (
            "unrestricted.filesystem.read",
            "unrestricted.filesystem.write",
        ),
        "side_effect_class": "workspace_write",
        "idempotency": "idempotent_with_key",
        "timeout_policy": {
            "timeout_seconds": 1800,
            "cancellation_grace_seconds": 30,
        },
        "descriptor_sha256": "a82dda33a175a035322260853ae34cc766e92b731bc98effdf4c4b82726c8442",
    },
    "builtin.pi_compat.write": {
        "model_tool_name": "write",
        "description": (
            "Write content to a file. Creates the file if it doesn't exist, overwrites "
            "if it does. Automatically creates parent directories."
        ),
        "input_schema": _object_schema(
            {
                "path": STRING_SCHEMA,
                "content": STRING_SCHEMA,
            },
            ["path", "content"],
        ),
        "required_capabilities": ("unrestricted.filesystem.write",),
        "side_effect_class": "workspace_write",
        "idempotency": "idempotent_with_key",
        "timeout_policy": {
            "timeout_seconds": 1800,
            "cancellation_grace_seconds": 30,
        },
        "descriptor_sha256": "922d66804e282bf2a476bfce064abb2743cec597e65a037346a0d86febbd4065",
    },
    "builtin.pi_compat.grep": {
        "model_tool_name": "grep",
        "description": (
            "Search file contents for a pattern. Returns matching lines with file "
            "paths and line numbers. Respects .gitignore. Output is truncated to 100 "
            "matches or 50KB, whichever is reached first. Long lines are truncated to "
            "500 characters."
        ),
        "input_schema": _object_schema(
            {
                "pattern": STRING_SCHEMA,
                "path": STRING_SCHEMA,
                "glob": STRING_SCHEMA,
                "ignoreCase": BOOLEAN_SCHEMA,
                "literal": BOOLEAN_SCHEMA,
                "context": NUMBER_SCHEMA,
                "limit": NUMBER_SCHEMA,
            },
            ["pattern"],
        ),
        "required_capabilities": ("unrestricted.filesystem.read",),
        "side_effect_class": "read_only",
        "idempotency": "idempotent",
        "timeout_policy": {
            "timeout_seconds": 1800,
            "cancellation_grace_seconds": 30,
        },
        "descriptor_sha256": "fe13a083429258548be9351f123ef87eff2cb25b2ff57c03ae54ac7a8dc02517",
    },
    "builtin.pi_compat.find": {
        "model_tool_name": "find",
        "description": (
            "Search for files by glob pattern. Returns matching paths relative to the "
            "search directory. Respects .gitignore. Output is truncated to 1000 "
            "results or 50KB, whichever is reached first."
        ),
        "input_schema": _object_schema(
            {
                "pattern": STRING_SCHEMA,
                "path": STRING_SCHEMA,
                "limit": NUMBER_SCHEMA,
            },
            ["pattern"],
        ),
        "required_capabilities": ("unrestricted.filesystem.read",),
        "side_effect_class": "read_only",
        "idempotency": "idempotent",
        "timeout_policy": {
            "timeout_seconds": 1800,
            "cancellation_grace_seconds": 30,
        },
        "descriptor_sha256": "4c63ac8a7b0127298df7bac462b7d246177dee6bec139c1a64482368e763a753",
    },
    "builtin.pi_compat.ls": {
        "model_tool_name": "ls",
        "description": (
            "List directory contents. Returns entries sorted alphabetically, with '/' "
            "suffix for directories. Includes dotfiles. Output is truncated to 500 "
            "entries or 50KB, whichever is reached first."
        ),
        "input_schema": _object_schema(
            {
                "path": STRING_SCHEMA,
                "limit": NUMBER_SCHEMA,
            },
            [],
        ),
        "required_capabilities": ("unrestricted.filesystem.read",),
        "side_effect_class": "read_only",
        "idempotency": "idempotent",
        "timeout_policy": {
            "timeout_seconds": 1800,
            "cancellation_grace_seconds": 30,
        },
        "descriptor_sha256": "4cfdf4b63d9fe201ce88cf9a602c7c26a02426489575cc1404405135349b5e6a",
    },
    "builtin.pi_compat.submit": {
        "model_tool_name": "submit",
        "description": "Complete the current Millforge run.",
        "input_schema": _object_schema(
            {
                "terminal_result": {"type": "string", "enum": ["COMPLETE"]},
                "summary": STRING_SCHEMA,
            },
            ["terminal_result", "summary"],
        ),
        "required_capabilities": ("terminal.intent",),
        "side_effect_class": "terminal",
        "idempotency": "non_idempotent",
        "timeout_policy": {
            "timeout_seconds": 30,
            "cancellation_grace_seconds": 5,
        },
        "descriptor_sha256": "a0e969c35463e85ceb092084f12fdfb8cbd2b52238b56bbb23b365f4ba02b7d4",
    },
    "builtin.pi_compat.block": {
        "model_tool_name": "block",
        "description": "Report that the current Millforge run is blocked.",
        "input_schema": _object_schema(
            {
                "terminal_result": {"type": "string", "enum": ["BLOCKED"]},
                "summary": STRING_SCHEMA,
            },
            ["terminal_result", "summary"],
        ),
        "required_capabilities": ("terminal.intent",),
        "side_effect_class": "terminal",
        "idempotency": "non_idempotent",
        "timeout_policy": {
            "timeout_seconds": 30,
            "cancellation_grace_seconds": 5,
        },
        "descriptor_sha256": "394920064b8d29571e0d3c45f12a9c8988a48df4bb85b414153c9159e59be54b",
    },
    "builtin.pi_compat.reject": {
        "model_tool_name": "reject",
        "description": "Reject the current Millforge run.",
        "input_schema": _object_schema(
            {
                "terminal_result": {"type": "string", "enum": ["REJECTED"]},
                "summary": STRING_SCHEMA,
            },
            ["terminal_result", "summary"],
        ),
        "required_capabilities": ("terminal.intent",),
        "side_effect_class": "terminal",
        "idempotency": "non_idempotent",
        "timeout_policy": {
            "timeout_seconds": 30,
            "cancellation_grace_seconds": 5,
        },
        "descriptor_sha256": "44925915fa9f9a0d76893c0576d9bf934806432dd5306d9a66ec4113a038d15d",
    },
}

EXPECTED_OUTPUT_POLICY = {
    "max_output_bytes": 131_072,
    "max_summary_utf8": 65_536,
    "redact_secrets": False,
}
EXPECTED_SNAPSHOT_SHA256 = (
    "5de78f0943c5ef169f971651fd3220308b2dee2fae9641919c262824cc92808a"
)
EXPECTED_SNAPSHOT_ID = (
    "19eb2a742fa5c14def0c68284b314140a03ca955ca9e00dd7232038d58552bd6"
)


def test_pi_compat_descriptor_table_is_exact() -> None:
    assert PI_COMPAT_CAPABILITY_IDS == (
        "unrestricted.filesystem.read",
        "unrestricted.filesystem.write",
        "unrestricted.process.execute",
        "terminal.intent",
    )
    assert PI_COMPAT_TOOL_VERSION == 1
    assert tuple(
        descriptor.tool_id for descriptor in PI_COMPAT_TOOL_DESCRIPTORS
    ) == tuple(EXPECTED_DESCRIPTORS)

    for descriptor in PI_COMPAT_TOOL_DESCRIPTORS:
        expected = EXPECTED_DESCRIPTORS[descriptor.tool_id]
        actual = descriptor.model_dump(mode="json")

        assert actual["schema_version"] == 1
        assert actual["kind"] == "millforge.tool_descriptor.v1"
        assert actual["tool_version"] == PI_COMPAT_TOOL_VERSION
        assert actual["implementation_id"] == f"impl.{descriptor.tool_id}.v1"
        assert actual["model_tool_name"] == expected["model_tool_name"]
        assert actual["description"] == expected["description"]
        assert actual["input_schema"] == expected["input_schema"]
        assert actual["output_schema"] == OUTPUT_SCHEMA
        assert (
            tuple(actual["required_capabilities"]) == expected["required_capabilities"]
        )
        assert tuple(actual["produced_artifact_ids"]) == ()
        assert actual["side_effect_class"] == expected["side_effect_class"]
        assert actual["idempotency"] == expected["idempotency"]
        assert actual["timeout_policy"] == expected["timeout_policy"]
        assert actual["output_policy"] == EXPECTED_OUTPUT_POLICY
        assert descriptor.descriptor_sha256 == expected["descriptor_sha256"]


def test_pi_compat_registry_snapshot_is_exact_and_deterministic() -> None:
    registry = create_pi_compat_tool_registry()
    snapshot = registry.freeze()

    assert snapshot.snapshot_sha256 == EXPECTED_SNAPSHOT_SHA256
    assert snapshot.snapshot_id == EXPECTED_SNAPSHOT_ID
    assert create_pi_compat_tool_snapshot().snapshot_sha256 == snapshot.snapshot_sha256
    assert create_pi_compat_tool_snapshot().snapshot_id == snapshot.snapshot_id

    for descriptor in PI_COMPAT_TOOL_DESCRIPTORS:
        lookup = snapshot.resolve_exact(descriptor.tool_id, descriptor.tool_version)
        assert lookup.classification is CatalogLookupClassification.FOUND
        assert lookup.entry is not None
        assert lookup.entry.descriptor_sha256 == descriptor.descriptor_sha256


def test_pi_compat_catalog_is_separate_from_governed_catalog() -> None:
    pi_compat_tool_ids = {
        descriptor.tool_id for descriptor in PI_COMPAT_TOOL_DESCRIPTORS
    }
    governed_tool_ids = {descriptor.tool_id for descriptor in BUILTIN_TOOL_DESCRIPTORS}
    governed_snapshot = create_builtin_tool_snapshot()

    assert pi_compat_tool_ids.isdisjoint(governed_tool_ids)
    for descriptor in PI_COMPAT_TOOL_DESCRIPTORS:
        lookup = governed_snapshot.resolve_exact(
            descriptor.tool_id,
            descriptor.tool_version,
        )
        assert lookup.classification is CatalogLookupClassification.MISSING
        assert lookup.entry is None
