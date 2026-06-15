"""Canonical semantic payload construction for deterministic compiler hashing."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from typing import Any, TypeAlias

from millforge import canonical_json_serialize
from millforge.compiler.semantic import ResolvedHarness

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


def canonical_semantic_payload(resolved: ResolvedHarness) -> JsonObject:
    """Return the validated source semantics used for ``source_sha256``.

    The payload is built from fresh JSON-compatible containers and excludes
    request identity, parser metadata, source locations, output paths, catalog
    snapshot identity, and catalog-owned objects.
    """
    source = resolved.source
    return _fresh_json_object(
        {
            "schema_version": source.schema_version,
            "kind": source.kind,
            "harness_id": source.harness_id,
            "harness_version": source.harness_version,
            "stage_kind_ids": sorted(source.stage_scope.stage_kind_ids),
            "model_profile": _fresh_json_value(
                resolved.model_profile.model_dump(mode="json")
            ),
            "prompt": {
                "policy_id": source.prompt.policy_id,
                "system_instructions": source.prompt.system_instructions,
                "include_request_context": source.prompt.include_request_context,
            },
            "budgets": {
                "max_iterations": source.budgets.max_iterations,
                "max_validation_retries": source.budgets.max_validation_retries,
                "max_tool_errors": source.budgets.max_tool_errors,
                "max_prerequisite_violations": (
                    source.budgets.max_prerequisite_violations
                ),
                "max_premature_terminal_attempts": (
                    source.budgets.max_premature_terminal_attempts
                ),
            },
            "context": {
                "strategy_id": source.context.strategy_id,
                "budget_tokens": source.context.budget_tokens,
                "keep_recent_iterations": source.context.keep_recent_iterations,
                "phase_thresholds": list(source.context.phase_thresholds),
            },
            "nodes": [_node_payload(node) for node in resolved.resolved_nodes],
            "required_node_ids": sorted(resolved.required_node_ids),
            "required_capability_ids": sorted(resolved.required_capability_ids),
            "terminal_result_map": dict(sorted(resolved.terminal_result_map.items())),
            "artifacts": {
                "declared_artifact_ids": sorted(source.artifacts.declared_artifact_ids),
                "required_by_terminal": [
                    {
                        "terminal_result": item.terminal_result,
                        "artifact_ids": sorted(item.artifact_ids),
                    }
                    for item in sorted(
                        source.artifacts.required_by_terminal,
                        key=lambda item: item.terminal_result,
                    )
                ],
                "producer_evidence": [
                    {
                        "artifact_id": item.artifact_id,
                        "all_producer_node_ids": sorted(item.all_producer_node_ids),
                        "terminal_gated_producer_node_ids": sorted(
                            item.terminal_gated_producer_node_ids
                        ),
                    }
                    for item in sorted(
                        resolved.artifact_evidence,
                        key=lambda item: item.artifact_id,
                    )
                ],
            },
        }
    )


def canonical_semantic_bytes(resolved: ResolvedHarness) -> bytes:
    """Return UTF-8 canonical JSON bytes for a resolved semantic payload."""
    return canonical_json_serialize(canonical_semantic_payload(resolved)).encode(
        "utf-8"
    )


def source_sha256(resolved: ResolvedHarness) -> str:
    """Return SHA-256 over the canonical validated semantic payload."""
    return hashlib.sha256(canonical_semantic_bytes(resolved)).hexdigest()


def _node_payload(node: Any) -> JsonObject:
    source = node.source
    descriptor = node.descriptor
    return _fresh_json_object(
        {
            "node_id": node.node_id,
            "required": source.required,
            "terminal_result": source.terminal_result,
            "prerequisites": [
                {
                    "node_id": prerequisite.node_id,
                    "argument_matches": [
                        {
                            "prior_argument": match.prior_argument,
                            "current_argument": match.current_argument,
                        }
                        for match in sorted(
                            prerequisite.argument_matches,
                            key=lambda item: (
                                item.prior_argument,
                                item.current_argument,
                            ),
                        )
                    ],
                }
                for prerequisite in sorted(
                    source.prerequisites, key=lambda item: item.node_id
                )
            ],
            "produces": sorted(source.produces),
            "binding": {
                "tool_id": node.binding.tool_id,
                "tool_version": node.binding.tool_version,
                "descriptor_sha256": node.binding.descriptor_sha256,
                "implementation_id": node.binding.implementation_id,
            },
            "descriptor": {
                "model_tool_name": descriptor.model_tool_name,
                "description": descriptor.description,
                "input_schema": _fresh_json_value(descriptor.input_schema),
                "output_schema": _fresh_json_value(descriptor.output_schema),
                "side_effect_class": descriptor.side_effect_class.value,
                "idempotency": descriptor.idempotency.value,
                "required_capabilities": sorted(descriptor.required_capabilities),
                "produced_artifact_ids": sorted(descriptor.produced_artifact_ids),
            },
        }
    )


def _fresh_json_object(value: Mapping[str, Any]) -> JsonObject:
    converted = _fresh_json_value(value)
    if not isinstance(converted, dict):
        raise TypeError("expected JSON object")
    return converted


def _fresh_json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        return {str(key): _fresh_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_fresh_json_value(item) for item in value]
    return copy.deepcopy(value)
