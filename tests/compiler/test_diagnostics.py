"""Tests for compiler diagnostics and secret detection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from millforge.compiler import (
    DIAGNOSTIC_REGISTRY,
    DIAGNOSTIC_TRIGGER_MEANINGS,
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticField,
    DiagnosticSeverity,
    SourceLocation,
    SourceReference,
    detect_secret_candidate,
    sort_diagnostics,
)
from millforge.contracts import RedactionPolicy


def test_diagnostic_registry_is_unique_and_fixed() -> None:
    assert len(DIAGNOSTIC_REGISTRY) == len(set(DIAGNOSTIC_REGISTRY))
    assert set(DIAGNOSTIC_REGISTRY) == {
        "MF-S001",
        "MF-S002",
        "MF-S003",
        "MF-S004",
        "MF-S005",
        "MF-S006",
        "MF-S007",
        "MF-S008",
        "MF-S009",
        "MF-S010",
        "MF-S011",
        "MF-S012",
        "MF-S013",
        "MF-S014",
        "MF-S015",
        "MF-S016",
        "MF-S017",
        "MF-S018",
        "MF-S020",
        "MF-S021",
        "MF-S022",
        "MF-S023",
        "MF-S024",
        "MF-S025",
        "MF-S026",
        "MF-S027",
        "MF-S028",
        "MF-S029",
        "MF-R001",
        "MF-R002",
        "MF-R003",
        "MF-R004",
        "MF-R005",
        "MF-R006",
        "MF-R007",
        "MF-R008",
        "MF-R009",
        "MF-R010",
        "MF-R011",
        "MF-G001",
        "MF-G002",
        "MF-G003",
        "MF-G004",
        "MF-G005",
        "MF-G006",
        "MF-G007",
        "MF-G008",
        "MF-G009",
        "MF-G010",
        "MF-G011",
        "MF-G012",
        "MF-G013",
        "MF-C001",
        "MF-A001",
        "MF-A002",
        "MF-A003",
        "MF-A004",
        "MF-A005",
        "MF-A006",
        "MF-A007",
        "MF-L001",
        "MF-L002",
        "MF-L003",
        "MF-L004",
        "MF-O001",
        "MF-O002",
        "MF-O003",
        "MF-O004",
        "MF-O005",
        "MF-I001",
        "MF-D001",
    }
    assert DIAGNOSTIC_REGISTRY["MF-S018"] == (
        CompilerPhase.REQUEST,
        DiagnosticSeverity.ERROR,
    )
    assert DIAGNOSTIC_REGISTRY["MF-S020"] == (
        CompilerPhase.SCHEMA,
        DiagnosticSeverity.ERROR,
    )
    assert DIAGNOSTIC_REGISTRY["MF-R009"] == (
        CompilerPhase.RESOLUTION,
        DiagnosticSeverity.ERROR,
    )
    assert DIAGNOSTIC_REGISTRY["MF-C001"] == (
        CompilerPhase.CAPABILITY,
        DiagnosticSeverity.ERROR,
    )
    assert DIAGNOSTIC_REGISTRY["MF-A007"] == (
        CompilerPhase.ARTIFACT,
        DiagnosticSeverity.ERROR,
    )
    assert DIAGNOSTIC_REGISTRY["MF-L001"] == (
        CompilerPhase.LOWERING,
        DiagnosticSeverity.ERROR,
    )
    assert DIAGNOSTIC_REGISTRY["MF-O005"] == (
        CompilerPhase.OUTPUT,
        DiagnosticSeverity.ERROR,
    )
    assert DIAGNOSTIC_REGISTRY["MF-I001"] == (
        CompilerPhase.INTERNAL,
        DiagnosticSeverity.ERROR,
    )
    assert DIAGNOSTIC_REGISTRY["MF-D001"] == (
        CompilerPhase.INTERNAL,
        DiagnosticSeverity.WARNING,
    )


def test_semantic_diagnostic_code_table_matches_03b_trigger_meanings() -> None:
    expected = {
        "MF-R001": ("unknown-model-profile", CompilerPhase.RESOLUTION),
        "MF-R002": ("unknown-tool-reference", CompilerPhase.RESOLUTION),
        "MF-R003": ("catalog-entry-identity-mismatch", CompilerPhase.RESOLUTION),
        "MF-R004": ("malformed-catalog-entry", CompilerPhase.RESOLUTION),
        "MF-R005": ("duplicate-tool-binding", CompilerPhase.RESOLUTION),
        "MF-R006": ("duplicate-model-tool-name", CompilerPhase.RESOLUTION),
        "MF-R007": ("unsupported-tool-schema", CompilerPhase.RESOLUTION),
        "MF-R008": ("catalog-snapshot-drift", CompilerPhase.RESOLUTION),
        "MF-R009": ("catalog-internal-failure", CompilerPhase.RESOLUTION),
        "MF-R010": ("malformed-model-profile", CompilerPhase.RESOLUTION),
        "MF-R011": ("invalid-tool-reference", CompilerPhase.RESOLUTION),
        "MF-G001": ("unknown-prerequisite-node", CompilerPhase.GRAPH),
        "MF-G002": ("self-prerequisite", CompilerPhase.GRAPH),
        "MF-G003": ("duplicate-prerequisite", CompilerPhase.GRAPH),
        "MF-G004": ("prerequisite-cycle", CompilerPhase.GRAPH),
        "MF-G005": ("unreachable-node", CompilerPhase.GRAPH),
        "MF-G006": ("unreachable-terminal", CompilerPhase.GRAPH),
        "MF-G007": ("unreachable-required-node", CompilerPhase.GRAPH),
        "MF-G008": ("terminal-node-required", CompilerPhase.GRAPH),
        "MF-G009": ("terminal-result-illegal", CompilerPhase.GRAPH),
        "MF-G010": ("terminal-result-duplicate", CompilerPhase.GRAPH),
        "MF-G011": ("terminal-missing", CompilerPhase.GRAPH),
        "MF-G012": ("invalid-argument-match", CompilerPhase.GRAPH),
        "MF-G013": ("terminal-prerequisite", CompilerPhase.GRAPH),
        "MF-C001": ("capability-envelope-mismatch", CompilerPhase.CAPABILITY),
        "MF-A001": ("undeclared-produced-artifact", CompilerPhase.ARTIFACT),
        "MF-A002": ("tool-cannot-produce-artifact", CompilerPhase.ARTIFACT),
        "MF-A003": ("unknown-terminal-artifact-policy", CompilerPhase.ARTIFACT),
        "MF-A004": ("required-artifact-without-producer", CompilerPhase.ARTIFACT),
        "MF-A005": ("required-artifact-not-terminal-gated", CompilerPhase.ARTIFACT),
        "MF-A006": ("duplicate-artifact-id", CompilerPhase.ARTIFACT),
        "MF-A007": (
            "undeclared-terminal-required-artifact",
            CompilerPhase.ARTIFACT,
        ),
        "MF-L001": ("lowering-invariant-failed", CompilerPhase.LOWERING),
        "MF-L002": ("compiled-plan-validation-failed", CompilerPhase.LOWERING),
        "MF-L003": ("source-semantic-hash-failed", CompilerPhase.LOWERING),
        "MF-L004": ("compiled-hash-verification-failed", CompilerPhase.LOWERING),
        "MF-O001": ("output-path-invalid", CompilerPhase.OUTPUT),
        "MF-O002": ("diagnostics-write-failed", CompilerPhase.OUTPUT),
        "MF-O003": ("plan-write-failed", CompilerPhase.OUTPUT),
        "MF-O004": ("existing-output-integrity-failed", CompilerPhase.OUTPUT),
        "MF-O005": ("temporary-output-cleanup-failed", CompilerPhase.OUTPUT),
        "MF-I001": ("compiler-internal-error", CompilerPhase.INTERNAL),
    }

    assert DIAGNOSTIC_TRIGGER_MEANINGS == {
        code: meaning for code, (meaning, _phase) in expected.items()
    }
    for code, (_meaning, phase) in expected.items():
        assert DIAGNOSTIC_REGISTRY[code] == (phase, DiagnosticSeverity.ERROR)


def test_03c_lowering_internal_and_output_trigger_meanings_match_root_source() -> None:
    assert {
        code: DIAGNOSTIC_TRIGGER_MEANINGS[code]
        for code in ("MF-L001", "MF-L002", "MF-L003", "MF-L004", "MF-I001")
    } == {
        "MF-L001": "lowering-invariant-failed",
        "MF-L002": "compiled-plan-validation-failed",
        "MF-L003": "source-semantic-hash-failed",
        "MF-L004": "compiled-hash-verification-failed",
        "MF-I001": "compiler-internal-error",
    }
    assert {
        code: DIAGNOSTIC_REGISTRY[code]
        for code in ("MF-L001", "MF-L002", "MF-L003", "MF-L004", "MF-I001")
    } == {
        "MF-L001": (CompilerPhase.LOWERING, DiagnosticSeverity.ERROR),
        "MF-L002": (CompilerPhase.LOWERING, DiagnosticSeverity.ERROR),
        "MF-L003": (CompilerPhase.LOWERING, DiagnosticSeverity.ERROR),
        "MF-L004": (CompilerPhase.LOWERING, DiagnosticSeverity.ERROR),
        "MF-I001": (CompilerPhase.INTERNAL, DiagnosticSeverity.ERROR),
    }
    assert {
        code: DIAGNOSTIC_TRIGGER_MEANINGS[code]
        for code in ("MF-O001", "MF-O002", "MF-O003", "MF-O004", "MF-O005")
    } == {
        "MF-O001": "output-path-invalid",
        "MF-O002": "diagnostics-write-failed",
        "MF-O003": "plan-write-failed",
        "MF-O004": "existing-output-integrity-failed",
        "MF-O005": "temporary-output-cleanup-failed",
    }


def test_compiler_diagnostic_rejects_dynamic_phase_or_severity() -> None:
    with pytest.raises(ValidationError):
        CompilerDiagnostic(
            code="MF-S020",
            phase=CompilerPhase.REQUEST,
            severity=DiagnosticSeverity.ERROR,
            message="Invalid source schema.",
        )


def test_diagnostics_are_sorted_by_contract_key() -> None:
    later = CompilerDiagnostic(
        code="MF-S020",
        phase=CompilerPhase.SCHEMA,
        severity=DiagnosticSeverity.ERROR,
        message="Second.",
        source_reference=SourceReference(
            logical_path="harness.yaml",
            field_path="/graph",
            location=SourceLocation(line=5, column=1),
        ),
    )
    earlier = CompilerDiagnostic(
        code="MF-S006",
        phase=CompilerPhase.PARSE,
        severity=DiagnosticSeverity.ERROR,
        message="First.",
        source_reference=SourceReference(
            logical_path="harness.yaml",
            field_path="/graph/nodes",
            location=SourceLocation(line=10, column=1),
        ),
    )

    assert sort_diagnostics((later, earlier)) == (earlier, later)


def test_03c_diagnostics_sort_by_fixed_phase_and_internal_precedence() -> None:
    internal = CompilerDiagnostic(
        code="MF-I001",
        phase=CompilerPhase.INTERNAL,
        severity=DiagnosticSeverity.ERROR,
        message="Compiler failed internally.",
    )
    truncation = CompilerDiagnostic(
        code="MF-D001",
        phase=CompilerPhase.INTERNAL,
        severity=DiagnosticSeverity.WARNING,
        message="Diagnostics were truncated.",
    )
    output = CompilerDiagnostic(
        code="MF-O001",
        phase=CompilerPhase.OUTPUT,
        severity=DiagnosticSeverity.ERROR,
        message="Diagnostics write failed.",
    )
    lowering = CompilerDiagnostic(
        code="MF-L001",
        phase=CompilerPhase.LOWERING,
        severity=DiagnosticSeverity.ERROR,
        message="Lowering failed.",
    )

    assert sort_diagnostics((truncation, output, internal, lowering)) == (
        lowering,
        output,
        internal,
        truncation,
    )


def test_diagnostic_fields_are_bounded_and_strict() -> None:
    field = DiagnosticField(key="source_size", value=128)
    assert field.value == 128

    with pytest.raises(ValidationError):
        DiagnosticField(key="SourceSize", value=128)

    truncated = DiagnosticField(key="detail", value="x" * 600)
    assert isinstance(truncated.value, str)
    assert len(truncated.value.encode("utf-8")) <= 512
    assert truncated.value.endswith("[truncated]")


def test_diagnostic_public_string_and_repr_redact_sensitive_values() -> None:
    raw_secret = "sk-" + "test-" + "secret-" + "secret"
    field = DiagnosticField(key="api_key", value=raw_secret)
    diagnostic = CompilerDiagnostic(
        code="MF-S001",
        phase=CompilerPhase.REQUEST,
        severity=DiagnosticSeverity.ERROR,
        message=f"Request rejected with bearer {raw_secret}.",
        suggested_fix=f"Remove token={raw_secret}.",
        fields=(field,),
    )

    assert field.value == "**redacted**"
    assert raw_secret not in repr(field)
    assert raw_secret not in str(field)
    assert raw_secret not in repr(diagnostic)
    assert raw_secret not in str(diagnostic)
    assert raw_secret not in diagnostic.model_dump_json()


def test_secret_detection_uses_redaction_markers_and_fixed_patterns() -> None:
    policy = RedactionPolicy()

    assert detect_secret_candidate(
        field_path="/prompt/api_key",
        field_name="value",
        value="not-empty",
        policy=policy,
    )
    assert detect_secret_candidate(
        field_path="/prompt/system_instructions",
        field_name="system_instructions",
        value="Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        policy=policy,
    )
    assert not detect_secret_candidate(
        field_path="/prompt/system_instructions",
        field_name="system_instructions",
        value="Use the admitted tools.",
        policy=policy,
    )
