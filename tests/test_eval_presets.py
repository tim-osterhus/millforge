"""Regression tests for public Spec 07 compact-eval preset metadata."""

from __future__ import annotations

import json
import re
from collections import Counter

import millforge
import millforge.eval_presets as eval_presets
import pytest

from millforge.eval_modes import EVAL_DEFAULT_MODEL_PROFILE_ID, EVAL_SPEC_07_HARNESS_IDS
from millforge.eval_presets import (
    EVAL_PRESET_HARNESS_IDS,
    EVAL_PRESET_MODEL_PROFILE_ID,
    EvalPresetReadinessStatus,
    eval_preset_contract_gaps,
    eval_preset_source_record,
    iter_eval_preset_compile_cases,
    iter_eval_preset_source_records,
)
from millforge.eval_workflow import EvalStageId, default_compact_eval_workflow_graph

DENIED_PUBLIC_TOKENS = (
    "millrace-agents",
    "ideas/",
    "ref-forge",
    "api_key",
    "credential",
    "password",
    "provider endpoint",
    "live run",
    "live-run",
)
WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'`([{<])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`)\]}>,]*"
)
POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'`([{<])/(?!/)[^\s\"'`)\]}>,]*")
USER_HOME_PATH = re.compile(r"(?:^|[\s\"'`([{<])~(?:/|\\)[^\s\"'`)\]}>,]*")


def _render_public_payload() -> str:
    payload = {
        "source_records": [
            record.model_dump(mode="json")
            for record in iter_eval_preset_source_records()
        ],
        "compile_cases": [
            case.model_dump(mode="json") for case in iter_eval_preset_compile_cases()
        ],
        "contract_gaps": [
            gap.model_dump(mode="json") for gap in eval_preset_contract_gaps()
        ],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False)


def test_public_eval_preset_names_are_root_exported() -> None:
    for name in eval_presets.__all__:
        assert getattr(millforge, name) is getattr(eval_presets, name)
        assert name in millforge.__all__


def test_spec_07_harness_ids_have_exactly_one_source_record() -> None:
    records = iter_eval_preset_source_records()
    harness_counts = Counter(record.harness_id for record in records)

    assert EVAL_PRESET_HARNESS_IDS == EVAL_SPEC_07_HARNESS_IDS
    assert set(harness_counts) == set(EVAL_SPEC_07_HARNESS_IDS.values())
    assert all(count == 1 for count in harness_counts.values())
    for harness_id in EVAL_SPEC_07_HARNESS_IDS.values():
        assert eval_preset_source_record(harness_id).harness_id == harness_id


def test_preset_records_pin_current_stage_ids_and_terminal_results() -> None:
    graph = default_compact_eval_workflow_graph()
    records = iter_eval_preset_source_records()
    cases = iter_eval_preset_compile_cases()

    assert tuple(record.stage_id for record in records) == tuple(EvalStageId)
    assert tuple(case.stage_id for case in cases) == tuple(EvalStageId)
    assert {stage_id.value for stage_id in (record.stage_id for record in records)} == {
        "eval_planner",
        "eval_builder",
        "eval_checker",
        "eval_arbiter",
    }
    assert {case.stage_id: case.legal_terminal_results for case in cases} == {
        stage_id: graph.stage_contracts[stage_id].legal_terminal_results
        for stage_id in EvalStageId
    }


def test_preset_model_profile_tracks_default_eval_profile() -> None:
    assert EVAL_PRESET_MODEL_PROFILE_ID == EVAL_DEFAULT_MODEL_PROFILE_ID
    assert {
        record.model_profile_id for record in iter_eval_preset_source_records()
    } == {EVAL_DEFAULT_MODEL_PROFILE_ID}


def test_preset_artifact_ids_are_logical_not_layout_filenames() -> None:
    graph = default_compact_eval_workflow_graph()

    for case in iter_eval_preset_compile_cases():
        contract = graph.stage_contracts[case.stage_id]
        artifact_ids = case.input_artifact_ids + case.output_artifact_ids
        assert case.input_artifact_ids == contract.input_artifact_ids
        assert case.output_artifact_ids == contract.output_artifact_ids
        assert artifact_ids
        assert all("/" not in artifact_id for artifact_id in artifact_ids)
        assert all("\\" not in artifact_id for artifact_id in artifact_ids)
        assert all(not artifact_id.endswith(".json") for artifact_id in artifact_ids)
        assert "plan.json" not in artifact_ids


def test_legacy_builtin_compiler_fixtures_are_not_spec_07_presets() -> None:
    public_harness_ids = {
        record.harness_id for record in iter_eval_preset_source_records()
    }

    assert all(
        not harness_id.startswith("millforge.test.builtin.")
        for harness_id in public_harness_ids
    )
    with pytest.raises(KeyError):
        eval_preset_source_record("millforge.test.builtin.planner.v1")


def test_planner_preset_declares_no_workspace_authority() -> None:
    planner = eval_preset_source_record(EVAL_SPEC_07_HARNESS_IDS[EvalStageId.PLANNER])

    assert planner.workspace_tool_ids == ()
    assert planner.workspace_paths == ()
    assert "workspace.read" not in planner.compiler_capability_ids


def test_known_contract_gaps_are_public_and_later_packet_owned() -> None:
    gaps = eval_preset_contract_gaps()
    gaps_by_id = {gap.gap_id: gap for gap in gaps}

    assert set(gaps_by_id) == {
        "duplicate-exact-tool-references",
        "artifact-evidence-production",
        "capability-vocabulary-projection",
        "stale-review-draft-model-profile",
        "readiness-before-live-admission",
        "planner-workspace-authority-preservation",
    }
    assert {gap.owner for gap in gaps}.issubset({"07B", "07C", "07B/07C", "07D/07E"})
    assert all(gap.affected_stage_ids for gap in gaps)
    for record in iter_eval_preset_source_records():
        assert set(record.contract_gap_ids).issubset(gaps_by_id)
    for case in iter_eval_preset_compile_cases():
        assert set(case.contract_gap_ids).issubset(gaps_by_id)


def test_public_preset_metadata_contains_no_private_or_runtime_local_material() -> None:
    rendered = _render_public_payload()
    lowered = rendered.lower()

    for token in DENIED_PUBLIC_TOKENS:
        assert token.lower() not in lowered
    assert WINDOWS_ABSOLUTE_PATH.search(rendered) is None
    assert POSIX_ABSOLUTE_PATH.search(rendered) is None
    assert USER_HOME_PATH.search(rendered) is None


def test_spec_07_presets_remain_non_ready_metadata_only_records() -> None:
    allowed_statuses = {
        EvalPresetReadinessStatus.MISSING,
        EvalPresetReadinessStatus.BLOCKED_BY_CONTRACT_GAP,
    }

    for record in iter_eval_preset_source_records():
        assert record.readiness_status in allowed_statuses
        assert record.implemented is False
        assert record.compiled is False
        assert record.statically_available is False
        assert record.live_admitted is False
        assert record.execution_ready is False

    for case in iter_eval_preset_compile_cases():
        assert case.readiness_status in allowed_statuses
