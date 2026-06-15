"""Tests for compiler source contracts and shared validators."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from millforge.compiler import (
    HarnessSource,
    parse_tool_reference,
    validate_argument_name,
    validate_artifact_id,
    validate_harness_id,
    validate_node_id,
    validate_terminal_result,
    validate_tool_reference,
)


def _source_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "kind": "millforge_harness",
        "harness_id": "millforge.test.builder.compiler.v1",
        "harness_version": 1,
        "stage_scope": {"stage_kind_ids": ["builder"]},
        "model_profile_id": "fake.builder.v1",
        "prompt": {
            "policy_id": "millforge.test.builder.policy.v1",
            "system_instructions": "Use the admitted tools.",
            "include_request_context": True,
        },
        "budgets": {
            "max_iterations": 12,
            "max_validation_retries": 2,
            "max_tool_errors": 2,
            "max_prerequisite_violations": 2,
            "max_premature_terminal_attempts": 2,
        },
        "context": {
            "strategy_id": "forge.tiered.v1",
            "budget_tokens": 12000,
            "keep_recent_iterations": 2,
            "phase_thresholds": [0.60, 0.75, 0.90],
        },
        "graph": {
            "nodes": {
                "read_file": {"tool_ref": "builtin.workspace.read_file@1"},
                "apply_patch": {
                    "tool_ref": "builtin.workspace.apply_patch@1",
                    "prerequisites": [
                        {
                            "node_id": "read_file",
                            "argument_matches": {"path": "path"},
                        }
                    ],
                    "produces": ["patch_summary"],
                },
                "submit_patch": {
                    "tool_ref": "builtin.terminal.submit@1",
                    "terminal_result": "BUILDER_COMPLETE",
                    "prerequisites": [{"node_id": "apply_patch"}],
                },
            }
        },
        "artifacts": {
            "declared_artifact_ids": ["patch_summary"],
            "required_by_terminal": {"BUILDER_COMPLETE": ["patch_summary"]},
        },
    }


def test_harness_source_converts_authoring_mappings_to_immutable_records() -> None:
    payload = _source_payload()
    source = HarnessSource.model_validate(payload)

    assert source.graph.nodes[0].node_id == "read_file"
    assert isinstance(source.graph.nodes, tuple)
    apply_patch = source.graph.nodes[1]
    assert apply_patch.prerequisites[0].argument_matches[0].prior_argument == "path"
    assert (
        source.artifacts.required_by_terminal[0].terminal_result == "BUILDER_COMPLETE"
    )


def test_harness_source_rejects_unknown_nested_fields_and_scalar_coercion() -> None:
    payload = _source_payload()
    prompt = payload["prompt"]
    assert isinstance(prompt, dict)
    prompt["unexpected"] = "field"

    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)

    payload = _source_payload()
    budgets = payload["budgets"]
    assert isinstance(budgets, dict)
    budgets["max_iterations"] = "12"

    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)

    payload = _source_payload()
    prompt = payload["prompt"]
    assert isinstance(prompt, dict)
    prompt["include_request_context"] = 1

    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)


def test_harness_graph_mapping_rejects_body_level_node_id_override() -> None:
    payload = _source_payload()
    nodes = payload["graph"]
    assert isinstance(nodes, dict)
    node_body = nodes["nodes"]["read_file"]
    assert isinstance(node_body, dict)
    node_body["node_id"] = "other_node"

    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)


def test_source_models_deep_snapshot_mutable_inputs() -> None:
    payload = _source_payload()
    source = HarnessSource.model_validate(payload)

    graph = payload["graph"]
    assert isinstance(graph, dict)
    nodes = graph["nodes"]
    assert isinstance(nodes, dict)
    nodes["later"] = {"tool_ref": "builtin.workspace.read_file@1"}

    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)
    declared = artifacts["declared_artifact_ids"]
    assert isinstance(declared, list)
    declared.append("later")

    assert tuple(node.node_id for node in source.graph.nodes) == (
        "read_file",
        "apply_patch",
        "submit_patch",
    )
    assert source.artifacts.declared_artifact_ids == ("patch_summary",)


def test_source_dump_then_validate_is_stable_and_deeply_immutable() -> None:
    source = HarnessSource.model_validate(_source_payload())
    dumped = source.model_dump(mode="json")
    restored = HarnessSource.model_validate(json.loads(json.dumps(dumped)))

    assert restored == source
    with pytest.raises(ValidationError):
        source.graph.nodes[0].node_id = "other_node"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        HarnessSource.model_validate({**dumped, "unknown": "field"})


@pytest.mark.parametrize(
    ("validator", "valid", "invalid"),
    [
        (validate_harness_id, "millforge.test-1_builder.v1", "Millforge"),
        (validate_node_id, "node_1", "node-1"),
        (validate_artifact_id, "artifact.id-1", "Artifact"),
        (validate_terminal_result, "BUILDER_COMPLETE", "builder_complete"),
        (validate_argument_name, "_arg1", "1arg"),
        (validate_tool_reference, "builtin.workspace.read_file@1", "builtin.latest"),
    ],
)
def test_shared_identifier_validators_enforce_source_grammar(
    validator: object, valid: str, invalid: str
) -> None:
    assert callable(validator)
    assert validator(valid) == valid
    with pytest.raises(ValueError):
        validator(invalid)


def test_tool_reference_parser_rejects_aliases_ranges_and_bad_versions() -> None:
    parsed = parse_tool_reference("builtin.workspace.read_file@2147483647")
    assert parsed.tool_id == "builtin.workspace.read_file"
    assert parsed.version == 2_147_483_647

    for value in (
        "builtin.workspace.read_file",
        "builtin.workspace.read_file@latest",
        "builtin.workspace.read_file@0",
        "builtin.workspace.read_file@01",
        "builtin.workspace.read_file@2147483648",
        "builtin.workspace.read_file@1..2",
    ):
        with pytest.raises(ValueError):
            parse_tool_reference(value)


def test_budget_and_context_ranges_match_contract() -> None:
    payload = _source_payload()
    source = HarnessSource.model_validate(payload)
    assert source.budgets.max_iterations == 12
    assert source.context.phase_thresholds == (0.6, 0.75, 0.9)

    payload = _source_payload()
    context = payload["context"]
    assert isinstance(context, dict)
    context["phase_thresholds"] = [0.8, 0.7, 0.9]
    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)

    payload = _source_payload()
    context = payload["context"]
    assert isinstance(context, dict)
    context["budget_tokens"] = 255
    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)

    payload = _source_payload()
    budgets = payload["budgets"]
    assert isinstance(budgets, dict)
    budgets["max_iterations"] = True
    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)

    payload = _source_payload()
    context = payload["context"]
    assert isinstance(context, dict)
    context["phase_thresholds"] = [0.6, float("inf"), 0.9]
    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)


def test_source_rejects_duplicate_artifact_stage_and_confusable_identifier_values() -> (
    None
):
    payload = _source_payload()
    stage_scope = payload["stage_scope"]
    assert isinstance(stage_scope, dict)
    stage_scope["stage_kind_ids"] = ["builder", "builder"]
    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)

    payload = _source_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)
    artifacts["declared_artifact_ids"] = ["patch_summary", "patch_summary"]
    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)

    payload = _source_payload()
    payload["harness_id"] = "millforge.test.bu\u0456lder.v1"
    with pytest.raises(ValidationError):
        HarnessSource.model_validate(payload)
