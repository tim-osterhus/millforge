from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

import millforge.tools.builtin_runtime as builtin_runtime
import millforge.tools as public_tools
from millforge import (
    ArtifactRef,
    CancellationRef,
    CapabilityEnvelope,
    CapabilityGrant,
    CompilerIdentity,
    CompiledArtifactPolicy,
    CompiledBudgetPolicy,
    CompiledContextPolicy,
    CompiledHarnessNode,
    CompiledHarnessPlan,
    CompiledModelProfile,
    CompiledPrerequisite,
    CustomToolApprovalPolicy,
    CustomToolCompilerPolicy,
    CustomToolDeclaration,
    CustomToolDescriptionPolicy,
    CustomToolRuntimeKind,
    CustomToolSourceManifest,
    CompiledPromptPolicy,
    Deadline,
    IdempotencyClass,
    RunDirRef,
    SideEffectCertainty,
    SideEffectClass,
    SideEffectRecord,
    StageIdentity,
    TerminalArtifactRequirement,
    TimeoutRef,
    TimingMetadata,
    ToolBindingRef,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolOutputPolicy,
    ToolTimeoutPolicy,
    ValidatedToolCall,
    compile_custom_tools,
)
from millforge.compiled_plan import finalize_compiled_plan_sha256
from millforge.compiler import (
    CompileStatus,
    HarnessCompileRequest,
    compile as compile_harness,
)
from millforge.connectors import (
    ConnectorAdmissionRecord,
    ConnectorAdmissionSnapshot,
    ConnectorApprovalPolicy,
    ConnectorBrokerOutcome,
    DeterministicFakeConnectorBroker,
)
from millforge.tools import (
    RuntimeToolRegistry,
    FrozenToolRegistrySnapshot,
    ToolBindingDenialCode,
    ToolDescriptor,
    ToolExecutionErrorCode,
    ToolRegistry,
    create_builtin_tool_executor,
    create_builtin_tool_snapshot,
    create_tool_executor,
    iter_builtin_tool_descriptors,
)
from millforge.tools.path_policy import PathPolicyError, validate_logical_path
from millforge.tools.results import canonical_sha256
from tests.compiler.conftest import StaticModelProfileCatalogSnapshot


def test_create_builtin_tool_executor_is_public() -> None:
    assert public_tools.create_builtin_tool_executor is create_builtin_tool_executor


@pytest.mark.asyncio
async def test_production_builtin_snapshot_compiled_executor_readiness_smoke(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    source_root.mkdir()
    (output_root / "compiled").mkdir(parents=True)
    source_path = source_root / "harness.json"
    source_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "kind": "millforge_harness",
                "harness_id": "millforge.test.builtin.readiness.v1",
                "harness_version": 1,
                "stage_scope": {"stage_kind_ids": ["builder"]},
                "model_profile_id": "profile.standard",
                "prompt": {
                    "policy_id": "millforge.test.builtin.readiness.policy.v1",
                    "system_instructions": "Exercise the built-in executor.",
                    "include_request_context": True,
                },
                "budgets": {
                    "max_iterations": 6,
                    "max_validation_retries": 1,
                    "max_tool_errors": 2,
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
                        "inspect": {"tool_ref": "builtin.request.inspect@1"},
                        "read_file": {"tool_ref": "builtin.workspace.read_file@1"},
                        "submit": {
                            "tool_ref": "builtin.terminal.submit@1",
                            "terminal_result": "BUILDER_COMPLETE",
                        },
                        "reject": {
                            "tool_ref": "builtin.terminal.reject@1",
                            "terminal_result": "BUILDER_REJECTED",
                        },
                        "escalate": {
                            "tool_ref": "builtin.terminal.escalate@1",
                            "terminal_result": "BUILDER_ESCALATED",
                        },
                    }
                },
                "artifacts": {
                    "declared_artifact_ids": [],
                    "required_by_terminal": {},
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    request = HarnessCompileRequest(
        request_id="request.builtin.readiness.v1",
        source_path="harness.json",
        source_root=str(source_root),
        source_format="json",
        output_dir="compiled",
        output_root=str(output_root),
        expected_harness_id="millforge.test.builtin.readiness.v1",
        stage_kind_id="builder",
        legal_terminal_results=(
            "BUILDER_COMPLETE",
            "BUILDER_ESCALATED",
            "BUILDER_REJECTED",
        ),
        capability_envelope=CapabilityEnvelope(
            grants=(
                CapabilityGrant(capability_id="request.read"),
                CapabilityGrant(capability_id="terminal.intent"),
                CapabilityGrant(capability_id="workspace.read"),
            )
        ),
    )
    snapshot = cast(FrozenToolRegistrySnapshot, create_builtin_tool_snapshot())
    compile_result = compile_harness(
        request,
        tool_catalog=snapshot,
        model_profile_catalog=StaticModelProfileCatalogSnapshot(
            profiles={
                "profile.standard": CompiledModelProfile(profile_id="profile.standard")
            }
        ),
    )
    assert compile_result.status == CompileStatus.COMMITTED
    assert compile_result.compiled_plan_path is not None

    plan = CompiledHarnessPlan.model_validate_json(
        (output_root / compile_result.compiled_plan_path).read_text(encoding="utf-8")
    )
    snapshot_hashes = {
        record.tool_id: record.descriptor_sha256
        for record in snapshot.descriptor_hash_records
    }
    assert {
        node.binding.tool_id: node.binding.descriptor_sha256 for node in plan.nodes
    } == {
        node.binding.tool_id: snapshot_hashes[node.binding.tool_id]
        for node in plan.nodes
    }
    executor = create_builtin_tool_executor(plan)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text(
        "SECRET_TOKEN=abc123 /mnt/f/_Millrace/private.txt\n",
        encoding="utf-8",
    )
    context = _context(
        "request.read",
        "terminal.intent",
        "workspace.read",
        workspace_root=workspace,
    )

    read_file = await executor.execute_model_tool(
        model_tool_name="read_workspace_file",
        call_id="call-read-file",
        arguments={"path": "README.md"},
        context=context,
    )
    uncompiled = await executor.execute_model_tool(
        model_tool_name="write_workspace_file",
        call_id="call-uncompiled",
        arguments={"path": "README.md", "content": "nope"},
        context=context,
    )
    unauthorized = await executor.execute_model_tool(
        model_tool_name="read_workspace_file",
        call_id="call-unauthorized",
        arguments={"path": "README.md"},
        context=_context("request.read", workspace_root=workspace),
    )
    submitted = await executor.execute_model_tool(
        model_tool_name="submit_terminal_intent",
        call_id="call-submit",
        arguments={"terminal_result": "BUILDER_COMPLETE", "summary": "done"},
        context=context,
    )
    rejected = await executor.execute_model_tool(
        model_tool_name="reject_terminal_intent",
        call_id="call-reject",
        arguments={"terminal_result": "BUILDER_REJECTED", "summary": "reject"},
        context=context,
    )
    escalated = await executor.execute_model_tool(
        model_tool_name="escalate_terminal_intent",
        call_id="call-escalate",
        arguments={
            "terminal_result": "BUILDER_ESCALATED",
            "summary": "blocked",
            "blocker": "needs operator",
        },
        context=context,
    )

    assert read_file.status is ToolExecutionStatus.SUCCESS
    assert "SECRET_TOKEN=abc123" not in read_file.structured_data["content"]
    assert "/mnt/f/_Millrace/private.txt" not in read_file.structured_data["content"]
    assert read_file.output_sha256 == canonical_sha256(read_file.structured_data)
    assert uncompiled.status is ToolExecutionStatus.NOT_EXECUTED
    assert uncompiled.error_code == ToolBindingDenialCode.NOT_FOUND.value
    assert unauthorized.status is ToolExecutionStatus.NOT_EXECUTED
    assert unauthorized.error_code == ToolExecutionErrorCode.CAPABILITY_DENIED.value
    assert [submitted.status, rejected.status, escalated.status] == [
        ToolExecutionStatus.SUCCESS,
        ToolExecutionStatus.SUCCESS,
        ToolExecutionStatus.SUCCESS,
    ]
    assert [
        submitted.structured_data["terminal_result"],
        rejected.structured_data["terminal_result"],
        escalated.structured_data["terminal_result"],
    ] == ["BUILDER_COMPLETE", "BUILDER_REJECTED", "BUILDER_ESCALATED"]

    traces = executor.trace_records
    assert [trace.tool_call_id for trace in traces] == [
        "call-read-file",
        "call-uncompiled",
        "call-unauthorized",
        "call-submit",
        "call-reject",
        "call-escalate",
    ]
    assert [trace.sequence for trace in traces] == [1, 2, 3, 4, 5, 6]
    assert traces[0].execution_status is ToolExecutionStatus.SUCCESS
    assert traces[0].output_sha256 == read_file.output_sha256
    assert traces[1].binding_resolution_status == "uncompiled"
    assert traces[1].side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert traces[1].output_sha256 is None
    assert traces[2].capability_decisions[0].decision.value == "denied"
    assert traces[2].side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert traces[2].output_sha256 is None


@pytest.mark.asyncio
async def test_exact_compiled_binding_success_dispatches_implementation() -> None:
    inspect_descriptor = _descriptor("builtin.request.inspect")
    terminal_descriptor = _descriptor("builtin.terminal.submit")
    plan = _plan(inspect_descriptor, terminal_descriptor)
    calls: list[ValidatedToolCall] = []

    async def implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        calls.append(call)
        return _success(call, inspect_descriptor)

    registry = RuntimeToolRegistry()
    registry.register(inspect_descriptor.implementation_id, implementation)
    registry.register(terminal_descriptor.implementation_id, implementation)
    executor = create_tool_executor(
        plan=plan,
        descriptor_snapshot=create_builtin_tool_snapshot(),
        runtime_registry=registry,
    )

    result = await executor.execute_model_tool(
        model_tool_name=inspect_descriptor.model_tool_name,
        call_id="call-1",
        arguments={},
        context=_context("request.read"),
    )

    assert result.status is ToolExecutionStatus.SUCCESS
    assert [call.binding for call in calls] == [plan.nodes[0].binding]
    assert executor.trace_records[0].execution_status is ToolExecutionStatus.SUCCESS


@pytest.mark.asyncio
async def test_uncompiled_and_ambiguous_model_visible_names_emit_traces() -> None:
    inspect_descriptor = _descriptor("builtin.request.inspect")
    terminal_descriptor = _descriptor("builtin.terminal.submit")
    plan = _plan(inspect_descriptor, terminal_descriptor)
    duplicate = plan.nodes[1].model_copy(
        update={
            "node_id": "duplicate-inspect",
            "model_tool_name": inspect_descriptor.model_tool_name,
        }
    )
    ambiguous_plan = plan.model_copy(update={"nodes": (plan.nodes[0], duplicate)})
    executor = create_builtin_tool_executor(ambiguous_plan)
    uncompiled_descriptor = _descriptor("builtin.workspace.read_file")

    uncompiled = await executor.execute_model_tool(
        model_tool_name=uncompiled_descriptor.model_tool_name,
        call_id="call-uncompiled",
        arguments={"path": "README.md"},
        context=_context(),
    )
    ambiguous = await executor.execute_model_tool(
        model_tool_name=inspect_descriptor.model_tool_name,
        call_id="call-ambiguous",
        arguments={},
        context=_context(),
    )

    assert uncompiled.status is ToolExecutionStatus.NOT_EXECUTED
    assert uncompiled.error_code == ToolBindingDenialCode.NOT_FOUND.value
    assert ambiguous.status is ToolExecutionStatus.NOT_EXECUTED
    assert ambiguous.error_code == ToolBindingDenialCode.CONFLICT.value
    assert [trace.sequence for trace in executor.trace_records] == [1, 2]

    uncompiled_trace, ambiguous_trace = executor.trace_records
    assert uncompiled_trace.tool_call_id == "call-uncompiled"
    assert ambiguous_trace.tool_call_id == "call-ambiguous"
    assert uncompiled_trace.model_tool_name == uncompiled_descriptor.model_tool_name
    assert ambiguous_trace.model_tool_name == inspect_descriptor.model_tool_name
    assert uncompiled_trace.input_sha256 == canonical_sha256({"path": "README.md"})
    assert ambiguous_trace.input_sha256 == canonical_sha256({})
    assert uncompiled_trace.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert ambiguous_trace.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert uncompiled_trace.output_sha256 is None
    assert ambiguous_trace.output_sha256 is None
    assert uncompiled_trace.binding_resolution_status == "uncompiled"
    assert ambiguous_trace.binding_resolution_status == "ambiguous"


@pytest.mark.asyncio
async def test_missing_runtime_implementation_surfaces_hard_failure_and_trace() -> None:
    descriptor = _descriptor("builtin.request.inspect")
    terminal_descriptor = _descriptor("builtin.terminal.submit")
    plan = _plan(descriptor, terminal_descriptor)
    executor = create_tool_executor(
        plan=plan,
        descriptor_snapshot=create_builtin_tool_snapshot(),
        runtime_registry=RuntimeToolRegistry(),
    )

    result = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-missing-impl",
        arguments={},
        context=_context("request.read"),
    )

    assert result.status is ToolExecutionStatus.HARD_FAILURE
    assert result.error_code == ToolBindingDenialCode.NOT_FOUND.value
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert result.output_sha256 is None
    assert len(executor.trace_records) == 1
    trace = executor.trace_records[0]
    assert trace.execution_status is ToolExecutionStatus.HARD_FAILURE
    assert trace.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert trace.output_sha256 is None


@pytest.mark.parametrize(
    "binding_update,expected_field",
    [
        ({"tool_id": "builtin.request.missing"}, "tool_id/tool_version"),
        ({"tool_version": 2}, "tool_id/tool_version"),
    ],
)
@pytest.mark.asyncio
async def test_stale_compiled_binding_identity_rejects_before_dispatch(
    binding_update: dict[str, Any],
    expected_field: str,
) -> None:
    inspect_descriptor = _descriptor("builtin.request.inspect")
    terminal_descriptor = _descriptor("builtin.terminal.submit")
    plan = _plan(inspect_descriptor, terminal_descriptor)
    stale_node = plan.nodes[0].model_copy(
        update={
            "binding": plan.nodes[0].binding.model_copy(update=binding_update),
        }
    )
    stale_plan = plan.model_copy(update={"nodes": (stale_node, plan.nodes[1])})
    calls: list[ValidatedToolCall] = []
    executor = _executor_with_call_log(stale_plan, calls)

    result = await executor.execute_model_tool(
        model_tool_name=inspect_descriptor.model_tool_name,
        call_id=f"call-stale-{expected_field}",
        arguments={},
        context=_context("request.read"),
    )

    assert result.status is ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == ToolBindingDenialCode.NOT_FOUND.value
    assert result.structured_data["evidence"]["binding_field"] == expected_field
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert result.output_sha256 is None
    assert calls == []
    trace = executor.trace_records[0]
    assert trace.execution_status is ToolExecutionStatus.NOT_EXECUTED
    assert trace.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert trace.output_sha256 is None


@pytest.mark.parametrize(
    "field,update,expected_field",
    [
        ("binding", {"descriptor_sha256": "a" * 64}, "descriptor_sha256"),
        ("binding", {"implementation_id": "impl.other.v1"}, "implementation_id"),
        ("node", {"model_tool_name": "inspect_request_changed"}, "model_tool_name"),
        (
            "node",
            {
                "input_schema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                }
            },
            "input_schema",
        ),
        (
            "node",
            {"required_capabilities": ("request.read", "workspace.read")},
            "required_capabilities",
        ),
        (
            "node",
            {"produced_artifact_ids": ("unexpected_artifact",)},
            "produced_artifact_ids",
        ),
        (
            "node",
            {"side_effect_class": SideEffectClass.WORKSPACE_WRITE},
            "side_effect_class",
        ),
        ("node", {"idempotency": IdempotencyClass.NON_IDEMPOTENT}, "idempotency"),
    ],
)
@pytest.mark.asyncio
async def test_projection_and_binding_mismatches_reject_before_dispatch(
    field: str,
    update: dict[str, Any],
    expected_field: str,
) -> None:
    inspect_descriptor = _descriptor("builtin.request.inspect")
    terminal_descriptor = _descriptor("builtin.terminal.submit")
    plan = _plan(inspect_descriptor, terminal_descriptor)
    node = plan.nodes[0]
    if field == "binding":
        node = node.model_copy(
            update={"binding": node.binding.model_copy(update=update)}
        )
    else:
        node = node.model_copy(update=update)
    mutated = plan.model_copy(update={"nodes": (node, plan.nodes[1])})
    calls: list[ValidatedToolCall] = []
    executor = _executor_with_call_log(mutated, calls)

    result = await executor.execute_model_tool(
        model_tool_name=node.model_tool_name,
        call_id=f"call-{expected_field}",
        arguments={},
        context=_context(),
    )

    assert result.status is ToolExecutionStatus.HARD_FAILURE
    assert result.error_code == ToolBindingDenialCode.BINDING_MISMATCH.value
    assert result.structured_data["evidence"]["binding_field"] == expected_field
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert result.output_sha256 is None
    assert calls == []
    trace = executor.trace_records[0]
    assert trace.execution_status is ToolExecutionStatus.HARD_FAILURE
    assert trace.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert trace.output_sha256 is None


@pytest.mark.asyncio
async def test_output_schema_descriptor_drift_rejects_stale_compiled_binding_before_dispatch() -> (
    None
):
    baseline = _connector_descriptor()
    drifted = _connector_descriptor(
        output_schema={
            "type": "object",
            "properties": {"changed": {"type": "boolean"}},
            "required": ["changed"],
            "additionalProperties": False,
        }
    )
    terminal = _descriptor("builtin.terminal.submit")
    plan = _plan(baseline, terminal)
    registry = ToolRegistry()
    baseline_registry = ToolRegistry()
    baseline_registry.register(baseline)
    registry.register(drifted)
    registry.register(terminal)
    calls: list[ValidatedToolCall] = []

    async def implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        calls.append(call)
        return _success(call, baseline)

    runtime_registry = RuntimeToolRegistry()
    runtime_registry.register(drifted.implementation_id, implementation)
    runtime_registry.register(terminal.implementation_id, implementation)
    executor = create_tool_executor(
        plan=plan,
        descriptor_snapshot=registry.freeze(),
        runtime_registry=runtime_registry,
        connector_admission_snapshot=_connector_admission_snapshot_for(
            baseline,
            baseline_registry.freeze(),
        ),
        connector_broker=DeterministicFakeConnectorBroker(),
    )

    result = await executor.execute_model_tool(
        model_tool_name=baseline.model_tool_name,
        call_id="call-output-schema-drift",
        arguments={"message": "hello"},
        context=_context(),
    )

    assert result.status is ToolExecutionStatus.HARD_FAILURE
    assert result.error_code == ToolBindingDenialCode.BINDING_MISMATCH.value
    assert result.structured_data["evidence"]["binding_field"] == "descriptor_sha256"
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert result.output_sha256 is None
    assert calls == []


@pytest.mark.asyncio
async def test_connector_shaped_descriptor_compiles_generically_but_execution_requires_runtime_registration(
    tmp_path: Path,
) -> None:
    connector = _connector_descriptor()
    terminal = _descriptor("builtin.terminal.submit")
    registry = ToolRegistry()
    registry.register(connector)
    registry.register(terminal)
    snapshot = registry.freeze()
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    source_root.mkdir()
    (output_root / "compiled").mkdir(parents=True)
    (source_root / "harness.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "kind": "millforge_harness",
                "harness_id": "millforge.test.connector.boundary.v1",
                "harness_version": 1,
                "stage_scope": {"stage_kind_ids": ["builder"]},
                "model_profile_id": "profile.standard",
                "prompt": {
                    "policy_id": "millforge.test.connector.boundary.policy.v1",
                    "system_instructions": "Exercise generic connector-shaped binding.",
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
                        "echo": {"tool_ref": "connector.test.echo@1"},
                        "done": {
                            "tool_ref": "builtin.terminal.submit@1",
                            "terminal_result": "BUILDER_COMPLETE",
                            "prerequisites": [{"node_id": "echo"}],
                        },
                    }
                },
                "artifacts": {
                    "declared_artifact_ids": [],
                    "required_by_terminal": {},
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    compile_result = compile_harness(
        HarnessCompileRequest(
            request_id="request.connector.boundary.v1",
            source_path="harness.json",
            source_root=str(source_root),
            source_format="json",
            output_dir="compiled",
            output_root=str(output_root),
            expected_harness_id="millforge.test.connector.boundary.v1",
            stage_kind_id="builder",
            legal_terminal_results=("BUILDER_COMPLETE",),
            capability_envelope=CapabilityEnvelope(
                grants=(CapabilityGrant(capability_id="terminal.intent"),)
            ),
        ),
        tool_catalog=snapshot,
        model_profile_catalog=StaticModelProfileCatalogSnapshot(
            profiles={
                "profile.standard": CompiledModelProfile(profile_id="profile.standard")
            }
        ),
    )
    assert compile_result.status == CompileStatus.COMMITTED
    assert compile_result.compiled_plan_path is not None
    plan = CompiledHarnessPlan.model_validate_json(
        (output_root / compile_result.compiled_plan_path).read_text(encoding="utf-8")
    )
    echo_node = next(node for node in plan.nodes if node.node_id == "echo")
    assert echo_node.binding.tool_id == "connector.test.echo"
    assert echo_node.model_tool_name == "connector_test_echo"
    assert echo_node.binding.descriptor_sha256 == connector.descriptor_sha256

    executor = create_tool_executor(
        plan=plan,
        descriptor_snapshot=snapshot,
        runtime_registry=RuntimeToolRegistry(),
        connector_admission_snapshot=_connector_admission_snapshot_for(
            connector,
            snapshot,
        ),
        connector_broker=DeterministicFakeConnectorBroker(),
    )
    result = await executor.execute_model_tool(
        model_tool_name="connector_test_echo",
        call_id="call-connector-missing-impl",
        arguments={"message": "hello"},
        context=_context("terminal.intent"),
    )

    assert result.status is ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == ToolBindingDenialCode.NOT_FOUND.value
    assert result.structured_data["evidence"]["provider_tool_name"] == "echo"
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert len(executor.trace_records) == 1
    trace = executor.trace_records[0]
    assert trace.execution_status is ToolExecutionStatus.NOT_EXECUTED
    assert trace.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED


@pytest.mark.asyncio
async def test_compiled_contract_only_custom_tool_missing_implementation_fails_closed() -> (
    None
):
    source = CustomToolSourceManifest(
        package_id="custom.package",
        package_version=1,
        source_name="operator-source",
        created_at="2026-06-18T04:45:00Z",
        tools=(
            CustomToolDeclaration(
                tool_id="custom.echo",
                tool_version=1,
                implementation_id="custom.echo.impl",
                runtime_kind=CustomToolRuntimeKind.CONTRACT_ONLY,
                model_tool_name="custom_echo",
                description="Summarize a supplied message.",
                description_policy=CustomToolDescriptionPolicy.OPERATOR_SUPPLIED,
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
                required_capabilities=("cap.custom.echo",),
                produced_artifact_ids=(),
                side_effect_class=SideEffectClass.READ_ONLY,
                idempotency=IdempotencyClass.IDEMPOTENT,
                timeout_policy=ToolTimeoutPolicy(
                    timeout_seconds=30,
                    cancellation_grace_seconds=5,
                ),
                output_policy=ToolOutputPolicy(
                    max_output_bytes=4096,
                    max_summary_utf8=512,
                    redact_secrets=True,
                ),
                approval_policy=CustomToolApprovalPolicy.NONE,
            ),
        ),
    )
    result = compile_custom_tools(
        source,
        CustomToolCompilerPolicy(allowed_capability_ids=("cap.custom.echo",)),
    )
    assert result.accepted is True
    descriptor = result.descriptors[0]
    terminal = _descriptor("builtin.terminal.submit")
    registry = ToolRegistry()
    registry.register(descriptor)
    registry.register(terminal)
    broker = DeterministicFakeConnectorBroker(
        {
            ("connector.fake_mcp", "echo"): ConnectorBrokerOutcome(
                status=ToolExecutionStatus.SUCCESS,
                summary="broker should not run",
                structured_data={"summary": "unexpected connector execution"},
            )
        }
    )
    executor = create_tool_executor(
        plan=_plan(descriptor, terminal),
        descriptor_snapshot=registry.freeze(),
        runtime_registry=RuntimeToolRegistry(),
        connector_broker=broker,
    )

    denied = await executor.execute_model_tool(
        model_tool_name="custom_echo",
        call_id="call-custom-missing-impl",
        arguments={"message": "hello"},
        context=_context("cap.custom.echo"),
    )

    assert denied.status is ToolExecutionStatus.HARD_FAILURE
    assert denied.error_code == ToolBindingDenialCode.NOT_FOUND.value
    assert denied.structured_data["evidence"]["binding_field"] == "implementation_id"
    assert denied.structured_data["evidence"]["implementation_id"] == (
        "custom.echo.impl"
    )
    assert denied.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert denied.output_sha256 is None
    assert broker.requests == ()
    trace = executor.trace_records[0]
    assert trace.binding_resolution_status == "resolved"
    assert trace.binding.tool_id == "custom.echo"
    assert trace.binding.descriptor_sha256 == descriptor.descriptor_sha256
    assert trace.binding.implementation_id == "custom.echo.impl"
    assert trace.capability_decisions == ()
    assert trace.execution_status is ToolExecutionStatus.HARD_FAILURE
    assert trace.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert trace.output_sha256 is None
    assert trace.broker_attempted is None
    assert trace.redacted_evidence == {}


@pytest.mark.asyncio
async def test_validated_call_binding_mismatch_rejects_before_dispatch() -> None:
    inspect_descriptor = _descriptor("builtin.request.inspect")
    terminal_descriptor = _descriptor("builtin.terminal.submit")
    plan = _plan(inspect_descriptor, terminal_descriptor)
    calls: list[ValidatedToolCall] = []
    executor = _executor_with_call_log(plan, calls)
    node = plan.nodes[0]
    call = ValidatedToolCall(
        call_id="call-mismatch",
        node_id=node.node_id,
        binding=node.binding.model_copy(update={"descriptor_sha256": "b" * 64}),
        arguments={},
    )

    result = await executor.execute(call, _context())

    assert result.status is ToolExecutionStatus.HARD_FAILURE
    assert result.error_code == ToolBindingDenialCode.BINDING_MISMATCH.value
    assert result.structured_data["evidence"]["binding_field"] == "descriptor_sha256"
    assert calls == []


def test_missing_runtime_implementation_rejects_before_dispatch() -> None:
    plan = _plan(
        _descriptor("builtin.request.inspect"), _descriptor("builtin.terminal.submit")
    )
    executor = create_tool_executor(
        plan=plan,
        descriptor_snapshot=create_builtin_tool_snapshot(),
        runtime_registry=RuntimeToolRegistry(),
    )

    resolved = executor.validate_model_tool_call(
        model_tool_name="inspect_request",
        call_id="call-missing-impl",
        arguments={},
    )

    assert isinstance(resolved, ToolExecutionResult)
    assert resolved.error_code == ToolBindingDenialCode.NOT_FOUND.value
    assert resolved.structured_data["evidence"]["binding_field"] == "implementation_id"


def test_schema_invalid_arguments_reject_before_dispatch() -> None:
    descriptor = _descriptor("builtin.workspace.read_file")
    executor = create_builtin_tool_executor(
        _plan(descriptor, _descriptor("builtin.terminal.submit"))
    )

    result = executor.validate_model_tool_call(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-invalid-args",
        arguments={"path": "README.md", "absolute_path": "/tmp/secret"},
    )

    assert isinstance(result, ToolExecutionResult)
    assert result.error_code == ToolExecutionErrorCode.INVALID_ARGUMENTS.value


@pytest.mark.asyncio
async def test_schema_valid_unauthorized_arguments_recheck_capabilities() -> None:
    descriptor = _descriptor("builtin.workspace.read_file")
    calls: list[ValidatedToolCall] = []
    executor = _executor_with_call_log(
        _plan(descriptor, _descriptor("builtin.terminal.submit")),
        calls,
    )

    result = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-capability",
        arguments={"path": "README.md"},
        context=_context(),
    )

    assert result.error_code == ToolExecutionErrorCode.CAPABILITY_DENIED.value
    assert result.status is ToolExecutionStatus.NOT_EXECUTED
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert calls == []
    assert executor.trace_records[-1].capability_decisions[0].decision.value == "denied"


@pytest.mark.asyncio
async def test_prerequisite_deadline_and_cancellation_denials_are_pre_entry() -> None:
    inspect_descriptor = _descriptor("builtin.request.inspect")
    terminal_descriptor = _descriptor("builtin.terminal.submit")
    plan = _plan(inspect_descriptor, terminal_descriptor)
    terminal = plan.nodes[1].model_copy(
        update={"prerequisites": (CompiledPrerequisite(node_id=plan.nodes[0].node_id),)}
    )
    plan = plan.model_copy(update={"nodes": (plan.nodes[0], terminal)})
    calls: list[ValidatedToolCall] = []
    executor = _executor_with_call_log(plan, calls)

    prereq_denied = await executor.execute_model_tool(
        model_tool_name=terminal_descriptor.model_tool_name,
        call_id="call-prereq",
        arguments={"terminal_result": "BUILDER_COMPLETE", "summary": "done"},
        context=_context("terminal.intent"),
    )
    timed_out = await executor.execute_model_tool(
        model_tool_name=inspect_descriptor.model_tool_name,
        call_id="call-timeout",
        arguments={},
        context=_context("request.read", current_monotonic=60.0),
    )
    cancelled = await executor.execute_model_tool(
        model_tool_name=inspect_descriptor.model_tool_name,
        call_id="call-cancel",
        arguments={},
        context=_context("request.read", cancellation_requested=True),
    )

    assert prereq_denied.error_code == ToolExecutionErrorCode.PREREQUISITE_DENIED.value
    assert timed_out.error_code == ToolExecutionErrorCode.TIMEOUT.value
    assert cancelled.error_code == ToolExecutionErrorCode.CANCELLED.value
    assert calls == []
    assert all(trace.output_sha256 is None for trace in executor.trace_records)


@pytest.mark.asyncio
async def test_request_builtins_return_bounded_runtime_context_only(
    tmp_path: Path,
) -> None:
    executor = create_builtin_tool_executor(
        _plan(
            _descriptor("builtin.request.inspect"),
            _descriptor("builtin.request.read_requirements"),
            _descriptor("builtin.terminal.submit"),
        )
    )
    context = _context(
        "request.read",
        workspace_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        input_artifacts=(ArtifactRef(artifact_id="input", path=Path("input.md")),),
        work_item_id="task-1",
    )

    inspected = await executor.execute_model_tool(
        model_tool_name="inspect_request",
        call_id="call-request-inspect",
        arguments={},
        context=context,
    )
    requirements = await executor.execute_model_tool(
        model_tool_name="read_request_requirements",
        call_id="call-request-requirements",
        arguments={},
        context=context,
    )

    assert inspected.status is ToolExecutionStatus.SUCCESS
    assert inspected.structured_data["request_id"] == "request-1"
    assert inspected.structured_data["stage_id"] == "builder"
    assert inspected.structured_data["objective"] == "task-1"
    assert inspected.structured_data["artifact_refs"] == ["input"]
    assert requirements.status is ToolExecutionStatus.SUCCESS
    assert "capability:request.read" in requirements.structured_data["requirements"]
    assert "/tmp" not in str(inspected.structured_data)
    assert "/tmp" not in str(requirements.structured_data)


@pytest.mark.asyncio
async def test_workspace_builtins_reject_escaping_paths_and_symlinks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("needle\n", encoding="utf-8")
    external = tmp_path / "outside.txt"
    external.write_text("secret", encoding="utf-8")
    (workspace / "escape-link.txt").symlink_to(external)
    executor = create_builtin_tool_executor(
        _plan(
            _descriptor("builtin.workspace.read_file"),
            _descriptor("builtin.workspace.search_text"),
            _descriptor("builtin.workspace.list_files"),
            _descriptor("builtin.terminal.submit"),
        )
    )
    context = _context(
        "workspace.read",
        "workspace.search",
        workspace_root=workspace,
    )

    admitted = await executor.execute_model_tool(
        model_tool_name="read_workspace_file",
        call_id="call-read-admitted",
        arguments={"path": "README.md"},
        context=context,
    )
    assert admitted.status is ToolExecutionStatus.SUCCESS
    assert admitted.structured_data["content"] == "needle\n"

    for index, path in enumerate(
        (
            "../outside.txt",
            "/tmp/outside.txt",
            "C:/Windows/System32",
            "//server/share/file.txt",
            "\\\\.\\NUL",
            "README.md:ads",
            "escape-link.txt",
        ),
        start=1,
    ):
        denied = await executor.execute_model_tool(
            model_tool_name="read_workspace_file",
            call_id=f"call-read-denied-{index}",
            arguments={"path": path},
            context=context,
        )
        assert denied.status is ToolExecutionStatus.NOT_EXECUTED
        assert denied.error_code == ToolExecutionErrorCode.POLICY_DENIED.value

    searched = await executor.execute_model_tool(
        model_tool_name="search_workspace_text",
        call_id="call-search",
        arguments={"query": "needle", "root": ".", "max_results": 5},
        context=context,
    )
    listed = await executor.execute_model_tool(
        model_tool_name="list_workspace_files",
        call_id="call-list",
        arguments={"root": ".", "glob": "*.md"},
        context=context,
    )
    assert searched.structured_data["matches"][0]["path"] == "README.md"
    assert listed.structured_data["paths"] == ["README.md"]


@pytest.mark.parametrize(
    "path",
    (
        "CON",
        "con",
        "PRN",
        "prn",
        "AUX.txt",
        "dir/CON",
        "dir/aux.txt",
        "NUL",
        *tuple(f"COM{i}" for i in range(1, 10)),
        "COM9.md",
        *tuple(f"LPT{i}" for i in range(1, 10)),
        "nested/path/LPT9.md",
    ),
)
def test_validate_logical_path_rejects_windows_reserved_device_names(path: str) -> None:
    with pytest.raises(PathPolicyError):
        validate_logical_path(path)


def test_validate_logical_path_allows_similar_non_device_names() -> None:
    assert validate_logical_path("COM10.txt").as_posix() == "COM10.txt"
    assert validate_logical_path("nested/LPT10.md").as_posix() == "nested/LPT10.md"


@pytest.mark.asyncio
async def test_workspace_write_is_atomic_and_patch_validates_touched_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "a.txt").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=workspace, check=True, stdout=subprocess.PIPE)
    executor = create_builtin_tool_executor(
        _plan(
            _descriptor("builtin.workspace.write_file"),
            _descriptor("builtin.workspace.apply_patch"),
            _descriptor("builtin.terminal.submit"),
        )
    )
    context = _context("workspace.write", workspace_root=workspace)

    written = await executor.execute_model_tool(
        model_tool_name="write_workspace_file",
        call_id="call-write",
        arguments={"path": "src/out.txt", "content": "hello"},
        context=context,
    )
    assert written.status is ToolExecutionStatus.SUCCESS
    assert (workspace / "src" / "out.txt").read_text(encoding="utf-8") == "hello"
    assert not list((workspace / "src").glob(".out.txt.tmp.*"))

    denied = await executor.execute_model_tool(
        model_tool_name="apply_workspace_patch",
        call_id="call-patch-denied",
        arguments={
            "patch": (
                "diff --git a/src/a.txt b/../escape.txt\n"
                "--- a/src/a.txt\n"
                "+++ b/../escape.txt\n"
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new\n"
            )
        },
        context=context,
    )
    assert denied.status is ToolExecutionStatus.NOT_EXECUTED
    assert denied.error_code == ToolExecutionErrorCode.POLICY_DENIED.value


@pytest.mark.asyncio
async def test_artifact_builtins_enforce_compiled_policy_and_return_refs(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "millforge"
    artifact_root.mkdir()
    plan_descriptor = _descriptor("builtin.artifact.write_plan")
    verdict_descriptor = _descriptor("builtin.artifact.write_verdict")
    plan = _plan(
        plan_descriptor,
        verdict_descriptor,
        _descriptor("builtin.artifact.read"),
        _descriptor("builtin.terminal.submit"),
    )
    executor = create_builtin_tool_executor(plan)
    context = _context(
        "artifact.read",
        "artifact.write",
        artifact_root=artifact_root,
        compiled_artifact_policy=plan.artifact_policy,
    ).model_copy(
        update={
            "run_directory": RunDirRef(run_id="run-1", path=tmp_path),
            "artifact_root": artifact_root,
        }
    )

    written = await executor.execute_model_tool(
        model_tool_name="write_plan_artifact",
        call_id="call-artifact-write",
        arguments={"plan": "do work"},
        context=context,
    )
    assert written.status is ToolExecutionStatus.SUCCESS
    assert written.structured_data["artifact_id"] == "plan"
    assert len(written.artifact_refs) == 1
    written_ref = written.artifact_refs[0]
    assert written_ref.artifact_id == "plan"
    assert written_ref.path.as_posix() == "millforge/plan.md"
    assert written_ref.content_type == "text/markdown"
    assert not written_ref.path.is_absolute()
    assert (context.run_directory.path / written_ref.path).read_text(
        encoding="utf-8"
    ) == "do work"

    read = await executor.execute_model_tool(
        model_tool_name="read_artifact",
        call_id="call-artifact-read",
        arguments={"artifact_id": "plan"},
        context=context,
    )
    assert read.status is ToolExecutionStatus.SUCCESS
    assert read.structured_data["content"] == "do work"
    assert (
        read.structured_data["content_sha256"]
        == written.structured_data["content_sha256"]
    )
    assert len(read.artifact_refs) == 1
    read_ref = read.artifact_refs[0]
    assert read_ref.artifact_id == written_ref.artifact_id
    assert read_ref.path == written_ref.path
    assert read_ref.content_type == written_ref.content_type

    denied = await executor.execute_model_tool(
        model_tool_name="write_verdict_artifact",
        call_id="call-artifact-denied",
        arguments={"artifact_id": "checker_verdict", "verdict": "ok"},
        context=context.model_copy(
            update={
                "compiled_artifact_policy": CompiledArtifactPolicy(
                    declared_artifact_ids=("plan",),
                    required_by_terminal=(),
                )
            }
        ),
    )
    assert denied.status is ToolExecutionStatus.NOT_EXECUTED
    assert denied.error_code == ToolExecutionErrorCode.POLICY_DENIED.value


@pytest.mark.asyncio
async def test_shell_builtins_run_only_approved_profiles_with_bounded_redacted_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    descriptor = _descriptor("builtin.shell.run_tests")
    executor = create_builtin_tool_executor(
        _plan(descriptor, _descriptor("builtin.terminal.submit"))
    )
    calls: list[dict[str, Any]] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        kwargs["args"] = args[0]
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=b"SECRET_TOKEN=abc123 /mnt/f/_Millrace/private.txt\n",
            stderr=b"",
        )

    monkeypatch.setattr(builtin_runtime.subprocess, "run", fake_run)

    result = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-shell",
        arguments={
            "profile": "tool-boundary",
            "selector": "all",
            "max_output_bytes": 64,
        },
        context=_context("process.test", workspace_root=tmp_path),
    )

    assert result.status is ToolExecutionStatus.SUCCESS
    assert result.structured_data["exit_code"] == 0
    assert "SECRET_TOKEN=abc123" not in result.summary
    assert "/mnt/f/_Millrace/private.txt" not in result.summary
    assert calls[0]["args"] == [
        "python",
        "-m",
        "pytest",
        "tests/test_tool_execution_boundary.py",
        "tests/test_builtin_tool_catalog.py",
    ]
    assert calls[0]["cwd"] == tmp_path
    assert calls[0]["shell"] is False
    assert set(calls[0]["env"]) <= {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SYSTEMROOT",
        "WINDIR",
        "PYTHONUNBUFFERED",
        "NO_COLOR",
        "LC_ALL",
    }

    rejected_shell_arguments: list[dict[str, Any]] = [
        {"profile": "python -m pytest"},
        {"profile": "tool-boundary", "command": "python -m pytest"},
        {"profile": "tool-boundary", "cwd": "."},
        {"profile": "tool-boundary", "env": {"TOKEN": "secret"}},
        {"profile": "tool-boundary", "args": ["-q"]},
        {"profile": "tool-boundary", "selector": "--network"},
        {"profile": "pip-install"},
    ]
    for index, arguments in enumerate(
        rejected_shell_arguments,
        start=1,
    ):
        rejected = await executor.execute_model_tool(
            model_tool_name=descriptor.model_tool_name,
            call_id=f"call-shell-rejected-{index}",
            arguments=arguments,
            context=_context("process.test", workspace_root=tmp_path),
        )
        assert rejected.status is ToolExecutionStatus.NOT_EXECUTED
        assert rejected.error_code in {
            ToolExecutionErrorCode.INVALID_ARGUMENTS.value,
            ToolBindingDenialCode.INVALID_ARGUMENTS.value,
        }


@pytest.mark.asyncio
async def test_shell_builtins_classify_failure_timeout_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    descriptor = _descriptor("builtin.shell.run_static_check")
    executor = create_builtin_tool_executor(
        _plan(descriptor, _descriptor("builtin.terminal.submit"))
    )

    def failing_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args[0], 1, stdout=b"bad", stderr=b"")

    monkeypatch.setattr(builtin_runtime.subprocess, "run", failing_run)
    failed = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-shell-failed",
        arguments={"profile": "tool-boundary-ruff"},
        context=_context("process.static_check", workspace_root=tmp_path),
    )
    assert failed.status is ToolExecutionStatus.SOFT_FAILURE
    assert failed.structured_data["exit_code"] == 1
    assert failed.side_effect_certainty is SideEffectCertainty.CONFIRMED_COMPLETE

    def timeout_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(
            cmd=["python", "-m", "ruff"],
            timeout=1,
            output=b"partial",
            stderr=b"/mnt/f/_Millrace/private.txt",
        )

    monkeypatch.setattr(builtin_runtime.subprocess, "run", timeout_run)
    timed_out = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-shell-timeout",
        arguments={"profile": "tool-boundary-ruff"},
        context=_context("process.static_check", workspace_root=tmp_path),
    )
    assert timed_out.status is ToolExecutionStatus.TIMED_OUT
    assert timed_out.error_code == ToolExecutionErrorCode.TIMEOUT.value
    assert timed_out.retryable is False
    assert timed_out.side_effect_certainty is SideEffectCertainty.COMPLETION_UNKNOWN
    assert "/mnt/f" not in timed_out.summary

    cancelled = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-shell-cancelled",
        arguments={"profile": "tool-boundary-ruff"},
        context=_context(
            "process.static_check",
            workspace_root=tmp_path,
            cancellation_requested=True,
        ),
    )
    assert cancelled.status is ToolExecutionStatus.NOT_EXECUTED
    assert cancelled.error_code == ToolExecutionErrorCode.CANCELLED.value


@pytest.mark.asyncio
async def test_terminal_builtins_validate_intents_and_required_artifacts(
    tmp_path: Path,
) -> None:
    submit = _descriptor("builtin.terminal.submit")
    reject = _descriptor("builtin.terminal.reject")
    escalate = _descriptor("builtin.terminal.escalate")
    plan = _plan(submit, reject, escalate)
    executor = create_builtin_tool_executor(plan)
    context = _context(
        "terminal.intent",
        compiled_artifact_policy=CompiledArtifactPolicy(
            declared_artifact_ids=("plan",),
            required_by_terminal=(
                TerminalArtifactRequirement(
                    terminal_result="BUILDER_COMPLETE",
                    artifact_ids=("plan",),
                ),
            ),
        ),
        input_artifacts=(
            ArtifactRef(artifact_id="plan", path=Path("millforge/plan.md")),
        ),
        artifact_root=tmp_path,
    )

    submitted = await executor.execute_model_tool(
        model_tool_name=submit.model_tool_name,
        call_id="call-terminal-submit",
        arguments={
            "terminal_result": "BUILDER_COMPLETE",
            "summary": "done",
            "artifact_refs": ["plan"],
        },
        context=context,
    )
    rejected = await executor.execute_model_tool(
        model_tool_name=reject.model_tool_name,
        call_id="call-terminal-reject",
        arguments={"terminal_result": "BUILDER_REJECTED", "summary": "reject"},
        context=context,
    )
    escalated = await executor.execute_model_tool(
        model_tool_name=escalate.model_tool_name,
        call_id="call-terminal-escalate",
        arguments={
            "terminal_result": "BUILDER_ESCALATED",
            "summary": "blocked",
            "blocker": "needs operator",
        },
        context=context,
    )

    assert submitted.status is ToolExecutionStatus.SUCCESS
    assert submitted.artifact_refs[0].artifact_id == "plan"
    assert rejected.status is ToolExecutionStatus.SUCCESS
    assert escalated.status is ToolExecutionStatus.SUCCESS

    missing_artifact = await executor.execute_model_tool(
        model_tool_name=submit.model_tool_name,
        call_id="call-terminal-missing-artifact",
        arguments={"terminal_result": "BUILDER_COMPLETE", "summary": "done"},
        context=context.model_copy(update={"input_artifacts": ()}),
    )
    assert missing_artifact.status is ToolExecutionStatus.NOT_EXECUTED
    assert (
        missing_artifact.error_code
        == ToolExecutionErrorCode.TERMINAL_INTENT_INVALID.value
    )

    invalid_result = await executor.execute_model_tool(
        model_tool_name=submit.model_tool_name,
        call_id="call-terminal-invalid-result",
        arguments={"terminal_result": "BLOCKED", "summary": "wrong"},
        context=context,
    )
    assert invalid_result.status is ToolExecutionStatus.NOT_EXECUTED
    assert (
        invalid_result.error_code
        == ToolExecutionErrorCode.TERMINAL_INTENT_INVALID.value
    )

    invalid_mapping_plan = plan.model_copy(
        update={
            "terminal_result_map": {
                **plan.terminal_result_map,
                plan.nodes[0].node_id: "BLOCKED",
            }
        }
    )
    invalid_mapping_executor = create_builtin_tool_executor(invalid_mapping_plan)
    invalid_mapping = await invalid_mapping_executor.execute_model_tool(
        model_tool_name=submit.model_tool_name,
        call_id="call-terminal-invalid-mapping",
        arguments={"terminal_result": "BUILDER_COMPLETE", "summary": "done"},
        context=context,
    )
    assert invalid_mapping.status is ToolExecutionStatus.NOT_EXECUTED
    assert (
        invalid_mapping.error_code
        == ToolExecutionErrorCode.TERMINAL_INTENT_INVALID.value
    )


@pytest.mark.asyncio
async def test_terminal_builtins_reject_model_authored_required_artifacts(
    tmp_path: Path,
) -> None:
    submit = _descriptor("builtin.terminal.submit")
    plan = _plan(submit)
    executor = create_builtin_tool_executor(plan)
    context = _context(
        "terminal.intent",
        compiled_artifact_policy=CompiledArtifactPolicy(
            declared_artifact_ids=("plan",),
            required_by_terminal=(
                TerminalArtifactRequirement(
                    terminal_result="BUILDER_COMPLETE",
                    artifact_ids=("plan",),
                ),
            ),
        ),
        input_artifacts=(),
        artifact_root=tmp_path,
    )

    result = await executor.execute_model_tool(
        model_tool_name=submit.model_tool_name,
        call_id="call-terminal-model-authored-artifact",
        arguments={
            "terminal_result": "BUILDER_COMPLETE",
            "summary": "done",
            "artifact_refs": ["plan"],
        },
        context=context,
    )

    assert result.status is ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == ToolExecutionErrorCode.TERMINAL_INTENT_INVALID.value


@pytest.mark.asyncio
async def test_terminal_builtins_reject_unknown_explicit_artifact_refs(
    tmp_path: Path,
) -> None:
    submit = _descriptor("builtin.terminal.submit")
    plan = _plan(submit)
    executor = create_builtin_tool_executor(plan)
    context = _context(
        "terminal.intent",
        compiled_artifact_policy=CompiledArtifactPolicy(
            declared_artifact_ids=("plan",),
            required_by_terminal=(
                TerminalArtifactRequirement(
                    terminal_result="BUILDER_COMPLETE",
                    artifact_ids=("plan",),
                ),
            ),
        ),
        input_artifacts=(
            ArtifactRef(artifact_id="plan", path=Path("millforge/plan.md")),
        ),
        artifact_root=tmp_path,
    )

    result = await executor.execute_model_tool(
        model_tool_name=submit.model_tool_name,
        call_id="call-terminal-unknown-artifact",
        arguments={
            "terminal_result": "BUILDER_COMPLETE",
            "summary": "done",
            "artifact_refs": ["bogus"],
        },
        context=context,
    )

    assert result.status is ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == ToolExecutionErrorCode.TERMINAL_INTENT_INVALID.value


def test_terminal_builtins_do_not_reference_millrace_control_surfaces() -> None:
    source = Path(builtin_runtime.__file__).read_text(encoding="utf-8")
    terminal_region = source.split("def _terminal_intent", 1)[1].split(
        "def call_node_produced_ids", 1
    )[0]

    for forbidden in ("queue", "daemon", "tmux", "git", "release"):
        assert forbidden not in terminal_region


@pytest.mark.parametrize(
    "tool_id,arguments,capability,context_update,expected_code",
    [
        (
            "builtin.workspace.read_file",
            {"path": "../outside.txt"},
            "workspace.read",
            {},
            ToolExecutionErrorCode.POLICY_DENIED.value,
        ),
        (
            "builtin.artifact.read",
            {"artifact_id": "checker_verdict"},
            "artifact.read",
            {
                "compiled_artifact_policy": CompiledArtifactPolicy(
                    declared_artifact_ids=("plan",),
                    required_by_terminal=(),
                )
            },
            ToolExecutionErrorCode.POLICY_DENIED.value,
        ),
        (
            "builtin.shell.run_tests",
            {"profile": "python -m pytest"},
            "process.test",
            {},
            ToolExecutionErrorCode.INVALID_ARGUMENTS.value,
        ),
        (
            "builtin.terminal.submit",
            {"terminal_result": "BUILDER_COMPLETE", "summary": "done"},
            "terminal.intent",
            {
                "compiled_artifact_policy": CompiledArtifactPolicy(
                    declared_artifact_ids=("plan",),
                    required_by_terminal=(
                        TerminalArtifactRequirement(
                            terminal_result="BUILDER_COMPLETE",
                            artifact_ids=("plan",),
                        ),
                    ),
                ),
                "input_artifacts": (),
            },
            ToolExecutionErrorCode.TERMINAL_INTENT_INVALID.value,
        ),
    ],
)
def test_builtin_pre_entry_policy_validator_denies_before_implementation_entry(
    tmp_path: Path,
    tool_id: str,
    arguments: dict[str, Any],
    capability: str,
    context_update: dict[str, Any],
    expected_code: str,
) -> None:
    descriptor = _descriptor(tool_id)
    terminal = _descriptor("builtin.terminal.submit")
    plan = (
        _plan(descriptor)
        if descriptor.tool_id == terminal.tool_id
        else _plan(
            descriptor,
            terminal,
        )
    )
    executor = create_builtin_tool_executor(plan)
    resolved = executor.validate_model_tool_call(
        model_tool_name=descriptor.model_tool_name,
        call_id=f"call-pre-entry-{tool_id}",
        arguments=arguments,
    )
    assert isinstance(resolved, ValidatedToolCall)

    context = _context(
        capability,
        workspace_root=tmp_path,
        artifact_root=tmp_path,
    ).model_copy(update=context_update)
    result = builtin_runtime.validate_builtin_pre_entry_policy(resolved, context)

    assert result is not None
    assert result.status is ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == expected_code
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert result.output_sha256 is None


def test_builtin_pre_entry_policy_validator_denies_missing_read_diff_root(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor("builtin.workspace.read_diff")
    plan = _plan(descriptor, _descriptor("builtin.terminal.submit"))
    executor = create_builtin_tool_executor(plan)
    resolved = executor.validate_model_tool_call(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-pre-entry-missing-read-diff-root",
        arguments={},
    )
    assert isinstance(resolved, ValidatedToolCall)

    result = builtin_runtime.validate_builtin_pre_entry_policy(
        resolved,
        _context(
            "workspace.diff.read",
            workspace_root=tmp_path / "missing-workspace",
        ),
    )

    assert result is not None
    assert result.status is ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == ToolExecutionErrorCode.POLICY_DENIED.value
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert result.output_sha256 is None


@pytest.mark.asyncio
async def test_builtin_pre_entry_policy_denial_blocks_missing_read_diff_dispatch(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor("builtin.workspace.read_diff")
    calls: list[ValidatedToolCall] = []
    executor = _executor_with_call_log(
        _plan(descriptor, _descriptor("builtin.terminal.submit")),
        calls,
    )

    result = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-pre-entry-missing-read-diff-dispatch",
        arguments={},
        context=_context(
            "workspace.diff.read",
            workspace_root=tmp_path / "missing-workspace",
        ),
    )

    assert result.status is ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == ToolExecutionErrorCode.POLICY_DENIED.value
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert result.output_sha256 is None
    assert calls == []
    assert len(executor.trace_records) == 1
    trace = executor.trace_records[0]
    assert trace.execution_status is ToolExecutionStatus.NOT_EXECUTED
    assert trace.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert trace.output_sha256 is None


@pytest.mark.parametrize(
    "tool_id,arguments,capability,context_update,expected_code",
    [
        (
            "builtin.workspace.read_file",
            {"path": "../outside.txt"},
            "workspace.read",
            {},
            ToolExecutionErrorCode.POLICY_DENIED.value,
        ),
        (
            "builtin.artifact.read",
            {"artifact_id": "checker_verdict"},
            "artifact.read",
            {
                "compiled_artifact_policy": CompiledArtifactPolicy(
                    declared_artifact_ids=("plan",),
                    required_by_terminal=(),
                )
            },
            ToolExecutionErrorCode.POLICY_DENIED.value,
        ),
        (
            "builtin.shell.run_tests",
            {"profile": "python -m pytest"},
            "process.test",
            {},
            ToolExecutionErrorCode.INVALID_ARGUMENTS.value,
        ),
        (
            "builtin.terminal.submit",
            {"terminal_result": "BUILDER_COMPLETE", "summary": "done"},
            "terminal.intent",
            {
                "compiled_artifact_policy": CompiledArtifactPolicy(
                    declared_artifact_ids=("plan",),
                    required_by_terminal=(
                        TerminalArtifactRequirement(
                            terminal_result="BUILDER_COMPLETE",
                            artifact_ids=("plan",),
                        ),
                    ),
                ),
                "input_artifacts": (),
            },
            ToolExecutionErrorCode.TERMINAL_INTENT_INVALID.value,
        ),
    ],
)
@pytest.mark.asyncio
async def test_builtin_pre_entry_policy_denial_blocks_dispatch(
    tmp_path: Path,
    tool_id: str,
    arguments: dict[str, Any],
    capability: str,
    context_update: dict[str, Any],
    expected_code: str,
) -> None:
    descriptor = _descriptor(tool_id)
    terminal = _descriptor("builtin.terminal.submit")
    plan = (
        _plan(descriptor)
        if descriptor.tool_id == terminal.tool_id
        else _plan(
            descriptor,
            terminal,
        )
    )
    calls: list[ValidatedToolCall] = []
    executor = _executor_with_call_log(plan, calls)

    context = _context(
        capability,
        workspace_root=tmp_path,
        artifact_root=tmp_path,
    ).model_copy(update=context_update)
    result = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id=f"call-pre-entry-dispatch-{tool_id}",
        arguments=arguments,
        context=context,
    )

    assert result.status is ToolExecutionStatus.NOT_EXECUTED
    assert result.error_code == expected_code
    assert result.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert result.output_sha256 is None
    assert calls == []
    assert len(executor.trace_records) == 1
    trace = executor.trace_records[0]
    assert trace.execution_status is ToolExecutionStatus.NOT_EXECUTED
    assert trace.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert trace.output_sha256 is None


def test_builtin_pre_entry_policy_validator_allows_safe_read_policy(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor("builtin.workspace.read_file")
    target = tmp_path / "safe.txt"
    target.write_text("ok", encoding="utf-8")
    executor = create_builtin_tool_executor(
        _plan(descriptor, _descriptor("builtin.terminal.submit"))
    )
    resolved = executor.validate_model_tool_call(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-pre-entry-safe-read",
        arguments={"path": "safe.txt"},
    )
    assert isinstance(resolved, ValidatedToolCall)

    result = builtin_runtime.validate_builtin_pre_entry_policy(
        resolved,
        _context("workspace.read", workspace_root=tmp_path),
    )

    assert result is None


def test_builtin_pre_entry_policy_validator_allows_safe_read_diff_policy(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor("builtin.workspace.read_diff")
    plan = _plan(descriptor, _descriptor("builtin.terminal.submit"))
    executor = create_builtin_tool_executor(plan)
    resolved = executor.validate_model_tool_call(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-pre-entry-safe-read-diff",
        arguments={"paths": ["safe.txt"]},
    )
    assert isinstance(resolved, ValidatedToolCall)

    result = builtin_runtime.validate_builtin_pre_entry_policy(
        resolved,
        _context("workspace.diff.read", workspace_root=tmp_path),
    )

    assert result is None


@pytest.mark.asyncio
async def test_success_results_are_redacted_bounded_and_hashed_from_sanitized_output(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor("builtin.workspace.read_file")
    terminal_descriptor = _descriptor("builtin.terminal.submit")
    plan = _plan(descriptor, terminal_descriptor)
    (tmp_path / "README.md").write_text("read complete\n", encoding="utf-8")
    raw_summary = "SECRET_TOKEN=abc123 /mnt/f/_Millrace/private.txt " + (
        "summary " * 1500
    )
    raw_content = "/mnt/f/_Millrace/private.txt SECRET_TOKEN=abc123 " + (
        "content " * 140000
    )

    async def implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=call.call_id,
            status=ToolExecutionStatus.SUCCESS,
            summary=raw_summary,
            structured_data={
                "status": "success",
                "summary": "read complete",
                "content": raw_content,
                "truncated": False,
                "artifact_refs": [],
            },
            side_effect_class=descriptor.side_effect_class,
            idempotency=descriptor.idempotency,
            side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
            input_sha256="c" * 64,
            output_sha256="f" * 64,
            timing=TimingMetadata(
                started_at="1970-01-01T00:00:00+00:00",
                completed_at="1970-01-01T00:00:00+00:00",
                duration_ms=0.0,
            ),
        )

    registry = RuntimeToolRegistry()
    registry.register(descriptor.implementation_id, implementation)
    executor = create_tool_executor(
        plan=plan,
        descriptor_snapshot=create_builtin_tool_snapshot(),
        runtime_registry=registry,
    )

    result = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-redacted",
        arguments={"path": "README.md"},
        context=_context("workspace.read", workspace_root=tmp_path),
    )

    assert result.status is ToolExecutionStatus.SUCCESS
    assert "SECRET_TOKEN=abc123" not in result.summary
    assert "/mnt/f/_Millrace/private.txt" not in result.summary
    assert "[truncated]" in result.summary
    assert "SECRET_TOKEN=abc123" not in result.structured_data["content"]
    assert "/mnt/f/_Millrace/private.txt" not in result.structured_data["content"]
    assert "[truncated]" in result.structured_data["content"]
    assert result.output_sha256 == canonical_sha256(result.structured_data)
    trace = executor.trace_records[-1]
    assert trace.execution_status is ToolExecutionStatus.SUCCESS
    assert trace.summary == result.summary
    assert trace.output_sha256 == result.output_sha256


@pytest.mark.asyncio
async def test_implementation_error_output_validation_and_redaction() -> None:
    descriptor = _descriptor("builtin.request.inspect")
    terminal_descriptor = _descriptor("builtin.terminal.submit")
    plan = _plan(descriptor, terminal_descriptor)

    async def raises(
        _call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        raise RuntimeError("SECRET_TOKEN=abc123 path /mnt/f/_Millrace/private.txt")

    registry = RuntimeToolRegistry()
    registry.register(descriptor.implementation_id, raises)
    registry.register(terminal_descriptor.implementation_id, raises)
    executor = create_tool_executor(
        plan=plan,
        descriptor_snapshot=create_builtin_tool_snapshot(),
        runtime_registry=registry,
    )

    impl_error = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-impl-error",
        arguments={},
        context=_context("request.read"),
    )

    assert impl_error.status is ToolExecutionStatus.HARD_FAILURE
    assert impl_error.error_code == ToolExecutionErrorCode.IMPLEMENTATION_ERROR.value
    assert impl_error.side_effect_certainty is SideEffectCertainty.COMPLETION_UNKNOWN
    assert impl_error.retryable is False
    assert "/mnt/f" not in str(impl_error.structured_data)
    assert "abc123" not in str(impl_error.structured_data)

    calls: list[ValidatedToolCall] = []
    bad_output_executor = _executor_with_call_log(plan, calls, invalid_output=True)
    output_error = await bad_output_executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-output-error",
        arguments={},
        context=_context("request.read"),
    )
    assert (
        output_error.error_code == ToolExecutionErrorCode.OUTPUT_VALIDATION_FAILED.value
    )
    assert output_error.status is ToolExecutionStatus.HARD_FAILURE


@pytest.mark.asyncio
async def test_terminal_intent_trace_validation_and_retryability_rules() -> None:
    descriptor = _descriptor("builtin.terminal.submit")
    executor = _executor_with_call_log(
        _plan(_descriptor("builtin.request.inspect"), descriptor),
        [],
    )

    result = await executor.execute_model_tool(
        model_tool_name=descriptor.model_tool_name,
        call_id="call-terminal",
        arguments={"terminal_result": "BLOCKED", "summary": "wrong"},
        context=_context("terminal.intent"),
    )

    assert result.error_code == ToolExecutionErrorCode.TERMINAL_INTENT_INVALID.value
    trace = executor.trace_records[-1]
    assert trace.tool_call_id == "call-terminal"
    assert trace.side_effect_certainty is SideEffectCertainty.NOT_ATTEMPTED
    assert trace.output_sha256 is None

    with pytest.raises(ValueError, match="completion_unknown"):
        ToolExecutionResult(
            call_id="call-unsafe-retry",
            status=ToolExecutionStatus.HARD_FAILURE,
            summary="unknown",
            structured_data={},
            error_code=ToolExecutionErrorCode.AMBIGUOUS_SIDE_EFFECT.value,
            retryable=True,
            side_effect_class=SideEffectClass.WORKSPACE_WRITE,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            side_effect_certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
            side_effect_record=SideEffectRecord(
                certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                detail_code=ToolExecutionErrorCode.AMBIGUOUS_SIDE_EFFECT.value,
                summary="unknown",
                retry_allowed=True,
            ),
            input_sha256="d" * 64,
            output_sha256=None,
            timing=TimingMetadata(
                started_at="1970-01-01T00:00:00+00:00",
                completed_at="1970-01-01T00:00:00+00:00",
                duration_ms=0.0,
            ),
        )


def _executor_with_call_log(
    plan: CompiledHarnessPlan,
    calls: list[ValidatedToolCall],
    *,
    invalid_output: bool = False,
) -> Any:
    async def implementation(
        call: ValidatedToolCall, _context: ToolExecutionContext
    ) -> ToolExecutionResult:
        calls.append(call)
        descriptor = _descriptor_by_implementation(call.binding.implementation_id)
        if invalid_output:
            return _invalid_output(call, descriptor)
        return _success(call, descriptor)

    registry = RuntimeToolRegistry()
    for descriptor in iter_builtin_tool_descriptors():
        registry.register(descriptor.implementation_id, implementation)
    return create_tool_executor(
        plan=plan,
        descriptor_snapshot=create_builtin_tool_snapshot(),
        runtime_registry=registry,
    )


def _descriptor(tool_id: str) -> ToolDescriptor:
    for descriptor in iter_builtin_tool_descriptors():
        if descriptor.tool_id == tool_id:
            return descriptor
    raise AssertionError(tool_id)


def _connector_descriptor(**updates: Any) -> ToolDescriptor:
    values: dict[str, Any] = {
        "tool_id": "connector.test.echo",
        "tool_version": 1,
        "implementation_id": "impl.connector.test.echo.v1",
        "model_tool_name": "connector_test_echo",
        "description": "Synthetic connector-shaped echo descriptor.",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "enum": ["success", "soft_failure", "hard_failure"],
                    "type": "string",
                },
                "summary": {"type": "string"},
            },
            "required": ["status", "summary"],
            "additionalProperties": False,
        },
        "required_capabilities": (),
        "produced_artifact_ids": (),
        "side_effect_class": SideEffectClass.READ_ONLY,
        "idempotency": IdempotencyClass.IDEMPOTENT,
        "timeout_policy": {
            "timeout_seconds": 30,
            "cancellation_grace_seconds": 5,
        },
        "output_policy": {
            "max_output_bytes": 4096,
            "max_summary_utf8": 512,
            "redact_secrets": True,
        },
    }
    values.update(updates)
    return ToolDescriptor(**values)


def _connector_admission_snapshot_for(
    descriptor: ToolDescriptor,
    snapshot: FrozenToolRegistrySnapshot,
) -> ConnectorAdmissionSnapshot:
    record = ConnectorAdmissionRecord(
        connector_id="connector.test",
        provider_tool_name="echo",
        connector_identity_sha256="1" * 64,
        discovery_snapshot_sha256="2" * 64,
        raw_tool_sha256="3" * 64,
        descriptor_sha256=descriptor.descriptor_sha256,
        required_capabilities=descriptor.required_capabilities,
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
        timeout_policy=descriptor.timeout_policy,
        output_policy=descriptor.output_policy,
        approval_policy=ConnectorApprovalPolicy.NONE,
    )
    return ConnectorAdmissionSnapshot(records=(record,), descriptor_snapshot=snapshot)


def _descriptor_by_implementation(implementation_id: str) -> ToolDescriptor:
    for descriptor in iter_builtin_tool_descriptors():
        if descriptor.implementation_id == implementation_id:
            return descriptor
    raise AssertionError(implementation_id)


def _plan(*descriptors: ToolDescriptor) -> CompiledHarnessPlan:
    nodes = tuple(
        _node(index, descriptor) for index, descriptor in enumerate(descriptors)
    )
    required_capabilities = tuple(
        sorted(
            {capability for node in nodes for capability in node.required_capabilities}
        )
    )
    artifacts = tuple(
        sorted({artifact for node in nodes for artifact in node.produced_artifact_ids})
    )
    terminal_requirements = tuple(
        TerminalArtifactRequirement(
            terminal_result=node.terminal_result,
            artifact_ids=node.produced_artifact_ids,
        )
        for node in nodes
        if node.terminal_result is not None and node.produced_artifact_ids
    )
    plan = CompiledHarnessPlan(
        schema_version="1.0",
        kind="compiled_millforge_harness",
        harness_id="harness.tool-boundary",
        harness_version=1,
        source_sha256="1" * 64,
        compiled_sha256="0" * 64,
        stage_kind_ids=("builder",),
        model_profile=CompiledModelProfile(profile_id="profile.standard"),
        prompt_policy=CompiledPromptPolicy(
            policy_id="prompt.standard",
            system_instructions="Run tools.",
            include_request_context=True,
        ),
        budgets=CompiledBudgetPolicy(
            max_iterations=3,
            max_validation_retries=1,
            max_tool_errors=1,
            max_prerequisite_violations=1,
            max_premature_terminal_attempts=1,
        ),
        context_policy=CompiledContextPolicy(
            strategy_id="forge.tiered.v1",
            budget_tokens=4096,
            keep_recent_iterations=1,
            phase_thresholds=(0.5, 0.75, 0.9),
        ),
        nodes=nodes,
        required_capabilities=required_capabilities,
        terminal_result_map={
            node.node_id: node.terminal_result
            for node in nodes
            if node.terminal_result is not None
        },
        artifact_policy=CompiledArtifactPolicy(
            declared_artifact_ids=artifacts,
            required_by_terminal=terminal_requirements,
        ),
        compiler_identity=CompilerIdentity(
            name="test-compiler",
            version="1",
            build_id="test",
        ),
    )
    return finalize_compiled_plan_sha256(plan)


def _node(index: int, descriptor: ToolDescriptor) -> CompiledHarnessNode:
    terminal_results = {
        "builtin.terminal.submit": "BUILDER_COMPLETE",
        "builtin.terminal.reject": "BUILDER_REJECTED",
        "builtin.terminal.escalate": "BUILDER_ESCALATED",
    }
    terminal_result = terminal_results.get(descriptor.tool_id)
    return CompiledHarnessNode(
        node_id=f"node-{index}-{descriptor.tool_id.rsplit('.', 1)[-1]}",
        model_tool_name=descriptor.model_tool_name,
        description=descriptor.description,
        input_schema=descriptor.model_dump(mode="json")["input_schema"],
        binding=ToolBindingRef(
            tool_id=descriptor.tool_id,
            tool_version=descriptor.tool_version,
            descriptor_sha256=descriptor.descriptor_sha256,
            implementation_id=descriptor.implementation_id,
        ),
        prerequisites=(),
        required=terminal_result is None,
        terminal_result=terminal_result,
        required_capabilities=descriptor.required_capabilities,
        produced_artifact_ids=descriptor.produced_artifact_ids,
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
    )


def _context(
    *capabilities: str,
    current_monotonic: float = 0.0,
    cancellation_requested: bool = False,
    workspace_root: Path | None = None,
    artifact_root: Path | None = None,
    compiled_artifact_policy: CompiledArtifactPolicy | None = None,
    input_artifacts: tuple[ArtifactRef, ...] = (),
    work_item_id: str | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="request-1",
        run_id="run-1",
        stage=StageIdentity(
            plane="execution",
            node_id="builder",
            stage_kind_id="builder",
        ),
        run_directory=RunDirRef(run_id="run-1", path=Path("/tmp/run-1")),
        capability_envelope=CapabilityEnvelope(
            grants=tuple(CapabilityGrant(capability_id=item) for item in capabilities)
        ),
        timeout=TimeoutRef(timeout_seconds=60.0),
        cancellation=CancellationRef(cancellation_id="cancel-1"),
        deadline=Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=60.0,
            effective_deadline_monotonic=60.0,
            source="request",
        ),
        workspace_root=workspace_root or Path("/tmp/workspace"),
        artifact_root=artifact_root or Path("/tmp/run-1/artifacts"),
        compiled_artifact_policy=compiled_artifact_policy,
        input_artifacts=input_artifacts,
        work_item_id=work_item_id,
        cancellation_requested=cancellation_requested,
        current_monotonic=current_monotonic,
    )


def _success(
    call: ValidatedToolCall, descriptor: ToolDescriptor
) -> ToolExecutionResult:
    output = _valid_output(descriptor)
    return ToolExecutionResult(
        call_id=call.call_id,
        status=ToolExecutionStatus.SUCCESS,
        summary="ok",
        structured_data=output,
        side_effect_class=descriptor.side_effect_class,
        idempotency=descriptor.idempotency,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        input_sha256="c" * 64,
        output_sha256="e" * 64,
        timing=TimingMetadata(
            started_at="1970-01-01T00:00:00+00:00",
            completed_at="1970-01-01T00:00:00+00:00",
            duration_ms=0.0,
        ),
    )


def _invalid_output(
    call: ValidatedToolCall, descriptor: ToolDescriptor
) -> ToolExecutionResult:
    return _success(call, descriptor).model_copy(
        update={"structured_data": {"ok": True}}
    )


def _valid_output(descriptor: ToolDescriptor) -> dict[str, Any]:
    common = {"status": "success", "summary": "ok"}
    if descriptor.tool_id == "builtin.request.inspect":
        return {
            **common,
            "request_id": "request-1",
            "stage_id": "builder",
            "objective": "test",
            "artifact_refs": [],
            "truncated": False,
        }
    if descriptor.tool_id == "builtin.terminal.submit":
        return {**common, "terminal_result": "BUILDER_COMPLETE"}
    if descriptor.tool_id == "builtin.terminal.reject":
        return {**common, "terminal_result": "BUILDER_REJECTED"}
    if descriptor.tool_id == "builtin.terminal.escalate":
        return {**common, "terminal_result": "BUILDER_ESCALATED"}
    if descriptor.tool_id == "builtin.workspace.read_file":
        return {
            **common,
            "content": "hello",
            "truncated": False,
            "artifact_refs": [],
        }
    return common
