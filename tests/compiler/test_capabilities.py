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


def test_no_required_capabilities_and_unrelated_grants_are_accepted() -> None:
    result = validate_capability_grants(
        {"done": _entry("tools.done", ())},
        CapabilityEnvelope(grants=(CapabilityGrant(capability_id="network.http"),)),
    )

    assert result.ok
    assert result.required_capability_ids == ()


def test_matching_capability_grants_pass_exactly() -> None:
    result = validate_capability_grants(
        {"read": _entry("tools.read", ("workspace.read",))},
        CapabilityEnvelope(grants=(CapabilityGrant(capability_id="workspace.read"),)),
    )

    assert result.ok
    assert result.required_capability_ids == ("workspace.read",)


def test_duplicate_descriptor_capability_declarations_are_collapsed() -> None:
    entry = _entry("tools.read", ("workspace.read",))
    duplicated = entry.model_copy(
        update={"required_capabilities": ("workspace.read", "workspace.read")}
    )

    result = validate_capability_grants(
        {"read": duplicated},
        CapabilityEnvelope(grants=(CapabilityGrant(capability_id="workspace.read"),)),
    )

    assert result.ok
    assert result.required_capability_ids == ("workspace.read",)


def test_missing_capability_lists_all_affected_nodes_deterministically() -> None:
    result = validate_capability_grants(
        {
            "zeta": _entry("tools.zeta", ("artifact.write",)),
            "alpha": _entry("tools.alpha", ("artifact.write",)),
            "read": _entry("tools.read", ("workspace.read",)),
        },
        CapabilityEnvelope(grants=(CapabilityGrant(capability_id="workspace.read"),)),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-C001"]
    assert result.diagnostics[0].node_id == "alpha"
    assert result.diagnostics[0].related_ids == ("zeta",)
    assert result.diagnostics[0].fields[0].value == "artifact.write"


def test_runtime_only_grant_constraints_do_not_block_compile_time_capability_match() -> (
    None
):
    result = validate_capability_grants(
        {"read": _entry("tools.read", ("workspace.read",))},
        CapabilityEnvelope(
            grants=(
                CapabilityGrant(
                    capability_id="workspace.read",
                    constraints={"paths": ("docs",), "runtime_checked": True},
                ),
            )
        ),
    )

    assert result.ok


def test_offline_envelope_accepts_network_capability_tools_by_explicit_grant() -> None:
    result = validate_capability_grants(
        {"fetch": _entry("tools.fetch", ("network.http",))},
        CapabilityEnvelope(grants=(CapabilityGrant(capability_id="network.http"),)),
    )

    assert result.ok
    assert result.required_capability_ids == ("network.http",)


def test_terminal_node_capability_requirements_are_aggregated() -> None:
    result = validate_capability_grants(
        {"complete": _entry("tools.complete", ("evidence.emit",))},
        CapabilityEnvelope(grants=()),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-C001"]
    assert result.diagnostics[0].node_id == "complete"
    assert result.diagnostics[0].fields[0].value == "evidence.emit"
