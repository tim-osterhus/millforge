"""Ordinary compiler source for the unrestricted Millforge base preset."""

from __future__ import annotations

from millforge.compiler.source import HarnessSource
from millforge.compiler.validators import validate_terminal_result, validate_unique
from millforge.tools.pi_compat_catalog import (
    DEFAULT_BASE_TERMINAL_RESULTS,
    _terminal_token,
)

__all__ = ["millforge_base_harness_source"]

_CONFIG_ID = "millforge-base.v1"
_HARNESS_ID = "millforge.base.unrestricted_agent.v1"
_TOOL_PACK_ID = "millforge.toolpack.pi_compat.unrestricted.v1"
_STAGE_KIND = "millforge_base"
_PROMPT_POLICY_ID = "millforge.base.prompt.v1"

_NODES = (
    ("read", "builtin.pi_compat.read@1", None),
    ("bash", "builtin.pi_compat.bash@1", None),
    ("edit", "builtin.pi_compat.edit@1", None),
    ("write", "builtin.pi_compat.write@1", None),
    ("grep", "builtin.pi_compat.grep@1", None),
    ("find", "builtin.pi_compat.find@1", None),
    ("ls", "builtin.pi_compat.ls@1", None),
    ("submit", "builtin.pi_compat.submit@1", "COMPLETE"),
    ("block", "builtin.pi_compat.block@1", "BLOCKED"),
    ("reject", "builtin.pi_compat.reject@1", "REJECTED"),
)


def canonicalize_base_terminal_results(
    legal_terminal_results: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate and sort the bounded terminal vocabulary once at the public edge."""

    if not isinstance(legal_terminal_results, tuple):
        raise ValueError("legal_terminal_results must be a tuple")
    if not 1 <= len(legal_terminal_results) <= 64:
        raise ValueError("legal_terminal_results must contain 1 through 64 values")
    if any(not isinstance(result, str) for result in legal_terminal_results):
        raise ValueError("legal_terminal_results values must be strings")
    for result in legal_terminal_results:
        validate_terminal_result(result)
    validate_unique(legal_terminal_results, "legal_terminal_results")
    return tuple(sorted(legal_terminal_results))


def _terminal_nodes(
    legal_terminal_results: tuple[str, ...],
) -> tuple[tuple[str, str, str], ...]:
    if legal_terminal_results == DEFAULT_BASE_TERMINAL_RESULTS:
        return _NODES[-3:]
    tokens = tuple(_terminal_token(result) for result in legal_terminal_results)
    if len(set(tokens)) != len(tokens):
        raise ValueError("configured terminal tool identities collide")
    return tuple(
        (
            f"terminal_{token}",
            f"builtin.pi_compat.terminal.{token}@1",
            result,
        )
        for result, token in zip(legal_terminal_results, tokens, strict=True)
    )


def millforge_base_harness_source(
    *,
    model_profile_id: str,
    system_instructions: str,
) -> HarnessSource:
    """Materialize the unrestricted preset as an ordinary harness source."""

    return _millforge_base_harness_source_for_terminal_results(
        model_profile_id=model_profile_id,
        system_instructions=system_instructions,
        legal_terminal_results=DEFAULT_BASE_TERMINAL_RESULTS,
    )


def _millforge_base_harness_source_for_terminal_results(
    *,
    model_profile_id: str,
    system_instructions: str,
    legal_terminal_results: tuple[str, ...],
) -> HarnessSource:
    """Materialize the preset from already-canonical terminal configuration."""

    return HarnessSource.model_validate(
        {
            "schema_version": "1.0",
            "kind": "millforge_harness",
            "harness_id": _HARNESS_ID,
            "harness_version": 1,
            "stage_scope": {"stage_kind_ids": (_STAGE_KIND,)},
            "model_profile_id": model_profile_id,
            "prompt": {
                "policy_id": _PROMPT_POLICY_ID,
                "system_instructions": system_instructions,
                "include_request_context": True,
            },
            "budgets": {
                "max_iterations": 100,
                "max_validation_retries": 4,
                "max_tool_errors": 16,
                "max_prerequisite_violations": 16,
                "max_premature_terminal_attempts": 8,
            },
            "context": {
                "strategy_id": "forge.tiered.v1",
                "budget_tokens": 32_768,
                "keep_recent_iterations": 4,
                "phase_thresholds": (0.6, 0.75, 0.9),
            },
            "graph": {
                "nodes": tuple(
                    {
                        "node_id": node_id,
                        "tool_ref": tool_ref,
                        "required": False,
                        "prerequisites": (),
                        "terminal_result": terminal_result,
                        "produces": (),
                    }
                    for node_id, tool_ref, terminal_result in (
                        *_NODES[:-3],
                        *_terminal_nodes(legal_terminal_results),
                    )
                )
            },
            "artifacts": {
                "declared_artifact_ids": (),
                "required_by_terminal": (),
            },
        }
    )
