"""Tests for compiler request, invocation, and result contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from millforge.compiler import (
    CompileInvocation,
    CompileStatus,
    CompilerDiagnostic,
    CompilerPhase,
    DefaultHarnessCompileRequestAdmission,
    DiagnosticReportState,
    DiagnosticField,
    DiagnosticSeverity,
    HarnessCompileRequest,
    HarnessCompileResult,
    PlanCommitCertainty,
)
from millforge.contracts import CapabilityEnvelope, CapabilityGrant

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


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
                "submit_patch": {
                    "tool_ref": "builtin.terminal.submit@1",
                    "terminal_result": "BUILDER_COMPLETE",
                    "prerequisites": [{"node_id": "read_file"}],
                },
                "submit_blocked": {
                    "tool_ref": "builtin.terminal.submit@1",
                    "terminal_result": "BLOCKED",
                    "prerequisites": [{"node_id": "read_file"}],
                },
            }
        },
        "artifacts": {
            "declared_artifact_ids": ["patch_summary"],
            "required_by_terminal": {"BUILDER_COMPLETE": ["patch_summary"]},
        },
    }


def _request() -> HarnessCompileRequest:
    return HarnessCompileRequest(
        request_id="request.c48f9299",
        source_path="harness.yaml",
        source_root="/tmp/source",
        source_format="yaml",
        output_dir="out",
        output_root="/tmp/output",
        expected_harness_id="millforge.test.builder.compiler.v1",
        stage_kind_id="builder",
        legal_terminal_results=("BUILDER_COMPLETE", "BLOCKED"),
        capability_envelope=CapabilityEnvelope(
            grants=(
                CapabilityGrant(
                    capability_id="workspace.read",
                    constraints={"paths": ["src"]},
                ),
            )
        ),
    )


def _raw_request(tmp_path: Path) -> dict[str, object]:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    source_root.mkdir(parents=True)
    output_root.mkdir(parents=True)
    (output_root / "compiled").mkdir()
    (source_root / "harness.json").write_text(
        json.dumps(_source_payload()), encoding="utf-8"
    )
    return {
        "request_id": "request.c48f9299",
        "source_path": "harness.json",
        "source_root": str(source_root),
        "source_format": "json",
        "output_dir": "compiled",
        "output_root": str(output_root),
        "expected_harness_id": "millforge.test.builder.compiler.v1",
        "stage_kind_id": "builder",
        "legal_terminal_results": ("BUILDER_COMPLETE", "BLOCKED"),
        "capability_envelope": {"grants": ()},
    }


def _write_source_payload(raw: dict[str, object], payload: dict[str, object]) -> None:
    Path(str(raw["source_root"]), str(raw["source_path"])).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _diagnostic(index: int, *, field_count: int = 0) -> CompilerDiagnostic:
    fields = tuple(
        DiagnosticField(key=f"field_{field}", value="x" * 512)
        for field in range(field_count)
    )
    return CompilerDiagnostic(
        code="MF-S020",
        phase=CompilerPhase.SCHEMA,
        severity=DiagnosticSeverity.ERROR,
        message=f"Diagnostic {index:03d}.",
        fields=fields,
    )


def test_compile_invocation_deep_snapshots_request_capability_constraints() -> None:
    request = _request()
    invocation = CompileInvocation.from_request(request)

    constraints = request.capability_envelope.grants[0].constraints
    assert constraints is not None
    constraints["paths"].append("tests")

    invocation_constraints = invocation.request.capability_envelope.grants[
        0
    ].constraints
    assert invocation_constraints is not None
    assert invocation_constraints["paths"] == ("src",)
    with pytest.raises(AttributeError):
        invocation_constraints["paths"].append("mutated")


def test_compile_request_serializes_and_round_trips_public_json() -> None:
    request = _request()

    serialized = request.model_dump_json()
    restored = HarnessCompileRequest.model_validate_json(serialized)

    assert restored.model_dump(mode="json") == request.model_dump(mode="json")
    assert restored.capability_envelope.grants[0].capability_id == "workspace.read"
    assert restored.capability_envelope.grants[0].constraints == {"paths": ["src"]}


def test_compile_result_rejects_prepared_committed_without_plan_publication_evidence() -> (
    None
):
    with pytest.raises(ValidationError):
        HarnessCompileResult(
            request_id="request.c48f9299",
            status=CompileStatus.PREPARED,
            plan_commit_certainty=PlanCommitCertainty.COMMITTED,
            compiled_plan_path="compiled/plan.json",
        )

    with pytest.raises(ValidationError):
        HarnessCompileResult(
            request_id="request.c48f9299",
            status=CompileStatus.PREPARED,
            plan_commit_certainty=PlanCommitCertainty.COMMITTED,
            compiled_sha256=SHA_A,
        )

    prepared = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.PREPARED,
        plan_commit_certainty=PlanCommitCertainty.COMMITTED,
        source_document_sha256=SHA_A,
        source_sha256=SHA_B,
        harness_id="millforge.test.builder.compiler.v1",
        compiled_plan_path="compiled/plan.json",
        compiled_sha256=SHA_C,
    )
    assert prepared.compiled_plan_path == "compiled/plan.json"
    assert prepared.compiled_sha256 == SHA_C


def test_compile_result_serializes_and_round_trips_public_json() -> None:
    result = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.COMMITTED,
        plan_commit_certainty=PlanCommitCertainty.COMMITTED,
        diagnostic_report_state=DiagnosticReportState.COMMITTED,
        source_document_sha256=SHA_A,
        source_sha256=SHA_B,
        request_identity_sha256=SHA_A,
        harness_id="millforge.test.builder.compiler.v1",
        compiled_plan_path="compiled/plan.json",
        compiled_sha256=SHA_C,
        diagnostics_path="compiled/diagnostics.json",
    )

    serialized = result.model_dump_json()
    restored = HarnessCompileResult.model_validate_json(serialized)

    assert restored.model_dump(mode="json") == result.model_dump(mode="json")
    assert "source_semantic_sha256" not in serialized
    assert restored.source_sha256 == SHA_B
    assert restored.request_identity_sha256 == SHA_A
    assert restored.status == CompileStatus.COMMITTED
    assert restored.plan_commit_certainty == PlanCommitCertainty.COMMITTED
    assert restored.diagnostic_report_state == DiagnosticReportState.COMMITTED
    assert restored.compiled_plan_path == "compiled/plan.json"
    assert restored.compiled_sha256 == SHA_C


def test_compile_result_accepts_legacy_semantic_hash_input_but_dumps_source_sha() -> (
    None
):
    restored = HarnessCompileResult.model_validate(
        {
            "request_id": "request.c48f9299",
            "status": "prepared",
            "plan_commit_certainty": "absent",
            "source_document_sha256": SHA_A,
            "source_semantic_sha256": SHA_B,
            "harness_id": "millforge.test.builder.compiler.v1",
        }
    )

    assert restored.source_sha256 == SHA_B
    assert "source_sha256" in restored.model_dump(mode="json")
    assert "source_semantic_sha256" not in restored.model_dump(mode="json")


def test_compile_result_diagnostic_report_state_requires_safe_relative_path() -> None:
    result = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.PREPARED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        diagnostic_report_state=DiagnosticReportState.PREPARED,
        source_document_sha256=SHA_A,
        source_sha256=SHA_B,
        harness_id="millforge.test.builder.compiler.v1",
        diagnostics_path="compiled/diagnostics.json",
    )
    assert result.diagnostic_report_state == DiagnosticReportState.PREPARED

    with pytest.raises(ValidationError):
        HarnessCompileResult(
            request_id="request.c48f9299",
            status=CompileStatus.PREPARED,
            plan_commit_certainty=PlanCommitCertainty.ABSENT,
            diagnostic_report_state=DiagnosticReportState.PREPARED,
            source_document_sha256=SHA_A,
            source_sha256=SHA_B,
            harness_id="millforge.test.builder.compiler.v1",
        )

    with pytest.raises(ValidationError):
        HarnessCompileResult(
            request_id="request.c48f9299",
            status=CompileStatus.PREPARED,
            plan_commit_certainty=PlanCommitCertainty.ABSENT,
            source_document_sha256=SHA_A,
            source_sha256=SHA_B,
            harness_id="millforge.test.builder.compiler.v1",
            diagnostics_path="/tmp/diagnostics.json",
        )

    for unsafe_path in (
        "compiled/../diagnostics.json",
        "compiled/diagnostics.json:stream",
        "compiled/CON.json",
        "compiled/nested\\diagnostics.json",
        "compiled/trailing.",
        "compiled/trailing ",
    ):
        with pytest.raises(ValidationError):
            HarnessCompileResult(
                request_id="request.c48f9299",
                status=CompileStatus.PREPARED,
                plan_commit_certainty=PlanCommitCertainty.ABSENT,
                source_document_sha256=SHA_A,
                source_sha256=SHA_B,
                harness_id="millforge.test.builder.compiler.v1",
                diagnostics_path=unsafe_path,
            )


def test_compile_result_truncates_over_limit_diagnostics() -> None:
    result = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.FAILED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        failure_phase=CompilerPhase.REQUEST,
        diagnostics=tuple(_diagnostic(index) for index in range(129)),
    )

    assert len(result.diagnostics) == 128
    assert result.diagnostics[-1].code == "MF-D001"
    assert len(result.model_dump_json().encode("utf-8")) <= 256 * 1024


def test_compile_result_truncates_over_size_bound_diagnostics() -> None:
    result = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.FAILED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        failure_phase=CompilerPhase.REQUEST,
        diagnostics=tuple(_diagnostic(index, field_count=16) for index in range(30)),
    )

    assert result.diagnostics[-1].code == "MF-D001"
    assert len(result.model_dump_json().encode("utf-8")) <= 256 * 1024


def test_compile_request_rejects_invalid_terminal_results_and_paths() -> None:
    with pytest.raises(ValidationError):
        HarnessCompileRequest(
            **{
                **_request().model_dump(),
                "legal_terminal_results": ("BLOCKED", "BLOCKED"),
            }
        )

    absolute_request = HarnessCompileRequest(
        **{
            **_request().model_dump(),
            "source_path": "/absolute.yaml",
            "output_dir": "/absolute/out",
        }
    )
    assert absolute_request.source_path == "/absolute.yaml"
    assert absolute_request.output_dir == "/absolute/out"

    with pytest.raises(ValidationError):
        HarnessCompileRequest(
            **{
                **_request().model_dump(),
                "source_path": "",
            }
        )


def test_compile_result_invariants_for_failed_and_committed_outcomes() -> None:
    failed = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.FAILED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        failure_phase=CompilerPhase.REQUEST,
    )
    assert failed.compiled_sha256 is None

    with pytest.raises(ValidationError):
        HarnessCompileResult(
            request_id="request.c48f9299",
            status=CompileStatus.FAILED,
            plan_commit_certainty=PlanCommitCertainty.ABSENT,
            compiled_sha256=SHA_A,
        )

    committed = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.COMMITTED,
        plan_commit_certainty=PlanCommitCertainty.COMMITTED,
        source_document_sha256=SHA_A,
        source_sha256=SHA_B,
        harness_id="millforge.test.builder.compiler.v1",
        compiled_plan_path="compiled/plan.json",
        compiled_sha256=SHA_C,
    )
    assert committed.harness_id == "millforge.test.builder.compiler.v1"


def test_compile_result_phase_certainty_matrix_without_lowering_or_publication() -> (
    None
):
    pre_read = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.FAILED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        failure_phase=CompilerPhase.REQUEST,
    )
    assert pre_read.source_document_sha256 is None

    post_read = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.FAILED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        failure_phase=CompilerPhase.PARSE,
        source_document_sha256=SHA_A,
    )
    assert post_read.source_document_sha256 == SHA_A

    post_schema = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.FAILED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        failure_phase=CompilerPhase.SCHEMA,
        source_document_sha256=SHA_A,
    )
    assert post_schema.compiled_plan_path is None

    prepared = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.PREPARED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        source_document_sha256=SHA_A,
        source_sha256=SHA_B,
        harness_id="millforge.test.builder.compiler.v1",
    )
    assert prepared.compiled_sha256 is None

    with pytest.raises(ValidationError):
        HarnessCompileResult(
            request_id="request.c48f9299",
            status=CompileStatus.FAILED,
            plan_commit_certainty=PlanCommitCertainty.ABSENT,
            failure_phase=CompilerPhase.REQUEST,
            source_document_sha256=SHA_A,
        )

    with pytest.raises(ValidationError):
        HarnessCompileResult(
            request_id="request.c48f9299",
            status=CompileStatus.FAILED,
            plan_commit_certainty=PlanCommitCertainty.ABSENT,
            failure_phase=CompilerPhase.PARSE,
            source_document_sha256=SHA_A,
            source_sha256=SHA_B,
        )

    with pytest.raises(ValidationError):
        HarnessCompileResult(
            request_id="request.c48f9299",
            status=CompileStatus.PREPARED,
            plan_commit_certainty=PlanCommitCertainty.ABSENT,
            source_document_sha256=SHA_A,
        )


def test_unknown_certainty_is_limited_to_output_failure_after_publication_risk() -> (
    None
):
    result = HarnessCompileResult(
        request_id="request.c48f9299",
        status=CompileStatus.FAILED,
        plan_commit_certainty=PlanCommitCertainty.UNKNOWN,
        failure_phase=CompilerPhase.OUTPUT,
        compiled_plan_path="compiled/plan.json",
        compiled_sha256=SHA_A,
    )
    assert result.plan_commit_certainty == PlanCommitCertainty.UNKNOWN

    with pytest.raises(ValidationError):
        HarnessCompileResult(
            request_id="request.c48f9299",
            status=CompileStatus.FAILED,
            plan_commit_certainty=PlanCommitCertainty.UNKNOWN,
            failure_phase=CompilerPhase.PARSE,
            compiled_plan_path="compiled/plan.json",
            compiled_sha256=SHA_A,
        )


def test_raw_request_admission_accepts_valid_request_after_source_parse(
    tmp_path: Path,
) -> None:
    admitted = DefaultHarnessCompileRequestAdmission().admit(_raw_request(tmp_path))

    assert admitted.request is not None
    assert admitted.result is None
    assert admitted.request.source_path == "harness.json"
    assert (tmp_path / "output" / "compiled").is_dir()


def test_raw_request_admission_fails_malformed_request_without_exception(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    raw["request_id"] = 42

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    assert admitted.result.request_id == "request.invalid"
    assert admitted.result.failure_phase == CompilerPhase.REQUEST
    diagnostic = admitted.result.diagnostics[0]
    assert diagnostic.code == "MF-S018"
    assert diagnostic.message == "Compile request schema validation failed."
    assert diagnostic.fields[0].key == "field_path"
    assert diagnostic.fields[0].value == "/request_id"

    serialized = admitted.result.model_dump_json()
    rendered = str(admitted.result)
    assert "ValidationError" not in serialized
    assert '"input"' not in serialized
    assert '"ctx"' not in serialized
    assert '"url"' not in serialized
    assert "42" not in serialized
    assert "ValidationError" not in rendered
    assert "42" not in rendered


def test_request_source_format_schema_failure_does_not_open_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_request(tmp_path)
    raw["source_format"] = "toml"

    def fail_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("source should not be opened after request failure")

    monkeypatch.setattr(Path, "open", fail_open)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    assert admitted.result.failure_phase == CompilerPhase.REQUEST
    assert admitted.result.source_document_sha256 is None
    assert admitted.result.source_sha256 is None
    assert admitted.result.harness_id is None
    diagnostic = admitted.result.diagnostics[0]
    assert diagnostic.code == "MF-S018"
    assert diagnostic.fields[0].key == "field_path"
    assert diagnostic.fields[0].value == "/source_format"


def test_source_is_not_opened_before_root_and_output_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_request(tmp_path)
    raw["output_dir"] = "missing"

    def fail_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("source should not be opened after output failure")

    monkeypatch.setattr(Path, "open", fail_open)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == "MF-S017"


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("source_path", "../harness.json", "MF-S001"),
        ("source_path", "/tmp/harness.json", "MF-S001"),
        ("output_dir", "../compiled", "MF-S002"),
        ("output_dir", "/tmp/compiled", "MF-S002"),
    ],
)
def test_absolute_and_traversal_paths_use_canonical_outside_root_codes(
    tmp_path: Path,
    field: str,
    value: str,
    expected_code: str,
) -> None:
    raw = _raw_request(tmp_path)
    raw[field] = value

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == expected_code


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("source_path", "CON", "MF-S001"),
        ("source_path", "nested/NUL.json", "MF-S001"),
        ("source_path", "harness.json:stream", "MF-S001"),
        ("source_path", "nested\\harness.json", "MF-S001"),
        ("source_path", "trailing.json.", "MF-S001"),
        ("source_path", "trailing.json ", "MF-S001"),
        ("output_dir", "AUX", "MF-S002"),
        ("output_dir", "compiled:LPT1", "MF-S002"),
        ("output_dir", "nested\\compiled", "MF-S002"),
        ("output_dir", "compiled.", "MF-S002"),
        ("output_dir", "compiled ", "MF-S002"),
    ],
)
def test_device_ads_and_backslash_paths_use_canonical_outside_root_codes(
    tmp_path: Path,
    field: str,
    value: str,
    expected_code: str,
) -> None:
    raw = _raw_request(tmp_path)
    raw[field] = value

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == expected_code


@pytest.mark.parametrize(
    "legal_terminal_results",
    [
        (),
        ("builder_complete",),
        ("BUILDER_COMPLETE", "BUILDER_COMPLETE"),
    ],
)
def test_raw_request_admission_rejects_empty_malformed_and_duplicate_terminals(
    tmp_path: Path,
    legal_terminal_results: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_request(tmp_path)
    raw["legal_terminal_results"] = legal_terminal_results

    def fail_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("source should not be opened after request failure")

    monkeypatch.setattr(Path, "open", fail_open)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    assert admitted.result.failure_phase == CompilerPhase.REQUEST
    assert admitted.result.diagnostics[0].code == "MF-S018"
    field_path = admitted.result.diagnostics[0].fields[0].value
    assert isinstance(field_path, str)
    assert field_path.startswith("/legal_terminal_results")


def test_source_outside_root_precedes_output_root_failure_without_opening_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_request(tmp_path)
    raw["source_path"] = "../harness.json"
    raw["output_root"] = str(tmp_path / "missing-output-root")

    def fail_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("source should not be opened after output failure")

    monkeypatch.setattr(Path, "open", fail_open)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.result is not None
    assert [diagnostic.code for diagnostic in admitted.result.diagnostics] == [
        "MF-S001",
        "MF-S016",
    ]


def test_source_root_containment_rejects_traversal_and_outside_symlink(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    raw["source_path"] = "../harness.json"
    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)
    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == "MF-S001"

    raw = _raw_request(tmp_path / "symlink")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "harness.json").write_text(
        json.dumps(_source_payload()), encoding="utf-8"
    )
    (Path(str(raw["source_root"])) / "outside.json").symlink_to(
        outside / "harness.json"
    )
    raw["source_path"] = "outside.json"

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == "MF-S001"


def test_source_admission_rejects_in_root_final_symlink_even_when_target_is_regular(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_request(tmp_path)
    source_root = Path(str(raw["source_root"]))
    target = source_root / "target.json"
    target.write_text(json.dumps(_source_payload()), encoding="utf-8")
    (source_root / "link.json").symlink_to(target)
    raw["source_path"] = "link.json"

    def fail_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("source should not be opened for final symlinks")

    monkeypatch.setattr(Path, "open", fail_open)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    assert admitted.result.failure_phase == CompilerPhase.REQUEST
    assert admitted.result.diagnostics[0].code == "MF-S014"


def test_source_admission_rejects_missing_and_non_regular_source_paths(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    Path(str(raw["source_root"]), "harness.json").unlink()
    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)
    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == "MF-S013"

    if hasattr(os, "mkfifo"):
        raw = _raw_request(tmp_path / "fifo")
        source_path = Path(str(raw["source_root"]), "harness.json")
        source_path.unlink()
        os.mkfifo(source_path)

        admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

        assert admitted.result is not None
        assert admitted.result.diagnostics[0].code == "MF-S014"


def test_source_replacement_is_reported_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_request(tmp_path)
    source_path = Path(str(raw["source_root"]), "harness.json").resolve()
    original_open = Path.open
    replaced = False

    def replacing_open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        nonlocal replaced
        if self == source_path and not replaced:
            replaced = True
            source_path.unlink()
            source_path.write_text(json.dumps(_source_payload()), encoding="utf-8")
        return original_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", replacing_open)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == "MF-S015"


def test_output_root_containment_and_conflicts_are_reported_without_creating_dirs(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    raw["output_dir"] = "existing"
    Path(str(raw["output_root"]), "existing").mkdir()
    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)
    assert admitted.request is not None
    assert admitted.result is None

    raw = _raw_request(tmp_path / "non_dir")
    output_dir = Path(str(raw["output_root"]), "not-a-directory")
    output_dir.write_text("not a directory", encoding="utf-8")
    raw["output_dir"] = "not-a-directory"
    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)
    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == "MF-S017"

    raw = _raw_request(tmp_path / "missing_parent")
    raw["output_dir"] = "missing/compiled"
    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)
    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == "MF-S017"
    assert not Path(str(raw["output_root"]), "missing").exists()

    raw = _raw_request(tmp_path / "outside_output")
    outside = tmp_path / "outside-output"
    outside.mkdir()
    Path(str(raw["output_root"]), "link").symlink_to(outside, target_is_directory=True)
    raw["output_dir"] = "link/compiled"

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == "MF-S002"


def test_parse_failures_return_failed_compile_results_with_source_hash(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    source_path = Path(str(raw["source_root"]), "harness.json")
    source_path.write_text("{not valid json", encoding="utf-8")

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    assert admitted.result.failure_phase == CompilerPhase.PARSE
    assert admitted.result.source_document_sha256 is not None
    assert admitted.result.source_sha256 is None
    assert admitted.result.harness_id is None
    assert admitted.result.diagnostics[0].code == "MF-S011"


def test_post_parse_admission_rejects_expected_harness_mismatch(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    raw["expected_harness_id"] = "millforge.test.other.compiler.v1"

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    assert admitted.result.failure_phase == CompilerPhase.SCHEMA
    assert admitted.result.source_document_sha256 is not None
    assert admitted.result.source_sha256 is not None
    assert admitted.result.harness_id == "millforge.test.builder.compiler.v1"
    diagnostic = admitted.result.diagnostics[0]
    assert diagnostic.code == "MF-S027"
    assert diagnostic.source_reference is not None
    assert diagnostic.source_reference.field_path == "/harness_id"
    assert diagnostic.fields[0].value == "/harness_id"


def test_post_parse_admission_rejects_stage_scope_mismatch(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    raw["stage_kind_id"] = "checker"

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    assert admitted.result.failure_phase == CompilerPhase.SCHEMA
    assert admitted.result.source_document_sha256 is not None
    assert admitted.result.source_sha256 is not None
    assert admitted.result.harness_id == "millforge.test.builder.compiler.v1"
    diagnostic = admitted.result.diagnostics[0]
    assert diagnostic.code == "MF-S028"
    assert diagnostic.source_reference is not None
    assert diagnostic.source_reference.field_path == "/stage_scope/stage_kind_ids"


def test_post_parse_admission_rejects_legal_terminal_parity_gaps(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    raw["legal_terminal_results"] = ("BUILDER_COMPLETE",)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    diagnostic = admitted.result.diagnostics[0]
    assert diagnostic.code == "MF-S029"
    assert diagnostic.source_reference is not None
    assert diagnostic.source_reference.field_path.endswith("/terminal_result")


def test_post_parse_admission_rejects_duplicate_source_terminal_result(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    payload = _source_payload()
    graph = payload["graph"]
    assert isinstance(graph, dict)
    nodes = graph["nodes"]
    assert isinstance(nodes, dict)
    del nodes["submit_blocked"]
    nodes["duplicate_submit"] = {
        "tool_ref": "builtin.terminal.submit@1",
        "terminal_result": "BUILDER_COMPLETE",
        "prerequisites": [{"node_id": "read_file"}],
    }
    _write_source_payload(raw, payload)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    diagnostic = admitted.result.diagnostics[0]
    assert diagnostic.code == "MF-S029"
    assert diagnostic.source_reference is not None
    assert diagnostic.source_reference.field_path == "/graph/nodes/2/terminal_result"
    assert diagnostic.fields[0].value == "/graph/nodes/2/terminal_result"


def test_post_parse_admission_rejects_unused_source_terminal_mapping(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    payload = _source_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)
    artifacts["required_by_terminal"] = {"NEEDS_ARTIFACTS": ["patch_summary"]}
    _write_source_payload(raw, payload)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    diagnostic = admitted.result.diagnostics[0]
    assert diagnostic.code == "MF-S029"
    assert diagnostic.source_reference is not None
    assert (
        diagnostic.source_reference.field_path
        == "/artifacts/required_by_terminal/0/terminal_result"
    )


def test_post_parse_admission_reports_source_scalar_secret_without_leak(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    secret_value = "Bearer " + "abcdefghijklmnopqrstuvwxyz"
    payload = _source_payload()
    prompt = payload["prompt"]
    assert isinstance(prompt, dict)
    prompt["system_instructions"] = secret_value
    _write_source_payload(raw, payload)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    diagnostic = admitted.result.diagnostics[0]
    assert diagnostic.code == "MF-S026"
    assert diagnostic.source_reference is not None
    assert diagnostic.source_reference.field_path == "/prompt/system_instructions"
    assert diagnostic.fields[0].value == "/prompt/system_instructions"
    serialized = admitted.result.model_dump_json()
    rendered = str(admitted.result)
    assert secret_value not in serialized
    assert secret_value not in rendered


def test_post_parse_checks_do_not_mask_source_schema_failures(
    tmp_path: Path,
) -> None:
    raw = _raw_request(tmp_path)
    raw["expected_harness_id"] = "millforge.test.other.compiler.v1"
    payload = _source_payload()
    payload["unexpected"] = "field"
    _write_source_payload(raw, payload)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    assert admitted.result.diagnostics[0].code == "MF-S021"


@pytest.mark.parametrize(
    ("mutator", "expected_code", "expected_path"),
    [
        (
            lambda payload: payload["prompt"].__setitem__("unknown", "field"),
            "MF-S021",
            "/prompt/unknown",
        ),
        (
            lambda payload: payload.__setitem__("harness_id", "Millforge"),
            "MF-S022",
            "/harness_id",
        ),
        (
            lambda payload: payload["graph"]["nodes"]["read_file"].__setitem__(
                "tool_ref", "builtin.workspace.read_file"
            ),
            "MF-S023",
            "/graph/nodes/0/tool_ref",
        ),
        (
            lambda payload: payload["budgets"].__setitem__("max_iterations", 0),
            "MF-S024",
            "/budgets/max_iterations",
        ),
        (
            lambda payload: payload["context"].__setitem__("budget_tokens", 255),
            "MF-S025",
            "/context/budget_tokens",
        ),
    ],
)
def test_raw_request_admission_preserves_exact_source_schema_codes(
    tmp_path: Path,
    mutator: object,
    expected_code: str,
    expected_path: str,
) -> None:
    raw = _raw_request(tmp_path)
    payload = _source_payload()
    assert callable(mutator)
    mutator(payload)
    _write_source_payload(raw, payload)

    admitted = DefaultHarnessCompileRequestAdmission().admit(raw)

    assert admitted.request is None
    assert admitted.result is not None
    assert admitted.result.failure_phase == CompilerPhase.SCHEMA
    diagnostic = admitted.result.diagnostics[0]
    assert diagnostic.code == expected_code
    assert diagnostic.source_reference is not None
    assert diagnostic.source_reference.field_path == expected_path
    assert diagnostic.fields[0].value == expected_path
