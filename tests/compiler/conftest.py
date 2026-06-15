"""Reusable static compiler catalog fixtures."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import pytest

from millforge import CompiledModelProfile, IdempotencyClass, SideEffectClass
from millforge.compiler import (
    ModelProfileCatalogLookup,
    ToolCatalogEntry,
    ToolCatalogLookup,
    admit_model_profile,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
GOLDEN_HARNESS_ID = "millforge.test.golden.compiler.v1"
GOLDEN_POLICY_ID = "millforge.test.golden.policy.v1"
GOLDEN_PROFILE_ID = "profile.golden"


def make_raw_tool_descriptor(
    *,
    tool_id: str = "tools.echo",
    tool_version: int = 1,
    descriptor_sha256: str = SHA_A,
    implementation_id: str = "impl.tools.echo.v1",
    model_tool_name: str = "echo",
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    required_capabilities: tuple[str, ...] = ("workspace.read",),
    produced_artifact_ids: tuple[str, ...] = ("echo_output",),
) -> dict[str, Any]:
    """Build a raw catalog descriptor before compiler admission."""
    return {
        "tool_id": tool_id,
        "tool_version": tool_version,
        "implementation_id": implementation_id,
        "descriptor_sha256": descriptor_sha256,
        "model_tool_name": model_tool_name,
        "description": "Echo test input.",
        "input_schema": input_schema
        or {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        "output_schema": output_schema
        or {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        "side_effect_class": SideEffectClass.READ_ONLY,
        "idempotency": IdempotencyClass.IDEMPOTENT,
        "required_capabilities": required_capabilities,
        "produced_artifact_ids": produced_artifact_ids,
    }


def make_golden_compile_request(
    *,
    source_path: str,
    source_format: Literal["yaml", "json"],
    source_root: str = "/tmp/golden-source",
    output_root: str = "/tmp/golden-output",
) -> Any:
    from millforge import CapabilityEnvelope, CapabilityGrant
    from millforge.compiler import HarnessCompileRequest

    return HarnessCompileRequest(
        request_id="request.golden.compiler.v1",
        source_path=source_path,
        source_root=source_root,
        source_format=source_format,
        output_dir="compiled",
        output_root=output_root,
        expected_harness_id=GOLDEN_HARNESS_ID,
        stage_kind_id="builder",
        legal_terminal_results=("BLOCKED", "BUILDER_COMPLETE"),
        capability_envelope=CapabilityEnvelope(
            grants=(
                CapabilityGrant(capability_id="artifact.write"),
                CapabilityGrant(capability_id="evidence.emit"),
                CapabilityGrant(capability_id="diagnostics.write"),
                CapabilityGrant(capability_id="workspace.read"),
            )
        ),
    )


def make_golden_tool_catalog_snapshot() -> "StaticToolCatalogSnapshot":
    return StaticToolCatalogSnapshot(
        entries={
            ("tools.collect_context", 1): make_raw_tool_descriptor(
                tool_id="tools.collect_context",
                descriptor_sha256="1" * 64,
                implementation_id="impl.tools.collect_context.v1",
                model_tool_name="collect_context",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
                required_capabilities=("workspace.read",),
                produced_artifact_ids=("draft",),
            ),
            ("tools.write_report", 1): make_raw_tool_descriptor(
                tool_id="tools.write_report",
                descriptor_sha256="2" * 64,
                implementation_id="impl.tools.write_report.v1",
                model_tool_name="write_report",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "report": {"type": "string"},
                    },
                    "required": ["path", "report"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"report": {"type": "string"}},
                    "required": ["report"],
                    "additionalProperties": False,
                },
                required_capabilities=("artifact.write", "workspace.read"),
                produced_artifact_ids=("report",),
            ),
            ("tools.write_failure_report", 1): make_raw_tool_descriptor(
                tool_id="tools.write_failure_report",
                descriptor_sha256="3" * 64,
                implementation_id="impl.tools.write_failure_report.v1",
                model_tool_name="write_failure_report",
                input_schema={
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                    "required": ["error"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"failure_report": {"type": "string"}},
                    "required": ["failure_report"],
                    "additionalProperties": False,
                },
                required_capabilities=("artifact.write", "diagnostics.write"),
                produced_artifact_ids=("failure_report",),
            ),
            ("tools.finish_blocked", 1): make_raw_tool_descriptor(
                tool_id="tools.finish_blocked",
                descriptor_sha256="4" * 64,
                implementation_id="impl.tools.finish_blocked.v1",
                model_tool_name="finish_blocked",
                input_schema={
                    "type": "object",
                    "properties": {"result": {"const": "BLOCKED"}},
                    "required": ["result"],
                    "additionalProperties": False,
                },
                required_capabilities=("evidence.emit",),
                produced_artifact_ids=(),
            ),
            ("tools.finish_success", 1): make_raw_tool_descriptor(
                tool_id="tools.finish_success",
                descriptor_sha256="5" * 64,
                implementation_id="impl.tools.finish_success.v1",
                model_tool_name="finish_success",
                input_schema={
                    "type": "object",
                    "properties": {"result": {"const": "BUILDER_COMPLETE"}},
                    "required": ["result"],
                    "additionalProperties": False,
                },
                required_capabilities=("evidence.emit",),
                produced_artifact_ids=(),
            ),
        }
    )


def make_golden_model_profile_catalog_snapshot() -> "StaticModelProfileCatalogSnapshot":
    return StaticModelProfileCatalogSnapshot(
        profiles={GOLDEN_PROFILE_ID: CompiledModelProfile(profile_id=GOLDEN_PROFILE_ID)}
    )


class StaticToolCatalogSnapshot:
    """Test-only exact-version tool snapshot with admitted immutable entries."""

    def __init__(
        self,
        *,
        entries: Mapping[tuple[str, int], Mapping[str, Any] | ToolCatalogEntry],
        snapshot_id: str = SHA_B,
        snapshot_sha256: str = SHA_C,
    ) -> None:
        self._snapshot_id = snapshot_id
        self._snapshot_sha256 = snapshot_sha256
        self._entries = {
            key: (
                value
                if isinstance(value, ToolCatalogEntry)
                else ToolCatalogEntry.admit(
                    value,
                    expected_tool_id=key[0],
                    expected_tool_version=key[1],
                )
            )
            for key, value in entries.items()
        }

    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def snapshot_sha256(self) -> str:
        return self._snapshot_sha256

    def resolve_exact(self, tool_id: str, tool_version: int) -> ToolCatalogLookup:
        entry = self._entries.get((tool_id, tool_version))
        if entry is None:
            return ToolCatalogLookup.missing(
                error_code="tool.missing",
                evidence={"tool_ref": f"{tool_id}@{tool_version}"},
            )
        return ToolCatalogLookup.found(entry)


class StaticModelProfileCatalogSnapshot:
    """Test-only model-profile snapshot with accepted compiled profiles only."""

    def __init__(
        self,
        *,
        profiles: Mapping[str, CompiledModelProfile | Mapping[str, Any]],
        snapshot_id: str = SHA_C,
        snapshot_sha256: str = SHA_B,
    ) -> None:
        self._snapshot_id = snapshot_id
        self._snapshot_sha256 = snapshot_sha256
        self._profiles = {
            profile_id: admit_model_profile(profile, expected_profile_id=profile_id)
            for profile_id, profile in profiles.items()
        }

    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def snapshot_sha256(self) -> str:
        return self._snapshot_sha256

    def resolve_exact(self, profile_id: str) -> ModelProfileCatalogLookup:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return ModelProfileCatalogLookup.missing(
                error_code="profile.missing",
                evidence={"profile_id": profile_id},
            )
        return ModelProfileCatalogLookup.found(profile)


@pytest.fixture
def static_tool_catalog_snapshot() -> StaticToolCatalogSnapshot:
    descriptor = make_raw_tool_descriptor()
    return StaticToolCatalogSnapshot(
        entries={(descriptor["tool_id"], descriptor["tool_version"]): descriptor}
    )


@pytest.fixture
def static_model_profile_catalog_snapshot() -> StaticModelProfileCatalogSnapshot:
    return StaticModelProfileCatalogSnapshot(
        profiles={
            "profile.standard": CompiledModelProfile(profile_id="profile.standard")
        }
    )
