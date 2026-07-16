"""Default validated compiler service orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from millforge import canonical_json_serialize, verify_compiled_plan_sha256
from millforge.compiled_plan import CompiledHarnessPlan
from millforge.compiled_plan import calculate_compiled_plan_sha256
from millforge.compiler.catalogs import (
    ModelProfileCatalogSnapshot,
    ToolCatalogSnapshot,
)
from millforge.compiler.canonicalization import source_sha256
from millforge.compiler.diagnostics import (
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticField,
    DiagnosticSeverity,
    bound_diagnostics,
)
from millforge.compiler.lowering import (
    CompiledPlanValidationError,
    LoweringInvariantError,
    SourceSemanticHashError,
    lower_resolved_harness,
)
from millforge.compiler.output import persist_compile_outputs
from millforge.compiler.parsing import ParsedHarnessSource
from millforge.compiler.requests import (
    CompileStatus,
    DefaultHarnessCompileRequestAdmission,
    HarnessCompileRequest,
    HarnessCompileRequestAdmission,
    HarnessCompileResult,
    HarnessRequestAdmissionResult,
    PlanCommitCertainty,
    _post_parse_source_diagnostics,
)
from millforge.compiler.source import HarnessSource
from millforge.compiler.semantic import ResolvedHarness, compile_semantic_from_admission
from millforge.contracts import CapabilityEnvelope


class HarnessCompiler(Protocol):
    """Public typed compiler service boundary."""

    def compile(
        self,
        request: HarnessCompileRequest,
        *,
        tool_catalog: ToolCatalogSnapshot,
        model_profile_catalog: ModelProfileCatalogSnapshot,
    ) -> HarnessCompileResult:
        """Compile an admitted harness request."""
        ...


class InMemoryHarnessCompileError(Exception):
    """Raised when a validated in-memory harness source cannot compile."""

    diagnostics: tuple[CompilerDiagnostic, ...]

    def __init__(self, diagnostics: tuple[CompilerDiagnostic, ...]) -> None:
        self.diagnostics = tuple(
            diagnostic.model_copy(update={"source_reference": None})
            for diagnostic in bound_diagnostics(diagnostics)
        )
        super().__init__(
            self.diagnostics[0].message
            if self.diagnostics
            else "in-memory harness compilation failed"
        )


def compile(
    request: HarnessCompileRequest,
    *,
    tool_catalog: ToolCatalogSnapshot,
    model_profile_catalog: ModelProfileCatalogSnapshot,
) -> HarnessCompileResult:
    """Compile an admitted request through semantic validation, lowering, and output."""
    return _compile_validated(
        request,
        tool_catalog=tool_catalog,
        model_profile_catalog=model_profile_catalog,
    )


def compile_harness_source_in_memory(
    *,
    request_id: str,
    source: HarnessSource,
    stage_kind_id: str,
    legal_terminal_results: tuple[str, ...],
    capability_envelope: CapabilityEnvelope,
    tool_catalog: ToolCatalogSnapshot,
    model_profile_catalog: ModelProfileCatalogSnapshot,
) -> CompiledHarnessPlan:
    """Compile a validated harness source without filesystem admission or publication."""
    request = HarnessCompileRequest(
        request_id=request_id,
        source_path="in-memory",
        source_root="/",
        source_format="json",
        output_dir="in-memory",
        output_root="/",
        expected_harness_id=source.harness_id,
        stage_kind_id=stage_kind_id,
        legal_terminal_results=legal_terminal_results,
        capability_envelope=capability_envelope,
    )
    parsed_source = ParsedHarnessSource(
        source=source,
        source_document_sha256="0" * 64,
    )
    diagnostics = _post_parse_source_diagnostics(
        request=request,
        parsed=parsed_source,
    )
    if diagnostics:
        raise InMemoryHarnessCompileError(diagnostics)

    try:
        outcome = _compile_post_parse(
            HarnessRequestAdmissionResult(
                request=request,
                parsed_source=parsed_source,
            ),
            tool_catalog=tool_catalog,
            model_profile_catalog=model_profile_catalog,
        )
    except Exception as exc:
        raise InMemoryHarnessCompileError(
            (
                _diagnostic(
                    "MF-I001",
                    CompilerPhase.INTERNAL,
                    "Compiler service failed internally.",
                    fields={"error_type": type(exc).__name__},
                ),
            )
        ) from exc
    if isinstance(outcome, HarnessCompileResult):
        raise InMemoryHarnessCompileError(outcome.diagnostics)
    return outcome


def _compile_validated(
    request: HarnessCompileRequest,
    *,
    tool_catalog: ToolCatalogSnapshot,
    model_profile_catalog: ModelProfileCatalogSnapshot,
) -> HarnessCompileResult:
    """Private implementation for the public typed compiler entry point."""
    try:
        admission = DefaultHarnessCompileRequestAdmission().admit(
            request.model_dump(mode="python")
        )
        return _compile_admitted(
            admission,
            tool_catalog=tool_catalog,
            model_profile_catalog=model_profile_catalog,
        )
    except Exception as exc:
        return _failed_result(
            request=request,
            phase=CompilerPhase.INTERNAL,
            diagnostics=(
                _diagnostic(
                    "MF-I001",
                    CompilerPhase.INTERNAL,
                    "Compiler service failed internally.",
                    fields={"error_type": type(exc).__name__},
                ),
            ),
        )


def compile_raw(
    raw_request: Mapping[str, object],
    *,
    tool_catalog: ToolCatalogSnapshot,
    model_profile_catalog: ModelProfileCatalogSnapshot,
    admission: HarnessCompileRequestAdmission | None = None,
) -> HarnessCompileResult:
    """Admit a raw request, then compile through the validated typed service."""
    admitted = (admission or DefaultHarnessCompileRequestAdmission()).admit(raw_request)
    if admitted.result is None and admitted.request is not None:
        request = admitted.request
    else:
        request = HarnessCompileRequest.model_construct(
            request_id="request.invalid",
            source_path="invalid",
            source_root="/",
            source_format="json",
            output_dir="invalid",
            output_root="/",
            expected_harness_id="millforge.invalid",
            stage_kind_id="builder",
            legal_terminal_results=("BLOCKED",),
            capability_envelope={"grants": ()},
        )
    try:
        return _compile_admitted(
            admitted,
            tool_catalog=tool_catalog,
            model_profile_catalog=model_profile_catalog,
        )
    except Exception as exc:
        return _failed_result(
            request=request,
            phase=CompilerPhase.INTERNAL,
            diagnostics=(
                _diagnostic(
                    "MF-I001",
                    CompilerPhase.INTERNAL,
                    "Compiler service failed internally.",
                    fields={"error_type": type(exc).__name__},
                ),
            ),
        )


def _compile_admitted(
    admission: HarnessRequestAdmissionResult,
    *,
    tool_catalog: ToolCatalogSnapshot,
    model_profile_catalog: ModelProfileCatalogSnapshot,
) -> HarnessCompileResult:
    if admission.result is not None:
        return admission.result
    assert admission.request is not None
    assert admission.parsed_source is not None
    outcome = _compile_post_parse(
        admission,
        tool_catalog=tool_catalog,
        model_profile_catalog=model_profile_catalog,
    )
    if isinstance(outcome, HarnessCompileResult):
        return outcome
    return persist_compile_outputs(
        request=admission.request,
        plan=outcome,
        source_document_sha256=admission.parsed_source.source_document_sha256,
    )


def _compile_post_parse(
    admission: HarnessRequestAdmissionResult,
    *,
    tool_catalog: ToolCatalogSnapshot,
    model_profile_catalog: ModelProfileCatalogSnapshot,
) -> CompiledHarnessPlan | HarnessCompileResult:
    """Run the shared semantic, lowering, and compiled-hash phases."""
    assert admission.request is not None
    assert admission.parsed_source is not None
    source = admission.parsed_source.source
    semantic = compile_semantic_from_admission(
        admission,
        source,
        tool_snapshot=tool_catalog,
        model_profile_snapshot=model_profile_catalog,
    )
    if semantic.frontend_result is not None:
        return semantic.frontend_result
    if semantic.resolved_harness is None or semantic.diagnostics:
        return _failed_result(
            request=admission.request,
            phase=_failure_phase(semantic.diagnostics),
            diagnostics=semantic.diagnostics
            or (
                _diagnostic(
                    "MF-I001",
                    CompilerPhase.INTERNAL,
                    "Semantic compilation returned no resolved harness.",
                ),
            ),
            source_document_sha256=admission.parsed_source.source_document_sha256,
        )

    try:
        plan = lower_resolved_harness(semantic.resolved_harness)
    except SourceSemanticHashError as exc:
        return _failed_result(
            request=admission.request,
            phase=CompilerPhase.LOWERING,
            diagnostics=(
                _diagnostic(
                    "MF-L003",
                    CompilerPhase.LOWERING,
                    "Source semantic hash calculation failed during lowering.",
                    fields={"error_type": type(exc).__name__},
                ),
            ),
            source_document_sha256=admission.parsed_source.source_document_sha256,
        )
    except CompiledPlanValidationError as exc:
        return _lowering_failed_result(
            request=admission.request,
            source_document_sha256=admission.parsed_source.source_document_sha256,
            resolved=semantic.resolved_harness,
            code="MF-L002",
            message="Compiled plan model validation failed during lowering.",
            error_type=type(exc).__name__,
        )
    except LoweringInvariantError as exc:
        return _lowering_failed_result(
            request=admission.request,
            source_document_sha256=admission.parsed_source.source_document_sha256,
            resolved=semantic.resolved_harness,
            code="MF-L001",
            message="Lowering invariant failed.",
            error_type=type(exc).__name__,
        )

    hash_failure = _compiled_hash_failure(plan)
    if hash_failure is not None:
        return _failed_result(
            request=admission.request,
            phase=CompilerPhase.LOWERING,
            diagnostics=(hash_failure,),
            source_document_sha256=admission.parsed_source.source_document_sha256,
            source_sha256=plan.source_sha256,
            harness_id=plan.harness_id,
        )

    return plan


def _compiled_hash_failure(plan: object) -> CompilerDiagnostic | None:
    try:
        payload = plan.model_dump(mode="json")  # type: ignore[attr-defined]
        raw = canonical_json_serialize(payload)
        computed = calculate_compiled_plan_sha256(payload)
        if computed != plan.compiled_sha256:  # type: ignore[attr-defined]
            return _compiled_hash_diagnostic(computed_hash=computed)
        verified, verified_computed, warnings, _restored = verify_compiled_plan_sha256(
            raw,
            expected_compiled_hash=plan.compiled_sha256,  # type: ignore[attr-defined]
            expected_harness_id=plan.harness_id,  # type: ignore[attr-defined]
            expected_harness_version=plan.harness_version,  # type: ignore[attr-defined]
        )
    except Exception as exc:
        return _compiled_hash_diagnostic(error_type=type(exc).__name__)
    if verified and verified_computed == plan.compiled_sha256:  # type: ignore[attr-defined]
        return None
    computed_hash = verified_computed if verified_computed else computed
    return _compiled_hash_diagnostic(
        warning_count=len(warnings),
        computed_hash=computed_hash,
    )


def _compiled_hash_diagnostic(
    *,
    warning_count: int | None = None,
    computed_hash: str | None = None,
    error_type: str | None = None,
) -> CompilerDiagnostic:
    fields: dict[str, str | int] = {}
    if warning_count is not None:
        fields["warning_count"] = warning_count
    if computed_hash is not None:
        fields["computed_hash"] = computed_hash
    if error_type is not None:
        fields["error_type"] = error_type
    return _diagnostic(
        "MF-L004",
        CompilerPhase.LOWERING,
        "Compiled plan canonical hash verification failed.",
        fields=fields,
    )


def _failed_result(
    *,
    request: HarnessCompileRequest,
    phase: CompilerPhase,
    diagnostics: tuple[CompilerDiagnostic, ...],
    source_document_sha256: str | None = None,
    source_sha256: str | None = None,
    harness_id: str | None = None,
) -> HarnessCompileResult:
    return HarnessCompileResult(
        request_id=request.request_id,
        status=CompileStatus.FAILED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        failure_phase=phase,
        source_document_sha256=source_document_sha256,
        source_sha256=source_sha256,
        harness_id=harness_id,
        diagnostics=bound_diagnostics(diagnostics),
    )


def _lowering_failed_result(
    *,
    request: HarnessCompileRequest,
    source_document_sha256: str,
    resolved: ResolvedHarness,
    code: str,
    message: str,
    error_type: str,
) -> HarnessCompileResult:
    try:
        semantic_hash = source_sha256(resolved)
    except Exception:
        semantic_hash = None
    return _failed_result(
        request=request,
        phase=CompilerPhase.LOWERING,
        diagnostics=(
            _diagnostic(
                code,
                CompilerPhase.LOWERING,
                message,
                fields={"error_type": error_type},
            ),
        ),
        source_document_sha256=source_document_sha256,
        source_sha256=semantic_hash,
        harness_id=resolved.source.harness_id,
    )


def _failure_phase(diagnostics: tuple[CompilerDiagnostic, ...]) -> CompilerPhase:
    return diagnostics[0].phase if diagnostics else CompilerPhase.INTERNAL


def _diagnostic(
    code: str,
    phase: CompilerPhase,
    message: str,
    *,
    fields: Mapping[str, str | int] | None = None,
) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        phase=phase,
        message=message,
        fields=tuple(
            DiagnosticField(key=key, value=value)
            for key, value in sorted((fields or {}).items())
        ),
    )
