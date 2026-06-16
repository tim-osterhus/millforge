from __future__ import annotations

import importlib
import re
from collections.abc import Mapping
from typing import Any, cast

import pytest

import millforge.tools as public_tools
from millforge import (
    CapabilityEnvelope,
    CapabilityGrant,
    IdempotencyClass,
    SideEffectClass,
)
from millforge.compiler import ToolCatalogSnapshot, validate_capability_grants
from millforge.compiler.catalogs import (
    CatalogLookupClassification,
    capture_catalog_snapshot_metadata,
)
from millforge.compiler.schema_validation import (
    normalize_json_schema,
    validate_json_schema_subset,
)
from millforge.tools import (
    BUILTIN_TOOL_DESCRIPTORS,
    BUILTIN_TOOL_VERSION,
    FrozenToolRegistrySnapshot,
    ToolDescriptor,
    create_builtin_tool_registry,
    create_builtin_tool_snapshot,
    iter_builtin_tool_descriptors,
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
JsonObject = dict[str, Any]


def _object_schema(properties: JsonObject, required: list[str]) -> JsonObject:
    return {
        "additionalProperties": False,
        "properties": properties,
        "required": sorted(required),
        "type": "object",
    }


STATUS_SCHEMA: JsonObject = {
    "enum": ["success", "soft_failure", "hard_failure"],
    "type": "string",
}
STRING_SCHEMA: JsonObject = {"type": "string"}
INTEGER_SCHEMA: JsonObject = {"type": "integer"}
BOOLEAN_SCHEMA: JsonObject = {"type": "boolean"}
STRING_ARRAY_SCHEMA: JsonObject = {
    "items": STRING_SCHEMA,
    "type": "array",
}
COMMON_STATUS_SUMMARY_PROPERTIES: JsonObject = {
    "status": STATUS_SCHEMA,
    "summary": STRING_SCHEMA,
}
EMPTY_OBJECT_SCHEMA = _object_schema({}, [])

EXPECTED_TOOL_IDS = (
    "builtin.request.inspect",
    "builtin.request.read_requirements",
    "builtin.workspace.list_files",
    "builtin.workspace.read_file",
    "builtin.workspace.search_text",
    "builtin.workspace.write_file",
    "builtin.workspace.apply_patch",
    "builtin.workspace.read_diff",
    "builtin.shell.run_tests",
    "builtin.shell.run_static_check",
    "builtin.artifact.read",
    "builtin.artifact.write_plan",
    "builtin.artifact.write_patch_summary",
    "builtin.artifact.write_test_results",
    "builtin.artifact.write_verdict",
    "builtin.terminal.submit",
    "builtin.terminal.reject",
    "builtin.terminal.escalate",
)

EXPECTED_CAPABILITIES = {
    "builtin.request.inspect": ("request.read",),
    "builtin.request.read_requirements": ("request.read",),
    "builtin.workspace.list_files": ("workspace.read",),
    "builtin.workspace.read_file": ("workspace.read",),
    "builtin.workspace.search_text": ("workspace.read", "workspace.search"),
    "builtin.workspace.write_file": ("workspace.write",),
    "builtin.workspace.apply_patch": ("workspace.write",),
    "builtin.workspace.read_diff": ("workspace.diff.read",),
    "builtin.shell.run_tests": ("process.test",),
    "builtin.shell.run_static_check": ("process.static_check",),
    "builtin.artifact.read": ("artifact.read",),
    "builtin.artifact.write_plan": ("artifact.write",),
    "builtin.artifact.write_patch_summary": ("artifact.write",),
    "builtin.artifact.write_test_results": ("artifact.write",),
    "builtin.artifact.write_verdict": ("artifact.write",),
    "builtin.terminal.submit": ("terminal.intent",),
    "builtin.terminal.reject": ("terminal.intent",),
    "builtin.terminal.escalate": ("terminal.intent",),
}

EXPECTED_ARTIFACTS = {
    "builtin.artifact.write_plan": ("plan",),
    "builtin.artifact.write_patch_summary": ("patch_summary",),
    "builtin.artifact.write_test_results": ("test_results",),
    "builtin.artifact.write_verdict": ("arbiter_verdict", "checker_verdict"),
}

EXPECTED_CLASSIFICATIONS = {
    "builtin.request.inspect": (SideEffectClass.READ_ONLY, IdempotencyClass.IDEMPOTENT),
    "builtin.request.read_requirements": (
        SideEffectClass.READ_ONLY,
        IdempotencyClass.IDEMPOTENT,
    ),
    "builtin.workspace.list_files": (
        SideEffectClass.READ_ONLY,
        IdempotencyClass.IDEMPOTENT,
    ),
    "builtin.workspace.read_file": (
        SideEffectClass.READ_ONLY,
        IdempotencyClass.IDEMPOTENT,
    ),
    "builtin.workspace.search_text": (
        SideEffectClass.READ_ONLY,
        IdempotencyClass.IDEMPOTENT,
    ),
    "builtin.workspace.write_file": (
        SideEffectClass.WORKSPACE_WRITE,
        IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    "builtin.workspace.apply_patch": (
        SideEffectClass.WORKSPACE_WRITE,
        IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    "builtin.workspace.read_diff": (
        SideEffectClass.READ_ONLY,
        IdempotencyClass.IDEMPOTENT,
    ),
    "builtin.shell.run_tests": (
        SideEffectClass.PROCESS_EXECUTION,
        IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    "builtin.shell.run_static_check": (
        SideEffectClass.PROCESS_EXECUTION,
        IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    "builtin.artifact.read": (
        SideEffectClass.READ_ONLY,
        IdempotencyClass.IDEMPOTENT,
    ),
    "builtin.artifact.write_plan": (
        SideEffectClass.ARTIFACT_WRITE,
        IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    "builtin.artifact.write_patch_summary": (
        SideEffectClass.ARTIFACT_WRITE,
        IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    "builtin.artifact.write_test_results": (
        SideEffectClass.ARTIFACT_WRITE,
        IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    "builtin.artifact.write_verdict": (
        SideEffectClass.ARTIFACT_WRITE,
        IdempotencyClass.IDEMPOTENT_WITH_KEY,
    ),
    "builtin.terminal.submit": (
        SideEffectClass.TERMINAL,
        IdempotencyClass.NON_IDEMPOTENT,
    ),
    "builtin.terminal.reject": (
        SideEffectClass.TERMINAL,
        IdempotencyClass.NON_IDEMPOTENT,
    ),
    "builtin.terminal.escalate": (
        SideEffectClass.TERMINAL,
        IdempotencyClass.NON_IDEMPOTENT,
    ),
}

EXPECTED_INPUT_SCHEMAS = {
    "builtin.request.inspect": EMPTY_OBJECT_SCHEMA,
    "builtin.request.read_requirements": EMPTY_OBJECT_SCHEMA,
    "builtin.workspace.list_files": _object_schema(
        {
            "glob": STRING_SCHEMA,
            "max_results": INTEGER_SCHEMA,
            "root": STRING_SCHEMA,
        },
        ["root"],
    ),
    "builtin.workspace.read_file": _object_schema(
        {
            "max_bytes": INTEGER_SCHEMA,
            "path": STRING_SCHEMA,
        },
        ["path"],
    ),
    "builtin.workspace.search_text": _object_schema(
        {
            "glob": STRING_SCHEMA,
            "max_results": INTEGER_SCHEMA,
            "query": STRING_SCHEMA,
            "root": STRING_SCHEMA,
        },
        ["query"],
    ),
    "builtin.workspace.write_file": _object_schema(
        {
            "content": STRING_SCHEMA,
            "expected_sha256": STRING_SCHEMA,
            "path": STRING_SCHEMA,
        },
        ["content", "path"],
    ),
    "builtin.workspace.apply_patch": _object_schema(
        {
            "expected_base_sha256": STRING_SCHEMA,
            "patch": STRING_SCHEMA,
        },
        ["patch"],
    ),
    "builtin.workspace.read_diff": _object_schema(
        {
            "max_bytes": INTEGER_SCHEMA,
            "paths": STRING_ARRAY_SCHEMA,
        },
        [],
    ),
    "builtin.shell.run_tests": _object_schema(
        {
            "max_output_bytes": INTEGER_SCHEMA,
            "profile": STRING_SCHEMA,
            "selector": STRING_SCHEMA,
        },
        ["profile"],
    ),
    "builtin.shell.run_static_check": _object_schema(
        {
            "max_output_bytes": INTEGER_SCHEMA,
            "profile": STRING_SCHEMA,
            "selector": STRING_SCHEMA,
        },
        ["profile"],
    ),
    "builtin.artifact.read": _object_schema(
        {
            "artifact_id": STRING_SCHEMA,
            "max_bytes": INTEGER_SCHEMA,
        },
        ["artifact_id"],
    ),
    "builtin.artifact.write_plan": _object_schema({"plan": STRING_SCHEMA}, ["plan"]),
    "builtin.artifact.write_patch_summary": _object_schema(
        {"summary": STRING_SCHEMA},
        ["summary"],
    ),
    "builtin.artifact.write_test_results": _object_schema(
        {"results": STRING_SCHEMA},
        ["results"],
    ),
    "builtin.artifact.write_verdict": _object_schema(
        {
            "artifact_id": {
                "enum": ["checker_verdict", "arbiter_verdict"],
                "type": "string",
            },
            "verdict": STRING_SCHEMA,
        },
        ["artifact_id", "verdict"],
    ),
    "builtin.terminal.submit": _object_schema(
        {
            "artifact_refs": STRING_ARRAY_SCHEMA,
            "summary": STRING_SCHEMA,
            "terminal_result": STRING_SCHEMA,
        },
        ["summary", "terminal_result"],
    ),
    "builtin.terminal.reject": _object_schema(
        {
            "artifact_refs": STRING_ARRAY_SCHEMA,
            "summary": STRING_SCHEMA,
            "terminal_result": STRING_SCHEMA,
        },
        ["summary", "terminal_result"],
    ),
    "builtin.terminal.escalate": _object_schema(
        {
            "artifact_refs": STRING_ARRAY_SCHEMA,
            "blocker": STRING_SCHEMA,
            "summary": STRING_SCHEMA,
            "terminal_result": STRING_SCHEMA,
        },
        ["blocker", "summary", "terminal_result"],
    ),
}

EXPECTED_OUTPUT_SCHEMAS = {
    "builtin.request.inspect": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_refs": STRING_ARRAY_SCHEMA,
            "objective": STRING_SCHEMA,
            "request_id": STRING_SCHEMA,
            "stage_id": STRING_SCHEMA,
            "truncated": BOOLEAN_SCHEMA,
        },
        ["artifact_refs", "objective", "request_id", "stage_id", "status", "summary"],
    ),
    "builtin.request.read_requirements": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_refs": STRING_ARRAY_SCHEMA,
            "requirements": STRING_ARRAY_SCHEMA,
            "truncated": BOOLEAN_SCHEMA,
        },
        ["artifact_refs", "requirements", "status", "summary"],
    ),
    "builtin.workspace.list_files": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "paths": STRING_ARRAY_SCHEMA,
            "truncated": BOOLEAN_SCHEMA,
        },
        ["paths", "status", "summary", "truncated"],
    ),
    "builtin.workspace.read_file": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_refs": STRING_ARRAY_SCHEMA,
            "content": STRING_SCHEMA,
            "truncated": BOOLEAN_SCHEMA,
        },
        ["artifact_refs", "content", "status", "summary", "truncated"],
    ),
    "builtin.workspace.search_text": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "matches": {
                "items": _object_schema(
                    {
                        "line": INTEGER_SCHEMA,
                        "path": STRING_SCHEMA,
                        "snippet": STRING_SCHEMA,
                    },
                    ["line", "path", "snippet"],
                ),
                "type": "array",
            },
            "truncated": BOOLEAN_SCHEMA,
        },
        ["matches", "status", "summary", "truncated"],
    ),
    "builtin.workspace.write_file": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "content_sha256": STRING_SCHEMA,
            "path": STRING_SCHEMA,
        },
        ["content_sha256", "path", "status", "summary"],
    ),
    "builtin.workspace.apply_patch": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "changed_paths": STRING_ARRAY_SCHEMA,
            "diff_sha256": STRING_SCHEMA,
        },
        ["changed_paths", "diff_sha256", "status", "summary"],
    ),
    "builtin.workspace.read_diff": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "diff": STRING_SCHEMA,
            "diff_sha256": STRING_SCHEMA,
            "truncated": BOOLEAN_SCHEMA,
        },
        ["diff", "diff_sha256", "status", "summary", "truncated"],
    ),
    "builtin.shell.run_tests": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_refs": STRING_ARRAY_SCHEMA,
            "exit_code": INTEGER_SCHEMA,
            "truncated": BOOLEAN_SCHEMA,
        },
        ["artifact_refs", "exit_code", "status", "summary", "truncated"],
    ),
    "builtin.shell.run_static_check": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_refs": STRING_ARRAY_SCHEMA,
            "exit_code": INTEGER_SCHEMA,
            "truncated": BOOLEAN_SCHEMA,
        },
        ["artifact_refs", "exit_code", "status", "summary", "truncated"],
    ),
    "builtin.artifact.read": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_id": STRING_SCHEMA,
            "content": STRING_SCHEMA,
            "content_sha256": STRING_SCHEMA,
            "truncated": BOOLEAN_SCHEMA,
        },
        ["artifact_id", "content", "content_sha256", "status", "summary", "truncated"],
    ),
    "builtin.artifact.write_plan": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_id": STRING_SCHEMA,
            "content_sha256": STRING_SCHEMA,
        },
        ["artifact_id", "content_sha256", "status", "summary"],
    ),
    "builtin.artifact.write_patch_summary": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_id": STRING_SCHEMA,
            "content_sha256": STRING_SCHEMA,
        },
        ["artifact_id", "content_sha256", "status", "summary"],
    ),
    "builtin.artifact.write_test_results": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_id": STRING_SCHEMA,
            "content_sha256": STRING_SCHEMA,
        },
        ["artifact_id", "content_sha256", "status", "summary"],
    ),
    "builtin.artifact.write_verdict": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "artifact_id": STRING_SCHEMA,
            "content_sha256": STRING_SCHEMA,
        },
        ["artifact_id", "content_sha256", "status", "summary"],
    ),
    "builtin.terminal.submit": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "terminal_result": STRING_SCHEMA,
        },
        ["status", "summary", "terminal_result"],
    ),
    "builtin.terminal.reject": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "terminal_result": STRING_SCHEMA,
        },
        ["status", "summary", "terminal_result"],
    ),
    "builtin.terminal.escalate": _object_schema(
        {
            **COMMON_STATUS_SUMMARY_PROPERTIES,
            "terminal_result": STRING_SCHEMA,
        },
        ["status", "summary", "terminal_result"],
    ),
}

EXPECTED_BUILTIN_DESCRIPTOR_HASHES = {
    "builtin.artifact.read": "925f7b4152a695ebd64e7341f709b938e0df9523b25ed5ee8e603e383483c926",
    "builtin.artifact.write_patch_summary": "1790cd8470902fa89434a83a3b27d9a3cc7c37cd75d059654e0fc3dbb31c1c96",
    "builtin.artifact.write_plan": "a863fd921266dcb54f694bd51cc57b68cad8ea68bdcff2fb8cea5abca0f5370e",
    "builtin.artifact.write_test_results": "485ad6582ffaf257933abc7ee94b29d57a83fd757f51db5c8526a6eb2a17e561",
    "builtin.artifact.write_verdict": "d9faf4fd76aac48768cd70b132c39f51d339f28034a010b4e98661edf976aa93",
    "builtin.request.inspect": "166b7f135c25b683e7fb12762c98e380f17e5a9bf1f21ca231e6804b38c1fa6a",
    "builtin.request.read_requirements": "9a39700d56e58134541114c99bc1fdbd2a6788bed1e16c517fcaf373151cddf2",
    "builtin.shell.run_static_check": "eed0d6ad8cb9d268964527190ab7d2efc74182f0c80a3d956bc37a44b5c36a0c",
    "builtin.shell.run_tests": "80a1a93eb4e4e85b4dfff417cb08bbafd6b29c5a0fbb2bc31dfc13f8d74ff31d",
    "builtin.terminal.escalate": "929e0fcbfd60cb32da6103fd794e33b928596b93272a38f7c3d92d0a01670f62",
    "builtin.terminal.reject": "229e177ffbc505ac5e1455629533b17ce558cfebe8cb6d49b46e7586bb6a60a8",
    "builtin.terminal.submit": "748b495f1703e5795935f4cec7cfbf4406a3964c8c54e6ef0adbcc4cc660c2bb",
    "builtin.workspace.apply_patch": "3089adc6627cc05b4b7e5c7c8db7813673ac1e23b2464a621801e36e91561479",
    "builtin.workspace.list_files": "86e97a4fc1e5f3796ea7e94906449c0f6b06338a980d2a604f809db84841223e",
    "builtin.workspace.read_diff": "77c2e4c97e67ea0d20dfe9ec3571d98f0516640f5dc3466de7a132a096441cd2",
    "builtin.workspace.read_file": "d00add1aa0281d3d5e5fc137e25872b8ab870fa525829c0ad55b1fe6ac1f9eae",
    "builtin.workspace.search_text": "55d6d17aa2027c27712ae7ddceb5572f4da4d5ac1262b94b2c7cf01ff6300939",
    "builtin.workspace.write_file": "9bbffe058e37d56cd7da1eb820ea6e10eea6d753cb48290f1fd4bb2bfe980de3",
}

EXPECTED_BUILTIN_SNAPSHOT_SHA256 = (
    "621be3b11711f4602ab761add762e499803b4fb776da806a4852a742166d3e2f"
)
EXPECTED_BUILTIN_SNAPSHOT_ID = (
    "16ee09eff72aefb6968f21a7175852323ae690f0c33baaa72ec8de354b146066"
)


def test_builtin_catalog_has_exact_descriptor_matrix() -> None:
    descriptors = iter_builtin_tool_descriptors()
    by_id = {descriptor.tool_id: descriptor for descriptor in descriptors}

    assert descriptors is BUILTIN_TOOL_DESCRIPTORS
    assert tuple(by_id) == EXPECTED_TOOL_IDS
    assert len(descriptors) == 18
    assert {descriptor.tool_version for descriptor in descriptors} == {
        BUILTIN_TOOL_VERSION
    }
    assert {descriptor.tool_version for descriptor in descriptors} == {1}
    assert len({descriptor.implementation_id for descriptor in descriptors}) == 18
    assert len({descriptor.model_tool_name for descriptor in descriptors}) == 18
    assert len(set(public_tools.BUILTIN_CAPABILITY_IDS)) == len(
        public_tools.BUILTIN_CAPABILITY_IDS
    )
    assert tuple(sorted(public_tools.BUILTIN_CAPABILITY_IDS)) == (
        public_tools.BUILTIN_CAPABILITY_IDS
    )

    for tool_id, descriptor in by_id.items():
        assert isinstance(descriptor, ToolDescriptor)
        assert descriptor.required_capabilities == EXPECTED_CAPABILITIES[tool_id]
        assert descriptor.produced_artifact_ids == EXPECTED_ARTIFACTS.get(tool_id, ())
        assert (
            descriptor.side_effect_class,
            descriptor.idempotency,
        ) == EXPECTED_CLASSIFICATIONS[tool_id]


def test_builtin_schemas_are_closed_and_stay_in_accepted_subset() -> None:
    for descriptor in iter_builtin_tool_descriptors():
        for schema in (descriptor.input_schema, descriptor.output_schema):
            normalized = validate_json_schema_subset(schema)
            assert normalized["type"] == "object"
            assert normalized["additionalProperties"] is False


def test_builtin_schemas_match_canonical_04b_matrix() -> None:
    for descriptor in iter_builtin_tool_descriptors():
        assert (
            normalize_json_schema(descriptor.input_schema)
            == EXPECTED_INPUT_SCHEMAS[descriptor.tool_id]
        )
        assert (
            normalize_json_schema(descriptor.output_schema)
            == EXPECTED_OUTPUT_SCHEMAS[descriptor.tool_id]
        )


def test_builtin_catalog_uses_only_safe_descriptor_vocabularies() -> None:
    bad_workspace_fields = {
        "absolute_path",
        "files",
        "host_path",
        "pattern",
        "relative_path",
        "relative_root",
        "text",
    }
    bad_shell_fields = {"args", "command", "cwd", "env", "raw_args"}
    bad_descriptor_control_fields = {"idempotency_key", "ok"}
    bad_terminal_fields = {
        "daemon_control",
        "reason",
        "queue_control",
        "result",
        "status_marker",
        "pause",
        "resume",
        "stop",
    }

    for descriptor in iter_builtin_tool_descriptors():
        property_names = set(
            _schema_property_names(descriptor.input_schema)
            + _schema_property_names(descriptor.output_schema)
        )
        assert property_names.isdisjoint(bad_descriptor_control_fields)
        if descriptor.tool_id.startswith("builtin.workspace."):
            assert property_names.isdisjoint(bad_workspace_fields)
        if descriptor.tool_id.startswith("builtin.shell."):
            assert property_names.isdisjoint(bad_shell_fields)
            assert "profile" in property_names
            assert "selector" in property_names
        if descriptor.tool_id.startswith("builtin.terminal."):
            assert property_names.isdisjoint(bad_terminal_fields)


def test_builtin_registry_and_snapshot_are_deterministic() -> None:
    registry = create_builtin_tool_registry()
    snapshot = registry.freeze()
    repeated = create_builtin_tool_snapshot()

    assert isinstance(snapshot, FrozenToolRegistrySnapshot)
    assert isinstance(snapshot, ToolCatalogSnapshot)
    assert snapshot.snapshot_id == repeated.snapshot_id
    assert snapshot.snapshot_sha256 == repeated.snapshot_sha256
    assert SHA256_RE.fullmatch(snapshot.snapshot_id)
    assert SHA256_RE.fullmatch(snapshot.snapshot_sha256)
    assert (
        capture_catalog_snapshot_metadata(snapshot).snapshot_id == snapshot.snapshot_id
    )

    for descriptor in iter_builtin_tool_descriptors():
        lookup = snapshot.resolve_exact(descriptor.tool_id, descriptor.tool_version)
        assert lookup.classification is CatalogLookupClassification.FOUND
        assert lookup.entry is not None
        assert lookup.entry.descriptor_sha256 == descriptor.descriptor_sha256

    assert (
        snapshot.resolve_exact("builtin.workspace.read_file.latest", 1).classification
        is CatalogLookupClassification.INVALID
    )
    assert (
        snapshot.resolve_exact(
            "builtin.workspace.read_file", cast(Any, "1")
        ).classification
        is CatalogLookupClassification.INVALID
    )
    assert (
        snapshot.resolve_exact("builtin.workspace.missing", 1).classification
        is CatalogLookupClassification.MISSING
    )


def test_builtin_descriptor_and_snapshot_hashes_are_golden_vectors() -> None:
    snapshot = create_builtin_tool_registry().freeze()

    assert snapshot.snapshot_sha256 == EXPECTED_BUILTIN_SNAPSHOT_SHA256
    assert snapshot.snapshot_id == EXPECTED_BUILTIN_SNAPSHOT_ID
    assert {
        record.tool_id: record.descriptor_sha256
        for record in snapshot.descriptor_hash_records
    } == EXPECTED_BUILTIN_DESCRIPTOR_HASHES


def test_builtin_descriptors_feed_capability_validation() -> None:
    entries = {
        descriptor.tool_id: descriptor.to_catalog_entry()
        for descriptor in iter_builtin_tool_descriptors()
    }
    all_grants = CapabilityEnvelope(
        grants=(
            CapabilityGrant(capability_id="artifact.read"),
            CapabilityGrant(capability_id="artifact.write"),
            CapabilityGrant(capability_id="process.static_check"),
            CapabilityGrant(capability_id="process.test"),
            CapabilityGrant(capability_id="request.read"),
            CapabilityGrant(capability_id="terminal.intent"),
            CapabilityGrant(capability_id="workspace.diff.read"),
            CapabilityGrant(capability_id="workspace.read"),
            CapabilityGrant(capability_id="workspace.search"),
            CapabilityGrant(capability_id="workspace.write"),
        )
    )

    accepted = validate_capability_grants(entries, all_grants)
    assert accepted.ok
    assert accepted.required_capability_ids == (
        "artifact.read",
        "artifact.write",
        "process.static_check",
        "process.test",
        "request.read",
        "terminal.intent",
        "workspace.diff.read",
        "workspace.read",
        "workspace.search",
        "workspace.write",
    )

    missing_search = validate_capability_grants(
        {"search": entries["builtin.workspace.search_text"]},
        CapabilityEnvelope(grants=(CapabilityGrant(capability_id="workspace.read"),)),
    )
    assert not missing_search.ok
    assert missing_search.diagnostics[0].fields[0].value == "workspace.search"


def test_builtin_module_exports_descriptor_helpers_without_execution_surface() -> None:
    module = importlib.import_module("millforge.tools.builtins")

    expected_exports = {
        "BUILTIN_CAPABILITY_IDS",
        "BUILTIN_TOOL_DESCRIPTORS",
        "BUILTIN_TOOL_VERSION",
        "create_builtin_tool_registry",
        "create_builtin_tool_snapshot",
        "iter_builtin_tool_descriptors",
    }
    assert expected_exports <= set(module.__all__)
    assert expected_exports <= set(public_tools.__all__)
    assert public_tools.iter_builtin_tool_descriptors is iter_builtin_tool_descriptors

    forbidden_exports = {
        "BuiltinToolExecutor",
        "ConnectorAdmission",
        "DefaultToolPreset",
        "MillraceRunner",
        "ToolDispatchMap",
    }
    assert forbidden_exports.isdisjoint(module.__all__)
    assert forbidden_exports.isdisjoint(public_tools.__all__)


def test_descriptor_hashes_are_computed_not_source_of_truth_constants() -> None:
    descriptor = iter_builtin_tool_descriptors()[0]
    changed = descriptor.model_copy(update={"description": "Changed description."})

    assert SHA256_RE.fullmatch(descriptor.descriptor_sha256)
    assert changed.descriptor_sha256 != descriptor.descriptor_sha256


@pytest.mark.parametrize("tool_id", EXPECTED_TOOL_IDS)
def test_all_builtin_tool_references_resolve_exactly(tool_id: str) -> None:
    snapshot = create_builtin_tool_snapshot()

    assert (
        snapshot.resolve_exact(tool_id, 1).classification
        is CatalogLookupClassification.FOUND
    )
    assert (
        snapshot.resolve_exact(tool_id, 2).classification
        is CatalogLookupClassification.MISSING
    )


def _schema_property_names(schema: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    properties = schema.get("properties", {})
    if isinstance(properties, Mapping):
        for name, child in properties.items():
            names.append(str(name))
            if isinstance(child, Mapping):
                names.extend(_schema_property_names(child))
    items = schema.get("items")
    if isinstance(items, Mapping):
        names.extend(_schema_property_names(items))
    return tuple(names)
