from __future__ import annotations

import ast
import importlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from millforge import CapabilityEnvelope, CapabilityGrant, CompiledModelProfile
from millforge.compiler import (
    CatalogLookupClassification,
    CompileInvocation,
    CompileStatus,
    HarnessCompileRequest,
    HarnessSource,
    ModelProfileCatalogLookup,
    ToolCatalogSnapshot,
    compile as compile_harness,
    compile_semantic,
    lower_resolved_harness,
)
from millforge.connectors import (
    ConnectorAdmissionManifest,
    ConnectorAdmissionPolicy,
    ConnectorApprovalPolicy,
    ConnectorDiscoverySnapshot,
    admit_connector_tools,
)
from millforge.custom_tools import (
    CustomToolApprovalPolicy,
    CustomToolCompilerPolicy,
    CustomToolSourceManifest,
    compile_custom_tools,
)
from millforge.tools import ToolRegistry, iter_builtin_tool_descriptors

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "tests" / "fixtures" / "spec05_conformance_matrix.json"
CONNECTOR_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "connectors"
CUSTOM_TOOL_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "custom_tools"
MIXED_HARNESS_ROOT = ROOT / "tests" / "fixtures" / "spec05_mixed_harness"

MIXED_TOOL_IDS = {
    "builtin.terminal.submit",
    "connector.fake_mcp.list_context",
    "connector.fake_mcp.echo",
    "custom.echo",
    "custom.list",
}
MIXED_CAPABILITY_IDS = (
    "terminal.intent",
    "cap.connector.echo",
    "cap.connector.list_context",
    "cap.custom.echo",
    "cap.custom.list",
)

ALLOWED_STATUSES = {"implemented", "deferred", "not_applicable"}
REQUIRED_ROW_FIELDS = {
    "requirement_id",
    "requirement_summary",
    "source_specs",
    "implemented_by_files",
    "covered_by_tests",
    "fixture_paths",
    "evidence_commands",
    "accepted_evidence_paths",
    "status",
    "notes",
}
DEFERRED_ROW_FIELDS = REQUIRED_ROW_FIELDS | {"deferred_scope"}
NOT_APPLICABLE_REQUIREMENT_IDS: frozenset[str] = frozenset()
DEFERRED_SCOPE_BOUNDARY_TERMS = (
    "future-only",
    "real connector transport",
    "marketplace installation",
    "automatic discovery",
    "custom runtime execution",
    "production presets",
    "millrace runner integration",
    "eval workflows",
    "live model/backend validation",
)
ALLOWED_COMMAND_PREFIXES = (
    "python -m pytest",
    "python -m ruff",
    "python -m mypy",
    "python -m pip check",
    "python -m build",
    "python -m zipfile",
    "tar -tzf",
    "git diff",
    "git status",
    "git ls-files",
    "git check-ignore",
)
FORBIDDEN_COMMAND_TERMS = (
    "curl",
    "wget",
    "http://",
    "https://",
    "openai",
    "deepseek",
    "mcp",
    "npm",
    "pip install",
    "millrace daemon",
)
FORBIDDEN_IMPLEMENTED_CLAIMS = (
    "live connector",
    "marketplace",
    "custom runtime execution",
    "production preset",
    "millrace runner",
    "eval workflow",
    "live model",
)
PRIVATE_PREFIXES = ("millrace-agents/", "ideas/", "ref-forge/")
ALLOWED_ROOT_EXPORT_MODULES = {
    "millforge.artifacts",
    "millforge.compiled_plan",
    "millforge.contracts",
    "millforge.custom_tools",
    "millforge.eval_artifacts",
    "millforge.eval_boundary",
    "millforge.eval_modes",
    "millforge.eval_presets",
    "millforge.eval_suite",
    "millforge.eval_workflow",
    "millforge.exceptions",
    "millforge.protocols",
    "millforge.runtime",
    "millforge.tools",
}
EXPECTED_PUBLIC_EXPORTS = {
    "millforge.connectors": {
        "CONNECTOR_ADMISSION_MANIFEST_KIND",
        "CONNECTOR_ADMISSION_MANIFEST_SCHEMA",
        "CONNECTOR_ADMISSION_MANIFEST_VERSION",
        "CONNECTOR_ADMISSION_RECORD_HASH_KIND",
        "CONNECTOR_DISCOVERY_SNAPSHOT_HASH_KIND",
        "CONNECTOR_DISCOVERY_TOOL_HASH_KIND",
        "CONNECTOR_IDENTITY_HASH_KIND",
        "ConnectorAdmissionBinding",
        "ConnectorAdmissionManifest",
        "ConnectorAdmissionPolicy",
        "ConnectorAdmissionRecord",
        "ConnectorAdmissionResult",
        "ConnectorAdmissionSnapshot",
        "ConnectorAdmissionSnapshotError",
        "ConnectorApprovalPolicy",
        "ConnectorBroker",
        "ConnectorBrokerOutcome",
        "ConnectorContractValidation",
        "ConnectorDiagnostic",
        "ConnectorDiagnosticCode",
        "ConnectorDiagnosticEvidence",
        "ConnectorDiagnosticPhase",
        "ConnectorDiagnosticSeverity",
        "ConnectorDiscoverySnapshot",
        "ConnectorIdentity",
        "ConnectorInvocationRequest",
        "ConnectorProtocol",
        "ConnectorProviderToolEvidence",
        "ConnectorToolSelection",
        "ConnectorTransportKind",
        "DeniedConnectorTool",
        "DescriptionPolicy",
        "DeterministicFakeConnectorBroker",
        "DiscoveredProviderTool",
        "ExpectedConnectorIdentity",
        "InputSchemaPolicy",
        "OutputSchemaPolicy",
        "admit_connector_tools",
        "connector_diagnostic",
        "connector_idempotency_key",
        "malformed_input_diagnostic",
    },
    "millforge.custom_tools": {
        "CUSTOM_TOOL_COMPILATION_RECORD_HASH_KIND",
        "CUSTOM_TOOL_DECLARATION_HASH_KIND",
        "CUSTOM_TOOL_SOURCE_HASH_KIND",
        "CUSTOM_TOOL_SOURCE_KIND",
        "CUSTOM_TOOL_SOURCE_SCHEMA",
        "CUSTOM_TOOL_SOURCE_VERSION",
        "CustomToolApprovalPolicy",
        "CustomToolCompilationRecord",
        "CustomToolCompilationResult",
        "CustomToolCompilerPolicy",
        "CustomToolContractModel",
        "CustomToolContractValidation",
        "CustomToolDeclaration",
        "CustomToolDescriptionPolicy",
        "CustomToolDiagnostic",
        "CustomToolDiagnosticCode",
        "CustomToolDiagnosticEvidence",
        "CustomToolDiagnosticPhase",
        "CustomToolDiagnosticSeverity",
        "CustomToolInputPolicy",
        "CustomToolOutputPolicy",
        "CustomToolRuntimeKind",
        "CustomToolSourceManifest",
        "compilation_record_from_declaration",
        "compile_custom_tools",
        "custom_tool_diagnostic",
        "malformed_input_diagnostic",
        "redact_custom_tool_text",
        "tool_descriptor_from_declaration",
    },
    "millforge.eval_suite": {
        "EVAL_SUITE_CAMPAIGN_MANIFEST_HASH_KIND",
        "EVAL_SUITE_DEFAULT_CAMPAIGN_ID",
        "EVAL_SUITE_DEFAULT_CAMPAIGN_CREATED_AT",
        "EVAL_SUITE_DEFAULT_FIXTURE_PACK_ID",
        "EVAL_SUITE_DEFAULT_SCORER_VERSION",
        "EVAL_SUITE_FIXTURE_HASH_KIND",
        "EVAL_SUITE_FIXTURE_PACK_HASH_KIND",
        "EVAL_SUITE_MODEL_MANIFEST_HASH_KIND",
        "EVAL_SUITE_SCHEMA_VERSION",
        "EVAL_SUITE_SCORER_INPUT_HASH_KIND",
        "EVAL_SUITE_SCORER_RESULT_HASH_KIND",
        "EvalBudgetPolicyReference",
        "EvalCampaignKind",
        "EvalCampaignManifest",
        "EvalCapabilityAuditSummary",
        "EvalCheckResult",
        "EvalDifficultyLevel",
        "EvalDifficultyMetadata",
        "EvalExpectedMutationKind",
        "EvalExpectedMutationPolicy",
        "EvalFailureTaxonomyLabel",
        "EvalFixturePackSummary",
        "EvalHashRecord",
        "EvalHiddenCheck",
        "EvalLiveDenialDiagnostic",
        "EvalModelManifest",
        "EvalModelPricingMetadata",
        "EvalModelRateLimitMetadata",
        "EvalPublicArtifactProjection",
        "EvalRunnerAcceptanceProjection",
        "EvalRunnerContextProjection",
        "EvalRunnerTaskProjection",
        "EvalScorerInput",
        "EvalScorerResult",
        "EvalSuiteContractModel",
        "EvalSuiteExecutionMode",
        "EvalTaskCategory",
        "EvalTaskFixture",
        "EvalTrialOutcome",
        "EvalVisibleCheck",
        "calculate_eval_campaign_manifest_hash",
        "calculate_eval_fixture_pack_hash",
        "calculate_eval_model_manifest_hash",
        "calculate_eval_scorer_input_hash",
        "calculate_eval_scorer_result_hash",
        "calculate_eval_task_fixture_hash",
        "canonical_eval_suite_bytes",
        "default_eval_suite_campaign_manifest",
        "eval_model_manifest_from_profile",
        "eval_public_artifact_projection",
        "eval_runner_acceptance_projection",
        "eval_runner_context_projection",
        "eval_runner_task_projection",
        "load_eval_fixture_pack_summary",
        "load_eval_task_fixture",
        "load_eval_task_fixtures",
        "score_eval_trial",
    },
    "millforge.tools": {
        "BUILTIN_CAPABILITY_IDS",
        "BUILTIN_TOOL_DESCRIPTORS",
        "BUILTIN_TOOL_VERSION",
        "CompiledToolBindingExecutor",
        "DESCRIPTOR_HASH_KIND",
        "DESCRIPTOR_SCHEMA_VERSION",
        "FrozenDescriptorHashRecord",
        "FrozenToolRegistrySnapshot",
        "MAX_CANCELLATION_GRACE_SECONDS",
        "MAX_OUTPUT_BYTES",
        "MAX_OUTPUT_SUMMARY_UTF8",
        "MAX_TIMEOUT_SECONDS",
        "RuntimeToolRegistry",
        "SNAPSHOT_ID_KIND",
        "SNAPSHOT_KIND",
        "ToolBindingDenialCode",
        "ToolDescriptor",
        "ToolExecutionErrorCode",
        "ToolOutputPolicy",
        "ToolRegistry",
        "ToolRegistryError",
        "ToolRegistryErrorCode",
        "ToolTimeoutPolicy",
        "create_builtin_runtime_registry",
        "create_builtin_tool_executor",
        "create_builtin_tool_registry",
        "create_builtin_tool_snapshot",
        "create_tool_executor",
        "descriptor_hash_payload",
        "iter_builtin_tool_descriptors",
    },
}
README_DEFERRED_05D_CLAIMS = (
    "live connector transport",
    "marketplace installation",
    "automatic discovery or admission",
    "custom runtime execution",
    "production stage presets",
    "Millrace runner integration",
    "eval-suite execution",
    "live connector execution",
    "live provider/model/tool execution",
    "live model/backend validation",
)
ROADMAP_DEFERRED_05D_CLAIMS = (
    "live connector transport",
    "marketplace installation",
    "automatic discovery or admission",
    "custom runtime execution",
    "Millrace runner integration",
    "eval workflows",
    "live model/backend validation",
)
REQUIRED_ROWS = {
    "05A.discovery_non_authoritative",
    "05A.explicit_admission_required",
    "05A.identity_frozen",
    "05A.discovery_hashing",
    "05A.admission_hashing",
    "05A.schema_subset",
    "05A.description_policy",
    "05A.capability_policy",
    "05A.side_effect_policy",
    "05A.approval_policy",
    "05A.diagnostics",
    "05B.snapshot_binding",
    "05B.snapshot_staleness",
    "05B.broker_required",
    "05B.input_preflight",
    "05B.connector_dispatch_authority",
    "05B.narrow_broker_request",
    "05B.fake_broker_only",
    "05B.identity_drift",
    "05B.capability_drift",
    "05B.approval_gate",
    "05B.operator_out_of_band",
    "05B.output_validation",
    "05B.retry_certainty",
    "05B.timeout_cancellation_retry",
    "05B.per_invocation_revalidation",
    "05B.provider_scope",
    "05B.trace_evidence",
    "05B.no_runtime_authority_bleed",
    "05C.source_contract",
    "05C.compile_only",
    "05C.runtime_kind_closed",
    "05C.descriptor_lowering",
    "05C.record_hashing",
    "05C.schema_subset",
    "05C.source_authority_closed",
    "05C.capability_artifact_policy",
    "05C.approval_policy",
    "05C.description_containment",
    "05C.manifest_atomicity",
    "05C.hash_determinism",
    "05C.missing_implementation",
    "05C.diagnostics",
    "05D.mixed_registry",
    "05D.mixed_catalog",
    "05D.harness_selection",
    "05D.illegal_tool_denial",
    "05D.no_authority_bleed",
    "05D.no_live_claims",
    "05D.public_api_boundary",
    "05D.import_side_effect_boundary",
    "05D.package_boundary",
    "05D.documentation_claim_boundary",
    "05D.deferred_scope_boundary",
}


class StaticMixedModelProfileSnapshot:
    snapshot_id = "b" * 64
    snapshot_sha256 = "c" * 64

    def resolve_exact(self, profile_id: str) -> ModelProfileCatalogLookup:
        if profile_id != "profile.mixed":
            return ModelProfileCatalogLookup.missing(error_code="profile.missing")
        return ModelProfileCatalogLookup.found(
            CompiledModelProfile(profile_id="profile.mixed")
        )


def test_mixed_closure_fixtures_compile_through_generic_registry_catalog_and_lowering(
    tmp_path: Path,
) -> None:
    snapshot = ConnectorDiscoverySnapshot.model_validate(
        _load_json(CONNECTOR_FIXTURE_ROOT / "valid/closure_discovery_snapshot.json")
    )
    manifest = ConnectorAdmissionManifest.model_validate(
        _load_json(CONNECTOR_FIXTURE_ROOT / "valid/closure_admission_manifest.json")
    )
    policy = ConnectorAdmissionPolicy.model_validate(
        _load_json(CONNECTOR_FIXTURE_ROOT / "valid/closure_admission_policy.json")
    )
    custom_fixture = _load_json(
        CUSTOM_TOOL_FIXTURE_ROOT / "valid/closure_source_manifest.json"
    )
    custom_expected = _load_json(
        CUSTOM_TOOL_FIXTURE_ROOT / "valid/closure_expected_hashes.json"
    )
    custom_source = CustomToolSourceManifest.model_validate(custom_fixture["manifest"])
    custom_policy = CustomToolCompilerPolicy.model_validate(custom_fixture["policy"])
    harness_source = HarnessSource.model_validate(
        _load_json(MIXED_HARNESS_ROOT / "harness.json")
    )

    connector_result = admit_connector_tools(
        snapshot.model_dump(mode="json"),
        manifest.model_dump(mode="json"),
        policy.model_dump(mode="json"),
    )
    custom_result = compile_custom_tools(
        custom_source.model_dump(mode="json"),
        custom_policy.model_dump(mode="json"),
    )

    assert connector_result.accepted is True
    assert connector_result.diagnostics == ()
    assert custom_result.accepted is True
    assert custom_result.diagnostics == ()
    assert {tool.provider_tool_name for tool in snapshot.provider_tools} == {
        "delete_everything",
        "echo",
        "list_context",
        "unadmitted_notes",
    }
    assert [tool.provider_tool_name for tool in manifest.denied_tools] == [
        "delete_everything"
    ]
    assert manifest.denied_tools[0].approval_policy is ConnectorApprovalPolicy.FORBIDDEN
    assert {
        selection.provider_tool_name: selection.approval_policy
        for selection in manifest.selected_tools
    } == {
        "list_context": ConnectorApprovalPolicy.NONE,
        "echo": ConnectorApprovalPolicy.MILLRACE_EXPLICIT,
    }
    assert {tool.tool_id: tool.approval_policy for tool in custom_source.tools} == {
        "custom.echo": CustomToolApprovalPolicy.NONE,
        "custom.list": CustomToolApprovalPolicy.MILLRACE_EXPLICIT,
    }
    assert custom_expected["source_sha256"] == custom_result.source_sha256
    assert custom_expected["descriptor_sha256_by_tool_id"] == {
        descriptor.tool_id: descriptor.descriptor_sha256
        for descriptor in custom_result.descriptors
    }
    assert custom_expected["compilation_record_sha256_by_tool_id"] == {
        record.tool_id: record.compilation_record_sha256
        for record in custom_result.records
    }

    registry = ToolRegistry()
    registry.register(
        next(
            descriptor
            for descriptor in iter_builtin_tool_descriptors()
            if descriptor.tool_id == "builtin.terminal.submit"
        )
    )
    for descriptor in (*connector_result.descriptors, *custom_result.descriptors):
        registry.register(descriptor)
    catalog = registry.freeze()

    assert isinstance(catalog, ToolCatalogSnapshot)
    assert {record.tool_id for record in catalog.descriptor_hash_records} == (
        MIXED_TOOL_IDS
    )
    for illegal_tool_id in (
        "connector.fake_mcp.delete_everything",
        "connector.fake_mcp.unadmitted_notes",
        "custom.uncompiled",
    ):
        lookup = catalog.resolve_exact(illegal_tool_id, 1)
        assert lookup.classification is CatalogLookupClassification.MISSING
        assert lookup.entry is None

    request = _mixed_request(tmp_path)
    semantic = compile_semantic(
        CompileInvocation.from_request(request),
        harness_source,
        tool_snapshot=catalog,
        model_profile_snapshot=StaticMixedModelProfileSnapshot(),
    )
    denied_semantic = compile_semantic(
        CompileInvocation.from_request(request),
        _mixed_source_with_tool_ref("connector.fake_mcp.delete_everything@1"),
        tool_snapshot=catalog,
        model_profile_snapshot=StaticMixedModelProfileSnapshot(),
    )
    service_result = compile_harness(
        request,
        tool_catalog=catalog,
        model_profile_catalog=StaticMixedModelProfileSnapshot(),
    )

    assert semantic.diagnostics == ()
    assert semantic.resolved_harness is not None
    plan = lower_resolved_harness(semantic.resolved_harness)
    assert {node.binding.tool_id for node in plan.nodes} == MIXED_TOOL_IDS
    assert plan.required_capabilities == tuple(sorted(MIXED_CAPABILITY_IDS))
    assert [diagnostic.code for diagnostic in denied_semantic.diagnostics] == [
        "MF-R002"
    ]
    assert service_result.status is CompileStatus.COMMITTED
    assert service_result.diagnostics == ()
    assert service_result.compiled_plan_path is not None


def test_spec05_conformance_matrix_is_fail_closed() -> None:
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))

    assert isinstance(matrix, list)
    assert matrix
    row_ids = [row["requirement_id"] for row in matrix]
    assert len(row_ids) == len(set(row_ids))
    assert REQUIRED_ROWS <= set(row_ids)

    for row in matrix:
        _validate_row(row)


def test_spec05_conformance_matrix_validator_rejects_drift() -> None:
    implemented_row = _matrix_row("05A.discovery_non_authoritative")
    deferred_row = _matrix_row("05D.deferred_scope_boundary")

    for status in ("accepted_evidence", "unexpected_status"):
        row = _copy_row(implemented_row)
        row["status"] = status
        _assert_invalid_row(row)

    for missing_field in ("requirement_summary", "fixture_paths"):
        row = _copy_row(implemented_row)
        row.pop(missing_field)
        _assert_invalid_row(row)

    for bad_fixture_path in (
        "/tmp/spec05_fixture.json",
        "../spec05_fixture.json",
        "tests/fixtures/does-not-exist.json",
    ):
        row = _copy_row(implemented_row)
        row["fixture_paths"] = [bad_fixture_path]
        _assert_invalid_row(row)

    row = _copy_row(implemented_row)
    row["evidence_commands"] = ["curl https://example.invalid/spec05"]
    _assert_invalid_row(row)

    row = _copy_row(implemented_row)
    row["implemented_by_files"] = ["tests/fixtures/spec05_conformance_matrix.json"]
    row["covered_by_tests"] = ["tests/test_connector_custom_tool_closure.py"]
    _assert_invalid_row(row)

    row = _copy_row(implemented_row)
    row["implemented_by_files"] = ["millrace-agents/MILLRACE.md"]
    _assert_invalid_row(row)

    row = _copy_row(implemented_row)
    row["notes"] = f"{row['notes']} Implements live connector execution."
    _assert_invalid_row(row)

    row = _copy_row(deferred_row)
    row.pop("deferred_scope")
    _assert_invalid_row(row)

    row = _copy_row(deferred_row)
    row["deferred_scope"] = "Future work."
    _assert_invalid_row(row)

    row = _copy_row(implemented_row)
    row["status"] = "not_applicable"
    row["implemented_by_files"] = []
    row["covered_by_tests"] = []
    row["notes"] = "Not applicable to this closure target."
    _assert_invalid_row(row)


def test_public_package_exports_are_explicit_offline_contract_surfaces() -> None:
    root_exports = tuple(__import__("millforge").__all__)
    root_export_names = set(root_exports)
    root_imported_names = _root_public_imported_names()

    assert len(root_exports) == len(root_export_names)
    assert root_export_names <= root_imported_names
    assert not any(
        name.startswith("_") or name.startswith("Forge") for name in root_exports
    )

    for module_name, expected_exports in EXPECTED_PUBLIC_EXPORTS.items():
        module = importlib.import_module(module_name)
        exports = tuple(module.__all__)

        assert len(exports) == len(set(exports))
        assert set(exports) == expected_exports
        assert not any(
            name.startswith("_") or name.startswith("Forge") for name in exports
        )
        for name in exports:
            assert hasattr(module, name), (module_name, name)


def test_readme_and_roadmap_keep_05d_claims_offline_and_future_scope_explicit() -> None:
    readme = _normalized_text((ROOT / "README.md").read_text(encoding="utf-8"))
    roadmap = _normalized_text((ROOT / "ROADMAP.md").read_text(encoding="utf-8"))

    for phrase in (
        "05D adds deterministic offline closure evidence",
        "built-in",
        "admitted connector",
        "compiled contract-only custom-tool descriptors",
        "does not add",
    ):
        assert phrase in readme
    for phrase in README_DEFERRED_05D_CLAIMS:
        assert phrase in readme

    for phrase in (
        "05A through 05D now cover the offline closure path",
        "deterministic conformance matrix",
        "remain future work",
    ):
        assert phrase in roadmap
    for phrase in ROADMAP_DEFERRED_05D_CLAIMS:
        assert phrase in roadmap


def _validate_row(row: dict[str, Any]) -> None:
    assert REQUIRED_ROW_FIELDS <= set(row)
    assert row["requirement_id"] in REQUIRED_ROWS
    assert row["status"] in ALLOWED_STATUSES
    expected_fields = (
        DEFERRED_ROW_FIELDS if row["status"] == "deferred" else REQUIRED_ROW_FIELDS
    )
    assert set(row) == expected_fields
    assert isinstance(row["requirement_summary"], str) and row["requirement_summary"]
    assert isinstance(row["notes"], str) and row["notes"]
    _assert_string_list(row, "source_specs", allow_empty=False)
    _assert_string_list(row, "implemented_by_files", allow_empty=True)
    _assert_string_list(row, "covered_by_tests", allow_empty=True)
    _assert_string_list(row, "fixture_paths", allow_empty=True)
    _assert_string_list(row, "evidence_commands", allow_empty=False)
    _assert_string_list(row, "accepted_evidence_paths", allow_empty=True)

    for path in [
        *row["implemented_by_files"],
        *row["covered_by_tests"],
        *row["fixture_paths"],
        *row["accepted_evidence_paths"],
    ]:
        _assert_repo_path(path)
    for path in row["covered_by_tests"]:
        _assert_test_reference_path(path)

    for command in row["evidence_commands"]:
        _assert_offline_command(command)

    if row["status"] == "implemented":
        _assert_strong_implemented_evidence(row)
        _assert_no_forbidden_implemented_claim(row)
    elif row["status"] == "deferred":
        _assert_deferred_scope(row)
    else:
        _assert_not_applicable_contract(row)


def _matrix_row(requirement_id: str) -> dict[str, Any]:
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    return next(row for row in matrix if row["requirement_id"] == requirement_id)


def _copy_row(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(row))


def _assert_invalid_row(row: dict[str, Any]) -> None:
    with pytest.raises(AssertionError):
        _validate_row(row)


def _assert_string_list(
    row: dict[str, Any], field_name: str, *, allow_empty: bool
) -> None:
    values = row[field_name]
    assert isinstance(values, list), (row["requirement_id"], field_name)
    assert allow_empty or values, (row["requirement_id"], field_name)
    assert all(isinstance(value, str) and value for value in values), (
        row["requirement_id"],
        field_name,
    )


def _assert_repo_path(path_text: str) -> None:
    assert isinstance(path_text, str)
    path = Path(path_text)
    assert not path.is_absolute()
    assert ".." not in path.parts
    assert (ROOT / path).exists(), path_text


def _assert_test_reference_path(path_text: str) -> None:
    path = Path(path_text)
    assert path.parts and path.parts[0] == "tests", path_text
    assert path.name.startswith("test_") or path_text == "tests/compiler", path_text


def _assert_offline_command(command: str) -> None:
    assert command.startswith(ALLOWED_COMMAND_PREFIXES), command
    lowered = command.lower()
    assert not any(term in lowered for term in FORBIDDEN_COMMAND_TERMS), command
    for selector in re.findall(
        r"tests/[^\s:]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*)?", command
    ):
        path_text, _, test_name = selector.partition("::")
        path = ROOT / path_text
        assert path.exists(), command
        if test_name:
            source = path.read_text(encoding="utf-8")
            assert f"def {test_name}(" in source, command


def _assert_strong_implemented_evidence(row: dict[str, Any]) -> None:
    implementation_paths = set(row["implemented_by_files"])
    test_paths = set(row["covered_by_tests"])
    weak_only = {
        "tests/fixtures/spec05_conformance_matrix.json",
        "tests/test_connector_custom_tool_closure.py",
    }

    assert implementation_paths
    assert test_paths
    assert not (implementation_paths | test_paths) <= weak_only
    assert not any(path.startswith(PRIVATE_PREFIXES) for path in implementation_paths)


def _assert_deferred_scope(row: dict[str, Any]) -> None:
    scope = row["deferred_scope"]
    assert isinstance(scope, str) and scope.strip()
    scope_text = scope.casefold()
    notes_text = row["notes"].casefold()
    assert "deferred" in notes_text
    assert any(boundary in scope_text for boundary in ("future-only", "future work")), (
        scope
    )
    if row["requirement_id"] == "05D.deferred_scope_boundary":
        assert all(term in scope_text for term in DEFERRED_SCOPE_BOUNDARY_TERMS), scope


def _assert_not_applicable_contract(row: dict[str, Any]) -> None:
    assert row["requirement_id"] in NOT_APPLICABLE_REQUIREMENT_IDS
    assert not row["implemented_by_files"]
    assert "not applicable" in row["notes"].casefold()


def _assert_no_forbidden_implemented_claim(row: dict[str, Any]) -> None:
    claim_text = f"{row['requirement_summary']} {row['notes']}".lower()
    assert not any(term in claim_text for term in FORBIDDEN_IMPLEMENTED_CLAIMS)


def _normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _root_public_imported_names() -> set[str]:
    tree = ast.parse((ROOT / "src/millforge/__init__.py").read_text(encoding="utf-8"))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        assert node.module in ALLOWED_ROOT_EXPORT_MODULES, node.module
        for alias in node.names:
            imported_names.add(alias.asname or alias.name)
    return imported_names


def _mixed_request(tmp_path: Path) -> HarnessCompileRequest:
    output_root = tmp_path / "output"
    output_root.mkdir()
    (output_root / "compiled").mkdir()
    return HarnessCompileRequest(
        request_id="request.spec05.mixed_closure.v1",
        source_path="harness.json",
        source_root=str(MIXED_HARNESS_ROOT),
        source_format="json",
        output_dir="compiled",
        output_root=str(output_root),
        expected_harness_id="millforge.test.spec05.mixed_closure.v1",
        stage_kind_id="builder",
        legal_terminal_results=("BUILDER_COMPLETE",),
        capability_envelope=CapabilityEnvelope(
            grants=(
                CapabilityGrant(capability_id="workspace.read"),
                CapabilityGrant(capability_id="terminal.intent"),
                CapabilityGrant(capability_id="cap.connector.echo"),
                CapabilityGrant(capability_id="cap.connector.list_context"),
                CapabilityGrant(capability_id="cap.custom.echo"),
                CapabilityGrant(capability_id="cap.custom.list"),
            )
        ),
    )


def _mixed_source_with_tool_ref(tool_ref: str) -> HarnessSource:
    raw = _load_json(MIXED_HARNESS_ROOT / "harness.json")
    raw["graph"]["nodes"] = {
        "illegal": {"tool_ref": tool_ref, "terminal_result": "BUILDER_COMPLETE"}
    }
    raw["artifacts"] = {"declared_artifact_ids": [], "required_by_terminal": {}}
    return HarnessSource.model_validate(raw)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
