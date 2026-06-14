"""Semantic compiler orchestration tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest
from pydantic import ValidationError
from millforge import CapabilityEnvelope, CapabilityGrant, CompiledModelProfile
from millforge.compiler import (
    ArtifactPolicySource,
    CompileInvocation,
    CompileStatus,
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticSeverity,
    HarnessCompileRequest,
    HarnessCompileResult,
    HarnessRequestAdmissionResult,
    HarnessGraphSource,
    HarnessSource,
    HarnessNodeSource,
    PlanCommitCertainty,
    TerminalArtifactPolicySource,
    ModelProfileCatalogLookup,
    ToolCatalogEntry,
    ToolCatalogLookup,
    compile_semantic,
    property_schema_compatibility_bytes,
    compile_semantic_from_admission,
    validate_harness_graph,
)
from tests.compiler.conftest import (
    SHA_B,
    SHA_C,
    StaticModelProfileCatalogSnapshot,
    make_raw_tool_descriptor,
)


def _source(nodes: Mapping[str, Mapping[str, Any]] | None = None) -> HarnessSource:
    return HarnessSource.model_validate(
        {
            "schema_version": "1.0",
            "kind": "millforge_harness",
            "harness_id": "millforge.test.semantic.v1",
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
                "nodes": nodes
                or {
                    "inspect": {
                        "tool_ref": "tools.inspect@1",
                        "required": True,
                        "produces": ["report"],
                    },
                    "work": {
                        "tool_ref": "tools.work@1",
                        "prerequisites": [{"node_id": "inspect"}],
                    },
                    "done": {
                        "tool_ref": "tools.done@1",
                        "terminal_result": "BUILDER_COMPLETE",
                        "prerequisites": [{"node_id": "work"}],
                    },
                }
            },
            "artifacts": {
                "declared_artifact_ids": ["report"],
                "required_by_terminal": {"BUILDER_COMPLETE": ["report"]},
            },
        }
    )


def _minimal_source(tool_ref: str) -> HarnessSource:
    return HarnessSource.model_validate(
        {
            "schema_version": "1.0",
            "kind": "millforge_harness",
            "harness_id": "millforge.test.semantic.v1",
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
                    "done": {
                        "tool_ref": tool_ref,
                        "terminal_result": "BUILDER_COMPLETE",
                    }
                }
            },
            "artifacts": {"declared_artifact_ids": [], "required_by_terminal": {}},
        }
    )


def _request() -> HarnessCompileRequest:
    return HarnessCompileRequest(
        request_id="request.semantic.v1",
        source_path="harness.yaml",
        source_root="/tmp",
        source_format="yaml",
        output_dir="out",
        output_root="/tmp",
        expected_harness_id="millforge.test.semantic.v1",
        stage_kind_id="builder",
        legal_terminal_results=("BUILDER_COMPLETE",),
        capability_envelope=CapabilityEnvelope(
            grants=(CapabilityGrant(capability_id="workspace.read"),)
        ),
    )


def _entry(
    tool_id: str,
    *,
    model_tool_name: str | None = None,
    input_schema: Mapping[str, Any] | None = None,
    produced_artifact_ids: tuple[str, ...] = (),
    required_capabilities: tuple[str, ...] = ("workspace.read",),
) -> ToolCatalogEntry:
    return ToolCatalogEntry.admit(
        make_raw_tool_descriptor(
            tool_id=tool_id,
            implementation_id=f"impl.{tool_id}.v1",
            model_tool_name=model_tool_name or tool_id.replace(".", "_"),
            input_schema=input_schema,
            produced_artifact_ids=produced_artifact_ids,
            required_capabilities=required_capabilities,
        ),
        expected_tool_id=tool_id,
        expected_tool_version=1,
    )


class CountingToolSnapshot:
    def __init__(self, entries: Mapping[tuple[str, int], ToolCatalogEntry]) -> None:
        self.metadata_reads: list[str] = []
        self.lookups: list[tuple[str, int]] = []
        self._entries = dict(entries)

    @property
    def snapshot_id(self) -> str:
        self.metadata_reads.append("id")
        return SHA_B

    @property
    def snapshot_sha256(self) -> str:
        self.metadata_reads.append("sha")
        return SHA_C

    def resolve_exact(self, tool_id: str, tool_version: int) -> ToolCatalogLookup:
        self.lookups.append((tool_id, tool_version))
        entry = self._entries.get((tool_id, tool_version))
        if entry is None:
            return ToolCatalogLookup.missing()
        return ToolCatalogLookup.found(entry)


class CountingModelProfileSnapshot:
    def __init__(
        self,
        *,
        snapshot_id: str = SHA_B,
        snapshot_sha256: str = SHA_C,
        explode_on_sha256: bool = False,
    ) -> None:
        self.metadata_reads: list[str] = []
        self.lookups: list[str] = []
        self._snapshot_id = snapshot_id
        self._snapshot_sha256 = snapshot_sha256
        self._explode_on_sha256 = explode_on_sha256

    @property
    def snapshot_id(self) -> str:
        self.metadata_reads.append("id")
        return self._snapshot_id

    @property
    def snapshot_sha256(self) -> str:
        self.metadata_reads.append("sha")
        if self._explode_on_sha256:
            raise RuntimeError("backend unavailable")
        return self._snapshot_sha256

    def resolve_exact(self, profile_id: str) -> ModelProfileCatalogLookup:
        self.lookups.append(profile_id)
        return ModelProfileCatalogLookup.missing()


class UnsupportedSchemaToolSnapshot:
    def __init__(self) -> None:
        self.metadata_reads: list[str] = []
        self.lookups: list[tuple[str, int]] = []

    @property
    def snapshot_id(self) -> str:
        self.metadata_reads.append("id")
        return SHA_B

    @property
    def snapshot_sha256(self) -> str:
        self.metadata_reads.append("sha")
        return SHA_C

    def resolve_exact(self, tool_id: str, tool_version: int) -> ToolCatalogLookup:
        self.lookups.append((tool_id, tool_version))
        return ToolCatalogLookup.invalid(error_code="unsupported-tool-schema")


class DriftingToolSnapshot:
    def __init__(self, entry: ToolCatalogEntry) -> None:
        self.metadata_reads: list[str] = []
        self.lookups: list[tuple[str, int]] = []
        self._entry = entry
        self._call_count = 0

    @property
    def snapshot_id(self) -> str:
        self.metadata_reads.append("id")
        return SHA_B

    @property
    def snapshot_sha256(self) -> str:
        self.metadata_reads.append("sha")
        return SHA_C

    def resolve_exact(self, tool_id: str, tool_version: int) -> ToolCatalogLookup:
        self.lookups.append((tool_id, tool_version))
        self._call_count += 1
        if self._call_count == 1:
            return ToolCatalogLookup.found(self._entry)
        return ToolCatalogLookup.invalid(error_code="catalog-snapshot-drift")


def _model_snapshot() -> StaticModelProfileCatalogSnapshot:
    return StaticModelProfileCatalogSnapshot(
        profiles={
            "profile.standard": CompiledModelProfile(profile_id="profile.standard")
        }
    )


def _tool_snapshot() -> CountingToolSnapshot:
    return CountingToolSnapshot(
        {
            ("tools.inspect", 1): _entry(
                "tools.inspect", produced_artifact_ids=("report",)
            ),
            ("tools.work", 1): _entry("tools.work"),
            ("tools.done", 1): _entry("tools.done"),
        }
    )


def _invalid_tool_ref_source(tool_ref: str) -> HarnessSource:
    source = _minimal_source("tools.done@1")
    invalid_node = HarnessNodeSource.model_construct(
        node_id="done",
        tool_ref=tool_ref,
        terminal_result="BUILDER_COMPLETE",
    )
    invalid_graph = HarnessGraphSource.model_construct(nodes=(invalid_node,))
    return source.model_copy(update={"graph": invalid_graph})


def test_semantic_diagnostic_registry_accepts_resolution_capability_artifact_codes() -> (
    None
):
    diagnostics = [
        CompilerDiagnostic(
            code="MF-R011",
            phase=CompilerPhase.RESOLUTION,
            severity=DiagnosticSeverity.ERROR,
            message="Invalid reference.",
        ),
        CompilerDiagnostic(
            code="MF-C001",
            phase=CompilerPhase.CAPABILITY,
            severity=DiagnosticSeverity.ERROR,
            message="Missing capability.",
        ),
        CompilerDiagnostic(
            code="MF-A007",
            phase=CompilerPhase.ARTIFACT,
            severity=DiagnosticSeverity.ERROR,
            message="Missing gated producer.",
        ),
    ]

    assert [diagnostic.code for diagnostic in diagnostics] == [
        "MF-R011",
        "MF-C001",
        "MF-A007",
    ]


def test_semantic_compile_resolves_private_immutable_ir_and_captures_metadata_once() -> (
    None
):
    tool_snapshot = _tool_snapshot()
    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _source(),
        tool_snapshot=tool_snapshot,
        model_profile_snapshot=_model_snapshot(),
    )

    assert result.ok
    assert result.resolved_harness is not None
    assert tool_snapshot.metadata_reads == ["id", "sha"]
    assert tool_snapshot.lookups == [
        ("tools.done", 1),
        ("tools.inspect", 1),
        ("tools.work", 1),
    ]
    assert result.resolved_harness.required_capability_ids == ("workspace.read",)
    assert result.resolved_harness.terminal_result_map == {"done": "BUILDER_COMPLETE"}
    assert result.resolved_harness.artifact_evidence[0].all_producer_node_ids == (
        "inspect",
    )


@pytest.mark.parametrize(
    "payload_update",
    [
        "declared_artifact_ids",
        "produces",
        "required_artifact_ids",
    ],
)
def test_public_source_duplicate_artifacts_are_rejected_by_03a_validation(
    payload_update: str,
) -> None:
    payload = _source().model_dump(mode="json")
    if payload_update == "declared_artifact_ids":
        payload["artifacts"]["declared_artifact_ids"] = ["report", "report"]
    elif payload_update == "produces":
        payload["graph"]["nodes"][0]["produces"] = ["report", "report"]
    else:
        payload["artifacts"]["required_by_terminal"][0]["artifact_ids"] = [
            "report",
            "report",
        ]

    with pytest.raises(ValidationError, match="values must be unique"):
        HarnessSource.model_validate(payload)


def test_semantic_boundary_reports_mf_a006_for_duplicate_declared_artifact_ids() -> (
    None
):
    source = _source()
    artifacts = ArtifactPolicySource.model_construct(
        declared_artifact_ids=("report", "report"),
        required_by_terminal=source.artifacts.required_by_terminal,
    )

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        source.model_copy(update={"artifacts": artifacts}),
        tool_snapshot=_tool_snapshot(),
        model_profile_snapshot=_model_snapshot(),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-A006"]


def test_semantic_boundary_reports_mf_a006_for_duplicate_node_produced_artifacts() -> (
    None
):
    source = _source()
    nodes = tuple(
        node.model_copy(update={"produces": ("report", "report")})
        if node.node_id == "inspect"
        else node
        for node in source.graph.nodes
    )
    graph = HarnessGraphSource.model_construct(nodes=nodes)

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        source.model_copy(update={"graph": graph}),
        tool_snapshot=_tool_snapshot(),
        model_profile_snapshot=_model_snapshot(),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-A006"]


def test_semantic_boundary_reports_mf_a006_for_duplicate_terminal_required_artifacts() -> (
    None
):
    source = _source()
    artifacts = ArtifactPolicySource.model_construct(
        declared_artifact_ids=source.artifacts.declared_artifact_ids,
        required_by_terminal=(
            TerminalArtifactPolicySource.model_construct(
                terminal_result="BUILDER_COMPLETE",
                artifact_ids=("report", "report"),
            ),
        ),
    )

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        source.model_copy(update={"artifacts": artifacts}),
        tool_snapshot=_tool_snapshot(),
        model_profile_snapshot=_model_snapshot(),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-A006"]


def test_semantic_compile_rejects_mutation_of_terminal_result_map() -> None:
    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _source(),
        tool_snapshot=_tool_snapshot(),
        model_profile_snapshot=_model_snapshot(),
    )

    assert result.ok
    assert result.resolved_harness is not None

    with pytest.raises(TypeError):
        cast(Any, result.resolved_harness.terminal_result_map)["extra"] = "BLOCKED"


def test_frontend_failure_returns_03a_result_without_touching_catalogs() -> None:
    diagnostic = CompilerDiagnostic(
        code="MF-S001",
        phase=CompilerPhase.REQUEST,
        severity=DiagnosticSeverity.ERROR,
        message="Source path must stay within source_root.",
    )
    frontend_result = HarnessCompileResult(
        request_id="request.semantic.v1",
        status=CompileStatus.FAILED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        failure_phase=CompilerPhase.REQUEST,
        diagnostics=(diagnostic,),
    )
    tool_snapshot = _tool_snapshot()

    result = compile_semantic_from_admission(
        HarnessRequestAdmissionResult(result=frontend_result),
        None,
        tool_snapshot=tool_snapshot,
        model_profile_snapshot=_model_snapshot(),
    )

    assert result.frontend_result == frontend_result
    assert result.diagnostics == (diagnostic,)
    assert tool_snapshot.metadata_reads == []
    assert tool_snapshot.lookups == []


def test_invalid_direct_semantic_tool_reference_emits_mf_r011_without_catalog_access() -> (
    None
):
    tool_snapshot = CountingToolSnapshot({})
    model_snapshot = CountingModelProfileSnapshot()

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _invalid_tool_ref_source("bad-ref"),
        tool_snapshot=tool_snapshot,
        model_profile_snapshot=model_snapshot,
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R011"]
    assert result.resolved_harness is None
    assert tool_snapshot.metadata_reads == []
    assert tool_snapshot.lookups == []
    assert model_snapshot.metadata_reads == []
    assert model_snapshot.lookups == []


def test_missing_model_profile_emits_mf_r001() -> None:
    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _minimal_source("tools.done@1"),
        tool_snapshot=CountingToolSnapshot({("tools.done", 1): _entry("tools.done")}),
        model_profile_snapshot=StaticModelProfileCatalogSnapshot(profiles={}),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R001"]
    assert result.resolved_harness is None


def test_invalid_model_profile_lookup_emits_mf_r010() -> None:
    class InvalidModelSnapshot:
        @property
        def snapshot_id(self) -> str:
            return SHA_B

        @property
        def snapshot_sha256(self) -> str:
            return SHA_C

        def resolve_exact(self, profile_id: str) -> ModelProfileCatalogLookup:
            return ModelProfileCatalogLookup.invalid(
                error_code="profile.invalid",
                evidence={"profile_id": profile_id},
            )

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _minimal_source("tools.done@1"),
        tool_snapshot=CountingToolSnapshot({("tools.done", 1): _entry("tools.done")}),
        model_profile_snapshot=InvalidModelSnapshot(),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R010"]
    assert result.resolved_harness is None


@pytest.mark.parametrize(
    ("snapshot_id", "snapshot_sha256"),
    [
        ("g" * 64, SHA_C),
        (SHA_B, "g" * 64),
    ],
)
def test_malformed_model_profile_snapshot_metadata_emits_mf_r009_without_lookup(
    snapshot_id: str, snapshot_sha256: str
) -> None:
    tool_snapshot = CountingToolSnapshot({})
    model_snapshot = CountingModelProfileSnapshot(
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
    )

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _minimal_source("tools.done@1"),
        tool_snapshot=tool_snapshot,
        model_profile_snapshot=model_snapshot,
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R009"]
    assert result.resolved_harness is None
    assert tool_snapshot.metadata_reads == ["id", "sha"]
    assert tool_snapshot.lookups == []
    assert model_snapshot.metadata_reads == ["id", "sha"]
    assert model_snapshot.lookups == []


def test_model_profile_snapshot_metadata_exception_emits_mf_r009_without_lookup() -> (
    None
):
    snapshot = CountingModelProfileSnapshot(explode_on_sha256=True)
    tool_snapshot = CountingToolSnapshot({})
    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _minimal_source("tools.done@1"),
        tool_snapshot=tool_snapshot,
        model_profile_snapshot=snapshot,
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R009"]
    assert result.resolved_harness is None
    assert tool_snapshot.metadata_reads == ["id", "sha"]
    assert tool_snapshot.lookups == []
    assert snapshot.metadata_reads == ["id", "sha"]
    assert snapshot.lookups == []


def test_missing_tool_lookup_emits_mf_r002() -> None:
    snapshot = CountingToolSnapshot({})

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _minimal_source("tools.done@1"),
        tool_snapshot=snapshot,
        model_profile_snapshot=_model_snapshot(),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R002"]
    assert result.resolved_harness is None
    assert snapshot.lookups == [("tools.done", 1)]


def test_tool_lookup_exception_emits_mf_r009() -> None:
    class ExplodingToolSnapshot(CountingToolSnapshot):
        def resolve_exact(self, tool_id: str, tool_version: int) -> ToolCatalogLookup:
            self.lookups.append((tool_id, tool_version))
            raise RuntimeError("backend unavailable")

    snapshot = ExplodingToolSnapshot({})
    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _minimal_source("tools.done@1"),
        tool_snapshot=snapshot,
        model_profile_snapshot=_model_snapshot(),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R009"]
    assert result.resolved_harness is None
    assert snapshot.lookups == [("tools.done", 1)]


def test_unsupported_tool_schema_lookup_emits_mf_r007() -> None:
    snapshot = UnsupportedSchemaToolSnapshot()

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _minimal_source("tools.done@1"),
        tool_snapshot=snapshot,
        model_profile_snapshot=_model_snapshot(),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R007"]
    assert result.resolved_harness is None
    assert snapshot.lookups == [("tools.done", 1)]


def test_catalog_snapshot_drift_emits_mf_r008_on_repeated_invocation() -> None:
    snapshot = DriftingToolSnapshot(_entry("tools.done"))
    invocation = CompileInvocation.from_request(_request())
    source = _minimal_source("tools.done@1")

    first = compile_semantic(
        invocation,
        source,
        tool_snapshot=snapshot,
        model_profile_snapshot=_model_snapshot(),
    )
    second = compile_semantic(
        invocation,
        source,
        tool_snapshot=snapshot,
        model_profile_snapshot=_model_snapshot(),
    )

    assert first.ok
    assert first.resolved_harness is not None
    assert [diagnostic.code for diagnostic in second.diagnostics] == ["MF-R008"]
    assert second.resolved_harness is None
    assert snapshot.lookups == [("tools.done", 1), ("tools.done", 1)]


def test_duplicate_exact_tool_bindings_are_rejected_before_lookup() -> None:
    source = _source(
        {
            "first": {"tool_ref": "tools.same@1"},
            "second": {
                "tool_ref": "tools.same@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "first"}],
            },
        }
    )
    tool_snapshot = CountingToolSnapshot(
        {("tools.same", 1): _entry("tools.same", produced_artifact_ids=("report",))}
    )

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        source,
        tool_snapshot=tool_snapshot,
        model_profile_snapshot=_model_snapshot(),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R005"]
    assert tool_snapshot.metadata_reads == []
    assert tool_snapshot.lookups == []


def test_duplicate_model_tool_names_are_rejected_after_resolution() -> None:
    source = _source()
    tool_snapshot = CountingToolSnapshot(
        {
            ("tools.inspect", 1): _entry(
                "tools.inspect",
                model_tool_name="duplicate",
                produced_artifact_ids=("report",),
            ),
            ("tools.work", 1): _entry("tools.work", model_tool_name="duplicate"),
            ("tools.done", 1): _entry("tools.done"),
        }
    )

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        source,
        tool_snapshot=tool_snapshot,
        model_profile_snapshot=_model_snapshot(),
    )

    assert "MF-R006" in [diagnostic.code for diagnostic in result.diagnostics]


def test_mismatched_found_model_profile_is_rejected() -> None:
    class MismatchedModelSnapshot:
        def __init__(self) -> None:
            self.metadata_reads: list[str] = []

        @property
        def snapshot_id(self) -> str:
            self.metadata_reads.append("id")
            return SHA_B

        @property
        def snapshot_sha256(self) -> str:
            self.metadata_reads.append("sha")
            return SHA_C

        def resolve_exact(self, profile_id: str) -> ModelProfileCatalogLookup:
            assert profile_id == "profile.standard"
            return ModelProfileCatalogLookup.found(
                CompiledModelProfile(profile_id="profile.other")
            )

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _minimal_source("tools.done@1"),
        tool_snapshot=_tool_snapshot(),
        model_profile_snapshot=MismatchedModelSnapshot(),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R010"]
    assert result.resolved_harness is None


def test_mismatched_found_tool_entry_is_rejected() -> None:
    class MismatchedToolSnapshot(CountingToolSnapshot):
        def resolve_exact(self, tool_id: str, tool_version: int) -> ToolCatalogLookup:
            self.lookups.append((tool_id, tool_version))
            entry = self._entries.get((tool_id, tool_version))
            if entry is None:
                return ToolCatalogLookup.missing()
            return ToolCatalogLookup.found(
                entry.model_copy(update={"tool_id": "tools.other"})
            )

    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        _minimal_source("tools.done@1"),
        tool_snapshot=MismatchedToolSnapshot({("tools.done", 1): _entry("tools.done")}),
        model_profile_snapshot=_model_snapshot(),
    )

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R003"]
    assert result.resolved_harness is None


def test_argument_validation_runs_only_for_resolved_node_descriptors() -> None:
    source = _source(
        {
            "prior": {"tool_ref": "tools.prior@1"},
            "current": {
                "tool_ref": "tools.current@1",
                "prerequisites": [
                    {
                        "node_id": "prior",
                        "argument_matches": {"absent": "message"},
                    }
                ],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "current"}],
            },
        }
    )
    all_entries = {
        "prior": _entry("tools.prior"),
        "current": _entry("tools.current"),
        "done": _entry("tools.done"),
    }
    unresolved_prior = dict(all_entries)
    del unresolved_prior["prior"]

    with_all_descriptors = validate_harness_graph(source, all_entries)
    with_unresolved_prior = validate_harness_graph(source, unresolved_prior)

    assert [diagnostic.code for diagnostic in with_all_descriptors.diagnostics] == [
        "MF-G012"
    ]
    assert with_unresolved_prior.diagnostics == ()


def test_argument_validation_uses_normalized_property_schema_bytes() -> None:
    source = _source(
        {
            "prior": {"tool_ref": "tools.prior@1"},
            "current": {
                "tool_ref": "tools.current@1",
                "prerequisites": [
                    {
                        "node_id": "prior",
                        "argument_matches": {"payload": "payload"},
                    }
                ],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "current"}],
            },
        }
    )
    prior_property = {
        "type": "string",
        "description": "ignored",
        "enum": ["final", "draft"],
        "default": "draft",
    }
    current_property = {
        "type": "string",
        "description": "also ignored",
        "enum": ["final", "draft"],
        "default": "final",
    }
    assert property_schema_compatibility_bytes(prior_property) == (
        property_schema_compatibility_bytes(current_property)
    )

    descriptors = {
        "prior": _entry(
            "tools.prior",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"payload": prior_property},
                "required": ["payload"],
            },
        ),
        "current": _entry(
            "tools.current",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"payload": current_property},
                "required": ["payload"],
            },
        ),
        "done": _entry("tools.done"),
    }

    assert validate_harness_graph(source, descriptors).diagnostics == ()


def test_argument_validation_rejects_declared_enum_order_mismatch() -> None:
    source = _source(
        {
            "prior": {"tool_ref": "tools.prior@1"},
            "current": {
                "tool_ref": "tools.current@1",
                "prerequisites": [
                    {
                        "node_id": "prior",
                        "argument_matches": {"payload": "payload"},
                    }
                ],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "current"}],
            },
        }
    )
    descriptors = {
        "prior": _entry(
            "tools.prior",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "payload": {"type": "string", "enum": ["final", "draft"]}
                },
                "required": ["payload"],
            },
        ),
        "current": _entry(
            "tools.current",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "payload": {"type": "string", "enum": ["draft", "final"]}
                },
                "required": ["payload"],
            },
        ),
        "done": _entry("tools.done"),
    }

    result = validate_harness_graph(source, descriptors)

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-G012"]
