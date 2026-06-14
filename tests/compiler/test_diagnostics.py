"""Tests for compiler diagnostics and secret detection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from millforge.compiler import (
    DIAGNOSTIC_REGISTRY,
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
    assert DIAGNOSTIC_REGISTRY["MF-D001"] == (
        CompilerPhase.INTERNAL,
        DiagnosticSeverity.WARNING,
    )


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
