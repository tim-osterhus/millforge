"""Ordinary compiler source for the unrestricted Millforge base preset."""

from __future__ import annotations

from millforge.compiler.source import HarnessSource

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


def millforge_base_harness_source(
    *,
    model_profile_id: str,
    system_instructions: str,
) -> HarnessSource:
    """Materialize the unrestricted preset as an ordinary harness source."""

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
                    for node_id, tool_ref, terminal_result in _NODES
                )
            },
            "artifacts": {
                "declared_artifact_ids": (),
                "required_by_terminal": (),
            },
        }
    )
