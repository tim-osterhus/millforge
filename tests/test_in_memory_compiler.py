"""Bounded tests for the in-memory harness compiler entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from millforge import (
    CapabilityEnvelope,
    CapabilityGrant,
    canonical_compiled_plan_bytes,
)
from millforge.compiler import (
    HarnessCompileRequest,
    HarnessSource,
    InMemoryHarnessCompileError,
    compile,
    compile_harness_source_in_memory,
)
from tests.compiler.conftest import (
    StaticModelProfileCatalogSnapshot,
    StaticToolCatalogSnapshot,
    make_raw_tool_descriptor,
)


def _source_payload(
    *, instructions: str = "Compile deterministically."
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "kind": "millforge_harness",
        "harness_id": "millforge.test.in_memory.v1",
        "harness_version": 1,
        "stage_scope": {"stage_kind_ids": ["builder"]},
        "model_profile_id": "profile.standard",
        "prompt": {
            "policy_id": "millforge.test.in_memory.policy.v1",
            "system_instructions": instructions,
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
            "budget_tokens": 4096,
            "keep_recent_iterations": 1,
            "phase_thresholds": [0.5, 0.75, 0.9],
        },
        "graph": {
            "nodes": {
                "done": {
                    "tool_ref": "tools.echo@1",
                    "terminal_result": "BUILDER_COMPLETE",
                }
            }
        },
        "artifacts": {"declared_artifact_ids": [], "required_by_terminal": {}},
    }


def _catalogs() -> tuple[StaticToolCatalogSnapshot, StaticModelProfileCatalogSnapshot]:
    descriptor = make_raw_tool_descriptor(
        tool_id="tools.echo",
        implementation_id="impl.tools.echo.v1",
        model_tool_name="echo",
        produced_artifact_ids=(),
    )
    return (
        StaticToolCatalogSnapshot(entries={("tools.echo", 1): descriptor}),
        StaticModelProfileCatalogSnapshot(
            profiles={"profile.standard": {"profile_id": "profile.standard"}}
        ),
    )


def _capability_envelope() -> CapabilityEnvelope:
    return CapabilityEnvelope(grants=(CapabilityGrant(capability_id="workspace.read"),))


def _in_memory_compile(
    source: HarnessSource,
    *,
    stage_kind_id: str = "builder",
    legal_terminal_results: tuple[str, ...] = ("BUILDER_COMPLETE",),
):
    tool_catalog, model_profile_catalog = _catalogs()
    return compile_harness_source_in_memory(
        request_id="request.in_memory.v1",
        source=source,
        stage_kind_id=stage_kind_id,
        legal_terminal_results=legal_terminal_results,
        capability_envelope=_capability_envelope(),
        tool_catalog=tool_catalog,
        model_profile_catalog=model_profile_catalog,
    )


def _path_request(
    tmp_path: Path,
    payload: dict[str, object],
    *,
    stage_kind_id: str = "builder",
    legal_terminal_results: tuple[str, ...] = ("BUILDER_COMPLETE",),
) -> HarnessCompileRequest:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    source_root.mkdir()
    output_root.mkdir()
    (output_root / "compiled").mkdir()
    (source_root / "harness.json").write_text(json.dumps(payload), encoding="utf-8")
    return HarnessCompileRequest(
        request_id="request.in_memory.v1",
        source_path="harness.json",
        source_root=str(source_root),
        source_format="json",
        output_dir="compiled",
        output_root=str(output_root),
        expected_harness_id="millforge.test.in_memory.v1",
        stage_kind_id=stage_kind_id,
        legal_terminal_results=legal_terminal_results,
        capability_envelope=_capability_envelope(),
    )


def test_in_memory_compilation_matches_path_canonical_bytes(tmp_path: Path) -> None:
    payload = _source_payload()
    request = _path_request(tmp_path, payload)
    tool_catalog, model_profile_catalog = _catalogs()

    path_result = compile(
        request,
        tool_catalog=tool_catalog,
        model_profile_catalog=model_profile_catalog,
    )
    plan = _in_memory_compile(HarnessSource.model_validate(payload))

    assert path_result.compiled_plan_path is not None
    assert (
        canonical_compiled_plan_bytes(plan)
        == Path(
            request.output_root,
            path_result.compiled_plan_path,
        ).read_bytes()
    )


@pytest.mark.parametrize(
    ("stage_kind_id", "legal_terminal_results", "instructions", "expected_code"),
    (
        ("arbiter", ("BUILDER_COMPLETE",), "Compile deterministically.", "MF-S028"),
        ("builder", ("BLOCKED",), "Compile deterministically.", "MF-S029"),
        (
            "builder",
            ("BUILDER_COMPLETE",),
            "Bearer abcdefghijklmnop",
            "MF-S026",
        ),
        (
            "builder",
            ("BUILDER_COMPLETE",),
            "Resolved context: OPENAI_API_KEY=abcdefghijklmnopqrstuvwxyz",
            "MF-S026",
        ),
    ),
)
def test_in_memory_negative_parity_matches_path_diagnostic_codes(
    tmp_path: Path,
    stage_kind_id: str,
    legal_terminal_results: tuple[str, ...],
    instructions: str,
    expected_code: str,
) -> None:
    payload = _source_payload(instructions=instructions)
    request = _path_request(
        tmp_path,
        payload,
        stage_kind_id=stage_kind_id,
        legal_terminal_results=legal_terminal_results,
    )
    tool_catalog, model_profile_catalog = _catalogs()

    path_result = compile(
        request,
        tool_catalog=tool_catalog,
        model_profile_catalog=model_profile_catalog,
    )
    with pytest.raises(InMemoryHarnessCompileError) as exc_info:
        _in_memory_compile(
            HarnessSource.model_validate(payload),
            stage_kind_id=stage_kind_id,
            legal_terminal_results=legal_terminal_results,
        )

    assert [diagnostic.code for diagnostic in path_result.diagnostics] == [
        expected_code
    ]
    assert [diagnostic.code for diagnostic in exc_info.value.diagnostics] == [
        expected_code
    ]


def test_in_memory_compiler_writes_nothing_and_exposes_only_diagnostics(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("preserved\n", encoding="utf-8")
    before = tuple(path.name for path in tmp_path.iterdir())

    _in_memory_compile(HarnessSource.model_validate(_source_payload()))
    with pytest.raises(InMemoryHarnessCompileError) as exc_info:
        _in_memory_compile(
            HarnessSource.model_validate(_source_payload()),
            stage_kind_id="arbiter",
        )

    error = exc_info.value
    empty_error = InMemoryHarnessCompileError(())
    assert tuple(path.name for path in tmp_path.iterdir()) == before
    assert error.__dict__ == {"diagnostics": error.diagnostics}
    assert isinstance(error.diagnostics, tuple)
    assert str(error) == error.diagnostics[0].message
    assert all(diagnostic.source_reference is None for diagnostic in error.diagnostics)
    assert str(tmp_path) not in str(error)
    assert empty_error.__dict__ == {"diagnostics": ()}
    assert str(empty_error) == "in-memory harness compilation failed"
