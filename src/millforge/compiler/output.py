"""Atomic compiler output publication helpers."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from millforge.compiled_plan import CompiledHarnessPlan, canonical_compiled_plan_bytes
from millforge.compiled_plan import canonical_json_serialize
from millforge.compiler.diagnostics import (
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticField,
    DiagnosticSeverity,
)
from millforge.compiler.requests import (
    CompileStatus,
    DiagnosticReportState,
    HarnessCompileRequest,
    HarnessCompileResult,
    PlanCommitCertainty,
)
from millforge.compiler.validators import validate_utf8_size
from millforge.compiler.validators import validate_sha256

MAX_COMPILED_PLAN_OUTPUT_BYTES = 1024 * 1024
_HARNESS_ID_FILENAME_SAFE = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)


@dataclass(frozen=True)
class _AdmittedOutputDir:
    """Exact admitted output-directory handle used for anchored writes."""

    path: Path
    fd: int


def request_identity_sha256(request: HarnessCompileRequest) -> str:
    """Hash the request identity fields that affect output addressing."""
    payload = {
        "request_id": request.request_id,
        "source_path": request.source_path,
        "source_format": request.source_format,
        "expected_harness_id": request.expected_harness_id,
        "stage_kind_id": request.stage_kind_id,
    }
    return hashlib.sha256(canonical_json_serialize(payload).encode("utf-8")).hexdigest()


def compiled_plan_output_path(
    request: HarnessCompileRequest, plan: CompiledHarnessPlan
) -> str:
    """Return the safe relative content-addressed compiled-plan output path."""
    return _join_output_path(
        request.output_dir,
        (
            f"{_safe_harness_identity(plan.harness_id)}@{plan.harness_version}."
            f"{plan.compiled_sha256}.compiled.json"
        ),
    )


def diagnostics_output_path(
    request: HarnessCompileRequest,
    *,
    harness_id: str | None = None,
    harness_version: int | None = None,
    source_document_sha256: str | None = None,
    compiled_sha256: str | None = None,
) -> str:
    """Return the safe relative request-addressed diagnostics output path."""
    request_hash = request_identity_sha256(request)
    if compiled_sha256 is not None:
        digest = validate_sha256(compiled_sha256, "compiled_sha256")
        prefix = _harness_identity_prefix(
            harness_id=harness_id,
            harness_version=harness_version,
        )
        filename = f"{prefix}.{digest}.request-{request_hash}.diagnostics.json"
    elif source_document_sha256 is not None:
        digest = validate_sha256(source_document_sha256, "source_document_sha256")
        prefix = _harness_identity_prefix(
            harness_id=harness_id,
            harness_version=harness_version,
        )
        filename = f"{prefix}.{digest}.request-{request_hash}.diagnostics.json"
    else:
        filename = f"request-{request_hash}.diagnostics.json"
    return _join_output_path(
        request.output_dir,
        filename,
    )


def persist_compile_outputs(
    *,
    request: HarnessCompileRequest,
    plan: CompiledHarnessPlan,
    diagnostics: tuple[CompilerDiagnostic, ...] = (),
    source_document_sha256: str | None = None,
) -> HarnessCompileResult:
    """Persist prepared diagnostics, publish a compiled plan, then commit diagnostics."""
    request_hash = request_identity_sha256(request)
    output_dir = _admit_output_dir(request, request_hash=request_hash)
    if isinstance(output_dir, HarnessCompileResult):
        return output_dir

    plan_relpath = compiled_plan_output_path(request, plan)
    diagnostics_relpath = diagnostics_output_path(
        request,
        harness_id=plan.harness_id,
        harness_version=plan.harness_version,
        source_document_sha256=source_document_sha256,
        compiled_sha256=plan.compiled_sha256,
    )
    plan_name = Path(plan_relpath).name
    diagnostics_name = Path(diagnostics_relpath).name

    try:
        if not _admitted_output_dir_matches(output_dir):
            return _failed_output_result(
                request=request,
                request_hash=request_hash,
                code="MF-O001",
                message="Output directory could not be confirmed after admission.",
                source_document_sha256=source_document_sha256,
                diagnostics=diagnostics,
            )

        prepared_result = HarnessCompileResult(
            request_id=request.request_id,
            status=CompileStatus.PREPARED,
            plan_commit_certainty=PlanCommitCertainty.ABSENT,
            diagnostic_report_state=DiagnosticReportState.PREPARED,
            source_document_sha256=source_document_sha256,
            source_sha256=plan.source_sha256,
            request_identity_sha256=request_hash,
            harness_id=plan.harness_id,
            diagnostics_path=diagnostics_relpath,
            diagnostics=diagnostics,
        )
        prepared_error = _write_diagnostics_report(
            output_dir=output_dir, filename=diagnostics_name, result=prepared_result
        )
        if prepared_error is not None:
            return _failed_output_result(
                request=request,
                plan=plan,
                request_hash=request_hash,
                code="MF-O002",
                message="Prepared diagnostics report could not be written.",
                source_document_sha256=source_document_sha256,
                diagnostics=diagnostics,
                fields=prepared_error,
            )

        plan_bytes = canonical_compiled_plan_bytes(plan)
        if len(plan_bytes) > MAX_COMPILED_PLAN_OUTPUT_BYTES:
            return _failed_output_result(
                request=request,
                plan=plan,
                request_hash=request_hash,
                code="MF-O003",
                message="Compiled plan exceeds the output size limit.",
                source_document_sha256=source_document_sha256,
                diagnostics=diagnostics,
                diagnostics_path=diagnostics_relpath,
                diagnostic_report_state=DiagnosticReportState.PREPARED,
                fields=(_field("compiled_plan_size", len(plan_bytes)),),
            )

        publish = _publish_content_addressed_file(
            output_dir=output_dir, destination_name=plan_name, data=plan_bytes
        )
        if publish.result == _PublishResult.CONFLICT:
            return _failed_output_result(
                request=request,
                plan=plan,
                request_hash=request_hash,
                code="MF-O004",
                message="Compiled plan destination already contains different bytes.",
                source_document_sha256=source_document_sha256,
                diagnostics=diagnostics,
                diagnostics_path=diagnostics_relpath,
                diagnostic_report_state=DiagnosticReportState.PREPARED,
                plan_certainty=PlanCommitCertainty.ABSENT,
                cleanup_error=publish.cleanup_error,
            )
        if publish.error is not None:
            return _failed_output_result(
                request=request,
                plan=plan,
                request_hash=request_hash,
                code="MF-O003",
                message="Compiled plan temporary file could not be written.",
                source_document_sha256=source_document_sha256,
                diagnostics=diagnostics,
                diagnostics_path=diagnostics_relpath,
                diagnostic_report_state=DiagnosticReportState.PREPARED,
                fields=publish.error,
                cleanup_error=publish.cleanup_error,
            )
        if publish.cleanup_error is not None:
            return _failed_output_result(
                request=request,
                plan=plan,
                request_hash=request_hash,
                code="MF-O005",
                message="Compiled plan temporary file could not be cleaned up.",
                source_document_sha256=source_document_sha256,
                diagnostics=diagnostics,
                diagnostics_path=diagnostics_relpath,
                diagnostic_report_state=DiagnosticReportState.PREPARED,
                plan_relpath=plan_relpath,
                plan_certainty=PlanCommitCertainty.UNKNOWN,
                fields=publish.cleanup_error,
            )

        if not _destination_matches(
            output_dir=output_dir, destination_name=plan_name, data=plan_bytes
        ) or not _fsync_directory(output_dir.fd):
            durability_result = _failed_output_result(
                request=request,
                plan=plan,
                request_hash=request_hash,
                code="MF-O003",
                message="Compiled plan publication durability could not be confirmed.",
                source_document_sha256=source_document_sha256,
                diagnostics=diagnostics,
                diagnostics_path=diagnostics_relpath,
                diagnostic_report_state=DiagnosticReportState.COMMITTED,
                plan_relpath=plan_relpath,
                plan_certainty=PlanCommitCertainty.UNKNOWN,
            )
            commit_error = _write_diagnostics_report(
                output_dir=output_dir,
                filename=diagnostics_name,
                result=durability_result,
            )
            if commit_error is None:
                return durability_result
            return HarnessCompileResult(
                request_id=request.request_id,
                status=CompileStatus.FAILED,
                plan_commit_certainty=PlanCommitCertainty.UNKNOWN,
                diagnostic_report_state=DiagnosticReportState.UNKNOWN,
                failure_phase=CompilerPhase.OUTPUT,
                source_document_sha256=source_document_sha256,
                source_sha256=plan.source_sha256,
                request_identity_sha256=request_hash,
                harness_id=plan.harness_id,
                compiled_plan_path=plan_relpath,
                compiled_sha256=plan.compiled_sha256,
                diagnostics_path=diagnostics_relpath,
                diagnostics=(
                    *durability_result.diagnostics,
                    _output_diagnostic(
                        code="MF-O002",
                        message="Committed diagnostics report could not be written.",
                        fields=commit_error,
                    ),
                ),
            )

        committed_result = HarnessCompileResult(
            request_id=request.request_id,
            status=CompileStatus.COMMITTED,
            plan_commit_certainty=PlanCommitCertainty.COMMITTED,
            diagnostic_report_state=DiagnosticReportState.COMMITTED,
            source_document_sha256=source_document_sha256,
            source_sha256=plan.source_sha256,
            request_identity_sha256=request_hash,
            harness_id=plan.harness_id,
            compiled_plan_path=plan_relpath,
            compiled_sha256=plan.compiled_sha256,
            diagnostics_path=diagnostics_relpath,
            diagnostics=diagnostics,
        )
        commit_error = _write_diagnostics_report(
            output_dir=output_dir,
            filename=diagnostics_name,
            result=committed_result,
        )
        if commit_error is None:
            return committed_result
        return HarnessCompileResult(
            request_id=request.request_id,
            status=CompileStatus.PREPARED,
            plan_commit_certainty=PlanCommitCertainty.COMMITTED,
            diagnostic_report_state=DiagnosticReportState.UNKNOWN,
            source_document_sha256=source_document_sha256,
            source_sha256=plan.source_sha256,
            request_identity_sha256=request_hash,
            harness_id=plan.harness_id,
            compiled_plan_path=plan_relpath,
            compiled_sha256=plan.compiled_sha256,
            diagnostics_path=diagnostics_relpath,
            diagnostics=(
                *diagnostics,
                _output_diagnostic(
                    code="MF-O002",
                    message="Committed diagnostics report could not be written.",
                    fields=commit_error,
                ),
            ),
        )
    finally:
        _close_output_dir(output_dir)


class _PublishResult:
    PUBLISHED = "published"
    REUSED = "reused"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class _PublishOutcome:
    result: str | None = None
    error: tuple[DiagnosticField, ...] | None = None
    cleanup_error: tuple[DiagnosticField, ...] | None = None


def _publish_content_addressed_file(
    *,
    output_dir: _AdmittedOutputDir,
    destination_name: str,
    data: bytes,
) -> _PublishOutcome:
    temp_name = _temporary_path(destination_name)
    result: str | None = None
    error: tuple[DiagnosticField, ...] | None = None
    try:
        _write_exclusive_temp(output_dir.fd, temp_name, data)
        try:
            os.link(
                temp_name,
                destination_name,
                src_dir_fd=output_dir.fd,
                dst_dir_fd=output_dir.fd,
            )
        except FileExistsError:
            result = (
                _PublishResult.REUSED
                if _destination_matches(
                    output_dir=output_dir, destination_name=destination_name, data=data
                )
                else _PublishResult.CONFLICT
            )
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                result = (
                    _PublishResult.REUSED
                    if _destination_matches(
                        output_dir=output_dir,
                        destination_name=destination_name,
                        data=data,
                    )
                    else _PublishResult.CONFLICT
                )
            else:
                error = (_field("errno", exc.errno or errno.EIO),)
        else:
            result = _PublishResult.PUBLISHED
    except FileExistsError as exc:
        return _PublishOutcome(error=(_field("errno", exc.errno or errno.EEXIST),))
    except OSError as exc:
        error = (_field("errno", exc.errno or errno.EIO),)
    cleanup_error = _unlink_if_present(output_dir=output_dir, filename=temp_name)
    return _PublishOutcome(result=result, error=error, cleanup_error=cleanup_error)


def _write_diagnostics_report(
    *,
    output_dir: _AdmittedOutputDir,
    filename: str,
    result: HarnessCompileResult,
) -> tuple[DiagnosticField, ...] | None:
    data = canonical_json_serialize(result.model_dump(mode="json")).encode("utf-8")
    temp_name = _temporary_path(filename)
    try:
        _write_exclusive_temp(output_dir.fd, temp_name, data)
        os.replace(
            temp_name,
            filename,
            src_dir_fd=output_dir.fd,
            dst_dir_fd=output_dir.fd,
        )
        if not _fsync_directory(output_dir.fd):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
    except FileExistsError as exc:
        return (_field("errno", exc.errno or errno.EEXIST),)
    except OSError as exc:
        _unlink_if_present(output_dir=output_dir, filename=temp_name)
        return (_field("errno", exc.errno or errno.EIO),)
    return None


def _write_exclusive_temp(directory_fd: int, filename: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(filename, flags, 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _admit_output_dir(
    request: HarnessCompileRequest,
    *,
    request_hash: str | None = None,
) -> _AdmittedOutputDir | HarnessCompileResult:
    root = Path(request.output_root)
    output_parts = _relative_posix_parts(request.output_dir)
    if output_parts is None:
        return _failed_output_result(
            request=request,
            code="MF-O001",
            message="Output directory must stay within output_root.",
            request_hash=request_hash,
        )
    try:
        root_resolved = root.resolve(strict=True)
        if not stat.S_ISDIR(root_resolved.lstat().st_mode):
            raise NotADirectoryError(request.output_root)
        candidate = root_resolved.joinpath(*output_parts)
        parent = candidate.parent.resolve(strict=True)
        if not _is_relative_to(parent, root_resolved):
            raise PermissionError(request.output_dir)
        resolved = parent.joinpath(candidate.name)
        before = resolved.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise NotADirectoryError(request.output_dir)
        resolved = resolved.resolve(strict=True)
        if not _is_relative_to(resolved, root_resolved):
            raise PermissionError(request.output_dir)
        if (
            os.open not in os.supports_dir_fd
            or os.link not in os.supports_dir_fd
            or os.unlink not in os.supports_dir_fd
            or not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "O_NOFOLLOW")
        ):
            raise OSError(
                errno.EOPNOTSUPP, "anchored output-directory operations unavailable"
            )
        flags = os.O_RDONLY | os.O_DIRECTORY
        flags |= os.O_NOFOLLOW
        fd = os.open(resolved, flags)
        try:
            opened = os.fstat(fd)
            current = resolved.lstat()
            if (
                opened.st_dev != current.st_dev
                or opened.st_ino != current.st_ino
                or not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(current.st_mode)
            ):
                raise PermissionError(request.output_dir)
        except Exception:
            os.close(fd)
            raise
        return _AdmittedOutputDir(path=resolved, fd=fd)
    except OSError as exc:
        return _failed_output_result(
            request=request,
            code="MF-O001",
            message="Output directory could not be admitted for diagnostics.",
            request_hash=request_hash,
            fields=(_field("errno", exc.errno or errno.EIO),),
        )


def _failed_output_result(
    *,
    request: HarnessCompileRequest,
    code: str,
    message: str,
    plan: CompiledHarnessPlan | None = None,
    request_hash: str | None = None,
    source_document_sha256: str | None = None,
    diagnostics: tuple[CompilerDiagnostic, ...] = (),
    diagnostics_path: str | None = None,
    diagnostic_report_state: DiagnosticReportState = DiagnosticReportState.ABSENT,
    plan_relpath: str | None = None,
    plan_certainty: PlanCommitCertainty = PlanCommitCertainty.ABSENT,
    fields: tuple[DiagnosticField, ...] = (),
    cleanup_error: tuple[DiagnosticField, ...] | None = None,
) -> HarnessCompileResult:
    cleanup_diagnostics = (
        (
            _output_diagnostic(
                code="MF-O005",
                message="Compiled plan temporary file could not be cleaned up.",
                fields=cleanup_error,
            ),
        )
        if cleanup_error is not None
        else ()
    )
    return HarnessCompileResult(
        request_id=request.request_id,
        status=CompileStatus.FAILED,
        plan_commit_certainty=plan_certainty,
        diagnostic_report_state=diagnostic_report_state,
        failure_phase=CompilerPhase.OUTPUT,
        source_document_sha256=source_document_sha256,
        source_sha256=None if plan is None else plan.source_sha256,
        request_identity_sha256=request_hash,
        harness_id=None if plan is None else plan.harness_id,
        compiled_plan_path=plan_relpath,
        compiled_sha256=(
            None
            if plan is None or plan_certainty == PlanCommitCertainty.ABSENT
            else plan.compiled_sha256
        ),
        diagnostics_path=diagnostics_path,
        diagnostics=(
            *diagnostics,
            _output_diagnostic(code=code, message=message, fields=fields),
            *cleanup_diagnostics,
        ),
    )


def _output_diagnostic(
    *,
    code: str,
    message: str,
    fields: tuple[DiagnosticField, ...] = (),
) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        phase=CompilerPhase.OUTPUT,
        message=message,
        fields=fields,
    )


def _field(key: str, value: str | int | float | bool) -> DiagnosticField:
    return DiagnosticField(key=key, value=value)


def _destination_matches(
    *,
    output_dir: _AdmittedOutputDir,
    destination_name: str,
    data: bytes,
) -> bool:
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(destination_name, flags, dir_fd=output_dir.fd)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read() == data
    except OSError:
        return False


def _fsync_directory(directory: Path | int) -> bool:
    if isinstance(directory, int):
        try:
            os.fsync(directory)
        except OSError:
            return False
        return True
    if not hasattr(os, "O_DIRECTORY"):
        return True
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return False
    try:
        os.fsync(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def _temporary_path(destination_name: str) -> str:
    return f".{destination_name}.{secrets.token_hex(8)}.tmp"


def _unlink_if_present(
    *, output_dir: _AdmittedOutputDir, filename: str
) -> tuple[DiagnosticField, ...] | None:
    try:
        os.unlink(filename, dir_fd=output_dir.fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return (_field("errno", exc.errno or errno.EIO),)
    return None


def _join_output_path(output_dir: str, filename: str) -> str:
    value = f"{PurePosixPath(output_dir).as_posix().rstrip('/')}/{filename}"
    return validate_utf8_size(value, "output path", 1024)


def _safe_harness_identity(value: str) -> str:
    return validate_utf8_size(
        quote(value, safe=_HARNESS_ID_FILENAME_SAFE),
        "harness identity",
        320,
    )


def _harness_identity_prefix(
    *,
    harness_id: str | None,
    harness_version: int | None,
) -> str:
    if harness_id is None or harness_version is None:
        raise ValueError("harness diagnostics paths require identity and version")
    if harness_version <= 0:
        raise ValueError("harness_version must be positive")
    return f"{_safe_harness_identity(harness_id)}@{harness_version}"


def _relative_posix_parts(value: str) -> tuple[str, ...] | None:
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/") or "\\" in value:
        return None
    parts = path.parts
    if not parts or any(_is_forbidden_path_part(part) for part in parts):
        return None
    return parts


def _is_forbidden_path_part(part: str) -> bool:
    reserved_names = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    stem = part.split(".", 1)[0].casefold()
    return (
        part in {"", ".", ".."}
        or ":" in part
        or "\x00" in part
        or stem in reserved_names
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _admitted_output_dir_matches(output_dir: _AdmittedOutputDir) -> bool:
    try:
        current = output_dir.path.lstat()
        admitted = os.fstat(output_dir.fd)
    except OSError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and stat.S_ISDIR(admitted.st_mode)
        and current.st_dev == admitted.st_dev
        and current.st_ino == admitted.st_ino
    )


def _close_output_dir(output_dir: _AdmittedOutputDir) -> None:
    try:
        os.close(output_dir.fd)
    except OSError:
        return
