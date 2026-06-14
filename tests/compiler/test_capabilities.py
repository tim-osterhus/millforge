"""Capability aggregation tests."""

from __future__ import annotations

from millforge import CapabilityEnvelope, CapabilityGrant
from millforge.compiler import (
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticSeverity,
    ToolCatalogEntry,
    validate_capability_grants,
)
from tests.compiler.conftest import make_raw_tool_descriptor


def _entry(tool_id: str, capabilities: tuple[str, ...]) -> ToolCatalogEntry:
    return ToolCatalogEntry.admit(
        make_raw_tool_descriptor(
            tool_id=tool_id,
            implementation_id=f"impl.{tool_id}.v1",
            model_tool_name=tool_id.replace(".", "_"),
            required_capabilities=capabilities,
            produced_artifact_ids=(),
        ),
        expected_tool_id=tool_id,
        expected_tool_version=1,
    )


def test_capability_diagnostic_registry_adds_mf_c001() -> None:
    diagnostic = CompilerDiagnostic(
        code="MF-C001",
        phase=CompilerPhase.CAPABILITY,
        severity=DiagnosticSeverity.ERROR,
        message="Missing capability.",
    )

    assert diagnostic.code == "MF-C001"


def test_required_capabilities_are_aggregated_from_descriptors_only() -> None:
    result = validate_capability_grants(
        {
            "write": _entry("tools.write", ("artifact.write", "workspace.read")),
            "read": _entry("tools.read", ("workspace.read",)),
        },
        CapabilityEnvelope(grants=(CapabilityGrant(capability_id="workspace.read"),)),
    )

    assert result.required_capability_ids == ("artifact.write", "workspace.read")
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-C001"]
    assert result.diagnostics[0].fields[0].value == "artifact.write"


def test_matching_capability_grants_pass_exactly() -> None:
    result = validate_capability_grants(
        {"read": _entry("tools.read", ("workspace.read",))},
        CapabilityEnvelope(grants=(CapabilityGrant(capability_id="workspace.read"),)),
    )

    assert result.ok
    assert result.required_capability_ids == ("workspace.read",)
