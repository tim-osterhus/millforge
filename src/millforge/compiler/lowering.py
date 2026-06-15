"""Lower resolved semantic compiler IR into immutable compiled plans."""

from __future__ import annotations

from collections.abc import Mapping
from importlib import metadata
from typing import Any

from pydantic import ValidationError

from millforge.compiled_plan import (
    ArgumentMatch,
    CompilerIdentity,
    CompiledArtifactPolicy,
    CompiledBudgetPolicy,
    CompiledContextPolicy,
    CompiledHarnessNode,
    CompiledHarnessPlan,
    CompiledPrerequisite,
    CompiledPromptPolicy,
    TerminalArtifactRequirement,
    ToolBindingRef,
    finalize_compiled_plan_sha256,
)
from millforge.compiler.canonicalization import source_sha256
from millforge.compiler.semantic import ResolvedHarness, ResolvedToolBinding

COMPILER_NAME = "millforge"
COMPILER_BUILD_ID = "millforge.compiler.lowering.v1"
_PLACEHOLDER_SHA256 = "0" * 64


class LoweringInvariantError(ValueError):
    """Raised when resolved semantic IR violates a lowering invariant."""


class CompiledPlanValidationError(ValueError):
    """Raised when the accepted compiled plan model rejects lowered values."""


class SourceSemanticHashError(ValueError):
    """Raised when canonical source semantic hash calculation fails."""


def lower_resolved_harness(resolved: ResolvedHarness) -> CompiledHarnessPlan:
    """Return a fully validated compiled plan for an accepted resolved harness."""
    source = resolved.source
    try:
        semantic_hash = source_sha256(resolved)
    except (TypeError, ValueError) as exc:
        raise SourceSemanticHashError(
            "source semantic hash calculation failed"
        ) from exc

    try:
        plan = CompiledHarnessPlan(
            schema_version=source.schema_version,
            kind="compiled_millforge_harness",
            harness_id=source.harness_id,
            harness_version=source.harness_version,
            source_sha256=semantic_hash,
            compiled_sha256=_PLACEHOLDER_SHA256,
            stage_kind_ids=tuple(sorted(source.stage_scope.stage_kind_ids)),
            model_profile=resolved.model_profile.model_copy(deep=True),
            prompt_policy=CompiledPromptPolicy(
                policy_id=source.prompt.policy_id,
                system_instructions=source.prompt.system_instructions,
                include_request_context=source.prompt.include_request_context,
            ),
            budgets=CompiledBudgetPolicy(
                max_iterations=source.budgets.max_iterations,
                max_validation_retries=source.budgets.max_validation_retries,
                max_tool_errors=source.budgets.max_tool_errors,
                max_prerequisite_violations=(
                    source.budgets.max_prerequisite_violations
                ),
                max_premature_terminal_attempts=(
                    source.budgets.max_premature_terminal_attempts
                ),
            ),
            context_policy=CompiledContextPolicy(
                strategy_id=source.context.strategy_id,
                budget_tokens=source.context.budget_tokens,
                keep_recent_iterations=source.context.keep_recent_iterations,
                phase_thresholds=(
                    source.context.phase_thresholds[0],
                    source.context.phase_thresholds[1],
                    source.context.phase_thresholds[2],
                ),
            ),
            nodes=tuple(
                _lower_node(node)
                for node in sorted(
                    resolved.resolved_nodes, key=lambda item: item.node_id
                )
            ),
            required_capabilities=tuple(sorted(resolved.required_capability_ids)),
            terminal_result_map=dict(sorted(resolved.terminal_result_map.items())),
            artifact_policy=CompiledArtifactPolicy(
                declared_artifact_ids=tuple(
                    sorted(source.artifacts.declared_artifact_ids)
                ),
                required_by_terminal=tuple(
                    TerminalArtifactRequirement(
                        terminal_result=item.terminal_result,
                        artifact_ids=tuple(sorted(item.artifact_ids)),
                    )
                    for item in sorted(
                        source.artifacts.required_by_terminal,
                        key=lambda requirement: requirement.terminal_result,
                    )
                ),
            ),
            compiler_identity=compiler_identity(),
        )
        return finalize_compiled_plan_sha256(plan)
    except CompiledPlanValidationError:
        raise
    except ValidationError as exc:
        raise CompiledPlanValidationError("compiled plan validation failed") from exc
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise LoweringInvariantError("lowering invariant failed") from exc


def compiler_identity() -> CompilerIdentity:
    """Return the deterministic compiler identity embedded in compiled plans."""
    return CompilerIdentity(
        name=COMPILER_NAME,
        version=_installed_version(),
        build_id=COMPILER_BUILD_ID,
    )


def _lower_node(node: ResolvedToolBinding) -> CompiledHarnessNode:
    descriptor = node.descriptor
    source = node.source
    return CompiledHarnessNode(
        node_id=node.node_id,
        model_tool_name=descriptor.model_tool_name,
        description=descriptor.description,
        input_schema=_fresh_json_object(descriptor.input_schema),
        binding=ToolBindingRef(
            tool_id=node.binding.tool_id,
            tool_version=node.binding.tool_version,
            descriptor_sha256=node.binding.descriptor_sha256,
            implementation_id=node.binding.implementation_id,
        ),
        prerequisites=tuple(
            CompiledPrerequisite(
                node_id=prerequisite.node_id,
                argument_matches=tuple(
                    ArgumentMatch(
                        prerequisite_argument=match.prior_argument,
                        current_argument=match.current_argument,
                    )
                    for match in sorted(
                        prerequisite.argument_matches,
                        key=lambda item: (item.prior_argument, item.current_argument),
                    )
                ),
            )
            for prerequisite in sorted(
                source.prerequisites, key=lambda item: item.node_id
            )
        ),
        required=source.required,
        terminal_result=source.terminal_result,
        required_capabilities=tuple(sorted(descriptor.required_capabilities)),
        produced_artifact_ids=tuple(sorted(descriptor.produced_artifact_ids)),
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
    )


def _installed_version() -> str:
    try:
        return metadata.version("millforge")
    except metadata.PackageNotFoundError:
        from millforge import __version__

        return __version__


def _fresh_json_object(value: Any) -> dict[str, Any]:
    copied = _fresh_json_value(value)
    if not isinstance(copied, dict):
        raise TypeError("expected JSON object")
    return copied


def _fresh_json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _fresh_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_fresh_json_value(item) for item in value]
    raise TypeError(f"unsupported JSON value {type(value).__name__}")
