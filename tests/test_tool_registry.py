from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, cast

import pytest
from pydantic import ValidationError

import millforge
import millforge.tools as public_tools
from millforge import (
    CapabilityEnvelope,
    CapabilityGrant,
    CompiledModelProfile,
    IdempotencyClass,
    SideEffectClass,
)
from millforge.compiler import (
    CatalogLookupClassification,
    CompileInvocation,
    HarnessCompileRequest,
    HarnessSource,
    ModelProfileCatalogLookup,
    ToolCatalogSnapshot,
    capture_catalog_snapshot_metadata,
    compile_semantic,
    lower_resolved_harness,
)
from millforge.tools import (
    DESCRIPTOR_HASH_KIND,
    DESCRIPTOR_SCHEMA_VERSION,
    MAX_CANCELLATION_GRACE_SECONDS,
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_SUMMARY_UTF8,
    MAX_TIMEOUT_SECONDS,
    SNAPSHOT_ID_KIND,
    SNAPSHOT_KIND,
    FrozenDescriptorHashRecord,
    FrozenToolRegistrySnapshot,
    ToolDescriptor,
    ToolOutputPolicy,
    ToolRegistry,
    ToolRegistryError,
    ToolRegistryErrorCode,
    ToolTimeoutPolicy,
    descriptor_hash_payload,
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

BASE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "annotation"},
        "api_key": {"type": "string"},
    },
    "required": ["api_key", "message"],
    "additionalProperties": False,
}
BASE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


class StaticModelSnapshot:
    snapshot_id = "b" * 64
    snapshot_sha256 = "c" * 64

    def resolve_exact(self, profile_id: str) -> ModelProfileCatalogLookup:
        if profile_id != "profile.standard":
            return ModelProfileCatalogLookup.missing(error_code="profile.missing")
        return ModelProfileCatalogLookup.found(
            CompiledModelProfile(profile_id="profile.standard")
        )


def test_descriptor_rejects_unknown_fields_invalid_scalars_and_bad_schema() -> None:
    with pytest.raises(ValidationError):
        _descriptor(descriptor_sha256="a" * 64)
    with pytest.raises(ValidationError):
        _descriptor(tool_id="Bad ID")
    with pytest.raises(ValidationError):
        _descriptor(tool_version=0)
    with pytest.raises(ValidationError):
        _descriptor(input_schema={"type": "object", "additionalProperties": True})
    with pytest.raises(ValidationError):
        _descriptor(input_schema={**BASE_INPUT_SCHEMA, "callable": lambda: None})


def test_descriptor_deep_snapshots_and_normalizes_caller_inputs() -> None:
    schema = {
        "type": "object",
        "properties": {
            "b": {"type": "string", "default": "ignored"},
            "a": {"type": "integer", "description": "ignored"},
        },
        "required": ["b", "a"],
        "additionalProperties": False,
    }
    capabilities = ["cap.beta", "cap.alpha"]
    artifacts = ["artifact.beta", "artifact.alpha"]
    descriptor = _descriptor(
        input_schema=schema,
        required_capabilities=capabilities,
        produced_artifact_ids=artifacts,
    )
    digest = descriptor.descriptor_sha256

    cast(dict[str, Any], schema["properties"])["c"] = {"type": "boolean"}
    capabilities.append("cap.zed")
    artifacts.append("artifact.zed")

    assert descriptor.descriptor_sha256 == digest
    assert tuple(descriptor.input_schema["properties"]) == ("a", "b")
    assert "default" not in descriptor.input_schema["properties"]["b"]
    assert descriptor.required_capabilities == ("cap.alpha", "cap.beta")
    assert descriptor.produced_artifact_ids == ("artifact.alpha", "artifact.beta")
    with pytest.raises(TypeError):
        descriptor.input_schema["x"] = {"type": "string"}  # type: ignore[index]


def test_descriptor_hash_stable_across_semantic_order_and_annotations() -> None:
    left = _descriptor(
        input_schema=BASE_INPUT_SCHEMA,
        required_capabilities=("cap.beta", "cap.alpha"),
        produced_artifact_ids=("artifact.beta", "artifact.alpha"),
    )
    right = _descriptor(
        input_schema={
            "additionalProperties": False,
            "required": ["message", "api_key"],
            "properties": {
                "api_key": {"type": "string", "description": "stripped"},
                "message": {"default": "ignored", "type": "string"},
            },
            "type": "object",
        },
        required_capabilities=("cap.alpha", "cap.beta"),
        produced_artifact_ids=("artifact.alpha", "artifact.beta"),
    )

    assert left.descriptor_sha256 == right.descriptor_sha256
    assert SHA256_RE.fullmatch(left.descriptor_sha256)


@pytest.mark.parametrize(
    "field,update",
    [
        ("schema_version", {"schema_version": 2}),
        ("kind", {"kind": "millforge.tool_descriptor.v2"}),
        ("tool_id", {"tool_id": "test.registry.other"}),
        ("tool_version", {"tool_version": 2}),
        ("implementation_id", {"implementation_id": "impl.test.registry.other.v1"}),
        ("model_tool_name", {"model_tool_name": "other_tool"}),
        ("description", {"description": "Changed description."}),
        (
            "input_schema",
            {
                "input_schema": {
                    "type": "object",
                    "properties": {"message": {"type": "integer"}},
                    "required": ["message"],
                    "additionalProperties": False,
                }
            },
        ),
        (
            "output_schema",
            {
                "output_schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                }
            },
        ),
        ("required_capabilities", {"required_capabilities": ("cap.other",)}),
        ("produced_artifact_ids", {"produced_artifact_ids": ("artifact.other",)}),
        ("side_effect_class", {"side_effect_class": SideEffectClass.ARTIFACT_WRITE}),
        ("idempotency", {"idempotency": IdempotencyClass.NON_IDEMPOTENT}),
        (
            "timeout_policy",
            {
                "timeout_policy": ToolTimeoutPolicy(
                    timeout_seconds=42, cancellation_grace_seconds=5
                )
            },
        ),
        (
            "output_policy",
            {
                "output_policy": ToolOutputPolicy(
                    max_output_bytes=4096,
                    max_summary_utf8=42,
                    redact_secrets=True,
                )
            },
        ),
    ],
)
def test_descriptor_hash_changes_for_hash_covered_fields(
    field: str, update: Mapping[str, Any]
) -> None:
    baseline = _descriptor()

    if field in {"schema_version", "kind"}:
        with pytest.raises(ValidationError):
            _descriptor(**update)
        changed = baseline.model_copy(update=dict(update))
    else:
        changed = _descriptor(**dict(update))

    assert changed.descriptor_sha256 != baseline.descriptor_sha256


def test_policy_validation_and_capability_requirements_are_closed() -> None:
    with pytest.raises(ValidationError):
        ToolTimeoutPolicy(timeout_seconds=0, cancellation_grace_seconds=1)
    with pytest.raises(ValidationError):
        ToolOutputPolicy(
            max_output_bytes=1,
            max_summary_utf8=0,
            redact_secrets=True,
        )
    with pytest.raises(ValidationError):
        _descriptor(
            side_effect_class=SideEffectClass.WORKSPACE_WRITE,
            required_capabilities=(),
        )
    with pytest.raises(ValidationError):
        _descriptor(required_capabilities=("Bad Cap",))
    with pytest.raises(ValidationError):
        _descriptor(
            required_capabilities=(),
            produced_artifact_ids=("artifact.output",),
        )


@pytest.mark.parametrize(
    "side_effect_class",
    [
        SideEffectClass.ARTIFACT_WRITE,
        SideEffectClass.WORKSPACE_WRITE,
        SideEffectClass.PROCESS_EXECUTION,
        SideEffectClass.NETWORK_READ,
        SideEffectClass.NETWORK_WRITE,
        SideEffectClass.TERMINAL,
    ],
)
def test_side_effect_descriptor_families_require_explicit_capabilities(
    side_effect_class: SideEffectClass,
) -> None:
    with pytest.raises(ValidationError):
        _descriptor(
            side_effect_class=side_effect_class,
            required_capabilities=(),
            produced_artifact_ids=(),
        )


def test_secret_values_are_rejected_without_rejecting_secret_like_schema_names() -> (
    None
):
    accepted = _descriptor()
    assert "api_key" in accepted.input_schema["properties"]

    with pytest.raises(ValidationError) as exc:
        _descriptor(description="OPENAI_API_KEY=abcdefghijklmnopqrstuvwxyz")
    assert "abcdefghijklmnopqrstuvwxyz" not in str(exc.value)

    with pytest.raises(ValidationError):
        _descriptor(
            input_schema={
                "type": "object",
                "properties": {"x": {"const": "bearer abcdefghijklmnop"}},
                "required": ["x"],
                "additionalProperties": False,
            }
        )


def test_registry_rejects_duplicates_freezes_and_returns_exact_lookups() -> None:
    registry = ToolRegistry()
    descriptor = _descriptor()
    registry.register(descriptor)

    with pytest.raises(ToolRegistryError) as duplicate_tool:
        registry.register(_descriptor(implementation_id="impl.test.registry.other.v1"))
    assert duplicate_tool.value.code is ToolRegistryErrorCode.DUPLICATE_TOOL

    registry.register(
        _descriptor(
            tool_id="test.registry.other",
            implementation_id="impl.test.registry.other.v1",
            model_tool_name="other_tool",
        )
    )
    with pytest.raises(ToolRegistryError) as duplicate_implementation:
        registry.register(
            _descriptor(
                tool_id="test.registry.third",
                implementation_id="impl.test.registry.other.v1",
                model_tool_name="third_tool",
            )
        )
    assert (
        duplicate_implementation.value.code
        is ToolRegistryErrorCode.DUPLICATE_IMPLEMENTATION
    )

    snapshot = registry.freeze()
    repeated = registry.freeze()
    assert repeated is snapshot
    assert snapshot.snapshot_id == repeated.snapshot_id
    assert snapshot.snapshot_sha256 == repeated.snapshot_sha256
    assert isinstance(snapshot, ToolCatalogSnapshot)
    assert (
        capture_catalog_snapshot_metadata(snapshot).snapshot_id == snapshot.snapshot_id
    )

    found = snapshot.resolve_exact("test.registry.echo", 1)
    missing = snapshot.resolve_exact("test.registry.echo", 2)
    invalid = snapshot.resolve_exact("test.registry.echo", "latest")  # type: ignore[arg-type]
    alias = snapshot.resolve_exact("test.registry.echo.latest", 1)

    assert found.classification is CatalogLookupClassification.FOUND
    assert found.entry is not None
    assert found.entry.descriptor_sha256 == descriptor.descriptor_sha256
    assert missing.classification is CatalogLookupClassification.MISSING
    assert invalid.classification is CatalogLookupClassification.INVALID
    assert alias.classification is CatalogLookupClassification.INVALID
    assert SHA256_RE.fullmatch(snapshot.snapshot_id)
    assert SHA256_RE.fullmatch(snapshot.snapshot_sha256)

    with pytest.raises(ToolRegistryError) as frozen:
        registry.register(
            _descriptor(
                tool_id="test.registry.after",
                implementation_id="impl.test.registry.after.v1",
                model_tool_name="after_tool",
            )
        )
    assert frozen.value.code is ToolRegistryErrorCode.REGISTRY_FROZEN


def test_snapshot_hash_records_are_sorted_immutable_and_order_independent() -> None:
    left = ToolRegistry()
    left.register(
        _descriptor(
            tool_id="test.registry.zed",
            implementation_id="impl.test.registry.zed.v1",
            model_tool_name="zed_tool",
        )
    )
    left.register(_descriptor())

    right = ToolRegistry()
    right.register(_descriptor())
    right.register(
        _descriptor(
            tool_id="test.registry.zed",
            implementation_id="impl.test.registry.zed.v1",
            model_tool_name="zed_tool",
        )
    )

    left_snapshot = left.freeze()
    right_snapshot = right.freeze()

    assert left_snapshot.snapshot_sha256 == right_snapshot.snapshot_sha256
    assert left_snapshot.snapshot_id == right_snapshot.snapshot_id
    assert [
        (record.tool_id, record.tool_version)
        for record in left_snapshot.descriptor_hash_records
    ] == [("test.registry.echo", 1), ("test.registry.zed", 1)]
    with pytest.raises(ValidationError):
        left_snapshot.descriptor_hash_records[0].tool_id = "test.registry.changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        left_snapshot.descriptor_hash_records[0] = (  # type: ignore[index]
            left_snapshot.descriptor_hash_records[1]
        )


@pytest.mark.parametrize(
    "tool_id,tool_version",
    [
        ("latest", 1),
        ("test.registry.echo.latest", 1),
        ("test.registry.echo@1", 1),
        ("test.registry.echo", "latest"),
        ("test.registry.echo", "1"),
        ("test.registry.echo", 0),
        ("test.registry.echo", 1.5),
        (123, 1),
    ],
)
def test_snapshot_lookup_invalid_inputs_fail_closed_without_raising(
    tool_id: Any, tool_version: Any
) -> None:
    registry = ToolRegistry()
    registry.register(_descriptor())
    snapshot = registry.freeze()

    lookup = snapshot.resolve_exact(tool_id, tool_version)

    assert lookup.classification is CatalogLookupClassification.INVALID
    assert lookup.error_code == ToolRegistryErrorCode.LOOKUP_INVALID.value


def test_snapshot_lookup_redacts_secret_bearing_error_evidence() -> None:
    snapshot = ToolRegistry().freeze()

    lookup = snapshot.resolve_exact(
        "OPENAI_API_KEY=abcdefghijklmnopqrstuvwxyz",
        "latest",  # type: ignore[arg-type]
    )

    assert lookup.classification is CatalogLookupClassification.INVALID
    evidence = dict(lookup.evidence)
    assert "abcdefghijklmnopqrstuvwxyz" not in repr(lookup)
    assert evidence["tool_id"] == "**redacted**"


def test_projection_and_compiler_lowering_preserve_registry_hashes() -> None:
    registry = ToolRegistry()
    inspect_descriptor = _descriptor(
        tool_id="test.registry.inspect",
        implementation_id="impl.test.registry.inspect.v1",
        model_tool_name="inspect_request",
        produced_artifact_ids=("report",),
    )
    done_descriptor = _descriptor(
        tool_id="test.registry.done",
        implementation_id="impl.test.registry.done.v1",
        model_tool_name="submit_done",
        input_schema={
            "type": "object",
            "properties": {"result": {"const": "BUILDER_COMPLETE"}},
            "required": ["result"],
            "additionalProperties": False,
        },
        produced_artifact_ids=(),
    )
    registry.register(inspect_descriptor)
    registry.register(done_descriptor)
    snapshot = registry.freeze()

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _source(),
        tool_snapshot=snapshot,
        model_profile_snapshot=StaticModelSnapshot(),
    )
    assert result.diagnostics == ()
    assert result.resolved_harness is not None

    plan = lower_resolved_harness(result.resolved_harness)
    hashes = {
        node.binding.tool_id: node.binding.descriptor_sha256 for node in plan.nodes
    }
    assert hashes == {
        "test.registry.done": done_descriptor.descriptor_sha256,
        "test.registry.inspect": inspect_descriptor.descriptor_sha256,
    }


def test_golden_descriptor_and_snapshot_hash_vector() -> None:
    registry = ToolRegistry()
    registry.register(_descriptor())
    snapshot = registry.freeze()

    assert (
        _descriptor().descriptor_sha256
        == "f2f9adb170666d4c48a41bcc1703a0961366c66dca7d83037358904fb3c48209"
    )
    assert (
        snapshot.snapshot_sha256
        == "aea075b7030fa302acba9f63f1c77bad4f3576b54c630a662c514dcebc4d96e1"
    )
    assert (
        snapshot.snapshot_id
        == "7884ba5119e556d7f6b5c1880fa505134de35c0add35f4f0bab9f9ebb08a714a"
    )


def test_invalid_schema_runtime_secret_and_supplied_hash_paths_fail_closed() -> None:
    with pytest.raises(ValidationError):
        _descriptor(
            input_schema={
                "type": "object",
                "properties": {"x": {"$ref": "#/runtime"}},
                "required": ["x"],
                "additionalProperties": False,
            }
        )

    data = _descriptor().model_dump(mode="json")
    data["descriptor_sha256"] = "a" * 64
    with pytest.raises(ValidationError):
        ToolDescriptor.model_validate(data)


def _descriptor(**updates: Any) -> ToolDescriptor:
    values: dict[str, Any] = {
        "tool_id": "test.registry.echo",
        "tool_version": 1,
        "implementation_id": "impl.test.registry.echo.v1",
        "model_tool_name": "echo_message",
        "description": "Echo a registry test message.",
        "input_schema": BASE_INPUT_SCHEMA,
        "output_schema": BASE_OUTPUT_SCHEMA,
        "required_capabilities": ("cap.registry.read",),
        "produced_artifact_ids": ("artifact.output",),
        "side_effect_class": SideEffectClass.READ_ONLY,
        "idempotency": IdempotencyClass.IDEMPOTENT,
        "timeout_policy": _timeout(),
        "output_policy": _output(),
    }
    values.update(updates)
    return ToolDescriptor(**values)


def _timeout(**updates: Any) -> ToolTimeoutPolicy:
    values = {"timeout_seconds": 30, "cancellation_grace_seconds": 5}
    values.update(updates)
    return ToolTimeoutPolicy(**values)


def _output(**updates: Any) -> ToolOutputPolicy:
    values: dict[str, Any] = {
        "max_output_bytes": 4096,
        "max_summary_utf8": 512,
        "redact_secrets": True,
    }
    values.update(updates)
    return ToolOutputPolicy(**values)


def _request() -> HarnessCompileRequest:
    return HarnessCompileRequest(
        request_id="request.registry.v1",
        source_path="harness.yaml",
        source_root="/tmp",
        source_format="yaml",
        output_dir="out",
        output_root="/tmp",
        expected_harness_id="millforge.test.registry.v1",
        stage_kind_id="builder",
        legal_terminal_results=("BUILDER_COMPLETE",),
        capability_envelope=CapabilityEnvelope(
            grants=(CapabilityGrant(capability_id="cap.registry.read"),)
        ),
    )


def _source() -> HarnessSource:
    return HarnessSource.model_validate(
        {
            "schema_version": "1.0",
            "kind": "millforge_harness",
            "harness_id": "millforge.test.registry.v1",
            "harness_version": 1,
            "stage_scope": {"stage_kind_ids": ["builder"]},
            "model_profile_id": "profile.standard",
            "prompt": {
                "policy_id": "millforge.test.policy.v1",
                "system_instructions": "Complete the request.",
                "include_request_context": True,
            },
            "budgets": {
                "max_iterations": 4,
                "max_validation_retries": 1,
                "max_tool_errors": 1,
                "max_prerequisite_violations": 1,
                "max_premature_terminal_attempts": 1,
            },
            "context": {
                "strategy_id": "forge.tiered.v1",
                "budget_tokens": 12000,
                "keep_recent_iterations": 1,
                "phase_thresholds": [0.6, 0.75, 0.9],
            },
            "graph": {
                "nodes": {
                    "inspect": {
                        "tool_ref": "test.registry.inspect@1",
                        "required": True,
                        "produces": ["report"],
                    },
                    "done": {
                        "tool_ref": "test.registry.done@1",
                        "terminal_result": "BUILDER_COMPLETE",
                        "prerequisites": [{"node_id": "inspect"}],
                    },
                }
            },
            "artifacts": {
                "declared_artifact_ids": ["report"],
                "required_by_terminal": {"BUILDER_COMPLETE": ["report"]},
            },
        }
    )


def test_public_hash_domain_constant_is_generic() -> None:
    assert DESCRIPTOR_HASH_KIND == "millforge.tool_descriptor.v1"
    assert DESCRIPTOR_SCHEMA_VERSION == 1
    assert SNAPSHOT_KIND == "millforge.tool_registry.snapshot.v1"
    assert SNAPSHOT_ID_KIND == "millforge.tool_registry.snapshot_id.v1"
    assert MAX_TIMEOUT_SECONDS == 86_400
    assert MAX_CANCELLATION_GRACE_SECONDS == 3_600
    assert MAX_OUTPUT_BYTES == 64 * 1024 * 1024
    assert MAX_OUTPUT_SUMMARY_UTF8 == 65_536
    assert not DESCRIPTOR_HASH_KIND.startswith("builtin.")
    assert hashlib.sha256(DESCRIPTOR_HASH_KIND.encode("utf-8")).hexdigest()


def test_public_exports_expose_registry_boundary_without_builtins() -> None:
    expected_exports = {
        "DESCRIPTOR_HASH_KIND",
        "DESCRIPTOR_SCHEMA_VERSION",
        "MAX_CANCELLATION_GRACE_SECONDS",
        "MAX_OUTPUT_BYTES",
        "MAX_OUTPUT_SUMMARY_UTF8",
        "MAX_TIMEOUT_SECONDS",
        "SNAPSHOT_ID_KIND",
        "SNAPSHOT_KIND",
        "FrozenDescriptorHashRecord",
        "FrozenToolRegistrySnapshot",
        "ToolDescriptor",
        "ToolOutputPolicy",
        "ToolRegistry",
        "ToolRegistryError",
        "ToolRegistryErrorCode",
        "ToolTimeoutPolicy",
        "descriptor_hash_payload",
    }

    assert expected_exports <= set(public_tools.__all__)
    assert expected_exports <= set(millforge.__all__)
    assert public_tools.ToolDescriptor is ToolDescriptor
    assert public_tools.FrozenDescriptorHashRecord is FrozenDescriptorHashRecord
    assert public_tools.FrozenToolRegistrySnapshot is FrozenToolRegistrySnapshot
    assert public_tools.descriptor_hash_payload is descriptor_hash_payload

    forbidden_exports = {
        "BuiltinToolRegistry",
        "DefaultToolRegistry",
        "ProductionToolPreset",
        "ConnectorAdmission",
        "MillraceRunner",
    }
    assert forbidden_exports.isdisjoint(public_tools.__all__)
