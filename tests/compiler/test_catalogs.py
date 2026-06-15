"""Tests for compiler catalog snapshots and immutable entry admission."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from pydantic import ValidationError

from millforge import CompiledModelProfile, IdempotencyClass, SideEffectClass
from millforge.compiler import (
    CatalogLookupClassification,
    CatalogMetadataError,
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticSeverity,
    ModelProfileCatalogLookup,
    ModelProfileCatalogSnapshot,
    RawToolDescriptor,
    ToolCatalogEntry,
    ToolCatalogLookup,
    ToolCatalogSnapshot,
    admit_model_profile,
    capture_catalog_snapshot_metadata,
)

SHA_A = "a" * 64


def make_raw_tool_descriptor(
    *,
    tool_id: str = "tools.echo",
    tool_version: int = 1,
    descriptor_sha256: str = SHA_A,
    input_schema: dict[str, Any] | None = None,
    required_capabilities: tuple[str, ...] = ("workspace.read",),
) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "tool_version": tool_version,
        "implementation_id": "impl.tools.echo.v1",
        "descriptor_sha256": descriptor_sha256,
        "model_tool_name": "echo",
        "description": "Echo test input.",
        "input_schema": input_schema
        or {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "side_effect_class": SideEffectClass.READ_ONLY,
        "idempotency": IdempotencyClass.IDEMPOTENT,
        "required_capabilities": required_capabilities,
        "produced_artifact_ids": ("echo_output",),
    }


def test_resolution_diagnostic_registry_adds_mf_r001_through_mf_r011() -> None:
    diagnostic = CompilerDiagnostic(
        code="MF-R009",
        phase=CompilerPhase.RESOLUTION,
        severity=DiagnosticSeverity.ERROR,
        message="Catalog metadata is invalid.",
    )

    assert diagnostic.code == "MF-R009"


def test_snapshot_metadata_is_captured_once_and_validated() -> None:
    class CountingSnapshot:
        def __init__(self) -> None:
            self.reads: list[str] = []

        @property
        def snapshot_id(self) -> str:
            self.reads.append("id")
            return "a" * 64

        @property
        def snapshot_sha256(self) -> str:
            self.reads.append("sha")
            return "b" * 64

        def resolve_exact(self, tool_id: str, tool_version: int) -> ToolCatalogLookup:
            raise AssertionError("metadata capture must not perform lookup")

    snapshot = CountingSnapshot()
    metadata = capture_catalog_snapshot_metadata(snapshot)

    assert metadata.snapshot_id == "a" * 64
    assert metadata.snapshot_sha256 == "b" * 64
    assert snapshot.reads == ["id", "sha"]


def test_snapshot_metadata_failures_are_mf_r009() -> None:
    class BrokenSnapshot:
        @property
        def snapshot_id(self) -> str:
            raise RuntimeError("sk-test-secret-secret")

        @property
        def snapshot_sha256(self) -> str:
            return "b" * 64

        def resolve_exact(self, profile_id: str) -> ModelProfileCatalogLookup:
            raise AssertionError("metadata failure must not perform lookup")

    with pytest.raises(CatalogMetadataError) as exc_info:
        capture_catalog_snapshot_metadata(BrokenSnapshot())

    assert exc_info.value.diagnostic_code == "MF-R009"
    assert exc_info.value.evidence == (("error_type", "RuntimeError"),)


def test_tool_lookup_outcomes_are_closed_and_redact_evidence() -> None:
    entry = ToolCatalogEntry.admit(
        make_raw_tool_descriptor(),
        expected_tool_id="tools.echo",
        expected_tool_version=1,
    )
    found = ToolCatalogLookup.found(entry)
    missing = ToolCatalogLookup.missing(
        error_code="tool.missing",
        evidence={"api_key": "sk-test-secret-secret"},
    )
    invalid = ToolCatalogLookup.invalid(error_code="tool.invalid")
    unsupported_schema = ToolCatalogLookup.invalid(error_code="unsupported-tool-schema")
    drift = ToolCatalogLookup.invalid(error_code="catalog-snapshot-drift")

    assert found.classification is CatalogLookupClassification.FOUND
    assert found.entry == entry
    assert missing.classification is CatalogLookupClassification.MISSING
    assert missing.evidence == (("api_key", "**redacted**"),)
    assert invalid.classification is CatalogLookupClassification.INVALID
    assert unsupported_schema.error_code == "unsupported-tool-schema"
    assert drift.error_code == "catalog-snapshot-drift"

    with pytest.raises(ValidationError):
        ToolCatalogLookup.model_validate({"classification": "unknown"})
    with pytest.raises(ValidationError):
        ToolCatalogLookup(classification=CatalogLookupClassification.FOUND)


def test_tool_entry_admission_deep_freezes_schema_and_caller_mutation() -> None:
    descriptor = make_raw_tool_descriptor()
    descriptor["input_schema"]["properties"]["message"]["description"] = "before"

    entry = ToolCatalogEntry.admit(
        descriptor,
        expected_tool_id="tools.echo",
        expected_tool_version=1,
    )
    descriptor["input_schema"]["properties"]["message"]["description"] = "after"

    assert isinstance(entry.input_schema, MappingProxyType)
    assert "description" not in entry.input_schema["properties"]["message"]
    with pytest.raises(TypeError):
        entry.input_schema["properties"]["message"]["description"] = "mutated"


def test_tool_entry_admission_validates_identity_hashes_uniqueness_and_runtime_values() -> (
    None
):
    with pytest.raises(ValidationError):
        RawToolDescriptor.model_validate(
            make_raw_tool_descriptor(descriptor_sha256="ABC")
        )

    with pytest.raises(ValidationError):
        RawToolDescriptor.model_validate(
            make_raw_tool_descriptor(
                required_capabilities=("workspace.read", "workspace.read")
            )
        )
    with pytest.raises(ValidationError):
        RawToolDescriptor.model_validate(
            make_raw_tool_descriptor(required_capabilities=("Bad Cap",))
        )

    with pytest.raises(ValueError, match="tool_id"):
        ToolCatalogEntry.admit(
            make_raw_tool_descriptor(tool_id="tools.other"),
            expected_tool_id="tools.echo",
            expected_tool_version=1,
        )

    with pytest.raises(ValueError, match="callables"):
        ToolCatalogEntry.admit(
            make_raw_tool_descriptor(input_schema={"type": lambda: "object"}),
            expected_tool_id="tools.echo",
            expected_tool_version=1,
        )


def test_static_tool_catalog_snapshot_reuses_admitted_entries_and_is_immutable(
    static_tool_catalog_snapshot: Any,
    static_model_profile_catalog_snapshot: Any,
) -> None:
    snapshot = static_tool_catalog_snapshot
    model_snapshot = static_model_profile_catalog_snapshot
    metadata = capture_catalog_snapshot_metadata(snapshot)
    lookup = snapshot.resolve_exact("tools.echo", 1)
    repeated = snapshot.resolve_exact("tools.echo", 1)
    missing = snapshot.resolve_exact("tools.echo", 2)

    assert metadata.snapshot_id == "b" * 64
    assert lookup.classification is CatalogLookupClassification.FOUND
    assert lookup.entry is not None
    assert lookup.entry.input_schema["properties"]["message"]["type"] == "string"
    assert lookup == repeated
    assert missing.classification is CatalogLookupClassification.MISSING

    with pytest.raises(TypeError):
        snapshot._entries[("tools.injected", 1)] = ToolCatalogEntry.admit(  # noqa: SLF001
            make_raw_tool_descriptor(tool_id="tools.injected"),
            expected_tool_id="tools.injected",
            expected_tool_version=1,
        )
    with pytest.raises(TypeError):
        model_snapshot._profiles["profile.injected"] = CompiledModelProfile(  # noqa: SLF001
            profile_id="profile.injected"
        )

    assert snapshot.resolve_exact("tools.injected", 1).classification is (
        CatalogLookupClassification.MISSING
    )
    assert model_snapshot.resolve_exact("profile.injected").classification is (
        CatalogLookupClassification.MISSING
    )


def test_catalog_snapshot_protocols_expose_resolve_exact_boundary(
    static_tool_catalog_snapshot: Any,
    static_model_profile_catalog_snapshot: Any,
) -> None:
    class OldToolSnapshot:
        snapshot_id = "a" * 64
        snapshot_sha256 = "b" * 64

        def lookup_tool(self, tool_id: str, tool_version: int) -> ToolCatalogLookup:
            return ToolCatalogLookup.missing()

    class OldModelProfileSnapshot:
        snapshot_id = "a" * 64
        snapshot_sha256 = "b" * 64

        def lookup_model_profile(self, profile_id: str) -> ModelProfileCatalogLookup:
            return ModelProfileCatalogLookup.missing()

    assert isinstance(static_tool_catalog_snapshot, ToolCatalogSnapshot)
    assert isinstance(
        static_model_profile_catalog_snapshot,
        ModelProfileCatalogSnapshot,
    )
    assert not isinstance(OldToolSnapshot(), ToolCatalogSnapshot)
    assert not isinstance(OldModelProfileSnapshot(), ModelProfileCatalogSnapshot)


def test_model_profile_admission_accepts_only_compiled_profile_contract() -> None:
    profile = admit_model_profile(
        {"profile_id": "profile.standard"},
        expected_profile_id="profile.standard",
    )

    assert isinstance(profile, CompiledModelProfile)
    assert profile.profile_id == "profile.standard"

    rejected: list[dict[str, Any]] = [
        {"profile_id": "profile.standard", "provider_endpoint": "https://example.com"},
        {"profile_id": "profile.standard", "api_key": "sk-test-secret-secret"},
        {"profile_id": "profile.other"},
    ]
    for payload in rejected:
        with pytest.raises(ValueError):
            admit_model_profile(payload, expected_profile_id="profile.standard")


def test_static_model_profile_snapshot_lookup_is_closed(
    static_model_profile_catalog_snapshot: Any,
) -> None:
    snapshot = static_model_profile_catalog_snapshot
    found = snapshot.resolve_exact("profile.standard")
    missing = snapshot.resolve_exact("profile.other")

    assert capture_catalog_snapshot_metadata(snapshot).snapshot_sha256 == "b" * 64
    assert found.classification is CatalogLookupClassification.FOUND
    assert found.profile == CompiledModelProfile(profile_id="profile.standard")
    assert missing.classification is CatalogLookupClassification.MISSING
    with pytest.raises(ValidationError):
        ModelProfileCatalogLookup(classification=CatalogLookupClassification.FOUND)
