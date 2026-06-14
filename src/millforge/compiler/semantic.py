"""Private semantic compiler orchestration and resolved IR."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_serializer,
    model_validator,
)

from millforge import CompiledModelProfile
from millforge.compiler.artifact_validation import (
    ArtifactProducerEvidence,
    ArtifactValidationResult,
    validate_artifacts,
)
from millforge.compiler.capabilities import (
    CapabilityValidationResult,
    validate_capability_grants,
)
from millforge.compiler.catalogs import (
    CatalogLookupClassification,
    CatalogMetadataError,
    CatalogSnapshotMetadata,
    ModelProfileCatalogSnapshot,
    ToolCatalogEntry,
    ToolCatalogLookup,
    ToolCatalogSnapshot,
    capture_catalog_snapshot_metadata,
)
from millforge.compiler.diagnostics import (
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticField,
    DiagnosticSeverity,
    bound_diagnostics,
)
from millforge.compiler.graph import (
    GraphValidationResult,
    ResolvedNodeDescriptor,
    validate_harness_graph,
)
from millforge.compiler.requests import (
    CompileInvocation,
    HarnessCompileResult,
    HarnessRequestAdmissionResult,
)
from millforge.compiler.source import HarnessNodeSource, HarnessSource
from millforge.compiler.validators import ToolReference, parse_tool_reference

_TOOL_LOOKUP_ERROR_CODE_UNSUPPORTED_SCHEMA = "unsupported-tool-schema"
_TOOL_LOOKUP_ERROR_CODE_DRIFT = "catalog-snapshot-drift"


class ResolvedToolBindingRef(BaseModel):
    """Exact resolved tool binding evidence without lowering imports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: StrictStr
    tool_version: int = Field(gt=0)
    descriptor_sha256: StrictStr
    implementation_id: StrictStr


class ResolvedToolBinding(BaseModel):
    """One node's exact resolved descriptor and lowering-ready binding evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    node_id: StrictStr
    source: HarnessNodeSource
    descriptor: ToolCatalogEntry
    binding: ResolvedToolBindingRef


class ResolvedHarness(BaseModel):
    """Private immutable semantic IR consumed by later lowering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation: CompileInvocation
    source: HarnessSource
    tool_snapshot: CatalogSnapshotMetadata
    model_profile_snapshot: CatalogSnapshotMetadata
    model_profile: CompiledModelProfile
    resolved_nodes: tuple[ResolvedToolBinding, ...]
    terminal_node_ids: tuple[StrictStr, ...]
    terminal_result_map: Mapping[StrictStr, StrictStr]
    required_node_ids: tuple[StrictStr, ...]
    required_capability_ids: tuple[StrictStr, ...]
    artifact_evidence: tuple[ArtifactProducerEvidence, ...]

    @model_validator(mode="after")
    def _freeze_terminal_result_map(self) -> ResolvedHarness:
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


class SemanticCompileResult(BaseModel):
    """Private semantic boundary result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolved_harness: ResolvedHarness | None = None
    frontend_result: HarnessCompileResult | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = Field(default_factory=tuple)
    graph_result: GraphValidationResult | None = None
    capability_result: CapabilityValidationResult | None = None
    artifact_result: ArtifactValidationResult | None = None

    @property
    def ok(self) -> bool:
        return self.resolved_harness is not None and not self.diagnostics


def compile_semantic_from_admission(
    admission: HarnessRequestAdmissionResult,
    source: HarnessSource | None,
    *,
    tool_snapshot: ToolCatalogSnapshot,
    model_profile_snapshot: ModelProfileCatalogSnapshot,
) -> SemanticCompileResult:
    """Return 03A failures unchanged, otherwise enter semantic compilation."""
    if admission.result is not None:
        return SemanticCompileResult(
            frontend_result=admission.result,
            diagnostics=admission.result.diagnostics,
        )
    if source is None:
        raise ValueError("successful admission requires a parsed HarnessSource")
    assert admission.request is not None
    return compile_semantic(
        CompileInvocation.from_request(admission.request),
        source,
        tool_snapshot=tool_snapshot,
        model_profile_snapshot=model_profile_snapshot,
    )


def compile_semantic(
    invocation: CompileInvocation,
    source: HarnessSource,
    *,
    tool_snapshot: ToolCatalogSnapshot,
    model_profile_snapshot: ModelProfileCatalogSnapshot,
) -> SemanticCompileResult:
    """Resolve semantic compiler state without lowering or side effects."""
    diagnostics: list[CompilerDiagnostic] = []
    parsed_refs, preflight_diagnostics = _parse_unique_tool_refs(source)
    diagnostics.extend(preflight_diagnostics)
    if preflight_diagnostics:
        return SemanticCompileResult(diagnostics=bound_diagnostics(diagnostics))
    try:
        tool_metadata = capture_catalog_snapshot_metadata(tool_snapshot)
        model_metadata = capture_catalog_snapshot_metadata(model_profile_snapshot)
    except CatalogMetadataError as exc:
        return SemanticCompileResult(
            diagnostics=(
                _diagnostic(
                    exc.diagnostic_code,
                    "Catalog snapshot metadata is invalid.",
                    fields=dict(exc.evidence),
                ),
            )
        )

    model_profile = _lookup_model_profile(
        source.model_profile_id,
        model_profile_snapshot,
        diagnostics,
    )

    resolved_nodes = _lookup_tool_bindings(
        source,
        parsed_refs,
        tool_snapshot,
        diagnostics,
    )
    resolved_descriptors = {
        node.node_id: ResolvedNodeDescriptor(
            node_id=node.node_id,
            source=node.source,
            descriptor=node.descriptor,
        )
        for node in resolved_nodes
    }

    diagnostics.extend(_duplicate_model_tool_name_diagnostics(resolved_nodes))
    graph_result = validate_harness_graph(
        source,
        resolved_descriptors,
        allowed_terminal_results=invocation.request.legal_terminal_results,
    )
    diagnostics.extend(graph_result.diagnostics)

    capability_result = validate_capability_grants(
        resolved_descriptors,
        invocation.request.capability_envelope,
    )
    diagnostics.extend(capability_result.diagnostics)

    artifact_result = validate_artifacts(source, resolved_descriptors, graph_result)
    diagnostics.extend(artifact_result.diagnostics)
    bounded = bound_diagnostics(diagnostics)
    if bounded or model_profile is None:
        return SemanticCompileResult(
            diagnostics=bounded,
            graph_result=graph_result,
            capability_result=capability_result,
            artifact_result=artifact_result,
        )

    return SemanticCompileResult(
        resolved_harness=ResolvedHarness(
            invocation=invocation.model_copy(deep=True),
            source=source.model_copy(deep=True),
            tool_snapshot=tool_metadata,
            model_profile_snapshot=model_metadata,
            model_profile=model_profile.model_copy(deep=True),
            resolved_nodes=tuple(sorted(resolved_nodes, key=lambda item: item.node_id)),
            terminal_node_ids=graph_result.terminal_node_ids,
            terminal_result_map=dict(sorted(graph_result.terminal_result_map.items())),
            required_node_ids=graph_result.required_node_ids,
            required_capability_ids=capability_result.required_capability_ids,
            artifact_evidence=artifact_result.producer_evidence,
        ),
        graph_result=graph_result,
        capability_result=capability_result,
        artifact_result=artifact_result,
    )


def _parse_unique_tool_refs(
    source: HarnessSource,
) -> tuple[dict[str, ToolReference], tuple[CompilerDiagnostic, ...]]:
    refs: dict[str, ToolReference] = {}
    seen_bindings: dict[tuple[str, int], str] = {}
    diagnostics: list[CompilerDiagnostic] = []
    for node in sorted(source.graph.nodes, key=lambda item: item.node_id):
        try:
            reference = parse_tool_reference(node.tool_ref)
        except ValueError:
            diagnostics.append(
                _diagnostic(
                    "MF-R011",
                    f"Node {node.node_id!r} has invalid exact tool reference.",
                    node_id=node.node_id,
                    fields={"tool_ref": node.tool_ref},
                )
            )
            continue
        key = (reference.tool_id, reference.version)
        previous = seen_bindings.setdefault(key, node.node_id)
        if previous != node.node_id:
            diagnostics.append(
                _diagnostic(
                    "MF-R005",
                    f"Exact tool binding {node.tool_ref!r} is used by multiple nodes.",
                    node_id=previous,
                    related_ids=(node.node_id,),
                    fields={"tool_ref": node.tool_ref},
                )
            )
        refs[node.node_id] = reference
    return refs, tuple(diagnostics)


def _lookup_model_profile(
    profile_id: str,
    snapshot: ModelProfileCatalogSnapshot,
    diagnostics: list[CompilerDiagnostic],
) -> CompiledModelProfile | None:
    try:
        lookup = snapshot.resolve_exact(profile_id)
    except Exception as exc:
        diagnostics.append(
            _diagnostic(
                "MF-R009",
                "Model profile catalog lookup failed.",
                fields={"error_type": type(exc).__name__, "profile_id": profile_id},
            )
        )
        return None
    if lookup.classification is CatalogLookupClassification.FOUND:
        if lookup.profile is None:
            diagnostics.append(
                _diagnostic(
                    "MF-R010",
                    "Model profile lookup returned no profile payload.",
                    fields={"profile_id": profile_id},
                )
            )
            return None
        if lookup.profile.profile_id != profile_id:
            diagnostics.append(
                _diagnostic(
                    "MF-R010",
                    "Model profile lookup returned a mismatched profile.",
                    fields={
                        "profile_id": profile_id,
                        "resolved_profile_id": lookup.profile.profile_id,
                    },
                )
            )
            return None
        return lookup.profile.model_copy(deep=True)
    code = (
        "MF-R001"
        if lookup.classification is CatalogLookupClassification.MISSING
        else "MF-R010"
    )
    diagnostics.append(
        _diagnostic(
            code,
            f"Model profile {profile_id!r} could not be resolved.",
            fields={"profile_id": profile_id, **dict(lookup.evidence)},
        )
    )
    return None


def _lookup_tool_bindings(
    source: HarnessSource,
    parsed_refs: Mapping[str, ToolReference],
    snapshot: ToolCatalogSnapshot,
    diagnostics: list[CompilerDiagnostic],
) -> list[ResolvedToolBinding]:
    resolved: list[ResolvedToolBinding] = []
    nodes_by_id = {node.node_id: node for node in source.graph.nodes}
    for node_id, reference in sorted(parsed_refs.items()):
        try:
            lookup = snapshot.resolve_exact(reference.tool_id, reference.version)
        except Exception as exc:
            diagnostics.append(
                _diagnostic(
                    "MF-R009",
                    "Tool catalog lookup failed.",
                    node_id=node_id,
                    fields={
                        "error_type": type(exc).__name__,
                        "tool_id": reference.tool_id,
                    },
                )
            )
            continue
        if lookup.classification is CatalogLookupClassification.FOUND:
            if lookup.entry is None:
                diagnostics.append(
                    _diagnostic(
                        "MF-R004",
                        "Tool binding lookup returned no entry payload.",
                        node_id=node_id,
                        fields={
                            "tool_id": reference.tool_id,
                            "tool_version": reference.version,
                        },
                    )
                )
                continue
            entry = lookup.entry
            if (
                entry.tool_id != reference.tool_id
                or entry.tool_version != reference.version
            ):
                diagnostics.append(
                    _diagnostic(
                        "MF-R003",
                        "Tool binding lookup returned a mismatched entry.",
                        node_id=node_id,
                        fields={
                            "tool_id": reference.tool_id,
                            "tool_version": reference.version,
                            "resolved_tool_id": entry.tool_id,
                            "resolved_tool_version": entry.tool_version,
                        },
                    )
                )
                continue
            resolved.append(
                ResolvedToolBinding(
                    node_id=node_id,
                    source=nodes_by_id[node_id],
                    descriptor=entry,
                    binding=ResolvedToolBindingRef(
                        tool_id=entry.tool_id,
                        tool_version=entry.tool_version,
                        descriptor_sha256=entry.descriptor_sha256,
                        implementation_id=entry.implementation_id,
                    ),
                )
            )
            continue
        code = (
            "MF-R002"
            if lookup.classification is CatalogLookupClassification.MISSING
            else _tool_lookup_invalid_code(lookup)
        )
        diagnostics.append(
            _diagnostic(
                code,
                f"Tool binding {reference.tool_id}@{reference.version} could not be resolved.",
                node_id=node_id,
                fields={
                    "tool_id": reference.tool_id,
                    "tool_version": reference.version,
                    **dict(lookup.evidence),
                },
            )
        )
    return resolved


def _tool_lookup_invalid_code(lookup: ToolCatalogLookup) -> str:
    if lookup.error_code == _TOOL_LOOKUP_ERROR_CODE_UNSUPPORTED_SCHEMA:
        return "MF-R007"
    if lookup.error_code == _TOOL_LOOKUP_ERROR_CODE_DRIFT:
        return "MF-R008"
    return "MF-R004"


def _duplicate_model_tool_name_diagnostics(
    resolved_nodes: list[ResolvedToolBinding],
) -> list[CompilerDiagnostic]:
    seen: dict[str, str] = {}
    diagnostics: list[CompilerDiagnostic] = []
    for node in sorted(resolved_nodes, key=lambda item: item.node_id):
        name = node.descriptor.model_tool_name
        previous = seen.setdefault(name, node.node_id)
        if previous != node.node_id:
            diagnostics.append(
                _diagnostic(
                    "MF-R006",
                    f"Model tool name {name!r} is used by multiple resolved nodes.",
                    node_id=previous,
                    related_ids=(node.node_id,),
                    fields={"model_tool_name": name},
                )
            )
    return diagnostics


def _diagnostic(
    code: str,
    message: str,
    *,
    node_id: str | None = None,
    related_ids: tuple[str, ...] = (),
    fields: Mapping[str, str | int] | None = None,
) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        phase=CompilerPhase.RESOLUTION,
        severity=DiagnosticSeverity.ERROR,
        message=message,
        node_id=node_id,
        related_ids=related_ids,
        fields=tuple(
            DiagnosticField(key=key, value=value)
            for key, value in sorted((fields or {}).items())
        ),
    )
