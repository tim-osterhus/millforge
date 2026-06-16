"""Built-in catalog compiler fixture tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from millforge import (
    CapabilityEnvelope,
    CapabilityGrant,
    CompiledModelProfile,
    canonical_compiled_plan_bytes,
    verify_compiled_plan_sha256,
)
from millforge.compiler import (
    CompileInvocation,
    CompilerPhase,
    HarnessCompileRequest,
    HarnessSource,
    HarnessSourceParser,
    SourceDocument,
    compile_semantic,
    lower_resolved_harness,
)
from millforge.tools import create_builtin_tool_snapshot
from tests.compiler.conftest import StaticModelProfileCatalogSnapshot

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class BuiltinHarnessFixture:
    name: str
    filename: str
    harness_id: str
    stage_kind_id: str
    legal_terminal_results: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    declared_artifact_ids: tuple[str, ...]
    required_by_terminal: tuple[tuple[str, tuple[str, ...]], ...]
    produced_artifact_nodes: dict[str, tuple[str, ...]]


BUILTIN_HARNESS_FIXTURES = (
    BuiltinHarnessFixture(
        name="planner",
        filename="builtin_planner_harness.yaml",
        harness_id="millforge.test.builtin.planner.v1",
        stage_kind_id="planner",
        legal_terminal_results=("PLANNER_COMPLETE",),
        required_capabilities=("artifact.write", "request.read", "terminal.intent"),
        declared_artifact_ids=("plan",),
        required_by_terminal=(("PLANNER_COMPLETE", ("plan",)),),
        produced_artifact_nodes={"write_plan": ("plan",)},
    ),
    BuiltinHarnessFixture(
        name="builder",
        filename="builtin_builder_harness.yaml",
        harness_id="millforge.test.builtin.builder.v1",
        stage_kind_id="builder",
        legal_terminal_results=("BUILDER_COMPLETE",),
        required_capabilities=(
            "artifact.read",
            "artifact.write",
            "process.static_check",
            "process.test",
            "request.read",
            "terminal.intent",
            "workspace.read",
            "workspace.write",
        ),
        declared_artifact_ids=("patch_summary", "test_results"),
        required_by_terminal=(("BUILDER_COMPLETE", ("patch_summary", "test_results")),),
        produced_artifact_nodes={
            "write_patch_summary": ("patch_summary",),
            "write_test_results": ("test_results",),
        },
    ),
    BuiltinHarnessFixture(
        name="checker",
        filename="builtin_checker_harness.yaml",
        harness_id="millforge.test.builtin.checker.v1",
        stage_kind_id="checker",
        legal_terminal_results=("CHECKER_COMPLETE",),
        required_capabilities=("artifact.read", "artifact.write", "terminal.intent"),
        declared_artifact_ids=("arbiter_verdict", "checker_verdict"),
        required_by_terminal=(("CHECKER_COMPLETE", ("checker_verdict",)),),
        produced_artifact_nodes={
            "write_checker_verdict": ("arbiter_verdict", "checker_verdict")
        },
    ),
    BuiltinHarnessFixture(
        name="arbiter",
        filename="builtin_arbiter_harness.yaml",
        harness_id="millforge.test.builtin.arbiter.v1",
        stage_kind_id="arbiter",
        legal_terminal_results=("ARBITER_COMPLETE",),
        required_capabilities=(
            "artifact.read",
            "artifact.write",
            "request.read",
            "terminal.intent",
        ),
        declared_artifact_ids=("arbiter_verdict", "checker_verdict"),
        required_by_terminal=(("ARBITER_COMPLETE", ("arbiter_verdict",)),),
        produced_artifact_nodes={
            "write_arbiter_verdict": ("arbiter_verdict", "checker_verdict")
        },
    ),
)


@pytest.mark.parametrize("case", BUILTIN_HARNESS_FIXTURES, ids=lambda case: case.name)
def test_builtin_harness_fixtures_compile_with_production_snapshot(
    case: BuiltinHarnessFixture,
) -> None:
    result = _compile_fixture(case)

    assert result.diagnostics == ()
    assert result.resolved_harness is not None
    assert result.capability_result is not None
    assert (
        result.capability_result.required_capability_ids == case.required_capabilities
    )
    assert result.artifact_result is not None
    assert result.artifact_result.ok

    plan = lower_resolved_harness(result.resolved_harness)
    plan_bytes = canonical_compiled_plan_bytes(plan)
    verified, computed, warnings, restored = verify_compiled_plan_sha256(
        plan_bytes.decode("utf-8"),
        expected_compiled_hash=plan.compiled_sha256,
        expected_harness_id=case.harness_id,
        expected_harness_version=1,
    )

    assert verified is True
    assert computed == plan.compiled_sha256
    assert warnings == []
    assert restored == plan
    assert plan.harness_id == case.harness_id
    assert plan.stage_kind_ids == (case.stage_kind_id,)
    assert plan.required_capabilities == case.required_capabilities
    assert plan.artifact_policy.declared_artifact_ids == case.declared_artifact_ids
    assert (
        tuple(
            (policy.terminal_result, policy.artifact_ids)
            for policy in plan.artifact_policy.required_by_terminal
        )
        == case.required_by_terminal
    )
    assert {
        node.node_id: node.produced_artifact_ids
        for node in plan.nodes
        if node.produced_artifact_ids
    } == case.produced_artifact_nodes


def test_builtin_builder_fixture_missing_capability_uses_existing_diagnostic() -> None:
    case = BUILTIN_HARNESS_FIXTURES[1]
    grants = tuple(
        capability
        for capability in case.required_capabilities
        if capability != "process.test"
    )

    result = _compile_fixture(case, capability_ids=grants)

    assert result.resolved_harness is None
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-C001"]
    assert result.diagnostics[0].phase is CompilerPhase.CAPABILITY
    assert result.diagnostics[0].fields[0].value == "process.test"


def test_builtin_builder_fixture_rejects_artifact_mismatch_semantically() -> None:
    case = BUILTIN_HARNESS_FIXTURES[1]
    source = _fixture_source(case).model_copy(deep=True)
    mutated_nodes = tuple(
        node.model_copy(update={"produces": ("plan",)})
        if node.node_id == "write_patch_summary"
        else node
        for node in source.graph.nodes
    )
    source = source.model_copy(
        update={"graph": source.graph.model_copy(update={"nodes": mutated_nodes})}
    )

    result = _compile_source(case, source)

    assert result.resolved_harness is None
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "MF-A001",
        "MF-A002",
        "MF-A004",
    ]
    assert {diagnostic.phase for diagnostic in result.diagnostics} == {
        CompilerPhase.ARTIFACT
    }


def _compile_fixture(
    case: BuiltinHarnessFixture,
    *,
    capability_ids: tuple[str, ...] | None = None,
) -> Any:
    return _compile_source(case, _fixture_source(case), capability_ids=capability_ids)


def _compile_source(
    case: BuiltinHarnessFixture,
    source: HarnessSource,
    *,
    capability_ids: tuple[str, ...] | None = None,
) -> Any:
    request = HarnessCompileRequest(
        request_id=f"request.builtin.{case.name}.v1",
        source_path=case.filename,
        source_root=str(FIXTURES),
        source_format="yaml",
        output_dir="compiled",
        output_root="/tmp/millforge-builtin-fixtures",
        expected_harness_id=case.harness_id,
        stage_kind_id=case.stage_kind_id,
        legal_terminal_results=case.legal_terminal_results,
        capability_envelope=CapabilityEnvelope(
            grants=tuple(
                CapabilityGrant(capability_id=capability_id)
                for capability_id in (capability_ids or case.required_capabilities)
            )
        ),
    )
    return compile_semantic(
        CompileInvocation.from_request(request),
        source,
        tool_snapshot=create_builtin_tool_snapshot(),
        model_profile_snapshot=StaticModelProfileCatalogSnapshot(
            profiles={
                "profile.standard": CompiledModelProfile(profile_id="profile.standard")
            }
        ),
    )


def _fixture_source(
    case: BuiltinHarnessFixture, source_format: Literal["yaml"] = "yaml"
) -> HarnessSource:
    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path=case.filename,
            format=source_format,
            content=(FIXTURES / case.filename).read_bytes(),
        )
    )
    assert parsed.diagnostics == ()
    assert parsed.source is not None
    return parsed.source
