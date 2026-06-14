"""Deterministic semantic graph and argument validation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from millforge.compiler.catalogs import ToolCatalogEntry
from millforge.compiler.diagnostics import (
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticField,
    DiagnosticSeverity,
    sort_diagnostics,
)
from millforge.compiler.schema_validation import property_schema_compatibility_bytes
from millforge.compiler.source import HarnessNodeSource, HarnessSource
from millforge.compiler.validators import validate_node_id


class ResolvedNodeDescriptor(BaseModel):
    """Source node paired with its admitted catalog descriptor."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    node_id: StrictStr
    source: HarnessNodeSource
    descriptor: ToolCatalogEntry

    @field_validator("node_id")
    @classmethod
    def _node_id_valid(cls, value: str) -> str:
        return validate_node_id(value)


class GraphValidationResult(BaseModel):
    """Deterministic graph validation output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnostics: tuple[CompilerDiagnostic, ...] = Field(default_factory=tuple)
    terminal_node_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    terminal_result_map: Mapping[StrictStr, StrictStr] = Field(default_factory=dict)
    required_node_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    @model_validator(mode="after")
    def _freeze_terminal_result_map(self) -> GraphValidationResult:
        object.__setattr__(
            self,
            "terminal_result_map",
            MappingProxyType(dict(sorted(self.terminal_result_map.items()))),
        )
        return self

    @field_serializer("terminal_result_map")
    def _serialize_terminal_result_map(
        self, value: Mapping[StrictStr, StrictStr]
    ) -> dict[str, str]:
        return dict(value)


def validate_harness_graph(
    source: HarnessSource,
    resolved_nodes: Mapping[str, ToolCatalogEntry | ResolvedNodeDescriptor],
    *,
    allowed_terminal_results: Iterable[str] | None = None,
) -> GraphValidationResult:
    """Validate source graph topology and top-level argument matches."""
    nodes_by_id = {node.node_id: node for node in source.graph.nodes}
    resolved_by_id = _resolved_entries(resolved_nodes)
    allowed_results = (
        None
        if allowed_terminal_results is None
        else frozenset(allowed_terminal_results)
    )
    diagnostics: list[CompilerDiagnostic] = []

    duplicate_edges = _duplicate_prerequisite_pairs(source.graph.nodes)
    unknown_edges = _unknown_prerequisite_pairs(source.graph.nodes, nodes_by_id)
    self_edges = _self_prerequisite_pairs(source.graph.nodes)
    terminal_as_prereq_edges = _terminal_as_prerequisite_pairs(
        source.graph.nodes, nodes_by_id
    )
    invalid_edges = (
        duplicate_edges | unknown_edges | self_edges | terminal_as_prereq_edges
    )

    diagnostics.extend(
        _diagnostic(
            "MF-G001",
            f"Node {dependent!r} references unknown prerequisite {prereq!r}.",
            node_id=dependent,
            fields={"prerequisite": prereq},
        )
        for dependent, prereq in sorted(unknown_edges)
    )
    diagnostics.extend(
        _diagnostic(
            "MF-G002",
            f"Node {node_id!r} cannot require itself.",
            node_id=node_id,
        )
        for node_id, _ in sorted(self_edges)
    )
    diagnostics.extend(
        _diagnostic(
            "MF-G003",
            f"Node {dependent!r} declares duplicate prerequisite {prereq!r}.",
            node_id=dependent,
            fields={"prerequisite": prereq},
        )
        for dependent, prereq in sorted(duplicate_edges)
    )
    diagnostics.extend(
        _diagnostic(
            "MF-G013",
            f"Terminal node {prereq!r} cannot be a prerequisite.",
            node_id=dependent,
            related_ids=(prereq,),
        )
        for dependent, prereq in sorted(terminal_as_prereq_edges)
    )

    adjacency = _valid_adjacency(source.graph.nodes, invalid_edges)
    cycle_nodes = _cycle_nodes(adjacency)
    for component in _cycle_components(adjacency):
        diagnostics.append(
            _diagnostic(
                "MF-G004",
                f"Prerequisite cycle includes {', '.join(component)}.",
                node_id=component[0],
                related_ids=component[1:],
            )
        )

    terminal_nodes = sorted(
        (node for node in source.graph.nodes if node.terminal_result is not None),
        key=lambda item: item.node_id,
    )
    terminal_node_ids = tuple(node.node_id for node in terminal_nodes)
    terminal_result_map = {
        node.node_id: node.terminal_result
        for node in terminal_nodes
        if node.terminal_result is not None
    }
    required_node_ids = tuple(
        sorted(node.node_id for node in source.graph.nodes if node.required)
    )

    if not terminal_nodes:
        diagnostics.append(
            _diagnostic("MF-G011", "Graph must contain at least one terminal node.")
        )

    terminal_results: dict[str, str] = {}
    duplicate_terminal_results: set[str] = set()
    seen_terminal_results: set[str] = set()
    for node in terminal_nodes:
        assert node.terminal_result is not None
        seen_terminal_results.add(node.terminal_result)
        if allowed_results is not None and node.terminal_result not in allowed_results:
            diagnostics.append(
                _diagnostic(
                    "MF-G009",
                    f"Terminal result {node.terminal_result!r} is not allowed.",
                    node_id=node.node_id,
                    fields={"terminal_result": node.terminal_result},
                )
            )
        previous = terminal_results.setdefault(node.terminal_result, node.node_id)
        if previous != node.node_id:
            duplicate_terminal_results.add(node.terminal_result)
    for terminal_result in sorted(duplicate_terminal_results):
        related_ids = tuple(
            node.node_id
            for node in terminal_nodes
            if node.terminal_result == terminal_result
        )
        diagnostics.append(
            _diagnostic(
                "MF-G010",
                f"Terminal result {terminal_result!r} is used by multiple nodes.",
                node_id=related_ids[0],
                related_ids=related_ids[1:],
                fields={"terminal_result": terminal_result},
            )
        )
    if allowed_results is not None:
        for terminal_result in sorted(allowed_results - seen_terminal_results):
            diagnostics.append(
                _diagnostic(
                    "MF-G011",
                    f"Terminal result {terminal_result!r} has no terminal node.",
                    fields={"terminal_result": terminal_result},
                )
            )

    for node in sorted(source.graph.nodes, key=lambda item: item.node_id):
        if node.required and node.terminal_result is not None:
            diagnostics.append(
                _diagnostic(
                    "MF-G008",
                    f"Terminal node {node.node_id!r} cannot be required.",
                    node_id=node.node_id,
                )
            )

    satisfiable = _satisfiable_nodes(nodes_by_id, adjacency, cycle_nodes, invalid_edges)
    unresolved_blocked = _nodes_blocked_by_unresolved(
        nodes_by_id, adjacency, resolved_by_id
    )
    for node in sorted(source.graph.nodes, key=lambda item: item.node_id):
        if node.node_id in cycle_nodes:
            continue
        if node.node_id in unresolved_blocked:
            continue
        if any(
            (node.node_id, prereq.node_id) in invalid_edges
            for prereq in node.prerequisites
        ):
            continue
        if node.node_id not in satisfiable:
            if node.terminal_result is not None:
                code = "MF-G006"
            elif node.required:
                code = "MF-G007"
            else:
                code = "MF-G005"
            diagnostics.append(
                _diagnostic(
                    code,
                    f"Node {node.node_id!r} is not reachable from graph roots.",
                    node_id=node.node_id,
                )
            )

    diagnostics.extend(
        _argument_diagnostics(
            source.graph.nodes,
            nodes_by_id,
            resolved_by_id,
            invalid_edges=invalid_edges,
        )
    )

    return GraphValidationResult(
        diagnostics=sort_diagnostics(diagnostics),
        terminal_node_ids=terminal_node_ids,
        terminal_result_map=terminal_result_map,
        required_node_ids=required_node_ids,
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


def _duplicate_prerequisite_pairs(
    nodes: Sequence[HarnessNodeSource],
) -> set[tuple[str, str]]:
    duplicates: set[tuple[str, str]] = set()
    for node in nodes:
        seen: set[str] = set()
        for prerequisite in node.prerequisites:
            if prerequisite.node_id in seen:
                duplicates.add((node.node_id, prerequisite.node_id))
            seen.add(prerequisite.node_id)
    return duplicates


def _unknown_prerequisite_pairs(
    nodes: Sequence[HarnessNodeSource],
    nodes_by_id: Mapping[str, HarnessNodeSource],
) -> set[tuple[str, str]]:
    return {
        (node.node_id, prerequisite.node_id)
        for node in nodes
        for prerequisite in node.prerequisites
        if prerequisite.node_id not in nodes_by_id
    }


def _self_prerequisite_pairs(
    nodes: Sequence[HarnessNodeSource],
) -> set[tuple[str, str]]:
    return {
        (node.node_id, prerequisite.node_id)
        for node in nodes
        for prerequisite in node.prerequisites
        if prerequisite.node_id == node.node_id
    }


def _terminal_as_prerequisite_pairs(
    nodes: Sequence[HarnessNodeSource],
    nodes_by_id: Mapping[str, HarnessNodeSource],
) -> set[tuple[str, str]]:
    return {
        (node.node_id, prerequisite.node_id)
        for node in nodes
        for prerequisite in node.prerequisites
        if (
            prerequisite.node_id in nodes_by_id
            and nodes_by_id[prerequisite.node_id].terminal_result is not None
        )
    }


def _valid_adjacency(
    nodes: Sequence[HarnessNodeSource],
    invalid_edges: set[tuple[str, str]],
) -> dict[str, tuple[str, ...]]:
    adjacency: dict[str, list[str]] = {node.node_id: [] for node in nodes}
    for node in nodes:
        for prerequisite in node.prerequisites:
            if (node.node_id, prerequisite.node_id) not in invalid_edges:
                adjacency[prerequisite.node_id].append(node.node_id)
    return {key: tuple(sorted(set(value))) for key, value in adjacency.items()}


def _reverse_adjacency(
    nodes_by_id: Mapping[str, HarnessNodeSource],
    adjacency: Mapping[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    reverse: dict[str, list[str]] = {node_id: [] for node_id in nodes_by_id}
    for prerequisite, dependents in adjacency.items():
        for dependent in dependents:
            reverse[dependent].append(prerequisite)
    return {key: tuple(sorted(value)) for key, value in reverse.items()}


def _cycle_nodes(adjacency: Mapping[str, tuple[str, ...]]) -> set[str]:
    return {
        node_id for component in _cycle_components(adjacency) for node_id in component
    }


def _cycle_components(
    adjacency: Mapping[str, tuple[str, ...]],
) -> list[tuple[str, ...]]:
    index = 0
    stack: list[str] = []
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node_id: str) -> None:
        nonlocal index
        indices[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for dependent in adjacency.get(node_id, ()):
            if dependent not in indices:
                visit(dependent)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[dependent])
            elif dependent in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[dependent])
        if lowlinks[node_id] != indices[node_id]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node_id:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for node_id in sorted(adjacency):
        if node_id not in indices:
            visit(node_id)
    return sorted(components)


def _satisfiable_nodes(
    nodes_by_id: Mapping[str, HarnessNodeSource],
    adjacency: Mapping[str, tuple[str, ...]],
    cycle_nodes: set[str],
    invalid_edges: set[tuple[str, str]],
) -> set[str]:
    reverse = _reverse_adjacency(nodes_by_id, adjacency)
    memo: dict[str, bool] = {}

    def satisfiable(node_id: str) -> bool:
        if node_id in memo:
            return memo[node_id]
        if node_id in cycle_nodes:
            memo[node_id] = False
            return False
        node = nodes_by_id[node_id]
        if any(
            (node.node_id, prereq.node_id) in invalid_edges
            for prereq in node.prerequisites
        ):
            memo[node_id] = False
            return False
        memo[node_id] = all(satisfiable(prereq_id) for prereq_id in reverse[node_id])
        return memo[node_id]

    return {node_id for node_id in sorted(nodes_by_id) if satisfiable(node_id)}


def _nodes_blocked_by_unresolved(
    nodes_by_id: Mapping[str, HarnessNodeSource],
    adjacency: Mapping[str, tuple[str, ...]],
    resolved_by_id: Mapping[str, ToolCatalogEntry],
) -> set[str]:
    unresolved = set(nodes_by_id) - set(resolved_by_id)
    if not unresolved:
        return set()
    blocked = set(unresolved)
    stack = sorted(unresolved)
    while stack:
        current = stack.pop()
        for dependent in adjacency.get(current, ()):
            if dependent in blocked:
                continue
            blocked.add(dependent)
            stack.append(dependent)
    return blocked


def _argument_diagnostics(
    nodes: Sequence[HarnessNodeSource],
    nodes_by_id: Mapping[str, HarnessNodeSource],
    resolved_by_id: Mapping[str, ToolCatalogEntry],
    *,
    invalid_edges: set[tuple[str, str]],
) -> list[CompilerDiagnostic]:
    diagnostics: list[CompilerDiagnostic] = []
    for node in sorted(nodes, key=lambda item: item.node_id):
        current_descriptor = resolved_by_id.get(node.node_id)
        if current_descriptor is None:
            continue
        current_required = _required_properties(current_descriptor.input_schema)
        current_properties = _schema_properties(current_descriptor.input_schema)
        for prerequisite in sorted(node.prerequisites, key=lambda item: item.node_id):
            if (node.node_id, prerequisite.node_id) in invalid_edges:
                continue
            prior_descriptor = resolved_by_id.get(prerequisite.node_id)
            if prior_descriptor is None or prerequisite.node_id not in nodes_by_id:
                continue
            prior_required = _required_properties(prior_descriptor.input_schema)
            prior_properties = _schema_properties(prior_descriptor.input_schema)
            diagnostics.extend(
                _argument_edge_diagnostics(
                    node_id=node.node_id,
                    prerequisite_id=prerequisite.node_id,
                    matches=prerequisite.argument_matches,
                    prior_required=prior_required,
                    prior_properties=prior_properties,
                    current_required=current_required,
                    current_properties=current_properties,
                )
            )
    return diagnostics


def _argument_edge_diagnostics(
    *,
    node_id: str,
    prerequisite_id: str,
    matches: Sequence[Any],
    prior_required: set[str],
    prior_properties: Mapping[str, Any],
    current_required: set[str],
    current_properties: Mapping[str, Any],
) -> list[CompilerDiagnostic]:
    diagnostics: list[CompilerDiagnostic] = []
    prior_seen: dict[str, str] = {}
    current_seen: dict[str, str] = {}
    for match in sorted(
        matches, key=lambda item: (item.prior_argument, item.current_argument)
    ):
        fields = {
            "prerequisite": prerequisite_id,
            "prior_argument": match.prior_argument,
            "current_argument": match.current_argument,
        }
        if (
            match.prior_argument in prior_seen
            and prior_seen[match.prior_argument] != match.current_argument
        ):
            diagnostics.append(
                _diagnostic(
                    "MF-G012",
                    "Argument match fans out from one prerequisite argument.",
                    node_id=node_id,
                    related_ids=(prerequisite_id,),
                    fields=fields,
                )
            )
        if (
            match.current_argument in current_seen
            and current_seen[match.current_argument] != match.prior_argument
        ):
            diagnostics.append(
                _diagnostic(
                    "MF-G012",
                    "Argument match fans in to one current argument.",
                    node_id=node_id,
                    related_ids=(prerequisite_id,),
                    fields=fields,
                )
            )
        prior_seen.setdefault(match.prior_argument, match.current_argument)
        current_seen.setdefault(match.current_argument, match.prior_argument)

        if match.prior_argument not in prior_properties:
            diagnostics.append(
                _diagnostic(
                    "MF-G012",
                    "Argument match references unknown prerequisite argument.",
                    node_id=node_id,
                    related_ids=(prerequisite_id,),
                    fields=fields,
                )
            )
            continue
        if match.current_argument not in current_properties:
            diagnostics.append(
                _diagnostic(
                    "MF-G012",
                    "Argument match references unknown current argument.",
                    node_id=node_id,
                    related_ids=(prerequisite_id,),
                    fields=fields,
                )
            )
            continue
        if match.prior_argument not in prior_required:
            diagnostics.append(
                _diagnostic(
                    "MF-G012",
                    "Prerequisite argument match source must be required.",
                    node_id=node_id,
                    related_ids=(prerequisite_id,),
                    fields=fields,
                )
            )
            continue
        if match.current_argument not in current_required:
            diagnostics.append(
                _diagnostic(
                    "MF-G012",
                    "Current argument match target must be required.",
                    node_id=node_id,
                    related_ids=(prerequisite_id,),
                    fields=fields,
                )
            )
            continue
        if property_schema_compatibility_bytes(
            prior_properties[match.prior_argument]
        ) != (
            property_schema_compatibility_bytes(
                current_properties[match.current_argument]
            )
        ):
            diagnostics.append(
                _diagnostic(
                    "MF-G012",
                    "Argument match schemas are not compatible.",
                    node_id=node_id,
                    related_ids=(prerequisite_id,),
                    fields=fields,
                )
            )
    return diagnostics


def _schema_properties(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    properties = schema.get("properties")
    return properties if isinstance(properties, Mapping) else {}


def _required_properties(schema: Mapping[str, Any]) -> set[str]:
    required = schema.get("required")
    return set(required) if isinstance(required, tuple | list) else set()


def _diagnostic(
    code: str,
    message: str,
    *,
    node_id: str | None = None,
    related_ids: tuple[str, ...] = (),
    fields: Mapping[str, str] | None = None,
) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        phase=CompilerPhase.GRAPH,
        severity=DiagnosticSeverity.ERROR,
        message=message,
        node_id=node_id,
        related_ids=related_ids,
        fields=tuple(
            DiagnosticField(key=key, value=value)
            for key, value in sorted((fields or {}).items())
        ),
    )
