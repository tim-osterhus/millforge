"""Reusable static compiler catalog fixtures."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
