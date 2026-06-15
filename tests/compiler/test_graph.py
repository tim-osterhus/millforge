"""Tests for deterministic graph and argument validation."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from millforge.compiler import (
    ArgumentMatchSource,
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticSeverity,
    HarnessSource,
    ToolCatalogEntry,
    validate_harness_graph,
)
from millforge.compiler.source import MAX_NODES
from tests.compiler.conftest import make_raw_tool_descriptor


def _source(
    nodes: Mapping[str, Mapping[str, Any]],
    *,
    required_artifacts: Mapping[str, tuple[str, ...]] | None = None,
) -> HarnessSource:
    return HarnessSource.model_validate(
        {
            "schema_version": "1.0",
            "kind": "millforge_harness",
            "harness_id": "millforge.test.graph.v1",
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
                "declared_artifact_ids": [],
                "required_by_terminal": required_artifacts or {},
            },
        }
    )


def _entry(
    *,
    tool_id: str,
    input_schema: Mapping[str, Any] | None = None,
) -> ToolCatalogEntry:
    return ToolCatalogEntry.admit(
        make_raw_tool_descriptor(
            tool_id=tool_id,
            model_tool_name=tool_id.replace(".", "_"),
            implementation_id=f"impl.{tool_id}.v1",
            input_schema=input_schema,
        ),
        expected_tool_id=tool_id,
        expected_tool_version=1,
    )


def _entries(source: HarnessSource) -> dict[str, ToolCatalogEntry]:
    return {
        node.node_id: _entry(tool_id=node.tool_ref.removesuffix("@1"))
        for node in source.graph.nodes
    }


def _codes(diagnostics: tuple[CompilerDiagnostic, ...]) -> list[str]:
    return [diagnostic.code for diagnostic in diagnostics]


def _code_node_pairs(
    diagnostics: tuple[CompilerDiagnostic, ...],
) -> list[tuple[str, str | None]]:
    return [(diagnostic.code, diagnostic.node_id) for diagnostic in diagnostics]


def _simple_oracle(
    nodes: Mapping[str, Mapping[str, Any]],
) -> tuple[bool, dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    prerequisites = {
        node_id: tuple(prereq["node_id"] for prereq in payload.get("prerequisites", ()))
        for node_id, payload in nodes.items()
    }
    cycle_nodes: set[str] = set()
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            cycle_nodes.update(visiting)
            return
        if node_id in visited:
            return
        visiting.add(node_id)
        for prerequisite in prerequisites[node_id]:
            if prerequisite in prerequisites:
                visit(prerequisite)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in sorted(nodes):
        visit(node_id)

    ancestor_cache: dict[str, tuple[str, ...]] = {}

    def ancestors(node_id: str) -> tuple[str, ...]:
        if node_id in ancestor_cache:
            return ancestor_cache[node_id]
        found: set[str] = set()
        stack = list(prerequisites[node_id])
        while stack:
            current = stack.pop()
            if current in found or current not in prerequisites:
                continue
            found.add(current)
            stack.extend(prerequisites[current])
        ancestor_cache[node_id] = tuple(sorted(found))
        return ancestor_cache[node_id]

    required = {
        node_id for node_id, payload in nodes.items() if payload.get("required", False)
    }
    terminal_gaps = {
        node_id: tuple(sorted(required - set(ancestors(node_id)) - {node_id}))
        for node_id, payload in nodes.items()
        if payload.get("terminal_result") is not None
    }
    terminal_gaps = {node_id: gaps for node_id, gaps in terminal_gaps.items() if gaps}
    return (
        not cycle_nodes,
        {node_id: ancestors(node_id) for node_id in nodes},
        terminal_gaps,
    )


def test_graph_diagnostic_registry_adds_mf_g001_through_mf_g013() -> None:
    diagnostic = CompilerDiagnostic(
        code="MF-G013",
        phase=CompilerPhase.GRAPH,
        severity=DiagnosticSeverity.ERROR,
        message="Argument match is invalid.",
    )

    assert diagnostic.code == "MF-G013"


def test_valid_graph_accepts_required_gate_optional_disconnected_node() -> None:
    source = _source(
        {
            "inspect": {
                "tool_ref": "tools.inspect@1",
                "required": True,
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
            "optional": {"tool_ref": "tools.optional@1"},
        }
    )

    result = validate_harness_graph(
        source,
        _entries(source),
        allowed_terminal_results={"BUILDER_COMPLETE"},
    )

    assert result.ok
    assert result.terminal_result_map == {"done": "BUILDER_COMPLETE"}
    assert result.required_node_ids == ("inspect",)


def test_valid_graph_accepts_disconnected_optional_prerequisite_chain() -> None:
    source = _source(
        {
            "inspect": {
                "tool_ref": "tools.inspect@1",
                "required": True,
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
            "optional_root": {"tool_ref": "tools.optional_root@1"},
            "optional_child": {
                "tool_ref": "tools.optional_child@1",
                "prerequisites": [{"node_id": "optional_root"}],
            },
        }
    )

    result = validate_harness_graph(
        source,
        _entries(source),
        allowed_terminal_results={"BUILDER_COMPLETE"},
    )

    assert result.ok
    assert result.terminal_result_map == {"done": "BUILDER_COMPLETE"}
    assert result.required_node_ids == ("inspect",)


def test_graph_oracle_metamorphic_required_terminal_gating_and_bounds() -> None:
    nodes: dict[str, dict[str, Any]] = {
        "setup": {"tool_ref": "tools.setup@1", "required": True},
        "collect": {
            "tool_ref": "tools.collect@1",
            "prerequisites": [{"node_id": "setup"}],
        },
        "complete": {
            "tool_ref": "tools.complete@1",
            "terminal_result": "BUILDER_COMPLETE",
            "prerequisites": [{"node_id": "collect"}],
        },
        "blocked": {
            "tool_ref": "tools.blocked@1",
            "terminal_result": "BLOCKED",
            "prerequisites": [{"node_id": "collect"}],
        },
        "unused_root": {"tool_ref": "tools.unused_root@1"},
        "unused_child": {
            "tool_ref": "tools.unused_child@1",
            "prerequisites": [{"node_id": "unused_root"}],
        },
    }
    source = _source(nodes)
    oracle_ok, oracle_ancestors, oracle_gaps = _simple_oracle(nodes)

    result = validate_harness_graph(
        source,
        _entries(source),
        allowed_terminal_results={"BLOCKED", "BUILDER_COMPLETE"},
    )

    assert oracle_ok
    assert oracle_gaps == {}
    assert {"setup", "collect"} <= set(oracle_ancestors["complete"])
    assert {"setup", "collect"} <= set(oracle_ancestors["blocked"])
    assert result.ok

    permuted = _source(dict(reversed(tuple(nodes.items()))))
    renamed_nodes = {
        node_id.replace("collect", "gather"): {
            **payload,
            "tool_ref": payload["tool_ref"].replace("collect", "gather"),
            "prerequisites": [
                {
                    **prereq,
                    "node_id": prereq["node_id"].replace("collect", "gather"),
                }
                for prereq in payload.get("prerequisites", ())
            ],
        }
        for node_id, payload in nodes.items()
    }
    renamed = _source(renamed_nodes)

    assert (
        validate_harness_graph(
            permuted,
            _entries(permuted),
            allowed_terminal_results={"BLOCKED", "BUILDER_COMPLETE"},
        ).diagnostics
        == result.diagnostics
    )
    assert validate_harness_graph(
        renamed,
        _entries(renamed),
        allowed_terminal_results={"BLOCKED", "BUILDER_COMPLETE"},
    ).ok

    ungated_nodes = {
        **nodes,
        "complete": {
            **nodes["complete"],
            "prerequisites": [],
        },
    }
    ungated_source = _source(ungated_nodes)
    _, _, ungated_gaps = _simple_oracle(ungated_nodes)
    ungated_result = validate_harness_graph(
        ungated_source,
        _entries(ungated_source),
        allowed_terminal_results={"BLOCKED", "BUILDER_COMPLETE"},
    )

    assert ungated_gaps == {"complete": ("setup",)}
    assert _code_node_pairs(ungated_result.diagnostics) == [("MF-G006", "complete")]
    assert ungated_result.diagnostics[0].related_ids == ("setup",)

    bounded_nodes: dict[str, dict[str, Any]] = {
        "root": {"tool_ref": "tools.root@1", "required": True},
        "collect": {
            "tool_ref": "tools.collect@1",
            "prerequisites": [{"node_id": "root"}],
        },
        "complete": {
            "tool_ref": "tools.complete@1",
            "terminal_result": "BUILDER_COMPLETE",
            "prerequisites": [{"node_id": "collect"}],
        },
    }
    for index in range(1, MAX_NODES - 2):
        node_id = f"node_{index:03d}"
        bounded_nodes[node_id] = {
            "tool_ref": f"tools.{node_id}@1",
            "prerequisites": [{"node_id": "root"}],
        }
    assert len(bounded_nodes) == MAX_NODES
    bounded_source = _source(bounded_nodes)
    started = time.perf_counter()
    bounded_result = validate_harness_graph(bounded_source, _entries(bounded_source))
    elapsed = time.perf_counter() - started

    assert bounded_result.ok
    assert elapsed < 1.0


def test_graph_validation_emits_stable_topology_and_terminal_diagnostics() -> None:
    first = _source(
        {
            "zcycle": {
                "tool_ref": "tools.zcycle@1",
                "prerequisites": [{"node_id": "acycle"}],
            },
            "acycle": {
                "tool_ref": "tools.acycle@1",
                "prerequisites": [{"node_id": "zcycle"}],
            },
            "terminal_a": {
                "tool_ref": "tools.terminal_a@1",
                "terminal_result": "DONE",
            },
            "needs_terminal": {
                "tool_ref": "tools.needs_terminal@1",
                "prerequisites": [{"node_id": "terminal_a"}],
            },
            "bad": {
                "tool_ref": "tools.bad@1",
                "required": True,
                "prerequisites": [
                    {"node_id": "missing"},
                    {"node_id": "missing"},
                    {"node_id": "bad"},
                ],
            },
            "terminal_b": {
                "tool_ref": "tools.terminal_b@1",
                "terminal_result": "DONE",
                "required": True,
                "prerequisites": [{"node_id": "missing"}],
            },
            "terminal_c": {
                "tool_ref": "tools.terminal_c@1",
                "terminal_result": "OTHER",
                "prerequisites": [{"node_id": "missing"}],
            },
        }
    )
    second = _source(
        {
            "terminal_b": {
                "tool_ref": "tools.terminal_b@1",
                "terminal_result": "DONE",
                "required": True,
                "prerequisites": [{"node_id": "missing"}],
            },
            "terminal_c": {
                "tool_ref": "tools.terminal_c@1",
                "terminal_result": "OTHER",
                "prerequisites": [{"node_id": "missing"}],
            },
            "bad": {
                "tool_ref": "tools.bad@1",
                "required": True,
                "prerequisites": [
                    {"node_id": "bad"},
                    {"node_id": "missing"},
                    {"node_id": "missing"},
                ],
            },
            "needs_terminal": {
                "tool_ref": "tools.needs_terminal@1",
                "prerequisites": [{"node_id": "terminal_a"}],
            },
            "terminal_a": {
                "tool_ref": "tools.terminal_a@1",
                "terminal_result": "DONE",
            },
            "acycle": {
                "tool_ref": "tools.acycle@1",
                "prerequisites": [{"node_id": "zcycle"}],
            },
            "zcycle": {
                "tool_ref": "tools.zcycle@1",
                "prerequisites": [{"node_id": "acycle"}],
            },
        }
    )

    first_result = validate_harness_graph(
        first, _entries(first), allowed_terminal_results={"BUILDER_COMPLETE"}
    )
    second_result = validate_harness_graph(
        second, _entries(second), allowed_terminal_results={"BUILDER_COMPLETE"}
    )

    assert _codes(first_result.diagnostics) == _codes(second_result.diagnostics)
    assert _codes(first_result.diagnostics).count("MF-G001") == 3
    assert {"MF-G002", "MF-G003", "MF-G004", "MF-G008"} <= set(
        _codes(first_result.diagnostics)
    )
    assert {"MF-G009", "MF-G010", "MF-G011", "MF-G013"} <= set(
        _codes(first_result.diagnostics)
    )


def test_missing_terminal_is_reported() -> None:
    source = _source({"work": {"tool_ref": "tools.work@1"}})

    result = validate_harness_graph(source, _entries(source))

    assert _codes(result.diagnostics) == ["MF-G011"]


def test_canonical_prerequisite_cycle_is_mf_g004_not_swapped() -> None:
    source = _source(
        {
            "acycle": {
                "tool_ref": "tools.acycle@1",
                "prerequisites": [{"node_id": "zcycle"}],
            },
            "zcycle": {
                "tool_ref": "tools.zcycle@1",
                "prerequisites": [{"node_id": "acycle"}],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "DONE",
            },
        }
    )

    result = validate_harness_graph(source, _entries(source))

    assert _codes(result.diagnostics) == ["MF-G004"]
    assert result.diagnostics[0].node_id == "acycle"
    assert result.diagnostics[0].related_ids == ("zcycle",)


def test_canonical_unreachable_optional_node_is_mf_g005_not_swapped() -> None:
    source = _source(
        {
            "blocked": {
                "tool_ref": "tools.blocked@1",
                "prerequisites": [{"node_id": "missing"}],
            },
            "optional": {
                "tool_ref": "tools.optional@1",
                "prerequisites": [{"node_id": "blocked"}],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "DONE",
            },
        }
    )

    result = validate_harness_graph(source, _entries(source))

    assert _code_node_pairs(result.diagnostics) == [
        ("MF-G001", "blocked"),
        ("MF-G005", "optional"),
    ]


def test_canonical_unreachable_terminal_is_mf_g006_not_swapped() -> None:
    source = _source(
        {
            "blocked": {
                "tool_ref": "tools.blocked@1",
                "prerequisites": [{"node_id": "missing"}],
            },
            "terminal": {
                "tool_ref": "tools.terminal@1",
                "terminal_result": "DONE",
                "prerequisites": [{"node_id": "blocked"}],
            },
        }
    )

    result = validate_harness_graph(source, _entries(source))

    assert _code_node_pairs(result.diagnostics) == [
        ("MF-G001", "blocked"),
        ("MF-G006", "terminal"),
    ]


def test_canonical_unreachable_required_node_is_mf_g007_not_swapped() -> None:
    source = _source(
        {
            "blocked": {
                "tool_ref": "tools.blocked@1",
                "prerequisites": [{"node_id": "missing"}],
            },
            "required": {
                "tool_ref": "tools.required@1",
                "required": True,
                "prerequisites": [{"node_id": "blocked"}],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "DONE",
            },
        }
    )

    result = validate_harness_graph(source, _entries(source))

    assert _code_node_pairs(result.diagnostics) == [
        ("MF-G001", "blocked"),
        ("MF-G007", "required"),
    ]


def test_argument_matches_validate_required_top_level_schema_compatibility() -> None:
    source = _source(
        {
            "prior": {"tool_ref": "tools.prior@1"},
            "current": {
                "tool_ref": "tools.current@1",
                "prerequisites": [
                    {
                        "node_id": "prior",
                        "argument_matches": {"message": "message"},
                    }
                ],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "DONE",
                "prerequisites": [{"node_id": "current"}],
            },
        }
    )
    entries = _entries(source)
    incompatible = _entry(
        tool_id="tools.current",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "integer"}},
            "required": ["message"],
            "additionalProperties": False,
        },
    )

    assert validate_harness_graph(source, entries).ok

    entries["current"] = incompatible
    result = validate_harness_graph(source, entries)

    assert _codes(result.diagnostics) == ["MF-G012"]
    fields = {field.key: field.value for field in result.diagnostics[0].fields}
    assert fields["current_argument"] == "message"
    assert fields["prior_argument"] == "message"


def test_argument_matches_reject_missing_and_optional_properties() -> None:
    source = _source(
        {
            "prior": {"tool_ref": "tools.prior@1"},
            "current": {
                "tool_ref": "tools.current@1",
                "prerequisites": [
                    {
                        "node_id": "prior",
                        "argument_matches": {"optional": "missing"},
                    }
                ],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "DONE",
                "prerequisites": [{"node_id": "current"}],
            },
        }
    )
    entries = {
        **_entries(source),
        "prior": _entry(
            tool_id="tools.prior",
            input_schema={
                "type": "object",
                "properties": {"optional": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
        ),
    }

    result = validate_harness_graph(source, entries)

    assert _codes(result.diagnostics) == ["MF-G012"]
    assert "unknown current argument" in result.diagnostics[0].message


def test_unresolved_descriptors_suppress_dependent_graph_and_argument_diagnostics() -> (
    None
):
    source = _source(
        {
            "prior": {"tool_ref": "tools.prior@1"},
            "current": {
                "tool_ref": "tools.current@1",
                "prerequisites": [
                    {
                        "node_id": "prior",
                        "argument_matches": {"missing": "missing"},
                    }
                ],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "DONE",
                "prerequisites": [{"node_id": "current"}],
            },
            "required": {"tool_ref": "tools.required@1", "required": True},
        }
    )
    entries = _entries(source)
    del entries["prior"]

    result = validate_harness_graph(source, entries)

    assert result.diagnostics == ()


def test_argument_match_fan_conflicts_are_rejected_for_constructed_sources() -> None:
    source = _source(
        {
            "prior": {"tool_ref": "tools.prior@1"},
            "current": {
                "tool_ref": "tools.current@1",
                "prerequisites": [{"node_id": "prior"}],
            },
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "DONE",
                "prerequisites": [{"node_id": "current"}],
            },
        }
    )
    nodes = list(source.graph.nodes)
    prereq = (
        nodes[1]
        .prerequisites[0]
        .model_copy(
            update={
                "argument_matches": (
                    ArgumentMatchSource(prior_argument="message", current_argument="a"),
                    ArgumentMatchSource(prior_argument="message", current_argument="b"),
                    ArgumentMatchSource(prior_argument="a", current_argument="b"),
                )
            }
        )
    )
    nodes[1] = nodes[1].model_copy(update={"prerequisites": (prereq,)})
    source = source.model_copy(
        update={"graph": source.graph.model_copy(update={"nodes": tuple(nodes)})}
    )
    entries = {
        node.node_id: _entry(
            tool_id=node.tool_ref.removesuffix("@1"),
            input_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "a": {"type": "string"},
                    "b": {"type": "string"},
                },
                "required": ["message", "a", "b"],
                "additionalProperties": False,
            },
        )
        for node in source.graph.nodes
    }

    result = validate_harness_graph(source, entries)

    assert _codes(result.diagnostics).count("MF-G012") == 2


def test_canonical_terminal_prerequisite_is_mf_g013_not_argument_match() -> None:
    source = _source(
        {
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "DONE",
            },
            "after_done": {
                "tool_ref": "tools.after_done@1",
                "prerequisites": [{"node_id": "done"}],
            },
        }
    )

    result = validate_harness_graph(source, _entries(source))

    assert _codes(result.diagnostics) == ["MF-G013"]


def test_canonical_graph_diagnostic_single_triggers_for_terminal_rows() -> None:
    terminal_required = _source(
        {
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "DONE",
                "required": True,
            }
        }
    )
    illegal_terminal = _source(
        {
            "done": {
                "tool_ref": "tools.done@1",
                "terminal_result": "DONE",
            }
        }
    )
    duplicate_terminal = _source(
        {
            "done_a": {
                "tool_ref": "tools.done_a@1",
                "terminal_result": "DONE",
            },
            "done_b": {
                "tool_ref": "tools.done_b@1",
                "terminal_result": "DONE",
            },
        }
    )

    assert _codes(
        validate_harness_graph(
            terminal_required, _entries(terminal_required)
        ).diagnostics
    ) == ["MF-G008"]
    assert _codes(
        validate_harness_graph(
            illegal_terminal,
            _entries(illegal_terminal),
            allowed_terminal_results=set(),
        ).diagnostics
    ) == ["MF-G009"]
    assert _codes(
        validate_harness_graph(
            duplicate_terminal,
            _entries(duplicate_terminal),
        ).diagnostics
    ) == ["MF-G010"]


def test_canonical_reachability_diagnostics_are_not_swapped() -> None:
    source = _source(
        {
            "blocked": {
                "tool_ref": "tools.blocked@1",
                "prerequisites": [{"node_id": "missing"}],
            },
            "unreachable_node": {
                "tool_ref": "tools.unreachable_node@1",
                "prerequisites": [{"node_id": "blocked"}],
            },
            "unreachable_required": {
                "tool_ref": "tools.unreachable_required@1",
                "required": True,
                "prerequisites": [{"node_id": "blocked"}],
            },
            "unreachable_terminal": {
                "tool_ref": "tools.unreachable_terminal@1",
                "terminal_result": "DONE",
                "prerequisites": [{"node_id": "blocked"}],
            },
        }
    )

    codes = _codes(validate_harness_graph(source, _entries(source)).diagnostics)

    assert "MF-G005" in codes
    assert "MF-G006" in codes
    assert "MF-G007" in codes
