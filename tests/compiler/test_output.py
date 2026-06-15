"""Tests for compiler output publication."""

from __future__ import annotations

import hashlib
import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from millforge.compiled_plan import canonical_compiled_plan_bytes
from millforge.compiler import (
    CompileStatus,
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticField,
    DiagnosticReportState,
    DiagnosticSeverity,
    HarnessCompileRequest,
    HarnessCompileResult,
    PlanCommitCertainty,
    compiled_plan_output_path,
    diagnostics_output_path,
    persist_compile_outputs,
    request_identity_sha256,
)
import millforge.compiler.output as compiler_output
from millforge.contracts import CapabilityEnvelope
from tests.conftest import make_canonical_builder_compiled_plan

_SECRET_SENTINEL = "super-secret-output-token"


@dataclass(frozen=True)
class _OutputSentinels:
    existing_output: Path
    outside_output_root: Path


def _request(tmp_path: Path, *, output_dir: str = "compiled") -> HarnessCompileRequest:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    source_root.mkdir()
    output_root.mkdir()
    (output_root / "compiled").mkdir()
    return HarnessCompileRequest(
        request_id="request.output.v1",
        source_path="harness.yaml",
        source_root=str(source_root),
        source_format="yaml",
        output_dir=output_dir,
        output_root=str(output_root),
        expected_harness_id="builder.runtime_slice.v1",
        stage_kind_id="builder",
        legal_terminal_results=("BUILDER_COMPLETE", "BLOCKED"),
        capability_envelope=CapabilityEnvelope(grants=()),
    )


def _compiled_diagnostics_path(request: HarnessCompileRequest, plan: Any) -> str:
    return diagnostics_output_path(
        request,
        harness_id=plan.harness_id,
        harness_version=plan.harness_version,
        compiled_sha256=plan.compiled_sha256,
    )


def _assert_output_failure_row(
    result: HarnessCompileResult,
    request: HarnessCompileRequest,
    *,
    certainty: PlanCommitCertainty,
    diagnostics_state: DiagnosticReportState,
    diagnostic_codes: tuple[str, ...],
    plan_path_state: str,
    diagnostics_path_state: str,
    temp_file_state: str = "absent",
    sentinels: _OutputSentinels | None = None,
) -> None:
    assert result.status == CompileStatus.FAILED
    assert result.failure_phase == CompilerPhase.OUTPUT
    assert result.plan_commit_certainty == certainty
    assert result.diagnostic_report_state == diagnostics_state
    assert [diagnostic.code for diagnostic in result.diagnostics] == list(
        diagnostic_codes
    )
    assert _path_state(request, result.compiled_plan_path) == plan_path_state
    assert _path_state(request, result.diagnostics_path) == diagnostics_path_state
    assert _paths_stay_under_output_root(request, result)
    assert _diagnostics_are_complete_and_redacted(result, request)
    temp_files = list(Path(request.output_root, request.output_dir).glob("*.tmp"))
    assert ("present" if temp_files else "absent") == temp_file_state
    if sentinels is not None:
        assert sentinels.existing_output.read_text(encoding="utf-8") == (
            "existing valid output\n"
        )
        assert sentinels.outside_output_root.read_text(encoding="utf-8") == (
            "outside unchanged\n"
        )


def _path_state(request: HarnessCompileRequest, relpath: str | None) -> str:
    if relpath is None:
        return "absent"
    path = Path(request.output_root, relpath)
    return "present" if path.exists() else "missing"


def _paths_stay_under_output_root(
    request: HarnessCompileRequest, result: HarnessCompileResult
) -> bool:
    root = Path(request.output_root).resolve()
    for relpath in (result.compiled_plan_path, result.diagnostics_path):
        if relpath is None:
            continue
        resolved = Path(request.output_root, relpath).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return False
    return True


def _diagnostics_are_complete_and_redacted(
    result: HarnessCompileResult, request: HarnessCompileRequest
) -> bool:
    assert result.request_id == request.request_id
    assert result.request_identity_sha256 == request_identity_sha256(request)
    assert result.diagnostics
    for diagnostic in result.diagnostics:
        assert diagnostic.phase == CompilerPhase.OUTPUT
        assert diagnostic.severity == DiagnosticSeverity.ERROR
        assert diagnostic.message
    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    assert _SECRET_SENTINEL not in serialized
    assert str(request.output_root) not in serialized
    assert str(request.source_root) not in serialized
    return True


def _prepare_output_sentinels(
    request: HarnessCompileRequest, tmp_path: Path
) -> _OutputSentinels:
    existing_output = Path(
        request.output_root, request.output_dir, "existing-valid-output.txt"
    )
    existing_output.write_text("existing valid output\n", encoding="utf-8")
    outside_output_root = tmp_path / "outside-output-root.txt"
    outside_output_root.write_text("outside unchanged\n", encoding="utf-8")
    return _OutputSentinels(
        existing_output=existing_output,
        outside_output_root=outside_output_root,
    )


def test_request_identity_hashes_only_the_canonical_identity_fields(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    changed = request.model_copy(
        update={
            "output_dir": "other",
            "output_root": "/tmp/other-root",
            "source_root": "/tmp/other-source",
            "legal_terminal_results": ("BLOCKED", "BUILDER_COMPLETE"),
        }
    )
    payload = {
        "request_id": request.request_id,
        "source_path": request.source_path,
        "source_format": request.source_format,
        "expected_harness_id": request.expected_harness_id,
        "stage_kind_id": request.stage_kind_id,
    }
    expected = hashlib.sha256(
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()

    assert request_identity_sha256(request) == expected
    assert request_identity_sha256(changed) == expected
    assert (
        request_identity_sha256(request.model_copy(update={"stage_kind_id": "checker"}))
        != expected
    )


def test_output_paths_are_under_output_dir_and_distinguish_addressing(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()

    plan_path = compiled_plan_output_path(request, plan)
    diagnostics_path = diagnostics_output_path(request)

    assert plan_path.startswith("compiled/")
    assert diagnostics_path.startswith("compiled/")
    assert plan.compiled_sha256 in plan_path
    assert request_identity_sha256(request) in diagnostics_path
    assert plan_path.endswith(
        f"@{plan.harness_version}.{plan.compiled_sha256}.compiled.json"
    )
    assert ".compiled-" not in plan_path
    assert Path(diagnostics_path).name.startswith("request-")
    assert "\\" not in plan_path + diagnostics_path


def test_harness_identity_filename_encoding_is_reversible_and_collision_resistant(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    escaped_plan = make_canonical_builder_compiled_plan().model_copy(
        update={"harness_id": "builder/runtime slice@v1"}
    )
    literal_plan = escaped_plan.model_copy(
        update={"harness_id": "builder%2Fruntime%20slice%40v1"}
    )

    escaped_path = compiled_plan_output_path(request, escaped_plan)
    literal_path = compiled_plan_output_path(request, literal_plan)

    assert "builder%2Fruntime%20slice%40v1" in escaped_path
    assert "builder%252Fruntime%2520slice%2540v1" in literal_path
    assert escaped_path != literal_path
    assert "/" not in Path(escaped_path).name
    assert "\\" not in escaped_path + literal_path


def test_diagnostics_paths_support_request_source_and_compiled_digest_forms(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    request_hash = request_identity_sha256(request)
    source_document_sha256 = "a" * 64

    assert diagnostics_output_path(request) == (
        f"compiled/request-{request_hash}.diagnostics.json"
    )
    assert diagnostics_output_path(
        request,
        harness_id=plan.harness_id,
        harness_version=plan.harness_version,
        source_document_sha256=source_document_sha256,
    ) == (
        f"compiled/{plan.harness_id}@{plan.harness_version}."
        f"{source_document_sha256}.request-{request_hash}.diagnostics.json"
    )
    assert diagnostics_output_path(
        request,
        harness_id=plan.harness_id,
        harness_version=plan.harness_version,
        compiled_sha256=plan.compiled_sha256,
    ) == (
        f"compiled/{plan.harness_id}@{plan.harness_version}."
        f"{plan.compiled_sha256}.request-{request_hash}.diagnostics.json"
    )


def test_output_directory_replacement_with_symlink_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    output_dir = Path(request.output_root, request.output_dir)
    backup_dir = output_dir.with_name(f"{output_dir.name}.backup")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    original_admit = compiler_output._admit_output_dir

    def swap_admitted_output_dir(
        request: HarnessCompileRequest, *, request_hash: str | None = None
    ) -> HarnessCompileResult | object:
        admitted = original_admit(request, request_hash=request_hash)
        if isinstance(admitted, HarnessCompileResult):
            return admitted
        output_dir.rename(backup_dir)
        output_dir.symlink_to(outside_dir, target_is_directory=True)
        return admitted

    monkeypatch.setattr(
        "millforge.compiler.output._admit_output_dir", swap_admitted_output_dir
    )

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.ABSENT,
        diagnostic_codes=("MF-O001",),
        plan_path_state="absent",
        diagnostics_path_state="absent",
    )
    assert list(outside_dir.iterdir()) == []
    assert list(backup_dir.iterdir()) == []


def test_persist_compile_outputs_writes_prepared_plan_and_committed_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    source_document_sha256 = "a" * 64
    diagnostic = CompilerDiagnostic(
        code="MF-D001",
        phase=CompilerPhase.INTERNAL,
        severity=DiagnosticSeverity.WARNING,
        message="Token api_key=supersecretvalue should be redacted.",
        fields=(DiagnosticField(key="credential", value="api_key=supersecretvalue"),),
    )
    reports: list[dict[str, Any]] = []
    original_write_report = compiler_output._write_diagnostics_report

    def capture_diagnostics_report(**kwargs: Any) -> tuple[DiagnosticField, ...] | None:
        reports.append(kwargs["result"].model_dump(mode="json"))
        return original_write_report(**kwargs)

    monkeypatch.setattr(
        "millforge.compiler.output._write_diagnostics_report",
        capture_diagnostics_report,
    )

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        diagnostics=(diagnostic,),
        source_document_sha256=source_document_sha256,
    )

    assert result.status == CompileStatus.COMMITTED
    assert result.plan_commit_certainty == PlanCommitCertainty.COMMITTED
    assert result.diagnostic_report_state == DiagnosticReportState.COMMITTED
    assert result.compiled_plan_path == compiled_plan_output_path(request, plan)
    assert result.diagnostics_path == _compiled_diagnostics_path(request, plan)
    assert result.compiled_plan_path is not None
    assert result.diagnostics_path is not None
    plan_path = Path(request.output_root, result.compiled_plan_path)
    diagnostics_path = Path(request.output_root, result.diagnostics_path)
    assert plan_path.read_bytes() == canonical_compiled_plan_bytes(plan)
    persisted = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    assert [report["status"] for report in reports] == ["prepared", "committed"]
    for report in reports:
        assert tuple(report) == (
            "request_id",
            "status",
            "plan_commit_certainty",
            "diagnostic_report_state",
            "failure_phase",
            "source_document_sha256",
            "source_sha256",
            "request_identity_sha256",
            "harness_id",
            "compiled_plan_path",
            "compiled_sha256",
            "diagnostics_path",
            "diagnostics",
        )
    assert reports[0]["diagnostic_report_state"] == "prepared"
    assert reports[0]["plan_commit_certainty"] == "absent"
    assert reports[0]["compiled_plan_path"] is None
    assert reports[0]["compiled_sha256"] is None
    assert reports[0]["diagnostics_path"] == result.diagnostics_path
    assert reports[1]["diagnostic_report_state"] == "committed"
    assert reports[1]["compiled_plan_path"] == result.compiled_plan_path
    assert reports[1]["compiled_sha256"] == plan.compiled_sha256
    assert persisted == reports[1]
    assert persisted["status"] == "committed"
    assert persisted["plan_commit_certainty"] == "committed"
    assert persisted["diagnostic_report_state"] == "committed"
    assert persisted["request_id"] == request.request_id
    assert persisted["request_identity_sha256"] == request_identity_sha256(request)
    assert persisted["source_document_sha256"] == source_document_sha256
    assert persisted["source_sha256"] == plan.source_sha256
    assert persisted["harness_id"] == plan.harness_id
    assert persisted["failure_phase"] is None
    assert persisted["diagnostics_path"].startswith("compiled/")
    assert persisted["compiled_plan_path"].startswith("compiled/")
    assert "\\" not in persisted["diagnostics_path"] + persisted["compiled_plan_path"]
    assert not Path(persisted["diagnostics_path"]).is_absolute()
    assert not Path(persisted["compiled_plan_path"]).is_absolute()
    assert persisted["diagnostics"][0]["message"] == (
        "Token api_key**redacted** should be redacted."
    )
    assert persisted["diagnostics"][0]["fields"] == [
        {"key": "credential", "value": "**redacted**"}
    ]
    serialized = json.dumps(persisted, sort_keys=True)
    assert "supersecretvalue" not in serialized
    assert str(tmp_path) not in serialized
    assert "http://" not in serialized
    assert "AWS_SECRET_ACCESS_KEY" not in serialized


def test_prepared_diagnostics_directory_fsync_failure_returns_output_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    sentinels = _prepare_output_sentinels(request, tmp_path)
    calls = 0

    def fail_first_fsync(_target: object) -> bool:
        nonlocal calls
        calls += 1
        return calls != 1

    monkeypatch.setattr("millforge.compiler.output._fsync_directory", fail_first_fsync)

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.ABSENT,
        diagnostic_codes=("MF-O002",),
        plan_path_state="absent",
        diagnostics_path_state="absent",
        sentinels=sentinels,
    )


def test_failure_before_prepared_diagnostics_temp_creation_returns_output_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    sentinels = _prepare_output_sentinels(request, tmp_path)
    diagnostics_name = Path(_compiled_diagnostics_path(request, plan)).name
    original_write = compiler_output._write_exclusive_temp

    def fail_before_diagnostics_temp_creation(
        directory_fd: int, filename: str, data: bytes
    ) -> None:
        if filename.startswith(f".{diagnostics_name}."):
            raise OSError(errno.EACCES, os.strerror(errno.EACCES))
        original_write(directory_fd, filename, data)

    monkeypatch.setattr(
        "millforge.compiler.output._write_exclusive_temp",
        fail_before_diagnostics_temp_creation,
    )

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.ABSENT,
        diagnostic_codes=("MF-O002",),
        plan_path_state="absent",
        diagnostics_path_state="absent",
        sentinels=sentinels,
    )


def test_failure_during_prepared_diagnostics_write_returns_output_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    sentinels = _prepare_output_sentinels(request, tmp_path)
    diagnostics_name = Path(_compiled_diagnostics_path(request, plan)).name
    original_write = compiler_output._write_exclusive_temp

    def fail_during_diagnostics_write(
        directory_fd: int, filename: str, data: bytes
    ) -> None:
        if filename.startswith(f".{diagnostics_name}."):
            original_write(directory_fd, filename, data[:1])
            raise OSError(errno.EIO, f"{os.strerror(errno.EIO)} {_SECRET_SENTINEL}")
        original_write(directory_fd, filename, data)

    monkeypatch.setattr(
        "millforge.compiler.output._write_exclusive_temp",
        fail_during_diagnostics_write,
    )

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.ABSENT,
        diagnostic_codes=("MF-O002",),
        plan_path_state="absent",
        diagnostics_path_state="absent",
        sentinels=sentinels,
    )


def test_compiled_plan_size_bound_preserves_prepared_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    sentinels = _prepare_output_sentinels(request, tmp_path)
    monkeypatch.setattr("millforge.compiler.output.MAX_COMPILED_PLAN_OUTPUT_BYTES", 1)

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.PREPARED,
        diagnostic_codes=("MF-O003",),
        plan_path_state="absent",
        diagnostics_path_state="present",
        sentinels=sentinels,
    )
    assert result.diagnostics[0].fields[0].key == "compiled_plan_size"


def test_plan_temporary_write_failure_preserves_prepared_diagnostics_and_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    sentinels = _prepare_output_sentinels(request, tmp_path)
    original_write = compiler_output._write_exclusive_temp

    def fail_plan_temp(directory_fd: int, filename: str, data: bytes) -> None:
        if filename.endswith(".compiled.json") or ".compiled.json." in filename:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        original_write(directory_fd, filename, data)

    monkeypatch.setattr(
        "millforge.compiler.output._write_exclusive_temp", fail_plan_temp
    )

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.PREPARED,
        diagnostic_codes=("MF-O003",),
        plan_path_state="absent",
        diagnostics_path_state="present",
        sentinels=sentinels,
    )
    assert result.diagnostics_path == _compiled_diagnostics_path(request, plan)


def test_plan_write_failure_after_fsync_before_publication_cleans_temp_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    sentinels = _prepare_output_sentinels(request, tmp_path)
    original_write = compiler_output._write_exclusive_temp

    def fail_after_plan_temp_fsync(
        directory_fd: int, filename: str, data: bytes
    ) -> None:
        if ".compiled.json." in filename:
            original_write(directory_fd, filename, data)
            raise OSError(errno.EIO, f"{os.strerror(errno.EIO)} {_SECRET_SENTINEL}")
        original_write(directory_fd, filename, data)

    monkeypatch.setattr(
        "millforge.compiler.output._write_exclusive_temp",
        fail_after_plan_temp_fsync,
    )

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.PREPARED,
        diagnostic_codes=("MF-O003",),
        plan_path_state="absent",
        diagnostics_path_state="present",
        sentinels=sentinels,
    )
    assert result.diagnostics_path == _compiled_diagnostics_path(request, plan)


def test_plan_publication_failure_preserves_prepared_diagnostics_and_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    sentinels = _prepare_output_sentinels(request, tmp_path)
    original_link = os.link

    def fail_no_clobber_publication(
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if str(dst).endswith(".compiled.json"):
            raise OSError(errno.EIO, f"{os.strerror(errno.EIO)} {_SECRET_SENTINEL}")
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", fail_no_clobber_publication)
    monkeypatch.setattr(
        os,
        "supports_dir_fd",
        {*os.supports_dir_fd, fail_no_clobber_publication},
    )

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.PREPARED,
        diagnostic_codes=("MF-O003",),
        plan_path_state="absent",
        diagnostics_path_state="present",
        sentinels=sentinels,
    )
    assert result.diagnostics_path == _compiled_diagnostics_path(request, plan)


def test_plan_write_failure_with_cleanup_failure_reports_both_codes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    sentinels = _prepare_output_sentinels(request, tmp_path)

    def fail_publication(**_kwargs: object) -> compiler_output._PublishOutcome:
        return compiler_output._PublishOutcome(
            error=(compiler_output._field("errno", errno.EIO),),
            cleanup_error=(compiler_output._field("errno", errno.EIO),),
        )

    monkeypatch.setattr(
        "millforge.compiler.output._publish_content_addressed_file", fail_publication
    )

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.PREPARED,
        diagnostic_codes=("MF-O003", "MF-O005"),
        plan_path_state="absent",
        diagnostics_path_state="present",
        sentinels=sentinels,
    )


def test_plan_cleanup_failure_after_reuse_reports_unknown_certainty_and_preserves_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    first = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )
    assert first.status == CompileStatus.COMMITTED
    assert first.compiled_plan_path == compiled_plan_output_path(request, plan)
    assert first.diagnostics_path == _compiled_diagnostics_path(request, plan)
    other_request = request.model_copy(update={"request_id": "request.output.v2"})

    def fail_cleanup(**_kwargs: object):
        return (compiler_output._field("errno", errno.EIO),)

    monkeypatch.setattr("millforge.compiler.output._unlink_if_present", fail_cleanup)

    result = persist_compile_outputs(
        request=other_request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        other_request,
        certainty=PlanCommitCertainty.UNKNOWN,
        diagnostics_state=DiagnosticReportState.PREPARED,
        diagnostic_codes=("MF-O005",),
        plan_path_state="present",
        diagnostics_path_state="present",
        temp_file_state="present",
    )
    assert result.compiled_plan_path == compiled_plan_output_path(other_request, plan)
    assert result.compiled_sha256 == plan.compiled_sha256
    assert result.diagnostics_path == _compiled_diagnostics_path(other_request, plan)
    output_dir = Path(request.output_root, request.output_dir)
    assert Path(request.output_root, result.compiled_plan_path).read_bytes() == (
        canonical_compiled_plan_bytes(plan)
    )
    assert len(list(output_dir.glob("*.compiled.json"))) == 1
    assert len(list(output_dir.glob("*.diagnostics.json"))) == 2
    assert first.compiled_plan_path == result.compiled_plan_path
    assert first.diagnostics_path != result.diagnostics_path


def test_content_addressed_plan_publication_reuses_identical_existing_bytes(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    first = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )
    assert first.status == CompileStatus.COMMITTED

    second = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    assert second.status == CompileStatus.COMMITTED
    assert list(Path(request.output_root, request.output_dir).glob("*.tmp")) == []


def test_content_addressed_plan_publication_fails_closed_on_conflicting_bytes(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    destination = Path(request.output_root, compiled_plan_output_path(request, plan))
    destination.write_bytes(b"different bytes")

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.PREPARED,
        diagnostic_codes=("MF-O004",),
        plan_path_state="absent",
        diagnostics_path_state="present",
    )
    assert destination.read_bytes() == b"different bytes"


def test_output_admission_failure_returns_in_memory_diagnostics_without_writes(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, output_dir="missing/compiled")
    plan = make_canonical_builder_compiled_plan()

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.ABSENT,
        diagnostics_state=DiagnosticReportState.ABSENT,
        diagnostic_codes=("MF-O001",),
        plan_path_state="absent",
        diagnostics_path_state="absent",
    )
    assert not Path(request.output_root, "missing").exists()


def test_plan_durability_unknown_reports_unknown_certainty_and_committed_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    calls = 0

    def fail_plan_directory(_target: object) -> bool:
        nonlocal calls
        calls += 1
        return calls != 2

    monkeypatch.setattr(
        "millforge.compiler.output._fsync_directory", fail_plan_directory
    )

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.UNKNOWN,
        diagnostics_state=DiagnosticReportState.COMMITTED,
        diagnostic_codes=("MF-O003",),
        plan_path_state="present",
        diagnostics_path_state="present",
    )
    assert result.compiled_plan_path == compiled_plan_output_path(request, plan)
    assert result.compiled_sha256 == plan.compiled_sha256


def test_plan_destination_inspection_failure_reports_unknown_certainty_after_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    original_destination_matches = compiler_output._destination_matches
    calls = 0

    def fail_post_publish_inspection(
        *,
        output_dir: Any,
        destination_name: str,
        data: bytes,
    ) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        return original_destination_matches(
            output_dir=output_dir,
            destination_name=destination_name,
            data=data,
        )

    monkeypatch.setattr(
        "millforge.compiler.output._destination_matches",
        fail_post_publish_inspection,
    )

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    _assert_output_failure_row(
        result,
        request,
        certainty=PlanCommitCertainty.UNKNOWN,
        diagnostics_state=DiagnosticReportState.COMMITTED,
        diagnostic_codes=("MF-O003",),
        plan_path_state="present",
        diagnostics_path_state="present",
    )
    assert result.compiled_plan_path == compiled_plan_output_path(request, plan)
    assert Path(request.output_root, result.compiled_plan_path).read_bytes() == (
        canonical_compiled_plan_bytes(plan)
    )


def test_committed_diagnostics_failure_reports_prepared_result_with_plan_committed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    calls = 0
    original_replace = os.replace

    def fail_second_replace(
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno := 5, os.strerror(errno))
        original_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", fail_second_replace)

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    assert result.status == CompileStatus.PREPARED
    assert result.plan_commit_certainty == PlanCommitCertainty.COMMITTED
    assert result.diagnostic_report_state == DiagnosticReportState.UNKNOWN
    assert result.compiled_plan_path == compiled_plan_output_path(request, plan)
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-O002"]


def test_committed_diagnostics_directory_fsync_failure_reports_prepared_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()
    calls = 0

    def fail_third_fsync(_target: object) -> bool:
        nonlocal calls
        calls += 1
        return calls != 3

    monkeypatch.setattr("millforge.compiler.output._fsync_directory", fail_third_fsync)

    result = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    assert result.status == CompileStatus.PREPARED
    assert result.plan_commit_certainty == PlanCommitCertainty.COMMITTED
    assert result.diagnostic_report_state == DiagnosticReportState.UNKNOWN
    assert result.compiled_plan_path == compiled_plan_output_path(request, plan)
    assert result.diagnostics_path == _compiled_diagnostics_path(request, plan)
    assert Path(request.output_root, result.compiled_plan_path).read_bytes() == (
        canonical_compiled_plan_bytes(plan)
    )
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-O002"]


def test_concurrent_content_addressed_publication_reuses_identical_plan_bytes(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    plan = make_canonical_builder_compiled_plan()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: persist_compile_outputs(
                    request=request,
                    plan=plan,
                    source_document_sha256="a" * 64,
                ),
                range(8),
            )
        )

    assert {result.status for result in results} == {CompileStatus.COMMITTED}
    assert {result.plan_commit_certainty for result in results} == {
        PlanCommitCertainty.COMMITTED
    }
    assert {result.diagnostic_report_state for result in results} == {
        DiagnosticReportState.COMMITTED
    }
    assert {result.compiled_plan_path for result in results} == {
        compiled_plan_output_path(request, plan)
    }
    assert {result.diagnostics_path for result in results} == {
        _compiled_diagnostics_path(request, plan)
    }
    output_dir = Path(request.output_root, request.output_dir)
    assert Path(
        request.output_root, compiled_plan_output_path(request, plan)
    ).read_bytes() == (canonical_compiled_plan_bytes(plan))
    assert list(output_dir.glob("*.tmp")) == []
    assert len(list(output_dir.glob("*.compiled.json"))) == 1
    assert len(list(output_dir.glob("*.diagnostics.json"))) == 1


def test_identical_plan_bytes_from_different_requests_keep_distinct_diagnostics(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    other_request = request.model_copy(update={"request_id": "request.output.v2"})
    plan = make_canonical_builder_compiled_plan()

    first = persist_compile_outputs(
        request=request,
        plan=plan,
        source_document_sha256="a" * 64,
    )
    second = persist_compile_outputs(
        request=other_request,
        plan=plan,
        source_document_sha256="a" * 64,
    )

    assert first.status == CompileStatus.COMMITTED
    assert second.status == CompileStatus.COMMITTED
    assert first.compiled_plan_path == second.compiled_plan_path
    assert first.diagnostics_path != second.diagnostics_path
    assert first.diagnostics_path is not None
    assert second.diagnostics_path is not None
    output_dir = Path(request.output_root, request.output_dir)
    assert len(list(output_dir.glob("*.compiled.json"))) == 1
    assert len(list(output_dir.glob("*.diagnostics.json"))) == 2
    first_report = json.loads(
        Path(request.output_root, first.diagnostics_path).read_text(encoding="utf-8")
    )
    second_report = json.loads(
        Path(request.output_root, second.diagnostics_path).read_text(encoding="utf-8")
    )
    assert first_report["request_id"] == request.request_id
    assert second_report["request_id"] == other_request.request_id
