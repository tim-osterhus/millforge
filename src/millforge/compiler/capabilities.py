"""Deterministic capability aggregation for resolved compiler descriptors."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from millforge.compiler.catalogs import ToolCatalogEntry
from millforge.compiler.diagnostics import (
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticField,
    DiagnosticSeverity,
    sort_diagnostics,
)
from millforge.compiler.graph import ResolvedNodeDescriptor
from millforge.contracts import CapabilityEnvelope


class CapabilityValidationResult(BaseModel):
    """Immutable result of capability aggregation and grant comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    required_capability_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    diagnostics: tuple[CompilerDiagnostic, ...] = Field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.diagnostics


def validate_capability_grants(
    resolved_nodes: Mapping[str, ToolCatalogEntry | ResolvedNodeDescriptor],
    envelope: CapabilityEnvelope,
) -> CapabilityValidationResult:
    """Aggregate descriptor-required capabilities and compare exact grants."""
    capability_nodes: dict[str, list[str]] = defaultdict(list)
    for node_id, value in resolved_nodes.items():
        descriptor = (
            value.descriptor if isinstance(value, ResolvedNodeDescriptor) else value
        )
        for capability_id in descriptor.required_capabilities:
            capability_nodes[capability_id].append(node_id)

    required = tuple(sorted(capability_nodes))
    granted = {grant.capability_id for grant in envelope.grants}
    diagnostics = [
        CompilerDiagnostic(
            code="MF-C001",
            phase=CompilerPhase.CAPABILITY,
            severity=DiagnosticSeverity.ERROR,
            message=f"Required capability {capability_id!r} is not granted.",
            node_id=sorted(node_ids)[0],
            related_ids=tuple(sorted(node_ids)[1:]),
            fields=(DiagnosticField(key="capability_id", value=capability_id),),
        )
        for capability_id, node_ids in sorted(capability_nodes.items())
        if capability_id not in granted
    ]
    return CapabilityValidationResult(
        required_capability_ids=required,
        diagnostics=sort_diagnostics(diagnostics),
    )
