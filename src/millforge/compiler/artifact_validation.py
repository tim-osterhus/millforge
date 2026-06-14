"""Artifact satisfiability validation for semantic compiler resolution."""

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
from millforge.compiler.graph import GraphValidationResult, ResolvedNodeDescriptor
from millforge.compiler.source import HarnessNodeSource, HarnessSource


class ArtifactProducerEvidence(BaseModel):
    """Producer evidence for one artifact ID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: StrictStr
    all_producer_node_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    terminal_gated_producer_node_ids: tuple[StrictStr, ...] = Field(
        default_factory=tuple
    )


class ArtifactValidationResult(BaseModel):
    """Immutable artifact validation result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnostics: tuple[CompilerDiagnostic, ...] = Field(default_factory=tuple)
    producer_evidence: tuple[ArtifactProducerEvidence, ...] = Field(
        default_factory=tuple
    )

    @property
    def ok(self) -> bool:
        return not self.diagnostics


def validate_artifacts(
    source: HarnessSource,
    resolved_nodes: Mapping[str, ToolCatalogEntry | ResolvedNodeDescriptor],
    graph_result: GraphValidationResult,
) -> ArtifactValidationResult:
    """Validate source artifact declarations against resolved producer evidence."""
    nodes_by_id = {node.node_id: node for node in source.graph.nodes}
    descriptors = _resolved_entries(resolved_nodes)
    declared = set(source.artifacts.declared_artifact_ids)
    diagnostics: list[CompilerDiagnostic] = []
    valid_producers: dict[str, list[str]] = defaultdict(list)

    for artifact_id in _duplicates(source.artifacts.declared_artifact_ids):
        diagnostics.append(
            _diagnostic(
                "MF-A006",
                f"Declared artifact {artifact_id!r} is duplicated.",
                artifact_id=artifact_id,
            )
        )

    for node in sorted(source.graph.nodes, key=lambda item: item.node_id):
        descriptor = descriptors.get(node.node_id)
        if descriptor is None:
            continue
        descriptor_artifacts = set(descriptor.produced_artifact_ids)
        for artifact_id in _duplicates(node.produces):
            diagnostics.append(
                _diagnostic(
                    "MF-A006",
                    f"Node {node.node_id!r} duplicates produced artifact {artifact_id!r}.",
                    node_id=node.node_id,
                    artifact_id=artifact_id,
                )
            )
        for artifact_id in sorted(set(node.produces)):
            if artifact_id not in declared:
                diagnostics.append(
                    _diagnostic(
                        "MF-A001",
                        f"Node {node.node_id!r} produces undeclared artifact {artifact_id!r}.",
                        node_id=node.node_id,
                        artifact_id=artifact_id,
                    )
                )
            if artifact_id not in descriptor_artifacts:
                diagnostics.append(
                    _diagnostic(
                        "MF-A002",
                        f"Node {node.node_id!r} cannot produce artifact {artifact_id!r}.",
                        node_id=node.node_id,
                        artifact_id=artifact_id,
                    )
                )
            if artifact_id in declared and artifact_id in descriptor_artifacts:
                valid_producers[artifact_id].append(node.node_id)

    terminal_results = set(graph_result.terminal_result_map.values())
    terminal_nodes_by_result = {
        result: node_id for node_id, result in graph_result.terminal_result_map.items()
    }
    for requirement in source.artifacts.required_by_terminal:
        for artifact_id in _duplicates(requirement.artifact_ids):
            diagnostics.append(
                _diagnostic(
                    "MF-A006",
                    f"Terminal artifact policy {requirement.terminal_result!r} duplicates artifact {artifact_id!r}.",
                    artifact_id=artifact_id,
                    terminal_result=requirement.terminal_result,
                )
            )
        if requirement.terminal_result not in terminal_results:
            diagnostics.append(
                _diagnostic(
                    "MF-A003",
                    f"Artifact policy references unknown terminal result {requirement.terminal_result!r}.",
                    terminal_result=requirement.terminal_result,
                )
            )
            continue
        for artifact_id in sorted(set(requirement.artifact_ids)):
            if artifact_id not in declared:
                diagnostics.append(
                    _diagnostic(
                        "MF-A007",
                        f"Terminal-required artifact {artifact_id!r} is not declared.",
                        artifact_id=artifact_id,
                        terminal_result=requirement.terminal_result,
                    )
                )

    producer_evidence: list[ArtifactProducerEvidence] = []
    terminal_gated: dict[str, set[str]] = defaultdict(set)
    if not graph_result.diagnostics:
        ancestors_by_terminal = {
            terminal_node_id: _ancestors(terminal_node_id, nodes_by_id)
            for terminal_node_id in graph_result.terminal_node_ids
        }
        globally_required = set(graph_result.required_node_ids)
        for requirement in source.artifacts.required_by_terminal:
            terminal_node_id = terminal_nodes_by_result.get(requirement.terminal_result)
            if terminal_node_id is None:
                continue
            gated_nodes = ancestors_by_terminal[terminal_node_id] | globally_required
            for artifact_id in sorted(set(requirement.artifact_ids)):
                if artifact_id not in declared:
                    continue
                producers = set(valid_producers.get(artifact_id, ()))
                gated_producers = producers & gated_nodes
                terminal_gated[artifact_id].update(gated_producers)
                if not producers:
                    diagnostics.append(
                        _diagnostic(
                            "MF-A004",
                            f"Terminal-required artifact {artifact_id!r} has no valid producer.",
                            artifact_id=artifact_id,
                            terminal_result=requirement.terminal_result,
                        )
                    )
                elif not gated_producers:
                    diagnostics.append(
                        _diagnostic(
                            "MF-A005",
                            f"Terminal-required artifact {artifact_id!r} has no terminal-gated producer.",
                            artifact_id=artifact_id,
                            terminal_result=requirement.terminal_result,
                        )
                    )

    for artifact_id in sorted(declared):
        producer_evidence.append(
            ArtifactProducerEvidence(
                artifact_id=artifact_id,
                all_producer_node_ids=tuple(
                    sorted(valid_producers.get(artifact_id, ()))
                ),
                terminal_gated_producer_node_ids=tuple(
                    sorted(terminal_gated.get(artifact_id, ()))
                ),
            )
        )

    return ArtifactValidationResult(
        diagnostics=sort_diagnostics(diagnostics),
        producer_evidence=tuple(producer_evidence),
    )


def _resolved_entries(
    resolved_nodes: Mapping[str, ToolCatalogEntry | ResolvedNodeDescriptor],
) -> dict[str, ToolCatalogEntry]:
    entries: dict[str, ToolCatalogEntry] = {}
    for node_id, value in resolved_nodes.items():
        entries[node_id] = (
            value.descriptor if isinstance(value, ResolvedNodeDescriptor) else value
        )
    return entries


def _ancestors(
    terminal_node_id: str,
    nodes_by_id: Mapping[str, HarnessNodeSource],
) -> set[str]:
    reverse = {
        node.node_id: tuple(prereq.node_id for prereq in node.prerequisites)
        for node in nodes_by_id.values()
    }
    ancestors: set[str] = set()
    stack = list(reverse[terminal_node_id])
    while stack:
        node_id = stack.pop()
        if node_id in ancestors or node_id not in reverse:
            continue
        ancestors.add(node_id)
        stack.extend(reverse[node_id])
    return ancestors


def _duplicates(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))


def _diagnostic(
    code: str,
    message: str,
    *,
    node_id: str | None = None,
    artifact_id: str | None = None,
    terminal_result: str | None = None,
) -> CompilerDiagnostic:
    fields = []
    if artifact_id is not None:
        fields.append(DiagnosticField(key="artifact_id", value=artifact_id))
    if terminal_result is not None:
        fields.append(DiagnosticField(key="terminal_result", value=terminal_result))
    return CompilerDiagnostic(
        code=code,
        phase=CompilerPhase.ARTIFACT,
        severity=DiagnosticSeverity.ERROR,
        message=message,
        node_id=node_id,
        fields=tuple(fields),
    )
