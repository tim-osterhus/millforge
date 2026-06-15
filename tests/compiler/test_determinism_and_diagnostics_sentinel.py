"""Determinism and diagnostics sentinel campaign tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from millforge import (
    CapabilityGrant,
    redact_diagnostic_mapping,
)
from millforge.compiler import (
    CompileStatus,
    CompilerDiagnostic,
    CompilerPhase,
    DiagnosticField,
    DiagnosticSeverity,
    HarnessCompileRequest,
    compile as compile_harness,
)
from millforge.compiler.diagnostics import (
    MAX_DIAGNOSTICS_WITHOUT_TRUNCATION_WARNING,
    bound_diagnostics,
)
from millforge.contracts import CapabilityEnvelope
from millforge.exceptions import MillforgeError
from tests.compiler.conftest import (
    make_golden_model_profile_catalog_snapshot,
    make_golden_tool_catalog_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parents[2]
PYTHONPATH = os.pathsep.join((str(REPO_ROOT / "src"), str(REPO_ROOT)))


@dataclass(frozen=True)
class CampaignFixture:
    name: str
    yaml_filename: str
    json_filename: str
    expected_harness_id: str
    legal_terminal_results: tuple[str, ...]


class CampaignRow(TypedDict):
    fixture: str
    variant: str
    status: str
    source_document_sha256: str | None
    source_sha256: str | None
    compiled_sha256: str | None
    compiled_plan_bytes_sha256: str
    diagnostic_codes: list[str]


CAMPAIGN_FIXTURES = (
    CampaignFixture(
        name="full",
        yaml_filename="golden_harness.yaml",
        json_filename="golden_harness.json",
        expected_harness_id="millforge.test.golden.compiler.v1",
        legal_terminal_results=("BLOCKED", "BUILDER_COMPLETE"),
    ),
    CampaignFixture(
        name="simple_success",
        yaml_filename="representative_simple_success.yaml",
        json_filename="representative_simple_success.json",
        expected_harness_id="millforge.test.representative.simple.v1",
        legal_terminal_results=("BUILDER_COMPLETE",),
    ),
    CampaignFixture(
        name="blocked_artifact",
        yaml_filename="representative_blocked_artifact.yaml",
        json_filename="representative_blocked_artifact.json",
        expected_harness_id="millforge.test.representative.blocked.v1",
        legal_terminal_results=("BLOCKED",),
    ),
)

SENTINELS = (
    "sk-live-diagnostics-sentinel-000000",
    "Bearer diagnostics-sentinel-token-000000",
    "X-API-Key: diagnostics-sentinel-header-000000",
    "https://diag-user:diag-pass@diagnostics.example.invalid/v1?api_key=diag-query",
    "AWS_SECRET_ACCESS_KEY=diagnostics-sentinel-env-000000",
    "provider payload token=diagnostics-sentinel-provider-000000",
    "tool argument password=diagnostics-sentinel-tool-000000",
    "exception cause secret=diagnostics-sentinel-exception-000000",
)


def test_determinism_campaign_across_fixture_and_environment_variants(
    tmp_path: Path,
) -> None:
    rows: list[CampaignRow] = []

    for case in CAMPAIGN_FIXTURES:
        baseline = _compile_variant(
            tmp_path,
            case,
            variant_name="baseline-yaml-lf",
            content=(FIXTURES / case.yaml_filename).read_bytes(),
            source_format="yaml",
            suffix=".yaml",
        )
        rows.append(baseline)
        baseline_document_hash = baseline["source_document_sha256"]
        baseline_semantic = _semantic_fingerprint(baseline)

        for variant_name, content, source_format, suffix in _document_variants(case):
            row = _compile_variant(
                tmp_path,
                case,
                variant_name=variant_name,
                content=content,
                source_format=source_format,
                suffix=suffix,
            )
            rows.append(row)
            assert _semantic_fingerprint(row) == baseline_semantic
            if variant_name == "json-canonical-lf":
                assert row["source_document_sha256"] != baseline_document_hash

        subprocess_rows = _subprocess_campaign_rows(tmp_path, case)
        rows.extend(subprocess_rows)
        for row in subprocess_rows:
            assert _semantic_fingerprint(row) == baseline_semantic

    assert len(rows) == 3 * (1 + 8 + 4)
    assert {row["status"] for row in rows} == {"committed"}
    assert {tuple(row["diagnostic_codes"]) for row in rows} == {()}


def test_diagnostics_sentinel_corpus_is_redacted_bounded_and_deterministic() -> None:
    cyclic: dict[str, object] = {"api_key": SENTINELS[0]}
    cyclic["self"] = cyclic

    class HostileRepr:
        def __repr__(self) -> str:
            raise AssertionError(SENTINELS[0])

    provider_error = RuntimeError(SENTINELS[-1])
    provider_error.__cause__ = ValueError(SENTINELS[5])
    raw_mapping = {
        "authorization": SENTINELS[1],
        "headers": {"x-api-key": SENTINELS[2]},
        "credential_url": SENTINELS[3],
        "env": SENTINELS[4],
        "provider_error": provider_error,
        "tool_arguments": {"password": SENTINELS[6]},
        "nested": [SENTINELS[0], {"token": SENTINELS[1]}, cyclic],
        "long_scalar": "prefix " + SENTINELS[5] + " " + ("x" * 5000),
        "hostile": HostileRepr(),
    }
    redacted_mapping = redact_diagnostic_mapping(raw_mapping, secret_values=SENTINELS)
    owned_exception = MillforgeError(
        f"provider failed with {SENTINELS[5]}",
        cause=provider_error,
    )
    diagnostic = CompilerDiagnostic(
        code="MF-I001",
        phase=CompilerPhase.INTERNAL,
        severity=DiagnosticSeverity.ERROR,
        message=f"Provider failed with {SENTINELS[1]}",
        suggested_fix=f"Remove {SENTINELS[4]} before retrying.",
        fields=(
            DiagnosticField(key="api_key", value=SENTINELS[0]),
            DiagnosticField(key="authorization", value=SENTINELS[1]),
            DiagnosticField(key="credential_url", value=SENTINELS[3]),
            DiagnosticField(key="provider_error", value=SENTINELS[5]),
            DiagnosticField(key="tool_secret", value=SENTINELS[6]),
            DiagnosticField(key="detail", value="x" * 5000),
        ),
    )
    compile_result = _failed_compile_result_for_scan(diagnostic)
    report_json = compile_result.model_dump_json()
    scan_surfaces = (
        repr(redacted_mapping),
        json.dumps(redacted_mapping, sort_keys=True),
        repr(diagnostic),
        str(diagnostic),
        diagnostic.model_dump_json(),
        repr(compile_result),
        str(compile_result),
        report_json,
        str(owned_exception),
        repr(owned_exception),
    )

    for surface in scan_surfaces:
        assert_no_sentinel_leak(surface)
    assert "diagnostics.example.invalid" not in report_json
    assert "https://" not in report_json
    assert "HostileRepr" in repr(redacted_mapping)


def test_diagnostic_truncation_warning_is_reserved_and_stable() -> None:
    diagnostics = tuple(
        CompilerDiagnostic(
            code="MF-S020",
            phase=CompilerPhase.SCHEMA,
            severity=DiagnosticSeverity.ERROR,
            message=f"Invalid source schema row {index:03d}.",
        )
        for index in range(MAX_DIAGNOSTICS_WITHOUT_TRUNCATION_WARNING + 20)
    )

    first = bound_diagnostics(diagnostics)
    second = bound_diagnostics(tuple(reversed(diagnostics)))

    assert first == second
    assert len(first) == MAX_DIAGNOSTICS_WITHOUT_TRUNCATION_WARNING + 1
    assert first[-1].code == "MF-D001"
    assert first[-1].message == "Diagnostics were truncated."


def assert_no_sentinel_leak(surface: str) -> None:
    for sentinel in SENTINELS:
        assert sentinel not in surface


def _compile_variant(
    tmp_path: Path,
    case: CampaignFixture,
    *,
    variant_name: str,
    content: bytes,
    source_format: Literal["yaml", "json"],
    suffix: str,
) -> CampaignRow:
    root = tmp_path / case.name / variant_name / "source"
    output_root = tmp_path / case.name / variant_name / "output"
    root.mkdir(parents=True)
    output_root.mkdir(parents=True)
    (output_root / "compiled").mkdir()
    source_path = f"{case.name}-{variant_name}{suffix}"
    (root / source_path).write_bytes(content)
    request = _request(
        case,
        source_path=source_path,
        source_root=root,
        output_root=output_root,
        source_format=source_format,
        request_id=_request_id(case.name, variant_name),
        reverse_grants="capability-order" in variant_name,
    )
    result = compile_harness(
        request,
        tool_catalog=make_golden_tool_catalog_snapshot(),
        model_profile_catalog=make_golden_model_profile_catalog_snapshot(),
    )
    assert result.status == CompileStatus.COMMITTED
    assert result.compiled_plan_path is not None
    plan_bytes = Path(output_root, result.compiled_plan_path).read_bytes()
    return _campaign_row(case.name, variant_name, result, plan_bytes)


def _subprocess_campaign_rows(
    tmp_path: Path, case: CampaignFixture
) -> list[CampaignRow]:
    rows: list[CampaignRow] = []
    for seed, locale, timezone, catalog_order in (
        ("0", "C", "UTC", "normal"),
        ("123", "C.UTF-8", "Pacific/Honolulu", "reverse"),
        ("0", "C.UTF-8", "UTC", "reverse"),
        ("123", "C", "Pacific/Honolulu", "normal"),
    ):
        variant_name = (
            f"subprocess-seed-{seed}-{catalog_order}-{timezone.replace('/', '_')}"
        )
        root = tmp_path / case.name / variant_name / "source"
        output_root = tmp_path / case.name / variant_name / "output"
        cwd = tmp_path / case.name / variant_name / "cwd"
        root.mkdir(parents=True)
        output_root.mkdir(parents=True)
        (output_root / "compiled").mkdir()
        cwd.mkdir(parents=True)
        source_path = f"{case.name}-{variant_name}.json"
        source = _json_bytes(_ordered_source_payload(case, "node-order"))
        (root / source_path).write_bytes(source)
        env = os.environ.copy()
        env.update(
            {
                "PYTHONHASHSEED": seed,
                "LC_ALL": locale,
                "TZ": timezone,
                "PYTHONPATH": PYTHONPATH,
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _SUBPROCESS_COMPILE_SCRIPT,
                json.dumps(
                    {
                        "case": case.name,
                        "variant": variant_name,
                        "expected_harness_id": case.expected_harness_id,
                        "legal_terminal_results": case.legal_terminal_results,
                        "source_root": str(root),
                        "source_path": source_path,
                        "output_root": str(output_root),
                        "catalog_order": catalog_order,
                    }
                ),
            ],
            check=True,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
        )
        rows.append(json.loads(completed.stdout))
    return rows


def _request(
    case: CampaignFixture,
    *,
    source_path: str,
    source_root: Path,
    output_root: Path,
    source_format: Literal["yaml", "json"],
    request_id: str,
    reverse_grants: bool = False,
) -> HarnessCompileRequest:
    grants: tuple[CapabilityGrant, ...] = (
        CapabilityGrant(capability_id="artifact.write"),
        CapabilityGrant(capability_id="diagnostics.write"),
        CapabilityGrant(capability_id="evidence.emit"),
        CapabilityGrant(capability_id="workspace.read"),
    )
    if reverse_grants:
        grants = tuple(reversed(grants))
    return HarnessCompileRequest(
        request_id=request_id,
        source_path=source_path,
        source_root=str(source_root),
        source_format=source_format,
        output_dir="compiled",
        output_root=str(output_root),
        expected_harness_id=case.expected_harness_id,
        stage_kind_id="builder",
        legal_terminal_results=case.legal_terminal_results,
        capability_envelope=CapabilityEnvelope(grants=grants),
    )


def _request_id(case_name: str, variant_name: str) -> str:
    safe_variant = variant_name.lower().replace("-", ".").replace("_", ".")
    return f"request.{case_name}.{safe_variant}"


def _document_variants(
    case: CampaignFixture,
) -> tuple[tuple[str, bytes, Literal["yaml", "json"], str], ...]:
    yaml_lf = (FIXTURES / case.yaml_filename).read_bytes()
    parsed = _json_fixture_payload(case)
    return (
        ("yaml-crlf", yaml_lf.replace(b"\n", b"\r\n"), "yaml", ".yaml"),
        ("yaml-comments", _yaml_with_comments(yaml_lf), "yaml", ".yaml"),
        (
            "json-canonical-lf",
            (FIXTURES / case.json_filename).read_bytes(),
            "json",
            ".json",
        ),
        (
            "json-crlf",
            (FIXTURES / case.json_filename).read_bytes().replace(b"\n", b"\r\n"),
            "json",
            ".json",
        ),
        (
            "json-mapping-order",
            _json_bytes(_reordered_top_level(parsed)),
            "json",
            ".json",
        ),
        (
            "json-node-order",
            _json_bytes(_ordered_source_payload(case, "node-order")),
            "json",
            ".json",
        ),
        (
            "json-artifact-capability-order",
            _json_bytes(_ordered_source_payload(case, "artifact-capability-order")),
            "json",
            ".json",
        ),
        (
            "json-prerequisite-order",
            _json_bytes(_ordered_source_payload(case, "prerequisite-order")),
            "json",
            ".json",
        ),
    )


def _json_fixture_payload(case: CampaignFixture) -> dict[str, Any]:
    payload = json.loads((FIXTURES / case.json_filename).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _ordered_source_payload(case: CampaignFixture, order_kind: str) -> dict[str, Any]:
    payload = _json_fixture_payload(case)
    if order_kind == "node-order":
        nodes = cast_dict(cast_dict(payload["graph"])["nodes"])
        cast_dict(payload["graph"])["nodes"] = {
            key: nodes[key] for key in sorted(nodes, reverse=True)
        }
    elif order_kind == "artifact-capability-order":
        artifacts = cast_dict(payload["artifacts"])
        artifacts["declared_artifact_ids"] = sorted(
            cast_list(artifacts["declared_artifact_ids"]), reverse=True
        )
        required_by_terminal = cast_dict(artifacts["required_by_terminal"])
        artifacts["required_by_terminal"] = {
            key: sorted(cast_list(value), reverse=True)
            for key, value in sorted(required_by_terminal.items(), reverse=True)
        }
    elif order_kind == "prerequisite-order":
        for node in cast_dict(cast_dict(payload["graph"])["nodes"]).values():
            node_map = cast_dict(node)
            if "prerequisites" in node_map:
                node_map["prerequisites"] = list(
                    reversed(cast_list(node_map["prerequisites"]))
                )
    return payload


def _reordered_top_level(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in sorted(payload, reverse=True)}


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=False).encode("utf-8") + b"\n"


def _yaml_with_comments(content: bytes) -> bytes:
    text = content.decode("utf-8")
    lines = ["# Determinism campaign comment"]
    for line in text.splitlines():
        lines.append(line)
        if line.strip() == "graph:":
            lines.append("  # Node order comment")
        if line.strip() == "artifacts:":
            lines.append("  # Artifact order comment")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _semantic_fingerprint(row: CampaignRow) -> tuple[object, ...]:
    return (
        row["source_sha256"],
        row["compiled_sha256"],
        row["compiled_plan_bytes_sha256"],
        tuple(row["diagnostic_codes"]),
    )


def _campaign_row(
    fixture_name: str,
    variant_name: str,
    result: Any,
    plan_bytes: bytes,
) -> CampaignRow:
    return {
        "fixture": fixture_name,
        "variant": variant_name,
        "status": result.status.value,
        "source_document_sha256": result.source_document_sha256,
        "source_sha256": result.source_sha256,
        "compiled_sha256": result.compiled_sha256,
        "compiled_plan_bytes_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "diagnostic_codes": [diagnostic.code for diagnostic in result.diagnostics],
    }


def _failed_compile_result_for_scan(diagnostic: CompilerDiagnostic) -> Any:
    from millforge.compiler.requests import (
        CompileStatus,
        DiagnosticReportState,
        HarnessCompileResult,
        PlanCommitCertainty,
    )

    return HarnessCompileResult(
        request_id="request.diagnostics.sentinel",
        status=CompileStatus.FAILED,
        plan_commit_certainty=PlanCommitCertainty.ABSENT,
        diagnostic_report_state=DiagnosticReportState.ABSENT,
        failure_phase=CompilerPhase.INTERNAL,
        diagnostics=(diagnostic,),
    )


def cast_dict(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return value


def cast_list(value: object) -> list[Any]:
    assert isinstance(value, list)
    return value


_SUBPROCESS_COMPILE_SCRIPT = r"""
import hashlib
import json
import sys
from pathlib import Path

from millforge import CapabilityEnvelope, CapabilityGrant, canonical_compiled_plan_bytes
from millforge.compiler import HarnessCompileRequest, compile as compile_harness
from tests.compiler.conftest import (
    StaticModelProfileCatalogSnapshot,
    StaticToolCatalogSnapshot,
    make_golden_model_profile_catalog_snapshot,
    make_golden_tool_catalog_snapshot,
)

config = json.loads(sys.argv[1])
base_tool_catalog = make_golden_tool_catalog_snapshot()
base_model_catalog = make_golden_model_profile_catalog_snapshot()
if config["catalog_order"] == "reverse":
    tool_catalog = StaticToolCatalogSnapshot(
        entries=dict(reversed(tuple(base_tool_catalog._entries.items()))),
        snapshot_id=base_tool_catalog.snapshot_id,
        snapshot_sha256=base_tool_catalog.snapshot_sha256,
    )
    model_catalog = StaticModelProfileCatalogSnapshot(
        profiles=dict(reversed(tuple(base_model_catalog._profiles.items()))),
        snapshot_id=base_model_catalog.snapshot_id,
        snapshot_sha256=base_model_catalog.snapshot_sha256,
    )
else:
    tool_catalog = base_tool_catalog
    model_catalog = base_model_catalog
request = HarnessCompileRequest(
    request_id=(
        "request."
        + config["case"]
        + "."
        + config["variant"].lower().replace("-", ".").replace("_", ".")
    ),
    source_path=config["source_path"],
    source_root=config["source_root"],
    source_format="json",
    output_dir="compiled",
    output_root=config["output_root"],
    expected_harness_id=config["expected_harness_id"],
    stage_kind_id="builder",
    legal_terminal_results=tuple(config["legal_terminal_results"]),
    capability_envelope=CapabilityEnvelope(
        grants=(
            CapabilityGrant(capability_id="artifact.write"),
            CapabilityGrant(capability_id="diagnostics.write"),
            CapabilityGrant(capability_id="evidence.emit"),
            CapabilityGrant(capability_id="workspace.read"),
        )
    ),
)
result = compile_harness(
    request,
    tool_catalog=tool_catalog,
    model_profile_catalog=model_catalog,
)
if result.compiled_plan_path is None:
    plan_bytes = canonical_compiled_plan_bytes({"failed": True})
else:
    plan_bytes = Path(config["output_root"], result.compiled_plan_path).read_bytes()
print(json.dumps({
    "fixture": config["case"],
    "variant": config["variant"],
    "status": result.status.value,
    "source_document_sha256": result.source_document_sha256,
    "source_sha256": result.source_sha256,
    "compiled_sha256": result.compiled_sha256,
    "compiled_plan_bytes_sha256": hashlib.sha256(plan_bytes).hexdigest(),
    "diagnostic_codes": [diagnostic.code for diagnostic in result.diagnostics],
}, sort_keys=True))
"""
