"""Compile request, invocation, and result contracts."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from typing import Literal, Protocol

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from millforge.compiler.diagnostics import (
    bound_diagnostics,
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticField,
    DiagnosticSeverity,
    SourceReference,
    detect_secret_candidate,
)
from millforge.compiler.parsing import (
    MAX_SOURCE_SIZE_BYTES,
    HarnessSourceParser,
    HarnessSourceParserProtocol,
    ParsedHarnessSource,
    SourceDocument,
)
from millforge.compiler.source import HarnessSource
from millforge.compiler.validators import (
    validate_harness_id,
    validate_request_id,
    validate_sha256,
    validate_stage_kind_id,
    validate_terminal_result,
    validate_unique,
    validate_utf8_size,
)
from millforge.contracts import CapabilityEnvelope, RedactionPolicy

_INVALID_REQUEST_ID = "request.invalid"


def _freeze_json_like(value: Any) -> Any:
    """Recursively freeze JSON-like nested containers."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json_like(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json_like(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json_like(item) for item in value)
    return value


class CompileStatus(str, Enum):
    """Closed compile-result status values."""

    FAILED = "failed"
    PREPARED = "prepared"
    COMMITTED = "committed"


class PlanCommitCertainty(str, Enum):
    """Closed plan publication certainty values."""

    ABSENT = "absent"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


class DiagnosticReportState(str, Enum):
    """Closed diagnostic report persistence states."""

    ABSENT = "absent"
    PREPARED = "prepared"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


class HarnessCompileRequest(BaseModel):
    """Serializable attribute-frozen public compile request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: StrictStr
    source_path: StrictStr
    source_root: StrictStr
    source_format: Literal["yaml", "json"]
    output_dir: StrictStr
    output_root: StrictStr
    expected_harness_id: StrictStr
    stage_kind_id: StrictStr
    legal_terminal_results: tuple[StrictStr, ...] = Field(min_length=1, max_length=64)
    capability_envelope: CapabilityEnvelope

    @field_validator("request_id")
    @classmethod
    def _request_id_valid(cls, value: str) -> str:
        return validate_request_id(value)

    @field_validator("source_path", "output_dir")
    @classmethod
    def _relative_path_size_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must be nonblank")
        return validate_utf8_size(value, "path", 1024)

    @field_validator("source_root", "output_root")
    @classmethod
    def _absolute_root_size_valid(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("root must be an absolute path string")
        return validate_utf8_size(value, "root", 4096)

    @field_validator("expected_harness_id")
    @classmethod
    def _expected_harness_id_valid(cls, value: str) -> str:
        return validate_harness_id(value)

    @field_validator("stage_kind_id")
    @classmethod
    def _stage_kind_id_valid(cls, value: str) -> str:
        return validate_stage_kind_id(value)

    @field_validator("legal_terminal_results")
    @classmethod
    def _legal_terminal_results_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            validate_terminal_result(item)
        return validate_unique(value, "legal_terminal_results")


class CompileInvocation(BaseModel):
    """Private deeply snapshotted compile invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: HarnessCompileRequest

    @classmethod
    def from_request(cls, request: HarnessCompileRequest) -> CompileInvocation:
        """Build a private deep snapshot of request-owned data."""
        snapshot = request.model_copy(deep=True)
        for grant in snapshot.capability_envelope.grants:
            if grant.constraints is not None:
                object.__setattr__(
                    grant, "constraints", _freeze_json_like(grant.constraints)
                )
        return cls(request=snapshot)


class HarnessCompileResult(BaseModel):
    """Immutable compile result boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: StrictStr
    status: CompileStatus
    plan_commit_certainty: PlanCommitCertainty
    diagnostic_report_state: DiagnosticReportState = DiagnosticReportState.ABSENT
    failure_phase: CompilerPhase | None = None
    source_document_sha256: StrictStr | None = None
    source_sha256: StrictStr | None = Field(
        default=None,
        validation_alias=AliasChoices("source_sha256", "source_semantic_sha256"),
    )
    request_identity_sha256: StrictStr | None = None
    harness_id: StrictStr | None = None
    compiled_plan_path: StrictStr | None = None
    compiled_sha256: StrictStr | None = None
    diagnostics_path: StrictStr | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = Field(default_factory=tuple)

    @field_validator("request_id")
    @classmethod
    def _request_id_valid(cls, value: str) -> str:
        return validate_request_id(value)

    @field_validator(
        "source_document_sha256",
        "source_sha256",
        "request_identity_sha256",
        "compiled_sha256",
    )
    @classmethod
    def _hash_valid(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "sha256")
        return validate_sha256(value, field_name)

    @field_validator("harness_id")
    @classmethod
    def _harness_id_valid(cls, value: str | None) -> str | None:
        return None if value is None else validate_harness_id(value)

    @field_validator("compiled_plan_path", "diagnostics_path")
    @classmethod
    def _relative_output_path_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip() or _relative_posix_parts(value) is None:
            raise ValueError("output path must be a relative POSIX path string")
        return validate_utf8_size(value, "output path", 1024)

    @field_validator("diagnostics")
    @classmethod
    def _diagnostics_sorted(
        cls, value: tuple[CompilerDiagnostic, ...]
    ) -> tuple[CompilerDiagnostic, ...]:
        return bound_diagnostics(value)

    @model_validator(mode="after")
    def _result_invariants(self) -> HarnessCompileResult:
        if self.source_sha256 is not None and self.source_document_sha256 is None:
            raise ValueError("semantic hashes require a source document hash")
        if self.harness_id is not None and self.source_sha256 is None:
            raise ValueError("harness identity requires a semantic hash")
        if self.diagnostic_report_state == DiagnosticReportState.ABSENT:
            if self.diagnostics_path is not None:
                raise ValueError("absent diagnostics cannot carry a diagnostics path")
        elif self.diagnostic_report_state in {
            DiagnosticReportState.PREPARED,
            DiagnosticReportState.COMMITTED,
        }:
            if self.diagnostics_path is None:
                raise ValueError("persisted diagnostics require a diagnostics path")
        if self.status == CompileStatus.FAILED:
            if self.failure_phase is None:
                raise ValueError("failed results require a failure phase")
            if self.plan_commit_certainty == PlanCommitCertainty.COMMITTED:
                raise ValueError("failed results cannot have committed certainty")
            if self.failure_phase == CompilerPhase.REQUEST:
                if (
                    self.source_document_sha256 is not None
                    or self.source_sha256 is not None
                    or self.harness_id is not None
                ):
                    raise ValueError("request failures cannot carry source evidence")
            if self.failure_phase == CompilerPhase.PARSE:
                if self.source_sha256 is not None or self.harness_id is not None:
                    raise ValueError("parse failures cannot carry semantic evidence")
            if self.plan_commit_certainty == PlanCommitCertainty.ABSENT:
                if (
                    self.compiled_plan_path is not None
                    or self.compiled_sha256 is not None
                ):
                    raise ValueError(
                        "failed/absent carries no plan path or compiled hash"
                    )
            if self.plan_commit_certainty == PlanCommitCertainty.UNKNOWN:
                if self.failure_phase != CompilerPhase.OUTPUT:
                    raise ValueError(
                        "unknown certainty is only valid for output failures"
                    )
                if self.compiled_plan_path is None or self.compiled_sha256 is None:
                    raise ValueError(
                        "unknown certainty requires candidate plan path and hash"
                    )
        if self.status == CompileStatus.PREPARED:
            if self.failure_phase is not None:
                raise ValueError("prepared results cannot carry a failure phase")
            if (
                self.source_document_sha256 is None
                or self.source_sha256 is None
                or self.harness_id is None
            ):
                raise ValueError(
                    "prepared results require source hashes and harness identity"
                )
            if self.plan_commit_certainty == PlanCommitCertainty.UNKNOWN:
                raise ValueError("prepared results cannot have unknown certainty")
            if self.plan_commit_certainty == PlanCommitCertainty.COMMITTED:
                if self.compiled_plan_path is None or self.compiled_sha256 is None:
                    raise ValueError(
                        "prepared/committed results require compiled plan path and hash"
                    )
            if (
                self.plan_commit_certainty == PlanCommitCertainty.ABSENT
                and self.compiled_plan_path is not None
            ):
                raise ValueError("prepared/absent carries no committed plan path")
        if self.status == CompileStatus.COMMITTED:
            if self.failure_phase is not None:
                raise ValueError("committed results cannot carry a failure phase")
            if self.plan_commit_certainty != PlanCommitCertainty.COMMITTED:
                raise ValueError("committed results require committed certainty")
            required = (
                self.compiled_plan_path,
                self.compiled_sha256,
                self.source_sha256,
                self.source_document_sha256,
                self.harness_id,
            )
            if any(item is None for item in required):
                raise ValueError(
                    "committed results require paths, hashes, and harness identity"
                )
        return self


class HarnessRequestAdmissionResult(BaseModel):
    """Raw request admission result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: HarnessCompileRequest | None = None
    parsed_source: ParsedHarnessSource | None = None
    result: HarnessCompileResult | None = None

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> HarnessRequestAdmissionResult:
        if (self.request is None) == (self.result is None):
            raise ValueError("exactly one of request or result must be provided")
        if self.request is None and self.parsed_source is not None:
            raise ValueError("failed admission cannot carry parsed source")
        if self.request is not None:
            if self.parsed_source is None or self.parsed_source.source is None:
                raise ValueError("successful admission requires parsed source")
        return self


class HarnessCompileRequestAdmission(Protocol):
    """Protocol for raw mapping admission into compile requests."""

    def admit(
        self,
        raw_request: Mapping[str, object],
    ) -> HarnessRequestAdmissionResult:
        """Admit a raw request or return a failed compile result."""
        ...


class DefaultHarnessCompileRequestAdmission:
    """Filesystem-safe admission boundary for raw compile requests."""

    def __init__(
        self,
        *,
        parser: HarnessSourceParserProtocol | None = None,
    ) -> None:
        self._parser = parser or HarnessSourceParser()

    def admit(
        self,
        raw_request: Mapping[str, object],
    ) -> HarnessRequestAdmissionResult:
        """Admit a raw request after path and source checks, or fail deterministically."""
        request_id = _request_id_from_raw(raw_request)
        try:
            request = HarnessCompileRequest.model_validate(raw_request)
        except ValidationError as exc:
            return _failed_admission(
                request_id=request_id,
                code="MF-S018",
                message="Compile request schema validation failed.",
                phase=CompilerPhase.REQUEST,
                fields=(_diagnostic_field("field_path", _validation_pointer(exc)),),
            )

        root_admission = _admit_roots_and_output(request)
        if root_admission is not None:
            return root_admission

        source_admission = _read_admitted_source(request)
        if isinstance(source_admission, HarnessRequestAdmissionResult):
            return source_admission

        parsed = self._parser.parse(
            SourceDocument(
                logical_path=request.source_path,
                format=request.source_format,
                content=source_admission,
            )
        )
        if parsed.diagnostics:
            return HarnessRequestAdmissionResult(
                result=HarnessCompileResult(
                    request_id=request.request_id,
                    status=CompileStatus.FAILED,
                    plan_commit_certainty=PlanCommitCertainty.ABSENT,
                    failure_phase=parsed.diagnostics[0].phase,
                    source_document_sha256=(
                        None
                        if parsed.source_document_sha256 == "0" * 64
                        else parsed.source_document_sha256
                    ),
                    diagnostics=parsed.diagnostics,
                )
            )
        source_diagnostics = _post_parse_source_diagnostics(
            request=request,
            parsed=parsed,
        )
        if source_diagnostics:
            return _failed_source_admission(
                request=request,
                parsed=parsed,
                diagnostics=source_diagnostics,
            )
        return HarnessRequestAdmissionResult(request=request, parsed_source=parsed)


def _request_id_from_raw(raw_request: Mapping[str, object]) -> str:
    value = raw_request.get("request_id")
    if isinstance(value, str):
        try:
            return validate_request_id(value)
        except ValueError:
            pass
    return _INVALID_REQUEST_ID


def _admit_roots_and_output(
    request: HarnessCompileRequest,
) -> HarnessRequestAdmissionResult | None:
    diagnostics: list[CompilerDiagnostic] = []

    source_parts = _relative_posix_parts(request.source_path)
    if source_parts is None:
        diagnostics.append(
            _request_diagnostic(
                code="MF-S001",
                message="Source path must stay within source_root.",
            )
        )

    output_parts = _relative_posix_parts(request.output_dir)
    if output_parts is None:
        diagnostics.append(
            _request_diagnostic(
                code="MF-S002",
                message="Output directory must stay within output_root.",
            )
        )

    output_root = _resolve_existing_directory(
        request.output_root,
        request_id=request.request_id,
        code="MF-S016",
        label="output_root",
    )
    if isinstance(output_root, HarnessRequestAdmissionResult):
        assert output_root.result is not None
        diagnostics.extend(output_root.result.diagnostics)
        output_root_path: Path | None = None
    else:
        output_root_path = output_root

    source_root = _resolve_existing_directory(
        request.source_root,
        request_id=request.request_id,
        code="MF-S013",
        label="source_root",
    )
    if isinstance(source_root, HarnessRequestAdmissionResult):
        assert source_root.result is not None
        diagnostics.extend(source_root.result.diagnostics)
        source_root_path: Path | None = None
    else:
        source_root_path = source_root

    if output_root_path is not None and output_parts is not None:
        output_diagnostic = _admit_output_directory(
            request=request,
            output_root=output_root_path,
            output_parts=output_parts,
        )
        if output_diagnostic is not None:
            diagnostics.append(output_diagnostic)

    if source_root_path is not None and source_parts is not None:
        source_diagnostic = _admit_source_path(
            source_root=source_root_path,
            source_parts=source_parts,
        )
        if source_diagnostic is not None:
            diagnostics.append(source_diagnostic)

    if diagnostics:
        return _failed_request_admission_many(
            request_id=request.request_id,
            diagnostics=tuple(diagnostics),
        )
    return None


def _resolve_existing_directory(
    value: str,
    *,
    request_id: str,
    code: str,
    label: str,
) -> Path | HarnessRequestAdmissionResult:
    try:
        path = Path(value)
        path_stat = path.lstat()
    except OSError as exc:
        return _failed_admission(
            request_id=request_id,
            code=code,
            message=f"{label} must be an existing directory.",
            phase=CompilerPhase.REQUEST,
            fields=(_diagnostic_field("errno", exc.errno or errno.ENOENT),),
        )
    if not stat.S_ISDIR(path_stat.st_mode):
        return _failed_admission(
            request_id=request_id,
            code=code,
            message=f"{label} must be a directory.",
            phase=CompilerPhase.REQUEST,
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        return _failed_admission(
            request_id=request_id,
            code=code,
            message=f"{label} must be an existing directory.",
            phase=CompilerPhase.REQUEST,
            fields=(_diagnostic_field("errno", exc.errno or errno.ENOENT),),
        )
    return resolved


def _read_admitted_source(
    request: HarnessCompileRequest,
) -> bytes | HarnessRequestAdmissionResult:
    root = Path(request.source_root).resolve(strict=True)
    source_parts = _relative_posix_parts(request.source_path)
    if source_parts is None:
        return _failed_admission(
            request_id=request.request_id,
            code="MF-S001",
            message="Source path must stay within source_root.",
            phase=CompilerPhase.REQUEST,
        )
    candidate = root.joinpath(*source_parts)
    try:
        parent_resolved = candidate.parent.resolve(strict=True)
    except OSError as exc:
        return _failed_admission(
            request_id=request.request_id,
            code="MF-S013",
            message="Source file does not exist.",
            phase=CompilerPhase.REQUEST,
            fields=(_diagnostic_field("errno", exc.errno or errno.ENOENT),),
        )
    if not _is_relative_to(parent_resolved, root):
        return _failed_admission(
            request_id=request.request_id,
            code="MF-S001",
            message="Source path must stay within source_root.",
            phase=CompilerPhase.REQUEST,
        )
    resolved = parent_resolved.joinpath(candidate.name)

    try:
        before = resolved.lstat()
    except FileNotFoundError as exc:
        return _failed_admission(
            request_id=request.request_id,
            code="MF-S013",
            message="Source file does not exist.",
            phase=CompilerPhase.REQUEST,
            fields=(_diagnostic_field("errno", exc.errno or errno.ENOENT),),
        )
    except OSError as exc:
        return _failed_admission(
            request_id=request.request_id,
            code="MF-S013",
            message="Source file could not be read.",
            phase=CompilerPhase.REQUEST,
            fields=(_diagnostic_field("errno", exc.errno or errno.EIO),),
        )
    if not stat.S_ISREG(before.st_mode):
        if stat.S_ISLNK(before.st_mode):
            try:
                symlink_target = resolved.resolve(strict=True)
            except OSError:
                symlink_target = None
            else:
                if symlink_target is not None and not _is_relative_to(
                    symlink_target, root
                ):
                    return _failed_admission(
                        request_id=request.request_id,
                        code="MF-S001",
                        message="Source path must stay within source_root.",
                        phase=CompilerPhase.REQUEST,
                    )
        return _failed_admission(
            request_id=request.request_id,
            code="MF-S014",
            message="Source path must be a regular file.",
            phase=CompilerPhase.REQUEST,
        )

    try:
        with resolved.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not _same_file(before, opened) or not stat.S_ISREG(opened.st_mode):
                return _source_replaced(request.request_id)
            content = handle.read(MAX_SOURCE_SIZE_BYTES + 1)
            if len(content) > MAX_SOURCE_SIZE_BYTES:
                return _failed_admission(
                    request_id=request.request_id,
                    code="MF-S003",
                    message="Source file exceeds the maximum size.",
                    phase=CompilerPhase.PARSE,
                    fields=(_diagnostic_field("source_size", len(content)),),
                )
            after = resolved.stat()
            if not _same_file(opened, after):
                return _source_replaced(request.request_id)
            return content
    except OSError as exc:
        return _failed_admission(
            request_id=request.request_id,
            code="MF-S013",
            message="Source file could not be read.",
            phase=CompilerPhase.REQUEST,
            fields=(_diagnostic_field("errno", exc.errno or errno.EIO),),
        )


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
        or part.endswith((" ", "."))
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


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
    )


def _source_replaced(request_id: str) -> HarnessRequestAdmissionResult:
    return _failed_admission(
        request_id=request_id,
        code="MF-S015",
        message="Source file changed during admission.",
        phase=CompilerPhase.REQUEST,
    )


def _failed_source_admission(
    *,
    request: HarnessCompileRequest,
    parsed: ParsedHarnessSource,
    diagnostics: tuple[CompilerDiagnostic, ...],
) -> HarnessRequestAdmissionResult:
    source = parsed.source
    source_sha256 = None
    harness_id = None
    if source is not None:
        source_sha256 = _source_semantic_sha256(source)
        harness_id = source.harness_id
    return HarnessRequestAdmissionResult(
        result=HarnessCompileResult(
            request_id=request.request_id,
            status=CompileStatus.FAILED,
            plan_commit_certainty=PlanCommitCertainty.ABSENT,
            failure_phase=CompilerPhase.SCHEMA,
            source_document_sha256=parsed.source_document_sha256,
            source_sha256=source_sha256,
            harness_id=harness_id,
            diagnostics=diagnostics,
        )
    )


def _source_semantic_sha256(source: HarnessSource) -> str:
    payload = json.dumps(
        source.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _post_parse_source_diagnostics(
    *,
    request: HarnessCompileRequest,
    parsed: ParsedHarnessSource,
) -> tuple[CompilerDiagnostic, ...]:
    source = parsed.source
    if source is None:
        return ()
    diagnostics = [
        *_cross_field_diagnostics(
            request=request,
            source=source,
            logical_path=request.source_path,
            location_index=parsed.location_index,
        ),
        *_secret_scalar_diagnostics(
            source=source,
            logical_path=request.source_path,
            location_index=parsed.location_index,
        ),
    ]
    return bound_diagnostics(diagnostics)


def _cross_field_diagnostics(
    *,
    request: HarnessCompileRequest,
    source: HarnessSource,
    logical_path: str,
    location_index: tuple[SourceReference, ...],
) -> tuple[CompilerDiagnostic, ...]:
    diagnostics: list[CompilerDiagnostic] = []
    if source.harness_id != request.expected_harness_id:
        diagnostics.append(
            _source_schema_diagnostic(
                code="MF-S027",
                message="Source harness_id does not match expected_harness_id.",
                logical_path=logical_path,
                field_path="/harness_id",
                location_index=location_index,
                fields=(_diagnostic_field("field_path", "/harness_id"),),
            )
        )

    if request.stage_kind_id not in source.stage_scope.stage_kind_ids:
        diagnostics.append(
            _source_schema_diagnostic(
                code="MF-S028",
                message="Requested stage_kind_id is outside source stage_scope.",
                logical_path=logical_path,
                field_path="/stage_scope/stage_kind_ids",
                location_index=location_index,
                fields=(
                    _diagnostic_field("field_path", "/stage_scope/stage_kind_ids"),
                ),
            )
        )

    terminal_diagnostic = _terminal_result_diagnostic(
        request=request,
        source=source,
        logical_path=logical_path,
        location_index=location_index,
    )
    if terminal_diagnostic is not None:
        diagnostics.append(terminal_diagnostic)
    return tuple(diagnostics)


def _terminal_result_diagnostic(
    *,
    request: HarnessCompileRequest,
    source: HarnessSource,
    logical_path: str,
    location_index: tuple[SourceReference, ...],
) -> CompilerDiagnostic | None:
    terminal_paths: dict[str, str] = {}
    for index, node in enumerate(source.graph.nodes):
        if node.terminal_result is None:
            continue
        field_path = f"/graph/nodes/{index}/terminal_result"
        if node.terminal_result in terminal_paths:
            return _source_schema_diagnostic(
                code="MF-S029",
                message="Legal terminal results must match source terminal declarations.",
                logical_path=logical_path,
                field_path=field_path,
                location_index=location_index,
                fields=(_diagnostic_field("field_path", field_path),),
            )
        terminal_paths[node.terminal_result] = field_path
    source_terminals = set(terminal_paths)
    requested_terminals = set(request.legal_terminal_results)

    missing_source_terminal = next(
        (
            terminal
            for terminal in request.legal_terminal_results
            if terminal not in source_terminals
        ),
        None,
    )
    undeclared_source_terminal = next(
        (
            node.terminal_result
            for node in source.graph.nodes
            if node.terminal_result is not None
            and node.terminal_result not in requested_terminals
        ),
        None,
    )
    unused_artifact_mapping = next(
        (
            item.terminal_result
            for item in source.artifacts.required_by_terminal
            if item.terminal_result not in source_terminals
        ),
        None,
    )
    if (
        missing_source_terminal is None
        and undeclared_source_terminal is None
        and unused_artifact_mapping is None
    ):
        return None

    if undeclared_source_terminal is not None:
        field_path = terminal_paths[undeclared_source_terminal]
    elif unused_artifact_mapping is not None:
        field_path = next(
            (
                f"/artifacts/required_by_terminal/{index}/terminal_result"
                for index, item in enumerate(source.artifacts.required_by_terminal)
                if item.terminal_result == unused_artifact_mapping
            ),
            "/artifacts/required_by_terminal",
        )
    else:
        field_path = "/graph/nodes"
    return _source_schema_diagnostic(
        code="MF-S029",
        message="Legal terminal results must match source terminal declarations.",
        logical_path=logical_path,
        field_path=field_path,
        location_index=location_index,
        fields=(_diagnostic_field("field_path", field_path),),
    )


def _secret_scalar_diagnostics(
    *,
    source: HarnessSource,
    logical_path: str,
    location_index: tuple[SourceReference, ...],
) -> tuple[CompilerDiagnostic, ...]:
    policy = RedactionPolicy()
    diagnostics: list[CompilerDiagnostic] = []
    for field_path, field_name, value in _source_scalar_strings(
        source.model_dump(mode="python")
    ):
        if not detect_secret_candidate(
            field_path=field_path,
            field_name=field_name,
            value=value,
            policy=policy,
        ):
            continue
        diagnostics.append(
            _source_schema_diagnostic(
                code="MF-S026",
                message="Source scalar resembles a secret and must be removed.",
                logical_path=logical_path,
                field_path=field_path,
                location_index=location_index,
                fields=(_diagnostic_field("field_path", field_path),),
            )
        )
    return tuple(diagnostics)


def _source_scalar_strings(
    value: object,
    *,
    field_path: str = "/",
    field_name: str = "",
) -> tuple[tuple[str, str, str], ...]:
    if isinstance(value, str):
        return ((field_path, field_name, value),)
    if isinstance(value, Mapping):
        result: list[tuple[str, str, str]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            result.extend(
                _source_scalar_strings(
                    item,
                    field_path=_join_pointer(field_path, key),
                    field_name=key,
                )
            )
        return tuple(result)
    if isinstance(value, tuple | list):
        result = []
        for index, item in enumerate(value):
            result.extend(
                _source_scalar_strings(
                    item,
                    field_path=_join_pointer(field_path, str(index)),
                    field_name=field_name,
                )
            )
        return tuple(result)
    return ()


def _source_schema_diagnostic(
    *,
    code: str,
    message: str,
    logical_path: str,
    field_path: str,
    location_index: tuple[SourceReference, ...],
    fields: tuple[DiagnosticField, ...] = (),
) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        phase=CompilerPhase.SCHEMA,
        message=message,
        source_reference=_nearest_source_reference(
            logical_path=logical_path,
            field_path=field_path,
            location_index=location_index,
        ),
        fields=fields,
    )


def _nearest_source_reference(
    *,
    logical_path: str,
    field_path: str,
    location_index: tuple[SourceReference, ...],
) -> SourceReference:
    by_path = {reference.field_path: reference for reference in location_index}
    candidate = field_path
    while candidate:
        if candidate in by_path:
            reference = by_path[candidate]
            if reference.field_path == field_path:
                return reference
            return SourceReference(
                logical_path=reference.logical_path,
                field_path=field_path,
                location=reference.location,
            )
        if candidate == "/":
            break
        candidate = candidate.rsplit("/", 1)[0] or "/"
    return SourceReference(logical_path=logical_path, field_path=field_path)


def _failed_admission(
    *,
    request_id: str,
    code: str,
    message: str,
    phase: CompilerPhase,
    fields: tuple[DiagnosticField, ...] = (),
) -> HarnessRequestAdmissionResult:
    return _failed_request_admission_many(
        request_id=request_id,
        diagnostics=(
            CompilerDiagnostic(
                code=code,
                severity=DiagnosticSeverity.ERROR,
                phase=phase,
                message=message,
                fields=fields,
            ),
        ),
    )


def _failed_request_admission_many(
    *,
    request_id: str,
    diagnostics: tuple[CompilerDiagnostic, ...],
) -> HarnessRequestAdmissionResult:
    return HarnessRequestAdmissionResult(
        result=HarnessCompileResult(
            request_id=request_id,
            status=CompileStatus.FAILED,
            plan_commit_certainty=PlanCommitCertainty.ABSENT,
            failure_phase=diagnostics[0].phase,
            diagnostics=diagnostics,
        )
    )


def _request_diagnostic(
    *,
    code: str,
    message: str,
    fields: tuple[DiagnosticField, ...] = (),
) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        phase=CompilerPhase.REQUEST,
        message=message,
        fields=fields,
    )


def _admit_output_directory(
    *,
    request: HarnessCompileRequest,
    output_root: Path,
    output_parts: tuple[str, ...],
) -> CompilerDiagnostic | None:
    candidate = output_root.joinpath(*output_parts)
    try:
        parent_resolved = candidate.parent.resolve(strict=True)
    except OSError as exc:
        return _request_diagnostic(
            code="MF-S017",
            message="Output directory must already exist.",
            fields=(_diagnostic_field("errno", exc.errno or errno.ENOENT),),
        )
    if not _is_relative_to(parent_resolved, output_root):
        return _request_diagnostic(
            code="MF-S002",
            message="Output directory must stay within output_root.",
        )
    resolved = parent_resolved.joinpath(candidate.name)
    try:
        before = resolved.lstat()
    except FileNotFoundError as exc:
        return _request_diagnostic(
            code="MF-S017",
            message="Output directory must already exist.",
            fields=(_diagnostic_field("errno", exc.errno or errno.ENOENT),),
        )
    except OSError as exc:
        return _request_diagnostic(
            code="MF-S017",
            message="Output directory could not be admitted.",
            fields=(_diagnostic_field("errno", exc.errno or errno.EIO),),
        )
    if stat.S_ISLNK(before.st_mode):
        try:
            symlink_target = resolved.resolve(strict=True)
        except OSError as exc:
            return _request_diagnostic(
                code="MF-S017",
                message="Output directory could not be admitted.",
                fields=(_diagnostic_field("errno", exc.errno or errno.EIO),),
            )
        if not _is_relative_to(symlink_target, output_root):
            return _request_diagnostic(
                code="MF-S002",
                message="Output directory must stay within output_root.",
            )
        return _request_diagnostic(
            code="MF-S017",
            message="Output directory must be a directory.",
            fields=(_diagnostic_field("output_dir", request.output_dir),),
        )
    if not stat.S_ISDIR(before.st_mode):
        return _request_diagnostic(
            code="MF-S017",
            message="Output directory must be a directory.",
            fields=(_diagnostic_field("output_dir", request.output_dir),),
        )
    try:
        resolved = resolved.resolve(strict=True)
    except OSError as exc:
        return _request_diagnostic(
            code="MF-S017",
            message="Output directory could not be admitted.",
            fields=(_diagnostic_field("errno", exc.errno or errno.EIO),),
        )
    if not _is_relative_to(resolved, output_root):
        return _request_diagnostic(
            code="MF-S002",
            message="Output directory must stay within output_root.",
        )
    return None


def _admit_source_path(
    *,
    source_root: Path,
    source_parts: tuple[str, ...],
) -> CompilerDiagnostic | None:
    candidate = source_root.joinpath(*source_parts)
    try:
        parent_resolved = candidate.parent.resolve(strict=True)
    except OSError:
        return None
    if not _is_relative_to(parent_resolved, source_root):
        return _request_diagnostic(
            code="MF-S001",
            message="Source path must stay within source_root.",
        )
    resolved = parent_resolved.joinpath(candidate.name)
    try:
        before = resolved.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(before.st_mode):
        try:
            symlink_target = resolved.resolve(strict=True)
        except OSError:
            return None
        if not _is_relative_to(symlink_target, source_root):
            return _request_diagnostic(
                code="MF-S001",
                message="Source path must stay within source_root.",
            )
    return None


def _diagnostic_field(key: str, value: str | int) -> DiagnosticField:
    return DiagnosticField(key=key, value=value)


def _join_pointer(parent: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"/{escaped}" if parent == "/" else f"{parent}/{escaped}"


def _validation_pointer(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "/"
    loc = errors[0]["loc"]
    if not isinstance(loc, tuple) or not loc:
        return "/"
    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in loc]
    return "/" + "/".join(parts)
