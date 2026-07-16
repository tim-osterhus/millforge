"""Bounded coverage for the ordinary Millforge base harness source."""

from __future__ import annotations

from millforge.compiled_plan import CompiledModelProfile
from millforge.compiler import HarnessSource, compile_harness_source_in_memory
from millforge.contracts import CapabilityEnvelope, CapabilityGrant
from millforge.tools.pi_compat_catalog import (
    PI_COMPAT_CAPABILITY_IDS,
    create_pi_compat_tool_snapshot,
)
from millforge.base.harness import millforge_base_harness_source
from tests.compiler.conftest import StaticModelProfileCatalogSnapshot


def _compile(
    source: HarnessSource,
    *,
    legal_terminal_results: tuple[str, ...] = ("COMPLETE", "BLOCKED", "REJECTED"),
):
    return compile_harness_source_in_memory(
        request_id="millforge-base-harness-test",
        source=source,
        stage_kind_id="millforge_base",
        legal_terminal_results=legal_terminal_results,
        capability_envelope=CapabilityEnvelope(
            grants=tuple(
                CapabilityGrant(capability_id=capability_id)
                for capability_id in sorted(PI_COMPAT_CAPABILITY_IDS)
            )
        ),
        tool_catalog=create_pi_compat_tool_snapshot(),
        model_profile_catalog=StaticModelProfileCatalogSnapshot(
            profiles={
                source.model_profile_id: CompiledModelProfile(
                    profile_id=source.model_profile_id
                )
            }
        ),
    )


def test_base_harness_source_has_exact_ten_node_policy_and_compiles() -> None:
    source = millforge_base_harness_source(
        model_profile_id="profile.base",
        system_instructions="Use the supplied tools.",
    )

    assert source.harness_id == "millforge.base.unrestricted_agent.v1"
    assert source.harness_version == 1
    assert source.stage_scope.stage_kind_ids == ("millforge_base",)
    assert source.model_profile_id == "profile.base"
    assert source.prompt.policy_id == "millforge.base.prompt.v1"
    assert source.prompt.include_request_context is True
    assert source.budgets.model_dump() == {
        "max_iterations": 100,
        "max_validation_retries": 4,
        "max_tool_errors": 16,
        "max_prerequisite_violations": 16,
        "max_premature_terminal_attempts": 8,
    }
    assert source.context.model_dump() == {
        "strategy_id": "forge.tiered.v1",
        "budget_tokens": 32768,
        "keep_recent_iterations": 4,
        "phase_thresholds": (0.6, 0.75, 0.9),
    }
    assert [node.node_id for node in source.graph.nodes] == [
        "read",
        "bash",
        "edit",
        "write",
        "grep",
        "find",
        "ls",
        "submit",
        "block",
        "reject",
    ]
    assert all(
        not node.required and not node.prerequisites and not node.produces
        for node in source.graph.nodes
    )
    assert {
        node.node_id: node.terminal_result
        for node in source.graph.nodes
        if node.terminal_result
    } == {"submit": "COMPLETE", "block": "BLOCKED", "reject": "REJECTED"}
    assert source.artifacts.declared_artifact_ids == ()
    assert source.artifacts.required_by_terminal == ()

    plan = _compile(source)

    assert [node.node_id for node in plan.nodes] == sorted(
        node.node_id for node in source.graph.nodes
    )
    assert {node.node_id: node.model_tool_name for node in plan.nodes} == {
        "read": "read",
        "bash": "bash",
        "edit": "edit",
        "write": "write",
        "grep": "grep",
        "find": "find",
        "ls": "ls",
        "submit": "submit",
        "block": "block",
        "reject": "reject",
    }


def test_custom_read_edit_submit_graph_reuses_descriptor_subset_normally() -> None:
    source = HarnessSource.model_validate(
        {
            "schema_version": "1.0",
            "kind": "millforge_harness",
            "harness_id": "millforge.test.custom_subset.v1",
            "harness_version": 1,
            "stage_scope": {"stage_kind_ids": ("millforge_base",)},
            "model_profile_id": "profile.base",
            "prompt": {
                "policy_id": "millforge.test.custom_subset.prompt.v1",
                "system_instructions": "Use only the declared tools.",
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
                "budget_tokens": 1024,
                "keep_recent_iterations": 1,
                "phase_thresholds": (0.5, 0.75, 0.9),
            },
            "graph": {
                "nodes": {
                    "read": {"tool_ref": "builtin.pi_compat.read@1"},
                    "edit": {"tool_ref": "builtin.pi_compat.edit@1"},
                    "submit": {
                        "tool_ref": "builtin.pi_compat.submit@1",
                        "terminal_result": "COMPLETE",
                    },
                }
            },
            "artifacts": {"declared_artifact_ids": (), "required_by_terminal": ()},
        }
    )

    plan = _compile(source, legal_terminal_results=("COMPLETE",))

    assert [node.node_id for node in plan.nodes] == ["edit", "read", "submit"]
    assert plan.required_capabilities == (
        "terminal.intent",
        "unrestricted.filesystem.read",
        "unrestricted.filesystem.write",
    )
