"""Artifact validation tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from millforge.compiler import (
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticSeverity,
    HarnessSource,
    ToolCatalogEntry,
    validate_artifacts,
    validate_harness_graph,
)
from tests.compiler.conftest import make_raw_tool_descriptor


def _source(
    nodes: Mapping[str, Mapping[str, Any]],
    *,
    declared: tuple[str, ...] = ("report",),
    required_by_terminal: Mapping[str, tuple[str, ...]] | None = None,
) -> HarnessSource:
    return HarnessSource.model_validate(
        {
            "schema_version": "1.0",
            "kind": "millforge_harness",
            "harness_id": "millforge.test.artifacts.v1",
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
            "graph": {"nodes": nodes},
            "artifacts": {
                "declared_artifact_ids": declared,
                "required_by_terminal": required_by_terminal
                or {"BUILDER_COMPLETE": ("report",)},
            },
        }
    )


def _entry(tool_id: str, produced: tuple[str, ...]) -> ToolCatalogEntry:
    return ToolCatalogEntry.admit(
        make_raw_tool_descriptor(
            tool_id=tool_id,
            implementation_id=f"impl.{tool_id}.v1",
            model_tool_name=tool_id.replace(".", "_"),
            produced_artifact_ids=produced,
        ),
        expected_tool_id=tool_id,
        expected_tool_version=1,
    )


def _entries(
    source: HarnessSource, produced: Mapping[str, tuple[str, ...]]
) -> dict[str, ToolCatalogEntry]:
    return {
        node.node_id: _entry(
            node.tool_ref.removesuffix("@1"), produced.get(node.node_id, ())
        )
        for node in source.graph.nodes
    }


def test_artifact_diagnostic_registry_adds_mf_a001_through_mf_a007() -> None:
    diagnostic = CompilerDiagnostic(
        code="MF-A001",
        phase=CompilerPhase.ARTIFACT,
        severity=DiagnosticSeverity.ERROR,
        message="Artifact is invalid.",
    )

    assert diagnostic.code == "MF-A001"


def test_terminal_required_artifact_accepts_gated_producer_and_records_evidence() -> (
    None
):
    source = _source(
        {
            "produce": {
                "tool_ref": "tools.produce@1",
                "produces": ["report"],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "produce"}],
            },
        }
    )
    entries = _entries(source, {"produce": ("report",)})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert result.ok
    assert result.producer_evidence[0].all_producer_node_ids == ("produce",)
    assert result.producer_evidence[0].terminal_gated_producer_node_ids == ("produce",)


def test_artifact_validation_reports_declared_produced_and_terminal_errors() -> None:
    source = _source(
        {
            "producer": {
                "tool_ref": "tools.producer@1",
                "produces": ["undeclared", "report"],
            },
            "optional": {
                "tool_ref": "tools.optional@1",
                "produces": ["other"],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "BUILDER_COMPLETE",
            },
        },
        declared=("report", "orphan"),
        required_by_terminal={
            "BUILDER_COMPLETE": ("report", "missing_declared"),
            "UNKNOWN": ("report",),
        },
    )
    entries = _entries(source, {"producer": ("report",), "optional": ("other",)})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)
    codes = [diagnostic.code for diagnostic in result.diagnostics]

    assert codes == [
        "MF-A001",
        "MF-A001",
        "MF-A002",
        "MF-A003",
        "MF-A005",
        "MF-A007",
    ]


def test_unknown_terminal_artifact_policy_is_mf_a003_without_cascades() -> None:
    source = _source(
        {
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "BUILDER_COMPLETE",
            },
        },
        declared=(),
        required_by_terminal={"UNKNOWN": ("missing",)},
    )
    entries = _entries(source, {})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-A003"]


def test_terminal_required_artifact_without_any_producer_is_mf_a004() -> None:
    source = _source(
        {
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "BUILDER_COMPLETE",
            },
        },
    )
    entries = _entries(source, {})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-A004"]


def test_terminal_required_artifact_needs_source_declared_producer() -> None:
    source = _source(
        {
            "producer": {
                "tool_ref": "tools.producer@1",
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "producer"}],
            },
        },
    )
    entries = _entries(source, {"producer": ("report",)})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-A004"]
    assert result.producer_evidence[0].all_producer_node_ids == ()
    assert result.producer_evidence[0].terminal_gated_producer_node_ids == ()


def test_terminal_required_artifact_without_gated_producer_is_mf_a005() -> None:
    source = _source(
        {
            "producer": {
                "tool_ref": "tools.producer@1",
                "produces": ["report"],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "BUILDER_COMPLETE",
            },
        },
    )
    entries = _entries(source, {"producer": ("report",)})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-A005"]


def test_branch_only_producer_is_not_valid_for_other_terminal_requirement() -> None:
    source = _source(
        {
            "producer": {
                "tool_ref": "tools.producer@1",
                "produces": ["report"],
            },
            "blocked": {
                "tool_ref": "tools.blocked@1",
                "terminal_result": "BLOCKED",
                "prerequisites": [{"node_id": "producer"}],
            },
            "complete": {
                "tool_ref": "tools.complete@1",
                "terminal_result": "BUILDER_COMPLETE",
            },
        },
        required_by_terminal={"BUILDER_COMPLETE": ("report",)},
    )
    entries = _entries(source, {"producer": ("report",)})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BLOCKED", "BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert graph.diagnostics == ()
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-A005"]
    assert result.producer_evidence[0].all_producer_node_ids == ("producer",)
    assert result.producer_evidence[0].terminal_gated_producer_node_ids == ()


def test_workspace_read_diff_is_not_a_terminal_artifact_producer() -> None:
    source = _source(
        {
            "read_diff": {
                "tool_ref": "builtin.workspace.read_diff@1",
                "produces": ["workspace_diff"],
            },
            "done": {
                "tool_ref": "builtin.terminal.submit@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "read_diff"}],
            },
        },
        declared=("workspace_diff",),
        required_by_terminal={"BUILDER_COMPLETE": ("workspace_diff",)},
    )
    entries = _entries(source, {"read_diff": ()})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "MF-A002",
        "MF-A004",
    ]
    assert result.producer_evidence[0].all_producer_node_ids == ()
    assert result.producer_evidence[0].terminal_gated_producer_node_ids == ()


def test_workspace_diff_artifact_writer_satisfies_terminal_artifact_requirement() -> (
    None
):
    source = _source(
        {
            "write_diff": {
                "tool_ref": "builtin.artifact.write_workspace_diff@1",
                "produces": ["workspace_diff"],
            },
            "done": {
                "tool_ref": "builtin.terminal.submit@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "write_diff"}],
            },
        },
        declared=("workspace_diff",),
        required_by_terminal={"BUILDER_COMPLETE": ("workspace_diff",)},
    )
    entries = _entries(source, {"write_diff": ("workspace_diff",)})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert result.ok
    assert result.producer_evidence[0].artifact_id == "workspace_diff"
    assert result.producer_evidence[0].all_producer_node_ids == ("write_diff",)
    assert result.producer_evidence[0].terminal_gated_producer_node_ids == (
        "write_diff",
    )


def test_global_required_producer_satisfies_terminal_artifact_requirement() -> None:
    source = _source(
        {
            "producer": {
                "tool_ref": "tools.producer@1",
                "required": True,
                "produces": ["report"],
            },
            "complete": {
                "tool_ref": "tools.complete@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "producer"}],
            },
        }
    )
    entries = _entries(source, {"producer": ("report",)})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert graph.diagnostics == ()
    assert result.ok
    assert result.producer_evidence[0].terminal_gated_producer_node_ids == ("producer",)


def test_multiple_valid_producers_record_all_and_accept_terminal_gated_one() -> None:
    source = _source(
        {
            "branch": {
                "tool_ref": "tools.branch@1",
                "produces": ["report"],
            },
            "producer": {
                "tool_ref": "tools.producer@1",
                "produces": ["report"],
            },
            "blocked": {
                "tool_ref": "tools.blocked@1",
                "terminal_result": "BLOCKED",
                "prerequisites": [{"node_id": "branch"}],
            },
            "complete": {
                "tool_ref": "tools.complete@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "producer"}],
            },
        },
        required_by_terminal={"BUILDER_COMPLETE": ("report",)},
    )
    entries = _entries(source, {"branch": ("report",), "producer": ("report",)})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BLOCKED", "BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert graph.diagnostics == ()
    assert result.ok
    assert result.producer_evidence[0].all_producer_node_ids == ("branch", "producer")
    assert result.producer_evidence[0].terminal_gated_producer_node_ids == ("producer",)


def test_graph_invalidity_suppresses_dependent_producer_reachability_diagnostics() -> (
    None
):
    source = _source(
        {
            "producer": {
                "tool_ref": "tools.producer@1",
                "produces": ["report"],
            },
            "complete": {
                "tool_ref": "tools.complete@1",
                "terminal_result": "BUILDER_COMPLETE",
                "prerequisites": [{"node_id": "missing"}],
            },
        }
    )
    entries = _entries(source, {"producer": ("report",)})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert [diagnostic.code for diagnostic in graph.diagnostics] == ["MF-G001"]
    assert result.diagnostics == ()
    assert result.producer_evidence[0].all_producer_node_ids == ("producer",)
    assert result.producer_evidence[0].terminal_gated_producer_node_ids == ()


def test_undeclared_terminal_required_artifact_is_mf_a007_without_cascades() -> None:
    source = _source(
        {
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "BUILDER_COMPLETE",
            },
        },
        declared=(),
        required_by_terminal={"BUILDER_COMPLETE": ("missing",)},
    )
    entries = _entries(source, {})
    graph = validate_harness_graph(
        source, entries, allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    result = validate_artifacts(source, entries, graph)

    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-A007"]
